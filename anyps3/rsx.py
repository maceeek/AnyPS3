"""Native RSX device memory; unknown GPU operations must stop explicitly."""

from .rsx_capture import CPP as CAPTURE_CPP, TYPES as CAPTURE_TYPES
from .rsx_surface import CPP as SURFACE_CPP
from .rsx_display import CPP as DISPLAY_CPP
from .rsx_draw import CPP as DRAW_CPP, TYPES as DRAW_TYPES
from .rsx_blit import CPP as BLIT_CPP
from .rsx_registers import TYPES as REGISTER_TYPES

TYPES = CAPTURE_TYPES + DRAW_TYPES + REGISTER_TYPES + r'''
struct NativeRsx {
    RsxPerfCapture perf_capture;
    struct Tile {bool bound=false;std::uint32_t location=0,offset=0,size=0,pitch=0,compression=0,base=0,bank=0;std::uint64_t range_word=0,format_word=0;};
    struct DisplayBuffer {std::uint32_t width=0,height=0,pitch=0,offset=0;};
    struct Zcull {bool bound=false;std::uint32_t width=0,height=0,start=0,offset=0;std::uint64_t region_word=0,offset_word=0,status_word=0;};
    std::uint32_t device=0,local_size=0,memory_handle=0;
    std::uint32_t context=0,dma=0,driver=0,reports=0,event_queue=0,event_port=0;
    std::array<std::uint32_t,256> io{};
    std::array<std::uint64_t,256> io_flags{};
    bool fifo_enabled=false,semaphore_wait=false,trace=false,vsync=true;std::uint32_t return_offset=UINT32_MAX,packet_index=0;
    std::uint64_t commands=0;
    std::uint64_t zcull_generation=0;
    std::uint64_t vertex_cache_generation=0;
    std::array<std::uint32_t,5> report_values{};
    std::uint64_t report_timestamp=0;
    bool render_disabled=false; // NV4097_SET_RENDER_ENABLE mode 2 evaluated to a zero report value
    std::uint64_t vblank_epoch_ns=0,vblank_ticks=0;
    unsigned vblank_poll_ticks=0;
    std::uint64_t display_queue_generation=0,display_queue_acknowledged=0;
    std::array<std::uint32_t,2> flip_modes{{2,2}};
    std::array<Tile,15> tiles{};
    std::array<DisplayBuffer,8> display_buffers{};
    std::array<Zcull,8> zculls{};
    RsxRegisters registers;
    RsxShaderArray<std::uint32_t,512*4> transform_constants{};
    RsxShaderArray<bool,512*4> transform_constants_written{};
    // 544 vertex-program instruction slots (RPCS3 ffba2bf0 Program/program_util.h:10 max_vertex_program_instructions).
    RsxShaderArray<std::uint32_t,544*4> transform_program{};
    RsxShaderArray<bool,544*4> transform_program_written{};
    std::uint64_t transform_program_generation=0;
    RsxDrawState draw;
#ifdef ANYPS3_VULKAN
    std::shared_ptr<VulkanHostService> host;
    std::unique_ptr<NativeVulkan> renderer;
#ifdef ANYPS3_RSX_VK
    std::shared_ptr<void> graphics_owner;
    std::function<bool(unsigned)> graphics_clear;
    void* graphics_session=nullptr; // owned by the synchronous FIFO scope
#endif
#endif
    std::uint64_t cpu_clears=0,rendered_clears=0,captured_frames=0,flips=0,diag_skipped=0;
    NativeRsx(){io.fill(UINT32_MAX);}
};
'''

CPP = r'''
static void rsx_worker_events(PpuState&,const std::function<void()>&);
static std::uint32_t rsx_worker_display_call(PpuState&);
static void rsx_worker_release(PpuState&);

#ifdef ANYPS3_RSX_VK
static void rsx_graphics_flush(PpuState& s,const char* reason="explicit");
static void rsx_capture_sync(PpuState& s);
static void rsx_graphics_sync_report(PpuState& s,std::uint64_t address);
static bool rsx_graphics_try_conditional(PpuState& s,std::uint64_t address);
static void rsx_graphics_reset_conditional(PpuState& s);
static bool rsx_graphics_resolve_conditional(PpuState& s);
static bool rsx_graphics_conditional_fault(PpuState& s);
static bool rsx_graphics_flip_async(PpuState&,unsigned,std::uint64_t,std::uint64_t,unsigned,std::uint64_t);
static void rsx_graphics_flip_prepare(PpuState&);
static bool rsx_graphics_poll(PpuState&);
static bool rsx_graphics_defer_label(PpuState&,std::uint64_t,unsigned);
static bool rsx_graphics_defer_report(PpuState&,std::uint64_t,unsigned,bool,bool);
[[maybe_unused]] static bool rsx_graphics_scanout(PpuState& s,unsigned id,std::vector<std::uint8_t>* capture,bool present);
#endif
// FRAMETIME wave: page-synchronisation requests of other threads (SPU lanes, PPU faults) used to wait for the owner's whole FIFO run or
// vsync wait (measured: mean 1.7 ms behind a run, 9 ms behind a vsync wait, 9.8 ms/frame summed over the lanes). The game worker installs a
// hook that serves such requests (RsxOwnerQueue::Request::put_generation == 1, kind HostAccess) at FIFO packet boundaries and inside the
// vsync wait; requesters count themselves in rsx_host_waiters so that the check is one relaxed load per packet.
// ANYPS3_RSX_HOST_DRAIN=0 and ANYPS3_RSX_FLIP_EARLY=0 restore the previous behaviour (A/B switches).
static std::atomic<unsigned> rsx_host_waiters{0};
static std::function<void()>& rsx_owner_hook(){static thread_local std::function<void()> hook;return hook;}
[[maybe_unused]] static inline void rsx_owner_poll(){
    if(__builtin_expect(rsx_host_waiters.load(std::memory_order_relaxed)!=0,0)){auto& hook=rsx_owner_hook();if(hook)hook();}
}
[[maybe_unused]] static bool rsx_flip_early_enabled(){static const bool on=[]{const char* v=std::getenv("ANYPS3_RSX_FLIP_EARLY");return !v || std::string(v)!="0";}();return on;}
''' + CAPTURE_CPP + SURFACE_CPP + DISPLAY_CPP + r'''
[[maybe_unused]] static void rsx_set_flip_mode(PpuState& s,unsigned head){
    std::unique_ptr<GuestMemory::ConfigurationPermit> configuration;
    if(s.process.rsx_worker.enabled && !s.process.rsx_worker.on_owner())configuration=std::make_unique<GuestMemory::ConfigurationPermit>(s.memory);
    if(!s.rsx.context || head>1 || s.r[3]<1 || s.r[3]>3)throw std::runtime_error("invalid native GCM flip mode");
    s.rsx.flip_modes[head]=s.r[3];
    if(head==1)s.rsx.vsync=s.r[3]==2;
    // Configure presentation policy. This does not queue, complete or display a frame.
    std::fprintf(stderr,"native GCM flip mode head=%u mode=%u\n",head,s.rsx.flip_modes[head]);
}
static std::uint64_t rsx_address(const NativeRsx& gpu,std::uint32_t offset){
    if(offset>=0x10000000 || gpu.io[offset>>20]==UINT32_MAX)throw std::runtime_error("RSX reads unmapped IO offset "+std::to_string(offset));
    return std::uint64_t(gpu.io[offset>>20])+(offset&0xfffff);
}
static std::uint64_t rsx_semaphore_address(const NativeRsx& gpu,std::uint32_t context,std::uint32_t offset){
    if(offset&3)throw std::runtime_error("unaligned RSX semaphore");
    switch(context){
    case 0x66606660:case 0x66616661:
        if(offset>=0x1000)throw std::runtime_error("RSX semaphore outside report area");
        return gpu.reports+offset;
    case 0x56616660:case 0x56616661:
        if(offset>=0x100000)throw std::runtime_error("RSX semaphore outside device area");
        return gpu.device+offset;
    default:throw std::runtime_error("unsupported RSX semaphore DMA context "+std::to_string(context));
    }
}
// DIAGNOSTIC ONLY (ANYPS3_RSX_DIAG_SKIP=1): log and skip an unsupported RSX
// operation instead of stopping, to enumerate further RSX gaps in one run.
// A run with skipped operations is never evidence of correct rendering/menu.
static bool rsx_diag_skip(){static const bool enabled=[]{const char* v=std::getenv("ANYPS3_RSX_DIAG_SKIP");return v && std::string(v)=="1";}();return enabled;}
template<class F> static void rsx_diag(PpuState& s,const char* what,std::uint32_t reg,F&& f){
    if(!rsx_diag_skip()){f();return;}
    try{f();}catch(const std::runtime_error& e){
        ++s.rsx.diag_skipped;
        std::fprintf(stderr,"RSX DIAG SKIP %s method=%04x skipped=%llu: %s\n",what,unsigned(reg),static_cast<unsigned long long>(s.rsx.diag_skipped),e.what());
    }
}
static std::uint32_t rsx_reg(const NativeRsx& gpu,std::uint32_t r){
    // A register never written since context allocation holds its reset
    // value. RPCS3 (rsx_methods.cpp:134-137, rsx_state::init) resets the whole
    // register file to zero; the remaining non-zero defaults there are
    // cellGcmInit commands, which the native libgcm here issues itself.
    const auto it=gpu.registers.find(r);
    return it==gpu.registers.end()?0u:it->second;
}
// Guest address of the 16-byte report at offset in the report DMA context (0x1a8).
static std::uint64_t rsx_report_address(const NativeRsx& gpu,std::uint32_t offset){
    switch(rsx_reg(gpu,0x1a8)){
    case 0x66626660:
        if(offset>=2048*16)throw std::runtime_error("RSX report outside local report area");
        return std::uint64_t(gpu.reports)+0x1400+offset;
    case 0xbad68000:return rsx_address(gpu,0x0e000000+offset);
    case 0xfeed0000:
        if(gpu.local_size<16 || offset>gpu.local_size-16)throw std::runtime_error("RSX report outside VRAM");
        return UINT64_C(0xc0000000)+offset;
    case 0xfeed0001:return rsx_address(gpu,offset);
    default:throw std::runtime_error("unsupported RSX report DMA context "+std::to_string(rsx_reg(gpu,0x1a8)));
    }
}
static void rsx_get_report(PpuState& s,std::uint32_t value){
    auto& gpu=s.rsx;auto& m=s.memory;
    const auto type=value>>24,offset=value&0xffffff;
    if(type<1 || type>5 || (offset&15))throw std::runtime_error("invalid RSX report type or alignment");
    const auto address=rsx_report_address(gpu,offset);
    const auto format=rsx_reg(gpu,0x208);bool surface_active=false;
    if(rsx_reg(gpu,0x198)==0xfeed0000)for(const auto& z:gpu.zculls){
        if(!z.bound)continue;
        if(z.offset==rsx_reg(gpu,0x214) && ((z.region_word>>36)&15)==((format>>5)&7) && ((z.region_word>>40)&15)==((format>>12)&15)){surface_active=true;break;}}
    const bool stats=rsx_reg(gpu,0x1804)!=0;
#ifdef ANYPS3_RSX_VK
    if(rsx_graphics_defer_report(s,address,type,surface_active,stats))return;
    rsx_graphics_flush(s,"report");
#endif
    m.range(address,16,true);
    const auto now=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
    const auto timestamp=std::max(now,gpu.report_timestamp+1);
    // The synchronous fallback has completed its GPU prefix above.
    auto result=gpu.report_values[0];
    if(type>1){
        // Z-cull statistics follow RPCS3 RSXZCULL.cpp ZCULL_control::write
        // (derived, RPCS3 marks STATS..STATS2 "Not implemented"): the
        // value depends on the stats enable and on whether the current zeta
        // surface is a bound Z-cull region, sampled when the report is written.
        result=type==5?((result || !surface_active || !stats)?0u:0xffffu):((surface_active && stats)?0xffffffffu:0u);
    }
    m.store(address,8,timestamp);m.store(address+8,4,result);m.store(address+12,4,0);
    gpu.report_timestamp=timestamp;
}
// NV4097_SET_RENDER_ENABLE 0x1e98 (RPCS3 ffba2bf0 NV47/HW/nv4097.cpp:692-726, RSXZCULL.cpp:986-1036,
// RSXThread.cpp:2876-2900): mode arg>>24 = 1 renders unconditionally; 2 renders only while the 32-bit value
// (CellGcmReportData.value, +8) of the report at offset arg&0xffffff in the report DMA context is nonzero,
// evaluated when the method executes, after the referenced report producer completes. RPCS3 ignores other modes
// with an error log; their hardware meaning is unknown here, so they stop explicitly.
static void rsx_set_render_enable(PpuState& s,std::uint32_t value){
    auto& gpu=s.rsx;const auto mode=value>>24,offset=value&0xffffff;
    if(mode==1){
#ifdef ANYPS3_RSX_VK
        rsx_graphics_reset_conditional(s);
#endif
        gpu.render_disabled=false;return;}
    if(mode!=2)throw std::runtime_error("unsupported RSX render enable mode "+std::to_string(mode));
    if(offset&15)throw std::runtime_error("misaligned RSX conditional render report");
    const auto address=rsx_report_address(gpu,offset);
#ifdef ANYPS3_RSX_VK
    if(rsx_graphics_try_conditional(s,address+8))return;
    rsx_graphics_reset_conditional(s);
    rsx_graphics_sync_report(s,address+8);
#endif
    gpu.render_disabled=s.memory.load(address+8,4)==0;
}

struct RsxEventTicket {std::uint64_t name=0;bool pending=false,full=false;};
static RsxEventTicket rsx_event_ticket(PpuState& s,std::uint64_t flags,const char* error,bool vblank=false){
    RsxEventTicket ticket;
    rsx_worker_events(s,[&]{auto& gpu=s.rsx;
        auto port=std::find_if(s.events.ports.begin(),s.events.ports.end(),[&](const auto& p){return p.id==gpu.event_port;});
        auto queue=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return q.id==gpu.event_queue;});
        if(flags && (port==s.events.ports.end() || queue==s.events.queues.end() || port->queue!=queue->id))throw std::runtime_error(error);
        if(!flags)return;
        ticket.name=port->name;
        if(vblank)for(const auto& e:queue->data)if(e[0]==ticket.name && e[1]==0 && e[2]==flags && e[3]==0)ticket.pending=true;
        ticket.full=queue->data.size()>=queue->capacity;
        if(ticket.full && !vblank)throw std::runtime_error(error);
        if(!ticket.pending && !ticket.full)queue->data.reserve(queue->data.size()+1);
    });return ticket;
}
static void rsx_event_publish(PpuState& s,unsigned id,std::uint64_t name,std::uint64_t flags,const char* error,bool wake=true){
    if(!flags)return;
    anyps3_ft::mark(anyps3_ft::EventPub,id,name,flags);
    // A raw fault may block the coordinator in its signal frame. Publish
    // memory on the owner and queue only the immutable LV2 event there; the
    // next coordinator safe point delivers it or poisons with the same error.
    auto publish=[events=&s.events,reschedule=&s.reschedule,id,name,flags,error=std::string(error),wake]{
        auto queue=std::find_if(events->queues.begin(),events->queues.end(),[&](const auto& q){return q.id==id;});
        if(queue==events->queues.end() || queue->data.size()>=queue->capacity)throw std::runtime_error(error);
        queue->data.push_back({name,0,flags,0});if(wake)*reschedule=true;
    };
    if(s.process.rsx_worker.on_owner() && s.memory.local_state().fault_claim){s.process.rsx_worker.queue->owner_post(std::move(publish));return;}
    rsx_worker_events(s,publish);
}
static void rsx_queue_display(PpuState& s,unsigned head,unsigned buffer){
    auto& gpu=s.rsx;auto& m=s.memory;
    if(head>1 || buffer>=gpu.display_buffers.size() || !gpu.display_buffers[buffer].width)
        throw std::runtime_error("invalid RSX display queue head or buffer");
    const auto flags=(UINT64_C(0x20)<<head)&m.load(std::uint64_t(gpu.driver)+0x12c0,4);
    const auto event=rsx_event_ticket(s,flags,"RSX display queue event cannot be delivered");
    const auto address=std::uint64_t(gpu.driver)+0x10b8+head*0x40;
    m.range(address,0x18,true);
    m.store(address+0x14,4,buffer);m.store(address+8,4,m.load(address+8,4)|0x40000000|(1u<<buffer));
    rsx_event_publish(s,gpu.event_queue,event.name,flags,"RSX display queue event cannot be delivered",false);
    ++gpu.display_queue_generation;
    // Queue readiness is not flip completion. The firmware handles this event
    // and must separately request presentation of the queued display buffer.
    std::fprintf(stderr,"RSX display queued head=%u buffer=%u\n",head,buffer);
}
static void rsx_release_semaphore(PpuState& s,std::uint64_t address,std::uint32_t value,bool backend=false){
#ifdef ANYPS3_RSX_VK
    // Front-end and back-end release labels order all preceding draws. Queue the
    // publication behind that prefix; the guarded first reader finishes it.
    // The flip/device gates are front-end control (RPCS3 nv47_sync.hpp), and
    // must stay immediately visible to firmware.
    const bool flip_gate=address==std::uint64_t(s.rsx.reports)+0x10 || address==std::uint64_t(s.rsx.device)+0x30;
    if(!flip_gate){
        if(rsx_graphics_defer_label(s,address,value))return;
        rsx_graphics_flush(s,"semaphore");
    }
#endif
    (void)backend;
    auto& gpu=s.rsx;
    // The device gate acknowledges a processed display-queue command. It is
    // separate from the report-area flip-completion semaphore. Retain the
    // queue generation because firmware resets this gate after queuing.
    const bool acknowledge=address==std::uint64_t(gpu.device)+0x30 && value==0 &&
        gpu.display_queue_generation>gpu.display_queue_acknowledged;
    s.memory.store(address,4,acknowledge?1:value);
    if(acknowledge)gpu.display_queue_acknowledged=gpu.display_queue_generation;
}
static bool rsx_tick_vblank(PpuState& s,std::uint64_t now_ns){
    auto& gpu=s.rsx;auto& m=s.memory;
    if(!gpu.context || !gpu.driver || !gpu.fifo_enabled)return false;
    if(!gpu.vblank_epoch_ns){gpu.vblank_epoch_ns=now_ns;return false;}
    if(now_ns<gpu.vblank_epoch_ns)throw std::runtime_error("RSX display clock moved backwards");
    // Native virtual 720p output advertises 60000/1001 Hz. Derive deadlines
    // from the epoch rather than repeatedly adding a rounded frame duration.
    const auto elapsed=now_ns-gpu.vblank_epoch_ns;
    const auto ticks=(elapsed/1001000000)*60+(elapsed%1001000000)*60/1001000000;
    if(ticks==gpu.vblank_ticks)return false;
    const auto flags=m.load(std::uint64_t(gpu.driver)+0x12c0,4)&0x802;
    const auto event=rsx_event_ticket(s,flags,"RSX vblank event port is disconnected",true);
    if(flags && !event.pending && event.full)return false;
    const auto head=std::uint64_t(gpu.driver)+0x10f8;
    m.range(head,0x40,true);
    const auto tick_ns=gpu.vblank_epoch_ns+(ticks/60)*1001000000+(ticks%60)*1001000000/60;
    const auto timebase=(tick_ns/1000000000)*79800000+(tick_ns%1000000000)*79800000/1000000000;
    m.store(head+0x1c,4,timebase);m.store(head+0x3c,4,timebase>>32);
    m.store(head+0x20,8,tick_ns/1000);m.store(head+0x30,8,ticks);
    gpu.vblank_ticks=ticks;
    anyps3_ft::mark(anyps3_ft::Vblank,ticks,now_ns-tick_ns,flags);
    if(flags && !event.pending)rsx_event_publish(s,gpu.event_queue,event.name,flags,"RSX vblank event port is disconnected");
    return true;
}
// Wait for the emulated vblank deadline `until` (the flip blocks the owner, which keeps the display ordering of later commands).
// On the RSX owner the wait is not idle time: everything recorded before the flip command is independent of the display, so it is
// submitted at once (the GPU finishes it, and the report/label writes that precede the flip become visible, before the vblank
// instead of after it), completed GPU prefixes are published while waiting, and page-synchronisation requests of other threads
// are served (rsx_owner_poll) instead of waiting for the end of the vblank.
static void rsx_wait_vblank(PpuState& s,std::chrono::steady_clock::time_point until){
#ifdef ANYPS3_RSX_VK
    // A flip that is already late has no wait to hide the GPU time in: its own submission follows at once.
    if(rsx_flip_early_enabled() && s.process.rsx_worker.on_owner() && std::chrono::steady_clock::now()<until)rsx_graphics_flip_prepare(s);
#endif
    // SPUs keep running while the PPU waits for vblank; sleep only when idle.
    while(std::chrono::steady_clock::now()<until){
        if(s.process.rsx_worker.on_owner()){
            auto& q=*s.process.rsx_worker.queue;
            std::unique_lock<std::mutex> lock(q.mutex);const auto seen=q.generation;lock.unlock();   // before serving: a later post wakes the wait
            bool pending=false;
#ifdef ANYPS3_RSX_VK
            if(rsx_flip_early_enabled())pending=rsx_graphics_poll(s);
#endif
            rsx_owner_poll();
            const auto step=std::chrono::steady_clock::now()+std::chrono::microseconds(pending?100:500);
            lock.lock();
            q.changed.wait_until(lock,std::min(until,step),[&]{return q.closing || q.generation!=seen;});
        }else if(!spu_run(s,65536))std::this_thread::sleep_until(std::min(until,std::chrono::steady_clock::now()+std::chrono::microseconds(500)));
    }
}
static void rsx_flip_display(PpuState& s,unsigned head,std::uint32_t value){
    auto& gpu=s.rsx;auto& m=s.memory;
    if(head>1)throw std::runtime_error("invalid RSX flip head");
    const auto address=std::uint64_t(gpu.driver)+0x10b8+head*0x40;
    unsigned id=8;
    if(value&0x80000000){
        if((value&0x7fffff00)!=0x100 || (value&0xf0))throw std::runtime_error("unsupported RSX flip channel or flags");
        id=value&15;if(id==15)id=m.load(address+0x14,4);
        if(id>=8 || !(m.load(address+8,4)&(1u<<id)))throw std::runtime_error("RSX flip buffer was not queued");
    }else for(unsigned i=0;i<gpu.display_buffers.size();++i)
        if(gpu.display_buffers[i].width && gpu.display_buffers[i].offset==value){id=i;break;}
    if(id>=8)throw std::runtime_error("RSX flip offset has no configured display buffer");
    const auto flags=(UINT64_C(8)<<head)&m.load(std::uint64_t(gpu.driver)+0x12c0,4);
    const auto event=rsx_event_ticket(s,flags,"RSX flip event cannot be delivered");
    m.range(address,0x18,true);m.range(std::uint64_t(gpu.reports)+0x10,16,true);
    anyps3_ft::mark(anyps3_ft::FlipReq,id,gpu.flip_modes[head],gpu.vblank_ticks);
    {   // Diagnostic (ANYPS3_FRAMETIME_FLIPDUMP=N): the FIFO words around the first N flips, to see what the game queues next to a flip.
        static const unsigned dump=[]{const char* v=std::getenv("ANYPS3_FRAMETIME_FLIPDUMP");return v?unsigned(std::strtoul(v,nullptr,10)):0u;}();
        static unsigned dumped=0;
        if(dump && dumped<dump && gpu.flips>=100){++dumped;
            const auto get=static_cast<std::uint32_t>(m.load(gpu.dma+0x44,4)),put=static_cast<std::uint32_t>(m.load(gpu.dma+0x40,4));
            std::fprintf(stderr,"FLIPDUMP flip=%llu get=%08x put=%08x:",(unsigned long long)gpu.flips,get,put);
            for(int i=-24;i<48;++i){const auto at=std::int64_t(get)+4*i;if(at<0)continue;
                try{std::fprintf(stderr," %s%08x",i==0?"|":"",unsigned(m.load(rsx_address(gpu,std::uint32_t(at)),4)));}catch(...){std::fprintf(stderr," ????????");}}
            std::fprintf(stderr,"\n");}
    }
    if(gpu.flip_modes[head]==2 && gpu.vblank_epoch_ns){
        const auto next=gpu.vblank_ticks+1;
        const auto deadline=gpu.vblank_epoch_ns+(next/60)*1001000000+(next%60)*1001000000/60;
        anyps3_ft::Span ft_vsync(anyps3_ft::FlipVsync,deadline,next);
        rsx_wait_vblank(s,std::chrono::steady_clock::time_point(std::chrono::nanoseconds(deadline)));
    }
#ifdef ANYPS3_RSX_VK
    // Unsaved frames may remain in flight. Guest completion fields and the
    // event are published only when that frame's timeline value completes.
    if(!gpu.perf_capture.replay && rsx_graphics_flip_async(s,id,address,std::uint64_t(gpu.reports)+0x10,gpu.event_queue,flags?event.name:0)){
        anyps3_ft::mark(anyps3_ft::FlipAsync,1,0,id);
        rsx_capture_flip(s);return;
    }
    anyps3_ft::mark(anyps3_ft::FlipAsync,0,0,id);
#endif
    // Saved captures keep their durable-to-stdio completion contract.
    if(gpu.perf_capture.replay){
#ifdef ANYPS3_RSX_VK
        rsx_graphics_flush(s,"replay_flip");
#endif
    }else rsx_capture_display(s,id);
    m.store(address,8,timebase_ticks());m.store(address+0x10,4,id);
    m.store(address+8,4,m.load(address+8,4)|0x80000000);
    m.store(std::uint64_t(gpu.reports)+0x10,8,0);m.store(std::uint64_t(gpu.reports)+0x18,8,0);
    rsx_event_publish(s,gpu.event_queue,event.name,flags,"RSX flip event cannot be delivered");
    rsx_capture_flip(s);
}
''' + DRAW_CPP + r'''
#ifdef ANYPS3_RSX_VK
static void rsx_graphics_flip_prepare(PpuState& s){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(!session || !session->observer_installed || session->previous_observer || session->failed)return;
    session->submit_async("flip_early");
    if(!s.rsx.graphics_session)session->leave_fifo();
}
// Publish completed GPU prefixes; returns whether work is still in flight (the caller then polls again soon).
static bool rsx_graphics_poll(PpuState& s){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(!session || session->failed || session->flights.empty())return false;
    session->retire(false);
    if(!s.rsx.graphics_session)session->leave_fifo();
    return !session->flights.empty();
}
static bool rsx_graphics_flip_async(PpuState& s,unsigned id,std::uint64_t address,std::uint64_t reports,unsigned queue,std::uint64_t event_name){
    anyps3_ft::Span ft_total(anyps3_ft::Mark,0xa0);
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(!session || !session->observer_installed || session->previous_observer || id>=s.rsx.display_buffers.size() || s.video.format!=0)return false;
    const char* directory=std::getenv("ANYPS3_FRAME_DIR");const bool files=directory && *directory;
    if(!files && !s.present.enabled)return false;
    if(files){const auto root=std::filesystem::canonical(directory);if(!std::filesystem::is_directory(root))throw std::runtime_error("RSX frame output is not a directory");
        const auto next=s.rsx.flips+1;const auto stride=rsx_frame_stride();if(next<=8 || next%stride==0 || rsx_frame_trigger(false))return false;}
    const auto& buffer=s.rsx.display_buffers[id];if(!buffer.width || !buffer.height)return false;
    const auto target=rsxdraw::bind_target(s,0xfeed0000,buffer.offset,buffer.pitch,buffer.width,buffer.height);
    std::shared_ptr<NativeVulkan::GraphicsImage> image;
    for(const auto& surface:session->surfaces)if(!surface.partial && surface.same(target,buffer.width,buffer.height,1,8,false)){image=surface.image;break;}
    if(!image || image->format==VK_FORMAT_R32_UINT)return false;
    const unsigned head=unsigned((address-(std::uint64_t(s.rsx.driver)+0x10b8))/0x40);
    const auto flags=(UINT64_C(8)<<head)&s.memory.load(std::uint64_t(s.rsx.driver)+0x12c0,4);
    if(!session->defer_flip(address,reports,id,queue,event_name,flags))return false;
    try{
        ++session->batch->frames;
        if(s.present.enabled){
            if(image->format==VK_FORMAT_R8G8B8A8_SRGB){auto raw=session->vk.graphics_image(*session->batch,VK_FORMAT_R8G8B8A8_UNORM,buffer.width,buffer.height,1);session->vk.graphics_copy(*session->batch,image,raw);image=std::move(raw);}
            anyps3_ft::Span ft_present(anyps3_ft::Mark,0xa2);
            session->vk.graphics_present(*session->batch,image,buffer.width,buffer.height,s.rsx.vsync,true);++s.present.presented_frames;
        }
        {anyps3_ft::Span ft_submit(anyps3_ft::Mark,0xa3);session->submit_async("flip");}++s.rsx.flips;
    }catch(...){session->failed=true;throw;}
    if(!s.rsx.graphics_session)session->leave_fifo();
    return true;
}
[[maybe_unused]] static bool rsx_graphics_scanout(PpuState& s,unsigned id,std::vector<std::uint8_t>* capture,bool present){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(!session || id>=s.rsx.display_buffers.size() || s.video.format!=0)return false;
    if(!s.rsx.graphics_session)session->enter_fifo();
    const auto& b=s.rsx.display_buffers[id];if(!b.width || !b.height)return false;
    const auto target=rsxdraw::bind_target(s,0xfeed0000,b.offset,b.pitch,b.width,b.height);
    if(target.tile && target.tile->compression!=0 && target.tile->compression!=7)
        throw std::runtime_error("unsupported RSX scanout tile layout");
    std::shared_ptr<NativeVulkan::GraphicsImage> image;
    for(const auto& surface:session->surfaces)if(!surface.partial && surface.same(target,b.width,b.height,1,8,false)){image=surface.image;break;}
    if(!image)return false;
    if(image->format==VK_FORMAT_R32_UINT)return false; // packed ARGB requires the coherent scanout codec
    // Complete/validate shaders and visibility, without guest readback.
    if(!present && !capture){++session->batch->frames;session->submit_async("flip");
        if(!s.rsx.graphics_session)session->leave_fifo();
        return true;}
    ++session->batch->frames;
    session->flush(false,nullptr,0,"scanout");
    if(present){
        // Scanout presents stored sRGB bytes, without a second decode. A
        // compatible-format image copy preserves those bytes exactly.
        if(image->format==VK_FORMAT_R8G8B8A8_SRGB){auto raw=session->vk.graphics_image(*session->batch,VK_FORMAT_R8G8B8A8_UNORM,b.width,b.height,1);
            session->vk.graphics_copy(*session->batch,image,raw);image=std::move(raw);}
        session->vk.graphics_present(*session->batch,image,b.width,b.height,s.rsx.vsync);
        session->flush(false,nullptr,0,"scanout");++s.present.presented_frames;
    }
    if(capture){
        const auto at=session->vk.graphics_transfer(*session->batch,image,VK_IMAGE_ASPECT_COLOR_BIT,4);
        session->vk.graphics_finish(*session->batch);capture->resize(std::size_t(b.width)*b.height*4);
        std::memcpy(capture->data(),static_cast<const char*>(session->batch->map)+at,capture->size());
        for(std::size_t i=3;i<capture->size();i+=4)(*capture)[i]=255;
        session->flush(false,nullptr,0,"scanout");
    }
    if(!s.rsx.graphics_session)session->leave_fifo();
    return true;
}
#endif
''' + BLIT_CPP + r'''
#ifdef ANYPS3_RSX_VK
static rsxgraphics::Session* rsx_conditional_session(PpuState& s){return static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());}
static bool rsx_graphics_try_conditional(PpuState& s,std::uint64_t address){auto* session=rsx_conditional_session(s);return session && session->try_conditional(address);}
static void rsx_graphics_reset_conditional(PpuState& s){if(auto* session=rsx_conditional_session(s))session->conditional.reset();}
static bool rsx_graphics_resolve_conditional(PpuState& s){auto* session=rsx_conditional_session(s);return session && session->conditional && session->resolve_conditional();}
static bool rsx_graphics_conditional_fault(PpuState& s){auto* session=rsx_conditional_session(s);return session && !session->failed && session->conditional && session->resolve_conditional();}
// A conditional test consumes ONE report value. Retire its producer prefix
// even when normal report polling is nonblocking. Unrelated surfaces and later
// work stay resident; a physical alias of a report is still a dependency.
static void rsx_graphics_sync_report(PpuState& s,std::uint64_t address){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(!session)return;
    GuestMemory::ObserverPause pause(s.memory);const auto range=s.memory.range(address,4,false);
    auto overlaps=[&](const auto& label){const auto at=reinterpret_cast<std::uintptr_t>(label.identity());
        for(const auto& part:range.parts){const auto p=reinterpret_cast<std::uintptr_t>(part.data);
            if(p<at+label.size && at<p+part.size)return true;}return false;};
    bool active=false;for(const auto& label:session->labels)if(overlaps(label)){active=true;break;}
    if(active)session->submit_async("conditional_report");
    std::uint64_t through=0;for(const auto& flight:session->flights)
        for(const auto& label:flight.labels)if(overlaps(label))through=flight.batch->completion;
    if(through)session->retire(true,through);
    // A report may also alias an ordinary render target. Commit only matching
    // physical spans before the BE load, preserving that existing coherence.
    for(const auto& part:range.parts){bool surface=false;
        for(const auto& target:session->surfaces)if(target.overlaps(part.data,part.size)){surface=true;break;}
        if(surface)session->flush(true,part.data,part.size,"conditional_surface");}
}
static void rsx_capture_sync(PpuState& s){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(session){if(!s.rsx.graphics_session)session->enter_fifo();session->flush(true,nullptr,0,"perf_capture");if(session->conditional)session->resolve_conditional();}
}
static bool rsx_graphics_defer_report(PpuState& s,std::uint64_t address,unsigned type,bool active,bool stats){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    return session && session->defer_report(address,type,active,stats);
}
static bool rsx_graphics_defer_label(PpuState& s,std::uint64_t address,unsigned value){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    return session && session->defer_label(address,value);
}
static void rsx_graphics_flush(PpuState& s,const char* reason){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    if(session){
        if(std::strcmp(reason,"replay_flip")==0){++session->batch->frames;session->submit_async("flip");}
        else if(std::strcmp(reason,"zcull_sync")==0)session->submit_async(reason);
        else session->flush(false,nullptr,0,reason);
        if(!s.rsx.graphics_session)session->leave_fifo();}
}
#endif

#ifdef ANYPS3_RSX_VK
static void rsx_graphics_reset_report(PpuState& s,unsigned value){
    auto* session=static_cast<rsxgraphics::Session*>(s.rsx.graphics_session?s.rsx.graphics_session:s.rsx.graphics_owner.get());
    // Pending visibility still contributes to lifetime pixel statistics, but
    // pre-reset draws must never reappear in the newly cleared report counter.
    if(session && value==1){for(auto& p:session->pending)p.report=false;
        for(auto& flight:session->flights)for(auto& p:flight.pending)p.report=false;}
    if(rsxgraphics::fallback_stats().enabled)++rsxgraphics::fallback_stats().paths["report_reset_deferred"];
}
#endif

static bool rsx_state_register(std::uint32_t reg){
    if(reg&3)return false;
    // State storage only. Rendering must interpret these values before use;
    // draw, shader upload, clear, report and transfer commands are excluded.
    if((reg>=0x180 && reg<=0x1b8) || // DMA contexts
       (reg>=0x200 && reg<=0x230) || // Surface, including raw reset fields 224/228/230
       (reg>=0x240 && reg<=0x24c) || // Raw reset state; interpretation deferred to renderer
       (reg>=0x280 && reg<=0x28c) || // Additional color surfaces
       (reg>=0x2b8 && reg<=0x388) || // Window clipping, blend, stencil, depth bounds
       (reg>=0x3c0 && reg<=0x3fc) || // Anisotropic spread
       (reg>=0x400 && reg<=0x47c) || // Raw sampling configuration retained verbatim
       (reg>=0x8cc && reg<=0x8d8) || // Fog
       (reg>=0x900 && reg<=0x97c) || // Four vertex textures
       (reg>=0x1480 && reg<=0x14fc) || // Polygon stipple pattern rows (rsx_gaps.py)
       (reg>=0xa20 && reg<=0xa3c) || // Viewport offset/scale
       (reg>=0xa60 && reg<=0xa7c) || // Polygon offset/depth
       (reg>=0xb00 && reg<=0xb64) || // Texture control2/texcoord control
       (reg>=0x1680 && reg<=0x16bc) || // Vertex array offsets
       (reg>=0x1738 && reg<=0x177c) || // Vertex base/array formats
       (reg>=0x181c && reg<=0x1820) || // Index array
       (reg>=0x1828 && reg<=0x187c) || // Polygon/cull, texture control3
       (reg>=0x1a00 && reg<=0x1bfc) || // Sixteen textures
       (reg>=0x1d00 && reg<=0x1d3c) || // Color keys
       (reg>=0x1d78 && reg<=0x1d90) || // Shader window, clear values (not clear action)
       (reg>=0x1dac && reg<=0x1db8) || // Restart index/line stipple
       (reg>=0x1e9c && reg<=0x1eac) || // Transform program address/Z-cull configuration
       (reg>=0x1fc0 && reg<=0x1fd4) || // Attribute mapping
       (reg>=0x1fec && reg<=0x1ff8) ||
       (reg>=0x2180 && reg<=0x2188) || (reg>=0x230c && reg<=0x2324) || // Memory copy state (not launch)
       (reg>=0x6180 && reg<=0x6188) || (reg>=0x6300 && reg<=0x630c) || // Linear 2D surfaces
       (reg>=0x8180 && reg<=0x8184) || (reg>=0x8300 && reg<=0x8304) || // Swizzled surface
       (reg>=0xa180 && reg<=0xa19c) || (reg>=0xa2f8 && reg<=0xa30c) || // CPU image state (not pixels)
       (reg>=0xc180 && reg<=0xc198) || (reg>=0xc2fc && reg<=0xc31c) ||
       (reg>=0xc400 && reg<=0xc408))return true; // Scaled image state (not launch)
    switch(reg){
    case 0:case 0x2000:case 0x6000:case 0x8000:case 0xa000:case 0xc000:case 0xe000:
    case 0x60:case 0x64:case 0x238:case 0x23c:
    case 0x394:case 0x398:case 0x3b0:case 0x3b8:case 0x3bc:
    case 0x8c0:case 0x8c4:case 0x8e4:case 0xa00:case 0xa04:case 0xa0c:
    case 0x1428:case 0x142c:case 0x1450:case 0x1454:case 0x145c:case 0x1478:case 0x147c:
    case 0x17cc:case 0x1804:case 0x1d60:case 0x1d64:case 0x1d6c:
    case 0x1710:case 0x1714:case 0x1718:
    case 0x1d98:case 0x1d9c:case 0x1da4:case 0x1e94:case 0x1e98:
    case 0x1ee0:case 0x1ee4:case 0x1ee8:case 0x1ef8:case 0x1efc:case 0x1fe0:case 0x1fe8:return true;
    default:return false;
    }
}

// FIFO method classes, built once from the exact supported-method predicate:
// 0 unsupported (explicit stop path), 1 register/state upload without any
// guest-visible side effect, 2 action (draw, clear, flush, report, semaphore,
// flip, transfer, render-enable, REF). GET publication may lag only behind
// class-1 methods; it is stored before every action and at every return.
static bool rsx_method_supported(std::uint32_t reg){
    return !(!rsx_state_register(reg) && !(reg>=0x1f00 && reg<=0x1f7c) && !(reg>=0xb80 && reg<=0xbfc) && !(reg>=0x1900 && reg<0x1a00) && !(reg>=0xa80 && reg<0xb00) && !(reg>=0x1e40 && reg<0x1e80) && !(reg>=0x1880 && reg<0x1900) && !(reg>=0x1500 && reg<0x1600) && !(reg>=0x1c00 && reg<0x1d00) && reg!=0x1808 && reg!=0x1814 && reg!=0x180c && reg!=0x1810 && reg!=0x1824 && reg!=0x50 && reg!=0x68 && reg!=0x6c && reg!=0x100 && reg!=0x110 && reg!=0x1438 && reg!=0x17c8 && reg!=0x1800 && reg!=0x1d94 && reg!=0x1d70 && reg!=0x1d74 && reg!=0xc40c && reg!=0x2328 && reg!=0x234 && reg!=0xa1c && reg!=0x1fd8 && !(reg>=0xa400 && reg<0xc000) && reg!=0xe940 && reg!=0xe944 && reg!=0xe920 && reg!=0xe924 && reg!=0x1818);
}
static bool rsx_method_action(std::uint32_t reg){
    switch(reg){case 0x1d94:case 0xa1c:case 0x1fd8:case 0x1808:case 0xe920:case 0xe924:case 0xe940:case 0xe944:case 0x1800:
    case 0x1e98:case 0x17c8:case 0x2328:case 0xc40c:case 0x68:case 0x6c:case 0x1d70:case 0x1d74:case 0x50:return true;
    default:return reg>=0xa400 && reg<0xc000;}
}
static unsigned rsx_method_class(std::uint32_t reg){
    static const auto table=[]{std::array<std::uint8_t,0x4000> t{};for(std::uint32_t r=0;r<0x10000;r+=4)t[r/4]=!rsx_method_supported(r)?0:rsx_method_action(r)?2:1;return t;}();
    return reg<0x10000 && !(reg&3)?table[reg/4]:(rsx_method_supported(reg)?(rsx_method_action(reg)?2u:1u):0u);
}
// Registers whose method only stores its value (class 1 and none of the special cases of the execution chain in rsx_run_impl: channel/copy
// objects, clear, zcull, vertex data and inline-array registers, begin/end/draw, program and constant uploads, flips, reports, blits,
// semaphores, ...). The consumer takes a table lookup for them instead of walking the chain. The list below must name every register that
// the chain treats individually; tests/test_rsx_fifo_plain.py compares both paths for every register.
static bool rsx_plain_register(std::uint32_t reg){
    switch(reg){case 0:case 0x50:case 0x68:case 0x6c:case 0x100:case 0x110:case 0x234:case 0x2000:case 0x6000:case 0x8000:case 0xa000:case 0xc000:case 0xe000:
    case 0x1d94:case 0xa1c:case 0x1fd8:case 0x1710:case 0x1714:case 0x1800:case 0x1808:case 0x180c:case 0x1810:case 0x1814:case 0x1818:case 0x1824:
    case 0x1e98:case 0x17c8:case 0x1438:case 0x2328:case 0xc40c:case 0x1d70:case 0x1d74:case 0xe920:case 0xe924:case 0xe940:case 0xe944:return false;
    default:break;}
    if((reg>=0xb80 && reg<=0xbfc) || (reg>=0x1f00 && reg<=0x1f7c) || (reg>=0xa400 && reg<0xc000))return false;
    if((reg>=0xa80 && reg<0xb00) || (reg>=0x1900 && reg<0x1a00) || (reg>=0x1e40 && reg<0x1e80) || (reg>=0x1880 && reg<0x1900) || (reg>=0x1500 && reg<0x1600) || (reg>=0x1c00 && reg<0x1d00))return false;
    return true;
}
static bool rsx_fifo_plain_fast=true;   // TEST HOOK: false walks the full chain for every method
static const std::array<std::uint8_t,0x4000>& rsx_plain_table(){
    static const auto table=[]{std::array<std::uint8_t,0x4000> t{};for(std::uint32_t r=0;r<0x10000;r+=4)t[r/4]=rsx_method_class(r)==1 && rsx_plain_register(r);return t;}();
    return table;
}
// One admitted read per 1 MiB IO page for longer packets; any failure
// repeats the original per-word loads, which raise the original error.
static void rsx_fifo_values(NativeRsx& gpu,GuestMemory& m,std::uint32_t at,unsigned count,std::uint32_t* out){
    if(count>=4){
        try{std::uint8_t bytes[0x2000];unsigned done=0;
            while(done<count){
                const unsigned n=std::min<unsigned>(count-done,(0x100000-((at+done*4)&0xfffff))/4);
                m.transfer(rsx_address(gpu,at+done*4),bytes+done*4,n*4,false);done+=n;
            }
            for(unsigned i=0;i<count;++i){const auto* b=bytes+4*i;out[i]=(std::uint32_t(b[0])<<24)|(std::uint32_t(b[1])<<16)|(std::uint32_t(b[2])<<8)|b[3];}
            return;
        }catch(const std::exception&){}
    }
    for(unsigned i=0;i<count;++i)out[i]=static_cast<std::uint32_t>(m.load(rsx_address(gpu,at+i*4),4));
}
static bool rsx_run_impl(PpuState& s,unsigned budget,bool idle_poll=false){
    auto& gpu=s.rsx;auto& m=s.memory;if(!gpu.fifo_enabled)return false;
    // Wall-clock pacing need not perform a vDSO clock read after every 256
    // PPU instructions. Flip waits still use the exact vblank deadline.
    bool progress=false;
    const bool clock_due=(gpu.vblank_poll_ticks++&31u)==0;
    if(!idle_poll || clock_due){const auto now=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());progress=rsx_tick_vblank(s,now);}
    // An idle precheck is not a stable FIFO snapshot: the producer can publish
    // PUT immediately afterwards. Never execute those commands outside the
    // graphics scope selected by rsx_run(); the next call will bind it first.
    if(idle_poll)return progress;
    // GET belongs to this consumer: keep a local cursor and refresh PUT only
    // when the cached value is reached. GET publication may lag behind pure
    // state methods only (rsx_method_class 1); it is stored before every
    // action method, before every return and on every exception.
    rsxdraw::HostPhase fifo_phase(rsxdraw::HostStage::fifo);   // HOST_PROFILE: FIFO decode (draws switch to their own stages and return here)
    std::uint32_t put=0,get=0;bool cursor=false,pending=false;
    auto publish=[&]{if(pending){m.store(gpu.dma+0x44,4,get);pending=false;}};
    try{
    for(unsigned step=0;step<budget;++step){
        rsx_owner_poll();
        if(!cursor){put=static_cast<std::uint32_t>(m.load(gpu.dma+0x40,4));get=static_cast<std::uint32_t>(m.load(gpu.dma+0x44,4));cursor=true;}
        else if(get==put)put=static_cast<std::uint32_t>(m.load(gpu.dma+0x40,4));
        if((get|put)&3)throw std::runtime_error("unaligned RSX FIFO position");
        if(get==put){publish();return progress;}
        const auto header=static_cast<std::uint32_t>(m.load(rsx_address(gpu,get),4));
        std::uint32_t next=get+4;
        if((header&0xe0000003)==0x20000000 || (header&0xe0000003)==1){
            next=header&((header&1)?0xfffffffc:0x1ffffffc);
            if(next==get){publish();return progress;}
            rsx_address(gpu,next);
        }else if((header&3)==2){
            if(gpu.return_offset!=UINT32_MAX)throw std::runtime_error("nested RSX FIFO call");
            next=header&0x1ffffffc;rsx_address(gpu,next);gpu.return_offset=get+4;
        }else if((header&0xffff0003)==0x20000){
            if(gpu.return_offset==UINT32_MAX)throw std::runtime_error("RSX FIFO return without call");
            next=gpu.return_offset;gpu.return_offset=UINT32_MAX;
        }else{
            if(header&0xa0030003)throw std::runtime_error("invalid RSX FIFO header "+std::to_string(header));
            const auto count=(header>>18)&0x7ff,method=header&0xfffc,increment=(header&0x40000000)?0u:4u;
            if(count){
                if(get<put && count*4+4>put-get){
                    put=static_cast<std::uint32_t>(m.load(gpu.dma+0x40,4));
                    if(put&3)throw std::runtime_error("unaligned RSX FIFO position");
                    if(get<put && count*4+4>put-get){publish();return progress;}
                }
                std::uint32_t values[0x800];
                for(unsigned i=0;i<count;++i){
                    const auto reg=method+i*increment;
                    if(!rsx_method_class(reg)){
                        if(!rsx_diag_skip()){
                        std::fprintf(stderr,"RSX unsupported method=%04x io=%08x header=%08x count=%u value=%08" PRIx64 "\n",reg,get,header,count,m.load(rsx_address(gpu,get+4+i*4),4));
                        // Diagnostic snapshot of retained state for the next implementation step.
                        for(const auto& [r,v]:std::map<std::uint32_t,std::uint32_t>(std::as_const(gpu).registers.begin(),std::as_const(gpu).registers.end()))
                            std::fprintf(stderr,"RSX state %04x=%08x\n",r,v);
                        for(unsigned w=0;w<gpu.transform_program.size();w+=4)if(gpu.transform_program_written[w])
                            std::fprintf(stderr,"RSX vp[%u]=%08x %08x %08x %08x\n",w/4,gpu.transform_program[w],gpu.transform_program[w+1],gpu.transform_program[w+2],gpu.transform_program[w+3]);
                        if(gpu.registers.count(0x8e4) && (rsx_reg(gpu,0x8e4)&3)==1){
                            const auto fp=UINT64_C(0xc0000000)+(rsx_reg(gpu,0x8e4)&~3u);
                            for(unsigned w=0;w<64;w+=4)std::fprintf(stderr,"RSX fp[%u]=%08" PRIx64 " %08" PRIx64 " %08" PRIx64 " %08" PRIx64 "\n",w/4,m.load(fp+w*4,4),m.load(fp+w*4+4,4),m.load(fp+w*4+8,4),m.load(fp+w*4+12,4));
                        }
                        for(unsigned w=0;w<gpu.transform_constants.size();w+=4)if(gpu.transform_constants_written[w])
                            std::fprintf(stderr,"RSX vc[%u]=%08x %08x %08x %08x\n",w/4,gpu.transform_constants[w],gpu.transform_constants[w+1],gpu.transform_constants[w+2],gpu.transform_constants[w+3]);
                        throw std::runtime_error("unimplemented native RSX method "+std::to_string(reg)+" after "+std::to_string(gpu.commands)+" FIFO packets");}
                        ++gpu.diag_skipped;std::fprintf(stderr,"RSX DIAG SKIP method=%04x stored as raw state\n",unsigned(reg));
                    }
                }
                rsx_fifo_values(gpu,m,get+4,count,values);
                const auto& plain=rsx_plain_table();
                for(unsigned i=gpu.packet_index;i<count;++i){
                    const auto reg=method+i*increment,value=values[i];
                    if(rsx_fifo_plain_fast && reg<0x10000 && !(reg&3) && plain[reg>>2]){rsx_capture_method(s,reg,value);gpu.registers.write(reg,value);continue;}
                    if(rsx_method_class(reg)==2)publish();
                    if(reg!=0x68)rsx_capture_method(s,reg,value);
                    if(reg==0 && value!=0x31337000)throw std::runtime_error("unsupported RSX channel object");
                    if(reg==0xe000 && value!=0xcafebabe)throw std::runtime_error("unsupported GCM driver object");
                    if((reg==0x2000 && value!=0x31337303) || (reg==0x6000 && value!=0x313371c3) ||
                       (reg==0x8000 && value!=0x31337a73) || (reg==0xa000 && value!=0x31337808) ||
                       (reg==0xc000 && value!=0x3137af00))throw std::runtime_error("unsupported RSX copy object");
                    if(reg==0x1d94)rsx_diag(s,"clear",reg,[&]{rsx_clear_surface(s,value);});
                    if(reg==0x234){++gpu.zcull_generation;continue;}
                    if(reg==0xa1c || reg==0x1fd8){
#ifdef ANYPS3_RSX_VK
                        if(reg==0xa1c)rsx_graphics_flush(s,"zcull_sync");
#endif
                        // INVALIDATE_L2 marks shader contents dirty (RPCS3
                        // nv4097::set_shader_program_dirty). FP bytes are read
                        // afresh each draw; GPU image barriers order dependencies.
                        // No CPU-visible completion is published by this command.
                        std::atomic_thread_fence(std::memory_order_seq_cst);continue;
                    }
                    if(reg==0x1710 || reg==0x1714){
                        // No decoded vertices are cached yet. Retain invalidation
                        // generation so a future cache cannot reuse old contents.
                        ++gpu.vertex_cache_generation;
                    }
                    if(rsx_vertex_data_register(gpu,reg,value))continue;
                    if(reg==0x1808){
                        if(value){
                            if(gpu.draw.primitive)throw std::runtime_error("nested RSX begin");
                            gpu.draw.primitive=value;gpu.draw.ranges.clear();gpu.draw.indexed=false;gpu.draw.immediate_vertices.clear();
                        }else{
                            if(!gpu.draw.primitive)throw std::runtime_error("RSX end without begin");
                            if(!gpu.draw.ranges.empty() || !gpu.draw.immediate_vertices.empty() || !gpu.draw.elements.empty())rsx_diag(s,"draw",reg,[&]{try{rsx_execute_draw(s);}catch(...){if(rsx_diag_skip()){gpu.draw.ranges.clear();gpu.draw.immediate_vertices.clear();gpu.draw.elements.clear();}throw;}});
                            gpu.draw.primitive=0;
                        }
                        continue;
                    }
                    if(reg==0x180c || reg==0x1810){
                        if(!gpu.draw.primitive || !gpu.draw.ranges.empty() || !gpu.draw.immediate_vertices.empty())
                            throw std::runtime_error("RSX inline index outside begin/end or mixed with another vertex source");
                        gpu.draw.indexed=true;
                        if(reg==0x180c){gpu.draw.elements.push_back({value&0xffff,2});gpu.draw.elements.push_back({value>>16,2});}
                        else gpu.draw.elements.push_back({value,4});
                        continue;
                    }
                    if(reg==0x1814 || reg==0x1824){
                        if(!gpu.draw.elements.empty())throw std::runtime_error("mixed RSX inline indices and array draws");
                        if(!gpu.draw.primitive)throw std::runtime_error("RSX draw command outside begin/end");
                        const bool indexed=reg==0x1824;
                        if(!gpu.draw.ranges.empty() && gpu.draw.indexed!=indexed)throw std::runtime_error("mixed RSX indexed and array draws");
                        gpu.draw.indexed=indexed;gpu.draw.ranges.push_back({value&0xffffff,(value>>24)+1});
                        continue;
                    }
                    if(reg>=0xb80 && reg<=0xbfc){
                        // Words follow the load position; it advances one
                        // instruction per four words after the batch.
                        if(!increment)throw std::runtime_error("non-incrementing RSX transform program upload");
                        const std::uint64_t word=std::uint64_t(rsx_reg(gpu,0x1e9c))*4+((method-0xb80)/4)%4+i;
                        if(word>=gpu.transform_program.size())throw std::runtime_error("RSX transform program upload outside 544 instructions");
                        gpu.transform_program.write(word,value);gpu.transform_program_written.write(word,true);
                        if(i+1==count){gpu.registers.write(0x1e9c,rsx_reg(gpu,0x1e9c)+(count+((method-0xb80)/4)%4)/4);++gpu.transform_program_generation;}
                        continue;
                    }
                    if(reg>=0x1f00 && reg<=0x1f7c){
                        const auto load=rsx_reg(gpu,0x1efc);
                        const std::uint64_t word=std::uint64_t(load)*4+(reg-0x1f00)/4;
                        if(word>=gpu.transform_constants.size())throw std::runtime_error("RSX transform constant index outside bank");
                        gpu.transform_constants.write(word,value);gpu.transform_constants_written.write(word,true);
                    }
                    if(reg==0xe920 || reg==0xe924){
                        rsx_flip_display(s,(reg-0xe920)/4,value);
                    }else if(reg==0xe940 || reg==0xe944){
                        rsx_queue_display(s,(reg-0xe940)/4,value);
                    }else if(reg==0x1800){
                        rsx_diag(s,"report",reg,[&]{rsx_get_report(s,value);});
                    }else if(reg==0x1e98){
                        rsx_set_render_enable(s,value);
                    }else if(reg==0x17c8){
                        #ifdef ANYPS3_RSX_VK
                        if(value==1 || value==2)rsx_graphics_reset_report(s,value);
#endif
                        if(value==1)gpu.report_values[0]=0;
                        else if(value==2){std::fill(gpu.report_values.begin()+1,gpu.report_values.end(),0);}
                        else throw std::runtime_error("unsupported RSX report reset type");
                    }else if(reg==0x1438){
                        if(value&~3u)throw std::runtime_error("unsupported RSX Z-cull clear mask");
                        // Invalidate acceleration metadata, not the depth surface.
                        // No cached visibility results or pending draws exist yet.
                        if(value)++gpu.zcull_generation;
                    }else if(reg==0x2328){
                        rsx_diag(s,"copy",reg,[&]{rsxblit::copy(s,value);});
                    }else if(reg>=0xa400 && reg<0xc000){
                        rsx_diag(s,"ifc",reg,[&]{rsxblit::cpu(s,reg,value);});
                    }else if(reg==0xc40c){
                        rsx_diag(s,"blit",reg,[&]{rsxblit::scaled(s,value);});
                    }else if(reg==0x68 || reg==0x6c){
                        const auto address=rsx_semaphore_address(gpu,rsx_reg(gpu,0x60),rsx_reg(gpu,0x64));
                        // Offline replay omits CPU scheduling/wait latency; the
                        // initial snapshot cannot reproduce later PPU writes.
                        if(reg==0x68 && gpu.perf_capture.replay)m.store(address,4,value);
                        if(reg==0x68 && m.load(address,4)!=value){
                            if(!gpu.semaphore_wait)std::fprintf(stderr,"RSX semaphore wait address=%08" PRIx64 " expected=%08x actual=%08" PRIx64 "\n",address,value,m.load(address,4));
                            gpu.semaphore_wait=true;gpu.packet_index=i;publish();return progress;
                        }
                        if(reg==0x68)rsx_capture_method(s,reg,value);
                        if(reg==0x6c)rsx_release_semaphore(s,address,value);
                        gpu.semaphore_wait=false;
                    }else if(reg==0x1d70 || reg==0x1d74){
                        // NV4097 back-end write / texture-read semaphore release (RPCS3 ffba2bf0
                        // NV47/HW/nv4097.cpp:787-842): context DMA 0x1a4, offset 0x1d6c (16-byte aligned).
                        // Back-end release stores (arg&0xff00ff00)|((arg&0xff)<<16)|((arg>>16)&0xff).
                        // Backend publication is ordered after its GPU prefix (or deferred behind a guarded reader).
                        const auto offset=rsx_reg(gpu,0x1d6c);
                        if(offset%16)throw std::runtime_error("unaligned NV4097 semaphore offset");
                        const auto address=rsx_semaphore_address(gpu,rsx_reg(gpu,0x1a4),offset);
                        const auto stored=reg==0x1d70?((value&0xff00ff00u)|((value&0xffu)<<16)|((value>>16)&0xffu)):value;
                        rsx_release_semaphore(s,address,stored,true);
                    }else if(reg==0x50)m.store(gpu.dma+0x48,4,value);
                    else if(reg==0x100 || reg==0x110 || reg==0x1d94){/* NOP / idle / clear already executed. */}
                    else gpu.registers.write(reg,value);
                }
                gpu.packet_index=0;
                next+=count*4;
            }
        }
        ++gpu.commands;progress=true;::RsxPerfCounters::add(::RsxPerfCounters::packets);
        if(gpu.trace)std::fprintf(stderr,"RSX packet io=%08x header=%08x next=%08x completed=%" PRIu64 "\n",get,header,next,gpu.commands);
        get=next;pending=true;
        if(gpu.trace)publish();
    }
    publish();return progress;
    }catch(...){try{publish();}catch(...){}throw;}
}
[[maybe_unused]] static bool rsx_run(PpuState& s,unsigned budget){
#ifdef ANYPS3_RSX_VK
    if(!s.rsx.fifo_enabled)return false;
    // HLE compatibility may enter with a legacy permit. RSX GPU work uses
    // pinned transfers; only observer metadata updates need brief admission.
    const char* requested_backend=std::getenv("ANYPS3_RSX_BACKEND");
    std::unique_ptr<GuestMemory::RawYield> payload;
    if(!requested_backend || std::string(requested_backend)=="gpu")payload=std::make_unique<GuestMemory::RawYield>(s.memory);
    const bool fifo_work=s.memory.load(s.rsx.dma+0x40,4)!=s.memory.load(s.rsx.dma+0x44,4);
    if(s.rsx.graphics_owner && (fifo_work || (s.rsx.vblank_poll_ticks&31u)==0)){auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());
        if(!session.flights.empty()){session.retire(false);session.leave_fifo();}}
    const char* backend=std::getenv("ANYPS3_RSX_BACKEND");
    if(backend && std::string(backend)!="gpu")return rsx_run_impl(s,budget);
    if(!fifo_work)return rsx_run_impl(s,budget,true);
    if(!s.rsx.renderer)s.rsx.renderer=std::make_unique<NativeVulkan>(s.rsx.host);
    if(!s.rsx.graphics_owner){
        if(s.process.rsx_worker.on_owner()){
            rsxgraphics::RendererContext context{s.memory,s.rsx,s.pc,s.thread_id,
                [&s](unsigned id,std::uint64_t name,std::uint64_t flags){rsx_event_publish(s,id,name,flags,"RSX deferred flip event cannot be delivered");},s.memory.regions,false};
            s.rsx.graphics_owner=std::make_shared<rsxgraphics::Session>(*s.rsx.renderer,std::move(context));
            // GPU completion events wake the owner (ANYPS3_RSX_COMPLETION=1) instead of a 100 us poll while flights are outstanding.
            if(s.rsx.renderer->completion_notifier_enabled() && s.process.rsx_worker.queue){auto* queue=s.process.rsx_worker.queue.get();s.rsx.renderer->set_completion_hook([queue]{queue->poke();});}
        }else s.rsx.graphics_owner=std::make_shared<rsxgraphics::Session>(*s.rsx.renderer,s);
    }
    auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());
    session.issuer(s.pc,s.thread_id);session.observe();session.enter_fifo();
    struct Scope {
        PpuState& state;rsxgraphics::Session& session;
        Scope(PpuState& s,rsxgraphics::Session& owner):state(s),session(owner){
            if(s.rsx.graphics_session)throw std::runtime_error("nested RSX graphics FIFO scope");
            session.fifo_context=true;session.context_validated=false;session.validate_texture_context();
            rsxdraw::fifo_target_cache=&session.target_cache;
            state.rsx.graphics_session=&session;
            state.rsx.graphics_clear=[&owner](unsigned mask){return rsxgraphics::clear(owner,mask);};
        }
        ~Scope(){rsxdraw::fifo_target_cache=nullptr;session.fifo_context=false;session.context_validated=false;state.rsx.graphics_session=nullptr;state.rsx.graphics_clear={};}
    } scope(s,session);
    ::RsxPerfCounters::add(::RsxPerfCounters::fifo_runs);
    try{const bool progress=rsx_run_impl(s,budget);session.leave_fifo();return progress;}
    catch(...){session.guard.disarm();if(!session.failed)session.flush(true,nullptr,0,"exception");session.clear_surfaces();throw;}

#else
    return rsx_run_impl(s,budget);
#endif
}
static void rsx_require_idle(PpuState& s){
    const auto& gpu=s.rsx;
    // Never discard a queued, partially decoded or incomplete draw command.
    if(gpu.packet_index || gpu.draw.primitive || gpu.return_offset!=UINT32_MAX || (gpu.fifo_enabled &&
       s.memory.load(gpu.dma+0x40,4)!=s.memory.load(gpu.dma+0x44,4)))
        throw std::runtime_error("RSX teardown/unmap with pending FIFO work is unsupported");
}
static void rsx_unmap_region(GuestMemory& m,std::uint32_t base,std::uint32_t size){
    auto it=std::find_if(m.regions.begin(),m.regions.end(),[&](const auto& v){return v.base==base && v.bytes->size()==size;});
    if(it==m.regions.end() || m.device_allocated<size)
        throw std::runtime_error("RSX owned device mapping is absent or inconsistent");
    m.mark_write(it->bytes->data(),size);m.device_allocated-=size;m.regions.erase(it);
}
// Retire only the physical intervals whose IO/tile interpretation changes.
// Call under a configuration handoff, before replacing the descriptors.
static void rsx_configuration_touch(PpuState& s,unsigned location,std::uint32_t offset,std::uint32_t size){
    if(!s.process.rsx_worker.enabled || !size)return;
    GuestMemory::RawPermit raw(s.memory);
    for(std::uint64_t at=offset;at<std::uint64_t(offset)+size;){
        const auto n=std::min<std::uint64_t>(std::uint64_t(offset)+size-at,0x100000-(at&0xfffff));
        if(location && s.rsx.io.at(at>>20)==UINT32_MAX){at+=n;continue;}
        const auto address=location?rsx_address(s.rsx,at):UINT64_C(0xc0000000)+at;
        const auto range=s.memory.range(address,n,false);
        for(const auto& part:range.parts)s.memory.notify_host(part.data,part.size,true);
        at+=n;
    }
}
static std::uint32_t rsx_syscall(PpuState& s){
    auto& r=s.r;auto& m=s.memory;
    if(s.process.rsx_worker.enabled && !s.process.rsx_worker.on_owner() && r[11]==674 && (r[4]==0x102 || r[4]==0x103))return rsx_worker_display_call(s);
    std::unique_ptr<GuestMemory::ConfigurationPermit> mapping;if(m.renderer_enabled && m.memory_owner())mapping=std::make_unique<GuestMemory::ConfigurationPermit>(m);
    auto& gpu=s.rsx;
    if(r[11]==669){
        // RPCS3 ffba2bf0 sys_rsx.cpp:347 ignores mem_handle; VRAM is singleton.
        if(!gpu.memory_handle)return MM_ENOMEM;
        if(gpu.context)throw std::runtime_error("RSX memory free while a context owns VRAM");
        rsx_unmap_region(m,0xc0000000,gpu.local_size);
        gpu.memory_handle=0;gpu.local_size=0;return 0;
    }
    if(r[11]==671){
        if(!gpu.context || static_cast<std::uint32_t>(r[3])!=gpu.context)return MM_EINVAL;
        rsx_require_idle(s);
        rsx_unmap_region(m,gpu.dma,0x300000);
        for(auto& t:s.ppu_threads)if(t.wait_kind==1 && t.wait_id==gpu.event_queue){
            t.wait_kind=0;t.wait_id=0;t.r[3]=0x80010013;t.pc+=4;s.reschedule=true;
        }
        for(auto& p:s.events.ports)if(p.queue==gpu.event_queue)p.queue=0;
        auto& ports=s.events.ports;auto& queues=s.events.queues;
        ports.erase(std::remove_if(ports.begin(),ports.end(),[&](const auto& p){return p.id==gpu.event_port;}),ports.end());
        queues.erase(std::remove_if(queues.begin(),queues.end(),[&](const auto& q){return q.id==gpu.event_queue;}),queues.end());
        // Reset all context-owned state, retaining the independently mapped
        // device/VRAM and cumulative diagnostic counters across reallocation.
        auto& worker=s.process.rsx_worker;
        if(worker.enabled && !worker.on_owner()){GuestMemory::RawYield yield(m);
            worker.queue->call(RsxOwnerQueue::Kind::MapBarrier,[&]{auto& f=*static_cast<PpuState*>(worker.facade.get());rsx_worker_release(f);
#ifdef ANYPS3_VULKAN
                f.rsx.renderer.reset();
#endif
            });
        }
        NativeRsx fresh;fresh.device=gpu.device;fresh.local_size=gpu.local_size;fresh.memory_handle=gpu.memory_handle;
#ifdef ANYPS3_VULKAN
        fresh.host=gpu.host;
#endif
        fresh.trace=gpu.trace;fresh.commands=gpu.commands;fresh.rendered_clears=gpu.rendered_clears;
        fresh.captured_frames=gpu.captured_frames;fresh.flips=gpu.flips;
        fresh.draw.draws=gpu.draw.draws;fresh.draw.triangles=gpu.draw.triangles;fresh.draw.pixels=gpu.draw.pixels;
        gpu=std::move(fresh);
        return 0;
    }
    if(r[11]==673){
        const auto io=r[4],size=r[5];
        if(!gpu.context || static_cast<std::uint32_t>(r[3])!=gpu.context || !size ||
           ((io|size)&0xfffff) || io>=0x10000000 || size>0x10000000-io)return MM_EINVAL;
        rsx_require_idle(s);
        rsx_configuration_touch(s,1,io,size);
        for(auto page=io>>20;page<(io+size)>>20;++page){gpu.io[page]=UINT32_MAX;gpu.io_flags[page]=0;}
        std::atomic_thread_fence(std::memory_order_seq_cst);return 0;
    }
    if(r[11]==676){
        if(static_cast<std::uint32_t>(r[3])!=8)throw std::runtime_error("unsupported RSX device unmap id");
        if(gpu.context)throw std::runtime_error("RSX device unmap while a context is active");
        // RPCS3 has only a TODO here. Implement an actual release of this
        // runtime's singleton mapping; repeating an absent unmap is harmless.
        if(gpu.device){rsx_unmap_region(m,gpu.device,0x100000);gpu.device=0;}
        return 0;
    }
    if(r[11]==677)
        throw std::runtime_error("unsupported RSX global attribute package "+std::to_string(static_cast<std::uint32_t>(r[3])));
    if(r[11]==674){
        if(!gpu.context || r[3]!=gpu.context)return MM_EINVAL;
        if(r[4]==0x103){
            if(r[5]>1 || r[6]>7)return MM_EINVAL;
            rsx_queue_display(s,r[5],r[6]);return 0;
        }
        if(r[4]==0x102){
            if(r[5]>1 || r[6]>UINT32_MAX)return MM_EINVAL;
            rsx_flip_display(s,r[5],r[6]);return 0;
        }
        if(r[4]==1){
            if((r[5]|r[6])&3 || r[5]>UINT32_MAX || r[6]>UINT32_MAX)return MM_EINVAL;
            rsx_address(gpu,r[5]);rsx_address(gpu,r[6]);
            m.store(gpu.dma+0x44,4,r[5]);m.store(gpu.dma+0x40,4,r[6]);gpu.return_offset=UINT32_MAX;gpu.packet_index=0;gpu.semaphore_wait=false;gpu.fifo_enabled=true;return 0;
        }
        if(r[4]==0x10a){
            if(r[5]>7)return 0xffffffef;
            const auto address=gpu.driver+0x10c0+r[5]*0x40;
            m.store(address,4,(m.load(address,4)&static_cast<std::uint32_t>(r[6]))|static_cast<std::uint32_t>(r[7]));
            return 0;
        }
        if(r[4]==0x104){
            const auto id=r[5]&0xff;if(id>=gpu.display_buffers.size())return 0xffffffef;
            if(r[8])throw std::runtime_error("unsupported RSX display buffer flags");
            auto& buffer=gpu.display_buffers[id];buffer.width=r[6]>>32;buffer.height=r[6];buffer.pitch=r[7]>>32;buffer.offset=r[7];
            std::fprintf(stderr,"RSX display buffer id=%" PRIu64 " width=%u height=%u pitch=%u offset=%08x\n",id,buffer.width,buffer.height,buffer.pitch,buffer.offset);
            return 0;
        }
        if(r[4]==0x300){
            if(r[5]>=gpu.tiles.size())return MM_EINVAL;
            if(r[8])throw std::runtime_error("unsupported RSX tile attribute flags");
            const auto descriptor=static_cast<std::uint32_t>(r[6]>>32),limit=static_cast<std::uint32_t>(r[6]);
            const auto format=static_cast<std::uint32_t>(r[7]);
            NativeRsx::Tile tile;tile.range_word=r[6];tile.format_word=r[7];tile.bound=(descriptor&3)!=0;
            tile.location=(descriptor&3)-1;tile.offset=descriptor&0x7fff0000;
            const auto end=std::uint64_t(limit&0x7fff0000)+0x10000;
            tile.pitch=(r[7]>>32)&0xffffff00;tile.compression=(format>>26)&15;tile.base=format&0x7ff;tile.bank=(descriptor>>4)&15;
            if(tile.bound){
                if(end<=tile.offset || !tile.pitch || tile.location>1 || !(format&0x40000000))return MM_EINVAL;
                const auto bound=tile.location?0x10000000:gpu.local_size;
                if(end>bound)return MM_EINVAL;
                if(tile.location)for(auto page=tile.offset>>20;page<=(end-1)>>20;++page)if(gpu.io[page]==UINT32_MAX)return MM_EINVAL;
            }
            const auto& old_tile=gpu.tiles[r[5]];
            if(old_tile.bound)rsx_configuration_touch(s,old_tile.location,old_tile.offset,old_tile.size);
            tile.size=end>tile.offset?end-tile.offset:0;gpu.tiles[r[5]]=tile;
            std::fprintf(stderr,"RSX tile id=%" PRIu64 " bound=%u location=%u offset=%08x size=%08x pitch=%u compression=%u\n",r[5],unsigned(tile.bound),tile.location,tile.offset,tile.size,tile.pitch,tile.compression);
            return 0;
        }
        if(r[4]==0x301){
            if(r[5]>=gpu.zculls.size())return 0xffffffef;
            NativeRsx::Zcull region;region.region_word=r[6];region.offset_word=r[7];region.status_word=r[8];
            region.width=((r[6]&UINT32_MAX)>>22)*64;region.height=r[6]&0xffc0;
            region.start=(r[7]>>32)&0xfffff000;region.offset=r[7]&0x0fffffff;
            const auto status=static_cast<std::uint32_t>(r[8]);region.bound=status!=0;
            if(region.bound && (!region.width || !region.height ||
               std::uint64_t(region.start)+std::uint64_t(region.width)*region.height>0x300000 ||
               region.offset>=gpu.local_size || !(r[6]&(UINT64_C(1)<<32)) || status!=0x202000))return MM_EINVAL;
            gpu.zculls[r[5]]=region;++gpu.zcull_generation;
            std::fprintf(stderr,"RSX Z-cull id=%" PRIu64 " bound=%u width=%u height=%u offset=%08x\n",r[5],unsigned(region.bound),region.width,region.height,region.offset);
            return 0;
        }
        throw std::runtime_error("unimplemented RSX context attribute "+std::to_string(r[4]));
    }
    if(r[11]==668){
        const auto size=r[5];
        if(!size || size%0x100000 || size>0x10000000)return 0xffffffef;
        if(r[6]!=0x80000 || r[7]!=0x300000 || r[8]!=15 || r[9]!=8)
            throw std::runtime_error("unsupported RSX local-memory allocation attributes");
        if(gpu.memory_handle)return MM_ENOMEM;
        m.access(r[3],4,true);m.access(r[4],8,true);
        m.map(0xc0000000,size,6,nullptr,0,true);gpu.local_size=size;gpu.memory_handle=0x5a5a5a5b;
        m.store(r[3],4,gpu.memory_handle);m.store(r[4],8,0xc0000000);
        std::fprintf(stderr,"RSX local memory allocated size=%08x\n",gpu.local_size);return 0;
    }
    if(r[11]==670){
        if(!gpu.device || !gpu.memory_handle || r[7]!=gpu.memory_handle)return MM_EINVAL;
        if(gpu.context)return MM_EBUSY;
        if(r[8]&~UINT64_C(0x20))throw std::runtime_error("unsupported RSX context mode");
        m.access(r[3],4,true);for(unsigned i=4;i<=6;++i)m.access(r[i],8,true);
        const auto dma=gpu.device+0x100000,driver=dma+0x100000,reports=driver+0x100000;
        m.map(dma,0x300000,6,nullptr,0,true);
        gpu.context=0x55555555;gpu.dma=dma;gpu.driver=driver;gpu.reports=reports;
        // The newly created context has no pending display work. This is its
        // initial availability gate, not completion of any submitted frame.
        m.store(gpu.device+0x30,4,1);
        m.store(driver,4,0x211);m.store(driver+4,4,0x5c);m.store(driver+8,4,gpu.local_size);
        m.store(driver+12,4,1);m.store(driver+16,4,500000000);m.store(driver+20,4,650000000);
        m.store(driver+0x2c,4,0x1000);m.store(driver+0x34,4,0x1400);
        m.store(driver+0x50,4,r[8]); // Actual structure layout, including firmware 3.01 mode bit.
        for(unsigned i=0;i<64;++i)m.store(reports+0x1000+i*16,8,UINT64_MAX);
        for(unsigned i=0;i<2048;++i){m.store(reports+0x1400+i*16,8,UINT64_MAX);m.store(reports+0x140c+i*16,4,UINT32_MAX);}
        gpu.event_queue=s.events.next_queue;s.events.next_queue+=0x100;
        gpu.event_port=s.events.next_port;s.events.next_port+=0x100;
        s.events.queues.push_back({gpu.event_queue,32,0,{}});
        s.events.ports.push_back({gpu.event_port,1,gpu.event_queue,(UINT64_C(1)<<32)|gpu.event_port});
        m.store(driver+0x12d0,4,gpu.event_queue);
        m.store(r[3],4,gpu.context);m.store(r[4],8,dma);m.store(r[5],8,driver);m.store(r[6],8,reports);
        std::fprintf(stderr,"RSX context allocated dma=%08x driver=%08x reports=%08x queue=%08x\n",dma,driver,reports,gpu.event_queue);return 0;
    }
    if(r[11]==672){
        const auto io=r[4],ea=r[5],size=r[6];
        if(r[3]!=gpu.context || !gpu.context || !size || (io|ea|size)%0x100000 ||
           io>=0x10000000 || size>0x10000000-io || ea>UINT32_MAX || size>UINT64_C(0x100000000)-ea)return MM_EINVAL;
        const auto access_flags=r[7]&~(UINT64_C(1)<<60);
        if(access_flags && access_flags!=UINT64_C(0xe000000000000800))throw std::runtime_error("unsupported RSX IO mapping flags");
        // IO pages may span adjacent guest backings (RSXMEM GuestMemory::range).
        for(std::uint64_t offset=0;offset<size;offset+=0x100000)(void)m.range(ea+offset,0x100000,false);
        rsx_configuration_touch(s,1,io,size);
        for(std::uint64_t offset=0;offset<size;offset+=0x100000){gpu.io[(io+offset)>>20]=ea+offset;gpu.io_flags[(io+offset)>>20]=r[7];}
        std::atomic_thread_fence(std::memory_order_seq_cst);
        std::fprintf(stderr,"RSX IO mapped io=%08" PRIx64 " ea=%08" PRIx64 " size=%08" PRIx64 "\n",io,ea,size);return 0;
    }
    if(r[11]==675){
        if(r[5]!=8)throw std::runtime_error("unsupported RSX device mapping");
        m.access(r[3],8,true);
        if(!gpu.device){
            for(std::uint64_t base=0x60000000;base<0xc0000000;base+=0x10000000){
                bool conflict=false;
                for(const auto& area:s.mmapper.areas)if(base<area.base+area.size && area.base<base+0x10000000)conflict=true;
                for(const auto& area:m.regions)if(base<area.base+area.bytes->size() && area.base<base+0x10000000)conflict=true;
                if(conflict)continue;
                m.map(base,0x100000,6,nullptr,0,true);gpu.device=base;break;
            }
            if(!gpu.device)return MM_ENOMEM;
        }
        m.store(r[3],8,gpu.device);
        std::fprintf(stderr,"RSX device mapped address=%08x\n",gpu.device);return 0;
    }
    throw std::runtime_error("unimplemented native RSX syscall "+std::to_string(r[11]));
}
'''
