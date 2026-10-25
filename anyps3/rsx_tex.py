"""Native RSX texture sampling; source evidence in batch/rsxtex-result.md."""

CPP = r'''
static float srgb_to_linear(float);
struct TextureLevel {
    unsigned width=0,height=0,pitch=0; const std::uint8_t* data=nullptr;
    unsigned depth=1,backing_width=0,backing_height=0,border=0;
    std::shared_ptr<const std::vector<Vec4>> decoded{};
    std::uint64_t offset=0;
};
struct TexturePlan {
    struct Run {const std::uint8_t* data;std::uint64_t size,offset;std::shared_ptr<GuestBacking> owner;std::uint64_t address=UINT64_MAX,mapping_id=0;};
    std::vector<Run> runs;std::uint64_t bytes=0;bool gathered=false;
};
// Layouts are immutable on cache hits. Preserve Texture value semantics when
// a decoder/materializer edits a level, without allocating on every copy.
class TextureLevels {
    using Storage=std::vector<TextureLevel>;
    std::shared_ptr<Storage> values;
    bool exposed=false;
    static const Storage& empty_values(){static const Storage empty;return empty;}
    const Storage& read()const{return values?*values:empty_values();}
    Storage& edit(){if(!values)values=std::make_shared<Storage>();else if(!values.unique())values=std::make_shared<Storage>(*values);return *values;}
public:
    TextureLevels()=default;
    TextureLevels(const TextureLevels& other):values(other.exposed && other.values?std::make_shared<Storage>(*other.values):other.values){}
    TextureLevels& operator=(const TextureLevels& other){if(this!=&other){auto next=other.exposed && other.values?std::make_shared<Storage>(*other.values):other.values;values=std::move(next);exposed=false;}return *this;}
    TextureLevels(TextureLevels&&)=default;TextureLevels& operator=(TextureLevels&&)=default;
    // Only fresh decoder-owned layouts may forget temporary mutable borrows.
    // Once an external mutable reference/iterator escapes, copies must detach
    // immediately: a later write through that old borrow cannot call edit().
    void freeze(){exposed=false;}
    std::size_t size()const{return read().size();}bool empty()const{return read().empty();}
    const TextureLevel& operator[](std::size_t i)const{return read()[i];}
    TextureLevel& operator[](std::size_t i){auto& v=edit()[i];exposed=true;return v;}
    const TextureLevel& back()const{return read().back();}TextureLevel& back(){auto& v=edit().back();exposed=true;return v;}
    void push_back(const TextureLevel& v){edit().push_back(v);}void resize(std::size_t n){edit().resize(n);}
    auto begin()const{return read().begin();}auto end()const{return read().end();}
    auto begin(){auto i=edit().begin();exposed=true;return i;}auto end(){auto i=edit().end();exposed=true;return i;}
};
struct Texture {
    bool valid=false;unsigned format=0,wrap_s=0,wrap_t=0,min=0,mag=0,gamma=0;bool unnormalized=false,swizzled=false;unsigned shadow=0;
    TextureLevels levels;float bias=0,min_lod=0,max_lod=0;
    std::array<unsigned,4> remap_channel{},remap_control{};
    Vec4 border{};
    // Host-address envelope used only for conservative draw alias checks.
    // A gathered texture can span several noncontiguous guest backings.
    const std::uint8_t* storage=nullptr;std::uint64_t storage_size=0;
    std::shared_ptr<GuestBacking> untiled;
    std::shared_ptr<TexturePlan> plan;bool deferred=false;
    unsigned dimension=2,wrap_r=3,mip_count=1;bool cube=false;
    unsigned signed_mask=0,biased=0,features=0;
#ifdef ANYPS3_RSX_VK
    std::shared_ptr<NativeVulkan::GraphicsImage> resident_image;
#endif
    bool alpha_kill=false;unsigned max_aniso=1,aniso_bias=0;
    // A fragment program samples a disabled unit (TEXTURE_CONTROL0 bit 31 clear): RPCS3 ffba2bf0 binds a
    // transparent-black null image with no texture control bits (VK/VKDraw.cpp:724-735, vkutils/scratch.cpp:58-110),
    // so every sample is vec4(0). Only the Vulkan graphics fragment path models it; valid stays false.
    bool null_texture=false;
};
#ifdef ANYPS3_RSX_VK
template<class State> static std::shared_ptr<NativeVulkan::GraphicsImage> rsx_graphics_texture(State&,unsigned,unsigned,unsigned,unsigned,unsigned,unsigned,bool,bool);
#endif
// RPCS3 image_utils.hpp:31-64,156-180: ceil-log2 Morton, X then Y then Z.
// Large texels are adjacent 32-bit words along X (TextureUtils.cpp:258-271).
static unsigned texture_pot(unsigned n){unsigned p=1;while(p<n)p*=2;return p;}
static std::uint64_t texture_swizzle(unsigned w,unsigned h,unsigned d,unsigned x,unsigned y,unsigned z,unsigned bpp,unsigned byte=0){
    const unsigned block=std::min(bpp,4u),words=bpp/block;
    w=texture_pot(w*words);h=texture_pot(h);d=texture_pot(d);x=x*words+byte/block;
    std::uint64_t index=0;unsigned bit=0;
    for(unsigned i=0;(w>>i)>1 || (h>>i)>1 || (d>>i)>1;++i){
        if((w>>i)>1)index|=std::uint64_t((x>>i)&1)<<bit++;
        if((h>>i)>1)index|=std::uint64_t((y>>i)&1)<<bit++;
        if((d>>i)>1)index|=std::uint64_t((z>>i)&1)<<bit++;
    }
    return index*block+byte%block;
}
static unsigned texture_features(unsigned f){
    // bit0=SNORM, bit1=gamma, bit2=biased normalization, bit3=16-bit lanes.
    switch(f){
    case 0x90:case 0x91:case 0x92:case 0x93:case 0x98:return 4;
    case 0x94:return 12;case 0x95:return 13;
    case 0x99:case 0x9a:case 0x9b:case 0x9c:case 0x9f:return 0;
    default:return 7;
    }
}
static unsigned texture_remap(unsigned f,unsigned control){
    // RSXTexture.cpp:243-309. Upper word selects XXXY for two-lane formats.
    const bool order=control>>16;unsigned lo=control&255;
    switch(f){
    case 0x9a:case 0x9b:case 0x9c:lo=0xe4;break;
    case 0x9f:lo=order?0x56:0x66;break;
    case 0x94:case 0x95:case 0x98:case 0x99:
        switch(lo){case 0xe4:lo=order?0x56:0x66;break;case 0x4e:lo=order?0xa9:0x99;break;
        case 0xee:lo=0xaa;break;case 0x44:lo=0x55;break;
        default:throw std::runtime_error("unsupported RSX two-channel remap encoding");}
    }
    return (control&0xff00)|lo;
}
static void configure_texture_sampling(Texture& t,const Reg& reg,unsigned unit,bool vertex=false){
    const unsigned base=(vertex?0x900:0x1a00)+unit*32,control0=reg.get(base+0xc,0);
    if(!(control0>>31))throw std::runtime_error("RSX FP samples disabled texture unit "+std::to_string(unit));
    t.alpha_kill=!vertex && (control0&4);static constexpr unsigned aniso_levels[]={1,2,4,6,8,10,12,16};t.max_aniso=aniso_levels[(control0>>4)&7];
    const auto address=reg(base+8),control1=vertex?0xaae4:reg(base+0x10),filter=vertex?(reg(base+0x14)&0x1fff)|0x01010000:reg(base+0x14);
    t.shadow=0;
    t.wrap_s=address&15;t.wrap_t=(address>>8)&15;t.wrap_r=(address>>16)&15;
    t.gamma=vertex?0:(address>>20)&15; // RGBA, applied after remap (RSXTexture.cpp:95-99).
    t.features=texture_features(t.format);t.signed_mask=vertex?0:filter>>28;t.biased=(address>>12)&15;
    if(t.biased>1)throw std::runtime_error("invalid RSX unsigned texture remap");
    // RPCS3 exposes SIGNED_REMAP_CLAMPED=3 but implements no algorithm for it.
    if((address>>24)&15)throw std::runtime_error("RSX signed remap CLAMPED semantics are undocumented");
    t.aniso_bias=(address>>4)&15; // Preserved; isotropic approximation above.
    if(t.format>=0x90 && t.format<=0x93){
        const unsigned zfunc=address>>28;
        if(zfunc>7)throw std::runtime_error("invalid RSX depth texture comparison");
        if(zfunc>0 && zfunc<7)t.shadow=zfunc;
    }
    t.min=(filter>>16)&7;t.mag=(filter>>24)&7;
    // CONVOLUTION_MIN (7) / CONVOLUTION_MAG (4): the RSX kernel (filter bits
    // 13-15) is undocumented. Derived approximation from RPCS3: gcm_enums.h
    // 1532/1547 ("looks close to linear"), VK/VKFormats.cpp:44,56 map them to
    // a linear filter without mipmapping.
    if(t.min==7)t.min=2;
    if(t.mag==4)t.mag=2;
    if(t.min<1 || t.min>6 || (t.mag!=1 && t.mag!=2))
        throw std::runtime_error("unsupported RSX texture filter 0x"+[&]{char b[16];std::snprintf(b,sizeof b,"%08x",unsigned(filter));return std::string(b);}());
    std::int32_t bias=filter&0x1fff;if(bias&0x1000)bias-=0x2000;
    t.bias=bias/256.f;t.min_lod=((control0>>19)&0xfff)/256.f;t.max_lod=((control0>>7)&0xfff)/256.f;
    const unsigned remap=texture_remap(t.format,control1);
    for(unsigned i=0;i<4;++i){t.remap_channel[i]=(remap>>(2*i))&3;t.remap_control[i]=(remap>>(8+2*i))&3;
        if(t.remap_control[i]==3)throw std::runtime_error("invalid RSX texture remap selector");}
    if(t.min_lod>t.max_lod)throw std::runtime_error("invalid RSX texture LOD range");
    const auto border_colour=reg.get(base+0x1c,0);
    t.border={((border_colour>>16)&255)/255.f,((border_colour>>8)&255)/255.f,(border_colour&255)/255.f,(border_colour>>24)/255.f};
    t.valid=true;
}
template<class State> static Texture decode_texture(State& s,const Reg& reg,unsigned unit,bool vertex=false,bool metadata=false,const std::shared_ptr<TexturePlan>& reusable_plan={}){
    if(host_stats().enabled){++host_stats().texture_metadata;if(!metadata)++host_stats().texture_materialize;}
    Texture t;auto& gpu=s.rsx;
    const unsigned base=(vertex?0x900:0x1a00)+unit*32;
    const auto control0=reg.get(base+0xc,0);
    if(!(control0>>31))throw std::runtime_error("RSX FP samples disabled texture unit "+std::to_string(unit));
    t.alpha_kill=!vertex && (control0&4);
    // Derived approximation, NOT the undocumented RSX anisotropic footprint:
    // sample with the existing isotropic 1x filter/LOD for every aniso setting.
    // RPCS3 delegates AF to host samplers (VKDraw.cpp, GL/glutils/sampler.cpp);
    // it supplies no software RSX algorithm. Preserve the requested state.
    static constexpr unsigned aniso_levels[]={1,2,4,6,8,10,12,16};
    t.max_aniso=aniso_levels[(control0>>4)&7];
    const auto format=reg(base+4),rect=reg(base+0x18);
#ifdef ANYPS3_RSX_VK
    const auto filter=vertex?(reg(base+0x14)&0x1fff)|0x01010000:reg(base+0x14);
#endif
    const unsigned location=format&3,dimension=(format>>4)&15,mipmaps=std::max(1u,format>>16);
    if(location!=1 && location!=2)throw std::runtime_error("invalid RSX texture location");
    t.dimension=dimension;t.cube=format&4;
    if(dimension<1 || dimension>3 || (t.cube && dimension!=2))throw std::runtime_error("invalid RSX texture dimension/cubemap");
    // Bit 3 clear = CELL_GCM_TEXTURE_BORDER_TEXTURE: rows/columns of border
    // texels surround every level (one texel for linear layouts).
    const bool border_texels=!vertex && !(format&8);
    const unsigned raw_format=(format>>8)&0xff;const bool linear=raw_format&0x20;
    t.unnormalized=raw_format&0x40;t.format=raw_format&~0x60u;
    const unsigned width=rect>>16,height=dimension==1?1:rect&0xffff;
    const unsigned control3=reg.get(vertex?base+0x10:0x1840+unit*4,0);
    const unsigned depth=dimension==3?control3>>20:1;
    if(!depth || depth>512 || (t.cube && width!=height))throw std::runtime_error("invalid RSX texture depth/cube size");
    if((dimension==1 && height!=1) || !width || !height || width>4096 || height>4096 || mipmaps>13)throw std::runtime_error("invalid RSX texture size or mipmap count");
    unsigned bytes_per_block=0,block=1;
    switch(t.format){
    case 0x81:bytes_per_block=1;break;
    case 0x82:case 0x83:case 0x84:case 0x8b:case 0x8f:case 0x92:case 0x93:case 0x94:case 0x97:case 0x98:case 0x99:case 0x9d:bytes_per_block=2;break;
    case 0x85:case 0x90:case 0x91:case 0x95:case 0x9c:case 0x9e:case 0x9f:bytes_per_block=4;break;
    case 0x9a:bytes_per_block=8;break;case 0x9b:bytes_per_block=16;break;
    case 0x8d:case 0x8e:bytes_per_block=4;block=2;break;
    case 0x86:bytes_per_block=8;block=4;break;
    case 0x87:case 0x88:bytes_per_block=16;block=4;break;
    default:throw std::runtime_error("unsupported RSX texture format "+std::to_string(t.format));
    }
    t.swizzled=!linear && block==1;
    // TextureUtils.cpp:626-706: face-major mip chains, constant LN pitch,
    // packed non-LN levels, and 128-byte alignment between non-LN faces.
    const unsigned suggested=control3&0xfffff;
    std::uint64_t offset=0,required=0;
    const unsigned border=border_texels && block==1?(linear?1:4):0;
    struct Pending{unsigned w,h,pitch,d,bw,bh;std::uint64_t offset;};std::vector<Pending> pending;
    t.mip_count=mipmaps;
    for(unsigned face=0;face<(t.cube?6u:1u);++face){
        unsigned w=width,h=height,d=depth;
        for(unsigned level=0;level<mipmaps;++level){
            unsigned bw=(w+block-1)/block,bh=block==4?(h+3)/4:h;
            if(border){bw+=2*border;bh+=2*border;if(!linear){bw=texture_pot(bw);bh=texture_pot(bh);}}
            const unsigned packed=bw*bytes_per_block;
            const unsigned pitch=linear?(suggested?suggested:packed):packed;
            if(pitch<packed)throw std::runtime_error("RSX texture pitch smaller than row");
            if(pitch%bytes_per_block)throw std::runtime_error("misaligned RSX texture pitch");
            pending.push_back({w,h,pitch,d,bw,bh,offset});
            const std::uint64_t size=std::uint64_t(pitch)*bh*d;
            std::uint64_t span=size;
            if(t.swizzled)span=std::max(span,texture_swizzle(bw,bh,d,bw-1,bh-1,d-1,bytes_per_block,bytes_per_block-1)+1);
            required=std::max(required,offset+span);offset+=size;
            w=std::max(1u,w/2);h=std::max(1u,h/2);d=std::max(1u,d/2);
        }
        if(!linear && t.cube)offset=(offset+127)&~UINT64_C(127);
    }
    offset=required;
    if(offset>0x10000000)throw std::runtime_error("RSX texture too large");
    const unsigned texture_offset=reg(base);
    const std::uint64_t limit=location==1?gpu.local_size:0x10000000u;
    if(texture_offset>=limit || offset>limit-texture_offset)throw std::runtime_error("RSX texture outside VRAM/IO aperture");
    bool tiled=false;
    for(const auto& tile:gpu.tiles)if(tile.bound && tile.location==location-1 &&
        texture_offset<std::uint64_t(tile.offset)+tile.size && tile.offset<std::uint64_t(texture_offset)+offset)tiled=true;
    const std::uint8_t* data=nullptr;
#ifdef ANYPS3_RSX_VK
    if(!metadata && !vertex && !t.cube && dimension==2 && mipmaps==1 && !border && !(filter>>28))
        t.resident_image=rsx_graphics_texture(s,texture_offset,location-1,width,height,pending[0].pitch,t.format,t.swizzled,false);
    if(t.resident_image){
        GuestMemory::ObserverPause snapshot_metadata(s.memory);
        const auto start=location_address(gpu,location-1,texture_offset,static_cast<unsigned>(offset));
        const auto source=s.memory.range(start,static_cast<unsigned>(offset),false);
        data=range_identity(source,0,1);
        std::uintptr_t low=UINTPTR_MAX,high=0;for(const auto& part:source.parts){const auto p=reinterpret_cast<std::uintptr_t>(part.data);low=std::min(low,p);high=std::max(high,p+part.size);}
        t.storage=reinterpret_cast<const std::uint8_t*>(low);t.storage_size=high-low;
    }else
#endif
    if(metadata){
        GuestMemory::ObserverPause validate_mapping(s.memory);
        if(reusable_plan){
            if(reusable_plan->bytes!=offset)throw std::runtime_error("RSX reusable texture layout size mismatch");
            t.plan=reusable_plan;t.deferred=true;
            for(const auto& run:t.plan->runs){
                const auto first=reinterpret_cast<std::uintptr_t>(run.data),old=reinterpret_cast<std::uintptr_t>(t.storage);
                const auto low=t.storage?std::min(old,first):first,high=t.storage?std::max(old+t.storage_size,first+run.size):first+run.size;
                t.storage=reinterpret_cast<const std::uint8_t*>(low);t.storage_size=high-low;
            }
        }else{
        t.plan=std::make_shared<TexturePlan>();t.plan->bytes=offset;t.plan->gathered=tiled;t.deferred=true;
        // Cache a proven first-match effective interval. range() scans every
        // region (twice) and allocates a Part vector for each 32-byte tile run.
        // Earlier overlapping regions bound the interval, so direct mutations
        // of the region list retain exactly the original first-match rules.
        std::size_t cached_region=SIZE_MAX;std::uint64_t cached_low=0,cached_high=0;
        auto append=[&](unsigned physical,unsigned n,unsigned logical){
            const auto start=location_address(gpu,location-1,physical,n);
            auto next_address=start;
            auto pin=[&](const std::uint8_t* pointer,unsigned count,const std::shared_ptr<GuestBacking>& owner,std::uint64_t mapping_id){
                auto& runs=t.plan->runs;
                if(!runs.empty() && runs.back().owner==owner && runs.back().data+runs.back().size==pointer && runs.back().offset+runs.back().size==logical && runs.back().mapping_id==mapping_id && runs.back().address+runs.back().size==next_address)runs.back().size+=count;
                else runs.push_back({pointer,count,logical,owner,next_address,mapping_id});
                logical+=count;next_address+=count;
                const auto first=reinterpret_cast<std::uintptr_t>(pointer),old=reinterpret_cast<std::uintptr_t>(t.storage);
                const auto low=t.storage?std::min(old,first):first,high=t.storage?std::max(old+t.storage_size,first+count):first+count;
                t.storage=reinterpret_cast<const std::uint8_t*>(low);t.storage_size=high-low;
            };
            if(cached_region!=SIZE_MAX && start>=cached_low && std::uint64_t(start)+n<=cached_high){
                const auto& region=rsx_maps(s)[cached_region];
                if(!(region.flags&4))throw std::runtime_error("guest memory permission fault");
                pin(region.bytes->data()+start-region.base,n,region.bytes,region.identity);s.memory.last_region=cached_region;return;
            }
            const auto source=s.memory.range(start,n,false);
            for(const auto& part:source.parts)pin(part.data,part.size,part.owner,part.mapping_id);
            if(!source.parts.empty()){
                const auto address=std::uint64_t(start)+source.parts.back().offset;
                for(std::size_t i=0;i<rsx_maps(s).size();++i){const auto& region=rsx_maps(s)[i];
                    if(address<region.base || address-region.base>=region.bytes->size())continue;
                    cached_region=i;cached_low=region.base;cached_high=region.base+region.bytes->size();
                    for(std::size_t j=0;j<i;++j){const auto& earlier=rsx_maps(s)[j];const auto end=earlier.base+earlier.bytes->size();
                        if(end<=address)cached_low=std::max(cached_low,end);
                        else if(earlier.base>address)cached_high=std::min(cached_high,earlier.base);
                    }
                    break;
                }
            }
        };
        RsxTileAddress tile_address;
        if(!tiled)append(texture_offset,offset,0);
        else for(unsigned at=0;at<offset;){
            const unsigned logical=texture_offset+at;unsigned n=std::min<unsigned>(32-(logical&31),offset-at);
            const NativeRsx::Tile* selected=nullptr;
            for(const auto& tile:gpu.tiles)if(tile.bound && tile.location==location-1){
                const auto end=std::uint64_t(tile.offset)+tile.size;
                if(logical>=tile.offset && logical<end){if(selected)throw std::runtime_error("overlapping RSX texture tiles");selected=&tile;n=std::min<std::uint64_t>(n,end-logical);}
                else if(tile.offset>logical)n=std::min(n,tile.offset-logical);
            }
            unsigned physical=logical;
            if(selected){const auto& tile=*selected;if(tile.compression!=0 && (tile.compression<7 || tile.compression>12))throw std::runtime_error("unsupported RSX texture tile compression mode");
                const unsigned relative=logical-tile.offset;physical=tile_address.address(tile,relative&~3u)+(relative&3);
                if(physical<tile.offset || std::uint64_t(physical)+n>std::uint64_t(tile.offset)+tile.size)throw std::runtime_error("RSX tiled texture span outside tile");}
            append(physical,n,at);at+=n;
        }
        }
        t.plan->gathered=tiled || t.plan->runs.size()!=1;
        if(!t.plan->gathered)data=t.plan->runs[0].data;
    }else if(tiled){
        t.untiled=std::make_shared<GuestBacking>(static_cast<unsigned>(offset));
        RsxTileAddress tile_address;
        // Compose texture byte addressing (including Morton and mip offsets)
        // with the memory-controller tile permutation. Texture pitch and tile
        // pitch describe different stages; they need not match. Select each
        // byte run separately, including untiled gaps and adjacent tiles.
        // RPCS3 RSXMemoryTiling.glsl:242-321, Core/RSXContext.cpp:24-40.
        for(unsigned at=0;at<offset;){
            const unsigned logical=texture_offset+at;
            unsigned n=std::min<unsigned>(32-(logical&31),static_cast<unsigned>(offset)-at);
            const NativeRsx::Tile* selected=nullptr;
            for(const auto& tile:gpu.tiles)if(tile.bound && tile.location==location-1){
                const auto end=std::uint64_t(tile.offset)+tile.size;
                if(logical>=tile.offset && logical<end){
                    if(selected)throw std::runtime_error("overlapping RSX texture tiles");
                    selected=&tile;n=std::min<std::uint64_t>(n,end-logical);
                }else if(tile.offset>logical)n=std::min(n,tile.offset-logical);
            }
            unsigned physical=logical;
            if(selected){
                const auto& tile=*selected;
                // Like the existing draw/clear model, defined compression tags
                // hold expanded sample bytes; no physical tag codec is claimed.
                if(tile.compression!=0 && (tile.compression<7 || tile.compression>12))
                    throw std::runtime_error("unsupported RSX texture tile compression mode");
                const unsigned relative=logical-tile.offset;
                physical=tile_address.address(tile,relative&~3u)+(relative&3);
                if(physical<tile.offset || std::uint64_t(physical)+n>std::uint64_t(tile.offset)+tile.size)
                    throw std::runtime_error("RSX tiled texture span outside tile");
            }
            const auto address=location_address(gpu,location-1,physical,n);
            // A single preserved 32-byte run can still straddle two guest
            // mappings. Bound it to the first region, without merging storage
            // or assuming that adjacent effective addresses share a host base.
            for(const auto& region:rsx_maps(s))if(address>=region.base && address-region.base<region.bytes->size()){
                n=std::min<std::uint64_t>(n,region.bytes->size()-(address-region.base));break;
            }
            const auto source_range=s.memory.range(address,n,false);
            const auto* source=range_identity(source_range,0,n);
            if(!source)throw std::runtime_error("RSX tiled texture run lost contiguous mapping");
            // Existing consumers compare integer host ranges, never dereference
            // this envelope. Include every source to avoid missed feedback/races.
            const auto first=reinterpret_cast<std::uintptr_t>(source);
            const auto old=reinterpret_cast<std::uintptr_t>(t.storage);
            const auto low=t.storage?std::min(old,first):first;
            const auto high=t.storage?std::max(old+t.storage_size,first+n):first+n;
            if(!t.storage || first<old)t.storage=source;
            t.storage_size=high-low;
            source_range.read(0,t.untiled->data()+at,n);at+=n;
        }
        data=t.untiled->data();
    }else{
        const auto start=location_address(gpu,location-1,texture_offset,static_cast<unsigned>(offset));
        const auto source=s.memory.range(start,static_cast<unsigned>(offset),false);
        if(source.parts.size()==1 && !s.memory.concurrent()){data=source.parts[0].data;t.storage=data;t.storage_size=offset;}
        else{
            t.untiled=std::make_shared<GuestBacking>(offset);source.read(0,t.untiled->data(),offset);data=t.untiled->data();
            std::uintptr_t low=UINTPTR_MAX,high=0;for(const auto& part:source.parts){const auto p=reinterpret_cast<std::uintptr_t>(part.data);low=std::min(low,p);high=std::max(high,p+part.size);}
            t.storage=reinterpret_cast<const std::uint8_t*>(low);t.storage_size=high-low;
        }
    }
    for(const auto& l:pending){t.levels.push_back({l.w,l.h,l.pitch,data?data+l.offset:nullptr,l.d,l.bw,l.bh,border});t.levels.back().offset=l.offset;}
    configure_texture_sampling(t,reg,unit,vertex);t.levels.freeze();return t;
}

template<class State> static void read_texture_run(State& state,const TexturePlan::Run& run,std::uint8_t* out){
    if(run.address==UINT64_MAX){pinned_host_read(state.memory,run.data,out,run.size);return;}
    auto mapping=state.memory.range(run.address,run.size,false);
    if(mapping.parts.size()!=1 || mapping.parts[0].data!=run.data || mapping.parts[0].owner!=run.owner || mapping.parts[0].mapping_id!=run.mapping_id)
        throw std::runtime_error("stale RSX texture mapping identity");
    mapping.read(0,out,run.size);
}
// Materialize only for an upload/CPU consumer, after resource cache lookup.
template<class State> static void materialize_texture(State& s,Texture& t){
    if(!t.deferred)return;
    HostByteAccess exact_bytes{(t.plan->gathered || s.memory.concurrent()) && s.memory.host_access_observer.supports_exact_bytes.load(std::memory_order_acquire),true};
    if(host_stats().enabled)++host_stats().texture_materialize;
    for(const auto& run:t.plan->runs)s.memory.notify_host(run.data,run.size,false);
    const std::uint8_t* data=nullptr;
    if(t.plan->gathered || s.memory.concurrent()){t.untiled=std::make_shared<GuestBacking>(t.plan->bytes);
        if(s.memory.concurrent() && std::all_of(t.plan->runs.begin(),t.plan->runs.end(),[](const auto& run){return run.address!=UINT64_MAX;})){
            std::vector<GuestMemory::Range> mappings;mappings.reserve(t.plan->runs.size());
            std::vector<GuestMemory::HostSpan> declared;declared.reserve(t.plan->runs.size());
            for(const auto& run:t.plan->runs){
                if(run.size>UINT_MAX)throw std::runtime_error("RSX texture run exceeds guest range size");
                auto mapping=s.memory.range(run.address,unsigned(run.size),false);
                if(mapping.parts.size()!=1 || mapping.parts[0].data!=run.data || mapping.parts[0].owner!=run.owner || mapping.parts[0].mapping_id!=run.mapping_id)
                    throw std::runtime_error("stale RSX texture mapping identity");
                declared.push_back({const_cast<std::uint8_t*>(run.data),unsigned(run.size),run.owner});mappings.push_back(std::move(mapping));
            }
            if(!mappings.empty()){
                ClaimTransfer transfer(mappings.front(),std::move(declared));
                for(unsigned i=0;i<mappings.size();++i){const auto& mapping=mappings[i];auto bytes=transfer.claim->snapshot(mapping,0,mapping.size);
                    std::copy(bytes.begin(),bytes.end(),t.untiled->data()+t.plan->runs[i].offset);}
            }
        }else for(const auto& run:t.plan->runs){
            if(s.memory.concurrent() || HostByteAccess::active())read_texture_run(s,run,t.untiled->data()+run.offset);
            else std::memcpy(t.untiled->data()+run.offset,run.data,run.size);
        }
        data=t.untiled->data();
    }else data=t.plan->runs[0].data;
    for(auto& level:t.levels)level.data=data+level.offset;
    t.deferred=false;
}
static Vec4 texel(const Texture& t,const TextureLevel& l,int x,int y,int z=0){
    if(host_stats().enabled)++host_stats().cpu_texel;
    // Draw-owned immutable decode; safe to share between raster workers.
    if(l.decoded)return (*l.decoded)[(std::size_t(z)*l.height+y)*l.width+x];
    // Returns R,G,B,A. Coordinates were wrapped into the level.
    x+=l.border;y+=l.border;
    if(t.format!=0x86 && t.format!=0x87 && t.format!=0x88){
        const unsigned bpp=t.format==0x81?1:t.format==0x9b?16:t.format==0x9a?8:
            (t.format==0x85||t.format==0x90||t.format==0x91||t.format==0x95||t.format==0x9c||t.format==0x9e||t.format==0x9f||t.format==0x8d||t.format==0x8e)?4:2;
        const unsigned bw=l.backing_width?l.backing_width:l.width,bh=l.backing_height?l.backing_height:l.height;
        std::uint64_t offset=(std::uint64_t(z)*bh+y)*l.pitch+std::uint64_t(x)*bpp;
        if(t.format==0x8d || t.format==0x8e)offset=(std::uint64_t(z)*bh+y)*l.pitch+std::uint64_t(x/2)*4;
        if(t.swizzled){
            offset=texture_swizzle(bw,bh,l.depth,x,y,z,bpp);
        }
        std::array<std::uint8_t,16> wide{};
        const auto* p=l.data+offset;
        if(t.swizzled && bpp>4){
            for(unsigned i=0;i<bpp;i+=4)std::copy_n(l.data+texture_swizzle(bw,bh,l.depth,x,y,z,bpp,i),4,wide.data()+i);
            p=wide.data();
        }
        if(t.format==0x9a || t.format==0x9b){Vec4 c;const unsigned n=t.format==0x9a?2:4;for(unsigned i=0;i<4;++i)c[i]=n==2?half_float(read_be(p+i*n,n)):bits_float(read_be(p+i*n,n));return c;}
        if(t.format==0x9c){const float x=bits_float(read_be(p,4));return {x,x,x,x};}
        if(t.format==0x94){const float x=read_be(p,2)/65535.f;return {1,x,1,x};}
        if(t.format==0x95){const float x=read_be(p,2)/65535.f,y=read_be(p+2,2)/65535.f;return {x,y,x,y};}
        if(t.format==0x9f){const float x=half_float(read_be(p,2)),y=half_float(read_be(p+2,2));return {y,x,y,x};}
        if(t.format==0x98 || t.format==0x99){
            auto lane=[&](unsigned v){return t.format==0x98?v/255.f:std::max(-1.f,float(v<128?int(v):int(v)-256)/127.f);};
            return {lane(p[0]),lane(p[1]),lane(p[0]),lane(p[1])};
        }
        if(t.format==0x8d)return {p[(x&1)?3:1]/255.f,p[2]/255.f,p[0]/255.f,1};
        if(t.format==0x8e)return {p[(x&1)?2:0]/255.f,p[3]/255.f,p[1]/255.f,1};
        if(t.format==0x91 || t.format==0x93){
            const float value=t.format==0x91?bits_float((read_be(p,4)>>8)<<7):bits_float(read_be(p,2)<<11)*bits_float(0x77000000);
            return {value,value,value,value};
        }
        if(t.format==0x90 || t.format==0x92){const float x=t.format==0x90?(read_be(p,4)>>8)/16777215.f:read_be(p,2)/65535.f;return {x,x,x,x};}
        if(t.format==0x9e)return {p[1]/255.f,p[2]/255.f,p[3]/255.f,1};
        if(t.format==0x85)return {p[1]/255.f,p[2]/255.f,p[3]/255.f,p[0]/255.f};
        if(t.format==0x81)return {p[0]/255.f,p[0]/255.f,p[0]/255.f,1};
        if(t.format==0x8b)return {p[1]/255.f,p[0]/255.f,p[1]/255.f,p[0]/255.f};
        const unsigned raw=(unsigned(p[0])<<8)|p[1];
        if(t.format==0x8f)return {((raw>>10)&63)/63.f,((raw>>5)&31)/31.f,(raw&31)/31.f,1};
        if(t.format==0x84)return {((raw>>11)&31)/31.f,((raw>>5)&63)/63.f,(raw&31)/31.f,1};
        if(t.format==0x97)return {((raw>>11)&31)/31.f,((raw>>6)&31)/31.f,((raw>>1)&31)/31.f,raw&1?1.f:0.f};
        if(t.format==0x9d)return {((raw>>10)&31)/31.f,((raw>>5)&31)/31.f,(raw&31)/31.f,1};
        if(t.format==0x82)return {((raw>>10)&31)/31.f,((raw>>5)&31)/31.f,(raw&31)/31.f,raw&0x8000?1.f:0.f};
        return {((raw>>8)&15)/15.f,((raw>>4)&15)/15.f,(raw&15)/15.f,(raw>>12)/15.f};
    }
    const unsigned bx=x/4,by=y/4,px=x%4,py=y%4;
    const unsigned block_bytes=t.format==0x86?8:16;
    // NV_texture_compression_vtc: blocks from up to four Z slices interleave.
    const unsigned group=(unsigned(z)/4)*4,count=std::min(4u,l.depth-group);
    const auto* b=l.data+std::uint64_t(group)*l.pitch*((l.height+3)/4)+
        (std::uint64_t(by)*l.pitch+std::uint64_t(bx)*block_bytes)*count+(unsigned(z)%4)*block_bytes;
    const auto* colour=t.format==0x86?b:b+8;
    // DXT blocks are little-endian words as on PC.
    const unsigned c0=colour[0]|(colour[1]<<8),c1=colour[2]|(colour[3]<<8);
    const unsigned index=(colour[4+py]>>(2*px))&3;
    auto rgb=[](unsigned c){return std::array<float,3>{((c>>11)&31)/31.f,((c>>5)&63)/63.f,(c&31)/31.f};};
    const auto a0=rgb(c0),a1=rgb(c1);
    std::array<float,3> c{};float alpha=1;
    if(c0>c1 || t.format!=0x86){
        switch(index){case 0:c=a0;break;case 1:c=a1;break;
            case 2:for(unsigned i=0;i<3;++i)c[i]=(2*a0[i]+a1[i])/3;break;
            default:for(unsigned i=0;i<3;++i)c[i]=(a0[i]+2*a1[i])/3;break;}
    }else{
        switch(index){case 0:c=a0;break;case 1:c=a1;break;
            case 2:for(unsigned i=0;i<3;++i)c[i]=(a0[i]+a1[i])/2;break;
            default:c={0,0,0};alpha=0;break;}
    }
    if(t.format==0x87){const unsigned nibble=(b[py*2+px/2]>>(4*(px&1)))&15;alpha=nibble/15.f;}
    else if(t.format==0x88){
        const unsigned al0=b[0],al1=b[1];
        std::uint64_t bits=0;for(unsigned i=0;i<6;++i)bits|=std::uint64_t(b[2+i])<<(8*i);
        const unsigned ai=(bits>>(3*(py*4+px)))&7;
        float a;
        if(ai==0)a=al0;else if(ai==1)a=al1;
        else if(al0>al1)a=((8-ai)*al0+(ai-1)*al1)/7.f;
        else if(ai<6)a=((6-ai)*al0+(ai-1)*al1)/5.f;
        else a=ai==6?0:255;
        alpha=a/255.f;
    }
    return {c[0],c[1],c[2],alpha};
}
static void prepare_texture_cache(Texture& t){
    if(t.format!=0x85 && t.format!=0x9a)return;
    std::uint64_t count=0;for(const auto& l:t.levels)count+=std::uint64_t(l.width)*l.height*l.depth;
    if(count>262144)return; // Bound scratch and decode work for tiny draws.
    for(auto& l:t.levels){
        auto decoded=std::make_shared<std::vector<Vec4>>();decoded->reserve(std::size_t(l.width)*l.height*l.depth);
        for(unsigned z=0;z<l.depth;++z)for(unsigned y=0;y<l.height;++y)for(unsigned x=0;x<l.width;++x)
            decoded->push_back(texel(t,l,x,y,z));
        l.decoded=std::move(decoded);
    }
}
static int wrap_coordinate(unsigned mode,int i,unsigned size,bool& border){
    const int n=int(size);
    switch(mode){
    case 1:return ((i%n)+n)%n;
    case 2:{const int period=2*n;int k=((i%period)+period)%period;return k<n?k:period-1-k;}
    case 3:case 5:return std::min(n-1,std::max(0,i));
    case 4:if(i<0 || i>=n){border=true;return 0;}return i;
    case 6:case 8:{int k=i<0?-i-1:i;return std::min(n-1,k);}
    default:throw std::runtime_error("unsupported RSX texture wrap mode "+std::to_string(mode));
    }
}
struct CubeCoord{unsigned face;float u,v;};
static CubeCoord cube_coordinate(float x,float y,float z){
    const float ax=std::fabs(x),ay=std::fabs(y),az=std::fabs(z);float u=0,v=0,m=0;unsigned f=0;
    if(ax>=ay && ax>=az){m=ax;f=x>=0?0:1;u=x>=0?-z:z;v=-y;}
    else if(ay>=az){m=ay;f=y>=0?2:3;u=x;v=y>=0?z:-z;}
    else{m=az;f=z>=0?4:5;u=z>=0?x:-x;v=-y;}
    if(!(m>0) || !std::isfinite(m))throw std::runtime_error("invalid RSX cubemap direction");
    return {f,(u/m+1)*.5f,(v/m+1)*.5f};
}
static float texture_gradient_length(const Texture& t,const Vec4& c,const Vec4& d){
    const auto& l=t.levels[0];
    if(t.cube){
        const auto q=cube_coordinate(c[0],c[1],c[2]);const unsigned axis=q.face/2;
        const float m=std::fabs(c[axis]),dm=c[axis]>=0?d[axis]:-d[axis];
        const float ds=q.face==0?-d[2]:q.face==1?d[2]:q.face==5?-d[0]:d[0];
        const float dt=q.face==2?d[2]:q.face==3?-d[2]:-d[1];
        return std::hypot((ds-(2*q.u-1)*dm)*(.5f*l.width/m),(dt-(2*q.v-1)*dm)*(.5f*l.height/m));
    }
    const float x=d[0]*(t.unnormalized?1:l.width),y=t.dimension==1?0:d[1]*(t.unnormalized?1:l.height);
    const float z=t.dimension==3?d[2]*(t.unnormalized?1:l.depth):0;
    return std::sqrt(x*x+y*y+z*z);
}
static Vec4 texture_input(const Texture& t,Vec4 c,bool border){
    if(!(t.features&1))return c;
    const unsigned bits=!border && (t.features&8)?16:8;const unsigned full=(1u<<bits)-1,sign=1u<<(bits-1);
    for(unsigned i=0;i<4;++i)if(t.signed_mask&(1u<<((i+1)%4))){
        // Actual texture bytes are signed before filtering. PS3 border SNORM
        // instead uses biased normalization (RSXTexture.cpp:383-432).
        const int raw=int(std::floor(std::clamp(c[i],0.f,1.f)*full+.5f));
        const int value=border?raw-int(sign):raw>=int(sign)?raw-int(full)-1:raw;
        c[i]=std::max(-1.f,float(value)/float(sign-1));
    }
    return c;
}
static Vec4 filter_level(const Texture& t,unsigned level,float un,float vn,bool bilinear,float shadow_ref=0,float wn=0,unsigned face=0){
    const auto& l=t.levels[face*t.mip_count+level];
    // Ordinary 1D/2D samples ignore the unused lanes, including NaN/Inf.
    // A cube direction has already been projected to face-local UV here.
    // RPCS3 GLSLCommon.cpp:568-613 passes x / xy / xyz respectively.
    const float u=un*l.width,v=t.dimension==1?.5f:vn*l.height,w=t.dimension==3?wn*l.depth:0.f;
    // Non-finite or huge texel coordinates have no defined RSX result (RPCS3
    // leaves it to the host sampler). Both renderers sample (0,0,0,0) instead of
    // converting NaN/inf to an integer address (undefined behaviour in C++).
    if(!std::isfinite(u) || !std::isfinite(v) || !std::isfinite(w) ||
       std::fabs(u)>1048576.f || std::fabs(v)>1048576.f || std::fabs(w)>1048576.f)return Vec4{0,0,0,0};
    auto fetch=[&](int x,int y,int z){
        bool border=false;unsigned f=face;
        int xx=0,yy=0;
        if(t.cube){
            // Match the non-seamless GL cube sampler used by RPCS3's GL
            // backend: the selected face clamps taps to its own edge.
            xx=std::clamp(x,0,int(l.width)-1);yy=std::clamp(y,0,int(l.height)-1);
        }else{
            xx=wrap_coordinate(t.wrap_s,x,l.width,border);yy=wrap_coordinate(t.wrap_t,y,l.height,border);
        }
        const int zz=t.dimension==3?wrap_coordinate(t.wrap_r,z,l.depth,border):0;
        auto c=border?t.border:texel(t,t.levels[f*t.mip_count+level],xx,yy,zz);
        if(t.shadow){
            const float z=c[0];bool pass=false;
            switch(t.shadow){case 1:pass=shadow_ref<z;break;case 2:pass=shadow_ref==z;break;case 3:pass=shadow_ref<=z;break;
                case 4:pass=shadow_ref>z;break;case 5:pass=shadow_ref!=z;break;case 6:pass=shadow_ref>=z;break;}
            const float result=pass?1.f:0.f;return Vec4{result,result,result,result};
        }
        return texture_input(t,c,border);
    };
    if(!bilinear)return fetch(int(std::floor(u)),int(std::floor(v)),int(std::floor(w)));
    const float fu=u-.5f,fv=v-.5f,fw=w-.5f;const int x0=int(std::floor(fu)),y0=int(std::floor(fv)),z0=int(std::floor(fw));
    const float ax=fu-x0,ay=fv-y0,az=fw-z0;
    auto plane=[&](int z){
        const auto c00=fetch(x0,y0,z),c10=fetch(x0+1,y0,z),c01=fetch(x0,y0+1,z),c11=fetch(x0+1,y0+1,z);
        Vec4 c;for(unsigned i=0;i<4;++i)c[i]=(c00[i]*(1-ax)+c10[i]*ax)*(1-ay)+(c01[i]*(1-ax)+c11[i]*ax)*ay;
        return c;
    };
    auto c=plane(t.dimension==3?z0:0);
    if(t.dimension==3){const auto c1=plane(z0+1);for(unsigned i=0;i<4;++i)c[i]=c[i]*(1-az)+c1[i]*az;}
    return c;
}
// lambda: log2 of the screen-space footprint in level-0 texels (-inf for none).
static Vec4 sample_texture(const Texture& t,float u,float v,float lambda=-std::numeric_limits<float>::infinity(),float shadow_ref=0,float w=0,bool* killed=nullptr){
    PerfTimer timer(perf_work?&perf_work->texture_ns:nullptr);
    if(perf_work)++perf_work->texture_samples;
    if(t.alpha_kill && !killed)throw std::runtime_error("RSX texture alpha kill requires fragment discard integration");
    const auto& base=t.levels[0];
    unsigned face=0;
    if(t.cube){const auto q=cube_coordinate(u,v,w);face=q.face;u=q.u;v=q.v;}
    if(t.unnormalized && !t.cube){u/=base.width;v/=base.height;w/=base.depth;}
    const float lod=std::min(t.max_lod,std::max(t.min_lod,lambda+t.bias));
    Vec4 c;
    if(!(lod>0))c=filter_level(t,0,u,v,t.mag==2,shadow_ref,w,face);
    else{
        const unsigned last=t.mip_count-1;
        switch(t.min){
        case 1:case 2:c=filter_level(t,0,u,v,t.min==2,shadow_ref,w,face);break;
        case 3:case 4:{const unsigned level=std::min(last,unsigned(std::lround(lod)));c=filter_level(t,level,u,v,t.min==4,shadow_ref,w,face);break;}
        default:{
            const unsigned l0=std::min(last,unsigned(std::floor(lod))),l1=std::min(last,l0+1);const float f=lod-std::floor(lod);
            const auto c0=filter_level(t,l0,u,v,t.min==6,shadow_ref,w,face),c1=filter_level(t,l1,u,v,t.min==6,shadow_ref,w,face);
            for(unsigned i=0;i<4;++i)c[i]=c0[i]*(1-f)+c1[i]*f;
            break;}
        }
    }
    // Remap in A,R,G,B order.
    const std::array<float,4> in{c[3],c[0],c[1],c[2]};std::array<float,4> out{};
    for(unsigned i=0;i<4;++i)out[i]=t.remap_control[i]==2?in[t.remap_channel[i]]:t.remap_control[i]==1?1.f:0.f;
    Vec4 result{out[1],out[2],out[3],out[0]};
    // RPCS3 _process_texel: filtered, remapped alpha, before gamma/BX2 and
    // instruction result modifiers. Retain killed lanes as derivative helpers.
    // This is RPCS3's epsilon comparison, not a claim of exact physical RSX.
    if(t.alpha_kill && result[3]<0.000001f){*killed=true;return result;}
    for(unsigned i=0;i<4;++i){
        const unsigned dst=(i+1)%4,src=t.remap_channel[dst];
        const bool input=t.remap_control[dst]==2;
        const bool snorm=input && (t.features&1) && (t.signed_mask&(1u<<src));
        if(!snorm && (t.features&2) && (t.gamma&(1u<<i)))result[i]=srgb_to_linear(result[i]);
        else if(!snorm && input && (t.features&4) && t.biased)result[i]=2*result[i]-1;
    }
    return result;
}

static Vec4 sample_vertex_texture(PpuState& s,unsigned unit,const Vec4& coord){
    const auto t=decode_texture(s,Reg{s.rsx},unit,true);
    if(t.format!=0x9b && t.format!=0x9c)throw std::runtime_error("unsupported RSX VP texture format");
    return sample_texture(t,coord[0],coord[1],coord[3],0,coord[2]);
}

'''
