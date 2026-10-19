import struct
import unittest

from anyps3.jump_tables import detect
from anyps3.prx import make_image

BLR = 0x4E800020


def rldic(ra, rs, sh, mb):
    mbe = ((mb & 31) << 1) | (mb >> 5)
    return (30 << 26) | (rs << 21) | (ra << 16) | ((sh & 31) << 11) | (mbe << 5) | (2 << 2) | ((sh >> 5) << 1)


def x(op_rt, ra, rb, xo):
    return (31 << 26) | (op_rt << 21) | (ra << 16) | (rb << 11) | (xo << 1)


def image(bound_reg_clobber=False, wrong_pointer=False):
    code = [0x83C28000,                 # lwz r30,-32768(r2)
            0x2B830002,                 # cmplwi cr7,r3,2
            0x419D0038,                 # bt cr7.gt,+0x38
            0x813E0008,                 # lwz r9,8(r30)
            rldic(7, 3, 2, 30),
            x(6, 7, 9, 23),             # lwzx r6,r7,r9
            (31 << 26) | (6 << 21) | (4 << 16) | (986 << 1),  # extsw r4,r6
            x(3, 4, 9, 266),            # add r3,r4,r9
            0x7C6903A6,                 # mtctr r3
            0x4E800420,                 # bctr
            0x0C, 0x10, 0x14,           # table at 0x10028
            BLR, BLR, BLR, BLR]
    if bound_reg_clobber:
        code[3:3] = [0x38630001]        # addi r3,r3,1 between bound and dispatch
    data = bytearray(0x200)
    struct.pack_into('>I', data, 0, 0x20100)
    table = 0x1002C if bound_reg_clobber else 0x10028
    struct.pack_into('>I', data, 0x108, table - 4 if wrong_pointer else table)
    return make_image([(0x10000, 5, b''.join(struct.pack('>I', w) for w in code)), (0x20000, 6, bytes(data))])


class JumpTableTests(unittest.TestCase):
    def test_verified_canonical_table(self):
        result = detect(image(), 0x10024, 0x28000)
        self.assertEqual((result['table'], result['count'], result['bound'], result['toc_slot']), (0x10028, 3, 0x10008, 0x20000))
        self.assertEqual(result['targets'], [0x10034, 0x10038, 0x1003C])

    def test_rejects_unproven_base_or_clobbered_index(self):
        self.assertIsNone(detect(image(wrong_pointer=True), 0x10024, 0x28000))
        clobbered = image(bound_reg_clobber=True)
        self.assertIsNone(detect(clobbered, 0x10028, 0x28000))  # base valid, index rewritten
        self.assertIsNone(detect(image(), 0x10020, 0x28000))  # not a bctr


def dispatch_image(words, count, table_slot_value=None):
    """Code at 0x10000 ending in bctr; offsets follow it, then one blr per case.

    TOC 0x28000: lwz r30,-32768(r2) reads pointer cell 0x20000 -> 0x20100,
    and both lwz rB,8(r30) and lwz rB,-0x7ef8(r2) read cell 0x20108.
    """
    table = 0x10000 + 4 * len(words)
    offsets = [4 * count + 4 * i for i in range(count)]
    code = [*words, *offsets] + [BLR] * count
    data = bytearray(0x200)
    struct.pack_into('>I', data, 0, 0x20100)
    struct.pack_into('>I', data, 0x108, table if table_slot_value is None else table_slot_value)
    img = make_image([(0x10000, 5, b''.join(struct.pack('>I', w) for w in code)), (0x20000, 6, bytes(data))])
    return img, table, [table + o for o in offsets]


TAIL = [0x7CC7482E, 0x7CC507B4, 0x7F254A14, 0x7F2903A6, 0x4E800420]  # lwzx r6,r7,r9; extsw r5,r6; add r25,r5,r9; mtctr r25; bctr
# Original 0x1e6e4: lhzu r8,28(r31); cmplwi cr1,r8,5; extsh r0,r8; bgt cr1;
# lwz r9,..(r30); rlwinm r7,r0,2,14,29; lhz r11,12(r31). Base displacement -> 8.
EXTENDED = [0x83C28000, 0xA51F001C, 0x28880005, 0x7D000734, 0x41850700, 0x813E0008, 0x540713BA, 0xA17F000C, *TAIL]


class GeneralizedJumpTableTests(unittest.TestCase):
    def check(self, words, count, **kw):
        img, table, targets = dispatch_image(words, count, **kw)
        result = detect(img, table - 4, 0x28000)
        self.assertIsNotNone(result)
        self.assertEqual((result['table'], result['count'], result['targets']), (table, count, targets))
        return result

    def rejected(self, words, count):
        img, table, _ = dispatch_image(words, count)
        self.assertIsNone(detect(img, table - 4, 0x28000))

    def test_rlwinm_scale_with_extension_from_bounded_register(self):
        result = self.check(EXTENDED, 6)
        self.assertEqual((result['bound'], result['toc_slot']), (0x10010, 0x20000))

    def test_extension_must_preserve_every_bounded_value(self):
        extsb = EXTENDED.copy(); extsb[3] = 0x7D000774             # extsb r0,r8
        self.check(extsb, 6)
        wide = extsb.copy(); wide[2] = 0x28880080                  # cmplwi cr1,r8,128
        self.rejected(wide, 129)                                    # extsb maps 128 to -128
        narrow = EXTENDED.copy(); narrow[6] = 0x540717BA           # rlwinm r7,r0,2,30,29: wrapping mask
        self.rejected(narrow, 6)
        mask = EXTENDED.copy(); mask[6] = 0x5407173A               # rlwinm r7,r0,2,28,29 keeps 0..3
        self.rejected(mask, 6)

    def test_index_register_may_be_reused_only_after_scale(self):
        # Original 0x19d48: cmplwi cr6,r11,4; extsh r11,r11; stw; bgt cr6;
        # rlwinm r3,r11,2,14,29; lwz r9,8(r30); lhz r11,12(r29); lwzx/extsw/add/mtctr.
        words = [0x83C28000, 0x2B0B0004, 0x7D6B0734, 0x91481488, 0x4199047C, 0x556313BA, 0x813E0008,
                 0xA17D000C, 0x7C83482E, 0x7C8007B4, 0x7D804A14, 0x7D8903A6, 0x4E800420]
        self.check(words, 5)
        early = words.copy(); early[5], early[7] = 0xA17D000C, 0x556313BA  # lhz r11 before the scale
        self.rejected(early, 5)

    def test_bound_condition_field_may_not_be_overwritten(self):
        other = EXTENDED.copy(); other.insert(4, 0x2C030000)       # cmpwi cr0,r3,0
        self.check(other, 6)
        same = EXTENDED.copy(); same.insert(4, 0x2C830000)         # cmpwi cr1,r3,0
        self.rejected(same, 6)

    def test_table_cell_read_directly_from_toc(self):
        direct = EXTENDED[1:]; direct[4] = 0x81228108               # lwz r9,-0x7ef8(r2)
        self.assertEqual(self.check(direct, 6)['toc_slot'], 0x20108)
        img, table, _ = dispatch_image(direct, 6, table_slot_value=0x10000 + 4 * len(direct) + 4)
        self.assertIsNone(detect(img, table - 4, 0x28000))           # data must name this table

    def test_bound_on_cleared_copy_when_scale_reads_only_kept_bits(self):
        # Original 0xb3714: lhzu r7,44(r29); extsh r9,r7; addi r9,r9,-1; clrlwi r8,r9,16;
        # cmplwi cr7,r8,4; bgt cr7; lwz r4,..(r30); rlwinm r3,r9,2,14,29; lhz r11,12(r29);
        # lwzx r0,r3,r4; extsw r12,r0; add r10,r12,r4; mtctr r10; bctr.
        words = [0x83C28000, 0xA4FD002C, 0x7CE90734, 0x3929FFFF, 0x5528043E, 0x2B880004, 0x419D05FC,
                 0x809E0008, 0x552313BA, 0xA17D000C, 0x7C03202E, 0x7C0C07B4, 0x7D4C2214, 0x7D4903A6, 0x4E800420]
        self.check(words, 5)
        wider = words.copy(); wider[8] = 0x5523137A                 # rlwinm r3,r9,2,13,29 reads bit 16
        self.rejected(wider, 5)
        unrelated = words.copy(); unrelated[4] = 0x3908FFFF         # addi r8,r8,-1: r8 not clrlwi(r9)
        self.rejected(unrelated, 5)

    def test_update_load_writes_base_register(self):
        from anyps3.jump_tables import _effects
        self.assertEqual(_effects(0xE8C90009), ({6, 9}, set()))    # ldu r6,8(r9)
        self.assertEqual(_effects(0xE8C90008), ({6}, set()))       # ld r6,8(r9)
        self.assertIsNone(_effects(0x48000001))                    # bl


class ModuleTocSelectionTests(unittest.TestCase):
    def test_tables_in_firmware_module_code_use_that_module_toc(self):
        # Process TOC 0x30000 (entry OPD at 0x20180) differs from the module TOC 0x28000
        # that the canonical fixture's lwz r30,-32768(r2) relies on.
        from anyps3.elf import ElfImage
        from anyps3.program import compile_program
        base = image()
        data = bytearray(base.read_virtual(0x20000, 0x200))
        struct.pack_into('>II', data, 0x180, 0x10000, 0x30000)
        code = base.read_virtual(0x10000, 0x44)
        img = ElfImage.parse(make_image([(0x10000, 5, code), (0x20000, 6, bytes(data))], entry=0x20180).data)
        plain = compile_program(img, process_entry=True, diagnostic=True)
        self.assertEqual(plain.jump_tables, ())
        module = compile_program(img, process_entry=True, diagnostic=True, code_tocs=((0x10000, 0x10100, 0x28000),))
        self.assertEqual([t['targets'] for t in module.jump_tables], [[0x10034, 0x10038, 0x1003C]])
        self.assertTrue({0x10034, 0x10038, 0x1003C} <= {op.address for op in module.operations})


class LwaxDispatchTests(GeneralizedJumpTableTests):
    def test_lwax_merged_load_and_sign_extension(self):
        # Original firmware libssl 0x2005af3c..0x2005af58: cmplwi cr7,r3,K; bt cr7.gt; lwz r9,..(r2);
        # rldic r0,r3,2,30; lwax r0,r9,r0; add r0,r0,r9; mtctr r0; bctr. Base displacement -> cell 0x20108.
        words = [0x2B830004, 0x419D0100, 0x81228108, rldic(0, 3, 2, 30),
                 x(0, 9, 0, 341), x(0, 0, 9, 266), 0x7C0903A6, 0x4E800420]
        result = self.check(words, 5)
        self.assertEqual(result['toc_slot'], 0x20108)
        self.rejected([*words[:4], x(0, 0, 0, 341), *words[5:]], 5)    # base register lost (rA == rB)
        self.rejected([*words[:4], x(0, 9, 0, 343), *words[5:]], 5)    # lhax is not a word offset


class BranchEnteredDispatchTests(unittest.TestCase):
    """Bound by bf crN.gt into a dispatch block that follows an unconditional b
    (firmware libsysutil 0x201240c8 / 0x20124160)."""

    def build(self, extra_branch=False, bt=False):
        code = [0x2B9E0002,                                  # 0x10000 cmplwi cr7,r30,2
                (0x419D0000 if bt else 0x409D0000) | 0x10,   # 0x10004 bf/bt cr7.gt,+0x10 -> 0x10014
                0x38600000,                                  # 0x10008 li r3,0
                (0x409D000C if extra_branch else 0x60000000),# 0x1000c second bf -> 0x10018? (only if extra)
                0x48000030,                                  # 0x10010 b +0x30 (elsewhere)
                0x81628108,                                  # 0x10014 lwz r11,-0x7ef8(r2)
                rldic(9, 30, 2, 30),                         # 0x10018
                x(0, 9, 11, 23),                             # 0x1001c lwzx r0,r9,r11
                (31 << 26) | (0 << 21) | (0 << 16) | (986 << 1),  # 0x10020 extsw r0,r0
                x(0, 0, 11, 266),                            # 0x10024 add r0,r0,r11
                0x7C0903A6,                                  # 0x10028 mtctr r0
                0x4E800420,                                  # 0x1002c bctr
                0x10, 0x14, 0x18,                            # 0x10030 table: -> 0x10040,0x10044,0x10048
                0, BLR, BLR, BLR, BLR]
        if extra_branch:
            code[3] = 0x409D0008                             # bf cr7.gt,+8 -> 0x10014 as well
        data = bytearray(0x200)
        struct.pack_into('>I', data, 0x108, 0x10030)
        return make_image([(0x10000, 5, b''.join(struct.pack('>I', w) for w in code)), (0x20000, 6, bytes(data))])

    def test_unique_bf_entry_proves_bound(self):
        result = detect(self.build(), 0x1002C, 0x28000)
        self.assertEqual((result['table'], result['count'], result['bound'], result['toc_slot']), (0x10030, 3, 0x10004, 0x20108))
        self.assertEqual(result['targets'], [0x10040, 0x10044, 0x10048])

    def test_rejects_second_entry_or_inverted_condition(self):
        self.assertIsNone(detect(self.build(extra_branch=True), 0x1002C, 0x28000))
        self.assertIsNone(detect(self.build(bt=True), 0x1002C, 0x28000))
