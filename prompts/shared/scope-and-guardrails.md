## Where you work

`/home/ray/chipyard`, inside the build container. Every bash command is rooted there.

Your **entire writable set** is these three files:

```
generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala      (config)
generators/gemmini/src/main/scala/gemmini/PE.scala                     (T-A)
generators/gemmini/src/main/scala/gemmini/SparseCraftSparsity.scala    (T-B)
```

A path allowlist runs over `git status` **before** your diff is collected. Touching
anything else — including the three integration hooks, the kernel, the golden reference,
the harness config — rejects the iteration and resets the tree, with no evaluation and no
measurement. That is 20–40 minutes lost for nothing.

## Explicitly forbidden

These are checked, and attempting them ends the iteration:

- Do not edit the golden reference, the equivalence checker, the tolerance configuration,
  the objective weights, the T0 rules, the metric-extraction scripts, the kernel, the
  workload selection, or the harness config.
- Do not edit the three ZBU integration hooks (`Scratchpad.scala`, `ExecuteController.scala`,
  `CounterFile.scala`). They are not yours.
- Do not weaken or delete an assertion to make an elaboration pass.
- Do not change Verilator warning flags, assertion severities, or simulation timeouts.
- Do not shrink the array, scratchpad or accumulator merely to make a diagnostic look
  better.
- Do not make the skip logic drop data it has not proven to be zero. The equivalence gate
  will catch it and the iteration is scored as a correctness failure, which is worse than
  a regression.
