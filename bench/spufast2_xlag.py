#!/usr/bin/env python3
"""Unstamped wall-sample timeline: align the per-thread tick axes of a wall-mode sample directory by cross-correlation and
report, per main-thread wait state, which SPU lanes were running. (Build 520b-style samples without CLOCK_MONOTONIC stamps:
time index = sequence + cumulative overruns, thread start offsets estimated by correlation of the 30 Hz structure.)
Usage: spufast2_xlag.py SAMPLE_DIR PROGRAM [--seconds 100] [--maxlag 2000]"""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local/ratchet/tools'))
import sample_report as sr
ap = argparse.ArgumentParser(); ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path)
ap.add_argument('--seconds', type=float, default=100); ap.add_argument('--maxlag', type=int, default=2000)
a = ap.parse_args()
maps = sr.read_maps(a.dir / 'maps.txt'); sym = sr.Symbols(a.program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
th = {}
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f); recs = d['records']
    if not recs: continue
    idx, c = [], 0
    for r in recs:
        c += r['overrun']; idx.append(r['sequence'] + c)
    th[f.name] = (d['role'], recs, idx)
period = 0.0005
def tail_mask(idx, end, n): return idx > end - n
ppu = next(v for v in th.values() if v[0] == 'ppu')
nbins = int(a.seconds / period)
end_all = {k: v[2][-1] for k, v in th.items()}
for k, (role, recs, idx) in th.items():
    sym.prepare({r['addresses'][0] for r in recs[-nbins - 5000:]}) if role == 'spu' else None
def signal(k, pred):
    role, recs, idx = th[k]; end = end_all[k]; s = 0
    for r, i in zip(reversed(recs), reversed(idx)):
        j = int(i - (end - nbins))
        if j < 0: break
        if pred(r): s |= 1 << j
    return s
def ppu_state(r):
    if r['guest_tid'] != 1: return False
    pc = r['pc']
    return 0x696c10 <= pc < 0x696e20 or 0x666040 <= pc < 0x666100 or 0x5564c0 <= pc < 0x556600
kp = next(k for k, v in th.items() if v[0] == 'ppu')
P = signal(kp, ppu_state)
lanes = {}
for k, (role, recs, idx) in th.items():
    if role != 'spu': continue
    lanes[hex(recs[0]['guest_tid'])] = (k, signal(k, lambda r: not WAIT.search(sym.cache.get(r['addresses'][0], '?'))))
def corr(x, y, maxlag):
    """binary correlation sum over lags: count of bins where x(t) and y(t+lag) are both 1"""
    out = {}
    mask = (1 << nbins) - 1
    for l in range(-maxlag, maxlag + 1):
        out[l] = (x & ((y << l) & mask if l >= 0 else y >> -l)).bit_count()
    return out
print('PPU wait-state share %.1f%% of bins' % (100 * P.bit_count() / nbins))
res = {}
for name, (k, L) in lanes.items():
    c = corr(P, L, a.maxlag); best = max(c, key=c.get)
    base = P.bit_count() * L.bit_count() / nbins
    print(f'lane {name}: lane running {100*L.bit_count()/nbins:.1f}%  best lag {best} bins ({best*period*1e3:.1f} ms) joint {c[best]} vs independent {base:.0f}')
    res[name] = (best, c)
import pickle; pickle.dump({n: v[0] for n, v in res.items()}, open(a.dir / 'xlag.pkl', 'wb'))

# ---- attribution of main-thread wait bins with the estimated lags ----
ig = [n for n in lanes if n != '0x100']
def shifted(L, lag):  # bin t of the PPU axis corresponds to bin t+lag on the lane axis
    return (L >> lag) if lag >= 0 else (L << -lag) & ((1 << nbins) - 1)
S = {n: shifted(lanes[n][1], res[n][0]) for n in ig}
kp_role, kp_recs, kp_idx = th[kp]
end = end_all[kp]
kinds = collections.defaultdict(lambda: [0, collections.Counter()])
for r, i in zip(kp_recs, kp_idx):
    j = int(i - (end - nbins))
    if j < 0 or r['guest_tid'] != 1: continue
    pc = r['pc']
    if 0x696c10 <= pc < 0x696e20 or 0x666040 <= pc < 0x666100: k = 'engine-counter wait'
    elif 0x5564c0 <= pc < 0x556600: k = 'job-flag wait'
    else: continue
    run = [n for n in ig if (S[n] >> j) & 1]
    kinds[k][0] += 1; kinds[k][1][len(run)] += 1
    for n in run: kinds[k][1]['lane ' + n] += 1
for k, (n, c) in kinds.items():
    print(f'{k}: {n} bins ({n*period*1e3/ (a.seconds/ (0.0334)) :.2f} ms per 33.4 ms frame)  # ig lanes running: ' + ' '.join(f'{m}:{100*c[m]/n:.0f}%' for m in range(6)))
    print('   per lane running during the wait: ' + ' '.join(f'{x[5:]}:{100*c[x]/n:.0f}%' for x in sorted(c, key=str) if str(x).startswith('lane')))
