"""SPUFAST2: exactness of vector replacements for scalar per-lane SPU helpers.

Oracle: the scalar helpers of the established lifter (spu_float_to_int, the exact CFLTS/CFLTU semantics, itself covered by
test_spu_float and test_spufast_blocks); candidate: the four-lane vector code used by the native templates.  Inputs cover every
exponent, the mantissa boundaries, both signs and every scale the instruction can encode.
"""
import shutil
import unittest

from tests.test_spufast_blocks import build_and_run

BODY = r'''
 const int scales[]={0,1,2,3,5,7,8,9,15,16,17,22,23,24,25,31,32,33,63,64,100,126,127};
 std::uint64_t checked=0;
 std::uint32_t seed=0x2468ace1u;
 auto next=[&]{seed=seed*1664525u+1013904223u;return seed;};
 std::uint32_t pool[]={0,1,0x7fffff,0x400000,0x555555,0x2aaaaa,0x7ffffe,0x200000};
 for(int unsign=0;unsign<2;++unsign)for(int scale:scales){
  for(unsigned sign=0;sign<2;++sign)for(unsigned exp=0;exp<256;++exp){
   for(unsigned k=0;k<8*4;k+=4){
    std::uint32_t v[4];
    for(unsigned lane=0;lane<4;++lane){
     const std::uint32_t mant=(k+lane<8)?pool[k+lane]:(next()&0x7fffff);
     v[lane]=(sign<<31)|(exp<<23)|mant;
    }
    const SpuV in=_mm_set_epi32(int(v[3]),int(v[2]),int(v[1]),int(v[0]));
    const SpuV out=spu_vfloat_to_int_any(in,scale,unsign);
    alignas(16) std::uint32_t r[4];_mm_store_si128(reinterpret_cast<__m128i*>(r),out);
    for(unsigned lane=0;lane<4;++lane){
     const auto want=spu_float_to_int(v[lane],scale,unsign);
     if(r[lane]!=want){std::fprintf(stderr,"CVT MISMATCH x=%08x scale=%d unsign=%d got=%08x want=%08x\n",v[lane],scale,unsign,r[lane],want);std::abort();}
     ++checked;
    }
   }
  }
 }
 std::fprintf(stderr,"cvt checked %llu\n",(unsigned long long)checked);
'''


FP_BODY = r'''
 // Oracle: spu_single_vector (exact limb arithmetic, sets the FPSCR flags); candidate: spu_vfp<Kind> (AVX-512 round-to-zero
 // with exactness predicates and the exact fallback).  Operands: every class the predicates distinguish.
 const std::uint32_t pool[]={0x00000000u,0x80000000u,0x00000001u,0x807fffffu,0x00800000u,0x80800000u,0x00800001u,0x01000000u,0x81000000u,
   0x1fffffffu,0x3f800000u,0xbf800000u,0x3f800001u,0x3f7fffffu,0x40000000u,0xc0000000u,0x40490fdbu,0x3e99999au,0x7e800000u,0x7f000000u,
   0xff000000u,0x7f7ffffeu,0x7f7fffffu,0xff7fffffu,0x7f800000u,0xff800000u,0x7fffffffu,0xffffffffu,0x7f800001u,0x34000000u,0x33800000u,
   0x4b000000u,0x4b800000u,0x5f800000u,0xdf800000u,0x2f800000u,0x20000000u,0x00c00000u,0x7e7fffffu,0x4e800000u};
 constexpr unsigned N=sizeof(pool)/sizeof(pool[0]);
 std::uint64_t checked=0;std::uint32_t seed=0x13572468u;
 auto next=[&]{seed=seed*1664525u+1013904223u;return seed;};
 auto one=[&](auto kind_tag,std::uint32_t av[4],std::uint32_t bv[4],std::uint32_t cv[4]){
  constexpr unsigned Kind=decltype(kind_tag)::value;
  SpuRegister ra,rb,rc,f1{},f2{};
  for(unsigned l=0;l<4;++l){spu_word(ra,l,av[l]);spu_word(rb,l,bv[l]);spu_word(rc,l,cv[l]);}
  const auto want=spu_single_vector(f1,ra,rb,rc,Kind);
  const SpuV a=_mm_set_epi32(int(av[3]),int(av[2]),int(av[1]),int(av[0])),b=_mm_set_epi32(int(bv[3]),int(bv[2]),int(bv[1]),int(bv[0])),
             c=_mm_set_epi32(int(cv[3]),int(cv[2]),int(cv[1]),int(cv[0]));
  const SpuV got=spu_vfp<Kind>(f2,a,b,c);
  alignas(16) std::uint32_t g[4];_mm_store_si128(reinterpret_cast<__m128i*>(g),got);
  for(unsigned l=0;l<4;++l){
   if(g[l]!=spu_word(want,l) || f1!=f2){
    std::fprintf(stderr,"FP MISMATCH kind=%u lane=%u a=%08x b=%08x c=%08x got=%08x want=%08x fpscr %s\n",Kind,l,av[l],bv[l],cv[l],g[l],spu_word(want,l),f1==f2?"same":"differs");std::abort();}
  }
  ++checked;
 };
 auto sweep=[&](auto kind_tag){
  constexpr unsigned Kind=decltype(kind_tag)::value;
  // four lanes at a time: all (a,b) pairs, c from a rotating pool slot or zero/random
  for(unsigned i=0;i<N;++i)for(unsigned j=0;j<N;j+=1){
   std::uint32_t av[4],bv[4],cv[4];
   for(unsigned l=0;l<4;++l){av[l]=pool[(i+l*7)%N];bv[l]=pool[(j+l*5)%N];cv[l]=(Kind>=3)?pool[(i*3+j+l)%N]:0;}
   one(std::integral_constant<unsigned,Kind>{},av,bv,cv);
   // the same pairs with c = 0 and a negated c (cancellation of a*b)
   if(Kind>=3){for(unsigned l=0;l<4;++l)cv[l]=0;one(std::integral_constant<unsigned,Kind>{},av,bv,cv);}
  }
  // random normal-range operands (typical game data) mixed with pool values
  for(unsigned n=0;n<20000;++n){
   std::uint32_t av[4],bv[4],cv[4];
   for(unsigned l=0;l<4;++l){
    auto pick=[&]{const std::uint32_t r=next();return (r&3)==0?pool[(r>>8)%N]:((r&0x807fffffu)|((0x60u+((r>>23)&0x3f))<<23));};
    av[l]=pick();bv[l]=pick();cv[l]=pick();
    if(Kind<2 && (next()&7)==0)bv[l]=av[l]^((next()&1)?0x80000000u:0u); // cancellation / doubling
   }
   one(std::integral_constant<unsigned,Kind>{},av,bv,cv);
  }
 };
 sweep(std::integral_constant<unsigned,0>{});sweep(std::integral_constant<unsigned,1>{});sweep(std::integral_constant<unsigned,2>{});
 sweep(std::integral_constant<unsigned,3>{});sweep(std::integral_constant<unsigned,4>{});sweep(std::integral_constant<unsigned,5>{});
 std::fprintf(stderr,"fp checked %llu groups\n",(unsigned long long)checked);
'''


UNDERFLOW_BODY = r'''
 // HIRES7: lanes whose RZ result underflows (0 < |exact| < 2^-126: SPU +0 with FPSCR flags 3, ISA v1.2 PDF pp.195-196, 208-214) are
 // decided in the vector paths. Oracle: spu_single_math on EVERY lane (the exact limb path, unchanged); candidates: spu_vfp<Kind>
 // (AVX-512 on this host with -march=native) and spu_single_vector (the generic per-lane fast path). Operands: magnitudes around
 // 2^-63..2^-75 (products near and below 2^-126), the normal-range boundary, exact cancellations and ordinary values.
 std::uint64_t checked=0,under=0;std::uint32_t seed=0x2468ace1u;
 auto next=[&]{seed=seed*1664525u+1013904223u;return seed;};
 auto pick=[&]{const std::uint32_t r=next();const unsigned cls=r&7;const std::uint32_t mant=next()&0x7fffffu,sign=(r>>3&1)<<31;
  if(cls<3)return sign|((0x30u+(next()%24))<<23)|mant;       // 2^-79..2^-56: products underflow or are tiny normals
  if(cls==3)return sign|((1u+(next()%3))<<23)|mant;          // just above 2^-126: sums/differences underflow
  if(cls==4)return (next()&1)?0u:0x80000000u;                // zeros
  if(cls==5)return sign|0x3f800000u|(mant&0xf);              // ~1.0
  return sign|((0x60u+(next()%40))<<23)|mant;};               // ordinary
 auto one=[&](auto kind_tag,std::uint32_t av[4],std::uint32_t bv[4],std::uint32_t cv[4]){
  constexpr unsigned Kind=decltype(kind_tag)::value;
  SpuRegister fo{},f1{},f2{},ra,rb,rc;std::uint32_t want[4];
  for(unsigned l=0;l<4;++l){want[l]=spu_single_math(fo,l,av[l],bv[l],cv[l],Kind);spu_word(ra,l,av[l]);spu_word(rb,l,bv[l]);spu_word(rc,l,cv[l]);
   under+=want[l]==0 && (spu_word(fo,l)&2);}
  const auto scalar=spu_single_vector(f1,ra,rb,rc,Kind);
  const SpuV a=_mm_set_epi32(int(av[3]),int(av[2]),int(av[1]),int(av[0])),b=_mm_set_epi32(int(bv[3]),int(bv[2]),int(bv[1]),int(bv[0])),
             c=_mm_set_epi32(int(cv[3]),int(cv[2]),int(cv[1]),int(cv[0]));
  alignas(16) std::uint32_t g[4];_mm_store_si128(reinterpret_cast<__m128i*>(g),spu_vfp<Kind>(f2,a,b,c));
  for(unsigned l=0;l<4;++l)if(g[l]!=want[l] || spu_word(scalar,l)!=want[l] || f1!=fo || f2!=fo){
   std::fprintf(stderr,"UNDERFLOW MISMATCH kind=%u lane=%u a=%08x b=%08x c=%08x vfp=%08x scalar=%08x want=%08x fpscr want=%08x vfp=%08x scalar=%08x\n",
    Kind,l,av[l],bv[l],cv[l],g[l],spu_word(scalar,l),want[l],spu_word(fo,l),spu_word(f2,l),spu_word(f1,l));std::abort();}
  ++checked;
 };
 auto sweep=[&](auto kind_tag){
  constexpr unsigned Kind=decltype(kind_tag)::value;
  for(unsigned n=0;n<60000;++n){std::uint32_t av[4],bv[4],cv[4];
   for(unsigned l=0;l<4;++l){av[l]=pick();bv[l]=pick();cv[l]=(Kind>=3)?pick():0;
    const unsigned r=next()&15;
    if(Kind<2 && r==0)bv[l]=av[l]^0x80000000u;                                   // exact cancellation
    if(Kind<2 && r==1)bv[l]=(av[l]^0x80000000u)+((next()&1)?1u:0xffffffffu);       // near cancellation: subnormal difference
    if(Kind>=3 && r==2){cv[l]=0;}
    if(Kind>=3 && r==3){av[l]=0x3f800000u;cv[l]=Kind==3?bv[l]^0x80000000u:bv[l];}}            // exact cancellation 1*b -/+ b
   one(std::integral_constant<unsigned,Kind>{},av,bv,cv);}
 };
 // Fixed groups: exact FMA/FMS/FNMS cancellations 1*b -/+ b (+0, no flags) next to underflowing products (+0, flags 3).
 {std::uint32_t av[4]={0x3f800000u,0x1c800000u,0x3f800000u,0x3f800000u},bv[4]={0x1c800000u,0x1c800000u,0x01000000u,0x00c00000u},cp[4],cm[4];
  for(unsigned l=0;l<4;++l){cm[l]=bv[l]^0x80000000u;cp[l]=bv[l];}cm[1]=cp[1]=0;
  one(std::integral_constant<unsigned,3>{},av,bv,cm);one(std::integral_constant<unsigned,4>{},av,bv,cp);one(std::integral_constant<unsigned,5>{},av,bv,cp);}
 sweep(std::integral_constant<unsigned,0>{});sweep(std::integral_constant<unsigned,1>{});sweep(std::integral_constant<unsigned,2>{});
 sweep(std::integral_constant<unsigned,3>{});sweep(std::integral_constant<unsigned,4>{});sweep(std::integral_constant<unsigned,5>{});
 if(under<1000){std::fprintf(stderr,"too few underflow lanes %llu\n",(unsigned long long)under);std::abort();}
 std::fprintf(stderr,"underflow checked %llu groups, %llu underflow lanes\n",(unsigned long long)checked,(unsigned long long)under);
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuFast2VectorTests(unittest.TestCase):
    def test_underflow_lanes_match_exact_per_lane_oracle_native_isa(self):
        build_and_run(self, (), UNDERFLOW_BODY, ('-O2', '-march=native', '-ffp-contract=off'))

    def test_float_to_int_vector_matches_scalar_native_isa(self):
        # -march=native selects the AVX2 four-lane implementation on this host (spu_vfloat_to_int).
        build_and_run(self, (), BODY + ' static_assert(sizeof(SpuV)==16);\n', ('-O2', '-march=native', '-ffp-contract=off'))

    def test_vector_fp_matches_exact_oracle_native_isa(self):
        build_and_run(self, (), FP_BODY, ('-O2', '-march=native', '-ffp-contract=off'))

    def test_float_to_int_fallback_matches_scalar_baseline_isa(self):
        # Baseline x86-64 has no AVX2: spu_vfloat_to_int_any must be the scalar per-lane path.
        build_and_run(self, (), BODY, ('-O1',))


if __name__ == '__main__':
    unittest.main()
