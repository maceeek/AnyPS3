"""Check emitted binary64 math against Python IEEE results and exact Fractions."""

from fractions import Fraction
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from anyps3.flow import lift
from tests.test_anyps3 import BLR, program


def bits(value):
    return struct.unpack('>Q', struct.pack('>d', value))[0]


def flags(value, exact, precision=53):
    encoded = bits(value)
    result = Fraction.from_float(value)
    inexact = result != exact
    upward = abs(result) > abs(exact)
    minimum=Fraction(2)**(-126 if precision==24 else -1022)
    classification = (18 if encoded >> 63 else 2) if value == 0 else (24 if encoded >> 63 else 20) if abs(result)<minimum else (8 if encoded >> 63 else 4)
    return (classification << 12) | (0x82020000 if inexact else 0) | (0x40000 if upward else 0) | (0x88000000 if exact and abs(exact)<minimum and inexact else 0)


@unittest.skipUnless(shutil.which('c++'), 'Native FP tests require c++.')
class FloatingTests(unittest.TestCase):
    def test_divide_special_results_status_aliases_and_original_loading_clamp(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        # Literal Book I v2.02 expectations: FPRF +inf=5, -inf=9,
        # QNaN=17; ZX=bit 5, VXZDZ=bit 10, VXIDI=bit 9, VXISI=bit 8.
        cases = [
            (bits(154.0), 0, 0x7ff0000000000000, 0x84005000),
            (bits(154.0), 1 << 63, 0xfff0000000000000, 0x84009000),
            (bits(-154.0), 0, 0xfff0000000000000, 0x84009000),
            (bits(-154.0), 1 << 63, 0x7ff0000000000000, 0x84005000),
            (1, 0, 0x7ff0000000000000, 0x84005000),
            (0, 0, 0x7ff8000000000000, 0xa0211000),
            (1 << 63, 0, 0x7ff8000000000000, 0xa0211000),
            (0x7ff0000000000000, 0x7ff0000000000000, 0x7ff8000000000000, 0xa0411000),
            (0x7ff0000000000000, 0, 0x7ff0000000000000, 0x5000),
            (0xfff0000000000000, bits(-2.0), 0x7ff0000000000000, 0x5000),
            (bits(2.0), 0xfff0000000000000, 1 << 63, 0x12000),
            (0, bits(-2.0), 1 << 63, 0x12000),
            (0x7ff8000020000000, 0, 0x7ff8000020000000, 0x11000),
            (0x7ff0000040000000, 0, 0x7ff8000040000000, 0xa1011000),
        ]
        body = '(void)call;\n'
        for precision in (24, 53):
            for a, b, out, status in cases:
                body += f's.fpscr=0; assert(fp_divide(s,UINT64_C(0x{a:016x}),UINT64_C(0x{b:016x}),{precision})==UINT64_C(0x{out:016x}) && s.fpscr==0x{status:08x}u);\n'
            for a, b, out, status in [
                (0x7ff0000000000000, bits(-0.75), 0x7ff0000000000000, 0x5000),
                (bits(0.75), 0xfff0000000000000, 0xfff0000000000000, 0x9000),
                (0xfff0000000000000, 0xfff0000000000000, 0xfff0000000000000, 0x9000),
                (0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000, 0xa0811000),
                (0x7ff0000000000000, 0x7ff0000040000000, 0x7ff8000040000000, 0xa1011000),
            ]:
                body += f's.fpscr=0x60000; assert(fp_add(s,UINT64_C(0x{a:016x}),UINT64_C(0x{b:016x}),{precision})==UINT64_C(0x{out:016x}) && s.fpscr==0x{status:08x}u);\n'
        body += r'''
 s.fpscr=0x02060000;
 assert(fp_divide(s,UINT64_C(0x3ff0000000000000),0)==UINT64_C(0x7ff0000000000000) && s.fpscr==0x86005000);
 // FX records a newly set exception bit, not reoccurrence of sticky ZX.
 s.fpscr=0x04060000;
 assert(fp_divide(s,UINT64_C(0x3ff0000000000000),0)==UINT64_C(0x7ff0000000000000) && s.fpscr==0x04005000);
 s.fpscr=0x20260000;
 assert(fp_divide(s,0,0)==UINT64_C(0x7ff8000000000000) && s.fpscr==0x20211000);
 s.fpscr=0;
 assert(fp_add(s,UINT64_C(0x7ff0000000000000),UINT64_C(0xbfe8000000000000))==UINT64_C(0x7ff0000000000000) && s.fpscr==0x5000);
 s.fpscr=0x04060000;
 assert(fp_add(s,UINT64_C(0xfff0000000000000),UINT64_C(0x7ff0000000000000))==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa4811000);
'''
        # Execute the actual original sequence, including endian guest store.
        body += 's.fpr[5]=UINT64_C(0x4063400000000000);s.fpr[6]=0;s.fpr[8]=UINT64_C(0x3fe8000000000000);s.r[24]=0x10100;s.fpscr=0x82004000;s.cr=0x12345678;\n'
        for word, pc in [(0xfda02818, 0x69f2b0), (0xed8d3024, 0x69f2b4),
                         (0xfc8c4028, 0x69f2b8), (0xfcc4622e, 0x69f2bc), (0xd0d80030, 0x69f2c0)]:
            body += '{auto& state=s;auto& r=s.r;(void)r;' + lift(word, pc).cpp + '}\n'
        body += r'''
 assert(s.fpr[12]==UINT64_C(0x7ff0000000000000) && s.fpr[4]==s.fpr[12]);
 assert(s.fpr[6]==UINT64_C(0x3fe8000000000000) && s.fpscr==0x86005000 && s.cr==0x12345678 && s.pc==0x69f2c4);
 assert(mem.load(0x10130,4)==0x3f400000 && mem.load(0x10130,1)==0x3f && mem.load(0x10131,1)==0x40);
'''
        for opcode in (59, 63):
            for target in (1, 2):
                word = (opcode << 26) | (target << 21) | (1 << 16) | (2 << 11) | (18 << 1) | 1
                body += 's.fpr[1]=UINT64_C(0x4063400000000000);s.fpr[2]=0;s.fpscr=0;s.cr=0x12345678;\n'
                body += '{auto& state=s;' + lift(word, 0x10000).cpp + '}\n'
                body += f'assert(s.fpr[{target}]==UINT64_C(0x7ff0000000000000) && s.fpscr==0x84005000 && s.cr==0x18345678);\n'
        # Enabled exceptions remain explicit and leave architectural state intact.
        for mode in (0x80, 0x40, 0x20, 0x10, 0x08):
            body += f's.fpr[12]=123;s.fpr[13]=UINT64_C(0x4063400000000000);s.fpr[6]=0;s.fpscr=0x{mode:x};s.pc=0x69f2b4;s.cr=0x12345678;\n'
            body += 'fault([&]{auto& state=s;' + lift(0xed8d3024, 0x69f2b4).cpp + '});\n'
            body += f'assert(s.fpr[12]==123 && s.fpscr==0x{mode:x} && s.cr==0x12345678 && s.pc==0x69f2b4);\n'
        RuntimeObjectsTests().run_native(body)

    def test_single_arithmetic_rounds_full_double_operands_once(self):
        import math
        from tests.test_runtime_objects import RuntimeObjectsTests
        root=math.sqrt(2.0)
        single=struct.unpack('>f',struct.pack('>f',2.0-root))[0] # 2-root is exact in binary64.
        body=f'(void)call; s.fpr[13]=UINT64_C(0x{bits(2.0):016x});s.fpr[0]=UINT64_C(0x{bits(root):016x});s.fpscr=0;\n'
        body+='{auto& state=s;'+lift(0xefad0028,0x6b2d4c).cpp+'}'
        exact=Fraction(2)-Fraction.from_float(root)
        body+=f' assert(s.fpr[29]==UINT64_C(0x{bits(single):016x}) && s.fpscr==0x{flags(single,exact,24):x}u);\n'
        RuntimeObjectsTests().run_native(body)

    def test_stfs_uses_unrounded_single_conversion(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        cases=[(bits(1.0),0x3f800000),(bits(2.0**0.5),0x3fb504f3),(bits(-0.1),0xbdcccccc),
               (0x8000000000000000,0x80000000),(0x7ff8000040000000,0x7fc00002),
               (bits(2.0**-127),0x00400000),(bits(2.0**-149),0x00000001),(bits(-3*2.0**-150),0x80000001),
               (bits(2.0**200),0x63800000)]
        body='(void)call;\n'+''.join(f' assert(fp_store_single(UINT64_C(0x{a:016x}))==0x{b:08x}u);\n' for a,b in cases)
        body+=r'''
 fault([&]{fp_store_single(UINT64_C(0x3680000000000000));}); // 2^-150: undefined.
 s.r[1]=0x10100;s.fpr[0]=UINT64_C(0x3ff6a09e667f3bcd);
'''
        body+='{auto& state=s;auto& r=s.r;(void)r;'+lift(0xd0010008,0x6b2d4c).cpp+'}'
        body+=' assert(mem.load(0x10108,4)==0x3fb504f3u);\n'
        RuntimeObjectsTests().run_native(body)

    def test_fsqrts_rounds_once_to_single(self):
        import math
        from tests.test_runtime_objects import RuntimeObjectsTests
        single = lambda n: Fraction.from_float(struct.unpack('>f', struct.pack('>I', n))[0])
        rng = random.Random(59)
        values = [2.0, 4.0, 0.25, 3.0, 0.1, 123456.789, 2.0**-140, 1e30, 1.0 + 2.0**-30]
        values += [rng.uniform(0, 4) * 2.0**rng.randint(-60, 60) for _ in range(40)]
        body = '(void)call;\n'
        for value in values:
            exact = Fraction.from_float(value)
            n = struct.unpack('>I', struct.pack('>f', math.sqrt(value)))[0]
            # Correct rounding: exact root lies between the neighbouring midpoints.
            for _ in range(3):
                low, high = (single(n - 1) + single(n)) / 2, (single(n) + single(n + 1)) / 2
                if low * low > exact: n -= 1
                elif high * high < exact: n += 1
                else: break
            result = single(n)
            inexact, up = result * result != exact, result * result > exact
            expected = (4 << 12) | (0x82020000 if inexact else 0) | (0x40000 if up else 0)
            out = bits(float(result))
            body += f' s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0x{bits(value):016x}),24)==UINT64_C(0x{out:016x}) && s.fpscr==0x{expected:x}u);\n'
        # Original game word fsqrts f31,f13 at 0x1c4a9c.
        self.assertEqual(lift(0xefe0682c, 0x1c4a9c).mnemonic, 'fsqrts')
        body += ' s.fpr[13]=UINT64_C(0x4000000000000000);s.fpscr=0;\n'
        body += '{auto& state=s;' + lift(0xefe0682c, 0x1c4a9c).cpp + '}'
        body += ' assert(s.fpr[31]==UINT64_C(0x3ff6a09e60000000) && s.fpscr==0x82024000u);\n'
        RuntimeObjectsTests().run_native(body)

    def test_fres_and_frsqrte_estimates(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        # Original words: fres f10,f2 at 0x683344; frsqrte f30,f10 at 0x633eb8.
        self.assertEqual(lift(0xed401030, 0x683344).mnemonic, 'fres')
        self.assertEqual(lift(0xffc05034, 0x633eb8).mnemonic, 'frsqrte')
        # FR/FI (0x60000) are undefined for estimates; XX (0x02000000) is unaltered.
        cases = [(False, bits(2.0), bits(0.5), 0x4000), (False, bits(3.0), 0x3fd5555560000000, 0x4000),
                 (False, bits(-4.0), bits(-0.25), 0x8000),
                 (False, 0, 0x7ff0000000000000, 0x84005000), (False, 1 << 63, 0xfff0000000000000, 0x84009000),
                 (False, 0x7ff0000000000000, 0, 0x2000), (False, 0x7ff0000040000000, 0x7ff8000040000000, 0xa1011000),
                 (True, bits(4.0), bits(0.5), 0x4000), (True, bits(0.25), bits(2.0), 0x4000),
                 (True, 0, 0x7ff0000000000000, 0x84005000), (True, 1 << 63, 0xfff0000000000000, 0x84009000),
                 (True, 0x7ff0000000000000, 0, 0x2000), (True, 0x7ff0000040000000, 0x7ff8000040000000, 0xa1011000)]
        body = '(void)call;\n'
        for root, value, out, status in cases:
            r = 'true' if root else 'false'
            body += f' s.fpscr=0;assert(fp_estimate(s,UINT64_C(0x{value:016x}),{r})==UINT64_C(0x{out:016x}) && (s.fpscr&~0x60000u)==0x{status:x}u);\n'
        body += r'''
 s.fpscr=0x02000000;assert(fp_estimate(s,UINT64_C(0x4008000000000000),false)==UINT64_C(0x3fd5555560000000) && (s.fpscr&~0x60000u)==0x02004000u);
 s.fpscr=0;{const auto v=fp_estimate(s,UINT64_C(0x4000000000000000),true);assert(v>=UINT64_C(0x3fe6a09e667f3bcc) && v<=UINT64_C(0x3fe6a09e667f3bce) && (s.fpscr&~0x60000u)==0x4000u);}
 s.fpscr=0;assert(fp_estimate(s,UINT64_C(0xbff0000000000000),true)==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa0011200u);
 s.fpr[2]=UINT64_C(0x4010000000000000);s.fpr[10]=UINT64_C(0x4010000000000000);s.fpscr=0;
'''
        body += '{auto& state=s;' + lift(0xed401030, 0x683344).cpp + lift(0xffc05034, 0x633eb8).cpp + '}'
        body += ' assert(s.fpr[10]==UINT64_C(0x3fd0000000000000) && s.fpr[30]==UINT64_C(0x4000000000000000) && s.fpscr==0x4000u);\n'
        RuntimeObjectsTests().run_native(body)

    def test_mtfsf_fields_and_summaries(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        # Original word mtfsf 0xff,f0 at 0x6e59b8; FLM is instruction bits 7..14.
        self.assertEqual(lift(0xfdfe058e, 0x6e59b8).mnemonic, 'mtfsf')
        x = lambda flm, frb, rc=0: (63 << 26) | (flm << 17) | (frb << 11) | (711 << 1) | rc
        body = '(void)call;\n'
        # Full restore: FEX/VX are recomputed (VXSNAN set -> VX; no enables -> no FEX).
        body += ' s.fpr[0]=UINT64_C(0xffffffff01024000);s.fpscr=0x6000ffffu;{auto& state=s;' + lift(0xfdfe058e, 0x6e59b8).cpp + '}\n'
        body += ' assert(s.fpscr==0x21024000u);\n'
        # Only field 7 (FLM bit 7 = 0x01): rounding/enable nibble; other fields kept.
        body += ' s.fpr[1]=UINT64_C(0x0000000000000003);s.fpscr=0x82004000u;{auto& state=s;' + lift(x(0x01, 1), 0x100).cpp + '}\n'
        body += ' assert(s.fpscr==0x82004003u);\n'
        # Fields 0,1,6,7: ZX (field 1) with ZE (field 6) gives FEX; Rc copies FX,FEX,VX,OX into CR1.
        body += ' s.fpr[2]=UINT64_C(0x84000010);s.fpscr=0;s.cr=0;{auto& state=s;' + lift(x(0xc3, 2, 1), 0x100).cpp + '}\n'
        body += ' assert(s.fpscr==0xc4000010u && ((s.cr>>24)&15)==0xc);\n'
        RuntimeObjectsTests().run_native(body)

    def test_multiply_and_fused_with_infinities(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        inf, ninf, qnan = 0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000
        # FPRF +inf 0x5000, -inf 0x9000; default QNaN class 0x11000;
        # FX 0x80000000, VX 0x20000000, VXIMZ 0x00100000, VXISI 0x00800000.
        products = [(inf, bits(2.0), 53, inf, 0x5000), (ninf, bits(2.0), 53, ninf, 0x9000),
                    (inf, ninf, 53, ninf, 0x9000), (bits(2.0), ninf, 24, ninf, 0x9000),
                    (inf, bits(0.5), 24, inf, 0x5000), (inf, 0, 53, qnan, 0xa0111000),
                    (1 << 63, inf, 24, qnan, 0xa0111000), (0x7ff8000000000123, inf, 53, 0x7ff8000000000123, 0x11000)]
        body = '(void)call;\n'
        for a, b, p, out, status in products:
            body += f' s.fpscr=0;assert(fp_multiply(s,UINT64_C(0x{a:016x}),UINT64_C(0x{b:016x}),{p})==UINT64_C(0x{out:016x}) && s.fpscr==0x{status:x}u);\n'
        fused = [(inf, bits(2.0), bits(1.0), False, inf, 0x5000), (inf, bits(2.0), bits(1.0), True, ninf, 0x9000),
                 (inf, bits(2.0), ninf, False, qnan, 0xa0811000), (inf, bits(2.0), inf, False, inf, 0x5000),
                 (bits(2.0), bits(3.0), ninf, False, ninf, 0x9000), (bits(2.0), bits(3.0), ninf, True, inf, 0x5000),
                 (0, inf, bits(1.0), False, qnan, 0xa0111000), (0, inf, bits(1.0), True, qnan, 0xa0111000)]
        for a, c, b, neg, out, status in fused:
            n = 'true' if neg else 'false'
            body += f' s.fpscr=0;assert(fp_fused(s,UINT64_C(0x{a:016x}),UINT64_C(0x{c:016x}),UINT64_C(0x{b:016x}),53,{n})==UINT64_C(0x{out:016x}) && s.fpscr==0x{status:x}u);\n'
        body += f' s.fpscr=0;assert(fp_fused(s,UINT64_C(0x{inf:016x}),0,UINT64_C(0x{qnan:016x}))==UINT64_C(0x{qnan:016x}) && s.fpscr==0xa0111000u);\n'
        # Original ACIT 0x637098 fmuls f0,f5,f11 after normalizing a zero vector (1/sqrt(0)).
        self.assertEqual(lift(0xec0502f2, 0x637098).mnemonic, 'fmul')
        body += f' s.fpr[5]=0;s.fpr[11]=UINT64_C(0x{inf:016x});s.fpscr=0x86008000u;{{auto& state=s;' + lift(0xec0502f2, 0x637098).cpp + '}fp_materialize(s);\n'
        body += f' assert(s.fpr[0]==UINT64_C(0x{qnan:016x}) && s.fpscr==0xa6111000u);\n'
        RuntimeObjectsTests().run_native(body)

    def test_compare_and_integer_conversion_specials(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        inf, ninf, qnan, snan = 0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000, 0x7ff0000000000001
        one = bits(1.0)
        # CR/FPCC: 8 lt, 4 gt, 2 eq, 1 unordered. FX 0x80000000 VX 0x20000000
        # VXSNAN 0x01000000 VXVC 0x00080000 VXCVI 0x00000100.
        compares = [(qnan, one, False, 1, 0x1000), (snan, one, False, 1, 0xa1001000),
                    (qnan, one, True, 1, 0xa0081000), (one, snan, True, 1, 0xa1081000),
                    (inf, one, False, 4, 0x4000), (ninf, one, False, 8, 0x8000), (inf, inf, False, 2, 0x2000),
                    (ninf, inf, True, 8, 0x8000), (one, inf, False, 8, 0x8000), (ninf, ninf, False, 2, 0x2000)]
        body = '(void)call;\n'
        for a, b, ordered, cr, status in compares:
            o = 'true' if ordered else 'false'
            body += f' s.fpscr=0;s.cr=0;fp_compare(s,7,UINT64_C(0x{a:016x}),UINT64_C(0x{b:016x}),{o});assert((s.cr&15)=={cr} && s.fpscr==0x{status:x}u);\n'
        conversions = [(qnan, 32, 0xffffffff80000000, 0xa0000100), (snan, 64, 0x8000000000000000, 0xa1000100),
                       (inf, 32, 0x7fffffff, 0xa0000100), (ninf, 64, 0x8000000000000000, 0xa0000100),
                       (bits(3e9), 32, 0x7fffffff, 0xa0000100), (bits(-3e9), 32, 0xffffffff80000000, 0xa0000100),
                       (bits(1e19), 64, 0x7fffffffffffffff, 0xa0000100), (bits(2.0**31), 32, 0x7fffffff, 0xa0000100),
                       (bits(-2.0**31), 32, 0xffffffff80000000, 0), (bits(-2.0**63), 64, 0x8000000000000000, 0)]
        for value, width, out, status in conversions:
            body += f' s.fpscr=0;assert(fp_to_integer(s,UINT64_C(0x{value:016x}),{width})==UINT64_C(0x{out:016x}) && s.fpscr==0x{status:x}u);\n'
        # FR/FI cleared, FPRF preserved, existing VXCVI keeps FX unchanged.
        body += f' s.fpscr=0x00064000u;assert(fp_to_integer(s,UINT64_C(0x{qnan:016x}),32)==UINT64_C(0xffffffff80000000) && s.fpscr==0xa0004100u);\n'
        body += f' s.fpscr=0x20000100u;fp_to_integer(s,UINT64_C(0x{inf:016x}),32);assert(s.fpscr==0x20000100u);\n'
        RuntimeObjectsTests().run_native(body)

    def test_indexed_fp_loads_and_stores(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        from anyps3.errors import AnyPS3Error
        # Original game words: lfsx f8,r22,r23 at 0x2d4e4; stfsx f2,r8,r7 at 0x11c8c.
        self.assertEqual(lift(0x7d16bc2e, 0x2d4e4).mnemonic, 'lfsx')
        self.assertEqual(lift(0x7c483d2e, 0x11c8c).mnemonic, 'stfsx')
        x = lambda xo, frt, ra, rb: (31 << 26) | (frt << 21) | (ra << 16) | (rb << 11) | (xo << 1)
        for xo in (567, 631, 695, 759):  # update forms with RA=0 are invalid
            with self.assertRaises(AnyPS3Error):
                lift(x(xo, 1, 0, 4), 0x10000)
        steps = [(x(535, 1, 3, 4), ''), (x(567, 2, 3, 4), ''), (x(599, 3, 0, 5), ''), (x(631, 4, 5, 6), ''),
                 (x(663, 7, 0, 8), ''), (x(695, 1, 9, 6), ''), (x(727, 3, 10, 6), ''), (x(759, 4, 11, 6), '')]
        body = r'''(void)call;
 mem.store(0x10100,4,0x3fc00000u);              // 1.5f
 mem.store(0x10200,8,UINT64_C(0xc002000000000000)); // -2.25
 mem.store(0x10208,8,UINT64_C(0x7ff0000000000001)); // SNaN kept bit-exact by lfd
 r[3]=0x10000;r[4]=0x100;r[5]=0x10200;r[6]=8;r[8]=0x10300;r[9]=0x10400;r[10]=0x10500;r[11]=0x10600;
 s.fpr[7]=UINT64_C(0x3ff6a09e667f3bcd);         // sqrt(2)
'''
        for word, _ in steps:
            body += '{auto& state=s;' + lift(word, 0x10000).cpp + '}\n'
        body += r'''
 assert(s.fpr[1]==UINT64_C(0x3ff8000000000000) && s.fpr[2]==s.fpr[1] && r[3]==0x10100);
 assert(s.fpr[3]==UINT64_C(0xc002000000000000));
 assert(s.fpr[4]==UINT64_C(0x7ff0000000000001) && r[5]==0x10208);
 assert(mem.load(0x10300,1)==0x3f && mem.load(0x10301,1)==0xb5 && mem.load(0x10300,4)==0x3fb504f3u);
 assert(mem.load(0x10408,4)==0x3fc00000u && r[9]==0x10408);
 assert(mem.load(0x10508,1)==0xc0 && mem.load(0x10508,8)==UINT64_C(0xc002000000000000) && r[10]==0x10500);
 assert(mem.load(0x10608,8)==UINT64_C(0x7ff0000000000001) && r[11]==0x10608);
'''
        RuntimeObjectsTests().run_native(body)

    def test_fsqrt_is_correctly_rounded_with_status_and_specials(self):
        import math
        from tests.test_runtime_objects import RuntimeObjectsTests
        rng=random.Random(22)
        values=[2.0,4.0,0.25,3.0,5e-324,2.2250738585072014e-308,1.7976931348623157e308,1e-310,0.1,123456.789]
        values+=[struct.unpack('>d',struct.pack('>Q',rng.getrandbits(62)))[0] for _ in range(40)]
        body='(void)call;\n'
        for value in values:
            exact=Fraction.from_float(value)
            result=math.sqrt(value) # IEEE-754 correctly rounded on the host.
            # Independent exactness check: square of the result versus the input.
            square=Fraction.from_float(result)**2
            inexact=square!=exact
            ulp_up=Fraction.from_float(result)**2>exact
            classification=4 if value else 2
            expected=(classification<<12)|(0x82020000 if inexact else 0)|(0x40000 if inexact and ulp_up else 0)
            body+=f' s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0x{bits(value):016x}))==UINT64_C(0x{bits(result):016x}) && s.fpscr==0x{expected:x}u);\n'
        body+=r'''
 s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0x8000000000000000))==UINT64_C(0x8000000000000000) && s.fpscr==0x12000);
 s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0x7ff0000040000000))==UINT64_C(0x7ff8000040000000) && s.fpscr==0xa1011000);
 s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0xbff0000000000000))==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa0011200u);
 s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0x7ff0000000000000))==UINT64_C(0x7ff0000000000000) && s.fpscr==0x5000);
 s.fpr[1]=UINT64_C(0x4000000000000000);s.fpscr=0x82002000;
'''
        # Lifted FP bodies may defer FR/FI/FPRF (fp_lazy) until FPSCR is observed;
        # anyps3_entry materializes on exit, a standalone body needs it explicitly.
        body+='{auto& state=s;'+lift(0xfc00082c,0x6b2d48).cpp+'}fp_materialize(s);'
        body+=r'''
 assert(s.fpr[0]==UINT64_C(0x3ff6a09e667f3bcd) && s.fpscr==0x82064000);
'''
        RuntimeObjectsTests().run_native(body)

    def test_nan_payload_precedence_signaling_status_and_subtraction_sign(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        body=r'''
 (void)call;
 const auto q=UINT64_C(0xfff8000020000000),sn=UINT64_C(0x7ff0000040000000);
 for(unsigned precision:{24u,53u}){
     for(auto fn:{fp_add,fp_multiply,fp_divide}){
         s.fpscr=0x82060000;
         assert(fn(s,0,q,precision)==q && s.fpscr==0x82011000);
         s.fpscr=0;
         assert(fn(s,q,sn,precision)==q && s.fpscr==0xa1011000);
         s.fpscr=0;
         assert(fn(s,sn,q,precision)==UINT64_C(0x7ff8000040000000) && s.fpscr==0xa1011000);
     }
     s.fpscr=0;
     assert(fp_fused(s,0,sn,q,precision,true)==q && s.fpscr==0xa1011000);
     s.fpscr=0;
     assert(fp_fused(s,sn,q,q,precision)==UINT64_C(0x7ff8000040000000) && s.fpscr==0xa1011000);
 }
 // The exact FSUBS that stopped original startup. NaN FRB keeps its sign;
 // both source registers survive a destination alias and stfs stores NaN.
 s.fpr[13]=0;s.fpr[1]=UINT64_C(0xffffffffe0000000);s.fpscr=0x82002000;
'''
        body+='{auto& state=s;'+lift(0xec0d0828,0x3acfc0).cpp+'}fp_materialize(s);'
        body+=r'''
 assert(s.fpr[0]==UINT64_C(0xffffffffe0000000) && s.fpscr==0x82011000);
 assert(fp_narrow_exact(s.fpr[0])==UINT32_MAX && s.fpr[1]==s.fpr[0]);
 s.fpscr=0;
 assert(fp_add(s,q|1,0,24)==(q|1) && s.fpscr==0x11000);
 assert(fp_fused(s,UINT64_C(0x7ff0000000000000),0,q)==q && s.fpscr==0xa0111000u);
 s.fpscr=0x80;bool caught=false;
 try{fp_add(s,q,0);}catch(const std::runtime_error&){caught=true;}
 assert(caught && s.fpscr==0x80);
'''
        RuntimeObjectsTests().run_native(body)

    def test_results_rounding_and_status_against_independent_rational_reference(self):
        source = compile_program(ElfImage.parse(program(BLR))).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        lines = ['#include <cassert>', 'int main() { PpuState s; std::uint64_t out;']
        count = 0

        def check(expression, expected, expected_flags):
            nonlocal count
            count += 1
            lines.append(f's.fpscr = 0; out = {expression}; if (out != UINT64_C(0x{expected:x}) || s.fpscr != UINT32_C(0x{expected_flags:x})) {{ std::fprintf(stderr,"vector {count}: out=%016" PRIx64 " flags=%08" PRIx32 "\\n",out,s.fpscr); return 1; }}')

        for value in (0,1,-1,79800000,(1<<53)-1,(1<<53)+1,(1<<53)+3,(1<<63)-1,-(1<<63),-(1<<53)-3):
            rounded = float(value)
            check(f'fp_from_integer(s,UINT64_C(0x{value & ((1<<64)-1):x}))',bits(rounded),flags(rounded,Fraction(value)))
        pairs = [(1.0,3.0),(1.0,10.0),(1.0,79800000.0),(-1.0,3.0),(-0.0,2.0),
                 (1.0,-1.0),(1.0,2.0**-100),(1.0,-2.0**-100),(2.0**100,1.0),(-1.0,-2.0**-100)]
        rng = random.Random(20261013)
        for _ in range(150):
            pairs.append((rng.uniform(-2,2)*2.0**rng.randint(-200,200),
                          rng.uniform(-2,2)*2.0**rng.randint(-200,200)))
        for a,b in pairs:
            fa,fb = Fraction.from_float(a),Fraction.from_float(b)
            for fn,value,exact in (('fp_add',a+b,fa+fb),('fp_multiply',a*b,fa*fb),('fp_divide',a/b,fa/fb)):
                check(f'{fn}(s,UINT64_C(0x{bits(a):x}),UINT64_C(0x{bits(b):x}))',bits(value),flags(value,exact))
        # Single precision expectations choose the closest exact rational among
        # neighboring IEEE encodings, avoiding a double-rounding oracle.
        def rounded_single(exact, negative_zero=False):
            if not exact: return -0.0 if negative_zero else 0.0
            raw=struct.unpack('>I',struct.pack('>f',float(abs(exact))))[0]
            candidates=[(struct.unpack('>f',struct.pack('>I',n))[0],n) for n in range(max(0,raw-1),raw+2)]
            value,_=min(candidates,key=lambda item:(abs(Fraction.from_float(item[0])-abs(exact)),item[1]&1))
            return -value if exact<0 else value
        single_pairs=[(1.0,2.0**-24),(1.0,-2.0**-25),(1.0+2.0**-24,2.0**-100),
                      (1.0+2.0**-24,-2.0**-100),(-0.0,2.0)]
        for _ in range(150):
            single_pairs.append((rng.uniform(-2,2)*2.0**rng.randint(-30,30),
                                 rng.uniform(-2,2)*2.0**rng.randint(-30,30)))
        for a,b in single_pairs:
            fa,fb=Fraction.from_float(a),Fraction.from_float(b)
            for fn,exact in (('fp_add',fa+fb),('fp_multiply',fa*fb),('fp_divide',fa/fb)):
                negative_zero=fn!='fp_add' and bool((bits(a)^bits(b))>>63)
                value=rounded_single(exact,negative_zero)
                check(f'{fn}(s,UINT64_C(0x{bits(a):x}),UINT64_C(0x{bits(b):x}),24)',bits(value),flags(value,exact))
        # Exact rational oracle covers gradual underflow, subnormal operands,
        # ties-to-even, rounding across the normal boundary and signed zero.
        for precision,minimum,ulp in ((24,-126,-149),(53,-1022,-1074)):
            tiny_pairs=[(2.0**minimum,0.5),(2.0**ulp,0.5),(2.0**ulp,1.5),
                        (3*2.0**ulp,0.5),(5*2.0**ulp,0.5),
                        (2.0**minimum,1.0-2.0**(-24 if precision==24 else -53)),
                        (2.0**minimum,2.0**(-24 if precision==24 else -52)),
                        (2.0**ulp,3.0),(2.0**minimum,-2.0**minimum),
                        (1.0,2.0**(-1000)),(2.0**(-1000),2.0**(-1000))]
            tiny_pairs += [(rng.randint(1,1<<20)*2.0**ulp,rng.uniform(0.25,4.0)) for _ in range(70)]
            for a,b in tiny_pairs:
                for sign in (1,-1):
                    a=abs(a)*sign
                    fa,fb=Fraction.from_float(a),Fraction.from_float(b)
                    for fn,exact in (('fp_add',fa+fb),('fp_multiply',fa*fb),('fp_divide',fa/fb)):
                        if abs(exact)>Fraction(2)**(127 if precision==24 else 1023):continue
                        negative_zero=fn!='fp_add' and bool((bits(a)^bits(b))>>63)
                        value=rounded_single(exact,negative_zero) if precision==24 else float(exact)
                        if precision==53 and not value and (exact<0 or (not exact and negative_zero)):value=-0.0
                        check(f'{fn}(s,UINT64_C(0x{bits(a):x}),UINT64_C(0x{bits(b):x}),{precision})',bits(value),flags(value,exact,precision))
        fused=[(1.0+2.0**-52,1.0-2.0**-52,-1.0),
               (1.0+2.0**-23,1.0-2.0**-23,-1.0),
               (1.0,1.0,2.0**-53),(1.0,1.0,-2.0**-54),
               (1.0,1.0,2.0**-1000),(1.0,1.0,-2.0**-1000),
               (2.0**500,2.0**500,-2.0**1000),
               (2.0**-500,2.0**-500,1.0),
               (0.0,-2.0,-0.0),(-0.0,-2.0,-0.0)]
        for _ in range(100):
            fused.append(tuple(rng.uniform(-2,2)*2.0**rng.randint(-200,200) for _ in range(3)))
        for a,c,b in fused:
            exact=Fraction.from_float(a)*Fraction.from_float(c)+Fraction.from_float(b)
            negative_zero=bool((bits(a)^bits(c))>>63) and bool(bits(b)>>63)
            rounded=float(exact) if exact else (-0.0 if negative_zero else 0.0)
            for negate in (False,True):
                value=-rounded if negate else rounded
                check(f'fp_fused(s,UINT64_C(0x{bits(a):x}),UINT64_C(0x{bits(c):x}),UINT64_C(0x{bits(b):x}),53,{str(negate).lower()})',
                      bits(value),flags(value,-exact if negate else exact))
            if not exact or 2.0**-126<=abs(exact)<=2.0**127:
                value=rounded_single(exact,negative_zero)
                check(f'fp_fused(s,UINT64_C(0x{bits(a):x}),UINT64_C(0x{bits(c):x}),UINT64_C(0x{bits(b):x}),24)',bits(value),flags(value,exact))
        for opcode in (59,63):
            for variant,expected in ((28,2.0),(29,10.0),(30,-2.0),(31,-10.0)):
                word=(opcode<<26)|(1<<21)|(1<<16)|(2<<11)|(3<<6)|(variant<<1)|1
                lines.append(f'{{auto& state=s;s.fpscr=0;s.fpr[1]=UINT64_C(0x{bits(2.0):x});s.fpr[3]=UINT64_C(0x{bits(3.0):x});s.fpr[2]=UINT64_C(0x{bits(4.0):x});{lift(word,0x10000).cpp}assert(s.fpr[1]==UINT64_C(0x{bits(expected):x}) && s.fpscr=={flags(expected,Fraction(expected))} && ((s.cr>>24)&15)==0);}}')
        # fsel compares both signed zeros as equal, chooses FRB for every NaN,
        # and does not modify FPSCR (including signaling NaN conditions).
        for condition,choose_c in ((0,True),(1<<63,True),(bits(1.0),True),(bits(-1.0),False),
                                   (0x7ff0000000000000,True),(0xfff0000000000000,False),
                                   (0x7ff0000000000001,False),(0xfff8000000000000,False)):
            word=(63<<26)|(3<<21)|(1<<16)|(2<<11)|(4<<6)|(23<<1)|1
            lines.append(f'{{ auto& state=s; s.fpr[1]=UINT64_C(0x{condition:x}); s.fpr[2]=123; s.fpr[4]=456; s.fpscr=0xa0000000; {lift(word,0x10000).cpp} assert(s.fpr[3]=={456 if choose_c else 123} && s.fpscr==0xa0000000 && ((s.cr>>24)&15)==10); }}')
        for value in (0.0,1.5,-1.5,42.0,0.75,-0.75,-float(1<<63),float((1<<63)-1024)):
            integer=int(value)
            check(f'fp_to_integer(s,UINT64_C(0x{bits(value):x}))',integer & ((1<<64)-1),
                  0x82020000 if Fraction.from_float(value)!=integer else 0)
        for value in (0.0,1.5,-1.5,42.0,0.75,-0.75,-2147483648.0,2147483647.9):
            integer=int(value)
            check(f'fp_to_integer(s,UINT64_C(0x{bits(value):x}),32)',integer & ((1<<64)-1),
                  0x82020000 if Fraction.from_float(value)!=integer else 0)
        for raw in (0,0x80000000,1,0x007fffff,0x00800000,0x3f800000,0xc1000000,0x7f800000,0xff800000):
            f=struct.unpack('>f',struct.pack('>I',raw))[0]
            check(f'fp_widen(UINT32_C(0x{raw:x}))',bits(float(f)),0)
        lines += [r'''
    for (std::uint32_t value : {0U,0x80000000U,1U,0x007fffffU,0x00800000U,0x3f800000U,0xc1000000U,0x7f800000U,0xff800000U,0x7fa00100U})
        assert(fp_narrow_exact(fp_widen(value)) == value);
    s.fpscr = 0; fp_compare(s,7,UINT64_C(0xbff0000000000000),0);
    assert((s.cr & 15) == 8 && (s.fpscr & 0xf000) == 0x8000);
    fp_compare(s,7,UINT64_C(0x8000000000000000),0); assert((s.cr & 15) == 2);
    // Inexact is sticky; later exact operation clears FI/FR, not XX/FX.
    s.fpscr = 0; fp_divide(s,UINT64_C(0x3ff0000000000000),UINT64_C(0x4008000000000000));
    fp_from_integer(s,1); assert(s.fpscr == UINT32_C(0x82004000));
    auto fault = [&](auto fn) { bool caught=false; try { fn(); } catch (const std::runtime_error&) { caught=true; } assert(caught); };
    assert(fp_divide(s,UINT64_C(0x3ff0000000000000),0)==UINT64_C(0x7ff0000000000000));
    assert(fp_add(s,1,0)==1); // smallest binary64 subnormal input
    assert(fp_add(s,UINT64_C(0x7ff8000000000000),0)==UINT64_C(0x7ff8000000000000));
    assert(fp_to_integer(s,UINT64_C(0x43e0000000000000))==UINT64_C(0x7fffffffffffffff)); // 2^63 saturates (VXCVI)
    assert(fp_to_integer(s,UINT64_C(0x41e0000000000000),32)==UINT64_C(0x7fffffff) && (s.fpscr & 0x100)); // 2^31
    fault([&]{ fp_narrow_exact(UINT64_C(0x3ff0000000000001)); });
    assert(fp_multiply(s,UINT64_C(0x0010000000000000),UINT64_C(0x0010000000000000))==0);
    s.fpscr=0x80; fault([&]{ fp_from_integer(s,1); });
    return 0; }
''']
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); cpp,binary=root/'fp.cpp',root/'fp'
            cpp.write_text(source+'\n'+'\n'.join(lines))
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(cpp),'-o',str(binary)],
                                 capture_output=True,text=True,timeout=60)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
