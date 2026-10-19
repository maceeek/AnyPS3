"""Validated exact-span commits inside a transferred GPU claim, without CPU admission."""
MEMBERS = r'''
        std::thread::id payload_owner=std::this_thread::get_id();
        void adopt(){
            if(!broker)throw std::runtime_error("adopt of retired GPU claim");
            auto& local=memory->local_state();if(local.leases || local.raw)throw std::runtime_error("GPU claim transfer inside CPU/raw admission");
            payload_owner=std::this_thread::get_id();
        }
        void require_owner()const{
            if(!broker || payload_owner!=std::this_thread::get_id())throw std::runtime_error("foreign or retired GPU claim payload");
            std::lock_guard<std::mutex> lock(broker->mutex);broker->check();
        }
        // These Range methods are templates because Range is declared after
        // the broker in GuestMemory; instantiation occurs after its definition.
        // Sorted union of the declared spans per backing, built once per claim.
        // Exact coverage then costs O(log S) per covered interval instead of a
        // scan of every span per step: a texture claim with many runs made each
        // snapshot O(S^2) and stalled the RSX owner for ~25 s (game run 334st).
        struct Covered {std::uintptr_t pin,begin,end;};
        std::vector<Covered> covered;const HostSpan* covered_spans=nullptr;std::size_t covered_count=0;
        const std::vector<Covered>& coverage(){
            if(covered_spans==spans.data() && covered_count==spans.size())return covered;
            covered.clear();covered.reserve(spans.size());
            for(const auto& span:spans){const auto begin=reinterpret_cast<std::uintptr_t>(span.pointer);
                covered.push_back({reinterpret_cast<std::uintptr_t>(span.pin.get()),begin,begin+span.size});}
            std::sort(covered.begin(),covered.end(),[](const Covered& a,const Covered& b){return a.pin<b.pin || (a.pin==b.pin && a.begin<b.begin);});
            std::size_t n=0;
            for(const auto& c:covered){if(n && covered[n-1].pin==c.pin && c.begin<=covered[n-1].end)covered[n-1].end=std::max(covered[n-1].end,c.end);else covered[n++]=c;}
            covered.resize(n);covered_spans=spans.data();covered_count=spans.size();return covered;
        }
        template<class Range> std::vector<HostSpan> commit_spans(const Range& range,unsigned offset,unsigned count,bool write){
            require_owner();
            if(range.memory!=memory || (write && !range.writable) || offset>range.size || count>range.size-offset)
                throw std::runtime_error("GPU claim range permission/bounds fault");
            range.check_epoch();
            for(const auto& part:range.parts){const auto ea=range.address+part.offset;
                bool valid=false;for(const auto& map:memory->parallel->snapshot->maps){const auto& r=map.region;
                    if(r.identity==part.mapping_id && r.bytes==part.owner && ea>=r.base && ea-r.base<r.bytes->size() &&
                       part.size<=r.bytes->size()-(ea-r.base) && r.bytes->data()+ea-r.base==part.data &&
                       (r.flags&(write?2:4)) && (!write || !(r.flags&1)))valid=true;}
                if(!valid)throw std::runtime_error("GPU commit stale mapping identity");
            }
            std::vector<HostSpan> result;
            for(const auto& part:range.parts){const auto first=std::max(offset,part.offset),last=std::min(offset+count,part.offset+part.size);
                if(first>=last)continue;
                const auto* pointer=part.data+first-part.offset;
                auto at=reinterpret_cast<std::uintptr_t>(pointer),end=at+last-first;
                // Require exact declared bytes, not merely their page keys.
                const auto& index=coverage();const auto pin=reinterpret_cast<std::uintptr_t>(part.owner.get());
                while(at<end){
                    auto it=std::upper_bound(index.begin(),index.end(),std::make_pair(pin,at),[](const std::pair<std::uintptr_t,std::uintptr_t>& key,const Covered& c){
                        return key.first<c.pin || (key.first==c.pin && key.second<c.begin);});
                    if(it==index.begin() || (--it)->pin!=pin || at>=it->end)throw std::runtime_error("GPU commit outside declared spans");
                    at=std::min(end,it->end);
                }
                result.push_back({const_cast<std::uint8_t*>(pointer),last-first,part.owner});
            }
            unsigned total=0;for(const auto& span:result)total+=span.size;
            if(total!=count)throw std::runtime_error("GPU commit range contains a hole");
            return result;
        }
        template<class Range> void commit(const Range& range,unsigned offset,const std::uint8_t* bytes,unsigned count){
            const auto plan=commit_spans(range,offset,count,true);
            LineLocks locks(*memory,plan);memory->bump_parallel(plan);
            for(const auto& span:plan){atomic_write(span.pin->coherent(span.pointer),bytes,span.size);bytes+=span.size;memory->publish_gpu_write(span.pointer,span.size);}
        }
        // Same validated exact spans as snapshot(), written to caller storage.
        template<class Range> void snapshot_into(const Range& range,unsigned offset,unsigned count,std::uint8_t* out){
            const auto plan=commit_spans(range,offset,count,false);
            for(const auto& span:plan){atomic_read(span.pin->coherent(span.pointer),out,span.size);out+=span.size;}
        }
        template<class Range> std::vector<std::uint8_t> snapshot(const Range& range,unsigned offset,unsigned count){
            const auto plan=commit_spans(range,offset,count,false);std::vector<std::uint8_t> result(count);unsigned at=0;
            for(const auto& span:plan){atomic_read(span.pin->coherent(span.pointer),result.data()+at,span.size);at+=span.size;}return result;
        }
        template<class Range> std::uint32_t fetch_or32(const Range& range,unsigned offset,std::uint32_t bits){
            const auto plan=commit_spans(range,offset,4,true);
            LineLocks locks(*memory,plan);std::uint8_t bytes[4];unsigned at=0;
            for(const auto& span:plan){atomic_read(span.pin->coherent(span.pointer),bytes+at,span.size);at+=span.size;}
            std::uint32_t old=0;for(auto byte:bytes)old=(old<<8)|byte;
            const auto value=old|bits;for(unsigned i=0;i<4;++i)bytes[i]=value>>(24-8*i);
            memory->bump_parallel(plan);at=0;for(const auto& span:plan){atomic_write(span.pin->coherent(span.pointer),bytes+at,span.size);at+=span.size;memory->publish_gpu_write(span.pointer,span.size);}
            return old;
        }
'''
