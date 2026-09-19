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

from constants import (BUILD_FRACTION, BUILD_MAKE_JOBS, CHIPYARD_PATH,
                       CONFIG_NAME, CONFIG_PACKAGE, GEMMINI_SW_REL,
                       HARNESS_FILE_REL, PARAMS_FILE_REL, R_CHIPYARD,
                       HARNESS_PATCHED_RTL_REL,
                       RTL_FILES_REL, RTL_PARAMS_FILE_REL, SIM_WORK_DIR,
                       VERILATOR_THREADS)
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

    # SparseCraftRTL.scala: the RTL knobs, regenerated from the design state
    # EVERY iteration. Harness-owned, not in the agent's writable set -- the
    # agent changes the mechanism, the state sets the knobs. If the agent could
    # write this it could flip gate_enable without the design state (and so the
    # cache key, T0 and the archive descriptor) ever knowing.
    rtl_params_path = os.path.join(chipyard_path, RTL_PARAMS_FILE_REL)
    os.makedirs(os.path.dirname(rtl_params_path), exist_ok=True)
    with open(rtl_params_path, "w") as f:
        f.write(state.to_rtl_scala())
    wrote.append(RTL_PARAMS_FILE_REL)

    # T-A scaffolding into PE.scala. Idempotent and re-applied after every tree
    # reset: the loop resets chipyard to its pinned commit each iteration, so a
    # one-shot patch would silently vanish and the agent would find a PE.scala
    # with no gating hook to improve. If the AGENT has since rewritten the
    # region, the sentinel is present and this is a no-op -- its edit wins.
    try:
        import rtl_scaffold
        pe_path = os.path.join(chipyard_path, RTL_FILES_REL[0])
        if os.path.isfile(pe_path):
            with open(pe_path) as f:
                before = f.read()
            after, changed = rtl_scaffold.apply_ta(before)
            if changed:
                with open(pe_path, "w") as f:
                    f.write(after)
                wrote.append(RTL_FILES_REL[0])
        # MAC_GATED_TOTAL: two harness-owned files, re-patched after every
        # tree reset for the same reason as T-A. NOT agent-writable -- a
        # counter the agent could edit is a counter the agent could fake.
        gsrc = os.path.join(chipyard_path, "generators/gemmini/src/main/scala/gemmini")
        cf_p = os.path.join(gsrc, "CounterFile.scala")
        ec_p = os.path.join(gsrc, "ExecuteController.scala")
        if os.path.isfile(cf_p) and os.path.isfile(ec_p):
            with open(cf_p) as f:
                cf0 = f.read()
            with open(ec_p) as f:
                ec0 = f.read()
            cf1, ec1, cchanged = rtl_scaffold.apply_counters(cf0, ec0)
            if cchanged:
                with open(cf_p, "w") as f:
                    f.write(cf1)
                with open(ec_p, "w") as f:
                    f.write(ec1)
                wrote.append("generators/gemmini/src/main/scala/gemmini/CounterFile.scala")
                wrote.append("generators/gemmini/src/main/scala/gemmini/ExecuteController.scala")
        # T-B: the ZBU, in Scratchpad.scala. Harness-owned for the same
        # reason as the counters -- the agent tunes zbu_enable/granule_size
        # via the design state, it does not hand-edit the bitmap.
        sp_p = os.path.join(gsrc, "Scratchpad.scala")
        if os.path.isfile(sp_p):
            with open(sp_p) as f:
                sp0 = f.read()
            sp1, spchanged = rtl_scaffold.apply_tb(sp0)
            if spchanged:
                with open(sp_p, "w") as f:
                    f.write(sp1)
                wrote.append("generators/gemmini/src/main/scala/gemmini/Scratchpad.scala")
        # The C side of the same counter. Scala CounterExternal and
        # gemmini_counter.h are maintained separately; patch both together or
        # the kernel fails to compile AFTER a 98 s elaboration.
        hdr_rel = f"{GEMMINI_SW_REL}/include/gemmini_counter.h"
        hdr_p = os.path.join(chipyard_path, hdr_rel)
        if os.path.isfile(hdr_p):
            with open(hdr_p) as f:
                h0 = f.read()
            h1, hchanged = rtl_scaffold.apply_counter_header(h0)
            if hchanged:
                with open(hdr_p, "w") as f:
                    f.write(h1)
                wrote.append(hdr_rel)
    except Exception as e:                       # never fail the iteration here
        logger.warning("T-A scaffolding skipped: %s", e)

    return ApplyResult(ok=True, params_path=params_path, wrote=wrote,
                       message=f"wrote {', '.join(wrote)} "
                               f"for state {state.state_hash()}")


# --------------------------------------------------------------------------
# 2.8 -- RTL identity. The half of the elaboration cache key that hw_hash()
# cannot see.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def rtl_digest(chipyard_path: str = CHIPYARD_PATH) -> str:
    """SHA-256 over the agent-writable RTL, as it exists in the tree right now.

    Returns 16 hex chars, or the sentinel ``none`` when no such file exists yet
    (true through Phase 2, since SparseCraftSparsity.scala arrives in Phase 3).

    A MISSING file and an EMPTY file must not hash alike: the first is "this
    technique does not exist yet", the second is "the agent truncated it". They
    are recorded distinctly below.
    """
    import hashlib
    h = hashlib.sha256()
    seen = 0
    for rel in RTL_FILES_REL:
        path = os.path.join(chipyard_path, rel)
        h.update(rel.encode())
        if os.path.isfile(path):
            with open(path, "rb") as f:
                h.update(b"present:")
                h.update(f.read())
            seen += 1
        else:
            h.update(b"absent")
    # Harness-patched RTL (counters) changes the netlist exactly as the
    # agent-writable set does. Excluded from `seen` -- these files always
    # exist, so counting them would defeat the "no technique yet" sentinel --
    # but folded into the hash so the cache tag tracks them.
    for rel in HARNESS_PATCHED_RTL_REL:
        path = os.path.join(chipyard_path, rel)
        h.update(rel.encode())
        if os.path.isfile(path):
            with open(path, "rb") as f:
                h.update(b"present:")
                h.update(f.read())
        else:
            h.update(b"absent")
    return "none" if seen == 0 else h.hexdigest()[:16]


# --------------------------------------------------------------------------
# N12 -- the RTL compile gate. Measured 17-19 s against a ~5 min iteration.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def rtl_compile_check(chipyard_path: str = CHIPYARD_PATH,
                      timeout_seconds: int = 600) -> dict:
    """`sbt "project gemmini" compile` -- type-check before paying for a build.

    Measured on this host 2026-09-19: 17 s clean, 19 s on a deliberately broken
    PE.scala, reporting exact file:line:col. sbt is NOT on the default PATH; it
    lives in chipyard's conda env, which `source env.sh` puts there.

    Returns ok=False with the compiler's own [error] lines so the diagnosis
    handed back to the agent is the compiler's, not a paraphrase.
    """
    cmd = (f'cd {chipyard_path} && source env.sh >/dev/null 2>&1 && '
           f'sbt -batch "project gemmini" compile')
    try:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                           text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "errors": "sbt compile timed out",
                "stdout_tail": ""}
    out = (r.stdout or "") + (r.stderr or "")
    errors = "\n".join(l for l in out.splitlines() if l.startswith("[error]"))
    return {"ok": r.returncode == 0, "returncode": r.returncode,
            "errors": errors[-4000:], "stdout_tail": out[-2000:]}


# --------------------------------------------------------------------------
# N30 + N31 -- Chisel elaboration and Verilator build.
# Keyed on hw_hash by the caller via _chia_tag.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def elaborate(state_json: str, chipyard_path: str = CHIPYARD_PATH,
              make_jobs: int = BUILD_MAKE_JOBS,
              verilator_threads: int = VERILATOR_THREADS,
              timeout_seconds: int = 5400,
              config_name: str = CONFIG_NAME,
              collect_src: bool = False) -> BuildArtifact:
    """Elaborate the SoC and build the Verilator simulator.

    make_jobs defaults to 8, not nproc: this host has 64 cores but only 30 GB
    of RAM, and a Chisel elaboration peaks at 8-12 GB.

    It was 16, reasoning about TWO concurrent elaborations. Measured
    2026-09-19: that is the wrong constraint. A SINGLE elaboration at -j16,
    with Ray's own ~8 GB of raylets/workers and a browser also resident, took
    the node to 28.99 GB / 30.43 GB and Ray's OOM killer terminated the job at
    minute 19 of a 20-minute build -- after sbt had already reached 5.95 GB.
    The whole rebuild measures ~94 s, so halving the C++ compile parallelism
    costs a couple of minutes and removes a failure mode that destroys a
    nearly-complete run.

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
        # VERILATOR_THREADS is a BUILD-time flag, so it lands here and not in
        # the run node. It defaulted to 1 (chipyard Makefile:124) and
        # extra_make_args was empty, so every simulation before 2026-09-19 was
        # single-threaded. The caller MUST fold verilator_threads into the
        # _chia_tag: two builds of the same design state with different thread
        # counts are different artifacts and must not answer each other.
        extra_make_args={"VERILATOR_THREADS": str(verilator_threads)},
        name=f"sparsecraft-{state.hw_hash()}-t{verilator_threads}",
    )
    # node.build(), not node.build(node). ChiselBuildNode.build is
    # `@ChiaFunction def build(self)`, and the decorator's wrapper is a plain
    # function on the class, so attribute access binds it -- passing the node
    # again hands it self twice. Every CHIA example calls it bare
    # (examples/riscv_extensions/nodes.py:120, examples/gem5_align:683).
    return node.build()


# --------------------------------------------------------------------------
# N12b (2.11) -- the assertion that N12 and N41 together cannot make.
#
# An RTL edit that compiles, computes the right answer, and instantiates
# NOTHING passes both gates and reports "no improvement" -- indistinguishable
# from a legitimate negative result, and poison for an unattended arm.
#
# Two ways to get this wrong, both hit on 2026-09-19:
#   * hashing a NAMED file: firtool renames modules (the mesh PE is emitted as
#     PE_256.sv, not PE.sv), so a named file can miss the change entirely.
#   * hashing RAW text: firtool embeds Scala source locations in comments
#     (`// @[PE.scala:70:15]`), so any patch that shifts line numbers changes
#     the hash of files whose hardware is identical -- 30 diff lines, 0 after
#     stripping comments.
# So: the WHOLE collateral, with comments stripped.
# --------------------------------------------------------------------------
_COMMENT_RE = re.compile(r"//.*$", re.M)


@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def netlist_digest(chipyard_path: str = CHIPYARD_PATH,
                   config_name: str = CONFIG_NAME) -> dict:
    """Hash the elaborated Verilog, ignoring firtool's source-location noise."""
    import glob
    import hashlib
    base = os.path.join(chipyard_path, "sims", "verilator", "generated-src",
                        f"chipyard.harness.TestHarness.{config_name}",
                        "gen-collateral")
    files = sorted(glob.glob(os.path.join(base, "*.sv"))
                   + glob.glob(os.path.join(base, "*.v")))
    if not files:
        return {"ok": False, "digest": "", "n_files": 0,
                "error": f"no generated Verilog under {base}"}
    h = hashlib.sha256()
    for f in files:
        h.update(os.path.basename(f).encode())
        with open(f, errors="ignore") as fh:
            stripped = _COMMENT_RE.sub("", fh.read())
        h.update(re.sub(r"[ \t]+$", "", stripped, flags=re.M).encode())
    return {"ok": True, "digest": h.hexdigest()[:16], "n_files": len(files)}


# --------------------------------------------------------------------------
# N32 -- cross-compile the SpMM kernel.
# Runs on the chipyard worker, not riscv_build: it needs gemmini_params.h,
# which elaboration just emitted into the Chipyard tree.
# --------------------------------------------------------------------------
@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def build_kernel(state_json: str, kernel_source: str, data_header: str,
                 kernel_name: str = "spmm",
                 chipyard_path: str = CHIPYARD_PATH,
                 timeout_seconds: int = 900) -> dict:
    """Compile one baremetal kernel against the freshly generated Gemmini header.

    ``data_header`` is the generated workload (blocked-dense A, dense X, and the
    host-computed golden Y) produced by ``workload/prep_matrices.py``. It is
    passed by VALUE rather than read from a path, because the head and this
    container share no filesystem -- the same reason kernel_source is.

    Failure is reported, never inferred: the caller gets returncode, the
    compiler's stderr, and the byte size of the artifact. A zero-byte binary
    with returncode 0 counts as a failure here, because that combination has
    actually happened on this project and reads as success everywhere else.
    """
    import json
    state = DesignState.from_dict(json.loads(state_json))

    work = os.path.join("/tmp", f"sparsecraft_sw_{state.sw_hash()}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, f"{kernel_name}.c")
    with open(src, "w") as f:
        f.write(kernel_source)
    # The kernel includes "spmm_data.h" by that exact name.
    with open(os.path.join(work, "spmm_data.h"), "w") as f:
        f.write(data_header)

    gemmini_sw = os.path.join(chipyard_path, GEMMINI_SW_REL)

    # Patch gemmini_counter.h HERE, not in apply_design_state.
    #
    # ELABORATION REGENERATES IT. Patching before the build is useless: the
    # sequence is apply_design_state (patch) -> elaborate (regenerate, wiping
    # the patch) -> build_kernel (header lacks MAC_GATED_TOTAL -> compile
    # error). The file even shows up in `git status` as touched, which makes
    # the patch look applied right up until the compiler disagrees.
    #
    # Doing it here, immediately before the compile, is the only point after
    # the last writer. Idempotent, so it costs nothing if elaboration ever
    # stops regenerating it.
    try:
        import rtl_scaffold
        hdr_p = os.path.join(gemmini_sw, "include", "gemmini_counter.h")
        if os.path.isfile(hdr_p):
            with open(hdr_p) as f:
                h0 = f.read()
            h1, hchanged = rtl_scaffold.apply_counter_header(h0)
            if hchanged:
                with open(hdr_p, "w") as f:
                    f.write(h1)
    except Exception as e:
        logger.warning("counter header patch skipped: %s", e)

    riscv = os.environ.get("RISCV", os.path.join(chipyard_path, ".conda-env", "riscv-tools"))
    gcc = os.path.join(riscv, "bin", "riscv64-unknown-elf-gcc")
    out = os.path.join(work, f"{kernel_name}.riscv")
    if os.path.exists(out):
        os.remove(out)          # never let a stale binary answer for a failed build

    # Flags mirror gemmini-rocc-tests/bareMetalC/Makefile. Two details matter:
    # the include root is the gemmini-rocc-tests dir itself (headers are
    # included as "include/gemmini_testutils.h"), and -DBAREMETAL=1 is what
    # keeps the headers off <sys/mman.h>.
    cmd = [
        gcc, "-O2", "-std=gnu99", "-static", "-specs=htif_nano.specs",
        "-DBAREMETAL=1", "-DPREALLOCATE=1", "-mcmodel=medany",
        f"-I{gemmini_sw}", f"-I{gemmini_sw}/riscv-tests",
        f"-I{gemmini_sw}/riscv-tests/env", f"-I{gemmini_sw}/bareMetalC",
        f"-I{work}",
        # B0 vs B1/B2 is one flag on the same instrument, so the baseline and
        # the search use identical measurement code.
        f"-DSPMM_DENSE={1 if state.dense_mode else 0}",
        # SW schedule levers. These change the Gemmini instruction schedule,
        # never the computation, so N41 still gates correctness. They MUST be
        # in the build cache tag as well -- a -D flag changes the binary
        # without changing the source, and loop.py folds them into build_id
        # for exactly that reason.
        f"-DSPMM_KCHUNK={int(state.k_chunk)}",
        f"-DSPMM_B_BLOCKS={int(state.b_blocks)}",
        f"-DSPMM_A_BLOCKS={int(state.a_blocks)}",
        src, "-o", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    content = b""
    size = 0
    if os.path.exists(out):
        size = os.path.getsize(out)
        with open(out, "rb") as f:
            content = f.read()
    ok = (r.returncode == 0) and size > 0
    return {"success": ok,
            "binary_name": f"{kernel_name}.riscv",
            "binary_content": content,
            "binary_bytes": size,
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
# Pattern name -> the P_* code attn_prefill.c switches on. An unknown name
# falls back to 0 (causal), which is the safe direction: it computes MORE
# blocks, never fewer, so a typo cannot silently fake a speedup.
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
    # SparseCraft-specific levers are carried in a comment header the model
    # must keep: the workload selection, the B0 dense-mode switch, and the RTL
    # microarchitecture parameters (markers until Phase 3 makes them real
    # Chisel parameters).
    for name in ("granule_size",):
        mm = re.search(rf"//\s*SPARSECRAFT\s+{name}\s*=\s*(\d+)", text)
        if mm:
            out[name] = int(mm.group(1))
    for name in ("gate_enable", "zbu_enable", "dense_mode"):
        mm = re.search(rf"//\s*SPARSECRAFT\s+{name}\s*=\s*([01])", text)
        if mm:
            out[name] = bool(int(mm.group(1)))
    for name in ("workload", "zbu_operand"):
        mm = re.search(rf"//\s*SPARSECRAFT\s+{name}\s*=\s*([A-Za-z0-9_]+)", text)
        if mm:
            out[name] = mm.group(1)
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
