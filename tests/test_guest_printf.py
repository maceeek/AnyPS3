"""Native firmware-3.01 printf regressions; expectations are fixed literals.

In particular these do NOT use host snprintf as the oracle: the original
firmware's truncation, integer h/hh and signed precision differ from ISO C.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.guest_printf import IMPORTS
from anyps3.program import compile_program
from anyps3.services import IMPORTS as SERVICES
from tests.test_anyps3 import BLR, program


NIDS = ('0x9f04f7af', '0xfa7f693d', '0x06574237', '0x0618936b')

COMMON = r'''
#include <cassert>
template<class F> void must_fault(F f){
 bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);
}
int main(int argc,char** argv){
 assert(argc==2);PpuState s;auto& r=s.r;auto& mem=s.memory;
 mem.map(0x20000,0x8000,6,nullptr,0);r[1]=0x27000;
 auto put=[&](unsigned a,const char* p){
   const auto n=std::strlen(p)+1;
   std::copy_n(p,n,mem.access(a,n,true));
 };
 auto check=[&](const char* f,std::initializer_list<std::uint64_t> a,const char* expected){
   put(0x21000,f);unsigned i=0;for(auto v:a)mem.store(0x22000+8*i++,8,v);
   r[3]=0x20000;r[4]=0x1000;r[5]=0x21000;r[6]=0x22000;
   anyps3_op_1000c(s);
   assert(r[3]==std::strlen(expected));
   assert(!std::memcmp(mem.access(0x20000,r[3]+1,false),expected,r[3]+1));
 };
 (void)check;(void)put;
 switch(std::atoi(argv[1])){
'''

CASES = {
    'registers_stack': r'''
 put(0x21000,"%d/%d/%d/%d/%d/%d/%lld/%x");
 r[3]=0x20000;r[4]=256;r[5]=0x21000;
 r[6]=1;r[7]=2;r[8]=3;r[9]=4;r[10]=5;
 mem.store(0x27068,8,999); // Poison last register's home slot.
 mem.store(0x27070,8,6);mem.store(0x27078,8,UINT64_C(0x8000000000000000));
 mem.store(0x27080,8,0xabcd);
 anyps3_op_10008(s);
 assert(r[3]==37);
 const char expected[]="1/2/3/4/5/6/-9223372036854775808/abcd";
 assert(!std::memcmp(mem.access(0x20000,sizeof(expected),false),expected,sizeof(expected)));
''',
    'va_list_big_endian': r'''
 put(0x21000,"%d|%llX|%s|%c");put(0x23000,"BE");
 mem.store(0x22000,4,0x12345678);mem.store(0x22004,4,0xfffffff9);
 mem.store(0x22008,4,0x01234567);mem.store(0x2200c,4,0x89abcdef);
 mem.store(0x22010,4,0x87654321);mem.store(0x22014,4,0x23000);
 mem.store(0x22018,8,0x141);
 r[3]=UINT64_C(0xdeadbeef00020000);r[4]=256;
 r[5]=UINT64_C(0xfeedface00021000);r[6]=UINT64_C(0xffffffff00022000);
 const auto cr=s.cr;s.fpr[1]=0xdeadbeef;
 anyps3_op_1000c(s);
 const char expected[]="-7|123456789ABCDEF|BE|A";
 assert(r[3]==23 && !std::memcmp(mem.access(0x20000,sizeof(expected),false),expected,sizeof(expected)));
 assert(mem.load(0x22000,8)==UINT64_C(0x12345678fffffff9));
 assert(s.cr==cr && s.fpr[1]==0xdeadbeef && r[6]==UINT64_C(0xffffffff00022000));
''',
    'truncation_and_nul': r'''
 put(0x21000,"abcdef");
 for(unsigned cap: {0u,1u,4u,7u}){
   std::fill_n(mem.access(0x20000,16,true),16,0x5a);
   r[3]=0x20000;r[4]=cap;r[5]=0x21000;anyps3_op_10008(s);
   assert(r[3]==6);
   for(unsigned i=0;i<16;++i){
     unsigned expected=0x5a;
     if(cap && i<std::min(cap-1,6u))expected=unsigned("abcdef"[i]);
     if(cap && i==6)expected=0; // Proven FW bug: NUL is at full length.
     assert(mem.load(0x20000+i,1)==expected);
   }
 }
 put(0x21000,"");r[3]=0x20000;r[4]=1;r[5]=0x21000;anyps3_op_10008(s);
 assert(!r[3] && !mem.load(0x20000,1));
 // Zero capacity permits an unmapped destination and va_list if unused.
 put(0x21000,"abcdef");r[3]=0;r[4]=0;r[5]=0x21000;r[6]=0;anyps3_op_1000c(s);assert(r[3]==6);
''',
    'integer_lengths_and_extremes': r'''
 check("%hhd|%hd|%ld|%lld",{0x1234,0x12345678,0x12345678,UINT64_C(0x8000000000000000)},
       "4660|305419896|305419896|-9223372036854775808");
 check("%d|%i|%u|%llu",{0x80000000,0xffffffff,UINT64_MAX,UINT64_MAX},
       "-2147483648|-1|4294967295|18446744073709551615");
 check("%hhx|%ho|%lx|%llx",{0x1234,0x10001,UINT64_C(0xabcdef0112345678),UINT64_MAX},
       "1234|200001|12345678|ffffffffffffffff");
''',
    'flags_width_precision': r'''
 check("[%#08x][%-6d][%+6d][% 5d]",{42,7,9,8},"[0x00002a][7     ][    +9][    8]");
 check("[%+.5d][%#.6x][%.0u][%#.0o]",{42,42,0,0},"[+0042][0x002a][][]");
 check("[%#X][%#o][%08.4d][%-05d]",{0xbeef,9,12,7},"[0XBEEF][011][    0012][7    ]");
 check("[%*.*d][%*.*d]",{UINT64_C(0xfffffff9),5,42,6,UINT64_MAX,42},"[00042  ][    42]");
 check("%+u|%#x|%p|%p|%%",{7,0,0,UINT64_C(0xabcdef0112345678)},"7|0|0x0|0x12345678|%");
''',
    'strings_char_bytes': r'''
 put(0x23000,"abcdef");
 check("[%8.3s][%-5s][%.3s]",{0x23000,0,0},"[     abc][(null)][(nu]");
 check("[%04s][%03c]",{0x23000,'Q'},"[abcdef][00Q]");
 mem.map(0x30000,3,6,nullptr,0);mem.store(0x30000,2,0xc3a9);mem.store(0x30002,1,0xff);
 check("%.3s",{0x30000},"\xc3\xa9\xff"); // No NUL beyond precision needed.
 check("%.0s",{0xffffffff},""); // Zero precision never dereferences s.
 put(0x21000,"x%cy");r[3]=0x20000;r[4]=16;r[5]=0x21000;r[6]=0;anyps3_op_10008(s);
 assert(r[3]==3 && mem.load(0x20000,4)==0x78007900);
''',
    'printf_vprintf_stderr': r'''
 put(0x21000,"reg=%d/%d/%d/%d/%d/%d/%d/%d\n");
 r[3]=0x21000;for(unsigned i=4;i<=10;++i)r[i]=i-3;
 mem.store(0x27070,8,8);anyps3_op_10000(s);assert(r[3]==20);
 put(0x21000,"list=%lld/%s\n");put(0x23000,"ok");
 mem.store(0x22000,8,UINT64_MAX);mem.store(0x22008,8,0x23000);
 r[3]=0x21000;r[4]=0x22000;anyps3_op_10004(s);assert(r[3]==11);
''',
    'unsupported_and_memory_faults': r'''
 for(const char* fmt:{"%q","%f","%e","%g","%n","%zd","%ls","%2$d","%","%1048577d","%*d"}){
   put(0x21000,fmt);mem.store(0x20000,8,UINT64_C(0xaaaaaaaaaaaaaaaa));
   r[3]=0x20000;r[4]=64;r[5]=0x21000;r[6]=0x80000000;
   must_fault([&]{anyps3_op_10008(s);});
   assert(mem.load(0x20000,8)==UINT64_C(0xaaaaaaaaaaaaaaaa));
 }
 put(0x21000,"%s");r[3]=0x20000;r[4]=64;r[5]=0x21000;r[6]=0xffffffff;
 must_fault([&]{anyps3_op_10008(s);});
 r[3]=0x20000;r[4]=64;r[5]=0x21000;r[6]=0xffffffff;
 must_fault([&]{anyps3_op_1000c(s);});
 put(0x21000,"abcdef");r[3]=0x27ffc;r[4]=4;r[5]=0x21000;
 must_fault([&]{anyps3_op_10008(s);}); // Out-of-map full-count terminator.
 r[3]=0xfffffffc;r[4]=4;r[5]=0x21000;must_fault([&]{anyps3_op_10008(s);});
 mem.map(0x30000,16,5,nullptr,0);r[3]=0x30000;r[4]=8;r[5]=0x21000;
 must_fault([&]{anyps3_op_10008(s);});
''',
    'split_destination': r'''
 mem.map(0x30000,2,6,nullptr,0);mem.map(0x30002,8,6,nullptr,0);
 put(0x21000,"abcde");r[3]=0x30000;r[4]=6;r[5]=0x21000;anyps3_op_10008(s);
 assert(r[3]==5 && mem.load(0x30000,2)==0x6162 && mem.load(0x30002,4)==0x63646500);
''',
}


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class GuestPrintfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='anyps3-printf-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.path = Path(cls.temp.name)
        imports = ({'library': 'sysPrxForUser', 'functions': [
            {'nid': nid, 'stub': 0x10000 + 4*i} for i, nid in enumerate(NIDS)]},)
        translated = compile_program(ElfImage.parse(program(BLR, BLR, BLR, BLR)),
                                     process_entry=True, extra_entries=(0x10004, 0x10008, 0x1000c),
                                     additional_imports=imports)
        cls.units = translated.cpp_units(units=2)
        source = translated.cpp().replace('int main(int argc, char** argv)',
                                          'int original_main(int argc, char** argv)')
        source += COMMON
        for i, body in enumerate(CASES.values()):
            source += f'case {i}:{{\n{body}\nbreak;}}\n'
        source += 'default:assert(false);\n}\n}\n'
        (cls.path / 'test.cpp').write_text(source)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                str(cls.path / 'test.cpp'), '-o', str(cls.path / 'test')],
                               capture_output=True, text=True, timeout=60)
        if build.returncode:
            raise AssertionError(build.stderr)

    def run_case(self, name):
        run = subprocess.run([str(self.path / 'test'), str(list(CASES).index(name))],
                             capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
        self.assertEqual(run.stdout, b'')
        return run.stderr

    def test_registration_and_split_emission(self):
        self.assertEqual(len(IMPORTS), 4)
        for i, nid in enumerate(NIDS):
            key = ('sysPrxForUser', nid)
            self.assertEqual(SERVICES[key], IMPORTS[key])
            self.assertIn(f'void anyps3_op_{0x10000 + 4*i:x}(PpuState& state)', self.units['program.cpp'])
            self.assertIn(f'anyps3_op_{0x10000 + 4*i:x}(state);',
                          '\n'.join(v for k, v in self.units.items() if k.startswith('ppu-')))

    def test_registers_and_stack(self):
        self.run_case('registers_stack')

    def test_va_list_big_endian_and_preservation(self):
        self.run_case('va_list_big_endian')

    def test_firmware_truncation_nul_and_zero_capacity(self):
        self.run_case('truncation_and_nul')

    def test_integer_lengths_and_extremes(self):
        self.run_case('integer_lengths_and_extremes')

    def test_flags_width_precision(self):
        self.run_case('flags_width_precision')

    def test_strings_and_character_bytes(self):
        self.run_case('strings_char_bytes')

    def test_printf_and_vprintf_stderr(self):
        stderr = self.run_case('printf_vprintf_stderr')
        self.assertIn(b'guest printf: reg=1/2/3/4/5/6/7/8\n', stderr)
        self.assertIn(b'guest printf: list=-1/ok\n', stderr)
        self.assertEqual(stderr.count(b'guest printf: '), 2)

    def test_unsupported_and_memory_faults(self):
        self.run_case('unsupported_and_memory_faults')

    def test_split_destination(self):
        self.run_case('split_destination')
