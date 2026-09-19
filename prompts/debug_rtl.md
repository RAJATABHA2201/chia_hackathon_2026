# SparseCraft repair turn (N71 classify + N73 repair)

A design you or another turn produced has failed. You get **one** turn to repair it. If you
cannot, say so plainly — the harness reverts to the parent design and the search continues,
which is a better outcome than a speculative edit that fails the same way twice.

You are the debug half of an Implement/Debug split. The proposer chose the mechanism; your
job is to make that mechanism build and compute the right answer, **not** to redesign it or
to quietly abandon it for something easier.

## What failed

```
failure class: ${FAILURE_CLASS}
```

```
${ERROR_TAIL}
```

## The design that failed

```json
${STATE}
```

Your writable set is unchanged and is exactly:

```
${PARAMS_PATH}
${PE_PATH}
${ZBU_PATH}
```

Every command runs with cwd `${CHIPYARD}`.

## Classify first, then act

Name the class before you edit. The right repair differs completely by class, and the
failure modes below are the ones this project has actually produced.

### `ELABORATION_FAILED`

Chisel elaborated to an error, or firtool refused the output.

- **Width mismatch / `Connection between sink and source failed`** — the commonest. Chisel
  does not auto-truncate. Find the two widths and make the narrowing explicit.
- **`Reference ... is not fully initialized`** — a `Wire` or an IO field on a path your
  `when` chain does not cover. Every branch must drive it, or give it a default before the
  `when`.
- **Combinational loop** — you read a signal you drive in the same cycle. Your zero-detect
  most likely reads the scratchpad read data that the skip signal itself gates. Break it
  with a register; a cycle of detection latency is a legitimate cost.
- **`Cannot index` / dynamic index out of range** — granule size does not divide the array
  dimension, so the bitmap index overruns. Fix the parameter, not the index.

### `KERNEL_BUILD_FAILED`

The kernel did not compile against the regenerated `gemmini_params.h`. You did not write
the kernel and you may not edit it — so this means your config changed a parameter the
kernel's static assertions depend on. Read the error, then change the parameter back or to
something the kernel can accept.

### `EQUIV_FAILED` — **the important one**

The design built, simulated, and produced **the wrong answer**. `equiv_mismatches` is
non-zero.

This is almost never a subtle numerical issue, because both sanctioned techniques are
bit-exact by construction: `c + 0·w ≡ c`, and skipping an identically-zero contribution
changes nothing. So a mismatch means the hardware **dropped data that was not zero**.

Work the list in this order:

1. **Off-by-one in the bitmap index.** Granule `g` of row `r` flagged, granule `g±1`
   skipped. Check the index arithmetic against the array dimension and the granule size.
2. **Stale bitmap.** The scratchpad row was overwritten and the bitmap was not updated, so
   a granule that used to be zero is still flagged. Every write path that touches the data
   must touch the bitmap.
3. **Detection on the wrong operand.** You flagged A and skipped on B, or the reverse.
4. **Gating the accumulator, not the product.** T-A must bypass `in_c` to `out_d`. If it
   zeroes the output instead, every gated PE destroys the partial sum flowing through it.
5. **Skip suppressed the wrong instruction.** A suppressed `preload` without its `compute`
   leaves the mesh holding the previous weights, which corrupts the *next* tile, not this
   one — so the mismatch appears in a tile you did not touch.

**Do not** widen a tolerance, weaken a comparison, or reduce the check's coverage. The
equivalence checker is not in your writable set and attempting to reach it ends the run.

### `T0_ILLEGAL`

A structural rule rejected the design in microseconds. The violated rule is named in the
error. Fix the parameter it names; do not try to route around it.

### `SCOPE_VIOLATION`

A previous turn wrote outside the three-file writable set. Revert whatever is outside it:

```bash
git status --porcelain
git checkout -- <the offending path>
```

## The one Chisel trap that will cost you your first iteration

Gemmini's PE is **generic**: `class PE[T <: Data](...)(implicit ev: Arithmetic[T])`. `io.in_a`
is of abstract type `T`, not `UInt` or `SInt`. The `Arithmetic[T]` typeclass
(`Arithmetic.scala`) gives you exactly these operations:

```
mac  *  +  -  >>  >  zero  identity  withWidthOf  clippedToWidthOf  relu  minimum
```

**There is no `===` and no `=/=` on `T`.** So this, which is the obvious thing to write,
does not compile:

```scala
val gated = io.in_a === 0.U        // type mismatch; found chisel3.UInt, required: T
```

Test for zero on the raw bits instead:

```scala
val a_is_zero = io.in_a.asUInt === io.in_a.zero.asUInt
```

`.asUInt` is available on any `Data`, and `.zero` comes from the typeclass, so this form is
correct for every element type Gemmini can be configured with. Writing
`io.in_a.asUInt === 0.U` also works for the integer configs used here, but it is *not*
correct for recoded-float configs, where zero is not all-zero bits — prefer the `.zero`
form.

This is not hypothetical: it was reproduced on this host, and the compile gate catches it
in 19 seconds with an exact file:line:col.

## How to edit

Same rules as the proposer, for the same reasons:

- **Never `sed` Chisel.** Rewrite whole files with a heredoc. `sed` exits 0 on no match, so
  a failed repair is indistinguishable from a successful one until the build has been paid
  for.
- **Verify**: `git diff --stat -- ${PARAMS_PATH} ${PE_PATH} ${ZBU_PATH}`. No output, no edit.
- **Compile before ending the turn**: `cd ${CHIPYARD} && source env.sh && sbt -batch "project gemmini" compile 2>&1 | tail -40`.
  This is the whole point of a repair turn — do not hand back something you have not
  compiled.
- **One tool call per turn.** The MCP transport on this host drops second-and-later results
  on the same session and your turn will hang waiting for them.
- Do not elaborate, do not run Verilator, do not `git commit`.

## Scope of the repair

Repair the mechanism the proposer chose. Specifically:

- **Do** fix widths, initialisation, indexing, pipelining, and missing bitmap updates.
- **Do** reduce a parameter (granule size, bitmap depth) if the failure is that it does not
  fit or does not divide — and say that you did.
- **Do not** disable the technique to make the build pass. A design with the technique
  switched off is the parent design with extra steps, and the harness scores it as a
  duplicate.
- **Do not** substitute a different technique.
- **Do not** delete or weaken an assertion so elaboration proceeds. An assertion that fires
  is telling you something true.

If the failure is not repairable within those limits — say, the mechanism needs an
interface the harness-owned hooks do not expose — **say so and stop.** Name the interface
you would have needed. That is a complete and useful outcome; it tells the loop where the
scaffold is too narrow.

## Required output format

```
### ==REPAIR==
class:     ELABORATION_FAILED | KERNEL_BUILD_FAILED | EQUIV_FAILED | T0_ILLEGAL | SCOPE_VIOLATION
cause:     the root cause in one or two sentences -- the mechanism, not the error text
fix:       what you changed, and why that addresses the cause
files:     the files you changed, one per line
compiled:  PASS | FAIL
```

If you could not repair it, use `fix: NONE` and one line naming what would have been
needed.
