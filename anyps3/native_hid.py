"""cellKb/cellMouse library state without a host keyboard or mouse backend.

The runtime exposes no keyboard or mouse device, so every port reports no
device; initialization, per-port configuration storage and shutdown are real
library state changes. Error codes and parameter limits: local RPCS3
Emu/Cell/Modules/cellKb.cpp:78-122,398-485, cellMouse.cpp:86-160, cellKb.h:7-14,
cellMouse.h:5-19, Emu/Io/Keyboard.h:13-31 (derived reference, not firmware).
"""

TYPES = r'''
struct NativeHid {
    unsigned keyboards=0,mice=0; // max_connect of an initialized library, 0 = uninitialized
    std::array<std::uint8_t,127> code_type{},read_mode{};
};
'''

IMPORTS = {
    ("sys_io", "0x433f6ec0"): ("cellKbInit", "r[3]=sign_word(hid_service(state,0));"),
    ("sys_io", "0xbfce3285"): ("cellKbEnd", "r[3]=sign_word(hid_service(state,1));"),
    ("sys_io", "0xa5f85e4d"): ("cellKbSetCodeType", "r[3]=sign_word(hid_service(state,2));"),
    ("sys_io", "0xdeefdfa7"): ("cellKbSetReadMode", "r[3]=sign_word(hid_service(state,3));"),
    ("sys_io", "0xc9030138"): ("cellMouseInit", "r[3]=sign_word(hid_service(state,4));"),
    ("sys_io", "0xe10183ce"): ("cellMouseEnd", "r[3]=sign_word(hid_service(state,5));"),
}

CPP = r'''
[[maybe_unused]] static std::uint32_t hid_service(PpuState& s,unsigned operation){
    auto& h=s.hid;
    const auto a=std::uint32_t(s.r[3]),b=std::uint32_t(s.r[4]); // u32 arguments
    constexpr std::uint32_t KB_INVALID=0x80121002,KB_ALREADY=0x80121003,KB_UNINIT=0x80121004;
    constexpr std::uint32_t MOUSE_INVALID=0x80121202,MOUSE_ALREADY=0x80121203,MOUSE_UNINIT=0x80121204;
    switch(operation){
    case 0:
        if(h.keyboards)return KB_ALREADY;
        if(!a || a>127)return KB_INVALID;
        h.keyboards=a;h.code_type.fill(0);h.read_mode.fill(0);
        std::fprintf(stderr,"native keyboard library initialized max=%u connected=0 (no host keyboard backend)\n",h.keyboards);
        return 0;
    case 1:if(!h.keyboards)return KB_UNINIT;h.keyboards=0;return 0;
    case 2:case 3:
        if(!h.keyboards)return KB_UNINIT;
        if(a>=127 || b>1)return KB_INVALID; // CODETYPE_ASCII=1, RMODE_PACKET=1
        (operation==2?h.code_type:h.read_mode)[a]=std::uint8_t(b);
        return 0;
    case 4:
        if(h.mice)return MOUSE_ALREADY;
        if(!a || a>127)return MOUSE_INVALID;
        h.mice=a;
        std::fprintf(stderr,"native mouse library initialized max=%u connected=0 (no host mouse backend)\n",h.mice);
        return 0;
    case 5:if(!h.mice)return MOUSE_UNINIT;h.mice=0;return 0;
    }
    throw std::runtime_error("unsupported native keyboard/mouse operation");
}
'''
