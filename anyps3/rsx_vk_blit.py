"""Device-only nearest copies of packed D24S8, with exact integer channel bytes."""
MEMBERS = r'''
    std::uint64_t graphics_scaled_depth_budget(unsigned sw,unsigned sh,unsigned dw,unsigned dh)const{
        const auto scale=graphics_render_scale,factor=scale>1?1+scale*scale:1;
        const auto alignment=std::max<std::uint64_t>(256,graphics_properties.limits.minStorageBufferOffsetAlignment);
        return (std::uint64_t(sw)*sh+std::uint64_t(dw)*dh)*5*factor+12*alignment+128;
    }
    // Guest mapping, tile identity, permissions and ownership are the caller's
    // responsibility. This primitive copies complete, single-sample images.
    bool graphics_scaled_depth_bytes(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& source,
            const std::shared_ptr<GraphicsImage>& destination,unsigned dx,unsigned dy){
        if(!source || !destination || source==destination || !source->depth ||
           source->format!=VK_FORMAT_D24_UNORM_S8_UINT || source->layers!=1 || destination->layers!=1 ||
           !dx || !dy || dx>INT32_MAX || dy>INT32_MAX)return false;
        const bool depth=destination->depth;
        if(destination->format!=(depth?VK_FORMAT_D24_UNORM_S8_UINT:VK_FORMAT_R8G8B8A8_UNORM))return false;
        const unsigned scale=graphics_render_scale;
        if(!source->width || !source->height || !destination->width || !destination->height)return false;
        if(scale>1 && (!source->visual || !destination->visual ||
           source->visual->width!=source->width*scale || source->visual->height!=source->height*scale ||
           destination->visual->width!=destination->width*scale || destination->visual->height!=destination->height*scale))return false;
        if((std::uint64_t(destination->width)*scale-1)*dx>UINT32_MAX ||
           (std::uint64_t(destination->height)*scale-1)*dy>UINT32_MAX)return false;
        // Admit the entire native/visual pair before recording either half.
        const auto alignment=std::max<std::uint64_t>(256,graphics_properties.limits.minStorageBufferOffsetAlignment);
        auto end=b.used;
        auto space=[&](std::uint64_t n){end=(end+alignment-1)&~(alignment-1);end+=n;};
        for(unsigned pass=0;pass<(scale>1?2u:1u);++pass){
            const auto k=pass?scale:1u;
            const auto input=std::uint64_t(source->width)*source->height*k*k;
            const auto output=std::uint64_t(destination->width)*destination->height*k*k;
            if(input>UINT32_MAX/4 || output>UINT32_MAX/4)return false;
            const auto groups=(output+255)/256,gx=std::min<std::uint64_t>(groups,graphics_properties.limits.maxComputeWorkGroupCount[0]);
            if(!gx || (groups+gx-1)/gx>graphics_properties.limits.maxComputeWorkGroupCount[1])return false;
            space(input*4);space((input+3)&~UINT64_C(3));space(output*4);space((output+3)&~UINT64_C(3));space(12*4);
        }
        if(end>b.capacity)return false;
        b.sync();graphics_begin(b);
        static const std::string code=R"BLIT(#version 450
layout(local_size_x=64) in;
layout(std430,binding=0) buffer Data{uint w[];};
layout(push_constant) uniform Base{uint base_;};
uint P(uint n){return w[base_+n];}
void main(){
 uint group=gl_GlobalInvocationID.x+gl_WorkGroupID.y*gl_NumWorkGroups.x*64u;
 uint first=group*4u,count=P(6u)*P(7u);if(first>=count)return;
 uint packed=0u;
 for(uint j=0u;j<4u && first+j<count;++j){uint i=first+j;
  uint x=min(((i%P(6u))*P(8u))>>20u,P(4u)-1u);
  uint y=min(((i/P(6u))*P(9u))>>20u,P(5u)-1u);
  uint q=y*P(4u)+x,d=w[P(0u)+q]&0xffffffu,at=P(1u)+q;
  uint s=(w[at/4u]>>(8u*(at%4u)))&255u;
  w[P(2u)+i]=P(10u)!=0u?d:((d>>8u)&255u)|((d&255u)<<8u)|(s<<16u)|((d>>16u)<<24u);
  packed|=s<<(8u*j);
 }
 if(P(10u)!=0u)w[P(3u)+group]=packed;
})BLIT";
        auto pipeline=compute_pipeline(code);b.compute_pins.push_back(pipeline);
        const auto set=graphics_descriptor_set(b,pipeline->descriptors,"depth scaled copy descriptor");
        VkDescriptorBufferInfo buffer{b.data_buffer,0,b.capacity};VkWriteDescriptorSet write{};
        write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.descriptorCount=1;
        write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&buffer;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);
        for(unsigned pass=0;pass<(scale>1?2u:1u);++pass){
            const auto from=pass?source->visual:source,to=pass?destination->visual:destination;
            const auto input=std::uint64_t(from->width)*from->height,output=std::uint64_t(to->width)*to->height;
            const auto src=graphics_space(b,input*4),ss=graphics_space(b,(input+3)&~UINT64_C(3));
            const auto dst=graphics_space(b,output*4),ds=graphics_space(b,(output+3)&~UINT64_C(3));
            graphics_barrier(b);
            VkBufferImageCopy copy{};copy.bufferOffset=src;copy.imageSubresource={VK_IMAGE_ASPECT_DEPTH_BIT,0,0,1};copy.imageExtent={from->width,from->height,1};
            vkCmdCopyImageToBuffer(b.command,from->image,VK_IMAGE_LAYOUT_GENERAL,b.data_buffer,1,&copy);
            copy.bufferOffset=ss;copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_STENCIL_BIT;
            vkCmdCopyImageToBuffer(b.command,from->image,VK_IMAGE_LAYOUT_GENERAL,b.data_buffer,1,&copy);
            const unsigned parameters[]={unsigned(src/4),unsigned(ss),unsigned(dst/4),unsigned(ds/4),from->width,from->height,to->width,to->height,dx,dy,unsigned(depth),0};
            const auto params=graphics_upload(b,parameters,sizeof(parameters));b.uploads.push_back({params,params,sizeof(parameters)});
            graphics_barrier(b);vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->pipeline);
            vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->layout,0,1,&set,0,nullptr);
            const unsigned base=params/4;vkCmdPushConstants(b.command,pipeline->layout,VK_SHADER_STAGE_COMPUTE_BIT,0,4,&base);
            const auto groups=(output+255)/256,gx=std::min<std::uint64_t>(groups,graphics_properties.limits.maxComputeWorkGroupCount[0]);
            vkCmdDispatch(b.command,gx,(groups+gx-1)/gx,1);graphics_barrier(b);
            copy.bufferOffset=dst;copy.imageSubresource.aspectMask=depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT;copy.imageExtent={to->width,to->height,1};
            vkCmdCopyBufferToImage(b.command,b.data_buffer,to->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);
            if(depth){copy.bufferOffset=ds;copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_STENCIL_BIT;
                vkCmdCopyBufferToImage(b.command,b.data_buffer,to->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);}
            graphics_barrier(b);b.pins.push_back(from);b.pins.push_back(to);graphics_visual_taint(*to,from->visual_taint,false);
        }
        return true;
    }
'''
