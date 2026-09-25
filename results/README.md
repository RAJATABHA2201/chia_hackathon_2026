# Run records

The raw records of the runs the results are drawn from, exactly as the loop
wrote them, minus the build caches (hundreds of MB of elaborated RTL and
simulator binaries that are regenerable). `summary/` is generated from these by
`python scripts/summarize_runs.py`.

| directory | what it is |
|---|---|
| `v2-final15/` | **the final run**: the V2 loop, 15 iterations, `dnn512` |
| `v2-final15-t30-aborted/` | V2's first launch at 30 Verilator threads, stopped after iteration 1 because simulation was slower than at 16 |
| `v1-final15/` | the first loop's (V1, branch `archive/v1-first-loop`) 15-iteration run on the same workload and baseline |
| `summary/` | per-iteration CSVs and the tables in the README, regenerated from the above |

## Files in a run

| file | written by | content |
|---|---|---|
| `iter_NNN.json` | the loop, once per iteration | the full record: verdict, design state, move, measured counters, area, energy, synthesis summary, repair ledger, candidates, prediction score |
| `llm_NNN.md` | the proposer | its full answer, ending in `==CANDIDATES==`, `==MUTATION==`, `==PREDICTION==` |
| `repair_NNN_K.md` | the repairer | attempt K's work order and transcript, ending in `==REPAIR==` |
| `diff_NNN.json` | the loop | the exact tree that was evaluated, as git diffs (`diff_NNN_repairK.json` after repair attempt K) |
| `synth_NNN.json` | N52 | synthesis detail including cells by type |
| `history.json` | the loop | what the proposer's history tool returns: every design tried, with its result or failure reason |
| `prompts.json` | the loop, at start | sha256 and size of every prompt the agents were given |
| `resume.json` | the loop, on `--resume` | where and how the run was resumed |
| `status.md` | the loop | the status tool's view of the last measured design |
| `profile/ChiaProfileCollector.log` | CHIA | per-node dispatch and completion times |
| `llm/` | CHIA | the Claude backend's own log |
| `console.log`, `console.resumed.log` | the driver | the console, before and after the resume |

## Reading the numbers

- `metrics.cycles`, `metrics.counters.*`, `equiv_mismatches`: measured in RTL
  simulation.
- `t3_logic_um2`: measured by yosys on NanGate45; `t3_sram_macro_um2`: modelled
  from capacity; `area_um2` is their sum.
- `energy_pj`: modelled from the measured counters by the model the run used.
  `summary/` re-scores it with the current model so runs compare under one
  model; `energy_recorded_uJ` there keeps the original.
- `t3_synthesis.fmax_mhz` and `t3_power_w`: recorded, not usable without
  place-and-route; every design is scored at the 2.0 ns target clock.

V2 iterations 1-7 ran before the resume and 8-15 after; `resume.json` records
that every earlier admission was replayed and matched first.
