# This iteration

You are editing a live Chipyard tree inside the build container. Use the bash
tool; every command runs with cwd `${CHIPYARD}`.

## The one file you may change

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

Do **not** build, elaborate, or run Verilator yourself. The loop does that after
your turn, and doing it by hand burns the container's build lock.

Use `read_status` for the harness-measured state of the current design, and
`query_history` / `get_pareto_front` to see what has already been tried rather
than re-deriving it.
