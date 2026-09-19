# This iteration (${ITERATION} of ${BUDGET})

Propose and implement **one coherent microarchitectural change** that answers the
diagnosis below, then compile it.

## Your writable set

Every command runs with cwd `${CHIPYARD}`, inside the build container. These three files,
and nothing else:

```
${PARAMS_PATH}     config: the GemminiArrayConfig design point
${PE_PATH}         T-A: the PE and its MacUnit -- zero-gating lives here
${ZBU_PATH}        T-B: the Zero-Bitmap Unit -- yours to write and rewrite
```

The N13 allowlist runs over `git status` before your diff is collected. Anything outside
those three paths rejects the iteration with no evaluation. In particular the three ZBU
integration hooks — `Scratchpad.scala`, `ExecuteController.scala`, `CounterFile.scala` —
are harness-owned. Read them to learn the interface; do not edit them.

## Current design state

```json
${PARENT_STATE}
```

## Measured feedback from the last iteration

This is the concrete artifact your change has to answer. Read it before choosing anything.

```
${DIAGNOSIS}
```

### Counters

```
${COUNTERS}
```

## What to do

1. **Read before you write.** Pull `${ZBU_PATH}` and the interface comment at its head; if
   you are touching T-A, read `${PE_PATH}`. One `cat` per turn — the transport on this host
   drops second-and-later tool results.

2. **Change one coherent thing.** A single mechanism, or a deliberately coupled pair if
   you state the coupling and reuse a `plan_id`. Changing the granule size *and* the array
   dimension *and* the scratchpad in one turn produces a result you cannot attribute, which
   is worth less than a smaller move you can.

3. **Write whole files with a heredoc. Never `sed` Chisel.**

   ```bash
   cat > ${ZBU_PATH} <<'EOF'
   ... complete file ...
   EOF
   ```

   `sed` exits 0 when it matches nothing. A silently failed edit is scored as a duplicate
   of your parent after the harness has paid 20–40 minutes for the build. This has already
   happened in this project.

4. **Verify the edit landed.**

   ```bash
   git diff --stat -- ${PARAMS_PATH} ${PE_PATH} ${ZBU_PATH}
   ```

   No output means no edit. Fix it before continuing.

5. **Compile, and do not end your turn on a failure.**

   ```bash
   cd ${CHIPYARD} && source env.sh && sbt -batch "project gemmini" compile 2>&1 | tail -40
   ```

   About 20 seconds, against the ~18 minutes an elaboration costs. Read the error, fix it, compile again.
   Report the outcome in `compiled:`.

6. If you touched `${PARAMS_PATH}`, keep every field explicit — do not collapse it back to
   `GemminiConfigs.leanConfig`. The file should read as the complete design point, and the
   harness parses the typed state back out of it.

7. End with the `==MUTATION==` and `==PREDICTION==` sections the system prompt requires.

## What not to do

Do not elaborate, do not run Verilator, do not `git commit`. The loop does all of that
after your turn; doing it by hand burns the container's build lock and the time budget.

Use `sparsecraft_status__sparsecraft_status_read_status` for the harness-measured state,
and `sparsecraft_history__sparsecraft_history_query_history` /
`sparsecraft_history__sparsecraft_history_get_pareto_front` to see what has already been
tried rather than re-deriving it. One call per turn.
