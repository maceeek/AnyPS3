"""Bounded, triggerable coherence evidence; owner context is never SPU origin."""
CPP = r'''
struct AccessTrace {
    bool enabled=false;std::string trigger;std::uint64_t emitted=0;
    AccessTrace(){const auto* v=std::getenv("ANYPS3_RSX_ACCESS_TRACE");enabled=v && std::strcmp(v,"1")==0;
        if(const auto* p=std::getenv("ANYPS3_RSX_ACCESS_TRACE_TRIGGER"))trigger=p;}
    bool active(){return enabled && emitted<4096 && (trigger.empty() || std::filesystem::exists(trigger));}
    template<class State> void record(const char* kind,const State& s,const void* pointer,std::uint64_t size,bool write,unsigned format=0,unsigned width=0,unsigned height=0,std::uint64_t label=0,std::uint64_t timeline=0){
        if(!active())return;
        const auto origin=HostAccessContext::sample();
        ++emitted;const auto at=reinterpret_cast<std::uintptr_t>(pointer);
        std::fprintf(stderr,"RSX ACCESS kind=%s write=%u origin=%s origin_id=%u origin_pc=0x%08x origin_ea=0x%llx operation=%llu owner_ppu=%u owner_pc=0x%08x host=0x%llx bytes=0x%llx format=0x%x width=%u height=%u label=0x%llx timeline=%llu",kind,unsigned(write),origin.name(),origin.guest_id,origin.guest_pc,(unsigned long long)origin.ea,(unsigned long long)origin.operation_id,s.thread_id,s.pc,(unsigned long long)at,(unsigned long long)size,format,width,height,(unsigned long long)label,(unsigned long long)timeline);
        for(const auto& r:s.memory.regions){const auto base=reinterpret_cast<std::uintptr_t>(r.bytes->data());if(at>=base && at-base<r.bytes->size())std::fprintf(stderr," guest=0x%llx",(unsigned long long)(r.base+at-base));}
        std::fputc('\n',stderr);
    }
};
static AccessTrace& access_trace(){static thread_local AccessTrace trace;return trace;}
'''
