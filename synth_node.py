"""N52 / T3 -- physical synthesis. The tier that turns area and Fmax from
predictions into measurements.

Until now the objective's third and fourth components (area, clock period) came
out of ``t1_model``'s closed-form equations. This node replaces them with
numbers a synthesis tool produced from the same RTL the simulator ran, which is
the difference between a modelled Pareto front and a measured one.

The flow is entirely open source and runs on this host with no licences:

    generated Verilog  ->  hammer-vlsi syn  ->  yosys  ->  abc  ->  NanGate45
                                                  |
                                                  +->  <top>.mapped.v
                                                  +->  <top>.synth_stat.txt   (area)
                                       openroad/OpenSTA on the mapped netlist  (slack -> Fmax)

Two tools, two numbers:

* **Area** comes from yosys' own ``stat -liberty`` pass, which sums the Liberty
  cell areas of the mapped netlist. That is post-synthesis *cell* area -- it
  excludes routing and any macro the netlist blackboxes, so it is a lower bound
  on die area and is reported as such.
* **Fmax** does NOT come from yosys. ``abc -D <period>`` only *targets* a period;
  it reports no slack. So after synthesis this node links the mapped netlist
  against the same Liberty in OpenSTA (shipped inside the ``openroad`` binary)
  and reads the worst setup slack, which gives a real critical path.

Both are run on a worker advertising the ``hammer`` resource. The Verilog
arrives BY VALUE in ``generated_src_files`` (the ``(filename, contents)`` pairs
``ChiselBuildNode(collect_generated_src=True)`` produces), because the elaborating
container and the synthesizing container do not share a filesystem.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction
from chia.vlsi.hammer import HammerNode

from constants import (PDK_ROOT, R_HAMMER, SYNTH_CLOCK_NS, SYNTH_TECHNOLOGY,
                       SYNTH_TIMEOUT_S, SYNTH_TOP_MODULE, SYNTH_WORK_DIR)

logger = logging.getLogger("sparsecraft.synth")


@dataclass
class SynthResult:
    """One T3 measurement. ``success`` False means the front must not admit it."""
    success: bool
    top_module: str = ""
    technology: str = ""
    # What we asked abc to hit. Slack is measured against this, so Fmax is
    # 1 / (target - slack) and is meaningful whether slack is positive or not.
    clock_target_ns: float = 0.0
    # Post-synthesis standard-cell area, um^2. Excludes routing and blackboxed
    # macros -- a lower bound on die area, not an estimate of it.
    area_um2: float = 0.0
    cell_count: int = 0
    seq_cell_count: int = 0
    # Worst setup slack from OpenSTA, ns. Negative means the target was missed.
    worst_slack_ns: float | None = None
    fmax_mhz: float | None = None
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    # Small text reports, kept for the archive. Netlists are NOT carried here:
    # a mapped Gemmini netlist is tens of MB and would ride the object store on
    # every iteration.
    reports: dict[str, str] = field(default_factory=dict)
    cells_by_type: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Technology table.
#
# Everything technology-specific lives here so that adding a node is a dict
# entry, not a code change. `install_key` is the hammer config key the plugin
# resolves its library paths against; `liberty` is the file OpenSTA links the
# mapped netlist against and MUST be the same corner yosys mapped to, or the
# slack number is fiction.
# ---------------------------------------------------------------------------
TECHNOLOGIES: dict[str, dict] = {
    "nangate45": {
        "plugin": "hammer.technology.nangate45",
        "install_key": "technology.nangate45.install_dir",
        # The nangate45 DIRECTORY, not its parent: hammer treats the install id
        # as a path prefix to strip, so a library declared as
        # "nangate45/lib/x.lib" resolves to "<install_dir>/lib/x.lib".
        "install_dir": os.path.join(PDK_ROOT, "nangate45"),
        "liberty": os.path.join(
            PDK_ROOT, "nangate45", "lib", "NangateOpenCellLibrary_typical.lib"),
        # hammer's yosys plugin emits `techmap -map <latch_map_file>`
        # unconditionally (its defaults.yml declares the key as null and the
        # plugin checks has_setting, not the value), so a technology that does
        # not override it dies on `techmap -map None`. nangate45 does not
        # override it; the image ships one.
        "latch_map": os.path.join(PDK_ROOT, "nangate45_latch_map.v"),
        # Only needed by the openroad fallback in _run_sta; standalone OpenSTA
        # reads Liberty and Verilog and nothing else.
        "lefs": [os.path.join(PDK_ROOT, "nangate45", "lef",
                              "NangateOpenCellLibrary.tech.lef"),
                 os.path.join(PDK_ROOT, "nangate45", "lef",
                              "NangateOpenCellLibrary.macro.lef")],
        "node_nm": 45,
    },
    "sky130": {
        "plugin": "hammer.technology.sky130",
        "install_key": "technology.sky130.sky130A",
        "install_dir": os.path.join(PDK_ROOT, "sky130A"),
        "liberty": os.path.join(
            PDK_ROOT, "sky130A", "libs.ref", "sky130_fd_sc_hd", "lib",
            "sky130_fd_sc_hd__tt_025C_1v80.lib"),
        # sky130's own plugin sets latch_map_file; leave it alone.
        "latch_map": None,
        "lefs": [os.path.join(PDK_ROOT, "sky130A", "libs.ref", "sky130_fd_sc_hd",
                              "techlef", "sky130_fd_sc_hd__nom.tlef"),
                 os.path.join(PDK_ROOT, "sky130A", "libs.ref", "sky130_fd_sc_hd",
                              "lef", "sky130_fd_sc_hd.lef")],
        "node_nm": 130,
    },
}


# Where the synthesis image puts things. Kept here rather than read from the
# environment so the node works on a worker whose shell never sourced anything.
_EDA_PREFIX = os.environ.get("SPARSECRAFT_EDA_PREFIX", "/home/ray/eda")
_HAMMER_BIN_DEFAULT = "/home/ray/chipyard/.conda-env/bin/hammer-vlsi"


def ensure_tool_path() -> str:
    """Put the EDA binaries and hammer's own bin dir on PATH, and return PATH.

    hammer-vlsi does not run standalone: it shells out to `hammer-shell` from
    the same bin directory and aborts with "hammer-shell does not appear to be
    on the path" if it is absent. Invoking hammer by absolute path is therefore
    not enough.

    This could be left to the worker's shell -- cluster.yaml does source
    chipyard's env.sh in `worker_env_commands` -- but a node that only works
    when its container happened to be launched a particular way is a node that
    fails mysteriously the first time someone runs it another way. So the node
    guarantees its own PATH, idempotently, and the shell setup becomes an
    optimisation rather than a requirement.
    """
    hammer_bin = os.environ.get("SPARSECRAFT_HAMMER_BIN", _HAMMER_BIN_DEFAULT)
    wanted = [os.path.dirname(hammer_bin)]
    wanted += [os.path.join(_EDA_PREFIX, tool, "bin")
               for tool in ("yosys", "openroad", "klayout")]
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep)
    for d in wanted:
        if d and os.path.isdir(d) and d not in parts:
            parts.append(d)
    os.environ["PATH"] = os.pathsep.join(parts)
    return os.environ["PATH"]


def _tool_path(name: str) -> str:
    """Absolute path to an EDA binary inside the synthesis image.

    Resolution order is deliberate: PATH first (so a differently-built image
    still works), then the layout this repo's Dockerfile installs. Returning
    the bare name as a last resort lets hammer produce its own, clearer error.
    """
    found = shutil.which(name)
    if found:
        return found
    prefix = os.environ.get("SPARSECRAFT_EDA_PREFIX", "/home/ray/eda")
    candidate = os.path.join(prefix, name, "bin", name)
    return candidate if os.path.isfile(candidate) else name


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------
_AREA_RE = re.compile(r"Chip area for (?:top )?module '\\?([^']+)':\s*([0-9.]+)")
_CELLS_RE = re.compile(r"Number of cells:\s*(\d+)")
_CELL_LINE_RE = re.compile(r"^\s{5,}(\S+)\s+(\d+)\s*$")
# Sequential cells in NanGate45 are DFF*/SDFF*; in sky130 they are *__dfxtp_ etc.
_SEQ_RE = re.compile(r"(^|_)(s?dff|dlh|dlx|dfxtp|dfrtp|dfstp|edfxtp)", re.I)


def parse_synth_stat(text: str) -> tuple[float, int, int, dict[str, int]]:
    """Pull area and the cell histogram out of yosys' ``stat -liberty`` output.

    yosys prints one ``=== <module> ===`` block per module in the hierarchy and
    a final whole-design block. We take the LAST area line, which is the
    flattened total; taking the first would report a single leaf module.
    """
    areas = _AREA_RE.findall(text)
    area = float(areas[-1][1]) if areas else 0.0

    counts = _CELLS_RE.findall(text)
    cells = int(counts[-1]) if counts else 0

    # The histogram lines of the last block only -- earlier blocks are
    # sub-modules and would double-count.
    tail = text.rsplit("===", 2)[-1] if "===" in text else text
    by_type: dict[str, int] = {}
    for line in tail.splitlines():
        m = _CELL_LINE_RE.match(line)
        if m and not m.group(1).startswith("$"):
            by_type[m.group(1)] = by_type.get(m.group(1), 0) + int(m.group(2))
    seq = sum(n for name, n in by_type.items() if _SEQ_RE.search(name))
    return area, cells, seq, by_type


_SLACK_RE = re.compile(r"^\s*worst slack\s+(-?[0-9.eE+]+)", re.M | re.I)


def parse_worst_slack(text: str) -> float | None:
    m = _SLACK_RE.search(text)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Top-module resolution
# ---------------------------------------------------------------------------
_MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)", re.M)
_INSTANCE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\([^)]*\)\s*)?"
                          r"[A-Za-z_][A-Za-z0-9_$]*\s*\(", re.M)


def resolve_top_module(sources: dict[str, str], hint: str = "Gemmini") -> tuple[str, list[str]]:
    """Find the outermost module matching *hint* in the generated RTL.

    firtool renames modules between chipyard versions (``Gemmini``,
    ``Gemmini_1``, ``LazyRoCCGemmini``...), so pinning a literal name is a
    latent break. Instead: collect every declared module whose name contains the
    hint, drop the ones that are instantiated by another module, and take what
    is left -- the root of that subtree.

    Returns ``(chosen, candidates)``. ``chosen`` is "" when nothing matched, and
    *candidates* is always populated so the caller can report what it did see.
    """
    declared: set[str] = set()
    instantiated: set[str] = set()
    for text in sources.values():
        declared.update(_MODULE_RE.findall(text))
        instantiated.update(_INSTANCE_RE.findall(text))

    candidates = sorted(m for m in declared if hint.lower() in m.lower())
    if not candidates:
        return "", sorted(declared)

    roots = [m for m in candidates if m not in instantiated]
    if roots:
        # Shortest name first: ``Gemmini`` beats ``Gemmini_inner_1``, and among
        # equals the sort is alphabetical so the choice is deterministic.
        roots.sort(key=lambda m: (len(m), m))
        return roots[0], candidates
    candidates.sort(key=lambda m: (len(m), m))
    return candidates[0], candidates


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------
def _yosys_sanitize(src: str) -> str:
    """Rewrite the one SystemVerilog construct yosys 0.38 cannot parse.

    firtool emits packed-array assignment patterns:

        wire [3:0][3:0] _GEN_4 = '{4'hC, 4'h8, 4'hE, 4'h6};

    and yosys 0.38 rejects the `'{` with "syntax error, unexpected OP_CAST"
    (measured: TLAtomicAutomata_pbus.sv:258, which killed the whole T3 tier --
    no yosys log, no Gemmini.mapped.v, and hammer reported only the missing
    output file). The recipe notes in docker/yosys_gemmini_recipe.md describe
    this rewrite, but it was never actually in the staging path.

    For a PACKED array the pattern is positionally identical to a
    concatenation -- `[3:0][3:0]` is 16 contiguous bits and the four 4-bit
    values appear in the same order -- so `= '{...}` -> `= {...}` preserves
    semantics exactly. It is applied ONLY to the staged synthesis copy; the
    simulated RTL is untouched, so this cannot affect any measured cycle
    count, only the netlist handed to yosys.
    """
    # Whitespace-aware: firtool also emits the pattern with the `'{` on the
    # line AFTER the `=`, e.g. TLROM.sv:22
    #     wire [511:0][63:0] _GEN =
    #         '{64'h0,
    # A literal "= '{" match misses that one and yosys dies 478 files later.
    import re as _re
    return _re.sub(r"=(\s*)'\{", r"=\1{", src)


def _resolve_synthesis_ifdefs(src: str) -> str:
    """Select the `SYNTHESIS branch, as defining SYNTHESIS would.

    Chipyard guards simulation-only bodies with `ifdef SYNTHESIS. hammer's
    yosys flow passes no +define+SYNTHESIS, so yosys would take the SIMULATION
    branch -- $value$plusargs and friends -- in modules that must synthesise.
    plusarg_reader is the load-bearing case: its SYNTHESIS branch is a plain
    `assign out = DEFAULT`.

    Only SYNTHESIS conditionals are resolved. Every other `ifdef is copied
    through untouched, and tracked only so a nested `else/`endif is attributed
    to the right directive.
    """
    out: list[str] = []
    stack: list[tuple] = []          # ("synth", emitting) | ("other", None)
    for line in src.splitlines(keepends=True):
        st = line.strip()
        if st.startswith("`ifdef SYNTHESIS") or st.startswith("`ifndef SYNTHESIS"):
            stack.append(("synth", st.startswith("`ifdef")))
            continue
        if st.startswith("`ifdef") or st.startswith("`ifndef"):
            stack.append(("other", None))
            out.append(line)
            continue
        if st.startswith("`else") and stack and stack[-1][0] == "synth":
            stack[-1] = ("synth", not stack[-1][1])
            continue
        if st.startswith("`endif") and stack and stack[-1][0] == "synth":
            stack.pop()
            continue
        if st.startswith("`endif"):
            if stack and stack[-1][0] == "other":
                stack.pop()
            out.append(line)
            continue
        if st.startswith("`else"):
            out.append(line)
            continue
        if all(kind != "synth" or emit for kind, emit in stack):
            out.append(line)
    return "".join(out)


@ChiaFunction(resources={R_HAMMER: 1})

def synthesize(
    generated_src_files: list[tuple[str, str]],
    top_module: str = SYNTH_TOP_MODULE,
    clock_period_ns: float = SYNTH_CLOCK_NS,
    technology: str = SYNTH_TECHNOLOGY,
    obj_dir: str = SYNTH_WORK_DIR,
    timeout_seconds: int = SYNTH_TIMEOUT_S,
    keep_netlist: bool = False,
    top_hint: str = "Gemmini",
) -> SynthResult:
    """Synthesize one design and measure its area and critical path."""
    tech = TECHNOLOGIES.get(technology)
    if tech is None:
        return SynthResult(success=False, top_module=top_module,
                           technology=technology, returncode=-2,
                           stderr=f"unknown technology {technology!r}; "
                                  f"known: {sorted(TECHNOLOGIES)}")
    if not os.path.isdir(tech["install_dir"]):
        return SynthResult(success=False, top_module=top_module,
                           technology=technology, returncode=-2,
                           stderr=f"PDK missing on this worker: "
                                  f"{tech['install_dir']}. Is this the "
                                  f"sparsecraft-synth image?")

    ensure_tool_path()
    obj_dir = os.path.abspath(obj_dir)
    shutil.rmtree(obj_dir, ignore_errors=True)
    src_dir = os.path.join(obj_dir, "input_src")
    os.makedirs(src_dir, exist_ok=True)

    # Stage the RTL. Only .v/.sv -- the pair list also carries .top.mems.conf,
    # which is memory-compiler input, not Verilog, and yosys would choke on it.
    staged: list[str] = []
    for filename, contents in generated_src_files:
        if not filename.endswith((".v", ".sv")):
            continue
        # TestDriver and the Verilator-only harness are simulation collateral;
        # they instantiate $fatal/$fdisplay and are not synthesizable.
        base = os.path.basename(filename)
        if base in ("TestDriver.v", "TestDriver.sv"):
            continue
        # plusarg_reader is NOT dropped any more. It is instantiated by the
        # TLMonitor assertion modules, so excluding it made `hierarchy -check
        # -top Gemmini` fail with "Module \\plusarg_reader ... is not part of
        # the design" after every file had already parsed. Its `ifdef
        # SYNTHESIS branch is a plain `assign out = DEFAULT`, which
        # _resolve_synthesis_ifdefs selects below.
        if base.lower().startswith("plusargtimeout"):
            continue
        # Behavioural harness models. Not synthesisable, and irrelevant anyway
        # because the top module is Gemmini, not TestHarness -- but yosys
        # parses every staged file before elaborating the top, so one
        # unparseable model kills the whole T3 tier.
        #
        # ClockSourceAtFreqMHz.v declares `timeunit 1ns/1ps;` INSIDE the module
        # (yosys 0.38: "syntax error, unexpected TOK_TIME_SCALE") and drives
        # its output with `always #(PERIOD/2.0)`. Note the GenericDigital*IOCell
        # models are NOT excluded: their `timescale is file-scope, which yosys
        # accepts, and EICG_wrapper is a real clock-gating latch.
        if base in ("ClockSourceAtFreqMHz.v", "SimDRAM.v", "SimJTAG.v",
                    "SimUART.v", "SimSerial.v"):
            continue
        path = os.path.join(src_dir, base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(_resolve_synthesis_ifdefs(_yosys_sanitize(contents)))
        staged.append(path)
    if not staged:
        return SynthResult(success=False, top_module=top_module,
                           technology=technology, returncode=-2,
                           stderr="no .v/.sv in generated_src_files -- was the "
                                  "build run with collect_generated_src=True?")

    if top_module in ("auto", "", None):
        sources = {name: text for name, text in generated_src_files
                   if name.endswith((".v", ".sv"))}
        top_module, candidates = resolve_top_module(sources, hint=top_hint)
        if not top_module:
            return SynthResult(
                success=False, top_module="auto", technology=technology,
                returncode=-2,
                stderr=(f"no module matching {top_hint!r} in {len(sources)} "
                        f"generated files. Declared modules (first 40): "
                        f"{candidates[:40]}"))
        logger.info("T3: resolved top module %r from %d candidates: %s",
                    top_module, len(candidates), candidates[:8])
    logger.info("T3: staged %d RTL files for %s @ %.2f ns (%s)",
                len(staged), top_module, clock_period_ns, technology)

    # --- hammer configs, written per call --------------------------------
    # Not checked-in YAML: the PDK path and the tool paths are properties of
    # the worker's image, and the clock target changes per design point. A
    # static file would have to be templated anyway.
    cfg_dir = os.path.join(obj_dir, "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "vlsi.core.technology": tech["plugin"],
        tech["install_key"]: tech["install_dir"],
        "vlsi.core.synthesis_tool": "hammer.synthesis.yosys",
        "vlsi.core.par_tool": "hammer.par.openroad",
        "synthesis.yosys.yosys_bin": _tool_path("yosys"),
        "par.openroad.openroad_bin": _tool_path("openroad"),
        "par.openroad.klayout_bin": _tool_path("klayout"),
        "vlsi.core.max_threads": int(os.environ.get("SPARSECRAFT_SYN_THREADS", "8")),
        "vlsi.inputs.clocks": [{
            "name": "clock",
            "period": f"{clock_period_ns}ns",
            # 5% of the target, the usual first-pass number. Held constant
            # across design points so it never biases a comparison.
            "uncertainty": f"{clock_period_ns * 0.05:.4f}ns",
        }],
        "synthesis.inputs": {
            "top_module": top_module,
            "input_files": staged,
        },
    }
    if tech.get("latch_map") and os.path.isfile(tech["latch_map"]):
        cfg["synthesis.yosys.latch_map_file"] = tech["latch_map"]
    cfg_path = os.path.join(cfg_dir, "sparsecraft-syn.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=1)

    # Called in-process via the static member rather than .chia_remote: we are
    # already on the worker that owns obj_dir, and a local call requests no
    # resources and does not re-dispatch. Same pattern as
    # examples/sky130_vlsi/hammer_syn_node.
    hammer_bin = os.environ.get("SPARSECRAFT_HAMMER_BIN", _HAMMER_BIN_DEFAULT)
    if not os.path.isfile(hammer_bin):
        hammer_bin = shutil.which("hammer-vlsi") or "hammer-vlsi"

    run = HammerNode.run(
        "syn",
        configs=[cfg_path],
        obj_dir=obj_dir,
        hammer_bin=hammer_bin,
        timeout_seconds=timeout_seconds,
    )

    result = SynthResult(
        success=False,
        top_module=top_module,
        technology=technology,
        clock_target_ns=clock_period_ns,
        returncode=run.returncode,
        stdout=run.stdout[-20000:],
        stderr=run.stderr[-20000:],
    )
    if not run.success:
        logger.error("T3: hammer syn failed rc=%s: %s",
                     run.returncode, (run.stderr or "")[-600:])
        return result

    # --- area, from yosys' own stat pass ---------------------------------
    run_dir = os.path.join(obj_dir, "syn-rundir")
    stat_path = os.path.join(run_dir, f"{top_module}.synth_stat.txt")
    if os.path.isfile(stat_path):
        with open(stat_path, errors="replace") as f:
            stat_text = f.read()
        (result.area_um2, result.cell_count,
         result.seq_cell_count, result.cells_by_type) = parse_synth_stat(stat_text)
        result.reports["synth_stat.txt"] = stat_text[-60000:]
    else:
        logger.warning("T3: no %s -- area will read 0", stat_path)

    check_path = os.path.join(run_dir, f"{top_module}.synth_check.rpt")
    if os.path.isfile(check_path):
        with open(check_path, errors="replace") as f:
            result.reports["synth_check.rpt"] = f.read()[-20000:]

    # --- Fmax, from OpenSTA on the mapped netlist ------------------------
    netlist = os.path.join(run_dir, f"{top_module}.mapped.v")
    if os.path.isfile(netlist):
        sta = _run_sta(netlist, tech["liberty"], top_module, clock_period_ns,
                       obj_dir, timeout_seconds=min(timeout_seconds, 3600),
                       lefs=tech.get("lefs"))
        result.reports["sta.log"] = sta[-40000:]
        result.worst_slack_ns = parse_worst_slack(sta)
        if result.worst_slack_ns is not None:
            achieved = clock_period_ns - result.worst_slack_ns
            result.fmax_mhz = (1000.0 / achieved) if achieved > 0 else None
        if keep_netlist:
            with open(netlist, errors="replace") as f:
                result.reports["mapped.v"] = f.read()
    else:
        logger.warning("T3: no mapped netlist at %s -- no Fmax", netlist)

    # A run that produced no area measured nothing, whatever hammer's exit code.
    result.success = result.area_um2 > 0.0
    logger.info("T3: %s area=%.1f um2 cells=%d seq=%d slack=%s fmax=%s",
                top_module, result.area_um2, result.cell_count,
                result.seq_cell_count, result.worst_slack_ns, result.fmax_mhz)
    return result


# The timing script. Deliberately tool-agnostic: standalone OpenSTA and the STA
# engine inside the openroad binary accept the same commands, so one script
# serves both and the only difference is whether LEFs have to be read first.
_STA_TCL = """\
{lef_reads}read_liberty {liberty}
read_verilog {netlist}
link_design {top}

set clk_ports [get_ports -quiet {{clock}}]
if {{ [llength $clk_ports] == 0 }} {{ set clk_ports [get_ports -quiet {{clk}}] }}
if {{ [llength $clk_ports] == 0 }} {{ set clk_ports [get_ports -quiet {{clock_uncore}}] }}
if {{ [llength $clk_ports] == 0 }} {{
  # No recognizable clock port: create a virtual clock so combinational paths
  # are still timed rather than the run reporting a vacuous zero slack.
  create_clock -name core_clk -period {period}
  puts "SPARSECRAFT_STA no clock port found; used a virtual clock"
}} else {{
  create_clock -name core_clk -period {period} $clk_ports
}}
set_propagated_clock [all_clocks]

report_checks -path_delay max -format summary -digits 4
report_worst_slack -max -digits 4
report_tns -digits 4
exit
"""


def _run_sta(netlist: str, liberty: str, top: str, period_ns: float,
             obj_dir: str, timeout_seconds: int = 3600,
             lefs: list[str] | None = None) -> str:
    """Time the mapped netlist with OpenSTA.

    Two engines can do this and the image has both. Standalone ``sta`` is
    preferred because it works from Liberty and Verilog alone. The ``openroad``
    binary embeds the same engine but builds an OpenDB database first, so
    ``read_verilog`` there fails with "no technology has been read" unless the
    LEFs are loaded -- hence the fallback reads them.
    """
    if not os.path.isfile(liberty):
        return f"SPARSECRAFT_STA liberty missing: {liberty}"

    sta_bin = _tool_path("sta")
    use_openroad = not os.path.isfile(sta_bin)
    if use_openroad:
        lef_reads = "".join(f"read_lef {p}\n" for p in (lefs or [])
                            if os.path.isfile(p))
        cmd_head = [_tool_path("openroad"), "-no_init", "-exit"]
    else:
        lef_reads = ""
        cmd_head = [sta_bin, "-no_init", "-exit"]

    script = os.path.join(obj_dir, "sparsecraft_sta.tcl")
    with open(script, "w") as f:
        f.write(_STA_TCL.format(lef_reads=lef_reads, liberty=liberty,
                                netlist=netlist, top=top, period=period_ns))
    try:
        r = subprocess.run(cmd_head + [script], capture_output=True, text=True,
                           timeout=timeout_seconds, cwd=obj_dir)
    except subprocess.TimeoutExpired:
        return f"SPARSECRAFT_STA timed out after {timeout_seconds}s"
    except FileNotFoundError as e:
        return f"SPARSECRAFT_STA no STA engine found: {e}"
    return (r.stdout or "") + "\n" + (r.stderr or "")
