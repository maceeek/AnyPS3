"""C++ support for isolated PPU functions that access guest memory.

The input fingerprint prevents accidental image mixups; it is not authentication.
No host pointer is ever formed from a guest address.
"""

from .par_memory import TYPES as PAR_MEMORY_TYPES
from .host_access import CPP as HOST_ACCESS_CPP
from .host_fault import CPP as HOST_FAULT_CPP
from .guest_backing import CPP as GUEST_BACKING_CPP
from .ppu_memory import CPP as PPU_MEMORY_CPP
from .elf import ElfImage
from .errors import AnyPS3Error

STACK_BASE = 0xD0000000
STACK_SIZE = 0x100000
STACK_POINTER = STACK_BASE + STACK_SIZE - 0x100
MEMORY_LIMIT = 512 * 1024 * 1024


def fingerprint(data: bytes) -> int:
    # CRC32 is an identity guard for local generated harnesses, not a signature.
    import zlib
    return zlib.crc32(data)


def memory_loader(image: ElfImage) -> str:
    segments = sorted((s for s in image.segments if s.type == 1 and s.memory_size),
                      key=lambda s: s.virtual_address)
    if len(image.data) > MEMORY_LIMIT or sum(s.memory_size for s in segments) + STACK_SIZE > MEMORY_LIMIT:
        raise AnyPS3Error("Function harness memory limit (512 MiB) exceeded.")
    end = 0
    for s in segments:
        if s.virtual_address < end:
            raise AnyPS3Error("Overlapping PT_LOAD segments are unsupported by the memory harness.")
        end = s.virtual_address + s.memory_size
        if s.virtual_address < STACK_BASE + STACK_SIZE and STACK_BASE < end:
            raise AnyPS3Error("PT_LOAD overlaps the experimental harness stack.")
    lines = ["static void load_image(GuestMemory& memory, const char* path) {",
             "    std::ifstream file(path, std::ios::binary | std::ios::ate);",
             f'    if (!file || file.tellg() != std::streampos({len(image.data)})) throw std::runtime_error("ELF size mismatch or unreadable input");',
             f"    std::vector<std::uint8_t> bytes({len(image.data)});",
             "    file.seekg(0);",
             '    if (!file.read(reinterpret_cast<char*>(bytes.data()), bytes.size())) throw std::runtime_error("ELF read failed");',
             f'    if (crc32(bytes) != UINT32_C(0x{fingerprint(image.data):08x})) throw std::runtime_error("ELF CRC32 mismatch: regenerate this harness for the input");']
    for s in segments:
        payload = f"bytes.data() + 0x{s.offset:x}" if s.file_size else "nullptr"
        lines.append(f"    memory.map(0x{s.virtual_address:x}, 0x{s.memory_size:x}, {s.flags & 7}, {payload}, 0x{s.file_size:x});")
    lines.extend([f"    memory.map(0x{STACK_BASE:x}, 0x{STACK_SIZE:x}, 6, nullptr, 0);", "}"])
    return "\n".join(lines)


MEMORY_CPP = r'''
#include <cstdint>
#include <vector>
#include <list>
#include <iterator>
#include <fstream>
#include <stdexcept>
#include <algorithm>
#include <utility>
#include <memory>
#include <optional>
#include <map>
#include <unordered_map>
#include <functional>
#include <array>
#include <cstring>
#include <cstdlib>
#include <cstdarg>
#include <mutex>
#include <atomic>
#include <condition_variable>
#include <thread>
#include <deque>
#include <exception>

#if defined(__linux__)
#if defined(__SSE2__)
#include <emmintrin.h>
#endif
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <signal.h>
#include <linux/membarrier.h>
#endif

// Shared between translation units, with bounded storage and no stderr I/O
// unless explicitly requested. Formatting happens only for DMA, not imports.
struct Anyps3TraceHistory {
    static constexpr unsigned capacity=512, width=256;
    mutable std::mutex mutex;
    std::array<std::array<char,width>,capacity> lines{};
    std::uint64_t written=0;
    void append(const char* text){std::lock_guard<std::mutex> lock(mutex);std::snprintf(lines[written++%capacity].data(),width,"%s",text);}
    void dump()const{
        std::lock_guard<std::mutex> lock(mutex);
        if(!written)return;
        std::fprintf(stderr,"TRACE HISTORY last=%llu total=%llu\n",static_cast<unsigned long long>(std::min<std::uint64_t>(written,capacity)),static_cast<unsigned long long>(written));
        for(auto i=written>capacity?written-capacity:0;i<written;++i)std::fputs(lines[i%capacity].data(),stderr);
    }
};
inline Anyps3TraceHistory anyps3_trace_history;
inline bool anyps3_trace_enabled(const char* name){const char* v=std::getenv(name);return v && std::strcmp(v,"1")==0;}
inline void anyps3_trace_dump(){anyps3_trace_history.dump();}
inline void anyps3_trace_import(const char* text){
    anyps3_trace_history.append(text);
    static const bool live=anyps3_trace_enabled("ANYPS3_TRACE_IMPORTS");
    if(live)std::fputs(text,stderr);
}
inline void anyps3_trace_dma(const char* format,...){
    char line[Anyps3TraceHistory::width];va_list args;va_start(args,format);std::vsnprintf(line,sizeof(line),format,args);va_end(args);
    anyps3_trace_history.append(line);
    static const bool live=anyps3_trace_enabled("ANYPS3_SPU_TRACE_DMA");
    if(live)std::fputs(line,stderr);
}

// Guest backings start on a host page and occupy whole host pages, so a
// guest page-aligned range owns whole host pages (HostGuard can protect it
// without allocator-edge readbacks) and host 128-byte lines match guest lines.
''' + GUEST_BACKING_CPP + r'''

''' + HOST_ACCESS_CPP + HOST_FAULT_CPP + r'''
// Fixed-size zeroed table on anonymous mmap storage: the kernel's zero pages replace a value-initialising memset, so
// creating one costs two syscalls and faults only the pages later used, independent of malloc trim/mmap heuristics
// (a 4 MiB std::vector fill per address space dominated tests that create fixture states by the hundred thousand).
template<class T> class ZeroTable {
    static_assert(std::is_trivially_copyable_v<T>);
    std::size_t count=0;T* data=nullptr;
    static T* map(std::size_t n){
        void* p=mmap(nullptr,n*sizeof(T),PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
        if(p==MAP_FAILED)throw std::bad_alloc();
        return static_cast<T*>(p);}
    void release(){if(data)munmap(data,count*sizeof(T));data=nullptr;count=0;}
public:
    explicit ZeroTable(std::size_t n):count(n),data(map(n)){}
    ZeroTable(const ZeroTable& o):count(o.count),data(map(o.count)){std::memcpy(data,o.data,count*sizeof(T));}
    ZeroTable(ZeroTable&& o)noexcept:count(std::exchange(o.count,0)),data(std::exchange(o.data,nullptr)){}
    ZeroTable& operator=(ZeroTable o)noexcept{std::swap(count,o.count);std::swap(data,o.data);return *this;}
    ~ZeroTable(){release();}
    T& operator[](std::size_t i){return data[i];}
    const T& operator[](std::size_t i)const{return data[i];}
    T* begin(){return data;}
    T* end(){return data+count;}
    std::size_t size()const{return count;}
};

struct GuestMemory {
    inline static std::atomic<std::uint64_t> next_region_identity{1};
    static std::uint64_t region_identity(){auto id=next_region_identity.fetch_add(1,std::memory_order_relaxed);if(!id || id==UINT64_MAX)throw std::runtime_error("guest mapping identity overflow");return id;}
    struct Region { std::uint64_t base; unsigned flags; std::shared_ptr<GuestBacking> bytes; std::string origin="map";std::uint64_t identity=region_identity(); };
''' + PAR_MEMORY_TYPES + r'''
    std::vector<Region> regions;
    std::uint64_t allocated = 0;
    std::uint64_t device_allocated = 0;
    std::uint64_t next_allocation = 0x10000000;
    // Write versions exist only for host 128-byte lines that a reservation
    // has observed; tokens are compared only between reservation and its
    // conditional store, so unobserved lines need no counter.
    struct SerialVersions {
        // The inventory remains iterable for texture certification. Reservation
        // insertion is cold; stores use only a physical-line radix, never hash.
        using Inventory=std::unordered_map<std::uintptr_t,std::uint64_t>;
        Inventory inventory;
        struct Node {
            std::array<std::unique_ptr<Node>,512> children{};
            std::array<std::uint64_t*,512> values{};
        };
        std::unique_ptr<Node> root;
        std::uintptr_t cached_block=UINTPTR_MAX;Node* cached_leaf=nullptr;
        SerialVersions()=default;
        SerialVersions(const Inventory& o){for(const auto& item:o)(*this)[item.first]=item.second;}
        SerialVersions(const SerialVersions& o):SerialVersions(o.inventory){}
        SerialVersions(SerialVersions&&)=default;
        SerialVersions& operator=(const SerialVersions& o){if(this!=&o){SerialVersions copy(o);*this=std::move(copy);}return *this;}
        SerialVersions& operator=(SerialVersions&&)=default;
        operator const Inventory&()const{return inventory;}
        SerialVersions& operator=(const Inventory& o){SerialVersions copy(o);*this=std::move(copy);return *this;}
        const std::uint64_t& at(std::uintptr_t line)const{return inventory.at(line);}
        std::uint64_t& at(std::uintptr_t line){return inventory.at(line);}
        bool operator==(const SerialVersions& other)const{return inventory==other.inventory;}
        bool operator!=(const SerialVersions& other)const{return !(*this==other);}
        bool empty()const{return inventory.empty();}
        auto size()const{return inventory.size();}
        auto begin()const{return inventory.begin();}auto end()const{return inventory.end();}
        auto begin(){return inventory.begin();}auto end(){return inventory.end();}
        std::uint64_t& operator[](std::uintptr_t line){
            if(auto* existing=find_line(line))return *existing;
            auto& value=inventory[line];if(!root)root=std::make_unique<Node>();auto* node=root.get();
            for(int shift=54;shift>=9;shift-=9){auto& child=node->children[(line>>shift)&511];if(!child)child=std::make_unique<Node>();node=child.get();}
            node->values[line&511]=&value;cached_block=UINTPTR_MAX;return value;
        }
        [[gnu::always_inline]] std::uint64_t* find_line(std::uintptr_t line){
            const auto block=line>>9;
            if(block!=cached_block){auto* node=root.get();
                for(int shift=54;node && shift>=9;shift-=9)node=node->children[(line>>shift)&511].get();
                cached_block=block;cached_leaf=node;}
            return cached_leaf?cached_leaf->values[line&511]:nullptr;
        }
    } write_versions;
    // A correctness-neutral lookup hint is also inspected by RSX texture
    // certification outside byte admission. Relaxed copyable storage avoids
    // sharing a plain mutable C++ object with legacy HLE pointer lookups.
    struct RegionHint {
        std::atomic<std::size_t> value{0};
        RegionHint()=default;
        RegionHint(const RegionHint& other):value(std::size_t(other)){}
        operator std::size_t()const{return value.load(std::memory_order_relaxed);}
        RegionHint& operator=(std::size_t n){value.store(n,std::memory_order_relaxed);return *this;}
        RegionHint& operator=(const RegionHint& other){return *this=std::size_t(other);}
    } last_region;
    // 4 MiB per address space; zero means cache miss, otherwise index+1.
    // Heap storage keeps PpuState and temporary fixture states off the stack.
    ZeroTable<std::uint32_t> page_regions=ZeroTable<std::uint32_t>(1u<<20);
    // RSX coherence observer. Host spans cover guest aliases too.
    // Raw pointers retained across GPU writes still require separate guards.
    struct HostObserver {
        std::function<void(const void*,std::uint64_t,bool)> callback;
        bool filtered=false;std::atomic<bool> supports_exact_bytes{false},watched{false};
        // Nodes and page cells have address-space lifetime. Only callback
        // replacement (still a RawPermit operation) reclaims them. Watch churn
        // updates atomic byte bitmaps; no reader gate, RMW, or remote fence.
        struct PageBits {
            std::array<std::atomic<std::uint64_t>,64> all{},sync{};
            std::atomic<unsigned> count{0};
            PageBits()=default;
            PageBits(const PageBits& o):count(o.count.load()){
                for(unsigned i=0;i<64;++i){all[i].store(o.all[i].load());sync[i].store(o.sync[i].load());}}
        };
        using WatchKey=std::tuple<unsigned,unsigned,bool>; // [begin,end), polling
        struct Node {
            std::array<std::unique_ptr<Node>,512> children{};
            std::array<std::atomic<Node*>,512> visible_children{};
            std::array<std::unique_ptr<PageBits>,512> cells{};
            std::array<std::atomic<PageBits*>,512> visible_cells{};
            std::map<unsigned,std::map<WatchKey,unsigned>> byte_watches;
            Node()=default;
            Node(const Node& o):byte_watches(o.byte_watches){for(unsigned i=0;i<512;++i){
                if(o.children[i])children[i]=std::make_unique<Node>(*o.children[i]);
                visible_children[i].store(children[i].get());
                if(o.cells[i])cells[i]=std::make_unique<PageBits>(*o.cells[i]);
                visible_cells[i].store(cells[i].get());}}
        };
        std::unique_ptr<Node> root;std::atomic<Node*> visible_root{nullptr};
        // Writer-only diagnostics. Reader lookups never touch these containers.
        std::map<std::uintptr_t,unsigned> pages;
        // Bound by the address space after preparing broker pages. Identity
        // and ownership belong to the destination, so copies never copy this
        // callback and assignments preserve/rebuild the destination summary.
        std::function<void(std::uintptr_t,bool)> page_summary;
        void clear_summary(){std::lock_guard<std::recursive_mutex> lock(writer);if(page_summary)for(const auto& item:pages)page_summary(item.first,false);}
        void refresh_summary(){std::lock_guard<std::recursive_mutex> lock(writer);if(page_summary)for(const auto& item:pages)page_summary(item.first,true);}
        inline static std::atomic<std::uint64_t> next_identity{1};
        const std::uint64_t identity=next_identity.fetch_add(1);
        mutable std::uint64_t revision=0;
        std::atomic<std::uint64_t> lifetime{0};
        mutable std::recursive_mutex writer;
        HostObserver()=default;
        HostObserver(const HostObserver& o){
            std::lock_guard<std::recursive_mutex> lock(o.writer);
            callback=o.callback;filtered=o.filtered;supports_exact_bytes=o.supports_exact_bytes.load(std::memory_order_acquire);watched=o.watched.load(std::memory_order_acquire);
            root=o.root?std::make_unique<Node>(*o.root):nullptr;visible_root.store(root.get());pages=o.pages;
        }
        HostObserver(HostObserver&& o) noexcept{*this=std::move(o);}
        HostObserver& operator=(const HostObserver& o){if(this!=&o){HostObserver next(o);*this=std::move(next);}return *this;}
        HostObserver& operator=(HostObserver&& o) noexcept{if(this!=&o){std::scoped_lock lock(writer,o.writer);clear_summary();o.clear_summary();callback=std::move(o.callback);filtered=o.filtered;supports_exact_bytes.store(o.supports_exact_bytes.load(std::memory_order_acquire),std::memory_order_release);watched.store(o.watched.load(std::memory_order_acquire),std::memory_order_release);root=std::move(o.root);visible_root.store(root.get(),std::memory_order_release);pages=std::move(o.pages);refresh_summary();++lifetime;o.visible_root.store(nullptr);++o.lifetime;invalidate_cache();o.invalidate_cache();}return *this;}
        void invalidate_cache()const{++revision;}
        // Lifecycle mutation requires RawPermit, unlike watch()/watch_poll().
        template<class F,std::enable_if_t<!std::is_same_v<std::decay_t<F>,HostObserver>,int> =0> HostObserver& operator=(F&& f){std::lock_guard<std::recursive_mutex> lock(writer);clear_summary();callback=std::forward<F>(f);filtered=false;supports_exact_bytes=false;watched=false;visible_root.store(nullptr);root.reset();pages.clear();++lifetime;invalidate_cache();return *this;}
        explicit operator bool()const{return bool(callback);}
        [[gnu::always_inline]] const Node* leaf(std::uintptr_t page)const{
            struct Cache {std::uint64_t identity=0,lifetime=0;std::uintptr_t block=0;const Node* leaf=nullptr;};
            thread_local Cache cache;const auto epoch=lifetime.load(std::memory_order_acquire),block=page>>9;
            if(cache.identity==identity && cache.lifetime==epoch && cache.block==block && cache.leaf)return cache.leaf;
            const Node* node=visible_root.load(std::memory_order_acquire);
            for(int shift=45;node && shift>=9;shift-=9)node=node->visible_children[(page>>shift)&511].load(std::memory_order_acquire);
            cache={identity,epoch,block,node};return node;
        }
        [[gnu::always_inline]] bool lookup(const void* p,std::uint64_t n,bool exact,bool sync_only)const{
            if(!filtered)return true;
            if(!n)return false;
            const auto at=reinterpret_cast<std::uintptr_t>(p);
            if(n>UINTPTR_MAX-at)throw std::runtime_error("host observer span overflow");
            const bool bytes=supports_exact_bytes.load(std::memory_order_acquire) && (exact || HostByteAccess::active());
            const auto last=(at+n-1)>>12;
            for(auto page=at>>12;page<=last;){
                const auto* node=leaf(page);const auto end=std::min(last,page|std::uintptr_t(511));
                if(node)for(auto i=page;i<=end;++i){
                    const auto* cell=node->visible_cells[i&511].load(std::memory_order_acquire);
                    if(!cell)continue;
                    if(!bytes){if(cell->count.load(std::memory_order_acquire))return true;continue;}
                    const auto first=std::max(at,i<<12)-(i<<12),limit=std::min(at+n,(i+1)<<12)-(i<<12);
                    const auto& bits=sync_only?cell->sync:cell->all;
                    for(auto word=first>>6;word<=((limit-1)>>6);++word){
                        const unsigned begin=word==(first>>6)?first&63:0,finish=word==((limit-1)>>6)?((limit-1)&63)+1:64;
                        const auto mask=(~UINT64_C(0)<<begin)&(~UINT64_C(0)>>(64-finish));
                        if(bits[word].load(std::memory_order_acquire)&mask)return true;
                    }
                }
                page=end+1;
            }
            return false;
        }
        [[gnu::always_inline]] bool interested(const void* p,std::uint64_t n,bool exact=false)const{return lookup(p,n,exact,false);}
        [[gnu::always_inline]] bool synchronous(const void* p,std::uint64_t n,bool exact=false)const{return lookup(p,n,exact,true);}
        // Scalar callers have already proved a single physical page. Read
        // the same stable atomic cells as lookup(), including a second bitmap
        // word at a 64-byte edge. No cached negative metadata or raw shortcut.
        [[gnu::always_inline]] bool scalar_interested(const void* p,unsigned n,bool sync_only)const{
            if(!filtered)return true;
            const auto at=reinterpret_cast<std::uintptr_t>(p),page=at>>12;
            const auto* node=leaf(page);if(!node)return false;
            const auto* cell=node->visible_cells[page&511].load(std::memory_order_acquire);if(!cell)return false;
            if(!supports_exact_bytes.load(std::memory_order_acquire))return cell->count.load(std::memory_order_acquire)!=0;
            const auto& bits=sync_only?cell->sync:cell->all;const unsigned begin=at&4095,offset=begin&63;
            const auto mask=~UINT64_C(0)<<offset;const auto first=bits[begin>>6].load(std::memory_order_acquire);
            if(offset+n<=64)return (first & mask & (~UINT64_C(0)>>(64-offset-n)))!=0;
            return (first & mask)!=0 || (bits[(begin>>6)+1].load(std::memory_order_acquire)&(~UINT64_C(0)>>(128-offset-n)))!=0;
        }
        void watch(const void* p,std::uint64_t n,bool add=true,bool polling=false){
            if(!n)return;
            const auto at=reinterpret_cast<std::uintptr_t>(p);
            if(n>UINTPTR_MAX-at)throw std::runtime_error("host observer span overflow");
            std::lock_guard<std::recursive_mutex> lock(writer);
            const auto last=(at+n-1)>>12;invalidate_cache();
            for(auto page=at>>12;page<=last;++page){
                if(!root){if(!add)return;root=std::make_unique<Node>();visible_root.store(root.get(),std::memory_order_release);}
                auto* node=root.get();
                for(int shift=45;node && shift>=9;shift-=9){const auto index=(page>>shift)&511;
                    auto& child=node->children[index];if(!child && add){child=std::make_unique<Node>();node->visible_children[index].store(child.get(),std::memory_order_release);}node=child.get();}
                if(!node)continue;
                const auto index=page&511;
                const WatchKey key{unsigned(std::max(at,page<<12)-(page<<12)),unsigned(std::min(at+n,(page+1)<<12)-(page<<12)),polling};
                auto& ranges=node->byte_watches[index];
                if(add){auto& count=ranges[key];if(count==~0u)throw std::runtime_error("host observer watch overflow");++count;}
                else{auto entry=ranges.find(key);if(entry==ranges.end())continue;if(!--entry->second)ranges.erase(entry);}
                if(!node->cells[index]){node->cells[index]=std::make_unique<PageBits>();node->visible_cells[index].store(node->cells[index].get(),std::memory_order_release);}
                auto& cell=*node->cells[index];std::array<std::uint64_t,64> all{},sync{};unsigned total=0;
                for(const auto& range:ranges){if(range.second>~0u-total)throw std::runtime_error("host observer watch overflow");total+=range.second;
                    const auto first=std::get<0>(range.first),end=std::get<1>(range.first);const bool poll=std::get<2>(range.first);
                    for(unsigned word=first>>6;word<=((end-1)>>6);++word){const auto begin=word==(first>>6)?first&63:0,finish=word==((end-1)>>6)?((end-1)&63)+1:64;
                        const auto mask=(~UINT64_C(0)<<begin)&(~UINT64_C(0)>>(64-finish));all[word]|=mask;if(!poll)sync[word]|=mask;}}
                for(unsigned word=0;word<64;++word){cell.all[word].store(all[word],std::memory_order_release);cell.sync[word].store(sync[word],std::memory_order_release);}
                cell.count.store(total,std::memory_order_release);
                if(total)pages[page]=total;else pages.erase(page);
                if(page_summary)page_summary(page,total!=0);
                if(ranges.empty())node->byte_watches.erase(index);
            }
            watched.store(!pages.empty(),std::memory_order_release);
        }
        // Pollable completion words retain their old CPU value until the GPU
        // owner commits under a page claim. Raw pointers remain page-synchronous.
        void watch_poll(const void* p,std::uint64_t n,bool add=true){watch(p,n,add,true);}
        void operator()(const void* p,std::uint64_t n,bool write)const{if(callback && interested(p,n))callback(p,n,write);}
    } host_access_observer;
    bool in_host_access_observer=false;
    // Dirty tokens replace byte invalidation watches, but protected pointers
    // still need owner preflight. Kernel writes return EFAULT instead of
    // delivering SIGSEGV, so recognize their page dependency before exposure.
    bool raw_cpu_dirty_interested(const void* pointer,std::uint64_t size,bool write)const{
        return write && !HostByteAccess::active() && cpu_dirty_filter.watched.load(std::memory_order_acquire) && cpu_dirty_filter.interested(pointer,size);
    }
    [[gnu::always_inline]] std::uint8_t* byte_pointer(const std::shared_ptr<GuestBacking>& pin,const std::uint8_t* p)const{
        return host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)?pin->coherent(p):const_cast<std::uint8_t*>(p);
    }
    [[gnu::always_inline]] void notify_host(const void* pointer,std::uint64_t size,bool write){
        if(concurrent()){notify_parallel(pointer,size,write);return;}
        if(!size || !host_access_observer || observer_paused() || (!host_access_observer.synchronous(pointer,size) && !raw_cpu_dirty_interested(pointer,size,write)))return;
        notify_callback(pointer,size,write);
    }
    [[gnu::noinline]] void notify_parallel(const void* pointer,std::uint64_t size,bool write){
        if(concurrent()){
            const auto it=local_access.find(this);
            if(it!=local_access.end() && it->second.quiet)return;
            // Only the owner reads/mutates observer metadata. The worker sends
            // spans even when no observer is installed, avoiding foreign cache
            // inspection and making installation/removal an owner-only action.
            if(renderer_enabled){
                // Raw service access already owns metadata admission. Elide
                // all owner RPC for unrelated bytes, including raw API calls.
                if(local_state().raw && (!host_access_observer || (!host_access_observer.synchronous(pointer,size) && !raw_cpu_dirty_interested(pointer,size,write))))return;
                if(size && !observer_paused())renderer_access(pointer,size,write);
                return;
            }
            if(!memory_owner()){if(size)foreign_notify(pointer,size,write);return;}
        }
        if(!size || !host_access_observer || observer_paused() || (!host_access_observer.synchronous(pointer,size) && !raw_cpu_dirty_interested(pointer,size,write)))return;
        notify_callback(pointer,size,write);
    }
    [[gnu::noinline]] std::uint8_t* notify_guest_access(std::shared_ptr<GuestBacking> pin,std::uint8_t* pointer,unsigned size,bool write,std::uint64_t address,bool exact_bytes){
        HostByteAccess bytes(exact_bytes && host_access_observer.supports_exact_bytes.load(std::memory_order_acquire),true);
        HostAccessContext::Address origin_address(address);
        if(write)mark_write(static_cast<const std::uint8_t*>(pointer),size);else notify_host(pointer,size,false);
        return exact_bytes && host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)?pin->coherent(pointer):pointer;
    }
    [[gnu::noinline]] std::uint8_t* notify_cached_scalar(std::shared_ptr<GuestBacking> pin,std::uint8_t* pointer,unsigned size,bool write,std::uint64_t address){
        HostByteAccess bytes(host_access_observer.supports_exact_bytes.load(std::memory_order_acquire),true);
        HostAccessContext::Address origin_address(address);
        // The cached path already checked this exact physical watch. Keep
        // the original backing pinned across the callback, as on a miss.
        notify_callback(pointer,size,write);
        if(write)mark_serial_versions(pointer,size);
        return byte_pointer(pin,pointer);
    }
    [[gnu::noinline]] void notify_callback(const void* pointer,std::uint64_t size,bool write){
        if(renderer_enabled){if(!observer_paused())renderer_access(pointer,size,write);return;}
        struct Guard {bool& active;explicit Guard(bool& a):active(a){active=true;}~Guard(){active=false;}};
        auto invoke=[&]{RawPermit raw(*this);Guard guard(in_host_access_observer);host_access_observer.callback(pointer,size,write);};
        if(concurrent() && parallel->owner_coherence)parallel->owner_coherence(invoke);else invoke();
    }
    // Restricted to target acquisition/readback. Never wrap input fetch or an
    // entire draw: those reads must still synchronize previous GPU writes.
    struct ObserverPause {
        GuestMemory& memory;bool previous;
        explicit ObserverPause(GuestMemory& m):memory(m),previous(m.concurrent() && !m.memory_owner()?false:m.in_host_access_observer){if(m.concurrent() && !m.memory_owner())++m.local_state().observer_pauses;else m.in_host_access_observer=true;}
        ~ObserverPause(){if(memory.concurrent() && !memory.memory_owner())--memory.local_state().observer_pauses;else memory.in_host_access_observer=previous;}
        ObserverPause(const ObserverPause&)=delete;
    };
    // Host allocations need not be adjacent when effective addresses are.
    // A Range pins every original backing; it never relocates buffers, bypasses
    // permissions, or writes padding on behalf of a raster operation.
    // Protect retained pointers while GPU ownership spans CPU/SPU turns.
    // Only complete pages strictly inside pinned vector storage are eligible:
    // never protect allocator metadata or another allocation's bytes.
    struct HostGuard {
        struct Span {std::uintptr_t begin,end;};
        // Desired intervals and actually protected intervals stay canonical.
        // FIFO entry is not a CPU memory access: protection survives it.
        std::vector<Span> spans,armed,readonly_spans,armed_readonly;
        std::uint64_t protection_calls=0,protection_bytes=0;
        std::function<void()> synchronize;
        HostGuard* next=nullptr;
        inline static HostGuard* head=nullptr;
#if defined(__linux__)
        inline static struct sigaction previous{};
        inline static bool installed=false;
        long owner_thread=0;
        static std::uintptr_t page_size(){static const auto p=static_cast<std::uintptr_t>(sysconf(_SC_PAGESIZE));return p;}
        static void fault(int sig,siginfo_t* info,void* context){
            const auto at=reinterpret_cast<std::uintptr_t>(info->si_addr);
            const auto saved_errno=errno;const auto routed=NativeHostFaultService::route(at,context);
            if(routed==1){errno=saved_errno;return;}
            if(routed==-1){static const char message[]="RSX protected backing synchronization failed\n";(void)!::write(2,message,sizeof(message)-1);_exit(125);}
            if(routed==-2)
            for(auto* guard=head;guard;guard=guard->next)if(guard->touches_any(reinterpret_cast<const void*>(at),1)){
                // Parallel leases request coherence before touching protected
                // bytes. A foreign raw-pointer fault is outside that protocol:
                // retain the explicit stop instead of invoking Vulkan here.
                if(syscall(SYS_gettid)!=guard->owner_thread){
                    static const char message[]="RSX protected guest backing accessed by foreign host thread\n";
                    (void)!::write(2,message,sizeof(message)-1);_exit(125);
                }
                guard->disarm();
                try{guard->synchronize();}catch(...){
                    // A C++ exception cannot unwind a signal frame safely.
                    // Fail explicitly; never resume with stale guest bytes.
                    static const char message[]="RSX protected backing synchronization failed\n";
                    (void)!::write(2,message,sizeof(message)-1);_exit(125);
                }
                return;
            }
            if(previous.sa_flags&SA_SIGINFO){if(previous.sa_sigaction){previous.sa_sigaction(sig,info,context);return;}}
            else if(previous.sa_handler!=SIG_DFL && previous.sa_handler!=SIG_IGN){previous.sa_handler(sig);return;}
            // Preserve the default action and signal identity for unrelated
            // faults. SIG_IGN is undefined for SIGSEGV, so use default there.
            struct sigaction action{};action.sa_handler=SIG_DFL;sigemptyset(&action.sa_mask);sigaction(sig,&action,nullptr);raise(sig);
        }
#endif
        explicit HostGuard(bool register_faults=true){if(register_faults){next=head;head=this;}}
        HostGuard(const HostGuard&)=delete;
        ~HostGuard(){disarm();auto** p=&head;while(*p && *p!=this)p=&(*p)->next;if(*p)*p=next;}
        bool touches(const void* pointer,std::uint64_t count)const{
            const auto at=reinterpret_cast<std::uintptr_t>(pointer);
            for(const auto& span:armed)if(count && at<span.end && span.begin<at+count)return true;
            return false;
        }
        bool touches_any(const void* pointer,std::uint64_t count)const{
            if(touches(pointer,count))return true;
            const auto at=reinterpret_cast<std::uintptr_t>(pointer);
            for(const auto& span:armed_readonly)if(count && at<span.end && span.begin<at+count)return true;
            return false;
        }
        void protect(Span span,int flags)noexcept{
#if defined(__linux__)
            if(span.begin==span.end)return;
            ++protection_calls;protection_bytes+=span.end-span.begin;
            if(mprotect(reinterpret_cast<void*>(span.begin),span.end-span.begin,flags))_exit(125);
#else
            (void)span;(void)flags;
#endif
        }
        void disarm()noexcept{
#if defined(__linux__)
            for(const auto& span:armed)protect(span,PROT_READ|PROT_WRITE);
            for(const auto& span:armed_readonly)protect(span,PROT_READ|PROT_WRITE);
#endif
            armed.clear();spans.clear();armed_readonly.clear();readonly_spans.clear();
        }
        void add_span(Span span){merge_span(spans,span);}
        void add_readonly_span(Span span){merge_span(readonly_spans,span);}
        static void merge_span(std::vector<Span>& spans,Span span){
            if(span.begin>=span.end)return;
            if(spans.empty() || spans.back().end<span.begin){spans.push_back(span);return;}
            auto first=std::lower_bound(spans.begin(),spans.end(),span.begin,[](const auto& a,std::uintptr_t b){return a.end<b;});
            auto last=first;
            while(last!=spans.end() && last->begin<=span.end){span.begin=std::min(span.begin,last->begin);span.end=std::max(span.end,last->end);++last;}
            if(first==last)spans.insert(first,span);
            else{*first=span;spans.erase(first+1,last);}
        }
        // Difference of two disjoint, ordered interval sets. No per-page map
        // and no sorting on the hot path. Adjacent runs are one syscall.
        template<class F> static void difference(const std::vector<Span>& a,const std::vector<Span>& b,F f){
            std::size_t j=0;
            for(const auto& span:a){auto at=span.begin;while(j<b.size() && b[j].end<=at)++j;
                for(auto k=j;k<b.size() && b[k].begin<span.end;++k){if(at<b[k].begin)f(Span{at,std::min(span.end,b[k].begin)});at=std::max(at,b[k].end);if(at>=span.end)break;}
                if(at<span.end)f(Span{at,span.end});}
        }
        void release(const void* p,std::uint64_t n){
#if defined(__linux__)
            if(!n || !touches_any(p,n))return;
            const auto page=page_size(),at=reinterpret_cast<std::uintptr_t>(p);
            const std::vector<Span> removed{{at&~(page-1),(at+n+page-1)&~(page-1)}};
            std::vector<Span> keep;difference(armed,removed,[&](Span span){keep.push_back(span);});
            difference(armed,keep,[&](Span span){protect(span,PROT_READ|PROT_WRITE);});
            armed=std::move(keep);spans=armed;
            keep.clear();difference(armed_readonly,removed,[&](Span span){keep.push_back(span);});
            difference(armed_readonly,keep,[&](Span span){protect(span,PROT_READ|PROT_WRITE);});
            armed_readonly=std::move(keep);readonly_spans=armed_readonly;
#else
            (void)p;(void)n;
#endif
        }
        bool add(const void* pointer,unsigned count,const GuestBacking& backing,bool read_only=false){
#if defined(__linux__)
            if(!count)return true;
            const auto page=page_size(),at=reinterpret_cast<std::uintptr_t>(pointer),base=reinterpret_cast<std::uintptr_t>(backing.data());
            const auto first=at&~(page-1),last=(at+count+page-1)&~(page-1);
            if(first<base || last>base+backing.size())return false;
            if(read_only)add_readonly_span({first,last});else add_span({first,last});return true;
#else
            (void)pointer;(void)count;(void)backing;(void)read_only;return false;
#endif
        }
        void arm(){
#if defined(__linux__)
            if(spans.empty() && armed.empty() && readonly_spans.empty() && armed_readonly.empty())return;
            if(!installed){struct sigaction action{};action.sa_sigaction=fault;action.sa_flags=SA_SIGINFO;sigemptyset(&action.sa_mask);
                if(sigaction(SIGSEGV,&action,&previous))throw std::runtime_error("RSX SIGSEGV guard installation failed");
                installed=true;}
            owner_thread=syscall(SYS_gettid);
            std::vector<Span> next_read;difference(readonly_spans,spans,[&](Span span){next_read.push_back(span);});
            std::vector<Span> next_all=spans;for(const auto& span:next_read)merge_span(next_all,span);
            difference(armed,next_all,[&](Span span){protect(span,PROT_READ|PROT_WRITE);});
            difference(armed_readonly,next_all,[&](Span span){protect(span,PROT_READ|PROT_WRITE);});
            difference(next_read,armed_readonly,[&](Span span){protect(span,PROT_READ);});
            difference(spans,armed,[&](Span span){protect(span,PROT_NONE);});
            armed=spans;armed_readonly=std::move(next_read);
#endif
        }
    };
    struct Range {
        // Ranges are copyable and may be shared by several readers. A plain
        // mutable epoch would race when they validate the same retained pin.
        struct Epoch {
            std::atomic<std::uint64_t> value{0};
            Epoch()=default;
            Epoch(const Epoch& other):value(other.value.load(std::memory_order_acquire)){}
            Epoch& operator=(const Epoch& other){return *this=static_cast<std::uint64_t>(other);}
            Epoch& operator=(std::uint64_t next){value.store(next,std::memory_order_release);return *this;}
            operator std::uint64_t()const{return value.load(std::memory_order_acquire);}
        };
        struct Part { unsigned offset,size; std::uint8_t* data; std::shared_ptr<GuestBacking> owner;std::uint64_t mapping_id=0; };
        GuestMemory* memory=nullptr;
        unsigned size=0;bool writable=false;std::uint64_t address=0;mutable Epoch epoch;
        std::vector<Part> parts;
        std::uint8_t* pointer(unsigned offset,unsigned count)const{
            memory->require_raw();check_epoch();
            if(offset>size || count>size-offset)throw std::runtime_error("guest range bounds fault");
            for(const auto& part:parts)if(offset>=part.offset && offset-part.offset<part.size && count<=part.size-(offset-part.offset))
                {auto* p=part.data+offset-part.offset;memory->notify_host(p,count,writable);return p;}
            return nullptr;
        }
        void check_epoch()const{
            if(!memory->concurrent() || epoch==memory->parallel->epoch)return;
            for(const auto& part:parts){const auto ea=address+part.offset;
                // A configuration handoff can already have edited the live
                // coordinator vector while retiring old GPU ownership. The
                // admitted immutable snapshot is the authoritative map until
                // publication; never validate readback against future rights.
                const auto& maps=memory->parallel->snapshot->maps;
                const auto it=std::find_if(maps.begin(),maps.end(),[&](const auto& map){const auto& r=map.region;return ea>=r.base && ea-r.base<r.bytes->size();});
                if(it==maps.end())throw std::runtime_error("stale guest range map epoch");
                const auto& r=it->region;
                if(r.identity!=part.mapping_id || r.bytes!=part.owner ||
                    part.size>r.bytes->size()-(ea-r.base) || r.bytes->data()+ea-r.base!=part.data ||
                    !(r.flags&(writable?2:4)) || (writable && (r.flags&1)))throw std::runtime_error("stale guest range map epoch");
            }
            // Caller holds a reader/raw permit, or a claim which excludes
            // map mutation. Publish only after every original pin validates.
            epoch=memory->parallel->epoch;
        }
        void read(unsigned offset,std::uint8_t* out,unsigned count)const{
        HostByteAccess exact_bytes{memory->host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
            Lease lease(*memory,address+offset,count,false,true);check_epoch();
            if(memory->concurrent()){
                if(offset>size || count>size-offset)throw std::runtime_error("guest range bounds fault");
                memory->read_admitted(address+offset,out,count);return;
            }
            if(offset>size || count>size-offset)throw std::runtime_error("guest range bounds fault");
            for(const auto& part:parts){
                const auto begin=std::max(offset,part.offset),end=std::min(offset+count,part.offset+part.size);
                if(begin<end){memory->notify_host(part.data+begin-part.offset,end-begin,false);std::copy_n(memory->byte_pointer(part.owner,part.data+begin-part.offset),end-begin,out+begin-offset);}
            }
        }
        void mark(unsigned offset,unsigned count)const{
            memory->require_raw();check_epoch();
            if(!writable)throw std::runtime_error("guest range is not writable");
            if(offset>size || count>size-offset)throw std::runtime_error("guest range bounds fault");
            for(const auto& part:parts){
                const auto begin=std::max(offset,part.offset),end=std::min(offset+count,part.offset+part.size);
                if(begin<end)memory->mark_write(part.data+begin-part.offset,end-begin);
            }
        }
        void write(unsigned offset,const std::uint8_t* input,unsigned count,bool track=true)const{
        HostByteAccess exact_bytes{memory->host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
            Lease lease(*memory,address+offset,count,true,true);check_epoch();
            if(memory->concurrent()){
                if(!track)throw std::runtime_error("parallel untracked guest write");
                if(!writable)throw std::runtime_error("guest range is not writable");
                if(offset>size || count>size-offset)throw std::runtime_error("guest range bounds fault");
                memory->transfer(address+offset,const_cast<std::uint8_t*>(input),count,true);return;
            }
            if(!writable)throw std::runtime_error("guest range is not writable");
            if(offset>size || count>size-offset)throw std::runtime_error("guest range bounds fault");
            if(track)mark(offset,count);
            for(const auto& part:parts){
                const auto begin=std::max(offset,part.offset),end=std::min(offset+count,part.offset+part.size);
                if(begin<end){memory->notify_host(part.data+begin-part.offset,end-begin,true);std::copy_n(input+begin-offset,end-begin,memory->byte_pointer(part.owner,part.data+begin-part.offset));}
            }
        }
        bool overlaps(const void* pointer,std::uint64_t count)const{
            const auto at=reinterpret_cast<std::uintptr_t>(pointer);
            if(!count)return false;
            for(const auto& part:parts){const auto p=reinterpret_cast<std::uintptr_t>(part.data);
                if(p<at+count && at<p+part.size)return true;}
            return false;
        }
        bool overlaps(const Range& other)const{
            for(const auto& part:other.parts)if(overlaps(part.data,part.size))return true;
            return false;
        }
    };
    // CPU-authoritative cache inputs may opt into deferred invalidation. They
    // must NOT replace synchronous watches for GPU-only surfaces or labels.
    // Registry mutation uses a metadata writer lock, independent of admission.
    // Byte writers consult atomic filters and immutable registry snapshots.
    struct CpuDirtyWatch {
        GuestMemory* memory;std::vector<Range> ranges;
        std::vector<std::pair<std::uintptr_t,std::uintptr_t>> spans;
        std::atomic<bool> dirty{true};bool registered=true;
        explicit CpuDirtyWatch(GuestMemory* m,std::vector<Range> r):memory(m),ranges(std::move(r)){
            for(const auto& range:ranges)for(const auto& part:range.parts)if(part.size){const auto p=reinterpret_cast<std::uintptr_t>(part.data);spans.push_back({p,p+part.size});}
            std::sort(spans.begin(),spans.end());std::size_t n=0;
            for(const auto& span:spans){if(n && span.first<=spans[n-1].second)spans[n-1].second=std::max(spans[n-1].second,span.second);else spans[n++]=span;}spans.resize(n);
        }
        bool overlaps(const void* pointer,unsigned size)const{
            if(!size)return false;
            const auto p=reinterpret_cast<std::uintptr_t>(pointer);
            auto it=std::lower_bound(spans.begin(),spans.end(),p+size,[](const auto& span,std::uintptr_t end){return span.first<end;});
            return it!=spans.begin() && (--it)->second>p;
        }
        bool consume(){return dirty.exchange(false,std::memory_order_acq_rel);}
    };
    HostObserver cpu_dirty_filter=[] {HostObserver filter;filter.filtered=true;filter.supports_exact_bytes=true;return filter;}();
    using CpuDirtyList=std::vector<std::shared_ptr<CpuDirtyWatch>>;
    CpuDirtyList cpu_dirty_watches;
    std::shared_ptr<const CpuDirtyList> gpu_dirty_watches=std::make_shared<const CpuDirtyList>();
    struct CpuDirtyRegistry {
        std::shared_ptr<const CpuDirtyList> current;
        std::atomic<const CpuDirtyList*> published;
        std::vector<std::shared_ptr<const CpuDirtyList>> retired;
        explicit CpuDirtyRegistry(std::shared_ptr<const CpuDirtyList> initial):current(std::move(initial)),published(current.get()){}
    };
    // Inactive address-space copies cannot enable parallel execution (the map
    // snapshot enforces uniqueness); active address spaces are never copied.
    std::shared_ptr<CpuDirtyRegistry> cpu_dirty_registry=std::make_shared<CpuDirtyRegistry>(gpu_dirty_watches);
    void publish_dirty_registry(){
        auto next=std::make_shared<const CpuDirtyList>(cpu_dirty_watches);
        auto& registry=*cpu_dirty_registry;
        // Retain the old list BEFORE publication or anything that may throw.
        registry.retired.push_back(registry.current);
        std::atomic_store_explicit(&gpu_dirty_watches,next,std::memory_order_release);
        registry.current=next;registry.published.store(next.get(),std::memory_order_seq_cst);
        const auto limit=parallel->reader_limit.load(std::memory_order_seq_cst);
        auto it=registry.retired.begin();
        while(it!=registry.retired.end()){
            bool pinned=false;
            for(unsigned i=0;i<limit;++i)if(parallel->readers[i].dirty_snapshot.load(std::memory_order_seq_cst)==it->get()){pinned=true;break;}
            if(pinned)++it;else it=registry.retired.erase(it);
        }
    }
    struct CpuDirtyPin {
        ParallelMemory::Reader& reader;const CpuDirtyList* snapshot=nullptr;
        explicit CpuDirtyPin(GuestMemory& m):reader([&]() -> ParallelMemory::Reader& {
            auto& local=m.local_state();if(local.bound_identity!=m.parallel->identity || local.slot==UINT32_MAX)m.bind_reader(local);
            return m.parallel->readers[local.slot];}()){
            // Classic SC hazard publication + validation. A writer either
            // observes this pin or publication forces the retry; no reader
            // acquires the renderer's mutex or shared_ptr atomic lock.
            do{snapshot=m.cpu_dirty_registry->published.load(std::memory_order_seq_cst);
                reader.dirty_snapshot.store(snapshot,std::memory_order_seq_cst);
            }while(snapshot!=m.cpu_dirty_registry->published.load(std::memory_order_seq_cst));
        }
        ~CpuDirtyPin(){reader.dirty_snapshot.store(nullptr,std::memory_order_release);}
        CpuDirtyPin(const CpuDirtyPin&)=delete;
    };
    std::shared_ptr<CpuDirtyWatch> watch_cpu_dirty(std::vector<Range> ranges){
        if(!concurrent())throw std::runtime_error("deferred dirty watches require concurrent byte API");
        if(ranges.empty())throw std::runtime_error("empty dirty watch registration");
        if(!memory_owner() && !renderer_owner())throw std::runtime_error("foreign dirty watch registration");
        MetadataPermit metadata(*this);
        for(const auto& range:ranges){if(range.memory!=this)throw std::runtime_error("dirty watch belongs to another address space");range.check_epoch();}
        auto watch=std::make_shared<CpuDirtyWatch>(this,std::move(ranges));
        for(const auto& span:watch->spans)cpu_dirty_filter.watch(reinterpret_cast<const void*>(span.first),span.second-span.first);
        cpu_dirty_watches.push_back(watch);
        publish_dirty_registry();return watch;
    }
    std::shared_ptr<CpuDirtyWatch> watch_cpu_dirty(const Range& range){return watch_cpu_dirty(std::vector<Range>{range});}
    void unwatch_cpu_dirty(const std::shared_ptr<CpuDirtyWatch>& watch){
        if(!watch || watch->memory!=this)throw std::runtime_error("invalid dirty watch removal");
        MetadataPermit metadata(*this);
        if(!watch->registered)throw std::runtime_error("retired dirty watch");
        for(const auto& span:watch->spans)cpu_dirty_filter.watch(reinterpret_cast<const void*>(span.first),span.second-span.first,false);
        watch->registered=false;
        cpu_dirty_watches.erase(std::remove(cpu_dirty_watches.begin(),cpu_dirty_watches.end(),watch),cpu_dirty_watches.end());
        publish_dirty_registry();
    }
    // Called AFTER payload release stores, before leaving byte admission or
    // publishing any following PUT/label. A racing consumer leaves a fresh
    // dirty bit for its next use. False sharing only causes extra uploads.
    [[gnu::always_inline]] void publish_cpu_write(const void* pointer,unsigned size){
        if(!size || !cpu_dirty_filter.watched.load(std::memory_order_acquire) || !cpu_dirty_filter.interested(pointer,size,true))return;
        publish_cpu_write_watched(pointer,size);
    }
    [[gnu::noinline]] void publish_cpu_write_watched(const void* pointer,unsigned size){
        CpuDirtyPin pin(*this);
        for(const auto& watch:*pin.snapshot)if(watch->overlaps(pointer,size))watch->dirty.store(true,std::memory_order_release);
    }
    void publish_gpu_write(const void* pointer,unsigned size){
        // GPU claims exclude bytes, not observer metadata permits. A cold
        // immutable registry snapshot keeps concurrent watch removal safe.
        const auto watches=std::atomic_load_explicit(&gpu_dirty_watches,std::memory_order_acquire);
        for(const auto& watch:*watches)if(watch->overlaps(pointer,size))watch->dirty.store(true,std::memory_order_release);
    }
    // One admitted, permission/map-checked read of the complete upload range.
    // This is an immutable byte snapshot, not a promise of atomic multiword
    // guest memory: producers still order their data with PUT/semaphores.
    std::vector<std::uint8_t> snapshot(const Range& range,unsigned offset,unsigned count){
        if(range.memory!=this || offset>range.size || count>range.size-offset)throw std::runtime_error("guest snapshot bounds/address space fault");
        std::vector<std::uint8_t> bytes(count);range.read(offset,bytes.data(),count);return bytes;
    }
    std::string range_diagnostic(std::uint64_t address,unsigned size,bool write)const{
        char head[160];std::snprintf(head,sizeof(head),"unmapped or split guest memory access address=%016" PRIx64 " size=%" PRIu64 " write=%u regions=[",address,std::uint64_t(size),unsigned(write));
        std::string message=head;
        for(const auto& r:regions)if(address<r.base+r.bytes->size() && r.base<address+size){
            char entry[96];std::snprintf(entry,sizeof(entry),"%08" PRIx64 "-%08" PRIx64 " flags=%u ",r.base,r.base+r.bytes->size(),r.flags);
            if(message.back()!='[')message+="; ";
            message+=entry;message+=r.origin;
        }
        return message+"]";
    }
    // Translation-only read Range (vertex inputs of a draw): identical Range, map-epoch/permission checks and observer
    // coherence point as range(address,size,false) -- creation retires GPU-authoritative bytes at this safe point, which the
    // draw relies on before it reserves staging space -- but without page-broker cell admission: nothing is read here.
    Range range_translated(std::uint64_t address,unsigned size){return range(address,size,false,false);}
    Range range(std::uint64_t address,unsigned size,bool write,bool brokered=true){
        Lease lease(*this,address,size,write,true,brokered);
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)
            throw std::runtime_error("guest address outside 32-bit space");
        Range result;result.memory=this;result.size=size;result.writable=write;result.address=address;result.epoch=parallel->epoch;
        if(concurrent()){
            auto next=address;unsigned left=size;
            while(left){const auto& map=translate_parallel(next,1,write);const auto& r=map.region;
                const unsigned n=std::min<std::uint64_t>(left,r.bytes->size()-(next-r.base));
                result.parts.push_back({static_cast<unsigned>(next-address),n,r.bytes->data()+next-r.base,r.bytes,r.identity});next+=n;left-=n;}
            return result;
        }
        auto next=address;unsigned remaining=size;
        // Full prevalidation: even reservation versions remain unchanged on error.
        while(remaining){
            const auto it=std::find_if(regions.begin(),regions.end(),[&](const auto& r){return next>=r.base && next-r.base<r.bytes->size();});
            if(it==regions.end())throw std::runtime_error(range_diagnostic(address,size,write));
            if(!(it->flags&(write?2:4)))throw std::runtime_error("guest memory permission fault");
            if(write && (it->flags&1))throw std::runtime_error("self-modifying code is unsupported");
            const auto count=static_cast<unsigned>(std::min<std::uint64_t>(remaining,it->bytes->size()-(next-it->base)));
            result.parts.push_back({static_cast<unsigned>(next-address),count,it->bytes->data()+next-it->base,it->bytes,it->identity});
            next+=count;remaining-=count;
        }
        if(size){
            const auto& last=result.parts.back();
            for(std::size_t i=0;i<regions.size();++i)if(regions[i].bytes==last.owner && address+last.offset>=regions[i].base &&
                address+last.offset-regions[i].base<regions[i].bytes->size()){last_region=i;break;}
        }
        for(const auto& part:result.parts)notify_host(part.data,part.size,write);
        return result;
    }
    // Only synchronous RSX command scopes enable gathered access(false).
    // Every returned read pointer stays alive through the entire command;
    // repeated reads take fresh snapshots, so no cross-command cache is stale.
    // Writes remain explicit Range writes to the original aliased backing.
    struct SplitReadData {std::vector<std::vector<std::uint8_t>> snapshots;};
    SplitReadData* split_reads=nullptr;
    inline static thread_local std::unordered_map<const GuestMemory*,SplitReadData*> renderer_split_reads;
    SplitReadData*& current_split_reads(){return renderer_owner()?renderer_split_reads[this]:split_reads;}
    struct SplitReadScope {
        GuestMemory& memory;SplitReadData* previous;
        std::unique_ptr<SplitReadData> data=std::make_unique<SplitReadData>();
        explicit SplitReadScope(GuestMemory& m):memory(m),previous(m.current_split_reads()){m.current_split_reads()=data.get();}
        ~SplitReadScope(){memory.current_split_reads()=previous;}
        SplitReadScope(const SplitReadScope&)=delete;
        SplitReadScope& operator=(const SplitReadScope&)=delete;
    };
    [[gnu::noinline]] void mark_write_parallel(const std::uint8_t* pointer,unsigned size){
            auto& local=local_state();if(!local.raw)throw std::runtime_error("parallel raw write outside exclusive scope");
            for(const auto& map:parallel->snapshot->maps){const auto first=reinterpret_cast<std::uintptr_t>(map.region.bytes->data()),at=reinterpret_cast<std::uintptr_t>(pointer);
                if(at>=first && at-first<map.region.bytes->size() && size<=map.region.bytes->size()-(at-first)){
                    bump_parallel({HostSpan{const_cast<std::uint8_t*>(pointer),size,map.region.bytes}});return;}}
            // New maps are initialized before they become visible to readers.
            return;
    }
    // With no observer or observed reservation, a serial mark has no side
    // effects. Keep this predicate inline; the slow path's spills/canary must
    // not tax every scalar store when the parallel executor is disabled.
    void mark_write(const std::uint8_t* pointer,unsigned size) {
        if(!concurrent() && !host_access_observer && write_versions.empty())return;
        mark_write_slow(pointer,size);
    }
    [[gnu::noinline]] void mark_write_slow(const std::uint8_t* pointer,unsigned size) {
        require_raw();
        notify_host(pointer,size,true);
        if(concurrent()){mark_write_parallel(pointer,size);return;}
        mark_serial_versions(pointer,size);
    }
    [[gnu::always_inline]] void mark_serial_versions(const std::uint8_t* pointer,unsigned size) {
        if(!size || write_versions.empty())return;
        const auto first=reinterpret_cast<std::uintptr_t>(pointer)/128;
        const auto last=(reinterpret_cast<std::uintptr_t>(pointer)+size-1)/128;
        if(size<=128){
            if(auto* version=write_versions.find_line(first)){
                if(*version==UINT64_MAX)throw std::runtime_error("guest write version overflow");
                ++*version;
            }
            if(last!=first)if(auto* version=write_versions.find_line(last)){
                if(*version==UINT64_MAX)throw std::runtime_error("guest write version overflow");
                ++*version;
            }
            return;
        }
        if(last-first>=write_versions.size()){
            for(auto& [line,version]:write_versions)if(line>=first && line<=last){if(version==UINT64_MAX)throw std::runtime_error("guest write version overflow");++version;}
        }else{
            for(auto line=first;line<=last;++line)if(auto* version=write_versions.find_line(line)){
                if(*version==UINT64_MAX)throw std::runtime_error("guest write version overflow");
                ++*version;
            }
        }
    }
    std::pair<std::uintptr_t,std::uint64_t> reservation_token(std::uint64_t address) {
        Lease lease(*this,address,128,false);
        if(concurrent()){
            if(!(address&127))if(const auto line=reserved_line(address,128,false);line.map){ScalarLock lock(*this,line.pointer,128);return reserved_token(line);}
            auto span=reservation_span(address,128);ScalarLock lock(*this,span.pointer,span.size);return parallel_token(address,128);}
        const auto pointer=reinterpret_cast<std::uintptr_t>(access_impl(address,128,false,false));
        auto version=write_versions[pointer/128];
        if((pointer+127)/128!=pointer/128){const auto tail=write_versions[(pointer+127)/128];if(tail>UINT64_MAX-version)throw std::runtime_error("guest reservation version overflow");version+=tail;}
        if(concurrent()){
            if(version>UINT64_MAX-parallel->epoch)throw std::runtime_error("guest reservation version overflow");
            version+=parallel->epoch;
        }
        return {pointer,version};
    }
    // PPU lwarx/ldarx: the 128-byte granule is reserved, but only the loaded
    // bytes must be mapped (a segment may end inside the granule). A granule
    // shared with another region would escape write tracking, so it stops.
    std::pair<std::uintptr_t,std::uint64_t> reservation_token(std::uint64_t address,unsigned size) {
        Lease lease(*this,address,size,false);
        if(concurrent()){
            if(const auto line=reserved_line(address,size,false);line.map){ScalarLock lock(*this,line.pointer,128);return reserved_token(line);}
            auto span=reservation_span(address,size);ScalarLock lock(*this,span.pointer,span.size);return parallel_token(address,size);}
        const auto line=address&~UINT64_C(127);
        const auto loaded=reinterpret_cast<std::uintptr_t>(access_impl(address,size,false,false));
        const auto& region=regions[last_region];
        if(line<region.base || line+128>region.base+region.bytes->size())
            for(const auto& other:regions)
                if(&other!=&region && line<other.base+other.bytes->size() && other.base<line+128)
                    throw std::runtime_error("PPU reservation granule spans guest mappings");
        const auto pointer=loaded-(address-line);
        auto version=write_versions[pointer/128];
        if((pointer+127)/128!=pointer/128){const auto tail=write_versions[(pointer+127)/128];if(tail>UINT64_MAX-version)throw std::runtime_error("guest reservation version overflow");version+=tail;}
        if(concurrent()){
            if(version>UINT64_MAX-parallel->epoch)throw std::runtime_error("guest reservation version overflow");
            version+=parallel->epoch;
        }
        return {pointer,version};
    }
    void map(std::uint64_t base, std::size_t size, unsigned flags,
             const std::uint8_t* input, std::size_t file_size, bool device=false) {
        if(concurrent() && !memory_owner())throw std::runtime_error("foreign guest mapping mutation");
        ConfigurationPermit configuration(*this);
        std::lock_guard<std::recursive_mutex> map_lock(parallel->data);
        RawPermit raw(*this);
        if (!size || base > UINT32_MAX || size > UINT64_C(0x100000000) - base ||
            file_size > size || size > UINT64_C(512 * 1024 * 1024) - allocated - device_allocated)
            throw std::runtime_error("invalid or oversized guest mapping");
        for (const auto& r : regions)
            if (base < r.base + r.bytes->size() && r.base < base + size)
                throw std::runtime_error("overlapping guest mapping");
        Region region{base, flags, std::make_shared<GuestBacking>(size, 0)};
        if (file_size) std::copy_n(input, file_size, region.bytes->begin());
        {if(concurrent()){ObserverPause pause(*this);mark_write(region.bytes->data(),size);}else mark_write(region.bytes->data(),size);}
        regions.push_back(std::move(region));mapping_changed();
        if(device)device_allocated+=size;else allocated+=size;
    }
    std::uint64_t allocate(std::size_t size, std::uint64_t alignment = 16) {
        if (!alignment || (alignment & (alignment - 1)) || alignment > 0x100000)
            throw std::runtime_error("invalid allocation alignment");
        auto base = (next_allocation + alignment - 1) & ~(alignment - 1);
        for (;;) {
            bool conflict = false;
            for (const auto& region : regions) {
                if (base < region.base + region.bytes->size() && region.base < base + size) {
                    base = (region.base + region.bytes->size() + alignment - 1) & ~(alignment - 1);
                    conflict = true; break;
                }
            }
            if (!conflict) break;
        }
        map(base, size, 6, nullptr, 0);
        next_allocation = base + size;
        return base;
    }
    void validate(std::uint64_t address,unsigned size,bool write){
        if(!concurrent()){access(address,size,write);return;}
        Lease lease(*this,address,size,write);
        translate_parallel(address,size,write);
    }
    std::uint8_t* access(std::uint64_t address, unsigned size, bool write) {
        require_raw();
        return access_impl(address,size,write,true);
    }
    std::uint8_t* access_impl(std::uint64_t address,unsigned size,bool write,bool gathered_read){
        return access_impl_mode<false>(address,size,write,gathered_read);
    }
    template<bool ExactBytes> std::uint8_t* access_impl_mode(std::uint64_t address,unsigned size,bool write,bool gathered_read) {
        if (address > UINT32_MAX || size > UINT64_C(0x100000000) - address)
            throw std::runtime_error("guest address outside 32-bit space");
        auto hit=[&](std::size_t index)->std::uint8_t*{
            if(index>=regions.size())return nullptr;
            auto& region=regions[index];
            if(address<region.base)return nullptr;
            const auto offset=address-region.base;
            if(offset>=region.bytes->size() || size>region.bytes->size()-offset)return nullptr;
            if(!(region.flags&(write?2:4)))throw std::runtime_error("guest memory permission fault");
            if(write && (region.flags&1))throw std::runtime_error("self-modifying code is unsupported");
            last_region=index;
            auto* pointer=region.bytes->data()+offset;
            // Page-filtered observers register physical host spans; aliases
            // and retained Range accesses use this same notification path.
            if(host_access_observer)return notify_guest_access(region.bytes,pointer,size,write,address,ExactBytes);
            if(write)mark_write(pointer,size);
            return pointer;
        };
        auto& cached=page_regions[address>>12];
        if(cached)if(auto* pointer=hit(cached-1))return pointer;
        if(auto* pointer=hit(last_region)){cached=static_cast<std::uint32_t>(last_region+1);return pointer;}
        for(std::size_t index=0;index<regions.size();++index){
            if(index==last_region)continue;
            if(auto* pointer=hit(index)){cached=static_cast<std::uint32_t>(index+1);return pointer;}
        }
        if(size && !write && gathered_read && current_split_reads()){
            const auto source=range(address,size,false);
            current_split_reads()->snapshots.emplace_back(size);
            auto& snapshot=current_split_reads()->snapshots.back();source.read(0,snapshot.data(),size);
            return snapshot.data();
        }
        throw std::runtime_error(range_diagnostic(address,size,write));
    }
    // Concurrent read body of transfer() for a caller that already holds the Lease of exactly these bytes
    // (Range::read): same translation, permission checks and atomic word reads, without the nested lease
    // revalidation, the span vector and the shared_ptr copies of parallel_spans.
    void read_admitted(std::uint64_t address,std::uint8_t* buffer,unsigned size){
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)throw std::runtime_error("guest transfer outside 32-bit space");
        auto next=address;unsigned left=size;
        while(left){const auto& map=translate_parallel(next,1,false);const auto& r=map.region;
            const unsigned n=std::min<std::uint64_t>(left,r.bytes->size()-(next-r.base));
            atomic_read(byte_pointer(r.bytes,r.bytes->data()+next-r.base),buffer,n);buffer+=n;next+=n;left-=n;}
    }
    void transfer(std::uint64_t address,std::uint8_t* buffer,unsigned size,bool write) {
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        // A transfer inside one guest page is one span: admit it like a scalar access and skip the per-page broker set.
        const bool one_page=size && address<=UINT32_MAX && (address>>12)==((address+size-1)>>12);
        Lease lease(*this,address,size,write,true,true,one_page);
        if(concurrent()){
            if(one_page && !write){read_admitted(address,buffer,size);return;}
            const auto spans=parallel_spans(address,size,write,true);
            if(write){LineLocks locks(*this,spans);bump_parallel(spans);for(const auto& part:spans){atomic_write(byte_pointer(part.pin,part.pointer),buffer,part.size);buffer+=part.size;publish_cpu_write(part.pointer,part.size);}}
            else for(const auto& part:spans){atomic_read(byte_pointer(part.pin,part.pointer),buffer,part.size);buffer+=part.size;}
            return;
        }
        if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)
            throw std::runtime_error("guest transfer outside 32-bit space");
        std::vector<std::pair<std::uint64_t,unsigned>> parts;
        auto next=address;auto remaining=size;
        // Validate the complete guest range before changing bytes or reservation tokens.
        while(remaining){
            const auto region=std::find_if(regions.begin(),regions.end(),[&](const auto& r){return next>=r.base && next-r.base<r.bytes->size();});
            if(region==regions.end())throw std::runtime_error("unmapped guest transfer span at "+std::to_string(next));
            if(!(region->flags&(write?2:4)))throw std::runtime_error("guest memory permission fault");
            if(write && (region->flags&1))throw std::runtime_error("self-modifying code is unsupported");
            const auto count=static_cast<unsigned>(std::min<std::uint64_t>(remaining,region->bytes->size()-(next-region->base)));
            parts.push_back({next,count});next+=count;remaining-=count;
        }
        for(const auto& part:parts){
            auto* host=access_impl_mode<true>(part.first,part.second,write,true);
            if(write)std::copy_n(buffer,part.second,host);else std::copy_n(host,part.second,buffer);
            buffer+=part.second;
        }
    }
    template<unsigned Fixed=0> [[gnu::noinline]] std::uint64_t load_parallel_impl(std::uint64_t address,unsigned size){
        if constexpr(Fixed)size=Fixed;
        ScalarAccess scalar(*this,address,size,false);
        if(scalar.map)return atomic_scalar_read(scalar.payload,size);
        if(const auto* map=owner_scalar(address,size,false)){
            if(size>8)throw std::runtime_error("scalar load exceeds 64 bits");
            return atomic_scalar_read(byte_pointer(map->region.bytes,map->region.bytes->data()+address-map->region.base),size);
        }
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        Lease lease(*this,address,size,false);
        const auto& map=translate_parallel(address,size,false);const auto* pointer=map.region.bytes->data()+address-map.region.base;
        if(size>8)throw std::runtime_error("scalar load exceeds 64 bits");
        return atomic_scalar_read(byte_pointer(map.region.bytes,pointer),size);
    }
    // One out-of-line copy per width: inlining ScalarAccess (EH cleanups,
    // hazards) into every generated load made a game TU need >10 GB in GCC.
    template<unsigned Size> [[gnu::noinline]] std::uint64_t load_parallel_inline(std::uint64_t address){
        {ScalarAccess scalar(*this,address,Size,false,true,true);
            if(scalar.map)return atomic_scalar_read(scalar.payload,Size);}
        return load_parallel_impl<Size>(address,Size);
    }
    // Keep the dynamic entry point for existing callers. Generated PPU loads
    // have constant widths, so their helpers can fold shifts and word masks.
    [[gnu::noinline]] std::uint64_t load_parallel(std::uint64_t address,unsigned size){return load_parallel_impl<0>(address,size);}
    // Validate every hit, including permissions and the physical watch. Most
    // PPU data is outside RSX watches even while an observer is installed.
    // Interested bytes still use the exact observer/origin slow path.
    [[gnu::always_inline]] std::uint8_t* cached_scalar_pointer(std::uint64_t address,unsigned size,bool write){
        if(address>UINT32_MAX || size>8 || size>UINT64_C(0x100000000)-address)return nullptr;
        const auto cached=page_regions[address>>12];
        if(!cached || cached-1>=regions.size())return nullptr;
        const auto index=cached-1;const auto& region=regions[index];
        if(address<region.base)return nullptr;
        const auto offset=address-region.base;
        if(offset>=region.bytes->size() || size>region.bytes->size()-offset ||
           !(region.flags&(write?2:4)) || (write && (region.flags&1)))return nullptr;
        last_region=index;return region.bytes->data()+offset;
    }
    [[gnu::always_inline]] std::uint8_t* observed_scalar_pointer(std::uint64_t address,unsigned size,bool write){
        auto* pointer=cached_scalar_pointer(address,size,write);if(!pointer)return nullptr;
        const auto& pin=regions[page_regions[address>>12]-1].bytes;
        if(size && host_access_observer && !observer_paused() && host_access_observer.synchronous(pointer,size))
            return notify_cached_scalar(pin,pointer,size,write,address);
        if(write)mark_serial_versions(pointer,size);
        return host_access_observer?byte_pointer(pin,pointer):pointer;
    }
    std::uint64_t load(std::uint64_t address,unsigned size){
        if(concurrent()){
            switch(size){
            case 1:return load_parallel_inline<1>(address);
            case 2:return load_parallel_inline<2>(address);
            case 4:return load_parallel_inline<4>(address);
            case 8:return load_parallel_inline<8>(address);
            default:return load_parallel(address,size);
            }
        }
        if(!host_access_observer)if(const auto* p=cached_scalar_pointer(address,size,false))return scalar_bytes_read(p,size);
        return load_observed(address,size);
    }
private:
    [[gnu::noinline]] std::uint64_t load_observed(std::uint64_t address,unsigned size){
        if(const auto* p=observed_scalar_pointer(address,size,false))return scalar_bytes_read(p,size);
        return load_unleased(address,size);
    }
    std::uint64_t load_unleased(std::uint64_t address, unsigned size) {
        return scalar_bytes_read(access_impl_mode<true>(address,size,false,true),size);
    }
    [[gnu::always_inline]] static std::uint64_t scalar_bytes_read(const std::uint8_t* p,unsigned size){
        // memcpy permits unaligned host pointers without aliasing UB.
        switch(size){
        case 1:return *p;
        case 2:{std::uint16_t v;std::memcpy(&v,p,2);return from_be(v);}
        case 4:{std::uint32_t v;std::memcpy(&v,p,4);return from_be(v);}
        case 8:{std::uint64_t v;std::memcpy(&v,p,8);return from_be(v);}
        default:{std::uint64_t value=0;for(unsigned i=0;i<size;++i)value=(value<<8)|p[i];return value;}
        }
    }
public:
    std::uint64_t load_reserved(std::uint64_t address,unsigned size,std::pair<std::uintptr_t,std::uint64_t>& token){
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        Lease lease(*this,address,size,false);
        if(concurrent()){
            if(const auto line=reserved_line(address,size,false);line.map){
                ScalarLock lock(*this,line.pointer,128);reserved_track(line);
                const auto value=load(address,size);token=reserved_token(line);return value;}
            const auto span=reservation_span(address,size);ScalarLock lock(*this,span.pointer,span.size);
            track_reservation(span);
            const auto value=load(address,size);token=parallel_token(address,size);return value;
        }
        auto value=load(address,size);token=reservation_token(address,size);return value;
    }
    bool store_conditional(std::uint64_t address,unsigned size,std::uint64_t value,const std::pair<std::uintptr_t,std::uint64_t>& token){
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        Lease lease(*this,address,size,true);
        if(concurrent()){
            // Fast path: write then read permission, as parallel_spans + reservation_span check them.
            if(const auto line=reserved_line(address,size,true);line.map){
                if(!(line.map->region.flags&4))throw std::runtime_error("guest memory permission fault");
                ScalarLock lock(*this,line.pointer,128);
                if(token!=reserved_token(line))return false;
                std::uint8_t bytes[8]{};if(size>8)throw std::runtime_error("scalar store exceeds 64 bits");
                for(unsigned i=size;i--;){bytes[i]=value;value>>=8;}
                auto* pointer=line.map->region.bytes->data()+(address-line.map->region.base);
                reserved_bump(line,pointer,size);atomic_write(byte_pointer(line.map->region.bytes,pointer),bytes,size);publish_cpu_write(pointer,size);return true;}
            const auto spans=parallel_spans(address,size,true,false);const auto reserved=reservation_span(address,size);ScalarLock lock(*this,reserved.pointer,reserved.size);
            if(token!=parallel_token(address,size))return false;
            std::uint8_t bytes[8]{};if(size>8)throw std::runtime_error("scalar store exceeds 64 bits");
            for(unsigned i=size;i--;){bytes[i]=value;value>>=8;}
            bump_parallel(spans);atomic_write(byte_pointer(spans[0].pin,spans[0].pointer),bytes,size);publish_cpu_write(spans[0].pointer,size);return true;
        }
        if(token!=reservation_token(address,size))return false;
        store(address,size,value);return true;
    }
    void getllar(std::uint64_t address,std::uint8_t* out,std::pair<std::uintptr_t,std::uint64_t>& token){
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        Lease lease(*this,address,128,false);
        if(concurrent()){
            if(!(address&127))if(const auto line=reserved_line(address,128,false);line.map){
                ScalarLock lock(*this,line.pointer,128);reserved_track(line);
                atomic_read(byte_pointer(line.map->region.bytes,line.pointer),out,128);token=reserved_token(line);return;}
            const auto spans=parallel_spans(address,128,false,false);LineLocks locks(*this,spans);
            track_reservation(reservation_span(address,128));
            atomic_read(byte_pointer(spans[0].pin,spans[0].pointer),out,128);token=parallel_token(address,128);return;
        }
        transfer(address,out,128,false);token=reservation_token(address);
    }
    bool putllc(std::uint64_t address,std::uint8_t* input,const std::pair<std::uintptr_t,std::uint64_t>& token,bool unconditional=false){
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        Lease lease(*this,address,128,true);
        if(concurrent()){
            if(!(address&127))if(const auto line=reserved_line(address,128,true);line.map){
                ScalarLock lock(*this,line.pointer,128);
                if(!unconditional){if(!(line.map->region.flags&4))throw std::runtime_error("guest memory permission fault");if(token!=reserved_token(line))return false;}
                reserved_bump(line,line.pointer,128);atomic_write(byte_pointer(line.map->region.bytes,line.pointer),input,128);publish_cpu_write(line.pointer,128);return true;}
            const auto spans=parallel_spans(address,128,true,false);LineLocks locks(*this,spans);
            if(!unconditional && token!=parallel_token(address,128))return false;
            bump_parallel(spans);atomic_write(byte_pointer(spans[0].pin,spans[0].pointer),input,128);publish_cpu_write(spans[0].pointer,128);return true;
        }
        if(!unconditional && token!=reservation_token(address))return false;
        transfer(address,input,128,true);return true;
    }
    static std::uint16_t from_be(std::uint16_t v){
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
        return __builtin_bswap16(v);
#else
        return v;
#endif
    }
    static std::uint32_t from_be(std::uint32_t v){
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
        return __builtin_bswap32(v);
#else
        return v;
#endif
    }
    static std::uint64_t from_be(std::uint64_t v){
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
        return __builtin_bswap64(v);
#else
        return v;
#endif
    }
    template<unsigned Fixed=0> [[gnu::noinline]] void store_parallel_impl(std::uint64_t address,unsigned size,std::uint64_t value){
        if constexpr(Fixed)size=Fixed;
        ScalarAccess scalar(*this,address,size,true);
        if(scalar.map){const auto& map=*scalar.map;auto* pointer=map.region.bytes->data()+address-map.region.base;
            ScalarWrite write(*this,map,address,size);if(write.tracked)bump_scalar(map,address,size);
            atomic_scalar_write(byte_pointer(map.region.bytes,pointer),size,value);publish_cpu_write(pointer,size);return;}
        if(const auto* map=owner_scalar(address,size,true)){
            if(size>8)throw std::runtime_error("scalar store exceeds 64 bits");
            auto* pointer=map->region.bytes->data()+address-map->region.base;ScalarLock lock(*this,pointer,size);
            bump_scalar(*map,address,size);atomic_scalar_write(byte_pointer(map->region.bytes,pointer),size,value);publish_cpu_write(pointer,size);return;
        }
        HostByteAccess exact_bytes{host_access_observer.supports_exact_bytes.load(std::memory_order_acquire)};
        Lease lease(*this,address,size,true);
        const auto& map=translate_parallel(address,size,true);auto* pointer=map.region.bytes->data()+address-map.region.base;
        if(size>8)throw std::runtime_error("scalar store exceeds 64 bits");
        ScalarWrite write(*this,map,address,size);
        if(write.tracked)bump_scalar(map,address,size);
        atomic_scalar_write(byte_pointer(map.region.bytes,pointer),size,value);
        publish_cpu_write(pointer,size);
    }
    [[gnu::noinline]] void store_scalar_admitted(const ParallelMemory::Mapping& map,std::uint64_t address,unsigned size,std::uint64_t value){
        ScalarWrite write(*this,map,address,size);if(write.tracked)bump_scalar(map,address,size);
        auto* pointer=map.region.bytes->data()+address-map.region.base;
        atomic_scalar_write(byte_pointer(map.region.bytes,pointer),size,value);publish_cpu_write(pointer,size);
    }
    // Out of line for the same compile-cost reason as load_parallel_inline.
    template<unsigned Size> [[gnu::noinline]] void store_parallel_inline(std::uint64_t address,std::uint64_t value){
        {ScalarAccess scalar(*this,address,Size,true,true,true);
            if(scalar.map){auto* pointer=scalar.pointer;
                const auto at=reinterpret_cast<std::uintptr_t>(pointer);
                if(at/128==(at+Size-1)/128){
                    if(!scalar.tracked->load(std::memory_order_acquire)){
                        atomic_scalar_write(scalar.payload,Size,value);
                        // Dirty notification is after payload, while BOTH page
                        // and reservation-writer hazards still protect it.
                        if(scalar.cell->dirty.load(std::memory_order_acquire))publish_cpu_write_watched(pointer,Size);
                        return;
                    }
                }
                // Retain the page/map pin but retire untracked-write interest
                // before the stripe lock (first LARX may own that stripe).
                parallel->readers[scalar.local->slot].page.store(at>>12,std::memory_order_release);
                store_scalar_admitted(*scalar.map,address,Size,value);return;
            }}
        store_parallel_impl<Size>(address,Size,value);
    }
    [[gnu::noinline]] void store_parallel(std::uint64_t address,unsigned size,std::uint64_t value){store_parallel_impl<0>(address,size,value);}
    void store(std::uint64_t address,unsigned size,std::uint64_t value){
        if(concurrent()){
            switch(size){
            case 1:store_parallel_inline<1>(address,value);return;
            case 2:store_parallel_inline<2>(address,value);return;
            case 4:store_parallel_inline<4>(address,value);return;
            case 8:store_parallel_inline<8>(address,value);return;
            default:store_parallel(address,size,value);return;
            }
        }
        if(!host_access_observer && write_versions.empty())if(auto* p=cached_scalar_pointer(address,size,true)){scalar_bytes_write(p,size,value);return;}
        store_observed(address,size,value);
    }
private:
    [[gnu::noinline]] void store_observed(std::uint64_t address,unsigned size,std::uint64_t value){
        if(auto* p=observed_scalar_pointer(address,size,true)){scalar_bytes_write(p,size,value);return;}
        store_unleased(address,size,value);
    }
    void store_unleased(std::uint64_t address, unsigned size, std::uint64_t value) {
        scalar_bytes_write(access_impl_mode<true>(address,size,true,true),size,value);
    }
    [[gnu::always_inline]] static void scalar_bytes_write(std::uint8_t* p,unsigned size,std::uint64_t value){
        switch(size){
        case 1:*p=static_cast<std::uint8_t>(value);break;
        case 2:{const auto v=from_be(static_cast<std::uint16_t>(value));std::memcpy(p,&v,2);break;}
        case 4:{const auto v=from_be(static_cast<std::uint32_t>(value));std::memcpy(p,&v,4);break;}
        case 8:{const auto v=from_be(value);std::memcpy(p,&v,8);break;}
        default:for(unsigned i=size;i>0;--i){p[i-1]=value&255;value>>=8;}
        }
    }
};

''' + PPU_MEMORY_CPP + r'''
static std::uint32_t crc32(const std::vector<std::uint8_t>& bytes) {
    std::uint32_t crc = UINT32_MAX;
    for (auto byte : bytes) {
        crc ^= byte;
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & (0U - (crc & 1U)));
    }
    return ~crc;
}
'''
