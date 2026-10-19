"""Native VMX checks: architectural byte order, aliases, CR6/VSCR and FP isolation."""
import shutil
import unittest
from anyps3.flow import lift
from tests import test_runtime_objects


def instruction(word):
    return '{'+lift(word,0x100).cpp+'}'


def vx(code,d=3,a=1,b=2):
    return (4<<26)|(d<<21)|(a<<16)|(b<<11)|code


@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class VectorTests(unittest.TestCase):
    def run_native(self,body):
        test_runtime_objects.RuntimeObjectsTests().run_native('(void)call;auto& state=s;'+body)

    def test_integer_element_families_and_shift_vectors(self):
        import random, struct
        rng=random.Random(4)
        A=bytes(rng.getrandbits(8) for _ in range(16));B=bytes(rng.getrandbits(8) for _ in range(16))
        A=A[:4]+bytes([0x80,0,0,0])+A[8:12]+bytes([0x7f,0xff,0xff,0xff])   # word extremes
        B=B[:2]+A[2:4]+B[4:]                                                 # one equal halfword
        def elems(v,n,signed=False):
            return [int.from_bytes(v[i:i+n],'big',signed=signed) for i in range(0,16,n)]
        def pack(values,n):
            return b''.join((x&((1<<8*n)-1)).to_bytes(n,'big') for x in values)
        expected={}
        for code,n,high in ((0xc,1,1),(0x4c,2,1),(0x8c,4,1),(0x10c,1,0),(0x14c,2,0),(0x18c,4,0)):
            a,b=elems(A,n),elems(B,n);h=len(a)//2;src=range(h) if high else range(h,2*h)
            expected[code]=pack([x for i in src for x in (a[i],b[i])],n)
        for code,n,high in ((0x20e,1,1),(0x28e,1,0),(0x24e,2,1),(0x2ce,2,0)):
            b=elems(B,n,True);h=len(b)//2
            expected[code]=pack(b[:h] if high else b[h:],2*n)
        for code,n,signed,f in ((0x2,1,0,max),(0x42,2,0,max),(0x82,4,0,max),(0x102,1,1,max),(0x142,2,1,max),(0x182,4,1,max),
                                (0x202,1,0,min),(0x242,2,0,min),(0x282,4,0,min),(0x302,1,1,min),(0x342,2,1,min),(0x382,4,1,min)):
            expected[code]=pack([f(x,y) for x,y in zip(elems(A,n,signed),elems(B,n,signed))],n)
        for code,n in ((0x4,1),(0x44,2),(0x84,4)):
            w=8*n;rot=lambda x,k:((x<<k)|(x>>(w-k)))&((1<<w)-1) if k else x
            expected[code]=pack([rot(x,y%w) for x,y in zip(elems(A,n),elems(B,n))],n)
        for code,n in ((0x6,1),(0x46,2)):
            expected[code]=pack([-1 if x==y else 0 for x,y in zip(elems(A,n),elems(B,n))],n)
        sat={}
        for code,n,signed in ((0x8e,2,0),(0xce,4,0),(0x18e,2,1),(0x1ce,4,1)):
            m=n//2;lo,hi=(-(1<<(8*m-1)),(1<<(8*m-1))-1) if signed else (0,(1<<8*m)-1)
            src=elems(A,n,signed)+elems(B,n,signed)
            expected[code]=pack([min(max(x,lo),hi) for x in src],m);sat[code]=any(x<lo or x>hi for x in src)
        load=lambda reg,v:''.join(f's.vr[{reg}][{i}]={v[i]};' for i in range(16))
        body='s.cr=0;'
        for code,value in expected.items():
            body+=load(1,A)+load(2,B)+'s.vscr=0;'+instruction(vx(code,3,0 if code in (0x20e,0x28e,0x24e,0x2ce) else 1,2))
            body+=''.join(f'assert(s.vr[3][{i}]=={value[i]});' for i in range(16))
            if code in sat: body+=f'assert(s.vscr=={int(sat[code])});'
        # Record forms: CR6 all-true 8, none 2, mixed 0; vD may alias vA.
        body+=load(1,A)+load(2,A)+instruction(vx(0x406,1,1,2))+'assert(((s.cr>>4)&15)==8);for(auto x:s.vr[1])assert(x==0xff);'
        body+=load(1,A)+load(2,bytes(x^1 for x in A))+instruction(vx(0x446,3,1,2))+'assert(((s.cr>>4)&15)==2);'
        body+=load(1,A)+load(2,B)+instruction(vx(0x446,3,1,2))+'assert(((s.cr>>4)&15)==0);'
        # Original word lvsl v1,0,r3 at 0x669508 (0x7c20180c); lvsr v1,0,r3.
        for ea in (0x10000,0x10003,0x1000f):
            body+=f'r[3]={ea};'+'{'+lift(0x7c20180c,0x669508).cpp+'}'
            body+=''.join(f'assert(s.vr[1][{i}]=={(ea&15)+i});' for i in range(16))
            body+=f'r[3]={ea};'+'{'+lift(0x7c20184c,0x100).cpp+'}'
            body+=''.join(f'assert(s.vr[1][{i}]=={16-(ea&15)+i});' for i in range(16))
        self.run_native(body)
        self.assertEqual(lift(0x1100108c,0x610b84).mnemonic,'vector_merge')       # original vmrghw
        self.assertEqual(lift(0x11808a4e,0x54f0dc).mnemonic,'vector_unpack')      # original vupkhsh
        self.assertEqual(lift(0x10001406,0x694bf8).mnemonic,'vector_compare_equal')  # original vcmpequb.

    def test_reciprocal_square_root_estimate(self):
        self.assertEqual(lift(0x1020714a,0x54fa74).mnemonic,'vrsqrtefp')         # original word
        values=[(0x40800000,0x3f000000),(0x3e800000,0x40000000),(0x00000000,0x7f800000),(0x80000000,0xff800000),
                (0x7f800000,0x00000000),(0xbf800000,0x7fc00000),(0x7f800001,0x7fc00001),(0xff800000,0x7fc00000)]
        body='s.fpscr=0x82000000;'
        for i in range(0,8,4):
            body+=''.join(f'vmx_word(s.vr[2],{j},0x{v:08x});' for j,(v,_) in enumerate(values[i:i+4]))
            body+=instruction(vx(0x14a,3,0,2))
            body+=''.join(f'assert(vmx_word(s.vr[3],{j})==0x{o:08x}u);' for j,(_,o) in enumerate(values[i:i+4]))
        body+='vmx_word(s.vr[2],0,0x40000000);'+instruction(vx(0x14a,3,0,2))
        body+='{const auto v=vmx_word(s.vr[3],0);assert(v>=0x3f3504f2u && v<=0x3f3504f4u);}'  # 1/sqrt(2) within 1 ulp
        body+='assert(s.fpscr==0x82000000);'
        self.run_native(body)

    def test_float_maximum_and_minimum(self):
        # Original game words: vminfp v0,v0,v13 at 0x55ef04; vmaxfp v18,v1,v30 at 0x5f7290.
        self.assertEqual(lift(0x10006c4a,0x55ef04).mnemonic,'vminfp')
        self.assertEqual(lift(0x1241f40a,0x5f7290).mnemonic,'vmaxfp')
        cases=[(0x3f800000,0x40000000,0x40000000,0x3f800000),   # 1,2
               (0xbf800000,0x3f800000,0x3f800000,0xbf800000),   # -1,1
               (0xc0000000,0xbf800000,0xbf800000,0xc0000000),   # -2,-1
               (0x80000000,0x00000000,0x00000000,0x80000000),   # -0,+0
               (0x00000000,0x80000000,0x00000000,0x80000000),   # +0,-0
               (0x00000001,0x80000001,0x00000001,0x80000001),   # denormals honoured (NJ=0)
               (0x7f800000,0x7f7fffff,0x7f800000,0x7f7fffff),   # +inf
               (0xff800000,0xc0000000,0xc0000000,0xff800000),   # -inf
               (0x7f800001,0x7fc00002,0x7fc00001,0x7fc00001),   # SNaN vA wins, quieted
               (0x3f800000,0xffa00000,0xffe00000,0xffe00000)]   # NaN vB quieted
        body='s.fpscr=0x82000000;'
        for i in range(0,len(cases),4):
            group=cases[i:i+4]+[cases[0]]*(4-len(cases[i:i+4]))
            for lane,(a,b,_,_) in enumerate(group):
                body+=f'vmx_word(s.vr[1],{lane},0x{a:08x});vmx_word(s.vr[2],{lane},0x{b:08x});'
            body+=instruction(vx(0x40a,3,1,2))+instruction(vx(0x44a,4,1,2))
            for lane,(_,_,high,low) in enumerate(group):
                body+=f'assert(vmx_word(s.vr[3],{lane})==0x{high:08x}u && vmx_word(s.vr[4],{lane})==0x{low:08x}u);'
        body+='assert(s.fpscr==0x82000000);'           # VMX does not touch FPSCR
        body+='for(unsigned i=0;i<4;++i){vmx_word(s.vr[1],i,0x3f800000);vmx_word(s.vr[2],i,0x40000000);}'
        body+=instruction(vx(0x40a,1,1,2))           # vD aliases vA
        body+='for(unsigned i=0;i<4;++i)assert(vmx_word(s.vr[1],i)==0x40000000);'
        body+='s.vscr=0x10000;fault([&]{auto& state=s;'+lift(vx(0x44a,3,1,2),0x100).cpp+'});'
        self.run_native(body)

    def test_loads_splats_permutation_and_aliases(self):
        body='for(unsigned i=0;i<32;++i)mem.store(0x10100+i,1,i);r[0]=0xdeadbeef;'
        for offset in range(16):
            body+=f'r[2]=0x10100+{offset};'
            body+=instruction((31<<26)|(3<<21)|(2<<11)|(519<<1))
            body+=f'for(unsigned i=0;i<16;++i)assert(s.vr[3][i]==(i+{offset}<16?i+{offset}:0));'
            body+=instruction((31<<26)|(3<<21)|(2<<11)|(551<<1))
            body+=f'for(unsigned i=0;i<16;++i)assert(s.vr[3][i]==(i<16-{offset}?0:i+{offset}-16));'
            body+=instruction((31<<26)|(3<<21)|(2<<11)|(71<<1))
            body+=f'for(unsigned i={offset&12};i<{(offset&12)+4};++i)assert(s.vr[3][i]==i);'
        body+='r[2]=0;'
        body+=instruction((31<<26)|(3<<21)|(2<<11)|(551<<1))
        body+='for(auto b:s.vr[3])assert(b==0);' # Aligned lvrx must not read address zero.
        body+='for(unsigned i=0;i<16;++i){s.vr[1][i]=i;s.vr[2][i]=i+16;s.vr[3][i]=31-i;}'
        body+=instruction(vx(43,3)|(3<<6))
        body+='for(unsigned i=0;i<16;++i)assert(s.vr[3][i]==31-i);'
        body+=instruction(vx(44,1)|(7<<6))
        body+='for(unsigned i=0;i<16;++i)assert(s.vr[1][i]==7+i);'
        body+=instruction(vx(0x28c,1,2,1))
        body+='for(unsigned i=0;i<16;++i)assert(s.vr[1][i]==15+i%4);'
        body+=instruction(vx(0x38c,1,31,0))
        body+='for(auto b:s.vr[1])assert(b==255);'
        self.run_native(body)

    def test_lane_arithmetic_comparison_and_condition_fields(self):
        values=(0x80000001,0xffffffff,0x12345678,0x7fffffff)
        counts=(0,31,32,255)
        body='s.cr=0xabcdef01;'
        for code in (0x80,0x480,0x184,0x284,0x384,0x86,0x286,0x386):
            for i,(a,b) in enumerate(zip(values,counts)):
                body+=f'vmx_word(s.vr[1],{i},UINT32_C({a}));vmx_word(s.vr[2],{i},{b});'
            body+=instruction(vx(code,1))
            for i,(a,b) in enumerate(zip(values,counts)):
                signed=a-(1<<32) if a>>31 else a
                expected={0x80:a+b,0x480:a-b,0x184:a<<(b&31),0x284:a>>(b&31),0x384:signed>>(b&31),
                          0x86:0xffffffff if a==b else 0,0x286:0xffffffff if a>b else 0,0x386:0xffffffff if signed>b else 0}[code]&0xffffffff
                body+=f'assert(vmx_word(s.vr[1],{i})==UINT32_C({expected}));'
            body+='assert(s.cr==0xabcdef01);'
        body+=instruction(vx(0x486,3,2,2))
        body+='assert(s.cr==0xabcdef81);'
        self.run_native(body)

    def test_float_comparison_conversion_fusion_and_status_isolation(self):
        body='s.fpscr=0xa0000001;s.cr=0x12345678;'
        body+='const std::uint32_t a[]={0,0x80000000,0x7fc00000,0x7f800000};const std::uint32_t b[]={0x80000000,0,0x3f800000,0xff800000};'
        body+='for(unsigned i=0;i<4;++i){vmx_word(s.vr[1],i,a[i]);vmx_word(s.vr[2],i,b[i]);}'
        body+=instruction(vx(0x4c6))
        body+='assert(vmx_word(s.vr[3],0)==UINT32_MAX && vmx_word(s.vr[3],1)==UINT32_MAX && vmx_word(s.vr[3],2)==0 && vmx_word(s.vr[3],3)==0 && s.cr==0x12345608);'
        body+='const std::uint32_t conversion[]={0xbf800000,0x4f800000,0x7fc00000,0x3fe00000};for(unsigned i=0;i<4;++i)vmx_word(s.vr[2],i,conversion[i]);'
        body+=instruction(vx(0x38a,2,0,2))
        body+='assert(vmx_word(s.vr[2],0)==0 && vmx_word(s.vr[2],1)==UINT32_MAX && vmx_word(s.vr[2],2)==0 && vmx_word(s.vr[2],3)==1 && s.vscr==1);'
        body+='for(unsigned i=0;i<4;++i)vmx_word(s.vr[2],i,0xfffffffd);'
        body+=instruction(vx(0x34a,2,1,2))
        body+='for(unsigned i=0;i<4;++i)assert(vmx_word(s.vr[2],i)==0xbfc00000);'
        # (1+2^-23)*(1-2^-23)-1 is -2^-46 only when the multiply is fused.
        body+='for(unsigned i=0;i<4;++i){vmx_word(s.vr[1],i,0x3f800001);vmx_word(s.vr[2],i,0xbf800000);vmx_word(s.vr[3],i,0x3f7ffffe);}'
        body+=instruction(vx(46,1,1,2)|(3<<6))
        body+='for(unsigned i=0;i<4;++i)assert(vmx_word(s.vr[1],i)==0xa8800000);assert(s.fpscr==0xa0000001 && s.vscr==1);'
        body+=instruction(vx(0x604,4,0,0))
        body+='assert(vmx_word(s.vr[4],3)==1 && vmx_word(s.vr[4],0)==0);vmx_word(s.vr[4],3,0);'
        body+=instruction(vx(0x644,0,0,4))
        body+='assert(s.vscr==0);'
        body+='const std::uint32_t reciprocal[]={0x40400000,0xc0400000,0x80000000,0x7f800000};for(unsigned i=0;i<4;++i)vmx_word(s.vr[2],i,reciprocal[i]);'
        body+=instruction(vx(0x10a,2,0,2))
        body+='assert(vmx_word(s.vr[2],0)==0x3eaaaaab && vmx_word(s.vr[2],1)==0xbeaaaaab && vmx_word(s.vr[2],2)==0xff800000 && vmx_word(s.vr[2],3)==0 && s.fpscr==0xa0000001);'
        body+='''{
 VmxFloatScope scope(s);
 assert(vmx_fused(s,0x3fc00000,0,0xff800000,false)==0xff800000);
 assert(vmx_fused(s,0x3fc00000,0,0xff800000,true)==0x7f800000);
 assert(vmx_fused(s,0,0,0x7f800000,false)==0x7fc00000);
 assert(vmx_fused(s,0x7f800000,0xff800000,0x3f800000,false)==0x7fc00000);
 assert(vmx_fused(s,0x7f800001,0x7fc00002,0x7fc00003,false)==0x7fc00001);
 assert(vmx_add(s,0x7f800000,0xff800000,false)==0x7fc00000);
 assert(vmx_add(s,0x3f800000,0xff800000,true)==0x7f800000);
 }assert(s.fpscr==0xa0000001);
'''
        self.run_native(body)
