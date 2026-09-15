# SparseCraft — CHIA Loop v2 (walking skeleton)

Agentic HW/SW co-design of sparse-attention extensions to Gemmini, built on
[CHIA](https://chialoops.ai). Implements the Loop v2 topology from
`../SparseCraft_Technical_Review.md` §3.2, reduced to the tiers that run today.

**Scope decisions:** prefill-only; LLM behind a placeholder seam until Vertex
credentials are wired. **T3 synthesis is no longer deferred** -- see below.

## Quick start

Two commands. One of them is getting a key.

```bash
export GEMINI_API_KEY=...      # https://aistudio.google.com/apikey
./run.sh --iters 5
```

`run.sh` checks the model is reachable, checks the toolchain is reachable,
brings the cluster up, runs the loop, and tears the cluster down on the way out
-- including when the loop crashes or you interrupt it. Everything before the
loop fails in seconds; the first elaboration is 20-40 minutes, so nothing that
can be known cheaply is left until then.

```bash
./run.sh --iters 20 --synth      # overnight, scored on MEASURED area and Fmax
./run.sh --backend anthropic     # any provider; see below
./run.sh --skip-llm              # harness only, no model at all
```

### The model

Any OpenAI-compatible provider works, because CHIA's `OpenAICompatLLM` drives
the agentic tool loop itself and the provider is just a `base_url` plus a key.

| backend | credential | note |
|---|---|---|
| `gemini` *(default)* | `GEMINI_API_KEY` | Google AI Studio key, OpenAI-compatible endpoint. Lowest setup. |
| `vertex` | `GOOGLE_CLOUD_PROJECT` + ADC | Bills to GCP credits. Needs `gcloud auth application-default login`. |
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `openai` | `OPENAI_API_KEY` | |
| `openrouter` | `OPENROUTER_API_KEY` | |
| `groq` | `GROQ_API_KEY` | |
| `custom` | `SPARSECRAFT_LLM_BASE_URL` | vLLM, Ollama, a gateway. Key optional. |
| `opencode` | none | CLI in a container; needs the `llm_cli` node uncommented. |

```bash
python check_llm.py --list     # every backend and whether it is ready
python check_llm.py            # actually call the configured one
```

**The agentic turn runs natively on the head, not in a container.** An API-key
backend is an HTTPS client; it needs `openai`/`google-genai`, which are in
`chia_env` and not in the EDA images. So `cluster.yaml` advertises the `llm`
resource on `head_local`. The consequence is the nice one: there are no
credentials to mount into any container and no image to rebuild when a key
changes. The model's only *write* path is unchanged -- still a `BashTool`
pinned to the chipyard bundle.

The key is read on the driver by `agent.make_llm` and travels inside the
constructed object. It is never put in a `runtime_env`, which Ray logs and
echoes back in job metadata.

### Checking it works, cheapest first

```bash
python check_llm.py            # seconds   -- is the model reachable?
python check_setup.py          # ~1 min    -- is every tier reachable?
chia up -y cluster.yaml
python smoke_agent.py          # ~10 s     -- one agentic turn, end to end
python run_synth.py --compare  # ~1 h      -- measured baseline vs candidate
./run.sh --no-up --iters 5     # hours     -- the real thing
```

`smoke_agent.py` is the one worth knowing about: it runs a single model turn
with the real tools attached, then verifies **from inside the build container**
that a shell actually executed what the model asked for. It proves the whole
agentic path in about ten seconds, before anything expensive starts.

## The graph

```
N74 integrity assert  ──▶ N10 propose (agentic)
                          └▶ N11 select      programmatic: T0 → dedup → T1 → ε-greedy
                             └▶ N13 scope    allowlist check BEFORE any write
                                └▶ N20 T0    legality, µs, names the constraint
                                   └▶ N21    hash/cache  (_chia_tag + Bypass)
                                      └▶ N22 T1 analytical
                                         └▶ N30/N31 elaborate  [key: hw_hash]
                                            └▶ N32 kernel      [key: sw_hash]
                                               └▶ N50 T2a simulate
                                                  └▶ N52 T3 synth    [key: hw_hash]
                                                     └▶ N60 Pareto admit  (NOT improves?)
                                                     └▶ N62 archive
                                                        └▶ N61 diagnose ─▶ back to N10
```

## Files

| File | Role |
|---|---|
| `design_state.py` | Typed delta over Gemmini's `leanConfig`; canonical hashes; Scala emitter |
| `t0_legality.py` | N20 + N13. Gemmini's real `require()`s **and** the review's §2.3 rules |
| `t1_model.py` | N22 analytical model. Footprint and metadata are **exact**, not estimated |
| `metrics.py` | Parses `SPARSECRAFT k=v` counters out of `RunResult.log` |
| `pareto.py` | N60 admission, N62 MAP-Elites archive, hypervolume, immutable weights |
| `llm_stub.py` | N10 placeholder — deterministic, emits the real K≥3 JSON contract |
| `nodes.py` | CHIA `@ChiaFunction` wrappers around Chipyard/Verilator nodes |
| `synth_node.py` | N52 T3. hammer + yosys for area, OpenSTA for Fmax |
| `docker/` | The synthesis image CHIA does not ship, plus its build-time self-test |
| `check_setup.py` | What is installed, probed from the live machine. Pre-flight check |
| `run_synth.py` | One-shot T3: baseline vs candidate area/Fmax, no loop involved |
| `loop.py` | The driver |
| `kernels/attn_prefill.c` | The measurement instrument (counters → stdout → metrics) |
| `cluster.yaml` | Single-machine cluster: native head + 3 podman EDA workers |
| `bypass_cache.yaml` | Cache/bypass keyed on `hw:` / `sw:` / `sim:` tags |

## Three things worth knowing

**1. The cache-key split is load-bearing, not an optimisation.** This host has
30 GB of RAM against 64 cores, and a Chisel elaboration peaks at 8–12 GB. That
caps concurrent elaborations at ~2, so the only way to get a useful evaluation
count is to not re-elaborate. `elaborate` is keyed on `hw_hash` alone; a
tiling-only mutation reuses the RTL entirely.

**N32 is not independent of N30**, though — the review's §3.1 treats the three
builds as separately keyed, but elaboration *emits* `gemmini_params.h` and the
kernels include it. So `sw_hash` carries `hw_hash` inside it.

**2. Gemmini already has a hardware softmax path.** The review's §1.2(2) calls
the missing `exp` unit a critical gap. Current Gemmini ships `Normalizer.scala`
with `NormCmd.{MAX, SUM_EXP, INV_SUM_EXP}`, an I-BERT integer `iexp`, and a
reciprocal — gated behind `has_normalizations`, which defaults to **false**.
T0 requires it true: without it `S` would have to be materialised.

**3. Counters are capped at 8 slots.** `counter_read` masks the index with
`0x7`, and a slot only counts from the moment it is configured. So the kernel
arms all eight *before* the workload, and the eight chosen are the ones the
review's lever table needs.

## The T3 synthesis tier

`hammer-vlsi 1.2.0` was **already** in `chia-chisel-build`, with plugins for
yosys, OpenROAD, NanGate45, Sky130 and ASAP7. What was missing was narrower:
the binaries hammer *drives*, and a PDK. `docker/SparseCraftSynthDockerfile`
adds both as a thin layer, and proves at build time that the chain works by
synthesizing a Gemmini-shaped MAC array.

Two numbers come out, from two different tools:

* **Area** -- yosys `stat -liberty`, the summed Liberty cell area of the mapped
  netlist. Post-synthesis *cell* area: it excludes routing and any blackboxed
  macro, so it is a lower bound on die area, not an estimate of it.
* **Fmax** -- NOT from yosys. `abc -D <period>` only *targets* a period and
  reports no slack, so the mapped netlist is linked into OpenSTA (inside the
  `openroad` binary) against the same Liberty corner, and Fmax is
  `1 / (target - worst_slack)`.

Synthesis is keyed on `hw_hash`, so every software-only mutation reuses one
run. `--synth` also flips chipyard's `ENABLE_YOSYS_FLOW` (yosys cannot read the
packed-array Verilog firtool emits by default), which means **the RTL that is
simulated is the RTL that is synthesized**. Those two build flavours carry
different cache tags (`hw:` vs `hwsrc:`) so they can never answer each other's
lookups.

KLayout is absent -- its conda package pins `qt<5.10`, unsolvable on current
channels. It is a GDS viewer and DRC engine; neither area nor Fmax touches GDS.

### Two defects in released hammer 1.2.0, patched in the image

Both are handled by `docker/patch_hammer.py`, idempotently, so an upstream fix
does not break the build.

1. **The NanGate45 description does not load.** Hammer's schema requires
   `grid_unit` on the stackup and every metal, and every width to be a multiple
   of it. `nangate45.tech.json` ships with neither -- naming the technology
   aborts with `decimal.InvalidOperation` before any tool runs. The patch copies
   down the grid unit the file already declares at top level and snaps ten
   off-grid widths.

2. **The yosys flow silently drops every flip-flop.** The plugin emits
   `dfflibmap -map-only`, which maps only registers that already exactly match a
   library cell. NanGate45 has no enable flop, so every `$_SDFFE_*` survived as
   a yosys internal cell -- and the run still exited 0, reporting
   `sequential elements: 0.000000`. Dropping the flag took the self-test from
   0 to 768 sequential cells and 17,581 to 22,863 um2. **This one produces a
   wrong number rather than an error**, which is why `docker/selftest.sh` now
   asserts that no `$_`-prefixed FF cells remain, that sequential area is
   non-zero, and that OpenSTA returns a worst slack.

### Measured, on this host

The image's build-time self-test synthesizes a 4x4 output-stationary int8 mesh
on NanGate45 at a 2 ns target:

| | |
|---|---|
| area | 22,863 um2 |
| cells | 17,856 (768 sequential) |
| worst setup slack | +0.1896 ns |
| Fmax | 552 MHz |
| critical path | PE(0,0) DFF -> PE(1,0) DFF, the systolic accumulate path |
| yosys wall clock | under 2 s at this size |

## What is deliberately stubbed

Graph edges exist; bodies are not implemented this pass:
N23 δ-calibration · N41 three-way numerical verdict · N51 T2b long-sequence ·
N70 canary self-test · N71/N72/N73 error classify / shrink /
repair · N75 stagnation monitor · N80 holdout · N81 T4.

**N41 is the one that matters.** Without it the front admits designs on
measured cycle count with nothing checking that the design computes the right
answer. Every other stub degrades the search; that one can make a result
meaningless.

**Watch for duplicate front points.** T1 does not model the L7 queue knobs
(`ex_queue_length`, reservation-station depth), so two different design states
can carry identical predicted `(t, E, A)` and both sit on the front — neither
dominates the other. That is correct Pareto behaviour and an honest signal:
those designs are genuinely indistinguishable until T2 measures them.

**Known limitation:** the MAP-Elites archive stays at one occupied niche.
That is honest, not a bug — with one evaluation per iteration and
greedy selection, designs that would occupy other niches (larger `B`) are
proposed but never selected, because they predict worse time. Filling the
archive needs **N24 fan-out**, which is stubbed. The stepping-stone path
(`ADMIT_STEP`) does fire, via the coupled shrink-B-then-widen-banking plan.

## Swapping in a real model

`llm_stub.ScriptedProposerLLM` subclasses CHIA's `LLMCallBase`, whose entire
contract is `prompt(user_message, tools) -> QueryResult`. Every real backend
implements the same. In `loop.py`:

```python
from chia.models.vertex import VertexGeminiLLM
llm = VertexGeminiLLM(model="...", system_message=Path("prompts/propose.md").read_text())
```

Then add a matching credentials resource to `cluster.yaml` (`vertex_creds`) and
mount ADC into the worker via `docker.run_options`. Nothing else moves.
