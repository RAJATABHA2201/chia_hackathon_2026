# Loop v2 — a plan to improve the CHIA loop significantly

---

## 0. One correction to the premise, because it changes what to build

The working assumption was that the loop is held back by *"oversimplified
prompt engineering — a few Markdown files covering 1–2 basic sparsity
techniques."* Half of that is right, half is not, and the half that is wrong
would send the work in the wrong direction.

**The prompts are not thin.** `propose_rtl.md` is 16 KB and already covers the
things a good agent prompt must: elaboration cost vs the compile gate, the
`sed`-on-Chisel trap that cost real iterations, the write-verify protocol, a
typed output schema with a required prediction block, explicit forbidden
moves. It is closer to the CHIA examples' `system.md` than to a stub.

**What is actually wrong is that 80 KB of written prompts have no node to run
in.** The inventory:

| file | bytes | intended node | status |
|---|---|---|---|
| `adapted/chisel-debugging.md` | 25,368 | debug agent | **never loaded** |
| `as-is/chisel_debugging.md` | 27,841 | reference | **never loaded** |
| `adapted/repair.md` | 9,352 | repair agent (N73) | **never loaded** |
| `debug_rtl.md` | 7,864 | debug LLM | **never loaded** |
| `adapted/triage.md` | 4,390 | error classifier (N71) | **never loaded** |
| `custom/diagnose-unclassified.md` | 2,228 | diagnose escalation (N61) | **never loaded** |
| `custom/emit-patch.md` | 1,709 | patch emit (N12) | **never loaded** |
| `propose.md` / `task.md` | 16,321 | superseded by the `_rtl` pair | dead |

`loop.py` loads exactly two: `propose_rtl.md` and `task_rtl.md`. Everything
else was written for a loop architecture that was designed and never built.
`docs/prompt_reuse_inventory.md` audited this on 2026-09-16 and the gap has
not closed since.

**So Phase 2 is not "write better prompts". It is "build the nodes the prompts
were written for", and only then extend the technique catalogue.**

The other half of the premise *is* right: the loop searches **two** techniques
(T-A zero-gated MAC, T-B ZBU) against the six families in
`SparseCraft_Technical_Review.md`. And one of those two is unsearchable — §5.

---

## 1. What the loop is missing, ranked by what it costs you

Evidence from `runs/final15` and `runs/claude-opus5-15iter`.

### 1.1 No repair agent — a failed gate wastes the whole iteration

Today a `COMPILE_FAILED`, `EQUIV_FAILED` or `ELABORATION_FAILED` reverts to the
parent and the iteration is gone. In `runs/agentic15b` (gemini) **10 of 14
proposals died at the compile gate** — 71% of the budget, recovering nothing.
Opus 5 is better (4/4 through N12 so far) but iteration 4 of
`claude-opus5-15iter` still lost a full iteration to `EQUIV_FAILED`.

CHIA's own `riscv_extensions` splits Implement-LLM from Debug-LLM with
different prompts. `adapted/repair.md` is already written for exactly this and
is explicit about the failure mode that matters: *"You will not shrink the
design until it trivially passes and claim victory."*

**Cost of not having it:** every gate failure is a lost iteration instead of a
bounded repair attempt.

### 1.2 One undifferentiated failure edge

Every failure returns to one agent with one prompt and the whole log. A Chisel
width mismatch, a timing miss and a **spot-VM preemption** need completely
different responses — and the last one is not a design failure at all. Feeding
it back as "your design failed to build" teaches the agent to abandon a good
mutation family.

`adapted/triage.md` is written for this node and is unused.

### 1.3 Diagnosis and proposal share one model and one turn

`N61 Diagnose` and `N10 Propose` are the same agent. The diagnosis becomes
post-hoc justification for the proposal it was going to make anyway. The
technical review's fix is a **rule table first, LLM only on `unclassified`** —
and `custom/diagnose-unclassified.md` exists for precisely that escalation.

It is also the largest recurring token cost in the loop.

### 1.4 No fan-out — 63 of 64 cores idle

Measured node time for one iteration of `final15`:

| node | share | parallelism |
|---|---|---|
| `simulate` | 53.0% | 16 threads |
| `synthesize_recipe` | 19.6% | **1 thread** (yosys) |
| `prompt` | 17.2% | network-bound |
| `elaborate` | 8.9% | `make -j8` (RAM-capped) |

The loop is a serial hill-climber: iteration N's proposal depends on iteration
N−1's measurement, so there is nothing to run concurrently. The only way to use
the box is to evaluate **K candidates per iteration**.

### 1.5 Synthesis sits on the critical path

`synthesize_recipe` is 19.6% of every iteration and nothing downstream needs it
before the next iteration starts. Dispatching it as a future on T2a pass would
overlap it with the next iteration's elaborate+simulate. **~8 min × 14 ≈ 2
hours per run**, for a change that is local to one call site.

---

## 2. Proposed prompt architecture

Follows the CHIA examples' convention — `prompts/<role>.md`, one file per node,
loaded by name — with a `shared/` layer the examples do not have but which
this loop needs because four nodes must agree on the same facts.

```
prompts/
  system/
    microarchitect.md        # N10 proposer role  (from propose_rtl.md)
    repairer.md              # N73 repair role    (from adapted/repair.md)
    diagnostician.md         # N61 escalation     (from custom/diagnose-unclassified.md)

  task/
    propose.md               # N10 per-iteration work order (from task_rtl.md)
    repair.md                # N73 work order, templated on failure class
    diagnose.md              # N61 work order, only on `unclassified`

  shared/                    # composed INTO the above; never loaded alone
    platform.md              # the host: podman, RAM ceiling, paths, timeouts
    execution-rules.md       # §3 -- when to elaborate, when to dry-run
    scope-and-guardrails.md  # writable set, forbidden moves, why
    feedback-schema.md       # how to read status/history, the counters
    output-contract.md       # ==MUTATION== / ==PREDICTION== schema

  strategy/                  # one technique per file, loaded SELECTIVELY
    t-a-zero-gated-mac.md    # current, from propose_rtl.md
    t-b-zero-granule-skip.md # current, from propose_rtl.md
    nm-structured.md         # new -- review §1.2(1)
    block-sparse.md          # new -- review §1.2(2)
    kv-compression.md        # new -- review §1.2(5)
    dataflow-tiling.md       # new -- review L1
    memory-layout.md         # new -- review L8

  reference/                 # never loaded at runtime; provenance only
    harvest/                 # already correctly documented as source material
    as-is/
```

**Two design rules that matter more than the tree shape:**

1. **`shared/` fragments are composed, not duplicated.** Today the writable
   set and the host warnings are restated in each prompt and will drift. A
   `load_prompt()` that resolves `{{include: shared/scope-and-guardrails.md}}`
   makes the scope rule have exactly one definition — which matters because
   the same rule is also enforced programmatically in `t0.check_patch_scope`.

2. **`strategy/` files are selected per iteration, not concatenated.** Loading
   all seven would be ~40 KB of technique text every turn, most of it
   irrelevant. Select by the current bottleneck from `N61`:

   | diagnosis | strategy files loaded |
   |---|---|
   | `dram_bound` | `kv-compression`, `memory-layout`, `block-sparse` |
   | `sram_bound` | `memory-layout`, `dataflow-tiling` |
   | `compute_bound` | `t-a-zero-gated-mac`, `nm-structured` |
   | `imbalance` | `block-sparse`, `dataflow-tiling` |

   This is also what makes the technique catalogue extensible without growing
   the prompt: adding an eighth technique adds a file, not tokens.

**Prompt-cache ordering.** CHIA's Table 6 shows cache-read tokens dominating
cost. Compose in stability order — `system/` and `shared/` first (immutable
across the whole run), `strategy/` next (changes only when the bottleneck
moves), `task/` last (changes every iteration). Today the volatile parts are
interleaved, so the cacheable prefix is short.

---

## 3. Execution and verification guidelines to extract

These are the "skills" that stop the agent wasting iterations. Some are already
in `propose_rtl.md` and would move to `shared/execution-rules.md`; the rest are
new and come from measured failures in this repo.

### 3.1 Elaboration rules — when to pay 20–40 minutes

```
ALWAYS run the compile gate before ending your turn:
    cd /home/ray/chipyard && source env.sh && sbt -batch "project gemmini" compile
  1-3 min against the 20-40 min an elaboration costs. An iteration that dies
  on a type error you could have seen in 2 minutes is the most wasteful thing
  you can do here.  [already in propose_rtl.md -- keep verbatim]

NEVER assume a marker edit changed the hardware. A Scala `if` on a parameter
  that stays false builds NO hardware. If rtl_digest moves and netlist_digest
  does not, the edit instantiated nothing -- that is the RTL_NOOP verdict, and
  it costs a full iteration.  [measured: this gate was silently broken until
  2026-09-23; see docs/claude_opus5_loop_status.md §5]

A SOFTWARE-only move (k_chunk, x_resident, b_blocks) does not change hw_hash,
  so elaboration is a cache hit and the iteration is ~20 min cheaper. When two
  candidate moves look equally promising and one is software-only, propose it
  first -- you learn the same amount for a third of the wall clock.
```

### 3.2 Dry-run checkpoints — cheap checks before expensive stages

```
BEFORE ending a turn:
  1. compile gate (above)                              ~2 min
  2. verify every write landed: `git -C <repo> status --short` must show the
     files you intended and NOTHING else. `sed` exits 0 when it matches
     nothing -- a failed edit is indistinguishable from a successful one
     until the elaboration 20 minutes later.   [already in propose_rtl.md]
  3. check the marker round-trip: read back SparseCraftParams.scala and
     confirm the field you set is the field that parses out. A marker the
     harness cannot parse silently reverts to the default -- this made the
     whole software half of co-design inert for a period in September.
  4. state your PREDICTION before the measurement exists. A prediction you
     cannot make is a mutation you do not understand.
```

### 3.3 Error recovery protocol

Per failure class, with the handler that owns it:

| class | detector | handler | attempts | charged to budget? |
|---|---|---|---|---|
| Scala/Chisel compile | non-zero exit + file:line | repair agent, error text + ±30 lines only | 2 | no |
| Gemmini `require()` | assertion at elaboration | **promote to a T0 rule** so it is never paid for twice | 0 | no |
| Verilator lint | warning class escalated to fatal | repair agent | 2 | no |
| sim hang / deadlock | wall-clock + no-progress watchdog | debug prompt with FSM state | 1 | no |
| functional divergence | first mismatch index | **shrink first**, then repair | 2 | no |
| numerical divergence | three-way verdict | record accuracy as an objective | — | **yes** |
| timing miss | WNS < 0 | **not an error** — a Pareto point | 0 | yes |
| **infra (preemption, OOM)** | exit-code classification | reschedule; **never shown to the agent** | — | **no** |

The last row is the one that silently corrupts a search: a preempted worker
currently reports "your design failed to build".

---

## 4. `loop.py` integration plan

Ordered so each step is independently shippable and testable. Node numbering
follows `SparseCraft_Technical_Review.md` §3.2.

### Step 1 — prompt loader (no behaviour change) · ~0.5 day

Extend `agent.load_prompt()` with `{{include: <path>}}` resolution and a
`compose(system=[...], task=..., strategy=[...])` helper. Move the existing two
prompts into the new tree with **byte-identical content**, so the first commit
changes zero model behaviour and is verifiable by diffing a rendered prompt
against the old file.

### Step 2 — async synthesis · ~0.5 day · **biggest win per line**

One call site. Dispatch `synthesize_recipe` as a future on T2a pass; resolve it
at `N60` or let it land late and update the front. Saves ~2 h per 15-iteration
run and removes 19.6% from the critical path.

```python
syn_future = synth_recipe.synthesize_recipe.chia_remote(...)   # no get()
...                                                             # sim, score
syn = get(syn_future)                                           # resolve later
```

### Step 3 — error classifier + repair agent · ~1 day

- `N71 ErrorClassifier` — programmatic, the table in §3.3, keyed on exit codes
  and log patterns. **Infra failures exit here and never reach the agent.**
- `N72 FailureShrinker` — programmatic binary search over sequence length /
  head / block index before showing a failure to any model.
- `N73 RepairAgent` — second `ClaudeCodeLLM` from `system/repairer.md`, bounded
  attempts, cheaper effort (`SPARSECRAFT_CLAUDE_EFFORT=high`), re-entering
  through the same scope check.

`agent.make_llm` already supports this; the loop simply never constructs a
second LLM. `adapted/repair.md` and `adapted/triage.md` are the content.

### Step 4 — split diagnose · ~0.5 day

`N61` becomes a rule table over the counters:

```
conflict_stalls/cycles > 0.15            -> bank_conflict_bound
dma_idle < 0.2 and mac_util < 0.4        -> load_imbalance
bytes_per_token flat and cycles up       -> index_decode_bound
dram_pct > 60                            -> dram_bound
sram_pct > 60                            -> sram_bound
else                                     -> unclassified   (escalate to LLM)
```

This also feeds the `strategy/` selection in §2, and removes the
diagnose-and-propose conflict of interest.

### Step 5 — candidate fan-out · ~1–2 days

`N10` emits **K ≥ 3** typed candidates with predicted deltas (the output
contract already has a `==PREDICTION==` block). `N11` selects programmatically
after T0/T0.5/T1 filtering. `N24` dispatches survivors in parallel.

Two things fall out for free: an auditable search policy you can name in the
paper, and an **agent prediction-accuracy figure** (predicted vs measured
delta) that nobody else will have.

### Step 6 — technique expansion · ~2–3 days

Only after Steps 1–5. Adding techniques to a loop that wastes 71% of its
proposals multiplies the waste.

---

## 5. Technique catalogue — what to add, and one to fix first

From `SparseCraft_Technical_Review.md`, ranked by RTL delta against baseline
Gemmini:

| technique | RTL delta | why it fits here |
|---|---|---|
| **Fix T-B measurability** | none — harness only | see below. Highest value: it makes an *existing* technique searchable |
| **Dataflow/tiling (L1)** | none | pure config + kernel; `k_chunk`/`b_blocks` already exist and are under-explored |
| **Memory layout (L8)** | none | zero area cost, and the review calls it *"the cheapest place for your agent to produce a novel result"* |
| **N:M structured** | small | 4→2 operand mux per lane + index SRAM; deterministic, no load imbalance |
| **Block-sparse** | small | if `B` = PE tile dim, the array needs **no modification** — address generator only |
| **KV compression** | small–medium | Gemmini's `mvin_scale_args` is already the right hook for per-group dequant |
| ~~element-wise zero-skip~~ | **large — avoid** | intersection + Benes crossbar + scatter-add; the review explicitly recommends excluding it |

**T-B must be fixed before anything is added.** It is wired into the hardware
and correct, but invisible to scoring: `sc_skip` gates one `mem.read` and saves
zero cycles, there is no ZBU counter in `CounterFile.scala`, and the energy
model has no term that could consume one. Its only measurable effect is **+4.4%
area** — strictly Pareto-dominated, so a correctly-reasoning agent rejects it
every time, which is exactly what has been observed.

Making it searchable needs two things landed **together**: a skipped-read
counter in the RTL (harness-owned, like `MAC_GATED_TOTAL` — a counter the agent
can edit is a counter it can fake) and an `e_sram` term that consumes it. Both
`t1_model.py` and `metrics.py` are in `IMMUTABLE_FILES`, so **this cannot be
done while a run is in flight**.

---

## 6. Sequencing and effort

| step | effort | independent? | ship before technique work? |
|---|---|---|---|
| 1. prompt loader + tree | 0.5 d | yes | yes |
| 2. async synthesis | 0.5 d | yes | yes |
| 3. classifier + repair | 1 d | yes | yes |
| 4. split diagnose | 0.5 d | needs 1 | yes |
| 5. fan-out | 1–2 d | needs 1, 4 | yes |
| 6. T-B measurability fix | 1 d | needs a quiet tree | **first of the technique work** |
| 7. new techniques | 2–3 d | needs 1–6 | last |

**~4 days to a materially better loop before a single new technique is added.**
Steps 2 and 3 alone recover roughly 2 hours per run and turn the majority of
gate failures from lost iterations into bounded repairs.

## 7. What this plan deliberately does not do

- **No gem5.** The review is explicit: CHIA's own case study spent 10.5 days
  aligning a gem5 model to BOOM RTL for ~6% holdout misalignment. T1 plus a
  calibrated T2b occupies the same rung far cheaper.
- **No element-wise sparse intersection RTL.** Multi-week, and the most likely
  single cause of missing a code freeze.
- **No change to what is scored** until the power flow is fixed. Energy stays
  modelled and labelled; see `docs/claude_opus5_loop_status.md` §4.
- **Nothing before the 25 Sep deadline.** `runs/final15` is in flight.
