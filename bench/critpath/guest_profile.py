#!/usr/bin/env python3
"""Window a PROF1 sample directory by guest steps and summarise the PPU thread by guest PC.

Usage: guest_profile.py SAMPLE_DIR STEPS_LO STEPS_HI [--role ppu] [--bucket 64] [--top 40]
The guest PC/step values are the last published asynchronous context (see sample_report.py notes).
"""
import argparse, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'local/ratchet/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_report as sr

ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('lo', type=int); ap.add_argument('hi', type=int)
ap.add_argument('--role', default='ppu'); ap.add_argument('--bucket', type=int, default=64)
ap.add_argument('--top', type=int, default=40)
a = ap.parse_args()
tot = 0; per_pc = collections.Counter(); per_bucket = collections.Counter(); per_tid = collections.Counter()
nflag = 0; period = 0
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != a.role: continue
    period = d['period_ns']
    for r in d['records']:
        if not (a.lo <= r['guest_steps'] <= a.hi): continue
        tot += 1
        per_tid[r['guest_tid']] += 1
        if r['flags'] & 1:
            nflag += 1
            per_pc[r['pc']] += 1
            per_bucket[r['pc'] // a.bucket * a.bucket] += 1
print(f'samples in window: {tot} (guest pc valid {nflag}), period {period} ns, cpu {tot*period/1e6:.1f} ms')
print('by guest thread:', dict(per_tid.most_common(8)))
print(f'-- by guest PC bucket {a.bucket}')
for b, n in per_bucket.most_common(a.top):
    print(f'{100*n/max(tot,1):6.2f}% {n:8d}  {b:#010x}')
