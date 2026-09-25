### S-1 — Dataflow and tiling (the software schedule)

These are **compiler defines**, not Chisel. You change them by editing the
`// SPARSECRAFT` markers in `SparseCraftParams.scala`; the harness turns them into
`-DSPMM_*` flags and rebuilds only the kernel. **A software-only move leaves
`hw_hash` unchanged, so elaboration and synthesis are cache hits and the iteration
costs roughly a third of a hardware one.** When two candidates look equally
promising and one is software-only, propose that one first — you learn the same
amount for a third of the wall clock.

| lever | define | what it moves |
|---|---|---|
| `k_chunk` | `-DSPMM_KCHUNK` | K-blocks accumulated per accumulator-resident pass. Bounded by scratchpad capacity: a pass stages `k_chunk*dim` rows of A plus `k_chunk*(N/dim)*dim` of B |
| `x_resident` | `-DSPMM_XRES` | hold all of X in the scratchpad for the whole matmul, eliminating its re-fetch |
| `b_blocks` | `-DSPMM_B_BLOCKS` | B mvin width in DIM-column tiles (0 = auto). Bounded by `dma_maxbytes` |
| `a_blocks` | `-DSPMM_A_BLOCKS` | A mvin blocking |
| `dense_mode` | `-DSPMM_DENSE` | walk every block including zeros. A **baseline control**, not an optimisation |

**The one that has already paid.** `x_resident` cut off-chip traffic 9.8x
(3,211,264 → 327,680 B) and cycles 1.34x for **zero area**, because the energy model
prices a DRAM byte at 22 pJ against a MAC's 0.30 pJ — a 73x ratio. Any move that
removes a fetch beats almost any move that removes arithmetic. Check
`bytes_offchip` before you touch anything on the compute side.

**Read the breakdown before choosing.** `mac/sram/dram` in the feedback tells you
which term you are allowed to move:

- `dram` dominant → attack the fetch: `x_resident`, larger `k_chunk`, `b_blocks`
- `sram` dominant → the scratchpad is now the cost. Reducing *capacity* can help:
  the energy model scales per-access cost as `sqrt(max(SRAM, 32KB) / 256KB)` over
  the total on-chip SRAM (scratchpad + accumulator), so a smaller SRAM is cheaper
  per access down to a 32 KB floor (0.35x the 256 KB cost). If the working set
  already fits, the surplus is pure cost.
- `mac` dominant → you are in T-A/N:M territory, and on this workload `mac` is 1-3%
  of the budget, so the ceiling there is small

**The coupling that makes this co-design.** Elaboration emits `gemmini_params.h`
carrying `DIM`, scratchpad capacity and accumulator size. The kernel **cannot be
built before the hardware is elaborated**, and a hardware change invalidates every
previously built kernel. That is why changing `meshRows`/`meshColumns` without
changing the tiling produces a design that compiles, elaborates, simulates — and
returns **wrong answers**. It has happened: `DIM` moved 16 → 32 while the kernel's
tiling still assumed 16, and the result was 32,674 mismatching outputs. If you
change `DIM`, you must change the schedule with it, in the same iteration.

**A lever is not spent until it is measured.** `k_chunk` and `b_blocks` sit at their
defaults far longer than the hardware knobs do, because the hardware knobs are more
visible in the design state. Both are bounded by capacity, both are free to try, and
neither has been swept.
