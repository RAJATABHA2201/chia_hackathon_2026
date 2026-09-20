#!/usr/bin/env python3
"""The co-design ledger: every design point, its hypothesis, and what it measured.

Generated FROM the run records (runs/<name>/iter_001.json), not from anyone's
recollection, so the table cannot drift from what was actually measured. Each
row names who proposed the change, because that distinction is the difference
between an agent result and a hand-engineered one and the paper has to be able
to state it correctly.

    python codesign_ledger.py            # markdown table to stdout
"""
from __future__ import annotations
import json
import os
import sys

RUNS = "/home/chia-sparsecraft/runs"

# name -> (proposer, layer, hypothesis). The hypothesis is recorded BEFORE the
# result is known; that is what makes a flat or negative outcome informative
# rather than something to be explained away afterwards.
POINTS = [
    ("zbu-refactor-check", "—",      "—",     "B1 baseline: sparse kernel, stock RTL"),
    ("cd-k64b",            "Claude", "SW",    "k_chunk 16->64: more K per accumulator-resident pass should cut Y round-trips"),
    ("cd-b4",              "Claude", "SW",    "b_blocks 0->4: wider B mvin should cut DMA transactions"),
    ("tb-counter",         "Claude", "HW",    "T-B/ZBU on: skip all-zero scratchpad rows to cut SRAM reads"),
    ("cd-i01-spbanks8",    "Claude", "HW",    "sp_banks 4->8: array idle 60% with dma_wait 3% => on-chip bank contention"),
    ("cd-i02-readdelay2",  "Claude", "HW",    "spad_read_delay 4->2: read latency sits on the stall's critical path"),
    ("cd-i03-dma128-b8",   "Claude", "HW+SW", "dma_maxbytes 64->128 AND b_blocks->8: T0 rejects b_blocks=8 at 64, so the software lever needs the hardware one"),
    ("cd-i04-inflight128", "Claude", "HW",    "max_in_flight_mem_reqs 64->128: attack DMA latency rather than bytes"),
    ("cd-i05-accbanks4",   "Claude", "HW",    "acc_banks 2->4: the accumulator is the other contended SRAM"),
    ("cd-i06-queues16",    "Claude", "HW",    "ex/ld_queue_length ->16: let the controller run further ahead"),
]

BASE = dict(cycles=26946, energy=20.463, area=4034991.47, offchip=670208, perfw=11.82)


def load(name):
    p = os.path.join(RUNS, name, "iter_001.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def row(name, who, layer, hyp):
    d = load(name)
    if d is None:
        return f"| {name} | {who} | {layer} | {hyp} | _queued_ | | | | |"
    m = d.get("metrics") or {}
    e = d.get("energy") or {}
    c = m.get("counters") or {}
    cyc = m.get("cycles")
    uj = (e.get("energy_pj") or 0) / 1e6
    area = d.get("area_um2") or 0
    off = (c.get("RDMA_BYTES_REC", 0) or 0) + (c.get("WDMA_BYTES_SENT", 0) or 0)
    pw = e.get("perf_per_watt_gops_w")

    def rel(v, b, lower_better=True):
        if not v or not b:
            return ""
        r = b / v if lower_better else v / b
        return f" ({r:.3f}x)"

    return (f"| {name} | {who} | {layer} | {hyp} | "
            f"{cyc:,}{rel(cyc, BASE['cycles'])} | "
            f"{uj:.2f} uJ{rel(uj, BASE['energy'])} | "
            f"{off:,} B{rel(off, BASE['offchip'])} | "
            f"{area/1e6:.3f} mm2{rel(area, BASE['area'])} | "
            f"{pw:.2f}{rel(pw, BASE['perfw'], lower_better=False)} |")


def main():
    print("# Co-design ledger — jag512, measured\n")
    print("Baseline B1 = 26,946 cycles | 20.46 uJ | 670,208 B off-chip | "
          "4.035 mm2 | 11.82 GOPS/W.")
    print("Ratios are baseline/measured for cycles, energy, off-chip and area "
          "(>1 is better) and measured/baseline for perf/W.\n")
    print("| design point | proposed by | layer | hypothesis (recorded before the result) | "
          "cycles | energy | off-chip | area | perf/W |")
    print("|---|---|---|---|---|---|---|---|---|")
    for name, who, layer, hyp in POINTS:
        print(row(name, who, layer, hyp))
    print()
    print("Agent (Gemini) runs: 44 iterations across agent15, codesign15, "
          "codesign-sanity, codesign15b, codesign15c -- 0 admitted non-baseline "
          "designs. Every point above was proposed by Claude, not discovered by "
          "the Gemini agent loop.")


if __name__ == "__main__":
    sys.exit(main())
