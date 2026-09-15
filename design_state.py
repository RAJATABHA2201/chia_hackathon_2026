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
    block_size: int = 32
    tile_m: int = 16
    tile_n: int = 16
    tile_k: int = 16

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
    )
    SW_FIELDS = ("block_size", "tile_m", "tile_n", "tile_k")

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
// The four SPARSECRAFT lines below are SOFTWARE-side tiling. They are compiler
// defines, not Chisel parameters, so they live in comments -- but the harness
// parses them back out, so they must be kept and kept well-formed.
// SPARSECRAFT block_size = {self.block_size}
// SPARSECRAFT tile_m = {self.tile_m}
// SPARSECRAFT tile_n = {self.tile_n}
// SPARSECRAFT tile_k = {self.tile_k}
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
    acc_read_full_width       = false,
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
