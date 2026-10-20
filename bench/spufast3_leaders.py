#!/usr/bin/env python3
"""Where do the block leaders of the generated SPU functions come from?  Reads the executable sources (image, base, bytes) from the
relocation index of a generated program.cpp (SpuRelocIndex records) and counts leaders by origin and the resulting block sizes:
  own     : source_leaders + direct branch targets of the chunk's own source (whole source, cross-chunk targets included)
  foreign : direct branch targets of OTHER sources that fall inside this source (the union that anyps3/spu_block.py adds)
Usage: spufast3_leaders.py PROGRAM_CPP"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anyps3 import spu_block as sb
from anyps3.spu import lift_parts

t = open(sys.argv[1], errors='replace').read()
i = t.index('static const SpuRelocIndex index({'); j = t.index('});', i)
recs = re.findall(r'\{(\d+),0x([0-9a-f]+),(\d+),(bytes_\d+)\}', t[i:j + 3])
blobs = {}
for m in re.finditer(r'static constexpr char (bytes_\d+)\[\]=\n((?:"[^\n]*"\n)+);', t):
    s = ''.join(re.findall(r'"([^"\n]*)"', m.group(2)))
    blobs[m.group(1)] = bytes(int(x, 16) for x in re.findall(r'\\x([0-9a-f]{2})', s))
sources = list(dict.fromkeys((int(b, 16), blobs[n]) for _, b, _, n in recs))
print(len(sources), 'unique sources,', sum(len(p) for _, p in sources) // 4, 'instructions')
decoded = {}
S = []
for base, payload in sources:
    insns = []
    for k in range(0, len(payload), 4):
        key = (base + k, payload[k:k + 4])
        if key not in decoded:
            decoded[key] = sb.decode(base + k, int.from_bytes(key[1], 'big'), lift_parts)
        insns.append(decoded[key])
    S.append((base, payload, insns, sb.source_leaders(insns), sb.direct_targets(insns)))
allt = set().union(*(x[4] for x in S))
own_total = foreign_total = insn_total = 0
blocks_old = blocks_new = 0
for base, payload, insns, sl, dt in S:
    hi = base + len(payload)
    own = {p for p in sl | dt if base <= p < hi} | {base}
    old = own | {p for p in allt if base <= p < hi}
    # proposed: foreign targets only when the branch leaves the range of the source that contains it
    cross = {p for b2, pl2, _, _, dt2 in S for p in dt2 if base <= p < hi and not (b2 <= p < b2 + len(pl2))}
    new = own | cross
    def nblocks(leaders):
        n = 0; cur = 0
        for x in insns:
            if cur and (x.pc in leaders or cur == sb.MAX_BLOCK): n += 1; cur = 0
            cur += 1
            if x.kind not in ('plain', 'store'): n += 1; cur = 0
        return n + (1 if cur else 0)
    blocks_old += nblocks(old); blocks_new += nblocks(new); insn_total += len(insns)
print(f'instructions {insn_total}: blocks with the global target union {blocks_old} (avg {insn_total/blocks_old:.2f}); '
      f'with own + cross-range targets only {blocks_new} (avg {insn_total/blocks_new:.2f})')

# Why does each block end (new policy)?  leader = next instruction is a leader, kind = terminator class, max = MAX_BLOCK.
import collections
term = collections.Counter(); size = collections.defaultdict(int)
for base, payload, insns, sl, dt in S:
    hi = base + len(payload)
    leaders = {p for p in sl | dt if base <= p < hi} | {base} | {p for b2, pl2, _, _, dt2 in S for p in dt2 if base <= p < hi and not (b2 <= p < b2 + len(pl2))}
    cur = 0
    for k, x in enumerate(insns):
        if cur and x.pc in leaders:
            term['leader'] += 1; size['leader'] += cur; cur = 0
        elif cur == sb.MAX_BLOCK:
            term['max'] += 1; size['max'] += cur; cur = 0
        cur += 1
        if x.kind not in ('plain', 'store'):
            term[x.kind] += 1; size[x.kind] += cur; cur = 0
    if cur: term['tail'] += 1; size['tail'] += cur
tot = sum(term.values())
print('block terminators (new policy):', ', '.join(f'{k} {v} ({100*v/tot:.0f}%, avg {size[k]/v:.1f})' for k, v in term.most_common()))

# Address-taken code: ILA immediates (function pointers / computed-branch targets) that fall inside the source's own range.
extra = 0; newb = 0; hit = collections.Counter()
for base, payload, insns, sl, dt in S:
    hi = base + len(payload)
    leaders = {p for p in sl | dt if base <= p < hi} | {base} | {p for b2, pl2, _, _, dt2 in S for p in dt2 if base <= p < hi and not (b2 <= p < b2 + len(pl2))}
    ila = set()
    for x in insns:
        if x.word >> 25 == 0x21 >> 1 and (x.word >> 24) == 0x21 or (x.word >> 25) == 0x10 and False:
            pass
    for x in insns:
        if (x.word >> 25) == 0x21:        # ILA: 0x21 << 7 (7-bit opcode 0100001)
            v = (x.word >> 7) & 0x3ffff
            if base <= v < hi and v % 4 == 0: ila.add(v)
    fresh = ila - leaders
    extra += len(fresh)
    for v in fresh: hit[v] += 1
print(f'ILA code-address immediates inside their own source that are not leaders: {extra}')
for want in (0x4980, 0x54a0, 0x5690, 0x120, 0x28ecc):
    print(hex(want), 'is such an ILA target in', hit.get(want, 0), 'sources')
