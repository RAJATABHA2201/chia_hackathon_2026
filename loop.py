"""The SparseCraft CHIA loop.

There is no simulated mode. Every result in this loop comes from a real node
execution: a real Chisel elaboration, a real Verilator run, real Gemmini
counters. The sanctioned way to skip expensive work on a rerun is CHIA's
cache + bypass keyed on `_chia_tag` -- the call still dispatches through Ray
with its real resources, but a provider serves the previously measured value.
That is the idiom every example uses; none of them fabricate results.

Graph (review Sec 3.2, tiers that run today):

    N74 integrity assert
      -> N10 propose      AGENTIC: model edits Chisel via BashTool in the build container
      -> N13 scope+diff   programmatic: changed_paths allowlist, then collect_diff
      -> N20 T0 legality  programmatic, microseconds, names the violated constraint
      -> N21 cache/dedup  _chia_tag + Bypass provider
      -> N22 T1 analytic  predicted-dominance filter (a FILTER, never a measurement)
      -> N30/N31 elaborate[key: hw_hash]
      -> N32 build kernel [key: sw_hash, which INCLUDES hw_hash]
      -> N50 T2a simulate real counters
      -> N60 Pareto admit (NOT improves?)
      -> N62 archive -> N61 diagnose -> back to N10

Run:
    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    chia up cluster.yaml
    chia job submit --working-dir . -- python loop.py --iters 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ray                                                              # noqa: E402
from ray.util.placement_group import placement_group, remove_placement_group  # noqa: E402
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy    # noqa: E402

from chia.base.ChiaFunction import get                                  # noqa: E402
from chia.base.bypass import Bypass, get_active_bypass                  # noqa: E402
from chia.base.cache import start_cache, stop_cache                     # noqa: E402
from chia.trace.profiler import start_collector, stop_collector         # noqa: E402

import agent                                                            # noqa: E402
import constants as C                                                   # noqa: E402
import diff_nodes                                                       # noqa: E402
import nodes                                                            # noqa: E402
import proposers                                                        # noqa: E402
import synth_node                                                       # noqa: E402
import t0_legality as t0                                                # noqa: E402
from design_state import BASELINE, DesignState                          # noqa: E402
from metrics import parse as parse_metrics, tripwire_ok                 # noqa: E402
from pareto import (Archive, ParetoFront, Point, Verdict, admit,        # noqa: E402
                    descriptor, weights_hash)
from t1_model import Workload, predict                                  # noqa: E402

IMMUTABLE_FILES = ("t0_legality.py", "pareto.py", "t1_model.py",
                   "metrics.py", "kernels/attn_prefill.c")


# ---- move classification: the hardware/software cost asymmetry -------------
def classify_move(parent: DesignState, child: DesignState) -> dict:
    """Which layer this iteration touched, and what that costs.

    The defining asymmetry of a hardware/software co-design loop, and the one
    thing this search has that a pure-RTL or pure-compiler search does not:

      SW_FIELDS (block_size, tile_m/n/k)  are -D flags on the kernel compile.
        Cost: recompile + simulate, ~11 min. The elaboration is reused.
      HW_FIELDS (the 20 RTL parameters)   change the generated Verilog.
        Cost: re-elaborate + recompile + simulate, ~30-50 min -- AND the
        software build is invalidated too, because elaboration emits the
        gemmini_params.h the kernel includes. That one-way dependency is why
        DesignState.sw_hash() deliberately carries hw_hash().

    Recording this per iteration is what makes the move-economics analysis
    possible: how often the agent reaches for a cheap move, whether it batches
    expensive ones, and how its distribution differs from greedy and random.
    """
    changed = parent.diff_from(child)
    hw = sorted(set(changed) & set(DesignState.HW_FIELDS))
    sw = sorted(set(changed) & set(DesignState.SW_FIELDS))
    cls = "HW+SW" if (hw and sw) else "HW" if hw else "SW" if sw else "NONE"
    return {
        "class": cls,
        "hw_fields": hw,
        "sw_fields": sw,
        "n_changed": len(changed),
        "changed": {k: [v[0], v[1]] for k, v in changed.items()},
        # True => this move cannot reuse the cached elaboration.
        "forces_elaboration": bool(hw),
    }


# ---- N74 integrity assert ------------------------------------------------
def integrity_manifest() -> dict:
    here = Path(__file__).resolve().parent
    man = {}
    for rel in IMMUTABLE_FILES:
        p = here / rel
        man[rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "MISSING"
    man["objective_weights"] = weights_hash()
    return man


def assert_integrity(baseline: dict) -> None:
    now = integrity_manifest()
    drift = {k: (baseline.get(k), v) for k, v in now.items() if baseline.get(k) != v}
    if drift:
        raise SystemExit(f"ABORT_RUN: immutable inputs changed mid-run: {drift}")


# ---- N61 diagnose: rule table first, model only on 'unclassified' --------
def diagnose(m) -> str:
    """Name the bottleneck from the counters.

    The scratchpad/accumulator wait counters and RESERVATION_STATION_FULL are
    NOT per-cycle fractions -- each one routinely exceeds the cycle count
    (measured: 2.39x, 2.49x, 2.87x of cycles on the baseline). They are
    free-running accumulations across banks and ports, so an absolute
    threshold on them is meaningless.

    The original rule fired `conflict_stall_fraction > 0.15` on a quantity
    whose value was 4.88, so it was true for EVERY design and the other three
    rules were unreachable. The diagnosis never changed, and the agent
    dutifully proposed banking changes for five of its first six moves --
    against a lever that left cycles bit-identical every time.

    So: the two counters that ARE bounded fractions (exe_active, dma_wait)
    drive the decision, and the unbounded ones are used only to attribute a
    stall between scratchpad pressure and issue-queue pressure, which is a
    comparison between like quantities and therefore sound.
    """
    dma_wait, exe = m.dma_wait_fraction(), m.exe_active_fraction()
    c = m.counters
    spad = sum(c.get(k, 0) for k in ("SCRATCHPAD_A_WAIT_CYCLE",
                                     "SCRATCHPAD_B_WAIT_CYCLE",
                                     "SCRATCHPAD_D_WAIT_CYCLE"))
    rs_full = c.get("RESERVATION_STATION_FULL_CYCLES", 0)

    if dma_wait > 0.30:
        return f"memory bound (dma_wait={dma_wait:.2f}) -- lever L5, then L1 reuse"
    if exe > 0.80:
        return f"compute bound (exe_active={exe:.2f}) -- lever L2 geometry"
    if exe < 0.40:
        # The array is idle most of the time. Attribute it by comparing the
        # two unbounded counters against EACH OTHER, never against cycles.
        if spad > rs_full:
            return (f"array idle (exe_active={exe:.2f}), scratchpad-wait dominated "
                    f"(spad={spad:,} vs rs_full={rs_full:,}) -- lever L1 tiling or "
                    f"L3 capacity. NOTE bank COUNT alone has been ineffective here.")
        return (f"array idle (exe_active={exe:.2f}), issue-queue dominated "
                f"(rs_full={rs_full:,} vs spad={spad:,}) -- lever L7 queue depths")
    return f"unclassified (exe_active={exe:.2f}, dma_wait={dma_wait:.2f})"


def tried_summary(history: dict, k: int = 6) -> str:
    """What the search has already spent turns on, and what it got.

    Without this the agent has no memory across turns: it sees one diagnosis,
    proposes the lever that diagnosis names, is rejected, sees the same
    diagnosis again and proposes the same lever. It has query_history as a
    pull tool and did not reach for it.
    """
    rows = [it for it in history.get("iterations", [])][-k:]
    if not rows:
        return ""
    out = []
    for it in rows:
        out.append(f"  - iter {it.get('iteration')}: {it.get('verdict')} "
                   f"({it.get('cycles'):,} cycles)" if it.get("cycles")
                   else f"  - iter {it.get('iteration')}: {it.get('verdict')}")
    return ("\n\nAlready evaluated this run -- do NOT repeat these:\n"
            + "\n".join(out))


def write_status(path: Path, state: DesignState, m, verdict: str) -> None:
    """Recomputed by the harness from measured results, never self-reported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Current design {state.state_hash()}", "",
             f"verdict: {verdict}", ""]
    if m:
        lines += [f"cycles: {m.cycles}", f"macs_useful: {m.macs_useful}",
                  f"off-chip bytes: {m.bytes_offchip()}",
                  f"exe_active_fraction: {m.exe_active_fraction():.3f}",
                  f"dma_wait_fraction: {m.dma_wait_fraction():.3f}",
                  f"conflict_stall_fraction: {m.conflict_stall_fraction():.3f}"]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--run-name", default=time.strftime("run-%Y%m%d-%H%M%S"))
    ap.add_argument("--seed-diff", help="resume from a previously collected diff.json")
    ap.add_argument("--proposer", default="agent",
                    choices=("agent", "random", "greedy"),
                    help="who picks the next design. 'agent' is the LLM; "
                         "'random' and 'greedy' are the non-agentic control "
                         "arms (proposers.py) and use no model at all. All "
                         "three face the identical harness, baseline, T0 rules "
                         "and budget -- only the chooser differs.")
    ap.add_argument("--skip-llm", action="store_true",
                    help="re-run the harness against the CURRENT tree without an "
                         "agentic turn (real nodes, no fabricated results)")
    ap.add_argument("--backend", default=None,
                    help="LLM backend (gemini, vertex, openai, anthropic, "
                         "openrouter, groq, opencode). Default: "
                         "$SPARSECRAFT_LLM_BACKEND, else gemini")
    ap.add_argument("--model", default=None,
                    help="model id; default is the backend's own default")
    ap.add_argument("--synth", action="store_true",
                    help="run the T3 tier: synthesize each design with "
                         "hammer+yosys and score it on MEASURED area and Fmax "
                         "instead of T1's analytical ones. Needs a worker "
                         "advertising the `hammer` resource (see cluster.yaml). "
                         "Adds ~5-20 min per unique hardware state and forces "
                         "elaboration to re-run with yosys-readable lowering.")
    ap.add_argument("--synth-tech", default=C.SYNTH_TECHNOLOGY,
                    help=f"technology for T3 (default {C.SYNTH_TECHNOLOGY})")
    ap.add_argument("--synth-clock-ns", type=float, default=C.SYNTH_CLOCK_NS,
                    help="clock target abc optimizes to and STA measures slack "
                         "against; HELD CONSTANT across iterations so Fmax "
                         "stays comparable")
    args = ap.parse_args()

    # Fail on the credential NOW, not 40 minutes from now. An elaboration is
    # the most expensive thing in this loop and there is no reason to pay for
    # one before knowing the model can be reached.
    # A control arm has no model to reach, so this check must not gate it.
    # Set here rather than where the arm is constructed, which is after this
    # point: otherwise --proposer greedy aborts on a missing credential it was
    # never going to use.
    if args.proposer != "agent":
        args.skip_llm = True
    if not args.skip_llm:
        try:
            info = agent.describe(args.backend)
        except ValueError as e:
            print(f"ABORT: {e}")
            return 2
        if not info["ready"]:
            missing = ("no credential: set "
                       + " or ".join(info["searched"])) if not info["credential_present"] \
                else f"the `{info['package']}` package is not importable"
            print(f"ABORT: backend {info['backend']!r} is not ready -- {missing}")
            print(f"       {info['credential_hint']}")
            print(f"       check with: python check_llm.py --list")
            print(f"       or run without a model:  --skip-llm")
            return 2
        print(f"proposer: {info['backend']}/{info['model']}"
              f"  credential={info['credential_var']}")
    else:
        print("proposer: DISABLED (--skip-llm); the tree is evaluated as-is")

    run_dir = Path(C.RUN_DIR) / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status.md"
    history_path = run_dir / "history.json"
    workload = Workload()
    manifest = integrity_manifest()

    runtime_env = C.runtime_env()
    if "RAY_JOB_CONFIG_JSON_ENV_VAR" in os.environ:
        runtime_env = {k: v for k, v in runtime_env.items() if k != "working_dir"}
    ray.init(address=os.environ.get("RAY_ADDRESS", "auto"),
             runtime_env=runtime_env, ignore_reinit_error=True)
    start_collector(log_dir=str(run_dir / "profile"))

    cfg = str(Path(__file__).resolve().parent / "bypass_cache.yaml")
    cache = start_cache(size=32, units="GB", cache_dir_path=C.CACHE_DIR, yaml_path=cfg)
    Bypass(yaml_path=cfg)

    def cache_provider(tag, data_path, *a, **kw):
        hit, value = get(cache.read.chia_remote(tag))
        if not hit:
            raise KeyError(f"cache miss for {tag!r}")
        return value

    def cache_hit(tag, data_path, *a, **kw):
        return get(cache.has.chia_remote(tag))

    for fn in ("elaborate", "build_kernel", "simulate", "synthesize"):
        get_active_bypass().set_provider(fn, cache_provider)
        get_active_bypass().set_cond(fn, cache_hit)

    # One placement group per pipeline: the editor BashTool and every
    # build/diff/reset node share ONE container exclusively, so the tree the
    # model edits is the tree that gets elaborated.
    pg = placement_group([{"CPU": 1, C.R_CHIPYARD: 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    pg_opts = {"scheduling_strategy": PlacementGroupSchedulingStrategy(
        placement_group=pg, placement_group_bundle_index=0)}

    editor = agent.make_editor(pg_opts)
    sealed = agent.make_sealed_tools(str(status_path), str(history_path))
    tools = [editor] + sealed

    # Control arms (proposers.py) use no model at all. Constructing the LLM
    # for them would burn a credential check and a provider handshake for
    # nothing, so the arm decides whether there is an agent.
    arm = None if args.proposer == "agent" else proposers.make_proposer(
        args.proposer, seed=args.seed)
    if arm is not None:
        args.skip_llm = True
        print(f"proposer: {arm.name.upper()} control arm (no LLM), seed={args.seed}")

    implement_llm = (None if args.skip_llm else
                     agent.make_llm("propose.md", backend=args.backend,
                                    model=args.model,
                                    log_dir=str(run_dir / "llm")))
    history = {"iterations": [], "front": []}

    front, archive = ParetoFront(), Archive()
    parent, parent_reward, diagnosis = BASELINE, 0.0, ""
    base_point = None
    seen = set()

    try:
        # Reset the container to a known tree, optionally seeding a prior diff.
        seed = json.loads(Path(args.seed_diff).read_text()) if args.seed_diff else {}
        err, msg = get(diff_nodes.reset_and_apply_diff.options(**pg_opts)
                       .chia_remote(seed))
        print(f"tree reset: {msg}")
        if err:
            return 1

        # Seed the baseline params file if the reset tree does not have one.
        #
        # The loop's write path is the model: N13 never renders the design
        # state, it reads back whatever the model wrote. That is correct, but
        # it leaves a hole at iteration zero -- on a fresh container
        # SparseCraftParams.scala does not exist, so SparseCraftConfig cannot
        # elaborate and the run dies on the first build with a Scala error that
        # says nothing about the real cause. Worse, it makes --skip-llm
        # impossible: there is no tree to evaluate.
        #
        # Writing the BASELINE once, and only when the file is absent, fixes
        # both without touching the model's authority over it: any subsequent
        # iteration reads back what the model wrote, exactly as before.
        # gemmini_params.h is EMITTED BY ELABORATION into the nested
        # gemmini-rocc-tests submodule (constants.py:GEMMINI_PARAMS_H_REL), so it
        # is dirty on every iteration through no action of the model's. Excused
        # by exact name, not by ignoring the submodule: with SUBMODULES now
        # recursing into it, any OTHER file the model touches in there is still
        # reported per-path and still rejected.
        harness_paths: set = {C.GEMMINI_PARAMS_H_REL}

        def ensure_baseline() -> None:
            """Write the baseline params + harness config if the tree lacks them.

            Called after every reset, not just once: a scope violation or a T0
            rejection resets the tree back to the pinned commit, which deletes
            both files. Without re-seeding, iteration N+1 would elaborate a
            config that no longer exists.

            Paths written here are recorded as HARNESS paths, so N13 can tell
            scaffolding the harness created from an edit the model made outside
            its one writable file.
            """
            if get(nodes.read_design_state.options(**pg_opts).chia_remote()):
                return
            seeded = get(nodes.apply_design_state.options(**pg_opts)
                         .chia_remote(json.dumps(BASELINE.canonical())))
            harness_paths.update(seeded.wrote)
            print(f"  seeded baseline: {seeded.message}")

        ensure_baseline()

        # A control arm needs to know whether its last proposal was admitted.
        # Reported at the START of the next iteration rather than at each exit
        # point: an iteration can leave via five different early `continue`s
        # (scope, T0, duplicate, elaboration, kernel, tripwire) and an arm that
        # missed any of them would stall waiting for a verdict that never came.
        # Not-admitted is therefore the default, and only N60 overrides it.
        last_admitted, last_reward = False, None

        for it in range(1, args.iters + 1):
            t_start = time.time()
            if arm is not None and it > 2:
                # it==2 is the arm's FIRST proposal; there is nothing to report
                # before it, since iteration 1 is the baseline.
                arm.observe(last_reward, last_admitted)
                last_admitted, last_reward = False, None
            assert_integrity(manifest)
            print(f"\n{'='*70}\niteration {it}  parent={parent.state_hash()}  "
                  f"front={len(front.points)}  niches={archive.occupancy()}")

            record = {"iteration": it, "manifest": manifest,
                      "parent_hash": parent.state_hash(),
                      "weights_hash": weights_hash(), "seed": args.seed}

            # ---- N10: whoever is choosing, the design lands in the tree ----
            # A control arm writes its state through apply_design_state -- the
            # same node that renders the agent's -- so every arm goes through
            # the identical scope check, T0 gate and measurement path. The only
            # difference between arms is who picked the state.
            # Iteration 1 measures the UNMUTATED baseline, for every arm.
            #
            # base_point -- the hypervolume reference -- is set from the first
            # measured design (see N60 below). Letting a proposer move first
            # means each arm is scored against its own opening proposal, so the
            # convergence curves are measured from different origins and cannot
            # be compared. The review fixes the reference to the baseline SoC
            # measurement x0 (Sec 3.3 Step 3); this is that.
            #
            # It costs one evaluation, and after the first run the whole
            # baseline chain is a cache hit, so the cost is seconds.
            if it == 1:
                print("  N10 baseline iteration (no proposal) -- fixes the "
                      "hypervolume reference for every arm")
            elif arm is not None:
                proposed = arm.propose(parent)
                record["proposed_hash"] = proposed.state_hash()
                get(nodes.apply_design_state.options(**pg_opts)
                    .chia_remote(json.dumps(proposed.canonical())))
            elif not args.skip_llm:
                msg_text = agent.load_prompt(
                    "task.md",
                    PARAMS_PATH=os.path.join(C.CHIPYARD_PATH, C.PARAMS_FILE_REL),
                    HARNESS_PATH=os.path.join(C.CHIPYARD_PATH, C.HARNESS_FILE_REL),
                    CHIPYARD=C.CHIPYARD_PATH,
                    PARENT_STATE=parent.to_json(),
                    DIAGNOSIS=diagnosis or "(first iteration - no measurement yet)",
                )
                cli = get(implement_llm.prompt.options(**C.LLM_OPTS)
                          .chia_remote(implement_llm, msg_text, tools))
                (run_dir / f"llm_{it:03d}.md").write_text(cli.result or "")
                record["llm_returncode"] = cli.returncode

            # ---- N13 scope check, THEN collect the diff ---------------------
            touched = get(diff_nodes.changed_paths.options(**pg_opts).chia_remote())
            scope = t0.check_patch_scope(touched, harness_paths=harness_paths)
            record["touched_paths"] = touched
            if not scope.legal:
                print(f"  N13 SCOPE VIOLATION: {scope.violations}")
                record["verdict"] = "SCOPE_VIOLATION"
                (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                get(diff_nodes.reset_and_apply_diff.options(**pg_opts).chia_remote({}))
                ensure_baseline()
                diagnosis = f"REJECTED: {scope.violations[0]}"
                continue

            err, diff = get(diff_nodes.collect_diff.options(**pg_opts).chia_remote())
            # Persist the diff IMMEDIATELY -- it is the only thing that survives
            # the container.
            (run_dir / f"diff_{it:03d}.json").write_text(json.dumps(diff, indent=2))
            record["diff_bytes"] = sum(len(v) for v in diff.values())

            # ---- N20 T0 legality --------------------------------------------
            # Read the state back from the TREE, not from the diff.
            # collect_diff returns {repo_path: diff_text}; state_from_tree
            # wants parsed Scala fields. Passing the former made from_dict
            # ignore every key and hand back the DEFAULT DesignState -- i.e.
            # the baseline -- so every iteration was scored as the baseline no
            # matter what had actually been written. Invisible under
            # --skip-llm, where the tree really is the baseline.
            parsed = get(nodes.read_design_state.options(**pg_opts).chia_remote())
            child = nodes.state_from_tree(parsed) or parent
            verdict_t0 = t0.check(child)
            record["state_hash"] = child.state_hash()
            # The FULL design state, not just its hash. Post-hoc work -- T3
            # synthesis of the final front, or re-measuring a point months
            # later -- needs the parameters themselves, and a hash cannot be
            # inverted. Reconstructing them from the diff files is possible
            # but fiddly, and one dict per iteration costs nothing.
            record["state"] = child.canonical()
            record["hw_hash"] = child.hw_hash()
            record["move"] = classify_move(parent, child)
            print(f"  N13 move={record['move']['class']}"
                  f"  hw={record['move']['hw_fields']}"
                  f"  sw={record['move']['sw_fields']}")
            if not verdict_t0.legal:
                print(f"  N20 T0 FAIL: {verdict_t0.violations}")
                record["verdict"] = "T0_ILLEGAL"
                record["violations"] = verdict_t0.violations
                (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                diagnosis = "T0 rejected: " + "; ".join(verdict_t0.violations)
                continue
            if child.state_hash() in seen:
                print("  N21 duplicate design; asking for a different edit")
                # Record it. A deduped iteration still consumed a turn and an
                # LLM call, so leaving it out of the records makes the arm look
                # more efficient than it was and hides livelocks entirely.
                record["verdict"] = "DUPLICATE"
                record["wall_clock_s"] = round(time.time() - t_start, 2)
                (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                get(nodes.apply_design_state.options(**pg_opts)
                    .chia_remote(json.dumps(parent.canonical())))
                diagnosis = (f"design {child.state_hash()} was ALREADY EVALUATED. "
                             f"Do not propose it again -- change a DIFFERENT lever.")
                continue
            seen.add(child.state_hash())

            # ---- N22 T1 (a FILTER; never a substitute for measurement) -------
            pred = predict(child, workload)
            record["t1_prediction"] = pred.to_dict()
            print(f"  N22 T1 predicts bound_by={pred.bound_by}  "
                  f"area={pred.area_um2:,.0f}um2")

            # ---- N30/N31 elaborate, N32 kernel, N50 simulate ----------------
            sj = json.dumps(child.canonical())
            kernel_src = (Path(__file__).resolve().parent / "kernels"
                          / "attn_prefill.c").read_text()

            # The tag family tracks what the build actually produced. With
            # --synth the artifact also carries the generated RTL and was
            # lowered for yosys, so it is a different artifact of the same
            # design state and must not answer to a plain `hw:` lookup.
            hw_tag = (f"hwsrc:{child.hw_hash()}" if args.synth
                      else f"hw:{child.hw_hash()}")
            art = get(nodes.elaborate.options(**pg_opts)
                      .chia_remote(sj, collect_src=args.synth, _chia_tag=hw_tag))
            if not art.success:
                print(f"  N30 ELABORATION FAILED rc={art.returncode}")
                record["verdict"] = "ELABORATION_FAILED"
                record["stderr_tail"] = art.stderr[-3000:]
                (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                diagnosis = f"elaboration failed:\n{art.stderr[-1500:]}"
                continue

            kern = get(nodes.build_kernel.options(**pg_opts)
                       .chia_remote(sj, kernel_src, _chia_tag=f"sw:{child.sw_hash()}"))
            if not kern["success"]:
                print(f"  N32 KERNEL BUILD FAILED rc={kern['returncode']}")
                record["verdict"] = "KERNEL_BUILD_FAILED"
                record["stderr_tail"] = kern["stderr"][-3000:]
                (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                diagnosis = f"kernel build failed:\n{kern['stderr'][-1500:]}"
                continue

            run = get(nodes.simulate.chia_remote(art, kern,
                                                 _chia_tag=f"sim:{child.sw_hash()}"))
            m = parse_metrics(run.log)
            record["metrics"] = m.to_dict()
            print(f"  N50 measured cycles={m.cycles:,}  "
                  f"off-chip={m.bytes_offchip():,}B")

            min_bytes = workload.seq_len * workload.d_head * 3
            if not tripwire_ok(m, min_bytes):
                print(f"  TRIPWIRE: {m.bytes_offchip()} < {min_bytes} bytes")
                record["verdict"] = "TRIPWIRE_FAILED"
                (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                continue

            # ---- N52 T3 synthesis: area and Fmax, measured ------------------
            # Keyed on hw_hash, so a software-only mutation reuses the previous
            # synthesis instead of paying for it again. Failure here is NOT
            # fatal: the iteration falls back to T1's predicted area/period and
            # says so in the record, because a broken synthesis run is a reason
            # to lose precision, not a reason to lose the measurement N50 just
            # made.
            area_um2, period_ns, area_src = pred.area_um2, pred.period_ns, "T1_MODEL"
            if args.synth:
                syn = get(synth_node.synthesize.chia_remote(
                    art.generated_src_files,
                    top_module=C.SYNTH_TOP_MODULE,
                    clock_period_ns=args.synth_clock_ns,
                    technology=args.synth_tech,
                    _chia_tag=(f"syn:{child.hw_hash()}@{args.synth_tech}"
                               f"@{args.synth_clock_ns}")))
                record["t3_synthesis"] = {
                    "success": syn.success, "top_module": syn.top_module,
                    "technology": syn.technology, "area_um2": syn.area_um2,
                    "cell_count": syn.cell_count, "seq_cells": syn.seq_cell_count,
                    "clock_target_ns": syn.clock_target_ns,
                    "worst_slack_ns": syn.worst_slack_ns,
                    "fmax_mhz": syn.fmax_mhz, "returncode": syn.returncode,
                }
                (run_dir / f"synth_{it:03d}.json").write_text(
                    json.dumps({**record["t3_synthesis"],
                                "reports": syn.reports,
                                "cells_by_type": syn.cells_by_type}, indent=2))
                if syn.success:
                    area_um2, area_src = syn.area_um2, f"T3_{syn.technology}"
                    # Fmax is the achieved period, not the requested one. When
                    # STA could not produce a slack we keep the target rather
                    # than inventing one, and the source string records that.
                    if syn.fmax_mhz:
                        period_ns = 1000.0 / syn.fmax_mhz
                    else:
                        period_ns, area_src = syn.clock_target_ns, f"{area_src}_NOSTA"
                    fmax = f"{syn.fmax_mhz:.1f}MHz" if syn.fmax_mhz else "n/a"
                    print(f"  N52 T3 {syn.technology}: "
                          f"area={syn.area_um2:,.0f}um2 "
                          f"cells={syn.cell_count:,} fmax={fmax}")
                else:
                    print(f"  N52 T3 FAILED rc={syn.returncode}: "
                          f"{(syn.stderr or '')[-300:]} -- falling back to T1")
            record["area_source"] = area_src
            record["period_ns"] = period_ns
            record["area_um2"] = area_um2

            # ---- N60 score + Pareto admit -----------------------------------
            # E stays T1's: there is no power tier. t and A are measured
            # whenever T3 ran, and `area_source` in the record says which.
            point = Point(state_hash=child.state_hash(), parent_hash=parent.state_hash(),
                          t=m.cycles * period_ns, E=pred.energy_pj, A=area_um2,
                          sram_bytes=pred.sram_bytes,
                          descriptor=descriptor(child, workload.density, workload.n_blocks()),
                          cost=time.time() - t_start)
            if base_point is None:
                base_point = point
                front.admit(point)
                archive.offer(point, 0.0)
                v, info = Verdict.ADMIT_FRONT, {"reward": 0.0, "baseline": True}
            else:
                v, info = admit(point, front, archive, base_point,
                                parent_reward=parent_reward, iteration=it,
                                budget=args.iters)
            record["verdict"] = v.value
            record["admit_info"] = info
            print(f"  N60 {v.value}  front={len(front.points)}  "
                  f"niches={archive.occupancy()}")

            diagnosis = diagnose(m) + tried_summary(history)
            record["diagnosis"] = diagnosis
            write_status(status_path, child, m, v.value)

            history["iterations"].append(
                {"iteration": it, "state": child.state_hash(),
                 "cycles": m.cycles, "verdict": v.value, "diagnosis": diagnosis})
            history["front"] = front.summary(10)
            history_path.write_text(json.dumps(history, indent=2))

            if v in (Verdict.ADMIT_FRONT, Verdict.ADMIT_ARCHIVE, Verdict.ADMIT_STEP):
                parent, parent_reward = child, info.get("reward", parent_reward)
                last_admitted, last_reward = True, info.get("reward")
            else:
                # Roll the TREE back to `parent`. Without this the tree keeps
                # the rejected design while `parent` does not, so the next
                # proposer is told one state and edits another. For the agent
                # that livelocks: it is shown the baseline, re-proposes the
                # change the tree already contains, the readback returns the
                # rejected state, and N21 dedups it -- forever. Observed on
                # agent-1, which spent iterations 2-8 on sp_banks and recorded
                # nothing after the first.
                get(nodes.apply_design_state.options(**pg_opts)
                    .chia_remote(json.dumps(parent.canonical())))

            record["wall_clock_s"] = round(time.time() - t_start, 2)
            (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))

        print(f"\n{'='*70}\nfinal Pareto front ({len(front.points)}):")
        for p in front.summary(10):
            print(f"  {p}")
        if base_point:
            print(f"hypervolume vs baseline: {front.hypervolume(base_point):.4g}")
        print(f"traces: {run_dir}")
        return 0

    finally:
        for t in tools:
            try:
                t.stop()
            except Exception:
                pass
        try:
            remove_placement_group(pg)
        except Exception:
            pass
        stop_cache()
        stop_collector()


if __name__ == "__main__":
    raise SystemExit(main())
