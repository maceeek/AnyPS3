"""Exact PowerPC binary64/binary32 arithmetic and FPSCR with guarded host paths.

Book I v2.02, chapter 4 and appendix A; Cell Handbook v1.1, appendix A.
All RN modes use one final rounding, including gradual underflow. Enabled
exceptions stop before arithmetic commits. Estimates and NI division/sqrt
use the documented accuracy bounds, not hardware-identical approximation bits.
"""

FLOAT_CPP = r'''
#include <cfloat>
#include <cmath>
#include <cstring>
#if defined(__SSE2__)
#include <xmmintrin.h>
#endif
#if defined(__AVX512F__)
#include <immintrin.h>
#endif
using Wide = unsigned __int128;
static unsigned fp_bits(Wide value) {
    const auto upper=static_cast<std::uint64_t>(value>>64),lower=static_cast<std::uint64_t>(value);
    return upper?128u-__builtin_clzll(upper):lower?64u-__builtin_clzll(lower):0;
}
struct FloatParts { bool sign; int exponent; std::uint64_t significand; };
static FloatParts fp_parts(std::uint64_t bits) {
    const auto exp = (bits >> 52) & 2047;
    const auto fraction = bits & UINT64_C(0xfffffffffffff);
    if (exp == 2047)
        throw std::runtime_error("internal FP decomposition requires a finite operand");
    if(!exp && fraction){
        const unsigned shift=__builtin_clzll(fraction)-11;
        return {bool(bits>>63),-1022-int(shift),fraction<<shift};
    }
    return {bool(bits >> 63), int(exp) - 1023, exp ? fraction | (UINT64_C(1) << 52) : 0};
}
static void fp_mode(const PpuState& s) {
    // Book I pp. 94-99: this runtime has no guest FP interrupt delivery.
    // Stop conservatively for any enable, independent of MSR FE (not modeled).
    if (s.fpscr & 0xf8) throw std::runtime_error("PPU enabled FP exceptions / MSR FE delivery is unsupported");
    // Cell Handbook v1.1 p.721 bounds NI divide/sqrt error to 1 or 2 ULP.
    // Use our IEEE result within those bounds and preserve NI. No Cell FTZ
    // behaviour is documented there; do not import another CPU's FTZ mode.
}
static bool fp_is_nan(std::uint64_t value) {
    return (value & UINT64_C(0x7fffffffffffffff)) > UINT64_C(0x7ff0000000000000);
}
static bool fp_is_infinite(std::uint64_t value) {
    return (value & UINT64_C(0x7fffffffffffffff)) == UINT64_C(0x7ff0000000000000);
}
static void fp_exception(PpuState& s, std::uint32_t flag) {
    if (~s.fpscr & flag) s.fpscr |= UINT32_C(0x80000000); // FX: new exception bit.
    s.fpscr |= flag;
    if (flag & UINT32_C(0x01f80700)) s.fpscr |= UINT32_C(0x20000000); // Invalid-operation summary (all nine causes).
}
// FPSCR bit 20 is reserved. FEX and VX are derived, including after writes.
[[maybe_unused]] static void fp_summaries(PpuState& s) {
    s.fpscr &= ~UINT32_C(0x60000800);
    if(s.fpscr & UINT32_C(0x01f80700))s.fpscr |= UINT32_C(0x20000000);
    if((s.fpscr>>22)&s.fpscr&0xf8)s.fpscr |= UINT32_C(0x40000000);
}
static inline void fp_materialize(PpuState& s);
[[maybe_unused]] static void fp_write_fields(PpuState& s,std::uint32_t value,std::uint32_t mask) {
    fp_materialize(s);
    mask &= ~UINT32_C(0x60000800);
    s.fpscr=(s.fpscr&~mask)|(value&mask);
    fp_summaries(s); // mtfsf/mtfsfi never set FX implicitly.
}
static inline void fp_materialize(PpuState& s);
[[maybe_unused]] static void fp_write_bit(PpuState& s,unsigned bit,bool set) {
    fp_materialize(s);
    const auto mask=UINT32_C(0x80000000)>>bit;
    if(mask & UINT32_C(0x60000800))return;
    if(set){
        if((mask & UINT32_C(0x1ff80700)) && !(s.fpscr&mask))s.fpscr|=UINT32_C(0x80000000);
        s.fpscr|=mask;
    }else s.fpscr&=~mask;
    fp_summaries(s);
}
static inline void fp_materialize(PpuState& s);
[[maybe_unused]] static void fp_move_field(PpuState& s,unsigned target,unsigned source) {
    fp_materialize(s);
    const unsigned shift=28-4*source,cr_shift=28-4*target;
    const auto value=(s.fpscr>>shift)&15u;
    s.cr=(s.cr&~(15u<<cr_shift))|(value<<cr_shift);
    s.fpscr&=~((15u<<shift)&UINT32_C(0x9ff80700)); // FX + actual exception bits only.
    fp_summaries(s);
}
static std::uint64_t fp_special_result(PpuState& s, std::uint64_t value) {
    // Exact infinities and generated quiet NaNs: clear FR/FI, classify FPRF.
    const unsigned classification = fp_is_nan(value) ? 17 : (value >> 63 ? 9 : 5);
    s.fpscr = (s.fpscr & ~UINT32_C(0x7f000)) | (classification << 12);
    return value;
}
[[maybe_unused]] static std::uint64_t fp_subtract_operand(std::uint64_t value) {
    return fp_is_nan(value) ? value : value ^ UINT64_C(0x8000000000000000);
}
static bool fp_nan_result(PpuState& s, std::initializer_list<std::uint64_t> operands,
                          unsigned /*precision*/, std::uint64_t& result) {
    bool found=false, signaling=false;
    for(auto value:operands)if(fp_is_nan(value)){
        if(!found){result=value | UINT64_C(0x8000000000000);found=true;}
        signaling |= !(value & UINT64_C(0x8000000000000));
    }
    if(!found)return false;
    if(signaling){
        if(!(s.fpscr & UINT32_C(0x01000000)))s.fpscr |= UINT32_C(0x80000000);
        s.fpscr |= UINT32_C(0x21000000); // VXSNAN and invalid-operation summary.
    }
    s.fpscr=(s.fpscr & ~UINT32_C(0x7f000)) | UINT32_C(0x11000); // NaN, FR=FI=0.
    return true;
}
// frsp special operands (PowerPC Book I, frsp): a QNaN is returned with its
// payload truncated to single precision, an SNaN is quieted and sets VXSNAN,
// +-infinity is returned unchanged; FPRF classifies the result, FR=FI=0.
[[maybe_unused]] static bool fp_round_single_special(PpuState& s, std::uint64_t bits, std::uint64_t& result) {
    if(fp_nan_result(s,{bits},24,result)){result &= ~UINT64_C(0x1fffffff);return true;}
    if(fp_is_infinite(bits)){result=fp_special_result(s,bits);return true;}
    return false;
}
static void fp_inexact(PpuState& s, bool inexact, bool up) {
    s.fpscr &= ~UINT32_C(0x60000);
    if (inexact) {
        if (!(s.fpscr & 0x02000000)) s.fpscr |= 0x80000000;
        s.fpscr |= 0x02020000;
        if (up) s.fpscr |= 0x40000;
    }
}

// PPUPERF3: one out-of-line dispatcher, shared by all lifted instructions in
// a TU. Never inline the residual machinery into the generated PPU pages.
// The soft implementation below remains the fallback and differential oracle.
// Tests can force it with ANYPS3_FP_FORCE_SOFT (a compile-time diagnostic only).
[[maybe_unused]] static bool fp_host_operand(std::uint64_t value) {
    // Zero or a normal binary64 with exponent [-400,400]. All products and
    // residuals then stay normal (lowest product bit >= 2^-904); TwoSum and
    // FMA cannot lose a nonzero residual to host underflow. No NaN/Inf/denormal.
    const unsigned exponent=(value>>52)&2047;
    return exponent-623u<=800u || !(value&UINT64_C(0x7fffffffffffffff));
}
// Host binary64 operations of the fast paths below (fp_host_arithmetic, fp_lazy). With AVX-512F they use EVEX static rounding
// {rn-sae}: round-to-nearest with every exception suppressed, independent of the thread's MXCSR, so the per-operation MXCSR read
// (vstmxcsr, the dominant cost of the lazy helpers) is dropped (FP_HOST_NEEDS_MXCSR=0). MXCSR DAZ/FTZ cannot act on these calls
// either: fp_host_operand admits no denormal operand, every rounded result and residual of in-range operands is zero or normal
// (see fp_host_operand), and binary32 results are range-checked (exponent 898..1149) before fp_rn_single, so it cannot underflow.
// Without AVX-512F they are the plain operators and the callers verify the default MXCSR first.
#if defined(__AVX512F__)
#define FP_HOST_NEEDS_MXCSR 0
#define FP_RN (_MM_FROUND_TO_NEAREST_INT|_MM_FROUND_NO_EXC)
[[gnu::always_inline]] static inline double fp_rn_add(double x,double y){return _mm_cvtsd_f64(_mm_add_round_sd(_mm_set_sd(x),_mm_set_sd(y),FP_RN));}
[[gnu::always_inline]] static inline double fp_rn_sub(double x,double y){return _mm_cvtsd_f64(_mm_sub_round_sd(_mm_set_sd(x),_mm_set_sd(y),FP_RN));}
[[gnu::always_inline]] static inline double fp_rn_mul(double x,double y){return _mm_cvtsd_f64(_mm_mul_round_sd(_mm_set_sd(x),_mm_set_sd(y),FP_RN));}
[[gnu::always_inline]] static inline double fp_rn_div(double x,double y){return _mm_cvtsd_f64(_mm_div_round_sd(_mm_set_sd(x),_mm_set_sd(y),FP_RN));}
[[gnu::always_inline]] static inline double fp_rn_sqrt(double x){return _mm_cvtsd_f64(_mm_sqrt_round_sd(_mm_set_sd(x),_mm_set_sd(x),FP_RN));}
[[gnu::always_inline]] static inline double fp_rn_fma(double x,double y,double z){
    return _mm_cvtsd_f64(_mm_fmadd_round_sd(_mm_set_sd(x),_mm_set_sd(y),_mm_set_sd(z),FP_RN));}
// Round to binary32 and widen back (the widening of a normal binary32 or zero is exact and raises nothing).
[[gnu::always_inline]] static inline double fp_rn_single(double x){
    return _mm_cvtsd_f64(_mm_cvtss_sd(_mm_setzero_pd(),_mm_cvt_roundsd_ss(_mm_setzero_ps(),_mm_set_sd(x),FP_RN)));}
#undef FP_RN
#else
#define FP_HOST_NEEDS_MXCSR 1
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_add(double x,double y){return x+y;}
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_sub(double x,double y){return x-y;}
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_mul(double x,double y){return x*y;}
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_div(double x,double y){return x/y;}
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_sqrt(double x){return std::sqrt(x);}
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_fma(double x,double y,double z){return std::fma(x,y,z);}
[[maybe_unused,gnu::always_inline]] static inline double fp_rn_single(double x){return static_cast<double>(static_cast<float>(x));}
#endif
[[maybe_unused]]
#if defined(__GNUC__) && !defined(__clang__)
__attribute__((noinline,optimize("fp-contract=off")))
#elif defined(__clang__)
__attribute__((noinline))
#endif
static bool fp_host_arithmetic(PpuState& s,std::uint64_t a,std::uint64_t b,
        std::uint64_t c,unsigned precision,unsigned operation,std::uint64_t& out) {
#if defined(__SSE2__) && FLT_EVAL_METHOD == 0 && FLT_RADIX == 2 && DBL_MANT_DIG == 53 && FLT_MANT_DIG == 24 && !defined(__FAST_MATH__) && !defined(ANYPS3_FP_FORCE_SOFT)
#if defined(__clang__)
#pragma clang fp contract(off)
#endif
    // RN=nearest, guest traps disabled. NI is preserved; the accepted normal
    // cases are IEEE even under Cell NI. Without AVX-512F (FP_HOST_NEEDS_MXCSR)
    // check the *current thread's* MXCSR every call, never cache it across host
    // libraries: require all traps masked, nearest rounding, FTZ=DAZ=0; ignore
    // existing sticky host flags.
    if((s.fpscr&0xfbu) || (FP_HOST_NEEDS_MXCSR && (_mm_getcsr()&0xffc0u)!=0x1f80u) ||
       (precision!=53 && precision!=24) || !fp_host_operand(a) ||
       !fp_host_operand(b) || !fp_host_operand(c))return false;
    double x,y,z;std::memcpy(&x,&a,8);std::memcpy(&y,&b,8);std::memcpy(&z,&c,8);
    double rounded,residual;
    switch(operation){
    case 0:{ // TwoSum: exact x+y = rounded+residual, including cancellation.
        rounded=fp_rn_add(x,y);const double v=fp_rn_sub(rounded,x);
        residual=fp_rn_add(fp_rn_sub(x,fp_rn_sub(rounded,v)),fp_rn_sub(y,v));break;
    }
    case 1:
        rounded=fp_rn_mul(x,y);residual=fp_rn_fma(x,y,-rounded);break;
    case 2:
        if(!y)return false;
        rounded=fp_rn_div(x,y);residual=fp_rn_fma(-rounded,y,x);
        if(b>>63)residual=-residual; // sign of exact quotient - rounded
        break;
    case 3:
        if(x<0)return false;
        rounded=fp_rn_sqrt(x);residual=fp_rn_fma(-rounded,rounded,x);break;
    case 4:{
        const double product=fp_rn_mul(x,z),product_error=fp_rn_fma(x,z,-product);
        const double sum=fp_rn_add(product,y),v=fp_rn_sub(sum,product);
        const double sum_error=fp_rn_add(fp_rn_sub(product,fp_rn_sub(sum,v)),fp_rn_sub(y,v));
        rounded=fp_rn_fma(x,z,y);
        // When the rounded unfused and fused sums agree, the exact residual
        // is product_error+sum_error. Only its sign/zero is needed; the range
        // bound above makes that addition safe even after cancellation.
        // Otherwise an expansion is needed: keep the proven integer path.
        if(sum!=rounded)return false;
        residual=fp_rn_add(product_error,sum_error);break;
    }
    default:return false;
    }
    std::uint64_t bits;std::memcpy(&bits,&rounded,8);
    if(precision==24){
        const unsigned exponent=(bits>>52)&2047;
        // Leave the binary32 underflow/overflow boundaries to fp_result,
        // including tininess BEFORE rounding. Zero here is always exact.
        if((bits&UINT64_C(0x7fffffffffffffff)) && (exponent<898 || exponent>1149))return false;
        // The sole double-rounding hazard: binary64 landed on a binary32
        // midpoint. Fall back even if the double residual happens to be zero.
        if((bits&UINT64_C(0x1fffffff))==UINT64_C(0x10000000))return false;
        const double single=fp_rn_single(rounded);
        // A distinct binary64 value and widened binary32 differ by >=1 double
        // ulp; the <=half-ulp double rounding error cannot reverse that sign.
        if(single!=rounded)residual=fp_rn_sub(rounded,single);
        rounded=single;std::memcpy(&bits,&rounded,8);
    }
    const bool inexact=residual!=0;
    fp_inexact(s,inexact,inexact && ((residual<0)!=bool(bits>>63)));
    const unsigned classification=(bits&UINT64_C(0x7fffffffffffffff)) ? (bits>>63?8:4) : (bits>>63?18:2);
    s.fpscr=(s.fpscr&~UINT32_C(0x1f000))|(classification<<12);
    out=bits;return true;
#else
    (void)s;(void)a;(void)b;(void)c;(void)precision;(void)operation;(void)out;
    return false;
#endif
}

static std::uint64_t fp_result(PpuState& s, bool sign, int exp, std::uint64_t q, bool inexact, bool up, unsigned precision = 53) {
    const int minimum=precision==24?-126:-1022;
    const bool tiny=q && exp<minimum;
    if(tiny){
        // q is unrounded; its fractional remainder is represented by inexact.
        // Shift first and use that sticky remainder for one final rounding,
        // including quotients and values straddling a subnormal midpoint.
        const unsigned shift=minimum-exp;
        if(shift>=64){inexact|=q!=0;q=0;up=false;}
        else{
            const auto rest=q&((UINT64_C(1)<<shift)-1),half=UINT64_C(1)<<(shift-1);
            q>>=shift;
            up=rest>half || (rest==half && (inexact || (q&1)));
            inexact|=rest!=0;
        }
        exp=minimum;
        if(inexact){
            if(!(s.fpscr & UINT32_C(0x08000000)))s.fpscr|=UINT32_C(0x80000000);
            s.fpscr|=UINT32_C(0x08000000); // UX, tininess before rounding.
        }
    }
    // 'up' is the nearest-even increment. Directed modes depend on the
    // sign and every discarded bit, after denormalization (no double rounding).
    switch(s.fpscr & 3){
    case 1: up=false;break;
    case 2: up=inexact && !sign;break;
    case 3: up=inexact && sign;break;
    }
    if (up) ++q;
    if (q == (UINT64_C(1) << precision)) { q >>= 1; ++exp; }
    if (q && exp > (precision == 24 ? 127 : 1023)) {
        // Book I p.98: disabled overflow always sets OX, XX and FI.
        const unsigned rn=s.fpscr&3;
        const bool infinity=rn==0 || (rn==2 && !sign) || (rn==3 && sign);
        fp_exception(s,UINT32_C(0x10000000));
        fp_inexact(s,true,false); // FR undefined on overflow; choose zero.
        const auto magnitude=infinity?UINT64_C(0x7ff0000000000000):
            precision==24?UINT64_C(0x47efffffe0000000):UINT64_C(0x7fefffffffffffff);
        s.fpscr=(s.fpscr&~UINT32_C(0x1f000))|((infinity?(sign?9:5):(sign?8:4))<<12);
        return (std::uint64_t(sign)<<63)|magnitude;
    }
    const bool subnormal=q && q<(UINT64_C(1)<<(precision-1));
    q <<= 53 - precision;
    fp_inexact(s, inexact, up);
    const unsigned classification = q ? (subnormal ? (sign ? 24 : 20) : (sign ? 8 : 4)) : (sign ? 18 : 2);
    s.fpscr = (s.fpscr & ~UINT32_C(0x1f000)) | (classification << 12);
    if(subnormal){
        if(precision==53)return (std::uint64_t(sign)<<63)|q;
        // FPRs hold widened binary32 subnormals as normal binary64 values.
        const unsigned shift=__builtin_clzll(q)-11;q<<=shift;exp-=shift;
    }
    return (std::uint64_t(sign) << 63) | (q ? (std::uint64_t(exp + 1023) << 52) | (q & UINT64_C(0xfffffffffffff)) : 0);
}
static std::uint64_t fp_pack(PpuState& s, bool sign, Wide magnitude, int scale, unsigned precision = 53) {
    if (!magnitude) return fp_result(s, sign, 0, 0, false, false);
    const unsigned bits=fp_bits(magnitude);
    const int shift = int(bits) - int(precision);
    std::uint64_t q;
    bool inexact = false, up = false;
    if (shift > 0) {
        q = static_cast<std::uint64_t>(magnitude >> shift);
        const Wide rest = magnitude & ((Wide(1) << shift) - 1);
        const Wide half = Wide(1) << (shift - 1);
        inexact = rest != 0;
        up = rest > half || (rest == half && (q & 1));
    } else q = static_cast<std::uint64_t>(magnitude << -shift);
    return fp_result(s, sign, int(bits) - 1 + scale, q, inexact, up, precision);
}
[[maybe_unused]] static std::uint64_t fp_from_integer(PpuState& s, std::uint64_t value) {
    fp_materialize(s);fp_mode(s);
    const bool sign = bool(value >> 63);
    return fp_pack(s, sign, sign ? UINT64_C(0) - value : value, 0);
}
[[maybe_unused]] static std::uint64_t fp_multiply(PpuState& s, std::uint64_t a, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t host_result;if(fp_host_arithmetic(s,a,b,0,precision,1,host_result))return host_result;
    fp_mode(s);std::uint64_t nan=0;if(fp_nan_result(s,{a,b},precision,nan))return nan;
    if (fp_is_infinite(a) || fp_is_infinite(b)) {
        if (!(a & UINT64_C(0x7fffffffffffffff)) || !(b & UINT64_C(0x7fffffffffffffff))) {
            fp_exception(s,UINT32_C(0x00100000)); // VXIMZ: infinity times zero.
            return fp_special_result(s,UINT64_C(0x7ff8000000000000));
        }
        return fp_special_result(s,((a^b)&UINT64_C(0x8000000000000000))|UINT64_C(0x7ff0000000000000));
    }
    const auto x = fp_parts(a), y = fp_parts(b);
    return fp_pack(s, x.sign != y.sign, Wide(x.significand) * y.significand, x.exponent + y.exponent - 104, precision);
}
static std::uint64_t fp_fused_sum(PpuState& s, std::uint64_t a, std::uint64_t c, std::uint64_t b, unsigned precision) {
    std::uint64_t host_result;if(fp_host_arithmetic(s,a,b,c,precision,4,host_result))return host_result;
    fp_mode(s);
    const auto am=a & UINT64_C(0x7fffffffffffffff),cm=c & UINT64_C(0x7fffffffffffffff);
    const bool zero_product=(am==UINT64_C(0x7ff0000000000000) && !cm) || (cm==UINT64_C(0x7ff0000000000000) && !am);
    // Invalid multiplication is recorded even when FRB supplies the NaN.
    if(zero_product)fp_exception(s,UINT32_C(0x00100000));
    std::uint64_t nan=0;if(fp_nan_result(s,{a,b,c},precision,nan))return nan;
    if(zero_product){
        // VXIMZ already recorded; the default QNaN is not negated.
        return fp_special_result(s,UINT64_C(0x7ff8000000000000));
    }
    const std::uint64_t sign_bit=UINT64_C(0x8000000000000000);
    if(am==UINT64_C(0x7ff0000000000000) || cm==UINT64_C(0x7ff0000000000000)){
        const auto product=((a^c)&sign_bit)|UINT64_C(0x7ff0000000000000);
        if(fp_is_infinite(b) && ((b^product)&sign_bit)){
            fp_exception(s,UINT32_C(0x00800000)); // VXISI: infinite product minus infinity.
            return fp_special_result(s,UINT64_C(0x7ff8000000000000));
        }
        return fp_special_result(s,product);
    }
    if(fp_is_infinite(b))return fp_special_result(s,b);
    const auto x=fp_parts(a), y=fp_parts(c), z=fp_parts(b);
    Wide product=Wide(x.significand)*y.significand, addend=z.significand;
    const bool product_sign=x.sign!=y.sign;
    if (!product && !addend) return fp_result(s, product_sign==z.sign?product_sign:(s.fpscr&3)==3, 0, 0, false, false);
    const int product_scale=x.exponent+y.exponent-104, addend_scale=z.exponent-52;
    if (!product) return fp_pack(s,z.sign,addend,addend_scale,precision);
    if (!addend) return fp_pack(s,product_sign,product,product_scale,precision);
    // Keep the exact product's 106 bits plus guard space. Only a much smaller
    // operand can lose low bits; a sticky bit preserves its rounding effect.
    const int scale=std::max(int(fp_bits(product))-1+product_scale,int(fp_bits(addend))-1+addend_scale)-120;
    auto align=[&](Wide value,int input_scale){
        const int shift=input_scale-scale;
        if(shift>=0)return value<<shift;
        const unsigned right=-shift;
        if(right>=128)return Wide(value!=0);
        return (value>>right)|Wide((value&((Wide(1)<<right)-1))!=0);
    };
    product=align(product,product_scale);addend=align(addend,addend_scale);
    if(product_sign==z.sign)return fp_pack(s,product_sign,product+addend,scale,precision);
    if(product==addend)return fp_result(s,(s.fpscr&3)==3,0,0,false,false);
    return product>addend ? fp_pack(s,product_sign,product-addend,scale,precision)
                         : fp_pack(s,z.sign,addend-product,scale,precision);
}
[[maybe_unused]] static std::uint64_t fp_fused(PpuState& s, std::uint64_t a, std::uint64_t c, std::uint64_t b, unsigned precision = 53, bool negate = false) {
    auto value=fp_fused_sum(s,a,c,b,precision);
    // fnmadd/fnmsub negate AFTER rounding; NaNs are never negated (Book I p.114).
    if(negate && !fp_is_nan(value)){
        value^=UINT64_C(0x8000000000000000);
        // All non-NaN FPRF classes encode the sign in bits FL/FG, or C+FL.
        const unsigned before=(s.fpscr>>12)&31;
        const unsigned after=(before==2?18:before==18?2:before^12);
        s.fpscr=(s.fpscr&~UINT32_C(0x1f000))|(after<<12);
    }
    return value;
}
[[maybe_unused]] static std::uint64_t fp_divide(PpuState& s, std::uint64_t a, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t host_result;if(fp_host_arithmetic(s,a,b,0,precision,2,host_result))return host_result;
    fp_mode(s);std::uint64_t nan=0;if(fp_nan_result(s,{a,b},precision,nan))return nan;
    const auto am=a & UINT64_C(0x7fffffffffffffff),bm=b & UINT64_C(0x7fffffffffffffff);
    const bool ai=fp_is_infinite(a),bi=fp_is_infinite(b),sign=bool((a^b)>>63);
    if ((!am && !bm) || (ai && bi)) {
        fp_exception(s,ai ? UINT32_C(0x00400000) : UINT32_C(0x00200000)); // VXIDI / VXZDZ.
        return fp_special_result(s,UINT64_C(0x7ff8000000000000));
    }
    if (ai) return fp_special_result(s,(std::uint64_t(sign)<<63)|UINT64_C(0x7ff0000000000000));
    if (bi) return fp_result(s,sign,0,0,false,false);
    if (!bm) {
        fp_exception(s,UINT32_C(0x04000000)); // ZX only for a finite nonzero dividend.
        return fp_special_result(s,(std::uint64_t(sign)<<63)|UINT64_C(0x7ff0000000000000));
    }
    const auto x = fp_parts(a), y = fp_parts(b);
    if (!x.significand) return fp_result(s, x.sign != y.sign, 0, 0, false, false);
    const bool smaller = x.significand < y.significand;
    const Wide numerator = Wide(x.significand) << (precision - 1 + unsigned(smaller));
    const auto q = static_cast<std::uint64_t>(numerator / y.significand);
    const auto rest = static_cast<std::uint64_t>(numerator % y.significand);
    const bool up = 2 * rest > y.significand || (2 * rest == y.significand && (q & 1));
    return fp_result(s, x.sign != y.sign, x.exponent - y.exponent - int(smaller), q, rest != 0, up, precision);
}
[[maybe_unused]] static std::uint64_t fp_sqrt(PpuState& s, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t host_result;if(fp_host_arithmetic(s,b,0,0,precision,3,host_result))return host_result;
    fp_mode(s);std::uint64_t nan=0;if(fp_nan_result(s,{b},precision,nan))return nan;
    if((b>>63) && (b&UINT64_C(0x7fffffffffffffff))){
        fp_exception(s,UINT32_C(0x00000200)); // VXSQRT, including -infinity.
        return fp_special_result(s,UINT64_C(0x7ff8000000000000));
    }
    if(fp_is_infinite(b))return fp_special_result(s,b);
    const auto x=fp_parts(b);
    if(!x.significand)return fp_result(s,x.sign,0,0,false,false,precision); // sqrt(+-0)=+-0
    // value = m * 2^e with an even e; m is widened so the integer root has
    // at least 55 bits. The remainder supplies a sticky bit for one rounding.
    int e=x.exponent-52;Wide m=x.significand;
    if(e&1){m<<=1;--e;}
    const unsigned widen=2*((112-fp_bits(m))/2);m<<=widen;e-=int(widen);
    Wide root=0,rest=m;
    for(Wide bit=Wide(1)<<(fp_bits(m)&~1u);bit;bit>>=2){
        if(rest>=root+bit){rest-=root+bit;root=(root>>1)+bit;}
        else root>>=1;
    }
    // The wide root plus sticky bit is rounded once, also for fsqrts.
    return fp_pack(s,false,(root<<1)|Wide(rest!=0),e/2-1,precision);
}
[[maybe_unused]] static std::uint64_t fp_estimate(PpuState& s, std::uint64_t b, bool root) {
    // Book I pp.125-126 permits implementation-dependent estimates. Use
    // rounded 1/b or the reciprocal of rounded sqrt(b), not Cell LUT bits.
    // XX/FR/FI are undefined for estimates; choose to preserve the old XX.
    fp_materialize(s);const auto before=s.fpscr;
    const auto value=root && b==UINT64_C(0x7ff0000000000000) ? (fp_mode(s),fp_result(s,false,0,0,false,false))
        : fp_divide(s,UINT64_C(0x3ff0000000000000),root?fp_sqrt(s,b):b,root?53:24);
    const bool added=s.fpscr&~before&UINT32_C(0x1df80700); // new exception bits other than XX
    s.fpscr=(s.fpscr&~UINT32_C(0x82000000))|(before&UINT32_C(0x82000000))|(added?UINT32_C(0x80000000):0);
    return value;
}
[[maybe_unused]] static std::uint64_t fp_add(PpuState& s, std::uint64_t a, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t host_result;if(fp_host_arithmetic(s,a,b,0,precision,0,host_result))return host_result;
    fp_mode(s);std::uint64_t nan=0;if(fp_nan_result(s,{a,b},precision,nan))return nan;
    const bool ai=fp_is_infinite(a),bi=fp_is_infinite(b);
    if (ai || bi) {
        if (ai && bi && ((a^b)>>63)) {
            fp_exception(s,UINT32_C(0x00800000)); // VXISI: magnitude subtraction of infinities.
            return fp_special_result(s,UINT64_C(0x7ff8000000000000));
        }
        return fp_special_result(s,ai ? a : b);
    }
    auto x = fp_parts(a), y = fp_parts(b);
    if (!x.significand && !y.significand) return fp_result(s, x.sign==y.sign?x.sign:(s.fpscr&3)==3, 0, 0, false, false);
    if (!x.significand) return fp_pack(s, y.sign, y.significand, y.exponent - 52, precision);
    if (!y.significand) return fp_pack(s, x.sign, x.significand, x.exponent - 52, precision);
    if (x.exponent < y.exponent) std::swap(x, y);
    const int gap = std::min(x.exponent - y.exponent, 64);
    // For a larger gap, one sticky bit suffices for either target precision.
    const Wide large = Wide(x.significand) << gap;
    const Wide small = x.exponent - y.exponent > 64 ? 1 : y.significand;
    const int scale = x.exponent - gap - 52;
    if (x.sign == y.sign) return fp_pack(s, x.sign, large + small, scale, precision);
    if (large == small) return fp_result(s, (s.fpscr&3)==3, 0, 0, false, false);
    return large > small ? fp_pack(s, x.sign, large - small, scale, precision)
                         : fp_pack(s, y.sign, small - large, scale, precision);
}
[[maybe_unused]] static std::uint64_t fp_to_integer(PpuState& s, std::uint64_t value, unsigned width=64, bool truncate=true) {
    fp_materialize(s);fp_mode(s);
    // Book I fctiw[z]/fctid[z]: NaN, infinities and out-of-range values give the
    // saturated integer with VXCVI (plus VXSNAN for SNaN); FR/FI cleared, FPRF
    // undefined (preserved). The 32-bit forms sign-extend into the undefined
    // high word, as the in-range negative path below does.
    const std::uint64_t minimum = width == 32 ? UINT64_C(0xffffffff80000000) : UINT64_C(0x8000000000000000);
    const std::uint64_t maximum = width == 32 ? UINT64_C(0x7fffffff) : UINT64_C(0x7fffffffffffffff);
    auto invalid=[&](bool signaling,bool negative){
        if(signaling){if(!(s.fpscr & UINT32_C(0x01000000)))s.fpscr|=UINT32_C(0x80000000);s.fpscr|=UINT32_C(0x01000000);}
        if(!(s.fpscr & UINT32_C(0x00000100)))s.fpscr|=UINT32_C(0x80000000);
        s.fpscr=(s.fpscr|UINT32_C(0x20000100))&~UINT32_C(0x60000);
        return negative ? minimum : maximum;
    };
    if (fp_is_nan(value)) return invalid(!(value & UINT64_C(0x8000000000000)), true);
    if (fp_is_infinite(value)) return invalid(false, value >> 63);
    const auto p = fp_parts(value);
    std::uint64_t magnitude = 0; bool inexact = false, up = false;
    const unsigned rn=truncate?1:s.fpscr&3;
    if (p.significand) {
        if (p.exponent < 0) {
            inexact=true;
            up=p.exponent==-1 && p.significand>UINT64_C(0x10000000000000);
        }
        else if (p.exponent < 52) {
            const unsigned shift = 52 - p.exponent;
            magnitude = p.significand >> shift;
            const auto rest=p.significand & ((UINT64_C(1)<<shift)-1),half=UINT64_C(1)<<(shift-1);
            inexact=rest!=0;
            up=rest>half || (rest==half && (magnitude&1));
        } else {
            if (p.exponent > 63) return invalid(false, p.sign);
            magnitude = p.significand << (p.exponent - 52);
        }
        if(rn==1)up=false;
        if(rn==2)up=inexact && !p.sign;
        if(rn==3)up=inexact && p.sign;
        if(up)++magnitude;
        // Validate the rounded integer (Book I appendix A.2).
        if (magnitude > (UINT64_C(1) << (width-1)) || (!p.sign && magnitude == (UINT64_C(1) << (width-1))))
            return invalid(false, p.sign);
    }
    fp_inexact(s, inexact, up); // FPRF is undefined for integer conversions; preserve it.
    return p.sign ? UINT64_C(0) - magnitude : magnitude;
}
[[maybe_unused,gnu::noinline,gnu::cold]] static std::uint64_t fp_widen_special(std::uint32_t value) {
    const auto sign = std::uint64_t(value >> 31) << 63;
    const unsigned exp = (value >> 23) & 255;
    std::uint64_t fraction = value & 0x7fffff;
    if (exp == 255) return sign | UINT64_C(0x7ff0000000000000) | (fraction << 29);
    if (!exp) {
        if (!fraction) return sign;
        int e = -126;
        while (!(fraction & 0x800000)) { fraction <<= 1; --e; }
        return sign | (std::uint64_t(e + 1023) << 52) | ((fraction & 0x7fffff) << 29);
    }
    return sign | (std::uint64_t(exp + 896) << 52) | (fraction << 29);
}
// lfs/lfsx (Power ISA DOUBLE, bit-exact widening). Inlined common cases: a normal binary32 (biased exponent 1..254) keeps sign and
// fraction and is rebiased by 896 (the exponent field cannot carry into the sign: 254+896 < 2048); +-0 keeps only the sign.
// Denormals, infinities and NaNs (no quieting) take the out-of-line helper.
[[maybe_unused,gnu::always_inline]] static inline std::uint64_t fp_widen(std::uint32_t value) {
    if (__builtin_expect(((value >> 23) & 255) - 1u < 254u, 1))
        return (std::uint64_t(value & 0x80000000u) << 32) | ((std::uint64_t(value & 0x7fffffffu) << 29) + (UINT64_C(896) << 52));
    if (!(value & 0x7fffffffu)) return std::uint64_t(value) << 32;
    return fp_widen_special(value);
}
[[maybe_unused]] static std::uint32_t fp_narrow_exact(std::uint64_t value) {
    const auto sign = static_cast<std::uint32_t>(value >> 32) & 0x80000000;
    const unsigned exp = (value >> 52) & 2047;
    const auto frac = value & UINT64_C(0xfffffffffffff);
    std::uint32_t out = sign;
    if (exp == 2047) out |= 0x7f800000 | (frac >> 29);
    else if (exp >= 897 && exp <= 1150) out |= ((exp - 896) << 23) | (frac >> 29);
    else if (exp >= 874 && exp <= 896) out |= (frac | (UINT64_C(1) << 52)) >> (926 - exp);
    if (fp_widen(out) != value)
        throw std::runtime_error("stfs requires a single-precision-representable value in this prototype");
    return out;
}
[[maybe_unused,gnu::noinline,gnu::cold]] static std::uint32_t fp_store_single_tiny(std::uint64_t value) {
    const unsigned exp = (value >> 52) & 2047;
    if (exp < 874) throw std::runtime_error("stfs of a value below single denormal range is architecturally undefined");
    const auto fraction = (value & UINT64_C(0xfffffffffffff)) | (UINT64_C(1) << 52);
    return static_cast<std::uint32_t>(((value >> 63) << 31) | (fraction >> (926 - exp)));
}
[[maybe_unused,gnu::always_inline]] static inline std::uint32_t fp_store_single(std::uint64_t value) {
    // Power ISA SINGLE (store conversion): bit selection without rounding; operands below the binary32 normal range out of line.
    const unsigned exp = (value >> 52) & 2047;
    if (__builtin_expect(exp > 896 || !(value & UINT64_C(0x7fffffffffffffff)), 1))
        return static_cast<std::uint32_t>(((value >> 62) << 30) | ((value >> 29) & 0x3fffffff));
    return fp_store_single_tiny(value);
}
static void fp_nan_flag(PpuState& s, std::uint32_t flag) {
    if (~s.fpscr & flag) s.fpscr |= UINT32_C(0x80000000); // FX on a new exception bit.
    s.fpscr |= flag | UINT32_C(0x20000000);                  // and the VX summary.
}
[[maybe_unused]] static void fp_compare(PpuState& s, unsigned field, std::uint64_t a, std::uint64_t b, bool ordered = false) {
    fp_materialize(s);fp_mode(s);
    const auto am = a & UINT64_C(0x7fffffffffffffff), bm = b & UINT64_C(0x7fffffffffffffff);
    const bool sign = a >> 63;
    unsigned result;
    if (fp_is_nan(a) || fp_is_nan(b)) {
        // Unordered. SNaN sets VXSNAN; fcmpo also sets VXVC (for QNaN, or SNaN with VE=0).
        result = 1;
        const bool signaling = (fp_is_nan(a) && !(a & UINT64_C(0x8000000000000))) || (fp_is_nan(b) && !(b & UINT64_C(0x8000000000000)));
        if (signaling) fp_nan_flag(s, UINT32_C(0x01000000));
        if (ordered) fp_nan_flag(s, UINT32_C(0x00080000));
    }
    // Infinities order correctly by magnitude bits; fp_mode already rejects traps.
    else if ((!am && !bm) || a == b) result = 2;
    else if ((a ^ b) >> 63) result = sign ? 8 : 4;
    else result = ((a < b) != sign) ? 8 : 4;
    const unsigned shift = (7 - field) * 4;
    s.cr = (s.cr & ~(UINT32_C(15) << shift)) | (result << shift);
    s.fpscr = (s.fpscr & ~UINT32_C(0xf000)) | (result << 12);
}

// ---- Lazy FR/FI/FPRF ---------------------------------------------------------
// With XX already set, RN rounding and every exception disabled, an arithmetic
// operation on in-range operands (fp_host_operand: zero or normal with exponent
// in [-400,400]) cannot set any other sticky bit: no overflow, underflow,
// invalid operation or zero divide (zero divisors and negative square roots
// are excluded). FX therefore cannot change, and the operation only rewrites
// the non-sticky FR, FI and FPRF. The fast path returns the host result and
// records the operation; fp_materialize replays its exact FPSCR update before
// FPSCR is observed (mffs/mcrfs/mtfs*, compares, conversions, estimates, frsp,
// VMX scopes, any exact arithmetic, and anyps3_entry exit). A later lazy
// operation overwrites FR/FI/FPRF completely, so only the last one is kept.
// Results: one IEEE rounding on the host; binary32 forms reuse the
// fp_host_arithmetic midpoint/range guards (no double-rounding hazard).
[[gnu::noinline,gnu::cold]] static void fp_materialize_pending(PpuState& s){
    const unsigned op=s.fp_lazy_op,precision=s.fp_lazy_precision;s.fp_lazy_op=0;
    const auto a=s.fp_lazy_a,b=s.fp_lazy_b,c=s.fp_lazy_c;
    switch(op){
    case 1:(void)fp_add(s,a,b,precision);break;
    case 2:(void)fp_multiply(s,a,b,precision);break;
    case 3:(void)fp_divide(s,a,b,precision);break;
    case 4:(void)fp_sqrt(s,a,precision);break;
    case 5:(void)fp_fused(s,a,c,b,precision,false);break;
    case 6:(void)fp_fused(s,a,c,b,precision,true);break;
    default:throw std::runtime_error("invalid lazy PPU FP operation");
    }
}
static inline void fp_materialize(PpuState& s){if(__builtin_expect(s.fp_lazy_op!=0,0))fp_materialize_pending(s);}
[[maybe_unused]] static std::uint32_t fp_read_fpscr(PpuState& s){fp_materialize(s);return s.fpscr;}
// Op: 1 add (a+b), 2 multiply (a*b), 3 divide (a/b), 4 sqrt(a), 5 a*c+b, 6 -(a*c+b).
// Small (no residual, no FPSCR read-modify-write): inlined into the lifted
// pages. Contraction cannot fuse across these single operations: every result
// leaves through an integer bit copy and the build uses -ffp-contract=off/ISO.
[[maybe_unused,gnu::always_inline]] static inline bool fp_lazy(PpuState& s,std::uint64_t a,std::uint64_t b,std::uint64_t c,unsigned precision,unsigned op,std::uint64_t& out){
#if defined(__SSE2__) && FLT_EVAL_METHOD == 0 && FLT_RADIX == 2 && DBL_MANT_DIG == 53 && FLT_MANT_DIG == 24 && !defined(__FAST_MATH__) && !defined(ANYPS3_FP_FORCE_SOFT)
#if defined(__clang__)
#pragma clang fp contract(off)
#endif
    if((s.fpscr&UINT32_C(0x020000fb))!=UINT32_C(0x02000000) || (precision!=53 && precision!=24) || !fp_host_operand(a) ||
       (op!=4 && !fp_host_operand(b)) || (op>=5 && !fp_host_operand(c)) || (FP_HOST_NEEDS_MXCSR && (_mm_getcsr()&0xffc0u)!=0x1f80u))return false;
    double x,y,z;std::memcpy(&x,&a,8);std::memcpy(&y,&b,8);std::memcpy(&z,&c,8);
    double rounded;
    switch(op){
    case 1:rounded=fp_rn_add(x,y);break;
    case 2:rounded=fp_rn_mul(x,y);break;
    case 3:if(!y)return false;rounded=fp_rn_div(x,y);break;
    case 4:if(x<0)return false;rounded=fp_rn_sqrt(x);break;
    default:rounded=fp_rn_fma(x,z,y);break;
    }
    std::uint64_t bits;std::memcpy(&bits,&rounded,8);
    if(precision==24){
        const unsigned exponent=(bits>>52)&2047;
        if((bits&UINT64_C(0x7fffffffffffffff)) && (exponent<898 || exponent>1149))return false;
        if((bits&UINT64_C(0x1fffffff))==UINT64_C(0x10000000))return false;
        const double single=fp_rn_single(rounded);std::memcpy(&bits,&single,8);
    }
    // fnmadd/fnmsub negate after rounding (never a NaN here).
    if(op==6)bits^=UINT64_C(0x8000000000000000);
    s.fp_lazy_a=a;s.fp_lazy_b=b;s.fp_lazy_c=c;s.fp_lazy_op=static_cast<std::uint8_t>(op);s.fp_lazy_precision=static_cast<std::uint8_t>(precision);
    out=bits;return true;
#else
    (void)s;(void)a;(void)b;(void)c;(void)precision;(void)op;(void)out;
    return false;
#endif
}
// Lifted code uses the lazy forms; the plain helpers stay exact (eager FPSCR).
// The XX/mode test is inlined so a cleared XX costs no extra call. An exact
// arithmetic operation rewrites FR/FI/FPRF completely, but a pending one is
// still replayed first so that a STOP inside it reports the exact FPSCR.
[[gnu::always_inline]] static inline bool fp_lazy_mode(const PpuState& s){return (s.fpscr&UINT32_C(0x020000fb))==UINT32_C(0x02000000);}
[[maybe_unused]] static std::uint64_t fp_add_lazy(PpuState& s, std::uint64_t a, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t r;return fp_lazy_mode(s) && fp_lazy(s,a,b,0,precision,1,r)?r:(fp_materialize(s),fp_add(s,a,b,precision));
}
[[maybe_unused]] static std::uint64_t fp_multiply_lazy(PpuState& s, std::uint64_t a, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t r;return fp_lazy_mode(s) && fp_lazy(s,a,b,0,precision,2,r)?r:(fp_materialize(s),fp_multiply(s,a,b,precision));
}
[[maybe_unused]] static std::uint64_t fp_divide_lazy(PpuState& s, std::uint64_t a, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t r;return fp_lazy_mode(s) && fp_lazy(s,a,b,0,precision,3,r)?r:(fp_materialize(s),fp_divide(s,a,b,precision));
}
[[maybe_unused]] static std::uint64_t fp_sqrt_lazy(PpuState& s, std::uint64_t b, unsigned precision = 53) {
    std::uint64_t r;return fp_lazy_mode(s) && fp_lazy(s,b,0,0,precision,4,r)?r:(fp_materialize(s),fp_sqrt(s,b,precision));
}
[[maybe_unused]] static std::uint64_t fp_fused_lazy(PpuState& s, std::uint64_t a, std::uint64_t c, std::uint64_t b, unsigned precision = 53, bool negate = false) {
    std::uint64_t r;return fp_lazy_mode(s) && fp_lazy(s,a,b,c,precision,negate?6:5,r)?r:(fp_materialize(s),fp_fused(s,a,c,b,precision,negate));
}
'''
