#!/usr/bin/env python3
"""Synthesize the Pareto-front designs after a run, for measured area.

    python scripts/synth_front.py runs/agent-1 runs/greedy-1 --out paper/synth.csv

Running T3 inside the loop costs every iteration ~8 minutes of yosys plus, for
a hardware move, a fresh 20-40 minute elaboration. Only the designs that reach
a figure need measured area, so this does them afterwards -- the same pattern
the review prescribes for expensive tiers ("final Pareto set only").

Designs are grouped by ``hw_hash`` before elaborating. Points differing only in
software share one elaboration, which usually collapses a front of several
points onto one or two hardware builds.

MUST NOT run while an arm is running: it needs the `chipyard` resource to
elaborate, and there is one chipyard container whose tree the loop is using.
Wait for the loop to finish.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

# scripts/ -> ../src: every importable module lives there, flat.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ray                                                              # noqa: E402
from ray.util.placement_group import placement_group, remove_placement_group  # noqa: E402
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy    # noqa: E402

from chia.base.ChiaFunction import get                                  # noqa: E402

import constants as C                                                   # noqa: E402
import nodes                                                            # noqa: E402
import synth_recipe                                                     # noqa: E402
from design_state import BASELINE, DesignState                          # noqa: E402


def states_from_run(d: Path) -> dict[str, DesignState]:
    """{state_hash: DesignState} for every design a run measured.

    Records written after the state-recording change carry the parameters
    directly. Older ones carry only a hash, so the state is rebuilt by walking
    ``move.changed`` from the baseline: each record names its parent hash and
    the fields that differ, which is enough to reconstruct the chain.
    """
    known: dict[str, DesignState] = {BASELINE.state_hash(): BASELINE}
    out: dict[str, DesignState] = {}

    for f in sorted(d.glob("iter_*.json")):
        try:
            rec = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        h = rec.get("state_hash")
        if not h:
            continue

        if rec.get("state"):
            st = DesignState.from_dict(rec["state"])
        else:
            parent = known.get(rec.get("parent_hash"))
            changed = (rec.get("move") or {}).get("changed") or {}
            if parent is None:
                continue                      # chain broken; skip rather than guess
            # move.changed is {field: [child_value, parent_value]}
            st = parent.mutate(**{k: v[0] for k, v in changed.items()})
            if st.state_hash() != h:
                continue                      # reconstruction disagreed; do not use
        known[h] = st
        out[h] = st
    return out


def front_hashes(d: Path) -> set[str]:
    """State hashes admitted to the front, from history.json."""
    hp = d / "history.json"
    if not hp.exists():
        return set()
    try:
        h = json.loads(hp.read_text())
    except json.JSONDecodeError:
        return set()
    hashes = set()
    for p in h.get("front", []):
        if isinstance(p, dict) and p.get("state"):
            hashes.add(p["state"])
        elif isinstance(p, str):
            hashes.add(p)
    # Fall back to every admitted iteration if the front summary is unusable.
    if not hashes:
        for it in h.get("iterations", []):
            if str(it.get("verdict", "")).startswith("ADMIT"):
                hashes.add(it.get("state"))
    return {x for x in hashes if x}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default="paper/synth.csv")
    ap.add_argument("--clock-ns", type=float, default=C.SYNTH_CLOCK_NS)
    ap.add_argument("--all", action="store_true",
                    help="every measured design, not just the front")
    ap.add_argument("--limit", type=int, default=0, help="cap the number synthesized")
    a = ap.parse_args()

    targets: dict[str, tuple[str, DesignState]] = {}
    for r in a.runs:
        d = Path(r)
        states = states_from_run(d)
        want = set(states) if a.all else (front_hashes(d) & set(states))
        if not a.all and not want:
            print(f"  {d.name}: no front hashes resolved; use --all to take every design")
        for h in want:
            targets.setdefault(h, (d.name, states[h]))

    if not targets:
        print("nothing to synthesize")
        return 1

    by_hw: dict[str, list] = {}
    for h, (arm, st) in targets.items():
        by_hw.setdefault(st.hw_hash(), []).append((h, arm, st))
    print(f"{len(targets)} design(s) over {len(by_hw)} distinct hardware build(s)")
    if a.limit:
        by_hw = dict(list(by_hw.items())[:a.limit])
        print(f"  limited to {len(by_hw)} build(s)")

    ray.init(address=os.environ.get("RAY_ADDRESS", "auto"),
             runtime_env=C.runtime_env(), ignore_reinit_error=True)
    pg = placement_group([{"CPU": 1, C.R_CHIPYARD: 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    pg_opts = {"scheduling_strategy": PlacementGroupSchedulingStrategy(
        placement_group=pg, placement_group_bundle_index=0)}

    rows = []
    try:
        for i, (hw, group) in enumerate(by_hw.items(), start=1):
            _, arm, st = group[0]
            print(f"\n[{i}/{len(by_hw)}] hw={hw}  ({len(group)} design(s): "
                  f"{', '.join(h[:8] for h, _, _ in group)})")

            get(nodes.apply_design_state.options(**pg_opts)
                .chia_remote(json.dumps(st.canonical())))
            art = get(nodes.elaborate.options(**pg_opts)
                      .chia_remote(json.dumps(st.canonical()), collect_src=True,
                                   _chia_tag=f"hwsrc:{hw}"))
            if not art.success:
                print(f"    elaborate FAILED rc={art.returncode}")
                rows.append({"hw_hash": hw, "error": "elaborate",
                             "returncode": art.returncode})
                continue

            res = get(synth_recipe.synthesize_recipe
                      .chia_remote(art.generated_src_files,
                                   clock_period_ns=a.clock_ns,
                                   _chia_tag=f"recipe:{hw}@{a.clock_ns}"))
            print(f"    area={res.area_um2:,.0f} um2  cells={res.cell_count:,}  "
                  f"fmax={res.fmax_mhz}  power={res.power_total_w}")
            if not res.success:
                print(f"    synth FAILED rc={res.returncode}: {res.stderr[-300:]}")

            for h, arm_name, s in group:
                rows.append({
                    "arm": arm_name, "state_hash": h, "hw_hash": hw,
                    "block_size": s.block_size, "meshRows": s.meshRows,
                    "sp_capacity_kb": s.sp_capacity_kb, "sp_banks": s.sp_banks,
                    "acc_capacity_kb": s.acc_capacity_kb,
                    "area_um2_measured": res.area_um2,
                    "seq_cells": res.seq_cell_count, "cells": res.cell_count,
                    "worst_slack_ns": res.worst_slack_ns, "fmax_mhz": res.fmax_mhz,
                    "power_total_w": res.power_total_w,
                    "power_leakage_w": res.power_leakage_w,
                    # SRAM is blackboxed -- NanGate45 has no compiler -- so the
                    # exact byte count must sit beside the logic area.
                    "sram_bytes_exact": (s.sp_capacity_kb + s.acc_capacity_kb) * 1024,
                    "success": res.success,
                })
    finally:
        try:
            remove_placement_group(pg)
        except Exception:
            pass

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        keys = sorted({k for r in rows for k in r})
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    print(f"\nwrote {len(rows)} row(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
