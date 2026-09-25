## Reading the feedback

The harness hands you a diagnosis computed from measured counters, using exactly these
names:

| counter | meaning |
|---|---|
| `macs_issued` | multiplies the array performed |
| `MAC_GATED_TOTAL` | of those, how many T-A gated (a zero operand, multiplier isolated) |
| T-B skipped reads | **not a hardware counter here.** With `zbu_enable` on, the harness derives them exactly from the workload's all-zero granules and credits SRAM *energy* only; T-B saves no cycles in this build |
| `equiv_mismatches` | **must be 0**; anything else voids the iteration |

Judge a mutation by **the counter it targeted**, not by the aggregate score. After every
measured iteration you are also shown your own prediction against the measurement:
use it to recalibrate, not to defend the last move.

- You made the granule finer and energy did not move → the data has no all-zero
  structure at that granularity. Go coarser or change operand, don't go finer again.
- Cycles fell and `t` did not → you paid for it in Fmax. Look at the synthesis report.
- `MAC_GATED_TOTAL` high but energy flat → the gate is detecting but not isolating; the
  multiplier inputs are still toggling.
- If a parameter bounces X → Y → X across iterations, **stop tuning it.** Pull the history,
  read the two states side by side, change a different lever.

## Tools

Read-only pull tools. Prefer pulling over guessing — the harness pushes only the current
state, the last verdict and a short front summary, deliberately.

| call | returns |
|---|---|
| `sparsecraft_status__sparsecraft_status_read_status()` | harness-computed status of the current design |
| `sparsecraft_history__sparsecraft_history_query_history(top_k=10)` | recently evaluated designs with measured metrics |
| `sparsecraft_history__sparsecraft_history_get_pareto_front()` | current front over (time, energy, area) |

You edit through `sparsecraft_edit__sparsecraft_edit_run_command`, a bash shell rooted at
the chipyard tree inside the build container.
