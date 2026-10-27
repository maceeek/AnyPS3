"""Vulkan graphics transport. RSX decoding and guest ownership live separately.

Sample planes use single-sample array layers with explicit viewport offsets.
This preserves the RSX sample-index layout even on devices without 2x MSAA or
programmable sample locations. Each plane is rasterized and shaded by hardware.
"""
from .rsx_vk_sampled import MEMBERS as SAMPLED_MEMBERS
from .rsx_vk_readback import MEMBERS as READBACK_MEMBERS
from .rsx_vk_upload import MEMBERS as UPLOAD_MEMBERS
from .rsx_vk_texture_decode import MEMBERS as DECODE_MEMBERS
from .rsx_vk_blit import MEMBERS as BLIT_MEMBERS

MEMBERS = r'''
#ifdef ANYPS3_RSX_VK
public:
    struct GraphicsState {
        unsigned width=0,height=0,samples=1,colors=1;
        // Guest coordinates stay in width/height. A visual draw can rasterize
        // into a larger attachment without changing guest viewport constants.
        unsigned raster_width=0,raster_height=0;
        bool scale_visual=false,visual_pass=false;
        // false: this native draw allocates no occlusion queries (its pixel count is never consumed: ZPASS counting off).
        bool visibility=true;
        // true: the caller records this native draw's visual half itself (no paired draw, no native->visual sync).
        bool visual_external=false;
        // true: the draw's fragment work does not depend on the sample plane (no 0x1d7c coverage/alpha-to-coverage, no packed
        // history, no lines/points), so its visual half may rasterize as true 2x MSAA (ANYPS3_RENDER_SCALE_MSAA, graphics_ms_*).
        bool ms_ok=false;
        // Vulkan rasterization samples of the pipeline and its render pass (1, or 2 for a visual MSAA draw/working image).
        unsigned raster_samples=1;
        VkFormat color=VK_FORMAT_R8G8B8A8_UNORM,depth=VK_FORMAT_UNDEFINED;
        std::array<VkPipelineColorBlendAttachmentState,4> blend{};
        VkPipelineDepthStencilStateCreateInfo z{};
        VkPipelineRasterizationStateCreateInfo raster{};
        std::array<float,4> constants{};
        VkRect2D scissor{};
        bool logic_enable=false;VkLogicOp logic=VK_LOGIC_OP_COPY;
    };
    static VkRect2D graphics_raster_scissor(const GraphicsState& s){
        if(!s.raster_width && !s.raster_height)return s.scissor;
        if(!s.width || !s.height || !s.raster_width || !s.raster_height ||
           s.width>INT32_MAX || s.height>INT32_MAX || s.raster_width>INT32_MAX || s.raster_height>INT32_MAX ||
           s.scissor.offset.x<0 || s.scissor.offset.y<0)
            throw std::runtime_error("invalid RSX visual raster extent/scissor");
        // Include exactly the physical pixel centers whose guest coordinate
        // lies in [begin,end). This also handles fractional resolution ratios
        // and clips an overhanging guest scissor before multiplying.
        auto edge=[](std::uint64_t x,unsigned native,unsigned raster){
            x=std::min<std::uint64_t>(x,native);
            return unsigned((2*x*raster+native-1)/(2*std::uint64_t(native)));
        };
        const auto x=edge(unsigned(s.scissor.offset.x),s.width,s.raster_width),y=edge(unsigned(s.scissor.offset.y),s.height,s.raster_height);
        const auto right=edge(std::uint64_t(s.scissor.offset.x)+s.scissor.extent.width,s.width,s.raster_width);
        const auto bottom=edge(std::uint64_t(s.scissor.offset.y)+s.scissor.extent.height,s.height,s.raster_height);
        return {{int(x),int(y)},{right-x,bottom-y}};
    }
    struct GraphicsPipeline;
    struct GraphicsImage {
        NativeVulkan* owner;
        VkSampler sampler=VK_NULL_HANDLE;
        VkImage image=VK_NULL_HANDLE;VkDeviceMemory memory=VK_NULL_HANDLE;
        std::vector<VkImageView> views;
        VkImageView array_view=VK_NULL_HANDLE,attachment_array=VK_NULL_HANDLE;
        VkFormat format;unsigned width,height,layers;
        bool depth;std::uint64_t memory_bytes=0;
        std::shared_ptr<GraphicsImage> visual;
        // Sampled stencil-aspect array view of a depth/stencil image (graphics_stencil_input), owned here. An input alias
        // sets `borrowed` to the image whose view it presents: the alias keeps that image alive and never destroys the view.
        VkImageView stencil_array_view=VK_NULL_HANDLE;std::shared_ptr<GraphicsImage> borrowed;
        std::uint64_t visual_taint=0,visual_taint_epoch=0; // ANYPS3_RENDER_SCALE_TRACE lineage: reconstruction reasons reaching this visual image this frame
        // Visual MSAA (ANYPS3_RENDER_SCALE_MSAA=1, graphics_ms_*): a 2-plane visual image may own a 2-sample working image `ms`
        // (views[0] attachment, array_view sampled colour/depth, stencil_array_view sampled stencil). While ms_current the
        // working image holds the content and the planes are stale; every access other than an MSAA draw first expands it
        // back into the planes (sample n -> plane n). ms_current/ms_stencil_* are recording-order state (recorder thread, or
        // the frontend after GraphicsBatch::sync); ms_wanted (an eligible draw targeted this image) is frontend state.
        std::shared_ptr<GraphicsImage> ms;bool ms_current=false,ms_wanted=false,ms_stencil_known=false;unsigned ms_stencil=0;
        std::shared_ptr<GraphicsPipeline> ms_expand,ms_expand_bits;std::array<VkFramebuffer,2> ms_expand_fb{};
        GraphicsImage(NativeVulkan* o,VkFormat f,unsigned w,unsigned h,unsigned l,bool d):owner(o),format(f),width(w),height(h),layers(l),depth(d){}
        ~GraphicsImage(){if(sampler)vkDestroySampler(owner->device,sampler,nullptr);
            for(auto f:ms_expand_fb)if(f)vkDestroyFramebuffer(owner->device,f,nullptr);
            for(auto v:views)vkDestroyImageView(owner->device,v,nullptr);
            if(array_view && !borrowed)vkDestroyImageView(owner->device,array_view,nullptr);
            if(stencil_array_view)vkDestroyImageView(owner->device,stencil_array_view,nullptr);
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
    struct GraphicsPush{float width,height,x,y;unsigned plane,layers;float shifts[8];float raster_scale[2];};
    static_assert(offsetof(GraphicsPush,raster_scale)==56 && sizeof(GraphicsPush)==64);
    struct GraphicsBatch;
    struct DrawJob {
        GraphicsBatch* batch=nullptr;std::shared_ptr<GraphicsPipeline> pipeline;GraphicsState s;
        std::array<std::shared_ptr<GraphicsImage>,4> colors;std::shared_ptr<GraphicsImage> depth;
        std::array<std::shared_ptr<GraphicsImage>,20> textures;std::array<std::shared_ptr<GraphicsBuffer>,16> raw_textures;
        std::array<std::array<float,2>,4> positions{};
        float bias_constant=0,bias_slope=0,min_bounds=0,max_bounds=1;
        unsigned vertices=0,first_vertex=0,query=0,passes=1;bool indexed=false,layered=false,multiview=false,defer_planes=false;
        std::uint64_t at=0,range=0,index_offset=0;
        std::shared_ptr<GraphicsPipeline> ms_pipeline;   // set: this visual half may draw into its attachments' MSAA working images
        void reset(){batch=nullptr;pipeline.reset();ms_pipeline.reset();depth.reset();for(auto& x:colors)x.reset();for(auto& x:textures)x.reset();for(auto& x:raw_textures)x.reset();}
    };
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
        std::vector<unsigned> phases;std::vector<const GraphicsPipeline*> phase_pipelines;std::vector<unsigned char> phase_visual;
        // Diagnostic (ANYPS3_RSX_VK_MARKS=1): BOTTOM_OF_PIPE timestamp chain; the time between two marks is attributed to the
        // kind of the later mark (GraphicsMark). Serializes the pipeline at every mark: attribution, not a performance mode.
        VkQueryPool marks=VK_NULL_HANDLE;unsigned mark_count=0;std::vector<unsigned> mark_kinds;
        bool coherent=false,recording=false,submitted=false,failed=false;
        std::uint64_t capacity=0,used=0;
        // Shader failures are consumed only after batch completion. Keep the
        // device words live until then instead of a graphics->transfer turn
        // and a four-byte copy after every draw (and every packed primitive).
        // paired_status_offsets: words a visual half shares with its native half (one slice per pair).
        std::vector<std::uint64_t> status_offsets,visual_status_offsets,paired_status_offsets;
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
        // Recorder-owned snapshots survive ring-slot reuse. Semantic command
        // boundaries drain them; generic recorder synchronization does not.
        std::vector<DrawJob> visual_jobs;
        bool replaying_visual=false;
        std::uint64_t visual_queued=0,visual_drains=0,visual_run_max=0;
        VkPipeline bound_graphics_pipeline=VK_NULL_HANDLE;
        bool dynamic_valid=false;
        std::array<unsigned,22> dynamic_state{};
        std::uint64_t dynamic_emits=0,dynamic_reuses=0,pipeline_binds=0;
        // Cached in NativeVulkan::graphics_framebuffers (device-wide). The images are weak: an entry never keeps an
        // image alive (a batch pins the attachments it records), and it matches only while all of them are alive,
        // because only then can no other view carry the keyed handles.
        struct Framebuffer {NativeVulkan* owner;VkFramebuffer handle;std::uint64_t last_use=0;std::vector<std::weak_ptr<GraphicsImage>> images;std::shared_ptr<GraphicsPipeline> pipeline;
            bool live()const{for(const auto& image:images)if(image.expired())return false;return true;}
            ~Framebuffer(){if(handle)vkDestroyFramebuffer(owner->device,handle,nullptr);}};
        struct FramebufferHash {
            std::size_t operator()(const std::array<std::uint64_t,10>& key)const{
                std::uint64_t h=0;for(auto word:key)h^=word+UINT64_C(0x9e3779b97f4a7c15)+(h<<6)+(h>>2);
                h^=h>>30;h*=UINT64_C(0xbf58476d1ce4e5b9);return h^(h>>27);
            }
        };
        GraphicsPins<Framebuffer> framebuffer_pins;
        GraphicsPins<GraphicsPipeline> pipeline_pins;
        std::uint64_t framebuffer_hits=0,framebuffer_misses=0;
        GraphicsImagePins pins;
        std::vector<std::shared_ptr<GraphicsBuffer>> buffer_pins;
        std::vector<std::shared_ptr<ComputePipeline>> compute_pins;
        double upload_ms=0,draw_ms=0,download_ms=0,wait_ms=0;
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
            framebuffer_pins.clear();pipeline_pins.clear();
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
        std::shared_ptr<GraphicsPipeline> ms_variant;   // same state and shaders with raster_samples=2 (visual MSAA), created on demand
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
    // Device-wide framebuffer cache (recording thread only: the draw recorder, or the owner after GraphicsBatch::sync).
    std::unordered_map<std::array<std::uint64_t,10>,std::shared_ptr<typename GraphicsBatch::Framebuffer>,typename GraphicsBatch::FramebufferHash> graphics_framebuffers;
public:
    std::size_t graphics_framebuffer_cache_size()const{return graphics_framebuffers.size();}
private:
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
    enum GraphicsSiteId:unsigned {SiteNone,SiteTexUpload,SiteSurfaceCopy,SiteTransfer,SiteUnpack,SitePack,SiteAspect,SiteDecode,SiteRawBuffer,SiteDeviceUpload,SitePredicate,SitePresent,SiteClear,SiteDeviceCopies,SiteVisual,SiteCount};
    static unsigned& graphics_last_kind(){static thread_local unsigned kind=MarkOther;return kind;}
    static unsigned& graphics_site_ref(){static thread_local unsigned site=SiteNone;return site;}
    struct GraphicsSite{unsigned previous;explicit GraphicsSite(unsigned s):previous(graphics_site_ref()){graphics_site_ref()=s;}~GraphicsSite(){graphics_site_ref()=previous;}};
    static void graphics_mark(GraphicsBatch& b,unsigned kind){
        if(!b.marks || b.mark_count>=4096)return;
        vkCmdWriteTimestamp(b.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,b.marks,b.mark_count++);b.mark_kinds.push_back((b.replaying_visual?unsigned(SiteVisual):graphics_site_ref())*16+kind);  // visual replays (incl. their planes) are one site
    }
    static void graphics_replay_planes(GraphicsBatch& b){
        for(unsigned plane=1;plane<b.plane_count;++plane){
            VkRenderPassBeginInfo pass{};pass.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;pass.renderPass=b.plane_pass;
            pass.framebuffer=b.plane_framebuffers[plane];pass.renderArea={{0,0},b.plane_extent};
            vkCmdBeginRenderPass(b.command,&pass,VK_SUBPASS_CONTENTS_INLINE);graphics_mark(b,MarkPassBegin);++b.render_passes;::RsxPerfCounters::add(::RsxPerfCounters::render_passes);
            const std::array<unsigned,22>* previous=b.dynamic_valid?&b.dynamic_state:nullptr;
            for(const auto& d:b.plane_draws){
                if(d.query!=UINT_MAX)vkCmdBeginQuery(b.command,b.occlusion,d.query+plane,d.query_flags);
                if(b.bound_graphics_pipeline!=d.pipeline){vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,d.pipeline);b.bound_graphics_pipeline=d.pipeline;++b.pipeline_binds;}
                vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,d.layout,0,1,&d.set,1,&d.offset);
                vkCmdPushConstants(b.command,d.layout,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush),&d.push[plane]);
                graphics_emit_dynamic(b.command,d.dynamic,previous);previous=&d.dynamic;
                if(d.indexed){vkCmdBindIndexBuffer(b.command,b.data_buffer,d.index_offset,VK_INDEX_TYPE_UINT32);vkCmdDrawIndexed(b.command,d.vertices,d.instances,d.first_vertex,0,0);}
                else vkCmdDraw(b.command,d.vertices,d.instances,d.first_vertex,0);
                if(d.query!=UINT_MAX)vkCmdEndQuery(b.command,b.occlusion,d.query+plane);
                graphics_mark(b,MarkDraw);
            }
            b.dynamic_state=b.plane_draws.back().dynamic;b.dynamic_valid=true;
            vkCmdEndRenderPass(b.command);graphics_mark(b,MarkPassEnd);::RsxPerfCounters::add(::RsxPerfCounters::pass_ends);
        }
        b.plane_draws.clear();b.plane_count=0;
    }
    static void graphics_close_pass_only(GraphicsBatch& b){
        if(b.active_framebuffer){vkCmdEndRenderPass(b.command);graphics_mark(b,MarkPassEnd);b.active_framebuffer=VK_NULL_HANDLE;::RsxPerfCounters::add(::RsxPerfCounters::pass_ends);}
        if(!b.plane_draws.empty())graphics_replay_planes(b);
    }
    static void graphics_end_pass(GraphicsBatch& b){
        b.sync();graphics_close_pass_only(b);
        if(b.replaying_visual || b.visual_jobs.empty())return;
        b.replaying_visual=true;++b.visual_drains;
        try{
            for(auto& job:b.visual_jobs)b.owner->graphics_record_draw(job);
            graphics_close_pass_only(b);
            b.visual_jobs.clear();b.replaying_visual=false;
        }catch(...){b.replaying_visual=false;b.failed=true;throw;}
    }
    // ANYPS3_RSX_BARRIER_HOST=1 keeps the former HOST stage/HOST_WRITE source in every graphics barrier. Default 0: every host
    // write a batch's commands read (staging, uploads) happens before its vkQueueSubmit, and the submission itself makes those
    // writes visible to the device (Vulkan host write ordering guarantee); no graphics path writes host memory a recorded
    // command consumes after submission. The GPU-GPU part of the barrier is unchanged.
    static bool graphics_barrier_host(){static const bool host=[]{const char* v=std::getenv("ANYPS3_RSX_BARRIER_HOST");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_BARRIER_HOST");}
        return v && !std::strcmp(v,"1");}();return host;}
    static void graphics_barrier(GraphicsBatch& b){
        graphics_end_pass(b);graphics_mark(b,graphics_last_kind());graphics_last_kind()=MarkOther;
        const bool host=graphics_barrier_host();
        VkMemoryBarrier m{};m.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        m.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT|VK_ACCESS_MEMORY_READ_BIT|(host?VK_ACCESS_HOST_WRITE_BIT:0);
        m.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT|(host?VK_PIPELINE_STAGE_HOST_BIT:0),
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
        const std::string* vs_canonical=&vs,*fs_canonical=&fs;
        const auto a=shader_identity(vs,vs_owner,&vs_canonical),b=shader_identity(fs,fs_owner,&fs_canonical);
        // Pointer equality with the canonical text owner proves equal bytes (shader_identity compared them once).
        const auto same_vs=[&](const GraphicsPipeline& p){return p.vs_source.get()==&vs || p.vs_source.get()==vs_canonical || (++shader_text_compares,*p.vs_source==vs);};
        const auto same_fs=[&](const GraphicsPipeline& p){return p.fs_source.get()==&fs || p.fs_source.get()==fs_canonical || (++shader_text_compares,*p.fs_source==fs);};
        std::array<std::uint64_t,34> fast_key{};fast_key[0]=a;fast_key[1]=b;unsigned field=0;
        auto field_=[&](unsigned v){const auto word=2+field/2;fast_key[word]|=std::uint64_t(v)<<(32*(field%2));++field;};
        field_(s.colors);field_(s.color);field_(s.depth);
        for(unsigned i=0;i<s.colors;++i){const auto& v=s.blend[i];field_(v.blendEnable);field_(v.srcColorBlendFactor);field_(v.dstColorBlendFactor);field_(v.colorBlendOp);field_(v.srcAlphaBlendFactor);field_(v.dstAlphaBlendFactor);field_(v.alphaBlendOp);field_(v.colorWriteMask);}
        field_(s.z.depthTestEnable);field_(s.z.depthWriteEnable);field_(s.z.depthCompareOp);field_(s.z.stencilTestEnable);field_(s.z.depthBoundsTestEnable);
        for(const auto* v:{&s.z.front,&s.z.back}){field_(v->failOp);field_(v->passOp);field_(v->depthFailOp);field_(v->compareOp);}
        field_(s.raster.frontFace);field_(s.raster.cullMode);field_(s.raster.depthBiasEnable);field_(s.logic_enable);field_(s.logic);field_(multiview?s.samples:0);field_(s.raster_samples);
        auto cached=graphics_pipeline_fast.find(fast_key);
        if(cached!=graphics_pipeline_fast.end())if(auto hit=cached->second.lock()){
            if(same_vs(*hit) && same_fs(*hit)){
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
        add(s.raster.frontFace);add(s.raster.cullMode);add(s.raster.depthBiasEnable);add(s.logic_enable);add(s.logic);add(multiview?s.samples:0);add(s.raster_samples);
        parts[5]=state_key.substr(split);
        key+=state_key;auto it=graphics_pipelines.find(key);
        // Hashes accelerate lookup; exact bytes remain authoritative even on a collision.
        if(it!=graphics_pipelines.end() && (!same_vs(*it->second) || !same_fs(*it->second))){
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
        auto p=std::make_shared<GraphicsPipeline>();p->device=device;p->last_use=++cache_clock;p->vs_source=vs_owner?shader_canonical_owner(vs_owner):std::make_shared<const std::string>(vs);p->fs_source=fs_owner?shader_canonical_owner(fs_owner):std::make_shared<const std::string>(fs);
        p->diagnostic_parts=parts;p->diagnostic_identity=identity;last_pipeline_diagnostic=p;
        std::vector<VkAttachmentDescription> attachments;
        std::vector<VkAttachmentReference> colors;
        for(unsigned n=0;n<s.colors+(s.depth!=VK_FORMAT_UNDEFINED);++n){
            const bool d=n==s.colors;VkAttachmentDescription a{};a.format=d?s.depth:s.color;a.samples=VkSampleCountFlagBits(s.raster_samples);
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
        VkPushConstantRange push{VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush)};
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
        VkPipelineMultisampleStateCreateInfo ms{};ms.sType=VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;ms.rasterizationSamples=VkSampleCountFlagBits(s.raster_samples);
        if(s.raster_samples!=1 && (s.raster_samples!=2 || multiview))throw std::runtime_error("RSX graphics pipeline raster samples");
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
    struct DrawProfile {double ms=0;std::uint64_t draws=0;std::size_t vs_bytes=0,fs_bytes=0;std::string fs_head;bool visual=false;};
    std::unordered_map<std::string,DrawProfile> draw_profile;std::uint64_t draw_profile_draws=0;
    // Native and visual halves of a pair share the pipeline; they are profiled as separate rows (key prefix N/V).
    void draw_profile_add(const GraphicsPipeline& p,double ms,bool visual=false){
        if(!p.vs_source || !p.fs_source)return;
        auto& e=draw_profile[std::string(visual?"V":"N")+*p.vs_source+'\0'+*p.fs_source];e.ms+=ms;++e.draws;e.vs_bytes=p.vs_source->size();e.fs_bytes=p.fs_source->size();e.visual=visual;
        if(++draw_profile_draws%20000==0)draw_profile_print();
    }
    void draw_profile_print(){
        std::vector<const DrawProfile*> rows;double total=0,visual_ms=0;std::uint64_t visual_draws=0;for(const auto& e:draw_profile){rows.push_back(&e.second);total+=e.second.ms;if(e.second.visual){visual_ms+=e.second.ms;visual_draws+=e.second.draws;}}
        std::sort(rows.begin(),rows.end(),[](const auto* a,const auto* c){return a->ms>c->ms;});
        std::fprintf(stderr,"RSX VK DRAW PROFILE draws=%llu shaders=%zu total_ms=%.3f visual_draws=%llu visual_ms=%.3f\n",(unsigned long long)draw_profile_draws,rows.size(),total,(unsigned long long)visual_draws,visual_ms);
        for(std::size_t i=0;i<rows.size() && i<20;++i){const auto& e=*rows[i];
            for(const auto& kv:draw_profile)if(&kv.second==rows[i]){
                std::fprintf(stderr,"RSX VK DRAW PROFILE rank=%zu ms=%.3f draws=%llu us_per_draw=%.2f vs_bytes=%zu fs_bytes=%zu visual=%d fs_hash=%016zx\n",i,e.ms,(unsigned long long)e.draws,1000*e.ms/std::max<std::uint64_t>(1,e.draws),e.vs_bytes,e.fs_bytes,int(e.visual),std::hash<std::string>{}(kv.first.substr(1)));
                if(const char* dir=std::getenv("ANYPS3_RSX_VK_PROFILE_DIR")){char name[64];std::snprintf(name,sizeof(name),"/shader-%016zx.glsl",std::hash<std::string>{}(kv.first.substr(1)));
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
#ifndef ANYPS3_RSX_GRAPHICS_STAGING_BYTES
#define ANYPS3_RSX_GRAPHICS_STAGING_BYTES (64*1024*1024)   // default staging of a graphics batch; native tests may compile a smaller one
#endif
    std::unique_ptr<GraphicsBatch> graphics_batch(std::uint64_t bytes=ANYPS3_RSX_GRAPHICS_STAGING_BYTES){
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
    unsigned graphics_render_scale=1;
    bool graphics_visual_batch=true;
    static bool graphics_read_visual_batch(){
        const char* value=std::getenv("ANYPS3_RENDER_SCALE_BATCH");
        if(!value || !std::strcmp(value,"1"))return true;
        if(!std::strcmp(value,"0"))return false;
        throw std::runtime_error("ANYPS3_RENDER_SCALE_BATCH must be 0 or 1");
    }
    std::uint64_t graphics_visual_draws=0,graphics_visual_native_copies=0,graphics_visual_host_invalidations=0;
    std::uint64_t graphics_visual_queued_total=0,graphics_visual_drains_total=0,graphics_visual_run_max=0;
    std::shared_ptr<GraphicsPipeline> graphics_paired_pipeline; // handed from a native draw to its visual half only
    static unsigned graphics_read_render_scale(){
        const char* value=std::getenv("ANYPS3_RENDER_SCALE");
        if(!value)return 1;
        if(value[0]<'1' || value[0]>'4' || value[1])throw std::runtime_error("ANYPS3_RENDER_SCALE must be an integer from 1 to 4");
        return unsigned(value[0]-'0');
    }
    std::uint64_t graphics_image_creations=0,graphics_visual_rtt_fast=0;
    // visual_format: the visual companion's format when it differs from the native one (display-only images whose visual
    // half is written by its own draws, e.g. an 8-bit conversion output); VK_FORMAT_UNDEFINED = same format.
    std::shared_ptr<GraphicsImage> graphics_image(GraphicsBatch& b,VkFormat f,unsigned w,unsigned h,unsigned layers,bool depth=false,bool pair=true,VkFormat visual_format=VK_FORMAT_UNDEFINED){b.sync();
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
        b.pins.push_back(image);
        if(pair && graphics_render_scale>1){
            const auto scale=graphics_render_scale;
            if(w>graphics_properties.limits.maxImageDimension2D/scale || h>graphics_properties.limits.maxImageDimension2D/scale)
                throw std::runtime_error("RSX visual image exceeds device limit");
            image->visual=graphics_image(b,visual_format!=VK_FORMAT_UNDEFINED?visual_format:f,w*scale,h*scale,layers,depth,false);
            // Parent cache accounting includes the paired allocation. Batch
            // pins may conservatively count it again; never undercount it.
            image->memory_bytes+=image->visual->memory_bytes;
        }
        return image;
    }
    // Native uploads/unsupported visual conversions are reconstructed from
    // authoritative pixels. This is counted separately from visual geometry,
    // per reason: the internal site plus an optional caller label
    // (graphics_visual_why, set by VisualWhy around the call).
    struct VisualSyncReason {std::string name;std::uint64_t count=0,pixels=0;unsigned logged=0;};
    std::vector<VisualSyncReason> graphics_visual_reasons;
    const char* graphics_visual_why=nullptr;
    struct VisualWhy {NativeVulkan& vk;const char* previous;VisualWhy(NativeVulkan& v,const char* why):vk(v),previous(v.graphics_visual_why){vk.graphics_visual_why=why;}~VisualWhy(){vk.graphics_visual_why=previous;}};
    // ANYPS3_RENDER_SCALE_TRACE=1: one-shot logs per reason and visual lineage
    // (which reconstruction reasons reach the presented image). Diagnostic only.
    static bool graphics_visual_trace(){
        static const bool on=[]{const char* v=std::getenv("ANYPS3_RENDER_SCALE_TRACE");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("ANYPS3_RENDER_SCALE_TRACE must be 0 or 1");
            return v && !std::strcmp(v,"1");}();
        return on;
    }
    // Lineage is frame-scoped: the first write of a presented frame replaces an
    // image's mask (render targets are rewritten per frame); later writes OR.
    std::uint64_t graphics_visual_epoch=0;
    void graphics_visual_taint(GraphicsImage& to,std::uint64_t taint,bool replace){
        if(replace || to.visual_taint_epoch!=graphics_visual_epoch)to.visual_taint=0;
        to.visual_taint|=taint;to.visual_taint_epoch=graphics_visual_epoch;
    }
    std::string graphics_visual_taint_names(std::uint64_t taint)const{
        std::string out;for(unsigned i=0;i<64 && i<graphics_visual_reasons.size();++i)if(taint>>i&1){if(!out.empty())out+=',';out+=graphics_visual_reasons[i].name;}
        return out.empty()?std::string("none"):out;
    }
    void graphics_visual_sync(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,const char* site){
        if(!image || !image->visual)return;
        b.sync();graphics_ms_planes(b,image.get());const auto& to=image->visual;
        VkFormatProperties props{};vkGetPhysicalDeviceFormatProperties(physical,image->format,&props);
        if((props.optimalTilingFeatures&(VK_FORMAT_FEATURE_BLIT_SRC_BIT|VK_FORMAT_FEATURE_BLIT_DST_BIT))!=(VK_FORMAT_FEATURE_BLIT_SRC_BIT|VK_FORMAT_FEATURE_BLIT_DST_BIT))
            throw std::runtime_error("RSX visual native upload format cannot blit");
        graphics_begin(b);graphics_barrier(b);
        const auto aspects=image->depth?(image->format==VK_FORMAT_D24_UNORM_S8_UINT?std::vector<VkImageAspectFlags>{VK_IMAGE_ASPECT_DEPTH_BIT,VK_IMAGE_ASPECT_STENCIL_BIT}:std::vector<VkImageAspectFlags>{VK_IMAGE_ASPECT_DEPTH_BIT}):std::vector<VkImageAspectFlags>{VK_IMAGE_ASPECT_COLOR_BIT};
        for(auto aspect:aspects){VkImageBlit c{};c.srcSubresource={aspect,0,0,image->layers};c.dstSubresource=c.srcSubresource;
            c.srcOffsets[1]={int(image->width),int(image->height),1};c.dstOffsets[1]={int(to->width),int(to->height),1};
            vkCmdBlitImage(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,to->image,VK_IMAGE_LAYOUT_GENERAL,1,&c,VK_FILTER_NEAREST);}
        b.pins.push_back(image);b.pins.push_back(to);++graphics_visual_native_copies;
        std::string name=graphics_visual_why?std::string(graphics_visual_why)+"/"+site:std::string(site);
        unsigned reason=0;while(reason<graphics_visual_reasons.size() && graphics_visual_reasons[reason].name!=name)++reason;
        if(reason==graphics_visual_reasons.size())graphics_visual_reasons.push_back({std::move(name)});
        auto& r=graphics_visual_reasons[reason];++r.count;r.pixels+=std::uint64_t(image->width)*image->height*image->layers;
        graphics_visual_taint(*to,std::uint64_t(1)<<std::min(reason,63u),true);
        if(graphics_visual_trace() && r.logged<4){++r.logged;
            std::fprintf(stderr,"RENDER_SCALE_SYNC reason=%s native=%ux%ux%u format=%d depth=%d count=%llu draws=%llu\n",r.name.c_str(),image->width,image->height,image->layers,int(image->format),int(image->depth),(unsigned long long)r.count,(unsigned long long)graphics_draws);}
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
''' + READBACK_MEMBERS + UPLOAD_MEMBERS + DECODE_MEMBERS + BLIT_MEMBERS + r'''
    // Images stay GENERAL, allowing explicit cross-draw transfer barriers.
    // Would graphics_space(b, bytes) (graphics_transfer of that size) succeed now? (Same alignment and bounds; no state change.)
    bool graphics_space_fits(const GraphicsBatch& b,std::uint64_t bytes)const{
        const auto alignment=std::max<std::uint64_t>(256,graphics_properties.limits.minStorageBufferOffsetAlignment);
        const auto at=(b.used+alignment-1)&~(alignment-1);
        return at<=b.capacity && bytes<=b.capacity-at;
    }
    std::uint64_t graphics_transfer(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,VkImageAspectFlags aspect,unsigned bpp,const void* data=nullptr){b.sync();
        GraphicsSite site(SiteTransfer);
        if(graphics_ms_any && image->ms){graphics_begin(b);graphics_end_pass(b);if(image->ms_current)graphics_ms_expand(b,*image);}   // a visual image itself
        graphics_begin(b);const auto bytes=std::uint64_t(image->width)*image->height*image->layers*bpp;
        const auto at=data?graphics_upload(b,data,bytes):graphics_space(b,bytes);
        graphics_barrier(b);VkBufferImageCopy copy{};copy.bufferOffset=at;copy.imageSubresource={aspect,0,0,image->layers};copy.imageExtent={image->width,image->height,1};
        graphics_stamp(b,data?0:2);
        if(data)vkCmdCopyBufferToImage(b.command,b.buffer,image->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);
        else {vkCmdCopyImageToBuffer(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,b.buffer,1,&copy);b.download_bytes+=bytes;}
        graphics_stamp(b,0,true);b.pins.push_back(image);if(data)graphics_visual_sync(b,image,"transfer");return at;
    }
    std::uint64_t graphics_visual_aspect_copies=0,graphics_visual_aspect_drawn=0;
    // ANYPS3_RENDER_SCALE_ASPECT_DRAW=0 restores the visual buffer round trip (A/B and test switch); default 1.
    static bool graphics_visual_aspect_draws(){static const bool value=[]{const char* v=std::getenv("ANYPS3_RENDER_SCALE_ASPECT_DRAW");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RENDER_SCALE_ASPECT_DRAW");}
            return !v || !std::strcmp(v,"1");}();
        return value;}
    // An input alias presenting the stencil aspect of a D24S8 image as its sampled array view (usampler2DArray).
    std::shared_ptr<GraphicsImage> graphics_stencil_input(const std::shared_ptr<GraphicsImage>& image){
        if(!image->depth || image->format!=VK_FORMAT_D24_UNORM_S8_UINT)throw std::runtime_error("RSX stencil input requires D24S8");
        if(!image->stencil_array_view){VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=image->image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D_ARRAY;vi.format=image->format;
            vi.subresourceRange={VK_IMAGE_ASPECT_STENCIL_BIT,0,1,0,image->layers};check(vkCreateImageView(device,&vi,nullptr,&image->stencil_array_view),"graphics stencil input view");}
        auto alias=std::make_shared<GraphicsImage>(this,image->format,image->width,image->height,image->layers,true);
        alias->array_view=image->stencil_array_view;alias->borrowed=image;alias->memory_bytes=0;return alias;
    }
    // Visual half of a one-word typeless reinterpretation. The visual image is display-only (never guest-visible; native
    // bytes keep the exact copy path), so one draw per plane reads the visual source through a sampled view and writes the
    // word image directly instead of the S*S-sized buffer round trip (two copies, two full barriers). Depth words carry
    // the 24-bit depth in the low bits like the copied D24 depth aspect (consumers mask 0xffffff); float source -> 24-bit
    // integer goes through binary64 where available. Stencil and R32 float bits are exact.
    void graphics_visual_aspect_draw(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& source,VkImageAspectFlags aspect,const std::shared_ptr<GraphicsImage>& result){
        const bool stencil=aspect==VK_IMAGE_ASPECT_STENCIL_BIT,depth=aspect==VK_IMAGE_ASPECT_DEPTH_BIT;
        if(result->width!=source->width || result->height!=source->height || result->layers!=source->layers || result->visual || source->visual ||
           result->format!=(stencil?VK_FORMAT_R8_UINT:VK_FORMAT_R32_UINT) || (!stencil && !depth && source->format!=VK_FORMAT_R32_SFLOAT))
            throw std::runtime_error("RSX visual aspect draw layout");
        static const auto vs=std::make_shared<const std::string>("#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2.0-1.0,0,1);}");
        auto text=[](const char* sampler,const char* out,const char* value){return std::make_shared<const std::string>(std::string("#version 450\nlayout(binding=17) uniform ")+sampler+
            " src_;layout(location=0) out "+out+" o;layout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane_;};\nvoid main(){ivec3 p=ivec3(ivec2(gl_FragCoord.xy),int(plane_));"+value+"}\n");};
        static const auto fs_depth64=text("sampler2DArray","uint","o=uint(round(double(clamp(texelFetch(src_,p,0).r,0.0,1.0))*16777215.0lf));");
        static const auto fs_depth32=text("sampler2DArray","uint","o=uint(round(clamp(texelFetch(src_,p,0).r,0.0,1.0)*16777215.0));");
        static const auto fs_stencil=text("usampler2DArray","uint","o=texelFetch(src_,p,0).r&255u;");
        static const auto fs_float=text("sampler2DArray","uint","o=floatBitsToUint(texelFetch(src_,p,0).r);");
        const auto& fs=stencil?fs_stencil:depth?(graphics_float64()?fs_depth64:fs_depth32):fs_float;
        GraphicsState s{};s.visual_pass=true;s.width=result->width;s.height=result->height;s.colors=1;s.samples=result->layers;s.color=result->format;
        s.scissor={{0,0},{result->width,result->height}};s.blend[0].colorWriteMask=15;
        std::array<std::shared_ptr<GraphicsImage>,4> colors{};colors[0]=result;
        std::array<std::shared_ptr<GraphicsImage>,20> inputs{};inputs[16]=stencil?graphics_stencil_input(source):source;
        const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{.5f,.5f},{.5f,.5f},{.5f,.5f}}};
        const std::vector<unsigned> data(16,0);
        graphics_draw(b,s,colors,{},*vs,*fs,data,3,positions,0,0,0,1,inputs,0,UINT64_MAX,{},vs,fs);
    }
    std::shared_ptr<GraphicsImage> graphics_aspect_image(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& source,VkImageAspectFlags aspect,const std::shared_ptr<GraphicsImage>& destination={},std::shared_ptr<GraphicsBuffer>* scratch=nullptr){b.sync();
        graphics_ms_planes(b,source.get());graphics_ms_planes(b,destination.get());
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
        // One word per texel keeps the scaled layout: the visual companion is
        // reinterpreted the same way after the native bytes (16-byte aligned).
        // Several words per texel would interleave subpixels; those rebuild.
        const bool visual=words==1 && source->visual && result->visual && graphics_render_scale>1;
        const auto visual_at=(bytes+15)&~UINT64_C(15),total=visual && !graphics_visual_aspect_draws()?visual_at+bytes*graphics_render_scale*graphics_render_scale:bytes;
        // A device-local transfer buffer permits byte-preserving depth/color
        // reinterpretation without an illegal cross-aspect vkCmdCopyImage,
        // normalized float reconstruction or any host-visible staging/read.
        auto transfer=scratch?*scratch:std::shared_ptr<GraphicsBuffer>{};
        if(transfer && transfer->size!=total)throw std::runtime_error("RSX cached aspect buffer mismatch");
        if(!transfer){transfer=std::make_shared<GraphicsBuffer>(this,total);
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=total;bi.usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        check(vkCreateBuffer(device,&bi,nullptr,&transfer->buffer),"graphics typeless buffer");
        VkMemoryRequirements requirements{};vkGetBufferMemoryRequirements(device,transfer->buffer,&requirements);
        VkMemoryAllocateInfo allocation{};allocation.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;allocation.allocationSize=requirements.size;allocation.memoryTypeIndex=memory_type(requirements.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        transfer->memory_bytes=requirements.size;check(vkAllocateMemory(device,&allocation,nullptr,&transfer->memory),"graphics typeless memory");check(vkBindBufferMemory(device,transfer->buffer,transfer->memory,0),"graphics typeless bind");if(scratch)*scratch=transfer;}
        graphics_begin(b);graphics_barrier(b);VkBufferImageCopy copy{};copy.imageSubresource={aspect,0,0,source->layers};copy.imageExtent={source->width,source->height,1};
        vkCmdCopyImageToBuffer(b.command,source->image,VK_IMAGE_LAYOUT_GENERAL,transfer->buffer,1,&copy);graphics_barrier(b);
        copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_COLOR_BIT;copy.imageExtent.width=result->width;
        vkCmdCopyBufferToImage(b.command,transfer->buffer,result->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);graphics_barrier(b);
        if(visual){
            if(graphics_visual_aspect_draws()){graphics_visual_aspect_draw(b,source->visual,aspect,result->visual);++graphics_visual_aspect_drawn;}
            else{copy.bufferOffset=visual_at;copy.imageSubresource.aspectMask=aspect;copy.imageExtent={source->visual->width,source->visual->height,1};
            vkCmdCopyImageToBuffer(b.command,source->visual->image,VK_IMAGE_LAYOUT_GENERAL,transfer->buffer,1,&copy);graphics_barrier(b);
            copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_COLOR_BIT;
            vkCmdCopyBufferToImage(b.command,transfer->buffer,result->visual->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);graphics_barrier(b);}
            graphics_visual_taint(*result->visual,source->visual->visual_taint,true);++graphics_visual_aspect_copies;}
        b.buffer_pins.push_back(transfer);b.pins.push_back(source);b.pins.push_back(result);if(!visual)graphics_visual_sync(b,result,"aspect");return result;
    }
    std::uint64_t graphics_full_clears=0;
    void graphics_clear_image(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,const VkClearColorValue& color,float depth,unsigned stencil,VkImageAspectFlags aspects){b.sync();
        GraphicsSite site(SiteClear);
        graphics_begin(b);graphics_barrier(b);
        // Visual MSAA: a clear of every aspect makes the working image current (its planes are then stale); a partial clear
        // of a current working image stays in it. Anything else clears the planes (a current working image is then absent).
        if(graphics_visual_msaa() && image->ms_wanted && !image->ms)graphics_ms_prepare(b,image);
        if(image->ms){
            const VkImageAspectFlags all=image->depth?VK_IMAGE_ASPECT_DEPTH_BIT|(image->format==VK_FORMAT_D24_UNORM_S8_UINT?VK_IMAGE_ASPECT_STENCIL_BIT:0):VK_IMAGE_ASPECT_COLOR_BIT;
            if(image->ms_current || (aspects&all)==all){
                const VkImageSubresourceRange whole{aspects,0,1,0,1};
                if(image->depth){const VkClearDepthStencilValue value{depth,stencil};vkCmdClearDepthStencilImage(b.command,image->ms->image,VK_IMAGE_LAYOUT_GENERAL,&value,1,&whole);}
                else vkCmdClearColorImage(b.command,image->ms->image,VK_IMAGE_LAYOUT_GENERAL,&color,1,&whole);
                if(aspects&VK_IMAGE_ASPECT_STENCIL_BIT){image->ms_stencil_known=true;image->ms_stencil=stencil&255;}
                image->ms_current=true;b.pins.push_back(image);++graphics_ms_clears;graphics_visual_taint(*image,0,true);return;
            }
        }
        const VkImageSubresourceRange range{aspects,0,1,0,image->layers};
        if(image->depth){const VkClearDepthStencilValue value{depth,stencil};vkCmdClearDepthStencilImage(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,&value,1,&range);}
        else vkCmdClearColorImage(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,&color,1,&range);
        b.pins.push_back(image);++graphics_full_clears;graphics_visual_taint(*image,0,true);
        if(image->visual)graphics_clear_image(b,image->visual,color,depth,stencil,aspects);
    }
    void graphics_copy(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& from,const std::shared_ptr<GraphicsImage>& to){b.sync();
        if(from==to)return;
        graphics_ms_planes(b,from.get());graphics_ms_planes(b,to.get());
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
        graphics_visual_taint(*to,from->visual_taint,true);
        if(to->visual){if(from->visual)graphics_copy(b,from->visual,to->visual);else graphics_visual_sync(b,to,"copy_unpaired");}
    }
    // Visual history through a native byte permutation (alias merge, equal
    // footprint rebind): regions are in visual units, the visual blocks of whole
    // native texels already proven by the caller to carry identical guest bytes
    // (visual_history_plan maps them, including multi-sample grids).
    std::uint64_t graphics_visual_history_copies=0,graphics_visual_history_texels=0;
    void graphics_visual_history(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& from,const std::shared_ptr<GraphicsImage>& to,
            const std::vector<VkImageCopy>& regions,std::uint64_t texels){b.sync();
        if(regions.empty())return;
        graphics_ms_planes(b,from.get());graphics_ms_planes(b,to.get());
        if(!from->visual || !to->visual || from->depth!=to->depth || from->visual->width!=from->width*graphics_render_scale ||
           to->visual->width!=to->width*graphics_render_scale || from->visual->height!=from->height*graphics_render_scale || to->visual->height!=to->height*graphics_render_scale)
            throw std::runtime_error("RSX visual history pair mismatch");
        const bool rgba_compatible=(from->format==VK_FORMAT_R8G8B8A8_SRGB && to->format==VK_FORMAT_R8G8B8A8_UNORM) ||
            (to->format==VK_FORMAT_R8G8B8A8_SRGB && from->format==VK_FORMAT_R8G8B8A8_UNORM);
        if(from->format!=to->format && !rgba_compatible)throw std::runtime_error("RSX visual history format mismatch");
        GraphicsSite site(SiteSurfaceCopy);
        std::vector<VkImageAspectFlags> aspects{from->depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT};
        if(from->format==VK_FORMAT_D24_UNORM_S8_UINT)aspects.push_back(VK_IMAGE_ASPECT_STENCIL_BIT);
        std::vector<VkImageCopy> scaled;scaled.reserve(regions.size()*aspects.size());
        for(const auto aspect:aspects)for(auto c:regions){
            const auto& v=from->visual;const auto& w=to->visual;
            if(c.srcSubresource.baseArrayLayer>=from->layers || c.dstSubresource.baseArrayLayer>=to->layers || c.extent.depth!=1 ||
               c.srcOffset.x<0 || c.srcOffset.y<0 || c.dstOffset.x<0 || c.dstOffset.y<0 ||
               unsigned(c.srcOffset.x)+c.extent.width>v->width || unsigned(c.srcOffset.y)+c.extent.height>v->height ||
               unsigned(c.dstOffset.x)+c.extent.width>w->width || unsigned(c.dstOffset.y)+c.extent.height>w->height)
                throw std::runtime_error("RSX visual history region bounds");
            c.srcSubresource.aspectMask=c.dstSubresource.aspectMask=aspect;c.srcSubresource.mipLevel=c.dstSubresource.mipLevel=0;c.srcSubresource.layerCount=c.dstSubresource.layerCount=1;
            c.srcOffset.z=c.dstOffset.z=0;scaled.push_back(c);}
        graphics_begin(b);graphics_barrier(b);
        vkCmdCopyImage(b.command,from->visual->image,VK_IMAGE_LAYOUT_GENERAL,to->visual->image,VK_IMAGE_LAYOUT_GENERAL,unsigned(scaled.size()),scaled.data());
        b.pins.push_back(from);b.pins.push_back(to);++graphics_visual_history_copies;graphics_visual_history_texels+=texels;graphics_visual_taint(*to->visual,from->visual->visual_taint,false);
    }
    // ---- visual MSAA (ANYPS3_RENDER_SCALE_MSAA=1; display-only, never guest-visible) ----
    // A 2-sample visual image (two single-sample planes, sample n = plane n, RSX 2x positions (.75,.75)/(.25,.25) = the Vulkan
    // standard 2x locations at the visual raster) normally draws each plane separately: vertex + fragment work twice. The
    // generated fragment shader already shades at the pixel centre and only coverage/depth are per plane, so a draw whose
    // fragment work does not depend on the plane (GraphicsState::ms_ok) gives the same samples when rasterized once into a
    // 2-sample working image with shift 0: per-sample coverage, depth/stencil test, blend; one shading per pixel. The
    // working image becomes current only through a full clear (graphics_clear_image), so no plane->sample load exists;
    // any other access expands it back into the planes first (graphics_ms_expand: one full-screen pass per plane; depth by
    // gl_FragDepth, stencil by a REPLACE of a known cleared value or 8 bit-plane draws, since the NVIDIA driver has no
    // VK_EXT_shader_stencil_export). Default 0 (planes only).
    static bool graphics_visual_msaa(){static const bool value=[]{const char* v=std::getenv("ANYPS3_RENDER_SCALE_MSAA");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RENDER_SCALE_MSAA");}
            return v && !std::strcmp(v,"1");}();
        return value;}
    std::atomic<std::uint64_t> graphics_ms_draws{0},graphics_ms_plane_draws{0},graphics_ms_expands{0},graphics_ms_bit_expands{0},graphics_ms_clears{0},graphics_ms_images{0};
    bool graphics_ms_any=false;   // set once any working image exists (frontend, published to the recorder through the job queue)
    // Formats whose 2-sample working image round-trips exactly through a sampled texelFetch and an attachment write.
    std::array<signed char,256> graphics_ms_format_known{};   // 0 unknown, 1 yes, -1 no (formats < 256)
    bool graphics_ms_format(VkFormat f,bool depth){
        if(unsigned(f)<256 && graphics_ms_format_known[f])return graphics_ms_format_known[f]>0;
        const bool yes=graphics_ms_format_query(f,depth);if(unsigned(f)<256)graphics_ms_format_known[f]=yes?1:-1;return yes;
    }
    bool graphics_ms_format_query(VkFormat f,bool depth){
        const auto& l=graphics_properties.limits;if(!l.standardSampleLocations)return false;
        const bool color=f==VK_FORMAT_R8G8B8A8_UNORM || f==VK_FORMAT_R5G6B5_UNORM_PACK16 || f==VK_FORMAT_R16G16B16A16_SFLOAT || f==VK_FORMAT_R32G32B32A32_SFLOAT || f==VK_FORMAT_R32_SFLOAT;
        if(depth?(f!=VK_FORMAT_D24_UNORM_S8_UINT && f!=VK_FORMAT_D16_UNORM):!color)return false;
        if(depth?!(l.framebufferDepthSampleCounts&l.sampledImageDepthSampleCounts&VK_SAMPLE_COUNT_2_BIT) || (f==VK_FORMAT_D24_UNORM_S8_UINT && !(l.framebufferStencilSampleCounts&l.sampledImageStencilSampleCounts&VK_SAMPLE_COUNT_2_BIT))
                :!(l.framebufferColorSampleCounts&l.sampledImageColorSampleCounts&VK_SAMPLE_COUNT_2_BIT))return false;
        VkImageFormatProperties p{};
        const VkImageUsageFlags usage=VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_SAMPLED_BIT|(depth?VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT:VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT);
        return vkGetPhysicalDeviceImageFormatProperties(physical,f,VK_IMAGE_TYPE_2D,VK_IMAGE_TILING_OPTIMAL,usage,0,&p)==VK_SUCCESS && (p.sampleCounts&VK_SAMPLE_COUNT_2_BIT);
    }
    // Frontend: creates the working image of visual image v, its expand pipelines and per-plane framebuffers. Outside a pass.
    bool graphics_ms_prepare(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& v){
        if(v->ms)return true;
        if(v->layers!=2 || v->visual || v->borrowed || !graphics_ms_format(v->format,v->depth)){v->ms_wanted=false;return false;}
        auto m=std::make_shared<GraphicsImage>(this,v->format,v->width,v->height,1,v->depth);
        VkImageCreateInfo ii{};ii.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ii.imageType=VK_IMAGE_TYPE_2D;ii.format=v->format;ii.extent={v->width,v->height,1};ii.mipLevels=1;ii.arrayLayers=1;
        ii.samples=VK_SAMPLE_COUNT_2_BIT;ii.tiling=VK_IMAGE_TILING_OPTIMAL;ii.usage=VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_SAMPLED_BIT|(v->depth?VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT:VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT);
        check(vkCreateImage(device,&ii,nullptr,&m->image),"visual msaa image");
        VkMemoryRequirements r{};vkGetImageMemoryRequirements(device,m->image,&r);VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        m->memory_bytes=r.size;check(vkAllocateMemory(device,&ai,nullptr,&m->memory),"visual msaa memory");check(vkBindImageMemory(device,m->image,m->memory,0),"visual msaa bind");
        const bool stencil=v->format==VK_FORMAT_D24_UNORM_S8_UINT;
        const VkImageAspectFlags all=v->depth?VK_IMAGE_ASPECT_DEPTH_BIT|(stencil?VK_IMAGE_ASPECT_STENCIL_BIT:0):VK_IMAGE_ASPECT_COLOR_BIT;
        auto view=[&](VkImageViewType type,VkImageAspectFlags aspect){VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=m->image;vi.viewType=type;vi.format=v->format;
            vi.subresourceRange={aspect,0,1,0,1};VkImageView out;check(vkCreateImageView(device,&vi,nullptr,&out),"visual msaa view");return out;};
        m->views.push_back(view(VK_IMAGE_VIEW_TYPE_2D,all));
        m->array_view=view(VK_IMAGE_VIEW_TYPE_2D_ARRAY,v->depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT);
        if(stencil)m->stencil_array_view=view(VK_IMAGE_VIEW_TYPE_2D_ARRAY,VK_IMAGE_ASPECT_STENCIL_BIT);
        graphics_begin(b);graphics_end_pass(b);VkImageMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;barrier.oldLayout=VK_IMAGE_LAYOUT_UNDEFINED;barrier.newLayout=VK_IMAGE_LAYOUT_GENERAL;
        barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.image=m->image;barrier.subresourceRange={all,0,1,0,1};barrier.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,0,nullptr,0,nullptr,1,&barrier);
        static const auto vs=std::make_shared<const std::string>("#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2.0-1.0,0,1);}");
        static const std::string head="#version 450\nlayout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane_;uint bit_;};\n";
        static const auto fs_color=std::make_shared<const std::string>(head+"layout(binding=17) uniform sampler2DMSArray src_;layout(location=0) out vec4 o;\n"
            "void main(){o=texelFetch(src_,ivec3(ivec2(gl_FragCoord.xy),0),int(plane_));}\n");
        static const auto fs_depth=std::make_shared<const std::string>(head+"layout(binding=17) uniform sampler2DMSArray src_;\n"
            "void main(){gl_FragDepth=texelFetch(src_,ivec3(ivec2(gl_FragCoord.xy),0),int(plane_)).r;}\n");
        static const auto fs_bits=std::make_shared<const std::string>(head+"layout(binding=18) uniform usampler2DMSArray stencil_;\n"
            "void main(){if(((texelFetch(stencil_,ivec3(ivec2(gl_FragCoord.xy),0),int(plane_)).r>>bit_)&1u)==0u)discard;}\n");
        GraphicsState e{};e.width=v->width;e.height=v->height;e.scissor={{0,0},{v->width,v->height}};
        if(v->depth){e.colors=0;e.depth=v->format;e.z.depthTestEnable=e.z.depthWriteEnable=true;e.z.depthCompareOp=VK_COMPARE_OP_ALWAYS;
            if(stencil){e.z.stencilTestEnable=true;e.z.front.failOp=e.z.front.depthFailOp=VK_STENCIL_OP_KEEP;e.z.front.passOp=VK_STENCIL_OP_REPLACE;e.z.front.compareOp=VK_COMPARE_OP_ALWAYS;e.z.back=e.z.front;}
            v->ms_expand=graphics_pipeline(e,*vs,*fs_depth,vs,fs_depth);
            if(stencil){e.z.depthTestEnable=e.z.depthWriteEnable=false;v->ms_expand_bits=graphics_pipeline(e,*vs,*fs_bits,vs,fs_bits);}}
        else{e.colors=1;e.color=v->format;e.blend[0].colorWriteMask=15;v->ms_expand=graphics_pipeline(e,*vs,*fs_color,vs,fs_color);}
        for(unsigned layer=0;layer<2;++layer){VkFramebufferCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;fi.renderPass=v->ms_expand->pass;fi.attachmentCount=1;fi.pAttachments=&v->views[layer];
            fi.width=v->width;fi.height=v->height;fi.layers=1;check(vkCreateFramebuffer(device,&fi,nullptr,&v->ms_expand_fb[layer]),"visual msaa expand framebuffer");}
        b.pins.push_back(v);v->ms=std::move(m);v->ms_current=false;graphics_ms_any=true;++graphics_ms_images;return true;
    }
    // Recording side (draw recorder, or the frontend after b.sync()): working image -> planes. Ends any open pass first.
    void graphics_ms_expand(GraphicsBatch& b,GraphicsImage& v){
        graphics_close_pass_only(b);
        const auto& m=*v.ms;const auto& p=*v.ms_expand;const bool bits=v.ms_expand_bits && !v.ms_stencil_known;
        if(!graphics_sampler){VkSamplerCreateInfo sampler{};sampler.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
            sampler.magFilter=sampler.minFilter=VK_FILTER_NEAREST;sampler.mipmapMode=VK_SAMPLER_MIPMAP_MODE_NEAREST;
            sampler.addressModeU=sampler.addressModeV=sampler.addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
            check(vkCreateSampler(device,&sampler,nullptr,&graphics_sampler),"graphics sampler");}
        const auto set=graphics_descriptor_set(b,p.descriptors,"visual msaa expand descriptor");
        std::array<VkWriteDescriptorSet,3> writes{};const VkDescriptorBufferInfo data{b.data_buffer,0,16};
        const VkDescriptorImageInfo images[2]{{graphics_sampler,m.array_view,VK_IMAGE_LAYOUT_GENERAL},{graphics_sampler,m.stencil_array_view,VK_IMAGE_LAYOUT_GENERAL}};
        for(auto& w:writes){w.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;w.dstSet=set;w.descriptorCount=1;}
        writes[0].dstBinding=0;writes[0].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC;writes[0].pBufferInfo=&data;
        writes[1].dstBinding=17;writes[1].descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;writes[1].pImageInfo=&images[0];
        writes[2].dstBinding=18;writes[2].descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;writes[2].pImageInfo=&images[1];
        vkUpdateDescriptorSets(device,m.stencil_array_view?3:2,writes.data(),0,nullptr);
        std::array<unsigned,22> d{};d[0]=d[4]=v.width;d[1]=d[5]=v.height;
        for(unsigned face=0;face<2;++face){d[6+face*3]=255;d[7+face*3]=255;d[8+face*3]=v.ms_stencil_known?v.ms_stencil&255:0;}
        const float one=1;std::memcpy(&d[19],&one,4);
        const std::uint32_t offset=0;
        for(unsigned layer=0;layer<2;++layer){
            VkRenderPassBeginInfo pass{};pass.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;pass.renderPass=p.pass;pass.framebuffer=v.ms_expand_fb[layer];pass.renderArea={{0,0},{v.width,v.height}};
            vkCmdBeginRenderPass(b.command,&pass,VK_SUBPASS_CONTENTS_INLINE);graphics_mark(b,MarkPassBegin);++b.render_passes;::RsxPerfCounters::add(::RsxPerfCounters::render_passes);
            vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,p.pipeline);
            vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,p.layout,0,1,&set,1,&offset);
            graphics_emit_dynamic(b.command,d,nullptr);
            GraphicsPush push{float(v.width),float(v.height),0,0,layer,0,{},{1,1}};
            vkCmdPushConstants(b.command,p.layout,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush),&push);
            vkCmdDraw(b.command,3,1,0,0);graphics_mark(b,MarkDraw);
            if(bits){vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,v.ms_expand_bits->pipeline);
                vkCmdSetStencilReference(b.command,VK_STENCIL_FACE_FRONT_AND_BACK,255);
                for(unsigned bit=0;bit<8;++bit){push.layers=bit;vkCmdSetStencilWriteMask(b.command,VK_STENCIL_FACE_FRONT_AND_BACK,1u<<bit);
                    vkCmdPushConstants(b.command,p.layout,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush),&push);
                    vkCmdDraw(b.command,3,1,0,0);graphics_mark(b,MarkDraw);}}
            vkCmdEndRenderPass(b.command);graphics_mark(b,MarkPassEnd);::RsxPerfCounters::add(::RsxPerfCounters::pass_ends);
        }
        b.bound_graphics_pipeline=bits?v.ms_expand_bits->pipeline:p.pipeline;b.dynamic_valid=false;
        v.ms_current=false;++graphics_ms_expands;if(bits)++graphics_ms_bit_expands;
    }
    // Frontend hook before any non-draw access to `image` (a native image with a visual companion, a visual image, or an
    // input alias of one): its pending visual draws are recorded, then a current working image is expanded.
    void graphics_ms_planes(GraphicsBatch& b,GraphicsImage* image){
        if(!graphics_ms_any || !image)return;
        b.sync();
        GraphicsImage* candidates[3]={image,image->visual.get(),image->borrowed.get()};
        bool any=false;for(auto* c:candidates)any|=c && c->ms;
        if(!any)return;
        graphics_begin(b);graphics_end_pass(b);
        for(auto* c:candidates)if(c && c->ms && c->ms_current)graphics_ms_expand(b,*c);
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
            // ANYPS3_SAMPLE_PROFILE with role rsx also samples this thread (its driver/recording cost is RSX work).
            std::unique_ptr<anyps3_sample::Thread> sample_profile;
            if(anyps3_sample::config().selected(anyps3_sample::Role::Rsx))sample_profile=std::make_unique<anyps3_sample::Thread>(anyps3_sample::Role::Rsx);
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
    // ---- GPU completion notifier (ANYPS3_RSX_COMPLETION=1) ----
    // A thread waits (vkWaitSemaphores in 2 ms steps) for the timeline value of every submitted batch in submission order and calls
    // graphics_completion_hook after each one. The RSX owner pokes its queue from the hook, so retirement and label publication run when a
    // batch completes instead of on a 100 us poll while flights are outstanding. The hook must not block (it runs on the notifier thread).
    struct CompletionWaiter {
        NativeVulkan* owner;std::mutex mutex;std::condition_variable wake;std::deque<std::uint64_t> values;std::function<void()> hook;
        std::atomic<bool> stopping{false};std::thread thread;std::uint64_t notified=0;bool calling=false;std::condition_variable idle;
        explicit CompletionWaiter(NativeVulkan* o):owner(o){thread=std::thread([this]{run();});}
        ~CompletionWaiter(){{std::lock_guard<std::mutex> lock(mutex);stopping.store(true,std::memory_order_release);}wake.notify_all();if(thread.joinable())thread.join();}
        void run(){
            ::pthread_setname_np(::pthread_self(),"rsx-complete");
            std::unique_lock<std::mutex> lock(mutex);
            for(;;){
                wake.wait(lock,[&]{return stopping.load(std::memory_order_acquire) || !values.empty();});
                if(stopping.load(std::memory_order_acquire))return;
                const std::uint64_t value=values.front();lock.unlock();
                for(;;){
                    VkSemaphoreWaitInfo info{};info.sType=VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO;info.semaphoreCount=1;info.pSemaphores=&owner->graphics_timeline;info.pValues=&value;
                    const auto result=vkWaitSemaphores(owner->device,&info,2000000);
                    if(result!=VK_TIMEOUT || stopping.load(std::memory_order_acquire))break;   // success, or an error the owner's own poll reports
                }
                lock.lock();
                if(stopping.load(std::memory_order_acquire))return;
                values.pop_front();++notified;const auto callback=hook;
                if(callback){calling=true;lock.unlock();callback();lock.lock();calling=false;idle.notify_all();}
            }
        }
    };
    std::unique_ptr<CompletionWaiter> completion_waiter;
    void completion_start(){
        const char* v=std::getenv("ANYPS3_RSX_COMPLETION");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("invalid ANYPS3_RSX_COMPLETION");
        if(v && !std::strcmp(v,"1"))completion_waiter=std::make_unique<CompletionWaiter>(this);
    }
    bool completion_notifier_enabled()const{return bool(completion_waiter);}
    // Installs/clears the hook (owner thread); no-op without the notifier.
    // Returns after any call of the previous hook has finished, so state captured by it may be destroyed afterwards.
    void set_completion_hook(std::function<void()> hook){if(completion_waiter){auto& w=*completion_waiter;std::unique_lock<std::mutex> lock(w.mutex);w.hook=std::move(hook);w.idle.wait(lock,[&]{return !w.calling;});}}
    void completion_watch(std::uint64_t value){
        if(!completion_waiter)return;
        {std::lock_guard<std::mutex> lock(completion_waiter->mutex);completion_waiter->values.push_back(value);}
        completion_waiter->wake.notify_one();
    }
    void completion_stop()noexcept{completion_waiter.reset();}
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
        if(j.s.visual_pass && graphics_visual_batch && !j.batch->replaying_visual){
            auto& b=*j.batch;b.visual_jobs.push_back(j);++b.visual_queued;
            b.visual_run_max=std::max<std::uint64_t>(b.visual_run_max,b.visual_jobs.size());
            if(b.visual_jobs.size()>=128)graphics_end_pass(b);
            return;
        }
        auto& b=*j.batch;const auto& s=j.s;const auto& colors=j.colors;const auto& depth=j.depth;const auto& textures=j.textures;const auto& raw_textures=j.raw_textures;
        // Visual MSAA: current working images this draw samples, or writes as a plane draw, are expanded first; the draw goes
        // into the working images only when every attachment's working image is current.
        bool ms_mode=false;
        if(graphics_ms_any){
            for(const auto& t:textures)if(t){if(t->ms && t->ms_current)graphics_ms_expand(b,*t);if(t->borrowed && t->borrowed->ms && t->borrowed->ms_current)graphics_ms_expand(b,*t->borrowed);}
            ms_mode=bool(j.ms_pipeline);
            for(unsigned i=0;i<s.colors && ms_mode;++i)ms_mode=colors[i]->ms && colors[i]->ms_current;
            if(ms_mode && depth)ms_mode=depth->ms && depth->ms_current;
            if(!ms_mode){for(unsigned i=0;i<s.colors;++i)if(colors[i] && colors[i]->ms && colors[i]->ms_current)graphics_ms_expand(b,*colors[i]);
                if(depth && depth->ms && depth->ms_current)graphics_ms_expand(b,*depth);
                if(j.ms_pipeline)++graphics_ms_plane_draws;}
            else ++graphics_ms_draws;
        }
        auto& p=ms_mode?*j.ms_pipeline:*j.pipeline;
        const unsigned raster_width=s.raster_width?s.raster_width:s.width,raster_height=s.raster_height?s.raster_height:s.height;
        // DIAGNOSTIC ONLY (ANYPS3_DIAG_VISUAL_NO_RASTER=1): visual halves get an empty scissor, so their vertex work, state and
        // pass structure stay and no fragment is produced (wrong image). Measures the fragment share of the visual cost.
        static const bool diag_no_raster=[]{const char* v=std::getenv("ANYPS3_DIAG_VISUAL_NO_RASTER");return v && !std::strcmp(v,"1");}();
        auto scissor=graphics_raster_scissor(s);if(diag_no_raster && s.visual_pass)scissor.extent={0,0};
        const auto& positions=j.positions;const float bias_constant=j.bias_constant,bias_slope=j.bias_slope,min_bounds=j.min_bounds,max_bounds=j.max_bounds;
        const unsigned vertices=j.vertices,first_vertex=j.first_vertex,query=j.query,passes=ms_mode?1u:j.passes;const bool layered=j.layered,multiview=j.multiview,defer_planes=j.defer_planes && !ms_mode;
        const std::uint64_t at=j.at,range=j.range;
        const bool timed=anyps3_perf_report();const auto t=timed?std::chrono::steady_clock::now():std::chrono::steady_clock::time_point{};   // record_back_ms is a PERF statistic
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
        typename GraphicsBatch::PlaneDraw deferred{};std::array<VkFramebuffer,4> plane_fb{};
        for(unsigned layer=0;layer<passes;++layer){
            std::array<VkImageView,5> views{};unsigned view_count=0;for(unsigned i=0;i<s.colors;++i)views[view_count++]=ms_mode?colors[i]->ms->views[0]:layered?graphics_attachment_array(*colors[i]):colors[i]->views[layer];
            if(depth)views[view_count++]=ms_mode?depth->ms->views[0]:layered?graphics_attachment_array(*depth):depth->views[layer];
            VkFramebufferCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;fi.renderPass=p.pass;fi.attachmentCount=view_count;fi.pAttachments=views.data();fi.width=raster_width;fi.height=raster_height;fi.layers=layered&&!multiview?s.samples:1;VkFramebuffer framebuffer;
            // All generated passes have one graphics subpass and identical
            // load/store/layout policy. Attachment formats/count define exact
            // render-pass compatibility; blend/shader variants share the FB.
            std::array<std::uint64_t,10> framebuffer_key{};framebuffer_key[0]=s.colors|(std::uint64_t(fi.layers)<<32);framebuffer_key[1]=s.color;framebuffer_key[2]=s.depth;framebuffer_key[3]=raster_width;framebuffer_key[4]=raster_height;
            for(unsigned i=0;i<view_count;++i)framebuffer_key[5+i]=reinterpret_cast<std::uint64_t>(views[i]);
            auto found=graphics_framebuffers.find(framebuffer_key);
            if(found!=graphics_framebuffers.end() && !found->second->live()){graphics_framebuffers.erase(found);found=graphics_framebuffers.end();}
            if(found!=graphics_framebuffers.end()){framebuffer=found->second->handle;found->second->last_use=++recorder_clock;b.framebuffer_pins.push_back(found->second);++b.framebuffer_hits;}
            else{check(vkCreateFramebuffer(device,&fi,nullptr,&framebuffer),"graphics framebuffer");auto entry=std::make_shared<typename GraphicsBatch::Framebuffer>();entry->owner=this;entry->handle=framebuffer;entry->last_use=++recorder_clock;entry->pipeline=ms_mode?j.ms_pipeline:j.pipeline;
                for(unsigned i=0;i<s.colors;++i)entry->images.push_back(ms_mode?colors[i]->ms:colors[i]);
                if(depth)entry->images.push_back(ms_mode?depth->ms:depth);
                if(graphics_framebuffers.size()>=framebuffer_limit){
                    for(auto it=graphics_framebuffers.begin();it!=graphics_framebuffers.end();){if(!it->second->live())it=graphics_framebuffers.erase(it);else ++it;}
                    if(graphics_framebuffers.size()>=framebuffer_limit)graphics_framebuffers.erase(lru_entry(graphics_framebuffers));}
                b.framebuffer_pins.push_back(entry);graphics_framebuffers.emplace(std::move(framebuffer_key),std::move(entry));++b.framebuffer_misses;::RsxPerfCounters::add(::RsxPerfCounters::framebuffer_misses);}
            VkRenderPassBeginInfo pass{};pass.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;pass.renderPass=p.pass;pass.framebuffer=framebuffer;pass.renderArea={{0,0},{raster_width,raster_height}};
            // Push constants: sample location P is evaluated by moving the
            // geometry by .5-P. Do not translate the viewport: negative viewport
            // origins need a Vulkan 1.1 extension. VS subtracts this offset in
            // clip coordinates.
            GraphicsPush push{float(s.width),float(s.height),positions[layer][0]-.5f,positions[layer][1]-.5f,layer,layered?s.samples:1u,{}, {float(raster_width)/s.width,float(raster_height)/s.height}};
            for(unsigned n=0;n<4;++n){push.shifts[2*n]=positions[n][0]-.5f;push.shifts[2*n+1]=positions[n][1]-.5f;}
            if(ms_mode){push.x=push.y=0;for(auto& v:push.shifts)v=0;}   // pixel-centre geometry: the hardware samples the 2x locations
            if(defer_planes && layer){plane_fb[layer]=framebuffer;deferred.push[layer]=push;continue;}
            // Framebuffer identity includes each layer view, extent, attachment
            // count and formats. Draw order is kept within each plane; planes
            // are distinct images and may be recorded as runs (PlaneDraw).
            if(b.active_framebuffer!=framebuffer){
                graphics_end_pass(b);
                vkCmdBeginRenderPass(b.command,&pass,VK_SUBPASS_CONTENTS_INLINE);graphics_mark(b,MarkPassBegin);
                b.active_framebuffer=framebuffer;++b.render_passes;::RsxPerfCounters::add(::RsxPerfCounters::render_passes);
            }else ++b.render_pass_reuses;
            // A framebuffer change may replay older visual draws. Open this
            // draw's timestamp pair only after their pairs have been closed.
            if(!layer){graphics_stamp(b,1);if(b.queries){b.phase_pipelines.resize(b.phases.size(),nullptr);b.phase_pipelines.back()=&p;b.phase_visual.resize(b.phases.size(),0);b.phase_visual.back()=s.visual_pass;}}
            // One query per sample pass; sum at the ownership boundary.
            if(query!=UINT_MAX)vkCmdBeginQuery(b.command,b.occlusion,query+layer,graphics_features.occlusionQueryPrecise?VK_QUERY_CONTROL_PRECISE_BIT:0);
            if(b.bound_graphics_pipeline!=p.pipeline){vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,p.pipeline);b.bound_graphics_pipeline=p.pipeline;++b.pipeline_binds;}
            const auto dynamic_at=static_cast<std::uint32_t>(at);vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_GRAPHICS,p.layout,0,1,&set,1,&dynamic_at);
            vkCmdPushConstants(b.command,p.layout,VK_SHADER_STAGE_VERTEX_BIT|VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(GraphicsPush),&push);
            // Dynamic state survives render-pass boundaries and compute binds,
            // but never command-buffer reset. Compare exact float bits so NaNs
            // and signed zero cannot manufacture a false state-cache hit.
            std::array<unsigned,22> dynamic{};
            dynamic[0]=raster_width;dynamic[1]=raster_height;
            dynamic[2]=scissor.offset.x;dynamic[3]=scissor.offset.y;dynamic[4]=scissor.extent.width;dynamic[5]=scissor.extent.height;
            for(unsigned face=0;face<2;++face){const auto& stencil=face?s.z.back:s.z.front;
                dynamic[6+face*3]=stencil.compareMask;dynamic[7+face*3]=stencil.writeMask;dynamic[8+face*3]=stencil.reference;}
            std::memcpy(dynamic.data()+12,s.constants.data(),16);
            std::memcpy(dynamic.data()+16,&bias_constant,4);std::memcpy(dynamic.data()+17,&bias_slope,4);
            std::memcpy(dynamic.data()+18,&min_bounds,4);std::memcpy(dynamic.data()+19,&max_bounds,4);
            auto changed=[&](unsigned first,unsigned count){const bool yes=!b.dynamic_valid || !std::equal(dynamic.begin()+first,dynamic.begin()+first+count,b.dynamic_state.begin()+first);
                if(yes)++b.dynamic_emits;else ++b.dynamic_reuses;return yes;};
            if(changed(0,2)){VkViewport viewport{0,0,float(raster_width),float(raster_height),0,1};vkCmdSetViewport(b.command,0,1,&viewport);}
            if(changed(2,4))vkCmdSetScissor(b.command,0,1,&scissor);
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
            if(query!=UINT_MAX)vkCmdEndQuery(b.command,b.occlusion,query+layer);
            graphics_mark(b,MarkDraw);
            if(defer_planes){deferred.pipeline=p.pipeline;deferred.layout=p.layout;deferred.set=set;deferred.offset=dynamic_at;deferred.dynamic=dynamic;
                deferred.vertices=vertices;deferred.instances=instances;deferred.first_vertex=first_vertex;deferred.query=query;
                deferred.indexed=j.indexed;deferred.index_offset=deferred.indexed?at+j.index_offset:0;
                deferred.query_flags=graphics_features.occlusionQueryPrecise?VK_QUERY_CONTROL_PRECISE_BIT:0;}
        }
        if(defer_planes){
            // A run keeps one framebuffer per plane; anything else ends it first
            // (this also ends the pass holding this draw's plane 0).
            bool same=b.plane_count==passes && b.plane_extent.width==raster_width && b.plane_extent.height==raster_height;
            for(unsigned layer=1;layer<passes && same;++layer)same=b.plane_framebuffers[layer]==plane_fb[layer];
            if(!b.plane_draws.empty() && !same)graphics_end_pass(b);
            if(b.plane_draws.empty()){b.plane_framebuffers=plane_fb;b.plane_pass=p.pass;b.plane_extent={raster_width,raster_height};b.plane_count=passes;}
            b.plane_draws.push_back(deferred);++b.plane_deferred;
        }
        if(ms_mode && depth && s.z.stencilTestEnable && (s.z.front.writeMask || s.z.back.writeMask))depth->ms_stencil_known=false;
        graphics_stamp(b,0,true);++b.draws;if(timed)b.record_back_ms+=resident_ms(t);
    }
    std::uint64_t graphics_draw(GraphicsBatch& b,const GraphicsState& s,const std::array<std::shared_ptr<GraphicsImage>,4>& colors,
                       const std::shared_ptr<GraphicsImage>& depth,const std::string& vs,const std::string& fs,
                       const std::vector<unsigned>& data,unsigned vertices,const std::array<std::array<float,2>,4>& positions,
                       float bias_constant=0,float bias_slope=0,float min_bounds=0,float max_bounds=1,const std::array<std::shared_ptr<GraphicsImage>,20>& textures={},unsigned first_vertex=0,std::uint64_t reuse=UINT64_MAX,const std::array<std::shared_ptr<GraphicsBuffer>,16>& raw_textures={},const std::shared_ptr<const std::string>& vs_owner={},const std::shared_ptr<const std::string>& fs_owner={},const std::uint8_t* tail=nullptr,std::uint64_t tail_bytes=0,bool layered_ok=false){
        if(s.raster_width || s.raster_height){
            (void)graphics_raster_scissor(s);
            const auto& limits=graphics_properties.limits;
            if(s.raster_width>limits.maxFramebufferWidth || s.raster_height>limits.maxFramebufferHeight ||
               s.raster_width>limits.maxViewportDimensions[0] || s.raster_height>limits.maxViewportDimensions[1])
                throw std::runtime_error("RSX visual raster exceeds device limits");
            for(unsigned i=0;i<s.colors;++i)if(!colors[i] || colors[i]->width<s.raster_width || colors[i]->height<s.raster_height)
                throw std::runtime_error("RSX visual color attachment extent mismatch");
            if(depth && (depth->width<s.raster_width || depth->height<s.raster_height))
                throw std::runtime_error("RSX visual depth attachment extent mismatch");
        }
        // Layered planes: every attachment must be a samples-layer array (else per-plane passes).
        bool layered=layered_ok && graphics_layered_output && s.samples>1;
        if(layered){for(unsigned i=0;i<s.colors;++i)if(colors[i]->layers!=s.samples)layered=false;if(depth && depth->layers!=s.samples)layered=false;}
        const bool multiview=layered && graphics_multiview;
        // The pipeline key has no visual_pass/raster field and a visual pair
        // has the same layer count, so the paired half reuses the native one.
        auto pipeline=s.visual_pass && graphics_paired_pipeline?std::move(graphics_paired_pipeline):graphics_pipeline(s,vs,fs,vs_owner,fs_owner,multiview);
        graphics_paired_pipeline.reset();b.pipeline_pins.push_back(pipeline);
        // Visual MSAA eligibility (graphics_ms_*): a plane-independent visual draw into 2-plane images it does not sample. The
        // targets are marked wanted (their next full clear creates/makes current the working images); once every target has a
        // working image the job carries the 2-sample pipeline variant and the recorder picks the mode from the image state.
        std::shared_ptr<GraphicsPipeline> ms_pipeline;
        if(s.visual_pass && s.ms_ok && s.samples==2 && !layered && (s.colors || depth) && graphics_visual_msaa()){
            bool ok=true;
            for(unsigned i=0;i<s.colors;++i)ok=ok && colors[i]->layers==2 && graphics_ms_format(colors[i]->format,false);
            if(depth)ok=ok && depth->layers==2 && graphics_ms_format(depth->format,true);
            for(const auto& t:textures)if(t && ok){for(unsigned i=0;i<s.colors;++i)ok=ok && t!=colors[i] && t->borrowed!=colors[i];ok=ok && (!depth || (t!=depth && t->borrowed!=depth));}
            if(ok){bool ready=true;
                for(unsigned i=0;i<s.colors;++i){colors[i]->ms_wanted=true;ready=ready && colors[i]->ms;}
                if(depth){depth->ms_wanted=true;ready=ready && depth->ms;}
                if(ready){if(!pipeline->ms_variant){auto m=s;m.raster_samples=2;pipeline->ms_variant=graphics_pipeline(m,vs,fs,vs_owner,fs_owner);}ms_pipeline=pipeline->ms_variant;}}
        }
        // An optional tail (vertex input bytes) directly follows the words in
        // the same immutable slice; the blob addresses it past data.size()*4.
        std::shared_ptr<GraphicsPipeline> paired;
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
        const bool counted=!s.visual_pass && s.visibility;
        if(counted && b.visibility_count+s.samples>1024)throw std::runtime_error("RSX graphics visibility budget");
        const auto query=counted?b.visibility_count++:UINT_MAX;::RsxPerfCounters::add(::RsxPerfCounters::draw_layers,s.samples);
        // Layered: every sample plane is one instance of a single draw into
        // a layered framebuffer. Per plane the primitive order is unchanged;
        // planes are distinct layers, so their relative order is unobservable.
        // DIAGNOSTIC ONLY (ANYPS3_DIAG_VISUAL_PLANE0=1): visual halves draw sample plane 0 only, so planes >=1 of every visual
        // MSAA image keep stale pixels (wrong image). Measures the GPU cost of the visual planes >=1; never in a game run.
        static const bool diag_plane0=[]{const char* v=std::getenv("ANYPS3_DIAG_VISUAL_PLANE0");return v && !std::strcmp(v,"1");}();
        const unsigned passes=layered?1:(diag_plane0 && s.visual_pass?1u:s.samples);graphics_last_queries=counted?(multiview?s.samples:passes):0u;
        // Plane runs (GraphicsBatch::PlaneDraw): planes >=1 of a per-plane draw
        // are recorded after plane 0's pass. ANYPS3_RSX_PLANE_RUNS=0 records
        // every plane immediately (one pass switch per plane).
        static const bool plane_runs=[]{const char* v=std::getenv("ANYPS3_RSX_PLANE_RUNS");
            if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_PLANE_RUNS");}
            return !v || !std::strcmp(v,"1");}();
        const bool defer_planes=plane_runs && passes>1;
        if(counted)b.visibility_count+=(multiview?s.samples:passes)-1;
        // Keep the existing logical readback accounting at record time. The
        // submit combines these requests; it does not create new requests.
        b.status_offsets.push_back(at+28);b.download_bytes+=4;
        // A visual half that reuses a native slice shares its failure word (either half's failure stops the batch).
        if(s.visual_pass){++graphics_visual_draws;(reuse==UINT64_MAX?b.visual_status_offsets:b.paired_status_offsets).push_back(at+28);}else ++graphics_draws;
        const bool asynchronous=draw_recorder && !b.inline_draws && !b.queries && !b.marks && !graphics_gpu_loop();
        std::optional<DrawJob> inline_job;DrawJob* job=asynchronous?&recorder_slot():&inline_job.emplace();
        const bool pair=!s.visual_pass && !s.raster_width && s.scale_visual && graphics_render_scale>1;
        if(pair)paired=pipeline;
        job->batch=&b;job->pipeline=std::move(pipeline);job->ms_pipeline=std::move(ms_pipeline);job->s=s;job->colors=colors;job->depth=depth;job->textures=textures;job->raw_textures=raw_textures;job->positions=positions;
        job->bias_constant=bias_constant;job->bias_slope=bias_slope;job->min_bounds=min_bounds;job->max_bounds=max_bounds;job->vertices=vertices;job->first_vertex=first_vertex;
        job->query=query;job->passes=passes;job->layered=layered;job->multiview=multiview;job->defer_planes=defer_planes;job->at=at;job->range=range;
        job->indexed=data.size()>15 && data[15]==1;job->index_offset=job->indexed?std::uint64_t(data[13])*4:0;
        if(asynchronous){b.pending_jobs.fetch_add(1,std::memory_order_relaxed);recorder_publish();}else graphics_record_draw(*job);
        if(!s.visual_pass && !s.raster_width && graphics_render_scale>1 && !s.visual_external){
            if(s.scale_visual){
                auto scaled=s;scaled.visual_pass=true;scaled.raster_width=s.width*graphics_render_scale;scaled.raster_height=s.height*graphics_render_scale;
                std::array<std::shared_ptr<GraphicsImage>,4> visual_colors{};
                for(unsigned i=0;i<s.colors;++i){if(!colors[i] || !colors[i]->visual)throw std::runtime_error("RSX missing visual color companion");visual_colors[i]=colors[i]->visual;}
                if(depth && !depth->visual)throw std::runtime_error("RSX missing visual depth companion");
                auto visual_textures=textures;for(auto& image:visual_textures)if(image && image->visual)image=image->visual;
                if(graphics_visual_trace()){std::uint64_t taint=0;for(const auto& image:visual_textures)if(image)taint|=image->visual_taint;for(unsigned i=0;i<s.colors;++i)graphics_visual_taint(*visual_colors[i],taint,false);}
                const auto saved_queries=graphics_last_queries;graphics_paired_pipeline=std::move(paired);
                struct Handoff {std::shared_ptr<GraphicsPipeline>& p;~Handoff(){p.reset();}} handoff{graphics_paired_pipeline};
                // The visual half reads the native half's immutable slice (same words and tail): no second copy, no
                // second upload and no separate staging requirement for the pair.
                graphics_draw(b,scaled,visual_colors,depth?depth->visual:nullptr,vs,fs,data,vertices,positions,bias_constant,bias_slope,min_bounds,max_bounds,visual_textures,first_vertex,at,raw_textures,vs_owner,fs_owner,tail,tail_bytes,layered_ok);
                graphics_last_queries=saved_queries;
            }else{
                for(unsigned i=0;i<s.colors;++i)graphics_visual_sync(b,colors[i],"draw_unscaled");
                if(depth)graphics_visual_sync(b,depth,"draw_unscaled");
            }
        }
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
            check(vkQueueSubmit(queue,1,&si,b.fence),"graphics submit");::RsxPerfCounters::add(::RsxPerfCounters::submits);graphics_serial=value;b.completion=value;b.submitted=true;++b.submissions;completion_watch(value);return value;
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
                b.phase_pipelines.resize(std::max(b.phase_pipelines.size(),b.phases.size()),nullptr);b.phase_visual.resize(std::max(b.phase_visual.size(),b.phases.size()),0);
                for(unsigned i=0;i<b.phases.size();++i){const double ms=double((values[2*i+1]-values[2*i])&mask)*b.timestamp_period/1e6;
                    if(b.phases[i]==1 && b.phase_pipelines[i])draw_profile_add(*b.phase_pipelines[i],ms,b.phase_visual[i]);
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
                        static const char* const sites[]={"-","tex_upload","surface_copy","transfer","unpack","pack","aspect","decode","raw_buffer","device_upload","predicate","present","clear","device_copies","visual"};
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
                        static const char* const sites[]={"-","tex_upload","surface_copy","transfer","unpack","pack","aspect","decode","raw_buffer","device_upload","predicate","present","clear","device_copies","visual"};
                        std::fprintf(stderr,"PERF GPU_MARKS frames=%llu",(unsigned long long)graphics_profile_frames);
                        for(unsigned k=0;k<graphics_mark_ns.size();++k){if(!graphics_mark_count[k])continue;
                            std::fprintf(stderr," %s:%s=%llu/%llu",sites[std::min<unsigned>(k/16,SiteCount-1)],names[std::min<unsigned>(k%16,MarkOther)],(unsigned long long)graphics_mark_ns[k],(unsigned long long)graphics_mark_count[k]);}
                        std::fputc('\n',stderr);}}}
            for(auto offset:b.visual_status_offsets){unsigned status=0;std::memcpy(&status,static_cast<const char*>(b.map)+offset,4);
                if(status)throw std::runtime_error("RSX visual shader failed status="+std::to_string(status));}
            for(auto offset:b.paired_status_offsets){unsigned status=0;std::memcpy(&status,static_cast<const char*>(b.map)+offset,4);
                if(status)throw std::runtime_error("RSX native/visual paired shader failed status="+std::to_string(status));}
            // Per-batch queue counters can move to a later batch (graphics_move_stats) before this one is collected; fold
            // whatever this batch holds into run totals so each count is reported exactly once.
            graphics_visual_queued_total+=b.visual_queued;graphics_visual_drains_total+=b.visual_drains;graphics_visual_run_max=std::max(graphics_visual_run_max,b.visual_run_max);
            b.visual_queued=b.visual_drains=b.visual_run_max=0;
            if(b.frames && graphics_render_scale>1 && graphics_profile_frames%120==0)
                std::fprintf(stderr,"PERF RENDER_SCALE scale=%u visual_draws=%llu native_copies=%llu host_invalidations=%llu queued=%llu drains=%llu max_run=%llu history_copies=%llu history_texels=%llu\n",graphics_render_scale,(unsigned long long)graphics_visual_draws,(unsigned long long)graphics_visual_native_copies,(unsigned long long)graphics_visual_host_invalidations,(unsigned long long)graphics_visual_queued_total,(unsigned long long)graphics_visual_drains_total,(unsigned long long)graphics_visual_run_max,(unsigned long long)graphics_visual_history_copies,(unsigned long long)graphics_visual_history_texels);
            if(b.frames && graphics_render_scale>1 && graphics_profile_frames%120==0 && graphics_visual_msaa())
                std::fprintf(stderr,"PERF RENDER_SCALE_MSAA images=%llu clears=%llu draws=%llu plane_draws=%llu expands=%llu bit_expands=%llu\n",(unsigned long long)graphics_ms_images.load(),(unsigned long long)graphics_ms_clears.load(),
                    (unsigned long long)graphics_ms_draws.load(),(unsigned long long)graphics_ms_plane_draws.load(),(unsigned long long)graphics_ms_expands.load(),(unsigned long long)graphics_ms_bit_expands.load());
            if(b.frames && graphics_render_scale>1 && graphics_profile_frames%120==0)for(const auto& r:graphics_visual_reasons)
                std::fprintf(stderr,"PERF RENDER_SCALE_SYNC reason=%s count=%llu pixels=%llu\n",r.name.c_str(),(unsigned long long)r.count,(unsigned long long)r.pixels);
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
            to.visual_queued+=from.visual_queued;to.visual_drains+=from.visual_drains;to.visual_run_max=std::max(to.visual_run_max,from.visual_run_max);
            from.visual_queued=from.visual_drains=from.visual_run_max=0;
            to.draws+=from.draws;to.record_back_ms+=from.record_back_ms;to.descriptor_hits+=from.descriptor_hits;to.descriptor_misses+=from.descriptor_misses;to.framebuffer_hits+=from.framebuffer_hits;to.framebuffer_misses+=from.framebuffer_misses;
            from.draws=0;from.record_back_ms=0;from.descriptor_hits=from.descriptor_misses=from.framebuffer_hits=from.framebuffer_misses=0;
        }
        to.upload_ms+=from.upload_ms;to.draw_ms+=from.draw_ms;to.download_ms+=from.download_ms;to.wait_ms+=from.wait_ms;
        from.upload_ms=from.draw_ms=from.download_ms=from.wait_ms=0;
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
        if(!b.visual_jobs.empty())throw std::runtime_error("RSX pending visual draws at batch reset");
        b.overflow_used=0;b.used=0;b.status_offsets.clear();b.visual_status_offsets.clear();b.paired_status_offsets.clear();b.uploads.clear();b.active_framebuffer=VK_NULL_HANDLE;b.completion=0;b.frames=0;b.descriptor_cache.clear();b.signal=VK_NULL_HANDLE;b.visibility_count=0;b.visibility.clear();b.query_count=0;b.phases.clear();b.phase_pipelines.clear();b.phase_visual.clear();
        b.bound_graphics_pipeline=VK_NULL_HANDLE;b.dynamic_valid=false;b.state_versions.fill(0);b.state_offsets.fill(0);

    }
private:
#endif
'''

MEMBERS += '\n#ifdef ANYPS3_RSX_VK\n' + SAMPLED_MEMBERS + '\nprivate:\n#endif\n'
