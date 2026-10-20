#!/usr/bin/env python3
"""Cross-tab of host symbol x LS-PC bucket for one SPU lane (wall or CPU samples).
Usage: spufast2_lane_xtab.py SAMPLE_DIR PROGRAM SPU_ID_HEX [--tail 0.5] [--bucket 64] [--top 40]"""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local/ratchet/tools'))
import sample_report as sr
ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path); ap.add_argument('spu', type=lambda x: int(x, 16))
ap.add_argument('--tail', type=float, default=0.5); ap.add_argument('--bucket', type=int, default=64); ap.add_argument('--top', type=int, default=40)
a = ap.parse_args()
maps = sr.read_maps(a.dir / 'maps.txt'); sym = sr.Symbols(a.program, maps)
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != 'spu' or not d['records'] or d['records'][0]['guest_tid'] != a.spu: continue
    recs = d['records']; recs = recs[int(len(recs) * (1 - a.tail)):]
    sym.prepare({r['addresses'][0] for r in recs})
    tot = len(recs)
    c = collections.Counter()
    h = collections.Counter(); l = collections.Counter()
    for r in recs:
        s = sym.cache.get(r['addresses'][0], '?')[:60]; b = r['pc'] // a.bucket * a.bucket
        c[(s, b)] += 1; h[s] += 1; l[b] += 1
    print(f'{f.name}: {tot} samples')
    print('-- host symbols'); [print(f'  {100*v/tot:6.2f}% {k}') for k, v in h.most_common(a.top)]
    print('-- LS buckets'); [print(f'  {100*v/tot:6.2f}% {k:#07x}') for k, v in l.most_common(a.top)]
    print('-- (host, LS)'); [print(f'  {100*v/tot:6.2f}% {k[0]:60s} {k[1]:#07x}') for k, v in c.most_common(a.top)]
