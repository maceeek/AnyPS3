"""RSX graphics preparation, image ownership and differential-testable codecs.

The synchronous draw entry point commits before returning. FIFO images can
remain resident across CPU/SPU turns, protected by access observers and host
page guards for retained raw pointers. No guest bytes are substituted.
"""
from .rsx_texture_cache import MEMBERS as TEXTURE_CACHE_MEMBERS
from .rsx_access_trace import CPP as ACCESS_TRACE_CPP
from .rsx_graphics_sampled import MEMBERS as SAMPLED_MEMBERS
from .rsx_graphics_rtt import MEMBERS as RTT_MEMBERS
from .rsx_graphics_rebind import MEMBERS as REBIND_MEMBERS
from .rsx_graphics_visual_history import MEMBERS as VISUAL_HISTORY_MEMBERS
from .rsx_graphics_conditional import MEMBERS as CONDITIONAL_MEMBERS
from .rsx_graphics_fp import CPP as SCALAR_FP_CPP
from .rsx_graphics_alias import CPP as ALIAS_CPP, HELPERS as ALIAS_HELPERS, RASTER as ALIAS_RASTER
from .rsx_graphics_codec import CPP as CODEC_CPP
from .rsx_fallback import CPP as FALLBACK_CPP
from .rsx_graphics_texture import GLSL as TEXTURE_GLSL
from .rsx_graphics_fixed import GLSL as FIXED_GLSL

CPP = r'''
#ifdef ANYPS3_RSX_VK
namespace rsxgraphics {
using namespace rsxdraw;
''' + FALLBACK_CPP + ACCESS_TRACE_CPP + r'''
static void replace_all(std::string& s,const std::string& a,const std::string& b){
    std::size_t p=0;while((p=s.find(a,p))!=std::string::npos){s.replace(p,a.size(),b);p+=b.size();}
}
''' + SCALAR_FP_CPP + '\nstatic const char* texture_glsl=R"RSXVK(' + TEXTURE_GLSL + ')RSXVK";\n' + r'''
''' + '\nstatic const char* alias_helpers=R"RSXVK(' + ALIAS_HELPERS + ')RSXVK";\nstatic const char* alias_raster=R"RSXVK(' + ALIAS_RASTER + ')RSXVK";\n' + r'''
''' + '\nstatic const char* fixed_glsl=R"RSXVK(' + FIXED_GLSL + ')RSXVK";\n' + r'''
struct SourceKeyHash {
    std::size_t operator()(const std::string& key)const{return std::hash<std::string>{}(key);}
    // Four independent multiply chains (the serial chain was ~4 cycles per word on per-draw keys); equality is still exact.
    std::size_t operator()(const std::vector<unsigned>& key)const{
        std::uint64_t a=0x9e3779b97f4a7c15u,b=0xc2b2ae3d27d4eb4fu,c=0x165667b19e3779f9u,d=0x27d4eb2f165667c5u;
        const auto* p=key.data();std::size_t n=key.size(),i=0;
        for(;i+4<=n;i+=4){a=(a^p[i])*0x100000001b3u;b=(b^p[i+1])*0x100000001b3u;c=(c^p[i+2])*0x100000001b3u;d=(d^p[i+3])*0x100000001b3u;}
        for(;i<n;++i)a=(a^p[i])*0x100000001b3u;
        std::uint64_t h=a^(b>>1|b<<63)^(c>>2|c<<62)^(d>>3|d<<61)^(std::uint64_t(n)*0xff51afd7ed558ccdu);
        h^=h>>33;h*=0xc4ceb9fe1a85ec53u;h^=h>>29;return std::size_t(h);
    }
};
static unsigned source_cache_limit(const char* name,unsigned fallback,unsigned maximum){
    const char* text=std::getenv(name);if(!text || !*text)return fallback;
    char* end=nullptr;const auto value=std::strtoul(text,&end,10);
    if(*text=='-' || *end || !value || value>maximum)throw std::runtime_error(std::string("invalid ")+name);
    return unsigned(value);
}
template<class Key> struct SourceCache {
    struct Entry{std::shared_ptr<const std::string> source;std::uint64_t used;};
    std::unordered_map<Key,Entry,SourceKeyHash> entries;std::uint64_t clock=0,bytes=0;
    const unsigned entry_limit=source_cache_limit("ANYPS3_RSX_SOURCE_CACHE_ENTRIES",1024,4096);
    const std::uint64_t byte_limit=std::uint64_t(source_cache_limit("ANYPS3_RSX_SOURCE_CACHE_MB",64,256))<<20;
    std::shared_ptr<const std::string> find(const Key& key){auto it=entries.find(key);if(it==entries.end())return {};it->second.used=++clock;return it->second.source;}
    std::shared_ptr<const std::string> put(Key key,std::string text){
        auto value=std::make_shared<const std::string>(std::move(text));
        if(value->size()>byte_limit)return value;
        while(!entries.empty() && (entries.size()>=entry_limit || bytes+value->size()>byte_limit)){
            auto old=std::min_element(entries.begin(),entries.end(),[](const auto& a,const auto& b){return a.second.used<b.second.used;});bytes-=old->second.source->size();entries.erase(old);::RsxPerfCounters::add(::RsxPerfCounters::src_evictions);}
        bytes+=value->size();entries.emplace(std::move(key),Entry{value,++clock});return value;
    }
};
// Binding 37 addresses the batch arena directly. Per-draw offsets select
// immutable blocks; later register/constant writes cannot alter queued draws.
static const char* state_glsl=R"GLSL(
layout(std430,binding=37) readonly buffer StateData{uint state_[];};
uint R(uint a){uint i=a/4u;return state_[w[16u+i/64u]+i%64u];}
vec4 state4(uint a){return uintBitsToFloat(uvec4(state_[a],state_[a+1u],state_[a+2u],state_[a+3u]));}
vec4 cstate4(uint i){return state4(state_[w[2]+(i>>7u)]+(i&127u));}
)GLSL";
static bool packed_color(const Reg& r){const unsigned f=r(0x208)&31;return r(0x220)!=0 && (f==13 || (!float_color(f) && ((f!=8 && f!=3) || (r(0x374)&1) || (f==3 && r(0x1fec)))));}
// Native implicit sampling obtains filtering and mip count from per-draw
// descriptors/samplers. Manual TXL/TXD and unsupported sampler layouts still
// specialize their tap paths. Do not compile dead helpers for sampler changes.
static bool implicit_sampler(const Texture& t){
    auto wrap=[](unsigned v){return v>=1 && v<=5 && v!=4;};
    return t.valid && !t.resident_image && !t.cube && !t.shadow && !t.alpha_kill &&
        !(t.dimension==3 && t.levels[0].depth==1) && wrap(t.wrap_s) && wrap(t.wrap_t) && (t.dimension!=3 || wrap(t.wrap_r));
}
static bool dynamic_resident_sampler(){
    static const bool enabled=[](){const char* value=std::getenv("ANYPS3_RSX_RESIDENT_SAMPLER_DYNAMIC");
        if(value && std::strcmp(value,"0") && std::strcmp(value,"1"))throw std::runtime_error("invalid ANYPS3_RSX_RESIDENT_SAMPLER_DYNAMIC");
        return !value || !std::strcmp(value,"1");}();
    return enabled;
}
static unsigned explicit_sampler_mask(const std::vector<FpInst>& fp){
    unsigned mask=0;for(const auto& i:fp){const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6);
        if(op==25 || op==47)mask|=1u<<((i.d>>17)&15);}
    return mask;
}
// Register-driven fragment discards of the Vulkan fragment epilogue. A bit is clear only when the draw's registers make
// that discard impossible for every fragment: window clip (w[8], one bit per present 0x2c0/0x2c4 pair), polygon stipple
// (0x147c==0), alpha test (0x304 bit 0 clear), multisample control (0x1d7c: sample-mask bits 16..19 for planes 0..3 all
// set and alpha-to-coverage bit 4 clear; reset value 0xffff0000). ANYPS3_RSX_FS_STATIC_DISCARD=0 keeps every test in the
// shader (run-time evaluation, the previous text).
// ANYPS3_RSX_LAZY_VISIBILITY=1: draws with ZPASS counting off (0x17cc==0) record no occlusion query (see the main draw).
// Default 0 keeps a query per sample pass of every draw (the host statistic draw.pixels counts all draws).
// ANYPS3_RSX_GEOMETRY_REUSE (default 1; 0 = every draw reads its vertex inputs from guest memory): Session::read_inputs.
static bool rsx_geometry_reuse(){
    static const bool on=[]{const char* v=std::getenv("ANYPS3_RSX_GEOMETRY_REUSE");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_GEOMETRY_REUSE");}
        return !v || !std::strcmp(v,"1");}();
    return on;
}
static bool rsx_lazy_visibility(){
    static const bool on=[]{const char* v=std::getenv("ANYPS3_RSX_LAZY_VISIBILITY");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_LAZY_VISIBILITY");}
        return v && !std::strcmp(v,"1");}();
    return on;
}
enum FragmentDiscard:unsigned {FragmentDiscardWindow=1,FragmentDiscardStipple=2,FragmentDiscardAlpha=4,FragmentDiscardCoverage=8,FragmentDiscardAll=15};
static bool fragment_static_discard(){
    static const bool on=[]{const char* v=std::getenv("ANYPS3_RSX_FS_STATIC_DISCARD");
        if(v && std::strcmp(v,"0") && std::strcmp(v,"1")){throw std::runtime_error("invalid ANYPS3_RSX_FS_STATIC_DISCARD");}
        return !v || !std::strcmp(v,"1");}();
    return on;
}
// width/height (when non-zero) are the draw's native raster extent: every fragment has pixel coordinates in
// [0,width)x[0,height) (raster_scale_ maps visual fragments back), so an inclusive window clip (0x2bc bit 0 clear) with
// one present window covering that whole extent never discards.
static unsigned fragment_discard_state(const Reg& r,unsigned width=0,unsigned height=0){
    if(!fragment_static_discard())return FragmentDiscardAll;
    unsigned state=0;bool windows=false,covered=false;
    for(unsigned i=0;i<8;++i)if(r.gpu.registers.count(0x2c0+8*i) && r.gpu.registers.count(0x2c4+8*i)){windows=true;
        const unsigned h=r(0x2c0+8*i),v=r(0x2c4+8*i);
        covered=covered || (width && height && (h&65535)==0 && (h>>16)>=width-1 && (v&65535)==0 && (v>>16)>=height-1);}
    if(windows && ((r(0x2bc)&1) || !covered))state|=FragmentDiscardWindow;
    if(r(0x147c)!=0)state|=FragmentDiscardStipple;
    if(r(0x304)&1)state|=FragmentDiscardAlpha;
    const unsigned aa=r.get(0x1d7c,0xffff0000);if(((aa>>16)&15)!=15 || (aa&16))state|=FragmentDiscardCoverage;
    return state;
}
static std::optional<std::string> fragment_source_uncached(const std::vector<FpInst>& fp,const Reg& r,unsigned count,const std::array<Texture,16>& textures,bool lines=false,rsxvk::FragmentFailure* failure=nullptr,unsigned discard_state=FragmentDiscardAll){
    HostPhase phase(HostStage::source);
    const bool packed=packed_color(r);const unsigned explicit_units=explicit_sampler_mask(fp);
    auto shade=scalar_fragment(fp,failure);if(!shade)return {};
    std::string source="#version 450\n";
    if(shade->find("subgroupQuadBroadcast")!=std::string::npos)source+="#extension GL_KHR_shader_subgroup_quad : require\n";
    if(lines)source+="#extension GL_ARB_gpu_shader_fp64 : require\n";
    if(rsx_layered_shaders().load(std::memory_order_relaxed))source+="layout(std430,binding=0) buffer Data {uint w[];};\nlayout(push_constant) uniform Params{vec2 extent;vec2 shift_push_;uint plane_push_;uint layers_;vec2 shifts_[4];layout(offset=56) vec2 raster_scale_;};\nlayout(location=0) in vec4 varying_[16];\nlayout(location=16) flat in uint primitive_;\nlayout(location=17) flat in uint plane_in_;\nvec2 shift;uint plane;\n";
    else source+="layout(std430,binding=0) buffer Data {uint w[];};\nlayout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane;layout(offset=56) vec2 raster_scale_;};\nlayout(location=0) in vec4 varying_[16];\nlayout(location=16) flat in uint primitive_;\n";
    for(unsigned i=0;i<count;++i){source+="layout(location="+std::to_string(i)+") out "+(packed?"uint":"vec4")+" out"+std::to_string(i)+";\n";
        if(packed)source+="layout(binding="+std::to_string(17+i)+") uniform usampler2DArray previous"+std::to_string(i)+";\n";}
    std::string common=rsxvk::common_glsl;
    const auto first=common.find("struct C "),last=common.find("uint rnd(");
    common.replace(first,last-first,"struct C {vec4 input_[15];vec4 cc[2];bool killed;};\n");
    std::string pattern=texture_glsl;
    const auto result_begin=pattern.find(" vec4 input_=c.argb,result;",pattern.find("vec4 textureSample("));
    const auto result_end=pattern.find("\n}",result_begin);
    std::string functions="vec4 textureResult(uint unit,vec4 c,inout bool killed){uint d=2064u+32u*unit;\n"+pattern.substr(result_begin,result_end-result_begin)+"\n}\n";
    pattern.replace(result_begin,result_end-result_begin," return textureResult(unit,c,killed);");
    // Texture alpha kill (descriptor word 14 = Texture::alpha_kill of the same draw, a fragment_source key field): with
    // static discards the per-unit flag is a constant mask, so a draw without alpha-kill units has no reachable discard.
    if(fragment_static_discard()){
        unsigned kill_mask=0;for(unsigned unit=0;unit<16;++unit)if(textures[unit].valid && textures[unit].alpha_kill)kill_mask|=1u<<unit;
        const std::string dynamic_kill="if(w[d+14u]!=0u && result.a<0.000001)";
        if(functions.find(dynamic_kill)==std::string::npos)throw std::runtime_error("RSX fragment alpha-kill template mismatch");
        replace_all(functions,dynamic_kill,"if((("+rsxvk::u(kill_mask)+">>unit)&1u)!=0u && result.a<0.000001)");
    }
    for(unsigned unit=0;unit<16;++unit)if(textures[unit].valid){
        const auto n=std::to_string(unit),u=rsxvk::u(unit);const auto& t=textures[unit];
        const bool volume=t.dimension==3 && t.levels[0].depth>1;
        source+="layout(binding="+std::to_string(unit+1)+") uniform sampler"+std::string(t.resident_image?"2D":volume?"3D":"2DArray")+" resident"+n+";\n";
        auto text=pattern;
        if(t.dimension!=3){
            const auto start=text.find("vec4 filter_("),end=text.find("float gradient(",start);
            text.replace(start,end-start,R"GLSL(
vec4 filter_(uint unit,uint level,vec3 uv,bool linear_,uint face,float ref){
 uint d=2064u+32u*unit,l=texture_offset(w[d])+(face*w[d+1u]+level)*4u;
 precise vec2 xy=uv.xy*vec2(w[l+1u],w[l+2u]);
 if(w[d+27u]==1u)xy.y=.5;
 if(any(isnan(xy)) || any(isinf(xy)) || any(greaterThan(abs(xy),vec2(1048576))))return vec4(0);
 if(!linear_)return texel_(unit,level,int(floor(xy.x)),int(floor(xy.y)),0,face,ref);
 xy-=.5;ivec2 p=ivec2(floor(xy));precise vec2 a=xy-vec2(p);
 precise vec4 c00=texel_(unit,level,p.x,p.y,0,face,ref),c10=texel_(unit,level,p.x+1,p.y,0,face,ref);
 precise vec4 c01=texel_(unit,level,p.x,p.y+1,0,face,ref),c11=texel_(unit,level,p.x+1,p.y+1,0,face,ref);
 return (c00*(1.0-a.x)+c10*a.x)*(1.0-a.y)+(c01*(1.0-a.x)+c11*a.x)*a.y;
}
)GLSL");
        }

        replace_all(text," // Resident-image substitution is inserted here, before the linear fetch.","");
        replace_all(text," c=load4(texture_offset(w[l])+4u*((uint(z)*w[l+2u]+uint(y))*w[l+1u]+uint(x)));",
            " c=texelFetch(resident"+n+","+(t.resident_image?"ivec2(x,y),0":volume?"ivec3(x,y,z),int(level)":"ivec3(x,y,face),int(level)")+ ");");
        if(t.resident_image){
            // RTT coordinates and explicit gradients belong to the guest.
            // Filtering/wrapping taps belong to the bound image, which may be
            // the higher-resolution visual companion. Keep rect-coordinate
            // normalization and LOD calculation below this prefix unchanged.
            const auto end=text.find("float gradient(");
            if(end==std::string::npos)throw std::runtime_error("RSX resident filter template mismatch");
            auto taps=text.substr(0,end);
            replace_all(taps,"w[l+1u]","uint(textureSize(resident"+n+",0).x)");
            replace_all(taps,"w[l+2u]","uint(textureSize(resident"+n+",0).y)");
            text.replace(0,end,taps);
        }
        for(const auto* name:{"texel_","filter_","gradient","textureSample"}){
            const auto f=std::string(name);replace_all(text,f+"(uint unit,",f+"(");replace_all(text,f+"(unit,",f+"(");replace_all(text,f+"(",f+n+"(");}
        replace_all(text,"cube_(","cube_"+n+"(");replace_all(text,"unit",u);
        replace_all(text,"w[d+27u]",rsxvk::u(t.dimension));replace_all(text,"w[d+28u]",rsxvk::u(t.cube));replace_all(text,"w[d+29u]",rsxvk::u(t.shadow));
        if(t.dimension!=3)replace_all(text,"n<2u","n<1u");
        // Live manual taps retain specialization. For implicit-only units,
        // these three fields occur only in unused manual helper functions;
        // keeping their descriptor loads avoids shader variants for dead code.
        const bool manual=!implicit_sampler(t) || (explicit_units&(1u<<unit));
        const bool dynamic=bool(t.resident_image) && dynamic_resident_sampler();
        for(const auto& field:std::vector<std::pair<unsigned,unsigned>>{{1,t.mip_count},{2,t.unnormalized},{3,t.wrap_s},{4,t.wrap_t},{5,t.min},{6,t.mag},{14,t.alpha_kill},{27,t.dimension},{28,t.cube},{29,t.shadow},{30,t.wrap_r}})
            if((manual || (field.first!=1 && field.first!=5 && field.first!=6)) &&
               (!dynamic || (field.first!=2 && field.first!=3 && field.first!=4 && field.first!=30)))
                replace_all(text,"w[d+"+rsxvk::u(field.first)+"]",rsxvk::u(field.second));
        functions+=text;
        replace_all(*shade,"gradient("+u+",b)","gradient"+n+"(a,b)");replace_all(*shade,"gradient("+u+",c)","gradient"+n+"(a,c)");
        replace_all(*shade,"gradient("+u+",dFdx(a))","gradient"+n+"(a,dFdx(a))");replace_all(*shade,"gradient("+u+",dFdy(a))","gradient"+n+"(a,dFdy(a))");
        replace_all(*shade,"textureSample("+u+",","textureSample"+n+"(");
    }
    // Disabled sampled units read RPCS3's transparent-black null image: every sample is vec4(0), no LOD needed.
    bool null_units=false;
    for(unsigned unit=0;unit<16;++unit)if(textures[unit].null_texture){const auto u=rsxvk::u(unit);null_units=true;
        replace_all(*shade,"textureSample("+u+",","null_sample_(");replace_all(*shade,"gradient("+u+",","null_gradient_(");}
    if(null_units)functions+="vec4 null_sample_(vec4 coord,float lambda,inout bool killed){return vec4(0);}\nfloat null_gradient_(vec4 delta){return 0.0;}\n";
    common.replace(common.find("vec4 texel_("),common.find("uint logic_(")-common.find("vec4 texel_("),functions);
    source+=common;if(packed)source+=fixed_glsl;
    for(unsigned unit=0;unit<16;++unit){const auto& t=textures[unit];
        if(!implicit_sampler(t))continue;
        const auto n=std::to_string(unit),u=rsxvk::u(unit);
        const bool volume=t.dimension==3 && t.levels[0].depth>1;
        source+="vec4 implicit"+n+"(vec4 coord,float bias,inout bool killed){uint d=2064u+32u*"+u+",l=texture_offset(w[d]);vec3 uv=coord.xyz;\n";
        source+="if(w[d+2u]!=0u)uv/=vec3(w[l+1u],w[l+2u],w[l+3u]);\n";
        if(t.dimension==1)source+="uv.y=0.5;\n";
        source+="vec3 scaled=uv*vec3(w[l+1u],w[l+2u],w[l+3u]);";
        if(t.dimension!=3)source+="scaled.z=0;";
        source+="if(any(isnan(scaled)) || any(isinf(scaled)) || any(greaterThan(abs(scaled),vec3(1048576))))return textureResult("+u+",vec4(0),killed);\n";
        source+="return textureResult("+u+",texture(resident"+n+","+(volume?"uv":"vec3(uv.xy,0)")+",bias),killed); }\n";
        for(const auto& i:fp){if(((i.d>>17)&15)!=unit)continue;const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6);
            if(op==23 || op==24 || op==49){
                // Implicit derivatives are evaluated by the texture unit.
                // TXB is the only opcode carrying an instruction LOD bias.
                const auto old="textureSample"+n+"(a,lod,killed)";
                // Each instruction is a scoped block. Replace only blocks of
                // the matching opcode (TEX and TXB may share a texture unit).
                std::size_t at=0;while((at=shade->find(old,at))!=std::string::npos){const auto begin=shade->rfind("{ precise vec4 a=",at);const auto block=shade->substr(begin,at-begin);
                    const bool biased=block.find("+a.w;")!=std::string::npos;
                    const auto value="implicit"+n+"(a,"+(biased?"a.w":"0.0")+",killed)";
                    // TXL/TXD have an explicit LOD construction; retain them.
                    if(block.find("float lod=a.w;")==std::string::npos && block.find("gradient"+n+"(a,b)")==std::string::npos){shade->replace(at,old.size(),value);at+=value.size();}else at+=old.size();}
                break;
            }}
    }
    std::string init=R"GLSL(
void main(){C q[1];bool front=w[11]!=0u || gl_FrontFacing;

 for(uint i=0u;i<15u;++i)q[0].input_[i]=vec4(0);
 q[0].cc[0]=q[0].cc[1]=vec4(0);q[0].killed=false;
 // Coverage/depth are per sample; RSX reference shades at pixel center.
 // Explicit interpolation restores the center after the coverage-plane shift.
 q[0].input_[0]=vec4((gl_FragCoord.xy/raster_scale_),gl_FragCoord.zw-dFdx(gl_FragCoord.zw)*shift.x-dFdy(gl_FragCoord.zw)*shift.y);
)GLSL";
    // Interpolate only inputs that some instruction reads (every source of an
    // FP instruction addresses the same input, d bits 13..16; the conservative
    // rule of rsx_execute_draw's used_inputs). The others stay vec4(0) and are
    // never observed, so the result is unchanged; fog reads input 3 only when
    // an instruction does. Input 0 (position) and 14 (facing) are always set.
    unsigned used_inputs=0;
    for(const auto& i:fp)if((i.s0&3)==1 || (i.s1&3)==1 || (i.s2&3)==1)used_inputs|=1u<<((i.d>>13)&15);
    for(unsigned i=1;i<=13;++i)if((used_inputs>>i)&1){
        const unsigned o=i<=2?i:i==3?5:i<=11?i+3:i==12?15:6;const auto n=std::to_string(i);
        // Two-sided colors (inputs 1/2) select the back varyings at run time.
        const auto center=i<=2?"((R(0x142cu)&1u)!=0u && !front?interpolateAtOffset(varying_["+std::to_string(o+2)+"],-shift):interpolateAtOffset(varying_["+std::to_string(o)+"],-shift))"
                              :"interpolateAtOffset(varying_["+std::to_string(o)+"],-shift)";
        init+=" {vec4 center="+center+";q[0].input_["+n+"]="+(i==3?"vec4(center.x,0,0,0)":"center")+";";
        if(i>=4)init+="if((R("+rsxvk::u(0xb40+4*(i-4))+")&1u)!=0u){q[0].input_["+n+"].z=0;q[0].input_["+n+"].w=1.0/q[0].input_[0].w;}";
        init+="}\n";
    }
    init+=" q[0].input_[14]=vec4(front?1:-1,0,0,0);\n";
    source+=init+scalar_locals();
    std::string line_init=R"GLSL(
 uint meta=w[10]+primitive_*208u;vec4 segment=load4(meta);vec2 center=(gl_FragCoord.xy/raster_scale_);
 float t=clamp(((center.x-segment.x)*segment.z+(center.y-segment.y)*segment.w)/(segment.z*segment.z+segment.w*segment.w),0,1);
 vec4 a=load4(meta+8u),b=load4(meta+76u);float iw=(1.0-t)*a.w+t*b.w;
 float z=(1.0-t)*a.z+t*b.z;float p0=(1.0-t)*a.w/iw,p1=t*b.w/iw;
 q[0].input_[0]=vec4(center,z,iw);
 for(uint i=1u;i<=13u;++i){uint o=i<=2u?i:i==3u?5u:i<=11u?i+3u:i==12u?15u:6u;
  vec4 v=R(0x368u)==0x1d00u && o>=1u && o<=4u?load4(meta+144u+o*4u):p0*load4(meta+12u+o*4u)+p1*load4(meta+80u+o*4u);
  q[0].input_[i]=i==3u?vec4(v.x,0,0,0):v;
  if(i>=4u && (R(0xb40u+4u*(i-4u))&1u)!=0u){q[0].input_[i].z=0;q[0].input_[i].w=1.0/iw;}}
)GLSL";
    if(lines)source+=line_init;
    bool fog=false;for(const auto& i:fp)if(((i.d>>13)&15)==3 && ((i.s0&3)==1 || (i.s1&3)==1 || (i.s2&3)==1))fog=true;
    std::string fog_source=R"GLSL(
 if(w[9]!=0u){float coord=q[0].input_[3].x,p0=F(0x8d0u),p1=F(0x8d4u),f=0;
 switch(R(0x8ccu)){
 case 0x2601u:f=p1*coord+p0-1.0;break;
 case 0x804u:f=p1*abs(coord)+p0-1.0;break;
 case 0x800u:f=exp(11.084*(p1*coord+p0-1.5));break;
 case 0x802u:f=exp(11.084*(p1*abs(coord)+p0-1.5));break;
 case 0x801u:case 0x803u:{float c=R(0x8ccu)==0x803u?abs(coord):coord;float t=4.709*(p1*c+p0-1.5);f=exp(-t*t);break;}}
 q[0].input_[3]=vec4(coord,clamp(f,0,1),0,0);}
)GLSL";
    if(fog)source+=fog_source;
    source+=*shade;
    source+=R"GLSL(
 // scalar output
 if(q[0].killed)discard;
 uvec2 pixel=uvec2((gl_FragCoord.xy/raster_scale_));bool inside=false;
)GLSL";
    // Register-driven discards (fragment_discard_state): a test that the draw's registers make impossible is not emitted, so
    // a shader without KIL/alpha-kill/depth output keeps early depth/stencil testing on the host GPU. Each omitted block
    // is a no-op for exactly the register values in the key (same R() values the block would read at run time).
    if(discard_state&FragmentDiscardWindow)source+=R"GLSL( for(uint i=0u;i<8u;++i)if((w[8]&(1u<<i))!=0u){uint h=R(0x2c0u+8u*i),v=R(0x2c4u+8u*i);
  inside=inside || (pixel.x>=(h&65535u) && pixel.x<=(h>>16u) && pixel.y>=(v&65535u) && pixel.y<=(v>>16u));}
 if(w[8]!=0u && ((R(0x2bcu)&1u)!=0u?inside:!inside))discard;
)GLSL";
    if(discard_state&FragmentDiscardStipple)source+=R"GLSL( if(w[11]==0u && R(0x147cu)!=0u && (R(0x1480u+4u*(pixel.y&31u))&(1u<<(pixel.x&31u)))==0u)discard;
)GLSL";
    source+=R"GLSL( vec4 c=rd(q[0],0u,(R(0x1d60u)&0x40u)==0u);
)GLSL";
    if(discard_state&FragmentDiscardAlpha)source+=R"GLSL( if((R(0x304u)&1u)!=0u && !cmp(R(0x308u),cpu_clamp(c,vec4(0),vec4(1)).a,float(R(0x30cu)&255u)/255.0))discard;
)GLSL";
    if(discard_state&FragmentDiscardCoverage)source+=R"GLSL( // NV4097_SET_ANTI_ALIASING_CONTROL 0x1d7c (RPCS3 ffba2bf0 rsx_decode.h:4010-4028): bit 0 enable (no effect in
 // RPCS3 graphics_pipeline_state.hpp:199-212), bit 4 alpha-to-coverage, bit 8 alpha-to-one, bits 16-31 sample mask.
 // Each pass renders one sample (plane). The coverage pattern is implementation-defined (Vulkan/GL; RPCS3 emulates
 // it stochastically): derived ordered pattern, round(alpha*samples) samples on average via a 4x4 Bayer offset.
 {uint aa_=R(0x1d7cu),mode_=(R(0x208u)>>12u)&15u,n_=mode_==0u?1u:mode_==3u?2u:4u;
  if((((aa_>>16u)>>plane)&1u)==0u)discard;
  if((aa_&16u)!=0u){const uint bayer_[16]=uint[16](0u,8u,2u,10u,12u,4u,14u,6u,3u,11u,1u,9u,15u,7u,13u,5u);
   float d_=(float(bayer_[(pixel.y&3u)*4u+(pixel.x&3u)])+.5)/16.;
   if(!(clamp(c.a,0.,1.)*float(n_)>float(plane)+d_))discard;}}
)GLSL";
    if(lines)source+=R"GLSL(
 bool hit=false,xmajor=abs(segment.z)>=abs(segment.w);uint width=uint(w[meta+4u]);
 for(uint row=0u;row<width && !hit;++row){double delta=double(row)-double(width-1u)*.5;
  double x=double(segment.x)-double(center.x)-(xmajor?0.0:delta)-1.e-7;
  double y=double(segment.y)-double(center.y)-(xmajor?delta:0.0)-1.e-14;
  double enter=0,leave=1;
  for(int n=0;n<4;++n){int xs=(n&1)!=0?1:-1,ys=(n&2)!=0?1:-1;
   double q=.5-double(xs)*x-double(ys)*y,d=double(xs)*double(segment.z)+double(ys)*double(segment.w);
   if(d>0)leave=min(leave,q/d);else if(d<0)enter=max(enter,q/d);else if(q<0)leave=-1;}
  hit=leave>enter && leave<1 && leave>0;}
 if(!hit)discard;
)GLSL";
    if(r(0x1d60)&14)source+="gl_FragDepth=rd(q[0],1u,false).z;\n";
    else if(lines)source+="gl_FragDepth=z;\n";
    for(unsigned i=0;i<count;++i){const unsigned o=i?i+1:0;
        auto value="rd(q[0],"+rsxvk::u((r(0x1d60)&0x40)?o:o*2)+","+((r(0x1d60)&0x40)?"false":"true")+")";
        // Alpha-to-one replaces the fragment alpha before blending (0x1d7c bit 8, read at run time).
        source+="{vec4 a2o_="+value+";if((R(0x1d7cu)&0x100u)!=0u)a2o_.a=1.0;\n";value="a2o_";
        if(!packed && !float_color(r(0x208)&31))value="cpu_clamp("+value+",vec4(0),vec4(1))";
        if(packed)value="packed_finish("+value+",texelFetch(previous"+std::to_string(i)+",ivec3(ivec2(gl_FragCoord.xy),plane),0).r,"+rsxvk::u(i)+")";
        source+="out"+std::to_string(i)+"="+value+";}\n";
    }
    replace_all(source,"rd(q[0],0u,(R(0x1d60u)&0x40u)==0u)",fp_read(0,(r(0x1d60)&0x40)==0));
    scalar_registers(source);
    replace_all(source,"uint R(uint a){return w[16u+a/4u];}",state_glsl);
    replace_all(source,"load4(w[14]+","state4(w[14]+");
    replace_all(source,"d=2064u+32u*","d=48u+32u*");
    return source+"}\n";
}
static std::shared_ptr<const std::string> fragment_source(const std::vector<FpInst>& fp,const Reg& r,unsigned count,const std::array<Texture,16>& textures,bool lines=false,rsxvk::FragmentFailure* failure=nullptr,unsigned width=0,unsigned height=0){
    static thread_local std::vector<unsigned> key;const unsigned discard_state=fragment_discard_state(r,width,height);
    key.assign({count,unsigned(lines),unsigned(packed_color(r)),unsigned(bool(r(0x1d60)&14u)),r(0x1d60)&64u,unsigned(float_color(r(0x208)&31u)),unsigned(rsx_layered_shaders().load(std::memory_order_relaxed)),discard_state});
    unsigned mask=0;for(unsigned unit=0;unit<16;++unit)if(textures[unit].valid)mask|=1u<<unit;else if(textures[unit].null_texture)mask|=0x10000u<<unit;key.push_back(mask);
    const unsigned explicit_units=explicit_sampler_mask(fp);
    for(unsigned unit=0;unit<16;++unit){const auto& t=textures[unit];if(!t.valid)continue;
        const bool manual=!implicit_sampler(t) || (explicit_units&(1u<<unit));
        const bool dynamic=bool(t.resident_image) && dynamic_resident_sampler();
        key.insert(key.end(),{unsigned(bool(t.resident_image)),t.format,t.dimension,unsigned(t.dimension==3 && t.levels[0].depth>1),unsigned(t.cube),t.shadow,unsigned(t.alpha_kill),dynamic?0u:t.wrap_s,dynamic?0u:t.wrap_t,dynamic?0u:t.wrap_r,manual?t.mip_count:0u,dynamic?0u:unsigned(t.unnormalized),manual?t.min:0u,manual?t.mag:0u});}
    // The source reads only the instruction words of fp (never its constants). A loader structure id names exactly those words
    // (rsxdraw::FpInst::structure); the tag word keeps the two key forms disjoint.
    if(!fp.empty() && fp[0].structure)key.insert(key.end(),{1u,unsigned(fp.size()),unsigned(fp[0].structure),unsigned(fp[0].structure>>32)});
    else{key.push_back(0u);for(const auto& i:fp)key.insert(key.end(),{i.d,i.s0,i.s1,i.s2,unsigned(i.has_constant),i.word_offset});}
    // Consecutive draws mostly repeat the previous key: compare before hashing.
    static thread_local std::vector<unsigned> last_key;static thread_local std::shared_ptr<const std::string> last_source;
    if(last_source && key==last_key){::RsxPerfCounters::add(::RsxPerfCounters::src_fs_hits);return last_source;}
    static thread_local SourceCache<std::vector<unsigned>> cache;
    if(auto hit=cache.find(key)){::RsxPerfCounters::add(::RsxPerfCounters::src_fs_hits);last_key=key;last_source=hit;return hit;}
    ::RsxPerfCounters::add(::RsxPerfCounters::src_fs_misses);
    auto source=fragment_source_uncached(fp,r,count,textures,lines,failure,discard_state);if(!source)return {};
    if(rsx_layered_shaders().load(std::memory_order_relaxed)){const auto main=source->find("void main(){");
        if(main==std::string::npos || source->find("void main(){",main+1)!=std::string::npos)throw std::runtime_error("RSX layered fragment shader template mismatch");
        source->replace(main,12,"void main(){shift=layers_>1u?shifts_[plane_in_]:shift_push_;plane=layers_>1u?plane_in_:plane_push_;");}
    auto stored=cache.put(key,std::move(*source));last_key=key;last_source=stored;return stored;
}
// Layered MSAA planes (rsx_layered_shaders): instance n renders sample plane n
// into layer n with its own sample shift; FS planes come from plane_out_.
static const char* layered_vertex_glsl=R"GLSL(layout(push_constant) uniform Params{vec2 extent;vec2 shift_push_;uint plane_push_;uint layers_;vec2 shifts_[4];layout(offset=56) vec2 raster_scale_;};
layout(location=17) flat out uint plane_out_;
vec2 shift;
void layered_plane_(){shift=layers_>1u?shifts_[gl_InstanceIndex]:shift_push_;plane_out_=layers_>1u?uint(gl_InstanceIndex):plane_push_;gl_Layer=layers_>1u?gl_InstanceIndex:0;}
)GLSL";
// Multiview (VK_KHR_multiview): view n = sample plane n, one draw instance. Outside a multiview
// render pass (single-plane passes) gl_ViewIndex is 0 and layers_ is 1.
static const char* multiview_vertex_glsl=R"GLSL(layout(push_constant) uniform Params{vec2 extent;vec2 shift_push_;uint plane_push_;uint layers_;vec2 shifts_[4];layout(offset=56) vec2 raster_scale_;};
layout(location=17) flat out uint plane_out_;
vec2 shift;
void layered_plane_(){shift=layers_>1u?shifts_[gl_ViewIndex]:shift_push_;plane_out_=layers_>1u?uint(gl_ViewIndex):plane_push_;}
)GLSL";
static std::string layered_vertex(std::string source){
    if(!rsx_layered_shaders().load(std::memory_order_relaxed))return source;
    const std::string block="layout(push_constant) uniform Params{vec2 extent;vec2 shift;layout(offset=56) vec2 raster_scale_;};\n";
    const auto at=source.find(block),main=source.find("void main(){");
    if(at==std::string::npos || main==std::string::npos)throw std::runtime_error("RSX layered vertex shader template mismatch");
    const bool multiview=rsx_multiview_shaders().load(std::memory_order_relaxed);
    source.replace(main,12,"void main(){layered_plane_();");source.replace(at,block.size(),multiview?multiview_vertex_glsl:layered_vertex_glsl);
    const auto version=source.find('\n');if(source.compare(0,13,"#version 450\n"))throw std::runtime_error("RSX layered vertex shader version mismatch");
    source.insert(version+1,multiview?"#extension GL_EXT_multiview : require\n":"#extension GL_ARB_shader_viewport_layer_array : require\n");return source;
}
static std::string vertex_source(){return layered_vertex(R"GLSL(#version 450
layout(std430,binding=0) readonly buffer Data{uint w[];};
layout(push_constant) uniform Params{vec2 extent;vec2 shift;layout(offset=56) vec2 raster_scale_;};
layout(location=0) out vec4 varying_[16];
layout(location=16) flat out uint primitive_;
vec4 load4(uint a){return uintBitsToFloat(uvec4(w[a],w[a+1u],w[a+2u],w[a+3u]));}
void main(){primitive_=uint(gl_VertexIndex)/3u;uint at=w[3]+uint(gl_VertexIndex)*68u;vec4 p=load4(at);float cw=1.0/p.w;
 gl_Position=vec4(((p.xy-shift/raster_scale_)/extent*2.0-1.0)*cw,p.z*cw,cw);
 for(uint i=0u;i<16u;++i)varying_[i]=load4(at+4u+4u*i);}
)GLSL");}
static const char* raw_vertex_glsl=R"GLSL(
uint device_vertex_(uint id){
 if(w[15]!=2u)return id;
 uint at=w[13],mode=w[at],tri=id/3u,corner=id%3u,pos=id;
 if(mode==6u)pos=tri+((tri&1u)!=0u && corner<2u?1u-corner:corner);
 else if(mode==7u || mode==10u)pos=corner==0u?0u:tri+corner;
 else if(mode==8u || mode==9u){uint c=id%6u;
  uint lane=mode==8u?(c==0u || c==3u?0u:c==1u?1u:c==2u || c==4u?2u:3u):(c==0u || c==3u?0u:c==1u?1u:c==2u || c==4u?3u:2u);
  pos=(id/6u)*(mode==8u?4u:2u)+lane;
 }
 for(uint i=0u;i<w[at+1u];++i){uint start=w[at+2u+i*2u],n=w[at+3u+i*2u];if(pos<n)return start+pos;pos-=n;}
 atomicOr(w[7],16u);return 0u;
}
uint vertex_byte(uint a){return (state_[a/4u]>>((a&3u)*8u))&255u;}
uint vertex_be(uint a,uint n){uint v=0u;for(uint c=0u;c<n;++c)v=(v<<8u)|vertex_byte(a+c);return v;}
float snorm_(uint v,uint bits){int n=int(v<<(32u-bits))>>int(32u-bits);return max(-1.0,float(n)/float((1u<<(bits-1u))-1u));}
vec4 attribute_(uint attr,uint id){
 uint d=w[12]+attr*8u,f=w[d],n=(f>>4u)&15u,type=f&15u;
 if(n==0u)return load4(d+4u);
 uint freq=f>>16u;if(freq>1u)id=(w[16u+0x1fc0u/4u]&(1u<<attr))!=0u?id%freq:id/freq;
 uint a=w[d+1u]+(id-w[d+2u])*w[d+3u],width=type==2u?4u:(type==1u || type==3u || type==5u)?2u:1u;
 if(type==6u){uint v=vertex_be(a,4u);return vec4(snorm_(v&2047u,11u),snorm_((v>>11u)&2047u,11u),snorm_(v>>22u,10u),1);}
 vec4 v=vec4(0,0,0,1);for(uint c=0u;c<n;++c){uint raw=vertex_be(a+c*width,width);
 v[c]=type==1u?snorm_(raw,16u):type==2u?uintBitsToFloat(raw):type==3u?unpackHalf2x16(raw).x:type==4u?float(raw)/255.0:type==5u?float(int(raw<<16u)>>16):float(raw);}
 return v;
}
)GLSL";
static std::optional<std::string> hardware_vertex_uncached(const NativeRsx& gpu,bool fetch=false,bool immediate=false){
    HostPhase phase(HostStage::source);
    const Reg r{gpu};auto emitted=rsxvk::vertex_shader(gpu,r.get(0x1ea0,0),true,true);if(!emitted)return {};
    const auto at=emitted->find("void main(){");if(at==std::string::npos)return {};
    std::string body=emitted->substr(at);
    replace_all(body,"uint id=gl_GlobalInvocationID.x;if(id>=w[0])return;","uint id=uint(gl_VertexIndex);");
    replace_all(body,"1u+id*64u+n*4u","w[3]+id*64u+n*4u");
    const std::string tail="for(uint n=0u;n<16u;++n){uvec4 v=floatBitsToUint(o[n]);uint at=1u+w[0]*64u+id*64u+n*4u;for(uint c=0u;c<4u;++c)w[at+c]=v[c];}}";
    if(body.find(tail)==std::string::npos)return {};
    const std::string scale="vec3(F(0xa30u),F(0xa34u),F(0xa38u))";
    const std::string offset="vec3(F(0xa20u),F(0xa24u),F(0xa28u))";
    replace_all(body,tail,"vec3 p=o[0].xyz/o[0].w*"+scale+"+"+offset+";gl_Position=vec4(((p.xy-shift/raster_scale_)/extent*2.0-1.0)*o[0].w,p.z*o[0].w,o[0].w);for(uint n=0u;n<16u;++n)varying_[n]=o[n];}");
    replace_all(body,"uint id=uint(gl_VertexIndex);","uint id=uint(gl_VertexIndex);primitive_=id/3u;");
    if(fetch || immediate){
        if(fetch){replace_all(body,"input_[n]=load4(w[3]+id*64u+n*4u)","input_[n]=attribute_(n,id)");
            replace_all(body,"primitive_=id/3u;","primitive_=id/3u;id=device_vertex_(id);");}
        // A non-finite position is left to the host clipper, which discards the primitive like the CPU
        // reference (rsx_draw.py clip_polygon); NaN/Inf propagate through the viewport transform below.
        replace_all(body,"gl_Position=vec4(","if(any(isnan(o[0])) || any(isinf(o[0]))){gl_Position=vec4(uintBitsToFloat(0x7fc00000u));for(uint n=0u;n<16u;++n)varying_[n]=vec4(0);return;}gl_Position=vec4(");
        replace_all(body,"for(uint n=0u;n<16u;++n)varying_[n]=o[n];}",R"GLSL(
 gl_ClipDistance[0]=o[0].z*F(0xa38u)+(F(0xa28u)-F(0x394u))*o[0].w;
 gl_ClipDistance[1]=(F(0x398u)-F(0xa28u))*o[0].w-o[0].z*F(0xa38u);
 for(uint n=0u;n<6u;++n){uint mode=(w[16u+0x1478u/4u]>>(4u*n))&15u;
 float d=o[n<3u?5u:6u][1u+n%3u];gl_ClipDistance[n+2u]=mode==0u || (w[16u+0x1ff4u/4u]&(1u<<(6u+n)))==0u?1.0:mode==1u?-d:d;}
 for(uint n=0u;n<16u;++n)varying_[n]=o[n];}
)GLSL");
    }
    auto source=std::string("#version 450\nlayout(std430,binding=0) buffer Data{uint w[];};\nlayout(push_constant) uniform Params{vec2 extent;vec2 shift;layout(offset=56) vec2 raster_scale_;};\nlayout(location=0) out vec4 varying_[16];\nlayout(location=16) flat out uint primitive_;\nvec4 load4(uint a){return uintBitsToFloat(uvec4(w[a],w[a+1u],w[a+2u],w[a+3u]));}\nfloat F(uint a){return uintBitsToFloat(w[16u+a/4u]);}\n")+((fetch || immediate)?std::string("out gl_PerVertex {vec4 gl_Position;float gl_PointSize;float gl_ClipDistance[8];};\n")+(fetch?raw_vertex_glsl:""):"")+body;
    replace_all(source,"float F(uint a){return uintBitsToFloat(w[16u+a/4u]);}",std::string(state_glsl)+"\nfloat F(uint a){return uintBitsToFloat(R(a));}");
    for(const auto* address:{"0x1fc0u","0x1478u","0x1ff4u"})replace_all(source,std::string("w[16u+")+address+"/4u]",std::string("R(")+address+")");
    replace_all(source,"load4(w[2]+","cstate4(");
    replace_all(source,"w[w[2]+1872u+","state_[state_[w[2]+15u]+");
    return layered_vertex(source);
}
static std::shared_ptr<const std::string> hardware_vertex(const NativeRsx& gpu,bool fetch=false,bool immediate=false){
    const unsigned start=Reg{gpu}.get(0x1ea0,0);
    static thread_local std::string key;key.clear();
    auto append_key=[&](const void* p,std::size_t n){key.append(static_cast<const char*>(p),n);};
    const unsigned flags[]={start,unsigned(fetch),unsigned(immediate),unsigned(rsx_layered_shaders().load(std::memory_order_relaxed))};append_key(flags,sizeof(flags));
    // Every instruction the translation can reach (branch targets included), by address.
    rsxvk::VertexFlow flow;if(!rsxvk::vertex_program_flow(gpu,start,flow))return {};
    for(unsigned pc=0;pc<544;++pc)if(flow.reach[pc]){append_key(&pc,sizeof(pc));append_key(gpu.transform_program_written.data()+pc*4,4*sizeof(bool));append_key(gpu.transform_program.data()+pc*4,16);}
    // Exact canonical bool bytes avoid rebuilding 1,872 mask bits per lookup.
    append_key(gpu.transform_constants_written.data(),468*4*sizeof(bool));
    static thread_local SourceCache<std::string> cache;
    if(auto hit=cache.find(key)){::RsxPerfCounters::add(::RsxPerfCounters::src_vs_hits);return hit;}
    ::RsxPerfCounters::add(::RsxPerfCounters::src_vs_misses);
    auto source=hardware_vertex_uncached(gpu,fetch,immediate);if(!source)return {};
    return cache.put(std::move(key),std::move(*source));
}
static VkBlendFactor factor(unsigned f){
    switch(f){case 0:return VK_BLEND_FACTOR_ZERO;case 1:return VK_BLEND_FACTOR_ONE;
    case 0x300:return VK_BLEND_FACTOR_SRC_COLOR;case 0x301:return VK_BLEND_FACTOR_ONE_MINUS_SRC_COLOR;
    case 0x302:return VK_BLEND_FACTOR_SRC_ALPHA;case 0x303:return VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    case 0x304:return VK_BLEND_FACTOR_DST_ALPHA;case 0x305:return VK_BLEND_FACTOR_ONE_MINUS_DST_ALPHA;
    case 0x306:return VK_BLEND_FACTOR_DST_COLOR;case 0x307:return VK_BLEND_FACTOR_ONE_MINUS_DST_COLOR;case 0x308:return VK_BLEND_FACTOR_SRC_ALPHA_SATURATE;
    case 0x8001:return VK_BLEND_FACTOR_CONSTANT_COLOR;case 0x8002:return VK_BLEND_FACTOR_ONE_MINUS_CONSTANT_COLOR;
    case 0x8003:return VK_BLEND_FACTOR_CONSTANT_ALPHA;case 0x8004:return VK_BLEND_FACTOR_ONE_MINUS_CONSTANT_ALPHA;
    default:throw std::runtime_error("unsupported graphics blend factor");}
}
static VkBlendOp equation(unsigned f){switch(f){case 0x8006:return VK_BLEND_OP_ADD;case 0x800a:return VK_BLEND_OP_SUBTRACT;case 0x800b:return VK_BLEND_OP_REVERSE_SUBTRACT;case 0x8007:return VK_BLEND_OP_MIN;case 0x8008:return VK_BLEND_OP_MAX;default:throw std::runtime_error("unsupported graphics blend equation");}}
static VkStencilOp stencil_op_vk(unsigned op){switch(op){case 0:return VK_STENCIL_OP_ZERO;case 0x1e00:return VK_STENCIL_OP_KEEP;case 0x1e01:return VK_STENCIL_OP_REPLACE;case 0x1e02:return VK_STENCIL_OP_INCREMENT_AND_CLAMP;case 0x1e03:return VK_STENCIL_OP_DECREMENT_AND_CLAMP;case 0x150a:return VK_STENCIL_OP_INVERT;case 0x8507:return VK_STENCIL_OP_INCREMENT_AND_WRAP;case 0x8508:return VK_STENCIL_OP_DECREMENT_AND_WRAP;default:throw std::runtime_error("unsupported graphics stencil op");}}
static VkCompareOp comparison(unsigned f){if(f<0x200 || f>0x207)throw std::runtime_error("unsupported graphics comparison");return static_cast<VkCompareOp>(f-0x200);}
static VkFormat color_format(unsigned f,bool srgb=false){switch(f){case 1:case 2:case 4:case 5:case 9:case 10:case 14:case 15:case 16:return VK_FORMAT_R32_UINT;case 3:return VK_FORMAT_R5G6B5_UNORM_PACK16;case 8:return srgb?VK_FORMAT_R8G8B8A8_SRGB:VK_FORMAT_R8G8B8A8_UNORM;case 11:return VK_FORMAT_R16G16B16A16_SFLOAT;case 12:return VK_FORMAT_R32G32B32A32_SFLOAT;case 13:return VK_FORMAT_R32_SFLOAT;default:return VK_FORMAT_UNDEFINED;}}
static unsigned image_bpp(VkFormat f){switch(f){case VK_FORMAT_R5G6B5_UNORM_PACK16:case VK_FORMAT_D16_UNORM:return 2;case VK_FORMAT_R16G16B16A16_SFLOAT:return 8;case VK_FORMAT_R32G32B32A32_SFLOAT:return 16;default:return 4;}}
struct Surface {
    Target target;unsigned width,height,samples,format;bool depth,dirty=false;
    std::shared_ptr<NativeVulkan::GraphicsImage> image;
    std::shared_ptr<NativeRsx::Tile> tile_snapshot;
    std::uint64_t last_use=0,generation=0;
    std::vector<GuestMemory::HostGuard::Span> protection{},physical{};
    bool protectable=false,partial=false;
    std::vector<GuestMemory::HostGuard::Span> exposed{};
    std::vector<std::uint8_t> edge_snapshot{};
    std::uint64_t edge_generation=UINT64_MAX;
    // data is a physical identity for overlap/watch bookkeeping only. Guest
    // bytes are committed through the pinned mapping at guest_offset.
    struct CommitRun {std::uint8_t* data;unsigned size,offset,guest_offset;};
    struct ReadbackPlan {std::vector<CommitRun> runs;std::vector<unsigned> words;std::shared_ptr<NativeVulkan::GraphicsBuffer> gpu_words;};
    std::shared_ptr<ReadbackPlan> readback{};
    // Retain the mapping and backing. No guest virtual address is an identity.
    bool same(const Target& t,unsigned w,unsigned h,unsigned n,unsigned f,bool d)const{
        if(width!=w || height!=h || samples!=n || format!=f || depth!=d || target.pitch!=t.pitch || target.offset!=t.offset ||
           target.base_offset!=t.base_offset || target.swizzle_width!=t.swizzle_width || target.swizzle_height!=t.swizzle_height)return false;
        if(bool(target.tile)!=bool(t.tile) || (target.tile && (target.tile->offset!=t.tile->offset || target.tile->pitch!=t.tile->pitch || target.tile->size!=t.tile->size || target.tile->bank!=t.tile->bank)))return false;
        if(!target.mapping || !t.mapping)return target.base==t.base;
        if(target.mapping->parts.size()!=t.mapping->parts.size())return false;
        for(unsigned i=0;i<t.mapping->parts.size();++i)if(target.mapping->parts[i].data!=t.mapping->parts[i].data || target.mapping->parts[i].size!=t.mapping->parts[i].size)return false;
        return true;
    }
    bool overlaps(const void* p,std::uint64_t size)const{
        if(!size)return false;
        const auto at=reinterpret_cast<std::uintptr_t>(p);
        auto span=std::lower_bound(physical.begin(),physical.end(),at,[](const auto& a,std::uintptr_t b){return a.end<=b;});
        return span!=physical.end() && span->begin<at+size;
    }
};
''' + CODEC_CPP + r'''

// Nonindexed array topology is a compact sequence of ranges. Bounds are
// exact over the consumed prefix, including modulo divisors, in O(ranges).
// The vertex shader expands topology; no per-vertex host index vector exists.
struct DeviceGeometry {
    const std::vector<std::pair<std::uint32_t,std::uint32_t>>& ranges;
    unsigned primitive,used=0,vertices=0;bool valid=false;
    explicit DeviceGeometry(const RsxDrawState& d):ranges(d.ranges),primitive(d.primitive){
        if(d.indexed || !d.elements.empty() || !d.immediate_vertices.empty() || primitive<5 || primitive>10)return;
        std::uint64_t n=0;
        for(const auto& r:ranges){if(std::uint64_t(r.first)+r.second>UINT64_C(0x100000000))return;n+=r.second;if(n>UINT_MAX)return;}
        std::uint64_t count=0;
        switch(primitive){case 5:used=n/3*3;count=used;break;
            case 6:case 7:case 10:used=n>=3?n:0;count=n>=3?(n-2)*3:0;break;
            case 8:used=n/4*4;count=n/4*6;break;
            case 9:used=n>=4?n/2*2:0;count=n>=4?(n/2-1)*6:0;break;}
        if(count>UINT_MAX)return;
        vertices=count;valid=true;
    }
    std::pair<unsigned,unsigned> bounds(unsigned frequency,bool mod)const{
        if(!used)return {UINT_MAX,0};
        unsigned remaining=used,lo=UINT_MAX,hi=0;
        for(const auto& r:ranges){const unsigned n=std::min(r.second,remaining);remaining-=n;if(!n)continue;
            const unsigned first=r.first,last=r.first+n-1;
            unsigned a=first,b=last;
            if(frequency>1){if(!mod){a/=frequency;b/=frequency;}
                else if(n>=frequency || first%frequency>last%frequency){a=0;b=frequency-1;}
                else{a%=frequency;b%=frequency;}}
            lo=std::min(lo,a);hi=std::max(hi,b);if(!remaining)break;
        }
        return {lo,hi};
    }
};
// Bounds belong to one draw. Division is monotone; modulo needs an exact
// pass, shared by attributes using the same frequency (no widened reads).
struct IndexBounds {
    const std::vector<std::array<unsigned,3>>& indices;
    unsigned lo=UINT_MAX,hi=0;const DeviceGeometry* device=nullptr;
    std::vector<std::pair<unsigned,std::pair<unsigned,unsigned>>> modulo;
    // known: the exact extent of input recorded while its indices were decoded (RsxDrawState::triangle_extent), else scanned.
    explicit IndexBounds(const std::vector<std::array<unsigned,3>>& input,const DeviceGeometry* sequence=nullptr,const std::pair<unsigned,unsigned>* known=nullptr):indices(input),device(sequence){
        if(device){const auto b=device->bounds(1,false);lo=b.first;hi=b.second;return;}
        if(known){lo=known->first;hi=known->second;return;}
        index_extent(indices.data(),indices.size(),lo,hi);
    }
    std::pair<unsigned,unsigned> get(unsigned frequency,bool mod){
        if(device)return device->bounds(frequency,mod);
        if(indices.empty())return {UINT_MAX,0};
        if(frequency<=1)return {lo,hi};
        if(!mod)return {lo/frequency,hi/frequency};
        for(const auto& entry:modulo)if(entry.first==frequency)return entry.second;
        unsigned first=UINT_MAX,last=0;
        for(const auto& tri:indices)for(auto id:tri){const auto v=id%frequency;first=std::min(first,v);last=std::max(last,v);}
        modulo.push_back({frequency,{first,last}});return {first,last};
    }
};
struct VertexProgramCache {
    std::array<std::uint64_t,3> versions{};
    unsigned start=UINT_MAX,texture_mask=0;
    const NativeRsx* identity=nullptr;
    bool valid=false;
    std::array<bool,3> attempted{};
    std::array<std::shared_ptr<const std::string>,3> sources{};
    void refresh(const NativeRsx& gpu){
        const std::array<std::uint64_t,3> current{gpu.transform_program.version(),gpu.transform_program_written.version(),gpu.transform_constants_written.version()};
        const auto first=Reg{gpu}.get(0x1ea0,0);
        if(valid && identity==&gpu && versions==current && start==first)return;
        identity=&gpu;versions=current;start=first;valid=true;attempted={};sources={};texture_mask=0;
        // Preserve the original whole-bank certification, even after END.
        for(unsigned pc=0;pc<544;++pc)if(gpu.transform_program_written[pc*4+1] && ((gpu.transform_program[pc*4+1]>>22)&31)==25)
            texture_mask|=1u<<((gpu.transform_program[pc*4+2]>>8)&3);
    }
    std::shared_ptr<const std::string> source_current(const NativeRsx& gpu,unsigned mode){
        if(!valid || identity!=&gpu)throw std::runtime_error("RSX vertex program cache was not refreshed");
        if(!attempted.at(mode)){sources[mode]=hardware_vertex(gpu,mode==1,mode==2);attempted[mode]=true;}return sources[mode];
    }
    std::shared_ptr<const std::string> source(const NativeRsx& gpu,unsigned mode){refresh(gpu);return source_current(gpu,mode);}
};

// Worker callers supply their private NativeRsx, pinned memory facade and
// a completion transport. No live PPU register file or event registry is kept.
struct RendererContext {
    GuestMemory& memory;NativeRsx& rsx;
    std::uint32_t pc=0,thread_id=0;
    std::function<void(unsigned,std::uint64_t,std::uint64_t)> event;
    std::vector<GuestMemory::Region> maps;
    bool coordinator_maps=false;
};
struct Session {
    NativeVulkan& vk;RendererContext state;std::unique_ptr<NativeVulkan::GraphicsBatch> batch;
    std::vector<Surface> surfaces,layout_cache;
    std::unordered_multimap<std::uintptr_t,std::size_t> surface_index;
    std::uint64_t layout_hits=0,layout_misses=0;
    static std::uintptr_t surface_key(const Target& t){return reinterpret_cast<std::uintptr_t>(t.base)^(std::uintptr_t(t.offset)<<1)^(std::uintptr_t(t.pitch)<<17);}
    void index_surfaces(){surface_index.clear();for(std::size_t i=0;i<surfaces.size();++i)surface_index.emplace(surface_key(surfaces[i].target),i);}
    struct Pending{unsigned status,first,count;bool report;};std::vector<Pending> pending;
    struct QuerySnapshot {std::shared_ptr<std::vector<std::uint64_t>> values;unsigned first,count;};
    std::shared_ptr<std::vector<std::uint64_t>> query_results=std::make_shared<std::vector<std::uint64_t>>();
    struct Label {GuestMemory::Range mapping;std::uint64_t address;unsigned value,size=4;
        bool report=false,active=false,stats=false;std::uint64_t base=0;std::vector<QuerySnapshot> queries{};
        unsigned kind=0,queue=0;std::uint64_t event_name=0,event_flags=0;
        const void* identity()const{return mapping.parts.front().data;}
        const std::shared_ptr<GuestBacking>& owner()const{return mapping.parts.front().owner;}
    };
    std::vector<Label> labels;
    struct Flight {
        std::unique_ptr<NativeVulkan::GraphicsBatch> batch;
        std::vector<Pending> pending;std::vector<Label> labels;
        std::shared_ptr<std::vector<std::uint64_t>> results;
        std::chrono::steady_clock::time_point submitted{};
        std::vector<std::function<void(const NativeVulkan::GraphicsBatch&)>> completions{};
        unsigned report_floor=0;   // see pending_report_floor
    };
    // RESET_REPORT clears the report attribution of every pending draw issued before it. Draws are only appended, so the
    // attributed ones are exactly [report_floor, size) with Pending::report set: a reset moves the floors (O(flights))
    // instead of rewriting every entry, and a report scans only the draws since the last reset.
    unsigned pending_report_floor=0;
    // Host-only consumers of bytes that the GPU writes into the current batch (asynchronous frame capture): run once that batch
    // completed (retire, or the synchronous flush), before its staging memory is reset. They never touch guest state.
    std::vector<std::function<void(const NativeVulkan::GraphicsBatch&)>> completions;
    double flight_ns=300000;   // EWMA of submit -> retire latency of a flight; only maintained for ANYPS3_RSX_POLL=adaptive (rsx_poll_adaptive)
    // flight_limit+1 persistent mappings/command+descriptor pools; an arena cannot be
    // reused until its exact timeline value completed and results retired.
    // ANYPS3_RSX_FLIGHTS (1..8, default 2) bounds the submitted, unretired arenas.
    static unsigned rsx_flight_limit(){static const unsigned value=[]{const char* v=std::getenv("ANYPS3_RSX_FLIGHTS");if(!v)return 2u;
            if(v[0]<'1' || v[0]>'8' || v[1])throw std::runtime_error("ANYPS3_RSX_FLIGHTS must be an integer from 1 to 8");
            return unsigned(v[0]-'0');}();
        return value;}
    const unsigned flight_limit=rsx_flight_limit();
    std::deque<Flight> flights;
    std::vector<std::unique_ptr<NativeVulkan::GraphicsBatch>> spare_batches;
    // Emptied label/pending vectors of retired flights: a submit moves the current ones into its flight, so without reuse every
    // FIFO turn regrows them (Label moves on each reallocation). Only capacity is recycled; a pooled vector is always empty.
    std::vector<std::vector<Label>> label_pool;std::vector<std::vector<Pending>> pending_pool;
    std::uint64_t async_submits=0,async_retires=0,label_waits=0,label_ready=0;
    unsigned submit_draws=64,submit_extend=4;
    void kick_draws(){
        if(!submit_draws || pending.size()<submit_draws)return;
        // Keep feeding Vulkan while the CPU records the next prefix. The
        // heuristic never waits for an arena: defer the kick under pressure.
        // Poll the timeline (a driver call) once per submit_draws/4 draws only.
        if(!flights.empty() && (pending.size()-submit_draws)%std::max(1u,submit_draws/4))return;
        retire(false);
        // While an earlier prefix still executes, the GPU is fed: extend this
        // prefix (bounded) instead of paying another queue submission. An
        // idle queue receives the prefix at the usual threshold.
        if(!flights.empty() && pending.size()<std::size_t(submit_draws)*submit_extend)return;
        if(flights.size()>=flight_limit && spare_batches.empty())return;
        submit_async("draw_prefix");
    }
    bool has_labels()const{if(!labels.empty())return true;for(const auto& f:flights)if(!f.labels.empty())return true;return false;}
    template<class F> void each_label(F f)const{for(const auto& flight:flights)for(const auto& label:flight.labels)f(label);for(const auto& label:labels)f(label);}
    void validate_pending(NativeVulkan::GraphicsBatch& b,const std::vector<Pending>& work){
        for(const auto& p:work){unsigned status=0;std::memcpy(&status,static_cast<char*>(b.map)+p.status,4);
            if(status&16)throw std::runtime_error("RSX GPU VP indexed access/address conversion failed; no guest commit");
            if(status&4)throw std::runtime_error("RSX derivatives/implicit texture LOD in divergent flow are unsupported; no guest commit");
            if(status&2)throw std::runtime_error("RSX FP execution budget exhausted; no guest commit");
            if(status)throw std::runtime_error("RSX graphics shader error; no guest commit");}
    }
    void retire_work(NativeVulkan::GraphicsBatch& b,const std::vector<Pending>& work,const std::shared_ptr<std::vector<std::uint64_t>>& results,std::vector<Label>& completed_labels,unsigned report_floor=0){
        validate_pending(b,work);*results=b.visibility;
        for(std::size_t n=0;n<work.size();++n){const auto& p=work[n];std::uint64_t count=0;for(unsigned i=0;i<p.count;++i)count+=b.visibility.at(p.first+i);
            state.rsx.draw.pixels+=count;if(p.report && n>=report_floor)state.rsx.report_values[0]+=count;}
        publish_labels(completed_labels);
    }
    void retire(bool wait,std::uint64_t through=UINT64_MAX){
        GuestMemory::RawYield payload(state.memory);
        HostPhase phase(HostStage::flush);PerfTimer timer(fallback_stats().enabled?&fallback_stats().gpu_flush_ns:nullptr);
        if(failed)throw std::runtime_error("RSX graphics session poisoned");
        try{while(!flights.empty() && flights.front().batch->completion<=through){auto& flight=flights.front();
            {anyps3_ft::Span ft_wait(anyps3_ft::WorkerWait,flight.batch->completion,wait?1:0,wait?20000:~std::uint64_t(0));
                if(!vk.graphics_collect(*flight.batch,wait))break;}
            anyps3_ft::mark(anyps3_ft::GpuRetire,flight.batch->completion,flight.pending.size(),wait?1:0,flight.labels.size());
            anyps3_ft::current_completion=flight.batch->completion;
            retire_work(*flight.batch,flight.pending,flight.results,flight.labels,flight.report_floor);
            for(auto& completion:flight.completions)completion(*flight.batch);
            if(rsx_poll_adaptive())flight_ns=0.8*flight_ns+0.2*double(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-flight.submitted).count());
            vk.graphics_reset(*flight.batch);vk.graphics_move_stats(*flight.batch,*batch);
            if(flight.labels.empty() && flight.labels.capacity() && label_pool.size()<4)label_pool.push_back(std::move(flight.labels));
            if(flight.pending.capacity() && pending_pool.size()<4){flight.pending.clear();pending_pool.push_back(std::move(flight.pending));}
            spare_batches.push_back(std::move(flight.batch));flights.pop_front();++async_retires;
        }}catch(...){failed=true;throw;}
    }
    void submit_async(const char* reason,bool wait_for_arena=true){
        GuestMemory::RawYield payload(state.memory);
        if(failed)throw std::runtime_error("RSX graphics session poisoned");
        if(!wait_for_arena){retire(false);if(flights.size()>=flight_limit && spare_batches.empty())return;}
        if(!batch->recording && !batch->submitted && pending.empty() && labels.empty() && !batch->frames)return;
        // No new GPU instruction means the last queued prefix is also this
        // completion point. Do not create an empty submission just for a
        // flip marker or another release/report at the same queue position.
        if(!batch->recording && !batch->submitted && !batch->completion && !batch->used && pending.empty() && completions.empty() && !flights.empty() && flights.back().labels.size()+labels.size()<=256){
            auto& tail=flights.back();tail.batch->frames+=batch->frames;batch->frames=0;
            for(auto& label:labels)tail.labels.push_back(std::move(label));
            labels.clear();if(fallback_stats().enabled){++fallback_stats().flush_calls[reason];++fallback_stats().paths["completion_prefix_reuse"];}
            return;
        }
        // A label with no preceding GPU command still has a completion point.
        if(!batch->recording && !batch->submitted){const unsigned zero=0;vk.graphics_upload(*batch,&zero,4);}
        auto& stats=fallback_stats();if(stats.enabled){++stats.flush_calls[reason];++stats.flush_submits[reason];}
        ::RsxPerfCounters::add(!std::strcmp(reason,"draw_prefix")?::RsxPerfCounters::submit_draw_prefix:!std::strcmp(reason,"fifo_labels")?::RsxPerfCounters::submit_labels: ::RsxPerfCounters::submit_other);
        try{
            const auto ft_completion=vk.graphics_submit(*batch);
            anyps3_ft::mark(anyps3_ft::GpuSubmit,ft_completion,pending.size(),labels.size(),anyps3_ft::pack(reason));
            std::unique_ptr<NativeVulkan::GraphicsBatch> next;
            if(!spare_batches.empty()){next=std::move(spare_batches.back());spare_batches.pop_back();}
            else if(flights.size()<flight_limit)next=vk.graphics_batch(batch->capacity);
            else{{PerfTimer arena_timer(fallback_stats().enabled?&fallback_stats().flush_retire_time["arena_rotation"]:nullptr);retire(true,flights.front().batch->completion);}
                next=std::move(spare_batches.back());spare_batches.pop_back();}
            vk.graphics_move_stats(*batch,*next);
            flights.push_back({std::move(batch),std::move(pending),std::move(labels),std::move(query_results),rsx_poll_adaptive()?std::chrono::steady_clock::now():std::chrono::steady_clock::time_point{},std::move(completions),pending_report_floor});
            batch=std::move(next);pending.clear();pending_report_floor=0;labels.clear();completions.clear();query_results=std::make_shared<std::vector<std::uint64_t>>();++async_submits;
            if(!label_pool.empty()){labels=std::move(label_pool.back());label_pool.pop_back();}
            if(!pending_pool.empty()){pending=std::move(pending_pool.back());pending_pool.pop_back();}
        }catch(...){failed=true;throw;}
    }
    bool label_page_overlap(const Label& label,const void* pointer,std::uint64_t size)const{
        if(!size)return false;
        const auto page=GuestMemory::HostGuard::page_size(),at=reinterpret_cast<std::uintptr_t>(pointer),p=reinterpret_cast<std::uintptr_t>(label.identity());
        if(HostByteAccess::active())return at<p+label.size && p<at+size;
        return (at&~(page-1))<((p+label.size+page-1)&~(page-1)) && (p&~(page-1))<((at+size+page-1)&~(page-1));
    }
    void synchronize_labels(const void* p,std::uint64_t size,bool write){
        ::RsxPerfCounters::add(::RsxPerfCounters::label_syncs);
        auto& stats=fallback_stats();if(stats.enabled)++stats.flush_calls["label_reader"];
        PerfTimer timer(stats.enabled?&stats.flush_time["label_reader"]:nullptr);
        bool active=false;for(const auto& label:labels)if(label_page_overlap(label,p,size))active=true;
        if(active)submit_async("label_submit",false);
        std::uint64_t through=0;for(const auto& flight:flights)for(const auto& label:flight.labels)if(label_page_overlap(label,p,size))through=flight.batch->completion;
        if(!through)return;
        each_label([&](const auto& label){if(label_page_overlap(label,p,size)){
            const auto at=reinterpret_cast<std::uintptr_t>(p),target=reinterpret_cast<std::uintptr_t>(label.identity());
            const bool exact=at<target+label.size && target<at+size;
            access_trace().record(exact?"label_reader":"label_page_dependency",state,p,size,write,0,0,0,label.address,through);
        }});
        if(vk.graphics_completed()>=through)++label_ready;else ++label_waits;
        // Polling a semaphore is allowed to see the old value. In particular
        // a CPU write before completion precedes the later GPU release.
        // Reports/flip records share the same completion ordering: a poll
        // never manufactures a wait for work that has not completed yet.
        retire(false,through);
    }
    bool defer_label(std::uint64_t address,unsigned value){
        if(!observer_installed || previous_observer)return false;
        GuestMemory::ObserverPause pause(state.memory);
        auto mapping=state.memory.range(address,4,true);
        if(mapping.parts.size()!=1)return false;
        const auto& part=mapping.parts[0];
        GuestMemory::HostGuard probe(false);const bool safe=probe.add(part.data,4,*part.owner);probe.spans.clear();
        if(!safe)return false;
        // Surface/label aliases keep the existing coherent store path.
        for(const auto& surface:surfaces)if(surface.overlaps(part.data,4))return false;
        // RPCS3 nv47_sync.hpp: a same-value release cannot communicate
        // completion. Preserve queued intermediate values before using this.
        const bool queued=label_overlap(part.data,4);
        std::array<std::uint8_t,4> previous{};
        if(!queued && !guard.touches(part.data,4)){mapping.read(0,previous.data(),4);if(read_be(previous.data(),4)==value){if(fallback_stats().enabled)++fallback_stats().paths["label_same_value"];return true;}}
        if(labels.size()>=256)submit_async("label_capacity");
        watch_poll(part.data,4,true);labels.push_back({std::move(mapping),address,value,4,false,false,false,0,{}});++ownership_revision;
        if(fallback_stats().enabled){++fallback_stats().paths["label_deferred"];std::ostringstream detail;detail<<"address=0x"<<std::hex<<address<<" value=0x"<<value<<" pc=0x"<<state.pc;fallback_stats().detail("label_producer",detail.str());}
        return true;
    }
    bool defer_flip(std::uint64_t address,std::uint64_t reports,unsigned id,unsigned queue,std::uint64_t event_name,std::uint64_t event_flags){
        if(!observer_installed || previous_observer || labels.size()>254)return false;
        GuestMemory::ObserverPause pause(state.memory);
        const auto driver=state.memory.range(address,24,true),report=state.memory.range(reports,16,true);
        if(driver.parts.size()!=1 || report.parts.size()!=1)return false;
        GuestMemory::HostGuard probe(false);
        if(!probe.add(driver.parts[0].data,24,*driver.parts[0].owner) || !probe.add(report.parts[0].data,16,*report.parts[0].owner))return false;
        for(const auto& surface:surfaces)if(surface.overlaps(driver.parts[0].data,24) || surface.overlaps(report.parts[0].data,16))return false;
        Label first{driver,address,id,24,false,false,false,0,{}};first.kind=1;
        Label second{report,reports,0,16,false,false,false,0,{}};second.kind=2;second.queue=queue;second.event_name=event_name;second.event_flags=event_flags;
        labels.push_back(std::move(first));labels.push_back(std::move(second));watch_poll(driver.parts[0].data,24,true);watch_poll(report.parts[0].data,16,true);++ownership_revision;return true;
    }
    bool defer_report(std::uint64_t address,unsigned type,bool active,bool stats){
        if(!observer_installed || previous_observer)return false;
        GuestMemory::ObserverPause pause(state.memory);auto mapping=state.memory.range(address,16,true);
        if(mapping.parts.size()!=1)return false;
        const auto& part=mapping.parts[0];GuestMemory::HostGuard probe(false);const bool safe=probe.add(part.data,16,*part.owner);probe.spans.clear();if(!safe)return false;
        for(const auto& surface:surfaces)if(surface.overlaps(part.data,16))return false;
        if(labels.size()>=256)submit_async("label_capacity");
        const auto* data=part.data;
        Label report{std::move(mapping),address,type,16,true,active,stats,state.rsx.report_values[0],{}};
        // Snapshot attribution now: RESET_REPORT may clear the live pending
        // flags later, but cannot change an already issued report's prefix.
        for(const auto& flight:flights)for(std::size_t n=flight.report_floor;n<flight.pending.size();++n){const auto& p=flight.pending[n];if(p.report)report.queries.push_back({flight.results,p.first,p.count});}
        for(std::size_t n=pending_report_floor;n<pending.size();++n){const auto& p=pending[n];if(p.report)report.queries.push_back({query_results,p.first,p.count});}
        labels.push_back(std::move(report));watch_poll(data,16,true);++ownership_revision;
        if(fallback_stats().enabled){++fallback_stats().paths["report_deferred"];std::ostringstream detail;detail<<"address=0x"<<std::hex<<address<<" type="<<type<<" pc=0x"<<state.pc;fallback_stats().detail("report_producer",detail.str());}
        return true;
    }
    bool label_overlap(const void* pointer,std::uint64_t size)const{
        const auto at=reinterpret_cast<std::uintptr_t>(pointer);
        bool overlap=false;each_label([&](const auto& label){const auto p=reinterpret_cast<std::uintptr_t>(label.identity());if(at<p+label.size && p<at+size)overlap=true;});return overlap;
    }
    void publish_labels(std::vector<Label>& completed_labels){
        if(completed_labels.empty())return;
        // Publication includes a driver status read/modify/write. PAR2 raw
        // admission makes that transaction exclusive; Range enforces mapping
        // epochs and uses the same atomic storage/version API as CPU/DMA.
        GuestMemory::ObserverPause pause(state.memory);
        for(const auto& label:completed_labels)label.mapping.check_epoch();
        // release() unprotects whole pages: drop each protected page once.
        {std::vector<std::pair<std::uintptr_t,std::uintptr_t>> pages;const auto page=GuestMemory::HostGuard::page_size();
            for(const auto& label:completed_labels){const auto p=reinterpret_cast<std::uintptr_t>(label.identity());pages.push_back({p&~(page-1),(p+label.size+page-1)&~(page-1)});}
            std::sort(pages.begin(),pages.end());std::size_t n=0;
            for(const auto& r:pages){if(n && r.first<=pages[n-1].second)pages[n-1].second=std::max(pages[n-1].second,r.second);else pages[n++]=r;}
            const auto before=guard.protection_calls;
            for(std::size_t i=0;i<n;++i)guard.release(reinterpret_cast<const void*>(pages[i].first),pages[i].second-pages[i].first);
            ::RsxPerfCounters::add(::RsxPerfCounters::mprotect_labels,guard.protection_calls-before);}
        // One GPU claim covers every label of this completion prefix (the same
        // exact-span validation as a per-label ClaimTransfer, one broker round).
        // An event publication (flip event, kind 2) may wait for the RSX coordinator (owner_call), while the owner may itself wait in
        // the page broker for these label pages: the claim never spans an event. Labels before the event are committed and the claim
        // released, the event is published, and a new claim covers the rest (program order of the writes and the event is kept).
        std::unique_ptr<GuestMemory::GpuPages> claim;
        auto open_claim=[&](std::size_t first){
            if(!state.memory.concurrent())return;
            std::vector<GuestMemory::HostSpan> declared;
            for(std::size_t i=first;i<completed_labels.size();++i)for(const auto& span:ClaimTransfer::spans(completed_labels[i].mapping,0,completed_labels[i].size)){
                if(std::none_of(declared.begin(),declared.end(),[&](const auto& d){return d.pointer==span.pointer && d.size==span.size && d.pin==span.pin;}))declared.push_back(span);}
            auto& m=state.memory;
            if(m.renderer_enabled && !m.memory_owner() && !m.local_state().fault_claim){m.renderer_pin([&]{claim=std::make_unique<GuestMemory::GpuPages>(m,std::move(declared));});claim->adopt();}
            else{m.enable_page_broker();claim=std::make_unique<GuestMemory::GpuPages>(m,std::move(declared));}
            for(std::size_t i=first;i<completed_labels.size();++i)claim->commit_spans(completed_labels[i].mapping,0,0,true);
        };
        open_claim(0);
        for(std::size_t index=0;index<completed_labels.size();++index){const auto& label=completed_labels[index];invalidate_raw(label.identity(),label.size,"label_publish");
            anyps3_ft::mark(anyps3_ft::LabelPub,label.address,label.value,label.kind,anyps3_ft::current_completion);
            std::array<std::uint8_t,24> bytes{};
            if(label.kind==1){
                if(claim)claim->snapshot_into(label.mapping,0,24,bytes.data());else label.mapping.read(0,bytes.data(),24);
                write_be(bytes.data(),8,timebase_ticks());write_be(bytes.data()+16,4,label.value);write_be(bytes.data()+8,4,read_be(bytes.data()+8,4)|0x80000000u);
            }else if(label.kind==2){
                if(label.event_flags){if(!state.event)throw std::runtime_error("RSX completion has no event transport");
                    const bool claimed=bool(claim);claim.reset();
                    state.event(label.queue,label.event_name,label.event_flags);
                    if(claimed)open_claim(index);}
            }else if(label.report){
                auto count=label.base;for(const auto& p:label.queries)for(unsigned i=0;i<p.count;++i)count+=p.values->at(p.first+i);
                unsigned value=unsigned(count);if(label.value>1)value=label.value==5?((value || !label.active || !label.stats)?0u:0xffffu):((label.active && label.stats)?0xffffffffu:0u);
                const auto now=std::uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
                state.rsx.report_timestamp=std::max(now,state.rsx.report_timestamp+1);
                write_be(bytes.data(),8,state.rsx.report_timestamp);write_be(bytes.data()+8,4,value);
            }else write_be(bytes.data(),4,label.value);
            if(claim)claim->commit(label.mapping,0,bytes.data(),label.size);else label.mapping.write(0,bytes.data(),label.size);
        }
        claim.reset();
        for(const auto& label:completed_labels)watch_poll(label.identity(),label.size,false);
        if(!completed_labels.empty())++ownership_revision;
        completed_labels.clear();
    }
    bool busy=false,failed=false,allow_resident_textures=true;
    double commit_ms=0;
    GuestMemory::HostGuard guard;
    // Permissions must be removed before the last owner of a protected
    // allocation disappears (e.g. unmap + texture eviction in one FIFO turn).
    std::vector<std::shared_ptr<GuestBacking>> guard_backings;
    std::uint64_t protection_revision=0,ownership_revision=1;
    struct RawTexture {
        struct DirtyInputs {
            GuestMemory& memory;std::shared_ptr<GuestMemory::CpuDirtyWatch> watch;
            explicit DirtyInputs(GuestMemory& m):memory(m){}
            ~DirtyInputs(){if(watch)memory.unwatch_cpu_dirty(watch);}
            bool consume(){return watch && watch->consume();}
        };
        std::shared_ptr<DirtyInputs> dirty_inputs;
        bool invalid=false;
        std::shared_ptr<TexturePlan> plan;std::uint64_t id=0;
        std::vector<GuestMemory::HostGuard::Span> guarded;
        struct GuardOwner{std::shared_ptr<GuestBacking> owner;const std::uint8_t* data;std::size_t size;};
        std::vector<GuardOwner> guard_owners;
        std::shared_ptr<NativeVulkan::SampledImage> image;std::vector<unsigned> key;std::uint64_t cost=0;const std::uint8_t* origin=nullptr;std::uint64_t origin_size=0;
        const std::uint8_t* source;std::uint64_t size,last_use=0;bool trusted=false;
        std::vector<std::shared_ptr<GuestBacking>> owners;
        std::vector<std::uint8_t> snapshot;
        std::shared_ptr<NativeVulkan::GraphicsBuffer> buffer;
        bool overlaps(const void* p,std::uint64_t n)const{const auto a=reinterpret_cast<std::uintptr_t>(source),b=reinterpret_cast<std::uintptr_t>(p),o=reinterpret_cast<std::uintptr_t>(origin);return n && ((a<b+n && b<a+size) || (origin && o<b+n && b<o+origin_size));}
    };
    std::uint64_t upload_arena_limit=256*1024*1024;
    bool ensure_upload_capacity(std::uint64_t required){
        if(required<=batch->capacity)return true;
        const auto limit=std::min(upload_arena_limit,vk.graphics_storage_limit());
        std::uint64_t capacity=batch->capacity;
        while(capacity<required && capacity<=limit/2)capacity*=2;
        if(capacity<required || capacity>limit)return false;
        flush(false,nullptr,0,"upload_resize");spare_batches.clear();
        auto larger=vk.graphics_batch(capacity);vk.graphics_move_stats(*batch,*larger);batch=std::move(larger);return true;
    }

    // Compare exact blocks, including presence. Legacy mutable references and
    // capture restore cannot evade this cache; unchanged blocks are not rebuilt.
    std::vector<unsigned> register_prefix;
    std::array<RsxRegisters::value_type,2048> register_values{};
    std::array<std::uint64_t,32> register_presence{};
    std::uint64_t register_blocks_rebuilt=0;
    std::array<std::uint64_t,32> register_versions{};
    // Bank epochs at the last refresh (RsxRegisters change tracking). A block
    // whose epoch is unchanged and that was never mutably borrowed holds the
    // same presence and values; every other block is compared by value.
    std::array<std::uint64_t,32> register_epochs{};std::uint64_t register_identity=0;std::uint32_t register_values_stale=0;
    void refresh_registers(){
        const auto& bank=std::as_const(state.rsx).registers;const Reg r{state.rsx};
        const bool fresh=register_prefix.empty() || register_identity!=bank.identity;if(fresh)register_prefix.assign(2576,0);
        register_identity=bank.identity;
        static constexpr std::pair<unsigned,unsigned> defaults[]={{0xa6c,0x207},{0x394,0},{0x398,0x3f800000},{0x368,0x1d01},{0x1834,0x901},{0x388,0x3f800000},{0x308,0x207},{0x324,0x01010101},{0x378,0x1503},{0x1d7c,0xffff0000}};
        for(unsigned block=0;block<32;++block){const auto* source=bank.values.data()+block*64;auto* previous=register_values.data()+block*64;
            const bool exposed=bank.block_exposed(block),tracked=!fresh && !exposed && register_epochs[block]==bank.epochs[block];
            register_epochs[block]=bank.epochs[block];
            if(tracked)continue;
            // A never-borrowed block whose epoch moved changed presence or a value since the last refresh (a write back to the
            // old value is rebuilt too: the same words, one redundant upload), so only a borrowed block is compared, and only
            // against a current snapshot: rebuilding a never-borrowed block leaves its snapshot stale (register_values_stale).
            const bool stale=(register_values_stale>>block)&1;
            if(!fresh && exposed && !stale && register_presence[block]==bank.written[block] && !std::memcmp(source,previous,64*sizeof(*source)))continue;
            ++register_blocks_rebuilt;++register_versions[block];register_presence[block]=bank.written[block];
            if(exposed){std::copy_n(source,64,previous);register_values_stale&=~(UINT32_C(1)<<block);}else register_values_stale|=UINT32_C(1)<<block;
            // Absent registers read 0 except the reset defaults: zero the block, store present slots only (set presence bits).
            auto* out=register_prefix.data()+16+block*64;std::memset(out,0,64*sizeof(unsigned));
            for(auto bits=bank.written[block];bits;bits&=bits-1){const unsigned i=unsigned(__builtin_ctzll(bits));out[i]=source[i].second;}
            static constexpr std::uint32_t default_blocks=[]{std::uint32_t m=0;for(const auto& value:defaults)m|=1u<<(value.first/256);return m;}();
            if((default_blocks>>block)&1)for(const auto& value:defaults)if(value.first/256==block)register_prefix[16+value.first/4]=r.get(value.first,value.second);
        }
    }
    void serialize_registers(std::vector<unsigned>& data,unsigned width,unsigned height){
        refresh_registers();const auto& bank=std::as_const(state.rsx).registers;
        data.assign(register_prefix.begin(),register_prefix.end());data[0]=width;data[1]=height;data[3]=data.size();
        for(unsigned i=0;i<8;++i)if(bank.count(0x2c0+8*i) && bank.count(0x2c4+8*i))data[8]|=1u<<i;
        data[9]=bank.count(0x8cc)!=0;
    }
    void serialize_draw_registers(std::vector<unsigned>& data,unsigned width,unsigned height){
        refresh_registers();const auto& bank=std::as_const(state.rsx).registers;
        // Same contents as assign(560,0): shrinking keeps the capacity without writes, then every word is zeroed.
        data.resize(48+16*32);zero_words(data.data(),data.size());data[0]=width;data[1]=height;data[3]=data.size();
        for(unsigned i=0;i<8;++i)if(bank.count(0x2c0+8*i) && bank.count(0x2c4+8*i))data[8]|=1u<<i;
        data[9]=bank.count(0x8cc)!=0;
    }
    VertexProgramCache vertex_program;
    std::array<std::uint64_t,2> constants_versions{};
    std::vector<unsigned> vertex_constants_cache,draw_data;
    std::vector<std::uint8_t> vertex_tail;
    // ---- Vertex input reuse (ANYPS3_RSX_GEOMETRY_REUSE; geometry_reuse is a per-session copy for tests) ----
    // A draw's vertex input group (one Range of one mapping, DrawReadBatch) is the same guest bytes in most draws of most
    // frames. An entry keeps a host copy of exactly the bytes of one group, keyed by guest address and size and pinned to
    // the Range's physical identity (host pointer, backing, mapping identity). A hit copies the entry instead of reading
    // guest memory. The draw still creates the group's Range (build_vertex_groups), so map/permission validation and the
    // observer coherence point (GPU-authoritative bytes retired, which republishes them via publish_gpu_write) happen at
    // the same point as before; a changed mapping at that address never matches the identity and is read afresh.
    // Invalidation (the raw texture cache's CPU-dirty protocol, rsx_texture_cache.py dirty_inputs/raw_dirty):
    //  * each entry owns a CpuDirtyWatch over exactly its bytes, registered and consumed BEFORE the bytes are read, so a
    //    writer admitted during or after the copy leaves a set bit; each use consumes the bit and re-reads when it was set;
    //  * lock-free writers publish after their payload stores and before any following label/PUT: PPU inline/medium/slow
    //    stores (page dirty flag -> publish_cpu_write), SPU DMA and GuestMemory::transfer/Range::write (publish_cpu_write),
    //    RSX/GPU claim commits and readbacks (publish_gpu_write). A write ordered before the draw by a label the FIFO
    //    acquired is therefore seen as a set bit at the draw (property A of DrawReadBatch::read); a hit reads no guest byte,
    //    so property B (the snapshot is complete before any later label) holds trivially;
    //  * raw-pointer writers (RawPermit/HLE/syscall buffers) reach host_access(write) through the dirty filter
    //    (raw_cpu_dirty_interested), which drops every overlapping entry (invalidate_geometry);
    //  * map/unmap/configuration handoff: identity mismatch (above); entries pin their backing, so a host pointer is never
    //    reused by another backing while an entry names it.
    // Bounds: <= geometry_entry_limit entries/watches (as the 512 raw texture watches), <= geometry_byte_limit host bytes;
    // LRU eviction. Cost for writers: a store to a watched page takes the medium path, one to a watched byte also scans the
    // watch list. An address whose entry was invalidated twice is never cached again (rewritten buffers stay on the direct
    // path), and every 4096 reads the entries already invalidated are dropped, so a rewritten buffer that is not drawn
    // again does not stay watched.
    struct GeometryEntry {
        std::shared_ptr<RawTexture::DirtyInputs> dirty;std::vector<std::uint8_t> bytes;
        const std::uint8_t* data=nullptr;std::shared_ptr<GuestBacking> owner;std::uint64_t mapping_id=0,last_use=0;
    };
    static constexpr unsigned geometry_entry_limit=512;static constexpr std::uint64_t geometry_byte_limit=64ull<<20,geometry_entry_bytes=4ull<<20;
    bool geometry_reuse=rsx_geometry_reuse();
    std::unordered_map<std::uint64_t,GeometryEntry> geometry_entries;std::unordered_map<std::uint64_t,unsigned> geometry_rewrites;
    std::uint64_t geometry_bytes=0,geometry_clock=0,geometry_hits=0,geometry_misses=0,geometry_rereads=0,geometry_evictions=0;
    // Counts one invalidation by a write of the entry at this guest address; true = never cache it again.
    bool geometry_rewritten(std::uint64_t address){
        if(geometry_rewrites.size()>=4096)geometry_rewrites.clear();
        return ++geometry_rewrites[address]>=2;
    }
    void geometry_erase(std::unordered_map<std::uint64_t,GeometryEntry>::iterator it,bool rewritten){
        if(rewritten)(void)geometry_rewritten(it->first>>32);
        geometry_bytes-=it->second.bytes.size();geometry_entries.erase(it);
    }
    void invalidate_geometry(const void* p,std::uint64_t n){
        if(!n || (geometry_entries.empty() && index_entries.empty()))return;
        const auto a=reinterpret_cast<std::uintptr_t>(p);
        for(auto it=geometry_entries.begin();it!=geometry_entries.end();){const auto b=reinterpret_cast<std::uintptr_t>(it->second.data);
            if(b<a+n && a<b+it->second.bytes.size()){auto next=std::next(it);geometry_erase(it,true);it=next;}else ++it;}
        for(auto it=index_entries.begin();it!=index_entries.end();){const auto b=reinterpret_cast<std::uintptr_t>(it->second.data);
            if(b<a+n && a<b+it->second.size){auto next=std::next(it);index_erase(it,true);it=next;}else ++it;}
    }
    void clear_geometry(){geometry_entries.clear();index_entries.clear();geometry_bytes=0;}
    // Copies the bytes of one vertex input Range to out; false = not reusable here (the caller reads the Range itself).
    bool geometry_read(const GuestMemory::Range& range,std::uint8_t* out){
        if(!geometry_reuse || !range.memory->concurrent() || range.memory!=&state.memory || range.parts.size()!=1 || !range.size ||
           range.size>geometry_entry_bytes || range.parts[0].size!=range.size || (!state.memory.memory_owner() && !state.memory.renderer_owner()))return false;
        const auto& part=range.parts[0];const std::uint64_t key=(range.address<<32)|range.size;
        if(!(++geometry_clock&4095))for(auto it=geometry_entries.begin();it!=geometry_entries.end();){
            const bool rewritten=it->second.dirty->watch->dirty.load(std::memory_order_acquire);
            if(rewritten || geometry_clock-it->second.last_use>(1u<<20)){auto next=std::next(it);geometry_erase(it,rewritten);it=next;}else ++it;}
        if(!(geometry_clock&4095))for(auto it=index_entries.begin();it!=index_entries.end();){
            const bool rewritten=it->second.dirty->watch->dirty.load(std::memory_order_acquire);
            if(rewritten || geometry_clock-it->second.last_use>(1u<<20)){auto next=std::next(it);index_erase(it,rewritten);it=next;}else ++it;}
        auto it=geometry_entries.find(key);
        if(it!=geometry_entries.end()){auto& e=it->second;
            if(e.data==part.data && e.owner==part.owner && e.mapping_id==part.mapping_id){e.last_use=geometry_clock;
                if(!e.dirty->consume()){std::memcpy(out,e.bytes.data(),range.size);++geometry_hits;return true;}
                // Written since the copy: the bit is consumed (clear before the read), refresh the copy under the same watch.
                if(geometry_rewritten(range.address)){geometry_erase(it,false);return false;}
                range.read(0,out,range.size);std::memcpy(e.bytes.data(),out,range.size);++geometry_rereads;return true;
            }
            geometry_erase(it,false);
        }
        {const auto rewrites=geometry_rewrites.find(range.address);if(rewrites!=geometry_rewrites.end() && rewrites->second>=2)return false;}
        while(!(geometry_entries.empty() && index_entries.empty()) && (geometry_entries.size()+index_entries.size()>=geometry_entry_limit || geometry_bytes+range.size>geometry_byte_limit))geometry_evict();
        GeometryEntry e;
        try{e.dirty=std::make_shared<RawTexture::DirtyInputs>(state.memory);e.dirty->watch=state.memory.watch_cpu_dirty(range);}
        catch(const std::exception&){return false;}   // not registrable (foreign thread, stale epoch): the caller's read raises any error
        e.dirty->consume();   // clear BEFORE the read: a writer admitted during/after it leaves a new bit
        range.read(0,out,range.size);
        e.bytes.assign(out,out+range.size);e.data=part.data;e.owner=part.owner;e.mapping_id=part.mapping_id;e.last_use=geometry_clock;
        geometry_bytes+=range.size;geometry_entries.emplace(key,std::move(e));++geometry_misses;return true;
    }
    // ---- Index list reuse (same switch, bounds and invalidation protocol as the vertex input entries above) ----
    // An entry keeps the decoded index list of one contiguous index span (rsx_execute_draw's union read), keyed by guest
    // address and byte size, valid only for the decode parameters it was decoded with (width, restart, restart index,
    // base index) and pinned to the span's physical identity. A use first creates the span's translation Range, which
    // performs the map epoch/permission validation and the observer coherence point that the transfer performed (a
    // GPU-authoritative span is retired and republished via publish_gpu_write), then consumes the entry's CPU-dirty bit.
    // A miss registers the watch and consumes it BEFORE the transfer reads the bytes (index_prepare), and stores the
    // decoded list afterwards (index_store); an exception in between drops the token (its watch is unregistered).
    struct IndexEntry {
        std::shared_ptr<RawTexture::DirtyInputs> dirty;const std::uint8_t* data=nullptr;std::shared_ptr<GuestBacking> owner;
        std::uint64_t mapping_id=0,last_use=0;unsigned size=0,width=0,restart_index=0,base=0;bool restart=false,any=false;
        std::uint32_t largest=0,smallest=0;std::vector<std::uint32_t> values;
    };
    std::unordered_map<std::uint64_t,IndexEntry> index_entries;std::uint64_t index_hits=0,index_misses=0;
    void index_erase(std::unordered_map<std::uint64_t,IndexEntry>::iterator it,bool rewritten){
        if(rewritten)(void)geometry_rewritten(it->first>>32);
        geometry_bytes-=it->second.values.size()*4;index_entries.erase(it);
    }
    bool index_cacheable(unsigned bytes)const{
        return geometry_reuse && state.memory.concurrent() && bytes && bytes<=geometry_entry_bytes && (state.memory.memory_owner() || state.memory.renderer_owner());
    }
    // The decoded list of the span [address,address+bytes) for these parameters into out; false = decode it (nothing changed).
    bool index_read(std::uint64_t address,unsigned bytes,unsigned width,bool restart,unsigned restart_index,unsigned base,
                    std::vector<std::uint32_t>& out,std::uint32_t& largest,std::uint32_t& smallest,bool& any){
        if(!index_cacheable(bytes))return false;
        const std::uint64_t key=(address<<32)|bytes;auto it=index_entries.find(key);
        if(it==index_entries.end())return false;
        auto& e=it->second;GuestMemory::Range range;
        try{range=state.memory.range_translated(address,bytes);}catch(const std::exception&){return false;}   // the transfer raises it
        if(range.parts.size()!=1 || range.parts[0].data!=e.data || range.parts[0].owner!=e.owner || range.parts[0].mapping_id!=e.mapping_id){index_erase(it,false);return false;}
        if(e.dirty->consume()){index_erase(it,true);return false;}
        if(e.width!=width || e.restart!=restart || e.restart_index!=restart_index || e.base!=base){index_erase(it,false);return false;}
        e.last_use=++geometry_clock;out.assign(e.values.begin(),e.values.end());largest=e.largest;smallest=e.smallest;any=e.any;++index_hits;return true;
    }
    struct IndexToken {std::uint64_t key;std::shared_ptr<RawTexture::DirtyInputs> dirty;const std::uint8_t* data;std::shared_ptr<GuestBacking> owner;std::uint64_t mapping_id;};
    std::optional<IndexToken> index_prepare(std::uint64_t address,unsigned bytes){
        if(!index_cacheable(bytes))return std::nullopt;
        {const auto rewrites=geometry_rewrites.find(address);if(rewrites!=geometry_rewrites.end() && rewrites->second>=2)return std::nullopt;}
        try{const auto range=state.memory.range_translated(address,bytes);if(range.parts.size()!=1 || range.parts[0].size!=bytes)return std::nullopt;
            IndexToken token{(address<<32)|bytes,std::make_shared<RawTexture::DirtyInputs>(state.memory),range.parts[0].data,range.parts[0].owner,range.parts[0].mapping_id};
            token.dirty->watch=state.memory.watch_cpu_dirty(range);token.dirty->consume();   // clear BEFORE the transfer reads the bytes
            return token;
        }catch(const std::exception&){return std::nullopt;}   // the transfer raises any error
    }
    void index_store(IndexToken&& token,unsigned width,bool restart,unsigned restart_index,unsigned base,const std::vector<std::uint32_t>& values,
                     std::uint32_t largest,std::uint32_t smallest,bool any){
        if(values.size()*4>geometry_entry_bytes)return;
        {auto old=index_entries.find(token.key);if(old!=index_entries.end())index_erase(old,false);}
        while(!(geometry_entries.empty() && index_entries.empty()) && (geometry_entries.size()+index_entries.size()>=geometry_entry_limit || geometry_bytes+values.size()*4>geometry_byte_limit))geometry_evict();
        IndexEntry e;e.dirty=std::move(token.dirty);e.data=token.data;e.owner=std::move(token.owner);e.mapping_id=token.mapping_id;e.last_use=++geometry_clock;
        e.size=unsigned(token.key&UINT32_MAX);e.width=width;e.restart=restart;e.restart_index=restart_index;e.base=base;e.any=any;e.largest=largest;e.smallest=smallest;e.values=values;
        geometry_bytes+=values.size()*4;index_entries.emplace(token.key,std::move(e));++index_misses;
    }
    // Least recently used vertex or index entry.
    void geometry_evict(){
        auto v=std::min_element(geometry_entries.begin(),geometry_entries.end(),[](const auto& x,const auto& y){return x.second.last_use<y.second.last_use;});
        auto i=std::min_element(index_entries.begin(),index_entries.end(),[](const auto& x,const auto& y){return x.second.last_use<y.second.last_use;});
        if(i==index_entries.end() || (v!=geometry_entries.end() && v->second.last_use<i->second.last_use))geometry_erase(v,false);else index_erase(i,false);
        ++geometry_evictions;
    }
    // DrawReadBatch::read with reuse: the same per-input order, memory check and reads for every input not served above.
    void read_inputs(const DrawReadBatch& batch,std::uint8_t* output){
        if(!batch.count)return;
        if(!geometry_reuse || !batch.inputs[0].range->memory->concurrent()){batch.read(output);return;}
        const auto* memory=batch.inputs[0].range->memory;
        for(unsigned i=0;i<batch.count;++i){const auto& input=batch.inputs[i];const auto& range=*input.range;
            if(range.memory!=memory)throw std::runtime_error("RSX mixed draw input memory");
            if(!geometry_read(range,output+input.destination))range.read(0,output+input.destination,range.size);}
    }
    std::array<bool,468*4> vertex_constants_written{};
    std::uint64_t vertex_constants_version=0,fragment_constants_version=0;
    std::vector<unsigned> fragment_constants_cache;
    // The VP constant bank is uploaded in 15 chunks of 32 vec4 plus the
    // written-mask block; a draw re-uploads only chunks whose words changed.
    // Shaders reach chunk c through the per-draw table (state_[w[2]+c]).
    static constexpr unsigned constant_chunk_words=128,constant_chunks=(468*4+constant_chunk_words-1)/constant_chunk_words;
    std::array<std::uint64_t,constant_chunks+1> constant_chunk_versions{};std::uint64_t constant_chunk_clock=0;
    std::array<unsigned,16> constant_table{};std::uint64_t constant_table_version=0;const void* constant_table_batch=nullptr;
    std::array<std::uint64_t,constant_chunks> constants_chunk_seen{};std::uint64_t constants_identity=0;
    void refresh_vertex_constants(){
        constexpr unsigned words=468*4;
        const auto& gpu=std::as_const(state.rsx);
        const std::array<std::uint64_t,2> versions{gpu.transform_constants.version(),gpu.transform_constants_written.version()};
        if(vertex_constants_cache.empty() || constants_versions!=versions){
            const bool fresh=vertex_constants_cache.size()!=words+468,masks=fresh || constants_versions[1]!=versions[1];
            constants_versions=versions;++vertex_constants_version;
            if(fresh)vertex_constants_cache.assign(words+468,0);
            // A chunk whose write version is unchanged (same never-borrowed bank) holds the cached words: skip its compare.
            static_assert(decltype(gpu.transform_constants)::chunk==constant_chunk_words,"constant chunks follow the bank's write chunks");
            const bool tracked=!fresh && !gpu.transform_constants.borrowed() && constants_identity==gpu.transform_constants.identity;
            constants_identity=gpu.transform_constants.identity;
            for(unsigned c=0;c<constant_chunks;++c){const unsigned first=c*constant_chunk_words,n=std::min(constant_chunk_words,words-first);
                const auto seen=gpu.transform_constants.chunk_version(c);
                if(tracked && constants_chunk_seen[c]==seen)continue;
                constants_chunk_seen[c]=seen;
                const auto* source=gpu.transform_constants.data()+first;auto* cached=vertex_constants_cache.data()+first;
                if(fresh || std::memcmp(source,cached,n*sizeof(unsigned))){std::copy_n(source,n,cached);constant_chunk_versions[c]=++constant_chunk_clock;}}
            if(masks){std::copy_n(gpu.transform_constants_written.begin(),words,vertex_constants_written.begin());
                for(unsigned n=0;n<468;++n){unsigned mask=0;for(unsigned c=0;c<4;++c)mask|=unsigned(vertex_constants_written[n*4+c])<<c;vertex_constants_cache[words+n]=mask;}
                constant_chunk_versions[constant_chunks]=++constant_chunk_clock;}
        }
    }
    void append_vertex_constants(std::vector<unsigned>& data){
        refresh_vertex_constants();data.insert(data.end(),vertex_constants_cache.begin(),vertex_constants_cache.end());
    }
    void refresh_fragment_constants(const std::vector<FpInst>& fp){
        bool changed=fragment_constants_cache.size()!=std::max<std::size_t>(4,fp.size()*4);
        if(!changed)for(unsigned i=0;i<fp.size();++i)if(std::memcmp(fragment_constants_cache.data()+4*i,fp[i].constant.data(),16)){changed=true;break;}
        if(changed){fragment_constants_cache.clear();for(const auto& i:fp)rsxvk::append(fragment_constants_cache,i.constant);
            if(fragment_constants_cache.empty())fragment_constants_cache.resize(4,0);
            ++fragment_constants_version;}
    }
    void bind_draw_blocks(std::vector<unsigned>& data,bool direct){
        std::array<NativeVulkan::GraphicsStateBlock,49> blocks{};
        for(unsigned i=0;i<32;++i)blocks[i]={register_versions[i]*2,register_prefix.data()+16+i*64,64};
        std::array<unsigned,64> last;
        if(direct && !std::as_const(state.rsx).registers.count(0x1ff4)){
            std::copy_n(blocks[31].data,64,last.begin());last[(0x1ff4/4)%64]=0x3fffff;
            blocks[31].data=last.data();++blocks[31].version;
        }
        constexpr unsigned words=468*4;
        for(unsigned c=0;c<constant_chunks;++c)blocks[32+c]={constant_chunk_versions[c],vertex_constants_cache.data()+c*constant_chunk_words,std::min(constant_chunk_words,words-c*constant_chunk_words)};
        blocks[32+constant_chunks]={constant_chunk_versions[constant_chunks],vertex_constants_cache.data()+words,468};
        blocks[48]={fragment_constants_version,fragment_constants_cache.data(),unsigned(fragment_constants_cache.size())};
        vk.graphics_state_blocks(*batch,blocks);
        std::array<unsigned,16> table{};for(unsigned c=0;c<=constant_chunks;++c)table[c]=unsigned(batch->state_offsets[32+c]);
        if(table!=constant_table || constant_table_batch!=batch.get() || !constant_table_version){constant_table=table;constant_table_batch=batch.get();++constant_table_version;}
        vk.graphics_state_blocks(*batch,std::array<NativeVulkan::GraphicsStateBlock,1>{{{constant_table_version,constant_table.data(),16}}},49);
        for(unsigned i=0;i<32;++i)data[16+i]=batch->state_offsets[i];
        data[2]=batch->state_offsets[49];data[14]=batch->state_offsets[48];
    }
    struct SurfaceImage {std::shared_ptr<NativeVulkan::GraphicsImage> image;std::uint64_t use;};
    std::vector<SurfaceImage> surface_images;
    std::uint64_t surface_image_bytes=0,surface_image_hits=0,surface_image_misses=0;
    std::shared_ptr<NativeVulkan::GraphicsImage> surface_image(VkFormat f,unsigned w,unsigned h,unsigned layers,bool depth=false){
        for(auto& e:surface_images){const auto& i=e.image;
            if(i->format!=f || i->width!=w || i->height!=h || i->layers!=layers || i->depth!=depth)continue;
            if(batch->pins.contains(i.get()))continue;
            bool active=false;for(const auto& flight:flights)if(flight.batch->pins.contains(i.get())){active=true;break;}
            if(active)continue;
            for(const auto& surface:surfaces)if(surface.image==i){active=true;break;}
            if(active)continue;
            // The framebuffer cache holds images weakly (handles, not pixel contents); command-buffer pins, RTT views
            // and active surfaces still retain the image and prevent its reuse.
            if(i.use_count()!=1)continue;
            e.use=++resource_clock;batch->pins.push_back(i);++surface_image_hits;return i;
        }
        ++surface_image_misses;auto image=vk.graphics_image(*batch,f,w,h,layers,depth);
        // The equal-footprint GPU path keeps ~67 MiB live in the measured
        // frame; retain a bounded working set instead of reallocating it.
        const std::uint64_t budget=96*1024*1024ull*(vk.graphics_render_scale==1?1:1+vk.graphics_render_scale*vk.graphics_render_scale);
        while(!surface_images.empty() && (surface_images.size()>=64 || surface_image_bytes+image->memory_bytes>budget)){
            auto old=std::min_element(surface_images.begin(),surface_images.end(),[](const auto& a,const auto& b){return a.use<b.use;});
            surface_image_bytes-=old->image->memory_bytes;surface_images.erase(old);
        }
        if(image->memory_bytes<=budget){surface_image_bytes+=image->memory_bytes;surface_images.push_back({image,++resource_clock});}
        return image;
    }
    struct HistoryImage {VkFormat format;unsigned slot,width,height,samples;std::uint64_t last_use;std::shared_ptr<NativeVulkan::GraphicsImage> image;};
    std::vector<HistoryImage> history_images;
    std::shared_ptr<NativeVulkan::GraphicsImage> history_image(VkFormat format,unsigned width,unsigned height,unsigned samples,unsigned slot){
        for(auto& entry:history_images)if(entry.slot==slot && entry.format==format && entry.width==width && entry.height==height && entry.samples==samples){entry.last_use=++resource_clock;return entry.image;}
        auto image=vk.graphics_image(*batch,format,width,height,samples);
        auto bytes=[&]{std::uint64_t n=0;for(const auto& e:history_images)n+=e.image->memory_bytes;return n;};
        while(!history_images.empty() && (history_images.size()>=8 || bytes()+image->memory_bytes>(128*1024*1024ull*(vk.graphics_render_scale==1?1:1+vk.graphics_render_scale*vk.graphics_render_scale)))){
            auto old=std::min_element(history_images.begin(),history_images.end(),[](const auto& a,const auto& b){return a.last_use<b.last_use;});history_images.erase(old);}
        if(image->memory_bytes<=(128*1024*1024ull*(vk.graphics_render_scale==1?1:1+vk.graphics_render_scale*vk.graphics_render_scale)))history_images.push_back({format,slot,width,height,samples,++resource_clock,image});
        return image;
    }
    std::vector<RawTexture> raw_textures;
    std::uint64_t raw_hits=0,raw_misses=0,raw_invalidations=0,raw_bytes=0,resource_clock=0,raw_evictions=0,surface_evictions=0;
''' + TEXTURE_CACHE_MEMBERS + SAMPLED_MEMBERS + RTT_MEMBERS + REBIND_MEMBERS + VISUAL_HISTORY_MEMBERS + CONDITIONAL_MEMBERS + r'''
    std::shared_ptr<NativeVulkan::GraphicsBuffer> texture_buffer(const Texture& t){
        if(state.coordinator_maps)state.maps=state.memory.regions;
        HostPhase phase(HostStage::texture_upload);
        const auto* source=t.levels[0].data;const auto size=t.untiled?t.untiled->size():t.storage_size;
        std::uint64_t reuse=0;std::shared_ptr<RawTexture::DirtyInputs> dirty;
        for(auto& entry:raw_textures)if(entry.buffer && entry.source==source && entry.size==size && entry.dirty_inputs){
            if(!raw_dirty(entry) && entry.trusted){++raw_hits;entry.last_use=++resource_clock;return entry.buffer;}
            reuse=entry.id;dirty=entry.dirty_inputs;break;}
        if(!dirty)dirty=dirty_inputs(t,source,size);
        bool snapshot_read=false;for(const auto& entry:raw_textures)if(entry.buffer && !entry.trusted && entry.source==source && entry.size==size){snapshot_read=true;break;}
        if(snapshot_read)state.memory.notify_host(source,size,false);
        struct ReadWatch {Session& owner;const Texture& texture;const void* source;unsigned size;
            ReadWatch(Session& o,const Texture& t,const void* p,unsigned n):owner(o),texture(t),source(p),size(n){if(!t.untiled && !owner.state.memory.concurrent())owner.watch_span(p,n,true);}
            ~ReadWatch(){if(!texture.untiled && !owner.state.memory.concurrent())owner.watch_span(source,size,false);}
        } watch(*this,t,source,size);
        std::vector<std::uint8_t> immutable;const auto* upload=source;
        if(state.memory.concurrent() && !t.untiled){immutable.resize(size);pinned_host_read(state.memory,source,immutable.data(),size);upload=immutable.data();}
        for(auto& entry:raw_textures)if(entry.buffer && entry.source==source && entry.size==size){
            if(!entry.invalid && (entry.trusted || std::memcmp(entry.snapshot.data(),upload,size)==0)){++raw_hits;entry.last_use=++resource_clock;return entry.buffer;}
            if(!entry.dirty_inputs)erase_raw(&entry-raw_textures.data(),"snapshot_mismatch");
            break;
        }
        if(batch->used+size+32*1024*1024>batch->capacity)submit_async("upload_capacity");
        trim_raw(reuse,size);
        RawTexture entry{};entry.dirty_inputs=std::move(dirty);entry.source=source;entry.size=size;entry.last_use=++resource_clock;
        if(t.untiled)entry.owners.push_back(t.untiled);
        else for(const auto& region:state.maps){const auto p=reinterpret_cast<std::uintptr_t>(source),base=reinterpret_cast<std::uintptr_t>(region.bytes->data());
            if(p>=base && p-base<=region.bytes->size() && size<=region.bytes->size()-(p-base)){entry.owners.push_back(region.bytes);break;}}
        if(entry.owners.empty())throw std::runtime_error("RSX texture upload has no pinned backing");
        GuestMemory::HostGuard probe(false);entry.trusted=observer_installed && !t.untiled && probe.add(source,size,*entry.owners[0]);probe.spans.clear();
        if(!entry.trusted)entry.snapshot.assign(upload,upload+size);
        entry.buffer=vk.graphics_buffer(*batch,upload,size);entry.cost=size;auto result=entry.buffer;++raw_misses;store_raw(std::move(entry),reuse);return result;
    }
    bool observer_installed=false;std::uint64_t observer_lifetime=0;
    GuestMemory::HostObserver previous_observer;
    explicit Session(NativeVulkan& v,RendererContext s):vk(v),state(std::move(s)),batch(v.graphics_batch()){
        if(const auto* value=std::getenv("ANYPS3_RSX_SUBMIT_DRAWS")){
            char* end=nullptr;const auto n=std::strtoul(value,&end,10);
            if(!*value || *end || *value=='-' || n>1000)throw std::runtime_error("invalid RSX draw submit budget");
            submit_draws=n;
        }
    }
    explicit Session(NativeVulkan& v,PpuState& s):Session(v,RendererContext{s.memory,s.rsx,s.pc,s.thread_id,
        [events=&s.events,wake=&s.reschedule](unsigned id,std::uint64_t name,std::uint64_t flags){
            auto queue=std::find_if(events->queues.begin(),events->queues.end(),[&](const auto& q){return q.id==id;});
            if(queue==events->queues.end() || queue->data.size()>=queue->capacity)throw std::runtime_error("RSX deferred flip event cannot be delivered");
            queue->data.push_back({name,0,flags,0});*wake=true;
        },s.memory.regions,true}){}
    void issuer(std::uint32_t pc,std::uint32_t tid){state.pc=pc;state.thread_id=tid;}
    void map_snapshot(std::vector<GuestMemory::Region> maps){state.maps=std::move(maps);context_validated=false;}
    ~Session(){GuestMemory::RawPermit metadata(state.memory);guard.disarm();clear_raw(nullptr);clear_geometry();if(observer_installed)state.memory.host_access_observer=std::move(previous_observer);}
    void observe(){
        // Session lifecycle is confined to the renderer/map owner. A stable
        // observer needs no global raw admission on every FIFO invocation.
        if(observer_installed)return;
        GuestMemory::RawPermit metadata(state.memory);
        // Bind is also usable before observe(). Those layout entries were
        // never registered; a delta merge must not mistake them for watches.
        surface_watches.clear();
        previous_observer=std::move(state.memory.host_access_observer);observer_installed=true;
        state.memory.host_access_observer=[this](const void* p,std::uint64_t n,bool write){host_access(p,n,write);if(previous_observer)previous_observer(p,n,write);};
        observer_lifetime=state.memory.host_access_observer.lifetime.load(std::memory_order_acquire);
        state.memory.host_access_observer.filtered=!previous_observer;
        state.memory.host_access_observer.supports_exact_bytes=true;
        for(auto& entry:raw_textures)if(!entry.dirty_inputs)raw_spans(entry,[&](const void* p,std::uint64_t n){watch_span(p,n,true);});
        refresh_surface_watches();
    }
    std::uint64_t reported_protection=0;
    void report_protection(){::RsxPerfCounters::add(::RsxPerfCounters::mprotect,guard.protection_calls-reported_protection);reported_protection=guard.protection_calls;}
    void leave_fifo(){
        // A coordinator map barrier also retires pins for removed mappings;
        // HostGuard keeps any protected backing alive until disarm/rearm below.
        if(state.coordinator_maps)state.maps=state.memory.regions;
        // Feed the queue before returning to PPU/SPU; the usual reader then
        // sees a completed prefix instead of submitting at the first load.
        ::RsxPerfCounters::add(::RsxPerfCounters::leave_fifo);
        report_protection();
        struct ReportProtection {Session& s;~ReportProtection(){s.report_protection();}} report_on_exit{*this};
        if(!labels.empty())submit_async("fifo_labels");
        commit_edges();
        guard.synchronize=[this]{
            GuestMemory::ObserverPause pause(state.memory);enter_fifo();flush(true,nullptr,0,"guard_fault");
            // A fault may be a raw read or write. Conservatively invalidate
            // both image ownership and all reservations before retrying it.
            {GuestMemory::RawPermit metadata(state.memory);
                for(const auto& surface:surfaces)if(surface.target.mapping)surface.target.mapping->mark(0,surface.target.mapping->size);}
            clear_surfaces();clear_raw("guard_fault");
        };
        // One allocator-edge target must not evict every unrelated resident
        // surface. Retained pointers still require a commit for that target.
        std::vector<std::pair<const void*,std::uint64_t>> unprotectable;
        for(const auto& surface:surfaces){const auto& t=surface.target;
            if(surface.protectable)continue;
            if(fallback_stats().enabled){++fallback_stats().paths["unprotectable_allocator_edge"];
                fallback_stats().detail("unprotectable", "width="+std::to_string(surface.width)+" height="+std::to_string(surface.height)+" offset="+std::to_string(t.offset)+" tile="+std::to_string(bool(t.tile)));}
            if(!surface.physical.empty()){const auto& span=surface.physical.front();unprotectable.push_back({reinterpret_cast<const void*>(span.begin),span.end-span.begin});}
        }
        for(const auto& span:unprotectable)flush(true,span.first,span.second,"unprotectable");
        if(!unprotectable.empty()){
            surfaces.erase(std::remove_if(surfaces.begin(),surfaces.end(),[&](const auto& surface){
                for(const auto& span:unprotectable)if(surface.overlaps(span.first,span.second))return true;
                return false;}),surfaces.end());refresh_surface_watches();
        }
        if(protection_revision==ownership_revision && (!guard.spans.empty() || !guard.readonly_spans.empty() || (surfaces.empty() && raw_textures.empty() && !has_labels())))return;
        ::RsxPerfCounters::add(::RsxPerfCounters::guard_rearm);
        guard.spans.clear();guard.readonly_spans.clear();
        std::vector<std::shared_ptr<GuestBacking>> next_backings;
        auto pin=[&](const auto& owner){if(std::find(next_backings.begin(),next_backings.end(),owner)==next_backings.end())next_backings.push_back(owner);};
        for(const auto& surface:surfaces){
            for(const auto& part:surface.target.mapping->parts){pin(part.owner);const auto base=reinterpret_cast<std::uintptr_t>(part.owner->data()),at=reinterpret_cast<std::uintptr_t>(part.data);
                if(at<base || at-base>part.owner->size() || part.size>part.owner->size()-(at-base))throw std::runtime_error("RSX resident backing changed");}
            for(const auto& span:surface.protection){bool pinned=false;for(const auto& part:surface.target.mapping->parts){const auto base=reinterpret_cast<std::uintptr_t>(part.owner->data());if(span.begin>=base && span.end<=base+part.owner->size()){pinned=true;break;}}if(!pinned)throw std::runtime_error("RSX resident protection backing changed");}
            for(const auto& span:surface.protection)guard.add_span(span);
        }
        each_label([&](const auto& label){pin(label.owner());if(!guard.add(label.identity(),label.size,*label.owner()))throw std::runtime_error("RSX deferred label backing changed");});
        for(auto& entry:raw_textures)if(entry.trusted){if(entry.plan){for(const auto& owner:entry.guard_owners){pin(owner.owner);if(owner.data!=owner.owner->data() || owner.size!=owner.owner->size())throw std::runtime_error("RSX texture backing guard changed");}
            for(const auto& span:entry.guarded)guard.add_readonly_span(span);}
            else{pin(entry.owners[0]);if(!guard.add(entry.source,entry.size,*entry.owners[0],true))throw std::runtime_error("RSX texture backing guard changed");}}
        if(static const bool trace=std::getenv("ANYPS3_RSX_GUARD_TRACE")!=nullptr;trace){
            std::vector<GuestMemory::HostGuard::Span> added,removed;
            GuestMemory::HostGuard::difference(guard.spans,guard.armed,[&](auto span){added.push_back(span);});
            GuestMemory::HostGuard::difference(guard.armed,guard.spans,[&](auto span){removed.push_back(span);});
            auto kind=[&](const GuestMemory::HostGuard::Span& span){const void* p=reinterpret_cast<const void*>(span.begin);const auto n=span.end-span.begin;
                for(const auto& surface:surfaces)for(const auto& q:surface.protection)if(q.begin<span.end && span.begin<q.end)return surface.partial?"partial":"surface";
                if(label_overlap(p,n)){return "label";}
                bool page_label=false;each_label([&](const auto& l){if(label_page_overlap(l,p,n))page_label=true;});return page_label?"label_page":"other";};
            for(const auto& span:added)std::fprintf(stderr,"RSX GUARD add %s pages=%llu\n",kind(span),(unsigned long long)((span.end-span.begin)>>12));
            for(const auto& span:removed)std::fprintf(stderr,"RSX GUARD remove pages=%llu\n",(unsigned long long)((span.end-span.begin)>>12));
        }
        {const auto before=guard.protection_calls;guard.arm();::RsxPerfCounters::add(::RsxPerfCounters::mprotect_arm,guard.protection_calls-before);}
        guard_backings=std::move(next_backings);protection_revision=ownership_revision;
    }
    void flush(bool download=true,const void* pointer=nullptr,std::uint64_t size=0,const char* reason="explicit"){
        GuestMemory::RawYield payload(state.memory);
        HostPhase phase(HostStage::flush);
        if(failed)throw std::runtime_error("RSX graphics session poisoned");
        if(busy)return;
        anyps3_ft::Span ft_flush(anyps3_ft::WorkerWait,anyps3_ft::pack(reason),download?2:3,100000);
        {PerfTimer retire_timer(fallback_stats().enabled?&fallback_stats().flush_retire_time[reason]:nullptr);retire(true);}
        auto& stats=fallback_stats();if(stats.enabled)++stats.flush_calls[reason];
        bool dirty_read=false;if(download)for(const auto& surface:surfaces)if(surface.dirty && (!pointer || surface.overlaps(pointer,size))){dirty_read=true;break;}
        if(!batch->recording && !dirty_read && pending.empty() && labels.empty() && completions.empty()){
            // Scanout can finish a readback directly before releasing its
            // staging bytes. Its signalled fence/command pool still need reset.
            if(batch->used || batch->visibility_count || !batch->pins.empty() || !batch->buffer_pins.empty() || batch->signal)vk.graphics_reset(*batch);
            return;
        }
        PerfTimer reason_timer(stats.enabled?&stats.flush_time[reason]:nullptr);
        PerfTimer flush_timer(fallback_stats().enabled?&fallback_stats().gpu_flush_ns:nullptr);
        struct Guard{bool& b;Guard(bool& v):b(v){b=true;}~Guard(){b=false;}} guard(busy);
        struct Read {Surface* surface;std::uint64_t color,stencil;bool packed;};std::vector<Read> reads;
        auto finish_reads=[&]{try{
            if(stats.enabled && batch->recording)++stats.flush_submits[reason];
            if(stats.enabled)stats.flush_readbacks[reason]+=reads.size();
            if(!reads.empty()){::RsxPerfCounters::add(::RsxPerfCounters::readback_flushes);::RsxPerfCounters::add(::RsxPerfCounters::readback_surfaces,reads.size());}
            vk.graphics_finish(*batch);
            retire_work(*batch,pending,query_results,labels,pending_report_floor);pending.clear();pending_report_floor=0;
            for(auto& completion:completions)completion(*batch);
            completions.clear();
            query_results=std::make_shared<std::vector<std::uint64_t>>();
            const auto commit_start=std::chrono::steady_clock::now();
            for(const auto& read:reads){auto& surface=*read.surface;
                if(read.packed){for(const auto& run:surface.readback->runs)this->guard.release(run.data,run.size);}
                else for(const auto& span:surface.physical)this->guard.release(reinterpret_cast<const void*>(span.begin),span.end-span.begin);
                ++ownership_revision;
                if(read.packed){GuestMemory::ObserverPause pause(state.memory);
                    SurfaceBytes transfer(surface,true,true);
                    for(const auto& run:surface.readback->runs){const auto* bytes=static_cast<const std::uint8_t*>(batch->map)+read.color+run.offset;
                        transfer.write(run.guest_offset,bytes,run.size);}
                    transfer.commit();
                }else commit_pixels(state.memory,surface,static_cast<const std::uint8_t*>(batch->map)+read.color,
                    read.stencil==UINT64_MAX?nullptr:static_cast<const std::uint8_t*>(batch->map)+read.stencil);
                if(read.packed){for(const auto& run:surface.readback->runs)invalidate_raw(run.data,run.size,"surface_commit");}
                else for(const auto& span:surface.physical)invalidate_raw(reinterpret_cast<const void*>(span.begin),span.end-span.begin,"surface_commit");
                surface.dirty=false;
            }
            commit_ms+=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-commit_start).count();
            vk.graphics_reset(*batch);reads.clear();
        }catch(...){failed=true;throw;}};
        for(auto& surface:surfaces)if(download && surface.dirty && (!pointer || surface.overlaps(pointer,size))){
            const auto pixels=std::uint64_t(surface.width)*surface.height*surface.samples;
            const auto image_bytes=pixels*(image_bpp(surface.image->format)+(surface.depth && surface.format==2?1:0));
            const auto table=surface.readback?surface.readback->words.size()*4:0;
            const auto required=image_bytes+table*(surface.readback && surface.readback->gpu_words?2:3)+65536;
            if(table && required<=batch->capacity){
                if(batch->used+required>batch->capacity)finish_reads();
                const auto at=vk.graphics_pack_readback(*batch,surface.image,surface.format,surface.depth,surface.target.bpp,surface.target.pitch,surface.samples,surface.readback->words,surface.readback->gpu_words);
                reads.push_back({&surface,at,UINT64_MAX,true});
            }else{if(batch->used+image_bytes+65536>batch->capacity)finish_reads();
                const auto aspect=surface.depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT;
                const auto color=vk.graphics_transfer(*batch,surface.image,aspect,image_bpp(surface.image->format));
                const auto stencil=surface.depth && surface.format==2?vk.graphics_transfer(*batch,surface.image,VK_IMAGE_ASPECT_STENCIL_BIT,1):UINT64_MAX;
                reads.push_back({&surface,color,stencil,false});}}
        finish_reads();
    }
    void host_access(const void* p,std::uint64_t size,bool write){
        if(busy){if(anyps3_ft::enabled(anyps3_ft::HostExec))anyps3_ft::exec_class|=4;return;}
        // ANYPS3_SPUPUT_DUMP=N (diagnostic, default off): describe the first N foreign read requests: exact sync/poll bits of the
        // span, overlapping labels, surfaces and raw texture spans (which watch made the requester synchronise?).
        static const unsigned dump_limit=[]{const char* v=std::getenv("ANYPS3_SPUPUT_DUMP");return v?unsigned(std::strtoul(v,nullptr,0)):0u;}();
        static unsigned dumped=0;
        if(dump_limit && dumped<dump_limit && !write){const auto origin=HostAccessContext::sample();
            if(origin.engine==HostAccessOrigin::Engine::Spu || origin.engine==HostAccessOrigin::Engine::Dma || origin.ea!=UINT64_MAX){++dumped;
            auto& o=state.memory.host_access_observer;const auto at=reinterpret_cast<std::uintptr_t>(p);
            std::fprintf(stderr,"SPUPUT DUMP host=0x%llx size=%llu engine=%s ea=0x%llx pc=0x%x id=0x%x sync_exact=%d all_exact=%d sync_page=%d\n",(unsigned long long)at,(unsigned long long)size,origin.name(),
                (unsigned long long)origin.ea,origin.guest_pc,origin.guest_id,int(o.synchronous(p,size,true)),int(o.interested(p,size,true)),int(o.synchronous(p,size,false)));
            each_label([&](const auto& label){const auto q=reinterpret_cast<std::uintptr_t>(label.identity());if(label_page_overlap(label,p,size))
                std::fprintf(stderr,"SPUPUT DUMP  label address=0x%llx size=%u kind=%u value=0x%x exact=%d\n",(unsigned long long)label.address,label.size,unsigned(label.kind),label.value,int(at<q+label.size && q<at+size));});
            for(const auto& surface:surfaces){bool page=false;for(const auto& span:surface.physical)if((span.begin>>12)<=((at+size-1)>>12) && (at>>12)<=((span.end-1)>>12))page=true;
                if(page)std::fprintf(stderr,"SPUPUT DUMP  surface base=0x%llx pitch=%u %ux%u fmt=%u depth=%d dirty=%d exact=%d spans=%zu\n",(unsigned long long)reinterpret_cast<std::uintptr_t>(surface.target.base),surface.target.pitch,surface.width,surface.height,surface.format,int(surface.depth),int(surface.dirty),int(surface.overlaps(p,size)),surface.physical.size());}
            for(const auto& entry:raw_textures){bool hit=false;raw_spans(entry,[&](const void* a,std::uint64_t n){const auto x=reinterpret_cast<std::uintptr_t>(a);if((x>>12)<=((at+size-1)>>12) && (at>>12)<=((x+n-1)>>12))hit=true;});
                if(hit)std::fprintf(stderr,"SPUPUT DUMP  raw source=0x%llx size=%llu dirty_inputs=%d buffer=%d plan=%d\n",(unsigned long long)reinterpret_cast<std::uintptr_t>(entry.source),(unsigned long long)entry.size,int(bool(entry.dirty_inputs)),int(bool(entry.buffer)),int(bool(entry.plan)));}}}
        const auto ft_invalidations=raw_invalidations;
        struct FtClass {const decltype(raw_invalidations)& now;decltype(raw_invalidations) before;~FtClass(){if(anyps3_ft::enabled(anyps3_ft::HostExec) && now!=before)anyps3_ft::exec_class|=32;}} ft_class{raw_invalidations,ft_invalidations};
        ::RsxPerfCounters::add(::RsxPerfCounters::host_access);
        GuestMemory::RawYield payload(state.memory);
        if(!state.rsx.graphics_session)enter_fifo();
        const bool protected_access=!HostByteAccess::active() && (write?guard.touches_any(p,size):guard.touches(p,size));
        if(anyps3_ft::enabled(anyps3_ft::HostExec) && protected_access)anyps3_ft::exec_class|=16;
        if(has_labels() && (label_overlap(p,size) || protected_access)){
            if(anyps3_ft::enabled(anyps3_ft::HostExec))anyps3_ft::exec_class|=8;
            if(fallback_stats().enabled){std::ostringstream detail;detail<<"write="<<write<<" pc=0x"<<std::hex<<state.pc;
                detail<<" ppu_owner="<<std::dec<<state.thread_id<<" host=0x"<<std::hex<<reinterpret_cast<std::uintptr_t>(p)<<" bytes=0x"<<size;
                for(const auto& region:state.maps){const auto base=reinterpret_cast<std::uintptr_t>(region.bytes->data()),at=reinterpret_cast<std::uintptr_t>(p);if(at>=base && at-base<region.bytes->size())detail<<" guest=0x"<<(region.base+at-base);}
                each_label([&](const auto& label){if(label_page_overlap(label,p,size))detail<<" label=0x"<<label.address;});
                detail<<" origin=owner_context_foreign_PC_unavailable";fallback_stats().detail("label_host_access",detail.str());}
            synchronize_labels(p,size,write);
        }
#if defined(__linux__)
        if(protected_access){
            // An API pointer may be passed to read/write syscalls: the kernel
            // reports EFAULT instead of delivering our SIGSEGV. Release every
            // image sharing its host pages BEFORE publishing the pointer.
            const auto page=GuestMemory::HostGuard::page_size(),at=reinterpret_cast<std::uintptr_t>(p);
            const auto first=at&~(page-1),last=(at+size+page-1)&~(page-1);
            p=reinterpret_cast<const void*>(first);size=last-first;
        }
#endif
        // The kernel does not deliver SIGSEGV for protected syscall buffers.
        // Release every texture sharing these pages before publishing a pointer.
        if(write || protected_access)invalidate_raw(p,size,write?"host_write":"protected_read");
        if(write)invalidate_geometry(p,size);
        bool overlap=false;for(const auto& s:surfaces)if(s.overlaps(p,size)){overlap=true;break;}
        if(!overlap){
            if(!HostByteAccess::active() && guard.touches_any(p,size)){if(anyps3_ft::enabled(anyps3_ft::HostExec))anyps3_ft::exec_class|=256;guard.release(p,size);++ownership_revision;if(!state.rsx.graphics_session)leave_fifo();}
            return;
        }
        if(anyps3_ft::enabled(anyps3_ft::HostExec)){anyps3_ft::exec_class|=64;for(const auto& s:surfaces)if(s.dirty && s.overlaps(p,size)){anyps3_ft::exec_class|=128;break;}}
        if(fallback_stats().enabled){std::ostringstream detail;detail<<"write="<<write<<" pc=0x"<<std::hex<<state.pc<<" host=0x"<<reinterpret_cast<std::uintptr_t>(p)<<" bytes=0x"<<size;
            for(const auto& region:state.maps){const auto base=reinterpret_cast<std::uintptr_t>(region.bytes->data()),at=reinterpret_cast<std::uintptr_t>(p);if(at>=base && at-base<region.bytes->size())detail<<" guest=0x"<<(region.base+at-base);}
            fallback_stats().detail("surface_host_access",detail.str());}
        flush(true,p,size,write?"host_write":"host_read");
        // Guest coherence remains authoritative. Until partial visual history
        // restoration is implemented, count every loss of a visual companion.
        for(const auto& surface:surfaces)if(surface.image->visual && surface.overlaps(p,size))++vk.graphics_visual_host_invalidations;
        surfaces.erase(std::remove_if(surfaces.begin(),surfaces.end(),[&](const auto& s){return s.overlaps(p,size);}),surfaces.end());refresh_surface_watches();
        if(!state.rsx.graphics_session)leave_fifo();
    }
    bool copy(const GuestMemory::Range& from,const GuestMemory::Range& to,unsigned cols,unsigned rows,unsigned in_pitch,unsigned out_pitch,unsigned required_format=0){
        Surface* src=nullptr;Surface* dst=nullptr;
        for(auto& surface:surfaces){const auto& t=surface.target;
            if(surface.partial || t.tile || t.swizzle_width || t.offset!=t.base_offset || surface.height*(surface.samples==4?2u:1u)!=rows || surface.width*(surface.samples>1?2u:1u)*t.bpp!=cols)continue;
            if(required_format && surface.format!=required_format)continue;
            if(t.pitch==in_pitch && from.parts.size()==1 && from.parts[0].data==t.base)src=&surface;
            if(t.pitch==out_pitch && to.parts.size()==1 && to.parts[0].data==t.base)dst=&surface;
        }
        if(!src || !dst || src->format!=dst->format || src->samples!=dst->samples || src->depth!=dst->depth || src->image->format!=dst->image->format)return false;
        vk.graphics_copy(*batch,src->image,dst->image);dirty_surface(*dst);return true;
    }
    void prepare_protection(Surface& surface){
        for(auto& cached:layout_cache)if(cached.same(surface.target,surface.width,surface.height,surface.samples,surface.format,surface.depth)){
            surface.protectable=cached.protectable;surface.protection=cached.protection;surface.physical=cached.physical;surface.readback=cached.readback;surface.exposed=cached.exposed;cached.last_use=++resource_clock;++layout_hits;return;}
        ++layout_misses;
        struct Run {std::uint8_t* data;unsigned logical,size,guest_offset;};std::vector<Run> runs;
        surface.readback=std::make_shared<Surface::ReadbackPlan>();
        GuestMemory::HostGuard probe(false);surface.protectable=bool(surface.target.mapping);
        if(!surface.protectable)return;
        const auto& t=surface.target;const unsigned width=surface.width*(surface.samples>1?2:1),height=surface.height*(surface.samples==4?2:1);
        // Mapping a tile pins its entire allocation; only rendered bytes need
        // ownership. Do not require unrelated allocator-edge pages to be safe.
        for(unsigned y=0;y<height;++y)for(unsigned byte=0;byte<width*t.bpp;){
            const unsigned offset=t.address(byte/t.bpp,y,byte%t.bpp);
            unsigned n=width*t.bpp-byte;
            if(t.swizzle_width)n=std::min(n,std::min(t.bpp,4u));
            else if(t.tile)n=std::min(n,32-(t.offset+y*t.pitch+byte)%32);
            for(unsigned done=0;done<n;){
                unsigned available=0;const auto* pointer=mapped_pointer(*t.mapping,offset+done,available);const unsigned count=std::min(n-done,available);
                const GuestMemory::Range::Part* owner=nullptr;
                for(const auto& p:t.mapping->parts)if(offset+done>=p.offset && offset+done-p.offset<p.size){owner=&p;break;}
                if(!count || !owner)throw std::runtime_error("RSX resident footprint outside mapping");
                const auto at=reinterpret_cast<std::uintptr_t>(pointer);surface.physical.push_back({at,at+count});
                runs.push_back({const_cast<std::uint8_t*>(pointer),y*t.pitch+byte+done,count,offset+done});
                if(!probe.add(pointer,count,*owner->owner)){
                    const auto page=GuestMemory::HostGuard::page_size(),base=reinterpret_cast<std::uintptr_t>(owner->owner->data());
                    const auto first=(base+page-1)&~(page-1),last=(base+owner->owner->size())&~(page-1);
                    const auto inside_first=std::max(at,first),inside_last=std::min(at+count,last);
                    if(inside_first<inside_last)probe.add_span({inside_first&~(page-1),(inside_last+page-1)&~(page-1)});
                    if(at<first)GuestMemory::HostGuard::merge_span(surface.exposed,{at,std::min(at+count,first)});
                    if(at+count>last)GuestMemory::HostGuard::merge_span(surface.exposed,{std::max(at,last),at+count});
                    if(fallback_stats().enabled){const auto page=std::uintptr_t(4096);fallback_stats().detail("unprotectable_backing",
                        "surface_offset="+std::to_string(t.offset)+" depth="+std::to_string(surface.depth)+" owner_size="+std::to_string(owner->owner->size())+" owner_mod_4k="+std::to_string(reinterpret_cast<std::uintptr_t>(owner->owner->data())%page)+" run_mod_4k="+std::to_string(at%page)+" run_bytes="+std::to_string(count));}
                }
                done+=count;
            }
            byte+=n;
        }
        {auto& spans=surface.physical;std::sort(spans.begin(),spans.end(),[](const auto& a,const auto& b){return a.begin<b.begin;});
            unsigned n=0;for(const auto& span:spans){if(n && span.begin<=spans[n-1].end)spans[n-1].end=std::max(spans[n-1].end,span.end);else spans[n++]=span;}spans.resize(n);}
        if(probe.spans.empty())surface.protectable=false;
        if(surface.protectable){
            auto& spans=probe.spans;std::sort(spans.begin(),spans.end(),[](const auto& a,const auto& b){return a.begin<b.begin;});
            for(const auto& span:spans){if(!surface.protection.empty() && span.begin<=surface.protection.back().end)surface.protection.back().end=std::max(surface.protection.back().end,span.end);else surface.protection.push_back(span);}
        }
        probe.spans.clear();
        std::sort(runs.begin(),runs.end(),[](const auto& a,const auto& b){return reinterpret_cast<std::uintptr_t>(a.data)<reinterpret_cast<std::uintptr_t>(b.data);});
        for(const auto& run:runs){
            auto& plan=*surface.readback;const unsigned offset=plan.words.size()*4;
            for(unsigned byte=0;byte<run.size;byte+=4)plan.words.push_back(run.logical+byte);
            if(!plan.runs.empty() && plan.runs.back().data+plan.runs.back().size==run.data && plan.runs.back().offset+plan.runs.back().size==offset && plan.runs.back().guest_offset+plan.runs.back().size==run.guest_offset)plan.runs.back().size+=run.size;
            else plan.runs.push_back({run.data,run.size,offset,run.guest_offset});
        }
        auto cost=[](const Surface& e){return e.readback?e.readback->words.size()*4+e.readback->runs.size()*sizeof(Surface::CommitRun):0;};
        const auto new_bytes=cost(surface);auto total=[&]{std::uint64_t n=0;for(const auto& e:layout_cache)n+=cost(e);return n;};
        while(!layout_cache.empty() && (layout_cache.size()>=64 || total()+new_bytes>64*1024*1024)){
            auto old=std::min_element(layout_cache.begin(),layout_cache.end(),[](const auto& a,const auto& b){return a.last_use<b.last_use;});layout_cache.erase(old);}
        if(new_bytes<=64*1024*1024){auto cached=surface;cached.image.reset();cached.last_use=++resource_clock;layout_cache.push_back(std::move(cached));}
    }
    static std::shared_ptr<Surface::ReadbackPlan> clipped_plan(const Surface& surface,const std::vector<GuestMemory::HostGuard::Span>& spans){
        auto plan=std::make_shared<Surface::ReadbackPlan>();
        const auto& source=*surface.readback;
        // Exact output size first (a partial plan of a large tiled surface used to grow word by word), then the same
        // run-major / span-minor order as before; an aligned start copies whole words, otherwise each word keeps the
        // constant sub-word remainder of its logical byte offset.
        std::size_t total_words=0,total_runs=0;
        for(const auto& run:source.runs)for(const auto& span:spans){
            const auto base=reinterpret_cast<std::uintptr_t>(run.data),first=std::max(base,span.begin),last=std::min(base+run.size,span.end);
            if(first>=last)continue;
            total_words+=(last-first+3)/4;++total_runs;
        }
        plan->words.reserve(total_words);plan->runs.reserve(total_runs);
        for(const auto& run:source.runs)for(const auto& span:spans){
            const auto base=reinterpret_cast<std::uintptr_t>(run.data),first=std::max(base,span.begin),last=std::min(base+run.size,span.end);
            if(first>=last)continue;
            const unsigned delta=first-base,offset=plan->words.size()*4,count=last-first;
            const auto at=std::uint64_t(run.offset)+delta;const auto* words=source.words.data()+at/4;const unsigned n=(count+3)/4,sub=at%4;
            if(!sub)plan->words.insert(plan->words.end(),words,words+n);
            else for(unsigned i=0;i<n;++i)plan->words.push_back(words[i]+sub);
            plan->runs.push_back({reinterpret_cast<std::uint8_t*>(first),count,offset,unsigned(run.guest_offset+delta)});
        }
        return plan;
    }
    void enter_fifo(){
        GuestMemory::ObserverPause edge_reads(state.memory);
        // A raw write on an allocator-edge page cannot fault safely. Preserve
        // those actual CPU bytes while committing the remaining GPU ownership,
        // then invalidate the image before any later draw/RTT lookup can use it.
        for(std::size_t i=0;i<surfaces.size();){auto& surface=surfaces[i];std::size_t offset=0;bool changed=false;
            if(surface.protectable && !surface.edge_snapshot.empty())for(const auto& span:surface.exposed){const auto n=span.end-span.begin;
                std::vector<std::uint8_t> bytes(n);pinned_host_read(state.memory,reinterpret_cast<const void*>(span.begin),bytes.data(),n);
                if(std::memcmp(bytes.data(),surface.edge_snapshot.data()+offset,n)){changed=true;}offset+=n;}
            if(!changed){++i;continue;}
            std::vector<std::uint8_t> current;for(const auto& span:surface.exposed){const auto at=current.size();current.resize(at+span.end-span.begin);pinned_host_read(state.memory,reinterpret_cast<const void*>(span.begin),current.data()+at,span.end-span.begin);}
            const auto physical=surface.physical.front();flush(true,reinterpret_cast<const void*>(physical.begin),physical.end-physical.begin,"edge_write");
            {GuestMemory::ObserverPause pause(state.memory);offset=0;for(const auto& span:surface.exposed){const auto n=span.end-span.begin;pinned_host_write(state.memory,reinterpret_cast<const void*>(span.begin),current.data()+offset,n);offset+=n;}}
            for(const auto& span:surface.exposed)invalidate_raw(reinterpret_cast<const void*>(span.begin),span.end-span.begin,"edge_write");
            surfaces.erase(surfaces.begin()+i);refresh_surface_watches();
        }
    }
    void commit_edges(){
        for(auto& surface:surfaces)if(surface.protectable && !surface.exposed.empty() && surface.dirty && surface.edge_generation!=surface.generation){
            auto original=surface.readback;surface.readback=clipped_plan(surface,surface.exposed);
            const auto physical=surface.physical.front();
            try{flush(true,reinterpret_cast<const void*>(physical.begin),physical.end-physical.begin,"allocator_edge");}
            catch(...){surface.readback=std::move(original);throw;}
            surface.readback=std::move(original);surface.dirty=true;surface.edge_generation=surface.generation;
            surface.edge_snapshot.clear();for(const auto& span:surface.exposed){const auto at=surface.edge_snapshot.size();surface.edge_snapshot.resize(at+span.end-span.begin);GuestMemory::ObserverPause pause(state.memory);pinned_host_read(state.memory,reinterpret_cast<const void*>(span.begin),surface.edge_snapshot.data()+at,span.end-span.begin);}
        }
    }

    // Device-only aliasing transition: the new surface's guest background is
    // uploaded, every overlapping dirty owner is packed to guest byte order on
    // the GPU and copied over exactly its physical bytes, then the result is
    // unpacked into the new image. Owner bytes outside the new footprint stay
    // GPU-authoritative as a partial remainder (committed on first use). This
    // replaces a synchronous readback + CPU commit + CPU re-read round trip.
    bool rebind_merge(Surface& surface,VkFormat format,bool discard=false){
        const char* setting=std::getenv("ANYPS3_RSX_GPU_ALIAS_MERGE");
        if(setting && std::strcmp(setting,"0") && std::strcmp(setting,"1"))throw std::runtime_error("invalid ANYPS3_RSX_GPU_ALIAS_MERGE");
        if(setting && !std::strcmp(setting,"0"))return false;
        const auto& t=surface.target;const auto& plan=surface.readback;
        const auto bytes=std::uint64_t(surface.width)*surface.height*surface.samples*t.bpp;
        // A discarding bind (full clear follows) needs no upload: only the old
        // owners' bytes outside the new footprint must survive (as remainders).
        const bool aliased=std::any_of(surfaces.begin(),surfaces.end(),[&](const Surface& old){for(const auto& span:surface.physical)if(old.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin))return true;return false;});
        if(!aliased)return false;
        // ANYPS3_RSX_MERGE_TRACE=1 (diagnostic): the new surface and every live surface it overlaps, for each bind over live aliases.
        {static const bool on=std::getenv("ANYPS3_RSX_MERGE_TRACE")!=nullptr;static unsigned lines=0;
            if(on && lines<200){++lines;
                std::fprintf(stderr,"RSX MERGE BIND new off=%u pitch=%u %ux%u s=%u fmt=%u depth=%d bpp=%u discard=%d protectable=%d image_fmt=%d\n",unsigned(t.offset),unsigned(t.pitch),surface.width,surface.height,surface.samples,surface.format,int(surface.depth),unsigned(t.bpp),int(discard),int(surface.protectable),int(format));
                for(const auto& old:surfaces){bool hit=false;for(const auto& span:surface.physical)if(old.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin)){hit=true;break;}
                    if(hit)std::fprintf(stderr,"RSX MERGE BIND   old off=%u pitch=%u %ux%u s=%u fmt=%u depth=%d bpp=%u dirty=%d partial=%d protectable=%d exposed=%zu words=%zu runs=%zu image_fmt=%d\n",unsigned(old.target.offset),unsigned(old.target.pitch),old.width,old.height,old.samples,old.format,int(old.depth),unsigned(old.target.bpp),int(old.dirty),int(old.partial),int(old.protectable),old.exposed.size(),old.readback?old.readback->words.size():size_t(0),old.readback?old.readback->runs.size():size_t(0),old.image?int(old.image->format):-1);}}}
        auto reject=[](::RsxPerfCounters::Id id){::RsxPerfCounters::add(id);return false;};
        // ANYPS3_RSX_MERGE_TRACE=1: why a new surface over live aliases cannot use the GPU merge (diagnostic).
        auto trace_new=[&](const char* why){static const bool on=std::getenv("ANYPS3_RSX_MERGE_TRACE")!=nullptr;static unsigned lines=0;
            if(on && lines++<60)std::fprintf(stderr,"RSX MERGE REJECT %s discard=%d protectable=%d exposed=%zu mapping=%d plan=%d plan_words=%zu bpp=%u offset=%u pitch=%u width=%u height=%u samples=%u format=%u depth=%d bytes=%llu\n",why,int(discard),int(surface.protectable),surface.exposed.size(),int(bool(t.mapping)),int(bool(plan)),plan?plan->words.size():size_t(0),unsigned(t.bpp),unsigned(t.offset),unsigned(t.pitch),surface.width,surface.height,surface.samples,surface.format,int(surface.depth),(unsigned long long)bytes);};
        if(!surface.protectable || !surface.exposed.empty() || !t.mapping){trace_new("protection");return reject(::RsxPerfCounters::merge_rej_new);}
        if(!discard){if(!plan || plan->words.empty() || (t.bpp!=1 && t.bpp!=2 && t.bpp%4) || (t.offset&3) || (t.pitch&3) || plan->words.size()*4!=bytes){trace_new("plan_or_bpp_or_align");return reject(::RsxPerfCounters::merge_rej_new);}
            for(const auto& run:plan->runs)if((run.offset|run.size)&3){trace_new("run_align");return reject(::RsxPerfCounters::merge_rej_new);}}
        std::vector<std::size_t> olds;std::uint64_t required=discard?0:unpack_budget(surface);
        for(std::size_t i=0;i<surfaces.size();++i){const auto& old=surfaces[i];bool overlap=false;
            for(const auto& span:surface.physical)if(old.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin)){overlap=true;break;}
            if(!overlap)continue;
            if(!old.protectable || !old.exposed.empty() || !old.readback || old.readback->words.empty() || !old.image)return reject(::RsxPerfCounters::merge_rej_old);
            if(old.dirty && !discard)required+=std::uint64_t(old.image->width)*old.image->height*old.image->layers*16+old.readback->words.size()*12+65536;
            olds.push_back(i);
        }
        if(olds.empty() || required>batch->capacity)return reject(::RsxPerfCounters::merge_rej_capacity);
        // Dirty owners must be disjoint: their copy order would otherwise matter.
        for(std::size_t a=0;a<olds.size();++a)for(std::size_t c=a+1;c<olds.size();++c){const auto& x=surfaces[olds[a]];const auto& y=surfaces[olds[c]];
            if(!x.dirty || !y.dirty)continue;
            for(const auto& span:x.physical)if(y.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin))return reject(::RsxPerfCounters::merge_rej_overlap);}
        t.mapping->check_epoch();for(auto i:olds)surfaces[i].target.mapping->check_epoch();
        if(batch->used+required>batch->capacity)submit_async("surface_merge_capacity");
        ::RsxPerfCounters::add(::RsxPerfCounters::alias_merges);
        auto image=surface_image(format,surface.width,surface.height,surface.samples,surface.depth);
        std::vector<std::uint8_t> packed;std::uint64_t at=0;
        if(!discard){struct Reading {bool& busy;bool previous;explicit Reading(bool& b):busy(b),previous(b){busy=true;}~Reading(){busy=previous;}} reading(busy);
            SurfaceBytes transfer(surface,false);transfer.snapshot();
            const auto* data=transfer.packed_data(*plan);
            if(!data){packed.resize(bytes);for(const auto& run:plan->runs){
                if(run.offset>bytes || run.size>bytes-run.offset)throw std::runtime_error("RSX surface upload run bounds");
                transfer.read(run.guest_offset,packed.data()+run.offset,run.size);}data=packed.data();}
            at=vk.graphics_device_upload(*batch,data,bytes);}
        bool dirty=false;std::vector<VkBufferCopy> regions;
        std::vector<Surface> remainders;std::vector<std::shared_ptr<NativeVulkan::GraphicsImage>> replaced;
        std::vector<std::pair<std::shared_ptr<NativeVulkan::GraphicsImage>,VisualHistoryUse>> history;std::uint64_t history_texels=0;
        for(auto i:olds){auto& old=surfaces[i];replaced.push_back(old.image);
            if(old.dirty){
                if(!discard){
                dirty=true;
                const auto source=vk.graphics_pack_readback(*batch,old.image,old.format,old.depth,old.target.bpp,old.target.pitch,old.samples,old.readback->words,old.readback->gpu_words,false);
                // Both run lists are sorted by physical address and disjoint.
                const auto& x=old.readback->runs;const auto& y=plan->runs;std::size_t a=0,c=0;std::vector<VisualHistoryPair> pairs;
                while(a<x.size() && c<y.size()){
                    const auto xb=reinterpret_cast<std::uintptr_t>(x[a].data),xe=xb+x[a].size,yb=reinterpret_cast<std::uintptr_t>(y[c].data),ye=yb+y[c].size;
                    const auto first=std::max(xb,yb),last=std::min(xe,ye);
                    if(first<last){regions.push_back({source+x[a].offset+(first-xb),at+y[c].offset+(first-yb),last-first});
                        pairs.push_back({unsigned(x[a].offset+(first-xb)),unsigned(y[c].offset+(first-yb)),unsigned(last-first)});}
                    if(xe<=ye)++a;else ++c;
                }
                // Dirty owners are disjoint, so mapped destination texels are too.
                if(image->visual){auto use=visual_history_plan(old,surface,format,pairs);
                    if(use.regions){history_texels+=use.texels;history.emplace_back(old.image,std::move(use));}}
                }
                std::vector<GuestMemory::HostGuard::Span> rest;
                GuestMemory::HostGuard::difference(old.physical,surface.physical,[&](auto span){rest.push_back(span);});
                if(!rest.empty()){Surface keep=old;keep.partial=true;keep.readback=clipped_plan(old,rest);keep.physical=rest;keep.protection.clear();
                    const auto page=GuestMemory::HostGuard::page_size();
                    for(const auto& span:rest)GuestMemory::HostGuard::merge_span(keep.protection,{span.begin&~(page-1),(span.end+page-1)&~(page-1)});
                    keep.dirty=true;keep.last_use=++resource_clock;remainders.push_back(std::move(keep));}
            }
        }
        if(!discard){vk.graphics_device_copies(*batch,regions);NativeVulkan::VisualWhy why(vk,"rebind_merge");
            // Fully mapped destinations need no native reconstruction first.
            const bool complete=history_texels==std::uint64_t(surface.width)*surface.height*surface.samples;
            vk.graphics_unpack_upload(*batch,image,surface.format,surface.depth,t.bpp,t.pitch,surface.samples,plan->words,plan->gpu_words,nullptr,at,!complete);
            for(const auto& h:history)vk.graphics_visual_history(*batch,h.first,image,*h.second.regions,h.second.texels);}
        surfaces.erase(std::remove_if(surfaces.begin(),surfaces.end(),[&](const auto& s){return std::find(replaced.begin(),replaced.end(),s.image)!=replaced.end();}),surfaces.end());
        for(auto& keep:remainders)surfaces.push_back(std::move(keep));
        surface.image=std::move(image);surface.dirty=dirty;surface.last_use=++resource_clock;surface.generation=1;
        ++ownership_revision;surfaces.push_back(std::move(surface));refresh_surface_watches();
        if(fallback_stats().enabled)++fallback_stats().paths["surface_alias_merge_gpu"];
        return true;
    }
    // Device scratch of one packed->image unpack: packed input, word table and packed owner copies (3 x bytes), the texel output
    // (4 bytes per pixel for 1/2-byte formats held in R32_UINT, otherwise bytes) and slack. Equals bytes*4+65536 for whole-word formats.
    static std::uint64_t unpack_budget(const Surface& surface){
        const auto pixels=std::uint64_t(surface.width)*surface.height*surface.samples,bytes=pixels*surface.target.bpp;
        return bytes*3+std::max(bytes,pixels*4)+65536;
    }
    bool upload_surface(Surface& surface){
        const auto& t=surface.target;const auto& plan=surface.readback;
        const auto bytes=std::uint64_t(surface.width)*surface.height*surface.samples*t.bpp;
        // Split words (unaligned runs/offsets/pitch) retain the general CPU codec. The
        // inverse table must cover complete pixels, including every plane.
        if(!plan || plan->words.empty() || !t.mapping || (t.bpp!=1 && t.bpp!=2 && t.bpp%4) || (t.offset&3) || (t.pitch&3) || plan->words.size()*4!=bytes)return false;
        for(const auto& run:plan->runs)if((run.offset|run.size)&3)return false;
        // Keep the established arena/readback capacity policy. In particular,
        // never replace an arena containing the image's initial transition.
        if(unpack_budget(surface)>batch->capacity)return false;
        if(batch->used+unpack_budget(surface)>batch->capacity)submit_async("surface_upload_capacity");
        std::vector<std::uint8_t> packed;
        struct Reading {bool& busy;bool previous;explicit Reading(bool& b):busy(b),previous(b){busy=true;}~Reading(){busy=previous;}} reading(busy);
        SurfaceBytes transfer(surface,false);transfer.snapshot();
        const auto* data=transfer.packed_data(*plan);
        if(!data){packed.resize(bytes);for(const auto& run:plan->runs){
            if(run.offset>bytes || run.size>bytes-run.offset)throw std::runtime_error("RSX surface upload run bounds");
            transfer.read(run.guest_offset,packed.data()+run.offset,run.size);
        }data=packed.data();}
        vk.graphics_unpack_upload(*batch,surface.image,surface.format,surface.depth,t.bpp,t.pitch,surface.samples,plan->words,plan->gpu_words,data);
        return true;
    }
    struct RemainderPlan {std::shared_ptr<Surface::ReadbackPlan> old_plan,new_plan,remainder;};
    std::vector<RemainderPlan> remainder_plans;std::uint64_t remainder_plan_bytes=0;
    // A sample-count change reinterprets the same physical sample grid.
    // Preserve the covered prefix on the GPU and load only the background
    // values that are outside the old render footprint from guest storage.
    bool rebind_sample_grid(Surface& surface,VkFormat format,bool discard=false){
        if(surface.depth || surface.format!=8 || surface.target.swizzle_width || (surface.samples!=1 && surface.samples!=2))return false;
        Surface* source=nullptr;Surface* background=nullptr;
        for(auto& old:surfaces){bool overlap=false;for(const auto& span:surface.physical)if(old.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin)){overlap=true;break;}
            if(!overlap)continue;
            if(old.partial){
                if(background || old.depth || !old.same(surface.target,surface.width,surface.height,surface.samples,surface.format,false))return false;
                background=&old;continue;
            }
            if(source || old.depth || old.format!=8 || old.width!=surface.width || old.height!=surface.height || old.samples==surface.samples || (old.samples!=1 && old.samples!=2) || !old.same(surface.target,old.width,old.height,old.samples,old.format,false))return false;
            source=&old;
        }
        if(!source)return false;
        if(background){
            // The retained old image must own every byte outside the new
            // prefix. Never use stale covered pixels as CPU background.
            bool covered=true;
            GuestMemory::HostGuard::difference(surface.physical,source->physical,[&](auto span){
                GuestMemory::HostGuard::difference({span},background->physical,[&](auto){covered=false;});});
            if(!covered)return false;
        }
        // Byte-split host allocations retain the general coherent path. The
        // word-copy fast path must not turn an otherwise supported split pixel
        // into a new explicit-stop case after recording GPU work.
        for(const auto& run:source->readback->runs)if((reinterpret_cast<std::uintptr_t>(run.data)|run.size|run.offset)&3)return false;
        for(const auto& span:surface.physical)if((span.begin|span.end)&3)return false;
        auto input=source->image;const unsigned source_samples=source->samples;const bool was_dirty=source->dirty;
        NativeVulkan::VisualWhy why(vk,"sample_grid");
        if((input->format!=VK_FORMAT_R8G8B8A8_UNORM && input->format!=VK_FORMAT_R8G8B8A8_SRGB) || (format!=VK_FORMAT_R8G8B8A8_UNORM && format!=VK_FORMAT_R8G8B8A8_SRGB))return false;
        rtt_space(std::uint64_t(surface.width)*surface.height*(surface.samples+source_samples)*8);
        if(!discard && input->format==VK_FORMAT_R8G8B8A8_SRGB){auto raw=surface_image(VK_FORMAT_R8G8B8A8_UNORM,source->width,source->height,source_samples);vk.graphics_copy(*batch,input,raw);input=std::move(raw);}
        surface.image=surface_image(VK_FORMAT_R8G8B8A8_UNORM,surface.width,surface.height,surface.samples);
        for(const auto& span:surface.physical)guard.release(reinterpret_cast<const void*>(span.begin),span.end-span.begin);
        ++ownership_revision;
        // Old overlap is intentionally stale on the host. Every covered byte
        // is replaced by the following bit-compatible GPU copy before use.
        if(!discard && source_samples<surface.samples && background){
            vk.graphics_copy(*batch,background->image,surface.image);
            if(fallback_stats().enabled)++fallback_stats().paths["sample_grid_background_gpu"];
        }else if(!discard && source_samples<surface.samples){
            const bool uploaded=upload_surface(surface);
            if(!uploaded){std::vector<std::uint8_t> background(std::uint64_t(surface.width)*surface.height*surface.samples*4);
                busy=true;try{load_pixels(surface,background.data(),nullptr);busy=false;}catch(...){busy=false;throw;}
                vk.graphics_transfer(*batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,background.data());}

        }
        if(!discard){
        const std::string vs="#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2.0-1.0,0,1);}";
        // Same flattened-grid reinterpretation at native and visual extent: destination
        // pixel X/plane maps to grid column X*Nd+plane, source pixel G/Ns, plane G%Ns.
        // On the visual pair this is the reinterpretation of the grid the surface would
        // have at the higher resolution (no native-cell blocks: planes stay in spatial order).
        const std::string fs="#version 450\nlayout(binding=17) uniform sampler2DArray src_;layout(location=0) out vec4 color_;layout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane_;};void main(){ivec2 p=ivec2(gl_FragCoord.xy);uint gx=uint(p.x)*"+std::to_string(surface.samples)+"u+plane_;if(gx>=uint(textureSize(src_,0).x)*"+std::to_string(source_samples)+"u)discard;color_=texelFetch(src_,ivec3(int(gx/"+std::to_string(source_samples)+"u),p.y,int(gx%"+std::to_string(source_samples)+"u)),0);}";
        NativeVulkan::GraphicsState fixed{};fixed.scale_visual=true;fixed.width=surface.width;fixed.height=surface.height;fixed.colors=1;fixed.samples=surface.samples;fixed.color=VK_FORMAT_R8G8B8A8_UNORM;fixed.scissor={{0,0},{surface.width,surface.height}};fixed.blend[0].colorWriteMask=15;
        std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> colors{};colors[0]=surface.image;
        std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> inputs{};inputs[16]=input;
        const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{.5f,.5f},{.5f,.5f},{.5f,.5f}}};
        std::vector<unsigned> data(16,0);vk.graphics_draw(*batch,fixed,colors,{},vs,fs,data,3,positions,0,0,0,1,inputs);
        if(format==VK_FORMAT_R8G8B8A8_SRGB){auto encoded=surface_image(format,surface.width,surface.height,surface.samples);vk.graphics_copy(*batch,surface.image,encoded);surface.image=std::move(encoded);}
        }
        // When reducing the sample grid, the old image also owns bytes
        // outside the new footprint. Commit exactly that remainder before
        // dropping its ownership; never lose the old right-hand sample grid.
        std::optional<Surface> retained_remainder;
        if(was_dirty){
            std::shared_ptr<Surface::ReadbackPlan> remainder;
            for(const auto& cached:remainder_plans)if(cached.old_plan==source->readback && cached.new_plan==surface.readback){remainder=cached.remainder;break;}
            const bool cache_miss=!remainder;
            if(cache_miss)remainder=std::make_shared<Surface::ReadbackPlan>();
            if(cache_miss)for(const auto& run:source->readback->runs){
                const std::vector<GuestMemory::HostGuard::Span> span{{reinterpret_cast<std::uintptr_t>(run.data),reinterpret_cast<std::uintptr_t>(run.data)+run.size}};
                GuestMemory::HostGuard::difference(span,surface.physical,[&](auto part){
                    const auto delta=part.begin-reinterpret_cast<std::uintptr_t>(run.data),count=part.end-part.begin;
                    if((delta|count|run.offset)&3)throw std::runtime_error("RSX sample-grid remainder is not word aligned");
                    const unsigned offset=remainder->words.size()*4;
                    remainder->words.insert(remainder->words.end(),source->readback->words.begin()+(run.offset+delta)/4,source->readback->words.begin()+(run.offset+delta+count)/4);
                    remainder->runs.push_back({reinterpret_cast<std::uint8_t*>(part.begin),unsigned(count),offset,unsigned(run.guest_offset+delta)});
                });
            }
            if(cache_miss){
                auto cost=[](const auto& p){return p->words.size()*4+p->runs.size()*sizeof(Surface::CommitRun);};
                const auto bytes=cost(source->readback)+cost(surface.readback)+cost(remainder);
                while(!remainder_plans.empty() && (remainder_plans.size()>=64 || remainder_plan_bytes+bytes>64*1024*1024)){
                    const auto& old=remainder_plans.front();remainder_plan_bytes-=cost(old.old_plan)+cost(old.new_plan)+cost(old.remainder);remainder_plans.erase(remainder_plans.begin());}
                if(bytes<=64*1024*1024){remainder_plan_bytes+=bytes;remainder_plans.push_back({source->readback,surface.readback,remainder});}
            }
            if(!remainder->words.empty()){
                // Full safe backing pages allow the uncovered bytes to remain
                // GPU authoritative until their first real reader. The next
                // expansion can merge this image without a host roundtrip.
                const auto required=std::uint64_t(source->width)*source->height*source->samples*4+remainder->words.size()*12+65536;
                if(source->protectable && source->exposed.empty() && required<=batch->capacity){
                    retained_remainder=*source;auto& keep=*retained_remainder;keep.partial=true;keep.readback=std::move(remainder);keep.physical.clear();
                    for(const auto& run:keep.readback->runs){const auto p=reinterpret_cast<std::uintptr_t>(run.data);GuestMemory::HostGuard::merge_span(keep.physical,{p,p+run.size});}
                    keep.protection.clear();const auto page=GuestMemory::HostGuard::page_size();
                    for(const auto& span:keep.physical)GuestMemory::HostGuard::merge_span(keep.protection,{span.begin&~(page-1),(span.end+page-1)&~(page-1)});
                    if(fallback_stats().enabled)++fallback_stats().paths["sample_grid_remainder_deferred"];
                }else{
                    source->readback=std::move(remainder);
                    const auto span=source->physical.front();flush(true,reinterpret_cast<const void*>(span.begin),span.end-span.begin,"sample_grid_uncovered");
                }
            }
        }
        const auto image=source->image,old_background=background?background->image:std::shared_ptr<NativeVulkan::GraphicsImage>{};
        surfaces.erase(std::remove_if(surfaces.begin(),surfaces.end(),[&](const auto& old){return old.image==image || (old_background && old.image==old_background);}),surfaces.end());
        if(retained_remainder)surfaces.push_back(std::move(*retained_remainder));
        surface.dirty=was_dirty && !discard;surface.last_use=++resource_clock;surface.generation=1;surfaces.push_back(std::move(surface));refresh_surface_watches();
        if(fallback_stats().enabled)++fallback_stats().paths[discard?"surface_sample_grid_discard":"surface_sample_grid_gpu_copy"];
        return true;
    }
    Surface& bind(Target target,unsigned width,unsigned height,unsigned samples,unsigned format,bool depth,bool srgb=false,bool packed=false,bool discard=false){
        const auto f=depth?(format==1?VK_FORMAT_D16_UNORM:VK_FORMAT_D24_UNORM_S8_UINT):packed?VK_FORMAT_R32_UINT:color_format(format,srgb);
        const auto candidates=surface_index.equal_range(surface_key(target));
        for(auto it=candidates.first;it!=candidates.second;++it){if(it->second>=surfaces.size())continue;auto& surface=surfaces[it->second];if(surface.partial || !surface.same(target,width,height,samples,format,depth))continue;
            if(surface.image->format!=f){
                const auto old=surface.image->format;
                const bool byte_compatible=(old==VK_FORMAT_R8G8B8A8_UNORM && f==VK_FORMAT_R8G8B8A8_SRGB) || (old==VK_FORMAT_R8G8B8A8_SRGB && f==VK_FORMAT_R8G8B8A8_UNORM);
                if(!byte_compatible)continue;
                auto converted=surface_image(f,width,height,samples,depth);vk.graphics_copy(*batch,surface.image,converted);surface.image=std::move(converted);++surface.generation;
                if(fallback_stats().enabled)++fallback_stats().paths["surface_compatible_alias_gpu_copy"];
            }
            surface.last_use=++resource_clock;return surface;
        }
        ::RsxPerfCounters::add(::RsxPerfCounters::surface_binds_new);
        {   // ANYPS3_RSX_BIND_TRACE=1: why a bind creates a new surface (first 120 binds after the 3000th): new target vs the surfaces at the same offset.
            static const bool trace=std::getenv("ANYPS3_RSX_BIND_TRACE")!=nullptr;static unsigned seen=0,shown=0;
            if(trace && ++seen>3000 && shown<120){++shown;
                std::fprintf(stderr,"BIND_TRACE new offset=%u base=%u pitch=%u %ux%u samples=%u fmt=%u depth=%d tile=%d discard=%d |",target.offset,target.base_offset,target.pitch,width,height,samples,format,int(depth),int(bool(target.tile)),int(discard));
                for(const auto& old:surfaces)if(old.target.offset==target.offset || old.target.base_offset==target.base_offset)
                    std::fprintf(stderr," [off=%u pitch=%u %ux%u s=%u f=%u d=%d partial=%d dirty=%d imgfmt=%d]",old.target.offset,old.target.pitch,old.width,old.height,old.samples,old.format,int(old.depth),int(old.partial),int(old.dirty),int(old.image?old.image->format:0));
                std::fprintf(stderr,"\n");}
        }
        Surface surface{target,width,height,samples,format,depth,false,{},{}};
        if(target.tile){surface.tile_snapshot=std::make_shared<NativeRsx::Tile>(*target.tile);surface.target.tile=surface.tile_snapshot.get();}
        prepare_protection(surface);
        if(rebind_sample_grid(surface,f,discard))return surfaces.back();
        if(rebind_equal_footprint(surface,f,discard))return surfaces.back();
        if(rebind_merge(surface,f,discard))return surfaces.back();
        // Disjoint subresources of one tile have independent ownership.
        auto aliases=[&](const Surface& other){for(const auto& span:surface.physical)if(other.overlaps(reinterpret_cast<const void*>(span.begin),span.end-span.begin))return true;return false;};
        bool alias=false;for(const auto& other:surfaces)if(aliases(other)){alias=true;break;}
        if(alias){
            if(fallback_stats().enabled)for(const auto& other:surfaces)if(aliases(other)){
                std::ostringstream detail;detail<<"old={offset:"<<other.target.offset<<",width:"<<other.width<<",height:"<<other.height<<",format:"<<other.format<<",samples:"<<other.samples<<",depth:"<<other.depth<<"} new={offset:"<<target.offset<<",width:"<<width<<",height:"<<height<<",format:"<<format<<",samples:"<<samples<<",depth:"<<depth<<"}";fallback_stats().detail("surface_alias_readback",detail.str());}
            std::vector<GuestMemory::HostGuard::Span> affected;
            // A discarding bind (full clear of every byte this surface owns)
            // overwrites an old surface whose physical bytes it fully covers:
            // committing that old image first could never be observed.
            auto covered=[&](const Surface& other){bool inside=true;
                GuestMemory::HostGuard::difference(other.physical,surface.physical,[&](auto){inside=false;});return inside;};
            for(const auto& other:surfaces)if(aliases(other) && !other.physical.empty() && !(discard && covered(other)))affected.push_back(other.physical.front());
            ::RsxPerfCounters::add(::RsxPerfCounters::alias_flushes,affected.size());
            for(const auto& span:affected)flush(true,reinterpret_cast<const void*>(span.begin),span.end-span.begin,"surface_alias");
            surfaces.erase(std::remove_if(surfaces.begin(),surfaces.end(),aliases),surfaces.end());refresh_surface_watches();
        }
        const auto image_factor=vk.graphics_render_scale==1?1:1+vk.graphics_render_scale*vk.graphics_render_scale;
        const auto bytes_required=std::uint64_t(width)*height*samples*image_bpp(f)*image_factor;
        auto cached_bytes=[&]{std::uint64_t n=0;for(const auto& entry:surfaces)n+=entry.image->memory_bytes;return n;};
        while(!surfaces.empty() && (surfaces.size()>=64 || cached_bytes()+bytes_required>256*1024*1024ull*image_factor)){
            // Commit every dirty target before removing its physical ownership.
            flush(true,nullptr,0,"surface_eviction");auto old=std::min_element(surfaces.begin(),surfaces.end(),[](const auto& a,const auto& b){return a.last_use<b.last_use;});surfaces.erase(old);refresh_surface_watches();++surface_evictions;}
        surface.image=surface_image(f,width,height,samples,depth);
        for(const auto& span:surface.physical)this->guard.release(reinterpret_cast<const void*>(span.begin),span.end-span.begin);
        ++ownership_revision;
        NativeVulkan::VisualWhy why(vk,alias?"bind_alias":"bind_fresh");
        const bool uploaded=discard || upload_surface(surface);
        if(!uploaded){const auto bpp=image_bpp(f);std::vector<std::uint8_t> bytes(std::uint64_t(width)*height*samples*bpp),stencil;
            if(depth && format==2)stencil.resize(std::uint64_t(width)*height*samples);
            busy=true;try{load_pixels(surface,bytes.data(),stencil.empty()?nullptr:stencil.data());busy=false;}catch(...){busy=false;throw;}
            vk.graphics_transfer(*batch,surface.image,depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,bpp,bytes.data());
            if(!stencil.empty())vk.graphics_transfer(*batch,surface.image,VK_IMAGE_ASPECT_STENCIL_BIT,1,stencil.data());
        }
        surface.last_use=++resource_clock;surfaces.push_back(std::move(surface));refresh_surface_watches();return surfaces.back();
    }
};
''' + ALIAS_CPP + r'''
static bool supported(const NativeRsx& gpu,const std::vector<FpInst>& fp,const std::array<Texture,16>& textures){
    (void)fp;const Reg r{gpu};if(r(0x220) && color_format(r(0x208)&31)==VK_FORMAT_UNDEFINED)return false;
    for(const auto& t:textures)if(t.valid){auto wrap=[](unsigned v){return (v>=1 && v<=6)||v==8;};
        if(!t.mip_count || t.mip_count*(t.cube?6u:1u)!=t.levels.size() || t.mip_count>13 || t.min<1 || t.min>6 || t.mag<1 || t.mag>2 || !wrap(t.wrap_s) || !wrap(t.wrap_t) || (t.dimension==3 && !wrap(t.wrap_r)))return false;}
    return true;
}
static bool certified_target(Target target,unsigned width,unsigned height){
    // CPU stripe workers cannot update shared reservation versions safely.
    // Graphics has no such workers; bind/readback mark writes synchronously.
    target.observed=false;return parallel_target(target,width,height);
}
struct GeometryFallback {
    void* context=nullptr;void (*call)(void*)=nullptr;
    void operator()()const{if(!call)throw std::runtime_error("RSX device geometry needs its original materializer");call(context);}
};
template<class Vertex> static bool prepared(Session& session,const std::vector<std::array<unsigned,3>>& indices,Vertex vertex,
        const std::vector<FpInst>& fp,const std::array<Texture,16>& supplied_textures,const std::array<Target,4>& supplied_colors,Target depth,
        unsigned width,unsigned height,unsigned x0,unsigned y0,unsigned x1,unsigned y1,bool point=false,const std::map<unsigned,LineGeometry>* lines=nullptr,const DeviceGeometry* device=nullptr,GeometryFallback materialize={}){
    GuestMemory::RawYield payload(session.state.memory);
    HostPhase phase(HostStage::geometry);
    DrawAttempt attempt;auto colors=supplied_colors;
    auto& vk=session.vk;auto& state=session.state;const Reg r{state.rsx};
    if(lines && !vk.graphics_float64())return attempt.reject("device_line_fp64");
    if(r(0x220) && color_format(r(0x208)&31)==VK_FORMAT_UNDEFINED)return attempt.reject("color_format");
    if(!supported(state.rsx,fp,supplied_textures))return attempt.reject("texture_layout");
    const auto target=r(0x220);unsigned count=target==0?0:(target==1 || target==2)?1:target==0x13?2:target==0x17?3:target==0x1f?4:5;
    if(count>4)return attempt.reject("target_count");
    // The masks suppress every color store, including blend and logic ops.
    // MRT nibble n controls target n (target 0 uses the byte-spaced mask).
    bool neutral_colors=true;
    for(unsigned n=0;n<count;++n){const bool writes=n==0?(r.get(0x324,0x01010101)&0x01010101u):((r(0x370)>>(n*4))&15u);
        if(writes)neutral_colors=false;else colors[n]=Target{};}
    if(count && neutral_colors){count=0;colors={};attempt.path("neutral_color_alias");}

    const unsigned aa=(r(0x208)>>12)&15,samples=aa==0?1:aa==3?2:4,nx=samples>1?2:1,ny=samples==4?2:1;
    if(!certified_target(depth,width*nx,height*ny))return attempt.reject("depth_storage_alias");
    bool alias=false;
    for(unsigned i=0;i<count;++i)if(target_overlaps(colors[i],depth,width*nx,height*ny)){
        if(attempt.stats.enabled)attempt.stats.detail("color_depth_alias",alias_detail(colors[i],depth,width*nx,height*ny,r));
        alias=true;}
    // Binding/validation already happened in rsx_execute_draw. A read-only
    // ALWAYS depth test cannot affect any sample; retaining its Vulkan image
    // would manufacture a dependency on bytes that the CPU never uses.
    if(alias && !(r(0xa70)&1) && !(r(0x328)&1) && !(r(0x380)&1) && (!(r(0xa74)&1) || r.get(0xa6c,0x207)==0x207)){
        depth=Target{};alias=false;attempt.path("neutral_depth_alias");}
    for(unsigned i=0;i<count;++i){if(!certified_target(colors[i],width*nx,height*ny))return attempt.reject("color_storage_alias");
        for(unsigned j=0;j<i;++j)if(target_overlaps(colors[i],colors[j],width*nx,height*ny)){if(!depth.base)return attempt.reject("mrt_alias");alias=true;}}
    // The compute alias rasterizer has no 0x1d7c coverage/alpha-to-one or null texture model.
    if(alias && r.get(0x1d7c,0xffff0000)!=0xffff0000)return attempt.reject("alias_multisample_control");
    // Fragment textures whose bytes alias a color target sample a pre-draw
    // snapshot (the CPU reference copies the same bytes in rsx_execute_draw):
    // the RSX texture cache is not coherent with ROP writes inside a draw, and
    // RPCS3 ffba2bf0 samples a bound target through a pre-draw copy
    // (texture_cache_helpers.h:750-779). A guest-byte upload is already such a
    // copy; an image that this draw writes is copied below. Depth feedback
    // keeps its CPU reference until a GPU depth snapshot is proven.
    bool color_feedback=false;
    for(const auto& texture:supplied_textures)if(texture.valid && !texture.untiled && !(texture.plan && texture.plan->gathered)){
        if(target_overlaps(depth,texture.storage,texture.storage_size,width*nx,height*ny))return attempt.reject("sequential_depth_texture_feedback");
        for(unsigned i=0;i<count;++i)if(target_overlaps(colors[i],texture.storage,texture.storage_size,width*nx,height*ny))color_feedback=true;
    }
    // The 16-texture array is copied only where this draw can edit it: the attachment-alias path (materialize /
    // re-decode) and the feedback snapshots. Every other draw reads the caller's array.
    bool edit_textures=alias || color_feedback;
    if(!edit_textures)for(const auto& texture:supplied_textures)if(texture.resident_image && texture.plan && texture.plan->gathered){edit_textures=true;break;}
    // Heap copy only on the editing path: an empty std::optional of 16 Textures still cost ~500 instructions per draw.
    std::unique_ptr<std::array<Texture,16>> owned_textures;if(edit_textures)owned_textures=std::make_unique<std::array<Texture,16>>(supplied_textures);
    const auto& textures=edit_textures?*owned_textures:supplied_textures;
    if(alias)for(const auto& texture:textures)if(texture.null_texture)return attempt.reject("alias_null_texture");
    if(color_feedback)attempt.path("texture_feedback_snapshot");
    // An image must never be sampled while this draw writes that very image:
    // gathered RTT views and contiguous feedback textures resolved to an
    // active attachment sample a GPU copy taken before the draw.
    if(edit_textures)for(auto& texture:*owned_textures)if(texture.resident_image && ((texture.plan && texture.plan->gathered) || (color_feedback && texture.valid && !texture.untiled))){
        bool feedback=false;
        for(const auto& surface:session.surfaces)if(surface.image==texture.resident_image){
            for(unsigned i=0;i<count;++i)if(colors[i].base && surface.same(colors[i],width,height,samples,r(0x208)&31,false))feedback=true;
            if(depth.base && surface.same(depth,width,height,samples,(r(0x208)>>5)&7,true))feedback=true;
        }
        if(feedback){session.rtt_space(texture.resident_image->memory_bytes);auto snapshot=vk.graphics_image(*session.batch,texture.resident_image->format,texture.resident_image->width,texture.resident_image->height,texture.resident_image->layers,texture.resident_image->depth);
            vk.graphics_copy(*session.batch,texture.resident_image,snapshot);texture.resident_image=std::move(snapshot);attempt.path(texture.plan && texture.plan->gathered?"rtt_feedback_snapshot":"texture_feedback_image_copy");}
    }
    if(device && alias){materialize();device=nullptr;}
    phase.next(HostStage::g_inputs);
    std::pair<unsigned,unsigned> known_extent;IndexBounds bounds(indices,device,state.rsx.draw.triangle_extent(indices,known_extent));
    // Vertex attributes frequently interleave one buffer: attributes whose spans overlap or touch
    // share one guest Range (one lease/claim, one copy) and index into it by byte offset.
    struct VertexExtent {bool used=false;unsigned group=0;std::uint64_t address=0,span=0;};
    struct VertexGroup {std::uint64_t address=0,span=0;};
    std::array<VertexExtent,16> vertex_extent{};std::array<VertexGroup,16> vertex_groups{};unsigned vertex_group_count=0;bool vertex_grouped=false;
    std::array<std::optional<GuestMemory::Range>,16> vertex_inputs;  // per group
    auto build_vertex_groups=[&]{
        std::array<unsigned,16> order{};unsigned n=0;
        for(unsigned a=0;a<16;++a)if(vertex_extent[a].used)order[n++]=a;
        std::sort(order.begin(),order.begin()+n,[&](unsigned x,unsigned y){return vertex_extent[x].address<vertex_extent[y].address || (vertex_extent[x].address==vertex_extent[y].address && x<y);});
        vertex_group_count=0;
        for(unsigned i=0;i<n;++i){auto& e=vertex_extent[order[i]];
            if(vertex_group_count){auto& g=vertex_groups[vertex_group_count-1];const auto end=std::max(g.address+g.span,e.address+e.span);
                if(e.address<=g.address+g.span && end-g.address<=UINT_MAX){g.span=end-g.address;e.group=vertex_group_count-1;continue;}}
            vertex_groups[vertex_group_count]={e.address,e.span};e.group=vertex_group_count++;}
        for(unsigned g=0;g<vertex_group_count;++g)vertex_inputs[g].emplace(state.memory.range_translated(vertex_groups[g].address,vertex_groups[g].span));
        vertex_grouped=true;
    };
    session.vertex_program.refresh(state.rsx);
    // Vertex fetches may depend on earlier primitives in this same draw.
    // Preparing all vertices up front is safe only for immutable input spans;
    // CPU thread-count/min-work gates are unrelated to this certification.
    if(!point && !lines)for(unsigned unit=0;unit<4;++unit)if(session.vertex_program.texture_mask&(1u<<unit)){
        const auto t=decode_texture(state,r,unit,true);
        if(!t.untiled){if(target_overlaps(depth,t.storage,t.storage_size,width*nx,height*ny))return attempt.reject("sequential_vertex_texture_feedback");
            for(unsigned i=0;i<count;++i)if(target_overlaps(colors[i],t.storage,t.storage_size,width*nx,height*ny))return attempt.reject("sequential_vertex_texture_feedback");}
    }
    if(!point && !lines && (device?device->vertices!=0:!indices.empty()) && state.rsx.draw.immediate_vertices.empty()){
        auto aliases=[&](const void* p,unsigned size){
            if(target_overlaps(depth,p,size,width*nx,height*ny))return true;
            for(unsigned i=0;i<count;++i)if(target_overlaps(colors[i],p,size,width*nx,height*ny))return true;
            return false;
        };
        const auto saved=state.memory.last_region;
        bool safe=true;
        try{
        for(unsigned a=0;a<16 && safe;++a)if(r(0x1ff0)&(1u<<a)){
            const auto format=r(0x1740+4*a);const unsigned type=format&15,size=(format>>4)&15,stride=(format>>8)&255;
            if(!size)continue;
            if(size>4){safe=false;break;}
            const unsigned lane=type==2?4:(type==1 || type==3 || type==5)?2:type==6?4:1;
            const unsigned bytes=type==6?4:lane*size,offset=r(0x1680+4*a);
            const auto interval=bounds.get(format>>16,(r(0x1fc0)&(1u<<a))!=0);
            const auto first=interval.first,last=interval.second;
            const auto at=std::uint64_t(offset&0x7fffffff)+r(0x1738)+std::uint64_t(stride)*first;
            const auto span=std::uint64_t(stride)*(last-first)+bytes;
            if(at+span>UINT64_C(0x100000000) || span>UINT_MAX){safe=false;break;}
            vertex_extent[a]={true,0,location_address(state.rsx,offset>>31,at,span),span};
        }
        if(safe){build_vertex_groups();
            for(unsigned g=0;g<vertex_group_count && safe;++g)for(const auto& part:vertex_inputs[g]->parts)if(aliases(part.data,part.size)){safe=false;break;}}
        }catch(const std::exception&){safe=false;}
        state.memory.last_region=saved;if(!safe)return attempt.reject("sequential_vertex_feedback_or_unmapped_input");
    }
    if(alias && session.conditional && session.resolve_conditional())return true;
    if(alias){for(auto& t:*owned_textures)materialize_texture(state,t);colors=supplied_colors;session.flush(true,nullptr,0,"attachment_alias");session.clear_surfaces();session.allow_resident_textures=false;
        try{for(unsigned unit=0;unit<16;++unit)if((*owned_textures)[unit].resident_image)(*owned_textures)[unit]=decode_texture(state,r,unit);}
        catch(...){session.allow_resident_textures=true;throw;}session.allow_resident_textures=true;}
    phase.next(HostStage::g_state);
    const bool packed=packed_color(r),srgb=!packed && r(0x1fec);
    NativeVulkan::GraphicsState fixed;fixed.scale_visual=true;fixed.width=width;fixed.height=height;fixed.samples=samples;fixed.colors=count;fixed.color=packed?VK_FORMAT_R32_UINT:color_format(r(0x208)&31,srgb);
    // A draw's occlusion count is consumed only by ZPASS reports (Pending::report, from 0x17cc). With lazy visibility a draw
    // with ZPASS counting off records no query: guest-visible reports and predicates are unchanged; only the host statistic
    // draw.pixels then excludes such draws.
    fixed.visibility=!rsx_lazy_visibility() || r(0x17cc)!=0;
    // Plane-independent fragment work (visual MSAA, NativeVulkan::graphics_ms_*): 2 samples at the standard 2x positions, no
    // 0x1d7c sample mask/alpha-to-coverage (the only per-plane fragment test, fragment_discard_state), no packed history
    // (read per plane), no line/point coverage emulation.
    {const unsigned control=r.get(0x1d7c,0xffff0000);fixed.ms_ok=!alias && !point && !lines && !packed && aa==3 && ((control>>16)&15)==15 && !(control&16);}
    if(!alias && (count && !vk.graphics_format(fixed.color)))return attempt.reject("device_color_format");
    if(depth.base)fixed.depth=((r(0x208)>>5)&7)==1?VK_FORMAT_D16_UNORM:VK_FORMAT_D24_UNORM_S8_UINT;
    if(!alias && (fixed.depth!=VK_FORMAT_UNDEFINED && !vk.graphics_format(fixed.depth,true)))return attempt.reject("device_depth_format");
    fixed.scissor={{int(x0),int(y0)},{x1-x0,y1-y0}};
    fixed.raster.frontFace=r.get(0x1834,0x901)==0x901?VK_FRONT_FACE_COUNTER_CLOCKWISE:VK_FRONT_FACE_CLOCKWISE;
    fixed.z.depthTestEnable=depth.base && ((r(0xa74)&1) || (r(0xa70)&1));fixed.z.depthWriteEnable=depth.base && (r(0xa70)&1);
    fixed.z.depthCompareOp=(r(0xa74)&1)?comparison(r.get(0xa6c,0x207)):VK_COMPARE_OP_ALWAYS;
    // Bounds require optional feature support; CPU fallback preserves semantics.
    if(!alias && (r(0x380) && !vk.graphics_depth_bounds()))return attempt.reject("device_depth_bounds");
    fixed.z.depthBoundsTestEnable=r(0x380)&1;
    fixed.raster.depthBiasEnable=!point && !lines && (r(0xa68)&1);
    const unsigned format=r(0x208)&31;const bool z24=((r(0x208)>>5)&7)==2;
    for(unsigned i=0;i<2;++i){const Stencil stencil(r,i!=0,z24);if(!stencil.enabled)continue;fixed.z.stencilTestEnable=true;
        auto& f=i?fixed.z.back:fixed.z.front;f.failOp=stencil_op_vk(stencil.fail);f.passOp=stencil_op_vk(stencil.zpass);f.depthFailOp=stencil_op_vk(stencil.zfail);
        f.compareOp=comparison(stencil.func);f.compareMask=stencil.read_mask;f.writeMask=stencil.mask;f.reference=stencil.ref;}
    if(point || lines)fixed.z.back=fixed.z.front;
    fixed.logic_enable=count && !packed && (r(0x374)&1);fixed.logic=static_cast<VkLogicOp>(r.get(0x378,0x1503)-0x1500);
    if(fixed.logic_enable && !vk.graphics_logic())return attempt.reject("device_logic_op");
    const unsigned blend_mask=(r(0x374)&1)?0:((r(0x310)&1)|(r(0x36c)&14));
    for(unsigned i=0;i<count;++i){auto& b=fixed.blend[i];const auto mask=i?((r(0x370)>>(4*i))&15):0;
        b.colorWriteMask=i?((mask&1?VK_COLOR_COMPONENT_A_BIT:0)|(mask&2?VK_COLOR_COMPONENT_R_BIT:0)|(mask&4?VK_COLOR_COMPONENT_G_BIT:0)|(mask&8?VK_COLOR_COMPONENT_B_BIT:0)):
            ((r.get(0x324,0x01010101)&0x10000?VK_COLOR_COMPONENT_R_BIT:0)|(r.get(0x324,0x01010101)&0x100?VK_COLOR_COMPONENT_G_BIT:0)|(r.get(0x324,0x01010101)&1?VK_COLOR_COMPONENT_B_BIT:0)|(r.get(0x324,0x01010101)&0x1000000?VK_COLOR_COMPONENT_A_BIT:0));
        b.blendEnable=(blend_mask>>i)&1;if(b.blendEnable){b.srcColorBlendFactor=factor(r(0x314)&65535);b.srcAlphaBlendFactor=factor(r(0x314)>>16);b.dstColorBlendFactor=factor(r(0x318)&65535);b.dstAlphaBlendFactor=factor(r(0x318)>>16);b.colorBlendOp=equation(r(0x320)&65535);b.alphaBlendOp=equation(r(0x320)>>16);}
        if(packed){b.blendEnable=false;b.colorWriteMask=b.colorWriteMask?VK_COLOR_COMPONENT_R_BIT:0;}}
    // A packed attachment with every guest channel masked returns exactly its
    // old word. Suppress that redundant store in Vulkan as well. With no color
    // writes, all primitives can share one immutable history snapshot; depth,
    // stencil, fragment discard and precise sample counts still execute.
    bool sequential_packed=false;
    if(packed)for(unsigned i=0;i<count;++i)sequential_packed|=fixed.blend[i].colorWriteMask!=0;
    fixed.constants=format==11?Vec4{half_float(r(0x31c)&65535),half_float(r(0x31c)>>16),half_float(r(0x37c)&65535),half_float(r(0x37c)>>16)}:
        Vec4{float((r(0x31c)>>16)&255)/255,float((r(0x31c)>>8)&255)/255,float(r(0x31c)&255)/255,float(r(0x31c)>>24)/255};
    if(!alias && (!vk.graphics_sample_interpolation()))return attempt.reject("device_sample_interpolation");
    if(!alias && (!vk.graphics_fragment_storage()))return attempt.reject("device_fragment_storage");
    if(!alias && (r(0x17cc) && !vk.graphics_precise_visibility()))return attempt.reject("device_precise_visibility");
    if(!alias && (count>1 && !vk.graphics_independent_blend()))return attempt.reject("device_independent_blend");
    phase.next(HostStage::g_shaders);
    rsxvk::FragmentFailure failure;std::shared_ptr<const std::string> fs;
    if(alias){auto source=rsxvk::fragment(fp,true,false,&failure);if(source)fs=std::make_shared<const std::string>(std::move(*source));}
    else fs=fragment_source(fp,r,count,textures,lines!=nullptr,&failure,width,height);
    if(fs && !vk.graphics_quad_operations() && fs->find("subgroupQuadBroadcast")!=std::string::npos)return attempt.reject("device_fragment_quad_operations");
    if(!fs){if(attempt.stats.enabled)attempt.stats.detail("fragment_program",fragment_failure(fp,failure,r));return attempt.reject("fragment_program");}
    auto& data=session.draw_data;if(alias)session.serialize_registers(data,width,height);else session.serialize_draw_registers(data,width,height);
    data[11]=point?1u:lines?2u:0u;
    const float sx=bits_float(r(0xa30)),sy=bits_float(r(0xa34)),sz=bits_float(r(0xa38));
    const float vx=bits_float(r(0xa20)),vy=bits_float(r(0xa24)),vz=bits_float(r(0xa28));
    const bool fetch_candidate=!alias && !point && !lines && r.get(0x368,0x1d01)!=0x1d00 && vk.graphics_vertex_fetch();
    auto hw_vertex=session.vertex_program.source_current(state.rsx,fetch_candidate?(state.rsx.draw.immediate_vertices.empty()?1:2):0);
    {   // ANYPS3_RSX_VP_TRACE=1: vertex programs that cannot run on the GPU (distinct start/length, first 40): scalar/vector opcodes per instruction.
        static const bool trace=std::getenv("ANYPS3_RSX_VP_TRACE")!=nullptr;static std::set<std::string> seen;
        if(trace && !hw_vertex && seen.size()<40){
            const auto& gpu=std::as_const(state.rsx);const unsigned first=r.get(0x1ea0,0);std::ostringstream text;text<<"start="<<first<<" vertices="<<(device?device->vertices:unsigned(indices.size()*3))<<" ops:";
            for(unsigned pc=first;pc<544;++pc){
                bool written=true;for(unsigned c=0;c<4;++c)written&=gpu.transform_program_written[pc*4+c];if(!written){text<<" [unwritten@"<<pc<<"]";break;}
                const auto* d=gpu.transform_program.data()+pc*4;const unsigned vec=(d[1]>>22)&31,sca=(d[1]>>27)&31;
                text<<" "<<pc<<":v"<<vec<<"/s"<<sca;
                if(sca==8||sca==9||sca==10||sca==12||sca==17||sca==18)text<<"->"<<(((d[0]>>23)&1)<<9|((d[2]&63)<<3)|(d[3]>>29))<<(sca==17||sca==18?" bool":"");
                if(d[3]&1){text<<" END";break;}
            }
            if(seen.insert(text.str()).second)std::fprintf(stderr,"VP_TRACE %s\n",text.str().c_str());
        }
    }
    if(attempt.stats.enabled){
        if(alias)attempt.path("geometry_attachment_alias");
        else if(point)attempt.path("geometry_points");
        else if(lines)attempt.path("geometry_lines");
        else if(!state.rsx.draw.immediate_vertices.empty())attempt.path("geometry_immediate");
        else if(!hw_vertex){
            attempt.path("geometry_vertex_program");bool classified=false;
            for(unsigned pc=r.get(0x1ea0,0);pc<544;++pc){
                const auto* d=std::as_const(state.rsx).transform_program.data()+pc*4;
                bool written=true;for(unsigned c=0;c<4;++c)written&=std::as_const(state.rsx).transform_program_written[pc*4+c];
                if(!written){attempt.path("geometry_vp_unwritten");classified=true;break;}
                const unsigned vec=(d[1]>>22)&31,sca=(d[1]>>27)&31;
                if(vec==25){attempt.path("geometry_vp_texture");classified=true;break;}
                if(sca==8 || sca==9 || sca==10 || sca==12 || sca==17 || sca==18){attempt.path("geometry_vp_branch");classified=true;break;}
                if(sca==11){attempt.path("geometry_vp_unverified_CLI");classified=true;break;}
                if(d[3]&1)break;
            }
            if(!classified)attempt.path("geometry_vp_register_constant_or_opcode");
        }
        else if(r.get(0x368,0x1d01)==0x1d00)attempt.path("geometry_flat_shading");
        else if(!vk.graphics_vertex_fetch())attempt.path("geometry_device_fetch");
        else attempt.path("geometry_direct");
    }
    const bool direct_immediate=!alias && !point && !lines && !state.rsx.draw.immediate_vertices.empty() && hw_vertex && r.get(0x368,0x1d01)!=0x1d00 && vk.graphics_vertex_fetch();
    const bool direct=direct_immediate || (!alias && !point && !lines && state.rsx.draw.immediate_vertices.empty() && hw_vertex && r.get(0x368,0x1d01)!=0x1d00 && vk.graphics_vertex_fetch());
    if(device && !direct){materialize();device=nullptr;bounds.device=nullptr;}
    bool raw_vertex=!alias && !point && !lines && bool(hw_vertex) && r.get(0x368,0x1d01)!=0x1d00;
    std::vector<unsigned> line_data;
    std::uint64_t tail_bytes=0;unsigned tail_attributes=0;std::array<unsigned,16> tail_rel{},group_rel{};std::array<bool,16> group_added{};
    struct TailPad {std::uint64_t at,n;};std::array<TailPad,16> tail_pads{};unsigned tail_pad_count=0;DrawReadBatch input_reads;
    std::vector<std::array<unsigned,3>> accepted;
    phase.next(HostStage::g_data);
    unsigned vertices=0;
    if(!direct)for(const auto& tri:indices){std::array<const ShadedVertex*,3> v{&vertex(tri[0]),&vertex(tri[1]),&vertex(tri[2])};
        const auto polygon=clip_polygon(v,point?0:r(0x1478),r.get(0x1ff4,0x3fffff),sz,vz,bits_float(r.get(0x394,0)),bits_float(r.get(0x398,0x3f800000)));
        if(polygon.size()!=3)raw_vertex=false;
        else for(unsigned n=0;n<3;++n)if(polygon[n].clip!=v[n]->clip)raw_vertex=false;
        for(unsigned fan=1;fan+1<polygon.size();++fan){std::array<ShadedVertex,3> t{polygon[0],polygon[fan],polygon[fan+1]};
            for(auto& p:t){const float iw=1.f/p.clip[3];p.clip={p.clip[0]*iw*sx+vx,p.clip[1]*iw*sy+vy,p.clip[2]*iw*sz+vz,iw};
                if(!(iw>0))return attempt.reject("nonpositive_clip_w");
                if(r.get(0x368,0x1d01)==0x1d00)for(unsigned n=1;n<=4;++n)p.o[n]=v[r(0x1454)?0:2]->o[n];}
            const float area=(t[1].clip[0]-t[0].clip[0])*(t[2].clip[1]-t[0].clip[1])-(t[2].clip[0]-t[0].clip[0])*(t[1].clip[1]-t[0].clip[1]);
            if(area==0)continue;
            const bool front=r.get(0x1834,0x901)==0x901?area<0:!(area<0);const auto face=r.get(0x1830,0x405);
            if(!point && !lines && (r(0x183c)&1) && (face==0x408 || (face==0x404 && front) || (face==0x405 && !front)))continue;
            for(const auto& p:t){rsxvk::append(data,p.clip);for(const auto& o:p.o)rsxvk::append(data,o);}vertices+=3;accepted.push_back(tri);
            if(lines){const auto& line=lines->at(tri[0]);rsxvk::append(line_data,{line.x,line.y,line.dx,line.dy});
                line_data.insert(line_data.end(),{line.width,0,0,0});
                for(unsigned n=0;n<2;++n){rsxvk::append(line_data,t[n].clip);for(const auto& o:t[n].o)rsxvk::append(line_data,o);}
                for(const auto& o:line.provoking)rsxvk::append(line_data,o);}
        }
    }
    if(raw_vertex && !direct){
        data.resize(alias?2576:560);
        for(const auto& tri:accepted)for(unsigned index:tri)for(unsigned a=0;a<16;++a){
            const Vec4 input=!(r(0x1ff0)&(1u<<a))?Vec4{0,0,0,1}:state.rsx.draw.immediate_vertices.empty()?fetch_attribute(state,r,a,index):register_attribute(state.rsx.draw.immediate_vertices[index][a]);
            rsxvk::append(data,input);
        }
    }
    if(direct_immediate){
        HostPhase upload_phase(HostStage::vertex_upload);

        for(const auto& tri:indices)for(unsigned index:tri){
            if(index>=state.rsx.draw.immediate_vertices.size())throw std::runtime_error("RSX immediate vertex index out of range");
            for(unsigned a=0;a<16;++a)rsxvk::append(data,(r(0x1ff0)&(1u<<a))?register_attribute(state.rsx.draw.immediate_vertices[index][a]):Vec4{0,0,0,1});
        }
        vertices=indices.size()*3;
        if(r(0x183c)&1){const auto face=r.get(0x1830,0x405);fixed.raster.cullMode=face==0x408?VK_CULL_MODE_FRONT_AND_BACK:face==0x404?VK_CULL_MODE_FRONT_BIT:VK_CULL_MODE_BACK_BIT;}
        if(host_stats().enabled)++host_stats().gpu_vertex;
        attempt.path("gpu_immediate_vertex");
    }else if(direct){
        HostPhase upload_phase(HostStage::vertex_upload);
        if(host_stats().enabled)++host_stats().gpu_vertex;

        data[13]=data.size();
        if(device){data.push_back(device->primitive);data.push_back(device->ranges.size());
            for(const auto& range:device->ranges){data.push_back(range.first);data.push_back(range.second);}
            vertices=device->vertices;data[15]=2;attempt.path("gpu_array_topology");}
        else{static_assert(sizeof(std::array<unsigned,3>)==3*sizeof(unsigned),"packed triangle indices");
            const auto* first=reinterpret_cast<const unsigned*>(indices.data());data.insert(data.end(),first,first+indices.size()*3);
            vertices=indices.size()*3;data[15]=1;}
        data[12]=data.size();const unsigned desc=data.size();data.resize(desc+16*8);
        std::uint64_t tail_used=0;
        if(!vertex_grouped && vertices){
            for(unsigned a=0;a<16;++a){const unsigned f=(r(0x1ff0)&(1u<<a))?r(0x1740+4*a):0;
                const unsigned type=f&15,n=(f>>4)&15,stride=(f>>8)&255,frequency=f>>16;
                if(!n)continue;
                if(n>4 || type==0 || type>7)throw std::runtime_error("unsupported RSX vertex attribute type/size");
                const auto interval=bounds.get(frequency,(r(0x1fc0)&(1u<<a))!=0);
                const unsigned width=type==2?4:(type==1 || type==3 || type==5)?2:1,bytes=type==6?4:width*n,offset=r(0x1680+4*a);
                const auto at=std::uint64_t(offset&0x7fffffff)+r(0x1738)+std::uint64_t(stride)*interval.first;
                const auto span=std::uint64_t(stride)*(interval.second-interval.first)+bytes;
                if(at>UINT_MAX || span>UINT_MAX || at+span>UINT64_C(0x100000000))throw std::runtime_error("RSX vertex address overflow");
                vertex_extent[a]={true,0,location_address(state.rsx,offset>>31,at,span),span};
            }
            build_vertex_groups();
        }
        const unsigned input_mask=r(0x1ff0);auto* words=data.data();
        for(unsigned a=0;a<16;++a){const unsigned d=desc+a*8,f=(input_mask&(1u<<a))?r(0x1740+4*a):0;
            const unsigned type=f&15,n=(f>>4)&15,stride=(f>>8)&255,frequency=f>>16;
            data[d]=f;
            // A disabled input reads (0,0,0,1): its words directly (float_bits(1.f)==0x3f800000).
            if(!(input_mask&(1u<<a))){words[d+4]=words[d+5]=words[d+6]=0;words[d+7]=0x3f800000u;continue;}
            if(!n){const auto v=register_attribute(state.rsx.draw.immediate[a]);
                for(unsigned c=0;c<4;++c)data[d+4+c]=float_bits(v[c]);
                continue;}
            if(n>4 || type==0 || type>7)throw std::runtime_error("unsupported RSX vertex attribute type/size");
            if(!vertices)continue;
            const auto interval=bounds.get(frequency,(r(0x1fc0)&(1u<<a))!=0);
            const auto first=interval.first,last=interval.second;
            const unsigned width=type==2?4:(type==1 || type==3 || type==5)?2:1,bytes=type==6?4:width*n,offset=r(0x1680+4*a);
            const auto at=std::uint64_t(offset&0x7fffffff)+r(0x1738)+std::uint64_t(stride)*first;
            const auto span=std::uint64_t(stride)*(last-first)+bytes;
            if(at>UINT_MAX || span>UINT_MAX || at+span>UINT64_C(0x100000000))throw std::runtime_error("RSX vertex address overflow");
            const auto& extent=vertex_extent[a];
            if(!extent.used || extent.span!=span || extent.address!=location_address(state.rsx,offset>>31,at,span))throw std::runtime_error("RSX vertex input extent changed during draw preparation");
            const auto& group=vertex_groups[extent.group];
            // Bytes go to the persistent tail scratch, word padded with zeros;
            // data[d+1] is rebased onto the final word count before upload.
            // Vertex bytes become one immutable staging slice addressed with
            // absolute device-buffer byte offsets (data[d+1]), word padded
            // with zeros, read directly into staging right before recording.
            data[d+1]=0;data[d+2]=first;data[d+3]=stride;tail_attributes|=1u<<a;
            if(!group_added[extent.group]){group_added[extent.group]=true;group_rel[extent.group]=unsigned(tail_used);
                const auto padded=(group.span+3)&~std::uint64_t(3);
                if(padded>group.span)tail_pads[tail_pad_count++]={tail_used+group.span,padded-group.span};
                input_reads.add(*vertex_inputs[extent.group],tail_used);tail_used+=padded;::RsxPerfCounters::add(::RsxPerfCounters::vertex_bytes,group.span);}
            tail_rel[a]=group_rel[extent.group]+unsigned(extent.address-group.address);
        }
        if(r(0x183c)&1){const auto face=r.get(0x1830,0x405);fixed.raster.cullMode=face==0x408?VK_CULL_MODE_FRONT_AND_BACK:face==0x404?VK_CULL_MODE_FRONT_BIT:VK_CULL_MODE_BACK_BIT;}

        tail_bytes=tail_used;
        // Per-primitive packed draws may change batches between uses: keep
        // one snapshot (same read point as before) and copy it per upload.
        if(sequential_packed && tail_bytes){if(session.vertex_tail.size()<tail_bytes)session.vertex_tail.resize(tail_bytes);
            session.read_inputs(input_reads,session.vertex_tail.data());for(unsigned i=0;i<tail_pad_count;++i)std::memset(session.vertex_tail.data()+tail_pads[i].at,0,tail_pads[i].n);}
        attempt.path("gpu_vertex_fetch");
    }
    // Shader constants are draw data, not shader-source identity. Animated
    // matrices/tints must not trigger glslang or VkPipeline creation per draw.
    if(alias){data[2]=data.size();session.append_vertex_constants(data);
        data[14]=data.size();for(const auto& inst:fp)rsxvk::append(data,inst.constant);}
    else{session.refresh_vertex_constants();session.refresh_fragment_constants(fp);}
    const unsigned line_base=data.size();if(lines){data[10]=line_base;data.insert(data.end(),line_data.begin(),line_data.end());}
    std::array<std::shared_ptr<NativeVulkan::GraphicsBuffer>,16> raw_buffers{};
    std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> texture_images{};
    phase.next(HostStage::g_tex);
    for(unsigned unit=0;unit<16;++unit){const auto& t=textures[unit];if(!t.valid)continue;
        texture_images[unit]=t.resident_image?t.resident_image:(!alias?session.texture_image(t):nullptr);
        const unsigned desc=(alias?2064:48)+unit*32;data[desc]=data.size();data[desc+1]=t.mip_count;data[desc+2]=t.unnormalized;data[desc+3]=t.wrap_s;data[desc+4]=t.wrap_t;
        data[desc+5]=t.min;data[desc+6]=t.mag;data[desc+7]=float_bits(t.bias);data[desc+8]=float_bits(t.min_lod);data[desc+9]=float_bits(t.max_lod);
        data[desc+10]=t.gamma;data[desc+11]=t.features;data[desc+12]=t.signed_mask;data[desc+13]=t.biased;data[desc+14]=t.alpha_kill;
        const Vec4 border=t.shadow?t.border:texture_input(t,t.border,true);   // pure: once per unit, not per channel
        for(unsigned c=0;c<4;++c){data[desc+15+c]=t.remap_channel[c];data[desc+19+c]=t.remap_control[c];data[desc+23+c]=float_bits(border[c]);}
        data[desc+27]=t.dimension;data[desc+28]=t.cube;data[desc+29]=t.shadow;data[desc+30]=t.wrap_r;

        const unsigned raw_levels=data.size();data[desc+31]=raw_levels;data.resize(raw_levels+t.levels.size()*8);
        for(unsigned l=0;l<t.levels.size();++l){const auto& v=t.levels[l];const auto at=raw_levels+l*8;
            data[at]=t.plan?v.offset:v.data-t.levels[0].data;data[at+1]=v.pitch;data[at+2]=v.backing_width;data[at+3]=v.backing_height;data[at+4]=v.border;data[at+5]=t.format;data[at+6]=t.swizzled;data[at+7]=v.depth;}
        data[desc]=data.size();const unsigned levels=data.size();data.resize(levels+t.levels.size()*4);
        for(unsigned l=0;l<t.levels.size();++l){const auto& level=t.levels[l];data[levels+4*l]=data.size();data[levels+4*l+1]=level.width;data[levels+4*l+2]=level.height;data[levels+4*l+3]=level.depth;
            if(alias && !t.resident_image)for(unsigned z=0;z<level.depth;++z)for(unsigned y=0;y<level.height;++y)for(unsigned x=0;x<level.width;++x){auto c=texel(t,level,x,y,z);rsxvk::append(data,t.shadow?c:texture_input(t,c,false));}}}
    if(vertices==0)return true;
    if(alias){bool parallel_alias=false;if(!draw_alias(session,std::move(data),*fs,colors,depth,width,height,samples,count,vertices,x0,y0,x1,y1,parallel_alias))return attempt.reject("color_depth_alias_device_capacity");
        attempt.path(parallel_alias?"parallel_attachment_alias":"component_attachment_alias");return true;}
    attempt.path("scalar_fragment");
    const auto visual_blob_bytes=vk.graphics_render_scale>1?std::uint64_t(data.size())*4:0;
    if(visual_blob_bytes && !session.ensure_upload_capacity(data.size()*4+tail_bytes+visual_blob_bytes+128*1024))throw std::runtime_error("RSX paired draw exceeds staging capacity");
    if(session.batch->used+data.size()*4+visual_blob_bytes+32*1024*1024>session.batch->capacity || session.batch->visibility_count+samples>=1000 || session.batch->pipeline_pins.size()>=64)session.submit_async("batch_capacity");
    std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};std::shared_ptr<NativeVulkan::GraphicsImage> z;
    phase.next(HostStage::g_bind);
    for(unsigned i=0;i<count;++i)images[i]=colors[i].base?session.bind(colors[i],width,height,samples,format,false,srgb,packed).image:session.history_image(fixed.color,width,height,samples,4+i);
    if(depth.base)z=session.bind(depth,width,height,samples,(r(0x208)>>5)&7,true).image;
    const std::array<std::array<float,2>,4> positions=aa==3?std::array<std::array<float,2>,4>{{{.75f,.75f},{.25f,.25f},{0,0},{0,0}}}:
        aa==4?std::array<std::array<float,2>,4>{{{.25f,.25f},{.75f,.25f},{.25f,.75f},{.75f,.75f}}}:
        aa==5?std::array<std::array<float,2>,4>{{{.375f,.125f},{.875f,.375f},{.125f,.625f},{.625f,.875f}}}:
        std::array<std::array<float,2>,4>{{{.5f,.5f},{0,0},{0,0},{0,0}}};
    if(packed)for(unsigned i=0;i<count;++i)texture_images[16+i]=session.history_image(fixed.color,width,height,samples,i);
    std::uint64_t reuse=UINT64_MAX;
    for(unsigned primitive=0;primitive<(sequential_packed?vertices/3:1u);++primitive){
    if(session.batch->used+(reuse==UINT64_MAX?data.size()*4+tail_bytes:0)+visual_blob_bytes+128*1024>session.batch->capacity || session.batch->visibility_count+samples>=1000 || session.batch->pipeline_pins.size()>=64){session.submit_async("batch_capacity");reuse=UINT64_MAX;}
    if(packed)for(unsigned i=0;i<count;++i){vk.graphics_copy(*session.batch,images[i],texture_images[16+i]);::RsxPerfCounters::add(::RsxPerfCounters::history_copies);}
    const auto first=session.batch->visibility_count;
    // graphics_draw allocates the descriptor blob at aligned staging offset.
    phase.next(HostStage::record);
    static thread_local std::shared_ptr<const std::string> pass_vertex;static thread_local bool pass_layered=false;
    if(!pass_vertex || pass_layered!=rsx_layered_shaders().load(std::memory_order_relaxed)){pass_layered=rsx_layered_shaders().load(std::memory_order_relaxed);pass_vertex=std::make_shared<const std::string>(vertex_source());}const auto& vertex_owner=raw_vertex?hw_vertex:pass_vertex;const auto& vertex_shader=*vertex_owner;
    session.bind_draw_blocks(data,direct);
    phase.next(HostStage::tail);
    if(tail_bytes && reuse==UINT64_MAX){
        const auto tail_at=vk.graphics_reserve_upload(*session.batch,tail_bytes);if(tail_at+tail_bytes>UINT_MAX)throw std::runtime_error("RSX graphics vertex slice offset overflow");
        for(unsigned a=0;a<16;++a)if(tail_attributes&(1u<<a))data[data[12]+a*8+1]=unsigned(tail_at)+tail_rel[a];
        auto* out=static_cast<std::uint8_t*>(session.batch->map)+tail_at;
        if(sequential_packed)std::memcpy(out,session.vertex_tail.data(),tail_bytes);
        else{session.read_inputs(input_reads,out);for(unsigned i=0;i<tail_pad_count;++i)std::memset(out+tail_pads[i].at,0,tail_pads[i].n);}
    }
    ::RsxPerfCounters::add(::RsxPerfCounters::blob_bytes,data.size()*4);
    phase.next(HostStage::draw);
    const auto actual=[&]{
        NativeVulkan::ConditionalScope condition(vk,*session.batch,session.conditional?session.conditional->buffer:nullptr,session.conditional?session.conditional->offset:0);
        return vk.graphics_draw(*session.batch,fixed,images,z,vertex_shader,*fs,data,sequential_packed?3:vertices,positions,bits_float(r(0xa7c)),bits_float(r(0xa78)),bits_float(r(0x384)),bits_float(r.get(0x388,0x3f800000)),texture_images,sequential_packed?primitive*3:0,reuse,raw_buffers,vertex_owner,fs,nullptr,0,true);
    }();
    if(actual+data.size()*4+tail_bytes>UINT_MAX)throw std::runtime_error("RSX graphics status offset overflow");
    reuse=actual;
    session.pending.push_back({static_cast<unsigned>(actual+7*4),first,vk.graphics_last_queries,r(0x17cc)!=0});
    for(auto& surface:session.surfaces){for(unsigned i=0;i<count;++i)if(surface.image==images[i])session.dirty_surface(surface);
        if(surface.image==z && ((r(0xa70)&1) || fixed.z.stencilTestEnable))session.dirty_surface(surface);}
    }
    phase.next(HostStage::kick);session.kick_draws();
    state.rsx.draw.triangles+=vertices/3;::RsxPerfCounters::add(::RsxPerfCounters::gpu_draws);return true;
}

static bool clear(Session& session,unsigned mask){
    ::RsxPerfCounters::add(::RsxPerfCounters::clears);
    GuestMemory::RawYield payload(session.state.memory);
    auto& state=session.state;const Reg r{state.rsx};
    auto reject=[&](const char* reason){if(fallback_stats().enabled)++fallback_stats().paths[std::string("clear_reject_")+reason];return false;};
    if(mask&~0xf3u)return reject("mask");
    const unsigned sf=r(0x208),format=sf&31,df=(sf>>5)&7,raster=(sf>>8)&15,aa=(sf>>12)&15;
    if(raster!=1 && raster!=2)return reject("raster");
    if(aa!=0 && aa!=3 && aa!=4 && aa!=5)return reject("samples");
    if(r.get(0x1d98,0x0fff0000)!=0x0fff0000 || r.get(0x1d9c,0x0fff0000)!=0x0fff0000)return reject("rectangle");
    if(raster==2 && r(0x2b8))return reject("swizzle_window");
    const unsigned width=r(0x200)>>16,height=r(0x204)>>16,samples=aa==0?1:aa==3?2:4,nx=samples>1?2:1,ny=samples==4?2:1;
    if(!width || !height)return true;
    if(width>4096 || height>4096)return reject("extent");
    const unsigned x0=std::min(width,r(0x8c0)&65535),y0=std::min(height,r(0x8c4)&65535);
    const unsigned x1=std::min(width,(r(0x8c0)&65535)+(r(0x8c0)>>16)),y1=std::min(height,(r(0x8c4)&65535)+(r(0x8c4)>>16));
    if(x0>=x1 || y0>=y1)return true;
    // The CPU clear writes packed F24 bits. The depth image stores those
    // same 24 bits as UNORM; clearing their storage does not implement or
    // change floating-depth comparison semantics. Keep the CPU nonfinite stop.
    if((r(0x3b0)&0x1000) && (mask&1) && df==2 && (r(0x1d8c)&0xff000000u)==0xff000000u)
        throw std::runtime_error("unsupported RSX nonfinite float clear depth");
    const bool use_color=(mask&0xf0) && !float_color(format) && r(0x220);
    if(use_color && color_format(format)==VK_FORMAT_UNDEFINED)return reject("color_format");
    const bool integer=use_color && color_format(format)==VK_FORMAT_R32_UINT;
    const bool use_depth=(mask&1) || ((mask&2) && df==2 && (r(0x32c)&255));
    if((mask&3) && df!=1 && df!=2)return reject("depth_format");
    const unsigned lw=(sf>>16)&255,lh=sf>>24;
    if(raster==2 && (lw>12 || lh>12 || width>(1u<<lw) || height>(1u<<lh)))return reject("swizzle_extent");
    const unsigned sw=raster==2?(1u<<lw)*nx:0,sh=raster==2?(1u<<lh)*ny:0;
    std::vector<unsigned> ids;
    if(use_color)switch(r(0x220)){case 1:ids={0};break;case 2:ids={1};break;case 0x13:ids={0,1};break;case 0x17:ids={0,1,2};break;case 0x1f:ids={0,1,2,3};break;default:return reject("targets");}
    const unsigned packed=r(0x1d90),cb=use_color?color_bytes(format):4;
    unsigned clear_word=packed,color_bits=0;
    if(use_color){
        if(format==1 || format==2){
            color_bits=(mask&0x10?0x7c00u:0)|(mask&0x20?0x3e0u:0)|(mask&0x40?0x1fu:0)|(mask&0x80?0x8000u:0);
            clear_word=format==1?(packed&0x7fffu):(packed|0x8000u);
        }else if(format==3)color_bits=(mask&0x10?0xf800u:0)|(mask&0x20?0x7e0u:0)|(mask&0x40?0x1fu:0);
        else if(format==9)color_bits=(mask&0x40)?0xffu:0;
        else if(format==10)color_bits=(mask&0x20?0xff00u:0)|(mask&0x40?0xffu:0);
        else color_bits=(mask&0x10?0xff0000u:0)|(mask&0x20?0xff00u:0)|(mask&0x40?0xffu:0)|(mask&0x80?0xff000000u:0);
    }
    if(!color_bits)ids.clear();
    const unsigned full_bits=cb==1?255u:cb==2?65535u:UINT_MAX;
    clear_word&=full_bits; // R32_UINT storage zero-extends sub-word pixels.
    const bool full_rect=!x0 && !y0 && x1==width && y1==height;
    const bool full_color=color_bits==full_bits;
    const unsigned stencil_mask=(df==2 && (mask&2))?(r(0x32c)&255):0;
    const bool full_depth=(mask&1) && (df==1 || stencil_mask==255);
    auto bind_clear=[&](unsigned context,unsigned offset,unsigned pitch,unsigned bpp){
        const auto start=std::uint64_t(offset)+std::uint64_t(r(0x2b8)>>16)*ny*pitch+std::uint64_t(r(0x2b8)&65535)*nx*bpp;
        if(start>UINT_MAX)throw std::runtime_error("RSX clear window offset overflow");
        return bind_target(state,context,unsigned(start),pitch,width*nx,height*ny,bpp,sw,sh);
    };
    static constexpr unsigned contexts[]={0x194,0x18c,0x1b4,0x1b8},offsets[]={0x210,0x218,0x288,0x28c},pitches[]={0x20c,0x21c,0x280,0x284};
    std::vector<Target> targets;
    for(unsigned id:ids)targets.push_back(bind_clear(r(contexts[id]),r(offsets[id]),r(pitches[id]),cb));
    Target depth;if(use_depth)depth=bind_clear(r(0x198),r(0x214),r(0x22c),df==1?2:4);
    for(unsigned i=0;i<targets.size();++i){if(depth.base && targets[i].mapping->overlaps(*depth.mapping))throw std::runtime_error("overlapping RSX clear attachments");
        for(unsigned j=0;j<i;++j)if(targets[i].mapping->overlaps(*targets[j].mapping))throw std::runtime_error("overlapping RSX clear attachments");}
    if(targets.empty() && !depth.base)return true;
    NativeVulkan::GraphicsState fixed;fixed.scale_visual=true;fixed.width=width;fixed.height=height;fixed.samples=samples;fixed.colors=targets.size();fixed.color=color_format(format);
    fixed.depth=depth.base?(df==1?VK_FORMAT_D16_UNORM:VK_FORMAT_D24_UNORM_S8_UINT):VK_FORMAT_UNDEFINED;
    if(fixed.colors && !session.vk.graphics_format(fixed.color))return reject("device_color_format");
    if(depth.base && !session.vk.graphics_format(fixed.depth,true))return reject("device_depth_format");
    fixed.scissor={{int(x0),int(y0)},{x1-x0,y1-y0}};
    for(unsigned i=0;i<fixed.colors;++i)fixed.blend[i].colorWriteMask=integer?VK_COLOR_COMPONENT_R_BIT:
        (mask&0x10?VK_COLOR_COMPONENT_R_BIT:0)|(mask&0x20?VK_COLOR_COMPONENT_G_BIT:0)|(mask&0x40?VK_COLOR_COMPONENT_B_BIT:0)|(mask&0x80?VK_COLOR_COMPONENT_A_BIT:0);
    fixed.z.depthTestEnable=fixed.z.depthWriteEnable=depth.base && (mask&1);fixed.z.depthCompareOp=VK_COMPARE_OP_ALWAYS;
    if(depth.base && df==2 && stencil_mask){fixed.z.stencilTestEnable=true;fixed.z.front.compareOp=VK_COMPARE_OP_ALWAYS;fixed.z.front.passOp=VK_STENCIL_OP_REPLACE;
        fixed.z.front.writeMask=stencil_mask;fixed.z.front.compareMask=255;fixed.z.front.reference=r(0x1d8c)&255;fixed.z.back=fixed.z.front;}
    std::array<std::uint8_t,4> clear_bytes{};write_be(clear_bytes.data(),format==3?2:4,packed);
    const auto color=read_color(clear_bytes.data(),format==3?3:8);
    const float z=float(df==1?(r(0x1d8c)&65535):(r(0x1d8c)>>8))/float(df==1?65535:16777215);
    std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};std::shared_ptr<NativeVulkan::GraphicsImage> zi;
    for(unsigned i=0;i<fixed.colors;++i)images[i]=session.bind(targets[i],width,height,samples,format,false,false,integer,full_rect && full_color).image;
    if(depth.base)zi=session.bind(depth,width,height,samples,df,true,false,false,full_rect && full_depth).image;
    if(full_rect && (targets.empty() || full_color) && (!depth.base || !stencil_mask || stencil_mask==255)){
        VkClearColorValue value{};if(integer)value.uint32[0]=clear_word;else std::copy(color.begin(),color.end(),value.float32);
        for(unsigned i=0;i<fixed.colors;++i)session.vk.graphics_clear_image(*session.batch,images[i],value,0,0,VK_IMAGE_ASPECT_COLOR_BIT);
        if(zi){const VkImageAspectFlags aspects=(mask&1?VK_IMAGE_ASPECT_DEPTH_BIT:0)|(stencil_mask?VK_IMAGE_ASPECT_STENCIL_BIT:0);
            session.vk.graphics_clear_image(*session.batch,zi,value,z,r(0x1d8c)&255,aspects);}
        if(fallback_stats().enabled)++fallback_stats().paths["clear_transfer"];
    }else{
        static const auto vs=std::make_shared<const std::string>("#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}");
        std::string fs="#version 450\nlayout(std430,binding=0) readonly buffer Data{uint w[];};\nlayout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane;layout(offset=56) vec2 raster_scale_;};\n";
        std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> inputs{};
        for(unsigned i=0;i<fixed.colors;++i){const auto n=std::to_string(i);fs+="layout(location="+n+") out "+(integer?"uint":"vec4")+" c"+n+";\n";
            if(integer && !full_color){inputs[16+i]=session.surface_image(VK_FORMAT_R32_UINT,width,height,samples);session.vk.graphics_copy(*session.batch,images[i],inputs[16+i]);
                fs+="layout(binding="+std::to_string(17+i)+") uniform usampler2DArray old"+n+";\n";}}
        fs+="void main(){";
        for(unsigned i=0;i<fixed.colors;++i){const auto n=std::to_string(i);fs+="c"+n+"=";
            if(integer){if(!full_color)fs+="(texelFetch(old"+n+",ivec3(ivec2(gl_FragCoord.xy),plane),0).r&~w[5])|";fs+="(w[4]&w[5]);";}
            else fs+="uintBitsToFloat(uvec4(w[0],w[1],w[2],w[3]));";}
        if(depth.base && (mask&1))fs+="gl_FragDepth=uintBitsToFloat(w[6]);";
        fs+="}\n";
        static thread_local SourceCache<std::string> shaders;
        auto owned=shaders.find(fs);if(!owned)owned=shaders.put(fs,fs);
        std::vector<unsigned> data(16,0);for(unsigned i=0;i<4;++i)data[i]=float_bits(color[i]);data[4]=clear_word;data[5]=color_bits;data[6]=float_bits(z);
        const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{.5f,.5f},{.5f,.5f},{.5f,.5f}}};
        session.vk.graphics_draw(*session.batch,fixed,images,zi,*vs,*owned,data,3,positions,0,0,0,1,inputs,0,UINT64_MAX,{},vs,owned);
        if(fallback_stats().enabled)++fallback_stats().paths["clear_masked_gpu"];
    }
    for(auto& surface:session.surfaces){for(unsigned i=0;i<fixed.colors;++i)if(surface.image==images[i])session.dirty_surface(surface);if(surface.image==zi)session.dirty_surface(surface);}
    ++state.rsx.rendered_clears;return true;
}
} // namespace rsxgraphics
namespace rsxdraw {
template<class State> static std::shared_ptr<NativeVulkan::GraphicsImage> rsx_graphics_texture(State& s,unsigned offset,unsigned location,unsigned width,unsigned height,unsigned pitch,unsigned format,bool swizzled,bool validated_metadata){
    if(!s.rsx.graphics_session)return {};
    auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_session);
    if(!session.allow_resident_textures)return {};
    const unsigned expected=format==0x85?8:format==0x84?3:format==0x9a?11:format==0x9b?12:0;
    if(!expected)return {};
    auto candidate=[&](const auto& surface){const auto& t=surface.target;
        if(surface.partial || surface.depth || surface.samples!=1 || surface.width!=width || surface.height!=height || surface.format!=expected ||
           surface.image->format!=rsxgraphics::color_format(expected) ||
           bool(t.swizzle_width)!=swizzled || (!swizzled && t.pitch!=pitch))return false;
        if(swizzled && (t.swizzle_width!=texture_pot(width) || t.swizzle_height!=texture_pot(height)))return false;
        return !t.tile && t.offset==t.base_offset;};
    // The host identity of the first byte is needed only to compare it with a surface that passes every other test. A
    // validated_metadata caller (Session::texture_metadata) holds this texture's plan decoded under the validated texture
    // context (same maps, IO table and rights), so the translation below cannot fault there, and with observers paused it
    // touches no guest-visible state. Other callers keep the translation (and its original error) first.
    if(validated_metadata && std::none_of(session.surfaces.begin(),session.surfaces.end(),candidate))return {};
    GuestMemory::ObserverPause acquire(s.memory);
    const auto address=location_address(s.rsx,location,offset,1);const auto mapping=s.memory.range(address,1,false);const auto* data=range_identity(mapping,0,1);
    for(const auto& surface:session.surfaces){const auto& t=surface.target;
        if(!candidate(surface) || t.base!=data)continue;
        // Inactive render targets can be sampled directly. A same-draw
        // framebuffer feedback is still rejected/snapshotted by prepared().
        return surface.image;
    }
    return {};
}
}
#endif
'''
