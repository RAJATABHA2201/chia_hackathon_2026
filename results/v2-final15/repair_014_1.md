<!-- work order -->
# Repair: iteration 14, attempt 1 of 3

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
  - zbu.bitmap_budget: bitmap 32768 bits exceeds 5% of scratchpad (64KB) -- granule too fine
```

## The proposer's stated mechanism

This is what the change was supposed to do. Preserve it.

```
### ==MUTATION==
technique: T-B
files:     generators/gemmini/src/main/scala/gemmini/SparseCraftSparsity.scala
           generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala
change:    One coupled T-B redesign (plan_id ZBU-STORE-2), both halves in this iteration.
           (1) `granule_size = 4 -> 2` marker: nGran goes 4 -> 8 bits per row, granBits 32 -> 16, so the write-side detector becomes eight 16-bit OR trees and the read-side reduction an 8-input AND.
           (2) Bitmap storage replaced: the resettable `RegInit(VecInit(...))` flop array becomes an unreset async-read `Mem(n, UInt(nGran.W))`, plus an `init_ctr` sweep that writes zero into every entry after reset, stalls (does not advance) whenever a real write claims the port, and an `init_done` term ANDed into `io.skip` so nothing can be skipped on an uninitialised entry.
compiled:  PASS

### ==PREDICTION==
The harness's T-B credit is derived from the workload's all-zero granules, so halving the granule raises captured zero mass 0.772 -> 0.879 (+10.7 pp) and removes a further ~10% of credited A-side scratchpad reads; at the measured 0.28–0.30 %-energy per captured point that is about -3% on `energy_pj`, entirely in the SRAM term. `cycles` is flat by construction — `sc_skip` gates only `mem.read(raddr, ren && !sc_skip)` and the surrounding `ren`/`enq.valid`/`req.ready` are stock — and `macs_issued` / `MAC_GATED_TOTAL` are untouched. On area the granule step alone is modelled at +4.6% from the two calibrated doublings (16->8: +1.2%, 8->4: +2.3% for 8,192 flops); the storage half should refund part of that by dropping the reset pin and reset buffer tree from 32,768 bits, so I predict a *net* area increase smaller than +4.6% — and the residual against +4.6% is the attributable measurement of the storage move. Correctness is unchanged: a narrower granule can only make the skip predicate stricter, and `init_done` plus `hazard` make an uninitialised or in-flight entry unskippable.

  time:   flat
  energy: better
  area:   worse
  fmax:   flat

plan_id ZBU-STORE-2 — this is a single iteration containing both steps, not a two-step plan; neither half is expected to regress on its own axis.
```

## Design state

Fields the proposer changed (parent -> proposed). The direction rule applies to
every line here: you may move a value part of the way back, never to or past the
parent value.

```
granule_size: 4 -> 2
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
  "granule_size": 2
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

The `zbu.bitmap_budget` rule is **scale-invariant in every lever I have**, so `granule_size = 2` is unconditionally illegal at INT8, not illegal merely for this capacity. Working the rule's own arithmetic:

- `sp_rows = sp_capacity_kb*8192 / (dim*8)` = `64*8192/(16*8)` = **4096** rows
- bitmap bits = `sp_rows * (dim/granule_size)` = `4096 * (16/2)` = **32768** bits (exactly the number the harness reported)
- budget = 5% of `sp_capacity_kb*8192` = `0.05 * 524288` = **26214.4** bits → violated by **1.25x**

Substituting the first line into the second, the bitmap is `sp_capacity_kb*8192/(dim*8) * dim/granule_size = sp_capacity_kb*1024/granule_size` bits, while the budget is `sp_capacity_kb*409.6` bits. **Both `sp_capacity_kb` and `dim` cancel**, so the rule reduces to `granule_size >= 1024/409.6 = 2.5`, independent of scratchpad capacity, banking and array size. Raising `sp_capacity_kb` (the obvious coupled field) raises the bitmap by exactly the same factor as the budget and changes nothing; raising `dim` trades rows for bits-per-row one-for-one and also changes nothing; `sp_banks`, `zbu_operand`, `k_chunk`, `b_blocks` do not appear in the relation. The only remaining lever is `granule_size` itself, and `zbu.granule_divides_dim` plus `gemmini.pow2_dim` restrict it to the divisors `{1,2,4,8,16}` of `dim=16`; the nearest legal value satisfying `>= 2.5` is **4**, which is precisely the parent value. There is no legal value strictly between the proposed `2` and the parent `4` (3 does not divide 16, and no power-of-two `dim` makes it divide). Confidence: 5/5 — the relation is exact, the reported 32768 bits reproduces from the rule verbatim, and the capacity/dim cancellation is algebraic rather than numerical.

## Fix

- None. Every edit that makes `zbu.bitmap_budget` pass requires setting `granule_size` back to `4` (the parent value) or coarser, which is the prohibited revert, or shrinking/disabling the ZBU, which is the prohibited mechanism removal. The second, storage half of the proposer's coupled change (`Mem`-based bitmap + `init_ctr` sweep + `init_done` term) is legal at any granule and is untouched in `SparseCraftSparsity.scala`, but it cannot be measured under this work order without carrying the illegal granule with it.

**Lever I would have needed:** either a `granule_size` of 3 (illegal under `zbu.granule_divides_dim`), or a bitmap budget that is not a fixed fraction of the same capacity that defines `sp_rows` — e.g. a budget expressed per row, or an entry width other than 8 bits per scratchpad element so the denominator grows without the numerator. Element width is frozen at INT8 by the harness, so neither is reachable from the three writable files.

## Verification

- Rule arithmetic re-derived by hand from the T0 table: bitmap `= sp_capacity_kb*1024/granule_size` bits vs budget `= sp_capacity_kb*409.6` bits ⇒ `granule_size >= 2.5` for all capacities and all `dim`. At `granule_size=2` the ratio is exactly **1.25x** over budget, matching the reported 32768 vs 26214.4 bits.
- Read the markers in `SparseCraftParams.scala`: `// SPARSECRAFT granule_size = 2` at line 97, `zbu_enable = 1` at 68, `gate_enable = 1` at 62, `workload`/`dense_mode` at 7–8. All markers well formed; the state parses as reported, so this is a genuine legality violation and not a malformed-marker artefact.
- No file was written, so the tree is exactly as the proposer left it (their `compiled: PASS` stands). No sbt compile was run because no Chisel was edited.

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