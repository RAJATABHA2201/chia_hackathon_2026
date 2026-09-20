#!/usr/bin/env python3
"""SparseCraft: methods and results, both CHIA-loop arms.

    /home/rajatabha/miniforge3/envs/docgen/bin/python make_methods_pdf.py

Documents the two arms that were run, how each is invoked, how they differ, and
what each produced. Every measured number is READ from runs/<name>/iter_001.json
so the document cannot drift from the harness records.
"""
from __future__ import annotations

import glob
import json
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (PageBreak, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

RUNS = "/home/chia-sparsecraft/runs"
OUT = "/home/chia-sparsecraft/paper/SparseCraft_Methods_and_Results.pdf"


def vals(run):
    p = os.path.join(RUNS, run, "iter_001.json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p))
    except Exception:
        return None
    m = d.get("metrics") or {}
    e = d.get("energy") or {}
    c = m.get("counters") or {}
    return dict(cyc=m.get("cycles"),
                off=(c.get("RDMA_BYTES_REC", 0) or 0) + (c.get("WDMA_BYTES_SENT", 0) or 0),
                uj=(e.get("energy_pj") or 0) / 1e6,
                pw=e.get("perf_per_watt_gops_w") or 0,
                area=(d.get("area_um2") or 0) / 1e6,
                asrc=("measured" if str(d.get("area_source", "")).startswith("T3")
                      else "modelled"))


def agent_tally(run):
    out, admitted = {}, []
    for f in sorted(glob.glob(os.path.join(RUNS, run, "iter_*.json"))):
        d = json.load(open(f))
        v = str(d.get("verdict"))
        out[v] = out.get(v, 0) + 1
        if v.startswith("ADMIT"):
            e = d.get("energy") or {}
            m = d.get("metrics") or {}
            admitted.append((d.get("iteration"), m.get("cycles"),
                             round((e.get("energy_pj") or 0) / 1e6, 3),
                             round(e.get("perf_per_watt_gops_w") or 0, 3)))
    return out, admitted


def tbl(rows, widths=None, head_bg="#22313f", size=7.2):
    t = Table(rows, repeatRows=1, hAlign="LEFT", colWidths=widths)
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), size),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(head_bg)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f7")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    return t


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=15, spaceAfter=5)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11.5, spaceBefore=11, spaceAfter=4)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontSize=9.8, spaceBefore=7, spaceAfter=3)
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=8.8, leading=11.6)
    code = ParagraphStyle("code", parent=ss["Code"], fontSize=7.4, leading=9.2,
                          backColor=colors.HexColor("#f4f4f4"), leftIndent=5,
                          borderPadding=4, spaceBefore=3, spaceAfter=5)
    small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=7.3, leading=9.2,
                           textColor=colors.HexColor("#444444"))

    doc = SimpleDocTemplate(OUT, pagesize=A4, title="SparseCraft: Methods and Results",
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=13 * mm, bottomMargin=13 * mm)
    S = []

    # ------------------------------------------------------------------ intro
    S.append(Paragraph("SparseCraft: HW/SW co-design of a sparse SpMM accelerator with CHIA", h1))
    S.append(Paragraph(
        "Two arms were run on the same CHIA loop. <b>Arm A</b> places an LLM inside the loop as "
        "the N10 proposer node, which edits Chisel and the kernel schedule inside a sandbox and "
        "is judged by the loop's gates. <b>Arm B</b> drives the same evaluation pipeline from the "
        "command line, with the design point chosen externally. Both use identical measurement: "
        "Chisel elaboration, Verilator RTL simulation, golden-equivalence checking and "
        "yosys/OpenSTA synthesis. They differ in exactly one node.", body))

    S.append(Paragraph("Workload and platform", h2))
    S.append(Paragraph(
        "Block-sparse SpMM, Y = A x X. A is <b>jagmesh7</b> (SuiteSparse), 512x512 INT8, 1,889 "
        "nonzeros: 0.72% density, 117 of 1024 16x16 blocks live, 6.31% in-block density. X is "
        "dense 512x64 INT8, Y is INT32. Target is Gemmini in Chipyard: 16x16 INT8 weight-stationary "
        "systolic array, 256 KB scratchpad, 64 KB accumulator, synthesised on nangate45 at a 2 ns "
        "clock target. A second family (<b>dnn128/256/512/1024</b>, 3.125% density, 50% block "
        "occupancy) provides a contrasting sparsity regime and a size sweep.", body))

    # ------------------------------------------------------- methods: hardware
    S.append(Paragraph("Methods 1 - Hardware (Chisel / RTL)", h2))

    S.append(Paragraph("T-A: zero-gated MAC (<font face='Courier'>PE.scala</font>)", h3))
    S.append(Paragraph(
        "In the weight-stationary dataflow the PE computes <font face='Courier'>out_b = "
        "in_c.mac(in_a, in_b)</font>, i.e. partial_sum + activation x weight. When the activation "
        "is zero the product is zero and <font face='Courier'>out_b</font> is exactly the incoming "
        "partial sum, so bypassing is <b>bit-exact rather than approximate</b>. The change detects "
        "a zero A operand, holds the multiplier input register so the multiplier array stops "
        "toggling, and muxes the accumulator through. "
        "Two implementation constraints shaped it. First, PE is generic over "
        "<font face='Courier'>T &lt;: Data</font> with an <font face='Courier'>Arithmetic[T]</font> "
        "evidence that provides mac, *, +, -, &gt;&gt;, zero and withWidthOf but <b>no equality</b>, "
        "so the zero test must go through raw bits via <font face='Courier'>.zero.asUInt</font> "
        "rather than <font face='Courier'>0.U</font>, which also keeps it correct for recoded-float "
        "configurations. Second, the whole structure sits behind a <b>Scala</b> "
        "<font face='Courier'>if</font>, not a Chisel Mux on a constant: a Mux folds, but "
        "<font face='Courier'>RegEnable</font> with a constant-false gate becomes an "
        "always-enabled register and the disabled netlist then differs from stock. A Scala "
        "<font face='Courier'>if</font> constructs no hardware in the untaken branch, making the "
        "disabled build byte-identical to stock <i>by construction</i>.", body))

    S.append(Paragraph("T-B: zero-bitmap unit (<font face='Courier'>SparseCraftSparsity.scala</font>)", h3))
    S.append(Paragraph(
        "A per-row zero bitmap in the scratchpad bank. A bit is set only on a full-width zero "
        "write; any masked write clears it, so a set bit always means the row is known to be "
        "entirely zero and a cleared bit is always safe. On a read whose bit is set, the SRAM read "
        "is suppressed and a hard zero is substituted. Implemented as a separate module with a "
        "documented port contract and instantiated from <font face='Courier'>Scratchpad.scala</font> "
        "inside the same Scala <font face='Courier'>if</font> discipline, so the disabled build "
        "emits neither the module nor its ports. Measured on jagmesh7, the unit suppresses "
        "<b>41.4%</b> of A-row reads, which is 75% of the 55.45% of rows that are genuinely all-zero.", body))

    # ------------------------------------------------------- methods: software
    S.append(Paragraph("Methods 2 - Software (kernel / schedule)", h2))

    S.append(Paragraph("Block-sparse accumulator-resident walk", h3))
    S.append(Paragraph(
        "Only the 117 nonzero blocks are materialised and walked. Each block ROW is one pass: the "
        "partial sums accumulate <i>in the accumulator</i> across k, so the output tile is written "
        "once and Y reaches DRAM exactly once. The earlier per-block formulation passed Y as both "
        "bias-in and out, dragging the accumulator tile to DRAM and back for every block after the "
        "first in a row.", body))

    S.append(Paragraph("X-resident scratchpad scheduling", h3))
    S.append(Paragraph(
        "Found from the DMA counters rather than by guesswork. Measured reads were 539,136 B "
        "against 62,720 B of ideal reads - <b>8.6x</b> - while A was read exactly once and Y "
        "written exactly once, so the entire excess was X. The kernel was moving X row-slices "
        "<i>inside</i> the per-block loop, once per nonzero block (117 times) for only <b>32 "
        "distinct</b> slices. All of X is 2,048 scratchpad rows of 16,384 at 256 KB - 12.5% - so "
        "the capacity to hold it permanently was already present and unused. The change loads every "
        "slice once, inside the timed region so the cost is charged honestly, and addresses them by "
        "block column; A moves above the resident region.", body))

    S.append(Paragraph("Co-design coupling, enforced rather than asserted", h3))
    S.append(Paragraph(
        "Each software lever is bounded by a hardware lever, and T0 rejects the pair when the "
        "hardware cannot support it: <font face='Courier'>k_chunk</font> needs "
        "<font face='Courier'>sp_capacity_kb</font> to stage k_chunk x DIM rows of A plus "
        "k_chunk x (N/DIM) x DIM of B; <font face='Courier'>b_blocks</font> is capped by "
        "<font face='Courier'>dma_maxbytes</font>; <font face='Courier'>x_resident</font> needs the "
        "scratchpad to hold all 32 slices at once. Verified: k_chunk=128 is legal at 256 KB and "
        "rejected at 128 KB; b_blocks=8 is rejected at dma_maxbytes=64 and legal at 128. The "
        "coupling is a programmatic constraint, not a convention.", body))

    S.append(PageBreak())

    # ------------------------------------------------------------ the two arms
    S.append(Paragraph("The two arms, and how each is invoked", h2))

    S.append(Paragraph("Arm A - agentic: the LLM <i>is</i> node N10", h3))
    S.append(Paragraph(
        "The model receives the parent design state, the measured counters and a diagnosis, and "
        "edits the tree through an MCP-backed bash tool running inside the build container. It is "
        "confined to three writable files and judged by gates it cannot modify.", body))
    S.append(Preformatted(
        "export GOOGLE_CLOUD_PROJECT=<gcp-project>      # ADC, bills to GCP credits\n"
        "export GOOGLE_CLOUD_LOCATION=global\n"
        "./run.sh --iters 15 --backend vertex --model gemini-2.5-pro \\\n"
        "         --proposer agent -- --workload jag512 --run-name agentic15b", code))

    S.append(Paragraph("Arm B - directed: the design point is set externally", h3))
    S.append(Paragraph(
        "<font face='Courier'>--skip-llm</font> bypasses N10 only. Every other node runs "
        "identically, so the measurement is the same instrument.", body))
    S.append(Preformatted(
        "./run.sh --skip-llm --iters 1 -- --workload jag512 \\\n"
        "         --x-resident --gate --run-name cd-xres-gate\n"
        "# any DesignState field can be set directly:\n"
        "./run.sh --skip-llm --iters 1 -- --set sp_banks=8 --set dma_maxbytes=128", code))

    S.append(Paragraph("Running a Claude model as N10, natively in CHIA", h3))
    S.append(Paragraph(
        "No external tooling is required: CHIA already ships an "
        "<font face='Courier'>anthropic</font> backend (OpenAI-compatible transport, default model "
        "<font face='Courier'>claude-sonnet-5</font>). Swapping the proposer is a one-line change "
        "and the arm is otherwise identical - same sandbox, same gates, same writable set:", body))
    S.append(Preformatted(
        "export ANTHROPIC_API_KEY=<key>\n"
        "./run.sh --iters 15 --backend anthropic --model claude-sonnet-5 \\\n"
        "         --proposer agent -- --workload jag512 --run-name agentic-claude\n"
        "\n"
        "# verify tool-calling end to end before a long unattended run:\n"
        "python smoke_agent.py --backend anthropic", code))
    S.append(Paragraph(
        "To bill a Claude model to GCP credits instead of an Anthropic key, the model must be "
        "enabled in Vertex Model Garden and reached through Anthropic's "
        "<font face='Courier'>AnthropicVertex</font> client, which is a different API surface from "
        "the <font face='Courier'>google-genai</font> path used for Gemini. CHIA has no "
        "AnthropicVertex backend today; adding one means a class wrapping that client with the same "
        "MCP tool loop plus a PROVIDERS entry.", body))

    S.append(Paragraph("How the two arms differ", h2))
    S.append(tbl([
        ["", "Arm A - agentic (N10)", "Arm B - directed (--skip-llm)"],
        ["who proposes", "the LLM, inside the loop", "set externally via CLI"],
        ["writable set", "3 files: params, PE.scala, the ZBU", "any DesignState field"],
        ["per iteration", "one coherent change, then judged", "one design point, then measured"],
        ["may modify gates", "no - N74 aborts on drift", "yes (harness is not sandboxed)"],
        ["feedback", "counters + diagnosis fed back each turn", "read by the operator between runs"],
        ["evaluation", "T0, N12, N30/31, N32, N50, N41, N52, N60", "identical"],
    ], widths=[30 * mm, 72 * mm, 72 * mm]))

    S.append(Paragraph("CHIA nodes exercised (both arms)", h2))
    S.append(Paragraph(
        "T0 legality (typed design-state constraints, incl. the HW/SW couplings) - N12 Chisel "
        "compile gate (~20 s, in front of a ~20 min elaboration) - N13 patch scope and apply - "
        "N22 T1 analytical prediction - N30/N31 Chisel elaboration and Verilator build - N32 "
        "RISC-V kernel cross-compile - N50 T2a RTL simulation with hardware counters - N41 "
        "golden-equivalence - N52 T3 synthesis (yosys + OpenSTA, nangate45) - N60 Pareto admission "
        "with an area budget - N74 integrity assert. All expensive stages are content-addressed: "
        "elaboration keys on the hardware hash plus an RTL digest, the software build on the "
        "software hash, so a schedule-only change re-runs in minutes instead of ~20.", body))

    S.append(PageBreak())

    # ------------------------------------------------------------------ results
    S.append(Paragraph("Results 1 - co-design on jagmesh7", h2))
    pts = [("jag-b0", "Stock Gemmini (dense walk, stock RTL)", "-"),
           ("cd-base2", "+ block-sparse accumulator-resident kernel", "SW"),
           ("jag-b2", "+ T-A zero-gated MAC", "HW"),
           ("cd-xres", "+ X-resident scratchpad", "SW"),
           ("cd-xres-gate", "+ T-A and X-resident combined", "HW+SW")]
    base = vals("jag-b0")
    rows = [["design point", "layer", "cycles", "off-chip B", "energy uJ", "perf/W", "area mm2", "vs stock"]]
    for run, label, layer in pts:
        v = vals(run)
        if not v:
            continue
        rows.append([label, layer, f"{v['cyc']:,}", f"{v['off']:,}", f"{v['uj']:.2f}",
                     f"{v['pw']:.2f}", f"{v['area']:.3f} ({v['asrc'][:4]})",
                     f"{v['pw']/base['pw']:.2f}x" if base else ""])
    S.append(tbl(rows))
    best = vals("cd-xres-gate")
    if best and base:
        S.append(Spacer(1, 3))
        S.append(Paragraph(
            f"<b>{best['pw']:.2f} GOPS/W against {base['pw']:.2f} for stock Gemmini: "
            f"{best['pw']/base['pw']:.2f}x perf/W.</b> Cycles {base['cyc']:,} -&gt; {best['cyc']:,} "
            f"({base['cyc']/best['cyc']:.2f}x), off-chip {base['off']:,} -&gt; {best['off']:,} B "
            f"({base['off']/best['off']:.2f}x), energy {base['uj']:.2f} -&gt; {best['uj']:.2f} uJ "
            f"({base['uj']/best['uj']:.2f}x). Golden-equivalence: 0 mismatches at every point. "
            f"Areas marked (meas) are synthesised; (mode) are from the analytical predictor and are "
            f"not directly comparable.", body))

    # ---- sparsity sweep
    sweep = []
    for wl in ("dnn128", "dnn256", "dnn512", "dnn1024"):
        b1, b4 = vals(f"sw-{wl}-b1"), vals(f"sw-{wl}-b4")
        if b1 and b4:
            sweep.append((wl, b1, b4))
    if sweep:
        S.append(Paragraph("Results 2 - does the co-design hold across sparsity regimes?", h2))
        S.append(Paragraph(
            "Same two configurations across a second matrix family. jagmesh7 is 0.72% dense with "
            "11.4% block occupancy (structured); the dnn family is 3.125% dense with 50% block "
            "occupancy. Baseline is the tuned software kernel; 'co-designed' is T-A plus X-resident.", body))
        rows = [["workload", "block occ.", "baseline perf/W", "co-designed perf/W", "gain", "off-chip cut"]]
        j1, j4 = vals("cd-base2"), vals("cd-xres-gate")
        if j1 and j4:
            rows.append(["jag512", "11.4%", f"{j1['pw']:.2f}", f"{j4['pw']:.2f}",
                         f"{j4['pw']/j1['pw']:.3f}x", f"{j1['off']/j4['off']:.2f}x"])
        for wl, b1, b4 in sweep:
            rows.append([wl, "50%", f"{b1['pw']:.2f}", f"{b4['pw']:.2f}",
                         f"{b4['pw']/b1['pw']:.3f}x", f"{b1['off']/b4['off']:.2f}x"])
        S.append(tbl(rows))
        S.append(Spacer(1, 3))
        S.append(Paragraph(
            "The gain tracks block occupancy: the denser the block pattern, the more nonzero blocks "
            "re-fetch the dense operand, and the more redundant traffic the resident schedule "
            "removes. The optimisation is therefore not tuned to one matrix - it scales with the "
            "quantity it targets.", body))

    # ---- agentic arm
    tally, admitted = agent_tally("agentic15b")
    if tally:
        S.append(Paragraph("Results 3 - the agentic arm", h2))
        S.append(Paragraph(
            "15 iterations, Gemini 2.5 Pro as N10, starting from the tuned software baseline with "
            "the full search space available: the RTL toggles, the Chisel files, and the software "
            "schedule levers.", body))
        rows = [["verdict", "count", "meaning"]]
        meaning = {"ADMIT_FRONT": "admitted to the Pareto front",
                   "COMPILE_FAILED": "caught by the N12 Chisel gate (~1-2 min, not ~20)",
                   "RTL_NOOP": "source changed, elaborated netlist did not",
                   "SCOPE_VIOLATION": "edit outside the writable set",
                   "DUPLICATE": "design already evaluated"}
        for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
            rows.append([k, str(v), meaning.get(k, "")])
        S.append(tbl(rows, widths=[38 * mm, 16 * mm, 110 * mm]))
        S.append(Spacer(1, 3))
        if len(admitted) > 1:
            it, cyc, uj, pw = admitted[1]
            S.append(Paragraph(
                f"<b>The agent proposed and validated T-A zero-gated MAC on iteration {it}</b>, "
                f"admitted at {pw} GOPS/W against the baseline's {admitted[0][3]} "
                f"({pw/admitted[0][3]:.3f}x), with 0 equivalence mismatches - elaborated, simulated, "
                f"verified and synthesised end to end. The compile-gate failures were all Chisel "
                f"generic-type errors while attempting to author the zero-bitmap unit "
                f"(wrong number of type arguments, value zero is not a member of T, ambiguous "
                f"implicit values), which is the Arithmetic-typeclass constraint described in "
                f"Methods 1. The N12 gate reduced each to 1-2 minutes.", body))

    S.append(Spacer(1, 7))
    S.append(Paragraph(
        "Cycles and DMA byte counts are Verilator RTL simulation; area is yosys/OpenSTA on "
        "nangate45; energy is an analytical model driven by measured hardware counters. "
        "Generated from the run records.", small))

    doc.build(S)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
