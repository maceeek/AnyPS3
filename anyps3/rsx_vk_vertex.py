"""RSX VP ALU, predicates, address registers and checked indexed fetch."""
CPP = r'''
static std::optional<std::string> vertex_shader(const NativeRsx& gpu,unsigned start,bool dynamic_constants=false,bool checked=false){
    std::string body="void main(){uint id=gl_GlobalInvocationID.x;if(id>=w[0])return;vec4 t[32],o[16],input_[16],cc[2];ivec4 address_[2];cc[0]=cc[1]=vec4(0);address_[0]=address_[1]=ivec4(0);for(uint n=0u;n<32u;++n)t[n]=vec4(0);for(uint n=0u;n<16u;++n){o[n]=vec4(0,0,0,1);input_[n]=load4(1u+id*64u+n*4u);}\n";
    bool ended=false;
    for(unsigned pc=start;pc<544;++pc){
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
                    if(index<468)for(unsigned j=0;j<4;++j){if(!gpu.transform_constants_written[index*4+j])valid=false;c[j]=bits_float(gpu.transform_constants[index*4+j]);}
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
        if(sca){const auto a=src(2);std::string expr;
            switch(sca){case 1:expr="a";break;case 2:expr="vec4(1)/a";break;
            case 3:expr="uintBitsToFloat((floatBitsToUint(clamp(abs(vec4(1)/a),vec4(5.42101e-20),vec4(1.884467e19)))&uvec4(0x7fffffffu))|(floatBitsToUint(vec4(1)/a)&uvec4(0x80000000u)))";break;
            case 4:expr="vec4(1.0/sqrt(abs(a.x)))";break;case 5:expr="exp(a)";break;case 6:expr="log(a)";break;
            case 13:expr="log2(abs(a))";break;case 14:expr="exp2(a)";break;case 15:expr="sin(a)";break;case 16:expr="cos(a)";break;
            default:return {};
            }
            body+="{precise vec4 a="+a+";";write(expr,true);body+="}\n";
        }
        body+="}\n";
        if(!valid)return {};
        if(d[3]&1){ended=true;break;}
    }
    if(!ended)return {};
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
