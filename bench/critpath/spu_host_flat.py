#!/usr/bin/env python3
"""Host-function flat profile of one SPU lane (CPU-time or wall samples; wall: parked samples excluded).
Usage: spu_host_flat.py SAMPLE_DIR PROGRAM SPU_ID_HEX [--tail 1.0] [--top 30]"""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'local/ratchet/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_report as sr
ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path); ap.add_argument('spu', type=lambda x: int(x, 16))
ap.add_argument('--tail', type=float, default=1.0); ap.add_argument('--top', type=int, default=30)
a = ap.parse_args()
maps = sr.read_maps(a.dir / 'maps.txt'); sym = sr.Symbols(a.program, maps)
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != 'spu' or not d['records'] or d['records'][0]['guest_tid'] != a.spu: continue
    recs = d['records']; recs = recs[int(len(recs) * (1 - a.tail)):]
    sym.prepare({r['addresses'][0] for r in recs})
    c = collections.Counter(sym.cache.get(r['addresses'][0], '?') for r in recs)
    wait = sum(v for k, v in c.items() if re.search(r'sem_trywait|futex|pthread_cond|nanosleep', k))
    run = len(recs) - wait
    print(f'{f.name}: {len(recs)} samples, running {run}')
    for k, v in c.most_common(a.top):
        print(f'  {100*v/max(run,1):6.2f}% of running  {k[:150]}')
