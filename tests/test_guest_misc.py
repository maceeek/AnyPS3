"""Native imports, with literal FW-3.01 masks, bytes and error expectations."""

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.guest_misc import IMPORTS, UNSUPPORTED
from anyps3.program import compile_program
from anyps3.services import IMPORTS as SERVICES
from tests.test_anyps3 import BLR, program


NIDS = ('0x3ef17f8c', '0x791b9219', '0xe6f2c1e7', '0xc4fd6121',
        '0xdd0c1e09', '0x5fdfb2fe', '0x45fe2fce', '0xdd3b27ac')
COMMON = r'''
#include <cassert>
template<class F> void must_fault(F f,const char* message){
 bool caught=false;try{f();}catch(const std::runtime_error& e){caught=true;assert(std::strstr(e.what(),message));}assert(caught);
}
int main(int argc,char** argv){
 assert(argc==2);PpuState s;auto& mem=s.memory;auto& r=s.r;
 mem.map(0x20000,0x8000,6,nullptr,0);r[1]=0x27000;
 auto put=[&](unsigned a,const char* text){std::copy_n(text,std::strlen(text)+1,mem.access(a,std::strlen(text)+1,true));};
 auto ctype=[&](std::uint64_t c){r[3]=c;anyps3_op_10000(s);return r[3];};
 auto format=[&](unsigned dst=0x23000){r[3]=dst;r[4]=0x21000;r[5]=0x22000;anyps3_op_10004(s);};
 auto attach=[&]{r[3]=0x12345678;anyps3_op_10010(s);return r[3];};
 auto detach=[&]{r[3]=0x12345678;anyps3_op_10014(s);return r[3];};
 auto init=[&](unsigned a,unsigned d,unsigned t=0,unsigned u=0){
  r[3]=a;r[4]=d;r[5]=t;r[6]=u;anyps3_op_10018(s);assert(!r[3]);
 };
 (void)put;(void)ctype;(void)format;(void)attach;(void)detach;(void)init;
 switch(std::strtol(argv[1],nullptr,10)){
'''

CASES = {
    'ctype_fixed_classes_and_s32_argument': r'''
 assert(ctype(UINT64_MAX)==0 && ctype(0xffffffff)==0);
 assert(ctype(0)==0x20 && ctype(8)==0x20 && ctype(9)==0x408);
 assert(ctype(10)==8 && ctype(13)==8 && ctype(14)==0x20);
 assert(ctype(' ')==0x18 && ctype('!')==0x10 && ctype('0')==4);
 assert(ctype('9')==4 && ctype('A')==0x41 && ctype('F')==0x41);
 assert(ctype('G')==1 && ctype('Z')==1 && ctype('a')==0x42);
 assert(ctype('f')==0x42 && ctype('g')==2 && ctype('z')==2);
 assert(ctype('{')==0x10 && ctype(127)==0x20);
 assert(ctype(0xaabbccdd00000041)==0x41); // r3 is an s32, no host Unicode/locale.
''',
    'ctype_outside_verified_range': r'''
 for(auto c:{UINT64_C(128),UINT64_C(255),UINT64_C(0xfffffffe),UINT64_C(0x80000000)})
  must_fault([&]{ctype(c);},"EOF/ASCII range");
''',
    'ctype_table_firmware_digest': r'''
 for(int c=-1;c<=127;++c){const auto v=ctype(std::uint64_t(std::int64_t(c)));std::putchar((v>>8)&255);std::putchar(v&255);}
''',
    'vsprintf_be_slots_and_terminator': r'''
 put(0x21000,"%d/%08x/%llx/%s/%c");put(0x24000,"guest");
 mem.store(0x22000,8,0xaaaaaaaa80000000);mem.store(0x22008,8,0xbbbbbbbb1234abcd);
 mem.store(0x22010,8,0x0123456789abcdef);mem.store(0x22018,8,0xcccccccc00024000);
 mem.store(0x22020,8,'!');mem.store(0x23000,8,UINT64_MAX);
 const auto old=mem.load(0x22000,8);r[2]=0x10203040;s.cr=0x12345678;s.fpr[5]=0x123456789abcdef0;r[31]=0xfedcba98;
 format();assert(r[3]==44);
 assert(!std::memcmp(mem.access(0x23000,45,false),"-2147483648/1234abcd/123456789abcdef/guest/!",45));
 assert(mem.load(0x22000,8)==old && r[2]==0x10203040 && s.cr==0x12345678);
 assert(s.fpr[5]==0x123456789abcdef0 && r[31]==0xfedcba98);
''',
    'vsprintf_empty_and_embedded_zero': r'''
 put(0x21000,"");mem.store(0x23000,2,0xffff);format();assert(!r[3] && mem.load(0x23000,2)==0xff);
 put(0x21000,"A%cB");mem.store(0x22000,8,0);mem.store(0x23000,8,UINT64_MAX);
 format();assert(r[3]==3 && mem.load(0x23000,4)==0x41004200 && mem.load(0x23004,1)==255);
''',
    'vsprintf_adjacent_mappings_and_fault_atomicity': r'''
 put(0x21000,"abcdef");mem.map(0x30000,3,6,nullptr,0);mem.map(0x30003,4,6,nullptr,0);
 format(0x30000);assert(r[3]==6 && mem.load(0x30000,2)==0x6162 && mem.load(0x30005,2)==0x6600);
 mem.map(0x31000,6,6,nullptr,0);mem.store(0x31000,4,0xcccccccc);
 must_fault([&]{format(0x31000);},"unmapped");assert(mem.load(0x31000,4)==0xcccccccc);
 mem.map(0x32000,7,4,nullptr,0);must_fault([&]{format(0x32000);},"permission");
 must_fault([&]{format(0xfffffffd);},"overflow");
''',
    'vsprintf_bad_format_and_ap': r'''
 put(0x21000,"%f");mem.store(0x23000,4,0xcccccccc);
 must_fault([&]{format();},"unsupported");assert(mem.load(0x23000,4)==0xcccccccc);
 put(0x21000,"%x");r[3]=0x23000;r[4]=0x21000;r[5]=0x90000;
 must_fault([&]{anyps3_op_10004(s);},"unmapped");assert(mem.load(0x23000,4)==0xcccccccc);
''',
    'qsort_empty_singleton_and_comparator_stop': r'''
 r[3]=0xfffffff0;r[4]=0;r[5]=0xffffffff;r[6]=0;anyps3_op_1000c(s);assert(r[3]==0xfffffff0);
 r[4]=1;anyps3_op_1000c(s);assert(r[3]==0xfffffff0);
 mem.store(0x23000,8,0x0200000001000000);r[3]=0x23000;r[4]=2;r[5]=4;r[6]=0x24000;
 must_fault([&]{anyps3_op_1000c(s);},"comparator OPD");assert(mem.load(0x23000,8)==0x0200000001000000);
 r[5]=0;must_fault([&]{anyps3_op_1000c(s);},"comparator OPD");
 r[3]=0xfffffffc;r[5]=4;must_fault([&]{anyps3_op_1000c(s);},"overflow");
 r[3]=0x23000;r[4]=0xffffffff;r[5]=0xffffffff;must_fault([&]{anyps3_op_1000c(s);},"overflow");
''',
    'spu_printf_absence_registration_reset': r'''
 assert(attach()==UINT64_C(0xffffffff8001000f) && detach()==UINT64_C(0xffffffff8001000f));
 init(0x12340000,0);must_fault([&]{attach();},"registered guest OPD");assert(detach()==UINT64_C(0xffffffff8001000f));
 init(0,0x56780000);assert(attach()==UINT64_C(0xffffffff8001000f));must_fault([&]{detach();},"registered guest OPD");
 anyps3_op_1001c(s);assert(!r[3]);assert(attach()==UINT64_C(0xffffffff8001000f) && detach()==UINT64_C(0xffffffff8001000f));
 init(0,0,0x12345678,0x23456789);assert(attach()==UINT64_C(0xffffffff8001000f));
''',
    'spu_printf_process_identity_and_shared_threads': r'''
 init(0x12345678,0x87654321);
 PpuState other;other.memory=s.memory;other.r[1]=r[1];other.r[3]=7;
 anyps3_op_10010(other);assert(other.r[3]==UINT64_C(0xffffffff8001000f));
 other.thread_atexit=s.thread_atexit;
 must_fault([&]{anyps3_op_10010(other);},"registered guest OPD");
 anyps3_op_1001c(other);assert(attach()==UINT64_C(0xffffffff8001000f));
 init(0x12345678,0);s.thread_atexit=std::make_shared<std::array<std::uint32_t,8>>();
 assert(attach()==UINT64_C(0xffffffff8001000f));
''',
    'process_exit_callbacks_stop': r'''
 s.atexit_callback=0x24000;r[3]=37;must_fault([&]{anyps3_op_10008(s);},"atexit callbacks");assert(r[3]==37);
 s.atexit_callback=0;s.at_exit_callback=0x24000;
 must_fault([&]{anyps3_op_10008(s);},"atexit callbacks");
''',
}


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class GuestMiscTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='anyps3-misc-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.path = Path(cls.temp.name)
        entries = tuple(0x10000 + 4*i for i in range(len(NIDS)))
        imports = ({'library': 'sysPrxForUser', 'functions': [
            {'nid': nid, 'stub': address} for nid, address in zip(NIDS, entries)]},)
        translated = compile_program(ElfImage.parse(program(*([BLR]*len(NIDS)))),
                                     process_entry=True, extra_entries=entries[1:], additional_imports=imports)
        cls.units = translated.cpp_units(units=2)
        source = translated.cpp().replace('int main(int argc, char** argv)',
                                          'int original_main(int argc, char** argv)') + COMMON
        for i, body in enumerate(CASES.values()):
            source += f'case {i}:{{\n{body}\nbreak;}}\n'
        for i, status in enumerate((0, 37, -1, 0x12345678, 41)):
            source += f'case {100+i}:r[3]=std::uint64_t(std::int64_t({status}));'
            if i == 4:
                source += 's.thread_id=2;s.ppu_threads.push_back(PpuContext{});'
            source += 'anyps3_op_10008(s);assert(false);break;\n'
        source += 'default:assert(false);\n}\n}\n'
        (cls.path / 'test.cpp').write_text(source)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                str(cls.path / 'test.cpp'), '-o', str(cls.path / 'test')],
                               capture_output=True, text=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stderr)

    def run_case(self, name):
        run = subprocess.run([str(self.path / 'test'), str(list(CASES).index(name))],
                             capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
        if name == 'ctype_table_firmware_digest':
            self.assertEqual(len(run.stdout), 258)
            # Hash of original ELF bytes 0x280129b0..0x28012ab1, not a host ctype oracle.
            self.assertEqual(hashlib.sha256(run.stdout).hexdigest(),
                             '1f361662178a9c6f469ceb84124606ee4f4f515292255d943e0447d828db8fab')
        else:
            self.assertEqual(run.stdout, b'')

    def test_process_exit_actual_status(self):
        for i, status in enumerate((0, 37, -1, 0x12345678, 41)):
            with self.subTest(status=status):
                run = subprocess.run([str(self.path / 'test'), str(100+i)],
                                     capture_output=True, timeout=5)
                self.assertEqual(run.returncode, status & 255, run.stderr.decode(errors='replace'))
                self.assertIn(f'native process exit status={status}\n'.encode(), run.stderr)
                self.assertEqual(run.stdout, b'')

    def test_registration_split_emission_and_nid_hashes(self):
        suffix = bytes.fromhex('6759659904250490566427499489741a')
        for (library, nid), (name, _) in {**IMPORTS, **UNSUPPORTED}.items():
            if name is not None:
                digest = hashlib.sha1(name.encode() + suffix).digest()
                self.assertEqual(nid, f'0x{int.from_bytes(digest[:4], "little"):08x}')
            if (library, nid) in IMPORTS:
                self.assertEqual(SERVICES[(library, nid)], IMPORTS[(library, nid)])
            else:
                self.assertNotIn((library, nid), SERVICES)
        for i in range(len(NIDS)):
            self.assertIn(f'void anyps3_op_{0x10000+4*i:x}(PpuState& state)', self.units['program.cpp'])
            self.assertIn(f'anyps3_op_{0x10000+4*i:x}(state);',
                          '\n'.join(v for k, v in self.units.items() if k.startswith('ppu-')))

    def test_unsupported_imports_remain_explicit_translation_traps(self):
        image = ElfImage.parse(program(*([BLR]*len(UNSUPPORTED))))
        entries = tuple(0x10000+4*i for i in range(len(UNSUPPORTED)))
        imports = tuple({'library': library, 'functions': [{'nid': nid, 'stub': address}]}
                        for (library, nid), address in zip(UNSUPPORTED, entries))
        translated = compile_program(image, diagnostic=True, process_entry=True,
                                     extra_entries=entries[1:], additional_imports=imports)
        self.assertEqual(len(translated.traps), len(UNSUPPORTED))
        for (library, nid), address in zip(UNSUPPORTED, entries):
            self.assertEqual(translated.traps[address], f'Unimplemented import {library} NID={nid}')


for _name in CASES:
    def _test(self, name=_name):
        self.run_case(name)
    setattr(GuestMiscTests, 'test_' + _name, _test)
