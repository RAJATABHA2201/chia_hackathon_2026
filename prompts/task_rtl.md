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

4. **Verify the edit landed. The command must be exactly this one:**

   ```bash
   git -C ${CHIPYARD}/generators/gemmini status --short -- ${PARAMS_PATH} ${PE_PATH} ${ZBU_PATH}
   ```

   ` M` means modified, `??` means a new file you created. Either one means the edit
   landed. No output at all means no edit.

   **Every part of that command matters, and the obvious shorter forms all silently
   report success as failure:**

   - `git -C .../generators/gemmini` — your cwd is `${CHIPYARD}`, the SUPERPROJECT, and
     `generators/gemmini` is a git SUBMODULE. Git does not descend into a submodule for a
     path-limited status, so the same command run from `${CHIPYARD}` prints nothing no
     matter what you changed. Absolute paths are fine once `-C` points at the submodule.
   - `status`, not `diff` — `${PARAMS_PATH}` and `${ZBU_PATH}` are UNTRACKED (the harness
     seeds the first, you create the second, and the tree reset removes both every
     iteration). `git diff` never shows untracked files.

   An agent run lost an entire iteration to this: it wrote the file correctly, ran the
   wrong verification, saw nothing, rewrote it three times, concluded "the file writing
   operation appears to be non-functional", and reported `technique: NONE` for a change
   it had in fact made. If in doubt, skip git and read the file back:

   ```bash
   grep -n 'SPARSECRAFT' ${PARAMS_PATH}
   ```

5. **Compile, and do not end your turn on a failure.**

   ```bash
   cd ${CHIPYARD} && source env.sh && sbt -batch "project gemmini" compile 2>&1 | tail -40
   ```

   About 20 seconds, against the ~18 minutes an elaboration costs. Read the error, fix it, compile again.
   Report the outcome in `compiled:`.

6. If you touched `${PARAMS_PATH}`, keep every field explicit — do not collapse it back to
   `GemminiConfigs.leanConfig`. The file should read as the complete design point, and the
   harness parses the typed state back out of it.

   **The RTL toggles are MARKER COMMENTS, not constructor fields.** `gate_enable`,
   `zbu_enable`, `granule_size` and `zbu_operand` are not members of
   `GemminiArrayConfig`. They live as `// SPARSECRAFT <name> = <int>` lines in the
   header of `${PARAMS_PATH}`, and the harness parses them back out of the comments.
   To enable T-A you edit the marker:

   ```
   // SPARSECRAFT gate_enable = 1      <- 1 enables, 0 disables
   ```

   Writing `gate_enable = true,` inside the `.copy(...)` call instead is a Scala type
   error — `unknown parameter name: gate_enable` — and it fails the compile gate. That
   has already cost an iteration in this project. Keep every `// SPARSECRAFT` line
   present and well-formed: a missing one is not "unchanged", it is unparseable.

   **`workload` and `dense_mode` are frozen.** They are `// SPARSECRAFT` markers too,
   but T0 rejects any iteration that changes them, because they choose the BENCHMARK
   rather than the hardware — a design measured on a different matrix is comparable to
   nothing. Reproduce those two lines exactly as you found them.

7. **The design-state JSON field names are NOT the Scala parameter names.** The state
   above is the harness's vocabulary; `${PARAMS_PATH}` is Chisel. They differ, e.g.

   ```
   state JSON            SparseCraftParams.scala
   sp_capacity_kb: 256   sp_capacity     = CapacityInKilobytes(256),
   acc_capacity_kb: 64   acc_capacity    = CapacityInKilobytes(64),
   gate_enable: false    // SPARSECRAFT gate_enable = 0      (a marker, not a field)
   ```

   So do not reconstruct this file from the JSON — you will invent parameter names that
   do not exist and fail the compile gate, which has already happened twice here
   (`unknown parameter name: sp_capacity_kb`). `cat` the file first and rewrite it with
   ONLY the values you mean to change, preserving its exact structure.

8. End with the `==MUTATION==` and `==PREDICTION==` sections the system prompt requires.

## What not to do

Do not elaborate, do not run Verilator, do not `git commit`. The loop does all of that
after your turn; doing it by hand burns the container's build lock and the time budget.

Use `sparsecraft_status__sparsecraft_status_read_status` for the harness-measured state,
and `sparsecraft_history__sparsecraft_history_query_history` /
`sparsecraft_history__sparsecraft_history_get_pareto_front` to see what has already been
tried rather than re-deriving it. One call per turn.
