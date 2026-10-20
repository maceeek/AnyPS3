"""Physical reservation notifications; registration precedes predicate recheck."""
TYPES = r'''
    struct ReservationNotifications {
        struct Listener {
            std::mutex mutex;std::function<void()> wake;
            void notify(){std::lock_guard<std::mutex> lock(mutex);if(wake)wake();}
            void detach(){std::lock_guard<std::mutex> lock(mutex);wake={};}
        };
        std::mutex mutex;
        std::unordered_map<std::uintptr_t,std::vector<std::weak_ptr<Listener>>> lines;
        std::atomic<unsigned> count{0};
        // Conservative physical-line filter. Hash collisions only cause the
        // old locked lookup; an unrelated hot store no longer locks the
        // listener registry merely because some SPU is asleep elsewhere.
        std::array<std::atomic<unsigned>,4096> buckets{};
        // Exact change filter for reservation holders (SPU threads), independent of subscriptions. Every
        // guest-visible bump of a tracked granule (bump_scalar/bump_parallel run under the stripe lock,
        // AFTER the version increment and BEFORE the guest bytes change) and every map-epoch change lands
        // in notify() below and increments a per-line-hash counter (64-bit: no wrap in practice) or the
        // global one. A holder samples observe() BEFORE a successful token compare; while observe() still
        // returns the sampled value no bump has been published since that sample, so the token cannot
        // have changed after the compare (a bump whose notify() is still in flight is ordered after the
        // observer, exactly as if it had taken the stripe lock first). Hash collisions and spans wider
        // than the table only make the holder fall back to the full compare.
        // One cache line per counter: pollers read these at millions of checks per second, so an unrelated
        // neighbouring granule's bump must not invalidate the line they hold (256 KB, only used lines are hot).
        struct alignas(64) Counter {std::atomic<std::uint64_t> value{0};};
        std::array<Counter,4096> touched{};
        alignas(64) std::atomic<std::uint64_t> touched_all{0};
        using Observation=std::array<std::uint64_t,3>;
        Observation observe(std::uintptr_t pointer)const{
            // A reservation granule is one 128-byte guest line; an odd mapping base can straddle two
            // physical lines (same span as subscribe()).
            const auto first=pointer/128,last=(pointer+127)/128;
            return {touched[first%touched.size()].value.load(std::memory_order_acquire),
                    touched[last%touched.size()].value.load(std::memory_order_acquire),
                    touched_all.load(std::memory_order_acquire)};
        }
        void touch(const void* pointer,std::uint64_t size,bool all){
            if(all){touched_all.fetch_add(1,std::memory_order_acq_rel);return;}
            if(!size)return;
            const auto first=reinterpret_cast<std::uintptr_t>(pointer)/128,last=(reinterpret_cast<std::uintptr_t>(pointer)+size-1)/128;
            if(last-first>=touched.size()){touched_all.fetch_add(1,std::memory_order_acq_rel);return;}
            for(auto line=first;line<=last;++line)touched[line%touched.size()].value.fetch_add(1,std::memory_order_acq_rel);
        }
        std::shared_ptr<Listener> subscribe(std::uintptr_t pointer,std::function<void()> wake){
            auto listener=std::make_shared<Listener>();listener->wake=std::move(wake);
            std::lock_guard<std::mutex> lock(mutex);
            // Reservation tokens identify a 128-byte guest granule; unusual
            // mid-line mappings may cover two physical lines.
            for(auto line=pointer/128;line<=(pointer+127)/128;++line){lines[line].push_back(listener);buckets[line%buckets.size()].fetch_add(1,std::memory_order_release);}
            count.fetch_add(1,std::memory_order_release);return listener;
        }
        void remove(std::uintptr_t pointer,const std::shared_ptr<Listener>& listener){
            listener->detach();
            std::lock_guard<std::mutex> lock(mutex);
            for(auto line=pointer/128;line<=(pointer+127)/128;++line){auto it=lines.find(line);if(it==lines.end())continue;
                auto& list=it->second;const auto old=list.size();list.erase(std::remove_if(list.begin(),list.end(),[&](const auto& weak){auto live=weak.lock();return !live || live==listener;}),list.end());
                buckets[line%buckets.size()].fetch_sub(old-list.size(),std::memory_order_release);
                if(list.empty())lines.erase(it);}
            count.fetch_sub(1,std::memory_order_release);
        }
        void notify(const void* pointer,std::uint64_t size,bool all=false){
            touch(pointer,size,all);
            if(!count.load(std::memory_order_acquire) || (!size && !all))return;
            if(!all){const auto first=reinterpret_cast<std::uintptr_t>(pointer)/128,last=(reinterpret_cast<std::uintptr_t>(pointer)+size-1)/128;
                if(last-first<buckets.size()){bool possible=false;for(auto line=first;line<=last;++line)possible|=buckets[line%buckets.size()].load(std::memory_order_acquire)!=0;if(!possible)return;}}
            std::vector<std::shared_ptr<Listener>> targets;
            {std::lock_guard<std::mutex> lock(mutex);
                const auto first=reinterpret_cast<std::uintptr_t>(pointer)/128;
                const auto last=all?UINTPTR_MAX:(reinterpret_cast<std::uintptr_t>(pointer)+size-1)/128;
                auto collect=[&](const auto& list){for(const auto& weak:list)if(auto listener=weak.lock())targets.push_back(std::move(listener));};
                if(all || last-first>=lines.size()){for(const auto& [line,list]:lines)if(all || (line>=first && line<=last))collect(list);}
                else for(auto line=first;line<=last;++line){const auto it=lines.find(line);if(it!=lines.end())collect(it->second);}
            }
            std::sort(targets.begin(),targets.end());targets.erase(std::unique(targets.begin(),targets.end()),targets.end());
            // Registry lock is released before waking; callbacks only publish
            // a worker CV generation and never enter guest memory or RPC.
            for(const auto& target:targets)target->notify();
        }
    };
    struct ReservationWatch {
        std::shared_ptr<ReservationNotifications> notifications;
        std::shared_ptr<ReservationNotifications::Listener> listener;
        std::uintptr_t pointer;
        ReservationWatch(std::shared_ptr<ReservationNotifications> n,std::uintptr_t p,std::function<void()> wake):notifications(std::move(n)),pointer(p){listener=notifications->subscribe(p,std::move(wake));}
        ~ReservationWatch(){notifications->remove(pointer,listener);}
        ReservationWatch(const ReservationWatch&)=delete;
    };
'''
