"""Vulkan compute transport; enabled only by ANYPS3_RSX_VK.

One packed storage buffer is uploaded and synchronously downloaded. Pipelines
are cached by the complete generated source, never by a guest address/hash alone.
"""
from .rsx_vk_resident import MEMBERS as RESIDENT_MEMBERS
from .rsx_vk_cache import MEMBERS as CACHE_MEMBERS

HEADERS = r'''
#ifdef ANYPS3_RSX_VK
#include <glslang/Include/glslang_c_interface.h>
#include <glslang/Public/resource_limits_c.h>
#include <glslang/build_info.h>
#include <filesystem>
#include <fstream>
#include <unistd.h>
#include <memory>
#include <map>
#include <mutex>
#include <chrono>
#include <algorithm>
#include <functional>
#endif
'''
MEMBERS = CACHE_MEMBERS + r'''
#ifdef ANYPS3_RSX_VK
public:
    struct ComputePipeline {
        std::uint64_t last_use=0;
        VkDevice device;
        VkDescriptorSetLayout descriptors=VK_NULL_HANDLE;
        VkPipelineLayout layout=VK_NULL_HANDLE;
        VkPipeline pipeline=VK_NULL_HANDLE;
        ~ComputePipeline(){
            if(pipeline)vkDestroyPipeline(device,pipeline,nullptr);
            if(layout)vkDestroyPipelineLayout(device,layout,nullptr);
            if(descriptors)vkDestroyDescriptorSetLayout(device,descriptors,nullptr);
        }
    };
    private:
    std::map<std::string,std::shared_ptr<ComputePipeline>> compute_pipelines;
    std::recursive_mutex compute_mutex;
    unsigned resident_recordings=0;
    std::vector<unsigned> compile_glsl(const std::string& source,glslang_stage_t stage,bool optimize=false){
        const auto cache_key=spirv_key(source,stage,optimize);
        if(auto code=load_spirv(cache_key);!code.empty()){++compile_stats.spirv_disk_hits;return code;}
        ++compile_stats.spirv_disk_misses;
        const auto compile_start=std::chrono::steady_clock::now();++compile_stats.spirv_compiles;
        // glslang has process-wide state. Serialize initialization and compiler use.
        static std::mutex compiler_mutex;
        std::lock_guard<std::mutex> lock(compiler_mutex);
        static const bool initialized=glslang_initialize_process()!=0;
        if(!initialized)throw std::runtime_error("glslang initialization failed");
        glslang_input_t input{};
        input.language=GLSLANG_SOURCE_GLSL;input.stage=stage;
        const bool quad=source.find("subgroupQuadBroadcast")!=std::string::npos || source.find("GL_EXT_multiview")!=std::string::npos;
        input.client=GLSLANG_CLIENT_VULKAN;input.client_version=quad?GLSLANG_TARGET_VULKAN_1_1:GLSLANG_TARGET_VULKAN_1_0;
        input.target_language=GLSLANG_TARGET_SPV;input.target_language_version=quad?GLSLANG_TARGET_SPV_1_3:GLSLANG_TARGET_SPV_1_0;
        input.code=source.c_str();input.default_version=450;input.default_profile=GLSLANG_NO_PROFILE;
        input.messages=static_cast<glslang_messages_t>(GLSLANG_MSG_SPV_RULES_BIT|GLSLANG_MSG_VULKAN_RULES_BIT);
        input.resource=glslang_default_resource();
        using Shader=std::unique_ptr<glslang_shader_t,decltype(&glslang_shader_delete)>;
        using Program=std::unique_ptr<glslang_program_t,decltype(&glslang_program_delete)>;
        Shader shader(glslang_shader_create(&input),glslang_shader_delete);
        if(!shader)throw std::runtime_error("glslang_shader_create failed");
        if(!glslang_shader_preprocess(shader.get(),&input) || !glslang_shader_parse(shader.get(),&input))
            throw std::runtime_error(std::string("RSX Vulkan GLSL: ")+glslang_shader_get_info_log(shader.get()));
        Program program(glslang_program_create(),glslang_program_delete);
        if(!program)throw std::runtime_error("glslang_program_create failed");
        glslang_program_add_shader(program.get(),shader.get());
        if(!glslang_program_link(program.get(),input.messages))
            throw std::runtime_error(std::string("RSX Vulkan link: ")+glslang_program_get_info_log(program.get()));
        glslang_spv_options_t options{};options.disable_optimizer=!optimize;options.optimize_size=false;options.validate=true;
        glslang_program_SPIRV_generate_with_options(program.get(),stage,&options);
        const auto size=glslang_program_SPIRV_get_size(program.get());
        if(!size)throw std::runtime_error("RSX Vulkan produced empty SPIR-V");
        const auto* words=glslang_program_SPIRV_get_ptr(program.get());
        std::vector<unsigned> code(words,words+size);
        const auto compile_ns=elapsed_ns(compile_start);compile_stats.spirv_ns+=compile_ns;
        if(stage==GLSLANG_STAGE_FRAGMENT && std::getenv("ANYPS3_RSX_SHADER_METRICS"))std::fprintf(stderr,"RSX FS COMPILE glsl_bytes=%zu spirv_words=%zu glslang_ms=%.3f\n",source.size(),code.size(),compile_ns*1.e-6);
        save_spirv(cache_key,code);return code;
    }
    std::shared_ptr<ComputePipeline> compute_pipeline(const std::string& source){
        auto found=compute_pipelines.find(source);
        if(found!=compute_pipelines.end()){found->second->last_use=++cache_clock;++compile_stats.compute_hits;compile_stats_print();return found->second;}
        ++compile_stats.compute_misses;
        const auto spirv=compile_glsl(source,GLSLANG_STAGE_COMPUTE);
        if(compute_pipelines.size()>=compute_pipeline_limit){compute_pipelines.erase(lru_entry(compute_pipelines));++compile_stats.compute_evictions;}
        auto p=std::make_shared<ComputePipeline>();p->last_use=++cache_clock;p->device=device;
        VkDescriptorSetLayoutBinding binding{};binding.binding=0;binding.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        binding.descriptorCount=1;binding.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT;
        VkDescriptorSetLayoutCreateInfo di{};di.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        di.bindingCount=1;di.pBindings=&binding;
        check(vkCreateDescriptorSetLayout(device,&di,nullptr,&p->descriptors),"vkCreateDescriptorSetLayout");
        VkPipelineLayoutCreateInfo li{};li.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;li.setLayoutCount=1;li.pSetLayouts=&p->descriptors;
        VkPushConstantRange push{VK_SHADER_STAGE_COMPUTE_BIT,0,4};li.pushConstantRangeCount=1;li.pPushConstantRanges=&push;
        check(vkCreatePipelineLayout(device,&li,nullptr,&p->layout),"vkCreatePipelineLayout");
        VkShaderModuleCreateInfo mi{};mi.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;mi.codeSize=spirv.size()*4;
        mi.pCode=spirv.data();VkShaderModule module=VK_NULL_HANDLE;
        const auto module_start=std::chrono::steady_clock::now();
        check(vkCreateShaderModule(device,&mi,nullptr,&module),"vkCreateShaderModule");
        ++compile_stats.shader_modules;compile_stats.module_ns+=elapsed_ns(module_start);
        VkComputePipelineCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;ci.layout=p->layout;
        ci.stage.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;ci.stage.stage=VK_SHADER_STAGE_COMPUTE_BIT;
        ci.stage.module=module;ci.stage.pName="main";
        const auto pipeline_start=std::chrono::steady_clock::now();
        const auto result=vkCreateComputePipelines(device,pipeline_cache,1,&ci,nullptr,&p->pipeline);
        ++compile_stats.compute_pipelines;compile_stats.pipeline_ns+=elapsed_ns(pipeline_start);pipeline_cache_created();
        vkDestroyShaderModule(device,module,nullptr);check(result,"vkCreateComputePipelines");
        save_pipeline_cache();compile_stats_print();
        return compute_pipelines.emplace(source,std::move(p)).first->second;
    }
public:
    std::uint64_t compute_dispatches=0;
    std::size_t compute_cache_size()const{return compute_pipelines.size();}
    bool compute_fits(std::size_t bytes,unsigned groups)const{
        VkPhysicalDeviceProperties properties{};vkGetPhysicalDeviceProperties(physical,&properties);
        return groups && groups<=properties.limits.maxComputeWorkGroupCount[0] && bytes && bytes<=properties.limits.maxStorageBufferRange;
    }
    // Each workgroup has 64 invocations. No guest memory may be committed until
    // this returns; errors are explicit and are never treated as a CPU fallback.
    void compute(const std::string& source,std::vector<std::uint32_t>& words,unsigned groups){
        std::lock_guard<std::recursive_mutex> lock(compute_mutex);
        VkPhysicalDeviceProperties properties{};vkGetPhysicalDeviceProperties(physical,&properties);
        const auto bytes=words.size()*sizeof(std::uint32_t);
        if(!groups || groups>properties.limits.maxComputeWorkGroupCount[0] || !bytes || bytes>properties.limits.maxStorageBufferRange)
            throw std::runtime_error("RSX Vulkan compute buffer/dispatch exceeds device limits");
        auto pin=compute_pipeline(source);auto& pipeline=*pin;
        Work work{device,pool};
        struct Descriptors { VkDevice device;VkDescriptorPool pool=VK_NULL_HANDLE;
            ~Descriptors(){if(pool){vkDeviceWaitIdle(device);vkDestroyDescriptorPool(device,pool,nullptr);}} } descriptors{device};
        VkBufferCreateInfo bi{};bi.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;bi.size=bytes;
        bi.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;bi.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
        check(vkCreateBuffer(device,&bi,nullptr,&work.buffer),"vkCreateBuffer(compute)");
        VkMemoryRequirements req{};vkGetBufferMemoryRequirements(device,work.buffer,&req);
        VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=req.size;
        ai.memoryTypeIndex=host_read_memory_type(req.memoryTypeBits);
        const bool coherent=memory.memoryTypes[ai.memoryTypeIndex].propertyFlags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        check(vkAllocateMemory(device,&ai,nullptr,&work.staging),"vkAllocateMemory(compute)");
        check(vkBindBufferMemory(device,work.buffer,work.staging,0),"vkBindBufferMemory(compute)");
        check(vkMapMemory(device,work.staging,0,VK_WHOLE_SIZE,0,&work.mapped),"vkMapMemory(compute)");
        std::memcpy(work.mapped,words.data(),bytes);
        VkMappedMemoryRange range{};range.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;range.memory=work.staging;range.size=VK_WHOLE_SIZE;
        if(!coherent)check(vkFlushMappedMemoryRanges(device,1,&range),"vkFlushMappedMemoryRanges(compute)");
        VkDescriptorPoolSize pool_size{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1};
        VkDescriptorPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;pi.maxSets=1;pi.poolSizeCount=1;pi.pPoolSizes=&pool_size;
        check(vkCreateDescriptorPool(device,&pi,nullptr,&descriptors.pool),"vkCreateDescriptorPool");
        VkDescriptorSetAllocateInfo si{};si.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;si.descriptorPool=descriptors.pool;
        si.descriptorSetCount=1;si.pSetLayouts=&pipeline.descriptors;VkDescriptorSet set=VK_NULL_HANDLE;
        check(vkAllocateDescriptorSets(device,&si,&set),"vkAllocateDescriptorSets");
        VkDescriptorBufferInfo buffer{work.buffer,0,bytes};VkWriteDescriptorSet write{};
        write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.descriptorCount=1;
        write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&buffer;
        vkUpdateDescriptorSets(device,1,&write,0,nullptr);
        VkCommandBufferAllocateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;ci.commandPool=pool;
        ci.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ci.commandBufferCount=1;
        check(vkAllocateCommandBuffers(device,&ci,&work.command),"vkAllocateCommandBuffers(compute)");
        VkCommandBufferBeginInfo begin{};begin.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;begin.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        check(vkBeginCommandBuffer(work.command,&begin),"vkBeginCommandBuffer(compute)");
        VkBufferMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
        barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;barrier.buffer=work.buffer;barrier.size=VK_WHOLE_SIZE;
        barrier.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT;
        vkCmdPipelineBarrier(work.command,VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,0,nullptr,1,&barrier,0,nullptr);
        vkCmdBindPipeline(work.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline.pipeline);
        vkCmdBindDescriptorSets(work.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline.layout,0,1,&set,0,nullptr);
        vkCmdDispatch(work.command,groups,1,1);
        barrier.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
        vkCmdPipelineBarrier(work.command,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,0,nullptr,1,&barrier,0,nullptr);
        check(vkEndCommandBuffer(work.command),"vkEndCommandBuffer(compute)");
        VkFenceCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        check(vkCreateFence(device,&fi,nullptr,&work.fence),"vkCreateFence(compute)");
        VkSubmitInfo submit{};submit.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;submit.commandBufferCount=1;submit.pCommandBuffers=&work.command;
        check(vkQueueSubmit(queue,1,&submit,work.fence),"vkQueueSubmit(compute)");
        check(vkWaitForFences(device,1,&work.fence,VK_TRUE,UINT64_MAX),"vkWaitForFences(compute)");
        if(!coherent)check(vkInvalidateMappedMemoryRanges(device,1,&range),"vkInvalidateMappedMemoryRanges(compute)");
        std::memcpy(words.data(),work.mapped,bytes);++compute_dispatches;
    }
private:
#endif
'''

MEMBERS += '\n#ifdef ANYPS3_RSX_VK\n' + RESIDENT_MEMBERS + '\n#endif\n'
