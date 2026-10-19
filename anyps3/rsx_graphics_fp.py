"""Scalar fragment lowering; reuse ALU expressions, never the quad ABI.

FP32 locals are the backing bits of the FP16 aliases. All register indices and
write masks are resolved on the host before GLSL reaches a shader compiler.
"""
CPP = r'''
static std::string fp_read(unsigned index,bool half){
    if(!half)return "R"+std::to_string(index);
    return "H"+std::to_string(index);
}
static void scalar_registers(std::string& text){
    for(unsigned n=0;n<96;++n)for(const auto* lane:{"l","0","output_lane"}){
        replace_all(text,"rd(q["+std::string(lane)+"],"+rsxvk::u(n)+",true)",fp_read(n,true));
        if(n<48)replace_all(text,"rd(q["+std::string(lane)+"],"+rsxvk::u(n)+",false)",fp_read(n,false));}
    replace_all(text,"q[l]","q[0]");
}
static std::string scalar_locals(){
    std::string s="bool returned=false;\n";
    for(unsigned n=0;n<48;++n)s+="precise vec4 R"+std::to_string(n)+"=vec4(0);\n";
    // H[2*r] aliases R[r].xy, H[2*r+1] aliases R[r].zw, in
    // low-half/high-half order. Macros are expressions, not extra storage.
    for(unsigned n=0;n<96;++n){const auto r="R"+std::to_string(n/2);const auto a=std::string(n%2?"z":"x"),b=std::string(n%2?"w":"y");
        s+="#define H"+std::to_string(n)+" vec4(unpackHalf2x16(floatBitsToUint("+r+"."+a+")),unpackHalf2x16(floatBitsToUint("+r+"."+b+")))\n";}
    return s;
}
static std::optional<std::string> scalar_instruction(const FpInst& instruction,unsigned pc,rsxvk::FragmentFailure* failure,const std::string& active="true"){
    auto i=instruction;i.d|=1;auto s=rsxvk::fragment({i},true,false,failure);if(!s)return {};
    const std::string head="void shade(inout C q[4]) {\n",tail="}\n";
    if(s->compare(0,head.size(),head)!=0)throw std::runtime_error("scalar FP lowering ABI");
    s->erase(0,head.size());s->resize(s->size()-tail.size());
    if(s->empty())return s;
    replace_all(*s,"{ vec4 aa[4]; for(uint l=0u;l<4u;++l) aa[l]=","{ precise vec4 a=");
    replace_all(*s,"for(uint l=0u;l<4u;++l) aa[l].xyz/=aa[l].w;","a.xyz/=a.w;");
    replace_all(*s,"for(uint l=0u;l<4u;++l) { precise vec4 a=aa[l], b=","precise vec4 b=");
    replace_all(*s,"aa[1]-aa[0]","dFdx(a)");replace_all(*s,"aa[2]-aa[0]","dFdy(a)");replace_all(*s,"aa[l]","a");
    replace_all(*s,"}}\n","}\n");
    replace_all(*s,"load4(w[14]+0u)","load4(w[14]+"+rsxvk::u(pc*4)+")");
    const unsigned dst=(i.d>>1)&63,mask=(i.d>>9)&15;const bool half=i.d&128;
    const auto write="wr(q[l],"+rsxvk::u(dst)+","+(half?"true":"false")+",mask,v);";
    std::string store;
    for(unsigned c=0;c<4;++c)if(mask&(1u<<c)){
        const auto component=std::string(1,"xyzw"[c]);
        store+="if(pass["+rsxvk::u(c)+"]){";
        if(!half)store+="R"+std::to_string(dst)+"."+component+"=v."+component+";";
        else {const auto at=(dst&1)*2+c/2;const auto target="R"+std::to_string(dst/2)+"."+std::string(1,"xyzw"[at]);
            store+=target+"=uintBitsToFloat((floatBitsToUint("+target+")&"+std::string(c&1?"65535u":"0xffff0000u")+")|((packHalf2x16(vec2(v."+component+",0))&65535u)"+std::string(c&1?"<<16u":"")+"));";}
        store+="}\n";}
    replace_all(*s,write,store);
    replace_all(*s,"uint mask="+rsxvk::u(mask)+";for(uint n=0u;n<4u;++n)if(!pass[n])mask&=~(1u<<n);","");
    if(i.d&256){
        const auto cc=std::to_string((i.s0>>30)&1);
        std::string updates;for(unsigned c=0;c<4;++c)if(mask&(1u<<c))updates+="if(pass["+rsxvk::u(c)+"])q[l].cc["+cc+"]["+rsxvk::u(c)+"]=stored["+rsxvk::u(c)+"];\n";
        replace_all(*s,"for(uint n=0u;n<4u;++n)if((mask&(1u<<n))!=0u)q[l].cc["+cc+"][n]=stored[n];",updates);
    }
    const unsigned exec=(i.s0>>18)&7,op=(i.d>>24)&63;
    if(exec==7){
        replace_all(*s,"bvec4 pass=bvec4(true);","");replace_all(*s,"any(pass)","true");
        for(unsigned c=0;c<4;++c)replace_all(*s,"if(pass["+rsxvk::u(c)+"])","");
    }
    if(op!=23 && op!=24 && op!=25 && op!=47 && op!=49){replace_all(*s,"bool killed=false;","");replace_all(*s,"if(killed && true)q[l].killed=true;","");replace_all(*s,"if(killed && any(pass))q[l].killed=true;","");}
    scalar_registers(*s);
    if(active!="true"){
        // Compute helpers before predicated writes, but never execute a
        // texture fault in an IF that is inactive for the whole quad.
        const auto value=s->find("precise vec4 v="),commit=s->find(";\n",value)+2;
        if(value==std::string::npos || commit<2)throw std::runtime_error("scalar FP commit ABI");
        *s=s->substr(0,commit)+"if("+active+"){\n"+s->substr(commit,s->size()-commit-2)+"}\n}\n";
        if(op==21 || op==22 || op==23 || op==24 || op==49){
            std::string guard="uint quad_mask=0u;\n";
            for(unsigned lane=0;lane<4;++lane)guard+="quad_mask|=uint(subgroupQuadBroadcast(uint("+active+"),"+rsxvk::u(lane)+"))<<"+rsxvk::u(lane)+";\n";
            guard+="if(quad_mask!=0u && quad_mask!=15u)atomicOr(w[7],4u);\n";
            *s="{\n"+guard+"if(quad_mask!=0u)"+*s+"}\n";
        }
    }
    return s;
}
static std::optional<std::string> scalar_fragment(const std::vector<FpInst>& code,rsxvk::FragmentFailure* failure){
    const bool flow=std::any_of(code.begin(),code.end(),[](const auto& i){return (i.s1>>31)!=0;});
    if(!flow){std::string body;for(unsigned pc=0;pc<code.size();++pc){auto s=scalar_instruction(code[pc],pc,failure);if(!s){if(failure)failure->instruction=pc;return {};}body+=*s;if(code[pc].d&1)return body;}
        if(!code.empty()){if(failure)*failure={unsigned(code.size()-1),"missing_end"};return {};}return body;}
    const bool divergent_exit=std::any_of(code.begin(),code.end(),[](const auto& i){const auto op=((i.d>>24)&63)|((i.s1>>31)<<6);return op==69 || (op==64 && ((i.s0>>18)&7)!=7);});
    if(divergent_exit)for(unsigned pc=0;pc<code.size();++pc){const auto& i=code[pc];const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6);if(op==21 || op==22 || op==23 || op==24 || op==49){if(failure)*failure={pc,"derivative_with_divergent_exit"};return {};}}
    std::map<unsigned,unsigned> offsets;unsigned words=0;
    for(unsigned pc=0;pc<code.size();++pc){const auto& i=code[pc];const auto at=i.word_offset==UINT32_MAX?words:i.word_offset;
        if(!offsets.emplace(at,pc).second){if(failure)*failure={pc,"duplicate_instruction_offset"};return {};}
        words=at+4+(i.has_constant?4:0);}
    offsets.emplace(words,unsigned(code.size()));
    auto target=[&](unsigned at){auto i=offsets.find(at);return i==offsets.end()?UINT_MAX:i->second;};
    auto predicate=[&](const FpInst& i){
        const unsigned exec=(i.s0>>18)&7;if(exec==7)return std::string("true");if(!exec)return std::string("false");
        std::string c="q[0].cc["+std::to_string(i.s0>>31)+"].";for(unsigned n=0;n<4;++n)c+="xyzw"[(i.s0>>(21+2*n))&3];
        std::string p="(";if(exec&1)p+="any(lessThan("+c+",vec4(0)))||";if(exec&2)p+="any(equal("+c+",vec4(0)))||";if(exec&4)p+="any(greaterThan("+c+",vec4(0)))||";
        return p+"false)";};
    std::string body;bool valid=true;
    auto emit=[&](auto&& self,unsigned begin,unsigned end,unsigned depth,bool divergent,unsigned loops,const std::string& active)->void{
        if(depth>32){valid=false;if(failure)*failure={begin,"flow_nesting"};return;}
        for(unsigned pc=begin;pc<end && valid;++pc){const auto& i=code[pc];const unsigned op=((i.d>>24)&63)|((i.s1>>31)<<6);const auto condition=predicate(i);
            auto reject=[&](const char* reason){valid=false;if(failure)*failure={pc,reason};};
            body+="if(!returned){\n";
            if(op>=64){
                if(op==66){const auto other=target(i.s1&0x7fffffff),after=target(i.s2);if(other<pc+1 || after<other || after>end){reject("if_range");return;}
                    bool derivatives=false;for(unsigned at=pc+1;at<after;++at){const auto& x=code[at];const auto o=((x.d>>24)&63)|((x.s1>>31)<<6);derivatives|=o==21 || o==22 || o==23 || o==24 || o==49;}
                    if(derivatives || active!="true"){
                        const auto branch="branch"+std::to_string(pc),taken="taken"+std::to_string(pc),other_active="other"+std::to_string(pc);
                        body+="bool "+branch+"="+condition+", "+taken+"=("+active+") && "+branch+", "+other_active+"=("+active+") && !"+branch+";\n";
                        self(self,pc+1,other,depth+1,divergent,loops,taken);self(self,other,after,depth+1,divergent,loops,other_active);
                    }else{
                        body+="if("+condition+"){\n";self(self,pc+1,other,depth+1,divergent || condition!="true",loops,active);body+="}else{\n";self(self,other,after,depth+1,divergent || condition!="true",loops,active);body+="}\n";
                    }pc=after-1;
                }else if(op==67 || op==68){if(active!="true"){reject("loop_in_predicated_IF");return;}const auto after=target(i.s2),count=(i.s1>>2)&255;if(after<pc+1 || after>end){reject("loop_range");return;}
                    const auto n=std::to_string(pc);body+="if("+condition+")for(uint iteration"+n+"=0u;iteration"+n+"<"+rsxvk::u(count)+" && !returned;++iteration"+n+"){\n";
                    self(self,pc+1,after,depth+1,divergent || condition!="true",loops+1,active);body+="}\n";pc=after-1;
                }else if(op==64){if(!loops){reject("break_outside_loop");return;}body+="if("+condition+")break;\n";}
                else if(op==69)body+="if(("+active+") && ("+condition+"))returned=true;\n";
                else {reject(op==65?"CAL_unsupported_by_CPU_reference":"flow_opcode");return;}
            }else{
                if(divergent && (op==21 || op==22 || op==23 || op==24 || op==49)){reject("derivative_in_divergent_flow");return;}
                auto s=scalar_instruction(i,pc,failure,active);if(!s){valid=false;if(failure)failure->instruction=pc;return;}body+=*s;
            }
            if(i.d&1)body+="if("+active+")returned=true;\n";
            body+="}\n";
        }
    };
    emit(emit,0,unsigned(code.size()),0,false,0,"true");if(!valid)return {};return body;
}
'''
