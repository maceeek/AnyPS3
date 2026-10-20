"""Lazy FR/FI/FPRF for lifted PPU FP arithmetic (floating.py fp_lazy).

1. Differential: a lazy operation followed by fp_materialize leaves exactly the
   result and FPSCR of the eager helper (validated against exact rationals in
   test_floating), for random operands, FPSCR states and operation chains.
2. A translated program with literal FPSCR values (Book I classes): mffs after
   an inexact fadd, and fcmpu keeping the FPRF C bit of a preceding -0 result.
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

CODE = [0xfc22182a,  # fadd f1,f2,f3        1 + 2^-60 -> 1.0, inexact
        0xfc80048e,  # mffs f4
        0xfca200b2,  # fmul f5,f2,f2        1.0 exact
        0xfce800b2,  # fmul f7,f8,f2        -0.0 exact (FPRF -zero, C=1)
        0xfc821000,  # fcmpu cr1,f2,f2      equal; FPRF C bit stays from -0
        0xfd20048e,  # mffs f9
        0xed4218ba,  # fmadds f10,f2,f2,f3  1 + 2^-60 rounds to single 1.0, inexact
        0xfd605090,  # fmr f11,f10
        0x4e800020]  # blr

DIFFERENTIAL = r'''
#include <cassert>
#include <random>
static std::mt19937_64 rng(99);
static std::uint64_t operand(){
    const std::uint64_t sign=(rng()&1)<<63,raw=rng();
    switch(rng()%10){
    case 0:return sign;                                                   // +-0
    case 1:return sign|UINT64_C(0x7ff0000000000000);                      // infinity
    case 2:return sign|UINT64_C(0x7ff0000000000000)|((raw&UINT64_C(0xfffffffffffff))|1); // NaN
    case 3:return sign|(raw&UINT64_C(0xfffffffffffff));                   // denormal
    case 4:return sign|(UINT64_C(1060)<<52)|(raw&UINT64_C(0xfffffffffffff)); // just outside the host range
    case 5:return sign|(UINT64_C(0x3ff)<<52)|(raw&UINT64_C(0xfffffffffffff)); // [1,2)
    case 6:{const float f=float(std::int32_t(raw))*0x1p-20f;double d=f;std::uint64_t u;std::memcpy(&u,&d,8);return u;} // single-representable
    default:return sign|((UINT64_C(623)+raw%801)<<52)|(raw&UINT64_C(0xfffffffffffff)); // in range
    }
}
static std::uint32_t fpscr_state(){
    switch(rng()%6){
    case 0:return 0;
    case 1:return 0x02000000u;                                            // XX only
    case 2:return 0x82000000u|std::uint32_t(rng()&0x7f000u);              // FX/XX + stale FR/FI/FPRF
    case 3:return 0x02000000u|std::uint32_t(rng()%4);                     // XX + directed rounding
    case 4:return 0x02000004u;                                            // XX + NI
    default:return 0x02000000u|std::uint32_t(rng()&0x1df80700u)|std::uint32_t(rng()&0x7f000u); // XX + other sticky bits
    }
}
static std::uint64_t run(PpuState& s,unsigned op,std::uint64_t a,std::uint64_t b,std::uint64_t c,unsigned precision,bool lazy){
    switch(op){
    case 0:return lazy?fp_add_lazy(s,a,b,precision):fp_add(s,a,b,precision);
    case 1:return lazy?fp_multiply_lazy(s,a,b,precision):fp_multiply(s,a,b,precision);
    case 2:return lazy?fp_divide_lazy(s,a,b,precision):fp_divide(s,a,b,precision);
    case 3:return lazy?fp_sqrt_lazy(s,a,precision):fp_sqrt(s,a,precision);
    case 4:return lazy?fp_fused_lazy(s,a,c,b,precision,false):fp_fused(s,a,c,b,precision,false);
    default:return lazy?fp_fused_lazy(s,a,c,b,precision,true):fp_fused(s,a,c,b,precision,true);
    }
}
int main(){
    unsigned lazy_hits=0;
    for(unsigned n=0;n<300000;++n){
        PpuState lazy,eager;lazy.fpscr=eager.fpscr=fpscr_state();
        const unsigned length=1+unsigned(rng()%3);
        for(unsigned k=0;k<length;++k){
            const unsigned op=unsigned(rng()%6),precision=rng()&1?24:53;
            const auto a=operand(),b=operand(),c=operand();
            std::uint64_t x=0,y=0;bool lazy_error=false,eager_error=false;
            try{x=run(lazy,op,a,b,c,precision,true);}catch(const std::runtime_error&){lazy_error=true;}
            try{y=run(eager,op,a,b,c,precision,false);}catch(const std::runtime_error&){eager_error=true;}
            lazy_hits+=lazy.fp_lazy_op!=0;
            if(lazy_error!=eager_error || x!=y){std::fprintf(stderr,"op=%u p=%u a=%016llx b=%016llx c=%016llx lazy=%016llx eager=%016llx\n",op,precision,
                (unsigned long long)a,(unsigned long long)b,(unsigned long long)c,(unsigned long long)x,(unsigned long long)y);std::abort();}
            if(lazy_error)break;
        }
        fp_materialize(lazy);
        if(lazy.fpscr!=eager.fpscr || lazy.fp_lazy_op){std::fprintf(stderr,"fpscr lazy=%08x eager=%08x\n",lazy.fpscr,eager.fpscr);std::abort();}
    }
    // An observer that writes FPSCR first replays the pending update.
    {PpuState s;s.fpscr=0x02000000;const auto r=fp_add_lazy(s,UINT64_C(0x3ff0000000000000),UINT64_C(0x3c30000000000000));
     assert(r==UINT64_C(0x3ff0000000000000) && s.fp_lazy_op==1 && s.fpscr==0x02000000);
     fp_write_bit(s,31,true);assert(s.fp_lazy_op==0 && s.fpscr==0x02024001);}
    std::printf("lazy_hits=%u\n",lazy_hits);
    assert(lazy_hits>10000);
    std::puts("ok");
}
'''

PROGRAM = r'''
#include <cassert>
int main(){
    static const std::uint8_t code[]={@CODE@};
    PpuState s;s.memory.map(0x10000,0x1000,5,code,sizeof(code));
    s.pc=0x10000;s.lr=RETURN_SENTINEL;s.fpscr=0x02000000;
    s.fpr[2]=UINT64_C(0x3ff0000000000000);s.fpr[3]=UINT64_C(0x3c30000000000000);s.fpr[8]=UINT64_C(0x8000000000000000);
    anyps3_entry(s,false);
    assert(s.returned && s.steps==9 && s.fp_lazy_op==0);
    assert(s.fpr[1]==UINT64_C(0x3ff0000000000000) && s.fpr[5]==UINT64_C(0x3ff0000000000000) && s.fpr[7]==UINT64_C(0x8000000000000000));
    assert(s.fpr[4]==0x02024000);   // XX | FI | FPRF +normal
    assert(s.fpr[9]==0x02012000);   // FPRF = C (from -0) | FPCC equal
    assert(((s.cr>>24)&15)==2);     // cr1 = equal
    assert(s.fpr[10]==UINT64_C(0x3ff0000000000000) && s.fpr[11]==s.fpr[10]);
    assert(s.fpscr==0x02024000);    // exact on exit: fmadds inexact, +normal
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuFpLazy(unittest.TestCase):
    def build_and_run(self, source, flags=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', *flags,
                                    str(root/'test.cpp'), '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=300,
                                 env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
            self.assertEqual(run.returncode, 0, run.stderr[-4000:] + run.stdout[-2000:])
            self.assertIn('ok', run.stdout)

    def program(self, extra=()):
        payload = struct.pack('>' + 'I' * len(CODE), *CODE)
        image = make_image([(0x10000, 5, payload), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True, extra_entries=(0x10000,)).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        return source, payload

    def test_lazy_equals_eager(self):
        source, _ = self.program()
        self.build_and_run(source + DIFFERENTIAL)

    def test_translated_program_literal_fpscr(self):
        source, payload = self.program()
        self.build_and_run(source + PROGRAM.replace('@CODE@', ','.join(str(b) for b in payload)))
