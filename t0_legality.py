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


# Dense width of X in the generated SpMM workloads (workload/prep_matrices.py --n).
SPMM_N = 64


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


def check(s: DesignState, *, predicted_area_um2: float | None = None,
          pinned: dict | None = None) -> Verdict:
    """Return a Verdict naming every violated constraint.

    ``pinned`` freezes fields that define the QUESTION rather than the design.
    The caller passes the values the run was launched with; any deviation is a
    violation.
    """
    v: List[str] = []
    block_rows = s.meshRows * s.tileRows
    block_cols = s.meshColumns * s.tileColumns

    # --- Family 0: the benchmark is not a design variable -----------------
    # `workload` and `dense_mode` live in SW_FIELDS and are emitted as
    # `// SPARSECRAFT` markers in SparseCraftParams.scala -- which IS the
    # agent's writable file. Sec 9o says they "stay withheld -- an agent that
    # can change the workload or fall back to dense mode wins by changing the
    # question", but nothing enforced it, and loop.py picks the simulated
    # matrix straight off the proposed state:
    #     data_path = .../f"spmm_{child.workload}.h"
    # So a proposal that changed the marker and happened to compile would be
    # SIMULATED ON A DIFFERENT MATRIX and then scored against the baseline,
    # putting points from two benchmarks on one Pareto front. Sec 9m records
    # that every dnn* matrix behaves completely unlike the jagged ones, so the
    # contamination would not even be subtle.
    #
    # Measured, run codesign15b 2026-09-20: the agent went for this on its
    # FIRST move (jag512 -> dnn512) and again two iterations later. Only an
    # unrelated compile error stopped it. Asked to reduce cycles, it reached
    # for an easier benchmark -- which is reward hacking, and exactly the
    # class of thing a gate must catch rather than a prompt discourage.
    for _f, _want in (pinned or {}).items():
        _got = getattr(s, _f, None)
        if _got != _want:
            v.append(f"sparsecraft.frozen_{_f}: {_f} is FIXED at {_want!r} for this "
                     f"run and may not be proposed as {_got!r}. It selects the "
                     f"benchmark, not the hardware: changing it changes the "
                     f"question, so the result would be comparable neither to the "
                     f"baseline nor to any other iteration. Restore the "
                     f"`// SPARSECRAFT {_f} = {_want}` marker line and optimise the "
                     f"design instead.")

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
    # --- SpMM tiling ------------------------------------------------------
    # The kernel walks DIM x DIM blocks, so the tile IS the array dimension:
    # there are no independent T_m/T_n/T_k knobs any more. What must hold is
    # that one A block, one X panel and one Y panel fit the scratchpad and
    # accumulator at the chosen array size.
    working_bytes = (d.dim * d.dim + d.dim * SPMM_N) * INPUT_BYTES * 2
    if working_bytes > s.sp_capacity_kb * 1024:
        v.append(f"capacity.sp: working set {working_bytes}B "
                 f"exceeds sp_capacity {s.sp_capacity_kb}KB")
    acc_needed = d.dim * SPMM_N * ACC_BYTES
    if acc_needed > s.acc_capacity_kb * 1024:
        v.append(f"capacity.acc: {acc_needed}B exceeds acc_capacity "
                 f"{s.acc_capacity_kb}KB")
    if d.acc_rows < d.dim:
        v.append(f"capacity.acc_rows: acc_rows {d.acc_rows} < dim {d.dim}")

    # --- L0 RTL sparsity microarchitecture --------------------------------
    if s.granule_size <= 0:
        v.append(f"zbu.granule_positive: granule_size {s.granule_size} must be > 0")
    elif d.dim % s.granule_size != 0:
        v.append(f"zbu.granule_divides_dim: granule_size {s.granule_size} "
                 f"does not divide array dimension {d.dim}")
    if s.zbu_operand not in ("A", "B", "BOTH"):
        v.append(f"zbu.operand: {s.zbu_operand!r} not in (A, B, BOTH)")
    if s.zbu_enable:
        # One bit per granule per scratchpad row. Must not eat the scratchpad
        # it is meant to make cheaper.
        bitmap_bits = d.sp_rows * (d.dim // max(1, s.granule_size))
        if bitmap_bits > s.sp_capacity_kb * 1024 * 8 * 0.05:
            v.append(f"zbu.bitmap_budget: bitmap {bitmap_bits} bits exceeds 5% of "
                     f"scratchpad ({s.sp_capacity_kb}KB) -- granule too fine")

    # --- L0 SOFTWARE SCHEDULE, and its coupling to the hardware -----------
    # This is the HW/SW co-design constraint, enforced rather than trusted.
    # One accumulator-resident pass stages, in the scratchpad:
    #     A:  k_chunk * dim              rows
    #     B:  k_chunk * (N/dim) * dim    rows
    # so the SOFTWARE chunk depth is bounded by the HARDWARE scratchpad size.
    # Raising k_chunk without raising sp_capacity_kb is illegal, and the agent
    # has to move both together -- which is the whole point of co-design.
    if s.k_chunk < 1:
        v.append(f"sched.k_chunk_positive: k_chunk {s.k_chunk} must be >= 1")
    else:
        j_tiles = max(1, SPMM_N // d.dim)
        need_rows = s.k_chunk * d.dim * (1 + j_tiles)
        if need_rows > d.sp_rows:
            v.append(f"sched.k_chunk_fits_scratchpad: k_chunk {s.k_chunk} needs "
                     f"{need_rows} scratchpad rows (A {s.k_chunk * d.dim} + "
                     f"B {s.k_chunk * d.dim * j_tiles}) but sp_capacity_kb "
                     f"{s.sp_capacity_kb} gives only {d.sp_rows} -- raise the "
                     f"scratchpad or lower k_chunk")

    # gemmini moves at most MAX_BLOCK_LEN = dma_maxbytes/dim tiles per mvin, so
    # the SOFTWARE mvin width is bounded by the HARDWARE DMA width. Second
    # coupling, same idea.
    max_block_len = max(1, s.dma_maxbytes // d.dim)
    if s.b_blocks < 0:
        v.append(f"sched.b_blocks_nonneg: b_blocks {s.b_blocks} must be >= 0")
    elif s.b_blocks > max_block_len:
        v.append(f"sched.b_blocks_dma: b_blocks {s.b_blocks} exceeds "
                 f"MAX_BLOCK_LEN {max_block_len} implied by dma_maxbytes "
                 f"{s.dma_maxbytes} at dim {d.dim}")

    # A batching would read consecutive blocks as one (dim x blocks*dim)
    # matrix at row stride dim, which is NOT the [block][row][col] layout
    # prep_matrices.py emits. Correct only at 1 until that layout changes.
    # Rejected here rather than left to produce silently wrong data.
    if s.a_blocks != 1:
        v.append(f"sched.a_blocks_layout: a_blocks {s.a_blocks} != 1 is "
                 f"incompatible with the [block][row][col] A layout")

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
from constants import PARAMS_FILE_REL, RTL_FILES_REL  # noqa: E402

# The agent's writable set. Phase 3 widens this from one config file to the
# RTL it is meant to author.
#
#   PARAMS_FILE_REL          the typed config point (SparseCraftParams.scala)
#   PE.scala                 T-A lives here: 147 lines, one module, and the
#                            change is provably bit-exact
#   SparseCraftSparsity.scala  T-B's ZBU -- the agent writes and rewrites it
#
# DELIBERATELY NOT INCLUDED, and this is the D1 decision:
#   SparseCraftRTL.scala     harness-generated from the design state every
#                            iteration. The agent changes the MECHANISM; the
#                            harness sets the knobs. Letting the agent write
#                            it would let it flip gate_enable without the
#                            design state -- and therefore the cache key,
#                            the archive descriptor and T0 -- ever knowing.
#   Scratchpad / ExecuteController / CounterFile
#                            the three ZBU integration hooks. Integrating into
#                            a 1037-line controller is where an LLM silently
#                            breaks the pipeline; the interesting design space
#                            is INSIDE the ZBU module, which the agent owns.
WRITABLE_PATHS = frozenset({PARAMS_FILE_REL} | set(RTL_FILES_REL))


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
