"""Paths, resource names and the Ray runtime env for the SparseCraft loop.

Every in-container path here is an absolute path *inside* the CHIA images, not on
the head. The head only ever holds design states, diffs, metrics and traces.
"""

import os

# --- In-container paths (ghcr.io/ucb-bar/chia-chisel-build) -----------------
CHIPYARD_PATH = os.environ.get("SPARSECRAFT_CHIPYARD_PATH", "/home/ray/chipyard")

# Gemmini is a chipyard SUBMODULE and it carries its own chipyard-package
# configs (generators/gemmini/chipyard/GemminiConfigs.scala is where
# GemminiRocketConfig / LeanGemminiRocketConfig actually live). Both files we
# touch therefore sit inside that one submodule, which keeps diff collection to
# a single extra repo.
GEMMINI_REPO_REL = "generators/gemmini"
GEMMINI_SRC_REL = f"{GEMMINI_REPO_REL}/src/main/scala/gemmini"
GEMMINI_CFG_REL = f"{GEMMINI_REPO_REL}/chipyard"

# The writable set. Everything else in the tree is denied by the N13 scope
# check before git apply -- enforced programmatically, never by prompt (L-inf).
PARAMS_FILE_REL = f"{GEMMINI_SRC_REL}/SparseCraftParams.scala"
# Harness-owned, regenerated from the design state every iteration: the RTL
# microarchitecture parameters PE.scala and the ZBU read. NOT agent-writable --
# the agent changes the MECHANISM, the harness sets the knobs from the state.
RTL_PARAMS_FILE_REL = f"{GEMMINI_SRC_REL}/SparseCraftRTL.scala"
HARNESS_FILE_REL = f"{GEMMINI_CFG_REL}/SparseCraftConfigs.scala"

# The nested software submodule. Defined here, above SUBMODULES, because
# SUBMODULES now references it.
GEMMINI_SW_REL = f"{GEMMINI_REPO_REL}/software/gemmini-rocc-tests"

# Repos collect_diff/reset_and_apply_diff must track, beyond chipyard itself.
# Both levels: elaboration writes gemmini_params.h into the NESTED
# gemmini-rocc-tests submodule, so scanning only the outer one reports an
# opaque ' M software/gemmini-rocc-tests' that the N13 allowlist cannot tell
# apart from a model edit. Recursing gives per-file paths, and the generated
# header is then excused by name in loop.py's harness_paths.
SUBMODULES = [GEMMINI_REPO_REL, GEMMINI_SW_REL]

# Gemmini's generated header, emitted by elaboration. The kernels include it,
# which is why the software build's cache key must carry the config hash.
GEMMINI_PARAMS_H_REL = f"{GEMMINI_REPO_REL}/software/gemmini-rocc-tests/include/gemmini_params.h"

# --- The RTL the agent may write (Phase 3), and which therefore forms part of
# --- the ELABORATION IDENTITY alongside the typed config state.
#
# DesignState.hw_hash() hashes only the 23 typed config fields. Editing
# PE.scala changes the generated Verilog but NOT that hash, so a cache tag
# built from hw_hash alone would serve the PREVIOUS build and the loop would
# measure old hardware while attributing it to new RTL -- silently, with no
# error. nodes.rtl_digest() closes that hole and loop.py folds it into the tag.
RTL_FILES_REL = (
    f"{GEMMINI_SRC_REL}/PE.scala",                    # T-A: zero-gated MAC
    f"{GEMMINI_SRC_REL}/SparseCraftSparsity.scala",   # T-B: the ZBU (Phase 3)
)

# Files the HARNESS patches (rtl_scaffold), not the agent. These change the
# elaborated Verilog just as surely as PE.scala does, so they must enter the
# cache key too -- ta-gated4 -> ta-gated5 changed the counter guard in
# ExecuteController.scala and the tag did not move, which would have served a
# stale simulator under a new design. Separate tuple because the N13 scope
# check must keep denying these to the agent.
HARNESS_PATCHED_RTL_REL = (
    f"{GEMMINI_SRC_REL}/CounterFile.scala",       # CounterExternal slot
    f"{GEMMINI_SRC_REL}/ExecuteController.scala", # the gated-MAC accumulator
    f"{GEMMINI_SRC_REL}/Scratchpad.scala",        # T-B: the ZBU bitmap
)

CONFIG_NAME = "SparseCraftConfig"
CONFIG_PACKAGE = "chipyard"
BASELINE_CONFIG_NAME = "LeanGemminiRocketConfig"

# --- Parallelism tunables (see ../paR_THREADSrallelism.md) ---------------------------
# UPDATED 2026-09-20: the host went from 32 GB to 64 GB. Memory was the binding
# constraint for this whole project; it no longer is. 61 GB usable against 64
# logical cores is ~1 GB/core, so cores are now the limit and these numbers are
# sized for throughput rather than for survival.
#
# The history is kept deliberately: every number below was calibrated against
# 32 GB, and several hard-won failures (an OOM at 28.99/30.43 GB during
# elaboration; a synthesis OOM that killed a 15-iteration run at iteration 1)
# came from that ceiling. Do not "restore" the old values on a 64 GB host.

# `make -j` for Chisel elaboration + Verilator C++ compilation. NOT simulation.
# Verilator emits large translation units and each g++ peaks at 1-1.5 GB.
# At 32 GB the safe budget was ~16 and even that OOMed when other tenants held
# memory. At 61 GB, 24 x 1.5 GB = 36 GB still leaves ~20 GB for the raylets,
# the container runtime and the other user on this shared box.
def _safe_make_jobs() -> int:
    """`make -j` sized from the RAM THIS host has, not the one it was tuned on.

    The 24 above was calibrated for a 64 GB configuration. Measured 2026-09-24
    the host is back to 30 GB with ~10 GB free, and 24 concurrent g++ at
    1.0-1.5 GB each wants 24-36 GB -- which is precisely the OOM that killed an
    elaboration at minute 19 and a 15-iteration run at iteration 1 during the
    32 GB era. A hard-coded constant cannot notice that; MemAvailable can.

    Budget 1.5 GB per compiler against available memory, keep 4 GB back for the
    raylets and the container runtime, and clamp to [4, 12].

    12, not 24, since N52 synthesis overlaps the simulation (loop.py,
    synth_in_parallel). The worst case is a repair round whose elaboration
    starts while the previous round's synthesis is still running. Measured
    2026-09-25 on the 61 GB host: the IDLE cluster already holds ~18 GB (13 GB
    of it Ray's pre-spawned ray::IDLE workers, because CHIA never passes
    num_cpus), so 12 x 1.5 GB of g++ + ~20 GB of yosys + 18 GB is ~56 GB; at 24
    it would pass the ceiling. Elaboration is 4% of an iteration (1.8 min at -j8
    in V1's final15), so the cap costs nothing measurable.
    """
    env = os.environ.get("SPARSECRAFT_MAKE_JOBS")
    if env:
        return int(env)
    try:
        with open("/proc/meminfo") as f:
            avail_gb = next(int(l.split()[1]) for l in f
                            if l.startswith("MemAvailable")) / 1048576.0
    except Exception:                                        # noqa: BLE001
        return 8
    return max(4, min(12, int((avail_gb - 4.0) / 1.5)))


BUILD_MAKE_JOBS = _safe_make_jobs()

# Verilator simulation threading. This is a BUILD-time flag
# (chipyard sims/verilator/Makefile:124 `VERILATOR_THREADS ?= 1`), passed
# through ChiselBuildNode's extra_make_args -- so changing it REBUILDS the
# simulator and must therefore enter the elaboration cache key.
#
# It was effectively 1 until 2026-09-19 (extra_make_args was empty), which made
# every simulation single-threaded; setting it to 16 gave ~6x. The cluster.yaml
# constraint is `concurrent sims x threads <= cores`.
#
# 16, MEASURED (2026-09-25, Threadripper 9970X, 32 cores / 64 threads). The
# baseline design (identical state, dnn512) simulated in 19.8-22.0 min at 16
# threads across three V1 runs, and in 28.4 min at 30 threads (runs/
# v2-final15-t30-aborted): 29% SLOWER. Verilator's threads meet at a barrier
# every evaluation, and past ~16 the synchronisation costs more than the extra
# parallelism buys on this SoC. Do not raise it without re-measuring.
VERILATOR_THREADS = int(os.environ.get("SPARSECRAFT_VERILATOR_THREADS", "16"))

# --- Ray resource names (must match cluster.yaml) ---------------------------
R_CHIPYARD = "chipyard"
R_VERILATOR = "verilator_run"
R_RISCV_BUILD = "riscv_build"
R_HEAD_LOCAL = "head_local"
R_DATABASE = "database"
# Coarse LLM resource, requested 1.0/call so one agentic turn occupies a whole
# slot. The backends' own default (vertex_creds / opencode_creds, 0.01) is
# overridden at dispatch via .options(resources={"llm": 1.0}).
R_LLM = "llm"
# T3 physical synthesis. Advertised only by the sparsecraft-synth image
# (docker/SparseCraftSynthDockerfile) -- the stock chia images carry hammer but
# neither yosys nor a PDK, so a worker claiming this resource is claiming to
# have both.
R_HAMMER = "hammer"

HEAD_LOCAL = {"resources": {R_HEAD_LOCAL: 0.1}}
LLM_OPTS = {"resources": {R_LLM: 1.0}}

# Build nodes request 0.9 of a placement-group bundle that reserves 1.0, so the
# editor BashTool actor fits in the same bundle as the builder.
BUILD_FRACTION = 0.9

# --- Head-side durable paths ------------------------------------------------
# NOT derived from __file__: under `chia job submit --working-dir .` that
# resolves into an empty /tmp/ray/.../_ray_pkg_* copy.
# LOOP V2 ISOLATION. V1 defaults this to /home/chia-sparsecraft, so RUN_DIR and
# CACHE_DIR resolve to the SHARED runs/ and cache/ that the V1 loop is writing
# to right now. V2 points at its own tree instead, so the two can coexist and a
# V2 experiment can never overwrite a V1 run directory or poison the V1 cache.
# SPARSECRAFT_ROOT still overrides, so pointing V2 at the shared cache for a
# deliberate warm start is one env var.
# Where the modules live. Distinct from PROJECT_ROOT -- see runtime_env().
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- The package layout, in ONE place --------------------------------------
# Every importable module is in src/ (SOURCE_DIR), FLAT: Ray pickles each
# @ChiaFunction by reference under its top-level module name (`nodes`, never
# `src.nodes`), and runtime_env() ships the files individually to match. So
# src/ is a directory of top-level modules, not a Python package, and must
# stay one. Everything else is found relative to it:
#
#   PACKAGE_DIR   the sparsecraft-v2 checkout itself
#   CONFIG_DIR    cluster + cache/bypass YAML
#   PROMPTS_DIR   the prompt tree agent.py composes system messages from
#   KERNELS_DIR   the SpMM kernel source (kernels/spmm.c is IMMUTABLE)
#   WORKLOAD_DIR  prep_matrices.py and its generated headers + stats
#   SCRIPTS_DIR   operator entry points (run.sh, preflight, reports)
#
# DRIVER-SIDE ONLY. On a Ray worker this file is a py_modules copy under
# /tmp/ray/..., so these resolve to directories that do not exist there. No
# worker code reads them: kernel source and workload headers travel to the
# build container BY VALUE, and prompts are composed on the driver.
PACKAGE_DIR = os.path.dirname(SOURCE_DIR)
CONFIG_DIR = os.path.join(PACKAGE_DIR, "configs")
PROMPTS_DIR = os.path.join(PACKAGE_DIR, "prompts")
KERNELS_DIR = os.path.join(PACKAGE_DIR, "kernels")
WORKLOAD_DIR = os.path.join(PACKAGE_DIR, "workload")
SCRIPTS_DIR = os.path.join(PACKAGE_DIR, "scripts")

# Defaults to the checkout itself, so a fresh clone runs anywhere: runs/ and
# cache/ land beside src/. scripts/run.sh exports SPARSECRAFT_ROOT explicitly,
# and runtime_env() forwards it, so the driver and every worker agree even under
# `chia job submit`, where the driver's __file__ is an uploaded copy.
PROJECT_ROOT = os.environ.get("SPARSECRAFT_ROOT", PACKAGE_DIR)
RUN_DIR = os.path.join(PROJECT_ROOT, "runs")
CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")

SIM_WORK_DIR = "/tmp/sparsecraft_sim"
BUILD_WORK_DIR = "/tmp/sparsecraft_build"
SYNTH_WORK_DIR = "/tmp/sparsecraft_syn"

# --- T3 synthesis ------------------------------------------------------------
# In-container paths belonging to the synthesis image, not the head.
PDK_ROOT = os.environ.get("SPARSECRAFT_PDK_ROOT", "/home/ray/pdk")

# nangate45, not sky130: hammer's nangate45 plugin is the one the yosys/openroad
# plugins are actually validated against, the PDK is ~10 MB against sky130's
# multi-GB, and 45 nm is a more defensible node to quote accelerator area at
# than 130 nm. sky130 is installed in the same image and is a one-word swap
# (synth_node.TECHNOLOGIES) if a second technology point is wanted.
SYNTH_TECHNOLOGY = os.environ.get("SPARSECRAFT_SYNTH_TECH", "nangate45")

# "auto" resolves the outermost Gemmini module out of the generated RTL rather
# than hardcoding a name firtool is free to change. Set an explicit module name
# to pin it.
SYNTH_TOP_MODULE = os.environ.get("SPARSECRAFT_SYNTH_TOP", "auto")

# The clock target abc optimizes toward and OpenSTA measures slack against.
# Held CONSTANT across design points: Fmax is derived as 1/(target - slack), so
# a moving target would make two iterations incomparable.
SYNTH_CLOCK_NS = float(os.environ.get("SPARSECRAFT_SYNTH_CLOCK_NS", "2.0"))

# Yosys on a full Gemmini mesh is minutes, not hours, but abc can pathologically
# blow up on a wide multiplier array; this is the cutoff before the iteration is
# recorded as a T3 failure and the loop moves on.
SYNTH_TIMEOUT_S = int(os.environ.get("SPARSECRAFT_SYNTH_TIMEOUT_S", "7200"))

# --- Workload (prefill-only scope) ------------------------------------------
SEQ_LEN_T2A = 256          # functional slice, correctness gate
N_HEADS_T2A = 2
D_HEAD = 64
BLOCK_SIZE_DEFAULT = 32

# Patterns the agent is allowed to see. The held-out set lives in
# holdout.py and is never imported by any agent-facing node.
VISIBLE_PATTERNS = ("block_sparse_static", "nm_structured_2_4")


def runtime_env() -> dict:
    """Ship the sparsecraft package to workers.

    Paths are anchored on PROJECT_ROOT rather than __file__, which under
    `chia job submit --working-dir .` resolves into an empty _ray_pkg_* copy.
    """
    env_vars = {"SPARSECRAFT_ROOT": PROJECT_ROOT}
    # Forward the backend SELECTION, not the secret. The API key is read on the
    # driver by agent.make_llm and travels inside the constructed LLM object, so
    # a worker never needs it in its environment -- and it never lands in a
    # runtime_env that Ray logs and echoes back in job metadata.
    # MAKE_JOBS and VERILATOR_THREADS must be forwarded or they do nothing.
    # constants.py is imported ON THE WORKER, inside the container, so a value
    # exported in the driver's shell never reaches the process that actually
    # runs `make -j`. Setting SPARSECRAFT_MAKE_JOBS=8 and watching elaboration
    # still fork 24 compilers -- and OOM -- is the failure this prevents.
    for var in ("SPARSECRAFT_LLM_BACKEND", "SPARSECRAFT_LLM_MODEL",
                "SPARSECRAFT_SYNTH_TECH", "SPARSECRAFT_SYNTH_CLOCK_NS",
                "SPARSECRAFT_MAKE_JOBS", "SPARSECRAFT_VERILATOR_THREADS"):
        if os.environ.get(var):
            env_vars[var] = os.environ[var]
    # Ship the modules INDIVIDUALLY, not the package directory.
    #
    # `py_modules: [<dir>]` uploads the directory as a package, so a worker can
    # import `sparsecraft.nodes` but not `nodes` -- and `nodes` is exactly what
    # every @ChiaFunction here is pickled by reference as. The task then dies on
    # the worker with "No module named 'nodes'", which reads like a cluster
    # fault and is not one. Listing the files makes them top-level modules on
    # the worker, matching how the driver imports them. This is the idiom
    # examples/circt_issue_solver uses (py_modules of individual .py paths).
    # SOURCE_DIR, not PROJECT_ROOT/"sparsecraft". Those coincided in V1, where
    # the tree lived at <root>/sparsecraft; in V2 PROJECT_ROOT IS the source
    # tree, so the old join pointed at a directory that does not exist and
    # runtime_env() raised FileNotFoundError on the first ray.init.
    #
    # The two are genuinely different things and now say so: PROJECT_ROOT is
    # where DURABLE OUTPUT goes (runs/, cache/) and must not be derived from
    # __file__, because `chia job submit --working-dir .` resolves that into an
    # empty _ray_pkg_* copy. SOURCE_DIR is where the MODULES are, which is
    # exactly what __file__ gives -- and under job submit the package copy
    # holds the same files, so shipping from there is correct.
    here = SOURCE_DIR
    py_modules = sorted(
        os.path.join(here, f) for f in os.listdir(here)
        if f.endswith(".py") and not f.startswith("_"))
    # rtl_scaffold.py is imported by apply_design_state ON THE WORKER, so it
    # must be in py_modules like every other module the nodes import.
    return {
        "py_modules": py_modules,
        "excludes": ["**/__pycache__/**", "**/*.pyc", "runs/**", "cache/**"],
        "env_vars": env_vars,
    }
