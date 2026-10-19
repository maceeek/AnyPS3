"""Verified detection of GCC PPU switch jump tables in original images.

Recognizes the dispatch emitted for the game:

    cmplwi crN,rI,K ; ... ; bt crN.gt,default      (bound)
    or: cmplwi crN,rI,K ; bf crN.gt,L ... b ... L: (the only branch into L)
    lwz rB,d(rP) ; SCALE rT,rI ; lwzx rO,rT,rB ; extsw rX,rO   (or lwax rX,rT,rB)
    add rC,rX,rB ; mtctr rC ; bctr                 (dispatch, any order of
                                                    the first two)

SCALE is rldic rT,rI,2,30 or rlwinm rT,rI,2,MB,29 whose mask keeps every
index 0..K. Both read only the low word of rI, which cmplwi bounds.

The table base is proven from data, not assumed: rP is r2 itself or is
loaded from the TOC (lwz rP,D(r2)), and the word at rP+d must equal the
address right after the bctr, where the signed 32-bit offsets are stored. All K+1 targets must be
aligned executable code near the dispatch. Interleaved instructions must be
whitelisted with known GPR/CR effects and may not write a chain register
between its definition and use, nor the bound CR field. The index may only
be derived by mr or an extension/clear that preserves every value 0..K. Anything else is left untouched so execution stops
explicitly at the computed branch.
"""

from .elf import ElfImage
from .errors import AnyPS3Error

BCTR = 0x4E800420


def _field(word, lo, n):
    return (word >> (32 - lo - n)) & ((1 << n) - 1)


def _rt(w): return _field(w, 6, 5)
def _ra(w): return _field(w, 11, 5)
def _rb(w): return _field(w, 16, 5)
def _xo(w): return _field(w, 21, 10)
def _op(w): return w >> 26
def _crf(w): return _field(w, 6, 3)
def _simm(w): return (w & 0xFFFF) - (0x10000 if w & 0x8000 else 0)


_X_RA = {444, 28, 316, 60, 124, 412, 476, 284,        # or and xor andc nor orc nand eqv
         986, 922, 954, 26, 58,                       # extsw extsh extsb cntlzw cntlzd
         24, 536, 792, 824, 27, 539, 794}             # slw srw sraw srawi sld srd srad
_XO_RT = {266, 40, 235, 233, 104, 8, 10, 136, 138, 200, 202, 232, 234,
          75, 11, 73, 9, 491, 459, 489, 457}          # add subf mullw ... divdu
_X_LOAD = {23, 87, 279, 343, 21, 341, 534, 790, 339, 19}   # lwzx ... lhbrx mfspr mfcr
_X_LOAD_UPDATE = {55, 119, 311, 375, 53, 373}
_X_STORE = {151, 215, 407, 149, 662, 918, 535, 599, 663, 727, 103, 231, 359, 487, 983, 467}
_X_STORE_UPDATE = {183, 247, 439, 181, 567, 631, 695, 759}


def _effects(w):
    """(GPRs written, CR fields written) of a whitelisted instruction, or None."""
    op, rt, ra = _op(w), _rt(w), _ra(w)
    record = {0} if w & 1 else set()
    if op in (7, 8, 12, 14, 15, 32, 34, 40, 42, 48, 50):
        return ({rt} if op < 48 else set()), set()    # mulli subfic addic addi addis lwz lbz lhz lha lfs lfd
    if op == 13:
        return {rt}, {0}                              # addic.
    if op in (33, 35, 41, 43):
        return {rt, ra}, set()                        # lwzu lbzu lhzu lhau
    if op == 58 and w & 3 != 3:
        return ({rt, ra} if w & 3 == 1 else {rt}), set()   # ld ldu lwa
    if op in (36, 38, 44, 52, 54) or (op == 62 and w & 3 == 0):
        return set(), set()                           # stw stb sth stfs stfd std
    if op in (37, 39, 45, 49, 51, 53, 55) or (op == 62 and w & 3 == 1):
        return {ra}, set()                            # update forms
    if op in (24, 25, 26, 27):
        return {ra}, set()                            # ori oris xori xoris
    if op in (28, 29):
        return {ra}, {0}                              # andi. andis.
    if op in (20, 21, 23, 30):
        return {ra}, record                           # rlwimi rlwinm rlwnm rld*
    if op in (10, 11):
        return set(), {_crf(w)}                       # cmpli cmpi
    if op == 4:
        return set(), {6}                             # vector; vcmp. may write CR6
    if op == 59:
        return set(), ({1} if w & 1 else set())
    if op == 63:
        if _xo(w) in (0, 32, 64):
            return set(), {_crf(w)}                   # fcmpu fcmpo mcrfs
        return set(), ({1} if w & 1 else set())
    if op != 31:
        return None
    xo = _xo(w)
    if xo in (0, 32) and not w & 1:
        return set(), {_crf(w)}                       # cmp cmpl
    if xo in _X_RA or (w >> 2) & 0x1FF == 413:
        return {ra}, record                           # incl. sradi
    if (w >> 1) & 0x1FF in _XO_RT:
        return {rt}, record
    if w & 1:
        return None
    if xo in _X_LOAD:
        return {rt}, set()
    if xo in _X_LOAD_UPDATE:
        return {rt, ra}, set()
    if xo in _X_STORE:
        return set(), set()
    if xo in _X_STORE_UPDATE:
        return {ra}, set()
    return None


def _index_effect(w, ri):
    """For an instruction writing ri: (source register, largest preserved K).

    mr copies; extsw/clrldi keep the low word cmplwi bounds; extsh/extsb and
    clrlwi keep small values. The source register is traced further back.
    None means the index cannot be traced.
    """
    op, rs = _op(w), _rt(w)
    if _ra(w) != ri or w & 1:
        return None
    if op == 31 and _xo(w) == 444 and rs == _rb(w):
        return rs, 0xFFFFFFFF                         # mr ri,rs
    if op == 31:
        limit = {986: 0xFFFFFFFF, 922: 0x7FFF, 954: 0x7F}.get(_xo(w))
        return None if limit is None else (rs, limit)  # extsw extsh extsb ri,rs
    if op == 21 and _rb(w) == 0 and _field(w, 26, 5) == 31:
        return rs, (1 << (32 - _field(w, 21, 5))) - 1  # clrlwi ri,rs,MB
    if op == 30 and (w >> 2) & 7 == 0 and _field(w, 16, 5) == 0 and not (w >> 1) & 1:
        mb = _field(w, 21, 6)
        mb = (mb >> 1) | ((mb & 1) << 5)
        return rs, (1 << min(32, 64 - mb)) - 1         # clrldi ri,rs,MB
    return None


def _branches_to(word, entry, window=0x8000):
    """All relative b/bc words in +-window whose target is entry."""
    hits = []
    for a in range(max(0, entry - window), entry + window, 4):
        try:
            w = word(a)
        except AnyPS3Error:
            continue
        if _op(w) == 16 and not w & 2:
            disp = (w & 0xFFFC) - (0x10000 if w & 0x8000 else 0)
        elif _op(w) == 18 and not w & 2:
            disp = (w & 0x03FFFFFC) - (0x04000000 if w & 0x02000000 else 0)
        else:
            continue
        if (a + disp) & 0xFFFFFFFF == entry:
            hits.append((a, w))
    return hits


def detect(image: ElfImage, pc: int, toc: int):
    try:
        return _detect(image, pc, toc)
    except AnyPS3Error:
        return None


def _detect(image: ElfImage, pc: int, toc: int):
    word = lambda a: int.from_bytes(image.read_virtual(a, 4), 'big')
    if word(pc) != BCTR:
        return None
    # Dispatch chain, matched backwards by register flow.
    mtctr = word(pc - 4)
    if mtctr & 0xFC1FFFFF != 0x7C0903A6:
        return None
    rc = _rt(mtctr)
    found, others, at = {}, [], pc - 8
    rx = rb = ro = rt = None
    for _ in range(10):
        w = word(at)
        if 'add' not in found:
            if _op(w) == 31 and _xo(w) == 266 and not w & 1 and _rt(w) == rc:
                found['add'] = at, w
            else:
                others.append((at, w))
        elif 'extsw' not in found:
            add = found['add'][1]
            if _op(w) == 31 and _xo(w) == 986 and not w & 1 and _ra(w) in (_ra(add), _rb(add)):
                found['extsw'] = at, w; rx, ro = _ra(w), _rt(w)
                rb = _rb(add) if _ra(add) == rx else _ra(add)
            elif _op(w) == 31 and _xo(w) == 341 and not w & 1 and _rt(w) in (_ra(add), _rb(add)):
                # lwax rO,rT,rB = lwzx + extsw in one instruction (firmware libssl 0x2005af4c).
                rx = ro = _rt(w); rb = _rb(add) if _ra(add) == rx else _ra(add)
                if rb not in (_ra(w), _rb(w)) or _ra(w) == _rb(w):
                    return None
                found['extsw'] = found['lwzx'] = at, w; rt = _rb(w) if _ra(w) == rb else _ra(w)
            else:
                others.append((at, w))
        elif 'lwzx' not in found:
            if _op(w) == 31 and _xo(w) == 23 and not w & 1 and _rt(w) == ro and rb in (_ra(w), _rb(w)):
                found['lwzx'] = at, w; rt = _rb(w) if _ra(w) == rb else _ra(w)
            else:
                others.append((at, w))
        else:
            if 'scale' not in found and _op(w) in (21, 30) and _ra(w) == rt and not w & 1:
                found['scale'] = at, w
            elif 'lwz' not in found and _op(w) == 32 and _rt(w) == rb:
                found['lwz'] = at, w
            else:
                others.append((at, w))
            if 'scale' in found and 'lwz' in found:
                break
        at -= 4
    else:
        return None
    (scale_at, scale), (lwz_at, lwz) = found['scale'], found['lwz']
    if _op(scale) == 30:
        sh = _field(scale, 16, 5) | (((scale >> 1) & 1) << 5)
        mb = _field(scale, 21, 6)
        mb = (mb >> 1) | ((mb & 1) << 5)
        if (scale >> 2) & 7 != 2 or sh != 2 or mb != 30:
            return None
        scale_limit = 0xFFFFFFFF
    else:
        mb = _field(scale, 21, 5)
        if _field(scale, 16, 5) != 2 or _field(scale, 26, 5) != 29 or mb > 29:
            return None
        scale_limit = (1 << (30 - mb)) - 1
    ri = _rt(scale)
    rp, displacement = _ra(lwz), _simm(lwz)
    # Each chain register is live from its definition to its last use.
    live = [(rc, found['add'][0], pc - 4), (rx, found['extsw'][0], found['add'][0]),
            (ro, found['lwzx'][0], found['extsw'][0]), (rt, scale_at, found['lwzx'][0]),
            (rb, lwz_at, max(found['lwzx'][0], found['add'][0])),
            (ri, -1, scale_at), (rp, -1, lwz_at)]
    for address, w in others:
        effects = _effects(w)
        if effects is None or any(reg in effects[0] and start < address < end for reg, start, end in live):
            return None
    # Bound: walk back (straight line, whitelisted effects only) to bt crN.gt
    # and the cmplwi that defines it for the traced index register. GCC may
    # also bound rK = clrlwi(rI) while SCALE reads rI: valid when SCALE keeps
    # no bit outside the clrlwi mask, so both see the same index.
    at, bound, crf, index_limit, traced = at - 4, None, None, scale_limit, False
    dispatch_start, pending = at + 4, None
    for _ in range(64):
        w = word(at)
        base_load = _op(w) == 32 and _rt(w) == rp and _ra(w) == 2  # defines rP itself
        if pending and not traced and _op(w) == 21 and not w & 1 and (_ra(w), _rt(w), _rb(w)) == (pending[0], ri, 0) \
                and _field(w, 26, 5) == 31 and scale_limit <= (1 << (32 - _field(w, 21, 5))) - 1:
            count = pending[1]
            break
        if bound is not None and not pending and _op(w) == 10 and _crf(w) == crf and not _field(w, 10, 1):
            if _ra(w) == ri:
                count = (w & 0xFFFF) + 1
                break
            pending = _ra(w), (w & 0xFFFF) + 1
        elif bound is None and _op(w) == 16 and _field(w, 6, 5) == 12 and not w & 3 and _field(w, 11, 5) % 4 == 1:
            crf = _field(w, 11, 5) // 4
            bound = at
        elif bound is None and _op(w) == 18 and not w & 3:
            # The dispatch block follows an unconditional b, so it is entered
            # only by branches (firmware libsysutil 0x20124160). Exactly one
            # branch may reach it: bf crN.gt from the bound check, i.e. taken
            # iff the index is <= K. The walk continues back from that bf.
            hits = _branches_to(word, at + 4)
            if len(hits) != 1:
                return None
            branch, bw = hits[0]
            if _op(bw) != 16 or bw & 3 or _field(bw, 6, 5) & 0x1C != 4 or _field(bw, 11, 5) % 4 != 1:
                return None
            crf = _field(bw, 11, 5) // 4
            bound = at = branch
        else:
            effects = _effects(w)
            if effects is None or (rp in effects[0] and not base_load) or (bound is not None and crf in effects[1]):
                return None
            if bound is None and rb in effects[0]:
                return None
            if pending and (pending[0] in effects[0] or ri in effects[0]):
                return None
            if ri in effects[0]:
                step = _index_effect(w, ri)
                if step is None:
                    return None
                ri, index_limit, traced = step[0], min(index_limit, step[1]), True
        at -= 4
    else:
        return None
    if count - 1 > index_limit:
        return None
    # Base pointer: nearest preceding lwz rP,D(r2); its data must point at the
    # table. GCC may place the switch block after the epilogue blr, so the
    # prologue load can precede a blr. The data proof below still applies,
    # and tables only add translation targets: execution follows CTR.
    base_slot = toc if rp == 2 else None  # lwz rB,d(r2) reads the TOC itself
    at = dispatch_start
    for _ in range(0 if rp == 2 else 8192):
        w = word(at)
        if _op(w) == 32 and _rt(w) == rp and _ra(w) == 2:
            base_slot = toc + _simm(w)
            break
        at -= 4
    if base_slot is None:
        return None
    pointer = toc if rp == 2 else word(base_slot)
    table = word(pointer + displacement)
    if table != pc + 4 or count > 4096:
        return None
    targets = []
    for i in range(count):
        offset = word(table + 4 * i)
        offset -= 1 << 32 if offset & 0x80000000 else 0
        target = (table + offset) & 0xFFFFFFFF
        if target & 3 or abs(target - pc) > 0x100000:
            return None
        image.read_virtual(target, 4, executable=True)
        targets.append(target)
    return {'dispatch': pc, 'table': table, 'count': count, 'bound': bound,
            'toc_slot': toc + displacement if rp == 2 else base_slot, 'targets': targets}
