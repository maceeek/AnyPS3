"""EXT conditional rendering with immutable GPU predicates and exact query sums."""
MEMBERS = r'''
#ifdef ANYPS3_RSX_VK
public:
    VkPhysicalDeviceConditionalRenderingFeaturesEXT conditional_features{};
    PFN_vkCmdBeginConditionalRenderingEXT conditional_begin=nullptr;
    PFN_vkCmdEndConditionalRenderingEXT conditional_end=nullptr;
    bool graphics_conditional_rendering()const{return conditional_begin && conditional_end;}
    struct ConditionalQuery {VkQueryPool pool;unsigned first,count;};
    std::uint64_t conditional_predicates=0,conditional_draws=0;
    std::shared_ptr<GraphicsBuffer> graphics_predicate_buffer(){
        if(!graphics_conditional_rendering())throw std::runtime_error("RSX GPU conditional rendering unavailable");
        auto result=std::make_shared<GraphicsBuffer>(this,65536);
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=result->size;
        bi.usage=VK_BUFFER_USAGE_CONDITIONAL_RENDERING_BIT_EXT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        check(vkCreateBuffer(device,&bi,nullptr,&result->buffer),"conditional buffer");
        VkMemoryRequirements r{};vkGetBufferMemoryRequirements(device,result->buffer,&r);
        VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=memory_type(r.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        result->memory_bytes=r.size;check(vkAllocateMemory(device,&ai,nullptr,&result->memory),"conditional memory");
        check(vkBindBufferMemory(device,result->buffer,result->memory,0),"conditional bind");return result;
    }
    void graphics_predicate(GraphicsBatch& b,const std::shared_ptr<GraphicsBuffer>& output,unsigned offset,
            const std::vector<ConditionalQuery>& queries,unsigned base,unsigned type,bool active,bool stats){
        if(!graphics_conditional_rendering() || !output || offset%4 || std::uint64_t(offset)+4>output->size || type<1 || type>5)
            throw std::runtime_error("invalid RSX GPU predicate");
        std::uint64_t count=0;for(const auto& q:queries){if(!q.pool || !q.count || q.first>1024 || q.count>1024-q.first)throw std::runtime_error("invalid RSX predicate query span");count+=q.count;}
        if(count>65536 || count*8+65536>b.capacity-b.used)throw std::runtime_error("RSX predicate query capacity");
        graphics_begin(b);graphics_end_pass(b);const auto input=graphics_space(b,std::max<std::uint64_t>(8,count*8));
        std::uint64_t at=input;
        for(const auto& q:queries){
            // 64-bit copies preserve exact low32 wrap semantics even if a
            // hardware query count exceeds UINT_MAX. No host wait occurs.
            vkCmdCopyQueryPoolResults(b.command,q.pool,q.first,q.count,b.data_buffer,at,8,VK_QUERY_RESULT_64_BIT|VK_QUERY_RESULT_WAIT_BIT);at+=std::uint64_t(q.count)*8;
        }
        const auto result=graphics_space(b,4);
        const unsigned params[]={unsigned(count),unsigned(input/4),base,type,unsigned(active),unsigned(stats),unsigned(result/4)};
        const auto parameters=graphics_upload(b,params,sizeof(params));
        VkBufferCopy copy{parameters,parameters,sizeof(params)};vkCmdCopyBuffer(b.command,b.buffer,b.data_buffer,1,&copy);
        graphics_barrier(b);
        static const std::string shader=R"PRED(#version 450
layout(local_size_x=1) in;layout(std430,binding=0) buffer Data{uint w[];};layout(push_constant) uniform Base{uint base_;};
uint P(uint n){return w[base_+n];}
void main(){uint value=P(2u);for(uint i=0u;i<P(0u);++i)value+=w[P(1u)+i*2u];
 if(P(3u)>1u)value=P(3u)==5u?((value!=0u || P(4u)==0u || P(5u)==0u)?0u:65535u):((P(4u)!=0u && P(5u)!=0u)?0xffffffffu:0u);
 w[P(6u)]=value;}
)PRED";
        auto pipeline=compute_pipeline(shader);b.compute_pins.push_back(pipeline);
        const auto set=graphics_descriptor_set(b,pipeline->descriptors,"conditional descriptor");
        VkDescriptorBufferInfo info{b.data_buffer,0,b.capacity};VkWriteDescriptorSet write{};write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.descriptorCount=1;write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&info;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);
        vkCmdBindPipeline(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->pipeline);vkCmdBindDescriptorSets(b.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline->layout,0,1,&set,0,nullptr);
        const unsigned parameter_base=parameters/4;vkCmdPushConstants(b.command,pipeline->layout,VK_SHADER_STAGE_COMPUTE_BIT,0,4,&parameter_base);vkCmdDispatch(b.command,1,1,1);
        VkMemoryBarrier ready{};ready.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;ready.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;ready.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,1,&ready,0,nullptr,0,nullptr);
        copy={result,offset,4};vkCmdCopyBuffer(b.command,b.data_buffer,output->buffer,1,&copy);
        VkBufferMemoryBarrier visible{};visible.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;visible.srcQueueFamilyIndex=visible.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
        visible.buffer=output->buffer;visible.offset=offset;visible.size=4;visible.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;visible.dstAccessMask=VK_ACCESS_CONDITIONAL_RENDERING_READ_BIT_EXT;
        vkCmdPipelineBarrier(b.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_CONDITIONAL_RENDERING_BIT_EXT,0,0,nullptr,1,&visible,0,nullptr);
        b.buffer_pins.push_back(output);++conditional_predicates;
    }
    struct ConditionalScope {
        NativeVulkan& vk;GraphicsBatch& batch;bool active;
        ConditionalScope(NativeVulkan& v,GraphicsBatch& b,const std::shared_ptr<GraphicsBuffer>& buffer,unsigned offset):vk(v),batch(b),active(bool(buffer)){
            if(!active)return;
            if(!vk.graphics_conditional_rendering() || offset%4 || std::uint64_t(offset)+4>buffer->size)throw std::runtime_error("invalid RSX conditional draw");
            vk.graphics_begin(batch);vk.graphics_end_pass(batch);VkConditionalRenderingBeginInfoEXT info{};info.sType=VK_STRUCTURE_TYPE_CONDITIONAL_RENDERING_BEGIN_INFO_EXT;info.buffer=buffer->buffer;info.offset=offset;
            vk.conditional_begin(batch.command,&info);batch.buffer_pins.push_back(buffer);++vk.conditional_draws;
        }
        ~ConditionalScope(){if(active){vk.graphics_end_pass(batch);vk.conditional_end(batch.command);}}
    };
#endif
'''
