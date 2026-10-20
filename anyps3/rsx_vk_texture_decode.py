"""Raw guest texture bytes decoded on device before sampled-image transfer.

The codec is shared with RTT reinterpretation; filtering/remap remain separate.
"""
from .rsx_graphics_raw_texture import GLSL as RAW_GLSL
MEMBERS = r'''
    std::uint64_t graphics_texture_decodes=0,graphics_texture_decode_bytes=0;
    std::uint64_t graphics_decode_texture(GraphicsBatch& b,const std::vector<unsigned>& data,std::uint64_t output_bytes){
        GraphicsSite site(SiteDecode);
        const auto input_bytes=data.size()*4;
        const auto at=graphics_upload(b,data.data(),input_bytes),output=graphics_space(b,output_bytes);
        // Descriptor offsets are relative to at; outputs follow the input in
        // the same device arena. Source and destination cannot overlap.
        auto* header=static_cast<unsigned*>(b.map)+at/4;header[7]=unsigned((output-at)/4);
        static const std::string source=R"DECODE(#version 450
layout(local_size_x=64) in;
layout(std430,binding=0) buffer Data {uint w[];};
layout(push_constant) uniform Base {uint entry_;};
uint raw_byte(uint unit,uint at){uint p=4u*w[2]+at;return (w[p/4u]>>(8u*(p%4u)))&255u;}
uint raw_format(uint unit){return w[0];}
)DECODE" + std::string(R"DECODE(''' + RAW_GLSL + r''')DECODE") + R"DECODE(
vec4 pixel(uint index_,uint e){uint width=w[e+1u],height=w[e+2u],depth=w[e+3u];
 uint x=index_%width,y=(index_/width)%height,z=index_/(width*height);
 if(z>=depth)return vec4(0);
 return raw_texel(0u,entry_%w[2065],entry_/w[2065],int(x),int(y),int(z));
}
uint packed(vec4 c){
 if(w[6]==1u)return uint(floor(c.x*16777215.0+0.5));
 if(w[6]==2u)return uint(floor(c.x*65535.0+0.5));
 if(w[5]==4u)return packUnorm4x8(c);
 if(w[5]==2u)return (uint(floor(c.r*31.0+0.5))<<11u)|(uint(floor(c.g*63.0+0.5))<<5u)|uint(floor(c.b*31.0+0.5));
 return 0u;
}
void main(){uint e=w[3]+entry_*5u,n=w[e+1u]*w[e+2u]*w[e+3u];
 uint index_=gl_GlobalInvocationID.x+gl_WorkGroupID.y*gl_NumWorkGroups.x*64u;
 uint bpp=w[5],count=bpp==2u?(n+1u)/2u:n;if(index_>=count)return;
 uint output_=w[7]+w[e]/4u;
 if(bpp==2u){w[output_+index_]=packed(pixel(2u*index_,e))|(packed(pixel(2u*index_+1u,e))<<16u);return;}
 precise vec4 c=pixel(index_,e);
 if(bpp==4u){w[output_+index_]=packed(c);return;}
 if(bpp==8u){w[output_+index_*2u]=packHalf2x16(c.xy);w[output_+index_*2u+1u]=packHalf2x16(c.zw);return;}
 for(uint i=0u;i<4u;++i)w[output_+index_*4u+i]=floatBitsToUint(c[i]);
}
)DECODE";
        auto pipeline=compute_pipeline(source);b.compute_pins.push_back(pipeline);
        const VkDescriptorSet set=graphics_descriptor_set(b,pipeline->descriptors,"texture decode descriptor");
        VkDescriptorBufferInfo buffer{b.data_buffer,at,output+output_bytes-at};
        if(buffer.range>graphics_properties.limits.maxStorageBufferRange)throw std::runtime_error("RSX texture decode exceeds storage range");
        VkWriteDescriptorSet write{};write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.descriptorCount=1;write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&buffer;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);graphics_barrier(b);VkBufferCopy copy{at,at,input_bytes};vkCmdCopyBuffer(b.command,b.buffer,b.data_buffer,1,&copy);graphics_barrier(b);
        vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->pipeline);vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->layout,0,1,&set,0,nullptr);
        for(unsigned level=0;level<data[1];++level){const auto e=data[3]+level*5;const auto n=std::uint64_t(data[e+1])*data[e+2]*data[e+3];
            const auto count=data[5]==2?(n+1)/2:n,groups=(count+63)/64,gx=std::min<std::uint64_t>(groups,graphics_properties.limits.maxComputeWorkGroupCount[0]),gy=(groups+gx-1)/gx;
            if(gy>graphics_properties.limits.maxComputeWorkGroupCount[1])throw std::runtime_error("RSX texture decode exceeds dispatch limit");
            vkCmdPushConstants(b.command,pipeline->layout,VK_SHADER_STAGE_COMPUTE_BIT,0,4,&level);vkCmdDispatch(b.command,gx,gy,1);++graphics_texture_decodes;
        }
        graphics_texture_decode_bytes+=input_bytes;graphics_barrier(b);return output;
    }
'''
