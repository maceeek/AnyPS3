"""Opt-in Linux/x86-64 CPU sampling; no instruction-path instrumentation.

The handler has one producer per buffer and uses only always-lock-free atomics,
bounded scalar loads and ucontext registers. All I/O belongs to the collector.
Wire format and the limitations of asynchronous guest PC snapshots are described
in PROF1's INTEGRATION.md and in sample_report.py.
"""

TYPES = r'''
#include <signal.h>
#include <ucontext.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <pthread.h>
#include <time.h>
#include <cerrno>
#include <cstdint>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <atomic>
#include <memory>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <vector>
#include <string>
#include <stdexcept>
#include <chrono>
#include <limits>

namespace anyps3_sample {
enum class Role : std::uint32_t { Ppu=1, Rsx=2, Spu=3 };
struct Record {
    std::uint64_t sequence,overrun,guest_steps;
    std::uint32_t guest_pc,guest_tid,depth,flags;
    std::uint64_t addresses[8];
};
static_assert(sizeof(Record)==104 && offsetof(Record,addresses)==40,"sample wire ABI");
static_assert(std::atomic<std::uint64_t>::is_always_lock_free,"signal-safe counters");
static_assert(std::atomic<bool>::is_always_lock_free,"signal-safe active flag");
struct Slot {
    std::unique_ptr<Record[]> storage;
    Record* ring=nullptr;
    std::uint64_t capacity=0;
    alignas(64) std::atomic<std::uint64_t> head{0};
    alignas(64) std::atomic<std::uint64_t> tail{0};
    std::atomic<std::uint64_t> attempted{0},dropped{0},overruns{0};
    std::atomic<bool> active{false};
    const std::uint32_t* pc=nullptr;
    const std::uint32_t* guest_tid=nullptr;
    const std::uint64_t* steps=nullptr;
    std::uintptr_t stack_low=0,stack_high=0;
    bool frames=false;
    // PPU role, ANYPS3_SAMPLE_PROFILE_GUEST_STACK=1: addresses[1..] carry guest LR and the PPC64 back-chain return
    // addresses (read through process_vm_readv, which fails with EFAULT instead of faulting in the handler).
    const std::uint64_t* guest_lr=nullptr;const std::uint64_t* guest_r1=nullptr;const std::uint8_t* const* guest_base=nullptr;
    bool guest_stack=false;
    timer_t timer{};bool timer_created=false;
    int fd=-1;
    std::uint64_t written_attempted=UINT64_MAX;
    ~Slot(){if(fd>=0)::close(fd);}
};

// Linux/GCC diagnostic snapshots: aligned scalar __atomic loads cannot tear on
// this ABI and add no stores/instrumentation to guest execution. A multi-field
// snapshot is NOT a transaction: a context switch can be interrupted halfway.
// PPUGEN1 also publishes PC/steps only at its existing checkpoints. Host RIP is
// the precise interrupted instruction; guest PC is the last published context.
inline void handler(int, siginfo_t* info, void* context) noexcept {
#if defined(__linux__) && defined(__x86_64__)
    if(!info || info->si_code!=SI_TIMER)return;
    auto* slot=static_cast<Slot*>(info->si_value.sival_ptr);
    if(!slot || !slot->active.load(std::memory_order_acquire))return;
    const auto sequence=slot->attempted.fetch_add(1,std::memory_order_relaxed)+1;
    const auto overrun=info->si_overrun>0?std::uint64_t(info->si_overrun):0;
    slot->overruns.fetch_add(overrun,std::memory_order_relaxed);
    const auto head=slot->head.load(std::memory_order_relaxed);
    if(head-slot->tail.load(std::memory_order_acquire)>=slot->capacity){
        slot->dropped.fetch_add(1,std::memory_order_relaxed);return;
    }
    auto& r=slot->ring[head%slot->capacity];
    r.sequence=sequence;r.overrun=overrun;
    r.guest_steps=slot->steps?__atomic_load_n(slot->steps,__ATOMIC_RELAXED):0;
    r.guest_pc=slot->pc?__atomic_load_n(slot->pc,__ATOMIC_RELAXED):0;
    r.guest_tid=slot->guest_tid?__atomic_load_n(slot->guest_tid,__ATOMIC_RELAXED):0;
    r.flags=(slot->pc?1u:0u)|((slot->frames||slot->guest_stack)?2u:0u);
    const auto& g=static_cast<ucontext_t*>(context)->uc_mcontext.gregs;
    r.addresses[0]=std::uint64_t(g[REG_RIP]);r.depth=1;
    for(unsigned i=1;i<8;++i)r.addresses[i]=0;
    const auto sp=std::uintptr_t(g[REG_RSP]);auto fp=std::uintptr_t(g[REG_RBP]);
    // Opt-in only for builds with frame pointers. Never use backtrace/unwind,
    // never inspect a foreign thread, guard page, alternate or unknown stack.
    if(slot->guest_stack && slot->guest_lr && slot->guest_r1 && slot->guest_base){
        // Diagnostic: guest call chain. state.lr is current (write-through registers); then the back chain
        // 0(r1) -> parent frame, LR save slot 16(parent). Every read is bounds-checked and EFAULT-safe.
        const auto base=reinterpret_cast<std::uintptr_t>(__atomic_load_n(slot->guest_base,__ATOMIC_RELAXED));
        auto be64=[&](std::uint64_t ea,std::uint64_t& out)->bool{
            if(!base || (ea&7) || ea<0x10000 || ea>=0xfffffff0u)return false;
            std::uint64_t raw=0;struct iovec local{&raw,8},remote{reinterpret_cast<void*>(base+ea),8};
            if(::syscall(SYS_process_vm_readv,::getpid(),&local,1,&remote,1,0)!=8)return false;
            out=__builtin_bswap64(raw);return true;};
        r.addresses[r.depth++]=__atomic_load_n(slot->guest_lr,__ATOMIC_RELAXED);
        std::uint64_t frame=__atomic_load_n(slot->guest_r1,__ATOMIC_RELAXED);
        while(r.depth<8){
            std::uint64_t parent=0,ret=0;
            if(!be64(frame,parent) || parent<=frame || parent-frame>0x100000 || !be64(parent+16,ret) || !ret)break;
            r.addresses[r.depth++]=ret;frame=parent;
        }
    }else if(slot->frames && sp>=slot->stack_low && sp<slot->stack_high){
        auto lower=sp;
        while(r.depth<8 && !(fp&7) && fp>=lower && fp>=slot->stack_low &&
              fp<=slot->stack_high-2*sizeof(std::uintptr_t)){
            const auto* frame=reinterpret_cast<const std::uintptr_t*>(fp);
            const auto next=__atomic_load_n(frame,__ATOMIC_RELAXED);
            const auto ret=__atomic_load_n(frame+1,__ATOMIC_RELAXED);
            if(!ret)break;
            r.addresses[r.depth++]=ret;lower=fp+2*sizeof(std::uintptr_t);fp=next;
        }
    }
    slot->head.store(head+1,std::memory_order_release);
#else
    (void)info;(void)context;
#endif
}
inline std::runtime_error error(const std::string& what){
    return std::runtime_error("SAMPLE_PROFILE: "+what+": "+std::strerror(errno));
}
inline void write_all(int fd,const void* bytes,std::size_t size){
    auto* p=static_cast<const char*>(bytes);
    while(size){const auto n=::write(fd,p,size);if(n<0 && errno==EINTR)continue;
        if(n<=0)throw error("write");
        p+=n;size-=std::size_t(n);}
}
inline void put32(std::vector<unsigned char>& b,std::uint32_t v){for(unsigned i=0;i<4;++i)b.push_back((v>>(8*i))&255);}
inline void put64(std::vector<unsigned char>& b,std::uint64_t v){for(unsigned i=0;i<8;++i)b.push_back((v>>(8*i))&255);}
inline std::uint64_t checksum(const std::vector<unsigned char>& b){
    std::uint64_t h=UINT64_C(14695981039346656037);for(auto c:b){h^=c;h*=UINT64_C(1099511628211);}return h;
}
inline unsigned setting(const char* name,unsigned fallback,unsigned low,unsigned high){
    const char* text=std::getenv(name);if(!text)return fallback;
    char* end=nullptr;errno=0;const auto n=std::strtoul(text,&end,10);
    if(errno || !*text || *end || *text=='-' || n<low || n>high)
        throw std::runtime_error(std::string("SAMPLE_PROFILE: invalid ")+name);
    return unsigned(n);
}
struct Config {
    bool enabled=false,frames=false,stopping=false;
    unsigned roles=14,period_us=200,flush_ms=100,capacity=8192,wall_roles=0;bool guest_stack=false;
    int signo=0;std::string directory;
    std::mutex mutex;std::condition_variable changed;
    std::vector<std::unique_ptr<Slot>> slots;
    std::thread writer;std::string failure;
    explicit Config(){
        const char* dir=std::getenv("ANYPS3_SAMPLE_PROFILE");if(!dir || !*dir)return;
#if !defined(__linux__) || !defined(__x86_64__)
        throw std::runtime_error("SAMPLE_PROFILE: Linux x86-64 required");
#endif
        directory=dir;period_us=setting("ANYPS3_SAMPLE_PROFILE_US",200,50,1000000);
        flush_ms=setting("ANYPS3_SAMPLE_PROFILE_FLUSH_MS",100,10,5000);
        capacity=setting("ANYPS3_SAMPLE_PROFILE_CAPACITY",8192,16,1048576);
        frames=setting("ANYPS3_SAMPLE_PROFILE_FRAMES",0,0,1)!=0;
        if(const char* value=std::getenv("ANYPS3_SAMPLE_PROFILE_ROLES")){
            roles=0;std::string text=value;std::size_t start=0;
            for(;;){auto end=text.find(',',start);auto role=text.substr(start,end-start);
                if(role=="ppu")roles|=2;else if(role=="rsx")roles|=4;else if(role=="spu")roles|=8;
                else throw std::runtime_error("SAMPLE_PROFILE: invalid role list");
                if(end==std::string::npos)break;
                start=end+1;}
        }
        // Diagnostics: ANYPS3_SAMPLE_PROFILE_WALL=ppu,spu samples those roles on wall time (CLOCK_MONOTONIC, so a sleeping or
        // parked thread is sampled where it sleeps; the signal interrupts and restarts the sleep) instead of thread CPU time.
        if(const char* value=std::getenv("ANYPS3_SAMPLE_PROFILE_WALL")){
            std::string text=value;std::size_t start=0;
            for(;;){auto end=text.find(',',start);auto role=text.substr(start,end-start);
                if(role=="ppu")wall_roles|=2;else if(role=="rsx")wall_roles|=4;else if(role=="spu")wall_roles|=8;
                else throw std::runtime_error("SAMPLE_PROFILE: invalid wall role list");
                if(end==std::string::npos)break;
                start=end+1;}
        }
        guest_stack=setting("ANYPS3_SAMPLE_PROFILE_GUEST_STACK",0,0,1)!=0;
        signo=SIGRTMIN+6;if(signo>SIGRTMAX)throw std::runtime_error("SAMPLE_PROFILE: no realtime signal");
        struct sigaction previous{};if(::sigaction(signo,nullptr,&previous))throw error("sigaction query");
        if(previous.sa_handler!=SIG_DFL)throw std::runtime_error("SAMPLE_PROFILE: SIGRTMIN+6 already in use");
        // New session only; do not mix processes or overwrite old evidence.
        if(::mkdir(directory.c_str(),0700) && errno!=EEXIST)throw error("mkdir");
        const auto session=directory+"/session.txt";
        const int fd=::open(session.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
        if(fd<0)throw error("new session.txt (use a fresh directory)");
        try{
            struct stat st{};if(::stat("/proc/self/exe",&st))throw error("stat executable");
            char path[4096];const auto n=::readlink("/proc/self/exe",path,sizeof(path)-1);if(n<0)throw error("readlink executable");path[n]=0;
            const auto text=std::string("APS3_SAMPLE_SESSION_1\n")+std::to_string(st.st_dev)+" "+std::to_string(st.st_ino)+" "+
                std::to_string(st.st_size)+" "+std::to_string(st.st_mtim.tv_sec)+" "+std::to_string(st.st_mtim.tv_nsec)+"\n"+path+"\n";
            write_all(fd,text.data(),text.size());::close(fd);
        }catch(...){::close(fd);throw;}
        save_maps();
        struct sigaction action{};action.sa_sigaction=handler;action.sa_flags=SA_SIGINFO|SA_RESTART;
        ::sigemptyset(&action.sa_mask);if(::sigaction(signo,&action,nullptr))throw error("sigaction install");
        enabled=true;writer=std::thread([this]{collect();});
    }
    bool selected(Role role)const{return enabled && (roles&(1u<<unsigned(role)));}
    void save_maps(){
        const int in=::open("/proc/self/maps",O_RDONLY|O_CLOEXEC);if(in<0)throw error("open maps");
        const auto temporary=directory+"/maps.txt.tmp";const auto destination=directory+"/maps.txt";
        const int out=::open(temporary.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0600);
        if(out<0){::close(in);throw error("open maps output");}
        try{char bytes[8192];for(;;){const auto n=::read(in,bytes,sizeof(bytes));if(n<0 && errno==EINTR)continue;
                if(n<0)throw error("read maps");
                if(!n)break;
                write_all(out,bytes,std::size_t(n));}
            ::close(in);::close(out);
        }catch(...){::close(in);::close(out);throw;}
        if(::rename(temporary.c_str(),destination.c_str()))throw error("rename maps");
    }
    void drain(Slot& slot){
        const auto tail=slot.tail.load(std::memory_order_relaxed),head=slot.head.load(std::memory_order_acquire);
        const auto attempts=slot.attempted.load(std::memory_order_relaxed);
        if(head==tail && attempts==slot.written_attempted)return;
        const char magic[]="APCHUNK1";std::vector<unsigned char> bytes(magic,magic+8);
        put32(bytes,std::uint32_t((head-tail)*sizeof(Record)));put32(bytes,std::uint32_t(head-tail));
        put64(bytes,attempts);put64(bytes,slot.dropped.load(std::memory_order_relaxed));put64(bytes,slot.overruns.load(std::memory_order_relaxed));
        for(auto i=tail;i<head;++i){const auto& r=slot.ring[i%slot.capacity];
            const auto* p=reinterpret_cast<const unsigned char*>(&r);bytes.insert(bytes.end(),p,p+sizeof(r));}
        const auto hash=checksum(bytes);put64(bytes,hash);
        const char commit[]="APCOMMIT";bytes.insert(bytes.end(),commit,commit+8);
        write_all(slot.fd,bytes.data(),bytes.size());
        slot.tail.store(head,std::memory_order_release);slot.written_attempted=attempts;
    }
    void collect() noexcept {
        try{std::unique_lock<std::mutex> lock(mutex);for(;;){
                for(auto& slot:slots)drain(*slot);
                save_maps();if(stopping)return;
                changed.wait_for(lock,std::chrono::milliseconds(flush_ms),[this]{return stopping;});
            }
        }catch(const std::exception& e){
            {std::lock_guard<std::mutex> lock(mutex);failure=e.what();}
            std::fprintf(stderr,"%s\n",e.what());
            const auto path=directory+"/error.txt";const int fd=::open(path.c_str(),O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0600);
            if(fd>=0){const auto ignored=::write(fd,e.what(),std::strlen(e.what()));(void)ignored;::close(fd);}
        }
    }
    ~Config(){if(writer.joinable()){{std::lock_guard<std::mutex> lock(mutex);stopping=true;}changed.notify_one();writer.join();}}
};
inline Config& config(){static Config value;return value;}

class Thread {
    Config* owner=nullptr;Slot* slot=nullptr;sigset_t old_mask{};bool mask_saved=false;std::string start_path;
public:
    explicit Thread(Role role,const std::uint32_t* pc=nullptr,const std::uint32_t* tid=nullptr,const std::uint64_t* steps=nullptr,
                    const std::uint64_t* guest_lr=nullptr,const std::uint64_t* guest_r1=nullptr,const std::uint8_t* const* guest_base=nullptr){
        auto& cfg=config();if(!cfg.selected(role))return;
        owner=&cfg;sigset_t set;::sigemptyset(&set);::sigaddset(&set,cfg.signo);
        const int mask_error=::pthread_sigmask(SIG_BLOCK,&set,&old_mask);
        if(mask_error){errno=mask_error;throw error("block timer signal");}mask_saved=true;
        try{
            auto fresh=std::make_unique<Slot>();fresh->capacity=cfg.capacity;
            fresh->storage=std::make_unique<Record[]>(fresh->capacity); // zero before arming
            fresh->ring=fresh->storage.get(); // raw scalar access only in handler
            fresh->pc=pc;fresh->guest_tid=tid;fresh->steps=steps;fresh->frames=cfg.frames;
            if(cfg.guest_stack && role==Role::Ppu && guest_lr && guest_r1 && guest_base){
                fresh->guest_lr=guest_lr;fresh->guest_r1=guest_r1;fresh->guest_base=guest_base;fresh->guest_stack=true;fresh->frames=false;}
            if(fresh->frames){pthread_attr_t attr;const int a=::pthread_getattr_np(::pthread_self(),&attr);
                if(a){errno=a;throw error("get stack attributes");}
                void* base=nullptr;std::size_t size=0;const int b=::pthread_attr_getstack(&attr,&base,&size);::pthread_attr_destroy(&attr);
                if(b || size<16){errno=b;throw error("get stack bounds");}
                fresh->stack_low=reinterpret_cast<std::uintptr_t>(base);fresh->stack_high=fresh->stack_low+size;
            }
            const auto host_tid=std::uint64_t(::syscall(SYS_gettid));
            {
                std::lock_guard<std::mutex> lock(cfg.mutex);
                if(!cfg.failure.empty())throw std::runtime_error(cfg.failure);
                const auto path=cfg.directory+"/thread-"+std::to_string(host_tid)+"-"+std::to_string(cfg.slots.size())+".samples";
                fresh->fd=::open(path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(fresh->fd<0)throw error("open samples");
                start_path=path+".start";
                const char magic[]="APS3SP01";std::vector<unsigned char> header(magic,magic+8);
                put32(header,1);put32(header,sizeof(Record));put64(header,std::uint64_t(cfg.period_us)*1000);put64(header,host_tid);
                put32(header,unsigned(role));put32(header,8);put64(header,std::uint64_t(::getpid()));put64(header,0);
                write_all(fresh->fd,header.data(),header.size());slot=fresh.get();cfg.slots.push_back(std::move(fresh));
            }
            struct sigevent event{};event.sigev_notify=SIGEV_THREAD_ID;event.sigev_signo=cfg.signo;
            event.sigev_value.sival_ptr=slot;event._sigev_un._tid=pid_t(host_tid);
            const clockid_t clock=(cfg.wall_roles&(1u<<unsigned(role)))?CLOCK_MONOTONIC:CLOCK_THREAD_CPUTIME_ID;
            if(::timer_create(clock,&event,&slot->timer))throw error("timer_create");
            slot->timer_created=true;
            struct itimerspec spec{};spec.it_interval.tv_sec=cfg.period_us/1000000;
            spec.it_interval.tv_nsec=long(cfg.period_us%1000000)*1000;spec.it_value=spec.it_interval;
            slot->active.store(true,std::memory_order_release);
            struct timespec armed{};::clock_gettime(CLOCK_MONOTONIC,&armed);
            if(::timer_settime(slot->timer,0,&spec,nullptr))throw error("timer_settime");
            // Side file (the .samples wire ABI stays untouched): CLOCK_MONOTONIC time at which the timer was armed. Record k
            // (its `sequence`, counted from 1) was taken about k periods later, so the timelines of several threads can be
            // laid on one time axis (wall mode: ANYPS3_SAMPLE_PROFILE_WALL).
            {const int fd=::open(start_path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
                if(fd<0)throw error("open start file");
                const auto text=std::to_string(std::int64_t(armed.tv_sec)*1000000000+armed.tv_nsec)+" "+std::to_string(std::uint64_t(cfg.period_us)*1000)+" "+
                    (clock==CLOCK_MONOTONIC?"wall":"cpu")+"\n";
                write_all(fd,text.data(),text.size());::close(fd);}
            // Restore every other signal's state; this timer must be unblocked.
            auto run_mask=old_mask;::sigdelset(&run_mask,cfg.signo);
            const int c=::pthread_sigmask(SIG_SETMASK,&run_mask,nullptr);if(c){errno=c;throw error("unblock timer signal");}
        }catch(...){stop();throw;}
    }
    void stop() noexcept {
        if(!mask_saved)return;
        sigset_t set;::sigemptyset(&set);::sigaddset(&set,owner->signo);::pthread_sigmask(SIG_BLOCK,&set,nullptr);
        if(slot){slot->active.store(false,std::memory_order_release);
            if(slot->timer_created){::timer_delete(slot->timer);slot->timer_created=false;}
            // Pending timer notifications may survive timer_delete. Slot storage
            // remains owned by Config even after this thread exits.
            struct timespec zero{};siginfo_t info{};while(::sigtimedwait(&set,&info,&zero)>=0){}
        }
        ::pthread_sigmask(SIG_SETMASK,&old_mask,nullptr);mask_saved=false;
    }
    ~Thread(){stop();}
    Thread(const Thread&)=delete;Thread& operator=(const Thread&)=delete;
};
} // namespace anyps3_sample
'''
