"""Firmware 3.01 NP custom-menu transport (NPCU, distinct from NPCM commerce).

ACIT imports sceNpTerm, whose internal cleanup sends cmTerm even without any
custom-menu registration. Registration/UI requests remain explicit stops.
Evidence and the six-command inventory: local/ratchet/codex/npcu/commands.md.
"""

CPP = r'''
namespace native_npcu {
static std::vector<std::uint8_t> request(const std::vector<std::uint8_t>& data,
        unsigned channel, const std::set<std::uint64_t>& connections,
        const std::string& command) {
    if(command!="cmTerm")
        throw std::runtime_error("unsupported native NPCU command: "+command);
    auto word=[&](unsigned at)->std::uint32_t {
        if(at>data.size() || data.size()-at<4)
            throw std::runtime_error("truncated NPCU request");
        return (std::uint32_t(data[at])<<24)|(std::uint32_t(data[at+1])<<16)|
               (std::uint32_t(data[at+2])<<8)|data[at+3];
    };
    // Exact no-argument document produced by libsysutil_np 0x201dfe44..e6c.
    // Alignment bytes are not part of the tree/string tables.
    if(channel || data.size()!=112 || word(0)!=0x4e504355 || word(4)!=0x110 ||
       word(8)!=64 || word(12)!=28 || word(16)!=96 || word(20) ||
       word(24)!=96 || word(28)!=7 || word(32)!=112 || word(36) ||
       word(40)!=112 || word(44) || word(48)!=112 || word(52) ||
       word(56) || word(60) || word(64) || word(68) ||
       !std::equal(data.begin()+96,data.begin()+103,"cmTerm\0"))
        throw std::runtime_error("unsupported NPCU cmTerm request shape");
    for(unsigned at=72;at<92;at+=4)if(word(at)!=UINT32_MAX)
        throw std::runtime_error("unsupported NPCU cmTerm root links");
    unsigned port=UINT32_MAX;
    for(auto connection:connections)if((connection>>32)==0){
        if(port!=UINT32_MAX)throw std::runtime_error("ambiguous NPCU sysutil client");
        port=static_cast<std::uint32_t>(connection);
    }
    if(port>=4)throw std::runtime_error("NPCU request without sysutil client connection");
    // VSH 3.01 dispatch 0x671498 -> 0xf12b8 -> 0xf1250 clears the
    // custom-action/exception registrations and returns 0 unconditionally.
    // This backend's registration set is always empty: every command that
    // could populate it traps above. Thus cleanup is idempotent, independent
    // of PSN status; it neither signs out NP nor unregisters NPMG callbacks.
    // VSH 0xf1d08 creates root "result" with one int leaf also named "result"
    // (0xf1da0..dbc); CXML interns their shared name. Never echo "cmTerm".
    std::vector<std::uint8_t> response(128,0);
    auto put=[&](unsigned at,std::uint32_t value){
        for(unsigned j=0;j<4;++j)response[at+j]=value>>(24-8*j);
    };
    put(0,0x4e504355);put(4,0x110);put(8,64);put(12,44);
    put(16,112);put(24,112);put(28,7);
    put(32,128);put(40,128);put(48,128);put(68,1);
    for(unsigned at=72;at<92;at+=4)put(at,UINT32_MAX);
    put(96,1); // root and leaf names both have string offset 0; result = 0.
    std::copy_n("result",7,response.begin()+112);
    return response;
}
} // namespace native_npcu
'''
