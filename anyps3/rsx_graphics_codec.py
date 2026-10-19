"""Bulk graphics readback codecs; guest rows retain their pitch and padding."""
CPP = r'''

[[maybe_unused]] static std::unique_ptr<ClaimTransfer> surface_claim(const Surface& surface){
    const auto& mapping=surface.target.mapping;if(!mapping || !mapping->memory->concurrent())return {};
    std::vector<GuestMemory::HostSpan> declared;
    for(const auto& span:surface.physical)for(const auto& part:mapping->parts){
        const auto base=reinterpret_cast<std::uintptr_t>(part.data),first=std::max(base,span.begin),last=std::min(base+part.size,span.end);
        if(first<last)declared.push_back({reinterpret_cast<std::uint8_t*>(first),static_cast<unsigned>(last-first),part.owner});}
    if(declared.empty())throw std::runtime_error("RSX readback has no declared physical footprint");
    return std::make_unique<ClaimTransfer>(*mapping,std::move(declared));
}

// One exact physical transaction, including split mappings and aliases. Only
// the owned bytes are staged: pitch/tile holes are neither copied nor bumped.
// Validate all spans before payload, then take reservation locks/bump once for
// the whole writeback, rather than once for every tiled word/group.
struct SurfaceBytes {
    struct Run {unsigned guest,size,offset;GuestMemory::HostSpan span;};
    const Surface& surface;
    std::vector<Run> runs;
    std::vector<GuestMemory::HostSpan> spans;
    std::vector<std::uint8_t> bytes;
    std::unique_ptr<ClaimTransfer> transfer;
    bool writing;
    SurfaceBytes(const Surface& s,bool write,bool packed=false):surface(s),writing(write){
        const auto& mapping=surface.target.mapping;
        if(!mapping)throw std::runtime_error("RSX bulk surface has no mapping");
        std::vector<GuestMemory::HostGuard::Span> footprint;
        if(packed){for(const auto& run:s.readback->runs){const auto at=reinterpret_cast<std::uintptr_t>(run.data);footprint.push_back({at,at+run.size});}}
        else footprint=s.physical;
        for(const auto& part:mapping->parts)for(const auto& physical:footprint){
            const auto base=reinterpret_cast<std::uintptr_t>(part.data),first=std::max(base,physical.begin),last=std::min(base+part.size,physical.end);
            if(first>=last)continue;
            runs.push_back({unsigned(part.offset+first-base),unsigned(last-first),0,{reinterpret_cast<std::uint8_t*>(first),unsigned(last-first),part.owner}});
        }
        std::sort(runs.begin(),runs.end(),[](const auto& a,const auto& b){return a.guest<b.guest;});
        // GPU word tables can describe a 16-bit swizzled surface as one
        // two-byte run per pixel. Payload ownership is physical: combine only
        // exact adjacent guest/host bytes within the same pinned backing.
        // This preserves holes and avoids publishing the same dirty registry
        // once for every pixel of an otherwise contiguous transaction.
        std::size_t merged=0;
        for(const auto& run:runs){
            if(merged && runs[merged-1].span.pin==run.span.pin &&
               std::uint64_t(runs[merged-1].guest)+runs[merged-1].size==run.guest &&
               runs[merged-1].span.pointer+runs[merged-1].size==run.span.pointer &&
               std::uint64_t(runs[merged-1].size)+run.size<=UINT32_MAX){
                runs[merged-1].size+=run.size;runs[merged-1].span.size+=run.size;
            }else{runs[merged++]=run;}
        }
        runs.resize(merged);
        unsigned total=0;for(auto& run:runs){run.offset=total;total+=run.size;spans.push_back(run.span);}
        bytes.resize(total);
        if(mapping->memory->concurrent()){
            transfer=std::make_unique<ClaimTransfer>(*mapping,spans);
            for(const auto& run:runs)transfer->claim->commit_spans(*mapping,run.guest,run.size,write);
        }else{mapping->check_epoch();if(write && !mapping->writable)throw std::runtime_error("RSX bulk surface write permission");}
    }
    const std::uint8_t* packed_data(const Surface::ReadbackPlan& plan)const{
        if(plan.runs.size()!=runs.size())return nullptr;
        for(unsigned i=0;i<runs.size();++i)if(runs[i].guest!=plan.runs[i].guest_offset || runs[i].size!=plan.runs[i].size || runs[i].offset!=plan.runs[i].offset)return nullptr;
        return bytes.data();
    }
    void snapshot(){
        for(const auto& run:runs){auto* out=bytes.data()+run.offset;
            if(transfer)GuestMemory::atomic_read(run.span.pin->coherent(run.span.pointer),out,run.size);
            else surface.target.mapping->read(run.guest,out,run.size);}
    }
    template<bool Write> void copy(unsigned offset,std::uint8_t* data,unsigned count){
        auto run=std::upper_bound(runs.begin(),runs.end(),offset,[](unsigned a,const auto& b){return a<b.guest;});
        if(run!=runs.begin())--run;
        while(count){
            if(run==runs.end() || offset<run->guest || offset-run->guest>=run->size)throw std::runtime_error("RSX bulk surface outside footprint");
            const auto delta=offset-run->guest,n=std::min(count,run->size-delta);
            auto* local=bytes.data()+run->offset+delta;
            if constexpr(Write)std::memcpy(local,data,n);else std::memcpy(data,local,n);
            data+=n;offset+=n;count-=n;++run;
        }
    }
    void read(unsigned at,std::uint8_t* out,unsigned n){copy<false>(at,out,n);}
    void write(unsigned at,const std::uint8_t* in,unsigned n){copy<true>(at,const_cast<std::uint8_t*>(in),n);}
    void commit(){
        if(!writing)throw std::runtime_error("RSX bulk surface read-only commit");
        auto& memory=*surface.target.mapping->memory;
        if(transfer){
            transfer->claim->require_owner();GuestMemory::LineLocks locks(memory,spans);memory.bump_parallel(spans);
            for(const auto& run:runs){GuestMemory::atomic_write(run.span.pin->coherent(run.span.pointer),bytes.data()+run.offset,run.size);memory.publish_gpu_write(run.span.pointer,run.size);}
        }else for(const auto& run:runs)surface.target.mapping->write(run.guest,bytes.data()+run.offset,run.size);
    }
};

// Byte permutation loops deliberately have no Target lookup, observer or
// reservation hash lookup per pixel. GCC/Clang vectorize the linear kernels.
static void unpack_row(std::uint8_t* __restrict out,const std::uint8_t* __restrict in,
        const std::uint8_t* __restrict stencil,unsigned width,unsigned format,bool depth){
    if(!depth && format==8){
        for(unsigned x=0;x<width;++x){out[4*x]=in[4*x+3];out[4*x+1]=in[4*x];out[4*x+2]=in[4*x+1];out[4*x+3]=in[4*x+2];}
    }else if(depth && format==2){
        for(unsigned x=0;x<width;++x){unsigned v;std::memcpy(&v,in+4*x,4);write_be(out+4*x,4,(v<<8)|stencil[x]);}
    }else{
        const unsigned bpp=depth?2:color_bytes(format),word=depth || format==3 || format==11?2:4;
        const std::uint16_t endian=1;
        if(!*reinterpret_cast<const std::uint8_t*>(&endian)){std::memcpy(out,in,width*bpp);return;}
        if(word==2)for(unsigned x=0;x<width*bpp;x+=2){out[x]=in[x+1];out[x+1]=in[x];}
        else for(unsigned x=0;x<width*bpp;x+=4){out[x]=in[x+3];out[x+1]=in[x+2];out[x+2]=in[x+1];out[x+3]=in[x];}
    }
}
template<unsigned B> static void interleave_pixels(std::uint8_t* out,const std::uint8_t* a,const std::uint8_t* b,unsigned width){
    for(unsigned x=0;x<width;++x){std::memcpy(out+2*x*B,a+x*B,B);std::memcpy(out+(2*x+1)*B,b+x*B,B);}
}
template<unsigned B> static void deinterleave_pixels(const std::uint8_t* in,std::uint8_t* a,std::uint8_t* b,unsigned width){
    for(unsigned x=0;x<width;++x){std::memcpy(a+x*B,in+2*x*B,B);std::memcpy(b+x*B,in+(2*x+1)*B,B);}
}
static void load_pixels(const Surface& surface,std::uint8_t* out,std::uint8_t* stencil){
    const auto& t=surface.target;const unsigned bpp=t.bpp,nx=surface.samples>1?2:1,ny=surface.samples==4?2:1;
    const unsigned row_bytes=surface.width*nx*bpp;
    std::vector<std::uint8_t> row(row_bytes),plane(nx>1?row_bytes:0),linear;
    std::unique_ptr<SurfaceBytes> bulk;if(t.mapping){bulk=std::make_unique<SurfaceBytes>(surface,false);bulk->snapshot();}
    // The immutable readback permutation is also the inverse upload plan.
    // Snapshot only its declared physical runs; padding and unrelated tile
    // pages are never read. This replaces one Range/lease per 32B tile group
    // (or per swizzled word) with one per contiguous physical interval.
    if(t.mapping && (t.tile || t.swizzle_width) && surface.readback && !(row_bytes&3) && t.pitch>=row_bytes){
        const auto& plan=*surface.readback;const auto bytes=std::uint64_t(row_bytes)*surface.height*ny;
        bool complete=plan.words.size()*4==bytes;
        for(const auto& run:plan.runs)if((run.size|run.offset)&3)complete=false;
        if(complete){
            std::vector<std::uint8_t> packed;const auto* data=bulk->packed_data(plan);
            if(!data){packed.resize(bytes);for(const auto& run:plan.runs){
                if(run.offset>bytes || run.size>bytes-run.offset)throw std::runtime_error("RSX inverse upload bounds");
                bulk->read(run.guest_offset,packed.data()+run.offset,run.size);
            }data=packed.data();}
            linear.resize(bytes);
            for(unsigned i=0;i<plan.words.size();++i){const auto logical=plan.words[i];const unsigned y=logical/t.pitch,x=logical%t.pitch;
                if(y>=surface.height*ny || x>row_bytes-4)throw std::runtime_error("RSX inverse upload permutation bounds");
                std::memcpy(linear.data()+std::uint64_t(y)*row_bytes+x,data+4*i,4);
            }
        }
    }
    auto read=[&](unsigned offset,std::uint8_t* p,unsigned count){
        if(bulk)bulk->read(offset,p,count);else std::memcpy(p,t.base+offset,count);
    };
    for(unsigned y=0;y<surface.height;++y)for(unsigned sy=0;sy<ny;++sy){const unsigned ty=y*ny+sy;
        if(!linear.empty())std::memcpy(row.data(),linear.data()+std::uint64_t(ty)*row_bytes,row_bytes);
        else if(!t.tile && !t.swizzle_width)read(t.offset-t.base_offset+ty*t.pitch,row.data(),row.size());
        else if(t.tile){unsigned x=0;while(x<row.size()){
            const unsigned linear=t.offset+ty*t.pitch+x,count=std::min<unsigned>(row.size()-x,32-(linear&31));
            read(rsx_tiled_offset(*t.tile,linear-t.tile->offset)-t.base_offset,row.data()+x,count);x+=count;}
        }else for(unsigned x=0;x<surface.width*nx;++x)for(unsigned byte=0;byte<bpp;byte+=4)
            read(t.address(x,ty,byte),row.data()+x*bpp+byte,std::min(4u,bpp-byte));
        if(nx>1){auto* a=plane.data();auto* b=a+surface.width*bpp;
            switch(bpp){case 1:deinterleave_pixels<1>(row.data(),a,b,surface.width);break;case 2:deinterleave_pixels<2>(row.data(),a,b,surface.width);break;case 4:deinterleave_pixels<4>(row.data(),a,b,surface.width);break;
                case 8:deinterleave_pixels<8>(row.data(),a,b,surface.width);break;case 16:deinterleave_pixels<16>(row.data(),a,b,surface.width);break;
                default:throw std::runtime_error("unsupported graphics upload pixel size");}
        }
        for(unsigned sx=0;sx<nx;++sx){const unsigned layer=sy*nx+sx,index=(layer*surface.height+y)*surface.width;
            const auto* in=nx>1?plane.data()+sx*surface.width*bpp:row.data();auto* destination=out+std::uint64_t(index)*image_bpp(surface.image->format);
            if(surface.image->format==VK_FORMAT_R32_UINT){
                for(unsigned x=0;x<surface.width;++x){const unsigned v=read_be(in+x*bpp,bpp);std::memcpy(destination+4*x,&v,4);}
            }else if(!surface.depth && surface.format==8){
                for(unsigned x=0;x<surface.width;++x){destination[4*x]=in[4*x+1];destination[4*x+1]=in[4*x+2];destination[4*x+2]=in[4*x+3];destination[4*x+3]=in[4*x];}
            }else if(surface.depth && surface.format==2){
                for(unsigned x=0;x<surface.width;++x){const unsigned v=read_be(in+4*x,4)>>8;std::memcpy(destination+4*x,&v,4);stencil[index+x]=in[4*x+3];}
            }else unpack_row(destination,in,nullptr,surface.width,surface.format,surface.depth);
        }
    }
}
static void commit_pixels(GuestMemory& memory,Surface& surface,const std::uint8_t* from,const std::uint8_t* stencil){
    const auto& t=surface.target;const unsigned bpp=t.bpp,n=surface.samples,nx=n>1?2:1,ny=n==4?2:1;
    GuestMemory::ObserverPause pause(memory);
    // GPU completion invalidates every observed reservation in the physical
    // range once, including aliases and reservations taken after bind().
    if(t.mapping){if(!memory.concurrent())t.mapping->mark(0,t.mapping->size);}
    else memory.mark_write(t.base,t.pitch*(surface.height*ny-1)+surface.width*nx*bpp);
    std::unique_ptr<SurfaceBytes> bulk;if(t.mapping)bulk=std::make_unique<SurfaceBytes>(surface,true);
    auto store=[&](unsigned offset,const std::uint8_t* p,unsigned count){
        if(bulk)bulk->write(offset,p,count);
        else std::memcpy(t.base+offset,p,count);
    };
    std::vector<std::uint8_t> row(surface.width*nx*bpp),plane(nx>1?surface.width*nx*bpp:0);
    for(unsigned y=0;y<surface.height;++y)for(unsigned sy=0;sy<ny;++sy){
        for(unsigned sx=0;sx<nx;++sx){const unsigned layer=sy*nx+sx,index=(layer*surface.height+y)*surface.width;
            auto* out=nx==1?row.data():plane.data()+sx*surface.width*bpp;
            const auto* input=from+std::uint64_t(index)*image_bpp(surface.image->format);
            if(surface.image->format==VK_FORMAT_R32_UINT){
                for(unsigned x=0;x<surface.width;++x){unsigned v;std::memcpy(&v,input+4*x,4);write_be(out+x*bpp,bpp,v);}
            }else unpack_row(out,input,stencil?stencil+index:nullptr,surface.width,surface.format,surface.depth);
        }
        if(nx>1){const auto* a=plane.data();const auto* b=a+surface.width*bpp;
            switch(bpp){case 1:interleave_pixels<1>(row.data(),a,b,surface.width);break;case 2:interleave_pixels<2>(row.data(),a,b,surface.width);break;case 4:interleave_pixels<4>(row.data(),a,b,surface.width);break;
                case 8:interleave_pixels<8>(row.data(),a,b,surface.width);break;case 16:interleave_pixels<16>(row.data(),a,b,surface.width);break;
                default:throw std::runtime_error("unsupported graphics readback pixel size");}
        }
        const unsigned ty=y*ny+sy;
        if(!t.tile && !t.swizzle_width){store(t.offset-t.base_offset+ty*t.pitch,row.data(),row.size());continue;}
        if(t.tile){
            // RSX tiling preserves each aligned 32-byte group. One mapping
            // lookup and copy per group instead of one lookup per pixel.
            unsigned x=0;while(x<row.size()){
                const unsigned linear=t.offset+ty*t.pitch+x;
                const unsigned count=std::min<unsigned>(row.size()-x,32-(linear&31));
                store(rsx_tiled_offset(*t.tile,linear-t.tile->offset)-t.base_offset,row.data()+x,count);x+=count;
            }
        }else{
            for(unsigned x=0;x<surface.width*nx;++x)for(unsigned byte=0;byte<bpp;byte+=4)
                store(t.address(x,ty,byte),row.data()+x*bpp+byte,std::min(4u,bpp-byte));
        }
    }
    if(bulk)bulk->commit();
}
'''
