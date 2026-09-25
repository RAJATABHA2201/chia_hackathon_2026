### T-A — Zero-Gated MAC

When a multiplier operand is zero, the product is zero and the accumulator should pass
through unchanged. Detect that, hold the multiplier's input registers so the array does
not toggle, and bypass the accumulator input to the output.

- Lives in `PE.scala`, inside or around the existing `MacUnit`.
- **Bit-exact**: `c + 0·w` is identically `c`. If this changes a single output bit, you
  have a bug, not a trade-off.
- Buys **energy**. Does **not** buy cycles — the array is a fixed-latency pipeline and a
  gated PE still occupies its slot.
- Costs a comparator in the MAC path, so it can cost **Fmax**. That is a real trade and
  the harness will measure it.

Design choices that are genuinely yours: which operand(s) to test; whether to test the
stationary weight, the streaming activation, or both; whether the comparison is
registered (better Fmax, one cycle of latency to absorb) or combinational; whether the
gate is per-PE or shared across a row.
