"""Guest lightweight condition variables backed by cooperative PPU waits."""

CPP = r'''
// A single admitted RMW: no raw guest pointer escapes its CPU lease. The
// same physical stripes/tracking as PPU LARX/MFC atomics preserve reservations,
// aliases and publication. Observed/protected bytes retain the exact slow API.
[[maybe_unused]] static bool lwmutex_compare64(GuestMemory& m,std::uint64_t object,std::uint64_t expected,std::uint64_t desired){
    if(!m.concurrent()){
        if(m.load(object,8)!=expected)return false;
        m.store(object,8,desired);return true;
    }
    const auto commit=[&](const GuestMemory::ParallelMemory::Mapping& map,std::uint8_t* pointer,std::uint8_t* payload){
        if(!(map.region.flags&4))throw std::runtime_error("guest memory permission fault");
        GuestMemory::ScalarLock lock(m,pointer,8);
        const auto line=(object-map.region.base)/128;
        if(!map.state->tracked[line].load(std::memory_order_acquire))m.track_reservation(m.reservation_span(object,8));
        if(GuestMemory::atomic_scalar_read(payload,8)!=expected)return false;
        m.bump_scalar(map,object,8);GuestMemory::atomic_scalar_write(payload,8,desired);m.publish_cpu_write(pointer,8);return true;
    };
    {GuestMemory::ScalarAccess access(m,object,8,true);
        if(access.map)return commit(*access.map,access.pointer,access.payload);}
    if(!m.host_access_observer){
        GuestMemory::Lease lease(m,object,8,true);
        const auto& map=m.translate_parallel(object,8,true);
        auto* pointer=map.region.bytes->data()+object-map.region.base;
        return commit(map,pointer,m.byte_pointer(map.region.bytes,pointer));
    }
    // Exact read + write coherence is required on watched bytes. No pending
    // GPU result may be discarded by treating this RMW as a pure overwrite.
    std::pair<std::uintptr_t,std::uint64_t> token;
    if(m.load_reserved(object,8,token)!=expected)return false;
    return m.store_conditional(object,8,desired,token);
}
// Firmware 3.01 liblv2 0x2800cb5c (lwarx/stwcx + isync),
// 0x2800d09c..d0c4 (lwsync, ldarx/stdcx on owner|waiters).
// The local atomic RMW never overwrites the caller's architectural reservation.
[[maybe_unused]] static bool lwmutex_fast(PpuState& s,bool unlock){
    const auto object=s.r[3];auto& m=s.memory;
    if((object&7) || s.lwmutex_waiting || !s.mutexes.count(object))return false;
    // Keep recursive and error/contended paths in the existing implementation.
    if(unlock && m.load(object+12,4))return false;
    // Check/commit both words together. With no waiters this is equivalent
    // to firmware owner-word acquisition, using the existing aligned 64-bit
    // atomic commit (the waiter word remains zero).
    const auto owned=std::uint64_t(s.thread_id)<<32,free=UINT64_C(0xffffffff00000000);
    if(!lwmutex_compare64(m,object,unlock?owned:free,unlock?free:owned))return false;
    if(!unlock){s.wait_deadline=0;s.wait_timed_out=false;}
    s.r[3]=0;return true;
}
[[maybe_unused]] static void lwmutex_import(PpuState& s,bool unlock){
    if(!lwsync_trace_enabled() && lwmutex_fast(s,unlock))return;
    static thread_local HleMetric lock_metric("fallback","sys_lwmutex_lock");
    static thread_local HleMetric unlock_metric("fallback","sys_lwmutex_unlock");
    HleImportScope owner(s,unlock?&unlock_metric:&lock_metric,false);
    const auto object=s.r[3];
    if(unlock){
        if(lwsync_trace_enabled())lwsync_trace(s,"unlock",object,std::uint32_t(object),lwsync_owner(s,std::uint32_t(object)));
        lwmutex_unlock(s);
    }else{
        if(!lwsync_trace_enabled())lwmutex_lock(s);
        else {const auto before=lwsync_owner(s,std::uint32_t(object));lwmutex_lock(s);lwsync_trace(s,"lock",object,std::uint32_t(object),before);}
    }
}

[[maybe_unused]] static std::uint32_t lwmutex_trylock(PpuState& s){
    const auto object=std::uint32_t(s.r[3]);auto& m=s.memory;
    const auto owner=m.load(object,4);
    // RPCS3 sys_lwmutex_.cpp:273, with the existing cooperative lock state.
    if(owner==0xfffffffe)return MM_EINVAL;
    if(!s.mutexes.count(object))throw std::runtime_error("unknown lightweight mutex in trylock");
    const auto attr=m.load(object+8,4),protocol=attr&15,recursive=attr&0x30;
    if((protocol!=1 && protocol!=2 && protocol!=4) || (recursive!=16 && recursive!=32))
        throw std::runtime_error("unsupported/invalid lightweight mutex attributes in trylock");
    if(owner==0xfffffffd)throw std::runtime_error("kernel reserved lightweight mutex handoff is unsupported");
    if(owner==UINT32_MAX){m.store(object,4,s.thread_id);return 0;}
    if(owner!=s.thread_id)return MM_EBUSY;
    if(recursive!=16)return 0x80010008;
    const auto depth=m.load(object+12,4);
    if(depth==UINT32_MAX)return 0x80010011;
    m.store(object+12,4,depth+1);return 0;
}
[[maybe_unused]] static std::uint32_t lwcond_service(PpuState& s,unsigned operation){
    auto& r=s.r;auto& m=s.memory;auto& conditions=s.kernel.lwconditions;
    const auto object=r[3];
    if(operation==0){
        const auto mutex=r[4],attr=r[5];
        if(!s.mutexes.count(mutex))return MM_ESRCH;
        if(conditions.count(object))return MM_EBUSY;
        const auto name=m.load(attr,8);m.access(object,8,true);
        const auto id=s.kernel.next_lwcond;
        conditions.emplace(object,KernelSync::LightweightCond{id,mutex,name});
        m.store(object,4,mutex);m.store(object+4,4,id);s.kernel.next_lwcond+=0x100;
        return 0;
    }
    auto found=conditions.find(object);
    if(found==conditions.end())return MM_ESRCH;
    const auto condition=found->second;
    if(!s.mutexes.count(condition.mutex) || m.load(object,4)!=condition.mutex || m.load(object+4,4)!=condition.id)return MM_ESRCH;
    if(operation==1){
        if(s.resume_lwcond==object)return MM_EBUSY;
        for(const auto& thread:s.ppu_threads)
            if(thread.thread_id!=s.thread_id && ((thread.wait_kind==8 && thread.wait_id==object) || thread.resume_lwcond==object))return MM_EBUSY;
        m.store(object+4,4,0xfffffffe);conditions.erase(found);return 0;
    }
    if(operation==3 || operation==4){
        PpuContext* selected=nullptr;
        const auto protocol=m.load(condition.mutex+8,4)&15;
        for(auto& thread:s.ppu_threads)if(thread.thread_id!=s.thread_id && thread.wait_kind==8 && thread.wait_id==object){
            if(operation==4){thread.wait_kind=0;thread.resume_lwcond=object;s.reschedule=true;}
            else if(!selected || (protocol!=1 && thread.priority<selected->priority) ||
                ((protocol==1 || thread.priority==selected->priority) && thread.wait_sequence<selected->wait_sequence))selected=&thread;
        }
        if(selected){selected->wait_kind=0;selected->resume_lwcond=object;s.reschedule=true;}
        return 0;
    }
    if(operation!=2)throw std::runtime_error("unknown lightweight condition operation");
    // Timed wait (r4 = microseconds): on expiry the thread reacquires the
    // lwmutex untimed and returns CELL_ETIMEDOUT (RPCS3 sys_lwcond.cpp).
    if(s.wait_timed_out && s.resume_lwcond!=object){
        s.wait_timed_out=false;s.wait_deadline=0;s.resume_lwcond=object;s.lwcond_timed_out=true;
    }
    if(s.resume_lwcond==object){
        const auto timeout=r[4];r[3]=condition.mutex;r[4]=0;
        try{lwmutex_lock(s);}catch(...){r[3]=object;r[4]=timeout;throw;}
        const auto result=static_cast<std::uint32_t>(r[3]);r[3]=object;r[4]=timeout;
        if(s.ppu_block_pending)return 0;
        if(result)return result;
        m.store(condition.mutex+12,4,s.wait_depth);s.resume_lwcond=0;s.wait_deadline=0;
        if(s.lwcond_timed_out){s.lwcond_timed_out=false;return 0x8001000b;}
        return 0;
    }
    if(m.load(condition.mutex,4)!=s.thread_id)return MM_EPERM;
    if(!r[4] && s.ppu_threads.size()<2)throw std::runtime_error("lightweight condition wait requires an active peer");
    if(r[4] && !s.wait_deadline)s.wait_deadline=sync_ext_deadline(r[4]);
    s.wait_depth=m.load(condition.mutex+12,4);s.wait_sequence=s.next_wait_sequence++;
    m.store(condition.mutex+12,4,0);m.store(condition.mutex,4,UINT32_MAX);
    s.reschedule=true;ppu_defer_block(s,8,object);return 0;
}
'''
