"""Emulated vblank rate ANYPS3_VBLANK_HZ (VBLANKHZ task, headroom instrument).

Emitted once into the program header by vulkan.py (before NativeVulkan, whose presentation selection uses it); the RSX
runtime and tests/test_rsx_vblank_hz.py use the same helper.
"""

VBLANK_CPP = r'''
// ANYPS3_VBLANK_HZ: the emulated display rate as a positive integer "N" or rational "N/M" Hz (digits only, no sign/blank, no
// overflow; reduced N and M <= 1000000, N*M <= 9000000000 so the split arithmetic below cannot overflow; 1 <= rate <= 1000 Hz).
// Unset = 60000/1001, whose tick numbers and tick times are bit-identical to the former 60/1.001 arithmetic: both compute the
// exact floors of elapsed*60/1.001e9 and N*1.001e9/60 (same rationals, exact integer splits). A MEASUREMENT INSTRUMENT only:
// guest-visible refresh information (cellVideoOut state, 59.94 Hz) is not changed; GCM vblank count/time follow the ticks.
struct RsxVblankRate {
    std::uint64_t num=60000,den=1001;bool custom=false;
    std::uint64_t span()const{return den*UINT64_C(1000000000);}   // nanoseconds per `num` ticks
    std::uint64_t ticks(std::uint64_t elapsed_ns)const{return (elapsed_ns/span())*num+(elapsed_ns%span())*num/span();}   // floor(elapsed*rate)
    std::uint64_t tick_ns(std::uint64_t n)const{return (n/num)*span()+(n%num)*span()/num;}   // floor(n/rate), relative to the epoch
};
// Returns an empty string and sets `out`, or the grammar/range error.
inline std::string rsx_parse_vblank_hz(const char* text,RsxVblankRate& out){
    auto number=[](const char*& p,std::uint64_t& value)->bool{
        if(*p<'0' || *p>'9')return false;
        value=0;
        for(;*p>='0' && *p<='9';++p){if(value>(UINT64_MAX-9)/10)return false;value=value*10+unsigned(*p-'0');}
        return true;};
    const char* p=text;std::uint64_t num=0,den=1;
    if(!number(p,num))return "expected digits";
    if(*p=='/'){++p;if(!number(p,den))return "expected digits after '/'";}
    if(*p)return "unexpected character";
    if(!num || !den)return "zero numerator or denominator";
    std::uint64_t a=num,b=den;while(b){const auto t=a%b;a=b;b=t;}num/=a;den/=a;
    if(num>1000000 || den>1000000 || num*den>UINT64_C(9000000000))return "numerator/denominator too large";
    if(num<den || num>1000*den)return "rate outside 1..1000 Hz";
    out.num=num;out.den=den;out.custom=!(num==60000 && den==1001);return "";
}
inline const RsxVblankRate& rsx_vblank_rate(){   // one instance program-wide (inline)
    static const RsxVblankRate rate=[]{RsxVblankRate r;const char* v=std::getenv("ANYPS3_VBLANK_HZ");if(!v)return r;
        const auto error=rsx_parse_vblank_hz(v,r);
        if(!error.empty()){std::fprintf(stderr,"invalid ANYPS3_VBLANK_HZ=%s: %s (expected N or N/M Hz, e.g. 90 or 120000/1001)\n",v,error.c_str());std::_Exit(2);}
        if(r.custom)std::fprintf(stderr,"ANYPS3_VBLANK_HZ=%llu/%llu: emulated vblank %.3f Hz (measurement instrument; default 60000/1001)\n",
            (unsigned long long)r.num,(unsigned long long)r.den,double(r.num)/double(r.den));
        return r;}();
    return rate;
}
'''
