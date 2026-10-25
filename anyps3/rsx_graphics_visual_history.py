"""Visual-companion history through native alias transitions.

An alias merge or equal-footprint rebind moves guest bytes between surface
layouts with a native pack/unpack. The native result stays authoritative. This
plan maps every destination texel whose bytes all come from one whole source
texel (same guest format and bpp, 4-byte aligned words) to that source texel,
so the paired visual companion can copy the source's subpixel block instead of
nearest-upscaling native pixels. Partially covered texels, mixed formats and
guest background keep the native reconstruction.

Multi-sample surfaces: the visual image is the surface rendered at S times the
extent, so its flattened sample grid has column G=X*nx+sample. Native grid texel
g covers visual grid columns g*S..g*S+S-1, i.e. visual pixels g*S/nx.. of every
plane when S is a multiple of nx (and ny). Equal sample layouts with such a scale
copy those whole-pixel blocks of every plane; a sample-count change (or an odd
scale with MSAA) has no block form and keeps the native reconstruction.
Regions are returned in visual-image units.
"""
MEMBERS = r'''
    struct VisualHistoryPair {unsigned from,to,bytes;
        bool operator==(const VisualHistoryPair& p)const{return from==p.from && to==p.to && bytes==p.bytes;}
    };  // packed byte offsets of a physically identical byte range
    struct VisualHistoryPlan {
        std::weak_ptr<Surface::ReadbackPlan> from,to;std::array<unsigned,14> key{};
        std::vector<VisualHistoryPair> pairs;
        std::vector<unsigned> from_words,to_words; // geometry only: no host pointers, GPU resources or guest pixels
        std::shared_ptr<const std::vector<VkImageCopy>> regions;std::uint64_t texels=0,clock=0;bool complete=false;
        std::uint64_t bytes=0;
    };
    struct VisualHistoryUse {std::shared_ptr<const std::vector<VkImageCopy>> regions;std::uint64_t texels=0;bool complete=false;};
    std::vector<VisualHistoryPlan> visual_history_plans;std::uint64_t visual_history_clock=0,visual_history_plan_bytes=0;
    static constexpr std::uint64_t visual_history_plan_budget=64u<<20;
    std::uint64_t visual_history_structural_hits=0;
    std::uint64_t visual_history_plan_hits=0,visual_history_plan_misses=0,visual_history_rejects=0;
    static std::array<unsigned,14> visual_history_key(const Surface& from,const Surface& to,const std::vector<VisualHistoryPair>& pairs){
        unsigned hash=2166136261u;for(const auto& p:pairs)for(const unsigned v:{p.from,p.to,p.bytes}){hash^=v;hash*=16777619u;}
        return {from.width,from.height,from.samples,from.target.bpp,from.target.pitch,from.format,to.width,to.height,to.samples,to.target.bpp,to.target.pitch,to.format,unsigned(pairs.size()),hash};
    }
    // Returns the cached plan, without regions when the transition cannot
    // retain any visual history. Region coordinates are native texels.
    VisualHistoryUse visual_history_plan(const Surface& from,const Surface& to,VkFormat format,const std::vector<VisualHistoryPair>& pairs){
        // ANYPS3_RENDER_SCALE_HISTORY=0 restores whole native reconstruction (A/B measurement).
        static const bool enabled=[]{const char* v=std::getenv("ANYPS3_RENDER_SCALE_HISTORY");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("ANYPS3_RENDER_SCALE_HISTORY must be 0 or 1");
            return !v || !std::strcmp(v,"1");}();
        if(!enabled || vk.graphics_render_scale<2 || !from.image || !from.image->visual || pairs.empty())return {};
        const bool rgba_compatible=(from.image->format==VK_FORMAT_R8G8B8A8_SRGB && format==VK_FORMAT_R8G8B8A8_UNORM) ||
            (format==VK_FORMAT_R8G8B8A8_SRGB && from.image->format==VK_FORMAT_R8G8B8A8_UNORM);
        if(from.format!=to.format || from.depth!=to.depth || from.target.bpp!=to.target.bpp || from.target.bpp%4 || from.target.bpp>16 ||
           (from.image->format!=format && !rgba_compatible) || !from.readback || !to.readback || from.readback->words.empty() || to.readback->words.empty()){++visual_history_rejects;return {};}
        static const bool trace=std::getenv("ANYPS3_RSX_HISTORY_TRACE")!=nullptr;
        static thread_local std::uint64_t trace_calls=0;
        if(trace && ++trace_calls%60==1)std::fprintf(stderr,"HISTORY_PLAN calls=%llu hits=%llu structural_hits=%llu misses=%llu entries=%zu bytes=%llu\n",
            (unsigned long long)trace_calls,(unsigned long long)visual_history_plan_hits,(unsigned long long)visual_history_structural_hits,
            (unsigned long long)visual_history_plan_misses,visual_history_plans.size(),(unsigned long long)visual_history_plan_bytes);
        const auto key=visual_history_key(from,to,pairs);
        static const bool structural=[]{const char* v=std::getenv("ANYPS3_RSX_HISTORY_GEOMETRY_CACHE");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("ANYPS3_RSX_HISTORY_GEOMETRY_CACHE must be 0 or 1");
            return !v || !std::strcmp(v,"1");}();
        // The hash is a lookup prefilter, never proof that byte-range pairs match.
        // Recreated immutable readback plans may describe exactly the same word permutation.
        // Compare that geometry exactly; retained raw pointers/contents must not participate.
        for(auto& plan:visual_history_plans)if(plan.key==key && plan.pairs==pairs){
            const bool same_owner=plan.from.lock()==from.readback && plan.to.lock()==to.readback;
            if(!same_owner && (!structural || plan.from_words!=from.readback->words || plan.to_words!=to.readback->words))continue;
            if(!same_owner){++visual_history_structural_hits;plan.from=from.readback;plan.to=to.readback;}
            plan.clock=++visual_history_clock;++visual_history_plan_hits;return {plan.regions,plan.texels,plan.complete};}
        ++visual_history_plan_misses;
        // Walk logically contiguous word segments of both tables (one guest
        // row of whole texels each) and emit a texel run per sample phase:
        // O(words) compares, no per-texel division or dense texel map.
        const auto& a=from.readback->words;const auto& c=to.readback->words;
        const std::uint64_t texels=std::uint64_t(to.width)*to.height*to.samples;
        const unsigned bpp=from.target.bpp,fp=from.target.pitch,tp=to.target.pitch;
        const unsigned fnx=from.samples>1?2u:1u,fny=from.samples==4?2u:1u,tnx=to.samples>1?2u:1u,tny=to.samples==4?2u:1u;
        const std::uint64_t frow=std::uint64_t(from.width)*fnx*bpp,trow=std::uint64_t(to.width)*tnx*bpp;
        const unsigned scale=vk.graphics_render_scale;
        // grid: equal multi-sample layouts copy flattened grid runs (see module note).
        const bool grid=(tnx>1 || tny>1) && tnx==fnx && tny==fny && scale%tnx==0 && scale%tny==0;
        if((tnx>1 || tny>1 || fnx>1 || fny>1) && !grid){++visual_history_rejects;return {};}
        static const bool compact=[]{const char* v=std::getenv("ANYPS3_RSX_HISTORY_COMPACT_RUNS");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("ANYPS3_RSX_HISTORY_COMPACT_RUNS must be 0 or 1");
            return !v || !std::strcmp(v,"1");}();
        struct Run {std::array<unsigned,7> k;unsigned rows=1;}; // layer,col,len,src_layer,src_col,row,src_row
        struct RunHash {std::size_t operator()(const std::array<unsigned,5>& k)const{
            std::size_t h=2166136261u;for(auto v:k){h^=v;h*=16777619u;}return h;}};
        std::unordered_map<std::array<unsigned,5>,std::size_t,RunHash> tails;
        std::vector<Run> runs;std::uint64_t mapped=0;unsigned emitted=0;std::size_t cursor=0;bool usable=true;
        // Coalesce consecutive rows before sorting. Sample-phase conversion can otherwise emit
        // one run per texel (hundreds of thousands), although its final plan is only one per column.
        // Exact keys and both row coordinates guard every merge. Arbitrary physical/tiled ordering
        // still works: nonconsecutive runs remain separate for the final canonical sort/merge.
        auto run=[&](unsigned gy,unsigned gx,unsigned sy,unsigned sx,unsigned len){
            if(++emitted>(1u<<20)){usable=false;return;}mapped+=len;
            const std::array<unsigned,5> key{0,gx,len,0,sx};
            const unsigned row=gy,source_row=sy;
            auto extend=[&](std::size_t i){auto& r=runs[i];
                if(!std::equal(key.begin(),key.end(),r.k.begin()))return false;
                if(r.k[5]+r.rows==row && r.k[6]+r.rows==source_row){++r.rows;}
                else if(row+1==r.k[5] && source_row+1==r.k[6]){r.k[5]=row;r.k[6]=source_row;++r.rows;}
                else return false;
                cursor=i+1;return true;};
            if(compact){
                // Repeated raster rows revisit the same column sequence; avoid hashing each texel.
                if(cursor>=runs.size())cursor=0;
                if(cursor<runs.size() && extend(cursor))return;
                auto previous=tails.find(key);if(previous!=tails.end() && extend(previous->second))return;
                tails[key]=runs.size();}
            runs.push_back({{key[0],key[1],key[2],key[3],key[4],row,source_row},1});cursor=runs.size();};
        for(const auto& p:pairs){
            if((p.from|p.to|p.bytes)&3 || p.from/4+std::uint64_t(p.bytes/4)>a.size() || p.to/4+std::uint64_t(p.bytes/4)>c.size())continue;
            const unsigned count=p.bytes/4,x=p.from/4,y=p.to/4;
            for(unsigned k=0;k<count && usable;){
                const unsigned la=a[x+k],lc=c[y+k],ra=la/fp,xa=la%fp,rc=lc/tp,xc=lc%tp;
                if(xa%bpp || xc%bpp || ra>=from.height*fny || rc>=to.height*tny || xa>=frow || xc>=trow){++k;continue;}
                const auto room=std::min<std::uint64_t>(std::min(frow-xa,trow-xc)/4,count-k);
                unsigned n=1;while(n<room && a[x+k+n]==la+4*n && c[y+k+n]==lc+4*n)++n;
                // A trailing partial texel is skipped: it is never mapped.
                const unsigned pixels=n*4/bpp,gx=xc/bpp,sx=xa/bpp;k+=n;
                if(!pixels)continue;
                run(rc,gx,ra,sx,pixels); // flattened grid coordinates (pixel coordinates when single-sample)
            }
        }
        // A plan's physical->logical map is injective and the pairs are
        // disjoint physical ranges, so runs never overlap: mapped counts
        // distinct destination texels. Merge rows where both sides advance.
        std::sort(runs.begin(),runs.end(),[](const Run& l,const Run& r){return l.k<r.k;});
        auto plan=std::make_shared<std::vector<VkImageCopy>>();usable=usable && mapped!=0;
        for(std::size_t i=0;i<runs.size() && usable;){
            const auto& f=runs[i].k;std::size_t j=i+1;unsigned rows=runs[i].rows;
            while(j<runs.size() && std::equal(f.begin(),f.begin()+5,runs[j].k.begin()) && runs[j].k[5]==f[5]+rows && runs[j].k[6]==f[6]+rows){rows+=runs[j].rows;++j;}
            for(unsigned plane=0;plane<tnx*tny;++plane){
                VkImageCopy r{};r.srcSubresource={0,0,plane,1};r.dstSubresource={0,0,plane,1};
                r.srcOffset={int(f[4]*scale/fnx),int(f[6]*scale/fny),0};r.dstOffset={int(f[1]*scale/tnx),int(f[5]*scale/tny),0};
                r.extent={f[2]*scale/tnx,rows*scale/tny,1};plan->push_back(r);}
            if(plan->size()>65536)usable=false;
            i=j;
        }
        VisualHistoryPlan entry;entry.from=from.readback;entry.to=to.readback;entry.key=key;entry.clock=++visual_history_clock;
        entry.pairs=pairs;
        if(usable){entry.regions=std::move(plan);entry.texels=mapped;entry.complete=mapped==texels;}
        const VisualHistoryUse use{entry.regions,entry.texels,entry.complete};
        entry.bytes=sizeof(entry)+entry.pairs.capacity()*sizeof(VisualHistoryPair)+
            (entry.regions?entry.regions->capacity()*sizeof(VkImageCopy):0);
        if(structural && entry.bytes<=visual_history_plan_budget && (a.size()+c.size())*sizeof(unsigned)<=visual_history_plan_budget-entry.bytes){
            entry.from_words=a;entry.to_words=c;
            entry.bytes+=(entry.from_words.capacity()+entry.to_words.capacity())*sizeof(unsigned);}
        if(entry.bytes<=visual_history_plan_budget){
            while(!visual_history_plans.empty() && (visual_history_plans.size()>=64 || visual_history_plan_bytes+entry.bytes>visual_history_plan_budget)){
                auto old=std::min_element(visual_history_plans.begin(),visual_history_plans.end(),[](const auto& x,const auto& y){return x.clock<y.clock;});
                visual_history_plan_bytes-=old->bytes;visual_history_plans.erase(old);}
            visual_history_plan_bytes+=entry.bytes;visual_history_plans.push_back(std::move(entry));}
        if(!usable)++visual_history_rejects;
        return use;
    }
'''
