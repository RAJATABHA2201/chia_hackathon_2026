"""T3 by driving yosys directly, per docker/yosys_gemmini_recipe.md.

``synth_node.synthesize`` goes through hammer, and hammer's yosys plugin dies
in ``fill_outputs`` because yosys never produces a mapped netlist. The recipe
document works out why and clears five separate blockers; this module is that
recipe as code.

What it produces, all measured rather than modelled:

    area_um2       standard-cell area of the Gemmini tile on NanGate45
    seq_area_um2   the sequential share of it
    cell_count     mapped cell instances
    worst_slack_ns OpenSTA, against the target period
    fmax_mhz       derived from the slack
    power_*        OpenSTA report_power, off the same mapped netlist

Two honesty constraints the paper has to carry, both inherited from the recipe:

  * SRAM macros are blackboxed. NanGate45 has no SRAM compiler, so there is no
    real macro area to be had; reading the behavioural models instead would
    make yosys synthesise SRAM out of flip-flops and inflate the number past
    meaning. ``t1_model`` computes on-chip SRAM bytes exactly, and SRAM is
    60-80% of a tile, so area MUST be reported as logic-only with the byte
    count beside it.
  * ``report_power`` runs without annotated switching activity, so it uses
    default toggle rates. That is a standard-cell-library-grounded estimate,
    not a gate-accurate measurement. Feeding a VCD from the Verilator run
    would improve it and is not done here.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction

from constants import R_HAMMER, SYNTH_TIMEOUT_S, SYNTH_WORK_DIR
from synth_node import (ensure_tool_path, parse_synth_stat, parse_worst_slack,
                        _tool_path)

logger = logging.getLogger("sparsecraft.synth_recipe")

NANGATE_LIB = "/home/ray/pdk/nangate45/lib/NangateOpenCellLibrary_typical.lib"

# rocket-chip's simulation-only plusarg reader. Its only consumer in the
# Gemmini cone is an assertion, which synthesis drops -- but hierarchy -check
# fails without the module, and fails differently without its real parameter
# list (recipe blocker 3).
_STUBS = """\
(* blackbox *)
module plusarg_reader #(
  parameter FORMAT  = "borked=%d",
  parameter DEFAULT = 0,
  parameter WIDTH   = 1
) (output [WIDTH-1:0] out);
endmodule
"""

_MODULE_RE = re.compile(r'^\s*module\s+([A-Za-z_][\w$]*)', re.M)
_INST_RE = re.compile(
    r'^\s*([A-Za-z_][\w$]*)\s+(?:#\([^)]*\)\s*)?([A-Za-z_][\w$]*)\s*\(', re.M)
# The construct yosys 0.38 rejects as `unexpected OP_CAST`.
_ASSIGN_PATTERN_RE = re.compile(r"=\s*'\{")


@dataclass
class RecipeResult:
    success: bool = False
    top_module: str = "Gemmini"
    area_um2: float = 0.0
    seq_area_um2: float = 0.0
    cell_count: int = 0
    seq_cell_count: int = 0
    worst_slack_ns: float | None = None
    fmax_mhz: float | None = None
    clock_target_ns: float = 2.0
    power_total_w: float | None = None
    power_internal_w: float | None = None
    power_switching_w: float | None = None
    power_leakage_w: float | None = None
    # PROVENANCE. A power number is meaningless without knowing what switching
    # activity produced it, and the three sources differ by orders of
    # magnitude in trustworthiness:
    #   "vcd"      -- annotated from a real simulation trace of THIS workload
    #   "measured" -- one global toggle rate derived from this run's counters
    #   "default"  -- OpenSTA's built-in guess; design-specific but not
    #                 workload-specific
    # Recorded so a reader can never mistake one for another.
    power_activity_source: str | None = None
    power_activity: float | None = None
    # [{mode, activity, total, internal, switching, leakage}] -- one entry per
    # annotation scheme tried in the SAME STA session on the SAME netlist, so
    # the numbers differ only by the activity assumption.
    power_sweep: list | None = None
    sta_tail: str = ""
    cone_files: int = 0
    staged_files: int = 0
    rewritten_files: int = 0
    returncode: int = 0
    stderr: str = ""
    cells_by_type: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["stderr"] = self.stderr[-2000:]
        return d


def gemmini_cone(sources: dict[str, str], top: str = "Gemmini") -> set[str]:
    """Filenames in ``top``'s module closure.

    Recipe blocker 1: synth_node stages all 646 generated files and yosys
    parses every one before ``hierarchy -top`` prunes, so it dies on SoC
    TileLink glue that is not in Gemmini's cone at all. Walking the closure
    first cuts the set to ~23% and removes the offending files outright.
    """
    decl: dict[str, str] = {}
    for name, text in sources.items():
        for m in _MODULE_RE.findall(text):
            decl.setdefault(m, name)

    cone: set[str] = set()
    stack = [top]
    while stack:
        mod = stack.pop()
        if mod in cone or mod not in decl:
            continue
        cone.add(mod)
        stack.extend(n for n, _ in _INST_RE.findall(sources[decl[mod]])
                     if n in decl)
    return {decl[m] for m in cone}


def _parse_power(text: str) -> dict:
    """Pull the Total row out of OpenSTA's report_power table.

    Columns are Internal / Switching / Leakage / Total, in watts.

    ANCHORED on the table header, and sanity-checked, because the naive
    version of this function ("any line starting with total that has four
    numbers") silently matched a timing report and returned 630.0 W for every
    design in a 15-iteration run -- design-invariant and three orders of
    magnitude too large, with no error anywhere. A power parser that can
    return a wrong number is worse than one that returns nothing, because the
    wrong number gets published.

    Returns {} unless the table header was seen AND the row parses AND the
    components sum to the total.
    """
    lines = text.splitlines()
    header = None
    for i, line in enumerate(lines):
        low = line.lower()
        if "internal" in low and "switching" in low and "leakage" in low:
            header = i
            break
    if header is None:
        return {}

    for line in lines[header:]:
        if not line.strip().lower().startswith("total"):
            continue
        nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", line)
        if len(nums) < 4:
            continue
        internal, switching, leakage, total = (float(n) for n in nums[:4])
        if min(internal, switching, leakage, total) < 0:
            return {}
        # The three components must account for the total. A mis-locked row
        # fails this immediately.
        if total > 0 and abs((internal + switching + leakage) - total) > 0.02 * total:
            return {}
        return {"internal": internal, "switching": switching,
                "leakage": leakage, "total": total}
    return {}


@ChiaFunction(resources={R_HAMMER: 1})
def synthesize_recipe(generated_src_files: dict,
                      top_module: str = "Gemmini",
                      clock_period_ns: float = 2.0,
                      liberty: str = NANGATE_LIB,
                      timeout_seconds: int = SYNTH_TIMEOUT_S,
                      activity: float | None = None,
                      vcd_path: str | None = None,
                      activity_sweep: list | None = None) -> RecipeResult:
    """Synthesize the Gemmini tile and report area, timing and power."""
    r = RecipeResult(top_module=top_module, clock_target_ns=clock_period_ns)
    ensure_tool_path()

    if not generated_src_files:
        r.stderr = "no generated_src_files -- elaborate with collect_src=True"
        r.returncode = 2
        return r
    if not os.path.isfile(liberty):
        r.stderr = f"liberty not found: {liberty}"
        r.returncode = 2
        return r

    work = os.path.join(SYNTH_WORK_DIR, "recipe")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    # elaborate() returns a LIST of (filename, contents) pairs, not a mapping
    # (synth_node.py:300 iterates it that way). Accept either, and keep only
    # Verilog: the pair list also carries .top.mems.conf, which is
    # memory-compiler input and would make yosys choke.
    if isinstance(generated_src_files, dict):
        pairs = list(generated_src_files.items())
    else:
        pairs = list(generated_src_files)
    sources = {os.path.basename(n): c for n, c in pairs
               if n.endswith((".v", ".sv"))}
    # TestDriver and the Verilator harness are simulation collateral -- they
    # instantiate $fatal/$fdisplay and are not synthesizable. plusarg_reader is
    # dropped too; it is re-introduced as a blackbox stub below.
    for bad in [k for k in sources
                if k in ("TestDriver.v", "TestDriver.sv") or "plusarg" in k.lower()]:
        del sources[bad]

    r.staged_files = len(sources)
    cone = gemmini_cone(sources, top_module)
    if not cone:
        r.stderr = (f"no module {top_module!r} found in "
                    f"{len(sources)} generated Verilog files")
        r.returncode = 2
        return r
    r.cone_files = len(cone)

    # Recipe blocker 2: rewrite the assignment patterns yosys cannot parse.
    # Safe for firtool output specifically -- it only ever emits flat element
    # lists, so "= '{a, b, c}" and "= {a, b, c}" have identical width and bit
    # order. Done on COPIES; the originals are untouched.
    staged: list[str] = []
    for name in sorted(cone):
        text = sources[name]
        if _ASSIGN_PATTERN_RE.search(text):
            text = _ASSIGN_PATTERN_RE.sub("= {", text)
            r.rewritten_files += 1
        path = os.path.join(work, os.path.basename(name))
        with open(path, "w") as f:
            f.write(text)
        staged.append(path)

    stub_path = os.path.join(work, "_stubs.v")
    with open(stub_path, "w") as f:
        f.write(_STUBS)

    # Recipe blocker 4: the SRAM macros, read as blackboxes.
    #
    # ...and they must NOT also be read as source. Reading `.top.mems.v` with
    # `-lib` declares the macros as blackboxes, but the staged loop below
    # re-reads every staged file with `-sv`, and the full source overrides the
    # blackbox -- so yosys synthesises the scratchpad into FLIP-FLOPS.
    # Measured: 12,323,033 cells / 24.5 mm2, against ~1.4M / 1.71 mm2 when the
    # macros stay black. Sec 9f records this fix; it was never actually in the
    # code (the third documented-but-unimplemented fix found today, after the
    # yosys `'{` rewrite and the loop calling synth_node at all).
    mems = [n for n in sources if n.endswith(".top.mems.v")]
    mem_path = None
    if mems:
        mem_path = os.path.join(work, os.path.basename(mems[0]))
        with open(mem_path, "w") as f:
            f.write(sources[mems[0]])
        _mem_bases = {os.path.basename(m) for m in mems}
        _before = len(staged)
        staged = [p_ for p_ in staged
                  if os.path.basename(p_) not in _mem_bases]
        if len(staged) != _before:
            logger.info("T3: %d memory file(s) held as blackboxes, not source",
                        _before - len(staged))

    netlist = os.path.join(work, f"{top_module}.mapped.v")
    script = [f"read_liberty -lib {liberty}",
              f"read_verilog -sv {stub_path}"]
    if mem_path:
        script.append(f"read_verilog -lib {mem_path}")
    script += [f"read_verilog -sv {p}" for p in staged]
    script += [
        f"hierarchy -check -top {top_module}",
        # NOT -flatten: with it, abc is SIGKILLed on this 30 GB host (blocker 5)
        f"synth -top {top_module}",
        # yosys emits surviving assert blocks that OpenSTA cannot parse
        "chformal -remove",
        f"dfflibmap -liberty {liberty}",
        f"abc -fast -liberty {liberty}",
        "opt_clean -purge",
        f"write_verilog -noattr {netlist}",
        f"stat -liberty {liberty}",
    ]
    ys = os.path.join(work, "synth.ys")
    with open(ys, "w") as f:
        f.write("\n".join(script) + "\n")

    logger.info("T3-recipe: %d/%d files in the %s cone, %d rewritten",
                r.cone_files, r.staged_files, top_module, r.rewritten_files)

    proc = subprocess.run([_tool_path("yosys"), "-s", ys],
                          capture_output=True, text=True,
                          timeout=timeout_seconds)
    r.returncode = proc.returncode
    if proc.returncode != 0 or not os.path.isfile(netlist):
        r.stderr = (proc.stdout + proc.stderr)[-4000:]
        return r

    (r.area_um2, r.cell_count,
     r.seq_cell_count, r.cells_by_type) = parse_synth_stat(proc.stdout)

    # --- timing + power, both off the mapped netlist -----------------------
    #
    # The clock lookup has FALLBACKS and that is not cosmetic. The previous
    # script did `create_clock ... [get_ports clock]` bare: when no port of
    # that exact name exists, OpenSTA errors and the script dies BEFORE
    # report_checks and report_power ever run. The symptom is not an error in
    # the result -- it is `worst_slack_ns = None`, `fmax = None`, and a power
    # figure parsed out of whatever text happened to be on stdout. Measured
    # 2026-09-23: four different netlists (2,102,526 to 2,140,427 cells) all
    # reported power_total_w = 630.0, a number that is both design-invariant
    # and ~3 orders of magnitude too large for a Gemmini tile.
    #
    # Activity annotation decides whether the power number means anything
    # about THIS workload. Priority: a real VCD, else a measured global toggle
    # rate, else OpenSTA's default. Whichever ran is recorded on the result.
    # -input, NOT -global. This distinction is the whole correctness of the
    # number. `-global` forces ONE toggle rate onto every net in the design;
    # `-input` annotates the primary inputs and lets OpenSTA PROPAGATE through
    # the logic, so internal nets get an activity derived from their gate
    # functions and logic depth (and naturally decays with depth).
    #
    # Measured 2026-09-24, and this was my error: a global 0.307 -- which is
    # the MAC ARRAY UTILISATION, a datapath figure -- applied to all 2,102,526
    # nets gave 9.82 W, i.e. 4.08 W/mm^2 against a 0.1-0.5 W/mm^2 norm for this
    # class of design. The utilisation number was fine; forcing it onto the
    # control logic, DMA, TLB and clock tree was not.
    sweep = list(activity_sweep) if activity_sweep else []
    if not sweep:
        if vcd_path and os.path.isfile(vcd_path):
            sweep = [("vcd", None)]
        elif activity is not None:
            sweep = [("input", float(activity))]
        else:
            sweep = [("default", None)]

    def _annot(mode, val):
        if mode == "vcd":
            return f"read_vcd {vcd_path}"
        if mode == "input":
            return f"set_power_activity -input -activity {val:.6f} -duty 0.5"
        if mode == "global":
            return f"set_power_activity -global -activity {val:.6f} -duty 0.5"
        return "# default OpenSTA toggle rates"

    activity_tcl = "\n".join(
        f'puts "SPARSECRAFT_PWR_MODE {m} {v if v is not None else -1}"\n'
        f'{_annot(m, v)}\nreport_power -digits 6' for m, v in sweep)
    r.power_activity_source = sweep[0][0]
    r.power_activity = sweep[0][1]

    sta_tcl = os.path.join(work, "sta.tcl")
    with open(sta_tcl, "w") as f:
        f.write(f"""\
read_liberty {liberty}
read_verilog {netlist}
link_design {top_module}
set clk_ports [get_ports -quiet clock]
if {{ [llength $clk_ports] == 0 }} {{ set clk_ports [get_ports -quiet clk] }}
if {{ [llength $clk_ports] == 0 }} {{ set clk_ports [get_ports -quiet clock_uncore] }}
if {{ [llength $clk_ports] == 0 }} {{
  create_clock -name core_clk -period {clock_period_ns}
  puts "SPARSECRAFT_STA no clock port found; used a virtual clock"
}} else {{
  create_clock -name core_clk -period {clock_period_ns} $clk_ports
  puts "SPARSECRAFT_STA clocked $clk_ports"
}}
set_propagated_clock [all_clocks]
report_checks -path_delay max -digits 4
report_worst_slack -max -digits 4
{activity_tcl}
exit
""")
    sta = subprocess.run([_tool_path("sta"), "-no_init", "-exit", sta_tcl],
                         capture_output=True, text=True, timeout=timeout_seconds)
    sta_out = sta.stdout + sta.stderr
    # Kept on disk AND on the result: a power number whose STA log has been
    # thrown away cannot be audited later.
    with open(os.path.join(work, "sta.log"), "w") as f:
        f.write(sta_out)
    r.sta_tail = sta_out[-4000:]
    r.worst_slack_ns = parse_worst_slack(sta_out)
    if r.worst_slack_ns is not None:
        achieved = clock_period_ns - r.worst_slack_ns
        r.fmax_mhz = 1000.0 / achieved if achieved > 0 else None
    # Each mode printed a SPARSECRAFT_PWR_MODE marker before its own
    # report_power table, so the sections split cleanly and every number is
    # attributable to the annotation that produced it.
    r.power_sweep = []
    if "SPARSECRAFT_PWR_MODE" in sta_out:
        chunks = sta_out.split("SPARSECRAFT_PWR_MODE ")[1:]
        for ch in chunks:
            head, _, body = ch.partition("\n")
            bits = head.split()
            mode = bits[0] if bits else "?"
            try:
                act = float(bits[1]) if len(bits) > 1 else -1.0
            except ValueError:
                act = -1.0
            pp = _parse_power(body)
            if pp:
                r.power_sweep.append({"mode": mode,
                                      "activity": None if act < 0 else act,
                                      **pp})
        if r.power_sweep:
            first = r.power_sweep[0]
            r.power_internal_w = first["internal"]
            r.power_switching_w = first["switching"]
            r.power_leakage_w = first["leakage"]
            r.power_total_w = first["total"]
            r.success = r.area_um2 > 0
            return r

    p = _parse_power(sta_out)
    if p:
        r.power_internal_w = p["internal"]
        r.power_switching_w = p["switching"]
        r.power_leakage_w = p["leakage"]
        r.power_total_w = p["total"]
    else:
        # Non-fatal: area is the number of record, power is a bonus.
        r.stderr = ("report_power produced no Total row; area and timing are "
                    "still valid\n") + sta_out[-1500:]

    r.success = r.area_um2 > 0
    return r
