# This iteration

Propose one coherent mutation to the Gemmini design state that answers the
diagnosis below, and write it into `SparseCraftParams.scala`.

## Where you work

Every command runs with cwd `${CHIPYARD}`, inside the build container.

The one file you may change:

```
${PARAMS_PATH}
```

That is the **entire writable set**. The harness runs a path allowlist over
`git status` before it collects your diff — touching anything else gets the
iteration rejected and the tree reset, with no evaluation. Do not edit the
harness, the kernel, the golden reference, or any other Chisel source.

If `${HARNESS_PATH}` does not exist yet, create it exactly once with:

```scala
// SparseCraft harness config — written once, never mutated.
package chipyard

import org.chipsalliance.cde.config.Config

class SparseCraftConfig extends Config(
  new gemmini.LeanGemminiConfig(gemmini.SparseCraftParams.config) ++
  new freechips.rocketchip.rocket.WithNHugeCores(1) ++
  new chipyard.config.WithSystemBusWidth(128) ++
  new chipyard.config.AbstractConfig)
```

## Current design state

```json
${PARENT_STATE}
```

## Measured feedback from the last iteration

This is the concrete artifact your mutation has to answer. Read it before
choosing a lever.

```
${DIAGNOSIS}
```

## What to do

1. Read the current `${PARAMS_PATH}`.
2. Change **one** coherent thing — a single lever, or a deliberately coupled
   pair if you state that intent in a comment.
3. Keep the four `// SPARSECRAFT block_size / tile_m / tile_n / tile_k` marker
   lines well-formed. The harness parses your tiling out of them; mangle them
   and the iteration is scored against the wrong design.
4. Keep every field explicit. Do not collapse the config back to
   `GemminiConfigs.leanConfig` — the file should read as the complete design point.
5. **The fields are column-aligned, with a variable number of spaces before
   the `=`.** A line looks like `    sp_banks        = 4,` — so a pattern such
   as `s/sp_banks = 4/.../` matches NOTHING, and `sed` exits 0 having done
   nothing. Anchor on the name and allow any spacing:

   ```
   sed -i -E 's/^(\s*sp_banks\s*=\s*)[0-9]+/\18/' ${PARAMS_PATH}
   ```

6. **Verify the edit landed before you end your turn.** `sed` reports success
   whether or not it matched. Read the line back:

   ```
   grep -nE '^\s*sp_banks\s*=' ${PARAMS_PATH}
   ```

   If it still shows the old value, your pattern did not match — fix it and
   retry. An iteration whose edit silently failed is scored as a duplicate of
   its parent and the whole 30-50 minutes is wasted.
7. End your turn with the `==MUTATION==` and `==PREDICTION==` sections the
   system prompt requires.

Do **not** build, elaborate, or run Verilator yourself. The loop does that after
your turn, and doing it by hand burns the container's build lock.

Use `sparsecraft_status__sparsecraft_status_read_status` for the harness-measured state of the
current design, and `sparsecraft_history__sparsecraft_history_query_history` /
`sparsecraft_history__sparsecraft_history_get_pareto_front` to see what has already been tried
rather than re-deriving it.
