"""Parse Gemmini counters and cycle counts out of a VerilatorRunNode RunResult.

``RunResult.log`` is the simulator's HTIF stdout -- whatever the baremetal kernel
printf'd. So the kernel is the measurement instrument: it reads Gemmini's counter
file and ``rdcycle()`` and prints a machine-parseable block, which this module
turns back into a metric vector.

Counter names are the review's "counter the agent must observe" column. Most of
them already exist upstream in ``CounterFile.scala`` (45 CounterEvents + 8
CounterExternals); ``num_counter`` caps how many can be sampled at once, which is
why the kernel emits them in named passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

# The kernel prints one "SPARSECRAFT key=value" line per metric, so parsing is
# exact and order-independent. Anything else on stdout is ignored.
_LINE = re.compile(r"^\s*SPARSECRAFT\s+([A-Za-z0-9_]+)\s*=\s*(-?\d+)\s*$", re.M)

# Counters the loop requires on day 1. A lever whose counter is missing here is
# unsearchable (review Sec 1.4), so this list is asserted, not hoped for.
REQUIRED = (
    "cycles",
    "macs_useful",
    "EXE_ACTIVE_CYCLE",
    "LOAD_DMA_WAIT_CYCLE",
    "RDMA_BYTES_REC",
    "WDMA_BYTES_SENT",
)

OPTIONAL = (
    # Scratchpad row reads the ZBU suppressed. OPTIONAL rather than REQUIRED
    # because it only exists when zbuEnable is true -- with T-B off the whole
    # counter is elaborated away, and demanding it would fail every baseline.
    "ZBU_SKIPPED_ROWS",
    "MAC_GATED_TOTAL", "macs_issued", "tiles_issued", "equiv_mismatches",
    "MAIN_LD_CYCLES", "MAIN_ST_CYCLES", "MAIN_EX_CYCLES",
    "LOAD_SCRATCHPAD_WAIT_CYCLE", "STORE_SCRATCHPAD_WAIT_CYCLE",
    "SCRATCHPAD_A_WAIT_CYCLE", "SCRATCHPAD_B_WAIT_CYCLE", "SCRATCHPAD_D_WAIT_CYCLE",
    "ACC_A_WAIT_CYCLE", "ACC_B_WAIT_CYCLE", "ACC_D_WAIT_CYCLE",
    "EXE_FLUSH_CYCLE", "EXE_CONTROL_Q_BLOCK_CYCLE",
    "EXE_PRELOAD_HAZ_CYCLE", "EXE_OVERLAP_HAZ_CYCLE",
    "RESERVATION_STATION_FULL_CYCLES", "RESERVATION_STATION_ACTIVE_CYCLES",
    "DMA_TLB_MISS_CYCLE", "DMA_TLB_TOTAL_REQ",
    "RDMA_ACTIVE_CYCLE", "RDMA_TL_WAIT_CYCLES",
    "WDMA_ACTIVE_CYCLE", "WDMA_TL_WAIT_CYCLES",
    "RDMA_TOTAL_LATENCY", "WDMA_TOTAL_LATENCY",
)


class MetricsError(RuntimeError):
    pass


@dataclass
class Metrics:
    cycles: int
    macs_useful: int
    counters: dict = field(default_factory=dict)

    # ---- derived diagnostics. Never objectives -- see review Sec 3.3 Step 2:
    # rewarding utilisation invites shrinking the array to raise it.
    def mac_utilisation(self, pe_count: int) -> float:
        denom = pe_count * self.cycles
        return self.macs_useful / denom if denom else 0.0

    def bytes_offchip(self) -> int:
        return (self.counters.get("RDMA_BYTES_REC", 0)
                + self.counters.get("WDMA_BYTES_SENT", 0))

    def dma_wait_fraction(self) -> float:
        return self.counters.get("LOAD_DMA_WAIT_CYCLE", 0) / self.cycles if self.cycles else 0.0

    def conflict_stall_fraction(self) -> float:
        stalls = sum(self.counters.get(k, 0) for k in (
            "SCRATCHPAD_A_WAIT_CYCLE", "SCRATCHPAD_B_WAIT_CYCLE",
            "SCRATCHPAD_D_WAIT_CYCLE", "ACC_A_WAIT_CYCLE",
            "ACC_B_WAIT_CYCLE", "ACC_D_WAIT_CYCLE"))
        return stalls / self.cycles if self.cycles else 0.0

    def exe_active_fraction(self) -> float:
        return self.counters.get("EXE_ACTIVE_CYCLE", 0) / self.cycles if self.cycles else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["derived"] = {
            "bytes_offchip": self.bytes_offchip(),
            "dma_wait_fraction": round(self.dma_wait_fraction(), 4),
            "conflict_stall_fraction": round(self.conflict_stall_fraction(), 4),
            "exe_active_fraction": round(self.exe_active_fraction(), 4),
        }
        return d


def parse(log: str, *, require: bool = True) -> Metrics:
    """Turn a RunResult.log into a Metrics vector.

    Raises MetricsError when a REQUIRED counter is missing -- a silently absent
    counter would otherwise become a zero and read as a spectacular result.
    """
    found = {k: int(v) for k, v in _LINE.findall(log or "")}
    if require:
        missing = [k for k in REQUIRED if k not in found]
        if missing:
            raise MetricsError(
                f"missing required counters {missing}. The kernel must print "
                f"'SPARSECRAFT <name>=<int>' for each. Got: {sorted(found)}")
    cycles = found.pop("cycles", 0)
    macs = found.pop("macs_useful", 0)
    if require and cycles <= 0:
        raise MetricsError(f"non-positive cycle count ({cycles}); the run did not measure anything")
    return Metrics(cycles=cycles, macs_useful=macs, counters=found)


def tripwire_ok(m: Metrics, min_input_bytes: int) -> bool:
    """Information-theoretic tripwire (review Sec 2.5, Gap 3).

    A design whose off-chip byte count falls below what is needed to read the
    inputs once cannot have computed the answer -- it memorised or short-circuited.
    Cheap, decisive, and catches a whole class of cheats.
    """
    return m.bytes_offchip() >= min_input_bytes
