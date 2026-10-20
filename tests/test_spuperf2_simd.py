"""Independent BE SIMD vectors, tested with and without host ISA intrinsics."""
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.spu import CORE_CPP, lift


def reg(r, data):
    return f't.gpr[{r}]=SpuRegister{{'+','.join(map(str, data))+'};'


def check(r, data):
    return f'assert((t.gpr[{r}]==SpuRegister{{'+','.join(map(str, data))+'}));'


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPerf2SimdTests(unittest.TestCase):
    def native(self, body):
        source = '''
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <cassert>
using SpuRegister=std::array<std::uint8_t,16>;
inline const bool spu_stats_on=false; // defined by the runtime types (spu_runtime.py) in a real program
''' + CORE_CPP.split('static void spu_collect_events(', 1)[0] + '''
int main(){struct {std::array<SpuRegister,128> gpr{};SpuRegister fpscr{};unsigned pc=0x100;}t;
auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};
''' + body + '\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cpp').write_text(source)
            for native in (False, True):
                flags = ['-march=native'] if native else []
                command = ['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                           '-fsanitize=undefined', '-fno-sanitize-recover=undefined', *flags,
                           str(path / 'test.cpp'), '-o', str(path / 'test')]
                build = subprocess.run(command, capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, build.stderr)
                run = subprocess.run([str(path / 'test')], capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_word_halfword_arithmetic_logic_compare_and_aliases(self):
        a = bytes.fromhex('ffffffff800000007fffffff00008000')
        b = bytes.fromhex('00000001000000018000000000008000')
        body = ''
        for op in (0xc0, 0x40, 0xc1, 0x41, 0x241, 0x49, 0x2c1, 0x3c0, 0x240, 0x2c0,
                   0xc8, 0x48, 0x3c8, 0x248, 0x2c8):
            width = 2 if op in (0xc8, 0x48, 0x3c8, 0x248, 0x2c8) else 4
            expected = bytearray()
            for offset in range(0, 16, width):
                left, right = (int.from_bytes(v[offset:offset+width], 'big') for v in (a, b))
                if op in (0xc0, 0xc8): value = left + right
                elif op in (0x40, 0x48): value = right - left
                elif op == 0xc1: value = left & right
                elif op == 0x41: value = left | right
                elif op == 0x241: value = left ^ right
                elif op == 0x49: value = ~(left | right)
                elif op == 0x2c1: value = left & ~right
                elif op in (0x3c0, 0x3c8): value = -int(left == right)
                elif op in (0x240, 0x248):
                    left, right = (int.from_bytes(v[offset:offset+width], 'big', signed=True) for v in (a, b))
                    value = -int(left > right)
                else: value = -int(left > right)
                expected += (value % (1 << (8*width))).to_bytes(width, 'big')
            for target in (1, 2, 3):
                body += reg(1, a) + reg(2, b) + lift(0x100, op << 21 | 2 << 14 | 1 << 7 | target) + check(target, expected)
        for imm in (-512, -1, 0, 1, 511):
            expected = b''.join(((x + imm) % (1 << 32)).to_bytes(4, 'big') for x in struct.unpack('>4I', a))
            body += reg(1, a) + lift(0x100, 0x1c << 24 | (imm & 1023) << 14 | 1 << 7 | 1) + check(1, expected)
        self.native(body)

    def test_all_byte_rotations_and_shifts_boundaries_and_aliases(self):
        data = bytes(range(16, 32))
        body = ''
        for op in (0x1fc, 0x1dc, 0x1cc, 0x1ff, 0x1cf, 0x1df, 0x1fd, 0x1cd, 0x1dd):
            for n in range(32):
                rotating = op in (0x1fc, 0x1dc, 0x1cc)
                left = op in (0x1ff, 0x1cf, 0x1df)
                expected = (data[n % 16:] + data[:n % 16] if rotating else
                            (data[n:] + bytes(min(n, 16)) if left else bytes(min(n, 16)) + data[:max(0, 16-n)]))
                count = n if rotating or left else (-n) & 127
                operand = count << 3 if op in (0x1cc, 0x1cf, 0x1cd) else count
                for target in (1, 2, 3):
                    body += reg(1, data) + reg(2, operand.to_bytes(4, 'big') + bytes(12))
                    encoded = op << 21 | (count if op in (0x1fc, 0x1ff, 0x1fd) else 2) << 14 | 1 << 7 | target
                    body += lift(0x100, encoded) + check(target, expected)
        self.native(body)

    def test_shuffle_all_256_controls_and_all_four_aliases(self):
        expected = (list(range(16)) + list(range(64, 80))) * 4 + [0] * 64 + [255] * 32 + [128] * 32
        body = ''
        for base in range(0, 256, 16):
            for target in (1, 2, 3, 4):
                body += reg(1, range(16)) + reg(2, range(64, 80)) + reg(3, range(base, base+16))
                body += lift(0x100, 0xb << 28 | target << 21 | 2 << 14 | 1 << 7 | 3)
                body += check(target, expected[base:base+16])
        self.native(body)


if __name__ == '__main__':
    unittest.main()
