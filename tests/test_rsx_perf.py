"""Bit-exact differential tests against the frozen pre-optimization renderer.

The reference CPP literals below were captured before editing rsx_draw.py;
they deliberately do not derive the expected renderer from the new algorithm.
"""
REFERENCE_DRAW = r'''
#include <cmath>
#include <optional>
#include <cstring>
#include <limits>
// The interpreter loops are hot; optimize them independently of the -Og
// guest translation. This does not change their semantics.
#pragma GCC push_options
#pragma GCC optimize("O2")
namespace rsxdraw {
using Vec4=std::array<float,4>;
static float bits_float(std::uint32_t v){float f;std::memcpy(&f,&v,4);return f;}
static float half_float(std::uint16_t h){
    const unsigned sign=h>>15,exp=(h>>10)&31,frac=h&1023;
    float value;
    if(!exp)value=std::ldexp(float(frac),-24);
    else if(exp==31)value=frac?std::numeric_limits<float>::quiet_NaN():std::numeric_limits<float>::infinity();
    else value=std::ldexp(float(frac|1024),int(exp)-25);
    return sign?-value:value;
}
static std::uint16_t float_half(float f){
    // Round to nearest even binary16 (RSX H registers).
    std::uint32_t x;std::memcpy(&x,&f,4);
    const std::uint32_t sign=(x>>16)&0x8000;std::int32_t exp=((x>>23)&255)-127+15;std::uint32_t mant=x&0x7fffff;
    if(((x>>23)&255)==255)return sign|0x7c00|(mant?0x200:0);
    if(exp>=31)return sign|0x7c00;
    if(exp<=0){
        if(exp<-10)return sign;
        mant|=0x800000;const unsigned shift=14-exp;
        std::uint32_t h=mant>>shift;const std::uint32_t rest=mant&((1u<<shift)-1),half=1u<<(shift-1);
        if(rest>half || (rest==half && (h&1)))++h;
        return sign|h;
    }
    std::uint32_t h=(exp<<10)|(mant>>13);const std::uint32_t rest=mant&0x1fff;
    if(rest>0x1000 || (rest==0x1000 && (h&1)))++h;
    return sign|h;
}
struct Reg { // gpu.registers access with explicit failure for missing state.
    const NativeRsx& gpu;
    std::uint32_t operator()(std::uint32_t r)const{
        auto it=gpu.registers.find(r);
        if(it==gpu.registers.end())throw std::runtime_error("RSX draw lacks register state "+std::to_string(r));
        return it->second;
    }
    std::uint32_t get(std::uint32_t r,std::uint32_t fallback)const{auto it=gpu.registers.find(r);return it==gpu.registers.end()?fallback:it->second;}
};
static std::uint64_t location_address(const NativeRsx& gpu,unsigned location,std::uint32_t offset,std::uint32_t bytes){
    if(location==0){
        if(offset>gpu.local_size || bytes>gpu.local_size-offset)throw std::runtime_error("RSX draw reads outside VRAM");
        return UINT64_C(0xc0000000)+offset;
    }
    if(location==1){
        const auto first=rsx_address(gpu,offset);
        if(bytes && rsx_address(gpu,offset+bytes-1)!=first+bytes-1)throw std::runtime_error("RSX draw main-memory read spans discontiguous IO pages");
        return first;
    }
    throw std::runtime_error("unsupported RSX draw memory location");
}

// ---------------- Vertex program ----------------
struct VertexOut { std::array<Vec4,16> o; };
static Vec4 vp_source(const std::array<std::uint32_t,4>& d,unsigned raw,unsigned n,const std::array<Vec4,32>& temp,
                      const std::array<Vec4,16>& input,const NativeRsx& gpu){
    const unsigned type=raw&3,tmp=(raw>>2)&63;
    Vec4 v{};
    if(type==1){if(tmp>=32)throw std::runtime_error("RSX VP temp register out of range");v=temp[tmp];}
    else if(type==2)v=input[(d[1]>>8)&15];
    else if(type==3){
        if(d[3]&2)throw std::runtime_error("RSX VP indexed constant read is unsupported");
        const unsigned c=(d[1]>>12)&1023;
        if(c<468)for(unsigned i=0;i<4;++i){
            if(!gpu.transform_constants_written[c*4+i])throw std::runtime_error("RSX VP reads unwritten constant "+std::to_string(c));
            v[i]=bits_float(gpu.transform_constants[c*4+i]);
        }
    }else throw std::runtime_error("invalid RSX VP source register type");
    Vec4 s;for(unsigned i=0;i<4;++i)s[i]=v[(raw>>(14-2*i))&3];
    if((d[0]>>(21+n))&1)for(auto& x:s)x=std::fabs(x);
    if((raw>>16)&1)for(auto& x:s)x=-x;
    return s;
}
static VertexOut run_vertex_program(const NativeRsx& gpu,std::uint32_t start,const std::array<Vec4,16>& input){
    VertexOut out;for(auto& r:out.o)r={0,0,0,1};
    std::array<Vec4,32> temp{};
    unsigned pc=start;
    for(unsigned steps=0;;++steps){
        if(pc>=512 || steps>=512)throw std::runtime_error("RSX VP ran past 512 instructions");
        std::array<std::uint32_t,4> d;
        for(unsigned i=0;i<4;++i){
            if(!gpu.transform_program_written[pc*4+i])throw std::runtime_error("RSX VP executes unwritten instruction");
            d[i]=gpu.transform_program[pc*4+i];
        }
        if(((d[0]>>13)&1) && ((d[0]>>10)&7)!=7)throw std::runtime_error("RSX VP conditional execution is unsupported");
        if((d[0]>>14)&1 || (d[0]>>29)&1)throw std::runtime_error("RSX VP condition-code update is unsupported");
        if((d[0]>>27)&1)throw std::runtime_error("RSX VP indexed input is unsupported");
        const unsigned s0=((d[2]>>23)&511)|((d[1]&255)<<9),s1=(d[2]>>6)&0x1ffff,s2=((d[3]>>21)&2047)|((d[2]&63)<<11);
        const unsigned vec=(d[1]>>22)&31,sca=(d[1]>>27)&31;
        const unsigned dst=(d[3]>>2)&31,vtmp=(d[0]>>15)&63,stmp=(d[3]>>7)&63;
        const bool vec_result=(d[0]>>30)&1,sat=(d[0]>>26)&1;
        auto src=[&](unsigned i){return vp_source(d,i==0?s0:i==1?s1:s2,i,temp,input,gpu);};
        auto write=[&](const Vec4& value,bool scalar){
            const unsigned maskbits=scalar?(d[3]>>17)&15:(d[3]>>13)&15; // bit3=x .. bit0=w
            Vec4 v=value;if(sat)for(auto& x:v)x=std::min(1.f,std::max(0.f,x));
            const bool to_output=scalar?!vec_result:vec_result;
            const unsigned tmp=scalar?stmp:vtmp;
            for(unsigned i=0;i<4;++i)if(maskbits&(8>>i)){
                if(to_output && dst!=31){if(dst>15)throw std::runtime_error("RSX VP output register out of range");out.o[dst][i]=v[i];}
                if(tmp!=63){if(tmp>=32)throw std::runtime_error("RSX VP temp write out of range");temp[tmp][i]=v[i];}
            }
        };
        Vec4 vr{};bool vec_write=true;
        switch(vec){
        case 0:vec_write=false;break;
        case 1:vr=src(0);break;
        case 2:{auto a=src(0),b=src(1);for(unsigned i=0;i<4;++i)vr[i]=a[i]*b[i];break;}
        case 3:{auto a=src(0),b=src(2);for(unsigned i=0;i<4;++i)vr[i]=a[i]+b[i];break;}
        case 4:{auto a=src(0),b=src(1),c=src(2);for(unsigned i=0;i<4;++i)vr[i]=std::fma(a[i],b[i],c[i]);break;}
        case 5:{auto a=src(0),b=src(1);float t=a[0]*b[0]+a[1]*b[1]+a[2]*b[2];vr={t,t,t,t};break;}
        case 6:{auto a=src(0),b=src(1);float t=a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+b[3];vr={t,t,t,t};break;}
        case 7:{auto a=src(0),b=src(1);float t=a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+a[3]*b[3];vr={t,t,t,t};break;}
        case 8:{auto a=src(0),b=src(1);vr={1.f,a[1]*b[1],a[2],b[3]};break;}
        case 9:{auto a=src(0),b=src(1);for(unsigned i=0;i<4;++i)vr[i]=std::min(a[i],b[i]);break;}
        case 10:{auto a=src(0),b=src(1);for(unsigned i=0;i<4;++i)vr[i]=std::max(a[i],b[i]);break;}
        case 11:case 12:case 16:case 17:case 18:case 19:case 20:case 21:{
            auto a=src(0),b=src(1);
            for(unsigned i=0;i<4;++i){bool t=false;
                switch(vec){case 11:t=a[i]<b[i];break;case 12:t=a[i]>=b[i];break;case 16:t=a[i]==b[i];break;case 17:t=false;break;
                case 18:t=a[i]>b[i];break;case 19:t=a[i]<=b[i];break;case 20:t=a[i]!=b[i];break;case 21:t=true;break;}
                vr[i]=t?1.f:0.f;}
            break;}
        case 14:{auto a=src(0);for(unsigned i=0;i<4;++i)vr[i]=a[i]-std::floor(a[i]);break;}
        case 15:{auto a=src(0);for(unsigned i=0;i<4;++i)vr[i]=std::floor(a[i]);break;}
        case 22:{auto a=src(0);for(unsigned i=0;i<4;++i)vr[i]=a[i]>0?1.f:a[i]<0?-1.f:0.f;break;}
        default:throw std::runtime_error("unsupported RSX VP vector opcode "+std::to_string(vec));
        }
        if(vec_write)write(vr,false);
        Vec4 sr{};bool sca_write=true;
        switch(sca){
        case 0:sca_write=false;break;
        case 1:sr=src(2);break;
        case 2:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=1.f/a[i];break;}
        case 3:{auto a=src(2);for(unsigned i=0;i<4;++i){float r=1.f/a[i];sr[i]=std::copysign(std::min(std::max(std::fabs(r),5.42101e-20f),1.884467e19f),r);}break;}
        case 4:{auto a=src(2);float t=1.f/std::sqrt(std::fabs(a[0]));sr={t,t,t,t};break;}
        case 13:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::log2(std::fabs(a[i]));break;}
        case 14:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::exp2(a[i]);break;}
        case 15:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::sin(a[i]);break;}
        case 16:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::cos(a[i]);break;}
        default:throw std::runtime_error("unsupported RSX VP scalar opcode "+std::to_string(sca));
        }
        if(sca_write)write(sr,true);
        if(d[3]&1)return out;
        ++pc;
    }
}

// ---------------- Vertex fetch ----------------
static Vec4 register_attribute(const RsxDrawState::Immediate& value){
    Vec4 v{0,0,0,1};
    if(value.type==1){for(unsigned i=0;i<4;++i)v[i]=float((value.words[0]>>(24-8*i))&255)/255.f;}
    else for(unsigned i=0;i<value.count;++i)v[i]=bits_float(value.words[i]);
    return v;
}
static Vec4 fetch_attribute(PpuState& s,const Reg& reg,unsigned attr,std::uint32_t index){
    auto& gpu=s.rsx;auto& m=s.memory;
    const auto format=reg(0x1740+4*attr);
    const unsigned type=format&15,size=(format>>4)&15,stride=(format>>8)&255,frequency=format>>16;
    Vec4 v{0,0,0,1};
    if(!size){
        // Disabled array: the register attribute value (SET_VERTEX_DATA*_M).
        return register_attribute(gpu.draw.immediate[attr]);
    }
    if(frequency)throw std::runtime_error("RSX vertex frequency/divider operation is unsupported");
    if(size>4)throw std::runtime_error("invalid RSX vertex attribute size");
    const auto offset=reg(0x1680+4*attr);
    const unsigned width=type==2?4:(type==1 || type==3 || type==5)?2:type==6?4:1;
    const unsigned bytes=type==6?4:width*size;
    const std::uint64_t at=std::uint64_t(offset&0x7fffffff)+reg.get(0x1738,0)+std::uint64_t(stride)*index;
    if(at>0xffffffffu)throw std::runtime_error("RSX vertex address overflow");
    const auto address=location_address(gpu,offset>>31,static_cast<std::uint32_t>(at),bytes);
    if(type==6){
        const auto w=static_cast<std::uint32_t>(m.load(address,4));
        auto sx=[](std::uint32_t x,unsigned n){const std::int32_t t=std::int32_t(x<<(32-n))>>(32-n);return std::max(-1.f,float(t)/float((1<<(n-1))-1));};
        v={sx(w&2047,11),sx((w>>11)&2047,11),sx(w>>22,10),1.f};return v;
    }
    for(unsigned i=0;i<size;++i){
        const auto raw=static_cast<std::uint32_t>(m.load(address+i*width,width));
        switch(type){
        case 1:v[i]=std::max(-1.f,float(std::int16_t(raw))/32767.f);break;
        case 2:v[i]=bits_float(raw);break;
        case 3:v[i]=half_float(static_cast<std::uint16_t>(raw));break;
        case 4:v[i]=float(raw)/255.f;break;
        case 5:v[i]=float(std::int16_t(raw));break;
        case 7:v[i]=float(raw);break;
        default:throw std::runtime_error("unsupported RSX vertex attribute type "+std::to_string(type));
        }
    }
    return v;
}

// ---------------- Textures ----------------
struct TextureLevel { unsigned width=0,height=0,pitch=0; const std::uint8_t* data=nullptr; };
struct Texture {
    bool valid=false;unsigned format=0,wrap_s=0,wrap_t=0,min=0,mag=0,gamma=0;bool unnormalized=false;
    std::vector<TextureLevel> levels;float bias=0,min_lod=0,max_lod=0;
    std::array<unsigned,4> remap_channel{},remap_control{};
    Vec4 border{};
};
static Texture decode_texture(PpuState& s,const Reg& reg,unsigned unit){
    Texture t;auto& gpu=s.rsx;
    const unsigned base=0x1a00+unit*32;
    const auto control0=reg.get(base+0xc,0);
    if(!(control0>>31))throw std::runtime_error("RSX FP samples disabled texture unit "+std::to_string(unit));
    const auto format=reg(base+4),address=reg(base+8),control1=reg(base+0x10),filter=reg(base+0x14),rect=reg(base+0x18);
    const unsigned location=format&3,dimension=(format>>4)&15,mipmaps=std::max(1u,format>>16);
    if(location!=1 && location!=2)throw std::runtime_error("invalid RSX texture location");
    if(format&4 || dimension!=2)throw std::runtime_error("unsupported RSX texture dimension/cubemap");
    // Bit 3 clear = CELL_GCM_TEXTURE_BORDER_TEXTURE: rows/columns of border
    // texels surround every level (one texel for linear layouts).
    const bool border_texels=!(format&8);
    const unsigned raw_format=(format>>8)&0xff;const bool linear=raw_format&0x20;
    t.unnormalized=raw_format&0x40;t.format=raw_format&~0x60u;
    const unsigned width=rect>>16,height=rect&0xffff;
    if(!width || !height || width>4096 || height>4096 || mipmaps>13)throw std::runtime_error("invalid RSX texture size or mipmap count");
    unsigned bytes_per_block=0,block=1;
    switch(t.format){
    case 0x85:bytes_per_block=4;if(!linear)throw std::runtime_error("swizzled RSX A8R8G8B8 texture is unsupported");break;
    case 0x86:bytes_per_block=8;block=4;break;
    case 0x87:case 0x88:bytes_per_block=16;block=4;break;
    default:throw std::runtime_error("unsupported RSX texture format "+std::to_string(t.format));
    }
    // Mip levels follow each other. Linear (LN) textures keep the row pitch
    // for every level; other layouts use the packed pitch (RPCS3 TextureUtils).
    const unsigned suggested=reg.get(0x1840+unit*4,0)&0xfffff;
    std::uint64_t offset=0;unsigned w=width,h=height;
    if(border_texels && (block!=1 || !linear))throw std::runtime_error("RSX border texels are supported for linear uncompressed textures only");
    const unsigned border=border_texels?1:0;
    struct Pending{unsigned w,h,pitch;std::uint64_t offset;};std::vector<Pending> pending;
    for(unsigned level=0;level<mipmaps;++level){
        const unsigned packed=((w+2*border+block-1)/block)*bytes_per_block;
        unsigned pitch=linear?(suggested?suggested:packed):packed;
        if(pitch<packed)throw std::runtime_error("RSX texture pitch smaller than row");
        pending.push_back({w,h,pitch,offset+std::uint64_t(border)*pitch+border*bytes_per_block});
        offset+=std::uint64_t(pitch)*((h+2*border+block-1)/block);
        w=std::max(1u,w/2);h=std::max(1u,h/2);
    }
    if(offset>0x10000000)throw std::runtime_error("RSX texture too large");
    const auto start=location_address(gpu,location-1,reg(base),static_cast<std::uint32_t>(offset));
    const auto* data=s.memory.access(start,static_cast<unsigned>(offset),false);
    for(const auto& l:pending)t.levels.push_back({l.w,l.h,l.pitch,data+l.offset});
    t.wrap_s=address&15;t.wrap_t=(address>>8)&15;
    t.gamma=(address>>20)&15; // sRGB decode mask over input channels A,R,G,B (bits 0..3)
    if((address>>12)&15)throw std::runtime_error("RSX signed/unsigned texture remap is unsupported");
    t.min=(filter>>16)&7;t.mag=(filter>>24)&7;
    if(t.min<1 || t.min>6 || (t.mag!=1 && t.mag!=2))throw std::runtime_error("unsupported RSX texture filter");
    std::int32_t bias=filter&0x1fff;if(bias&0x1000)bias-=0x2000;
    t.bias=bias/256.f;t.min_lod=((control0>>19)&0xfff)/256.f;t.max_lod=((control0>>7)&0xfff)/256.f;
    for(unsigned i=0;i<4;++i){t.remap_channel[i]=(control1>>(2*i))&3;t.remap_control[i]=(control1>>(8+2*i))&3;}
    const auto border_colour=reg.get(base+0x1c,0);
    t.border={((border_colour>>16)&255)/255.f,((border_colour>>8)&255)/255.f,(border_colour&255)/255.f,(border_colour>>24)/255.f};
    t.valid=true;return t;
}
static Vec4 texel(const Texture& t,const TextureLevel& l,int x,int y){
    // Returns R,G,B,A in [0,1]. Coordinates were wrapped into the level.
    if(t.format==0x85){
        const auto* p=l.data+std::uint64_t(y)*l.pitch+std::uint64_t(x)*4;
        return {p[1]/255.f,p[2]/255.f,p[3]/255.f,p[0]/255.f};
    }
    const unsigned bx=x/4,by=y/4,px=x%4,py=y%4;
    const unsigned block_bytes=t.format==0x86?8:16;
    const auto* b=l.data+std::uint64_t(by)*l.pitch+std::uint64_t(bx)*block_bytes;
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
static Vec4 filter_level(const Texture& t,unsigned level,float un,float vn,bool bilinear){
    const auto& l=t.levels[level];
    const float u=un*l.width,v=vn*l.height;
    auto fetch=[&](int x,int y){
        bool border=false;
        const int xx=wrap_coordinate(t.wrap_s,x,l.width,border),yy=wrap_coordinate(t.wrap_t,y,l.height,border);
        if(border)return t.border;
        auto c=texel(t,l,xx,yy);
        if(t.gamma){
            // Hypothesis: decode before filtering, like sRGB texture hardware.
            auto srgb=[](float x){return x>0.04045f?std::pow((x+0.055f)/1.055f,2.4f):x/12.92f;};
            static const unsigned channel[4]={3,0,1,2};
            for(unsigned i=0;i<4;++i)if(t.gamma&(1u<<i))c[channel[i]]=srgb(c[channel[i]]);
        }
        return c;
    };
    if(!bilinear)return fetch(int(std::floor(u)),int(std::floor(v)));
    const float fu=u-0.5f,fv=v-0.5f;const int x0=int(std::floor(fu)),y0=int(std::floor(fv));
    const float ax=fu-x0,ay=fv-y0;
    const auto c00=fetch(x0,y0),c10=fetch(x0+1,y0),c01=fetch(x0,y0+1),c11=fetch(x0+1,y0+1);
    Vec4 c;for(unsigned i=0;i<4;++i)c[i]=(c00[i]*(1-ax)+c10[i]*ax)*(1-ay)+(c01[i]*(1-ax)+c11[i]*ax)*ay;
    return c;
}
// lambda: log2 of the screen-space footprint in level-0 texels (-inf for none).
static Vec4 sample_texture(const Texture& t,float u,float v,float lambda=-std::numeric_limits<float>::infinity()){
    const auto& base=t.levels[0];
    if(t.unnormalized){u/=base.width;v/=base.height;}
    const float lod=std::min(t.max_lod,std::max(t.min_lod,lambda+t.bias));
    Vec4 c;
    if(!(lod>0))c=filter_level(t,0,u,v,t.mag==2);
    else{
        const unsigned last=unsigned(t.levels.size())-1;
        switch(t.min){
        case 1:case 2:c=filter_level(t,0,u,v,t.min==2);break;
        case 3:case 4:{const unsigned level=std::min(last,unsigned(std::lround(lod)));c=filter_level(t,level,u,v,t.min==4);break;}
        default:{
            const unsigned l0=std::min(last,unsigned(std::floor(lod))),l1=std::min(last,l0+1);const float f=lod-std::floor(lod);
            const auto c0=filter_level(t,l0,u,v,t.min==6),c1=filter_level(t,l1,u,v,t.min==6);
            for(unsigned i=0;i<4;++i)c[i]=c0[i]*(1-f)+c1[i]*f;
            break;}
        }
    }
    // Remap in A,R,G,B order.
    const std::array<float,4> in{c[3],c[0],c[1],c[2]};std::array<float,4> out{};
    for(unsigned i=0;i<4;++i)out[i]=t.remap_control[i]==2?in[t.remap_channel[i]]:t.remap_control[i]==1?1.f:0.f;
    return {out[1],out[2],out[3],out[0]};
}

// ---------------- Fragment program ----------------
struct FpInst { std::uint32_t d,s0,s1,s2; Vec4 constant; bool has_constant=false; };
static std::vector<FpInst> load_fragment_program(PpuState& s,const Reg& reg){
    const auto value=reg(0x8e4);const unsigned location=(value&3);
    if(location!=1 && location!=2)throw std::runtime_error("invalid RSX fragment program location");
    std::vector<FpInst> program;
    std::uint32_t offset=value&~3u;
    auto word=[&](std::uint32_t at){
        const auto w=static_cast<std::uint32_t>(s.memory.load(location_address(s.rsx,location-1,at,4),4));
        return (w<<16)|(w>>16);
    };
    for(unsigned n=0;n<4096;++n){
        FpInst inst{word(offset),word(offset+4),word(offset+8),word(offset+12),{},false};
        offset+=16;
        if(((inst.s0&3)==2) || ((inst.s1&3)==2) || ((inst.s2&3)==2)){
            for(unsigned i=0;i<4;++i)inst.constant[i]=bits_float(word(offset+4*i));
            inst.has_constant=true;offset+=16;
        }
        program.push_back(inst);
        if(inst.d&1)return program;
    }
    throw std::runtime_error("RSX fragment program has no end");
}
struct FpContext {
    std::array<std::array<std::uint32_t,4>,48> r{}; // raw R registers; H registers alias halves
    std::array<Vec4,15> inputs{};
    std::array<Vec4,2> cc{};
    bool killed=false;
    const PpuState* state=nullptr;std::array<Texture,16>* textures=nullptr;
    Vec4 read_r(unsigned i)const{Vec4 v;for(unsigned c=0;c<4;++c)v[c]=bits_float(r.at(i)[c]);return v;}
    Vec4 read_h(unsigned i)const{
        const auto& reg=r.at(i/2);const unsigned base=(i&1)*2;Vec4 v;
        for(unsigned c=0;c<4;++c){const auto word=reg[base+c/2];v[c]=half_float(static_cast<std::uint16_t>(c&1?word>>16:word));}
        return v;
    }
    void write(unsigned i,bool half,unsigned mask,const Vec4& v){
        if(!half){for(unsigned c=0;c<4;++c)if(mask&(1u<<c)){std::uint32_t b;std::memcpy(&b,&v[c],4);r.at(i)[c]=b;}return;}
        auto& reg=r.at(i/2);const unsigned base=(i&1)*2;
        for(unsigned c=0;c<4;++c)if(mask&(1u<<c)){
            auto& word=reg[base+c/2];const std::uint32_t h=float_half(v[c]);
            word=c&1?(word&0xffff)|(h<<16):(word&0xffff0000)|h;
        }
    }
};
static Vec4 fp_source(const FpInst& inst,unsigned n,const FpContext& ctx){
    const auto raw=n==0?inst.s0:n==1?inst.s1:inst.s2;
    const unsigned type=raw&3,index=(raw>>2)&63;const bool half=(raw>>8)&1;
    Vec4 v{};
    switch(type){
    case 0:v=half?ctx.read_h(index):ctx.read_r(index);break;
    case 1:{const unsigned attr=(inst.d>>13)&15;if(attr>14)throw std::runtime_error("invalid RSX FP input attribute");v=ctx.inputs[attr];break;}
    case 2:v=inst.constant;break;
    default:throw std::runtime_error("invalid RSX FP source register type");
    }
    Vec4 sw;for(unsigned i=0;i<4;++i)sw[i]=v[(raw>>(9+2*i))&3];
    const bool absolute=n==0?((raw>>29)&1):((raw>>18)&1);
    if(absolute)for(auto& x:sw)x=std::fabs(x);
    if((raw>>17)&1)for(auto& x:sw)x=-x;
    return sw;
}
// Executes one 2x2 quad in lockstep (lane 0..3 = (x,y),(x+1,y),(x,y+1),(x+1,y+1)).
// Killed lanes keep running as helpers so coarse derivatives stay defined.
static void run_fragment_quad(const std::vector<FpInst>& program,std::array<FpContext,4>& quad){
    for(const auto& inst:program){
        const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
        if(opcode==0){if(inst.d&1)return;continue;}
        const unsigned exec=(inst.s0>>18)&7; // bit0 lt, bit1 eq, bit2 gt
        if(!exec){if(inst.d&1)return;continue;}
        if((inst.s1>>19)&511)throw std::runtime_error("RSX FP source precision modifiers are unsupported");
        std::array<Vec4,4> result{};
        if(opcode==21 || opcode==22){
            std::array<Vec4,4> x;for(unsigned l=0;l<4;++l)x[l]=fp_source(inst,0,quad[l]);
            for(unsigned l=0;l<4;++l)for(unsigned i=0;i<4;++i)
                result[l][i]=opcode==21?x[1][i]-x[0][i]:x[2][i]-x[0][i];
        }else if(opcode==23 || opcode==24){
            if(!quad[0].textures)throw std::runtime_error("RSX FP texture fetch without bound textures");
            const unsigned unit=(inst.d>>17)&15;auto& t=(*quad[0].textures)[unit];
            if(!t.valid)throw std::runtime_error("RSX FP samples undecoded texture unit");
            std::array<Vec4,4> x;
            for(unsigned l=0;l<4;++l){x[l]=fp_source(inst,0,quad[l]);if(opcode==24){x[l][0]/=x[l][3];x[l][1]/=x[l][3];}}
            const float scale_u=t.unnormalized?1.f:float(t.levels[0].width),scale_v=t.unnormalized?1.f:float(t.levels[0].height);
            const float dudx=(x[1][0]-x[0][0])*scale_u,dvdx=(x[1][1]-x[0][1])*scale_v;
            const float dudy=(x[2][0]-x[0][0])*scale_u,dvdy=(x[2][1]-x[0][1])*scale_v;
            const float rho=std::max(std::sqrt(dudx*dudx+dvdx*dvdx),std::sqrt(dudy*dudy+dvdy*dvdy));
            const float lambda=rho>0?std::log2(rho):-std::numeric_limits<float>::infinity();
            for(unsigned l=0;l<4;++l){
                result[l]=sample_texture(t,x[l][0],x[l][1],lambda);
                if((inst.d>>21)&1)for(auto& c:result[l])c=2*c-1; // _bx2
            }
        }else for(unsigned l=0;l<4;++l){
            auto& ctx=quad[l];
            auto a=[&]{return fp_source(inst,0,ctx);};
            auto b=[&]{return fp_source(inst,1,ctx);};
            auto c=[&]{return fp_source(inst,2,ctx);};
            Vec4& v=result[l];
            switch(opcode){
            case 1:v=a();break;
            case 2:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=x[i]*y[i];break;}
            case 3:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=x[i]+y[i];break;}
            case 4:{auto x=a(),y=b(),z=c();for(unsigned i=0;i<4;++i)v[i]=std::fma(x[i],y[i],z[i]);break;}
            case 5:{auto x=a(),y=b();float t=x[0]*y[0]+x[1]*y[1]+x[2]*y[2];v={t,t,t,t};break;}
            case 6:{auto x=a(),y=b();float t=x[0]*y[0]+x[1]*y[1]+x[2]*y[2]+x[3]*y[3];v={t,t,t,t};break;}
            case 8:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=std::min(x[i],y[i]);break;}
            case 9:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=std::max(x[i],y[i]);break;}
            case 10:case 11:case 12:case 13:case 14:case 15:{
                auto x=a(),y=b();
                for(unsigned i=0;i<4;++i){bool t=false;
                    switch(opcode){case 10:t=x[i]<y[i];break;case 11:t=x[i]>=y[i];break;case 12:t=x[i]<=y[i];break;
                    case 13:t=x[i]>y[i];break;case 14:t=x[i]!=y[i];break;case 15:t=x[i]==y[i];break;}
                    v[i]=t?1.f:0.f;}
                break;}
            case 16:{auto x=a();for(unsigned i=0;i<4;++i)v[i]=x[i]-std::floor(x[i]);break;}
            case 17:{auto x=a();for(unsigned i=0;i<4;++i)v[i]=std::floor(x[i]);break;}
            case 18:break;
            case 26:{float t=1.f/a()[0];v={t,t,t,t};break;}
            case 27:{float t=1.f/std::sqrt(std::fabs(a()[0]));v={t,t,t,t};break;}
            case 28:{float t=std::exp2(a()[0]);v={t,t,t,t};break;}
            case 29:{float t=std::log2(std::fabs(a()[0]));v={t,t,t,t};break;}
            case 31:{auto x=a(),y=b(),z=c();for(unsigned i=0;i<4;++i)v[i]=z[i]+x[i]*(y[i]-z[i]);break;}
            case 34:{float t=std::cos(a()[0]);v={t,t,t,t};break;}
            case 35:{float t=std::sin(a()[0]);v={t,t,t,t};break;}
            case 38:{float t=std::pow(a()[0],b()[0]);v={t,t,t,t};break;}
            case 56:{auto x=a(),y=b();float t=x[0]*y[0]+x[1]*y[1];v={t,t,t,t};break;}
            case 58:{auto x=a();float y=b()[0];for(unsigned i=0;i<4;++i)v[i]=x[i]/y;break;}
            default:throw std::runtime_error("unsupported RSX FP opcode "+std::to_string(opcode));
            }
        }
        for(unsigned l=0;l<4;++l){
            auto& ctx=quad[l];Vec4 v=result[l];
            std::array<bool,4> pass{true,true,true,true};
            if(exec!=7){
                const auto& cc=ctx.cc[(inst.s0>>31)&1];
                for(unsigned i=0;i<4;++i){
                    const float x=cc[(inst.s0>>(21+2*i))&3];
                    pass[i]=(x<0 && (exec&1)) || (x==0 && (exec&2)) || (x>0 && (exec&4));
                }
            }
            if(opcode==18){if(pass[0]||pass[1]||pass[2]||pass[3])ctx.killed=true;continue;}
            static const float scales[8]={1,2,4,8,1,0.5f,0.25f,0.125f};
            const unsigned scale=(inst.s1>>28)&7;
            if(scale==4)throw std::runtime_error("invalid RSX FP result scale");
            for(auto& x:v)x*=scales[scale];
            const bool half_dest=(inst.d>>7)&1,no_dest=(inst.d>>30)&1;
            if(inst.d>>31)for(auto& x:v)x=std::min(1.f,std::max(0.f,x));
            else if(!no_dest){
                // Precision clamp, with RPCS3's exempt opcodes (NRM MAX MIN COS SIN REFL FRC LIT LIF LG2).
                const bool exempt=opcode==57||opcode==9||opcode==8||opcode==34||opcode==35||opcode==54||opcode==16||opcode==30||opcode==60||opcode==29||
                    (opcode==1 && half_dest && ((inst.s0>>8)&1) && (inst.s0&3)==0);
                const unsigned prec=(inst.d>>22)&3;
                if(!exempt && !(prec==1 && !half_dest)){
                    const float limit=prec==2?2.f:prec==3?1.f:prec==1?65504.f:0.f;
                    if(limit)for(auto& x:v)x=std::min(limit,std::max(-limit,x));
                }
            }
            unsigned mask=(inst.d>>9)&15;
            for(unsigned i=0;i<4;++i)if(!pass[i])mask&=~(1u<<i);
            const unsigned dest=(inst.d>>1)&63;
            if(!no_dest)ctx.write(dest,half_dest,mask,v);
            if((inst.d>>8)&1){
                // CC receives the value as stored in the destination (rounded for H).
                const Vec4 stored=no_dest?v:half_dest?ctx.read_h(dest):ctx.read_r(dest);
                auto& cc=ctx.cc[(inst.s0>>30)&1];
                for(unsigned i=0;i<4;++i)if(mask&(1u<<i))cc[i]=stored[i];
            }
        }
        if(inst.d&1)return;
    }
}
// Single-pixel form: four identical lanes, so derivatives are zero.
[[maybe_unused]] static void run_fragment_program(const std::vector<FpInst>& program,FpContext& ctx){
    std::array<FpContext,4> quad{ctx,ctx,ctx,ctx};
    run_fragment_quad(program,quad);
    ctx=quad[0];
}

// ---------------- Surfaces ----------------
struct Target {
    std::uint32_t offset=0,pitch=0;const NativeRsx::Tile* tile=nullptr;std::uint8_t* base=nullptr;std::uint32_t base_offset=0;
    std::uint8_t* at(unsigned x,unsigned y)const{
        const auto linear=offset+std::uint64_t(y)*pitch+x*4;
        const auto physical=tile?rsx_tiled_offset(*tile,static_cast<std::uint32_t>(linear-tile->offset)):static_cast<std::uint32_t>(linear);
        return base+(physical-base_offset);
    }
};
static Target bind_target(PpuState& s,std::uint32_t context,std::uint32_t offset,std::uint32_t pitch,unsigned width,unsigned height){
    auto& gpu=s.rsx;
    if(context!=0xfeed0000)throw std::runtime_error("RSX draw supports local VRAM surfaces only");
    const std::uint64_t span=std::uint64_t(height-1)*pitch+std::uint64_t(width)*4;
    if(pitch<width*4 || pitch%4 || offset%4 || offset>gpu.local_size || span>gpu.local_size-offset)
        throw std::runtime_error("RSX draw surface outside VRAM/IO aperture or invalid pitch");
    Target t;t.offset=offset;t.pitch=pitch;t.base_offset=offset;
    std::uint64_t bytes=span;
    for(const auto& tile:gpu.tiles)if(tile.bound && !tile.location && offset<std::uint64_t(tile.offset)+tile.size && tile.offset<offset+span){
        if(t.tile || offset<tile.offset || offset+span>std::uint64_t(tile.offset)+tile.size || tile.pitch!=pitch)
            throw std::runtime_error("unsupported RSX draw surface tile layout");
        t.tile=&tile;t.base_offset=tile.offset;bytes=tile.size;
    }
    t.base=s.memory.access(UINT64_C(0xc0000000)+t.base_offset,static_cast<std::uint32_t>(bytes),true);
    return t;
}

struct ShadedVertex { Vec4 clip; std::array<Vec4,16> o; };
static bool compare(unsigned func,float a,float b){
    switch(func){
    case 0x200:return false;case 0x201:return a<b;case 0x202:return a==b;case 0x203:return a<=b;
    case 0x204:return a>b;case 0x205:return a!=b;case 0x206:return a>=b;case 0x207:return true;
    default:throw std::runtime_error("invalid RSX comparison function "+std::to_string(func));
    }
}
static float blend_factor(unsigned f,const Vec4& src,const Vec4& dst,const Vec4& constant,unsigned c){
    switch(f){
    case 0:return 0;case 1:return 1;
    case 0x300:return src[c];case 0x301:return 1-src[c];case 0x302:return src[3];case 0x303:return 1-src[3];
    case 0x304:return dst[3];case 0x305:return 1-dst[3];case 0x306:return dst[c];case 0x307:return 1-dst[c];
    case 0x308:return c==3?1.f:std::min(src[3],1-dst[3]);
    case 0x8001:return constant[c];case 0x8002:return 1-constant[c];case 0x8003:return constant[3];case 0x8004:return 1-constant[3];
    default:throw std::runtime_error("unsupported RSX blend factor "+std::to_string(f));
    }
}
} // namespace rsxdraw

// Returns true when reg is a SET_VERTEX_DATA*_M register write it handled.
static bool rsx_vertex_data_register(NativeRsx& gpu,std::uint32_t reg,std::uint32_t value){
    unsigned attr=0,component=0,count=0,type=0;
    if(reg>=0x1940 && reg<0x1980){attr=(reg-0x1940)/4;count=4;type=1;}
    else if(reg>=0x1e40 && reg<0x1e80){attr=(reg-0x1e40)/4;count=1;}
    else if(reg>=0x1880 && reg<0x1900){attr=(reg-0x1880)/8;component=(reg/4)%2;count=2;}
    else if(reg>=0x1500 && reg<0x1600){attr=(reg-0x1500)/16;component=(reg/4)%4;count=3;if(component==3)return true;}
    else if(reg>=0x1c00 && reg<0x1d00){attr=(reg-0x1c00)/16;component=(reg/4)%4;count=4;}
    else return false;
    auto& entry=gpu.draw.immediate[attr];
    entry.count=count;entry.type=type;entry.words[component]=value;
    // Inside begin/end, completing ATTR0 emits a vertex from current registers.
    if(gpu.draw.primitive && attr==0 && component+1==(count==3?3u:count)){
        if(!gpu.draw.ranges.empty())throw std::runtime_error("mixed RSX immediate and array draws");
        gpu.draw.immediate_vertices.push_back(gpu.draw.immediate);
    }
    return true;
}
static void rsx_execute_draw(PpuState& s){
    using namespace rsxdraw;
    auto& gpu=s.rsx;auto& draw=gpu.draw;auto& m=s.memory;const Reg reg{gpu};
    if(!(reg.get(0x1e98,0x01000000)&0x01000000))throw std::runtime_error("conditional RSX render enable is unsupported");
    // ---- indices
    std::vector<std::uint32_t> indices;
    const auto base_index=reg.get(0x173c,0);
    if(draw.indexed){
        const auto dma=reg(0x1820),offset=reg(0x181c);
        const unsigned type=(dma>>4)&15,width=type==0?4:type==1?2:0;
        if(!width || (dma&15)>1)throw std::runtime_error("unsupported RSX index array format/location");
        if(reg.get(0x1dac,0))throw std::runtime_error("RSX primitive restart is unsupported");
        for(const auto& [first,count]:draw.ranges){
            const auto address=location_address(gpu,dma&15,offset+first*width,count*width);
            for(unsigned i=0;i<count;++i)indices.push_back(static_cast<std::uint32_t>(m.load(address+i*width,width))+base_index);
        }
    }else if(!draw.immediate_vertices.empty()){
        for(std::uint32_t i=0;i<draw.immediate_vertices.size();++i)indices.push_back(i);
    }else for(const auto& [first,count]:draw.ranges)for(unsigned i=0;i<count;++i)indices.push_back(first+i);
    // ---- vertices
    const auto input_mask=reg(0x1ff0),start=reg.get(0x1ea0,0);
    std::map<std::uint32_t,ShadedVertex> cache;
    auto vertex=[&](std::uint32_t index)->const ShadedVertex&{
        auto it=cache.find(index);if(it!=cache.end())return it->second;
        std::array<Vec4,16> in{};
        for(unsigned a=0;a<16;++a)in[a]=!((input_mask>>a)&1)?Vec4{0,0,0,1}:
            draw.immediate_vertices.empty()?fetch_attribute(s,reg,a,index):register_attribute(draw.immediate_vertices[index][a]);
        const auto out=run_vertex_program(gpu,start,in);
        ShadedVertex v;v.clip=out.o[0];v.o=out.o;
        return cache.emplace(index,v).first->second;
    };
    // ---- primitive assembly
    std::vector<std::array<std::uint32_t,3>> triangles;
    const auto n=indices.size();
    switch(draw.primitive){
    case 5:for(std::size_t i=0;i+2<n;i+=3)triangles.push_back({indices[i],indices[i+1],indices[i+2]});break;
    case 6:for(std::size_t i=0;i+2<n;++i)triangles.push_back(i&1?std::array<std::uint32_t,3>{indices[i+1],indices[i],indices[i+2]}:std::array<std::uint32_t,3>{indices[i],indices[i+1],indices[i+2]});break;
    case 7:case 10:for(std::size_t i=1;i+1<n;++i)triangles.push_back({indices[0],indices[i],indices[i+1]});break;
    case 8:for(std::size_t i=0;i+3<n;i+=4){triangles.push_back({indices[i],indices[i+1],indices[i+2]});triangles.push_back({indices[i],indices[i+2],indices[i+3]});}break;
    case 9:for(std::size_t i=0;i+3<n;i+=2){triangles.push_back({indices[i],indices[i+1],indices[i+3]});triangles.push_back({indices[i],indices[i+3],indices[i+2]});}break;
    default:throw std::runtime_error("unsupported RSX primitive type "+std::to_string(draw.primitive));
    }
    // ---- fixed state
    const auto surface_format=reg(0x208),color_target=reg(0x220);
    const unsigned color_format=surface_format&31,depth_format=(surface_format>>5)&7;
    if(((surface_format>>8)&15)!=1 || ((surface_format>>12)&15))throw std::runtime_error("unsupported RSX draw surface type or antialiasing");
    if(color_target!=1 && color_target!=0)throw std::runtime_error("unsupported RSX draw color target set "+std::to_string(color_target));
    if(color_target && color_format!=8)throw std::runtime_error("unsupported RSX draw color format "+std::to_string(color_format));
    const unsigned width=reg(0x200)>>16,height=reg(0x204)>>16,clip_x=reg(0x200)&0xffff,clip_y=reg(0x204)&0xffff;
    if(!width || !height || width>4096 || height>4096)throw std::runtime_error("invalid RSX draw surface size");
    if(reg.get(0x328,0) || reg.get(0x348,0))throw std::runtime_error("RSX stencil test is unsupported");
    if(reg.get(0x374,0))throw std::runtime_error("RSX logic op is unsupported");
    if(reg.get(0x380,0))throw std::runtime_error("RSX depth bounds test is unsupported");
    if(reg.get(0x1828,0x1b02)!=0x1b02 || reg.get(0x182c,0x1b02)!=0x1b02)throw std::runtime_error("RSX non-fill polygon mode is unsupported");
    if(reg.get(0x142c,0))throw std::runtime_error("RSX two-sided lighting is unsupported");
    const auto shader_control=reg(0x1d60);
    if(shader_control&0xe)throw std::runtime_error("RSX fragment depth export is unsupported");
    if(reg.get(0x1d88,0)&0xff0000 || (reg.get(0x1d88,0)>>12)&15)throw std::runtime_error("unsupported RSX shader window origin/pixel center");
    const bool depth_test=reg.get(0xa74,0)&1,depth_write=reg.get(0xa70,0)&1;
    const bool use_depth=depth_test || depth_write;
    if(use_depth && depth_format!=2)throw std::runtime_error("unsupported RSX depth format for draw");
    const bool alpha_test=reg.get(0x304,0)&1,blend=reg.get(0x310,0)&1;
    if(reg.get(0x36c,0))throw std::runtime_error("RSX MRT blend is unsupported");
    const auto color_mask=reg.get(0x324,0x01010101);
    const unsigned shade_flat=reg.get(0x368,0x1d01)==0x1d00;
    const bool cull=reg.get(0x183c,0)&1;const auto cull_face=reg.get(0x1830,0x405),front_face=reg.get(0x1834,0x901);
    const float vx=bits_float(reg(0xa20)),vy=bits_float(reg(0xa24)),vz=bits_float(reg(0xa28));
    const float sx=bits_float(reg(0xa30)),sy=bits_float(reg(0xa34)),sz=bits_float(reg(0xa38));
    const float clip_min=bits_float(reg.get(0x394,0)),clip_max=bits_float(reg.get(0x398,0x3f800000));
    unsigned x0=std::max(clip_x,reg.get(0x8c0,0x10000000)&0xffff),y0=std::max(clip_y,reg.get(0x8c4,0x10000000)&0xffff);
    unsigned x1=std::min({clip_x+width,(reg.get(0x8c0,0x10000000)&0xffff)+(reg.get(0x8c0,0x10000000)>>16),width});
    unsigned y1=std::min({clip_y+height,(reg.get(0x8c4,0x10000000)&0xffff)+(reg.get(0x8c4,0x10000000)>>16),height});
    if(x0>=x1 || y0>=y1){draw.ranges.clear();draw.immediate_vertices.clear();++draw.draws;return;}
    std::vector<FpInst> fp;if(color_target || alpha_test)fp=load_fragment_program(s,reg);
    std::array<Texture,16> textures{};
    for(const auto& inst:fp){
        const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
        if(opcode==23 || opcode==24){const unsigned unit=(inst.d>>17)&15;if(!textures[unit].valid)textures[unit]=decode_texture(s,reg,unit);}
        else if(opcode==25 || opcode==47 || opcode==49 || opcode==51 || opcode==52)throw std::runtime_error("unsupported RSX FP texture opcode "+std::to_string(opcode));
    }
    Target color,depth;
    if(color_target)color=bind_target(s,reg(0x194),reg(0x210),reg(0x20c),width,height);
    if(use_depth)depth=bind_target(s,reg(0x198),reg(0x214),reg(0x22c),width,height);
    const unsigned output_reg=(shader_control&0x40)?0:0; // R0 or H0 (same index 0)
    const unsigned depth_func=depth_test?reg(0xa6c):0x207,alpha_func=alpha_test?reg(0x308):0x207;
    const float alpha_ref=float(reg.get(0x30c,0)&255)/255.f;
    const auto blend_sf=blend?reg(0x314):0,blend_df=blend?reg(0x318):0,blend_eq=blend?reg(0x320):0,blend_colour=reg.get(0x31c,0);
    const bool half_output=!(shader_control&0x40);
    // A single MOV from an inline constant cannot depend on interpolated inputs
    // or neighbouring lanes. Still use the interpreter for predicates, masks,
    // precision, half-register aliasing and errors, once at the first covered
    // quad. Geometry and all per-pixel fixed-function operations remain below.
    const bool constant_mov=fp.size()==1 && (((fp[0].d>>24)&63)|((fp[0].s1>>31)<<6))==1 && (fp[0].s0&3)==2;
    std::optional<Vec4> constant_output;
    // ---- per triangle
    std::uint64_t shaded=0;
    for(const auto& tri:triangles){
        std::array<const ShadedVertex*,3> v{&vertex(tri[0]),&vertex(tri[1]),&vertex(tri[2])};
        for(auto* p:v)if(!(p->clip[3]>0))throw std::runtime_error("RSX draw requires near-plane clipping (w<=0)");
        std::array<float,3> X,Y,Z,W;
        for(unsigned i=0;i<3;++i){
            const float iw=1.f/v[i]->clip[3];
            X[i]=v[i]->clip[0]*iw*sx+vx;Y[i]=v[i]->clip[1]*iw*sy+vy;Z[i]=v[i]->clip[2]*iw*sz+vz;W[i]=iw;
        }
        const float area=(X[1]-X[0])*(Y[2]-Y[0])-(X[2]-X[0])*(Y[1]-Y[0]);
        if(area==0)continue;
        // Window y grows downward; a counter-clockwise triangle in GL window
        // space (y up) has negative area here.
        const bool ccw=area<0;
        const bool front=front_face==0x901?ccw:!ccw;
        if(cull){
            if(cull_face==0x408 || (cull_face==0x404 && front) || (cull_face==0x405 && !front))continue;
        }
        ++draw.triangles;
        const int minx=std::max<int>(x0,int(std::floor(std::min({X[0],X[1],X[2]})))),maxx=std::min<int>(int(x1)-1,int(std::ceil(std::max({X[0],X[1],X[2]}))));
        const int miny=std::max<int>(y0,int(std::floor(std::min({Y[0],Y[1],Y[2]})))),maxy=std::min<int>(int(y1)-1,int(std::ceil(std::max({Y[0],Y[1],Y[2]}))));
        const float sign=area>0?1.f:-1.f;
        auto edge=[&](unsigned a,unsigned b,float px,float py){return ((X[b]-X[a])*(py-Y[a])-(Y[b]-Y[a])*(px-X[a]))*sign;};
        auto top_left=[&](unsigned a,unsigned b){
            const float dx=(X[b]-X[a])*sign,dy=(Y[b]-Y[a])*sign;
            return (dy==0 && dx>0) || dy<0; // top edge or left edge in y-down space
        };
        const bool tl0=top_left(1,2),tl1=top_left(2,0),tl2=top_left(0,1);
        for(int qy=miny&~1;qy<=maxy;qy+=2)for(int qx=minx&~1;qx<=maxx;qx+=2){
            std::array<bool,4> covered{};std::array<float,4> lb0{},lb1{},lb2{};
            bool any=false;
            for(unsigned l=0;l<4;++l){
                const int px=qx+(l&1),py=qy+(l>>1);
                const float cx=px+0.5f,cy=py+0.5f;
                const float e0=edge(1,2,cx,cy),e1=edge(2,0,cx,cy),e2=edge(0,1,cx,cy);
                const float sum=e0+e1+e2;lb0[l]=e0/sum;lb1[l]=e1/sum;lb2[l]=e2/sum;
                bool inside=px>=minx && px<=maxx && py>=miny && py<=maxy && e0>=0 && e1>=0 && e2>=0 &&
                    !((e0==0 && !tl0) || (e1==0 && !tl1) || (e2==0 && !tl2));
                covered[l]=inside;any|=inside;
            }
            if(!any)continue;
            std::optional<std::array<FpContext,4>> quad;std::array<float,4> lz{};
            if(!constant_mov)quad.emplace();
            for(unsigned l=0;l<4;++l){
                const float b0=lb0[l],b1=lb1[l],b2=lb2[l];
                lz[l]=b0*Z[0]+b1*Z[1]+b2*Z[2];
                if(constant_mov)continue;
                const float iw=b0*W[0]+b1*W[1]+b2*W[2];
                const float p0=b0*W[0]/iw,p1=b1*W[1]/iw,p2=b2*W[2]/iw;
                auto varying=[&](unsigned o){
                    if(shade_flat && (o==1 || o==2))return v[2]->o[o];
                    Vec4 r;for(unsigned c=0;c<4;++c)r[c]=p0*v[0]->o[o][c]+p1*v[1]->o[o][c]+p2*v[2]->o[o][c];return r;
                };
                auto& ctx=(*quad)[l];ctx.state=&s;ctx.textures=&textures;
                ctx.inputs[0]={qx+(l&1)+0.5f,qy+(l>>1)+0.5f,lz[l],iw};
                ctx.inputs[1]=varying(1);ctx.inputs[2]=varying(2);
                {auto f=varying(5);ctx.inputs[3]={f[0],0,0,0};}
                for(unsigned t=0;t<8;++t)ctx.inputs[4+t]=varying(7+t);
                ctx.inputs[12]=varying(15);ctx.inputs[13]=varying(6);
                ctx.inputs[14]={front?1.f:-1.f,0,0,0};
            }
            if(constant_mov){
                if(!constant_output){
                    FpContext ctx;run_fragment_program(fp,ctx);
                    constant_output=half_output?ctx.read_h(output_reg):ctx.read_r(output_reg);
                }
            }else if(!fp.empty())run_fragment_quad(fp,*quad);
            for(unsigned l=0;l<4;++l){
            if(!covered[l])continue;
            const int px=qx+(l&1),py=qy+(l>>1);
            const float z=lz[l];
            if(z<clip_min || z>clip_max)continue;
            std::uint32_t stored_depth=0;std::uint8_t* depth_ptr=nullptr;
            if(use_depth){
                depth_ptr=depth.at(px,py);
                stored_depth=(std::uint32_t(depth_ptr[0])<<16)|(std::uint32_t(depth_ptr[1])<<8)|depth_ptr[2];
            }
            const std::uint32_t z24=static_cast<std::uint32_t>(std::lround(std::min(1.f,std::max(0.f,z))*16777215.f));
            if(depth_test && !compare(depth_func,float(z24),float(stored_depth)))continue;
            Vec4 out{0,0,0,0};
            if(constant_mov)out=*constant_output;
            else if(!fp.empty()){
                if((*quad)[l].killed)continue;
                out=half_output?(*quad)[l].read_h(output_reg):(*quad)[l].read_r(output_reg);
            }
            if(alpha_test){
                if(!compare(alpha_func,std::min(1.f,std::max(0.f,out[3])),alpha_ref))continue;
            }
            if(use_depth && depth_write){
                m.mark_write(depth_ptr,3);depth_ptr[0]=z24>>16;depth_ptr[1]=z24>>8;depth_ptr[2]=z24;
            }
            if(color_target){
                auto* p=color.at(px,py);
                Vec4 src;for(unsigned c=0;c<4;++c)src[c]=std::min(1.f,std::max(0.f,out[c]));
                Vec4 dst{p[1]/255.f,p[2]/255.f,p[3]/255.f,p[0]/255.f};
                Vec4 result=src;
                if(blend){
                    const auto sf=blend_sf,df=blend_df,eq=blend_eq,bc=blend_colour;
                    const Vec4 constant{((bc>>16)&255)/255.f,((bc>>8)&255)/255.f,(bc&255)/255.f,(bc>>24)/255.f};
                    for(unsigned c=0;c<4;++c){
                        const unsigned sfc=c==3?sf>>16:sf&0xffff,dfc=c==3?df>>16:df&0xffff,e=c==3?eq>>16:eq&0xffff;
                        const float a=src[c]*blend_factor(sfc,src,dst,constant,c),b=dst[c]*blend_factor(dfc,src,dst,constant,c);
                        float r;
                        switch(e){case 0x8006:r=a+b;break;case 0x800a:r=a-b;break;case 0x800b:r=b-a;break;
                                  case 0x8007:r=std::min(src[c],dst[c]);break;case 0x8008:r=std::max(src[c],dst[c]);break;
                                  default:throw std::runtime_error("unsupported RSX blend equation "+std::to_string(e));}
                        result[c]=std::min(1.f,std::max(0.f,r));
                    }
                }
                auto byte=[](float f){return static_cast<std::uint8_t>(std::lround(f*255.f));};
                m.mark_write(p,4);
                if(color_mask&0x01000000)p[0]=byte(result[3]);
                if(color_mask&0x00010000)p[1]=byte(result[0]);
                if(color_mask&0x00000100)p[2]=byte(result[1]);
                if(color_mask&0x00000001)p[3]=byte(result[2]);
            }
            ++shaded;
            }
        }
    }
    draw.pixels+=shaded;++draw.draws;draw.ranges.clear();draw.immediate_vertices.clear();
    if(gpu.trace || draw.draws<=16 || draw.draws%256==0)
        std::fprintf(stderr,"native RSX draw=%llu primitive=%u vertices=%zu triangles=%zu pixels=%llu\n",
            static_cast<unsigned long long>(draw.draws),draw.primitive,indices.size(),triangles.size(),static_cast<unsigned long long>(shaded));
}
#pragma GCC pop_options
'''

REFERENCE_TILE = r'''
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

'''

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_rsx import GAME_VP


def native_source(body):
    """Compile frozen and current renderers into one process / host allocation."""
    source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
    source = source.replace('int main(int argc, char** argv)',
                            'int original_main(int argc, char** argv)')
    reference = REFERENCE_DRAW.replace('rsxdraw', 'reference_draw')
    reference = reference.replace('static bool rsx_vertex_data_register(',
                                  '[[maybe_unused]] static bool reference_vertex_data_register(')
    reference = reference.replace('rsx_execute_draw(', 'reference_execute_draw(')
    reference = reference.replace('rsx_tiled_offset(', 'reference_tiled_offset(')
    source += REFERENCE_TILE.replace('rsx_tiled_offset(', 'reference_tiled_offset(')
    source += reference
    vp = ','.join(f'{word}u' for word in GAME_VP)
    return source + '\n#include <cassert>\n' + HARNESS.replace('GAME_VP_WORDS', vp) + body


HARNESS = r'''
static void setup_draw(PpuState& s,unsigned width,unsigned height,unsigned pitch,bool tiled){
    auto& gpu=s.rsx;auto& m=s.memory;auto& R=gpu.registers;
    m.map(0x70000000,0x10000,6,nullptr,0);gpu.io[0]=0x70000000;
    m.map(0xc0000000,0x800000,6,nullptr,0);gpu.local_size=0x800000;
    const unsigned vp[]={GAME_VP_WORDS};
    for(unsigned i=0;i<sizeof(vp)/sizeof(*vp);++i){gpu.transform_program[i]=vp[i];gpu.transform_program_written[i]=true;}
    auto bits=[](float f){std::uint32_t w;std::memcpy(&w,&f,4);return w;};
    const float halfw=width/2.f,halfh=height/2.f;
    const float constants[]={halfw,halfh,halfw,halfh};
    for(unsigned i=0;i<4;++i){gpu.transform_constants[24+i]=bits(constants[i]);gpu.transform_constants_written[24+i]=true;}
    R[0x1ff0]=3;R[0x1740]=0x1042;R[0x1680]=0x80002000;
    R[0x1744]=0x822;R[0x1684]=0x80002100;R[0x1ea0]=0;
    R[0x208]=0x148;R[0x200]=width<<16;R[0x204]=height<<16;R[0x220]=1;
    R[0x194]=R[0x198]=0xfeed0000;R[0x210]=0x100000;R[0x214]=0x500000;
    R[0x20c]=R[0x22c]=pitch;R[0x1d60]=0x40;R[0x8e4]=0x1001;
    R[0xa20]=R[0xa30]=bits(halfw);R[0xa24]=bits(halfh);R[0xa34]=bits(-halfh);
    R[0xa28]=0;R[0xa38]=bits(1.f);
    const float positions[4][2]={{0,0},{float(width),0},{float(width),float(height)},{0,float(height)}};
    const float uv[4][2]={{-0.25f,-0.125f},{1.25f,-0.125f},{1.25f,1.125f},{-0.25f,1.125f}};
    for(unsigned i=0;i<4;++i){
        for(unsigned c=0;c<4;++c)m.store(0x70002000+i*16+c*4,4,bits(c<2?positions[i][c]:c==2?0.25f:1.f));
        for(unsigned c=0;c<2;++c)m.store(0x70002100+i*8+c*4,4,bits(uv[i][c]));
    }
    auto fpword=[&](unsigned i,unsigned w){m.store(0xc0001000+i*4,4,(w<<16)|(w>>16));};
    fpword(0,(1u<<24)|(15u<<9)|1u);fpword(1,2u|(0xe4u<<9)|(7u<<18));fpword(2,0);fpword(3,0);
    const float color[]={1.f,0.5f,0.25f,0.5f};for(unsigned i=0;i<4;++i)fpword(4+i,bits(color[i]));
    if(tiled){for(unsigned i=0;i<2;++i){auto& tile=gpu.tiles[i];tile.bound=true;tile.offset=i?0x500000:0x100000;tile.size=0x300000;tile.pitch=pitch;tile.bank=i;}}
    gpu.draw.primitive=8;gpu.draw.ranges={{0,4}};
}

static void observe(GuestMemory& m,unsigned mode){
    for(const auto& r:m.regions){
        const auto first=reinterpret_cast<std::uintptr_t>(r.bytes->data())/128;
        const auto last=(reinterpret_cast<std::uintptr_t>(r.bytes->data())+r.bytes->size()-1)/128;
        for(auto line=first;line<=last;++line)if(mode==1 || (mode==2 && line%13==0))m.write_versions[line]=7+line%19;
    }
}
struct DrawSnapshot {
    std::vector<std::vector<std::uint8_t>> bytes;
    std::unordered_map<std::uintptr_t,std::uint64_t> versions;
    RsxDrawState draw;
    std::string error;
    std::size_t last_region=0;
};
static DrawSnapshot snapshot(PpuState& s,const std::string& error=""){
    DrawSnapshot result;for(const auto& r:s.memory.regions)result.bytes.emplace_back(r.bytes->begin(),r.bytes->end());
    result.versions.insert(s.memory.write_versions.begin(),s.memory.write_versions.end());result.draw=s.rsx.draw;result.error=error;
    result.last_region=s.memory.last_region;return result;
}
static std::string execute(PpuState& s,bool reference){
    try{if(reference)reference_execute_draw(s);else rsx_execute_draw(s);return "";}
    catch(const std::runtime_error& e){return e.what();}
    catch(const std::out_of_range& e){return std::string("std::out_of_range: ")+e.what();}
}
static std::string differential(PpuState& s,unsigned case_id,unsigned reference_tile_size=0,bool snapshot_feedback=false){
    // 0f7addf intentionally changed feedback to a pre-draw texture snapshot.
    // Keep the frozen renderer untouched: only these explicit linear ARGB8 8x8
    // fixtures give its sampler an independent, immutable copy in spare VRAM.
    // Color/depth aliasing and the actual renderer's aliased input stay intact.
    constexpr unsigned snapshot_offset=0x400000,snapshot_bytes=448; // 8x8 + 4x4 + 2x2, each with pitch 32
    const unsigned texture_offset=snapshot_feedback?s.rsx.registers.at(0x1a00):0;
    if(snapshot_feedback){auto& bytes=*s.memory.regions[1].bytes;
        assert(s.memory.regions[1].base==0xc0000000 && s.rsx.registers[0x1840]==32 && s.rsx.registers[0x1a18]==0x80008);
        assert(((s.rsx.registers[0x1a04]>>8)&255)==0xa5 && (s.rsx.registers[0x1a04]&3)==1);
        assert(texture_offset+snapshot_bytes<=bytes.size() && snapshot_offset+snapshot_bytes<=bytes.size());
        std::copy_n(bytes.begin()+texture_offset,snapshot_bytes,bytes.begin()+snapshot_offset);
    }
    const auto initial=snapshot(s);const auto last=s.memory.last_region;
    // The frozen binder requires one backing for the entire tile. For the
    // adjacent-region case only, bound its reference view to the first MiB:
    // all shaded addresses fit there. The current draw retains the full tile
    // spanning both guest regions. Account for the continuation's bind-time
    // write mark explicitly, including any shared host reservation line.
    const auto tile_size=s.rsx.tiles[0].size;
    if(reference_tile_size)s.rsx.tiles[0].size=reference_tile_size;
    if(snapshot_feedback)s.rsx.registers[0x1a00]=snapshot_offset;
    const auto error=execute(s,true);
    if(snapshot_feedback)s.rsx.registers[0x1a00]=texture_offset;
    if(reference_tile_size){
        assert(error.empty() && s.memory.regions[2].base==0xc0200000);
        s.memory.mark_write(s.memory.regions[2].bytes->data(),tile_size-reference_tile_size);
    }
    const auto expected=snapshot(s,error);
    s.rsx.tiles[0].size=tile_size;
    for(unsigned i=0;i<initial.bytes.size();++i)s.memory.regions[i].bytes->assign(initial.bytes[i].begin(),initial.bytes[i].end());
    s.memory.write_versions=decltype(s.memory.write_versions){};
    for(const auto& entry:initial.versions)s.memory.write_versions[entry.first]=entry.second;
    s.rsx.draw=initial.draw;s.memory.last_region=last;
    const auto actual_error=execute(s,false);const auto actual=snapshot(s,actual_error);
    if(actual.bytes!=expected.bytes || actual.versions!=expected.versions || actual.error!=expected.error ||
       actual.draw.draws!=expected.draw.draws || actual.draw.triangles!=expected.draw.triangles || actual.draw.pixels!=expected.draw.pixels ||
       actual.draw.ranges!=expected.draw.ranges || actual.last_region!=expected.last_region ||
       actual.draw.immediate_vertices.size()!=expected.draw.immediate_vertices.size()){
        std::fprintf(stderr,"differential case %u: expected error [%s], actual [%s], bytes=%u versions=%u\n",case_id,
            expected.error.c_str(),actual.error.c_str(),unsigned(actual.bytes==expected.bytes),unsigned(actual.versions==expected.versions));
        std::abort();
    }
    return error;
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class RsxPerfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        source = cls.root / 'differential.cpp'
        source.write_text(native_source(DIFFERENTIAL_MAIN))
        cls.binary = cls.root / 'differential'
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra',
                                '-Werror', str(source), '-o', str(cls.binary)],
                               capture_output=True, text=True, timeout=90)
        if build.returncode:
            cls.directory.cleanup()
            raise AssertionError(build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_mode(self, mode):
        run = subprocess.run([str(self.binary), mode], capture_output=True,
                             text=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('PASS', run.stdout)

    def test_random_and_boundary_draws_bit_exact(self):
        self.run_mode('draws')

    def test_mapping_permissions_and_partial_write_faults(self):
        self.run_mode('faults')

    def test_tiled_addresses_and_lazy_bounds(self):
        self.run_mode('addresses')


DIFFERENTIAL_MAIN = r'''
int main(int argc,char** argv){
    assert(argc==2);const std::string mode=argv[1];
    unsigned seed=0x5eed1234;
    auto random=[&](){seed=1664525u*seed+1013904223u;return seed;};
    if(mode=="draws"){
        for(unsigned n=0;n<144;++n){
            PpuState s;const bool tiled=n%2;const unsigned width=17+n%67,height=9+n%39;
            setup_draw(s,width,height,tiled?512:width*4+12,tiled);
            auto& R=s.rsx.registers;auto& m=s.memory;
            for(unsigned i=0;i<60000;++i){(*m.regions[1].bytes)[0x100000+i]=random()>>24;(*m.regions[1].bytes)[0x500000+i]=random()>>24;}
            observe(m,n%3);
            R[0xa74]=n%3!=0;R[0xa70]=n%4!=0;R[0xa6c]=0x200+n%8;
            R[0x304]=n%3==0;R[0x308]=0x200+(n/3)%8;R[0x30c]=random()&255;
            R[0x324]=(n%16&1?0x01000000:0)|(n%16&2?0x00010000:0)|(n%16&4?0x100:0)|(n%16&8?1:0);
            R[0x310]=n%4==0;
            const unsigned factors[]={0,1,0x300,0x301,0x302,0x303,0x304,0x305,0x306,0x307,0x308,0x8001,0x8002,0x8003,0x8004};
            const unsigned eq[]={0x8006,0x800a,0x800b,0x8007,0x8008};
            R[0x314]=factors[n%15]|(factors[(n+3)%15]<<16);R[0x318]=factors[(n+7)%15]|(factors[(n+11)%15]<<16);
            R[0x320]=eq[n%5]|(eq[(n+2)%5]<<16);R[0x31c]=random();
            R[0x8c0]=((width-3)<<16)|(n%4);R[0x8c4]=((height-2)<<16)|(n%3);
            R[0x183c]=n%7==0;R[0x1830]=0x404+n%2;R[0x1834]=0x900+n%2;
            R[0x368]=n%2?0x1d00:0x1d01;
            if(n%5==1){
                // Subpixel/partly clipped, non-axis-aligned geometry. Preserve
                // all original perspective and edge FP operations on both sides.
                for(unsigned i=0;i<4;++i){
                    const float x=(i==1 || i==2?float(width)-1.125f:-2.375f)+(random()%7)*0.125f;
                    const float y=(i>=2?float(height)+1.25f:0.375f)+(random()%5)*0.125f;
                    const float coords[]={x,y,(n%7)*0.125f,1.f};
                    for(unsigned c=0;c<4;++c){unsigned bits;std::memcpy(&bits,&coords[c],4);m.store(0x70002000+i*16+c*4,4,bits);}
                }
                s.rsx.draw.primitive=n%2?5:6;
            }
            if(n%3){
                // TEX R0,TEX0; same full quad interpreter, varying attribute 4.
                const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
                for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
                R[0x1a00]=0x8000;R[0x1a04]=0x00030029|(0xa5<<8);R[0x1a08]=(1+n%8)*0x101;
                R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a10]=n%9?0xaae4:0x55e4;
                R[0x1a14]=((1+(n/3)%6)<<16)|((1+n%2)<<24);R[0x1a18]=(8u<<16)|8;R[0x1840]=32;
                for(unsigned i=0;i<448;++i)(*m.regions[1].bytes)[0x8000+i]=random()>>24;
                // Gamma channels have corrected semantics; fixed independent
                // expectations live in test_rsx_swz and test_rsx_gaps.
                // Frozen oracle covers gamma-disabled configurations only.
                if(n%5==0)R[0x1a04]=0x00010029|((0x86+(n/5)%3)<<8); // DXT1/3/5
            }else{
                // Binary32 boundary colors, half aliasing, masks and clamp.
                const unsigned values[]={0,0x80000000,0x3f000000,0x3f7fffff,0x3f800000,0xbf800000,0x40000000,0x7f800000,0xff800000,0x7fc00000};
                for(unsigned c=0;c<4;++c){const auto w=values[(n/3+c)%10];m.store(0xc0001010+c*4,4,(w<<16)|(w>>16));}
                if(n%2)R[0x1d60]=0;
            }
            differential(s,n);
        }
        for(unsigned attr=0;attr<15;++attr)for(unsigned variant=0;variant<3;++variant){
            PpuState s;setup_draw(s,31,23,256,true);auto& m=s.memory;
            observe(m,variant);
            const unsigned words[]={(1u<<24)|(15u<<9)|(attr<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
            for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
            if(variant==1)s.rsx.registers[0x368]=0x1d00;
            if(variant==2){
                // Vary attribute 0.w, retaining the original VP and arithmetic.
                for(unsigned i=0;i<4;++i)m.store(0x70002000+i*16+12,4,i%2?0x40000000:0x3f000000);
            }
            differential(s,2000+attr*3+variant);
        }
        for(unsigned n=0;n<6;++n){
            PpuState s;setup_draw(s,41,29,256,n%2);auto& m=s.memory;auto& R=s.rsx.registers;
            R[0xa70]=1;R[0x214]=R[0x210]+(n/2)*4;observe(m,1);
            const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
            for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
            // Texture, color and depth share host bytes. The frozen reference samples
            // an independent pre-draw copy; actual execution keeps the aliased input.
            for(auto& tile:s.rsx.tiles)tile.bound=false;
            R[0x1a00]=R[0x210];R[0x1a04]=0x00010029|(0xa5<<8);R[0x1a08]=0x0303;
            R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x02020000;R[0x1a18]=(8u<<16)|8;R[0x1840]=32;
            assert(differential(s,3000+n,0,true).empty());
        }
        std::puts("PASS 195 draw states, whole memory and exact write versions");
    }else if(mode=="faults"){
        for(unsigned n=0;n<14;++n){
            PpuState s;setup_draw(s,73,37,512,true);auto& m=s.memory;auto& gpu=s.rsx;observe(m,1);
            if(n==0)m.regions[1].flags=4;
            if(n==1)m.regions[1].flags=7;
            if(n==2)m.regions[1].bytes->resize(0x100010);
            if(n==3){m.regions[1].bytes->resize(0x200000);m.map(0xc0200000,0x600000,6,nullptr,0);}
            if(n==4)gpu.tiles[0].bank=4;
            if(n==5)gpu.tiles[0].pitch=768; // Layout mismatch, bind fails.
            if(n==6)gpu.tiles[0].offset+=4;
            if(n==7){gpu.tiles[0].size=512*37;gpu.tiles[0].bank=3;} // Valid prefix then tiled bounds fault.
            if(n==8){gpu.tiles[0].size=512*37+7;gpu.tiles[0].bank=0;}
            if(n==9){gpu.registers[0xa70]=1;gpu.tiles[0].bank=4;} // Depth committed before color fault.
            if(n==10){gpu.registers[0x310]=1;gpu.registers[0x314]=1;gpu.registers[0x318]=0;gpu.registers[0x320]=0;}
            if(n==11)gpu.registers[0x20c]=508;
            if(n==12){gpu.registers[0x20c]=17*256;gpu.tiles[0].pitch=17*256;}
            if(n==13){const unsigned w=(1u<<24)|(15u<<9)|(15u<<13)|1u;
                m.store(0xc0001000,4,(w<<16)|(w>>16));const unsigned src=1u|(0xe4u<<9)|(7u<<18);
                m.store(0xc0001004,4,(src<<16)|(src>>16));}
            const auto initial=snapshot(s);
            if(n==3){
                assert(differential(s,1000+n,0x100000).empty());
                // Fixed BE ARGB bytes at the first pixel of each tile column.
                for(const auto address:{0xc0100000u,0xc0108200u}){
                    assert(m.load(address,1)==0x80);assert(m.load(address+1,1)==0xff);
                    assert(m.load(address+2,1)==0x80);assert(m.load(address+3,1)==0x40);
                }
                assert(gpu.draw.draws==1 && gpu.draw.triangles==2 && gpu.draw.pixels==2701);
                assert(gpu.draw.ranges.empty());
            }else assert(!differential(s,1000+n).empty());
            if(n==8 || n==9)assert(snapshot(s).bytes!=initial.bytes);
        }
        std::puts("PASS adjacent-region draw and 13 faults: mapping, permissions, tile bounds, partial writes");
    }else if(mode=="addresses"){
        // Compare the cached target with the frozen address permutation at all
        // samples, including supported factor 1/2/power-of-two and odd divisors.
        for(unsigned columns:{1,2,3,4,5,6,7,8,10,11,12,13,16,20,24,32,64})for(unsigned bank=0;bank<4;++bank){
            NativeRsx::Tile tile;tile.pitch=columns*256;tile.offset=0x100000;tile.size=0x1000000;tile.bank=bank;
            std::vector<std::uint8_t> bytes(tile.size);
            rsxdraw::Target t;t.tile=&tile;t.base=bytes.data();t.base_offset=tile.offset;t.offset=tile.offset;t.pitch=tile.pitch;
            for(unsigned y=0;y<131;++y)for(unsigned x=0;x<tile.pitch/4;++x){
                const auto expected=reference_tiled_offset(tile,y*tile.pitch+x*4);
                assert(t.at(x,y)-bytes.data()==expected-tile.offset);
            }
        }
        // Partial final 32-byte run: no cache may swallow an address error.
        NativeRsx::Tile tile;tile.pitch=256;tile.offset=0;tile.size=65539;tile.bank=0;
        std::vector<std::uint8_t> bytes(0x20000);
        rsxdraw::Target t;t.tile=&tile;t.base=bytes.data();t.pitch=tile.pitch;
        for(unsigned i=0;i<65580;i+=4){
            std::string expected,actual;unsigned off=0;
            try{off=reference_tiled_offset(tile,i);}catch(const std::runtime_error& e){expected=e.what();}
            try{const auto p=t.at((i%256)/4,i/256);if(expected.empty())assert(p-bytes.data()==off);}catch(const std::runtime_error& e){actual=e.what();}
            assert(actual==expected);
        }
        std::puts("PASS tiled permutation and lazy partial-run faults");
    }else return 2;
}
'''
