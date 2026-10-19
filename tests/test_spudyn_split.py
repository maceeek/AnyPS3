"""Out-of-line interpreter links once and reenters independently split AOT code."""
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.native_build import compile_units, unit_sources, write_units
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_spu_block import image


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class DynamicSplitTests(unittest.TestCase):
    def test_split_link_unknown_word_page_and_return_to_static_overlay(self):
        img=image({0x100:0x40800383,0x104:1})  # IL r3,7; STOP 1
        translated=replace(compile_program(ElfImage.parse(program(BLR)),process_entry=True),
                           spu_images=(img,),spu_overlays=(((0x1000,bytes.fromhex('40800504')),),))
        units=translated.cpp_units(units=2)
        self.assertEqual(sum('void spu_interpret(PpuState& s' in text for text in units.values()),1)
        self.assertFalse(any('switch(word>>21)' in text for name,text in units.items() if name.startswith('spu-')))
        body=r'''
#include <cassert>
int main(){
 PpuState s;NativeSpu::Thread t;t.entry=0x100;std::vector<std::uint8_t> ls(0x40000);
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-i*8);};
 put(0x100,0x40800383);put(0x104,1);spu_prepare(s,t,ls.data());
 spu_step(s,t,ls.data());assert(t.steps==1 && t.pc==0x104 && spu_word(t.gpr[3],0)==7);
 put(0x100,0x40801505);t.pc=0x100;spu_step(s,t,ls.data());
 assert(t.steps==2 && t.pc==0x104 && spu_word(t.gpr[5],0)==42);
 put(0x20000,0x40801606);t.pc=0x20000;spu_step(s,t,ls.data());
 assert(t.steps==3 && t.pc==0x20004 && spu_word(t.gpr[6],0)==44);
 put(0x1000,0x40800504);t.pc=0x1000;spu_step(s,t,ls.data());
 assert(t.steps==4 && t.pc==0x1004 && spu_word(t.gpr[4],0)==10);
 put(0x100,0x40800383);t.pc=0x100;spu_step(s,t,ls.data());assert(t.steps==5 && spu_word(t.gpr[3],0)==7);
 put(0x20000,0x02000000);t.pc=0x20000;bool fault=false;
 try{spu_step(s,t,ls.data());}catch(const std::runtime_error&){fault=true;}
 assert(fault && t.steps==5 && t.pc==0x20000);
}
'''
        units['program.cpp']=units['program.cpp'].replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')+body
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);write_units(root,units)
            compile_units(unit_sources(root),root/'program',flags=('-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread'),jobs=2)
            result=subprocess.run([str(root/'program')],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':
    unittest.main()
