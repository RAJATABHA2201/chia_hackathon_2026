## How to edit Chisel here, and how not to

**Do not use `sed` on Chisel.** It has cost this loop whole iterations. `sed` exits 0 when
it matches nothing, so a failed edit is indistinguishable from a successful one until the
harness scores you as a duplicate of your parent — after paying for the full build. Chisel
is nested, brace-delimited and column-aligned; line-oriented substitution does not survive
contact with it.

Instead: **write the whole file with a heredoc, then verify with `git -C ... status`.**

```bash
cat > generators/gemmini/src/main/scala/gemmini/SparseCraftSparsity.scala <<'EOF'
... the complete file ...
EOF
git -C generators/gemmini status --short -- src/main/scala/gemmini/SparseCraftSparsity.scala
```

` M` means modified, `??` means a new file you created — either means the edit landed.
Nothing at all means it did not.

**Both halves of that command matter.** `generators/gemmini` is a git SUBMODULE, and git
does not descend into one for a path-limited status, so the same query run from the
chipyard root prints nothing however the file changed — you need the `-C`. And
`SparseCraftSparsity.scala` is UNTRACKED (you create it; the tree reset removes it every
iteration), so `git diff` cannot see it either. Using either shorter form reports a
perfectly good write as a failure. That has already cost this project an iteration: the
agent wrote the file, saw nothing, rewrote it three times, and concluded the editor tool
was broken.

**Compile before you finish.** You have a compile gate available and it takes 1–3 minutes
against the 20 minutes an elaboration costs:

```bash
cd /home/ray/chipyard && source env.sh && sbt -batch "project gemmini" compile 2>&1 | tail -40
```

An iteration that dies on a type error you could have seen in 2 minutes is the most
wasteful thing you can do here. Run it. If it fails, fix it and run it again. Only end
your turn on a clean compile.

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
