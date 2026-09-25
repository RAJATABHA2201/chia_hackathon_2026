"""Every T0 rule must fire, by name, on a crafted illegal state.

    python tests/test_t0.py          # plain runner, no pytest needed
    python -m pytest tests/          # if pytest is installed

The previous version targeted the attention-era rule set (`tiling.block_div_*`,
`capacity.working_set`) and mutated fields DesignState no longer has
(`block_size`, `tile_n`), so it crashed at the seventh case and tested nothing
after it. These cases are the rules t0_legality.py emits today.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from design_state import BASELINE                        # noqa: E402
import t0_legality as t0                                 # noqa: E402


def names(verdict):
    return {v.split(":")[0] for v in verdict.violations}


# (rule name, mutation). Each mutation is chosen so the named rule fires; other
# rules may fire too, which is fine -- the test is that THIS one is named.
CASES = [
    ("gemmini.square_array",                 dict(meshRows=8)),
    ("gemmini.pow2_dim",                     dict(meshRows=12, meshColumns=12)),
    ("gemmini.min_dim",                      dict(meshRows=1, meshColumns=1)),
    ("gemmini.num_counter",                  dict(num_counter=256)),
    ("gemmini.sp_bank_entries_pow2",         dict(sp_capacity_kb=192)),
    ("gemmini.mvin_scale_shared",            dict(mvin_scale_shared=True)),
    # (dim^2 + dim*64) * 2 B = 2,560 B at dim 16 > 2 KB
    ("capacity.sp",                          dict(sp_capacity_kb=2)),
    # dim * 64 * 4 B = 4,096 B > 2 KB
    ("capacity.acc",                         dict(acc_capacity_kb=2)),
    ("zbu.granule_divides_dim",              dict(granule_size=3)),
    # 16,384 rows * 16 granules/row = 262,144 bits > 5% of 256 KB
    ("zbu.bitmap_budget",                    dict(zbu_enable=True, granule_size=1)),
    # 1024 * 16 * (1 + 4) = 81,920 rows > 16,384
    ("sched.k_chunk_fits_scratchpad",        dict(k_chunk=1024)),
    # MAX_BLOCK_LEN = 64 / 16 = 4
    ("sched.b_blocks_dma",                   dict(b_blocks=8)),
    ("sched.a_blocks_layout",                dict(a_blocks=2)),
    ("banking.gather_streams",               dict(sp_banks=2, sp_capacity_kb=128)),
    ("memory.littles_law",                   dict(max_in_flight_mem_reqs=4, dma_maxbytes=16)),
    ("fusion.no_materialised_S",             dict(has_normalizations=False)),
]


def test_baseline_is_legal():
    v = t0.check(BASELINE)
    assert v.legal, f"baseline must be legal: {v.report()}"


def test_every_rule_fires_by_name():
    missed = []
    for rule, mut in CASES:
        got = names(t0.check(BASELINE.mutate(**mut)))
        if rule not in got:
            missed.append(f"{rule} did NOT fire on {mut}; got {sorted(got)}")
    assert not missed, "\n".join(missed)


def test_area_budget():
    va = t0.check(BASELINE, predicted_area_um2=t0.AREA_BUDGET_UM2 * 2)
    assert "area.budget" in names(va)


def test_frozen_benchmark_fields():
    moved = BASELINE.mutate(workload="dnn128")
    v = t0.check(moved, pinned={"workload": BASELINE.workload,
                                "dense_mode": BASELINE.dense_mode})
    assert "sparsecraft.frozen_workload" in names(v), names(v)


def test_patch_scope_allowlist():
    for path in sorted(t0.WRITABLE_PATHS):
        assert t0.check_patch_scope([path]).legal, f"writable path denied: {path}"
    for path in ("tests/golden/reference_output.bin",
                 "generators/gemmini/src/main/scala/gemmini/Scratchpad.scala",
                 "generators/gemmini/src/main/scala/gemmini/CounterFile.scala"):
        assert not t0.check_patch_scope([path]).legal, f"out-of-scope path allowed: {path}"


def main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
    print(f"\n{fails} failure(s)" if fails else f"\nAll T0 checks passed ({len(CASES)} rules).")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
