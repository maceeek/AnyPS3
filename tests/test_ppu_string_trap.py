"""lswi/stswi, conditional traps and vsubuws executed as lifted native code."""
import shutil
import unittest

from anyps3.errors import AnyPS3Error
from anyps3.flow import lift
from tests import test_runtime_objects


def op(word, name):
    return f'auto {name}=[&]{{auto& r=state.r;(void)r;{lift(word, 0x100).cpp}}};'


def x(opcd_rt, ra, rb, xo, to=None):
    return (31 << 26) | (opcd_rt << 21) | (ra << 16) | (rb << 11) | (xo << 1)


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class PpuStringTrapTests(unittest.TestCase):
    def run_native(self, body):
        test_runtime_objects.RuntimeObjectsTests().run_native('(void)call;auto& state=s;' + body)

    def test_load_store_string_word_immediate(self):
        body = op(0x7CA3A4AA, 'lswi20')                 # original libssl 0x20063e60: lswi r5,r3,20
        body += op(x(31, 3, 6, 597), 'lswi6')           # lswi r31,r3,6 wraps to r0
        body += op(x(5, 4, 7, 725), 'stswi7')           # stswi r5,r4,7
        body += op(x(5, 0, 0, 597), 'lswi32')           # NB=0 -> 32 bytes, EA=0+0 is unmapped
        body += r'''
 for(unsigned i=0;i<32;++i)mem.store(0x10100+i,1,i+1);
 r[3]=0x10100;r[5]=UINT64_C(0xffffffff00000000);r[10]=0x77;lswi20();
 assert(r[5]==0x01020304 && r[6]==0x05060708 && r[7]==0x090a0b0c && r[8]==0x0d0e0f10 && r[9]==0x11121314);
 assert(r[10]==0x77 && s.pc==0x104);
 r[0]=UINT64_C(0x123456789);lswi6();assert(r[31]==0x01020304 && r[0]==0x05060000);
 r[4]=0x10180;mem.store(0x10180,8,UINT64_C(0xaaaaaaaaaaaaaaaa));stswi7();
 assert(mem.load(0x10180,4)==0x01020304 && mem.load(0x10184,4)==0x050607aa);
 fault([&]{lswi32();});
'''
        self.run_native(body)
        with self.assertRaisesRegex(AnyPS3Error, 'Invalid lswi'):
            lift(x(3, 3, 8, 597), 0x100)                # lswi r3,r3,8 loads RA

    def test_trap_conditions_and_unconditional_trap(self):
        self.assertEqual(lift(0x7FE10808, 0x100).successors, ())     # tw 31,r1,r1
        body = op(x(4, 3, 4, 4), 'tweq') + op((3 << 26) | (24 << 21), 'twi_ne0')    # tw 4,r3,r4; twi 24,r0,0
        body += op((2 << 26) | (2 << 21) | (3 << 16) | 0xffff, 'tdi_ltu') + op(x(16, 3, 4, 4), 'twlt')
        body += op(0x7FE10808, 'trap')
        body += r'''
 r[3]=5;r[4]=6;tweq();assert(s.pc==0x104);r[4]=UINT64_C(0x1700000005);fault([&]{tweq();});   // low words equal
 r[0]=0;twi_ne0();r[0]=UINT64_C(0x500000000);twi_ne0();r[0]=UINT64_C(0xffffffff);fault([&]{twi_ne0();});
 r[3]=UINT64_C(0xfffffffffffffffe);fault([&]{tdi_ltu();});r[3]=UINT64_C(0xffffffffffffffff);tdi_ltu();
 r[3]=0xffffffff;r[4]=0;fault([&]{twlt();});r[3]=1;twlt();                                  // signed word compare
 fault([&]{trap();});
'''
        self.run_native(body)

    def test_integer_division_undefined_cases_yield_zero(self):
        # divw/divwu/divd/divdu r3,r4,r5 (xo 491/459/489/457): x/0 and INT_MIN/-1 -> 0 (RPCS3 PPUInterpreter).
        body = op(x(3, 4, 5, 491), 'divw') + op(x(3, 4, 5, 459), 'divwu') + op(x(3, 4, 5, 489), 'divd') + op(x(3, 4, 5, 457), 'divdu')
        body += op(x(3, 4, 5, 491) | 1, 'divw_rc')
        body += r'''
 r[4]=7;r[5]=0;r[3]=99;divw();assert(r[3]==0);r[3]=99;divwu();assert(r[3]==0);
 r[3]=99;divd();assert(r[3]==0);r[3]=99;divdu();assert(r[3]==0);
 r[4]=0x80000000;r[5]=0xffffffff;r[3]=99;divw();assert(r[3]==0);
 r[4]=UINT64_C(0x8000000000000000);r[5]=UINT64_MAX;r[3]=99;divd();assert(r[3]==0);
 r[4]=0x80000000;r[5]=0xffffffff;divwu();assert(r[3]==0);     // unsigned: 0x80000000/0xffffffff = 0
 r[4]=UINT64_C(0xfffffff9);r[5]=2;divw();assert(r[3]==UINT64_C(0xfffffffffffffffd));  // -7/2 = -3
 r[4]=7;r[5]=0;state.cr=0;state.xer=0;divw_rc();assert(r[3]==0 && ((state.cr>>28)&15)==2);   // CR0 EQ
'''
        self.run_native(body)

    def test_frsp_nan_and_infinity(self):
        # frsp f1,f2 (63/12): QNaN payload truncated, SNaN quieted + VXSNAN, +-inf unchanged; FPRF set.
        body = op((63 << 26) | (1 << 21) | (2 << 11) | (12 << 1), 'frsp')
        body += r'''
 state.fpscr=0;state.fpr[2]=UINT64_C(0x7ff8000000000000);frsp();
 assert(state.fpr[1]==UINT64_C(0x7ff8000000000000) && ((state.fpscr>>12)&31)==0x11 && !(state.fpscr&0x01000000));
 state.fpscr=0;state.fpr[2]=UINT64_C(0x7ff80000ffffffff);frsp();assert(state.fpr[1]==UINT64_C(0x7ff80000e0000000));
 state.fpscr=0;state.fpr[2]=UINT64_C(0x7ff0000000000001);frsp();
 assert(state.fpr[1]==UINT64_C(0x7ff8000000000000) && (state.fpscr&0x01000000) && (state.fpscr&0x20000000) && (state.fpscr&0x80000000));
 state.fpscr=0;state.fpr[2]=UINT64_C(0x7ff0000000000000);frsp();assert(state.fpr[1]==UINT64_C(0x7ff0000000000000) && ((state.fpscr>>12)&31)==0x5);
 state.fpscr=0;state.fpr[2]=UINT64_C(0xfff0000000000000);frsp();assert(state.fpr[1]==UINT64_C(0xfff0000000000000) && ((state.fpscr>>12)&31)==0x9);
 state.fpscr=0;state.fpr[2]=UINT64_C(0x3ff0000000000000);frsp();assert(state.fpr[1]==UINT64_C(0x3ff0000000000000));   // finite path unchanged
'''
        self.run_native(body)

    def test_vsubuws_saturates_and_sets_sat(self):
        body = op(0x10631680, 'sub')                    # vsubuws v3,v3,v2 (libssl/game 0x6693c0)
        body += r'''
 auto set=[&](unsigned v,std::uint32_t a,std::uint32_t b,std::uint32_t c,std::uint32_t d){std::uint32_t w[4]={a,b,c,d};
   for(unsigned i=0;i<4;++i)for(unsigned j=0;j<4;++j)s.vr[v][4*i+j]=w[i]>>(24-8*j);};
 set(3,5,1,0xffffffff,7);set(2,3,1,1,0);s.vscr=0;sub();
 assert(vmx_word(s.vr[3],0)==2 && vmx_word(s.vr[3],1)==0 && vmx_word(s.vr[3],2)==0xfffffffe && vmx_word(s.vr[3],3)==7 && s.vscr==0);
 set(3,1,0,0,0);set(2,2,0,0,0);sub();assert(vmx_word(s.vr[3],0)==0 && s.vscr==1);
'''
        self.run_native(body)

    def test_saturating_add_sub_all_widths(self):
        def vx(xo):
            return (4 << 26) | (3 << 21) | (1 << 16) | (2 << 11) | xo
        names = {0x200: 'aub', 0x240: 'auh', 0x280: 'auw', 0x300: 'asb', 0x340: 'ash', 0x380: 'asw',
                 0x600: 'sub', 0x640: 'suh', 0x700: 'ssb', 0x740: 'ssh', 0x780: 'ssw'}
        self.assertEqual(vx(0x280), 0x10611280)
        body = ''.join(op(vx(xo), name) for xo, name in names.items())
        # Independent expectations: words written big-endian into the 16-byte registers.
        body += r'''
 auto put=[&](unsigned v,std::array<std::uint32_t,4> w){for(unsigned i=0;i<4;++i)for(unsigned j=0;j<4;++j)s.vr[v][4*i+j]=w[i]>>(24-8*j);};
 auto check=[&](std::array<std::uint32_t,4> w,unsigned sat){for(unsigned i=0;i<4;++i)assert(vmx_word(s.vr[3],i)==w[i]);assert(s.vscr==sat);s.vscr=0;};
 s.vscr=0;
 put(1,{0xfffffffe,5,0x80000000,0});put(2,{1,6,0x80000000,0});auw();check({0xffffffff,11,0xffffffff,0},1);
 put(1,{1,2,3,4});put(2,{5,6,7,8});auw();check({6,8,10,12},0);
 put(1,{0x7fffffff,0x80000000,0xfffffffe,5});put(2,{1,0xffffffff,0xfffffffe,0xfffffffb});asw();check({0x7fffffff,0x80000000,0xfffffffc,0},1);
 put(1,{0x80000000,0x7fffffff,10,0});put(2,{1,0xffffffff,3,0});ssw();check({0x80000000,0x7fffffff,7,0},1);
 put(1,{0xff017f80,0,0,0x10203040});put(2,{0x02020101,0,0,0x01010101});aub();check({0xff038081,0,0,0x11213141},1);
 put(1,{0xff017f80,0,0,0});put(2,{0x02020101,0,0,0});asb();check({0x01037f81,0,0,0},1);
 put(1,{0x00057f80,0,0,0});put(2,{0x01020101,0,0,0});sub();check({0x00037e7f,0,0,0},1);
 put(1,{0x00057f80,0,0,0});put(2,{0x01fa8101,0,0,0});ssb();check({0xff0b7f80,0,0,0},1);
 put(1,{0xffff7fff,0x8000,0,0});put(2,{0x00020001,0x8000,0,0});auh();check({0xffff8000,0x0000ffff,0,0},1);
 put(1,{0xffff7fff,0x8000,0,0});put(2,{0x00020001,0x8000,0,0});ash();check({0x00017fff,0x8000,0,0},1);
 put(1,{0x00010005,0,0,0});put(2,{0x00020003,0,0,0});suh();check({0x00000002,0,0,0},1);
 put(1,{0x80007fff,0,0,0});put(2,{0x0001ffff,0,0,0});ssh();check({0x80007fff,0,0,0},1);
 put(1,{0x00030004,0,0,0});put(2,{0x00010001,0,0,0});ssh();check({0x00020003,0,0,0},0);
'''
        self.run_native(body)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class VectorMultiplyTests(unittest.TestCase):
    def test_even_odd_unsigned_signed_byte_halfword_products(self):
        def vx(xo, d=3, a=1, b=2):
            return (4 << 26) | (d << 21) | (a << 16) | (b << 11) | xo
        body = '(void)call;auto& state=s;'
        names = {}
        for xo in (0x008, 0x048, 0x108, 0x148, 0x208, 0x248, 0x308, 0x348):
            names[xo] = f'm{xo:x}'
            body += op(vx(xo), names[xo])
        body += r'''
 auto bytes=[&](unsigned v,std::initializer_list<unsigned> b){unsigned i=0;for(auto x:b)s.vr[v][i++]=x;};
 auto word=[&](unsigned i){return vmx_word(s.vr[3],i);};
 bytes(1,{0xff,0x02,0x80,0x7f,0x01,0xfe,0x00,0x10, 0x12,0x34,0xff,0xff,0x80,0x00,0x7f,0xff});
 bytes(2,{0xff,0x03,0x80,0x7f,0x05,0xfe,0xff,0x10, 0x56,0x78,0xff,0xff,0x80,0x00,0x80,0x01});
 m248();assert(word(0)==UINT32_C(0xff02)*0xff03 && word(1)==UINT32_C(0x01fe)*0x05fe && word(2)==UINT32_C(0x1234)*0x5678 && word(3)==UINT32_C(0x8000)*0x8000);
 m48();assert(word(0)==UINT32_C(0x807f)*0x807f && word(1)==UINT32_C(0x0010)*0xff10 && word(2)==UINT32_C(0xfffe0001) && word(3)==UINT32_C(0x7fff)*0x8001);
 m348();assert(word(0)==std::uint32_t(std::int32_t(-254)*-253) && word(3)==std::uint32_t(std::int32_t(-32768)*-32768));
 m148();assert(word(2)==1 && word(3)==std::uint32_t(std::int32_t(32767)*-32767));
 auto half=[&](unsigned i){return std::uint32_t(s.vr[3][2*i]<<8|s.vr[3][2*i+1]);};
 m8();assert(half(0)==0x02*0x03 && half(1)==0x7f*0x7f && half(2)==0xfe*0xfe && half(3)==0x10*0x10);
 m208();assert(half(0)==0xff*0xff && half(1)==0x80*0x80 && half(2)==0x01*0x05 && half(3)==0);
 m308();assert(half(0)==1 && half(1)==0x4000 && half(2)==5 && half(3)==0);
 m108();assert(half(0)==6 && half(1)==0x7f*0x7f && half(2)==4 && half(3)==0x100);
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body)
