"""SPU ISA v1.2 fixed hexadecimal vectors, executed as native lifted instructions.

Expected constants follow PDF195-201,202-234; no host floating point oracle.
"""
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
import unittest
from anyps3.spu import CORE_CPP, lift
from anyps3.errors import AnyPS3Error
from tests.test_spu import instruction
from tests import test_runtime_objects


def rr(op, target=3, a=1, b=2):
    return (op << 21) | (b << 14) | (a << 7) | target


def rrr(op, target=4, a=1, b=2, c=3):
    return (op << 28) | (target << 21) | (b << 14) | (a << 7) | c


def words(reg, values):
    return ''.join(f'spu_word(t.gpr[{reg}],{i},UINT32_C(0x{x:08x}));' for i, x in enumerate(values))


def doubles(reg, values):
    # Write bytes through the established word helper, independently of new double helpers.
    return words(reg, [part for x in values for part in (x >> 32, x & 0xffffffff)])


def expect(reg, values):
    return ''.join(f'assert(spu_word(t.gpr[{reg}],{i})==UINT32_C(0x{x:08x}));' for i, x in enumerate(values))


def expect_double(reg, values):
    return expect(reg, [part for x in values for part in (x >> 32, x & 0xffffffff)])


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuFloatTests(unittest.TestCase):
    def run_native(self, body):
        # Compile the actual production helpers and lift output in isolation;
        # the full regression suite also compiles these into the complete runtime.
        helpers = CORE_CPP.split('static void spu_collect_events(', 1)[0]
        source = r"""
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <cassert>
using SpuRegister=std::array<std::uint8_t,16>;
inline const bool spu_stats_on=false; // defined by the runtime types (spu_runtime.py) in a real program
""" + helpers + r"""
template<class F> void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){struct {std::array<SpuRegister,128> gpr{};SpuRegister fpscr{};unsigned pc=0x100;} t;
""" + body + '\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=undefined', '-fno-sanitize-recover=undefined',
                                    str(path / 'test.cpp'), '-o', str(path / 'test')],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(path / 'test')], capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_single_truncation_extended_range_flags_and_aliases(self):
        body = ''
        # 1+3*2^-25 truncates to 1 (nearest would give next float).
        # IEEE Inf bit pattern is a finite 2^128 on SPU.
        for target in (1, 2, 3):
            body += 't.fpscr={};'
            body += words(1, (0x3f800000, 0x7f800000, 0x00000001, 0x80000000))
            body += words(2, (0x33c00000, 0x7f800000, 0x00800000, 0x80000000))
            body += instruction(rr(0x2c4, target))
            body += expect(target, (0x3f800000, 0x7fffffff, 0x00800000, 0))
            body += 'assert(spu_word(t.fpscr,0)==0 && spu_word(t.fpscr,1)==5 && spu_word(t.fpscr,2)==1 && spu_word(t.fpscr,3)==0);'
        # Denormal inputs ignored; exact nonzero subnormal result flushes to +zero.
        body += 't.fpscr={};'
        body += words(1, (0x80800000, 0x7fffffff, 0xffffffff, 0xbf800000))
        body += words(2, (0x3f000000, 0x3f800001, 0x40000000, 0x00000001))
        body += instruction(rr(0x2c6))
        body += expect(3, (0, 0x7fffffff, 0xffffffff, 0))
        body += 'assert(spu_word(t.fpscr,0)==3 && spu_word(t.fpscr,1)==5 && spu_word(t.fpscr,2)==5 && spu_word(t.fpscr,3)==1);'
        body += words(1, (0xbf800000, 0, 0x7f800000, 0x00800001))
        body += words(2, (0xbf800000, 0x80000000, 0x7f000000, 0x00800000))
        body += instruction(rr(0x2c5))
        body += expect(3, (0, 0, 0x7f000000, 0))
        self.run_native(body)

    def test_fused_single_one_rounding_unbounded_product_and_aliases(self):
        body = ''
        # (1+2^-23)*(1-2^-23)-1 = -2^-46, lost with intermediate rounding.
        # 2^128*2 - 2^128 = 2^128, with no intermediate overflow exception.
        for target in (1, 2, 3, 4):
            body += 't.fpscr={};'
            body += words(1, (0x3f800001, 0x7f800000, 0x00800000, 0x80000000))
            body += words(2, (0x3f7ffffe, 0x40000000, 0x3f000000, 0x3f800000))
            body += words(3, (0xbf800000, 0xff800000, 0x00800000, 0x80000000))
            body += instruction(rrr(0xe, target))
            body += expect(target, (0xa8800000, 0x7f800000, 0x00c00000, 0))
            body += 'assert(spu_word(t.fpscr,1)==1 && spu_word(t.fpscr,2)==0);'
        for opcode, result in ((0xf, 0xa8800000), (0xd, 0x28800000)):
            body += words(1, (0x3f800001,) * 4) + words(2, (0x3f7ffffe,) * 4) + words(3, (0x3f800000,) * 4)
            body += instruction(rrr(opcode)) + expect(4, (result,) * 4)
        self.run_native(body)

    def test_single_comparisons_zero_denormal_sign_and_magnitude(self):
        body = ''
        for op, expected in ((0x2c2, (0, 0, 0, 0xffffffff)), (0x2ca, (0, 0xffffffff, 0, 0xffffffff)),
                             (0x3c2, (0xffffffff, 0, 0, 0)), (0x3ca, (0xffffffff, 0, 0xffffffff, 0))):
            for target in (1, 2, 3):
                body += words(1, (0x80000001, 0xc0000000, 0xbf800000, 0x7fffffff))
                body += words(2, (0x00000001, 0x3f800000, 0x3f800000, 0x7f800000))
                body += instruction(rr(op, target)) + expect(target, expected)
        body += 'assert(t.fpscr==SpuRegister{});'
        self.run_native(body)

    def test_scaled_conversions_saturation_truncation_and_undefined_scale(self):
        body = ''
        for target in (1, 3):
            for op, result in ((0x1d8, (0x7fffffff, 0x80000000, 0x00000001, 0xffffffff)),
                               (0x1d9, (0x80000000, 0, 1, 0))):
                body += words(1, (0x4f000000, 0xcf000000, 0x3fffffff, 0xbfffffff))
                body += instruction((op << 22) | (173 << 14) | (1 << 7) | target) + expect(target, result)
            for op, result in ((0x1da, (0x4effffff, 0xcf000000, 0xbf800000, 0x4b800000)),
                               (0x1db, (0x4effffff, 0x4f000000, 0x4f7fffff, 0x4b800000))):
                body += words(1, (0x7fffffff, 0x80000000, 0xffffffff, 0x01000001))
                body += instruction((op << 22) | (155 << 14) | (1 << 7) | target) + expect(target, result)
        body += words(1, (1, 0xffffffff, 0x80000000, 0))
        body += instruction((0x1db << 22) | (28 << 14) | (1 << 7) | 3)
        body += expect(3, (0, 0x0fffffff, 0x0f800000, 0))
        body += words(1, (0x00800000, 0x80800000, 0x7fffffff, 1))
        body += instruction((0x1d8 << 22) | (46 << 14) | (1 << 7) | 3)
        body += expect(3, (2, 0xfffffffe, 0x7fffffff, 0))
        self.run_native(body)
        for op, imm in ((0x1d8, 45), (0x1d9, 174), (0x1da, 27), (0x1db, 156)):
            with self.assertRaisesRegex(AnyPS3Error, 'scale outside'):
                lift(0x100, (op << 22) | (imm << 14))

    def test_fi_exact_fraction_fields_sign_and_aliases(self):
        body = ''
        # Y=1/2, base=1, step=1/16: 1-1/32 = 31/32.
        # Y=(2^19-1)/2^19, step=1/8192; truncation retains final tiny term.
        for target in (1, 2, 3):
            body += words(1, (0x00040000, 0x00040000, 0x0007ffff, 0))
            body += words(2, (0x3f800200, 0xbf800200, 0x3f800001, 0x40000400))
            body += instruction(rr(0x3d4, target)) + expect(target, (0x3f780000, 0xbf780000, 0x3f7ff800, 0x40000400))
        self.run_native(body)

    def test_double_rounding_modes_flags_and_signed_zeros(self):
        body = ''
        # 1 + 3*2^-54: nearest/up give next double, zero/down give 1.
        for mode, expected in ((0, 0x3ff0000000000001), (1, 0x3ff0000000000000),
                               (2, 0x3ff0000000000001), (3, 0x3ff0000000000000)):
            body += 't.fpscr={};' + f'spu_word(t.fpscr,0,{mode << 10});'
            body += doubles(1, (0x3ff0000000000000, 0xbff0000000000000))
            body += doubles(2, (0x3ca8000000000000, 0xbca8000000000000))
            body += instruction(rr(0x2cc, 1)) + expect_double(1, (expected, 0xbff0000000000001))
            body += 'assert(spu_word(t.fpscr,1)==0x800 && spu_word(t.fpscr,2)==0x800);'
        body += 't.fpscr={};spu_word(t.fpscr,0,3u<<10);'
        body += doubles(1, (0x3ff0000000000000, 0x8000000000000000))
        body += doubles(2, (0x3ff0000000000000, 0))
        body += instruction(rr(0x2cd)) + expect_double(3, (0x8000000000000000, 0x8000000000000000))
        body += 't.fpscr={};'
        body += doubles(1, (0x8000000000000000, 0x0010000000000000))
        body += doubles(2, (0x3ff0000000000000, 0x3fe0000000000000))
        body += instruction(rr(0x2ce)) + expect_double(3, (0x8000000000000000, 0x0008000000000000))
        body += 'assert(t.fpscr==SpuRegister{});'
        self.run_native(body)

    def test_double_overflow_underflow_nans_and_denormal_operands(self):
        body = 't.fpscr={};'
        body += doubles(1, (0x7fefffffffffffff, 0x0000000000000001))
        body += doubles(2, (0x4000000000000000, 0x3fe0000000000000))
        body += instruction(rr(0x2ce)) + expect_double(3, (0x7ff0000000000000, 0))
        body += 'assert(spu_word(t.fpscr,1)==0x2800 && spu_word(t.fpscr,2)==0x1800);'
        body += doubles(1, (0x7ff0000000000000, 0x7ff0000000000001))
        body += doubles(2, (0, 0x3ff0000000000000))
        body += instruction(rr(0x2ce, 2)) + expect_double(2, (0x7ff8000000000000, 0x7ff8000000000000))
        body += 'assert(spu_word(t.fpscr,1)==0x2c00 && spu_word(t.fpscr,2)==0x1e00);'
        body += 't.fpscr={};spu_word(t.fpscr,0,1u<<10);'
        body += doubles(1, (0x7fefffffffffffff, 0x0000000000000001))
        body += doubles(2, (0x4000000000000000, 0x0000000000000001))
        body += instruction(rr(0x2cc)) + expect_double(3, (0x7fefffffffffffff, 0x0000000000000002))
        self.run_native(body)

    def test_double_fused_one_rounding_aliases_and_post_round_negation(self):
        body = ''
        for target, expected in ((1, 0x4020000000000000), (2, 0x4022000000000000), (3, 0x4026000000000000)):
            body += doubles(1, (0x4000000000000000,) * 2)
            body += doubles(2, (0x4008000000000000,) * 2)
            body += doubles(3, (0x4014000000000000,) * 2)
            body += instruction(rr(0x35c, target)) + expect_double(target, (expected,) * 2)
        # (1+2^-52)*(1-2^-52)-1 = -2^-104.
        body += doubles(1, (0x3ff0000000000001,) * 2)
        body += doubles(2, (0x3feffffffffffffe,) * 2)
        body += doubles(3, (0xbff0000000000000,) * 2)
        body += instruction(rr(0x35c)) + expect_double(3, (0xb970000000000000,) * 2)
        for op, expected in ((0x35d, 0xb970000000000000), (0x35e, 0x3970000000000000)):
            body += doubles(1, (0x3ff0000000000001,) * 2) + doubles(2, (0x3feffffffffffffe,) * 2) + doubles(3, (0x3ff0000000000000,) * 2)
            body += instruction(rr(op)) + expect_double(3, (expected,) * 2)
        # RN+∞ rounds +1+epsilon up before dfnma negates; direct rounding of
        # the negative exact sum would give a different answer.
        body += 't.fpscr={};spu_word(t.fpscr,0,2u<<10);'
        body += doubles(1, (0x3ff0000000000000, 0x7ff8000000000001))
        body += doubles(2, (0x3ff0000000000000, 0x3ff0000000000000))
        body += doubles(3, (0x3c90000000000000, 0))
        body += instruction(rr(0x35f)) + expect_double(3, (0xbff0000000000001, 0x7ff8000000000000))
        self.run_native(body)

    def test_ieee_conversions_rounding_lane_layout_and_specials(self):
        body = 't.fpscr={};spu_word(t.fpscr,0,2u<<8);'
        body += doubles(1, (0x3ff0000018000000, 0x3ff0000010000000))
        body += instruction(rr(0x3b9, 1)) + expect(1, (0x3f800001, 0, 0x3f800001, 0))
        body += 'assert(spu_word(t.fpscr,1)==0x800 && spu_word(t.fpscr,2)==0x800);'
        body += words(1, (0x00000001, 0xdeadbeef, 0x80000000, 0xdeadbeef))
        body += instruction(rr(0x3b8, 1)) + expect_double(1, (0x36a0000000000000, 0x8000000000000000))
        body += words(1, (0x7f800000, 0, 0xff800001, 0))
        body += instruction(rr(0x3b8)) + expect_double(3, (0x7ff0000000000000, 0x7ff8000000000000))
        body += 'assert(spu_word(t.fpscr,2)==0xe00);'
        body += 't.fpscr={};'
        body += doubles(1, (0x3690000000000000, 0x47f0000000000000))
        body += instruction(rr(0x3b9)) + expect(3, (0, 0, 0x7f800000, 0))
        body += 'assert(spu_word(t.fpscr,1)==0x1800 && spu_word(t.fpscr,2)==0x2800);'
        self.run_native(body)

    def test_double_compare_and_test_special_masks(self):
        body = ''
        for op, result in ((0x2c3, (0, 0)), (0x2cb, (0xffffffffffffffff, 0)),
                           (0x3c3, (0, 0)), (0x3cb, (0, 0))):
            body += 't.fpscr={};'
            body += doubles(1, (0xc000000000000000, 0x7ff8000000000000))
            body += doubles(2, (0x3ff0000000000000, 0x7ff8000000000000))
            body += instruction(rr(op, 1)) + expect_double(1, result)
            body += 'assert(spu_word(t.fpscr,2)==0x400);'
        for value, mask in ((0x7ff8000000000001, 64), (0x7ff0000000000000, 32),
                            (0xfff0000000000000, 16), (0, 8), (0x8000000000000000, 4),
                            (1, 2), (0x8000000000000001, 1)):
            body += doubles(1, (value, 0x3ff0000000000000))
            body += instruction(rr(0x3bf, 1, b=mask)) + expect_double(1, (0xffffffffffffffff, 0))
        self.run_native(body)

    def test_integer_borrow_carry_logic_byte_arithmetic_and_mpya(self):
        body = ''
        vectors = ((0x42, (0, 1, 0, 1)), (0x341, (1, 0, 0xffffffff, 1)),
                   (0x342, (1, 1, 0, 0)), (0x343, (0, 1, 0, 1)),
                   (0xc9, (0xffffffff, 0, 0xffffffff, 0xffffffff)),
                   (0x249, (0, 0xffffffff, 0xfffffffe, 0xfffffffe)),
                   (0x2c9, (0xffffffff, 0xffffffff, 0xffffffff, 0xfffffffe)))
        for op, result in vectors:
            body += words(1, (0xffffffff, 0xffffffff, 1, 0)) + words(2, (0, 0xffffffff, 0, 1)) + words(3, (1, 1, 1, 1))
            body += instruction(rr(op)) + expect(3, result)
        body += words(1, (0xff00017f,) * 4) + words(2, (0x00ff0080,) * 4)
        body += instruction(rr(0x53, 1)) + expect(1, (0xffff0101,) * 4)
        body += words(1, (0xff00017f,) * 4)
        body += instruction(rr(0xd3, 2)) + expect(2, (0x80800180,) * 4)
        body += words(1, (0xffffffff,) * 4) + words(2, (0x00008000,) * 4) + words(3, (0xffffffff,) * 4)
        body += instruction(rrr(0xc, 1)) + expect(1, (0x00007fff,) * 4)
        self.run_native(body)

    def test_original_260_fcgt_stop_and_fpscr_write_read_masks(self):
        # stderr.log: SPU200 PC3b8c, original word58424618 (fcgt r24,r12,r9).
        body = words(12, (0xbc090640, 0x3f8d98a4, 0xbd588330, 0x3f800000))
        body += words(9, (0xc5dac0cc,) * 4)
        body += instruction(0x58424618) + expect(24, (0xffffffff,) * 4)
        body += words(1, (0xffffffff,) * 4)
        body += instruction(rr(0x3ba, 1)) + instruction(rr(0x398))
        body += expect(1, (0xffffffff,) * 4)  # fscrwr false target is untouched.
        body += expect(3, (0x00000f07, 0x00003f07, 0x00003f07, 0x00000f07))
        body += words(1, (0,) * 4) + instruction(rr(0x3ba)) + instruction(rr(0x398))
        body += expect(3, (0,) * 4)
        self.run_native(body)

    def test_exact_range_boundaries_ties_and_integer_carry_aliases(self):
        body = 't.fpscr={};'
        # Smax + a tiny positive value exceeds Smax BEFORE truncation;
        # Smax - that value truncates one ulp below Smax, without overflow.
        body += words(1, (0x7fffffff, 0x7fffffff, 0x00800000, 0xffffffff))
        body += words(2, (0x00800000, 0x80800000, 0x80800000, 0x80800000))
        body += instruction(rr(0x2c4)) + expect(3, (0x7fffffff, 0x7ffffffe, 0, 0xffffffff))
        body += 'assert(spu_word(t.fpscr,0)==5 && spu_word(t.fpscr,1)==1 && spu_word(t.fpscr,2)==0 && spu_word(t.fpscr,3)==5);'
        # Halfway ties: even rounds down for mantissa0, up for mantissa1.
        body += 't.fpscr={};'
        body += doubles(1, (0x3ff0000000000000, 0x3ff0000000000001))
        body += doubles(2, (0x3ca0000000000000,) * 2)
        body += instruction(rr(0x2cc)) + expect_double(3, (0x3ff0000000000000, 0x3ff0000000000002))
        # Tiny before rounding, inexact: rounds UP to the minimum normal single.
        body += 't.fpscr={};'
        body += doubles(1, (0x380fffffe0000000, 0xb80fffffe0000000))
        body += instruction(rr(0x3b9)) + expect(3, (0x00800000, 0, 0x80800000, 0))
        body += 'assert(spu_word(t.fpscr,1)==0x1800 && spu_word(t.fpscr,2)==0x1800);'
        # Infinity-minus-infinity is invalid; ordinary quiet NaN sets NAN only.
        body += 't.fpscr={};'
        body += doubles(1, (0x7ff0000000000000, 0x7ff8000000000001))
        body += doubles(2, (0x7ff0000000000000, 0))
        body += instruction(rr(0x2cd)) + expect_double(3, (0x7ff8000000000000,) * 2)
        body += 'assert(spu_word(t.fpscr,1)==0x400 && spu_word(t.fpscr,2)==0x200);'
        for target in (1, 2, 3):
            for op, expected in ((0x341, (0, 1, 1, 0)), (0x342, (0, 0, 1, 0)), (0x343, (1, 1, 0, 1))):
                body += words(1, (0, 0, 0xffffffff, 1))
                body += words(2, (1, 1, 1, 1))
                body += words(3, (0, 1, 0, 1))
                # Target's bit0 is also an input, hence alias-dependent vectors.
                if op == 0x341 and target == 1:
                    expected = (0, 0, 2, 0)
                elif op == 0x341 and target == 2:
                    expected = (1, 1, 2, 0)
                body += instruction(rr(op, target)) + expect(target, expected)
        self.run_native(body)

    def test_conditional_halts_and_explicit_unimplemented_services(self):
        body = words(1, (1, 0, 0, 0)) + words(2, (2, 0, 0, 0))
        body += instruction(rr(0x258)) + instruction(rr(0x2d8)) + instruction(rr(0x3d8))
        body += words(1, (0xffffffff, 0, 0, 0))
        body += 'fault([&]{' + instruction(rr(0x2d8)) + '});'
        body += words(1, (2, 0, 0, 0))
        body += 'fault([&]{' + instruction(rr(0x3d8)) + '});'
        self.run_native(body)
        for op in (0, 0x140):
            # Valid instructions; the runtime rejects unknown LV2 services.
            self.assertIn('spu_stop(s,t,', lift(0x100, rr(op)))


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuEstimateAndChannelCountTests(unittest.TestCase):
    def run_native(self, body):
        test_runtime_objects.RuntimeObjectsTests().run_native('(void)call;NativeSpu::Thread t;' + body)

    def test_frest_frsqest_with_fi_reach_twelve_bit_accuracy(self):
        # Independent property (ISA p.215/219 programming note): fi(x, frest(x)) estimates 1/x and
        # fi(x, frsqest(x)) estimates 1/sqrt(|x|); checked against host double math, not the tables.
        values = (1.0, 1.5, 1.999, 3.0, 0.1, 7.25, 1234.5, 3.0e-20, 6.0e18, -2.5, -0.37)
        body = 'auto f=[](std::uint32_t v){float x;std::memcpy(&x,&v,4);return double(x);};'
        for value in values:
            bits = struct.unpack('>I', struct.pack('>f', value))[0]
            body += words(1, (bits,) * 4)
            body += instruction(rr(0x1b8, 2, 1, 0)) + instruction(rr(0x3d4, 3, 1, 2))
            body += instruction(rr(0x1b9, 4, 1, 0)) + instruction(rr(0x3d4, 5, 1, 4))
            body += (f'{{const double x=f(UINT32_C(0x{bits:08x}));'
                     'for(unsigned i=0;i<4;++i){const double r=f(spu_word(t.gpr[3],i)),q=f(spu_word(t.gpr[5],i));'
                     'assert(std::fabs(r*x-1.0)<1.0/4096);assert(std::fabs(q*q*std::fabs(x)-1.0)<1.0/2048);'
                     'assert((spu_word(t.gpr[2],i)>>31)==(spu_word(t.gpr[1],i)>>31) && !(spu_word(t.gpr[4],i)>>31));}}')
        self.run_native(body)

    def test_rchcnt_reports_modeled_channel_state_and_stops_on_unmodeled(self):
        def rchcnt(channel, target=3):
            return instruction((0xf << 21) | (channel << 7) | target)
        body = words(3, (0xdead, 0xbeef, 0xcafe, 0xf00d))
        body += rchcnt(29) + expect(3, (0, 0, 0, 0))            # inbound mailbox empty, slots 1..3 zeroed
        body += 't.in_mailbox={1,2,3};' + rchcnt(29) + expect(3, (3, 0, 0, 0))
        body += rchcnt(28) + expect(3, (1, 0, 0, 0)) + 't.out_mailbox_full=true;' + rchcnt(28) + expect(3, (0, 0, 0, 0))
        body += rchcnt(30) + expect(3, (1, 0, 0, 0)) + rchcnt(21) + expect(3, (16, 0, 0, 0))
        body += rchcnt(24) + expect(3, (0, 0, 0, 0)) + 't.tag_status_ready=true;' + rchcnt(24) + expect(3, (1, 0, 0, 0))
        body += rchcnt(27) + expect(3, (0, 0, 0, 0)) + rchcnt(3) + expect(3, (0, 0, 0, 0))
        body += 't.signal_ready[1]=true;' + rchcnt(4) + expect(3, (1, 0, 0, 0))
        body += rchcnt(0) + expect(3, (0, 0, 0, 0)) + 't.event_ready=true;' + rchcnt(0) + expect(3, (1, 0, 0, 0))
        body += 'assert(t.in_mailbox.size()==3 && t.out_mailbox_full && t.tag_status_ready && t.event_ready);'
        for channel in (7, 8, 9, 14, 15, 26):
            body += rchcnt(channel) + expect(3, (1, 0, 0, 0))
        for channel in (5, 6, 10, 31, 127):
            body += rchcnt(channel) + expect(3, (0, 0, 0, 0))
        # MFC_RdListStallStat (25) is modeled: no stalled list -> count 0 (stall-and-notify support).
        body += rchcnt(25) + expect(3, (0, 0, 0, 0))
        self.run_native(body)
