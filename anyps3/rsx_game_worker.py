"""Opt-in game FIFO owner. Default serial execution remains the reference."""
TYPES = r'''
struct NativeRsxWorker {
    bool enabled=false;
    std::unique_ptr<RsxOwnerQueue> queue;
    std::shared_ptr<void> facade;
#if defined(__linux__)
    std::unique_ptr<NativeHostFaultService> faults;
#endif
    ExecutionTiming timing;
    std::atomic<std::uint64_t> flips{0},draws{0},rsx_ns{0},gpu_ns{0},flip_ns{0};
    std::atomic<bool> progress{false};
    bool paused=false;unsigned barrier_depth=0;
    // Owner publishes only while FIFO configuration is stable. Coordinator
    // never reads the mutable NativeRsx fields. PUT itself is the doorbell.
    std::atomic<std::uint32_t> fifo_put_address{0};
    std::atomic<bool> fifo_work{false},fifo_queued{false};
    std::uint64_t posted_put=UINT64_MAX; // coordinator-only, reset on handoff

    std::atomic<std::uint64_t> map_barriers{0},host_requests{0};
    std::optional<std::array<std::uint64_t,6>> last_metrics;
    std::optional<std::array<std::uint64_t,3>> last_memory;
#ifdef ANYPS3_RSX_PERF_COUNTERS
    std::optional<std::array<std::uint64_t,RsxPerfCounters::count>> last_counters;
#endif
    std::array<std::atomic<std::uint64_t>,32> stage_ns{};std::array<std::uint64_t,32> last_stage_ns{};
    std::function<void()> stop;
    std::shared_ptr<VulkanHostService> host;
    bool on_owner()const{return enabled && queue && RsxOwnerQueue::current==queue.get();}
    void check(){if(queue)queue->check();}
    ~NativeRsxWorker(){if(stop)stop();else if(queue)queue->shutdown();}
};
'''
# NativeVulkan/host is absent in CPU-only configurations.
TYPES = TYPES.replace('    std::shared_ptr<VulkanHostService> host;', '''#ifdef ANYPS3_VULKAN
    std::shared_ptr<VulkanHostService> host;
#endif''')

CPP = r'''
static bool rsx_run_inline(PpuState&,unsigned);
static bool rsx_tick_vblank(PpuState&,std::uint64_t);
static_assert(unsigned(rsxdraw::HostStage::count)<=32,"NativeRsxWorker::stage_ns holds 32 host stages");
// Idle service ticks of the RSX owner (GPU completion polling every 100 us while flights are outstanding): count and time (PERF RSX_SERVICE).
static std::atomic<std::uint64_t>& rsx_service_calls(){static std::atomic<std::uint64_t> v{0};return v;}
static std::atomic<std::uint64_t>& rsx_service_ns(){static std::atomic<std::uint64_t> v{0};return v;}
static void rsx_worker_pump(PpuState& s){
    auto& w=s.process.rsx_worker;if(!w.enabled){w.check();return;}if(w.on_owner())return;
    GuestMemory::RawYield yield(s.memory);w.queue->pump_coordinator();w.check();
}
static void rsx_worker_events(PpuState& s,const std::function<void()>& invoke){
    auto& w=s.process.rsx_worker;
    if(w.on_owner())w.queue->owner_call(invoke);else invoke();
}
static void rsx_worker_release(PpuState& s){
#ifdef ANYPS3_RSX_VK
    auto& g=s.rsx;
    if(g.graphics_owner){auto& session=*static_cast<rsxgraphics::Session*>(g.graphics_owner.get());
        session.enter_fifo();session.flush(true,nullptr,0,"map_barrier");session.clear_surfaces();session.clear_raw("map_barrier");
        g.graphics_session=nullptr;g.graphics_clear={};g.graphics_owner.reset();}
#else
    (void)s;
#endif
}
static void rsx_worker_publish(PpuState& s){
    auto& w=s.process.rsx_worker;const auto ns=w.timing.snapshot(std::chrono::steady_clock::now());
    if(!w.paused){
        w.fifo_work.store(s.rsx.fifo_enabled && s.memory.load(s.rsx.dma+0x40,4)!=s.memory.load(s.rsx.dma+0x44,4),std::memory_order_release);
        w.fifo_put_address.store(s.rsx.fifo_enabled?s.rsx.dma+0x40:0,std::memory_order_release);
    }
    w.flips.store(s.rsx.flips,std::memory_order_release);w.draws.store(s.rsx.draw.draws,std::memory_order_release);
    w.rsx_ns.store(ns[unsigned(ExecutionPhase::Rsx)],std::memory_order_release);
    {auto& h=rsxdraw::host_stats();if(h.enabled){h.select(h.active);for(unsigned i=0;i<h.ns.size();++i)w.stage_ns[i].store(h.ns[i],std::memory_order_relaxed);}}
    w.gpu_ns.store(ns[unsigned(ExecutionPhase::GpuWait)],std::memory_order_release);
    w.flip_ns.store(ns[unsigned(ExecutionPhase::FlipWait)],std::memory_order_release);
}
static void rsx_worker_start(PpuState& s){
    auto& w=s.process.rsx_worker;
    if(w.enabled){w.check();return;}
    if(!anyps3_trace_enabled("ANYPS3_RSX_WORKER"))return;
    auto& m=s.memory;if(!m.concurrent())m.enable_concurrency();m.restart_coherence();m.enable_page_broker();
    auto facade=std::make_shared<PpuState>(s.process);w.facade=facade;
    auto* f=facade.get();w.queue=std::make_unique<RsxOwnerQueue>();auto& q=*w.queue;
    w.paused=true;w.barrier_depth=0;
#ifdef ANYPS3_VULKAN
    w.host=std::make_shared<VulkanHostService>(std::this_thread::get_id(),[&q](std::function<void()> invoke){q.owner_call(invoke);});
    s.rsx.host=w.host;s.present.host_dispatch=[&w](const std::function<void()>& invoke){w.host->invoke(invoke);};
#endif
    // A configuration handoff only stops FIFO admission. Session/device,
    // pipelines and unaffected images survive it. Map invalidation runs before
    // the old immutable map snapshot is replaced, while its pins remain valid.
    m.renderer_barrier=[&w,&m,f](bool enter){
        if(enter){if(w.barrier_depth++)return;w.posted_put=UINT64_MAX;
            ExecutionScope wait(ExecutionPhase::RsxWait);
            try{w.queue->call(RsxOwnerQueue::Kind::MapBarrier,[&w]{w.paused=true;w.fifo_put_address.store(0,std::memory_order_release);++w.map_barriers;});}
            catch(...){--w.barrier_depth;throw;}
        }else{
            if(!w.barrier_depth)std::terminate();
            if(--w.barrier_depth)return;
            try{
                auto maps=m.regions;
                w.queue->post(RsxOwnerQueue::Kind::MapBarrier,[&w,f,maps=std::move(maps)]()mutable{
#ifdef ANYPS3_RSX_VK
                    if(f->rsx.graphics_owner){auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());session.map_snapshot(std::move(maps));session.context_validated=false;}
#else
                    (void)f;(void)maps;
#endif
                    w.paused=false;rsx_worker_publish(*f);
                });
            }catch(...){w.queue->poison(std::current_exception(),0,HostAccessContext::sample());}
        }
    };
    m.renderer_maps_changed=[&w,&m,f]{
        if(!w.barrier_depth)throw std::runtime_error("guest maps changed outside configuration handoff");
        std::vector<GuestMemory::Region> removed;
        for(const auto& old:m.parallel->snapshot->maps){const auto& r=old.region;
            const auto found=std::find_if(m.regions.begin(),m.regions.end(),[&](const auto& n){return n.base==r.base && n.flags==r.flags && n.identity==r.identity && n.bytes==r.bytes;});
            if(found==m.regions.end())removed.push_back(r);
        }
        // Pure additions have no old GPU ownership to materialize.
        if(removed.empty())return;
        ExecutionScope wait(ExecutionPhase::RsxWait);
        w.queue->call(RsxOwnerQueue::Kind::MapBarrier,[f,removed=std::move(removed)]{
#ifdef ANYPS3_RSX_VK
            if(!f->rsx.graphics_owner)return;
            auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
            GuestMemory::ObserverPause pause(f->memory);
            for(const auto& r:removed){auto* p=r.bytes->data();const auto size=r.bytes->size();
                session.host_access(p,size,true);
                // Layouts retain their original Range identities, too.
                session.layout_cache.erase(std::remove_if(session.layout_cache.begin(),session.layout_cache.end(),[&](const auto& surface){return surface.overlaps(p,size);}),session.layout_cache.end());
            }
            session.context_validated=false;
#else
            (void)f;(void)removed;
#endif
        });
    };
    m.renderer_access=[&w,&m](const void* p,std::uint64_t n,bool write){
        if(m.observer_paused())return;
        GuestMemory::RawYield yield(m);
        auto invoke=[&w,&m,p,n,write]{
            GuestMemory::ObserverPause pause(m);
            if(m.host_access_observer && (m.host_access_observer.interested(p,n) || m.raw_cpu_dirty_interested(p,n,write)))m.host_access_observer.callback(p,n,write);
        };
        if(w.on_owner())invoke();else{ExecutionScope wait(ExecutionPhase::RsxWait);++w.host_requests;w.queue->call(RsxOwnerQueue::Kind::HostAccess,invoke);}
    };
    m.renderer_metadata_allowed=[&w]{w.check();return true;};
    m.renderer_pin=[&m](const std::function<void()>& invoke){
        if(!m.renderer_owner())throw std::runtime_error("RSX pin transaction outside renderer owner");
        // ConfigurationPermit stops this FIFO thread before publishing maps.
        // Its immutable snapshot/pins therefore survive the whole transaction;
        // no coordinator RPC is needed for an existing validated Range.
        auto& local=m.local_state();const bool previous=local.renderer_claim;local.renderer_claim=true;
        struct Reset{bool& flag;bool previous;~Reset(){flag=previous;}} reset{local.renderer_claim,previous};
        invoke();
    };
    m.renderer_enabled=true;w.enabled=true;
    q.run_fifo=[&w,f](unsigned budget){
        struct Retire {NativeRsxWorker& worker;~Retire(){worker.fifo_queued.store(false,std::memory_order_release);}} retire{w};
        if(w.paused)return;
        ExecutionScope phase(w.timing,ExecutionPhase::Rsx);
        w.progress.store(rsx_run_inline(*f,budget),std::memory_order_release);rsx_worker_publish(*f);
    };
    q.service=[&w,f]{if(w.paused)return;ExecutionScope phase(w.timing,ExecutionPhase::Rsx);
        const auto service_start=std::chrono::steady_clock::now();struct ServiceTime{std::chrono::steady_clock::time_point start;~ServiceTime(){rsx_service_calls().fetch_add(1,std::memory_order_relaxed);rsx_service_ns().fetch_add(std::uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count()),std::memory_order_relaxed);}} service_time{service_start};
        const auto now=std::chrono::steady_clock::now().time_since_epoch();
        (void)rsx_tick_vblank(*f,std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());
#ifdef ANYPS3_RSX_VK
        if(f->rsx.graphics_owner){auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
            if(!session.flights.empty()){session.retire(false);session.leave_fifo();}}
#endif
        rsx_worker_publish(*f);
    };
    // Configuration/HostAccess can change FIFO controls even when idle service
    // has been replaced by an embedding/test host. Publish before its ack.
    q.after_request=[&w,f]{
        // Preserve the original service/error ordering at startup and after
        // configuration: display events precede the first FIFO payload read.
        if(!w.paused){const auto now=std::chrono::steady_clock::now().time_since_epoch();
            (void)rsx_tick_vblank(*f,std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());}
        rsx_worker_publish(*f);
    };
    q.deadline=[&w,f]{
        auto due=std::chrono::steady_clock::time_point::max();if(w.paused)return due;
#ifdef ANYPS3_RSX_VK
        // PUT can remain unchanged while the CPU polls a deferred GPU label.
        // Dropping empty FIFO ticks must not turn completion into a vblank
        // wait (or an infinite wait when no display context is installed).
        if(f->rsx.graphics_owner && !static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get())->flights.empty()){
            // ANYPS3_RSX_POLL=adaptive: the first poll of the oldest flight comes when ~70% of its expected GPU time has passed
            // (EWMA of earlier flights), then every 40 us; default: every 100 us.
            static const bool adaptive=[]{const char* v=std::getenv("ANYPS3_RSX_POLL");if(v && std::strcmp(v,"adaptive") && std::strcmp(v,"fixed"))throw std::runtime_error("invalid ANYPS3_RSX_POLL");return v && !std::strcmp(v,"adaptive");}();
            const auto now=std::chrono::steady_clock::now();
            if(f->rsx.renderer && f->rsx.renderer->completion_notifier_enabled())due=now+std::chrono::milliseconds(2);   // safety net: completions poke the queue
            else if(adaptive){auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
                const auto expected=session.flights.front().submitted+std::chrono::nanoseconds(std::int64_t(session.flight_ns*0.7));
                due=std::max(expected,now+std::chrono::microseconds(40));}
            else due=now+std::chrono::microseconds(100);
        }
#endif
        if(!f->rsx.context || !f->rsx.fifo_enabled || !f->rsx.vblank_epoch_ns)return due;
        const auto now=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
        const auto elapsed=std::uint64_t(now)-f->rsx.vblank_epoch_ns;
        const auto ticks=(elapsed/1001000000)*60+(elapsed%1001000000)*60/1001000000;
        const auto next=std::max(f->rsx.vblank_ticks,ticks)+1;
        return std::min(due,std::chrono::steady_clock::time_point(std::chrono::nanoseconds(f->rsx.vblank_epoch_ns+(next/60)*1001000000+(next%60)*1001000000/60)));
    };
    q.idle_teardown=[&w,f]{try{rsx_worker_release(*f);}catch(...){
#ifdef ANYPS3_RSX_VK
        f->rsx.graphics_owner.reset();
#endif
        throw;}
#ifdef ANYPS3_VULKAN
        f->rsx.renderer.reset();
#endif
        rsx_worker_publish(*f);
    };
    w.stop=[&w,&m,f]{
        if(!w.enabled)return;
        // SPU has already joined. Keep SDL/main dispatch alive through teardown.
        if(w.queue->thread.joinable()){try{w.queue->await(w.queue->post(RsxOwnerQueue::Kind::Stop));}catch(...){}w.queue->shutdown();}m.renderer_barrier={};m.renderer_maps_changed={};m.renderer_access={};m.renderer_pin={};m.renderer_cancel={};m.renderer_metadata_allowed={};
        #if defined(__linux__)
        w.faults.reset();
#endif
        #ifdef ANYPS3_VULKAN
        f->rsx.host.reset();f->present.host_dispatch={};
#endif
        m.renderer_enabled=false;m.renderer_thread={};m.page_broker.delegated_owner={};w.enabled=false;w.facade.reset();
    };
    m.renderer_cancel=[&w]{
#if defined(__linux__)
        // The main thread cannot join its own active signal frame through
        // NativeHostFaultService::~NativeHostFaultService. The bridge has
        // logged the original owner error; use the existing fault exit code.
        if(w.faults && w.faults->main_slot.load(std::memory_order_acquire)>=0){
            static const char message[]="RSX raw HLE fault owner failure\n";
            (void)!::write(2,message,sizeof(message)-1);_exit(125);
        }
#endif
        if(w.stop)w.stop();
    };
    q.coordinator_wake=[progress=s.process.spu_worker.owner_progress]{
        // A flip/host callback can arrive while the coordinator is parked in
        // SPU pause, rather than in an RSX queue wait or a signal frame.
        progress->signal();
#if defined(__linux__)
        NativeHostFaultService::wake_main();
#endif
    };
    q.failure=[&s](std::exception_ptr error,std::uint64_t sequence,HostAccessOrigin origin){
        s.supervisor.record(ExecutionEngine::Rsx,sequence,origin.guest_pc,origin.guest_id,error);
    };
    q.thread_scope=[]()->std::shared_ptr<void>{
        if(!anyps3_sample::config().selected(anyps3_sample::Role::Rsx))return {};
        return std::make_shared<anyps3_sample::Thread>(anyps3_sample::Role::Rsx);
    };
    q.launch();
    // Thread identity is published in an acknowledged ordered request.
    q.call(RsxOwnerQueue::Kind::MapBarrier,[&m,&w]{m.renderer_thread=std::this_thread::get_id();m.page_broker.delegated_owner=m.renderer_thread;w.paused=false;});
#if defined(__linux__)
    w.faults=std::make_unique<NativeHostFaultService>([&w,&m](std::uintptr_t address){
        bool handled=false;++w.host_requests;
        try{w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{
            auto& local=m.local_state();local.fault_claim=true;
            struct Reset{bool& flag;~Reset(){flag=false;}} reset{local.fault_claim};
            for(auto* guard=GuestMemory::HostGuard::head;guard;guard=guard->next)if(guard->touches_any(reinterpret_cast<const void*>(address),1)){
#ifdef ANYPS3_RSX_VK
                auto* session=static_cast<rsxgraphics::Session*>(static_cast<PpuState*>(w.facade.get())->rsx.graphics_owner.get());
                if(session && guard==&session->guard){
                    // A legacy retained pointer identifies a faulting PAGE,
                    // not an access to every resident image in the Session.
                    const auto page=GuestMemory::HostGuard::page_size();
                    const auto first=address&~(page-1);auto* pointer=reinterpret_cast<const std::uint8_t*>(first);
                    GuestMemory::ObserverPause pause(m);HostByteAccess raw_access(false,true);
                    session->host_access(pointer,page,true);
                    // The fault may retry a write. Publish reservation damage
                    // only for this page; metadata admission is brief and never
                    // covers the readback above. Backings are page aligned.
                    {GuestMemory::RawPermit metadata(m);m.mark_write(pointer,page);}
                }else
#endif
                {guard->disarm();guard->synchronize();}
                handled=true;break;
            }
        });}catch(const std::exception& error){
            // This callback runs on the bridge, outside the signal frame.
            const auto message=std::string("RSX protected backing synchronization failed: ")+error.what()+"\n";
            (void)!::write(2,message.data(),message.size());throw;
        }
        return handled;
    },[&q]{q.pump_coordinator();},[&m](const std::function<void()>& wait){
        const auto& local=m.local_state();if(local.leases)throw std::runtime_error("RSX raw fault inside reader admission");
        GuestMemory::RawYield yield(m);ExecutionScope phase(ExecutionPhase::RsxWait);wait();
    });
    long native_owner=0;q.call(RsxOwnerQueue::Kind::MapBarrier,[&]{native_owner=syscall(SYS_gettid);});w.faults->install(native_owner);
#endif

}
static std::uint32_t rsx_worker_display_call(PpuState& s){
    auto& w=s.process.rsx_worker;std::array<std::uint64_t,32> args;std::copy_n(s.r,32,args.begin());const auto pc=s.pc,tid=s.thread_id;std::uint32_t result=0;
    GuestMemory::RawYield yield(s.memory);ExecutionScope wait(ExecutionPhase::RsxWait);++w.host_requests;
    w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{
        auto& f=*static_cast<PpuState*>(w.facade.get());std::copy(args.begin(),args.end(),f.r);f.pc=pc;f.thread_id=tid;
        ExecutionScope phase(w.timing,ExecutionPhase::Rsx);result=rsx_syscall(f);rsx_worker_publish(f);
    });return result;
}
static void rsx_worker_perf(const PpuState& s){
    auto& w=s.process.rsx_worker;if(!w.enabled)return;
    const auto main=s.execution.snapshot(std::chrono::steady_clock::now());
    const std::array<std::uint64_t,6> current{{w.rsx_ns.load(std::memory_order_acquire),w.gpu_ns.load(std::memory_order_acquire),w.flip_ns.load(std::memory_order_acquire),main[unsigned(ExecutionPhase::RsxWait)],w.map_barriers.load(std::memory_order_acquire),w.host_requests.load(std::memory_order_acquire)}};
    if(w.last_metrics){const auto& last=*w.last_metrics;
        std::fprintf(stderr,"PERF RSX_WORKER WALL rsx_ns=%" PRIu64 " gpu_wait_ns=%" PRIu64 " flip_wait_ns=%" PRIu64 " main_wait_ns=%" PRIu64 " map_barriers=%" PRIu64 " host_requests=%" PRIu64 "\n",current[0]-last[0],current[1]-last[1],current[2]-last[2],current[3]-last[3],current[4]-last[4],current[5]-last[5]);}
    w.last_metrics=current;
#ifdef ANYPS3_RSX_PERF_COUNTERS
    std::array<std::uint64_t,RsxPerfCounters::count> counters{};
    for(unsigned i=0;i<counters.size();++i)counters[i]=RsxPerfCounters::values[i].load(std::memory_order_relaxed);
    if(w.last_counters){std::string line="PERF RSX_COUNTERS";char item[64];
        for(unsigned i=0;i<counters.size();++i){std::snprintf(item,sizeof(item)," %s=%" PRIu64,RsxPerfCounters::names[i],counters[i]-(*w.last_counters)[i]);line+=item;}
        std::fprintf(stderr,"%s\n",line.c_str());}
    w.last_counters=counters;
#endif
    {static std::uint64_t last_calls=0,last_ns=0;const auto calls=rsx_service_calls().load(std::memory_order_relaxed),ns=rsx_service_ns().load(std::memory_order_relaxed);
        std::fprintf(stderr,"PERF RSX_SERVICE calls=%" PRIu64 " ns=%" PRIu64 "\n",calls-last_calls,ns-last_ns);last_calls=calls;last_ns=ns;}
    {const std::array<std::uint64_t,3> mem{{GuestMemory::stat_membarriers.load(std::memory_order_relaxed),GuestMemory::stat_slow_leases.load(std::memory_order_relaxed),GuestMemory::stat_gpu_claims.load(std::memory_order_relaxed)}};
        if(w.last_memory)std::fprintf(stderr,"PERF GUEST_MEMORY membarriers=%" PRIu64 " slow_leases=%" PRIu64 " gpu_claims=%" PRIu64 "\n",mem[0]-(*w.last_memory)[0],mem[1]-(*w.last_memory)[1],mem[2]-(*w.last_memory)[2]);
        w.last_memory=mem;}
    if(rsxdraw::host_stats().enabled){
        static const char* names[]={"idle","indices","assembly","state","texture_metadata","targets","certification","shader_source","geometry","vertex_upload","texture_upload","record","flush","tail","draw","kick","g_inputs","g_state","g_shaders","g_data"};
        std::string line="PERF RSX_STAGES";char item[64];
        for(unsigned i=0;i<unsigned(rsxdraw::HostStage::count);++i){const auto v=w.stage_ns[i].load(std::memory_order_relaxed);std::snprintf(item,sizeof(item)," %s_ns=%" PRIu64,names[i],v-w.last_stage_ns[i]);line+=item;w.last_stage_ns[i]=v;}
        std::fprintf(stderr,"%s\n",line.c_str());
    }
}
static EngineStepReply rsx_worker_request(PpuState& s,unsigned budget){
    auto& w=s.process.rsx_worker;rsx_worker_pump(s);
    // Consume the progress flag without a locked RMW when nothing was published.
    auto progress=[&w]{return w.progress.load(std::memory_order_relaxed) && w.progress.exchange(false,std::memory_order_acq_rel);};
    const auto address=w.fifo_put_address.load(std::memory_order_acquire);
    if(!address || w.fifo_queued.load(std::memory_order_acquire))return {progress(),false};
    const auto put=s.memory.load(address,4);
    if(put==w.posted_put && !w.fifo_work.load(std::memory_order_acquire))return {progress(),false};
    const auto pc=s.pc,tid=s.thread_id;w.fifo_queued.store(true,std::memory_order_release);
    try{w.queue->post(RsxOwnerQueue::Kind::RunFifo,[f=static_cast<PpuState*>(w.facade.get()),pc,tid]{f->pc=pc;f->thread_id=tid;},budget);w.posted_put=put;}
    catch(...){w.fifo_queued.store(false,std::memory_order_release);throw;}
    return {progress(),false};
}
'''
