"""N71-N73 bookkeeping: classification, revert detection, report parsing.

    python tests/test_recovery.py

Pure functions over plain dicts -- no Ray, no container, no model.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import recovery as R                                     # noqa: E402

P = {"k_chunk": 16, "sp_capacity_kb": 64, "acc_capacity_kb": 64, "gate_enable": True}
Q = {**P, "k_chunk": 64, "acc_capacity_kb": 32}          # the proposer's move


def test_classify_routes_verdicts():
    assert R.classify("T0_ILLEGAL").name == "t0"
    assert R.classify("COMPILE_FAILED").name == "compile"
    assert R.classify("EQUIV_FAILED").name == "divergence"
    assert R.classify("EQUIV_MISSING").name == "hang"
    assert R.classify("TRIPWIRE_FAILED").name == "tripwire"
    assert R.classify("DUPLICATE").name == "unknown"
    assert "DUPLICATE" not in R.REPAIRABLE and "NO_EDIT" not in R.REPAIRABLE


def test_infra_is_checked_before_the_verdict():
    # An OOM during elaboration still says "elaboration failed" -- it must not
    # be handed to a model as a design failure.
    fc = R.classify("ELABORATION_FAILED", stderr="java.lang.OutOfMemoryError: out of memory")
    assert fc.name == "infra" and not fc.to_agent


def test_every_repairable_class_has_a_recheck_cost():
    for v in R.REPAIRABLE:
        fc = R.CLASSES[R.BY_VERDICT[v]]
        assert fc.retries >= 1 and fc.recheck and fc.note, v


def test_revert_check_allows_partial_moves_and_coupled_fields():
    assert not R.revert_check(P, Q, {**Q, "k_chunk": 32}).is_revert        # part way back
    assert not R.revert_check(P, Q, {**Q, "sp_capacity_kb": 256}).is_revert  # coupled field


def test_revert_check_catches_full_and_overshooting_reverts():
    assert R.revert_check(P, Q, {**Q, "k_chunk": 16}).reverted              # back to parent
    assert R.revert_check(P, Q, {**Q, "k_chunk": 8}).reverted               # past the parent
    assert R.revert_check(P, Q, {**Q, "acc_capacity_kb": 64}).reverted
    assert R.revert_check(P, P, P, parent_rtl="a", proposed_rtl="b",
                          repaired_rtl="a").rtl_reverted


def test_parse_repair_report_reads_the_last_block():
    text = ("I will end with a ### ==REPAIR== block\n"
            "### ==REPAIR==\nstatus: NOT_ACTIONABLE\n"          # a quoted draft
            "## Fix\n...\n"
            "### ==REPAIR==\nstatus:     FIXED\nconfidence: 4/5\nclass: compile\n"
            "files:      a/PE.scala\n            b/SparseCraftParams.scala\n"
            "compiled:   PASS\npreserved:  yes\n")
    r = R.parse_repair_report(text)
    assert r["status"] == "FIXED" and r["confidence"] == 4
    assert r["files"] == ["a/PE.scala", "b/SparseCraftParams.scala"]
    assert r["compiled"] == "PASS"


def test_parse_repair_report_tolerates_absence():
    assert R.parse_repair_report("no block at all") == {"status": "", "files": []}
    assert R.parse_repair_report("### ==REPAIR==\nstatus: not_actionable\n")["status"] == "NOT_ACTIONABLE"


def test_attempts_text_names_repeats():
    a = [R.RepairAttempt(1, "compile", "COMPILE_FAILED", status="FIXED",
                         verdict_after="COMPILE_FAILED", evidence_after="[error] x"),
         R.RepairAttempt(2, "compile", "COMPILE_FAILED", status="FIXED",
                         verdict_after="ELABORATION_FAILED")]
    t = R.attempts_text(a)
    assert "the same failure came back: COMPILE_FAILED" in t
    assert "moved on to a new failure: **ELABORATION_FAILED**" in t
    assert R.attempts_text([]).startswith("None.")


def test_mechanism_text_extracts_both_sections():
    t = R.mechanism_text("x\n### ==MUTATION==\ntechnique: T-A\n\n### ==PREDICTION==\ntime: flat\n")
    assert "==MUTATION==" in t and "technique: T-A" in t and "time: flat" in t
    assert "did not emit" in R.mechanism_text("nothing here")


def test_shrink_divergence_names_the_ratio():
    s = R.shrink_divergence({"equiv_mismatches": 10, "equiv_first_i": 0, "equiv_first_j": 0,
                             "equiv_got": 380, "equiv_want": 190})
    assert s.first_index == (0, 0) and "2x the expected" in s.summary


def main() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e!r}")
    print(f"\n{fails} failure(s)" if fails else "\nAll recovery checks passed.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
