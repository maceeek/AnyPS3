"""RSX Vulkan compiler counters and optional, exact-key persistent caches."""

from .rsx_vk_resources import MEMBERS as RESOURCE_MEMBERS

MEMBERS = RESOURCE_MEMBERS + r'''
#ifdef ANYPS3_RSX_VK
public:
    struct CompileStats {
        std::uint64_t shader_modules=0,shader_module_hits=0,graphics_pipelines=0,compute_pipelines=0,spirv_compiles=0;
        std::uint64_t spirv_disk_hits=0,spirv_disk_misses=0,cache_rejected=0,cache_io_errors=0;
        std::uint64_t graphics_hits=0,graphics_misses=0,compute_hits=0,compute_misses=0;
        std::uint64_t miss_cold=0,miss_shader=0,miss_fixed_state=0;
        std::uint64_t miss_vs=0,miss_fs=0,miss_targets=0,miss_blend=0,miss_depth_stencil=0,miss_raster=0;
        std::uint64_t pipeline_evictions=0,module_evictions=0,compute_evictions=0,pipeline_cache_rotations=0;
        std::uint64_t module_ns=0,pipeline_ns=0,spirv_ns=0,pipeline_cache_load_bytes=0,pipeline_cache_saves=0,cache_driver_size_fixups=0;
    } compile_stats;
private:
    VkPipelineCache pipeline_cache=VK_NULL_HANDLE;
    bool pipeline_cache_dirty=false,cache_io_warned=false;
    std::filesystem::path vk_cache_root,pipeline_cache_path;
    std::chrono::steady_clock::time_point cache_saved_at{},compile_printed_at{};
    std::string last_pipeline_identity;
    std::array<std::string,6> last_pipeline_parts{};
    std::uint64_t cache_clock=0;
    struct ShaderIdentity {std::shared_ptr<const std::string> owner;std::uint64_t id,last_use;};
    std::map<const std::string*,ShaderIdentity> shader_owner_ids;
    static unsigned cache_limit(const char* name,unsigned fallback){
        const auto* text=std::getenv(name);if(!text || !*text)return fallback;
        char* end=nullptr;const auto n=std::strtoul(text,&end,10);
        if(*text=='-' || *end || !n || n>4096)throw std::runtime_error(std::string("invalid ")+name);
        return unsigned(n);
    }
    // The measured level working set has 190 pipelines / 206 shader modules.
    // Retain it without per-frame LRU recompilation; explicit budgets and
    // caller overrides still bound every cache. Source owners cover VS + FS.
    unsigned graphics_pipeline_limit=cache_limit("ANYPS3_RSX_PIPELINE_LIMIT",256);
    unsigned compute_pipeline_limit=cache_limit("ANYPS3_RSX_COMPUTE_LIMIT",64);
    unsigned shader_module_limit=cache_limit("ANYPS3_RSX_MODULE_LIMIT",256);
    unsigned framebuffer_limit=cache_limit("ANYPS3_RSX_FRAMEBUFFER_LIMIT",64);
    unsigned identity_limit=cache_limit("ANYPS3_RSX_IDENTITY_LIMIT",512);
    template<class Map> static auto lru_entry(Map& entries){
        return std::min_element(entries.begin(),entries.end(),[](const auto& a,const auto& b){return a.second->last_use<b.second->last_use;});
    }
    std::uint64_t shader_identity(const std::string& source,const std::shared_ptr<const std::string>& owner){
        if(owner){if(owner.get()!=&source)throw std::runtime_error("RSX shader owner mismatch");
            auto hit=shader_owner_ids.find(owner.get());if(hit!=shader_owner_ids.end()){hit->second.last_use=++cache_clock;return hit->second.id;}}
        const auto id=cache_hash(source.data(),source.size());
        if(owner){if(shader_owner_ids.size()>=identity_limit){auto old=std::min_element(shader_owner_ids.begin(),shader_owner_ids.end(),[](const auto& a,const auto& b){return a.second.last_use<b.second.last_use;});shader_owner_ids.erase(old);}
            shader_owner_ids.emplace(owner.get(),ShaderIdentity{owner,id,++cache_clock});}
        return id;
    }
    static std::uint64_t elapsed_ns(std::chrono::steady_clock::time_point start){
        return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
    }
    static std::uint64_t cache_hash(const void* bytes,std::size_t size){
        auto* p=static_cast<const unsigned char*>(bytes);std::uint64_t h=UINT64_C(14695981039346656037);
        for(std::size_t i=0;i<size;++i){h^=p[i];h*=UINT64_C(1099511628211);}return h;
    }
    static std::string cache_hex(std::uint64_t value){char b[17];std::snprintf(b,sizeof(b),"%016llx",static_cast<unsigned long long>(value));return b;}
    void cache_io_error()noexcept{
        ++compile_stats.cache_io_errors;
        if(!cache_io_warned){std::fprintf(stderr,"RSX VK cache I/O unavailable; using memory cache\n");cache_io_warned=true;}
    }
    // A checksum detects accidental damage; exact source comparison resolves
    // filename-hash collisions. These files are local compiler artifacts.
    std::vector<char> cache_read(const std::filesystem::path& path){
        std::ifstream in(path,std::ios::binary|std::ios::ate);if(!in)return {};
        const auto n=in.tellg();if(n<16 || n>64*1024*1024){++compile_stats.cache_rejected;return {};}
        std::vector<char> blob(static_cast<std::size_t>(n));in.seekg(0);
        if(!in.read(blob.data(),blob.size())){cache_io_error();return {};}
        std::uint64_t sum=0;std::memcpy(&sum,blob.data()+8,8);
        if(std::memcmp(blob.data(),"ANYVK008",8) || sum!=cache_hash(blob.data()+16,blob.size()-16)){++compile_stats.cache_rejected;return {};}
        std::error_code ec;std::filesystem::last_write_time(path,std::filesystem::file_time_type::clock::now(),ec);
        return {blob.begin()+16,blob.end()};
    }
    void trim_disk_cache(std::uint64_t incoming){
        struct Entry{std::filesystem::path path;std::filesystem::file_time_type time;std::uint64_t size;};std::vector<Entry> entries;std::uint64_t bytes=0;
        std::error_code ec;
        for(std::filesystem::directory_iterator it(vk_cache_root,ec),end;!ec && it!=end;it.increment(ec)){
            const auto name=it->path().filename().string();
            if(name.size()<4 || name.substr(name.size()-4)!=".bin" || (name.rfind("spv-",0)!=0 && name.rfind("pipeline-",0)!=0))continue;
            const auto size=it->file_size(ec);if(ec)break;const auto time=it->last_write_time(ec);if(ec)break;
            entries.push_back({it->path(),time,size});bytes+=size;
        }
        std::sort(entries.begin(),entries.end(),[](const auto& a,const auto& b){return a.time<b.time;});
        std::size_t left=entries.size();for(const auto& entry:entries){if(left<1024 && bytes+incoming<=256*1024*1024)break;
            if(std::filesystem::remove(entry.path,ec)){bytes-=entry.size;--left;}if(ec){cache_io_error();break;}}
    }
    void cache_write(const std::filesystem::path& path,const std::vector<char>& payload)noexcept{
        try{
            if(payload.size()>32*1024*1024)return;
            trim_disk_cache(payload.size()+16);
            std::vector<char> blob(16);std::memcpy(blob.data(),"ANYVK008",8);
            const auto sum=cache_hash(payload.data(),payload.size());std::memcpy(blob.data()+8,&sum,8);blob.insert(blob.end(),payload.begin(),payload.end());
            std::string name=path.string()+".tmp-XXXXXX";std::vector<char> temp(name.begin(),name.end());temp.push_back(0);
            const int fd=mkstemp(temp.data());if(fd<0){cache_io_error();return;}
            std::size_t done=0;while(done<blob.size()){const auto n=::write(fd,blob.data()+done,blob.size()-done);if(n<=0)break;done+=n;}
            const bool closed=::close(fd)==0;
            if(done!=blob.size() || !closed || ::rename(temp.data(),path.c_str())!=0){::unlink(temp.data());cache_io_error();}
        }catch(...){cache_io_error();}
    }
    static std::string spirv_key(const std::string& source,glslang_stage_t stage,bool optimize){
        // Include compiler version/flavor, target environment, every option and
        // source bytes. Increment schema when compiler configuration changes.
        return "rsxvk9-spv2-auto-vulkan1.0-or1.1-spv1.0-or1.3-validate1-glsl450:"+std::to_string(GLSLANG_VERSION_MAJOR)+"."+
            std::to_string(GLSLANG_VERSION_MINOR)+"."+std::to_string(GLSLANG_VERSION_PATCH)+GLSLANG_VERSION_FLAVOR+":"+
            std::to_string(stage)+":"+std::to_string(optimize)+":"+source;
    }
    std::vector<unsigned> load_spirv(const std::string& key){
        if(vk_cache_root.empty())return {};
        auto blob=cache_read(vk_cache_root/("spv-"+cache_hex(cache_hash(key.data(),key.size()))+".bin"));if(blob.empty())return {};
        const auto at=sizeof(std::uint64_t)+key.size();std::uint64_t key_size=0;
        if(blob.size()>=8)std::memcpy(&key_size,blob.data(),8);
        if(key_size!=key.size() || blob.size()<at+20 || (blob.size()-at)%4 || std::memcmp(blob.data()+8,key.data(),key.size())){++compile_stats.cache_rejected;return {};}
        std::vector<unsigned> code((blob.size()-at)/4);std::memcpy(code.data(),blob.data()+at,code.size()*4);
        if(code[0]!=0x07230203u || code[1]!=0x00010000u || !code[3] || code[4]){++compile_stats.cache_rejected;return {};}
        return code;
    }
    void save_spirv(const std::string& key,const std::vector<unsigned>& code){
        if(vk_cache_root.empty())return;
        if(key.size()>16*1024*1024 || code.size()>4*1024*1024)return;
        const std::uint64_t n=key.size();std::vector<char> blob(8+n+code.size()*4);
        std::memcpy(blob.data(),&n,8);std::memcpy(blob.data()+8,key.data(),n);std::memcpy(blob.data()+8+n,code.data(),code.size()*4);
        cache_write(vk_cache_root/("spv-"+cache_hex(cache_hash(key.data(),key.size()))+".bin"),blob);
    }
    void init_pipeline_cache(){
        if(const auto* root=std::getenv("ANYPS3_CACHE_ROOT");root && *root){
            std::error_code ec;vk_cache_root=std::filesystem::path(root)/"vk";std::filesystem::create_directories(vk_cache_root,ec);
            if(ec){cache_io_error();vk_cache_root.clear();}
        }
        VkPhysicalDeviceProperties p{};vkGetPhysicalDeviceProperties(physical,&p);
        std::string id=std::to_string(p.vendorID)+"-"+std::to_string(p.deviceID)+"-"+std::to_string(p.driverVersion)+"-";
        for(unsigned i=0;i<VK_UUID_SIZE;++i){char hex[3];std::snprintf(hex,sizeof(hex),"%02x",p.pipelineCacheUUID[i]);id+=hex;}
        std::vector<char> data;
        if(!vk_cache_root.empty()){
            pipeline_cache_path=vk_cache_root/("pipeline-"+id+".bin");data=cache_read(pipeline_cache_path);
            if(!data.empty()){
                VkPipelineCacheHeaderVersionOne h{};if(data.size()>=sizeof(h))std::memcpy(&h,data.data(),sizeof(h));
                if(data.size()<sizeof(h) || h.headerSize<sizeof(h) || h.headerSize>data.size() || h.headerVersion!=VK_PIPELINE_CACHE_HEADER_VERSION_ONE ||
                   h.vendorID!=p.vendorID || h.deviceID!=p.deviceID || std::memcmp(h.pipelineCacheUUID,p.pipelineCacheUUID,VK_UUID_SIZE)){
                    ++compile_stats.cache_rejected;data.clear();}
            }
        }
        VkPipelineCacheCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;ci.initialDataSize=data.size();ci.pInitialData=data.empty()?nullptr:data.data();
        const auto result=vkCreatePipelineCache(device,&ci,nullptr,&pipeline_cache);
        if(result!=VK_SUCCESS && !data.empty()){++compile_stats.cache_rejected;data.clear();ci.initialDataSize=0;ci.pInitialData=nullptr;
            check(vkCreatePipelineCache(device,&ci,nullptr,&pipeline_cache),"vkCreatePipelineCache empty");}
        else check(result,"vkCreatePipelineCache");
        compile_stats.pipeline_cache_load_bytes=data.size();cache_saved_at=std::chrono::steady_clock::now();
    }
    unsigned pipeline_cache_creations=0;
    void pipeline_cache_created(){
        pipeline_cache_dirty=true;
        ++pipeline_cache_creations;std::size_t bytes=0;
        if(pipeline_cache_creations%16==0)check(vkGetPipelineCacheData(device,pipeline_cache,&bytes,nullptr),"pipeline cache size");
        if(pipeline_cache_creations>=128 || bytes>32*1024*1024){
            save_pipeline_cache(true);vkDestroyPipelineCache(device,pipeline_cache,nullptr);pipeline_cache=VK_NULL_HANDLE;
            VkPipelineCacheCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;
            check(vkCreatePipelineCache(device,&ci,nullptr,&pipeline_cache),"rotate VkPipelineCache");
            pipeline_cache_creations=0;++compile_stats.pipeline_cache_rotations;
        }
    }
    void save_pipeline_cache(bool force=false)noexcept{
        if(!pipeline_cache || !pipeline_cache_dirty || pipeline_cache_path.empty())return;
        if(!force && elapsed_ns(cache_saved_at)<UINT64_C(2000000000))return;
        try{
            std::size_t n=0;auto result=vkGetPipelineCacheData(device,pipeline_cache,&n,nullptr);
            if(result!=VK_SUCCESS || n>64*1024*1024){cache_io_error();return;}
            std::vector<char> data(n);result=vkGetPipelineCacheData(device,pipeline_cache,&n,data.data());
            if(result!=VK_SUCCESS){cache_io_error();return;}
            // Mesa 26.2 Lavapipe reports 20 after writing its complete 32-byte
            // empty header (the size-only query returns 32). Accept only that
            // verified header, never guess the length of opaque cache payload.
            if(n==20 && data.size()==sizeof(VkPipelineCacheHeaderVersionOne)){
                VkPhysicalDeviceProperties p{};vkGetPhysicalDeviceProperties(physical,&p);VkPipelineCacheHeaderVersionOne h{};std::memcpy(&h,data.data(),sizeof(h));
                if(p.vendorID==0x10005u && p.deviceType==VK_PHYSICAL_DEVICE_TYPE_CPU && h.headerSize==data.size() && h.headerVersion==VK_PIPELINE_CACHE_HEADER_VERSION_ONE &&
                   h.vendorID==p.vendorID && h.deviceID==p.deviceID && !std::memcmp(h.pipelineCacheUUID,p.pipelineCacheUUID,VK_UUID_SIZE)){n=data.size();++compile_stats.cache_driver_size_fixups;}
            }
            data.resize(n);cache_write(pipeline_cache_path,data);
            ++compile_stats.pipeline_cache_saves;pipeline_cache_dirty=false;cache_saved_at=std::chrono::steady_clock::now();
        }catch(...){cache_io_error();}
    }
public:
    void compile_stats_print(bool force=false){
        // Called on every pipeline cache hit (every draw): read the switches once.
        static const bool enabled=[]{const auto* a=std::getenv("ANYPS3_RSX_FALLBACK_STATS"),*b=std::getenv("ANYPS3_RSX_HOST_PROFILE");
            return (a && !std::strcmp(a,"1")) || (b && !std::strcmp(b,"1"));}();
        if(!enabled)return;
        if(!force && elapsed_ns(compile_printed_at)<UINT64_C(2000000000))return;
        compile_printed_at=std::chrono::steady_clock::now();save_pipeline_cache();const auto& c=compile_stats;
        print_resource_stats();
        std::fprintf(stderr,"RSX VK COMPILE STATS phase=%s modules=%llu module_hits=%llu graphics_pipelines=%llu compute_pipelines=%llu spirv_compiles=%llu spirv_disk_hits=%llu spirv_disk_misses=%llu graphics_hits=%llu graphics_misses=%llu compute_hits=%llu compute_misses=%llu miss_cold=%llu miss_shader=%llu miss_fixed_state=%llu module_ms=%.3f pipeline_ms=%.3f glslang_ms=%.3f pipeline_cache_load_bytes=%llu pipeline_cache_saves=%llu cache_driver_size_fixups=%llu cache_rejected=%llu cache_io_errors=%llu miss_vs=%llu miss_fs=%llu miss_targets=%llu miss_blend=%llu miss_depth_stencil=%llu miss_raster=%llu pipeline_evictions=%llu module_evictions=%llu compute_evictions=%llu pipeline_cache_rotations=%llu live_pipeline_cache=%zu live_compute_cache=%zu live_module_cache=%zu live_identities=%zu spirv_cache_items=0 spirv_cache_bytes=0\n",
            force?"final":"periodic",(unsigned long long)c.shader_modules,(unsigned long long)c.shader_module_hits,(unsigned long long)c.graphics_pipelines,(unsigned long long)c.compute_pipelines,(unsigned long long)c.spirv_compiles,
            (unsigned long long)c.spirv_disk_hits,(unsigned long long)c.spirv_disk_misses,(unsigned long long)c.graphics_hits,(unsigned long long)c.graphics_misses,(unsigned long long)c.compute_hits,(unsigned long long)c.compute_misses,
            (unsigned long long)c.miss_cold,(unsigned long long)c.miss_shader,(unsigned long long)c.miss_fixed_state,c.module_ns/1.e6,c.pipeline_ns/1.e6,c.spirv_ns/1.e6,
            (unsigned long long)c.pipeline_cache_load_bytes,(unsigned long long)c.pipeline_cache_saves,(unsigned long long)c.cache_driver_size_fixups,(unsigned long long)c.cache_rejected,(unsigned long long)c.cache_io_errors,(unsigned long long)c.miss_vs,(unsigned long long)c.miss_fs,(unsigned long long)c.miss_targets,(unsigned long long)c.miss_blend,(unsigned long long)c.miss_depth_stencil,(unsigned long long)c.miss_raster,(unsigned long long)c.pipeline_evictions,(unsigned long long)c.module_evictions,(unsigned long long)c.compute_evictions,(unsigned long long)c.pipeline_cache_rotations,graphics_pipelines.size(),compute_pipelines.size(),graphics_shaders.size(),shader_owner_ids.size());
    }
private:
#endif
'''
