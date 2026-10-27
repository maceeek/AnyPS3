"""GuestMemory::atomic_read / atomic_write (the bulk copy under transfer(), SPU DMA, PPU medium accesses and RSX reads) copy exactly
n bytes for every source/destination misalignment 0..15 and every size 0..300 (head bytes, unrolled 32-byte word body, 8-byte words,
tail bytes), leave every byte outside the destination span untouched, and agree with a plain byte-by-byte copy. The expectation is
a byte loop over a pattern, independent of the word/unroll structure; GuestMemory::transfer is also checked across a page split."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_par1 import source

BODY = r'''
 std::vector<std::uint8_t> src(400),dst(400),want(400);
 unsigned checks=0;
 for(unsigned so=0;so<16;++so)for(unsigned dof=0;dof<16;++dof)for(unsigned n=0;n<=300;++n){
  for(unsigned i=0;i<400;++i){src[i]=std::uint8_t(i*37+so*11+n);dst[i]=std::uint8_t(0xa5^i);}
  want=dst;for(unsigned i=0;i<n;++i)want[dof+i]=src[so+i];
  GuestMemory::atomic_read(src.data()+so,dst.data()+dof,n);assert(dst==want);
  for(unsigned i=0;i<400;++i){dst[i]=std::uint8_t(0x5a^(i*3));}
  want=dst;for(unsigned i=0;i<n;++i)want[dof+i]=src[so+i];
  GuestMemory::atomic_write(dst.data()+dof,src.data()+so,n);assert(dst==want);
  ++checks;
 }
 // Through the concurrent transfer path: a span that crosses a page boundary and a one-page span, read and write.
 GuestMemory m;m.map(0x10000,3*4096,6,nullptr,0);m.enable_concurrency();
 std::vector<std::uint8_t> in(3*4096),out(3*4096);for(unsigned i=0;i<3*4096;++i)in[i]=std::uint8_t(i*13+7);
 for(const auto& [at,n]:std::vector<std::pair<std::uint64_t,unsigned>>{{0x10ff3,4100},{0x11001,1000},{0x10000,3*4096-1},{0x12ffd,3}}){
  m.transfer(at,in.data(),n,true);std::fill(out.begin(),out.end(),0);m.transfer(at,out.data(),n,false);
  for(unsigned i=0;i<n;++i){assert(out[i]==in[i]);}
  for(unsigned i=n;i<3*4096;++i){assert(out[i]==0);}
  for(unsigned i=0;i<n;++i){assert(m.load(at+i,1)==in[i]);}
 }
 std::printf("PASS atomic copies equal byte copies (%u cases)\n",checks);
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class AtomicCopyUnrolledTests(unittest.TestCase):
    def test_all_alignments_and_sizes(self):
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / 't.cpp'
            cpp.write_text(source(BODY).replace('#include <cassert>', '#include <cassert>\n#include <vector>\n#include <utility>', 1))
            for opt in ('-O0', '-O2'):
                p = subprocess.run(['c++', '-std=c++17', opt, '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))],
                                   capture_output=True, text=True, timeout=600)
                self.assertEqual(p.returncode, 0, p.stderr[-8000:])
                p = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, text=True, timeout=300,
                                   env={**os.environ, 'SDL_AUDIODRIVER': 'dummy'})
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn('PASS atomic copies', p.stdout)


if __name__ == '__main__':
    unittest.main()
