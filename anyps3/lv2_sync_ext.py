"""LV2 synchronization extension; ABI from RPCS3 ffba2bf09ef39806.

Wait kinds 9..12 use the native cooperative scheduler. Pending results remain
owned by the syscall until it is retried at the original guest PC.
"""

TYPES = r'''
struct Lv2SyncExt {
    struct Flag {std::uint32_t id,protocol,type,handles=1;std::uint64_t key,pattern;};
    struct Rwlock {std::uint32_t id,protocol,handles=1,writer=0,readers=0;std::uint64_t key;};
    struct Wait {
        unsigned kind;std::uint32_t id,priority;std::uint64_t sequence,deadline,bits=0,mode=0,output=0;
        bool done=false;std::uint32_t status=0;std::uint64_t pattern=0;
    };
    std::vector<Flag> flags;
    std::vector<Rwlock> rwlocks;
    std::map<std::uint32_t,Wait> waits;
    std::map<std::uint32_t,std::uint32_t> flag_ids,rwlock_ids;
    std::uint32_t next_flag=0x98000000,next_rwlock=0x88000000;
    std::uint64_t sequence=0;
    std::int64_t rtc_offset=0;
};
'''

CPP = r'''
static std::uint32_t sync_ext_result(PpuState& s) {
    const auto it=s.sync_ext.waits.find(s.thread_id);
    if(it==s.sync_ext.waits.end() || !it->second.done)throw std::runtime_error("sync wait resumed without a result");
    const auto w=it->second;
    if(w.kind==9 && w.output)s.memory.store(w.output,8,w.pattern);
    s.sync_ext.waits.erase(it);return w.status;
}
static void sync_ext_arm(PpuState& s,unsigned kind,std::uint32_t id,std::uint64_t timeout,
                         std::uint64_t bits=0,std::uint64_t mode=0,std::uint64_t output=0) {
    const auto now=sync_ext_now();
    if(timeout>UINT64_MAX-now)throw std::runtime_error("sync timeout exceeds host clock range");
    s.sync_ext.waits.emplace(s.thread_id,Lv2SyncExt::Wait{kind,id,s.priority,++s.sync_ext.sequence,timeout?now+timeout:0,bits,mode,output});
}
[[noreturn]] static void sync_ext_wait(PpuState& s,unsigned kind,std::uint32_t id,std::uint64_t timeout,
                                      std::uint64_t bits=0,std::uint64_t mode=0,std::uint64_t output=0) {
    sync_ext_arm(s,kind,id,timeout,bits,mode,output);ppu_block(s,kind,id);
}
static std::uint32_t sync_ext_object(PpuState& s,bool flag) {
    auto& r=s.r;auto& m=s.memory;auto& x=s.sync_ext;
    if(!r[3] || !r[4])return 0x8001000d;
    m.access(r[4],32,false);m.access(r[3],4,true);
    const auto protocol=m.load(r[4],4),shared=m.load(r[4]+4,4),key=shared==0x100?m.load(r[4]+8,8):0;
    const auto policy=m.load(r[4]+16,4),type=m.load(r[4]+20,4);
    if((protocol!=1 && protocol!=2) || (shared!=0x100 && shared!=0x200))return MM_EINVAL;
    if(flag && type!=0x10000 && type!=0x20000)return MM_EINVAL;
    if(shared==0x100 && (!key || policy<1 || policy>3))return MM_EINVAL;
    if(flag){
        for(auto& f:x.flags)if(key && f.key==key){
            if(policy==1)return MM_EEXIST;
            const auto id=x.next_flag;x.next_flag+=0x100;x.flag_ids.emplace(id,f.id);
            ++f.handles;m.store(r[3],4,id);return 0;
        }
    }else{
        for(auto& l:x.rwlocks)if(key && l.key==key){
            if(policy==1)return MM_EEXIST;
            const auto id=x.next_rwlock;x.next_rwlock+=0x100;x.rwlock_ids.emplace(id,l.id);
            ++l.handles;m.store(r[3],4,id);return 0;
        }
    }
    if(shared==0x100 && policy==2)return MM_ESRCH;
    const auto id=flag?x.next_flag:x.next_rwlock;
    if(flag){x.next_flag+=0x100;x.flags.push_back({id,static_cast<std::uint32_t>(protocol),static_cast<std::uint32_t>(type),1,key,r[5]});}
    else{x.next_rwlock+=0x100;x.rwlocks.push_back({id,static_cast<std::uint32_t>(protocol),1,0,0,key});}
    (flag?x.flag_ids:x.rwlock_ids).emplace(id,id);
    m.store(r[3],4,id);return 0;
}
static std::uint32_t sync_ext_flag(PpuState& s) {
    auto& r=s.r;auto& x=s.sync_ext;auto& m=s.memory;
    if(r[11]==82)return sync_ext_object(s,true);
    if(r[11]==85 && x.waits.count(s.thread_id))return sync_ext_result(s);
    const auto mode=static_cast<std::uint32_t>(r[5]);
    if(r[11]==85 || r[11]==86){
        if(r[6])m.store(r[6],8,0);
        if((mode&15)!=1 && (mode&15)!=2)return MM_EINVAL;
        if((mode&~15u)!=0 && (mode&~15u)!=16 && (mode&~15u)!=32)return MM_EINVAL;
    }
    if(r[11]==132 && r[4])m.store(r[4],4,0);
    const auto handle=x.flag_ids.find(static_cast<std::uint32_t>(r[3]));
    const auto object=handle==x.flag_ids.end()?0:handle->second;
    auto it=std::find_if(x.flags.begin(),x.flags.end(),[&](const auto& f){return f.id==object;});
    if(it==x.flags.end()){
        if(r[11]==139 && r[4])m.store(r[4],8,0);
        return MM_ESRCH;
    }
    auto& f=*it;
    if(r[11]==83){
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==9 && pair.second.id==f.id)return MM_EBUSY;
        x.flag_ids.erase(handle);
        if(!--f.handles)x.flags.erase(it);
        return 0;
    }
    if(r[11]==85 || r[11]==86){
        const auto pattern=f.pattern;
        if(sync_ext_match(f.pattern,r[4],mode)){
            if(r[6])m.store(r[6],8,pattern);
            return 0;
        }
        if(r[11]==86)return MM_EBUSY;
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==9 && pair.second.id==f.id && f.type==0x10000)return MM_EPERM;
        sync_ext_wait(s,9,f.id,r[7],r[4],mode,r[6]);
    }
    if(r[11]==87){f.pattern|=r[4];sync_ext_poll(s);return 0;}
    if(r[11]==118){f.pattern&=r[4];return 0;}
    if(r[11]==132){
        unsigned count=0;
        for(auto& pair:x.waits)if(!pair.second.done && pair.second.kind==9 && pair.second.id==f.id){
            pair.second.pattern=f.pattern;sync_ext_finish(s,pair.first,pair.second,0x80010013);++count;
        }
        if(r[4])m.store(r[4],4,count);
        return 0;
    }
    if(!r[4])return 0x8001000d;
    m.store(r[4],8,f.pattern);return 0;
}
static std::uint32_t sync_ext_rwlock(PpuState& s) {
    auto& r=s.r;auto& x=s.sync_ext;
    if(r[11]==120)return sync_ext_object(s,false);
    if((r[11]==122 || r[11]==125) && x.waits.count(s.thread_id))return sync_ext_result(s);
    const auto handle=x.rwlock_ids.find(static_cast<std::uint32_t>(r[3]));
    const auto object=handle==x.rwlock_ids.end()?0:handle->second;
    auto it=std::find_if(x.rwlocks.begin(),x.rwlocks.end(),[&](const auto& l){return l.id==object;});
    if(it==x.rwlocks.end())return MM_ESRCH;
    auto& l=*it;
    if(r[11]==121){
        if(l.writer || l.readers)return MM_EBUSY;
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.id==l.id)return MM_EBUSY;
        x.rwlock_ids.erase(handle);
        if(!--l.handles)x.rwlocks.erase(it);
        return 0;
    }
    if(r[11]==124){if(!l.readers)return MM_EPERM;--l.readers;sync_ext_poll(s);return 0;}
    if(r[11]==127){if(l.writer!=s.thread_id)return MM_EPERM;l.writer=0;sync_ext_poll(s);return 0;}
    const bool write=r[11]==125 || r[11]==126;
    if(write && l.writer==s.thread_id)return 0x80010008;
    bool pending_writer=false;
    for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==11 && pair.second.id==l.id)pending_writer=true;
    if(!l.writer && (!write || !l.readers) && !pending_writer){
        if(write)l.writer=s.thread_id;else ++l.readers;
        return 0;
    }
    if(r[11]==123 || r[11]==126)return MM_EBUSY;
    sync_ext_wait(s,write?11:10,l.id,r[4]);
}
static std::uint32_t sync_ext_misc(PpuState& s) {
    auto& r=s.r;auto& m=s.memory;
    switch(r[11]){
    case 18:return 0; // This process has no guest parent.
    case 43:s.reschedule=true;return 0;
    case 45:{
        PpuContext* target=r[3]==s.thread_id?&s:nullptr;
        if(!target)for(auto& t:s.ppu_threads)if(t.thread_id==r[3]){target=&t;break;}
        if(!target)return MM_ESRCH;
        if(target->joiner)return MM_EBUSY;
        if(!target->joinable)return target->returned?MM_ESRCH:MM_EINVAL;
        if(target->returned){
            s.ppu_threads.erase(std::remove_if(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& t){return t.thread_id==r[3];}),s.ppu_threads.end());
            return 0;
        }
        target->joinable=false;return 0;
    }
    case 49:{
        if(!r[3])return 0x8001000d;
        const auto base=s.stack_base?s.stack_base:UINT64_C(0xd0000000);
        for(const auto& region:m.regions)if(region.base==base){
            m.access(r[3],8,true);m.store(r[3],4,base);m.store(r[3]+4,4,region.bytes->size());return 0;
        }
        throw std::runtime_error("thread stack mapping is absent");
    }
    case 110:{
        const auto cond=std::find_if(s.kernel.conditions.begin(),s.kernel.conditions.end(),[&](const auto& c){return c.id==r[3] && c.handles;});
        if(cond==s.kernel.conditions.end())return MM_ESRCH;
        PpuContext* target=r[4]==s.thread_id?&s:nullptr;
        if(!target)for(auto& t:s.ppu_threads)if(t.thread_id==r[4] && !t.returned){target=&t;break;}
        if(!target)return MM_ESRCH;
        if(target->wait_kind!=4 || target->wait_id!=cond->id)return MM_EPERM;
        target->wait_kind=0;target->resume_cond=cond->id;s.reschedule=true;return 0;
    }
    case 140:{
        if(!r[4])return MM_EINVAL;
        auto p=std::find_if(s.events.ports.begin(),s.events.ports.end(),[&](const auto& port){return port.id==r[3];});
        auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& queue){return queue.key==r[4];});
        if(p==s.events.ports.end() || q==s.events.queues.end())return MM_ESRCH;
        if(p->type!=3)return MM_EINVAL;
        if(p->queue)return 0x80010015;
        p->queue=q->id;return 0;
    }
    case 142:
        if(s.sync_ext.waits.count(s.thread_id))return sync_ext_result(s);
        if(!static_cast<std::uint32_t>(r[3])){s.reschedule=true;return 0;}
        sync_ext_arm(s,12,0,std::uint64_t(static_cast<std::uint32_t>(r[3]))*1000000);
        ppu_defer_block(s,12,0);return 0;
    case 143:return 0x80010003; // ROOT-only, game process is unprivileged.
    case 351:return mmapper_page_attribute(s,static_cast<std::uint32_t>(r[3]),static_cast<std::uint32_t>(r[4]));
    case 362:return mmapper_syscall(s);
    case 402:return 0x8001002b; // No debug console: RPCS3 sys_tty.cpp:23-26.
    case 866:{
        // Firmware 3.01 librtc +0x3ec..0x430 converts seconds since 2000
        // to microseconds since year 1, adding 63082281600000000.
        const auto unix_seconds=std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        if(r[3]==0x3002){
            if(r[4]>1)return 0x80010500;
            if(!r[5] || !r[6])return 0x8001000d;
            m.access(r[5],8,true);m.access(r[6],8,true);
            m.store(r[5],8,unix_seconds-946684800+s.sync_ext.rtc_offset);m.store(r[6],8,0);return 0;
        }
        if(r[3]==0x3003){
            if(r[4]>3155759999ULL)throw std::runtime_error("secure RTC set outside firmware 2000..2099 range");
            s.sync_ext.rtc_offset=static_cast<std::int64_t>(r[4])-(unix_seconds-946684800);return 0;
        }
        throw std::runtime_error("unsupported secure RTC command");
    }
    default:throw std::runtime_error("unsupported sync extension syscall");
    }
}
[[maybe_unused]] static bool syscall_sync_ext(PpuState& state) {
    switch(state.r[11]){
    case 82:case 83:case 85:case 86:case 87:case 118:case 132:case 139:
        state.r[3]=sync_ext_flag(state);return true;
    case 120:case 121:case 122:case 123:case 124:case 125:case 126:case 127:
        state.r[3]=sync_ext_rwlock(state);return true;
    case 18:case 43:case 45:case 49:case 110:case 140:case 142:case 143:case 351:case 362:case 402:case 866:
        state.r[3]=sync_ext_misc(state);return true;
    default:return false;
    }
}
'''
