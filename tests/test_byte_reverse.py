"""Byte-reversed indexed loads and stores (Power ISA 2.02 lwbrx/lhbrx/stwbrx/sthbrx)."""
import shutil
import unittest
from anyps3.flow import lift
from tests import test_runtime_objects


def x_form(rt, ra, rb, xo):
    return (31 << 26) | (rt << 21) | (ra << 16) | (rb << 11) | (xo << 1)


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class ByteReverseTests(unittest.TestCase):
    def test_reversed_access_widths_zero_extension_and_ra_zero(self):
        body = r'''
 (void)call; s.r[21]=UINT64_C(0xffffffff11223344); s.r[7]=0x10100;
'''
        body += '{auto& state=s;auto& r=s.r;(void)r;' + lift(0x7ea03d2c, 0x4f2690).cpp + '}'
        body += r'''
 assert(mem.load(0x10100,1)==0x44 && mem.load(0x10103,1)==0x11 && mem.load(0x10104,4)==0);
 s.r[3]=0x10000; s.r[4]=0x100;
'''
        body += '{auto& state=s;auto& r=s.r;(void)r;' + lift(x_form(5, 3, 4, 534), 0x1000).cpp + '}'
        body += ' assert(s.r[5]==UINT64_C(0x11223344));\n'
        body += ' mem.store(0x10100,4,0x8899aabb);'
        body += '{auto& state=s;auto& r=s.r;(void)r;' + lift(x_form(6, 3, 4, 790), 0x1004).cpp + '}'
        body += ' assert(s.r[6]==0x9988);\n s.r[8]=UINT64_C(0x123456789abcdef0);'
        body += '{auto& state=s;auto& r=s.r;(void)r;' + lift(x_form(8, 3, 4, 918), 0x1008).cpp + '}'
        body += ' assert(mem.load(0x10100,4)==0xf0deaabb);\n'
        test_runtime_objects.RuntimeObjectsTests().run_native(body)
