"""RSX big-endian index decode and index extent, scalar and SSE4.1 builds.

Literal cases are worked out by hand; random cases compare against a direct
per-element reading of the big-endian bytes (the format definition).
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
#include <random>
static void literal_(){
 // BE16 0x1234, restart 0xffff, 0x0001 with base 10.
 const std::uint8_t a[]={0x12,0x34,0xff,0xff,0x00,0x01};std::uint32_t out[3];std::uint32_t largest=7;bool any=false;
 rsxdraw::decode_indices(a,out,3,2,true,0xffff,10,largest,any);
 assert(out[0]==0x1234+10 && out[1]==UINT32_MAX && out[2]==11 && largest==0x1234 && any);
 // Without restart 0xffff is an ordinary index.
 rsxdraw::decode_indices(a,out,3,2,false,0xffff,0,largest,any);assert(out[1]==0xffff && largest==0xffff && any);
 // BE32, all restart: nothing counts for the overflow check.
 const std::uint8_t b[]={0xde,0xad,0xbe,0xef,0xde,0xad,0xbe,0xef,0xde,0xad,0xbe,0xef,0xde,0xad,0xbe,0xef,0xde,0xad,0xbe,0xef};std::uint32_t o5[5];
 rsxdraw::decode_indices(b,o5,5,4,true,0xdeadbeef,3,largest,any);for(auto v:o5)assert(v==UINT32_MAX);assert(!any && largest==0);
 rsxdraw::decode_indices(b,o5,5,4,false,0,3,largest,any);for(auto v:o5)assert(v==0xdeadbef2u);assert(any && largest==0xdeadbeef);
 // Base addition wraps modulo 2^32 exactly like the scalar decoder (the caller rejects overflow).
 const std::uint8_t c[]={0xff,0xff,0xff,0xfe};std::uint32_t one;rsxdraw::decode_indices(c,&one,1,4,false,0,5,largest,any);assert(one==3 && largest==0xfffffffe);
 // Extent of the unrestarted values (BE16, 17 values: one 16-lane block and a scalar tail), restart lanes excluded at both ends.
 std::uint8_t e[34];std::uint32_t o17[17],smallest=0;for(unsigned i=0;i<17;++i){const unsigned v=i==3?0xffff:i==9?0:i==16?0x8000:100+i;e[2*i]=std::uint8_t(v>>8);e[2*i+1]=std::uint8_t(v);}
 rsxdraw::decode_indices(e,o17,17,2,true,0xffff,0,largest,smallest,any);assert(any && smallest==0 && largest==0x8000 && o17[3]==UINT32_MAX && o17[16]==0x8000);
 rsxdraw::decode_indices(e,o17,17,2,true,0,0,largest,smallest,any);assert(any && smallest==100 && largest==0xffff && o17[9]==UINT32_MAX);
 std::vector<std::array<std::uint32_t,3>> tris{{9,4,7},{12,3,8},{5,5,5}};unsigned lo=UINT_MAX,hi=0;rsxdraw::index_extent(tris.data(),tris.size(),lo,hi);assert(lo==3 && hi==12);
 lo=UINT_MAX;hi=0;rsxdraw::index_extent(tris.data(),0,lo,hi);assert(lo==UINT_MAX && hi==0);
 std::puts("PASS literal BE16/BE32 decode, restart, wrap and extent");
}
static void random_(){
 std::mt19937 rng(7);
 for(unsigned n=0;n<20000;++n){
  const unsigned width=rng()&1?2:4,count=rng()%(n%4?70:300);const bool restart=rng()&1;
  std::vector<std::uint8_t> bytes(count*width);for(auto& v:bytes)v=std::uint8_t(rng()%5==0?0xff:rng());
  const std::uint32_t restart_index=width==2?0xffff:(rng()&1?0xffffffffu:0xffffff00u|(rng()&255));
  const std::uint32_t base=rng()%3==0?0:rng();
  std::vector<std::uint32_t> out(count+1,0x5a5a5a5a),expect(count+1,0x5a5a5a5a);std::uint32_t largest=0,smallest=0;bool any=false,expect_any=false;std::uint32_t expect_largest=0,expect_smallest=UINT32_MAX;
  for(unsigned i=0;i<count;++i){std::uint32_t raw=0;for(unsigned k=0;k<width;++k)raw=(raw<<8)|bytes[i*width+k];
   if(restart && raw==restart_index)expect[i]=UINT32_MAX;else{expect[i]=raw+base;expect_any=true;expect_largest=std::max(expect_largest,raw);expect_smallest=std::min(expect_smallest,raw);}}
  rsxdraw::decode_indices(bytes.data(),out.data(),count,width,restart,restart_index,base,largest,any);
  assert(out==expect && any==expect_any && (!any || largest==expect_largest));
  std::fill(out.begin(),out.end(),0x5a5a5a5au);any=!expect_any;
  rsxdraw::decode_indices(bytes.data(),out.data(),count,width,restart,restart_index,base,largest,smallest,any);
  assert(out==expect && any==expect_any && (!any || (largest==expect_largest && smallest==expect_smallest)));
  std::vector<std::array<std::uint32_t,3>> tris(rng()%40);unsigned lo=UINT_MAX,hi=0,elo=UINT_MAX,ehi=0;
  for(auto& t:tris)for(auto& v:t){v=rng();elo=std::min(elo,v);ehi=std::max(ehi,v);}
  rsxdraw::index_extent(tris.data(),tris.size(),lo,hi);assert(lo==elo && hi==ehi);
 }
 std::puts("PASS random decode and extent equal the per-element definition");
}
int main(){try{literal_();random_();
#if defined(__AVX2__)
 std::puts("simd=2");
#elif defined(__SSSE3__) && defined(__SSE4_1__)
 std::puts("simd=1");
#else
 std::puts("simd=0");
#endif
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_flags():
    try:
        with open('/proc/cpuinfo') as f:
            flags = f.read()
    except OSError:
        return None
    return ['-march=native'] if ' sse4_1' in flags and ' ssse3' in flags else None


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class RsxIndexDecode(unittest.TestCase):
    def run_variant(self, flags):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cpp = root/'test.cpp'
            cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', *flags,
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(cpp), *LIBRARIES, '-o', str(root/'test')],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=120, env=dict(os.environ))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(run.stdout.count('PASS '), 2)
            return run.stdout

    def test_baseline(self):
        self.assertIn('simd=0', self.run_variant([]))

    def test_host_isa(self):
        flags = native_flags()
        if not flags:
            self.skipTest('host lacks SSE4.1')
        self.assertRegex(self.run_variant(flags), 'simd=[12]')

    def test_sse41(self):
        if not native_flags():
            self.skipTest('host lacks SSE4.1')
        self.assertIn('simd=1', self.run_variant(['-mssse3', '-msse4.1']))

    def test_avx2(self):
        flags = native_flags()
        with open('/proc/cpuinfo') as f:
            avx2 = ' avx2' in f.read()
        if not flags or not avx2:
            self.skipTest('host lacks AVX2')
        self.assertIn('simd=2', self.run_variant(['-mssse3', '-msse4.1', '-mavx2']))
