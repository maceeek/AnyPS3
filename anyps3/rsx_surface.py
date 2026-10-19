"""RSX surface addressing and Vulkan-backed packed clears.

The address permutation describes RSX's 256-byte by 64-row tile blocks.
Compression tags are kept uncompressed: compression is a storage optimization,
not permission to discard the configured address layout or sample locations.
"""

CPP = r'''
[[maybe_unused]] static std::uint32_t rsx_tiled_offset(const NativeRsx::Tile& tile,std::uint32_t linear){
    if(!tile.pitch || tile.pitch%256 || tile.offset%65536 || tile.bank>3 || linear>=tile.size)
        throw std::runtime_error("invalid RSX tile address configuration");
    const unsigned columns=tile.pitch/256;
    unsigned factor=columns;
    if(columns&(columns-1)){
        factor=0;
        for(unsigned divisor:{3,5,7,11,13})if(columns%divisor==0){factor=columns/divisor;break;}
    }
    if(!factor || (factor&(factor-1)))throw std::runtime_error("unsupported RSX tile pitch");
    const unsigned byte_address=tile.offset+linear;
    const unsigned row=linear/tile.pitch,block_y=row/64;
    const unsigned block=(tile.offset/16384)+block_y*columns+(linear%tile.pitch)/256;
    unsigned selector=block&3;
    if(factor==2)selector=(selector+2*(block_y&1))&3;
    unsigned bank=factor==1?selector:(((selector&1)*2+(selector>>1)+block_y)&3);
    bank=(bank+tile.bank)&3;
    unsigned physical=((block>>2)<<16)|(bank<<14)|((row&56)<<8)|((byte_address&224)<<3)|
        ((((row>>2)^(byte_address>>6))&1)<<7)|((row&3)<<5)|(byte_address&31);
    physical^=(((physical>>12)^block)&1)<<9;
    physical^=((physical>>11)&1)<<10;
    if(physical<tile.offset || std::uint64_t(physical)+4>std::uint64_t(tile.offset)+tile.size)
        throw std::runtime_error("RSX tiled sample outside configured tile");
    return physical;
}

// CPU span walkers visit consecutive 32-byte runs. All eight runs in a
// 256-byte horizontal block share its division/bank/row calculation. Keep
// only that prefix, with exact tile fields; no address table or guest bytes
// are cached. Validation remains lazy at the first original memory access.
struct RsxTileAddress {
    std::array<unsigned,4> key{};
    bool valid=false;
    unsigned columns=0,factor=0,last_block=UINT32_MAX,prefix=0;
    void refresh(const NativeRsx::Tile& tile){
        const std::array<unsigned,4> next{tile.offset,tile.size,tile.pitch,tile.bank};
        if(valid && key==next)return;
        if(!tile.pitch || tile.pitch%256 || tile.offset%65536 || tile.bank>3)
            throw std::runtime_error("invalid RSX tile address configuration");
        const auto count=tile.pitch/256;unsigned scale=count;
        if(count&(count-1)){scale=0;for(unsigned divisor:{3,5,7,11,13})if(count%divisor==0){scale=count/divisor;break;}}
        if(!scale || (scale&(scale-1)))throw std::runtime_error("unsupported RSX tile pitch");
        key=next;columns=count;factor=scale;last_block=UINT32_MAX;valid=true;
    }
    unsigned address(const NativeRsx::Tile& tile,unsigned linear){
        // Match the original order: an out-of-range access is a configuration
        // fault even when the pitch itself is also unsupported.
        if(linear>=tile.size)throw std::runtime_error("invalid RSX tile address configuration");
        refresh(tile);
        const auto group=linear>>8;
        if(group!=last_block){
            const unsigned row=group/columns,block_y=row/64;
            const unsigned block=(tile.offset/16384)+block_y*columns+group%columns;
            unsigned selector=block&3;if(factor==2)selector=(selector+2*(block_y&1))&3;
            unsigned bank=factor==1?selector:(((selector&1)*2+(selector>>1)+block_y)&3);bank=(bank+tile.bank)&3;
            prefix=((block>>2)<<16)|(bank<<14)|((row&56)<<8)|((row&3)<<5)|((row&4)<<5);
            prefix^=(((row>>4)^block)&1)<<9;prefix^=((row>>3)&1)<<10;
            last_block=group;
        }
        const unsigned physical=prefix^((linear&224)<<3)^((linear&64)<<1)^(linear&31);
        if(physical<tile.offset || std::uint64_t(physical)+4>std::uint64_t(tile.offset)+tile.size)
            throw std::runtime_error("RSX tiled sample outside configured tile");
        return physical;
    }
};


// Packed storage model derived from RPCS3 ffba2bf0 VKGSRender::clear_surface,
// Utils/color_utils.hpp and Common/TextureUtils.cpp. CLEAR_SURFACE selects its
// own components; SET_COLOR_MASK(_MRT), depth-write and back-stencil masks are
// draw state and do not gate this command. Float color clears are defined NOPs.
// Clear-only declarations follow its entry-point marker. Scanout-only users
// can include the tile helper prefix without pulling in the IO callback.
[[maybe_unused]] static void rsx_clear_surface(PpuState& s,std::uint32_t mask);
static std::uint64_t rsx_address(const NativeRsx& gpu,std::uint32_t offset); // rsx.py
[[maybe_unused]] static void rsx_clear_surface(PpuState& s,std::uint32_t mask){
    if(!mask)return;
#ifdef ANYPS3_RSX_VK
    if(s.rsx.graphics_clear && s.rsx.graphics_clear(mask))return;
#endif
#ifndef ANYPS3_VULKAN
    (void)s;
    throw std::runtime_error("RSX surface clear requires a Vulkan build (-DANYPS3_VULKAN -lvulkan)");
#else
    auto& gpu=s.rsx;auto& m=s.memory;++gpu.cpu_clears;
    if(std::getenv("ANYPS3_RSX_CLEAR_TRACE")){
        std::fprintf(stderr,"RSX CPU CLEAR mask=%x callback=%u",mask,unsigned(
#ifdef ANYPS3_RSX_VK
            bool(gpu.graphics_clear)
#else
            false
#endif
        ));
        for(unsigned reg:{0x208u,0x200u,0x204u,0x220u,0x2b8u,0x3b0u,0x32cu,0x1d98u,0x1d9cu,0x194u,0x198u,0x20cu,0x22cu,0x210u,0x214u})
            std::fprintf(stderr," r%x=%x",reg,gpu.registers.count(reg)?gpu.registers.at(reg):0u);
        std::fputc('\n',stderr);
    }
    auto state=[&](unsigned reg,unsigned reset=0u){
        const auto it=gpu.registers.find(reg);return it==gpu.registers.end()?reset:it->second;
    };
    if(mask&~0xf3u)throw std::runtime_error("unsupported RSX clear mask bits");
    // These registers have a decoder but neither reference backend implements
    // custom rectangles. Do not guess that they intersect the scissor.
    for(unsigned reg:{0x1d98u,0x1d9cu})if(state(reg,0x0fff0000)!=0x0fff0000)
        throw std::runtime_error("unsupported RSX custom clear rectangle");
    const auto format=state(0x208),horizontal=state(0x200),vertical=state(0x204);
    const unsigned raster=(format>>8)&15,aa=(format>>12)&15;
    if(raster!=1 && raster!=2)throw std::runtime_error("unsupported RSX clear raster type");
    if(aa!=0 && aa!=3 && aa!=4 && aa!=5)throw std::runtime_error("unsupported RSX clear antialiasing");
    const unsigned samples_x=aa?2:1,samples_y=aa>=4?2:1;
    const unsigned width=horizontal>>16,height=vertical>>16;
    // Zero clip = invalid framebuffer; RPCS3 VKGSRender::clear_surface returns
    // without clearing (VKGSRender.cpp:1329-1332, RSXThread.cpp:1477-1487).
    if(!width || !height)return;
    if(width>4096 || height>4096)throw std::runtime_error("invalid RSX clear dimensions");
    const unsigned sample_width=width*samples_x,sample_height=height*samples_y;
    const bool swizzled=raster==2;
    unsigned storage_width=sample_width,storage_height=sample_height;
    if(swizzled){
        const unsigned lw=(format>>16)&255,lh=format>>24;
        if(lw>12 || lh>12)throw std::runtime_error("invalid RSX swizzled clear dimensions");
        storage_width=(1u<<lw)*samples_x;storage_height=(1u<<lh)*samples_y;
        if(sample_width>storage_width || sample_height>storage_height)
            throw std::runtime_error("RSX clear clip exceeds swizzled surface dimensions");
    }
    const unsigned window_x=state(0x2b8)&0xffff,window_y=state(0x2b8)>>16;
    if(swizzled && (window_x || window_y))throw std::runtime_error("unsupported RSX swizzled clear window offset");
    // RPCS3 RSXThread.cpp get_framebuffer_layout leaves clip origins unused;
    // the framebuffer starts at the surface/window offset, not clip_origin*bpp.
    const auto sx=state(0x8c0),sy=state(0x8c4);
    const unsigned x=std::min(width,sx&0xffff)*samples_x,y=std::min(height,sy&0xffff)*samples_y;
    const unsigned right=std::min(width,(sx&0xffff)+(sx>>16))*samples_x;
    const unsigned bottom=std::min(height,(sy&0xffff)+(sy>>16))*samples_y;
    if(right==x || bottom==y)return;
    struct Surface {
        unsigned offset=0,pitch=0,base=0,span=0,bpp=0,log_w=0,log_h=0;
        bool swizzled=false;
        std::array<std::uint8_t,4> value{},bits{};
        const NativeRsx::Tile* tile=nullptr;
        GuestMemory::Range mapping;
        std::vector<std::uint8_t> rgba;
        unsigned address(unsigned row,unsigned col)const{
            std::uint64_t linear=0;
            if(swizzled){
                unsigned bit=0;
                for(unsigned i=0;i<std::max(log_w,log_h);++i){
                    if(i<log_w)linear|=std::uint64_t((col>>i)&1)<<bit++;
                    if(i<log_h)linear|=std::uint64_t((row>>i)&1)<<bit++;
                }
                linear=offset+linear*bpp;
            }else linear=std::uint64_t(offset)+std::uint64_t(row)*pitch+col*bpp;
            const auto physical=tile?std::uint64_t(rsx_tiled_offset(*tile,(linear-tile->offset)&~3u))+(linear&3):linear;
            if(physical<base || physical-base>span || bpp>span-(physical-base))
                throw std::runtime_error("RSX clear sample outside surface");
            return static_cast<unsigned>(physical);
        }
    };
    std::vector<Surface> surfaces;
    auto prepare=[&](unsigned context,unsigned offset,unsigned pitch,unsigned bpp,unsigned value,unsigned bits,bool depth){
        if(!bits)return;
        // 0xfeed0000 = local VRAM, 0xfeed0001 = main memory through the IO
        // aperture (same contiguity rule as draw location 1).
        if(context!=0xfeed0000 && context!=0xfeed0001)throw std::runtime_error("unsupported RSX clear surface DMA context");
        const bool main=context==0xfeed0001;const std::uint64_t limit=main?0x10000000u:gpu.local_size;
        Surface surface;surface.bpp=bpp;surface.swizzled=swizzled;
        surface.pitch=swizzled?storage_width*bpp:pitch;
        const auto start=std::uint64_t(offset)+std::uint64_t(window_y)*samples_y*surface.pitch+std::uint64_t(window_x)*samples_x*bpp;
        const auto span=swizzled?std::uint64_t(storage_width)*storage_height*bpp:
            std::uint64_t(sample_height-1)*surface.pitch+sample_width*bpp;
        if(surface.pitch<sample_width*bpp || surface.pitch%bpp || start%bpp ||
           start>=limit || span>limit-start)
            throw std::runtime_error("RSX clear surface outside VRAM/IO aperture or invalid pitch");
        surface.offset=surface.base=static_cast<unsigned>(start);surface.span=static_cast<unsigned>(span);
        while((1u<<surface.log_w)<storage_width)++surface.log_w;
        while((1u<<surface.log_h)<storage_height)++surface.log_h;
        for(unsigned b=0;b<bpp;++b){surface.value[b]=value>>(8*(bpp-1-b));surface.bits[b]=bits>>(8*(bpp-1-b));}
        for(const auto& tile:gpu.tiles)if(tile.bound && tile.location==(main?1u:0u) && start<std::uint64_t(tile.offset)+tile.size && tile.offset<start+span){
            if(swizzled || surface.tile || start<tile.offset || start+span>std::uint64_t(tile.offset)+tile.size || tile.pitch!=surface.pitch)
                throw std::runtime_error("RSX clear crosses tile boundaries or mismatches tile layout");
            // Existing runtime stores supported compression modes expanded.
            const bool supported=tile.compression==0 || (depth?(tile.compression>=9 && tile.compression<=12):tile.compression==7);
            if(!supported)throw std::runtime_error("unsupported RSX clear tile compression mode");
            surface.tile=&tile;surface.base=tile.offset;surface.span=tile.size;
        }
        std::uint64_t address=UINT64_C(0xc0000000)+surface.base;
        if(main){
            address=rsx_address(gpu,surface.base);
            for(std::uint64_t at=(std::uint64_t(surface.base)&~UINT64_C(0xfffff))+0x100000;at<std::uint64_t(surface.base)+surface.span;at+=0x100000)
                if(rsx_address(gpu,static_cast<std::uint32_t>(at))!=address+(at-surface.base))
                    throw std::runtime_error("RSX clear main-memory surface spans discontiguous IO pages");
        }
        // Read and write permissions are checked before Vulkan or mark_write.
        (void)m.range(address,surface.span,false);
        surface.mapping=m.range(address,surface.span,true);
        surface.rgba.resize(std::size_t(sample_width)*sample_height*4);
        // Validate every address before submitting any clear or touching VRAM.
        for(unsigned row=0;row<sample_height;++row)for(unsigned col=0;col<sample_width;++col){
            auto* out=surface.rgba.data()+(std::size_t(row)*sample_width+col)*4;
            surface.mapping.read(surface.address(row,col)-surface.base,out,bpp);
        }
        surfaces.push_back(std::move(surface));
    };
    if(mask&0xf0){
        const unsigned f=format&31;unsigned bpp=4,value=state(0x1d90),bits=0;
        if(f==1 || f==2 || f==3){
            bpp=2;
            if(mask&0x10)bits|=f==3?0xf800:0x7c00;
            if(mask&0x20)bits|=f==3?0x07e0:0x03e0;
            if(mask&0x40)bits|=0x001f;
            if(f!=3 && (mask&0x80))bits|=0x8000;
            if(f==1)value&=0x7fff;else if(f==2)value|=0x8000;
        }else if(f==9){bpp=1;bits=(mask&0x40)?0xff:0;}
        else if(f==10){bpp=2;bits=((mask&0x20)?0xff00:0)|((mask&0x40)?0xff:0);}
        else if(f==11 || f==12 || f==13){
            // CLEAR_SURFACE cannot clear floating-point color targets (both
            // GL and Vulkan reference paths explicitly remove all color bits).
        }else if(f==4 || f==5 || f==8 || f==14 || f==15 || f==16){
            if(mask&0x10)bits|=0x00ff0000;
            if(mask&0x20)bits|=0x0000ff00;
            if(mask&0x40)bits|=0x000000ff;
            if(mask&0x80)bits|=0xff000000;
            // Even ABGR clear uses the raw register word: the reference swaps
            // both clear values AND masks before its ABGR attachment conversion.
        }else throw std::runtime_error("unsupported RSX clear color format");
        std::vector<unsigned> targets;
        switch(state(0x220)){
        case 0:break;case 1:targets={0};break;case 2:targets={1};break;
        case 0x13:targets={0,1};break;case 0x17:targets={0,1,2};break;
        case 0x1f:targets={0,1,2,3};break;
        default:throw std::runtime_error("unsupported RSX clear color target");
        }
        static constexpr unsigned contexts[]={0x194,0x18c,0x1b4,0x1b8};
        static constexpr unsigned offsets[]={0x210,0x218,0x288,0x28c};
        static constexpr unsigned pitches[]={0x20c,0x21c,0x280,0x284};
        for(unsigned t:targets)prepare(state(contexts[t]),state(offsets[t]),state(pitches[t]),bpp,value,bits,false);
    }
    if(mask&3){
        const unsigned f=(format>>5)&7;unsigned bits=0;
        if(f!=1 && f!=2)throw std::runtime_error("unsupported RSX clear depth format");
        if(mask&1)bits=f==1?0xffff:0xffffff00;
        if(f==2 && (mask&2))bits|=state(0x32c)&255;
        const unsigned value=state(0x1d8c);
        if((mask&1) && f==2 && (state(0x3b0)&0x1000)){
            // F24 is an unsigned E8M16 encoding, expanded by appending
            // seven zero bits. Nonfinite depth is not characterized here.
            const unsigned raw=value>>8;
            if((raw&0xff0000)==0xff0000)
                throw std::runtime_error("unsupported RSX nonfinite float clear depth");
        }
        prepare(state(0x198),state(0x214),state(0x22c),f==1?2:4,value,bits,true);
    }
    // Reject aliases conservatively, including distinct attachments in one
    // tile: their physical overlap has not been characterized for this command.
    for(unsigned i=0;i<surfaces.size();++i)for(unsigned j=0;j<i;++j)
        if(surfaces[i].mapping.overlaps(surfaces[j].mapping))
            throw std::runtime_error("overlapping RSX clear attachments");
    if(surfaces.empty())return;
    if(!gpu.renderer)gpu.renderer=std::make_unique<NativeVulkan>(s.rsx.host);
    // RGBA8 transports packed bytes exactly, including depth, without converting
    // them to host depth/float formats. Each physical MSAA sample is a texel.
    for(auto& surface:surfaces){
        const auto& v=surface.value;
        const unsigned argb=(unsigned(v[3])<<24)|(unsigned(v[0])<<16)|(unsigned(v[1])<<8)|v[2];
        gpu.renderer->clear(surface.rgba,sample_width,sample_height,x,y,right-x,bottom-y,argb);
    }
    // All validation and Vulkan readbacks succeeded. Component/stencil masks
    // merge only their packed bits; padding and all other bytes stay untouched.
    for(auto& surface:surfaces)for(unsigned row=y;row<bottom;++row)for(unsigned col=x;col<right;++col){
        const auto offset=surface.address(row,col)-surface.base;
        std::array<std::uint8_t,4> out{};surface.mapping.read(offset,out.data(),surface.bpp);
        const auto* p=surface.rgba.data()+(std::size_t(row)*sample_width+col)*4;
        for(unsigned b=0;b<surface.bpp;++b)out[b]=(out[b]&~surface.bits[b])|(p[b]&surface.bits[b]);
        surface.mapping.write(offset,out.data(),surface.bpp);
    }
    ++gpu.rendered_clears;
    std::fprintf(stderr,"native Vulkan RSX clear=%llu surface=%ux%u samples=%u mask=%02x attachments=%zu\n",
        static_cast<unsigned long long>(gpu.rendered_clears),width,height,samples_x*samples_y,mask,surfaces.size());
#endif
}
'''
