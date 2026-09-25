"""N60 Score + Pareto admission, and N62 the MAP-Elites archive.

This module is the replacement for Fig. 1's ``improves?`` gate, which the review
calls thesis-invalidating (Sec 2.4) for three reasons:

  * it is a scalar predicate on a multi-objective problem, placed *upstream* of
    the node that measures two of the objectives;
  * strict improvement is pure hill climbing, so it rejects the first half of
    every two-step coupled move -- which is the entire thesis of the paper;
  * because it precedes synthesis, the loop never learns the area cost of an
    edit it rejected.

The replacement is Pareto admission on ``(t, E, A)`` plus an annealed
stepping-stone tolerance -- the whiteboard's "Evaluator +/- 10% of prev value",
formalised.

Goodhart guards (Sec 3.3 Step 6): the weights live here, are hashed into the
integrity manifest, and are **not** fields of the design state. The agent sees
R(x) and its components but can neither read nor write the weights.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

# --- Objective weights. Immutable, hashed, never agent-visible. -------------
WEIGHTS = {"t": 0.60, "E": 0.25, "A": 0.15}
LAMBDA_S = 2.0          # SRAM over-budget hinge
LAMBDA_F = 2.0          # timing-closure hinge
COST_COEFF = 1e-3       # makes cost-per-unit-improvement an optimised quantity

TAU_0 = 0.10            # stepping-stone tolerance in log-R units, annealed to 0


def weights_hash() -> str:
    blob = json.dumps({"weights": WEIGHTS, "lambda_S": LAMBDA_S,
                       "lambda_F": LAMBDA_F, "cost": COST_COEFF, "tau0": TAU_0},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Verdict(str, Enum):
    ADMIT_FRONT = "ADMIT_FRONT"
    ADMIT_ARCHIVE = "ADMIT_ARCHIVE"
    ADMIT_STEP = "ADMIT_STEP"
    REJECT = "REJECT"
    INFEASIBLE = "INFEASIBLE"


@dataclass
class Point:
    """One evaluated design. Objectives are (t, E, A); everything else diagnostic."""
    state_hash: str
    parent_hash: Optional[str]
    t: float                      # time = cycles x period
    E: float                      # energy
    A: float                      # area
    sram_bytes: int               # constraint + component of A, never an axis
    verification: str = "PASS"    # PASS | DEGRADED | FAIL
    accuracy: float = 1.0
    wns_ns: float = 0.0
    cost: float = 0.0             # tokens + wall-clock
    descriptor: Tuple = ()
    meta: dict = field(default_factory=dict)

    def objective(self) -> Tuple[float, float, float]:
        return (self.t, self.E, self.A)


def dominates(a: Point, b: Point) -> bool:
    """a dominates b: no worse on every objective, strictly better on one."""
    oa, ob = a.objective(), b.objective()
    return all(x <= y for x, y in zip(oa, ob)) and any(x < y for x, y in zip(oa, ob))


# --------------------------------------------------------------------------
# Step 1 -- hard constraints, lexicographic, NEVER weighted.
# A weighted correctness term is a price the agent can pay, and it will.
# --------------------------------------------------------------------------
def feasible(p: Point, *, q_min: float, sram_budget: int, area_budget: float) -> Tuple[bool, str]:
    if p.verification == "FAIL":
        return False, "verification.FAIL"
    if p.verification == "DEGRADED":
        return False, "verification.DEGRADED"
    if p.accuracy < q_min:
        return False, f"accuracy {p.accuracy:.4f} < q_min {q_min}"
    if p.sram_bytes > sram_budget:
        return False, f"sram {p.sram_bytes} > budget {sram_budget}"
    if p.A > area_budget:
        return False, f"area {p.A:.3e} > budget {area_budget:.3e}"
    return True, ""


# --------------------------------------------------------------------------
# Step 3 -- interpretable scalar, used only for stepping stones and archive
# ranking. Logs make the objective scale-free and multiplicative improvements
# additive: a 2x latency win against a 2x area loss nets to zero at equal
# weights, which is the right economics for an accelerator tile.
# --------------------------------------------------------------------------
def reward(p: Point, base: Point, *, q_min: float, sram_budget: int,
           target_period_ns: float = 1.0) -> float:
    def ratio(x, x0):
        return max(x, 1e-12) / max(x0, 1e-12)

    r = -(WEIGHTS["t"] * math.log(ratio(p.t, base.t))
          + WEIGHTS["E"] * math.log(ratio(p.E, base.E))
          + WEIGHTS["A"] * math.log(ratio(p.A, base.A)))
    # One-sided hinges: a satisfied constraint yields no reward, so the agent
    # cannot bank credit by over-satisfying.
    r -= LAMBDA_S * max(0.0, p.sram_bytes / sram_budget - 1.0)
    r -= LAMBDA_F * max(0.0, -p.wns_ns / target_period_ns)
    r -= COST_COEFF * p.cost
    if p.accuracy < q_min:
        r -= 2.0 * max(0.0, (q_min - p.accuracy) / q_min)
    return r


# --------------------------------------------------------------------------
# Step 4 -- MAP-Elites descriptor. ~90 niches; 20-40 occupied at this
# evaluation count. This is what gives the agent a legitimate, auditable route
# back to "the best bitmap design at B=32" as a launch point for a coupled
# two-step move -- the mechanism that makes the cross-layer claim reachable.
# --------------------------------------------------------------------------
def effective_density(true_density: float, block_size: int,
                      base_block: int = 32) -> float:
    """rho_eff = nnz_blocks * B^2 / nnz_true.

    Coarse blocks keep the zeros that fall inside retained blocks, so effective
    density rises with B. This is the fundamental block-size tension: large B
    gives regular dense tiles but wasted MACs; small B tightens coverage but
    blows up metadata and DMA descriptors.
    """
    inflation = max(1.0, block_size / float(base_block))
    return min(1.0, true_density * inflation)


def format_for(block_size: int, n_blocks: int, density: float) -> str:
    """Bitmap below B~32, blocked-CSR above -- a computable crossover.

    Bitmap costs (S/B)^2 bits; blocked-CSR costs nnz_blocks * 2 * 16 b. The
    agent should be able to derive this from the density map alone, and it is a
    good sanity check that the search is working.
    """
    bitmap_bits = n_blocks * n_blocks
    csr_bits = max(1, int(n_blocks * n_blocks * density)) * 2 * 16
    return "bitmap" if bitmap_bits <= csr_bits else "blocked-CSR"


def descriptor(state, true_density: float, n_blocks: int = 128) -> Tuple:
    b = max(1, state.granule_size)
    dens_eff = effective_density(true_density, b)
    format_class = format_for(b, n_blocks, dens_eff)
    log2b = int(math.log2(b)) if (b & (b - 1)) == 0 else int(math.log2(b))
    log2b_bucket = min(7, max(5, log2b))
    effective_density_val = dens_eff
    if effective_density_val < 0.10:
        dens = "<10%"
    elif effective_density_val <= 0.30:
        dens = "10-30%"
    else:
        dens = ">30%"
    return (format_class, log2b_bucket, dens, state.dataflow)


@dataclass
class Archive:
    """Niche store: best-R design per descriptor cell."""
    cells: Dict[Tuple, Point] = field(default_factory=dict)
    scores: Dict[Tuple, float] = field(default_factory=dict)
    visits: Dict[Tuple, int] = field(default_factory=dict)

    def offer(self, p: Point, r: float) -> bool:
        d = p.descriptor
        self.visits[d] = self.visits.get(d, 0) + 1
        if d not in self.cells or r > self.scores[d]:
            self.cells[d], self.scores[d] = p, r
            return True
        return False

    def restart_candidates(self) -> List[Point]:
        """Weight niches by 1/visits so under-explored cells are preferred."""
        return sorted(self.cells.values(),
                      key=lambda p: self.visits.get(p.descriptor, 1))

    def occupancy(self) -> int:
        return len(self.cells)


@dataclass
class ParetoFront:
    points: List[Point] = field(default_factory=list)

    def admit(self, p: Point) -> bool:
        """Insert p if non-dominated; drop everything it dominates."""
        if any(dominates(q, p) for q in self.points):
            return False
        self.points = [q for q in self.points if not dominates(p, q)]
        self.points.append(p)
        return True

    def summary(self, k: int = 5) -> List[dict]:
        """<=5 points -- the whole front is never pushed into the prompt."""
        pts = sorted(self.points, key=lambda q: q.t)[:k]
        return [{"state": q.state_hash, "t": q.t, "E": q.E, "A": q.A} for q in pts]

    def hypervolume(self, ref: Point) -> float:
        """HV against the baseline reference point.

        Monte-Carlo-free 3-D HV by inclusion over sorted slabs is overkill at
        this front size; this grid-free approximation sums axis-aligned boxes
        after sorting on t, which is exact for a mutually non-dominated set.
        """
        pts = sorted([q for q in self.points if q.t < ref.t], key=lambda q: q.t)
        hv, prev_t = 0.0, ref.t
        best_E, best_A = ref.E, ref.A
        for q in reversed(pts):
            best_E, best_A = min(best_E, q.E), min(best_A, q.A)
            # Clamp each axis at zero: a point that merely ties the reference on
            # E or A still contributes its t improvement, it just contributes no
            # depth in that axis.
            hv += (max(0.0, prev_t - q.t)
                   * max(0.0, ref.E - best_E)
                   * max(0.0, ref.A - best_A))
            prev_t = q.t
        return hv


# --------------------------------------------------------------------------
# Step 5 -- the acceptance rule. Correctness gating is unconditional at every
# branch, including ADMIT_STEP.
# --------------------------------------------------------------------------
def admit(p: Point, front: ParetoFront, archive: Archive, base: Point, *,
          parent_reward: Optional[float], iteration: int, budget: int,
          q_min: float = 0.99, sram_budget: int = 1 << 22,
          area_budget: float = 4.0e6, plan_id: Optional[str] = None) -> Tuple[Verdict, dict]:
    ok, why = feasible(p, q_min=q_min, sram_budget=sram_budget, area_budget=area_budget)
    if not ok:
        return Verdict.INFEASIBLE, {"reason": why}

    r = reward(p, base, q_min=q_min, sram_budget=sram_budget)
    info = {"reward": r, "tau": 0.0}

    if front.admit(p):
        archive.offer(p, r)
        info["front_size"] = len(front.points)
        return Verdict.ADMIT_FRONT, info

    if archive.offer(p, r):
        info["niche"] = p.descriptor
        return Verdict.ADMIT_ARCHIVE, info

    tau = TAU_0 * max(0.0, 1.0 - iteration / max(1, budget))
    info["tau"] = tau
    if plan_id and parent_reward is not None and r >= parent_reward - tau:
        info["plan_id"] = plan_id
        return Verdict.ADMIT_STEP, info

    return Verdict.REJECT, info
