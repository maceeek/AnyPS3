#!/usr/bin/env python3
"""Compare the last PERF SPU_STATS / SPU_DYNAMIC lines of several game runs (stderr.log of native-virtual-NAME).

usage: spufast3_stats.py RUN [RUN ...]   (RUN = native-virtual-NAME directory or NAME under local/ratchet)
Prints per lane: steps (G), native_calls, interpreter_calls, native_miss, cand_try/cand_fail/reloc_find/reloc_hit, wake latency
and per-1000-steps ratios.  Cumulative counters of the whole run (not a window)."""
import os, re, sys

RATCHET = os.environ.get("ANYPS3_RATCHET_DIR", "/home/martink/AnyPS3/local/ratchet")


def last_stats(path):
    lanes = {}
    dyn = None
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("PERF SPU_STATS"):
                kv = dict(re.findall(r"(\w+)=(\w+)", line))
                try:
                    lanes[kv["id"]] = {k: (int(v, 16) if k == "id" else int(v)) for k, v in kv.items()}
                except ValueError:
                    pass  # interleaved with another thread's stderr line
            elif line.startswith("PERF SPU_DYNAMIC"):
                dyn = dict((k, int(v)) for k, v in re.findall(r"(\w+)=(\d+)", line))
    return lanes, dyn


def main():
    for run in sys.argv[1:]:
        d = run if os.path.isdir(run) else os.path.join(RATCHET, "native-virtual-" + run)
        lanes, dyn = last_stats(os.path.join(d, "stderr.log"))
        print(f"== {run}  dyn={dyn}")
        for i, v in sorted(lanes.items()):
            st = max(v.get("steps", 1), 1)
            print(f"  lane {i:>8}: steps {st/1e9:6.2f}G native_calls {v['native_calls']:>10} interp {v['interpreter_calls']:>9} "
                  f"miss {v['native_miss']:>9} cand_try {v.get('cand_try',0):>9} cand_fail {v.get('cand_fail',0):>9} "
                  f"reloc_find {v.get('reloc_find',0):>9} reloc_hit {v.get('reloc_hit',0):>9} dma {v['dma']:>9} wrch {v['wrch']:>10} "
                  f"wake_us/n {v.get('wake_lat_us',0)}/{v.get('wake_count',0)}  steps/native_call {st/max(v['native_calls'],1):6.1f}")


main()
