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
        if(batch->used+64*1024>batch->capacity || batch->visibility_count+1>=1000 || batch->pipeline_pins.size()>=64 || retained+bytes>256*1024*1024)
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
            rtt_space(std::uint64_t(level.width)*level.height*16+surface.image->memory_bytes+(typeless_depth?std::uint64_t(surface.width)*surface.height*surface.samples*5:typeless_float?surface.image->memory_bytes:0));
            auto sampling_image=surface.image;std::shared_ptr<NativeVulkan::GraphicsImage> stencil_image;
            if(typeless_float){sampling_image=vk.graphics_aspect_image(*batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,view.sampling,&view.color_transfer);reason("float_typeless");}
            if(typeless_depth){sampling_image=vk.graphics_aspect_image(*batch,surface.image,VK_IMAGE_ASPECT_DEPTH_BIT,view.sampling,&view.color_transfer);stencil_image=vk.graphics_aspect_image(*batch,surface.image,VK_IMAGE_ASPECT_STENCIL_BIT,view.stencil,&view.stencil_transfer);reason("depth_typeless");}
            if(surface.image->format==VK_FORMAT_R8G8B8A8_SRGB){sampling_image=view.sampling?view.sampling:vk.graphics_image(*batch,VK_FORMAT_R8G8B8A8_UNORM,surface.width,surface.height,surface.samples);vk.graphics_copy(*batch,surface.image,sampling_image);reason("srgb_bytecopy");}
            auto image=view.image?view.image:vk.graphics_image(*batch,VK_FORMAT_R32G32B32A32_SFLOAT,level.width,level.height,1);
            if(!view.fs){
            std::string fs="#version 450\nlayout(std430,binding=0) readonly buffer Data{uint w[];};\nlayout(location=0) out vec4 color_;\nlayout(binding=17) uniform ";
            const bool packed=surface.image->format==VK_FORMAT_R32_UINT,integer_source=packed || typeless_depth || typeless_float;
            fs+=integer_source?"usampler2DArray src_;\n":"sampler2DArray src_;\n";
            if(typeless_depth)fs+="layout(binding=18) uniform usampler2DArray stencil_;\n";
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
            fs+="void main(){color_=raw_texel(0u,0u,0u,int(gl_FragCoord.x),int(gl_FragCoord.y),0);}\n";
            std::vector<unsigned> data(2588,0);data[0]=origin;data[1]=target.pitch;data[2]=target.bpp;data[3]=nx;data[4]=ny;data[5]=texture_pot(target.swizzle_width*(target.bpp/std::min(target.bpp,4u)));data[6]=texture_pot(target.swizzle_height);
            const unsigned d=2064;data[d+1]=1;data[d+11]=texture.features;data[d+12]=texture.signed_mask;data[d+27]=2;data[d+29]=texture.shadow;data[d+31]=2576;
            data[2576]=0;data[2577]=level.pitch;data[2578]=level.backing_width;data[2579]=level.backing_height;data[2582]=texture.swizzled;data[2583]=1;
            view.fs=std::make_shared<const std::string>(std::move(fs));view.data=std::move(data);
            }
            static const auto vs=std::make_shared<const std::string>("#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2.0-1.0,0,1);}");
            NativeVulkan::GraphicsState fixed{};fixed.width=level.width;fixed.height=level.height;fixed.colors=1;fixed.samples=1;fixed.color=VK_FORMAT_R32G32B32A32_SFLOAT;
            fixed.scissor={{0,0},{level.width,level.height}};fixed.blend[0].colorWriteMask=15;
            std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> colors{};colors[0]=image;
            std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> inputs{};inputs[16]=sampling_image;inputs[17]=stencil_image;
            const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{0,0},{0,0},{0,0}}};
            vk.graphics_draw(*batch,fixed,colors,{},*vs,*view.fs,view.data,3,positions,0,0,0,1,inputs,0,UINT64_MAX,{},vs,view.fs);
            view.plan=texture.plan;view.source=surface.image;view.image=image;view.generation=surface.generation;view.last_use=++resource_clock;
            view.format=texture.format;view.signed_mask=texture.signed_mask;view.features=texture.features;view.shadow=texture.shadow;
            view.layout=surface.readback;view.origin=origin;view.sampling=std::move(sampling_image);view.stencil=std::move(stencil_image);
            if(!replacing){
                auto cached_bytes=[&]{std::uint64_t total=0;for(const auto& e:rtt_views)total+=e.bytes();return total;};
                while(!rtt_views.empty() && (rtt_views.size()>=64 || cached_bytes()+view.bytes()>128*1024*1024)){
                    auto old=std::min_element(rtt_views.begin(),rtt_views.end(),[](const auto& a,const auto& b){return a.last_use<b.last_use;});rtt_views.erase(old);}
                if(view.bytes()<=128*1024*1024)rtt_views.push_back(std::move(view));
            }
            reason("conversion");if(fallback_stats().enabled){++fallback_stats().paths[target.tile?"rtt_tiled":"rtt_linear"];if(surface.samples>1)++fallback_stats().paths["rtt_sample_expansion"];if(!expected || surface.format!=expected)++fallback_stats().paths["rtt_reinterpret"];}
            return image;
        }
        reason(owner_found?"unsupported_view":"no_owner");reason("miss");return {};
    }
'''
