# archive/ — not loaded at runtime, kept for provenance

Nothing here is opened by any code path in `agent.py` or `loop.py`. It is here
so that "why does this prompt say that?" has an answer inside the repo, and so
a V1-vs-V2 diff can be read against the thing it was taken from.

| directory | what it is |
|---|---|
| `v1-prompts-verbatim/` | V1's whole `prompts/` tree, byte-for-byte. The diff baseline. |
| `v1-prompt-material/` | V1's `reference/` and `harvest/`: source prompts that devices were grafted out of, plus the four prompts V1 shipped and never loaded (`triage`, `chisel-debugging`, `debug_rtl`, `emit-patch`). |
| `v2-superseded/` | V2 files replaced during V2 development. `repairer.v2-draft.md` is the first N73 system prompt, superseded by `prompts/system/repairer.md` + `prompts/debug/*` (see `docs/v1-to-v2.md` §3.1 for why). |
| `legacy-kernels/` | `attn_prefill.c`, the kernel from the attention-era project. Nothing reads it; the loop's measurement instrument is `kernels/spmm.c`. |

Two of those four were promoted into live nodes in V2 and now live under
`prompts/system/`: `adapted/repair.md` became `system/repairer.md` (N73), and
`custom/diagnose-unclassified.md` became `system/diagnostician.md`. The copies
here are the originals. `system/repairer.md` has since been rewritten; its
first V2 form is in `v2-superseded/`.
