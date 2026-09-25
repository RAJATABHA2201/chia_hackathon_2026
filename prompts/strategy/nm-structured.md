### T-C — N:M structured sparsity (2:4 lineage)

Within every group of `M` contiguous elements along the reduction (K) dimension,
exactly `N` survive. The surviving positions are recorded as a narrow index and the
PE selects its operand through a small mux instead of reading the group densely.

- Lives in `PE.scala`, which is **yours** — the operand select sits in front of the
  existing `MacUnit`, on the streaming operand.
- **The only sparsity form here with zero load imbalance by construction.** Every
  lane retires the same number of MACs every cycle, so PE utilisation is
  dense-equivalent and deterministic. There is no work-stealing, no arbitration, no
  tail effect. Compare T-B, whose benefit depends entirely on where the zeros fall.
- Speedup is exactly `M/N` **if the mesh consumes the compacted stream**. Read the
  next paragraph before you predict one.

**What you can and cannot reach.** The operand mux is yours. The *issue* path that
would feed the mesh a compacted stream is `ExecuteController.scala`, which is not in
your writable set. So a `PE.scala`-only N:M lands as an **energy and area** move —
the multiplier sees a selected operand instead of a dense group — and **not** as a
cycle move. Predict `time: flat` unless you can point at the mechanism that shortens
the issue schedule, and if you can, say which file it would need.

**The metadata cost, exactly.** `ceil(log2(C(M,N)))` bits per group. For 2:4 that is
2 bits per surviving element: **12.5% overhead at INT8**, 6.25% at INT16. Keep it in
its own narrow storage, not interleaved with the payload — interleaving destroys the
payload's power-of-two addressing and costs you more in address generation than the
mux saves.

**Hard constraint, and T0 will reject you for it.** The group size `M` must divide
the spatial-array reduction width. This mesh is `meshRows x meshColumns` with
`tileRows x tileColumns` inside it; with a 16-wide reduction, `M ∈ {2, 4, 8, 16}`.
`M = 3` or `M = 6` is not a design trade-off, it is an illegal state.

**Where this technique genuinely competes with T-A.** Both act on the multiplier.
T-A gates a zero it *finds* at run time and pays a comparator per PE; N:M
*guarantees* the density offline and pays a mux plus an index read. On this
workload `MAC_GATED_TOTAL` reports 93.75% of multiplies are already zero-operand,
which is far sparser than 2:4 — so N:M would be **denser than the data**, forcing
work that T-A already skips for free. Enabling both is not obviously additive and
may be strictly worse than T-A alone. If you propose N:M here, say what it buys
that T-A does not, or propose it for a workload where the data is not this sparse.
