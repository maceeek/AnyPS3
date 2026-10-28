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
    // TEST HOOK / diagnostic: pool structure of one device. Stops on a broken invariant: ranges of a block (live + free)
    // must tile it exactly without overlap, free neighbours must be coalesced, block live/used counts and the retained
    // empty bytes must match the ranges.
    struct PoolReport {std::uint64_t blocks=0,empty_blocks=0,live=0,free_ranges=0,used=0,reserved=0;};
    static PoolReport memory_pool_report(VkDevice d){
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);PoolReport report;
        const auto device=c.devices.find(d);if(device==c.devices.end())return report;
        std::map<const void*,std::vector<std::pair<VkDeviceSize,VkDeviceSize>>> live;
        for(const auto& [key,sub]:c.subs)if(sub->device==d){(void)key;live[sub->block].push_back({sub->offset,sub->size});}
        for(const auto& [type,pool]:device->second.pools)for(const auto& block:pool.blocks){(void)type;
            auto ranges=live[block.get()];VkDeviceSize used=0;for(const auto& r:ranges)used+=r.second;
            if(ranges.size()!=block->live || used!=block->used)throw std::runtime_error("pool block live/used count mismatch");
            for(auto it=block->free.begin();it!=block->free.end();++it){ranges.push_back(*it);
                const auto next=std::next(it);if(next!=block->free.end() && it->first+it->second==next->first)throw std::runtime_error("pool free ranges not coalesced");}
            std::sort(ranges.begin(),ranges.end());VkDeviceSize at=0;
            for(const auto& r:ranges){if(r.first!=at || !r.second)throw std::runtime_error("pool block ranges overlap or leave a hole");at+=r.second;}
            if(at!=block->size)throw std::runtime_error("pool block ranges do not cover the block");
            ++report.blocks;report.empty_blocks+=!block->live;report.live+=block->live;report.free_ranges+=block->free.size();report.used+=block->used;report.reserved+=block->size;}
        VkDeviceSize all_empty=0;for(const auto& [dev,state]:c.devices)for(const auto& [type,pool]:state.pools)for(const auto& block:pool.blocks){(void)dev;(void)type;if(!block->live)all_empty+=block->size;}
        if(all_empty!=c.pool_empty)throw std::runtime_error("pool retained empty bytes mismatch");
        return report;
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

    // Device-local (never host-visible, hence never mapped) allocations: vkFreeMemory/vkAllocateMemory cost dozens of RM ioctls
    // each on NVIDIA (single frees up to 16.8 ms at 1440p, HIRES2), so the driver should rarely see them during play.
    // (1) POOL (ANYPS3_RSX_MEMORY_POOL, default 1; 0 = the exact-size recycler alone): an allocation of at most half a block is
    //     a sub-range of a large driver block (ANYPS3_RSX_MEMORY_POOL_MB, default 256) of the same memory type. Best fit over
    //     offset-ordered free ranges, coalesced on free. The caller gets a pool handle (the address of its Sub record, never a
    //     driver handle: a colliding driver handle is an explicit stop); the bind/map/free wrappers below translate it to
    //     (block, offset). A range is pooled only when this thread's preceding vkGet*MemoryRequirements call reported the
    //     same size, so its alignment is known; offsets are aligned to max(alignment, bufferImageGranularity) and sizes
    //     rounded to the granularity, so linear and optimal resources never share a granularity page. Every bind re-reads
    //     the object's requirements and stops if the range cannot hold it. A freed range is reusable at once: the old code
    //     freed the whole allocation at the same point, i.e. after the GPU finished with every resource bound to it.
    //     Empty blocks stay allocated up to the recycler limit (at least one block per pool); only the excess is freed.
    // (2) RECYCLER (ANYPS3_RSX_MEMORY_CACHE_MB, default 384, 0 = off, which also disables the pool): every other cacheable
    //     allocation is recycled by exact (memory type, size); the oldest blocks are really freed first.
    // TEST HOOK ANYPS3_RSX_MEMORY_POOL_TYPES=coherent also pools HOST_COHERENT host-visible types (lavapipe has no
    // device-local-only type): a mapped pool range is a view of its block's persistent mapping. Non-coherent pool memory is
    // never created, so a flush/invalidate of a pool handle is an explicit stop.
    struct MemoryCache {
        struct Free {VkDevice device;std::uint32_t type;VkDeviceSize size;VkDeviceMemory memory;std::uint64_t stamp;};
        struct Block {VkDeviceMemory memory=VK_NULL_HANDLE;VkDeviceSize size=0,used=0;std::map<VkDeviceSize,VkDeviceSize> free;unsigned live=0;void* map=nullptr;};
        struct Pool {std::vector<std::unique_ptr<Block>> blocks;};
        struct Sub {VkDevice device;std::uint32_t type;Block* block;VkDeviceSize offset,size,requested;};
        struct Device {std::uint32_t recycle=0,pool=0;VkDeviceSize granularity=1,block=UINT64_C(256)<<20;std::map<std::uint32_t,Pool> pools;};
        std::mutex mutex;std::vector<Free> free;std::map<std::pair<std::uint64_t,std::uint64_t>,std::pair<std::uint32_t,VkDeviceSize>> live;   // key (VkDevice, VkDeviceMemory): handles are only unique per device
        std::map<VkDevice,Device> devices;std::unordered_map<std::uint64_t,std::unique_ptr<Sub>> subs;   // key: pool handle value
        VkDeviceSize bytes=0,limit=UINT64_C(384)<<20,pool_empty=0;std::uint64_t clock=0;
    };
    static MemoryCache& memory_cache(){static MemoryCache cache;return cache;}
    // This thread's last vkGet*MemoryRequirements result (consumed by the next vkAllocateMemory of the same size).
    struct PendingRequirement {VkDevice device=VK_NULL_HANDLE;VkDeviceSize size=0,alignment=0;};
    static PendingRequirement& pending_requirement(){static thread_local PendingRequirement pending;return pending;}
    static void vkGetImageMemoryRequirements(VkDevice d,VkImage image,VkMemoryRequirements* r){::vkGetImageMemoryRequirements(d,image,r);pending_requirement()={d,r->size,r->alignment};}
    static void vkGetBufferMemoryRequirements(VkDevice d,VkBuffer buffer,VkMemoryRequirements* r){::vkGetBufferMemoryRequirements(d,buffer,r);pending_requirement()={d,r->size,r->alignment};}
    void configure_memory_cache(VkDevice d){
        const char* types=std::getenv("ANYPS3_RSX_MEMORY_POOL_TYPES");
        if(types && std::strcmp(types,"coherent"))throw std::runtime_error("invalid ANYPS3_RSX_MEMORY_POOL_TYPES");
        std::uint32_t mask=0,pool_mask=0;
        for(std::uint32_t i=0;i<memory.memoryTypeCount;++i){const auto f=memory.memoryTypes[i].propertyFlags;
            if((f&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) && !(f&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)){mask|=1u<<i;pool_mask|=1u<<i;}
            else if(types && (f&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))pool_mask|=1u<<i;}
        VkPhysicalDeviceProperties properties{};::vkGetPhysicalDeviceProperties(physical,&properties);
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);
        c.limit=UINT64_C(384)<<20;   // default on every configuration: the singleton must not keep a previous device's setting
        if(const char* v=std::getenv("ANYPS3_RSX_MEMORY_CACHE_MB")){char* end=nullptr;const auto n=std::strtoull(v,&end,10);
            if(!*v || *end || n>65536){throw std::runtime_error("invalid ANYPS3_RSX_MEMORY_CACHE_MB");}
            c.limit=n<<20;}
        bool pool=true;VkDeviceSize block=UINT64_C(256)<<20;
        if(const char* v=std::getenv("ANYPS3_RSX_MEMORY_POOL")){if(std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("invalid ANYPS3_RSX_MEMORY_POOL");pool=*v=='1';}
        if(const char* v=std::getenv("ANYPS3_RSX_MEMORY_POOL_MB")){char* end=nullptr;const auto n=std::strtoull(v,&end,10);
            if(!*v || *end || n<1 || n>4096)throw std::runtime_error("invalid ANYPS3_RSX_MEMORY_POOL_MB");
            block=n<<20;}
        auto& device=c.devices[d];device=MemoryCache::Device{};
        device.recycle=c.limit?mask:0;device.pool=c.limit && pool?pool_mask:0;
        device.granularity=std::max<VkDeviceSize>(1,properties.limits.bufferImageGranularity);device.block=block;
    }
    static void release_memory_cache(VkDevice d)noexcept{
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);
        for(auto it=c.free.begin();it!=c.free.end();){if(it->device==d){c.bytes-=it->size;::vkFreeMemory(d,it->memory,nullptr);it=c.free.erase(it);}else ++it;}
        const auto device=c.devices.find(d);
        if(device!=c.devices.end()){
            for(auto& [type,pool]:device->second.pools)for(auto& block:pool.blocks){(void)type;if(!block->live)c.pool_empty-=block->size;
                if(block->map){::vkUnmapMemory(d,block->memory);}::vkFreeMemory(d,block->memory,nullptr);}
            c.devices.erase(device);}
        for(auto it=c.subs.begin();it!=c.subs.end();)if(it->second->device==d)it=c.subs.erase(it);else ++it;
        for(auto it=c.live.begin();it!=c.live.end();)if(it->first.first==resource_id(d))it=c.live.erase(it);else ++it;
    }
    // Pool lookup (caller holds the mutex); nullptr for a driver handle.
    static MemoryCache::Sub* pool_sub(MemoryCache& c,VkDevice d,VkDeviceMemory memory){
        const auto it=c.subs.find(resource_id(memory));return it!=c.subs.end() && it->second->device==d?it->second.get():nullptr;
    }
    // Best fit over every block of the pool; a new block when nothing fits. Caller holds the mutex.
    static VkResult pool_allocate(MemoryCache& c,VkDevice d,MemoryCache::Device& device,std::uint32_t type,VkDeviceSize requested,VkDeviceSize alignment,VkDeviceMemory* out){
        const auto granularity=device.granularity;
        const auto align=std::max(alignment?alignment:1,granularity);
        if(align&(align-1))return VK_ERROR_FEATURE_NOT_PRESENT;   // not a power of two: leave it to the driver
        const auto size=(requested+granularity-1)/granularity*granularity;
        auto& pool=device.pools[type];
        MemoryCache::Block* best=nullptr;VkDeviceSize best_offset=0,best_waste=UINT64_MAX;
        for(auto& block:pool.blocks)for(const auto& [offset,length]:block->free){
            const auto at=(offset+align-1)&~(align-1);
            if(at+size>offset+length)continue;
            const auto waste=length-size;
            if(waste<best_waste){best_waste=waste;best=block.get();best_offset=at;if(!waste)break;}
        }
        if(!best){
            VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=device.block;ai.memoryTypeIndex=type;
            auto block=std::make_unique<MemoryCache::Block>();
            VkResult r;{VkCallTimer miss(VcAllocMiss,ai.allocationSize);r=::vkAllocateMemory(d,&ai,nullptr,&block->memory);}
            if(r!=VK_SUCCESS)return r;
            if(c.subs.count(resource_id(block->memory))){::vkFreeMemory(d,block->memory,nullptr);throw std::runtime_error("Vulkan device memory handle collides with a pool range");}
            ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_allocs);::RsxPerfCounters::add(::RsxPerfCounters::vk_pool_blocks);
            resource_stats().add("device_memory_blocks",resource_id(block->memory),ai.allocationSize);
            block->size=ai.allocationSize;block->free[0]=ai.allocationSize;c.pool_empty+=block->size;
            best=block.get();best_offset=0;pool.blocks.push_back(std::move(block));
        }
        // Split the chosen free range around [best_offset, best_offset+size).
        auto range=best->free.upper_bound(best_offset);--range;
        const auto start=range->first,end=range->first+range->second;best->free.erase(range);
        if(best_offset>start)best->free[start]=best_offset-start;
        if(best_offset+size<end)best->free[best_offset+size]=end-best_offset-size;
        if(!best->live++)c.pool_empty-=best->size;
        best->used+=size;
        auto sub=std::make_unique<MemoryCache::Sub>(MemoryCache::Sub{d,type,best,best_offset,size,requested});
        *out=reinterpret_cast<VkDeviceMemory>(sub.get());c.subs.emplace(resource_id(*out),std::move(sub));
        ::RsxPerfCounters::add(::RsxPerfCounters::vk_pool_ranges);
        return VK_SUCCESS;
    }
    // Returns a range (coalescing with its neighbours); frees empty blocks beyond the retained limit. Caller holds the mutex.
    static void pool_release(MemoryCache& c,MemoryCache::Sub& sub){
        auto& block=*sub.block;auto offset=sub.offset,size=sub.size;
        auto next=block.free.lower_bound(offset);
        if(next!=block.free.end() && next->first==offset+size){size+=next->second;next=block.free.erase(next);}
        if(next!=block.free.begin()){auto previous=std::prev(next);if(previous->first+previous->second==offset){offset=previous->first;size+=previous->second;block.free.erase(previous);}}
        block.free[offset]=size;block.used-=sub.size;
        if(--block.live)return;
        c.pool_empty+=block.size;
        auto& pool=c.devices.at(sub.device).pools.at(sub.type);
        while(c.pool_empty>std::max(c.limit,block.size)){
            auto victim=std::find_if(pool.blocks.begin(),pool.blocks.end(),[](const auto& b){return !b->live;});
            if(victim==pool.blocks.end())break;
            c.pool_empty-=(*victim)->size;resource_stats().remove("device_memory_blocks",resource_id((*victim)->memory));
            if((*victim)->map){::vkUnmapMemory(sub.device,(*victim)->memory);}
            {VkCallTimer t(VcFree);::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(sub.device,(*victim)->memory,nullptr);}
            if(victim->get()==&block){pool.blocks.erase(victim);break;}
            pool.blocks.erase(victim);
        }
    }
    static VkResult vkAllocateMemory(VkDevice d,const VkMemoryAllocateInfo* ci,const VkAllocationCallbacks* a,VkDeviceMemory* out){
        auto& c=memory_cache();bool cacheable=false;
        const auto pending=pending_requirement();pending_requirement()={};
        {std::lock_guard<std::mutex> lock(c.mutex);
            const auto device=c.devices.find(d);
            const bool plain=!ci->pNext && !a && device!=c.devices.end() && ci->memoryTypeIndex<32;
            if(plain && (device->second.pool&(1u<<ci->memoryTypeIndex)) && pending.device==d && pending.size==ci->allocationSize && ci->allocationSize && ci->allocationSize<=device->second.block/2){
                auto r=pool_allocate(c,d,device->second,ci->memoryTypeIndex,ci->allocationSize,pending.alignment,out);
                if(r==VK_ERROR_OUT_OF_DEVICE_MEMORY){
                    for(auto it=c.free.begin();it!=c.free.end();){if(it->device==d){c.bytes-=it->size;::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(d,it->memory,nullptr);it=c.free.erase(it);}else ++it;}
                    r=pool_allocate(c,d,device->second,ci->memoryTypeIndex,ci->allocationSize,pending.alignment,out);}
                if(r==VK_SUCCESS){resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);return r;}
                // Anything else (no block fits the heap any more, odd alignment): an exact driver allocation below.
            }
            cacheable=plain && (device->second.recycle&(1u<<ci->memoryTypeIndex));
            if(cacheable){
                for(auto it=c.free.begin();it!=c.free.end();++it)if(it->device==d && it->type==ci->memoryTypeIndex && it->size==ci->allocationSize){
                    VkCallTimer reuse(VcAllocReuse,ci->allocationSize);*out=it->memory;c.bytes-=it->size;c.free.erase(it);c.live[{resource_id(d),resource_id(*out)}]={ci->memoryTypeIndex,ci->allocationSize};
                    ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_reuses);resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);return VK_SUCCESS;}}}
        VkCallTimer miss(VcAllocMiss,ci->allocationSize);::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_allocs);auto r=::vkAllocateMemory(d,ci,a,out);
        // Any allocation that fails for device memory (a host-visible staging block can share a heap with the cached
        // device-local blocks) gets the cached blocks back before the failure is reported.
        if(r==VK_ERROR_OUT_OF_DEVICE_MEMORY){release_memory_cache_blocks(d);r=::vkAllocateMemory(d,ci,a,out);}
        if(r==VK_SUCCESS){resource_stats().add("device_memory",resource_id(*out),ci->allocationSize);
            std::lock_guard<std::mutex> lock(c.mutex);
            if(c.subs.count(resource_id(*out))){::vkFreeMemory(d,*out,a);*out=VK_NULL_HANDLE;throw std::runtime_error("Vulkan device memory handle collides with a pool range");}
            if(cacheable)c.live[{resource_id(d),resource_id(*out)}]={ci->memoryTypeIndex,ci->allocationSize};}
        return r;
    }
    // Out of device memory: every cached exact block and every empty pool block goes back to the driver.
    static void release_memory_cache_blocks(VkDevice d){
        auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);
        for(auto it=c.free.begin();it!=c.free.end();){if(it->device==d){c.bytes-=it->size;::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(d,it->memory,nullptr);it=c.free.erase(it);}else ++it;}
        const auto device=c.devices.find(d);if(device==c.devices.end())return;
        for(auto& [type,pool]:device->second.pools){(void)type;
            for(auto it=pool.blocks.begin();it!=pool.blocks.end();){auto& block=**it;if(block.live){++it;continue;}
                c.pool_empty-=block.size;resource_stats().remove("device_memory_blocks",resource_id(block.memory));if(block.map){::vkUnmapMemory(d,block.memory);}
                ::RsxPerfCounters::add(::RsxPerfCounters::vk_memory_frees);::vkFreeMemory(d,block.memory,nullptr);it=pool.blocks.erase(it);}}
    }
    static void vkFreeMemory(VkDevice d,VkDeviceMemory object,const VkAllocationCallbacks* a){
        resource_stats().remove("device_memory",resource_id(object));
        auto& c=memory_cache();
        {std::lock_guard<std::mutex> lock(c.mutex);
            if(auto* sub=pool_sub(c,d,object)){if(a)throw std::runtime_error("pool range freed with allocation callbacks");
                pool_release(c,*sub);c.subs.erase(resource_id(object));return;}
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
    // A pool range must hold the object it is bound to: aligned offset, enough bytes (re-read from the driver).
    static VkResult vkBindImageMemory(VkDevice d,VkImage image,VkDeviceMemory memory,VkDeviceSize offset){
        auto& c=memory_cache();VkDeviceMemory target=memory;
        {std::lock_guard<std::mutex> lock(c.mutex);
            if(const auto* sub=pool_sub(c,d,memory)){VkMemoryRequirements r{};::vkGetImageMemoryRequirements(d,image,&r);
                if(!(r.memoryTypeBits&(1u<<sub->type)) || (sub->offset+offset)%std::max<VkDeviceSize>(1,r.alignment) || offset+r.size>sub->requested)
                    throw std::runtime_error("RSX image does not fit its pooled device-memory range");
                target=sub->block->memory;offset+=sub->offset;}}
        return ::vkBindImageMemory(d,image,target,offset);
    }
    static VkResult vkBindBufferMemory(VkDevice d,VkBuffer buffer,VkDeviceMemory memory,VkDeviceSize offset){
        auto& c=memory_cache();VkDeviceMemory target=memory;
        {std::lock_guard<std::mutex> lock(c.mutex);
            if(const auto* sub=pool_sub(c,d,memory)){VkMemoryRequirements r{};::vkGetBufferMemoryRequirements(d,buffer,&r);
                if(!(r.memoryTypeBits&(1u<<sub->type)) || (sub->offset+offset)%std::max<VkDeviceSize>(1,r.alignment) || offset+r.size>sub->requested)
                    throw std::runtime_error("RSX buffer does not fit its pooled device-memory range");
                target=sub->block->memory;offset+=sub->offset;}}
        return ::vkBindBufferMemory(d,buffer,target,offset);
    }
    // Pool memory is HOST_COHERENT whenever it is host-visible (test hook): one persistent block mapping serves every range.
    static VkResult vkMapMemory(VkDevice d,VkDeviceMemory memory,VkDeviceSize offset,VkDeviceSize size,VkMemoryMapFlags flags,void** data){
        auto& c=memory_cache();
        {std::lock_guard<std::mutex> lock(c.mutex);
            if(const auto* sub=pool_sub(c,d,memory)){
                if(flags || offset>sub->requested || (size!=VK_WHOLE_SIZE && size>sub->requested-offset))throw std::runtime_error("invalid map of a pooled device-memory range");
                if(!sub->block->map){const auto r=::vkMapMemory(d,sub->block->memory,0,VK_WHOLE_SIZE,0,&sub->block->map);if(r!=VK_SUCCESS)return r;}
                *data=static_cast<char*>(sub->block->map)+sub->offset+offset;return VK_SUCCESS;}}
        return ::vkMapMemory(d,memory,offset,size,flags,data);
    }
    static void vkUnmapMemory(VkDevice d,VkDeviceMemory memory){
        auto& c=memory_cache();
        {std::lock_guard<std::mutex> lock(c.mutex);if(pool_sub(c,d,memory))return;}   // the block mapping stays until the block is freed
        ::vkUnmapMemory(d,memory);
    }
    static VkResult vkFlushMappedMemoryRanges(VkDevice d,std::uint32_t n,const VkMappedMemoryRange* ranges){
        {auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);for(std::uint32_t i=0;i<n;++i)if(pool_sub(c,d,ranges[i].memory))throw std::runtime_error("flush of a coherent pooled device-memory range");}
        return ::vkFlushMappedMemoryRanges(d,n,ranges);
    }
    static VkResult vkInvalidateMappedMemoryRanges(VkDevice d,std::uint32_t n,const VkMappedMemoryRange* ranges){
        {auto& c=memory_cache();std::lock_guard<std::mutex> lock(c.mutex);for(std::uint32_t i=0;i<n;++i)if(pool_sub(c,d,ranges[i].memory))throw std::runtime_error("invalidate of a coherent pooled device-memory range");}
        return ::vkInvalidateMappedMemoryRanges(d,n,ranges);
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
