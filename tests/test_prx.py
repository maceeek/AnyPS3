import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
import unittest

from anyps3.bundle import combine
from anyps3.elf import ElfImage
from anyps3.errors import AnyPS3Error
from anyps3.program import compile_program
from anyps3.prx import RelocatedModule, make_image, relocate, constructor_entries, linkage, link_references
from tests.test_anyps3 import program, BLR


def prx_fixture():
    code=bytearray(256)
    # Actual module start writes 42 through its relocated TOC, then returns zero.
    struct.pack_into('>6I',code,0,0x3880002a,0x90820000,0x38600000,BLR,0x80620000,BLR)
    struct.pack_into('>H2B28s5I',code,0x30,0,1,0,b'TestModule',0x80,0x70,0xa8,0xa8,0xa8)
    struct.pack_into('>BB5H4x3I',code,0x70,28,0,1,0x8000,1,0,0,0,0xb0,0xb8)
    struct.pack_into('>BB5H4x3I',code,0x8c,28,0,1,1,1,0,0,0xd0,0xb4,0xbc)
    struct.pack_into('>8I',code,0xb0,0xbc9a0086,0x11223344,0xc0,0xc8,0,0x80,0x10,0x80)
    code[0xd0:0xd8]=b'TestLib\0'
    records=[(0x50,1,0x80),(0x54,0,0x70),(0x58,0,0xa8),(0x5c,0,0xa8),(0x60,0,0xa8),
             (0x84,0,0xb0),(0x88,0,0xb8),(0x9c,0,0xd0),(0xa0,0,0xb4),(0xa4,0,0xbc),
             (0xb8,0,0xc0),(0xbc,0,0xc8),(0xc0,0,0),(0xc4,1,0x80),(0xc8,0,0x10),(0xcc,1,0x80)]
    rel=b''.join(struct.pack('>QHBBIQ',off,0,seg,0,1,value) for off,seg,value in records)
    data=bytearray(make_image([(0,5,bytes(code)),(0x10000,6,bytes(256)),(0,0,rel)]).data)
    struct.pack_into('>H',data,16,0xffa4)
    first_offset=struct.unpack_from('>Q',data,64+8)[0]
    struct.pack_into('>Q',data,64+24,first_offset+0x30)
    struct.pack_into('>Q',data,64+40,0x30000) # Deliberate original VA overlap.
    struct.pack_into('>I',data,64+112,0x700000a4)
    struct.pack_into('>Q',data,64+112+40,0)
    return bytes(data)


def import_reference_fixture(attributes=0x2009, kind=1):
    data=bytearray(0x100)
    struct.pack_into('>I',data,0,BLR)
    struct.pack_into('>BB5H4x3I',data,0x10,28,0,1,attributes,1,0,0,0x10040,0x10050,0x10058)
    data[0x40:0x4c]=b'cellSysutil\0'
    struct.pack_into('>2I',data,0x50,0xb59872ef,0x10060)
    struct.pack_into('>I',data,0x58,0x10000)
    struct.pack_into('>9I',data,0x60,kind,0x10080,0,1,0x10084,8,0,0,0)
    struct.pack_into('>2I',data,0x80,0x1b8,0x168)
    return make_image([(0x10000,7,bytes(data))])


class PrxTests(unittest.TestCase):
    def test_import_references_receive_linked_export_descriptor(self):
        image=import_reference_fixture()
        imports=linkage(image,0x10010,0x1002c,exports=False)
        self.assertEqual(imports[0]['functions'][0]['references'],
                         [{'type':1,'address':0x10080,'addend':0},{'type':1,'address':0x10084,'addend':8}])
        export={'descriptor':0x20130bb4,'code':0x201104d0}
        module=RelocatedModule(image,{'imports':imports})
        linked_image,linked=link_references(module,{('cellSysutil','0xb59872ef'):(None,export)})
        self.assertEqual(linked_image.read_virtual(0x10080,8),bytes.fromhex('20130bb420130bbc'))
        self.assertEqual(image.read_virtual(0x10080,8),bytes.fromhex('000001b800000168'))
        self.assertEqual([x['code'] for x in linked],[0x201104d0,0x201104d0])
        with self.assertRaisesRegex(AnyPS3Error,'Unlinked'):link_references(module,{})
        plain=import_reference_fixture(attributes=9)
        self.assertNotIn('references',linkage(plain,0x10010,0x1002c,exports=False)[0]['functions'][0])
        with self.assertRaisesRegex(AnyPS3Error,'reference type 4'):
            linkage(import_reference_fixture(kind=4),0x10010,0x1002c,exports=False)

    def test_constructor_walker_uses_encoded_toc_slot_and_validates_opd(self):
        for displacement in (-32724,-32736):
            code=bytearray(128);data=bytearray(256)
            struct.pack_into('>I',code,0,0x81220000|(displacement&0xffff))
            code[4:20]=bytes.fromhex('7c0802a6f821ff81fbe100783be9fffc')
            struct.pack_into('>I',code,64,BLR)
            toc=0x28000
            struct.pack_into('>I',data,0x8000+displacement,0x20088)
            struct.pack_into('>4I',data,0x80,0xffffffff,0x200a0,0,0)
            struct.pack_into('>2I',data,0xa0,0x10040,toc)
            image=make_image([(0x10000,5,bytes(code)),(0x20000,6,bytes(data))])
            self.assertEqual(constructor_entries(image,toc),[0x10040])
            struct.pack_into('>I',data,0xa4,toc+4)
            image=make_image([(0x10000,5,bytes(code)),(0x20000,6,bytes(data))])
            with self.assertRaisesRegex(AnyPS3Error,'constructor TOC'):constructor_entries(image,toc)

    def test_relocation_separates_segments_and_resolves_opds(self):
        result=relocate(ElfImage.parse(prx_fixture()))
        self.assertEqual(result.image.resolve_entry(),(0x20000000,0x20030080))
        self.assertEqual(result.report['relocation_counts'],{1:16})
        self.assertEqual(result.report['imports'],[])
        self.assertEqual(result.report['exports'][1]['functions'][0]['code'],0x20000010)

    def test_invalid_relocation_does_not_succeed(self):
        original=prx_fixture();im=ElfImage.parse(original);off=im.segments[2].offset
        for relative,fmt,value in ((16,'>Q',1<<40),(12,'>I',999),(11,'>B',16),(0,'>Q',0x30000)):
            data=bytearray(original);struct.pack_into(fmt,data,off+relative,value)
            with self.subTest(value=value),self.assertRaises(AnyPS3Error):relocate(ElfImage.parse(bytes(data)))

    def test_bundle_keeps_original_load_bytes_and_rejects_overlap(self):
        game=ElfImage.parse(program(BLR));module=relocate(ElfImage.parse(prx_fixture())).image
        combined=combine(game,(module,))
        for s in game.segments:
            self.assertEqual(combined.read_virtual(s.virtual_address,s.file_size),game.data[s.offset:s.offset+s.file_size])
        with self.assertRaisesRegex(AnyPS3Error,'overlap'):combine(game,(game,))

    @unittest.skipUnless(shutil.which('c++'),'Native C++ compiler unavailable')
    def test_resident_start_executes_before_first_export(self):
        game=ElfImage.parse(program(0x7fe802a6,0x4800000d,0x7fe803a6,BLR,BLR))
        module=relocate(ElfImage.parse(prx_fixture()))
        image=combine(game,(module.image,))
        catalog=({'ids':[],'resident':True,'name':'fixture','start':module.image.entry,'exports':module.report['exports']},)
        imports=({'library':'TestLib','functions':[{'nid':'0x11223344','stub':0x10010}]},)
        result=compile_program(image,process_entry=True,additional_imports=imports,native_modules=catalog)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'test.cpp').write_text(result.cpp(max_steps=100));(p/'input.elf').write_bytes(image.data)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(p/'test.cpp'),'-o',str(p/'test')],capture_output=True,text=True,timeout=300)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(p/'test'),str(p/'input.elf')],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('resident module started: fixture',run.stderr)
            self.assertIn('returned r3=42',run.stdout)

    @unittest.skipUnless(shutil.which('c++'),'Native C++ compiler unavailable')
    def test_real_module_start_and_export_execute_in_game_call_chain(self):
        words=[0x7fe802a6,0x38600000,0x48000039,0x60000000,0x48000041,0x7fe803a6,BLR]
        words += [0x60000000]*(21-len(words));words[16]=BLR;words[20]=BLR
        game=ElfImage.parse(program(*words));module=relocate(ElfImage.parse(prx_fixture()))
        image=combine(game,(module.image,))
        catalog=({'ids':[0],'name':'fixture','start':module.image.entry,'exports':module.report['exports']},)
        imports=({'library':'cellSysmodule','functions':[{'nid':'0x32267a31','stub':0x10040}]},
                 {'library':'TestLib','functions':[{'nid':'0x11223344','stub':0x10050}]})
        result=compile_program(image,process_entry=True,additional_imports=imports,native_modules=catalog)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cpp=root/'native.cpp';binary=root/'native';elf=root/'image.elf'
            cpp.write_text(result.cpp(max_steps=200));elf.write_bytes(image.data)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(cpp),'-o',str(binary)],capture_output=True,text=True,timeout=300)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary),str(elf)],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('returned r3=42',run.stdout)
            self.assertIn('native module load id=0',run.stderr)
            # Exercise failures in the same generated native runtime. An absent
            # module, an unloaded export, and failed start must never succeed.
            source=result.cpp(max_steps=200).replace('int main(int argc, char** argv)',
                                                      'int original_main(int argc, char** argv)')
            source += r'''
#include <cassert>
int main() {
    PpuState s;
    auto fault=[&](auto fn) { bool failed=false; try { fn(); } catch(const std::runtime_error&) { failed=true; } assert(failed); };
    s.r[3]=99; fault([&]{ native_module_load(s); }); assert(s.loaded_modules.empty());
    s.pc=0x10050; fault([&]{ anyps3_entry(s,false); }); assert(s.loaded_modules.empty());
    s.module_loading=true; s.module_id=0; s.r[3]=1;
    fault([&]{ native_module_return(s); }); assert(s.loaded_modules.empty());
    s.r[3]=0; s.module_lr=0x1000; s.module_toc=0x2000;
    native_module_return(s); assert(s.loaded_modules.count(0) && s.pc==0x1000 && s.r[2]==0x2000);
    s.r[3]=0; s.lr=0x1000; native_module_load(s); assert(s.r[3]==0x80012001);
}
'''
            cpp.write_text(source)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(cpp),'-o',str(binary)],capture_output=True,text=True,timeout=300)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(binary)],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
