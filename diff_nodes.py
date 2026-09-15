"""Diff collection, reset and re-application -- the persistence mechanism.

The container filesystem is pure scratch. The ONLY state that crosses an
iteration boundary is the diff blob (plus the LLM session transcript and the
head-side status files). Anything the model produced that is not in the diff is
gone when the container dies.

This follows `examples/common/common_nodes.py:43-166` exactly, with two
SparseCraft-specific points:

  * The submodule list is ``["generators/gemmini"]`` -- Gemmini is a chipyard
    submodule and carries its own chipyard-package configs, so both files the
    loop touches live inside that one repo.
  * Submodules are reset to the commit the PARENT pins (``git ls-tree HEAD
    <sm>``), never to their own HEAD, which a previous debug turn may have moved.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from chia.base.ChiaFunction import ChiaFunction

from constants import BUILD_FRACTION, CHIPYARD_PATH, R_CHIPYARD, SUBMODULES


@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def collect_diff(chipyard_path: str = CHIPYARD_PATH,
                 submodules: Optional[list] = None) -> tuple:
    """Collect git diffs from chipyard and each tracked submodule.

    Returns ``(error, {repo_path: diff_text})``. Key ``""`` is the root chipyard
    diff; ``"generators/gemmini"`` is the submodule's. Submodule diffs are taken
    with ``cwd=<submodule>`` so each is directly ``git apply``-able there.

    ``git add -N .`` first so newly created files (SparseCraftParams.scala on the
    first iteration) appear in the diff at all; the index is reset afterwards.
    """
    submodules = SUBMODULES if submodules is None else submodules

    for sm in submodules:
        sm_path = os.path.join(chipyard_path, sm)
        if not os.path.exists(os.path.join(sm_path, ".git")):
            return (1, {})

    diffs: dict = {}
    root = subprocess.run(["git", "diff", "--ignore-submodules=all"],
                          cwd=chipyard_path, capture_output=True, text=True)
    diffs[""] = root.stdout

    for sm in submodules:
        sm_path = os.path.join(chipyard_path, sm)
        subprocess.run(["git", "add", "-N", "."], cwd=sm_path, check=False)
        d = subprocess.run(["git", "diff"], cwd=sm_path,
                           capture_output=True, text=True)
        diffs[sm] = d.stdout
        subprocess.run(["git", "reset"], cwd=sm_path, check=False)

    return (0, diffs)


@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def reset_and_apply_diff(diff_dict: dict, chipyard_path: str = CHIPYARD_PATH,
                         submodules: Optional[list] = None) -> tuple:
    """Reset chipyard + submodules to their pinned commits, then apply diff_dict.

    This is what lets iteration N+1 resume on a *different* container from the
    one that produced the edit.
    """
    submodules = SUBMODULES if submodules is None else submodules

    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=chipyard_path, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=chipyard_path, check=False)

    for sm in submodules:
        sm_path = os.path.join(chipyard_path, sm)
        if not os.path.isdir(sm_path):
            continue
        # Reset to the commit the PARENT records, not the submodule's own HEAD:
        # a previous debug turn may have moved HEAD and it would silently stick.
        r = subprocess.run(["git", "ls-tree", "HEAD", sm], cwd=chipyard_path,
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            subprocess.run(["git", "reset", "--hard", r.stdout.split()[2]],
                           cwd=sm_path, check=False)
        else:
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=sm_path, check=False)
        subprocess.run(["git", "clean", "-fd"], cwd=sm_path, check=False)

    if not diff_dict:
        return (0, "clean state - no diff to apply")

    if diff_dict.get(""):
        r = subprocess.run(["git", "apply", "--ignore-whitespace"],
                           input=diff_dict[""], text=True,
                           cwd=chipyard_path, capture_output=True)
        if r.returncode != 0:
            return (1, f"root git apply failed: {r.stderr}")

    for sm in submodules:
        if diff_dict.get(sm):
            r = subprocess.run(["git", "apply", "--ignore-whitespace"],
                               input=diff_dict[sm], text=True,
                               cwd=os.path.join(chipyard_path, sm),
                               capture_output=True)
            if r.returncode != 0:
                return (1, f"{sm} git apply failed: {r.stderr}")

    return (0, "applied diff successfully")


@ChiaFunction(resources={R_CHIPYARD: BUILD_FRACTION})
def changed_paths(chipyard_path: str = CHIPYARD_PATH,
                  submodules: Optional[list] = None) -> list:
    """Chipyard-rooted paths the working tree has modified.

    Feeds the N13 scope allowlist: the orchestrator refuses the diff if this
    names anything outside the writable set, BEFORE it is stored or re-applied.
    """
    submodules = SUBMODULES if submodules is None else submodules
    out = []
    r = subprocess.run(["git", "status", "--porcelain", "--ignore-submodules=all"],
                       cwd=chipyard_path, capture_output=True, text=True)
    out += [ln[3:].strip() for ln in r.stdout.splitlines() if ln.strip()]
    for sm in submodules:
        sm_path = os.path.join(chipyard_path, sm)
        if not os.path.isdir(sm_path):
            continue
        r = subprocess.run(["git", "status", "--porcelain"], cwd=sm_path,
                           capture_output=True, text=True)
        out += [os.path.join(sm, ln[3:].strip())
                for ln in r.stdout.splitlines() if ln.strip()]
    return out
