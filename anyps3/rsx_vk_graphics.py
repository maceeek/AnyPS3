"""Vulkan graphics transport. RSX decoding and guest ownership live separately.

Sample planes use single-sample array layers with explicit viewport offsets.
This preserves the RSX sample-index layout even on devices without 2x MSAA or
programmable sample locations. Each plane is rasterized and shaded by hardware.
"""
from .rsx_vk_sampled import MEMBERS as SAMPLED_MEMBERS
from .rsx_vk_readback import MEMBERS as READBACK_MEMBERS
from .rsx_vk_upload import MEMBERS as UPLOAD_MEMBERS
from .rsx_vk_texture_decode import MEMBERS as DECODE_MEMBERS

MEMBERS = r'''
#ifdef ANYPS3_RSX_VK
public:
    struct GraphicsState {
        unsigned width=0,height=0,samples=1,colors=1;
        VkFormat color=VK_FORMAT_R8G8B8A8_UNORM,depth=VK_FORMAT_UNDEFINED;
        std::array<VkPipelineColorBlendAttachmentState,4> blend{};
        VkPipelineDepthStencilStateCreateInfo z{};
        VkPipelineRasterizationStateCreateInfo raster{};
        std::array<float,4> constants{};
        VkRect2D scissor{};
        bool logic_enable=false;VkLogicOp logic=VK_LOGIC_OP_COPY;
    };
    struct GraphicsPipeline;
    struct GraphicsImage {
        NativeVulkan* owner;
        VkSampler sampler=VK_NULL_HANDLE;
        VkImage image=VK_NULL_HANDLE;VkDeviceMemory memory=VK_NULL_HANDLE;
        std::vector<VkImageView> views;
        VkImageView array_view=VK_NULL_HANDLE,attachment_array=VK_NULL_HANDLE;
        VkFormat format;unsigned width,height,layers;
        bool depth;std::uint64_t memory_bytes=0;
        GraphicsImage(NativeVulkan* o,VkFormat f,unsigned w,unsigned h,unsigned l,bool d):owner(o),format(f),width(w),height(h),layers(l),depth(d){}
        ~GraphicsImage(){if(sampler)vkDestroySampler(owner->device,sampler,nullptr);
            for(auto v:views)vkDestroyImageView(owner->device,v,nullptr);
            if(array_view)vkDestroyImageView(owner->device,array_view,nullptr);
            if(attachment_array)vkDestroyImageView(owner->device,attachment_array,nullptr);
            if(image)vkDestroyImage(owner->device,image,nullptr);
            if(memory)vkFreeMemory(owner->device,memory,nullptr);
        }
    };
    struct GraphicsBuffer {
        NativeVulkan* owner;VkBuffer buffer=VK_NULL_HANDLE;VkDeviceMemory memory=VK_NULL_HANDLE;std::uint64_t size,memory_bytes=0;
        GraphicsBuffer(NativeVulkan* o,std::uint64_t n):owner(o),size(n){}
        ~GraphicsBuffer(){if(buffer)vkDestroyBuffer(owner->device,buffer,nullptr);if(memory)vkFreeMemory(owner->device,memory,nullptr);}
    };
    template<std::size_t N> struct GraphicsKeyHash {
        std::size_t operator()(const std::array<std::uint64_t,N>& key)const{std::uint64_t h=UINT64_C(0x9e3779b97f4a7c15);for(auto v:key){v^=v>>30;v*=UINT64_C(0xbf58476d1ce4e5b9);v^=v>>27;h^=v+UINT64_C(0x9e3779b97f4a7c15)+(h<<6)+(h>>2);}return h;}
    };
    // A batch owns each image once until its completion fence. Open addressing
    // keeps the warm push/clear path allocation-free and avoids rescanning all
    // previous draw references to compute the RTT capacity budget.
    template<class Resource> struct GraphicsPins {
        std::vector<std::shared_ptr<Resource>> owners;
        std::vector<const Resource*> slots=std::vector<const Resource*>(64,nullptr);
        std::uint64_t bytes=0;
        static std::size_t hash(const Resource* p){auto n=reinterpret_cast<std::uintptr_t>(p)>>4;n^=n>>23;return n*UINT64_C(0x9e3779b97f4a7c15);}
        void insert(const Resource* p){auto at=hash(p)&(slots.size()-1);while(slots[at])at=(at+1)&(slots.size()-1);slots[at]=p;}
        bool contains(const Resource* image)const{
            auto at=hash(image)&(slots.size()-1);
            while(slots[at]){if(slots[at]==image)return true;at=(at+1)&(slots.size()-1);}return false;
        }
        void push_back(const std::shared_ptr<Resource>& image){
            if(!image)return;
            auto at=hash(image.get())&(slots.size()-1);
            while(slots[at]){if(slots[at]==image.get())return;at=(at+1)&(slots.size()-1);}
            if((owners.size()+1)*2>slots.size()){slots.assign(slots.size()*2,nullptr);for(const auto& p:owners)insert(p.get());insert(image.get());}
            else slots[at]=image.get();
            owners.push_back(image);if constexpr(std::is_same_v<Resource,GraphicsImage>)bytes+=image->memory_bytes;
        }
        void clear(){owners.clear();std::fill(slots.begin(),slots.end(),nullptr);bytes=0;}
        bool empty()const{return owners.empty();}std::size_t size()const{return owners.size();}
        auto begin()const{return owners.begin();}auto end()const{return owners.end();}
    };
    using GraphicsImagePins=GraphicsPins<GraphicsImage>;
    // Push constants of every graphics draw (vertex and fragment stages).
    struct GraphicsPush{float width,height,x,y;unsigned plane,layers;float shifts[8];};
    struct GraphicsBatch {
        NativeVulkan* owner;
        VkBuffer buffer=VK_NULL_HANDLE,data_buffer=VK_NULL_HANDLE;VkDeviceMemory memory=VK_NULL_HANDLE,data_memory=VK_NULL_HANDLE;void* map=nullptr;
        VkCommandBuffer command=VK_NULL_HANDLE,upload_command=VK_NULL_HANDLE,transfer_command=VK_NULL_HANDLE;VkFence fence=VK_NULL_HANDLE;
        VkCommandPool command_pool=VK_NULL_HANDLE;   // owns `command`: the draw recorder records it while the frontend resets/frees other batches' buffers
        // Async prelude (NativeVulkan::transfer_queue): staging->device copies on the DMA queue, timestamps in their own 2-slot pool.
        VkQueryPool transfer_queries=VK_NULL_HANDLE;bool transfer_pending=false;std::uint64_t upload_wait=0;VkDescriptorPool descriptors=VK_NULL_HANDLE;
        // Further pools chained when one batch records more sets than `descriptors` holds.
        std::vector<VkDescriptorPool> overflow_descriptors;std::size_t overflow_used=0;
        struct DescriptorEntry{VkDescriptorSet set;std::uint64_t last_use;};
        // State blocks use immutable slices until this exact batch retires.
        static constexpr unsigned state_slots=52;
        std::array<std::uint64_t,state_slots> state_versions{},state_offsets{};
        std::uint64_t state_block_uploads=0,state_block_hits=0;
        using DescriptorKey=std::array<std::uint64_t,38>;
        std::unordered_map<DescriptorKey,DescriptorEntry,GraphicsKeyHash<38>> descriptor_cache;
        std::uint64_t descriptor_hits=0,descriptor_misses=0;
        VkSemaphore signal=VK_NULL_HANDLE;
        std::uint64_t completion=0,frames=0;
        VkQueryPool busy_queries=VK_NULL_HANDLE;
        VkQueryPool occlusion=VK_NULL_HANDLE;unsigned visibility_count=0;std::vector<std::uint64_t> visibility;
        VkQueryPool queries=VK_NULL_HANDLE;unsigned query_count=0,timestamp_bits=0;float timestamp_period=0;
        std::vector<unsigned> phases;std::vector<const GraphicsPipeline*> phase_pipelines;
        // Diagnostic (ANYPS3_RSX_VK_MARKS=1): BOTTOM_OF_PIPE timestamp chain; the time between two marks is attributed to the
        // kind of the later mark (GraphicsMark). Serializes the pipeline at every mark: attribution, not a performance mode.
        VkQueryPool marks=VK_NULL_HANDLE;unsigned mark_count=0;std::vector<unsigned> mark_kinds;
        bool coherent=false,recording=false,submitted=false,failed=false;
        std::uint64_t capacity=0,used=0;
        // Shader failures are consumed only after batch completion. Keep the
        // device words live until then instead of a graphics->transfer turn
        // and a four-byte copy after every draw (and every packed primitive).
        std::vector<std::uint64_t> status_offsets;
        // Only fresh immutable CPU slices go in the prelude. GPU scratch and
        // readbacks are deliberately absent: they retain their command order.
        std::vector<VkBufferCopy> uploads;
        VkFramebuffer active_framebuffer=VK_NULL_HANDLE;
        std::uint64_t render_passes=0,render_pass_reuses=0,upload_commands=0;
        // MSAA plane runs: while consecutive draws keep plane 0's pass on one
        // attachment set, their planes >=1 are recorded later, one pass per
        // plane, when that pass ends (graphics_end_pass). Planes are distinct
        // images; each plane keeps the original draw order, and every command
        // outside a pass (copy, barrier, clear, submit) ends the pass first.
        struct PlaneDraw {
            VkPipeline pipeline;VkPipelineLayout layout;VkDescriptorSet set;std::uint32_t offset;
            std::array<GraphicsPush,4> push;std::array<unsigned,22> dynamic;
            unsigned vertices,instances,first_vertex,query;bool indexed;VkDeviceSize index_offset;VkQueryControlFlags query_flags;
        };
        std::vector<PlaneDraw> plane_draws;std::array<VkFramebuffer,4> plane_framebuffers{};
        VkRenderPass plane_pass=VK_NULL_HANDLE;VkExtent2D plane_extent{};unsigned plane_count=0;
        std::uint64_t plane_deferred=0;
        VkPipeline bound_graphics_pipeline=VK_NULL_HANDLE;
        bool dynamic_valid=false;
        std::array<unsigned,22> dynamic_state{};
        std::uint64_t dynamic_emits=0,dynamic_reuses=0,pipeline_binds=0;
        struct Framebuffer {NativeVulkan* owner;VkFramebuffer handle;std::uint64_t last_use=0;std::vector<std::shared_ptr<GraphicsImage>> images;std::shared_ptr<GraphicsPipeline> pipeline;
            ~Framebuffer(){if(handle)vkDestroyFramebuffer(owner->device,handle,nullptr);}};
        struct FramebufferHash {
            std::size_t operator()(const std::array<std::uint64_t,10>& key)const{
                std::uint64_t h=0;for(auto word:key)h^=word+UINT64_C(0x9e3779b97f4a7c15)+(h<<6)+(h>>2);
                h^=h>>30;h*=UINT64_C(0xbf58476d1ce4e5b9);return h^(h>>27);
            }
        };
        std::unordered_map<std::array<std::uint64_t,10>,std::shared_ptr<Framebuffer>,FramebufferHash> framebuffers;
        GraphicsPins<Framebuffer> framebuffer_pins;
        GraphicsPins<GraphicsPipeline> pipeline_pins;
        std::uint64_t framebuffer_hits=0,framebuffer_misses=0;
        GraphicsImagePins pins;
        std::vector<std::shared_ptr<GraphicsBuffer>> buffer_pins;
        std::vector<std::shared_ptr<ComputePipeline>> compute_pins;
        double upload_ms=0,draw_ms=0,download_ms=0,wait_ms=0,pipeline_ms=0,record_ms=0;
        std::uint64_t upload_bytes=0,download_bytes=0,submissions=0,draws=0;
        double record_back_ms=0;   // recorder-owned (back half of graphics_draw)
        bool inline_draws=false;   // ConditionalScope: draws are recorded inline while a conditional-rendering scope is open
        // Waits until the draw recorder has recorded every queued draw (see DrawJob). Required before any use of recorder-owned state.
        std::atomic<unsigned> pending_jobs{0};   // queued, not yet recorded draws of this batch
        void sync(){if(pending_jobs.load(std::memory_order_acquire) || owner->recorder_failed())owner->recorder_drain();}
        explicit GraphicsBatch(NativeVulkan* o):owner(o){}
        ~GraphicsBatch(){
            if(pending_jobs.load(std::memory_order_acquire))owner->recorder_quiesce();
            if(submitted)vkWaitForFences(owner->device,1,&fence,VK_TRUE,UINT64_MAX);
            // A prelude submitted on the DMA queue before a failing graphics submit (or before an abandoned batch) still reads our staging
            // buffer and writes our device buffer: wait for its upload timeline value before anything is freed. upload_wait is set by
            // the submission itself, independently of profiling, and cleared by graphics_reset after retirement.
            if(upload_wait && owner->graphics_upload_timeline){VkSemaphoreWaitInfo wi{};wi.sType=VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO;wi.semaphoreCount=1;wi.pSemaphores=&owner->graphics_upload_timeline;wi.pValues=&upload_wait;
                vkWaitSemaphores(owner->device,&wi,UINT64_MAX);}
            framebuffer_pins.clear();framebuffers.clear();pipeline_pins.clear();
            if(busy_queries)vkDestroyQueryPool(owner->device,busy_queries,nullptr);
            if(occlusion)vkDestroyQueryPool(owner->device,occlusion,nullptr);
            if(queries)vkDestroyQueryPool(owner->device,queries,nullptr);
            if(marks)vkDestroyQueryPool(owner->device,marks,nullptr);
            if(descriptors)vkDestroyDescriptorPool(owner->device,descriptors,nullptr);
            for(auto pool:overflow_descriptors)vkDestroyDescriptorPool(owner->device,pool,nullptr);
            if(fence)vkDestroyFence(owner->device,fence,nullptr);
            if(upload_command)vkFreeCommandBuffers(owner->device,owner->pool,1,&upload_command);
            if(transfer_command)vkFreeCommandBuffers(owner->device,owner->transfer_pool,1,&transfer_command);
            if(transfer_queries)vkDestroyQueryPool(owner->device,transfer_queries,nullptr);
            if(command)vkFreeCommandBuffers(owner->device,command_pool,1,&command);
            if(command_pool)vkDestroyCommandPool(owner->device,command_pool,nullptr);
            if(map)vkUnmapMemory(owner->device,memory);
            if(data_buffer)vkDestroyBuffer(owner->device,data_buffer,nullptr);
            if(data_memory)vkFreeMemory(owner->device,data_memory,nullptr);
            if(buffer)vkDestroyBuffer(owner->device,buffer,nullptr);
            if(memory)vkFreeMemory(owner->device,memory,nullptr);
        }
    };
public:
    struct GraphicsLayout {
        VkDevice device=VK_NULL_HANDLE;
        VkDescriptorSetLayout descriptors=VK_NULL_HANDLE;VkPipelineLayout layout=VK_NULL_HANDLE;
        ~GraphicsLayout(){if(layout)vkDestroyPipelineLayout(device,layout,nullptr);if(descriptors)vkDestroyDescriptorSetLayout(device,descriptors,nullptr);}
    };
    struct GraphicsPipeline {
        VkDevice device;std::shared_ptr<const std::string> vs_source,fs_source;
        std::shared_ptr<GraphicsLayout> shared_layout;
        std::array<std::string,6> diagnostic_parts;std::string diagnostic_identity;
        std::uint64_t last_use=0;
        VkRenderPass pass=VK_NULL_HANDLE;VkDescriptorSetLayout descriptors=VK_NULL_HANDLE;
        VkPipelineLayout layout=VK_NULL_HANDLE;VkPipeline pipeline=VK_NULL_HANDLE;
        ~GraphicsPipeline(){
            if(pipeline)vkDestroyPipeline(device,pipeline,nullptr);
            if(pass)vkDestroyRenderPass(device,pass,nullptr);
        }
    };
private:
    std::shared_ptr<GraphicsLayout> graphics_layout;
    std::map<std::string,std::shared_ptr<GraphicsPipeline>> graphics_pipelines;
    std::weak_ptr<GraphicsPipeline> last_pipeline_diagnostic;
    std::unordered_map<std::array<std::uint64_t,34>,std::weak_ptr<GraphicsPipeline>,GraphicsKeyHash<34>> graphics_pipeline_fast;
    struct GraphicsShader {std::uint64_t last_use=0;VkDevice device;VkShaderModule module=VK_NULL_HANDLE;~GraphicsShader(){if(module)vkDestroyShaderModule(device,module,nullptr);}};
    std::map<std::string,std::shared_ptr<GraphicsShader>> graphics_shaders;
    VkSampler graphics_sampler=VK_NULL_HANDLE;
    unsigned graphics_recordings=0;
    VkSemaphore graphics_timeline=VK_NULL_HANDLE;
    std::uint64_t graphics_serial=0;
public:
    // TEST HOOK (tests/test_rsx_async_upload.py): makes the next graphics submit fail AFTER its DMA prelude was submitted.
    bool graphics_test_submit_fault=false;
    // TEST HOOK: N>0 makes the Nth graphics submission of the ANYPS3_RSX_GPU_LOOP benchmark fail (earlier iterations are then still pending).
    unsigned graphics_test_loop_fault=0;
    std::uint64_t graphics_busy_ns=0,graphics_profile_frames=0,graphics_completed_batches=0,graphics_prelude_ns=0;
    std::array<std::uint64_t,256> graphics_mark_ns{},graphics_mark_count{};
    std::uint64_t graphics_loop_ns=0,graphics_loop_warm_ns=0,graphics_loop_batches=0;
    static unsigned graphics_gpu_loop(){static const unsigned loop=[]{const char* v=std::getenv("ANYPS3_RSX_GPU_LOOP");return v?unsigned(std::strtoul(v,nullptr,10)):0u;}();return loop;}
private:
    VkPhysicalDeviceFeatures graphics_features{};VkPhysicalDeviceSubgroupProperties graphics_subgroups{};
    VkPhysicalDeviceProperties graphics_properties{};
    // Dynamic state words (graphics_draw): 0-1 viewport extent, 2-5 scissor,
    // 6-11 stencil compare/write/reference front and back, 12-15 blend
    // constants, 16-17 depth bias, 18-19 depth bounds (float bits).
    static void graphics_emit_dynamic(VkCommandBuffer command,const std::array<unsigned,22>& d,const std::array<unsigned,22>* previous){
        auto changed=[&](unsigned first,unsigned count){return !previous || !std::equal(d.begin()+first,d.begin()+first+count,previous->begin()+first);};
        if(changed(0,2)){VkViewport viewport{0,0,float(d[0]),float(d[1]),0,1};vkCmdSetViewport(command,0,1,&viewport);}
        if(changed(2,4)){VkRect2D scissor{{int(d[2]),int(d[3])},{d[4],d[5]}};vkCmdSetScissor(command,0,1,&scissor);}
        for(unsigned face=0;face<2;++face){const auto mask=face?VK_STENCIL_FACE_BACK_BIT:VK_STENCIL_FACE_FRONT_BIT;
            if(changed(6+face*3,1))vkCmdSetStencilCompareMask(command,mask,d[6+face*3]);
            if(changed(7+face*3,1))vkCmdSetStencilWriteMask(command,mask,d[7+face*3]);
            if(changed(8+face*3,1))vkCmdSetStencilReference(command,mask,d[8+face*3]);}
        float f[8];std::memcpy(f,d.data()+12,sizeof(f));
        if(changed(12,4))vkCmdSetBlendConstants(command,f);
        if(changed(16,2))vkCmdSetDepthBias(command,f[4],0,f[5]);
        if(changed(18,2))vkCmdSetDepthBounds(command,f[6],f[7]);
    }
    enum GraphicsMark:unsigned {MarkStart,MarkDraw,MarkPassBegin,MarkPassEnd,MarkBarrier,MarkCopyBuffer,MarkCopyImage,MarkDispatch,MarkClear,MarkFill,MarkTail,MarkOther,MarkCount};
    // Call sites that issue copies/dispatches/barriers tag themselves (GraphicsSite) so the marks attribute them per site.
    enum GraphicsSiteId:unsigned {SiteNone,SiteTexUpload,SiteSurfaceCopy,SiteTransfer,SiteUnpack,SitePack,SiteAspect,SiteDecode,SiteRawBuffer,SiteDeviceUpload,SitePredicate,SitePresent,SiteClear,SiteDeviceCopies,SiteCount};
    static unsigned& graphics_last_kind(){static thread_local unsigned kind=MarkOther;return kind;}
    static unsigned& graphics_site_ref(){static thread_local unsigned site=SiteNone;return site;}
    struct GraphicsSite{unsigned previous;explicit GraphicsSite(unsigned s):previous(graphics_site_ref()){graphics_site_ref()=s;}~GraphicsSite(){graphics_site_ref()=previous;}};
    static void graphics_mark(GraphicsBatch& b,unsigned kind){
        if(!b.marks || b.mark_count>=4096)return;
        vkCmdWriteTimestamp(b.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.marks,b.mark_count++);b.mark_kinds.push_back(graphics_site_ref()*16+kind);
    }
    static void graphics_replay_planes(GraphicsBatch& b){
        for(unsigned plane=1;plane<b.plane_count;++plane){
            VkRenderPassBeginInfo pass{};pass.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;pass.renderPass=b.plane_pass;
            pass.framebuffer=b.plane_framebuffers[plane];pass.renderArea={{0,0},b.plane_extent};
            vkCmdBeginRenderPass(b.command,&pass,VK_SUBPASS_CONTENTS_INLINE);graphics_mark(b,MarkPassBegin);++b.render_passes;::RsxPerfCounters::add(::RsxPerfCounters::render_passes);
            const std::array<unsigned,22>* previous=b.dynamic_valid?&b.dynamic_state:nullptr;
            for(const auto& d:b.plane_draws){
                vkCmdBeginQuery(b.command,b.occlusion,d.query+plane,d.query_flags);
                if(b.bound_graphics_pipeline!=d.pipeline){vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,d.pipeline);b.bound_graphics_pipeline=d.pipeline;++b.pipeline_binds;}
                vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,d.layout,0,1,&d.set,1,&d.offset);
                vkCmdPushConstants(b.command,d.layout,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush),&d.push[plane]);
                graphics_emit_dynamic(b.command,d.dynamic,previous);previous=&d.dynamic;
                if(d.indexed){vkCmdBindIndexBuffer(b.command,b.data_buffer,d.index_offset,VK_INDEX_TYPE_UINT32);vkCmdDrawIndexed(b.command,d.vertices,d.instances,d.first_vertex,0,0);}
                else vkCmdDraw(b.command,d.vertices,d.instances,d.first_vertex,0);
                vkCmdEndQuery(b.command,b.occlusion,d.query+plane);graphics_mark(b,MarkDraw);
            }
            b.dynamic_state=b.plane_draws.back().dynamic;b.dynamic_valid=true;
            vkCmdEndRenderPass(b.command);graphics_mark(b,MarkPassEnd);::RsxPerfCounters::add(::RsxPerfCounters::pass_ends);
        }
        b.plane_draws.clear();b.plane_count=0;
    }
    static void graphics_end_pass(GraphicsBatch& b){
        b.sync();
        if(b.active_framebuffer){vkCmdEndRenderPass(b.command);graphics_mark(b,MarkPassEnd);b.active_framebuffer=VK_NULL_HANDLE;::RsxPerfCounters::add(::RsxPerfCounters::pass_ends);}
        if(!b.plane_draws.empty())graphics_replay_planes(b);
    }
    static void graphics_barrier(GraphicsBatch& b){
        graphics_end_pass(b);graphics_mark(b,graphics_last_kind());graphics_last_kind()=MarkOther;
        VkMemoryBarrier m{};m.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        m.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT|VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_HOST_WRITE_BIT;
        m.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT|VK_PIPELINE_STAGE_HOST_BIT,
            VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,1,&m,0,nullptr,0,nullptr);graphics_mark(b,MarkBarrier);
    }
    void graphics_begin(GraphicsBatch& b){
        if(b.owner!=this || b.failed)throw std::runtime_error("RSX graphics batch owner/failure");
        if(b.recording)return;
        if(b.submitted || b.completion)throw std::runtime_error("RSX graphics begin before retirement/reset");
        if(!b.plane_draws.empty())throw std::runtime_error("RSX graphics deferred MSAA planes outlived their batch");
        VkCommandBufferBeginInfo i{};i.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;i.flags=graphics_gpu_loop()?VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT:VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        check(vkBeginCommandBuffer(b.command,&i),"graphics begin");b.recording=true;++graphics_recordings;
        if(b.queries)vkCmdResetQueryPool(b.command,b.queries,0,4096);
        vkCmdResetQueryPool(b.command,b.occlusion,0,1024);
        if(transfer_queue && b.busy_queries){vkCmdResetQueryPool(b.command,b.busy_queries,0,3);vkCmdWriteTimestamp(b.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.busy_queries,0);}
        if(b.marks){vkCmdResetQueryPool(b.command,b.marks,0,4096);b.mark_count=0;b.mark_kinds.clear();graphics_mark(b,MarkStart);}
    }
    void graphics_stamp(GraphicsBatch& b,unsigned phase,bool end=false){
        b.sync();
        if(!b.queries)return;
        if(b.query_count>=4096)throw std::runtime_error("RSX graphics query budget exhausted; finish batch");
        if(!end)b.phases.push_back(phase);
        vkCmdWriteTimestamp(b.command,end?VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT:VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,b.queries,b.query_count++);
    }
    std::uint64_t graphics_space(GraphicsBatch& b,std::uint64_t bytes){
        const auto alignment=std::max<std::uint64_t>(256,graphics_properties.limits.minStorageBufferOffsetAlignment);
        const auto at=(b.used+alignment-1)&~(alignment-1);
        if(at>b.capacity || bytes>b.capacity-at)throw std::runtime_error("RSX graphics staging exhausted; finish batch");
        b.used=at+bytes;return at;
    }
    std::shared_ptr<GraphicsPipeline> graphics_pipeline(const GraphicsState& s,const std::string& vs,const std::string& fs,const std::shared_ptr<const std::string>& vs_owner={},const std::shared_ptr<const std::string>& fs_owner={},bool multiview=false){
        // The steady path has a bounded, allocation-free exact state key.
        const auto a=shader_identity(vs,vs_owner),b=shader_identity(fs,fs_owner);
        std::array<std::uint64_t,34> fast_key{};fast_key[0]=a;fast_key[1]=b;unsigned field=0;
        auto field_=[&](unsigned v){const auto word=2+field/2;fast_key[word]|=std::uint64_t(v)<<(32*(field%2));++field;};
        field_(s.colors);field_(s.color);field_(s.depth);
        for(unsigned i=0;i<s.colors;++i){const auto& v=s.blend[i];field_(v.blendEnable);field_(v.srcColorBlendFactor);field_(v.dstColorBlendFactor);field_(v.colorBlendOp);field_(v.srcAlphaBlendFactor);field_(v.dstAlphaBlendFactor);field_(v.alphaBlendOp);field_(v.colorWriteMask);}
        field_(s.z.depthTestEnable);field_(s.z.depthWriteEnable);field_(s.z.depthCompareOp);field_(s.z.stencilTestEnable);field_(s.z.depthBoundsTestEnable);
        for(const auto* v:{&s.z.front,&s.z.back}){field_(v->failOp);field_(v->passOp);field_(v->depthFailOp);field_(v->compareOp);}
        field_(s.raster.frontFace);field_(s.raster.cullMode);field_(s.raster.depthBiasEnable);field_(s.logic_enable);field_(s.logic);field_(multiview?s.samples:0);
        auto cached=graphics_pipeline_fast.find(fast_key);
        if(cached!=graphics_pipeline_fast.end())if(auto hit=cached->second.lock()){
            if((hit->vs_source.get()==&vs || *hit->vs_source==vs) && (hit->fs_source.get()==&fs || *hit->fs_source==fs)){
                hit->last_use=++cache_clock;++compile_stats.graphics_hits;last_pipeline_diagnostic=hit;compile_stats_print();return hit;}}
        if(graphics_pipeline_fast.size()>=graphics_pipeline_limit)graphics_pipeline_fast.erase(graphics_pipeline_fast.begin());
        // Cold diagnostics retain the serialized field decomposition.
        std::string key;
        key.append(reinterpret_cast<const char*>(&a),sizeof(a));key.append(reinterpret_cast<const char*>(&b),sizeof(b));
        std::string state_key;auto add=[&](unsigned n){state_key.append(reinterpret_cast<const char*>(&n),sizeof(n));};
        std::array<std::string,6> parts{std::string(reinterpret_cast<const char*>(&a),sizeof(a)),std::string(reinterpret_cast<const char*>(&b),sizeof(b)),{},{},{},{}};
        add(s.colors);add(s.color);add(s.depth);parts[2]=state_key;std::size_t split=state_key.size();
        for(unsigned i=0;i<s.colors;++i){const auto& v=s.blend[i];add(v.blendEnable);add(v.srcColorBlendFactor);add(v.dstColorBlendFactor);
            add(v.colorBlendOp);add(v.srcAlphaBlendFactor);add(v.dstAlphaBlendFactor);add(v.alphaBlendOp);add(v.colorWriteMask);}
        parts[3]=state_key.substr(split);split=state_key.size();
        add(s.z.depthTestEnable);add(s.z.depthWriteEnable);add(s.z.depthCompareOp);add(s.z.stencilTestEnable);add(s.z.depthBoundsTestEnable);
        for(const auto* v:{&s.z.front,&s.z.back}){add(v->failOp);add(v->passOp);add(v->depthFailOp);add(v->compareOp);}
        parts[4]=state_key.substr(split);split=state_key.size();
        add(s.raster.frontFace);add(s.raster.cullMode);add(s.raster.depthBiasEnable);add(s.logic_enable);add(s.logic);add(multiview?s.samples:0);
        parts[5]=state_key.substr(split);
        key+=state_key;auto it=graphics_pipelines.find(key);
        // Hashes accelerate lookup; exact bytes remain authoritative even on a collision.
        if(it!=graphics_pipelines.end() && ((it->second->vs_source.get()!=&vs && *it->second->vs_source!=vs) || (it->second->fs_source.get()!=&fs && *it->second->fs_source!=fs))){
            key+='\0'+vs+'\0'+fs;it=graphics_pipelines.find(key);}
        if(it!=graphics_pipelines.end()){it->second->last_use=++cache_clock;++compile_stats.graphics_hits;last_pipeline_parts=parts;last_pipeline_identity=std::string(reinterpret_cast<const char*>(&a),sizeof(a))+std::string(reinterpret_cast<const char*>(&b),sizeof(b));compile_stats_print();graphics_pipeline_fast[fast_key]=it->second;last_pipeline_diagnostic=it->second;return it->second;}
        ++compile_stats.graphics_misses;::RsxPerfCounters::add(::RsxPerfCounters::pipeline_misses);
        const auto identity=key.substr(0,key.size()-state_key.size());
        if(auto last=last_pipeline_diagnostic.lock()){last_pipeline_parts=last->diagnostic_parts;last_pipeline_identity=last->diagnostic_identity;}
        if(last_pipeline_identity.empty())++compile_stats.miss_cold;
        else if(identity!=last_pipeline_identity)++compile_stats.miss_shader;
        else ++compile_stats.miss_fixed_state;
        const std::array<std::uint64_t*,6> reason_counts{&compile_stats.miss_vs,&compile_stats.miss_fs,&compile_stats.miss_targets,&compile_stats.miss_blend,&compile_stats.miss_depth_stencil,&compile_stats.miss_raster};
        for(unsigned i=0;i<6;++i)if(parts[i]!=last_pipeline_parts[i])++*reason_counts[i];
        last_pipeline_parts=parts;last_pipeline_identity=identity;
        if(graphics_pipelines.size()>=graphics_pipeline_limit){graphics_pipelines.erase(lru_entry(graphics_pipelines));++compile_stats.pipeline_evictions;}
        auto p=std::make_shared<GraphicsPipeline>();p->device=device;p->last_use=++cache_clock;p->vs_source=vs_owner?vs_owner:std::make_shared<const std::string>(vs);p->fs_source=fs_owner?fs_owner:std::make_shared<const std::string>(fs);
        p->diagnostic_parts=parts;p->diagnostic_identity=identity;last_pipeline_diagnostic=p;
        std::vector<VkAttachmentDescription> attachments;
        std::vector<VkAttachmentReference> colors;
        for(unsigned n=0;n<s.colors+(s.depth!=VK_FORMAT_UNDEFINED);++n){
            const bool d=n==s.colors;VkAttachmentDescription a{};a.format=d?s.depth:s.color;a.samples=VK_SAMPLE_COUNT_1_BIT;
            a.loadOp=VK_ATTACHMENT_LOAD_OP_LOAD;a.storeOp=VK_ATTACHMENT_STORE_OP_STORE;
            a.stencilLoadOp=VK_ATTACHMENT_LOAD_OP_LOAD;a.stencilStoreOp=VK_ATTACHMENT_STORE_OP_STORE;
            a.initialLayout=a.finalLayout=VK_IMAGE_LAYOUT_GENERAL;attachments.push_back(a);
            if(!d)colors.push_back({n,VK_IMAGE_LAYOUT_GENERAL});
        }
        VkAttachmentReference depth{s.colors,VK_IMAGE_LAYOUT_GENERAL};
        VkSubpassDescription sub{};sub.pipelineBindPoint=VK_PIPELINE_BIND_POINT_GRAPHICS;sub.colorAttachmentCount=s.colors;
        sub.pColorAttachments=colors.data();sub.pDepthStencilAttachment=s.depth!=VK_FORMAT_UNDEFINED?&depth:nullptr;
        VkSubpassDependency deps[2]{};
        deps[0]={VK_SUBPASS_EXTERNAL,0,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT,
            VK_ACCESS_MEMORY_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT|VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT|VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT,0};
        deps[1]={0,VK_SUBPASS_EXTERNAL,VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
            VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT|VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT,VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT,0};
        VkRenderPassCreateInfo ri{};ri.sType=VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;ri.attachmentCount=attachments.size();ri.pAttachments=attachments.data();
        ri.subpassCount=1;ri.pSubpasses=&sub;ri.dependencyCount=2;ri.pDependencies=deps;
        // Multiview: view n is sample plane n (array layer n of every attachment view).
        const std::uint32_t view_mask=multiview?(1u<<s.samples)-1:0;
        VkRenderPassMultiviewCreateInfo mv{};mv.sType=VK_STRUCTURE_TYPE_RENDER_PASS_MULTIVIEW_CREATE_INFO;mv.subpassCount=1;mv.pViewMasks=&view_mask;
        if(multiview){if(!graphics_multiview || s.samples<2 || s.samples>4)throw std::runtime_error("RSX multiview render pass unsupported");ri.pNext=&mv;}
        check(vkCreateRenderPass(device,&ri,nullptr,&p->pass),"graphics render pass");
        if(!graphics_layout){auto layout=std::make_shared<GraphicsLayout>();layout->device=device;
        std::array<VkDescriptorSetLayoutBinding,38> bind{};
        bind[0]={0,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC,1,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,nullptr};
        for(unsigned i=1;i<=20;++i)bind[i]={i,VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,1,VK_SHADER_STAGE_FRAGMENT_BIT,nullptr};
        for(unsigned i=21;i<37;++i)bind[i]={i,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1,VK_SHADER_STAGE_FRAGMENT_BIT,nullptr};
        bind[37]={37,VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,nullptr};
        VkDescriptorSetLayoutCreateInfo di{};di.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;di.bindingCount=bind.size();di.pBindings=bind.data();
        check(vkCreateDescriptorSetLayout(device,&di,nullptr,&layout->descriptors),"graphics descriptors");
        VkPushConstantRange push{VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,56};
        VkPipelineLayoutCreateInfo li{};li.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;li.setLayoutCount=1;li.pSetLayouts=&layout->descriptors;li.pushConstantRangeCount=1;li.pPushConstantRanges=&push;
        check(vkCreatePipelineLayout(device,&li,nullptr,&layout->layout),"graphics layout");graphics_layout=std::move(layout);}
        p->shared_layout=graphics_layout;p->descriptors=graphics_layout->descriptors;p->layout=graphics_layout->layout;
        VkPipelineShaderStageCreateInfo stages[2]{};std::vector<std::shared_ptr<GraphicsShader>> module_pins;
        for(unsigned i=0;i<2;++i){auto shader_key=std::string(i?"f":"v")+(i?fs:vs);auto cached=graphics_shaders.find(shader_key);
            if(cached==graphics_shaders.end()){
                if(graphics_shaders.size()>=shader_module_limit){graphics_shaders.erase(lru_entry(graphics_shaders));++compile_stats.module_evictions;}
                auto code=compile_glsl(i?fs:vs,i?GLSLANG_STAGE_FRAGMENT:GLSLANG_STAGE_VERTEX,true);
                auto module=std::make_shared<GraphicsShader>();module->device=device;
                VkShaderModuleCreateInfo mi{};mi.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;mi.codeSize=code.size()*4;mi.pCode=code.data();
                const auto module_start=std::chrono::steady_clock::now();
                check(vkCreateShaderModule(device,&mi,nullptr,&module->module),"graphics shader");
                ++compile_stats.shader_modules;compile_stats.module_ns+=elapsed_ns(module_start);
                cached=graphics_shaders.emplace(std::move(shader_key),std::move(module)).first;
            }else ++compile_stats.shader_module_hits;
            cached->second->last_use=++cache_clock;module_pins.push_back(cached->second);stages[i].sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;stages[i].stage=i?VK_SHADER_STAGE_FRAGMENT_BIT:VK_SHADER_STAGE_VERTEX_BIT;stages[i].module=cached->second->module;stages[i].pName="main";}
        VkPipelineVertexInputStateCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
        VkPipelineInputAssemblyStateCreateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;ai.topology=VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
        VkPipelineViewportStateCreateInfo viewport{};viewport.sType=VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;viewport.viewportCount=viewport.scissorCount=1;
        auto raster=s.raster;raster.sType=VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;raster.polygonMode=VK_POLYGON_MODE_FILL;raster.lineWidth=1;
        VkPipelineMultisampleStateCreateInfo ms{};ms.sType=VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;ms.rasterizationSamples=VK_SAMPLE_COUNT_1_BIT;
        auto z=s.z;z.sType=VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
        VkPipelineColorBlendStateCreateInfo cb{};cb.sType=VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;cb.attachmentCount=s.colors;cb.pAttachments=s.blend.data();
        cb.logicOpEnable=s.logic_enable;cb.logicOp=s.logic;
        const VkDynamicState ds[]={VK_DYNAMIC_STATE_VIEWPORT,VK_DYNAMIC_STATE_SCISSOR,VK_DYNAMIC_STATE_BLEND_CONSTANTS,VK_DYNAMIC_STATE_DEPTH_BIAS,VK_DYNAMIC_STATE_DEPTH_BOUNDS,VK_DYNAMIC_STATE_STENCIL_COMPARE_MASK,VK_DYNAMIC_STATE_STENCIL_WRITE_MASK,VK_DYNAMIC_STATE_STENCIL_REFERENCE};
        VkPipelineDynamicStateCreateInfo dy{};dy.sType=VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;dy.dynamicStateCount=sizeof(ds)/sizeof(ds[0]);dy.pDynamicStates=ds;
        VkGraphicsPipelineCreateInfo gi{};gi.sType=VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;gi.stageCount=2;gi.pStages=stages;
        gi.pVertexInputState=&vi;gi.pInputAssemblyState=&ai;gi.pViewportState=&viewport;gi.pRasterizationState=&raster;gi.pMultisampleState=&ms;
        gi.pDepthStencilState=&z;gi.pColorBlendState=&cb;gi.pDynamicState=&dy;gi.layout=p->layout;gi.renderPass=p->pass;
        const auto pipeline_start=std::chrono::steady_clock::now();
        check(vkCreateGraphicsPipelines(device,pipeline_cache,1,&gi,nullptr,&p->pipeline),"vkCreateGraphicsPipelines");
        const auto pipeline_ns=elapsed_ns(pipeline_start);
        if(std::getenv("ANYPS3_RSX_SHADER_METRICS")){std::uint64_t virtual_pages=0,resident_pages=0;std::ifstream statm("/proc/self/statm");statm>>virtual_pages>>resident_pages;
            std::fprintf(stderr,"RSX FS PIPELINE glsl_bytes=%zu pipeline_ms=%.3f rss_bytes=%llu\n",fs.size(),pipeline_ns*1.e-6,(unsigned long long)(resident_pages*std::uint64_t(sysconf(_SC_PAGESIZE))));}
        ++compile_stats.graphics_pipelines;compile_stats.pipeline_ns+=pipeline_ns;pipeline_cache_created();
        save_pipeline_cache();compile_stats_print();
        graphics_pipeline_fast[fast_key]=p;return graphics_pipelines.emplace(std::move(key),std::move(p)).first->second;
    }
public:
    // True when batch preludes run on the dedicated DMA queue (graphics_upload_submissions counts those submissions).
    bool graphics_async_upload()const{return transfer_queue!=VK_NULL_HANDLE;}
    std::uint64_t graphics_upload_submissions()const{return graphics_upload_serial;}
    bool graphics_sample_interpolation()const{return graphics_features.sampleRateShading;}
    bool graphics_depth_bounds()const{return graphics_features.depthBounds;}
    bool graphics_precise_visibility()const{return graphics_features.occlusionQueryPrecise;}
    bool graphics_quad_operations()const{return (graphics_subgroups.supportedStages&VK_SHADER_STAGE_FRAGMENT_BIT) && (graphics_subgroups.supportedOperations&VK_SUBGROUP_FEATURE_QUAD_BIT);}
    bool graphics_fragment_storage()const{return graphics_features.fragmentStoresAndAtomics;}
    bool graphics_independent_blend()const{return graphics_features.independentBlend;}
    bool graphics_logic()const{return graphics_features.logicOp;}
    bool graphics_vertex_fetch()const{return graphics_features.shaderClipDistance && graphics_features.vertexPipelineStoresAndAtomics;}
    bool graphics_float64()const{return graphics_features.shaderFloat64;}
    // ANYPS3_RSX_VK_PROFILE: GPU time per draw by fragment/vertex shader text.
    struct DrawProfile {double ms=0;std::uint64_t draws=0;std::size_t vs_bytes=0,fs_bytes=0;std::string fs_head;};
    std::unordered_map<std::string,DrawProfile> draw_profile;std::uint64_t draw_profile_draws=0;
    void draw_profile_add(const GraphicsPipeline& p,double ms){
        if(!p.vs_source || !p.fs_source)return;
        auto& e=draw_profile[*p.vs_source+'\0'+*p.fs_source];e.ms+=ms;++e.draws;e.vs_bytes=p.vs_source->size();e.fs_bytes=p.fs_source->size();
        if(++draw_profile_draws%20000==0)draw_profile_print();
    }
    void draw_profile_print(){
        std::vector<const DrawProfile*> rows;double total=0;for(const auto& e:draw_profile){rows.push_back(&e.second);total+=e.second.ms;}
        std::sort(rows.begin(),rows.end(),[](const auto* a,const auto* c){return a->ms>c->ms;});
        std::fprintf(stderr,"RSX VK DRAW PROFILE draws=%llu shaders=%zu total_ms=%.3f\n",(unsigned long long)draw_profile_draws,rows.size(),total);
        for(std::size_t i=0;i<rows.size() && i<20;++i){const auto& e=*rows[i];
            for(const auto& kv:draw_profile)if(&kv.second==rows[i]){
                std::fprintf(stderr,"RSX VK DRAW PROFILE rank=%zu ms=%.3f draws=%llu us_per_draw=%.2f vs_bytes=%zu fs_bytes=%zu fs_hash=%016zx\n",i,e.ms,(unsigned long long)e.draws,1000*e.ms/std::max<std::uint64_t>(1,e.draws),e.vs_bytes,e.fs_bytes,std::hash<std::string>{}(kv.first));
                if(const char* dir=std::getenv("ANYPS3_RSX_VK_PROFILE_DIR")){char name[64];std::snprintf(name,sizeof(name),"/shader-%016zx.glsl",std::hash<std::string>{}(kv.first));
                    if(FILE* f=std::fopen((std::string(dir)+name).c_str(),"w")){std::fwrite(kv.first.data(),1,kv.first.size(),f);std::fclose(f);}}
                break;}}
    }
    std::uint64_t graphics_draws=0;bool graphics_layered_output=false,graphics_multiview=false;unsigned graphics_last_queries=1;
    // All sample planes as one layered attachment (same aspects as the per-plane views).
    VkImageView graphics_attachment_array(GraphicsImage& image){
        if(image.attachment_array)return image.attachment_array;
        const VkImageAspectFlags aspect=image.depth?VK_IMAGE_ASPECT_DEPTH_BIT|(image.format==VK_FORMAT_D24_UNORM_S8_UINT?VK_IMAGE_ASPECT_STENCIL_BIT:0):VK_IMAGE_ASPECT_COLOR_BIT;
        VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=image.image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D_ARRAY;vi.format=image.format;
        vi.subresourceRange={aspect,0,1,0,image.layers};check(vkCreateImageView(device,&vi,nullptr,&image.attachment_array),"graphics layered attachment view");return image.attachment_array;
    }
    std::size_t graphics_cache_size()const{return graphics_pipelines.size();}
    std::uint64_t graphics_storage_limit()const{return graphics_properties.limits.maxStorageBufferRange;}
    // Physical-device format properties are immutable for this device.
    mutable std::array<VkFormatFeatureFlags,256> graphics_format_flags{};
    mutable std::array<bool,256> graphics_format_known{};
    bool graphics_format(VkFormat f,bool depth=false)const{
        VkFormatFeatureFlags flags=0;
        if(unsigned(f)<graphics_format_flags.size() && graphics_format_known[f])flags=graphics_format_flags[f];
        else{VkFormatProperties p{};vkGetPhysicalDeviceFormatProperties(physical,f,&p);flags=p.optimalTilingFeatures;
            if(unsigned(f)<graphics_format_flags.size()){graphics_format_flags[f]=flags;graphics_format_known[f]=true;}}
        return (flags&(depth?VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT:VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT))!=0;
    }
    std::unique_ptr<GraphicsBatch> graphics_batch(std::uint64_t bytes=64*1024*1024){
        auto b=std::make_unique<GraphicsBatch>(this);b->capacity=bytes;
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=bytes;bi.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_INDEX_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        // Both queue families touch these two buffers (prelude on the DMA queue, batch on the graphics queue); no ownership transfer needed.
        const std::uint32_t sharing_families[2]={family,transfer_family};
        if(transfer_queue){bi.sharingMode=VK_SHARING_MODE_CONCURRENT;bi.queueFamilyIndexCount=2;bi.pQueueFamilyIndices=sharing_families;}
        check(vkCreateBuffer(device,&bi,nullptr,&b->buffer),"graphics staging buffer");
        VkMemoryRequirements r{};vkGetBufferMemoryRequirements(device,b->buffer,&r);
        VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=host_read_memory_type(r.memoryTypeBits);
        b->coherent=memory.memoryTypes[ai.memoryTypeIndex].propertyFlags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        check(vkAllocateMemory(device,&ai,nullptr,&b->memory),"graphics staging memory");check(vkBindBufferMemory(device,b->buffer,b->memory,0),"graphics staging bind");
        check(vkMapMemory(device,b->memory,0,VK_WHOLE_SIZE,0,&b->map),"graphics staging map");
        check(vkCreateBuffer(device,&bi,nullptr,&b->data_buffer),"graphics device buffer");
        vkGetBufferMemoryRequirements(device,b->data_buffer,&r);ai.allocationSize=r.size;ai.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        check(vkAllocateMemory(device,&ai,nullptr,&b->data_memory),"graphics device memory");check(vkBindBufferMemory(device,b->data_buffer,b->data_memory,0),"graphics device buffer bind");
        VkCommandBufferAllocateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;ci.commandPool=pool;ci.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ci.commandBufferCount=1;
        {VkCommandPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;pi.queueFamilyIndex=family;pi.flags=VK_COMMAND_POOL_CREATE_TRANSIENT_BIT|VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
            check(vkCreateCommandPool(device,&pi,nullptr,&b->command_pool),"graphics batch command pool");}
        ci.commandPool=b->command_pool;check(vkAllocateCommandBuffers(device,&ci,&b->command),"graphics command buffer");ci.commandPool=pool;
        check(vkAllocateCommandBuffers(device,&ci,&b->upload_command),"graphics upload command buffer");
        if(transfer_queue){ci.commandPool=transfer_pool;check(vkAllocateCommandBuffers(device,&ci,&b->transfer_command),"graphics transfer command buffer");ci.commandPool=pool;}
        VkFenceCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;check(vkCreateFence(device,&fi,nullptr,&b->fence),"graphics fence");
        b->descriptors=graphics_descriptor_pool();
        VkQueryPoolCreateInfo oi{};oi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;oi.queryType=VK_QUERY_TYPE_OCCLUSION;oi.queryCount=1024;
        check(vkCreateQueryPool(device,&oi,nullptr,&b->occlusion),"graphics visibility pool");
        unsigned n=0;vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,nullptr);std::vector<VkQueueFamilyProperties> queues(n);vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,queues.data());
        b->timestamp_bits=queues[family].timestampValidBits;b->timestamp_period=graphics_properties.limits.timestampPeriod;
        if(b->timestamp_bits && std::getenv("ANYPS3_RSX_VK_PROFILE")){VkQueryPoolCreateInfo qi{};qi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;qi.queryType=VK_QUERY_TYPE_TIMESTAMP;qi.queryCount=4096;
            check(vkCreateQueryPool(device,&qi,nullptr,&b->queries),"graphics timestamps");}
        if(b->timestamp_bits && (std::getenv("ANYPS3_RSX_VK_PROFILE") || std::getenv("ANYPS3_PERF_REPORT"))){VkQueryPoolCreateInfo qi{};qi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;qi.queryType=VK_QUERY_TYPE_TIMESTAMP;qi.queryCount=3;
            check(vkCreateQueryPool(device,&qi,nullptr,&b->busy_queries),"graphics busy timestamps");
            if(transfer_queue && graphics_host_query_reset){qi.queryCount=2;check(vkCreateQueryPool(device,&qi,nullptr,&b->transfer_queries),"graphics transfer timestamps");vkResetQueryPool(device,b->transfer_queries,0,2);}}
        if(b->timestamp_bits && std::getenv("ANYPS3_RSX_VK_MARKS")){VkQueryPoolCreateInfo qi{};qi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;qi.queryType=VK_QUERY_TYPE_TIMESTAMP;qi.queryCount=4096;
            check(vkCreateQueryPool(device,&qi,nullptr,&b->marks),"graphics mark timestamps");}
        return b;
    }
    std::uint64_t graphics_image_creations=0;
    std::shared_ptr<GraphicsImage> graphics_image(GraphicsBatch& b,VkFormat f,unsigned w,unsigned h,unsigned layers,bool depth=false){b.sync();
        if(!w || !h || !layers || layers>4 || !graphics_format(f,depth))throw std::runtime_error("unsupported graphics image format/extent");
        ++graphics_image_creations;auto image=std::make_shared<GraphicsImage>(this,f,w,h,layers,depth);
        VkImageCreateInfo ii{};ii.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ii.imageType=VK_IMAGE_TYPE_2D;ii.format=f;ii.extent={w,h,1};ii.mipLevels=1;ii.arrayLayers=layers;
        ii.samples=VK_SAMPLE_COUNT_1_BIT;ii.tiling=VK_IMAGE_TILING_OPTIMAL;ii.usage=VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT|(depth?VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT:VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)|VK_IMAGE_USAGE_SAMPLED_BIT;
        check(vkCreateImage(device,&ii,nullptr,&image->image),"graphics image");
        VkMemoryRequirements r{};vkGetImageMemoryRequirements(device,image->image,&r);VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        image->memory_bytes=r.size;check(vkAllocateMemory(device,&ai,nullptr,&image->memory),"graphics image memory");check(vkBindImageMemory(device,image->image,image->memory,0),"graphics image bind");
        const VkImageAspectFlags aspect=depth?VK_IMAGE_ASPECT_DEPTH_BIT|(f==VK_FORMAT_D24_UNORM_S8_UINT?VK_IMAGE_ASPECT_STENCIL_BIT:0):VK_IMAGE_ASPECT_COLOR_BIT;
        for(unsigned layer=0;layer<layers;++layer){VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=image->image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D;vi.format=f;
            vi.subresourceRange={aspect,0,1,layer,1};VkImageView view;check(vkCreateImageView(device,&vi,nullptr,&view),"graphics view");image->views.push_back(view);}
        {VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=image->image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D_ARRAY;vi.format=f;
            vi.subresourceRange={depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,layers};check(vkCreateImageView(device,&vi,nullptr,&image->array_view),"graphics array view");}
        graphics_begin(b);graphics_end_pass(b);VkImageMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;barrier.oldLayout=VK_IMAGE_LAYOUT_UNDEFINED;barrier.newLayout=VK_IMAGE_LAYOUT_GENERAL;
        barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.image=image->image;barrier.subresourceRange={aspect,0,1,0,layers};barrier.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,0,nullptr,0,nullptr,1,&barrier);
        b.pins.push_back(image);return image;
    }
    // Host bytes copied into the device data buffer at their command-order
    // position (not the batch prelude): later commands of this batch may
    // overwrite them on the device before the consumer runs.
    std::uint64_t graphics_device_upload(GraphicsBatch& b,const void* data,std::uint64_t bytes){b.sync();
        GraphicsSite site(SiteDeviceUpload);
        const auto at=graphics_upload(b,data,bytes);graphics_barrier(b);
        VkBufferCopy copy{at,at,bytes};vkCmdCopyBuffer(b.command,b.buffer,b.data_buffer,1,&copy);graphics_barrier(b);return at;
    }
    void graphics_device_copies(GraphicsBatch& b,const std::vector<VkBufferCopy>& regions){b.sync();
        if(regions.empty())return;
        GraphicsSite site(SiteDeviceCopies);
        graphics_begin(b);graphics_barrier(b);vkCmdCopyBuffer(b.command,b.data_buffer,b.data_buffer,unsigned(regions.size()),regions.data());graphics_barrier(b);
    }
    // Immutable slice filled in place by the caller (staging map + offset);
    // the batch prelude copies it to the device buffer before any consumer.
    std::uint64_t graphics_reserve_upload(GraphicsBatch& b,std::uint64_t bytes){
        graphics_begin(b);const auto at=graphics_space(b,bytes);b.uploads.push_back({at,at,bytes});b.upload_bytes+=bytes;
        ::RsxPerfCounters::add(::RsxPerfCounters::upload_bytes,bytes);return at;
    }
    std::uint64_t graphics_upload(GraphicsBatch& b,const void* data,std::uint64_t bytes){
        graphics_begin(b);const auto at=graphics_space(b,bytes);std::memcpy(static_cast<char*>(b.map)+at,data,bytes);b.upload_bytes+=bytes;::RsxPerfCounters::add(::RsxPerfCounters::upload_bytes,bytes);return at;
    }
    struct GraphicsStateBlock {std::uint64_t version;const unsigned* data;unsigned words;};
    template<std::size_t N> void graphics_state_blocks(GraphicsBatch& b,const std::array<GraphicsStateBlock,N>& blocks,unsigned first=0){
        static_assert(N<=GraphicsBatch::state_slots,"RSX state block slots");
        if(first+N>GraphicsBatch::state_slots)throw std::runtime_error("RSX state block slot range");
        for(unsigned index=0;index<blocks.size();++index){const auto& block=blocks[index];const unsigned slot=first+index;
            if(!block.version || !block.words)throw std::runtime_error("RSX invalid immutable state block");
            if(b.state_versions[slot]==block.version){++b.state_block_hits;continue;}
            const auto size=std::uint64_t(block.words)*4,at=graphics_upload(b,block.data,size);
            b.state_versions[slot]=block.version;b.state_offsets[slot]=at/4;++b.state_block_uploads;::RsxPerfCounters::add(::RsxPerfCounters::state_uploads);
            b.uploads.push_back({at,at,size});
        }
    }
    std::shared_ptr<GraphicsBuffer> graphics_buffer(GraphicsBatch& b,const void* data,std::uint64_t size){b.sync();
        GraphicsSite site(SiteRawBuffer);
        const auto bytes=(size+3)&~UINT64_C(3);
        if(!bytes || bytes>graphics_properties.limits.maxStorageBufferRange)throw std::runtime_error("RSX texture buffer storage limit");
        auto result=std::make_shared<GraphicsBuffer>(this,bytes);
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=bytes;bi.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        check(vkCreateBuffer(device,&bi,nullptr,&result->buffer),"graphics raw buffer");
        VkMemoryRequirements r{};vkGetBufferMemoryRequirements(device,result->buffer,&r);VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        result->memory_bytes=r.size;check(vkAllocateMemory(device,&ai,nullptr,&result->memory),"graphics raw memory");check(vkBindBufferMemory(device,result->buffer,result->memory,0),"graphics raw bind");
        graphics_begin(b);const auto at=graphics_space(b,bytes);std::memset(static_cast<char*>(b.map)+at,0,bytes);std::memcpy(static_cast<char*>(b.map)+at,data,size);b.upload_bytes+=bytes;
        graphics_barrier(b);VkBufferCopy copy{at,0,bytes};vkCmdCopyBuffer(b.command,b.buffer,result->buffer,1,&copy);graphics_barrier(b);b.buffer_pins.push_back(result);return result;
    }
''' + READBACK_MEMBERS + UPLOAD_MEMBERS + DECODE_MEMBERS + r'''
    // Images stay GENERAL, allowing explicit cross-draw transfer barriers.
    std::uint64_t graphics_transfer(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,VkImageAspectFlags aspect,unsigned bpp,const void* data=nullptr){b.sync();
        GraphicsSite site(SiteTransfer);
        graphics_begin(b);const auto bytes=std::uint64_t(image->width)*image->height*image->layers*bpp;
        const auto at=data?graphics_upload(b,data,bytes):graphics_space(b,bytes);
        graphics_barrier(b);VkBufferImageCopy copy{};copy.bufferOffset=at;copy.imageSubresource={aspect,0,0,image->layers};copy.imageExtent={image->width,image->height,1};
        graphics_stamp(b,data?0:2);
        if(data)vkCmdCopyBufferToImage(b.command,b.buffer,image->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);
        else {vkCmdCopyImageToBuffer(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,b.buffer,1,&copy);b.download_bytes+=bytes;}
        graphics_stamp(b,0,true);b.pins.push_back(image);return at;
    }
    std::shared_ptr<GraphicsImage> graphics_aspect_image(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& source,VkImageAspectFlags aspect,const std::shared_ptr<GraphicsImage>& destination={},std::shared_ptr<GraphicsBuffer>* scratch=nullptr){b.sync();
        GraphicsSite site(SiteAspect);
        const bool depth=source->depth && source->format==VK_FORMAT_D24_UNORM_S8_UINT && (aspect==VK_IMAGE_ASPECT_DEPTH_BIT || aspect==VK_IMAGE_ASPECT_STENCIL_BIT);
        const bool color=!source->depth && aspect==VK_IMAGE_ASPECT_COLOR_BIT && (source->format==VK_FORMAT_R16G16B16A16_SFLOAT || source->format==VK_FORMAT_R32G32B32A32_SFLOAT || source->format==VK_FORMAT_R32_SFLOAT);
        if(!depth && !color)throw std::runtime_error("unsupported RSX typeless aspect copy");
        const unsigned bpp=color?(source->format==VK_FORMAT_R16G16B16A16_SFLOAT?8:source->format==VK_FORMAT_R32G32B32A32_SFLOAT?16:4):aspect==VK_IMAGE_ASPECT_DEPTH_BIT?4:1;
        const unsigned words=color?bpp/4:1;
        if(std::uint64_t(source->width)*words>graphics_properties.limits.maxImageDimension2D)throw std::runtime_error("RSX typeless image word extent exceeds device limit");
        const auto bytes=std::uint64_t(source->width)*source->height*source->layers*bpp;
        const auto format=bpp==1?VK_FORMAT_R8_UINT:VK_FORMAT_R32_UINT;
        auto result=destination?destination:graphics_image(b,format,source->width*words,source->height,source->layers);
        if(result->format!=format || result->width!=source->width*words || result->height!=source->height || result->layers!=source->layers || result->depth)throw std::runtime_error("RSX cached aspect destination mismatch");
        // A device-local transfer buffer permits byte-preserving depth/color
        // reinterpretation without an illegal cross-aspect vkCmdCopyImage,
        // normalized float reconstruction or any host-visible staging/read.
        auto transfer=scratch?*scratch:std::shared_ptr<GraphicsBuffer>{};
        if(transfer && transfer->size!=bytes)throw std::runtime_error("RSX cached aspect buffer mismatch");
        if(!transfer){transfer=std::make_shared<GraphicsBuffer>(this,bytes);
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=bytes;bi.usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        check(vkCreateBuffer(device,&bi,nullptr,&transfer->buffer),"graphics typeless buffer");
        VkMemoryRequirements requirements{};vkGetBufferMemoryRequirements(device,transfer->buffer,&requirements);
        VkMemoryAllocateInfo allocation{};allocation.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;allocation.allocationSize=requirements.size;allocation.memoryTypeIndex=memory_type(requirements.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        transfer->memory_bytes=requirements.size;check(vkAllocateMemory(device,&allocation,nullptr,&transfer->memory),"graphics typeless memory");check(vkBindBufferMemory(device,transfer->buffer,transfer->memory,0),"graphics typeless bind");if(scratch)*scratch=transfer;}
        graphics_begin(b);graphics_barrier(b);VkBufferImageCopy copy{};copy.imageSubresource={aspect,0,0,source->layers};copy.imageExtent={source->width,source->height,1};
        vkCmdCopyImageToBuffer(b.command,source->image,VK_IMAGE_LAYOUT_GENERAL,transfer->buffer,1,&copy);graphics_barrier(b);
        copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_COLOR_BIT;copy.imageExtent.width=result->width;
        vkCmdCopyBufferToImage(b.command,transfer->buffer,result->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);graphics_barrier(b);
        b.buffer_pins.push_back(transfer);b.pins.push_back(source);b.pins.push_back(result);return result;
    }
    std::uint64_t graphics_full_clears=0;
    void graphics_clear_image(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,const VkClearColorValue& color,float depth,unsigned stencil,VkImageAspectFlags aspects){b.sync();
        GraphicsSite site(SiteClear);
        graphics_begin(b);graphics_barrier(b);
        const VkImageSubresourceRange range{aspects,0,1,0,image->layers};
        if(image->depth){const VkClearDepthStencilValue value{depth,stencil};vkCmdClearDepthStencilImage(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,&value,1,&range);}
        else vkCmdClearColorImage(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,&color,1,&range);
        b.pins.push_back(image);++graphics_full_clears;
    }
    void graphics_copy(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& from,const std::shared_ptr<GraphicsImage>& to){b.sync();
        if(from==to)return;
        GraphicsSite site(SiteSurfaceCopy);
        const bool rgba_compatible=(from->format==VK_FORMAT_R8G8B8A8_SRGB && to->format==VK_FORMAT_R8G8B8A8_UNORM) ||
            (to->format==VK_FORMAT_R8G8B8A8_SRGB && from->format==VK_FORMAT_R8G8B8A8_UNORM);
        if((from->format!=to->format && !rgba_compatible) || from->width!=to->width || from->height!=to->height || from->layers!=to->layers || from->depth!=to->depth)
            throw std::runtime_error("RSX graphics copy shape mismatch");
        graphics_begin(b);graphics_barrier(b);
        for(const VkImageAspectFlags aspect:from->depth?std::vector<VkImageAspectFlags>{VK_IMAGE_ASPECT_DEPTH_BIT}:std::vector<VkImageAspectFlags>{VK_IMAGE_ASPECT_COLOR_BIT}){
            VkImageCopy c{};c.srcSubresource={aspect,0,0,from->layers};c.dstSubresource={aspect,0,0,to->layers};c.extent={from->width,from->height,1};
            vkCmdCopyImage(b.command,from->image,VK_IMAGE_LAYOUT_GENERAL,to->image,VK_IMAGE_LAYOUT_GENERAL,1,&c);}
        if(from->format==VK_FORMAT_D24_UNORM_S8_UINT){VkImageCopy c{};c.srcSubresource={VK_IMAGE_ASPECT_STENCIL_BIT,0,0,from->layers};c.dstSubresource={VK_IMAGE_ASPECT_STENCIL_BIT,0,0,to->layers};c.extent={from->width,from->height,1};
            vkCmdCopyImage(b.command,from->image,VK_IMAGE_LAYOUT_GENERAL,to->image,VK_IMAGE_LAYOUT_GENERAL,1,&c);}
        b.pins.push_back(from);b.pins.push_back(to);
    }
    // ---- asynchronous draw recorder (ANYPS3_RSX_RECORDER=1) ----
    // graphics_draw is split into a front half (pipeline lookup, staging copy, pins, query/status accounting; RSX worker thread) and a back
    // half (graphics_record_draw: descriptor set, framebuffer, render pass, dynamic state, draw; recorder thread, FIFO). Batch state is
    // partitioned by owner: the front half owns recording/used/capacity/map/uploads/state_*/pins/buffer_pins/pipeline_pins/status_offsets/
    // visibility_count/draws and the staging bytes; the recorder owns the command buffer, descriptor cache and pools, framebuffers,
    // framebuffer_pins, active_framebuffer, bound pipeline, dynamic state, plane runs, timestamps and marks. Every other entry point that
    // receives a GraphicsBatch calls b.sync() first, which waits until all queued draws are recorded (and rethrows a recorder failure);
    // the frontend-only entry points (graphics_begin of a fresh batch, graphics_space/upload/reserve_upload/state_blocks) do not.
    // Each batch records into its own command pool, so resetting/freeing another batch never races with the recorder.
    struct DrawJob {
        GraphicsBatch* batch=nullptr;std::shared_ptr<GraphicsPipeline> pipeline;GraphicsState s;
        std::array<std::shared_ptr<GraphicsImage>,4> colors;std::shared_ptr<GraphicsImage> depth;
        std::array<std::shared_ptr<GraphicsImage>,20> textures;std::array<std::shared_ptr<GraphicsBuffer>,16> raw_textures;
        std::array<std::array<float,2>,4> positions{};
        float bias_constant=0,bias_slope=0,min_bounds=0,max_bounds=1;
        unsigned vertices=0,first_vertex=0,query=0,passes=1;bool indexed=false,layered=false,multiview=false,defer_planes=false;
        std::uint64_t at=0,range=0,index_offset=0;
        void reset(){batch=nullptr;pipeline.reset();depth.reset();for(auto& x:colors)x.reset();for(auto& x:textures)x.reset();for(auto& x:raw_textures)x.reset();}
    };
    static bool& in_recorder_thread(){static thread_local bool recorder=false;return recorder;}
    static void recorder_relax(){
#if defined(__x86_64__) || defined(__i386__)
        __builtin_ia32_pause();
#else
        std::this_thread::yield();
#endif
    }
    // TEST HOOK (tests/test_rsx_recorder.py): the Nth recorded draw (0 = off) throws on the recorder thread.
    std::atomic<unsigned> graphics_test_recorder_fault{0};
    std::uint64_t recorder_clock=0;   // LRU stamps of the batch-local caches, recorder-thread only
    struct DrawRecorder {
        static constexpr std::size_t slots=256;
        NativeVulkan* owner;std::vector<DrawJob> ring=std::vector<DrawJob>(slots);
        alignas(64) std::atomic<std::uint64_t> produced{0};alignas(64) std::atomic<std::uint64_t> consumed{0};alignas(64) std::atomic<int> waiters{0};
        std::atomic<bool> sleeping{false},stopping{false},failed{false};
        std::mutex mutex;std::condition_variable wake,done;std::exception_ptr error;std::uint64_t recorded=0;std::thread thread;
        explicit DrawRecorder(NativeVulkan* o):owner(o){thread=std::thread([this]{run();});}
        ~DrawRecorder(){
            {std::lock_guard<std::mutex> lock(mutex);stopping.store(true,std::memory_order_seq_cst);}
            wake.notify_all();if(thread.joinable())thread.join();
        }
        [[noreturn]] void rethrow(){std::exception_ptr e;{std::lock_guard<std::mutex> lock(mutex);e=error;}std::rethrow_exception(e);}
        void run(){
            in_recorder_thread()=true;::pthread_setname_np(::pthread_self(),"rsx-record");
            for(;;){
                const auto c=consumed.load(std::memory_order_relaxed);
                if(produced.load(std::memory_order_acquire)>c){
                    auto& job=ring[c%slots];
                    if(!failed.load(std::memory_order_acquire)){
                        try{
                            const auto fault=owner->graphics_test_recorder_fault.load(std::memory_order_relaxed);
                            if(fault && ++recorded==fault)throw std::runtime_error("RSX draw recorder test fault");
                            owner->graphics_record_draw(job);
                        }catch(...){{std::lock_guard<std::mutex> lock(mutex);error=std::current_exception();}failed.store(true,std::memory_order_release);}
                    }
                    auto* batch=job.batch;job.reset();
                    if(batch)batch->pending_jobs.fetch_sub(1,std::memory_order_release);
                    consumed.store(c+1,std::memory_order_release);
                    std::atomic_thread_fence(std::memory_order_seq_cst);
                    if(waiters.load(std::memory_order_relaxed)>0){std::lock_guard<std::mutex> lock(mutex);done.notify_all();}
                    continue;
                }
                if(stopping.load(std::memory_order_acquire))return;
                bool found=false;
                for(unsigned spin=0;spin<4000;++spin){recorder_relax();if(produced.load(std::memory_order_acquire)>c){found=true;break;}}
                if(found)continue;
                std::unique_lock<std::mutex> lock(mutex);
                sleeping.store(true,std::memory_order_seq_cst);std::atomic_thread_fence(std::memory_order_seq_cst);
                wake.wait(lock,[&]{return produced.load(std::memory_order_acquire)>c || stopping.load(std::memory_order_acquire);});
                sleeping.store(false,std::memory_order_relaxed);
            }
        }
    };
    std::unique_ptr<DrawRecorder> draw_recorder;
    bool recorder_failed()const{return draw_recorder && draw_recorder->failed.load(std::memory_order_acquire);}
    void recorder_start(){
        const char* v=std::getenv("ANYPS3_RSX_RECORDER");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("invalid ANYPS3_RSX_RECORDER");
        if(v && !std::strcmp(v,"1") && !graphics_gpu_loop())draw_recorder=std::make_unique<DrawRecorder>(this);
    }
    // Waits until every queued draw is recorded. No-op on the recorder thread and without a recorder.
    void recorder_drain(){
        if(!draw_recorder || in_recorder_thread())return;
        auto& r=*draw_recorder;const auto target=r.produced.load(std::memory_order_acquire);
        if(r.consumed.load(std::memory_order_acquire)<target){
            for(unsigned spin=0;spin<4000 && r.consumed.load(std::memory_order_acquire)<target;++spin)recorder_relax();
            if(r.consumed.load(std::memory_order_acquire)<target){
                std::unique_lock<std::mutex> lock(r.mutex);
                r.waiters.fetch_add(1,std::memory_order_seq_cst);std::atomic_thread_fence(std::memory_order_seq_cst);
                r.done.wait(lock,[&]{return r.consumed.load(std::memory_order_acquire)>=target;});
                r.waiters.fetch_sub(1,std::memory_order_relaxed);
            }
        }
        if(r.failed.load(std::memory_order_acquire))r.rethrow();
    }
    // Same wait without reporting a recorder failure (destructors, teardown).
    void recorder_quiesce()noexcept{
        if(!draw_recorder || in_recorder_thread())return;
        auto& r=*draw_recorder;const auto target=r.produced.load(std::memory_order_acquire);
        std::unique_lock<std::mutex> lock(r.mutex);
        r.waiters.fetch_add(1,std::memory_order_seq_cst);std::atomic_thread_fence(std::memory_order_seq_cst);
        r.done.wait(lock,[&]{return r.consumed.load(std::memory_order_acquire)>=target;});
        r.waiters.fetch_sub(1,std::memory_order_relaxed);
    }
    void recorder_stop()noexcept{if(draw_recorder){recorder_quiesce();draw_recorder.reset();}}
    DrawJob& recorder_slot(){
        auto& r=*draw_recorder;if(r.failed.load(std::memory_order_acquire))r.rethrow();
        const auto n=r.produced.load(std::memory_order_relaxed);
        for(unsigned spin=0;n-r.consumed.load(std::memory_order_acquire)>=DrawRecorder::slots;++spin){
            if(spin<2000){recorder_relax();continue;}
            std::unique_lock<std::mutex> lock(r.mutex);
            r.waiters.fetch_add(1,std::memory_order_seq_cst);std::atomic_thread_fence(std::memory_order_seq_cst);
            r.done.wait(lock,[&]{return n-r.consumed.load(std::memory_order_acquire)<DrawRecorder::slots;});
            r.waiters.fetch_sub(1,std::memory_order_relaxed);
            if(r.failed.load(std::memory_order_acquire))r.rethrow();
        }
        return r.ring[n%DrawRecorder::slots];
    }
    void recorder_publish(){
        auto& r=*draw_recorder;r.produced.store(r.produced.load(std::memory_order_relaxed)+1,std::memory_order_release);
        std::atomic_thread_fence(std::memory_order_seq_cst);
        if(r.sleeping.load(std::memory_order_relaxed)){std::lock_guard<std::mutex> lock(r.mutex);r.wake.notify_one();}
    }
    // Back half of graphics_draw: descriptor set, framebuffer, render pass, dynamic state and the draw itself. Runs inline or on the
    // draw recorder thread; it only touches recorder-owned batch state (see DrawJob) and immutable objects.
    void graphics_record_draw(DrawJob& j){
        auto& b=*j.batch;auto& p=*j.pipeline;const auto& s=j.s;const auto& colors=j.colors;const auto& depth=j.depth;const auto& textures=j.textures;const auto& raw_textures=j.raw_textures;
        const auto& positions=j.positions;const float bias_constant=j.bias_constant,bias_slope=j.bias_slope,min_bounds=j.min_bounds,max_bounds=j.max_bounds;
        const unsigned vertices=j.vertices,first_vertex=j.first_vertex,query=j.query,passes=j.passes;const bool layered=j.layered,multiview=j.multiview,defer_planes=j.defer_planes;
        const std::uint64_t at=j.at,range=j.range;
        auto t=std::chrono::steady_clock::now();
        typename GraphicsBatch::DescriptorKey descriptor_key{};descriptor_key[0]=reinterpret_cast<std::uint64_t>(p.descriptors);descriptor_key[1]=range;
        for(unsigned u=0;u<20;++u)descriptor_key[2+u]=textures[u]?reinterpret_cast<std::uint64_t>(u>=16?textures[u]->array_view:textures[u]->views[0]):0;
        for(unsigned u=0;u<16;++u)descriptor_key[22+u]=raw_textures[u]?reinterpret_cast<std::uint64_t>(raw_textures[u]->buffer):0;
        auto cached_set=b.descriptor_cache.find(descriptor_key);VkDescriptorSet set;
        if(cached_set!=b.descriptor_cache.end()){set=cached_set->second.set;cached_set->second.last_use=++recorder_clock;++b.descriptor_hits;}
        else{++b.descriptor_misses;
        set=graphics_descriptor_set(b,p.descriptors,"graphics draw descriptor");
        // One update call carries every binding (same writes, same order).
        std::array<VkWriteDescriptorSet,38> writes{};std::array<VkDescriptorBufferInfo,18> buffers{};std::array<VkDescriptorImageInfo,20> images{};unsigned n=0,nb=0,ni=0;
        buffers[nb]={b.data_buffer,0,range};writes[n].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[n].dstSet=set;writes[n].dstBinding=0;writes[n].descriptorCount=1;writes[n].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC;writes[n].pBufferInfo=&buffers[nb++];++n;
        buffers[nb]={b.data_buffer,0,b.capacity};writes[n].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[n].dstSet=set;writes[n].dstBinding=37;writes[n].descriptorCount=1;writes[n].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;writes[n].pBufferInfo=&buffers[nb++];++n;
        for(unsigned unit=0;unit<20;++unit)if(textures[unit]){
            if(!graphics_sampler){VkSamplerCreateInfo sampler{};sampler.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
                sampler.magFilter=sampler.minFilter=VK_FILTER_NEAREST;sampler.mipmapMode=VK_SAMPLER_MIPMAP_MODE_NEAREST;
                sampler.addressModeU=sampler.addressModeV=sampler.addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
                check(vkCreateSampler(device,&sampler,nullptr,&graphics_sampler),"graphics sampler");}
            images[ni]={textures[unit]->sampler?textures[unit]->sampler:graphics_sampler,unit>=16?textures[unit]->array_view:textures[unit]->views[0],VK_IMAGE_LAYOUT_GENERAL};
            writes[n].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[n].dstSet=set;writes[n].dstBinding=unit+1;
            writes[n].descriptorCount=1;writes[n].descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;writes[n].pImageInfo=&images[ni++];++n;
        }
        for(unsigned unit=0;unit<16;++unit)if(raw_textures[unit]){
            buffers[nb]={raw_textures[unit]->buffer,0,raw_textures[unit]->size};
            writes[n].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[n].dstSet=set;writes[n].dstBinding=21+unit;writes[n].descriptorCount=1;writes[n].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;writes[n].pBufferInfo=&buffers[nb++];++n;}
        ::RsxPerfCounters::add(::RsxPerfCounters::descriptor_writes,n);vkUpdateDescriptorSets(device,n,writes.data(),0,nullptr);
        if(b.descriptor_cache.size()>=128){auto old=std::min_element(b.descriptor_cache.begin(),b.descriptor_cache.end(),[](const auto& a,const auto& b){return a.second.last_use<b.second.last_use;});b.descriptor_cache.erase(old);}
        // Descriptor storage stays in the bounded pool until the batch fence.
        b.descriptor_cache.emplace(std::move(descriptor_key),typename GraphicsBatch::DescriptorEntry{set,++recorder_clock});
        }
        graphics_stamp(b,1);if(b.queries){b.phase_pipelines.resize(b.phases.size(),nullptr);b.phase_pipelines.back()=&p;}
        typename GraphicsBatch::PlaneDraw deferred{};std::array<VkFramebuffer,4> plane_fb{};
        for(unsigned layer=0;layer<passes;++layer){
            std::array<VkImageView,5> views{};unsigned view_count=0;for(unsigned i=0;i<s.colors;++i)views[view_count++]=layered?graphics_attachment_array(*colors[i]):colors[i]->views[layer];
            if(depth)views[view_count++]=layered?graphics_attachment_array(*depth):depth->views[layer];
            VkFramebufferCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;fi.renderPass=p.pass;fi.attachmentCount=view_count;fi.pAttachments=views.data();fi.width=s.width;fi.height=s.height;fi.layers=layered&&!multiview?s.samples:1;VkFramebuffer framebuffer;
            // All generated passes have one graphics subpass and identical
            // load/store/layout policy. Attachment formats/count define exact
            // render-pass compatibility; blend/shader variants share the FB.
            std::array<std::uint64_t,10> framebuffer_key{};framebuffer_key[0]=s.colors;framebuffer_key[1]=s.color;framebuffer_key[2]=s.depth;framebuffer_key[3]=s.width;framebuffer_key[4]=s.height;
            for(unsigned i=0;i<view_count;++i)framebuffer_key[5+i]=reinterpret_cast<std::uint64_t>(views[i]);
            auto found=b.framebuffers.find(framebuffer_key);
            if(found!=b.framebuffers.end()){framebuffer=found->second->handle;found->second->last_use=++recorder_clock;b.framebuffer_pins.push_back(found->second);++b.framebuffer_hits;}
            else{check(vkCreateFramebuffer(device,&fi,nullptr,&framebuffer),"graphics framebuffer");auto entry=std::make_shared<typename GraphicsBatch::Framebuffer>();entry->owner=this;entry->handle=framebuffer;entry->last_use=++recorder_clock;entry->pipeline=j.pipeline;
                for(unsigned i=0;i<s.colors;++i)entry->images.push_back(colors[i]);
                if(depth)entry->images.push_back(depth);
                if(b.framebuffers.size()>=framebuffer_limit)b.framebuffers.erase(lru_entry(b.framebuffers));
                b.framebuffer_pins.push_back(entry);b.framebuffers.emplace(std::move(framebuffer_key),std::move(entry));++b.framebuffer_misses;::RsxPerfCounters::add(::RsxPerfCounters::framebuffer_misses);}
            VkRenderPassBeginInfo pass{};pass.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;pass.renderPass=p.pass;pass.framebuffer=framebuffer;pass.renderArea={{0,0},{s.width,s.height}};
            // Push constants: sample location P is evaluated by moving the
            // geometry by .5-P. Do not translate the viewport: negative viewport
            // origins need a Vulkan 1.1 extension. VS subtracts this offset in
            // clip coordinates.
            GraphicsPush push{float(s.width),float(s.height),positions[layer][0]-.5f,positions[layer][1]-.5f,layer,layered?s.samples:1u,{}};
            for(unsigned n=0;n<4;++n){push.shifts[2*n]=positions[n][0]-.5f;push.shifts[2*n+1]=positions[n][1]-.5f;}
            if(defer_planes && layer){plane_fb[layer]=framebuffer;deferred.push[layer]=push;continue;}
            // Framebuffer identity includes each layer view, extent, attachment
            // count and formats. Draw order is kept within each plane; planes
            // are distinct images and may be recorded as runs (PlaneDraw).
            if(b.active_framebuffer!=framebuffer){
                graphics_end_pass(b);
                vkCmdBeginRenderPass(b.command,&pass,VK_SUBPASS_CONTENTS_INLINE);graphics_mark(b,MarkPassBegin);
                b.active_framebuffer=framebuffer;++b.render_passes;::RsxPerfCounters::add(::RsxPerfCounters::render_passes);
            }else ++b.render_pass_reuses;
            // One query per sample pass; sum at the ownership boundary.
            vkCmdBeginQuery(b.command,b.occlusion,query+layer,graphics_features.occlusionQueryPrecise?VK_QUERY_CONTROL_PRECISE_BIT:0);
            if(b.bound_graphics_pipeline!=p.pipeline){vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,p.pipeline);b.bound_graphics_pipeline=p.pipeline;++b.pipeline_binds;}
            const auto dynamic_at=static_cast<std::uint32_t>(at);vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,p.layout,0,1,&set,1,&dynamic_at);
            vkCmdPushConstants(b.command,p.layout,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush),&push);
            // Dynamic state survives render-pass boundaries and compute binds,
            // but never command-buffer reset. Compare exact float bits so NaNs
            // and signed zero cannot manufacture a false state-cache hit.
            std::array<unsigned,22> dynamic{};
            dynamic[0]=s.width;dynamic[1]=s.height;
            dynamic[2]=s.scissor.offset.x;dynamic[3]=s.scissor.offset.y;dynamic[4]=s.scissor.extent.width;dynamic[5]=s.scissor.extent.height;
            for(unsigned face=0;face<2;++face){const auto& stencil=face?s.z.back:s.z.front;
                dynamic[6+face*3]=stencil.compareMask;dynamic[7+face*3]=stencil.writeMask;dynamic[8+face*3]=stencil.reference;}
            std::memcpy(dynamic.data()+12,s.constants.data(),16);
            std::memcpy(dynamic.data()+16,&bias_constant,4);std::memcpy(dynamic.data()+17,&bias_slope,4);
            std::memcpy(dynamic.data()+18,&min_bounds,4);std::memcpy(dynamic.data()+19,&max_bounds,4);
            auto changed=[&](unsigned first,unsigned count){const bool yes=!b.dynamic_valid || !std::equal(dynamic.begin()+first,dynamic.begin()+first+count,b.dynamic_state.begin()+first);
                if(yes)++b.dynamic_emits;else ++b.dynamic_reuses;return yes;};
            if(changed(0,2)){VkViewport viewport{0,0,float(s.width),float(s.height),0,1};vkCmdSetViewport(b.command,0,1,&viewport);}
            if(changed(2,4))vkCmdSetScissor(b.command,0,1,&s.scissor);
            for(unsigned face=0;face<2;++face){const auto mask=face?VK_STENCIL_FACE_BACK_BIT:VK_STENCIL_FACE_FRONT_BIT;const auto& stencil=face?s.z.back:s.z.front;
                if(changed(6+face*3,1))vkCmdSetStencilCompareMask(b.command,mask,stencil.compareMask);
                if(changed(7+face*3,1))vkCmdSetStencilWriteMask(b.command,mask,stencil.writeMask);
                if(changed(8+face*3,1))vkCmdSetStencilReference(b.command,mask,stencil.reference);}
            if(changed(12,4))vkCmdSetBlendConstants(b.command,s.constants.data());
            if(changed(16,2))vkCmdSetDepthBias(b.command,bias_constant,0,bias_slope);
            if(changed(18,2))vkCmdSetDepthBounds(b.command,min_bounds,max_bounds);
            b.dynamic_state=dynamic;b.dynamic_valid=true;
            const unsigned instances=layered&&!multiview?s.samples:1;
            if(j.indexed){vkCmdBindIndexBuffer(b.command,b.data_buffer,at+j.index_offset,VK_INDEX_TYPE_UINT32);vkCmdDrawIndexed(b.command,vertices,instances,first_vertex,0,0);}
            else vkCmdDraw(b.command,vertices,instances,first_vertex,0);
            vkCmdEndQuery(b.command,b.occlusion,query+layer);graphics_mark(b,MarkDraw);
            if(defer_planes){deferred.pipeline=p.pipeline;deferred.layout=p.layout;deferred.set=set;deferred.offset=dynamic_at;deferred.dynamic=dynamic;
                deferred.vertices=vertices;deferred.instances=instances;deferred.first_vertex=first_vertex;deferred.query=query;
                deferred.indexed=j.indexed;deferred.index_offset=deferred.indexed?at+j.index_offset:0;
                deferred.query_flags=graphics_features.occlusionQueryPrecise?VK_QUERY_CONTROL_PRECISE_BIT:0;}
        }
        if(defer_planes){
            // A run keeps one framebuffer per plane; anything else ends it first
            // (this also ends the pass holding this draw's plane 0).
            bool same=b.plane_count==passes && b.plane_extent.width==s.width && b.plane_extent.height==s.height;
            for(unsigned layer=1;layer<passes && same;++layer)same=b.plane_framebuffers[layer]==plane_fb[layer];
            if(!b.plane_draws.empty() && !same)graphics_end_pass(b);
            if(b.plane_draws.empty()){b.plane_framebuffers=plane_fb;b.plane_pass=p.pass;b.plane_extent={s.width,s.height};b.plane_count=passes;}
            b.plane_draws.push_back(deferred);++b.plane_deferred;
        }
        graphics_stamp(b,0,true);++b.draws;b.record_back_ms+=resident_ms(t);
    }
    std::uint64_t graphics_draw(GraphicsBatch& b,const GraphicsState& s,const std::array<std::shared_ptr<GraphicsImage>,4>& colors,
                       const std::shared_ptr<GraphicsImage>& depth,const std::string& vs,const std::string& fs,
                       const std::vector<unsigned>& data,unsigned vertices,const std::array<std::array<float,2>,4>& positions,
                       float bias_constant=0,float bias_slope=0,float min_bounds=0,float max_bounds=1,const std::array<std::shared_ptr<GraphicsImage>,20>& textures={},unsigned first_vertex=0,std::uint64_t reuse=UINT64_MAX,const std::array<std::shared_ptr<GraphicsBuffer>,16>& raw_textures={},const std::shared_ptr<const std::string>& vs_owner={},const std::shared_ptr<const std::string>& fs_owner={},const std::uint8_t* tail=nullptr,std::uint64_t tail_bytes=0,bool layered_ok=false){
        // Layered planes: every attachment must be a samples-layer array (else per-plane passes).
        bool layered=layered_ok && graphics_layered_output && s.samples>1;
        if(layered){for(unsigned i=0;i<s.colors;++i)if(colors[i]->layers!=s.samples)layered=false;if(depth && depth->layers!=s.samples)layered=false;}
        const bool multiview=layered && graphics_multiview;
        auto t=std::chrono::steady_clock::now();auto pipeline=graphics_pipeline(s,vs,fs,vs_owner,fs_owner,multiview);b.pipeline_pins.push_back(pipeline);b.pipeline_ms+=resident_ms(t);t=std::chrono::steady_clock::now();
        // An optional tail (vertex input bytes) directly follows the words in
        // the same immutable slice; the blob addresses it past data.size()*4.
        const auto words=data.size()*4,bytes=words+tail_bytes;if(bytes>graphics_properties.limits.maxStorageBufferRange)throw std::runtime_error("RSX graphics storage range limit");
        std::uint64_t at=reuse;
        if(reuse==UINT64_MAX){graphics_begin(b);at=graphics_space(b,bytes);auto* out=static_cast<char*>(b.map)+at;std::memcpy(out,data.data(),words);if(tail_bytes)std::memcpy(out+words,tail,tail_bytes);
            b.upload_bytes+=bytes;::RsxPerfCounters::add(::RsxPerfCounters::upload_bytes,bytes);}
        // The dynamic blob binding's range is not observable by the shaders
        // (no length(), no robust access): round it to a power-of-two bucket
        // that still fits after this offset so equal texture sets share one
        // descriptor across draws. The exact size is used when no bucket fits.
        std::uint64_t range=bytes;
        {std::uint64_t bucket=4096;while(bucket<bytes)bucket<<=1;
            if(bucket<=graphics_properties.limits.maxStorageBufferRange && at+bucket<=b.capacity)range=bucket;}
        for(unsigned i=0;i<s.colors;++i)b.pins.push_back(colors[i]);
        if(depth)b.pins.push_back(depth);
        for(const auto& image:textures)if(image)b.pins.push_back(image);
        for(const auto& raw:raw_textures)if(raw)b.buffer_pins.push_back(raw);
        if(reuse==UINT64_MAX)b.uploads.push_back({at,at,bytes});
        if(b.visibility_count+s.samples>1024)throw std::runtime_error("RSX graphics visibility budget");
        const auto query=b.visibility_count++;::RsxPerfCounters::add(::RsxPerfCounters::draw_layers,s.samples);
        // Layered: every sample plane is one instance of a single draw into
        // a layered framebuffer. Per plane the primitive order is unchanged;
        // planes are distinct layers, so their relative order is unobservable.
        const unsigned passes=layered?1:s.samples;graphics_last_queries=multiview?s.samples:passes;
        // Plane runs (GraphicsBatch::PlaneDraw): planes >=1 of a per-plane draw
        // are recorded after plane 0's pass. ANYPS3_RSX_PLANE_RUNS=0 records
        // every plane immediately (one pass switch per plane).
        static const bool plane_runs=[]{const char* v=std::getenv("ANYPS3_RSX_PLANE_RUNS");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_PLANE_RUNS");}
            return !v || !std::strcmp(v,"1");}();
        const bool defer_planes=plane_runs && passes>1;
        b.visibility_count+=(multiview?s.samples:passes)-1;
        // Keep the existing logical readback accounting at record time. The
        // submit combines these requests; it does not create new requests.
        b.status_offsets.push_back(at+28);b.download_bytes+=4;
        ++graphics_draws;b.record_ms+=resident_ms(t);
        const bool asynchronous=draw_recorder && !b.inline_draws && !b.queries && !b.marks && !graphics_gpu_loop();
        std::optional<DrawJob> inline_job;DrawJob* job=asynchronous?&recorder_slot():&inline_job.emplace();
        job->batch=&b;job->pipeline=std::move(pipeline);job->s=s;job->colors=colors;job->depth=depth;job->textures=textures;job->raw_textures=raw_textures;job->positions=positions;
        job->bias_constant=bias_constant;job->bias_slope=bias_slope;job->min_bounds=min_bounds;job->max_bounds=max_bounds;job->vertices=vertices;job->first_vertex=first_vertex;
        job->query=query;job->passes=passes;job->layered=layered;job->multiview=multiview;job->defer_planes=defer_planes;job->at=at;job->range=range;
        job->indexed=data.size()>15 && data[15]==1;job->index_offset=job->indexed?std::uint64_t(data[13])*4:0;
        if(asynchronous){b.pending_jobs.fetch_add(1,std::memory_order_relaxed);recorder_publish();}else graphics_record_draw(*job);
        return at;
    }
    // A value identifies one queue prefix. Submission never waits for it.
    std::uint64_t graphics_submit(GraphicsBatch& b){b.sync();
        if(b.failed)throw std::runtime_error("RSX graphics batch poisoned");
        if(!b.recording)return b.completion;
        try{
            graphics_end_pass(b);
            if(!b.status_offsets.empty()){
                // Reused draw blobs share an atomic-OR failure word. Its final
                // value covers every use; overlapping VkBufferCopy destinations
                // are forbidden, so deduplicate before the single copy command.
                std::sort(b.status_offsets.begin(),b.status_offsets.end());
                b.status_offsets.erase(std::unique(b.status_offsets.begin(),b.status_offsets.end()),b.status_offsets.end());
                VkBufferMemoryBarrier status{};status.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
                status.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;status.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT;
                status.srcQueueFamilyIndex=status.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
                status.buffer=b.data_buffer;status.offset=0;status.size=b.used;
                vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_VERTEX_SHADER_BIT|VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                    VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,1,&status,0,nullptr);
                std::vector<VkBufferCopy> copies;copies.reserve(b.status_offsets.size());
                for(auto at:b.status_offsets)copies.push_back({at,at,4});
                vkCmdCopyBuffer(b.command,b.data_buffer,b.buffer,unsigned(copies.size()),copies.data());
            }
            if(!b.coherent){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=b.memory;r.size=VK_WHOLE_SIZE;check(vkFlushMappedMemoryRanges(device,1,&r),"graphics flush");}
            graphics_mark(b,graphics_last_kind());graphics_last_kind()=MarkOther;
            VkMemoryBarrier m{};m.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;m.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT;m.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
            vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&m,0,nullptr,0,nullptr);graphics_mark(b,MarkTail);
            if(b.busy_queries)vkCmdWriteTimestamp(b.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.busy_queries,1);
            check(vkEndCommandBuffer(b.command),"graphics end");b.recording=false;
            // Recording is complete, so every immutable slice is known. Upload
            // them once before all consumers instead of breaking every draw's
            // pass. Distinct allocations never overlap, even across versions.
            VkCommandBufferBeginInfo begin{};begin.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;begin.flags=graphics_gpu_loop()?VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT:VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
            VkCommandBuffer commands[2]{};unsigned command_count=0;
            if(transfer_queue){
                // The prelude runs on the DMA queue while the graphics queue still executes the previous batch. The batch waits on
                // an upload timeline value; the semaphore wait is the execution and memory dependency the barrier used to provide.
                // Both buffers are concurrent-shared, every copy reads this batch's staging and writes this batch's device buffer
                // (reused only after its previous use retired).
                if(!b.uploads.empty()){
                    check(vkBeginCommandBuffer(b.transfer_command,&begin),"graphics transfer begin");
                    if(b.transfer_queries)vkCmdWriteTimestamp(b.transfer_command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,b.transfer_queries,0);
                    vkCmdCopyBuffer(b.transfer_command,b.buffer,b.data_buffer,unsigned(b.uploads.size()),b.uploads.data());++b.upload_commands;
                    if(b.transfer_queries)vkCmdWriteTimestamp(b.transfer_command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.transfer_queries,1);
                    check(vkEndCommandBuffer(b.transfer_command),"graphics transfer end");
                    if(graphics_upload_serial==UINT64_MAX)throw std::runtime_error("RSX graphics upload timeline exhausted");
                    const std::uint64_t upload_value=graphics_upload_serial+1;
                    VkTimelineSemaphoreSubmitInfo upload_timeline{};upload_timeline.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;upload_timeline.signalSemaphoreValueCount=1;upload_timeline.pSignalSemaphoreValues=&upload_value;
                    VkSubmitInfo ui{};ui.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;ui.pNext=&upload_timeline;ui.commandBufferCount=1;ui.pCommandBuffers=&b.transfer_command;ui.signalSemaphoreCount=1;ui.pSignalSemaphores=&graphics_upload_timeline;
                    check(vkQueueSubmit(transfer_queue,1,&ui,VK_NULL_HANDLE),"graphics upload submit");
                    graphics_upload_serial=upload_value;b.upload_wait=upload_value;b.transfer_pending=b.transfer_queries!=VK_NULL_HANDLE;
                }
                commands[command_count++]=b.command;
            }else{
            check(vkBeginCommandBuffer(b.upload_command,&begin),"graphics uploads begin");
            if(b.busy_queries){vkCmdResetQueryPool(b.upload_command,b.busy_queries,0,3);vkCmdWriteTimestamp(b.upload_command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.busy_queries,0);}
            if(!b.uploads.empty()){
                vkCmdCopyBuffer(b.upload_command,b.buffer,b.data_buffer,unsigned(b.uploads.size()),b.uploads.data());++b.upload_commands;
                VkBufferMemoryBarrier ready{};ready.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
                ready.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;ready.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
                ready.srcQueueFamilyIndex=ready.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;ready.buffer=b.data_buffer;ready.offset=0;ready.size=b.used;
                vkCmdPipelineBarrier(b.upload_command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,0,nullptr,1,&ready,0,nullptr);
            }
            if(b.busy_queries)vkCmdWriteTimestamp(b.upload_command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.busy_queries,2);
            check(vkEndCommandBuffer(b.upload_command),"graphics uploads end");
            commands[command_count++]=b.upload_command;commands[command_count++]=b.command;
            }
            if(graphics_serial==UINT64_MAX)throw std::runtime_error("RSX graphics timeline exhausted");
            const std::uint64_t value=graphics_serial+1,values[]={value,0};const VkSemaphore signals[]={graphics_timeline,b.signal};
            const std::uint64_t wait_values[]={b.upload_wait};const VkSemaphore waits[]={graphics_upload_timeline};const VkPipelineStageFlags wait_stages[]={VK_PIPELINE_STAGE_ALL_COMMANDS_BIT};
            const bool wait_upload=transfer_queue && !b.uploads.empty();
            VkTimelineSemaphoreSubmitInfo timeline{};timeline.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;timeline.signalSemaphoreValueCount=b.signal?2:1;timeline.pSignalSemaphoreValues=values;
            if(wait_upload){timeline.waitSemaphoreValueCount=1;timeline.pWaitSemaphoreValues=wait_values;}
            VkSubmitInfo si{};si.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;si.pNext=&timeline;si.commandBufferCount=command_count;si.pCommandBuffers=commands;si.signalSemaphoreCount=timeline.signalSemaphoreValueCount;si.pSignalSemaphores=signals;
            if(wait_upload){si.waitSemaphoreCount=1;si.pWaitSemaphores=waits;si.pWaitDstStageMask=wait_stages;}
            if(const unsigned loop=graphics_gpu_loop()){
                // DIAGNOSTIC ONLY (ANYPS3_RSX_GPU_LOOP=N, replay benchmark): run this batch N extra times back to back so the GPU
                // is saturated (driver boosts clocks); the second half is timed on the host. Guest-visible results of such a
                // run are meaningless (blending and feedback accumulate), it never belongs in a game run.
                // Validity: the command buffers are recorded SIMULTANEOUS_USE (the host resubmits them while earlier iterations are still
                // pending) and the graphics iterations are chained on a loop timeline so that no two executions overlap; the DMA copies
                // read an immutable snapshot of the staging bytes (the batch writes draw-status words back into its own staging buffer)
                // and are chained on the upload timeline (every iteration writes the same scratch ranges).
                const unsigned warm=loop/2,timed=loop-warm;
                // With an async prelude each iteration also issues a same-sized prelude copy into a scratch buffer on the DMA queue
                // (the steady state of consecutive batches: batch k+1's upload overlaps batch k's graphics work).
                const bool overlap=transfer_queue && !b.uploads.empty();
                // The DMA copy command buffer is freed on every exit path, but only after both queues drained (see the catch below).
                struct LoopCopy{NativeVulkan* owner;VkCommandBuffer command=VK_NULL_HANDLE;~LoopCopy(){if(command)vkFreeCommandBuffers(owner->device,owner->transfer_pool,1,&command);}} loop_copy_owner{this};
                VkCommandBuffer& loop_copy=loop_copy_owner.command;
                try{
                if(!graphics_loop_timeline){VkSemaphoreTypeCreateInfo tt{};tt.sType=VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;tt.semaphoreType=VK_SEMAPHORE_TYPE_TIMELINE;
                    VkSemaphoreCreateInfo ti{};ti.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;ti.pNext=&tt;check(vkCreateSemaphore(device,&ti,nullptr,&graphics_loop_timeline),"graphics loop timeline");}
                if(overlap){
                    if(graphics_loop_scratch_size<b.capacity){
                        if(graphics_loop_scratch){vkDeviceWaitIdle(device);vkDestroyBuffer(device,graphics_loop_scratch,nullptr);vkFreeMemory(device,graphics_loop_scratch_memory,nullptr);}
                        VkBufferCreateInfo sbi{};sbi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;sbi.size=b.capacity;sbi.usage=VK_BUFFER_USAGE_TRANSFER_DST_BIT;
                        const std::uint32_t fam[2]={family,transfer_family};sbi.sharingMode=VK_SHARING_MODE_CONCURRENT;sbi.queueFamilyIndexCount=2;sbi.pQueueFamilyIndices=fam;
                        check(vkCreateBuffer(device,&sbi,nullptr,&graphics_loop_scratch),"loop scratch");
                        VkMemoryRequirements r{};vkGetBufferMemoryRequirements(device,graphics_loop_scratch,&r);VkMemoryAllocateInfo mi{};mi.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;mi.allocationSize=r.size;mi.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
                        check(vkAllocateMemory(device,&mi,nullptr,&graphics_loop_scratch_memory),"loop scratch memory");check(vkBindBufferMemory(device,graphics_loop_scratch,graphics_loop_scratch_memory,0),"loop scratch bind");graphics_loop_scratch_size=b.capacity;
                    }
                    // Snapshot of the staging bytes in the same kind of host memory as the staging buffer itself (PCIe-bound copies).
                    if(graphics_loop_snapshot_size<b.used){
                        vkDeviceWaitIdle(device);
                        if(graphics_loop_snapshot_map){vkUnmapMemory(device,graphics_loop_snapshot_memory);graphics_loop_snapshot_map=nullptr;}
                        if(graphics_loop_snapshot){vkDestroyBuffer(device,graphics_loop_snapshot,nullptr);graphics_loop_snapshot=VK_NULL_HANDLE;}
                        if(graphics_loop_snapshot_memory){vkFreeMemory(device,graphics_loop_snapshot_memory,nullptr);graphics_loop_snapshot_memory=VK_NULL_HANDLE;}
                        graphics_loop_snapshot_size=0;
                        VkBufferCreateInfo sbi{};sbi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;sbi.size=b.capacity;sbi.usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
                        const std::uint32_t fam[2]={family,transfer_family};sbi.sharingMode=VK_SHARING_MODE_CONCURRENT;sbi.queueFamilyIndexCount=2;sbi.pQueueFamilyIndices=fam;
                        check(vkCreateBuffer(device,&sbi,nullptr,&graphics_loop_snapshot),"loop snapshot");
                        VkMemoryRequirements r{};vkGetBufferMemoryRequirements(device,graphics_loop_snapshot,&r);VkMemoryAllocateInfo mi{};mi.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;mi.allocationSize=r.size;mi.memoryTypeIndex=host_read_memory_type(r.memoryTypeBits);
                        graphics_loop_snapshot_coherent=memory.memoryTypes[mi.memoryTypeIndex].propertyFlags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
                        check(vkAllocateMemory(device,&mi,nullptr,&graphics_loop_snapshot_memory),"loop snapshot memory");check(vkBindBufferMemory(device,graphics_loop_snapshot,graphics_loop_snapshot_memory,0),"loop snapshot bind");
                        check(vkMapMemory(device,graphics_loop_snapshot_memory,0,VK_WHOLE_SIZE,0,&graphics_loop_snapshot_map),"loop snapshot map");graphics_loop_snapshot_size=b.capacity;
                    }
                    std::memcpy(graphics_loop_snapshot_map,b.map,b.used);
                    if(!graphics_loop_snapshot_coherent){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=graphics_loop_snapshot_memory;r.size=VK_WHOLE_SIZE;check(vkFlushMappedMemoryRanges(device,1,&r),"loop snapshot flush");}
                    VkCommandBufferAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;ai.commandPool=transfer_pool;ai.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ai.commandBufferCount=1;
                    check(vkAllocateCommandBuffers(device,&ai,&loop_copy),"loop copy command");VkCommandBufferBeginInfo lb{};lb.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;lb.flags=VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT;
                    check(vkBeginCommandBuffer(loop_copy,&lb),"loop copy begin");vkCmdCopyBuffer(loop_copy,graphics_loop_snapshot,graphics_loop_scratch,unsigned(b.uploads.size()),b.uploads.data());check(vkEndCommandBuffer(loop_copy),"loop copy end");
                }
                auto run=[&](unsigned n){for(unsigned k=0;k<n;++k){
                    std::uint64_t waits_value[2]{};VkSemaphore waits_semaphore[2]{};VkPipelineStageFlags waits_stage[2]{};unsigned wait_count=0;
                    if(overlap){
                        const std::uint64_t before=graphics_upload_serial,u=before+1; // the serial advances only after the submission succeeded
                        // The previous DMA submission (real prelude or earlier iteration) completes before this one starts: no scratch WAW overlap.
                        VkTimelineSemaphoreSubmitInfo ut{};ut.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;ut.waitSemaphoreValueCount=1;ut.pWaitSemaphoreValues=&before;ut.signalSemaphoreValueCount=1;ut.pSignalSemaphoreValues=&u;
                        const VkPipelineStageFlags transfer_stage=VK_PIPELINE_STAGE_TRANSFER_BIT;
                        VkSubmitInfo ui{};ui.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;ui.pNext=&ut;ui.commandBufferCount=1;ui.pCommandBuffers=&loop_copy;ui.waitSemaphoreCount=1;ui.pWaitSemaphores=&graphics_upload_timeline;ui.pWaitDstStageMask=&transfer_stage;ui.signalSemaphoreCount=1;ui.pSignalSemaphores=&graphics_upload_timeline;
                        check(vkQueueSubmit(transfer_queue,1,&ui,VK_NULL_HANDLE),"graphics loop upload submit");graphics_upload_serial=u;
                        waits_value[wait_count]=u;waits_semaphore[wait_count]=graphics_upload_timeline;waits_stage[wait_count++]=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
                    }
                    // The previous graphics iteration has completed before this one starts (no overlap of one command buffer with itself).
                    const std::uint64_t previous=graphics_loop_serial,current=previous+1;
                    waits_value[wait_count]=previous;waits_semaphore[wait_count]=graphics_loop_timeline;waits_stage[wait_count++]=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
                    VkTimelineSemaphoreSubmitInfo gt{};gt.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;gt.waitSemaphoreValueCount=wait_count;gt.pWaitSemaphoreValues=waits_value;gt.signalSemaphoreValueCount=1;gt.pSignalSemaphoreValues=&current;
                    VkSubmitInfo gi{};gi.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;gi.pNext=&gt;gi.commandBufferCount=command_count;gi.pCommandBuffers=commands;gi.waitSemaphoreCount=wait_count;gi.pWaitSemaphores=waits_semaphore;gi.pWaitDstStageMask=waits_stage;gi.signalSemaphoreCount=1;gi.pSignalSemaphores=&graphics_loop_timeline;
                    if(graphics_test_loop_fault && !--graphics_test_loop_fault)throw std::runtime_error("injected graphics loop submit failure (test hook)");
                    check(vkQueueSubmit(queue,1,&gi,VK_NULL_HANDLE),"graphics loop submit");graphics_loop_serial=current;}
                    check(vkQueueWaitIdle(queue),"graphics loop idle");if(overlap)check(vkQueueWaitIdle(transfer_queue),"graphics loop transfer idle");};
                const auto t0=std::chrono::steady_clock::now();run(warm);const auto t1=std::chrono::steady_clock::now();run(timed);const auto t2=std::chrono::steady_clock::now();
                graphics_loop_warm_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count()/std::max(1u,warm);
                graphics_loop_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(t2-t1).count()/timed;++graphics_loop_batches;
                }catch(...){
                    // Earlier iterations may still be pending: drain both queues (ignoring their errors) before anything is released,
                    // then surface the original failure.
                    vkQueueWaitIdle(queue);if(transfer_queue)vkQueueWaitIdle(transfer_queue);
                    throw;
                }
                std::fprintf(stderr,"PERF GPU_LOOP batches=%llu steady_ns=%llu warm_ns=%llu\n",(unsigned long long)graphics_loop_batches,(unsigned long long)graphics_loop_ns,(unsigned long long)graphics_loop_warm_ns);
            }
            if(graphics_test_submit_fault){graphics_test_submit_fault=false;throw std::runtime_error("injected graphics submit failure (test hook)");}
            check(vkQueueSubmit(queue,1,&si,b.fence),"graphics submit");::RsxPerfCounters::add(::RsxPerfCounters::submits);graphics_serial=value;b.completion=value;b.submitted=true;++b.submissions;return value;
        }catch(...){b.failed=true;throw;}
    }
    std::uint64_t graphics_completed(){::RsxPerfCounters::add(::RsxPerfCounters::timeline_polls);std::uint64_t value=0;check(vkGetSemaphoreCounterValue(device,graphics_timeline,&value),"graphics timeline counter");return value;}
    bool graphics_collect(GraphicsBatch& b,bool wait=true){b.sync();
        if(b.failed)throw std::runtime_error("RSX graphics batch poisoned");
        if(!b.submitted)return true;
        try{
            if(wait){
                // An already completed label requires no blocking kernel call.
                if(graphics_completed()<b.completion){const auto t=std::chrono::steady_clock::now();::RsxPerfCounters::add(::RsxPerfCounters::collect_waits);struct WaitClock{std::chrono::steady_clock::time_point t;~WaitClock(){::RsxPerfCounters::add(::RsxPerfCounters::wait_ns,std::uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-t).count()));}} wait_clock{t};VkSemaphoreWaitInfo wi{};wi.sType=VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO;wi.semaphoreCount=1;wi.pSemaphores=&graphics_timeline;wi.pValues=&b.completion;
                    check(vkWaitSemaphores(device,&wi,UINT64_MAX),"graphics timeline wait");b.wait_ms+=resident_ms(t);}
            }else if(graphics_completed()<b.completion)return false;
            // The timeline signal proves execution/memory completion. The
            // fence also covers VkSubmitInfo lifetime before command reuse.
            ::RsxPerfCounters::add(::RsxPerfCounters::fence_polls);const auto fence_status=vkGetFenceStatus(device,b.fence);
            if(fence_status==VK_NOT_READY){if(!wait)return false;const auto t=std::chrono::steady_clock::now();check(vkWaitForFences(device,1,&b.fence,VK_TRUE,UINT64_MAX),"graphics retirement fence");b.wait_ms+=resident_ms(t);}
            else check(fence_status,"graphics retirement status");
            b.submitted=false;--graphics_recordings;
            if(!b.coherent){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=b.memory;r.size=VK_WHOLE_SIZE;check(vkInvalidateMappedMemoryRanges(device,1,&r),"graphics invalidate");}
            if(b.queries && b.query_count){std::vector<std::uint64_t> values(b.query_count);check(vkGetQueryPoolResults(device,b.queries,0,b.query_count,values.size()*8,values.data(),8,VK_QUERY_RESULT_64_BIT),"graphics query results");
                const auto mask=b.timestamp_bits>=64?UINT64_MAX:((UINT64_C(1)<<b.timestamp_bits)-1);
                if(b.phase_pipelines.size()<b.phases.size())b.phase_pipelines.resize(b.phases.size(),nullptr);
                for(unsigned i=0;i<b.phases.size();++i){const double ms=double((values[2*i+1]-values[2*i])&mask)*b.timestamp_period/1e6;
                    if(b.phases[i]==1 && b.phase_pipelines[i])draw_profile_add(*b.phase_pipelines[i],ms);
                    if(b.phases[i]==0)b.upload_ms+=ms;else if(b.phases[i]==1)b.draw_ms+=ms;else b.download_ms+=ms;}}
            if(b.busy_queries){std::uint64_t ticks[3]{};check(vkGetQueryPoolResults(device,b.busy_queries,0,transfer_queue?2:3,sizeof(ticks),ticks,8,VK_QUERY_RESULT_64_BIT),"graphics busy results");
                // Non-async: ticks[2]-ticks[0] is the in-queue prelude. Async: busy_ns is the graphics-queue time of the batch and
                // the DMA queue's prelude is measured by its own two timestamps (prelude_ns).
                const auto busy_mask=b.timestamp_bits>=64?UINT64_MAX:((UINT64_C(1)<<b.timestamp_bits)-1);
                double prelude_batch_ns=transfer_queue?0.0:double((ticks[2]-ticks[0])&busy_mask)*b.timestamp_period;
                if(transfer_queue){ticks[2]=ticks[0];
                    if(b.transfer_pending){std::uint64_t t[2]{};check(vkGetQueryPoolResults(device,b.transfer_queries,0,2,sizeof(t),t,8,VK_QUERY_RESULT_64_BIT),"graphics transfer results");
                        const auto transfer_mask=transfer_timestamp_bits>=64?UINT64_MAX:((UINT64_C(1)<<transfer_timestamp_bits)-1); // the DMA family's own width
                        prelude_batch_ns=double((t[1]-t[0])&transfer_mask)*b.timestamp_period;vkResetQueryPool(device,b.transfer_queries,0,2);b.transfer_pending=false;}}
                graphics_busy_ns+=std::uint64_t(double((ticks[1]-ticks[0])&busy_mask)*b.timestamp_period);graphics_profile_frames+=b.frames;++graphics_completed_batches;
                graphics_prelude_ns+=std::uint64_t(prelude_batch_ns);
                if(b.marks && b.mark_count>1){std::vector<std::uint64_t> stamps(b.mark_count);
                    check(vkGetQueryPoolResults(device,b.marks,0,b.mark_count,stamps.size()*8,stamps.data(),8,VK_QUERY_RESULT_64_BIT),"graphics mark results");
                    if(std::getenv("ANYPS3_RSX_VK_MARKS_TRACE") && graphics_completed_batches>=300 && graphics_completed_batches<330){
                        static const char* const names[]={"start","draw","pass_begin","pass_end","barrier","copy_buffer","copy_image","dispatch","clear","fill","tail","other"};
                        static const char* const sites[]={"-","tex_upload","surface_copy","transfer","unpack","pack","aspect","decode","raw_buffer","device_upload","predicate","present","clear","device_copies"};
                        std::fprintf(stderr,"TRACE batch=%llu marks=%u prelude_ns=%llu busy_ns=%llu\n",(unsigned long long)graphics_completed_batches,b.mark_count,(unsigned long long)prelude_batch_ns,(unsigned long long)(double((ticks[1]-ticks[0])&busy_mask)*b.timestamp_period));
                        unsigned run=0;unsigned long long runns=0;unsigned last=UINT_MAX;
                        for(unsigned i=1;i<=b.mark_count;++i){
                            const bool end=i==b.mark_count;const auto kind=end?UINT_MAX:b.mark_kinds[i];const unsigned long long ns=end?0:(unsigned long long)(double((stamps[i]-stamps[i-1])&busy_mask)*b.timestamp_period);
                            if(!end && kind==last){++run;runns+=ns;continue;}
                            if(last!=UINT_MAX)std::fprintf(stderr,"TRACE   %s:%s x%u %.1f us\n",sites[std::min<unsigned>(last/16,SiteCount-1)],names[std::min<unsigned>(last%16,MarkOther)],run,runns/1000.0);
                            last=kind;run=1;runns=ns;
                        }
                    }
                    for(unsigned i=1;i<b.mark_count;++i){const auto kind=b.mark_kinds[i];graphics_mark_ns[kind]+=std::uint64_t(double((stamps[i]-stamps[i-1])&busy_mask)*b.timestamp_period);++graphics_mark_count[kind];}
                    graphics_mark_ns[MarkStart]+=std::uint64_t(double((stamps[0]-ticks[2])&busy_mask)*b.timestamp_period);++graphics_mark_count[MarkStart];}
                if(b.frames){std::fprintf(stderr,"PERF GPU busy_ns=%llu frames=%llu batches=%llu timeline=%llu prelude_ns=%llu\n",(unsigned long long)graphics_busy_ns,(unsigned long long)graphics_profile_frames,(unsigned long long)graphics_completed_batches,(unsigned long long)b.completion,(unsigned long long)graphics_prelude_ns);
                    if(b.marks){static const char* const names[]={"start","draw","pass_begin","pass_end","barrier","copy_buffer","copy_image","dispatch","clear","fill","tail","other"};
                        static const char* const sites[]={"-","tex_upload","surface_copy","transfer","unpack","pack","aspect","decode","raw_buffer","device_upload","predicate","present","clear","device_copies"};
                        std::fprintf(stderr,"PERF GPU_MARKS frames=%llu",(unsigned long long)graphics_profile_frames);
                        for(unsigned k=0;k<graphics_mark_ns.size();++k){if(!graphics_mark_count[k])continue;
                            std::fprintf(stderr," %s:%s=%llu/%llu",sites[std::min<unsigned>(k/16,SiteCount-1)],names[std::min<unsigned>(k%16,MarkOther)],(unsigned long long)graphics_mark_ns[k],(unsigned long long)graphics_mark_count[k]);}
                        std::fputc('\n',stderr);}}}
            b.visibility.resize(b.visibility_count);
            if(b.visibility_count)check(vkGetQueryPoolResults(device,b.occlusion,0,b.visibility_count,b.visibility.size()*8,b.visibility.data(),8,VK_QUERY_RESULT_64_BIT),"graphics visibility results");
            return true;
        }catch(...){b.failed=true;throw;}
    }
    void graphics_finish(GraphicsBatch& b){::RsxPerfCounters::add(::RsxPerfCounters::finish_waits);graphics_submit(b);graphics_collect(b);}
    static void graphics_move_stats(GraphicsBatch& from,GraphicsBatch& to){
        from.sync();
        // Counters the draw recorder increments move only while the destination has no queued draws (otherwise they stay with `from`).
        if(!to.pending_jobs.load(std::memory_order_acquire)){
            to.draws+=from.draws;to.record_back_ms+=from.record_back_ms;to.descriptor_hits+=from.descriptor_hits;to.descriptor_misses+=from.descriptor_misses;to.framebuffer_hits+=from.framebuffer_hits;to.framebuffer_misses+=from.framebuffer_misses;
            from.draws=0;from.record_back_ms=0;from.descriptor_hits=from.descriptor_misses=from.framebuffer_hits=from.framebuffer_misses=0;
        }
        to.upload_ms+=from.upload_ms;to.draw_ms+=from.draw_ms;to.download_ms+=from.download_ms;to.wait_ms+=from.wait_ms;to.pipeline_ms+=from.pipeline_ms;to.record_ms+=from.record_ms;
        from.upload_ms=from.draw_ms=from.download_ms=from.wait_ms=from.pipeline_ms=from.record_ms=0;
        to.upload_bytes+=from.upload_bytes;to.download_bytes+=from.download_bytes;to.submissions+=from.submissions;
        from.upload_bytes=from.download_bytes=from.submissions=0;
    }
    VkDescriptorPool graphics_descriptor_pool(){
        VkDescriptorPoolSize sizes[]={{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC,1024},{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,17*1024},{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,20*1024}};VkDescriptorPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;pi.maxSets=1024;pi.poolSizeCount=3;pi.pPoolSizes=sizes;
        VkDescriptorPool pool=VK_NULL_HANDLE;check(vkCreateDescriptorPool(device,&pi,nullptr,&pool),"graphics descriptor pool");return pool;
    }
    // One batch can record more draws and texture decodes than a single pool
    // holds (a real frame has thousands of draws); exhaustion chains a further
    // pool until graphics_reset. A failure on an empty pool is a real error.
    VkDescriptorSet graphics_descriptor_set(GraphicsBatch& b,VkDescriptorSetLayout layout,const char* operation){b.sync();
        VkDescriptorSetAllocateInfo si{};si.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;si.descriptorSetCount=1;si.pSetLayouts=&layout;
        bool fresh=false;
        for(;;){
            si.descriptorPool=b.overflow_used?b.overflow_descriptors[b.overflow_used-1]:b.descriptors;
            VkDescriptorSet set=VK_NULL_HANDLE;::RsxPerfCounters::add(::RsxPerfCounters::descriptor_allocs);const VkResult result=vkAllocateDescriptorSets(device,&si,&set);
            if(result==VK_SUCCESS)return set;
            if(fresh || (result!=VK_ERROR_OUT_OF_POOL_MEMORY && result!=VK_ERROR_FRAGMENTED_POOL))check(result,operation);
            if(b.overflow_used==b.overflow_descriptors.size()){
                if(b.overflow_descriptors.size()>=256)throw std::runtime_error(std::string(operation)+": descriptor pool chain exceeds 256 pools");
                b.overflow_descriptors.push_back(graphics_descriptor_pool());
            }
            ++b.overflow_used;fresh=true;
        }
    }
    void graphics_reset(GraphicsBatch& b){b.sync();
        if(b.recording || b.submitted || b.failed)throw std::runtime_error("RSX graphics reset pending/failed batch");
        b.framebuffer_pins.clear();b.pipeline_pins.clear();b.pins.clear();b.buffer_pins.clear();b.compute_pins.clear();
        check(vkResetFences(device,1,&b.fence),"graphics reset fence");check(vkResetCommandBuffer(b.command,0),"graphics reset commands");
        check(vkResetCommandBuffer(b.upload_command,0),"graphics reset uploads");if(b.transfer_command)check(vkResetCommandBuffer(b.transfer_command,0),"graphics reset transfer");b.upload_wait=0;
        check(vkResetDescriptorPool(device,b.descriptors,0),"graphics reset descriptors");
        for(std::size_t i=0;i<b.overflow_used;++i){check(vkResetDescriptorPool(device,b.overflow_descriptors[i],0),"graphics reset descriptors");}
        b.overflow_used=0;b.used=0;b.status_offsets.clear();b.uploads.clear();b.active_framebuffer=VK_NULL_HANDLE;b.completion=0;b.frames=0;b.descriptor_cache.clear();b.signal=VK_NULL_HANDLE;b.visibility_count=0;b.visibility.clear();b.query_count=0;b.phases.clear();b.phase_pipelines.clear();
        b.bound_graphics_pipeline=VK_NULL_HANDLE;b.dynamic_valid=false;b.state_versions.fill(0);b.state_offsets.fill(0);

    }
private:
#endif
'''

MEMBERS += '\n#ifdef ANYPS3_RSX_VK\n' + SAMPLED_MEMBERS + '\nprivate:\n#endif\n'
