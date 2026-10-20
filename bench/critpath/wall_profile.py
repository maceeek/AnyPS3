#!/usr/bin/env python3
"""Wall-clock PPU sample analysis (ANYPS3_SAMPLE_PROFILE_WALL=ppu + ANYPS3_SAMPLE_PROFILE_GUEST_STACK=1).

Usage: wall_profile.py SAMPLE_DIR PROGRAM STEPS_LO STEPS_HI [--tid 1] [--top 25]
Each record is one wall-time sample of the main host thread: addresses[0] = interrupted host RIP, addresses[1..] = guest LR and
saved return addresses, guest_pc = last published PC, flags bit0 = pc valid.
"""
import argparse, collections, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'local/ratchet/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_report as sr

ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path)
ap.add_argument('lo', type=int); ap.add_argument('hi', type=int)
ap.add_argument('--tid', type=int, default=1); ap.add_argument('--top', type=int, default=25)
ap.add_argument('--role', default='ppu')
a = ap.parse_args()

recs = []
period = 0
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != a.role: continue
    period = d['period_ns']
    recs += [r for r in d['records'] if a.lo <= r['guest_steps'] <= a.hi and r['guest_tid'] == a.tid]
print(f'{len(recs)} samples of guest thread {a.tid}, period {period} ns -> {len(recs)*period/1e9:.2f} s of wall time')
maps = sr.read_maps(a.dir / 'maps.txt')
sym = sr.Symbols(a.program, maps)
rips = {r['addresses'][0] for r in recs}
sym.prepare(rips)
def name(rip): return sym.cache.get(rip, '?')
WAIT = re.compile(r'nanosleep|futex|pthread_cond|syscall|__lll|sem_|ppoll|poll|clock_nanosleep|sleep_for')
state = collections.Counter(); per_state_pc = {'run': collections.Counter(), 'wait': collections.Counter()}
sleep_rips = collections.Counter()
for r in recs:
    n = name(r['addresses'][0])
    kind = 'wait' if WAIT.search(n) else 'run'
    state[kind] += 1
    per_state_pc[kind][r['pc'] >> 6 << 6] += 1
    if kind == 'wait': sleep_rips[n] += 1
tot = max(len(recs), 1)
print('host state:', {k: f'{100*v/tot:.1f}%' for k, v in state.items()})
print('wait RIPs:', sleep_rips.most_common(6))
def regions(pc):
    if 0x666040 <= pc < 0x666100: return 'delay fn 0x666068'
    if 0x5564c0 <= pc < 0x556600: return 'wait fn 0x5564d4'
    if 0x696c10 <= pc < 0x696e20: return 'engine-counter wait 0x696c10'
    if 0x665600 <= pc < 0x665900: return 'rsx wait loops 0x665668/0x665800'
    return None
for kind in ('run', 'wait'):
    agg = collections.Counter()
    for pc, n in per_state_pc[kind].items():
        agg[regions(pc) or 'other guest code'] += n
    print(f'-- {kind}: {sum(agg.values())} samples ({100*sum(agg.values())/tot:.1f}% of wall)')
    for k, v in agg.most_common(): print(f'   {100*v/tot:6.2f}%  {k}')
    print('   top 64-byte PC buckets:')
    for pc, v in per_state_pc[kind].most_common(a.top // 2 + 4): print(f'     {100*v/tot:6.2f}%  {pc:#010x}')
# Callers of the wait functions (guest LR chain)
print('-- callers (LR / next return) of samples inside the wait/delay functions:')
chains = collections.defaultdict(collections.Counter)
for r in recs:
    reg = regions(r['pc'])
    if not reg: continue
    ad = r['addresses'][1:]
    key = ' <- '.join(f'{x:#x}' for x in ad[:3]) if ad else '(no chain)'
    chains[reg][key] += 1
for reg, c in chains.items():
    print(f'   {reg}: {sum(c.values())} samples ({100*sum(c.values())/tot:.1f}% of wall)')
    for k, v in c.most_common(8): print(f'      {100*v/tot:6.2f}%  {k}')
