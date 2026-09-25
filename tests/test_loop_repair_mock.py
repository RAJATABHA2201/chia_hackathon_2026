#!/usr/bin/env python3
"""The N73 repair loop, end to end, against a FAKE cluster.

    python tests/test_loop_repair_mock.py        # ~10 s, no cluster needed

Drives the REAL loop.main() through nine scripted iterations. Every CHIA node
is replaced by a Python function over an in-memory "tree" (a DesignState, an
RTL string, and any out-of-scope dirt), and the proposer and repairer turns are
scripted. What is real: loop.main(), recovery.py, agent.load_prompt with the
real prompt files, T0, T1, pareto, metrics.parse.

It checks the control flow that a 10-hour unattended run cannot afford to get
wrong: repair -> re-evaluate -> pass; revert detection; NOT_ACTIONABLE stops
without a rebuild; the per-iteration budget; infra never reaches a model; a
no-edit repair; a hang routed to the hang class; exact rollback; and that each
work order carries the stated mechanism and the ledger of earlier attempts.

Runs in a SUBPROCESS when collected by a test runner, because it monkeypatches
loop's module globals and that must not leak into other tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parent.parent / "src"


def test_repair_loop_end_to_end():
    r = subprocess.run([sys.executable, str(HERE), "--run"], capture_output=True,
                       text=True, timeout=900)
    tail = (r.stdout + r.stderr)[-3000:]
    assert r.returncode == 0, tail


def _mock(env: dict, root: str) -> subprocess.CompletedProcess:
    e = {**os.environ, "PYTHONHASHSEED": "0", "MOCK_RUNROOT": root, **env}
    return subprocess.run([sys.executable, str(HERE), "--run"], capture_output=True,
                          text=True, timeout=900, env=e)


def test_resume_matches_uninterrupted():
    """Stop a run at iteration 6, --resume it, and compare with one that never stopped.

    Same verdicts, same history, same Pareto front, iteration for iteration.
    PYTHONHASHSEED pins the fake simulator's cycle counts across processes.
    """
    full, part = tempfile.mkdtemp(), tempfile.mkdtemp()
    r = _mock({"MOCK_REPORT": f"{full}/report.json"}, full)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    _mock({"MOCK_STOP_AT": "6"}, part)                     # dies at the start of 6
    done = sorted(os.listdir(f"{part}/runs/mock"))
    assert "iter_005.json" in done and "iter_006.json" not in done, done
    r = _mock({"MOCK_RESUME": "1", "MOCK_REPORT": f"{part}/report.json"}, part)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    assert "RESUMED mock at iteration 6" in r.stdout, r.stdout[-2000:]
    a = json.load(open(f"{full}/report.json"))
    b = json.load(open(f"{part}/report.json"))
    for key in ("verdicts", "history", "front"):
        assert a[key] == b[key], f"{key} differs after resume:\n{a[key]}\n{b[key]}"


if __name__ == "__main__" and "--run" not in sys.argv:
    fails = 0
    for name, fn in (("test_repair_loop_end_to_end", test_repair_loop_end_to_end),
                     ("test_resume_matches_uninterrupted", test_resume_matches_uninterrupted)):
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {name}: {e}")
    sys.exit(1 if fails else 0)

if __name__ != "__main__":
    # imported by a test runner: only the wrapper above is exposed
    pass
else:
  RUNROOT = Path(os.environ.get("MOCK_RUNROOT") or tempfile.mkdtemp(prefix="sparsecraft-mock-"))
  sys.path.insert(0, str(SRC))
  import loop                                        # noqa: E402
  from design_state import BASELINE, DesignState     # noqa: E402

  # A real measured counter set (V1 runs/final15 iteration 13), embedded so the
  # test does not depend on any run directory existing.
  REAL = {"cycles": 50780, "macs_useful": 524288, "counters": {
      "macs_issued": 8388608, "tiles_issued": 512, "nz_blocks": 512, "total_blocks": 1024,
      "dense_mode": 0, "M": 512, "K": 512, "N": 64, "dim": 16, "nnz": 8192,
      "equiv_mismatches": 0, "equiv_first_i": -1, "equiv_first_j": -1, "equiv_got": 0,
      "equiv_want": 0, "checksum": -7297, "EXE_ACTIVE_CYCLE": 38393,
      "LOAD_DMA_WAIT_CYCLE": 762, "SCRATCHPAD_A_WAIT_CYCLE": 487522,
      "SCRATCHPAD_B_WAIT_CYCLE": 499700, "RESERVATION_STATION_FULL_CYCLES": 545685,
      "MAC_GATED_TOTAL": 7864320, "RDMA_BYTES_REC": 196608, "WDMA_BYTES_SENT": 131072,
      "done": 1}}
  PE = "generators/gemmini/src/main/scala/gemmini/PE.scala"
  PARAMS = "generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala"


  # ----------------------------------------------------------- the fake tree --
  class Tree:
      def __init__(self):
          self.reset()

      def reset(self):
          self.state = None          # None until apply_design_state seeds it
          self.rtl = "rtl-baseline"
          self.extra_paths = []      # out-of-scope dirt

      def snapshot(self):
          return {"state": self.state.canonical() if self.state else None,
                  "rtl": self.rtl, "extra": list(self.extra_paths)}

      def restore(self, d):
          self.reset()
          if d:
              self.state = DesignState.from_dict(d["state"]) if d.get("state") else None
              self.rtl = d.get("rtl", "rtl-baseline")
              self.extra_paths = list(d.get("extra", []))


  TREE = Tree()
  CALLS = []                         # (node, detail) log, for assertions
  SCRIPT = {}                        # iteration -> scenario dict
  CUR = {"it": 0, "round": 0}


  def h(s):
      return hashlib.sha256(s.encode()).hexdigest()[:16]


  class Node:
      """Stands in for a @ChiaFunction: .options() -> self, .chia_remote() -> value."""
      def __init__(self, name, fn):
          self.name, self.fn = name, fn

      def options(self, **kw):
          return self

      def chia_remote(self, *a, **kw):
          CALLS.append((self.name, CUR["it"]))
          return self.fn(*a, **kw)


  def scen():
      return SCRIPT.get(CUR["it"], {})


  # --- diff_nodes
  def _reset_and_apply(diff):
      TREE.restore(diff)
      return (0, "ok")


  def _changed_paths():
      out = []
      if TREE.state is not None:
          out.append(PARAMS)
      if TREE.rtl != "rtl-baseline":
          out.append(PE)
      return out + TREE.extra_paths


  def _collect_diff():
      return (0, TREE.snapshot())


  fake_diff_nodes = types.SimpleNamespace(
      reset_and_apply_diff=Node("reset_and_apply_diff", _reset_and_apply),
      changed_paths=Node("changed_paths", _changed_paths),
      collect_diff=Node("collect_diff", _collect_diff))


  # --- nodes
  def _read_design_state():
      return TREE.state


  def _apply_design_state(sj):
      TREE.state = DesignState.from_dict(json.loads(sj))
      return types.SimpleNamespace(wrote=[PARAMS], message="seeded")


  def _gate(name, default):
      """Scenario lookup: SCRIPT[it][name] is a list consumed one per round."""
      seq = scen().get(name)
      if not seq:
          return default
      return seq.pop(0) if len(seq) > 1 else seq[0]


  def _compile():
      ok = _gate("compile", True)
      if ok is True:
          return {"ok": True, "returncode": 0, "errors": "", "stdout_tail": ""}
      return {"ok": False, "returncode": 1, "errors": ok, "stdout_tail": ""}


  def _elaborate(sj, collect_src=False, _chia_tag=None):
      r = _gate("elab", True)
      ok = r is True
      return types.SimpleNamespace(success=ok, returncode=0 if ok else 1,
                                   stderr="" if ok else r,
                                   generated_src_files={})


  def _netlist(_chia_tag=None):
      return {"ok": True, "digest": h(_chia_tag), "n_files": 654}


  def _build_kernel(sj, ks, dh, _chia_tag=None):
      return {"success": True, "returncode": 0, "stderr": "", "binary_bytes": 1,
              "binary_content": b"", "binary_name": "spmm"}


  def _simulate(art, kern, timeout_seconds=0, _chia_tag=None):
      c = dict(REAL["counters"])
      mism = _gate("equiv", 0)
      if mism is None:
          # a hang: the kernel never reaches its printf block
          return types.SimpleNamespace(log="bbl loader\n[timeout]\n", returncode=124,
                                       success=False)
      c["equiv_mismatches"] = mism
      if mism:
          c.update(equiv_first_i=0, equiv_first_j=0, equiv_got=380, equiv_want=190)
      # cycles depend on the state so different designs measure differently
      cyc = REAL["cycles"] - (hash(TREE.rtl) % 97) - (TREE.state.k_chunk if TREE.state else 0)
      log = f"SPARSECRAFT cycles = {cyc}\nSPARSECRAFT macs_useful = {REAL['macs_useful']}\n"
      log += "".join(f"SPARSECRAFT {k} = {v}\n" for k, v in c.items())
      return types.SimpleNamespace(log=log, returncode=0, success=True)


  def _synth(src_files, top_module=None, clock_period_ns=2.0, activity=None, _chia_tag=None):
      return types.SimpleNamespace(
          success=True, top_module=top_module, area_um2=2_383_982.0,
          power_total_w=252.8, power_internal_w=1.0, power_switching_w=1.0,
          power_leakage_w=0.1, power_activity_source="default" if activity is None else "measured",
          power_activity=activity, sta_tail="", cone_files=150, staged_files=150,
          cell_count=2_088_594, seq_cell_count=93_909, clock_target_ns=clock_period_ns,
          worst_slack_ns=-4995.1, fmax_mhz=0.2, returncode=0, cells_by_type={}, stderr="")


  fake_synth_recipe = types.SimpleNamespace(synthesize_recipe=Node("synthesize_recipe", _synth))


  fake_nodes = types.SimpleNamespace(
      read_design_state=Node("read_design_state", _read_design_state),
      apply_design_state=Node("apply_design_state", _apply_design_state),
      apply_rtl_params=Node("apply_rtl_params", lambda sj: {"changed": False}),
      rtl_digest=Node("rtl_digest", lambda: h(TREE.rtl)),
      rtl_compile_check=Node("rtl_compile_check", _compile),
      elaborate=Node("elaborate", _elaborate),
      netlist_digest=Node("netlist_digest", _netlist),
      build_kernel=Node("build_kernel", _build_kernel),
      simulate=Node("simulate", _simulate),
      state_from_tree=lambda parsed: parsed)


  # --- the two agents
  def _apply_edit(edit):
      """edit: dict(state={field: value}, rtl=str, extra=[paths], clear_extra=bool)"""
      if not edit:
          return
      if edit.get("state"):
          TREE.state = TREE.state.mutate(**edit["state"])
      if edit.get("rtl"):
          TREE.rtl = edit["rtl"]
      if edit.get("extra"):
          TREE.extra_paths += edit["extra"]
      if edit.get("clear_extra"):
          TREE.extra_paths = []


  class FakeLLM:
      def __init__(self, role):
          self.role = role
          self.prompt = Node(f"llm:{role}", self._prompt)
          self.briefs = []

      def _prompt(self, _self, msg, tools):
          s = scen()
          if self.role == "propose":
              self.briefs.append(msg)
              _apply_edit(s.get("propose"))
              it_ = CUR["it"]
              text = ("reasoning...\n### ==CANDIDATES==\n"
                      f"technique: CONFIG\nchange: implemented move {it_}\nrationale: r\n"
                      "time: flat\nenergy: flat\narea: better\n\n"
                      f"technique: T-B\nchange: alternative A from iter {it_}\n"
                      "rationale: zero granules in A\ntime: flat\nenergy: better\narea: worse\n\n"
                      "### ==MUTATION==\ntechnique: CONFIG\n"
                      f"change: implemented move {it_}\ncompiled: PASS\n\n"
                      "### ==PREDICTION==\nmechanism.\n  time: flat\n  energy: flat\n"
                      "  area: better\n")
          else:
              self.briefs.append(msg)
              reps = s.setdefault("repairs", [])
              r = reps.pop(0) if reps else {"status": "NOT_ACTIONABLE"}
              _apply_edit(r.get("edit"))
              text = ("## Root cause\nx. Confidence: 4/5\n## Fix\n- y\n"
                      "## Verification\nz\n## Self-audit\n- no\n\n"
                      f"### ==REPAIR==\nstatus: {r['status']}\nconfidence: 4\n"
                      "class: x\nfiles: none\ncompiled: PASS\npreserved: yes\n")
          return types.SimpleNamespace(result=text, returncode=0, success=True,
                                       stderr="", stream_result="")


  LLMS = {}


  def _make_llm(system_file, backend=None, model=None, log_dir=None):
      role = "repair" if "repairer" in system_file else "propose"
      LLMS[role] = FakeLLM(role)
      # the REAL system prompt must resolve (includes, files)
      import agent as _agent
      LLMS[role].system = _agent.read_prompt(system_file)
      return LLMS[role]


  def _get(x):
      return x


  class _Cache:
      def __init__(self):
          self.read = Node("cache.read", lambda tag: (False, None))
          self.has = Node("cache.has", lambda tag: False)


  def _before_iteration_hook():
      pass


  # ----------------------------------------------------------------- patching --
  loop.C.RUN_DIR = str(RUNROOT / "runs")
  loop.C.CACHE_DIR = str(RUNROOT / "cache")
  loop.get = _get
  loop.nodes = fake_nodes
  loop.diff_nodes = fake_diff_nodes
  loop.synth_recipe = fake_synth_recipe
  loop.ray = types.SimpleNamespace(init=lambda **kw: None, get=lambda x: x)
  loop.placement_group = lambda *a, **kw: types.SimpleNamespace(ready=lambda: None)
  loop.remove_placement_group = lambda pg: None
  loop.PlacementGroupSchedulingStrategy = lambda **kw: None
  loop.start_cache = lambda **kw: _Cache()
  loop.stop_cache = lambda: None
  loop.start_collector = lambda **kw: None
  loop.stop_collector = lambda: None
  loop.Bypass = lambda **kw: None
  loop.get_active_bypass = lambda: types.SimpleNamespace(
      set_provider=lambda *a: None, set_cond=lambda *a: None)
  loop.agent.describe = lambda backend=None: {
      "ready": True, "backend": "fake", "model": "fake", "credential_var": "-",
      "credential_present": True, "searched": [], "package": None,
      "credential_hint": ""}
  loop.agent.make_editor = lambda pg: types.SimpleNamespace(stop=lambda: None)
  loop.agent.make_sealed_tools = lambda a, b: []
  loop.agent.make_llm = _make_llm

  # the iteration counter, for the scenario lookups: wrap assert_integrity, which
  # is the first thing every iteration calls
  _real_ai = loop.assert_integrity


  def _ai(man):
      CUR["it"] += 1
      if os.environ.get("MOCK_STOP_AT") and CUR["it"] == int(os.environ["MOCK_STOP_AT"]):
          raise KeyboardInterrupt("mock: simulated stop")
      return _real_ai(man)


  loop.assert_integrity = _ai

  # ---------------------------------------------------------------- scenarios --
  ERR = "[error] /home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/PE.scala:147:31: value === is not a member of type parameter T"
  SCRIPT.update({
      # 1: baseline
      # 2: compile fails once, repair fixes it -> measured
      2: {"propose": {"state": {"k_chunk": 8}, "rtl": "rtl-ta-v2"},
          "compile": [ERR, True],
          "repairs": [{"status": "FIXED", "edit": {"rtl": "rtl-ta-v2-fixed"}}]},
      # 3: T0 illegal, repair moves the field all the way back -> REVERTED
      3: {"propose": {"state": {"k_chunk": 1024}},
          "repairs": [{"status": "FIXED", "edit": {"state": {"k_chunk": 8}}}]},
      # 4: divergence, repairer says NOT_ACTIONABLE -> no re-evaluation
      4: {"propose": {"state": {"b_blocks": 2}},
          "equiv": [4096],
          "repairs": [{"status": "NOT_ACTIONABLE"}]},
      # 5: scope violation, repair removes the stray path -> measured
      5: {"propose": {"state": {"acc_capacity_kb": 32}, "extra": ["generators/gemmini/src/main/scala/gemmini/Scratchpad.scala"]},
          "repairs": [{"status": "FIXED", "edit": {"clear_extra": True}}]},
      # 6: compile fails every time -> class cap / budget, then rollback
      6: {"propose": {"rtl": "rtl-broken"},
          "compile": [ERR, ERR, ERR, ERR],
          "repairs": [{"status": "FIXED", "edit": {"rtl": "rtl-broken-1"}},
                      {"status": "FIXED", "edit": {"rtl": "rtl-broken-2"}},
                      {"status": "FIXED", "edit": {"rtl": "rtl-broken-3"}}]},
      # 7: elaboration OOM -> infra, never repaired
      7: {"propose": {"state": {"sp_banks": 8}},
          "elab": ["java.lang.OutOfMemoryError: out of memory"]},
      # 8: compile fails, repair makes NO edit -> REPAIR_NO_EDIT
      8: {"propose": {"rtl": "rtl-x"}, "compile": [ERR, ERR],
          "repairs": [{"status": "FIXED", "edit": {}}]},
      # 9: hang (no equiv line) -> hang class, repair NOT_ACTIONABLE
      9: {"propose": {"rtl": "rtl-hang"}, "equiv": [None],
          "repairs": [{"status": "NOT_ACTIONABLE"}]},
      # 10: the proposer re-proposes iteration 9's failed design -> DUPLICATE,
      # and the refusal must say WHICH iteration and WHY it failed
      10: {"propose": {"rtl": "rtl-hang"}},
  })
  N = 10

  sys.argv = ["loop.py", "--iters", str(N), "--synth", "--run-name", "mock",
              "--cache-scope", "off", "--backend", "claude"]
  run = RUNROOT / "runs" / "mock"
  if os.environ.get("MOCK_RESUME"):
      sys.argv.append("--resume")
      CUR["it"] = len(list(run.glob("iter_*.json")))
  rc = loop.main()
  recs = {i: json.loads((run / f"iter_{i:03d}.json").read_text()) for i in range(1, N + 1)}
  if os.environ.get("MOCK_REPORT"):
      h = json.loads((run / "history.json").read_text())
      json.dump({"verdicts": {i: recs[i].get("verdict") for i in recs},
                 "history": [[e.get("iteration"), e.get("state"), e.get("verdict")]
                             for e in h.get("iterations", [])],
                 "front": h.get("front")},
                open(os.environ["MOCK_REPORT"], "w"), default=str)
      sys.exit(0 if rc == 0 else 1)

  fails = []


  def expect(cond, msg):
      print(("PASS " if cond else "FAIL ") + msg)
      if not cond:
          fails.append(msg)


  expect(rc == 0, f"main() returned {rc}")
  expect("system" in LLMS["repair"].__dict__ and len(LLMS["repair"].system) > 20000,
         "repairer system prompt resolved from the real files")
  v = {i: r.get("verdict") for i, r in recs.items()}
  print("verdicts:", v)
  expect(v[1] == "ADMIT_FRONT", "iter 1 baseline admitted")
  r2 = recs[2]
  expect(v[2] not in ("COMPILE_FAILED",) and r2.get("repair", {}).get("outcome") == "repaired",
         f"iter 2 repaired and measured (verdict {v[2]})")
  expect(r2["repair"]["first_verdict"] == "COMPILE_FAILED", "iter 2 records first verdict")
  expect(r2["repair"]["attempts"][0]["verdict_after"] == "PASS", "iter 2 attempt 1 -> PASS")
  expect((run / "diff_002_repair1.json").is_file(), "iter 2 repaired diff kept beside the original")
  expect(v[3] == "T0_ILLEGAL" and recs[3]["repair"]["stop_reason"].startswith("repair round came back REPAIR_REVERTED"),
         f"iter 3 revert detected, T0_ILLEGAL stands ({recs[3].get('repair',{}).get('stop_reason')})")
  expect(v[4] == "EQUIV_FAILED" and recs[4]["repair"]["stop_reason"] == "repairer reported NOT_ACTIONABLE",
         "iter 4 NOT_ACTIONABLE stops without re-evaluating")
  expect(recs[4]["repair"]["attempts"][0]["verdict_after"] == "EQUIV_FAILED", "iter 4 attempt not re-run")
  expect(recs[5].get("repair", {}).get("outcome") == "repaired", f"iter 5 scope repaired (verdict {v[5]})")
  expect(v[6] == "COMPILE_FAILED" and len(recs[6]["repair"]["attempts"]) == 3,
         f"iter 6 budget of 3 spent then COMPILE_FAILED ({len(recs[6].get('repair',{}).get('attempts',[]))} attempts)")
  expect(v[7] == "ELABORATION_FAILED" and recs[7].get("failure_class") == "infra" and "repair" not in recs[7],
         f"iter 7 OOM classified infra and never repaired ({recs[7].get('failure_class')})")
  expect(v[8] == "COMPILE_FAILED" and recs[8]["repair"]["stop_reason"] == "repair round came back REPAIR_NO_EDIT",
         f"iter 8 no-edit repair detected ({recs[8].get('repair',{}).get('stop_reason')})")
  expect(v[9] == "EQUIV_MISSING" and recs[9]["repair"]["attempts"][0]["failure_class"] == "hang",
         "iter 9 hang routed to the hang class")
  # rollback after every final failure must restore the parent's exact tree
  expect(TREE.extra_paths == [], "no out-of-scope dirt survives")
  # the repair briefs carry the mechanism and the prior attempts
  b6 = [b for b in LLMS["repair"].briefs if "iteration 6, attempt 3" in b]
  expect(bool(b6) and "Attempt 1" in b6[0] and "Attempt 2" in b6[0]
         and "the same failure came back" in b6[0], "attempt 3 brief carries attempts 1-2 and their outcomes")
  expect(all("### ==MUTATION==" in b for b in LLMS["repair"].briefs), "every brief carries the stated mechanism")
  b3 = [b for b in LLMS["repair"].briefs if "iteration 3," in b][0]
  expect("k_chunk: 8 -> 1024" in b3 or "k_chunk: 16 -> 1024" in b3, "T0 brief lists the mutated field")
  expect("sched.k_chunk_fits_scratchpad" in b3, "T0 brief carries the violated rule")
  # --- strategy selection, prediction scoring, candidate backlog ---------
  pb = LLMS["propose"].briefs
  expect(recs[2].get("strategy", {}).get("label") == "balanced",
         f"iter 2 strategy label from the baseline counters ({recs[2].get('strategy')})")
  expect(recs[2]["strategy"]["modules"] == ["strategy/resource-sizing.md",
                                            "strategy/dataflow-tiling.md"],
         "balanced selects resource-sizing + dataflow-tiling, never re-adds T-A/T-B")
  expect("## Levers for this bottleneck: `balanced`" in pb[0]
         and "Resource sizing" in pb[0] and "Dataflow and tiling" in pb[0],
         "the proposer's work order carries the selected modules")
  expect("T-A — Zero-Gated MAC" not in pb[0], "T-A module is not duplicated into the work order")
  expect("balanced, no single bottleneck" in pb[0], "the dead band now has an actionable diagnosis")
  expect(recs[2].get("prediction_score", {}).get("scored", 0) >= 1,
         f"iter 2 prediction scored against the parent ({recs[2].get('prediction_score')})")
  expect([c["implemented"] for c in recs[2].get("candidates", [])] == [True, False],
         "iter 2 candidates parsed; the implemented move is not double-counted")
  after_fail = [b for b in pb if "MOVES YOU LISTED EARLIER" in b]
  expect(bool(after_fail) and "alternative A from iter" in after_fail[0],
         "after a failed iteration the proposer gets its untried alternatives back")
  expect(any("YOUR PREDICTION vs THE MEASUREMENT" in b for b in pb),
         "after a measured iteration the proposer sees its prediction scored")
  # --- N52 overlaps the simulation ---------------------------------------
  order1 = [n for n, i in CALLS if i == 1 and n in ("synthesize_recipe", "simulate")]
  expect(order1 == ["synthesize_recipe", "simulate"],
         f"synthesis dispatched BEFORE the simulation ({order1})")
  expect(recs[1].get("t3_synthesis", {}).get("dispatch") == "parallel",
         f"iter 1 synthesis joined from the parallel dispatch ({recs[1].get('t3_synthesis', {}).get('dispatch')})")
  expect(str(recs[1].get("area_source", "")).startswith("T3_"),
         f"area comes from synthesis, not T1 ({recs[1].get('area_source')})")
  n2 = sum(1 for n, i in CALLS if n == "synthesize_recipe" and i == 2)
  n6 = sum(1 for n, i in CALLS if n == "synthesize_recipe" and i == 6)
  expect(n2 == 1, f"iter 2: synthesis only for the repaired design that compiled ({n2})")
  expect(n6 == 0, f"iter 6: nothing synthesised for a design that never compiled ({n6})")
  # --- failed designs are remembered, and a repeat is explained ---------
  h = json.loads((run / "history.json").read_text())
  h9 = [e for e in h["iterations"] if e.get("iteration") == 9]
  expect(bool(h9) and "repair agent found it cannot work here" in h9[0].get("reason", ""),
         f"iter 9's failed design is in the history WITH its reason ({h9})")
  expect(recs[10]["verdict"] == "DUPLICATE"
         and "design from iteration 9, which was EQUIV_MISSING" in recs[10].get("diagnosis", ""),
         f"iter 10's refusal names iteration 9 and its verdict ({recs[10].get('diagnosis', '')[:200]})")
  b5 = [b for b in pb if "This iteration (5 of" in b]
  expect(bool(b5) and "NOT MEASURED:" in b5[0] and "iter 4:" in b5[0],
         "after iteration 4 failed, iteration 5's work order lists it as tried and failed")
  expect(bool(b5) and "THE REPAIR AGENT'S ROOT-CAUSE ANALYSIS" in b5[0],
         "a NOT_ACTIONABLE root cause reaches the proposer")
  print(f"\n{len(fails)} failure(s)")
  sys.exit(1 if fails else 0)
