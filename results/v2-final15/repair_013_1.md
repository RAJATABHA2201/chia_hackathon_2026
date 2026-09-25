<!-- work order -->
# Repair: iteration 13, attempt 1 of 3

A gate failed on the proposer's mutation.

| | |
|---|---|
| verdict | `T0_ILLEGAL` |
| failure class | **t0** |
| playbook section | **T0_ILLEGAL** in "Failure playbook" |
| re-checked after your turn | T0 legality (microseconds), then the full ladder |

The design state violates a named legality rule. Nothing was built. Work the rule's arithmetic; move a coupled field or move the mutated field PART of the way back, never all of it.

## Evidence

```
T0 violations:
  - fusion.no_materialised_S: has_normalizations=false leaves no hardware softmax (NormCmd.MAX/SUM_EXP/INV_SUM_EXP), so S would have to be materialised
```

## The proposer's stated mechanism

This is what the change was supposed to do. Preserve it.

```
### ==MUTATION==
technique: CONFIG
files:     generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala
change:    `has_normalizations: true -> false`. The `Normalizer` at Scratchpad.scala:657 collapses to `is_passthru = true` (a wire), removing its 16-lane 32-bit reduction tree, its two stat contexts and its reciprocal / reciprocal-sqrt units; the LAYERNORM/IGELU/SOFTMAX legs leave the AccumulatorScale MuxCase; CONFIG_NORM leaves the StoreController decode. RELU and the ordinary scale path are preserved by the `else` branches. All ten `// SPARSECRAFT` markers reproduced unchanged, including the frozen `workload` and `dense_mode`.
compiled:  PASS

### ==PREDICTION==
No hardware counter moves: `cycles`, `macs_issued`, `MAC_GATED_TOTAL`, `RDMA/WDMA_BYTES` and the SRAM access counts are all driven by the mvin/execute/mvout path, and the normalization block sits beside the mvout scale path without being activated by any instruction this kernel issues. The measurement that should move is the synthesized `area_um2`, downward, because that block is real netlist today and will not be elaborated at all after this change. `equiv_mismatches` must stay 0: with `act = NO_ACTIVATION` the activation MuxCase falls through to its default in both configurations, so the mvout arithmetic is bit-identical.

  time:   flat
  energy: flat
  area:   better
  fmax:   flat

Not part of a coupled plan — this move is deliberately orthogonal to T-A and T-B so its area delta is attributable on its own. The pre-registered falsification: if area comes back flat within noise, synthesis was already stripping the Normalizer as unreachable constant logic and dead-datapath removal is closed for the remaining iterations. If area drops, the size of the drop measures the normalization block directly and tells me whether candidate 2 (granule 4 -> 2, +4.6% area for -3% energy) can be paid for out of it in iteration 14.
```

## Design state

Fields the proposer changed (parent -> proposed). The direction rule applies to
every line here: you may move a value part of the way back, never to or past the
parent value.

```
has_normalizations: True -> False
```

Parent (built, simulated, matched the golden reference):

```json
{
  "a_blocks": 1,
  "acc_banks": 2,
  "acc_capacity_kb": 64,
  "acc_latency": 2,
  "b_blocks": 4,
  "dataflow": "WS",
  "dense_mode": false,
  "dma_buswidth": 128,
  "dma_maxbytes": 64,
  "ex_queue_length": 8,
  "gate_enable": true,
  "granule_size": 4,
  "has_normalizations": true,
  "k_chunk": 16,
  "ld_queue_length": 8,
  "max_in_flight_mem_reqs": 64,
  "meshColumns": 16,
  "meshRows": 16,
  "mvin_scale_shared": false,
  "num_counter": 8,
  "reservation_station_entries_ex": 16,
  "reservation_station_entries_ld": 8,
  "reservation_station_entries_st": 4,
  "sp_banks": 4,
  "sp_capacity_kb": 64,
  "spad_read_delay": 4,
  "st_queue_length": 2,
  "tileColumns": 1,
  "tileRows": 1,
  "tlb_size": 4,
  "workload": "dnn512",
  "x_resident": true,
  "zbu_enable": true,
  "zbu_operand": "A"
}
```

The failing tree, only where it differs from the parent (the proposer's move,
plus anything an earlier repair attempt changed; `{}` means the difference is
entirely in the RTL source):

```json
{
  "has_normalizations": false
}
```

Files the proposer changed:

```
generators/gemmini/src/main/scala/gemmini/PE.scala
generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala
generators/gemmini/src/main/scala/gemmini/SparseCraftSparsity.scala
```

## Earlier repair attempts this iteration

None. This is the first repair attempt for this iteration.

## Your writable set

Every command runs with cwd `/home/ray/chipyard`. These three files, nothing else:

```
/home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala
/home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/PE.scala
/home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/SparseCraftSparsity.scala
```

Verify every write with
`git -C /home/ray/chipyard/generators/gemmini status --short -- /home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala /home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/PE.scala /home/ray/chipyard/generators/gemmini/src/main/scala/gemmini/SparseCraftSparsity.scala`,
and if you touched Chisel, compile before you end your turn:
`cd /home/ray/chipyard && source env.sh && sbt -batch "project gemmini" compile 2>&1 | tail -40`.

One tool call per turn. End with the sections the system prompt requires,
`### ==REPAIR==` last.


<!-- transcript -->
## Root cause

The mutation is the design field `has_normalizations`, moved `true -> false` (`SparseCraftParams.scala:205`, with the marker/comment block starting at line 67). The violated rule is `fusion.no_materialised_S`, whose condition in the T0 table is stated as an invariant, not an inequality: **`has_normalizations` stays `true`**. There is no coupled field in the rule — it names exactly one field and one required value — and the mutated field is boolean, so the direction rule's escape hatch ("move `f` to a legal value *strictly between* `p` and `q`") has an empty candidate set: the only legal value is the parent value. Raising or lowering `sp_capacity_kb`, `acc_capacity_kb`, the array dimensions, `k_chunk`, `b_blocks` or any ZBU/gating lever cannot satisfy a rule that does not reference them; the proposer's mechanism (deleting the `Normalizer` reduction tree, stat contexts and reciprocal units to recover area) *is* precisely the thing the rule forbids, because without hardware `NormCmd.MAX/SUM_EXP/INV_SUM_EXP` the attention score matrix `S` would have to be materialised. So every edit that makes T0 pass is, by construction, either the exact revert `has_normalizations = true` or a disabling of the stated mechanism — both of which are hard prohibitions in my instructions and are detected automatically by comparing parent/proposed/repaired states. I am therefore not editing the tree. Confidence: 5/5 — the rule is a single-field frozen invariant on the one field the proposer moved, and the field is boolean, so the legal-intermediate set is provably empty.

## Fix

- None. No file was edited. The only available repair is the prohibited revert of `SparseCraftParams.scala:205` back to `has_normalizations = true`; the lever I would have needed is a T0 rule that permits `has_normalizations = false` when the workload issues no `CONFIG_NORM` / softmax instruction (which `dnn512` does not), or a separate parameter that strips the `Normalizer`'s datapath while keeping the `NormCmd` capability flag set. Neither exists in the writable set.

## Verification

- `git -C generators/gemmini status --short` confirms `SparseCraftParams.scala` carries the proposer's state and that I added no new modifications; the tracked-file set (`CounterFile`, `ExecuteController`, `PE`, `Scratchpad`) is unchanged from what I was handed, i.e. harness hooks plus the proposer's T-A region.
- `grep` confirms the violating line is `has_normalizations = false,` at `SparseCraftParams.scala:205`, and that all ten `// SPARSECRAFT` markers are present and well formed (`workload = dnn512`, `dense_mode = 0` frozen, `gate_enable = 1`, `zbu_enable = 1`, `granule_size = 4`, `zbu_operand = A`, `k_chunk = 16`, `b_blocks = 4`, `x_resident = 1`), so T0 reports exactly one violation and it is the `fusion.no_materialised_S` invariant.
- Rule arithmetic: `fusion.no_materialised_S` has no numeric term to rebalance — required value `true`, proposed value `false`, parent value `true`. Legal-values-strictly-between = ∅.
- No Chisel was touched, so no compile was run (`NOT_RUN`); running one would have told me nothing about a parsed-state legality rule.

## Self-audit

- Did you change any path outside the three writable files?          no
- Did you move a mutated field back to (or past) its parent value?    no
- Did you disable, bypass or delete the proposer's mechanism?         no
- Did you weaken an assert/require, or touch workload/dense_mode?     no
- Is the proposer's stated mechanism still intact and exercised?      yes (intact in the tree; it cannot be made legal)

### ==REPAIR==
status:     NOT_ACTIONABLE
confidence: 5
class:      t0
files:      none
compiled:   NOT_RUN
preserved:  yes