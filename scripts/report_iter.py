#!/usr/bin/env python3
"""Full parameter dump for one iteration (or all) of a run.

    python scripts/report_iter.py final15          # every completed iteration
    python scripts/report_iter.py final15 3        # just iteration 3
    python scripts/report_iter.py final15 --runs-dir /home/chia-sparsecraft/runs

Reads THIS checkout's runs/ (constants.RUN_DIR, i.e. $SPARSECRAFT_ROOT/runs)
unless --runs-dir says otherwise. It used to hardcode V1's shared
/home/chia-sparsecraft/runs, so a V2 run was invisible to it.
"""
import argparse, json, sys, glob, os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import constants as C                                                   # noqa: E402

_ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_ap.add_argument("run", nargs="?", default="final15", help="run name under the runs dir")
_ap.add_argument("iteration", nargs="?", type=int, default=None, help="only this iteration")
_ap.add_argument("--runs-dir", default=C.RUN_DIR, help=f"default: {C.RUN_DIR}")
_a = _ap.parse_args()
run, only = _a.run, _a.iteration
base = os.path.join(_a.runs_dir, run)

def fmt(v, n=0):
    if v is None: return "-"
    if isinstance(v, float): return f"{v:,.{n}f}"
    if isinstance(v, int): return f"{v:,}"
    return str(v)

files = sorted(glob.glob(f"{base}/iter_*.json"))
if not files:
    print(f"no iterations yet in {base}"); sys.exit(0)

prev = None
for f in files:
    d = json.load(open(f))
    it = d.get("iteration")
    if only and it != only: continue
    m  = d.get("metrics", {}) or {}
    c  = m.get("counters", {}) or {}
    der= m.get("derived", {}) or {}
    e  = d.get("energy", {}) or {}
    t3 = d.get("t3_synthesis", {}) or {}
    st = d.get("state", {}) or {}
    mv = d.get("move", {}) or {}

    print("=" * 78)
    print(f"ITERATION {it}   verdict={d.get('verdict')}   wall={fmt((d.get('wall_clock_s') or 0)/60,1)} min")
    print("=" * 78)
    print("-- change ------------------------------------------------------")
    # loop.classify_move writes class / hw_fields / sw_fields; reading
    # kind / hw / sw printed "None" for every iteration.
    print(f"   move kind        {mv.get('class') if isinstance(mv,dict) else mv}")
    print(f"   hw fields        {mv.get('hw_fields') if isinstance(mv,dict) else '-'}")
    print(f"   sw fields        {mv.get('sw_fields') if isinstance(mv,dict) else '-'}")
    if isinstance(mv, dict) and mv.get('changed'):
        # move.changed is {field: [child_value, parent_value]} -- see synth_front.py
        for k, v in mv['changed'].items():
            print(f"   changed          {k}: {v[1]} -> {v[0]}")
    rp = d.get("repair") or {}
    if rp:
        print(f"   repair           first {rp.get('first_verdict')} -> {rp.get('outcome')}"
              f" after {len(rp.get('attempts') or [])} attempt(s)"
              + (f"  ({rp.get('stop_reason')})" if rp.get('stop_reason') else ""))
        for a in rp.get("attempts") or []:
            print(f"     #{a.get('attempt')} {a.get('failure_class'):<11} "
                  f"{a.get('verdict_before')} -> {a.get('verdict_after') or '?'}"
                  f"  reported={a.get('status') or '-'} conf={a.get('confidence')}"
                  f"  {a.get('wall_s', 0)/60:.1f} min")
    print(f"   rtl_digest       {d.get('rtl_digest')}")
    print(f"   netlist_digest   {d.get('netlist_digest')}  ({fmt(d.get('netlist_files'))} files)")
    print(f"   parent           {d.get('parent_hash')}   -> state {d.get('state_hash')}")
    print("-- design state ------------------------------------------------")
    for k in ("meshRows","meshColumns","dataflow","sp_capacity_kb","acc_capacity_kb",
              "sp_banks","acc_banks","dma_maxbytes","tlb_size","gate_enable",
              "zbu_enable","k_chunk","b_blocks","x_resident","dense_mode","workload"):
        if k in st: print(f"   {k:18s} {st[k]}")
    print("-- performance (MEASURED) --------------------------------------")
    print(f"   cycles           {fmt(m.get('cycles'))}")
    print(f"   off-chip bytes   {fmt((c.get('RDMA_BYTES_REC',0)+c.get('WDMA_BYTES_SENT',0)))}"
          f"   (rd {fmt(c.get('RDMA_BYTES_REC'))} / wr {fmt(c.get('WDMA_BYTES_SENT'))})")
    print(f"   equiv mismatches {fmt(d.get('equiv_mismatches'))}")
    print(f"   macs issued      {fmt(c.get('macs_issued'))}   useful {fmt(m.get('macs_useful'))}"
          f"   gated {fmt(c.get('MAC_GATED_TOTAL'))}")
    print(f"   exe active frac  {fmt(der.get('exe_active_fraction'),4)}")
    print("-- area / timing (MEASURED, yosys+NanGate45) -------------------")
    print(f"   area total       {fmt((d.get('area_um2') or 0)/1e6,4)} mm2   [{d.get('area_source')}]")
    print(f"   logic area       {fmt(d.get('t3_logic_um2'))} um2")
    print(f"   sram macro area  {fmt(d.get('t3_sram_macro_um2'))} um2")
    print(f"   cells            {fmt(t3.get('cell_count'))}   seq {fmt(t3.get('seq_cells'))}")
    print(f"   period used      {fmt(d.get('period_ns'),3)} ns")
    print("-- energy (MODELLED, t1_model) ---------------------------------")
    print(f"   energy           {fmt((d.get('energy_pj') or e.get('energy_pj') or 0)/1e6,2)} uJ"
          f"   [{d.get('energy_source','T1_MODEL')}]")
    print(f"   power            {fmt(d.get('power_w') or e.get('power_w'),4)} W")
    print(f"   perf             {fmt(e.get('perf_gops'),3)} GOPS"
          f"   perf/W {fmt(e.get('perf_per_watt_gops_w'),2)} GOPS/W")
    b = e.get("breakdown",{}) or {}
    print(f"   mac/sram/dram    {fmt(b.get('mac_pct'),0)}/{fmt(b.get('sram_pct'),0)}/{fmt(b.get('dram_pct'),0)}%"
          f"   mac_eff {fmt((e.get('mac_efficiency') or 0)*100,1)}%")
    print("-- power (OpenSTA, RECORDED not scored) ------------------------")
    print(f"   t3 power         {fmt(d.get('t3_power_w'),4)} W"
          f"   activity={t3.get('power_activity_source')}")
    if prev and m.get("cycles"):
        pc, pe_, pa = prev
        print("-- vs previous admitted ----------------------------------------")
        if pc: print(f"   cycles           {pc:,} -> {m['cycles']:,}   {pc/m['cycles']:.3f}x")
        cur_e = (d.get('energy_pj') or e.get('energy_pj') or 0)/1e6
        if pe_ and cur_e: print(f"   energy           {pe_:.2f} -> {cur_e:.2f} uJ   {pe_/cur_e:.3f}x")
        ca = (d.get('area_um2') or 0)/1e6
        if pa and ca: print(f"   area             {pa:.4f} -> {ca:.4f} mm2  {ca/pa:.4f}x")
    if d.get("verdict") == "ADMIT_FRONT":
        prev = (m.get("cycles"), (d.get('energy_pj') or e.get('energy_pj') or 0)/1e6,
                (d.get('area_um2') or 0)/1e6)
    print()
