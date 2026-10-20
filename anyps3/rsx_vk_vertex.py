"""RSX VP ALU, predicates, address registers and checked indexed fetch."""
CPP = r'''
// Reachable instructions of a vertex program from `start`, mirroring run_vertex_program's control flow: after an instruction a taken
// branch (BRI/CAL by the CC predicate, BRB/CLB by a bool constant, RET by the call stack) jumps, otherwise END ends the program,
// otherwise execution continues at pc+1. BRA (address register target) and CLI (no verified semantics) are not translated. A
// reachable unwritten instruction rejects the program (the interpreter stops only if it is executed; the CPU path keeps that).
struct VertexFlow {
    std::array<bool,544> reach{},leader{};
    bool control=false,calls=false,falls_off=false;
    static unsigned immediate(const std::uint32_t* d){return ((d[0]>>23)&1)<<9|((d[2]&63)<<3)|(d[3]>>29);}
};
static bool vertex_program_flow(const NativeRsx& gpu,unsigned start,VertexFlow& flow){
    flow=VertexFlow{};
    if(start>=544)return false;
    std::vector<unsigned> work{start};flow.leader[start]=true;
    while(!work.empty()){
        const unsigned pc=work.back();work.pop_back();
        if(flow.reach[pc])continue;
        for(unsigned c=0;c<4;++c)if(!gpu.transform_program_written[pc*4+c])return false;
        flow.reach[pc]=true;
        const std::uint32_t* d=gpu.transform_program.data()+pc*4;
        const unsigned sca=(d[1]>>27)&31;
        if(sca==8 || sca==11)return false;
        const bool jump=sca==9 || sca==10 || sca==17 || sca==18,call=sca==10 || sca==18;
        if(jump || sca==12)flow.control=true;
        if(call || sca==12)flow.calls=true;
        if(jump){const unsigned target=VertexFlow::immediate(d);if(target<544){flow.leader[target]=true;work.push_back(target);}}
        const bool always=(sca==9 || sca==12) && ((d[0]>>10)&7)==7;   // CC condition TR: pass_ is true, the instruction never falls through
        if((!(d[3]&1) && !always) || call){   // fall-through, or the return point of a call (taken or not)
            if(pc+1<544){if(call)flow.leader[pc+1]=true;work.push_back(pc+1);}
            else if(!(d[3]&1))flow.falls_off=true;
        }
    }
    return true;
}
static std::optional<std::string> vertex_shader(const NativeRsx& gpu,unsigned start,bool dynamic_constants=false,bool checked=false){
    VertexFlow flow;if(!vertex_program_flow(gpu,start,flow))return {};
    // Programs with branches need the checked mode (runtime error flag for the budget, call stack, targets, unwritten constants).
    if(flow.control && !(checked && dynamic_constants))return {};
    if(!flow.control && flow.falls_off)return {};
    std::string body="void main(){uint id=gl_GlobalInvocationID.x;if(id>=w[0])return;vec4 t[32],o[16],input_[16],cc[2];ivec4 address_[2];cc[0]=cc[1]=vec4(0);address_[0]=address_[1]=ivec4(0);for(uint n=0u;n<32u;++n)t[n]=vec4(0);for(uint n=0u;n<16u;++n){o[n]=vec4(0,0,0,1);input_[n]=load4(1u+id*64u+n*4u);}\n";
    // Control flow: one switch case per branch target inside a bounded loop; straight-line code falls through the cases.
    if(flow.control){
        body+="uint pc_="+u(start)+",steps_=0u;bool done_=false;";
        if(flow.calls)body+="uint stack_[16],sp_=0u;";
        body+="for(;;){if(++steps_>65536u){atomicOr(w[7],16u);return;}switch(pc_){\n";
    }
    bool ended=false;
    for(unsigned pc=0;pc<544;++pc){
        if(!flow.reach[pc])continue;
        if(flow.control && flow.leader[pc])body+="case "+u(pc)+":\n";
        std::array<unsigned,4> d{};for(unsigned c=0;c<4;++c){if(!gpu.transform_program_written[pc*4+c])return {};d[c]=gpu.transform_program[pc*4+c];}
        const bool indexed_input=d[0]&(1u<<27),indexed_constant=d[3]&2;
        if((indexed_input || indexed_constant) && !checked)return {};
        const unsigned condition=(d[0]>>10)&7,cc_index=(d[0]>>25)&1;
        std::string condition_value="cc["+std::to_string(cc_index)+"].";
        for(unsigned c=0;c<4;++c)condition_value+="xyzw"[(d[0]>>(8-2*c))&3];
        body+="{vec4 test_="+condition_value+";bvec4 pass_=bvec4(";
        for(unsigned c=0;c<4;++c){if(c)body+=",";
            const auto v="test_["+std::to_string(c)+"]";
            body+=condition==7?"true":"("+std::string(condition&1?v+"<0.0 || ":"")+std::string(condition&2?v+"==0.0 || ":"")+std::string(condition&4?v+">0.0 || ":"")+"false)";}
        body+=");\n";
        const auto address="address_["+std::to_string((d[0]>>24)&1)+"]["+std::to_string(d[0]&3)+"]";
        unsigned raw[3]={((d[2]>>23)&511)|((d[1]&255)<<9),(d[2]>>6)&0x1ffff,((d[3]>>21)&2047)|((d[2]&63)<<11)};
        bool valid=true;
        auto src=[&](unsigned n){
            const auto r=raw[n];const unsigned type=r&3,index=(r>>2)&63;std::string v;
            if(type==1 && index<32)v="t["+std::to_string(index)+"]";
            else if(type==2){
                auto index=std::to_string((d[1]>>8)&15);
                if(indexed_input){index="("+index+"+"+address+")";body+="if("+index+"<0 || "+index+">=16){atomicOr(w[7],16u);return;}\n";}
                v="input_["+index+"]";
            }
            else if(type==3){const unsigned index=(d[1]>>12)&1023;Vec4 c{};
                if(indexed_constant){
                    const auto idx="(("+u(index)+"+uint("+address+"))&511u)";
                    body+="if("+idx+">=468u){atomicOr(w[7],16u);return;}if(w[w[2]+1872u+"+idx+"]!=15u){atomicOr(w[7],16u);return;}\n";
                    v="load4(w[2]+4u*"+idx+")";
                }else{
                    if(index<468){
                        bool all_written=true;for(unsigned j=0;j<4;++j){if(!gpu.transform_constants_written[index*4+j])all_written=false;c[j]=bits_float(gpu.transform_constants[index*4+j]);}
                        // A path that is not executed may reference an unwritten constant: check at run time (the interpreter stops only when it reads one).
                        if(flow.control)body+="if(w[w[2]+1872u+"+u(index)+"]!=15u){atomicOr(w[7],16u);return;}\n";
                        else if(!all_written)valid=false;
                    }
                    v=dynamic_constants?(index<468?"load4(w[2]+"+u(index*4)+")":"vec4(0)"):literal(c);
                }
            }else{valid=false;return std::string("vec4(0)");}
            v="("+v+").";for(unsigned c=0;c<4;++c)v+="xyzw"[(r>>(14-2*c))&3];
            if(d[0]&(1u<<(21+n)))v="abs("+v+")";
            if(r&(1u<<16))v="(-"+v+")";
            return v;
        };
        const unsigned vec=(d[1]>>22)&31,sca=(d[1]>>27)&31,dst=(d[3]>>2)&31;
        auto write=[&](std::string expr,bool scalar){
            const unsigned mask=scalar?(d[3]>>17)&15:(d[3]>>13)&15,tmp=scalar?(d[3]>>7)&63:(d[0]>>15)&63;
            const bool to_output=scalar?!(d[0]&(1u<<30)):bool(d[0]&(1u<<30));
            if(tmp!=63 && tmp>=32){valid=false;return;}
            
            if(to_output && dst!=31 && dst>15){valid=false;return;}
            body+="{precise vec4 v="+expr+";";
            if(d[0]&(1u<<26))body+="v=clamp(v,vec4(0),vec4(1));";
            for(unsigned c=0;c<4;++c)if(mask&(8>>c)){
                if(d[0]&(1u<<13))body+="if(pass_["+u(c)+"]){";
                if(!scalar && ((d[1]>>22)&31)==13){
                    if(tmp>1 || (to_output && dst!=31)){valid=false;return;}
                    body+="if(isnan(v["+u(c)+"]) || isinf(v["+u(c)+"]) || v["+u(c)+"] < -2147483648.0 || v["+u(c)+"] >= 2147483648.0){atomicOr(w[7],16u);return;}address_["+u(tmp)+"]["+u(c)+"]=int(floor(v["+u(c)+"]));";
                }else{
                if(tmp==63 && (!to_output || (dst==31 && (d[0]&((1u<<14)|(1u<<29))))))body+="cc["+u(cc_index)+"]["+u(c)+"]=v["+u(c)+"];";
                if(to_output && dst!=31 && !(scalar && tmp!=63))body+="o["+std::to_string(dst)+"]["+std::to_string(c)+"]=v["+std::to_string(c)+"];";
                if(tmp!=63)body+="t["+std::to_string(tmp)+"]["+std::to_string(c)+"]=v["+std::to_string(c)+"];";
                }
                if(d[0]&(1u<<13))body+="}";
            }
            body+="}\n";
        };
        if(vec){std::string a,b,c,expr;
            // Source reads occur before vector writes; scalar reads follow them.
            a=src(0);
            if(vec==3)b=src(2);else if(vec!=1 && vec!=13 && vec!=14 && vec!=15 && vec!=22)b=src(1);
            if(vec==4)c=src(2);
            body+="{precise vec4 a="+a+",b="+(b.empty()?"vec4(0)":b)+",c="+(c.empty()?"vec4(0)":c)+";";
            switch(vec){case 13:if(!checked)return {};[[fallthrough]];case 1:expr="a";break;case 2:expr="a*b";break;case 3:expr="a+b";break;case 4:expr="fma(a,b,c)";break;
            case 5:expr="vec4(a.x*b.x+a.y*b.y+a.z*b.z)";break;
            case 6:expr="vec4(a.x*b.x+a.y*b.y+a.z*b.z+b.w)";break;
            case 7:expr="vec4(a.x*b.x+a.y*b.y+a.z*b.z+a.w*b.w)";break;
            case 8:expr="vec4(1,a.y*b.y,a.z,b.w)";break;case 9:expr="min(a,b)";break;case 10:expr="max(a,b)";break;
            case 11:expr="vec4(lessThan(a,b))";break;case 12:expr="vec4(greaterThanEqual(a,b))";break;
            case 14:expr="a-floor(a)";break;case 15:expr="floor(a)";break;case 16:expr="vec4(equal(a,b))";break;
            case 17:expr="vec4(0)";break;case 18:expr="vec4(greaterThan(a,b))";break;case 19:expr="vec4(lessThanEqual(a,b))";break;
            case 20:expr="vec4(notEqual(a,b))";break;case 21:expr="vec4(1)";break;case 22:expr="sign(a)";break;
            default:return {};
            }
            write(expr,false);body+="}\n";
        }
        if(sca && !(flow.control && (sca==9 || sca==10 || sca==12 || sca==17 || sca==18))){const auto a=src(2);std::string expr;
            switch(sca){case 1:expr="a";break;case 2:expr="vec4(1)/a";break;
            case 3:expr="uintBitsToFloat((floatBitsToUint(clamp(abs(vec4(1)/a),vec4(5.42101e-20),vec4(1.884467e19)))&uvec4(0x7fffffffu))|(floatBitsToUint(vec4(1)/a)&uvec4(0x80000000u)))";break;
            case 4:expr="vec4(1.0/sqrt(abs(a.x)))";break;case 5:expr="exp(a)";break;case 6:expr="log(a)";break;
            case 13:expr="log2(abs(a))";break;case 14:expr="exp2(a)";break;case 15:expr="sin(a)";break;case 16:expr="cos(a)";break;
            default:return {};
            }
            body+="{precise vec4 a="+a+";";write(expr,true);body+="}\n";
        }
        if(flow.control && (sca==9 || sca==10 || sca==12 || sca==17 || sca==18)){
            // The predicate was evaluated before this instruction's writes (pass_), exactly like run_vertex_program's `pass`.
            const bool bool_branch=sca==17 || sca==18,call=sca==10 || sca==18;
            const std::string condition=bool_branch?"(((R(0x1ff8u)>>"+u((d[3]>>23)&31)+")&1u)=="+u((d[3]>>28)&1)+")":"(pass_.x||pass_.y||pass_.z||pass_.w)";
            if(sca==12)body+="if("+condition+"){if(sp_==0u){done_=true;break;}pc_=stack_[--sp_];continue;}\n";
            else{
                const unsigned target=VertexFlow::immediate(d.data());
                const std::string jump=target>=544?"atomicOr(w[7],16u);return;":"pc_="+u(target)+";continue;";
                body+="if("+condition+"){"+(call?"if(sp_>=16u){atomicOr(w[7],16u);return;}stack_[sp_++]="+u(pc+1)+";":"")+jump+"}\n";
            }
        }
        body+="}\n";
        if(!valid)return {};
        if(d[3]&1){
            if(flow.control){body+="done_=true;break;\n";continue;}
            ended=true;break;
        }
        if(flow.control && pc+1>=544)body+="atomicOr(w[7],16u);return;\n";
    }
    if(flow.control)body+="default:atomicOr(w[7],16u);return;}if(done_)break;}\n";
    else if(!ended)return {};
    body+="for(uint n=0u;n<16u;++n){uvec4 v=floatBitsToUint(o[n]);uint at=1u+w[0]*64u+id*64u+n*4u;for(uint c=0u;c<4u;++c)w[at+c]=v[c];}}\n";
    return preamble()+body;
}
[[maybe_unused]] static bool vertices(NativeVulkan& vk,const NativeRsx& gpu,unsigned start,const std::vector<std::array<Vec4,16>>& inputs,std::vector<VertexOut>& outputs){
    auto shader=vertex_shader(gpu,start,true);if(!shader)return false;
    if(inputs.empty()){outputs.clear();return true;}
    if(inputs.size()>UINT32_MAX/128)return false;
    std::vector<unsigned> data{static_cast<unsigned>(inputs.size())};
    for(const auto& in:inputs)for(const auto& v:in)append(data,v);
    const auto at=data.size();data.resize(data.size()+inputs.size()*64);
    for(unsigned i=0;i<468*4;++i)data.push_back(gpu.transform_constants[i]);
    const std::string from="load4(w[2]+",to="load4(1u+w[0]*128u+";
    for(std::size_t pos=0;(pos=shader->find(from,pos))!=std::string::npos;pos+=to.size())shader->replace(pos,from.size(),to);
    if(!vk.compute_fits(data.size()*4,(inputs.size()+63)/64))return false;
    vk.compute(*shader,data,(inputs.size()+63)/64);outputs.resize(inputs.size());
    for(unsigned i=0;i<inputs.size();++i)for(unsigned n=0;n<16;++n)for(unsigned c=0;c<4;++c)outputs[i].o[n][c]=bits_float(data[at+i*64+n*4+c]);
    return true;
}
'''
