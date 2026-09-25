"""N10/N11 -- K candidate proposals, and scoring the agent's own predictions.

WHY FAN-OUT LOOKS DIFFERENT HERE THAN IN THE PLAN
-------------------------------------------------
The review's fan-out is "emit K candidates, evaluate K in parallel". That
shape does not fit this loop, and the reason is structural rather than
incidental: **the agent's write path is a single git worktree inside one
container.** It edits Chisel with a bash tool; there is exactly one tree, so
there is exactly one design state at a time. Evaluating K in parallel would
need K worktrees, K elaborations and K placement-group bundles -- a change to
the cluster topology, not to a prompt.

What IS available for one agent turn, and is most of the value:

1. **An auditable search policy.** V1 records what the agent did. It does not
   record what the agent CONSIDERED, so "why this move and not another" has
   no answer in the artifacts. K recorded candidates make the policy
   inspectable, which is the thing a reviewer actually asks about.

2. **A prediction-accuracy figure.** The output contract already demands a
   per-objective prediction. Nothing has ever scored it against the
   measurement. Predicted-vs-measured over N iterations is a genuinely novel
   result for an agentic co-design paper and costs one comparison per
   iteration.

3. **A backlog.** Candidates the agent proposed and did not implement are
   real, typed, already-reasoned design points. When the search stalls -- and
   `runs/final15` stalled into `unclassified` for four iterations -- a
   selector can hand one back instead of letting the agent re-derive from
   scratch.

So: one turn, one implemented mutation (unchanged), K recorded alternatives.
The tree stays single, the cost stays one agent call, and the artifacts get
the three things above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

_SECTION = re.compile(r"^###\s*==([A-Z]+)==\s*$", re.M)
# The direction word may be followed by commentary -- the agent routinely
# writes `energy: better  (~27.7 -> ~25.4 uJ, -8%)`, and that parenthetical is
# the useful part of the prediction. Anchoring to end-of-line dropped every
# such line silently, which is the worst way for a parser to fail.
_DIRECTION = re.compile(r"^\s*(time|energy|area|fmax)\s*:\s*(better|worse|flat)\b",
                        re.M | re.I)
_FIELD = re.compile(r"^\s*(technique|files|change|compiled|rationale|lever)\s*:\s*(.*)$",
                    re.M | re.I)

OBJECTIVES = ("time", "energy", "area", "fmax")


@dataclass
class Candidate:
    technique: str = ""
    change: str = ""
    rationale: str = ""
    predicted: dict = field(default_factory=dict)
    implemented: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _split_sections(text: str) -> dict:
    """Map ``==NAME==`` -> the body under it."""
    out, last, pos = {}, None, 0
    for m in _SECTION.finditer(text or ""):
        if last is not None:
            out[last] = text[pos:m.start()].strip()
        last, pos = m.group(1).upper(), m.end()
    if last is not None:
        out[last] = text[pos:].strip()
    return out


def parse_prediction(body: str) -> dict:
    """Per-objective direction from a ==PREDICTION== body."""
    return {k.lower(): v.lower() for k, v in _DIRECTION.findall(body or "")}


_BLOCK_START = re.compile(r"^\s*technique\s*:", re.M | re.I)
_LEVER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^\s,;]+)\s*->\s*([^\s,;.]+)")


def _same_move(a: str, b: str) -> bool:
    """Do two descriptions name the same move?

    The implemented move appears twice -- once in ==CANDIDATES==, once in
    ==MUTATION== -- usually worded differently ("acc_capacity_kb: 32 -> 16" vs
    "acc_capacity_kb: 32 -> 16. Halves the accumulator macro..."). Matching on
    the `field: old -> new` levers first, then on the normalised first line,
    keeps the implemented move out of the backlog of UNTRIED alternatives.
    """
    la, lb = set(_LEVER.findall(a or "")), set(_LEVER.findall(b or ""))
    if la and lb:
        return bool(la & lb)
    na = " ".join((a or "").lower().split())[:60]
    nb = " ".join((b or "").lower().split())[:60]
    return bool(na) and bool(nb) and (na.startswith(nb) or nb.startswith(na))


def parse_candidates(transcript: str) -> list:
    """Every candidate the agent offered, implemented one first.

    Tolerant by design. A transcript that omits ==CANDIDATES== yields just the
    implemented mutation, so this works unchanged against V1 transcripts and
    against a model that ignores the new section -- a parser that requires the
    new format would turn a prompt-following lapse into a lost iteration.

    Blocks are split at each `technique:` line. The first version split on
    blank lines with a pattern that needed TWO of them, while the contract asks
    for one -- so every list collapsed into a single block holding the last
    candidate's fields.
    """
    sec = _split_sections(transcript)
    out: list = []

    impl = None
    if "MUTATION" in sec:
        f = {k.lower(): v.strip() for k, v in _FIELD.findall(sec["MUTATION"])}
        impl = Candidate(technique=f.get("technique", ""),
                         change=f.get("change", ""),
                         predicted=parse_prediction(sec.get("PREDICTION", "")),
                         implemented=True)
        out.append(impl)

    if "CANDIDATES" in sec:
        body = sec["CANDIDATES"]
        starts = [m.start() for m in _BLOCK_START.finditer(body)]
        for i, st in enumerate(starts):
            blk = body[st:starts[i + 1] if i + 1 < len(starts) else len(body)]
            f = {k.lower(): v.strip() for k, v in _FIELD.findall(blk)}
            if not f:
                continue
            change = f.get("change", "") or f.get("lever", "")
            if impl is not None and _same_move(change, impl.change):
                # The implemented move, listed again: keep one copy, and take
                # the rationale the MUTATION section does not carry.
                impl.rationale = impl.rationale or f.get("rationale", "")
                continue
            out.append(Candidate(technique=f.get("technique", ""),
                                 change=change,
                                 rationale=f.get("rationale", ""),
                                 predicted=parse_prediction(blk),
                                 implemented=False))
    return out


def score_prediction(predicted: dict, measured: dict) -> dict:
    """Compare a stated prediction against what the harness measured.

    `measured` carries the raw values for parent and child; the direction is
    derived here rather than trusted, so an agent cannot score itself.

    "flat" is deliberately generous -- within 1% counts. The question is
    whether the agent modelled the MECHANISM, not whether it guessed a
    third decimal place.
    """
    got, hits, total = {}, 0, 0
    for k in OBJECTIVES:
        p = predicted.get(k)
        cur, prev = measured.get(k), measured.get(f"{k}_parent")
        if p is None or cur is None or prev is None or not prev:
            continue
        rel = (cur - prev) / prev
        # `better` means SMALLER for every objective here (time, energy, area,
        # and the period that stands in for fmax).
        actual = "flat" if abs(rel) < 0.01 else ("better" if rel < 0 else "worse")
        got[k] = {"predicted": p, "actual": actual, "rel": round(rel, 4),
                  "hit": p == actual}
        total += 1
        hits += int(p == actual)
    return {"per_objective": got, "hits": hits, "scored": total,
            "accuracy": (hits / total) if total else None}


def backlog(candidates: list, seen_changes: set | None = None) -> list:
    """Unimplemented candidates worth re-offering, freshest first.

    Filters out anything whose change string has already been tried, so a
    stalled search is handed something new rather than the move it just made.
    """
    seen = seen_changes or set()
    return [c for c in candidates
            if not c.implemented and c.change and c.change not in seen]
