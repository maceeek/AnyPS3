"""Native SPU image metadata services; execution requires separate AOT code."""

TYPES = r'''
template<class T> struct SpuPublished {
    std::atomic<T> value;
    SpuPublished(T v=0):value(v){}
    SpuPublished(const SpuPublished& o):value(o.value.load()){}
    SpuPublished& operator=(const SpuPublished& o){value.store(o.value.load());return *this;}
    SpuPublished& operator=(T v){value.store(v);return *this;}
    operator T()const{return value.load();}
};
// Bounded mailbox: payload, capacity and pop are one mutex transaction.
// The mutex/CV protocol is intentionally conventional; no separate count can
// publish an uninitialized slot. Push overwrites slot four as LV2 specifies.
struct SpuInbox {
    struct State {std::mutex mutex;std::vector<std::uint32_t> values;std::function<void()> wake;std::atomic<bool> event{false};};
    std::shared_ptr<State> state=std::make_shared<State>();
    SpuInbox()=default;
    SpuInbox(const SpuInbox& other){std::lock_guard<std::mutex> lock(other.state->mutex);state->values=other.state->values;}
    SpuInbox& operator=(const SpuInbox& other){if(this!=&other){SpuInbox next(other);state=std::move(next.state);}return *this;}
    SpuInbox(SpuInbox&&)=default;
    SpuInbox& operator=(SpuInbox&&)=default;
    SpuInbox& operator=(std::initializer_list<std::uint32_t> values){std::lock_guard<std::mutex> lock(state->mutex);if(state->values.empty() && values.size())state->event.store(true,std::memory_order_release);state->values=values;if(state->wake)state->wake();return *this;}
    bool empty()const{std::lock_guard<std::mutex> lock(state->mutex);return state->values.empty();}
    unsigned size()const{std::lock_guard<std::mutex> lock(state->mutex);return state->values.size();}
    void clear(){std::lock_guard<std::mutex> lock(state->mutex);state->values.clear();state->event.store(false,std::memory_order_release);}
    std::uint32_t operator[](unsigned i)const{std::lock_guard<std::mutex> lock(state->mutex);return state->values.at(i);}
    bool operator==(const std::vector<std::uint32_t>& values)const{std::lock_guard<std::mutex> lock(state->mutex);return state->values==values;}
    bool pop(std::uint32_t& value){std::lock_guard<std::mutex> lock(state->mutex);if(state->values.empty())return false;value=state->values.front();state->values.erase(state->values.begin());return true;}
    void push_back(std::uint32_t value){std::lock_guard<std::mutex> lock(state->mutex);if(state->values.size()>=4)throw std::runtime_error("SPU inbound mailbox capacity exceeded");if(state->values.empty())state->event.store(true,std::memory_order_release);state->values.push_back(value);if(state->wake)state->wake();}
    void push_overwrite(std::uint32_t value){std::lock_guard<std::mutex> lock(state->mutex);if(state->values.empty())state->event.store(true,std::memory_order_release);if(state->values.size()<4)state->values.push_back(value);else state->values.back()=value;if(state->wake)state->wake();}
    bool reserve_reply(){std::lock_guard<std::mutex> lock(state->mutex);
        if(!state->values.empty()){state->values={0x8001000a};if(state->wake)state->wake();return false;}
        state->values.push_back(0);state->event.store(true,std::memory_order_release);return true;
    }
    void finish_reply(std::uint32_t value){std::lock_guard<std::mutex> lock(state->mutex);
        if(state->values.empty())throw std::runtime_error("SPU reply reservation lost");
        state->values.front()=value;if(state->wake)state->wake();
    }
    void set_wakeup(std::function<void()> wake){std::lock_guard<std::mutex> lock(state->mutex);state->wake=std::move(wake);}
};
// Value + notification count share one atomic word, including zero-valued
// notifications and OR mode. Consumer clears both with one exchange.
struct SpuSignal {
    struct State {std::atomic<std::uint64_t> data{0};std::atomic<bool> event{false};std::mutex mutex;std::function<void()> wake;};
    std::shared_ptr<State> state=std::make_shared<State>();
    SpuSignal()=default;
    SpuSignal(const SpuSignal& other){state->data.store(other.state->data.load());}
    SpuSignal& operator=(const SpuSignal& other){if(this!=&other){auto next=std::make_shared<State>();next->data.store(other.state->data.load());state=std::move(next);}return *this;}
    SpuSignal(SpuSignal&&)=default;SpuSignal& operator=(SpuSignal&&)=default;
    bool ready()const{return (state->data.load(std::memory_order_acquire)>>32)!=0;}
    void push(std::uint32_t value,bool merge){
        const auto data=UINT64_C(0x100000000)|value;
        const auto before=merge?state->data.fetch_or(data,std::memory_order_acq_rel):state->data.exchange(data,std::memory_order_acq_rel);
        if(!(before>>32))state->event.store(true,std::memory_order_release);
        std::lock_guard<std::mutex> lock(state->mutex);if(state->wake)state->wake();
    }
    bool pop(std::uint32_t& value){const auto data=state->data.exchange(0,std::memory_order_acq_rel);value=data;return (data>>32)!=0;}
    void clear(){state->data.store(0,std::memory_order_release);state->event.store(false,std::memory_order_release);}
    void set_wakeup(std::function<void()> wake){std::lock_guard<std::mutex> lock(state->mutex);state->wake=std::move(wake);}
};
using SpuRegister=std::array<std::uint8_t,16>;
struct SpuBlocked {};
struct SpuSliceEnd {};
struct SpuControlEnd {};
struct PpuState;
struct NativeSpu {
    struct Segment {std::uint32_t type,ls,size,address;};
    struct Thread {
        // Superblock dispatch hint (pc -> translated function, relocation delta).
        // Purely a lookup cache: every native entry still validates LS bytes.
        struct DispatchEntry {std::uint32_t pc=UINT32_MAX,delta=0;void(*fn)(PpuState&,Thread&,std::uint8_t*)=nullptr;};
        static constexpr unsigned dispatch_slots=4096;
        std::vector<DispatchEntry> dispatch_cache;
        bool block_decline=false,block_exact=true;
        // Next retired-instruction position at which a superblock entry samples interrupts.
        std::uint64_t irq_next=0;
        std::uint32_t id=0,number=0,entry=0,options=0;
        std::string name; std::array<std::uint64_t,4> arguments{};
        std::array<std::uint32_t,64> ports{};
        std::array<std::shared_ptr<EventQueueState>,64> event_targets{};
        std::array<std::pair<std::uint32_t,std::uint32_t>,32> queue_bindings{};
        std::shared_ptr<std::vector<std::uint8_t>> initial_ls;
        bool stopped=false,exit_status_ready=false,waiting_receive=false;
        bool interrupts_enabled=false,srr0_valid=false,srr0_pending=false;
        std::uint32_t srr0=0,srr0_write=0;
        std::uint32_t decrementer=0;std::uint64_t decrementer_tick=0;
        bool decrementer_running=false;
        std::uint32_t exit_status=0,receive_key=0;
        std::array<SpuRegister,128> gpr{};
        SpuRegister fpscr{};
        std::uint32_t pc=0,image_index=UINT32_MAX;std::uint64_t steps=0;
        SpuPublished<std::uint64_t> published_steps{0};
        // Internal AOT dispatch context; each spu_step defaults to one instruction.
        unsigned block_budget=1;std::uint64_t block_limit=UINT64_MAX;bool block_trace=false,block_control_exit=false;
        bool dynamic_miss=false;
        // Relocation is a hint only: generated word/run guards revalidate LS.
        std::uint32_t reloc_delta=0;
        // Translation identity is independent of the thread's boot image.
        std::uint32_t reloc_image_index=UINT32_MAX;
        SpuPublished<std::uint64_t> relocated_entries{0},interpreted_instructions{0};
        // ANYPS3_SPU_STATS=1 diagnostic counters (single writer: the lane; see spu_stat).
        std::array<SpuPublished<std::uint64_t>,28> stats{};
        // ANYPS3_SPU_STATS: interpreter entry PCs (pc<<32|count), open addressing, lane-written.
        std::shared_ptr<std::array<std::atomic<std::uint64_t>,1024>> interp_entries;
        std::shared_ptr<std::array<std::atomic<std::uint64_t>,1024>> native_entries; // ANYPS3_SPU_STATS: dispatcher entry PCs (pc+1)<<32|count
        struct DmaWrite {unsigned base,end;std::uint64_t ea;};
        std::vector<DmaWrite> dma_writes;
        std::set<std::string> dynamic_blocks;
        // Last logged bytes and overlapping provenance per 64-byte LS cell: skips re-hashing unchanged cells.
        struct DynamicSeen {bool valid=false;std::string mode;std::array<std::uint8_t,64> bytes{};std::vector<DmaWrite> origins;};
        std::unordered_map<unsigned,DynamicSeen> dynamic_seen;
        std::uint64_t control_generation=0;
        std::array<std::uint32_t,5> dma{};std::uint32_t tag_mask=0,tag_status=0;
        // MFC list stall-and-notify (CBEA v1.02 9.1.4; RPCS3 ffba2bf0 SPUThread.cpp do_list_transfer): the rest of a
        // stalled list per tag, stall mask/status (channel 25) and a pending ANY/ALL tag update (mode 1/2).
        struct StalledList {bool active=false,put=false;std::uint64_t high=0;std::uint32_t next=0,end=0,local=0;};
        std::array<StalledList,32> stalled_lists{};std::uint32_t stall_mask=0,stall_stat=0,stalled_active=0 /* bit per stalled_lists[tag].active */;bool stall_stat_ready=false;
        unsigned tag_update_pending=0;
        bool tag_status_ready=false;
        std::uint32_t out_mailbox=0;
        bool out_mailbox_full=false;
        SpuInbox in_mailbox;
        std::array<SpuSignal,2> snr;
        std::pair<std::uintptr_t,std::uint64_t> reservation{};
        std::uint64_t reservation_address=UINT64_MAX;
        // Reservation-lost filter for concurrent memory (spu_reservation_intact): change counters sampled
        // before the last successful token compare, the physical granule and memory they belong to.
        std::array<std::uint64_t,3> reservation_observed{};std::uintptr_t reservation_observed_pointer=0;
        std::uint64_t reservation_observed_memory=0;bool reservation_observed_valid=false;
        // Pool-lane busy-poll detector (spu_poll_note in spu.py). enabled is set only while a pool lane runs
        // this thread; parked/parked_kind describe the SpuBlocked that lane just caught.
        struct PollState {
            enum Kind : std::uint32_t {None,Getllar,EventCount};
            struct Site {Kind kind=None;std::uint64_t address=0;std::uint32_t pc=0;bool valid=false;
                std::array<SpuRegister,128> regs{};SpuRegister fpscr{};};
            bool enabled=false,parked=false,resume_pass=false,spinning=false;Kind parked_kind=None;
            std::chrono::steady_clock::time_point spin_start{};
            unsigned count=0,parks=0,window=0,next_site=0;std::uint64_t total_parks=0;
            std::array<Site,2> sites;
        } idle_poll;
        std::uint32_t atomic_status=0;bool atomic_status_ready=false;
        std::uint32_t event_mask=0,event_status=0;
        bool event_ready=false,waiting_event=false;
        std::array<std::uint32_t,2> signals{};std::array<bool,2> signal_ready{};
        SpuPublished<unsigned> signal_config{0};unsigned waiting_signal=0;
        std::vector<Segment> segments;
    };
    struct Group {
        std::uint32_t id=0,count=0,priority=0;SpuPublished<std::uint32_t> state{0};
        std::uint32_t type=0,memory_charge=0;
        std::uint32_t run_queue=0,exception_queue=0;
        std::uint32_t joiner=0,join_cause=0,exit_status=0;
        std::uint32_t receive_thread=UINT32_MAX;
        bool scheduled=false;std::uint64_t schedule_order=0;
        std::string name;std::vector<Thread> threads;std::vector<std::uint8_t> context;
        std::shared_ptr<std::recursive_mutex> metadata_mutex=std::make_shared<std::recursive_mutex>();
    };
    // Nodes never move when another group is erased. deque's middle erase
    // would invalidate the Group pointers borrowed by unrelated host workers.
    struct Groups:std::list<Group> {
        using Base=std::list<Group>;using Base::Base;using Base::operator=;
        Group& operator[](std::size_t index){auto it=this->begin();std::advance(it,index);return *it;}
        const Group& operator[](std::size_t index)const{auto it=this->begin();std::advance(it,index);return *it;}
    };
    Groups groups;std::uint32_t next_group=0x04000100;
    std::uint64_t schedule_order=0;std::chrono::steady_clock::time_point schedule_at{};
};
using SpuNativeFunction=void(*)(PpuState&,NativeSpu::Thread&,std::uint8_t*);
struct SpuNativeRange {std::uint32_t lo,hi;const std::uint8_t* code;SpuNativeFunction fn;const std::uint8_t* leaders;};
// ANYPS3_SPU_STATS=1: diagnostic counters (spu_stat), read by the lane worker and the PERF report.
inline const bool spu_stats_on=[](){const char* v=std::getenv("ANYPS3_SPU_STATS");return v && v[0]=='1' && !v[1];}();
'''

CPP = r'''
[[maybe_unused]] static void spu_image_import(PpuState& s) {
    auto& m=s.memory; auto& r=s.r;
    const auto output=r[3],source=r[4],mode=r[5];
    if(mode>1){r[3]=MM_EINVAL;return;}
    if(!mode)throw std::runtime_error("protected SPU image import is not implemented");
    m.validate(output,16,true);m.validate(source,52,false);
    if(m.load(source,4)!=0x7f454c46 || m.load(source+4,1)!=1 ||
       m.load(source+5,1)!=2 || m.load(source+6,1)!=1 ||
       m.load(source+18,2)!=23 || m.load(source+20,4)!=1 ||
       m.load(source+40,2)!=52 || m.load(source+42,2)!=32){r[3]=0x80010007;return;}
    const auto entry=m.load(source+24,4),phoff=m.load(source+28,4),count=m.load(source+44,2);
    if(!count || count>128 || phoff<52 || entry>=0x40000 || entry%4){r[3]=0x80010007;return;}
    m.validate(source+phoff,count*32,false);
    struct Segment {std::uint32_t type,ls,size,address;};
    std::vector<Segment> segments;
    for(unsigned i=0;i<count;++i){
        const auto h=source+phoff+i*32;
        const auto type=m.load(h,4),offset=m.load(h+4,4),ls=m.load(h+8,4),
            filesz=m.load(h+16,4),memsz=m.load(h+20,4);
        if(type==1){
            if(filesz>memsz || ls>0x40000 || memsz>0x40000-ls){r[3]=0x80010007;return;}
            if(filesz){
                m.validate(source+offset,filesz,false);
                segments.push_back({1,static_cast<std::uint32_t>(ls),static_cast<std::uint32_t>(filesz),static_cast<std::uint32_t>(source+offset)});
            }
            if(memsz>filesz)segments.push_back({2,static_cast<std::uint32_t>(ls+filesz),static_cast<std::uint32_t>(memsz-filesz),0});
        }else if(type==4){
            if(filesz<0x34){r[3]=0x80010007;return;}
            m.validate(source+offset,filesz,false);
            segments.push_back({4,0,0x20,static_cast<std::uint32_t>(source+offset+0x14)});
        }else {r[3]=0x80010007;return;}
    }
    if(segments.empty()){r[3]=0x80010007;return;}
    const auto table=m.allocate(segments.size()*24,8);
    for(unsigned i=0;i<segments.size();++i){
        const auto address=table+i*24;const auto& seg=segments[i];
        m.store(address,4,seg.type);m.store(address+4,4,seg.ls);
        m.store(address+8,4,seg.size);m.store(address+16,4,seg.address);
    }
    m.store(output,4,0);m.store(output+4,4,entry);
    m.store(output+8,4,table);m.store(output+12,4,segments.size());
    std::fprintf(stderr,"SPU image imported entry=0x%05" PRIx64 " segments=%zu source=0x%08" PRIx64 "\n",entry,segments.size(),source);
    r[3]=0;
}

static std::string spu_object_name(GuestMemory& m,std::uint64_t address,unsigned size) {
    std::string result;
    for(unsigned i=0;i+1<size;++i){const auto c=m.load(address+i,1);if(!c)break;result+=static_cast<char>(c);}
    return result;
}

static std::uint32_t spu_syscall(PpuState& s) {
    const auto op=s.r[11];const bool per_thread=op==184 || op==187 || op==188;
    std::unique_ptr<SpuOwnerScope> control;
    if(!(s.process.spu_worker.pool_mode && (op==170 || op==184 || op==187 || op==188)))
        control=std::make_unique<SpuOwnerScope>(s,std::uint32_t(s.r[op==172?4:3]),per_thread,false);
    auto& m=s.memory;auto& r=s.r;auto& groups=s.spu.groups;
    if(r[11]==170){
        const auto count=static_cast<std::uint32_t>(r[4]),priority=static_cast<std::uint32_t>(r[5]);
        const auto size=m.load(r[6],4),name=m.load(r[6]+4,4),type=m.load(r[6]+8,4);
        if(!count || count>s.spu_group_limit || size>128)return MM_EINVAL;
        if(type!=0 && type!=0x18)throw std::runtime_error("SPU group type not implemented: "+std::to_string(type));
        if(!type && (priority<16 || priority>255))return MM_EINVAL;
        unsigned physical=type?count:0;for(const auto& existing:groups)if(existing.type==0x18)physical+=existing.count;
        if(physical>=s.spu_group_limit)return MM_EBUSY;
        NativeSpu::Group group;
        group.name=spu_object_name(m,name,size);group.count=count;group.priority=priority;group.type=type;
        const auto used=main_memory_used(s);
        const auto bytes=count*0x40000;
        group.memory_charge=type?0:bytes; // Exclusive non-context groups reserve physical SPUs, not main-memory save areas.
        if(used>0x10000000 || group.memory_charge>0x10000000-used)return MM_ENOMEM;
        if(groups.size()>=255)return 0x80010001;
        m.validate(r[3],4,true);group.context.resize(bytes);group.id=s.spu.next_group;
        groups.push_back(std::move(group));s.spu.next_group+=0x100;m.allocated+=groups.back().memory_charge;
        m.store(r[3],4,groups.back().id);
        std::fprintf(stderr,"SPU group created id=%08x threads=%u type=%x main_memory=%u name=%s\n",groups.back().id,count,groups.back().type,groups.back().memory_charge,groups.back().name.c_str());
        return 0;
    }
    if(r[11]==184 || r[11]==187 || r[11]==188){
        if((r[11]==184 && r[4]>1) || (r[11]==187 && r[4]>3))return MM_EINVAL;
        NativeSpu::Thread* thread=nullptr;
        for(auto& group:groups)for(auto& candidate:group.threads)if(candidate.id==static_cast<std::uint32_t>(r[3]))thread=&candidate;
        if(!thread)return MM_ESRCH;
        if(r[11]==187){thread->signal_config=r[4];return 0;}
        if(r[11]==188){m.store(r[4],8,thread->signal_config);return 0;}
        const auto number=static_cast<unsigned>(r[4]),value=static_cast<std::uint32_t>(r[5]);
        if(s.process.spu_worker.pool_mode){thread->snr[number].push(value,(thread->signal_config&(1u<<number))!=0);return 0;}
        if(!thread->signal_ready[number])thread->snr[number].state->event.store(true,std::memory_order_release);
        thread->signals[number]=(thread->signal_config&(1u<<number))?thread->signals[number]|value:value;
        thread->signal_ready[number]=true;return 0;
    }
    const auto group_id=r[11]==172?r[4]:r[3];
    auto it=std::find_if(groups.begin(),groups.end(),[&](const auto& g){return g.id==group_id;});
    if(it==groups.end())return MM_ESRCH;
    auto& group=*it;
    if(r[11]==178){
        if(group.state<1)return 0x8001000f;
        if(group.joiner && group.joiner!=s.thread_id)return MM_EBUSY;
        if(r[4])m.validate(r[4],4,true);
        if(r[5])m.validate(r[5],4,true);
        if(!group.join_cause){group.joiner=s.thread_id;ppu_block(s,5,group.id);}
        const auto cause=group.join_cause,status=group.exit_status;
        group.joiner=0;group.join_cause=0;
        if(!r[4])return 0x8001000d;
        m.store(r[4],4,cause);
        if(!r[5])return 0x8001000d;
        m.store(r[5],4,status);r[4]=cause;r[5]=status;return 0;
    }
    auto queue_exists=[&](std::uint64_t id){return std::any_of(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return q.id==id;});};
    if(r[11]==251){
        if(!r[5])return MM_EINVAL;
        if(!queue_exists(r[4]))return MM_ESRCH;
        if(!group.state)return 0x8001000f;
        for(unsigned port=0;port<64;++port){
            if(!(r[5]&(UINT64_C(1)<<port)))continue;
            bool used=false;
            for(const auto& thread:group.threads)if(queue_exists(thread.ports[port]))used=true;
            if(used)continue;
            m.validate(r[6],1,true);
            for(auto& thread:group.threads){thread.ports[port]=r[4];for(auto& q:s.events.queues)if(q.id==r[4])thread.event_targets[port]=q.data.state;}
            m.store(r[6],1,port);return 0;
        }
        return 0x80010015;
    }
    if(r[11]==252){
        if(r[4]>63)return MM_EINVAL;
        for(auto& thread:group.threads){thread.ports[r[4]]=0;thread.event_targets[r[4]].reset();}
        return 0;
    }
    if(r[11]==185){
        if(r[5]!=1 && r[5]!=2)return MM_EINVAL;
        auto& port=r[5]==1?group.run_queue:group.exception_queue;
        if(queue_exists(port))return MM_EBUSY;
        if(!queue_exists(r[4]))return MM_ESRCH;
        port=r[4];return 0;
    }
    if(r[11]==186){
        if(r[4]==1)group.run_queue=0;
        if(r[4]==2)group.exception_queue=0;
        return 0;
    }
    if(r[11]==171){
        if(group.state>1)return MM_EBUSY;
        if(s.process.spu_worker.pool_mode){
            for(auto& t:group.threads){if(t.control_generation==UINT64_MAX)throw std::runtime_error("SPU context generation overflow");++t.control_generation;}
            auto& lanes=s.process.spu_worker.lanes;
            for(auto& lane:lanes)if(lane->group_id==group.id)lane->shutdown();
            // Scope's acknowledged lane pointers must remain alive through its destruction.
            for(auto& lane:lanes)if(lane->group_id==group.id){lane->step=[](unsigned){return false;};lane->group_id=0;lane->guest_thread=0;lane->lane_thread_id=0;lane->group=nullptr;lane->context=nullptr;}
        }
        m.allocated-=group.memory_charge;groups.erase(it);return 0;
    }
    if(r[11]==172){
        if(group.state || group.threads.size()>=group.count)return MM_EBUSY;
        if(r[5]>=256)return MM_EINVAL;
        for(const auto& thread:group.threads)if(thread.number==r[5])return MM_EBUSY;
        const auto image=r[6],attr=r[7],args=r[8];
        m.validate(image,16,false);m.validate(attr,12,false);m.validate(args,32,false);
        const auto type=m.load(image,4),entry=m.load(image+4,4),table=m.load(image+8,4),count=m.load(image+12,4);
        const Lv2SpuExt::Image* kernel_image=nullptr;
        if(type==1){
            for(const auto& img:s.spu_ext.images)if(img.id==entry)kernel_image=&img;
            if(!kernel_image)return MM_ESRCH;
        }else if(type)return MM_EINVAL;
        if(!kernel_image && (entry>=0x40000 || entry%4 || !count || count>32))return MM_EINVAL;
        const auto name=m.load(attr,4),length=m.load(attr+4,4),options=m.load(attr+8,4);
        if(length>128 || options>3)return MM_EINVAL;
        NativeSpu::Thread thread;
        thread.name=spu_object_name(m,name,length);thread.entry=kernel_image?kernel_image->entry:entry;
        if(kernel_image)thread.initial_ls=kernel_image->ls;
        thread.number=r[5];thread.options=options;
        bool copied=false,info=false;
        if(!kernel_image)m.validate(table,count*24,false);
        for(unsigned i=0;!kernel_image && i<count;++i){
            const auto a=table+i*24;
            NativeSpu::Segment segment{static_cast<std::uint32_t>(m.load(a,4)),static_cast<std::uint32_t>(m.load(a+4,4)),static_cast<std::uint32_t>(m.load(a+8,4)),static_cast<std::uint32_t>(m.load(a+16,4))};
            if(segment.type==4){if(info || segment.size>256)return MM_EINVAL;info=true;}
            else {
                if(segment.type!=1 && segment.type!=2)return MM_EINVAL;
                if(!segment.size || (segment.ls|segment.size)%16 || segment.ls>=0x40000 || segment.size>0x40000-segment.ls)return MM_EINVAL;
                if(segment.type==1){if(segment.address%4)return MM_EINVAL;m.validate(segment.address,segment.size,false);copied=true;}
                for(const auto& other:thread.segments)if(other.type!=4 && segment.ls<other.ls+other.size && other.ls<segment.ls+segment.size)return MM_EINVAL;
            }
            thread.segments.push_back(segment);
        }
        if(!kernel_image && !copied)return MM_EINVAL;
        for(unsigned i=0;i<4;++i)thread.arguments[i]=m.load(args+i*8,8);
        m.validate(r[3],4,true);thread.id=(group.threads.size()<<24)|(group.id&0xffffff);
        group.threads.push_back(std::move(thread));m.store(r[3],4,group.threads.back().id);
        if(group.threads.size()==group.count)group.state=1;
        std::fprintf(stderr,"SPU thread initialized id=%08x entry=%05x name=%s\n",group.threads.back().id,group.threads.back().entry,group.threads.back().name.c_str());
        return 0;
    }
    if(r[11]==173){
        if(group.state!=1)return 0x8001000f;
        unsigned running=group.count;for(const auto& other:groups)if(other.state==6)running+=other.count;
        if(running>s.spu_group_limit && !s.process.spu_worker.pool_mode)throw std::runtime_error("SPU oversubscription requires scheduler context switching");
        for(unsigned i=0;i<group.threads.size();++i){
            auto& thread=group.threads[i];auto* ls=group.context.data()+i*0x40000;
            if(thread.control_generation==UINT64_MAX)throw std::runtime_error("SPU context generation overflow");
            ++thread.control_generation;thread.block_control_exit=false;
            std::fill_n(ls,0x40000,0);
            if(thread.initial_ls)std::copy_n(thread.initial_ls->data(),0x40000,ls);
            for(const auto& seg:thread.segments){
                if(seg.type==1)m.transfer(seg.address,ls+seg.ls,seg.size,false);
                if(seg.type==2)for(unsigned b=0;b<seg.size;++b)ls[seg.ls+b]=seg.address>>(24-(b%4)*8);
            }
            spu_prepare(s,thread,ls);
            thread.stopped=false;thread.exit_status_ready=false;thread.waiting_receive=false;
            thread.dma_writes.clear();thread.dynamic_blocks.clear();thread.dynamic_seen.clear();thread.dynamic_miss=false;thread.reloc_delta=0;
            thread.steps=0;thread.published_steps.value.store(0,std::memory_order_relaxed);thread.dma={};thread.tag_mask=0;thread.tag_status=0;thread.tag_status_ready=false;thread.stalled_lists={};thread.stall_mask=0;thread.stall_stat=0;thread.stall_stat_ready=false;thread.tag_update_pending=0;
            thread.out_mailbox_full=false;thread.in_mailbox.clear();thread.reservation_address=UINT64_MAX;
            thread.reservation_observed_valid=false;thread.idle_poll=NativeSpu::Thread::PollState{};
            thread.atomic_status_ready=false;thread.event_mask=0;thread.event_status=0;
            thread.event_ready=false;thread.waiting_event=false;thread.waiting_signal=0;
            thread.interrupts_enabled=false;thread.srr0_valid=false;thread.srr0_pending=false;
            thread.srr0=0;thread.srr0_write=0;
            thread.signals={};thread.signal_ready={};for(auto& snr:thread.snr)snr.clear();
        }
        group.join_cause=0;group.exit_status=0;group.receive_thread=UINT32_MAX;
        group.schedule_order=++s.spu.schedule_order;group.state=6;
        if(group.run_queue){
            auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& x){return x.id==group.run_queue;});
            if(q!=s.events.queues.end() && q->data.size()<q->capacity)q->data.push_back({UINT64_C(0xffffffff53505500),group.id,0,0});
        }
        spu_run(s,1024);
        return 0;
    }
    throw std::runtime_error("unimplemented SPU group operation");
}
'''
