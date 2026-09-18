# Anatomy of a run

What `./run.sh` actually does, what you get at the end, where it lands, and
which files the model reads. Written against the tree as of 2026-09-18.

---

## Quick answers

| Question | Answer |
|---|---|
| Is everything inside `sparsecraft/`? | **The code, yes. The output, no.** Results are written to the *parent* directory. |
| Where do results go? | `/home/chia-sparsecraft/runs/<run-name>/` |
| Where does the cache go? | `/home/chia-sparsecraft/cache/` (32 GB budget, survives between runs) |
| Which prompts does the model read? | Exactly two: `prompts/propose.md` and `prompts/task.md` |
| How long is one iteration? | 20–40 min without `--synth`, up to ~2 h more with it |
| What does one iteration cost in tokens? | roughly $0.60–0.70 on `gemini-2.5-pro` |

---

## 1. Where things live

Code is self-contained in `sparsecraft/`. **Output is not** — it goes to
`PROJECT_ROOT`, which is the *parent* directory (`constants.py:64-66`):

```
/home/chia-sparsecraft/            <- PROJECT_ROOT
├── sparsecraft/                   <- all the code (a git repo)
│   ├── loop.py                    the orchestrator
│   ├── run.sh                     the entry point
│   ├── agent.py                   LLM backends + the sealed tools
│   ├── nodes.py  synth_node.py    the build / sim / synthesis nodes
│   ├── t0_legality.py  t1_model.py  metrics.py  pareto.py
│   ├── cluster.yaml               which container each node type runs in
│   └── prompts/                   <- see section 2
├── runs/                          <- EVERY RESULT LANDS HERE
├── cache/                         <- 32 GB result cache, reused across runs
└── chia/ chia-work/               the CHIA framework (not ours)
```

If you copy `sparsecraft/` somewhere else, set `SPARSECRAFT_ROOT` or the run
will still write to `/home/chia-sparsecraft/`.

---

## 2. Which prompts the model actually reads

`prompts/` has 14 files. **The loop loads two of them.**

| File | Role | Loaded at |
|---|---|---|
| `prompts/propose.md` | **system message** — sent once per iteration, unchanged | `loop.py:211` via `agent.make_llm` |
| `prompts/task.md` | **user message** — rendered per iteration with 5 variables | `loop.py:278` via `agent.load_prompt` |

The other twelve are **inert**. No code path opens them:

- `prompts/as-is/`, `prompts/adapted/`, `prompts/harvest/` — vendored reference
  prompts from `chia/examples/`, kept for provenance and future use.
  `harvest/` in particular is source material we copied *ideas* out of; see
  `prompts/harvest/README.md`.
- `prompts/custom/` — header-only stubs for nodes that do not exist yet
  (N12 Emit Patch, N61 Diagnose escalation).
- `prompts/adapted/repair.md` and `chisel-debugging.md` are written and adapted
  but have **no consumer yet** — the repair node (N73) is not built, and there
  is no `{AUX_DIR}` injection mechanism in `agent.load_prompt`.

### The five variables substituted into `task.md`

Filled at `loop.py:278-285`. `agent.load_prompt` validates them in both
directions and raises if the template and the call disagree.

| Variable | Value |
|---|---|
| `${CHIPYARD}` | `/home/ray/chipyard` — the cwd for every bash command |
| `${PARAMS_PATH}` | the one writable file, `.../gemmini/SparseCraftParams.scala` |
| `${HARNESS_PATH}` | `.../config/SparseCraftConfigs.scala` |
| `${PARENT_STATE}` | the current design state, as JSON |
| `${DIAGNOSIS}` | the bottleneck named from last iteration's counters |

---

## 3. What one iteration does

Before the loop starts once: `ensure_baseline()` (`loop.py:245`, called at `:264`) writes the
baseline `SparseCraftParams.scala` and the harness config if the tree lacks them.

Then per iteration:

| # | Node | What happens | Fails as |
|---|---|---|---|
| 1 | **N74** | assert every immutable input still hashes the same | aborts the run |
| 2 | **N10** | the model reads the diagnosis and edits `SparseCraftParams.scala` through a bash tool in the build container | — |
| 3 | **N13** | allowlist check over `git status`; the writable set is exactly one file | `SCOPE_VIOLATION` |
| 4 | **N20** | T0 legality — divisibility, capacity, Little's Law, etc. Microseconds. | `T0_ILLEGAL` |
| 5 | **N21** | duplicate-design check by state hash | asks for a different edit |
| 6 | **N22** | T1 analytical model predicts what will bound the design | — |
| 7 | **N30/N31** | Chisel elaborate → Verilator build. **This is the 20–40 min.** | `ELABORATION_FAILED` |
| 8 | **N32** | cross-compile the attention kernel | `KERNEL_BUILD_FAILED` |
| 9 | **N50** | run it, read Gemmini's hardware counters | — |
| 10 | — | information-theoretic tripwire: off-chip bytes ≥ one full read of the inputs | `TRIPWIRE_FAILED` |
| 11 | **N52** | T3 synthesis — yosys + OpenSTA for area and Fmax. **Only with `--synth`.** | logged, not fatal |
| 12 | **N60** | score, then Pareto-admit against (time, energy, area) | `REJECT` |
| 13 | **N61** | name the next bottleneck from the counters, feed it to the next iteration | — |

Any of the five `*_FAILED` / `VIOLATION` verdicts ends that iteration early and
the loop continues to the next one. The design state is written back to the
tree each time, so a rejected iteration resets rather than compounding.

> **`--synth` is off by default.** Without it, step 11 never runs, so **area and
> Fmax are never measured** and the Pareto front is effectively 1–2 dimensional.
> For a result you intend to report, use `--synth`.

---

## 4. What you get at the end

### On the console

```
final Pareto front (N):
  <point>
  ...
hypervolume vs baseline: 0.1234
traces: /home/chia-sparsecraft/runs/<run-name>
```

### On disk, in `/home/chia-sparsecraft/runs/<run-name>/`

| File | Written | Contains |
|---|---|---|
| `iter_001.json` … | every iteration, `loop.py:462` | **the per-iteration record** — verdict, design-state hash, parent hash, all measured counters, T1 prediction, diagnosis, admit info, wall clock |
| `history.json` | every iteration, `loop.py:456` | running list of `{iteration, state, cycles, verdict, diagnosis}` + the current front. This is what `query_history` serves to the model. |
| `status.md` | every iteration, `loop.py:450` | human-readable current state. This is what `read_status` serves to the model. |
| `diff_001.json` … | after the scope check, `loop.py:307` | the patch the model actually produced |
| `llm_001.md` … | every iteration, `loop.py:288` | the model's raw reply for that turn |
| `llm/` | per call | full request/response transcripts from the backend |
| `profile/` | continuously | Ray profiling traces |
| `synth_001.json` … | only with `--synth`, `loop.py:402` | area, Fmax, WNS, critical-path endpoint |

The run name defaults to `run-YYYYmmdd-HHMMSS`; override with `--run-name`.

**`iter_NNN.json` is the artifact of record.** Everything else is either derived
from it or a convenience view.

---

## 5. Running it

### Dry run — verify without spending 20–40 minutes

```bash
cd /home/chia-sparsecraft/sparsecraft
python check_setup.py     # every tier, every tool, every image
python check_llm.py       # one real (cheap) call to the model
```

`check_setup.py` ends with a `T2 / T3 / agentic` READY summary. Both are
seconds. There is no `--dry-run` flag on `loop.py` itself — the cheapest real
exercise is `smoke_agent.py`, which runs **one** agent turn with tool calls and
no build (~8 s).

### The real thing

```bash
./run.sh --backend vertex --iters 5            # no area/Fmax
./run.sh --backend vertex --iters 20 --synth   # the reportable version
```

Useful flags: `--no-up` (cluster already running), `--no-down` (leave it up),
`--skip-llm` (exercise the harness with no model at all), `--run-name NAME`.

### Cost

One iteration is roughly $0.60–0.70 of `gemini-2.5-pro`, so a 20-iteration run
is ~$13. The binding constraint is **wall clock, not money**: at 20–40 min per
iteration and at most 2 concurrent elaborations (30 GB RAM), 20 iterations is
most of a day.
