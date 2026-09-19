#!/usr/bin/env bash
# Measured area for the designs the figures need, in priority order.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
PY=/home/rajatabha/miniforge3/envs/chia_env/bin/python
R=/home/chia-sparsecraft/runs
P=/home/chia-sparsecraft/paper
say(){ printf '\n\033[1m== [%s] %s\033[0m\n' "$(date +%H:%M)" "$*"; }
busy(){ ps -eo comm=,args= | awk '($1=="python"||$1=="python3") && (index($0,"loop.py")||index($0,"synth_front.py"))' | grep -q .; }
wait_free(){ while busy; do sleep 30; done; }

wait_free
# --all on agent-1: gets the BASELINE too, which the front excludes because it
# was dominated -- without it there is no reference to measure the delta from.
say "agent-1 all designs (includes baseline)"
$PY synth_front.py $R/agent-1 --all --out $P/synth_agent_all.csv 2>&1 | tail -20
wait_free
say "greedy-1 front"
$PY synth_front.py $R/greedy-1 --out $P/synth_greedy.csv 2>&1 | tail -20
wait_free
say "random-1 front"
$PY synth_front.py $R/random-1 --out $P/synth_random.csv 2>&1 | tail -20
say "SYNTHESIS DONE -- $P"
