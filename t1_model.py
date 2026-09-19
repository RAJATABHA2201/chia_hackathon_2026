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
# Per SRAM bit, NanGate45. CALIBRATED 2026-09-19 against the PDK's own
# fakeram45_*.lib macros rather than guessed: 26 macros, area/bits ranges
# 0.50-2.07 um2/bit, median 0.725 over all and 0.622 over the 12 macros at
# scratchpad scale (>= 8 Kbit), which is the regime Gemmini's scratchpad and
# accumulator occupy. The previous value, 0.15 at "an unspecified node",
# understated NanGate45 SRAM by 4.1x -- and since SRAM is 60-80% of accelerator
# area, that made every modelled area figure wrong in the direction that
# flatters a design for buying more scratchpad.
SRAM_BIT_AREA_UM2 = 0.6217
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
    """An SpMM shape. Fixed, outside the loop, hashed into the trace.

    Populated from the JSON that ``workload/prep_matrices.py`` writes beside
    each generated header, so the model and the kernel can never disagree about
    what is being computed. The attention shape this class used to carry
    (seq_len / n_heads / d_head / causal) is gone with the attention kernel.
    """
    M: int = 512                 # rows of A
    K: int = 512                 # cols of A / rows of X
    N: int = 64                  # dense width of X
    dim: int = 16                # Gemmini tile dimension
    nnz: int = 8192              # true nonzeros in A
    nz_blocks: int = 512         # dim x dim blocks containing >= 1 nonzero
    total_blocks: int = 1024
    dense_mode: bool = False     # B0 walks every block, zeros included

    @classmethod
    def from_stats(cls, stats: dict, dense_mode: bool = False) -> "Workload":
        """Build from a workload/generated/*.json emitted by prep_matrices.py."""
        return cls(M=stats["M"], K=stats["K"], N=stats["N"], dim=stats["dim"],
                   nnz=stats["nnz"], nz_blocks=stats["nz_blocks"],
                   total_blocks=stats["total_blocks"], dense_mode=dense_mode)

    def tiles_issued(self) -> int:
        """Tile matmuls the kernel actually issues."""
        return self.total_blocks if self.dense_mode else self.nz_blocks

    def macs_issued(self) -> int:
        """Products the array performs, zeros included."""
        return self.tiles_issued() * self.dim * self.dim * self.N

    def macs_useful(self) -> int:
        """Products with a nonzero A operand -- the work that must happen."""
        return self.nnz * self.N

    def in_block_density(self) -> float:
        """Density INSIDE issued blocks: the headroom RTL zero-skip competes for."""
        d = self.nz_blocks * self.dim * self.dim
        return (self.nnz / d) if d else 0.0

    def eta_balance(self) -> float:
        """Load balance across block rows.

        Unstructured sparsity spreads nonzero blocks unevenly over block rows,
        so a naive row-to-PE-row assignment idles on short rows. Without the
        per-row distribution here, the honest value is 1.0 rather than an
        invented one -- T1 is a filter, and a fabricated imbalance term would
        make it reject designs for a reason it cannot actually see.
        """
        return 1.0


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


def sram_macro_area_um2(s: DesignState) -> float:
    """Area of the SRAM macros, which post-synthesis LOGIC area excludes.

    T3 blackboxes the scratchpad/accumulator macros -- that is what makes the
    netlist tractable (11.6M cells -> 1.4M) and lets OpenSTA run at all -- but
    it also means yosys' reported area is logic ONLY. Since SRAM is 60-80% of
    accelerator area, quoting the logic figure as "the area" would understate
    the design several-fold and, worse, would price the ZBU's bitmap against
    nothing.

    This is the standard flow: macro area comes from the memory compiler's
    datasheet (here, the fakeram45 liberties), not from synthesising the RAM
    as flip-flops. It is a real number, but it is ADDED to the measured one,
    so any report must say so rather than implying both came from yosys.
    """
    return sram_bytes_exact(s) * 8 * SRAM_BIT_AREA_UM2


def metadata_bytes_exact(s: DesignState, w: Workload) -> int:
    """Sparsity metadata. Exact for a given format choice.

    Bitmap is one bit per block; blocked-CSR is two 16-bit coordinates per
    nonzero block. Picking the cheaper of the two is the sanity check that the
    format lever is being used rather than assumed.
    """
    bitmap = (w.total_blocks + 7) // 8
    blocked_csr = w.nz_blocks * 2 * 2
    return min(bitmap, blocked_csr)


def predict(s: DesignState, w: Workload) -> Prediction:
    """Analytical estimate for SpMM. A FILTER and a logged prediction, never a
    measurement -- and on this project T1/measured ran 186x apart on the old
    workload, so it must never be allowed to reject a design outright."""
    d = derive(s)
    dim = d.dim
    p_mac = s.pe_count

    macs = w.macs_issued()
    eta = w.eta_balance()
    compute_cycles = macs / (p_mac * eta) if p_mac else float("inf")

    # Off-chip traffic: every issued A block, the dense X once per block row,
    # and the INT32 Y writeback.
    a_bytes = w.tiles_issued() * dim * dim * INPUT_BYTES
    x_bytes = w.K * w.N * INPUT_BYTES * max(1, w.M // dim)
    y_bytes = w.M * w.N * ACC_BYTES
    meta_bytes = metadata_bytes_exact(s, w)
    bytes_offchip = a_bytes + x_bytes + y_bytes + meta_bytes

    bw_bytes_per_cycle = s.dma_buswidth / 8.0
    in_flight = s.max_in_flight_mem_reqs * s.dma_maxbytes
    required = bw_bytes_per_cycle * DRAM_LATENCY_CYCLES
    bw_efficiency = min(1.0, in_flight / required) if required else 1.0
    achieved_bw = bw_bytes_per_cycle * bw_efficiency
    memory_cycles = bytes_offchip / achieved_bw if achieved_bw else float("inf")

    # One block-index decode per issued tile.
    decode_cycles = w.tiles_issued()

    cycles, bound_by = max(
        (compute_cycles, "compute"),
        (memory_cycles, "memory"),
        (decode_cycles, "index_decode"),
        key=lambda t: t[0],
    )

    sram_b = sram_bytes_exact(s)
    area = (sram_b * 8 * SRAM_BIT_AREA_UM2) + (p_mac * MAC_AREA_UM2) + CTRL_AREA_UM2
    if s.has_normalizations:
        area *= 1.02
    # The RTL sparsity hardware is not free. SCNN's headline limitation is that
    # its design was LARGER than the dense equivalent; a model that cannot see
    # that cost would let the search buy it for nothing.
    if s.gate_enable:
        area *= 1.01                       # a comparator per PE
    if s.zbu_enable:
        bitmap_bits = d.sp_rows * (dim // max(1, s.granule_size))
        area += bitmap_bits * SRAM_BIT_AREA_UM2

    period = (BASE_PERIOD_NS
              + PERIOD_PER_LOG2_DIM_NS * math.log2(max(2, dim))
              + PERIOD_PER_BANK_NS * (s.sp_banks + s.acc_banks))

    sram_scale = math.sqrt(max(1.0, sram_b / (256 * 1024)))
    sram_traffic = macs * 2 * INPUT_BYTES
    # Energy is charged on macs ISSUED, not useful: a dense array pays for the
    # zeros it multiplies. Gating is what removes that, and it is the only
    # reason gate_enable can show an energy win here.
    active = macs
    if s.gate_enable:
        active = int(macs * w.in_block_density())
    energy = (active * ENERGY_PJ_PER_MAC
              + sram_traffic * ENERGY_PJ_PER_SRAM_BYTE * sram_scale
              + bytes_offchip * ENERGY_PJ_PER_DRAM_BYTE)

    return Prediction(
        cycles=cycles, bound_by=bound_by, bytes_offchip=bytes_offchip,
        sram_bytes=sram_b, metadata_bytes=meta_bytes, eta_balance=eta,
        kv_refetch=1.0, area_um2=area, period_ns=period,
        time_ns=cycles * period, energy_pj=energy,
    )


# ---------------------------------------------------------------------------
# N53 -- energy, power and efficiency from MEASURED counters.
#
# This replaces the circular version. The old energy term was
#   energy = macs_useful * E_mac + ...
# where macs_useful is reported BY THE KERNEL, so a sparser kernel reported
# lower energy whatever the hardware did. On a loop whose entire claim is that
# the HARDWARE did it, that axis proved nothing.
#
# What changes here:
#   * DRAM traffic is the measured RDMA_BYTES_REC + WDMA_BYTES_SENT counters,
#     not a model. That is the single largest term (~53% -- see plan Sec 9f).
#   * Compute energy is charged on macs_ISSUED, what the array actually
#     performed, not on macs_useful, what was arithmetically necessary. A dense
#     array pays for the zeros it multiplies; that gap IS the RTL opportunity.
#   * MAC_GATED_CYCLES, when the Phase-3 RTL counter exists, moves gated
#     multiplies onto a cheaper rate. Until then gating is modelled from
#     in-block density and the result is labelled as modelled.
# ---------------------------------------------------------------------------

# Energy of a gated MAC relative to an active one. Operand isolation stops the
# multiplier array toggling but the PE still clocks, so the floor is leakage
# plus clock-tree, not zero. 0.10 is a conservative placeholder pending a
# two-point OpenSTA power calibration; it is the one number here that is not
# measured or PDK-derived.
GATED_MAC_ENERGY_FRACTION = 0.10


@dataclass
class EnergyReport:
    energy_pj: float
    power_w: float
    perf_gops: float                 # useful ops/s (2 ops per useful MAC)
    perf_per_watt_gops_w: float      # == useful ops per joule
    energy_per_useful_op_pj: float
    mac_efficiency: float            # useful / issued -- how much work counted
    dram_measured: bool              # False => the DRAM term was modelled
    gating_measured: bool            # False => gating was modelled, not counted
    breakdown: dict

    def to_dict(self) -> dict:
        return asdict(self)


def energy_report(s: DesignState, m, w: Workload, period_ns: float) -> EnergyReport:
    """Per-iteration energy, power and performance-per-watt from measurement.

    ``m`` is a metrics.Metrics: measured cycles and Gemmini's own counters.
    ``period_ns`` is the measured clock period when T3 ran, else T1's estimate.
    """
    cycles = max(1, m.cycles)
    time_s = cycles * period_ns * 1e-9

    macs_issued = m.counters.get("macs_issued") or w.macs_issued()
    macs_useful = m.macs_useful or w.macs_useful()

    # --- compute -----------------------------------------------------------
    # MAC_GATED_TOTAL counts zero-valued A operands presented to the array. It
    # lives in ExecuteController and is NOT predicated on SparseCraftRTL.
    # gateEnable, so it reports the same OPPORTUNITY in the gated and ungated
    # builds alike. Only the gated build converts that opportunity into saved
    # energy, so `s.gate_enable` still decides whether the discount applies --
    # without this the ungated arm receives the same discount and the T-A A/B
    # measures exactly zero benefit for a purely clerical reason.
    gated_opportunity = m.counters.get("MAC_GATED_TOTAL")   # Phase-3 RTL counter (3.4)
    gating_measured = gated_opportunity is not None
    if gating_measured:
        gated = gated_opportunity if s.gate_enable else 0
    else:
        gated_opportunity = int(macs_issued * (1.0 - w.in_block_density()))
        gated = gated_opportunity if s.gate_enable else 0
    # HARD consistency check. A measured gated count above the issued count is
    # physically impossible and means the RTL counter is miscounting (it did:
    # ta-gated4 reported 385,024 gated against 262,144 issued, 1.47x, by
    # counting flush/preload/garbage rows and padded lanes). The old
    # `max(0, ...)` below SILENTLY clamped that to active=0 and produced a
    # plausible-looking energy number, which is how the bug survived a run
    # that reported ADMIT_FRONT with every gate green. Fail loudly instead:
    # a bad counter invalidates the entire energy report, so there is no
    # useful degraded mode to fall back to.
    if gating_measured and gated_opportunity > macs_issued:
        raise ValueError(
            f"MAC_GATED_TOTAL={gated_opportunity:,} exceeds macs_issued={macs_issued:,} "
            f"({gated_opportunity / max(1, macs_issued):.2f}x) -- the RTL gated-MAC counter "
            f"is over-counting. Energy is not reportable. Check the guard in "
            f"rtl_scaffold.apply_counters (a_garbage / a_unpadded_cols / "
            f"im2colling) before trusting any number from this iteration."
        )
    active = max(0, macs_issued - gated)
    e_mac = (active * ENERGY_PJ_PER_MAC
             + gated * ENERGY_PJ_PER_MAC * GATED_MAC_ENERGY_FRACTION)

    # --- on-chip traffic ---------------------------------------------------
    # Two operand bytes per issued MAC. A skipped granule never reaches the
    # array, so T-B removes these as well as the multiply -- which is why it
    # dominates T-A on this workload (plan Sec 9f).
    sram_scale = math.sqrt(max(1.0, sram_bytes_exact(s) / (256 * 1024)))
    e_sram = macs_issued * 2 * INPUT_BYTES * ENERGY_PJ_PER_SRAM_BYTE * sram_scale

    # --- off-chip traffic: MEASURED --------------------------------------
    dram_bytes = m.bytes_offchip()
    dram_measured = dram_bytes > 0
    if not dram_measured:
        dram_bytes = w.tiles_issued() * w.dim * w.dim + w.K * w.N + w.M * w.N * ACC_BYTES
    e_dram = dram_bytes * ENERGY_PJ_PER_DRAM_BYTE

    total_pj = e_mac + e_sram + e_dram
    power_w = (total_pj * 1e-12) / time_s if time_s > 0 else 0.0

    useful_ops = macs_useful * 2.0            # a MAC is a multiply and an add
    perf_gops = (useful_ops / time_s) / 1e9 if time_s > 0 else 0.0
    perf_per_watt = perf_gops / power_w if power_w > 0 else 0.0

    return EnergyReport(
        energy_pj=total_pj,
        power_w=power_w,
        perf_gops=perf_gops,
        perf_per_watt_gops_w=perf_per_watt,
        energy_per_useful_op_pj=(total_pj / useful_ops) if useful_ops else 0.0,
        mac_efficiency=(macs_useful / macs_issued) if macs_issued else 0.0,
        dram_measured=dram_measured,
        gating_measured=gating_measured,
        breakdown={"mac_pj": e_mac, "sram_pj": e_sram, "dram_pj": e_dram,
                   "mac_pct": 100.0 * e_mac / total_pj if total_pj else 0.0,
                   "sram_pct": 100.0 * e_sram / total_pj if total_pj else 0.0,
                   "dram_pct": 100.0 * e_dram / total_pj if total_pj else 0.0,
                   "macs_issued": macs_issued, "macs_useful": macs_useful,
                   "macs_gated": gated, "macs_gated_opportunity": gated_opportunity, "dram_bytes": dram_bytes,
                   "time_s": time_s},
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
