"""N71 error classifier, N72 failure shrinker, N73 repair orchestration.

WHY THIS FILE EXISTS
--------------------
V1 has ONE failure edge. Every gate failure -- a Chisel width mismatch, a
functional divergence, a spot-VM preemption -- reverts to the parent and costs
the whole iteration, and the whole log goes back to the same agent with the
same prompt. Three problems follow from that, and they are different problems:

1. **A recoverable failure is not recovered.** In `runs/agentic15b` ten of
   fourteen proposals died at the compile gate: 71% of the evaluation budget
   spent, nothing learned, no repair attempted. CHIA's own `riscv_extensions`
   splits Implement-LLM from Debug-LLM precisely here.

2. **Infrastructure failure is fed back as design failure.** A preempted
   worker currently tells the agent "your design failed to build". That is
   false, and it teaches the agent to abandon a mutation family that was
   fine. This class must never reach a model.

3. **The failing case is shown raw.** A 32,674-mismatch divergence is not a
   debugging input; the first mismatching index in a minimised case is.

CLASSES ARE NOT SEVERITIES. Each class carries its own retry policy, its own
budget accounting, and its own answer to "should a model see this at all".

REPAIR IS ITERATIVE. The loop re-runs the whole gate ladder after every repair
turn and hands the next turn the NEW evidence plus a ledger of what earlier
attempts changed and what came back (`attempts_text`). Sessions are not
resumed, so that ledger is the repair agent's only memory. Reverts are decided
here from the parent / proposed / repaired states (`revert_check`), never from
the agent's self-audit.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


# --- the taxonomy ----------------------------------------------------------
#
# `charged` is whether the failure consumes an evaluation from the budget. A
# timing miss is a real Pareto data point and is charged; an OOM is not the
# design's fault and is not.
#
# `to_agent` is whether ANY model is allowed to see it. Infra is the one class
# where the answer is no, for the reason in (2) above.
@dataclass(frozen=True)
class FailureClass:
    name: str
    retries: int
    charged: bool
    to_agent: bool
    shrink: bool = False
    note: str = ""
    # What the harness re-runs after a repair attempt, and roughly what it
    # costs. Shown to the repair agent, because "a wrong guess here costs 40
    # minutes" is the single most useful fact for deciding how sure to be.
    recheck: str = "the full gate ladder"


CLASSES = {
    # Retry counts are per ITERATION and are also capped by the loop's total
    # --repair-budget. They are sized by the price of the re-check: a compile
    # or T0 attempt costs seconds to verify, a divergence attempt costs a full
    # elaboration plus a 16-minute simulation.
    "t0": FailureClass(
        "t0", retries=2, charged=False, to_agent=True,
        recheck="T0 legality (microseconds), then the full ladder",
        note="The design state violates a named legality rule. Nothing was "
             "built. Work the rule's arithmetic; move a coupled field or move "
             "the mutated field PART of the way back, never all of it."),
    "compile": FailureClass(
        "compile", retries=3, charged=False, to_agent=True,
        recheck="sbt compile (20 s to 3 min), then the full ladder",
        note="Scala/Chisel type error. Cheap to fix and cheap to re-check: the "
             "compile gate is 1-3 min against the 20-40 min an elaboration costs. "
             "You can run the compile yourself; do."),
    "elaboration": FailureClass(
        "elaboration", retries=2, charged=False, to_agent=True,
        recheck="a full Chisel elaboration and Verilator build (20-40 min)",
        note="FIRRTL/elaboration failure. A Gemmini require() here should be "
             "PROMOTED TO A T0 RULE so the same illegal state is never paid "
             "for twice."),
    "lint": FailureClass(
        "lint", retries=2, charged=False, to_agent=True,
        recheck="a full Chisel elaboration and Verilator build (20-40 min)",
        note="Verilator width/latch/comb-loop. Escalated to fatal in the "
             "immutable harness config, not in anything the agent can edit."),
    "kernel": FailureClass(
        "kernel", retries=2, charged=False, to_agent=True,
        recheck="the kernel cross-compile (1-3 min; elaboration is reused if "
                "the hardware did not change)",
        note="Cross-compile failure. Usually gemmini_params.h disagreeing with "
             "the kernel's tiling assumptions after a hardware change."),
    "divergence": FailureClass(
        "divergence", retries=1, charged=False, to_agent=True, shrink=True,
        recheck="elaboration if the hardware changed, then a 16-minute "
                "simulation and the golden comparison",
        note="Functional mismatch against the golden reference. SHRINK FIRST: "
             "a first-mismatch index beats 32,674 raw mismatches."),
    "hang": FailureClass(
        "hang", retries=1, charged=False, to_agent=True,
        recheck="elaboration if the hardware changed, then a full simulation",
        note="The kernel never printed its equivalence line: the design hung "
             "(a handshake that never completes) or the simulation timed out."),
    "tripwire": FailureClass(
        "tripwire", retries=1, charged=False, to_agent=True,
        recheck="elaboration if the hardware changed, then a full simulation",
        note="Off-chip bytes fell below one read of the inputs: the design "
             "skipped a load the result depends on, or a counter stopped "
             "counting."),
    "rtl_noop": FailureClass(
        "rtl_noop", retries=1, charged=False, to_agent=True,
        recheck="a full Chisel elaboration (20-40 min)",
        note="Source changed, elaborated netlist did not. Usually logic behind "
             "a Scala `if` on a parameter that is false."),
    "scope": FailureClass(
        "scope", retries=1, charged=False, to_agent=True,
        recheck="the scope allowlist (seconds), then the full ladder",
        note="Touched a file outside the writable set. The allowlist is "
             "enforced before git apply and is not negotiable."),
    "timing": FailureClass(
        "timing", retries=0, charged=True, to_agent=False,
        note="WNS < 0 is NOT an error -- it is a Pareto data point. Record the "
             "achieved Fmax and let the objective handle it."),
    "infra": FailureClass(
        "infra", retries=0, charged=False, to_agent=False,
        note="Preemption, OOM, container death, network. NOT a design failure. "
             "Never shown to a model: 'your design failed to build' is a lie "
             "that costs a good mutation family."),
    "unknown": FailureClass(
        "unknown", retries=1, charged=False, to_agent=True,
        note="Unclassified. Widen the table rather than letting these "
             "accumulate silently."),
}


# The loop's own verdicts -> class. Verdicts NOT listed here (AGENT_FAILED,
# NO_EDIT, DUPLICATE) are proposal-policy failures, not bugs in a mutation, and
# REPAIRABLE excludes them for that reason.
BY_VERDICT = {
    "T0_ILLEGAL": "t0",
    "COMPILE_FAILED": "compile",
    "ELABORATION_FAILED": "elaboration",
    "KERNEL_BUILD_FAILED": "kernel",
    "EQUIV_FAILED": "divergence",
    "EQUIV_MISSING": "hang",
    "TRIPWIRE_FAILED": "tripwire",
    "RTL_NOOP": "rtl_noop",
    "SCOPE_VIOLATION": "scope",
    "INFRA_FAILURE": "infra",
}
REPAIRABLE = frozenset(v for v, c in BY_VERDICT.items() if CLASSES[c].to_agent)


# Ordered most-specific first. Infra patterns are checked BEFORE design
# patterns: an OOM during elaboration says "elaboration failed" in the log too,
# and misreading it as a design failure is exactly the bug this prevents.
_INFRA = re.compile(
    r"out of memory|oom-kill|memoryerror|ray\.exceptions\.OutOfMemory|"
    r"preempt|instance terminated|node.*died|connection refused|"
    r"raylet.*died|lost connection|task was killed", re.I)

_PATTERNS = (
    ("lint",        re.compile(r"%Warning-(WIDTH|LATCH|UNOPTFLAT|COMBDLY)|%Error-", re.I)),
    ("compile",     re.compile(r"\[error\].*\.scala|not found: value|type mismatch|"
                               r"value .* is not a member|sbt.*error", re.I)),
    ("elaboration", re.compile(r"firrtl|elaborat|requirement failed|chisel3\.", re.I)),
    ("kernel",      re.compile(r"riscv64-unknown-elf-gcc|undefined reference|"
                               r"implicit declaration|\.h: No such file", re.I)),
)


def classify(verdict: str | None = None, *, stderr: str = "",
             returncode: int | None = None, exception: str = "") -> FailureClass:
    """Name the failure class. Programmatic, deterministic, no model involved.

    `verdict` is the loop's own label when it has one; `stderr`/`exception`
    are searched when it does not, or when the verdict is too coarse to act on.
    """
    blob = f"{stderr}\n{exception}"

    # Infra first, always. See the comment on _INFRA.
    if _INFRA.search(blob):
        return CLASSES["infra"]

    if verdict in BY_VERDICT:
        return CLASSES[BY_VERDICT[verdict]]

    for name, pat in _PATTERNS:
        if pat.search(blob):
            return CLASSES[name]

    return CLASSES["unknown"]


# --- N72: minimise a failing case before any model sees it -----------------

@dataclass
class ShrunkCase:
    summary: str
    first_index: tuple | None = None
    got: int | None = None
    want: int | None = None
    total_mismatches: int = 0
    extra: dict = field(default_factory=dict)


def shrink_divergence(counters: dict) -> ShrunkCase:
    """Reduce a functional divergence to the one fact worth debugging.

    The simulator already records the first mismatching element -- the loop
    just never used it, and fed the raw count to the agent instead. "32,674
    mismatches" says a design is wrong; "[0,0] got 339 want 190" says where to
    look. A programmatic shrinker is cheap and transforms repair success.

    Deliberately reads only counters the HARNESS owns. A shrinker that
    re-simulated would be a second measurement path the agent could influence.
    """
    n = int(counters.get("equiv_mismatches", 0) or 0)
    i = counters.get("equiv_first_i")
    j = counters.get("equiv_first_j")
    got = counters.get("equiv_got")
    want = counters.get("equiv_want")

    if n and i is not None and j is not None and i >= 0:
        delta = (got - want) if (got is not None and want is not None) else None
        hint = ""
        if delta is not None and want:
            ratio = got / want if want else 0
            if abs(ratio - round(ratio)) < 1e-6 and round(ratio) > 1:
                hint = (f" The result is {round(ratio)}x the expected value, which "
                        f"usually means an accumulation ran more times than it "
                        f"should -- check tiling against DIM.")
            elif delta == 0:
                hint = " The first mismatch has zero delta; suspect an index, not arithmetic."
        return ShrunkCase(
            summary=(f"{n:,} mismatching outputs. FIRST at [{i},{j}]: got {got}, "
                     f"want {want}.{hint}"),
            first_index=(i, j), got=got, want=want, total_mismatches=n)

    return ShrunkCase(summary=f"{n:,} mismatching outputs; no first-index counter recorded.",
                      total_mismatches=n)


# --- N73: the repair loop's bookkeeping -----------------------------------
#
# The loop runs:  evaluate -> gate fails -> repair turn -> RE-EVALUATE the whole
# ladder -> ...  until the tree passes, the repair agent says NOT_ACTIONABLE,
# or the per-class / per-iteration budget runs out. What follows is everything
# about that cycle that can be decided WITHOUT a cluster: which fields the
# mutation moved, whether a repair reverted them, what each attempt did, and
# what the agent reported. Pure functions over plain dicts, so they are
# testable on the head with no Ray and no container.


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def mutated_fields(parent: dict, proposed: dict) -> dict:
    """``{field: (parent_value, proposed_value)}`` for every field that moved."""
    return {k: (parent.get(k), proposed.get(k))
            for k in sorted(set(parent) | set(proposed))
            if parent.get(k) != proposed.get(k)}


def mutated_fields_text(parent: dict, proposed: dict) -> str:
    moved = mutated_fields(parent, proposed)
    if not moved:
        return ("(no design-state field changed: the mutation is in the RTL "
                "source only -- see the files below)")
    return "\n".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in moved.items())


@dataclass
class RevertCheck:
    """Did a repair undo what the proposer did?

    A revert is the cheapest way through any gate and the one outcome that
    makes a repair worthless, so it is decided here, from the three states,
    rather than trusted from the agent's self-audit.

    Per mutated field ``f`` moved ``p -> q`` by the proposer, the repaired
    value ``r`` is a REVERT when:
      - ``r == p`` (moved all the way back), or
      - numeric and on the far side of ``p`` from ``q`` (moved past it).
    ``r`` strictly between ``p`` and ``q`` is allowed: that is "the nearest
    legal value in the direction the proposer chose", which is how a T0
    capacity rule is legitimately repaired.
    """
    reverted: dict = field(default_factory=dict)     # f -> (p, q, r)
    rtl_reverted: bool = False
    repair_changed: dict = field(default_factory=dict)  # f -> (q, r), any field

    @property
    def is_revert(self) -> bool:
        return bool(self.reverted) or self.rtl_reverted

    def summary(self) -> str:
        parts = [f"{f}: proposer {p!r} -> {q!r}, repair set {r!r}"
                 for f, (p, q, r) in self.reverted.items()]
        if self.rtl_reverted:
            parts.append("the RTL source is byte-identical to the parent's again")
        return "; ".join(parts) or "no revert"


def revert_check(parent: dict, proposed: dict, repaired: dict, *,
                 parent_rtl: str | None = None, proposed_rtl: str | None = None,
                 repaired_rtl: str | None = None) -> RevertCheck:
    out = RevertCheck()
    for f, (p, q) in mutated_fields(parent, proposed).items():
        r = repaired.get(f)
        if r == p:
            out.reverted[f] = (p, q, r)
        elif _num(p) and _num(q) and _num(r) and (q - p) * (r - p) < 0:
            out.reverted[f] = (p, q, r)
    for f in sorted(set(proposed) | set(repaired)):
        if proposed.get(f) != repaired.get(f):
            out.repair_changed[f] = (proposed.get(f), repaired.get(f))
    # RTL: the proposer changed the source and the repair put it back exactly.
    if (proposed_rtl and parent_rtl and repaired_rtl
            and proposed_rtl != parent_rtl and repaired_rtl == parent_rtl):
        out.rtl_reverted = True
    return out


@dataclass
class RepairAttempt:
    """One repair turn and what the harness measured after it."""
    attempt: int
    failure_class: str
    verdict_before: str
    status: str = ""            # the agent's ==REPAIR== status
    confidence: int | None = None
    files: list = field(default_factory=list)
    compiled: str = ""
    verdict_after: str = ""     # "" until re-evaluated; "PASS" if it cleared
    evidence_after: str = ""
    changed: str = ""           # what the repair changed in the design state
    wall_s: float = 0.0
    call_ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def attempts_text(attempts: list) -> str:
    """The PRIOR_ATTEMPTS block: what was tried, and what the harness saw.

    This is the repair loop's memory. Sessions are not resumed, so without it
    attempt 3 would re-derive attempt 1's hypothesis and re-apply attempt 1's
    fix. Bounded: one short paragraph per attempt.
    """
    if not attempts:
        return "None. This is the first repair attempt for this iteration."
    out = []
    for a in attempts:
        after = a.verdict_after or "(not re-evaluated)"
        if a.verdict_after == a.verdict_before:
            verdict = f"**the same failure came back: {after}**"
        elif a.verdict_after == "PASS":
            verdict = "passed"
        else:
            verdict = f"moved on to a new failure: **{after}**"
        lines = [f"- Attempt {a.attempt} ({a.failure_class}, reported "
                 f"`{a.status or 'no ==REPAIR== block'}`, confidence "
                 f"{a.confidence if a.confidence is not None else '?'}/5): "
                 f"{verdict}."]
        if a.files:
            lines.append(f"  files changed: {', '.join(a.files)}")
        if a.changed:
            lines.append(f"  design-state change: {a.changed}")
        if a.evidence_after:
            ev = a.evidence_after.strip().replace("\n", "\n    ")
            lines.append(f"  evidence afterwards:\n    {ev}")
        out.append("\n".join(lines))
    return ("The harness re-ran the gates after each of these. Where the same "
            "failure came back, that fix did not address the cause: do not "
            "repeat it.\n\n" + "\n".join(out))


_REPAIR_SECTION = re.compile(r"^###\s*==REPAIR==\s*$", re.M)
_REPAIR_FIELD = re.compile(r"^\s*(status|confidence|class|files|compiled|preserved)"
                           r"\s*:\s*(.*)$", re.I)


def parse_repair_report(text: str) -> dict:
    """The agent's ``==REPAIR==`` block, tolerant of formatting slips.

    Read from the LAST header, so a model that quotes the contract in its
    reasoning cannot be parsed from the quote. `files` may continue on the
    following lines. Missing block -> ``{"status": ""}``, which the loop
    treats as "made an edit, did not report it": the gates still decide.
    """
    heads = list(_REPAIR_SECTION.finditer(text or ""))
    if not heads:
        return {"status": "", "files": []}
    body = text[heads[-1].end():]
    out: dict = {"files": []}
    key = None
    for line in body.splitlines():
        m = _REPAIR_FIELD.match(line)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "files":
                if val and val.lower() != "none":
                    out["files"].append(val)
            else:
                out[key] = val
            continue
        if key == "files" and line.strip() and not line.strip().startswith(("#", "`")):
            out["files"].append(line.strip())
        elif line.strip().startswith("#"):
            break
    st = (out.get("status") or "").upper()
    out["status"] = ("NOT_ACTIONABLE" if "NOT_ACTIONABLE" in st
                     else "FIXED" if "FIXED" in st else "")
    try:
        out["confidence"] = int(re.search(r"\d", out.get("confidence", "")).group(0))
    except (AttributeError, TypeError):
        out["confidence"] = None
    return out


def mechanism_text(proposer_transcript: str, limit: int = 2500) -> str:
    """The proposer's ==MUTATION== and ==PREDICTION== sections, verbatim.

    The repairer's first instruction is "read the stated mechanism". V1 and
    the first V2 draft said so and never passed it, so the repairer was asked
    to preserve an intent it was never shown.
    """
    from candidates import _split_sections
    sec = _split_sections(proposer_transcript or "")
    parts = [f"### =={k}==\n{sec[k].strip()}" for k in ("MUTATION", "PREDICTION")
             if sec.get(k)]
    text = "\n\n".join(parts)
    if not text:
        return ("(the proposer did not emit ==MUTATION==/==PREDICTION== "
                "sections; infer the intent from the diff and the changed fields)")
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def evidence_text(fc: FailureClass, *, stderr: str = "", violations=None,
                  shrunk: ShrunkCase | None = None, extra: str = "",
                  context_lines: int = 40) -> str:
    """The EVIDENCE block. Bounded on purpose.

    A repair prompt that ships the whole log costs more than the iteration it
    is trying to save and buries the one line that matters. Compile errors
    carry file:line, T0 violations carry their arithmetic, a divergence is one
    shrunk element -- each is small and each is the whole story.
    """
    parts = []
    if violations:
        parts.append("T0 violations:\n" + "\n".join(f"  - {v}" for v in violations))
    if shrunk is not None:
        parts.append("Minimised failing case: " + shrunk.summary)
    if extra:
        parts.append(extra.strip())
    if stderr:
        tail = "\n".join(stderr.strip().split("\n")[-context_lines:])
        parts.append(f"Last {context_lines} lines of the failing output:\n{tail}")
    return "\n\n".join(parts) or "(the harness recorded no further detail)"


def root_cause_text(transcript: str, limit: int = 900) -> str:
    """The repair agent's "## Root cause" paragraph, for the PROPOSER.

    When a repair ends NOT_ACTIONABLE the repairer has usually worked out WHY
    the mechanism cannot work here -- and that is exactly what the proposer
    needs so it does not propose the same thing again. Measured, run
    v2-final15: the repairer proved (5/5) that no array size other than the
    workload's 16x16 blocking can compute Y correctly; the proposer was told only
    "wrong answer, rolled back", and re-proposed the identical design twice.
    """
    t = transcript or ""
    if "<!-- transcript -->" in t:
        t = t.split("<!-- transcript -->", 1)[1]
    m = re.search(r"^##\s*Root cause\s*$(.*?)(?=^##\s|^###\s*==REPAIR==|\Z)", t, re.M | re.S)
    body = " ".join((m.group(1) if m else "").split())
    return body if len(body) <= limit else body[:limit].rsplit(" ", 1)[0] + " ..."
