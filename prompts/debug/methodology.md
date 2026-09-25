## Debugging method

Adapted from CHIA's `common_debugging.md` for this loop. The principles are
general; the costs, tools and gates are the ones you actually have here.

### First principle: minimise the inner loop

Everything below serves one goal: shorten *reproduce, hypothesise, fix, check*.
Before each edit ask: **what is the fastest check that tells me this fix
worked?** In this loop the checks have very different prices:

| check | who runs it | cost |
|---|---|---|
| T0 legality arithmetic | you, by hand, from the rule text | seconds |
| `sbt -batch "project gemmini" compile` | **you**, through the edit tool | 20 s to 3 min |
| Chisel elaboration + Verilator build | the harness, after your turn | 20 to 40 min |
| kernel cross-compile | the harness | 1 to 3 min |
| RTL simulation + golden comparison | the harness | about 16 min |

So: anything a compile can catch, catch it yourself before you end the turn.
Anything only elaboration or simulation can catch, you must reason about
carefully *before* ending the turn, because a wrong guess costs the harness a
full rebuild to discover.

### Layered validation: the gate ladder

The harness validates in dependency order and stops at the first failure:

```
scope -> T0 legality -> compile -> elaboration -> netlist changed (RTL_NOOP)
      -> kernel build -> simulation -> tripwire -> functional equivalence
```

**Never debug a later gate while an earlier one fails.** A lower-layer fault
corrupts everything above it, so its symptoms up there are uninterpretable.
And expect the ladder to move: fixing a compile error routinely exposes an
elaboration error, and that is progress, not a new problem. The harness will
hand you the new failure on your next attempt.

### One change at a time

Change exactly one thing per attempt. If you change two and the failure
disappears you do not know which was needed, and if it persists you do not
know which one is still wrong. A muddied attempt costs the harness a rebuild
and teaches nobody anything.

### Verify inputs before internals

When a module's output is wrong, check what it was fed first. Here that means,
in order:

1. the `// SPARSECRAFT` markers in `SparseCraftParams.scala`, which the harness
   parses into the design state (a malformed marker is not "unchanged", it is
   unparseable);
2. the elaborated parameters the kernel is compiled against (`DIM`, scratchpad
   rows, accumulator rows), which come from `gemmini_params.h`;
3. only then the Chisel logic the mutation added.

Many "logic bugs" are correct logic operating on the wrong configuration.

### Diff against the last known good

This loop hands you the cleanest possible bisection: **the parent design built,
simulated and matched the golden reference.** The mutation is the only
difference. Start from `git -C generators/gemmini diff` and the list of changed
design fields in your work order, not from a fresh read of the whole tree.

One caution: the working tree also carries harness-owned patches (counters,
the ZBU integration hook, the params file). They are in the diff too, and they
are not the mutation. Your work order names the fields and files the proposer
changed; stay inside those unless the evidence points elsewhere.

### Localise with evidence, not by reading in circles

Do not re-read RTL hoping to spot the bug by inspection. Use what the harness
measured:

- the **first mismatching output** `[i, j]`, its value and the expected value,
  and the total mismatch count;
- the hardware **counters** (`macs_issued`, `MAC_GATED_TOTAL`, DMA bytes,
  `exe_active_fraction`) against what the mutation claimed they would do;
- the compiler's and elaborator's own `file:line` messages, verbatim.

A counter that moved in the direction the mechanism predicts but by a wrong
amount is a *magnitude* bug (width, count, index). A counter that did not move
at all is a *wiring* bug (the logic is not on the path, or is dead).

### You cannot instrument the scored design

In a normal debug session you would add `printf` and assertions and re-run the
failing test. Here the harness owns simulation, and whatever you leave in the
three writable files is **what gets built, synthesised and scored**. So:

- Do not leave `printf`, debug counters or extra registers in the design.
  They cost area, can cost Fmax, and flood the simulation log.
- Encode invariants as reasoning instead: write down what must hold ("a bitmap
  bit is set only after a full-width zero write to that row") and check each
  code path against it.
- Never weaken, delete or gate off an existing `assert`/`require` to get past
  a gate. That hides the bug and is treated as an evasion.

### Root-cause discipline

Fix the cause, never the symptom.

- **Resource exhaustion is a symptom.** If a capacity rule fires, ask what the
  mutation consumed, not how to shrink something unrelated until it fits.
- **Do not hide a bug by changing parameters.** Turning a technique off,
  shrinking the array, or moving a lever back to the parent's value makes the
  gate pass by removing the thing being tested. The harness detects reverts.
- **Fixing one bug can expose another.** A large error masks smaller ones.

### Tactics

- **Bisect a coupled move.** If the mutation changed several fields or two
  mechanisms at once, decide which part the evidence implicates before editing
  either.
- **Compare symmetric constructs.** Replicated structure repeats one idiom
  (every PE, both operand paths, every bank, every `// SPARSECRAFT` marker).
  The instance that differs from its siblings is the suspect. When you check a
  signal in one instance, check it in all of them.
- **Check coincident events.** Where two events are assumed exclusive but can
  share a cycle (write and read to the same row, enqueue and dequeue, a gate
  decision and the operand arriving), ask what happens when both are high.
- **Destructive checks mask bugs.** Logic that reads state in a way that also
  changes it (a read that clears, a lookup that advances a pointer) can hide
  the real behaviour. Prefer side-effect-free reads when reasoning.

### Avoid tunnel vision

Tool calls are expensive here: the transport allows **one per turn**, so each
read costs you a full round trip. Plan your reads. If you have spent about ten
calls on one file without converging on a hypothesis, or have discarded three
hypotheses in the same module, stop and widen the search: the fault may be in
how *existing* code treats the *new* signal, or in a file you have not opened.

### When a failure is not actionable

"This failure cannot be fixed with the levers in the writable set" is a
complete and correct outcome. Report it with the evidence. It is far better
than a fix you cannot justify, which the harness will spend 20 to 40 minutes
disproving.
