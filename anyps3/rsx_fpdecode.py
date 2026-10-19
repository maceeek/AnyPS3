"""Predecoded FP operands; errors remain lazy, at the original operand read."""

TYPES = r'''
struct FpFlowPlan {std::map<unsigned,unsigned> offsets;bool duplicate=false;};
struct FpSourcePlan {
    unsigned type=0,index=0,precision=0;bool half=false,absolute=false,negative=false,indexed=false;
    std::array<unsigned,4> swizzle{};
};
'''

PREPARE = r'''
static void prepare_fragment_instruction(FpInst& inst){
    for(unsigned n=0;n<3;++n){
        const unsigned raw=n==0?inst.s0:n==1?inst.s1:inst.s2;
        auto& p=inst.sources[n];p.type=raw&3;p.half=raw&256;
        p.index=p.type==1?(inst.d>>13)&15:(raw>>2)&63;
        p.precision=(inst.s1>>(19+3*n))&7;
        if(p.precision==5 || (p.precision==1 && p.half && p.type==0))p.precision=0;
        p.absolute=(raw>>(n?18:29))&1;p.negative=(raw>>17)&1;p.indexed=inst.s2&(1u<<30);
        for(unsigned i=0;i<4;++i)p.swizzle[i]=(raw>>(9+2*i))&3;
    }
    const unsigned op=((inst.d>>24)&63)|((inst.s1>>31)<<6);
    if(op>=1 && op<=3 && ((inst.s0>>18)&7)==7 && ((inst.d>>9)&15)==15 &&
       !(inst.d&0xc0c00180u) && !(inst.s1&0x70000000u) && ((inst.d>>1)&63)<48)
        inst.fast_opcode=op;
    inst.decoded=true;
}
// Exact instruction/constant bytes, never guest pointers or floating equality
// (NaNs and signed zeros must be distinct). Loading still performs all guest
// reads, preserving access faults and GuestMemory's last-region hint.
static std::vector<FpInst> prepared_fragment_program(std::vector<FpInst> program){
    static thread_local std::vector<std::vector<FpInst>> cache;
    for(const auto& entry:cache){
        if(entry.size()!=program.size())continue;
        bool same=true;
        for(unsigned i=0;i<entry.size();++i){const auto& a=entry[i];const auto& b=program[i];
            if(a.d!=b.d || a.s0!=b.s0 || a.s1!=b.s1 || a.s2!=b.s2 || a.word_offset!=b.word_offset ||
               a.has_constant!=b.has_constant || std::memcmp(a.constant.data(),b.constant.data(),16)){same=false;break;}
        }
        if(same)return entry;
    }
    for(auto& inst:program)prepare_fragment_instruction(inst);
    if(!program.empty())program[0].program_prepared=true;
    if(std::any_of(program.begin(),program.end(),[](const FpInst& i){return (i.s1>>31)!=0;})){
        auto flow=std::make_shared<FpFlowPlan>();unsigned words=0;
        for(unsigned i=0;i<program.size();++i){
            const auto& inst=program[i];const unsigned at=inst.word_offset==UINT32_MAX?words:inst.word_offset;
            if(!flow->offsets.emplace(at,i).second)flow->duplicate=true;
            words=at+4+(inst.has_constant?4:0);
        }
        flow->offsets.emplace(words,unsigned(program.size()));program[0].flow=flow;
    }
    if(cache.size()==64)cache.erase(cache.begin());
    cache.push_back(program);return program;
}
'''

SOURCE = r'''
static inline Vec4 fp_source(const FpInst& inst,unsigned n,const FpContext& ctx){
    if(!inst.decoded)return fp_source_raw(inst,n,ctx);
    const auto& p=inst.sources[n];Vec4 sw;
    switch(p.type){
    case 0:{
        const auto& r=ctx.r.at(p.half?p.index/2:p.index);
        for(unsigned i=0;i<4;++i){const unsigned c=p.swizzle[i];
            sw[i]=p.half?half_float(static_cast<std::uint16_t>(r[(p.index&1)*2+c/2]>>(16*(c&1)))):bits_float(r[c]);
        }break;
    }
    case 1:
        if(p.indexed)throw std::runtime_error("RSX FP indexed input is unsupported");
        if(p.index>14)throw std::runtime_error("invalid RSX FP input attribute");
        for(unsigned i=0;i<4;++i)sw[i]=ctx.inputs[p.index][p.swizzle[i]];
        break;
    case 2:for(unsigned i=0;i<4;++i)sw[i]=inst.constant[p.swizzle[i]];break;
    default:throw std::runtime_error("invalid RSX FP source register type");
    }
    if(p.precision>5)throw std::runtime_error("unsupported RSX FP source precision modifier");
    // Preserve min/max operand order: NaN clamps and signed zero are observable.
    if(p.precision==4)for(auto& x:sw)x=std::min(1.f,std::max(0.f,x));
    if(p.absolute)for(auto& x:sw)x=std::fabs(x);
    if(p.precision && p.precision!=4){
        const float limit=p.precision==1?65504.f:p.precision==2?2.f:1.f;
        for(auto& x:sw)x=std::min(limit,std::max(-limit,x));
    }
    if(p.negative)for(auto& x:sw)x=-x;
    return sw;
}
'''

FAST = r'''
// Four native binary32 lanes, with the same separate multiply/add operations.
// No reassociation, approximate math, or FMA contraction is enabled here.
using FpLanes = float __attribute__((vector_size(16)));
static bool fast_fragment_instruction(const FpInst& inst,std::array<FpContext,4>& quad){
    if(!inst.fast_opcode)return false;
    std::array<Vec4,4> a{},b{};
    for(unsigned l=0;l<4;++l){a[l]=fp_source(inst,0,quad[l]);if(inst.fast_opcode!=1)b[l]=fp_source(inst,1,quad[l]);}
    const unsigned dest=(inst.d>>1)&63;
    for(unsigned c=0;c<4;++c){
        const FpLanes x{a[0][c],a[1][c],a[2][c],a[3][c]},y{b[0][c],b[1][c],b[2][c],b[3][c]};
        const FpLanes v=inst.fast_opcode==1?x:inst.fast_opcode==2?x*y:x+y;
        for(unsigned l=0;l<4;++l)quad[l].r[dest][c]=float_bits(v[l]);
    }
    return true;
}
'''
