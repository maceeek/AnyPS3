"""Opt-in per-frame timeline stamps (FRAMETIME): where does a frame spend its time, which path ends last?

ANYPS3_FRAMETIME=/path/ring.bin enables a fixed-size ring of 64-byte events in a MAP_SHARED file (it survives SIGKILL; the
header's head counter says how many events were ever reserved, the newest min(head, capacity) reservations are the window).
Off by default: the only cost is one predictable branch on a plain bool. ANYPS3_FRAMETIME_EVENTS=N sets the ring capacity
(power of two, default 2^23 = 512 MiB of sparse file), ANYPS3_FRAMETIME_PEEK_REG=N additionally records the big-endian word that guest register rN
points to at every guest block (diagnostic: what does a polling loop wait for?); ANYPS3_FRAMETIME_PEEK_PC=0xADDR limits that
to blocks at guest pc ADDR or ADDR+4 (the system call instruction, depending on where the runtime has advanced pc).

Wire format version 2 (little endian, tools/analysis in bench/frametime/analyze.py; version 1 had 48-byte events without a
commit sequence and could not tell a half-written or overwritten slot from a valid one -- the reader still accepts it):
  header 64 bytes: char magic[8]="ANYPSFT1"; u64 capacity; u64 version=2; u64 head (atomic reservation counter);
                   u64 dropped (atomic: events given up because their slot was busy or already taken by a newer lap); u64 pad[3]
  event 64 bytes:  u64 seq (atomic commit word, see below);
                   u64 ns (CLOCK_MONOTONIC, same clock as the emulated vblank epoch; for spans = START time);
                   u32 kind; u32 tid (Linux thread id); u64 a, b, c, d (meaning per kind below); u64 pad
Spans are emitted once, at their end, with ns = start and b = duration in ns.
Publication protocol (per slot, exclusive ownership): a writer reserves the index i = head.fetch_add(1), then claims slot
i & mask by a compare-and-swap of seq from an even value below 2i+2 (never written, or committed by an older lap) to the odd
2i+1, fills the fields, and publishes seq = 2i+2 with release ordering. If the slot is odd (another writer is inside it, e.g.
a stalled thread from the previous lap) or already holds a lap >= i, the event is dropped and counted in `dropped`: a writer
never waits and never overwrites a slot that another writer owns. A reader accepts window index j only when seq == 2j+2 before
and after copying the fields, so it never returns a half-written event (the writer was killed or is mid-store), an event of
another lap (a delayed writer that committed after the ring wrapped) or a dropped one; it counts those slots as missing.

kind  name        thread       fields
 1  PostFifo     main         a=PUT seen, b=budget
 2  FifoRun      rsx worker   span; a=draws processed, c=flips counter after, d=ns from the last PostFifo to the start
 3  Vblank       rsx worker   a=tick number, b=lateness ns (publication - ideal tick), c=event flags
 4  FlipReq      rsx worker   a=display id, b=mode (2 = vsync), c=vblank ticks seen so far, d=0
 5  FlipVsync    rsx worker   span (the vsync wait), a=deadline ns, c=ticks at deadline
 6  FlipAsync    rsx worker   a=1 async present queued / 0 synchronous fallback, c=display id
 7  GpuSubmit    rsx worker   a=completion id, b=pending draws, c=labels, d=reason (first 8 chars, ASCII little endian)
 8  GpuRetire    rsx worker   a=completion id, b=draws, c=wait flag (1 = blocking collect), d=labels
 9  LabelPub     rsx worker   a=guest address, b=value, c=kind (0 plain,1 flip status,2 flip report,..), d=completion id of the flight
10  HostReq      main/other   span (requester side wait for an RSX owner HostAccess), a=host pointer, c=write flag
11  HostExec     rsx worker   span (owner executing a HostAccess), a=host pointer, b=exec ns, c=queue wait ns
12  Block        guest thr.   a=pc|lr<<32, b=us (usleep) or object id, c=guest thread id|wait kind<<32, d=peeked addr<<32|word
13  Idle         main         span (coordinator idle sleep), a=reason (1 timed, 2 spu wait, 3 rsx wait), c=requested us
14  Service      rsx worker   span (idle service tick: vblank + retire), only >= 50 us
15  Fault        rsx worker   span (host fault service request on the owner), c=queue wait ns
16  DisplayReq   main         span (cellGcm display HLE call waiting on the owner), a=syscall number
17  DisplayExec  rsx worker   span (owner executing it), a=syscall number, c=queue wait ns
18  BarrierReq   main         span (map barrier / maps changed wait), a=1 enter, 2 leave-post, 3 maps changed
19  Switch       main         a=from guest thread, b=to guest thread, c=next pc
20  EventPub     rsx worker   a=event queue id, b=event name, c=flags
21  WorkerWait   rsx worker   span: a=completion id & c=1 (blocking GPU collect, >= 20 us) or a=packed reason & c=2/3 (flush with/without download, >= 100 us)
22  Mark         any          user marker (tests)
"""

CPP = r'''
#ifndef ANYPS3_FRAMETIME_DEFINED
#define ANYPS3_FRAMETIME_DEFINED
#include <sys/mman.h>
#include <sys/syscall.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <functional>
namespace anyps3_ft {
enum Kind : std::uint32_t {PostFifo=1,FifoRun=2,Vblank=3,FlipReq=4,FlipVsync=5,FlipAsync=6,GpuSubmit=7,GpuRetire=8,LabelPub=9,
    HostReq=10,HostExec=11,Block=12,Idle=13,Service=14,Fault=15,DisplayReq=16,DisplayExec=17,BarrierReq=18,Switch=19,EventPub=20,
    WorkerWait=21,Mark=22};
struct alignas(64) Event {std::atomic<std::uint64_t> seq;std::uint64_t ns;std::uint32_t kind,tid;std::uint64_t a,b,c,d,pad;};
static_assert(sizeof(Event)==64 && std::atomic<std::uint64_t>::is_always_lock_free,"frametime event ABI");
struct Header {char magic[8];std::uint64_t capacity,version;std::atomic<std::uint64_t> head,dropped;std::uint64_t pad[3];};
static_assert(sizeof(Header)==64,"frametime header ABI");
inline Header* header=nullptr;
inline Event* ring=nullptr;
inline std::uint64_t ring_mask=0;
inline bool on=false;
inline int peek_register=-1;
inline std::uint32_t peek_pc=0;
inline std::atomic<std::uint64_t> last_post_ns{0};
inline thread_local std::uint64_t current_completion=0;
inline std::uint64_t pack(const char* text){std::uint64_t v=0;for(unsigned i=0;i<8 && text && text[i];++i)v|=std::uint64_t(std::uint8_t(text[i]))<<(8*i);return v;}
inline std::uint64_t now(){timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return std::uint64_t(t.tv_sec)*1000000000ull+std::uint64_t(t.tv_nsec);}
inline std::uint32_t this_tid(){static thread_local const std::uint32_t tid=std::uint32_t(syscall(SYS_gettid));return tid;}
inline bool open_ring(){
    const char* path=std::getenv("ANYPS3_FRAMETIME");if(!path || !*path)return false;
    std::uint64_t capacity=std::uint64_t(1)<<23;
    if(const char* n=std::getenv("ANYPS3_FRAMETIME_EVENTS")){char* end=nullptr;const auto v=std::strtoull(n,&end,10);
        if(end==n || *end || v<1024 || (v&(v-1)) || v>(std::uint64_t(1)<<28)){std::fprintf(stderr,"invalid ANYPS3_FRAMETIME_EVENTS\n");std::_Exit(2);}capacity=v;}
    if(const char* r=std::getenv("ANYPS3_FRAMETIME_PEEK_REG")){char* end=nullptr;const auto v=std::strtol(r,&end,10);
        if(end==r || *end || v<0 || v>31){std::fprintf(stderr,"invalid ANYPS3_FRAMETIME_PEEK_REG\n");std::_Exit(2);}peek_register=int(v);}
    if(const char* r=std::getenv("ANYPS3_FRAMETIME_PEEK_PC")){char* end=nullptr;const auto v=std::strtoul(r,&end,0);
        if(end==r || *end || v>0xffffffffu){std::fprintf(stderr,"invalid ANYPS3_FRAMETIME_PEEK_PC\n");std::_Exit(2);}peek_pc=std::uint32_t(v);}
    const int fd=::open(path,O_RDWR|O_CREAT|O_TRUNC,0644);
    if(fd<0){std::fprintf(stderr,"ANYPS3_FRAMETIME: cannot open %s\n",path);std::_Exit(2);}
    const std::uint64_t bytes=sizeof(Header)+capacity*sizeof(Event);       // mmap offset 0 is page aligned: events are 64-byte aligned
    if(::ftruncate(fd,off_t(bytes))!=0){std::fprintf(stderr,"ANYPS3_FRAMETIME: ftruncate failed\n");std::_Exit(2);}
    void* map=::mmap(nullptr,bytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);::close(fd);
    if(map==MAP_FAILED){std::fprintf(stderr,"ANYPS3_FRAMETIME: mmap failed\n");std::_Exit(2);}
    header=static_cast<Header*>(map);std::memcpy(header->magic,"ANYPSFT1",8);header->capacity=capacity;header->version=2;
    header->head.store(0,std::memory_order_release);header->dropped.store(0,std::memory_order_release);
    ring=reinterpret_cast<Event*>(static_cast<char*>(map)+sizeof(Header));ring_mask=capacity-1;
    return true;
}
inline const bool enabled_at_start=(on=open_ring());
// Slot ownership (see the wire format): reserve an index, claim its slot exclusively, fill, commit. Events of different
// threads interleave but never overlap; a writer that cannot own the slot drops its event (counted) instead of waiting.
inline std::uint64_t reserve(){return header->head.fetch_add(1,std::memory_order_relaxed);}
// Returns the claimed slot (seq odd = 2i+1) or nullptr when the event was dropped.
inline Event* claim(std::uint64_t i){
    Event& e=ring[i&ring_mask];
    std::uint64_t s=e.seq.load(std::memory_order_relaxed);
    if((s&1) || s>=2*i+2 || !e.seq.compare_exchange_strong(s,2*i+1,std::memory_order_acquire,std::memory_order_relaxed)){
        header->dropped.fetch_add(1,std::memory_order_relaxed);return nullptr;}
    return &e;
}
inline void commit(Event* e,std::uint64_t i){e->seq.store(2*i+2,std::memory_order_release);}
inline void put(std::uint64_t ns,std::uint32_t kind,std::uint64_t a,std::uint64_t b,std::uint64_t c,std::uint64_t d){
    const auto i=reserve();
    Event* e=claim(i);if(!e)return;
    e->ns=ns;e->kind=kind;e->tid=this_tid();e->a=a;e->b=b;e->c=c;e->d=d;
    commit(e,i);
}
[[gnu::always_inline]] inline void mark(std::uint32_t kind,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,std::uint64_t d=0){
    if(__builtin_expect(on,0))put(now(),kind,a,b,c,d);
}
struct Span {
    std::uint32_t kind;std::uint64_t a,c,d,t0,min_ns;
    explicit Span(std::uint32_t k,std::uint64_t a_=0,std::uint64_t c_=0,std::uint64_t min=0,std::uint64_t d_=0):
        kind(k),a(a_),c(c_),d(d_),t0(on?now():0),min_ns(min){}
    ~Span(){if(!t0)return;const auto t1=now();if(t1-t0>=min_ns)put(t0,kind,a,t1-t0,c,d);}
    Span(const Span&)=delete;Span& operator=(const Span&)=delete;
};
// Wrap a closure that another thread executes: records the queue wait (post -> start) and the execution time.
inline std::function<void()> wrap(std::uint32_t kind,std::uint64_t a,std::function<void()> f){
    if(!on)return f;
    const auto posted=now();
    return [kind,a,posted,f=std::move(f)]{
        const auto t0=now();
        struct Done {std::uint32_t kind;std::uint64_t a,posted,t0;~Done(){put(t0,kind,a,now()-t0,t0-posted,0);}} done{kind,a,posted,t0};
        f();
    };
}
// Guest thread blocks (usleep, semaphore, event queue ...). `State` is PpuState: kept a template so tests can compile this alone.
template<class State> inline void block(State& s,unsigned kind,std::uint64_t id){
    if(__builtin_expect(!on,1))return;
    std::uint64_t peek=0;
    if(peek_register>=0 && (!peek_pc || std::uint32_t(s.pc)==peek_pc || std::uint32_t(s.pc)==peek_pc+4)){const auto address=std::uint32_t(s.r[peek_register]);
        try{peek=(std::uint64_t(address)<<32)|std::uint32_t(s.memory.load(address,4));}catch(...){peek=(std::uint64_t(address)<<32)|0xdeadbeefu;}}
    put(now(),Block,(std::uint64_t(std::uint32_t(s.lr))<<32)|std::uint32_t(s.pc),kind==12?std::uint64_t(s.r[3]):id,
        std::uint64_t(s.thread_id)|(std::uint64_t(kind)<<32),peek);
}
}
#define ANYPS3_FT_BLOCK(s,kind,id) anyps3_ft::block(s,kind,id)
#define ANYPS3_FT_SPAN(var,kind,...) anyps3_ft::Span var(anyps3_ft::kind,__VA_ARGS__)
#define ANYPS3_FT_MARK(kind,...) anyps3_ft::mark(anyps3_ft::kind,__VA_ARGS__)
#endif
'''
