#!/usr/bin/env python3
"""Per-host-instruction sample histogram of one function in one SPU lane's samples, joined with objdump.
Usage: spufast2_rip_hist.py SAMPLE_DIR PROGRAM SPU_ID_HEX FUNCTION_SUBSTR [--tail 0.5] [--top 60]"""
import argparse, collections, re, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local/ratchet/tools'))
import sample_report as sr
ap = argparse.ArgumentParser()
ap.add_argument('dir', type=Path); ap.add_argument('program', type=Path); ap.add_argument('spu', type=lambda x: int(x, 16)); ap.add_argument('func')
ap.add_argument('--tail', type=float, default=0.5); ap.add_argument('--top', type=int, default=60)
a = ap.parse_args()
base = None
for line in (a.dir / 'maps.txt').read_text().splitlines():
    if str(a.program) in line and ' 00000000 ' in line:
        base = int(line.split('-')[0], 16); break
nm = subprocess.run(['nm', '-C', '-S', '--defined-only', str(a.program)], capture_output=True, text=True).stdout
fns = []
for l in nm.splitlines():
    m = re.match(r'([0-9a-f]+) ([0-9a-f]+) [Tt] (.*)', l)
    if m and a.func in m.group(3) and '.cold' not in m.group(3): fns.append((int(m.group(1), 16), int(m.group(2), 16), m.group(3)))
lo, size, name = fns[0]
print(name, hex(lo), size)
for f in sorted(a.dir.glob('thread-*.samples')):
    d = sr.read_samples(f)
    if d['role'] != 'spu' or not d['records'] or d['records'][0]['guest_tid'] != a.spu: continue
    recs = d['records']; recs = recs[int(len(recs) * (1 - a.tail)):]
    hist = collections.Counter(); tot = 0
    for r in recs:
        v = r['addresses'][0] - base
        if lo <= v < lo + size: hist[v] += 1; tot += 1
    print(f'{tot} samples of {len(recs)} in function')
    out = subprocess.run(['objdump', '-d', '--no-show-raw-insn', '-C', f'--start-address={lo}', f'--stop-address={lo+size}', str(a.program)], capture_output=True, text=True).stdout
    asm = {}
    for l in out.splitlines():
        m = re.match(r'\s*([0-9a-f]+):\s+(.*)', l)
        if m: asm[int(m.group(1), 16)] = m.group(2)
    top = sorted(hist.items(), key=lambda kv: -kv[1])[:a.top]
    for v, n in top: print(f'{100*n/tot:6.2f}%  {v-lo:#07x}  {asm.get(v, "?")}')
    # category sums
    cat = collections.Counter()
    for v, n in hist.items():
        s = asm.get(v, '?').split()[0] if asm.get(v) else '?'
        cat[s] += n
    print('-- mnemonic share'); [print(f'  {100*n/tot:6.2f}% {k}') for k, n in cat.most_common(25)]
