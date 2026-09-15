"""N20 -- the T0 legality validator.

Two rule families, both pure functions of the design state, both microseconds:

1. **Gemmini elaboration invariants.** Transcribed from the actual ``require()``
   calls in ``GemminiConfigs.scala`` (pinned commit 8c3f992). Every one of these
   would otherwise surface as a 20-40 minute failed elaboration. This is the
   review's point in Sec 2.3: the agent should learn the feasible region by
   *name*, in microseconds, not by burning a build.

2. **Review Sec 2.3 rules** -- tiling divisibility, working-set fit, bank count
   vs. concurrent gather streams, Little's Law, and the no-materialised-S
   fusion requirement.

A violation returns the constraint **by name**, which is what gets fed back so
the agent learns the boundary rather than guessing at it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from design_state import DesignState

# Datatype widths of GemminiConfigs.defaultConfig, in bits. These are not yet
# part of the search space; when they become mutable, derive them from the state.
INPUT_W = 8
ACC_W = 32
INPUT_BYTES = INPUT_W // 8
ACC_BYTES = ACC_W // 8

# Memory-system constants for Little's Law. dma_buswidth is in bits.
MEM_LATENCY_CYCLES = 100
# Concurrent gather streams the scratchpad must serve: A, B and D operands.
CONCURRENT_GATHER_STREAMS = 3

AREA_BUDGET_UM2 = 4.0e6


@dataclass
class Verdict:
    legal: bool
    violations: List[str]

    def __bool__(self) -> bool:
        return self.legal

    def report(self) -> str:
        if self.legal:
            return "T0 PASS"
        return "T0 FAIL:\n  - " + "\n  - ".join(self.violations)


def _is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


# --------------------------------------------------------------------------
# Derived Gemmini quantities. Mirrors GemminiConfigs.scala:106-113,185-196.
# --------------------------------------------------------------------------
@dataclass
class Derived:
    dim: int
    sp_width: int
    sp_bank_entries: int
    acc_bank_entries: int
    sp_rows: int
    acc_rows: int
    usable_sp_tiles: int
    total_acc_tiles: int


def derive(s: DesignState) -> Derived:
    block_rows = s.meshRows * s.tileRows
    block_cols = s.meshColumns * s.tileColumns
    dim = block_rows
    sp_width = block_cols * INPUT_W
    # Integer division, matching Scala's Int arithmetic.
    sp_bank_entries = (s.sp_capacity_kb * 1024 * 8) // (s.sp_banks * sp_width) if sp_width else 0
    acc_den = s.acc_banks * block_cols * ACC_W
    acc_bank_entries = (s.acc_capacity_kb * 1024 * 8) // acc_den if acc_den else 0
    sp_rows = s.sp_banks * sp_bank_entries
    acc_rows = s.acc_banks * acc_bank_entries
    return Derived(
        dim=dim, sp_width=sp_width,
        sp_bank_entries=sp_bank_entries, acc_bank_entries=acc_bank_entries,
        sp_rows=sp_rows, acc_rows=acc_rows,
        usable_sp_tiles=(sp_rows // dim) - 2 if dim else 0,
        total_acc_tiles=(acc_rows // dim) if dim else 0,
    )


def check(s: DesignState, *, predicted_area_um2: float | None = None) -> Verdict:
    """Return a Verdict naming every violated constraint."""
    v: List[str] = []
    block_rows = s.meshRows * s.tileRows
    block_cols = s.meshColumns * s.tileColumns

    # --- Family 1: Gemmini elaboration invariants -------------------------
    if block_rows != block_cols:
        v.append(f"gemmini.square_array: meshRows*tileRows ({block_rows}) != "
                 f"meshColumns*tileColumns ({block_cols})")
    if block_cols < 2:
        v.append(f"gemmini.min_dim: systolic array dimension {block_cols} < 2")
    if not _is_pow2(block_cols):
        v.append(f"gemmini.pow2_dim: systolic array dimension {block_cols} is not a power of 2")
    if s.num_counter >= 256:
        v.append(f"gemmini.num_counter: {s.num_counter} must be < 256")

    d = derive(s)
    if d.sp_bank_entries <= 0:
        v.append(f"gemmini.sp_bank_entries_positive: sp_capacity {s.sp_capacity_kb} KB over "
                 f"{s.sp_banks} banks of width {d.sp_width} b yields {d.sp_bank_entries} rows")
    else:
        if not _is_pow2(d.sp_bank_entries):
            v.append(f"gemmini.sp_bank_entries_pow2: {d.sp_bank_entries} rows/bank is not a power of 2")
        if block_rows and d.sp_bank_entries % block_rows != 0:
            v.append(f"gemmini.sp_bank_entries_multiple_of_dim: {d.sp_bank_entries} rows/bank "
                     f"not a multiple of array dim {block_rows}")

    if d.acc_bank_entries <= 0:
        v.append(f"gemmini.acc_bank_entries_positive: acc_capacity {s.acc_capacity_kb} KB over "
                 f"{s.acc_banks} banks yields {d.acc_bank_entries} rows")
    elif block_rows and d.acc_bank_entries % block_rows != 0:
        v.append(f"gemmini.acc_bank_entries_multiple_of_dim: {d.acc_bank_entries} rows/bank "
                 f"not a multiple of array dim {block_rows}")

    # assert(USABLE_SP_TILES >= TOTAL_ACC_TILES) -- GemminiConfigs.scala:~262
    if d.usable_sp_tiles < d.total_acc_tiles:
        v.append(f"gemmini.sp_tiles_ge_acc_tiles: usable SP tiles {d.usable_sp_tiles} < "
                 f"accumulator tiles {d.total_acc_tiles}")

    # mvin_scale_shared demands inputType.getWidth == accType.getWidth, which is
    # 8 vs 32 in defaultConfig -- so it is unconditionally illegal here. Without
    # this rule every proposal setting it burns a full elaboration.
    if s.mvin_scale_shared and INPUT_W != ACC_W:
        v.append(f"gemmini.mvin_scale_shared: requires inputType width == accType width "
                 f"({INPUT_W} != {ACC_W})")

    # --- Family 2: review Sec 2.3 rules -----------------------------------
    if s.tile_n <= 0 or s.tile_m <= 0 or s.tile_k <= 0:
        v.append(f"tiling.positive: (T_m,T_n,T_k)=({s.tile_m},{s.tile_n},{s.tile_k}) must be positive")
    else:
        if s.block_size % s.tile_n != 0:
            v.append(f"tiling.block_div_tn: B ({s.block_size}) mod T_n ({s.tile_n}) != 0")
        if s.block_size % s.tile_m != 0:
            v.append(f"tiling.block_div_tm: B ({s.block_size}) mod T_m ({s.tile_m}) != 0")

        # Working set must fit the scratchpad. Double-buffered => x2.
        working_bytes = (s.tile_m * s.tile_k + s.tile_k * s.tile_n
                         + s.tile_m * s.tile_n) * INPUT_BYTES * 2
        sp_bytes = s.sp_capacity_kb * 1024
        if working_bytes > sp_bytes:
            v.append(f"capacity.working_set: {working_bytes} B double-buffered working set "
                     f"exceeds sp_capacity {sp_bytes} B")

        acc_needed = s.tile_m * s.tile_n * ACC_BYTES
        if acc_needed > s.acc_capacity_kb * 1024:
            v.append(f"capacity.acc: T_m*T_n*acc_bytes = {acc_needed} B exceeds "
                     f"acc_capacity {s.acc_capacity_kb * 1024} B")
        if d.acc_rows < s.tile_m:
            v.append(f"capacity.acc_rows: acc_rows {d.acc_rows} < T_m {s.tile_m}")

    if s.sp_banks < CONCURRENT_GATHER_STREAMS:
        v.append(f"banking.gather_streams: sp_banks {s.sp_banks} < "
                 f"{CONCURRENT_GATHER_STREAMS} concurrent gather streams (A,B,D)")

    # Little's Law: you cannot sustain BW at latency L without BW*L in flight.
    bw_bytes_per_cycle = s.dma_buswidth // 8
    required_in_flight = bw_bytes_per_cycle * MEM_LATENCY_CYCLES
    actual_in_flight = s.max_in_flight_mem_reqs * s.dma_maxbytes
    if actual_in_flight < required_in_flight:
        v.append(f"memory.littles_law: {actual_in_flight} B in flight "
                 f"(max_in_flight_mem_reqs {s.max_in_flight_mem_reqs} x dma_maxbytes "
                 f"{s.dma_maxbytes}) < BW*latency {required_in_flight} B "
                 f"({bw_bytes_per_cycle} B/cyc x {MEM_LATENCY_CYCLES} cyc)")

    # Prefill fusion: S must never be materialised, which needs the online-softmax
    # path in hardware. Without normalizations the kernel has to round-trip
    # through the core and materialise scores.
    if not s.has_normalizations:
        v.append("fusion.no_materialised_S: has_normalizations=false leaves no hardware "
                 "softmax (NormCmd.MAX/SUM_EXP/INV_SUM_EXP), so S would have to be materialised")

    if predicted_area_um2 is not None and predicted_area_um2 > AREA_BUDGET_UM2:
        v.append(f"area.budget: predicted area {predicted_area_um2:.3e} um2 exceeds "
                 f"budget {AREA_BUDGET_UM2:.3e} um2")

    return Verdict(legal=not v, violations=v)


# --------------------------------------------------------------------------
# N13 -- patch scope allowlist. Enforced programmatically before git apply,
# never by prompt instruction (review L-inf).
# --------------------------------------------------------------------------
from constants import PARAMS_FILE_REL  # noqa: E402

WRITABLE_PATHS = frozenset({PARAMS_FILE_REL})


def check_patch_scope(paths, harness_paths=()) -> Verdict:
    """Reject any patch touching a path outside the writable set.

    ``harness_paths`` are files the HARNESS itself wrote this run -- the
    one-time SparseCraftConfigs.scala scaffolding, which has to exist for the
    config to elaborate but is deliberately NOT in the model's writable set.
    They are passed in by the caller that wrote them rather than being baked
    into WRITABLE_PATHS, so the model's write authority is still exactly one
    file: a path is only tolerated here because the harness put it there on
    this run, never because it is named in a constant the model could aim at.
    """
    allowed = set(WRITABLE_PATHS) | set(harness_paths)
    out = []
    for p in paths:
        if p in allowed:
            continue
        out.append(f"scope.denied: {p} is outside the writable set "
                   f"{sorted(WRITABLE_PATHS)}")
    return Verdict(legal=not out, violations=out)
