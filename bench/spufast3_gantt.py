#!/usr/bin/env python3
"""Text Gantt chart of stamped wall samples (ANYPS3_SAMPLE_PROFILE_WALL=ppu,spu, build >= 522): one row per sampled thread, one
character per sample period, for a few spans of the measured window.

Usage: spufast3_gantt.py SAMPLE_DIR PROGRAM STEPS_LO STEPS_HI [--spans 3] [--span-ms 50] [--at 0.2,0.5,0.8]
 PPU guest thread 1 (main): p pacing wait, E/e engine-counter wait running/sleeping, J/j job-flag wait running/sleeping,
                            W guest work, . sleeping elsewhere
 other PPU threads:         W running, . sleeping
 SPU lanes (guest tid in hex): k running in SPURS kernel (LS < 0x1000), # running elsewhere, . parked/sleeping
"""
import argparse, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'critpath'))
import rawsamples as rs
import sample_report as sr

ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path); ap.add_argument('lo', type=int); ap.add_argument('hi', type=int)
ap.add_argument('--spans', type=int, default=3); ap.add_argument('--span-ms', type=float, default=50.0)
ap.add_argument('--at', default='0.2,0.5,0.8', help='relative positions of the spans in the window')
ap.add_argument('--guest-tid', type=int, default=1)
a = ap.parse_args()
maps = sr.read_maps(a.dir / 'maps.txt'); sym = sr.Symbols(a.program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
threads = [rs.read(f) for f in sorted(a.dir.glob('thread-*.samples'))]
sym.prepare({r['rip'] for _, recs in threads for r in recs})
def running(r): return not WAIT.search(sym.cache.get(r['rip'], '?'))
ppu = [(h, recs) for h, recs in threads if h['role'] == 'ppu']
period = ppu[0][0]['period_ns']
main = next(recs for h, recs in ppu)
prec = [r for r in main if r['guest_tid'] == a.guest_tid and a.lo <= r['guest_steps'] <= a.hi]
ta, tb = prec[0]['stamp'], prec[-1]['stamp']

def main_state(r):
    pc, run = r['pc'], running(r)
    if 0x5c5200 <= pc < 0x5c5800: return 'p'
    if 0x696c10 <= pc < 0x696e20 or 0x666040 <= pc < 0x666100: return 'E' if run else 'e'
    if 0x5564c0 <= pc < 0x556600: return 'J' if run else 'j'
    return 'W' if run else '.'

rows = []
for h, recs in threads:
    if h['role'] == 'ppu':
        by = {}
        for r in recs:
            by.setdefault(r['guest_tid'], []).append(r)
        for tid, rr in sorted(by.items()):
            rr = [r for r in rr if ta <= r['stamp'] <= tb]
            if not rr: continue
            rows.append((f'ppu{tid}', rr, (lambda r: main_state(r)) if tid == a.guest_tid else (lambda r: 'W' if running(r) else '.')))
    elif h['role'] == 'spu' and recs:
        rr = [r for r in recs if ta <= r['stamp'] <= tb]
        rows.append((f'spu{recs[0]["guest_tid"]:x}', rr, lambda r: ('k' if r['pc'] < 0x1000 else '#') if running(r) else '.'))
n = int(a.span_ms * 1e6 / period)
print(f'window {(tb-ta)/1e9:.1f} s, period {period/1e3:.0f} us, span {a.span_ms} ms = {n} columns')
for frac in [float(x) for x in a.at.split(',')][:a.spans]:
    t0 = ta + int((tb - ta) * frac)
    # align the span start to the end of a pacing wait of the main thread
    m = [r for r in prec if r['stamp'] >= t0]
    for i in range(1, len(m)):
        if main_state(m[i - 1]) == 'p' and main_state(m[i]) != 'p': t0 = m[i]['stamp']; break
    print(f'--- span at {(t0-ta)/1e9:.2f} s')
    for name, rr, f in rows:
        cols = ['?'] * n
        for r in rr:
            c = (r['stamp'] - t0) // period
            if 0 <= c < n: cols[c] = f(r)
        print(f'{name:>10s} ' + ''.join(cols))
