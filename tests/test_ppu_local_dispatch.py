"""Indirect transfers to a target in the same page use the page's own switch.

Same expectations as the cross-page call loop of test_ppufast_codegen (r3=3000,
r4=1000, 6005 steps), and an indirect jump to an untranslated address in the
same page stops with that PC and the counted failing step.
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

CODE = [0x38600000,  # 0x00 li r3,0
        0x38800000,  # 0x04 li r4,0
        0x7fe802a6,  # 0x08 mflr r31
        0x48000019,  # 0x0c bl 0x24
        0x38840001,  # 0x10 addi r4,r4,1
        0x2c0403e8,  # 0x14 cmpwi r4,1000
        0x4180fff4,  # 0x18 blt 0x0c
        0x7fe803a6,  # 0x1c mtlr r31
        0x4e800020,  # 0x20 blr
        0x38630003,  # 0x24 addi r3,r3,3
        0x4e800020,  # 0x28 blr (same-page return)
        0x60000000,  # 0x2c
        0x60000000,  # 0x30
        0x60000000,  # 0x34
        0x60000000,  # 0x38
        0x60000000,  # 0x3c
        0x7ca903a6,  # 0x40 mtctr r5
        0x4e800420,  # 0x44 bctr
        0x60000000]  # 0x48 never statically reachable

MAIN = r'''
#include <cassert>
int main(){
    static const std::uint8_t code[]={@CODE@};
    {PpuState s;s.memory.map(0x10000,0x1000,5,code,sizeof(code));s.pc=0x10000;s.lr=RETURN_SENTINEL;
     anyps3_entry(s,false);
     assert(s.returned && s.r[3]==3000 && s.r[4]==1000 && s.steps==6005 && s.lr==RETURN_SENTINEL);}
    {PpuState s;s.memory.map(0x10000,0x1000,5,code,sizeof(code));s.pc=0x10040;s.r[5]=0x10048;bool failed=false;
     try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what())=="target has no statically translated block";}
     assert(failed && s.pc==0x10048 && s.steps==3 && s.ctr==0x10048 && !s.returned);}
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuLocalDispatch(unittest.TestCase):
    def test_same_page_indirect_transfers(self):
        payload = struct.pack('>' + 'I' * len(CODE), *CODE)
        image = make_image([(0x10000, 5, payload), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True, extra_entries=(0x10000, 0x10040)).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        self.assertNotIn('case 0x10048:', source)
        self.assertIn('goto ppu_dispatch_local;', source)
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
