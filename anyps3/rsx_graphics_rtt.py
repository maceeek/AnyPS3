"""Validated render-target byte layouts converted to sampled images on the GPU."""
from .rsx_graphics_raw_texture import GLSL as RAW_GLSL
MEMBERS = r'''
    struct RttView {
        std::shared_ptr<TexturePlan> plan;
        std::shared_ptr<NativeVulkan::GraphicsImage> source,image;
        std::uint64_t generation,last_use;
        unsigned format,signed_mask,features,shadow;
        std::shared_ptr<Surface::ReadbackPlan> layout;
        unsigned origin=0;
        std::shared_ptr<NativeVulkan::GraphicsImage> sampling,stencil;
        std::shared_ptr<NativeVulkan::GraphicsBuffer> color_transfer,stencil_transfer;
        std::shared_ptr<const std::string> fs;
        std::vector<unsigned> data;
        std::uint64_t bytes()const{
            return (image?image->memory_bytes:0)+(sampling && sampling!=source?sampling->memory_bytes:0)+(stencil?stencil->memory_bytes:0)+(color_transfer?color_transfer->memory_bytes:0)+(stencil_transfer?stencil_transfer->memory_bytes:0);
        }
    };
    std::vector<RttView> rtt_views;
    // Immutable layout proofs survive GPU writes, but never a mapping/plan
    // replacement. Keep exact shared identities, not unverified hashes.
    using RttProofKey=std::array<std::uintptr_t,3>;
    struct RttProofHash {std::size_t operator()(const RttProofKey& key)const{std::size_t h=0;for(auto v:key)h=(h*16777619u)^v;return h;}};
    struct RttProof {std::weak_ptr<TexturePlan> texture;std::weak_ptr<Surface::ReadbackPlan> surface;std::uint64_t used;};
    // A proof owns neither layout. The metadata/surface caches already bound
    // their real storage; counting it again per proof caused repeated eviction
    // and revalidation of the same large tiled layout every frame.
    std::unordered_map<RttProofKey,RttProof,RttProofHash> rtt_proofs;
    // rtt_image's row-coverage check is a pure function of seven integers (layout of the texture rows inside the
    // surface); the per-row loop ran for every draw sampling a render target. Memoized by value, exactly.
    using RttCoverKey=std::array<std::uint64_t,7>;
    struct RttCoverHash {std::size_t operator()(const RttCoverKey& key)const{std::size_t h=0;for(auto v:key)h=(h*1099511628211ull)^v;return h;}};
    std::unordered_map<RttCoverKey,bool,RttCoverHash> rtt_cover_memo;
    static bool rtt_visual_fast(){static const bool on=[]{const char* v=std::getenv("ANYPS3_RENDER_SCALE_RTT_FAST");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RENDER_SCALE_RTT_FAST");}
        return !v || !std::strcmp(v,"1");}();return on;}
    bool rtt_rows_covered(std::uint64_t origin,std::uint64_t pitch,std::uint64_t row_bytes,unsigned rows,std::uint64_t target_pitch,unsigned source_height,unsigned source_width){
        const RttCoverKey key{origin,pitch,row_bytes,rows,target_pitch,source_height,source_width};
        const auto found=rtt_cover_memo.find(key);if(found!=rtt_cover_memo.end())return found->second;
        bool covered=true;
        for(unsigned y=0;y<rows && covered;++y){const auto at=origin+std::uint64_t(y)*pitch;covered=at/target_pitch<source_height && at%target_pitch+row_bytes<=source_width;}
        if(rtt_cover_memo.size()>=8192)rtt_cover_memo.clear();
        rtt_cover_memo.emplace(key,covered);return covered;
    }
    bool rtt_proved(const std::shared_ptr<TexturePlan>& texture,const std::shared_ptr<Surface::ReadbackPlan>& surface,unsigned origin){
        const RttProofKey key{reinterpret_cast<std::uintptr_t>(texture.get()),reinterpret_cast<std::uintptr_t>(surface.get()),origin};
        auto found=rtt_proofs.find(key);if(found==rtt_proofs.end())return false;
        if(found->second.texture.lock()!=texture || found->second.surface.lock()!=surface){rtt_proofs.erase(found);return false;}
        found->second.used=++resource_clock;return true;
    }
    void remember_rtt_proof(const std::shared_ptr<TexturePlan>& texture,const std::shared_ptr<Surface::ReadbackPlan>& surface,unsigned origin){
        const RttProofKey key{reinterpret_cast<std::uintptr_t>(texture.get()),reinterpret_cast<std::uintptr_t>(surface.get()),origin};
        if(rtt_proofs.find(key)==rtt_proofs.end() && rtt_proofs.size()>=256){
            auto old=std::min_element(rtt_proofs.begin(),rtt_proofs.end(),[](const auto& a,const auto& c){
                return std::make_pair(!a.second.texture.expired() && !a.second.surface.expired(),a.second.used)<
                    std::make_pair(!c.second.texture.expired() && !c.second.surface.expired(),c.second.used);});rtt_proofs.erase(old);}
        rtt_proofs.insert_or_assign(key,RttProof{texture,surface,++resource_clock});
    }
    void rtt_space(std::uint64_t bytes){
        const auto retained=batch->pins.bytes;
        const auto image_budget=256*1024*1024ull*(vk.graphics_render_scale==1?1:1+vk.graphics_render_scale*vk.graphics_render_scale);
        if(batch->used+64*1024>batch->capacity || batch->visibility_count+1>=1000 || batch->pipeline_pins.size()>=64 || retained+bytes>image_budget)
            submit_async("rtt_capacity");
    }
    static const std::uint8_t* mapped_pointer(const GuestMemory::Range& mapping,unsigned offset,unsigned& available){
        for(const auto& part:mapping.parts)if(offset>=part.offset && offset-part.offset<part.size){available=part.size-(offset-part.offset);return part.data+offset-part.offset;}
        available=0;return nullptr;
    }
    std::shared_ptr<NativeVulkan::GraphicsImage> rtt_image(const Texture& texture,unsigned offset,unsigned location){
        auto reason=[&](const char* name){if(fallback_stats().enabled)++fallback_stats().paths[std::string("rtt_")+name];};
        if(!allow_resident_textures || !texture.plan || texture.levels.size()!=1 || texture.dimension!=2 || texture.cube || texture.levels[0].border){reason("unsupported_shape");return {};}
        const auto& level=texture.levels[0];bool owner_found=false;
        auto reject=[&](const char* name,const Surface& surface,std::uint64_t origin){
            reason(name);if(!fallback_stats().enabled)return;
            std::ostringstream detail;detail<<"cause="<<name<<" texture={format:"<<texture.format<<",width:"<<level.width<<",height:"<<level.height<<",pitch:"<<level.pitch<<",swizzle:"<<texture.swizzled<<",offset:"<<offset<<"} surface={format:"<<surface.format<<",width:"<<surface.width<<",height:"<<surface.height<<",samples:"<<surface.samples<<",pitch:"<<surface.target.pitch<<",tile:"<<bool(surface.target.tile)<<",swizzle:"<<surface.target.swizzle_width<<",origin:"<<origin<<"}";
            fallback_stats().detail("rtt_view",detail.str());
        };
        for(auto& surface:surfaces){const auto& target=surface.target;
            if(surface.partial)continue;
            if(!target.mapping || !target.bpp || texture.plan->runs.empty() || !surface.overlaps(texture.plan->runs.front().data,1))continue;
            // Prove physical byte identity, including split/alias mappings.
            // Linear surfaces can be found through another effective address.
            std::int64_t origin=-1;
            if(target.tile){
                const auto& tile=*target.tile;
                if(tile.location!=location || offset<target.offset || offset>=std::uint64_t(tile.offset)+tile.size)continue;
                origin=offset-target.offset;
            }else if(!texture.plan->runs.empty())for(const auto& part:target.mapping->parts){
                const auto p=reinterpret_cast<std::uintptr_t>(texture.plan->runs[0].data),a=reinterpret_cast<std::uintptr_t>(part.data);
                if(p>=a && p-a<part.size){origin=part.offset+(p-a)-(target.offset-target.base_offset);break;}
            }
            if(origin<0)continue;
            owner_found=true;
            if(target.swizzle_width && (surface.width*(surface.samples>1?2:1)!=target.swizzle_width || surface.height*(surface.samples==4?2:1)!=target.swizzle_height)){reject("swizzle_extent",surface,origin);continue;}
            const unsigned nx=surface.samples>1?2:1,ny=surface.samples==4?2:1;
            const unsigned pixel_bytes=texture.format==0x9b?16:texture.format==0x9a?8:
                texture.format==0x81?1:(texture.format==0x85 || texture.format==0x90 || texture.format==0x91 || texture.format==0x95 || texture.format==0x9c || texture.format==0x9e || texture.format==0x9f)?4:2;
            // BC blocks retain their LE byte stream; the existing raw GPU decoder
            // consumes it directly from the authoritative render target.
            const bool block=texture.format>=0x86 && texture.format<=0x88;
            if(texture.format==0x8d || texture.format==0x8e){reject("block_format",surface,origin);continue;}
            const bool typeless_float=!surface.depth && surface.image->format!=VK_FORMAT_R32_UINT && ((surface.format==11 && texture.format!=0x9a) || (surface.format==12 && texture.format!=0x9b) || (surface.format==13 && texture.format!=0x9c));
            const bool typeless_depth=surface.depth && surface.format==2 && texture.format==0x85 && vk.graphics_format(VK_FORMAT_R32_UINT) && vk.graphics_format(VK_FORMAT_R8_UINT);
            if(surface.depth && !typeless_depth && (surface.format==1?texture.format!=0x92:texture.format!=0x90)){reject("depth_reinterpret",surface,origin);continue;}
            const std::uint64_t row_bytes=block?std::uint64_t((level.width+3)/4)*(texture.format==0x86?8:16):std::uint64_t(level.width)*pixel_bytes;
            const unsigned rows=block?(level.height+3)/4:level.height;
            bool covered=true;
            const unsigned source_width=surface.width*nx*target.bpp,source_height=surface.height*ny;
            const bool contiguous=(!block && texture.swizzled) || target.swizzle_width;
            if(contiguous){
                if(target.swizzle_width)covered=std::uint64_t(origin)+texture.plan->bytes<=std::uint64_t(target.swizzle_width)*target.swizzle_height*target.bpp;
                else{const auto last=std::uint64_t(origin)+texture.plan->bytes;covered=last<=std::uint64_t(target.pitch)*source_height && (source_width==target.pitch || std::uint64_t(origin)/target.pitch==(last-1)/target.pitch) && (source_width==target.pitch || last%target.pitch<=source_width);}
            }else covered=rtt_rows_covered(std::uint64_t(origin),level.pitch,row_bytes,rows,target.pitch,source_height,source_width);
            if(!covered){reject("pitch_or_extent",surface,origin);continue;}
            std::size_t reuse=rtt_views.size();
            for(std::size_t i=0;i<rtt_views.size();++i){auto& view=rtt_views[i];
                const auto& old=view.source;const auto& current=surface.image;
                const bool compatible=old && old->format==current->format && old->width==current->width && old->height==current->height && old->layers==current->layers && old->depth==current->depth;
                if(view.plan==texture.plan && compatible && view.layout==surface.readback && view.origin==unsigned(origin) && view.format==texture.format && view.signed_mask==texture.signed_mask && view.features==texture.features && view.shadow==texture.shadow){
                    if(view.source==surface.image && view.generation==surface.generation){view.last_use=++resource_clock;reason("conversion_hit");return view.image;}
                    reuse=i;break;}}
            // Command order + graphics barriers preserve old users, including
            // batches in flight. Rewrite the same image only after those reads.
            // Storage follows immutable layout/format; freshness still requires
            // exact source-image identity AND generation. Dropping the previous
            // source owner here also lets completed surface pools reuse it.
            const bool replacing=reuse<rtt_views.size();RttView fresh{};
            auto& view=replacing?rtt_views[reuse]:fresh;
            if(replacing)reason("storage_reuse");
            // Only sampled bytes matter; padding is never read by the GPU.
            const bool proved=rtt_proved(texture.plan,surface.readback,unsigned(origin));
            if(proved)reason("layout_proof_hit");else reason("layout_proof_miss");
            bool matches=true;
            auto run=texture.plan->runs.begin();
            for(unsigned y=0;!proved && y<(contiguous?1:rows) && matches;++y)for(std::uint64_t x=0;x<(contiguous?texture.plan->bytes:row_bytes);){
                const auto logical=std::uint64_t(y)*level.pitch+x;
                while(run!=texture.plan->runs.end() && run->offset+run->size<=logical)++run;
                if(run==texture.plan->runs.end() || run->offset>logical){matches=false;break;}
                const auto source_linear=std::uint64_t(origin)+logical;
                const unsigned at=target.swizzle_width?source_linear:target.address((source_linear%target.pitch)/target.bpp,source_linear/target.pitch,source_linear%target.bpp);
                unsigned available=0;const auto* physical=mapped_pointer(*target.mapping,at,available);
                unsigned n=std::min<std::uint64_t>((contiguous?texture.plan->bytes:row_bytes)-x,run->size-(logical-run->offset));
                if(target.tile)n=std::min<unsigned>(n,32-(source_linear+target.offset)%32);
                n=std::min(n,available);
                if(!n || physical!=run->data+(logical-run->offset)){matches=false;break;}x+=n;
            }
            if(!matches){reject("physical_layout",surface,origin);continue;}
            if(!proved)remember_rtt_proof(texture.plan,surface.readback,unsigned(origin));
            // A full compatible single-sample view requires no GPU copy.
            const unsigned expected=texture.format==0x85?8:texture.format==0x84?3:texture.format==0x9a?11:texture.format==0x9b?12:0;
            if(!surface.depth && !(texture.features&1 && texture.signed_mask) && surface.samples==1 && origin==0 && level.width==surface.width && level.height==surface.height && surface.format==expected && surface.image->format==color_format(expected) && texture.swizzled==bool(target.swizzle_width) && level.pitch==target.pitch){
                reason("direct");return surface.image;
            }
            rtt_space(std::uint64_t(level.width)*level.height*16+surface.image->memory_bytes+(typeless_depth?std::uint64_t(surface.width)*surface.height*surface.samples*5*(1+vk.graphics_render_scale*vk.graphics_render_scale):typeless_float?surface.image->memory_bytes:0));
            // Trace mode labels reconstructions with the conversion kind (interned, bounded).
            const char* rtt_label="rtt";
            if(NativeVulkan::graphics_visual_trace()){static std::set<std::string> labels;
                const auto label="rtt:tex"+std::to_string(level.width)+"x"+std::to_string(level.height)+"f"+std::to_string(texture.format)+(texture.swizzled?"sw":"")+
                    ":surf"+std::to_string(surface.width)+"x"+std::to_string(surface.height)+"f"+std::to_string(surface.format)+"s"+std::to_string(surface.samples)+(target.swizzle_width?"sw":"");
                if(labels.size()<128 || labels.count(label))rtt_label=labels.insert(label).first->c_str();}
            NativeVulkan::VisualWhy why(vk,rtt_label);
            auto sampling_image=surface.image;std::shared_ptr<NativeVulkan::GraphicsImage> stencil_image;
            if(typeless_float){sampling_image=vk.graphics_aspect_image(*batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,view.sampling,&view.color_transfer);reason("float_typeless");}
            if(typeless_depth){sampling_image=vk.graphics_aspect_image(*batch,surface.image,VK_IMAGE_ASPECT_DEPTH_BIT,view.sampling,&view.color_transfer);stencil_image=vk.graphics_aspect_image(*batch,surface.image,VK_IMAGE_ASPECT_STENCIL_BIT,view.stencil,&view.stencil_transfer);reason("depth_typeless");}
            if(surface.image->format==VK_FORMAT_R8G8B8A8_SRGB){sampling_image=view.sampling?view.sampling:vk.graphics_image(*batch,VK_FORMAT_R8G8B8A8_UNORM,surface.width,surface.height,surface.samples);vk.graphics_copy(*batch,surface.image,sampling_image);reason("srgb_bytecopy");}
            // A visual texel must correspond to one complete source texel.
            // Byte-shifted aliases retain the exact native decoder below.
            // Typeless depth samples word images whose companions graphics_aspect_image
            // reinterpreted from the visual depth/stencil (one word per texel).
            const bool visual_depth=typeless_depth && sampling_image->visual && stencil_image->visual && surface.image->visual;
            // A swizzled texture of a swizzled single-sample surface with the same extent at
            // origin 0 addresses the same Morton order: texel (x,y) is surface pixel (x,y), so
            // destination subtexels map spatially like the linear case.
            const bool swizzle_identity=target.swizzle_width && texture.swizzled && origin==0 && surface.samples==1 &&
                level.width==surface.width && level.height==surface.height && target.swizzle_width==surface.width && target.swizzle_height==surface.height;
            // B8 render targets are stored packed (one guest byte per R32_UINT texel); a B8 texture reads that byte.
            const bool packed_b8=!surface.depth && surface.format==9 && texture.format==0x81 && surface.image->format==VK_FORMAT_R32_UINT;
            const bool visual_typed=!block && !typeless_float && (!typeless_depth || visual_depth) && (swizzle_identity || (!target.swizzle_width && !texture.swizzled)) && pixel_bytes==target.bpp &&
                origin%target.bpp==0 && target.pitch%target.bpp==0 && level.pitch%target.bpp==0 &&
                ((surface.depth && ((surface.format==1 && texture.format==0x92) || (surface.format==2 && (texture.format==0x90 || visual_depth)))) ||
                 (!surface.depth && expected && expected==surface.format) || packed_b8);
            // Visual fast path (display-only half; the native half below is unchanged): a linear A8R8G8B8 (0x85) read of an
            // A8R8G8B8 colour target or of a typeless D24S8 target with the same pitch, without SNORM, is per texel the
            // visual source texel at sample-grid position (x+ox, y+oy) (raw_byte's address math reduces to that because
            // `covered` keeps every row inside one target row and origin is texel aligned). Its 8-bit channels are k/255,
            // so an R8G8B8A8_UNORM visual output holds them exactly (the decoder's output was R32G32B32A32_SFLOAT).
            // ANYPS3_RENDER_SCALE_RTT_FAST=0 keeps the generic decoder for the visual half too.
            const bool fast_visual=visual_typed && rtt_visual_fast() && vk.graphics_render_scale>1 && texture.format==0x85 && !texture.swizzled && !target.swizzle_width &&
                level.pitch==target.pitch && target.bpp==4 && !(texture.features&1) && origin>=0 && unsigned(origin)%4==0 &&
                ((!surface.depth && surface.format==8 && sampling_image->format==VK_FORMAT_R8G8B8A8_UNORM && sampling_image->visual) || (typeless_depth && visual_depth));
            const VkFormat visual_format=fast_visual?VK_FORMAT_R8G8B8A8_UNORM:VK_FORMAT_R32G32B32A32_SFLOAT;
            auto image=view.image;
            if(image && image->visual && image->visual->format!=visual_format)image.reset();
            if(!image)image=vk.graphics_image(*batch,VK_FORMAT_R32G32B32A32_SFLOAT,level.width,level.height,1,false,true,visual_format);
            if(NativeVulkan::graphics_visual_trace() && vk.graphics_render_scale>1){
                static std::vector<std::array<unsigned,12>> seen;
                const std::array<unsigned,12> key{texture.format,level.width,level.height,level.pitch,surface.format,surface.width,surface.height,surface.samples,target.pitch,unsigned(bool(target.tile)),unsigned(visual_typed),unsigned(typeless_float)|unsigned(typeless_depth)<<1|unsigned(block)<<2|unsigned(texture.swizzled)<<3};
                if(seen.size()<256 && std::find(seen.begin(),seen.end(),key)==seen.end()){seen.push_back(key);
                    std::fprintf(stderr,"RENDER_SCALE_RTT texture=%ux%u fmt=0x%x pitch=%u swizzled=%d origin=%lld surface=%ux%u fmt=%u samples=%u pitch=%u swizzle=%u tile=%d depth=%d typeless_float=%d typeless_depth=%d block=%d visual_typed=%d\n",
                        level.width,level.height,texture.format,level.pitch,int(texture.swizzled),(long long)origin,surface.width,surface.height,surface.format,surface.samples,target.pitch,target.swizzle_width,int(bool(target.tile)),int(surface.depth),int(typeless_float),int(typeless_depth),int(block),int(visual_typed));}}
            if(!view.fs){
            std::string fs="#version 450\nlayout(std430,binding=0) readonly buffer Data{uint w[];};\nlayout(location=0) out vec4 color_;\nlayout(binding=17) uniform ";
            const bool packed=surface.image->format==VK_FORMAT_R32_UINT,integer_source=packed || typeless_depth || typeless_float;
            fs+=integer_source?"usampler2DArray src_;\n":"sampler2DArray src_;\n";
            if(typeless_depth)fs+="layout(binding=18) uniform usampler2DArray stencil_;\n";
            // Visual sample grid = the flattened grid the surface would have if
            // rendered at the visual extent: visual grid column G=X*nx+sample.
            // A native grid texel g covers visual grid columns g*S..g*S+S-1, so
            // G=(g+fract)*S, then pixel G/n and sample G%n (identity when n=1).
            // Mapping native sample cells to SxS same-plane blocks instead would
            // interleave planes out of spatial order (zig-zag after a resolve).
            if(visual_typed)fs+="layout(push_constant) uniform Visual{layout(offset=56) vec2 raster_scale_;};\nivec3 visual_xyl(uvec2 g){vec2 ratio=vec2(textureSize(src_,0).xy)/vec2("+std::to_string(surface.width)+","+std::to_string(surface.height)+");uvec2 n=uvec2(w[3],w[4]),G=uvec2((vec2(g)+fract(gl_FragCoord.xy/raster_scale_))*ratio);uvec2 c=G/n,l=G%n;return ivec3(c,int(l.y*n.x+l.x)); }\n";
            fs+="uint raw_format(uint unit){return "+rsxvk::u(texture.format)+";}\n";
            fs+="uint raw_byte(uint unit,uint at){uint a=at+w[0],y=a/w[1],x=(a%w[1])/w[2],byte_=a%w[2];";
            if(target.swizzle_width){fs+="uint block=min(w[2],4u),words=w[2]/block,index_=a/block;uint xx=0u,yy=0u,bit_=0u;for(uint i=0u;(w[5]>>i)>1u || (w[6]>>i)>1u;++i){if((w[5]>>i)>1u)xx|=((index_>>bit_++)&1u)<<i;if((w[6]>>i)>1u)yy|=((index_>>bit_++)&1u)<<i;}x=xx/words;y=yy;byte_=(xx%words)*block+a%block;";}
            fs+="uint layer=(y%w[4])*w[3]+x%w[3];";
            if(typeless_float)fs+="uvec4 c=texelFetch(src_,ivec3((x/w[3])*(w[2]/4u)+byte_/4u,y/w[4],layer),0);uint v=c.r;";
            else fs+=integer_source?"uvec4 c=texelFetch(src_,ivec3(x/w[3],y/w[4],layer),0);uint v=c.r;":"vec4 c=texelFetch(src_,ivec3(x/w[3],y/w[4],layer),0);uint v=0u;";
            if(typeless_float)fs+="byte_%=4u;return (v>>(8u*"+std::string(surface.format==11?"(byte_^1u)":"(3u-byte_)")+"))&255u;}\n";
            else if(typeless_depth)fs+="v=((v&0xffffffu)<<8u)|(texelFetch(stencil_,ivec3(x/w[3],y/w[4],layer),0).r&255u);";
            else if(surface.depth)fs+="v=uint(round(clamp(c.r,0,1)*"+std::string(surface.format==1?"65535.0":"16777215.0")+"));"+std::string(surface.format==2?"v<<=8u;":"");
            else if(!packed)switch(surface.format){
                case 8:fs+="uvec4 b=uvec4(round(clamp(c,0,1)*255.0));v=(b.a<<24u)|(b.r<<16u)|(b.g<<8u)|b.b;";break;
                case 3:fs+="uvec3 b=uvec3(round(clamp(c.rgb,0,1)*vec3(31,63,31)));v=(b.r<<11u)|(b.g<<5u)|b.b;";break;
                case 11:fs+="v=packHalf2x16(vec2(c[byte_/2u],0))&65535u;byte_%=2u;return (v>>(8u*(1u-byte_)))&255u;}\n";break;
                case 12:fs+="v=floatBitsToUint(c[byte_/4u]);byte_%=4u;return (v>>(8u*(3u-byte_)))&255u;}\n";break;
                case 13:fs+="v=floatBitsToUint(c.r);";break;
                default:reason("source_format");return {};
            }
            if(!typeless_float && (surface.depth || packed || (surface.format!=11 && surface.format!=12)))fs+="return (v>>(8u*(w[2]-1u-byte_)))&255u;}\n";
            fs+=R"RTT(
''' + RAW_GLSL + r'''
)RTT";
            if(visual_typed)replace_all(fs,"ivec3(x/w[3],y/w[4],layer)","visual_xyl(uvec2(x,y))");
            fs+="void main(){vec2 xy="+std::string(visual_typed?"gl_FragCoord.xy/raster_scale_":"gl_FragCoord.xy")+";color_=raw_texel(0u,0u,0u,int(xy.x),int(xy.y),0);}\n";
            std::vector<unsigned> data(2588,0);data[0]=origin;data[1]=target.pitch;data[2]=target.bpp;data[3]=nx;data[4]=ny;data[5]=texture_pot(target.swizzle_width*(target.bpp/std::min(target.bpp,4u)));data[6]=texture_pot(target.swizzle_height);
            const unsigned d=2064;data[d+1]=1;data[d+11]=texture.features;data[d+12]=texture.signed_mask;data[d+27]=2;data[d+29]=texture.shadow;data[d+31]=2576;
            data[2576]=0;data[2577]=level.pitch;data[2578]=level.backing_width;data[2579]=level.backing_height;data[2582]=texture.swizzled;data[2583]=1;
            view.fs=std::make_shared<const std::string>(std::move(fs));view.data=std::move(data);
            }
            static const auto vs=std::make_shared<const std::string>("#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2.0-1.0,0,1);}");
            NativeVulkan::GraphicsState fixed{};fixed.scale_visual=visual_typed && !fast_visual;fixed.visual_external=fast_visual;fixed.width=level.width;fixed.height=level.height;fixed.colors=1;fixed.samples=1;fixed.color=VK_FORMAT_R32G32B32A32_SFLOAT;
            fixed.scissor={{0,0},{level.width,level.height}};fixed.blend[0].colorWriteMask=15;
            std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> colors{};colors[0]=image;
            std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> inputs{};inputs[16]=sampling_image;inputs[17]=stencil_image;
            const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{0,0},{0,0},{0,0}}};
            vk.graphics_draw(*batch,fixed,colors,{},*vs,*view.fs,view.data,3,positions,0,0,0,1,inputs,0,UINT64_MAX,{},vs,view.fs);
            if(fast_visual){
                // Same visual_xyl mapping as the generic visual decoder (surface extent from w[9..10]); the colour case is
                // the decoder's round(clamp(c)*255)/255, the depth case its d24<<8|stencil word split into A8R8G8B8 bytes.
                static const std::string head="#version 450\nlayout(std430,binding=0) readonly buffer Data{uint w[];};\nlayout(location=0) out vec4 color_;\nlayout(push_constant) uniform Visual{layout(offset=56) vec2 raster_scale_;};\n";
                static const std::string xyl="ivec3 visual_xyl(uvec2 g){vec2 ratio=vec2(textureSize(src_,0).xy)/vec2(w[9],w[10]);uvec2 n=uvec2(w[3],w[4]),G=uvec2((vec2(g)+fract(gl_FragCoord.xy/raster_scale_))*ratio);uvec2 c=G/n,l=G%n;return ivec3(c,int(l.y*n.x+l.x));}\n"
                    "ivec3 at_(){vec2 xy=gl_FragCoord.xy/raster_scale_;return visual_xyl(uvec2(uint(int(xy.x)),uint(int(xy.y)))+uvec2(w[5],w[6]));}\n";
                static const auto fs_color=std::make_shared<const std::string>(head+"layout(binding=17) uniform sampler2DArray src_;\n"+xyl+
                    "void main(){color_=vec4(uvec4(round(clamp(texelFetch(src_,at_(),0),0,1)*255.0)))/255.0;}\n");
                static const auto fs_depth=std::make_shared<const std::string>(head+"layout(binding=17) uniform usampler2DArray src_;\nlayout(binding=18) uniform usampler2DArray stencil_;\n"+xyl+
                    "void main(){ivec3 p=at_();uint v=((texelFetch(src_,p,0).r&0xffffffu)<<8u)|(texelFetch(stencil_,p,0).r&255u);color_=vec4(uvec4((v>>16u)&255u,(v>>8u)&255u,v&255u,v>>24u))/255.0;}\n");
                std::vector<unsigned> vdata(16,0);vdata[3]=nx;vdata[4]=ny;vdata[5]=(unsigned(origin)%target.pitch)/4;vdata[6]=unsigned(origin)/target.pitch;vdata[9]=surface.width;vdata[10]=surface.height;
                const auto scale=vk.graphics_render_scale;
                NativeVulkan::GraphicsState v{};v.visual_pass=true;v.width=level.width;v.height=level.height;v.raster_width=level.width*scale;v.raster_height=level.height*scale;
                v.colors=1;v.samples=1;v.color=VK_FORMAT_R8G8B8A8_UNORM;v.scissor={{0,0},{level.width,level.height}};v.blend[0].colorWriteMask=15;
                std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> visual_colors{};visual_colors[0]=image->visual;
                std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> visual_inputs{};visual_inputs[16]=sampling_image->visual;if(typeless_depth)visual_inputs[17]=stencil_image->visual;
                const auto& fs=typeless_depth?fs_depth:fs_color;
                vk.graphics_draw(*batch,v,visual_colors,{},*vs,*fs,vdata,3,positions,0,0,0,1,visual_inputs,0,UINT64_MAX,{},vs,fs);
                ++vk.graphics_visual_rtt_fast;
            }
            view.plan=texture.plan;view.source=surface.image;view.image=image;view.generation=surface.generation;view.last_use=++resource_clock;
            view.format=texture.format;view.signed_mask=texture.signed_mask;view.features=texture.features;view.shadow=texture.shadow;
            view.layout=surface.readback;view.origin=origin;view.sampling=std::move(sampling_image);view.stencil=std::move(stencil_image);
            if(!replacing){
                auto cached_bytes=[&]{std::uint64_t total=0;for(const auto& e:rtt_views)total+=e.bytes();return total;};
                const auto budget=128*1024*1024ull*(vk.graphics_render_scale==1?1:1+vk.graphics_render_scale*vk.graphics_render_scale);
                while(!rtt_views.empty() && (rtt_views.size()>=64 || cached_bytes()+view.bytes()>budget)){
                    auto old=std::min_element(rtt_views.begin(),rtt_views.end(),[](const auto& a,const auto& b){return a.last_use<b.last_use;});rtt_views.erase(old);}
                if(view.bytes()<=budget)rtt_views.push_back(std::move(view));
            }
            reason("conversion");if(fallback_stats().enabled){++fallback_stats().paths[target.tile?"rtt_tiled":"rtt_linear"];if(surface.samples>1)++fallback_stats().paths["rtt_sample_expansion"];if(!expected || surface.format!=expected)++fallback_stats().paths["rtt_reinterpret"];}
            return image;
        }
        reason(owner_found?"unsupported_view":"no_owner");reason("miss");return {};
    }
'''
