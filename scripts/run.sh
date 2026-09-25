#!/usr/bin/env bash
# One command: preflight, cluster up, loop, cluster down.
#
# Claude Code as the agent (the default). Needs no key and no cloud project --
# the CLI uses the login already on this host:
#   claude            # once, interactively, if `claude auth` has never run
#   scripts/run.sh --iters 20 --synth
#
# Vertex (bills to GCP credits; project/location default in the env block below):
#   gcloud auth application-default login --no-launch-browser   # once
#   scripts/run.sh --backend vertex --iters 20 --synth
#
# Or an AI Studio key, which needs no gcloud and no cloud project:
#   export GEMINI_API_KEY=...        # https://aistudio.google.com/apikey
#   scripts/run.sh                   # 5 iterations
#   scripts/run.sh --iters 20 --synth  # overnight, with measured area and Fmax
#
# Everything before the loop is a cheap check that fails in seconds. Everything
# after it is a teardown that runs even if the loop crashes or you Ctrl-C.
set -euo pipefail

# Run from the sparsecraft-v2 ROOT, wherever this is invoked from: every path
# below (src/, configs/, runs/) is relative to it. This file lives in scripts/.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Where runs/ and cache/ go. Exported so the driver and every worker agree.
export SPARSECRAFT_ROOT="${SPARSECRAFT_ROOT:-$ROOT}"

ITERS=5
EXTRA=()
BRING_UP=1
TEAR_DOWN=1
SKIP_PREFLIGHT=0
SUBMIT=0
NEEDS_LLM=1

usage() {
    cat <<USAGE
usage: scripts/run.sh [options] [-- extra src/loop.py args]

  --iters N          iterations (default $ITERS)
  --backend NAME     claude | gemini | vertex | openai | claude_api |
                     anthropic | openrouter | groq   (default: claude)
  --model ID         model id; default is the backend's own
  --synth            score on MEASURED area and Fmax (T3), not T1's model
  --proposer NAME    agent | random | greedy -- who picks the next design.
                     random and greedy are the non-agentic control arms and
                     use no model, so they need no credentials.
  --cache-scope S    who may satisfy a cache hit: run (default) | global | off.
                     run   = only work done earlier in THIS run; the cache
                             starts empty, so nothing from an older run leaks in.
                     global= the shared cache, reusable across runs.
                     off   = recompute everything.
  --skip-llm         run the harness with no model at all
  --no-up            assume the cluster is already running
  --no-down          leave the cluster up when the loop finishes
  --submit           run through \`chia job submit\` instead of directly
  --skip-preflight   skip the credential and toolchain checks
  -h, --help         this

Anything after -- is passed through to src/loop.py unchanged.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --iters)          ITERS="$2"; shift 2 ;;
        --backend)        EXTRA+=(--backend "$2"); export SPARSECRAFT_LLM_BACKEND="$2"; shift 2 ;;
        --model)          EXTRA+=(--model "$2");   export SPARSECRAFT_LLM_MODEL="$2";   shift 2 ;;
        --synth)          EXTRA+=(--synth); shift ;;
        --proposer)       EXTRA+=(--proposer "$2")
                          # random/greedy use no model, so the credential
                          # preflight must not gate them -- otherwise an
                          # overnight control arm dies on an unrelated auth
                          # hiccup.
                          [[ "$2" != "agent" ]] && NEEDS_LLM=0
                          shift 2 ;;
        --cache-scope)    EXTRA+=(--cache-scope "$2"); shift 2 ;;
        --cache)          EXTRA+=(--cache); shift ;;
        --no-cache)       EXTRA+=(--no-cache); shift ;;
        --skip-llm)       EXTRA+=(--skip-llm); shift ;;
        --no-up)          BRING_UP=0; shift ;;
        --no-down)        TEAR_DOWN=0; shift ;;
        --submit)         SUBMIT=1; shift ;;
        --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        --)               shift; EXTRA+=("$@"); break ;;
        *)                echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# --- environment ----------------------------------------------------------
# shellcheck disable=SC1091
source ~/miniforge3/etc/profile.d/conda.sh
conda activate chia_env
export PATH="$HOME/bin:$PATH"                 # the docker -> podman shim
# Unbuffered, always. Redirect this script to a log (which is the normal way to
# run something that takes hours) and a buffered driver shows nothing at all
# until it exits -- so a run that is progressing is indistinguishable from one
# that is wedged. Ray's actor output is line-flushed and the driver's was not,
# which made the log actively misleading.
export PYTHONUNBUFFERED=1
export TMPDIR="${TMPDIR:-$HOME/podman-tmp}"
mkdir -p "$TMPDIR"

# --- Vertex AI defaults ----------------------------------------------------
# Only read when --backend vertex is in play; harmless otherwise. Set here
# rather than left to the shell so a clean login can run the loop: Vertex
# authenticates with ADC plus a PROJECT, and the project is not part of the
# credential -- VertexGeminiLLM reads GOOGLE_CLOUD_PROJECT in its constructor
# (chia/models/vertex.py:242) and GOOGLE_CLOUD_LOCATION just below it, falling
# back to us-central1. Unset, the failure is a DefaultCredentialsError that
# reads like missing auth when the auth is fine.
#
# Both are :- defaults, so exporting either one before calling still wins.
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-chia-hackathon-2026}"
# "global", not a region. Measured 2026-09-19: gemini-3.8-flash (and every
# other Gemini 3.x) 404s with NOT_FOUND in us-central1 and us-east5, and is
# served ONLY from the global endpoint. gemini-2.5-pro works on global too, so
# this default is strictly safer than a region for every model we use.
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
THIS_MACHINE="$(hostname -I | awk '{print $1}')"
export THIS_MACHINE
echo "head ip: $THIS_MACHINE"

# --- preflight ------------------------------------------------------------
# Both checks are seconds. The loop's first elaboration is 20-40 minutes, so
# anything that can be known cheaply is worth knowing before that starts.
if [[ $SKIP_PREFLIGHT -eq 0 ]]; then
    if [[ $NEEDS_LLM -eq 1 && " ${EXTRA[*]-} " != *" --skip-llm "* ]]; then
        say "preflight 1/2: can we reach the model?"
        if ! python scripts/check_llm.py; then
            echo
            echo "The loop needs a model. The shortest path, if the default"
            echo "claude backend is not logged in:"
            echo "    claude            # once, interactively, to log in"
            echo "Or a key-based backend instead:"
            echo "    export GEMINI_API_KEY=...   # https://aistudio.google.com/apikey"
            echo "    scripts/run.sh --backend gemini"
            echo "Other options:  python scripts/check_llm.py --list"
            echo "Or run the harness with no model at all:  scripts/run.sh --skip-llm"
            exit 1
        fi
    fi
    say "preflight 2/2: is the toolchain reachable?"
    python scripts/check_setup.py --quick || {
        echo "A tier the loop needs is missing. Full detail: python scripts/check_setup.py"
        exit 1
    }
fi

# --- cluster --------------------------------------------------------------
# The teardown is a trap, not a trailing command: a crashed or interrupted loop
# must not leave EDA containers holding this host's 30 GB of RAM.
cleanup() {
    local rc=$?
    if [[ $TEAR_DOWN -eq 1 && $BRING_UP -eq 1 ]]; then
        say "tearing the cluster down"
        chia down -y configs/cluster.yaml || true
    fi
    exit $rc
}
trap cleanup EXIT INT TERM

if [[ $BRING_UP -eq 1 ]]; then
    say "bringing the cluster up"
    chia up -y configs/cluster.yaml
fi

# --- the loop -------------------------------------------------------------
say "running $ITERS iterations"
set +e
if [[ $SUBMIT -eq 1 ]]; then
    # Through the job server the driver does NOT inherit this shell, so the
    # backend selection has to be handed over explicitly. The API key still
    # does not travel: agent.make_llm reads it on the driver, and under
    # --submit the driver is on this same host.
    RT_JSON=$(python - <<'PY'
import json, os
# SPARSECRAFT_* already covers SPARSECRAFT_CLAUDE_{BIN,EFFORT,...}. PATH is
# added for the claude backend: the driver execs the bare name `claude`, which
# only resolves through the ~/bin shim, and a job-server driver does not
# inherit this shell.
env = {k: v for k, v in os.environ.items()
       if k.startswith("SPARSECRAFT_") or k in (
           "PATH",
           "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
           "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
           "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
           "GOOGLE_APPLICATION_CREDENTIALS")}
print(json.dumps({"env_vars": env}))
PY
)
    chia job submit --working-dir . --runtime-env-json "$RT_JSON" \
        -- python -u src/loop.py --iters "$ITERS" ${EXTRA[@]+"${EXTRA[@]}"}
else
    python -u src/loop.py --iters "$ITERS" ${EXTRA[@]+"${EXTRA[@]}"}
fi
RC=$?
set -e

say "loop finished (exit $RC)"
LATEST="$(ls -dt runs/*/ 2>/dev/null | head -1 || true)"
[[ -n "$LATEST" ]] && echo "traces: $LATEST"
exit $RC
