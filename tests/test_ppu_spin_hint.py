"""Cell spin hints (or 31,31,31 / or 1,1,1) stay architectural no-ops with exact steps.

The loop counts to 1000; steps are li + 1000*(or, or, addi, cmpwi, blt) + blr.
The generated block that contains the hints issues the host spin-wait hint.
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

CODE = [0x38600000,  # li 3,0
        0x7ffffb78,  # 1: or 31,31,31 (db16cyc)
        0x7c210b78,  # or 1,1,1 (low priority)
        0x38630001,  # addi 3,3,1
        0x2c0303e8,  # cmpwi 3,1000
        0x4180fff0,  # blt 1b
        0x4e800020]  # blr

MAIN = r'''
#include <cassert>
int main(){
    static const std::uint8_t code[]={@CODE@};
    PpuState s;s.memory.map(0x10000,0x1000,5,code,sizeof(code));
    s.pc=0x10000;s.lr=RETURN_SENTINEL;s.r[1]=0x1234;s.r[31]=0x5678;
    anyps3_entry(s,false);
    assert(s.returned && s.r[3]==1000 && s.steps==5002 && s.r[1]==0x1234 && s.r[31]==0x5678 && (s.cr>>28)==2);
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuSpinHint(unittest.TestCase):
    def test_spin_hint_loop(self):
        payload = struct.pack('>' + 'I' * len(CODE), *CODE)
        image = make_image([(0x10000, 5, payload), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True, extra_entries=(0x10000,)).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        page = source.split('void page_10(PpuState& state) {')[1].split('\n}\n')[0]
        self.assertEqual(page.count('ANYPS3_SPIN_HINT();'), 1)  # only the loop block has hints
        source += MAIN.replace('@CODE@', ','.join(str(b) for b in payload))
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
