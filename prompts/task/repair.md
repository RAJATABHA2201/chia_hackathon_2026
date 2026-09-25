# Repair: iteration ${ITERATION}, attempt ${ATTEMPT} of ${MAX_ATTEMPTS}

A gate failed on the proposer's mutation.

| | |
|---|---|
| verdict | `${VERDICT}` |
| failure class | **${FAILURE_CLASS}** |
| playbook section | **${VERDICT}** in "Failure playbook" |
| re-checked after your turn | ${RECHECK} |

${CLASS_NOTE}

## Evidence

```
${EVIDENCE}
```

## The proposer's stated mechanism

This is what the change was supposed to do. Preserve it.

```
${MUTATION}
```

## Design state

Fields the proposer changed (parent -> proposed). The direction rule applies to
every line here: you may move a value part of the way back, never to or past the
parent value.

```
${MUTATED_FIELDS}
```

Parent (built, simulated, matched the golden reference):

```json
${PARENT_STATE}
```

The failing tree, only where it differs from the parent (the proposer's move,
plus anything an earlier repair attempt changed; `{}` means the difference is
entirely in the RTL source):

```json
${PROPOSED_STATE}
```

Files the proposer changed:

```
${TOUCHED}
```

## Earlier repair attempts this iteration

${PRIOR_ATTEMPTS}

## Your writable set

Every command runs with cwd `${CHIPYARD}`. These three files, nothing else:

```
${PARAMS_PATH}
${PE_PATH}
${ZBU_PATH}
```

Verify every write with
`git -C ${CHIPYARD}/generators/gemmini status --short -- ${PARAMS_PATH} ${PE_PATH} ${ZBU_PATH}`,
and if you touched Chisel, compile before you end your turn:
`cd ${CHIPYARD} && source env.sh && sbt -batch "project gemmini" compile 2>&1 | tail -40`.

One tool call per turn. End with the sections the system prompt requires,
`### ==REPAIR==` last.
