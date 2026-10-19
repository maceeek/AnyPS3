"""Kernel mutex, condition and semaphore objects for cooperative native startup.

Two sysutil transport channels are provisioned with real empty synchronization
objects. Supported waits release the PPU context; timed mutex locks resume with ETIMEDOUT.
IPC keys and attributes are recovered from local firmware 3.01 libsysutil.
"""
TYPES = r'''
struct KernelSync {
    struct LightweightCond {std::uint32_t id;std::uint64_t mutex,name;};
    std::map<std::uint64_t,LightweightCond> lwconditions;
    std::uint32_t next_lwcond=0x97000000;
    struct Mutex { std::uint32_t id, recursive, owner=0, depth=0, handles=0; std::uint64_t key=0; bool system=false;std::shared_ptr<std::recursive_mutex> guard=std::make_shared<std::recursive_mutex>(); };
    struct Cond { std::uint32_t id, mutex, handles=0; std::uint64_t key=0; bool system=false;std::shared_ptr<std::recursive_mutex> guard=std::make_shared<std::recursive_mutex>(); };
    struct Semaphore { std::uint32_t id,value,maximum;std::shared_ptr<std::recursive_mutex> guard=std::make_shared<std::recursive_mutex>(); };
    std::vector<Mutex> mutexes;
    std::vector<Cond> conditions;
    std::vector<Semaphore> semaphores;
    std::uint32_t next_semaphore=0x96000000;
    std::uint32_t next_mutex=0x85000300, next_cond=0x86000900;
    KernelSync() {
        for (unsigned channel=0;channel<2;++channel) {
            const auto id=0x85000000+channel*0x100;
            const auto key=UINT64_C(0x8006010000000000)|channel;
            mutexes.push_back({id,32,0,0,0,key|0x20,true});
            for (unsigned c=0;c<4;++c)
                conditions.push_back({0x86000000+(channel*4+c)*0x100,id,0,key|(0x30+c*0x10),true});
        }
        mutexes.push_back({0x85000200,32,0,0,0,UINT64_C(0x8006010000000110),true});
        conditions.push_back({0x86000800,0x85000200,0,UINT64_C(0x8006010000000111),true});
    }
};
'''
CPP = r'''
static void report_sysutil_rings(PpuState& s) {
    const auto& bytes=*s.mmapper.objects.front().bytes;
    auto word=[&](unsigned a){return (std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
    for (unsigned channel=0;channel<2;++channel) {
        const auto base=channel*0x8000;
        const auto offset=word(base), capacity=word(base+4), read=word(base+12), size=word(base+16);
        std::fprintf(stderr,"sysutil ring %u offset=%u capacity=%u queued=%u state=%u data=",channel,offset,capacity,size,word(base+20));
        if (capacity && offset<=0x8000 && capacity<=0x8000-offset && read<capacity && size<=capacity)
            for (unsigned i=0;i<std::min(size,256u);++i) std::fprintf(stderr,"%02x",bytes[base+offset+(read+i)%capacity]);
        std::fprintf(stderr,"\n");
    }
}

static std::uint32_t kernel_sync(PpuState& s) {
    auto& r=s.r; auto& k=s.kernel; auto& mem=s.memory;
    if(r[11]==90) {
        const auto initial=static_cast<std::uint32_t>(r[5]), maximum=static_cast<std::uint32_t>(r[6]);
        if(!maximum || maximum>INT32_MAX || initial>maximum)return MM_EINVAL;
        mem.validate(r[4],32,false);
        const auto protocol=mem.load(r[4],4),shared=mem.load(r[4]+4,4);
        if(protocol!=1 && protocol!=2)return MM_EINVAL;
        if(shared==0x100)throw std::runtime_error("shared semaphores are unsupported");
        if(shared!=0x200)return MM_EINVAL;
        mem.validate(r[3],4,true);const auto id=k.next_semaphore;k.next_semaphore+=0x100;
        k.semaphores.push_back({id,initial,maximum});mem.store(r[3],4,id);return 0;
    }
    if(r[11]>=91 && r[11]<=95) {
        auto it=std::find_if(k.semaphores.begin(),k.semaphores.end(),[&](const auto& sem){return sem.id==r[3];});
        if(it==k.semaphores.end())return MM_ESRCH;
        auto pin=it->guard;std::lock_guard<std::recursive_mutex> object(*pin);
        if(r[11]==91){k.semaphores.erase(it);return 0;}
        if(r[11]==92 || r[11]==93) {
            if(it->value){--it->value;return 0;}
            if(r[11]==93)return MM_EBUSY;
            if(s.ppu_threads.size()>1 && !r[4])ppu_block(s,2,it->id);
            throw std::runtime_error("semaphore wait requires an active scheduler");
        }
        if(r[11]==94) {
            const auto count=static_cast<std::uint32_t>(r[4]);
            if(!count || count>INT32_MAX)return MM_EINVAL;
            if(count>it->maximum-it->value)return MM_EBUSY;
            it->value+=count;return 0;
        }
        mem.store(r[4],4,it->value);return 0;
    }
    if (r[11]==100) {
        const auto out=r[3], attr=r[4]; mem.validate(attr,40,false);
        const auto protocol=mem.load(attr,4), recursive=mem.load(attr+4,4), shared=mem.load(attr+8,4);
        const auto adaptive=mem.load(attr+12,4), key=shared==0x100?mem.load(attr+16,8):0, flags=mem.load(attr+24,4);
        if (protocol<1 || protocol>3 || (recursive!=16 && recursive!=32) || (shared!=0x100 && shared!=0x200)) return MM_EINVAL;
        if (adaptive!=0x2000) throw std::runtime_error("unsupported adaptive kernel mutex");
        if (shared==0x100 && (!key || flags<1 || flags>3)) return MM_EINVAL;
        for (auto& m:k.mutexes) if (key && m.key==key) {
            if (flags==1) return MM_EEXIST;
            mem.validate(out,4,true); ++m.handles; mem.store(out,4,m.id); return 0;
        }
        if (shared==0x100 && flags==2) return MM_ESRCH;
        mem.validate(out,4,true); const auto id=k.next_mutex; k.next_mutex+=0x100;
        k.mutexes.push_back({id,static_cast<std::uint32_t>(recursive),0,0,1,key,false}); mem.store(out,4,id); return 0;
    }
    if (r[11]>=101 && r[11]<=105) {
        const auto id=r[11]==105?r[4]:r[3];
        auto it=std::find_if(k.mutexes.begin(),k.mutexes.end(),[&](const auto& m){return m.id==id && m.handles;});
        if (it==k.mutexes.end()) return MM_ESRCH;
        auto& m=*it;auto pin=m.guard;std::lock_guard<std::recursive_mutex> object(*pin);
        if (r[11]==101) {
            if (m.owner) return MM_EBUSY;
            for (const auto& c:k.conditions) if (c.mutex==id && c.handles) return MM_EPERM;
            if (--m.handles==0 && !m.system) k.mutexes.erase(it);
            return 0;
        }
        if (r[11]==102 || r[11]==103) {
            if (!m.owner) {m.owner=s.thread_id;s.wait_deadline=0;s.wait_timed_out=false;return 0;}
            if (m.owner!=s.thread_id) {
                if(r[11]==103)return MM_EBUSY;
                if(r[4]){ // timeout in microseconds; the deadline survives retried syscalls
                    if(s.wait_timed_out){s.wait_timed_out=false;s.wait_deadline=0;return 0x8001000b;} // CELL_ETIMEDOUT
                    if(!s.wait_deadline)s.wait_deadline=sync_ext_now()+r[4];
                }
                ppu_block(s,3,m.id);
            }
            if (m.recursive!=16) return 0x80010008;
            if (m.depth==UINT32_MAX) return 0x80010011;
            ++m.depth; return 0;
        }
        if (r[11]==104) {
            if (m.owner!=s.thread_id) return MM_EPERM;
            if (m.depth) --m.depth; else m.owner=0;
            return 0;
        }
        const auto out=r[3], attr=r[5]; mem.validate(attr,24,false);
        const auto shared=mem.load(attr,4), flags=mem.load(attr+4,4), key=shared==0x100?mem.load(attr+8,8):0;
        if (shared!=0x100 && shared!=0x200) return MM_EINVAL;
        if (shared==0x100 && (!key || flags<1 || flags>3)) return MM_EINVAL;
        for (auto& c:k.conditions) if (key && c.key==key) {
            if (flags==1) return MM_EEXIST;
            if (c.mutex!=id) throw std::runtime_error("condition opened with a different mutex");
            mem.validate(out,4,true); ++c.handles; mem.store(out,4,c.id); return 0;
        }
        if (shared==0x100 && flags==2) return MM_ESRCH;
        mem.validate(out,4,true); const auto cid=k.next_cond; k.next_cond+=0x100;
        k.conditions.push_back({cid,static_cast<std::uint32_t>(id),1,key,false}); mem.store(out,4,cid); return 0;
    }
    auto it=std::find_if(k.conditions.begin(),k.conditions.end(),[&](const auto& c){return c.id==r[3] && c.handles;});
    if (it==k.conditions.end()) return MM_ESRCH;
    auto pin=it->guard;std::unique_lock<std::recursive_mutex> object(*pin);
    switch (r[11]) {
    case 106:
        for(const auto& thread:s.ppu_threads)if(thread.wait_kind==4 && thread.wait_id==it->id)return MM_EBUSY;
        if (--it->handles==0 && !it->system) k.conditions.erase(it);
        return 0;
    case 108: case 109: {
        PpuContext* selected=nullptr;
        for(auto& thread:s.ppu_threads)if(thread.wait_kind==4 && thread.wait_id==it->id){
            if(r[11]==109){thread.wait_kind=0;thread.resume_cond=it->id;s.reschedule=true;}
            else if(!selected || thread.priority<selected->priority)selected=&thread;
        }
        if(selected){selected->wait_kind=0;selected->resume_cond=it->id;s.reschedule=true;}
        return 0;
    }
    case 107: {
        auto mutex=std::find_if(k.mutexes.begin(),k.mutexes.end(),[&](const auto& m){return m.id==it->mutex;});
        if (mutex==k.mutexes.end()) return MM_ESRCH;
        auto parent_pin=mutex->guard;std::unique_lock<std::recursive_mutex> parent(*parent_pin);
        if(s.resume_cond==it->id){
            if(mutex->owner)ppu_block(s,3,mutex->id);
            mutex->owner=s.thread_id;mutex->depth=s.wait_depth;s.resume_cond=0;return 0;
        }
        if (mutex->owner!=s.thread_id) return MM_EPERM;
        mutex->owner=0;
        bool serviced=false;
        object.unlock();parent.unlock();
        try { GuestMemory::RawPermit legacy_transport(mem);report_sysutil_rings(s); serviced=service_sysutil_connect(s,r[3]); } catch (...) { std::lock(parent,object);mutex->owner=s.thread_id; throw; }
        std::lock(parent,object);
        mutex->owner=s.thread_id;
        if (serviced) return 0;
        if(s.ppu_threads.size()>1 && !r[4]){
            s.wait_depth=mutex->depth;mutex->owner=0;mutex->depth=0;ppu_block(s,4,it->id);
        }
        throw std::runtime_error("kernel condition wait requires an active peer/scheduler");
    }
    default: throw std::runtime_error("unsupported kernel synchronization operation");
    }
}
'''
