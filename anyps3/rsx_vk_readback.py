"""Device-side byte packing and physical-order gather for surface readback.

The immutable map is validated from the original Target layout on the host.
The compute shader preserves raw FP/depth bits; it never samples/converts floats.
"""
MEMBERS = r'''
    std::uint64_t graphics_readback_dispatches=0,graphics_rebind_packs=0;
    std::uint64_t graphics_pack_readback(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,
            unsigned format,bool depth,unsigned bpp,unsigned pitch,unsigned samples,
            const std::vector<unsigned>& words,std::shared_ptr<GraphicsBuffer>& word_buffer,bool download=true){
        GraphicsSite site(SitePack);
        if(words.empty() || !pitch || (samples!=1 && samples!=2 && samples!=4))throw std::runtime_error("invalid RSX readback layout");
        unsigned image_bpp=4;
        switch(image->format){case VK_FORMAT_D16_UNORM:case VK_FORMAT_R5G6B5_UNORM_PACK16:image_bpp=2;break;
            case VK_FORMAT_R16G16B16A16_SFLOAT:image_bpp=8;break;case VK_FORMAT_R32G32B32A32_SFLOAT:image_bpp=16;break;default:break;}
        const auto input_bytes=std::uint64_t(image->width)*image->height*image->layers*image_bpp;
        const bool stencil=depth && format==2;
        graphics_begin(b);
        const auto input=graphics_space(b,input_bytes),stencil_at=stencil?graphics_space(b,input_bytes/4):0;
        const auto table=graphics_space(b,words.size()*4),output=graphics_space(b,words.size()*4);
        if(!word_buffer)word_buffer=graphics_buffer(b,words.data(),words.size()*4);
        graphics_barrier(b);VkBufferCopy map_copy{0,table,words.size()*4};vkCmdCopyBuffer(b.command,word_buffer->buffer,b.data_buffer,1,&map_copy);
        VkBufferImageCopy copy{};copy.bufferOffset=input;copy.imageSubresource={depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,0,0,image->layers};copy.imageExtent={image->width,image->height,1};
        vkCmdCopyImageToBuffer(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,b.data_buffer,1,&copy);
        if(stencil){copy.bufferOffset=stencil_at;copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_STENCIL_BIT;vkCmdCopyImageToBuffer(b.command,image->image,VK_IMAGE_LAYOUT_GENERAL,b.data_buffer,1,&copy);}
        const unsigned parameters[]={unsigned(input/4),unsigned(stencil_at),unsigned(output/4),unsigned(table/4),unsigned(words.size()),image->width,image->height,samples,bpp,pitch,format,unsigned(depth),image_bpp,unsigned(image->format==VK_FORMAT_R32_UINT)};
        const auto params=graphics_upload(b,parameters,sizeof(parameters));
        b.uploads.push_back({params,params,sizeof(parameters)}); // immutable CPU slice: copied by the batch prelude
        static const std::string source=R"PACK(#version 450
layout(local_size_x=64) in;
layout(std430,binding=0) buffer Data {uint w[];};
layout(push_constant) uniform Base {uint base_;};
uint P(uint i){return w[base_+i];}
uint input_byte(uint byte_){uint at=4u*P(0u)+byte_;return (w[at/4u]>>(8u*(at%4u)))&255u;}
uint guest_byte(uint logical){
 uint bpp=P(8u),nx=P(7u)>1u?2u:1u,ny=P(7u)==4u?2u:1u;
 uint yy=logical/P(9u),xx=logical%P(9u);
 if(yy>=P(6u)*ny || xx>=P(5u)*nx*bpp)return 0u;
 uint x=xx/bpp,byte_=xx%bpp,layer=(yy%ny)*nx+x%nx;
 uint pixel=(layer*P(6u)+yy/ny)*P(5u)+x/nx;
 if(P(13u)!=0u)return (w[P(0u)+pixel]>>(8u*(bpp-1u-byte_)))&255u;
 if(P(11u)==0u && P(10u)==8u)return input_byte(pixel*4u+(byte_+3u)%4u);
 if(P(11u)!=0u && P(10u)==2u){uint at=P(1u)+pixel;uint v=((w[P(0u)+pixel]&0xffffffu)<<8u)|((w[at/4u]>>(8u*(at%4u)))&255u);return (v>>(8u*(3u-byte_)))&255u;}
 uint unit=(P(11u)!=0u || P(10u)==3u || P(10u)==11u)?2u:4u;
 return input_byte(pixel*P(12u)+(byte_^(unit-1u)));
}
void main(){uint index_=gl_GlobalInvocationID.x+gl_WorkGroupID.y*gl_NumWorkGroups.x*64u;if(index_>=P(4u))return;
 uint logical=w[P(3u)+index_],value=0u;
 for(uint byte_=0u;byte_<4u;++byte_)value|=guest_byte(logical+byte_)<<(8u*byte_);
 w[P(2u)+index_]=value;
}
)PACK";
        auto pipeline=compute_pipeline(source);b.compute_pins.push_back(pipeline);
        const VkDescriptorSet set=graphics_descriptor_set(b,pipeline->descriptors,"graphics readback descriptor");
        VkDescriptorBufferInfo buffer{b.data_buffer,0,b.capacity};VkWriteDescriptorSet write{};write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.dstBinding=0;write.descriptorCount=1;write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&buffer;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);graphics_barrier(b);
        vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->pipeline);vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->layout,0,1,&set,0,nullptr);
        const unsigned base=params/4;vkCmdPushConstants(b.command,pipeline->layout,VK_SHADER_STAGE_COMPUTE_BIT,0,4,&base);const auto groups=(words.size()+63)/64,gx=std::min<std::size_t>(groups,graphics_properties.limits.maxComputeWorkGroupCount[0]),gy=(groups+gx-1)/gx;
        if(gy>graphics_properties.limits.maxComputeWorkGroupCount[1])throw std::runtime_error("RSX readback dispatch exceeds device limit");
        vkCmdDispatch(b.command,gx,gy,1);if(download)++graphics_readback_dispatches;else ++graphics_rebind_packs;
        graphics_barrier(b);if(download){VkBufferCopy result{output,output,words.size()*4};vkCmdCopyBuffer(b.command,b.data_buffer,b.buffer,1,&result);graphics_barrier(b);b.download_bytes+=words.size()*4;}b.buffer_pins.push_back(word_buffer);b.pins.push_back(image);return output;
    }
'''
