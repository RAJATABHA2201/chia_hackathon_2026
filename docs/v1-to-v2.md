# From V1 to V2: changes and refinements

What V2 changes relative to V1 (`../sparsecraft/`, commit `7643a4b`), what is
wired and what is not, and how to verify each piece. The plan V2 started from
is `sparsecraft/docs/loop_v2_plan.md` in the V1 tree; this file records what
the V2 **code** actually does, checked against it.

## Status at a glance

| plan step | change | state in V2 |
|---|---|---|
| 0 | separate tree, isolated `runs/` and `cache/`, prompt layout | **done** |
| 1 | prompt loader with `{{include:}}`, `shared/` fragments | **done** |
| 2 | asynchronous synthesis | not started (deprioritised: throughput, not sparsity) |
| 3 | error classifier, shrinker, repair agent | **done, and rewritten as an iterative loop** (§3) |
| 4 | diagnose as a rule table plus LLM escalation | **rule table done** (every band labelled, incl. `balanced`); LLM escalation not constructed |
| 5 | K-candidate fan-out | **done as feedback**: candidates parsed, predictions scored, untried alternatives handed back (§2.5) |
| 6 | T-B measurability | **done without RTL changes**, via `zbu_model.py` (§2.3) |
| 7 | new strategy modules | **done**: selected per measured bottleneck into the work order (§2.4) |
| - | SRAM energy floor 256 KB -> 32 KB | **done** (§2.6) |
| - | debugging prompt set for the repairer | **done** (§3.1) |
| - | package reorganisation (`src/`, `scripts/`, `configs/`) | **done** (§4) |

---

## 1. Why a separate tree

V1 was running `runs/final15` when V2 began. Editing in place was unsafe for
three reasons: V1 hashes its immutable inputs every iteration and aborts on
drift; V1 and V2 would have shared `runs/` and `cache/`; and a V1-vs-V2
comparison needs V1 unmodified. **Nothing in V2 writes to the V1 tree.**

The two trees still share the **cluster** (same `configs/cluster.yaml`
contents, same container names), so they cannot run loops at the same time.

---

## 2. Changes, by area

### 2.1 Isolation (`src/constants.py`)

`PROJECT_ROOT` defaults to `/home/chia-sparsecraft/sparsecraft-v2`, so
`RUN_DIR` and `CACHE_DIR` are V2's own. `SPARSECRAFT_ROOT` still overrides.

`runtime_env()` ships modules from `SOURCE_DIR` (the directory `constants.py`
is in). An earlier V2 version joined `PROJECT_ROOT/"sparsecraft"`, which does
not exist in V2, and would have raised `FileNotFoundError` on the first
`ray.init`.

### 2.2 Prompt loading (`src/agent.py`)

- `resolve_includes()` expands `{{include: path}}` lines recursively, relative
  to `prompts/`, raising on a cycle or a missing file.
- `read_prompt()` loads a system prompt with its includes.
- `load_prompt()` renders a `${NAME}` work order and raises if the placeholders
  and the call site's arguments differ in either direction.
- `compose()` builds a system message in prompt-cache order (stable first). It
  is **not called** by the loop yet (§5).

### 2.3 T-B measurability (`src/zbu_model.py`, `src/t1_model.py`)

`t1_model`'s energy term for T-B reads a `ZBU_SKIPPED_ROWS` counter that
`CounterFile.scala` never defines, so enabling the ZBU cost area and earned
exactly 0.00 uJ: always Pareto-dominated, however well it worked. Without an
RTL change, `zbu_model.py` counts all-zero granules exactly from the
workload's own `spmm_A` data, and `t1_model` uses that derived count when the
counter is absent, recording where the number came from in `zbu_source`.

### 2.4 Diagnosis labels and strategy selection (`src/loop.py`)

`diagnose()` names the bottleneck from bounded counters; `bottleneck_label()`
turns that into `memory`, `compute`, `scratchpad`, `issue_queue` or
`balanced`. `balanced` is new: the band `dma_wait <= 0.30`, `0.40 <= exe_active
<= 0.80` used to return `unclassified`, and V1's search sat there from
iteration 5 to 15 with no actionable diagnosis.

`strategy_section()` renders the modules `BOTTLENECK_STRATEGY` selects for the
last **measured** diagnosis into the work order's `${STRATEGY}` block, minus
T-A and T-B (always in the system prompt). So `dataflow-tiling.md`,
`resource-sizing.md` and `nm-structured.md` now reach the model when the
bottleneck calls for them; `record["strategy"]` says which. The work order,
not the system prompt, carries them so the system prefix stays cached.

`system/microarchitect.md` called any config move "a wasted iteration", which
would have contradicted the lever modules; it now says a config or schedule
move is legitimate when the measured bottleneck points at it or when it pays
for or exploits a sparsity mechanism. `shared/feedback-schema.md` named
counters the harness never prints (`MACS_ISSUED`, `MAC_GATED_CYCLES`,
`ZBU_SKIPPED_TILES`); it now uses `macs_issued`, `MAC_GATED_TOTAL`, and says
T-B's skipped reads are derived, energy-only.

### 2.5 Candidates and prediction feedback (`src/candidates.py`)

The proposer lists `K` candidate moves (`--candidates`, default 3). The loop
now parses them (`record["candidates"]`), scores the implemented move's
`==PREDICTION==` against the parent's measured objectives
(`record["prediction_score"]`, direction derived from the numbers), and feeds
back into the next diagnosis: the per-objective result with a running tally,
and, after a change that was not admitted, up to three untried alternatives
the agent itself listed. The parser previously split candidates on two blank
lines while the contract asks for one, collapsing every list into one block;
it now splits at each `technique:` line and recognises the implemented move
inside the list so it is not handed back as untried.

### 2.6 SRAM energy floor (`src/t1_model.py`)

Per-access SRAM energy is `sqrt(max(SRAM, SRAM_FLOOR_BYTES) / SRAM_REF_BYTES)`
with the reference at 256 KB and the floor now 32 KB (was the reference
itself). Both the T1 filter and the scored energy use one helper,
`sram_access_scale()`. Re-scoring V1's recorded designs: baseline (320 KB)
unchanged at 95.67 uJ; its 128 KB designs 27.73 -> 21.84 uJ; its final 80 KB
design 27.73 -> 18.86 uJ. V1's rejected T-B design (iteration 10) scores
18.50 uJ under V2's model. **Energy is therefore not comparable with V1's
records for designs under 256 KB.** `t1_model.py` is an immutable input; this
changes its hash, which matters only across runs.

### 2.7 Synthesis overlaps simulation; sized for the 64 GB host

V1's final15 spent 56% of its wall clock simulating (26.6 min/iteration), 18%
synthesising (8.1 min) and 4% elaborating, strictly one after another, with the
hammer container idle during simulation and the verilator container idle during
synthesis. Synthesis needs only the elaborated sources; the one input it took
from simulation is the switching activity, which feeds only OpenSTA power, which
is recorded and not scored. So `evaluate_tree` now dispatches N52 as soon as
N12b passes and the iteration joins it after N41 (`record["t3_synthesis"]
["dispatch"] = "parallel"`). The recorded power then uses default activity.
With `SPARSECRAFT_ENERGY_SOURCE=measured` (power scored) or
`SPARSECRAFT_SYNTH_PARALLEL=0` it stays sequential and activity-annotated.
Expected saving: about 8 min per iteration.

The synthesis cache tag now carries the RTL digest (`synth_tag()`), closing the
latent key gap in §5.

Host: 61 GiB RAM, Threadripper 9970X (32 cores / 64 threads).
`VERILATOR_THREADS` stays 16. Measured on the identical baseline design: 28.4 min
at 30 threads against 19.8-22.0 min at 16 (three V1 runs), 29% slower, so the
first V2 launch was stopped after iteration 1 and restarted at 16
(`runs/v2-final15-t30-aborted`). `make -j` is capped at 12 so
a repair round's elaboration can overlap an in-flight synthesis without
approaching the RAM ceiling: the idle cluster already holds ~18 GB (13 GB of it
Ray's pre-spawned idle workers), so `-j12` keeps the worst case near 56 GB.

### 2.8 Failed designs are remembered; runs can resume

Measured in `runs/v2-final15`: iteration 5 proposed a 32x32 array, failed
equivalence, and the repairer proved (5/5) that no array size other than the
workload's 16x16 blocking can compute `Y` correctly. The proposer was told only
"wrong answer, rolled back", and `history.json` recorded measured designs only,
so the failed design was invisible to it. It re-proposed the identical design in
iterations 6 and 7 (`DUPLICATE`). Fixed:

- An evaluated design that fails a gate is appended to the history with a
  `reason` (a `NOT_ACTIONABLE` repair's root cause wins), so `tried_summary` and
  the history tool both show it as tried and failed.
- A `DUPLICATE` names the iteration it repeats and that iteration's verdict and
  reason (`seen_info`).
- A `NOT_ACTIONABLE` repair's root cause is appended to the proposer's next
  diagnosis, and the failure-path diagnosis now carries the tried list.
- `--resume` continues an existing run from its records (`reconstruct_run`): it
  replays every measured iteration through the same Pareto admission and aborts
  if any verdict differs, checks the immutable-input manifest against the run's
  own, rebuilds history, dedup set, candidates and prediction tally, restores
  the parent's exact tree, and writes `resume.json`. `v2-final15` was stopped at
  iteration 8 and resumed this way; the resumed proposer left the 32x32 design
  on its first turn. `tests/test_loop_repair_mock.py` checks that a run stopped
  at iteration 6 and resumed matches an uninterrupted one in verdicts, history
  and front.

### 2.9 T-B is credited only for skips the hardware can make

`zbu_model.derived_zbu_rows` credited zero SUB-ROW granules when the granule
was finer than a row. The ZBU hands the scratchpad one `skip` bit per read, and
the harness-owned hook in `Scratchpad.scala` suppresses the whole row on it, so
a finer granule can only ever skip rows that are entirely zero. In
`v2-final15`, iterations 10 and 11 refined the granule 16 -> 8 -> 4 and were
credited 9.6% and 5.3% energy that no build could deliver; the extra bitmap area
was real. The credit is now row-level whatever the requested granule (the
granule is still validated by T0 and still costs area). Re-scored with the fix,
the run's final design is 17.04 uJ (5.61x vs baseline), not the 14.73 uJ
(6.50x) it recorded; `scripts/summarize_runs.py` reports both.

### 2.10 Portable paths

`PROJECT_ROOT` (runs/, cache/) defaults to the checkout instead of a path on
the development host, and `scripts/run.sh` exports it so the driver and the
workers agree. `make_report.py` and `codesign_ledger.py` were V1's paper tools
over V1-era runs that are not in this repository; they live on the
`archive/v1-first-loop` branch.

---

## 3. The repair loop (N71-N73)

### 3.1 Prompts

The repairer's system prompt combines CHIA's `timing_opt` debugging
references with the first V2 draft of `repairer.md`, rewritten for this
project:

| file | content |
|---|---|
| `prompts/system/repairer.md` | role, mission, what it is given, the iterative contract, hard prohibitions, procedure, confidence protocol, tools; includes everything below |
| `prompts/debug/methodology.md` | from CHIA `common_debugging.md`: the inner loop priced with this loop's real costs, the gate ladder as layered validation, diff against the parent as last-known-good, why the scored design cannot be instrumented, root-cause discipline, tunnel vision under a one-call-per-turn transport |
| `prompts/debug/chisel-gemmini.md` | from CHIA `chisel_debugging.md`: Chisel traps and toolchain behaviours kept; BOOM material replaced by this tree's Gemmini facts (generic `PE[T]`, the forwarded operand, WS double buffering, Scala-level technique flags, markers vs fields, the ZBU port contract, `gemmini_params.h` coupling) |
| `prompts/debug/failure-playbook.md` | one section per verdict: meaning, what is re-checked and at what cost, usual causes, and what does not count as a fix; T0 rules with their formulas and the direction rule; a first-mismatch symptom table for divergences |
| `prompts/debug/repair-contract.md` | Root cause / Fix / Verification / Self-audit, then the `==REPAIR==` block the harness parses |
| `prompts/task/repair.md` | the per-attempt work order |

The first draft (`archive/v2-superseded/repairer.v2-draft.md`) had been
adapted from an attention project: it forbade `has_normalizations = false` for
a softmax path this workload does not use, allowed one writable file where
there are three, told the agent to read two reference files by path (the
sealed Claude backend has no file tool on the head), and told it to "read the
stated mechanism first" when the loop never passed it one.

The references are inlined by include rather than referenced by path because
the Claude backend runs with `--tools ""`. That also keeps them in the cached
system prefix; everything that changes per attempt is in the work order.

### 3.2 Orchestration (`src/loop.py`, `src/recovery.py`)

**Before.** Repair ran only on `COMPILE_FAILED`. Inside `attempt_recovery()`
the gate was not re-run between attempts: the function returned success as
soon as the CLI session completed, so a second attempt only happened if the
call itself crashed. The repairer was not shown the proposer's mechanism.

**Now.**

- The gate ladder N13 to N41 is one function, `evaluate_tree(round_no)`, that
  **returns** a `GateFail` or `GatePass` instead of ending the iteration with
  `continue`. Every failure is recorded, and the tree rolled back, in one place.
- After each repair turn the **whole** ladder re-runs from the scope check.
  The next turn gets the new evidence plus `recovery.attempts_text()`: what
  each earlier attempt reported, changed, and what came back.
- Repair covers every design-failure verdict: `SCOPE_VIOLATION`, `T0_ILLEGAL`,
  `COMPILE_FAILED`, `ELABORATION_FAILED`, `RTL_NOOP`, `KERNEL_BUILD_FAILED`,
  `TRIPWIRE_FAILED`, `EQUIV_FAILED`, `EQUIV_MISSING`. Never
  `AGENT_FAILED`, `NO_EDIT` or `DUPLICATE` (proposal-policy failures), and never
  an infrastructure failure.
- `recovery.revert_check()` decides reverts from the parent, proposed and
  repaired states (and RTL digests). A repair round that reverts, changes
  nothing, or lands on an already evaluated design stops the loop; the failure
  that stands is the last one actually measured.
- `==REPAIR== status: NOT_ACTIONABLE` stops without spending a rebuild.
- Budget: `--repair-budget` (default 3) per iteration, plus a per-class cap
  sized by the re-check cost (`recovery.CLASSES`).

### 3.3 Fixes made along the way

| issue | before | now |
|---|---|---|
| rollback after a rejected or failed design | `apply_design_state(parent)`: rewrote the params file only, so the agent's rejected `PE.scala` / ZBU edits stayed in the tree | `rollback_to_parent()`: reset and re-apply the parent's own diff, RTL included |
| a design that hangs | the kernel prints no counters, `metrics.parse` raises, and the iteration was filed as `INFRA_FAILURE` | routed to `EQUIV_MISSING`, class `hang`, unless the log shows an infra cause |
| `TRIPWIRE_FAILED` | set no diagnosis, so the proposer saw the previous iteration's | says what the floor was and what crossed it |
| `EQUIV_MISSING` diagnosis | "the measurement instrument is broken, not the design" | "the design hung or the simulation hit its time limit" |
| final failure caused by infra | would have been reported to the proposer as a design failure | classified first; the proposer is told its mutation was not evaluated |

Verified by `tests/test_loop_repair_mock.py` (the real `loop.main()` on a fake
cluster, nine scripted iterations) and `tests/test_recovery.py`.

---

## 4. Package reorganisation

```
before (flat)                      after
  *.py (26 modules)                  src/        17 importable modules, flat
  *.yaml (4)                         scripts/    9 entry points + run.sh
  run.sh                             configs/    4 YAML
  kernels/{spmm.c,attn_prefill.c}    kernels/    spmm.c
  prompts/ workload/ docker/         (unchanged)
  tests/ docs/ archive/              archive/legacy-kernels/attn_prefill.c
```

**The constraint that shaped it.** Ray pickles each `@ChiaFunction` by
reference under its top-level module name, so `src/` must stay a directory of
top-level modules, not a package. `runtime_env()` ships each `src/*.py`
individually, which now means exactly the 17 modules; the operator scripts
were never needed on workers and are no longer shipped.

**Where paths now come from.** `constants.py` defines `PACKAGE_DIR`,
`CONFIG_DIR`, `PROMPTS_DIR`, `KERNELS_DIR`, `WORKLOAD_DIR` and `SCRIPTS_DIR`
once. `agent.py`, `loop.py` and `t1_model.py` resolve through them; scripts put
`../src` on `sys.path`.

**Updated references.** `scripts/run.sh` (runs from the root wherever it is
invoked; `src/loop.py`, `configs/cluster.yaml`, `scripts/check_*.py`),
`scripts/check_setup.py` (reads `configs/` and `src/`), `scripts/run_synth.py`
(`configs/bypass_cache.yaml`), `scripts/report_iter.py`, every script's usage
text, `docker/build.sh`, `docker/yosys_gemmini_recipe.md`,
`docker/inventory.spec`, `configs/no_cache.yaml`, and the tests.

**Stale references found and fixed, not caused by the move:**

| file | problem |
|---|---|
| `docker/build.sh` | hardcoded `cd /home/chia-sparsecraft/sparsecraft`: building from V2 built **V1's** Dockerfile. Now self-locating. |
| `scripts/report_iter.py` | hardcoded V1's `/home/chia-sparsecraft/runs`, so a V2 run was invisible; and read `move['kind'/'hw'/'sw']` where the loop writes `class`/`hw_fields`/`sw_fields`, printing `None` for every iteration. Now defaults to `constants.RUN_DIR`, takes `--runs-dir`, and prints the changed field and any repair. |
| `tests/test_t0.py` | targeted the attention-era rule set and fields `DesignState` no longer has; it crashed at the seventh case. Rewritten against the 16 rules `t0_legality.py` emits today. |
| `loop.integrity_manifest()` | recorded `"MISSING"` for an absent immutable file, so after a move the check would compare `MISSING == MISSING` and pass while guarding nothing. Now fatal. |
| `scripts/check_setup.py` | silently skipped a source file it could not find, so the wiring probe would report success while checking nothing. Now reported. |

**Removed from main since (§2.10).** `scripts/make_report.py` and
`scripts/codesign_ledger.py` read named **historical** runs (`agent-1`,
`greedy-1`, `cd-k64b`, ...) from the shared `/home/chia-sparsecraft/runs`. That
stays their default; `SPARSECRAFT_REPORT_RUNS` / `SPARSECRAFT_LEDGER_RUNS`
override it.

**Immutable inputs.** The move changed the manifest's keys (they now carry
`src/`) and one hash: `t1_model.py`, whose only change is resolving the
workload header through `constants.WORKLOAD_DIR` instead of `__file__`.
`t0_legality.py`, `pareto.py`, `metrics.py` and `kernels/spmm.c` are
byte-identical. The manifest is taken at run start, so this matters only when
comparing a V2 record's manifest against a V1 one.

---

## 5. Known gaps

- **The diagnostician LLM is not constructed.** `system/diagnostician.md` is a
  stub; the rule table labels every band.
- **Stale T0 rule.** `fusion.no_materialised_S` (keep `has_normalizations`)
  comes from the attention project. In `v2-final15` iteration 13 it blocked
  removing normalisation hardware SpMM never uses, a genuine area saving.
- **Missing T0 rule.** The array dimension must equal the workload's block
  dimension (`SPMM_DIM`); today that is discovered after a 30 min simulation
  (`v2-final15` iteration 5) instead of in microseconds.
- **The proposer does not see the T0 formulas** (only the repairer's playbook
  has them), so it spent iteration 14 on a granule the bitmap budget can never
  admit at this array size.
- **`agent.compose()` is unused**: strategy modules go into the work order
  instead, deliberately (§2.4).
- **No cache provider is registered for `synthesize_recipe`** (the loop
  registers `"synthesize"`), so synthesis always runs. Its tag now carries the
  RTL digest, so registering one would be safe.
- **`netlist_digest` is never served from cache** for the same reason, so on
  an elaboration cache hit it hashes whatever was elaborated last on disk. This
  affects the recorded digest, not any measurement.

---

## 6. Running and verifying V2

```bash
cd /home/chia-sparsecraft/sparsecraft-v2
source ~/miniforge3/etc/profile.d/conda.sh && conda activate chia_env
export PATH="$HOME/bin:$PATH"          # docker -> podman shim, claude shim
```

No cluster needed:

```bash
python tests/test_t0.py
python tests/test_recovery.py
python tests/test_candidates.py
python tests/test_t1_energy.py
python tests/test_loop_repair_mock.py
python -c "import sys; sys.path.insert(0,'src'); import constants as C; print(C.RUN_DIR)"
#   must print /home/chia-sparsecraft/sparsecraft-v2/runs
```

Preflight and smoke:

```bash
python scripts/check_llm.py
python scripts/check_setup.py --quick
python scripts/smoke_agent.py --backend claude      # needs the cluster up
```

A run, against a cluster that is already up:

```bash
scripts/run.sh --iters 15 --synth --no-up --no-down -- --run-name <name>
python scripts/report_iter.py <name>                # full per-iteration dump
```

`--no-up --no-down` is **required** whenever another loop's cluster is up.

---

## 7. Invariants V2 must not break

1. **The agent's writable set is three files**: `SparseCraftParams.scala`,
   `PE.scala`, `SparseCraftSparsity.scala`. Enforced by
   `t0.check_patch_scope` after every turn, proposer and repairer alike.
2. **`--tools ""` stays.** Without it Claude Code has its own Bash/Edit on the
   head, beside the immutable scorer.
3. **Counters stay harness-owned.** A counter the agent can edit is a counter
   it can fake.
4. **Iteration 1 is the unmutated baseline**, and it is never repaired.
5. **Infrastructure failures never reach a model**, and the proposer is told
   its mutation was not evaluated.
6. **Reverts are decided by the harness**, from states, never from a
   self-audit.
7. **A failure that stands leaves the parent's exact tree**, RTL included.
8. **`src/` stays flat.** A package layout breaks every remote task.
9. **Energy stays labelled `T1_MODEL`** until the power flow has a
   physical-design step.
