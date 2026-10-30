"""C++ memory leases used only by the opt-in parallel executor.

Ordinary parallel accesses use GNU atomic byte-addressable storage and immutable maps.
Raw legacy HLE/RSX scopes exclude memory accesses, not private SPU execution.
Reservations serialize physical 128-byte lines, including ordinary stores.
"""
from .par_pages import TYPES as PAGE_BROKER_TYPES
from .par_wait import TYPES as RESERVATION_WAIT_TYPES

TYPES = RESERVATION_WAIT_TYPES + r'''
    // Diagnostic event counters (PERF GUEST_MEMORY); relaxed, any thread.
    inline static std::atomic<std::uint64_t> stat_membarriers{0},stat_slow_leases{0},stat_gpu_claims{0};
    struct ParallelMemory {
        struct Request {
            HostAccessOrigin origin=HostAccessContext::sample();bool byte_access=HostByteAccess::active();
            const void* pointer=nullptr;std::uint64_t size=0;bool write=false,done=false;
            std::vector<std::pair<const void*,std::uint64_t>> spans;
            std::exception_ptr error;std::condition_variable ready;std::uint64_t generation=0;
        };
        inline static std::atomic<std::uint64_t> next_identity{1};
        const std::uint64_t identity=next_identity.fetch_add(1);
        std::shared_ptr<ReservationNotifications> notifications=std::make_shared<ReservationNotifications>();
        bool enabled=false;
        std::thread::id owner;
        std::recursive_mutex data;
        std::mutex queue_mutex;std::condition_variable queue_space;
        static constexpr unsigned queue_capacity=64;
        std::deque<std::shared_ptr<Request>> requests;
        std::atomic<std::uint64_t> raw_generation{0};
        std::atomic<unsigned> coherence_pending{0};
        bool stopping=false;
        std::exception_ptr stop_error;
        std::function<void()> wake_owner;
        std::function<void(const std::function<void()>&)> owner_coherence;
        std::uint64_t epoch=1;
        // Each host writes only its own cache line. The rare exclusive side
        // supplies the StoreLoad barrier with Linux membarrier, not the PPU.
        struct alignas(64) Reader {
            // Compact scalar admission publishes one hazard for map lifetime,
            // page ownership and (tagged) untracked writes. The pending value
            // pins maps before a physical page can be resolved.
            static constexpr std::uintptr_t scalar_pending=UINTPTR_MAX-1;
            static constexpr std::uintptr_t scalar_write=std::uintptr_t(1)<<(sizeof(std::uintptr_t)*8-1);
            std::atomic<bool> used{false},active{false};
            std::atomic<std::uintptr_t> page{UINTPTR_MAX},write_line{UINTPTR_MAX};
            std::atomic<const void*> dirty_snapshot{nullptr};
            // PPUFAST inline section sequence, odd while the owner runs
            // translated code that may use the flat view without hazards.
            std::atomic<std::uint64_t> section{0};
        };
        std::array<Reader,64> readers;
        bool asymmetric=false;
        std::atomic<unsigned> reader_limit{0},admission_waiters{0};
        std::atomic<bool> exclusive{false};
        std::mutex admission_mutex;std::condition_variable admission_changed;
        struct BackingState {
            std::shared_ptr<GuestBacking> bytes;
            std::vector<std::uint64_t> versions;
            std::unique_ptr<std::atomic<bool>[]> tracked;
            explicit BackingState(std::shared_ptr<GuestBacking> b):bytes(std::move(b)),versions((bytes->size()+127)/128,0),tracked(new std::atomic<bool>[versions.size()]{}){}
        };
        struct Mapping {Region region;std::shared_ptr<BackingState> state;};
        struct Snapshot {std::uint64_t epoch;std::vector<Mapping> maps;};
        std::unique_ptr<Snapshot> snapshot;
        struct alignas(64) LineMutex {
            std::atomic_flag held=ATOMIC_FLAG_INIT;
            void lock(){unsigned spins=0;while(held.test_and_set(std::memory_order_acquire)){if(++spins>=16){std::this_thread::yield();spins=0;}}}
            void unlock(){held.clear(std::memory_order_release);}
        };
        std::array<LineMutex,4096> lines;
        std::unordered_map<const GuestBacking*,std::weak_ptr<BackingState>> backings;
    };
    std::shared_ptr<ParallelMemory> parallel=std::make_shared<ParallelMemory>();
    // Inline storage for the usual scalar/VMX/DMA span and page sets. Large
    // declared batches retain a cold vector fallback; no truncation or cap.
    template<class T,unsigned N=4> struct SmallList {
        std::array<T,N> small{};std::vector<T> large;unsigned count=0;
        void push_back(T value){
            if(count<N){small[count++]=std::move(value);return;}
            if(count==N){large.reserve(N*2);for(auto& item:small)large.push_back(std::move(item));}
            large.push_back(std::move(value));++count;
        }
        auto begin(){return count>N?large.data():small.data();}auto end(){return begin()+count;}
        auto begin()const{return count>N?large.data():small.data();}auto end()const{return begin()+count;}
        auto size()const{return count;}bool empty()const{return !count;}
        auto& operator[](unsigned i){return begin()[i];}const auto& operator[](unsigned i)const{return begin()[i];}
        void unique_sort(){std::sort(begin(),end());const auto n=unsigned(std::unique(begin(),end())-begin());
            if(count>N && n<=N){for(unsigned i=0;i<n;++i)small[i]=std::move(large[i]);large.clear();}
            else if(count>N){large.resize(n);}
            count=n;}
    };
    struct ScalarCache {
        std::uint64_t epoch=0,base=0,end=0;unsigned flags=0;
        const ParallelMemory::Mapping* map=nullptr;
        std::uint8_t* pointer=nullptr;std::uint8_t* coherent=nullptr;
        const std::atomic<bool>* tracked=nullptr;
    };
    struct LocalAccess {
        unsigned leases=0,raw=0,quiet=0,slot=UINT32_MAX,observer_pauses=0,raw_yields=0;bool fault_claim=false,renderer_claim=false,is_owner=false;
        unsigned scalar_batch_depth=0;bool scalar_batch_active=false,inline_active=false;
        const SmallList<std::uintptr_t,4>* leased_pages=nullptr;std::uintptr_t leased_scalar_page=UINTPTR_MAX;
        const ParallelMemory::Snapshot* snapshot=nullptr;
        std::array<std::pair<std::uint32_t,unsigned>,64> cache{};
        std::array<ScalarCache,64> scalar_cache{};
        std::weak_ptr<ParallelMemory> bound;std::uint64_t bound_identity=0;
        ~LocalAccess(){if(auto p=bound.lock())if(slot<p->readers.size()){p->readers[slot].dirty_snapshot.store(nullptr,std::memory_order_seq_cst);p->readers[slot].active.store(false);p->readers[slot].used.store(false);}}
    };
    inline static thread_local std::unordered_map<const GuestMemory*,LocalAccess> local_access;
    [[gnu::always_inline]] LocalAccess& local_state()const{
        // unordered_map node references survive rehash. Cache the common one
        // address-space case without an atomic/shared_ptr operation per load.
        thread_local const GuestMemory* cached_memory=nullptr;thread_local LocalAccess* cached_local=nullptr;
        if(cached_memory==this)return *cached_local;
        auto& local=local_access[this];cached_memory=this;cached_local=&local;return local;
    }
    // Bound before launch and cleared after join. Only the registered renderer
    // may use brief metadata permits; it cannot publish guest maps.
    bool renderer_enabled=false;
    std::thread::id renderer_thread;
    std::function<void(bool)> renderer_barrier;
    std::function<void()> renderer_cancel;
    std::function<void()> renderer_maps_changed;
    unsigned configuration_depth=0;
    std::function<bool()> renderer_metadata_allowed;
    std::function<void(const void*,std::uint64_t,bool)> renderer_access;
    std::function<void(const std::function<void()>&)> renderer_pin;
    bool renderer_owner()const{return renderer_enabled && std::this_thread::get_id()==renderer_thread;}
    bool observer_paused()const{return concurrent() && !memory_owner()?local_state().observer_pauses!=0:in_host_access_observer;}
    // Writers serialize only with other metadata writers. Scalar readers use
    // stable atomic watch cells and never acquire this mutex or close admission.
    struct MetadataMutex {
        std::recursive_mutex value;MetadataMutex()=default;
        MetadataMutex(const MetadataMutex&){}
        MetadataMutex& operator=(const MetadataMutex&){return *this;}
    } metadata_mutex;
    struct MetadataPermit {
        std::unique_lock<std::recursive_mutex> lock;
        explicit MetadataPermit(GuestMemory& m):lock(m.metadata_mutex.value,std::defer_lock){
            if(m.concurrent() && !m.memory_owner() && !m.renderer_owner())throw std::runtime_error("foreign renderer metadata permit");
            lock.lock();
        }
    };
    void renderer_metadata(const std::function<void()>& invoke){MetadataPermit metadata(*this);invoke();}
    bool parallel_enabled=false;
    bool concurrent()const{return __builtin_expect(parallel_enabled,false);}
    bool memory_owner()const{return std::this_thread::get_id()==parallel->owner;}
    bool raw_allowed()const{
        if(!concurrent())return true;
        const auto it=local_access.find(this);
        return it!=local_access.end() && (it->second.raw && (memory_owner() || renderer_owner()));
    }
    void require_raw()const{if(!raw_allowed())throw std::runtime_error("parallel guest raw pointer requires an exclusive owner permit");}
    static bool register_membarrier(){
#if defined(__linux__) && defined(SYS_membarrier) && defined(__x86_64__)
        static const bool available=[] {
            const auto supported=syscall(SYS_membarrier,MEMBARRIER_CMD_QUERY,0,0);
            return supported>=0 && (supported&MEMBARRIER_CMD_PRIVATE_EXPEDITED) &&
                !syscall(SYS_membarrier,MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED,0,0);
        }();
        return available;
#else
        return false;
#endif
    }
    void asymmetric_barrier(){
#if defined(__linux__) && defined(SYS_membarrier) && defined(__x86_64__)
        if(parallel->asymmetric){stat_membarriers.fetch_add(1,std::memory_order_relaxed);if(syscall(SYS_membarrier,MEMBARRIER_CMD_PRIVATE_EXPEDITED,0,0))
            throw std::runtime_error("guest memory membarrier failed");}
#endif
    }
    // Raw owners exclude guest-memory readers; private SPU execution may
    // continue. State control separately acknowledges the affected workers.
    // Quiescence is used only by map publication and legacy non-atomic raw
    // pointer owners. Ordinary scalar/VMX/DMA operations never take this mutex.
    // Asymmetric admission on Linux/x86-64, seq_cst fallback elsewhere: a
    // reader precedes the exclusive scan or sees the closed gate and withdraws.
    [[gnu::noinline]] void bind_reader(LocalAccess& local){
        if(local.bound_identity!=parallel->identity){
            if(local.inline_active)throw std::runtime_error("inline section crosses address-space rebinding");
            if(auto previous=local.bound.lock())if(local.slot<previous->readers.size())previous->readers[local.slot].used.store(false);
            local.slot=UINT32_MAX;local.leases=0;local.quiet=0;local.cache={};local.scalar_cache={};local.bound=parallel;local.bound_identity=parallel->identity;local.is_owner=memory_owner();
        }
        if(local.slot==UINT32_MAX){
            for(unsigned i=0;i<parallel->readers.size();++i){bool unused=false;if(parallel->readers[i].used.compare_exchange_strong(unused,true)){local.slot=i;break;}}
            if(local.slot==UINT32_MAX)throw std::runtime_error("guest memory reader slots exhausted");
            auto limit=parallel->reader_limit.load(std::memory_order_seq_cst);
            while(limit<=local.slot && !parallel->reader_limit.compare_exchange_weak(limit,local.slot+1,std::memory_order_seq_cst)){}
        }
    }
    [[gnu::noinline]] void wait_reader_gate(){
        std::unique_lock<std::mutex> lock(parallel->admission_mutex);
        ++parallel->admission_waiters;
        parallel->admission_changed.wait(lock,[&]{return !parallel->exclusive.load(std::memory_order_seq_cst);});
        --parallel->admission_waiters;
    }
    [[gnu::always_inline]] void enter_reader(LocalAccess& local){
        if(local.bound_identity!=parallel->identity || local.slot==UINT32_MAX)bind_reader(local);
        for(;;){
            if(parallel->exclusive.load(std::memory_order_acquire))wait_reader_gate();
            if(parallel->asymmetric)parallel->readers[local.slot].active.store(true,std::memory_order_release);
            else parallel->readers[local.slot].active.store(true,std::memory_order_seq_cst);
            // Compiler barrier pairs with the expedited remote CPU barrier.
            std::atomic_signal_fence(std::memory_order_seq_cst);
            if(parallel->asymmetric){if(!parallel->exclusive.load(std::memory_order_acquire))break;}
            else if(!parallel->exclusive.load(std::memory_order_seq_cst))break;
            leave_reader(local);
        }
        local.snapshot=parallel->snapshot.get();
    }
    [[gnu::always_inline]] void leave_reader(LocalAccess& local){
        if(parallel->asymmetric)parallel->readers[local.slot].active.store(false,std::memory_order_release);
        else parallel->readers[local.slot].active.store(false,std::memory_order_seq_cst);
        const bool waiting=parallel->asymmetric?parallel->exclusive.load(std::memory_order_acquire):parallel->exclusive.load(std::memory_order_seq_cst);
        if(waiting){
            notify_admission();
        }
    }
    // ScalarAccess is admitted only on the asymmetric ABI. A cold gate,
    // binding or retry goes to the existing outlined helper, so the AOT fast
    // path needs no SC fallback loop or registration machinery.
    [[gnu::always_inline]] bool try_enter_scalar_reader(LocalAccess& local){
        if(local.bound_identity!=parallel->identity || local.slot==UINT32_MAX ||
           parallel->exclusive.load(std::memory_order_acquire))return false;
        parallel->readers[local.slot].page.store(ParallelMemory::Reader::scalar_pending,std::memory_order_release);
        std::atomic_signal_fence(std::memory_order_seq_cst);
        if(parallel->exclusive.load(std::memory_order_acquire)){leave_scalar_reader(local);return false;}
        local.snapshot=parallel->snapshot.get();return true;
    }
    [[gnu::always_inline]] void leave_scalar_reader(LocalAccess& local){
        parallel->readers[local.slot].page.store(UINTPTR_MAX,std::memory_order_release);
        if(parallel->exclusive.load(std::memory_order_acquire))notify_admission();
    }
    [[gnu::noinline]] void notify_admission(){
        // Reader retirement is a release store, not a StoreLoad fence. A
        // waiter-count load may pass that store: observing zero waiters does
        // NOT ensure a later waiter can already see active=false. Always
        // synchronize with its predicate check under the admission mutex.
        // This path is cold: ordinary readers call it only at a closed gate.
        std::lock_guard<std::mutex> lock(parallel->admission_mutex);
        parallel->admission_changed.notify_all();
    }
    [[gnu::always_inline]] void suspend_scalar_batch(LocalAccess& local){
        // Every admission, raw scope or blocking byte API first retires an
        // inline section; translated code then sees the blocked table.
        if(local.inline_active)inline_leave(local);
        if(local.scalar_batch_active && !local.leases){local.scalar_batch_active=false;leave_reader(local);}
    }
    [[gnu::noinline]] void resume_scalar_batch(LocalAccess& local){
        enter_reader(local);local.scalar_batch_active=true;
    }
    // A bounded translated PPU dispatch can amortize map admission. This is
    // NOT a private payload: every scalar still checks live watches, claims,
    // reservation interest and uses acquire/release atomics. Cold byte APIs
    // revoke the batch before they can block/call the renderer. A waiting raw
    // writer revokes it at the next scalar or at dispatch exit.
    struct ScalarBatch {
        GuestMemory* memory=nullptr;LocalAccess* local=nullptr;
        explicit ScalarBatch(GuestMemory& m){
            if(!m.concurrent() || !m.parallel->asymmetric || !m.page_broker.enabled.load(std::memory_order_acquire))return;
            auto& state=m.local_state();if(!m.memory_owner() || state.raw || state.leases)return;
            memory=&m;local=&state;++state.scalar_batch_depth;
        }
        ~ScalarBatch(){if(local && !--local->scalar_batch_depth)memory->suspend_scalar_batch(*local);}
        ScalarBatch(const ScalarBatch&)=delete;
    };
    void release_exclusive(){
        parallel->raw_generation.fetch_add(1,std::memory_order_release);
        parallel->exclusive.store(false,std::memory_order_seq_cst);
        notify_admission();
    }
    void release_reader_slot(){auto& local=local_state();if(local.leases || local.raw)throw std::runtime_error("guest reader deregistration inside access");
        suspend_scalar_batch(local);
        if(local.slot<parallel->readers.size()){parallel->readers[local.slot].used.store(false,std::memory_order_release);local.slot=UINT32_MAX;}}
    struct RawPermit {
        GuestMemory& memory;bool active=false;
        explicit RawPermit(GuestMemory& m):memory(m){if(!m.concurrent())return;
            if(!m.memory_owner() && !m.renderer_owner())throw std::runtime_error("foreign raw guest permit");
            auto& local=m.local_state();m.suspend_scalar_batch(local);if(local.leases)throw std::runtime_error("raw guest permit inside reader lease");
            // A legacy service excludes CPU byte readers, not the FIFO owner
            // or its resident images. Actual dependencies go through notify_host
            // and protected pointers; never wait on unrelated GPU page claims.
            if(!local.raw){
                if(!m.renderer_enabled)for(;;){
                    {std::lock_guard<std::mutex> lock(m.page_broker.mutex);if(!m.page_broker.owners)break;
                        if(!m.page_broker.pump)throw std::runtime_error("legacy raw permit overlaps page ownership; configure owner pump or use RawPages");}
                    m.page_broker.wait_change(true);
                }
                // Main and renderer both publish observer metadata. They must
                // contend for ONE exclusive token, not store true concurrently.
                bool free=false;
                if(!m.parallel->exclusive.compare_exchange_strong(free,true,std::memory_order_seq_cst)){
                    std::unique_lock<std::mutex> lock(m.parallel->admission_mutex);++m.parallel->admission_waiters;
                    m.parallel->admission_changed.wait(lock,[&]{bool available=false;return m.parallel->exclusive.compare_exchange_strong(available,true,std::memory_order_seq_cst);});
                    --m.parallel->admission_waiters;
                }
                try{m.asymmetric_barrier();}catch(...){m.release_exclusive();throw;}
                auto quiet=[&]{
                    const auto limit=m.parallel->reader_limit.load(std::memory_order_seq_cst);
                    for(unsigned i=0;i<limit;++i)if(m.parallel->readers[i].active.load(std::memory_order_seq_cst) ||
                        m.parallel->readers[i].page.load(std::memory_order_seq_cst)!=UINTPTR_MAX)return false;
                    return true;
                };
                if(!quiet()){
                    std::unique_lock<std::mutex> lock(m.parallel->admission_mutex);++m.parallel->admission_waiters;
                    m.parallel->admission_changed.wait(lock,quiet);--m.parallel->admission_waiters;
                }
            }
            ++local.raw;local.snapshot=m.parallel->snapshot.get();active=true;
        }
        ~RawPermit(){if(active && !--memory.local_state().raw)memory.release_exclusive();}
        RawPermit(const RawPermit&)=delete;
    };
    struct RawYield {
        GuestMemory& memory;unsigned depth=0;
        explicit RawYield(GuestMemory& m):memory(m){if(!m.concurrent())return;auto& local=m.local_state();depth=local.raw;
            // Worker pause/RPC can wait before acquiring a RawPermit. A batch
            // map pin must never span that wait, even when raw depth is zero.
            m.suspend_scalar_batch(local);
            if(depth){local.raw=0;m.release_exclusive();++local.raw_yields;}}
        ~RawYield(){if(depth){
            // Restore admission even while propagating the original owner
            // failure; join the poisoned renderer before re-entering raw HLE.
            if(memory.renderer_enabled && memory.memory_owner() && memory.renderer_metadata_allowed){
                try{(void)memory.renderer_metadata_allowed();}catch(...){if(memory.renderer_cancel)memory.renderer_cancel();}
            }
            {RawPermit raw(memory);memory.local_state().raw=depth+1;}--memory.local_state().raw_yields;
        }}
        RawYield(const RawYield&)=delete;
    };
    // Only map/configuration writers stop FIFO admission. RawYield releases
    // byte exclusion while the barrier services coordinator RPC; it NEVER
    // resumes FIFO halfway through a map transaction.
    struct ConfigurationPermit {
        GuestMemory& memory;bool active=false;
        explicit ConfigurationPermit(GuestMemory& m):memory(m){
            if(!m.renderer_enabled || !m.memory_owner())return;
            if(m.configuration_depth++){active=true;return;}
            try{RawYield yield(m);m.renderer_barrier(true);active=true;}
            catch(...){--m.configuration_depth;throw;}
        }
        ~ConfigurationPermit(){if(active && !--memory.configuration_depth && memory.renderer_barrier){RawYield yield(memory);if(memory.renderer_barrier)memory.renderer_barrier(false);}}
        ConfigurationPermit(const ConfigurationPermit&)=delete;
    };
    void publish_maps(){
        auto next=std::make_unique<ParallelMemory::Snapshot>();next->epoch=parallel->epoch;
        for(const auto& r:regions){
            page_broker.prepare(r.bytes->data(),r.bytes->size());
            auto& weak=parallel->backings[r.bytes.get()];auto state=weak.lock();
            if(!state){state=std::make_shared<ParallelMemory::BackingState>(r.bytes);weak=state;}
            next->maps.push_back({r,std::move(state)});
        }
        inline_publish(next->maps);
        // A watch may predate mapping a retained backing. Rebuild its live
        // summary after prepare, including host radix leaves created now.
        host_access_observer.refresh_summary();cpu_dirty_filter.refresh_summary();
        parallel->snapshot=std::move(next);
        if(concurrent())local_state().snapshot=parallel->snapshot.get();
    }
    void enable_concurrency(){
        if(parallel.use_count()!=1)throw std::runtime_error("parallel guest memory cannot share a copied address space");
        parallel->asymmetric=register_membarrier();
        parallel->owner=std::this_thread::get_id();publish_maps();parallel->enabled=true;parallel_enabled=true;
        host_access_observer.page_summary=[this](std::uintptr_t page,bool watched){if(auto* cell=page_broker.cell(page))cell->observed.store(watched,std::memory_order_release);inline_mark_host(page,InlineFlat::observed,watched);};
        cpu_dirty_filter.page_summary=[this](std::uintptr_t page,bool watched){if(auto* cell=page_broker.cell(page))cell->dirty.store(watched,std::memory_order_release);inline_mark_host(page,InlineFlat::dirty,watched);};
        host_access_observer.refresh_summary();cpu_dirty_filter.refresh_summary();
        inline_enabled.store(parallel->asymmetric && inline_flat.base,std::memory_order_release);
        page_broker.quiesce=[this](const std::vector<std::uintptr_t>& pages){
            // The claim bit is already published. Inline sections did not
            // publish page hazards: block their table entries, fence, then
            // wait until every section that could have missed it has ended.
            if(local_state().inline_active)inline_leave(local_state());
            for(const auto page:pages)inline_mark_host(page,InlineFlat::claimed,true);
            asymmetric_barrier();inline_wait_sections();
            for(;;){bool busy=false;
                for(unsigned i=0;i<parallel->reader_limit.load(std::memory_order_acquire);++i){
                    const auto page=parallel->readers[i].page.load(std::memory_order_acquire);
                    busy|=std::binary_search(pages.begin(),pages.end(),page&~ParallelMemory::Reader::scalar_write);
                }
                if(!busy)return;
                // A reader may need a queued owner operation before retiring.
                // Preserve the broker's bounded wait/pump protocol; spinning
                // here would strand that reader and its pending claim.
                page_broker.wait_change(true);
            }
        };
    }
    void mapping_changed(){
        ConfigurationPermit configuration(*this);
        RawPermit raw(*this);
        if(renderer_enabled && renderer_maps_changed){RawYield yield(*this);renderer_maps_changed();}
        if(parallel->epoch==UINT32_MAX)throw std::runtime_error("guest map epoch overflow");
        ++parallel->epoch;parallel->notifications->notify(nullptr,0,true);std::fill(page_regions.begin(),page_regions.end(),0);last_region=0;
        if(concurrent())publish_maps();
    }
    struct HostSpan {std::uint8_t* pointer;unsigned size;std::shared_ptr<GuestBacking> pin;};
''' + PAGE_BROKER_TYPES + r'''
    // Only the map owner may publish maps, raw scopes or observer metadata.
    // On that same thread an unobserved scalar operation needs no reader
    // admission against itself. Foreign payloads still use atomic words and
    // physical stripe locks. Claims can only begin on this thread, so an
    // unclaimed owner scalar needs no CPU pin against a future acquisition.
    [[gnu::always_inline]] const ParallelMemory::Mapping* owner_scalar(std::uint64_t address,unsigned size,bool write){
        if(!memory_owner() || renderer_enabled)return nullptr;
        auto& local=local_state();local.snapshot=parallel->snapshot.get();
        if(local.leases)return nullptr; // Preserve the nested batch contract.
        const auto& map=translate_parallel(address,size,write);
        const bool observed=!local.raw && host_access_observer && !observer_paused() &&
            host_access_observer.synchronous(map.region.bytes->data()+address-map.region.base,size);
        if(page_broker.enabled.load(std::memory_order_acquire)){
            // Interested accesses use the complete lease/coherence loop:
            // observer callbacks may create new claims before payload access.
            if(observed)return nullptr;
            if(!page_broker.owner_unclaimed(map.region.bytes->data()+address-map.region.base,size))return nullptr;
        }
        if(observed){
            const auto pin=map.region.bytes;const auto* pointer=pin->data()+address-map.region.base;
            HostByteAccess bytes(host_access_observer.supports_exact_bytes.load(std::memory_order_acquire),true);
            HostAccessContext::Address origin_address(address);
            notify_host(pointer,size,write);
            // A callback may publish a new map. Resolve again only after it
            // returns; never retain a Mapping reference across publication.
            local.snapshot=parallel->snapshot.get();const auto& current=translate_parallel(address,size,write);
            // Installation/poison of the broker in a callback must not turn
            // a previously valid owner shortcut into an unchecked payload.
            if(page_broker.enabled.load(std::memory_order_acquire) &&
                !page_broker.owner_unclaimed(current.region.bytes->data()+address-current.region.base,size))return nullptr;
            return &current;
        }
        return &map;
    }
    // Reader-admitted probe: does one mapping of the current snapshot contain [address,address+size)?  No cache update.
    bool single_mapping(std::uint64_t address,unsigned size){
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)return false;
        auto& local=local_state();const auto& maps=local.snapshot->maps;
        auto matches=[&](const auto& r){return address>=r.base && address-r.base<r.bytes->size() && size<=r.bytes->size()-(address-r.base);};
        const auto& cached=local.cache[(address>>12)%local.cache.size()];
        if(cached.first==local.snapshot->epoch && cached.second<maps.size() && matches(maps[cached.second].region))return true;
        for(const auto& map:maps)if(matches(map.region))return true;
        return false;
    }
    [[gnu::always_inline]] const ParallelMemory::Mapping& translate_parallel(std::uint64_t address,unsigned size,bool write){
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)throw std::runtime_error("guest address outside 32-bit space");
        auto& local=local_state();const auto& maps=local.snapshot->maps;
        auto& cached=local.cache[(address>>12)%local.cache.size()];
        auto matches=[&](const auto& r){return address>=r.base && address-r.base<r.bytes->size() && size<=r.bytes->size()-(address-r.base);};
        unsigned index=UINT32_MAX;
        if(cached.first==local.snapshot->epoch && cached.second<maps.size() && matches(maps[cached.second].region))index=cached.second;
        else for(unsigned i=0;i<maps.size();++i)if(matches(maps[i].region)){index=i;cached={static_cast<std::uint32_t>(local.snapshot->epoch),i};break;}
        if(index==UINT32_MAX)throw std::runtime_error("unmapped or split guest memory access address="+std::to_string(address)+" size="+std::to_string(size));
        const auto& map=maps[index];
        if(!(map.region.flags&(write?2:4)))throw std::runtime_error("guest memory permission fault");
        if(write && (map.region.flags&1))throw std::runtime_error("self-modifying code is unsupported");
        cache_scalar_mapping(local,map,address);return map;
    }
    // Called only AFTER reader admission; immutable map epoch owns all cached
    // pointers. Physical/coherent views and tracking remain pinned by that map.
    // No watch result, payload or reservation version is cached here.
    [[gnu::always_inline]] const ScalarCache* cached_parallel_map(LocalAccess& local,std::uint64_t address,unsigned size,bool write){
        const auto& hit=local.scalar_cache[(address>>12)%local.scalar_cache.size()];
        if(hit.epoch!=local.snapshot->epoch || address<hit.base || address>=hit.end || size>hit.end-address ||
           !(hit.flags&(write?2:4)) || (write && (hit.flags&1)))return nullptr;
        return &hit;
    }
    void cache_scalar_mapping(LocalAccess& local,const ParallelMemory::Mapping& map,std::uint64_t address){
        const auto& r=map.region;auto& cached=local.scalar_cache[(address>>12)%local.scalar_cache.size()];
        cached={local.snapshot->epoch,r.base,r.base+r.bytes->size(),r.flags,&map,r.bytes->data(),r.bytes->coherent_data(),map.state->tracked.get()};
    }
    // Minimal scalar lifetime: no batch containers and no temporary origin or
    // HostByteAccess TLS changes for bytes which cannot invoke an observer.
    // The complete Lease path below handles callbacks, faults and nesting.
    struct ScalarAccess {
        GuestMemory& memory;LocalAccess* local=nullptr;const ParallelMemory::Mapping* map=nullptr;
        std::uint8_t* pointer=nullptr;std::uint8_t* payload=nullptr;const std::atomic<bool>* tracked=nullptr;PageBroker::Cell* cell=nullptr;
        bool compact=false,batched=false,full_lease=true;
        [[gnu::always_inline]] void retire(LocalAccess& state){
            if(batched)memory.parallel->readers[state.slot].page.store(UINTPTR_MAX,std::memory_order_release);
            else if(compact)memory.leave_scalar_reader(state);
            else{memory.parallel->readers[state.slot].page.store(UINTPTR_MAX,std::memory_order_release);memory.leave_reader(state);}
        }
        [[gnu::always_inline]] void reject(LocalAccess& state){retire(state);memory.suspend_scalar_batch(state);}
        [[gnu::always_inline]] ScalarAccess(GuestMemory& m,std::uint64_t address,unsigned size,bool write,bool cached_only=false,bool payload_only=false):memory(m),compact(cached_only),full_lease(!payload_only){
            if(!size || size>8 || !m.parallel->asymmetric || !m.page_broker.enabled.load(std::memory_order_acquire))return;
            auto& state=m.local_state();if(state.raw || state.leases)return;
            if(cached_only && state.scalar_batch_depth){
                if(state.scalar_batch_active && m.parallel->exclusive.load(std::memory_order_acquire))m.suspend_scalar_batch(state);
                if(!state.scalar_batch_active)m.resume_scalar_batch(state);
                batched=true;
            }else if(cached_only){if(!m.try_enter_scalar_reader(state))return;}
            else{m.suspend_scalar_batch(state);m.enter_reader(state);}
            try{
                if(!cached_only)(void)m.translate_parallel(address,size,write);
                const auto* hit=m.cached_parallel_map(state,address,size,write);
                if(!hit){reject(state);return;}
                const auto& candidate=*hit->map;pointer=hit->pointer+address-hit->base;
                payload=m.host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)?hit->coherent+address-hit->base:pointer;
                tracked=hit->tracked+(address-hit->base)/128;
                const auto at=reinterpret_cast<std::uintptr_t>(pointer);
                if((at>>12)!=((at+size-1)>>12)){reject(state);return;}
                m.page_broker.check_fast();cell=m.page_broker.cell(at>>12);
                if(!cell)throw std::runtime_error("unprepared guest broker page");
                if(!(state.is_owner?m.in_host_access_observer:state.observer_pauses!=0) && m.host_access_observer &&
                   (!m.host_access_observer.filtered || cell->observed.load(std::memory_order_acquire)) &&
                   m.host_access_observer.scalar_interested(pointer,size,true)){reject(state);return;}
                m.parallel->readers[state.slot].page.store((at>>12)|(compact && write?ParallelMemory::Reader::scalar_write:0),std::memory_order_release);
                std::atomic_signal_fence(std::memory_order_seq_cst);
                if(cell->state.load(std::memory_order_acquire)&PageBroker::claimed){
                    reject(state);return;}
                m.page_broker.check_fast();
                if(full_lease){state.leased_scalar_page=at>>12;state.leased_pages=nullptr;++state.leases;++state.quiet;}
                local=&state;map=&candidate;
            }catch(...){reject(state);throw;}
        }
        [[gnu::always_inline]] ~ScalarAccess(){if(local){if(full_lease){--local->quiet;--local->leases;}retire(*local);}}
        ScalarAccess(const ScalarAccess&)=delete;
    };
    // GNU atomic byte-addressable storage. Naturally aligned scalars use their
    // actual width; bulk fringes use bytes, never read/merge/store a neighbour.
    // This is essential when two unreserved subwords share an enclosing word.
    // All concurrent payload touches remain atomic (raw owners still exclude
    // them). Release/acquire also orders FIFO PUT/GET and label publication.
    using AtomicWord=std::uint64_t __attribute__((__may_alias__));
    using AtomicHalf=std::uint16_t __attribute__((__may_alias__));
    using AtomicInt=std::uint32_t __attribute__((__may_alias__));
    // An 8-byte-aligned quadword: atomic_read's same two 8-byte atomic loads,
    // but one 16-byte store into the private destination, so a following
    // 16-byte vector load of it is store-forwarded instead of stalling.
    static void atomic_read16(const std::uint8_t* p,std::uint8_t (&out)[16]){
        const auto low=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p),__ATOMIC_ACQUIRE);
        const auto high=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p+8),__ATOMIC_ACQUIRE);
#if defined(__SSE2__)
        _mm_storeu_si128(reinterpret_cast<__m128i*>(out),_mm_set_epi64x(static_cast<long long>(high),static_cast<long long>(low)));
#else
        std::memcpy(out,&low,8);std::memcpy(out+8,&high,8);
#endif
    }
    // Ascending order: bytes up to the first 8-byte boundary, then aligned 8-byte words, then the tail bytes - the same
    // sequence of atomic accesses (and the same load-then-store interleaving) as one byte-or-word step per iteration. The word
    // body is unrolled by four instead of re-checking size and alignment per word (~3x faster in cache, HIRES2-W).
    static void atomic_read(const std::uint8_t* p,std::uint8_t* out,unsigned n){
        while(n && (reinterpret_cast<std::uintptr_t>(p)&7)){*out++=__atomic_load_n(p++,__ATOMIC_ACQUIRE);--n;}
        for(;n>=32;n-=32,p+=32,out+=32){
            std::uint64_t v;
            v=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p),__ATOMIC_ACQUIRE);std::memcpy(out,&v,8);
            v=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p+8),__ATOMIC_ACQUIRE);std::memcpy(out+8,&v,8);
            v=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p+16),__ATOMIC_ACQUIRE);std::memcpy(out+16,&v,8);
            v=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p+24),__ATOMIC_ACQUIRE);std::memcpy(out+24,&v,8);
        }
        for(;n>=8;n-=8,p+=8,out+=8){const auto word=__atomic_load_n(reinterpret_cast<const AtomicWord*>(p),__ATOMIC_ACQUIRE);std::memcpy(out,&word,8);}
        while(n){*out++=__atomic_load_n(p++,__ATOMIC_ACQUIRE);--n;}
    }
    static void atomic_write(std::uint8_t* p,const std::uint8_t* in,unsigned n){
        while(n && (reinterpret_cast<std::uintptr_t>(p)&7)){__atomic_store_n(p++,*in++,__ATOMIC_RELEASE);--n;}
        for(;n>=32;n-=32,p+=32,in+=32){
            // Each source word is read just before its store (as in the one-word loop), so an overlapping source keeps its result.
            std::uint64_t v;
            std::memcpy(&v,in,8);__atomic_store_n(reinterpret_cast<AtomicWord*>(p),v,__ATOMIC_RELEASE);
            std::memcpy(&v,in+8,8);__atomic_store_n(reinterpret_cast<AtomicWord*>(p+8),v,__ATOMIC_RELEASE);
            std::memcpy(&v,in+16,8);__atomic_store_n(reinterpret_cast<AtomicWord*>(p+16),v,__ATOMIC_RELEASE);
            std::memcpy(&v,in+24,8);__atomic_store_n(reinterpret_cast<AtomicWord*>(p+24),v,__ATOMIC_RELEASE);
        }
        for(;n>=8;n-=8,p+=8,in+=8){std::uint64_t value;std::memcpy(&value,in,8);__atomic_store_n(reinterpret_cast<AtomicWord*>(p),value,__ATOMIC_RELEASE);}
        while(n){__atomic_store_n(p++,*in++,__ATOMIC_RELEASE);--n;}
    }
    static std::uint64_t atomic_scalar_read(const std::uint8_t* pointer,unsigned size){
        if(size && !(reinterpret_cast<std::uintptr_t>(pointer)&(size-1))){
            switch(size){
            case 1:return __atomic_load_n(pointer,__ATOMIC_ACQUIRE);
            case 2:return from_be(__atomic_load_n(reinterpret_cast<const AtomicHalf*>(pointer),__ATOMIC_ACQUIRE));
            case 4:return from_be(__atomic_load_n(reinterpret_cast<const AtomicInt*>(pointer),__ATOMIC_ACQUIRE));
            case 8:return from_be(__atomic_load_n(reinterpret_cast<const AtomicWord*>(pointer),__ATOMIC_ACQUIRE));
            }
        }
        // A word-aligned doubleword (the RSX FIFO header+value pair, a PPU ld at a 4-byte boundary): its two aligned words in
        // ascending order, one acquire load each. Every value this returns is one the bytewise sequence below can return (each
        // word's four ascending byte loads at one instant); no byte outside [pointer,pointer+8) is touched.
        if(size==8 && !(reinterpret_cast<std::uintptr_t>(pointer)&3)){
            const std::uint64_t high=from_be(__atomic_load_n(reinterpret_cast<const AtomicInt*>(pointer),__ATOMIC_ACQUIRE));
            return (high<<32)|from_be(__atomic_load_n(reinterpret_cast<const AtomicInt*>(pointer+4),__ATOMIC_ACQUIRE));
        }
        std::uint8_t bytes[8]{};atomic_read(pointer,bytes,size);std::uint64_t value=0;
        for(unsigned i=0;i<size;++i)value=(value<<8)|bytes[i];
        return value;
    }
    static void atomic_scalar_write(std::uint8_t* pointer,unsigned size,std::uint64_t value){
        if(size && !(reinterpret_cast<std::uintptr_t>(pointer)&(size-1))){
            switch(size){
            case 1:__atomic_store_n(pointer,static_cast<std::uint8_t>(value),__ATOMIC_RELEASE);return;
            case 2:__atomic_store_n(reinterpret_cast<AtomicHalf*>(pointer),from_be(static_cast<std::uint16_t>(value)),__ATOMIC_RELEASE);return;
            case 4:__atomic_store_n(reinterpret_cast<AtomicInt*>(pointer),from_be(static_cast<std::uint32_t>(value)),__ATOMIC_RELEASE);return;
            case 8:__atomic_store_n(reinterpret_cast<AtomicWord*>(pointer),from_be(value),__ATOMIC_RELEASE);return;
            }
        }
        std::uint8_t bytes[8]{};for(unsigned i=size;i--;){bytes[i]=value;value>>=8;}atomic_write(pointer,bytes,size);
    }
    struct Lease {
        GuestMemory& memory;HostAccessContext::Address origin_address;bool active=false,scalar_hazard=false;std::optional<CpuPages> pages;PageBroker::Cell* scalar_cell=nullptr;
        // false: translation-only admission (GuestMemory::range of a read-only Range): reader gate, map snapshot and observer
        // coherence as usual, but no page-broker cell increments, because no payload byte is read or written under it.
        bool brokered_=true;
        // one_page: the caller proved [address,address+size) lies in one 4 KiB guest page and reads or writes it as one
        // span (SPU DMA, HLE memset/memcpy): admission may use the scalar path, which resolves one physical page directly.
        // One page may still hold several mappings (or a hole): then the scalar path declines and enter() splits the span.
        [[gnu::always_inline]] Lease(GuestMemory& m,std::uint64_t address,unsigned size,bool write,bool split=false,bool brokered=true,bool one_page=false):memory(m),origin_address(address,m.concurrent() || bool(m.host_access_observer)),brokered_(brokered){
            if(!m.concurrent())return;
            m.suspend_scalar_batch(m.local_state());
            if((!split && size && size<=8) || one_page){auto& local=m.local_state();
                if(!local.raw && !local.leases && scalar_enter(address,size,write,one_page))return;}
            enter(address,size,write,split);
        }
        // Keep the serial guard independent of the growing page/coherence
        // protocol. Otherwise whole-TU inlining can tax every serial load.
        [[gnu::noinline]] void enter(std::uint64_t address,unsigned size,bool write,bool split){
            auto& m=memory;auto& local=m.local_state();stat_slow_leases.fetch_add(1,std::memory_order_relaxed);
            if(local.raw){
                // Legacy HLE still uses load/store/Range/DMA byte APIs inside
                // its raw scope. The old global barrier made these coherent
                // implicitly; now preflight only their actual physical spans.
                if(m.renderer_enabled && m.memory_owner() && !m.observer_paused() && m.host_access_observer){
                    if(!split && size){
                        const auto& map=m.translate_parallel(address,size,write);auto* p=map.region.bytes->data()+address-map.region.base;
                        if(m.host_access_observer.synchronous(p,size))m.notify_host(p,size,write);
                    }else{
                        const auto spans=m.parallel_spans(address,size,write,split);
                        for(const auto& span:spans)if(m.host_access_observer.synchronous(span.pointer,span.size))m.notify_host(span.pointer,span.size,write);
                    }
                }
                return;
            }
            if(local.leases){
                if(brokered_ && m.page_broker.enabled.load(std::memory_order_acquire)){
                    const auto required=PageBroker::cpu_keys(m.cpu_spans(address,size,write,split));
                    if(local.leased_pages?!std::includes(local.leased_pages->begin(),local.leased_pages->end(),required.begin(),required.end()):(!required.empty() && (required.size()!=1 || required[0]!=local.leased_scalar_page)))
                        throw std::runtime_error("nested CPU lease expands page set; predeclare complete batch");
                }
                ++local.leases;++local.quiet;active=true;return;
            }
            if(!split && size && size<=8 && scalar_enter(address,size,write))return;
            for(;;){
                m.enter_reader(local);
                try{
                    // Watch bitmaps are published atomically without reader exclusion.
                    // Admission still protects callback and map lifetime.
                    if(brokered_ && m.page_broker.enabled.load(std::memory_order_acquire)){
                        auto requested=m.cpu_spans(address,size,write,split);
                        pages.emplace(m.page_broker,requested);
                        if(!pages->admitted){pages.reset();m.leave_reader(local);
                            // CPU coherence must run outside all admission.
                            {anyps3_ft::Span ft(anyps3_ft::LeaseWait,address,1|(write?256:0),0,size);if(m.page_broker.coherence)m.notify_spans(requested,write);}
                            {anyps3_ft::Span ft(anyps3_ft::LeaseWait,address,3|(write?256:0),0,size);m.page_broker.wait_change(m.memory_owner());}continue;}
                    }
                    if(m.observer_paused() || !m.host_access_observer){if(pages)local.leased_pages=&pages->pages;++local.leases;++local.quiet;active=true;return;}
                    bool interested=false;auto next=address;unsigned left=size;
                    while(left){const auto& map=m.translate_parallel(next,split?1:left,write);const auto& r=map.region;
                        const unsigned n=std::min<std::uint64_t>(left,r.bytes->size()-(next-r.base));
                        interested=interested || (m.host_access_observer && m.host_access_observer.synchronous(r.bytes->data()+next-r.base,n));next+=n;left-=n;}
                    if(!interested){if(pages)local.leased_pages=&pages->pages;++local.leases;++local.quiet;active=true;return;}
                    // Drop admission before owner RPC. The owner returns a
                    // generation captured before it releases exclusive access;
                    // a newer raw/GPU scope forces another preflight.
                    const auto epoch=local.snapshot->epoch;
                    auto spans=m.cpu_spans(address,size,write,split);
                    pages.reset();m.leave_reader(local);
                    std::uint64_t generation;{anyps3_ft::Span ft(anyps3_ft::LeaseWait,address,2|(write?256:0),0,size);generation=m.notify_spans(spans,write);}
                    m.enter_reader(local);
                    if(epoch==local.snapshot->epoch && generation==m.parallel->raw_generation.load(std::memory_order_acquire)){
                        if(brokered_ && m.page_broker.enabled.load(std::memory_order_acquire)){
                            pages.emplace(m.page_broker,m.cpu_spans(address,size,write,split));
                            if(!pages->admitted){pages.reset();m.leave_reader(local);
                                {anyps3_ft::Span ft(anyps3_ft::LeaseWait,address,1|(write?256:0),0,size);if(m.page_broker.coherence)m.notify_spans(spans,write);}
                                {anyps3_ft::Span ft(anyps3_ft::LeaseWait,address,3|(write?256:0),0,size);m.page_broker.wait_change(m.memory_owner());}continue;}
                        }
                        if(pages)local.leased_pages=&pages->pages;
                        ++local.leases;++local.quiet;active=true;return;
                    }
                    m.leave_reader(local);
                }catch(...){if(m.parallel->readers[local.slot].active.load())m.leave_reader(local);throw;}
            }
        }
        // Scalar accesses overwhelmingly touch one physical page and no watched
        // bytes. Resolve directly under reader admission: no span/pin/key array
        // is constructed. The immutable snapshot already pins its backing.
        [[gnu::always_inline]] bool scalar_enter(std::uint64_t address,unsigned size,bool write,bool decline_split=false){
            auto& m=memory;auto& local=m.local_state();m.enter_reader(local);
            // decline_split (one-page transfer): a span that no single mapping contains is not an error here; the general
            // admission (enter) splits it per mapping and reports a real hole or permission fault itself.
            if(decline_split && !m.single_mapping(address,size)){m.leave_reader(local);return false;}
            try{const auto& map=m.translate_parallel(address,size,write);
                const auto* pointer=map.region.bytes->data()+address-map.region.base;
                const auto at=reinterpret_cast<std::uintptr_t>(pointer);
                if((at>>12)!=((at+size-1)>>12) || (!m.observer_paused() && m.host_access_observer && m.host_access_observer.synchronous(pointer,size))){m.leave_reader(local);return false;}
                if(m.page_broker.enabled.load(std::memory_order_acquire)){
                    m.page_broker.check_fast();auto* entry=m.page_broker.cell(at>>12);
                    if(!entry)throw std::runtime_error("unprepared guest broker page");
                    if(m.parallel->asymmetric){
                        m.parallel->readers[local.slot].page.store(at>>12,std::memory_order_release);
                        std::atomic_signal_fence(std::memory_order_seq_cst);
                        if(entry->state.load(std::memory_order_acquire)&PageBroker::claimed){
                            m.parallel->readers[local.slot].page.store(UINTPTR_MAX,std::memory_order_release);m.leave_reader(local);return false;}
                        scalar_hazard=true;
                    }else{
                        auto state=entry->state.load(std::memory_order_relaxed);
                        for(;;){if(state&PageBroker::claimed){m.leave_reader(local);return false;}
                            if(state==PageBroker::claimed-1)throw std::runtime_error("guest page reader overflow");
                            if(entry->state.compare_exchange_weak(state,state+1,std::memory_order_acquire,std::memory_order_relaxed))break;}
                        scalar_cell=entry;
                    }
                    m.page_broker.check_fast();
                }
                local.leased_scalar_page=at>>12;local.leased_pages=nullptr;++local.leases;++local.quiet;active=true;return true;
            }catch(...){if(scalar_hazard){m.parallel->readers[local.slot].page.store(UINTPTR_MAX,std::memory_order_release);scalar_hazard=false;}if(scalar_cell){PageBroker::retire(*scalar_cell,m.page_broker);scalar_cell=nullptr;}m.leave_reader(local);throw;}
        }
        [[gnu::always_inline]] ~Lease(){if(active){if(scalar_hazard){auto& local=memory.local_state();
                memory.parallel->readers[local.slot].page.store(UINTPTR_MAX,std::memory_order_release);
                --local.quiet;--local.leases;memory.leave_reader(local);
            }else finish();}}
        [[gnu::noinline]] void finish(){if(scalar_cell)PageBroker::retire(*scalar_cell,memory.page_broker);pages.reset();auto& local=memory.local_state();--local.quiet;if(!--local.leases){local.leased_pages=nullptr;memory.leave_reader(local);}}
        Lease(const Lease&)=delete;
    };
    CpuSpanList cpu_spans(std::uint64_t address,unsigned size,bool write,bool split=true){
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)throw std::runtime_error("guest transfer outside 32-bit space");
        CpuSpanList spans;auto next=address;unsigned left=size;
        while(left){const auto& map=translate_parallel(next,split?1:left,write);const auto& r=map.region;
            const unsigned n=std::min<std::uint64_t>(left,r.bytes->size()-(next-r.base));
            spans.push_back({r.bytes->data()+next-r.base,n,r.bytes});next+=n;left-=n;}
        return spans;
    }
    std::vector<HostSpan> parallel_spans(std::uint64_t address,unsigned size,bool write,bool split=true){
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)throw std::runtime_error("guest transfer outside 32-bit space");
        std::vector<HostSpan> spans;auto next=address;unsigned left=size;
        while(left){const auto& map=translate_parallel(next,split?1:left,write);const auto& r=map.region;
            const unsigned n=std::min<std::uint64_t>(left,r.bytes->size()-(next-r.base));
            spans.push_back({r.bytes->data()+next-r.base,n,r.bytes});next+=n;left-=n;}
        return spans;
    }
    struct ScalarLock {
        std::unique_lock<ParallelMemory::LineMutex> first,second;
        [[gnu::always_inline]] ~ScalarLock()=default;
        ScalarLock(ScalarLock&&)=default;
        ScalarLock& operator=(ScalarLock&&)=default;
        [[gnu::always_inline]] ScalarLock(GuestMemory& m,const void* p,unsigned n){
            const auto at=reinterpret_cast<std::uintptr_t>(p);
            auto a=(at/128)%m.parallel->lines.size(),b=((at+std::max(n,1u)-1)/128)%m.parallel->lines.size();
            if(a>b)std::swap(a,b);
            first=std::unique_lock<ParallelMemory::LineMutex>(m.parallel->lines[a]);
            if(a!=b)second=std::unique_lock<ParallelMemory::LineMutex>(m.parallel->lines[b]);
        }
    };
    // Reservation tracking is sticky for a physical backing's lifetime. The
    // first LARX publishes interest, fences remote CPUs, then drains only the
    // untracked stores already in flight. All later stores take the stripe.
    // Caller owns the reservation span's stripes; never wait with a CPU byte
    // writer hazard (a normal Lease is fine).
    void track_reservation(const HostSpan& span){
        auto state=parallel->backings.at(span.pin.get()).lock();
        const auto first=(span.pointer-span.pin->data())/128,last=(span.pointer-span.pin->data()+span.size-1)/128;
        bool changed=false;for(auto i=first;i<=last;++i)if(!state->tracked[i].load(std::memory_order_acquire)){
            state->tracked[i].store(true,std::memory_order_release);changed=true;}
        if(!changed || !parallel->asymmetric)return;
        // Sticky like the line flags: stores to this page leave the inline
        // fast path; untracked lines still use the in-section medium path.
        {const auto at=reinterpret_cast<std::uintptr_t>(span.pointer);for(auto page=at>>12;page<=(at+span.size-1)>>12;++page)inline_mark_host(page,InlineFlat::tracked,true);}
        asymmetric_barrier();inline_wait_sections();
        const auto physical=reinterpret_cast<std::uintptr_t>(span.pin->data())/128;
        for(;;){bool busy=false;
            for(unsigned i=0;i<parallel->reader_limit.load(std::memory_order_acquire);++i){
                const auto line=parallel->readers[i].write_line.load(std::memory_order_acquire);
                busy|=line>=physical+first && line<=physical+last;
                const auto page=parallel->readers[i].page.load(std::memory_order_acquire);
                const auto key=page&~ParallelMemory::Reader::scalar_write;
                // A tracked scalar drops its write tag BEFORE taking stripes,
                // so the first LARX never waits on a writer blocked by itself.
                busy|=(page&ParallelMemory::Reader::scalar_write) &&
                    key>=(physical+first)/32 && key<=(physical+last)/32;
            }
            if(!busy)return;
            std::this_thread::yield();
        }
    }
    struct ScalarWrite {
        ParallelMemory::LineMutex* first=nullptr;ParallelMemory::LineMutex* second=nullptr;
        ParallelMemory::Reader* hazard=nullptr;
        bool tracked=true;
        [[gnu::always_inline]] ScalarWrite(GuestMemory& m,const ParallelMemory::Mapping& map,std::uint64_t address,unsigned size){
            auto* pointer=map.region.bytes->data()+address-map.region.base;
            const auto at=reinterpret_cast<std::uintptr_t>(pointer);auto& local=m.local_state();
            if(m.parallel->asymmetric && local.leases && !local.raw && size && (at/128)==((at+size-1)/128)){
                hazard=&m.parallel->readers[local.slot];hazard->write_line.store(at/128,std::memory_order_release);
                std::atomic_signal_fence(std::memory_order_seq_cst);
                tracked=map.state->tracked[(address-map.region.base)/128].load(std::memory_order_acquire);
                if(!tracked)return;
                hazard->write_line.store(UINTPTR_MAX,std::memory_order_release);hazard=nullptr;
            }
            auto a=(at/128)%m.parallel->lines.size(),b=((at+std::max(size,1u)-1)/128)%m.parallel->lines.size();
            if(a>b)std::swap(a,b);
            first=&m.parallel->lines[a];first->lock();
            if(a!=b){second=&m.parallel->lines[b];second->lock();}
        }
        [[gnu::always_inline]] ~ScalarWrite(){if(second)second->unlock();if(first)first->unlock();if(hazard)hazard->write_line.store(UINTPTR_MAX,std::memory_order_release);}
        ScalarWrite(const ScalarWrite&)=delete;
    };
    void bump_scalar(const ParallelMemory::Mapping& map,std::uint64_t address,unsigned size){
        const auto first=(address-map.region.base)/128,last=(address-map.region.base+size-1)/128;
        for(auto i=first;size && i<=last;++i){auto& v=map.state->versions[i];if(v==UINT32_MAX)throw std::runtime_error("guest write version overflow");++v;}
        parallel->notifications->notify(map.region.bytes->data()+address-map.region.base,size);
    }
    struct LineLocks {
        GuestMemory& memory;std::vector<std::unique_lock<ParallelMemory::LineMutex>> locks;
        explicit LineLocks(GuestMemory& m,const std::vector<HostSpan>& spans):memory(m){
            std::vector<unsigned> indices;
            for(const auto& span:spans)if(span.size){const auto first=reinterpret_cast<std::uintptr_t>(span.pointer)/128,last=(reinterpret_cast<std::uintptr_t>(span.pointer)+span.size-1)/128;
                for(auto line=first;line<=last;++line)indices.push_back(line%m.parallel->lines.size());}
            std::sort(indices.begin(),indices.end());indices.erase(std::unique(indices.begin(),indices.end()),indices.end());
            for(auto i:indices)locks.emplace_back(m.parallel->lines[i]);
        }
    };
    HostSpan reservation_span(std::uint64_t address,unsigned size){
        const auto& map=translate_parallel(address,size,false);const auto& r=map.region;
        const auto line=address&~UINT64_C(127),begin=std::max(line,r.base),end=std::min<std::uint64_t>(line+128,r.base+r.bytes->size());
        return {r.bytes->data()+begin-r.base,static_cast<unsigned>(end-begin),r.bytes};
    }
    void bump_parallel(const std::vector<HostSpan>& spans){
        // Caller holds all affected stripe locks (or owner-exclusive raw scope).
        for(const auto& span:spans){
            const auto it=parallel->backings.find(span.pin.get());auto state=it==parallel->backings.end()?nullptr:it->second.lock();
            if(!state)throw std::runtime_error("guest backing without reservation state");
            const auto first=(span.pointer-span.pin->data())/128,last=(span.pointer-span.pin->data()+span.size-1)/128;
            for(auto i=first;span.size && i<=last;++i){if(state->versions[i]==UINT32_MAX)throw std::runtime_error("guest write version overflow");++state->versions[i];}
            parallel->notifications->notify(span.pointer,span.size);
        }
    }
    std::pair<std::uintptr_t,std::uint64_t> parallel_token(std::uint64_t address,unsigned size){
        const auto& map=translate_parallel(address,size,false);const auto& r=map.region;
        const auto pointer=reinterpret_cast<std::uintptr_t>(r.bytes->data()+address-r.base);
        // Backing is page aligned, but a guest mapping may start mid-granule.
        // Preserve the PPU partial-map restriction of the serial path.
        const auto line=address&~UINT64_C(127);
        if(line<r.base || line+128>r.base+r.bytes->size())for(const auto& other:local_state().snapshot->maps)
            if(other.region.identity!=r.identity && line<other.region.base+other.region.bytes->size() && other.region.base<line+128)
                throw std::runtime_error("PPU reservation granule spans guest mappings");
        const auto span=reservation_span(address,size);
        track_reservation(span);
        const auto first=(span.pointer-r.bytes->data())/128,last=(span.pointer-r.bytes->data()+span.size-1)/128;
        std::uint64_t version=0;for(auto i=first;i<=last;++i){version+=map.state->versions[i];if(version>UINT32_MAX)throw std::runtime_error("guest reservation version overflow");}
        return {pointer-(address-line),(local_state().snapshot->epoch<<32)|version};
    }
    // Reservation fast path: the access and its whole 128-byte granule lie in ONE mapping (the common case).
    // The published Mapping holds the same BackingState that parallel->backings names (publish_maps), so the
    // tracked flags and versions are reached without a backing hash lookup, a span vector or a weak_ptr lock.
    // A granule clipped by its mapping keeps the general path. Same stripes, tracking and token as
    // reservation_span + track_reservation + parallel_token; the caller holds reader admission (Lease).
    struct ReservedLine {const ParallelMemory::Mapping* map=nullptr;std::uint8_t* pointer=nullptr;std::size_t first=0,last=0;};
    [[gnu::always_inline]] ReservedLine reserved_line(std::uint64_t address,unsigned size,bool write){
        const auto& map=translate_parallel(address,size,write);const auto& r=map.region;
        const auto line=address&~UINT64_C(127);
        if(line<r.base || line+128>r.base+r.bytes->size())return {};
        auto* pointer=r.bytes->data()+(line-r.base);const auto offset=static_cast<std::size_t>(pointer-r.bytes->data());
        return {&map,pointer,offset/128,(offset+127)/128};
    }
    // Tracking is sticky: only the first reservation of a granule takes track_reservation (fences, drain).
    [[gnu::always_inline]] void reserved_track(const ReservedLine& l){
        const auto& tracked=l.map->state->tracked;
        for(auto i=l.first;i<=l.last;++i)if(!tracked[i].load(std::memory_order_acquire)){track_reservation({l.pointer,128,l.map->region.bytes});return;}
    }
    [[gnu::always_inline]] std::pair<std::uintptr_t,std::uint64_t> reserved_token(const ReservedLine& l){
        reserved_track(l);
        const auto& versions=l.map->state->versions;
        std::uint64_t version=0;for(auto i=l.first;i<=l.last;++i){version+=versions[i];if(version>UINT32_MAX)throw std::runtime_error("guest reservation version overflow");}
        return {reinterpret_cast<std::uintptr_t>(l.pointer),(local_state().snapshot->epoch<<32)|version};
    }
    // bump_parallel for the single span [pointer,pointer+size) of this mapping. Caller holds its stripes.
    [[gnu::always_inline]] void reserved_bump(const ReservedLine& l,std::uint8_t* pointer,unsigned size){
        auto& versions=l.map->state->versions;const auto offset=static_cast<std::size_t>(pointer-l.map->region.bytes->data());
        for(auto i=offset/128;size && i<=(offset+size-1)/128;++i){if(versions[i]==UINT32_MAX)throw std::runtime_error("guest write version overflow");++versions[i];}
        parallel->notifications->notify(pointer,size);
    }
    std::uint64_t enqueue_coherence(const std::shared_ptr<ParallelMemory::Request>& request){
        std::unique_lock<std::mutex> lock(parallel->queue_mutex);
        parallel->queue_space.wait(lock,[&]{return parallel->stopping || parallel->requests.size()<ParallelMemory::queue_capacity;});
        if(parallel->stopping){if(parallel->stop_error)std::rethrow_exception(parallel->stop_error);throw std::runtime_error("guest memory coherence stopped");}
        parallel->requests.push_back(request);parallel->coherence_pending.fetch_add(1,std::memory_order_release);if(parallel->wake_owner)parallel->wake_owner();
        request->ready.wait(lock,[&]{return request->done || parallel->stopping;});
        if(request->error)std::rethrow_exception(request->error);
        if(!request->done){if(parallel->stop_error)std::rethrow_exception(parallel->stop_error);throw std::runtime_error("guest memory coherence stopped");}
        return request->generation;
    }
    std::uint64_t foreign_notify(const void* p,std::uint64_t n,bool write){
        auto request=std::make_shared<ParallelMemory::Request>();request->pointer=p;request->size=n;request->write=write;return enqueue_coherence(request);
    }
    template<class Spans> std::uint64_t notify_spans(const Spans& spans,bool write){
        if(renderer_enabled){
            for(const auto& span:spans)notify_host(span.pointer,span.size,write);
            return parallel->raw_generation.load(std::memory_order_acquire);
        }
        if(!memory_owner()){
            auto request=std::make_shared<ParallelMemory::Request>();request->write=write;
            for(const auto& span:spans)request->spans.push_back({span.pointer,span.size});
            return enqueue_coherence(request);
        }
        if(page_broker.enabled.load() && page_broker.coherence){page_broker.coherence(std::vector<HostSpan>(spans.begin(),spans.end()),write);return parallel->raw_generation.load(std::memory_order_acquire);}
        {RawPermit raw(*this);for(const auto& span:spans)notify_host(span.pointer,span.size,write);}
        return parallel->raw_generation.load(std::memory_order_acquire);
    }
    // Only the RSX owner drains this queue. No queue/data/map lock spans callback,
    // GPU fence or completion wait. A failing callback is returned to its waiter.
    void service_coherence(){
        if(!concurrent())return;
        if(!memory_owner())throw std::runtime_error("foreign coherence service");
        if(!parallel->coherence_pending.load(std::memory_order_acquire))return;
        for(;;){
            std::shared_ptr<ParallelMemory::Request> request;
            {std::lock_guard<std::mutex> lock(parallel->queue_mutex);if(parallel->requests.empty())return;request=parallel->requests.front();parallel->requests.pop_front();parallel->coherence_pending.fetch_sub(1,std::memory_order_release);parallel->queue_space.notify_one();}
            try{HostAccessContext origin(request->origin);HostByteAccess byte_access(request->byte_access,true);
                if(page_broker.enabled.load() && page_broker.coherence){
                    std::vector<HostSpan> spans;
                    const auto inputs=request->spans.empty()?std::vector<std::pair<const void*,std::uint64_t>>{{request->pointer,request->size}}:request->spans;
                    for(const auto& input:inputs){bool found=false;const auto at=reinterpret_cast<std::uintptr_t>(input.first);
                        for(const auto& map:parallel->snapshot->maps){const auto& pin=map.region.bytes;const auto base=reinterpret_cast<std::uintptr_t>(pin->data());
                            if(at>=base && at-base<=pin->size() && input.second<=pin->size()-(at-base)){spans.push_back({const_cast<std::uint8_t*>(static_cast<const std::uint8_t*>(input.first)),static_cast<unsigned>(input.second),pin});found=true;break;}}
                        if(!found)throw std::runtime_error("coherence span mapping revoked");
                    }
                    page_broker.coherence(spans,request->write);
                }else{RawPermit raw(*this);
                    if(request->spans.empty())notify_host(request->pointer,request->size,request->write);
                    else for(const auto& span:request->spans)notify_host(span.first,span.second,request->write);
                }
                request->generation=parallel->raw_generation.load(std::memory_order_acquire);}catch(...){request->error=std::current_exception();}
            {std::lock_guard<std::mutex> lock(parallel->queue_mutex);request->done=true;}request->ready.notify_all();
        }
    }
    // A new worker session after a clean join; pending requests would belong
    // to the stopped session and are never silently dropped.
    void restart_coherence(){
        std::lock_guard<std::mutex> lock(parallel->queue_mutex);
        if(!parallel->requests.empty())throw std::runtime_error("guest memory coherence restart with pending requests");
        parallel->stopping=false;parallel->stop_error=nullptr;
    }
    void stop_coherence(std::exception_ptr error={}){
        std::lock_guard<std::mutex> lock(parallel->queue_mutex);parallel->stopping=true;parallel->stop_error=error;parallel->queue_space.notify_all();
        for(auto& request:parallel->requests)request->ready.notify_all();
    }

    // ---- PPUFAST: flat guest view with an inline admission table ----
    // One PROT_NONE host reservation spans the 32-bit guest space. Each
    // page-aligned guest map gets one more MAP_SHARED view of its memfd
    // (mremap old_size=0 of the coherent alias): the same physical bytes as
    // every byte API, never a copy. One blocker byte per 4 KiB guest page
    // (host page, broker cell, watch cell and mprotect granularity) decides
    // whether translated code may access the view inline. Any blocker sends
    // the access to the unchanged admitted path. Only the map owner runs
    // inline sections; a section is reader admission (exclusive raw/map
    // writers wait for it) plus an odd sequence that blocker writers wait
    // to change after publishing their bit and an asymmetric barrier.
    struct InlineFlat {
        static constexpr std::uint8_t no_load=1,no_store=2,claimed=4,observed=8,tracked=16,dirty=32;
        static constexpr std::uint8_t load_block=no_load|claimed|observed;
        static constexpr std::uint8_t store_block=no_store|claimed|observed|tracked|dirty;
        static constexpr std::uint64_t pages=std::uint64_t(1)<<20,reserve=(std::uint64_t(1)<<32)+0x10000;
        using Table=std::atomic<std::uint8_t>;
        struct Map {std::uint64_t guest,size,bytes;unsigned flags;std::uintptr_t host;std::uint64_t identity;std::shared_ptr<GuestBacking> pin;};
        std::uint8_t* base=nullptr;std::unique_ptr<Table[]> table;std::vector<Map> maps;std::mutex mutex;
        std::uint64_t remaps=0,publications=0;
        InlineFlat()=default;
        // Address-space copies start without a view; the source keeps its own.
        InlineFlat(const InlineFlat&){}
        InlineFlat& operator=(const InlineFlat& other){if(this!=&other)reset();return *this;}
        ~InlineFlat(){reset();}
        void reset(){
#if defined(__linux__)
            if(base)munmap(base,reserve);
#endif
            base=nullptr;table.reset();maps.clear();
        }
        static const Table* blocked(){
            static const std::unique_ptr<Table[]> all=[]{std::unique_ptr<Table[]> t(new Table[pages]);
                for(std::uint64_t i=0;i<pages;++i){t[i].store(0xff,std::memory_order_relaxed);}
                return t;}();
            return all.get();
        }
    } inline_flat;
    // Owner-thread view used by translated code: the live table only while
    // its section is active, otherwise an all-blocked table.
    struct InlineView {
        const InlineFlat::Table* table=InlineFlat::blocked();std::uint8_t* base=nullptr;bool active=false;
        InlineView()=default;InlineView(const InlineView&){}InlineView& operator=(const InlineView&){return *this;}
    } inline_view;
    struct InlineEnabled {
        std::atomic<bool> value{false};InlineEnabled()=default;InlineEnabled(const InlineEnabled&){}
        InlineEnabled& operator=(const InlineEnabled&){value.store(false);return *this;}
        bool load(std::memory_order order)const{return value.load(order);}void store(bool v,std::memory_order order){value.store(v,order);}
    } inline_enabled;
    struct InlineCounters {std::uint64_t medium_loads=0,medium_stores=0,slow_loads=0,slow_stores=0,claim_clears=0,resumes=0,blocked_resumes=0;} inline_counters;
    template<unsigned N> [[gnu::always_inline]] static std::uint64_t inline_read(const std::uint8_t* p){
        if constexpr(N==1)return *reinterpret_cast<const volatile std::uint8_t*>(p);
        else if constexpr(N==2){typedef std::uint16_t U __attribute__((aligned(1)));return __builtin_bswap16(*reinterpret_cast<const volatile U*>(p));}
        else if constexpr(N==4){typedef std::uint32_t U __attribute__((aligned(1)));return __builtin_bswap32(*reinterpret_cast<const volatile U*>(p));}
        else{static_assert(N==8,"inline scalar width");typedef std::uint64_t U __attribute__((aligned(1)));return __builtin_bswap64(*reinterpret_cast<const volatile U*>(p));}
    }
    template<unsigned N> [[gnu::always_inline]] static void inline_write(std::uint8_t* p,std::uint64_t value){
        if constexpr(N==1)*reinterpret_cast<volatile std::uint8_t*>(p)=static_cast<std::uint8_t>(value);
        else if constexpr(N==2){typedef std::uint16_t U __attribute__((aligned(1)));*reinterpret_cast<volatile U*>(p)=__builtin_bswap16(static_cast<std::uint16_t>(value));}
        else if constexpr(N==4){typedef std::uint32_t U __attribute__((aligned(1)));*reinterpret_cast<volatile U*>(p)=__builtin_bswap32(static_cast<std::uint32_t>(value));}
        else{static_assert(N==8,"inline scalar width");typedef std::uint64_t U __attribute__((aligned(1)));*reinterpret_cast<volatile U*>(p)=__builtin_bswap64(value);}
    }
    // In-page check shared by every inline user (the caller holds a section).
    [[gnu::always_inline]] static bool inline_span(std::uint64_t address,unsigned size){
        return size && (address>>32)==0 && (address&4095)+size<=4096;
    }
    // Rebuild under map publication (RawPermit: no section is active).
    void inline_publish(const std::vector<ParallelMemory::Mapping>& next){
#if defined(__linux__) && defined(__x86_64__)
        auto& f=inline_flat;std::lock_guard<std::mutex> lock(f.mutex);
        if(!f.base){
            void* p=mmap(nullptr,InlineFlat::reserve,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE,-1,0);
            if(p==MAP_FAILED)return; // No view: every access keeps the admitted path.
            f.base=static_cast<std::uint8_t*>(p);f.table.reset(new InlineFlat::Table[InlineFlat::pages]);
        }
        ++f.publications;
        std::vector<InlineFlat::Map> want;
        for(const auto& map:next){const auto& r=map.region;const std::uint64_t bytes=r.bytes->size(),size=(bytes+4095)&~std::uint64_t(4095);
            if(!bytes || (r.base&4095) || r.base>UINT32_MAX || size>(std::uint64_t(1)<<32)-r.base || !r.bytes->coherent_data())continue;
            want.push_back({r.base,size,bytes,r.flags,reinterpret_cast<std::uintptr_t>(r.bytes->data()),r.identity,r.bytes});}
        auto same=[](const InlineFlat::Map& a,const InlineFlat::Map& b){return a.guest==b.guest && a.size==b.size && a.identity==b.identity && a.host==b.host && a.pin==b.pin;};
        auto unmap=[&](std::uint64_t guest,std::uint64_t size){
            if(mmap(f.base+guest,size,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE|MAP_FIXED,-1,0)==MAP_FAILED)
                throw std::runtime_error("guest flat view unmap failed");};
        for(const auto& old:f.maps)if(std::none_of(want.begin(),want.end(),[&](const auto& w){return same(old,w);}))unmap(old.guest,old.size);
        std::vector<InlineFlat::Map> mapped;
        for(auto& w:want){
            if(std::none_of(f.maps.begin(),f.maps.end(),[&](const auto& old){return same(old,w);})){
                void* target=f.base+w.guest;
                void* p=mremap(w.pin->coherent_data(),0,w.size,MREMAP_MAYMOVE|MREMAP_FIXED,target);
                if(p!=target){if(p!=MAP_FAILED)munmap(p,w.size);unmap(w.guest,w.size);continue;}
                ++f.remaps;
            }
            mapped.push_back(std::move(w));
        }
        f.maps=std::move(mapped);
        for(std::uint64_t i=0;i<InlineFlat::pages;++i)f.table[i].store(InlineFlat::no_load|InlineFlat::no_store,std::memory_order_relaxed);
        for(const auto& map:f.maps){
            const auto full=map.bytes>>12;
            const std::uint8_t bits=((map.flags&4)?0:InlineFlat::no_load)|(((map.flags&2) && !(map.flags&1))?0:InlineFlat::no_store);
            const ParallelMemory::BackingState* state=nullptr;
            for(const auto& m:next)if(m.region.identity==map.identity){state=m.state.get();break;}
            for(std::uint64_t i=0;i<full;++i){
                std::uint8_t page=bits;
                if(const auto* cell=page_broker.cell((map.host>>12)+i))if(cell->state.load(std::memory_order_acquire)&PageBroker::claimed)page|=InlineFlat::claimed;
                if(state)for(std::uint64_t line=i*32;line<(i+1)*32 && line<state->versions.size();++line)if(state->tracked[line].load(std::memory_order_acquire)){page|=InlineFlat::tracked;break;}
                f.table[(map.guest>>12)+i].store(page,std::memory_order_relaxed);
            }
        }
        std::atomic_thread_fence(std::memory_order_seq_cst);
#else
        (void)next;
#endif
    }
    // Physical host page -> every flat guest alias. Observer/dirty summaries,
    // claims and reservations publish here before their own quiescence.
    void inline_mark_host(std::uintptr_t page,std::uint8_t bit,bool set){
        auto& f=inline_flat;std::lock_guard<std::mutex> lock(f.mutex);if(!f.table)return;
        for(const auto& map:f.maps){const auto first=map.host>>12;
            if(page<first || page-first>=map.size>>12)continue;
            auto& entry=f.table[(map.guest>>12)+(page-first)];
            if(set)entry.fetch_or(bit,std::memory_order_seq_cst);else entry.fetch_and(static_cast<std::uint8_t>(~bit),std::memory_order_seq_cst);
        }
    }
    // A claim bit has no release hook in the broker. The owner clears it
    // lazily under the same mutex that claimers use to publish it, and only
    // while the physical page is observably unclaimed.
    bool inline_clear_claim(std::uint64_t address){
        auto& f=inline_flat;std::lock_guard<std::mutex> lock(f.mutex);if(!f.table)return false;
        const auto guest=address>>12;
        for(const auto& map:f.maps){if(guest<(map.guest>>12) || guest-(map.guest>>12)>=map.size>>12)continue;
            const auto* cell=page_broker.cell((map.host>>12)+guest-(map.guest>>12));
            if(!cell || (cell->state.load(std::memory_order_seq_cst)&PageBroker::claimed))return false;
            f.table[guest].fetch_and(static_cast<std::uint8_t>(~InlineFlat::claimed),std::memory_order_seq_cst);++inline_counters.claim_clears;return true;}
        return false;
    }
    // Caller published its blocker and executed asymmetric_barrier().
    void inline_wait_sections(){
        if(!inline_enabled.load(std::memory_order_acquire))return;
        const auto self=local_state().slot;const auto limit=parallel->reader_limit.load(std::memory_order_acquire);
        for(unsigned i=0;i<limit;++i){if(i==self)continue;
            const auto seen=parallel->readers[i].section.load(std::memory_order_acquire);if(!(seen&1))continue;
            for(unsigned spins=0;parallel->readers[i].section.load(std::memory_order_acquire)==seen;)
                if(++spins>=64){std::this_thread::yield();spins=0;}else __builtin_ia32_pause();
        }
    }
    [[gnu::noinline]] void inline_leave(LocalAccess& local){
        local.inline_active=false;inline_view.active=false;inline_view.table=InlineFlat::blocked();
        auto& reader=parallel->readers[local.slot];reader.section.store(reader.section.load(std::memory_order_relaxed)+1,std::memory_order_release);
        leave_reader(local);
    }
    [[gnu::always_inline]] void inline_suspend(){if(inline_view.active)inline_leave(local_state());}
    [[gnu::always_inline]] bool inline_observer_compatible()const{
        return !host_access_observer || (host_access_observer.filtered && host_access_observer.supports_exact_bytes.load(std::memory_order_acquire));
    }
    // Owner only. Enters reader admission (waits at a closed raw gate) and
    // publishes the odd section sequence before any inline table load.
    [[gnu::noinline]] void inline_resume(){
        if(!inline_enabled.load(std::memory_order_acquire) || inline_view.active || !memory_owner())return;
        auto& local=local_state();
        if(local.inline_active || local.raw || local.leases || local.scalar_batch_active || local.observer_pauses)return;
        if(page_broker.stopped.load(std::memory_order_acquire) || !inline_observer_compatible()){++inline_counters.blocked_resumes;return;}
        enter_reader(local);
        // Observer installation needs RawPermit; re-check after admission.
        if(!inline_observer_compatible() || page_broker.stopped.load(std::memory_order_acquire)){leave_reader(local);++inline_counters.blocked_resumes;return;}
        auto& reader=parallel->readers[local.slot];reader.section.store(reader.section.load(std::memory_order_relaxed)+1,std::memory_order_release);
        std::atomic_signal_fence(std::memory_order_seq_cst);
        local.inline_active=true;inline_view.base=inline_flat.base;inline_view.table=inline_flat.table.get();inline_view.active=true;
        ++inline_counters.resumes;
    }
    [[gnu::always_inline]] void inline_enter_dispatch(){if(!inline_view.active && inline_enabled.load(std::memory_order_relaxed))inline_resume();}
    // In-section medium paths. They never block, never take stripe locks
    // and never call observers, so blocker writers can wait for sections.
    [[gnu::noinline]] bool inline_medium_load(std::uint64_t address,unsigned size,std::uint8_t* out){
        if(!inline_view.active || !inline_span(address,size))return false;
        auto bits=inline_view.table[address>>12].load(std::memory_order_seq_cst);
        if((bits&InlineFlat::claimed) && !(bits&(InlineFlat::load_block&~InlineFlat::claimed)) && inline_clear_claim(address))
            bits=inline_view.table[address>>12].load(std::memory_order_seq_cst);
        if(bits&InlineFlat::load_block)return false;
        atomic_read(inline_view.base+address,out,size);++inline_counters.medium_loads;return true;
    }
    [[gnu::noinline]] bool inline_medium_store(std::uint64_t address,unsigned size,const std::uint8_t* in){
        if(!inline_view.active || !inline_span(address,size))return false;
        auto bits=inline_view.table[address>>12].load(std::memory_order_seq_cst);
        if((bits&InlineFlat::claimed) && !(bits&(InlineFlat::no_store|InlineFlat::observed)) && inline_clear_claim(address))
            bits=inline_view.table[address>>12].load(std::memory_order_seq_cst);
        if(bits&(InlineFlat::no_store|InlineFlat::observed|InlineFlat::claimed))return false;
        std::uint8_t* host=nullptr;
        if(bits&(InlineFlat::tracked|InlineFlat::dirty)){
            // Section admission pinned this snapshot; the page is mapped and
            // writable (no_store clear), so the cached translation cannot throw.
            const auto* found=&translate_parallel(address,size,true);
            const auto offset=address-found->region.base;host=found->region.bytes->data()+offset;
            if(bits&InlineFlat::tracked)for(auto line=offset/128;line<=(offset+size-1)/128;++line)
                if(found->state->tracked[line].load(std::memory_order_acquire))return false;
        }
        atomic_write(inline_view.base+address,in,size);
        if(bits&InlineFlat::dirty)publish_cpu_write(host,size);
        ++inline_counters.medium_stores;return true;
    }
    // Reusable API for other translated/HLE users of the owner thread: one
    // scalar through the flat view if the page admits it, else false.
    template<unsigned N> [[gnu::always_inline]] bool inline_try_load(std::uint64_t address,std::uint64_t& value)const{
        if(!((address>>32)==0 && (N==1 || (address&4095)<=4096-N) && !(inline_view.table[address>>12].load(std::memory_order_relaxed)&InlineFlat::load_block)))return false;
        value=inline_read<N>(inline_view.base+address);return true;
    }
    template<unsigned N> [[gnu::always_inline]] bool inline_try_store(std::uint64_t address,std::uint64_t value){
        if(!((address>>32)==0 && (N==1 || (address&4095)<=4096-N) && !(inline_view.table[address>>12].load(std::memory_order_relaxed)&InlineFlat::store_block)))return false;
        inline_write<N>(inline_view.base+address,value);return true;
    }
'''
