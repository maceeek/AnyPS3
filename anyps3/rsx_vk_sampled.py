"""Sampled images uploaded once per coherent RSX texture cache entry."""
MEMBERS = r'''
public:
    struct SampledImage:GraphicsImage {
        unsigned volume_depth,mip_levels;
        std::array<unsigned,16> sampling;
        bool extended_sampler;
        SampledImage(NativeVulkan* o,VkFormat f,unsigned w,unsigned h,unsigned z,unsigned layers,unsigned mips,
                std::array<unsigned,16> key,bool extended):GraphicsImage(o,f,w,h,layers,false),
                volume_depth(z),mip_levels(mips),sampling(key),extended_sampler(extended){}
    };
    static std::array<unsigned,16> sampled_key(const VkSamplerCreateInfo& s){
        auto bits=[](float f){unsigned v;std::memcpy(&v,&f,4);return v;};
        return {s.flags,unsigned(s.magFilter),unsigned(s.minFilter),unsigned(s.mipmapMode),
            unsigned(s.addressModeU),unsigned(s.addressModeV),unsigned(s.addressModeW),bits(s.mipLodBias),
            s.anisotropyEnable,bits(s.maxAnisotropy),s.compareEnable,unsigned(s.compareOp),
            bits(s.minLod),bits(s.maxLod),unsigned(s.borderColor),s.unnormalizedCoordinates};
    }
    bool graphics_sampled_format(VkFormat f,bool bc=false)const{VkFormatProperties p{};vkGetPhysicalDeviceFormatProperties(physical,f,&p);return (!bc || graphics_features.textureCompressionBC) && (p.optimalTilingFeatures&VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT);}
    std::uint64_t sampled_image_creations=0,sampled_image_reuses=0;
    std::shared_ptr<SampledImage> graphics_sampled_image(GraphicsBatch& b,VkFormat format,unsigned width,unsigned height,unsigned depth,unsigned faces,unsigned mips,
            const std::vector<std::uint8_t>& pixels,std::vector<VkBufferImageCopy> copies,const VkSamplerCreateInfo& sampler,std::uint64_t device_offset=UINT64_MAX,
            const std::shared_ptr<SampledImage>& reusable={}){b.sync();
        GraphicsSite site(SiteTexUpload);
        const bool depth_format=format==VK_FORMAT_D24_UNORM_S8_UINT || format==VK_FORMAT_D16_UNORM;
        const VkImageAspectFlags aspect=depth_format?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT;
        const VkImageAspectFlags transition=aspect|(format==VK_FORMAT_D24_UNORM_S8_UINT?VK_IMAGE_ASPECT_STENCIL_BIT:0);
        const auto key=sampled_key(sampler);auto image=reusable;
        if(image && (image->format!=format || image->width!=width || image->height!=height || image->layers!=faces ||
                image->volume_depth!=depth || image->mip_levels!=mips || image->sampling!=key || image->extended_sampler || sampler.pNext))
            throw std::runtime_error("RSX sampled image reuse identity mismatch");
        if(!image){
        image=std::make_shared<SampledImage>(this,format,width,height,depth,faces,mips,key,sampler.pNext!=nullptr);
        VkImageCreateInfo ii{};ii.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ii.imageType=depth>1?VK_IMAGE_TYPE_3D:VK_IMAGE_TYPE_2D;ii.format=format;ii.extent={width,height,depth};ii.mipLevels=mips;ii.arrayLayers=faces;
        ii.samples=VK_SAMPLE_COUNT_1_BIT;ii.tiling=VK_IMAGE_TILING_OPTIMAL;ii.usage=VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_SAMPLED_BIT;
        check(vkCreateImage(device,&ii,nullptr,&image->image),"sampled image");VkMemoryRequirements r{};vkGetImageMemoryRequirements(device,image->image,&r);
        VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        image->memory_bytes=r.size;check(vkAllocateMemory(device,&ai,nullptr,&image->memory),"sampled memory");check(vkBindImageMemory(device,image->image,image->memory,0),"sampled bind");
        VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=image->image;vi.viewType=depth>1?VK_IMAGE_VIEW_TYPE_3D:VK_IMAGE_VIEW_TYPE_2D_ARRAY;vi.format=format;vi.subresourceRange={aspect,0,mips,0,faces};
        if(depth_format)vi.components={VK_COMPONENT_SWIZZLE_R,VK_COMPONENT_SWIZZLE_R,VK_COMPONENT_SWIZZLE_R,VK_COMPONENT_SWIZZLE_R};
        VkImageView view;check(vkCreateImageView(device,&vi,nullptr,&view),"sampled view");image->views.push_back(view);
        check(vkCreateSampler(device,&sampler,nullptr,&image->sampler),"sampled sampler");
        ++sampled_image_creations;
        }else ++sampled_image_reuses;
        const auto at=device_offset==UINT64_MAX?graphics_upload(b,pixels.data(),pixels.size()):device_offset;for(auto& c:copies)c.bufferOffset+=at;
        graphics_begin(b);graphics_end_pass(b);VkImageMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
        // One queue owns these images. Preserve reads from earlier commands
        // (also earlier submissions) before updating the same allocation.
        // No CPU wait or image destruction is needed for a new generation.
        barrier.oldLayout=reusable?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_UNDEFINED;barrier.newLayout=VK_IMAGE_LAYOUT_GENERAL;barrier.image=image->image;barrier.subresourceRange={transition,0,mips,0,faces};barrier.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;
        if(reusable)barrier.srcAccessMask=VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT;
        vkCmdPipelineBarrier(b.command,reusable?VK_PIPELINE_STAGE_ALL_COMMANDS_BIT:VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,0,nullptr,1,&barrier);
        vkCmdCopyBufferToImage(b.command,device_offset==UINT64_MAX?b.buffer:b.data_buffer,image->image,VK_IMAGE_LAYOUT_GENERAL,copies.size(),copies.data());
        barrier.oldLayout=VK_IMAGE_LAYOUT_GENERAL;barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_SHADER_READ_BIT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,0,0,nullptr,0,nullptr,1,&barrier);b.pins.push_back(image);return image;
    }
'''
