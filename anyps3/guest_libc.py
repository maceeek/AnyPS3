"""sysPrxForUser string/memory primitives executed on guest memory.

Semantics follow RPCS3 Emu/Cell/Modules/sys_libc_.cpp (_sys_memcmp :135,
_sys_memmove :180, _sys_strchr :261, _sys_strncpy :311, _sys_strncasecmp :335,
_sys_strrchr :353). PPU char is unsigned. Unmapped guest ranges trap in
GuestMemory::access; nothing is skipped.
"""

IMPORTS = {
    ("sysPrxForUser", "0xfb5db080"): ("_sys_memcmp", "r[3]=sign_word(std::uint32_t(guest_memcmp(state,r[3],r[4],std::uint32_t(r[5]))));"),
    ("sysPrxForUser", "0x27427742"): ("_sys_memmove", "guest_memmove(state);"),
    ("sysPrxForUser", "0x7498887b"): ("_sys_strchr", "r[3]=guest_strchr(state,r[3],std::uint8_t(r[4]),false);"),
    ("sysPrxForUser", "0x191f0c4a"): ("_sys_strrchr", "r[3]=guest_strchr(state,r[3],std::uint8_t(r[4]),true);"),
    ("sysPrxForUser", "0xd3039d4d"): ("_sys_strncpy", "guest_strncpy(state);"),
    ("sysPrxForUser", "0x1ca525a2"): ("_sys_strncasecmp", "r[3]=sign_word(std::uint32_t(guest_strncasecmp(state,r[3],r[4],std::uint32_t(r[5]))));"),
}

CPP = r'''
[[maybe_unused]] static std::int32_t guest_memcmp(PpuState& state,std::uint64_t a,std::uint64_t b,std::uint32_t size){
    if(size>16*1024*1024)throw std::runtime_error("guest memcmp exceeds diagnostic limit");
    if(!size)return 0;
    std::vector<std::uint8_t> x(size),y(size); // ranges may span adjacent mappings
    state.memory.transfer(a&UINT32_MAX,x.data(),size,false);state.memory.transfer(b&UINT32_MAX,y.data(),size,false);
    for(std::uint32_t i=0;i<size;++i)if(x[i]!=y[i])return x[i]<y[i]?-1:1;
    return 0;
}
[[maybe_unused]] static void guest_memmove(PpuState& state){
    const auto dst=state.r[3]&UINT32_MAX,src=state.r[4]&UINT32_MAX;const auto size=std::uint32_t(state.r[5]);
    if(size>16*1024*1024)throw std::runtime_error("guest memmove exceeds diagnostic limit");
    if(size){ // the temporary copy gives memmove overlap semantics across mappings
        std::vector<std::uint8_t> bytes(size);
        state.memory.transfer(src,bytes.data(),size,false);state.memory.transfer(dst,bytes.data(),size,true);
    }
    state.r[3]=dst;
}
[[maybe_unused]] static std::uint64_t guest_strchr(PpuState& state,std::uint64_t str,std::uint8_t ch,bool last){
    str&=UINT32_MAX;std::uint64_t found=0;
    for(std::uint32_t i=0;;++i){
        if(i>=16*1024*1024)throw std::runtime_error("guest strchr exceeds diagnostic limit");
        const auto c=*state.memory.access(str+i,1,false);
        if(c==ch){found=str+i;if(!last)return found;}
        if(!c)return found;
    }
}
[[maybe_unused]] static void guest_strncpy(PpuState& state){
    const auto dst=state.r[3]&UINT32_MAX,src=state.r[4]&UINT32_MAX;
    const auto len=static_cast<std::int32_t>(state.r[5]);
    if(!dst || !src){state.r[3]=0;return;}
    if(len>16*1024*1024)throw std::runtime_error("guest strncpy exceeds diagnostic limit");
    for(std::int32_t i=0;i<len;++i){
        const auto c=*state.memory.access(src+i,1,false);
        *state.memory.access(dst+i,1,true)=c;
        if(!c){for(++i;i<len;++i)*state.memory.access(dst+i,1,true)=0;break;}
    }
    state.r[3]=dst;
}
[[maybe_unused]] static std::int32_t guest_strncasecmp(PpuState& state,std::uint64_t a,std::uint64_t b,std::uint32_t n){
    a&=UINT32_MAX;b&=UINT32_MAX;
    for(std::uint32_t i=0;i<n;++i){
        if(i>=16*1024*1024)throw std::runtime_error("guest strncasecmp exceeds diagnostic limit");
        int x=*state.memory.access(a+i,1,false),y=*state.memory.access(b+i,1,false);
        // The firmware ctype table covers -1..127 only (RPCS3 _sys_tolower ensure).
        if(x>127 || y>127)throw std::runtime_error("guest strncasecmp byte outside ctype table is unsupported");
        if(x>='A' && x<='Z')x+=0x20;
        if(y>='A' && y<='Z')y+=0x20;
        if(x<y)return -1;
        if(x>y)return 1;
        if(!x)break;
    }
    return 0;
}
'''
