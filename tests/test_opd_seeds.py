import hashlib
import struct
import unittest

from anyps3.build_game import module_opd_seeds, opd_section_seeds
from anyps3.errors import AnyPS3Error
from anyps3.prx import make_image

TOC = 0x28000


def image(descriptors, code_size=0x40):
    # Entry OPD at 0x20000 defines the process TOC; the .opd table follows at 0x20100.
    data = bytearray(0x200)
    struct.pack_into('>II', data, 0, 0x10000, TOC)
    for i, (code, toc) in enumerate(descriptors):
        struct.pack_into('>II', data, 0x100 + 8 * i, code, toc)
    return make_image([(0x10000, 5, bytes(code_size)), (0x20000, 6, bytes(data))], entry=0x20000)


def spec(img, count, **override):
    raw = img.read_virtual(0x20100, 8 * count)
    result = dict(address=0x20100, size=8 * count, toc=TOC, sha256=hashlib.sha256(raw).hexdigest())
    result.update(override)
    return result


class OpdSectionSeedTests(unittest.TestCase):
    def test_unique_sorted_code_entries(self):
        img = image([(0x10010, TOC), (0x10004, TOC), (0x10010, TOC), (0x1003c, TOC)])
        self.assertEqual(opd_section_seeds(img, spec(img, 4)), [0x10004, 0x10010, 0x1003c])

    def test_rejections(self):
        cases = {
            'foreign TOC': ([(0x10004, TOC), (0x10008, TOC + 8)], {}),
            'unaligned': ([(0x10006, TOC)], {}),
            'not executable': ([(0x20000, TOC)], {}),
            'past code end': ([(0x10040, TOC)], {}),
            'identity': ([(0x10004, TOC)], {'sha256': '0' * 64}),
            'process TOC': ([(0x10004, TOC)], {'toc': TOC + 8}),
            'size': ([(0x10004, TOC)], {'size': 12}),
            'alignment': ([(0x10004, TOC)], {'address': 0x20104}),
        }
        for name, (descriptors, override) in cases.items():
            with self.subTest(name):
                img = image(descriptors)
                with self.assertRaises(AnyPS3Error):
                    opd_section_seeds(img, spec(img, len(descriptors), **override))


if __name__ == '__main__':
    unittest.main()


class ModuleOpdSeedTests(unittest.TestCase):
    def test_only_descriptors_with_module_toc_and_aligned_code_in_executable_segments(self):
        data = bytearray(0x80)
        struct.pack_into('>II', data, 0x00, 0x10010, TOC)        # valid
        struct.pack_into('>II', data, 0x08, 0x10013, TOC)        # unaligned code
        struct.pack_into('>II', data, 0x10, 0x10020, TOC + 8)    # foreign TOC
        struct.pack_into('>II', data, 0x18, 0x10040, TOC)        # one past the 0x40-byte code segment
        struct.pack_into('>II', data, 0x24, 0x1003c, TOC)        # valid at a 4-byte (not 8-byte) offset
        struct.pack_into('>II', data, 0x30, 0x20000, TOC)        # data, not code
        struct.pack_into('>II', data, 0x38, 0x10010, TOC)        # duplicate
        img = make_image([(0x10000, 5, bytes(0x40)), (0x20000, 6, bytes(data))], entry=0x20000)
        self.assertEqual(module_opd_seeds(img, TOC), [0x10010, 0x1003c])

