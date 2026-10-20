#!/usr/bin/env python3
"""Per SPU lane wall-clock state (running vs parked/waiting) and LS-PC histogram of the running samples.
Usage: spu_wall.py SAMPLE_DIR PROGRAM [--tail 0.3] [--top 8] [--bucket 256]   (needs ANYPS3_SAMPLE_PROFILE_WALL=...spu)"""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'local/ratchet/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_report as sr
ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path)
ap.add_argument('--tail', type=float, default=0.3); ap.add_argument('--top', type=int, default=8); ap.add_argument('--bucket', type=int, default=256)
a = ap.parse_args()
maps = sr.read_maps(a.dir / 'maps.txt'); sym = sr.Symbols(a.program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
files = []
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != 'spu': continue
    recs = d['records']; recs = recs[int(len(recs) * (1 - a.tail)):]
    files.append((f.name, d['period_ns'], recs))
rips = {r['addresses'][0] for _, _, recs in files for r in recs}
sym.prepare(rips)
for name, period, recs in files:
    n = len(recs)
    states = collections.Counter(); pcs = collections.Counter(); waits = collections.Counter()
    for r in recs:
        nm = sym.cache.get(r['addresses'][0], '?')
        if WAIT.search(nm): states['parked/wait'] += 1; waits[nm] += 1
        else: states['running'] += 1; pcs[r['pc'] // a.bucket * a.bucket] += 1
    tid = recs[0]['guest_tid']
    print(f'== {name} spu id {tid:#x}: {n} samples ({n*period/1e9:.1f} s)  running {100*states["running"]/n:.1f}%  parked/wait {100*states["parked/wait"]/n:.1f}%  {waits.most_common(2)}')
    print('   running LS buckets:', ', '.join(f'{b:#x}:{100*v/n:.1f}%' for b, v in pcs.most_common(a.top)))
