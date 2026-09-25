"""==CANDIDATES== / ==MUTATION== / ==PREDICTION== parsing and prediction scoring.

    python tests/test_candidates.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import candidates as K                                   # noqa: E402

TEXT = """Reasoning that mentions technique: nothing in particular.

### ==CANDIDATES==
technique: CONFIG
change:    acc_capacity_kb: 32 -> 16
rationale: the accumulator holds 4 KB of live partial sums
time:      flat
energy:    better
area:      better

technique: T-B
change:    granule 16 -> 4 on the A operand
rationale: 12% of A granules are all-zero at 4
time:      flat
energy:    better
area:      worse
---
technique: CONFIG
change:    k_chunk: 16 -> 8
rationale: halves the staged working set
time:      worse
energy:    flat
area:      flat

### ==MUTATION==
technique: CONFIG
files:     generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala
change:    acc_capacity_kb: 32 -> 16. Halves the accumulator macro.
compiled:  PASS

### ==PREDICTION==
The accumulator macro shrinks; nothing else moves.
  time:   flat
  energy: better  (~18.9 -> ~18.0 uJ)
  area:   better
  fmax:   flat
"""


def test_single_blank_lines_and_rules_both_split():
    c = K.parse_candidates(TEXT)
    assert [x.implemented for x in c] == [True, False, False], [(x.implemented, x.change) for x in c]
    assert c[1].change.startswith("granule 16 -> 4") and c[2].change == "k_chunk: 16 -> 8"


def test_implemented_move_is_not_listed_twice_and_keeps_its_rationale():
    c = K.parse_candidates(TEXT)
    assert sum(1 for x in c if "acc_capacity_kb" in x.change) == 1
    assert c[0].rationale.startswith("the accumulator holds")


def test_prediction_parsed_with_commentary():
    assert K.parse_candidates(TEXT)[0].predicted == {
        "time": "flat", "energy": "better", "area": "better", "fmax": "flat"}


def test_backlog_excludes_what_was_tried():
    c = K.parse_candidates(TEXT)
    left = K.backlog(c, {"k_chunk: 16 -> 8"})
    assert [x.change for x in left] == [c[1].change]


def test_score_prediction_derives_direction_from_numbers():
    sc = K.score_prediction({"time": "flat", "energy": "better", "area": "worse"},
                            {"time": 100.0, "time_parent": 100.4,
                             "energy": 18.0, "energy_parent": 18.9,
                             "area": 2.77, "area_parent": 2.87})
    assert sc["scored"] == 3 and sc["hits"] == 2
    assert sc["per_objective"]["area"]["actual"] == "better"


def test_no_sections_is_harmless():
    assert K.parse_candidates("the model wrote nothing structured") == []


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
    print(f"\n{fails} failure(s)" if fails else "\nAll candidate checks passed.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
