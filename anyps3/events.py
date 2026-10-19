"""Native bounded event FIFO; absent events are never fabricated."""
TYPES = r'''
struct EventQueueState {
    using Event=std::array<std::uint64_t,4>;
    mutable std::mutex mutex;std::vector<Event> values;
    std::atomic<unsigned> capacity{0};bool exists=true;
    EventQueueState(unsigned n,std::initializer_list<Event> initial):values(initial),capacity(n){}
    std::uint32_t send(const Event& event){std::lock_guard<std::mutex> lock(mutex);
        if(!exists)return 0x80010016;
        if(values.size()>=capacity.load(std::memory_order_relaxed))return 0x8001000a;
        values.push_back(event);return 0;
    }
};
struct Events {
    // The current coordinator is the sole registry owner and FIFO consumer.
    // Lane producers pin State at bind time; they never scan this vector.
    struct Data {
        using Event=EventQueueState::Event;
        std::shared_ptr<EventQueueState> state;
        explicit Data(unsigned capacity=UINT32_MAX,std::initializer_list<Event> initial={}):state(std::make_shared<EventQueueState>(capacity,initial)){}
        std::size_t size()const{std::lock_guard<std::mutex> lock(state->mutex);return state->values.size();}
        bool empty()const{return !size();}
        Event operator[](std::size_t i)const{std::lock_guard<std::mutex> lock(state->mutex);return state->values.at(i);}
        Event front()const{return (*this)[0];}
        void reserve(std::size_t n){std::lock_guard<std::mutex> lock(state->mutex);state->values.reserve(n);}
        void clear(){std::lock_guard<std::mutex> lock(state->mutex);state->values.clear();}
        std::uint32_t send(const Event& event){return state->send(event);}
        void push_back(const Event& event){if(send(event))throw std::runtime_error("event queue append raced capacity or destruction");}
        void close()const{if(state){std::lock_guard<std::mutex> lock(state->mutex);state->exists=false;}}
        // Iteration owns an immutable snapshot, including RSX/VSH retained
        // iteration. Erase uses indices: concurrent producers only append.
        struct End {};
        struct Iterator {
            std::shared_ptr<const std::vector<Event>> snapshot;std::size_t index=0;
            const Event& operator*()const{return snapshot->at(index);}
            Iterator& operator++(){++index;return *this;}
            Iterator operator+(std::size_t n)const{return {snapshot,index+n};}
            bool operator!=(End)const{return index<snapshot->size();}
        };
        Iterator begin()const{std::lock_guard<std::mutex> lock(state->mutex);return {std::make_shared<const std::vector<Event>>(state->values),0};}
        End end()const{return {};}
        void erase(const Iterator& first,const Iterator& last){std::lock_guard<std::mutex> lock(state->mutex);
            if(first.index>last.index || last.index>state->values.size())throw std::runtime_error("event FIFO erase bounds");
            state->values.erase(state->values.begin()+first.index,state->values.begin()+last.index);
        }
        void erase(const Iterator& at){erase(at,at+1);}
    };
    struct Capacity {
        std::shared_ptr<EventQueueState> state;
        operator unsigned()const{return state->capacity.load(std::memory_order_relaxed);}
        Capacity& operator=(unsigned n){state->capacity.store(n,std::memory_order_relaxed);return *this;}
    };
    struct Queue {
        std::uint32_t id;Data data;Capacity capacity;std::uint64_t key;std::uint32_t type;
        Queue(std::uint32_t i,unsigned n,std::uint64_t k,std::initializer_list<Data::Event> initial={},std::uint32_t t=1):id(i),data(n,initial),capacity{data.state},key(k),type(t){}
        Queue(const Queue&)=default;Queue(Queue&&) noexcept=default;
        Queue& operator=(Queue&& other) noexcept{
            if(this!=&other){if(data.state!=other.data.state)data.close();id=other.id;data=std::move(other.data);capacity=std::move(other.capacity);key=other.key;type=other.type;}return *this;
        }
        Queue& operator=(const Queue& other){
            if(this!=&other){if(data.state!=other.data.state)data.close();id=other.id;data=other.data;capacity=other.capacity;key=other.key;type=other.type;}return *this;
        }
    };
    struct Queues:std::vector<Queue> {
        using Base=std::vector<Queue>;using Base::Base;using Base::operator=;
        auto erase(Base::const_iterator at){at->data.close();return Base::erase(at);}
        auto erase(Base::const_iterator first,Base::const_iterator last){for(auto it=first;it!=last;++it)it->data.close();return Base::erase(first,last);}
        void clear(){for(auto& q:*this)q.data.close();Base::clear();}
        ~Queues(){clear();}
    };
    struct Port { std::uint32_t id, type, queue=0; std::uint64_t name=0; };
    Queues queues;
    std::vector<Port> ports;
    std::uint32_t next_queue=0x8d000000, next_port=0x0e000000;
    std::vector<std::pair<std::uint64_t,std::array<std::uint64_t,4>>> deferred;
    void post(std::uint64_t key,const std::array<std::uint64_t,4>& event){deferred.push_back({key,event});flush();}
    void flush(){
        auto it=deferred.begin();
        for(;it!=deferred.end();++it){
            auto q=std::find_if(queues.begin(),queues.end(),[&](const auto& x){return x.key==it->first;});
            if(q==queues.end())throw std::runtime_error("native event target queue is absent");
            const auto result=q->data.send(it->second);
            if(result==0x8001000a)break;
            if(result)throw std::runtime_error("native event queue was destroyed");
        }
        deferred.erase(deferred.begin(),it);
    }
};
'''
CPP = r'''
static std::uint32_t event_syscall(PpuState& s) {
    auto& r=s.r; auto& e=s.events; auto& mem=s.memory;
    if (r[11]==128) {
        const auto protocol=mem.load(r[4],4), type=mem.load(r[4]+4,4);
        if (!r[6] || r[6]>127 || (protocol!=1 && protocol!=2) || (type!=1 && type!=2)) return MM_EINVAL;
        for (const auto& q:e.queues) if (r[5] && q.key==r[5]) return MM_EEXIST;
        mem.validate(r[3],4,true); if(e.next_queue>UINT32_MAX-0x100)throw std::runtime_error("event queue ID exhausted");const auto id=e.next_queue; e.next_queue+=0x100;
        e.queues.push_back({id,static_cast<std::uint32_t>(r[6]),r[5],{},static_cast<std::uint32_t>(type)}); mem.store(r[3],4,id); return 0;
    }
    if(r[11]==130 && s.cancelled_event==r[3]){s.cancelled_event=0;s.wait_deadline=0;s.wait_timed_out=false;return 0x80010013;}
    if (r[11]>=129 && r[11]<=133) {
        auto it=std::find_if(e.queues.begin(),e.queues.end(),[&](const auto& q){return q.id==r[3];});
        if (it==e.queues.end()) return MM_ESRCH;
        auto& q=*it;
        // RPCS3 ffba2bf0 sys_event.cpp:441-444,483-486: PPU receivers
        // reject SPU queues before reading outputs or consuming an event.
        if ((r[11]==130 || r[11]==131) && q.type!=1) return MM_EINVAL;
        // sysutil callback queue (libsysutil 3.01 0x20113060..74 -> 0x201133c0 tryreceive): pump VSH
        // dialogs so their pad-driven completion is posted before the guest reads the queue.
        if ((r[11]==130 || r[11]==131) && (q.key & ~UINT64_C(3))==UINT64_C(0x8006010000000100)) {GuestMemory::RawPermit raw(mem);vsh_ui_poll(s);}
        if (r[11]==130 || r[11]==131) e.flush();
        switch (r[11]) {
        case 129: {
            if (r[4]>1) return MM_EINVAL;
            auto receiving=[&](const auto& group,const auto& thread){
                if(!thread.waiting_receive || (group.state!=3 && group.state!=5))return false;
                for(const auto& binding:thread.queue_bindings)if(binding.first==thread.receive_key && binding.second==q.id)return true;
                return false;
            };
            bool waiting=false;
            for(const auto& thread:s.ppu_threads)waiting|=thread.wait_kind==1 && thread.wait_id==q.id;
            for(const auto& group:s.spu.groups)for(const auto& thread:group.threads)waiting|=receiving(group,thread);
            if(!r[4] && waiting)return MM_EBUSY;
            q.data.close();
            for(auto& thread:s.ppu_threads)if(thread.wait_kind==1 && thread.wait_id==q.id){thread.wait_kind=0;thread.cancelled_event=q.id;s.reschedule=true;}
            for(auto& group:s.spu.groups){
                if(std::none_of(group.threads.begin(),group.threads.end(),[&](const auto& t){return receiving(group,t);}))continue;
                SpuOwnerScope control(s,group.id,false,false);
                for(auto& thread:group.threads)if(receiving(group,thread)){
                    thread.in_mailbox={0x80010013};thread.waiting_receive=false;
                    thread.pc=(thread.pc+4)&0x3fffc;++thread.steps;thread.published_steps.value.store(thread.steps,std::memory_order_relaxed);
                    group.receive_thread=UINT32_MAX;group.state=group.state==5?4:6;
                }
            }
            for (auto& p:e.ports) if (p.queue==q.id) p.queue=0;
            e.queues.erase(it); return 0;
        }
        case 131: {
            const auto size=static_cast<std::int32_t>(r[5]);
            const auto count=size>0?std::min<std::size_t>(size,q.data.size()):0;
            mem.validate(r[6],4,true);
            if (count) mem.validate(r[4],count*32,true);
            for (std::size_t i=0;i<count;++i) for (unsigned j=0;j<4;++j) mem.store(r[4]+i*32+j*8,8,q.data[i][j]);
            q.data.erase(q.data.begin(),q.data.begin()+count); mem.store(r[6],4,count); return 0;
        }
        case 130:
            if (q.data.empty()) {
                if(r[5]){ // timeout in microseconds; deadline survives retried receives
                    if(s.wait_timed_out){s.wait_timed_out=false;s.wait_deadline=0;return 0x8001000b;} // CELL_ETIMEDOUT
                    if(!s.wait_deadline)s.wait_deadline=sync_ext_now()+r[5];
                }else if(s.ppu_threads.size()<2 && !s.process.spu_worker.enabled)throw std::runtime_error("empty event queue receive requires active peer/scheduler");
                ppu_defer_block(s,1,q.id);return 0;
            }
            s.wait_deadline=0;s.wait_timed_out=false;
            for (unsigned j=0;j<4;++j) r[4+j]=q.data.front()[j];
            q.data.erase(q.data.begin()); return 0;
        case 133: q.data.clear(); return 0;
        default: throw std::runtime_error("unsupported event queue operation");
        }
    }
    if (r[11]==134) {
        if (r[4]!=1 && r[4]!=3) return MM_EINVAL;
        mem.validate(r[3],4,true); if(e.next_port>UINT32_MAX-0x100)throw std::runtime_error("event port ID exhausted");const auto id=e.next_port; e.next_port+=0x100;
        e.ports.push_back({id,static_cast<std::uint32_t>(r[4]),0,r[5]?r[5]:(UINT64_C(1)<<32)|id});
        mem.store(r[3],4,id); return 0;
    }
    auto it=std::find_if(e.ports.begin(),e.ports.end(),[&](const auto& p){return p.id==r[3];});
    if (it==e.ports.end()) return MM_ESRCH;
    auto& p=*it;
    switch (r[11]) {
    case 135: if (p.queue) return 0x80010015; e.ports.erase(it); return 0;
    case 136:
        if (p.type!=1) return MM_EINVAL;
        if (p.queue) return 0x80010015;
        for (const auto& q:e.queues) if (q.id==r[4]) {p.queue=q.id;return 0;}
        return MM_ESRCH;
    case 137: if (!p.queue) return 0x80010016; p.queue=0; return 0;
    case 138:
        for (auto& q:e.queues) if (p.queue && q.id==p.queue) {
            return q.data.send({p.name,r[4],r[5],r[6]});
        }
        return 0x80010016;
    default: throw std::runtime_error("unsupported event port operation");
    }
}
'''
