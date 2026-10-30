"""Physical page ownership API shared by CPU, raw adapters and future RSX owner.

Claim transactions never wait while holding a partial page set. Payload access
still uses the uniform atomic word ABI; page ownership is a separate lifetime.
"""
from .par_gpu import MEMBERS as GPU_COMMIT_MEMBERS

TYPES = r'''
    using CpuPageList=SmallList<std::uintptr_t>;
    using CpuSpanList=SmallList<HostSpan,2>;
    struct PageBroker {
        static constexpr std::uint32_t claimed=UINT32_C(0x80000000);
        struct Cell {
            std::atomic<std::uint32_t> state{0};std::atomic<std::uint64_t> exclusive{0};
            // Stable physical-page summaries, NOT cached negative lookups.
            // Watch publication updates these cells even for warmed aliases.
            std::atomic<bool> observed{false},dirty{false};
        };
        struct Node {std::array<std::atomic<Node*>,512> children{};std::array<Cell,512> cells{};};
        // Nodes live until broker destruction, including after unmap. Map owner
        // installs them before reader publication; atomic links also allow a
        // retiring GPU claim to release while an unrelated map is published.
        std::unique_ptr<Node> root=std::make_unique<Node>();std::vector<std::unique_ptr<Node>> nodes;
        inline static std::atomic<std::uint64_t> next_identity{1};
        const std::uint64_t identity=next_identity.fetch_add(1);
        mutable std::mutex mutex;std::condition_variable changed;
        std::atomic<bool> enabled{false},stopped{false};
        std::thread::id map_owner,delegated_owner;
        std::uint64_t next_token=1;
        std::atomic<std::uint64_t> generation{0};std::atomic<unsigned> waiters{0};
        unsigned owners=0;bool stopping=false;std::exception_ptr error;
        std::function<void()> pump;
        std::function<void(const std::vector<HostSpan>&,bool)> coherence;
        std::function<void(const std::vector<std::uintptr_t>&)> quiesce;
        PageBroker()=default;
        PageBroker(const PageBroker& other){std::lock_guard<std::mutex> lock(other.mutex);if(other.enabled.load() || other.owners)throw std::runtime_error("copy of active page broker");}
        PageBroker& operator=(const PageBroker& other){
            if(this==&other)return *this;
            std::scoped_lock lock(mutex,other.mutex);
            if(enabled.load() || other.enabled.load() || owners || other.owners)throw std::runtime_error("copy of active page broker");
            // Preserve nodes: TLS caches may still name them. No active claim
            // or CPU reader exists when an inactive address space is copied.
            next_token=1;generation=0;stopping=false;stopped=false;error=nullptr;pump={};coherence={};return *this;
        }
        void prepare(const void* pointer,std::uint64_t size){
            if(!size)return;
            const auto at=reinterpret_cast<std::uintptr_t>(pointer);
            std::lock_guard<std::mutex> lock(mutex);
            for(auto page=at>>12;page<=(at+size-1)>>12;page=(page|std::uintptr_t(511))+1){
                auto* node=root.get();for(int shift=45;shift>=9;shift-=9){auto& link=node->children[(page>>shift)&511];auto* child=link.load(std::memory_order_acquire);
                    if(!child){auto allocated=std::make_unique<Node>();child=allocated.get();nodes.push_back(std::move(allocated));link.store(child,std::memory_order_release);}node=child;}
            }
        }
        [[gnu::always_inline]] Cell* cell(std::uintptr_t page)const{
            // Cache only positive leaves, so publication never leaves a stale
            // negative hint. Identity prevents reuse after broker destruction.
            // Four fully associative entries (2 MiB host blocks, round-robin refill): alternating pages of different backings
            // (the RSX FIFO page and its GET/control page, a draw's buffers) no longer evict each other on every access (one
            // entry walked 5 levels per GET store). A leaf, once linked, stays linked to the same block for the broker's lifetime.
            // Identity 0 is never a broker's, so an empty entry never matches.
            struct Cache {std::uint64_t identity=0;std::uintptr_t block=0;Node* leaf=nullptr;};
            struct Caches {std::array<Cache,4> entry{};unsigned next=0;};thread_local Caches caches;
            const auto block=page>>9;
            for(const auto& cache:caches.entry)if(cache.identity==identity && cache.block==block)return &cache.leaf->cells[page&511];
            auto* node=root.get();
            for(int shift=45;node && shift>=9;shift-=9)node=node->children[(page>>shift)&511].load(std::memory_order_acquire);
            if(!node)return nullptr;
            caches.entry[caches.next++&3]={identity,block,node};
            return &node->cells[page&511];
        }
        bool owner_unclaimed(const void* pointer,unsigned size){
            if(stopped.load(std::memory_order_acquire))return false;
            const auto at=reinterpret_cast<std::uintptr_t>(pointer);if(!size)return true;
            for(auto page=at>>12;page<=(at+size-1)>>12;++page){const auto* entry=cell(page);if(!entry || (entry->state.load(std::memory_order_acquire)&claimed))return false;}
            return true;
        }
        template<class Spans> static CpuPageList cpu_keys(const Spans& spans){
            CpuPageList result;
            for(const auto& span:spans){const auto at=reinterpret_cast<std::uintptr_t>(span.pointer),base=reinterpret_cast<std::uintptr_t>(span.pin->data());
                if(at<base || at-base>span.pin->size() || span.size>span.pin->size()-(at-base))throw std::runtime_error("broker span outside backing");
                if(span.size)for(auto page=at>>12;page<=(at+span.size-1)>>12;++page)result.push_back(page);}
            if(result.size()>1)result.unique_sort();
            return result;
        }
        static std::vector<std::uintptr_t> keys(const std::vector<HostSpan>& spans){auto list=cpu_keys(spans);return {list.begin(),list.end()};}
        void check()const{if(error)std::rethrow_exception(error);if(stopping)throw std::runtime_error("guest page broker stopped");}
        [[gnu::noinline]] void check_stopped()const{std::lock_guard<std::mutex> lock(mutex);check();}
        [[gnu::always_inline]] void check_fast()const{if(stopped.load(std::memory_order_acquire))check_stopped();}
        void signal_cpu()noexcept{
            generation.fetch_add(1,std::memory_order_release);
            if(waiters.load(std::memory_order_seq_cst)){std::lock_guard<std::mutex> lock(mutex);changed.notify_all();}
        }
        static void retire(Cell& entry,PageBroker& broker)noexcept{
            const auto old=entry.state.fetch_sub(1,std::memory_order_release);
            if(!(old&~claimed))std::terminate();
            if(old==(claimed|1u))broker.signal_cpu();
        }
        template<class Pages> bool try_cpu(const Pages& pages,SmallList<Cell*>& admitted){
            check_fast();
            // Resolve/allocate the complete small-buffer list before admission.
            // An allocation or invalid page cannot strand an acquired prefix.
            for(auto page:pages){auto* entry=cell(page);if(!entry)throw std::runtime_error("unprepared guest broker page");admitted.push_back(entry);}
            unsigned acquired=0;
            try{for(auto* entry:admitted){auto state=entry->state.load(std::memory_order_relaxed);
                    for(;;){if(state&claimed){for(unsigned i=0;i<acquired;++i)retire(*admitted[i],*this);admitted={};return false;}
                        if(state==claimed-1)throw std::runtime_error("guest page reader overflow");
                        if(entry->state.compare_exchange_weak(state,state+1,std::memory_order_acquire,std::memory_order_relaxed))break;}
                    ++acquired;
                }
                check_fast();return true;
            }catch(...){for(unsigned i=0;i<acquired;++i)retire(*admitted[i],*this);admitted={};throw;}
        }
        void release_cpu(const SmallList<Cell*>& admitted)noexcept{for(auto* entry:admitted)retire(*entry,*this);}
        void wait_change(bool owner=false){
            std::unique_lock<std::mutex> lock(mutex);check();++waiters;const auto seen=generation.load(std::memory_order_acquire);lock.unlock();
            try{if(owner && pump)pump();lock.lock();check();
                changed.wait_for(lock,std::chrono::milliseconds(1),[&]{return stopping || generation.load(std::memory_order_acquire)!=seen;});check();--waiters;
            }catch(...){if(!lock.owns_lock())lock.lock();--waiters;throw;}
        }
        // `publication`: held (never across the wait below) while the claim bits are published, so a claim never appears inside
        // a scope holding that lock (GuestMemory::metadata_mutex: RSX blit admission-to-access stability, rsx_blit.py).
        std::uint64_t acquire(const std::vector<std::uintptr_t>& pages,bool delegated=false,std::recursive_mutex* publication=nullptr){
            if(map_owner!=std::thread::id{} && map_owner!=std::this_thread::get_id() && !(delegated && delegated_owner==std::this_thread::get_id()))throw std::runtime_error("foreign page claim acquisition");
            std::uint64_t token=0;
            for(;;){
                {std::unique_lock<std::recursive_mutex> metadata;if(publication)metadata=std::unique_lock<std::recursive_mutex>(*publication);
                    std::lock_guard<std::mutex> lock(mutex);check();bool busy=false;
                    for(auto page:pages){const auto* entry=cell(page);if(!entry)throw std::runtime_error("unprepared guest broker claim");busy|=(entry->state.load(std::memory_order_acquire)&claimed)!=0;}
                    if(!busy){if(next_token==UINT64_MAX)throw std::runtime_error("guest page ownership token overflow");token=next_token++;
                        for(auto page:pages){auto* entry=cell(page);entry->exclusive.store(token,std::memory_order_relaxed);entry->state.fetch_or(claimed,std::memory_order_acq_rel);}++owners;break;}}
                wait_change(true);
            }
            try{if(quiesce)quiesce(pages);
                for(;;){check_fast();bool busy=false;for(auto page:pages)busy|=(cell(page)->state.load(std::memory_order_acquire)&~claimed)!=0;if(!busy)return token;wait_change(true);}}
            catch(...){release(pages,token);throw;}
        }
        void release(const std::vector<std::uintptr_t>& pages,std::uint64_t token)noexcept{
            std::lock_guard<std::mutex> lock(mutex);
            for(auto page:pages){auto* entry=cell(page);if(!entry || entry->exclusive.load(std::memory_order_relaxed)!=token)std::terminate();
                entry->exclusive.store(0,std::memory_order_relaxed);entry->state.fetch_and(~claimed,std::memory_order_release);}
            if(!owners)std::terminate();
            --owners;generation.fetch_add(1,std::memory_order_release);changed.notify_all();
        }
        void stop(std::exception_ptr reason={}){std::lock_guard<std::mutex> lock(mutex);stopping=true;error=reason;stopped.store(true,std::memory_order_release);generation.fetch_add(1,std::memory_order_release);changed.notify_all();}
    };
    PageBroker page_broker;
    void enable_page_broker(){
        if(page_broker.enabled.load(std::memory_order_acquire))return;
        if(!concurrent())throw std::runtime_error("page broker requires parallel memory ABI");
        RawPermit raw(*this);page_broker.map_owner=parallel->owner;page_broker.enabled.store(true,std::memory_order_release);
    }
    struct CpuPages {
        PageBroker& broker;CpuPageList pages;SmallList<PageBroker::Cell*> cells;bool admitted=false;
        template<class Spans> CpuPages(PageBroker& b,const Spans& spans):broker(b),pages(PageBroker::cpu_keys(spans)){admitted=broker.try_cpu(pages,cells);}
        ~CpuPages(){if(admitted)broker.release_cpu(cells);}
        CpuPages(const CpuPages&)=delete;
    };
    // Move-only persistent GPU ownership. Pins survive map-reader admission;
    // release only after fence/readback/unprotect, never merely after submit.
    struct GpuPages {
        PageBroker* broker=nullptr;GuestMemory* memory=nullptr;std::vector<HostSpan> spans;
        std::vector<std::uintptr_t> pages;std::uint64_t token=0;
        GpuPages()=default;
        GpuPages(GuestMemory& m,std::vector<HostSpan> requested):spans(std::move(requested)){
            if(!m.page_broker.enabled.load())throw std::runtime_error("enable page broker before ownership acquisition");
            auto& local=m.local_state();if(local.leases || local.raw)throw std::runtime_error("page ownership inside CPU/raw admission");
            // Only map owner may translate and pin new claims in this phase.
            if(!m.memory_owner() && !(m.renderer_owner() && (local.fault_claim || local.renderer_claim)))throw std::runtime_error("foreign page ownership requires map owner pin transaction");
            memory=&m;broker=&m.page_broker;GuestMemory::stat_gpu_claims.fetch_add(1,std::memory_order_relaxed);pages=PageBroker::keys(spans);token=broker->acquire(pages,m.renderer_owner() && (local.fault_claim || local.renderer_claim),&m.metadata_mutex.value);
        }
        GpuPages(GpuPages&& o)noexcept:broker(o.broker),memory(o.memory),spans(std::move(o.spans)),pages(std::move(o.pages)),token(o.token),payload_owner(o.payload_owner){o.broker=nullptr;}
        GpuPages& operator=(GpuPages&& o)noexcept{if(this!=&o){reset();broker=o.broker;memory=o.memory;spans=std::move(o.spans);pages=std::move(o.pages);token=o.token;payload_owner=o.payload_owner;o.broker=nullptr;}return *this;}
        GpuPages(const GpuPages&)=delete;
        void mark(unsigned index){
            if(!broker || !memory->memory_owner())throw std::runtime_error("foreign or retired GPU raw commit");
            const auto& span=spans.at(index);LineLocks locks(*memory,{span});memory->bump_parallel({span});
        }
''' + GPU_COMMIT_MEMBERS + r'''
        std::uint8_t* pointer(unsigned index){if(!broker)throw std::runtime_error("retired GPU page pointer");if(!memory->memory_owner())throw std::runtime_error("foreign raw GPU pointer; use claim commit/snapshot");return spans.at(index).pointer;}
        void reset()noexcept{if(broker){broker->release(pages,token);broker=nullptr;spans.clear();}covered_spans=nullptr;covered.clear();}
        ~GpuPages(){reset();}
    };
    // Non-atomic raw accesses must hold the exact same exclusion as GPU
    // ownership. This adapter gives pointers only for its predeclared spans.
    struct RawPages {
        GuestMemory& memory;GpuPages ownership;
        RawPages(GuestMemory& m,std::vector<HostSpan> spans):memory(m),ownership(m,std::move(spans)){}
        std::uint8_t* pointer(unsigned index){return ownership.spans.at(index).pointer;}
        void mark(unsigned index){
            ownership.mark(index);
        }
    };
'''
