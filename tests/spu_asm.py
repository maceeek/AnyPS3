"""Minimal SPU assembler for native regression programs (encodings: IBM SPU ISA v1.2).

Only the instructions the pool-lane tests need. Every encoding is checked by the production lifter in
tests/test_spu_pool_poll.py (the programs must behave as written), not by a second table here.
"""


def rr(op, rb, ra, rt):
    return (op << 21) | (rb << 14) | (ra << 7) | rt


def ri10(op, value, ra, rt):
    return (op << 24) | ((value & 0x3ff) << 14) | (ra << 7) | rt


def ri16(op, value, rt):
    return (op << 23) | ((value & 0xffff) << 7) | rt


def ri18(op, value, rt):
    return (op << 25) | ((value & 0x3ffff) << 7) | rt


# MFC / SPU channel numbers (CBEA v1.02 table of SPU channels).
SPU_RD_EVENT_STAT, SPU_WR_EVENT_MASK, SPU_WR_EVENT_ACK = 0, 1, 2
MFC_LSA, MFC_EAH, MFC_EAL, MFC_SIZE, MFC_TAG_ID, MFC_CMD = 16, 17, 18, 19, 20, 21
MFC_RD_ATOMIC_STAT, SPU_WR_OUT_MBOX = 27, 28
GETLLAR, PUTLLUC = 0xd0, 0xb0


class Asm:
    def __init__(self, base=0x100):
        self.base, self.words, self.labels, self.fixups = base, [], {}, []

    def label(self, name):
        self.labels[name] = self.base + 4 * len(self.words)

    def emit(self, word):
        self.words.append(word & 0xffffffff)

    def il(self, rt, value): self.emit(ri16(0x081, value, rt))
    def ila(self, rt, value): self.emit(ri18(0x21, value, rt))
    def ai(self, rt, ra, value): self.emit(ri10(0x1c, value, ra, rt))
    def ceq(self, rt, ra, rb): self.emit(rr(0x3c0, rb, ra, rt))
    def ceqi(self, rt, ra, value): self.emit(ri10(0x7c, value, ra, rt))
    def stqa(self, rt, address): self.emit(ri16(0x041, address >> 2, rt))
    def lqa(self, rt, address): self.emit(ri16(0x061, address >> 2, rt))
    def wrch(self, channel, rt): self.emit(rr(0x10d, 0, channel, rt))
    def rdch(self, rt, channel): self.emit(rr(0x00d, 0, channel, rt))
    def rchcnt(self, rt, channel): self.emit(rr(0x00f, 0, channel, rt))
    def stop(self, code): self.emit(code & 0x3fff)

    def _branch(self, op, rt, name):
        self.fixups.append((len(self.words), op, rt, name))
        self.emit(0)

    def br(self, name): self._branch(0x064, 0, name)
    def brz(self, rt, name): self._branch(0x040, rt, name)
    def brnz(self, rt, name): self._branch(0x042, rt, name)

    def finish(self):
        for index, op, rt, name in self.fixups:
            self.words[index] = ri16(op, (self.labels[name] - (self.base + 4 * index)) // 4, rt)
        return list(self.words)
