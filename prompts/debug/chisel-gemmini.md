## Chisel and Gemmini: the traps that fail gates here

Adapted from CHIA's `chisel_debugging.md`. The BOOM-specific material is gone;
what remains is the Chisel-universal part plus what this Gemmini tree adds.

### 1. Chisel language traps

**Last connect wins.** The last `:=` to a signal in source order is the one
that elaborates. When a value is wrong, find *every* assignment to it, not just
the one you wrote. Gemmini's `PE.scala` depends on this deliberately: after the
dataflow `when` blocks, a trailing `when (!valid) { c1 := c1; c2 := c2 ... }`
overrides them. Code you add *above* that block can be silently overridden by
it; code you add *below* it overrides the stock behaviour on every cycle.
Defaults must come **before** the real connections, never after.

**Width inference is silent.** `a + b` is `max(w(a), w(b))` bits: the carry is
dropped unless you use `+&`. `Cat(a, b)` is `w(a) + w(b)` bits, so one wrong
operand width shifts every bit after it. `x(hi, lo)` is inclusive at both ends
and zero-indexed. Truncation on assignment to a narrower target raises no
error. Width problems are best confirmed from the generated Verilog, not from
reasoning about the Scala.

**Static versus dynamic shift.** `x >> n` with a Scala `Int` is free bit
selection. `x >> n.U` builds a barrel shifter, and `x << n.U` *widens* the
result by the shift range. An accidental `.U` on a constant shift costs area,
may cost Fmax, and changes downstream widths.

**Decoupled and queues.** Test `fire` (`valid && ready`), not `valid`.
`Queue(n)` has `n` entries and one cycle of latency; an off-by-one in depth
deadlocks or drops data. An `Irrevocable` producer may not drop `valid` once
raised.

**Uninitialised wires.** Every `Wire` must be driven on every path.
`Reference ... is not fully initialized` means some `when` branch leaves it
undriven: give it a `WireDefault(...)` or assign a default *before* the `when`.

**No cross-module register writes.** A parent cannot assign a child's
register, even through a method. Control flows parent to child through an
`Input`, child to parent through an `Output`.

**Elaboration time versus simulation time.** A Scala exception, a failed
`require`, a FIRRTL width or initialisation error is an *elaboration* bug: read
the stack trace. Wrong values with a clean build are a *simulation* bug: reason
from the counters and the first mismatch.

### 2. Toolchain behaviours

**firtool removes dead logic.** Logic whose output reaches nothing, a register
constant-folded away, or a branch behind a Scala `if` on a `false` parameter
does not exist in the netlist. That is what `RTL_NOOP` reports: your source
changed and the elaborated hardware did not. Before debugging "my change has
no effect", establish that it is on a path that drives an output.

**Verilator random initialisation.** Adding registers changes CIRCT's random
initialisation array and can move cycle counts by under 0.1% with no
functional change. That is simulation noise, not a bug.

**Register arrays and build time.** `RegInit(VecInit(Seq.fill(n)(...)))` with
thousands of entries makes Verilator emit enormous C++:

| entries | approx. C++ lines | approx. Verilator compile |
|---|---|---|
| 64 | 10 K | seconds |
| 256 | 50 K | about 1 min |
| 1024 | 200 K | about 10 min |
| 4096 | 1 M | about 75 min |

A per-row bitmap sized to a scratchpad bank can land in the bottom rows of
that table, and a build that exceeds the harness timeout fails as if it were
an error.

**`SyncReadMem` versus a register array.** `SyncReadMem` has undefined initial
contents, a registered (one cycle late) read, and undefined data on a
same-cycle read and write to one address. For a structure whose bits *grant
permission to skip data*, each of those is a correctness bug, not a
performance detail: an uninitialised or late bit skips a row that is not zero.
Use `RegInit(VecInit(...))` where the initial value or combinational read
matters, and explicitly initialise anything held in `SyncReadMem`.

### 3. This Gemmini tree

**The PE is generic.** `class PE[T <: Data](...)(implicit ev: Arithmetic[T])`:
`io.in_a` is an abstract `T`, not a `UInt`. `Arithmetic[T]` provides `mac * + -
>> > zero identity withWidthOf clippedToWidthOf relu minimum` and **no `===` or
`=/=`**. Compare raw bits: `io.in_a.asUInt === io.in_a.zero.asUInt`.

**The PE forwards its operand.** `io.out_a := a` carries the streaming operand
to the next PE in the row. Zero-gating must isolate the *multiplier's* inputs
and select the *output*; it must never hold or zero the value forwarded to the
neighbour, or every PE downstream computes on the wrong operand.

**Weight-stationary double buffering.** In WS the stationary operand lives in
`c1`/`c2`, and `in_control.propagate` selects which one feeds the multiplier
while the other is preloaded from `in_d`. Partial sums travel down on
`in_b`/`out_b`. A change that mixes up which register is live, or gates the
preload path, produces wrong answers that look tile-periodic.

**The technique flags are Scala `if`s, set by the harness.** `SparseCraftRTL`
(`gateEnable`, `zbuEnable`, ...) is regenerated from the `// SPARSECRAFT`
markers every iteration. Logic you place under `if (SparseCraftRTL.zbuEnable)`
does not exist when the marker says `zbu_enable = 0`: that is an `RTL_NOOP`,
not a Chisel bug. The T-A region in `PE.scala` is bracketed by
`// ===== SPARSECRAFT T-A BEGIN/END =====` sentinels; keep them, the harness
uses them to know the region is yours.

**Markers are comments, not constructor fields.** `gate_enable`, `zbu_enable`,
`granule_size`, `zbu_operand`, `k_chunk`, `b_blocks`, `x_resident` are
`// SPARSECRAFT <name> = <int>` lines. Writing them into the `.copy(...)` call
is `unknown parameter name`. Keep every marker present and well formed.

**The state vocabulary is not the Scala vocabulary.** `sp_capacity_kb: 64` in
the design state is `sp_capacity = CapacityInKilobytes(64)` in Scala. Never
reconstruct `SparseCraftParams.scala` from the JSON; edit the file you read.

**`SparseCraftSparsity.scala` has a port contract.** `Scratchpad.scala` is
harness-owned and instantiates `SparseCraftZBU(n, w)` verbatim with
`write_fire, write_addr, write_data, write_full, read_addr, read_en -> skip`.
Renaming or retyping a port fails elaboration. Two semantics are relied on:
`skip` may be high **only** for a row known to be entirely zero, and a partial
(masked) write must **clear** the row's bit. When a read is skipped the
scratchpad returns a hard zero, so a wrongly set bit silently turns real data
into zeros.

**The file is untracked.** `SparseCraftSparsity.scala` is created per
iteration, so `git diff` never shows it. Verify edits with
`git -C generators/gemmini status --short -- <path>` or by reading the file
back.

**Elaboration writes the kernel's view of the hardware.** `gemmini_params.h`
(DIM, scratchpad and accumulator rows) is emitted by elaboration and the kernel
compiles against it. Changing `meshRows`/`meshColumns`/`tileRows`/`tileColumns`
without matching the software schedule compiles, elaborates, simulates and
returns **wrong answers**: `DIM` moved 16 to 32 once with the tiling still
assuming 16, and 32,674 outputs mismatched.
