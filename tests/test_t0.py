"""Verification step 3: every T0 rule must fire, by name, on a crafted illegal state."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from design_state import BASELINE, DesignState          # noqa: E402
import t0_legality as t0                                 # noqa: E402


def names(verdict):
    return {v.split(":")[0] for v in verdict.violations}


CASES = [
    # (rule name, mutation)
    ("gemmini.square_array",                 dict(meshRows=8)),
    ("gemmini.pow2_dim",                     dict(meshRows=12, meshColumns=12)),
    ("gemmini.min_dim",                      dict(meshRows=1, meshColumns=1)),
    ("gemmini.num_counter",                  dict(num_counter=256)),
    ("gemmini.sp_bank_entries_pow2",         dict(sp_capacity_kb=192)),
    ("gemmini.mvin_scale_shared",            dict(mvin_scale_shared=True)),
    ("tiling.block_div_tn",                  dict(block_size=32, tile_n=12)),
    ("tiling.block_div_tm",                  dict(block_size=32, tile_m=12)),
    ("capacity.working_set",                 dict(tile_m=512, tile_n=512, tile_k=512)),
    ("banking.gather_streams",               dict(sp_banks=2, sp_capacity_kb=128)),
    ("memory.littles_law",                   dict(max_in_flight_mem_reqs=4, dma_maxbytes=16)),
    ("fusion.no_materialised_S",             dict(has_normalizations=False)),
]


def main():
    failures = []

    assert t0.check(BASELINE).legal, f"baseline must be legal: {t0.check(BASELINE).report()}"
    print("PASS  baseline is legal")

    for rule, mut in CASES:
        got = names(t0.check(BASELINE.mutate(**mut)))
        if rule in got:
            print(f"PASS  {rule:<40} fired on {mut}")
        else:
            failures.append(f"{rule} did NOT fire on {mut}; got {sorted(got)}")
            print(f"FAIL  {rule:<40} did not fire on {mut} -> {sorted(got)}")

    # area budget is passed in, not a state field
    va = t0.check(BASELINE, predicted_area_um2=t0.AREA_BUDGET_UM2 * 2)
    if "area.budget" in names(va):
        print("PASS  area.budget                          fired on 2x budget")
    else:
        failures.append("area.budget did not fire")

    # N13 patch scope allowlist
    ok = t0.check_patch_scope(["generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala"])
    bad = t0.check_patch_scope(["tests/golden/reference_output.bin"])
    assert ok.legal, "the one writable file must be allowed"
    assert not bad.legal, "a golden-reference write must be denied"
    print("PASS  patch scope allowlist admits the params file, denies the golden reference")

    if failures:
        print(f"\n{len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nAll {len(CASES) + 3} T0 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
