"""Shared relocation bodies and one content index across separately compiled TUs."""
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from anyps3.elf import ElfImage
from anyps3.native_build import compile_units,unit_sources,write_units
from anyps3.program import compile_program
from anyps3.spu import generate_parts
from tests.test_anyps3 import BLR,program
from tests.test_spu_block import image


@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class RelocSplitTests(unittest.TestCase):
    def test_private_runs_get_constant_zero_specialization_from_the_same_lifter(self):
        integer=image({0x100+i*4:0x1c004183 for i in range(8)})
        # One superblock function per source chunk serves every relocation delta.
        _,functions,_=generate_parts((integer,))
        self.assertEqual([n for n,_ in functions],['spu_fn_0'])
        self.assertIn('const std::uint32_t delta=t.reloc_delta;',functions[0][1])
        floating=image({0x100+i*4:0x58800000 for i in range(8)})
        _,functions,_=generate_parts((floating,))
        self.assertEqual([n for n,_ in functions],['spu_fn_0'])

    def test_shared_functions_index_once_and_relocation_across_translation_units(self):
        img=image({0xffc:0x33000105,0x1000:1,0x1004:0x1c00c183,0x1008:0x35000280})
        _,functions,_=generate_parts((img,))
        self.assertEqual([n for n,_ in functions],['spu_fn_0'])
        translated=replace(compile_program(ElfImage.parse(program(BLR)),process_entry=True),spu_images=(img,))
        units=translated.cpp_units(units=2)
        self.assertEqual(sum('class SpuRelocIndex' in v for v in units.values()),1)
        self.assertFalse(any('SpuRelocIndex' in v for n,v in units.items() if n.startswith('spu-')))
        body=r'''
#include <cassert>
int main(){
 PpuState s;NativeSpu::Thread t;t.entry=0xffc;std::vector<std::uint8_t> ls(0x40000);
 const std::uint8_t code[]={0x33,0,1,5,0,0,0,1,0x1c,0,0xc1,0x83,0x35,0,2,0x80};
 std::copy_n(code,16,ls.data()+0xffc);spu_prepare(s,t,ls.data());
 std::copy_n(code,16,ls.data()+0x10680);t.pc=0x10680;
 bool stopped=false;
 try{while(t.steps<4)spu_step(s,t,ls.data(),4-unsigned(t.steps));}catch(const SpuSliceEnd&){++t.steps;stopped=true;}
 assert(stopped && t.steps==4 && t.pc==0x10688 && spu_word(t.gpr[3],0)==3 && spu_word(t.gpr[5],0)==0x10684);
 assert(t.relocated_entries.value.load()>0 && !t.interpreted_instructions.value.load());
}
'''
        units['program.cpp']=units['program.cpp'].replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')+body
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write_units(root,units)
            compile_units(unit_sources(root),root/'test',flags=('-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread'),jobs=2)
            result=subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
