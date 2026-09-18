#!/usr/bin/env bash
# Run the remaining arms after whatever is currently in flight.
# Arms cannot overlap -- one chipyard container, one tree.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
RUNS=/home/chia-sparsecraft/runs
ITERS="${ITERS:-12}"
say() { printf '\n\033[1m== [%s] %s\033[0m\n' "$(date +%H:%M)" "$*"; }
wait_free() { while pgrep -f 'python -u loop.py' >/dev/null; do sleep 30; done; }

say "waiting for the in-flight arm"
wait_free

for spec in "agent-1:--backend vertex --proposer agent" "random-1:--proposer random"; do
    name="${spec%%:*}"; flags="${spec#*:}"
    [ -f "$RUNS/$name/history.json" ] && { say "$name done already -- skip"; continue; }
    say "starting $name"
    # shellcheck disable=SC2086
    ./run.sh $flags --iters "$ITERS" --no-up --no-down -- --run-name "$name" \
        > "$RUNS/$name.log" 2>&1
    say "$name exited $?"
    wait_free
done

say "analysing"
/home/rajatabha/miniforge3/envs/chia_env/bin/python analyze.py \
    $(ls -d $RUNS/agent-1 $RUNS/greedy-1 $RUNS/random-1 2>/dev/null) \
    --out /home/chia-sparsecraft/paper 2>&1 | tail -40
say "done -- /home/chia-sparsecraft/paper"
