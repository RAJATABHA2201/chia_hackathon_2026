# harvest/ — source material, not prompts

**Nothing in this directory is loaded at runtime.** No code path in `agent.py`
or `loop.py` opens these files, and none of them is a prompt for any node.

They are the five reference prompts that `docs/prompt_reuse_inventory.md` marks
ADAPT but whose destination is *"merge into"*, *"fold into"*, or *"revision
of"* an existing prompt rather than a file of their own. We take specific
devices out of them and graft those into `../propose.md`, `../task.md` and
`../adapted/repair.md`; the rest is discarded.

They are vendored here, byte-for-byte and never edited, so that the adaptation
commit's diff can be read against the thing it was taken from, and so that
"why does `propose.md` have this section?" has an answer inside this repo.

| File | Devices taken | Grafted into |
|---|---|---|
| `engineer-charter-and-scope.md` | `Environment:` → `SCOPE` → `Rules:` skeleton; cheat paths enumerated by name; "there is no human to ask"; non-action as a legitimate outcome | `../propose.md` |
| `microarchitect-charter-and-tools.md` | charter framing and loop mechanics, retargeted from ISA extension to parameter mutation | `../propose.md` |
| `cycle-alignment-node.md` | three devices only, of 421 lines: counter↔stat mapping table, sentinel-delimited output schema, and the `Warnings` section (its rules are container facts, not gem5 facts) | `../propose.md` |
| `divergence-triage.md` | triage framing for "measured behaviour diverged from reference" | `../adapted/repair.md` |
| `per-iteration-work-order.md` | ordering discipline only — target, then config, then failing artifacts | `../task.md` |

## Provenance

Copied from `/home/chia-sparsecraft/chia/examples/`. That tree is not itself a
git checkout, but `chia-work/.upstream-commit` records upstream
`16c35e92aaaf9511c6453bf94cd5cf589698f4e3`, and the inventory verified by md5
that all 33 `.md`/`.txt` files under `chia/examples` and `chia-work/examples`
are byte-identical.

Original paths, in the table's order:

```
timing_opt      is NOT the source of any file here; see ../adapted/ and ../as-is/
circt_issue_solver/prompts/system.md          ->  engineer-charter-and-scope.md
riscv_extensions/prompts/system.md            ->  microarchitect-charter-and-tools.md
gem5_align/prompts/align_node_prompt.md       ->  cycle-alignment-node.md
riscv_extensions/prompts/debug.md             ->  divergence-triage.md
riscv_extensions/prompts/task.md              ->  per-iteration-work-order.md
```

The two `system.md` files were renamed by their differing emphasis (scope rules
vs. loop mechanics and tool roster) because a flat copy would collide.
