"""Independent bit/rational oracles for SPU FP and observable DMA fast paths."""
from fractions import Fraction
import random
import shutil
import unittest

from anyps3.spu import lift
from tests import test_runtime_objects, test_spu_float


def rational(bits):
    exponent = (bits >> 23) & 255
    if not exponent:
        return Fraction(0)
    value = Fraction(0x800000 | (bits & 0x7fffff)) * Fraction(2) ** (exponent - 150)
    return -value if bits >> 31 else value


def expected(a, b, c, kind):
    """Exact rational arithmetic, independent of the runtime's fixed limbs/SIMD."""
    x, y, z = map(rational, (a, b, c))
    value = (x+y, x-y, x*y, x*y+z, x*y-z, z-x*y)[kind]
    flag = int(any(((v >> 23) & 255) == 255 or
                   (not (v & 0x7f800000) and (v & 0x7fffff))
                   for v in ((a, b) if kind < 3 else (a, b, c))))
    if not value:
        return 0, flag
    sign = 0x80000000 if value < 0 else 0
    value = abs(value)
    if value < Fraction(2) ** -126:
        return 0, flag | 3
    if value > rational(0x7fffffff):
        return sign | 0x7fffffff, flag | 5
    exponent = value.numerator.bit_length() - value.denominator.bit_length()
    significand = int(value / Fraction(2) ** (exponent - 23))
    return sign | ((exponent + 127) << 23) | (significand & 0x7fffff), flag | int(exponent == 128)


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPerfFloatTests(unittest.TestCase):
    def test_mxcsr_isolation_rz_ftz_daz_sticky_flags_and_aliases(self):
        body = r'''
#if defined(__x86_64__) || defined(__i386__)
 const auto saved=_mm_getcsr();
 for(unsigned rounding=0;rounding<4;++rounding)for(unsigned flush=0;flush<4;++flush)for(unsigned masks=0;masks<2;++masks){
  const unsigned host=(rounding<<13)|((flush&1)?0x8000:0)|((flush&2)?0x40:0)|(masks?0x1f80:0)|0x25;
  _mm_setcsr(host);
'''
        for target in (1, 2, 3):
            body += 't.fpscr={};spu_word(t.fpscr,3,0xf00);'
            body += test_spu_float.words(1, (0x3f800000, 0xbf800000, 0, 0x00800000))
            body += test_spu_float.words(2, (0x33c00000, 0xb3c00000, 0x80000000, 0x00800000))
            body += '{auto& g=t.gpr;'+lift(0x100, test_spu_float.rr(0x2c4, target))+'}'
            body += test_spu_float.expect(target, (0x3f800000, 0xbf800000, 0, 0x01000000))
            body += 'assert(_mm_getcsr()==host && spu_word(t.fpscr,0)==0 && spu_word(t.fpscr,3)==0xf00);'
        for target in (1, 2, 3, 4):
            body += test_spu_float.words(1, (0x3f800001, 0x00800000, 0x7f7fffff, 0))
            body += test_spu_float.words(2, (0x3f7ffffe, 0x3f000000, 0x40000000, 0x80000000))
            body += test_spu_float.words(3, (0xbf800000, 0x00800000, 0xff7fffff, 0x80000000))
            body += '{auto& g=t.gpr;'+lift(0x100, test_spu_float.rrr(0xe, target))+'}'
            body += test_spu_float.expect(target, (0xa8800000, 0x00c00000, 0x7f7fffff, 0))
            body += 'assert(_mm_getcsr()==host);'
        body += r'''
  _mm_setcsr(saved);
 }
#endif
'''
        test_spu_float.SpuFloatTests().run_native(body)

    def test_fixed_overflow_underflow_extended_and_per_lane_fpscr(self):
        body = 't.fpscr={};'
        # SPU table Smin/Smax; IEEE finite maximum is not the SPU limit.
        body += test_spu_float.words(1, (0x7f7fffff, 0x7f7fffff, 0x00800001, 0x7fffffff))
        body += test_spu_float.words(2, (0x73000000, 0x73800000, 0x80800000, 0x7fffffff))
        body += '{auto& g=t.gpr;'+lift(0x100, test_spu_float.rr(0x2c4))+'}'
        body += test_spu_float.expect(3, (0x7f7fffff, 0x7f800000, 0, 0x7fffffff))
        body += test_spu_float.expect(0, (0, 0, 0, 0))  # unrelated register unchanged
        body += 'assert(spu_word(t.fpscr,0)==0 && spu_word(t.fpscr,1)==1 && spu_word(t.fpscr,2)==3 && spu_word(t.fpscr,3)==5);'
        body += 't.fpscr={};'
        body += test_spu_float.words(1, (0x80800000, 0x7f800000, 0xff800001, 1))
        body += test_spu_float.words(2, (0x3f000000, 0, 0x3f800000, 0x3f800000))
        body += '{auto& g=t.gpr;'+lift(0x100, test_spu_float.rr(0x2c6))+'}'
        body += test_spu_float.expect(3, (0, 0, 0xff800001, 0))
        body += 'assert(spu_word(t.fpscr,0)==3 && spu_word(t.fpscr,1)==1 && spu_word(t.fpscr,2)==1 && spu_word(t.fpscr,3)==1);'
        test_spu_float.SpuFloatTests().run_native(body)

    def test_rational_corpus_all_six_operations_and_fpscr(self):
        rng = random.Random(0x535055)
        special = [0, 0x80000000, 1, 0x807fffff, 0x00800000, 0x00800001,
                   0x3f800000, 0x3f800001, 0x3f7ffffe, 0x7f7fffff,
                   0x7f800000, 0x7fffffff, 0xff800000, 0xffffffff]
        cases = []
        for kind in range(6):
            for i in range(400):
                args = [rng.getrandbits(32) if i % 3 else rng.choice(special) for _ in range(3)]
                result, flag = expected(*args, kind)
                cases.append((*args, kind, result, flag))
        table = ',\n'.join('{'+','.join(f'0x{x:08x}u' for x in case)+'}' for case in cases)
        body = 'struct Case{std::uint32_t a,b,c,kind,result,flag;};const Case cases[]={'+table+'};\n'
        body += r'''
 for(unsigned start=0;start<2400;start+=4){
  SpuRegister a{},b{},c{},flags{};
  for(unsigned i=0;i<4;++i){const auto& v=cases[start+i];spu_word(a,i,v.a);spu_word(b,i,v.b);spu_word(c,i,v.c);spu_word(flags,i,0xa00);}
  const auto result=spu_single_vector(flags,a,b,c,cases[start].kind);
  for(unsigned i=0;i<4;++i){const auto& v=cases[start+i];assert(spu_word(result,i)==v.result);assert(spu_word(flags,i)==(0xa00|v.flag));}
 }
'''
        test_spu_float.SpuFloatTests().run_native(body)


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPerfDmaTests(unittest.TestCase):
    def test_contiguous_alias_observer_reservations_cache_and_permission_errors(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 mem.regions.push_back({0x30000,6,mem.regions[0].bytes});
 for(unsigned i=0;i<32;++i)mem.store(0x10100+i,1,0xa0+i);
 std::vector<std::pair<unsigned,bool>> observed;
 mem.host_access_observer=[&](const void*,std::uint64_t size,bool write){observed.push_back({unsigned(size),write});};
 t.dma={0x200,0,0x30100,32,31};spu_write_channel(s,t,ls,21,0x40);
 assert(observed.size()==1 && observed[0].first==32 && !observed[0].second);observed.clear();
 const auto reservation=mem.reservation_token(0x10100);observed.clear();
 for(unsigned i=0;i<32;++i){assert(ls[0x200+i]==0xa0+i);ls[0x200+i]=0x40+i;}
 spu_write_channel(s,t,ls,21,0x20);
 assert(observed.size()==1 && observed[0].first==32 && observed[0].second);observed.clear();
 assert(mem.reservation_token(0x10100)!=reservation);
 for(unsigned i=0;i<32;++i)assert(mem.load(0x10100+i,1)==0x40+i);
 // Cache is an index, not a stale pointer: replacing a backing is observed.
 const auto index=mem.last_region;mem.regions[index].bytes=std::make_shared<GuestBacking>(0x2000,0xdd);
 spu_transfer_main(mem,0x10100,ls+0x200,32,false);assert(ls[0x200]==0xdd);
 mem.host_access_observer={};
 mem.map(0x40000,32,4,nullptr,0);mem.map(0x41000,32,7,nullptr,0);
 for(const auto address:{0x40000u,0x41000u}){
  t.dma={0x200,0,address,32,0};fault([&]{spu_write_channel(s,t,ls,21,0x20);});assert(mem.load(address,1)==0);
 }
 t.dma={0x200,1,0x10000,32,0};fault([&]{spu_write_channel(s,t,ls,21,0x40);});
 t.dma={0x200,0,0x10100,32,32};fault([&]{spu_write_channel(s,t,ls,21,0x40);});
''')

    def test_atomic_get_conditional_unconditional_alias_invalidation_and_trace(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 mem.regions.push_back({0x30000,6,mem.regions[0].bytes});
 for(unsigned i=0;i<128;++i)mem.store(0x10100+i,1,i);
 t.dma={0x200,0,0x10100,128,0};spu_write_channel(s,t,ls,21,0xd0);
 assert(spu_read_channel(s,t,27)==4 && t.reservation_address==0x10100);
 for(unsigned i=0;i<128;++i){assert(ls[0x200+i]==i);ls[0x200+i]=255-i;}
 spu_write_channel(s,t,ls,21,0xb4);assert(spu_read_channel(s,t,27)==0 && t.reservation_address==UINT64_MAX);
 assert(mem.load(0x30100,4)==0xfffefdfc);
 spu_write_channel(s,t,ls,21,0xd0);assert(spu_read_channel(s,t,27)==4);
 mem.store(0x30100,4,0x12345678);ls[0x200]=0xaa;
 spu_write_channel(s,t,ls,21,0xb4);assert(spu_read_channel(s,t,27)==1 && mem.load(0x10100,4)==0x12345678);
 spu_write_channel(s,t,ls,21,0xb0);assert(spu_read_channel(s,t,27)==2 && mem.load(0x10100,4)==0xaafefdfc);
 const auto before=anyps3_trace_history.written;
 for(unsigned i=0;i<520;++i){t.pc=i;spu_trace_dma(t,"GET",0x12345678,0x200,16,31);}
 assert(anyps3_trace_history.written==before+520);
 const auto* last=anyps3_trace_history.lines[(anyps3_trace_history.written-1)%512].data();
 assert(std::string(last)=="SPU DMA GET ea=0000000012345678 ls=00200 size=0010 tag=1f spu=00000000 pc=00207\n");
 fault([&]{spu_write_channel(s,t,ls,21,0x99);});
''')

    def test_profile_counts_exact_work_tag_completion_and_lists(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;spu_profile.enabled=true;spu_profile_on=true;spu_profile.category={};spu_profile.fp_fast_lanes=0;spu_profile.fp_slow_lanes=0;
 spu_profile.dma_commands=spu_profile.dma_bytes=spu_profile.dma_elements=0;
 NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 for(unsigned i=0;i<32;++i)mem.store(0x10100+i,1,0xa0+i);
 t.dma={0x200,0,0x10100,16,0};spu_write_channel(s,t,ls,21,0x40);
 spu_write_channel(s,t,ls,22,0x80000001);spu_write_channel(s,t,ls,23,2);
 assert(spu_channel_count(s,t,24)==1 && spu_read_channel(s,t,24)==0x80000001 && spu_channel_count(s,t,24)==0);
 // BE descriptors: 4-byte element advances next local start to a quadword.
 const std::uint8_t list[]={0,0,0,4,0,1,1,0, 0,0,0,16,0,1,1,16};std::copy_n(list,16,ls+0x400);
 t.dma={0x300,0,0x400,16,1};spu_write_channel(s,t,ls,21,0x44);
 assert(spu_load_word(ls+0x300)==0xa0a1a2a3 && spu_load_word(ls+0x310)==0xb0b1b2b3);
 t.dma={0x300,0,0x400,0,1};spu_write_channel(s,t,ls,21,0x44);
 assert(spu_profile.dma_commands==3 && spu_profile.dma_bytes==36 && spu_profile.dma_elements==3);
 SpuRegister a{},b{};for(unsigned i=0;i<4;++i){spu_word(a,i,0x3f800000);spu_word(b,i,0x3f800000);}
 const auto sum=spu_single_vector(t.fpscr,a,b,SpuRegister{},0);assert(spu_word(sum,0)==0x40000000);
#if defined(__x86_64__)
 assert(spu_profile.fp_fast_lanes==4 && spu_profile.fp_slow_lanes==0);
#endif
 spu_word(a,0,1);(void)spu_single_vector(t.fpscr,a,b,SpuRegister{},0);
 assert(spu_profile.fp_slow_lanes>=1 && spu_profile.category[SpuProfile::FP].calls==2);
 spu_profile.at=std::chrono::steady_clock::now()-std::chrono::seconds(3);spu_profile.polls=1023;
 const auto at=spu_profile.at;spu_profile_report();assert(spu_profile.at>at); // periodic path
 assert(spu_profile.category[SpuProfile::DMA].calls==3 && spu_profile.category[SpuProfile::DMA].ns>0);
 spu_profile.enabled=false;spu_profile_on=false;
''')
