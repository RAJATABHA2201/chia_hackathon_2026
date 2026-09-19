// T2a functional slice: block-sparse causal prefill attention on Gemmini.
//
// This kernel is the measurement instrument. RunResult.log is nothing but the
// simulator's HTIF stdout, so anything the harness scores has to be printed
// here in the exact "SPARSECRAFT <name>=<int>" form metrics.py parses.
//
// 64-bit values use %lu / (unsigned long), NEVER %llu. This build links
// newlib-NANO (-specs=htif_nano.specs in nodes.py), whose printf has no
// long-long conversion: "%llu" emits the literal text "lu" and the metric
// parses as nothing, so the iteration dies in metrics.parse AFTER paying
// for a full elaborate and a ~10 min simulate. -u _printf_long_long does
// NOT fix it (verified under spike). On RV64 LP64 a long is 64 bits, so
// %lu costs no range.
//
// Counters come from Gemmini's CounterFile. The hardware exposes exactly
// EIGHT slots (counter_read masks the index with 0x7), and a slot only counts
// from the moment it is configured -- so all eight are armed before the
// workload, not after, and the eight chosen here are the ones the review's
// lever table actually needs to validate a move.

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdbool.h>

#include "include/gemmini_testutils.h"   // pulls in gemmini.h -> params -> counter

#ifndef BLOCK_SIZE
#define BLOCK_SIZE 32
#endif
#ifndef TILE_M
#define TILE_M 16
#endif
#ifndef TILE_N
#define TILE_N 16
#endif
#ifndef TILE_K
#define TILE_K 16
#endif

// --- Sparsity pattern (software lever) -------------------------------------
// Which score blocks are computed at all. Causal masking is applied on top of
// every pattern, so these describe the sparsity WITHIN the causal triangle --
// i.e. they are genuine attention-sparsity techniques, not just the
// lower-triangular structure every decoder has.
//
//   0 CAUSAL          every block with bj <= bi. The dense-causal reference.
//   1 SLIDING_WINDOW  a local band of WINDOW_BLOCKS blocks (Longformer).
//   2 WINDOW_GLOBAL   the local band, plus the first GLOBAL_BLOCKS columns
//                     attended by every row (Longformer / BigBird global
//                     tokens).
//   3 STRIDED         the local band, plus every STRIDE-th block further back
//                     (Sparse Transformer, Child et al.).
//
// The pattern changes which blocks are issued to Gemmini, so it moves cycles,
// MACs and off-chip bytes -- and it couples to the hardware: WINDOW_BLOCKS x
// BLOCK_SIZE is the token span, while BLOCK_SIZE must divide the systolic
// array dimension. A narrow window shrinks the working set, which changes
// which scratchpad size is the right one.
#define P_CAUSAL         0
#define P_SLIDING_WINDOW 1
#define P_WINDOW_GLOBAL  2
#define P_STRIDED        3

#ifndef SPARSITY_PATTERN
#define SPARSITY_PATTERN P_CAUSAL
#endif
#ifndef WINDOW_BLOCKS
#define WINDOW_BLOCKS 4
#endif
#ifndef GLOBAL_BLOCKS
#define GLOBAL_BLOCKS 1
#endif
#ifndef STRIDE_BLOCKS
#define STRIDE_BLOCKS 2
#endif

// Small enough to stay a functional gate, large enough that block sparsity is
// not semantically absent: at B=32 this is 8 blocks per row, not 2.
#define SEQ_LEN  256
#define D_HEAD   64
#define N_BLOCKS (SEQ_LEN / BLOCK_SIZE)

static elem_t Q[SEQ_LEN][D_HEAD] row_align(1);
static elem_t Kmat[SEQ_LEN][D_HEAD] row_align(1);
static elem_t Vmat[SEQ_LEN][D_HEAD] row_align(1);
static elem_t Omat[SEQ_LEN][D_HEAD] row_align(1);
static elem_t Sblk[BLOCK_SIZE][BLOCK_SIZE] row_align(1);

// Block mask: is score block (bi, bj) computed at all?
//
// Causality first -- no pattern may attend the future. Then the pattern
// decides which of the permitted blocks are actually retained. The diagonal
// block is always kept: a row that attends nothing produces a degenerate
// softmax, and a kernel that can produce one is not measuring attention.
static inline bool block_retained(int bi, int bj) {
  if (bj > bi) return false;
  const int back = bi - bj;               // how far into the past
  if (back == 0) return true;             // always attend self

#if   SPARSITY_PATTERN == P_SLIDING_WINDOW
  return back < WINDOW_BLOCKS;
#elif SPARSITY_PATTERN == P_WINDOW_GLOBAL
  return back < WINDOW_BLOCKS || bj < GLOBAL_BLOCKS;
#elif SPARSITY_PATTERN == P_STRIDED
  return back < WINDOW_BLOCKS || (back % STRIDE_BLOCKS) == 0;
#else
  return true;                            // P_CAUSAL
#endif
}

static void fill(void) {
  // Deliberately non-degenerate: no exact zeros, so a kernel that
  // short-circuits cannot match the reference by accident.
  for (int i = 0; i < SEQ_LEN; i++)
    for (int d = 0; d < D_HEAD; d++) {
      Q[i][d]    = (elem_t)(((i * 7 + d * 3) % 13) - 6 + ((i + d) % 2));
      Kmat[i][d] = (elem_t)(((i * 5 + d * 11) % 11) - 5 + ((i * d) % 2));
      Vmat[i][d] = (elem_t)(((i * 3 + d * 17) % 9) - 4 + ((i + 2 * d) % 2));
      Omat[i][d] = 0;
    }
  for (int a = 0; a < BLOCK_SIZE; a++)
    for (int b = 0; b < BLOCK_SIZE; b++) Sblk[a][b] = 0;
}

// The eight counter slots, armed before the workload.
enum {
  C_EXE_ACTIVE = 0, C_LOAD_DMA_WAIT, C_SPAD_A_WAIT, C_SPAD_B_WAIT,
  C_RS_FULL, C_TLB_MISS, C_RDMA_BYTES, C_WDMA_BYTES
};

int main(void) {
  gemmini_flush(0);
  fill();

  uint64_t nnz_blocks = 0;
  for (int bi = 0; bi < N_BLOCKS; bi++)
    for (int bj = 0; bj < N_BLOCKS; bj++)
      if (block_retained(bi, bj)) nnz_blocks++;

  // Arm the counters BEFORE the workload. counter_configure handles the
  // incremental/external split itself (codes above INCREMENTAL_COUNTERS are
  // rebased and flagged non-incremental).
  counter_reset();
  counter_configure(C_EXE_ACTIVE,    EXE_ACTIVE_CYCLE);
  counter_configure(C_LOAD_DMA_WAIT, LOAD_DMA_WAIT_CYCLE);
  counter_configure(C_SPAD_A_WAIT,   SCRATCHPAD_A_WAIT_CYCLE);
  counter_configure(C_SPAD_B_WAIT,   SCRATCHPAD_B_WAIT_CYCLE);
  counter_configure(C_RS_FULL,       RESERVATION_STATION_FULL_CYCLES);
  counter_configure(C_TLB_MISS,      DMA_TLB_MISS_CYCLE);
  counter_configure(C_RDMA_BYTES,    RDMA_BYTES_REC);
  counter_configure(C_WDMA_BYTES,    WDMA_BYTES_SENT);

  uint64_t t0 = read_cycles();

  // One pass over retained blocks: QK^T then PV, accumulating into O.
  // Sblk is scratch for a single B x B block and is never materialised at
  // SEQ_LEN x SEQ_LEN -- the fusion requirement that makes long-context
  // prefill feasible rather than merely faster.
  for (int bi = 0; bi < N_BLOCKS; bi++) {
    for (int bj = 0; bj < N_BLOCKS; bj++) {
      if (!block_retained(bi, bj)) continue;

      // Sblk = Q_block x K_block^T   (transpose_B = true)
      tiled_matmul_auto(BLOCK_SIZE, BLOCK_SIZE, D_HEAD,
                        (elem_t*)&Q[bi * BLOCK_SIZE][0],
                        (elem_t*)&Kmat[bj * BLOCK_SIZE][0],
                        NULL, (elem_t*)&Sblk[0][0],
                        D_HEAD, D_HEAD, BLOCK_SIZE, BLOCK_SIZE,
                        MVIN_SCALE_IDENTITY, MVIN_SCALE_IDENTITY, MVIN_SCALE_IDENTITY,
                        NO_ACTIVATION, ACC_SCALE_IDENTITY, ACC_SCALE_IDENTITY,
                        false,
                        false, true,
                        false, false,
                        0, WS);

      // O_block += Sblk x V_block
      tiled_matmul_auto(BLOCK_SIZE, D_HEAD, BLOCK_SIZE,
                        (elem_t*)&Sblk[0][0],
                        (elem_t*)&Vmat[bj * BLOCK_SIZE][0],
                        NULL, (elem_t*)&Omat[bi * BLOCK_SIZE][0],
                        BLOCK_SIZE, D_HEAD, D_HEAD, D_HEAD,
                        MVIN_SCALE_IDENTITY, MVIN_SCALE_IDENTITY, MVIN_SCALE_IDENTITY,
                        NO_ACTIVATION, ACC_SCALE_IDENTITY, ACC_SCALE_IDENTITY,
                        false,
                        false, false,
                        false, false,
                        0, WS);
    }
  }

  uint64_t t1 = read_cycles();

  // A checksum the harness compares against the dense reference. Printed, not
  // asserted: the equivalence verdict belongs to the harness, never to code
  // the agent could reach.
  int64_t checksum = 0;
  for (int i = 0; i < SEQ_LEN; i++)
    for (int d = 0; d < D_HEAD; d++) checksum += (int64_t)Omat[i][d];

  uint64_t macs = nnz_blocks * (uint64_t)BLOCK_SIZE * BLOCK_SIZE * D_HEAD * 2ull;

  printf("SPARSECRAFT cycles=%lu\n", (unsigned long)(t1 - t0));
  printf("SPARSECRAFT macs_useful=%lu\n", (unsigned long)macs);
  printf("SPARSECRAFT nnz_blocks=%lu\n", (unsigned long)nnz_blocks);
  // Echo the pattern back so the record shows what was actually compiled in,
  // not merely what the design state asked for.
  printf("SPARSECRAFT sparsity_pattern=%d\n", SPARSITY_PATTERN);
  printf("SPARSECRAFT window_blocks=%d\n", WINDOW_BLOCKS);
  printf("SPARSECRAFT global_blocks=%d\n", GLOBAL_BLOCKS);
  printf("SPARSECRAFT stride_blocks=%d\n", STRIDE_BLOCKS);
  printf("SPARSECRAFT seq_len=%d\n", SEQ_LEN);
  printf("SPARSECRAFT block_size=%d\n", BLOCK_SIZE);
  printf("SPARSECRAFT d_head=%d\n", D_HEAD);
  printf("SPARSECRAFT checksum=%ld\n", (long)checksum);

  printf("SPARSECRAFT EXE_ACTIVE_CYCLE=%u\n", counter_read(C_EXE_ACTIVE));
  printf("SPARSECRAFT LOAD_DMA_WAIT_CYCLE=%u\n", counter_read(C_LOAD_DMA_WAIT));
  printf("SPARSECRAFT SCRATCHPAD_A_WAIT_CYCLE=%u\n", counter_read(C_SPAD_A_WAIT));
  printf("SPARSECRAFT SCRATCHPAD_B_WAIT_CYCLE=%u\n", counter_read(C_SPAD_B_WAIT));
  printf("SPARSECRAFT RESERVATION_STATION_FULL_CYCLES=%u\n", counter_read(C_RS_FULL));
  printf("SPARSECRAFT DMA_TLB_MISS_CYCLE=%u\n", counter_read(C_TLB_MISS));
  printf("SPARSECRAFT RDMA_BYTES_REC=%u\n", counter_read(C_RDMA_BYTES));
  printf("SPARSECRAFT WDMA_BYTES_SENT=%u\n", counter_read(C_WDMA_BYTES));

  printf("SPARSECRAFT done=1\n");
  return 0;
}
