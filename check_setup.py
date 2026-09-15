#!/usr/bin/env python3
"""Ground truth for what is installed, and what is not.

Every claim about this environment in a slide, a README or a paper should come
from here rather than from memory. It probes the host, then probes INSIDE each
container image, and prints one row per capability with the evidence that
decided it.

    python check_setup.py              # table
    python check_setup.py --json       # machine-readable, for the deck
    python check_setup.py --quick      # host + image presence only, no container starts

Exit status is 0 when every capability the loop actually requires is present,
1 otherwise -- so it doubles as a pre-flight check before `chia up`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(ROOT)

# Container engine. This host has podman and a ~/bin/docker shim pointing at it
# (CHIA's YAML parser hardcodes engine="docker"), so probe through podman
# directly and let the shim be the cluster's problem.
ENGINE = shutil.which("podman") or shutil.which("docker") or "podman"
# podman's default TMPDIR is on the 70 GB root volume; a 31 GB image overruns it.
ENV = {**os.environ, "TMPDIR": os.environ.get(
    "TMPDIR", os.path.join(os.path.expanduser("~"), "podman-tmp"))}

SYNTH_IMAGE = os.environ.get("SPARSECRAFT_SYNTH_IMAGE",
                             "localhost/sparsecraft-synth:latest")

# What each image is asked to do in cluster.yaml, and the binaries that make
# that true. A missing binary here is a missing TIER, not a cosmetic gap.
IMAGE_PROBES: dict[str, dict] = {
    "ghcr.io/ucb-bar/chia-chisel-build:latest": {
        "role": "N30/N31 elaborate + N32 kernel build  (resource: chipyard)",
        "bins": ["sbt", "firtool", "verilator", "riscv64-unknown-elf-gcc"],
        "paths": ["/home/ray/chipyard/env.sh",
                  "/home/ray/chipyard/generators/gemmini"],
        "required": True,
    },
    "ghcr.io/ucb-bar/chia-verilator-run:latest": {
        "role": "N50 T2a simulate  (resource: verilator_run)",
        # Deliberately NOT verilator. This image runs a simulator that was
        # already compiled in the build image; what it must provide is the
        # shared libraries that binary was linked against. libriscv.so IS the
        # golden model, which is why the two images have to ship byte-identical
        # copies and why checking for a `verilator` binary here would be
        # checking the wrong thing.
        "bins": [],
        "paths": ["/usr/local/lib/libriscv.so", "/usr/local/lib/libdramsim.so"],
        "required": True,
    },
    "ghcr.io/ucb-bar/chia-riscv-cross:latest": {
        "role": "standalone cross-compile  (resource: riscv_build)",
        "bins": ["riscv64-unknown-elf-gcc"],
        "paths": [],
        "required": False,
    },
    "ghcr.io/ucb-bar/chia-opencode:latest": {
        "role": "N10 agentic proposer  (resource: llm)",
        "bins": ["opencode"],
        "paths": [],
        "required": False,
    },
    SYNTH_IMAGE: {
        "role": "N52 T3 synthesis  (resource: hammer)",
        "bins": ["hammer-vlsi", "yosys", "openroad"],
        "paths": ["/home/ray/pdk/nangate45/lib/NangateOpenCellLibrary_typical.lib",
                  "/home/ray/pdk/nangate45/lef/NangateOpenCellLibrary.tech.lef"],
        "required": False,
        "build": "docker/build.sh",
    },
}

OK, MISS, WARN = "OK", "MISSING", "PARTIAL"


def sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=ENV)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------
def probe_host() -> dict:
    out: dict = {}

    mem_kb = 0
    try:
        with open("/proc/meminfo") as f:
            m = re.search(r"MemTotal:\s+(\d+)", f.read())
            mem_kb = int(m.group(1)) if m else 0
    except OSError:
        pass
    out["cores"] = os.cpu_count() or 0
    out["ram_gb"] = round(mem_kb / 1024 / 1024, 1)
    # The single most load-bearing number in this whole setup: a Chisel
    # elaboration peaks at 8-12 GB, so this is what caps concurrency, not cores.
    out["max_concurrent_elaborations"] = max(1, int(out["ram_gb"] // 12))

    try:
        st = os.statvfs(PROJECT)
        out["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
    except OSError:
        out["disk_free_gb"] = None

    rc, ver = sh([ENGINE, "--version"])
    out["container_engine"] = ENGINE
    out["container_engine_version"] = ver.strip().splitlines()[0] if rc == 0 else None
    shim = os.path.expanduser("~/bin/docker")
    out["docker_shim"] = shim if os.path.exists(shim) else None

    py = sys.executable
    out["python"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    out["python_path"] = py
    out["conda_env"] = os.environ.get("CONDA_DEFAULT_ENV")
    for mod in ("ray", "chia"):
        rc, o = sh([py, "-c",
                    f"import {mod},sys;print(getattr({mod},'__version__','(no __version__)'))"])
        out[f"{mod}_version"] = o.strip().splitlines()[-1] if rc == 0 else None

    # Vertex ADC: the one credential the agentic tier needs and the one thing
    # cluster.yaml still has commented out.
    adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    out["gcloud_adc"] = adc if os.path.exists(adc) else None
    out["google_cloud_project"] = os.environ.get("GOOGLE_CLOUD_PROJECT")
    return out


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def local_images() -> dict[str, str]:
    rc, out = sh([ENGINE, "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"])
    if rc != 0:
        return {}
    found = {}
    for line in out.splitlines():
        if "\t" in line:
            name, size = line.split("\t", 1)
            found[name.strip()] = size.strip()
    return found


_PROBE = r'''
source /home/ray/chipyard/env.sh 2>/dev/null
EDA="${SPARSECRAFT_EDA_PREFIX:-/home/ray/eda}"
export PATH="$PATH:$EDA/yosys/bin:$EDA/openroad/bin:$EDA/klayout/bin"
for b in %s; do
  p="$(command -v "$b" 2>/dev/null)"
  echo "BIN|$b|${p:-}"
done
for f in %s; do
  if [ -e "$f" ]; then echo "PATH|$f|yes"; else echo "PATH|$f|no"; fi
done
python -c "import importlib.util as u; print('PY|hammer|'+('yes' if u.find_spec('hammer') else 'no'))" 2>/dev/null \
  || echo "PY|hammer|no"
'''


def probe_image(image: str, spec: dict, present: bool) -> dict:
    res = {"image": image, "role": spec["role"], "required": spec["required"],
           "present": present, "bins": {}, "paths": {}, "hammer_module": None}
    if not present:
        res["status"] = MISS
        res["reason"] = "image not pulled/built on this host"
        if spec.get("build"):
            res["fix"] = f"build it: {spec['build']}"
        return res

    # Placeholders so the shell `for` loops are always well formed; both are
    # filtered out of the results below rather than reported as capabilities.
    script = _PROBE % (" ".join(spec["bins"]) or "__none__",
                       " ".join(spec["paths"]) or "/__none__")
    rc, out = sh([ENGINE, "run", "--rm", "--entrypoint", "", image,
                  "bash", "-c", script], timeout=300)
    if rc != 0 and not out.strip():
        res["status"] = WARN
        res["reason"] = f"probe container failed rc={rc}"
        return res

    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        kind, key, val = parts
        if kind == "BIN":
            if key == "__none__":
                continue
            res["bins"][key] = val or None
        elif kind == "PATH" and key != "/__none__":
            res["paths"][key] = (val == "yes")
        elif kind == "PY":
            res["hammer_module"] = (val == "yes")

    missing_bins = [b for b, p in res["bins"].items() if not p]
    missing_paths = [p for p, ok in res["paths"].items() if not ok]
    if missing_bins or missing_paths:
        res["status"] = WARN
        res["reason"] = "missing: " + ", ".join(missing_bins + missing_paths)
    else:
        res["status"] = OK
    return res


# ---------------------------------------------------------------------------
# Loop wiring: does cluster.yaml advertise a worker for every resource the
# code requests? A resource nobody advertises does not error -- the task just
# never schedules, which is the worst failure mode there is.
# ---------------------------------------------------------------------------
def probe_wiring() -> dict:
    cluster = os.path.join(ROOT, "cluster.yaml")
    advertised: set[str] = set()
    images_used: dict[str, str] = {}
    if os.path.exists(cluster):
        text = open(cluster).read()
        for m in re.finditer(r'resources:\s*\{([^}]*)\}', text):
            for km in re.finditer(r'"([^"]+)"\s*:', m.group(1)):
                advertised.add(km.group(1))
        for m in re.finditer(r'image:\s*"([^"]+)"', text):
            images_used[m.group(1)] = m.group(1)

    requested: dict[str, list[str]] = {}
    for fname in ("nodes.py", "synth_node.py", "diff_nodes.py", "agent.py"):
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            continue
        src = open(path).read()
        for m in re.finditer(r'resources=\{([^}]*)\}', src):
            body = m.group(1)
            for km in re.finditer(r'(?:"([A-Za-z_][\w]*)"|R_([A-Z_]+))\s*:', body):
                name = km.group(1)
                if name is None:
                    # R_CHIPYARD -> "chipyard": resolve through constants.
                    const = "R_" + km.group(2)
                    cm = re.search(rf'^{const}\s*=\s*"([^"]+)"',
                                   open(os.path.join(ROOT, "constants.py")).read(), re.M)
                    name = cm.group(1) if cm else const
                requested.setdefault(name, []).append(fname)

    return {
        "advertised": sorted(advertised),
        "requested": {k: sorted(set(v)) for k, v in sorted(requested.items())},
        "unadvertised": sorted(set(requested) - advertised),
        "cluster_images": sorted(images_used),
    }


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="skip container probes (fast, but only checks presence)")
    args = ap.parse_args()

    report = {"host": probe_host(), "wiring": probe_wiring(), "images": []}
    have = local_images()
    for image, spec in IMAGE_PROBES.items():
        present = image in have
        if args.quick:
            report["images"].append(
                {"image": image, "role": spec["role"], "present": present,
                 "required": spec["required"],
                 "status": OK if present else MISS,
                 "size": have.get(image)})
        else:
            r = probe_image(image, spec, present)
            r["size"] = have.get(image)
            report["images"].append(r)

    required_bad = [r for r in report["images"]
                    if r["required"] and r["status"] != OK]
    report["ready_for_t2"] = not required_bad
    synth = next((r for r in report["images"] if r["image"] == SYNTH_IMAGE), None)
    report["ready_for_t3"] = bool(synth and synth["status"] == OK)
    report["ready_for_llm"] = bool(report["host"]["gcloud_adc"])

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ready_for_t2"] else 1

    h = report["host"]
    print("=" * 78)
    print("SparseCraft setup report")
    print("=" * 78)
    print(f"  host           {h['cores']} cores / {h['ram_gb']} GB RAM  "
          f"-> at most {h['max_concurrent_elaborations']} concurrent elaboration(s)")
    print(f"  disk free      {h['disk_free_gb']} GB")
    print(f"  engine         {h['container_engine']}  "
          f"({h['container_engine_version']})")
    print(f"  docker shim    {h['docker_shim'] or 'ABSENT -- chia up will fail'}")
    print(f"  python         {h['python']}  env={h['conda_env']}")
    print(f"  ray / chia     {h['ray_version']} / {h['chia_version']}")
    print(f"  vertex ADC     {h['gcloud_adc'] or 'NOT PRESENT (agentic tier disabled)'}")
    print()
    print(f"{'status':<9} {'tier / resource':<52} image")
    print("-" * 78)
    for r in report["images"]:
        print(f"{r['status']:<9} {r['role']:<52} {r['image'].split('/')[-1]}")
        if r.get("reason"):
            print(f"{'':<9}   {r['reason']}")
        if r.get("fix"):
            print(f"{'':<9}   fix: {r['fix']}")
        for b, p in (r.get("bins") or {}).items():
            print(f"{'':<9}   {'+' if p else '-'} {b:<28} {p or '(absent)'}")
        for pth, ok in (r.get("paths") or {}).items():
            print(f"{'':<9}   {'+' if ok else '-'} {os.path.basename(pth)}")
    print()
    w = report["wiring"]
    print("resources requested by the code vs advertised by cluster.yaml:")
    for name, files in w["requested"].items():
        mark = "+" if name in w["advertised"] else "-"
        print(f"  {mark} {name:<18} requested by {', '.join(files)}")
    if w["unadvertised"]:
        print(f"  !! NO WORKER ADVERTISES: {w['unadvertised']} -- "
              f"those tasks will hang in PENDING, not error")
    print()
    print(f"  T2 (simulate)  : {'READY' if report['ready_for_t2'] else 'BLOCKED'}")
    print(f"  T3 (synthesis) : {'READY' if report['ready_for_t3'] else 'BLOCKED'}")
    print(f"  agentic (LLM)  : {'READY' if report['ready_for_llm'] else 'BLOCKED - no ADC'}")
    return 0 if report["ready_for_t2"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
