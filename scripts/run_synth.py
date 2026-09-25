#!/usr/bin/env python3
"""One-shot T3: elaborate a config, synthesize it, print measured area and Fmax.

The loop's ``--synth`` flag runs this tier inside the search. This script runs
it on its own, which is what you want for the two numbers a paper actually
needs: the baseline Gemmini's area and Fmax, and the modified design's, in the
same technology at the same clock target.

    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    chia up configs/cluster.yaml
    chia job submit --working-dir . -- python scripts/run_synth.py --compare

``--compare`` elaborates and synthesizes both configs and prints the ratio
table. Both runs go through the same cache the loop uses, so a config already
elaborated for a loop run is not elaborated again.

Nothing here estimates anything. If synthesis fails the row says so; it does
not fall back to a model, because the entire point of this script is to
produce numbers that came out of a tool.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# scripts/ -> ../src: every importable module lives there, flat.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ray                                                              # noqa: E402
from ray.util.placement_group import placement_group, remove_placement_group  # noqa: E402
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy    # noqa: E402

from chia.base.ChiaFunction import get                                  # noqa: E402
from chia.base.bypass import Bypass, get_active_bypass                  # noqa: E402
from chia.base.cache import start_cache, stop_cache                     # noqa: E402

import constants as C                                                   # noqa: E402
import nodes                                                            # noqa: E402
import synth_node                                                       # noqa: E402
from design_state import BASELINE                                       # noqa: E402


def synth_one(config_name: str, state_json: str, args, pg_opts: dict) -> dict:
    """Elaborate one config with RTL collection, then synthesize it."""
    t0 = time.time()
    print(f"\n--- {config_name} ---")
    print("  elaborating (collect_src=True, ENABLE_YOSYS_FLOW=1) ...")
    art = get(nodes.elaborate.options(**pg_opts).chia_remote(
        state_json, config_name=config_name, collect_src=True,
        _chia_tag=f"hwsrc:{config_name}:{args.state_tag}"))
    if not art.success:
        print(f"  ELABORATION FAILED rc={art.returncode}")
        print((art.stderr or "")[-2000:])
        return {"config": config_name, "ok": False, "stage": "elaborate",
                "returncode": art.returncode}
    n_src = len(art.generated_src_files)
    print(f"  elaborated: {n_src} generated source files "
          f"[{time.time() - t0:.0f}s]")
    if n_src == 0:
        return {"config": config_name, "ok": False, "stage": "elaborate",
                "error": "no generated sources -- a cached artifact from a "
                         "run without collect_src probably answered this"}

    print(f"  synthesizing top={args.top} tech={args.tech} "
          f"clock={args.clock_ns}ns ...")
    syn = get(synth_node.synthesize.chia_remote(
        art.generated_src_files, top_module=args.top,
        clock_period_ns=args.clock_ns, technology=args.tech,
        top_hint=args.top_hint,
        _chia_tag=f"syn:{config_name}:{args.state_tag}@{args.tech}@{args.clock_ns}"))

    row = {"config": config_name, "ok": syn.success, "stage": "synthesize",
           "top_module": syn.top_module, "technology": syn.technology,
           "area_um2": syn.area_um2, "cell_count": syn.cell_count,
           "seq_cells": syn.seq_cell_count,
           "clock_target_ns": syn.clock_target_ns,
           "worst_slack_ns": syn.worst_slack_ns, "fmax_mhz": syn.fmax_mhz,
           "returncode": syn.returncode,
           "wall_clock_s": round(time.time() - t0, 1)}
    if not syn.success:
        print(f"  SYNTHESIS FAILED rc={syn.returncode}")
        print((syn.stderr or "")[-2000:])
        row["stderr_tail"] = (syn.stderr or "")[-4000:]
    else:
        fmax = f"{syn.fmax_mhz:.1f} MHz" if syn.fmax_mhz else "n/a (no STA slack)"
        print(f"  area  {syn.area_um2:>14,.1f} um2")
        print(f"  cells {syn.cell_count:>14,}  ({syn.seq_cell_count:,} sequential)")
        print(f"  Fmax  {fmax:>14}   (slack {syn.worst_slack_ns} ns "
              f"vs {syn.clock_target_ns} ns target)")
    row["reports"] = syn.reports
    row["cells_by_type"] = syn.cells_by_type
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=C.CONFIG_NAME,
                    help=f"Chisel config to synthesize (default {C.CONFIG_NAME})")
    ap.add_argument("--baseline", action="store_true",
                    help=f"synthesize {C.BASELINE_CONFIG_NAME} instead")
    ap.add_argument("--compare", action="store_true",
                    help="synthesize both and print the ratio table")
    ap.add_argument("--tech", default=C.SYNTH_TECHNOLOGY)
    ap.add_argument("--clock-ns", type=float, default=C.SYNTH_CLOCK_NS)
    ap.add_argument("--top", default=C.SYNTH_TOP_MODULE,
                    help="'auto' resolves the outermost module matching --top-hint")
    ap.add_argument("--top-hint", default="Gemmini")
    ap.add_argument("--state-tag", default="tree",
                    help="cache-key suffix; change it to force a re-elaboration "
                         "after editing the Scala by hand")
    ap.add_argument("--out", default=None, help="write the JSON report here")
    args = ap.parse_args()

    configs = ([C.BASELINE_CONFIG_NAME, C.CONFIG_NAME] if args.compare
               else [C.BASELINE_CONFIG_NAME if args.baseline else args.config])

    runtime_env = C.runtime_env()
    if "RAY_JOB_CONFIG_JSON_ENV_VAR" in os.environ:
        runtime_env = {k: v for k, v in runtime_env.items() if k != "working_dir"}
    ray.init(address=os.environ.get("RAY_ADDRESS", "auto"),
             runtime_env=runtime_env, ignore_reinit_error=True)

    cfg = os.path.join(C.CONFIG_DIR, "bypass_cache.yaml")
    cache = start_cache(size=32, units="GB", cache_dir_path=C.CACHE_DIR, yaml_path=cfg)
    Bypass(yaml_path=cfg)

    def provider(tag, data_path, *a, **kw):
        hit, value = get(cache.read.chia_remote(tag))
        if not hit:
            raise KeyError(f"cache miss for {tag!r}")
        return value

    def cond(tag, data_path, *a, **kw):
        return get(cache.has.chia_remote(tag))

    for fn in ("elaborate", "synthesize"):
        get_active_bypass().set_provider(fn, provider)
        get_active_bypass().set_cond(fn, cond)

    pg = placement_group([{"CPU": 1, C.R_CHIPYARD: 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    pg_opts = {"scheduling_strategy": PlacementGroupSchedulingStrategy(
        placement_group=pg, placement_group_bundle_index=0)}

    state_json = json.dumps(BASELINE.canonical())
    rows = []
    try:
        for name in configs:
            rows.append(synth_one(name, state_json, args, pg_opts))
    finally:
        try:
            remove_placement_group(pg)
        except Exception:
            pass
        stop_cache()

    print("\n" + "=" * 74)
    print(f"{'config':<28} {'area um2':>13} {'cells':>10} {'Fmax MHz':>10}")
    print("-" * 74)
    for r in rows:
        if r.get("ok"):
            fm = f"{r['fmax_mhz']:.1f}" if r.get("fmax_mhz") else "n/a"
            print(f"{r['config']:<28} {r['area_um2']:>13,.1f} "
                  f"{r['cell_count']:>10,} {fm:>10}")
        else:
            print(f"{r['config']:<28} {'FAILED at ' + r.get('stage', '?'):>35}")

    good = [r for r in rows if r.get("ok")]
    if len(good) == 2:
        base, cand = good[0], good[1]
        print("-" * 74)
        da = cand["area_um2"] / base["area_um2"] if base["area_um2"] else float("nan")
        print(f"{'area ratio (cand/base)':<28} {da:>13.3f}"
              f"   {'larger' if da > 1 else 'smaller'}")
        if base.get("fmax_mhz") and cand.get("fmax_mhz"):
            df = cand["fmax_mhz"] / base["fmax_mhz"]
            print(f"{'Fmax ratio (cand/base)':<28} {df:>13.3f}"
                  f"   {'faster' if df > 1 else 'slower'}")

    out = args.out or os.path.join(
        C.RUN_DIR, f"synth-{time.strftime('%Y%m%d-%H%M%S')}.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(
        {"technology": args.tech, "clock_target_ns": args.clock_ns,
         "rows": rows}, indent=2))
    print(f"\nreport: {out}")
    return 0 if all(r.get("ok") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
