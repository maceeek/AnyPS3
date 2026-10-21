"""SDL2 Vulkan WSI transport; no host readback on a resident scanout."""
MEMBERS = r'''
#ifdef ANYPS3_RSX_VK
private:
    struct GraphicsWsi {
        NativeVulkan& vk;
        void* library=nullptr;void* window=nullptr;bool initialized=false,shown=false;
        unsigned width=0,height=0;bool vsync=true,overlay_bgra=false;
        VkSurfaceKHR surface=VK_NULL_HANDLE;VkSwapchainKHR swapchain=VK_NULL_HANDLE;
        std::vector<VkImage> images;VkExtent2D extent{};
        VkFence acquire=VK_NULL_HANDLE;std::vector<VkSemaphore> present_ready;
        int (*init)(unsigned)=nullptr;void (*quit)(unsigned)=nullptr;
        void* (*create)(const char*,int,int,int,int,unsigned)=nullptr;
        void (*destroy_window)(void*)=nullptr;void (*resize)(void*,int,int)=nullptr;void (*show)(void*)=nullptr;
        int (*extensions)(void*,unsigned*,const char**)=nullptr;int (*create_surface)(void*,VkInstance,VkSurfaceKHR*)=nullptr;
        const char* (*error)()=nullptr;void (*pump)()=nullptr;int (*peep)(void*,int,int,unsigned,unsigned)=nullptr;
        unsigned (*window_id)(void*)=nullptr;
        explicit GraphicsWsi(NativeVulkan& v):vk(v){}
        template<class T> T symbol(const char* name){auto* p=dlsym(library,name);if(!p)throw std::runtime_error(std::string("SDL Vulkan missing symbol ")+name);return reinterpret_cast<T>(p);}
        [[noreturn]] void fail(const char* op){throw std::runtime_error(std::string("SDL Vulkan ")+op+": "+error());}
        ~GraphicsWsi(){
            if(vk.device)vkDeviceWaitIdle(vk.device);
            if(swapchain)vkDestroySwapchainKHR(vk.device,swapchain,nullptr);
            if(acquire)vkDestroyFence(vk.device,acquire,nullptr);
            for(auto semaphore:present_ready)if(semaphore)vkDestroySemaphore(vk.device,semaphore,nullptr);
            if(surface)vkDestroySurfaceKHR(vk.instance,surface,nullptr);
            if(window || initialized || library)vk.graphics_host->invoke([&]{
                if(window)destroy_window(window);
                if(initialized)quit(0x20);
                if(library)dlclose(library);
            });
        }
        std::vector<const char*> open(){std::vector<const char*> result;vk.graphics_host->invoke([&]{result=open_host();});return result;}
        std::vector<const char*> open_host(){
            library=dlopen("libSDL2-2.0.so.0",RTLD_NOW|RTLD_LOCAL);if(!library)throw std::runtime_error("SDL Vulkan requires SDL2");
            init=symbol<decltype(init)>("SDL_InitSubSystem");quit=symbol<decltype(quit)>("SDL_QuitSubSystem");error=symbol<decltype(error)>("SDL_GetError");
            create=symbol<decltype(create)>("SDL_CreateWindow");destroy_window=symbol<decltype(destroy_window)>("SDL_DestroyWindow");
            resize=symbol<decltype(resize)>("SDL_SetWindowSize");show=symbol<decltype(show)>("SDL_ShowWindow");
            extensions=symbol<decltype(extensions)>("SDL_Vulkan_GetInstanceExtensions");create_surface=symbol<decltype(create_surface)>("SDL_Vulkan_CreateSurface");
            pump=symbol<decltype(pump)>("SDL_PumpEvents");peep=symbol<decltype(peep)>("SDL_PeepEvents");window_id=symbol<decltype(window_id)>("SDL_GetWindowID");
            if(init(0x20))fail("video initialization");
            initialized=true;window=create("AnyPS3 Vulkan guest scanout",0x2fff0000,0x2fff0000,64,64,0x10000008u);
            if(!window)fail("window creation");
            unsigned n=0;if(!extensions(window,&n,nullptr))fail("instance extensions");
            std::vector<const char*> names(n);if(!extensions(window,&n,names.data()))fail("instance extensions");return names;
        }
        void poll(){vk.graphics_host->invoke([&]{poll_host();});}
        void poll_host(){
            pump();alignas(void*) unsigned char event[128]{};int count;
            for(unsigned type:{0x100u,0x200u}){
                while((count=peep(event,1,2,type,type))>0){unsigned id=0;std::memcpy(&id,event+8,4);
                    if(type==0x100 || (event[12]==14 && id==window_id(window)))throw std::runtime_error("SDL Vulkan presentation window closed; run terminated");}
                if(count<0)fail("event read");
            }
        }
        void configure(unsigned w,unsigned h,bool sync){
            if(!surface){
                vk.graphics_host->invoke([&]{if(!create_surface(window,vk.instance,&surface))fail("surface creation");});
                VkBool32 supported=0;check(vkGetPhysicalDeviceSurfaceSupportKHR(vk.physical,vk.family,surface,&supported),"present queue support");
                if(!supported)throw std::runtime_error("Vulkan graphics queue cannot present SDL surface");
                VkFenceCreateInfo f{};f.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;check(vkCreateFence(vk.device,&f,nullptr,&acquire),"present acquire fence");
            }
            if(swapchain && width==w && height==h && vsync==sync)return;
            check(vkQueueWaitIdle(vk.queue),"present resize wait");
            vk.graphics_host->invoke([&]{resize(window,w,h);show(window);});shown=true;width=w;height=h;vsync=sync;
            VkSurfaceCapabilitiesKHR caps{};check(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(vk.physical,surface,&caps),"present capabilities");
            unsigned n=0;check(vkGetPhysicalDeviceSurfaceFormatsKHR(vk.physical,surface,&n,nullptr),"present formats");std::vector<VkSurfaceFormatKHR> formats(n);
            check(vkGetPhysicalDeviceSurfaceFormatsKHR(vk.physical,surface,&n,formats.data()),"present formats");
            VkSurfaceFormatKHR selected{};bool found=false;
            for(auto f:formats)if(f.format==VK_FORMAT_B8G8R8A8_UNORM || f.format==VK_FORMAT_R8G8B8A8_UNORM){selected=f;found=true;break;}
            if(!found)throw std::runtime_error("Vulkan scanout requires an UNORM surface format");
            VkFormatProperties props{};vkGetPhysicalDeviceFormatProperties(vk.physical,selected.format,&props);
            if(!(props.optimalTilingFeatures&VK_FORMAT_FEATURE_BLIT_DST_BIT) || !(caps.supportedUsageFlags&VK_IMAGE_USAGE_TRANSFER_DST_BIT))throw std::runtime_error("Vulkan scanout surface does not support blit destination");
            overlay_bgra=selected.format==VK_FORMAT_B8G8R8A8_UNORM;
            extent=caps.currentExtent;
            if(extent.width==UINT32_MAX)extent={std::clamp(w,caps.minImageExtent.width,caps.maxImageExtent.width),std::clamp(h,caps.minImageExtent.height,caps.maxImageExtent.height)};
            // The emulated 59.94 Hz vblank already paces guest flips. A host FIFO
            // swapchain would block the RSX thread on the compositor's refresh as
            // well (menu 59.9 -> 20 FPS in run 340save), so VSYNC flips use
            // tear-free, non-blocking MAILBOX; IMMEDIATE without VSYNC; FIFO is
            // the always-available fallback. ANYPS3_PRESENT_MODE=fifo|mailbox|immediate
            // forces a mode for diagnostics.
            check(vkGetPhysicalDeviceSurfacePresentModesKHR(vk.physical,surface,&n,nullptr),"present modes");std::vector<VkPresentModeKHR> modes(n);
            check(vkGetPhysicalDeviceSurfacePresentModesKHR(vk.physical,surface,&n,modes.data()),"present modes");
            auto has=[&](VkPresentModeKHR m){return std::find(modes.begin(),modes.end(),m)!=modes.end();};
            VkPresentModeKHR mode=VK_PRESENT_MODE_FIFO_KHR;
            // ANYPS3_VBLANK_HZ (emulated rate other than the default 60000/1001): the host monitor's FIFO refresh must not cap the
            // emulated rate, so VSYNC flips fall back to IMMEDIATE rather than FIFO when MAILBOX is missing.
            const char* vblank_hz=std::getenv("ANYPS3_VBLANK_HZ");const bool custom_rate=vblank_hz && std::string(vblank_hz)!="60000/1001";
            const std::vector<VkPresentModeKHR> preferred=sync?(custom_rate?std::vector<VkPresentModeKHR>{VK_PRESENT_MODE_MAILBOX_KHR,VK_PRESENT_MODE_IMMEDIATE_KHR}:
                std::vector<VkPresentModeKHR>{VK_PRESENT_MODE_MAILBOX_KHR}):
                std::vector<VkPresentModeKHR>{VK_PRESENT_MODE_IMMEDIATE_KHR,VK_PRESENT_MODE_MAILBOX_KHR};
            for(auto m:preferred)if(has(m)){mode=m;break;}
            if(const char* forced=std::getenv("ANYPS3_PRESENT_MODE")){
                const std::string f=forced;const auto m=f=="fifo"?VK_PRESENT_MODE_FIFO_KHR:f=="mailbox"?VK_PRESENT_MODE_MAILBOX_KHR:
                    f=="immediate"?VK_PRESENT_MODE_IMMEDIATE_KHR:VK_PRESENT_MODE_MAX_ENUM_KHR;
                if(m==VK_PRESENT_MODE_MAX_ENUM_KHR || !has(m))throw std::runtime_error("ANYPS3_PRESENT_MODE is not fifo/mailbox/immediate or unsupported by the surface");
                mode=m;
            }
            VkSwapchainCreateInfoKHR ci{};ci.sType=VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;ci.surface=surface;
            // Three images let MAILBOX/IMMEDIATE acquire without waiting for the compositor.
            ci.minImageCount=std::max(mode==VK_PRESENT_MODE_FIFO_KHR?2u:3u,caps.minImageCount);if(caps.maxImageCount)ci.minImageCount=std::min(ci.minImageCount,caps.maxImageCount);
            std::fprintf(stderr,"Vulkan WSI present mode=%s images>=%u guest_vsync=%d\n",mode==VK_PRESENT_MODE_MAILBOX_KHR?"mailbox":
                         mode==VK_PRESENT_MODE_IMMEDIATE_KHR?"immediate":"fifo",ci.minImageCount,int(sync));
            ci.imageFormat=selected.format;ci.imageColorSpace=selected.colorSpace;ci.imageExtent=extent;ci.imageArrayLayers=1;ci.imageUsage=VK_IMAGE_USAGE_TRANSFER_DST_BIT;
            ci.imageSharingMode=VK_SHARING_MODE_EXCLUSIVE;ci.preTransform=caps.currentTransform;ci.compositeAlpha=VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
            if(!(caps.supportedCompositeAlpha&ci.compositeAlpha))throw std::runtime_error("Vulkan scanout requires opaque composition");
            ci.presentMode=mode;ci.clipped=VK_TRUE;ci.oldSwapchain=swapchain;
            VkSwapchainKHR next=VK_NULL_HANDLE;check(vkCreateSwapchainKHR(vk.device,&ci,nullptr,&next),"present swapchain");
            if(swapchain)vkDestroySwapchainKHR(vk.device,swapchain,nullptr);
            for(auto semaphore:present_ready)if(semaphore)vkDestroySemaphore(vk.device,semaphore,nullptr);
            present_ready.clear();
            swapchain=next;check(vkGetSwapchainImagesKHR(vk.device,swapchain,&n,nullptr),"present images");images.resize(n);check(vkGetSwapchainImagesKHR(vk.device,swapchain,&n,images.data()),"present images");
            present_ready.resize(n,VK_NULL_HANDLE);
            VkSemaphoreCreateInfo sem{};sem.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
            for(auto& semaphore:present_ready)check(vkCreateSemaphore(vk.device,&sem,nullptr,&semaphore),"present image semaphore");
        }
        void display(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& source,unsigned w,unsigned h,bool sync,bool async){b.sync();
            configure(w,h,sync);poll();unsigned index=0;
            VkFormatProperties source_properties{};vkGetPhysicalDeviceFormatProperties(vk.physical,source->format,&source_properties);
            if(!(source_properties.optimalTilingFeatures&VK_FORMAT_FEATURE_BLIT_SRC_BIT))throw std::runtime_error("Vulkan scanout image does not support blit source");
            auto result=vkAcquireNextImageKHR(vk.device,swapchain,UINT64_MAX,VK_NULL_HANDLE,acquire,&index);
            if(result==VK_ERROR_OUT_OF_DATE_KHR){width=0;configure(w,h,sync);result=vkAcquireNextImageKHR(vk.device,swapchain,UINT64_MAX,VK_NULL_HANDLE,acquire,&index);}
            if(result!=VK_SUBOPTIMAL_KHR)check(result,"present acquire");
            check(vkWaitForFences(vk.device,1,&acquire,VK_TRUE,UINT64_MAX),"present acquire wait");check(vkResetFences(vk.device,1,&acquire),"present acquire reset");
            vk.graphics_begin(b);vk.graphics_barrier(b);
            VkImageMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;barrier.oldLayout=VK_IMAGE_LAYOUT_UNDEFINED;barrier.newLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.image=images[index];barrier.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};barrier.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;
            vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,0,nullptr,1,&barrier);
            VkImageBlit blit{};blit.srcSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};blit.dstSubresource=blit.srcSubresource;
            blit.srcOffsets[1]={int(w),int(h),1};blit.dstOffsets[1]={int(extent.width),int(extent.height),1};
            VkImageMemoryBarrier source_barrier{};source_barrier.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            source_barrier.srcQueueFamilyIndex=source_barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
            source_barrier.image=source->image;source_barrier.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
            source_barrier.oldLayout=VK_IMAGE_LAYOUT_GENERAL;source_barrier.newLayout=VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            source_barrier.srcAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;source_barrier.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT;
            vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,0,nullptr,1,&source_barrier);
            vkCmdBlitImage(b.command,source->image,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,images[index],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&blit,VK_FILTER_NEAREST);
            source_barrier.oldLayout=VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;source_barrier.newLayout=VK_IMAGE_LAYOUT_GENERAL;
            source_barrier.srcAccessMask=VK_ACCESS_TRANSFER_READ_BIT;source_barrier.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
            vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,0,nullptr,0,nullptr,1,&source_barrier);
#ifdef ANYPS3_VSH_OVERLAY_AVAILABLE
            // Compose host VSH only into the acquired WSI image after guest blit.
            auto panel=anyps3_vsh_overlay::raster(extent.width,extent.height,overlay_bgra);
            if(!panel.pixels.empty()){
                const auto at=vk.graphics_upload(b,panel.pixels.data(),panel.pixels.size());
                barrier.oldLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;barrier.newLayout=barrier.oldLayout;
                barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;
                vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,0,nullptr,1,&barrier);
                VkBufferImageCopy copy{};copy.bufferOffset=at;copy.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};
                copy.imageOffset={int(panel.x),int(panel.y),0};copy.imageExtent={panel.width,panel.height,1};
                vkCmdCopyBufferToImage(b.command,b.buffer,images[index],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
            }
#endif
            barrier.oldLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;barrier.newLayout=VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=0;
            vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,0,0,nullptr,0,nullptr,1,&barrier);
            // Acquire + fence wait completed the previous present of this
            // IMAGE. A submit fence/QueueWaitIdle alone cannot prove that the
            // presentation engine consumed a previous frame's semaphore.
            const auto ready=present_ready.at(index);b.signal=ready;if(async)vk.graphics_submit(b);else vk.graphics_finish(b);
            VkPresentInfoKHR pi{};pi.sType=VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;pi.waitSemaphoreCount=1;pi.pWaitSemaphores=&ready;pi.swapchainCount=1;pi.pSwapchains=&swapchain;pi.pImageIndices=&index;
            result=vkQueuePresentKHR(vk.queue,&pi);
            if(result!=VK_SUBOPTIMAL_KHR && result!=VK_ERROR_OUT_OF_DATE_KHR)check(result,"queue present");
            if(result!=VK_SUCCESS)width=0;
        }
    };
    std::unique_ptr<GraphicsWsi> graphics_wsi;
    std::vector<const char*> graphics_present_extensions(){
        const auto* env=std::getenv("ANYPS3_PRESENT");if(!env || std::strcmp(env,"sdl"))return {};
        graphics_wsi=std::make_unique<GraphicsWsi>(*this);return graphics_wsi->open();
    }
public:
    void graphics_present(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,unsigned w,unsigned h,bool vsync,bool async=false){b.sync();
        if(!graphics_wsi)throw std::runtime_error("Vulkan presentation was not initialized");
        graphics_wsi->display(b,image,w,h,vsync,async);
    }
private:
#endif
'''
