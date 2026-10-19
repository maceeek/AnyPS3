"""Additional native RSX codecs and arithmetic.

Derived from RPCS3 ffba2bf09ef3980638f9327b25240d81414034ab:
GL/GLRenderTargets.cpp:9-68, Program/FragmentProgramDecompiler.cpp:1074-1295,
Program/GLSLSnippets/RSXProg/RSXProgramCommon.glsl:20-40.
These are software semantics, not a claim of bit-exact RSX silicon arithmetic.
Included only by rsx_draw; no program.py registration is required.
"""

CPP = r'''
static std::uint32_t float_bits(float f){std::uint32_t b;std::memcpy(&b,&f,4);return b;}
static unsigned read_be(const std::uint8_t* p,unsigned n){unsigned v=0;for(unsigned i=0;i<n;++i)v=(v<<8)|p[i];return v;}
static void write_be(std::uint8_t* p,unsigned n,unsigned v){for(unsigned i=0;i<n;++i)p[i]=v>>(8*(n-1-i));}
static Vec4 lit_value(const Vec4& x){
    return {1,std::max(0.f,x[0]),x[0]>0?std::pow(std::max(1.e-10f,x[1]),x[3]):0.f,1};
}
static unsigned color_bytes(unsigned f){
    switch(f){case 1:case 2:case 3:case 10:return 2;case 9:return 1;case 11:return 8;case 12:return 16;
    case 4:case 5:case 8:case 13:case 14:case 15:case 16:return 4;
    default:throw std::runtime_error("unsupported RSX draw color format "+std::to_string(f));}
}
static bool float_color(unsigned f){return f>=11 && f<=13;}
static Vec4 read_color(const std::uint8_t* p,unsigned f){
    if(f==11 || f==12){Vec4 c;const unsigned n=f==11?2:4;for(unsigned i=0;i<4;++i)c[i]=f==11?half_float(read_be(p+i*n,n)):bits_float(read_be(p+i*n,n));return c;}
    if(f==13){const auto x=bits_float(read_be(p,4));return {x,x,x,x};}
    if(f<=3){const auto x=read_be(p,2);if(f==3)return {((x>>11)&31)/31.f,((x>>5)&63)/63.f,(x&31)/31.f,1};
        return {((x>>10)&31)/31.f,((x>>5)&31)/31.f,(x&31)/31.f,f==2?1.f:0.f};}
    if(f==9)return {p[0]/255.f,p[0]/255.f,p[0]/255.f,1};
    if(f==10)return {p[1]/255.f,p[0]/255.f,p[1]/255.f,p[0]/255.f};
    const bool bgr=f>=14;
    return {p[bgr?3:1]/255.f,p[2]/255.f,p[bgr?1:3]/255.f,(f==4 || f==14)?0.f:(f==5 || f==15)?1.f:p[0]/255.f};
}
static void write_color(std::uint8_t* p,unsigned f,const Vec4& c,unsigned mask,bool logic,unsigned op){
    static const unsigned masks[4]={0x10000,0x100,1,0x1000000};
    if(float_color(f)){
        if(logic)throw std::runtime_error("RSX logic operations on float targets are unsupported");
        const unsigned n=f==11?2:4,count=f==13?1:4;
        for(unsigned i=0;i<count;++i)if(mask&masks[i])write_be(p+i*n,n,f==11?float_half(c[i]):float_bits(c[i]));
        return;
    }
    auto quant=[](float x,unsigned max){return unsigned(std::lround(std::clamp(x,0.f,1.f)*max));};
    auto combine=[&](unsigned src,unsigned dst){
        // logic_op is declared by rsx_fixed before this function's definition.
        return logic?logic_op(op,src,dst):src;
    };
    if(f<=3){
        const unsigned old=read_be(p,2);unsigned packed=f==3?(quant(c[0],31)<<11)|(quant(c[1],63)<<5)|quant(c[2],31):
            (f==2?0x8000:0)|(quant(c[0],31)<<10)|(quant(c[1],31)<<5)|quant(c[2],31);
        unsigned enabled=0;
        if(mask&masks[0])enabled|=f==3?0xf800:0x7c00;
        if(mask&masks[1])enabled|=f==3?0x7e0:0x3e0;
        if(mask&masks[2])enabled|=31;
        if(f!=3 && (mask&masks[3]))enabled|=0x8000;
        write_be(p,2,(old&~enabled)|(combine(packed,old)&enabled));return;
    }
    if(f==9){if(mask&masks[2])p[0]=combine(quant(c[2],255),p[0]);return;}
    if(f==10){if(mask&masks[1])p[0]=combine(quant(c[1],255),p[0]);if(mask&masks[2])p[1]=combine(quant(c[2],255),p[1]);return;}
    const bool bgr=f>=14;
    for(unsigned i=0;i<4;++i)if(mask&masks[i]){
        const unsigned slot=i==3?0:i==1?2:i==0?(bgr?3:1):(bgr?1:3);
        const unsigned value=i==3&&(f==4||f==14)?0:i==3&&(f==5||f==15)?255:quant(c[i],255);
        p[slot]=combine(value,p[slot]);
    }
}
'''
