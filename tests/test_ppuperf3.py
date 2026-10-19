"""Guarded PPU host arithmetic versus the unchanged integer path.

The native loop compares complete FPSCR and binary64 FPR bits for >20 million
operations. Literal vectors independently cover rounding, double rounding,
sticky FX/XX, exceptional operands, denormals and enabled-exception stops.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.floating import FLOAT_CPP


HEAD = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <stdexcept>
#if defined(__SSE2__)
#include <xmmintrin.h>
#endif
struct PpuState { std::uint32_t fpscr=0,cr=0; };
'''


def helpers():
    return HEAD + '\nnamespace fast {\n' + FLOAT_CPP + r'''
}
#define ANYPS3_FP_FORCE_SOFT 1
namespace soft {
''' + FLOAT_CPP + '\n}\n#undef ANYPS3_FP_FORCE_SOFT\n'


@unittest.skipUnless(shutil.which('c++'), 'Native PPU FP requires c++.')
class PpuPerf3Tests(unittest.TestCase):
    def native(self, body, extra=(), timeout=120):
        with tempfile.TemporaryDirectory(prefix='anyps3-ppuperf3-') as directory:
            cpp, exe = Path(directory)/'test.cpp', Path(directory)/'test'
            cpp.write_text(helpers()+body)
            build = subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror',
                                    '-ffp-contract=off',*extra,str(cpp),'-o',str(exe)],
                                   capture_output=True,text=True,timeout=60)
            self.assertEqual(build.returncode,0,build.stderr)
            run = subprocess.run([str(exe)],capture_output=True,text=True,timeout=timeout)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            return run.stdout

    def test_literal_results_fi_fr_sticky_denormals_and_single_double_rounding(self):
        self.native(r'''
int main(){
 PpuState s;
 s.fpscr=0;assert(fast::fp_add(s,0x3ff0000000000000,0x3ca0000000000000)==0x3ff0000000000000 && s.fpscr==0x82024000);
 s.fpscr=0;assert(fast::fp_add(s,0x3ff0000000000000,0x3cb8000000000000)==0x3ff0000000000002 && s.fpscr==0x82064000);
 s.fpscr=0;assert(fast::fp_add(s,0xbff0000000000000,0xbcb8000000000000)==0xbff0000000000002 && s.fpscr==0x82068000);
 s.fpscr=0x02060000;assert(fast::fp_add(s,0x3ff0000000000000,0x3ca0000000000000)==0x3ff0000000000000 && s.fpscr==0x02024000);
 s.fpscr=0x82060000;assert(fast::fp_add(s,0x3ff0000000000000,0x3ff0000000000000)==0x4000000000000000 && s.fpscr==0x82004000);
 s.fpscr=0;assert(fast::fp_divide(s,0x3ff0000000000000,0x4008000000000000)==0x3fd5555555555555 && s.fpscr==0x82024000);
 s.fpscr=0;assert(fast::fp_divide(s,0x4000000000000000,0x4008000000000000,24)==0x3fe5555560000000 && s.fpscr==0x82064000);
 s.fpscr=0;assert(fast::fp_sqrt(s,0x4000000000000000)==0x3ff6a09e667f3bcd && s.fpscr==0x82064000);
 s.fpscr=0;assert(fast::fp_sqrt(s,0x4000000000000000,24)==0x3ff6a09e60000000 && s.fpscr==0x82024000);
 // (1+2^-52)^2 rounds down, but (1-2^-53)^2 rounds down in magnitude too.
 s.fpscr=0;assert(fast::fp_multiply(s,0x3ff0000000000001,0x3ff0000000000001)==0x3ff0000000000002 && s.fpscr==0x82024000);
 // Exact double cancellation of the high product leaves -2^-104, not zero.
 s.fpscr=0;assert(fast::fp_fused(s,0x3ff0000000000001,0x3feffffffffffffe,0xbff0000000000000)==0xb970000000000000 && s.fpscr==0x8000);
 // Double midpoint +/- 2^-80: converting the rounded double to float is wrong.
 s.fpscr=0;assert(fast::fp_add(s,0x3ff0000010000000,0x3af0000000000000,24)==0x3ff0000020000000 && s.fpscr==0x82064000);
 s.fpscr=0;assert(fast::fp_add(s,0x3ff0000010000000,0xbaf0000000000000,24)==0x3ff0000000000000 && s.fpscr==0x82024000);
 s.fpscr=0;assert(fast::fp_fused(s,0x3ff0000010000000,0x3ff0000000000000,0x3af0000000000000,24)==0x3ff0000020000000 && s.fpscr==0x82064000);
 s.fpscr=0;assert(fast::fp_fused(s,0x3ff0000010000000,0x3ff0000000000000,0xbaf0000000000000,24)==0x3ff0000000000000 && s.fpscr==0x82024000);
 // FPRF classifies widened binary32 subnormals as single subnormals.
 s.fpscr=0;assert(fast::fp_multiply(s,0x3810000000000000,0x3fe0000000000000,24)==0x3800000000000000 && s.fpscr==0x14000);
 s.fpscr=0;assert(fast::fp_multiply(s,0x0010000000000000,0x3fefffffffffffff)==0x0010000000000000 && s.fpscr==0x8a064000);
 s.fpscr=0;assert(fast::fp_add(s,1,1)==2 && s.fpscr==0x14000);
 s.fpscr=0;assert(fast::fp_sqrt(s,0x8000000000000000)==0x8000000000000000 && s.fpscr==0x12000);
 s.fpscr=0;assert(fast::fp_divide(s,0x3ff0000000000000,0)==0x7ff0000000000000 && s.fpscr==0x84005000);
 s.fpscr=0;assert(fast::fp_add(s,0x7ff0000000000001,0x3ff0000000000000)==0x7ff8000000000001 && s.fpscr==0xa1011000);
 for(unsigned mode:{8u,16u,32u,64u,128u}){
  s.fpscr=mode;bool stopped=false;
  try{fast::fp_add(s,0x3ff0000000000000,0x4000000000000000);}catch(const std::runtime_error&){stopped=true;}
  assert(stopped && s.fpscr==mode);
 }
}
''')

    def test_millions_of_full_status_differential_operations(self):
        body = r'''
static std::uint64_t random_bits(){
 static std::uint64_t v=0x68e5af639f401d27;v^=v<<13;v^=v>>7;v^=v<<17;return v;
}
static std::uint64_t operand(unsigned kind){
 auto r=random_bits();
 switch(kind%8){
 case 0:return r; // full binary64 space, including all exceptional encodings
 case 1:return (r&0x800fffffffffffff)|((623+(r>>32)%801)<<52);
 case 2:return (r&0x800fffffffffffff)|((993+(r>>32)%61)<<52);
 case 3:return soft::fp_widen(static_cast<std::uint32_t>(r));
 case 4:return r&0x8000000000000000;
 case 5:{const std::uint64_t edges[]={0,1,0xfffffffffffff,0x10000000000000,0x7fefffffffffffff,
           0x7ff0000000000000,0x7ff8000000000001,0x7ff0000000000001,
           0x3810000000000000,0x47efffffe0000000,0x3ff0000010000000};
          return edges[(r>>32)%11]^(r&0x8000000000000000);}
 case 6:return (r&0x800fffffffffffff)|((873+(r>>32)%279)<<52);
 default:return (r&0x800fffffffffffff)|((621+(r>>32)%805)<<52); // guard boundaries
 }
}
int main(){
 std::uint64_t comparisons=0,hits[5]={};
 for(unsigned i=0;i<2000000;++i){
  auto a=operand(i/251),b=operand(i/251),c=operand(i/251);
  if(i%17==0)b=a^0x8000000000000000; // cancellation
  if(i%19==0)c=0x3ff0000000000000; // fused == add
  std::uint32_t initial=static_cast<std::uint32_t>(random_bits())&~0xfbu;
  if(i%7==0)initial|=i&3; // all guest rounding modes
  for(unsigned precision:{24u,53u})for(unsigned operation=0;operation<6;++operation){
   PpuState f,s;f.fpscr=s.fpscr=initial;
   std::uint64_t x=0,y=0;
   switch(operation){
   case 0:x=fast::fp_add(f,a,b,precision);y=soft::fp_add(s,a,b,precision);break;
   case 1:x=fast::fp_multiply(f,a,b,precision);y=soft::fp_multiply(s,a,b,precision);break;
   case 2:x=fast::fp_divide(f,a,b,precision);y=soft::fp_divide(s,a,b,precision);break;
   case 3:x=fast::fp_sqrt(f,a,precision);y=soft::fp_sqrt(s,a,precision);break;
   case 4:x=fast::fp_fused(f,a,c,b,precision);y=soft::fp_fused(s,a,c,b,precision);break;
   case 5:x=fast::fp_fused(f,a,c,b,precision,true);y=soft::fp_fused(s,a,c,b,precision,true);break;
   }
   if(x!=y || f.fpscr!=s.fpscr){
    std::fprintf(stderr,"i=%u op=%u p=%u a=%016llx b=%016llx c=%016llx initial=%08x fast=%016llx/%08x soft=%016llx/%08x\n",
      i,operation,precision,(unsigned long long)a,(unsigned long long)b,(unsigned long long)c,initial,
      (unsigned long long)x,f.fpscr,(unsigned long long)y,s.fpscr);return 1;
   }
   ++comparisons;
  }
  if(i%251==0 && !(initial&0xfb))for(unsigned op=0;op<5;++op){
   PpuState p;p.fpscr=initial;std::uint64_t out=0;
   hits[op]+=fast::fp_host_arithmetic(p,a,op==3?0:b,op==4?c:0,53,op,out);
  }
 }
#if defined(__SSE2__) && FLT_EVAL_METHOD == 0 && !defined(__FAST_MATH__)
 for(auto count:hits)assert(count>100);
#endif
 std::printf("comparisons=%llu hits=%llu,%llu,%llu,%llu,%llu\n",(unsigned long long)comparisons,
   (unsigned long long)hits[0],(unsigned long long)hits[1],(unsigned long long)hits[2],(unsigned long long)hits[3],(unsigned long long)hits[4]);
}
'''
        for extra in ((), ('-march=native',), ('-march=native','-ffp-contract=fast')):
            with self.subTest(flags=extra):
                output=self.native(body,extra=extra)
                self.assertIn('comparisons=24000000',output)

    def test_host_mode_changes_reject_without_guest_mutation(self):
        self.native(r'''
int main(){
#if defined(__SSE2__) && FLT_EVAL_METHOD == 0 && !defined(__FAST_MATH__)
 const auto original=_mm_getcsr();
 for(unsigned csr:{0x1f80u|0x8000u,0x1f80u|0x40u,0x1f80u|0x2000u,
                   0x1f80u|0x4000u,0x1f80u|0x6000u,0x1f80u&~0x80u}){
  _mm_setcsr(csr);
  PpuState s;s.fpscr=0x12340000;std::uint64_t out=123;
  const bool accepted=fast::fp_host_arithmetic(s,0x3ff0000000000000,0x4000000000000000,0,53,0,out);
  const auto r=fast::fp_add(s,0x3ff0000000000000,0x4000000000000000);
  _mm_setcsr(original);
  assert(!accepted && out==123 && r==0x4008000000000000 && s.fpscr==0x12304000);
 }
 _mm_setcsr(0x1fbf); // existing host sticky flags are harmless
 PpuState s;std::uint64_t out=0;
 const bool accepted=fast::fp_host_arithmetic(s,0x3ff0000000000000,0x4000000000000000,0,53,0,out);
 _mm_setcsr(original);assert(accepted && out==0x4008000000000000 && s.fpscr==0x4000);
 for(unsigned guest:{1u,2u,3u,8u,16u,32u,64u,128u}){
  s.fpscr=guest;out=123;
  assert(!fast::fp_host_arithmetic(s,0x3ff0000000000000,0x4000000000000000,0,53,0,out));
  assert(s.fpscr==guest && out==123);
 }
#endif
}
''', extra=('-fsanitize=undefined','-fno-sanitize-recover=all'))

    def test_fast_math_build_disables_host_path(self):
        self.native(r'''
int main(){PpuState s;std::uint64_t out=123;
 assert(!fast::fp_host_arithmetic(s,0x3ff0000000000000,0x4000000000000000,0,53,0,out));
 assert(out==123 && s.fpscr==0);
 assert(fast::fp_add(s,0x3ff0000000000000,0x4000000000000000)==0x4008000000000000 && s.fpscr==0x4000);
}
''', extra=('-ffast-math',))
