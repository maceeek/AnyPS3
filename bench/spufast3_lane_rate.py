#!/usr/bin/env python3
"""Effective host cost per SPU instruction of every SPU lane in a stamped wall-sample window.
Usage: spufast3_lane_rate.py SAMPLE_DIR PROGRAM  (uses the middle 80% of every lane's records)
Prints per lane: running share, steps per second while running, ns per SPU instruction (running samples * period / step delta)."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'critpath'))
import rawsamples as rs
import sample_report as sr
d = Path(sys.argv[1]); program = Path(sys.argv[2])
maps = sr.read_maps(d / 'maps.txt'); sym = sr.Symbols(program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
threads = [rs.read(f) for f in sorted(d.glob('thread-*.samples'))]
sym.prepare({r['rip'] for _, recs in threads for r in recs})
for h, recs in threads:
    if h['role'] != 'spu' or len(recs) < 100: continue
    n = len(recs); recs = recs[int(n * .1):int(n * .9)]
    period = h['period_ns']
    run = sum(1 for r in recs if not WAIT.search(sym.cache.get(r['rip'], '?')))
    t = (recs[-1]['stamp'] - recs[0]['stamp']) / 1e9
    steps = recs[-1]['guest_steps'] - recs[0]['guest_steps']
    print(f'lane {recs[0]["guest_tid"]:>8x}: running {100*run/len(recs):5.1f}%  steps {steps/1e6:8.1f} M in {t:6.1f} s -> {steps/t/1e6:6.2f} M/s wall;  '
          f'{run*period/1e9/max(steps,1)*1e9:6.2f} ns per instruction while running')
