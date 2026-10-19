"""Native import tests with fixed FW-3.01 capacity/alignment expectations."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.guest_heap import IMPORTS
from anyps3.program import compile_program
from anyps3.services import IMPORTS as SERVICES
from tests.test_anyps3 import BLR, program

NIDS = ('0xb2fcf2c8', '0x35168520', '0x44265c08', '0x8a561d92', '0xaede4b03')
COMMON = r'''
#include <cassert>
template<class F> void must_fault(F f,const char* message){
 bool caught=false;try{f();}catch(const std::runtime_error& e){caught=true;assert(std::strstr(e.what(),message));}assert(caught);
}
int main(int argc,char**argv){
 assert(argc==2);PpuState s;auto& mem=s.memory;auto& r=s.r;
 mem.map(0x20000,0x10000,6,nullptr,0);r[1]=0x2f000;
 std::copy_n("HeapTest",9,mem.access(0x21000,9,true));
 auto create=[&](unsigned size,unsigned flags=0,unsigned option=0){
   r[3]=0x21000;r[4]=size;r[5]=flags;r[6]=option;anyps3_op_10000(s);return unsigned(r[3]);
 };
 auto alloc=[&](unsigned heap,unsigned size){r[3]=heap;r[4]=size;anyps3_op_10004(s);return unsigned(r[3]);};
 auto align=[&](unsigned heap,unsigned alignment,unsigned size){r[3]=heap;r[4]=alignment;r[5]=size;anyps3_op_10008(s);return unsigned(r[3]);};
 auto release=[&](unsigned heap,unsigned pointer){r[3]=heap;r[4]=pointer;anyps3_op_1000c(s);assert(!r[3]);};
 auto destroy=[&](unsigned heap){r[3]=heap;anyps3_op_10010(s);assert(!r[3]);};
 auto supplied=[&](unsigned size=0x10000){
   mem.map(0x30000,0x30000,6,nullptr,0);
   mem.store(0x22000,4,8);mem.store(0x22004,4,0x40000);
   return create(size,0x80000000,0x22000);
 };
 (void)create;(void)alloc;(void)align;(void)release;(void)destroy;(void)supplied;
 switch(std::atoi(argv[1])){
'''
CASES = {
    'owned_accounting_and_delete_live': r'''
 const auto used=main_memory_used(s);const auto mapped=mem.allocated;
 const auto h=create(0x10001);assert(h && !(h&15));
 assert(main_memory_used(s)==used+0x20000 && mem.allocated==mapped+0x20000);
 const auto a=alloc(h,32),b=alloc(h,4096);assert(a && b && a+32<=b);
 mem.store(a,8,0x0123456789abcdef);mem.store(b,4,0x76543210);
 must_fault([&]{guest_free(s,h-16);},"foreign");
 destroy(h);assert(main_memory_used(s)==used && mem.allocated==mapped);
 must_fault([&]{mem.load(a,1);},"unmapped");
 must_fault([&]{alloc(h,1);},"heap handle");must_fault([&]{destroy(h);},"heap handle");
''',
    'supplied_buffer_spanning_adjacent_mappings': r'''
 // Contiguous guest addresses backed by two separate mappings (as libhttp's pool in run 269).
 mem.map(0x30000,0x8000,6,nullptr,0);mem.map(0x38000,0x8000,6,nullptr,0);
 mem.store(0x22000,4,8);mem.store(0x22004,4,0x30000);
 const auto h=create(0x10000,0x80000000,0x22000);assert(h==0x30010);
 unsigned last=0;for(unsigned i=0;i<14;++i){const auto a=alloc(h,4000);assert(a);last=a;}
 assert(last>0x38000);mem.store(last,4,0xabcdef01);assert(mem.load(last,4)==0xabcdef01);
 destroy(h);
 mem.store(0x22004,4,0x34000);must_fault([&]{create(0x10000,0x80000000,0x22000);},"unmapped supplied");  // runs past 0x40000
 mem.map(0x48000,0x8000,6,nullptr,0);mem.store(0x22004,4,0x3c000);
 must_fault([&]{create(0x10000,0x80000000,0x22000);},"unmapped supplied");  // gap 0x40000-0x48000
 mem.store(0x22004,4,0x30000);const auto h2=create(0x10000,0x80000000,0x22000);
 mem.regions.erase(std::remove_if(mem.regions.begin(),mem.regions.end(),[](const auto& v){return v.base==0x38000;}),mem.regions.end());
 must_fault([&]{alloc(h2,1);},"heap handle");
''',
    'supplied_lifetime_and_accounting': r'''
 const auto h=supplied();const auto used=main_memory_used(s);assert(h==0x40010);
 const auto a=alloc(h,64);assert(a==0x40220);mem.store(a,4,0x12345678);
 assert(main_memory_used(s)==used);destroy(h);
 assert(main_memory_used(s)==used && mem.load(a,4)==0x12345678);
 mem.store(a,4,0x98765432); // Caller still owns the backing after delete.
 const auto h2=create(0x10000,0x80000000,0x22000);assert(h2==h);destroy(h2);
''',
    'fixed_firmware_capacity': r'''
 const auto h=supplied();
 assert(!alloc(h,0xfda0));const auto p=alloc(h,0xfd98);assert(p && !(p&15));
 assert(!alloc(h,1));mem.store(p,1,0x42);mem.store(p+0xfd97,1,0x81);
 release(h,p);assert(!alloc(h,0xfda0));assert(alloc(h,0xfd98)==p);
''',
    'small_zero_and_chunk_charges': r'''
 const auto h=supplied();unsigned previous=0;
 for(unsigned n:{0u,1u,6u,7u,8u,16u,232u,233u}){
   const auto p=alloc(h,n);assert(p && !(p&15));
   if(previous)assert(p>previous);
   previous=p;
   if(n){mem.store(p,1,0x12);mem.store(p+n-1,1,0x34);}
 }
 // Zero allocation is a real, individually releasable chunk.
 const auto zero=alloc(h,0);release(h,zero);assert(alloc(h,0)==zero);
 assert(!alloc(h,0xffffffff) && !alloc(h,0xffffffc0));
 assert(!alloc(0,12) && !align(0,64,12));
''',
    'reuse_coalescing_and_nonoverlap': r'''
 const auto h=supplied();const auto a=alloc(h,0x2000),b=alloc(h,0x2000),c=alloc(h,0x2000);
 assert(a+0x2000<=b && b+0x2000<=c);
 mem.store(a,4,0x11111111);mem.store(c,4,0x33333333);
 release(h,b);const auto b2=alloc(h,0x2000);assert(b2==b);
 assert(mem.load(a,4)==0x11111111 && mem.load(c,4)==0x33333333);
 release(h,b2);release(h,a);release(h,c);
 assert(alloc(h,0xfd98)==a); // All chunks, including top, coalesce again.
''',
    'alignment_and_temporary_capacity': r'''
 const auto h=supplied();
 for(unsigned a:{0u,1u,4u,16u,24u,32u,64u,256u,4096u}){
   const auto p=align(h,a,23);assert(p);
   const unsigned effective=a<=16?16:a==24?32:a;
   assert(p%effective==0);mem.store(p,1,0x11);mem.store(p+22,1,0x22);release(h,p);
 }
 assert(!align(h,64,0xfd80)); // Fits payload; original temporary allocation does not fit.
 assert(!align(h,0x80000000,0xffffffff));
 must_fault([&]{align(h,0x80000001,1);},"overflow");
 assert(alloc(h,0xfd98));
''',
    'create_options_and_flags': r'''
 assert(!create(0) && !create(0xffff0001));
 mem.store(0x22000,4,7);mem.store(0x22004,4,0x40000);
 const auto before=mem.allocated;assert(!create(0x10000,0,0x22000));assert(mem.allocated==before);
 mem.store(0x22000,4,8);
 // Nonnegative flags ignore supplied pointer; negative odd flags allocate their own backing too.
 for(unsigned f:{0u,1u,0x80000001u}){
   const auto h=create(1,f,0x22000);assert(h>=0x10000000);assert(mem.allocated==before+0x10000);destroy(h);
 }
 mem.store(0x22004,4,0);const auto h=create(1,0x80000000,0x22000);assert(h);destroy(h);
''',
    'budget_container_and_address_reservations': r'''
 s.mmapper.areas.push_back({0x10000000,0x10000000,0x10000});
 const auto h=create(0x10000);assert(h==0x20000010);destroy(h);
 s.mmapper.container_reserved=0x10000000-mem.allocated;
 const auto count=mem.regions.size();assert(!create(0x10000));assert(mem.regions.size()==count);
 s.mmapper.container_reserved=0;
 const auto bytes=std::make_shared<GuestBacking>(0x10000);
 s.mmapper.objects.push_back({0,0x8000100,0x10000,false,true,bytes});
 const auto used=main_memory_used(s);const auto h2=create(1);assert(main_memory_used(s)==used+0x10000);
 destroy(h2);assert(main_memory_used(s)==used);
''',
    'foreign_interior_double_and_heap_errors': r'''
 const auto h=supplied();const auto h2=create(1);const auto a=alloc(h,32),b=alloc(h2,32);
 release(h,0);
 must_fault([&]{release(h,b);},"foreign");must_fault([&]{release(h,a+1);},"interior");
 assert(alloc(h2,32)>b);release(h,a);
 must_fault([&]{release(h,a);},"already freed");must_fault([&]{alloc(0xdead,1);},"heap handle");
 must_fault([&]{destroy(0);},"heap handle");destroy(h2);destroy(h);
''',
    'supplied_bounds_aliases_and_overlap': r'''
 const auto h=supplied();
 must_fault([&]{create(0x10000,0x80000000,0x22000);},"overlapping");
 mem.store(0x22004,4,0x40001);must_fault([&]{create(0x10000,0x80000000,0x22000);},"aligned");
 mem.store(0x22004,4,0x70000);must_fault([&]{create(0x10000,0x80000000,0x22000);},"unmapped");
 mem.store(0x22004,4,0xffff0000);must_fault([&]{create(0x20000,0x80000000,0x22000);},"unmapped");
 auto backing=guest_heap_region(s,0x40000,0x10000)->bytes;
 mem.regions.push_back({0x70000,6,backing});must_fault([&]{alloc(h,1);},"aliased");mem.regions.pop_back();
 destroy(h);mem.store(0x22004,4,0x40000);
 must_fault([&]{create(0x240,0x80000000,0x22000);},"size rejected");
 const auto before=mem.allocated;r[3]=0x90000;r[4]=0x10000;r[5]=0;r[6]=0;
 must_fault([&]{anyps3_op_10000(s);},"unmapped");assert(mem.allocated==before);
''',
    'state_identity_removed_and_replaced_backing': r'''
 const auto h=supplied();PpuState other;other.memory=s.memory;other.r[1]=s.r[1];other.r[3]=h;other.r[4]=1;
 must_fault([&]{anyps3_op_10004(other);},"heap handle");
 auto it=guest_heap_region(s,0x40000,0x10000);const auto old=it->bytes;
 it->bytes=std::make_shared<GuestBacking>(0x30000);
 must_fault([&]{alloc(h,1);},"replaced");it->bytes=old;
 destroy(h);
 // Same host address and even the same guest backing cannot revive old lifecycle.
 const auto fresh=create(0x10000,0x80000000,0x22000);assert(fresh==h);
 s.thread_atexit=std::make_shared<std::array<std::uint32_t,8>>();
 must_fault([&]{alloc(h,1);},"heap handle");
''',
}

@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class GuestHeapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='anyps3-heap-')
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
        self.assertEqual(run.stdout, b'')

    def test_registration_and_split_emission(self):
        self.assertEqual(len(IMPORTS), 5)
        for i, nid in enumerate(NIDS):
            self.assertEqual(SERVICES[('sysPrxForUser', nid)], IMPORTS[('sysPrxForUser', nid)])
            self.assertIn(f'void anyps3_op_{0x10000+4*i:x}(PpuState& state)', self.units['program.cpp'])
            self.assertIn(f'anyps3_op_{0x10000+4*i:x}(state);',
                          '\n'.join(v for k, v in self.units.items() if k.startswith('ppu-')))
        for nid in ('0xb6369393', '0xd1ad4570', '0x8985b5b6', '0x3172759d'):
            self.assertNotIn(('sysPrxForUser', nid), SERVICES)

for _name in CASES:
    def _test(self, name=_name):
        self.run_case(name)
    setattr(GuestHeapTests, 'test_' + _name, _test)
