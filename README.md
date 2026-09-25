# SparseCraft

**An agentic hardware/software co-design loop for sparse matrix multiply on
Gemmini, built on [CHIA](https://chialoops.ai).** CHIA Hackathon 2026.

A Claude Code agent edits a Gemmini accelerator's Chisel RTL, its memory
configuration and its software schedule. A fixed harness it cannot touch builds
every proposal, simulates it cycle-accurately, checks every output against a
golden reference, synthesises it and scores it. When a gate fails, a second
agent debugs the change before the iteration is spent.

## Results

Workload `dnn512`: `Y = A * X`, `A` a 512x512 INT8 sparse matrix (SuiteSparse
pattern, 16x16 blocks), `X` dense 512x64. Baseline: stock Gemmini (16x16
array, 256 KB scratchpad, 64 KB accumulator) running a block-sparse SpMM kernel.
Best design of the final 15-iteration run:

| | baseline | SparseCraft | |
|---|---|---|---|
| cycles (measured) | 106,650 | 50,862 | **2.10x fewer** |
| off-chip traffic (measured) | 3.21 MB | 0.33 MB | **9.8x less** |
| area (measured logic + modelled SRAM) | 4.035 mm² | 3.115 mm² | **22.8% smaller** |
| energy (modelled) | 95.67 µJ | 17.04 µJ | **5.61x less** |
| wrong outputs | 0 | 0 | every admitted design verified |

The agent got there across three layers:

- **Software schedule:** keep all of `X` resident in the scratchpad. 9.8x less
  off-chip traffic, 1.34x fewer cycles, zero area.
- **Microarchitecture, in Chisel it wrote:** zero-gated MACs (T-A) and zero-row
  read skipping in the scratchpad (T-B).
- **Memory sizing:** once `X` was resident, the scratchpad shrank 256 → 64 KB
  and the accumulator 64 → 32 KB. This cut area, and cut cycles 36%.

Full iteration-by-iteration account, the V1/V2 comparison, and every caveat:
**[docs/results.md](docs/results.md)**. Raw records: [results/](results/).

## How the loop works

```
N10 propose (agent) -> N13 scope -> N20 legality -> N21 dedup -> N12 compile
  -> N30 elaborate -> N12b netlist changed? -> N32 kernel -> N50 simulate
  -> tripwire -> N41 equivalence -> N52 synthesise (overlapped with N50)
  -> N60 Pareto admit -> N61 diagnose -> next proposal

any gate fails -> N71 classify -> N73 repair (second agent) -> re-run every gate
```

- **The agent is sealed.** Claude Code runs with `--tools ""`, so its only way
  into the design is a bash tool inside the build container, and only three
  files are writable. The scorer, legality rules, metrics parser and kernel are
  hashed every iteration; any drift aborts the run.
- **Failures are repaired, not just recorded.** A second agent is briefed with
  the evidence, the proposer's stated intent and every earlier attempt. The
  whole gate ladder re-runs after each fix. Reverts are detected from the
  design states, and infrastructure faults never reach a model.
- **The proposer learns from the loop.** It gets strategy guidance chosen for
  the measured bottleneck, its own predictions scored against measurement, the
  alternatives it listed but did not try, and every failed design with the
  reason it failed.
- **Runs are auditable and resumable.** Every prompt is hashed into the run.
  Every tree is saved as a diff. `--resume` rebuilds a run from its records and
  verifies every past admission before continuing.

Details: [docs/architecture.md](docs/architecture.md).

## Two loops

This is the second loop. The first is kept on branch
[`archive/v1-first-loop`](https://github.com/RAJATABHA2201/chia_hackathon_2026/tree/archive/v1-first-loop).

| | V1 (first loop) | V2 (this branch) |
|---|---|---|
| failure handling | one attempt per iteration | iterative repair agent + debugging playbook |
| T-B zero skipping | built, but invisible to the score, so always rejected | measurable, admitted |
| strategy | one static prompt | modules chosen by the measured bottleneck |
| per iteration | 40 min | 21.9 min (synthesis overlapped) |
| best design | 2.09x cycles, **-31.3% area**, 5.07x energy (T-A only) | 2.10x cycles, -22.8% area, **5.61x energy** (T-A + T-B) |

The two best designs are complementary rather than one dominating: V1's is
smaller, V2's uses less energy. Both lie on one Pareto front
([docs/results.md](docs/results.md)).
[docs/v1-to-v2.md](docs/v1-to-v2.md) records every change between them.

## Quick start

Needs a host with podman, the CHIA `chia_env` conda environment, the Chipyard,
Verilator and synthesis images named in `configs/cluster.yaml`, and a logged-in
Claude Code CLI.

```bash
python scripts/check_setup.py --quick     # is every tier reachable?
python scripts/check_llm.py               # can we reach the model?
scripts/run.sh --iters 15 --synth         # cluster up, run, cluster down
scripts/run.sh --iters 15 --no-up --no-down -- --run-name my-run   # cluster already up
python scripts/report_iter.py my-run      # per-iteration report
```

No cluster needed for the tests, which include the real loop driven through a
fake cluster:

```bash
for t in tests/test_*.py; do python "$t"; done
python scripts/summarize_runs.py          # regenerate results/summary/ from the records
```

## Repository layout

```
src/        the loop: driver, CHIA nodes, agent backends, models, legality, repair
            (flat on purpose: Ray pickles remote functions by module name)
scripts/    entry points: run.sh, preflight checks, reports, result summaries
configs/    cluster and cache configuration
prompts/    system prompts, per-iteration work orders, shared fragments,
            strategy modules, and the repairer's debugging references
kernels/    spmm.c: the SpMM kernel and golden check (immutable to the agent)
workload/   matrix preparation and the generated workload headers
docker/     the synthesis image (yosys, OpenSTA, NanGate45) and its self-test
tests/      unit tests and a mock-cluster test of the whole loop
docs/       architecture, results, and the V1-to-V2 change log
results/    the published run records and generated summaries
archive/    superseded prompts, kept for provenance; never loaded
```

## Limitations

- Energy is modelled from measured counters, not measured. SRAM macro area is
  modelled from capacity.
- Synthesis without place-and-route gives no usable timing or power, so every
  design is scored at the same 2.0 ns clock.
- Results are for one workload (`dnn512`) and one run per loop.
- The block-sparse kernel is part of the baseline, so the gains above are on top
  of software block skipping.

[docs/results.md](docs/results.md) lists the corrections made after the run:
T-B's energy credit fixed to what the hardware can skip, and the mid-run resume.

## Built on

[CHIA](https://chialoops.ai), [Chipyard](https://github.com/ucb-bar/chipyard)
and [Gemmini](https://github.com/ucb-bar/gemmini), the
[SuiteSparse Matrix Collection](https://sparse.tamu.edu), yosys and OpenSTA with
the NanGate45 library. The in-loop agent is Claude Code (`claude-opus-5`).
