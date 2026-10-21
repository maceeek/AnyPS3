"""A concurrent one-page guest read that continues into a second mapping (GuestMemory::transfer -> read_admitted) resolves every
source mapping and its read permission before the first destination byte is written (review-spuput-1 finding 2).

Independent expectations: a sentinel-filled destination stays byte-for-byte unchanged when the second part is a hole or a write-only
mapping (the serial path, which validates the whole span first, behaves the same); a readable split returns the big-endian words the
test stored itself.
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

MAIN = r'''
#include <cassert>
static void check(bool concurrent){
    for(unsigned second_flags:{0u,2u,6u}){
        PpuState s;auto& m=s.memory;
        m.map(0x20000,8,6,nullptr,0);if(second_flags)m.map(0x20008,8,second_flags,nullptr,0);
        m.store(0x20000,4,0x01020304);m.store(0x20004,4,0x05060708);
        if(second_flags==6){m.store(0x20008,4,0x090a0b0c);m.store(0x2000c,4,0x0d0e0f10);}
        if(concurrent)m.enable_concurrency();
        std::uint8_t buffer[24];std::memset(buffer,0xcc,sizeof(buffer));
        bool fault=false;
        try{m.transfer(0x20000,buffer+4,16,false);}catch(const std::runtime_error&){fault=true;}
        if(second_flags!=6){
            assert(fault);
            for(unsigned i=0;i<sizeof(buffer);++i)if(buffer[i]!=0xcc){std::fprintf(stderr,"concurrent=%d flags=%u: destination byte %u written before the fault\n",concurrent,second_flags,i);std::abort();}
        }else{
            assert(!fault);
            for(unsigned i=0;i<16;++i)assert(buffer[4+i]==i+1);
            for(unsigned i:{0u,1u,2u,3u,20u,21u,22u,23u})assert(buffer[i]==0xcc);
        }
    }
}
int main(){check(false);check(true);std::puts("ok");}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SplitOnePageRead(unittest.TestCase):
    def test_fault_leaves_destination_untouched(self):
        payload = struct.pack('>I', 0x4e800020)
        image = make_image([(0x10000, 5, payload), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True, extra_entries=(0x10000,)).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', str(root/'test.cpp'),
                                    '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=120,
                                 env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
            self.assertEqual(run.returncode, 0, run.stderr[-4000:] + run.stdout[-2000:])
            self.assertIn('ok', run.stdout)


if __name__ == '__main__':
    unittest.main()
