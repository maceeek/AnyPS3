"""Opt-in draw diagnostics. Worker counters are private until the draw joins."""

CPP = r'''
enum class HostStage {idle,indices,assembly,state,texture_metadata,targets,certification,source,geometry,vertex_upload,texture_upload,record,flush,tail,draw,kick,g_inputs,g_state,g_shaders,g_data,count};   // count <= 32 (NativeRsxWorker::stage_ns)
struct HostStats {
    bool enabled=std::getenv("ANYPS3_RSX_HOST_PROFILE")!=nullptr;
    std::array<std::uint64_t,unsigned(HostStage::count)> ns{};
    std::uint64_t texture_metadata=0,texture_materialize=0,texture_plan_hit=0,draws=0;
    std::uint64_t cpu_vertex=0,cpu_attribute=0,cpu_clip=0,cpu_texel=0,gpu_vertex=0;
    HostStage active=HostStage::idle;std::chrono::steady_clock::time_point since;
    HostStats(){if(enabled)since=std::chrono::steady_clock::now();}
    HostStage select(HostStage stage){auto previous=active;if(enabled){const auto now=std::chrono::steady_clock::now();ns[unsigned(active)]+=std::chrono::duration_cast<std::chrono::nanoseconds>(now-since).count();since=now;active=stage;}return previous;}
    void print(){if(!enabled)return;select(active);const char* names[]={"idle","indices","assembly","state","texture_metadata","targets","certification","shader_source","geometry","vertex_upload","texture_upload","record","flush","tail","draw","kick","g_inputs","g_state","g_shaders","g_data"};
        std::fprintf(stderr,"RSX HOST PROFILE draws=%llu texture_metadata=%llu texture_materialize=%llu texture_plan_hit=%llu",(unsigned long long)draws,(unsigned long long)texture_metadata,(unsigned long long)texture_materialize,(unsigned long long)texture_plan_hit);for(unsigned i=1;i<unsigned(HostStage::count);++i)std::fprintf(stderr," %s_ms=%.3f",names[i],ns[i]/1.e6);
        for(unsigned i=1;i<unsigned(HostStage::count);++i)std::fprintf(stderr," %s_us_per_draw=%.3f",names[i],draws?ns[i]/(1.e3*draws):0);
        std::fprintf(stderr," cpu_vp=%llu cpu_attribute=%llu cpu_clip=%llu cpu_texel=%llu gpu_vertex_fetch=%llu\n",(unsigned long long)cpu_vertex,(unsigned long long)cpu_attribute,(unsigned long long)cpu_clip,(unsigned long long)cpu_texel,(unsigned long long)gpu_vertex);}
    ~HostStats(){print();}
};
static HostStats& host_stats(){static thread_local HostStats stats;return stats;}
struct HostPhase {
    HostStats& stats;HostStage previous;
    explicit HostPhase(HostStage stage):stats(host_stats()),previous(stats.select(stage)){}
    void next(HostStage stage){stats.select(stage);}
    ~HostPhase(){stats.select(previous);}
};
struct PerfWork {
    std::uint64_t quads=0,fp_instructions=0,texture_samples=0,raster_ns=0,fp_ns=0,texture_ns=0;
    void add(const PerfWork& b){
        quads+=b.quads;fp_instructions+=b.fp_instructions;texture_samples+=b.texture_samples;
        raster_ns+=b.raster_ns;fp_ns+=b.fp_ns;texture_ns+=b.texture_ns;
    }
};
static thread_local PerfWork* perf_work=nullptr;
struct PerfBind {
    PerfWork* previous;
    explicit PerfBind(PerfWork* p):previous(perf_work){perf_work=p;}
    ~PerfBind(){perf_work=previous;}
};
using PerfClock=std::chrono::steady_clock;
struct PerfTimer {
    std::uint64_t* output;PerfClock::time_point start;
    explicit PerfTimer(std::uint64_t* p):output(p){if(output)start=PerfClock::now();}
    ~PerfTimer(){if(output)*output+=std::chrono::duration_cast<std::chrono::nanoseconds>(PerfClock::now()-start).count();}
};
enum class DrawReason { parallel,one_thread,small,primitive,fixed,fragment,target,vertex_texture,
    texture_alias,vertex_alias,geometry,small_geometry,pool,empty,count };
struct PerfStats {
    bool enabled=false;unsigned every=256;
    std::uint64_t draws=0,errors=0,pixels=0,wall_ns=0;
    PerfWork work;
    std::array<std::uint64_t,unsigned(DrawReason::count)> reasons{};
    PerfStats(){
        const char* p=std::getenv("ANYPS3_RSX_STATS");enabled=p && std::strcmp(p,"1")==0;
        if(const char* n=std::getenv("ANYPS3_RSX_STATS_EVERY")){
            char* end=nullptr;const auto v=std::strtoul(n,&end,10);
            if(end!=n && !*end && v && v<=UINT_MAX)every=unsigned(v);
        }
    }
    void print()const{
        const char* names[]={"parallel","one_thread","small","primitive","fixed","fragment","target",
            "vertex_texture","texture_alias","vertex_alias","geometry","small_geometry","pool","empty"};
        std::fprintf(stderr,"RSX stats draws=%llu errors=%llu pixels=%llu fragments=%llu fp_instructions=%llu texture_samples=%llu wall_ms=%.3f raster_worker_ms=%.3f fp_worker_ms=%.3f texture_worker_ms=%.3f reasons=",
            (unsigned long long)draws,(unsigned long long)errors,(unsigned long long)pixels,
            (unsigned long long)(work.quads*4),(unsigned long long)work.fp_instructions,
            (unsigned long long)work.texture_samples,wall_ns/1.e6,work.raster_ns/1.e6,work.fp_ns/1.e6,work.texture_ns/1.e6);
        for(unsigned i=0;i<reasons.size();++i)if(reasons[i])std::fprintf(stderr,"%s:%llu,",names[i],(unsigned long long)reasons[i]);
        std::fputc('\n',stderr);
    }
};
struct PerfDraw {
    PerfStats& stats;PerfWork work;DrawReason reason=DrawReason::empty;
    PerfBind bind;PerfClock::time_point start;int exceptions;std::uint64_t pixels=0;
    PerfDraw():stats(state()),bind(stats.enabled?&work:nullptr),exceptions(std::uncaught_exceptions()){
        if(stats.enabled)start=PerfClock::now();
    }
    static PerfStats& state(){static thread_local PerfStats s;return s;}
    ~PerfDraw(){
        if(!stats.enabled)return;
        ++stats.draws;stats.errors+=std::uncaught_exceptions()>exceptions;stats.pixels+=pixels;
        stats.work.add(work);++stats.reasons[unsigned(reason)];
        stats.wall_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(PerfClock::now()-start).count();
        if(stats.draws%stats.every==0)stats.print();
    }
};
'''
