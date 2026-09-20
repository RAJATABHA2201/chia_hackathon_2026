"""The SparseCraft design state: a typed delta over Gemmini's leanConfig.

The state is deliberately a *typed parameter vector*, not free-form Chisel. Every
field maps onto a real ``GemminiArrayConfig`` parameter
(``generators/gemmini/src/main/scala/gemmini/GemminiConfigs.scala``), which is what
makes T0 legality checkable in microseconds and mutations auditable.

The only artifact this module emits into the Chipyard tree is
``SparseCraftParams.scala`` -- one file, one object, no logic. That is the whole
writable surface of the loop (review L-inf).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, fields, replace

# Values that must be emitted as bare Scala expressions, not quoted strings.
_SCALA_ENUMS = {
    "dataflow": {"WS": "Dataflow.WS", "OS": "Dataflow.OS", "BOTH": "Dataflow.BOTH"},
}


@dataclass(frozen=True)
class DesignState:
    """One point in the SparseCraft search space.

    Defaults reproduce Gemmini's ``leanConfig`` (WS dataflow, 64 in-flight
    requests) with normalizations enabled -- the hardware softmax path
    (NormCmd.MAX / SUM_EXP / INV_SUM_EXP + I-BERT iexp) that prefill attention
    needs, and that is off by default upstream.
    """

    # --- L2 PE array geometry and precision ---
    meshRows: int = 16
    meshColumns: int = 16
    tileRows: int = 1
    tileColumns: int = 1
    dataflow: str = "WS"

    # --- L3 scratchpad / accumulator ---
    sp_capacity_kb: int = 256
    acc_capacity_kb: int = 64
    sp_banks: int = 4
    acc_banks: int = 2
    spad_read_delay: int = 4
    acc_latency: int = 2

    # --- L5 address generation / DMA ---
    max_in_flight_mem_reqs: int = 64
    dma_maxbytes: int = 64
    dma_buswidth: int = 128
    tlb_size: int = 4

    # --- L7 RoCC / queueing surface ---
    ld_queue_length: int = 8
    st_queue_length: int = 2
    ex_queue_length: int = 8
    reservation_station_entries_ld: int = 8
    reservation_station_entries_st: int = 4
    reservation_station_entries_ex: int = 16

    # --- L10 numerics / nonlinear ---
    has_normalizations: bool = True
    mvin_scale_shared: bool = False

    # --- profiling ---
    num_counter: int = 8

    # --- software-side tiling (drives the kernel, not the RTL) ---
    # Separated out so a tiling-only mutation can skip elaboration entirely.
    # --- L0 RTL sparsity microarchitecture --------------------------------
    # These are NOT config knobs on stock Gemmini: they parameterise the RTL
    # the loop adds. They live in the design state so T0 can check them, the
    # cache can key on them, and the non-agentic control arms can search the
    # same axes the agent does (plan D6).
    #
    #   gate_enable    T-A: zero-operand gating in the PE (PE.scala).
    #                  Energy only -- a gated PE still occupies its pipeline
    #                  slot, so cycles are unchanged by construction.
    #   zbu_enable     T-B: zero-granule skip (SparseCraftSparsity.scala).
    #   granule_size   detection granularity in elements. MUST divide the
    #                  array dimension. 1 = per element, DIM = whole row.
    #   zbu_operand    which operand the bitmap is built over.
    gate_enable: bool = False
    zbu_enable: bool = False
    granule_size: int = 16
    zbu_operand: str = "A"          # "A" | "B" | "BOTH"

    # --- software-side tiling (drives the kernel, not the RTL) ------------
    # dense_mode selects the B0 baseline: walk every block including the
    # structurally zero ones, i.e. a GEMM that ignores sparsity entirely.
    workload: str = "dnn512"        # which generated header the kernel builds against
    dense_mode: bool = False

    # ---- SOFTWARE SCHEDULE (the SW half of HW/SW co-design) ---------------
    # Compile-time knobs on the kernel's Gemmini instruction schedule. They
    # change HOW the work is issued, never WHAT is computed: the golden check,
    # the timed region and the block walk are untouched, so N41 still gates
    # correctness and no setting can improve a score by computing less.
    #
    # These exist because hand-tuning exactly this layer produced the largest
    # result in the project so far (accumulator-resident scheduling: 2.00x
    # energy, 4.51x cycles). Leaving it outside the loop meant the agent could
    # not reach the lever that mattered most.
    #
    # k_chunk couples DIRECTLY to sp_capacity_kb, which is a HARDWARE lever:
    # one pass stages k_chunk*DIM rows of A and k_chunk*(N/DIM)*DIM rows of B,
    # so a bigger chunk needs a bigger scratchpad. T0 rejects the combinations
    # that do not fit. That coupling is the co-design.
    k_chunk: int = 16               # K-blocks accumulated per resident pass
    # X-RESIDENT SCRATCHPAD. Couples to sp_capacity_kb exactly as k_chunk does:
    # all of X is SPMM_KB*(N/DIM)*DIM scratchpad rows (2,048 of 16,384 at
    # 256 KB), so it only fits if the hardware provides the capacity. Measured
    # motivation: X was being re-fetched from DRAM once per nonzero block, 117
    # times, for 8.6x the ideal read traffic.
    x_resident: bool = False
    b_blocks: int = 0               # B mvin width in DIM-column tiles; 0 = auto
    a_blocks: int = 1               # A mvin width in DIM-column tiles

    # ---------------------------------------------------------------- keys --
    # Fields that change the elaborated hardware. Everything not listed here is
    # software-only, so a mutation touching only those reuses the cached RTL.
    HW_FIELDS = (
        "meshRows", "meshColumns", "tileRows", "tileColumns", "dataflow",
        "sp_capacity_kb", "acc_capacity_kb", "sp_banks", "acc_banks",
        "spad_read_delay", "acc_latency",
        "max_in_flight_mem_reqs", "dma_maxbytes", "dma_buswidth", "tlb_size",
        "ld_queue_length", "st_queue_length", "ex_queue_length",
        "reservation_station_entries_ld", "reservation_station_entries_st",
        "reservation_station_entries_ex",
        "has_normalizations", "mvin_scale_shared", "num_counter",
        # The RTL sparsity parameters change the generated Verilog, so they
        # belong to the elaboration cache key, not the software one.
        "gate_enable", "zbu_enable", "granule_size", "zbu_operand",
    )
    SW_FIELDS = ("workload", "dense_mode", "k_chunk", "b_blocks", "a_blocks",
                 "x_resident")

    # --------------------------------------------------------------- derived -
    @property
    def pe_count(self) -> int:
        return (self.meshRows * self.tileRows) * (self.meshColumns * self.tileColumns)

    @property
    def reduction_width(self) -> int:
        """Spatial-array reduction width -- the axis N:M groups must divide."""
        return self.meshRows * self.tileRows

    # ----------------------------------------------------------------- hash -
    def canonical(self) -> dict:
        """Field-ordered dict; the basis of every cache key and trace record."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def _hash_of(self, names) -> str:
        blob = json.dumps({n: getattr(self, n) for n in sorted(names)},
                          sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def state_hash(self) -> str:
        """Full state identity -- the N21 dedup key."""
        return self._hash_of([f.name for f in fields(self)])

    def hw_hash(self) -> str:
        """Elaboration cache key (N30/N31)."""
        return self._hash_of(self.HW_FIELDS)

    def sw_hash(self) -> str:
        """Software build cache key (N32).

        Carries the hardware hash on purpose: elaboration emits
        ``gemmini_params.h``, which the kernels include, so a hardware change
        invalidates the software build even when no kernel source changed.
        """
        blob = json.dumps({"sw": {n: getattr(self, n) for n in sorted(self.SW_FIELDS)},
                           "hw": self.hw_hash()}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # ------------------------------------------------------------- mutation -
    def mutate(self, **kw) -> "DesignState":
        """Return a new state with the named fields replaced."""
        unknown = set(kw) - {f.name for f in fields(self)}
        if unknown:
            raise KeyError(f"unknown design-state field(s): {sorted(unknown)}")
        return replace(self, **kw)

    def diff_from(self, other: "DesignState") -> dict:
        """Fields where self differs from other, as {name: (other, self)}."""
        return {f.name: (getattr(other, f.name), getattr(self, f.name))
                for f in fields(self)
                if getattr(other, f.name) != getattr(self, f.name)}

    # --------------------------------------------------------- Scala emitter -
    def to_scala(self) -> str:
        """Emit SparseCraftParams.scala -- the loop's entire writable surface.

        Built on ``GemminiConfigs.defaultConfig`` with every searched knob stated
        explicitly (rather than on leanConfig with hidden deltas) so the file
        reads as the complete design point.
        """
        df = _SCALA_ENUMS["dataflow"][self.dataflow]
        b = lambda v: "true" if v else "false"   # noqa: E731
        return f"""// SparseCraft design point.
// hash: {self.state_hash()}   hw: {self.hw_hash()}   sw: {self.sw_hash()}
//
// The SPARSECRAFT lines below are SOFTWARE-side: tiling and the
// sparsity pattern. They are compiler
// defines, not Chisel parameters, so they live in comments -- but the harness
// parses them back out, so they must be kept and kept well-formed.
// SPARSECRAFT workload = {self.workload}
// SPARSECRAFT dense_mode = {int(self.dense_mode)}
// The Gemmini instruction schedule (Sec 9o's software co-design levers). They
// reach the build as -DSPMM_KCHUNK / -DSPMM_B_BLOCKS (nodes.py), but they were
// NOT emitted here, so nothing wrote them into the tree and read_design_state
// could not read them back: every proposal silently reverted to the defaults
// the moment the state was re-parsed. Measured 2026-09-20 -- a run launched
// with k_chunk=64 reported `[SW] k_chunk 64 -> 16` and reused the baseline's
// sw_hash, i.e. the whole software half of co-design was inert, for the agent
// and the CLI alike.
// SPARSECRAFT k_chunk = {self.k_chunk}
// SPARSECRAFT b_blocks = {self.b_blocks}
// SPARSECRAFT x_resident = {int(self.x_resident)}
// The four below are RTL-microarchitecture parameters. They are markers, NOT
// GemminiArrayConfig fields, until Phase 3 adds the corresponding Chisel
// parameters -- emitting them as `.copy(gate_enable = ...)` before the field
// exists makes elaboration fail with a Scala type error.
// SPARSECRAFT gate_enable = {int(self.gate_enable)}
// SPARSECRAFT zbu_enable = {int(self.zbu_enable)}
// SPARSECRAFT granule_size = {self.granule_size}
// SPARSECRAFT zbu_operand = {self.zbu_operand}
package gemmini

import chisel3._

object SparseCraftParams {{
  val config = GemminiConfigs.defaultConfig.copy(
    // --- L2 PE array geometry ---
    meshRows    = {self.meshRows},
    meshColumns = {self.meshColumns},
    tileRows    = {self.tileRows},
    tileColumns = {self.tileColumns},
    dataflow    = {df},

    // --- L3 scratchpad / accumulator ---
    sp_capacity     = CapacityInKilobytes({self.sp_capacity_kb}),
    acc_capacity    = CapacityInKilobytes({self.acc_capacity_kb}),
    sp_banks        = {self.sp_banks},
    acc_banks       = {self.acc_banks},
    spad_read_delay = {self.spad_read_delay},
    acc_latency     = {self.acc_latency},

    // --- L5 address generation / DMA ---
    max_in_flight_mem_reqs = {self.max_in_flight_mem_reqs},
    dma_maxbytes           = {self.dma_maxbytes},
    dma_buswidth           = {self.dma_buswidth},
    tlb_size               = {self.tlb_size},

    // --- L7 RoCC / queueing surface ---
    ld_queue_length = {self.ld_queue_length},
    st_queue_length = {self.st_queue_length},
    ex_queue_length = {self.ex_queue_length},
    reservation_station_entries_ld = {self.reservation_station_entries_ld},
    reservation_station_entries_st = {self.reservation_station_entries_st},
    reservation_station_entries_ex = {self.reservation_station_entries_ex},

    // --- L10 numerics / nonlinear (hardware softmax path) ---
    has_normalizations = {b(self.has_normalizations)},
    mvin_scale_shared  = {b(self.mvin_scale_shared)},

    // --- profiling ---
    num_counter = {self.num_counter},

    // --- leanConfig deltas, kept explicit ---
    // TRUE, not false. The SpMM golden reference ranges past +-400, and a
    // narrowed accumulator read clips at elem_t (+-127), so the equivalence
    // gate would fail a CORRECT design -- the worst kind of gate failure,
    // because it looks like the agent broke correctness. The kernel passes
    // full_C=true and reads acc_t out, which requires this.
    acc_read_full_width       = true,
    ex_read_from_acc          = false,
    ex_write_to_spad          = false,
    hardcode_d_to_garbage_addr = true,
  )
}}
"""

    def to_json(self) -> str:
        return json.dumps(self.canonical(), sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "DesignState":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


    # --------------------------------------------------- RTL param emitter -
    def to_rtl_scala(self) -> str:
        """Emit SparseCraftRTL.scala -- the RTL microarchitecture parameters.

        These are deliberately NOT GemminiArrayConfig fields. Adding them to
        that case class would mean plumbing them through Mesh -> Tile -> PE,
        touching four upstream files for no benefit: PE.scala can read a Scala
        `val` directly, and because it is elaboration-time constant, Chisel
        specialises the generated Verilog exactly as it would for a parameter.
        A `false` here emits no gating hardware at all, not a disabled mux.
        """
        b = lambda v: "true" if v else "false"   # noqa: E731
        return f"""// GENERATED BY THE SPARSECRAFT LOOP -- do not hand-edit.
// hash: {self.state_hash()}   hw: {self.hw_hash()}
package gemmini

/** RTL microarchitecture parameters for the SparseCraft sparsity extensions.
  *
  * Elaboration-time constants. Each `false`/`0` must elaborate to EXACTLY the
  * stock Gemmini netlist -- that is the property the baseline depends on, and
  * it is what makes an A/B against vanilla hardware meaningful.
  */
object SparseCraftRTL {{
  /** T-A: gate the MAC when an operand is zero. Energy only; a gated PE still
    * occupies its pipeline slot, so cycles are unchanged by construction. */
  val gateEnable: Boolean = {b(self.gate_enable)}

  /** T-B: zero-granule skip via the ZBU. */
  val zbuEnable: Boolean = {b(self.zbu_enable)}

  /** Zero-detection granularity, in elements. Must divide the array
    * dimension; T0 rejects any state where it does not. */
  val granuleSize: Int = {self.granule_size}

  /** Which operand the bitmap is built over: "A", "B" or "BOTH". */
  val zbuOperand: String = "{self.zbu_operand}"
}}
"""


# The Chipyard-side harness config. Written once at setup and never mutated --
# keeping it out of the writable set is what makes the N13 allowlist a one-liner.
HARNESS_SCALA = """// GENERATED BY THE SPARSECRAFT CHIA LOOP -- written once, never mutated.
package chipyard

import org.chipsalliance.cde.config.Config

class SparseCraftConfig extends Config(
  new gemmini.LeanGemminiConfig(gemmini.SparseCraftParams.config) ++
  new freechips.rocketchip.rocket.WithNHugeCores(1) ++
  new chipyard.config.WithSystemBusWidth(128) ++
  new chipyard.config.AbstractConfig)
"""

BASELINE = DesignState()
