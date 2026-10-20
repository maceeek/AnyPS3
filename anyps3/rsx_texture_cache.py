"""Physical page indices and validated lazy texture plans for the graphics session."""
MEMBERS = r'''
    // The measured level retains 307 raw images / 297 MiB and 319 plans.
    // Half a GiB holds this working set on the 12-GiB target without making
    // the cache unbounded. Metadata keeps its independent 64-MiB budgets.
    static constexpr unsigned texture_cache_entries=512;
    static constexpr std::uint64_t raw_cache_limit=UINT64_C(512)*1024*1024;
    struct MetadataEntry {Texture texture;std::uint64_t used;};
    std::map<std::array<unsigned,4>,MetadataEntry> texture_plans;
    struct PlanEntry {std::shared_ptr<TexturePlan> plan;std::uint64_t used;};
    std::map<std::array<unsigned,4>,PlanEntry> layout_plans;
    std::uint64_t layout_plan_bytes=0,layout_clock=0;
    std::uint64_t texture_plan_bytes=0;
    std::vector<std::uint64_t> texture_context,context_scratch;
    TargetCache target_cache;
    bool fifo_context=false,context_validated=false;
    std::uint64_t texture_metadata_hits=0,texture_metadata_misses=0;
    void validate_texture_context(){
        if(fifo_context && context_validated)return;
        if(!state.memory.concurrent())target_cache.refresh_reservations(state.memory);
        if(state.coordinator_maps)state.maps=state.memory.regions;
        // Regions can be mutated directly. Compare exact mapping identities,
        // rights and lengths before reusing a validated plan, never just a hash.
        context_scratch.clear();context_scratch.push_back(state.rsx.local_size);
        for(auto v:state.rsx.io)context_scratch.push_back(v);
        for(auto v:state.rsx.io_flags)context_scratch.push_back(v);
        for(const auto& t:state.rsx.tiles){context_scratch.insert(context_scratch.end(),{t.bound,t.location,t.offset,t.size,t.pitch,t.compression,t.base,t.bank,t.range_word,t.format_word});}
        for(const auto& r:state.maps)context_scratch.insert(context_scratch.end(),{r.base,r.flags,r.identity,reinterpret_cast<std::uintptr_t>(r.bytes.get()),reinterpret_cast<std::uintptr_t>(r.bytes->data()),r.bytes->size()});
        if(context_scratch!=texture_context){texture_plans.clear();layout_plans.clear();target_cache.entries.clear();texture_plan_bytes=layout_plan_bytes=0;texture_context=context_scratch;}
        context_validated=true;
    }
    Texture texture_metadata(unsigned unit,bool context_validated=false){
        if(fallback_stats().enabled)++fallback_stats().paths["texture_metadata"];
        if(!context_validated)validate_texture_context();
        const Reg r{state.rsx};const unsigned base=0x1a00+32*unit;
        const std::array<unsigned,4> key{r(base),r(base+4),r(base+0x18),r(0x1840+4*unit)};
        auto found=texture_plans.find(key);
        if(found==texture_plans.end()){
            // Address/format/dimensions/pitch determine the physical plan.
            // Remap/filter/LOD/alpha state affect sampling, never guest layout.
            const auto& layout_key=key;
            auto layout=layout_plans.find(layout_key);
            const auto reusable=layout==layout_plans.end()?std::shared_ptr<TexturePlan>{}:layout->second.plan;
            auto t=decode_texture(state,r,unit,false,true,reusable);
            if(layout==layout_plans.end()){
                const auto bytes=t.plan->runs.size()*sizeof(TexturePlan::Run);
                while(!layout_plans.empty() && (layout_plans.size()>=texture_cache_entries || layout_plan_bytes+bytes>64*1024*1024)){
                    auto old=std::min_element(layout_plans.begin(),layout_plans.end(),[](const auto& a,const auto& b){return a.second.used<b.second.used;});layout_plan_bytes-=old->second.plan->runs.size()*sizeof(TexturePlan::Run);layout_plans.erase(old);}
                layout_plan_bytes+=bytes;layout_plans.emplace(layout_key,PlanEntry{t.plan,++layout_clock});
                if(fallback_stats().enabled)++fallback_stats().paths["texture_layout_miss"];
            }else{layout->second.used=++layout_clock;if(fallback_stats().enabled)++fallback_stats().paths["texture_layout_hit"];}
            const auto cost=t.plan->runs.size()*sizeof(TexturePlan::Run)+t.levels.size()*sizeof(TextureLevel);
            while(!texture_plans.empty() && (texture_plans.size()>=texture_cache_entries || texture_plan_bytes+cost>64*1024*1024)){
                auto old=std::min_element(texture_plans.begin(),texture_plans.end(),[](const auto& a,const auto& b){return a.second.used<b.second.used;});
                const auto& texture=old->second.texture;texture_plan_bytes-=texture.plan->runs.size()*sizeof(TexturePlan::Run)+texture.levels.size()*sizeof(TextureLevel);texture_plans.erase(old);}
            // One oversized plan remains usable for this draw, but is not
            // retained in the metadata map after the next cache miss.
            texture_plan_bytes+=cost;
            found=texture_plans.emplace(key,MetadataEntry{std::move(t),++layout_clock}).first;++texture_metadata_misses;if(fallback_stats().enabled)++fallback_stats().paths["texture_metadata_miss"];
        }else{++texture_metadata_hits;if(fallback_stats().enabled)++fallback_stats().paths["texture_metadata_hit"];if(host_stats().enabled)++host_stats().texture_plan_hit;}
        found->second.used=++layout_clock;
        auto t=found->second.texture;configure_texture_sampling(t,r,unit);
        const auto format=r(base+4),filter=r(base+0x14);
        const auto& level=static_cast<const Texture&>(t).levels[0];
        if(!t.cube && t.dimension==2 && t.mip_count==1 && !level.border && !(filter>>28))
            t.resident_image=rsx_graphics_texture(state,r(base),(format&3)-1,level.width,level.height,level.pitch,t.format,t.swizzled);
        if(!t.resident_image && !t.cube && t.dimension==2 && t.mip_count==1 && !level.border && !(filter>>28))
            t.resident_image=rtt_image(t,r(base),(format&3)-1);
        return t;
    }
    std::unordered_map<std::uintptr_t,std::vector<std::uint64_t>> raw_pages;
    std::vector<std::uint64_t> invalidation_candidates;
    std::unordered_map<std::uint64_t,std::size_t> raw_positions;
    std::unordered_multimap<std::size_t,std::uint64_t> sampled_entries;
    static std::size_t sampled_key(const std::vector<unsigned>& key,const void* origin,std::uint64_t size,std::uint64_t storage){
        return SourceKeyHash{}(key)^(reinterpret_cast<std::uintptr_t>(origin)*UINT64_C(0x9e3779b97f4a7c15))^(size*16777619u)^(storage<<1);
    }
    static std::size_t sampled_key(const RawTexture& e){return sampled_key(e.key,e.plan?static_cast<const void*>(e.plan.get()):e.origin,e.size,e.origin_size);}
    std::uint64_t raw_identity=0;
    std::uint64_t raw_revision=1;
    struct WriteProof {std::weak_ptr<Surface::ReadbackPlan> layout;std::uint64_t revision;};
    std::unordered_map<const Surface::ReadbackPlan*,WriteProof> write_proofs;
    struct SurfaceWatch {const void* first;std::uint64_t second;std::shared_ptr<GuestMemory::Range> mapping;};
    std::vector<SurfaceWatch> surface_watches;
    std::vector<std::shared_ptr<GuestMemory::Range>> surface_watch_mappings;
    template<class F> void raw_spans(const RawTexture& e,F f)const{
        if(e.plan){for(const auto& run:e.plan->runs)f(run.data,run.size);}
        else{f(e.source,e.size);if(e.origin && e.origin!=e.source)f(e.origin,e.origin_size);}
    }
    void watch_span(const void* p,std::uint64_t n,bool add){
        if(!observer_installed || previous_observer || !n)return;
        GuestMemory::MetadataPermit metadata(state.memory);state.memory.host_access_observer.watch(p,n,add);
    }
    void watch_poll(const void* p,std::uint64_t n,bool add){
        if(!observer_installed || previous_observer || !n)return;
        GuestMemory::MetadataPermit metadata(state.memory);state.memory.host_access_observer.watch_poll(p,n,add);
    }
    std::shared_ptr<RawTexture::DirtyInputs> dirty_inputs(const Texture& t,const void* source,std::uint64_t size){
        if(!state.memory.concurrent() || (!t.plan && t.untiled))return {};
        auto input=std::make_shared<RawTexture::DirtyInputs>(state.memory);
        std::vector<GuestMemory::Range> mappings;
        if(t.plan){mappings.reserve(t.plan->runs.size());for(const auto& run:t.plan->runs){
            if(run.size>UINT_MAX)throw std::runtime_error("RSX dirty input exceeds guest range");
            if(run.address==UINT64_MAX){for(auto& mapping:pinned_host_ranges(state.memory,run.data,run.size,false))mappings.push_back(std::move(mapping));}
            else{auto mapping=state.memory.range(run.address,run.size,false);
                if(mapping.parts.size()!=1 || mapping.parts[0].data!=run.data || mapping.parts[0].owner!=run.owner || mapping.parts[0].mapping_id!=run.mapping_id)throw std::runtime_error("stale RSX dirty texture mapping");
                mappings.push_back(std::move(mapping));}
        }}else{if(size>UINT_MAX)throw std::runtime_error("RSX dirty input exceeds guest range");mappings=pinned_host_ranges(state.memory,source,size,false);}
        // PAR11 validates and indexes the exact physical union once, retaining
        // holes and all original map identities in one dirty token.
        input->watch=state.memory.watch_cpu_dirty(std::move(mappings));
        // Clear BEFORE snapshot. Any writer admitted during/after the copy
        // leaves a new bit; never acknowledge it at upload completion.
        input->consume();return input;
    }
    void dirty_raw(RawTexture& e,const char* reason){
        if(e.invalid)return;
        e.invalid=true;++raw_revision;
        ++raw_invalidations;if(fallback_stats().enabled)++fallback_stats().paths[std::string("texture_invalidate_")+reason];
    }
    bool raw_dirty(RawTexture& e){
        if(e.dirty_inputs && e.dirty_inputs->consume())dirty_raw(e,"cpu_dirty");
        return e.invalid;
    }
    void trim_raw(std::uint64_t reuse,std::uint64_t cost){
        while(!raw_textures.empty()){
            const auto found=raw_positions.find(reuse);const bool replacing=reuse && found!=raw_positions.end();
            const auto reclaimed=replacing?raw_textures[found->second].cost:0;
            if(raw_textures.size()+(replacing?0:1)<=texture_cache_entries && raw_bytes-reclaimed+cost<=raw_cache_limit)return;
            // A replacement adds no member and reclaims its old cost. Keep its
            // token/ID while evicting other LRU entries, unless it alone is huge.
            auto old=std::min_element(raw_textures.begin(),raw_textures.end(),[&](const auto& a,const auto& c){return std::make_pair(a.id==reuse,a.last_use)<std::make_pair(c.id==reuse,c.last_use);});
            erase_raw(old-raw_textures.begin(),nullptr);++raw_evictions;
        }
    }
    void store_raw(RawTexture entry,std::uint64_t reuse){
        // An individually larger upload can be used by its pinned draw, but
        // must not leave an oversized cache entry behind after retirement.
        if(entry.cost>raw_cache_limit)return;
        const auto found=raw_positions.find(reuse);
        if(reuse && found!=raw_positions.end()){
            auto& old=raw_textures[found->second];entry.id=old.id;raw_bytes-=old.cost;raw_bytes+=entry.cost;
            // A formerly untrusted entry can become protectable after late
            // observe(). Rebuild HostGuard before exposing retained pointers.
            old=std::move(entry);++raw_revision;++ownership_revision;return;
        }
        raw_bytes+=entry.cost;raw_textures.push_back(std::move(entry));index_raw(raw_textures.back());
    }
    void index_raw(RawTexture& e){
        ++raw_revision;
        ++ownership_revision;
        e.id=++raw_identity;raw_positions[e.id]=raw_textures.size()-1;
        if(e.image)sampled_entries.emplace(sampled_key(e),e.id);
        raw_spans(e,[&](const void* p,std::uint64_t n){if(!n)return;if(!e.dirty_inputs)watch_span(p,n,true);
            const auto first=reinterpret_cast<std::uintptr_t>(p)>>12,last=(reinterpret_cast<std::uintptr_t>(p)+n-1)>>12;
            for(auto page=first;page<=last;++page){auto& ids=raw_pages[page];if(std::find(ids.begin(),ids.end(),e.id)==ids.end())ids.push_back(e.id);}});
    }
    void erase_raw(std::size_t position,const char* reason){
        ++raw_revision;
        ++ownership_revision;
        const auto& e=raw_textures[position];raw_bytes-=e.cost;
        if(e.image){const auto range=sampled_entries.equal_range(sampled_key(e));for(auto it=range.first;it!=range.second;++it)if(it->second==e.id){sampled_entries.erase(it);break;}}
        if(reason){++raw_invalidations;if(fallback_stats().enabled)++fallback_stats().paths[std::string("texture_invalidate_")+reason];}
        raw_spans(e,[&](const void* p,std::uint64_t n){if(!n)return;if(!e.dirty_inputs)watch_span(p,n,false);
            const auto first=reinterpret_cast<std::uintptr_t>(p)>>12,last=(reinterpret_cast<std::uintptr_t>(p)+n-1)>>12;
            for(auto page=first;page<=last;++page){auto it=raw_pages.find(page);if(it!=raw_pages.end()){auto& ids=it->second;ids.erase(std::remove(ids.begin(),ids.end(),e.id),ids.end());if(ids.empty())raw_pages.erase(it);}}});
        raw_positions.erase(e.id);if(position+1!=raw_textures.size()){raw_textures[position]=std::move(raw_textures.back());raw_positions[raw_textures[position].id]=position;}
        raw_textures.pop_back();
    }
    void clear_raw(const char* reason){while(!raw_textures.empty())erase_raw(raw_textures.size()-1,reason);}
    void invalidate_raw(const void* p,std::uint64_t n,const char* reason){
        if(!n || raw_pages.empty())return;
        auto& candidates=invalidation_candidates;candidates.clear();const auto first=reinterpret_cast<std::uintptr_t>(p)>>12,last=(reinterpret_cast<std::uintptr_t>(p)+n-1)>>12;
        if(last-first>raw_pages.size()){for(const auto& page:raw_pages)if(page.first>=first && page.first<=last)candidates.insert(candidates.end(),page.second.begin(),page.second.end());}
        else for(auto page=first;page<=last;++page){auto it=raw_pages.find(page);if(it!=raw_pages.end())candidates.insert(candidates.end(),it->second.begin(),it->second.end());}
        std::sort(candidates.begin(),candidates.end());candidates.erase(std::unique(candidates.begin(),candidates.end()),candidates.end());
        for(auto id:candidates){auto at=raw_positions.find(id);if(at==raw_positions.end())continue;
            bool overlap=false;raw_spans(raw_textures[at->second],[&](const void* a,std::uint64_t size){const auto x=reinterpret_cast<std::uintptr_t>(a),y=reinterpret_cast<std::uintptr_t>(p);if(x<y+n && y<x+size)overlap=true;});
            if(overlap){const auto& entry=raw_textures[at->second];
                access_trace().record(reason?reason:"invalidate",state,p,n,true,entry.key.empty()?0:entry.key[0],entry.key.size()>17?entry.key[17]:0,entry.key.size()>18?entry.key[18]:0);
                if(entry.dirty_inputs && (!reason || (std::strcmp(reason,"host_write") && std::strcmp(reason,"protected_read"))))dirty_raw(raw_textures[at->second],reason?reason:"invalidate");
                else erase_raw(at->second,reason);}}
    }
    void refresh_surface_watches(){
        ++ownership_revision;index_surfaces();
        std::vector<SurfaceWatch> next;std::vector<std::shared_ptr<GuestMemory::Range>> mappings;
        for(const auto& s:surfaces){mappings.push_back(s.target.mapping);for(const auto& span:s.physical)next.push_back({reinterpret_cast<const void*>(span.begin),span.end-span.begin,s.target.mapping});}
        auto less=[](const SurfaceWatch& a,const SurfaceWatch& b){const auto x=reinterpret_cast<std::uintptr_t>(a.first),y=reinterpret_cast<std::uintptr_t>(b.first);return x<y || (x==y && a.second<b.second);};
        std::sort(next.begin(),next.end(),less);
        // Watch the physical union, independent of surface/sample-grid splits.
        // Pin every original mapping, including aliases hidden by normalization.
        std::size_t count=0;
        for(const auto& span:next){const auto at=reinterpret_cast<std::uintptr_t>(span.first);
            if(count && at<=reinterpret_cast<std::uintptr_t>(next[count-1].first)+next[count-1].second){auto& back=next[count-1];back.second=std::max(reinterpret_cast<std::uintptr_t>(back.first)+back.second,at+span.second)-reinterpret_cast<std::uintptr_t>(back.first);}
            else next[count++]=span;}
        next.resize(count);
        bool equal=surface_watches.size()==next.size();
        if(equal)for(std::size_t i=0;i<next.size();++i)if(less(next[i],surface_watches[i]) || less(surface_watches[i],next[i])){equal=false;break;}
        // ANYPS3_RSX_WATCH_TRACE=1: how often the surface watch set really changes and how many bytes are (un)watched (diagnostic).
        static const bool trace=std::getenv("ANYPS3_RSX_WATCH_TRACE")!=nullptr;
        static std::uint64_t t_calls=0,t_same=0,t_added=0,t_removed=0,t_span_ops=0;
        if(trace){
            ++t_calls;if(equal)++t_same;
            else{std::size_t a=0,b=0;
                while(a<surface_watches.size() || b<next.size()){
                    if(b==next.size() || (a<surface_watches.size() && less(surface_watches[a],next[b]))){t_removed+=surface_watches[a++].second;++t_span_ops;}
                    else if(a==surface_watches.size() || less(next[b],surface_watches[a])){t_added+=next[b++].second;++t_span_ops;}
                    else{++a;++b;}}}
            if(t_calls%200==0)std::fprintf(stderr,"WATCH_STATS refreshes=%llu unchanged=%llu span_ops=%llu added_kb=%llu removed_kb=%llu\n",(unsigned long long)t_calls,(unsigned long long)t_same,(unsigned long long)t_span_ops,(unsigned long long)(t_added>>10),(unsigned long long)(t_removed>>10));
        }
        if(equal){surface_watch_mappings=std::move(mappings);return;}
        GuestMemory::MetadataPermit metadata(state.memory);
        std::size_t a=0,b=0;
        while(a<surface_watches.size() || b<next.size()){
            if(b==next.size() || (a<surface_watches.size() && less(surface_watches[a],next[b]))){const auto& span=surface_watches[a++];watch_span(span.first,span.second,false);}
            else if(a==surface_watches.size() || less(next[b],surface_watches[a])){const auto& span=next[b++];watch_span(span.first,span.second,true);}
            else{++a;++b;}
        }
        surface_watches=std::move(next);surface_watch_mappings=std::move(mappings);
    }
    void dirty_surface(Surface& surface){
        surface.dirty=true;++surface.generation;
        // All overlapping raw entries are absent or marked invalid. Repeating
        // the same immutable footprint needs no page lookup until membership
        // or uploaded contents change. Check its weak identity to defeat pointer
        // reuse without retaining evicted readback plans and guest backings.
        const auto proof=write_proofs.find(surface.readback.get());
        if(proof!=write_proofs.end() && proof->second.revision==raw_revision && proof->second.layout.lock()==surface.readback)return;
        for(const auto& span:surface.physical)invalidate_raw(reinterpret_cast<const void*>(span.begin),span.end-span.begin,"gpu_write");
        if(write_proofs.size()>=128)write_proofs.clear();
        write_proofs[surface.readback.get()]={surface.readback,raw_revision};
    }
    void clear_surfaces(){surfaces.clear();refresh_surface_watches();}
'''
