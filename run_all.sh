#!/usr/bin/env bash
# All three arms, in order, one at a time. Analyse after each.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
R=/home/chia-sparsecraft/runs
I="${ITERS:-12}"
say() { printf '\n\033[1m== [%s] %s\033[0m\n' "$(date +%H:%M)" "$*"; }
free_now() { ! ps -eo comm=,args= | awk '($1=="python"||$1=="python3") && index($0,"loop.py")' | grep -q .; }
wait_free() { until free_now; do sleep 30; done; }

wait_free
for spec in "agent-1:--backend vertex --proposer agent" \
            "greedy-1:--proposer greedy" \
            "random-1:--proposer random"; do
    n="${spec%%:*}"; f="${spec#*:}"
    [ -f "$R/$n/history.json" ] && { say "$n already done -- skip"; continue; }
    say "starting $n ($I iterations)"
    # shellcheck disable=SC2086
    ./run.sh $f --iters "$I" --no-up --no-down -- --run-name "$n" > "$R/$n.log" 2>&1
    say "$n exited $?"
    wait_free
    say "interim analysis after $n"
    /home/rajatabha/miniforge3/envs/chia_env/bin/python analyze.py \
        $(ls -d $R/agent-1 $R/greedy-1 $R/random-1 2>/dev/null) \
        --out /home/chia-sparsecraft/paper 2>&1 | tail -22
done
say "ALL ARMS DONE -- /home/chia-sparsecraft/paper"
