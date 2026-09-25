#!/usr/bin/env python3
"""One agentic turn against a live cluster, and nothing else.

The cheapest possible test of the thing most likely to be broken: does the
model's turn actually dispatch, reach the MCP tool servers, run a command
inside the build container, and come back? Everything after N10 -- elaboration,
simulation, synthesis -- is minutes to hours, so proving this first is worth a
separate entry point.

    chia up configs/cluster.yaml
    python scripts/smoke_agent.py                    # uses the configured backend
    python scripts/smoke_agent.py --backend custom   # against a fake server

Exit 0 means the loop's agentic path works end to end.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# scripts/ -> ../src: every importable module lives there, flat.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import ray                                                              # noqa: E402
from ray.util.placement_group import placement_group, remove_placement_group  # noqa: E402
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy    # noqa: E402

from chia.base.ChiaFunction import ChiaFunction, get                   # noqa: E402

import agent                                                            # noqa: E402
import constants as C                                                   # noqa: E402

MARKER = "/tmp/sparsecraft_smoke_marker"

PROBE = (
    "This is a connectivity check, not a design task. Do exactly two things "
    "and then stop:\n"
    f"1. Use the bash tool to run:  hostname > {MARKER} && "
    f"ls /home/ray/chipyard | head -3 >> {MARKER} && cat {MARKER}\n"
    "2. Reply with the single word DONE.\n"
    "Do not edit any other file."
)


@ChiaFunction(resources={C.R_CHIPYARD: C.BUILD_FRACTION})
def read_marker(path: str = MARKER) -> str:
    """Read the marker back FROM THE BUILD CONTAINER.

    This is the assertion that matters, and it is deliberately not made by
    reading the model's transcript. The transcript proves the model claims to
    have run something; this proves a shell actually ran it in the same
    container the elaboration will later use. Those are different facts, and
    only the second one makes the write path real.
    """
    import os
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-tools", action="store_true",
                    help="prompt with no tools at all -- isolates the model "
                         "call from the MCP plumbing")
    args = ap.parse_args()

    info = agent.describe(args.backend)
    if not info["ready"]:
        print(f"ABORT: backend {info['backend']!r} is not ready. "
              f"Run: python scripts/check_llm.py --list")
        return 2
    print(f"backend  {info['backend']}/{info['model']}")

    ray.init(address=os.environ.get("RAY_ADDRESS", "auto"),
             runtime_env=C.runtime_env(), ignore_reinit_error=True)
    res = ray.cluster_resources()
    print("cluster resources:",
          {k: v for k, v in sorted(res.items()) if not k.startswith(("node:", "memory", "object_store"))})
    for needed in ("llm", C.R_CHIPYARD, C.R_HEAD_LOCAL):
        if res.get(needed, 0) <= 0:
            print(f"ABORT: no worker advertises {needed!r}. "
                  f"Is the cluster up with configs/cluster.yaml?")
            return 2

    run_dir = Path(C.RUN_DIR) / "smoke"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.md").write_text("# smoke test\nverdict: n/a\n")
    (run_dir / "history.json").write_text('{"iterations": [], "front": []}')

    pg = placement_group([{"CPU": 1, C.R_CHIPYARD: 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    pg_opts = {"scheduling_strategy": PlacementGroupSchedulingStrategy(
        placement_group=pg, placement_group_bundle_index=0)}

    tools = []
    llm = agent.make_llm("system/microarchitect.md", backend=args.backend, model=args.model)
    try:
        if not args.no_tools:
            print("starting the editor BashTool in the chipyard container ...")
            tools.append(agent.make_editor(pg_opts))
            print("starting the sealed status/history tools on the head ...")
            tools += agent.make_sealed_tools(str(run_dir / "status.md"),
                                             str(run_dir / "history.json"))
            for t in tools:
                print(f"  {t.name:22s} {getattr(t, 'hostname', '?')}:"
                      f"{getattr(t, 'port', '?')}")

        print("\ndispatching one agentic turn ...")
        t0 = time.time()
        cli = get(llm.prompt.options(**C.LLM_OPTS).chia_remote(llm, PROBE, tools))
        elapsed = time.time() - t0

        text = cli.result or ""
        # stream_result is the FULL transcript (every turn, including tool
        # results); result is only the final assistant message.
        transcript = getattr(cli, "stream_result", "") or ""
        (run_dir / "smoke.md").write_text(transcript or text)
        print(f"returned in {elapsed:.1f}s, returncode={cli.returncode}, "
              f"success={getattr(cli, 'success', '?')}")
        print("-" * 60)
        print(text[-1500:] if text else "(empty final message)")
        print("-" * 60)

        if args.no_tools:
            ok = bool(text)
            print(f"\nmodel answered: {ok}")
        else:
            tool_called = ("run_command" in transcript
                           or "sparsecraft_edit" in transcript)
            marker = get(read_marker.options(**pg_opts).chia_remote())
            ran_in_container = bool(marker.strip())
            print(f"\nmodel called the editor tool         : {tool_called}")
            print(f"a shell really ran in the container  : {ran_in_container}")
            if ran_in_container:
                first = marker.strip().splitlines()[0]
                print(f"  container hostname: {first}")
                print(f"  chipyard tree visible: "
                      f"{', '.join(marker.strip().splitlines()[1:4])}")
            else:
                print("  Nothing was written. Either the model declined to use "
                      "the tool, or the MCP round-trip failed. Re-run with "
                      "--no-tools to tell those apart.")
            ok = ran_in_container
        print(f"transcript: {run_dir / 'smoke.md'}")
        return 0 if ok else 1
    finally:
        for t in tools:
            try:
                t.stop()
            except Exception:
                pass
        try:
            remove_placement_group(pg)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
