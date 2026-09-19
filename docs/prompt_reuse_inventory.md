# Prompt Reuse Inventory

Audit date 2026-09-16. Compares `sparsecraft/prompts/` against the reference
prompts shipped under `chia/examples/`.

**Tree note:** `/home/chia-sparsecraft/chia/examples` and
`/home/chia-sparsecraft/chia-work/examples` are **byte-identical** for all 33
`.md`/`.txt` files (verified by md5 over the whole tree). `chia-work` is a
working copy, not a divergent source. All paths below use the `chia/` tree;
the `chia-work/` twin is the same file. In particular
`chia-work/examples/timing_opt/prompts/debugging.md` and
`chia/examples/timing_opt/prompts/debugging.md` are the same 6400-byte file —
and `chisel_debugging.md` / `common_debugging.md` exist in **both** trees too.

---

## Part A — Assessment of our current prompts

Axes: **R** role framing · **F** failure taxonomy · **T** tool-use contract ·
**O** output schema · **W** worked examples · **S** stop conditions.

| Our file | Bytes | Kind | R | F | T | O | W | S |
|---|---|---|---|---|---|---|---|---|
| `prompts/propose.md` | 4369 | system message (`agent.py:266`) | ~ | **N** | **BROKEN** | **N** | **N** | ~ |
| `prompts/task.md` | 1955 | per-iteration user message (`loop.py:278`) | N/A | **N** | ~ | **N** | ~ | Y |

### What our prompts already do well (do not regress these)

These have **no analogue anywhere in the reference set** and are the strongest
part of what we have:

- **Hard-constraint list** (`propose.md:34-51`) transcribed from the real
  `require()` sites, so the model learns the feasible region by name rather
  than by burning a 20–40 min elaboration. Mirrors `t0_legality.py:100-140`.
- **Anti-gaming objective framing** (`propose.md:30-32`): "MAC utilisation,
  SRAM footprint and off-chip bytes are diagnostics, not goals. Raising
  utilisation by shrinking the array will be scored as the regression it is."
- **Signature → bottleneck → lever table** (`propose.md:62-70`), which is the
  prompt-side mirror of `loop.py:86-97` `diagnose()`.
- **Levers catalogue L1/L2/L3/L5/L7/L10** (`propose.md:53-60`) tied to real
  parameter names.

### Concrete defects found

1. **The tool roster is wrong, and the two files disagree with each other.**
   `propose.md:78-80` advertises four tools:
   `query_history`, `get_pareto_front`, `compare(state_a, state_b)`,
   `query_density(region, granularity)`.
   Only **two** of those exist — `agent.py:348` and `agent.py:352`.
   `compare()` and `query_density()` are **not implemented anywhere**. Meanwhile
   `read_status` (which *does* exist, `agent.py:319`) is absent from
   `propose.md` and present only in `task.md:56`. So the system prompt promises
   two tools the model cannot call and hides one it can.

2. **No failure taxonomy.** The loop emits five distinct terminal verdicts —
   `SCOPE_VIOLATION` (`loop.py:296`), `T0_ILLEGAL` (`:316`),
   `ELABORATION_FAILED` (`:348`), `KERNEL_BUILD_FAILED` (`:358`),
   `TRIPWIRE_FAILED` (`:374`) — and neither prompt explains what any of them
   mean or how the model should respond differently to each. The model receives
   a raw stderr tail (`loop.py:352`, `:361`) with no interpretive frame.

3. **No output schema.** Neither prompt requires any response format. The loop
   reads the model's work back off the filesystem (`diff_nodes.collect_diff`),
   so the *stated prediction* asked for in `propose.md:19-21` is never captured
   in a machine-readable field — it survives only as a free-text Scala comment.

4. **No worked example of the actual edit.** `task.md:19-30` shows a worked
   example of the *harness config* (`SparseCraftConfig`), which the model must
   create once. There is no before/after example of a `SparseCraftParams.scala`
   mutation — the thing it does every single iteration.

5. **No stuck/escalation protocol and no bounded attempts.** Nothing tells the
   model what to do when its lever produces no movement, and nothing legitimises
   "this diagnosis is not actionable" as a valid outcome.

---

## Part B — Reuse inventory

| Source prompt (abs path) | Purpose | Verdict | What makes it good | Changes needed for sparse work | Proposed destination |
|---|---|---|---|---|---|
| `/home/chia-sparsecraft/chia/examples/timing_opt/prompts/debugging.md` (6.4 KB) | Repair node: fix a broken optimization without reverting it | **ADAPT** | The only 6/6 file in the reference set. Hard-prohibitions block with "violating these is a failure of this node"; an explicit **reward-hacking taxonomy** ("a revert is NOT a fix… detected automatically and rejected"); **confidence-gated action protocol** (rate 1–5, High → patch directly and do *not* run tests, Low → instrument with printf, never guess-fix); a "when you feel stuck" escalation ladder; required output sections with "missing sections will be treated as a failed run"; and a yes/no **self-audit checklist flagged as auto-verified**. | Swap the BOOM anti-revert surface (`WithBoom*` mixins, `enable*` flags) for our equivalents: disabling `has_normalizations`, shrinking the array to inflate utilisation, writing outside `SparseCraftParams.scala`, evading the byte tripwire. The self-audit block should mirror the checks that are *actually* auto-verified here (`t0_legality.check_patch_scope`, `metrics.tripwire_ok`) so the claim stays true. | `sparsecraft/prompts/repair.md` (new; feeds a future N73) |
| `…/examples/timing_opt/prompts/common_debugging.md` (10.3 KB) | General hardware-debug methodology, injected as required reading | **USE-AS-IS** | Domain-neutral debugging discipline: hypothesis formation, bisection, instrumentation. Only one incidental timing mention (`:149`, "non-critical path", used in the throughput sense). | None required for a first adoption. | `sparsecraft/prompts/aux/common_debugging.md` |
| `…/examples/timing_opt/prompts/chisel_debugging.md` (27.8 KB) | Chisel/Verilog trap textbook, injected as required reading | **ADAPT** | Eight sections of real Chisel failure modes — last-connect semantics, width inference, `Decoupled`/`Queue` misuse, elaboration-vs-simulation confusion, firtool DCE, Verilator random-init, `RegArray` vs `SyncReadMem`. Every subsection closes with a bolded **Rule:**. Directly relevant: our `ELABORATION_FAILED` path is exactly this class of error. | Strip §7 "BOOM-Specific Insights". Would need a Gemmini/CDE section added (`GemminiArrayConfig.copy` semantics, where the `require()` sites live) — **not authored in this session**. | `sparsecraft/prompts/aux/chisel_debugging.md` |
| `…/examples/circt_issue_solver/prompts/system.md` (4.5 KB) | Senior-engineer charter + environment + scope rules | **ADAPT** | Best *system-prompt skeleton* in the set: `Environment:` (exact paths, what is prebuilt/read-only, what rebuilds fast) → `SCOPE` → `Rules:` → `Code style:`. Two devices we lack outright: (a) the SCOPE block **enumerates the specific cheat paths and forbids them by name** (patchelf, `LD_PRELOAD`, `.so` swap) rather than gesturing at "don't cheat"; (b) "This is one step of an automated pipeline — there is no human to ask", plus explicitly legitimising a non-action outcome ("reporting it as out of scope IS a complete, correct outcome"). | Replace the CIRCT tree/SDK paths with the chipyard container paths; replace the LLVM/MLIR out-of-scope rule with the one-writable-file rule; drop the LLVM code-style section entirely. | merge into `sparsecraft/prompts/propose.md` |
| `…/examples/riscv_extensions/prompts/system.md` (5.6 KB) | RISC-V microarchitect charter, loop mechanics, tool roster | **ADAPT** | **Closest structural analogue to ours in the entire reference set** — same framework, same chipyard container, same `BashTool` editor, same propose→build→measure→feedback loop. Has an explicit "Rule #1: the spec is truth" anchor and a tool roster that matches what is actually wired up. | Retarget from ISA-extension implementation to parameter-space mutation; our objective is Pareto non-dominance, not "tests pass". | merge into `sparsecraft/prompts/propose.md` |
| `…/examples/riscv_extensions/prompts/task.md` (1.4 KB) | Per-iteration work order | **ADAPT** | Direct analogue of our `task.md`, and tighter: states the target, the config, and the concrete failing artifacts in a fixed order. | Substitute our per-iteration fields (`PARENT_STATE`, `DIAGNOSIS`, writable path). Ours already covers most of this; lift the ordering discipline, not the content. | `sparsecraft/prompts/task.md` (revision) |
| `…/examples/riscv_extensions/prompts/debug.md` (1.9 KB) | Spike-divergence triage on a commit log | **ADAPT** | Triage framing for "measured behaviour diverged from reference" — the shape our `TRIPWIRE_FAILED` and future N41 paths need. | Divergence source becomes Gemmini counters vs. expected, not Spike commit log. | fold into `sparsecraft/prompts/repair.md` |
| `…/examples/gem5_align/prompts/align_node_prompt.md` (30.2 KB) | Cycle-alignment task prompt; self-contained (acts as system + task) | **ADAPT** (selective harvest) | Scores on all six axes. Three devices worth taking: (a) a **counter ↔ stat mapping table** — precisely the shape our `propose.md` diagnosis table should grow into; (b) a **sentinel-delimited output schema** (`==ALIGNMENT_OUTPUT==` / `==SOURCE_PATCH==`) which is the cheapest possible fix for our missing output schema; (c) a `Warnings` section whose rules are container-performance facts, not domain facts — "one tool call per turn", "don't grep the chipyard root" — and are liftable near-verbatim. | Take the three devices only; the other ~27 KB is gem5-O3/BOOM alignment content with no analogue here. | harvest into `sparsecraft/prompts/propose.md` |
| `…/examples/circt_issue_solver/prompts/assess.md` (4.4 KB) | Pre-triage gate: is this actionable, is expected behaviour clear | **ADAPT** (deferred) | A *gate* prompt that legitimises "not actionable" as a first-class verdict before any work is spent. We have no triage step at all — every diagnosis currently flows straight into a mutation attempt. | Gate condition becomes "is this diagnosis actionable with the levers available", not "is this a real bug". | future `sparsecraft/prompts/triage.md` (blocked on N11 existing) |
| `…/examples/circt_issue_solver/prompts/regression.md` (2.0 KB) | Repair a fix that broke existing lit tests | **SKIP** | Good anti-regression framing. | No analogue until we have a regression suite — N40/N41 are both missing (see `docs/chia_loop_state.md` §3a). Revisit when they land. | — |
| `…/examples/circt_issue_solver/prompts/reproduce.md` (1.9 KB) | Build a minimal `repro.sh` with an exit-0-iff-fixed contract | **SKIP** | — | Issue-reproduction workflow; an optimization loop has no "reproduce the bug" phase. | — |
| `…/examples/circt_issue_solver/prompts/fix.md` (2.2 KB) | Root-cause fix + lit regression test | **SKIP** | — | Same: issue-driven, not optimization-driven. | — |
| `…/examples/circt_issue_solver/prompts/review.md` (3.1 KB) | Address PR feedback, author replies | **SKIP** | — | No human-review stage in this loop. | — |
| `…/examples/circt_issue_solver/prompts/review_assess.md` (3.3 KB) | Gate: is reviewer feedback actionable | **SKIP** | — | Same. | — |
| `…/examples/circt_issue_solver/prompts/writeup.md` (1.7 KB) | Emit a PR description from diff + verdict | **SKIP** | — | No PR artifact here; `runs/*/iter_NNN.json` already serves this role programmatically. | — |
| `…/examples/memcpy/prompts/implement.md` (2.2 KB) | Spec for a RoCC memcpy accelerator | **SKIP** | — | Spec-to-RTL authoring task; ours is parameter-space mutation on an existing generator. Different shape. | — |
| `…/examples/memcpy/prompts/debug.md` (0.9 KB) | Fix a build/sim failure without disabling the accelerator | **SKIP** | Uses the same anti-disable device as `timing_opt/debugging.md`. | Superseded — `timing_opt/debugging.md` does the same thing far more thoroughly. Adopting both would duplicate. | — |
| `…/examples/timing_opt/prompts/improve_timing.md` (16.0 KB) | Critical-path reduction, 4 phases + sub-block A/B synth | **SKIP** (harvest skeleton only) | The Phase 1–4 + "Phase 4b A/B experiment" skeleton is a genuinely good structure for any measure-driven optimization node. | Everything *inside* the skeleton is timing-specific and actively misleading — see Part D. Take the four-phase shape, discard the content. | — |
| `…/examples/timing_opt/prompts/improve_timing_ironlaw.md` (17.5 KB) | Same, objective = maximize IPC × frequency | **SKIP** | — | See Part D — the most dangerous file in the set for our purposes. | — |
| `…/examples/timing_opt/prompts/improve_timing_ironlaw_noab.md` (10.6 KB) | Iron-law variant, single tool, multi-cone attack | **SKIP** | The "attack every high-latency cone simultaneously, the metric only moves when all come down together" framing is an elegant multi-target device. | Same iron-law contamination; the framing would have to be fully re-typed to sparsity bottlenecks. | — |
| `…/examples/opencode-nvidia/nvidia_opencode_loop.py:155` · `…/vllm-opencode/vllm_opencode_loop.py:143` · `…/hello-world/hello-world-s4.py:38` | Inline sentinel health-check strings | **SKIP** ×3 | — | Trivial connectivity probes, not prompts. | — |

**Verdict counts: USE-AS-IS 1 · ADAPT 8 · SKIP 14.**

Also worth noting: `examples/common/common_nodes.py:405,476` implements
`load_prompt()` + `debug_failure()` with `$1..$9`/`$ARGUMENTS` substitution and
`{AUX_DIR}` aux-file injection. That aux mechanism is how `debugging.md` pulls
in the two textbooks, and it is the machinery we would need in order to adopt
rows 2–3 above. Our `agent.load_prompt()` (`agent.py:209`) does `${NAME}`
substitution but has **no aux-injection equivalent**.

---

## Part C — Prioritized adoption order

Highest leverage first. Each item names the defect from Part A it closes.

1. **Fix the tool roster in `propose.md`** — closes defect #1. Not a reuse task
   at all, it is a correctness bug: the system prompt currently advertises two
   nonexistent tools and omits one real one. Costs minutes, and until it is
   fixed every iteration starts by lying to the model.

2. **Adopt the `circt/system.md` + `riscv_extensions/system.md` skeleton into
   `propose.md`** — closes defects #2 (partially) and #5. Highest structural
   payoff: adds the Environment block, the enumerate-the-cheat-paths SCOPE
   device, "there is no human to ask", and legitimised non-action outcomes,
   while preserving everything in Part A's "do not regress" list.

3. **Add the gem5 sentinel output schema to `propose.md`** — closes defect #3.
   Cheapest possible fix (two sentinel markers), and it makes the stated
   prediction in `propose.md:19-21` machine-capturable, which in turn is what a
   future prediction-accuracy figure would be built on.

4. **Create `prompts/repair.md` from `timing_opt/debugging.md` (+ the riscv
   `debug.md` triage framing)** — closes defect #2 properly. Note this is
   **blocked on N73 existing** (`docs/chia_loop_state.md` §3a item 3): today
   every failure path just writes a diagnosis string and `continue`s, so there
   is no node to feed this prompt to. Write the prompt with the repair node.

5. **Add `aux/` injection to `agent.load_prompt()`, then adopt
   `common_debugging.md` (as-is) and `chisel_debugging.md` (adapted)** — the
   machinery first, then the two textbooks. Pays off specifically on the
   `ELABORATION_FAILED` path.

6. **Add a worked before/after example of a `SparseCraftParams.scala` mutation
   to `task.md`** — closes defect #4. Deliberately last among the substantive
   items: it is the one that most requires authoring genuinely new
   sparsity-specific content, which is out of scope for this session.

7. **`triage.md` from `circt/assess.md`** — deferred; blocked on N11 (Select
   Candidate), which does not exist.

---

## Part D — Reference prompts that would actively mislead a sparsity loop

Flagging these explicitly because three of them are large, well-written, and
superficially look like the closest match to "optimize a hardware design" — so
they are the most likely to be lifted by mistake.

- **`improve_timing.md:77`** — "Do not worry about the specific target clock
  period… period is now 5 ns / 200 MHz… try to maximize the product of IPC with
  frequency ala the iron law." Bakes a frequency objective into the charter. Our
  N60 admits on `t = cycles × period`, `E`, `A` (`loop.py:427-433`) — chasing
  frequency alone is a strict sub-goal and would distort the front.

- **`improve_timing_ironlaw.md` / `…_noab.md`, Goal sections (~lines 17–31)** —
  "minimize `CPI × cycle_time`", "**IPC loss is fully acceptable** — even
  substantial", and explicit licence to use "reduced issue/dispatch width,
  smaller queues, simpler schedulers". **This is the single most dangerous
  passage in the reference set for us.** Lifted into a Gemmini sparsity loop it
  authorizes shrinking the systolic array and queues — precisely the move
  `propose.md:31-32` already warns against, and precisely what our T1/T3 area
  term would reward while destroying the throughput the sparsity work exists to
  gain.

- **`improve_timing.md:96`** — "Decreasing the size… of structures to decrease
  critical path is HIGHLY DISCOURAGED and will be penalized." Directly
  contradicts the two ironlaw variants above. Whichever file you lift, you
  inherit an arbitrary and unexplained stance on structure resizing that has no
  bearing on N:M metadata widths or BSR tile sizing.

- **`improve_timing*.md`, Phase 4b and the tool sections** — the entire success
  criterion is `delta_ns = child.worst_slack − parent.worst_slack`, with
  "iterate if slack improved by less than ~10% of your target reduction", plus
  `vlsi_top` sizing advice naming `IssueUnitCollapsing` / `Rob` /
  `RegisterFileSynthesizable`. The **A/B harness shape is liftable**;
  **slack-as-verdict is not**. Our A/B verdict must be cycles/MACs on a 2:4 or
  BSR kernel. Note we do measure slack in T3 (`synth_node.py`, `loop.py:414`),
  but as an *input to `period_ns`*, never as the objective.

- **`improve_timing*.md`, Phase 1 + "Timing Report" section** — presumes a Genus
  `final_constrained.rpt` with `^Path N:` / `Endpoint:` / `Slack` lines and
  advises clustering paths by endpoint family. No such artifact exists in our
  flow (we run yosys + OpenSTA via hammer, different report format entirely).
  Lifted verbatim it sends the model hunting for a file that is not there.

- **Low-risk, confirmed safe to lift:** `chisel_debugging.md` and
  `common_debugging.md` contain only two incidental timing references
  (`chisel_debugging.md:34`, a barrel-shifter area/timing aside;
  `common_debugging.md:149`, "non-critical path" in the throughput sense).
  `timing_opt/prompts/debugging.md` is domain-neutral apart from the `WithBoom*`
  mixin names in its prohibitions list, which are exactly the part Part B says
  to replace.
