#!/usr/bin/env python3
"""Turn a SuiteSparse .mtx matrix into a baremetal SpMM workload header.

Emits the blocked-dense representation Gemmini actually consumes, plus a golden
reference computed here on the host.

WHY THE VALUES ARE SYNTHESISED, AND THE PATTERN IS NOT
------------------------------------------------------
The two matrices this project leans on are numerically degenerate:

  * ``n1024-l*.mtx`` (GraphChallenge sparse DNN) has exactly ONE distinct
    nonzero value, 0.0625.
  * ``sparse-images-1024_subset.mtx`` is ``coordinate pattern`` -- it carries no
    values at all, only positions.

With every value equal, a product degenerates into a count, and a functional
equivalence check loses most of its power to catch an indexing bug. So this
script keeps the REAL sparsity pattern -- which is the only thing a sparse
accelerator's performance depends on -- and synthesises non-degenerate INT8
values over it. That is standard practice in the accelerator literature, and it
is stated here rather than buried because it is a methodological choice.

Synthesised values are drawn from a seeded RNG and are NEVER zero: a structural
zero and a value that happens to be zero must stay distinguishable, or the
zero-skip hardware would be measured against a moving target.

GOLDEN REFERENCE
----------------
``Y_golden`` is computed here, in numpy, and embedded as a constant array. It is
deliberately NOT computed on the simulated Rocket core: a 512x512x64 scalar
matmul is ~17M MACs, which at the measured ~3-4k simulated cycles/s would add
hours to every iteration and swamp the measurement it exists to protect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# NO numpy: it is absent from every conda env on this host, and chia_env's
# Ray/Python versions are pinned by CHIA and must not be perturbed by a solve.
# Nothing here needs it -- the golden matmul only ever touches nonzeros, so it
# is O(nnz * N) (~0.5M ops here), which is seconds in pure Python.
import random


def read_mtx(path: str):
    """Read a MatrixMarket coordinate file. Returns (rows, cols, coords, pattern)."""
    pattern = False
    coords = []
    with open(path, errors="ignore") as f:
        header = f.readline()
        if "pattern" in header:
            pattern = True
        n = m = nnz = 0
        for line in f:
            if line.startswith("%"):
                continue
            n, m, nnz = (int(x) for x in line.split()[:3])
            break
        for line in f:
            if not line.strip() or line.startswith("%"):
                continue
            p = line.split()
            coords.append((int(p[0]) - 1, int(p[1]) - 1))
            if len(coords) == nnz:
                break
    return n, m, coords, pattern


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", required=True, help="path to a .mtx file")
    ap.add_argument("--rows", type=int, default=512, help="M: rows of A kept")
    ap.add_argument("--cols", type=int, default=512, help="K: cols of A kept")
    ap.add_argument("--n", type=int, default=64, help="N: columns of the dense X")
    ap.add_argument("--dim", type=int, default=16, help="Gemmini tile dimension")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--name", default="spmm", help="symbol prefix / stem")
    ap.add_argument("--out", required=True, help="output .h path")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    n_full, m_full, coords, pattern = read_mtx(a.matrix)

    M, K, N, DIM = a.rows, a.cols, a.n, a.dim
    if M % DIM or K % DIM:
        print(f"ERROR: rows ({M}) and cols ({K}) must be multiples of dim ({DIM})",
              file=sys.stderr)
        return 2

    coords = [(i, j) for i, j in coords if i < M and j < K]
    nnz = len(coords)
    if nnz == 0:
        print("ERROR: the slice contains no nonzeros", file=sys.stderr)
        return 2

    # Dense INT8 A over the REAL pattern. Values in [-8,8] \ {0}: small enough
    # that a DIM-deep INT32 accumulation cannot overflow, and never zero, so a
    # structural zero stays distinguishable from a stored zero.
    A = [[0] * K for _ in range(M)]
    for i, j in coords:
        v = rng.randint(1, 8) * rng.choice((-1, 1))
        A[i][j] = v

    X = [[(rng.randint(-8, 8) or 1) for _ in range(N)] for _ in range(K)]

    # Golden Y = A @ X, accumulated over NONZEROS ONLY -> O(nnz*N), not O(M*K*N).
    Y = [[0] * N for _ in range(M)]
    for i, j in coords:
        v = A[i][j]
        Yi, Xj = Y[i], X[j]
        for t in range(N):
            Yi[t] += v * Xj[t]

    # --- block the matrix the way the accelerator will walk it -------------
    Mb, Kb = M // DIM, K // DIM
    blocks, brow, bcol = [], [], []
    occupied = set((i // DIM, j // DIM) for i, j in coords)
    for bi in range(Mb):
        for bj in range(Kb):
            if (bi, bj) not in occupied:
                continue
            blk = [A[bi * DIM + r][bj * DIM:(bj + 1) * DIM] for r in range(DIM)]
            blocks.append(blk)
            brow.append(bi)
            bcol.append(bj)
    nzb = len(blocks)
    inblk = 100.0 * nnz / (nzb * DIM * DIM) if nzb else 0.0

    stats = {
        "matrix": os.path.basename(a.matrix), "M": M, "K": K, "N": N, "dim": DIM,
        "nnz": int(nnz), "density_pct": round(100.0 * nnz / (M * K), 4),
        "nz_blocks": nzb, "total_blocks": Mb * Kb,
        "nz_block_pct": round(100.0 * nzb / (Mb * Kb), 3),
        "in_block_density_pct": round(inblk, 3),
        "source_was_pattern": bool(pattern),
        "dense_macs": M * K * N, "block_macs": nzb * DIM * DIM * N,
        "useful_macs": int(nnz) * N,
    }

    def flat(x):
        """Flatten arbitrarily nested lists of ints."""
        if isinstance(x, int):
            return [x]
        out = []
        for e in x:
            out.extend(flat(e))
        return out

    def arr(x, per_line=32):
        f = flat(x)
        return ",\n".join("  " + ",".join(str(v) for v in f[s:s + per_line])
                           for s in range(0, len(f), per_line))

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write(f"""// GENERATED by workload/prep_matrices.py -- do not edit.
// {json.dumps(stats)}
#ifndef SPMM_DATA_H
#define SPMM_DATA_H
#include <stdint.h>

#define SPMM_M   {M}
#define SPMM_K   {K}
#define SPMM_N   {N}
#define SPMM_DIM {DIM}
#define SPMM_MB  {Mb}
#define SPMM_KB  {Kb}
#define SPMM_NZB {nzb}
#define SPMM_NNZ {int(nnz)}

// Nonzero A blocks, dense DIM x DIM, in block-row-major order.
static const int8_t spmm_A[SPMM_NZB][SPMM_DIM][SPMM_DIM] = {{
{arr(blocks)}
}};
static const uint16_t spmm_blk_row[SPMM_NZB] = {{
{arr(brow)}
}};
static const uint16_t spmm_blk_col[SPMM_NZB] = {{
{arr(bcol)}
}};
static const int8_t spmm_X[SPMM_K][SPMM_N] = {{
{arr(X)}
}};
// Golden reference, computed on the host. Never recomputed on the target.
static const int32_t spmm_Y_golden[SPMM_M][SPMM_N] = {{
{arr(Y, 16)}
}};
#endif
""")

    with open(os.path.splitext(a.out)[0] + ".json", "w") as f:
        json.dump(stats, f, indent=2)

    print(json.dumps(stats, indent=2))
    print(f"\nwrote {a.out}  ({os.path.getsize(a.out)/1e6:.2f} MB)")
    print(f"dense MACs {stats['dense_macs']:,} -> block MACs {stats['block_macs']:,} "
          f"-> useful MACs {stats['useful_macs']:,}")
    print(f"software block-skip saves {stats['dense_macs']/max(1,stats['block_macs']):.2f}x; "
          f"RTL zero-skip could save a further "
          f"{stats['block_macs']/max(1,stats['useful_macs']):.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
