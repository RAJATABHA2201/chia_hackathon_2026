# SparseCraft: what this project actually does

State as of 2026-09-19. Written to be read by someone who has not been in the
weeds: what the system is, what it measures, what we ran, and — separated
carefully — what we can and cannot claim from it.

---

## 1. The research question

Can an LLM agent search a **coupled hardware/software design space** better than
mechanical baselines, under a search budget small enough to be realistic?

"Coupled" is the load-bearing word. The design state spans two layers that cost
different amounts to evaluate and depend on each other asymmetrically:

| Layer | Levers | Cost to evaluate | Notes |
|---|---|---|---|
| **Hardware** (RTL) | 19: array geometry, scratchpad/accumulator capacity and banking, DMA, TLB, queue depths | **~20 min** — re-elaborate Chisel, rebuild kernel, simulate | changing one also invalidates the software build |
| **Software** (kernel) | 1 real: `block_size` | **~19 min** — rebuild kernel, simulate | elaboration is reused |

The asymmetry is encoded in the cache keys: `sw_hash()` deliberately contains
`hw_hash()`, because elaboration emits `gemmini_params.h`, which the kernel
`#include`s. A hardware change invalidates software; software never invalidates
hardware.

**Honest note on scope.** The software side is *one* lever. `tile_m/tile_n/tile_k`
are passed to the kernel compile and `#define`d, but the kernel body never reads
them — verified by measurement: changing one rebuilds, simulates for 17 minutes,
and returns cycles identical to the parent's. So "hardware/software co-design"
here means 19 hardware knobs against 1 software knob, and the paper must say so.

---

## 2. What the loop does, per iteration

The target is a Gemmini systolic-array accelerator inside a Chipyard SoC,
running a block-sparse prefill-attention kernel.

```
N74  integrity assert      every immutable input still hashes the same
N10  propose               the agent (or a control arm) edits ONE file
N13  scope check           allowlist over git status -- writable set is 1 file
N20  T0 legality           divisibility/capacity/Little's-Law rules, microseconds
N21  dedup                 has this exact design been evaluated before?
N22  T1 analytical model   predicted cycles/area  (see the calibration note)
N30/31/32  build           Chisel elaborate -> Verilator build -> kernel compile
N50  simulate              run it, read 8 Gemmini hardware counters
--   tripwire              off-chip bytes >= one full read of the inputs
N52  T3 synthesis          yosys + NanGate45 -> area, Fmax   (optional)
N60  Pareto admit          non-dominated on (time, energy, area)?
N61  diagnose              name the bottleneck, feed it to the next iteration
```

The agent's authority is deliberately narrow: it may edit exactly one file
(`SparseCraftParams.scala`) and call three read-only query tools. It never
builds, measures, or scores anything.

### Guardrails, and why they are mechanisms rather than promises

- **Patch-scope allowlist** — enforced over `git status` before the diff is
  collected, not by asking the model nicely.
- **Information-theoretic tripwire** — rejects any design whose off-chip byte
  count falls below one full read of the inputs. **It fired on a real run** (§4).
- **Content-addressed integrity manifest** — every rule file, weight file and
  metric script hashed into every iteration record.

---

## 3. The three arms

All three face an identical harness, identical baseline, identical T0 rules and
an identical budget of 12 iterations. Only the *chooser* differs.

| Arm | Chooser | LLM? |
|---|---|---|
| **agent** | Gemini 2.5 Pro on Vertex, reads the diagnosis, edits the file | yes |
| **greedy** | coordinate descent: one lever at a time, never combining layers | no |
| **random** | uniform over one lever's candidate values | no |

Iteration 1 of every arm measures the **unmutated baseline**, so the hypervolume
reference is identical across arms. Without that, each arm would be scored from
its own opening proposal and the curves would not be comparable.

---

## 4. What we actually got

### Headline

| Arm | Best **admitted** | vs baseline | Phantom admissions |
|---|---|---|---|
| **agent** | **88,772 cycles** | **1.014x** | 1 of 4 |
| random | 89,851 | 1.002x | 5 of 8 |
| greedy | 89,986 | 1.000x — found nothing | 5 of 6 |

The agent's winning move changed **four fields at once**:

```
meshRows 16->8   meshColumns 16->8   tileRows 1->2   tileColumns 1->2
```

Same total PE count, re-organised. It preserves T0's
`meshRows*tileRows == meshColumns*tileColumns`, which is why a **single-lever
search cannot make this move at all**: any one field alone breaks the constraint
and is rejected before anything is built. An early greedy run demonstrated
exactly this, spending 11 of 11 iterations failing on `meshColumns`.

### The guardrail fired

Random's iteration 9 reported **80,564 cycles — an apparent 12% win** — and was
`TRIPWIRE_FAILED`. Its off-chip byte count fell below one full read of the
inputs, so it cannot have computed the answer. The integrity mechanism caught a
reward hack during a real run, unprompted.

> Our first analysis script reported that 80,564 as the best result, because it
> took the minimum over every *measured* iteration rather than every *admitted*
> one. That is the single number that must never reach a paper, and it took a
> deliberate check to catch.

### Agent prediction accuracy: 1 in 6

Each turn the agent states a mechanism and a predicted direction per objective.
It was correct on time in **1 of 6** cases — and still found the only real
improvement. The value is in proposal diversity, not in forward prediction.

### The analytical model is badly miscalibrated

| | T1 predicted | measured | ratio |
|---|---|---|---|
| cycles | 16,777,216 | 89,986 | **186x** |
| area | 537,842 um2 | 2,372,199 um2 | **4.4x** |

T1 is used as a *rejection filter*. Uncalibrated, it discards good designs before
they are ever built. It should be recorded as a prediction until `delta_o` is
measured.

### Phantom admissions

A design whose measured cycles **tie** the baseline is formally non-dominated, so
Pareto admission accepts it if the *modelled* area differs. The controls' fronts
are mostly this: 5 of 6 for greedy, 5 of 8 for random. **Novelty by state hash is
not novelty by behaviour** — and the effect is amplified by an inert lever and a
miscalibrated area estimate.

---

## 5. What is measured and what is not

This distinction decides what the paper may claim.

| Quantity | Status |
|---|---|
| **cycles** | **measured** — Verilator, real Gemmini hardware counters |
| off-chip bytes | **measured** — RDMA/WDMA counters |
| **area** | measured post-synthesis (yosys + NanGate45) for designs run through T3; **modelled by T1 otherwise**, and T1 is 4.4x off |
| Fmax / slack | measured by OpenSTA where T3 ran |
| power | **estimated** — `report_power` with default toggle rates, no annotated switching activity |
| SRAM area | **not measured at all** — NanGate45 has no SRAM compiler, macros are blackboxed. SRAM is 60-80% of a real tile; the exact byte count is reported beside the logic area |

Every iteration record carries `area_source`, so a modelled number can never be
plotted as a measured one by accident.

---

## 6. Limitations, stated plainly

- **12 iterations per arm, one seed.** No statistical claim is available. The
  agent's margin is 1.4% and rests on a single successful move out of twelve.
- **One workload**, one kernel, prefill only.
- **Software space is one real lever.**
- **No held-out workload.** `holdout.py` is referenced in `constants.py` but was
  never written, so the overfitting objection is open.
- **No gate self-test.** Nothing would detect a gate that silently stopped
  failing things it should fail.
- **Power is estimated**, not measured.

---

## 7. The other result: how the harness lies

Nine defects were found by running the system rather than reading it. Six of the
nine produced **plausible-looking wrong data rather than a crash** — the
dangerous kind.

| Defect | What it silently produced |
|---|---|
| Nested-submodule scope check | Loop never completed a single iteration |
| `%llu` under newlib-nano | Metrics died *after* paying full elaborate + simulate |
| `state_from_tree(diff)` shape mismatch | **Every design scored as the baseline** |
| Column-aligned Scala vs naive `sed` | **Every agent edit silently doing nothing** |
| No tree rollback on reject | Agent livelock — 6 of 8 iterations lost, 5 unrecorded |
| `conflict_stall_fraction` = 4.88 | Diagnosis **constant for every design ever measured** |
| Hypervolume reference per-arm | Arms measured from different origins |
| Inert `tile_*` levers | Front polluted with behaviourally identical designs |
| Best-over-all-iterations | Reported a **tripwire-rejected cheat** as the result |

The sixth is the most instructive. The scratchpad and reservation-station
counters are free-running accumulations — each exceeds the total cycle count
(2.39x, 2.49x, 2.87x) — so a rule written as `conflict_stall_fraction > 0.15`
was true for every design. The diagnosis read "bank-conflict bound, lever L3
banking" every iteration, and the agent dutifully proposed banking changes in
five of its first six moves against a lever that left cycles bit-identical.

**The agent was not behaving badly. The harness was lying to it consistently.**

---

## 8. What is running now

Post-hoc T3 synthesis on the Pareto-front designs, in priority order:
agent-1 (including the baseline, which the front excludes because it was
dominated), then greedy-1, then random-1. Roughly 28 minutes per distinct
hardware build. This replaces `area_source: T1_MODEL` with measured area on the
designs that appear in the figures.

Not planned: more arms, more iterations. A second seed per arm would address the
one-seed objection and costs about six hours; measured area was judged the better
use of the time.
