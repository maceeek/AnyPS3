"""Visual-companion history through native alias transitions.

An alias merge or equal-footprint rebind moves guest bytes between surface
layouts with a native pack/unpack. The native result stays authoritative. This
plan maps every destination texel whose bytes all come from one whole source
texel (same guest format and bpp, 4-byte aligned words) to that source texel,
so the paired visual companion can copy the source's subpixel block instead of
nearest-upscaling native pixels. Partially covered texels, mixed formats and
guest background keep the native reconstruction.
"""
MEMBERS = r'''
    struct VisualHistoryPair {unsigned from,to,bytes;};  // packed byte offsets of a physically identical byte range
    struct VisualHistoryPlan {
        std::weak_ptr<Surface::ReadbackPlan> from,to;std::array<unsigned,14> key{};
        std::shared_ptr<const std::vector<VkImageCopy>> regions;std::uint64_t texels=0,clock=0;bool complete=false;
    };
    struct VisualHistoryUse {std::shared_ptr<const std::vector<VkImageCopy>> regions;std::uint64_t texels=0;bool complete=false;};
    std::vector<VisualHistoryPlan> visual_history_plans;std::uint64_t visual_history_clock=0;
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
        const auto key=visual_history_key(from,to,pairs);
        for(auto& plan:visual_history_plans)if(plan.key==key && plan.from.lock()==from.readback && plan.to.lock()==to.readback){
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
        struct Run {std::array<unsigned,7> k;}; // layer,col,len,src_layer,src_col,row,src_row
        std::vector<Run> runs;std::uint64_t mapped=0;bool usable=true;
        // gy/gx: guest row/pixel of the destination, sy/sx of the source; len texels of one phase.
        auto run=[&](unsigned gy,unsigned gx,unsigned sy,unsigned sx,unsigned len){
            runs.push_back({{(gy%tny)*tnx+gx%tnx,gx/tnx,len,(sy%fny)*fnx+sx%fnx,sx/fnx,gy/tny,sy/fny}});mapped+=len;};
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
                if(tnx==1 && fnx==1)run(rc,gx,ra,sx,pixels);
                else if(tnx==2 && fnx==2 && gx%2==sx%2){for(unsigned i=0;i<2 && i<pixels;++i)run(rc,gx+i,ra,sx+i,(pixels-i+1)/2);}
                else for(unsigned i=0;i<pixels;++i)run(rc,gx+i,ra,sx+i,1);
                if(runs.size()>(1u<<20))usable=false;
            }
        }
        // A plan's physical->logical map is injective and the pairs are
        // disjoint physical ranges, so runs never overlap: mapped counts
        // distinct destination texels. Merge rows where both sides advance.
        std::sort(runs.begin(),runs.end(),[](const Run& l,const Run& r){return l.k<r.k;});
        auto plan=std::make_shared<std::vector<VkImageCopy>>();usable=usable && mapped!=0;
        for(std::size_t i=0;i<runs.size() && usable;){
            const auto& f=runs[i].k;std::size_t j=i+1;
            while(j<runs.size() && std::equal(f.begin(),f.begin()+5,runs[j].k.begin()) && runs[j].k[5]==f[5]+(j-i) && runs[j].k[6]==f[6]+(j-i))++j;
            VkImageCopy r{};r.srcSubresource={0,0,f[3],1};r.dstSubresource={0,0,f[0],1};
            r.srcOffset={int(f[4]),int(f[6]),0};r.dstOffset={int(f[1]),int(f[5]),0};r.extent={f[2],unsigned(j-i),1};
            plan->push_back(r);if(plan->size()>65536)usable=false;i=j;
        }
        if(visual_history_plans.size()>=64)visual_history_plans.erase(std::min_element(visual_history_plans.begin(),visual_history_plans.end(),[](const auto& x,const auto& y){return x.clock<y.clock;}));
        VisualHistoryPlan entry;entry.from=from.readback;entry.to=to.readback;entry.key=key;entry.clock=++visual_history_clock;
        if(usable){entry.regions=std::move(plan);entry.texels=mapped;entry.complete=mapped==texels;}
        const VisualHistoryUse use{entry.regions,entry.texels,entry.complete};
        visual_history_plans.push_back(std::move(entry));
        if(!usable)++visual_history_rejects;
        return use;
    }
'''
