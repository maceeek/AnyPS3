#!/usr/bin/env python3
"""Per SPU thread LS-PC histogram from a PROF1 sample directory (window by the SPU lane's own step counter optional).

Usage: spu_profile.py SAMPLE_DIR [--bucket 256] [--top 12] [--lo STEPS --hi STEPS]  (steps = that SPU thread's counter)
"""
import argparse, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'local/ratchet/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_report as sr
ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('--bucket', type=int, default=256); ap.add_argument('--top', type=int, default=12)
ap.add_argument('--tail', type=float, default=0.5, help='use only the last fraction of each file (steady state)')
a = ap.parse_args()
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != 'spu': continue
    recs = d['records']; recs = recs[int(len(recs) * (1 - a.tail)):]
    if not recs: continue
    tids = collections.Counter(r['guest_tid'] for r in recs)
    pcs = collections.Counter(r['pc'] // a.bucket * a.bucket for r in recs if r['flags'] & 1)
    steps0, steps1 = recs[0]['guest_steps'], recs[-1]['guest_steps']
    print(f'== {f.name} spu tids {dict(tids)} samples {len(recs)} cpu {len(recs)*d["period_ns"]/1e6:.0f} ms steps {steps0}..{steps1} ({steps1-steps0})')
    for b, n in pcs.most_common(a.top):
        print(f'   {100*n/len(recs):6.2f}%  LS {b:#07x}')
