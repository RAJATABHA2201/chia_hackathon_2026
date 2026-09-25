## Failure playbook

Your work order names exactly one verdict and points you at its section below.
Read that section first. Each one says what the verdict means, what the
harness re-runs after your turn, where the cause usually is, and what does
**not** count as a fix.

The harness re-runs the **whole** gate ladder after every repair attempt, from
the scope check down. So your fix is also re-checked for scope, legality,
duplication and reverts; you cannot pass one gate by breaking an earlier one.

---

### SCOPE_VIOLATION

**Meaning.** The working tree has a modified or new path outside the three
writable files. Nothing was built.
**Re-check after your turn.** Scope, then the full ladder. Seconds if it fails
again, a full build if it passes.

**Do this.**

1. Read the list of offending paths in your evidence.
2. Restore each one: `git -C generators/gemmini checkout -- <path>` for a
   tracked file, `rm <path>` for a file the proposer created.
3. If the proposer needed that change, re-express it inside
   `SparseCraftParams.scala`, `PE.scala` or `SparseCraftSparsity.scala`. If it
   cannot be expressed there, the mechanism is not reachable from the writable
   set: report `NOT_ACTIONABLE`.

**Not a fix.** Editing a harness-owned hook (`Scratchpad.scala`,
`ExecuteController.scala`, `CounterFile.scala`, `SparseCraftRTL.scala`, the
harness config, the kernel) in any form. The allowlist is not negotiable.

---

### T0_ILLEGAL

**Meaning.** The parsed design state violates a named legality rule. Nothing
was built. The violation text includes the arithmetic.
**Re-check.** T0 is microseconds, then the full ladder.

**Work the rule arithmetically.** These are the rules and their formulas
(`dim = meshRows * tileRows`, INT8 inputs, INT32 accumulators, `N = 64`):

| rule | must hold |
|---|---|
| `gemmini.square_array` | `meshRows*tileRows == meshColumns*tileColumns` |
| `gemmini.min_dim`, `gemmini.pow2_dim` | `dim >= 2`, `dim` a power of 2 |
| `gemmini.sp_bank_entries_*` | `rows/bank = sp_capacity_kb*8192 / (sp_banks * dim*8)` is > 0, a power of 2, a multiple of `dim` |
| `gemmini.acc_bank_entries_*` | `acc_capacity_kb*8192 / (acc_banks * dim*32)` is > 0 and a multiple of `dim` |
| `gemmini.sp_tiles_ge_acc_tiles` | `sp_rows/dim - 2 >= acc_rows/dim` |
| `gemmini.mvin_scale_shared` | always illegal here (8-bit input, 32-bit accumulator) |
| `capacity.sp` | `(dim*dim + dim*64) * 2 bytes <= sp_capacity_kb*1024` |
| `capacity.acc`, `capacity.acc_rows` | `dim*64*4 bytes <= acc_capacity_kb*1024`, `acc_rows >= dim` |
| `zbu.granule_divides_dim` | `dim % granule_size == 0`, `granule_size > 0` |
| `zbu.bitmap_budget` | with ZBU on: `sp_rows * (dim/granule_size) bits <= 5%` of the scratchpad bits |
| `sched.k_chunk_fits_scratchpad` | `k_chunk * dim * (1 + max(1, 64/dim)) <= sp_rows` |
| `sched.b_blocks_dma` | `b_blocks <= dma_maxbytes / dim` |
| `sched.a_blocks_layout` | `a_blocks == 1` |
| `banking.gather_streams` | `sp_banks >= 3` |
| `memory.littles_law` | `max_in_flight_mem_reqs * dma_maxbytes >= (dma_buswidth/8) * 100` |
| `fusion.no_materialised_S` | `has_normalizations` stays `true` |
| `sparsecraft.frozen_*` | `workload` and `dense_mode` markers unchanged |

**Direction rule for repairing a mutation.** The proposer moved some field `f`
from its parent value `p` to a proposed value `q`. You may:

- change a *coupled* field the rule names (raise `sp_capacity_kb` so the
  proposed `k_chunk` fits, raise `dma_maxbytes` so `b_blocks` fits), or
- move `f` to a legal value **strictly between** `p` and `q`, the nearest one
  that satisfies the rule.

You may **not** set `f` back to `p` or past it. That is a revert, and the
harness detects it by comparing the three states.

**Not a fix.** Shrinking an unrelated resource until the arithmetic passes;
touching `workload` or `dense_mode`; turning `has_normalizations` off.

---

### COMPILE_FAILED

**Meaning.** `sbt -batch "project gemmini" compile` failed. Your evidence is
the compiler's own `[error]` lines with `file:line:col`.
**Re-check.** The compile gate (20 s to 3 min), then the full ladder.
**You can and must run the compile yourself before ending your turn.**

**Usual causes, in order of frequency here.**

1. `===`/`=/=` on the generic `T` in `PE.scala`: compare `asUInt` against
   `zero.asUInt`.
2. `unknown parameter name: <x>`: a design-state name or a marker written as a
   `GemminiArrayConfig` field. Markers are comments; Scala names differ from
   JSON names.
3. `type mismatch` between `UInt`/`SInt`/`T`, or a `Bool` used where a
   `UInt` is expected (`.asUInt`, `.asTypeOf(...)`, `.B`, `.U`).
4. Missing import or an identifier from another package (`chisel3.util._`).
5. A syntax slip from an edit that did not write the whole file.

**Procedure.** Fix the first error only, recompile, read the new first error.
Scala errors cascade; the tenth message is usually noise from the first.

**Not a fix.** Deleting the proposer's logic, commenting out the region, or
putting it behind `if (false)`.

---

### ELABORATION_FAILED

**Meaning.** Compile passed, but Chisel/FIRRTL elaboration or the Verilator
build failed. Your evidence is the tail of the build log.
**Re-check.** A full elaboration, 20 to 40 minutes. Be sure before you end.

**Read the log for these, top to bottom.**

- `requirement failed` / `assertion failed` in a Gemmini config or module:
  a parameter relation the Scala enforces at elaboration. The message usually
  says what it wanted. If it is arithmetic on config fields, it is effectively
  a T0 rule the table does not have yet; fix the parameters and say which
  relation fired.
- `not fully initialized`: a `Wire` or output left undriven on some path.
- `Uninferred width` / width mismatch in FIRRTL: see width inference.
- Combinational loop detected: a gate or skip decision that feeds back into
  its own input in the same cycle, often through a `Mux` on an output.
- Port or type errors naming `SparseCraftZBU`: the port contract changed.
- A Verilator build that runs out of time or memory after a `VecInit` of
  thousands of registers: see register-array sizing.

**Not a fix.** Weakening or removing a `require`/`assert`; changing Verilator
flags, warning severities or timeouts (harness-owned, and out of scope).

---

### RTL_NOOP

**Meaning.** Your RTL source changed, but the elaborated netlist is
byte-identical to the previous design. The mechanism was not instantiated.
**Re-check.** A full elaboration.

**Usual causes.**

1. The logic sits under `if (SparseCraftRTL.gateEnable)` or `zbuEnable` while
   that `// SPARSECRAFT` marker is `0`. The proposer changed the mechanism but
   not the toggle that instantiates it.
2. The new signal drives nothing: firtool removed it. Trace it forward to an
   output port or a register that an output reads.
3. The edit is in a branch the current configuration never elaborates, for
   example the output-stationary branch of `PE.scala` while the design is
   weight-stationary.
4. The edit was overwritten by a later `:=` (last connect).

**Not a fix.** Adding a dummy output, a counter or a register that exists only
to make the netlist differ. The harness is checking that the *mechanism*
reached the hardware, and a decoy defeats that on purpose.

---

### KERNEL_BUILD_FAILED

**Meaning.** The hardware elaborated, but the SpMM kernel did not
cross-compile against the `gemmini_params.h` that elaboration emitted.
**Re-check.** The kernel build (1 to 3 min); elaboration is a cache hit if the
hardware did not change.

**Usual causes.** The software schedule and the hardware disagree: a
`k_chunk`, `b_blocks` or `x_resident` marker the new `DIM`, scratchpad or DMA
width cannot support, or a macro the kernel expects that the new
configuration no longer defines. You cannot edit the kernel. The fix is in the
markers or the hardware parameters.

---

### TRIPWIRE_FAILED

**Meaning.** Measured off-chip traffic is below the information-theoretic
floor: less than one read of the nonzero A blocks, all of X, and one write of
Y. The design computed from data it did not load, or the counter is not
counting.
**Re-check.** A full build and simulation.

**Usual causes.** Skip logic that suppresses a DMA transfer rather than a
scratchpad read; a change that stops a counter event from firing. Skipping may
only ever avoid work on data **proven** zero, and it may not remove a load
that the result depends on.

---

### EQUIV_FAILED

**Meaning.** Build and simulation succeeded, but outputs differ from the
host-computed golden `Y = A * X`. Your evidence is the mismatch count and the
**first** mismatching element `[i, j]` with `got` and `want`.
**Re-check.** A full build and a 16 minute simulation. This is the most
expensive repair there is: reason it through before you edit.

**Read the first mismatch like a symptom table.**

| symptom | where to look |
|---|---|
| `got` is an integer multiple of `want` | an accumulation ran that many times: `k_chunk`/tiling against `DIM`, or a partial sum not cleared between passes |
| `got == 0` where `want != 0` | data dropped: a skip bit set on a row that was not all zero, a partial write that did not clear its bit, or a gate that fired on a non-zero operand (compared the wrong bits or width) |
| `got` equals a neighbouring or previous value | stale data: a skipped read with no hard-zero mux, a registered lookup one cycle late, a held register where the value should pass through |
| mismatches everywhere from `[0, 0]` | systematic: `DIM` changed without the schedule, a gating condition inverted, or the forwarded operand gated |
| first mismatch at `i` or `j` a multiple of `DIM` | tile boundary: bitmap or block indexing off by one, bank selection, `propagate` register selection |
| small errors (off by one, sign) | width or signedness: `asUInt` on a signed value, truncation, `clippedToWidthOf` |
| only some rows or columns | a per-PE or per-bank bug: generator loop index, one bank mis-wired |

**Invariants to check your change against.** T-A: `c + 0 * w == c`, bit for
bit, and the forwarded operand is untouched. T-B: `skip` is high only when the
row is *known* zero; every partial write clears the bit; the bit read belongs
to the row being read *this* cycle; a same-cycle write and read to one row
resolves in favour of safety.

**Not a fix.** Disabling the technique, restricting it to a case that never
fires, or changing the workload or tiling so the broken path is not exercised.

---

### EQUIV_MISSING

**Meaning.** The simulation ended without the kernel printing its equivalence
line. Either the design **hung** (a handshake that never completes, a queue
that never drains, a skip that leaves a response owed) or the simulation hit
its time limit. Your evidence is the simulator's return code and log tail.
**Re-check.** A full build and simulation.

**Usual causes.** A skipped read that no longer returns a response while its
requester still waits for one; a `ready` that depends on a `valid` that
depends on the same `ready`; gating that holds a control register.

---

### Not yours to repair

`INFRA_FAILURE` (out of memory, a preempted worker, a lost connection),
`AGENT_FAILED`, `NO_EDIT` and `DUPLICATE` are never handed to you. The first is
not a design failure at all, and the others are proposal-policy failures, not
bugs in a mutation.
