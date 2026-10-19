"""Device-local compute arena, explicitly scoped batches (no guest-memory hooks).

A batch owns its command stream; all API calls use NativeVulkan's compute mutex.
Offsets are words. Uploads/downloads are explicit; dispatch never waits. The
caller must keep the arena alive and finish before exposing dirty guest bytes.
"""
MEMBERS = r'''
public:
    struct ResidentArena {
        NativeVulkan* owner;
        VkBuffer gpu=VK_NULL_HANDLE,upload=VK_NULL_HANDLE,download=VK_NULL_HANDLE;
        VkDeviceMemory gpu_memory=VK_NULL_HANDLE,upload_memory=VK_NULL_HANDLE,download_memory=VK_NULL_HANDLE;
        void* upload_map=nullptr;void* download_map=nullptr;
        bool upload_coherent=false,download_coherent=false,recording=false,submitted=false,poisoned=false,pinned=false;
        VkDescriptorPool descriptors=VK_NULL_HANDLE;VkDescriptorSet set=VK_NULL_HANDLE;
        VkCommandBuffer command=VK_NULL_HANDLE;VkFence fence=VK_NULL_HANDLE;
        unsigned capacity=0,used=0,draws=0;
        std::vector<std::pair<unsigned,unsigned>> upload_slots,downloads,free_spans;
        std::map<unsigned,unsigned> allocations;
        VkQueryPool timestamps=VK_NULL_HANDLE;unsigned timestamp_bits=0,query_count=0;float timestamp_period=0;
        std::vector<unsigned> query_phases;
        double upload_gpu_ms=0,dispatch_gpu_ms=0,readback_gpu_ms=0;
        std::uint64_t upload_bytes=0,download_bytes=0,submissions=0,dispatches=0;
        double upload_cpu_ms=0,record_cpu_ms=0,wait_cpu_ms=0,readback_cpu_ms=0,pipeline_cpu_ms=0;
        std::vector<std::shared_ptr<ComputePipeline>> pipeline_pins;
        explicit ResidentArena(NativeVulkan* o):owner(o){}
        ~ResidentArena(){
            std::lock_guard<std::recursive_mutex> lock(owner->compute_mutex);
            const auto d=owner->device;
            if(submitted)vkWaitForFences(d,1,&fence,VK_TRUE,UINT64_MAX);
            if(timestamps)vkDestroyQueryPool(d,timestamps,nullptr);
            if(pinned)--owner->resident_recordings;
            if(command)vkFreeCommandBuffers(d,owner->pool,1,&command);
            if(fence)vkDestroyFence(d,fence,nullptr);
            if(descriptors)vkDestroyDescriptorPool(d,descriptors,nullptr);
            if(upload_map)vkUnmapMemory(d,upload_memory);
            if(download_map)vkUnmapMemory(d,download_memory);
            for(auto b:{gpu,upload,download})if(b)vkDestroyBuffer(d,b,nullptr);
            for(auto m:{gpu_memory,upload_memory,download_memory})if(m)vkFreeMemory(d,m,nullptr);
        }
        ResidentArena(const ResidentArena&)=delete;
        ResidentArena& operator=(const ResidentArena&)=delete;
    };
private:
    static double resident_ms(std::chrono::steady_clock::time_point t){
        return std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-t).count();
    }
    void resident_check(ResidentArena& a){
        if(a.owner!=this || a.poisoned)throw std::runtime_error("RSX resident arena owner/failure");
    }
    void resident_begin(ResidentArena& a){
        resident_check(a);
        if(a.recording)return;
        if(a.submitted)throw std::runtime_error("RSX resident submit still pending");
        VkCommandBufferBeginInfo b{};b.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;b.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        check(vkBeginCommandBuffer(a.command,&b),"resident begin");a.recording=true;a.pinned=true;++resident_recordings;a.downloads.clear();
        if(a.timestamps)vkCmdResetQueryPool(a.command,a.timestamps,0,4096);
    }
    void resident_timestamp_begin(ResidentArena& a,unsigned phase){
        if(!a.timestamps)return;
        if(a.query_count>4094)throw std::runtime_error("RSX resident timestamp capacity; finish batch first");
        a.query_phases.push_back(phase);vkCmdWriteTimestamp(a.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,a.timestamps,a.query_count++);
    }
    void resident_timestamp_end(ResidentArena& a){
        if(a.timestamps)vkCmdWriteTimestamp(a.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,a.timestamps,a.query_count++);
    }
    void resident_barrier(ResidentArena& a){
        // Includes preceding FIFO draws, copies, and staging host writes.
        VkMemoryBarrier b{};b.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        b.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT|VK_ACCESS_TRANSFER_WRITE_BIT|VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_SHADER_READ_BIT;
        b.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT|VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT;
        vkCmdPipelineBarrier(a.command,VK_PIPELINE_STAGE_HOST_BIT|VK_PIPELINE_STAGE_TRANSFER_BIT|VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT|VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,1,&b,0,nullptr,0,nullptr);
    }
public:
    std::unique_ptr<ResidentArena> resident_arena(unsigned words){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);
        if(!compute_fits(std::uint64_t(words)*4,1))throw std::runtime_error("RSX resident arena exceeds storage limits");
        auto a=std::make_unique<ResidentArena>(this);a->capacity=words;
        VkPhysicalDeviceProperties properties{};vkGetPhysicalDeviceProperties(physical,&properties);
        unsigned families=0;vkGetPhysicalDeviceQueueFamilyProperties(physical,&families,nullptr);
        std::vector<VkQueueFamilyProperties> queues(families);vkGetPhysicalDeviceQueueFamilyProperties(physical,&families,queues.data());
        a->timestamp_bits=queues[family].timestampValidBits;a->timestamp_period=properties.limits.timestampPeriod;
        if(a->timestamp_bits && std::getenv("ANYPS3_RSX_VK_PROFILE")){VkQueryPoolCreateInfo qi{};qi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;qi.queryType=VK_QUERY_TYPE_TIMESTAMP;qi.queryCount=4096;
            check(vkCreateQueryPool(device,&qi,nullptr,&a->timestamps),"resident timestamp pool");}
        auto buffer=[&](VkBuffer& b,VkDeviceMemory& m,VkBufferUsageFlags usage,VkMemoryPropertyFlags flags,void** map,bool* coherent){
            VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=std::uint64_t(words)*4;
            bi.usage=usage;bi.sharingMode=VK_SHARING_MODE_EXCLUSIVE;check(vkCreateBuffer(device,&bi,nullptr,&b),"resident buffer");
            VkMemoryRequirements r{};vkGetBufferMemoryRequirements(device,b,&r);
            VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=r.size;ai.memoryTypeIndex=(flags==VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT && (usage&VK_BUFFER_USAGE_TRANSFER_DST_BIT))?host_read_memory_type(r.memoryTypeBits):memory_type(r.memoryTypeBits,flags);
            check(vkAllocateMemory(device,&ai,nullptr,&m),"resident memory");check(vkBindBufferMemory(device,b,m,0),"resident bind");
            if(map){check(vkMapMemory(device,m,0,VK_WHOLE_SIZE,0,map),"resident map");
                *coherent=memory.memoryTypes[ai.memoryTypeIndex].propertyFlags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;}
        };
        buffer(a->gpu,a->gpu_memory,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,
            VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT,nullptr,nullptr);
        buffer(a->upload,a->upload_memory,VK_BUFFER_USAGE_TRANSFER_SRC_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT,&a->upload_map,&a->upload_coherent);
        buffer(a->download,a->download_memory,VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT,&a->download_map,&a->download_coherent);
        VkCommandBufferAllocateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;ci.commandPool=pool;ci.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ci.commandBufferCount=1;
        check(vkAllocateCommandBuffers(device,&ci,&a->command),"resident command");
        VkFenceCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;check(vkCreateFence(device,&fi,nullptr,&a->fence),"resident fence");
        VkDescriptorPoolSize size{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1};VkDescriptorPoolCreateInfo pi{};
        pi.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;pi.maxSets=1;pi.poolSizeCount=1;pi.pPoolSizes=&size;
        check(vkCreateDescriptorPool(device,&pi,nullptr,&a->descriptors),"resident descriptor pool");
        // All compute layouts have the same storage binding and push range.
        const auto pipeline=compute_pipeline("#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};void main(){w[0]=w[0];}");
        const auto& p=*pipeline;VkDescriptorSetAllocateInfo si{};si.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;si.descriptorPool=a->descriptors;si.descriptorSetCount=1;si.pSetLayouts=&p.descriptors;
        check(vkAllocateDescriptorSets(device,&si,&a->set),"resident descriptor set");
        VkDescriptorBufferInfo info{a->gpu,0,std::uint64_t(words)*4};VkWriteDescriptorSet write{};
        write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=a->set;write.descriptorCount=1;write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&info;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);return a;
    }
    unsigned resident_allocate(ResidentArena& a,unsigned words){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        if(!words)throw std::runtime_error("RSX resident zero allocation");
        for(auto it=a.free_spans.begin();it!=a.free_spans.end();++it)if(it->second>=words){
            const auto offset=it->first;it->first+=words;it->second-=words;if(!it->second)a.free_spans.erase(it);
            a.allocations[offset]=words;return offset;
        }
        if(words>a.capacity-a.used)throw std::runtime_error("RSX resident arena exhausted");
        const auto offset=a.used;a.used+=words;a.allocations[offset]=words;return offset;
    }
    void resident_free(ResidentArena& a,unsigned offset){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        if(a.recording || a.submitted)throw std::runtime_error("RSX resident free while recording/pending");
        auto it=a.allocations.find(offset);if(it==a.allocations.end())throw std::runtime_error("RSX resident invalid/double free");
        a.free_spans.push_back(*it);a.allocations.erase(it);std::sort(a.free_spans.begin(),a.free_spans.end());
        for(unsigned i=1;i<a.free_spans.size();)if(a.free_spans[i-1].first+a.free_spans[i-1].second==a.free_spans[i].first){
            a.free_spans[i-1].second+=a.free_spans[i].second;a.free_spans.erase(a.free_spans.begin()+i);
        }else ++i;
    }
    // A staging slot is immutable until finish. A slot must not be uploaded
    // twice in a batch (later host stores would overwrite earlier queued data).
    void resident_upload(ResidentArena& a,unsigned destination,const std::vector<unsigned>& words,unsigned staging){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        const auto t=std::chrono::steady_clock::now();
        if(words.empty() || destination>a.used || words.size()>a.used-destination || staging>a.capacity || words.size()>a.capacity-staging)
            throw std::runtime_error("RSX resident upload bounds");
        for(const auto& slot:a.upload_slots)if(staging<slot.first+slot.second && slot.first<staging+words.size())
            throw std::runtime_error("RSX resident staging slot reused before finish");
        a.upload_slots.push_back({staging,static_cast<unsigned>(words.size())});
        resident_begin(a);std::memcpy(static_cast<unsigned*>(a.upload_map)+staging,words.data(),words.size()*4);
        resident_barrier(a);VkBufferCopy copy{std::uint64_t(staging)*4,std::uint64_t(destination)*4,words.size()*4};
        resident_timestamp_begin(a,0);vkCmdCopyBuffer(a.command,a.upload,a.gpu,1,&copy);resident_timestamp_end(a);a.upload_bytes+=words.size()*4;a.upload_cpu_ms+=resident_ms(t);
    }
    void resident_dispatch(ResidentArena& a,const std::string& source,unsigned base,unsigned groups){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        if(base>=a.used || !compute_fits(std::uint64_t(a.capacity)*4,groups))throw std::runtime_error("RSX resident dispatch bounds");
        auto t=std::chrono::steady_clock::now();if(a.pipeline_pins.size()>=128)resident_finish(a);
        auto pipeline=compute_pipeline(source);auto& p=*pipeline;a.pipeline_pins.push_back(pipeline);a.pipeline_cpu_ms+=resident_ms(t);t=std::chrono::steady_clock::now();
        resident_begin(a);resident_barrier(a);
        vkCmdBindPipeline(a.command,VK_PIPELINE_BIND_POINT_COMPUTE,p.pipeline);
        vkCmdBindDescriptorSets(a.command,VK_PIPELINE_BIND_POINT_COMPUTE,p.layout,0,1,&a.set,0,nullptr);
        vkCmdPushConstants(a.command,p.layout,VK_SHADER_STAGE_COMPUTE_BIT,0,4,&base);
        resident_timestamp_begin(a,1);vkCmdDispatch(a.command,groups,1,1);resident_timestamp_end(a);++a.dispatches;++a.draws;++compute_dispatches;a.record_cpu_ms+=resident_ms(t);
    }
    // Copy resident surfaces/textures without round-tripping through the CPU.
    void resident_copy(ResidentArena& a,unsigned from,unsigned to,unsigned words){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        if(!words || from>a.used || words>a.used-from || to>a.used || words>a.used-to || (from<to+words && to<from+words))
            throw std::runtime_error("RSX resident copy bounds/overlap");
        resident_begin(a);resident_barrier(a);VkBufferCopy copy{std::uint64_t(from)*4,std::uint64_t(to)*4,std::uint64_t(words)*4};
        resident_timestamp_begin(a,0);vkCmdCopyBuffer(a.command,a.gpu,a.gpu,1,&copy);resident_timestamp_end(a);
    }
    void resident_download(ResidentArena& a,unsigned offset,unsigned words){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        if(!words || offset>a.used || words>a.used-offset)throw std::runtime_error("RSX resident readback bounds");
        resident_begin(a);resident_barrier(a);VkBufferCopy copy{std::uint64_t(offset)*4,std::uint64_t(offset)*4,std::uint64_t(words)*4};
        a.downloads.push_back({offset,words});
        resident_timestamp_begin(a,2);vkCmdCopyBuffer(a.command,a.gpu,a.download,1,&copy);resident_timestamp_end(a);a.download_bytes+=std::uint64_t(words)*4;
    }
    void resident_finish(ResidentArena& a){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);if(!a.recording)return;
        try{
            if(!a.upload_coherent){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=a.upload_memory;r.size=VK_WHOLE_SIZE;
                check(vkFlushMappedMemoryRanges(device,1,&r),"resident flush");}
            VkMemoryBarrier b{};b.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;b.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;b.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
            vkCmdPipelineBarrier(a.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&b,0,nullptr,0,nullptr);
            check(vkEndCommandBuffer(a.command),"resident end");a.recording=false;
            VkSubmitInfo si{};si.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;si.commandBufferCount=1;si.pCommandBuffers=&a.command;
            check(vkQueueSubmit(queue,1,&si,a.fence),"resident submit");a.submitted=true;++a.submissions;
            const auto t=std::chrono::steady_clock::now();check(vkWaitForFences(device,1,&a.fence,VK_TRUE,UINT64_MAX),"resident wait");a.wait_cpu_ms+=resident_ms(t);a.submitted=false;
            if(!a.download_coherent){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=a.download_memory;r.size=VK_WHOLE_SIZE;
                check(vkInvalidateMappedMemoryRanges(device,1,&r),"resident invalidate");}
            if(a.timestamps && a.query_count){
                std::vector<std::uint64_t> values(a.query_count);check(vkGetQueryPoolResults(device,a.timestamps,0,a.query_count,values.size()*8,values.data(),8,VK_QUERY_RESULT_64_BIT|VK_QUERY_RESULT_WAIT_BIT),"resident timestamps");
                const std::uint64_t mask=a.timestamp_bits>=64?UINT64_MAX:((UINT64_C(1)<<a.timestamp_bits)-1);
                for(unsigned i=0;i<a.query_phases.size();++i){const auto ticks=(values[2*i+1]-values[2*i])&mask;const double ms=double(ticks)*a.timestamp_period/1e6;
                    if(a.query_phases[i]==0)a.upload_gpu_ms+=ms;else if(a.query_phases[i]==1)a.dispatch_gpu_ms+=ms;else a.readback_gpu_ms+=ms;}
                a.query_count=0;a.query_phases.clear();
            }
            a.upload_slots.clear();
            check(vkResetFences(device,1,&a.fence),"resident reset fence");check(vkResetCommandBuffer(a.command,0),"resident reset command");a.draws=0;a.pipeline_pins.clear();a.pinned=false;--resident_recordings;
        }catch(...){a.poisoned=true;throw;}
    }
    std::vector<unsigned> resident_read(ResidentArena& a,unsigned offset,unsigned words){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);resident_check(a);
        if(a.recording || a.submitted || offset>a.used || words>a.used-offset)throw std::runtime_error("RSX resident read before finish/bounds");
        bool available=words==0;for(const auto& span:a.downloads)if(offset>=span.first && offset-span.first<=span.second && words<=span.second-(offset-span.first))available=true;
        if(!available)throw std::runtime_error("RSX resident read without download");
        const auto t=std::chrono::steady_clock::now();const auto* data=static_cast<const unsigned*>(a.download_map)+offset;
        std::vector<unsigned> out(data,data+words);a.readback_cpu_ms+=resident_ms(t);return out;
    }
private:
'''
