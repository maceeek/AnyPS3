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

    static VkResult vkAllocateMemory(VkDevice d,const VkMemoryAllocateInfo* ci,const VkAllocationCallbacks* a,VkDeviceMemory* out){::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_allocs);const auto r=::vkAllocateMemory(d,ci,a,out);if(r==VK_SUCCESS)resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);return r;}
    static void vkFreeMemory(VkDevice d,VkDeviceMemory object,const VkAllocationCallbacks* a){::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);resource_stats().remove("device_memory",resource_id(object));::vkFreeMemory(d,object,a);}
    static void vkDestroyPipeline(VkDevice d,VkPipeline object,const VkAllocationCallbacks* a){resource_stats().remove("pipelines",resource_id(object));::vkDestroyPipeline(d,object,a);}
    static void release_descriptor_sets(VkDescriptorPool pool){auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);auto it=stats.descriptor_sets.find(resource_id(pool));if(it!=stats.descriptor_sets.end()){for(auto id:it->second)stats.remove("descriptor_sets",id);stats.descriptor_sets.erase(it);}}
    static VkResult vkResetDescriptorPool(VkDevice d,VkDescriptorPool pool,VkDescriptorPoolResetFlags flags){const auto r=::vkResetDescriptorPool(d,pool,flags);if(r==VK_SUCCESS)release_descriptor_sets(pool);return r;}
    static VkResult vkAllocateDescriptorSets(VkDevice d,const VkDescriptorSetAllocateInfo* ci,VkDescriptorSet* out){const auto r=::vkAllocateDescriptorSets(d,ci,out);if(r==VK_SUCCESS){auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);for(unsigned i=0;i<ci->descriptorSetCount;++i){auto id=resource_id(out[i]);stats.add("descriptor_sets",id);stats.descriptor_sets[resource_id(ci->descriptorPool)].push_back(id);}}return r;}
    static VkResult vkCreateGraphicsPipelines(VkDevice d,VkPipelineCache cache,std::uint32_t n,const VkGraphicsPipelineCreateInfo* ci,const VkAllocationCallbacks* a,VkPipeline* out){const auto r=::vkCreateGraphicsPipelines(d,cache,n,ci,a,out);for(unsigned i=0;i<n;++i)if(out[i])resource_stats().add("pipelines",resource_id(out[i]));return r;}
    static VkResult vkCreateComputePipelines(VkDevice d,VkPipelineCache cache,std::uint32_t n,const VkComputePipelineCreateInfo* ci,const VkAllocationCallbacks* a,VkPipeline* out){const auto r=::vkCreateComputePipelines(d,cache,n,ci,a,out);for(unsigned i=0;i<n;++i)if(out[i])resource_stats().add("pipelines",resource_id(out[i]));return r;}
#endif
'''
