#!/usr/bin/env python3
"""Episodes in which the main thread waits for SPU work (engine-counter / job-flag wait) while NO game SPU lane (not 0x100) is running.
Prints the length histogram (500 us bins) and the total, i.e. the part of the SPU wait that is pure scheduling/wake latency.
Usage: spufast2_idle_gaps.py SAMPLE_DIR PROGRAM STEPS_LO STEPS_HI [FRAME_MS]"""
import collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'critpath'))
import rawsamples as rs
import sample_report as sr
d = Path(sys.argv[1]); program = Path(sys.argv[2]); lo, hi = int(sys.argv[3]), int(sys.argv[4]); frame_ms = float(sys.argv[5]) if len(sys.argv) > 5 else 32.4
maps = sr.read_maps(d / 'maps.txt'); sym = sr.Symbols(program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
threads = [rs.read(f) for f in sorted(d.glob('thread-*.samples'))]
sym.prepare({r['rip'] for _, recs in threads for r in recs})
ppu = next(t for t in threads if t[0]['role'] == 'ppu'); period = ppu[0]['period_ns']
prec = [r for r in ppu[1] if r['guest_tid'] == 1 and lo <= r['guest_steps'] <= hi]
ta, tb = prec[0]['stamp'], prec[-1]['stamp']; nb = (tb - ta) // period + 1
run = [0] * nb
for h, recs in threads:
    if h['role'] != 'spu' or recs[0]['guest_tid'] == 0x100: continue
    for r in recs:
        if ta <= r['stamp'] <= tb and not WAIT.search(sym.cache.get(r['rip'], '?')): run[(r['stamp'] - ta) // period] += 1
wait = [False] * nb
for r in prec:
    pc = r['pc']
    if 0x696c10 <= pc < 0x696e20 or 0x666040 <= pc < 0x666100 or 0x5564c0 <= pc < 0x556600: wait[(r['stamp'] - ta) // period] = True
episodes = []; cur = 0
for b in range(nb):
    if wait[b] and run[b] == 0: cur += 1
    else:
        if cur: episodes.append(cur)
        cur = 0
frames = (tb - ta) / 1e6 / frame_ms
tot = sum(episodes)
print(f'idle-lane wait bins: {tot} = {tot * period / 1e6 / frames:.2f} ms/frame in {len(episodes)} episodes ({len(episodes) / frames:.1f} per frame)')
h = collections.Counter(min(e, 20) for e in episodes)
print('episode length (x0.5 ms): count, share of idle time: ' + '; '.join(f'{k}{"+" if k == 20 else ""}:{h[k]}/{100 * k * h[k] / tot:.0f}%' for k in sorted(h)))
