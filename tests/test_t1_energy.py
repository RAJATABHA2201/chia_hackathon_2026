"""The SRAM energy term: shrinking SRAM below the reference must earn energy.

    python tests/test_t1_energy.py

Before 2026-09-25 the floor was the 256 KB reference itself, so every design at
or below it paid the full per-access cost and V1's capacity shrinks (320 ->
192 -> 128 -> 96 -> 80 KB) all scored identical energy.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import t1_model as T                                     # noqa: E402
from design_state import BASELINE                        # noqa: E402
from metrics import Metrics                              # noqa: E402

KB = 1024
# V1 runs/final15 iteration 13's measured counters (x_resident, gated).
COUNTERS = {"macs_issued": 8388608, "tiles_issued": 512, "nz_blocks": 512,
            "total_blocks": 1024, "M": 512, "K": 512, "N": 64, "dim": 16,
            "nnz": 8192, "equiv_mismatches": 0, "EXE_ACTIVE_CYCLE": 38393,
            "LOAD_DMA_WAIT_CYCLE": 762, "MAC_GATED_TOTAL": 7864320,
            "RDMA_BYTES_REC": 196608, "WDMA_BYTES_SENT": 131072}


def test_scale_is_unchanged_above_the_reference():
    assert abs(T.sram_access_scale(320 * KB) - 1.118034) < 1e-6
    assert abs(T.sram_access_scale(256 * KB) - 1.0) < 1e-12


def test_scale_falls_below_the_reference_down_to_the_floor():
    s = [T.sram_access_scale(b * KB) for b in (192, 128, 96, 80, 32)]
    assert all(a > b for a, b in zip(s, s[1:])), s
    assert T.sram_access_scale(8 * KB) == T.sram_access_scale(T.SRAM_FLOOR_BYTES)


def test_smaller_sram_scores_less_energy_on_identical_counters():
    w = T.Workload.from_stats(json.load(open(os.path.join(
        ROOT, "workload", "generated", "spmm_dnn512.json"))), dense_mode=False)
    m = Metrics(50780, 524288, dict(COUNTERS))
    base = BASELINE.mutate(gate_enable=True, x_resident=True)
    e = [T.energy_report(base.mutate(sp_capacity_kb=sp, acc_capacity_kb=acc), m, w, 2.0).energy_pj
         for sp, acc in ((256, 64), (64, 64), (64, 32), (64, 16))]
    assert all(a > b for a, b in zip(e, e[1:])), e


def test_zbu_credit_is_row_level():
    """A finer T-B granule costs bitmap area but cannot skip MORE reads.

    The scratchpad gets one skip bit per read and suppresses a whole row, so
    granule 4 must earn exactly granule 16's energy, and both must beat off.
    """
    w = T.Workload.from_stats(json.load(open(os.path.join(
        ROOT, "workload", "generated", "spmm_dnn512.json"))), dense_mode=False)
    m = Metrics(50862, 524288, dict(COUNTERS))
    s = BASELINE.mutate(gate_enable=True, x_resident=True, sp_capacity_kb=64,
                        acc_capacity_kb=32)
    e_off = T.energy_report(s, m, w, 2.0).energy_pj
    e16 = T.energy_report(s.mutate(zbu_enable=True, granule_size=16), m, w, 2.0).energy_pj
    e4 = T.energy_report(s.mutate(zbu_enable=True, granule_size=4), m, w, 2.0).energy_pj
    assert e16 < e_off, (e16, e_off)
    assert abs(e4 - e16) < 1e-6, (e4, e16)


def main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as ex:
                fails += 1
                print(f"FAIL  {name}: {ex!r}")
    print(f"\n{fails} failure(s)" if fails else "\nAll energy-model checks passed.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
