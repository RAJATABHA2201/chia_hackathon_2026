"""Phase 3 scaffolding: apply the T-A zero-gated MAC into Gemmini's PE.scala.

Applied by the harness, not the agent, and applied IDEMPOTENTLY -- the loop
resets the chipyard tree to its pinned commit every iteration, so a one-shot
edit would vanish. Sentinel comments make re-application a no-op.

WHY THE HARNESS OWNS THE FIRST VERSION
--------------------------------------
The agent is meant to IMPROVE a mechanism, not invent one from a blank file
against a 147-line generic-typed module it cannot compile-test cheaply. This
lands a correct, bit-exact reference; the agent then owns PE.scala and can
change the detection, the isolation strategy, or the operand it gates on.

CORRECTNESS
-----------
In the WS dataflow the PE computes out_b = in_c.mac(in_a, in_b), i.e.
partial_sum + activation * weight. When the activation is zero the product is
zero and out_b is exactly the incoming partial sum, so bypassing is
bit-exact -- not an approximation, and N41 must never fire on it alone.

The typeclass trap: PE is generic over `T <: Data` with an `Arithmetic[T]`
evidence, and that typeclass (Arithmetic.scala:31-49) provides mac, *, +, -,
>>, >, zero, identity, withWidthOf, clippedToWidthOf, relu, minimum -- and NO
`===`. `io.in_a === 0.U` does not compile. The zero test must go through the
raw bits, and `.zero.asUInt` rather than `0.U` so it stays correct for the
recoded-float configs where zero is not all-zero bits.
"""
from __future__ import annotations
import os, re

BEGIN = "// ===== SPARSECRAFT T-A BEGIN (harness-applied; idempotent) ====="
CNT_BEGIN = "// ===== SPARSECRAFT COUNTER BEGIN ====="
CNT_END   = "// ===== SPARSECRAFT COUNTER END ====="
END   = "// ===== SPARSECRAFT T-A END ====="

_DECL = f'''{BEGIN}
  // SCALA-level `if`, not a Chisel Mux on a constant. This distinction is
  // load-bearing and was found by assertion, not by reasoning:
  //
  // The first version computed `sc_a_is_zero` unconditionally and relied on
  // `SparseCraftRTL.gateEnable.B` being false to constant-fold the Mux away.
  // It did fold the mux -- but `RegEnable(io.in_a, !sc_gate)` with a constant
  // false gate becomes an always-enabled register, and the elaborated netlist
  // came out DIFFERENT from stock Gemmini even with gating disabled
  // (all-collateral md5 c22c8d06f881 -> 961c1f669db4). A contaminated
  // baseline makes every later "vs vanilla" number carry an unknown offset.
  //
  // A Scala `if` constructs no hardware at all in the disabled branch, so
  // gateEnable=false is byte-identical to stock by construction rather than
  // by trusting the optimiser.
  val (sc_gate, sc_mac_a) = if (SparseCraftRTL.gateEnable) {{
    // No `===` exists on T (Arithmetic.scala provides mac/*/+/-/>>/>/zero/...
    // but not equality), so the zero test goes through the raw bits, and via
    // `.zero.asUInt` rather than 0.U so it stays correct for recoded floats.
    val z = io.in_a.asUInt === io.in_a.zero.asUInt
    val g = z && io.in_valid
    // Operand isolation: hold the last non-gated value so the multiplier
    // array stops toggling. The held product is discarded by the output mux,
    // so this costs no correctness.
    val held = RegEnable(io.in_a, !g)
    (g, Mux(g, held, io.in_a))
  }} else (false.B, io.in_a)
{END}
'''

def apply_ta(pe_src: str) -> tuple[str, bool]:
    """Return (patched source, changed?). Idempotent."""
    if BEGIN in pe_src:
        return pe_src, False

    # 1. declarations, right after the operand aliases
    anchor = "  val a  = io.in_a\n"
    if anchor not in pe_src:
        raise RuntimeError("PE.scala: operand alias anchor not found")
    src = pe_src.replace(anchor, anchor + _DECL, 1)

    # 2. feed the held operand to the multiplier
    old_in_a = "  mac_unit.io.in_a := a\n"
    if old_in_a not in src:
        raise RuntimeError("PE.scala: mac_unit.io.in_a anchor not found")
    src = src.replace(
        old_in_a,
        "  // SPARSECRAFT T-A: isolated operand. With gating off this IS `a`,\n"
        "  // because the Scala `if` above built nothing else.\n"
        "  mac_unit.io.in_a := sc_mac_a\n", 1)

    # 3. bypass the accumulator on a gated MAC, in BOTH WS branches.
    #    out_b = in_c.mac(in_a, in_b) = b + a*w, so a==0 makes it exactly b.
    n = src.count("      io.out_b := mac_unit.io.out_d\n")
    if n != 2:
        raise RuntimeError(f"PE.scala: expected 2 WS out_b assignments, found {n}")
    # `if` at Scala level again: with gating off this emits the stock
    # assignment verbatim, so no mux reaches the netlist to be folded.
    src = src.replace(
        "      io.out_b := mac_unit.io.out_d\n",
        "      // SPARSECRAFT T-A: a == 0 => product is 0 => out_b is exactly the\n"
        "      // incoming partial sum. Bit-exact, not an approximation.\n"
        "      io.out_b := (if (SparseCraftRTL.gateEnable) "
        "Mux(sc_gate, b, mac_unit.io.out_d) else mac_unit.io.out_d)\n")
    return src, True


def apply_counters(cf_src: str, ec_src: str) -> tuple[str, str, bool]:
    """Add MAC_GATED_TOTAL: the measured count of zero-operand multiplies.

    WHERE IT IS COUNTED, AND WHY NOT IN THE PE
    ------------------------------------------
    The obvious place is the PE, but CounterEvent signals are Bool -- one
    per-cycle event -- and a 16x16 mesh can gate up to 256 MACs in a cycle.
    Carrying a COUNT means a 32-bit CounterExternal, and getting a per-PE
    signal to one would mean threading an aggregate up PE -> Tile -> Mesh ->
    MeshWithDelays -> ExecuteController: five upstream files, all of them
    load-bearing, for a number obtainable in one.

    Instead it is counted where the A row ENTERS the mesh
    (ExecuteController:879). In the weight-stationary dataflow each element of
    that row streams across `meshColumns` PEs, multiplying against a different
    weight at each, so one zero element gates exactly `meshColumns` MACs over
    its lifetime. PopCount of the zeros times meshColumns is therefore the
    same quantity the PE-level counter would have produced.

    A useful side effect: this counts zero OPERANDS whether or not gating is
    enabled, so the baseline reports the opportunity it is leaving on the
    table, and gate-on vs gate-off is a like-for-like comparison.
    """
    if CNT_BEGIN in ec_src:
        return cf_src, ec_src, False

    # --- CounterFile: one new 32-bit external slot -----------------------
    old_n = "  val WDMA_TOTAL_LATENCY = 7\n\n  val n = 8"
    if old_n not in cf_src:
        raise RuntimeError("CounterFile.scala: CounterExternal block not found")
    cf = cf_src.replace(old_n,
        "  val WDMA_TOTAL_LATENCY = 7\n\n"
        "  // SPARSECRAFT: zero-operand multiplies, counted at the mesh input.\n"
        "  val MAC_GATED_TOTAL = 8\n\n  val n = 9", 1)

    # --- ExecuteController: accumulate and publish ------------------------
    anchor_ec = "  CounterEventIO.init(io.counter)\n"
    if anchor_ec not in ec_src:
        raise RuntimeError("ExecuteController.scala: counter init anchor not found")
    block = f"""{CNT_BEGIN}
  // One zero element of the A row gates `meshColumns` MACs as it streams
  // across the array, so the per-fire contribution is PopCount(zeros) *
  // meshColumns. Free-running; CounterFile rebases external counters itself.
  val sc_mac_gated_total = RegInit(0.U(32.W))
  // GUARDED the way Gemmini itself guards a real A row (ExecuteController's
  // own `cntl.a_fire && mesh.io.a.fire && !cntl.a_garbage &&
  // cntl.a_unpadded_cols > 0 && !cntl.im2colling`).
  //
  // The first version counted on bare `mesh.io.a.fire` across all lanes, and
  // reported 385,024 gated against 262,144 issued -- 1.47x more gated MACs
  // than multiplies performed, which is impossible. `a.fire` also asserts on
  // flush, preload and garbage rows, and the padded lanes of a partial row
  // read as zero. Only populated lanes of a real compute row may be counted.
  when (mesh.io.a.fire && !cntl.a_garbage && cntl.a_unpadded_cols > 0.U &&
        !cntl.im2colling) {{
    val sc_lanes = mesh.io.a.bits.flatten
    val sc_zeros = PopCount(sc_lanes.zipWithIndex.map {{ case (x, i) =>
      (i.U < cntl.a_unpadded_cols) && (x.asUInt === 0.U) }})
    sc_mac_gated_total := sc_mac_gated_total + sc_zeros * meshColumns.U
  }}
{CNT_END}
  CounterEventIO.init(io.counter)
  io.counter.connectExternalCounter(CounterExternal.MAC_GATED_TOTAL, sc_mac_gated_total)
"""
    ec = ec_src.replace(anchor_ec, block, 1)
    return cf, ec, True


def apply_counter_header(hdr_src: str) -> tuple[str, bool]:
    """Add MAC_GATED_TOTAL to gemmini_counter.h.

    The Scala side (CounterExternal) and the C side (gemmini_counter.h) are
    maintained SEPARATELY -- adding the counter to one does not add it to the
    other, and the mismatch only surfaces when the kernel fails to compile:

        spmm.c:110: error: 'MAC_GATED_TOTAL' undeclared

    Caught by N32 with the exact compiler error, which is the harness behaving
    correctly, but it costs an elaboration (98 s) to discover because the
    kernel builds AFTER the RTL. Worth remembering when adding any future
    counter: patch both sides in the same change.
    """
    if "MAC_GATED_TOTAL" in hdr_src:
        return hdr_src, False
    anchor_h = "#define WDMA_TOTAL_LATENCY (INCREMENTAL_COUNTERS + 7)"
    if anchor_h not in hdr_src:
        raise RuntimeError("gemmini_counter.h: external counter block not found")
    return hdr_src.replace(
        anchor_h,
        anchor_h + "\n\n// SPARSECRAFT: zero-operand multiplies, counted at the mesh\n"
                   "// input (ExecuteController). Must match CounterExternal.MAC_GATED_TOTAL.\n"
                   "#define MAC_GATED_TOTAL (INCREMENTAL_COUNTERS + 8)", 1), True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pe", required=True)
    ap.add_argument("--counterfile")
    ap.add_argument("--execcontroller")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.counterfile and a.execcontroller:
        cf, ec, ch = apply_counters(open(a.counterfile).read(),
                                    open(a.execcontroller).read())
        if ch and not a.check:
            open(a.counterfile, "w").write(cf)
            open(a.execcontroller, "w").write(ec)
        print("COUNTERS_" + ("APPLIED" if ch else "ALREADY_APPLIED"))
    src = open(a.pe).read()
    out, changed = apply_ta(src)
    if a.check:
        print("ALREADY_APPLIED" if not changed else "WOULD_APPLY")
        return 0
    if changed:
        open(a.pe, "w").write(out)
    print("APPLIED" if changed else "ALREADY_APPLIED (no-op)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# T-B: the Zero Bitmap Unit (ZBU), in ScratchpadBank.
#
# T-A gates the MULTIPLY, which the measured energy mix prices at 0.45%. The
# 25.7% that matters is the SRAM operand READ, and skipping that requires
# knowing a row is zero BEFORE issuing the read -- i.e. metadata. Hence a
# bitmap written on mvin and consulted on read.
#
# The key structural fact: a Gemmini scratchpad row is exactly DIM elements,
# so at granule_size = DIM one row IS one granule and the bitmap is 1 bit per
# row. 4096 rows/bank x 4 banks = 16,384 bits = 2 KB = 0.78% of a 256 KB
# scratchpad, inside the 5% T0 budget.
#
# Bit-exactness: a row whose bitmap bit is set is all zeros, so returning a
# hard zero instead of reading it is the same value. The bit is set ONLY on a
# full-width zero write; any masked write clears it and the row is read
# normally. A cleared bit is always safe, so the conservative direction is the
# correct one.
# ---------------------------------------------------------------------------
ZBU_SENTINEL = "// SPARSECRAFT-ZBU"


# ---------------------------------------------------------------------------
# The ZBU as an AGENT-OWNED MODULE.
#
# It used to live inline inside the Scratchpad patch, which made
# SparseCraftSparsity.scala a dead writable slot: the prompt called it "yours
# to write and rewrite", but nothing instantiated it, so anything the agent
# wrote there compiled, elaborated to a byte-identical netlist and came back
# as N12b RTL_NOOP -- after paying a full elaboration. Task 3.3.
#
# Scratchpad.scala stays harness-owned and now only WIRES this module up. The
# mechanism itself -- granularity, how the bit is computed, what is remembered
# -- is the agent's, which is the whole point of T-B being a search target.
# ---------------------------------------------------------------------------
def zbu_module_src() -> str:
    """The seed SparseCraftSparsity.scala: a minimal, correct ZBU."""
    return '''// See README.md for license details.
package gemmini

import chisel3._
import chisel3.util._

/** Zero-Bitmap Unit (ZBU) -- T-B: zero-granule skipping.
  *
  * ==== INTERFACE -- THIS PORT LIST IS A CONTRACT ====
  *
  * Scratchpad.scala is harness-owned and instantiates this module verbatim.
  * It will not adapt to a changed port list, so renaming or re-typing a port
  * fails elaboration. Everything INSIDE the module is yours.
  *
  *   n   rows in the bank -- one bitmap bit per row
  *   w   row width in bits
  *
  *   io.write_fire  Input  Bool       a write is landing this cycle
  *   io.write_addr  Input  UInt       its row
  *   io.write_data  Input  UInt(w.W)  its data
  *   io.write_full  Input  Bool       the write covers the WHOLE row
  *   io.read_addr   Input  UInt       the row being read this cycle
  *   io.read_en     Input  Bool       a read is being issued this cycle
  *   io.skip        Output Bool       this read may be skipped: row is zero
  *
  * ==== SEMANTICS THE HARNESS RELIES ON ====
  *
  *  - `skip` may be high ONLY when the row is KNOWN to be entirely zero.
  *    Skipping a zero row is bit-exact: the scratchpad returns a hard zero
  *    instead of reading it, which is the same value. A WRONGLY set bit
  *    silently corrupts the result and N41 will fail the iteration.
  *  - A partial (masked) write must CLEAR the bit -- the row is then partly
  *    unknown. A cleared bit is always safe, so conservative is correct.
  *
  * ==== WHAT TO TRY ====
  *
  * The seed tracks one bit per row at granule = DIM. Finer granularity finds
  * more zeros but costs more bitmap state; tracking the B operand as well as A
  * catches a different population; remembering a count rather than a flag lets
  * you skip partially.
  */
class SparseCraftZBU(n: Int, w: Int) extends Module {
  val io = IO(new Bundle {
    val write_fire = Input(Bool())
    val write_addr = Input(UInt(log2Ceil(n).W))
    val write_data = Input(UInt(w.W))
    val write_full = Input(Bool())
    val read_addr  = Input(UInt(log2Ceil(n).W))
    val read_en    = Input(Bool())
    val skip       = Output(Bool())
  })

  // One bit per row. At granule_size = DIM a row IS a granule.
  val bitmap = RegInit(VecInit(Seq.fill(n)(false.B)))

  // Set only on a FULL-WIDTH zero write; any masked write clears it.
  when (io.write_fire) {
    bitmap(io.write_addr) := io.write_full && (io.write_data === 0.U)
  }

  io.skip := bitmap(io.read_addr) && io.read_en
}
'''


def apply_tb(sp_src: str):
    """Patch Scratchpad.scala with the ZBU. Idempotent."""
    if ZBU_SENTINEL in sp_src:
        return sp_src, False

    out = sp_src

    # --- 1. expose a per-bank skip pulse for the counter ------------------
    anchor_io = "    val write = Flipped(new ScratchpadWriteIO(n, w, mask_len))"
    if anchor_io not in out:
        raise RuntimeError("Scratchpad.scala: ScratchpadBank write IO not found")
    # Optional port, not a plain one: an unconditional Output(Bool()) is still
    # a module port when zbuEnable is false, so the "disabled" netlist would
    # NOT match stock and every baseline would be contaminated. This is the
    # exact trap T-A hit (Sec 9h); Option makes the port vanish entirely.
    out = out.replace(anchor_io,
        anchor_io + f"\n    val sc_zbu_skip = if (SparseCraftRTL.zbuEnable) "
                    f"Some(Output(Bool())) else None   {ZBU_SENTINEL}", 1)

    # --- 2. default-drive it, so the ext_mem branch is legal too ----------
    anchor_def = "  val fromDMA = io.read.req.bits.fromDMA"
    if anchor_def not in out:
        raise RuntimeError("Scratchpad.scala: fromDMA anchor not found")
    out = out.replace(anchor_def,
        anchor_def + f"\n  io.sc_zbu_skip.foreach(_ := false.B)   {ZBU_SENTINEL} default", 1)

    # --- 3. the bitmap itself, on the SyncReadMem path --------------------
    old = """    val raddr = io.read.req.bits.addr
    val rdata = if (single_ported) {
      assert(!(ren && io.write.fire))
      mem.read(raddr, ren && !io.write.fire).asUInt
    } else {
      mem.read(raddr, ren).asUInt
    }
    q.io.enq.valid := RegNext(ren)
    q.io.enq.bits.data := rdata"""
    if old not in out:
        raise RuntimeError("Scratchpad.scala: SyncReadMem read block not found")
    new = f"""    val raddr = io.read.req.bits.addr

    {ZBU_SENTINEL} -- the mechanism itself lives in SparseCraftSparsity.scala,
    // which the AGENT owns. This file only wires it up. Keeping the bitmap
    // here instead made that file a dead writable slot: nothing instantiated
    // it, so an agent edit elaborated to an identical netlist and came back as
    // N12b RTL_NOOP after a full elaboration had been paid for (task 3.3).
    //
    // The instantiation is INSIDE a Scala `if`, so with zbuEnable false the
    // module is never elaborated -- not one register or port is emitted and
    // the netlist stays byte-identical to stock. Instantiating it
    // unconditionally would leave it driven-but-unread, which firtool is not
    // obliged to prune: the same mistake that contaminated the first T-A
    // baseline (Sec 9h).
    val sc_skip = if (SparseCraftRTL.zbuEnable) {{
      val sc_zbu = Module(new SparseCraftZBU(n, w))
      sc_zbu.io.write_fire := io.write.fire
      sc_zbu.io.write_addr := io.write.addr
      sc_zbu.io.write_data := io.write.data.asUInt
      // A masked write leaves the row partly unknown, so the module clears the
      // bit unless the write covers the whole row. `aligned_to >= w` means the
      // bank has no mask granularity finer than a row, so every write is full.
      sc_zbu.io.write_full := (if (aligned_to >= w) true.B
                               else io.write.mask.asUInt.andR)
      sc_zbu.io.read_addr := raddr
      sc_zbu.io.read_en := ren
      sc_zbu.io.skip
    }} else false.B
    io.sc_zbu_skip.foreach(_ := sc_skip)

    val rdata = if (single_ported) {{
      assert(!(ren && io.write.fire))
      // The enable is selected in SCALA, not Chisel. `ren && !sc_skip` with
      // sc_skip = false.B relies on firtool folding it back to `ren`, and the
      // netlist-equality check proved it does not (c48e84df -> 968764a4).
      // Emit the stock expression verbatim when disabled.
      mem.read(raddr, if (SparseCraftRTL.zbuEnable) (ren && !io.write.fire && !sc_skip)
                      else (ren && !io.write.fire)).asUInt
    }} else {{
      mem.read(raddr, if (SparseCraftRTL.zbuEnable) (ren && !sc_skip) else ren).asUInt
    }}
    q.io.enq.valid := RegNext(ren)
    q.io.enq.bits.data := (if (SparseCraftRTL.zbuEnable)
                             Mux(RegNext(sc_skip), 0.U, rdata) else rdata)"""
    out = out.replace(old, new, 1)
    return out, True
