## Warnings — facts about this host, not about accelerator design

- **Issue at most one tool call per turn.** The MCP streamable-HTTP transport on this host
  drops second-and-later results on the same session: the server returns 200 on an empty
  stream and your turn then waits forever for results that never arrive. Wait for one
  result before issuing the next call. This is a live defect, not a style preference.
- **Do not grep the chipyard root.** It is >10 GB with build artifacts and will hang the
  bash tool. Start inside `generators/gemmini/`.
- **Do not run `git commit`.** The loop captures your diff from working-tree state.
- **Do not elaborate or run Verilator yourself.** `sbt compile` is sanctioned and expected;
  a full build is not. It burns the container's build lock and 20–40 minutes.
- Stderr-silencing redirects (`2>/dev/null`, `>/dev/null`, `2>&1`) are fine and are not
  treated as writes.
