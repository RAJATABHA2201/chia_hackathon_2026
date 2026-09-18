#!/usr/bin/env bash
# Chain the arms so the night runs itself.
#
# The arms CANNOT overlap: each reserves the single `chipyard` container and
# edits the tree inside it, so two at once race on SparseCraftParams.scala.
# This waits for one to exit before starting the next, then analyses whatever
# exists -- so an arm that dies still leaves the earlier results intact and
# analysed.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
RUNS=/home/chia-sparsecraft/runs
ITERS="${ITERS:-12}"

wait_for_loop() {                       # block until no loop.py is running
    while pgrep -f 'python -u loop.py' >/dev/null; do sleep 30; done
}

say() { printf '\n\033[1m== [%s] %s\033[0m\n' "$(date +%H:%M)" "$*"; }

say "waiting for any in-flight arm to finish"
wait_for_loop

for spec in "greedy:--proposer greedy" "random:--proposer random"; do
    name="${spec%%:*}"; flags="${spec#*:}"
    if [ -f "$RUNS/$name-1/history.json" ]; then
        say "$name-1 already has results -- skipping"
        continue
    fi
    say "starting $name-1 ($ITERS iterations)"
    # shellcheck disable=SC2086
    ./run.sh $flags --iters "$ITERS" --no-up --no-down \
        -- --run-name "$name-1" > "$RUNS/$name-1.log" 2>&1
    say "$name-1 finished (exit $?)"
    wait_for_loop
done

say "analysing everything that completed"
/home/rajatabha/miniforge3/envs/chia_env/bin/python analyze.py \
    $(ls -d $RUNS/agent-1 $RUNS/greedy-1 $RUNS/random-1 2>/dev/null) \
    --out /home/chia-sparsecraft/paper 2>&1 | tail -40

say "done -- CSVs and summary.md are in /home/chia-sparsecraft/paper"
