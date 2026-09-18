"""The CHIA nodes: N13 apply, N30/N31 elaborate, N32 software build, N50 simulate.

Every node here is a ``@ChiaFunction`` whose ``resources={...}`` must match a
node type in cluster.yaml. The expensive ones wrap CHIA's existing Chipyard
nodes rather than reimplementing them.

Cache keys are the point of this module. ``ChiselBuildNode.build`` is the
expensive edit (20-40 min), so elaboration is keyed on ``hw_hash`` alone: a
tiling-only mutation reuses the elaborated RTL entirely. The software build is
keyed on ``sw_hash``, which *includes* the hardware hash -- elaboration emits
``gemmini_params.h`` and the kernels include it, so a hardware change
invalidates the software build even when no kernel source changed. (The review's
Sec 3.1 treats these as independent; they are not.)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction
from chia.chipyard.chisel_build_node import ChiselBuildNode
from chia.chipyard.state_def import BuildArtifact, BuildTarget, RunResult
from chia.chipyard.verilator_run_node import VerilatorRunNode

from constants import (BUILD_FRACTION, CHIPYARD_PATH, CONFIG_NAME,
                       CONFIG_PACKAGE, GEMMINI_SW_REL, HARNESS_FILE_REL,
                       PARAMS_FILE_REL, R_CHIPYARD, SIM_WORK_DIR)
from design_state import HARNESS_SCALA, DesignState

logger = logging.getLogger("sparsecraft.nodes")


@dataclass
class ApplyResult:
    ok: bool
    params_path: str
    message: str
    # Repo-relative paths this call created. The caller hands these to
    # t0_legality.check_patch_scope as harness_paths, so scaffolding the
    # harness wrote is not mistaken for an out-of-scope model edit.
    wrote: list = field(default_factory=list)


# --------------------------------------------------------------------------
# N13 -- write the design state into the Chipyard tree.
#
# The scope check runs on the HEAD before this is ever dispatched
# (t0_legality.check_patch_scope). This node writes exactly one file and
# refuses anything else, so the agent has no write path to the source tree.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def apply_design_state(state_json: str, chipyard_path: str = CHIPYARD_PATH) -> ApplyResult:
    """Render SparseCraftParams.scala (and the one-time harness config)."""
    import json
    state = DesignState.from_dict(json.loads(state_json))

    params_path = os.path.join(chipyard_path, PARAMS_FILE_REL)
    harness_path = os.path.join(chipyard_path, HARNESS_FILE_REL)
    for p in (params_path, harness_path):
        os.makedirs(os.path.dirname(p), exist_ok=True)

    wrote = []
    # The harness config is written once and never mutated. Keeping it out of
    # the writable set is what makes the N13 allowlist a single path.
    if not os.path.exists(harness_path):
        with open(harness_path, "w") as f:
            f.write(HARNESS_SCALA)
        wrote.append(HARNESS_FILE_REL)

    with open(params_path, "w") as f:
        f.write(state.to_scala())
    wrote.append(PARAMS_FILE_REL)

    return ApplyResult(ok=True, params_path=params_path, wrote=wrote,
                       message=f"wrote {', '.join(wrote)} "
                               f"for state {state.state_hash()}")


# --------------------------------------------------------------------------
# N30 + N31 -- Chisel elaboration and Verilator build.
# Keyed on hw_hash by the caller via _chia_tag.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def elaborate(state_json: str, chipyard_path: str = CHIPYARD_PATH,
              make_jobs: int = 16, timeout_seconds: int = 5400,
              config_name: str = CONFIG_NAME,
              collect_src: bool = False) -> BuildArtifact:
    """Elaborate the SoC and build the Verilator simulator.

    make_jobs defaults to 16, not nproc: this host has 64 cores but only 30 GB
    of RAM, and a Chisel elaboration peaks at 8-12 GB. Two concurrent
    elaborations at -j16 is the ceiling here.

    ``collect_src`` turns this into the input stage of the T3 synthesis tier: it
    carries every generated .v/.sv back inside the BuildArtifact so a worker in
    a *different* container can synthesize the same RTL this simulator was
    built from. It costs object-store traffic (chipyard's generated collateral
    is O(100 MB) of text), which is why it is off by default and why the
    caller must fold it into the cache tag -- an artifact without sources
    cannot satisfy a request that needs them.

    It does NOT flip chipyard's ENABLE_YOSYS_FLOW. That flag appends
    ``disallowPackedArrays`` to the firtool lowering options, ostensibly so
    yosys can read the output -- but on this design it makes firtool emit no
    Verilog at all (docker/yosys_gemmini_recipe.md, blocker 2). The packed-array
    forms yosys cannot parse are fixed downstream instead.

    The upside of not setting it is that the RTL which gets simulated is now
    byte-identical to the RTL that gets synthesized: one lowering, one flavour,
    so a cache key can never straddle two.
    """
    import json
    state = DesignState.from_dict(json.loads(state_json))

    # NOTE: the params file is NOT regenerated here. The model wrote it via the
    # editor BashTool and the harness already read the state back out of it
    # (read_design_state); rewriting it would silently discard the agentic edit.
    node = ChiselBuildNode(
        chipyard_path=chipyard_path,
        config=config_name,
        config_package=CONFIG_PACKAGE,
        target=BuildTarget.VERILATOR,
        make_jobs=make_jobs,
        timeout_seconds=timeout_seconds,
        # clean=False: the Chisel generator cache (chipyard.jar) is the
        # difference between a 5-minute and a 40-minute rebuild, and our only
        # source edit is one regenerated Scala file that sbt tracks correctly.
        clean=False,
        clean_sim=True,
        collect_generated_src=collect_src,
        # ENABLE_YOSYS_FLOW is deliberately NOT set. It adds firtool's
        # disallowPackedArrays, which on this design makes firtool emit NO
        # Verilog at all: the pass pipeline completes, gen-collateral/ is
        # empty and model_module_hierarchy.json is never written, so make
        # fails. docker/yosys_gemmini_recipe.md blocker 2. The packed-array
        # constructs yosys cannot parse are handled downstream instead, by
        # rewriting "= '{" to "= {" in the staged copies.
        extra_make_args={},
        name=f"sparsecraft-{state.hw_hash()}",
    )
    # node.build(), not node.build(node). ChiselBuildNode.build is
    # `@ChiaFunction def build(self)`, and the decorator's wrapper is a plain
    # function on the class, so attribute access binds it -- passing the node
    # again hands it self twice. Every CHIA example calls it bare
    # (examples/riscv_extensions/nodes.py:120, examples/gem5_align:683).
    return node.build()


# --------------------------------------------------------------------------
# N32 -- cross-compile the attention kernel.
# Runs on the chipyard worker, not riscv_build: it needs gemmini_params.h,
# which elaboration just emitted into the Chipyard tree.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def build_kernel(state_json: str, kernel_source: str, kernel_name: str = "attn_prefill",
                 chipyard_path: str = CHIPYARD_PATH,
                 timeout_seconds: int = 600) -> dict:
    """Compile one baremetal kernel against the freshly generated Gemmini header."""
    import json
    state = DesignState.from_dict(json.loads(state_json))

    work = os.path.join("/tmp", f"sparsecraft_sw_{state.sw_hash()}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, f"{kernel_name}.c")
    with open(src, "w") as f:
        f.write(kernel_source)

    gemmini_sw = os.path.join(chipyard_path, GEMMINI_SW_REL)
    riscv = os.environ.get("RISCV", os.path.join(chipyard_path, ".conda-env", "riscv-tools"))
    gcc = os.path.join(riscv, "bin", "riscv64-unknown-elf-gcc")
    out = os.path.join(work, f"{kernel_name}.riscv")

    # Flags mirror gemmini-rocc-tests/bareMetalC/Makefile. Two details matter:
    # the include root is the gemmini-rocc-tests dir itself (headers are
    # included as "include/gemmini_testutils.h"), and -DBAREMETAL=1 is what
    # keeps the headers off <sys/mman.h>.
    cmd = [
        gcc, "-O2", "-std=gnu99", "-static", "-specs=htif_nano.specs",
        "-DBAREMETAL=1", "-DPREALLOCATE=1", "-mcmodel=medany",
        f"-I{gemmini_sw}", f"-I{gemmini_sw}/riscv-tests",
        f"-I{gemmini_sw}/riscv-tests/env", f"-I{gemmini_sw}/bareMetalC",
        f"-DBLOCK_SIZE={state.block_size}",
        f"-DTILE_M={state.tile_m}", f"-DTILE_N={state.tile_n}", f"-DTILE_K={state.tile_k}",
        src, "-o", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    content = b""
    if r.returncode == 0 and os.path.exists(out):
        with open(out, "rb") as f:
            content = f.read()
    return {"success": r.returncode == 0 and bool(content),
            "binary_name": f"{kernel_name}.riscv",
            "binary_content": content,
            "returncode": r.returncode,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-4000:],
            "cmd": " ".join(cmd)}


# --------------------------------------------------------------------------
# N50 -- T2a functional simulation. Correctness gate + counters.
# --------------------------------------------------------------------------
@ChiaFunction(resources={"verilator_run": 1})
def simulate(artifact: BuildArtifact, kernel: dict,
             timeout_seconds: int = 3600,
             timeout_cycles: int | None = 200_000_000,
             work_dir: str = SIM_WORK_DIR) -> RunResult:
    """Run one kernel ELF on the built simulator; counters arrive on stdout."""
    if not artifact.success:
        raise RuntimeError(f"cannot simulate a failed build: rc={artifact.returncode}")
    if not kernel.get("success"):
        raise RuntimeError(f"cannot simulate a failed kernel build: {kernel.get('stderr','')[:400]}")

    os.makedirs(work_dir, exist_ok=True)
    node = VerilatorRunNode()
    # Same binding rule as elaborate() above: bare call, keyword arguments,
    # matching examples/common/verilator.py:83.
    return node.run(
        artifact=artifact,
        test_binary_content=kernel["binary_content"],
        test_binary_name=kernel["binary_name"],
        work_dir=work_dir,
        timeout_cycles=timeout_cycles,
        timeout_seconds=timeout_seconds,
        # verbose=False: the commit-log trace is enormous and we only need the
        # kernel's own printf output.
        verbose=False,
        cleanup_task_dir=True,
    )


# --------------------------------------------------------------------------
# Read the design state back OUT of the tree the model edited.
#
# The model writes Scala; T0, T1, the cache keys and the archive descriptor all
# need the typed vector. So the harness parses the generated params file rather
# than trusting anything the model reports about its own edit -- the same
# principle as recomputing status from measured results.
# --------------------------------------------------------------------------
_FIELD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,]+?),?\s*(?://.*)?$", re.M)

_SCALA_TO_FIELD = {
    "meshRows": "meshRows", "meshColumns": "meshColumns",
    "tileRows": "tileRows", "tileColumns": "tileColumns",
    "sp_banks": "sp_banks", "acc_banks": "acc_banks",
    "spad_read_delay": "spad_read_delay", "acc_latency": "acc_latency",
    "max_in_flight_mem_reqs": "max_in_flight_mem_reqs",
    "dma_maxbytes": "dma_maxbytes", "dma_buswidth": "dma_buswidth",
    "tlb_size": "tlb_size",
    "ld_queue_length": "ld_queue_length", "st_queue_length": "st_queue_length",
    "ex_queue_length": "ex_queue_length",
    "reservation_station_entries_ld": "reservation_station_entries_ld",
    "reservation_station_entries_st": "reservation_station_entries_st",
    "reservation_station_entries_ex": "reservation_station_entries_ex",
    "num_counter": "num_counter",
}


def parse_params_scala(text: str) -> dict:
    """Extract the typed design state from SparseCraftParams.scala."""
    out: dict = {}
    for key, val in _FIELD_RE.findall(text):
        val = val.strip()
        field = _SCALA_TO_FIELD.get(key)
        if field and val.lstrip("-").isdigit():
            out[field] = int(val)
        elif key == "dataflow":
            out["dataflow"] = val.split(".")[-1].strip()
        elif key in ("has_normalizations", "mvin_scale_shared") and val in ("true", "false"):
            out[key] = (val == "true")
        elif key == "sp_capacity":
            mm = re.search(r"CapacityInKilobytes\((\d+)\)", val)
            if mm:
                out["sp_capacity_kb"] = int(mm.group(1))
        elif key == "acc_capacity":
            mm = re.search(r"CapacityInKilobytes\((\d+)\)", val)
            if mm:
                out["acc_capacity_kb"] = int(mm.group(1))
    # Software-side tiling is carried in a comment header the model must keep.
    for name in ("block_size", "tile_m", "tile_n", "tile_k"):
        mm = re.search(rf"//\s*SPARSECRAFT\s+{name}\s*=\s*(\d+)", text)
        if mm:
            out[name] = int(mm.group(1))
    return out


@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def read_design_state(chipyard_path: str = CHIPYARD_PATH) -> dict:
    """Read and parse SparseCraftParams.scala from the build container."""
    path = os.path.join(chipyard_path, PARAMS_FILE_REL)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return parse_params_scala(f.read())


def state_from_tree(parsed: dict):
    """Build a DesignState from parsed Scala fields, defaulting the rest."""
    if not parsed:
        return None
    return DesignState.from_dict({**DesignState().canonical(), **parsed})
