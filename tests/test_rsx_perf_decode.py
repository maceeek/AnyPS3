"""FP preparation and exact numeric fast paths, against unprepared execution."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program

MAIN = r'''
#include <cassert>
using namespace rsxdraw;
static float old_half(unsigned h){
 const unsigned sign=h>>15,exp=(h>>10)&31,frac=h&1023;
 float v;
 if(!exp)v=std::ldexp(float(frac),-24);
 else if(exp==31)v=frac?std::numeric_limits<float>::quiet_NaN():std::numeric_limits<float>::infinity();
 else v=std::ldexp(float(frac|1024),int(exp)-25);
 return sign?-v:v;
}
static unsigned seed=0xbabecafe;
static unsigned rng(){seed^=seed<<13;seed^=seed>>17;seed^=seed<<5;return seed;}
static bool same(const FpContext& a,const FpContext& b){
 return a.r==b.r && !std::memcmp(a.cc.data(),b.cc.data(),sizeof(a.cc)) && a.killed==b.killed;
}
static void operands(){
 FpContext ctx;
 for(auto& r:ctx.r)for(auto& c:r)c=rng();
 for(auto& v:ctx.inputs)for(auto& c:v)c=bits_float(rng());
 // Every source type/register/precision, random swizzle and both modifiers.
 for(unsigned n=0;n<3;++n)for(unsigned type=0;type<4;++type)for(unsigned index=0;index<64;++index)
 for(unsigned precision=0;precision<8;++precision)for(unsigned half=0;half<2;++half){
   FpInst i{};i.d=(index%16)<<13;i.s2=0;
   const unsigned raw=type|(index<<2)|(half<<8)|((rng()&255)<<9)|((rng()&1)<<17)|((rng()&1)<<(n?18:29));
   i.s1=precision<<(19+3*n);
   if(n==0)i.s0=raw;else if(n==1)i.s1|=raw;else i.s2=raw;
   if(index&1)i.s2|=1u<<30;
   for(auto& c:i.constant)c=bits_float(rng());
   Vec4 expected{},actual{};std::string e,a;
   try{expected=fp_source_raw(i,n,ctx);}catch(const std::exception& x){e=x.what();}
   prepare_fragment_instruction(i);
   try{actual=fp_source(i,n,ctx);}catch(const std::exception& x){a=x.what();}
   assert(e==a);if(e.empty())assert(!std::memcmp(expected.data(),actual.data(),16));
 }
}
static void instructions(){
 const unsigned ops[]={0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,26,27,28,29,30,
  31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,54,56,57,58,59,60,61,62,63,127};
 for(unsigned round=0;round<8;++round)for(unsigned op:ops){
   std::array<FpContext,4> a{},b{};
   for(auto& ctx:a){for(auto& r:ctx.r)for(auto& c:r)c=(rng()&0x80000000u)|0x3f000000u|(rng()&0x7fffffu);
     ctx.cc[0]={-1,0,1,bits_float(0x7fc00000)};ctx.cc[1]={0,1,-1,0};}
   b=a;
   FpInst i{};i.d=((op&63)<<24)|(15u<<9)|((round&1)?128u:0u)|1u;
   i.s0=(0xe4u<<9)|(7u<<18);i.s1=(0xe4u<<9)|((op>>6)<<31);i.s2=0xe4u<<9;
   if(round==1)i.d|=1u<<8; // CC and half aliases
   if(round==2)i.d|=1u<<31; // saturation
   if(round==3)i.s1|=2u<<28; // result scale
   if(round==4)i.s0=(i.s0&~(7u<<18))|(3u<<18); // predicate
   if(round==5)i.d|=2u<<22; // result precision
   if(round==6)i.s1|=1u<<19; // source precision
   if(round==7)i.d=(i.d&~(15u<<9))|(5u<<9); // mask
   std::string e,f;
   try{run_fragment_quad_linear({i},a);}catch(const std::exception& x){e=x.what();}
   prepare_fragment_instruction(i);
   try{run_fragment_quad_linear({i},b);}catch(const std::exception& x){f=x.what();}
   assert(e==f);for(unsigned lane=0;lane<4;++lane)assert(same(a[lane],b[lane]));
 }
 // Multiple instructions retain R/H aliasing and helper lanes after KIL.
 for(unsigned n=0;n<128;++n){
   std::vector<FpInst> raw,prepared;std::array<FpContext,4> a{},b{};
   for(unsigned op:{1u,2u,3u,1u,18u,3u}){
     FpInst i{};i.d=(op<<24)|(15u<<9)|((n&1)?128u:0u);i.s0=2u|(0xe4u<<9)|(7u<<18);i.s1=2u|(0xe4u<<9);
     for(auto& c:i.constant)c=bits_float((rng()&0x80000000u)|0x3f000000u|(rng()&0x7fffffu));
     raw.push_back(i);prepare_fragment_instruction(i);prepared.push_back(i);
   }
   run_fragment_quad(raw,a);run_fragment_quad(prepared,b);
   for(unsigned lane=0;lane<4;++lane)assert(same(a[lane],b[lane]));
 }
}
static void special_lanes(){
 const unsigned patterns[]={0,0x80000000u,1,0x80000001u,0x007fffffu,0x00800000u,0x3f800000u,0xbf800000u,
   0x7f7fffffu,0xff7fffffu,0x7f800000u,0xff800000u,0x7fc00001u,0x7fc02000u,0xffc01234u,0x7f800001u,0xff800002u};
 for(unsigned op:{1u,2u,3u})for(unsigned x:patterns)for(unsigned y:patterns){
   std::array<FpContext,4> a{},b{};
   for(auto& ctx:a){ctx.r[1]={x,y,x,y};ctx.r[2]={y,x,y,x};}b=a;
   FpInst i{};i.d=(op<<24)|(15u<<9)|1u;i.s0=4u|(0xe4u<<9)|(7u<<18);i.s1=8u|(0xe4u<<9);
   run_fragment_quad_linear({i},a);prepare_fragment_instruction(i);run_fragment_quad_linear({i},b);
   for(unsigned l=0;l<4;++l)assert(same(a[l],b[l]));
 }
}
static void cache(){
 FpInst i{};i.d=(1u<<24)|(15u<<9)|1;i.s0=2u|(0xe4u<<9)|(7u<<18);i.has_constant=true;
 i.constant={1,2,3,4};auto a=prepared_fragment_program({i});
 i.constant[0]=2;auto b=prepared_fragment_program({i});assert(a[0].constant[0]==1 && b[0].constant[0]==2);
 i.constant[0]=bits_float(0x80000000);auto c=prepared_fragment_program({i});
 i.constant[0]=0;auto d=prepared_fragment_program({i});assert(float_bits(c[0].constant[0])==0x80000000 && float_bits(d[0].constant[0])==0);
 for(unsigned n=0;n<80;++n){i.d=(2u<<24)|(15u<<9)|1;i.constant[0]=float(n);prepared_fragment_program({i});}
 assert(prepared_fragment_program({a[0]})[0].constant[0]==1);
}
int main(){
 assert(float_bits(half_float(0x0001))==0x33800000);assert(float_bits(half_float(0x03ff))==0x387fc000);
 assert(float_bits(half_float(0x7bff))==0x477fe000);assert(float_bits(half_float(0xfe01))==0xffc00000);
 for(const int mode:{FE_TONEAREST,FE_DOWNWARD,FE_UPWARD,FE_TOWARDZERO}){
   assert(!std::fesetround(mode));
   for(unsigned h=0;h<65536;++h)assert(float_bits(half_float(h))==float_bits(old_half(h)));
   operands();instructions();special_lanes();cache();
 }
 std::puts("PASS half exhaustive, lazy operands/errors, SIMD, predicates, modifiers, aliases and byte-keyed cache in four rounding modes");
}
'''

@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class RsxPerfDecodeTests(unittest.TestCase):
    def test_native_decode_and_fast_paths(self):
        source=compile_program(ElfImage.parse(program(BLR)),process_entry=True).cpp()
        source=source.replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')+MAIN
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);file=root/'decode.cpp';file.write_text(source)
            build=subprocess.run(['c++','-std=c++17','-O2','-pthread','-Wall','-Wextra','-Werror',str(file),'-o',str(root/'decode')],capture_output=True,text=True,timeout=120)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(root/'decode')],capture_output=True,text=True,timeout=120,env=dict(os.environ,ANYPS3_RSX_THREADS='1'))
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn('PASS',run.stdout)
