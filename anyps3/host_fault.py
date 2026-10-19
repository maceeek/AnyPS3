"""Foreign HostGuard rendezvous; the coordinator services control RPC without memory admission."""
CPP = r'''
#include <array>
#include <atomic>
#include <thread>
#include <functional>
#include <stdexcept>
#include <climits>
#include <cstdint>
#include <fstream>
#include <string>
#include <cstdio>
#if defined(__linux__)
#include <sys/syscall.h>
#include <unistd.h>
#include <fcntl.h>
#include <cerrno>
#include <linux/futex.h>
#include <ucontext.h>
struct NativeHostFaultService {
    struct Slot {std::atomic<int> state{0},generation{0};std::uintptr_t address=0;int result=0,access=0;HostAccessOrigin origin;};
    std::array<Slot,64> slots;
    int pipefd[2]{-1,-1};std::thread bridge;
    long owner_tid=0,coordinator_tid=syscall(SYS_gettid);
    std::atomic<int> main_slot{-1};
    std::function<void()> coordinator_pump,main_wait_loop;
    std::function<void(const std::function<void()>&)> coordinator_wait;
    std::function<bool(std::uintptr_t)> synchronize;
    inline static std::atomic<NativeHostFaultService*> active{nullptr};
    inline static std::atomic<unsigned> handlers{0};
    static_assert(std::atomic<int>::is_always_lock_free && std::atomic<unsigned>::is_always_lock_free && std::atomic<NativeHostFaultService*>::is_always_lock_free,"fault mailbox needs lock-free atomics");
    static bool resolved(std::uintptr_t address,int access){
        // A queued fault can arrive after an earlier MapBarrier has already
        // removed its protection. Retry only a currently permitted access to
        // our primary memfd; unrelated/non-executable/RO faults keep SIGSEGV.
        if(!access)return false;
        std::ifstream maps("/proc/self/maps");std::string line;
        while(std::getline(maps,line)){unsigned long long first=0,last=0;char permissions[5]{};
            if(std::sscanf(line.c_str(),"%llx-%llx %4s",&first,&last,permissions)!=3 || address<first || address>=last)continue;
            if(line.find("memfd:anyps3-guest")==std::string::npos)return false;
            return access==1?permissions[0]=='r':access==2?permissions[1]=='w':permissions[2]=='x';
        }return false;
    }
    static void wake_main(){
        auto* service=active.load(std::memory_order_acquire);if(!service)return;
        const auto index=service->main_slot.load(std::memory_order_acquire);if(index<0)return;
        auto& slot=service->slots[unsigned(index)];slot.generation.fetch_add(1,std::memory_order_release);
        syscall(SYS_futex,reinterpret_cast<int*>(&slot.generation),FUTEX_WAKE_PRIVATE,1,nullptr,nullptr,0);
    }
    void wait_main(){
        auto& slot=slots[unsigned(main_slot.load(std::memory_order_acquire))];
        while(slot.state.load(std::memory_order_acquire)!=3){
            const auto seen=slot.generation.load(std::memory_order_acquire);
            if(coordinator_pump)coordinator_pump();
            if(slot.state.load(std::memory_order_acquire)!=3)syscall(SYS_futex,reinterpret_cast<int*>(&slot.generation),FUTEX_WAIT_PRIVATE,seen,nullptr,nullptr,0);
        }
    }
    explicit NativeHostFaultService(std::function<bool(std::uintptr_t)> invoke,std::function<void()> pump={},std::function<void(const std::function<void()>&)> wait={}):
        coordinator_pump(std::move(pump)),coordinator_wait(std::move(wait)),synchronize(std::move(invoke)){
        main_wait_loop=[this]{wait_main();};
        if(pipe2(pipefd,O_CLOEXEC))throw std::runtime_error("cannot create RSX host fault mailbox");
        try{bridge=std::thread([this]{for(;;){unsigned index=0;ssize_t n;
            do{n=::read(pipefd[0],&index,sizeof(index));}while(n<0 && errno==EINTR);
            if(n!=sizeof(index) || index==UINT_MAX)break;
            auto& slot=slots.at(index);
            if(slot.state.load(std::memory_order_acquire)!=2)std::terminate();
            try{HostAccessContext origin(slot.origin);slot.result=(synchronize(slot.address) || resolved(slot.address,slot.access))?1:0;}catch(...){slot.result=-1;}
            slot.state.store(3,std::memory_order_release);slot.generation.fetch_add(1,std::memory_order_release);syscall(SYS_futex,reinterpret_cast<int*>(&slot.generation),FUTEX_WAKE_PRIVATE,1,nullptr,nullptr,0);syscall(SYS_futex,reinterpret_cast<int*>(&slot.state),FUTEX_WAKE_PRIVATE,1,nullptr,nullptr,0);
        }});}catch(...){close(pipefd[0]);close(pipefd[1]);throw;}
    }
    void install(long owner){owner_tid=owner;NativeHostFaultService* none=nullptr;
        if(!active.compare_exchange_strong(none,this))throw std::runtime_error("RSX fault service already installed");}
    // -2 means owner/no route: preserve the original synchronous owner path.
    static int route(std::uintptr_t address,void* context)noexcept{
        handlers.fetch_add(1,std::memory_order_acq_rel);auto* service=active.load(std::memory_order_acquire);
        if(!service || syscall(SYS_gettid)==service->owner_tid){handlers.fetch_sub(1,std::memory_order_release);return -2;}
        unsigned index=0;for(;index<service->slots.size();++index){int unused=0;if(service->slots[index].state.compare_exchange_strong(unused,1))break;}
        if(index==service->slots.size()){handlers.fetch_sub(1,std::memory_order_release);return -1;}
        auto& slot=service->slots[index];slot.address=address;slot.access=0;slot.origin=HostAccessContext::sample();
#if defined(__x86_64__) && defined(REG_ERR)
        const auto error=static_cast<ucontext_t*>(context)->uc_mcontext.gregs[REG_ERR];slot.access=(error&16)?3:(error&2)?2:1;
#else
        (void)context;
#endif
        slot.state.store(2,std::memory_order_release);
        const bool main_thread=syscall(SYS_gettid)==service->coordinator_tid;
        if(main_thread)service->main_slot.store(int(index),std::memory_order_release);
        ssize_t n;do{n=::write(service->pipefd[1],&index,sizeof(index));}while(n<0 && errno==EINTR);
        if(n!=sizeof(index)){slot.state.store(0);handlers.fetch_sub(1,std::memory_order_release);return -1;}
        if(main_thread){try{if(service->coordinator_wait)service->coordinator_wait(service->main_wait_loop);else service->main_wait_loop();}
            catch(...){static const char message[]="RSX coordinator fault service failed\n";(void)!::write(2,message,sizeof(message)-1);_exit(125);}}
        else while(slot.state.load(std::memory_order_acquire)!=3)syscall(SYS_futex,reinterpret_cast<int*>(&slot.state),FUTEX_WAIT_PRIVATE,2,nullptr,nullptr,0);
        if(main_thread)service->main_slot.store(-1,std::memory_order_release);
        const auto result=slot.result;slot.state.store(0,std::memory_order_release);handlers.fetch_sub(1,std::memory_order_release);return result;
    }
    ~NativeHostFaultService(){
        auto* self=this;active.compare_exchange_strong(self,nullptr);
        while(handlers.load(std::memory_order_acquire))std::this_thread::yield();
        unsigned end=UINT_MAX;ssize_t n;do{n=::write(pipefd[1],&end,sizeof(end));}while(n<0 && errno==EINTR);
        if(bridge.joinable()){bridge.join();}
        close(pipefd[0]);close(pipefd[1]);
    }
};
#endif
'''
