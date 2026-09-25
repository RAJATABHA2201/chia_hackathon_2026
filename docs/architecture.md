# SparseCraft architecture

How the loop is built: layout, gates, repair, prompts, models, caching and the
synthesis tier. Start with the [README](../README.md) for results and the quick
start, and [v1-to-v2.md](v1-to-v2.md) for what changed from the first loop.

## Layout

```
sparsecraft-v2/
├── README.md
├── src/          every importable module, FLAT (see "Why src/ is flat")
├── scripts/      operator entry points: run.sh, preflight, reports
├── configs/      cluster and cache/bypass YAML
├── prompts/      system prompts, work orders, and their shared fragments
├── kernels/      spmm.c, the measurement instrument (immutable)
├── workload/     prep_matrices.py and the generated SuiteSparse headers
├── docker/       the synthesis image CHIA does not ship, and its self-test
├── tests/        runnable without a cluster
├── docs/         migration guide
└── archive/      superseded prompts and code, kept for provenance, never loaded
```

`runs/` and `cache/` are created at the root on first use
(`$SPARSECRAFT_ROOT`, default this directory).

### `src/`: the loop

| module | role |
|---|---|
| `loop.py` | the driver: the iteration, the gate ladder (`evaluate_tree`), the repair loop, scoring |
| `agent.py` | backends (`PROVIDERS`, `make_llm`), prompt loading and `{{include:}}` resolution, the sealed MCP tools |
| `recovery.py` | N71-N73: failure classes, revert detection, the repair ledger, report parsing |
| `constants.py` | paths, the package layout, Ray resource names, `runtime_env()` |
| `design_state.py` | the typed design point, its hashes, the Scala emitter |
| `t0_legality.py` | N20 legality rules and the N13 scope allowlist (immutable) |
| `t1_model.py` | N22 analytical model and the energy model (immutable) |
| `zbu_model.py` | exact all-zero granule counts from the workload, for T-B's energy term |
| `metrics.py` | parses the kernel's `SPARSECRAFT k = v` counters (immutable) |
| `pareto.py` | N60 admission, the MAP-Elites archive, hypervolume (immutable) |
| `nodes.py` | `@ChiaFunction` nodes: elaborate, compile check, kernel build, simulate, state readback |
| `diff_nodes.py` | tree diff, reset and re-apply: the only state that crosses iterations |
| `rtl_scaffold.py` | the harness-owned RTL patches (T-A hook, ZBU seed, counters) |
| `synth_recipe.py`, `synth_node.py` | N52 T3 synthesis (yosys + OpenSTA on NanGate45) |
| `proposers.py` | the non-agentic control arms (`--proposer random|greedy`) |
| `candidates.py` | parses the proposer's `==CANDIDATES==` / `==MUTATION==` / `==PREDICTION==`, scores predictions |

The five **immutable** inputs (`src/t0_legality.py`, `src/pareto.py`,
`src/t1_model.py`, `src/metrics.py`, `kernels/spmm.c`) are hashed at run start
and re-checked every iteration; any drift aborts the run. A missing file is
fatal rather than hashed, so a moved file cannot turn the check into a no-op.

### `scripts/`: what you run

| script | what it answers |
|---|---|
| `run.sh` | the whole thing: preflight, cluster, loop, teardown |
| `check_llm.py` | is the model reachable? (`--list` shows every backend) |
| `check_setup.py` | is every tier installed and wired? (`--quick`: no container starts) |
| `smoke_agent.py` | does one agentic turn really run a command in the build container? (~10 s) |
| `run_synth.py` | measured area/Fmax of the baseline vs a candidate, no loop |
| `report_iter.py` | full parameter dump of one iteration, or all of a run |
| `summarize_runs.py` | regenerate `results/summary/` from the published run records |
| `synth_front.py`, `analyze.py` | post-hoc synthesis of a front; cross-run analysis CSVs |

### Why `src/` is flat

Ray pickles every `@ChiaFunction` **by reference, under its top-level module
name**: a task defined in `nodes.py` is shipped as "call `nodes.elaborate`".
`constants.runtime_env()` therefore uploads each `src/*.py` file individually
as a top-level module. Making `src/` a Python package (`src.nodes`) would make
every task fail on the worker with `No module named 'nodes'`. So `src/` is a
directory of top-level modules; scripts put it on `sys.path`, and `loop.py`
does the same for itself.

Paths to everything else (`CONFIG_DIR`, `PROMPTS_DIR`, `KERNELS_DIR`,
`WORKLOAD_DIR`, `SCRIPTS_DIR`) are defined once in `constants.py`, relative to
`src/`. They are driver-side only: kernel source and workload headers reach
the build container by value.

## The graph

```
N74 integrity assert
 -> N10 propose        agentic: edits Chisel + markers through a BashTool in the build container
 -> N13 scope          allowlist over git status, before the diff is collected
 -> N20 T0 legality    microseconds; names the violated rule
 -> N21 identity       (state_hash, rtl_digest); dedup
 -> N12 compile gate   sbt compile, 20 s to 3 min
 -> N22 T1 analytical  a filter, never a measurement
 -> N30/N31 elaborate  [key: hw_hash + rtl digest + Verilator threads]
 -> N12b netlist check did the RTL edit reach the hardware?
 -> N32 kernel build   [key: sw_hash + kernel/header/schedule bytes]
 -> N50 simulate       real Gemmini counters
 -> tripwire, N41 equivalence against the golden Y = A*X
 -> N52 T3 synthesis   measured area; DISPATCHED right after N12b, so it runs in the
                       hammer container while the verilator container simulates
 -> N60 Pareto admit -> N62 archive -> N61 diagnose -> back to N10

any gate N13..N41 fails on the agent arm:
 -> N71 classify       infra is detected first and never shown to a model
 -> N73 repair turn    system/repairer.md + task/repair.md
 -> re-run the WHOLE ladder from N13
 -> repeat until pass, NOT_ACTIONABLE, a detected revert, or the budget
```

A failure that stands is recorded once and the tree is rolled back **exactly**
to the parent (reset, then re-apply the parent's own diff), RTL included.

## The repair loop (N71-N73)

When a gate fails on an agent-proposed design, a second LLM instance, built
from `prompts/system/repairer.md`, is handed a work order
(`prompts/task/repair.md`) containing the verdict and its failure class, the
evidence (the compiler's lines, the violated T0 rule with its arithmetic, or
the first mismatching output), the proposer's stated mechanism verbatim, the
parent and failing design states, and **every earlier repair attempt in this
iteration with what the harness measured afterwards**. Sessions are not
resumed, so that ledger is the repairer's memory.

After each repair turn the harness re-runs every gate from the scope check
down. The loop stops when:

| stop | what is recorded |
|---|---|
| every gate passes | the repaired design is scored normally; `record["repair"]` keeps the history |
| the repairer reports `NOT_ACTIONABLE` | the original failure; no rebuild is spent on a guess |
| a repair round is a revert, a no-edit, or a duplicate | the last genuinely measured failure |
| the budget runs out | the last failure |

**Reverts are decided by the harness, not the repairer's self-audit.** For
every field the proposer moved `p -> q`, a repaired value equal to `p`, or past
it, is a revert; a value strictly between `p` and `q` (the nearest legal value
in the proposer's direction) is allowed. An RTL digest restored to the
parent's is also a revert.

**Budget.** `--repair-budget N` (default 3) attempts per iteration in total,
and a per-class cap sized by the price of the re-check
(`recovery.CLASSES[...].retries`): compile 3, T0 2, elaboration 2, kernel 2,
divergence 1, hang 1, tripwire 1, RTL no-op 1, scope 1. `--no-repair` turns it
off. Repair runs only on the agent arm, never on the baseline or the control
arms.

**Infrastructure is never a design failure.** An OOM, a preempted worker or a
lost connection is classified before anything else; no model sees it, and the
proposer is told explicitly that its mutation was not evaluated.

Artifacts per iteration: `llm_NNN.md` (proposer), `repair_NNN_K.md` (work
order and transcript of attempt K), `diff_NNN.json` (the proposer's tree),
`diff_NNN_repairK.json` (the tree after attempt K), `iter_NNN.json` (the
record, with a `repair` block when one ran).

## Prompts

```
prompts/
├── system/
│   ├── microarchitect.md     N10 proposer (includes strategy/t-a, strategy/t-b, shared/*)
│   ├── repairer.md           N73 repairer (includes debug/*, shared/scope, execution, platform)
│   └── diagnostician.md      N61 escalation: a stub, not constructed by the loop
├── task/
│   ├── propose.md            per-iteration work order for N10
│   └── repair.md             per-attempt work order for N73
├── debug/                    the repairer's reference material
│   ├── methodology.md        adapted from CHIA's common_debugging.md
│   ├── chisel-gemmini.md     adapted from CHIA's chisel_debugging.md, BOOM parts replaced by Gemmini's
│   ├── failure-playbook.md   one section per verdict: meaning, re-check cost, causes, what is not a fix
│   └── repair-contract.md    the ==REPAIR== output contract the harness parses
├── shared/                   facts every agent must agree on (writable set, host quirks, schemas)
└── strategy/                 technique modules (T-A, T-B, N:M, dataflow/tiling, resource sizing)
```

`{{include: path}}` lines (alone on their line) are expanded recursively by
`agent.resolve_includes`; a cycle or a missing file raises. Work orders use
`${NAME}` placeholders and must match their call site exactly, both ways, or
`agent.load_prompt` raises: a typo fails at iteration 1, not as a baffling
proposal hours in. The system prompts are static for the whole run, so they
stay in the prompt cache; everything that changes per attempt is in the work
order.

The Claude backend runs **sealed** (`--tools ""`), so the agent cannot read
files on the head. That is why the debugging references are inlined by include
rather than referenced by path, which is how CHIA's own `timing_opt` example
hands them over.

## The model

| backend | credential | note |
|---|---|---|
| `claude` *(default)* | the `claude` CLI's own login | Claude Code as the agent, `claude-opus-5`. Uses the OAuth login in `~/.claude/.credentials.json`, i.e. subscription usage. `ANTHROPIC_API_KEY`, if exported, silently takes precedence. |
| `gemini` | `GEMINI_API_KEY` | AI Studio key, OpenAI-compatible endpoint |
| `vertex` | `GOOGLE_CLOUD_PROJECT` + ADC | bills to GCP; `gcloud auth application-default login` |
| `claude_api` | `ANTHROPIC_API_KEY` | same model through the Anthropic SDK; fallback if the CLI is broken |
| `anthropic`, `openai`, `openrouter`, `groq`, `custom`, `opencode` | see `src/agent.py` | |

```bash
python scripts/check_llm.py --list     # every backend and whether it is ready
```

The agentic turn runs natively on the head (the `llm` resource is on
`head_local` in `configs/cluster.yaml`), so no credential is mounted into any
container. Claude Code is sealed to the same tool surface as every other arm:
`--tools ""` (no built-in Read/Write/Bash, which would otherwise run on the
head beside the immutable scorer), `--strict-mcp-config`,
`--setting-sources ""`, `--disable-slash-commands`.

| env var | default | |
|---|---|---|
| `SPARSECRAFT_CLAUDE_EFFORT` | `xhigh` | proposer effort: `low`/`medium`/`high`/`xhigh`/`max` |
| `SPARSECRAFT_REPAIR_EFFORT` | `high` | repairer effort, applied when the repair LLM is constructed |
| `SPARSECRAFT_LLM_MODEL` | backend default | e.g. `claude-opus-5` |
| `SPARSECRAFT_CLAUDE_FALLBACK_MODEL` | *(off)* | off on purpose: it would silently run part of an arm on another model |
| `SPARSECRAFT_CLAUDE_BUILTIN_TOOLS` | *(off)* | un-seals the scorer; debugging only, never for a scored run |
| `SPARSECRAFT_RATE_LIMIT_MAX_WAIT_S` | `21600` | a usage limit is waited out around the call instead of failing the iteration |
| `SPARSECRAFT_MAKE_JOBS` | adaptive, at most 12 | elaboration's `make -j`; capped so it can overlap a running synthesis |
| `SPARSECRAFT_VERILATOR_THREADS` | `16` | simulator threads (a build flag, so it is in the elaboration cache key). Measured: 30 threads simulated the baseline 29% SLOWER than 16 |
| `SPARSECRAFT_SYNTH_PARALLEL` | `1` | run N52 synthesis alongside the simulation; forced sequential when `SPARSECRAFT_ENERGY_SOURCE=measured` |

## Caching

`--cache-scope run` (default): a hit may only be served by work done **earlier
in this run**; the cache directory starts empty inside the run directory, so
nothing from an older run can leak in. `global` reuses the shared cache,
`off` recomputes everything (`configs/no_cache.yaml`). Keys are
content-addressed: `hwsrc:`/`hw:` (hardware hash + RTL digest + Verilator
threads), `sw:` (software hash + the bytes that enter the compiler), `sim:`.
A cached **failure** is never served.

## Tests

No cluster, no container, no model:

```bash
python tests/test_t0.py                 # every T0 rule fires by name
python tests/test_recovery.py           # classification, revert detection, report parsing
python tests/test_candidates.py         # candidate parsing, dedup, prediction scoring
python tests/test_t1_energy.py          # SRAM floor, and T-B credited at row level only
python tests/test_loop_repair_mock.py   # the real loop.main() on a fake cluster, 9 scripted iterations
```

The mock test drives the real `loop.main()`, `recovery.py`, prompt files, T0,
T1 and Pareto code through: a compile failure repaired and measured; a T0
repair that reverts the mutation; a divergence the repairer declares
`NOT_ACTIONABLE`; a scope violation repaired; a budget exhausted; an OOM
classified as infra; a no-edit repair; a hang; exact rollback; and work orders
carrying the mechanism and the attempt ledger.

## The T3 synthesis tier

`hammer-vlsi 1.2.0` is already in `chia-chisel-build`. What was missing was the
binaries it drives and a PDK: `docker/SparseCraftSynthDockerfile` adds both,
built by `docker/build.sh` from this checkout, and proves at build time that
the chain works by synthesising a Gemmini-shaped MAC array.

* **Area**: yosys `stat -liberty`, the summed Liberty cell area of the mapped
  netlist, plus the SRAM macro area. Post-synthesis cell area: a lower bound on
  die area, not an estimate of it.
* **Fmax and power**: from OpenSTA, but **not usable from this flow**. yosys
  maps without buffering or sizing, so a single high-fanout net carries
  microseconds of delay (Fmax about 0.2 MHz) and the power report inherits the
  same defect. Fixing both needs an OpenROAD placement and repair step. So the
  loop scores on the target clock period, energy stays `T1_MODEL`, and OpenSTA
  power is recorded but not scored (`SPARSECRAFT_ENERGY_SOURCE`).

`--synth` (the default) also flips chipyard's `ENABLE_YOSYS_FLOW`, so the RTL
that is simulated is the RTL that is synthesised; the two build flavours carry
different cache tags (`hw:` vs `hwsrc:`).

Two defects in released hammer 1.2.0 are patched in the image, idempotently,
by `docker/patch_hammer.py`: the NanGate45 technology description does not
load (missing `grid_unit`, off-grid widths), and the yosys flow's
`dfflibmap -map-only` silently drops every enable flip-flop while exiting 0.
`docker/selftest.sh` asserts against the second one.

## What the proposer is told each iteration

Beyond the design state and the counters, the work order (`task/propose.md`)
carries three things the harness computes:

- **Levers for the measured bottleneck** (`${STRATEGY}`). `diagnose()` labels
  the last *measured* design `memory`, `compute`, `scratchpad`, `issue_queue` or
  `balanced` (neither starved nor saturated: the band V1 lived in), and
  `BOTTLENECK_STRATEGY` selects strategy modules for it. T-A and T-B are always
  in the system prompt; the work order adds only the others the bottleneck calls
  for (`dataflow-tiling`, `resource-sizing`, `nm-structured`), never a
  duplicate. The system prompt stays byte-stable, so it stays cached.
- **Its own prediction, scored.** `==PREDICTION==` is parsed and compared with
  the parent's measured time, energy, area and period by
  `candidates.score_prediction`, which derives the direction from the numbers.
  The result, and a running tally, go into the next diagnosis and
  `record["prediction_score"]`.
- **Its untried alternatives** after a change that was not admitted: moves it
  listed in `==CANDIDATES==` and has not implemented, most recent first, at
  most three.

## The energy model

Energy is modelled (`T1_MODEL`) from measured counters: MACs issued (less
T-A-gated ones), on-chip SRAM traffic (less T-B's skipped reads, derived from
the workload's all-zero ROWS, the only reads the ZBU interface can skip), and
measured off-chip bytes, at 0.30 / 1.2 / 22 pJ per MAC / SRAM byte / DRAM byte.
Per-access SRAM cost scales as `sqrt(max(SRAM, 32 KB) / 256 KB)` over
scratchpad + accumulator. The floor was the 256 KB reference itself until
2026-09-25, so every smaller design scored identical energy; re-scoring V1's
recorded designs, its final 80 KB design moves from 27.73 to 18.86 uJ (the
320 KB baseline is unchanged at 95.67 uJ).

## Known gaps

Stated so nobody has to rediscover them:

- **The diagnostician LLM is not constructed.** `system/diagnostician.md` is a
  stub; the rule table now labels every band, including `balanced`.
- **Timing and power from OpenSTA are unusable** without a placement step (see
  the T3 section), so Fmax is not a live objective.
- **No cache provider is registered for `synthesize_recipe`** (the loop registers
  `"synthesize"`), so synthesis always runs; its tag now carries the RTL digest,
  so registering one would be safe.
- **Energy numbers are not comparable with V1's records** for designs under
  256 KB of SRAM, because of the floor change above.
