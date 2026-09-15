#!/usr/bin/env python
"""Repair two defects in the released hammer 1.2.0 that block the open flow.

1. The NanGate45 technology description.

hammer's pydantic models require ``grid_unit`` on the Stackup AND on every
Metal -- ``hammer/tech/stackup.py`` runs
``Decimal(str(values.get("grid_unit")))`` inside a Metal validator. The sky130
plugin shipped in that same release carries both fields; nangate45 carries
neither, so merely naming ``hammer.technology.nangate45`` aborts with
``decimal.InvalidOperation`` before any tool is invoked. That is a defect in
the released plugin, not in our configuration.

   Nothing is invented here: the value copied down is the ``grid_unit`` that
   ``nangate45.tech.json`` already declares at its top level.

2. The yosys plugin's flip-flop mapping.

   ``hammer/synthesis/yosys/__init__.py`` emits
   ``dfflibmap -map-only -liberty <lib>``. ``-map-only`` tells yosys to map
   only internal FF types that ALREADY exactly match a library cell, and to
   convert nothing. NanGate45 has no enable- or sync-reset flop, so every
   ``$_SDFFE_*`` / ``$_DFFE_*`` survives into the mapped netlist as a yosys
   internal cell. The netlist still "succeeds"; it just silently reports
   ``sequential elements: 0.000000`` and an area with every register missing.
   On a systolic array that is most of the design.

   Dropping the flag lets dfflibmap do the type conversion it was going to
   have to do anyway. Nothing else about the emitted script changes.

Both edits are idempotent, so a future hammer that fixes them upstream still
builds cleanly.
"""

import importlib.util
import json
import pathlib
import sys
from decimal import ROUND_HALF_UP, Decimal


def main() -> int:
    spec = importlib.util.find_spec("hammer.technology.nangate45")
    if spec is None or not spec.submodule_search_locations:
        print("hammer.technology.nangate45 not importable", file=sys.stderr)
        return 1
    tech_json = pathlib.Path(spec.submodule_search_locations[0]) / "nangate45.tech.json"
    d = json.loads(tech_json.read_text())

    grid = d.get("grid_unit")
    if grid is None:
        print("no top-level grid_unit to copy down", file=sys.stderr)
        return 1

    gu = Decimal(str(grid))

    def snap(value):
        """Round to the nearest multiple of the grid unit, hammer's own rule."""
        v = Decimal(str(value))
        return v.quantize(Decimal(1)) if gu == 0 else (v / gu).quantize(
            Decimal(1), rounding=ROUND_HALF_UP) * gu

    changed = 0
    snapped = 0
    for stackup in d.get("stackups", []):
        if "grid_unit" not in stackup:
            stackup["grid_unit"] = grid
            changed += 1
        for metal in stackup.get("metals", []):
            if "grid_unit" not in metal:
                metal["grid_unit"] = grid
                changed += 1
            # Second defect in the same file: several widths are not multiples
            # of the declared grid. `max_width: 1073741.8235` is a "practically
            # infinite" sentinel (2^30/1000) that happens to land off-grid, and
            # hammer's Metal validator rejects the whole technology for it.
            # These are the exact fields that validator checks.
            for field in ("min_width", "pitch", "offset", "max_width"):
                if metal.get(field) is None:
                    continue
                want = snap(metal[field])
                if Decimal(str(metal[field])) != want:
                    metal[field] = float(want)
                    snapped += 1
            table = metal.get("power_strap_width_table") or []
            for i, width in enumerate(table):
                want = snap(width)
                if Decimal(str(width)) != want:
                    table[i] = float(want)
                    snapped += 1

    if changed or snapped:
        tech_json.write_text(json.dumps(d, indent=1))
        print(f"added {changed} grid_unit field(s), snapped {snapped} "
              f"off-grid width(s) in {tech_json}")
    else:
        print("nangate45.tech.json is already consistent with hammer's schema")

    # Prove the technology now loads, rather than trusting the edit.
    from hammer.tech import HammerTechnology
    tech = HammerTechnology.load_from_module("hammer.technology.nangate45")
    print("nangate45 loads:", tech.config.name,
          "stackups:", [s.name for s in tech.config.stackups])

    return patch_yosys_dfflibmap()


def patch_yosys_dfflibmap() -> int:
    """Drop ``-map-only`` from the yosys plugin's dfflibmap invocation."""
    spec = importlib.util.find_spec("hammer.synthesis.yosys")
    if spec is None or not spec.origin:
        print("hammer.synthesis.yosys not importable", file=sys.stderr)
        return 1
    src = pathlib.Path(spec.origin)
    text = src.read_text()

    before = 'dfflibmap -map-only -liberty '
    after = 'dfflibmap -liberty '
    if before in text:
        src.write_text(text.replace(before, after))
        print(f"dropped -map-only from dfflibmap in {src}")
    elif after in text:
        print("yosys plugin already maps flip-flops without -map-only")
    else:
        # Fail loudly: silently skipping means shipping an image whose area
        # numbers omit every register, which is worse than not shipping.
        print("could not find the dfflibmap invocation to patch in "
              f"{src} -- refusing to ship an image that under-reports area",
              file=sys.stderr)
        return 1

    # Invalidate any stale bytecode so the edited source is what runs.
    for pyc in src.parent.glob("__pycache__/*.pyc"):
        pyc.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
