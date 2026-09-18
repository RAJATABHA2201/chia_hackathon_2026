#!/usr/bin/env bash
# Final chain: wait for the in-flight arm, then greedy, then random, then analyse.
# Greedy before random: greedy is the control the paper's claim rests on;
# random only shows the space is not trivial. If the night runs short, the
# arm that matters has already run.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
RUNS=/home/chia-sparsecraft/runs
ITERS="${ITERS:-12}"
say() { printf '\n\033[1m== [%s] %s\033[0m\n' "$(date +%H:%M)" "$*"; }
wait_free() { while pgrep -f 'python -u loop.py' >/dev/null; do sleep 30; done; }

say "waiting for the in-flight arm"; wait_free

for spec in "greedy-1:--proposer greedy" "random-1:--proposer random"; do
    name="${spec%%:*}"; flags="${spec#*:}"
    [ -f "$RUNS/$name/history.json" ] && { say "$name done already -- skip"; continue; }
    say "starting $name ($ITERS iterations)"
    # shellcheck disable=SC2086
    ./run.sh $flags --iters "$ITERS" --no-up --no-down -- --run-name "$name" \
        > "$RUNS/$name.log" 2>&1
    say "$name exited $?"
    wait_free
    say "interim analysis"
    /home/rajatabha/miniforge3/envs/chia_env/bin/python analyze.py \
        $(ls -d $RUNS/agent-1 $RUNS/greedy-1 $RUNS/random-1 2>/dev/null) \
        --out /home/chia-sparsecraft/paper 2>&1 | tail -25
done
say "all arms done -- /home/chia-sparsecraft/paper"
