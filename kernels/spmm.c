// T2a measurement instrument: block-sparse SpMM (Y = A x X) on Gemmini.
//
// A is a real SuiteSparse sparsity pattern, INT8-valued, held as dense DIM x DIM
// blocks with only the nonzero blocks materialised. X is dense INT8. Y is INT32.
//
// This file is the measurement instrument, so everything the harness scores has
// to be printed here in the exact "SPARSECRAFT <name>=<int>" form metrics.py
// parses. Three rules inherited from the attention kernel, all of them earned:
//
//  * 64-bit values use %lu / (unsigned long), NEVER %llu. This build links
//    newlib-NANO (-specs=htif_nano.specs), whose printf has no long-long
//    conversion: "%llu" emits the literal text "lu" and the metric parses as
//    nothing -- so the iteration dies in metrics.parse AFTER paying for a full
//    elaborate and simulate.
//  * Gemmini exposes exactly EIGHT counter slots (counter_read masks with 0x7),
//    and a slot only counts from the moment it is configured, so all eight are
//    armed BEFORE the timed region.
//  * The equivalence verdict is PRINTED, never asserted. The verdict belongs to
//    the harness (N41), never to code inside the agent's reach.
//
// TWO BUILD MODES, one flag, so the baselines are the same instrument:
//   -DSPMM_DENSE=0  (default)  walk only the nonzero blocks -- software block
//                              sparsity. This is B1/B2.
//   -DSPMM_DENSE=1             walk every block, zeros included -- a dense GEMM
//                              that ignores sparsity entirely. This is B0, the
//                              vanilla-Gemmini denominator.
// Same data, same counters, same equivalence check; only the block walk differs.
// Y is bit-identical between the two modes, which is itself a self-test.

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

#include "include/gemmini_testutils.h"
#include "spmm_data.h"

#ifndef SPMM_DENSE
#define SPMM_DENSE 0
#endif

#define DIM_ SPMM_DIM

// Y must be acc_t (INT32), not elem_t. The golden reference ranges well past
// INT8, so a narrowed accumulator read would clip and the equivalence gate
// would fail a CORRECT design. This is why the config must carry
// acc_read_full_width = true and why full_C is true below.
static acc_t Y[SPMM_M][SPMM_N] row_align_acc(1);

// A zero DIM x DIM block, used only by the dense mode to feed the structural
// zeros the sparse mode skips.
static elem_t Zero[SPMM_DIM][SPMM_DIM] row_align(1);

// (bi,bj) -> index into spmm_A, or -1 for a structurally zero block.
// Built ONCE, BEFORE the timer starts. The first version of this kernel did a
// linear scan over all SPMM_NZB blocks for each of MB*KB positions inside the
// timed region -- 524,288 CPU iterations at M=512 -- which inflated the B0
// baseline with search time and would have manufactured a speedup that was not
// real. B0 is the denominator every later claim divides by, so CPU work must
// never land inside the measured window.
static int32_t blk_idx[SPMM_MB][SPMM_KB];

// Eight slots, and all eight are spoken for (counter_read masks with 0x7).
// DMA_TLB_MISS_CYCLE was dropped for MAC_GATED_TOTAL: it was the least
// load-bearing of the eight, while MAC_GATED_TOTAL is what turns the T-A
// energy claim from modelled into measured. RDMA/WDMA bytes are NOT
// negotiable -- N53 charges DRAM energy off them, and that is ~85% of total.
//
// RESERVATION_STATION_FULL_CYCLES has now been dropped for ZBU_SKIPPED_ROWS,
// for the same reason and by the same argument: it was the least load-bearing
// of the remaining eight (diagnose() notes it is a free-running accumulation
// that routinely exceeds the cycle count, so an absolute threshold on it is
// meaningless), while ZBU_SKIPPED_ROWS is what turns the T-B energy claim from
// unmeasurable into measured. Without it e_sram is derived from macs_issued,
// which a suppressed read does not change, so the ZBU scores as pure area cost.
enum {
  C_EXE_ACTIVE = 0, C_LOAD_DMA_WAIT, C_SPAD_A_WAIT, C_SPAD_B_WAIT,
  C_ZBU_SKIPPED, C_MAC_GATED, C_RDMA_BYTES, C_WDMA_BYTES
};

// ACCUMULATOR-RESIDENT PATH (default). See plan Sec 9j.
//
// The original path called tiled_matmul_auto once per NONZERO BLOCK, passing Y
// as both D (bias in) and C (out). Every block after the first in a block row
// therefore dragged the 2 KB accumulator tile out to DRAM and back. Measured:
// Y round-trip traffic outweighed A traffic 14:1, and DRAM is 91.4% of energy,
// so the array-side sparsity techniques were aimed at ~11% of reads.
//
// Here each block ROW is one pass: mvin its A blocks and the matching X row
// slices, accumulate across them INSIDE the array, and mvout Y exactly once.
// The X slices are scattered in DRAM but each is 16 contiguous rows at stride
// SPMM_N, which gemmini_extended_mvin expresses directly -- so no gather and
// no preprocessing, which matters because X is the ACTIVATION matrix and a
// prep-time gather of it would not be legitimate for a real deployment.
//
// -DSPMM_ACCRES=0 restores the per-block path, so the kernel change itself can
// be A/B'd against the same instrument.
#ifndef SPMM_ACCRES
#define SPMM_ACCRES 1
#endif

// K-blocks issued per pass. Scratchpad is BANK_NUM*BANK_ROWS rows of DIM bytes
// (16,384 rows here); one pass needs K*DIM rows for A and K*J*DIM for B, so 16
// is far inside the budget. Longer block rows are chunked, and only the FIRST
// chunk overwrites the accumulator.
// Overridable from the design state via -D (SW schedule levers). Defaults
// reproduce the hand-tuned schedule exactly, so an unset knob is a no-op.
#ifndef SPMM_KCHUNK
#define SPMM_KCHUNK 16
#endif

// Output-column tiles per B mvin. MAX_BLOCK_LEN = MAX_BYTES/DIM (= 4 here) is
// the most gemmini will move in one instruction.
#ifndef SPMM_B_BLOCKS
#define SPMM_B_BLOCKS 0           /* 0 = auto: as wide as gemmini allows */
#endif
#if SPMM_B_BLOCKS > 0
#define B_BLOCKS SPMM_B_BLOCKS
#else
#define B_BLOCKS ((SPMM_N / SPMM_DIM) < MAX_BLOCK_LEN ? (SPMM_N / SPMM_DIM) : MAX_BLOCK_LEN)
#endif

// A mvin width, in DIM-column tiles. Only legal when the blocks being batched
// are CONTIGUOUS in DRAM: prep_matrices.py emits spmm_A in block-row-major
// order, so consecutive entries of a row ARE adjacent, but a batched mvin
// reads them as one (DIM x blocks*DIM) matrix with row stride DIM -- which is
// NOT the [b][r][c] layout. So batching A is only correct for a_blocks == 1
// until the layout changes; values > 1 are rejected by T0 rather than
// silently producing wrong data.
#ifndef SPMM_A_BLOCKS
#define SPMM_A_BLOCKS 1
#endif

// The per-row plan, built BEFORE the timer (same discipline as blk_idx: no CPU
// search inside the measured window).
static const elem_t *row_A[SPMM_MB][SPMM_KB];
static int32_t       row_C[SPMM_MB][SPMM_KB];
static int32_t       row_N[SPMM_MB];

// One Gemmini tile matmul: Y[bi] += Ablk x X[bj].
// `first` selects whether this is the opening product for the output block row
// (no bias) or an accumulation onto what is already there (bias = Y itself).
static inline void block_mac(const elem_t *Ablk, int bi, int bj, int first) {
  tiled_matmul_auto(
      /* dim_I */ DIM_, /* dim_J */ SPMM_N, /* dim_K */ DIM_,
      /* A */ (elem_t *)Ablk,
      /* B */ (elem_t *)&spmm_X[bj * DIM_][0],
      /* D */ first ? NULL : (void *)&Y[bi * DIM_][0],
      /* C */ (void *)&Y[bi * DIM_][0],
      /* strides A,B,D,C */ DIM_, SPMM_N, SPMM_N, SPMM_N,
      MVIN_SCALE_IDENTITY, MVIN_SCALE_IDENTITY, MVIN_SCALE_IDENTITY,
      NO_ACTIVATION, ACC_SCALE_IDENTITY, ACC_SCALE_IDENTITY,
      /* repeating_bias */ false,
      /* transpose_A, transpose_B */ false, false,
      /* full_C */ true, /* low_D */ false,
      0, WS);
}

#if SPMM_ACCRES
// One accumulator-resident pass over block row `bi`.
//   Y[bi] = sum_k  Ablk[k] x X[bcol[k]]
// Mirrors sp_tiled_matmul_ws (gemmini.h:404) with I=1, J=SPMM_N/DIM, K=nb, and
// B moved in from scattered block rows. no_bias throughout: the whole sum is
// formed in the array, so the accumulator is written once and Y reaches DRAM
// once.
static void row_mac(int bi, const elem_t *const *Ablk,
                    const int32_t *bcol, int nb) {
  const size_t J = SPMM_N / DIM_;
  // (3 << ADDR_LEN-2) sets both the accumulator bit and the ACCUMULATE bit;
  // (1 << ADDR_LEN-3) is full_C. Clearing ADDR_LEN-2 turns accumulate into
  // overwrite -- gemmini.h:484 does exactly this for the no-bias case.
  const uint32_t C_sp = (3u << (ADDR_LEN - 2)) | (1u << (ADDR_LEN - 3));

  int done = 0, chunk = 0;
  while (done < nb) {
    size_t K = (size_t)(nb - done);
    if (K > SPMM_KCHUNK) K = SPMM_KCHUNK;

    const uint32_t A_sp = 0;
    const uint32_t B_sp = BANK_NUM * BANK_ROWS - K * J * DIM;

    // B: the X row slices. Stride is SPMM_N -- the slice is 16 contiguous rows
    // of the dense X, read in place at its true address.
    //
    // Moved in B_BLOCKS output-column tiles at a time, as gemmini's own
    // sp_tiled_matmul does. One mvin of J*DIM columns instead of J mvins of
    // DIM: with MAX_BYTES=64 the DMA granule is 64 B, so J separate 16-byte
    // row reads each cost a full granule and waste 4x. Measured reads were
    // 6.5x the analytic model; this is the one contributor that was clearly
    // self-inflicted.
    gemmini_extended_config_ld(SPMM_N * sizeof(elem_t), MVIN_SCALE_IDENTITY);
    for (size_t k = 0; k < K; k++) {
      const elem_t *Xr = &spmm_X[bcol[done + k] * DIM_][0];
      for (size_t j = 0; j < J; j += B_BLOCKS) {
        const size_t blocks = (j + B_BLOCKS <= J) ? (size_t)B_BLOCKS : J - j;
        gemmini_extended_mvin(Xr + j * DIM_, B_sp + (k * J + j) * DIM,
                              blocks * DIM, DIM);
      }
    }

    // A: each block is a dense DIM x DIM tile, stride DIM_.
    gemmini_extended_config_ld(DIM_ * sizeof(elem_t), MVIN_SCALE_IDENTITY);
    for (size_t k = 0; k < K; k++) {
      gemmini_extended_mvin(Ablk[done + k], A_sp + k * DIM, DIM, DIM);
    }

    // WEIGHT-STATIONARY accumulation. Mirrors sp_tiled_matmul_ws
    // (gemmini.h:526), NOT the output-stationary variant at :404 -- in WS the
    // partial sums accumulate IN THE ACCUMULATOR across k, so B is preloaded
    // as the weights and compute takes GARBAGE as its second operand. The
    // accumulate bit is cleared at k == 0 (first contribution), not at k-last.
    // Getting this backwards is what produced 4,068 golden mismatches on the
    // first attempt; I=1 here, so gemmini's i-indexed weight reuse collapses
    // away and every (k,j) preloads its own B tile.
    for (size_t k = 0; k < K; k++) {
      for (size_t j = 0; j < J; j++) {
        uint32_t out = C_sp + j * DIM;
        if (k == 0 && chunk == 0)
          out &= ~(1u << (ADDR_LEN - 2));      // first contribution overwrites
        gemmini_extended_preload(B_sp + (k * J + j) * DIM, out,
                                 DIM, DIM, DIM, DIM);
        gemmini_extended_compute_preloaded(A_sp + k * DIM, GARBAGE_ADDR,
                                           DIM, DIM, DIM, DIM);
      }
    }
    done += (int)K;
    chunk++;
  }

  // Y leaves the accumulator exactly once for this block row.
  gemmini_extended_config_st(SPMM_N * sizeof(acc_t), NO_ACTIVATION,
                             ACC_SCALE_IDENTITY);
  for (size_t j = 0; j < J; j++) {
    gemmini_extended_mvout((void *)&Y[bi * DIM_][j * DIM_],
                           C_sp + j * DIM, DIM, DIM);
  }
}
#endif

int main(void) {
  gemmini_flush(0);

  for (int i = 0; i < SPMM_M; i++)
    for (int j = 0; j < SPMM_N; j++) Y[i][j] = 0;
  for (int r = 0; r < DIM_; r++)
    for (int c = 0; c < DIM_; c++) Zero[r][c] = 0;

  for (int bi = 0; bi < SPMM_MB; bi++)
    for (int bj = 0; bj < SPMM_KB; bj++) blk_idx[bi][bj] = -1;
  for (int b = 0; b < SPMM_NZB; b++)
    blk_idx[spmm_blk_row[b]][spmm_blk_col[b]] = b;

  // The per-row plan. Built here, OUTSIDE the timed window, for the same
  // reason blk_idx is: a search inside the window would be charged to the
  // hardware and would manufacture a result.
  for (int bi = 0; bi < SPMM_MB; bi++) row_N[bi] = 0;
#if SPMM_DENSE
  // B0: every block of every row, structural zeros included.
  for (int bi = 0; bi < SPMM_MB; bi++)
    for (int bj = 0; bj < SPMM_KB; bj++) {
      const int32_t bidx = blk_idx[bi][bj];
      row_A[bi][row_N[bi]] = (bidx < 0) ? (const elem_t *)Zero
                                        : (const elem_t *)spmm_A[bidx];
      row_C[bi][row_N[bi]] = bj;
      row_N[bi]++;
    }
#else
  // B1/B2: only the nonzero blocks.
  for (int b = 0; b < SPMM_NZB; b++) {
    const int bi = spmm_blk_row[b];
    row_A[bi][row_N[bi]] = (const elem_t *)spmm_A[b];
    row_C[bi][row_N[bi]] = spmm_blk_col[b];
    row_N[bi]++;
  }
#endif

  // The array config is loop-invariant; set it once, outside the window.
  gemmini_extended_config_ex(WS, 0, 0, 1, false, false);

  counter_reset();
  counter_configure(C_EXE_ACTIVE,    EXE_ACTIVE_CYCLE);
  counter_configure(C_LOAD_DMA_WAIT, LOAD_DMA_WAIT_CYCLE);
  counter_configure(C_SPAD_A_WAIT,   SCRATCHPAD_A_WAIT_CYCLE);
  counter_configure(C_SPAD_B_WAIT,   SCRATCHPAD_B_WAIT_CYCLE);
  counter_configure(C_ZBU_SKIPPED,   ZBU_SKIPPED_ROWS);
  counter_configure(C_MAC_GATED,     MAC_GATED_TOTAL);
  counter_configure(C_RDMA_BYTES,    RDMA_BYTES_REC);
  counter_configure(C_WDMA_BYTES,    WDMA_BYTES_SENT);

  uint64_t tiles = 0;
  uint64_t t0 = read_cycles();

#if SPMM_ACCRES
  // One pass per block row. Identical control flow in both modes -- the ONLY
  // difference between B0 and B1 is which blocks row_N/row_A carry, which is
  // what makes the B0 -> B1 ratio a sparsity result and not a kernel-quality
  // one.
  for (int bi = 0; bi < SPMM_MB; bi++) {
    if (row_N[bi] == 0) continue;
    row_mac(bi, row_A[bi], row_C[bi], row_N[bi]);
    tiles += (uint64_t)row_N[bi];
  }
#else
  // Legacy per-block path: Y round-trips to DRAM once per block. Kept so the
  // kernel change itself can be measured against the same instrument.
  for (int bi = 0; bi < SPMM_MB; bi++) {
    int first = 1;
    for (int t = 0; t < row_N[bi]; t++) {
      block_mac(row_A[bi][t], bi, row_C[bi][t], first);
      first = 0;
      tiles++;
    }
  }
#endif

  uint64_t t1 = read_cycles();

  // --- N41 functional equivalence, against a host-computed golden -----------
  // Printed, not asserted. A non-zero mismatch count voids the iteration at the
  // harness, which is the only place that decision belongs.
  uint64_t mismatches = 0;
  int64_t checksum = 0;
  int32_t first_bad_i = -1, first_bad_j = -1;
  int32_t got = 0, want = 0;
  for (int i = 0; i < SPMM_M; i++)
    for (int j = 0; j < SPMM_N; j++) {
      const int32_t y = Y[i][j];
      checksum += (int64_t)y;
      if (y != spmm_Y_golden[i][j]) {
        if (mismatches == 0) {
          first_bad_i = i; first_bad_j = j; got = y; want = spmm_Y_golden[i][j];
        }
        mismatches++;
      }
    }

  printf("SPARSECRAFT cycles=%lu\n", (unsigned long)(t1 - t0));
  // macs_useful counts only products with a nonzero A operand -- the work that
  // must happen. macs_issued counts what a dense array actually performs. Their
  // ratio is the headroom the RTL techniques are competing for, and reporting
  // both is what keeps the energy axis from being a restatement of the kernel.
  printf("SPARSECRAFT macs_useful=%lu\n", (unsigned long)((uint64_t)SPMM_NNZ * SPMM_N));
  printf("SPARSECRAFT macs_issued=%lu\n",
         (unsigned long)(tiles * (uint64_t)DIM_ * DIM_ * SPMM_N));
  printf("SPARSECRAFT tiles_issued=%lu\n", (unsigned long)tiles);
  printf("SPARSECRAFT nz_blocks=%d\n", SPMM_NZB);
  printf("SPARSECRAFT total_blocks=%d\n", SPMM_MB * SPMM_KB);
  printf("SPARSECRAFT dense_mode=%d\n", SPMM_DENSE);
  printf("SPARSECRAFT M=%d\n", SPMM_M);
  printf("SPARSECRAFT K=%d\n", SPMM_K);
  printf("SPARSECRAFT N=%d\n", SPMM_N);
  printf("SPARSECRAFT dim=%d\n", DIM_);
  printf("SPARSECRAFT nnz=%d\n", SPMM_NNZ);
  printf("SPARSECRAFT equiv_mismatches=%lu\n", (unsigned long)mismatches);
  printf("SPARSECRAFT equiv_first_i=%ld\n", (long)first_bad_i);
  printf("SPARSECRAFT equiv_first_j=%ld\n", (long)first_bad_j);
  printf("SPARSECRAFT equiv_got=%ld\n", (long)got);
  printf("SPARSECRAFT equiv_want=%ld\n", (long)want);
  printf("SPARSECRAFT checksum=%ld\n", (long)checksum);

  printf("SPARSECRAFT EXE_ACTIVE_CYCLE=%u\n", counter_read(C_EXE_ACTIVE));
  printf("SPARSECRAFT LOAD_DMA_WAIT_CYCLE=%u\n", counter_read(C_LOAD_DMA_WAIT));
  printf("SPARSECRAFT SCRATCHPAD_A_WAIT_CYCLE=%u\n", counter_read(C_SPAD_A_WAIT));
  printf("SPARSECRAFT SCRATCHPAD_B_WAIT_CYCLE=%u\n", counter_read(C_SPAD_B_WAIT));
  printf("SPARSECRAFT ZBU_SKIPPED_ROWS=%u\n", counter_read(C_ZBU_SKIPPED));
  printf("SPARSECRAFT MAC_GATED_TOTAL=%u\n", counter_read(C_MAC_GATED));
  printf("SPARSECRAFT RDMA_BYTES_REC=%u\n", counter_read(C_RDMA_BYTES));
  printf("SPARSECRAFT WDMA_BYTES_SENT=%u\n", counter_read(C_WDMA_BYTES));

  printf("SPARSECRAFT done=1\n");
  return 0;
}
