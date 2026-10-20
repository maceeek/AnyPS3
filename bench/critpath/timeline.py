#!/usr/bin/env python3
"""Cross-thread wall timeline of a wall-mode sample directory: for each state of the main (PPU) thread, how many SPU lanes ran at
the same time. Needs build 522+ samples (ANYPS3_SAMPLE_PROFILE_WALL=ppu,spu ANYPS3_SAMPLE_PROFILE_GUEST_STACK=1).

Usage: timeline.py SAMPLE_DIR PROGRAM STEPS_LO STEPS_HI [GUEST_TID=1]   (steps = PPU guest steps bounding the window, from the PERF interval lines)
Time axis: the CLOCK_MONOTONIC stamp stored in every record (rawsamples.py). A bin is one sample period.
"""
import collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rawsamples as rs
import sample_report as sr

d = Path(sys.argv[1]); program = Path(sys.argv[2]); lo, hi = int(sys.argv[3]), int(sys.argv[4])
GUEST_TID = int(sys.argv[5]) if len(sys.argv) > 5 else 1
maps = sr.read_maps(d / 'maps.txt'); sym = sr.Symbols(program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
threads = [rs.read(f) for f in sorted(d.glob('thread-*.samples'))]
if any(not recs or not recs[0]['stamp'] for h, recs in threads if h['role'] in ('ppu', 'spu')):
    sys.exit('records carry no wall-clock stamp: sample with ANYPS3_SAMPLE_PROFILE_WALL=ppu,spu (build 522+)')
sym.prepare({r['rip'] for _, recs in threads for r in recs})
def running(r): return not WAIT.search(sym.cache.get(r['rip'], '?'))
ppu_header, ppu_records = next(t for t in threads if t[0]['role'] == 'ppu')
period = ppu_header['period_ns']
prec = [r for r in ppu_records if r['guest_tid'] == GUEST_TID and lo <= r['guest_steps'] <= hi]
ta, tb = prec[0]['stamp'], prec[-1]['stamp']
print(f'window {(tb-ta)/1e9:.1f} s, {len(prec)} PPU samples, period {period/1e3:.0f} us')
def region(r, run):
    pc = r['pc']
    if 0x5c5200 <= pc < 0x5c5800: return 'pacing wait (usleep 50 loop 0x5c56d4/0x5c5230)' + (' [running]' if run else '')
    if 0x696c10 <= pc < 0x696e20 or 0x666040 <= pc < 0x666100:
        return 'SPU engine-counter wait ' + ('spin' if run else 'sleep')
    if 0x5564c0 <= pc < 0x556600: return 'SPU job-flag wait 0x5564d4 ' + ('spin' if run else 'sleep')
    return 'guest work/runtime' if run else 'sleep elsewhere'
lanes = []
for header, recs in threads:
    if header['role'] != 'spu': continue
    bins = collections.defaultdict(bool)
    for r in recs:
        if ta <= r['stamp'] <= tb: bins[(r['stamp'] - ta) // period] |= running(r)
    lanes.append((hex(recs[0]['guest_tid']), bins))
res = collections.defaultdict(lambda: [0, [0] * len(lanes), 0])
for r in prec:
    run = running(r); k = region(r, run); b = (r['stamp'] - ta) // period
    e = res[k]; e[0] += 1
    for i, (_, bins) in enumerate(lanes):
        if bins.get(b): e[1][i] += 1; e[2] += 1
tot = len(prec)
print(f'{"main-thread state":52s} {"wall%":>6s} {"lanes":>6s}  running% per lane: ' + ' '.join(f'{n:>9s}' for n, _ in lanes))
for k, (n, per, total) in sorted(res.items(), key=lambda kv: -kv[1][0]):
    print(f'{k:52s} {100*n/tot:6.1f} {total/n:6.2f}  ' + ' ' * 19 + ' '.join(f'{100*x/n:9.0f}' for x in per))
