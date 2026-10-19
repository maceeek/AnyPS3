"""PPC64 rlw* with wrapping masks and cntlz{w,d}: literal Book I results.

Encodings come from llvm-mc (powerpc64). Expected values are derived by hand:
rlw* rotates the low word, duplicates it into both halves (ROTL32) and ANDs
with MASK(MB+32,ME+32); a wrapping mask (MB>ME) covers the whole upper word.
"""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.program import compile_program
from anyps3.prx import make_image

CODE = [0x54830706,  # rlwinm 3,4,0,28,3
        0x5c853706,  # rlwnm 5,4,6,28,3
        0x50874706,  # rlwimi 7,4,8,28,3
        0x548c221e,  # rlwinm 12,4,4,8,15 (non-wrapping)
        0x7d280034,  # cntlzw 8,9
        0x7d6a0074,  # cntlzd 10,11
        0x7dcd0035,  # cntlzw. 13,14
        0x7e0f0074,  # cntlzd 15,16
        0x7e510034,  # cntlzw 17,18
        0x4e800020]  # blr

MAIN = r'''
#include <cassert>
int main(){
    static const std::uint8_t code[]={@CODE@};
    PpuState s;s.memory.map(0x10000,0x1000,5,code,sizeof(code));
    s.pc=0x10000;s.lr=RETURN_SENTINEL;
    s.r[4]=UINT64_C(0xdeadbeef12345678);   // the upper word of RS never participates
    s.r[6]=36;                             // rlwnm uses (rB & 31) = 4
    s.r[7]=UINT64_C(0x1111111111111111);
    s.r[9]=UINT64_C(0xffffffff00010000);   // cntlzw counts the low word: 15
    s.r[11]=UINT64_C(0x0000000000010000);  // 47
    s.r[14]=UINT64_C(0xffffffff00000000);  // low word zero: 32, CR0 = GT
    s.r[16]=0;                             // 64
    s.r[18]=UINT64_C(0x80000000);          // 0
    anyps3_entry(s,false);
    assert(s.returned);
    // rotl32(0x12345678,0) duplicated & 0xfffffffff000000f
    assert(s.r[3]==UINT64_C(0x1234567810000008));
    // rotl32(.,4)=0x23456781 duplicated & 0xfffffffff000000f
    assert(s.r[5]==UINT64_C(0x2345678120000001));
    // rotl32(.,8)=0x34567812: (0x1111..&0x0ffffff0)|(dup&0xfffffffff000000f)
    assert(s.r[7]==UINT64_C(0x3456781231111112));
    // non-wrapping mask 0x00ff0000 keeps the upper word zero
    assert(s.r[12]==UINT64_C(0x0000000000450000));
    assert(s.r[8]==15 && s.r[10]==47 && s.r[13]==32 && s.r[15]==64 && s.r[17]==0);
    assert((s.cr>>28)==4);
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuRotateCount(unittest.TestCase):
    def test_wrapping_rotate_masks_and_count_leading_zeros(self):
        payload = struct.pack('>' + 'I' * len(CODE), *CODE)
        image = make_image([(0x10000, 5, payload), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True, extra_entries=(0x10000,)).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source += MAIN.replace("@CODE@", ','.join(str(b) for b in payload))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
                                    str(root/'test.cpp'), '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
            self.assertEqual(run.returncode, 0, run.stderr[-4000:])
            self.assertIn('ok', run.stdout)
