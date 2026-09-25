"""Derive the ZBU's skipped-granule count from the workload, with no RTL.

WHY THIS FILE EXISTS
--------------------
`t1_model.energy_report` already has the ZBU energy term, already bounds-checks
it, and already raises on over-counting:

    zbu_rows        = int(m.counters.get("ZBU_SKIPPED_ROWS", 0) or 0)
    zbu_saved_bytes = zbu_rows * w.dim * INPUT_BYTES
    e_sram          = (sram_bytes_charged - zbu_saved_bytes) * ...

It reads a counter that `CounterFile.scala` never defines -- the only
SparseCraft counter in the RTL is `MAC_GATED_TOTAL = 8`. So the lookup returns
0, the term vanishes, and T-B's entire benefit is invisible to scoring. Its
cost (a bitmap, ~+4.4% area) is fully synthesised and fully charged, so the
technique is strictly Pareto-dominated and a correctly-reasoning agent rejects
it every time. It has.

Adding the counter means editing RTL. This module does it the other way: the
number of all-zero granules is a **property of the workload**, and the harness
generated the workload, so it can count them exactly.

WHAT THIS IS, AND IS NOT
------------------------
It is an EXACT count over the actual `spmm_A` data, not a statistical estimate
from a density figure. For a given granule size it reports precisely how many
granules of the stored A operand contain only zeros.

It is still a MODEL of what the hardware would skip, not a measurement of what
it did. `energy_source` must say so. The distinction matters because the RTL's
`sc_skip` currently suppresses the scratchpad READ only -- `ren`,
`q.io.enq.valid` and `io.read.req.ready` are stock -- so the pipeline slot is
still consumed and **no cycles are saved**. This module therefore makes T-B's
energy benefit visible and does not invent a cycle benefit that the hardware
does not deliver.

A SUBTLETY THAT CHANGES THE ANSWER
----------------------------------
`spmm_A` is stored BLOCKED-DENSE: `spmm_A[NZB][DIM][DIM]`, only the nonzero
blocks, with `spmm_blk_row` / `spmm_blk_col` giving their positions. The
all-zero blocks are not stored and are never fetched. So the ZBU cannot "skip"
them -- there is nothing to skip. Its opportunity is confined to granules that
are all-zero *inside* a block that was worth fetching. Counting the zero blocks
as savings would inflate the number several-fold and produce exactly the kind
of plausible-looking wrong answer the `t1_model` assertion exists to catch.
"""

from __future__ import annotations

import functools
import os
import re

# The A payload, as emitted by workload/prep_matrices.py.
_A_DECL = re.compile(r"static\s+const\s+int8_t\s+spmm_A\s*\[[^\]]*\]\s*\[[^\]]*\]"
                     r"\s*\[[^\]]*\]\s*=\s*\{", re.S)
_DEFINE = re.compile(r"^#define\s+(SPMM_\w+)\s+(\d+)\s*$", re.M)


def _parse_header(path: str) -> tuple[dict, list[int]]:
    """Return (defines, flat A values) from a generated workload header."""
    with open(path) as f:
        text = f.read()
    defines = {k: int(v) for k, v in _DEFINE.findall(text)}

    m = _A_DECL.search(text)
    if not m:
        raise ValueError(f"{path}: no spmm_A declaration found")
    # Take everything from the opening brace to the matching `};` that closes
    # the declaration. The payload is plain integers and braces, so a scan for
    # the terminator is enough and far cheaper than a real C parser.
    tail = text[m.end():]
    end = tail.find("};")
    if end < 0:
        raise ValueError(f"{path}: unterminated spmm_A initialiser")
    body = tail[:end]
    vals = [int(v) for v in re.findall(r"-?\d+", body)]
    return defines, vals


@functools.lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> tuple[dict, tuple]:
    """Cached parse. `mtime` is in the key so a regenerated header re-parses."""
    d, vals = _parse_header(path)
    return d, tuple(vals)


def skippable_granules(header_path: str, granule_size: int,
                       operand: str = "A") -> dict:
    """Count all-zero granules in the stored A operand.

    Returns a dict with the count, the total, and enough context that a caller
    can show its work rather than asserting a number.

    ``granule_size`` must divide ``DIM``: a granule is a contiguous run within
    one row of one block, and a run that straddles a row boundary is not what
    the hardware tests.
    """
    if operand != "A":
        # The seed ZBU taps the A path only; B is still fetched. Reporting a
        # saving for an operand the hardware does not skip would be a lie the
        # energy model has no way to catch.
        return {"supported": False, "reason": f"operand {operand!r} is not skipped by this ZBU"}

    st = os.stat(header_path)
    defines, vals = _load(header_path, st.st_mtime)
    dim = defines["SPMM_DIM"]
    nzb = defines["SPMM_NZB"]

    if granule_size <= 0 or dim % granule_size:
        return {"supported": False,
                "reason": f"granule_size={granule_size} does not divide DIM={dim}"}

    expected = nzb * dim * dim
    if len(vals) != expected:
        return {"supported": False,
                "reason": f"parsed {len(vals):,} A values, expected {expected:,}"}

    per_row = dim // granule_size
    zero = 0
    total = nzb * dim * per_row
    for base in range(0, expected, granule_size):
        if not any(vals[base:base + granule_size]):
            zero += 1

    return {
        "supported": True,
        "granule_size": granule_size,
        "zero_granules": zero,
        "total_granules": total,
        "fraction": zero / total if total else 0.0,
        "elements_skipped_per_pass": zero * granule_size,
        "stored_a_elements": expected,
        "dim": dim,
        "nz_blocks": nzb,
    }


def derived_zbu_rows(header_path: str, granule_size: int, operand: str,
                     macs_issued: int) -> tuple[int, dict]:
    """Skipped A reads expressed in the unit `t1_model` already uses.

    `t1_model` charges `zbu_saved_bytes = zbu_rows * dim * INPUT_BYTES`, i.e.
    it counts DIM-wide row reads avoided. A finer granule skips part of a row,
    so the count is converted rather than assumed:

        skipped_elements = zero_granules x granule_size x reuse
        zbu_rows         = skipped_elements / dim

    ``reuse`` is how many times the stored A operand is read over the whole
    matmul, derived from the measured `macs_issued` rather than from the loop
    order -- each issued MAC consumes exactly one A element, so the ratio is
    exact and needs no assumption about tiling.
    """
    info = skippable_granules(header_path, granule_size, operand)
    if not info.get("supported"):
        return 0, info

    stored = info["stored_a_elements"]
    reuse = (macs_issued / stored) if stored else 0.0
    skipped_elements = info["elements_skipped_per_pass"] * reuse
    rows = int(skipped_elements // info["dim"])

    info = dict(info, reuse=reuse, skipped_elements=skipped_elements,
                zbu_rows=rows, source="derived_from_workload")
    return rows, info
