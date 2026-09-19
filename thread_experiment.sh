#!/usr/bin/env bash
# VERILATOR_THREADS 16 -> 32 A/B.
#
# simulate is 99.7% of an iteration (profiled: 1087.6 s of 1090 s on jag512),
# so per-sim Verilator threading is the only knob that touches the real cost.
# Threads are a BUILD-time flag inside the elaboration cache tag, so this
# forces a full rebuild -- which is exactly why it is worth measuring once
# rather than assuming.
#
# Baseline to beat: 1087.6 s at 16 threads, jag512, identical design state.
set -u
source /home/rajatabha/miniforge3/etc/profile.d/conda.sh && conda activate chia_env
export PATH=$HOME/bin:$PATH TMPDIR=/home/chia-sparsecraft/podman-tmp
export SPARSECRAFT_ROOT=/home/chia-sparsecraft
export RAY_ADDRESS=$(hostname -I | awk '{print $1}'):6379
export SPARSECRAFT_MAKE_JOBS=24
export SPARSECRAFT_VERILATOR_THREADS=32

cd /home/chia-sparsecraft/sparsecraft
echo "### 32-thread build + simulate, jag512, no synthesis (isolates simulate)"
/usr/bin/time -f "WALL %e s" timeout 5400 chia job submit --working-dir . -- \
  python loop.py --iters 1 --skip-llm --workload jag512 --no-synth \
  --sim-timeout 5400 --run-name threads32 2>&1 \
  | grep -E "N50 |N41 |N53 |N60 |WALL|Traceback|FAILED"

echo
echo "### simulate exec_time from the profile (the number that matters)"
python3 - <<'PY'
import json, glob, os
p = sorted(glob.glob("/home/chia-sparsecraft/runs/threads32/profile/*.log"))
if not p:
    print("  no profile log"); raise SystemExit
for line in open(p[0]):
    try: r = json.loads(line)
    except Exception: continue
    if r.get("type") == "complete" and r.get("func") == "simulate":
        t = r["exec_time_s"]
        print(f"  simulate @32 threads = {t:,.1f} s")
        print(f"  baseline  @16 threads = 1,087.6 s")
        print(f"  speedup = {1087.6/t:.2f}x" if t else "")
PY
