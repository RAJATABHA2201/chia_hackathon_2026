# SparseCraft CHIA loop — status, 2026-09-24

Written for: whoever picks this repo up next, including a CHIA hackathon reviewer.

This records what the loop is, what it has produced, and — with equal weight —
what it cannot currently measure. Every number below came out of a tool; where
a number is modelled rather than measured, it says so on the same line.

---

## 1. What the loop is

A closed agentic co-design loop over a Gemmini accelerator inside Chipyard,
orchestrated by CHIA. One iteration = one agent proposal, evaluated through a
cheapest-first gate cascade, scored on a three-objective Pareto front.

```
N10 agent turn -> N13 scope check -> N20 T0 legality -> N12 compile gate
 -> N22 T1 model -> N30 elaborate -> N12b netlist digest -> N32 kernel build
 -> N50 simulate -> N41 equivalence -> T3 synthesis -> N60 Pareto verdict
```

Iteration 1 is always the unmutated baseline: it fixes the hypervolume
reference so every arm is scored from the same origin.

### The agent

`claude-opus-5` through Claude Code, via CHIA's `ClaudeCodeLLM`. Structurally
different from a chat-completion backend: one `prompt()` is a COMPLETE agentic
session — the agent plans, calls the MCP tools, reads results and retries
inside a single node invocation. From the loop's side the contract is
unchanged (same `ChiaFunction`, same dispatch line, same `QueryResult`).

Exactly **one agent call per iteration**, from iteration 2 onward. A
15-iteration run is therefore 14 agent calls. Verified against the reference
Gemini run `runs/agentic15b`: 15 `iter_*.json`, 14 `llm_*.md`, 28 profiler
`prompt` lines (14 dispatch + 14 complete).

The agent is sealed to exactly the three MCP tools the harness passes:

| flag | why |
|---|---|
| `--tools ""` | removes Claude Code's built-in Read/Write/Bash, which would otherwise run on the HEAD with `--dangerously-skip-permissions` — the same filesystem as `loop.py`, `metrics.py` and `t1_model.py`, which are in `IMMUTABLE_FILES` precisely because a metric the agent can edit is a metric it can fake |
| `--strict-mcp-config` | drops the developer's personal MCP servers |
| `--setting-sources ""` | drops user/project settings, so a stray `model` or `effort` cannot silently change the arm mid-study |
| `--disable-slash-commands` | drops skills |

Confirmed in the transcripts: the only tool that appears is
`mcp__sparsecraft_edit__*`. Zero built-ins. That is what makes the claude arm
comparable to the gemini arm.

---

## 2. The two techniques under search

### T-A — zero-gated MAC (hardware)

Per-PE zero test on the streaming A operand, with operand isolation so a
zero-valued multiply does not toggle the multiplier. Lives in `PE.scala`
behind a **Scala** `if (SparseCraftRTL.gateEnable)` — not a Chisel `Mux` on a
constant. That distinction is load-bearing and was found by assertion, not
reasoning: a Mux-based version constant-folded but left an always-enabled
`RegEnable`, so the netlist differed from stock Gemmini even with gating
disabled. A contaminated baseline makes every later "vs vanilla" number carry
an unknown offset.

`MAC_GATED_TOTAL` is a **free-running** counter: it tallies zero-operand
multiplies whether or not gating hardware exists, so it reports the same
*opportunity* (93.75% on `dnn512`) in both builds. Only `s.gate_enable`
converts that opportunity into an energy discount — without that guard the
ungated arm would receive the same credit and the A/B comparison would be
meaningless.

### T-B — the ZBU (hardware, deliberately not used)

Wired into `Scratchpad.scala` and functional, but **unmeasurable by the
current scoring**, and the agent independently re-derived this from the tree
rather than being told:

- `sc_skip` feeds exactly one place, `mem.read(raddr, ren && !sc_skip)`. `ren`,
  `q.io.enq.valid` and `io.read.req.ready` are stock, so a skipped granule
  still consumes its slot. **Zero cycles saved.**
- `CounterFile.scala` defines one SparseCraft external id, `MAC_GATED_TOTAL`.
  There is no ZBU event, so the SRAM-read saving is invisible to a
  counter-driven energy model.
- Its cost — 4 banks x 4096 rows of bitmap flops plus a read mux — is fully
  synthesised and fully charged to area.

Net: **+4.4% area for nothing measurable.** An agent that enables it and then
rejects it is reasoning correctly from the numbers it is shown. Making T-B
searchable requires a skipped-read counter in the RTL *and* an `e_sram` term
that consumes it, landed together.

### Software levers (DSE)

`k_chunk`, `b_blocks`, `x_resident`, `a_blocks`, `dense_mode` reach the build
as `-DSPMM_*` compiler defines against a single baremetal kernel. The
coupling that makes this co-design: elaboration emits `gemmini_params.h`
(carrying `DIM`, scratchpad capacity, accumulator size), so **the kernel
cannot be built before the hardware is elaborated**, and a hardware change
invalidates every previously built kernel.

---

## 3. Results

Run `runs/final15` (15 iterations, `claude-opus-5`, effort `xhigh`,
`--cache-scope run`, T1-modelled energy). First three iterations:

| iter | change | verdict | cycles | off-chip B | area mm2 | energy uJ |
|---|---|---|---|---|---|---|
| 1 | baseline | ADMIT_FRONT | 106,650 | 3,211,264 | 4.0350 | 95.67 |
| 2 | `gate_enable` (HW + RTL) | ADMIT_FRONT | 106,650 | 3,211,264 | 4.1047 | 93.55 |
| 3 | `x_resident` (SW only) | ADMIT_FRONT | **79,433** | **327,680** | 4.1078 | **30.11** |

**Cumulative vs the stock accelerator, after three iterations:**

| metric | baseline | now | | provenance |
|---|---|---|---|---|
| cycles | 106,650 | 79,433 | **1.34x faster** | measured, Verilator |
| off-chip bytes | 3,211,264 | 327,680 | **9.80x less** | measured, HW counters |
| area | 4.035 mm2 | 4.108 mm2 | +1.8% | measured, yosys/NanGate45 |
| energy | 95.67 uJ | 30.11 uJ | **3.18x better** | **MODELLED**, `t1_model` |
| perf/W | 10.96 | 34.82 GOPS/W | **3.18x better** | **MODELLED** |
| correctness | — | 0 mismatches | | measured, vs golden |

Nearly all of it came from **one software flag**. `gate_enable` bought 2.2%
energy because MAC is only 3% of the budget; `x_resident` bought the rest by
cutting DRAM traffic almost 10x, and DRAM was 74%. The energy model prices a
DRAM byte at 22 pJ against a MAC's 0.30 pJ — a 73x ratio — so the agent
attacking the memory term rather than repeating its own successful technique
is the correct reading of the numbers.

The bottleneck moved as a result: `mac/sram/dram` went **3/24/74% -> 1/75/24%**
and array occupancy **39.0% -> 89.5%**. SRAM is now the dominant term.

### Reproducibility

The decision sequence reproduced exactly across three independent runs
(`claude-opus5-agentic`, `claude-opus5-15iter`, `final15`): `gate_enable` at
iteration 2, `x_resident` at iteration 3, same measured outcomes. The agent
reaches the same conclusions by **different implementations** — `rtl_digest`
differs between runs for the same decision — so the convergence is in the
reasoning, not in memorised text.

### A negative result worth recording

In `runs/claude-opus5-15iter` iteration 4, the agent proposed
`meshRows/meshColumns 16 -> 32`. It compiled, elaborated and simulated, and
was **functionally wrong**: 32,674 mismatching outputs, 1.69x slower, 8.5x
more DRAM traffic. `DIM` changed while the kernel's tiling still assumed 16.
N41 caught it, the design was reverted, and the front was never contaminated.
This is the gate cascade working as designed.

---

## 4. What cannot currently be measured, and why

Stated plainly because a number that looks plausible and is wrong is worse
than no number.

### Power — OpenSTA is wired up, and its output is not usable

`synth_recipe.py` runs OpenSTA `report_power` on the mapped netlist with
activity annotated from measured counters. It works, in the sense that it
produces a real tool number that responds to design changes (v2 vs v3 gating:
2,140,427 vs 2,121,156 cells -> 9.887 vs 9.831 W). It is nonetheless **not
usable as a measurement**, for a reason that is a property of the flow rather
than of the tool:

| annotation | total W | W/mm2 |
|---|---|---|
| `-input` 0.3072 (measured operand rate, propagated) | 1936.70 | 805.20 |
| `-input` 0.1000 | 630.30 | 262.05 |
| `-input` 0.0200 | 126.38 | 52.54 |
| `default` | 126.38 | 52.54 |
| `-global` 0.3072 | 9.82 | 4.08 |
| *plausible for this design class* | | *0.1-0.5* |

Every mode is implausible. The cause is visible in the timing report: a single
`INV_X1` on the critical path carrying **4,763 ns** of delay, against a 2.0 ns
target. **Yosys maps but does not buffer or size gates against an SDC**, so
high-fanout nets have pathological capacitance. Switching power differs 1,100x
between `-global` and `-input` on the *same netlist* for that reason.

This is also why `worst_slack_ns = -5150` and **Fmax is unavailable**
(`area_source` carries `_TIMING_UNUSABLE`). Timing and power are the same
defect. Genus reports meaningful pre-P&R power because it applies a wire load
model *and* maps timing-driven; the open flow here does neither.

**The fix is a physical-design step**, not a parameter: OpenROAD
`initialize_floorplan` -> `global_placement` -> `estimate_parasitics
-placement` -> `repair_design`, then annotate. OpenROAD is installed and the
PDK carries the ORFS assets. Estimated 30-90 min per design on 2.1M cells —
tractable for two or three designs, not for fifteen.

Until then `SPARSECRAFT_ENERGY_SOURCE` defaults to `model`: the OpenSTA figure
is **recorded on every iteration** (`t3_power_w`, `t3_energy_pj`) and does not
move the front.

### Energy — modelled, and the model's scope is narrower than it looks

`t1_model` charges three terms only: `macs_issued`, SRAM bytes, DRAM bytes. It
has **no term** for control logic, reservation stations, DMA, TLB, the
transaction tracker, the clock tree, or leakage — roughly 90% of the 2.1M
cells. On-chip it reports 0.1211 W where OpenSTA reports 9.8241 W, and that
81x decomposes cleanly into ~15x (activity annotation) x ~5.3x (scope). The
5.3x is the model under-counting, not the tool over-counting.

### Consequence for what should be claimed

Cycles, off-chip bytes and area are tool-measured and unqualified. Energy and
perf/W are modelled and must be labelled as such. CHIA's own published
15-iteration case study reports frequency, area, IPC and correctness — and no
power at all — so this is in line with the framework's own precedent.

---

## 5. Harness bugs found and fixed

Each was invisible: the loop reported a plausible number and nothing failed.

**`bypass_cache.yaml` registered three node names that do not exist.** CHIA
fails this silently — `chia/base/bypass.py:325` is
`if not self._bypass.get(func_name, False): return`, so an unregistered node
is simply never bypassed, with no warning.

1. `synthesize` vs the actual `synthesize_recipe`, and tag `syn:.*` vs the
   emitted `synr:...` — so **synthesis was never cached in any run**.
2. `netlist_digest` was not registered at all. `loop.py` passes
   `_chia_tag=f"net:{hw_tag}"` and a comment explains the intent — "the digest
   travels with the artifact" — but **the fix never took effect**, because the
   node was never added to the YAML. `netlist_digest` globs `gen-collateral/`
   off disk, and an `elaborate` cache hit does not rewrite that directory, so
   N12b reported the *previous* design's netlist.

Measured: the same baseline design reported netlist `a64e2c09c3b4ebcc` under
cache and `6f3c5995546620a3` when genuinely elaborated. **N12b — the gate whose
entire job is "did the RTL reach the hardware?" — was reporting on hardware
that was never built**, and was comparing stale to stale, so it could not have
fired. Measurements were unaffected (`simulate` and `synthesize_recipe` consume
the artifact dict, keyed correctly), but as the code's own comment says: *a
stale CHECK is worse than a stale measurement.*

**Fmax was silently fabricated.** `create_clock ... [get_ports clock]` with no
fallback aborted the STA script before `report_checks` or `report_power` ever
ran; a loose regex then scraped `630.0` W out of unrelated text — identical
across four different netlists. Fixed with clock-port fallbacks, a parser
anchored on the report table with a component-sum check, and a plausibility
gate that rejects a slack worse than the target instead of turning it into a
period (that division was inflating energy 2500x).

**`BUILD_MAKE_JOBS` was hard-coded for a host that no longer exists.** 24 was
calibrated for 64 GB; the host is 30 GB. 24 concurrent `g++` at 1-1.5 GB wants
24-36 GB. Worse, `SPARSECRAFT_MAKE_JOBS` was not in `runtime_env()`'s forwarded
whitelist, so exporting it would have done nothing — `constants.py` is imported
*on the worker inside the container*. Both fixed; the default now derives from
`/proc/meminfo`.

**A subscription usage limit would have destroyed a run.** CHIA raises
`RateLimitError` and never retries it (correct). But the per-iteration
`except BaseException` filed it as `INFRA_FAILURE` and continued, and the next
iteration hits the same limit in milliseconds — so one limit at iteration 6 of
15 destroys iterations 7-15, each failing seconds apart. `loop.agent_turn()`
now waits out `exc.reset_time` and re-issues the same turn.

---

## 6. Cache scoping

`--cache-scope run` (the default) puts the cache inside the run directory, so
it starts empty: work done **earlier in the same run** can satisfy a hit, and
nothing from an older run can. `global` restores cross-run reuse; `off`
recomputes everything.

The keys are content-addressed (`hw_hash` + `rtl_digest` + `sw_hash`), so a
hit is impossible for a design that differs in any way — run-scoping is about
provenance, not correctness. It also bounds the blast radius of a
cache-registration bug like the ones above to a single run.

---

## 7. Reproducing a run

```bash
cd /home/chia-sparsecraft/sparsecraft
python check_llm.py                      # is the model reachable
python check_setup.py                    # is the toolchain reachable
python smoke_agent.py --backend claude    # one agentic turn, end to end
./run.sh --iters 15 --synth               # the study

python report_iter.py <run-name>          # full per-iteration parameter dump
```

Per run, under `runs/<name>/`: `iter_NNN.json` (verdict, design state,
counters, area), `llm_NNN.md` (agent reasoning), `diff_NNN.json` (the actual
Chisel/config diff), `synth_NNN.json`, `history.json`, `profile/` (token
counts and per-node timings), `cache/`.

## 8. Known limitations

- **Fmax and power are unavailable** without a physical-design step (§4).
- **No fan-out.** The loop is a serial hill-climber: one proposal per
  iteration, so 63 of 64 cores idle during the single-threaded synthesis.
  Candidate fan-out is the fix and is not implemented.
- **Synthesis is on the critical path.** Dispatching T3 asynchronously on T2a
  pass would overlap ~8 min per iteration with the next iteration's work.
- **T-B is unsearchable** until a skipped-read counter and a matching `e_sram`
  term land together (§2).
- **No held-out workload.** The agent sees every pattern it is evaluated on.
