# Synthesizing the Gemmini tile with yosys + NanGate45

The working recipe, and the five blockers that had to be cleared to get it.
Reached 2026-09-19. Supersedes the hammer path in `src/synth_node.py`, which fails
at `fill_outputs` because yosys never produces a mapped netlist.

## Result

```
Chip area for top module 'Gemmini':  2,372,199 um2   (2.37 mm2)
  of which sequential elements:        298,773 um2   (12.59%)
```

Standard-cell logic only; SRAM macros are blackboxed (see blocker 4).
Wall time ~8 min on this host.

**Calibration note for the paper:** `t1_model.predict()` estimates 537,842 um2
for the same design. The measured value is **4.4x larger**. Together with the
186x cycles error (T1 predicted 16,777,216, measured 89,986) this makes the T1
rejection filter actively dangerous as a gate -- it should be recorded as a
prediction, not used to reject, until `delta_o` is calibrated.

## The five blockers

1. **Whole-SoC parse.** `synth_node` stages all 646 generated files and yosys
   reads every one before `hierarchy -top Gemmini` prunes. It dies on SoC
   TileLink glue (`TLAtomicAutomata_pbus.sv:258`) that is not in Gemmini's cone
   at all. Fix: compute the module closure from `Gemmini` and stage only those
   -- **153 files, 23% of the original set**, which is also much faster.

2. **firtool assignment patterns.** firtool emits `wire [18:0] x = '{1'h0, ...}`.
   Yosys 0.38's frontend rejects `'{` (`unexpected OP_CAST`). Chipyard's
   `ENABLE_YOSYS_FLOW` is supposed to prevent this by adding
   `disallowPackedArrays`, but with that flag **firtool emits no Verilog at
   all** -- its pass pipeline completes, `gen-collateral/` is empty, and
   `model_module_hierarchy.json` is never written, so make fails. So that route
   is closed. Fix instead: rewrite `= '{` to `= {` in the staged copies. Safe
   for firtool output specifically, which only ever emits flat element lists,
   so the assignment pattern and the concatenation have identical width and
   bit order. Only 3 real Gemmini files need it (CounterFile, LoopMatmulStC,
   RRArbiter) plus 5 TLMonitors.

3. **`plusarg_reader` undefined.** A rocket-chip simulation-only module
   (`util/PlusArg.scala`) with no body. Its only consumer in the cone is an
   assertion, which synthesis drops. Fix: a blackbox stub carrying its real
   parameter list -- `FORMAT`, `DEFAULT`, `WIDTH` -- or hierarchy fails with
   "does not have a parameter named 'WIDTH'".

4. **SRAM macros undefined.** `--repl-seq-mem` replaces memories with `mem_ext`
   and friends, defined behaviourally in `*.top.mems.v`. Reading that file
   normally makes yosys synthesise SRAM out of flip-flops, which inflates area
   beyond meaning. NanGate45 has no SRAM compiler, so there is no real macro
   area to be had either. Fix: `read_verilog -lib` on the mems file, which
   reads the modules as blackboxes. **Report SRAM separately** -- `t1_model`
   computes on-chip SRAM bytes exactly, and the review notes that SRAM is
   60-80% of tile area, so quoting logic area alone without saying so would be
   misleading.

5. **ABC out of memory.** `synth -top Gemmini -flatten` followed by `abc` gets
   SIGKILLed (return code 137) on this 30 GB host. Fix: drop `-flatten` and use
   `abc -fast`. Hierarchical synthesis keeps ABC's working set small; `stat`
   still reports whole-design area.

Plus one for the STA step: yosys emits surviving `always @(posedge clock)
assert(...)` blocks into the netlist and OpenSTA cannot parse them. Add
`chformal -remove` after `synth`, and `write_verilog -noattr`.

## The script

```tcl
read_liberty -lib $LIB
read_verilog -sv stubs.v                 # blackbox plusarg_reader
read_verilog -lib <config>.top.mems.v    # blackbox the SRAM macros
read_verilog -sv <each of the 153 cone files, with '{ rewritten>
hierarchy -check -top Gemmini
synth -top Gemmini                       # NOT -flatten: ABC OOMs
chformal -remove                         # or OpenSTA cannot read the netlist
dfflibmap -liberty $LIB
abc -fast -liberty $LIB                  # -fast: ABC OOMs otherwise
opt_clean -purge
write_verilog -noattr Gemmini.mapped.v
stat -liberty $LIB
```

`$LIB = /home/ray/pdk/nangate45/lib/NangateOpenCellLibrary_typical.lib`

## Power

OpenSTA in this image supports `report_power`, so power comes off the same
mapped netlist with no P&R and no T4 tier:

```tcl
read_liberty $LIB
read_verilog Gemmini.mapped.v
link_design Gemmini
create_clock -name clk -period 2.0 [get_ports clock]
report_checks -path_delay max
report_power
```

Without annotated switching activity this uses default toggle rates: a
standard-cell-library-grounded estimate, not a gate-accurate measurement, and
the paper must say so. Feeding a VCD from the Verilator run would improve it.

## What is and is not trustworthy (measured 2026-09-19)

| Quantity | Value | Trust |
|---|---|---|
| Standard-cell area | 2,372,199 um2 | **Credible.** `stat -liberty` sums cell areas straight from the liberty; no timing model is involved. |
| Worst slack @2.0ns | -5150.49 ns | **NOT credible.** |
| Total power | 625.7 W | **NOT credible.** |

The worst path has **28 stages** and reports -5150 ns. Twenty-eight NanGate45
gates are 1-3 ns, not 5 microseconds. The delays are being extrapolated far
outside the liberty's characterization range, which is what happens when nets
have enormous fanout and nothing has buffered them: `abc -fast` plus
hierarchical (non-flattened) synthesis does no sizing or buffer insertion. The
625 W is the same defect seen through switching power, since it scales with the
same wrong capacitance.

So this recipe currently yields **area only**. To make timing and power real,
the netlist needs a physical-ish optimization pass before STA -- at minimum
`set_driving_cell` / `set_load` / `set_max_fanout` constraints, and realistically
OpenROAD's `repair_design` to insert buffers and fix fanout and transition
violations. That is the next piece of work, not something the STA invocation
above can fix by itself.

Reporting guidance for the paper: quote **area as measured**, and either omit
Fmax and power or state explicitly that they come from the T1 analytical model.
Do not quote the numbers above for timing or power.
