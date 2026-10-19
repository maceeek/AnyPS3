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
