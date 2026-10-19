"""Latched NV4097 report predicates without a CPU query/readback round trip."""
MEMBERS = r'''
    struct ConditionalState {Label report;std::shared_ptr<NativeVulkan::GraphicsBuffer> buffer;unsigned offset;};
    std::optional<ConditionalState> conditional;
    struct PredicateArena {std::shared_ptr<NativeVulkan::GraphicsBuffer> buffer;unsigned used;};
    std::vector<PredicateArena> predicate_arenas;
    static unsigned report_value(const Label& report,std::uint64_t count){
        const unsigned value=unsigned(count);
        return report.value==1?value:report.value==5?((value || !report.active || !report.stats)?0u:0xffffu):((report.active && report.stats)?0xffffffffu:0u);
    }
    bool resolve_conditional(){
        if(!conditional)return state.rsx.render_disabled;
        // Used only at explicit ownership/capture or exceptional validation
        // boundaries. The ordinary graphics path never waits for this result.
        flush(false,nullptr,0,"conditional_resolve");
        const auto& report=conditional->report;auto count=report.base;
        for(const auto& q:report.queries){if(q.values->size()<q.first+q.count)throw std::runtime_error("RSX conditional result prefix incomplete");
            for(unsigned i=0;i<q.count;++i)count+=q.values->at(q.first+i);}
        state.rsx.render_disabled=report_value(report,count)==0;conditional.reset();return state.rsx.render_disabled;
    }
    bool try_conditional(std::uint64_t address){
        if(!vk.graphics_conditional_rendering())return false;
        const auto* enabled=std::getenv("ANYPS3_RSX_GPU_CONDITIONAL");
        if(enabled && std::strcmp(enabled,"0") && std::strcmp(enabled,"1"))throw std::runtime_error("invalid ANYPS3_RSX_GPU_CONDITIONAL");
        if(enabled && !std::strcmp(enabled,"0"))return false;
        GuestMemory::ObserverPause pause(state.memory);const auto mapping=state.memory.range(address,4,false);
        if(mapping.parts.size()!=1)return false;
        const auto* value=mapping.parts.front().data;const auto at=reinterpret_cast<std::uintptr_t>(value);
        for(const auto& surface:surfaces)if(surface.overlaps(value,4))return false;
        const Label* newest=nullptr;
        each_label([&](const Label& label){const auto p=reinterpret_cast<std::uintptr_t>(label.identity());if(at<p+label.size && p<at+4)newest=&label;});
        if(!newest || !newest->report || newest->kind || newest->size!=16 || value!=static_cast<const std::uint8_t*>(newest->identity())+8)return false;
        newest->mapping.check_epoch();Label report=*newest;
        unsigned base=unsigned(report.base);std::vector<NativeVulkan::ConditionalQuery> queries;
        for(const auto& q:report.queries){
            if(q.values->size()>=q.first+q.count){for(unsigned i=0;i<q.count;++i)base+=unsigned(q.values->at(q.first+i));continue;}
            NativeVulkan::GraphicsBatch* source=nullptr;
            if(q.values==query_results)source=batch.get();
            else for(const auto& flight:flights)if(q.values==flight.results){source=flight.batch.get();break;}
            if(!source || q.first+q.count>source->visibility_count)throw std::runtime_error("RSX conditional query producer missing");
            if(!queries.empty() && queries.back().pool==source->occlusion && queries.back().first+queries.back().count==q.first)queries.back().count+=q.count;
            else queries.push_back({source->occlusion,q.first,q.count});
        }
        conditional.reset();
        // Already known values and non-count statistics need no GPU predicate.
        if(queries.empty() || (report.value>1 && (report.value!=5 || !report.active || !report.stats))){state.rsx.render_disabled=report_value(report,base)==0;return true;}
        std::uint64_t count=0;for(const auto& q:queries)count+=q.count;
        if(count*8+65536>batch->capacity-batch->used){
            // Do not move/reset source query pools before their copy is
            // recorded. The established synchronous path handles this limit.
            return false;
        }
        PredicateArena* arena=nullptr;
        for(auto& entry:predicate_arenas)if(entry.used+4<=entry.buffer->size){arena=&entry;break;}
        if(!arena)for(auto& entry:predicate_arenas)if(entry.buffer.use_count()==1){entry.used=0;arena=&entry;break;}
        if(!arena){
            if(predicate_arenas.size()>=4)return false;
            predicate_arenas.push_back({vk.graphics_predicate_buffer(),0});arena=&predicate_arenas.back();
        }
        const auto offset=arena->used;arena->used+=4;
        vk.graphics_predicate(*batch,arena->buffer,offset,queries,base,report.value,report.active,report.stats);
        conditional.emplace(ConditionalState{std::move(report),arena->buffer,offset});state.rsx.render_disabled=false;
        if(fallback_stats().enabled)++fallback_stats().paths["conditional_gpu"];
        return true;
    }
'''
