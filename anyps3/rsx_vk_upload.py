"""Bit-preserving inverse surface permutation and endian conversion on device."""
MEMBERS = r'''
    std::uint64_t graphics_surface_uploads=0;
    void graphics_unpack_upload(GraphicsBatch& b,const std::shared_ptr<GraphicsImage>& image,
            unsigned format,bool depth,unsigned bpp,unsigned pitch,unsigned samples,
            const std::vector<unsigned>& words,std::shared_ptr<GraphicsBuffer>& word_buffer,const void* bytes,std::uint64_t device_input=UINT64_MAX){
        const auto size=words.size()*4,pixels=std::uint64_t(image->width)*image->height*samples;
        const bool stencil=depth && format==2;
        if(device_input!=UINT64_MAX && (device_input%4 || device_input>b.used || size>b.used-device_input))throw std::runtime_error("RSX device surface upload input bounds");
        const auto input=device_input==UINT64_MAX?graphics_upload(b,bytes,size):device_input,table=graphics_space(b,size),output=graphics_space(b,size);
        const auto stencil_at=stencil?graphics_space(b,(pixels+3)&~UINT64_C(3)):0;
        if(!word_buffer)word_buffer=graphics_buffer(b,words.data(),size);
        graphics_barrier(b);VkBufferCopy copies[]={{input,input,size},{0,table,size}};
        if(device_input==UINT64_MAX)vkCmdCopyBuffer(b.command,b.buffer,b.data_buffer,1,copies);
        vkCmdCopyBuffer(b.command,word_buffer->buffer,b.data_buffer,1,copies+1);
        if(stencil)vkCmdFillBuffer(b.command,b.data_buffer,stencil_at,(pixels+3)&~UINT64_C(3),0);
        const unsigned parameters[]={unsigned(input/4),unsigned(table/4),unsigned(output/4),unsigned(stencil_at/4),unsigned(words.size()),image->width,image->height,samples,bpp,pitch,format,unsigned(depth),unsigned(image->format==VK_FORMAT_R32_UINT)};
        const auto params=graphics_upload(b,parameters,sizeof(parameters));
        VkBufferCopy params_copy{params,params,sizeof(parameters)};vkCmdCopyBuffer(b.command,b.buffer,b.data_buffer,1,&params_copy);
        static const std::string source=R"UPLOAD(#version 450
layout(local_size_x=64) in;
layout(std430,binding=0) buffer Data{uint w[];};
layout(push_constant) uniform Base{uint base_;};
uint P(uint n){return w[base_+n];}
uint reverse_(uint v){return (v>>24u)|((v>>8u)&0xff00u)|((v<<8u)&0xff0000u)|(v<<24u);}
void main(){uint i=gl_GlobalInvocationID.x+gl_WorkGroupID.y*gl_NumWorkGroups.x*64u;if(i>=P(4u))return;
 uint logical=w[P(1u)+i],yy=logical/P(9u),xx=logical%P(9u),nx=P(7u)>1u?2u:1u,ny=P(7u)==4u?2u:1u;
 uint x=xx/P(8u),layer=(yy%ny)*nx+x%nx,pixel=(layer*P(6u)+yy/ny)*P(5u)+x/nx;
 uint at=P(2u)+pixel*(P(8u)/4u)+(xx%P(8u))/4u,v=w[P(0u)+i],value;
 if(P(11u)!=0u && P(10u)==2u){value=reverse_(v)>>8u;atomicOr(w[P(3u)+pixel/4u],(v>>24u)<<(8u*(pixel%4u)));}
 else if(P(12u)!=0u)value=reverse_(v);
 else if(P(11u)==0u && P(10u)==8u)value=(v>>8u)|(v<<24u);
 else if(P(10u)==11u)value=((v>>8u)&0x00ff00ffu)|((v<<8u)&0xff00ff00u);
 else value=reverse_(v);
 w[at]=value;
}
)UPLOAD";
        auto pipeline=compute_pipeline(source);b.compute_pins.push_back(pipeline);
        const auto set=graphics_descriptor_set(b,pipeline->descriptors,"surface upload descriptor");
        VkDescriptorBufferInfo buffer{b.data_buffer,0,b.capacity};VkWriteDescriptorSet write{};write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.descriptorCount=1;write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&buffer;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);graphics_barrier(b);
        vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->pipeline);vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->layout,0,1,&set,0,nullptr);
        const unsigned base=params/4;vkCmdPushConstants(b.command,pipeline->layout,VK_SHADER_STAGE_COMPUTE_BIT,0,4,&base);
        const auto groups=(words.size()+63)/64,gx=std::min<std::size_t>(groups,graphics_properties.limits.maxComputeWorkGroupCount[0]),gy=(groups+gx-1)/gx;
        if(gy>graphics_properties.limits.maxComputeWorkGroupCount[1])throw std::runtime_error("RSX surface upload dispatch limit");
        vkCmdDispatch(b.command,gx,gy,1);graphics_barrier(b);
        VkBufferImageCopy copy{};copy.bufferOffset=output;copy.imageSubresource={depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,0,0,image->layers};copy.imageExtent={image->width,image->height,1};
        vkCmdCopyBufferToImage(b.command,b.data_buffer,image->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);
        if(stencil){copy.bufferOffset=stencil_at;copy.imageSubresource.aspectMask=VK_IMAGE_ASPECT_STENCIL_BIT;vkCmdCopyBufferToImage(b.command,b.data_buffer,image->image,VK_IMAGE_LAYOUT_GENERAL,1,&copy);}
        graphics_barrier(b);b.buffer_pins.push_back(word_buffer);b.pins.push_back(image);++graphics_surface_uploads;
    }
'''
