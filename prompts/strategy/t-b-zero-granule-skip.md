### T-B — Zero-Granule Skip (the ZBU)

Detect all-zero **granules** as data is written into the scratchpad, record one bit per
granule in a bitmap, and at execute time do not push a flagged granule through the mesh.
When every granule of an operand tile is flagged, the granule's scratchpad read is
suppressed and a hard zero is muxed in.

**Read this before you predict a speedup.** In THIS build the ZBU saves no cycles.
`sc_skip` gates exactly one thing -- `mem.read(raddr, ren && !sc_skip)` in
`Scratchpad.scala`. The surrounding `ren`, `q.io.enq.valid` and
`io.read.req.ready` are stock, so a skipped granule still occupies its pipeline
slot and still returns a response. The `preload`+`compute` suppression that
would buy `2 x DIM` cycles needs execute-side gating that is NOT wired, and
`ExecuteController.scala` is not in your writable set. Predict `time: flat` and
do not spend the iteration hunting for a cycle win that the wiring cannot
deliver.

- Lives in `SparseCraftSparsity.scala`, which is **yours**. The three integration points
  (`Scratchpad.scala` mvin tap, `ExecuteController.scala` query, `CounterFile.scala`
  event ids) are written by the harness, are **not** in your writable set, and their
  interface is documented at the top of `SparseCraftSparsity.scala`. Match that interface.
- **Also bit-exact.** Skipping a contribution that is identically zero changes nothing.
  An off-by-one in the bitmap, however, silently drops *real* data and produces a wrong
  answer that looks like a spectacular speedup. The harness checks every output against a
  golden scalar reference and rejects the iteration on any mismatch.
- Buys **cycles**, and energy, and off-chip bytes if the store side is skipped too.
- Costs **area** (the bitmap is storage) and possibly **Fmax** (a lookup on the execute
  critical path).

Design choices that are genuinely yours, and this is where the search actually lives:

- **Granularity.** A granule can be one element, a group of G elements within a row, a
  whole row, or a whole DIM x DIM tile. Fine granularity catches more zeros and costs more
  bitmap. Coarse granularity is cheap and catches almost nothing on unstructured data.
  This must divide the array dimension.
- **Which operand.** A only, the stationary operand only, or both.
- **Where the detector sits.** On the mvin write path (detect once, reuse many times) or
  on the scratchpad read path (no storage, repeated work).
- **Bitmap organisation.** Flat register file, banked SRAM, or reuse of an existing
  structure.
- **Detector pipeline depth.** A wide OR-reduce over a full row is a long combinational
  path; splitting it costs latency and buys Fmax.
