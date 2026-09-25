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
      -> N41 functional equivalence against the golden Y = A*X
      -> N60 Pareto admit (NOT improves?)
      -> N62 archive -> N61 diagnose -> back to N10

    Any gate from N13 to N41 that fails on the agent arm goes to N71-N73:
      N71 classify (recovery.classify; infra never reaches a model)
      -> N73 repair turn (system/repairer.md + task/repair.md)
      -> re-run the WHOLE ladder from N13 on the repaired tree
      -> repeat until it passes, the repairer says NOT_ACTIONABLE, a revert
         is detected, or the budget (--repair-budget, per-class caps) runs out.
    A failure that stands is recorded once and the tree is rolled back EXACTLY
    to the parent (rollback_to_parent: reset + re-apply the parent's diff).

Run (from the sparsecraft-v2 root; scripts/run.sh does all of this):
    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    chia up configs/cluster.yaml
    python -u src/loop.py --iters 5
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
import candidates                                                       # noqa: E402
import constants as C                                                   # noqa: E402
import diff_nodes                                                       # noqa: E402
import nodes                                                            # noqa: E402
import proposers                                                        # noqa: E402
import synth_node
import synth_recipe                                                       # noqa: E402
import t0_legality as t0                                                # noqa: E402
import recovery                                                         # noqa: E402
from dataclasses import dataclass, field, fields as dc_fields  # noqa: E402
from design_state import BASELINE, DesignState                          # noqa: E402
from metrics import Metrics, MetricsError, parse as parse_metrics, tripwire_ok  # noqa: E402
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

# Relative to C.PACKAGE_DIR. The scorer, the legality rules, the metric parser
# and the kernel: a number the agent could edit is a number it could fake.
IMMUTABLE_FILES = ("src/t0_legality.py", "src/pareto.py", "src/t1_model.py",
                   "src/metrics.py", "kernels/spmm.c")


# ---- the gate ladder's two outcomes ------------------------------------------
#
# N13 -> N41 used to be straight-line code inside the iteration, and every gate
# failure ended it with `continue`. That made the ladder impossible to RE-ENTER:
# after a repair turn edits the tree, every gate has to run again from the scope
# check down (a compile fix can break scope, a T0 fix changes the hardware hash),
# and there was no way back to the top except the next iteration.
#
# So the ladder is one function, `evaluate_tree`, and a gate failure is a value
# it RETURNS. The caller decides what a failure means -- repair it, or record it
# and roll back -- in exactly one place, instead of twelve.
@dataclass
class GateFail:
    """A gate stopped the ladder. Everything the repairer or the record needs."""
    verdict: str
    child: object = None              # the parsed DesignState, if the gate got that far
    stderr: str = ""                  # tool output, for classify() and the evidence
    violations: list = None           # T0 violations, verbatim
    shrunk: object = None             # recovery.ShrunkCase for a divergence
    evidence: str = ""                # any further gate-specific detail
    diagnosis: str = ""               # what the PROPOSER is told if this stands
    extra: dict = field(default_factory=dict)   # record fields to set on finalize
    repairable: bool = True           # False: a proposal-policy verdict, never repaired


@dataclass
class GatePass:
    """Every gate passed; what the rest of the iteration consumes."""
    child: object
    m: object
    pred: object
    art: object
    rtl_id: str
    diff: dict
    hw_tag: str = ""


def synth_tag(child, rtl_id: str, tech: str, clock_ns: float, activity) -> str:
    """Cache tag for N52. The RTL digest is IN it: hw_hash cannot see an
    RTL-only edit, so without it such an edit would answer to its parent's
    synthesis the moment a provider is ever registered for this node."""
    act = f"a{activity:.6f}" if activity is not None else "adefault"
    return f"synr:{child.hw_hash()}+rtl{rtl_id}@{tech}@{clock_ns}@{act}"


def synth_kwargs(art, args, activity) -> dict:
    """Arguments for synth_recipe.synthesize_recipe, in one place so the
    parallel and the sequential dispatch cannot drift apart."""
    return dict(
        # synth_recipe needs a REAL module name; "auto" was synth_node's sentinel.
        top_module=("Gemmini" if C.SYNTH_TOP_MODULE in ("auto", "", None)
                    else C.SYNTH_TOP_MODULE),
        clock_period_ns=args.synth_clock_ns,
        activity=activity)


# N52 runs IN PARALLEL with the simulation unless measured power is being scored.
#
# Synthesis needs the elaborated sources and nothing from the simulation except
# the switching activity, and that feeds only OpenSTA POWER -- recorded, never
# scored (energy is T1_MODEL; see SPARSECRAFT_ENERGY_SOURCE). Area and cell
# counts do not depend on it. So it can start the moment elaboration passes, in
# the hammer container, while the verilator container simulates. V1 ran them
# back to back: 26.6 min of simulation, then 8.1 min of synthesis with every
# other container idle -- 18% of the run's wall clock spent waiting on nothing.
#
# With SPARSECRAFT_ENERGY_SOURCE=measured the power number IS scored, so it must
# be annotated with this design's measured activity, and synthesis stays after
# the simulation as before.
def synth_in_parallel() -> bool:
    return (os.environ.get("SPARSECRAFT_ENERGY_SOURCE", "model").lower() != "measured"
            and os.environ.get("SPARSECRAFT_SYNTH_PARALLEL", "1") not in ("0", "false", "no"))


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
    """Hash every immutable input. A MISSING file is fatal, not a hash.

    It used to record the string "MISSING" and carry on. After a file moved,
    both the run-start manifest and every per-iteration check then read
    "MISSING", compared equal, and the integrity check passed while guarding
    nothing -- the failure mode a reorganisation produces silently.
    """
    root = Path(C.PACKAGE_DIR)
    man = {}
    for rel in IMMUTABLE_FILES:
        p = root / rel
        if not p.is_file():
            raise SystemExit(f"ABORT_RUN: immutable input missing: {p}")
        man[rel] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
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
    # Reached only with dma_wait <= 0.30 and 0.40 <= exe_active <= 0.80: the
    # array is neither starved nor saturated. This used to return
    # "unclassified", which named nothing -- and V1's search lived here from
    # iteration 5 to 15 (exe_active ~0.75, dma_wait ~0.01), so the proposer
    # got no actionable diagnosis for most of the run and spent iteration 7 on
    # a bank change that left cycles bit-identical.
    return (f"balanced, no single bottleneck (exe_active={exe:.2f}, "
            f"dma_wait={dma_wait:.2f}): the array is neither starved nor "
            f"saturated, so resizing a resource that already fits will not move "
            f"cycles. Change the MECHANISM (T-A / T-B scope, granularity, operand), "
            f"or remove capacity the software schedule no longer needs")


# --- N61b: the diagnosis, as a LABEL the harness can act on -----------------
#
# `diagnose` returns prose for the agent to read. That is right for a prompt
# and useless for a decision: nothing downstream can branch on a sentence.
# This returns the same judgement as a typed label, from the SAME rules, so
# the two can never disagree -- the label is derived from the prose, not
# computed a second time.
#
# What it buys: the strategy modules are SELECTED rather than concatenated.
# All of them together are ~40 KB of technique text on every turn, most of it
# irrelevant to the bottleneck actually in front of the agent. Loading the two
# that match keeps the prompt short and, more importantly, keeps the long
# cacheable prefix stable while only the short tail moves.
BOTTLENECK_STRATEGY = {
    # dram-bound: the fetch is the cost. Software schedule first -- it is a
    # third the wall clock -- then the granule skip that avoids reads.
    "memory":       ["strategy/dataflow-tiling.md",
                     "strategy/t-b-zero-granule-skip.md"],
    # compute-bound: the array is saturated, so the multiplier is the target.
    "compute":      ["strategy/t-a-zero-gated-mac.md",
                     "strategy/nm-structured.md"],
    # array idle, scratchpad-wait dominated: capacity/tiling, not the mesh.
    "scratchpad":   ["strategy/resource-sizing.md",
                     "strategy/dataflow-tiling.md"],
    # array idle, issue-queue dominated: queue depths are the only lever that
    # addresses it, and resource-sizing carries the "banks have never worked"
    # warning that has cost this loop several iterations.
    "issue_queue":  ["strategy/resource-sizing.md"],
    # No single bottleneck (exe_active 0.40-0.80, no DMA wait). Resizing what
    # already fits is how iterations get wasted here, so the mechanism modules
    # lead; resource-sizing carries the "what no clear bottleneck means" and
    # "shrinking is a move" guidance written for exactly this regime.
    "balanced":     ["strategy/t-a-zero-gated-mac.md",
                     "strategy/t-b-zero-granule-skip.md",
                     "strategy/resource-sizing.md",
                     "strategy/dataflow-tiling.md"],
    # Fallback for a diagnosis the rules above did not produce (e.g. none yet).
    "unclassified": ["strategy/t-a-zero-gated-mac.md",
                     "strategy/t-b-zero-granule-skip.md",
                     "strategy/dataflow-tiling.md"],
}

# Already included statically by system/microarchitect.md. They are the
# sanctioned techniques, so the proposer always has them; the work order adds
# only the modules the current bottleneck calls for ON TOP of these, and never
# repeats one.
STRATEGY_IN_SYSTEM = frozenset({"strategy/t-a-zero-gated-mac.md",
                                "strategy/t-b-zero-granule-skip.md"})


def bottleneck_label(diag: str) -> str:
    """Map `diagnose`'s prose to one of BOTTLENECK_STRATEGY's keys."""
    d = (diag or "").lower()
    if d.startswith("memory bound"):
        return "memory"
    if d.startswith("compute bound"):
        return "compute"
    if "scratchpad-wait dominated" in d:
        return "scratchpad"
    if "issue-queue dominated" in d:
        return "issue_queue"
    if d.startswith("balanced"):
        return "balanced"
    return "unclassified"


def strategy_for(diag: str) -> list:
    """Which strategy modules this iteration's bottleneck calls for."""
    return BOTTLENECK_STRATEGY.get(bottleneck_label(diag),
                                   BOTTLENECK_STRATEGY["unclassified"])


def strategy_section(diag: str) -> str:
    """The ${STRATEGY} block of the proposer's work order.

    Selected, not concatenated: all five modules are ~12 KB and most of it is
    irrelevant to the bottleneck in front of the agent. It goes in the WORK
    ORDER rather than the system prompt so the system prompt stays byte-stable
    (and cached) for the whole run; only this block moves, and only when the
    diagnosed bottleneck does.
    """
    label = bottleneck_label(diag)
    mods = [m for m in strategy_for(diag) if m not in STRATEGY_IN_SYSTEM]
    if not mods:
        return ""
    body = "\n\n".join(agent.read_prompt(m) for m in mods)
    return (f"## Levers for this bottleneck: `{label}`\n\n"
            "Selected by the harness from the last MEASURED diagnosis. T-A and "
            "T-B, in your system prompt, remain the primary target. These are "
            "the co-design levers this bottleneck points at: use one when the "
            "counters say it is the binding constraint, or as the coupled half of "
            "a sparsity move (the resource a mechanism frees, or the one it "
            "needs). Say which counter you expect it to move.\n\n" + body)


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
        elif it.get("reason"):
            # A design that FAILED a gate. It is listed so it is never
            # re-proposed: V2's first final run re-proposed a failed design
            # twice because only MEASURED designs used to appear here.
            res += f" | NOT MEASURED: {it['reason'][:400]}"
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


def counters_block(m, energy_pj: float, ppw: float, area_um2: float) -> str:
    """The COUNTERS block of the proposer's work order."""
    return "\n".join([
        f"cycles                 = {m.cycles:,}",
        f"off_chip_bytes         = {m.bytes_offchip():,}",
        f"macs_issued            = {m.counters.get('macs_issued', 0):,}",
        f"macs_useful            = {m.macs_useful:,}",
        f"MAC_GATED_TOTAL        = {m.counters.get('MAC_GATED_TOTAL', 0):,}",
        f"exe_active_fraction    = {m.exe_active_fraction():.4f}",
        f"RDMA_BYTES_REC         = {m.counters.get('RDMA_BYTES_REC', 0):,}",
        f"WDMA_BYTES_SENT        = {m.counters.get('WDMA_BYTES_SENT', 0):,}",
        f"energy_pj              = {energy_pj:,.0f}",
        f"perf_per_watt_gops_w   = {ppw:.3f}",
        f"area_um2               = {area_um2:,.0f}",
    ])


# Verdicts that carry no NEW design: nothing to remember them by.
_NO_DESIGN = ("DUPLICATE", "NO_EDIT", "AGENT_FAILED", "REPAIR_REVERTED",
              "REPAIR_NO_EDIT", "INFRA_FAILURE")


def failure_reason(record: dict, run_dir: Path) -> str:
    """One line on WHY an evaluated design failed, from its record.

    A NOT_ACTIONABLE repair's root cause wins: it is the only place the
    mechanism-level explanation exists.
    """
    rp = record.get("repair") or {}
    att = rp.get("attempts") or []
    if att and att[-1].get("status") == "NOT_ACTIONABLE":
        f = run_dir / f"repair_{record['iteration']:03d}_{att[-1]['attempt']}.md"
        if f.is_file():
            rc = recovery.root_cause_text(f.read_text())
            if rc:
                return f"the repair agent found it cannot work here: {rc}"
    for key in ("shrunk_case", "scope_violations", "violations", "compile_errors", "stderr_tail"):
        v = record.get(key)
        if v:
            v = "; ".join(v) if isinstance(v, list) else str(v)
            return " ".join(v.split())[:400]
    return str(record.get("verdict", "?"))


def reconstruct_run(run_dir: Path, args, workload) -> dict:
    """Rebuild the loop's state from a run's own records, for --resume.

    Nothing is taken on trust. Every measured iteration is replayed through
    the SAME Pareto admission, and the replayed verdict must equal the
    recorded one or the resume aborts. The immutable inputs must hash to the
    run's own start manifest. The tree is restored from the parent's exact diff.
    """
    recs = [json.loads(f.read_text()) for f in sorted(run_dir.glob("iter_*.json"))]
    its = [r.get("iteration") for r in recs]
    if not recs or its != list(range(1, len(recs) + 1)):
        raise SystemExit(f"ABORT: cannot resume {run_dir}: iterations on disk are {its}")
    now = integrity_manifest()
    if recs[0].get("manifest") != now:
        raise SystemExit(f"ABORT_RUN: cannot resume: the immutable inputs changed since "
                         f"the run started ({recs[0].get('manifest')} -> {now})")

    hp = run_dir / "history.json"
    history = json.loads(hp.read_text()) if hp.is_file() else {"iterations": [], "front": []}
    in_hist = {h.get("iteration") for h in history.get("iterations", [])}
    st = dict(front=ParetoFront(), archive=Archive(), base_point=None, parent=BASELINE,
              parent_reward=0.0, parent_rtl_id=None, parent_diff=None, parent_scores=None,
              seen=set(), seen_info={}, prev_netlist=None, prev_rtl_id=None,
              pred_tally=[0, 0], cand_pool=[], tried_changes=set(),
              last_admitted=False, last_reward=None, baseline_scores=None,
              last_measured=None)
    added = []
    for r in recs:
        it, v = r["iteration"], r.get("verdict")
        for c in r.get("candidates") or []:
            cd = candidates.Candidate(**c)
            if cd.implemented:
                if cd.change:
                    st["tried_changes"].add(cd.change)
            else:
                st["cand_pool"].append((it, cd))
        ps = r.get("prediction_score") or {}
        st["pred_tally"][0] += ps.get("hits", 0)
        st["pred_tally"][1] += ps.get("scored", 0)
        if r.get("netlist_digest"):
            st["prev_netlist"], st["prev_rtl_id"] = r["netlist_digest"], r.get("rtl_digest")
        ident = (r.get("state_hash"), r.get("rtl_digest"))
        if "admit_info" in r:                                 # reached N60: measured
            child = DesignState.from_dict(r["state"])
            m = Metrics(r["metrics"]["cycles"], r["metrics"]["macs_useful"],
                        r["metrics"]["counters"])
            point = Point(state_hash=child.state_hash(), parent_hash=r["parent_hash"],
                          t=m.cycles * r["period_ns"], E=r["energy_pj"], A=r["area_um2"],
                          sram_bytes=r["t1_prediction"]["sram_bytes"],
                          descriptor=descriptor(child, workload.in_block_density(),
                                                workload.total_blocks),
                          cost=r.get("wall_clock_s", 0.0))
            if st["base_point"] is None:
                st["base_point"] = point
                st["front"].admit(point)
                st["archive"].offer(point, 0.0)
                vv, info = Verdict.ADMIT_FRONT, {"reward": 0.0}
            else:
                vv, info = admit(point, st["front"], st["archive"], st["base_point"],
                                 parent_reward=st["parent_reward"], iteration=it,
                                 budget=args.iters, area_budget=t0.AREA_BUDGET_UM2)
            if vv.value != v:
                raise SystemExit(f"ABORT: cannot resume: replaying iteration {it} gives "
                                 f"{vv.value}, the record says {v}")
            if all(ident):
                st["seen"].add(ident)
                st["seen_info"][ident] = (it, v, f"measured: {m.cycles:,} cycles, "
                                                 f"{r['energy_pj'] / 1e6:.2f} uJ, "
                                                 f"{r['area_um2'] / 1e6:.4f} mm2")
            st["last_measured"] = (r, m)
            if st["baseline_scores"] is None:
                st["baseline_scores"] = {"E": r["energy"]["energy_pj"], "A": r["area_um2"],
                                         "ppw": r["energy"]["perf_per_watt_gops_w"],
                                         "cyc": m.cycles}
            if vv in (Verdict.ADMIT_FRONT, Verdict.ADMIT_ARCHIVE, Verdict.ADMIT_STEP):
                rp = r.get("repair") or {}
                k = len(rp.get("attempts") or []) if rp.get("outcome") == "repaired" else 0
                dfile = run_dir / (f"diff_{it:03d}_repair{k}.json" if k else f"diff_{it:03d}.json")
                st.update(parent=child, parent_reward=info.get("reward", st["parent_reward"]),
                          parent_rtl_id=r.get("rtl_digest"),
                          parent_diff=json.loads(dfile.read_text()),
                          parent_scores={"time": point.t, "energy": r["energy_pj"],
                                         "area": r["area_um2"], "fmax": r["period_ns"]},
                          last_admitted=True, last_reward=info.get("reward"))
            else:
                st.update(last_admitted=False, last_reward=None)
        else:
            st.update(last_admitted=False, last_reward=None)
            if v not in _NO_DESIGN and r.get("state_hash"):
                reason = failure_reason(r, run_dir)
                if all(ident):
                    if v not in ("SCOPE_VIOLATION", "T0_ILLEGAL"):
                        st["seen"].add(ident)
                    st["seen_info"][ident] = (it, v, reason)
                if it not in in_hist:
                    mv = (r.get("move") or {}).get("changed") or {}
                    added.append({"iteration": it, "state": r["state_hash"],
                                  "mutation": {f: [b, a] for f, (a, b) in mv.items()},
                                  "verdict": v, "reason": reason})
    if added:
        history["iterations"] = sorted(history.get("iterations", []) + added,
                                       key=lambda h: h.get("iteration", 0))
    history["front"] = st["front"].summary(10)
    st["history"] = history
    st["next_it"] = len(recs) + 1
    st["last_record"] = recs[-1]
    return st


def proposer_feedback(score, tally, admitted: bool, pool: list, tried: set,
                      k: int = 3) -> str:
    """What the proposer learns about its OWN reasoning, appended to the diagnosis.

    Two things, both bounded to a few lines:

    1. Its prediction against the measurement. The output contract has always
       demanded a per-objective prediction and nothing ever scored it, so the
       agent never learned which mechanisms it models well. The direction is
       derived by candidates.score_prediction from the raw parent/child numbers,
       never taken from the agent.
    2. After a change that was NOT admitted (rejected or failed): moves the
       agent itself listed in ==CANDIDATES== and has not tried. Already-reasoned
       alternatives, handed back instead of re-derived from scratch.
    """
    out = []
    if score and score.get("scored"):
        parts = [f"{o} {d['predicted']} -> measured {d['actual']} ({d['rel'] * 100:+.1f}%)"
                 for o, d in score["per_objective"].items()]
        out.append("YOUR PREDICTION vs THE MEASUREMENT: " + "; ".join(parts)
                   + f". {score['hits']} of {score['scored']} right this time, "
                     f"{tally[0]} of {tally[1]} across the run.")
    if not admitted and pool:
        seen, rows = set(), []
        for it_, c_ in reversed(pool):                   # most recent first
            if c_.implemented or not c_.change or c_.change in tried or c_.change in seen:
                continue
            seen.add(c_.change)
            rows.append(f"  - iter {it_}: [{c_.technique or '?'}] {c_.change}"
                        + (f" -- {c_.rationale}" if c_.rationale else ""))
            if len(rows) >= k:
                break
        if rows:
            out.append("MOVES YOU LISTED EARLIER AND HAVE NOT TRIED. Your last change "
                       "was not admitted; check these against the diagnosis before "
                       "inventing a new one:\n" + "\n".join(rows))
    return ("\n\n" + "\n\n".join(out)) if out else ""


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


# The Claude Code backend is the only one here that can hit a SUBSCRIPTION
# usage limit rather than a per-minute API rate limit, and CHIA raises
# RateLimitError for it without retrying (chia/models/claude.py: it is in the
# "never retry, propagate immediately" set, correctly -- retrying in a tight
# loop cannot help).
#
# Imported lazily and tolerantly: a gemini or vertex arm must not fail to start
# because the claude backend is not importable. An EMPTY tuple in an `except`
# clause matches nothing, which is exactly the no-op wanted there.
try:
    from chia.models.claude import RateLimitError as _ClaudeRateLimitError
    RATE_LIMIT_ERRORS: tuple = (_ClaudeRateLimitError,)
except Exception:                                                # noqa: BLE001
    RATE_LIMIT_ERRORS = ()

# A usage window is hours, not minutes, so the ceiling has to be hours too --
# but not unbounded, or a misparsed reset date parks an overnight run forever.
RATE_LIMIT_MAX_WAIT_S = int(os.environ.get("SPARSECRAFT_RATE_LIMIT_MAX_WAIT_S", 6 * 3600))
RATE_LIMIT_RETRIES = int(os.environ.get("SPARSECRAFT_RATE_LIMIT_RETRIES", 3))


def measured_activity(state, m) -> float | None:
    """Global switching activity for OpenSTA, derived from THIS run's counters.

    Returned as toggles per clock cycle for `set_power_activity -global`, or
    None when the counters needed are absent (then OpenSTA uses its own default
    toggle rates and the result is labelled "default" rather than "measured").

    WHAT THIS IS, stated plainly because a power number inherits the honesty of
    its activity figure: ONE SCALAR applied to every net. It is derived from
    measurement -- the MAC issue rate this workload actually achieved -- but it
    is not per-net measured activity. Only a VCD or SAIF from a gate-level
    simulation gives that, and gate-level simulation is orders of magnitude
    slower than the 16-minute RTL run. So this sits honestly between OpenSTA's
    default guess and a real trace, and `power_activity_source` on the result
    says which of the three produced any given number.

    The derivation: `macs_issued / (cycles * dim^2)` is the fraction of MAC
    slots the workload exercised. When gating is ON, a zero-operand MAC holds
    its operand register instead of toggling the multiplier, so the gated
    fraction is removed -- which is precisely the mechanism T-A claims, now
    priced by the power tool rather than asserted by a constant.
    """
    c = getattr(m, "counters", None) or {}
    cycles = getattr(m, "cycles", 0) or 0
    dim = c.get("dim") or 0
    issued = c.get("macs_issued") or 0
    if not (cycles and dim and issued):
        return None
    slots = cycles * dim * dim
    if slots <= 0:
        return None
    act = issued / slots
    gated = c.get("MAC_GATED_TOTAL")
    if getattr(state, "gate_enable", False) and gated:
        act *= max(0.0, 1.0 - (gated / issued))
    # OpenSTA takes toggles/cycle; clamp to a sane band so a broken counter
    # cannot produce a nonsense annotation that silently prices the design.
    return float(min(max(act, 1e-4), 2.0))


def repair_turn(repair_llm, tools: list, brief: str) -> tuple[str, bool]:
    """One N73 repair turn: returns (transcript, call_ok).

    Never raises. A repair turn that errors (usage limit exhausted, CLI crash)
    must cost the repair, not the iteration: the caller then records the gate
    failure it was trying to fix, exactly as if no repair had been attempted.
    """
    try:
        cli = agent_turn(repair_llm, brief, tools)
    except Exception as exc:                                    # noqa: BLE001
        print(f"  N73 repair call failed: {type(exc).__name__}: {str(exc)[:200]}")
        return "", False
    return (cli.result or ""), bool(getattr(cli, "success", False))


def agent_turn(implement_llm, msg_text: str, tools: list):
    """One agentic turn, waiting out a usage limit instead of dying on it.

    Retried HERE, around the call, rather than at the iteration level, and the
    distinction is the whole point. The generic per-iteration handler in main()
    catches everything and moves to the next proposal -- right for an OOM or a
    container that died, badly wrong for a usage limit: the next iteration hits
    the same limit within milliseconds, so a 20-iteration run that reaches the
    limit at iteration 6 does not lose one iteration, it loses fourteen, each
    filed as INFRA_FAILURE seconds apart. That is what happened to run
    `agent15` for a different reason, and it is not worth repeating.

    Waiting here instead costs the wall clock and keeps the iteration: no state
    is unwound, the proposal is re-issued unchanged, and the budget is intact.
    """
    last = None
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            return get(implement_llm.prompt.options(**C.LLM_OPTS)
                       .chia_remote(implement_llm, msg_text, tools))
        except RATE_LIMIT_ERRORS as exc:                          # noqa: PERF203
            last = exc
            reset = getattr(exc, "reset_time", None)
            wait = RATE_LIMIT_MAX_WAIT_S
            if reset is not None:
                from datetime import datetime, timezone
                now = datetime.now(reset.tzinfo or timezone.utc)
                wait = (reset - now).total_seconds()
            # +60s of slack: waking up exactly at the boundary earns a second
            # rate limit and burns a retry for nothing.
            wait = max(60.0, min(float(wait) + 60.0, RATE_LIMIT_MAX_WAIT_S))
            if attempt == RATE_LIMIT_RETRIES:
                break
            print(f"  !! USAGE LIMIT (attempt {attempt}/{RATE_LIMIT_RETRIES}). "
                  f"resets {reset}; sleeping {wait/60:.0f} min, then re-issuing "
                  f"this same turn.", flush=True)
            time.sleep(wait)
    raise last


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
    # Default OFF, i.e. everything recomputed. The cache is content-addressed
    # and provably cannot serve a stale result for a NEW design -- a hit needs
    # an identical hw_hash / rtl_digest / sw_hash -- so this changes nothing
    # about what the agent's proposals measure. What it changes is the
    # BASELINE, which is byte-identical run to run and was previously a
    # seconds-long hit. Recomputing it costs 20-40 min and buys the property
    # that every number in the run came from a tool that ran during the run.
    # See configs/no_cache.yaml.
    ap.add_argument("--candidates", type=int, default=3, metavar="K",
                    help="how many distinct moves the agent must list each turn, "
                         "itself included (N10 fan-out). 0 disables the section "
                         "entirely and restores V1 behaviour. Recorded ones that "
                         "were NOT implemented become a backlog the selector can "
                         "hand back when the search stalls. Default: 3.")
    ap.add_argument("--no-repair", action="store_true",
                    help="disable the N73 repair agent. A gate failure then "
                         "costs the whole iteration, as in V1.")
    ap.add_argument("--resume", action="store_true",
                    help="continue the EXISTING run --run-name from its records: "
                         "rebuild the front, archive, history, parent and dedup set "
                         "(replaying every admission and aborting on any mismatch), "
                         "restore the parent's exact tree, and carry on at the next "
                         "iteration. --iters stays the run's total budget.")
    ap.add_argument("--repair-budget", type=int, default=3, metavar="N",
                    help="maximum N73 repair attempts per iteration, across all "
                         "failure classes. Each class also has its own cap "
                         "(recovery.CLASSES[...].retries), sized by what its "
                         "re-check costs: compile 3, T0 2, elaboration 2, "
                         "kernel 2, divergence 1. After every attempt the WHOLE "
                         "gate ladder re-runs. Default: 3.")
    ap.add_argument("--cache-scope", choices=("run", "global", "off"),
                    default="run",
                    help="who may satisfy a cache hit. 'run' (default): only "
                         "work done EARLIER IN THIS RUN -- the cache starts "
                         "empty in the run directory, so a repeated design is "
                         "cheap but nothing from an older run can leak in. "
                         "'global': the shared cache, reusable across runs. "
                         "'off': recompute every node, reuse nothing.")
    ap.add_argument("--no-cache", dest="cache_scope", action="store_const",
                    const="off", help="alias for --cache-scope off.")
    ap.add_argument("--cache", dest="cache_scope", action="store_const",
                    const="run", help="alias for --cache-scope run.")
    ap.add_argument("--backend", default=None,
                    help="LLM backend (claude, gemini, vertex, openai, "
                         "claude_api, anthropic, openrouter, groq, opencode). "
                         "Default: $SPARSECRAFT_LLM_BACKEND, else claude")
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
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                    help="override any DesignState field, repeatable: "
                         "--set sp_banks=8 --set dma_maxbytes=128. Types are "
                         "coerced from the dataclass, and T0 still judges the "
                         "result -- this sets the design point, it does not "
                         "excuse an illegal one. For sweeping the hardware "
                         "levers without an agent.")
    ap.add_argument("--x-resident", action="store_true",
                    help="hold ALL of X in the scratchpad for the whole matmul "
                         "instead of re-mvin'ing its slices once per nonzero "
                         "block. SOFTWARE lever, coupled to sp_capacity_kb: X is "
                         "2,048 scratchpad rows of 16,384 at 256 KB.")
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
                         "advertising the `hammer` resource (see configs/cluster.yaml). "
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
            print(f"       check with: python scripts/check_llm.py --list")
            print(f"       or run without a model:  --skip-llm")
            return 2
        print(f"proposer: {info['backend']}/{info['model']}"
              f"  credential={info['credential_var']}")
    else:
        print("proposer: DISABLED (--skip-llm); the tree is evaluated as-is")

    # Apply the CLI overrides to the baseline BEFORE anything is written or
    # hashed, so the cache key and the trace both describe what actually ran.
    global BASELINE, _BASELINE_SCORES
    # --set FIELD=VALUE, coerced against the dataclass so a typo or a bad type
    # fails here rather than surfacing as a mystery three stages later.
    _overrides = {}
    if args.set:
        _types = {f.name: f.type for f in dc_fields(BASELINE)}
        for item in args.set:
            if "=" not in item:
                raise SystemExit(f"--set expects FIELD=VALUE, got {item!r}")
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if k not in _types:
                raise SystemExit(f"--set: unknown field {k!r}. Known: "
                                 f"{', '.join(sorted(_types))}")
            cur = getattr(BASELINE, k)
            if isinstance(cur, bool):
                _overrides[k] = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                _overrides[k] = int(v)
            elif isinstance(cur, float):
                _overrides[k] = float(v)
            else:
                _overrides[k] = v

    if (args.workload or args.dense or args.gate or args.zbu or _overrides
            or args.k_chunk is not None or args.b_blocks is not None
            or args.spad_kb is not None):
        BASELINE = BASELINE.mutate(
            **({"workload": args.workload} if args.workload else {}),
            **({"dense_mode": True} if args.dense else {}),
            **({"gate_enable": True} if args.gate else {}),
            **({"zbu_enable": True} if args.zbu else {}),
            **({"x_resident": True} if args.x_resident else {}),
            **({"k_chunk": args.k_chunk} if args.k_chunk is not None else {}),
            **({"b_blocks": args.b_blocks} if args.b_blocks is not None else {}),
            **({"sp_capacity_kb": args.spad_kb} if args.spad_kb is not None else {}),
            **_overrides)
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
    _wl_json = (Path(C.WORKLOAD_DIR) / "generated"
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

    # Two independent dials, and conflating them is what made the old --cache
    # unsafe: the YAML decides WHICH NODES may be served, the cache DIRECTORY
    # decides WHOSE WORK may serve them.
    #
    # scope="run" points the directory inside run_dir, so it starts EMPTY. A
    # design the agent repeats within this run is a hit (worth having -- the
    # agent does revisit points), while nothing an earlier run computed can
    # satisfy anything here. That is the property that makes a run's numbers
    # re-derivable from the run itself.
    if args.cache_scope == "off":
        cfg_name, cache_dir = "no_cache.yaml", C.CACHE_DIR
        policy = "recomputing every node, reusing nothing"
    elif args.cache_scope == "global":
        cfg_name, cache_dir = "bypass_cache.yaml", C.CACHE_DIR
        policy = f"reusing exact-hash hits from ANY run ({C.CACHE_DIR})"
    else:
        cfg_name, cache_dir = "bypass_cache.yaml", str(run_dir / "cache")
        policy = "reusing exact-hash hits from THIS RUN only"
    os.makedirs(cache_dir, exist_ok=True)
    # Printed, not silent: "why did that take 40 minutes" and "why was that
    # instant" are the same question, and the answer is this line.
    cfg = os.path.join(C.CONFIG_DIR, cfg_name)
    print(f"cache policy: {args.cache_scope} ({cfg_name}) -- {policy}")
    cache = start_cache(size=32, units="GB", cache_dir_path=cache_dir, yaml_path=cfg)
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
                     agent.make_llm("system/microarchitect.md", backend=args.backend,
                                    model=args.model,
                                    log_dir=str(run_dir / "llm")))
    # N73. A SECOND LLM with a different system prompt, per CHIA's own
    # Implement-LLM / Debug-LLM split (examples/riscv_extensions). V1 shipped
    # `adapted/repair.md` -- 9,352 bytes of repair prompt -- and never
    # constructed anything to load it, so a gate failure cost the whole
    # iteration. `system/repairer.md` is that file, now at its node.
    #
    # Cheaper effort on purpose: repair is a narrower problem than proposal,
    # it is bounded to 1-2 attempts, and it is charged to no budget -- so the
    # wall clock of a failed iteration matters more than its depth.
    repair_llm = None
    if not args.skip_llm and not args.no_repair:
        import os as _os
        _eff = _os.environ.get("SPARSECRAFT_CLAUDE_EFFORT")
        _os.environ["SPARSECRAFT_CLAUDE_EFFORT"] = _os.environ.get(
            "SPARSECRAFT_REPAIR_EFFORT", "high")
        try:
            repair_llm = agent.make_llm("system/repairer.md", backend=args.backend,
                                        model=args.model,
                                        log_dir=str(run_dir / "llm"))
        finally:
            if _eff is None:
                _os.environ.pop("SPARSECRAFT_CLAUDE_EFFORT", None)
            else:
                _os.environ["SPARSECRAFT_CLAUDE_EFFORT"] = _eff
    history = {"iterations": [], "front": []}

    # Which prompts this run ACTUALLY loaded, fully composed, with their hashes.
    # A run record should prove what the agents were told, not rely on the tree
    # still holding the same files when someone reads the results later.
    if not args.skip_llm:
        _pm = {}
        for _rel, _role in (("system/microarchitect.md", "proposer system"),
                            ("task/propose.md", "proposer work order (template)"),
                            ("system/repairer.md", "repairer system"),
                            ("task/repair.md", "repairer work order (template)")):
            if "repair" in _rel and repair_llm is None:
                continue
            _txt = (agent.read_prompt(_rel) if _rel.startswith("system/")
                    else (Path(C.PROMPTS_DIR) / _rel).read_text())
            _pm[_rel] = {"role": _role, "chars": len(_txt),
                         "sha256": hashlib.sha256(_txt.encode()).hexdigest()[:16]}
            print(f"prompt: {_rel:<26} {_pm[_rel]['chars']:>6,} chars  "
                  f"sha256 {_pm[_rel]['sha256']}  ({_role})")
        (run_dir / "prompts.json").write_text(json.dumps(_pm, indent=2))
        print(f"repair: {'ON' if repair_llm is not None else 'OFF'}"
              + (f", budget {args.repair_budget}/iteration" if repair_llm is not None else ""))

    prev_netlist, prev_rtl_id = None, None
    front, archive = ParetoFront(), Archive()
    parent, parent_reward, diagnosis = BASELINE, 0.0, ""
    # The parent's RTL digest, alongside its config. N21's identity is the PAIR
    # (state_hash, rtl_digest), because an RTL-only edit leaves the config
    # untouched and would otherwise read as a duplicate of its own parent.
    parent_rtl_id = None
    base_point = None
    seen = set()
    # ident -> (iteration, verdict, reason): what each already-evaluated design
    # IS, so a DUPLICATE can say which iteration it repeats and why that failed.
    seen_info: dict = {}

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

        # The EXACT tree of the current parent, as collect_diff returned it when
        # the parent was admitted. None until the baseline is admitted.
        parent_diff = None

        def rollback_to_parent() -> None:
            """Restore the tree to EXACTLY the parent's -- RTL included.

            `apply_design_state(parent)`, the previous rollback, rewrites the
            params file and nothing else. The Chisel the agent wrote survives it:
            PE.scala's scaffold is a no-op once the agent's sentinel is present,
            and SparseCraftSparsity.scala is only ever seeded when absent. So a
            REJECTED RTL change stayed in the tree while `parent` and
            `parent_rtl_id` described the old one, and the next proposer edited
            on top of hardware nobody had admitted. With a repair agent writing
            RTL as well, that leak would compound.

            Reset + re-apply the parent's own diff is the primitive `--seed-diff`
            already uses, and it is exact. Build outputs survive it: they are
            git-ignored, and the reset is `git clean -fd`, never `-x`.
            """
            err_, msg_ = get(diff_nodes.reset_and_apply_diff.options(**pg_opts)
                             .chia_remote(parent_diff or {}))
            if err_:
                # Should not happen -- the diff applied cleanly when it was
                # collected -- but a rollback that fails must still leave a tree
                # that describes `parent`, never a half-applied one.
                print(f"  rollback: {msg_}; falling back to params-only rollback")
                get(diff_nodes.reset_and_apply_diff.options(**pg_opts).chia_remote({}))
                ensure_baseline()
                get(nodes.apply_design_state.options(**pg_opts)
                    .chia_remote(json.dumps(parent.canonical())))
            ensure_baseline()
            # SparseCraftRTL.scala is regenerated from the markers only inside
            # the ladder; make the tree at rest agree with the parent's toggles.
            get(nodes.apply_rtl_params.options(**pg_opts)
                .chia_remote(json.dumps(parent.canonical())))

        # A control arm needs to know whether its last proposal was admitted.
        # Reported at the START of the next iteration rather than at each exit
        # point: an iteration can leave via five different early `continue`s
        # (scope, T0, duplicate, elaboration, kernel, tripwire) and an arm that
        # missed any of them would stall waiting for a verdict that never came.
        # Not-admitted is therefore the default, and only N60 overrides it.
        last_admitted, last_reward = False, None

        last_counters = "(no measurement yet)"
        # The last MEASURED diagnosis, for strategy selection. `diagnosis` itself
        # becomes a failure message after a failed iteration, which names no
        # bottleneck, so it cannot choose the levers.
        strategy_diag = ""
        # N10 candidates: every alternative the agent listed and did not
        # implement, the changes it did implement, the parent's measured
        # objectives (to score predictions against), and a running tally.
        cand_pool: list = []
        tried_changes: set = set()
        parent_scores = None
        pred_tally = [0, 0]

        start_it = 1
        if args.resume:
            _rs = reconstruct_run(run_dir, args, workload)
            history = _rs["history"]
            history_path.write_text(json.dumps(history, indent=2))
            front, archive, base_point = _rs["front"], _rs["archive"], _rs["base_point"]
            parent, parent_reward = _rs["parent"], _rs["parent_reward"]
            parent_rtl_id, parent_diff = _rs["parent_rtl_id"], _rs["parent_diff"]
            parent_scores, pred_tally = _rs["parent_scores"], _rs["pred_tally"]
            seen, seen_info = _rs["seen"], _rs["seen_info"]
            prev_netlist, prev_rtl_id = _rs["prev_netlist"], _rs["prev_rtl_id"]
            cand_pool, tried_changes = _rs["cand_pool"], _rs["tried_changes"]
            last_admitted, last_reward = _rs["last_admitted"], _rs["last_reward"]
            _BASELINE_SCORES = _rs["baseline_scores"]
            _lr, _lm = _rs["last_measured"]
            strategy_diag = diagnose(_lm)
            last_counters = counters_block(_lm, _lr["energy"]["energy_pj"],
                                           _lr["energy"]["perf_per_watt_gops_w"],
                                           _lr["area_um2"])
            _last = _rs["last_record"]
            if "admit_info" in _last:
                diagnosis = _last.get("diagnosis") or (strategy_diag + tried_summary(history))
            else:
                _info = seen_info.get((_last.get("state_hash"), _last.get("rtl_digest")))
                diagnosis = (f"your previous proposal was {_last.get('verdict')}"
                             + (f": it is the design from iteration {_info[0]}, which was "
                                f"{_info[1]} -- {_info[2]}" if _info else "")
                             + tried_summary(history)
                             + proposer_feedback(None, pred_tally, False, cand_pool,
                                                 tried_changes))
            start_it = _rs["next_it"]
            rollback_to_parent()
            (run_dir / "resume.json").write_text(json.dumps({
                "resumed_at_iteration": start_it,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "parent": parent.state_hash(), "parent_rtl": parent_rtl_id,
                "front": len(front.points), "seen": len(seen),
                "history_entries": len(history.get("iterations", []))}, indent=2))
            print(f"RESUMED {args.run_name} at iteration {start_it}: parent "
                  f"{parent.state_hash()}  front={len(front.points)}  "
                  f"seen={len(seen)}  history={len(history.get('iterations', []))} "
                  f"(every admission replayed and matched)")

        for it in range(start_it, args.iters + 1):
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
                # The proposer's own words, for the repairer: its ==MUTATION==
                # and ==PREDICTION== are the intent a repair must preserve.
                proposer_text = ""

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
                    # K is a PARAMETER, not prose in the system prompt. It
                    # lives in the per-iteration work order because the count
                    # is a search-policy knob that may change between runs,
                    # while the ==CANDIDATES== FORMAT is immutable and belongs
                    # in the cacheable prefix.
                    _k = int(getattr(args, "candidates", 3) or 0)
                    _cand = ("" if _k < 1 else
                             f"**List {_k} distinct candidate moves** in the "
                             f"`==CANDIDATES==` section, including the one you "
                             f"implement. Genuinely different moves, not one move "
                             f"at {_k} magnitudes. If you can only justify fewer, "
                             f"give fewer and say why -- the harness keeps the "
                             f"ones you do not implement and may hand one back "
                             f"when the search stalls.")
                    msg_text = agent.load_prompt(
                        "task/propose.md",
                        ITERATION=str(it),
                        BUDGET=str(args.iters),
                        PARAMS_PATH=os.path.join(C.CHIPYARD_PATH, C.PARAMS_FILE_REL),
                        PE_PATH=os.path.join(C.CHIPYARD_PATH, C.RTL_FILES_REL[0]),
                        ZBU_PATH=os.path.join(C.CHIPYARD_PATH, C.RTL_FILES_REL[1]),
                        CHIPYARD=C.CHIPYARD_PATH,
                        PARENT_STATE=parent.to_json(),
                        DIAGNOSIS=diagnosis or "(first iteration - no measurement yet)",
                        COUNTERS=last_counters,
                        CANDIDATES=_cand,
                        STRATEGY=strategy_section(strategy_diag),
                    )
                    record["strategy"] = {
                        "label": bottleneck_label(strategy_diag),
                        "modules": [m_ for m_ in strategy_for(strategy_diag)
                                    if m_ not in STRATEGY_IN_SYSTEM]}
                    cli = agent_turn(implement_llm, msg_text, tools)
                    proposer_text = cli.result or ""
                    # ==CANDIDATES== / ==MUTATION== / ==PREDICTION==, parsed.
                    # Tolerant: a turn without the sections yields [] and costs
                    # nothing but the feedback.
                    _cands = candidates.parse_candidates(proposer_text)
                    record["candidates"] = [c_.to_dict() for c_ in _cands]
                    _impl = next((c_ for c_ in _cands if c_.implemented), None)
                    record["prediction"] = _impl.predicted if _impl else {}
                    if _impl is not None and _impl.change:
                        tried_changes.add(_impl.change)
                    cand_pool.extend((it, c_) for c_ in _cands if not c_.implemented)
                    (run_dir / f"llm_{it:03d}.md").write_text(proposer_text)
                    record["llm_returncode"] = cli.returncode

                # ==== N13 -> N41: the gate ladder, as one re-enterable function ====
                #
                # Round 0 evaluates the proposer's tree. Round k >= 1 re-evaluates
                # the tree after repair turn k -- the WHOLE ladder, from the scope
                # check down, because a repair can break an earlier gate as easily
                # as it fixes a later one. Every gate failure is RETURNED as a
                # GateFail; nothing in here writes the iteration record or rolls
                # the tree back. The caller does both, once.
                proposed_state = None       # the proposer's parsed state (round 0)
                proposed_rtl = None         # ... and its RTL digest
                round_idents: list = []     # (state_hash, rtl_digest) per round
                # N52 dispatched in parallel: {"hw_tag", "ref", "t", "tag"}. One
                # slot: a repair round that keeps the hardware reuses it, one that
                # changes the hardware replaces it (the old task still finishes
                # and is ignored -- it holds the hammer slot, but for ~8 min of a
                # ~27 min simulation, so the new one never waits in practice).
                pending_synth: dict = {}

                def evaluate_tree(round_no: int):
                    nonlocal prev_netlist, prev_rtl_id
                    # ---- N13 scope check, THEN collect the diff ---------------------
                    touched = get(diff_nodes.changed_paths.options(**pg_opts).chia_remote())
                    scope = t0.check_patch_scope(touched, harness_paths=harness_paths)
                    record["touched_paths"] = touched
                    if not scope.legal:
                        print(f"  N13 SCOPE VIOLATION: {scope.violations}")
                        return GateFail(
                            "SCOPE_VIOLATION",
                            evidence=("Paths outside the writable set:\n"
                                      + "\n".join(f"  - {v}" for v in scope.violations)),
                            diagnosis=f"REJECTED: {scope.violations[0]}",
                            extra={"scope_violations": scope.violations})

                    err, diff = get(diff_nodes.collect_diff.options(**pg_opts).chia_remote())
                    # Persist the diff IMMEDIATELY -- it is the only thing that survives
                    # the container.
                    # Round 0 is the proposer's mutation; a repaired tree is kept
                    # beside it, never over it, so the record shows both.
                    (run_dir / (f"diff_{it:03d}.json" if not round_no
                                else f"diff_{it:03d}_repair{round_no}.json")
                     ).write_text(json.dumps(diff, indent=2))
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
                    # N73 revert guard, config half. A repair that moves a mutated
                    # field back to (or past) the parent's value has not fixed the
                    # mutation, it has removed it -- decided here from the three
                    # states, never from the repairer's self-audit.
                    if round_no and proposed_state is not None:
                        _rc = recovery.revert_check(parent.canonical(), proposed_state,
                                                    child.canonical())
                        if _rc.reverted:
                            print(f"  N73 REPAIR REVERTED the mutation: {_rc.summary()}")
                            return GateFail("REPAIR_REVERTED", child=child,
                                            repairable=False, evidence=_rc.summary())
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
                        return GateFail(
                            "T0_ILLEGAL", child=child, violations=verdict_t0.violations,
                            diagnosis="T0 rejected: " + "; ".join(verdict_t0.violations),
                            extra={"violations": verdict_t0.violations})
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
                    if (it > 1 and arm is None and not args.skip_llm and not round_no
                            and record["move"]["n_changed"] == 0
                            and record.get("llm_returncode", 0) != 0):
                        print(f"  N10 AGENT FAILED (rc={record['llm_returncode']})"
                              f" -- no edit reached the tree; NOT a duplicate")
                        return GateFail(
                            "AGENT_FAILED", child=child, repairable=False,
                            diagnosis=("your previous turn produced no usable output, so no "
                                       "edit was made. Begin THIS turn with a tool call that "
                                       "makes one concrete edit."))
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

                    # N73 revert guard, RTL half: the proposer changed the source and
                    # the repair put it back byte-for-byte.
                    if (round_no and proposed_rtl and parent_rtl_id
                            and proposed_rtl != parent_rtl_id and rtl_id == parent_rtl_id):
                        print("  N73 REPAIR REVERTED the RTL to the parent's")
                        return GateFail("REPAIR_REVERTED", child=child, repairable=False,
                                        evidence="the RTL source is byte-identical to "
                                                 "the parent's again")
                    # A repair turn that changed nothing. Distinct from DUPLICATE,
                    # and caught before it: the unchanged tree IS the failed one.
                    if round_no and round_idents and ident == round_idents[-1]:
                        print("  N73 REPAIR MADE NO EDIT")
                        return GateFail("REPAIR_NO_EDIT", child=child, repairable=False,
                                        evidence="neither the config nor the RTL changed "
                                                 "during the repair turn")
                    round_idents.append(ident)

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
                        if round_no:
                            print("  N73 REPAIR REVERTED: the tree is the parent's again")
                            return GateFail("REPAIR_REVERTED", child=child, repairable=False,
                                            evidence="config and RTL are both the "
                                                     "parent's again")
                        print("  N21 NO EDIT: neither the config nor the RTL changed")
                        return GateFail("NO_EDIT", child=child, repairable=False, diagnosis=(
                            "NOTHING CHANGED. Neither the config nor the RTL differs from "
                            "your parent, so whatever you reported last turn was not "
                            "actually written. The harness reads the FILES, never your "
                            "report. Call the edit tool, wait for the tool RESULT that "
                            "confirms the write, and only then describe what you did."))

                    if ident in seen:
                        print("  N21 duplicate design; asking for a different edit")
                        # Record it. A deduped iteration still consumed a turn and an
                        # LLM call, so leaving it out of the records makes the arm look
                        # more efficient than it was and hides livelocks entirely.
                        _prev = seen_info.get(ident)
                        return GateFail(
                            "DUPLICATE", child=child, repairable=False,
                            diagnosis=(f"design {child.state_hash()} with this RTL was ALREADY "
                                       f"EVALUATED"
                                       + (f": it is exactly the design from iteration "
                                          f"{_prev[0]}, which was {_prev[1]} -- {_prev[2]}"
                                          if _prev else "")
                                       + ". Proposing it again returns the same verdict "
                                       "without measuring anything. Change a DIFFERENT "
                                       "lever, or change the RTL mechanism."))
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
                        # The cheapest failure to recover from: the gate is 1-3 min,
                        # the error carries file:line, and a fix re-checks for the same
                        # 1-3 min. V1 spent the whole iteration here -- 10 of 14
                        # proposals in runs/agentic15b died exactly this way.
                        return GateFail(
                            "COMPILE_FAILED", child=child, stderr=gate["errors"],
                            diagnosis=("the Chisel did not compile:\n"
                                       + gate["errors"][-1500:]),
                            extra={"compile_errors": gate["errors"]})
                    print("  N12 compile OK")

                    # ---- N22 T1 (a FILTER; never a substitute for measurement) -------
                    pred = predict(child, workload)
                    record["t1_prediction"] = pred.to_dict()
                    print(f"  N22 T1 predicts bound_by={pred.bound_by}  "
                          f"area={pred.area_um2:,.0f}um2")

                    # ---- N30/N31 elaborate, N32 kernel, N50 simulate ----------------
                    sj = json.dumps(child.canonical())
                    kernel_src = (Path(C.KERNELS_DIR) / "spmm.c").read_text()
                    # The generated workload travels by VALUE: the head and the build
                    # container share no filesystem. Missing is fatal and loud -- a
                    # silently empty header would compile to a kernel measuring nothing.
                    data_path = Path(C.WORKLOAD_DIR) / "generated" / f"spmm_{child.workload}.h"
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
                        return GateFail(
                            "ELABORATION_FAILED", child=child, stderr=art.stderr,
                            diagnosis=f"elaboration failed:\n{art.stderr[-1500:]}",
                            extra={"stderr_tail": art.stderr[-3000:]})

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
                            return GateFail(
                                "RTL_NOOP", child=child,
                                evidence=(f"rtl_digest changed {prev_rtl_id} -> {rtl_id}, "
                                          f"but the elaborated netlist did not "
                                          f"({nl['digest']}, {nl.get('n_files', 0)} files)."),
                                diagnosis=(
                                "Your RTL edit compiled and changed the source, but the "
                                "ELABORATED HARDWARE is byte-identical to the parent's. "
                                "The mechanism was not instantiated. Common cause: the "
                                "logic sits behind a Scala `if` on a parameter that is "
                                "false, or it is dead code nothing reads. Verify the "
                                "signal you added actually drives an output."))
                    if nl.get("ok"):
                        prev_netlist, prev_rtl_id = nl["digest"], rtl_id
                        print(f"  N12b netlist {nl['digest']} ({nl['n_files']} files)")

                    # ---- N52, dispatched now: overlaps the kernel build + simulation
                    if (args.synth and synth_in_parallel()
                            and pending_synth.get("hw_tag") != hw_tag):
                        _tag = synth_tag(child, rtl_id, args.synth_tech,
                                         args.synth_clock_ns, None)
                        pending_synth.clear()
                        pending_synth.update(
                            hw_tag=hw_tag, tag=_tag, t=time.time(),
                            ref=synth_recipe.synthesize_recipe.chia_remote(
                                dict(art.generated_src_files),
                                **synth_kwargs(art, args, None), _chia_tag=_tag))
                        print("  N52 T3 synthesis dispatched; runs in parallel with "
                              "the simulation")

                    kern = get(nodes.build_kernel.options(**pg_opts)
                               .chia_remote(sj, kernel_src, data_header,
                                            _chia_tag=f"sw:{child.sw_hash()}+k{build_id}"))
                    if not kern["success"]:
                        print(f"  N32 KERNEL BUILD FAILED rc={kern['returncode']} "
                              f"bytes={kern.get('binary_bytes', 0)}")
                        return GateFail(
                            "KERNEL_BUILD_FAILED", child=child, stderr=kern["stderr"],
                            diagnosis=f"kernel build failed:\n{kern['stderr'][-1500:]}",
                            extra={"stderr_tail": kern["stderr"][-3000:]})

                    run = get(nodes.simulate.chia_remote(
                        art, kern, timeout_seconds=args.sim_timeout,
                        _chia_tag=f"sim:{child.sw_hash()}+k{build_id}"))
                    try:
                        m = parse_metrics(run.log)
                    except MetricsError as _me:
                        print(f"  N50 NO COUNTERS: {_me}")
                        return GateFail(
                            "EQUIV_MISSING", child=child,
                            stderr=(getattr(run, "log", "") or "")[-4000:],
                            evidence=(f"simulator returncode "
                                      f"{getattr(run, 'returncode', '?')}; the kernel "
                                      f"printed no counters at all ({_me})"),
                            diagnosis=("the simulation produced no counters: the design "
                                       "hung (a handshake that never completes) or the "
                                       "simulation hit its time limit."))
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
                        return GateFail(
                            "TRIPWIRE_FAILED", child=child,
                            evidence=(f"measured off-chip bytes {m.bytes_offchip():,} < "
                                      f"floor {min_bytes:,} (one read of the nonzero A "
                                      f"blocks, all of X, one write of Y)"),
                            diagnosis=(f"TRIPWIRE: the design moved {m.bytes_offchip():,} "
                                       f"off-chip bytes, below the {min_bytes:,} needed to "
                                       f"read its inputs once. It skipped a load the result "
                                       f"depends on, or a counter stopped counting."))

                    # ---- N41 functional equivalence: MANDATORY GATE -----------------
                    # The kernel compares every output against a host-computed golden
                    # and PRINTS the count; the verdict is taken here, where the agent
                    # cannot reach it. A design that is fast and wrong is not a result.
                    mism = m.counters.get("equiv_mismatches")
                    if mism is None:
                        print("  N41 ABORT: kernel reported no equiv_mismatches counter")
                        # The kernel prints this line unconditionally at the end, so
                        # its absence means the run never reached the end: the design
                        # hung, or the simulation hit its time limit. Infra causes
                        # (an OOM-killed simulator) are caught by classify() on the log.
                        return GateFail(
                            "EQUIV_MISSING", child=child,
                            stderr=(getattr(run, "log", "") or "")[-4000:],
                            evidence=(f"simulator returncode "
                                      f"{getattr(run, 'returncode', '?')}; the kernel never "
                                      f"printed its equiv_mismatches line"),
                            diagnosis=("the kernel never printed its equivalence line: the "
                                       "design hung (a handshake that never completes) or "
                                       "the simulation hit its time limit."))
                    record["equiv_mismatches"] = mism
                    if mism != 0:
                        print(f"  N41 EQUIV FAILED: {mism:,} mismatching outputs "
                              f"(first at [{m.counters.get('equiv_first_i')},"
                              f"{m.counters.get('equiv_first_j')}] "
                              f"got={m.counters.get('equiv_got')} "
                              f"want={m.counters.get('equiv_want')})")
                        # N72. The simulator already recorded the first mismatching
                        # element; V1 reported only the raw count. "32,674 mismatches"
                        # says a design is wrong, "[0,0] got 339 want 190" says where
                        # to look -- and the ratio hint names the usual cause.
                        _sc = recovery.shrink_divergence(m.counters)
                        record["shrunk_case"] = _sc.summary
                        print(f"  N72 {_sc.summary}")
                        return GateFail(
                            "EQUIV_FAILED", child=child, shrunk=_sc,
                            diagnosis=(f"WRONG ANSWER: {mism} of "
                                       f"{wl_stats['M'] * wl_stats['N']} outputs differ from "
                                       f"the golden reference ({_sc.summary}). The design "
                                       f"computed something, but not Y = A*X."))
                    print(f"  N41 equivalence OK (0 mismatches)")
                    return GatePass(child=child, m=m, pred=pred, art=art,
                                    rtl_id=rtl_id, diff=diff, hw_tag=hw_tag)

                res = evaluate_tree(0)
                first_verdict = res.verdict if isinstance(res, GateFail) else "PASS"
                if res.child is not None:
                    proposed_state = res.child.canonical()
                proposed_rtl = record.get("rtl_digest")

                # ==== N71 -> N73: iterative repair ===============================
                #
                # classify -> brief -> repair turn -> RE-EVALUATE -> ... until the
                # tree passes, the repairer reports NOT_ACTIONABLE, the attempt
                # budget (total and per class) runs out, or a repair round comes
                # back as a revert / no-edit / duplicate. Only the AGENT arm is
                # repaired: iteration 1 is the baseline, and a control arm has no
                # model and is scored on its proposals alone.
                repair_log: list = []
                stop_reason = ""
                last_repair_text = ""
                _budget = (args.repair_budget if (repair_llm is not None
                                                  and arm is None and it > 1) else 0)
                _per_class: dict = {}
                while isinstance(res, GateFail):
                    fc = recovery.classify(res.verdict, stderr=res.stderr)
                    if not _budget:
                        break
                    if not res.repairable or res.verdict not in recovery.REPAIRABLE:
                        stop_reason = f"{res.verdict} is not a repairable verdict"
                        break
                    if not fc.to_agent:
                        stop_reason = (f"classified {fc.name}: not a design failure, "
                                       f"never shown to a model")
                        print(f"  N71 class={fc.name} -- NOT a design failure; no repair")
                        break
                    if len(repair_log) >= _budget:
                        stop_reason = f"repair budget ({_budget}) exhausted"
                        break
                    if _per_class.get(fc.name, 0) >= fc.retries:
                        stop_reason = (f"class {fc.name} cap ({fc.retries} "
                                       f"attempt(s)) reached")
                        break
                    k = len(repair_log) + 1
                    _per_class[fc.name] = _per_class.get(fc.name, 0) + 1
                    shrunk = res.shrunk
                    if shrunk is None and fc.shrink and record.get("metrics"):
                        shrunk = recovery.shrink_divergence(
                            (record["metrics"] or {}).get("counters", {}) or {})
                    _cur = res.child.canonical() if res.child is not None else None
                    brief = agent.load_prompt(
                        "task/repair.md",
                        ITERATION=str(it), ATTEMPT=str(k), MAX_ATTEMPTS=str(_budget),
                        VERDICT=res.verdict, FAILURE_CLASS=fc.name,
                        RECHECK=fc.recheck, CLASS_NOTE=fc.note,
                        EVIDENCE=recovery.evidence_text(
                            fc, stderr=res.stderr, violations=res.violations,
                            shrunk=shrunk, extra=res.evidence),
                        MUTATION=recovery.mechanism_text(proposer_text),
                        MUTATED_FIELDS=(recovery.mutated_fields_text(
                            parent.canonical(), proposed_state)
                            if proposed_state is not None else
                            "(not parsed: the scope gate runs before the state is read)"),
                        PARENT_STATE=parent.to_json(),
                        # Only where the failing tree differs from the parent: the
                        # full dump is the parent's, above, and 34 repeated fields
                        # bury the few that matter.
                        PROPOSED_STATE=(json.dumps(
                            {k_: v_ for k_, v_ in _cur.items()
                             if parent.canonical().get(k_) != v_}, indent=2)
                            if _cur is not None else "(not parsed yet)"),
                        TOUCHED="\n".join(
                            p_ for p_ in (record.get("touched_paths") or [])
                            if p_ in t0.WRITABLE_PATHS or p_ not in harness_paths
                        ) or "(none)",
                        PRIOR_ATTEMPTS=recovery.attempts_text(repair_log),
                        CHIPYARD=C.CHIPYARD_PATH,
                        PARAMS_PATH=os.path.join(C.CHIPYARD_PATH, C.PARAMS_FILE_REL),
                        PE_PATH=os.path.join(C.CHIPYARD_PATH, C.RTL_FILES_REL[0]),
                        ZBU_PATH=os.path.join(C.CHIPYARD_PATH, C.RTL_FILES_REL[1]),
                    )
                    print(f"  N73 repair attempt {k}/{_budget} "
                          f"(verdict={res.verdict}, class={fc.name})")
                    _t_rep = time.time()
                    _text, _ok = repair_turn(repair_llm, tools, brief)
                    last_repair_text = _text
                    (run_dir / f"repair_{it:03d}_{k}.md").write_text(
                        f"<!-- work order -->\n{brief}\n\n<!-- transcript -->\n{_text}")
                    rep = recovery.parse_repair_report(_text)
                    att = recovery.RepairAttempt(
                        attempt=k, failure_class=fc.name, verdict_before=res.verdict,
                        status=rep.get("status", ""), confidence=rep.get("confidence"),
                        files=rep.get("files", []), compiled=rep.get("compiled", ""),
                        call_ok=_ok)
                    repair_log.append(att)
                    print(f"  N73 repairer reported status={att.status or '(none)'} "
                          f"confidence={att.confidence}")
                    if not _ok:
                        att.verdict_after = res.verdict
                        att.evidence_after = "(the repair call failed; nothing re-evaluated)"
                        stop_reason = "repair call failed"
                        break
                    if att.status == "NOT_ACTIONABLE":
                        att.verdict_after = res.verdict
                        att.evidence_after = "(reported NOT_ACTIONABLE; not re-evaluated)"
                        stop_reason = "repairer reported NOT_ACTIONABLE"
                        break
                    _before = res
                    res = evaluate_tree(k)
                    att.wall_s = round(time.time() - _t_rep, 1)
                    _after = res.child.canonical() if res.child is not None else None
                    if _cur is not None and _after is not None:
                        att.changed = ", ".join(
                            f"{f_}: {a_!r} -> {b_!r}" for f_, (a_, b_)
                            in recovery.mutated_fields(_cur, _after).items())
                    if isinstance(res, GatePass):
                        att.verdict_after = "PASS"
                        print(f"  N73 repair SUCCEEDED on attempt {k}: every gate "
                              f"passes (first failure was {first_verdict})")
                        break
                    att.verdict_after = res.verdict
                    att.evidence_after = recovery.evidence_text(
                        recovery.classify(res.verdict, stderr=res.stderr),
                        stderr=res.stderr, violations=res.violations,
                        shrunk=res.shrunk, extra=res.evidence, context_lines=15)
                    if res.verdict in ("REPAIR_REVERTED", "REPAIR_NO_EDIT", "DUPLICATE"):
                        # The repaired tree is not a design worth recording: the
                        # failure that stands is the last one actually measured.
                        stop_reason = f"repair round came back {res.verdict}"
                        res = _before
                        break

                if repair_log:
                    record["repair"] = {
                        "first_verdict": first_verdict,
                        "attempts": [a_.to_dict() for a_ in repair_log],
                        "budget": _budget,
                        "outcome": ("repaired" if isinstance(res, GatePass)
                                    else "failed"),
                        "stop_reason": stop_reason,
                    }
                    record["repair"]["proposed_state_hash"] = (
                        DesignState.from_dict(proposed_state).state_hash()
                        if proposed_state is not None else None)

                if isinstance(res, GateFail):
                    # ---- final failure: record it once, roll back exactly ----
                    _fc = recovery.classify(res.verdict, stderr=res.stderr)
                    record["verdict"] = res.verdict
                    record["failure_class"] = _fc.name
                    record["failure_charged"] = _fc.charged
                    record.update(res.extra or {})
                    record["wall_clock_s"] = round(time.time() - t_start, 2)
                    (run_dir / f"iter_{it:03d}.json").write_text(
                        json.dumps(record, indent=2))
                    rollback_to_parent()
                    print(f"  => {res.verdict} stands"
                          + (f" after {len(repair_log)} repair attempt(s): {stop_reason}"
                             if repair_log else "")
                          + f"; tree rolled back to parent {parent.state_hash()}")
                    diagnosis = res.diagnosis or f"{res.verdict}"
                    if not _fc.to_agent:
                        # An OOM or a lost worker is NOT a design failure, and
                        # saying otherwise teaches the proposer to abandon a
                        # mutation family that was fine.
                        diagnosis = ("the previous iteration hit an INFRASTRUCTURE "
                                     f"failure ({_fc.name}) at {res.verdict}, not a "
                                     "design failure. Your mutation was not evaluated; "
                                     "nothing about it is known to be wrong. The tree "
                                     "has been rolled back to the parent.")
                    elif res.verdict in recovery.REPAIRABLE:
                        _tried = (f" {len(repair_log)} repair attempt(s) did not fix it"
                                  f" ({stop_reason})." if repair_log else "")
                        diagnosis += (f"\n\nYour mutation failed at {res.verdict}.{_tried} "
                                      "The tree has been ROLLED BACK to the parent design, "
                                      "so your change is NOT present: start from the parent.")
                    if (repair_log and repair_log[-1].status == "NOT_ACTIONABLE"
                            and last_repair_text):
                        _rc = recovery.root_cause_text(last_repair_text)
                        if _rc:
                            diagnosis += ("\n\nTHE REPAIR AGENT'S ROOT-CAUSE ANALYSIS (it "
                                          "found no fix that keeps your mechanism): " + _rc)
                    # Remember the failed design by what it IS, so the proposer
                    # sees it in the history and a repeat is explained, not just
                    # refused. Only MEASURED designs used to reach the history.
                    if (res.verdict not in _NO_DESIGN and res.child is not None
                            and record.get("state_hash")):
                        _reason = failure_reason(record, run_dir)
                        if record.get("rtl_digest"):
                            seen_info[(record["state_hash"], record["rtl_digest"])] = (
                                it, res.verdict, _reason)
                        history["iterations"].append(
                            {"iteration": it, "state": record["state_hash"],
                             "mutation": {f: [getattr(parent, f), getattr(res.child, f)]
                                          for f in parent.diff_from(res.child)},
                             "verdict": res.verdict, "reason": _reason})
                        history_path.write_text(json.dumps(history, indent=2))
                    if _fc.to_agent:
                        diagnosis += tried_summary(history)
                        diagnosis += proposer_feedback(None, pred_tally, False,
                                                       cand_pool, tried_changes)
                    record["diagnosis"] = diagnosis
                    (run_dir / f"iter_{it:03d}.json").write_text(
                        json.dumps(record, indent=2))
                    continue

                child, m, pred, art, rtl_id = (res.child, res.m, res.pred,
                                               res.art, res.rtl_id)
                evaluated_hw_tag = res.hw_tag
                evaluated_diff = res.diff

                # ---- N52 T3 synthesis: area and Fmax, measured ------------------
                # Keyed on hw_hash, so a software-only mutation reuses the previous
                # synthesis instead of paying for it again. Failure here is NOT
                # fatal: the iteration falls back to T1's predicted area/period and
                # says so in the record, because a broken synthesis run is a reason
                # to lose precision, not a reason to lose the measurement N50 just
                # made.
                area_um2, period_ns, area_src = pred.area_um2, pred.period_ns, "T1_MODEL"
                # From the counters this iteration actually produced.
                _activity = measured_activity(child, m)
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
                      if (pending_synth.get("hw_tag") == evaluated_hw_tag
                              and "ref" in pending_synth):
                          # Dispatched when elaboration passed; usually finished
                          # long before the simulation did.
                          _t_join = time.time()
                          syn = get(pending_synth["ref"])
                          _dispatch = "parallel"
                          print(f"  N52 T3 joined the parallel synthesis "
                                f"(dispatched {(_t_join - pending_synth['t']) / 60:.1f} min "
                                f"ago, waited {(time.time() - _t_join) / 60:.1f} min)")
                      else:
                          # Sequential: measured power is being scored, or the
                          # parallel path is switched off. Annotated with THIS
                          # design's measured activity, and the activity is in the
                          # tag: the same netlist at another toggle rate is a
                          # different power number.
                          syn = get(synth_recipe.synthesize_recipe.chia_remote(
                              dict(art.generated_src_files),
                              **synth_kwargs(art, args, _activity),
                              _chia_tag=synth_tag(child, rtl_id, args.synth_tech,
                                                  args.synth_clock_ns, _activity)))
                          _dispatch = "sequential"
                      record["t3_synthesis"] = {
                          "success": syn.success, "top_module": syn.top_module,
                          "technology": args.synth_tech, "area_um2": syn.area_um2,
                          "power_total_w": syn.power_total_w,
                          "power_internal_w": syn.power_internal_w,
                          "power_switching_w": syn.power_switching_w,
                          "power_leakage_w": syn.power_leakage_w,
                          "power_activity_source": syn.power_activity_source,
                          "power_activity": syn.power_activity,
                          "sta_tail": syn.sta_tail,
                          "cone_files": syn.cone_files,
                          "staged_files": syn.staged_files,
                          "cell_count": syn.cell_count, "seq_cells": syn.seq_cell_count,
                          "clock_target_ns": syn.clock_target_ns,
                          "worst_slack_ns": syn.worst_slack_ns,
                          "fmax_mhz": syn.fmax_mhz, "returncode": syn.returncode,
                          "dispatch": _dispatch,
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
                          # A NEGATIVE SLACK LARGER THAN THE TARGET MEANS THE
                          # TIMING RESULT IS NOT USABLE, and must not be turned
                          # into a period. Measured 2026-09-24: slack came back
                          # -5150.49 ns against a 2.0 ns target, i.e. fmax
                          # 0.194 MHz. Dividing 1000 by that gives a 5152 ns
                          # period, and since E = P x cycles x period, energy
                          # came out 5.4 J instead of ~95 uJ -- a 2500x
                          # inflation produced entirely by trusting a number
                          # the tool had already flagged as hopeless.
                          #
                          # A design that misses its target by more than 2x has
                          # not been meaningfully timed (unconstrained paths
                          # through blackboxed SRAM macros are the usual
                          # cause). Keep the target period and say so, exactly
                          # as when STA produced nothing at all.
                          _slack = syn.worst_slack_ns
                          _fmax_ok = bool(syn.fmax_mhz) and (
                              _slack is None or _slack > -syn.clock_target_ns)
                          if _fmax_ok:
                              period_ns = 1000.0 / syn.fmax_mhz
                          elif syn.fmax_mhz:
                              period_ns = syn.clock_target_ns
                              area_src = f"{area_src}_TIMING_UNUSABLE"
                              print(f"  N52 timing REJECTED: slack={_slack:.1f}ns vs "
                                    f"{syn.clock_target_ns}ns target "
                                    f"(fmax {syn.fmax_mhz:.3f}MHz implausible); "
                                    f"using the target period for energy")
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

                # ---- THE POWER TIER -------------------------------------------
                # Until 2026-09-24 the comment here read "E stays T1's: there is
                # no power tier", and the E that reached the Pareto front came
                # from three constants (ENERGY_PJ_PER_MAC/SRAM_BYTE/DRAM_BYTE).
                # Now, whenever synthesis produced a power figure, energy is
                # computed from it instead:
                #
                #     E [pJ] = P [W] x cycles x period_ns x 1e3
                #
                # Both inputs are measured -- P by OpenSTA on the mapped
                # netlist, cycles by the simulator -- so the product is too.
                # `energy_source` records which path ran, and the fallback to
                # the model is explicit rather than silent, because a run whose
                # energy quietly changed meaning mid-way is unpublishable.
                # WHICH ENERGY IS SCORED is an explicit choice, not a silent
                # preference. SPARSECRAFT_ENERGY_SOURCE=measured switches the
                # Pareto objective to OpenSTA's power; the DEFAULT is the T1
                # model, and the reason is measured, not stylistic:
                #
                #   model, on-chip only   0.1211 W
                #   OpenSTA (same scope)  9.8241 W   -> 81x
                #
                # which decomposes exactly into (a) ~15x from annotating every
                # net with the MAC-array utilisation instead of propagating
                # from the primary inputs, and (b) ~5.3x because t1_model
                # charges ONLY macs_issued and SRAM bytes and has no term at
                # all for control, DMA, TLB, xactTracker, clock tree or
                # leakage -- roughly 90% of the 2.1M cells. (b) is the model
                # being wrong; (a) was a harness bug. Until the activity is
                # annotated from a gate-level VCD, the OpenSTA figure is
                # RECORDED on every iteration but does not move the front.
                energy_pj, energy_src = er.energy_pj, "T1_MODEL"
                power_w = er.power_w
                _pw = record.get("t3_synthesis", {}).get("power_total_w")
                _pa = record.get("t3_synthesis", {}).get("power_activity_source")
                record["t3_power_w"] = _pw            # always recorded
                record["t3_energy_pj"] = (float(_pw) * m.cycles * period_ns * 1e3
                                          if _pw and _pw > 0 else None)
                if _pw and _pw > 0 and os.environ.get(
                        "SPARSECRAFT_ENERGY_SOURCE", "model").lower() == "measured":
                    power_w = float(_pw)
                    energy_pj = power_w * m.cycles * period_ns * 1e3
                    energy_src = f"T3_OPENSTA_{(_pa or 'unknown').upper()}"
                record["energy_pj"] = energy_pj
                record["energy_source"] = energy_src
                record["power_w"] = power_w

                _perf_gops = er.perf_gops
                _ppw = (_perf_gops / power_w) if power_w > 0 else 0.0
                print(f"  N53 energy={energy_pj/1e6:,.2f} uJ  power={power_w:.4f} W  "
                      f"perf={_perf_gops:.2f} GOPS  perf/W={_ppw:.2f} GOPS/W  "
                      f"[{energy_src}]")
                print(f"      mac_eff={er.mac_efficiency:.1%} "
                      f"(useful/issued)  breakdown mac/sram/dram = "
                      f"{er.breakdown['mac_pct']:.0f}/{er.breakdown['sram_pct']:.0f}/"
                      f"{er.breakdown['dram_pct']:.0f}%"
                      f"{'' if er.dram_measured else '  [DRAM MODELLED]'}"
                      f"{'' if er.gating_measured else '  [gating modelled]'}")

                # ---- N60 score + Pareto admit -----------------------------------
                # All three objectives are now measured whenever T3 ran: t from
                # the simulator, A from yosys, E from OpenSTA's power on the
                # mapped netlist. `area_source` and `energy_source` in the
                # record say which path produced each.
                point = Point(state_hash=child.state_hash(), parent_hash=parent.state_hash(),
                              t=m.cycles * period_ns, E=energy_pj, A=area_um2,
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
                    # Pass the budget explicitly. pareto.admit carries its own
                    # 4.0e6 default, so leaving it out meant T0 and N60 could
                    # disagree about the ceiling -- and they did: raising T0's
                    # constant alone would have left N60 still rejecting every
                    # design. One source of truth.
                    v, info = admit(point, front, archive, base_point,
                                    parent_reward=parent_reward, iteration=it,
                                    budget=args.iters,
                                    area_budget=t0.AREA_BUDGET_UM2)
                record["verdict"] = v.value
                record["admit_info"] = info
                # Prediction vs measurement, against the PARENT's measured
                # objectives. "better" means smaller for all four (fmax is scored
                # on the period).
                _now = {"time": point.t, "energy": energy_pj, "area": area_um2,
                        "fmax": period_ns}
                if parent_scores is not None and record.get("prediction"):
                    _psc = candidates.score_prediction(
                        record["prediction"],
                        {**_now, **{f"{k_}_parent": v_ for k_, v_ in parent_scores.items()}})
                    record["prediction_score"] = _psc
                    pred_tally[0] += _psc["hits"]
                    pred_tally[1] += _psc["scored"]
                    if _psc["scored"]:
                        print(f"  N10 prediction: {_psc['hits']}/{_psc['scored']} "
                              f"objectives right "
                              f"(run: {pred_tally[0]}/{pred_tally[1]})")
                print(f"  N60 {v.value}  front={len(front.points)}  "
                      f"niches={archive.occupancy()}")

                # ---- per-iteration scoreboard --------------------------------
                # The three objectives plus perf/W, each against the BASELINE
                # iteration, so improvement (or its absence) is visible without
                # opening the JSON.
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
                # energy_pj / power_w / _ppw, NOT er.* -- the table has to show the
                # numbers that reached the Pareto front. It printed er.* while
                # N53 printed the measured ones, so one iteration reported two
                # different energies four lines apart.
                print(f"    energy           {energy_pj/1e6:>12,.2f} uJ   {_vs(energy_pj, _b['E'])}   [{energy_src}]")
                print(f"    power            {power_w:>13.4f} W")
                print(f"    perf/W           {_ppw:>9.2f} GOPS/W   "
                      f"{_vs(er.perf_per_watt_gops_w, _b['ppw'], True)}")
                print(f"    area             {area_um2/1e6:>12.3f} mm2   {_vs(area_um2, _b['A'])}"
                      f"   [{record.get('area_source', '?')}]")
                print(f"    off-chip bytes   {m.bytes_offchip():>15,}")
                print("  " + "=" * 64)

                strategy_diag = diagnose(m)
                diagnosis = strategy_diag + tried_summary(history)
                if repair_log:
                    diagnosis += (f"\n\nNOTE: your last mutation first failed "
                                  f"{first_verdict} and was repaired in "
                                  f"{len(repair_log)} attempt(s) before it was "
                                  f"measured. The numbers above include the repair, "
                                  f"so the mechanism as you wrote it needed fixing: "
                                  f"check your next change against the same failure.")
                diagnosis += proposer_feedback(
                    record.get("prediction_score"), pred_tally,
                    v in (Verdict.ADMIT_FRONT, Verdict.ADMIT_ARCHIVE, Verdict.ADMIT_STEP),
                    cand_pool, tried_changes)
                record["diagnosis"] = diagnosis
                # Hardware counters handed to the next proposal. The agent cannot
                # read the simulator, so this is its only view of what the design
                # actually did -- keep it factual and units-explicit.
                last_counters = counters_block(m, er.energy_pj,
                                               er.perf_per_watt_gops_w, area_um2)
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
                if record.get("rtl_digest"):
                    seen_info[(child.state_hash(), record["rtl_digest"])] = (
                        it, v.value, f"measured: {m.cycles:,} cycles, "
                                     f"{energy_pj / 1e6:.2f} uJ, {area_um2 / 1e6:.4f} mm2")

                if v in (Verdict.ADMIT_FRONT, Verdict.ADMIT_ARCHIVE, Verdict.ADMIT_STEP):
                    parent, parent_reward = child, info.get("reward", parent_reward)
                    # The exact tree this design was measured from -- the repaired
                    # one, if a repair ran -- so a later rollback restores its RTL
                    # too, not just its params.
                    parent_diff = evaluated_diff
                    parent_scores = _now
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
                    rollback_to_parent()

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
