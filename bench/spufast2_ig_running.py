#!/usr/bin/env python3
"""Running-only profile of SPU lanes: host symbol shares and LS-PC buckets of non-parked samples.
Usage: spufast2_ig_running.py SAMPLE_DIR PROGRAM SPU_ID_HEX[,..] [--tail 0.5] [--bucket 256] [--top 25]"""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local/ratchet/tools'))
import sample_report as sr
ap = argparse.ArgumentParser(); ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path); ap.add_argument('lanes')
ap.add_argument('--tail', type=float, default=0.5); ap.add_argument('--bucket', type=int, default=256); ap.add_argument('--top', type=int, default=25)
a = ap.parse_args()
want = {int(x, 16) for x in a.lanes.split(',')}
maps = sr.read_maps(a.dir / 'maps.txt'); sym = sr.Symbols(a.program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
host = collections.Counter(); ls = collections.Counter(); fn = collections.Counter(); tot = run = 0
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != 'spu' or not d['records'] or d['records'][0]['guest_tid'] not in want: continue
    recs = d['records']; recs = recs[int(len(recs) * (1 - a.tail)):]
    sym.prepare({r['addresses'][0] for r in recs}); tot += len(recs)
    for r in recs:
        s = sym.cache.get(r['addresses'][0], '?')
        if WAIT.search(s): continue
        run += 1; name = re.sub(r'\(.*', '', s)[:50]; host[name] += 1; ls[r['pc'] // a.bucket * a.bucket] += 1
print(f'{tot} samples, running {run} ({100*run/tot:.1f}%)')
print('-- host (share of running)'); [print(f'  {100*v/run:6.2f}% {k}') for k, v in host.most_common(a.top)]
gen = sum(v for k, v in host.items() if re.search(r'spu_fn_\d+', k)); print(f'  generated spu_fn_*: {100*gen/run:.1f}%')
print('-- LS buckets (share of running)'); [print(f'  {100*v/run:6.2f}% {k:#07x}') for k, v in ls.most_common(a.top)]
