"""Cooperative native PPU contexts sharing one process address space."""

TYPES = r'''
struct PpuBlocked {};
struct PpuContext {
    // Process-wide registry; copied contexts and newly created threads share it.
    std::shared_ptr<std::array<std::uint32_t,8>> thread_atexit=std::make_shared<std::array<std::uint32_t,8>>();
    std::uint64_t r[32]{},fpr[32]{};
    std::array<std::array<std::uint8_t,16>,32> vr{};
    // Fields the scheduler reads for every blocked thread at each preemption point sit together, so that a pass over
    // dozens of ~1.3 KB contexts touches one cache line per thread (ppu_schedule).
    std::uint64_t wait_id=0,wait_deadline=0;
    unsigned wait_kind=0;std::uint32_t thread_id=1,priority=1000;
    bool returned=false,atexit_running=false,wait_timed_out=false,lwcond_timed_out=false;
    std::uint64_t lr=RETURN_SENTINEL,ctr=0,xer=0;
    std::uint32_t cr=0,pc=0,fpscr=0,vrsave=0,vscr=0;
    bool joinable=false;std::uint32_t joiner=0;
    std::uint64_t exit_status=0,stack_base=0,tls_base=0;
    // sysPrxForUser sys_ppu_thread_exit: pending exit value and next atexit slot while callbacks run.
    std::uint64_t atexit_value=0;unsigned atexit_next=0;
    std::pair<std::uintptr_t,std::uint64_t> reservation{};
    std::uint64_t reservation_address=UINT64_MAX;unsigned reservation_size=0;
    std::uint64_t resume_cond=0;std::uint32_t wait_depth=0;
    std::uint64_t lwmutex_waiting=0,resume_lwcond=0,wait_sequence=0;
    // Timed kernel mutex wait (sys_mutex_lock timeout, steady-clock microseconds): wait_deadline/wait_timed_out above.
    std::uint32_t cancelled_event=0;
    // Lazy FR/FI/FPRF (floating.py fp_lazy): the last arithmetic operation whose
    // exact FPSCR update is still pending; fp_lazy_op==0 means none.
    std::uint64_t fp_lazy_a=0,fp_lazy_b=0,fp_lazy_c=0;std::uint8_t fp_lazy_op=0,fp_lazy_precision=0;
};
'''

CPP = r'''

// The extension state is process-wide, just like kernel mutex/semaphore state.
static std::uint64_t sync_ext_now() {
    return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
static bool sync_ext_match(std::uint64_t& pattern,std::uint64_t bits,unsigned mode) {
    if((mode&15)==1 ? (pattern&bits)!=bits : !(pattern&bits))return false;
    if(mode&16)pattern&=~bits;
    if(mode&32)pattern=0;
    return true;
}
static void sync_ext_finish(PpuState& s,std::uint32_t tid,Lv2SyncExt::Wait& w,std::uint32_t status) {
    w.done=true;w.status=status;
    if(tid==s.thread_id)s.wait_kind=0;
    for(auto& t:s.ppu_threads)if(t.thread_id==tid)t.wait_kind=0;
    s.reschedule=true;
}
// The scheduler reads the host clock at most once per pass (lazily; `cached` is 0 until read): a pass over dozens of timed
// waiters used to read it for each.
static void sync_ext_poll(PpuState& s,std::uint64_t& cached) {
    auto& x=s.sync_ext;
    // Nothing to poll: the deadline loop, the event-flag queues and the rwlock queues are all driven by x.waits entries.
    if(x.waits.empty())return;
    if(!cached)cached=sync_ext_now();
    const auto now=cached;
    bool flag_waits=false,rwlock_waits=false;
    for(auto& pair:x.waits){
        auto& w=pair.second;
        // w.priority is consumed only by the flag (kind 9) and rwlock-writer (kind 11) queue orders below.
        if(w.kind==9 || w.kind==11)for(const auto& t:s.ppu_threads)if(t.thread_id==pair.first)w.priority=t.priority;
        flag_waits|=w.kind==9;rwlock_waits|=w.kind==10 || w.kind==11;
        if(!w.done && w.deadline && now>=w.deadline){
            if(w.kind==9)for(const auto& f:x.flags)if(f.id==w.id)w.pattern=f.pattern;
            sync_ext_finish(s,pair.first,w,w.kind==12?0:0x8001000b);
        }
    }
    auto before=[](const auto& a,const auto& b,unsigned protocol){
        return protocol==2 && a.priority!=b.priority?a.priority<b.priority:a.sequence<b.sequence;
    };
    if(flag_waits)for(auto& f:x.flags){
        // Consume patterns in queue order at wake time; runnable threads cannot
        // steal a bit from a selected waiter while it reacquires the CPU.
        std::vector<std::uint32_t> queue;
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==9 && pair.second.id==f.id)queue.push_back(pair.first);
        std::sort(queue.begin(),queue.end(),[&](auto a,auto b){return before(x.waits.at(a),x.waits.at(b),f.protocol);});
        for(auto tid:queue){auto& w=x.waits.at(tid);const auto pattern=f.pattern;
            if(sync_ext_match(f.pattern,w.bits,w.mode)){w.pattern=pattern;sync_ext_finish(s,tid,w,0);}
        }
    }
    if(rwlock_waits)for(auto& l:x.rwlocks){
        if(l.writer)continue;
        std::uint32_t writer=0;
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==11 && pair.second.id==l.id)
            if(!writer || before(pair.second,x.waits.at(writer),l.protocol))writer=pair.first;
        if(writer){
            if(!l.readers){l.writer=writer;sync_ext_finish(s,writer,x.waits.at(writer),0);}
            continue;
        }
        for(auto& pair:x.waits)if(!pair.second.done && pair.second.kind==10 && pair.second.id==l.id){
            ++l.readers;sync_ext_finish(s,pair.first,pair.second,0);
        }
    }
}
static void sync_ext_poll(PpuState& s){std::uint64_t cached=0;sync_ext_poll(s,cached);}
[[noreturn]] static void ppu_block(PpuState& s,unsigned kind,std::uint64_t id) {
    s.wait_kind=kind;s.wait_id=id;throw PpuBlocked{};
}
// Audited helpers can return a blocked disposition through one AOT operation.
// Direct/serial callers keep the original exception contract. The fields belong
// to PpuCpu, not a saved PpuContext; no disposition follows a context switch.
static void ppu_defer_block(PpuState& s,unsigned kind,std::uint64_t id) {
    s.wait_kind=kind;s.wait_id=id;
    if(!s.ppu_defer_allowed)throw PpuBlocked{};
    s.ppu_block_pending=true;
}
struct PpuDeferredScope {
    PpuState& state;bool previous;std::uint32_t pc;std::uint64_t argument;
    explicit PpuDeferredScope(PpuState& s):state(s),previous(s.ppu_defer_allowed),pc(s.pc),argument(s.r[3]){
        if(s.ppu_block_pending)throw std::runtime_error("PPU operation entered with an unconsumed blocked disposition");
        s.ppu_defer_allowed=previous || s.process.spu_worker.enabled || s.process.rsx_worker.enabled;
    }
    ~PpuDeferredScope(){state.ppu_defer_allowed=previous;
        if(state.ppu_block_pending){state.pc=pc;state.r[3]=argument;}}
    PpuDeferredScope(const PpuDeferredScope&)=delete;
};
[[maybe_unused]] static void ppu_schedule(PpuState& s,bool mandatory=false) {
    // Scheduler predicates use validated scalar memory only. Each nested
    // RSX/audio engine takes its own compatibility scope; do not exclude SPU
    // DMA for the whole scheduling loop or while choosing a ready context.
    SpuServiceScope owner(s,true);
    ExecutionScope scheduling(s.execution,ExecutionPhase::Coordinator);
    if(s.module_loading)throw std::runtime_error("PPU scheduling during module load is unsupported");
    if(mandatory){s.process.spu_worker.refresh_pending=true;spu_run(s,1024);rsx_run(s,1024);}
    auto active=std::find_if(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& t){return t.thread_id==s.thread_id;});
    if(active==s.ppu_threads.end()){s.ppu_threads.push_back(static_cast<PpuContext&>(s));active=std::prev(s.ppu_threads.end());}
    else *active=static_cast<PpuContext&>(s);
    for(;;){
    audio_run(s);
    std::uint64_t cached_now=0; // host clock, read at most once per pass
    const auto clock_now=[&]{if(!cached_now)cached_now=sync_ext_now();return cached_now;};
    sync_ext_poll(s,cached_now);
    for(auto& thread:s.ppu_threads){
        const unsigned kind=thread.wait_kind;
        if(!kind)continue; // a runnable context has no readiness condition
        bool ready=false;
        // Kernel object ids are never reused (monotonic counters), so the first match is the object. An event queue is
        // tested through its lock-free event count.
        if(kind==1){for(const auto& q:s.events.queues)if(q.id==thread.wait_id){ready=q.data.has_events();break;}}
        else if(kind==2){for(const auto& sem:s.kernel.semaphores)if(sem.id==thread.wait_id){ready=sem.value!=0;break;}}
        else if(kind==3){for(const auto& mutex:s.kernel.mutexes)if(mutex.id==thread.wait_id){ready=!mutex.owner;break;}}
        // An expired timed mutex wait resumes; the retried lock reports ETIMEDOUT
        // unless the mutex became free (RPCS3 sys_mutex.cpp:247-290).
        if(!ready && (kind==1 || kind==3 || kind==7 || kind==8) &&
           thread.wait_deadline && clock_now()>=thread.wait_deadline){thread.wait_timed_out=true;ready=true;}
        if(kind==5)for(const auto& group:s.spu.groups)if(group.id==thread.wait_id && group.join_cause)ready=true;
        if(kind==6)for(const auto& target:s.ppu_threads)if(target.thread_id==thread.wait_id && target.returned)ready=true;
        if(kind==7 && s.mutexes.count(thread.wait_id) && s.memory.load(thread.wait_id,4)==UINT32_MAX)ready=true;
        if(kind==13 && std::none_of(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& other){
            return other.thread_id!=thread.thread_id && other.atexit_running;}))ready=true;
        if(ready)thread.wait_kind=0;
    }
    // Rotate equal priorities; a smaller number is a higher LV2 priority. One pass in rotation order finds the best
    // ready context (`next`, the first of the best priority) and the best of the others (`second`, same tie rule).
    // The Cell PPU has two hardware threads: LV2 runs the two highest-priority
    // ready threads at once, so a busy higher-priority thread cannot starve the
    // next one. On a preemption point (not a block) alternate between the best
    // two ready contexts, which serially approximates both hardware threads.
    PpuContext* next=nullptr;
    PpuContext* second=nullptr;
    const auto current=static_cast<std::size_t>(active-s.ppu_threads.begin());
    const auto count=s.ppu_threads.size();
    for(std::size_t offset=1,index=current;offset<=count;++offset){
        if(++index==count)index=0;
        auto& candidate=s.ppu_threads[index];
        if(candidate.wait_kind || candidate.returned)continue;
        if(!next || candidate.priority<next->priority){second=next;next=&candidate;}
        else if(!second || candidate.priority<second->priority)second=&candidate;
    }
    if(!mandatory && next && !active->wait_kind && !active->returned){
        if(second && &*active==next)next=second;
    }
    if(!next){
        if(mandatory){
            // A timed PPU sleep can leave no instruction ticks for milliseconds.
            // Preserve the SPU scheduler deadline while the coordinator waits.
            s.process.spu_worker.refresh_pending=true;
            const bool spu_progress=spu_run(s,1024),rsx_progress=rsx_run(s,1024);
            if(spu_progress || rsx_progress)continue;
            // Idle until the earliest timed wake-up (at most 1 ms, so vblank and
            // other host-clock events are still polled). Active audio retains
            // its 100 us service bound, but cannot hide an earlier PPU deadline.
            std::uint64_t earliest=UINT64_MAX;
            for(const auto& pair:s.sync_ext.waits)if(!pair.second.done && pair.second.deadline)earliest=std::min(earliest,pair.second.deadline);
            for(const auto& thread:s.ppu_threads)if(thread.wait_kind && thread.wait_deadline)earliest=std::min(earliest,thread.wait_deadline);
            const bool audio_pending=s.audio.maximum && (s.audio.running || !s.audio.notifications.empty());
            if(earliest!=UINT64_MAX || audio_pending){const auto now=sync_ext_now();
                const std::uint64_t ceiling=audio_pending?100:1000;
                const auto delay=earliest==UINT64_MAX?ceiling:earliest>now?std::min(earliest-now,ceiling):0;
                if(delay){ExecutionScope idle(s.execution,ExecutionPhase::Idle);GuestMemory::RawYield yield(s.memory);std::this_thread::sleep_for(std::chrono::microseconds(delay));}
                continue;}
            if(s.process.spu_worker.enabled && std::any_of(s.spu.groups.begin(),s.spu.groups.end(),[](const auto& g){return g.state==6 || g.state==3;})){
                ExecutionScope idle(s.execution,ExecutionPhase::Idle);{GuestMemory::RawYield yield(s.memory);s.process.spu_worker.service();s.process.spu_worker.idle_wait();}continue;
            }
            if(s.process.rsx_worker.enabled){
                ExecutionScope idle(s.execution,ExecutionPhase::RsxWait);GuestMemory::RawYield yield(s.memory);
                rsx_worker_pump(s);std::this_thread::sleep_for(std::chrono::microseconds(100));continue;
            }
            throw std::runtime_error("native scheduler has no runnable PPU/SPU context or progressing RSX command");
        }
        return;
    }
    // Per-switch trace only on request: ~2000 switches/s in a level made it a stderr hot path.
    static const bool trace_switch=[]{const char* v=std::getenv("ANYPS3_TRACE_PPU_SWITCH");return v && std::string(v)=="1";}();
    if(trace_switch && next->thread_id!=s.thread_id)std::fprintf(stderr,"PPU switch %u -> %u pc=%08x\n",s.thread_id,next->thread_id,next->pc);
    // Keeping the same context at a preemption point: the slot was just saved from `s` and the readiness pass only touches
    // blocked contexts, so copying it back is the identity. A mandatory pass may have woken the active context.
    if(!mandatory && next==&*active){s.reschedule=false;return;}
    static_cast<PpuContext&>(s)=*next;s.reschedule=false;
    return;
    }
}

#include <csignal>
// Diagnostic only: SIGUSR1 requests a dump of every PPU context's scheduling
// state at the next main-loop check (async-signal-safe flag only).
static std::atomic<bool> anyps3_dump_requested{false};
static void anyps3_dump_signal(int){anyps3_dump_requested.store(true);}
[[maybe_unused]] static const bool anyps3_dump_installed=(std::signal(SIGUSR1,anyps3_dump_signal),true);
[[maybe_unused]] static void anyps3_dump_threads(PpuState& s){
    SpuOwnerScope owner(s);
    anyps3_trace_dump();
    const auto now=sync_ext_now();
    std::fprintf(stderr,"THREAD DUMP current=%u steps=%llu threads=%zu now_us=%llu\n",s.thread_id,
        static_cast<unsigned long long>(s.steps),s.ppu_threads.size(),static_cast<unsigned long long>(now));
    auto line=[&](const PpuContext& t,const char* tag){
        std::fprintf(stderr,"THREAD %s id=%u pc=%08x lr=%08llx prio=%u wait_kind=%u wait_id=%llx deadline=%llu timed_out=%u returned=%u lwmutex_waiting=%llx resume_cond=%llx resume_lwcond=%llx\n",
            tag,t.thread_id,t.pc,static_cast<unsigned long long>(t.lr),t.priority,t.wait_kind,static_cast<unsigned long long>(t.wait_id),
            static_cast<unsigned long long>(t.wait_deadline),unsigned(t.wait_timed_out),unsigned(t.returned),
            static_cast<unsigned long long>(t.lwmutex_waiting),static_cast<unsigned long long>(t.resume_cond),static_cast<unsigned long long>(t.resume_lwcond));
    };
    line(s,"current");
    for(const auto& t:s.ppu_threads)line(t,"saved");
    for(const auto& m:s.kernel.mutexes)std::fprintf(stderr,"MUTEX id=%x owner=%u depth=%u\n",m.id,m.owner,m.depth);
    for(const auto& id:s.mutexes)std::fprintf(stderr,"LWMUTEX addr=%llx owner=%08llx waiters=%llu\n",static_cast<unsigned long long>(id),
        static_cast<unsigned long long>(s.memory.load(id,4)),static_cast<unsigned long long>(s.memory.load(id+4,4)));
    // SPU threads (owner scope above): wait reason, mailbox/tag/event state; optional LS dump.
    const char* dir=std::getenv("ANYPS3_SPU_LS_DUMP_DIR");
    for(const auto& group:s.spu.groups){
        std::fprintf(stderr,"SPU GROUP id=%x state=%u name=%s\n",group.id,unsigned(group.state.value.load()),group.name.c_str());
        for(std::size_t i=0;i<group.threads.size();++i){const auto& t=group.threads[i];
            std::fprintf(stderr,"SPU THREAD id=%x pc=%05x steps=%llu stopped=%u waiting_event=%u event_mask=%x event_status=%x waiting_signal=%u waiting_receive=%u "
                "in_mbox=%zu out_mbox_full=%u tag_mask=%x tag_status=%x reservation=%llx\n",t.id,t.pc,static_cast<unsigned long long>(t.steps),unsigned(t.stopped),
                unsigned(t.waiting_event),t.event_mask,t.event_status,t.waiting_signal,unsigned(t.waiting_receive),t.in_mailbox.state?t.in_mailbox.state->values.size():std::size_t(0),unsigned(t.out_mailbox_full),
                t.tag_mask,t.tag_status,static_cast<unsigned long long>(t.reservation_address));
            if(dir && group.context.size()>=(i+1)*0x40000){char path[1024];std::snprintf(path,sizeof(path),"%s/usr1-spu-%x-pc%05x.ls",dir,t.id,t.pc);
                if(FILE* f=std::fopen(path,"wb")){std::fwrite(group.context.data()+i*0x40000,1,0x40000,f);std::fclose(f);std::fprintf(stderr,"SPU LS dump %s\n",path);}}
        }
    }
}
[[maybe_unused]] static void ppu_thread_create(PpuState& s) {
    auto& r=s.r;auto& m=s.memory;
    const auto out=r[3],descriptor=r[4],argument=r[5],priority=r[6],flags=r[8],name=r[9];
    if(priority>3071 || flags>1){r[3]=MM_EINVAL;return;}
    if(r[7]>0x100000)throw std::runtime_error("PPU thread stack exceeds diagnostic limit");
    const auto stack_size=(std::max<std::uint64_t>(r[7],0x4000)+0xfff)&~UINT64_C(0xfff);
    s.ppu_threads.erase(std::remove_if(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& t){return t.thread_id!=s.thread_id && t.returned && !t.joinable;}),s.ppu_threads.end());
    if(s.ppu_threads.size()>=64){r[3]=0x80010001;return;}
    PpuContext thread;thread.thread_atexit=s.thread_atexit;thread.pc=m.load(descriptor,4);thread.r[2]=m.load(descriptor+4,4);
    m.access(thread.pc,4,false);m.access(out,8,true);
    std::string label;
    if(name)for(unsigned i=0;i<127;++i){const auto c=m.load(name+i,1);if(!c)break;label+=static_cast<char>(c);}
    const auto id=s.next_thread;
    const auto stack=UINT64_C(0xd0100000)+(id-2)*0x100000;
    m.map(stack,stack_size,6,nullptr,0);
    const auto tls=m.allocate(s.tls_memory_size+0x30);
    for(unsigned i=0;i<s.tls_file_size;++i)m.store(tls+0x30+i,1,m.load(s.tls_address+i,1));
    thread.r[1]=stack+stack_size-0x200;thread.r[3]=argument;thread.r[13]=tls+0x7030;
    thread.thread_id=id;thread.priority=priority;
    thread.joinable=flags==1;thread.stack_base=stack;thread.tls_base=tls;
    if(s.ppu_threads.empty())s.ppu_threads.push_back(static_cast<PpuContext&>(s));
    s.ppu_threads.push_back(thread);++s.next_thread;
    m.store(out,8,id);r[3]=0;s.reschedule=true;
    std::fprintf(stderr,"PPU thread created id=%u pc=%08x priority=%u name=%s\n",id,thread.pc,thread.priority,label.c_str());
}
// Guest return address of a native-invoked atexit callback (page 0xfffe0, beside MODULE_RETURN).
constexpr std::uint32_t ATEXIT_RETURN=0xfffe0010;
[[maybe_unused,noreturn]] static void ppu_thread_exit(PpuState& s){
    GuestMemory::ConfigurationPermit configuration(s.memory);
    // Raw sys_ppu_thread_exit (syscall 41); registered callbacks belong to the liblv2 wrapper below.
    if(s.thread_id==1)throw std::runtime_error("main PPU thread exit requires process shutdown");
    // Process-atexit callbacks are a different API and do not run here.
    for(const auto base:{s.stack_base,s.tls_base}){
        if(!base)throw std::runtime_error("exiting PPU thread lacks owned stack/TLS");
        const auto it=std::find_if(s.memory.regions.begin(),s.memory.regions.end(),[&](const auto& region){return region.base==base;});
        if(it==s.memory.regions.end())throw std::runtime_error("exiting PPU thread lost stack/TLS mapping");
    }
    for(const auto base:{s.stack_base,s.tls_base}){
        const auto it=std::find_if(s.memory.regions.begin(),s.memory.regions.end(),[&](const auto& region){return region.base==base;});
        s.memory.mark_write(it->bytes->data(),it->bytes->size());
        s.memory.allocated-=it->bytes->size();s.memory.regions.erase(it);s.memory.mapping_changed();
    }
    s.exit_status=s.r[3];s.returned=true;s.wait_kind=0;s.stack_base=0;s.tls_base=0;
    std::fprintf(stderr,"native PPU exit id=%u status=%llu joinable=%u\n",s.thread_id,static_cast<unsigned long long>(s.exit_status),unsigned(s.joinable));
    throw PpuBlocked{}; // This call never returns into the released guest stack.
}
// sysPrxForUser sys_ppu_thread_exit (RPCS3 ffba2bf0 Modules/sys_ppu_thread_.cpp:168-190): call every
// nonzero slot of the 8-entry atexit table in slot order, without arguments, then exit with the
// original value. Each callback runs as guest code on this thread and returns to ATEXIT_RETURN.
[[maybe_unused]] static void ppu_atexit_continue(PpuState& s){
    if(!s.atexit_running)throw std::runtime_error("atexit return without a pending PPU thread exit");
    while(s.atexit_next<s.thread_atexit->size()){
        const auto opd=(*s.thread_atexit)[s.atexit_next++];
        if(!opd)continue;
        s.pc=checked_target(s.memory.load(opd,4));s.r[2]=s.memory.load(std::uint64_t(opd)+4,4);s.lr=ATEXIT_RETURN;
        return;
    }
    s.atexit_running=false;s.r[3]=s.atexit_value;ppu_thread_exit(s);
}
[[maybe_unused,noreturn]] static void ppu_thread_exit_wrapper(PpuState& s){
    if(s.atexit_running)throw std::runtime_error("nested sys_ppu_thread_exit during atexit callbacks");
    // The original holds a process lwmutex around the callbacks: a concurrent exit waits for the holder
    // (wait kind 13) and retries this import once no other thread runs atexit callbacks.
    for(const auto& other:s.ppu_threads)
        if(other.thread_id!=s.thread_id && other.atexit_running)ppu_block(s,13,other.thread_id);
    s.atexit_value=s.r[3];s.atexit_next=0;s.atexit_running=true;
    ppu_atexit_continue(s);
    // Callbacks remain: leave the import through the scheduler with pc at the first callback.
    throw PpuBlocked{};
}
[[maybe_unused]] static std::uint32_t ppu_thread_join(PpuState& s){
    const auto id=static_cast<std::uint32_t>(s.r[3]);
    if(id==s.thread_id)return 0x80010008;
    auto target=std::find_if(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& t){return t.thread_id==id;});
    if(target==s.ppu_threads.end())return MM_ESRCH;
    if(!target->joinable || (target->joiner && target->joiner!=s.thread_id))return MM_EINVAL;
    if(!s.r[4])return 0x8001000d;
    s.memory.access(s.r[4],8,true);
    target->joiner=s.thread_id;
    if(!target->returned)ppu_block(s,6,id);
    s.memory.store(s.r[4],8,target->exit_status);
    s.ppu_threads.erase(target);return 0;
}
'''
