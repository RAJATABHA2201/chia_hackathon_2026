## Required output format

End your response with `==CANDIDATES==`, then `==MUTATION==`, then
`==PREDICTION==`.

```
### ==CANDIDATES==
List the distinct moves you considered this turn, including the one you went on
to implement. The work order for this iteration says how many. One block each,
separated by a blank line:

technique: T-A | T-B | BOTH | CONFIG
change:    the lever and its value, `name: old -> new`, or the mechanism in one line
rationale: which counter or measurement makes you expect this to help, in one line
time:      better | worse | flat
energy:    better | worse | flat
area:      better | worse | flat

Three genuinely different moves, not one move at three magnitudes. If you can
only justify two, say so and give two -- a padded third is worse than a short
list, because the harness keeps the ones you did not implement and may hand
one back when the search stalls.
```


End your response with exactly these two sections. The literal strings `==MUTATION==` and
`==PREDICTION==` must appear **only** as section headers — do not mention them in your
reasoning above.

```
### ==MUTATION==
technique: T-A | T-B | BOTH | CONFIG | NONE
files:     the files you changed, one per line
change:    what you changed, in mechanism terms, 1-4 lines. For a config knob,
           `name: old -> new`. For RTL, name the structure you added, moved or
           resized -- not a diff.
compiled:  PASS | FAIL   (the result of the sbt compile you ran)
If you changed nothing because the diagnosis was not actionable, write "NONE" and
one line saying which lever you would have needed.

### ==PREDICTION==
The mechanism you expect, in one or two sentences: which counter moves, in which
direction, and why that follows from the change you made.
Then the predicted direction on each objective, one line each:
  time:   better | worse | flat
  energy: better | worse | flat
  area:   better | worse | flat
  fmax:   better | worse | flat
If this is one step of a coupled move whose first step regresses, name the plan_id
you are reusing and say which step this is.
```

The harness records your prediction against the measured result. A stated prediction that
turns out wrong is more useful than an unstated one — it is how the loop learns which
mechanisms you model well. Do not hedge every axis to "flat" to avoid being wrong.
