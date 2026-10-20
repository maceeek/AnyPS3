"""Live Vulkan object accounting, including allocation sizes and process RSS."""
MEMBERS = r'''  
#ifdef ANYPS3_RSX_VK
public:
    struct ResourceStats {
        struct Kind {std::map<std::uint64_t,std::uint64_t> objects;std::uint64_t bytes=0,peak_count=0,peak_bytes=0;};
        std::map<std::string,Kind> kinds;
        std::map<std::uint64_t,std::vector<std::uint64_t>> descriptor_sets;
        std::recursive_mutex mutex;
        void add(const char* kind,std::uint64_t key,std::uint64_t bytes=0){std::lock_guard<std::recursive_mutex> lock(mutex);auto& k=kinds[kind];k.objects[key]=bytes;k.bytes+=bytes;k.peak_count=std::max<std::uint64_t>(k.peak_count,k.objects.size());k.peak_bytes=std::max(k.peak_bytes,k.bytes);}
        void remove(const char* kind,std::uint64_t key){std::lock_guard<std::recursive_mutex> lock(mutex);auto& k=kinds[kind];auto it=k.objects.find(key);if(it!=k.objects.end()){k.bytes-=it->second;k.objects.erase(it);}}
    };
    static ResourceStats& resource_stats(){static ResourceStats stats;return stats;}
    template<class T> static std::uint64_t resource_id(T handle){return static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(handle));}
    static void print_resource_stats(){
        auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);
        std::uint64_t virtual_pages=0,resident_pages=0;std::ifstream statm("/proc/self/statm");statm>>virtual_pages>>resident_pages;
        std::fprintf(stderr,"RSX VK LIVE rss_bytes=%llu",(unsigned long long)(resident_pages*std::uint64_t(sysconf(_SC_PAGESIZE))));
        for(const auto& p:stats.kinds)std::fprintf(stderr," %s=%zu %s_bytes=%llu %s_peak=%llu %s_peak_bytes=%llu",p.first.c_str(),p.second.objects.size(),p.first.c_str(),(unsigned long long)p.second.bytes,p.first.c_str(),(unsigned long long)p.second.peak_count,p.first.c_str(),(unsigned long long)p.second.peak_bytes);
        std::fputc('\n',stderr);
    }
private:
    static VkResult vkCreateBuffer(VkDevice d,const VkBufferCreateInfo* ci,const VkAllocationCallbacks* a,VkBuffer* out){
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_buffers);const auto r=::vkCreateBuffer(d,ci,a,out);if(r==VK_SUCCESS){VkMemoryRequirements req{};::vkGetBufferMemoryRequirements(d,*out,&req);resource_stats().add("buffers",resource_id(*out),req.size);}return r;
    }
    static void vkDestroyBuffer(VkDevice d,VkBuffer object,const VkAllocationCallbacks* a){
        resource_stats().remove("buffers",resource_id(object));::vkDestroyBuffer(d,object,a);
    }
    static VkResult vkCreateImage(VkDevice d,const VkImageCreateInfo* ci,const VkAllocationCallbacks* a,VkImage* out){
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_images);const auto r=::vkCreateImage(d,ci,a,out);if(r==VK_SUCCESS){VkMemoryRequirements req{};::vkGetImageMemoryRequirements(d,*out,&req);resource_stats().add("images",resource_id(*out),req.size);}return r;
    }
    static void vkDestroyImage(VkDevice d,VkImage object,const VkAllocationCallbacks* a){
        resource_stats().remove("images",resource_id(object));::vkDestroyImage(d,object,a);
    }
    static VkResult vkCreateShaderModule(VkDevice d,const VkShaderModuleCreateInfo* ci,const VkAllocationCallbacks* a,VkShaderModule* out){
        const auto r=::vkCreateShaderModule(d,ci,a,out);if(r==VK_SUCCESS){resource_stats().add("shader_modules",resource_id(*out),ci->codeSize);}return r;
    }
    static void vkDestroyShaderModule(VkDevice d,VkShaderModule object,const VkAllocationCallbacks* a){
        resource_stats().remove("shader_modules",resource_id(object));::vkDestroyShaderModule(d,object,a);
    }
    static VkResult vkCreateFramebuffer(VkDevice d,const VkFramebufferCreateInfo* ci,const VkAllocationCallbacks* a,VkFramebuffer* out){
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_framebuffers);const auto r=::vkCreateFramebuffer(d,ci,a,out);if(r==VK_SUCCESS){resource_stats().add("framebuffers",resource_id(*out),0);}return r;
    }
    static void vkDestroyFramebuffer(VkDevice d,VkFramebuffer object,const VkAllocationCallbacks* a){
        resource_stats().remove("framebuffers",resource_id(object));::vkDestroyFramebuffer(d,object,a);
    }
    static VkResult vkCreateDescriptorPool(VkDevice d,const VkDescriptorPoolCreateInfo* ci,const VkAllocationCallbacks* a,VkDescriptorPool* out){
        const auto r=::vkCreateDescriptorPool(d,ci,a,out);if(r==VK_SUCCESS){resource_stats().add("descriptor_pools",resource_id(*out),0);}return r;
    }
    static void vkDestroyDescriptorPool(VkDevice d,VkDescriptorPool object,const VkAllocationCallbacks* a){
        resource_stats().remove("descriptor_pools",resource_id(object));release_descriptor_sets(object);::vkDestroyDescriptorPool(d,object,a);
    }

    // Device-local (never host-visible, hence never mapped) allocations are recycled by exact
    // (memory type, size): vkFreeMemory/vkAllocateMemory cost dozens of RM ioctls each on NVIDIA.
    // The Vulkan lifetime rules are unchanged: a block enters the cache only where the old code
    // freed it, i.e. after the GPU finished with every resource bound to it. ANYPS3_RSX_MEMORY_CACHE_MB
    // (default 384, 0 = off) bounds the cached bytes; the oldest blocks are really freed first.
    struct MemoryCache {
        struct Free {VkDevice device;std::uint32_t type;VkDeviceSize size;VkDeviceMemory memory;std::uint64_t stamp;};
        std::mutex mutex;std::vector<Free> free;std::map<std::pair<std::uint64_t,std::uint64_t>,std::pair<std::uint32_t,VkDeviceSize>> live;   // key (VkDevice, VkDeviceMemory): handles are only unique per device
        std::map<VkDevice,std::uint32_t> types;VkDeviceSize bytes=0,limit=UINT64_C(384)<<20;std::uint64_t clock=0;
    };
    static MemoryCache& memory_cache(){static MemoryCache cache;return cache;}
    void configure_memory_cache(VkDevice d){
        std::uint32_t mask=0;
        for(std::uint32_t i=0;i<memory.memoryTypeCount;++i){const auto f=memory.memoryTypes[i].propertyFlags;
            if((f&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) && !(f&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT))mask|=1u<<i;}
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);
        c.limit=UINT64_C(384)<<20;   // default on every configuration: the singleton must not keep a previous device's setting
        if(const char* v=std::getenv("ANYPS3_RSX_MEMORY_CACHE_MB")){char* end=nullptr;const auto n=std::strtoull(v,&end,10);
            if(!*v || *end || n>65536){throw std::runtime_error("invalid ANYPS3_RSX_MEMORY_CACHE_MB");}
            c.limit=n<<20;}
        c.types[d]=c.limit?mask:0;
    }
    static void release_memory_cache(VkDevice d)noexcept{
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);
        for(auto it=c.free.begin();it!=c.free.end();){if(it->device==d){c.bytes-=it->size;::vkFreeMemory(d,it->memory,nullptr);it=c.free.erase(it);}else ++it;}
        c.types.erase(d);
        for(auto it=c.live.begin();it!=c.live.end();)if(it->first.first==resource_id(d))it=c.live.erase(it);else ++it;
    }
    static VkResult vkAllocateMemory(VkDevice d,const VkMemoryAllocateInfo* ci,const VkAllocationCallbacks* a,VkDeviceMemory* out){
        auto& c=memory_cache();bool cacheable=false;
        {std::lock_guard<std::mutex> lock(c.mutex);
            const auto t=c.types.find(d);cacheable=!ci->pNext && !a && t!=c.types.end() && ci->memoryTypeIndex<32 && (t->second&(1u<<ci->memoryTypeIndex));
            if(cacheable){
                for(auto it=c.free.begin();it!=c.free.end();++it)if(it->device==d && it->type==ci->memoryTypeIndex && it->size==ci->allocationSize){
                    *out=it->memory;c.bytes-=it->size;c.free.erase(it);c.live[{resource_id(d),resource_id(*out)}]={ci->memoryTypeIndex,ci->allocationSize};
                    ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_reuses);resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);return VK_SUCCESS;}}}
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_allocs);auto r=::vkAllocateMemory(d,ci,a,out);
        // Any allocation that fails for device memory (a host-visible staging block can share a heap with the cached
        // device-local blocks) gets the cached blocks back before the failure is reported.
        if(r==VK_ERROR_OUT_OF_DEVICE_MEMORY){release_memory_cache_blocks(d);r=::vkAllocateMemory(d,ci,a,out);}
        if(r==VK_SUCCESS){resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);
            if(cacheable){std::lock_guard<std::mutex> lock(c.mutex);c.live[{resource_id(d),resource_id(*out)}]={ci->memoryTypeIndex,ci->allocationSize};}}
        return r;
    }
    static void release_memory_cache_blocks(VkDevice d){
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);
        for(auto it=c.free.begin();it!=c.free.end();){if(it->device==d){c.bytes-=it->size;::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(d,it->memory,nullptr);it=c.free.erase(it);}else ++it;}
    }
    static void vkFreeMemory(VkDevice d,VkDeviceMemory object,const VkAllocationCallbacks* a){
        resource_stats().remove("device_memory",resource_id(object));
        auto& c=memory_cache();
        {std::lock_guard<std::mutex> lock(c.mutex);
            const auto live=c.live.find({resource_id(d),resource_id(object)});
            if(live!=c.live.end() && !a){
                const auto entry=live->second;c.live.erase(live);
                if(entry.second<=c.limit){
                    c.free.push_back({d,entry.first,entry.second,object,++c.clock});c.bytes+=entry.second;
                    while(c.bytes>c.limit){auto oldest=std::min_element(c.free.begin(),c.free.end(),[](const auto& x,const auto& y){return x.stamp<y.stamp;});
                        c.bytes-=oldest->size;::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(oldest->device,oldest->memory,nullptr);c.free.erase(oldest);}
                    return;}}}
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(d,object,a);
    }
    static void vkDestroyPipeline(VkDevice d,VkPipeline object,const VkAllocationCallbacks* a){resource_stats().remove("pipelines",resource_id(object));::vkDestroyPipeline(d,object,a);}
    static void release_descriptor_sets(VkDescriptorPool pool){auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);auto it=stats.descriptor_sets.find(resource_id(pool));if(it!=stats.descriptor_sets.end()){for(auto id:it->second)stats.remove("descriptor_sets",id);stats.descriptor_sets.erase(it);}}
    static VkResult vkResetDescriptorPool(VkDevice d,VkDescriptorPool pool,VkDescriptorPoolResetFlags flags){const auto r=::vkResetDescriptorPool(d,pool,flags);if(r==VK_SUCCESS)release_descriptor_sets(pool);return r;}
    static VkResult vkAllocateDescriptorSets(VkDevice d,const VkDescriptorSetAllocateInfo* ci,VkDescriptorSet* out){const auto r=::vkAllocateDescriptorSets(d,ci,out);if(r==VK_SUCCESS){auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);for(unsigned i=0;i<ci->descriptorSetCount;++i){auto id=resource_id(out[i]);stats.add("descriptor_sets",id);stats.descriptor_sets[resource_id(ci->descriptorPool)].push_back(id);}}return r;}
    static VkResult vkCreateGraphicsPipelines(VkDevice d,VkPipelineCache cache,std::uint32_t n,const VkGraphicsPipelineCreateInfo* ci,const VkAllocationCallbacks* a,VkPipeline* out){const auto r=::vkCreateGraphicsPipelines(d,cache,n,ci,a,out);for(unsigned i=0;i<n;++i)if(out[i])resource_stats().add("pipelines",resource_id(out[i]));return r;}
    static VkResult vkCreateComputePipelines(VkDevice d,VkPipelineCache cache,std::uint32_t n,const VkComputePipelineCreateInfo* ci,const VkAllocationCallbacks* a,VkPipeline* out){const auto r=::vkCreateComputePipelines(d,cache,n,ci,a,out);for(unsigned i=0;i<n;++i)if(out[i])resource_stats().add("pipelines",resource_id(out[i]));return r;}
#endif
'''
