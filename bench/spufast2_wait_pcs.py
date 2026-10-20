#!/usr/bin/env python3
"""Stamped wall-sample attribution of the main thread's SPU waits (needs ANYPS3_SAMPLE_PROFILE_WALL=ppu,spu, build with sampler stamps).
For every PPU sample of guest thread 1 inside the SPU engine-counter wait (0x696c10, delay 0x666040) or the job-flag wait (0x5564c0),
look at the SPU lanes' samples in the same time bin: how many lanes run (not parked), and WHERE (LS-PC class: kernel < 0x1000,
job/module code, parked-poll loops) and in which host function class (generated, interpreter/dispatch, DMA/memory, other).
Usage: spufast2_wait_pcs.py SAMPLE_DIR PROGRAM STEPS_LO STEPS_HI [FRAME_MS]   (steps = PPU guest steps of the window ends)"""
import collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'critpath'))
import rawsamples as rs
import sample_report as sr

d = Path(sys.argv[1]); program = Path(sys.argv[2]); lo, hi = int(sys.argv[3]), int(sys.argv[4])
frame_ms = float(sys.argv[5]) if len(sys.argv) > 5 else 33.4
maps = sr.read_maps(d / 'maps.txt'); sym = sr.Symbols(program, maps)
WAIT = re.compile(r'sem_trywait|nanosleep|futex|pthread_cond|__lll|syscall|poll|sleep')
threads = [rs.read(f) for f in sorted(d.glob('thread-*.samples'))]
sym.prepare({r['rip'] for _, recs in threads for r in recs})
ppu = next(t for t in threads if t[0]['role'] == 'ppu')
period = ppu[0]['period_ns']
prec = [r for r in ppu[1] if r['guest_tid'] == 1 and lo <= r['guest_steps'] <= hi]
ta, tb = prec[0]['stamp'], prec[-1]['stamp']
window_s = (tb - ta) / 1e9
def host_class(name):
    if WAIT.search(name): return 'parked'
    if re.search(r'spu_fn_\d+', name): return 'generated'
    if re.search(r'spu_step|interpret|spu_single|spu_float|spu_vfp|spu_native|spu_block|spu_reloc|spu_regions|memcmp|memcpy|_dl_mcount', name): return 'interp/dispatch/slow'
    if re.search(r'transfer|cpu_spans|Lease|PageBroker|publish|notify|spu_write_channel|spu_read|collect|ReservationNot', name): return 'dma/channel/memory'
    return 'other'
def ls_class(pc):
    if pc < 0x1000: return 'kernel(<0x1000)'
    if pc >= 0x3f000: return 'park/poll(>=0x3f000)'
    return 'module/job'
lanes = {}
for h, recs in threads:
    if h['role'] != 'spu': continue
    bins = collections.defaultdict(list)
    for r in recs:
        if ta <= r['stamp'] <= tb: bins[(r['stamp'] - ta) // period].append(r)
    lanes[hex(recs[0]['guest_tid'])] = bins
def region(pc):
    if 0x696c10 <= pc < 0x696e20 or 0x666040 <= pc < 0x666100: return 'engine-counter wait'
    if 0x5564c0 <= pc < 0x556600: return 'job-flag wait'
    return None
pcs = collections.defaultdict(collections.Counter)
syms = collections.defaultdict(collections.Counter)
tot = collections.Counter(); nrun = collections.defaultdict(collections.Counter); lscls = collections.defaultdict(collections.Counter); hostcls = collections.defaultdict(collections.Counter)
for r in prec:
    k = region(r['pc'])
    if not k: continue
    b = (r['stamp'] - ta) // period; tot[k] += 1; running = 0
    for name, bins in lanes.items():
        if name == '0x100': continue
        for lr in bins.get(b, ())[:1]:
            hc = host_class(sym.cache.get(lr['rip'], '?'))
            if hc == 'parked': continue
            running += 1; lscls[k][ls_class(lr['pc'])] += 1; hostcls[k][hc] += 1; pcs[k][lr['pc'] // 64 * 64] += 1; syms[k][re.sub(r'\(.*','',sym.cache.get(lr['rip'], '?'))[:48]] += 1
    nrun[k][running] += 1
frames = window_s * 1000 / frame_ms
print(f'window {window_s:.1f} s ({frames:.0f} frames of {frame_ms} ms), {len(prec)} PPU samples')
for k, n in tot.items():
    print(f'{k}: {n} bins = {n * period / 1e6 / frames:.2f} ms/frame; ig lanes running: ' + ' '.join(f'{m}:{100 * nrun[k][m] / n:.0f}%' for m in range(6)))
    t = sum(lscls[k].values())
    print('   lane samples by LS class: ' + ', '.join(f'{c} {100 * v / t:.0f}%' for c, v in lscls[k].most_common()))
    t = sum(hostcls[k].values())
    print('   lane samples by host class: ' + ', '.join(f'{c} {100 * v / t:.0f}%' for c, v in hostcls[k].most_common()))
    t = sum(pcs[k].values())
    print('   top LS 64-byte buckets of running lanes: ' + ' '.join(f'{b:#x}:{100 * v / t:.1f}%' for b, v in pcs[k].most_common(16)))
    t = sum(syms[k].values())
    print('   top host functions of running lanes: ' + '; '.join(f'{n} {100 * v / t:.1f}%' for n, v in syms[k].most_common(22)))
