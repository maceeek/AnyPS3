"""Literal PowerPC Book I 2.02 expectations; no host FP arithmetic oracle.

Arithmetic vectors follow pp. 87-99, 110-118, 125-126 and appendix A.
NI bounds are from Cell BE Programming Handbook 1.1 PDF p.721.
The tiny C++ harness executes the same helpers and lifted instructions as the
runtime, without linking unrelated services undergoing parallel development.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.floating import FLOAT_CPP
from anyps3.flow import lift


def u64(value):
    return f'UINT64_C(0x{value:016x})'


def instruction(word):
    return '{auto& state=s; auto& r=s.r; (void)r; ' + lift(word, 0x100).cpp + '}\n'


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class FpSpecialTests(unittest.TestCase):
    def native(self, body):
        source = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <initializer_list>
#include <stdexcept>
struct Memory {
 std::array<std::uint8_t,256> bytes{};
 std::uint64_t load(unsigned address,unsigned width){
  std::uint64_t value=0;for(unsigned i=0;i<width;++i)value=(value<<8)|bytes.at(address+i);return value;
 }
 void store(unsigned address,unsigned width,std::uint64_t value){
  for(unsigned i=0;i<width;++i){bytes.at(address+width-1-i)=value;value>>=8;}
 }
};
// Lifted loads/stores call the PPUFAST helpers; this mock memory has no flat view.
template<unsigned N,class S> std::uint64_t ppu_load(S& s,std::uint64_t ea){return s.memory.load(static_cast<unsigned>(ea),N);}
template<unsigned N,class S> void ppu_store(S& s,std::uint64_t ea,std::uint64_t v){s.memory.store(static_cast<unsigned>(ea),N,v);}
struct PpuState {
 std::uint32_t fpscr=0,cr=0,pc=0;
 std::array<std::uint64_t,32> fpr{},r{}; Memory memory;
 std::uint64_t fp_lazy_a=0,fp_lazy_b=0,fp_lazy_c=0;std::uint8_t fp_lazy_op=0,fp_lazy_precision=0; // lazy FPSCR record (PpuContext)
};
''' + FLOAT_CPP + '\nint main(){PpuState s;\n' + body + '\n}\n'
        with tempfile.TemporaryDirectory(prefix='anyps3-fpspecial-') as directory:
            cpp, exe = Path(directory)/'test.cpp', Path(directory)/'test'
            cpp.write_text(source)
            build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',
                                    '-fsanitize=undefined', '-fno-sanitize-recover=all',
                                    str(cpp),'-o',str(exe)], capture_output=True,text=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            run = subprocess.run([str(exe)],capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_rounding_modes_overflow_gradual_underflow_and_signed_zero(self):
        # 1 + half an ulp; ties-even gives 1; upward gives the next value.
        # 1 + 3*half ulp gives the even SECOND successor under RN=0.
        body = ''
        for precision, half, ulp, tiny, normal, maximum, twice_max in (
            (53,0x3ca0000000000000,1,1,0x0010000000000000,0x7fefffffffffffff,0x7fefffffffffffff),
            (24,0x3e70000000000000,0x20000000,0x36a0000000000000,0x3810000000000000,0x47efffffe0000000,0x47efffffe0000000),
        ):
            for rn in range(4):
                for sign in (0,1):
                    signbit=sign<<63
                    result=0x3ff0000000000000+(ulp if rn==(3 if sign else 2) else 0)
                    status=(0x82068000 if sign else 0x82064000) if rn==(3 if sign else 2) else (0x82028000 if sign else 0x82024000)
                    body += f's.fpscr={rn};assert(fp_add(s,{u64(0x3ff0000000000000|signbit)},{u64(half|signbit)},{precision})=={u64(result|signbit)} && s.fpscr==0x{status|rn:x}u);\n'
                    # Overflow results: nearest or direction of sign => infinity.
                    infinity=rn in (0,3 if sign else 2)
                    result=(0x7ff0000000000000 if infinity else maximum)|signbit
                    status=0x92020000|((9 if sign else 5) if infinity else (8 if sign else 4))<<12|rn
                    body += f's.fpscr={rn};assert(fp_multiply(s,{u64(twice_max|signbit)},{u64(0x4000000000000000)},{precision})=={u64(result)} && s.fpscr==0x{status:x}u);\n'
                    # Minimum subnormal / 2 is an exact halfway case to zero.
                    increment=rn==(3 if sign else 2)
                    result=(tiny if increment else 0)|signbit
                    status=0x8a020000 | (0x40000 if increment else 0) | ((24 if sign else 20) if increment else (18 if sign else 2))<<12 | rn
                    body += f's.fpscr={rn};assert(fp_divide(s,{u64(tiny|signbit)},{u64(0x4000000000000000)},{precision})=={u64(result)} && s.fpscr==0x{status:x}u);\n'
                    # Exact underflow into subnormals does not set UX or XX.
                    expected=0x0008000000000000 if precision==53 else 0x3800000000000000
                    body += f's.fpscr={rn};assert(fp_multiply(s,{u64(normal|signbit)},{u64(0x3fe0000000000000)},{precision})=={u64(expected|signbit)} && s.fpscr==0x{((24 if sign else 20)<<12)|rn:x}u);\n'
                for a,b in ((0,1<<63),(0x3ff0000000000000,0xbff0000000000000)):
                    result=1<<63 if rn==3 else 0
                    body += f's.fpscr={rn};assert(fp_add(s,{u64(a)},{u64(b)},{precision})=={u64(result)} && s.fpscr==0x{(0x12000 if rn==3 else 0x2000)|rn:x}u);\n'
        # Underflow BEFORE rounding: largest midpoint rounds up to min normal.
        body += r'''
s.fpscr=0;assert(fp_multiply(s,UINT64_C(0x0010000000000000),UINT64_C(0x3fefffffffffffff))==UINT64_C(0x0010000000000000) && s.fpscr==0x8a064000u);
// Vast gaps must still influence directed rounding (also below minimum subnormal).
s.fpscr=2;assert(fp_add(s,UINT64_C(0x3ff0000000000000),1)==UINT64_C(0x3ff0000000000001) && s.fpscr==0x82064002u);
s.fpscr=1;assert(fp_add(s,UINT64_C(0x3ff0000000000000),UINT64_C(0x8000000000000001))==UINT64_C(0x3fefffffffffffff) && s.fpscr==0x82024001u);
s.fpscr=2;assert(fp_multiply(s,1,1)==1 && s.fpscr==0x8a074002u);
s.fpscr=0;assert(fp_add(s,UINT64_C(0x8000000000000000),UINT64_C(0x8000000000000000))==UINT64_C(0x8000000000000000) && s.fpscr==0x12000);
'''
        self.native(body)

    def test_finite_division_sqrt_frsp_and_integer_source_rounding(self):
        body=''
        # Exact real sqrt(2) lies strictly between these adjacent encodings.
        for precision,lo,hi,nearest in ((53,0x3ff6a09e667f3bcc,0x3ff6a09e667f3bcd,True),
                                        (24,0x3ff6a09e60000000,0x3ff6a09e80000000,False)):
            for rn in range(4):
                up=nearest if rn==0 else rn==2
                status=0x82024000|(0x40000 if up else 0)|rn
                body+=f's.fpscr={rn};assert(fp_sqrt(s,UINT64_C(0x4000000000000000),{precision})=={u64(hi if up else lo)} && s.fpscr==0x{status:x}u);\n'
        # Exact 1/3; positive/negative rounding directions reverse.
        for precision,lo,hi,nearest in ((53,0x3fd5555555555555,0x3fd5555555555556,False),
                                        (24,0x3fd5555540000000,0x3fd5555560000000,True)):
            for rn in range(4):
                for sign in (0,1):
                    up=nearest if rn==0 else rn==(3 if sign else 2)
                    result=(hi if up else lo)|(sign<<63)
                    status=0x82020000|(0x40000 if up else 0)|((8 if sign else 4)<<12)|rn
                    body+=f's.fpscr={rn};assert(fp_divide(s,{u64(0x3ff0000000000000|(sign<<63))},UINT64_C(0x4008000000000000),{precision})=={u64(result)} && s.fpscr==0x{status:x}u);\n'
        for rn in range(4):
            for sign in (0,1):
                # halfway between 1 and its first binary32 successor.
                up=rn==(3 if sign else 2)
                body+=f's.fpscr={rn};s.fpr[1]={u64(0x3ff0000010000000|(sign<<63))};\n'
                body+=instruction((63<<26)|(1<<21)|(1<<11)|(12<<1))
                body+=f'assert(s.fpr[1]=={u64((0x3ff0000020000000 if up else 0x3ff0000000000000)|(sign<<63))} && s.fpscr==0x{0x82020000|(0x40000 if up else 0)|((8 if sign else 4)<<12)|rn:x}u);\n'
        # Odd halfway significand rounds to even second successor.
        body+='s.fpscr=0;assert(fp_add(s,UINT64_C(0x3ff0000000000001),UINT64_C(0x3ca0000000000000))==UINT64_C(0x3ff0000000000002) && s.fpscr==0x82064000u);\n'
        # Integer 'NaN-looking' raw bits are always an integer for fcfid.
        body+='s.fpscr=0;assert(fp_from_integer(s,UINT64_C(0x8000000000000000))==UINT64_C(0xc3e0000000000000) && s.fpscr==0x8000u);\n'
        self.native(body)

    def test_nan_invalid_operations_and_payload_precedence(self):
        # Payloads below the binary32 payload boundary MUST survive f*s.
        body = r'''
const auto inf=UINT64_C(0x7ff0000000000000),ninf=UINT64_C(0xfff0000000000000);
const auto q=UINT64_C(0xfff8000000000123),sn=UINT64_C(0x7ff0000000000456),one=UINT64_C(0x3ff0000000000000);
for(unsigned p:{24u,53u}){
 for(auto fn:{fp_add,fp_multiply,fp_divide}){
  s.fpscr=0x60000;assert(fn(s,q,sn,p)==q && s.fpscr==0xa1011000u);
  s.fpscr=0;assert(fn(s,sn,q,p)==UINT64_C(0x7ff8000000000456) && s.fpscr==0xa1011000u);
 }
 s.fpscr=0;assert(fp_fused(s,one,sn,q,p,true)==q && s.fpscr==0xa1011000u);
 s.fpscr=0;assert(fp_fused(s,inf,0,q,p,true)==q && s.fpscr==0xa0111000u);
 s.fpscr=0;assert(fp_fused(s,inf,0,sn,p,true)==UINT64_C(0x7ff8000000000456) && s.fpscr==0xa1111000u);
 s.fpscr=0;assert(fp_fused(s,inf,one,ninf,p)==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa0811000u);
 s.fpscr=0;assert(fp_sqrt(s,ninf,p)==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa0011200u);
 s.fpscr=0;assert(fp_sqrt(s,one|UINT64_C(0x8000000000000000),p)==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa0011200u);
 s.fpscr=0;assert(fp_sqrt(s,inf,p)==inf && s.fpscr==0x5000u);
 s.fpscr=0;assert(fp_sqrt(s,UINT64_C(0x8000000000000000),p)==UINT64_C(0x8000000000000000) && s.fpscr==0x12000u);
 s.fpscr=0;assert(fp_sqrt(s,sn,p)==UINT64_C(0x7ff8000000000456) && s.fpscr==0xa1011000u);
}
std::uint64_t result=0;
s.fpscr=0;assert(fp_round_single_special(s,q,result) && result==UINT64_C(0xfff8000000000000) && s.fpscr==0x11000u);
s.fpscr=0;assert(fp_round_single_special(s,sn,result) && result==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa1011000u);
// Sticky cause without FX: recurrence does not set FX, new cause does.
s.fpscr=0x20000200;fp_sqrt(s,ninf);assert(s.fpscr==0x20011200u);
s.fpscr=0x20000200;fp_divide(s,0,0);assert(s.fpscr==0xa0211200u);
'''
        self.native(body)

    def test_fused_directed_rounding_and_lifted_aliases(self):
        body=''
        for op in (59,63):
            half=0x3e70000000000000 if op==59 else 0x3ca0000000000000
            ulp=0x20000000 if op==59 else 1
            for variant in (28,29,30,31):
                for target in (1,2,3):
                    for rn in range(4):
                        # fmsub uses negative B, so all four variants start at 1+half-ulp.
                        addend=half | ((1<<63) if variant in (28,30) else 0)
                        negative=variant>=30
                        out=(0x3ff0000000000000+(ulp if rn==2 else 0))|((1<<63) if negative else 0)
                        status=0x82020000|(0x40000 if rn==2 else 0)|((8 if negative else 4)<<12)|rn
                        word=(op<<26)|(target<<21)|(1<<16)|(2<<11)|(3<<6)|(variant<<1)|1
                        body+=f's.fpscr={rn};s.cr=0x12345678;s.fpr[1]={u64(0x3ff0000000000000)};s.fpr[2]={u64(addend)};s.fpr[3]={u64(0x3ff0000000000000)};\n'
                        body+=instruction(word)
                        body+=f'assert(s.fpr[{target}]=={u64(out)} && s.fpscr==0x{status:x}u && s.cr==0x18345678u && s.pc==0x104);\n'
        # Fused intermediate overflow must not set OX (max*2 - max = max).
        body+='s.fpscr=0;assert(fp_fused(s,UINT64_C(0x7fefffffffffffff),UINT64_C(0x4000000000000000),UINT64_C(0xffefffffffffffff))==UINT64_C(0x7fefffffffffffff) && s.fpscr==0x4000);\n'
        self.native(body)

    def test_integer_conversions_rounding_saturation_and_fcfid(self):
        body=''
        # Literal ties, negative fractions, and endpoint rounding.
        values=[(0x3ff8000000000000,(2,1,2,1)),(0xbff8000000000000,(-2,-1,-1,-2)),
                (0x3fe0000000000000,(0,0,1,0)),(0xbfe0000000000000,(0,0,0,-1)),
                (0x4004000000000000,(2,2,3,2)),(0xc004000000000000,(-2,-2,-2,-3))]
        for width,xo in ((32,14),(64,814)):
            for value,results in values:
                for rn,out in enumerate(results):
                    up=(abs(out)==2 and value in (0x3ff8000000000000,0xbff8000000000000)) or (abs(out)==1 and value in (0x3fe0000000000000,0xbfe0000000000000)) or abs(out)==3
                    status=0x82020000|(0x40000 if up else 0)|rn|0x11000
                    body+=f's.fpscr=0x{0x11000|rn:x};s.fpr[1]={u64(value)};\n'
                    body+=instruction((63<<26)|(1<<21)|(1<<11)|(xo<<1)|1)
                    body+=f'assert(s.fpr[1]=={u64(out&((1<<64)-1))} && s.fpscr==0x{status:x}u && ((s.cr>>24)&15)==8);\n'
            for value,out,flags in ((0x7ff8000000000001,1<<(width-1),0xa0000100),(0x7ff0000000000001,1<<(width-1),0xa1000100),
                                    (0xfff0000000000000,1<<(width-1),0xa0000100),(0x7ff0000000000000,(1<<(width-1))-1,0xa0000100)):
                if width==32 and out==1<<31:out=0xffffffff80000000
                body+=f's.fpscr=0;assert(fp_to_integer(s,{u64(value)},{width},false)=={u64(out)} && s.fpscr==0x{flags:x}u);\n'
        body+=r'''
s.fpscr=2;assert(fp_to_integer(s,UINT64_C(0x41dfffffffe00000),32,false)==UINT64_C(0x7fffffff) && s.fpscr==0xa0000102u); // 2^31-0.5 rounds out
s.fpscr=1;assert(fp_to_integer(s,UINT64_C(0x41dfffffffe00000),32,false)==UINT64_C(0x7fffffff) && s.fpscr==0x82020001u);
s.fpscr=3;assert(fp_to_integer(s,UINT64_C(0xc1e0000000100000),32,false)==UINT64_C(0xffffffff80000000) && s.fpscr==0xa0000103u);
s.fpscr=2;assert(fp_to_integer(s,UINT64_C(0x8000000000000001),64,false)==0 && s.fpscr==0x82020002u);
s.fpscr=3;assert(fp_to_integer(s,UINT64_C(0x8000000000000001),64,false)==UINT64_MAX && s.fpscr==0x82060003u);
'''
        for rn,result,status in ((0,0x4340000000000000,0x82024000),(1,0x4340000000000000,0x82024000),(2,0x4340000000000001,0x82064000),(3,0x4340000000000000,0x82024000)):
            body+=f's.fpscr={rn};assert(fp_from_integer(s,UINT64_C(0x0020000000000001))=={u64(result)} && s.fpscr==0x{status|rn:x}u);\n'
        self.native(body)

    def test_fpscr_control_compare_and_non_arithmetic_instructions(self):
        body=''
        # mtfsfi: FX is only written in field 0, not auto-raised by OX.
        for field,value,expected in ((0,1,0x10000000),(1,1,0x21000000),(5,7,0x20000700),(7,7,7)):
            body+='s.fpscr=0;s.cr=0;\n'+instruction((63<<26)|(field<<23)|(value<<12)|(134<<1)|1)
            body+=f'assert(s.fpscr==0x{expected:x}u && ((s.cr>>24)&15)=={expected>>28});\n'
        body+='s.fpscr=0;\n'+instruction((63<<26)|(7<<21)|(38<<1)|1)
        body+='assert(s.fpscr==0xa1000000u && ((s.cr>>24)&15)==10);\n'
        body+=instruction((63<<26)|(7<<21)|(70<<1)|1)
        body+='assert(s.fpscr==0x80000000u && ((s.cr>>24)&15)==8);\n'
        body+='s.fpscr=0;s.fpr[2]=UINT64_C(0x84000010);\n'+instruction((63<<26)|(255<<17)|(2<<11)|(711<<1))
        body+='assert(s.fpscr==0xc4000010u);\n'
        body+=instruction((63<<26)|(6<<23)|(1<<18)|(64<<1))
        body+='assert(((s.cr>>4)&15)==4 && s.fpscr==0x80000010u);\n'
        # Setting FEX/VX or reserved bit 20 has no direct effect.
        for bit in (1,2,20):
            body+='s.fpscr=0;\n'+instruction((63<<26)|(bit<<21)|(38<<1))+'assert(s.fpscr==0);\n'
        # Moves, sign changes and fsel do not quiet SNaNs or raise exceptions,
        # even with VE enabled; record forms copy the existing FPSCR top nibble.
        for xo,expected in ((40,0xfff0000000000001),(72,0x7ff0000000000001),(136,0xfff0000000000001),(264,0x7ff0000000000001)):
            body+='s.fpscr=0x92000080;s.cr=0;s.fpr[1]=UINT64_C(0x7ff0000000000001);\n'
            body+=instruction((63<<26)|(1<<21)|(1<<11)|(xo<<1)|1)
            body+=f'assert(s.fpr[1]=={u64(expected)} && s.fpscr==0x92000080u && s.cr==0x09000000u);\n'
        for condition,expected in ((0,123),(1<<63,123),(0x7ff0000000000000,123),(0xfff0000000000000,456),(0x7ff0000000000001,456)):
            body+=f's.fpscr=0xa1000080;s.fpr[1]={u64(condition)};s.fpr[2]=456;s.fpr[3]=123;\n'
            body+=instruction((63<<26)|(1<<21)|(1<<16)|(2<<11)|(3<<6)|(23<<1)|1)
            body+=f'assert(s.fpr[1]=={expected} && s.fpscr==0xa1000080u && ((s.cr>>24)&15)==10);\n'
        body+='s.fpscr=0x84000010;\n'+instruction((63<<26)|(1<<21)|(583<<1)|1)+'assert(s.fpr[1]==0x84000010u && ((s.cr>>24)&15)==8);\n'
        body+=r'''
// Compare only updates FPCC, preserving C, FR and FI.
s.fpscr=0x70000;s.cr=0x12345678;fp_compare(s,3,UINT64_C(0x7ff8000000000001),0,true);assert(s.fpscr==0xa00f1000u && s.cr==0x12315678u);
s.fpscr=0x70000;fp_compare(s,7,UINT64_C(0x7ff0000000000001),0,false);assert(s.fpscr==0xa1071000u);
'''
        self.native(body)

    def test_frsp_original_stop_estimates_ni_and_enabled_traps(self):
        body='s.fpscr=0xa6304100;s.fpr[1]=0;\n'
        body+=instruction((63<<26)|(1<<21)|(1<<16)|(1<<11)|(18<<1))
        body+=instruction((63<<26)|(1<<21)|(1<<11)|(12<<1)|1)
        body+='assert(s.fpr[1]==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa6311100u && ((s.cr>>24)&15)==10);\n'
        body+=r'''
for(bool root:{false,true}){
 s.fpscr=0;assert(fp_estimate(s,0,root)==UINT64_C(0x7ff0000000000000) && s.fpscr==0x84005000u);
 s.fpscr=0;assert(fp_estimate(s,UINT64_C(0x8000000000000000),root)==UINT64_C(0xfff0000000000000) && s.fpscr==0x84009000u);
 s.fpscr=0;assert(fp_estimate(s,UINT64_C(0x7ff0000000000000),root)==0 && s.fpscr==0x2000);
 s.fpscr=0;assert(fp_estimate(s,UINT64_C(0x7ff0000000000001),root)==UINT64_C(0x7ff8000000000001) && s.fpscr==0xa1011000u);
}
s.fpscr=0;assert(fp_estimate(s,UINT64_C(0xfff0000000000000),false)==UINT64_C(0x8000000000000000) && s.fpscr==0x12000);
s.fpscr=0;assert(fp_estimate(s,UINT64_C(0xfff0000000000000),true)==UINT64_C(0x7ff8000000000000) && s.fpscr==0xa0011200u);
// NI: bounded approximation choice; no unsupported FTZ assumption.
for(unsigned rn=0;rn<4;++rn){
 s.fpscr=rn|4;assert(fp_add(s,1,0)==1 && s.fpscr==(0x14004u|rn));
 s.fpscr=rn|4;assert(fp_divide(s,UINT64_C(0x4010000000000000),UINT64_C(0x4000000000000000))==UINT64_C(0x4000000000000000) && s.fpscr==(0x4004u|rn));
 s.fpscr=rn|4;assert(fp_sqrt(s,UINT64_C(0x4010000000000000))==UINT64_C(0x4000000000000000) && s.fpscr==(0x4004u|rn));
}
'''
        for enable in (0x80,0x40,0x20,0x10,0x08):
            for word in ((63<<26)|(1<<21)|(1<<16)|(2<<11)|(21<<1),
                         (63<<26)|(1<<21)|(2<<11)|(12<<1),
                         (63<<26)|(1<<21)|(2<<11)|(814<<1)):
                body+=f's.fpscr={enable};s.fpr[1]=123;s.fpr[2]=0;s.cr=456;s.pc=0x100;bool caught_{enable}_{word}=false;try{{\n'
                body+=instruction(word)+f'}}catch(const std::runtime_error&){{caught_{enable}_{word}=true;}}\n'
                body+=f'assert(caught_{enable}_{word} && s.fpscr=={enable} && s.fpr[1]==123 && s.cr==456 && s.pc==0x100);\n'
        self.native(body)

    def test_load_store_be_preserves_snan_without_exceptions(self):
        body=r'''
s.fpscr=0x80;s.memory.store(16,4,0x7f800001);s.r[3]=16;
'''
        body+=instruction((48<<26)|(1<<21)|(3<<16))
        body+='assert(s.fpr[1]==UINT64_C(0x7ff0000020000000) && s.fpscr==0x80);\n'
        body+=instruction((52<<26)|(1<<21)|(3<<16)|4)
        body+='assert(s.memory.load(20,4)==0x7f800001 && s.memory.load(20,1)==0x7f && s.memory.load(23,1)==1 && s.fpscr==0x80);\n'
        body+='s.fpr[2]=UINT64_C(0xfff0000000000123);\n'+instruction((54<<26)|(2<<21)|(3<<16)|8)
        body+=instruction((50<<26)|(2<<21)|(3<<16)|8)
        body+='assert(s.fpr[2]==UINT64_C(0xfff0000000000123) && s.memory.load(24,1)==255 && s.memory.load(31,1)==0x23 && s.fpscr==0x80);\n'
        self.native(body)
