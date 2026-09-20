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
import synth_node
import synth_recipe                                                       # noqa: E402
import t0_legality as t0                                                # noqa: E402
from design_state import BASELINE, DesignState                          # noqa: E402
from metrics import parse as parse_metrics, tripwire_ok                 # noqa: E402
from pareto import (Archive, ParetoFront, Point, Verdict, admit,        # noqa: E402
                    descriptor, weights_hash)
from t1_model import Workload, predict, sram_macro_area_um2, energy_report                                  # noqa: E402

# Baseline objective values, captured on the first measured iteration and
# used only to print each later iteration's delta.
_BASELINE_SCORES = None

# Fields that live in HW_FIELDS but describe the RTL microarchitecture
# rather than a config knob -- labelled separately in the per-iteration
# proposal print so "what changed in the hardware" is unambiguous.
RTL_STATE_FIELDS = ("gate_enable", "zbu_enable", "granule_size",
                    "zbu_operand")

IMMUTABLE_FILES = ("t0_legality.py", "pareto.py", "t1_model.py",
                   "metrics.py", "kernels/spmm.c")


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


def tried_summary(history: dict, k: int = 12) -> str:
    """Every design point already spent, with the MUTATION and its result.

    The first version listed only iteration number, verdict and cycles. That
    is not enough to avoid a repeat: the agent cannot tell which lever "iter
    4: REJECT" refers to, so it re-proposes it. Run agent-1 spent 4 of 12
    iterations on DUPLICATE states -- at that rate a 15-iteration budget
    loses 5.

    So: name the field, both values, and what happened. Plus an explicit
    list of state hashes, because DUPLICATE is decided on the hash and the
    agent should be able to check its own proposal against it.
    """
    rows = [it for it in history.get("iterations", [])][-k:]
    if not rows:
        return ""
    out = []
    for it in rows:
        mut = it.get("mutation") or {}
        if mut:
            desc = ", ".join(f"{f}: {a} -> {b}" for f, (a, b) in mut.items())
        else:
            desc = "(no change -- baseline or duplicate)"
        res = it.get("verdict", "?")
        if it.get("cycles"):
            res += (f" | {it['cycles']:,} cyc"
                    f" | {it.get('energy_uJ', '?')} uJ"
                    f" | {it.get('perf_per_watt_GOPS_W', '?')} GOPS/W")
        out.append(f"  - iter {it.get('iteration')}: {desc}\n      -> {res}")
    hashes = ", ".join(sorted({it.get("state", "")[:8] for it in rows if it.get("state")}))
    return ("\n\nALREADY EVALUATED THIS RUN. Proposing any of these again is a "
            "wasted iteration -- the harness will return DUPLICATE without "
            "measuring anything:\n"
            + "\n".join(out)
            + f"\n\n  state hashes already in the archive: {hashes}\n"
            "  Before you answer, check that your proposal differs from EVERY "
            "line above. If the lever you want was already tried and REJECTED, "
            "either move a DIFFERENT lever or move the same one to a value not "
            "listed. Repeating a rejected value will not produce a new result.")


def write_status(path: Path, state: DesignState, m, verdict: str, er=None) -> None:
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
    if er is not None:
        lines += ["", "## energy / efficiency",
                  f"energy_uJ: {er.energy_pj/1e6:.3f}",
                  f"power_W: {er.power_w:.4f}",
                  f"perf_GOPS: {er.perf_gops:.3f}",
                  f"perf_per_watt_GOPS_W: {er.perf_per_watt_gops_w:.3f}",
                  f"energy_per_useful_op_pJ: {er.energy_per_useful_op_pj:.4f}",
                  f"mac_efficiency: {er.mac_efficiency:.4f}",
                  f"dram_measured: {er.dram_measured}",
                  f"gating_measured: {er.gating_measured}"]
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
    ap.add_argument("--workload", default=None,
                    help="workload stem to evaluate, e.g. dnn128 / dnn256 / "
                         "dnn512 / jag512. Overrides the baseline design "
                         "state's `workload` field. Must have a matching "
                         "workload/generated/spmm_<stem>.h")
    # --- co-design sweep knobs -------------------------------------------
    # The SOFTWARE half of Sec 9o, drivable without an agent so the coupling
    # can be measured directly. k_chunk and b_blocks are SW_FIELDS: they change
    # sw_hash but NOT hw_hash, so a sweep over them rebuilds the kernel and
    # re-simulates without paying the ~18 min elaboration -- which is what makes
    # a schedule sweep affordable and a capacity sweep not.
    ap.add_argument("--k-chunk", type=int, default=None,
                    help="K-blocks accumulated per accumulator-resident pass. "
                         "Bounded by sp_capacity_kb: a pass stages k_chunk*dim rows "
                         "of A plus k_chunk*(N/dim)*dim of B, and T0 rejects the "
                         "pair if the scratchpad cannot hold it. SOFTWARE lever.")
    ap.add_argument("--b-blocks", type=int, default=None,
                    help="B mvin width in DIM-column tiles (0 = auto). Bounded by "
                         "dma_maxbytes, which caps tiles per mvin. SOFTWARE lever.")
    ap.add_argument("--spad-kb", type=int, default=None,
                    help="scratchpad capacity in KB -- the HARDWARE half of the "
                         "k_chunk coupling. Changes hw_hash, so it forces a fresh "
                         "elaboration and synthesis.")
    ap.add_argument("--gate", action="store_true",
                    help="enable T-A zero-gated MAC (SparseCraftRTL.gateEnable). "
                         "Changes hw_hash, so it forces a fresh elaboration.")
    ap.add_argument("--zbu", action="store_true",
                    help="enable T-B zero-granule skipping (SparseCraftRTL.zbuEnable), "
                         "which instantiates the agent-owned SparseCraftZBU module from "
                         "Scratchpad.scala. The counterpart of --gate, and the only way "
                         "to exercise the ENABLED ZBU path without an agent -- needed to "
                         "prove the module is really wired in rather than dead code "
                         "(N12b RTL_NOOP). Changes hw_hash, so it forces a fresh "
                         "elaboration.")
    ap.add_argument("--dense", action="store_true",
                    help="B0 baseline: walk EVERY block including the "
                         "structurally zero ones, i.e. a GEMM that ignores "
                         "sparsity. Same instrument, one -D flag.")
    ap.add_argument("--sim-timeout", type=int, default=3600,
                    help="seconds before a Verilator run is abandoned. The "
                         "512-row workload exceeded 3600 s and was killed with "
                         "no output, so size the workload rather than raising "
                         "this blindly.")
    # T3 synthesis is ON by default (task 2.4): area and Fmax must be MEASURED
    # for every front point (G4), not taken from T1's analytical model.
    # --no-synth is the escape hatch for fast smoke runs.
    ap.add_argument("--no-synth", dest="synth", action="store_false",
                    help="skip the T3 synthesis tier and fall back to T1's "
                         "modelled area. Faster, but the run cannot support G4.")
    ap.add_argument("--synth", dest="synth", action="store_true", default=True,
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

    # Apply the CLI overrides to the baseline BEFORE anything is written or
    # hashed, so the cache key and the trace both describe what actually ran.
    global BASELINE
    if (args.workload or args.dense or args.gate or args.zbu
            or args.k_chunk is not None or args.b_blocks is not None
            or args.spad_kb is not None):
        BASELINE = BASELINE.mutate(
            **({"workload": args.workload} if args.workload else {}),
            **({"dense_mode": True} if args.dense else {}),
            **({"gate_enable": True} if args.gate else {}),
            **({"zbu_enable": True} if args.zbu else {}),
            **({"k_chunk": args.k_chunk} if args.k_chunk is not None else {}),
            **({"b_blocks": args.b_blocks} if args.b_blocks is not None else {}),
            **({"sp_capacity_kb": args.spad_kb} if args.spad_kb is not None else {}))
        print(f"baseline override: workload={BASELINE.workload} "
              f"dense_mode={BASELINE.dense_mode} gate_enable={BASELINE.gate_enable} "
              f"zbu_enable={BASELINE.zbu_enable} k_chunk={BASELINE.k_chunk} "
              f"b_blocks={BASELINE.b_blocks} spad_kb={BASELINE.sp_capacity_kb}")

    run_dir = Path(C.RUN_DIR) / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status.md"
    history_path = run_dir / "history.json"
    # Built from the generated stats after the CLI override is applied, so the
    # model, the kernel and the trace all describe the same experiment.
    _wl_json = (Path(__file__).resolve().parent / "workload" / "generated"
                / f"spmm_{BASELINE.workload}.json")
    if not _wl_json.is_file():
        raise SystemExit(f"ABORT_RUN: workload stats missing: {_wl_json}")
    workload = Workload.from_stats(json.loads(_wl_json.read_text()),
                                   dense_mode=BASELINE.dense_mode)
    print(f"workload: {workload.M}x{workload.K} x {workload.N}  "
          f"nnz={workload.nnz:,}  nz_blocks={workload.nz_blocks}  "
          f"in-block density={workload.in_block_density():.3f}  "
          f"dense_mode={workload.dense_mode}")
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

    def _is_failure(value) -> bool:
        """Is this cached value a FAILED result?

        The cache is keyed on the design, not on the outcome, so a build that
        failed for a reason since fixed -- a missing #define, a transient OOM,
        a bad path -- is otherwise served forever under the same tag. Observed
        2026-09-19: a KERNEL_BUILD_FAILED was cached, and two subsequent runs
        with the fix in place never executed build_kernel at all. The fix was
        correct; the loop simply never ran it.

        That is a silent-wrong-result generator for an unattended arm, so
        failures are never served: on a miss the node re-executes and either
        succeeds or fails again honestly.
        """
        try:
            # The cache stores (tag, payload), not the payload alone -- checking
            # the outer object silently matches nothing, which is how the first
            # version of this guard passed review and purged 0 entries.
            candidates = [value]
            if isinstance(value, tuple):
                candidates.extend(value)
            for v in candidates:
                d = v if isinstance(v, dict) else getattr(v, "__dict__", None)
                if not isinstance(d, dict):
                    continue
                if d.get("success") is False:
                    return True
                if d.get("returncode") not in (None, 0):
                    return True
        except Exception:
            return False
        return False

    def cache_provider(tag, data_path, *a, **kw):
        hit, value = get(cache.read.chia_remote(tag))
        if not hit:
            raise KeyError(f"cache miss for {tag!r}")
        if _is_failure(value):
            raise KeyError(f"cached FAILURE for {tag!r}; re-executing")
        return value

    def cache_hit(tag, data_path, *a, **kw):
        if not get(cache.has.chia_remote(tag)):
            return False
        hit, value = get(cache.read.chia_remote(tag))
        if hit and _is_failure(value):
            print(f"  cache: ignoring cached FAILURE for {tag}")
            return False
        return True

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
                     agent.make_llm("propose_rtl.md", backend=args.backend,
                                    model=args.model,
                                    log_dir=str(run_dir / "llm")))
    history = {"iterations": [], "front": []}

    prev_netlist, prev_rtl_id = None, None
    front, archive = ParetoFront(), Archive()
    parent, parent_reward, diagnosis = BASELINE, 0.0, ""
    # The parent's RTL digest, alongside its config. N21's identity is the PAIR
    # (state_hash, rtl_digest), because an RTL-only edit leaves the config
    # untouched and would otherwise read as a duplicate of its own parent.
    parent_rtl_id = None
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
        # Files the HARNESS writes into the tree each iteration. They are not
        # in the agent's writable set, so N13 must be told the harness put
        # them there -- otherwise the scope check rejects the harness's own
        # scaffolding as an out-of-scope model edit.
        harness_paths: set = {
            C.GEMMINI_PARAMS_H_REL,
            C.RTL_PARAMS_FILE_REL,
            f"{C.GEMMINI_SW_REL}/include/gemmini_counter.h",
            "generators/gemmini/src/main/scala/gemmini/CounterFile.scala",
            "generators/gemmini/src/main/scala/gemmini/ExecuteController.scala",
        }

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

        last_counters = "(no measurement yet)"

        for it in range(1, args.iters + 1):
            # A Ray OOM RAISES out of get() instead of returning a failed
            # result, so ANY heavy node -- elaborate, build_kernel,
            # simulate, synthesize -- can abort the whole run. Measured:
            # one synthesis OOM killed a 15-iteration run at iteration 1.
            # For a 10-hour unattended run that trade is unacceptable, so
            # infrastructure failure is contained to the iteration that
            # hit it: record it, print it, move to the next proposal.
            # Legality/correctness verdicts are NOT routed here -- those
            # are decided by the tiers and must keep their own verdicts.
            try:
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
                    # Every placeholder in task_rtl.md must be supplied and no
                    # extras passed -- agent.load_prompt raises on a mismatch
                    # rather than silently shipping a prompt with a literal
                    # "${PE_PATH}" in it. That guard fired on the first real agent
                    # run: the template was written with BUDGET/COUNTERS/ITERATION/
                    # PE_PATH/ZBU_PATH and this call site still passed the old
                    # HARNESS_PATH set, so iteration 2 died before any LLM call.
                    msg_text = agent.load_prompt(
                        "task_rtl.md",
                        ITERATION=str(it),
                        BUDGET=str(args.iters),
                        PARAMS_PATH=os.path.join(C.CHIPYARD_PATH, C.PARAMS_FILE_REL),
                        PE_PATH=os.path.join(C.CHIPYARD_PATH, C.RTL_FILES_REL[0]),
                        ZBU_PATH=os.path.join(C.CHIPYARD_PATH, C.RTL_FILES_REL[1]),
                        CHIPYARD=C.CHIPYARD_PATH,
                        PARENT_STATE=parent.to_json(),
                        DIAGNOSIS=diagnosis or "(first iteration - no measurement yet)",
                        COUNTERS=last_counters,
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
                # `pinned` freezes the benchmark. These two fields choose WHICH
                # matrix is simulated (data_path is built from child.workload
                # below), so leaving them free lets a proposal be measured on a
                # different problem and still scored against this baseline --
                # which run codesign15b's agent attempted on its first move.
                verdict_t0 = t0.check(child, pinned={
                    "workload": BASELINE.workload,
                    "dense_mode": BASELINE.dense_mode,
                })
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

                # ---- what the agent actually PROPOSED, field by field ------
                # The move class alone ("HW+SW") does not say what changed.
                # Print old -> new for every field, tagged by layer, plus any
                # RTL file the agent rewrote, so each iteration is readable
                # without opening the JSON or the raw LLM transcript.
                _changed = parent.diff_from(child)
                if _changed:
                    print("  " + "-" * 62)
                    print(f"  AGENT PROPOSAL (iteration {it})")
                    for _f in sorted(_changed):
                        if _f in DesignState.HW_FIELDS and _f in RTL_STATE_FIELDS:
                            _layer = "RTL"
                        elif _f in DesignState.HW_FIELDS:
                            _layer = "HW "
                        else:
                            _layer = "SW "
                        print(f"    [{_layer}] {_f:<28} "
                              f"{getattr(parent, _f)!r} -> {getattr(child, _f)!r}")
                    # RTL SOURCE edits are invisible in the state diff -- the
                    # agent rewrites Chisel directly, and that is the part of
                    # the claim that is actually about hardware.
                    _rtl_edits = [p_ for p_ in (touched or [])
                                  if p_.endswith(".scala")]
                    if _rtl_edits:
                        print(f"    [RTL] Chisel files rewritten by the agent:")
                        for _p in _rtl_edits:
                            print(f"           {_p}")
                    print("  " + "-" * 62)

                # ---- what is about to be simulated -------------------------
                print(f"  SIMULATING  workload={child.workload}"
                      f"  {workload.M}x{workload.K}x{workload.N}"
                      f"  nnz={workload.nnz:,}"
                      f"  nz_blocks={workload.nz_blocks}/{workload.total_blocks}"
                      f"  ({'dense walk' if child.dense_mode else 'sparse walk'})")
                print(f"              array={child.meshRows}x{child.meshColumns}"
                      f"  spad={child.sp_capacity_kb}KB/{child.sp_banks}b"
                      f"  acc={child.acc_capacity_kb}KB"
                      f"  tlb={child.tlb_size}"
                      f"  k_chunk={child.k_chunk} b_blocks={child.b_blocks}"
                      f"  gate={child.gate_enable} zbu={child.zbu_enable}")
                if not verdict_t0.legal:
                    print(f"  N20 T0 FAIL: {verdict_t0.violations}")
                    record["verdict"] = "T0_ILLEGAL"
                    record["violations"] = verdict_t0.violations
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = "T0 rejected: " + "; ".join(verdict_t0.violations)
                    continue
                # ---- N10 failure: the agent never got to propose ----------------
                # A failed LLM call that left the tree untouched is NOT a
                # duplicate proposal. Filing it as one tells the next iteration
                # "you already evaluated this design -- move a DIFFERENT lever",
                # which is false, and it sends the agent chasing a problem it
                # does not have. Run codesign15 (2026-09-20) lost every
                # iteration this way: vertex.py reported returncode 0 for turns
                # in which gemini-2.5-pro emitted nothing but thoughts, so each
                # no-op was filed as DUPLICATE and the run ended with the agent
                # never having proposed anything at all.
                #
                # Checked only when the tree is UNCHANGED: an agent that made a
                # real edit and then failed still has its edit evaluated, and
                # N12 gates it like any other.
                if (it > 1 and arm is None and not args.skip_llm
                        and record["move"]["n_changed"] == 0
                        and record.get("llm_returncode", 0) != 0):
                    print(f"  N10 AGENT FAILED (rc={record['llm_returncode']})"
                          f" -- no edit reached the tree; NOT a duplicate")
                    record["verdict"] = "AGENT_FAILED"
                    record["wall_clock_s"] = round(time.time() - t_start, 2)
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    get(nodes.apply_design_state.options(**pg_opts)
                        .chia_remote(json.dumps(parent.canonical())))
                    diagnosis = ("your previous turn produced no usable output, so no "
                                 "edit was made. Begin THIS turn with a tool call that "
                                 "makes one concrete edit.")
                    continue
                # ---- N21 identity: the CONFIG ALONE IS NOT THE DESIGN -----------
                # state_hash covers the dataclass fields and nothing else, but in
                # v3 the agent's main lever is Chisel. Improving the ZBU's
                # granularity or the PE's gating logic changes the hardware while
                # leaving every config field alone -- identical state_hash, so the
                # old key filed it as DUPLICATE and threw it away UNEVALUATED,
                # ninety lines before rtl_digest was even computed. That is the
                # whole v3 premise ("the agent edits Chisel") silently disabled;
                # it had not bitten yet only because the agent kept moving a
                # config field alongside, or doing nothing.
                #
                # So the identity is the PAIR. Hashing the RTL here also costs
                # nothing (five files) and makes the no-edit case detectable,
                # which the config hash alone cannot do.
                rtl_id = get(nodes.rtl_digest.options(**pg_opts).chia_remote())
                record["rtl_digest"] = rtl_id
                ident = (child.state_hash(), rtl_id)

                # An agent that reports an edit it never made. Distinct from
                # DUPLICATE ("you already tried this design") and from
                # AGENT_FAILED ("your call errored"): here the call SUCCEEDED,
                # the model answered in full -- technique, files, `compiled:
                # PASS` -- and issued no tool call at all, so not one byte
                # changed. Measured, run codesign15c 2026-09-20: three
                # consecutive iterations of fabricated work. Telling it "you
                # repeated a design" would be the wrong correction entirely.
                if (it > 1 and arm is None and not args.skip_llm
                        and ident == (parent.state_hash(), parent_rtl_id)):
                    print("  N21 NO EDIT: neither the config nor the RTL changed")
                    record["verdict"] = "NO_EDIT"
                    record["wall_clock_s"] = round(time.time() - t_start, 2)
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = (
                        "NOTHING CHANGED. Neither the config nor the RTL differs from "
                        "your parent, so whatever you reported last turn was not "
                        "actually written. The harness reads the FILES, never your "
                        "report. Call the edit tool, wait for the tool RESULT that "
                        "confirms the write, and only then describe what you did.")
                    continue

                if ident in seen:
                    print("  N21 duplicate design; asking for a different edit")
                    # Record it. A deduped iteration still consumed a turn and an
                    # LLM call, so leaving it out of the records makes the arm look
                    # more efficient than it was and hides livelocks entirely.
                    record["verdict"] = "DUPLICATE"
                    record["wall_clock_s"] = round(time.time() - t_start, 2)
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    get(nodes.apply_design_state.options(**pg_opts)
                        .chia_remote(json.dumps(parent.canonical())))
                    diagnosis = (f"design {child.state_hash()} with this RTL was ALREADY "
                                 f"EVALUATED. Do not propose it again -- change a "
                                 f"DIFFERENT lever, or change the RTL mechanism.")
                    continue
                seen.add(ident)

                # ---- make the HARDWARE match the state we just parsed ------------
                # The agent sets gate_enable / zbu_enable by editing the
                # `// SPARSECRAFT` markers, and read_design_state parses the state
                # back out of them -- but Chisel elaborates against
                # SparseCraftRTL.scala, which only apply_design_state writes, and
                # that is never called in the agent path. So the toggle reached the
                # state, the hash and the records, and never reached the hardware:
                # codesign15c iterations 5 and 6 proposed zbu_enable and
                # gate_enable and both elaborated to the BASELINE netlist
                # (6f3c5995546620a3), caught by N12b as RTL_NOOP.
                #
                # Done BEFORE the compile gate so N12 checks the real
                # configuration, and before rtl_digest is consumed -- this file is
                # in neither RTL_FILES_REL nor HARNESS_PATCHED_RTL_REL, so
                # rewriting it does not perturb that digest. hw_hash already covers
                # the toggles, so the elaboration cache key stays correct.
                _rp = get(nodes.apply_rtl_params.options(**pg_opts)
                          .chia_remote(json.dumps(child.canonical())))
                if _rp.get("changed"):
                    print(f"  N13 RTL params regenerated: gate={_rp.get('gate_enable')} "
                          f"zbu={_rp.get('zbu_enable')}")

                # ---- N12 RTL compile gate ---------------------------------------
                # 17-19 s against a ~5 min iteration. Catches the Chisel type
                # errors an LLM writing RTL will produce, before anything
                # expensive. It does NOT catch code that compiles and is wrong --
                # that is N41's job, and it is why a weak proposer is still costly.
                gate = get(nodes.rtl_compile_check.options(**pg_opts).chia_remote())
                record["n12_compile"] = {"ok": gate["ok"], "rc": gate["returncode"]}
                if not gate["ok"]:
                    print(f"  N12 COMPILE FAILED rc={gate['returncode']}")
                    print("  " + "\n  ".join(gate["errors"].splitlines()[:6]))
                    record["verdict"] = "COMPILE_FAILED"
                    record["compile_errors"] = gate["errors"]
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = ("the Chisel does not compile. Fix these before "
                                 "anything else:\n" + gate["errors"][-1500:])
                    get(nodes.apply_design_state.options(**pg_opts)
                        .chia_remote(json.dumps(parent.canonical())))
                    continue
                print("  N12 compile OK")

                # ---- N22 T1 (a FILTER; never a substitute for measurement) -------
                pred = predict(child, workload)
                record["t1_prediction"] = pred.to_dict()
                print(f"  N22 T1 predicts bound_by={pred.bound_by}  "
                      f"area={pred.area_um2:,.0f}um2")

                # ---- N30/N31 elaborate, N32 kernel, N50 simulate ----------------
                sj = json.dumps(child.canonical())
                here = Path(__file__).resolve().parent
                kernel_src = (here / "kernels" / "spmm.c").read_text()
                # The generated workload travels by VALUE: the head and the build
                # container share no filesystem. Missing is fatal and loud -- a
                # silently empty header would compile to a kernel measuring nothing.
                data_path = here / "workload" / "generated" / f"spmm_{child.workload}.h"
                if not data_path.is_file():
                    raise SystemExit(f"ABORT_RUN: workload header missing: {data_path}. "
                                     f"Generate it with workload/prep_matrices.py")
                data_header = data_path.read_text()

                # The build's identity is the BYTES THAT ENTER THE COMPILER, not a
                # field list. sw_hash() covers SW_FIELDS + hw_hash only, so editing
                # kernels/spmm.c left the tag unchanged and the loop served a STALE
                # BINARY -- while the integrity manifest correctly recorded the new
                # source hash. Measured numbers were attributed to code that never
                # ran, and every gate stayed green.
                #
                # Third instance of this class in one session (cf. rtl_digest not
                # covering harness-patched RTL, and the energy discount keying off
                # counter presence). Keying on the actual inputs is the version
                # that cannot drift: change either file and the tag moves.
                # The -D SW-schedule flags change the BINARY without changing
                # either file, so they belong in the key too -- otherwise a
                # k_chunk=16 binary would be served for a k_chunk=4 design and
                # reported as a measurement of the latter. Same class of bug
                # as the three cache holes already closed today.
                _sched = "|".join(f"{k}={getattr(child, k)}"
                                  for k in ("k_chunk", "b_blocks", "a_blocks",
                                            "dense_mode"))
                build_id = hashlib.sha256(
                    (kernel_src + "\x00" + data_header + "\x00" + _sched).encode()
                ).hexdigest()[:16]
                wl_stats = json.loads(data_path.with_suffix(".json").read_text())

                # The tag family tracks what the build actually produced. With
                # --synth the artifact also carries the generated RTL and was
                # lowered for yosys, so it is a different artifact of the same
                # design state and must not answer to a plain `hw:` lookup.
                # The elaboration cache key has THREE parts, and dropping any one
                # of them makes the loop measure the wrong hardware silently:
                #   hw_hash          the 23 typed config fields
                #   rtl              a digest of the agent-writable Chisel (2.8) --
                #                    hw_hash cannot see PE.scala, so without this an
                #                    RTL-only edit reuses the PREVIOUS build
                #   t<N>             VERILATOR_THREADS, a build-time flag that
                #                    changes the simulator binary
                # rtl_id was already computed for the N21 identity above -- the
                # tree has not been touched since, so recomputing it here would
                # only be a second hash of the same five files.
                hw_tag = ((f"hwsrc:{child.hw_hash()}" if args.synth
                           else f"hw:{child.hw_hash()}")
                          + f"+rtl{rtl_id}@t{C.VERILATOR_THREADS}")
                art = get(nodes.elaborate.options(**pg_opts)
                          .chia_remote(sj, collect_src=args.synth, _chia_tag=hw_tag))
                if not art.success:
                    print(f"  N30 ELABORATION FAILED rc={art.returncode}")
                    record["verdict"] = "ELABORATION_FAILED"
                    record["stderr_tail"] = art.stderr[-3000:]
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = f"elaboration failed:\n{art.stderr[-1500:]}"
                    continue

                # ---- N12b: the RTL must actually have reached the hardware ----
                # Keyed on hw_tag -- the SAME tag as the elaboration that produced
                # the Verilog it hashes. netlist_digest reads gen-collateral/ off
                # DISK, and that directory holds whatever was elaborated LAST. With
                # no tag it re-ran on every iteration and, whenever `elaborate` was
                # a cache hit (so nothing was re-elaborated), it hashed the
                # PREVIOUS design's Verilog. Measured: accres-b1/-b0/jag-b1 all had
                # gate_enable=False yet reported eeebfb46, the gate-ON netlist,
                # because each ran straight after a gate-ON run.
                #
                # Sharing the tag makes the digest travel with the artifact: it is
                # computed once, right after that design really elaborated, and
                # every later cache hit returns that design's own digest.
                #
                # Measurements were never wrong (simulate consumes the artifact,
                # which was cached correctly) -- but N12b, the gate whose entire
                # job is "did the RTL reach the hardware?", was reporting on the
                # wrong hardware. A stale CHECK is worse than a stale measurement.
                nl = get(nodes.netlist_digest.options(**pg_opts)
                         .chia_remote(_chia_tag=f"net:{hw_tag}"))
                record["netlist_digest"] = nl.get("digest", "")
                record["netlist_files"] = nl.get("n_files", 0)
                if nl.get("ok") and prev_netlist is not None:
                    rtl_changed = (rtl_id != prev_rtl_id)
                    net_changed = (nl["digest"] != prev_netlist)
                    if rtl_changed and not net_changed:
                        print(f"  N12b NO-OP RTL EDIT: rtl_digest changed "
                              f"{prev_rtl_id} -> {rtl_id} but the elaborated "
                              f"netlist did not ({nl['digest']}). The edit "
                              f"instantiated nothing.")
                        record["verdict"] = "RTL_NOOP"
                        (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                        diagnosis = (
                            "Your RTL edit compiled and changed the source, but the "
                            "ELABORATED HARDWARE is byte-identical to the parent's. "
                            "The mechanism was not instantiated. Common cause: the "
                            "logic sits behind a Scala `if` on a parameter that is "
                            "false, or it is dead code nothing reads. Verify the "
                            "signal you added actually drives an output.")
                        get(nodes.apply_design_state.options(**pg_opts)
                            .chia_remote(json.dumps(parent.canonical())))
                        continue
                if nl.get("ok"):
                    prev_netlist, prev_rtl_id = nl["digest"], rtl_id
                    print(f"  N12b netlist {nl['digest']} ({nl['n_files']} files)")

                kern = get(nodes.build_kernel.options(**pg_opts)
                           .chia_remote(sj, kernel_src, data_header,
                                        _chia_tag=f"sw:{child.sw_hash()}+k{build_id}"))
                if not kern["success"]:
                    print(f"  N32 KERNEL BUILD FAILED rc={kern['returncode']} "
                          f"bytes={kern.get('binary_bytes', 0)}")
                    record["verdict"] = "KERNEL_BUILD_FAILED"
                    record["stderr_tail"] = kern["stderr"][-3000:]
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = f"kernel build failed:\n{kern['stderr'][-1500:]}"
                    continue

                run = get(nodes.simulate.chia_remote(
                    art, kern, timeout_seconds=args.sim_timeout,
                    _chia_tag=f"sim:{child.sw_hash()}+k{build_id}"))
                m = parse_metrics(run.log)
                record["metrics"] = m.to_dict()
                print(f"  N50 measured cycles={m.cycles:,}  "
                      f"off-chip={m.bytes_offchip():,}B")

                # Information-theoretic floor for THIS workload: the nonzero A
                # blocks, the dense X, and the Y writeback, each read/written once.
                # Derived from the generated stats, not from an attention shape.
                min_bytes = (wl_stats["nz_blocks"] * wl_stats["dim"] ** 2
                             + wl_stats["K"] * wl_stats["N"]
                             + wl_stats["M"] * wl_stats["N"] * 4) // 4
                if not tripwire_ok(m, min_bytes):
                    print(f"  TRIPWIRE: {m.bytes_offchip()} < {min_bytes} bytes")
                    record["verdict"] = "TRIPWIRE_FAILED"
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    continue

                # ---- N41 functional equivalence: MANDATORY GATE -----------------
                # The kernel compares every output against a host-computed golden
                # and PRINTS the count; the verdict is taken here, where the agent
                # cannot reach it. A design that is fast and wrong is not a result.
                mism = m.counters.get("equiv_mismatches")
                if mism is None:
                    print("  N41 ABORT: kernel reported no equiv_mismatches counter")
                    record["verdict"] = "EQUIV_MISSING"
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = ("the kernel did not print equiv_mismatches; the "
                                 "measurement instrument is broken, not the design")
                    continue
                record["equiv_mismatches"] = mism
                if mism != 0:
                    print(f"  N41 EQUIV FAILED: {mism:,} mismatching outputs "
                          f"(first at [{m.counters.get('equiv_first_i')},"
                          f"{m.counters.get('equiv_first_j')}] "
                          f"got={m.counters.get('equiv_got')} "
                          f"want={m.counters.get('equiv_want')})")
                    record["verdict"] = "EQUIV_FAILED"
                    (run_dir / f"iter_{it:03d}.json").write_text(json.dumps(record, indent=2))
                    diagnosis = (f"WRONG ANSWER: {mism} of "
                                 f"{wl_stats['M'] * wl_stats['N']} outputs differ from the "
                                 f"golden reference. The design computed something, but not "
                                 f"Y = A*X. Fix correctness before optimising anything.")
                    continue
                print(f"  N41 equivalence OK (0 mismatches)")

                # ---- N52 T3 synthesis: area and Fmax, measured ------------------
                # Keyed on hw_hash, so a software-only mutation reuses the previous
                # synthesis instead of paying for it again. Failure here is NOT
                # fatal: the iteration falls back to T1's predicted area/period and
                # says so in the record, because a broken synthesis run is a reason
                # to lose precision, not a reason to lose the measurement N50 just
                # made.
                area_um2, period_ns, area_src = pred.area_um2, pred.period_ns, "T1_MODEL"
                if args.synth:
                  # A synthesis OOM RAISES out of get() rather than returning
                  # success=False, so the `else` branch below never sees it and the
                  # exception killed the whole run -- measured: iteration 1 of a
                  # 15-iteration run died when the node hit 28.93/30.43 GB and Ray
                  # killed the synthesize task. T3 is a precision tier, never a
                  # correctness one, so ANY failure here must degrade to T1's
                  # modelled area and let the loop continue. Losing one area number
                  # is a bad trade for losing fourteen measured iterations.
                  try:
                      # synth_recipe, NOT synth_node. synth_node stages all
                      # ~646 generated files and lets yosys parse the entire
                      # SoC before `hierarchy -top` prunes, and it does NOT
                      # blackbox the SRAMs -- so the scratchpad synthesises as
                      # FLIP-FLOPS. Measured, agent15 iteration 1: 23.6 mm2
                      # logic, 11,087,850 cells, 2,723,168 sequential -- and
                      # 256KB + 64KB of SRAM is 2,621,440 bits, which is
                      # exactly those flops. T1 modelled 1.799 mm2; 14x gap.
                      #
                      # synth_recipe walks the Gemmini module cone first
                      # (~23% of the files), blackboxes the memories and runs
                      # STA. Its own docstring says it exists to fix
                      # synth_node; loop.py was simply never switched over.
                      # Sec 9f verified the RECIPE -- the loop has been
                      # calling the other one the whole time.
                      syn = get(synth_recipe.synthesize_recipe.chia_remote(
                          dict(art.generated_src_files),
                          # synth_recipe needs a REAL module name. synth_node
                          # accepted the sentinel "auto" and resolved it via
                          # resolve_top_module(); the recipe has no such
                          # helper and fails with "no module 'auto' found in
                          # 646 files". Gemmini is the accelerator we are
                          # measuring, and it is the recipe's own default.
                          top_module=("Gemmini"
                                      if C.SYNTH_TOP_MODULE in ("auto", "", None)
                                      else C.SYNTH_TOP_MODULE),
                          clock_period_ns=args.synth_clock_ns,
                          _chia_tag=(f"synr:{child.hw_hash()}@{args.synth_tech}"
                                     f"@{args.synth_clock_ns}")))
                      record["t3_synthesis"] = {
                          "success": syn.success, "top_module": syn.top_module,
                          "technology": args.synth_tech, "area_um2": syn.area_um2,
                          "power_total_w": syn.power_total_w,
                          "cone_files": syn.cone_files,
                          "staged_files": syn.staged_files,
                          "cell_count": syn.cell_count, "seq_cells": syn.seq_cell_count,
                          "clock_target_ns": syn.clock_target_ns,
                          "worst_slack_ns": syn.worst_slack_ns,
                          "fmax_mhz": syn.fmax_mhz, "returncode": syn.returncode,
                      }
                      (run_dir / f"synth_{it:03d}.json").write_text(
                          json.dumps({**record["t3_synthesis"],
                                      "cells_by_type": syn.cells_by_type}, indent=2))
                      if syn.success:
                          # yosys area is LOGIC ONLY -- the SRAM macros are
                          # blackboxed, which is what keeps the netlist tractable.
                          # Add their datasheet area or the figure understates the
                          # design several-fold and prices the ZBU bitmap against
                          # nothing. The source label records the hybrid honestly.
                          macro_um2 = sram_macro_area_um2(child)
                          record["t3_logic_um2"] = syn.area_um2
                          record["t3_sram_macro_um2"] = macro_um2
                          area_um2 = syn.area_um2 + macro_um2
                          area_src = f"T3_{args.synth_tech}_logic+fakeram45_macros"
                          # Fmax is the achieved period, not the requested one. When
                          # STA could not produce a slack we keep the target rather
                          # than inventing one, and the source string records that.
                          if syn.fmax_mhz:
                              period_ns = 1000.0 / syn.fmax_mhz
                          else:
                              period_ns, area_src = syn.clock_target_ns, f"{area_src}_NOSTA"
                          fmax = f"{syn.fmax_mhz:.1f}MHz" if syn.fmax_mhz else "n/a"
                          print(f"  N52 T3 {args.synth_tech}: "
                                f"area={syn.area_um2:,.0f}um2 "
                                f"cells={syn.cell_count:,} fmax={fmax}")
                      else:
                          print(f"  N52 T3 FAILED rc={syn.returncode}: "
                                f"{(syn.stderr or '')[-300:]} -- falling back to T1")

                  except Exception as e:
                      # Includes ray.exceptions.OutOfMemoryError. Recorded, printed,
                      # and then the iteration carries on with T1 area.
                      msg = f"{type(e).__name__}: {e}"
                      print(f"  N52 T3 RAISED ({msg[:160]}) -- falling back to T1")
                      record["t3_exception"] = msg[:2000]
                      area_src = "T1_MODEL_after_T3_exception"
                record["area_source"] = area_src
                record["period_ns"] = period_ns
                record["area_um2"] = area_um2

                # ---- N53 energy / power / perf-per-watt -------------------------
                # Computed AFTER T3, so it uses the measured clock period when one
                # exists. DRAM energy comes from Gemmini's own byte counters, so
                # the largest term is measured rather than modelled.
                er = energy_report(child, m, workload, period_ns)
                record["energy"] = er.to_dict()
                print(f"  N53 energy={er.energy_pj/1e6:,.2f} uJ  power={er.power_w:.3f} W  "
                      f"perf={er.perf_gops:.2f} GOPS  perf/W={er.perf_per_watt_gops_w:.2f} GOPS/W")
                print(f"      mac_eff={er.mac_efficiency:.1%} "
                      f"(useful/issued)  breakdown mac/sram/dram = "
                      f"{er.breakdown['mac_pct']:.0f}/{er.breakdown['sram_pct']:.0f}/"
                      f"{er.breakdown['dram_pct']:.0f}%"
                      f"{'' if er.dram_measured else '  [DRAM MODELLED]'}"
                      f"{'' if er.gating_measured else '  [gating modelled]'}")

                # ---- N60 score + Pareto admit -----------------------------------
                # E stays T1's: there is no power tier. t and A are measured
                # whenever T3 ran, and `area_source` in the record says which.
                point = Point(state_hash=child.state_hash(), parent_hash=parent.state_hash(),
                              t=m.cycles * period_ns, E=er.energy_pj, A=area_um2,
                              sram_bytes=pred.sram_bytes,
                              # Workload is SpMM-shaped now: `density`/`n_blocks()` belonged to
                              # the retired attention Workload. The MAP-Elites niche should
                              # key on the density the RTL actually competes against --
                              # density INSIDE issued blocks -- not the whole-matrix figure.
                              descriptor=descriptor(child, workload.in_block_density(),
                                                    workload.total_blocks),
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

                # ---- per-iteration scoreboard --------------------------------
                # The three objectives plus perf/W, each against the BASELINE
                # iteration, so improvement (or its absence) is visible without
                # opening the JSON.
                global _BASELINE_SCORES
                if _BASELINE_SCORES is None:
                    _BASELINE_SCORES = {"E": er.energy_pj, "A": area_um2,
                                        "ppw": er.perf_per_watt_gops_w,
                                        "cyc": m.cycles}
                _b = _BASELINE_SCORES

                def _vs(now, base, higher_better=False):
                    if not base or not now:
                        return "      --"
                    r = (now / base) if higher_better else (base / now)
                    return f"{r:6.2f}x" + ("" if r >= 0.999 else "  WORSE")

                print("  " + "=" * 64)
                print(f"  ITERATION {it} RESULTS          value        vs baseline")
                print(f"    cycles           {m.cycles:>15,}   {_vs(m.cycles, _b['cyc'])}")
                print(f"    energy           {er.energy_pj/1e6:>12,.2f} uJ   {_vs(er.energy_pj, _b['E'])}")
                print(f"    power            {er.power_w:>13.3f} W")
                print(f"    perf/W           {er.perf_per_watt_gops_w:>9.2f} GOPS/W   "
                      f"{_vs(er.perf_per_watt_gops_w, _b['ppw'], True)}")
                print(f"    area             {area_um2/1e6:>12.3f} mm2   {_vs(area_um2, _b['A'])}"
                      f"   [{record.get('area_source', '?')}]")
                print(f"    off-chip bytes   {m.bytes_offchip():>15,}")
                print("  " + "=" * 64)

                diagnosis = diagnose(m) + tried_summary(history)
                record["diagnosis"] = diagnosis
                # Hardware counters handed to the next proposal. The agent cannot
                # read the simulator, so this is its only view of what the design
                # actually did -- keep it factual and units-explicit.
                last_counters = "\n".join([
                    f"cycles                 = {m.cycles:,}",
                    f"off_chip_bytes         = {m.bytes_offchip():,}",
                    f"macs_issued            = {m.counters.get('macs_issued', 0):,}",
                    f"macs_useful            = {m.macs_useful:,}",
                    f"MAC_GATED_TOTAL        = {m.counters.get('MAC_GATED_TOTAL', 0):,}",
                    f"exe_active_fraction    = {m.exe_active_fraction():.4f}",
                    f"RDMA_BYTES_REC         = {m.counters.get('RDMA_BYTES_REC', 0):,}",
                    f"WDMA_BYTES_SENT        = {m.counters.get('WDMA_BYTES_SENT', 0):,}",
                    f"energy_pj              = {er.energy_pj:,.0f}",
                    f"perf_per_watt_gops_w   = {er.perf_per_watt_gops_w:.3f}",
                    f"area_um2               = {area_um2:,.0f}",
                ])
                write_status(status_path, child, m, v.value, er)

                history["iterations"].append(
                    {"iteration": it, "state": child.state_hash(),
                     # The MUTATION, not just its outcome. Without this the
                     # agent is told "iter 4: REJECT (90,079 cycles)" and has
                     # no idea which lever that was, so it proposes the same
                     # one again. Measured on run agent-1: 4 of 12 iterations
                     # were DUPLICATE -- a third of the budget.
                     "mutation": {f: [getattr(parent, f), getattr(child, f)]
                                  for f in parent.diff_from(child)},
                     "cycles": m.cycles, "verdict": v.value,
                     "energy_uJ": round(er.energy_pj / 1e6, 3),
                     "power_W": round(er.power_w, 4),
                     "perf_GOPS": round(er.perf_gops, 3),
                     "perf_per_watt_GOPS_W": round(er.perf_per_watt_gops_w, 3),
                     "mac_efficiency": round(er.mac_efficiency, 4),
                     "area_um2": round(area_um2), "area_source": area_src,
                     "diagnosis": diagnosis})
                history["front"] = front.summary(10)
                history_path.write_text(json.dumps(history, indent=2))

                if v in (Verdict.ADMIT_FRONT, Verdict.ADMIT_ARCHIVE, Verdict.ADMIT_STEP):
                    parent, parent_reward = child, info.get("reward", parent_reward)
                    # The RTL travels with the config: the tree now holds this
                    # design's Chisel, so the next iteration's "did anything
                    # change?" test has to compare against THIS digest.
                    parent_rtl_id = rtl_id
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
            except (KeyboardInterrupt, SystemExit):
                # SystemExit is how N74 signals ABORT_RUN ("immutable inputs
                # changed mid-run"). That is a deliberate hard stop and must
                # NOT be contained: swallowing it turned one correct abort
                # into 14 futile iterations that each re-detected the same
                # drift. Infra failures are contained; intentional stops are
                # not.
                raise
            except BaseException as _e:          # incl. ray OutOfMemoryError
                import traceback as _tb
                _msg = f'{type(_e).__name__}: {_e}'
                print(f'  !! ITERATION {it} ABORTED -- {_msg[:200]}')
                record['verdict'] = 'INFRA_FAILURE'
                record['infra_error'] = _msg[:4000]
                record['traceback'] = _tb.format_exc()[-4000:]
                try:
                    (run_dir / f'iter_{it:03d}.json').write_text(
                        json.dumps(record, indent=2))
                except Exception:
                    pass
                diagnosis = ('previous iteration aborted on infrastructure '
                             f'failure: {_msg[:300]}')
                continue

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
