"""Exact device-only alias transition when old and new physical bytes coincide."""
MEMBERS = r'''
    static bool same_packed_footprint(const Surface& a,const Surface& c){
        if(!a.readback || !c.readback || a.readback->words.empty() || a.readback->words.size()!=c.readback->words.size())return false;
        const auto complete=[](const Surface& s){std::uint64_t bytes=0;
            for(const auto& r:s.readback->runs){if((reinterpret_cast<std::uintptr_t>(r.data)|r.size|r.offset)&3 || r.offset!=bytes)return false;bytes+=r.size;}
            return bytes==s.readback->words.size()*4;};
        if(!complete(a) || !complete(c))return false;
        const auto& x=a.readback->runs;const auto& y=c.readback->runs;std::size_t i=0,j=0;unsigned ax=0,cy=0;
        while(i<x.size() && j<y.size()){
            if(x[i].data+ax!=y[j].data+cy)return false;
            const auto n=std::min(x[i].size-ax,y[j].size-cy);if(!n)return false;
            ax+=n;cy+=n;if(ax==x[i].size){++i;ax=0;}if(cy==y[j].size){++j;cy=0;}}
        return i==x.size() && j==y.size();
    }
    bool rebind_equal_footprint(Surface& surface,VkFormat format,bool discard){
        const char* setting=std::getenv("ANYPS3_RSX_GPU_REBIND");
        if(setting && std::strcmp(setting,"0") && std::strcmp(setting,"1"))throw std::runtime_error("invalid ANYPS3_RSX_GPU_REBIND");
        if(setting && !std::strcmp(setting,"0"))return false;
        if(surface.target.bpp!=4 || (surface.depth?surface.format!=2:surface.format!=8))return false;
        Surface* source=nullptr;
        for(auto& old:surfaces){
            if(old.partial || old.depth!=surface.depth || old.format!=surface.format || old.target.bpp!=4 || old.image->format!=format)continue;
            if(same_packed_footprint(old,surface)){source=&old;break;}}
        if(!source)return false;
        // No overlapping owner may be discarded as a side effect of the
        // transition. Partial overlaps keep the established coherent fallback.
        for(const auto& other:surfaces)if(&other!=source)for(const auto& span:surface.physical)
            if(other.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin))return false;
        const auto bytes=surface.readback->words.size()*4;
        // Both pack and unpack need their own immutable tables and outputs;
        // reserve before recording, never recycle an in-flight input offset.
        const auto required=std::uint64_t(bytes)*8+65536;
        if(required>batch->capacity)return false;
        source->target.mapping->check_epoch();surface.target.mapping->check_epoch();
        if(batch->used+required>batch->capacity)submit_async("surface_rebind_capacity");
        auto image=surface_image(format,surface.width,surface.height,surface.samples,surface.depth);
        if(!discard){
            const auto packed=vk.graphics_pack_readback(*batch,source->image,source->format,source->depth,source->target.bpp,source->target.pitch,source->samples,source->readback->words,source->readback->gpu_words,false);
            vk.graphics_unpack_upload(*batch,image,surface.format,surface.depth,surface.target.bpp,surface.target.pitch,surface.samples,surface.readback->words,surface.readback->gpu_words,nullptr,packed);
        }
        const bool dirty=source->dirty;const auto original=source->image;
        surface.image=std::move(image);surface.dirty=dirty;surface.last_use=++resource_clock;
        surfaces.erase(std::remove_if(surfaces.begin(),surfaces.end(),[&](const auto& s){return s.image==original;}),surfaces.end());
        surfaces.push_back(std::move(surface));refresh_surface_watches();
        if(fallback_stats().enabled)++fallback_stats().paths[discard?"surface_equal_discard_gpu":"surface_equal_rebind_gpu"];
        return true;
    }
'''
