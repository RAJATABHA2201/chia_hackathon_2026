"""N22 -- the T1 analytical model.

The tier that is missing entirely from Fig. 1 (review Sec 2.3, Finding 1). It
predicts, in seconds and with no simulation:

  cycles ~ max( MACs_useful / (P * eta_balance),
                bytes_offchip / BW,
                indices / decode_rate )

Two of its outputs are **exact, not estimated** -- on-chip SRAM footprint and
metadata bytes -- which lets T1 reject a large fraction of proposals with zero
uncertainty. The rest carry a calibrated slack ``delta_o`` that N23 measures
against T2b; until that calibration exists, ``DELTA_UNCALIBRATED`` is used and
every T1 rejection is logged as provisional.

Because T3 synthesis is deferred on this host, T1 also carries the analytical
area and period estimates, so the objective stays 3-D ``(t, E, A)`` from day one
rather than collapsing to cycles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

from design_state import DesignState
from t0_legality import INPUT_W, ACC_W, INPUT_BYTES, ACC_BYTES, derive

# --- Technology / memory constants -----------------------------------------
# Relative, not absolute: everything is reported as a delta against the baseline
# measured in the same model, which is the only defensible use of these numbers.
SRAM_BIT_AREA_UM2 = 0.15        # per bit, 1R1W SRAM macro at an unspecified node
MAC_AREA_UM2 = 55.0             # per INT8 MAC + accumulate
CTRL_AREA_UM2 = 1.2e5           # fixed controller/DMA/queue overhead

BASE_PERIOD_NS = 1.0            # period of a DIM=16 array before depth penalty
# Fmax degrades with reduction-tree depth: period grows ~log2(DIM).
PERIOD_PER_LOG2_DIM_NS = 0.055
PERIOD_PER_BANK_NS = 0.004      # bank mux / address-decode pressure

DRAM_LATENCY_CYCLES = 100
ENERGY_PJ_PER_MAC = 0.30
ENERGY_PJ_PER_SRAM_BYTE = 1.2
ENERGY_PJ_PER_DRAM_BYTE = 22.0

DELTA_UNCALIBRATED = 0.35       # 35% slack until N23 measures the real value


@dataclass
class Workload:
    """A prefill-attention shape. Fixed, outside the loop, hashed into the trace."""
    seq_len: int = 4096
    n_heads: int = 8
    d_head: int = 64
    block_size: int = 32
    density: float = 0.25        # fraction of B x B score blocks retained
    causal: bool = True

    def n_blocks(self) -> int:
        return max(1, self.seq_len // self.block_size)

    def nnz_blocks(self) -> int:
        total = self.n_blocks() ** 2
        if self.causal:
            total = self.n_blocks() * (self.n_blocks() + 1) // 2
        return max(1, int(total * self.density))

    def eta_balance(self) -> float:
        """mean_p(nnz_p)/max_p(nnz_p) -- computable from the density map alone.

        Causal masking makes nnz-per-row triangular: row 0 has one block, the
        last row has n_blocks. Naive row-to-PE-row assignment costs up to 2x.
        """
        if not self.causal:
            return 1.0
        n = self.n_blocks()
        mean_nnz = (n + 1) / 2.0
        return mean_nnz / n if n else 1.0


@dataclass
class Prediction:
    cycles: float
    bound_by: str               # which term bound the prediction
    bytes_offchip: float
    sram_bytes: int             # EXACT
    metadata_bytes: int         # EXACT
    eta_balance: float
    kv_refetch: float
    area_um2: float
    period_ns: float
    time_ns: float
    energy_pj: float

    def objective(self) -> tuple:
        """(t, E, A) -- the three objectives. U and S stay diagnostic."""
        return (self.time_ns, self.energy_pj, self.area_um2)

    def to_dict(self) -> dict:
        return asdict(self)


def sram_bytes_exact(s: DesignState) -> int:
    """On-chip SRAM footprint. Exact -- this is arithmetic, not estimation."""
    return (s.sp_capacity_kb + s.acc_capacity_kb) * 1024


def metadata_bytes_exact(s: DesignState, w: Workload) -> int:
    """Sparsity metadata. Exact for a given format choice.

    Bitmap is (S/B)^2 bits; blocked-CSR is nnz_blocks x 2 x 16 b. The crossover
    is computable, and picking the cheaper of the two is the review's Sec 1.2(2)
    sanity check that the search is working.
    """
    n = w.n_blocks()
    bitmap = (n * n + 7) // 8
    blocked_csr = w.nnz_blocks() * 2 * 2
    return min(bitmap, blocked_csr) * w.n_heads


def predict(s: DesignState, w: Workload) -> Prediction:
    d = derive(s)
    dim = d.dim
    p_mac = s.pe_count

    # --- useful MACs: only retained blocks are computed ---
    macs_per_block = w.block_size * w.block_size * w.d_head
    # QK^T and PV are both B x B x d_head per retained block.
    macs_useful = w.nnz_blocks() * macs_per_block * 2 * w.n_heads

    eta = w.eta_balance()
    compute_cycles = macs_useful / (p_mac * eta) if p_mac else float("inf")

    # --- off-chip traffic: Q, K, V tiles plus the O writeback ---
    # K/V refetch factor: how many passes over KV the tiling forces. A tile row
    # that fits the scratchpad is fetched once; otherwise it is re-fetched.
    kv_tile_bytes = 2 * w.block_size * w.d_head * INPUT_BYTES
    sp_bytes_avail = s.sp_capacity_kb * 1024 / 2.0          # double-buffered
    tiles_resident = max(1.0, sp_bytes_avail / max(1.0, kv_tile_bytes))
    refetch = max(1.0, w.n_blocks() / tiles_resident)
    kv_bytes = 2 * w.seq_len * w.d_head * INPUT_BYTES * w.n_heads * refetch
    q_bytes = w.seq_len * w.d_head * INPUT_BYTES * w.n_heads
    o_bytes = w.seq_len * w.d_head * ACC_BYTES * w.n_heads
    meta_bytes = metadata_bytes_exact(s, w)
    bytes_offchip = kv_bytes + q_bytes + o_bytes + meta_bytes

    bw_bytes_per_cycle = s.dma_buswidth / 8.0
    # Little's Law: you only achieve full bandwidth with BW*L bytes in flight.
    in_flight = s.max_in_flight_mem_reqs * s.dma_maxbytes
    required = bw_bytes_per_cycle * DRAM_LATENCY_CYCLES
    bw_efficiency = min(1.0, in_flight / required) if required else 1.0
    achieved_bw = bw_bytes_per_cycle * bw_efficiency
    memory_cycles = bytes_offchip / achieved_bw if achieved_bw else float("inf")

    # --- metadata decode: one index per cycle per decoder ---
    decode_cycles = w.nnz_blocks() * w.n_heads

    cycles, bound_by = max(
        (compute_cycles, "compute"),
        (memory_cycles, "memory"),
        (decode_cycles, "index_decode"),
        key=lambda t: t[0],
    )

    # --- area: SRAM dominates 60-80% of tile area, as the review notes ---
    sram_b = sram_bytes_exact(s)
    area = (sram_b * 8 * SRAM_BIT_AREA_UM2) + (p_mac * MAC_AREA_UM2) + CTRL_AREA_UM2
    if s.has_normalizations:
        # I-BERT iexp + reciprocal + per-row m/l registers, ~2% of tile area.
        area *= 1.02

    # --- period: reduction-tree depth is the Fmax offender ---
    period = (BASE_PERIOD_NS
              + PERIOD_PER_LOG2_DIM_NS * math.log2(max(2, dim))
              + PERIOD_PER_BANK_NS * (s.sp_banks + s.acc_banks))

    # --- energy ---
    # Per-access SRAM energy grows with macro size (longer bitlines, deeper
    # decode); ~sqrt of capacity is the standard first-order scaling.
    sram_scale = math.sqrt(max(1.0, sram_b / (256 * 1024)))
    sram_traffic = macs_useful * 2 * INPUT_BYTES
    energy = (macs_useful * ENERGY_PJ_PER_MAC
              + sram_traffic * ENERGY_PJ_PER_SRAM_BYTE * sram_scale
              + bytes_offchip * ENERGY_PJ_PER_DRAM_BYTE)

    return Prediction(
        cycles=cycles, bound_by=bound_by, bytes_offchip=bytes_offchip,
        sram_bytes=sram_b, metadata_bytes=meta_bytes, eta_balance=eta,
        kv_refetch=refetch, area_um2=area, period_ns=period,
        # The objective is TIME, not cycles -- otherwise the loop rewards
        # designs that cannot close timing (review Sec 3.4, item 3).
        time_ns=cycles * period, energy_pj=energy,
    )


def dominates_with_slack(cand: Prediction, front: list, delta: float = DELTA_UNCALIBRATED) -> bool:
    """True if cand is predicted-dominated by the whole front, with slack.

    Rejects only when every objective is worse than the front's best by more
    than the model's own error. Until N23 measures delta, this is provisional
    and every rejection is logged as such.
    """
    if not front:
        return False
    c = cand.objective()
    bests = [min(p.objective()[i] for p in front) for i in range(len(c))]
    return all(c[i] > (1.0 + delta) * bests[i] for i in range(len(c)))
