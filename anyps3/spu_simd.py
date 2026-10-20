"""SPUFAST S2/S3: in-register SPU semantics for translated superblocks.

Registers keep the architectural big-endian byte layout in ``t.gpr``. Inside a
validated superblock (anyps3/spu_block.py) values live in host vector locals in
one of three representations:

* ``b``: architectural BE bytes (LS/GPR layout; byte operations, shuffles),
* ``w``: every 32-bit lane host-endian, lanes in architectural order,
* ``h``: every 16-bit lane host-endian, lanes in architectural order,

or as a compile-time constant (four BE words).  Bitwise operations are valid
in any shared representation.  Every template is a pure function of its
operands; FPSCR is written only by the exact slow path of an unsafe FP lane.
Opcodes without a template keep the original lifted body (anyps3/spu.lift),
with the touched registers written back before and reloaded after it.

References: SPU ISA v1.2 (27 January 2007) PDF, per-instruction pages; the
legacy lifted bodies in spu.lift are the differential oracle in tests.
"""
from .spu_interp import Expr


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _u32(x):
    return x & 0xffffffff


VEC_CPP = r'''
// ---- SPUFAST in-register helpers (x86-64; SSE2 baseline, wider ISA when enabled) ----
#if defined(__SSE2__)
#include <immintrin.h>
using SpuV=__m128i;
#define SPU_VI [[gnu::always_inline]] static inline
SPU_VI SpuV spu_vld(const SpuRegister& r){SpuV v;__builtin_memcpy(&v,r.data(),16);return v;}
SPU_VI void spu_vst(SpuRegister& r,SpuV v){__builtin_memcpy(r.data(),&v,16);}
SPU_VI SpuV spu_vldp(const std::uint8_t* p){SpuV v;__builtin_memcpy(&v,p,16);return v;}
SPU_VI void spu_vstp(std::uint8_t* p,SpuV v){__builtin_memcpy(p,&v,16);}
SPU_VI SpuV spu_vk(std::uint32_t a,std::uint32_t b,std::uint32_t c,std::uint32_t d){return _mm_setr_epi32(int(a),int(b),int(c),int(d));}
// b<->w: byte swap inside every word; b<->h: inside every halfword; w<->h: swap the halfwords of every word.
SPU_VI SpuV spu_vswap32(SpuV v){
#if defined(__SSSE3__)
    return _mm_shuffle_epi8(v,_mm_setr_epi8(3,2,1,0,7,6,5,4,11,10,9,8,15,14,13,12));
#else
    v=_mm_or_si128(_mm_slli_epi16(v,8),_mm_srli_epi16(v,8));
    return _mm_shufflehi_epi16(_mm_shufflelo_epi16(v,0xb1),0xb1);
#endif
}
SPU_VI SpuV spu_vswap16(SpuV v){return _mm_or_si128(_mm_slli_epi16(v,8),_mm_srli_epi16(v,8));}
SPU_VI SpuV spu_vhalves(SpuV v){return _mm_shufflehi_epi16(_mm_shufflelo_epi16(v,0xb1),0xb1);}
SPU_VI SpuV spu_vrev(SpuV v){ // reverse all 16 bytes
#if defined(__SSSE3__)
    return _mm_shuffle_epi8(v,_mm_setr_epi8(15,14,13,12,11,10,9,8,7,6,5,4,3,2,1,0));
#else
    v=spu_vswap16(v);v=_mm_shufflehi_epi16(_mm_shufflelo_epi16(v,0x1b),0x1b);return _mm_shuffle_epi32(v,0x4e);
#endif
}
// Host value of word 0 (w representation) / of BE word 0 (b representation).
SPU_VI std::uint32_t spu_vw0(SpuV w){return std::uint32_t(_mm_cvtsi128_si32(w));}
SPU_VI std::uint32_t spu_vb0(SpuV b){return __builtin_bswap32(std::uint32_t(_mm_cvtsi128_si32(b)));}
// BE register with word 0 = x and zero elsewhere (link registers).
SPU_VI SpuV spu_vlink(std::uint32_t x){return _mm_cvtsi32_si128(int(__builtin_bswap32(x)));}
SPU_VI SpuV spu_vnot(SpuV v){return _mm_xor_si128(v,_mm_set1_epi32(-1));}
SPU_VI SpuV spu_vselb(SpuV a,SpuV b,SpuV c){return _mm_or_si128(_mm_andnot_si128(c,a),_mm_and_si128(c,b));}
SPU_VI SpuV spu_vclgt32(SpuV a,SpuV b){const auto m=_mm_set1_epi32(INT32_MIN);return _mm_cmpgt_epi32(_mm_xor_si128(a,m),_mm_xor_si128(b,m));}
SPU_VI SpuV spu_vclgt16(SpuV a,SpuV b){const auto m=_mm_set1_epi16(INT16_MIN);return _mm_cmpgt_epi16(_mm_xor_si128(a,m),_mm_xor_si128(b,m));}
SPU_VI SpuV spu_vclgt8(SpuV a,SpuV b){const auto m=_mm_set1_epi8(INT8_MIN);return _mm_cmpgt_epi8(_mm_xor_si128(a,m),_mm_xor_si128(b,m));}
template<class F> SPU_VI SpuV spu_vmap32(SpuV w,F f){
    alignas(16) std::uint32_t x[4];_mm_store_si128(reinterpret_cast<__m128i*>(x),w);
    for(unsigned i=0;i<4;++i)x[i]=f(i,x[i]);
    return _mm_load_si128(reinterpret_cast<const __m128i*>(x));
}
template<class F> SPU_VI SpuV spu_vmap32x2(SpuV w,SpuV v,F f){
    alignas(16) std::uint32_t x[4],y[4];_mm_store_si128(reinterpret_cast<__m128i*>(x),w);_mm_store_si128(reinterpret_cast<__m128i*>(y),v);
    for(unsigned i=0;i<4;++i)x[i]=f(i,x[i],y[i]);
    return _mm_load_si128(reinterpret_cast<const __m128i*>(x));
}
template<class F> SPU_VI SpuV spu_vmap16x2(SpuV h,SpuV v,F f){
    alignas(16) std::uint16_t x[8],y[8];_mm_store_si128(reinterpret_cast<__m128i*>(x),h);_mm_store_si128(reinterpret_cast<__m128i*>(y),v);
    for(unsigned i=0;i<8;++i)x[i]=std::uint16_t(f(x[i],y[i]));
    return _mm_load_si128(reinterpret_cast<const __m128i*>(x));
}
// Word shifts with per-lane counts already reduced as the ISA specifies (w representation).
SPU_VI SpuV spu_vshl32(SpuV x,SpuV n){ // n in 0..63, >=32 gives 0
#if defined(__AVX2__)
    return _mm_sllv_epi32(x,n);
#else
    return spu_vmap32x2(x,n,[](unsigned,std::uint32_t a,std::uint32_t c){return c<32?a<<c:0u;});
#endif
}
SPU_VI SpuV spu_vshr32(SpuV x,SpuV n){
#if defined(__AVX2__)
    return _mm_srlv_epi32(x,n);
#else
    return spu_vmap32x2(x,n,[](unsigned,std::uint32_t a,std::uint32_t c){return c<32?a>>c:0u;});
#endif
}
SPU_VI SpuV spu_vsar32(SpuV x,SpuV n){
#if defined(__AVX2__)
    return _mm_srav_epi32(x,n);
#else
    return spu_vmap32x2(x,n,[](unsigned,std::uint32_t a,std::uint32_t c){return std::uint32_t(std::int32_t(a)>>(c<31?c:31));});
#endif
}
SPU_VI SpuV spu_vrot32(SpuV x,SpuV n){ // n in 0..31
#if defined(__AVX512F__) && defined(__AVX512VL__)
    return _mm_rolv_epi32(x,n);
#else
    return _mm_or_si128(spu_vshl32(x,n),spu_vshr32(x,_mm_sub_epi32(_mm_set1_epi32(32),n)));
#endif
}
// Halfword shifts, per-lane counts (h representation).
SPU_VI SpuV spu_vshl16(SpuV x,SpuV n){
#if defined(__AVX512BW__) && defined(__AVX512VL__)
    return _mm_sllv_epi16(x,n);
#else
    return spu_vmap16x2(x,n,[](unsigned a,unsigned c){return c<16?a<<c:0u;});
#endif
}
SPU_VI SpuV spu_vshr16(SpuV x,SpuV n){
#if defined(__AVX512BW__) && defined(__AVX512VL__)
    return _mm_srlv_epi16(x,n);
#else
    return spu_vmap16x2(x,n,[](unsigned a,unsigned c){return c<16?a>>c:0u;});
#endif
}
SPU_VI SpuV spu_vsar16(SpuV x,SpuV n){
#if defined(__AVX512BW__) && defined(__AVX512VL__)
    return _mm_srav_epi16(x,n);
#else
    return spu_vmap16x2(x,n,[](unsigned a,unsigned c){return unsigned(std::int32_t(std::int16_t(a))>>(c<15?c:15));});
#endif
}
// Quadword byte rotate/shift (b representation): 0 rotate n&15, 1 shift toward byte 0, 2 shift away from byte 0.
template<unsigned Kind> SPU_VI SpuV spu_vrotb(SpuV a,unsigned n){
#if defined(__SSSE3__)
    auto index=_mm_setr_epi8(0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15);
    if constexpr(Kind==2)index=_mm_sub_epi8(index,_mm_set1_epi8(char(n)));
    else index=_mm_add_epi8(index,_mm_set1_epi8(char(n)));
    if constexpr(Kind==0)index=_mm_and_si128(index,_mm_set1_epi8(15));
    else index=_mm_or_si128(index,_mm_cmpgt_epi8(index,_mm_set1_epi8(15)));
    return _mm_shuffle_epi8(a,index);
#else
    alignas(16) std::uint8_t x[16],r[16];_mm_store_si128(reinterpret_cast<__m128i*>(x),a);
    for(unsigned i=0;i<16;++i){
        if constexpr(Kind==0)r[i]=x[(i+n)&15];
        else if constexpr(Kind==1)r[i]=i+n<16?x[i+n]:0;
        else r[i]=i>=n?x[i-n]:0;
    }
    return _mm_load_si128(reinterpret_cast<const __m128i*>(r));
#endif
}
// Quadword bit rotate/shift by n in 0..7 (b representation): 0 rotate left, 1 shift left, 2 shift right.
template<unsigned Kind> SPU_VI SpuV spu_vrotbits(SpuV a,unsigned n){
    const SpuV r=spu_vrev(a);const SpuV c=_mm_cvtsi32_si128(int(n)),k=_mm_cvtsi32_si128(int(64-n));
    SpuV x;
    if constexpr(Kind==2)x=_mm_or_si128(_mm_srl_epi64(r,c),_mm_sll_epi64(_mm_srli_si128(r,8),k));
    else{
        x=_mm_or_si128(_mm_sll_epi64(r,c),_mm_srl_epi64(_mm_slli_si128(r,8),k));
        if constexpr(Kind==0)x=_mm_or_si128(x,_mm_srl_epi64(_mm_srli_si128(r,8),k));
    }
    return spu_vrev(x);
}
// SHUFB, ISA v1.2: control 0x80..0xbf -> 0x00, 0xc0..0xdf -> 0xff, 0xe0..0xff -> 0x80.
SPU_VI SpuV spu_vshufb(SpuV a,SpuV b,SpuV c){
#if defined(__AVX512VBMI__) && defined(__AVX512VL__) && defined(__AVX512BW__)
    const SpuV normal=_mm_permutex2var_epi8(a,c,b);
    const SpuV special=_mm_shuffle_epi8(_mm_setr_epi8(0,0,0,0,0,0,-1,-128,0,0,0,0,0,0,0,0),_mm_and_si128(_mm_srli_epi16(c,5),_mm_set1_epi8(7)));
    return _mm_mask_blend_epi8(_mm_movepi8_mask(c),normal,special);
#elif defined(__SSSE3__)
    const auto select_a=_mm_cmpeq_epi8(_mm_and_si128(c,_mm_set1_epi8(16)),_mm_setzero_si128());
    const auto normal=_mm_or_si128(_mm_and_si128(select_a,_mm_shuffle_epi8(a,c)),_mm_andnot_si128(select_a,_mm_shuffle_epi8(b,c)));
    const auto index=_mm_and_si128(_mm_srli_epi16(c,5),_mm_set1_epi8(7));
    return _mm_or_si128(normal,_mm_shuffle_epi8(_mm_setr_epi8(0,0,0,0,0,0,-1,-128,0,0,0,0,0,0,0,0),index));
#else
    SpuRegister x,y,z;spu_vst(x,a);spu_vst(y,b);spu_vst(z,c);return spu_vld(spu_shuffle_scalar(x,y,z));
#endif
}
SPU_VI SpuV spu_vcntb(SpuV a){
#if defined(__AVX512BITALG__) && defined(__AVX512VL__)
    return _mm_popcnt_epi8(a);
#elif defined(__SSSE3__)
    const auto lut=_mm_setr_epi8(0,1,1,2,1,2,2,3,1,2,2,3,2,3,3,4),low=_mm_set1_epi8(15);
    return _mm_add_epi8(_mm_shuffle_epi8(lut,_mm_and_si128(a,low)),_mm_shuffle_epi8(lut,_mm_and_si128(_mm_srli_epi16(a,4),low)));
#else
    alignas(16) std::uint8_t x[16];_mm_store_si128(reinterpret_cast<__m128i*>(x),a);
    for(auto& v:x)v=std::uint8_t(__builtin_popcount(v));
    return _mm_load_si128(reinterpret_cast<const __m128i*>(x));
#endif
}
SPU_VI SpuV spu_vclz(SpuV w){
#if defined(__AVX512CD__) && defined(__AVX512VL__)
    return _mm_lzcnt_epi32(w);
#else
    return spu_vmap32(w,[](unsigned,std::uint32_t x){return x?std::uint32_t(__builtin_clz(x)):32u;});
#endif
}
// 16x16->32 products of halfwords already isolated in 32-bit lanes (zero or sign extended).
SPU_VI SpuV spu_vmul32(SpuV a,SpuV b){
#if defined(__SSE4_1__)
    return _mm_mullo_epi32(a,b);
#else
    return spu_vmap32x2(a,b,[](unsigned,std::uint32_t x,std::uint32_t y){return x*y;});
#endif
}
SPU_VI SpuV spu_vlo16s(SpuV w){return _mm_srai_epi32(_mm_slli_epi32(w,16),16);}
SPU_VI SpuV spu_vlo16u(SpuV w){return _mm_and_si128(w,_mm_set1_epi32(0xffff));}
// SUMB (w result): high half sum of RB bytes, low half sum of RA bytes, per word.
SPU_VI SpuV spu_vsumb(SpuV a,SpuV b){
    const auto mask=_mm_set1_epi32(0x00ff00ff);
    auto sum=[&](SpuV x){const auto pairs=_mm_add_epi16(_mm_and_si128(x,mask),_mm_and_si128(_mm_srli_epi16(x,8),mask));
        return _mm_add_epi32(_mm_and_si128(pairs,_mm_set1_epi32(0xffff)),_mm_srli_epi32(pairs,16));};
    return _mm_or_si128(_mm_slli_epi32(sum(b),16),sum(a));
}
// FSM/FSMH/FSMB masks from the low bits of a scalar.
SPU_VI SpuV spu_vfsm(std::uint32_t x){const auto m=_mm_setr_epi32(8,4,2,1);return _mm_cmpeq_epi32(_mm_and_si128(_mm_set1_epi32(int(x&15)),m),m);}
SPU_VI SpuV spu_vfsmh(std::uint32_t x){const auto m=_mm_setr_epi16(128,64,32,16,8,4,2,1);return _mm_cmpeq_epi16(_mm_and_si128(_mm_set1_epi16(short(x&255)),m),m);}
SPU_VI SpuV spu_vfsmb(std::uint32_t x){
    const auto v=_mm_unpacklo_epi64(_mm_set1_epi8(char((x>>8)&255)),_mm_set1_epi8(char(x&255)));
    const auto m=_mm_setr_epi8(-128,64,32,16,8,4,2,1,-128,64,32,16,8,4,2,1);return _mm_cmpeq_epi8(_mm_and_si128(v,m),m);
}
// CBD/CHD/CWD/CDD insertion controls, index [size log2][n].
alignas(16) inline const std::array<std::array<std::array<std::uint8_t,16>,16>,4> spu_insert_controls=[]{
    std::array<std::array<std::array<std::uint8_t,16>,16>,4> r{};
    for(unsigned k=0;k<4;++k)for(unsigned n=0;n<16;++n){
        const unsigned size=1u<<k;for(unsigned i=0;i<16;++i)r[k][n][i]=std::uint8_t(16+i);
        const unsigned at=n&(16-size);for(unsigned i=0;i<size;++i)r[k][n][at+i]=std::uint8_t((size<4?4-size:0)+i);
    }
    return r;
}();
SPU_VI SpuV spu_vinsert(unsigned k,unsigned n){return spu_vldp(spu_insert_controls[k][n&15].data());}
SPU_VI SpuV spu_vxswd(SpuV w){ // lanes [w0,w1,w2,w3] -> [sign(w1),w1,sign(w3),w3]
    return _mm_unpacklo_epi32(_mm_shuffle_epi32(_mm_srai_epi32(w,31),_MM_SHUFFLE(3,3,3,1)),_mm_shuffle_epi32(w,_MM_SHUFFLE(3,3,3,1)));
}
// FCEQ/FCMEQ/FCGT/FCMGT (w): exponent-0 operands compare as +0 (ISA v1.2 PDF pp.203-207).
template<bool Magnitude,bool Equal> SPU_VI SpuV spu_vfcmp(SpuV a,SpuV b){
    const auto exp=_mm_set1_epi32(0x7f800000),zero=_mm_setzero_si128();
    a=_mm_andnot_si128(_mm_cmpeq_epi32(_mm_and_si128(a,exp),zero),a);
    b=_mm_andnot_si128(_mm_cmpeq_epi32(_mm_and_si128(b,exp),zero),b);
    if constexpr(Magnitude){const auto m=_mm_set1_epi32(0x7fffffff);a=_mm_and_si128(a,m);b=_mm_and_si128(b,m);}
    if constexpr(Equal)return _mm_cmpeq_epi32(a,b);
    else{
        auto key=[](SpuV x){const auto s=_mm_srai_epi32(x,31);return _mm_sub_epi32(_mm_xor_si128(_mm_and_si128(x,_mm_set1_epi32(0x7fffffff)),s),s);};
        return _mm_cmpgt_epi32(key(a),key(b));
    }
}
// Exact single-precision fallback for lanes the vector path cannot prove.
[[gnu::noinline,maybe_unused]] static SpuV spu_vfp_slow(SpuRegister& fpscr,SpuV a,SpuV b,SpuV c,SpuV fast,unsigned unsafe,unsigned kind){
    alignas(16) std::uint32_t x[4],y[4],z[4],r[4];
    _mm_store_si128(reinterpret_cast<__m128i*>(x),a);_mm_store_si128(reinterpret_cast<__m128i*>(y),b);
    _mm_store_si128(reinterpret_cast<__m128i*>(z),c);_mm_store_si128(reinterpret_cast<__m128i*>(r),fast);
    for(unsigned i=0;i<4;++i)if(unsafe&(1u<<i))r[i]=spu_single_math(fpscr,i,x[i],y[i],z[i],kind);else if(!(r[i]&0x7fffffffu))r[i]=0;
    return _mm_load_si128(reinterpret_cast<const __m128i*>(r));
}
// FA/FS/FM/FMA/FMS/FNMS on w-representation values. ISA v1.2 PDF pp.195-196, 208-214:
// truncation, no denormal/Inf/NaN; extended-range exponent 255 and every lane that can
// differ from an IEEE RZ computation (denormal or extended inputs, results outside the
// normal range below 0x7f7fffff, ambiguous zeros) take the exact limb path above.
template<unsigned Kind> SPU_VI SpuV spu_vfp(SpuRegister& fpscr,SpuV a,SpuV b,SpuV c){
#if defined(__AVX512F__) && defined(__AVX512VL__)
    if(__builtin_expect(spu_profile_on,0)){
        SpuRegister x,y,z;spu_vst(x,spu_vswap32(a));spu_vst(y,spu_vswap32(b));spu_vst(z,spu_vswap32(c));
        return spu_vswap32(spu_vld(spu_single_vector(fpscr,x,y,z,Kind)));
    }
    constexpr int rz=_MM_FROUND_TO_ZERO|_MM_FROUND_NO_EXC;
    const __m512 A=_mm512_zextps128_ps512(_mm_castsi128_ps(a)),B=_mm512_zextps128_ps512(_mm_castsi128_ps(b));
    __m512 R;
    if constexpr(Kind==0)R=_mm512_add_round_ps(A,B,rz);
    else if constexpr(Kind==1)R=_mm512_sub_round_ps(A,B,rz);
    else if constexpr(Kind==2)R=_mm512_mul_round_ps(A,B,rz);
    else{
        const __m512 C=_mm512_zextps128_ps512(_mm_castsi128_ps(c));
        if constexpr(Kind==3)R=_mm512_fmadd_round_ps(A,B,C,rz);
        else if constexpr(Kind==4)R=_mm512_fmsub_round_ps(A,B,C,rz);
        else R=_mm512_fnmadd_round_ps(A,B,C,rz);
    }
    const SpuV r=_mm_castps_si128(_mm512_castps512_ps128(R));
    const auto mag=_mm_set1_epi32(0x7fffffff),low=_mm_set1_epi32(0x00800000);
    const auto x=_mm_and_si128(a,mag),y=_mm_and_si128(b,mag),m=_mm_and_si128(r,mag);
    // ordinary: zero or exponent 1..254 (unsigned (v-0x800000) < 0x7f000000).
    auto ordinary=[&](SpuV v){return _mm_cmpeq_epi32_mask(v,_mm_setzero_si128())|_mm_cmplt_epu32_mask(_mm_sub_epi32(v,low),_mm_set1_epi32(0x7f000000));};
    __mmask8 safe=ordinary(x)&ordinary(y);
    const __mmask8 zero_result=_mm_cmpeq_epi32_mask(m,_mm_setzero_si128());
    __mmask8 exact_zero;
    if constexpr(Kind<2)exact_zero=_mm_cmpeq_epi32_mask(x,y);
    else exact_zero=_mm_cmpeq_epi32_mask(x,_mm_setzero_si128())|_mm_cmpeq_epi32_mask(y,_mm_setzero_si128());
    if constexpr(Kind>=3){const auto z=_mm_and_si128(c,mag);safe&=ordinary(z);exact_zero&=_mm_cmpeq_epi32_mask(z,_mm_setzero_si128());}
    const __mmask8 normal=_mm_cmplt_epu32_mask(_mm_sub_epi32(m,low),_mm_set1_epi32(0x7f7fffff-0x00800000));
    safe&=normal|(zero_result&exact_zero);
    if(__builtin_expect((safe&15)!=15,0))return spu_vfp_slow(fpscr,a,b,c,r,unsigned(~safe)&15u,Kind);
    return _mm_maskz_mov_epi32(__mmask8(~zero_result),r); // an architectural zero is +0
#else
    SpuRegister x,y,z;spu_vst(x,spu_vswap32(a));spu_vst(y,spu_vswap32(b));spu_vst(z,spu_vswap32(c));
    return spu_vswap32(spu_vld(spu_single_vector(fpscr,x,y,z,Kind)));
#endif
}
// CSFLT/CUFLT: exact for every input; truncation of >24 significant bits is the
// ISA's round-toward-zero. Results below the normal range keep the exact path.
SPU_VI std::uint32_t spu_int_to_float_fast(SpuRegister& fpscr,unsigned lane,std::uint32_t x,int scale,bool unsign){
    const bool sign=!unsign && (x>>31);
    std::uint32_t m=sign?0u-x:x;
    if(!m)return 0;
    const int top=31-__builtin_clz(m);
    const int exponent=top-scale;
    if(__builtin_expect(exponent< -126,0))return spu_int_to_float(fpscr,lane,x,scale,unsign);
    const std::uint32_t mantissa=top>=23?m>>(top-23):m<<(23-top);
    return (std::uint32_t(sign)<<31)|(std::uint32_t(exponent+127)<<23)|(mantissa&0x7fffff);
}
#if defined(__AVX2__)
// CFLTS/CFLTU on all four lanes (SPU ISA v1.2 PDF p.113-114; semantics of spu_float_to_int): no NaN/Inf, exponent 0 = 0,
// exponent 255 is an ordinary large number; magnitude m*2^e truncated, saturated to 0x7fffffff/0x80000000 (signed) or
// 0xffffffff (unsigned, negative inputs give 0).  e = exponent + (scale-150).
SPU_VI SpuV spu_vfloat_to_int(SpuV x,int scale,bool unsign){
    const SpuV zero=_mm_setzero_si128(),ones=_mm_set1_epi32(-1);
    const SpuV exp=_mm_and_si128(_mm_srli_epi32(x,23),_mm_set1_epi32(255));
    const SpuV mant=_mm_or_si128(_mm_and_si128(x,_mm_set1_epi32(0x7fffff)),_mm_set1_epi32(0x800000));
    const SpuV sign=_mm_srai_epi32(x,31);
    const SpuV e=_mm_add_epi32(exp,_mm_set1_epi32(scale-150));
    const SpuV limit=unsign?ones:_mm_blendv_epi8(_mm_set1_epi32(0x7fffffff),_mm_set1_epi32(int(0x80000000u)),sign);
    const SpuV epos=_mm_cmpgt_epi32(e,ones);
    const SpuV c32=_mm_set1_epi32(32);
    const SpuV ec=_mm_min_epi32(_mm_max_epi32(e,zero),c32);
    const SpuV lim_e=_mm_srlv_epi32(limit,ec);
    const SpuV over=_mm_or_si128(_mm_cmpgt_epi32(e,c32),_mm_xor_si128(_mm_cmpeq_epi32(_mm_min_epu32(mant,lim_e),mant),ones));
    const SpuV left=_mm_blendv_epi8(_mm_sllv_epi32(mant,ec),limit,over);
    const SpuV right=_mm_srlv_epi32(mant,_mm_min_epi32(_mm_max_epi32(_mm_sub_epi32(zero,e),zero),c32));
    SpuV r=_mm_blendv_epi8(right,left,epos);
    r=_mm_sub_epi32(_mm_xor_si128(r,sign),sign);
    const SpuV drop=_mm_or_si128(_mm_cmpeq_epi32(exp,zero),unsign?sign:zero);
    return _mm_andnot_si128(drop,r);
}
SPU_VI SpuV spu_vfloat_to_int_any(SpuV x,int scale,bool unsign){return spu_vfloat_to_int(x,scale,unsign);}
#else
SPU_VI SpuV spu_vfloat_to_int_any(SpuV x,int scale,bool unsign){return spu_vmap32(x,[&](unsigned,std::uint32_t v){return spu_float_to_int(v,scale,unsign);});}
#endif
// FI fast path: base and step products as one exact integer; normal results only.
SPU_VI std::uint32_t spu_interpolate_fast(SpuRegister& fpscr,unsigned lane,std::uint32_t a,std::uint32_t b){
    const unsigned be=(b>>23)&255;
    if(__builtin_expect(spu_single_diff(a) || spu_single_diff(b) || !be,0))return spu_interpolate(fpscr,lane,a,b);
    const std::uint64_t value=(std::uint64_t(0x2000|((b>>10)&0x1fff))<<19)-std::uint64_t(b&1023)*(a&0x7ffff);
    const int top=63-__builtin_clzll(value),exponent=top+int(be)-127-32;
    if(__builtin_expect(exponent< -126 || exponent>127,0))return spu_interpolate(fpscr,lane,a,b);
    const std::uint32_t mantissa=std::uint32_t(value>>(top-23));
    return (b&0x80000000u)|(std::uint32_t(exponent+127)<<23)|(mantissa&0x7fffff);
}
#endif
'''


class Unsupported(Exception):
    pass


# Template table: op (11-bit primary field) -> function(E, word, pc).
TEMPLATES = {}


def _reg(op_list):
    def wrap(fn):
        for op in op_list:
            if op in TEMPLATES:
                raise AssertionError(f'duplicate SPU template op 0x{op:x}')
            TEMPLATES[op] = fn
        return fn
    return wrap


def _range8(base8):
    """All 11-bit ops whose top 8 bits equal base8 (RI10/RI8 forms)."""
    return [(base8 << 3) | i for i in range(8)]


def _range9(base9):
    return [(base9 << 2) | i for i in range(4)]


def _range4(base4):
    return [(base4 << 7) | i for i in range(128)]


def _fields(word):
    return dict(t=word & 127, a=(word >> 7) & 127, b=(word >> 14) & 127, d=(word >> 21) & 127,
                i7=(word >> 14) & 127, i10=_signed((word >> 14) & 1023, 10),
                i16=(word >> 7) & 65535, i18=(word >> 7) & 0x3ffff)


# WRCH to the MFC parameter channels 16..20 (LSA, EAH, EAL, size, tag id) and the tag mask (22): the same effect as spu_write_channel for these
# channels (statistics aside), including the unchanged busy-poll detector state; with ANYPS3_SPU_PROFILE the original call
# keeps the profile categories exact.
@_reg([0x10d])
def _wrch_mfc_parameter(E, word, pc):
    f = _fields(word)
    if not (16 <= f['a'] <= 20 or f['a'] == 22):
        raise Unsupported('channel')
    target = f't.dma[{f["a"] - 16}]' if f['a'] != 22 else 't.tag_mask'
    E.emit(f'{{const std::uint32_t wv={E.word0(f["t"])};if(__builtin_expect(spu_profile_on,0))spu_write_channel(s,t,ls,{f["a"]},wv);'
           f'else{{spu_stat(t,13);{target}=wv;}}}}')


# ---- constants ----
@_reg(_range9(0x81) + _range9(0x82) + _range9(0x83))
def _il(E, word, pc):
    f = _fields(word); op = word >> 23
    i16 = f['i16']
    value = {0x81: _signed(i16, 16) & 0xffffffff, 0x82: i16 << 16, 0x83: i16 | (i16 << 16)}[op]
    E.set_const(f['t'], (value,) * 4)


@_reg([(0x21 << 4) | i for i in range(16)])
def _ila(E, word, pc):
    f = _fields(word)
    E.set_const(f['t'], (f['i18'],) * 4)


@_reg(_range9(0xc1))
def _iohl(E, word, pc):
    f = _fields(word)
    E.bitwise_const(f['t'], f['t'], (f['i16'],) * 4, 'or')


@_reg(_range9(0x65))
def _fsmbi(E, word, pc):
    f = _fields(word); i16 = f['i16']
    data = bytes(255 if i16 & (1 << (15 - i)) else 0 for i in range(16))
    E.set_const(f['t'], tuple(int.from_bytes(data[i*4:i*4+4], 'big') for i in range(4)))


# ---- bitwise ----
_BITWISE = {0xc1: 'and', 0x41: 'or', 0x241: 'xor', 0x49: 'nor', 0x2c1: 'andc', 0xc9: 'nand', 0x249: 'eqv', 0x2c9: 'orc'}


@_reg(list(_BITWISE))
def _bitwise(E, word, pc):
    f = _fields(word)
    E.bitwise(f['t'], f['a'], f['b'], _BITWISE[word >> 21])


@_reg(_range4(8))
def _selb(E, word, pc):
    f = _fields(word)
    E.selb(f['d'], f['a'], f['b'], f['t'])


@_reg(_range8(0x14) + _range8(4) + _range8(0x44) + _range8(0x15) + _range8(5) + _range8(0x45)
      + _range8(0x16) + _range8(6) + _range8(0x46))
def _bitwise_imm(E, word, pc):
    f = _fields(word); kind = word >> 24
    i10 = f['i10']
    if kind in (0x14, 4, 0x44):
        imm = i10 & 0xffffffff
    elif kind in (0x15, 5, 0x45):
        imm = (i10 & 65535) * 0x10001
    else:
        imm = (i10 & 255) * 0x01010101
    operation = 'and' if kind in (0x14, 0x15, 0x16) else 'or' if kind in (4, 5, 6) else 'xor'
    E.bitwise_const(f['t'], f['a'], (imm,) * 4, operation)


# ---- word arithmetic / compares (w) ----
_WORD = {0xc0: '_mm_add_epi32({a},{b})', 0x40: '_mm_sub_epi32({b},{a})', 0x3c0: '_mm_cmpeq_epi32({a},{b})',
         0x240: '_mm_cmpgt_epi32({a},{b})', 0x2c0: 'spu_vclgt32({a},{b})',
         0xc2: '_mm_srli_epi32(spu_vclgt32({a},_mm_add_epi32({a},{b})),31)',
         0x42: '_mm_andnot_si128(spu_vclgt32({a},{b}),_mm_set1_epi32(1))'}


@_reg(list(_WORD))
def _word(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w')
    E.set(f['t'], 'w', _WORD[word >> 21].format(a=a, b=b))


_WORD_IMM = {0x1c: '_mm_add_epi32({a},{k})', 0xc: '_mm_sub_epi32({k},{a})', 0x7c: '_mm_cmpeq_epi32({a},{k})',
             0x4c: '_mm_cmpgt_epi32({a},{k})', 0x5c: 'spu_vclgt32({a},{k})'}


@_reg(_range8(0x1c) + _range8(0xc) + _range8(0x7c) + _range8(0x4c) + _range8(0x5c))
def _word_imm(E, word, pc):
    f = _fields(word); kind = word >> 24
    imm = f['i10'] & 0xffffffff
    if kind == 0x1c and E.is_const(f['a']):
        E.set_const(f['t'], tuple((x + imm) & 0xffffffff for x in E.const(f['a'])))
        return
    a = E.get(f['a'], 'w')
    E.set(f['t'], 'w', _WORD_IMM[kind].format(a=a, k=f'_mm_set1_epi32(int(0x{imm:x}u))'))


# ---- halfword (h) ----
_HALF = {0xc8: '_mm_add_epi16({a},{b})', 0x48: '_mm_sub_epi16({b},{a})', 0x3c8: '_mm_cmpeq_epi16({a},{b})',
         0x248: '_mm_cmpgt_epi16({a},{b})', 0x2c8: 'spu_vclgt16({a},{b})'}


@_reg(list(_HALF))
def _half(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'h'); b = E.get(f['b'], 'h')
    E.set(f['t'], 'h', _HALF[word >> 21].format(a=a, b=b))


_HALF_IMM = {0x1d: '_mm_add_epi16({a},{k})', 0xd: '_mm_sub_epi16({k},{a})', 0x7d: '_mm_cmpeq_epi16({a},{k})',
             0x4d: '_mm_cmpgt_epi16({a},{k})', 0x5d: 'spu_vclgt16({a},{k})'}


@_reg(_range8(0x1d) + _range8(0xd) + _range8(0x7d) + _range8(0x4d) + _range8(0x5d))
def _half_imm(E, word, pc):
    f = _fields(word); kind = word >> 24
    imm = f['i10'] & 0xffff
    a = E.get(f['a'], 'h')
    E.set(f['t'], 'h', _HALF_IMM[kind].format(a=a, k=f'_mm_set1_epi16(short(0x{imm:x}))'))


# ---- bytes (b) ----
_BYTE = {0x3d0: '_mm_cmpeq_epi8({a},{b})', 0x250: '_mm_cmpgt_epi8({a},{b})', 0x2d0: 'spu_vclgt8({a},{b})',
         0xd3: '_mm_avg_epu8({a},{b})', 0x53: '_mm_sub_epi8(_mm_max_epu8({a},{b}),_mm_min_epu8({a},{b}))'}


@_reg(list(_BYTE))
def _byte(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'b'); b = E.get(f['b'], 'b')
    E.set(f['t'], 'b', _BYTE[word >> 21].format(a=a, b=b))


_BYTE_IMM = {0x7e: '_mm_cmpeq_epi8({a},{k})', 0x4e: '_mm_cmpgt_epi8({a},{k})', 0x5e: 'spu_vclgt8({a},{k})'}


@_reg(_range8(0x7e) + _range8(0x4e) + _range8(0x5e))
def _byte_imm(E, word, pc):
    f = _fields(word); kind = word >> 24
    imm = f['i10'] & 0xff
    a = E.get(f['a'], 'b')
    E.set(f['t'], 'b', _BYTE_IMM[kind].format(a=a, k=f'_mm_set1_epi8(char(0x{imm:x}))'))


# ---- word shifts and rotates (w) ----
@_reg([0x78, 0x79, 0x7a, 0x7b])
def _word_shift_imm(E, word, pc):
    f = _fields(word); op = word >> 21; i7 = f['i7']
    a = E.get(f['a'], 'w')
    if op == 0x78:
        n = i7 & 31
        expr = a if n == 0 else f'_mm_or_si128(_mm_slli_epi32({a},{n}),_mm_srli_epi32({a},{32-n}))'
    elif op == 0x7b:
        n = i7 & 63
        expr = f'_mm_slli_epi32({a},{n})' if n < 32 else '_mm_setzero_si128()'
    elif op == 0x79:
        n = (-i7) & 63
        expr = f'_mm_srli_epi32({a},{n})' if n < 32 else '_mm_setzero_si128()'
    else:
        n = min((-i7) & 63, 31)
        expr = f'_mm_srai_epi32({a},{n})'
    E.set(f['t'], 'w', expr)


@_reg([0x58, 0x59, 0x5a, 0x5b])
def _word_shift(E, word, pc):
    f = _fields(word); op = word >> 21
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w')
    if op == 0x58:
        expr = f'spu_vrot32({a},_mm_and_si128({b},_mm_set1_epi32(31)))'
    elif op == 0x5b:
        expr = f'spu_vshl32({a},_mm_and_si128({b},_mm_set1_epi32(63)))'
    elif op == 0x59:
        expr = f'spu_vshr32({a},_mm_and_si128(_mm_sub_epi32(_mm_setzero_si128(),{b}),_mm_set1_epi32(63)))'
    else:
        expr = f'spu_vsar32({a},_mm_and_si128(_mm_sub_epi32(_mm_setzero_si128(),{b}),_mm_set1_epi32(63)))'
    E.set(f['t'], 'w', expr)


# ---- halfword shifts and rotates (h) ----
@_reg([0x7c, 0x7d, 0x7e, 0x7f])
def _half_shift_imm(E, word, pc):
    f = _fields(word); op = word >> 21; i7 = f['i7']; kind = op & 3
    a = E.get(f['a'], 'h')
    if kind == 0:
        n = i7 & 15
        expr = a if n == 0 else f'_mm_or_si128(_mm_slli_epi16({a},{n}),_mm_srli_epi16({a},{16-n}))'
    elif kind == 1:
        n = (-i7) & 31
        expr = f'_mm_srli_epi16({a},{n})' if n < 16 else '_mm_setzero_si128()'
    elif kind == 2:
        n = min((-i7) & 31, 15)
        expr = f'_mm_srai_epi16({a},{n})'
    else:
        n = i7 & 31
        expr = f'_mm_slli_epi16({a},{n})' if n < 16 else '_mm_setzero_si128()'
    E.set(f['t'], 'h', expr)


@_reg([0x5c, 0x5d, 0x5e, 0x5f])
def _half_shift(E, word, pc):
    f = _fields(word); op = word >> 21; kind = op & 3
    a = E.get(f['a'], 'h'); b = E.get(f['b'], 'h')
    neg = f'_mm_and_si128(_mm_sub_epi16(_mm_setzero_si128(),{b}),_mm_set1_epi16(31))'
    if kind == 0:
        n = f'_mm_and_si128({b},_mm_set1_epi16(15))'
        expr = f'_mm_or_si128(spu_vshl16({a},{n}),spu_vshr16({a},_mm_sub_epi16(_mm_set1_epi16(16),{n})))'
    elif kind == 1:
        expr = f'spu_vshr16({a},{neg})'
    elif kind == 2:
        expr = f'spu_vsar16({a},{neg})'
    else:
        expr = f'spu_vshl16({a},_mm_and_si128({b},_mm_set1_epi16(31)))'
    E.set(f['t'], 'h', expr)


# ---- quadword byte and bit shifts (b) ----
_ROTB = {0x1fc: (0, 'imm', 15), 0x1dc: (0, 'reg', 15), 0x1cc: (0, 'bits', 15),
         0x1ff: (1, 'imm', 31), 0x1df: (1, 'reg', 31), 0x1cf: (1, 'bits', 31),
         0x1fd: (2, 'nimm', 31), 0x1dd: (2, 'nreg', 31), 0x1cd: (2, 'nbits', 31)}


@_reg(list(_ROTB))
def _rotb(E, word, pc):
    f = _fields(word); kind, source, mask = _ROTB[word >> 21]
    a = E.get(f['a'], 'b')
    if source in ('imm', 'nimm'):
        n = (f['i7'] if source == 'imm' else -f['i7']) & mask
        if kind != 0 and n >= 16:
            E.set(f['t'], 'b', '_mm_setzero_si128()')
            return
        E.set(f['t'], 'b', a if n == 0 else f'spu_vrotb<{kind}>({a},{n}u)')
        return
    count = E.word0(f['b'])
    if source in ('bits', 'nbits'):
        count = f'({count}>>3)'
    if source.startswith('n'):
        count = f'(0u-{count})'
    E.set(f['t'], 'b', f'spu_vrotb<{kind}>({a},({count})&{mask}u)')


_ROTBITS = {0x1f8: (0, 'imm'), 0x1d8: (0, 'reg'), 0x1fb: (1, 'imm'), 0x1db: (1, 'reg'),
            0x1f9: (2, 'nimm'), 0x1d9: (2, 'nreg')}


@_reg(list(_ROTBITS))
def _rotbits(E, word, pc):
    f = _fields(word); kind, source = _ROTBITS[word >> 21]
    a = E.get(f['a'], 'b')
    if source == 'imm' or source == 'nimm':
        n = (f['i7'] if source == 'imm' else -f['i7']) & 7
        E.set(f['t'], 'b', a if n == 0 else f'spu_vrotbits<{kind}>({a},{n}u)')
        return
    count = E.word0(f['b'])
    if source == 'nreg':
        count = f'(0u-{count})'
    E.set(f['t'], 'b', f'spu_vrotbits<{kind}>({a},({count})&7u)')


@_reg(_range4(0xb))
def _shufb(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'b'); b = E.get(f['b'], 'b'); c = E.get(f['t'], 'b')
    E.set(f['d'], 'b', f'spu_vshufb({a},{b},{c})')


@_reg([0x1d4, 0x1d5, 0x1d6, 0x1d7, 0x1f4, 0x1f5, 0x1f6, 0x1f7])
def _insert_control(E, word, pc):
    f = _fields(word); op = word >> 21
    base = E.word0(f['a'])
    offset = E.word0(f['b']) if op < 0x1f4 else f'{f["i7"]}u'
    E.set(f['t'], 'b', f'spu_vinsert({op & 3},{base}+{offset})')


@_reg([0x2b4])
def _cntb(E, word, pc):
    f = _fields(word)
    E.set(f['t'], 'b', f'spu_vcntb({E.get(f["a"], "b")})')


@_reg([0x2a5])
def _clz(E, word, pc):
    f = _fields(word)
    E.set(f['t'], 'w', f'spu_vclz({E.get(f["a"], "w")})')


@_reg([0x2b6])
def _xsbh(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'h')
    E.set(f['t'], 'h', f'_mm_srai_epi16(_mm_slli_epi16({a},8),8)')


@_reg([0x2ae])
def _xshw(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'w')
    E.set(f['t'], 'w', f'spu_vlo16s({a})')


@_reg([0x2a6])
def _xswd(E, word, pc):
    f = _fields(word)
    E.set(f['t'], 'w', f'spu_vxswd({E.get(f["a"], "w")})')


@_reg([0x1b4, 0x1b5, 0x1b6])
def _fsm(E, word, pc):
    f = _fields(word); op = word >> 21
    helper = {0x1b4: 'spu_vfsm', 0x1b5: 'spu_vfsmh', 0x1b6: 'spu_vfsmb'}[op]
    E.set(f['t'], 'b', f'{helper}({E.word0(f["a"])})')


@_reg([0x253])
def _sumb(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'b'); b = E.get(f['b'], 'b')
    E.set(f['t'], 'w', f'spu_vsumb({a},{b})')


# ---- multiplies (w) ----
@_reg([0x3c4, 0x3cc, 0x3c5, 0x3c6, 0x3c7, 0x3ce, 0x346, 0x34e] + _range8(0x74) + _range8(0x75))
def _mpy(E, word, pc):
    f = _fields(word); op = word >> 21
    a = E.get(f['a'], 'w')
    if op >> 3 in (0x74, 0x75):
        imm = f['i10'] & 65535
        if op >> 3 == 0x74:
            imm = _signed(imm, 16) & 0xffffffff
        left = f'spu_vlo16s({a})' if op >> 3 == 0x74 else f'spu_vlo16u({a})'
        E.set(f['t'], 'w', f'spu_vmul32({left},_mm_set1_epi32(int(0x{imm:x}u)))')
        return
    b = E.get(f['b'], 'w')
    signed_product = op in (0x3c4, 0x3c6, 0x3c7, 0x346)
    high_a = op in (0x3c5, 0x3c6, 0x3ce, 0x346, 0x34e)
    high_b = op in (0x3c6, 0x3ce, 0x346, 0x34e)
    def part(x, high):
        if high:
            return f'_mm_srai_epi32({x},16)' if signed_product else f'_mm_srli_epi32({x},16)'
        return f'spu_vlo16s({x})' if signed_product else f'spu_vlo16u({x})'
    product = f'spu_vmul32({part(a, high_a)},{part(b, high_b)})'
    if op == 0x3c5:
        product = f'_mm_slli_epi32({product},16)'
    elif op == 0x3c7:
        product = f'_mm_srai_epi32({product},16)'
    if op in (0x346, 0x34e):
        product = f'_mm_add_epi32({E.get(f["t"], "w")},{product})'
    E.set(f['t'], 'w', product)


@_reg(_range4(0xc))
def _mpya(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w'); c = E.get(f['t'], 'w')
    E.set(f['d'], 'w', f'_mm_add_epi32(spu_vmul32(spu_vlo16s({a}),spu_vlo16s({b})),{c})')


# ---- local storage ----
@_reg(_range8(0x34) + [0x1c4] + _range9(0x61) + _range9(0x67))
def _lq(E, word, pc):
    f = _fields(word); op = word >> 21
    if op >> 3 == 0x34:
        address = f'({E.word0(f["a"])}+0x{(f["i10"]*16)&0xffffffff:x}u)&0x3fff0u'
    elif op == 0x1c4:
        address = f'({E.word0(f["a"])}+{E.word0(f["b"])})&0x3fff0u'
    elif op >> 2 == 0x61:
        address = f'0x{(_signed(f["i16"], 16)*4)&0x3fff0:x}u'
    else:
        address = f'(({pc})+0x{(_signed(f["i16"], 16)*4)&0xffffffff:x}u)&0x3fff0u'
    E.load(f['t'], address)


@_reg(_range8(0x24) + [0x144] + _range9(0x41) + _range9(0x47))
def _stq(E, word, pc):
    f = _fields(word); op = word >> 21
    if op >> 3 == 0x24:
        address = f'({E.word0(f["a"])}+0x{(f["i10"]*16)&0xffffffff:x}u)&0x3fff0u'
    elif op == 0x144:
        address = f'({E.word0(f["a"])}+{E.word0(f["b"])})&0x3fff0u'
    elif op >> 2 == 0x41:
        address = f'0x{(_signed(f["i16"], 16)*4)&0x3fff0:x}u'
    else:
        address = f'(({pc})+0x{(_signed(f["i16"], 16)*4)&0xffffffff:x}u)&0x3fff0u'
    E.store(f['t'], address)


# ---- single precision ----
@_reg([0x2c4, 0x2c5, 0x2c6])
def _fp2(E, word, pc):
    f = _fields(word); kind = (word >> 21) - 0x2c4
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w')
    E.set(f['t'], 'w', f'spu_vfp<{kind}>(t.fpscr,{a},{b},_mm_setzero_si128())', effect=True)


@_reg(_range4(0xd) + _range4(0xe) + _range4(0xf))
def _fp3(E, word, pc):
    f = _fields(word); kind = {0xd: 5, 0xe: 3, 0xf: 4}[word >> 28]
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w'); c = E.get(f['t'], 'w')
    E.set(f['d'], 'w', f'spu_vfp<{kind}>(t.fpscr,{a},{b},{c})', effect=True)


@_reg([0x2c2, 0x2ca, 0x3c2, 0x3ca])
def _fcmp(E, word, pc):
    f = _fields(word); op = word >> 21
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w')
    E.set(f['t'], 'w', f'spu_vfcmp<{str(bool(op & 8)).lower()},{str(bool(op & 0x100)).lower()}>({a},{b})')


@_reg([0x1b8, 0x1b9])
def _estimate(E, word, pc):
    f = _fields(word)
    helper = 'spu_frest' if word >> 21 == 0x1b8 else 'spu_frsqest'
    a = E.get(f['a'], 'w')
    E.set(f['t'], 'w', f'spu_vmap32({a},[](unsigned,std::uint32_t x){{return {helper}(x);}})')


@_reg([0x3d4])
def _fi(E, word, pc):
    f = _fields(word)
    a = E.get(f['a'], 'w'); b = E.get(f['b'], 'w')
    E.set(f['t'], 'w', f'spu_vmap32x2({a},{b},[&](unsigned i,std::uint32_t x,std::uint32_t y){{return spu_interpolate_fast(t.fpscr,i,x,y);}})',
          effect=True)


@_reg(list(range(0x3b0, 0x3b8)))
def _convert(E, word, pc):
    f = _fields(word); kind = (word >> 21) >> 1
    i8 = (word >> 14) & 255
    scale = (173 if kind in (0x1d8, 0x1d9) else 155) - i8
    if not 0 <= scale <= 127:
        raise Unsupported('scale')
    a = E.get(f['a'], 'w')
    unsign = str(kind in (0x1d9, 0x1db)).lower()
    if kind in (0x1d8, 0x1d9):
        E.set(f['t'], 'w', f'spu_vfloat_to_int_any({a},{scale},{unsign})')
    else:
        E.set(f['t'], 'w', f'spu_vmap32({a},[&](unsigned i,std::uint32_t x){{return spu_int_to_float_fast(t.fpscr,i,x,{scale},{unsign});}})',
              effect=True)


# ---- no-ops (lnop, nop, hints) ----
@_reg([1, 0x201, 0x1ac] + [(8 << 4) | i for i in range(16)] + [(9 << 4) | i for i in range(16)])
def _nop(E, word, pc):
    return


def template(word):
    return TEMPLATES.get(word >> 21)
