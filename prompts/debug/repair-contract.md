## Required output format

End your response with these sections, in this order. The harness parses the
last one; a missing or malformed `==REPAIR==` block is recorded as a failed
attempt even if your edit was good.

```
## Root cause
One paragraph: which signal, parameter, relation or connection is wrong, and
why it produces exactly the evidence you were given. Cite `file:line`, or the
T0 rule by name. End with your confidence, e.g.
"Confidence: 4/5 -- the capacity relation is violated by 1.4x and the arithmetic is exact."

## Fix
One bullet per edit: `file:line`, what changed, and why the proposer's
mechanism survives it.

## Verification
The evidence that (a) the cause is addressed and (b) the mechanism is intact.
Name what you actually ran and what it returned: "sbt compile: success",
"working set now 0.81x of sp_capacity by direct arithmetic",
"gate_enable marker still 1; the T-A region is still instantiated".

## Self-audit
Answer each honestly. The harness checks the first four itself, so a wrong
answer changes nothing except the record of your honesty.
- Did you change any path outside the three writable files?          yes / no
- Did you move a mutated field back to (or past) its parent value?    yes / no
- Did you disable, bypass or delete the proposer's mechanism?         yes / no
- Did you weaken an assert/require, or touch workload/dense_mode?     yes / no
- Is the proposer's stated mechanism still intact and exercised?      yes / no

### ==REPAIR==
status:     FIXED | NOT_ACTIONABLE
confidence: 1-5
class:      the failure class you were handed
files:      each file you changed, one per line, or `none`
compiled:   PASS | FAIL | NOT_RUN     (the sbt compile you actually ran)
preserved:  yes | no                  (is the proposer's mechanism intact?)
```

The literal string `==REPAIR==` must appear **only** as that final header.

`status: FIXED` means you made an edit you believe addresses the root cause,
and the harness will re-run the ladder on it. `status: NOT_ACTIONABLE` means
you concluded the failure cannot be fixed with the writable set without
reverting the mutation; the harness stops repairing this iteration and does
not spend a rebuild on it. Say which lever you would have needed.

Report `compiled: PASS` only if you ran the compile in this session and read
a successful result. If you edited Chisel and did not compile, that is
`NOT_RUN`, and it is the most avoidable way to waste an attempt.
