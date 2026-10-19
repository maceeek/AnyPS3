"""Bounded two-way RSX owner transport used by the game and native fixtures.

The transport owns its thread and bounded FIFO; native replay harnesses can
supply an isolated renderer state. It never lends a live PPU register facade.
"""
TYPES = r'''
struct RsxOwnerQueue {
    enum class Kind {RunFifo,HostAccess,MapBarrier,Drain,Stop};
    struct Request {
        Kind kind=Kind::Drain;std::uint64_t sequence=0,put_generation=0;unsigned budget=0;
        HostAccessOrigin origin;std::function<void()> invoke;
        bool asynchronous=false,done=false,byte_access=HostByteAccess::active();std::exception_ptr error;
    };
    static constexpr unsigned capacity=64;
    std::mutex mutex;std::condition_variable changed;
    std::deque<std::shared_ptr<Request>> queue,coordinator;
    std::atomic<unsigned> coordinator_pending{0};
    std::atomic<bool> failed{false};
    std::thread thread;std::thread::id owner;
    const std::thread::id coordinator_thread=std::this_thread::get_id();
    std::uint64_t next_sequence=0,completed_sequence=0,failed_sequence=0,generation=0;
    bool closing=false,stopped=false,tearing_down=false,stop_requested=false;
    std::exception_ptr error;HostAccessOrigin failed_origin;
    std::function<std::shared_ptr<void>()> thread_scope;
    std::function<void(unsigned)> run_fifo;
    std::function<void()> service,after_request,idle_teardown,coordinator_service,coordinator_wake;
    std::function<void(std::exception_ptr,std::uint64_t,HostAccessOrigin)> failure;
    std::function<std::chrono::steady_clock::time_point()> deadline;
    void check(){if(!failed.load(std::memory_order_acquire))return;check_error();}
    [[gnu::noinline]] void check_error(){std::lock_guard<std::mutex> lock(mutex);if(error)std::rethrow_exception(error);}
    inline static thread_local RsxOwnerQueue* current=nullptr;
    void check_locked()const{if(error)std::rethrow_exception(error);if(closing || stopped)throw std::runtime_error("RSX owner queue stopped");}
    void poke(){std::lock_guard<std::mutex> lock(mutex);++generation;changed.notify_all();}
    void poison(std::exception_ptr failure,std::uint64_t sequence,HostAccessOrigin origin){
        bool first=false;
        {std::lock_guard<std::mutex> lock(mutex);
            if(!error){error=failure;failed_sequence=sequence;failed_origin=origin;first=true;failed.store(true,std::memory_order_release);}
            for(auto& list:{&queue,&coordinator}){for(auto& request:*list){request->error=error;request->done=true;}list->clear();}
            coordinator_pending.store(0,std::memory_order_release);closing=true;++generation;changed.notify_all();
        }
        if(first && this->failure)this->failure(failure,sequence,origin);
    }
    void pump_coordinator(){
        if(std::this_thread::get_id()!=coordinator_thread)return;
        if(coordinator_service)coordinator_service();
        if(!coordinator_pending.load(std::memory_order_acquire))return;
        for(;;){std::shared_ptr<Request> request;
            {std::lock_guard<std::mutex> lock(mutex);if(coordinator.empty())return;request=coordinator.front();coordinator.pop_front();coordinator_pending.fetch_sub(1,std::memory_order_release);}
            try{HostAccessContext origin(request->origin);HostByteAccess bytes(request->byte_access,true);request->invoke();}catch(...){request->error=std::current_exception();}
            {std::lock_guard<std::mutex> lock(mutex);request->done=true;++generation;}changed.notify_all();
            if(request->asynchronous && request->error){poison(request->error,request->sequence,request->origin);return;}
        }
    }
    template<class Predicate> void wait(std::unique_lock<std::mutex>& lock,Predicate ready){
        while(!ready()){
            const auto seen=generation;lock.unlock();pump_coordinator();lock.lock();
            if(!ready())changed.wait(lock,[&]{return ready() || generation!=seen;});
        }
    }
    std::shared_ptr<Request> post(Kind kind,std::function<void()> invoke={},unsigned budget=0,std::uint64_t put_generation=0){
        if(current==this)throw std::runtime_error("recursive RSX owner submission");
        switch(kind){case Kind::RunFifo:case Kind::HostAccess:case Kind::MapBarrier:case Kind::Drain:case Kind::Stop:break;
            default:throw std::runtime_error("unknown RSX owner request kind");}
        if(kind==Kind::HostAccess && !invoke)throw std::runtime_error("RSX host access lacks operation");
        auto request=std::make_shared<Request>();request->kind=kind;request->invoke=std::move(invoke);request->budget=budget;request->put_generation=put_generation;request->origin=HostAccessContext::sample();
        std::unique_lock<std::mutex> lock(mutex);check_locked();if(stop_requested)throw std::runtime_error("RSX owner queue stopped");
        wait(lock,[&]{return error || closing || queue.size()<capacity;});check_locked();if(stop_requested)throw std::runtime_error("RSX owner queue stopped");
        if(next_sequence==UINT64_MAX)throw std::runtime_error("RSX queue sequence overflow");
        request->sequence=++next_sequence;
        // Only adjacent asynchronous ticks may coalesce. Barriers and memory
        // requests are ordered, never discarded or hidden behind a newer PUT.
        if(kind==Kind::RunFifo && !queue.empty() && queue.back()->kind==kind){auto previous=queue.back();
            previous->invoke=std::move(request->invoke);previous->sequence=request->sequence;previous->origin=request->origin;previous->put_generation=std::max(previous->put_generation,put_generation);previous->budget=static_cast<unsigned>(std::min<std::uint64_t>(65536,std::uint64_t(previous->budget)+budget));return previous;}
        queue.push_back(request);if(kind==Kind::Stop)stop_requested=true;++generation;changed.notify_all();return request;
    }
    void await(const std::shared_ptr<Request>& request){
        std::unique_lock<std::mutex> lock(mutex);
        wait(lock,[&]{return request->done || error || stopped;});
        if(request->error)std::rethrow_exception(request->error);
        if(error)std::rethrow_exception(error);
        if(!request->done)throw std::runtime_error("RSX owner request cancelled");
    }
    void call(Kind kind,const std::function<void()>& invoke){await(post(kind,invoke));}
    void owner_call(const std::function<void()>& invoke){
        if(current!=this)throw std::runtime_error("coordinator call outside RSX owner");
        auto request=std::make_shared<Request>();request->invoke=invoke;request->origin=HostAccessContext::sample();
        std::unique_lock<std::mutex> lock(mutex);if(!tearing_down)check_locked();
        if(coordinator.size()>=capacity)throw std::runtime_error("RSX coordinator queue capacity exceeded");
        coordinator.push_back(request);coordinator_pending.fetch_add(1,std::memory_order_release);++generation;changed.notify_all();if(coordinator_wake)coordinator_wake();
        changed.wait(lock,[&]{return request->done || (!tearing_down && (error || closing));});
        if(request->error)std::rethrow_exception(request->error);
        if(error && !tearing_down)std::rethrow_exception(error);
        if(!request->done)throw std::runtime_error("RSX coordinator request cancelled");
    }
    void owner_post(std::function<void()> invoke){
        if(current!=this)throw std::runtime_error("coordinator post outside RSX owner");
        auto request=std::make_shared<Request>();request->invoke=std::move(invoke);request->asynchronous=true;request->origin=HostAccessContext::sample();
        std::lock_guard<std::mutex> lock(mutex);check_locked();
        if(coordinator.size()>=capacity)throw std::runtime_error("RSX coordinator queue capacity exceeded");
        coordinator.push_back(std::move(request));coordinator_pending.fetch_add(1,std::memory_order_release);++generation;changed.notify_all();if(coordinator_wake)coordinator_wake();
    }
    void launch(){
        if(thread.joinable())throw std::runtime_error("RSX owner already launched");
        thread=std::thread([this]{current=this;owner=std::this_thread::get_id();
            std::shared_ptr<Request> request;
            try{const auto scope=thread_scope?thread_scope():nullptr;for(;;){
                {std::unique_lock<std::mutex> lock(mutex);const auto seen=generation;
                    if(queue.empty() && !closing){lock.unlock();if(service)service();
                        const auto until=deadline?deadline():std::chrono::steady_clock::time_point::max();lock.lock();
                        // No deadline: wait without a timeout. A libstdc++ condition_variable without
                        // pthread_cond_clockwait waits on system_clock and converts the steady time point;
                        // time_point::max() then overflows into the past and the owner would spin.
                        const auto ready=[&]{return closing || !queue.empty() || generation!=seen;};
                        if(until==std::chrono::steady_clock::time_point::max())changed.wait(lock,ready);else changed.wait_until(lock,until,ready);}
                    if(closing && queue.empty())break;
                    if(queue.empty())continue;
                    request=queue.front();queue.pop_front();++generation;changed.notify_all();}
                if(service)service();
                HostAccessContext origin(request->origin);HostByteAccess bytes(request->byte_access,true);
                if(request->kind==Kind::RunFifo){if(request->invoke)request->invoke();if(!run_fifo)throw std::runtime_error("RSX FIFO owner callback absent");run_fifo(request->budget);}
                else if(request->invoke)request->invoke();
                if(after_request)after_request();
                {std::lock_guard<std::mutex> lock(mutex);request->done=true;completed_sequence=request->sequence;if(request->kind==Kind::Stop)closing=true;++generation;changed.notify_all();}
                request.reset();
            }}catch(...){auto failure=std::current_exception();if(request){std::lock_guard<std::mutex> lock(mutex);request->error=failure;request->done=true;}
                poison(failure,request?request->sequence:0,request?request->origin:HostAccessOrigin{});}
            // Renderer resources are destroyed on the same owner, even after
            // poison. Teardown cannot publish another successful completion.
            {std::lock_guard<std::mutex> lock(mutex);tearing_down=true;}
            try{if(idle_teardown)idle_teardown();}catch(...){poison(std::current_exception(),0,{});}
            {std::lock_guard<std::mutex> lock(mutex);stopped=true;++generation;changed.notify_all();}current=nullptr;
        });
    }
    void drain(){await(post(Kind::Drain));}
    void shutdown()noexcept{
        if(!thread.joinable())return;
        {std::lock_guard<std::mutex> lock(mutex);closing=true;
            const auto cancelled=std::make_exception_ptr(std::runtime_error("RSX owner shutdown cancelled request"));
            for(auto& list:{&queue,&coordinator}){if(list==&coordinator && tearing_down)continue;
                for(auto it=list->begin();it!=list->end();){auto& request=*it;
                    if(list==&coordinator && request->asynchronous && !error){++it;continue;}
                    request->error=error?error:cancelled;request->done=true;it=list->erase(it);if(list==&coordinator)coordinator_pending.fetch_sub(1,std::memory_order_release);
                }
            }
            ++generation;changed.notify_all();}
        // Teardown can still need the SDL/coordinator service. Never join
        // while the owner is blocked waiting for this very thread.
        if(std::this_thread::get_id()==coordinator_thread){pump_coordinator();std::unique_lock<std::mutex> lock(mutex);wait(lock,[&]{return stopped;});}
        thread.join();
    }
    ~RsxOwnerQueue(){shutdown();}
    RsxOwnerQueue()=default;RsxOwnerQueue(const RsxOwnerQueue&)=delete;
};
'''
