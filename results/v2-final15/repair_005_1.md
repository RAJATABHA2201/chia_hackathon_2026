<!-- work order -->
# Repair: iteration 5, attempt 1 of 3

A gate failed on the proposer's mutation.

| | |
|---|---|
| verdict | `EQUIV_FAILED` |
| failure class | **divergence** |
| playbook section | **EQUIV_FAILED** in "Failure playbook" |
| re-checked after your turn | elaboration if the hardware changed, then a 16-minute simulation and the golden comparison |

Functional mismatch against the golden reference. SHRINK FIRST: a first-mismatch index beats 32,674 raw mismatches.

## Evidence

```
Minimised failing case: 32,674 mismatching outputs. FIRST at [0,0]: got 339, want 190.
```

## The proposer's stated mechanism

This is what the change was supposed to do. Preserve it.

```
### ==MUTATION==
technique: CONFIG
files:     generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala
change:    L2 geometry: meshRows/meshColumns 16 -> 32 (tileRows/tileColumns stay 1), giving a 32x32 = 1024-PE square WS mesh in place of 256 PEs.
           ZBU granule_size 16 -> 32 so the granule keeps tracking the scratchpad row width, which is what the SyncReadMem row read-enable can actually consume.
           Scratchpad (256 KB), accumulator (64 KB), bank counts and all DMA/queue parameters held fixed, so the measured area delta is attributable to the mesh alone.
compiled:  PASS

### ==PREDICTION==
The mesh retires `DIM³` MACs per `2·DIM`-cycle preload+compute pair, so quadrupling the PEs cuts the cycle count for the same `macs_issued = 8,388,608` by up to 4x with no change to the operand stream; `exe_active` should stay high while `cycles` drops sharply. The bound is DMA: 327,680 off-chip bytes over ~20,000 cycles needs ~16 B/cycle against a 128-bit bus, so I expect the realised win to land between 2x and 4x, and the next diagnosis to flip from compute bound to memory bound — which is the informative outcome and points the following iteration at `dma_buswidth`. `MAC_GATED_TOTAL/macs_issued` should stay at 93.75% (T-A is per-PE and geometry-invariant), and the ZBU bitmap halves in entries.

  time:   better
  energy: flat
  area:   worse
  fmax:   flat
```

## Design state

Fields the proposer changed (parent -> proposed). The direction rule applies to
every line here: you may move a value part of the way back, never to or past the
parent value.

```
granule_size: 16 -> 32
meshColumns: 16 -> 32
meshRows: 16 -> 32
```

Parent (built, simulated, matched the golden reference):

```json
{
  "a_blocks": 1,
  "acc_banks": 2,
  "acc_capacity_kb": 64,
  "acc_latency": 2,
  "b_blocks": 0,
  "dataflow": "WS",
  "dense_mode": false,
  "dma_buswidth": 128,
  "dma_maxbytes": 64,
  "ex_queue_length": 8,
  "gate_enable": true,
  "granule_size": 16,
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
  "sp_capacity_kb": 256,
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
  "meshRows": 32,
  "meshColumns": 32,
  "granule_size": 32
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
I traced the failure to its root without editing anything; here is the analysis.

**What I read**

1. `SparseCraftParams.scala` — the mutation is `meshRows/meshColumns 16→32`, `granule_size 16→32`, everything else held.
2. `gemmini_params.h` (emitted by elaboration) — `#define DIM 32`, `BANK_ROWS 2048`, `MAX_BLOCK_LEN 2`.
3. The actual kernel, `/tmp/sparsecraft_sw_f6342c9ddfeb3619/spmm.c`, and its data header `spmm_data.h`.

**The decisive fact**

`spmm_data.h` is `GENERATED by workload/prep_matrices.py` and carries `"dim": 16` in its provenance line, hard-coded:

```
#define SPMM_N   64
#define SPMM_DIM 16      <-- block size the A matrix was materialised at
#define SPMM_MB  32
#define SPMM_KB  32
```

`spmm.c` does `#define DIM_ SPMM_DIM` and then, inside `row_mac()`, mixes `DIM_` (= 16, the DRAM-side block geometry) with `DIM` (= the *hardware* dim from `gemmini_params.h`) in the same expressions:

```c
#define X_SLICE_ROWS ((SPMM_N / DIM_) * DIM)
gemmini_extended_mvin(Ablk[done+k], A_SP_BASE + k*DIM, DIM, DIM);   // stride was DIM_
gemmini_extended_mvin(Xr + j*DIM_,  ..., blocks*DIM, DIM);
gemmini_extended_preload(b_addr, out, DIM, DIM, DIM, DIM);
```

This is only self-consistent when `DIM == DIM_`. With `DIM=32` and `DIM_=16`, the A mvin reads a 32×32 tile with a 16-byte row stride out of a 256-byte 16×16 block — it swallows four unrelated blocks and presents them as one A tile; the X slices, addressed at `&spmm_X[bj*DIM_][0]` but moved in 32 rows at a time, overlap by 2×. Every product is formed from the wrong operands from the very first block, which is exactly the observed signature: `SPMM_M*SPMM_N = 512*64 = 32,768` outputs, **32,674** wrong starting at `[0,0]` (the ~94 "correct" ones are coincidental zeros). This is verbatim the canonical case the playbook names: *"`DIM` moved 16 to 32 with the tiling still assuming 16, and 32,674 outputs mismatched."*

**Why it cannot be repaired from the writable set**

- `DIM = meshRows * tileRows`. The proposer's mechanism *is* `DIM = 32` (1024 PEs). There is no way to get 1024 PEs with `DIM = 16`.
- The direction rule permits moving `meshRows` to a legal value strictly between 16 and 32; `gemmini.pow2_dim` admits none, and in any case every `DIM ≠ 16` breaks the kernel identically, because the incompatibility is equality with `SPMM_DIM`, not magnitude.
- `SPMM_DIM` is fixed by `workload/prep_matrices.py` for `workload = dnn512`, and `workload` is a frozen `// SPARSECRAFT` marker. The kernel is explicitly not writable, and the `k_chunk`/`b_blocks`/`x_resident` markers cannot reconcile `DIM_` with `DIM`.
- Nothing in `PE.scala` or `SparseCraftSparsity.scala` can re-block a 16×16 DRAM-side A tile into a 32×32 mesh tile; the mvin address generation that mis-reads it lives in harness-owned RTL and in the kernel.

`granule_size = 32` is not implicated — the ZBU module never reads the marker; it is sized by the `(n, w)` the Scratchpad passes. Repairing it alone would leave `DIM = 32` and the same 32,674 mismatches.

I made no edit: any edit that passes this gate would have to put `DIM` back to 16, which is the parent value and a revert.

## Root cause
The kernel `spmm.c` is not DIM-generic: it defines `DIM_ = SPMM_DIM` (the block size at which `prep_matrices.py` materialised the sparse A for `workload = dnn512`, hard-coded to **16** in `spmm_data.h:10`) and then uses `DIM_` for DRAM pointer arithmetic and strides while using the elaborated hardware `DIM` (from `gemmini_params.h:9`, now **32**) for scratchpad addresses and for every `gemmini_extended_mvin` / `gemmini_extended_preload` row and column count — e.g. `gemmini_extended_mvin(Ablk[done+k], A_SP_BASE + k*DIM, DIM, DIM)` after `gemmini_extended_config_ld(DIM_*sizeof(elem_t), ...)`, and `#define X_SLICE_ROWS ((SPMM_N/DIM_)*DIM)`. The mutation `meshRows/meshColumns 16→32` makes `DIM = meshRows*tileRows = 32 ≠ SPMM_DIM = 16`, so every A tile is assembled from four adjacent 16×16 blocks read at a 16-byte stride and every X slice is moved in with 2× overlap; the array therefore multiplies the wrong operands from the first block onward, producing `32,674` wrong values out of `SPMM_M*SPMM_N = 32,768`, first at `[0,0]` with `got 339, want 190`. `granule_size 16→32` is not implicated (`SparseCraftSparsity.scala` is sized by the `(n, w)` Scratchpad passes and never reads the marker). Confidence: 5/5 — `SPMM_DIM` is 16 in all five generated software trees in `/tmp`, the kernel's `DIM`/`DIM_` mixing is only correct when they are equal, and the mismatch count matches the output-matrix size exactly.

## Fix
- None. No edit exists that addresses the cause while preserving the mechanism: the mechanism *is* `DIM = 32`, `DIM` is `meshRows*tileRows`, and `gemmini.pow2_dim` admits no legal value strictly between the parent's 16 and the proposed 32. Any change that makes the gate pass must restore `DIM = 16`, which is a revert of the mutated field to its parent value.

## Verification
- Read `/tmp/sparsecraft_sw_f6342c9ddfeb3619/spmm_data.h`: `#define SPMM_DIM 16`, provenance line `"dim": 16`, `"M": 512, "N": 64` → 32,768 outputs, against 32,674 reported mismatches.
- Read `generators/gemmini/software/gemmini-rocc-tests/include/gemmini_params.h`: `#define DIM 32` — elaboration did emit the new geometry, so the hardware built as intended; the divergence is the software/hardware blocking mismatch, not a Chisel bug.
- Read `spmm.c` `row_mac()`: `DIM_` and `DIM` are mixed in `X_SLICE_ROWS`, the A mvin (`config_ld` stride `DIM_`, mvin extent `DIM`) and the X mvin (`Xr + j*DIM_`, extent `blocks*DIM`). Correct only for `DIM == DIM_`.
- Working tree left untouched: `git -C generators/gemmini status --short` shows only the pre-existing harness-owned modifications and the three untracked SparseCraft files, unchanged by me. No sbt compile run because no Chisel was edited.
- Lever I would have needed: the workload block dimension (`dim` in `workload/prep_matrices.py`, i.e. a regenerated `spmm_data.h` with `SPMM_DIM 32`) or a DIM-generic kernel. Both are frozen and outside the writable set.

## Self-audit
- Did you change any path outside the three writable files?          no
- Did you move a mutated field back to (or past) its parent value?    no
- Did you disable, bypass or delete the proposer's mechanism?         no
- Did you weaken an assert/require, or touch workload/dense_mode?     no
- Is the proposer's stated mechanism still intact and exercised?      yes

### ==REPAIR==
status:     NOT_ACTIONABLE
confidence: 5
class:      EQUIV_FAILED
files:      none
compiled:   NOT_RUN
preserved:  yes