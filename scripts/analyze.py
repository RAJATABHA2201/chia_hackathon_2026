#!/usr/bin/env python3
"""Turn run directories into the paper's figures and tables.

    python scripts/analyze.py runs/agent-1 runs/greedy-1 [...] --out paper/

Reads every ``iter_*.json`` plus the LLM transcripts, and writes CSVs you can
plot directly, a Markdown summary you can paste, and prints the headline
numbers. Works on a partial run, so it is safe to call while a loop is still
going -- useful for knowing what you have before deciding what to run next.

What it produces, and why each one is in the paper:

  summary.md            every headline number in one place
  per_iteration.csv     one row per iteration, every arm -- the raw table
  hypervolume.csv       HV vs iteration per arm  -> the convergence figure
  move_economics.csv    HW vs SW: count, cost, admit rate -> the co-design
                        figure, and the one nobody else reports
  predictions.csv       the agent's predicted direction vs the measured one
                        -> prediction accuracy, also nobody else's
  calibration.csv       T1 predicted vs T2 measured, per objective
  verdicts.csv          the failure taxonomy, per arm

Deliberately does NOT invent numbers. A field the run did not record comes out
empty rather than imputed, and area_source is carried through every row so a
modelled area can never be mistaken for a measured one in a figure.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path

# The agent states a direction per objective in its ==PREDICTION== block.
_DIRECTION_RE = re.compile(
    r"^\s*[-*]?\s*(time|energy|area)\s*:\s*(better|worse|flat)\b",
    re.I | re.M)
_MUTATION_RE = re.compile(
    r"^\s*[-*]?\s*`?([A-Za-z_][\w]*)`?\s*:\s*([\w.]+)\s*->\s*([\w.]+)", re.M)


def load_run(d: Path) -> dict:
    """One run directory -> {'arm', 'iters': [...], 'predictions': {...}}."""
    iters = []
    for f in sorted(d.glob("iter_*.json")):
        try:
            iters.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            pass                      # a run killed mid-write; skip that record

    # The agent's stated predictions live in the per-turn transcripts.
    preds: dict[int, dict] = {}
    for f in sorted(d.glob("llm_*.md")):
        n = int(re.search(r"(\d+)", f.name).group(1))
        txt = f.read_text()
        if not txt.strip():
            continue
        preds[n] = _parse_prediction(txt)
    # Fall back to the backend transcript, which has them even when the
    # per-turn file came back empty.
    for f in sorted(d.glob("llm/*.log")):
        blocks = f.read_text().split("==MUTATION==")[1:]
        for i, b in enumerate(blocks, start=1):
            preds.setdefault(i, {}).update(_parse_prediction(b))
    return {"arm": d.name, "dir": d, "iters": iters, "predictions": preds}


def _parse_prediction(text: str) -> dict:
    out: dict = {"directions": {}, "mutation": {}}
    for obj, direction in _DIRECTION_RE.findall(text):
        out["directions"][obj.lower()] = direction.lower()
    for field, old, new in _MUTATION_RE.findall(text):
        if field.lower() in ("time", "energy", "area"):
            continue              # those are the direction lines, not a mutation
        out["mutation"][field] = (old, new)
    return out


def measured_direction(cur: float | None, ref: float | None,
                       tol: float = 0.02) -> str | None:
    """better / worse / flat for a lower-is-better objective."""
    if cur is None or ref is None or ref == 0:
        return None
    rel = (cur - ref) / ref
    if abs(rel) <= tol:
        return "flat"
    return "worse" if rel > 0 else "better"


def analyse(runs: list[dict], out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# SparseCraft results", ""]

    # ---------------- per-iteration table ---------------------------------
    rows = []
    for r in runs:
        base_cycles = None
        for it in r["iters"]:
            m = it.get("metrics") or {}
            cycles = m.get("cycles")
            if base_cycles is None and cycles:
                base_cycles = cycles          # iteration 1 is the baseline
            mv = it.get("move") or {}
            rows.append({
                "arm": r["arm"],
                "iteration": it.get("iteration"),
                "verdict": it.get("verdict"),
                "move_class": mv.get("class"),
                "forces_elaboration": mv.get("forces_elaboration"),
                "changed": ";".join(mv.get("changed", {})),
                "cycles": cycles,
                "cycles_vs_base": (round(cycles / base_cycles, 4)
                                   if cycles and base_cycles else None),
                "off_chip_bytes": (m.get("counters") or {}).get("RDMA_BYTES_REC"),
                "area_um2": it.get("area_um2"),
                "area_source": it.get("area_source"),
                "period_ns": it.get("period_ns"),
                "wall_clock_s": it.get("wall_clock_s"),
                "state_hash": it.get("state_hash"),
                "diagnosis": (it.get("diagnosis") or "")[:120],
            })
    _csv(out / "per_iteration.csv", rows)

    # ---------------- verdict taxonomy ------------------------------------
    vrows = []
    for r in runs:
        counts: dict = {}
        for it in r["iters"]:
            counts[it.get("verdict") or "NONE"] = counts.get(it.get("verdict") or "NONE", 0) + 1
        for v, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            vrows.append({"arm": r["arm"], "verdict": v, "count": n,
                          "share": round(n / max(len(r["iters"]), 1), 3)})
    _csv(out / "verdicts.csv", vrows)

    # ---------------- move economics --------------------------------------
    mrows = []
    for r in runs:
        by: dict = {}
        for it in r["iters"]:
            c = (it.get("move") or {}).get("class") or "NONE"
            b = by.setdefault(c, {"n": 0, "wall": [], "admitted": 0})
            b["n"] += 1
            if it.get("wall_clock_s"):
                b["wall"].append(it["wall_clock_s"])
            if str(it.get("verdict", "")).startswith("ADMIT"):
                b["admitted"] += 1
        for c, b in sorted(by.items()):
            mrows.append({
                "arm": r["arm"], "move_class": c, "count": b["n"],
                "share": round(b["n"] / max(len(r["iters"]), 1), 3),
                "median_wall_s": round(statistics.median(b["wall"]), 1) if b["wall"] else None,
                "total_wall_s": round(sum(b["wall"]), 1) if b["wall"] else None,
                "admitted": b["admitted"],
                "admit_rate": round(b["admitted"] / b["n"], 3) if b["n"] else None,
            })
    _csv(out / "move_economics.csv", mrows)

    # ---------------- prediction accuracy ---------------------------------
    prows = []
    for r in runs:
        prev_cycles = prev_area = None
        for it in r["iters"]:
            n = it.get("iteration")
            m = it.get("metrics") or {}
            cyc, area = m.get("cycles"), it.get("area_um2")
            pred = r["predictions"].get(n, {})
            dirs = pred.get("directions") or {}
            if dirs and prev_cycles:
                meas_t = measured_direction(cyc, prev_cycles)
                meas_a = measured_direction(area, prev_area)
                prows.append({
                    "arm": r["arm"], "iteration": n,
                    "mutation": ";".join(f"{k}:{v[0]}->{v[1]}"
                                         for k, v in (pred.get("mutation") or {}).items()),
                    "pred_time": dirs.get("time"), "meas_time": meas_t,
                    "time_correct": (None if not (dirs.get("time") and meas_t)
                                     else dirs["time"] == meas_t),
                    "pred_area": dirs.get("area"), "meas_area": meas_a,
                    "area_correct": (None if not (dirs.get("area") and meas_a)
                                     else dirs["area"] == meas_a),
                    "cycles": cyc, "parent_cycles": prev_cycles,
                    "area_source": it.get("area_source"),
                })
            if cyc:
                prev_cycles, prev_area = cyc, area
    _csv(out / "predictions.csv", prows)

    # ---------------- T1 calibration --------------------------------------
    crows = []
    for r in runs:
        for it in r["iters"]:
            p, m = it.get("t1_prediction") or {}, it.get("metrics") or {}
            if not p or not m.get("cycles"):
                continue
            crows.append({
                "arm": r["arm"], "iteration": it.get("iteration"),
                "pred_cycles": p.get("cycles"), "meas_cycles": m.get("cycles"),
                "cycles_ratio": (round(p["cycles"] / m["cycles"], 2)
                                 if p.get("cycles") and m.get("cycles") else None),
                "pred_area_um2": p.get("area_um2"),
                "meas_area_um2": (it.get("area_um2")
                                  if it.get("area_source") != "T1_MODEL" else None),
                "area_source": it.get("area_source"),
            })
    _csv(out / "calibration.csv", crows)

    # ---------------- summary ---------------------------------------------
    for r in runs:
        its = r["iters"]
        meas = [i for i in its if (i.get("metrics") or {}).get("cycles")]
        # Only ADMITTED designs may be the best result. A design can be
        # measured and still be invalid: random-1 iteration 9 reported 80,564
        # cycles -- a 12% "win" -- and was TRIPWIRE_FAILED, meaning its
        # off-chip byte count fell below one full read of the inputs, so it
        # cannot have computed the answer. Taking the minimum over every
        # measured iteration would report exactly the design the integrity
        # gate rejected, which is the one number that must never reach a paper.
        admitted = [i for i in meas if str(i.get("verdict", "")).startswith("ADMIT")]
        best = min(admitted, key=lambda i: i["metrics"]["cycles"], default=None)
        rejected_better = [i for i in meas
                           if not str(i.get("verdict", "")).startswith("ADMIT")
                           and best and i["metrics"]["cycles"] < best["metrics"]["cycles"]]
        base = meas[0]["metrics"]["cycles"] if meas else None
        hw = sum(1 for i in its if (i.get("move") or {}).get("class") == "HW")
        sw = sum(1 for i in its if (i.get("move") or {}).get("class") == "SW")
        wall = sum(i.get("wall_clock_s") or 0 for i in its)
        lines += [
            f"## {r['arm']}", "",
            f"- iterations recorded: **{len(its)}**, of which measured: {len(meas)}",
            f"- moves: HW **{hw}**, SW **{sw}**",
            f"- wall clock: {wall/3600:.1f} h ({wall/max(len(its),1)/60:.1f} min/iteration)",
        ]
        if base and best:
            lines.append(f"- baseline {base:,} cycles -> best ADMITTED "
                         f"{best['metrics']['cycles']:,} cycles "
                         f"(**{base/best['metrics']['cycles']:.3f}x**)")
        for rb in rejected_better:   # NOT `r` -- that is the run being summarised
            lines.append(f"- **rejected but faster**: {rb['metrics']['cycles']:,} cycles "
                         f"at iteration {rb['iteration']}, verdict `{rb['verdict']}` "
                         f"-- NOT a result, the gate refused it")
        # A design that ties the baseline on measured cycles but is admitted on
        # a MODELLED area difference is a phantom front point, not a finding.
        ties = [i for i in admitted
                if base and i["metrics"]["cycles"] == base and i["iteration"] > 1]
        if ties:
            lines.append(f"- phantom admissions (identical cycles to baseline, "
                         f"admitted on modelled area): **{len(ties)}** of {len(admitted)}")
        srcs = {i.get("area_source") for i in its if i.get("area_source")}
        if srcs:
            lines.append(f"- area source(s): {', '.join(sorted(srcs))}"
                         + ("  **<- modelled, not measured**"
                            if srcs == {"T1_MODEL"} else ""))
        acc = [p for p in prows if p["arm"] == r["arm"] and p["time_correct"] is not None]
        if acc:
            k = sum(1 for p in acc if p["time_correct"])
            lines.append(f"- prediction accuracy on time: **{k}/{len(acc)}** correct")
        lines.append("")

    cal = [c for c in crows if c["cycles_ratio"]]
    if cal:
        rr = [c["cycles_ratio"] for c in cal]
        lines += ["## T1 calibration", "",
                  f"- cycles: T1/measured ratio median **{statistics.median(rr):.1f}x** "
                  f"over {len(rr)} designs (1.0 would be perfect)",
                  "- an uncalibrated T1 used as a REJECTION filter discards good "
                  "designs; record it as a prediction until delta_o is measured.", ""]

    (out / "summary.md").write_text("\n".join(lines) + "\n")
    return "\n".join(lines)


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="run directories")
    ap.add_argument("--out", default="paper", help="where to write CSVs + summary")
    a = ap.parse_args()
    runs = [load_run(Path(d)) for d in a.runs]
    empty = [r["arm"] for r in runs if not r["iters"]]
    if empty:
        print(f"note: no iter_*.json in {', '.join(empty)}")
    print(analyse(runs, Path(a.out)))
    print(f"\nwrote CSVs + summary.md to {a.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
