"""Explicit-scope surface ownership for the resident compute path.

This is deliberately not installed as a transparent GuestMemory cache: its
caller must issue host_access for EVERY host read/write and drain before the
scope ends. See rsxvk2/memory-audit.md for raw-pointer integration barriers.
"""
CPP = r'''
class SurfaceCache {
public:
    struct Surface {
        Target target;unsigned width,height,bytes,stride,offset,words;
        bool dirty=false;std::shared_ptr<NativeRsx::Tile> tile_snapshot;std::uint64_t last_use=0;
    };
    NativeVulkan& vk;NativeVulkan::ResidentArena& arena;
    std::vector<Surface> surfaces;
    struct TextureBlob {std::vector<unsigned> content;unsigned offset;std::uint64_t last_use=0;};
    std::vector<TextureBlob> textures;
    std::map<unsigned,unsigned> zero_surfaces;
    unsigned staging=0;std::uint64_t clock=0;
    std::map<unsigned,std::uint64_t> zero_uses;
    bool building=false;std::set<unsigned> build_pins;
    struct BuildScope {SurfaceCache& cache;explicit BuildScope(SurfaceCache& c):cache(c){if(c.building)throw std::runtime_error("nested resident draw preparation");c.building=true;}
        ~BuildScope(){cache.building=false;cache.build_pins.clear();}};
    unsigned pin(unsigned at){if(building)build_pins.insert(at);return at;}
    template<class Vector> auto oldest_unpinned(Vector& entries){
        auto result=entries.end();for(auto it=entries.begin();it!=entries.end();++it)if(!build_pins.count(it->offset) && (result==entries.end() || it->last_use<result->last_use))result=it;return result;
    }
    auto oldest_zero(){auto result=zero_uses.end();for(auto it=zero_uses.begin();it!=zero_uses.end();++it)if(!build_pins.count(zero_surfaces.at(it->first)) && (result==zero_uses.end() || it->second<result->second))result=it;return result;}
    std::function<void()> before_readback;
    std::uint64_t surface_uploads=0,surface_readbacks=0,texture_uploads=0,texture_hits=0;
    SurfaceCache(NativeVulkan& v,NativeVulkan::ResidentArena& a):vk(v),arena(a){}
    static bool same(const Target& a,const Target& b){
        if(a.base!=b.base || a.offset!=b.offset || a.pitch!=b.pitch || a.bpp!=b.bpp || a.base_offset!=b.base_offset ||
           a.swizzle_width!=b.swizzle_width || a.swizzle_height!=b.swizzle_height || bool(a.tile)!=bool(b.tile) || bool(a.mapping)!=bool(b.mapping))return false;
        if(a.tile && (a.tile->offset!=b.tile->offset || a.tile->pitch!=b.tile->pitch || a.tile->size!=b.tile->size || a.tile->bank!=b.tile->bank))return false;
        if(a.mapping){const auto& ap=a.mapping->parts;const auto& bp=b.mapping->parts;if(ap.size()!=bp.size())return false;
            for(unsigned i=0;i<ap.size();++i)if(ap[i].data!=bp[i].data || ap[i].offset!=bp[i].offset || ap[i].size!=bp[i].size)return false;}
        return true;
    }
    unsigned allocate(unsigned words){
        auto fits=[&]{if(words<=arena.capacity-arena.used)return true;for(const auto& span:arena.free_spans)if(span.second>=words)return true;return false;};
        while(!fits() && (!surfaces.empty() || !textures.empty() || !zero_surfaces.empty())){
            auto surf=oldest_unpinned(surfaces);
            auto tex=oldest_unpinned(textures);
            auto zero=oldest_zero();
            const auto st=surf==surfaces.end()?UINT64_MAX:surf->last_use,tt=tex==textures.end()?UINT64_MAX:tex->last_use,zt=zero==zero_uses.end()?UINT64_MAX:zero->second;
            if(st==UINT64_MAX && tt==UINT64_MAX && zt==UINT64_MAX)break;
            if(before_readback){before_readback();}finish();
            if(st<=tt && st<=zt){readback(*surf);finish();vk.resident_free(arena,surf->offset);surfaces.erase(surf);}
            else if(tt<=zt){vk.resident_free(arena,tex->offset);textures.erase(tex);}
            else{vk.resident_free(arena,zero_surfaces.at(zero->first));zero_surfaces.erase(zero->first);zero_uses.erase(zero);}
        }
        return vk.resident_allocate(arena,words);
    }
    void upload(unsigned offset,const std::vector<unsigned>& data){
        if(data.size()>arena.capacity-staging){if(before_readback){before_readback();}finish();}
        vk.resident_upload(arena,offset,data,staging);staging+=data.size();
    }
    void finish(){vk.resident_finish(arena);staging=0;}
    void readback(Surface& surface){
        if(!surface.dirty)return;
        if(before_readback)before_readback();
        surface.target.observed=true;
        vk.resident_download(arena,surface.offset,surface.words);finish();
        const auto data=vk.resident_read(arena,surface.offset,surface.words);
        for(unsigned y=0;y<surface.height;++y)for(unsigned x=0;x<surface.width;++x){
            auto* p=surface.target.at(x,y);surface.target.mark(memory(),x,y,p);
            for(unsigned word=0;word<surface.stride;++word)write_be(p+word*4,std::min(4u,surface.bytes),data[surface.stride*(y*surface.width+x)+word]);
            surface.target.commit(x,y);
        }
        surface.dirty=false;++surface_readbacks;
    }
    // Reservation accounting is delegated to the existing Target codec.
    GuestMemory* guest=nullptr;
    GuestMemory& memory(){if(!guest)throw std::runtime_error("RSX surface cache has no guest owner");return *guest;}
    Surface& bind(Target target,unsigned width,unsigned height,unsigned bytes){
        if(!target.base || !width || !height || bytes!=target.bpp || !parallel_target(target,width,height))
            throw std::runtime_error("uncertified RSX resident surface");
        for(auto& surface:surfaces)if(surface.width==width && surface.height==height && surface.bytes==bytes && same(surface.target,target)){surface.last_use=++clock;pin(surface.offset);return surface;}
        // Reinterpreting/partially aliasing a resident surface must preserve its
        // latest bytes before a different layout is initialized from the host.
        for(auto it=surfaces.begin();it!=surfaces.end();){
            bool alias=false;
            if(target.mapping)for(const auto& p:target.mapping->parts)alias|=target_overlaps(it->target,p.data,p.size,it->width,it->height);
            else alias=target_overlaps(it->target,target.base,target_span(target,width,height),it->width,it->height);
            if(alias){if(before_readback)before_readback();readback(*it);finish();vk.resident_free(arena,it->offset);it=surfaces.erase(it);}else ++it;
        }
        const unsigned stride=(bytes+3)/4;
        const auto words64=std::uint64_t(width)*height*stride;
        if(words64>UINT_MAX)throw std::runtime_error("RSX resident surface size overflow");
        while(surfaces.size()>=64){auto old=oldest_unpinned(surfaces);if(old==surfaces.end())break;
            readback(*old);finish();vk.resident_free(arena,old->offset);surfaces.erase(old);}
        Surface surface{target,width,height,bytes,stride,allocate(static_cast<unsigned>(words64)),static_cast<unsigned>(words64),false,{}};
        if(target.tile){surface.tile_snapshot=std::make_shared<NativeRsx::Tile>(*target.tile);surface.target.tile=surface.tile_snapshot.get();}
        std::vector<unsigned> data;data.reserve(surface.words);
        for(unsigned y=0;y<height;++y)for(unsigned x=0;x<width;++x){const auto* p=target.at(x,y);
            for(unsigned word=0;word<stride;++word)data.push_back(read_be(p+word*4,std::min(4u,bytes)));}
        upload(surface.offset,data);++surface_uploads;surface.last_use=++clock;surfaces.push_back(surface);pin(surface.offset);return surfaces.back();
    }
    void host_access(const void* address,std::uint64_t size,bool write){
        for(auto it=surfaces.begin();it!=surfaces.end();){
            if(target_overlaps(it->target,address,size,it->width,it->height)){
                readback(*it);if(write){finish();vk.resident_free(arena,it->offset);it=surfaces.erase(it);continue;}
            }++it;
        }
        // Exact-content keys make stale texture blobs harmless even after a
        // host write; no address/generation guess can reuse different bytes.
    }
    unsigned zero_surface(unsigned words){
        auto found=zero_surfaces.find(words);if(found!=zero_surfaces.end()){zero_uses[words]=++clock;return pin(found->second);}
        while(zero_surfaces.size()>=16){auto old=oldest_zero();if(old==zero_uses.end())break;
            if(before_readback){before_readback();}finish();vk.resident_free(arena,zero_surfaces.at(old->first));zero_surfaces.erase(old->first);zero_uses.erase(old);}
        const auto at=allocate(words);upload(at,std::vector<unsigned>(words,0));zero_surfaces[words]=at;zero_uses[words]=++clock;return pin(at);
    }
    void trim_textures(unsigned words){
        auto used=[&]{std::uint64_t n=0;for(const auto& b:textures)n+=b.content.size();return n;};
        while(!textures.empty() && (textures.size()>=64 || used()+words>arena.capacity/2)){
            auto old=oldest_unpinned(textures);if(old==textures.end())break;
            if(before_readback){before_readback();}finish();vk.resident_free(arena,old->offset);textures.erase(old);}
    }
    unsigned texture_levels(const std::vector<unsigned>& data,unsigned levels){
        // Include level count in the key; descriptor offsets in the canonical
        // key are relative, uploaded descriptors point to fixed arena offsets.
        std::vector<unsigned> key=data;key.push_back(levels);key.push_back(0x52535854);
        for(auto& blob:textures)if(blob.content==key){++texture_hits;blob.last_use=++clock;return pin(blob.offset);}
        trim_textures(data.size());
        const unsigned at=allocate(data.size());auto relocated=data;
        for(unsigned l=0;l<levels;++l)relocated[3*l]+=at;
        upload(at,relocated);textures.push_back({key,at,++clock});++texture_uploads;return pin(at);
    }
    unsigned texture(const std::vector<unsigned>& data){
        for(auto& blob:textures)if(blob.content==data){++texture_hits;blob.last_use=++clock;return pin(blob.offset);}
        trim_textures(data.size());
        const unsigned at=allocate(data.size());upload(at,data);textures.push_back({data,at,++clock});++texture_uploads;return pin(at);
    }
    void flush(){for(auto& surface:surfaces)readback(surface);finish();}
};
'''
