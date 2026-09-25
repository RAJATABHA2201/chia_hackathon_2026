<!--
NODE: N12 Emit Patch (agentic, W_agent). Runs after N11 Select Candidate has
      chosen one mutation from the K>=3 that N10 proposed, and immediately
      before N13 Patch Scope Check & Apply.

PURPOSE: Turn exactly one selected mutation into a unified diff against
      SparseCraftParams.scala, and return it. This node does not write to the
      source tree -- that is the whole point of splitting it out of N10. The
      orchestrator applies the returned diff in a scratch git worktree only
      after check_patch_scope has cleared it, which is what makes the
      one-command replay claim true and where the L-infinity deny-list is
      actually enforced rather than merely requested.

INPUTS: the selected mutation (typed parameter delta, with the mechanism N10
      stated for it); the current design state; the contents of
      SparseCraftParams.scala; the writable-path allowlist.

OUTPUTS: a unified diff touching only
      generators/gemmini/src/main/scala/gemmini/SparseCraftParams.scala,
      or a typed parameter delta. Per review 3.2 the contract is
      "unified diff | typed param delta" -> patch_id. The four
      `// SPARSECRAFT block_size / tile_m / tile_n / tile_k` marker lines must
      survive well-formed; the harness parses the tiling out of them and a
      mangled marker scores the iteration against the wrong design.

SOURCE: SparseCraft_Technical_Review.md 3.1 SPLIT (node 3 "Propose edit via MCP
      tools" -> N12 Emit Patch + N13 Patch Scope Check & Apply); 2.2 row (c),
      which notes that node 3 as drawn implies the agent has write access to
      the source tree; 3.2 node table and the N12 -> N13 edge.

STATUS: stub — body not yet written
-->
