"""VMX host SIMD paths (vector.py) against the exact per-lane implementation and literal values.

Literal expectations are computed by hand from IEEE binary32 (comments give the
derivation); the differential part compares every SIMD helper bit for bit with
the exact lane functions (vmx_add/vmx_fused/...) and plain byte loops, under
baseline SSE2 and under the host ISA (SSSE3/FMA paths).
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
#include <random>
static VmxReg words(std::uint32_t a,std::uint32_t b,std::uint32_t c,std::uint32_t d){VmxReg r{};vmx_word(r,0,a);vmx_word(r,1,b);vmx_word(r,2,c);vmx_word(r,3,d);return r;}
static void expect(const VmxReg& r,std::uint32_t a,std::uint32_t b,std::uint32_t c,std::uint32_t d,const char* what){
    const std::uint32_t e[4]={a,b,c,d};
    for(unsigned i=0;i<4;++i)if(vmx_word(r,i)!=e[i]){std::fprintf(stderr,"%s lane %u: %08x != %08x\n",what,i,vmx_word(r,i),e[i]);std::abort();}
}
static std::mt19937_64 rng(12345);
static std::uint32_t lane(){
    const std::uint32_t sign=std::uint32_t(rng()&1)<<31,raw=std::uint32_t(rng());
    switch(rng()%12){
    case 0:return sign;                                          // +-0
    case 1:return sign|(raw&0x7fffff);                           // denormal
    case 2:return sign|0x7f800000;                               // infinity
    case 3:return sign|0x7f800000|((raw&0x7fffff)|1);             // NaN (quiet or signaling)
    case 4:return sign|(0x7e000000u+(raw&0x00ffffff));            // near the binary32 maximum
    case 5:return sign|(0x00800000u+(raw&0x00ffffff));            // smallest normals
    case 6:return sign|(0x3f800000u+(raw&0x007fffff));            // [1,2)
    case 7:{const float f=float(std::int32_t(raw)%4096);std::uint32_t u;std::memcpy(&u,&f,4);return u;}
    default:return raw;
    }
}
static VmxReg random_reg(){return words(lane(),lane(),lane(),lane());}
static bool same(const VmxReg& a,const VmxReg& b){return a==b;}
int main(){
    PpuState s;
    // ---- literal IEEE expectations -----------------------------------------
    VmxReg d;
    vmx_float_op(s,d,words(0x3fc00000,0x00800000,0x7f7fffff,0x3f800000),words(0x40100000,0x80400000,0x7f7fffff,0xbf800000),VmxReg{},0);
    // 1.5+2.25=3.75; min normal - 2^-127 = 2^-127 (denormal); max+max overflows to +inf (RN); 1-1=+0.
    expect(d,0x40700000,0x00400000,0x7f800000,0x00000000,"vaddfp");
    vmx_float_op(s,d,words(0x40400000,0x80000000,0x3f800000,0x00000001),words(0x3f800000,0x80000000,0x3f800000,0x00000001),VmxReg{},1);
    // 3-1=2; -0-(-0)=+0; 1-1=+0; denorm-denorm=+0.
    expect(d,0x40000000,0x00000000,0x00000000,0x00000000,"vsubfp");
    // vmaddfp is fused: (1+2^-12)^2 - (1+2^-11) = 2^-24 exactly (an unfused product rounds to 1+2^-11 and gives 0).
    // 2*3+(-6)=+0; 2*3+1=7; denormal*2+0 = 2^-148 (0x00000002).
    vmx_float_op(s,d,words(0x3f800800,0x40000000,0x40000000,0x00000001),words(0xbf801000,0xc0c00000,0x3f800000,0x00000000),words(0x3f800800,0x40400000,0x40400000,0x40000000),2);
    expect(d,0x33800000,0x00000000,0x40e00000,0x00000002,"vmaddfp");
    // vnmsubfp -(a*c-b): -(6-6)=-0; -(2*3-1)=-5; -((1+2^-12)^2-(1+2^-11)) = -2^-24.
    vmx_float_op(s,d,words(0x40000000,0x40000000,0x3f800800,0),words(0x40c00000,0x3f800000,0x3f801000,0),words(0x40400000,0x40400000,0x3f800800,0),3);
    expect(d,0x80000000,0xc0a00000,0xb3800000,0x80000000,"vnmsubfp");
    // NaN operands keep the exact path's precedence (vA, then vB, then vC) and quieting.
    vmx_float_op(s,d,words(0x7f800001,0x3f800000,0x3f800000,0x7f800000),words(0x7fc00002,0x7f800003,0x3f800000,0xff800000),words(0x3f800000,0x3f800000,0x7f800004,0x3f800000),2);
    expect(d,0x7fc00001,0x7fc00003,0x7fc00004,0x7fc00000,"vmaddfp NaN/inf");
    vmx_estimate_op(s,d,words(0x40000000,0x40400000,0x80000000,0x7f800000),false);
    // 1/2; RN(1/3)=0x3eaaaaab; 1/-0=-inf; 1/inf=+0.
    expect(d,0x3f000000,0x3eaaaaab,0xff800000,0x00000000,"vrefp");
    vmx_estimate_op(s,d,words(0x40800000,0xbf800000,0x80000000,0x41100000),true);
    // 1/sqrt(4)=0.5; negative -> default QNaN; -0 -> -inf; 1/sqrt(9)=RN(1/3).
    expect(d,0x3f000000,0x7fc00000,0xff800000,0x3eaaaaab,"vrsqrtefp");
    vmx_extreme_op(s,d,words(0x00000000,0x80000000,0x3f800000,0x7f800001),words(0x80000000,0x00000000,0x40000000,0x3f800000),true);
    expect(d,0x00000000,0x00000000,0x40000000,0x7fc00001,"vmaxfp");
    vmx_extreme_op(s,d,words(0x00000000,0x80000000,0x3f800000,0x3f800000),words(0x80000000,0x00000000,0x40000000,0xff800000),false);
    expect(d,0x80000000,0x80000000,0x3f800000,0xff800000,"vminfp");
    vmx_from_integer_op(s,d,words(0xfffffffd,0x7fffffff,0x80000000,0x00000001),1,true);
    // -3/2; RN(2^31-1)/2 = 2^30; -2^31/2 = -2^30; 1/2.
    expect(d,0xbfc00000,0x4e800000,0xce800000,0x3f000000,"vcfsx");
    vmx_from_integer_op(s,d,words(0xffffffff,0x80000000,0x01000001,0x00000003),0,false);
    // RN(2^32-1)=2^32; 2^31; RN(2^24+1)=2^24 (tie to even); 3.
    expect(d,0x4f800000,0x4f000000,0x4b800000,0x40400000,"vcfux");
    expect((vmx_float_compare<0x2c6>(d,words(0x3f800000,0x40000000,0x40400000,0x7fc00000),words(0,0x40000000,0x40800000,0)),d),
           0xffffffff,0,0,0,"vcmpgtfp");
    assert(vmx_float_compare<0xc6>(d,words(0,0x80000000,0x3f800000,0xbf800000),words(0x80000000,0,0x3f800000,0xbf800000))==8);
    assert(vmx_float_compare<0x1c6>(d,words(0x7f800001,0,0,0),words(0,0,0,0))==0);
    assert(vmx_float_compare<0x1c6>(d,words(0xff800000,0xbf800000,0x80000001,0x00000001),words(0xff800000,0,0x80000002,0))==0 &&
           vmx_word(d,0)==UINT32_MAX && vmx_word(d,1)==0 && vmx_word(d,2)==UINT32_MAX && vmx_word(d,3)==UINT32_MAX);
    {VmxReg a{},b{},c{};for(unsigned i=0;i<16;++i){a[i]=std::uint8_t(i);b[i]=std::uint8_t(0x10+i);c[i]=std::uint8_t(31-2*i);}
     vmx_permute(d,a,b,c);for(unsigned i=0;i<16;++i)assert(d[i]==(31-2*i<16?31-2*i:0x10+((31-2*i)&15)));
     vmx_shift_left_double<3>(d,a,b);for(unsigned i=0;i<16;++i)assert(d[i]==i+3);
     vmx_merge<4,false>(d,a,b);expect(d,0x00010203,0x10111213,0x04050607,0x14151617,"vmrghw");
     vmx_merge<2,true>(d,a,b);for(unsigned i=0;i<16;++i)assert(d[i]==((i&2)?0x10:0)+8+(i/4)*2+(i&1));
     vmx_word_add(d,words(0xffffffff,1,0x80000000,5),words(1,2,0x80000000,7),false);expect(d,0,3,0,12,"vadduwm");
     vmx_word_add(d,words(0,1,0x80000000,5),words(1,2,1,7),true);expect(d,0xffffffff,0xffffffff,0x7fffffff,0xfffffffe,"vsubuwm");}
    // ---- differential: SIMD helpers == exact lanes / byte loops -------------
    for(unsigned n=0;n<200000;++n){
        const auto a=random_reg(),b=random_reg(),c=random_reg();VmxReg fast,exact;
        for(unsigned kind=0;kind<4;++kind){
            vmx_float_op(s,fast,a,b,c,kind);vmx_float_exact(s,exact,a,b,c,kind);
            if(!same(fast,exact)){for(unsigned i=0;i<4;++i)std::fprintf(stderr,"kind %u lane %u a=%08x b=%08x c=%08x fast=%08x exact=%08x\n",kind,i,vmx_word(a,i),vmx_word(b,i),vmx_word(c,i),vmx_word(fast,i),vmx_word(exact,i));std::abort();}
        }
        for(bool root:{false,true}){vmx_estimate_op(s,fast,a,root);vmx_estimate_exact(s,exact,a,root);
            if(!same(fast,exact)){for(unsigned i=0;i<4;++i)std::fprintf(stderr,"estimate root=%d a=%08x fast=%08x exact=%08x\n",int(root),vmx_word(a,i),vmx_word(fast,i),vmx_word(exact,i));std::abort();}}
        for(bool maximum:{false,true}){vmx_extreme_op(s,fast,a,b,maximum);vmx_extreme_exact(s,exact,a,b,maximum);assert(same(fast,exact));}
        for(bool sign:{false,true}){const unsigned scale=unsigned(rng()%32);vmx_from_integer_op(s,fast,a,scale,sign);vmx_from_integer_exact(s,exact,a,scale,sign);
            if(!same(fast,exact)){for(unsigned i=0;i<4;++i)std::fprintf(stderr,"convert sign=%d scale=%u a=%08x fast=%08x exact=%08x\n",int(sign),scale,vmx_word(a,i),vmx_word(fast,i),vmx_word(exact,i));std::abort();}}
        for(unsigned kind:{0xc6u,0x1c6u,0x2c6u}){
            unsigned cr=kind==0xc6?vmx_float_compare<0xc6>(fast,a,b):kind==0x1c6?vmx_float_compare<0x1c6>(fast,a,b):vmx_float_compare<0x2c6>(fast,a,b);
            unsigned yes=0;for(unsigned i=0;i<4;++i){const bool m=vmx_compare(vmx_word(a,i),vmx_word(b,i),kind);yes+=m;vmx_word(exact,i,m?UINT32_MAX:0);}
            assert(same(fast,exact) && cr==(yes==4?8u:yes==0?2u:0u));
        }
        VmxReg loop{};
        vmx_select(fast,a,b,c);for(unsigned i=0;i<16;++i)loop[i]=(a[i]&~c[i])|(b[i]&c[i]);assert(same(fast,loop));
        vmx_permute(fast,a,b,c);for(unsigned i=0;i<16;++i)loop[i]=(c[i]&31)<16?a[c[i]&15]:b[c[i]&15];assert(same(fast,loop));
        vmx_shift_left_double<7>(fast,a,b);for(unsigned i=0;i<16;++i)loop[i]=i+7<16?a[i+7]:b[i+7-16];assert(same(fast,loop));
        vmx_merge<1,true>(fast,a,b);for(unsigned e=0;e<8;++e){loop[2*e]=a[8+e];loop[2*e+1]=b[8+e];}assert(same(fast,loop));
        vmx_logic<1092>(fast,a,b);for(unsigned i=0;i<16;++i)loop[i]=a[i]&~b[i];assert(same(fast,loop));
        vmx_logic<1284>(fast,a,b);for(unsigned i=0;i<16;++i)loop[i]=std::uint8_t(~(a[i]|b[i]));assert(same(fast,loop));
        // In-place forms (vD aliases an input) must read all inputs first.
        VmxReg alias=a;vmx_permute(alias,alias,b,c);vmx_permute(fast,a,b,c);assert(same(alias,fast));
        alias=a;vmx_float_op(s,alias,alias,b,alias,2);vmx_float_exact(s,exact,a,b,a,2);assert(same(alias,exact));
    }
    // Non-Java mode stays an explicit stop on every float form.
    s.vscr=0x10000;bool stopped=false;
    try{vmx_float_op(s,d,words(0,0,0,0),words(0,0,0,0),words(0,0,0,0),0);}catch(const std::runtime_error&){stopped=true;}
    assert(stopped && s.fpscr==0);
#if defined(ANYPS3_VMX_SIMD)
    std::printf("simd=1 ssse3=%d fma=%d\n",
#if defined(__SSSE3__)
        1,
#else
        0,
#endif
#if defined(__FMA__)
        1);
#else
        0);
#endif
#endif
    std::puts("ok");
}
'''


def native_flags():
    try:
        flags = open('/proc/cpuinfo').read()
    except OSError:
        return None
    return ['-march=native'] if all(f' {f}' in flags for f in ('ssse3', 'fma', 'avx2')) else None


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class VmxSimd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = make_image([(0x10000, 5, struct.pack('>I', 0x4E800020)), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        cls.source = compile_program(image, process_entry=True).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)') + MAIN

    def run_variant(self, flags):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(self.source)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', *flags,
                                    str(root/'test.cpp'), '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=300,
                                 env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
            self.assertEqual(run.returncode, 0, run.stderr[-6000:] + run.stdout[-2000:])
            self.assertIn('ok', run.stdout)
            return run.stdout

    def test_baseline_sse2(self):
        out = self.run_variant([])
        self.assertIn('simd=1 ssse3=0 fma=0', out)

    def test_host_isa(self):
        flags = native_flags()
        if not flags:
            self.skipTest('host lacks SSSE3/FMA/AVX2')
        out = self.run_variant(flags)
        self.assertIn('simd=1 ssse3=1 fma=1', out)

    def test_scalar_fallback(self):
        self.run_variant(['-DANYPS3_VMX_FORCE_SCALAR'])
