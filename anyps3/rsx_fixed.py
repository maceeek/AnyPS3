"""RSX fixed fragment functions, with explicit validation before raster workers.

Register/enumerant evidence: RPCS3 ffba2bf0 gcm_enums.h and rsx_decode.h;
fog transform: Program/GLSLSnippets/RSXProg/RSXFragmentPrologue.glsl:67-104;
Z24 offset unit: VK/VKDraw.cpp:212-233 (RSX unit = 1/(2^24-1)).
"""
CPP = r'''
// IEC sRGB transfer, as used by framebuffer_srgb_enabled() in RPCS3.
static float srgb_to_linear(float value){return value<=0.04045f?value/12.92f:std::pow((value+0.055f)/1.055f,2.4f);}
static float linear_to_srgb(float value){return value<=0.0031308f?value*12.92f:1.055f*std::pow(value,1.f/2.4f)-0.055f;}
static unsigned stencil_op(unsigned op,unsigned old,unsigned ref){
    switch(op){
    case 0:return 0;case 0x1e00:return old;case 0x1e01:return ref;
    case 0x1e02:return std::min(255u,old+1);case 0x1e03:return old?old-1:0;
    case 0x150a:return old^255;case 0x8507:return (old+1)&255;case 0x8508:return (old-1)&255;
    default:throw std::runtime_error("unsupported RSX stencil operation "+std::to_string(op));
    }
}
struct Stencil {
    bool enabled=false;
    unsigned mask=0,func=0,ref=0,read_mask=0,fail=0,zfail=0,zpass=0;
    explicit Stencil(const Reg& reg,bool back,bool active=true){
        enabled=active && (reg.get(0x328,0)&1);
        if(!enabled)return;
        // A partially specified old draw remains an explicit unsupported state.
        const unsigned offset=back && reg.get(0x348,0)?0x20:0;
        for(unsigned r=0x32c;r<=0x344;r+=4)if(!reg.gpu.registers.count(r+offset))
            throw std::runtime_error("RSX stencil test is unsupported");
        mask=reg(0x32c+offset)&255;func=reg(0x330+offset);ref=reg(0x334+offset)&255;
        read_mask=reg(0x338+offset)&255;fail=reg(0x33c+offset);zfail=reg(0x340+offset);zpass=reg(0x344+offset);
        if(func<0x200 || func>0x207)throw std::runtime_error("invalid RSX stencil comparison");
        (void)stencil_op(fail,0,ref);(void)stencil_op(zfail,0,ref);(void)stencil_op(zpass,0,ref);
    }
    bool test(unsigned value)const{return !enabled || compare(func,float(ref&read_mask),float(value&read_mask));}
    void apply(GuestMemory& memory,std::uint8_t* ptr,unsigned op,bool observed)const{
        if(!enabled || !mask)return;
        const unsigned value=(*ptr&~mask)|(stencil_op(op,*ptr,ref)&mask);
        if(value!=*ptr){if(observed)memory.mark_write(ptr,1);*ptr=value;}
    }
};
static unsigned logic_op(unsigned op,unsigned src,unsigned dst){
    switch(op){
    case 0x1500:return 0;case 0x1501:return src&dst;case 0x1502:return src&~dst;case 0x1503:return src;
    case 0x1504:return ~src&dst;case 0x1505:return dst;case 0x1506:return src^dst;case 0x1507:return src|dst;
    case 0x1508:return ~(src|dst);case 0x1509:return ~(src^dst);case 0x150a:return ~dst;case 0x150b:return src|~dst;
    case 0x150c:return ~src;case 0x150d:return ~src|dst;case 0x150e:return ~(src&dst);case 0x150f:return 255;
    default:throw std::runtime_error("invalid RSX logic operation");
    }
}
static Vec4 fog_value(unsigned mode,float coord,float p0,float p1){
    float f=0;
    switch(mode){
    case 0x2601:f=p1*coord+p0-1.f;break;
    case 0x804:f=p1*std::fabs(coord)+p0-1.f;break;
    case 0x800:f=std::exp(11.084f*(p1*coord+p0-1.5f));break;
    case 0x802:f=std::exp(11.084f*(p1*std::fabs(coord)+p0-1.5f));break;
    case 0x801:case 0x803:{const float c=mode==0x803?std::fabs(coord):coord;const float t=4.709f*(p1*c+p0-1.5f);f=std::exp(-t*t);break;}
    default:throw std::runtime_error("unsupported RSX fog mode");
    }
    return {coord,std::clamp(f,0.f,1.f),0,0};
}
// Reject active states for which the triangle renderer has no implementation.
// Storing reset state is permitted, but its non-neutral effect cannot disappear.
static void validate_triangle_state(const Reg& reg){
    const std::pair<unsigned,const char*> disabled[]={
        {0x238,"cylindrical wrap"},{0x23c,"cylindrical wrap1"},
        {0x1838,"polygon smoothing"},{0x1da4,"clip ID test"}};
    for(const auto& entry:disabled)if(reg.get(entry.first,0))throw std::runtime_error(std::string("unsupported RSX ")+entry.second);
    if(reg.get(0x1d78,1)!=1)throw std::runtime_error("unsupported RSX depth clip/clamp control");
    if(reg.get(0x1e94,0x11)!=0x11)throw std::runtime_error("unsupported RSX transform execution mode");
    if(reg.get(0x1fc4,0x06144321)!=0x06144321 || reg.get(0x1fc8,0xedcba987)!=0xedcba987 || reg.get(0x1fcc,0x6f)!=0x6f)
        throw std::runtime_error("unsupported RSX varying attribute mapping");
    if(reg.get(0x1fec,0)>1)throw std::runtime_error("unsupported RSX shader packer");
    if(reg.get(0x1fe0,1)!=1)throw std::runtime_error("unsupported RSX destination color reduction");
    if(reg.get(0x3b0,0x100000)!=0x100000)throw std::runtime_error("unsupported RSX control0/depth float/user clip");
    if(reg.get(0x224,0x80)!=0x80 || reg.get(0x228,0x100)!=0x100 || reg.get(0x230,0))
        throw std::runtime_error("unsupported RSX undocumented surface state");
    if(reg.get(0x368,0x1d01)!=0x1d00 && reg.get(0x368,0x1d01)!=0x1d01)throw std::runtime_error("invalid RSX shade model");
    if(reg.get(0x1834,0x901)!=0x900 && reg.get(0x1834,0x901)!=0x901)throw std::runtime_error("invalid RSX front face");
    if(reg.get(0x183c,0) && reg.get(0x1830,0x405)!=0x404 && reg.get(0x1830,0x405)!=0x405 && reg.get(0x1830,0x405)!=0x408)
        throw std::runtime_error("invalid RSX cull face");
    if(reg.get(0x1428,1)!=1)throw std::runtime_error("unsupported RSX specular enable state");
    if(reg.get(0x1454,0)>1)throw std::runtime_error("unsupported RSX flat shade provoking vertex selection");
    // These are the exact libgcm reset values in RPCS3 rsx_methods.cpp:271-324.
    // Their bit meanings are undocumented; only the reset configuration is accepted.
    for(unsigned r=0x240;r<=0x24c;r+=4)if(reg.get(r,r==0x240?0xffff:0)!=(r==0x240?0xffffu:0u))
        throw std::runtime_error("unsupported RSX undocumented raster state");
    for(unsigned r=0x400;r<=0x43c;r+=4)if(reg.get(r,0x7421)!=0x7421)throw std::runtime_error("unsupported RSX undocumented sampling state");
    const unsigned reset[]={0x9aabaa98,0x66666789,0x98766666,0x89aabaa9,0x99999999,0x88888889,0x98888888,0x99999999,
        0x56676654,0x33333345,0x54333333,0x45667665,0xaabbba99,0x66667899,0x99876666,0x99abbbaa};
    for(unsigned i=0;i<16;++i)if(reg.get(0x440+4*i,reset[i])!=reset[i])throw std::runtime_error("unsupported RSX undocumented sampling state");
}
'''
