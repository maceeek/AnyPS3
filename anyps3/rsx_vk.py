"""Opt-in RSX compute renderer. Embed CPP after rsx_draw.CPP.

Unsupported state is rejected before dispatch/guest writes, for CPU fallback.
The CPU's fetch, clip, texture decode and surface codecs remain authoritative.
See batch/rsxvk-result.md for the integration patch and measured limitations.
"""
from .rsx_surface_cache import CPP as SURFACE_CPP
from .rsx_vk_shader import COMMON, RASTER
from .rsx_vk_vertex import CPP as VERTEX_CPP

CPP = r'''
#ifdef ANYPS3_RSX_VK
#include <sstream>
namespace rsxvk {
using namespace rsxdraw;
static std::string u(unsigned x){return std::to_string(x)+"u";}
static std::string f(float x){return "uintBitsToFloat("+u(float_bits(x))+")";}
static std::string literal(const Vec4& v){return "vec4("+f(v[0])+","+f(v[1])+","+f(v[2])+","+f(v[3])+")";}
struct FragmentFailure {unsigned instruction=UINT_MAX;std::string reason;};
static std::optional<std::string> fragment_flow(const std::vector<FpInst>& code,bool dynamic_constants,FragmentFailure* failure);
static bool quad_flow(const std::vector<FpInst>& code){return std::any_of(code.begin(),code.end(),[](const FpInst& i){const unsigned test=(i.s0>>18)&7;return (i.s1>>31) && test!=0 && test!=7;});}
static std::optional<std::string> fragment(const std::vector<FpInst>& code,bool dynamic_constants=false,bool masked=false,FragmentFailure* failure=nullptr){
    if(std::any_of(code.begin(),code.end(),[](const FpInst& i){return (i.s1>>31)!=0;}))return fragment_flow(code,dynamic_constants,failure);
    std::string text=masked?"void shade(inout C q[4],uint lane_mask) {\n":"void shade(inout C q[4]) {\n";
    bool end=false;
    for(unsigned instruction=0;instruction<code.size();++instruction){
        const auto& i=code[instruction];
        auto reject=[&](const char* why)->std::optional<std::string>{if(failure)*failure={instruction,why};return {};};
        const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6);
        if(op>=64 || op==51 || op==52)return reject("opcode");
        if(op==0 || op==61 || op==62){if(i.d&1){end=true;break;}continue;}
        const unsigned exec=(i.s0>>18)&7,scale=(i.s1>>28)&7;
        if(!exec){if(i.d&1){end=true;break;}continue;}
        if(scale==4 && op!=18)return reject("scale");
        auto src=[&](unsigned n)->std::optional<std::string>{
            const unsigned raw=n==0?i.s0:n==1?i.s1:i.s2,type=raw&3,index=(raw>>2)&63,prec=(i.s1>>(19+3*n))&7;
            const bool half=raw&256;
            if(prec>5 || type==3 || (type==0 && index>=(half?64u:48u)) || (type==1 && (((i.d>>13)&15)>14 || (i.s2&(1u<<30)))))return {};
            std::string v=type==0?"rd(q[l],"+u(index)+","+(half?"true":"false")+")":
                type==1?"q[l].input_["+std::to_string((i.d>>13)&15)+"]":dynamic_constants?"load4(w[14]+"+u(instruction*4)+")":literal(i.constant);
            v="("+v+").";
            for(unsigned c=0;c<4;++c)v+="xyzw"[(raw>>(9+2*c))&3];
            auto precision=[&](std::string x){
                if(!prec || prec==5 || (prec==1 && half && type==0))return x;
                return "cpu_clamp("+x+",vec4("+f(prec==4?0.f:prec==1?-65504.f:prec==2?-2.f:-1.f)+"),vec4("+f(prec==1?65504.f:prec==2?2.f:1.f)+"))";
            };
            if(prec==4)v=precision(v);
            if(raw&(1u<<(n==0?29:18)))v="abs("+v+")";
            if(prec!=4)v=precision(v);
            if(raw&(1u<<17))v="(-"+v+")";
            return v;
        };
        const bool binary=op==2 || op==3 || op==4 || (op>=5 && op<=15) || op==25 || op==31 || op==38 || op==43 || op==46 || op==54 || op==56 || op==58 || op==59;
        const bool ternary=op==4 || op==25 || op==31 || op==43 || op==46;
        auto a=op==18 || op==32 || op==33?std::optional<std::string>{"vec4(0)"}:src(0);
        auto b=binary?src(1):std::optional<std::string>{"vec4(0)"},c=ternary?src(2):std::optional<std::string>{"vec4(0)"};if(!a || !b || !c)return reject("source_type_index_precision");
        const bool half=i.d&128,no_dest=i.d&(1u<<30);const unsigned dst=(i.d>>1)&63;
        if(op!=18 && !no_dest && dst>=(half?64u:48u))return reject("destination_index");
        std::string expr;
        switch(op){
        case 1:expr="a";break;case 2:expr="a*b";break;case 3:expr="a+b";break;case 4:expr="fma(a,b,c)";break;
        case 5:expr="vec4(a.x*b.x+a.y*b.y+a.z*b.z)";break;
        case 6:expr="vec4(a.x*b.x+a.y*b.y+a.z*b.z+a.w*b.w)";break;
        case 7:expr="vec4(1,a.y*b.y,a.z,b.w)";break;
        case 8:expr="cpu_min(a,b)";break;case 9:expr="cpu_max(a,b)";break;
        case 10:expr="vec4(lessThan(a,b))";break;case 11:expr="vec4(greaterThanEqual(a,b))";break;
        case 12:expr="vec4(lessThanEqual(a,b))";break;case 13:expr="vec4(greaterThan(a,b))";break;
        case 14:expr="vec4(notEqual(a,b))";break;case 15:expr="vec4(equal(a,b))";break;
        case 16:expr="a-floor(a)";break;case 17:expr="floor(a)";break;case 18:expr="vec4(0)";break;
        case 19:expr="vec4(uintBitsToFloat(pack_s8(a)))";break;
        case 20:expr="unpackSnorm4x8(floatBitsToUint(a.x))";break;
        case 21:case 22:expr="aa["+std::to_string(op==21?1:2)+"]-aa[0]";break;
        case 23:case 24:case 25:case 47:case 49:expr="textureSample("+u((i.d>>17)&15)+",aa[l],lod,killed)";break;
        case 26:expr="vec4(1.0/a.x)";break;case 27:expr="vec4(1.0/sqrt(abs(a.x)))";break;
        case 28:expr="vec4(exp2(a.x))";break;case 29:expr="vec4(log2(a.x))";break;
        case 30:expr="vec4(1,max(0.0,a.x),a.x>0?pow(max(1.e-10,a.y),a.w):0,1)";break;
        case 31:expr="c+a*(b-c)";break;case 32:expr="vec4(1)";break;case 33:expr="vec4(0)";break;
        case 34:expr="vec4(cos(a.x))";break;case 35:expr="vec4(sin(a.x))";break;
        case 36:expr="vec4(uintBitsToFloat(packHalf2x16(a.xy)))";break;
        case 37:expr="unpackHalf2x16(floatBitsToUint(a.x)).xyxy";break;
        case 38:expr="vec4(pow(a.x,b.x))";break;
        case 39:expr="vec4(uintBitsToFloat(pack_u8(a)))";break;
        case 40:expr="unpackUnorm4x8(floatBitsToUint(a.x))";break;
        case 41:expr="vec4(uintBitsToFloat(pack_u16(a.xy)))";break;
        case 42:expr="unpackUnorm2x16(floatBitsToUint(a.x)).xyxy";break;
        case 43:expr="vec2(a.x+b.x*c.x+b.y*c.y,a.y+b.x*c.z+b.y*c.w).xyxy";break;
        case 44:expr="vec4(uintBitsToFloat(pack_u8(mix(1.055*pow(max(a,vec4(0)),vec4(1.0/2.4))-0.055,12.92*a,lessThan(a,vec4(.0031308))))))";break;
        case 45:expr="mix(pow((unpackUnorm4x8(floatBitsToUint(a.x))+.055)/1.055,vec4(2.4)),unpackUnorm4x8(floatBitsToUint(a.x))/12.92,lessThanEqual(unpackUnorm4x8(floatBitsToUint(a.x)),vec4(.04045)))";break;
        case 46:expr="vec4(a.x*b.x+a.y*b.y+c.x)";break;
        case 54:expr="a-2.0*(a.x*b.x+a.y*b.y+a.z*b.z+a.w*b.w)*b";break;
        case 56:expr="vec4(a.x*b.x+a.y*b.y)";break;
        case 57:expr="(a.xyz/sqrt(a.x*a.x+a.y*a.y+a.z*a.z)).xyzz";break;
        case 58:expr="a/b.x";break;
        case 59:expr="vec4(a.x==0?a.x:a.x/sqrt(b.x),a.y==0?a.y:a.y/sqrt(b.x),a.z==0?a.z:a.z/sqrt(b.x),a.w==0?a.w:a.w/sqrt(b.x))";break;
        case 60:expr="vec4(1,a.y,a.y>0?exp2(a.w):0,1)";break;
        default:return reject("opcode");
        }
        text+="{ vec4 aa[4]; for(uint l=0u;l<4u;++l) aa[l]="+*a+";\n";
        if(op==24)text+="for(uint l=0u;l<4u;++l) aa[l].xyz/=aa[l].w;\n";
        text+="for(uint l=0u;l<4u;++l) { precise vec4 a=aa[l], b="+*b+", c="+*c+"; bool killed=false;\n";
        if(op==23 || op==24 || op==25 || op==47 || op==49){
            const auto unit=u((i.d>>17)&15);
            if(op==47)text+="float lod=a.w;\n";
            else if(op==25)text+="float rho=max(gradient("+unit+",b),gradient("+unit+",c));float lod=rho>0?log2(rho):uintBitsToFloat(0xff800000u);\n";
            else text+="float rho=max(gradient("+unit+",aa[1]-aa[0]),gradient("+unit+",aa[2]-aa[0]));float lod=(rho>0?log2(rho):uintBitsToFloat(0xff800000u))"+std::string(op==49?"+a.w":"")+";\n";
        }
        text+="precise vec4 v="+expr+";\n";
        // IEEE infinities/NaNs and packed bit containers are valid CPU model
        // values. Later precision/output clamps apply in the original order.
        if(masked)text+="if((lane_mask&(1u<<l))==0u)continue;\n";
        if((op==23 || op==24 || op==25 || op==47 || op==49) && (i.d&(1u<<21)))text+="v=2.0*v-1.0;\n";
        text+="bvec4 pass=bvec4(true);\n";
        if(exec!=7){
            text+="vec4 cc=q[l].cc["+std::to_string(i.s0>>31)+"].";
            for(unsigned n=0;n<4;++n)text+="xyzw"[(i.s0>>(21+2*n))&3];
            text+=";for(uint n=0u;n<4u;++n)pass[n]="+u(exec)+"==7u || (cc[n]<0 && ("+u(exec)+"&1u)!=0u) || (cc[n]==0 && ("+u(exec)+"&2u)!=0u) || (cc[n]>0 && ("+u(exec)+"&4u)!=0u);\n";
        }
        if(op==18)text+="if(any(pass))q[l].killed=true;";
        else{
            text+="if(killed && any(pass))q[l].killed=true;\n";
            static const float scales[]={1,2,4,8,1,.5f,.25f,.125f};
            if(scale)text+="v*="+f(scales[scale])+";\n";
            if(i.d>>31)text+="v=cpu_clamp(v,vec4(0),vec4(1));\n";
            else if(!no_dest){
                const bool exempt=op==57||op==9||op==8||op==34||op==35||op==54||op==16||op==30||op==60||op==29||
                    (op==1 && half && (i.s0&256) && (i.s0&3)==0);
                const unsigned prec=(i.d>>22)&3;
                if(!exempt && !(prec==1 && !half) && prec)text+="v=cpu_clamp(v,vec4("+f(prec==1?-65504.f:prec==2?-2.f:-1.f)+"),vec4("+f(prec==1?65504.f:prec==2?2.f:1.f)+"));\n";
            }
            text+="uint mask="+u((i.d>>9)&15)+";for(uint n=0u;n<4u;++n)if(!pass[n])mask&=~(1u<<n);\n";
            if(!no_dest)text+="wr(q[l],"+u(dst)+","+(half?"true":"false")+",mask,v);\n";
            if(i.d&256)text+="vec4 stored="+(no_dest?"v":"rd(q[l],"+u(dst)+","+(half?"true":"false")+")")+";for(uint n=0u;n<4u;++n)if((mask&(1u<<n))!=0u)q[l].cc["+std::to_string((i.s0>>30)&1)+"][n]=stored[n];\n";
        }
        text+="}}\n";
        if(i.d&1){end=true;break;}
    }
    if(!end && !code.empty()){if(failure)*failure={unsigned(code.size()-1),"missing_end"};return {};}
    return text+"}\n";
}
// Preserve the CPU interpreter's structured IF/LOOP/REP/BRK/RET execution.
// Each helper computes all quad lanes but commits only the active mask.
static std::optional<std::string> fragment_flow(const std::vector<FpInst>& code,bool dynamic_constants,FragmentFailure* failure){
    const bool uniform=!quad_flow(code);
    std::map<unsigned,unsigned> offsets;unsigned words=0;
    for(unsigned n=0;n<code.size();++n){const auto& i=code[n];const auto at=i.word_offset==UINT32_MAX?words:i.word_offset;
        if(!offsets.emplace(at,n).second){if(failure)*failure={n,"duplicate_instruction_offset"};return {};}
        words=at+4+(i.has_constant?4:0);}
    offsets.emplace(words,unsigned(code.size()));
    auto target=[&](unsigned word)->unsigned{const auto it=offsets.find(word);return it==offsets.end()?UINT_MAX:it->second;};
    std::string helpers,body="void shade(inout C q[4]){uint returned=0u,budget=1048576u,live0=15u,broken0=0u;\n";
    unsigned serial=0;bool valid=true;
    auto emit=[&](auto&& self,unsigned begin,unsigned end,const std::string& live,const std::string& broken,unsigned depth)->void{
        if(depth>32){valid=false;return;}
        for(unsigned pc=begin;pc<end && valid;++pc){const auto& i=code[pc];const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6),id=++serial;
            if(failure)*failure={pc,"flow_range_or_opcode"};
            const auto suffix=std::to_string(id),active="active"+suffix,taken="taken"+suffix;
            body+="{uint "+active+"="+live+"&~(returned|"+broken+");if("+active+"!=0u){if(budget==0u){atomicOr(w[7],2u);return;}--budget;\n";
            if(op>=64){
                body+="uint "+taken+"=0u;for(uint l=0u;l<4u;++l)if(("+active+"&(1u<<l))!=0u){vec4 cc=q[l].cc["+u(i.s0>>31)+"].";
                for(unsigned c=0;c<4;++c)body+="xyzw"[(i.s0>>(21+2*c))&3];
                const auto test=u((i.s0>>18)&7);
                body+=";for(uint c=0u;c<4u;++c)if("+test+"==7u || (cc[c]<0 && ("+test+"&1u)!=0u) || (cc[c]==0 && ("+test+"&2u)!=0u) || (cc[c]>0 && ("+test+"&4u)!=0u))"+taken+"|=1u<<l;}\n";
                if(op==64){if(!depth){valid=false;return;}body+=broken+"|="+taken+";\n";}
                else if(op==66){const unsigned other=target(i.s1&0x7fffffff),after=target(i.s2);
                    if(other<pc+1 || after<other || after>end){valid=false;return;}
                    self(self,pc+1,other,taken,broken,depth);
                    const auto opposite="other"+suffix;body+="uint "+opposite+"="+active+"&~"+taken+";\n";
                    self(self,other,after,opposite,broken,depth);pc=after-1;
                }else if(op==67 || op==68){const unsigned after=target(i.s2),count=(i.s1>>2)&255;
                    if(after<pc+1 || after>end){valid=false;return;}
                    const auto stopped="broken"+suffix,lanes="live"+suffix;
                    body+="uint "+stopped+"=0u,"+lanes+"="+taken+";for(uint iteration"+suffix+"=0u;iteration"+suffix+"<"+u(count)+" && ("+lanes+"&~returned)!=0u;++iteration"+suffix+"){\n";
                    self(self,pc+1,after,lanes,stopped,depth+1);
                    body+=lanes+"&=~"+stopped+";}\n";pc=after-1;
                }else if(op==69)body+="returned|="+taken+";\n";
                else {valid=false;return;} // CAL/unknown: original CPU emits explicit error.
            }else if(op!=61 && op!=62){
                // CPU rejects derivatives/implicit LOD in divergent flow. Do
                // not replace that error with undefined hardware derivatives.
                if(!uniform && (op==21 || op==22 || op==23 || op==24 || op==49))body+="if("+active+"!=15u){atomicOr(w[7],4u);return;}\n";
                auto single=i;single.d|=1;FragmentFailure nested;auto shader=fragment({single},dynamic_constants,true,&nested);
                if(!shader){if(failure)*failure={pc,nested.reason};valid=false;return;}
                const std::string from="void shade(",to="void step"+suffix+"(";
                shader->replace(shader->find(from),from.size(),to);
                if(dynamic_constants){const std::string from="load4(w[14]+0u)";auto at=shader->find(from);
                    if(at!=std::string::npos)shader->replace(at,from.size(),"load4(w[14]+"+u(pc*4)+")");}
                helpers+=*shader;body+="step"+suffix+"(q,"+active+");\n";
            }
            if(i.d&1)body+="returned|="+active+";\n";
            body+="}}\n";
        }
    };
    emit(emit,0,unsigned(code.size()),"live0","broken0",0);
    if(!valid)return {};
    return helpers+body+"}\n";
}
''' + '\nstatic const char* common_glsl=R"RSXVK(' + COMMON + ')RSXVK";\n' + r'''
static std::string preamble(){return "#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer Data {uint w[];};\n"+std::string(common_glsl);}
''' + '\nstatic const char* raster_glsl=R"RSXVK(' + RASTER + ')RSXVK";\n' + r'''
// Header 0..15; resolved fixed registers 16..2063; texture descriptors
// 2064..2575 (32 words/unit); geometry starts at 2576.
static void append(std::vector<unsigned>& data,const Vec4& v){for(float x:v)data.push_back(float_bits(x));}
static bool supported(const NativeRsx& gpu,const std::vector<FpInst>& fp,const std::array<Texture,16>& textures){
    const Reg r{gpu};
    if(!fragment(fp,true))return false;
    // These modes require additional shader/surface work. Nothing is dispatched.
    if(!(r(0x208)&31) || (r(0x208)&31)>16 || (r(0x208)&31)==6 || (r(0x208)&31)==7 ||
       (r(0x220)!=1 && r(0x220)!=2 && r(0x220)!=0x13 && r(0x220)!=0x17 && r(0x220)!=0x1f) || r(0x142c) ||
       r(0x147c) || r(0xa68) || r(0x1fec) || r(0x8cc) || r(0x2b8) || r(0x2bc))return false;
    if(float_color(r(0x208)&31) && (r(0x374)&1))return false;
    try{const Stencil front(r,false,((r(0x208)>>5)&7)!=1),back(r,true,((r(0x208)>>5)&7)!=1);(void)front;(void)back;
        if(r(0x374))(void)logic_op(r.get(0x378,0x1503),0,0);
    }catch(const std::runtime_error&){return false;}
    for(unsigned i=0;i<8;++i)if(gpu.registers.count(0x2c0+i*8) || gpu.registers.count(0x2c4+i*8))return false;
    for(unsigned i=0;i<10;++i)if(r(0xb40+4*i))return false;
    if(gpu.draw.primitive<5 || gpu.draw.primitive>10)return false;
    for(const auto& i:fp){const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6);
        if((op==23 || op==24 || op==25 || op==47 || op==49) && !textures[(i.d>>17)&15].valid)return false;}
    for(const auto& t:textures)if(t.valid){
        // CPU decodes every supported format/swizzle into float texels. GPU
        // filtering/remap operates in the same order as the reference sampler.
        if(t.cube || t.dimension!=2 || t.shadow || t.levels.empty() || t.mip_count!=t.levels.size() ||
           !t.mip_count || t.mip_count>13 || t.min<1 || t.min>6 || (t.mag!=1 && t.mag!=2))return false;
        auto wrap=[](unsigned v){return (v>=1 && v<=6)||v==8;};
        if(!wrap(t.wrap_s)||!wrap(t.wrap_t))return false;
    }
    return true;
}
''' + SURFACE_CPP + r'''

// Explicit test/embedding scope. Production draw hooks never create one until
// all guest access paths have coherence guards. Destruction cannot commit
// guest data or hide failure; callers MUST finish()/flush() explicitly.
struct ResidentSession {
    NativeVulkan& vk;PpuState& state;
    std::unique_ptr<NativeVulkan::ResidentArena> arena;
    SurfaceCache cache;
    struct Pending {unsigned base,count,pixels;bool report;};
    std::vector<Pending> pending;
    bool failed=false;
    unsigned snapshot_texture(unsigned source,unsigned width,unsigned height,unsigned format);
    explicit ResidentSession(NativeVulkan& v,PpuState& s,unsigned words=16*1024*1024):vk(v),state(s),arena(v.resident_arena(words)),cache(v,*arena){cache.guest=&s.memory;cache.before_readback=[this]{finish();};}
    void finish(){
        if(failed)throw std::runtime_error("RSX resident session is poisoned");
        for(const auto& p:pending){vk.resident_download(*arena,p.base+7,1);vk.resident_download(*arena,p.count,p.pixels);}
        cache.finish();
        for(const auto& p:pending)if(vk.resident_read(*arena,p.base+7,1)[0]){
            failed=true;throw std::runtime_error("RSX resident non-finite GPU result; no guest commit");}
        for(const auto& p:pending){std::uint64_t count=0;for(unsigned n:vk.resident_read(*arena,p.count,p.pixels))count+=n;
            state.rsx.draw.pixels+=count;if(p.report)state.rsx.report_values[0]+=count;}
        for(const auto& p:pending)vk.resident_free(*arena,p.base);
        pending.clear();
    }
    void flush(){finish();cache.flush();}
    void host_access(const void* address,std::uint64_t size,bool write){finish();cache.host_access(address,size,write);}
};
static std::string resident_source(std::string text){
    const std::string entry="void main(){\n    uint width=";
    const auto entry_at=text.find(entry);
    if(entry_at!=std::string::npos){
        text.insert(entry_at,"shared uint resident_counts[64];\n");
        const std::string exit="if(id>=qw*((height+1u)/2u))return;";
        const auto exit_at=text.find(exit);if(exit_at==std::string::npos)throw std::runtime_error("resident raster guard missing");
        text.replace(exit_at,exit.size(),"uint resident_count=0u;if(id<qw*((height+1u)/2u)){");
        const std::string increment="++w[w[6]+y*width+x];";
        const auto increment_at=text.find(increment);if(increment_at==std::string::npos)throw std::runtime_error("resident raster count missing");
        text.replace(increment_at,increment.size(),"++resident_count;");
        const auto end=text.find_last_of('}');
        text.insert(end,"}resident_counts[gl_LocalInvocationID.x]=resident_count;barrier();if(gl_LocalInvocationID.x==0u){uint sum=0u;for(uint i=0u;i<64u;++i)sum+=resident_counts[i];w[w[6]+gl_WorkGroupID.x]=sum;}\n");
    }
    const std::string declaration="layout(std430,binding=0) buffer Data {uint w[];};";
    const auto at=text.find(declaration);if(at==std::string::npos)throw std::runtime_error("RSX resident shader declaration missing");
    text.insert(at+declaration.size(),"\nlayout(push_constant) uniform DrawBase {uint draw_base;};\n");
    const std::string resource="uint texture_offset(uint p){return p;}";
    text.replace(text.find(resource),resource.size(),"uint texture_offset(uint p){return p-draw_base;}");
    std::size_t p=0;while((p=text.find("w[",p))!=std::string::npos){
        // Leave the unsized buffer array declaration intact.
        if(text[p+2]==']'){p+=2;continue;}
        text.insert(p+2,"draw_base+");p+=12;
    }
    return text;
}

unsigned ResidentSession::snapshot_texture(unsigned source,unsigned width,unsigned height,unsigned format){
    // Validate deferred shader status without reading surface bytes to the CPU.
    finish();
    if(!width || !height || format==6 || format==7 || !format || format>16)throw std::runtime_error("RSX resident snapshot format/size");
    const auto pixels64=std::uint64_t(width)*height;
    if(pixels64>(UINT_MAX-3)/4)throw std::runtime_error("RSX resident snapshot size overflow");
    const unsigned pixels=pixels64;
    const auto descriptor=vk.resident_allocate(*arena,3+pixels*4);
    cache.upload(descriptor,{descriptor+3,width,height});
    const auto params=vk.resident_allocate(*arena,4);
    cache.upload(params,{pixels,source-params,format,descriptor+3-params});
    const std::string shader=preamble()+R"GLSL(
void main(){uint i=gl_GlobalInvocationID.x;if(i>=w[0])return;
 vec4 c=colorRead(w[1]+i*colorStride(w[2]),w[2]);uint to=w[3]+4u*i;
 for(uint j=0u;j<4u;++j)w[to+j]=floatBitsToUint(c[j]);}
)GLSL";
    vk.resident_dispatch(*arena,resident_source(shader),params,(pixels+63)/64);
    // Descriptor/pixel allocation is an immutable GPU snapshot through this
    // session. Its transient conversion parameters can be reclaimed now.
    cache.finish();vk.resident_free(*arena,params);return descriptor;
}
// triangles are already clipped/cull-tested by the reference preparation.
// Vertex.clip is replaced by (window x,y,z,1/w); .o remains homogeneous varyings.
static bool draw(NativeVulkan& vk,PpuState& s,const std::vector<std::array<ShadedVertex,3>>& triangles,
                 const std::vector<FpInst>& fp,const std::array<Texture,16>& textures,
                 Target color,Target depth,unsigned width,unsigned height,unsigned x0,unsigned y0,unsigned x1,unsigned y1,
                 std::uint64_t& shaded,const std::array<Target,4>* mrt=nullptr,ResidentSession* resident=nullptr){
    auto& gpu=s.rsx;const Reg r{gpu};
    const unsigned aa=(r(0x208)>>12)&15,nx=aa?2:1,ny=aa>=4?2:1;
    const unsigned sw=width*nx,sh=height*ny;
    const unsigned count=r(0x220)==0x13?2:r(0x220)==0x17?3:r(0x220)==0x1f?4:1;
    std::array<Target,4> colors{};if(mrt)colors=*mrt;else colors[0]=color;
    if(count>1 && !mrt)return false;
    const unsigned cf=r(0x208)&31,cb=color_bytes(cf),stride=(cb+3)/4;
    for(unsigned i=0;i<count;++i){
        if(!colors[i].base || !parallel_target(colors[i],sw,sh) || target_overlaps(colors[i],depth,sw,sh))return false;
        for(unsigned j=0;j<i;++j)if(target_overlaps(colors[i],colors[j],sw,sh))return false;
        for(const auto& t:textures)if(t.valid && target_overlaps(colors[i],t.storage,t.storage_size,sw,sh))return false;
    }
    if(!supported(gpu,fp,textures) || !parallel_target(color,sw,sh) || !parallel_target(depth,sw,sh) ||
       target_overlaps(color,depth,sw,sh))return false;
    for(const auto& t:textures)if(t.valid && (target_overlaps(color,t.storage,t.storage_size,sw,sh) || target_overlaps(depth,t.storage,t.storage_size,sw,sh)))return false;
    for(const auto& tri:triangles)for(const auto& v:tri){
        for(float c:v.clip)if(!std::isfinite(c))return false;
        for(const auto& o:v.o)for(float c:o)if(!std::isfinite(c))return false;
        if(!(v.clip[3]>0) || std::fabs(v.clip[0])>1048576.f || std::fabs(v.clip[1])>1048576.f)return false;
    }
    if(aa!=0 && aa!=3 && aa!=4 && aa!=5)return false;
    if(!width || !height || width>4096 || height>4096 || x0>x1 || y0>y1 || x1>width || y1>height)return false;
    if(!parallel_fixed(r(0xa74)&1,r.get(0xa6c,0x207),r(0x304)&1,r.get(0x308,0x207),!(r(0x374)&1) && (r(0x310)&1),r(0x314),r(0x318),r(0x320)))return false;
    std::vector<unsigned> data(2576,0);
    data[0]=width;data[1]=height;data[2]=triangles.size();data[3]=data.size();
    data[8]=x0;data[9]=y0;data[10]=x1;data[11]=y1;
    for(const auto& pair:gpu.registers)if(pair.first<0x2000)data[16+pair.first/4]=pair.second;
    // Resolve the same reset/default values as the reference draw.
    for(const auto& pair:std::vector<std::pair<unsigned,unsigned>>{{0xa6c,0x207},{0x308,0x207},{0x324,0x01010101},{0x394,0},{0x398,0x3f800000},{0x368,0x1d01},{0x1834,0x901},{0x378,0x1503},{0x388,0x3f800000}})
        data[16+pair.first/4]=r.get(pair.first,pair.second);
    for(const auto& tri:triangles)for(const auto& v:tri){append(data,v.clip);for(const auto& o:v.o)append(data,o);}
    std::optional<SurfaceCache::BuildScope> build_scope;if(resident)build_scope.emplace(resident->cache);
    std::array<unsigned,4> resident_colors{};
    unsigned resident_depth=0;
    if(resident){
        if(&resident->vk!=&vk || &resident->state!=&s || resident->failed)throw std::runtime_error("RSX resident session mismatch/failure");
        for(unsigned i=0;i<count;++i)resident_colors[i]=resident->cache.bind(colors[i],sw,sh,cb).offset;
        if(depth.base)resident_depth=resident->cache.bind(depth,sw,sh,depth.bpp).offset;
        else resident_depth=resident->cache.zero_surface(sw*sh);
    }
    data[15]=count;
    if(!resident)for(unsigned i=0;i<count;++i){data[i?11+i:4]=data.size();
        for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw;++x){const auto* p=colors[i].at(x,y);
            for(unsigned word=0;word<stride;++word)data.push_back(read_be(p+4*word,std::min(4u,cb)));}
    }
    data[5]=data.size();const unsigned df=(r(0x208)>>5)&7;
    if(depth.base && df!=1 && df!=2)return false;
    if(!resident)for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw;++x)data.push_back(depth.base?read_be(depth.at(x,y),df==1?2:4):0);
    const unsigned count_words=resident?(((width+1)/2*((height+1)/2)+63)/64):width*height;
    data[6]=data.size();data.resize(data.size()+count_words,0);
    for(unsigned unit=0;unit<16;++unit){const auto& t=textures[unit];if(!t.valid)continue;
        const unsigned desc=2064+unit*32;
        data[desc]=data.size();data[desc+1]=t.mip_count;data[desc+2]=t.unnormalized;data[desc+3]=t.wrap_s;data[desc+4]=t.wrap_t;
        data[desc+5]=t.min;data[desc+6]=t.mag;data[desc+7]=float_bits(t.bias);data[desc+8]=float_bits(t.min_lod);data[desc+9]=float_bits(t.max_lod);
        data[desc+10]=t.gamma;data[desc+11]=t.features;data[desc+12]=t.signed_mask;data[desc+13]=t.biased;data[desc+14]=t.alpha_kill;
        for(unsigned c=0;c<4;++c){data[desc+15+c]=t.remap_channel[c];data[desc+19+c]=t.remap_control[c];data[desc+23+c]=float_bits(texture_input(t,t.border,true)[c]);}
        const unsigned levels=data.size();data.resize(data.size()+t.mip_count*3);
        for(unsigned l=0;l<t.mip_count;++l){const auto& level=t.levels[l];data[levels+3*l]=data.size();data[levels+3*l+1]=level.width;data[levels+3*l+2]=level.height;
            for(unsigned y=0;y<level.height;++y)for(unsigned x=0;x<level.width;++x)append(data,texture_input(t,texel(t,level,x,y),false));}
        if(resident){
            std::vector<unsigned> blob(data.begin()+levels,data.end());
            for(unsigned l=0;l<t.mip_count;++l)blob[3*l]-=levels;
            const auto address=resident->cache.texture_levels(blob,t.mip_count);data[desc]=address;data.resize(levels);
        }
    }
    auto shader=fragment(fp,true);if(!shader)return false;
    data[2575]=data.size();for(const auto& inst:fp)append(data,inst.constant);
    const std::string from="load4(w[14]+",to="load4(w[2575]+";
    for(std::size_t pos=0;(pos=shader->find(from,pos))!=std::string::npos;pos+=to.size())shader->replace(pos,from.size(),to);
    const unsigned groups=((width+1)/2*((height+1)/2)+63)/64;
    if(!vk.compute_fits(data.size()*4,groups))return false;
    if(resident){
        const auto base=resident->cache.allocate(data.size());
        data[4]=resident_colors[0]-base;for(unsigned i=1;i<count;++i)data[11+i]=resident_colors[i]-base;
        data[5]=resident_depth-base;
        resident->cache.upload(base,data);
        vk.resident_dispatch(*resident->arena,resident_source(preamble()+*shader+raster_glsl),base,groups);
        for(auto& surface:resident->cache.surfaces){
            for(unsigned i=0;i<count;++i)if(surface.offset==resident_colors[i])surface.dirty=true;
            if(depth.base && surface.offset==resident_depth && ((r(0xa70)&1) || ((r(0x328)&1) && df==2)))surface.dirty=true;
        }
        resident->pending.push_back({base,base+data[6],count_words,r(0x17cc)!=0});shaded=0;return true;
    }
    vk.compute(preamble()+*shader+raster_glsl,data,groups);
    if(data[7])return false; // Non-finite shader result: re-run the CPU reference, no guest writes yet.
    // Decode/encode ARGB words explicitly: guest BE and row padding are retained.
    // All GPU operations have completed before the first guest write.
    for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw;++x){const unsigned at=y*sw+x;
        for(unsigned i=0;i<count;++i){auto& target=colors[i];auto* p=target.at(x,y);target.mark(s.memory,x,y,p);
            for(unsigned word=0;word<stride;++word)write_be(p+4*word,std::min(4u,cb),data[data[i?11+i:4]+at*stride+word]);
            target.commit(x,y);}

        if(depth.base && ((r(0xa70)&1) || ((r(0x328)&1) && df==2))){auto* z=depth.at(x,y);depth.mark(s.memory,x,y,z);write_be(z,df==1?2:4,data[data[5]+at]);depth.commit(x,y);}
    }
    shaded=0;for(unsigned i=0;i<width*height;++i)shaded+=data[data[6]+i];
    return true;
}
static bool vertices(NativeVulkan&,const NativeRsx&,unsigned,const std::vector<std::array<Vec4,16>>&,std::vector<VertexOut>&);
// Called after CPU parallel certification excludes input/output aliases.
template<class Vertex> static bool prepared(NativeVulkan& vk,PpuState& s,
        const std::vector<std::array<unsigned,3>>& indices,Vertex vertex,
        const std::vector<FpInst>& fp,const std::array<Texture,16>& textures,Target color,Target depth,
        unsigned width,unsigned height,unsigned x0,unsigned y0,unsigned x1,unsigned y1,std::uint64_t& shaded,const std::array<Target,4>* mrt=nullptr,ResidentSession* resident=nullptr){
    const Reg r{s.rsx};if(!supported(s.rsx,fp,textures))return false;
    const float sx=bits_float(r(0xa30)),sy=bits_float(r(0xa34)),sz=bits_float(r(0xa38));
    const float vx=bits_float(r(0xa20)),vy=bits_float(r(0xa24)),vz=bits_float(r(0xa28));
    // Certification has already produced the CPU vertices. Batch the same
    // decoded attributes through the GPU VP. Keep CPU coordinates/varyings if
    // hardware arithmetic differs at all, preserving exact coverage semantics.
    std::map<unsigned,ShadedVertex> accelerated;
    std::vector<unsigned> ids;std::vector<std::array<Vec4,16>> inputs;
    for(const auto& t:indices)for(unsigned id:t)if(std::find(ids.begin(),ids.end(),id)==ids.end()){
        ids.push_back(id);std::array<Vec4,16> in{};
        for(unsigned a=0;a<16;++a)in[a]=!(r(0x1ff0)&(1u<<a))?Vec4{0,0,0,1}:
            s.rsx.draw.immediate_vertices.empty()?fetch_attribute(s,r,a,id):register_attribute(s.rsx.draw.immediate_vertices[id][a]);
        inputs.push_back(in);
    }
    std::vector<VertexOut> gpu_vertices;
    if(!resident && vertices(vk,s.rsx,r.get(0x1ea0,0),inputs,gpu_vertices))for(unsigned i=0;i<ids.size();++i){
        const auto& reference=vertex(ids[i]);bool exact=true;
        for(unsigned o=0;o<16;++o)for(unsigned c=0;c<4;++c)exact&=float_bits(gpu_vertices[i].o[o][c])==float_bits(reference.o[o][c]);
        if(exact){ShadedVertex v;v.o=gpu_vertices[i].o;v.clip=v.o[0];accelerated.emplace(ids[i],v);}
    }
    auto selected=[&](unsigned id)->const ShadedVertex&{const auto it=accelerated.find(id);return it==accelerated.end()?vertex(id):it->second;};
    std::vector<std::array<ShadedVertex,3>> ready;
    for(const auto& tri:indices){std::array<const ShadedVertex*,3> v{&selected(tri[0]),&selected(tri[1]),&selected(tri[2])};
        const auto polygon=clip_polygon(v,r(0x1478),r.get(0x1ff4,0x3fffff),sz,vz,bits_float(r.get(0x394,0)),bits_float(r.get(0x398,0x3f800000)));
        for(unsigned fan=1;fan+1<polygon.size();++fan){std::array<ShadedVertex,3> t{polygon[0],polygon[fan],polygon[fan+1]};
            for(auto& p:t){const float iw=1.f/p.clip[3];p.clip={p.clip[0]*iw*sx+vx,p.clip[1]*iw*sy+vy,p.clip[2]*iw*sz+vz,iw};
                if(r.get(0x368,0x1d01)==0x1d00)for(unsigned n=1;n<=4;++n)p.o[n]=v[r(0x1454)?0:2]->o[n];}
            const float area=(t[1].clip[0]-t[0].clip[0])*(t[2].clip[1]-t[0].clip[1])-(t[2].clip[0]-t[0].clip[0])*(t[1].clip[1]-t[0].clip[1]);
            if(area==0)continue;
            const bool front=r.get(0x1834,0x901)==0x901?area<0:!(area<0);const unsigned face=r.get(0x1830,0x405);
            if((r(0x183c)&1) && (face==0x408 || (face==0x404 && front) || (face==0x405 && !front)))continue;
            ready.push_back(t);
        }
    }
    if(!draw(vk,s,ready,fp,textures,color,depth,width,height,x0,y0,x1,y1,shaded,mrt,resident))return false;
    s.rsx.draw.triangles+=ready.size();return true;
}

''' + VERTEX_CPP + r'''
} // namespace rsxvk
#endif
'''
