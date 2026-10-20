"""ZeroTable (runtime.py): GuestMemory::page_regions on anonymous mmap storage.

1. Contract: a new table reads zero everywhere, writes stick, a copy is independent and equal, a move leaves the source
   empty, and the remap invalidation (std::fill over begin/end) clears every entry.
2. Cost: creating a state must not touch the 4 MiB table: mincore reports fewer than 16 of its 1024 pages resident right
   after a PpuState is constructed (a value-initialising fill makes all of them resident; that fill plus glibc trimming
   the freed table back to the kernel made test_ppu_fp_lazy run 5x slower).
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

PROGRAM = r'''
#include <cassert>
static std::size_t resident_pages(const void* p,std::size_t bytes){
    const std::size_t page=std::size_t(sysconf(_SC_PAGESIZE));std::vector<unsigned char> v((bytes+page-1)/page);
    if(mincore(const_cast<void*>(p),bytes,v.data()))std::abort();
    std::size_t n=0;for(auto c:v)n+=c&1;return n;}
int main(){
    {ZeroTable<std::uint32_t> t(1u<<20);
     assert(t.size()==(1u<<20));
     for(std::size_t i=0;i<t.size();i+=4093)assert(t[i]==0);
     assert(t[(1u<<20)-1]==0);
     t[0]=7;t[12345]=0xdeadbeef;t[(1u<<20)-1]=1;
     ZeroTable<std::uint32_t> c(t);
     assert(c[0]==7 && c[12345]==0xdeadbeef && c[(1u<<20)-1]==1 && c[1]==0);
     c[0]=9;assert(t[0]==7);
     ZeroTable<std::uint32_t> m(std::move(c));
     assert(c.size()==0 && m.size()==(1u<<20) && m[0]==9 && m[12345]==0xdeadbeef);
     t=m;assert(t[0]==9 && m[0]==9);
     std::fill(t.begin(),t.end(),0);
     for(std::size_t i=0;i<t.size();i+=511)assert(t[i]==0);
     assert(t[12345]==0 && t[(1u<<20)-1]==0);}
    {PpuState s;
     const auto& table=s.memory.page_regions;
     const auto resident=resident_pages(&table[0],table.size()*sizeof(table[0]));
     std::printf("resident_pages=%zu of %zu\n",resident,table.size()*sizeof(table[0])/std::size_t(sysconf(_SC_PAGESIZE)));
     assert(resident<16);}
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class ZeroTableTest(unittest.TestCase):
    def test_contract_and_construction_cost(self):
        payload = struct.pack('>I', 0x4e800020)
        image = make_image([(0x10000, 5, payload), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True, extra_entries=(0x10000,)).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source + PROGRAM)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
                                    str(root/'test.cpp'), '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=300,
                                 env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
            self.assertEqual(run.returncode, 0, run.stderr[-4000:] + run.stdout[-2000:])
            self.assertIn('ok', run.stdout)


if __name__ == '__main__':
    unittest.main()
