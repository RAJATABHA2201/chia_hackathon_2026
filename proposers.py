"""Non-agentic proposers -- the control arms for the search comparison.

The loop's claim is about an *agent's* search. That claim is unfalsifiable
without controls that face the identical harness: same baseline, same T0 rules,
same evaluation budget, same measurement path. The only thing that differs is
who chooses the next design.

Two arms, neither of which calls a model:

  RandomProposer  -- uniform over one lever's candidate values. Shows the space
                     is not so easy that blind sampling solves it.
  GreedyProposer  -- coordinate descent, ONE lever at a time, never combining a
                     hardware change with a software one in a single move. This
                     is the control that matters: it is the single-lever search
                     the agent has to beat, and the review (Sec 2.4) warns the
                     loop can silently degenerate into reproducing it.

Both emit a DesignState. They do not write files -- loop.py hands the state to
nodes.apply_design_state exactly as it would the agent's, so every arm goes
through the same scope check, the same T0 gate and the same measurement.

Illegal proposals are NOT filtered out here. T0 rejects them by name in
microseconds and the iteration is recorded as T0_ILLEGAL. That cost is part of
what distinguishes a good search from a bad one, and hiding it would flatter
the controls.
"""

from __future__ import annotations

import random
from typing import Optional

from design_state import DesignState

# --------------------------------------------------------------------------
# Candidate values per lever.
#
# Chosen to respect the constraints that are cheap to honour structurally
# (powers of two where Gemmini demands them) while still generating states T0
# will reject -- capacity and tile-count relations are left to T0, because a
# search that cannot tell a legal design from an illegal one is exactly what
# the comparison is measuring.
#
# Tagged by layer so an arm can be restricted to one side if needed, and so the
# move-economics analysis sees the same HW/SW partition loop.classify_move uses.
# --------------------------------------------------------------------------
# meshRows and meshColumns are NOT separate levers. T0 requires
# meshRows*tileRows == meshColumns*tileColumns (the array must be square), so
# moving one alone is illegal by construction: greedy-1 spent all 11 of its
# search iterations on meshColumns and every single one died at T0. The array
# dimension is one knob conceptually, so it is one lever here, applied to both
# fields together by _expand().
HW_LEVERS: dict[str, list] = {
    "array_dim":                      [4, 8, 16, 32],
    "dataflow":                       ["WS", "OS", "BOTH"],
    "sp_capacity_kb":                 [64, 128, 256, 512],
    "acc_capacity_kb":                [16, 32, 64, 128],
    "sp_banks":                       [3, 4, 8],
    "acc_banks":                      [2, 4],
    "spad_read_delay":                [1, 2, 4, 8],
    "acc_latency":                    [1, 2, 4],
    "max_in_flight_mem_reqs":         [16, 32, 64, 128],
    "dma_maxbytes":                   [32, 64, 128],
    "dma_buswidth":                   [64, 128, 256],
    "tlb_size":                       [2, 4, 8, 16],
    "ld_queue_length":                [4, 8, 16],
    "st_queue_length":                [2, 4, 8],
    "ex_queue_length":                [4, 8, 16],
    "reservation_station_entries_ld": [4, 8, 16],
    "reservation_station_entries_st": [2, 4, 8],
    "reservation_station_entries_ex": [8, 16, 32],
}

SW_LEVERS: dict[str, list] = {
    "block_size": [16, 32, 64, 128],
    # tile_m / tile_n / tile_k are NOT offered. They are passed to the kernel
    # compile as -DTILE_M etc. (nodes.py:178) and the kernel #defines them
    # (attn_prefill.c:31-38) -- but never reads them. Only BLOCK_SIZE is
    # actually used (:45, :51, :66). Measured: greedy changed tile_m, the
    # kernel was rebuilt and simulated for 17 minutes, and cycles came back
    # byte-identical to the baseline at 89,986.
    #
    # Offering an inert lever to a search is worse than offering none: the arm
    # spends a full evaluation to learn nothing, and the loop admits the result
    # as a new design because the state hash changed. Restore these here only
    # once the kernel actually tiles by them.
}

ALL_LEVERS: dict[str, list] = {**HW_LEVERS, **SW_LEVERS}

# Deliberately NOT offered to any arm:
#   has_normalizations   -- false removes the hardware softmax path, which the
#                           agent is also forbidden to touch. A control allowed
#                           to disable it would "win" by breaking the workload.
#   mvin_scale_shared    -- unconditionally illegal here (input width 8 vs
#                           accumulator 32); offering it would just burn budget
#                           on a constraint no search can satisfy.
#   num_counter          -- changes the counter file the harness reads its own
#                           measurements from.
_EXCLUDED = ("has_normalizations", "mvin_scale_shared", "num_counter",
             "tileRows", "tileColumns")


def _expand(field: str, value) -> dict:
    """A lever name -> the design-state fields it sets.

    Only array_dim is compound: it drives meshRows and meshColumns together so
    the square-array constraint holds by construction rather than by luck.
    """
    if field == "array_dim":
        return {"meshRows": value, "meshColumns": value}
    return {field: value}


def _current(state, field):
    return state.meshRows if field == "array_dim" else getattr(state, field)


def _alternatives(state: DesignState, field: str) -> list:
    """Candidate values for ``field`` other than the one currently set."""
    return [v for v in ALL_LEVERS[field] if v != _current(state, field)]


class RandomProposer:
    """Uniform random single-lever perturbation."""

    name = "random"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def propose(self, parent: DesignState) -> DesignState:
        fields = [f for f in ALL_LEVERS if _alternatives(parent, f)]
        field = self.rng.choice(fields)
        return parent.mutate(**_expand(field, self.rng.choice(_alternatives(parent, field))))

    def observe(self, reward: Optional[float], admitted: bool) -> None:
        """Random search ignores feedback -- that is the point of the arm."""


class GreedyProposer:
    """Per-lever coordinate descent: the single-lever search the agent must beat.

    Walks the levers in a fixed order. For each, tries its alternative values
    one at a time. A trial that is admitted becomes the new incumbent and the
    walk restarts from the first lever, since an accepted change can reopen
    levers already passed over. A trial that is not admitted is abandoned and
    the next candidate is tried.

    It never changes two levers at once, and never combines a hardware change
    with a software one -- so any design requiring a coordinated pair is
    unreachable for it by construction. That is precisely the gap the agent is
    supposed to exploit, and making it structural rather than incidental is
    what makes the comparison mean something.
    """

    name = "greedy"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        self.order = sorted(ALL_LEVERS)
        self.rng.shuffle(self.order)       # seed-dependent order, not alphabetical bias
        self.incumbent: Optional[DesignState] = None
        self.todo: list = []               # [(field, value)] still to try
        self.pending: Optional[tuple] = None   # awaiting a verdict
        self.exhausted = False

    def _rebuild(self) -> None:
        """Every (lever, value) still worth trying against the incumbent.

        Built as ONE flat list in lever order, values shuffled within a lever.
        The previous version kept a per-lever queue and refilled it whenever it
        emptied, which meant the lever index never advanced and the search
        could not leave lever #1: greedy-1 spent all 11 search iterations on
        meshColumns. A single list that is only ever popped from cannot do
        that.
        """
        self.todo = []
        for field in self.order:
            vals = _alternatives(self.incumbent, field)
            self.rng.shuffle(vals)
            self.todo.extend((field, v) for v in vals)

    def propose(self, parent: DesignState) -> DesignState:
        if self.incumbent is None:
            self.incumbent = parent
            self._rebuild()
        if self.todo:
            field, value = self.todo.pop(0)
            self.pending = (field, value)
            return self.incumbent.mutate(**_expand(field, value))

        # Every lever exhausted with no further improvement: converged. Keep
        # returning the incumbent; loop.py's dedup marks it a duplicate, which
        # is the honest record of a greedy search that has stalled.
        self.exhausted = True
        self.pending = None
        return self.incumbent

    def observe(self, reward: Optional[float], admitted: bool) -> None:
        if self.pending is None:
            return
        field, value = self.pending
        self.pending = None
        if admitted:
            # An accepted move can reopen levers already passed over, so the
            # candidate list is rebuilt against the new incumbent.
            self.incumbent = self.incumbent.mutate(**_expand(field, value))
            self._rebuild()


def make_proposer(name: str, seed: int = 0):
    if name == "random":
        return RandomProposer(seed)
    if name == "greedy":
        return GreedyProposer(seed)
    raise ValueError(f"unknown proposer {name!r}; expected 'random' or 'greedy'")
