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
    // DIAGNOSTIC driver-call attribution, only with ANYPS3_FRAMETIME and ANYPS3_FRAMETIME_MODE=full (else one predictable
    // branch per wrapped call). Every call of a wrapped entry point adds to per-code totals (RSX VK CALLS, printed with the
    // periodic compile statistics); a call lasting >= ANYPS3_VK_CALL_SPAN_US (default 50) is also a frame-ring Mark event
    // with ns = start, a = 0x564b0000|code, b = duration ns, c = size/count argument (the event's tid names the thread).
    enum VkCallCode:unsigned {VcAllocMiss,VcAllocReuse,VcFree,VcFreeCached,VcCreateImage,VcDestroyImage,VcCreateView,VcDestroyView,
        VcCreateBuffer,VcDestroyBuffer,VcCreateFramebuffer,VcDestroyFramebuffer,VcCreateDescriptorPool,VcAllocateDescriptorSets,
        VcUpdateDescriptorSets,VcResetDescriptorPool,VcQueueSubmit,VcWaitSemaphores,VcWaitForFences,VcSemaphoreCounter,VcFenceStatus,
        VcGraphicsPipeline,VcComputePipeline,VcShaderModule,VcResetCommandBuffer,VcBeginCommandBuffer,VcEndCommandBuffer,VcCreateSampler,
        VcQueryResults,VcResetFences,VcBeginRenderPass,VcPipelineBarrier,VcAcquire,VcPresent,VcHostPoll,VcGlslang,VcDescriptorPoolDestroy,
        VcPipelineDestroy,VcCount};
    static constexpr const char* vk_call_names[VcCount]={"alloc_miss","alloc_reuse","free","free_cached","create_image","destroy_image",
        "create_view","destroy_view","create_buffer","destroy_buffer","create_framebuffer","destroy_framebuffer","create_descriptor_pool",
        "allocate_descriptor_sets","update_descriptor_sets","reset_descriptor_pool","queue_submit","wait_semaphores","wait_fences",
        "semaphore_counter","fence_status","graphics_pipeline","compute_pipeline","shader_module","reset_command_buffer","begin_command_buffer",
        "end_command_buffer","create_sampler","query_results","reset_fences","begin_render_pass","pipeline_barrier","acquire_image",
        "queue_present","host_poll","glslang","destroy_descriptor_pool","destroy_pipeline"};
    struct VkCallStats {std::array<std::atomic<std::uint64_t>,VcCount> count{},ns{},max{};};
    static VkCallStats& vk_call_stats(){static VkCallStats stats;return stats;}
    static bool vk_call_diag(){return anyps3_ft::enabled(anyps3_ft::Mark);}
    static std::uint64_t vk_call_span_ns(){
        static const std::uint64_t value=[]()->std::uint64_t{const char* v=std::getenv("ANYPS3_VK_CALL_SPAN_US");if(!v)return 50000;
            char* end=nullptr;const auto n=std::strtoull(v,&end,10);
            if(!*v || *end || n>UINT64_C(1000000000))throw std::runtime_error("invalid ANYPS3_VK_CALL_SPAN_US");
            return n*1000;}();
        return value;}
    static void vk_call_record(unsigned code,std::uint64_t t0,std::uint64_t ns,std::uint64_t extra){
        auto& s=vk_call_stats();s.count[code].fetch_add(1,std::memory_order_relaxed);s.ns[code].fetch_add(ns,std::memory_order_relaxed);
        auto seen=s.max[code].load(std::memory_order_relaxed);while(ns>seen && !s.max[code].compare_exchange_weak(seen,ns,std::memory_order_relaxed)){}
        if(ns>=vk_call_span_ns())anyps3_ft::put(t0,anyps3_ft::Mark,0x564b0000u|code,ns,extra,0);
    }
    struct VkCallTimer {
        unsigned code;std::uint64_t extra,t0;
        explicit VkCallTimer(unsigned c,std::uint64_t x=0):code(c),extra(x),t0(__builtin_expect(vk_call_diag(),0)?anyps3_ft::now():0){}
        ~VkCallTimer(){if(t0)vk_call_record(code,t0,anyps3_ft::now()-t0,extra);}
        VkCallTimer(const VkCallTimer&)=delete;VkCallTimer& operator=(const VkCallTimer&)=delete;
    };
    static void vk_call_print(){
        if(!vk_call_diag())return;
        auto& s=vk_call_stats();std::string line="RSX VK CALLS";char item[96];
        for(unsigned i=0;i<VcCount;++i){const auto n=s.count[i].load(std::memory_order_relaxed);if(!n)continue;
            std::snprintf(item,sizeof(item)," %s=%llu/%llu/%llu",vk_call_names[i],(unsigned long long)n,(unsigned long long)s.ns[i].load(std::memory_order_relaxed),(unsigned long long)s.max[i].load(std::memory_order_relaxed));line+=item;}
        std::fprintf(stderr,"%s\n",line.c_str());
    }
private:
    static VkResult vkQueueSubmit(VkQueue q,std::uint32_t n,const VkSubmitInfo* s,VkFence f){VkCallTimer t(VcQueueSubmit,n?s[0].commandBufferCount:0);return ::vkQueueSubmit(q,n,s,f);}
    // The space before '(' keeps execution.py's GPU-wait rewrite (which wraps every call site in execution_gpu_wait) off these definitions.
    static VkResult vkWaitSemaphores (VkDevice d,const VkSemaphoreWaitInfo* i,std::uint64_t timeout){VkCallTimer t(VcWaitSemaphores,timeout);return ::vkWaitSemaphores (d,i,timeout);}
    static VkResult vkWaitForFences (VkDevice d,std::uint32_t n,const VkFence* f,VkBool32 all,std::uint64_t timeout){VkCallTimer t(VcWaitForFences,n);return ::vkWaitForFences (d,n,f,all,timeout);}
    static VkResult vkGetSemaphoreCounterValue(VkDevice d,VkSemaphore s,std::uint64_t* v){VkCallTimer t(VcSemaphoreCounter);return ::vkGetSemaphoreCounterValue(d,s,v);}
    static VkResult vkGetFenceStatus(VkDevice d,VkFence f){VkCallTimer t(VcFenceStatus);return ::vkGetFenceStatus(d,f);}
    static VkResult vkResetFences(VkDevice d,std::uint32_t n,const VkFence* f){VkCallTimer t(VcResetFences,n);return ::vkResetFences(d,n,f);}
    static void vkUpdateDescriptorSets(VkDevice d,std::uint32_t n,const VkWriteDescriptorSet* w,std::uint32_t m,const VkCopyDescriptorSet* c){VkCallTimer t(VcUpdateDescriptorSets,n);::vkUpdateDescriptorSets(d,n,w,m,c);}
    static VkResult vkResetCommandBuffer(VkCommandBuffer c,VkCommandBufferResetFlags f){VkCallTimer t(VcResetCommandBuffer);return ::vkResetCommandBuffer(c,f);}
    static VkResult vkBeginCommandBuffer(VkCommandBuffer c,const VkCommandBufferBeginInfo* i){VkCallTimer t(VcBeginCommandBuffer);return ::vkBeginCommandBuffer(c,i);}
    static VkResult vkEndCommandBuffer(VkCommandBuffer c){VkCallTimer t(VcEndCommandBuffer);return ::vkEndCommandBuffer(c);}
    static VkResult vkCreateImageView(VkDevice d,const VkImageViewCreateInfo* ci,const VkAllocationCallbacks* a,VkImageView* out){VkCallTimer t(VcCreateView);return ::vkCreateImageView(d,ci,a,out);}
    static void vkDestroyImageView(VkDevice d,VkImageView v,const VkAllocationCallbacks* a){VkCallTimer t(VcDestroyView);::vkDestroyImageView(d,v,a);}
    static VkResult vkCreateSampler(VkDevice d,const VkSamplerCreateInfo* ci,const VkAllocationCallbacks* a,VkSampler* out){VkCallTimer t(VcCreateSampler);return ::vkCreateSampler(d,ci,a,out);}
    static VkResult vkGetQueryPoolResults(VkDevice d,VkQueryPool p,std::uint32_t first,std::uint32_t n,std::size_t size,void* data,VkDeviceSize stride,VkQueryResultFlags f){VkCallTimer t(VcQueryResults,n);return ::vkGetQueryPoolResults(d,p,first,n,size,data,stride,f);}
    static void vkCmdBeginRenderPass(VkCommandBuffer c,const VkRenderPassBeginInfo* i,VkSubpassContents s){VkCallTimer t(VcBeginRenderPass);::vkCmdBeginRenderPass(c,i,s);}
    static void vkCmdPipelineBarrier(VkCommandBuffer c,VkPipelineStageFlags src,VkPipelineStageFlags dst,VkDependencyFlags f,std::uint32_t nm,const VkMemoryBarrier* m,std::uint32_t nb,const VkBufferMemoryBarrier* b,std::uint32_t ni,const VkImageMemoryBarrier* i){VkCallTimer t(VcPipelineBarrier);::vkCmdPipelineBarrier(c,src,dst,f,nm,m,nb,b,ni,i);}
    static VkResult vkAcquireNextImageKHR(VkDevice d,VkSwapchainKHR s,std::uint64_t timeout,VkSemaphore sem,VkFence f,std::uint32_t* index){VkCallTimer t(VcAcquire);return ::vkAcquireNextImageKHR(d,s,timeout,sem,f,index);}
    static VkResult vkQueuePresentKHR(VkQueue q,const VkPresentInfoKHR* i){VkCallTimer t(VcPresent);return ::vkQueuePresentKHR(q,i);}
    static VkResult vkCreateBuffer(VkDevice d,const VkBufferCreateInfo* ci,const VkAllocationCallbacks* a,VkBuffer* out){VkCallTimer t(VcCreateBuffer,ci->size);
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_buffers);const auto r=::vkCreateBuffer(d,ci,a,out);if(r==VK_SUCCESS){VkMemoryRequirements req{};::vkGetBufferMemoryRequirements(d,*out,&req);resource_stats().add("buffers",resource_id(*out),req.size);}return r;
    }
    static void vkDestroyBuffer(VkDevice d,VkBuffer object,const VkAllocationCallbacks* a){VkCallTimer t(VcDestroyBuffer);
        resource_stats().remove("buffers",resource_id(object));::vkDestroyBuffer(d,object,a);
    }
    static VkResult vkCreateImage(VkDevice d,const VkImageCreateInfo* ci,const VkAllocationCallbacks* a,VkImage* out){VkCallTimer t(VcCreateImage,std::uint64_t(ci->extent.width)*ci->extent.height);
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_images);const auto r=::vkCreateImage(d,ci,a,out);if(r==VK_SUCCESS){VkMemoryRequirements req{};::vkGetImageMemoryRequirements(d,*out,&req);resource_stats().add("images",resource_id(*out),req.size);}return r;
    }
    static void vkDestroyImage(VkDevice d,VkImage object,const VkAllocationCallbacks* a){VkCallTimer t(VcDestroyImage);
        resource_stats().remove("images",resource_id(object));::vkDestroyImage(d,object,a);
    }
    static VkResult vkCreateShaderModule(VkDevice d,const VkShaderModuleCreateInfo* ci,const VkAllocationCallbacks* a,VkShaderModule* out){VkCallTimer t(VcShaderModule,ci->codeSize);
        const auto r=::vkCreateShaderModule(d,ci,a,out);if(r==VK_SUCCESS){resource_stats().add("shader_modules",resource_id(*out),ci->codeSize);}return r;
    }
    static void vkDestroyShaderModule(VkDevice d,VkShaderModule object,const VkAllocationCallbacks* a){
        resource_stats().remove("shader_modules",resource_id(object));::vkDestroyShaderModule(d,object,a);
    }
    static VkResult vkCreateFramebuffer(VkDevice d,const VkFramebufferCreateInfo* ci,const VkAllocationCallbacks* a,VkFramebuffer* out){VkCallTimer t(VcCreateFramebuffer);
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_framebuffers);const auto r=::vkCreateFramebuffer(d,ci,a,out);if(r==VK_SUCCESS){resource_stats().add("framebuffers",resource_id(*out),0);}return r;
    }
    static void vkDestroyFramebuffer(VkDevice d,VkFramebuffer object,const VkAllocationCallbacks* a){VkCallTimer t(VcDestroyFramebuffer);
        resource_stats().remove("framebuffers",resource_id(object));::vkDestroyFramebuffer(d,object,a);
    }
    static VkResult vkCreateDescriptorPool(VkDevice d,const VkDescriptorPoolCreateInfo* ci,const VkAllocationCallbacks* a,VkDescriptorPool* out){VkCallTimer t(VcCreateDescriptorPool);
        const auto r=::vkCreateDescriptorPool(d,ci,a,out);if(r==VK_SUCCESS){resource_stats().add("descriptor_pools",resource_id(*out),0);}return r;
    }
    static void vkDestroyDescriptorPool(VkDevice d,VkDescriptorPool object,const VkAllocationCallbacks* a){VkCallTimer t(VcDescriptorPoolDestroy);
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
                    VkCallTimer reuse(VcAllocReuse,ci->allocationSize);*out=it->memory;c.bytes-=it->size;c.free.erase(it);c.live[{resource_id(d),resource_id(*out)}]={ci->memoryTypeIndex,ci->allocationSize};
                    ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_reuses);resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);return VK_SUCCESS;}}}
        VkCallTimer miss(VcAllocMiss,ci->allocationSize);::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_allocs);auto r=::vkAllocateMemory(d,ci,a,out);
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
                if(entry.second<=c.limit){VkCallTimer cached(VcFreeCached,entry.second);
                    c.free.push_back({d,entry.first,entry.second,object,++c.clock});c.bytes+=entry.second;
                    while(c.bytes>c.limit){auto oldest=std::min_element(c.free.begin(),c.free.end(),[](const auto& x,const auto& y){return x.stamp<y.stamp;});
                        c.bytes-=oldest->size;::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(oldest->device,oldest->memory,nullptr);c.free.erase(oldest);}
                    return;}}}
        VkCallTimer t(VcFree);::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(d,object,a);
    }
    static void vkDestroyPipeline(VkDevice d,VkPipeline object,const VkAllocationCallbacks* a){VkCallTimer t(VcPipelineDestroy);resource_stats().remove("pipelines",resource_id(object));::vkDestroyPipeline(d,object,a);}
    static void release_descriptor_sets(VkDescriptorPool pool){auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);auto it=stats.descriptor_sets.find(resource_id(pool));if(it!=stats.descriptor_sets.end()){for(auto id:it->second)stats.remove("descriptor_sets",id);stats.descriptor_sets.erase(it);}}
    static VkResult vkResetDescriptorPool(VkDevice d,VkDescriptorPool pool,VkDescriptorPoolResetFlags flags){VkCallTimer t(VcResetDescriptorPool);const auto r=::vkResetDescriptorPool(d,pool,flags);if(r==VK_SUCCESS)release_descriptor_sets(pool);return r;}
    static VkResult vkAllocateDescriptorSets(VkDevice d,const VkDescriptorSetAllocateInfo* ci,VkDescriptorSet* out){VkCallTimer t(VcAllocateDescriptorSets);const auto r=::vkAllocateDescriptorSets(d,ci,out);if(r==VK_SUCCESS){auto& stats=resource_stats();std::lock_guard<std::recursive_mutex> lock(stats.mutex);for(unsigned i=0;i<ci->descriptorSetCount;++i){auto id=resource_id(out[i]);stats.add("descriptor_sets",id);stats.descriptor_sets[resource_id(ci->descriptorPool)].push_back(id);}}return r;}
    static VkResult vkCreateGraphicsPipelines(VkDevice d,VkPipelineCache cache,std::uint32_t n,const VkGraphicsPipelineCreateInfo* ci,const VkAllocationCallbacks* a,VkPipeline* out){VkCallTimer t(VcGraphicsPipeline,n);const auto r=::vkCreateGraphicsPipelines(d,cache,n,ci,a,out);for(unsigned i=0;i<n;++i)if(out[i])resource_stats().add("pipelines",resource_id(out[i]));return r;}
    // Diagnostic command-kind tags for ANYPS3_RSX_VK_MARKS (see GraphicsMark); the wrappers only record the kind.
    static void vkCmdCopyBuffer(VkCommandBuffer c,VkBuffer a,VkBuffer b,std::uint32_t n,const VkBufferCopy* r){graphics_last_kind()=MarkCopyBuffer;::vkCmdCopyBuffer(c,a,b,n,r);}
    static void vkCmdCopyBufferToImage(VkCommandBuffer c,VkBuffer a,VkImage b,VkImageLayout l,std::uint32_t n,const VkBufferImageCopy* r){graphics_last_kind()=MarkCopyImage;::vkCmdCopyBufferToImage(c,a,b,l,n,r);}
    static void vkCmdCopyImageToBuffer(VkCommandBuffer c,VkImage a,VkImageLayout l,VkBuffer b,std::uint32_t n,const VkBufferImageCopy* r){graphics_last_kind()=MarkCopyImage;::vkCmdCopyImageToBuffer(c,a,l,b,n,r);}
    static void vkCmdCopyImage(VkCommandBuffer c,VkImage a,VkImageLayout al,VkImage b,VkImageLayout bl,std::uint32_t n,const VkImageCopy* r){graphics_last_kind()=MarkCopyImage;::vkCmdCopyImage(c,a,al,b,bl,n,r);}
    static void vkCmdFillBuffer(VkCommandBuffer c,VkBuffer b,VkDeviceSize o,VkDeviceSize n,std::uint32_t v){graphics_last_kind()=MarkFill;::vkCmdFillBuffer(c,b,o,n,v);}
    static void vkCmdDispatch(VkCommandBuffer c,std::uint32_t x,std::uint32_t y,std::uint32_t z){graphics_last_kind()=MarkDispatch;::vkCmdDispatch(c,x,y,z);}
    static void vkCmdClearColorImage(VkCommandBuffer c,VkImage i,VkImageLayout l,const VkClearColorValue* v,std::uint32_t n,const VkImageSubresourceRange* r){graphics_last_kind()=MarkClear;::vkCmdClearColorImage(c,i,l,v,n,r);}
    static void vkCmdClearDepthStencilImage(VkCommandBuffer c,VkImage i,VkImageLayout l,const VkClearDepthStencilValue* v,std::uint32_t n,const VkImageSubresourceRange* r){graphics_last_kind()=MarkClear;::vkCmdClearDepthStencilImage(c,i,l,v,n,r);}
    static VkResult vkCreateComputePipelines(VkDevice d,VkPipelineCache cache,std::uint32_t n,const VkComputePipelineCreateInfo* ci,const VkAllocationCallbacks* a,VkPipeline* out){VkCallTimer t(VcComputePipeline,n);const auto r=::vkCreateComputePipelines(d,cache,n,ci,a,out);for(unsigned i=0;i<n;++i)if(out[i])resource_stats().add("pipelines",resource_id(out[i]));return r;}
#endif
'''
