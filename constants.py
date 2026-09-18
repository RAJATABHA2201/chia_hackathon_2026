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

CONFIG_NAME = "SparseCraftConfig"
CONFIG_PACKAGE = "chipyard"
BASELINE_CONFIG_NAME = "LeanGemminiRocketConfig"

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
PROJECT_ROOT = os.environ.get("SPARSECRAFT_ROOT", "/home/chia-sparsecraft")
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
    for var in ("SPARSECRAFT_LLM_BACKEND", "SPARSECRAFT_LLM_MODEL",
                "SPARSECRAFT_SYNTH_TECH", "SPARSECRAFT_SYNTH_CLOCK_NS"):
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
    here = os.path.join(PROJECT_ROOT, "sparsecraft")
    py_modules = sorted(
        os.path.join(here, f) for f in os.listdir(here)
        if f.endswith(".py") and not f.startswith("_"))
    return {
        "py_modules": py_modules,
        "excludes": ["**/__pycache__/**", "**/*.pyc", "runs/**", "cache/**"],
        "env_vars": env_vars,
    }
