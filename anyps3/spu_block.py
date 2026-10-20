"""SPUFAST S1: validated SPU superblocks with in-register values.

Every executable segment and every original overlay ("source") is cut into
basic blocks.  A block is entered only after one comparison of all its original
instruction bytes with the current LS contents (relocated by ``t.reloc_delta``),
then runs as straight host code: no per-instruction fetch, PC store, interrupt
or limit check.  Correctness arguments (see docs in RESULT.md):

* LS can change only through (a) this SPU's stores, (b) its own channel
  operations (DMA, owner RPC) and (c) external writers.  Channel/STOP/halt
  instructions are one-instruction blocks, so every successor block validates
  again.  A store that overlaps not yet executed instructions of the running
  block ends the block at the next instruction.  External writers (PPU, peer
  SPU DMA) pause the lane at a scheduler slice boundary (SpuOwnerScope), i.e.
  never inside a block.
* Interrupts are sampled at every block entry.  The SPU takes an interrupt
  asynchronously at an instruction boundary (SPU ISA v1.2 ch. 12; CBE Handbook
  v1.1 18.3); a block entry is such a boundary and blocks are bounded
  (``MAX_BLOCK``), so the delivery delay is bounded and no event is lost: the
  condition stays latched in SPU_RdEventStat until acknowledged.
* The soft scheduler slice may end after a whole block (overshoot below
  ``MAX_BLOCK``).  The hard instruction limit stays exact: a block that would
  cross it is declined and the interpreter executes the exact prefix.
* Entries at a PC that is not a block leader (budget resume, interrupt return,
  computed jumps into a block) use the exact interpreter until the next leader.
"""
import json
import re

from .errors import AnyPS3Error
from .spu_interp import Expr
from . import spu_simd

MAX_BLOCK = 64
MAX_FUNCTION = 1024
REGION = 64  # validation granule (bytes) of the per-call validated-region bitmap
_EFFECT_TOKENS = ('spu_stop(', 'channel', 'throw', 'spu_dynamic_fault', 'spu_external_event', 'spu_interrupt_return')
_INDIRECT = (0x1a8, 0x1a9, 0x128, 0x129, 0x12a, 0x12b, 0x1ab, 0x1aa)
_STORES = lambda op: op >> 2 in (0x41, 0x47) or op >> 3 == 0x24 or op == 0x144


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


class Insn:
    __slots__ = ('pc', 'word', 'op', 'kind', 'body', 'nxt', 'target')

    def __init__(self, pc, word, kind, body='', nxt='', target=None):
        self.pc, self.word, self.op, self.kind = pc, word, word >> 21, kind
        self.body, self.nxt, self.target = body, nxt, target


def decode(pc, word, lift_parts):
    """Classify one original instruction; bodies use the relocated address."""
    op = word >> 21
    try:
        body, nxt = lift_parts(Expr(f'(0x{pc:x}u+delta)'), word)
    except AnyPS3Error as error:
        return Insn(pc, word, 'effect', f'spu_dynamic_fault(t,ls,{json.dumps(str(error))});')
    si16 = _signed((word >> 7) & 65535, 16)
    if op >> 2 in (0x64, 0x66):
        return Insn(pc, word, 'br', body, nxt, (pc + si16 * 4) & 0x3fffc)
    if op >> 2 in (0x60, 0x62):
        return Insn(pc, word, 'bra', body, nxt, (si16 * 4) & 0x3fffc)
    if op >> 2 in (0x40, 0x42, 0x44, 0x46):
        return Insn(pc, word, 'brc', body, nxt, (pc + si16 * 4) & 0x3fffc)
    if op in _INDIRECT:
        return Insn(pc, word, 'ind', body, nxt)
    if op == 0x10d and (16 <= ((word >> 7) & 127) <= 20 or ((word >> 7) & 127) == 22):
        # WRCH of an MFC parameter channel (LSA, EAH, EAL, size, tag id) or of the tag mask only stores into t.dma[] /
        # t.tag_mask: no LS access, no blocking, no event or interrupt state, cannot throw (spu_write_channel does exactly
        # this for channels 16-20 and 22), so it neither ends a block nor invalidates validated code.
        return Insn(pc, word, 'plain', body, nxt)
    if any(token in body for token in _EFFECT_TOKENS):
        return Insn(pc, word, 'effect', body, nxt)
    return Insn(pc, word, 'store' if _STORES(op) else 'plain', body, nxt)


class Values:
    """Block-local SPU register values (see spu_simd for representations)."""

    def __init__(self, lines):
        self.lines, self.regs, self.count = lines, {}, 0

    def emit(self, text):
        self.lines.append(text)

    def fresh(self):
        self.count += 1
        return f'v{self.count}'

    @staticmethod
    def constant(words, rep):
        if rep == 'b':
            words = [int.from_bytes(w.to_bytes(4, 'big'), 'little') for w in words]
        elif rep == 'h':
            words = [(w >> 16) | ((w & 0xffff) << 16) for w in words]
        return 'spu_vk(' + ','.join(f'0x{w:x}u' for w in words) + ')'

    def is_const(self, reg):
        entry = self.regs.get(reg)
        return entry is not None and entry['const'] is not None

    def const(self, reg):
        return self.regs[reg]['const']

    def _entry(self, reg):
        entry = self.regs.get(reg)
        if entry is None:
            name = self.fresh()
            self.emit(f'[[maybe_unused]] const SpuV {name}=spu_vld(g[{reg}]);')
            entry = self.regs[reg] = dict(reps={'b': name}, const=None, dirty=False)
        return entry

    def get(self, reg, rep):
        entry = self._entry(reg)
        reps = entry['reps']
        if rep in reps:
            return reps[rep]
        if entry['const'] is not None:
            expr = self.constant(entry['const'], rep)
        elif rep == 'b':
            expr = f'spu_vswap32({reps["w"]})' if 'w' in reps else f'spu_vswap16({reps["h"]})'
        elif rep == 'w':
            expr = f'spu_vswap32({reps["b"]})' if 'b' in reps else f'spu_vhalves({reps["h"]})'
        else:
            expr = f'spu_vswap16({reps["b"]})' if 'b' in reps else f'spu_vhalves({reps["w"]})'
        name = self.fresh()
        self.emit(f'[[maybe_unused]] const SpuV {name}={expr};')
        reps[rep] = name
        return name

    def word0(self, reg):
        """C++ expression: host value of architectural word 0."""
        entry = self._entry(reg)
        if entry['const'] is not None:
            return f'0x{entry["const"][0]:x}u'
        if 'w' in entry['reps']:
            return f'spu_vw0({entry["reps"]["w"]})'
        return f'spu_vb0({self.get(reg, "b")})'

    def set(self, reg, rep, expr, effect=False):
        name = self.fresh()
        self.emit(f'[[maybe_unused]] const SpuV {name}={expr};')
        self.regs[reg] = dict(reps={rep: name}, const=None, dirty=True)
        return name

    def set_const(self, reg, words):
        self.regs[reg] = dict(reps={}, const=tuple(w & 0xffffffff for w in words), dirty=True)

    def choose(self, regs):
        score = {'w': 0, 'b': 0, 'h': 0}
        for reg in regs:
            entry = self.regs.get(reg)
            if entry is None:
                score['b'] += 1
            elif entry['const'] is None:
                for rep in entry['reps']:
                    score[rep] += 1
        return max(('w', 'b', 'h'), key=lambda rep: score[rep])

    _FOLD = {'and': lambda x, y: x & y, 'or': lambda x, y: x | y, 'xor': lambda x, y: x ^ y,
             'nor': lambda x, y: ~(x | y), 'andc': lambda x, y: x & ~y, 'nand': lambda x, y: ~(x & y),
             'eqv': lambda x, y: ~(x ^ y), 'orc': lambda x, y: x | ~y}
    _EXPR = {'and': '_mm_and_si128({x},{y})', 'or': '_mm_or_si128({x},{y})', 'xor': '_mm_xor_si128({x},{y})',
             'nor': 'spu_vnot(_mm_or_si128({x},{y}))', 'andc': '_mm_andnot_si128({y},{x})',
             'nand': 'spu_vnot(_mm_and_si128({x},{y}))', 'eqv': 'spu_vnot(_mm_xor_si128({x},{y}))',
             'orc': '_mm_or_si128({x},spu_vnot({y}))'}

    def bitwise(self, t, a, b, operation):
        if self.is_const(a) and self.is_const(b):
            fold = self._FOLD[operation]
            self.set_const(t, [fold(x, y) for x, y in zip(self.const(a), self.const(b))])
            return
        rep = self.choose([r for r in (a, b) if not self.is_const(r)])
        x, y = self.get(a, rep), self.get(b, rep)
        self.set(t, rep, self._EXPR[operation].format(x=x, y=y))

    def bitwise_const(self, t, a, words, operation):
        if self.is_const(a):
            fold = self._FOLD[operation]
            self.set_const(t, [fold(x, y) for x, y in zip(self.const(a), words)])
            return
        rep = self.choose([a])
        x = self.get(a, rep)
        self.set(t, rep, self._EXPR[operation].format(x=x, y=self.constant(words, rep)))

    def selb(self, d, a, b, c):
        rep = self.choose([r for r in (a, b, c) if not self.is_const(r)])
        x, y, z = self.get(a, rep), self.get(b, rep), self.get(c, rep)
        self.set(d, rep, f'spu_vselb({x},{y},{z})')

    def materialize_b(self, reg):
        """Expression with BE bytes of reg, without changing the cache state."""
        entry = self.regs[reg]
        if entry['const'] is not None:
            return self.constant(entry['const'], 'b')
        reps = entry['reps']
        if 'b' in reps:
            return reps['b']
        return f'spu_vswap32({reps["w"]})' if 'w' in reps else f'spu_vswap16({reps["h"]})'

    def flush(self, regs=None):
        for reg, entry in sorted(self.regs.items()):
            if entry['dirty'] and (regs is None or reg in regs):
                self.emit(f'spu_vst(g[{reg}],{self.get(reg, "b")});')
                entry['dirty'] = False

    def pending_stores(self):
        return ''.join(f'spu_vst(g[{reg}],{self.materialize_b(reg)});'
                       for reg, entry in sorted(self.regs.items()) if entry['dirty'])

    def drop(self, regs):
        for reg in regs:
            self.regs.pop(reg, None)


class BlockValues(Values):
    """Values plus the block context needed by LS loads/stores."""

    def __init__(self, lines, block_end, insn_index, flen):
        super().__init__(lines)
        self.block_end, self.insn_index, self.current, self.flen = block_end, insn_index, None, flen

    def self_store_exit(self, address, pc, provenance=False):
        """A store overlapping this function's own (relocated) code range ends the call after the storing instruction:
        later instructions of the block, and regions validated earlier in this call, may have changed.  The next
        entry validates again.  Stores to data (the overwhelming majority) pay one range compare."""
        nxt = f'(a+0x{pc + 4 - self.block_start:x}u)'
        hit = f'((({address}+15u-flo)&0x3ffffu)<0x{self.flen + 15:x}u)'
        leave = f'{self.pending_stores()}block.executed=ex+{self.insn_index(pc) + 1};t.pc={nxt};return;'
        if provenance:
            return (f'if(__builtin_expect(prov|({hit}),0)){{if(prov)spu_ls_forget(t,{address},16);if({hit}){{{leave}}}}}')
        return f'if(__builtin_expect({hit},0)){{{leave}}}'

    def load(self, t, address):
        name = self.fresh()
        self.emit(f'const unsigned a{name}={address};')
        self.set(t, 'b', f'spu_vldp(ls+a{name})')

    def store(self, t, address):
        value = self.get(t, 'b')
        name = self.fresh()
        pc = self.current
        self.emit(f'const unsigned a{name}={address};spu_vstp(ls+a{name},{value});')
        # One unlikely branch for both rare conditions: provenance logging (diagnostic) and a store into the function's own range.
        self.emit(self.self_store_exit(f'a{name}', pc, provenance=True))


def _relocated(target):
    return f'((0x{target:x}u+delta)&0x3fffc)'


def source_leaders(insns):
    """Leaders implied by one source: successors of control/effect instructions,
    effects themselves and the first instruction after alignment padding that
    follows an unconditional transfer (typical function starts reached only by
    computed calls)."""
    leaders = set()
    for i, x in enumerate(insns):
        if x.kind not in ('plain', 'store'):
            leaders.add(x.pc + 4)
        if x.kind == 'effect':
            leaders.add(x.pc)
        if x.kind in ('br', 'bra', 'ind') and i + 1 < len(insns) and insns[i + 1].op in (1, 0x201):
            j = i + 1
            while j < len(insns) and insns[j].op in (1, 0x201):
                j += 1
            if j < len(insns):
                leaders.add(insns[j].pc)
    return leaders


def direct_targets(insns):
    return {x.target for x in insns if x.kind in ('br', 'brc', 'bra')}


def emit_function(name, code_name, lo, words, lift_parts, templates=True, insns=None, extra_leaders=()):
    """One native function for source bytes [lo, lo+4*len(words)) at original address lo.

    extra_leaders: direct branch targets and source leaders known from all sources."""
    hi = lo + 4 * len(words)
    if insns is None:
        insns = [decode(lo + 4 * i, w, lift_parts) for i, w in enumerate(words)]
    leaders = {lo} | {pc for pc in extra_leaders if lo <= pc < hi}
    leaders |= {pc for pc in source_leaders(insns) | direct_targets(insns) if lo <= pc < hi}
    blocks = []
    current = []
    for x in insns:
        if current and (x.pc in leaders or len(current) == MAX_BLOCK):
            blocks.append(current); current = []
        current.append(x)
        if x.kind not in ('plain', 'store'):
            blocks.append(current); current = []
    if current:
        blocks.append(current)
    starts = {b[0].pc for b in blocks}
    used_dispatch = any(b[-1].kind == 'ind' for b in blocks)
    flen = 4 * len(words)
    if flen > 64 * REGION:
        raise AnyPS3Error('SPU native function exceeds the validation bitmap')
    # ex/lim: instructions retired by this call and the budget below which a block may start without the
    # exact entry guard (soft slice, hard limit, interrupt sampling interval merged); vm: bitmap of REGION-byte
    # regions of this function whose LS bytes were proven equal to the translated bytes since the last event that
    # can change LS (a store of this function into its own range, a channel/effect instruction).  Both live in
    # registers; block.executed is only synchronised where the scope or a callee can observe it.
    lines = [f'void {name}(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls) {{',
             'auto& g=t.gpr;(void)g;(void)s;SpuBlockScope block(t);const std::uint32_t delta=t.reloc_delta;'
             f'const std::uint8_t* const code={code_name};',
             # flo = LS address of the function start in this call.  With a relocation the function's window
             # [flo, flo+flen) may wrap around the end of the 256 KB LS (code linked above the lowest loaded
             # address): every LS address derived from it is therefore masked (region compares, self-store check).
             f'const unsigned flo=(0x{lo:x}u+delta)&0x3ffffu;const bool prov=spu_provenance_on;(void)flo;(void)prov;'
             'unsigned ex=0,lim=spu_block_lim(t,block);std::uint64_t vm=0;(void)vm;']
    if used_dispatch:
        lines.append('spu_dispatch_local:')
    lines.append('switch((t.pc-delta)&0x3fffc){')
    lines.extend(f'case 0x{pc:x}:goto spu_b_{pc:x};' for pc in sorted(starts))
    lines.append('default:block.executed=ex;t.dynamic_miss=true;return;}')

    def edge(target):
        if target in starts:
            return f'goto spu_b_{target:x};'
        return f'{{block.executed=ex;t.pc={_relocated(target)};return;}}'

    for block in blocks:
        start = block[0].pc
        end = block[-1].pc + 4
        length = end - start
        offset = start - lo
        lines.append(f'spu_b_{start:x}:{{')
        region_lo, region_hi = offset // REGION, (offset + length - 1) // REGION
        mask = ((1 << (region_hi + 1)) - 1) ^ ((1 << region_lo) - 1)
        lines.append(f'const unsigned a=(0x{start:x}u+delta)&0x3fffcu;')
        # First tier: the block's (at most two) regions are compared inline in one straight-line expression (the first block
        # touched in each region of a call); everything else (budget gone, regions differ, wide blocks) calls the
        # out-of-line helper, which runs the exact entry guard (decline, dynamic miss, interrupt delivery) and returns
        # the new bitmap and budget by value so that ex/lim/vm stay in registers.
        slow_call = (f'block.executed=ex;const SpuSlow slow=spu_block_slow(s,t,block,ls,flo,code,0x{flen:x}u,a,0x{offset:x}u,{length},{len(block)},lim,vm,0x{mask:x}ull);'
                     f'if(!slow.ok)return;lim=slow.lim;vm=slow.vm;')
        if region_hi - region_lo <= 1:
            eq = '&&'.join(f'spu_region_eq<{min(REGION, flen - r * REGION)}>(ls,(flo+0x{r * REGION:x}u)&0x3ffffu,code+0x{r * REGION:x}u)'
                           for r in range(region_lo, region_hi + 1))
            lines.append(f'if(__builtin_expect((ex>=lim)|((vm&0x{mask:x}ull)!=0x{mask:x}ull),0)){{'
                         f'if(__builtin_expect((ex>=lim)||!({eq}),0)){{{slow_call}}}else vm|=0x{mask:x}ull;}}')
        else:
            lines.append(f'if(__builtin_expect((ex>=lim)|((vm&0x{mask:x}ull)!=0x{mask:x}ull),0)){{{slow_call}}}')
        index = {x.pc: i for i, x in enumerate(block)}
        values = BlockValues(lines, end, lambda pc: index[pc], flen)
        values.block_start = start
        last = block[-1]
        here = lambda pc: f'(a+0x{pc - start:x}u)'
        if last.kind == 'effect':
            # The effect may write LS (DMA) or let other writers run: forget every validated region, re-derive the budget.
            lines.append(f'block.executed=ex;t.pc=a;{{[[maybe_unused]] SpuRegister out{{}};{last.body}}}++ex;vm=0;lim=spu_block_lim(t,block);')
            lines.append(f'if(t.block_control_exit){{block.executed=ex;t.pc={_relocated((last.pc + 4) & 0x3fffc)};return;}}')
            lines.append(edge((last.pc + 4) & 0x3fffc) + '}')
            continue
        body_insns = block if last.kind in ('plain', 'store') else block[:-1]
        for x in body_insns:
            values.current = x.pc
            fn = spu_simd.template(x.word) if templates else None
            emitted = False
            if fn is not None:
                mark = len(lines)
                saved = {r: dict(e, reps=dict(e['reps'])) for r, e in values.regs.items()}
                saved_count = values.count
                try:
                    fn(values, x.word, f'(0x{x.pc:x}u+delta)')
                    emitted = True
                except spu_simd.Unsupported:
                    del lines[mark:]
                    values.regs, values.count = saved, saved_count
            if not emitted:
                used = {int(r) for r in re.findall(r'g\[(\d+)\]', x.body)}
                values.flush(used)
                if x.kind == 'store':
                    lines.append(f'{{[[maybe_unused]] SpuRegister out{{}};{x.body}{values.self_store_exit("address", x.pc)}}}')
                else:
                    lines.append(f'{{[[maybe_unused]] SpuRegister out{{}};{x.body}}}')
                values.drop(used)
        n = len(block)
        if last.kind in ('plain', 'store'):
            values.flush()
            lines.append(f'ex+={n};{edge((last.pc + 4) & 0x3fffc)}}}')
            continue
        values.current = last.pc
        if last.kind == 'br' or last.kind == 'bra':
            if last.op >> 2 in (0x62, 0x66):  # BRASL/BRSL link: word 0 = next actual address
                values.set(last.word & 127, 'b', f'spu_vlink({here(last.pc + 4)}&0x3fffc)')
            values.flush()
            if last.kind == 'br':
                lines.append(f'ex+={n};{edge(last.target)}}}')
            else:
                lines.append(f'block.executed=ex+{n};t.pc=0x{last.target:x}u;return;}}')
            continue
        if last.kind == 'brc':
            kind = last.op >> 2
            value = values.word0(last.word & 127)
            if kind in (0x44, 0x46):
                value = f'({value}&0xffffu)'
            values.flush()
            relation = '==' if kind in (0x40, 0x44) else '!='
            lines.append(f'ex+={n};if({value}{relation}0u){edge(last.target)}else {edge((last.pc + 4) & 0x3fffc)}}}')
            continue
        # Indirect branches (incl. interrupt flag updates, BISLED, IRET): precise state first.
        values.flush()
        lines.append(f't.pc={here(last.pc)};block.executed=ex+{n - 1};'
                     f'{{[[maybe_unused]] SpuRegister out{{}};{last.body}{last.nxt}}}ex+={n};lim=spu_block_lim(t,block);'
                     'goto spu_dispatch_local;}')
    lines.append('}')
    emit_function.last_starts = starts
    return '\n'.join(lines)


def collect_sources(images, overlays):
    """Unique (base, payload) executable sources in deterministic order."""
    sources = {}
    for index, image in enumerate(images):
        ls = bytearray(0x40000)
        for segment in image.segments:
            if segment.type != 1:
                continue
            a = segment.virtual_address
            if a + segment.memory_size > len(ls):
                raise AnyPS3Error('SPU segment outside local storage')
            data = image.data[segment.offset:segment.offset + segment.file_size]
            if segment.flags & 1:
                sources.setdefault((a, bytes(data[:len(data) & ~3])), None)
        for base, payload in overlays[index] if index < len(overlays) else ():
            if base % 4 or len(payload) % 4 or base + len(payload) > 0x40000:
                raise AnyPS3Error('Invalid static SPU overlay range')
            sources.setdefault((base, bytes(payload)), None)
    return [s for s in sources if len(s[1]) >= 4]


def generate_blocks(images, overlays, lift_parts):
    """Return ([(name, native function text)], dispatch C++ for program.cpp)."""
    functions = []
    ranges = []
    chunks = {}
    decoded = {}
    sources = []
    own_targets = []
    foreign = set()
    for base, payload in collect_sources(images, overlays):
        insns = []
        for i in range(0, len(payload), 4):
            key = (base + i, payload[i:i + 4])
            if key not in decoded:
                decoded[key] = decode(base + i, int.from_bytes(key[1], 'big'), lift_parts)
            insns.append(decoded[key])
        sources.append((base, payload, insns))
        targets = direct_targets(insns)
        own_targets.append(targets)
        # A direct branch that leaves the range of the source it sits in can only land in another source (cross-overlay
        # call, kernel <-> job module): that source needs a leader there.  A target inside the own range is a leader of
        # the own source only.  (The union of ALL sources' targets made every address that is a branch target in any of
        # ~200 kernel/module variants a leader of every other variant: 2.1 instructions per block instead of 6.9, i.e.
        # a register flush/reload and a validation guard every other instruction; bench/spufast3_leaders.py.)
        foreign |= {x for x in targets if not base <= x < base + len(payload)}
    for (base, payload, insns), targets in zip(sources, own_targets):
        leaders = source_leaders(insns) | targets | {x for x in foreign if base <= x < base + len(payload)}
        for at in range(0, len(payload), 4 * MAX_FUNCTION):
            data = payload[at:at + 4 * MAX_FUNCTION]
            key = (base + at, data)
            if key in chunks:
                chunks[key][1].update(leaders)
            else:
                chunks[key] = (insns[at // 4:at // 4 + MAX_FUNCTION], set(leaders))
    for number, ((lo, data), (insns, leaders)) in enumerate(chunks.items()):
        code = f'spu_code_{number}'
        name = f'spu_fn_{number}'
        words = [x.word for x in insns]
        literal = '\n'.join('"' + ''.join(f'\\x{b:02x}' for b in data[i:i + 64]) + '"'
                            for i in range(0, len(data), 64))
        text = (f'extern const std::uint8_t {code}[];\nconst std::uint8_t {code}[]=\n{literal};\n'
                + emit_function(name, code, lo, words, lift_parts, insns=insns, extra_leaders=leaders))
        starts = emit_function.last_starts
        bits = bytearray((len(words) + 7) // 8)
        for pc in starts:
            k = (pc - lo) // 4
            bits[k // 8] |= 1 << (k % 8)
        functions.append((name, text))
        ranges.append((lo, lo + len(data), code, name, bytes(bits)))
    dispatch = [f'extern const std::uint8_t {code}[];void {name}(PpuState&,NativeSpu::Thread&,std::uint8_t*);'
                f'static const std::uint8_t spu_lead_{name}[]={{{",".join(map(str, bits))}}};'
                for _, _, code, name, bits in ranges]
    dispatch.append('static const SpuNativeRange spu_native_ranges[]={'
                    + ''.join(f'{{0x{lo:x}u,0x{hi:x}u,{code},{name},spu_lead_{name}}},' for lo, hi, code, name, _ in ranges)
                    + '{0,0,nullptr,nullptr,nullptr}};')
    dispatch.append(f'static constexpr unsigned spu_native_range_count={len(ranges)};')
    dispatch.append(DISPATCH_CPP)
    return functions, '\n'.join(dispatch)


# Core declarations shared by all SPU translation units (appended to spu.CORE_CPP).
CORE_CPP = r'''
// ---- SPUFAST superblock entry ----
static constexpr unsigned SPU_MAX_BLOCK=''' + str(MAX_BLOCK) + r''';
static constexpr std::uint64_t SPU_IRQ_INTERVAL=256;
template<unsigned N> [[gnu::always_inline]] static inline bool spu_code_eq(const std::uint8_t* p,const std::uint8_t* e){
#if defined(__AVX512BW__)
    if constexpr(N==64){
        return _mm512_cmpeq_epi8_mask(_mm512_loadu_si512(p),_mm512_loadu_si512(e))==~0ull;
    }else
#endif
#if defined(__SSE2__)
    if constexpr(N>=16){
        __m128i acc=_mm_setzero_si128();
        for(unsigned i=0;i+16<=N;i+=16)acc=_mm_or_si128(acc,_mm_xor_si128(spu_vldp(p+i),spu_vldp(e+i)));
        if constexpr(N%16!=0)acc=_mm_or_si128(acc,_mm_xor_si128(spu_vldp(p+N-16),spu_vldp(e+N-16)));
        return _mm_movemask_epi8(_mm_cmpeq_epi8(acc,_mm_setzero_si128()))==0xffff;
    }else if constexpr(N==12){
        std::uint64_t x,y;std::uint32_t u,v;__builtin_memcpy(&x,p,8);__builtin_memcpy(&y,e,8);__builtin_memcpy(&u,p+8,4);__builtin_memcpy(&v,e+8,4);
        return !((x^y)|(u^v));
    }else if constexpr(N==8){std::uint64_t x,y;__builtin_memcpy(&x,p,8);__builtin_memcpy(&y,e,8);return x==y;}
    else{std::uint32_t x,y;__builtin_memcpy(&x,p,4);__builtin_memcpy(&y,e,4);return x==y;}
#else
    return !std::memcmp(p,e,N);
#endif
}
bool spu_block_enter(PpuState&,NativeSpu::Thread&,SpuBlockScope&,const std::uint8_t*,unsigned,const std::uint8_t*,unsigned,unsigned);
struct SpuSlow {std::uint64_t vm;std::uint32_t lim,ok;};
// Slow tier of the per-block guard (see spu_block.py): block.executed is already synchronised by the caller.
SpuSlow spu_block_slow(PpuState&,NativeSpu::Thread&,SpuBlockScope&,const std::uint8_t* ls,unsigned flo,const std::uint8_t* code,unsigned flen,
                       unsigned a,unsigned offset,unsigned length,unsigned count,unsigned lim,std::uint64_t vm,std::uint64_t mask);
// Number of instructions a call may retire before the exact block-entry guard must run again: the soft/hard
// budget (scope.fast) and, with interrupts enabled, the distance to the next interrupt sampling point.  Equivalent
// to the conditions `executed<fast && (!interrupts_enabled || steps+executed<irq_next)` the guard used per block.
// t.steps is constant during a call (the scope adds `executed` on exit), so the bound is computed once.
[[gnu::always_inline]] static inline unsigned spu_block_lim(const NativeSpu::Thread& t,const SpuBlockScope& block){
    unsigned lim=block.fast;
    if(t.interrupts_enabled){
        const std::uint64_t room=t.irq_next>t.steps?t.irq_next-t.steps:0;
        if(room<lim)lim=static_cast<unsigned>(room);
    }
    return lim;
}
// Regions (SPU_REGION bytes each) of a native function whose current LS bytes equal the translated bytes.
// `flo` = LS address of the function start (a relocated function's window [flo,flo+flen) may wrap around the end of
// the 256 KB LS, so the LS address of byte `off` is (flo+off)&0x3ffff), `code` = its translated bytes, `flen` = their
// length; `want` = bitmap of regions to test.  Returns the subset that matches completely (the last region may be
// partial).  A region that straddles the LS end is never reported valid: the exact per-block guard serves its blocks.
static constexpr unsigned SPU_REGION=''' + str(REGION) + r''';
template<unsigned N> [[gnu::always_inline]] static inline bool spu_region_eq(const std::uint8_t* ls,unsigned lsa,const std::uint8_t* code){
    return lsa<=0x40000u-N && spu_code_eq<N>(ls+lsa,code);
}
[[maybe_unused,gnu::noinline]] static std::uint64_t spu_regions_valid(const std::uint8_t* ls,unsigned flo,const std::uint8_t* code,unsigned flen,std::uint64_t want){
    std::uint64_t ok=0;
    for(std::uint64_t rest=want;rest;rest&=rest-1){
        const unsigned r=static_cast<unsigned>(__builtin_ctzll(rest)),off=r*SPU_REGION;
        if(off>=flen)continue;
        const unsigned n=std::min(SPU_REGION,flen-off),lsa=(flo+off)&0x3ffffu;
        if(lsa<=0x40000u-n && !std::memcmp(ls+lsa,code+off,n))ok|=std::uint64_t(1)<<r;
    }
    return ok;
}
'''

DISPATCH_CPP = r'''
// Exact entry guard for the slow cases of a superblock entry (see spu_block.py).
bool spu_block_enter(PpuState& s,NativeSpu::Thread& t,SpuBlockScope& block,const std::uint8_t* ls,unsigned a,const std::uint8_t* expected,unsigned length,unsigned count){
    spu_stat(t,26);
    if(block.executed>=block.limit){t.pc=a;return false;}
    if(block.hard-block.executed<count || (t.block_exact && block.limit-block.executed<count)){t.pc=a;t.block_decline=true;return false;}
    if(a>0x40000u-length || std::memcmp(ls+a,expected,length)){t.pc=a;t.dynamic_miss=true;return false;}
    // Asynchronous conditions are sampled at most every SPU_IRQ_INTERVAL retired
    // instructions; own channel/branch changes force the next entry to sample.
    if(t.interrupts_enabled && t.steps+block.executed>=t.irq_next){
        t.irq_next=t.steps+block.executed+SPU_IRQ_INTERVAL;
        t.pc=a;spu_take_interrupt(s,t);if(t.pc!=a)return false;
    }
    return true;
}
SpuSlow spu_block_slow(PpuState& s,NativeSpu::Thread& t,SpuBlockScope& block,const std::uint8_t* ls,unsigned flo,const std::uint8_t* code,unsigned flen,
                       unsigned a,unsigned offset,unsigned length,unsigned count,unsigned lim,std::uint64_t vm,std::uint64_t mask){
    // (1) Budget left: compare the block's regions that were not proven yet (first touch of a region in this call).
    if(block.executed<lim)vm|=spu_regions_valid(ls,flo,code,flen,mask&~vm);
    // (2) Budget gone or bytes differ: the exact guard decides.
    if(block.executed>=lim || (vm&mask)!=mask){
        if(!spu_block_enter(s,t,block,ls,a,code+offset,length,count))return {vm,lim,0};
        lim=spu_block_lim(t,block);vm|=spu_regions_valid(ls,flo,code,flen,mask&~vm);
    }
    return {vm,lim,1};
}
static const std::array<std::vector<const SpuNativeRange*>,64>& spu_native_pages(){
    static const auto pages=[]{
        std::array<std::vector<const SpuNativeRange*>,64> r;
        for(unsigned i=0;i<spu_native_range_count;++i){const auto& x=spu_native_ranges[i];
            for(unsigned page=x.lo>>12;page<=((x.hi-4)>>12) && page<64;++page)r[page].push_back(&x);}
        return r;
    }();
    return pages;
}
enum class SpuNativeResult {Progress,Miss,Decline};
static SpuNativeResult spu_native_call(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls,SpuNativeFunction fn,std::uint32_t delta){
    t.reloc_delta=delta;t.dynamic_miss=false;t.block_decline=false;spu_stat(t,8);
    const auto before=t.steps;
    auto count=[&]{if(delta && (!t.dynamic_miss || t.steps!=before)){auto& c=t.relocated_entries.value;c.store(c.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);}};
    try{fn(s,t,ls);}catch(...){count();throw;}
    count();
    if(t.steps!=before)return SpuNativeResult::Progress;
    if(t.block_decline){spu_stat(t,18);return SpuNativeResult::Decline;}
    if(t.dynamic_miss){spu_stat(t,17);return SpuNativeResult::Miss;}
    return SpuNativeResult::Progress;
}
static bool spu_native_candidates(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls,NativeSpu::Thread::DispatchEntry& slot,std::uint32_t pc,std::uint32_t original){
    const auto word=spu_load_word(ls+pc);const auto delta=(pc-original)&0x3fffc;
    for(const auto* r:spu_native_pages()[original>>12]){
        // Only the first word is a pre-filter: the block at pc may be shorter than any wider window, and the bytes after it
        // can legitimately differ from the translated source (the function validates exactly the bytes it executes).
        if(original<r->lo || original>=r->hi || spu_load_word(r->code+(original-r->lo))!=word)continue;
        slot={pc,delta,r->fn};spu_stat(t,21);
        const auto result=spu_native_call(s,t,ls,r->fn,delta);
        if(result==SpuNativeResult::Miss){spu_stat(t,22);slot.pc=UINT32_MAX;continue;}
        return result==SpuNativeResult::Progress;
    }
    return false;
}
// ANYPS3_SPU_STATS: count dispatcher entries per PC (open addressing, lane-written; reported by spu_dynamic_perf_report).
static void spu_note_entry(NativeSpu::Thread& t,std::uint32_t pc){
    if(!t.native_entries)t.native_entries=std::make_shared<std::array<std::atomic<std::uint64_t>,1024>>();
    auto& table=*t.native_entries;
    for(unsigned i=0,h=(pc>>2)*2654435761u>>22;i<64;++i,h=(h+1)&1023){
        const auto v=table[h].load(std::memory_order_relaxed);
        if(!v || (v>>32)==pc+1){table[h].store((std::uint64_t(pc+1)<<32)|((v&0xffffffffu)+1),std::memory_order_relaxed);break;}
    }
}
// True when native code made progress or changed state; false: interpret.
static bool spu_native_enter(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){
    const auto pc=t.pc;t.block_decline=false;
    if(__builtin_expect(spu_stats_on,0))spu_note_entry(t,pc);
    if(t.dispatch_cache.empty())t.dispatch_cache.resize(NativeSpu::Thread::dispatch_slots);
    auto& slot=t.dispatch_cache[(pc>>2)&(NativeSpu::Thread::dispatch_slots-1)];
    if(slot.pc==pc){
        spu_stat(t,9);
        const auto result=spu_native_call(s,t,ls,slot.fn,slot.delta);
        if(result!=SpuNativeResult::Miss)return result==SpuNativeResult::Progress;
        slot.pc=UINT32_MAX;spu_stat(t,20);
        if(t.pc!=pc)return true;
    }
    // Calls and returns of relocated code stay inside the image the previous native call ran: try its coordinates
    // before the unrelocated ones and before the content search.  Only a hint; every candidate validates its bytes.
    if(const auto last=t.reloc_delta){
        if(spu_native_candidates(s,t,ls,slot,pc,(pc-last)&0x3fffc))return true;
        if(t.block_decline || t.pc!=pc)return !t.block_decline;
    }
    if(spu_native_candidates(s,t,ls,slot,pc,pc))return true;
    if(t.block_decline || t.pc!=pc)return !t.block_decline;
    std::uint32_t original=0;
    spu_stat(t,23);
    if(spu_reloc_find(t,ls,original) && original!=pc){
        if(spu_provenance_enabled())spu_dynamic_record(t,ls,"relocated");
        spu_stat(t,24);
        if(spu_native_candidates(s,t,ls,slot,pc,original))return true;
    }
    return false;
}
// Whether a superblock starts at t.pc (dispatch hint or leader bit of a range whose
// first word matches); used to leave the interpreter as early as possible.
static bool spu_native_leader(NativeSpu::Thread& t,const std::uint8_t* ls,std::uint32_t delta){
    const auto pc=t.pc;
    if(!t.dispatch_cache.empty() && t.dispatch_cache[(pc>>2)&(NativeSpu::Thread::dispatch_slots-1)].pc==pc)return true;
    const auto word=spu_load_word(ls+pc);
    for(const auto original:{pc,(pc-delta)&0x3fffcu}){
        for(const auto* r:spu_native_pages()[original>>12]){
            if(original<r->lo || original>=r->hi)continue;
            const auto k=(original-r->lo)>>2;
            if(((r->leaders[k>>3]>>(k&7))&1) && spu_load_word(r->code+(original-r->lo))==word)return true;
        }
        if(!delta)break;
    }
    return false;
}
'''
