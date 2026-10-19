"""Opt-in SPU executors. LV2/control access transfers at acknowledged safe points.

The coordinator never executes the SPU backend in worker mode. It services
coherence RPC while waiting for a safe point, tick or shutdown, without holding
queue, memory or LV2 locks. The serial composition is left intact.
"""
from .rsx_worker import TYPES as RSX_OWNER_TYPES
from .rsx_game_worker import TYPES as RSX_GAME_TYPES, CPP as RSX_GAME_CPP

TYPES = RSX_OWNER_TYPES + RSX_GAME_TYPES + r'''

// Opaque/legacy callbacks remain conservative. Call sites with proven operands
// use this descriptor; it never substitutes for validation in the backend.
struct SpuOwnerOperation {
    enum Kind : unsigned { Unknown, Transfer, Channel, Stop };
    Kind kind=Unknown;std::uint64_t value=0;std::uint32_t size=0;bool put=false;
};
struct PpuState;
bool spu_owner_call(PpuState&,NativeSpu::Thread&,const std::function<void()>&,SpuOwnerOperation);
struct NativeSpuWorker {
    // The coordinator may be waiting on ANY lane when another lane, memory
    // coherence or RSX queues an owner callback. A lane-local CV cannot wake
    // that wait. All such publications share this owner-only wait channel.
    struct OwnerProgress {
        std::mutex mutex;std::condition_variable changed;
        unsigned waiters=0;std::uint64_t generation=0;
        void signal(){
            std::lock_guard<std::mutex> lock(mutex);
            if(waiters){++generation;changed.notify_all();}
        }
        struct Ticket {
            OwnerProgress& progress;std::uint64_t seen;
            explicit Ticket(OwnerProgress& p):progress(p){
                std::lock_guard<std::mutex> lock(p.mutex);
                ++p.waiters;seen=p.generation;
            }
            ~Ticket(){std::lock_guard<std::mutex> lock(progress.mutex);--progress.waiters;}
            void wait(){std::unique_lock<std::mutex> lock(progress.mutex);
                progress.changed.wait(lock,[&]{return progress.generation!=seen;});}
            Ticket(const Ticket&)=delete;
        };
    };
    // RSX teardown can outlive this worker member in Process destruction.
    // Its wake callback pins the channel, without borrowing the worker.
    std::shared_ptr<OwnerProgress> owner_progress=std::make_shared<OwnerProgress>();
    std::mutex mutex;std::condition_variable changed;std::thread thread;
    std::atomic<bool> stop{false};
    // Producers publish before wake/ack. Only the coordinator consumes attention.
    std::atomic<bool> attention{false},failed{false};
    std::atomic<unsigned> owner_pending{0};
    bool refresh_pending=true;unsigned refresh_ticks=0,pool_budget=1024;
    std::uint64_t observed_completed=0;
    std::atomic<std::uint64_t> pool_completed{0};
    bool enabled=false,active=false,last_progress=false;
    bool pool_mode=false,refreshing=false;NativeSpuWorker* parent=nullptr;
    std::uint32_t group_id=0,lane_thread_id=0;
    std::atomic<bool> scheduled{true};
    NativeSpu::Thread* context=nullptr;NativeSpu::Group* group=nullptr;
    inline static thread_local NativeSpuWorker* current=nullptr;
    ExecutionTiming::ClockProbe coordinator_clock;
    struct RpcMetrics {std::uint64_t calls=0,pause_calls=0,paused_lanes=0,pause_ns=0,service_ns=0;};
    std::map<std::pair<unsigned,std::uint64_t>,RpcMetrics> rpc_metrics;
    mutable std::map<std::pair<unsigned,std::uint64_t>,RpcMetrics> rpc_reported;
    mutable std::optional<std::chrono::steady_clock::time_point> rpc_report_at;
    std::uint64_t refresh_count=0;
    void perf_report(std::chrono::steady_clock::time_point now)const{
        const double seconds=rpc_report_at?std::chrono::duration<double>(now-*rpc_report_at).count():0;
        for(const auto& item:rpc_metrics){const auto& m=item.second;auto& old=rpc_reported[item.first];
            std::fprintf(stderr,"PERF SPU_RPC kind=%u op=%llu calls=%llu per_s=%.3f total=%llu pause_calls=%llu paused_lanes=%llu pause_ns=%llu service_ns=%llu\n",
                item.first.first,(unsigned long long)item.first.second,(unsigned long long)(m.calls-old.calls),
                seconds>0?(m.calls-old.calls)/seconds:0.,(unsigned long long)m.calls,
                (unsigned long long)(m.pause_calls-old.pause_calls),(unsigned long long)(m.paused_lanes-old.paused_lanes),
                (unsigned long long)(m.pause_ns-old.pause_ns),(unsigned long long)(m.service_ns-old.service_ns));old=m;}
        rpc_report_at=now;coordinator_clock.report("spu_coordinator");
        std::fprintf(stderr,"PERF SPU_REFRESH total=%llu\n",(unsigned long long)refresh_count);
    }
    struct OwnerRequest {bool refresh_needed=true,typed=false;std::function<void()> invoke;std::function<void(std::exception_ptr)> failed;bool done=false;std::exception_ptr error;std::condition_variable ready;};
    std::deque<std::shared_ptr<OwnerRequest>> owner_requests;
    std::vector<std::unique_ptr<NativeSpuWorker>> lanes;
    std::function<void()> refresh,detach;
    void signal_coordinator(){(parent?parent->owner_progress:owner_progress)->signal();}
    template<class Predicate> void wait_servicing(std::unique_lock<std::mutex>& lock,Predicate ready){
        while(!ready()){
            // Arm before the last pump/predicate check. No queue/lane lock is
            // held during service or CV sleep, including nested owner calls.
            OwnerProgress::Ticket ticket(*(parent?parent->owner_progress:owner_progress));
            lock.unlock();try{service();}catch(...){lock.lock();throw;}lock.lock();
            if(!ready()){lock.unlock();ticket.wait();lock.lock();}
        }
    }
    bool alive()const{if(!pool_mode)return thread.joinable();for(const auto& lane:lanes)if(lane->thread.joinable())return true;return enabled && !stop;}
    void owner_call(const std::function<void()>& invoke,bool refresh_needed=true,bool typed=false){
        if(!parent){invoke();return;}
        auto request=std::make_shared<OwnerRequest>();request->invoke=invoke;request->refresh_needed=refresh_needed;request->typed=typed;
        const auto request_pc=context?context->pc:0,request_tid=context?context->id:0;
        request->failed=[this,request_pc,request_tid](std::exception_ptr e){if(parent->failure)parent->failure(e,request_pc,request_tid);};
        // Publish the request before acknowledging a safe point. Otherwise
        // group destruction could observe !active, miss the not-yet-enqueued
        // command, and join a worker waiting for an owner that already left.
        std::unique_lock<std::mutex> execution_lock(mutex);
        if(stop)throw SpuControlEnd{};
        const auto context_generation=context?context->control_generation:0;
        std::unique_lock<std::mutex> queue_lock(parent->mutex);
        parent->owner_requests.push_back(request);parent->owner_pending.fetch_add(1,std::memory_order_release);parent->attention.store(true,std::memory_order_release);++parent->generation;parent->changed.notify_all();
        active=false;++generation;changed.notify_all();signal_coordinator();execution_lock.unlock();
        request->ready.wait(queue_lock,[&]{return request->done || parent->stop;});
        const auto e=request->error;const bool done=request->done;queue_lock.unlock();
        {std::unique_lock<std::mutex> lock(mutex);changed.wait(lock,[&]{return stop || !pauses;});if(!stop)active=true;}
        // A parked RPC frame is a state handoff, not an instruction boundary.
        // A restarted/destroyed context must unwind before its old AOT suffix
        // can overwrite the new PC/registers or retire the old block prefix.
        const bool replaced=context && context->control_generation!=context_generation;
        if(e){try{std::rethrow_exception(e);}catch(const SpuBlocked&){if(replaced)throw SpuControlEnd{};throw;}catch(const SpuSliceEnd&){if(replaced)throw SpuControlEnd{};throw;}}
        if(replaced)throw SpuControlEnd{};
        if(!done)throw std::runtime_error("SPU owner request cancelled");
        if(context && (stop || !scheduled.load() || context->stopped || (group && group->state!=6)))context->block_control_exit=true;
    }
    void drain_owner(){
        if(!pool_mode || !owner_pending.load(std::memory_order_acquire))return;
        for(;;){std::shared_ptr<OwnerRequest> request;
            {std::lock_guard<std::mutex> lock(mutex);if(owner_requests.empty())return;request=owner_requests.front();owner_requests.pop_front();owner_pending.fetch_sub(1,std::memory_order_release);refresh_pending|=request->refresh_needed;}
            auto* metrics=coordinator_clock.enabled && !request->typed?&rpc_metrics[{0,0}]:nullptr;
            std::chrono::steady_clock::time_point start{};
            if(metrics){++metrics->calls;start=coordinator_clock.now();}
            try{request->invoke();}
            catch(const SpuBlocked&){request->error=std::current_exception();}
            catch(const SpuSliceEnd&){request->error=std::current_exception();}
            catch(const SpuControlEnd&){request->error=std::current_exception();}
            catch(...){request->error=std::current_exception();if(request->failed)request->failed(request->error);}
            if(metrics)metrics->service_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(coordinator_clock.now()-start).count();
            {std::lock_guard<std::mutex> lock(mutex);request->done=true;}request->ready.notify_all();
        }
    }
    unsigned pauses=0,tickets=0,budget=1024,request_budget=1024;
    // A pool lane free-runs between coordinator ticks, so the PPU tick budget (spu_run(s,1024) per 256 PPU
    // steps) is no reason to slice it every 1024 instructions: each slice costs the lane mutex, the
    // coordinator signal and (with ANYPS3_PERF_REPORT) two clock reads. Pause/ack latency is one slice
    // (4096 instructions is tens of microseconds at ~10 ns/instruction). Explicit smaller requests stay
    // exact for stepping tests; a request of at least the tick budget selects the standard lane slice.
    static constexpr unsigned pool_tick_budget=1024,pool_lane_quantum=4096;
    static unsigned lane_budget(unsigned requested){return requested<pool_tick_budget?requested:pool_lane_quantum;}
    std::uint64_t generation=0,wake_generation=0;
    std::atomic<std::uint64_t> completed{0},worker_ns{0};
    std::exception_ptr error;
    std::uint32_t guest_pc=0,guest_thread=0;
    std::function<bool(unsigned)> step;
    // Written only by this lane, consumed after step returns on the same TID.
    bool event_driven=false;
    std::chrono::steady_clock::time_point wait_deadline=std::chrono::steady_clock::time_point::max();
    std::unique_ptr<GuestMemory::ReservationWatch> reservation_watch;
    void watch_reservation(GuestMemory& memory,std::uintptr_t pointer){
        if(reservation_watch && reservation_watch->pointer==pointer)return;
        reservation_watch=std::make_unique<GuestMemory::ReservationWatch>(memory.parallel->notifications,pointer,[this]{wake();});
    }
    std::function<void()> pump,cancel;
    std::function<void(std::exception_ptr,std::uint32_t,std::uint32_t)> failure;
    // Only coordinator accesses this recursion flag. A pumped request has its
    // worker blocked on the request's CV, not executing SPU/guest data.
    bool servicing=false;
    ~NativeSpuWorker(){shutdown();}
    void failed_context(std::uint32_t pc,std::uint32_t tid){std::lock_guard<std::mutex> lock(mutex);guest_pc=pc;guest_thread=tid;}
    void check(){if(!failed.load(std::memory_order_acquire))return;std::exception_ptr e;{std::lock_guard<std::mutex> lock(mutex);e=error;}if(e)std::rethrow_exception(e);if(pool_mode)for(auto& lane:lanes)lane->check();}
    std::pair<std::uint64_t,std::uint64_t> metrics(){
        std::uint64_t ns=worker_ns,quantums=completed;
        if(pool_mode)for(auto& lane:lanes){ns+=lane->worker_ns.load(std::memory_order_relaxed);quantums+=lane->completed.load(std::memory_order_relaxed);}
        return {ns,quantums};
    }
    void service(){drain_owner();if(pump){const bool previous=servicing;servicing=true;try{pump();}catch(...){servicing=previous;throw;}servicing=previous;}if(pool_mode && refresh && refresh_pending){refresh_pending=false;refresh();}}
    void launch(){
        enabled=true;
        thread=std::thread([this]{
            current=this;
            std::unique_lock<std::mutex> lock(mutex);
            try{
                anyps3_sample::Thread sample_profile(anyps3_sample::Role::Spu,context?&context->pc:nullptr,context?&context->id:nullptr,context?&context->steps:nullptr);
                while(!stop){
                    if(pauses && !tickets){active=false;changed.notify_all();signal_coordinator();changed.wait(lock,[&]{return stop || !pauses || tickets;});continue;}
                    if(tickets)--tickets;
                    const auto observed_wake=wake_generation;
                    active=true;const unsigned quantum=pauses?request_budget:budget;lock.unlock();
                    // Wall time of a quantum is only consumed by the ANYPS3_PERF_REPORT line (worker_ns); the
                    // lane loop reads the host clock only when that report is enabled.
                    std::chrono::steady_clock::time_point start{};if(coordinator_clock.enabled)start=std::chrono::steady_clock::now();
                    const bool progress=step(quantum);
                    const auto ns=coordinator_clock.enabled?std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count():0;
                    lock.lock();worker_ns+=ns;last_progress=progress;++completed;if(parent && progress)parent->pool_completed.fetch_add(1,std::memory_order_relaxed);++generation;active=false;if(pauses)changed.notify_all();signal_coordinator();
                    if(!progress && !tickets && !pauses){
                        const auto ready=[&]{return stop || pauses || tickets || wake_generation!=observed_wake;};
                        if(event_driven){
                            if(wait_deadline==std::chrono::steady_clock::time_point::max())changed.wait(lock,ready);
                            else changed.wait_until(lock,wait_deadline,ready);
                        }else changed.wait_for(lock,std::chrono::milliseconds(1),ready);
                    }
                }
            }catch(...){if(!lock.owns_lock())lock.lock();if(!error)error=std::current_exception();failed.store(true,std::memory_order_release);if(parent){parent->failed.store(true,std::memory_order_release);parent->attention.store(true,std::memory_order_release);}if(failure)failure(error,guest_pc,guest_thread);stop=true;active=false;tickets=0;++generation;changed.notify_all();signal_coordinator();}
        });
    }
    void wake(){std::lock_guard<std::mutex> lock(mutex);++wake_generation;++generation;changed.notify_all();signal_coordinator();}
    void wake_coordinator(){attention.store(true,std::memory_order_release);wake();if(auto* lane=current;lane && lane!=this)lane->wake();}
    void pause(){
        if(!enabled)return;
        if(pool_mode){++pauses;std::vector<NativeSpuWorker*> selected;for(auto& lane:lanes)selected.push_back(lane.get());for(auto* lane:selected)lane->pause();return;}
        std::unique_lock<std::mutex> lock(mutex);++pauses;++generation;changed.notify_all();
        try{wait_servicing(lock,[&]{return !active && !tickets;});}
        catch(...){--pauses;++generation;changed.notify_all();throw;}
    }
    void resume(){
        if(!enabled)return;
        if(pool_mode){for(auto& lane:lanes)lane->resume();--pauses;return;}
        std::lock_guard<std::mutex> lock(mutex);if(!pauses)std::terminate();--pauses;++wake_generation;++generation;changed.notify_all();
    }
    EngineStepReply request(unsigned quantum){
        if(pool_mode){
            check(); // A terminal error survives consumption of attention.
            // No lane locks, supervisor sequence or clock read on an empty tick.
            // Rotation is sampled every 64 ticks; blocking/control paths refresh
            // explicitly, so absence of PPU instructions cannot defer admission.
            if(++refresh_ticks==64){refresh_ticks=0;refresh_pending=true;}
            if(attention.load(std::memory_order_acquire) || refresh_pending){attention.exchange(false,std::memory_order_acq_rel);service();check();}
            if(quantum && quantum!=pool_budget){pool_budget=quantum;
                for(auto& lane:lanes){std::lock_guard<std::mutex> lock(lane->mutex);lane->budget=lane_budget(quantum);}}
            const auto completed_now=pool_completed.load(std::memory_order_relaxed);
            const bool progress=completed_now!=observed_completed;observed_completed=completed_now;
            return {progress,false};}
        check();std::unique_lock<std::mutex> lock(mutex);if(stop){lock.unlock();check();throw std::runtime_error("SPU worker already stopped");}request_budget=quantum;
        if(!pauses){if(quantum)budget=quantum;++generation;changed.notify_all();return {last_progress,false};}
        // A synchronous control path may lend the acknowledged state back for
        // exactly one quantum. It does not access SPU/events until re-ack.
        const auto target=completed+1;++tickets;++generation;changed.notify_all();
        wait_servicing(lock,[&]{return stop || (completed>=target && !active && !tickets);});
        const bool progress=last_progress;lock.unlock();check();return {progress,true};
    }
    void idle_wait(){
        std::unique_lock<std::mutex> lock(mutex);const auto seen=generation;
        lock.unlock();refresh_pending=true;service();lock.lock();
        changed.wait_for(lock,std::chrono::milliseconds(1),[&]{return stop || generation!=seen;});lock.unlock();check();
    }
    void shutdown()noexcept{
        if(pool_mode){
            if(stop && lanes.empty())return;
            try{pause();service();}catch(...){}
            for(auto& lane:lanes)lane->shutdown();
            // Keep failed lane exception_ptrs available to the next entry.
            for(auto& lane:lanes){if(lane->error && !error){error=lane->error;failed.store(true,std::memory_order_release);}worker_ns+=lane->worker_ns;completed+=lane->completed;}
            if(detach)detach();
            lanes.clear();stop=true;if(cancel)cancel();return;
        }
        if(!thread.joinable())return;
        // Finish the bounded quantum while servicing outstanding coherence.
        // Normal shutdown must not turn an in-flight DMA into a fake error.
        try{pause();}catch(...){/* cancellation below wakes every waiter */}
        {std::lock_guard<std::mutex> lock(mutex);stop=true;++generation;changed.notify_all();}
        if(cancel)cancel();
        thread.join();reservation_watch.reset();
    }
    NativeSpuWorker()=default;
    NativeSpuWorker(const NativeSpuWorker&)=delete;
};
'''

CPP = r'''
static void rsx_worker_start(PpuState&);
static void rsx_worker_pump(PpuState&);
static EngineStepReply rsx_worker_request(PpuState&,unsigned);
static bool spu_run_inline(PpuState&,unsigned);
static bool spu_run_thread(PpuState&,NativeSpu::Group&,unsigned,unsigned);
struct SpuOwnerScope {
    NativeSpuWorker& worker;std::unique_ptr<GuestMemory::RawPermit> raw;bool schedule_dirty;
    std::vector<NativeSpuWorker*> selected;
    std::vector<std::shared_ptr<std::recursive_mutex>> metadata_pins;
    std::vector<std::unique_lock<std::recursive_mutex>> metadata_locks;
    explicit SpuOwnerScope(PpuState& s,std::uint32_t target=0,bool thread_target=false,bool raw_needed=true,bool dirty=true):worker(s.process.spu_worker),schedule_dirty(dirty){
        if(!worker.enabled)return;
        try{
            {GuestMemory::RawYield yield(s.memory);
                if(worker.pool_mode){std::vector<NativeSpuWorker*> matching;for(auto& lane:worker.lanes)if(lane->group_id && (!target || (thread_target?lane->lane_thread_id==target:lane->group_id==target)))matching.push_back(lane.get());for(auto* lane:matching){lane->pause();selected.push_back(lane);}}
                else worker.pause();
                if(worker.pool_mode)worker.service();}
            worker.check();
            // Ack before metadata locking: a lane can briefly hold its group
            // lock to pin an event target, but never waits on owner under it.
            for(auto& group:s.spu.groups){bool match=!target || (!thread_target && group.id==target);
                if(thread_target)for(const auto& t:group.threads)match|=t.id==target;
                if(match){metadata_pins.push_back(group.metadata_mutex);metadata_locks.emplace_back(*metadata_pins.back());}
            }
            if(raw_needed)raw=std::make_unique<GuestMemory::RawPermit>(s.memory);
            // A control operation may request an engine tick before its scope
            // unwinds (group start). Mark dirty before lending it the state.
            if(worker.pool_mode && schedule_dirty)worker.refresh_pending=true;
        }catch(...){if(worker.pool_mode){for(auto* lane:selected)lane->resume();}else worker.resume();throw;}
    }
    ~SpuOwnerScope(){if(worker.enabled){if(schedule_dirty)worker.refresh_pending=true;raw.reset();metadata_locks.clear();metadata_pins.clear();if(worker.pool_mode){for(auto* lane:selected)lane->resume();}else worker.resume();}}
    SpuOwnerScope(const SpuOwnerScope&)=delete;
};
// Legacy raw HLE/RSX access excludes only guest-memory readers. Private SPU
// execution continues. Any wait that needs DMA uses RawYield before waiting.
struct SpuServiceScope {
    std::unique_ptr<SpuOwnerScope> legacy;
    std::unique_ptr<GuestMemory::RawPermit> raw;
    explicit SpuServiceScope(PpuState& s,bool atomic_only=false){auto& w=s.process.spu_worker;if(!w.enabled){if(s.process.rsx_worker.enabled && !atomic_only)raw=std::make_unique<GuestMemory::RawPermit>(s.memory);return;}
        if(!w.pool_mode){legacy=std::make_unique<SpuOwnerScope>(s,0,false,!atomic_only);return;}
        {GuestMemory::RawYield yield(s.memory);w.service();w.check();}
        if(!atomic_only)raw=std::make_unique<GuestMemory::RawPermit>(s.memory);
    }
};
// Audited syscall families use validated atomic memory, with no retained raw
// pointers. Unmigrated HLE/RSX retains the explicit compatibility permit.
[[maybe_unused]] static bool spu_atomic_syscall(std::uint64_t op){
    // Timers mutate only coordinator wait state; module-load SPU ticks have
    // their own admission. Neither timer retains a raw guest pointer.
    return op==141 || op==142 || (op>=90 && op<=95) || (op>=100 && op<=109) ||
           op==128 || op==129 || op==130 || op==131 || (op>=133 && op<=138) ||
           op==156 || op==158 || op==165 || op==166 || (op>=170 && op<=194) || op==251 || op==252;
}

bool spu_owner_call(PpuState& s,NativeSpu::Thread& t,const std::function<void()>& invoke,SpuOwnerOperation operation){
    auto* lane=NativeSpuWorker::current;if(!lane || !lane->parent)return false;
    // Registry traversal is owner-only. The blocked caller pins its context
    // and group until completion, including the old generation unwind.
    auto* group=lane->group;
    if(!group || lane->context!=&t)throw std::runtime_error("SPU owner RPC lacks pinned lane context");
    const auto generation=t.control_generation;
    const bool control=operation.kind==SpuOwnerOperation::Unknown || operation.kind==SpuOwnerOperation::Stop;
    lane->owner_call([&,group,generation,operation]{
        auto& worker=s.process.spu_worker;auto& clock=worker.coordinator_clock;
        NativeSpu::Thread* target=nullptr;bool scoped=false;
        if(operation.kind==SpuOwnerOperation::Transfer && operation.value>=0xf0000000u && operation.value<=UINT32_MAX){
            const auto index=(operation.value-0xf0000000u)/0x100000,offset=(operation.value-0xf0000000u)%0x100000;
            for(auto& peer:group->threads)if(peer.number==index){
                if(offset<0x40000 && operation.size<=0x40000-offset){target=&peer;scoped=true;}
                else if(operation.put && operation.size==4 && (offset==0x5400c || offset==0x5c00c))scoped=true;
                break;
            }
        }
        // Channel 30 fallback uses coordinator registry lookup and caller's
        // mailbox; queue send and inbox publication use their existing locks.
        if(operation.kind==SpuOwnerOperation::Channel && operation.value==30)scoped=true;
        const auto key=std::make_pair(unsigned(operation.kind),operation.kind==SpuOwnerOperation::Transfer?
            (scoped?(target?std::uint64_t(operation.put):UINT64_C(2)+((operation.value%0x100000)==0x5c00c)):UINT64_C(4)):
            operation.kind==SpuOwnerOperation::Channel?(operation.value<<8)|(operation.size>>24):operation.value);
        auto* metrics=clock.enabled?&worker.rpc_metrics[key]:nullptr;
        std::chrono::steady_clock::time_point begin{};
        if(metrics){++metrics->calls;begin=clock.now();}
        auto run=[&]{if(t.control_generation!=generation)throw SpuControlEnd{};invoke();};
        try{
            if(!scoped){
                // STOP/unknown and invalid DMA preserve the previous full-group
                // handoff, including group receive, exit and join transitions.
                SpuOwnerScope owner(s,group->id,false,false);
                if(metrics){++metrics->pause_calls;metrics->paused_lanes+=owner.selected.size();
                    metrics->pause_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(clock.now()-begin).count();}
                run();
            }else{
                // The caller is already inactive on its RPC CV. Only a peer LS
                // DMA needs a second lane's instruction-boundary acknowledgement.
                std::unique_ptr<SpuOwnerScope> peer;
                if(target && target!=&t){peer=std::make_unique<SpuOwnerScope>(s,target->id,true,false,false);
                    if(metrics){++metrics->pause_calls;metrics->paused_lanes+=peer->selected.size();
                        metrics->pause_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(clock.now()-begin).count();}}
                std::lock_guard<std::recursive_mutex> metadata(*group->metadata_mutex);
                run();
            }
        }catch(...){if(metrics)metrics->service_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(clock.now()-begin).count();throw;}
        if(metrics)metrics->service_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(clock.now()-begin).count();
    },control,true);return true;
}
bool spu_owner_call(PpuState& s,NativeSpu::Thread& t,const std::function<void()>& invoke){
    return spu_owner_call(s,t,invoke,{});
}
static void spu_pool_refresh(PpuState& s){
    auto& w=s.process.spu_worker;if(!w.pool_mode || w.refreshing)return;
    GuestMemory::RawYield yield(s.memory);
    struct RefreshGuard {bool& active;explicit RefreshGuard(bool& a):active(a){active=true;}~RefreshGuard(){active=false;}} refreshing(w.refreshing);
    ++w.refresh_count;
    const auto now=w.coordinator_clock.now();
    if(now-s.spu.schedule_at>=std::chrono::milliseconds(10)){
        for(auto& g:s.spu.groups)if(g.state==6 && g.scheduled)g.schedule_order=++s.spu.schedule_order;
        s.spu.schedule_at=now;
    }
    std::vector<NativeSpu::Group*> ready;
    for(auto& g:s.spu.groups){g.scheduled=false;if(g.state==6)ready.push_back(&g);}
    std::stable_sort(ready.begin(),ready.end(),[](const auto* a,const auto* b){
        const auto ap=a->type==0x18?0:a->priority,bp=b->type==0x18?0:b->priority;
        return ap!=bp?ap<bp:a->schedule_order<b->schedule_order;
    });
    unsigned slots=s.spu_group_limit;
    for(auto* g:ready)if(g->threads.size()<=slots){g->scheduled=true;slots-=g->threads.size();}
    // Revoke and acknowledge old hardware slots before admitting replacements.
    // A basic block can otherwise overlap a newly admitted group for a whole
    // quantum. RPC pumping during ack may itself call refresh; the guard above
    // keeps that reentrancy from scheduling a second set of replacements.
    std::vector<NativeSpuWorker*> preempted;
    for(auto& lane:w.lanes){bool scheduled=false;for(auto& g:s.spu.groups)if(g.id==lane->group_id)scheduled=g.scheduled;
        if(lane->scheduled.load() && !scheduled){lane->scheduled=false;lane->pause();preempted.push_back(lane.get());}}
    for(auto& lane:w.lanes){bool scheduled=false;for(auto& g:s.spu.groups)if(g.id==lane->group_id)scheduled=g.scheduled && g.state==6;
        if(lane->scheduled.load()!=scheduled){lane->scheduled.store(scheduled);lane->wake();}}
    for(auto* lane:preempted)lane->resume();
    for(auto& g:s.spu.groups){
        if(g.state!=6 && g.state!=3 && g.state!=4 && g.state!=5)continue;
        for(unsigned i=0;i<g.threads.size();++i){auto& t=g.threads[i];
            if(std::any_of(w.lanes.begin(),w.lanes.end(),[&](const auto& lane){return lane->lane_thread_id==t.id;}))continue;
            auto lane=std::make_unique<NativeSpuWorker>();lane->parent=&w;lane->group_id=g.id;lane->guest_thread=t.id;lane->lane_thread_id=t.id;lane->scheduled=g.scheduled;lane->pauses=w.pauses;lane->budget=NativeSpuWorker::lane_budget(w.pool_budget);lane->context=&t;lane->group=&g;
            auto facade=std::make_shared<PpuState>(s.process);auto* group=&g;
            lane->step=[facade,group,i](unsigned budget){try{const auto progress=spu_run_thread(*facade,*group,i,budget);if(group->state!=6 || !NativeSpuWorker::current->scheduled.load())facade->memory.release_reader_slot();return progress;}catch(const SpuControlEnd&){return false;}catch(...){auto& t=group->threads[i];NativeSpuWorker::current->failed_context(t.pc,t.id);throw;}};
            auto* wake_lane=lane.get();t.in_mailbox.set_wakeup([wake_lane]{wake_lane->wake();});for(auto& snr:t.snr)snr.set_wakeup([wake_lane]{wake_lane->wake();});
            lane->pump=[&w]{w.service();};lane->failure=w.failure;lane->launch();w.lanes.push_back(std::move(lane));
        }
    }
}
static void spu_worker_start(PpuState& s){
    auto& worker=s.process.spu_worker;
    if(worker.enabled){
        if(worker.alive())return;
        // anyps3_entry runs once per resident module_start before the game
        // entry; each returned session joined its worker cleanly. A recorded
        // worker failure stays a stop; otherwise the next entry relaunches.
        worker.check();
        {std::lock_guard<std::mutex> lock(worker.mutex);worker.stop=false;worker.active=false;worker.pauses=0;worker.tickets=0;++worker.generation;}
        s.memory.restart_coherence();
    }else{
        const char* value=std::getenv("ANYPS3_SPU_WORKER");const char* threads=std::getenv("ANYPS3_SPU_THREADS");
        if((!value || std::string(value)!="1") && (!threads || std::string(threads)!="1"))return;
        if(!s.memory.concurrent())s.memory.enable_concurrency();
    }
    auto facade=std::make_shared<PpuState>(s.process);
    worker.step=[facade](unsigned budget){return spu_run_inline(*facade,budget);};
    worker.pump=[&s]{rsx_worker_pump(s);s.memory.service_coherence();};
    // In pool mode the coordinator waits on the paused lane's own condition
    // (NativeSpuWorker::pause per lane). A lane that queues owner coherence
    // from its step must wake that condition too, otherwise both wait forever
    // (build 324t: anyps3_perf_report -> SpuOwnerScope -> pause vs. two lanes in
    // enqueue_coherence). The lane does not hold its own mutex inside step().
    s.memory.parallel->wake_owner=[&worker]{worker.wake_coordinator();};
    worker.cancel=[memory=&s.memory]{memory->stop_coherence();memory->parallel->owner_coherence={};memory->parallel->wake_owner={};};
    worker.failure=[&s,&worker](std::exception_ptr error,std::uint32_t pc,std::uint32_t tid){
        // Publish provenance before making the error visible to the PPU.
        // Its engine-step catch must not win record() with a fallback PC/TID.
        s.supervisor.record(ExecutionEngine::Spu,s.supervisor.next_sequence(),pc,tid,error);
        worker.failed.store(true,std::memory_order_release);worker.attention.store(true,std::memory_order_release);
        if(worker.pool_mode){std::lock_guard<std::mutex> lock(worker.mutex);if(!worker.error)worker.error=error;worker.failed.store(true,std::memory_order_release);worker.attention.store(true,std::memory_order_release);++worker.generation;worker.changed.notify_all();}
    };
    s.memory.parallel->owner_coherence=[&worker,memory=&s.memory](const std::function<void()>& invoke){
        if(worker.servicing || !worker.thread.joinable()){invoke();return;}
        {GuestMemory::RawYield yield(*memory);worker.pause();}
        try{worker.check();invoke();}catch(...){worker.resume();throw;}worker.resume();
    };
    const char* threads=std::getenv("ANYPS3_SPU_THREADS");
    if(threads && std::string(threads)=="1"){
        worker.enabled=true;worker.pool_mode=true;worker.refresh=[&s]{spu_pool_refresh(s);};
        worker.detach=[&s]{for(auto& group:s.spu.groups)for(auto& t:group.threads){t.in_mailbox.set_wakeup({});for(auto& snr:t.snr)snr.set_wakeup({});}};
        s.memory.parallel->owner_coherence=[](const std::function<void()>& invoke){invoke();};
        worker.refresh();
    }else worker.launch();
}
struct SpuWorkerSession {
    PpuState& state;
    explicit SpuWorkerSession(PpuState& s):state(s){
        try{rsx_worker_start(s);spu_worker_start(s);}catch(...){if(s.process.rsx_worker.stop)s.process.rsx_worker.stop();throw;}
    }
    ~SpuWorkerSession(){state.process.spu_worker.shutdown();if(state.process.rsx_worker.stop)state.process.rsx_worker.stop();}
    void finish(){auto& worker=state.process.spu_worker;if(worker.enabled){worker.shutdown();worker.check();}auto& rsx=state.process.rsx_worker;if(rsx.stop)rsx.stop();if(rsx.queue)rsx.queue->check();}
};
static EngineStepReply spu_worker_request(PpuState& s,unsigned budget){
    auto& worker=s.process.spu_worker;
    GuestMemory::RawYield yield(s.memory);
    try{return worker.request(budget);}catch(...){
        s.supervisor.record(ExecutionEngine::Spu,0,0,0,std::current_exception());throw;
    }
}
'''


def compose_execution(source):
    """Only the real process facade has worker state; serial unit mocks don't."""
    return source.replace('    auto& supervisor=s.supervisor;', '''    if((s.process.spu_worker.enabled || s.process.rsx_worker.enabled) && request.engine==ExecutionEngine::Audio && !s.audio.running){
        s.supervisor.next_sequence();audio_run_inline(s);return {false,true};
    }
    // Running audio mixes only once its period tick is due (audio_advance); the
    // entry loop and scheduler poll far more often. Skip the engine wrapper
    // (pump/service/scopes) until then; SPU and RSX keep their own polling.
    if((s.process.spu_worker.enabled || s.process.rsx_worker.enabled) && request.engine==ExecutionEngine::Audio && timebase_ticks()<s.audio.next_tick)
        return {false,true};
    if(s.process.spu_worker.enabled && request.engine==ExecutionEngine::Spu)return spu_worker_request(s,request.budget);
    if(s.process.rsx_worker.enabled && request.engine==ExecutionEngine::Rsx)return rsx_worker_request(s,request.budget);
    rsx_worker_pump(s);
    SpuServiceScope owner(s,s.process.rsx_worker.enabled);
    auto& supervisor=s.supervisor;''').replace('    ExecutionScope scope(s.execution,phase);', '''    ExecutionScope scope(s.execution,phase);
    HostAccessOrigin engine_origin;engine_origin.engine=request.engine==ExecutionEngine::Rsx?HostAccessOrigin::Engine::Rsx:HostAccessOrigin::Engine::Host;
    HostAccessContext access_context(engine_origin);''')
