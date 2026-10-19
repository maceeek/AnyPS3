"""Split translation units link into the same observable native program."""
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.bundle import combine
from anyps3.elf import ElfImage, Segment
from anyps3.errors import AnyPS3Error
from anyps3.native_build import compile_units, unit_sources, write_units
from anyps3.program import compile_program
from anyps3.prx import relocate
from anyps3.spu import generate_parts
from tests.test_anyps3 import BLR, program
from tests.test_prx import prx_fixture

FLAGS = ('-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread')


def il(register, value):
    # SPU ISA v1.2 "Immediate Load Word": opcode 0x081, I16, RT.
    return ((0x81 << 23) | ((value & 0xffff) << 7) | register).to_bytes(4, 'big')


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SplitUnitTests(unittest.TestCase):
    def build(self, root, units):
        write_units(root, units)
        sources = unit_sources(root)
        compile_units(sources, root/'program', flags=FLAGS, jobs=4)
        return sources

    def test_module_import_and_export_chain_runs_from_separate_units(self):
        words = [0x7fe802a6, 0x38600000, 0x48000039, 0x60000000, 0x48000041, 0x7fe803a6, BLR]
        words += [0x60000000]*(21-len(words)); words[16] = BLR; words[20] = BLR
        game = ElfImage.parse(program(*words)); module = relocate(ElfImage.parse(prx_fixture()))
        image = combine(game, (module.image,))
        catalog = ({'ids': [0], 'name': 'fixture', 'start': module.image.entry, 'exports': module.report['exports']},)
        imports = ({'library': 'cellSysmodule', 'functions': [{'nid': '0x32267a31', 'stub': 0x10040}]},
                   {'library': 'TestLib', 'functions': [{'nid': '0x11223344', 'stub': 0x10050}]})
        result = compile_program(image, process_entry=True, additional_imports=imports, native_modules=catalog)
        units = result.cpp_units(max_steps=200, units=3)
        pages = {name: text for name, text in units.items() if name.startswith('ppu-')}
        self.assertGreater(len(pages), 1)
        # The module load stub at 0x10040 is a runtime service called out of line.
        self.assertTrue(any('anyps3_op_10040(state);' in text for text in pages.values()))
        self.assertIn('void anyps3_op_10040(PpuState& state) {', units['program.cpp'])
        self.assertFalse(any('native_module_load(state)' in text for text in pages.values()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'image.elf').write_bytes(image.data)
            self.build(root, units)
            run = subprocess.run([str(root/'program'), str(root/'image.elf')], capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('returned r3=42', run.stdout)
            self.assertIn('native module load id=0', run.stderr)

    def test_syscall_result_and_unknown_syscall_stop_from_separate_units(self):
        for words, code, expected in (([0x39600093, 0x44000002, BLR], 0, 'returned r3=79800000 steps='),
                                      ([0x39607FFF, 0x44000002, BLR], 1, 'unimplemented LV2 syscall 32767')):
            data = program(*words)
            units = compile_program(ElfImage.parse(data), process_entry=True).cpp_units(max_steps=100, units=2)
            with self.subTest(words=words), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'input.elf').write_bytes(data)
                self.build(root, units)
                run = subprocess.run([str(root/'program'), str(root/'input.elf')], capture_output=True, text=True, timeout=5)
                self.assertEqual(run.returncode, code, run.stderr)
                self.assertIn(expected, run.stdout if code == 0 else run.stderr)

    def test_overlay_heavy_spu_page_is_split_and_selects_exact_words(self):
        payload = il(3, 7)
        image = ElfImage(payload, 32, 'big', 0, 2, 23, 0x100, (Segment(0, 1, 7, 0, 0x100, 4, 4, 4),))
        first = b''.join(il(3, i) for i in range(1024))
        second = b''.join(il(4, 1024+i) for i in range(1024))
        _, functions, dispatch = generate_parts((image,), (((0x1000, first), (0x1000, second)),))
        names = {name for name, _ in functions}
        # SPUFAST superblocks: one bounded function per source (image segment, each
        # 1024-word overlay); content dispatch selects among the overlapping ranges.
        self.assertEqual(names, {'spu_fn_0', 'spu_fn_1', 'spu_fn_2'})
        self.assertIn('{0x1000u,0x2000u,spu_code_1,spu_fn_1,spu_lead_spu_fn_1},{0x1000u,0x2000u,spu_code_2,spu_fn_2,spu_lead_spu_fn_2}', dispatch)
        translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
        translated = replace(translated, spu_images=(image,), spu_overlays=(((0x1000, first), (0x1000, second)),))
        units = translated.cpp_units(units=2)
        self.assertEqual(sum(name.startswith('spu-') for name in units), 2)
        harness = r'''
#include <cassert>
template<class F> void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){
 PpuState s;NativeSpu::Thread t;t.entry=0x100;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-8*i);};
 put(0x100,0x40800383u);spu_prepare(s,t,ls);spu_step(s,t,ls);assert(t.pc==0x104 && spu_word(t.gpr[3],0)==7);
 for(unsigned i=0;i<1024;++i)put(0x1000+4*i,(0x81u<<23)|(i<<7)|3);
 for(unsigned i:{0u,511u,512u,1023u}){t.pc=0x1000+4*i;spu_step(s,t,ls);assert(t.pc==0x1004+4*i && spu_word(t.gpr[3],3)==i);}
 for(unsigned i=0;i<1024;++i)put(0x1000+4*i,(0x81u<<23)|((1024+i)<<7)|4);
 for(unsigned i:{0u,511u,512u,1023u}){t.pc=0x1000+4*i;spu_step(s,t,ls);assert(t.pc==0x1004+4*i && spu_word(t.gpr[4],0)==1024+i);}
 put(0x1000+4*600,(0x81u<<23)|(600u<<7)|5);t.pc=0x1000+4*600;spu_step(s,t,ls);assert(t.pc==0x1004+4*600 && spu_word(t.gpr[5],0)==600);
 t.pc=0x2000;fault([&]{spu_step(s,t,ls);});
 std::puts("spu split ok");
}
'''
        units['program.cpp'] = units['program.cpp'].replace('int main(int argc, char** argv)',
                                                            'int original_main(int argc, char** argv)')+harness
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build(root, units)
            run = subprocess.run([str(root/'program')], capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('spu split ok', run.stdout)

    def test_modified_unit_and_bad_count_are_rejected(self):
        translated = compile_program(ElfImage.parse(program(BLR)))
        with self.assertRaises(AnyPS3Error):
            translated.cpp_units(units=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_units(root, translated.cpp_units(units=1))
            (root/'ppu-000.cpp').write_text('int changed;')
            with self.assertRaisesRegex(AnyPS3Error, 'ppu-000.cpp'):
                unit_sources(root)


if __name__ == '__main__':
    unittest.main()
