"""SPUFAST3: block leaders of the generated SPU functions come from the function's own source plus branches that leave a source's
range, not from the union of every source's branch targets (that union cut the translated code into ~2-instruction blocks:
197571 blocks for 412350 instructions in build 600; ~6.9 instructions per block with the narrower rule)."""
import re
import unittest

from anyps3.spu import lift_parts
from anyps3.spu_block import generate_blocks
from tests.spu_asm import Asm
from tests.test_spu_block import image


def words(base, build):
    a = Asm(base)
    build(a)
    return {base + 4 * i: w for i, w in enumerate(a.finish())}


def starts(functions, base):
    """Block start addresses of the function whose first block starts at `base`."""
    for _, text in functions:
        labels = [int(x, 16) for x in re.findall(r'^spu_b_([0-9a-f]+):\{', text, re.M)]
        if labels and labels[0] == base:
            return set(labels)
    raise AssertionError(f'no function at {base:#x}')


class SpuLeaderTests(unittest.TestCase):
    def test_branch_targets_of_another_source_inside_the_same_range_are_not_leaders(self):
        def a_code(a):
            a.ai(3, 3, 1); a.ai(3, 3, 2); a.br('mid'); a.ai(3, 3, 3)
            a.label('mid'); a.ai(3, 3, 4); a.ai(3, 3, 5); a.ai(3, 3, 6); a.ai(3, 3, 7)

        def b_code(a):                       # same address range, different content, no branches
            for k in range(8):
                a.ai(4, 4, 8 + k)
        wa, wb = words(0x100, a_code), words(0x100, b_code)
        functions, _ = generate_blocks([image(wa), image(wb)], ((), ()), lift_parts)
        fa = [t for _, t in functions if 'spu_b_110:' in t]       # source A has the target 0x110 (label 'mid')
        self.assertEqual(len(fa), 1)
        for _, text in functions:
            if text is fa[0]:
                continue
            labels = {int(x, 16) for x in re.findall(r'^spu_b_([0-9a-f]+):\{', text, re.M)}
            self.assertEqual(labels, {0x100}, 'a branch target of source A must not split source B into blocks')

    def test_branch_that_leaves_its_source_makes_a_leader_in_the_source_it_lands_in(self):
        # BR rel (opcode 0x064): the branch at 0x104 leaves source A's range [0x100, 0x108) for 0x2008 inside source D
        a_words = {0x100: 0x1c004183, 0x104: (0x64 << 23) | ((((0x2008 - 0x104) >> 2) & 0xffff) << 7)}

        def d_code(a):
            for k in range(8):
                a.ai(5, 5, k + 1)
        wd = words(0x2000, d_code)
        functions, _ = generate_blocks([image(a_words), image(wd)], ((), ()), lift_parts)
        self.assertIn(0x2008, starts(functions, 0x2000), 'a branch from another source into this range is an entry point')


if __name__ == '__main__':
    unittest.main()
