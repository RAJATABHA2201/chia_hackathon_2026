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
    """
    for line in text.splitlines():
        if line.strip().lower().startswith("total"):
            nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", line)
            if len(nums) >= 4:
                return {"internal": float(nums[0]), "switching": float(nums[1]),
                        "leakage": float(nums[2]), "total": float(nums[3])}
    return {}


@ChiaFunction(resources={R_HAMMER: 1})
def synthesize_recipe(generated_src_files: dict,
                      top_module: str = "Gemmini",
                      clock_period_ns: float = 2.0,
                      liberty: str = NANGATE_LIB,
                      timeout_seconds: int = SYNTH_TIMEOUT_S) -> RecipeResult:
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

    r.staged_files = len(generated_src_files)
    cone = gemmini_cone(generated_src_files, top_module)
    if not cone:
        r.stderr = (f"no module {top_module!r} found in "
                    f"{len(generated_src_files)} generated files")
        r.returncode = 2
        return r
    r.cone_files = len(cone)

    # Recipe blocker 2: rewrite the assignment patterns yosys cannot parse.
    # Safe for firtool output specifically -- it only ever emits flat element
    # lists, so "= '{a, b, c}" and "= {a, b, c}" have identical width and bit
    # order. Done on COPIES; the originals are untouched.
    staged: list[str] = []
    for name in sorted(cone):
        text = generated_src_files[name]
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
    mems = [n for n in generated_src_files if n.endswith(".top.mems.v")]
    mem_path = None
    if mems:
        mem_path = os.path.join(work, os.path.basename(mems[0]))
        with open(mem_path, "w") as f:
            f.write(generated_src_files[mems[0]])

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
    sta_tcl = os.path.join(work, "sta.tcl")
    with open(sta_tcl, "w") as f:
        f.write(f"""\
read_liberty {liberty}
read_verilog {netlist}
link_design {top_module}
create_clock -name clk -period {clock_period_ns} [get_ports clock]
report_checks -path_delay max
report_power
""")
    sta = subprocess.run([_tool_path("sta"), "-no_init", "-exit", sta_tcl],
                         capture_output=True, text=True, timeout=timeout_seconds)
    sta_out = sta.stdout + sta.stderr
    r.worst_slack_ns = parse_worst_slack(sta_out)
    if r.worst_slack_ns is not None:
        achieved = clock_period_ns - r.worst_slack_ns
        r.fmax_mhz = 1000.0 / achieved if achieved > 0 else None
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
