"""Cache-native texture upload, with all guest codecs resolved at upload time."""
MEMBERS = r'''
    // ANYPS3_RSX_TEXTURE_IMAGE_MEMO (default 1): the lookup below remembers, per identity slot, the inputs of a hit on a trusted
    // entry whose every candidate was trusted (no snapshot read), with raw_revision at that time. raw_revision moves at every
    // change of the raw cache that the lookup reads: insertion (index_raw), erase (erase_raw: also the only sampled_entries and
    // raw_positions changes, and every move of an entry inside raw_textures), replacement (store_raw) and invalidation
    // (dirty_raw); RawTexture fields are otherwise only written on entries not yet stored. With the same inputs and revision the
    // scan selects the same entry with the same trusted candidates, so only its CPU-dirty check (raw_dirty) runs again. 0 always scans.
    static bool texture_image_memo(){static const bool on=[]{const char* v=std::getenv("ANYPS3_RSX_TEXTURE_IMAGE_MEMO");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_TEXTURE_IMAGE_MEMO");}
        return !v || !std::strcmp(v,"1");}();return on;}
    struct ImageMemo {std::vector<unsigned> key;const void* identity=nullptr;bool planned=false;std::uint64_t origin_size=0,size=0,revision=0,id=0;std::size_t position=0;};
    std::array<ImageMemo,8> image_memo{};
    std::shared_ptr<NativeVulkan::GraphicsImage> texture_image(const Texture& supplied){
        HostPhase phase(HostStage::texture_upload);
        const auto* source=supplied.levels[0].data;const auto size=supplied.plan?supplied.plan->bytes:supplied.untiled?supplied.untiled->size():supplied.storage_size;
        std::uint64_t reuse=0;std::shared_ptr<RawTexture::DirtyInputs> dirty;
        std::shared_ptr<NativeVulkan::SampledImage> reusable_image;
        static thread_local std::vector<unsigned> key;
        {const auto& t=supplied;
        key.assign({t.format,t.dimension,unsigned(t.cube),unsigned(t.swizzled),t.mip_count,t.wrap_s,t.wrap_t,t.wrap_r,t.min,t.mag,t.features,t.signed_mask,t.shadow,unsigned(t.alpha_kill),float_bits(t.bias),float_bits(t.min_lod),float_bits(t.max_lod)});
        for(const auto& l:t.levels)key.insert(key.end(),{l.width,l.height,l.depth,l.pitch,l.backing_width,l.backing_height,l.border,unsigned(t.plan?l.offset:l.data-source)});
        // Synchronization can erase/move cache entries. Do it before taking
        // an entry reference, including for physically disjoint bytes that
        // share a protected host page with a resident surface.
        const void* identity=t.plan?static_cast<const void*>(t.plan.get()):t.untiled?t.storage:source;
        auto& memo=image_memo[(reinterpret_cast<std::uintptr_t>(identity)>>4)%image_memo.size()];
        if(texture_image_memo() && memo.revision==raw_revision && memo.identity==identity && memo.planned==bool(t.plan) && memo.origin_size==t.storage_size && memo.size==size && memo.key==key){
            if(memo.position>=raw_textures.size() || raw_textures[memo.position].id!=memo.id)throw std::runtime_error("RSX texture image memo outlived its raw entry");
            auto& entry=raw_textures[memo.position];
            if(!raw_dirty(entry) && entry.trusted){if(fallback_stats().enabled)++fallback_stats().paths["texture_hit"];++raw_hits;entry.last_use=++resource_clock;return entry.image;}
        }
        const auto signature=sampled_key(key,identity,size,t.storage_size);
        auto matches=[&](const RawTexture& e){return e.image && e.key==key && (t.plan?e.plan==t.plan:e.origin==(t.untiled?t.storage:source)) && e.origin_size==t.storage_size && e.size==size;};
        bool snapshot_read=false;RawTexture* first_match=nullptr;
        {const auto range=sampled_entries.equal_range(signature);
        for(auto it=range.first;it!=range.second;++it){auto& entry=raw_textures[raw_positions.at(it->second)];if(!matches(entry))continue;
            if(!first_match){first_match=&entry;}
            if(!entry.trusted){snapshot_read=true;break;}}}
        if(snapshot_read){first_match=nullptr;if(t.plan){for(const auto& run:t.plan->runs)state.memory.notify_host(run.data,run.size,false);}
            else state.memory.notify_host(source,size,false);}
        // Without a notification nothing ran since the scan above, so its first match is the entry a second scan would select.
        // A notification may synchronize and move entries: then the scan is repeated.
        if(!first_match){const auto range=sampled_entries.equal_range(signature);
            for(auto it=range.first;it!=range.second;++it){auto& entry=raw_textures[raw_positions.at(it->second)];if(matches(entry)){first_match=&entry;break;}}}
        if(first_match){auto& entry=*first_match;
            const bool invalid=raw_dirty(entry);
            bool same=!invalid && entry.trusted;
            if(!same && !invalid && t.plan){same=true;for(const auto& run:t.plan->runs){std::vector<std::uint8_t> bytes;
                const auto* current=run.data;if(state.memory.concurrent()){bytes.resize(run.size);read_texture_run(state,run,bytes.data());current=bytes.data();}
                if(std::memcmp(entry.snapshot.data()+run.offset,current,run.size)){same=false;break;}}}
            else if(!same && !invalid){std::vector<std::uint8_t> bytes;const auto* current=source;
                if(state.memory.concurrent() && !t.untiled){bytes.resize(size);pinned_host_read(state.memory,source,bytes.data(),size);current=bytes.data();}
                same=std::memcmp(entry.snapshot.data(),current,size)==0;}
            if(same){if(fallback_stats().enabled)++fallback_stats().paths["texture_hit"];++raw_hits;entry.last_use=++resource_clock;
                if(!snapshot_read && entry.trusted){memo.key=key;memo.identity=identity;memo.planned=bool(t.plan);memo.origin_size=t.storage_size;memo.size=size;
                    memo.revision=raw_revision;memo.id=entry.id;memo.position=std::size_t(&entry-raw_textures.data());}
                return entry.image;}
            if(entry.dirty_inputs){reuse=entry.id;dirty=entry.dirty_inputs;reusable_image=entry.image;}
            else erase_raw(&entry-raw_textures.data(),"snapshot_mismatch");
            }
        }
        // Cache hits do not copy level vectors or allocate a temporary key.
        Texture t=supplied;
        if(!dirty)dirty=dirty_inputs(t,source,size);
        if(fallback_stats().enabled){++fallback_stats().paths["texture_miss"];
            std::ostringstream detail;detail<<"draw="<<state.rsx.draw.draws<<" pc=0x"<<std::hex<<state.pc<<" format=0x"<<t.format<<std::dec<<" width="<<t.levels[0].width<<" height="<<t.levels[0].height<<" levels="<<t.levels.size()<<" gathered="<<bool(t.plan && t.plan->gathered);fallback_stats().detail("texture_cpu_upload",detail.str());}
        if(t.plan){for(const auto& run:t.plan->runs)access_trace().record("texture_upload",state,run.data,run.size,false,t.format,t.levels[0].width,t.levels[0].height);}
        else access_trace().record("texture_upload",state,source,size,false,t.format,t.levels[0].width,t.levels[0].height);
        struct UploadWatch {Session& owner;const Texture& texture;
            UploadWatch(Session& o,const Texture& t):owner(o),texture(t){if(t.plan && !owner.state.memory.concurrent())for(const auto& run:t.plan->runs)owner.watch_span(run.data,run.size,true);}
            ~UploadWatch(){if(texture.plan && !owner.state.memory.concurrent())for(const auto& run:texture.plan->runs)owner.watch_span(run.data,run.size,false);}
        } upload_watch(*this,t);
        materialize_texture(state,t);source=t.levels[0].data;
        // Use native UNORM/FP16 where the prefilter values are exactly those
        // represented by the image. Other codecs become canonical FP32 once.
        const bool signed_input=(t.features&1) && t.signed_mask && !t.shadow;
        VkFormat format=!signed_input && t.format==0x85?VK_FORMAT_R8G8B8A8_UNORM:
            !signed_input && t.format==0x84?VK_FORMAT_R5G6B5_UNORM_PACK16:
            t.format==0x9a?VK_FORMAT_R16G16B16A16_SFLOAT:VK_FORMAT_R32G32B32A32_SFLOAT;
        const bool bc=!signed_input && !t.alpha_kill && t.dimension!=3 && t.format>=0x86 && t.format<=0x88;
        const VkFormat bc_format=t.format==0x86?VK_FORMAT_BC1_RGBA_UNORM_BLOCK:t.format==0x87?VK_FORMAT_BC2_UNORM_BLOCK:VK_FORMAT_BC3_UNORM_BLOCK;
        // BC2/3 implementations may expand/interpolate endpoints with device
        // dependent precision. Use the existing precise raw compute codec so
        // both color and alpha keep the reference arithmetic on every GPU.
        const bool native_bc=bc && t.format==0x86 && vk.graphics_sampled_format(bc_format,true);
        if(native_bc)format=bc_format;
        const VkFormat depth_format=t.format==0x90?VK_FORMAT_D24_UNORM_S8_UINT:t.format==0x92?VK_FORMAT_D16_UNORM:VK_FORMAT_UNDEFINED;
        const bool native_depth=depth_format!=VK_FORMAT_UNDEFINED && vk.graphics_sampled_format(depth_format);
        if(native_depth)format=depth_format;
        const unsigned bpp=format==VK_FORMAT_D24_UNORM_S8_UINT?4:format==VK_FORMAT_D16_UNORM?2:format==VK_FORMAT_R8G8B8A8_UNORM?4:format==VK_FORMAT_R5G6B5_UNORM_PACK16?2:format==VK_FORMAT_R16G16B16A16_SFLOAT?8:16;
        std::uint64_t needed=0;
        for(const auto& l:t.levels){needed=(needed+15)&~UINT64_C(15);needed+=native_bc?std::uint64_t((l.width+3)/4)*((l.height+3)/4)*(t.format==0x86?8:16):std::uint64_t(l.width)*l.height*l.depth*bpp;}
        const bool compute_upload=!native_bc && ensure_upload_capacity(needed+size+65536);
        // Preserve the former supported upload sizes even on a device whose
        // SSBO limit cannot hold input+output together. This rare capacity path
        // uses the established codec; normal and resized uploads stay on GPU.
        if(needed>batch->capacity)throw std::runtime_error("RSX sampled texture exceeds upload capacity");
        if(batch->used+needed+(compute_upload?size+65536:0)+32*1024*1024>batch->capacity)submit_async("upload_capacity");
        trim_raw(reuse,needed);
        RawTexture entry{};entry.dirty_inputs=std::move(dirty);entry.plan=t.plan;entry.source=source;entry.origin=t.untiled?t.storage:source;entry.origin_size=t.storage_size;entry.size=size;entry.key=key;entry.last_use=++resource_clock;
        if(t.untiled)entry.owners.push_back(t.untiled);
        else for(const auto& region:state.maps){const auto p=reinterpret_cast<std::uintptr_t>(source),base=reinterpret_cast<std::uintptr_t>(region.bytes->data());
            if(p>=base && p-base<=region.bytes->size() && size<=region.bytes->size()-(p-base)){entry.owners.push_back(region.bytes);break;}}
        
        if(entry.owners.empty() && !t.plan)throw std::runtime_error("RSX sampled texture has no pinned backing");
        GuestMemory::HostGuard probe(false);
        if(t.plan){entry.trusted=observer_installed;for(const auto& run:t.plan->runs){if(!probe.add(run.data,run.size,*run.owner))entry.trusted=false;
            bool pinned=false;for(const auto& o:entry.guard_owners)if(o.owner==run.owner){pinned=true;break;}
            if(!pinned)entry.guard_owners.push_back({run.owner,run.owner->data(),run.owner->size()});}
            if(entry.trusted){auto& spans=probe.spans;std::sort(spans.begin(),spans.end(),[](const auto& a,const auto& b){return a.begin<b.begin;});
                for(const auto& span:spans){if(!entry.guarded.empty() && span.begin<=entry.guarded.back().end)entry.guarded.back().end=std::max(entry.guarded.back().end,span.end);else entry.guarded.push_back(span);}}}
        else entry.trusted=observer_installed && !t.untiled && probe.add(source,size,*entry.owners[0]);
        probe.spans.clear();
        if(!entry.trusted)entry.snapshot.assign(source,source+size);
        if(fallback_stats().enabled)++fallback_stats().paths[native_bc?"texture_block_upload":compute_upload?"texture_gpu_decode_upload":"texture_cpu_capacity_upload"];
        std::uint64_t device_offset=UINT64_MAX;
        std::vector<std::uint8_t> pixels;std::vector<VkBufferImageCopy> copies;
        std::vector<unsigned> decode(2576,0);decode[0]=t.format;decode[1]=t.levels.size();decode[5]=bpp;decode[6]=native_depth?(t.format==0x90?1:2):0;
        decode[2065]=t.mip_count;decode[2064+11]=t.features;decode[2064+12]=t.signed_mask;decode[2064+29]=t.shadow;
        decode[2064+31]=decode.size();
        for(const auto& l:t.levels)decode.insert(decode.end(),{unsigned(l.data-source),l.pitch,l.backing_width,l.backing_height,l.border,t.format,unsigned(t.swizzled),l.depth});
        decode[3]=decode.size();decode.resize(decode.size()+t.levels.size()*5);
        std::uint64_t output_bytes=0;
        for(unsigned n=0;n<t.levels.size();++n){const auto& l=t.levels[n];while(pixels.size()%16)pixels.push_back(0);
            VkBufferImageCopy copy{};copy.bufferOffset=pixels.size();copy.imageSubresource={native_depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,n%t.mip_count,n/t.mip_count,1};copy.imageExtent={l.width,l.height,l.depth};copies.push_back(copy);
            if(native_bc){const unsigned block=t.format==0x86?8:16,columns=(l.width+3)/4,rows=(l.height+3)/4;
                for(unsigned y=0;y<rows;++y){pixels.insert(pixels.end(),l.data+y*l.pitch,l.data+y*l.pitch+columns*block);}
                continue;}
            output_bytes=(output_bytes+15)&~UINT64_C(15);copy.bufferOffset=output_bytes;copies.back()=copy;
            const auto e=decode[3]+n*5;decode[e]=output_bytes;decode[e+1]=l.width;decode[e+2]=l.height;decode[e+3]=l.depth;
            output_bytes+=std::uint64_t(l.width)*l.height*l.depth*bpp;
        }
        if(!native_bc && !compute_upload){
            pixels.resize(output_bytes);
            for(unsigned n=0;n<t.levels.size();++n){const auto& l=t.levels[n];std::size_t at=copies[n].bufferOffset;
                for(unsigned z=0;z<l.depth;++z)for(unsigned y=0;y<l.height;++y)for(unsigned x=0;x<l.width;++x){auto c=texel(t,l,x,y,z);if(!t.shadow)c=texture_input(t,c,false);
                    if(native_depth){const std::uint32_t v=std::lround(c[0]*(t.format==0x90?16777215.f:65535.f));
                        if(bpp==2){const std::uint16_t v16=v;std::memcpy(pixels.data()+at,&v16,2);}else std::memcpy(pixels.data()+at,&v,4);}
                    else if(bpp==4){for(unsigned i=0;i<4;++i)pixels[at+i]=std::lround(c[i]*255.f);}
                    else if(bpp==2){const std::uint16_t v=(unsigned(std::lround(c[0]*31))<<11)|(unsigned(std::lround(c[1]*63))<<5)|unsigned(std::lround(c[2]*31));std::memcpy(pixels.data()+at,&v,2);}
                    else if(bpp==8){for(unsigned i=0;i<4;++i){const auto h=float_half(c[i]);std::memcpy(pixels.data()+at+i*2,&h,2);}}
                    else std::memcpy(pixels.data()+at,c.data(),16);
                    at+=bpp;
                }
            }
        }
        if(compute_upload){
            decode[2]=decode.size();const auto raw=decode.size();decode.resize(raw+(size+3)/4,0);std::memcpy(decode.data()+raw,source,size);
            output_bytes=(output_bytes+3)&~UINT64_C(3);
            if(decode.size()*4+output_bytes+65536>batch->capacity)throw std::runtime_error("RSX texture decode exceeds arena capacity");
            device_offset=vk.graphics_decode_texture(*batch,decode,output_bytes);
        }
        VkSamplerCreateInfo sampler{};sampler.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
        sampler.magFilter=t.mag==2?VK_FILTER_LINEAR:VK_FILTER_NEAREST;sampler.minFilter=(t.min==2 || t.min==4 || t.min==6)?VK_FILTER_LINEAR:VK_FILTER_NEAREST;
        sampler.mipmapMode=t.min>=5?VK_SAMPLER_MIPMAP_MODE_LINEAR:VK_SAMPLER_MIPMAP_MODE_NEAREST;
        auto wrap=[](unsigned w){return w==1?VK_SAMPLER_ADDRESS_MODE_REPEAT:w==2?VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT:VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;};
        sampler.addressModeU=wrap(t.wrap_s);sampler.addressModeV=wrap(t.wrap_t);sampler.addressModeW=wrap(t.wrap_r);
        // Sampler state is part of the coherent texture cache identity.
        // Explicit software filtering continues to use dynamic RSX metadata.
        sampler.mipLodBias=t.bias;sampler.minLod=t.min<=2?0:std::max(0.f,std::min(float(t.mip_count-1),t.min_lod));sampler.maxLod=t.min<=2?0:std::max(sampler.minLod,std::min(float(t.mip_count-1),t.max_lod));
        entry.image=vk.graphics_sampled_image(*batch,format,t.levels[0].width,t.levels[0].height,t.dimension==3?t.levels[0].depth:1,t.cube?6:1,t.mip_count,pixels,std::move(copies),sampler,device_offset,reusable_image);
        entry.cost=entry.image->memory_bytes;
        trim_raw(reuse,entry.cost);
        auto result=entry.image;++raw_misses;store_raw(std::move(entry),reuse);return result;
    }
'''
