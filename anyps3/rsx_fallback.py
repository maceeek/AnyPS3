"""Opt-in process lifetime fallback counts, bounded details and host wall time."""

CPP = r'''
struct FallbackStats {
    bool enabled=false;std::uint64_t draws=0,interval=4096,gpu_draws=0,cpu_fallback_draws=0;
    std::uint64_t gpu_attempt_ns=0,gpu_flush_ns=0,cpu_fallback_ns=0;
    std::string last_rejection;
    std::map<std::string,std::uint64_t> reasons,paths,flush_calls,flush_submits,flush_readbacks,flush_time,flush_retire_time;
    std::map<std::string,std::set<std::string>> details;
    FallbackStats(){
        const auto* e=std::getenv("ANYPS3_RSX_FALLBACK_STATS");enabled=e && std::string(e)=="1";
        if(const auto* n=std::getenv("ANYPS3_RSX_FALLBACK_INTERVAL")){
            char* end=nullptr;const auto v=std::strtoull(n,&end,10);
            if(!*n || *end || *n=='-' || !v)throw std::runtime_error("invalid ANYPS3_RSX_FALLBACK_INTERVAL");
            interval=v;
        }
    }
    void detail(const char* reason,const std::string& value){
        if(!enabled)return;
        auto& values=details[reason];if(values.size()>=32)return;
        if(values.insert(value).second)std::fprintf(stderr,"RSX FALLBACK DETAIL reason=%s %s\n",reason,value.c_str());
    }
    void print(const char* phase)const{
        if(!enabled)return;
        std::fprintf(stderr,"RSX FALLBACK STATS phase=%s draws=%llu",phase,static_cast<unsigned long long>(draws));
        for(const auto& p:reasons)std::fprintf(stderr," %s=%llu",p.first.c_str(),static_cast<unsigned long long>(p.second));
        for(const auto& p:paths)std::fprintf(stderr," path_%s=%llu",p.first.c_str(),static_cast<unsigned long long>(p.second));
        for(const auto& p:flush_calls)std::fprintf(stderr," flush_%s_calls=%llu",p.first.c_str(),(unsigned long long)p.second);
        for(const auto& p:flush_submits)std::fprintf(stderr," flush_%s_submits=%llu",p.first.c_str(),(unsigned long long)p.second);
        for(const auto& p:flush_readbacks)std::fprintf(stderr," flush_%s_readbacks=%llu",p.first.c_str(),(unsigned long long)p.second);
        for(const auto& p:flush_time)std::fprintf(stderr," flush_%s_ms=%.3f",p.first.c_str(),p.second/1.e6);
        for(const auto& p:flush_retire_time)std::fprintf(stderr," flush_%s_retire_ms=%.3f",p.first.c_str(),p.second/1.e6);
        std::fprintf(stderr," gpu_draws=%llu cpu_fallback_draws=%llu gpu_attempt_host_ms=%.3f gpu_flush_host_ms=%.3f cpu_fallback_host_ms=%.3f\n",
            static_cast<unsigned long long>(gpu_draws),static_cast<unsigned long long>(cpu_fallback_draws),gpu_attempt_ns/1.e6,gpu_flush_ns/1.e6,cpu_fallback_ns/1.e6);
    }
    ~FallbackStats(){print("final");}
};
static FallbackStats& fallback_stats(){static FallbackStats stats;return stats;}
struct DrawAttempt {
    FallbackStats& stats;bool rejected=false;std::uint64_t flush_start=0;std::chrono::steady_clock::time_point start;
    DrawAttempt():stats(fallback_stats()){if(stats.enabled){++stats.draws;flush_start=stats.gpu_flush_ns;start=std::chrono::steady_clock::now();}}
    bool reject(const char* reason){stats.last_rejection=reason;if(stats.enabled && !rejected)++stats.reasons[reason];rejected=true;return false;}
    void path(const char* name){if(stats.enabled)++stats.paths[name];}
    ~DrawAttempt(){if(stats.enabled){stats.gpu_attempt_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count()-(stats.gpu_flush_ns-flush_start);if(!rejected && std::uncaught_exceptions()==0)++stats.gpu_draws;
        if(stats.draws%stats.interval==0){stats.print("periodic");host_stats().print();}}}
};
static std::string fragment_failure(const std::vector<FpInst>& fp,const rsxvk::FragmentFailure& failure,const Reg& r){
    std::ostringstream out;out<<"cause="<<failure.reason<<" instruction="<<failure.instruction<<" shader_control=0x"<<std::hex<<r(0x1d60)<<" fp_address=0x"<<r(0x8e4)<<" words=";
    unsigned words=0;for(unsigned n=0;n<fp.size();++n){const auto& i=fp[n];const unsigned at=i.word_offset==UINT_MAX?words:i.word_offset;
        if(n==failure.instruction)out<<"[";
        out<<at<<":"<<i.d<<","<<i.s0<<","<<i.s1<<","<<i.s2;
        if(i.has_constant)for(float v:i.constant)out<<","<<float_bits(v);
        if(n==failure.instruction)out<<"]";
        out<<";";words=at+4+(i.has_constant?4:0);
        if(n==failure.instruction){out<<"op="<<std::dec<<(((i.d>>24)&63)|((i.s1>>31)<<6))<<",scale="<<((i.s1>>28)&7)<<",dst="<<((i.d>>1)&63)<<",half="<<bool(i.d&128)<<",mask="<<((i.d>>9)&15)<<",saturate="<<bool(i.d>>31)<<",result_precision="<<((i.d>>22)&3)<<",no_dest="<<bool(i.d&(1u<<30))<<",predicate="<<((i.s0>>18)&7);
            for(unsigned source=0;source<3;++source){const unsigned raw=source==0?i.s0:source==1?i.s1:i.s2;
                out<<",src"<<source<<"={type:"<<(raw&3)<<",index:"<<((raw&3)==1?((i.d>>13)&15):((raw>>2)&63))<<",half:"<<bool(raw&256)<<",precision:"<<((i.s1>>(19+3*source))&7)<<",abs:"<<bool(raw&(1u<<(source?18:29)))<<",neg:"<<bool(raw&(1u<<17))<<",indexed:"<<bool((raw&3)==1 && (i.s2&(1u<<30)))<<"}";}
            out<<std::hex<<";";}

    }
    return out.str();
}
static std::string alias_detail(const Target& color,const Target& depth,unsigned width,unsigned height,const Reg& r){
    std::ostringstream out;out<<std::hex<<"surface=0x"<<r(0x208)<<" color_format=0x"<<(r(0x208)&31)<<" depth_format=0x"<<((r(0x208)>>5)&7);
    for(const auto* t:{&color,&depth}){out<<(t==&color?" color":" depth")<<"={offset:0x"<<t->offset<<",base_offset:0x"<<t->base_offset<<",span:0x"<<target_span(*t,width,height)<<",pitch:0x"<<t->pitch<<",bpp:"<<t->bpp<<",swizzle:"<<t->swizzle_width<<"x"<<t->swizzle_height<<",tile:"<<bool(t->tile)<<",host_ranges:";
        if(t->mapping)for(const auto& p:t->mapping->parts)out<<"[0x"<<reinterpret_cast<std::uintptr_t>(p.data)<<",0x"<<reinterpret_cast<std::uintptr_t>(p.data)+p.size<<")";
        out<<"}";}
    out<<" depth_test="<<(r(0xa74)&1)<<" depth_func=0x"<<r.get(0xa6c,0x207)<<" depth_write="<<(r(0xa70)&1)<<" stencil="<<(r(0x328)&1)<<" bounds="<<(r(0x380)&1);
    return out.str();
}
'''
