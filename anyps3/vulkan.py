"""Optional Vulkan color-attachment backend, embedded in generated C++.

There is one rendering implementation: Vulkan. Lavapipe may execute it on a
CPU without a display server. Unsupported RSX commands still fail explicitly.
"""

from .rsx_vk_compute import HEADERS as COMPUTE_HEADERS, MEMBERS as COMPUTE_MEMBERS

from .rsx_vk_graphics import MEMBERS as GRAPHICS_MEMBERS
from .rsx_vk_present import MEMBERS as PRESENT_MEMBERS
from .rsx_vk_conditional import MEMBERS as CONDITIONAL_MEMBERS

CPP = r'''
#ifndef ANYPS3_RSX_PERF_COUNTERS
#define ANYPS3_RSX_PERF_COUNTERS 1
#include <atomic>
#include <array>
#include <cstdint>
// Renderer-owner event counters for PERF reports. One writer at a time (the
// FIFO owner); plain relaxed load+store, no locked RMW on the draw path.
struct RsxPerfCounters {
    enum Id : unsigned {draws,gpu_draws,fifo_runs,packets,leave_fifo,guard_rearm,submits,submit_draw_prefix,submit_labels,submit_other,
        finish_waits,collect_waits,wait_ns,readback_flushes,readback_surfaces,host_access,label_syncs,alias_flushes,
        descriptor_allocs,descriptor_writes,pipeline_misses,framebuffer_misses,render_passes,upload_bytes,state_uploads,
        surface_binds_new,clears,mprotect,vertex_bytes,blob_bytes,draw_layers,history_copies,pass_ends,alias_merges,fp_hits,fp_misses,timeline_polls,fence_polls,mprotect_arm,mprotect_labels,vk_memory_allocs,vk_memory_frees,vk_buffers,vk_images,vk_framebuffers,merge_rej_new,merge_rej_old,merge_rej_capacity,merge_rej_overlap,vk_memory_reuses,blit_calls,blit_pixels,blit_gpu,blit_rows,blit_batch,blit_pixel,blit_rows_ns,blit_batch_ns,blit_pixel_ns,m2mf_calls,m2mf_bytes,m2mf_ns,src_fs_hits,src_fs_misses,src_vs_hits,src_vs_misses,src_evictions,count};
    static constexpr const char* names[count]={"draws","gpu_draws","fifo_runs","packets","leave_fifo","guard_rearm","submits","submit_draw_prefix","submit_labels","submit_other",
        "finish_waits","collect_waits","wait_ns","readback_flushes","readback_surfaces","host_access","label_syncs","alias_flushes",
        "descriptor_allocs","descriptor_writes","pipeline_misses","framebuffer_misses","render_passes","upload_bytes","state_uploads",
        "surface_binds_new","clears","mprotect","vertex_bytes","blob_bytes","draw_layers","history_copies","pass_ends","alias_merges","fp_hits","fp_misses","timeline_polls","fence_polls","mprotect_arm","mprotect_labels","vk_memory_allocs","vk_memory_frees","vk_buffers","vk_images","vk_framebuffers","merge_rej_new","merge_rej_old","merge_rej_capacity","merge_rej_overlap","vk_memory_reuses","blit_calls","blit_pixels","blit_gpu","blit_rows","blit_batch","blit_pixel","blit_rows_ns","blit_batch_ns","blit_pixel_ns","m2mf_calls","m2mf_bytes","m2mf_ns","src_fs_hits","src_fs_misses","src_vs_hits","src_vs_misses","src_evictions"};
    static inline std::array<std::atomic<std::uint64_t>,count> values{};
    static void add(Id id,std::uint64_t n=1){auto& v=values[id];v.store(v.load(std::memory_order_relaxed)+n,std::memory_order_relaxed);}
};
#endif
#ifdef ANYPS3_VULKAN
#include <vulkan/vulkan.h>
// Device capability of the active renderer that changes generated shader text
// (layered MSAA planes). Shader caches include it in their keys.
inline std::atomic<bool>& rsx_layered_shaders(){static std::atomic<bool> value{false};return value;}
// Layered shaders select the sample plane with gl_ViewIndex (VK_KHR_multiview,
// core 1.1) instead of gl_InstanceIndex + gl_Layer. Implies rsx_layered_shaders().
inline std::atomic<bool>& rsx_multiview_shaders(){static std::atomic<bool> value{false};return value;}
#include <cstring>
#include <dlfcn.h>
#include <thread>
#include <condition_variable>
#include <optional>
#include <deque>
#include <mutex>
#include <functional>
#include <exception>
''' + COMPUTE_HEADERS + r'''


// Synchronous main-thread transport for SDL work issued by a future renderer
// owner. A queued callback cannot run after invoke() has returned/failed.
struct VulkanHostService {
    std::thread::id owner;
    std::function<void(std::function<void()>)> dispatch;
    explicit VulkanHostService(std::thread::id tid=std::this_thread::get_id(),std::function<void(std::function<void()>)> send={}):owner(tid),dispatch(std::move(send)){}
    void invoke(std::function<void()> action)const{
        if(std::this_thread::get_id()==owner){action();return;}
        if(!dispatch)throw std::runtime_error("SDL Vulkan host service requires main-thread dispatch");
        struct Work {std::mutex mutex;bool done=false,closed=false;std::exception_ptr error;std::function<void()> action;};
        auto work=std::make_shared<Work>();work->action=std::move(action);const auto tid=owner;
        try{dispatch([work,tid]{std::lock_guard<std::mutex> lock(work->mutex);
            if(work->closed)return;
            if(work->done){work->error=std::make_exception_ptr(std::runtime_error("SDL Vulkan host service repeated callback"));return;}
            if(std::this_thread::get_id()!=tid){work->error=std::make_exception_ptr(std::runtime_error("SDL Vulkan host callback ran on foreign thread"));work->done=true;return;}
            try{work->action();}catch(...){work->error=std::current_exception();}work->done=true;
        });}catch(...){std::lock_guard<std::mutex> lock(work->mutex);work->closed=true;throw;}
        std::lock_guard<std::mutex> lock(work->mutex);work->closed=true;
        if(!work->done)throw std::runtime_error("SDL Vulkan host dispatch returned before completion");
        if(work->error)std::rethrow_exception(work->error);
    }
};

class NativeVulkan {
    std::shared_ptr<VulkanHostService> graphics_host;
    VkInstance instance=VK_NULL_HANDLE;
    VkPhysicalDevice physical=VK_NULL_HANDLE;
    VkDevice device=VK_NULL_HANDLE;
    VkQueue queue=VK_NULL_HANDLE;
    VkCommandPool pool=VK_NULL_HANDLE;
    VkPhysicalDeviceMemoryProperties memory{};
    std::uint32_t family=0;
#ifdef ANYPS3_RSX_VK
    // Dedicated DMA (transfer-only) queue: batch preludes (staging->device buffer copies) run there, concurrently with the
    // previous batch's graphics work (ANYPS3_RSX_ASYNC_UPLOAD=0 disables; absent on devices with a single queue family).
    VkQueue transfer_queue=VK_NULL_HANDLE;VkCommandPool transfer_pool=VK_NULL_HANDLE;std::uint32_t transfer_family=UINT32_MAX;
    VkSemaphore graphics_upload_timeline=VK_NULL_HANDLE;std::uint64_t graphics_upload_serial=0;
    VkBuffer graphics_loop_scratch=VK_NULL_HANDLE;VkDeviceMemory graphics_loop_scratch_memory=VK_NULL_HANDLE;std::uint64_t graphics_loop_scratch_size=0; // diagnostic loop benchmark only
    // Diagnostic loop benchmark only: an immutable host snapshot of the staging bytes (the graphics batch writes status words
    // back into its own staging buffer), a serial chain so that no two iterations of one command buffer overlap.
    VkBuffer graphics_loop_snapshot=VK_NULL_HANDLE;VkDeviceMemory graphics_loop_snapshot_memory=VK_NULL_HANDLE;void* graphics_loop_snapshot_map=nullptr;bool graphics_loop_snapshot_coherent=false;std::uint64_t graphics_loop_snapshot_size=0;
    VkSemaphore graphics_loop_timeline=VK_NULL_HANDLE;std::uint64_t graphics_loop_serial=0;
    std::uint32_t transfer_timestamp_bits=0; // timestampValidBits of the transfer family (differs from the graphics family's)
    bool graphics_host_query_reset=false;
#endif
    static void check(VkResult result,const char* operation){
        if(result!=VK_SUCCESS)throw std::runtime_error(std::string(operation)+" failed: VkResult="+std::to_string(result));
    }
    std::uint32_t memory_type(std::uint32_t bits,VkMemoryPropertyFlags flags)const{
        for(std::uint32_t i=0;i<memory.memoryTypeCount;++i)
            if((bits&(1u<<i)) && (memory.memoryTypes[i].propertyFlags&flags)==flags)return i;
        throw std::runtime_error("Vulkan device has no suitable memory type");
    }
    // Buffers the CPU reads back: prefer HOST_CACHED. On discrete GPUs the first
    // HOST_VISIBLE type is usually write-combined (uncached), where CPU reads run
    // at a few tens of MB/s (measured: 207 ms for a 960x704 commit on RTX 4070).
    std::uint32_t host_read_memory_type(std::uint32_t bits)const{
        const VkMemoryPropertyFlags cached=VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
        for(std::uint32_t i=0;i<memory.memoryTypeCount;++i)
            if((bits&(1u<<i)) && (memory.memoryTypes[i].propertyFlags&cached)==cached)return i;
        return memory_type(bits,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    }
    void destroy()noexcept{
#ifdef ANYPS3_RSX_VK
        completion_stop();recorder_stop();
        graphics_wsi.reset();
#endif
        if(device){
            vkDeviceWaitIdle(device);
#ifdef ANYPS3_RSX_VK
            if(graphics_timeline)vkDestroySemaphore(device,graphics_timeline,nullptr);
            if(graphics_upload_timeline)vkDestroySemaphore(device,graphics_upload_timeline,nullptr);
            if(graphics_loop_scratch)vkDestroyBuffer(device,graphics_loop_scratch,nullptr);
            if(graphics_loop_scratch_memory)vkFreeMemory(device,graphics_loop_scratch_memory,nullptr);
            if(graphics_loop_snapshot_map)vkUnmapMemory(device,graphics_loop_snapshot_memory);
            if(graphics_loop_snapshot)vkDestroyBuffer(device,graphics_loop_snapshot,nullptr);
            if(graphics_loop_snapshot_memory)vkFreeMemory(device,graphics_loop_snapshot_memory,nullptr);
            if(graphics_loop_timeline)vkDestroySemaphore(device,graphics_loop_timeline,nullptr);
            if(graphics_sampler)vkDestroySampler(device,graphics_sampler,nullptr);
            save_pipeline_cache(true);compile_stats_print(true);
            graphics_pipelines.clear();
            graphics_layout.reset();
            graphics_shaders.clear();
            if(pipeline_cache)vkDestroyPipelineCache(device,pipeline_cache,nullptr);
            pipeline_cache=VK_NULL_HANDLE;
            compute_pipelines.clear();
#endif
            if(pool)vkDestroyCommandPool(device,pool,nullptr);
#ifdef ANYPS3_RSX_VK
            if(transfer_pool)vkDestroyCommandPool(device,transfer_pool,nullptr);
            release_memory_cache(device);
#endif
            vkDestroyDevice(device,nullptr);
        }
        if(instance)vkDestroyInstance(instance,nullptr);
    }
    struct Work {
        VkDevice device;
        VkCommandPool pool;
        VkBuffer buffer=VK_NULL_HANDLE;
        VkDeviceMemory staging=VK_NULL_HANDLE,image_memory=VK_NULL_HANDLE;
        VkImage image=VK_NULL_HANDLE;
        VkImageView view=VK_NULL_HANDLE;
        VkRenderPass render_pass=VK_NULL_HANDLE;
        VkFramebuffer framebuffer=VK_NULL_HANDLE;
        VkCommandBuffer command=VK_NULL_HANDLE;
        VkFence fence=VK_NULL_HANDLE;
        void* mapped=nullptr;
        ~Work(){
            // Resources can also be destroyed after a failed submit/wait.
            vkDeviceWaitIdle(device);
            if(mapped)vkUnmapMemory(device,staging);
            if(fence)vkDestroyFence(device,fence,nullptr);
            if(command)vkFreeCommandBuffers(device,pool,1,&command);
            if(framebuffer)vkDestroyFramebuffer(device,framebuffer,nullptr);
            if(render_pass)vkDestroyRenderPass(device,render_pass,nullptr);
            if(view)vkDestroyImageView(device,view,nullptr);
            if(image)vkDestroyImage(device,image,nullptr);
            if(buffer)vkDestroyBuffer(device,buffer,nullptr);
            if(image_memory)vkFreeMemory(device,image_memory,nullptr);
            if(staging)vkFreeMemory(device,staging,nullptr);
        }
    };
''' + COMPUTE_MEMBERS + GRAPHICS_MEMBERS + PRESENT_MEMBERS + CONDITIONAL_MEMBERS + r'''
public:
    std::string device_name;
    std::uint32_t device_type=0;
    std::uint64_t clears=0,readbacks=0;
    explicit NativeVulkan(std::shared_ptr<VulkanHostService> host={}):graphics_host(host?std::move(host):std::make_shared<VulkanHostService>()){
        try{
            VkApplicationInfo app{};app.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO;
            app.pApplicationName="AnyPS3";app.apiVersion=VK_API_VERSION_1_0;
#ifdef ANYPS3_RSX_VK
            app.apiVersion=VK_API_VERSION_1_2;
#endif
            VkInstanceCreateInfo info{};info.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;info.pApplicationInfo=&app;
#ifdef ANYPS3_RSX_VK
            const auto present_extensions=graphics_present_extensions();info.enabledExtensionCount=present_extensions.size();info.ppEnabledExtensionNames=present_extensions.data();
#endif
            check(vkCreateInstance(&info,nullptr,&instance),"vkCreateInstance");
            std::uint32_t count=0;
            check(vkEnumeratePhysicalDevices(instance,&count,nullptr),"vkEnumeratePhysicalDevices");
            if(!count)throw std::runtime_error("Vulkan has no physical devices (install a GPU ICD or Lavapipe)");
            std::vector<VkPhysicalDevice> devices(count);
            check(vkEnumeratePhysicalDevices(instance,&count,devices.data()),"vkEnumeratePhysicalDevices");
            const char* selection=std::getenv("ANYPS3_VK_DEVICE");
            const std::string requested=selection?selection:"auto";
            if(requested!="auto" && requested!="gpu" && requested!="lavapipe")
                throw std::runtime_error("invalid ANYPS3_VK_DEVICE: expected auto, gpu or lavapipe");
            int best=-1;
            for(std::uint32_t d=0;d<count;++d){
                VkPhysicalDeviceProperties properties{};vkGetPhysicalDeviceProperties(devices[d],&properties);
                if(requested=="gpu" && properties.deviceType!=VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU &&
                   properties.deviceType!=VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU)continue;
                if(requested=="lavapipe" && (properties.deviceType!=VK_PHYSICAL_DEVICE_TYPE_CPU ||
                   (std::string(properties.deviceName).find("llvmpipe")==std::string::npos &&
                    std::string(properties.deviceName).find("lavapipe")==std::string::npos)))continue;
                VkFormatProperties format{};vkGetPhysicalDeviceFormatProperties(devices[d],VK_FORMAT_R8G8B8A8_UNORM,&format);
                if(!(format.optimalTilingFeatures&VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT))continue;
                std::uint32_t n=0;vkGetPhysicalDeviceQueueFamilyProperties(devices[d],&n,nullptr);
                std::vector<VkQueueFamilyProperties> queues(n);
                vkGetPhysicalDeviceQueueFamilyProperties(devices[d],&n,queues.data());
                for(std::uint32_t i=0;i<n;++i)if(queues[i].queueCount && (queues[i].queueFlags&VK_QUEUE_GRAPHICS_BIT)
#ifdef ANYPS3_RSX_VK
                    && (queues[i].queueFlags&VK_QUEUE_COMPUTE_BIT)
#endif
                    ){
                    const int score=properties.deviceType==VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU?3:
                        properties.deviceType==VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU?2:1;
                    if(score>best){best=score;physical=devices[d];family=i;device_name=properties.deviceName;device_type=properties.deviceType;}
                    break;
                }
            }
            if(!physical)throw std::runtime_error("Vulkan has no matching device/graphics queue supporting RGBA8 (ANYPS3_VK_DEVICE="+requested+")");
            vkGetPhysicalDeviceMemoryProperties(physical,&memory);
            const float priority=1;
            VkDeviceQueueCreateInfo q{};q.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
            q.queueFamilyIndex=family;q.queueCount=1;q.pQueuePriorities=&priority;
            #ifdef ANYPS3_RSX_VK
            vkGetPhysicalDeviceFeatures(physical,&graphics_features);vkGetPhysicalDeviceProperties(physical,&graphics_properties);
            graphics_subgroups.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_PROPERTIES;VkPhysicalDeviceProperties2 properties2{};properties2.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;properties2.pNext=&graphics_subgroups;vkGetPhysicalDeviceProperties2(physical,&properties2);
            VkPhysicalDeviceFeatures enabled{};enabled.textureCompressionBC=graphics_features.textureCompressionBC;enabled.independentBlend=graphics_features.independentBlend;
            enabled.sampleRateShading=graphics_features.sampleRateShading;enabled.depthBounds=graphics_features.depthBounds;enabled.occlusionQueryPrecise=graphics_features.occlusionQueryPrecise;enabled.fragmentStoresAndAtomics=graphics_features.fragmentStoresAndAtomics;enabled.logicOp=graphics_features.logicOp;enabled.shaderFloat64=graphics_features.shaderFloat64;enabled.shaderClipDistance=graphics_features.shaderClipDistance;enabled.vertexPipelineStoresAndAtomics=graphics_features.vertexPipelineStoresAndAtomics;
#endif
            VkDeviceCreateInfo di{};di.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;di.queueCreateInfoCount=1;di.pQueueCreateInfos=&q;
            #ifdef ANYPS3_RSX_VK
            VkDeviceQueueCreateInfo queue_infos[2]{q,q};
            {const char* v=std::getenv("ANYPS3_RSX_ASYNC_UPLOAD");if(v && std::strcmp(v,"0") && std::strcmp(v,"1"))throw std::runtime_error("invalid ANYPS3_RSX_ASYNC_UPLOAD");
                if(!v || !std::strcmp(v,"1")){
                    std::uint32_t n=0;vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,nullptr);std::vector<VkQueueFamilyProperties> families(n);vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,families.data());
                    for(std::uint32_t i=0;i<n;++i)if(i!=family && families[i].queueCount && (families[i].queueFlags&VK_QUEUE_TRANSFER_BIT) &&
                        !(families[i].queueFlags&~(VK_QUEUE_TRANSFER_BIT|VK_QUEUE_SPARSE_BINDING_BIT)) && families[i].timestampValidBits){transfer_family=i;transfer_timestamp_bits=families[i].timestampValidBits;break;}}}
            if(transfer_family!=UINT32_MAX){queue_infos[1].queueFamilyIndex=transfer_family;di.queueCreateInfoCount=2;di.pQueueCreateInfos=queue_infos;}
            #endif
            #ifdef ANYPS3_RSX_VK
            VkPhysicalDeviceTimelineSemaphoreFeatures timeline{};timeline.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_TIMELINE_SEMAPHORE_FEATURES;
            std::uint32_t extension_count=0;check(vkEnumerateDeviceExtensionProperties(physical,nullptr,&extension_count,nullptr),"device extension count");
            std::vector<VkExtensionProperties> extensions(extension_count);check(vkEnumerateDeviceExtensionProperties(physical,nullptr,&extension_count,extensions.data()),"device extensions");
            bool conditional_available=false;for(const auto& e:extensions)conditional_available|=std::strcmp(e.extensionName,VK_EXT_CONDITIONAL_RENDERING_EXTENSION_NAME)==0;
            // Layered MSAA planes: one draw instance per sample plane writes gl_Layer.
            bool layer_available=false;for(const auto& e:extensions)layer_available|=std::strcmp(e.extensionName,VK_EXT_SHADER_VIEWPORT_INDEX_LAYER_EXTENSION_NAME)==0;
            // Opt-in (ANYPS3_RSX_LAYERED=1): bit-identical, but on the RTX 4070 SUPER
            // level replay it raised GPU busy time per frame ~35 -> 50..90 ms.
            {const char* value=std::getenv("ANYPS3_RSX_LAYERED");if(value && std::strcmp(value,"0") && std::strcmp(value,"1"))throw std::runtime_error("invalid ANYPS3_RSX_LAYERED");if(!value || !std::strcmp(value,"0"))layer_available=false;}
            conditional_features.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_CONDITIONAL_RENDERING_FEATURES_EXT;
            // Multiview MSAA planes (opt-in ANYPS3_RSX_MULTIVIEW=1; bit-identical, render passes 1712->76/frame,
            // but RTX 4070 SUPER replay GPU wait 5.6->30 ms/frame and no CPU gain, like gl_Layer): one draw and one render pass
            // write every sample plane; the vertex shader picks the plane shift from gl_ViewIndex.
            VkPhysicalDeviceMultiviewFeatures multiview_features{};multiview_features.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MULTIVIEW_FEATURES;
            if(conditional_available)timeline.pNext=&conditional_features;
            multiview_features.pNext=timeline.pNext;timeline.pNext=&multiview_features;
            VkPhysicalDeviceHostQueryResetFeatures host_reset{};host_reset.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_HOST_QUERY_RESET_FEATURES;host_reset.pNext=timeline.pNext;timeline.pNext=&host_reset;
            VkPhysicalDeviceFeatures2 features2{};features2.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;features2.pNext=&timeline;vkGetPhysicalDeviceFeatures2(physical,&features2);
            graphics_host_query_reset=host_reset.hostQueryReset;
            bool multiview_available=graphics_properties.apiVersion>=VK_API_VERSION_1_2 && multiview_features.multiview && !layer_available;
            {const char* value=std::getenv("ANYPS3_RSX_MULTIVIEW");if(value && std::strcmp(value,"0") && std::strcmp(value,"1"))throw std::runtime_error("invalid ANYPS3_RSX_MULTIVIEW");if(!value || !std::strcmp(value,"0"))multiview_available=false;}
            multiview_features.multiviewGeometryShader=VK_FALSE;multiview_features.multiviewTessellationShader=VK_FALSE;multiview_features.multiview=multiview_available?VK_TRUE:VK_FALSE;
            if(graphics_properties.apiVersion<VK_API_VERSION_1_2 || !timeline.timelineSemaphore)throw std::runtime_error("RSX Vulkan requires Vulkan 1.2 timeline semaphores");
            conditional_available=conditional_available && conditional_features.conditionalRendering;
            conditional_features.inheritedConditionalRendering=VK_FALSE;
            if(!conditional_available)multiview_features.pNext=nullptr;
            di.pNext=&timeline;di.pEnabledFeatures=&enabled;
            std::vector<const char*> device_extensions;
            if(graphics_wsi)device_extensions.push_back(VK_KHR_SWAPCHAIN_EXTENSION_NAME);
            if(conditional_available)device_extensions.push_back(VK_EXT_CONDITIONAL_RENDERING_EXTENSION_NAME);
            if(layer_available)device_extensions.push_back(VK_EXT_SHADER_VIEWPORT_INDEX_LAYER_EXTENSION_NAME);
            graphics_multiview=multiview_available;graphics_layered_output=layer_available||multiview_available;
            rsx_layered_shaders().store(layer_available||multiview_available,std::memory_order_relaxed);rsx_multiview_shaders().store(multiview_available,std::memory_order_relaxed);
            di.enabledExtensionCount=device_extensions.size();di.ppEnabledExtensionNames=device_extensions.data();
#endif
            check(vkCreateDevice(physical,&di,nullptr,&device),"vkCreateDevice");
#ifdef ANYPS3_RSX_VK
            configure_memory_cache(device);
#endif
            vkGetDeviceQueue(device,family,0,&queue);
#ifdef ANYPS3_RSX_VK
            if(transfer_family!=UINT32_MAX){
                vkGetDeviceQueue(device,transfer_family,0,&transfer_queue);
                VkCommandPoolCreateInfo tpi{};tpi.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;tpi.queueFamilyIndex=transfer_family;tpi.flags=VK_COMMAND_POOL_CREATE_TRANSIENT_BIT|VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
                check(vkCreateCommandPool(device,&tpi,nullptr,&transfer_pool),"transfer command pool");
            }
            if(conditional_available){
                conditional_begin=reinterpret_cast<PFN_vkCmdBeginConditionalRenderingEXT>(vkGetDeviceProcAddr(device,"vkCmdBeginConditionalRenderingEXT"));
                conditional_end=reinterpret_cast<PFN_vkCmdEndConditionalRenderingEXT>(vkGetDeviceProcAddr(device,"vkCmdEndConditionalRenderingEXT"));
                if(!graphics_conditional_rendering())throw std::runtime_error("Vulkan conditional rendering entry points unavailable");
            }
            init_pipeline_cache();
            VkSemaphoreTypeCreateInfo timeline_type{};timeline_type.sType=VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;timeline_type.semaphoreType=VK_SEMAPHORE_TYPE_TIMELINE;
            VkSemaphoreCreateInfo timeline_info{};timeline_info.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;timeline_info.pNext=&timeline_type;
            check(vkCreateSemaphore(device,&timeline_info,nullptr,&graphics_timeline),"graphics timeline");
            if(transfer_queue)check(vkCreateSemaphore(device,&timeline_info,nullptr,&graphics_upload_timeline),"graphics upload timeline");
#endif
            VkCommandPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
            pi.queueFamilyIndex=family;pi.flags=VK_COMMAND_POOL_CREATE_TRANSIENT_BIT|VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
            check(vkCreateCommandPool(device,&pi,nullptr,&pool),"vkCreateCommandPool");
#ifdef ANYPS3_RSX_VK
            recorder_start();completion_start();
#endif
            std::fprintf(stderr,"native Vulkan device=%s type=%u\n",device_name.c_str(),device_type);
        }catch(...){destroy();throw;}
    }
    ~NativeVulkan(){destroy();}
    NativeVulkan(const NativeVulkan&)=delete;
    NativeVulkan& operator=(const NativeVulkan&)=delete;

    // Packed host RGBA8. Upload preserves pixels outside the clear rectangle.
    // Download completes before the caller changes guest memory or FIFO GET.
    void clear(std::vector<std::uint8_t>& rgba,std::uint32_t width,std::uint32_t height,
               std::uint32_t x,std::uint32_t y,std::uint32_t w,std::uint32_t h,std::uint32_t argb,bool capture_only=false){
        if(!width || !height || width>8192 || height>8192 || rgba.size()!=std::uint64_t(width)*height*4 ||
           x>width || y>height || w>width-x || h>height-y)throw std::runtime_error("invalid Vulkan clear extent");
        if((!w || !h) && !capture_only)return;
        Work work{device,pool};
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=rgba.size();
        bi.usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;bi.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
        check(vkCreateBuffer(device,&bi,nullptr,&work.buffer),"vkCreateBuffer");
        VkMemoryRequirements req{};vkGetBufferMemoryRequirements(device,work.buffer,&req);
        VkMemoryAllocateInfo alloc{};alloc.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;alloc.allocationSize=req.size;
        alloc.memoryTypeIndex=host_read_memory_type(req.memoryTypeBits);
        const bool coherent=memory.memoryTypes[alloc.memoryTypeIndex].propertyFlags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        check(vkAllocateMemory(device,&alloc,nullptr,&work.staging),"vkAllocateMemory(staging)");
        check(vkBindBufferMemory(device,work.buffer,work.staging,0),"vkBindBufferMemory");
        check(vkMapMemory(device,work.staging,0,VK_WHOLE_SIZE,0,&work.mapped),"vkMapMemory");
        std::memcpy(work.mapped,rgba.data(),rgba.size());
        VkMappedMemoryRange range{};range.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;range.memory=work.staging;range.size=VK_WHOLE_SIZE;
        if(!coherent)check(vkFlushMappedMemoryRanges(device,1,&range),"vkFlushMappedMemoryRanges");
        VkImageCreateInfo ii{};ii.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ii.imageType=VK_IMAGE_TYPE_2D;
        ii.format=VK_FORMAT_R8G8B8A8_UNORM;ii.extent={width,height,1};ii.mipLevels=1;ii.arrayLayers=1;
        ii.samples=VK_SAMPLE_COUNT_1_BIT;ii.tiling=VK_IMAGE_TILING_OPTIMAL;
        ii.usage=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        ii.sharingMode=VK_SHARING_MODE_EXCLUSIVE;ii.initialLayout=VK_IMAGE_LAYOUT_UNDEFINED;
        check(vkCreateImage(device,&ii,nullptr,&work.image),"vkCreateImage");
        vkGetImageMemoryRequirements(device,work.image,&req);alloc.allocationSize=req.size;
        alloc.memoryTypeIndex=memory_type(req.memoryTypeBits,0);
        check(vkAllocateMemory(device,&alloc,nullptr,&work.image_memory),"vkAllocateMemory(image)");
        check(vkBindImageMemory(device,work.image,work.image_memory,0),"vkBindImageMemory");
        VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        vi.image=work.image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D;vi.format=ii.format;
        vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
        check(vkCreateImageView(device,&vi,nullptr,&work.view),"vkCreateImageView");
        VkAttachmentDescription attachment{};attachment.format=ii.format;attachment.samples=ii.samples;
        attachment.loadOp=VK_ATTACHMENT_LOAD_OP_LOAD;attachment.storeOp=VK_ATTACHMENT_STORE_OP_STORE;
        attachment.stencilLoadOp=VK_ATTACHMENT_LOAD_OP_DONT_CARE;attachment.stencilStoreOp=VK_ATTACHMENT_STORE_OP_DONT_CARE;
        attachment.initialLayout=VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;attachment.finalLayout=VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        VkAttachmentReference color{0,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
        VkSubpassDescription subpass{};subpass.pipelineBindPoint=VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount=1;subpass.pColorAttachments=&color;
        VkRenderPassCreateInfo ri{};ri.sType=VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
        ri.attachmentCount=1;ri.pAttachments=&attachment;ri.subpassCount=1;ri.pSubpasses=&subpass;
        check(vkCreateRenderPass(device,&ri,nullptr,&work.render_pass),"vkCreateRenderPass");
        VkFramebufferCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        fi.renderPass=work.render_pass;fi.attachmentCount=1;fi.pAttachments=&work.view;fi.width=width;fi.height=height;fi.layers=1;
        check(vkCreateFramebuffer(device,&fi,nullptr,&work.framebuffer),"vkCreateFramebuffer");
        VkCommandBufferAllocateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        ci.commandPool=pool;ci.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ci.commandBufferCount=1;
        check(vkAllocateCommandBuffers(device,&ci,&work.command),"vkAllocateCommandBuffers");
        VkCommandBufferBeginInfo begin{};begin.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;begin.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        check(vkBeginCommandBuffer(work.command,&begin),"vkBeginCommandBuffer");
        auto transition=[&](VkImageLayout old_layout,VkImageLayout new_layout,VkAccessFlags src,VkAccessFlags dst,VkPipelineStageFlags from,VkPipelineStageFlags to){
            VkImageMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            barrier.srcAccessMask=src;barrier.dstAccessMask=dst;barrier.oldLayout=old_layout;barrier.newLayout=new_layout;
            barrier.srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
            barrier.image=work.image;barrier.subresourceRange=vi.subresourceRange;
            vkCmdPipelineBarrier(work.command,from,to,0,0,nullptr,0,nullptr,1,&barrier);
        };
        transition(VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,0,VK_ACCESS_TRANSFER_WRITE_BIT,
                   VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT);
        VkBufferImageCopy copy{};copy.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};copy.imageExtent=ii.extent;
        vkCmdCopyBufferToImage(work.command,work.buffer,work.image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
        transition(VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_ACCESS_TRANSFER_WRITE_BIT,
                   VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);
        VkRenderPassBeginInfo pass{};pass.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;pass.renderPass=work.render_pass;
        pass.framebuffer=work.framebuffer;pass.renderArea={{0,0},{width,height}};
        vkCmdBeginRenderPass(work.command,&pass,VK_SUBPASS_CONTENTS_INLINE);
        VkClearAttachment clear{};clear.aspectMask=VK_IMAGE_ASPECT_COLOR_BIT;clear.colorAttachment=0;
        clear.clearValue.color.float32[0]=float((argb>>16)&255)/255;
        clear.clearValue.color.float32[1]=float((argb>>8)&255)/255;
        clear.clearValue.color.float32[2]=float(argb&255)/255;
        clear.clearValue.color.float32[3]=float(argb>>24)/255;
        VkClearRect rect{{{static_cast<std::int32_t>(x),static_cast<std::int32_t>(y)},{w,h}},0,1};
        if(!capture_only)vkCmdClearAttachments(work.command,1,&clear,1,&rect);
        vkCmdEndRenderPass(work.command);
        transition(VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                   VK_ACCESS_TRANSFER_READ_BIT,VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT);
        // The staging buffer changes from an upload source to a download target.
        VkBufferMemoryBarrier bb{};bb.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
        bb.srcAccessMask=VK_ACCESS_TRANSFER_READ_BIT;bb.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;
        bb.srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;bb.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
        bb.buffer=work.buffer;bb.size=VK_WHOLE_SIZE;
        vkCmdPipelineBarrier(work.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,1,&bb,0,nullptr);
        vkCmdCopyImageToBuffer(work.command,work.image,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,work.buffer,1,&copy);
        bb.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;bb.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
        vkCmdPipelineBarrier(work.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,0,nullptr,1,&bb,0,nullptr);
        check(vkEndCommandBuffer(work.command),"vkEndCommandBuffer");
        VkFenceCreateInfo fence{};fence.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        check(vkCreateFence(device,&fence,nullptr,&work.fence),"vkCreateFence");
        VkSubmitInfo submit{};submit.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;submit.commandBufferCount=1;submit.pCommandBuffers=&work.command;
        check(vkQueueSubmit(queue,1,&submit,work.fence),"vkQueueSubmit");
        check(vkWaitForFences(device,1,&work.fence,VK_TRUE,UINT64_MAX),"vkWaitForFences");
        if(!coherent)check(vkInvalidateMappedMemoryRanges(device,1,&range),"vkInvalidateMappedMemoryRanges");
        std::memcpy(rgba.data(),work.mapped,rgba.size());
        if(capture_only)++readbacks;else ++clears;
    }
    void readback(std::vector<std::uint8_t>& rgba,std::uint32_t width,std::uint32_t height){
        clear(rgba,width,height,0,0,0,0,0,true);
    }
};
#endif
'''
