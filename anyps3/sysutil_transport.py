"""Bounded native services for the firmware 3.01 sysutil transport.

Only registers a connection to an existing native event queue for process 1.
Video queries describe the configured virtual output, not rendered frames.
Unknown commands trap before consuming input. This is not a VSH replacement.
"""
from .native_savedata import TYPES as SAVE_TYPES, CPP as SAVE_CPP
from .native_trophy import TYPES as TROPHY_TYPES, CPP as TROPHY_CPP
from .native_vsh_ui import TYPES as VSH_UI_TYPES, CPP as VSH_UI_CPP
from .native_npcu import CPP as NPCU_CPP
from .native_gamedata import CPP as GAMEDATA_CPP

TYPES = SAVE_TYPES + TROPHY_TYPES + VSH_UI_TYPES + r'''
struct NativeVideo {
    std::uint8_t resolution=2, aspect=2, format=0; // 720p, 16:9, X8R8G8B8
    std::uint32_t pitch=1280*4;
};
struct NativeSystemSettings {
    NativeSavedata savedata;
    NativeVshUi vsh_ui;
    std::uint32_t language=1, enter_button=1; // English US, cross
    // VSH-side NP manager subscriptions, indexed by the connected sysutil port.
    // Callback OPDs/arguments remain in the original libsysutil_np client.
    std::array<bool,4> np_manager_callbacks{};
};
'''
CPP = SAVE_CPP + TROPHY_CPP + VSH_UI_CPP + NPCU_CPP + GAMEDATA_CPP + r'''
// True while a channel-1 PacketRead would park: the SAVE UI is open and neither a reply nor guest data is pending.
template<class State> static bool sysutil_save_ui_waiting(State& s) {
    const auto& u=s.settings.vsh_ui;
    if(!u.open || u.kind==NativeVshUi::Message || !s.settings.savedata.replies.empty())return false;
    const auto& bytes=*s.mmapper.objects.front().bytes;const unsigned size=0x8000+16;
    return !((std::uint32_t(bytes[size])<<24)|(std::uint32_t(bytes[size+1])<<16)|(std::uint32_t(bytes[size+2])<<8)|bytes[size+3]);
}
// Scheduler hook: poll host input for an open SAVE UI while its PacketRead waiter is parked (at most every 1 ms), and
// wake the parked waiters as VSH would signal their condition once a reply, a closed UI or guest ring data exists.
// Template: SAVE-only harnesses compile this transport against a PpuState without PPU contexts.
template<class State> [[maybe_unused]] static void sysutil_park_service(State& s) {
    auto& u=s.settings.vsh_ui;
    if(u.parked.empty())return;
    const auto now=std::chrono::steady_clock::now();
    if(now<u.park_poll)return;
    u.park_poll=now+std::chrono::milliseconds(1);
    GuestMemory::RawPermit legacy_transport(s.memory);
    vsh_ui_poll(s);
    if(sysutil_save_ui_waiting(s))return;
    for(const auto& [thread,cond]:u.parked)for(auto& t:s.ppu_threads)
        if(t.thread_id==thread && t.wait_kind==4 && t.wait_id==cond){t.wait_kind=0;t.resume_cond=cond;s.reschedule=true;}
    u.parked.clear();
}
static bool service_sysutil_connect(PpuState& s, std::uint64_t cond_id) {
    vsh_ui_poll(s);
    // Each channel owns four system conditions 0x86000000+(channel*4+c)*0x100
    // (kernel_sync.py): libsysutil waits for a reply on one and, in PacketWrite
    // (0x2011362c, ring full), for free space on another (0x86000500 for SAVE raw).
    if (cond_id<0x86000000 || cond_id>=0x86000800 || (cond_id&0xff)) return false;
    const unsigned channel=static_cast<unsigned>((cond_id-0x86000000)>>10);
    auto& bytes=*s.mmapper.objects.front().bytes;
    const unsigned base=channel*0x8000;
    auto word=[&](unsigned a){return (std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
    auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j) bytes[a+j]=static_cast<std::uint8_t>(v>>(24-j*8));};
    const auto offset=word(base), capacity=word(base+4), start=word(base+12), size=word(base+16);
    // SAVE replies are distinct packets, including raw lists/data. Firmware
    // PacketRead clears +20/+24 after consuming the final byte (3.01
    // libsysutil 0x20113b88..ba4); continuation chunks keep +20=1.
    auto& save=s.settings.savedata;
    auto save_publish=[&](unsigned consumed){
        if(save.replies.empty())return false;
        if(offset!=0x400 || capacity!=0x7c00 || start>=capacity || size>capacity)
            throw std::runtime_error("invalid SAVE reply ring");
        auto& packet=save.replies.front();
        const auto count=std::min<std::size_t>(capacity,packet.bytes.size()-packet.sent);
        const auto next=(start+consumed)%capacity;const bool end=packet.sent+count==packet.bytes.size();
        s.memory.mark_write(bytes.data()+base,0x8000);
        for(unsigned i=0;i<count;++i)bytes[base+offset+(next+i)%capacity]=packet.bytes[packet.sent+i];
        put(base+12,next);put(base+8,(next+count)%capacity);put(base+16,count);put(base+20,end?2:1);put(base+24,2);
        packet.sent+=count;if(end)save.replies.erase(save.replies.begin());return true;
    };
    // An unconsumed final packet (+20=2, +24=2; a 0-byte raw EOP has size 0) must
    // not be overwritten; a consumed continuation chunk keeps +20=1/+24=2.
    if(channel==1 && !size && !(word(base+20)==2 && word(base+24)==2) && !save.replies.empty())return save_publish(0);
    if(channel==1 && word(base+24)==1 && size && (save.writing || s.settings.vsh_ui.save_raw_expected)){
        if(!save.replies.empty())throw std::runtime_error("SAVE raw data before acceptance was consumed");
        if(offset!=0x400 || capacity!=0x7c00 || start>=capacity || size>capacity || (word(base+20)!=1 && word(base+20)!=2))
            throw std::runtime_error("invalid SAVE raw write ring");
        std::vector<std::uint8_t> payload(size);for(unsigned i=0;i<size;++i)payload[i]=bytes[base+offset+(start+i)%capacity];
        if(save.writing)savedata_vsh::raw_write(save,payload,word(base+20)==2);
        else vsh_ui::save_raw(s,payload,word(base+20)==2);
        if(save_publish(size))return true;
        s.memory.mark_write(bytes.data()+base,0x8000);
        const auto next=(start+size)%capacity;put(base+12,next);put(base+8,next);put(base+16,0);put(base+20,0);put(base+24,0);return true;
    }
    // An open SAVE UI has no reply until the host user acts. Real VSH signals the PacketRead condition only when it
    // writes one, so the waiter parks (kernel_sync 107 blocks it on the condition) and sysutil_park_service wakes it.
    // Returning "serviced" here made PacketRead spin and starve every other PPU thread (run 805au1: menu 60 -> 7 FPS,
    // 65-75 % of the cellAudio blocks left unmixed while the list was open). A non-empty reply queue whose final
    // packet is still unconsumed keeps the immediate re-poll.
    if(channel==1 && !size && s.settings.vsh_ui.open && s.settings.vsh_ui.kind!=NativeVshUi::Message){
        if(!save.replies.empty())return true;
        s.settings.vsh_ui.park_pending=true;return false;
    }
    if (word(base+20)!=2 || word(base+24)!=1 || !size) return false;
    if (offset!=0x400 || capacity!=0x7c00 || start>=capacity || size>capacity)
        throw std::runtime_error("invalid sysutil request ring");
    std::vector<std::uint8_t> request(size);
    for (unsigned i=0;i<size;++i) request[i]=bytes[base+offset+(start+i)%capacity];
    auto u32=[&](unsigned a)->std::uint32_t {
        if (a>size || size-a<4) throw std::runtime_error("truncated CMND request");
        return (std::uint32_t(request[a])<<24)|(std::uint32_t(request[a+1])<<16)|(std::uint32_t(request[a+2])<<8)|request[a+3];
    };
    if(size>=4 && u32(0)==0x53415645){
        if(channel!=1 || !s.sysutil_connections.count(UINT64_C(1)<<32) || !save.replies.empty())
            throw std::runtime_error("SAVE request on wrong/unconnected channel or before draining reply");
        savedata_vsh::request(s,request);
        if(!save_publish(size)){
            if(!s.settings.vsh_ui.open || s.settings.vsh_ui.kind==NativeVshUi::Message)
                throw std::runtime_error("SAVE command produced no response");
            s.memory.mark_write(bytes.data()+base,0x8000);
            const auto next=(start+size)%capacity;put(base+12,next);put(base+8,next);put(base+16,0);put(base+20,0);put(base+24,0);
        }
        return true;
    }
    // Protocol errors below report the raw request so its layout can be analysed.
    auto dump=[&](const char* reason){
        std::fprintf(stderr,"sysutil request channel=%u size=%u:",channel,size);
        for(unsigned i=0;i<size && i<1024;++i)std::fprintf(stderr,"%s%02x",i%32?"":"\n  ",request[i]);
        std::fprintf(stderr,"\n");throw std::runtime_error(reason);
    };
    if (size<64 || u32(4)!=0x110 || u32(8)!=0x40)
        dump("unsupported sysutil command format");
    const auto strings=u32(24), string_size=u32(28);
    if (strings>size || string_size>size-strings) dump("CMND string table out of bounds");
    auto string=[&](unsigned at) {
        std::string value;
        for (unsigned i=at;i<string_size;++i) {
            auto ch=request[strings+i]; if (!ch) return value; value.push_back(static_cast<char>(ch));
        }
        dump("unterminated CMND string");return value;
    };
    const auto command=string(u32(64));
    std::fprintf(stderr,"native sysutil command channel=%u name=%s bytes=%u\n",channel,command.c_str(),size);
    auto publish=[&](const std::vector<std::uint8_t>& response) {
        s.memory.mark_write(bytes.data()+base,0x8000);
        if(response.size()>capacity) throw std::runtime_error("sysutil response exceeds ring capacity");
        const auto next=(start+size)%capacity;
        for(unsigned i=0;i<response.size();++i) bytes[base+offset+(next+i)%capacity]=response[i];
        put(base+12,next);put(base+8,(next+response.size())%capacity);put(base+16,response.size());
        put(base+20,2);put(base+24,2);
    };
    if(u32(0)==0x4e504355){
        publish(native_npcu::request(request,channel,s.sysutil_connections,command));return true;
    }
    if(u32(0)==0x4d534744 || u32(0)==0x4f534b44){
        publish(vsh_ui::request(s,request,channel));return true;
    }
    if(u32(0)==0x4e505452){
        std::vector<std::uint8_t> response;
        if(native_trophy::more(s,request,channel,response)){publish(response);return true;}
    }
    if(u32(0)==0x47444154 && command!="bootInfo"){
        publish(native_gamedata::request(s,request,channel));return true;
    }
    if (u32(0)==0x47444154 && command=="bootInfo") {
        if(channel!=1 || !s.sysutil_connections.count(UINT64_C(1)<<32) || u32(12)!=28 || u32(68)!=0)
            throw std::runtime_error("invalid disc bootInfo request");
        if(s.fs.disc.empty()) throw std::runtime_error("bootInfo requires ANYPS3_DISC_ROOT mount");
        std::vector<std::uint8_t> response(192,0);
        auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)response[a+j]=v>>(24-j*8);};
        rp(0,0x47444154);rp(4,0x110);rp(8,64);rp(12,60);rp(16,128);rp(24,128);rp(28,31);
        rp(32,160);rp(40,160);rp(48,160);rp(52,32);rp(68,2);
        for(unsigned a=72;a<92;a+=4)rp(a,UINT32_MAX);
        rp(92,9);rp(96,1);rp(108,19);rp(112,6);rp(120,32);
        const char names[]="bootInfo\0elmResult\0elmBootInfo\0";std::copy_n(names,31,response.begin()+128);
        std::copy(s.fs.boot_info.begin(),s.fs.boot_info.end(),response.begin()+160);
        publish(response);return true;
    }
    if (u32(0)==0x50415241 && command=="get_sysparam") {
        if(channel!=0 || !s.sysutil_connections.count(0) || u32(12)!=44 || u32(68)!=1 ||
           string(u32(92))!="id" || u32(96)!=1 || u32(104))
            throw std::runtime_error("invalid system setting request");
        std::uint32_t value=0;
        switch(u32(100)) {
        case 0x111:value=s.settings.language;break;
        case 0x112:value=s.settings.enter_button;break;
        default:throw std::runtime_error("unsupported system setting id "+std::to_string(u32(100)));
        }
        std::vector<std::uint8_t> response(192,0);
        auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)response[a+j]=v>>(24-j*8);};
        rp(0,0x50415241);rp(4,0x110);rp(8,64);rp(12,88);rp(16,160);rp(24,160);rp(28,32);
        rp(32,192);rp(40,192);rp(48,192);rp(68,1);
        for(unsigned a=72;a<84;a+=4)rp(a,UINT32_MAX);
        rp(84,44);rp(88,44);rp(92,13);rp(96,1);rp(108,20);rp(112,1);
        for(unsigned a=120;a<136;a+=4)rp(a,UINT32_MAX);
        rp(136,26);rp(140,1);rp(144,value);
        const char names[]="get_sysparam\0result\0param\0value\0";std::copy_n(names,32,response.begin()+160);
        publish(response);return true;
    }
    if (u32(0)==0x50415241) {
        // PARA: root integer id, one param child with a binary payload.
        if(channel!=0 || !s.sysutil_connections.count(0) || (command!="get" && command!="set") || u32(12)!=104 ||
           u32(68)!=1 || u32(84)!=44 || u32(88)!=44 || string(u32(92))!="id" ||
           u32(96)!=1 || u32(104)!=0 || string(u32(108))!="param" || u32(112)!=2 ||
           u32(116)!=0 || string(u32(136))!="payload" || u32(140)!=6 || u32(152)!=UINT32_MAX)
            throw std::runtime_error("unsupported sysutil parameter shape");
        const auto id=u32(100), binary=u32(48), binary_size=u32(52), at=u32(144), length=u32(148);
        if(binary>size || binary_size>size-binary || at>binary_size || length>binary_size-at)
            throw std::runtime_error("sysutil parameter payload out of bounds");
        std::fprintf(stderr,"native sysutil parameter id=0x%08x length=%u\n",id,length);
        const bool configure=command=="set" && id==0x4b07 && length==28;
        const bool audio_parameter=(command=="get" && (id==0x4c05 || id==0x4c06 || id==0x4c07 || id==0x4c0b)) ||
            (command=="set" && (id==0x4c09 || id==0x4c0a));
        if(!configure && !audio_parameter && !(command=="get" && id==0x4b06 && length==24)) throw std::runtime_error("unsupported native sysutil parameter");
        const auto output=u32(binary+at);
        std::uint32_t result=0;
        std::vector<std::uint8_t> payload(request.begin()+binary+at,request.begin()+binary+at+length);
        if(audio_parameter){
            // Standalone non-audio transports can be composed without NativeAudio. A real audio request
            // in such a transport must stop explicitly; the full process routes to its actual endpoint.
            auto endpoint=[&](auto& state,int)->decltype(state.audio.parameter(id,payload)){
                return state.audio.parameter(id,payload);
            };
            auto absent=[](auto&,long)->std::uint32_t{throw std::runtime_error("audio PARA requires a native audio endpoint");};
            struct Dispatch:decltype(endpoint),decltype(absent){
                using decltype(endpoint)::operator();using decltype(absent)::operator();
            };
            result=Dispatch{endpoint,absent}(s,0);
        }else if(configure){
            const auto resolution=payload[4],format=payload[5],aspect=payload[6];
            const auto pitch=u32(binary+at+16),option=u32(binary+at+20),wait=u32(binary+at+24);
            if(option || wait>1)throw std::runtime_error("unsupported video configuration option");
            if(output)result=0x8002b225;
            else if(!pitch)result=0x8002b223;
            else if(resolution!=2)result=0x8002b226; // Native output currently supports 720p.
            else if(format || aspect>2 || pitch<1280*4 || pitch%64)result=0x8002b221;
            else{
                s.video.resolution=resolution;s.video.format=format;s.video.aspect=aspect?aspect:2;s.video.pitch=pitch;s.rsx.vsync=true;s.rsx.flip_modes[1]=2;
                std::fprintf(stderr,"native video configured 1280x720 pitch=%u aspect=%u\n",pitch,s.video.aspect);
            }
        }else{
            const auto device=u32(binary+at+4);
            std::fill(payload.begin()+8,payload.end(),0);
            if(output>1)result=0x8002b225;
            else if(device)result=0x8002b224;
            else if(output==1)payload[8]=1; // Secondary output is disabled.
            else{
                payload[9]=1; // RGB
                payload[16]=s.video.resolution;payload[17]=1; // Progressive
                payload[19]=s.video.aspect;payload[23]=1; // 59.94 Hz
            }
        }
        // Same tree as the request; root result replaces id. The firmware
        // parses and copies the returned payload itself.
        const unsigned blob=208, padded=(length+15)&~15u;
        std::vector<std::uint8_t> response(blob+padded,0);
        auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j) response[a+j]=v>>(24-j*8);};
        rp(0,0x50415241);rp(4,0x110);rp(8,64);rp(12,104);rp(16,176);rp(24,176);rp(28,25);
        rp(32,blob);rp(40,blob);rp(48,blob);rp(52,padded);rp(68,1);
        for(unsigned a=72;a<84;a+=4)rp(a,UINT32_MAX);
        rp(84,44);rp(88,44);rp(92,4);rp(96,1);rp(100,result);
        rp(108,11);rp(112,2);
        for(unsigned a=120;a<136;a+=4)rp(a,UINT32_MAX);
        rp(136,17);rp(140,6);rp(148,length);rp(152,UINT32_MAX);
        const char names[]="get\0result\0param\0payload\0";std::copy_n(names,25,response.begin()+176);
        if(command=="set")std::copy_n("set",3,response.begin()+176);
        std::copy(payload.begin(),payload.end(),response.begin()+blob);
        publish(response);return true;
    }
    if (u32(0)==0x4e505452 || u32(0)==0x4e504d47) {
        // NPTR tree: struct node = name, leaf-slot count, parent, prev, next,
        // first/last struct child (tree-relative); leaf = name, type, a, b.
        // Unused leaf slots have name UINT32_MAX. Type 6 = (binary offset, size).
        const auto tree=u32(8), tree_size=u32(12), binary=u32(48), binary_size=u32(52);
        if(tree!=64 || tree_size>size-tree || binary>size || binary_size>size-binary)
            throw std::runtime_error("NPTR request tables out of bounds");
        auto node=[&](unsigned rel){if(rel>tree_size || tree_size-rel<28)throw std::runtime_error("NPTR node out of bounds");return tree+rel;};
        auto leaf=[&](unsigned at,const char* name,std::uint32_t type)->std::pair<std::uint32_t,std::uint32_t>{
            const auto count=u32(at+4);
            if(count>(tree+tree_size-at-28)/16)throw std::runtime_error("NPTR leaf table out of bounds");
            for(unsigned i=0;i<count;++i){
                const auto e=at+28+i*16;
                if(u32(e)==UINT32_MAX || string(u32(e))!=name)continue;
                if(u32(e+4)!=type)throw std::runtime_error(std::string("unexpected NPTR leaf type: ")+name);
                return {u32(e+8),u32(e+12)};
            }
            throw std::runtime_error(std::string("missing NPTR leaf: ")+name);
        };
        auto bytes_of=[&](std::pair<std::uint32_t,std::uint32_t> v,std::uint32_t length){
            if(v.second!=length || v.first>binary_size || length>binary_size-v.first)
                throw std::runtime_error("NPTR binary leaf out of bounds");
            return std::vector<std::uint8_t>(request.begin()+binary+v.first,request.begin()+binary+v.first+length);
        };
        struct Leaf{const char* name;std::uint32_t value;std::vector<std::uint8_t> bytes;bool binary;};
        // Root leaves (type 1 int or type 6 binary) and optional empty struct children.
        auto nptr_reply_tree=[&](const std::vector<Leaf>& values,const std::vector<const char*>& children){
            std::string names=command;names.push_back(0);
            std::vector<std::uint32_t> offsets,child_names;
            for(const auto& v:values){offsets.push_back(names.size());names+=v.name;names.push_back(0);}
            for(const auto* c:children){child_names.push_back(names.size());names+=c;names.push_back(0);}
            const unsigned root_bytes=28+16*values.size(),tree_bytes=root_bytes+28*children.size();
            const unsigned str_at=(64+tree_bytes+15)&~15u,binary_at=(str_at+names.size()+15)&~15u;
            std::vector<std::uint8_t> blob;
            std::vector<std::uint32_t> blob_at;
            for(const auto& v:values){blob_at.push_back(blob.size());if(v.binary){blob.insert(blob.end(),v.bytes.begin(),v.bytes.end());blob.resize((blob.size()+15)&~std::size_t(15));}}
            std::vector<std::uint8_t> response(binary_at+blob.size(),0);
            auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)response[a+j]=v>>(24-j*8);};
            rp(0,u32(0));rp(4,0x110);rp(8,64);rp(12,tree_bytes);rp(16,str_at);rp(24,str_at);rp(28,names.size());
            rp(32,binary_at);rp(40,binary_at);rp(48,binary_at);rp(52,blob.size());rp(68,values.size());
            for(unsigned a=72;a<84;a+=4)rp(a,UINT32_MAX);
            rp(84,children.empty()?UINT32_MAX:root_bytes);rp(88,children.empty()?UINT32_MAX:root_bytes+28*(children.size()-1));
            for(unsigned i=0;i<values.size();++i){
                const auto e=92+i*16;rp(e,offsets[i]);
                if(values[i].binary){rp(e+4,6);rp(e+8,blob_at[i]);rp(e+12,values[i].bytes.size());}
                else{rp(e+4,1);rp(e+8,values[i].value);}
            }
            for(unsigned i=0;i<children.size();++i){
                const auto n=64+root_bytes+28*i;rp(n,child_names[i]);rp(n+8,0);
                rp(n+12,i?root_bytes+28*(i-1):UINT32_MAX);rp(n+16,i+1<children.size()?root_bytes+28*(i+1):UINT32_MAX);
                rp(n+20,UINT32_MAX);rp(n+24,UINT32_MAX);
            }
            std::copy(names.begin(),names.end(),response.begin()+str_at);
            std::copy(blob.begin(),blob.end(),response.begin()+binary_at);
            publish(response);
        };
        auto nptr_reply=[&](const std::vector<std::pair<const char*,std::uint32_t>>& values){
            std::vector<Leaf> leaves;for(const auto& v:values)leaves.push_back({v.first,v.second,{},false});
            nptr_reply_tree(leaves,{});
        };
        if(u32(0)==0x4e504d47){
            // Firmware 3.01 libsysutil_np, TOC 0x201f9180. All commands below
            // use the empty-root builder 0x201e229c and result parser 0x201e24a0.
            // The status query reads root int "status" at 0x201d6768, not param.
            const bool registration=command=="managerRegisterCallbackExt";
            const bool unregistration=command=="managerUnregisterCallbackExt";
            const bool status=command=="managerGetStatus";
            // These account/network getters branch away from their payload
            // parsers when result is negative. RPCS3 sceNp.cpp @ ffba2bf09
            // documents SCE_NP_ERROR_OFFLINE for each corresponding public API.
            const bool offline=command=="managerGetNpId" || command=="managerGetSubHandle" ||
                command=="managerGetUserIconURL" || command=="managerGetMyLanguages" ||
                command=="managerGetNetworkTime" || command=="managerGetPsHandle" ||
                command=="managerGetSubjectStatus" || command=="managerGetSubjectRegion";
            if(!registration && !unregistration && !status && !offline)
                throw std::runtime_error("unsupported native NPMG command: "+command);
            // Validate the complete no-parameter tree before changing the ring
            // or subscription state. Table capacities/padding are not payload.
            if(channel!=0 || tree_size!=28 || strings!=96 || string_size!=command.size()+1 ||
               u32(16)!=strings || u32(20) || u32(32)!=size || u32(36) ||
               u32(40)!=size || u32(44) || binary!=size || binary_size || u32(56) || u32(60) ||
               size!=((strings+string_size+15)&~15u) || u32(64) || u32(68))
                throw std::runtime_error("unsupported NPMG request shape: "+command);
            for(unsigned a=72;a<92;a+=4)if(u32(a)!=UINT32_MAX)
                throw std::runtime_error("unsupported NPMG root links: "+command);
            unsigned port=UINT32_MAX;
            for(auto connection:s.sysutil_connections)if((connection>>32)==0){
                if(port!=UINT32_MAX)throw std::runtime_error("ambiguous NPMG sysutil client");
                port=static_cast<std::uint32_t>(connection);
            }
            if(port>=s.settings.np_manager_callbacks.size())
                throw std::runtime_error("NPMG request without sysutil client connection");
            if(registration || unregistration){
                const auto key=UINT64_C(0x8006010000000100)|port;
                if(std::none_of(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return q.key==key;}))
                    throw std::runtime_error("NPMG callback references absent event queue");
                // Register/unregister the subscription. No PSN state transitions
                // exist in the offline runtime, so there are no status events.
                // Firmware routes future manager events via callback type 263
                // (0x201d8010); it stores the user's OPD only after result >= 0
                // (0x201d7ca8). Do not invent an initial login/ticket event.
                nptr_reply({{"result",0}});
                s.settings.np_manager_callbacks[port]=registration;
            }else if(status){
                nptr_reply({{"result",0},{"status",UINT32_MAX}}); // OFFLINE = -1.
            }else{
                nptr_reply({{"result",0x8002aa0c}}); // SCE_NP_ERROR_OFFLINE, no payload.
            }
            return true;
        }
        if(channel!=1 || !s.sysutil_connections.count(UINT64_C(1)<<32))
            throw std::runtime_error("NPTR request without sysutil client connection");
        const auto root=node(0);
        if(u32(root+8)!=UINT32_MAX || u32(root+12)!=UINT32_MAX || u32(root+16)!=UINT32_MAX)
            throw std::runtime_error("invalid NPTR root node");
        if(command=="f"){
            // sceNpTrophyCreateContext. One struct child (npCommId), leaves for
            // signature and options; validation follows SceNpCommunicationId/
            // Signature layout and the per-title TROPDIR configuration.
            if(u32(root+20)!=u32(root+24))throw std::runtime_error("unsupported NPTR create-context shape");
            const auto id=node(u32(root+20));
            if(string(u32(id))!="npCommId" || u32(id+8)!=0 || u32(id+12)!=UINT32_MAX || u32(id+16)!=UINT32_MAX ||
               u32(id+20)!=UINT32_MAX || u32(id+24)!=UINT32_MAX)
                throw std::runtime_error("unsupported NPTR npCommId node");
            const auto data=bytes_of(leaf(id,"np_comm_id_data",6),9);
            const auto num=bytes_of(leaf(id,"np_comm_id_num",6),1)[0];
            const auto sign=bytes_of(leaf(root,"np_comm_sign",6),160);
            const auto options=bytes_of(leaf(root,"options",6),8);
            std::uint64_t option=0;for(auto b:options)option=(option<<8)|b;
            std::string name(data.begin(),std::find(data.begin(),data.end(),0));
            const bool valid_sign=sign[0]==0xb9 && sign[1]==0xdd && sign[2]==0xe1 && sign[3]==0x3b &&
                                  sign[4]==1 && sign[5]==0 && std::all_of(sign.begin()+6,sign.begin()+12,[](auto b){return b==0;});
            std::uint32_t result=0,context=0;
            if(option>1)result=0x80022903;
            else if(num>99 || !valid_sign || name.empty() ||
                    name.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")!=std::string::npos)result=0x80022918;
            else{
                char suffix[4];std::snprintf(suffix,sizeof suffix,"_%02u",unsigned(num));
                const auto conf=name+suffix;
                if(s.fs.disc.empty())throw std::runtime_error("trophy context requires ANYPS3_DISC_ROOT mount");
                std::error_code error;
                const auto host=s.fs.resolve("/dev_bdvd/PS3_GAME/TROPDIR/"+conf+"/TROPHY.TRP").host;
                if(!std::filesystem::is_regular_file(host,error))result=0x8002291b;
                else{
                    auto& t=s.trophy;
                    for(unsigned i=0;i<t.contexts.size() && !context;++i)
                        if(t.contexts[i].empty()){t.contexts[i]=conf;t.read_only[i]=option==1;context=i+1;}
                    if(!context)result=0x80022907;
                }
                std::fprintf(stderr,"native trophy context conf=%s context=%u result=%08x\n",conf.c_str(),context,result);
            }
            nptr_reply({{"result",result},{"context",context}});
            return true;
        }
        if(command=="8"){
            // sceNpTrophyCreateHandle: no parameters, reply leaf mem_context = handle.
            const auto count=u32(root+4);
            if(u32(root+20)!=UINT32_MAX || u32(root+24)!=UINT32_MAX || count>(tree_size-28)/16)
                throw std::runtime_error("unsupported NPTR create-handle shape");
            for(unsigned i=0;i<count;++i)if(u32(root+28+i*16+4))throw std::runtime_error("unexpected NPTR create-handle parameter");
            std::uint32_t result=0x80022907,handle=0;
            for(unsigned i=0;i<s.trophy.handles.size();++i)if(!s.trophy.handles[i]){s.trophy.handles[i]=true;s.trophy.aborted[i]=false;handle=i+1;result=0;break;}
            std::fprintf(stderr,"native trophy handle=%u result=%08x\n",handle,result);
            nptr_reply({{"result",result},{"mem_context",handle}});
            return true;
        }
        if(command=="1"){
            // sceNpTrophyDestroyContext: single context leaf.
            if(u32(root+20)!=UINT32_MAX || u32(root+24)!=UINT32_MAX)throw std::runtime_error("unsupported NPTR destroy-context shape");
            const auto context=leaf(root,"context",1).first;
            auto& t=s.trophy;std::uint32_t result=0;
            if(!context || context>4 || t.contexts[context-1].empty())result=0x8002290a;
            else{t.contexts[context-1].clear();t.read_only[context-1]=false;t.registered[context-1]=false;}
            std::fprintf(stderr,"native trophy destroy context=%u result=%08x\n",context,result);
            nptr_reply({{"result",result}});
            return true;
        }
        if(command=="term"){
            // sceNpTrophyTerm: no parameters; releases this process's contexts and handles.
            const auto count=u32(root+4);
            if(u32(root+20)!=UINT32_MAX || u32(root+24)!=UINT32_MAX || count>(tree_size-28)/16)
                throw std::runtime_error("unsupported NPTR term shape");
            for(unsigned i=0;i<count;++i)if(u32(root+28+i*16+4))throw std::runtime_error("unexpected NPTR term parameter");
            s.trophy=NativeTrophy{};
            std::fprintf(stderr,"native trophy term\n");
            nptr_reply({{"result",0}});
            return true;
        }
        if(command=="9"){
            // sceNpTrophyDestroyHandle: single mem_context (handle) leaf.
            if(u32(root+20)!=UINT32_MAX || u32(root+24)!=UINT32_MAX)throw std::runtime_error("unsupported NPTR destroy-handle shape");
            const auto handle=leaf(root,"mem_context",1).first;
            std::uint32_t result=0;
            if(!handle || handle>4 || !s.trophy.handles[handle-1])result=0x80022911;
            else{s.trophy.handles[handle-1]=false;s.trophy.aborted[handle-1]=false;}
            std::fprintf(stderr,"native trophy destroy handle=%u result=%08x\n",handle,result);
            nptr_reply({{"result",result}});
            return true;
        }
        if(command=="o" || command=="h" || command=="i"){
            // GetRequiredDiskSpace ("o"), RegisterContext status ("h") and
            // installation ("i"). Leaves: context, mem_context (= handle) and,
            // for h/i, the 8-byte options value.
            if(u32(root+20)!=UINT32_MAX || u32(root+24)!=UINT32_MAX)throw std::runtime_error("unsupported NPTR context request shape");
            const auto context=leaf(root,"context",1).first,handle=leaf(root,"mem_context",1).first;
            if(command!="o"){
                const auto options=bytes_of(leaf(root,"options",6),8);
                if(std::any_of(options.begin(),options.end(),[](auto b){return b!=0;}))
                    throw std::runtime_error("unsupported trophy registration options");
            }
            auto& t=s.trophy;
            std::uint32_t result=0;
            if(!context || context>4 || t.contexts[context-1].empty())result=0x8002290a;
            else if(!handle || handle>4 || !t.handles[handle-1])result=0x80022911;
            else if(t.aborted[handle-1])result=0x80022910;
            if(result){
                if(command=="o")nptr_reply_tree({{"result",result,{},false},{"req_space",0,std::vector<std::uint8_t>(8),true}},{});
                else if(command=="h")nptr_reply({{"result",result},{"status",0}});
                else nptr_reply({{"result",result}});
                return true;
            }
            const auto conf=t.contexts[context-1];
            if(s.fs.disc.empty() || s.fs.hdd_root.empty())throw std::runtime_error("trophy registration requires disc and HDD roots");
            const auto trp_path=s.fs.resolve("/dev_bdvd/PS3_GAME/TROPDIR/"+conf+"/TROPHY.TRP").host;
            const auto installed_dir=s.fs.hdd_root/"home"/"00000001"/"trophy"/conf;
            const bool installed=std::filesystem::is_regular_file(installed_dir/"TROPCONF.SFM");
            // TRP v1 container: 0x40-byte header, then fixed-size entries of
            // name[32], offset u64, size u64. Encrypted versions are rejected.
            std::ifstream trp(trp_path,std::ios::binary);
            std::vector<std::uint8_t> header(0x40);
            if(!trp.read(reinterpret_cast<char*>(header.data()),header.size()))throw std::runtime_error("trophy TRP header read failed");
            auto be=[](const std::uint8_t* p,unsigned n){std::uint64_t v=0;for(unsigned i=0;i<n;++i)v=(v<<8)|p[i];return v;};
            const auto file_size=be(&header[8],8),files=be(&header[16],4),element=be(&header[20],4);
            if(be(&header[0],4)!=0xdca24d00 || be(&header[4],4)!=1 || element!=0x40 || !files || files>256 ||
               file_size!=std::filesystem::file_size(trp_path) || file_size<0x40+files*element)
                throw std::runtime_error("unsupported trophy TRP container");
            if(command=="o"){
                const std::uint64_t space=installed?0:file_size-0x40-files*element;
                std::vector<std::uint8_t> bytes(8);for(unsigned i=0;i<8;++i)bytes[i]=space>>(56-8*i);
                std::fprintf(stderr,"native trophy required space conf=%s bytes=%llu\n",conf.c_str(),static_cast<unsigned long long>(space));
                nptr_reply_tree({{"result",0,{},false},{"req_space",0,bytes,true}},{});
                return true;
            }
            if(command=="h"){
                const std::uint32_t status=installed?3:1; // INSTALLED : NOT_INSTALLED
                std::fprintf(stderr,"native trophy status conf=%s status=%u\n",conf.c_str(),status);
                nptr_reply({{"result",0},{"status",status}});
                return true;
            }
            std::vector<std::uint8_t> entries(files*element);
            if(!trp.read(reinterpret_cast<char*>(entries.data()),entries.size()))throw std::runtime_error("trophy TRP entry table read failed");
            std::filesystem::create_directories(installed_dir.parent_path());
            const auto temp=installed_dir.parent_path()/(conf+".installing");
            std::filesystem::remove_all(temp);std::filesystem::create_directory(temp);
            unsigned trophies=0;
            for(unsigned i=0;i<files;++i){
                const auto* e=&entries[i*element];
                const std::string name(reinterpret_cast<const char*>(e),std::find(e,e+32,0)-e);
                const auto offset=be(e+32,8),length=be(e+40,8);
                if(name.empty() || name.find_first_of("/\\")!=std::string::npos || name=="." || name==".." ||
                   offset<0x40+files*element || offset>file_size || length>file_size-offset)
                    throw std::runtime_error("invalid trophy TRP entry");
                std::vector<char> data(length);trp.seekg(offset);
                if(!trp.read(data.data(),data.size()))throw std::runtime_error("trophy TRP entry read failed");
                std::ofstream out(temp/name,std::ios::binary);
                if(!out.write(data.data(),data.size()))throw std::runtime_error("trophy install write failed");
                if(name.size()==11 && name.rfind("TROP",0)==0 && name.substr(7)==".PNG")++trophies;
            }
            if(!std::filesystem::is_regular_file(temp/"TROPCONF.SFM"))throw std::runtime_error("trophy TRP lacks TROPCONF.SFM");
            // Re-registration must preserve the real unlock history.
            if(std::filesystem::exists(installed_dir/"TROPUSR.DAT"))
                std::filesystem::copy_file(installed_dir/"TROPUSR.DAT",temp/"TROPUSR.DAT");
            if(std::filesystem::exists(installed_dir)){
                if(::syscall(SYS_renameat2,AT_FDCWD,temp.c_str(),AT_FDCWD,installed_dir.c_str(),RENAME_EXCHANGE))
                    throw std::runtime_error("trophy install directory exchange failed");
                std::filesystem::remove_all(temp);
            }else std::filesystem::rename(temp,installed_dir);
            t.registered[context-1]=true;
            std::fprintf(stderr,"native trophy installed conf=%s files=%u trophy_icons=%u\n",conf.c_str(),unsigned(files),trophies);
            // VSH progress events (sysutil type 299): data2=context<<32|status,
            // data3=completed<<32|total. Delivered via cellSysutilCheckCallback.
            std::uint32_t port=UINT32_MAX;
            for(auto c:s.sysutil_connections)if((c>>32)==0)port=static_cast<std::uint32_t>(c);
            if(port==UINT32_MAX)throw std::runtime_error("trophy events require sysutil channel 0 connection");
            const auto key=UINT64_C(0x8006010000000100)|port;
            auto status_event=[&](std::uint64_t status,std::uint64_t done,std::uint64_t total){
                s.events.post(key,{0,299,(std::uint64_t(context)<<32)|status,(done<<32)|total});
            };
            status_event(5,0,0);
            for(unsigned i=1;i<=trophies;++i)status_event(6,i,trophies);
            status_event(7,0,0);status_event(8,0,0);
            nptr_reply_tree({{"result",0,{},false}},{"platinumId"});
            return true;
        }
        if(std::getenv("ANYPS3_SYSUTIL_TRACE")){
            std::fprintf(stderr,"NPTR request words:");
            for(unsigned i=0;i+4<=size && i<1024;i+=4)std::fprintf(stderr," %08x",u32(i));
            std::fprintf(stderr,"\n");
        }
        throw std::runtime_error("unsupported native NPTR command: "+command);
    }
    if (u32(0)==0x4e504452 && command=="drmrequest") {
        // sceNpDrm request from libsysutil_np 0x201df0c4..0x201df328 (firmware 3.01): the reply is a
        // root with int leaf "result"; nonzero is returned to the caller at once, zero means accepted
        // with asynchronous completion. Observed reqtype 0: reqarg = {u32, k_licensee[16], path}.
        if(u32(68)!=2 || u32(12)!=60 || u32(84)!=UINT32_MAX || string(u32(92))!="reqtype" || u32(96)!=1 ||
           string(u32(108))!="reqarg" || u32(112)!=6 || u32(116) || u32(120)!=0x118 || u32(100)!=0)
            dump("unsupported NPDR drmrequest shape");
        const auto arg=u32(32)+u32(116);
        if(arg>size || size-arg<0x118)dump("NPDR drmrequest argument out of bounds");
        std::string path;
        for(unsigned i=20;i<0x118 && request[arg+i];++i)path.push_back(static_cast<char>(request[arg+i]));
        if(path.size()>=0x118-20)dump("unterminated NPDR drm path");
        std::uint32_t result=0;
        try{
            const auto host=s.fs.resolve(path);struct stat st{};
            if(::stat(host.host.c_str(),&st))result=NativeFilesystem::host_error(errno);
        }catch(const NativeFilesystem::Error& error){result=error.code;}
        // A present file would need EDAT/SELF license verification, which is not implemented.
        if(!result)throw std::runtime_error("NPDR drmrequest for existing "+path+": license verification is not implemented");
        // Missing file: the open error is reported (RPCS3 sceNp.cpp npDrmIsAvailable, derived; VSH unobserved).
        std::string names=command;names.push_back(0);
        const unsigned result_name=names.size();names+="result";names.push_back(0);
        const unsigned tree_size=28+16,str_at=(64+tree_size+15)&~15u,end=(str_at+names.size()+15)&~15u;
        std::vector<std::uint8_t> response(end,0);
        auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)response[a+j]=v>>(24-j*8);};
        rp(0,0x4e504452);rp(4,0x110);rp(8,64);rp(12,tree_size);rp(16,str_at);rp(24,str_at);rp(28,names.size());
        rp(32,end);rp(40,end);rp(48,end);
        rp(64,0);rp(68,1);for(unsigned a=72;a<92;a+=4)rp(a,UINT32_MAX);
        rp(92,result_name);rp(96,1);rp(100,result);
        std::copy(names.begin(),names.end(),response.begin()+str_at);
        std::fprintf(stderr,"native npdrm request path=%s result=%08x\n",path.c_str(),result);
        publish(response);return true;
    }
    if (u32(0)==0x4e54434c && command=="getstate") {
        // libnetctl cellNetCtlGetState (firmware 3.01 libnetctl 0x201a22e8-0x201a23a0): the reply is read
        // as leaf "result" (0x201a25d4) and int "state" inside child "param". The runtime provides no
        // network service, so the truthful state is CELL_NET_CTL_STATE_Disconnected (0).
        if(channel!=0 || u32(12)!=44 || u32(68)!=1 || u32(84)!=UINT32_MAX || u32(92)!=UINT32_MAX || u32(96) || u32(100) || u32(104))
            dump("unsupported NTCL getstate request shape");
        std::string names=command;names.push_back(0);
        const unsigned result_name=names.size();names+="result";names.push_back(0);
        const unsigned param_name=names.size();names+="param";names.push_back(0);
        const unsigned state_name=names.size();names+="state";names.push_back(0);
        const unsigned tree_size=(28+16)+(28+16),str_at=(64+tree_size+15)&~15u,end=(str_at+names.size()+15)&~15u;
        std::vector<std::uint8_t> response(end,0);
        auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)response[a+j]=v>>(24-j*8);};
        rp(0,0x4e54434c);rp(4,0x110);rp(8,64);rp(12,tree_size);rp(16,str_at);rp(24,str_at);rp(28,names.size());
        rp(32,end);rp(40,end);rp(48,end);
        // Root node at tree offset 0: one int leaf, one child at offset 44.
        rp(64,0);rp(68,1);rp(72,UINT32_MAX);rp(76,UINT32_MAX);rp(80,UINT32_MAX);rp(84,44);rp(88,44);
        rp(92,result_name);rp(96,1);rp(100,0);
        // Child "param": parent 0, no siblings/children, one int leaf "state".
        rp(108,param_name);rp(112,1);rp(116,0);rp(120,UINT32_MAX);rp(124,UINT32_MAX);rp(128,UINT32_MAX);rp(132,UINT32_MAX);
        rp(136,state_name);rp(140,1);rp(144,0);
        std::copy(names.begin(),names.end(),response.begin()+str_at);
        std::fprintf(stderr,"native netctl getstate state=0 (disconnected; no host network service)\n");
        publish(response);return true;
    }
    if (u32(0)==0x55535249 && command=="getStat") {
        // cellUserInfoGetStat (firmware 3.01 libsysutil_userinfo 0x20140a84..0x20140d60): request root "getStat",
        // one int leaf elmUserId (0 = current user). The client reads the reply root's int leaves elmResult
        // (0x20140c9c) and elmSelectedUsrId (0x20140cd0, stored to stat->id) and string leaf elmSelectedUsrName
        // (0x20140d34, copied to stat->name[64]); the root name is not checked. The name is the console's
        // /dev_hdd0/home/%08u/localusername (RPCS3 cellUserInfo.cpp:69-107). NOUSER/INTERNAL VSH codes are
        // unverified, so an absent user or username file stops explicitly instead of inventing a result.
        if(channel!=1 || !s.sysutil_connections.count(UINT64_C(1)<<32) || u32(12)!=44 || u32(68)!=1 ||
           string(u32(92))!="elmUserId" || u32(96)!=1 || u32(104))
            dump("unsupported USRI getStat request shape");
        const auto id=u32(100)?u32(100):1u; // CELL_SYSUTIL_USERID_CURRENT; the runtime's single user is 00000001.
        if(id>99999999 || s.fs.hdd_root.empty())dump("unsupported USRI getStat user/HDD");
        char home[16];std::snprintf(home,sizeof(home),"%08u",id);
        const auto file=std::filesystem::path(s.fs.hdd_root)/"home"/home/"localusername";
        if(!std::filesystem::is_regular_file(file))
            throw std::runtime_error("USRI getStat: missing "+file.string()+" (absent user or username; VSH error code unverified)");
        std::ifstream in(file,std::ios::binary);std::string name((std::istreambuf_iterator<char>(in)),{});
        if(!in.good() && !in.eof())throw std::runtime_error("USRI getStat: cannot read "+file.string());
        if(name.empty() || name.size()>63 || name.find('\0')!=std::string::npos)
            throw std::runtime_error("USRI getStat: invalid localusername");
        auto response=savedata_vsh::reply("getStat",{{"elmResult",0},{"elmSelectedUsrId",id}},"",{},"elmSelectedUsrName",name);
        for(unsigned j=0;j<4;++j)response[j]=static_cast<std::uint8_t>(0x55535249u>>(24-8*j));
        std::fprintf(stderr,"native userinfo getStat id=%u name=%s\n",id,name.c_str());
        publish(response);return true;
    }
    if (u32(0)!=0x434d4e44) {
        if(std::getenv("ANYPS3_SYSUTIL_TRACE")){
            std::fprintf(stderr,"sysutil request words:");
            for(unsigned i=0;i+4<=size && i<1024;i+=4)std::fprintf(stderr," %08x",u32(i));
            std::fprintf(stderr,"\n");
        }
        std::fprintf(stderr,"unsupported sysutil tag=0x%08x",u32(0));
        if (u32(68) && u32(12)>=44 && u32(92)<string_size && string(u32(92))=="id" && u32(96)==1)
            std::fprintf(stderr," parameter_id=0x%08x",u32(100));
        std::fprintf(stderr,"\n");
        throw std::runtime_error("unsupported native sysutil request family: "+command);
    }
    if (command!="connect") throw std::runtime_error("unsupported native sysutil command: "+command);
    const auto count=u32(68);
    if (u32(12)!=28+16*count || (channel==0 && count!=3) || (channel==1 && count!=0))
        throw std::runtime_error("unexpected connect attributes");
    std::uint32_t port=UINT32_MAX,pid=0,mcid=0; unsigned seen=0;
    if (channel==1) {
        for (auto connection:s.sysutil_connections) if ((connection>>32)==0) {
            if (port!=UINT32_MAX) throw std::runtime_error("ambiguous sysutil client");
            port=static_cast<std::uint32_t>(connection); pid=1; mcid=UINT32_MAX; seen=7;
        }
    }
    for (unsigned i=0;i<count;++i) {
        const auto a=92+i*16; const auto name=string(u32(a)); const auto type=u32(a+4), value=u32(a+8);
        if (type!=1 || u32(a+12)) throw std::runtime_error("unsupported connect attribute type");
        unsigned bit=0;
        if (name=="port") {port=value;bit=1;} else if (name=="pid") {pid=value;bit=2;} else if (name=="mcid") {mcid=value;bit=4;}
        if (!bit || (seen&bit)) throw std::runtime_error("unknown/duplicate connect attribute");
        seen|=bit;
    }
    if (seen!=7 || pid!=1 || port>3 || mcid!=UINT32_MAX)
        throw std::runtime_error("unsupported sysutil connection parameters");
    bool found=false;
    for (const auto& q:s.events.queues) if(q.key==(UINT64_C(0x8006010000000100)|port)) found=true;
    if (!found) throw std::runtime_error("sysutil connection references absent event queue");
    const std::uint64_t connection=(std::uint64_t(channel)<<32)|port;
    if (!s.sysutil_connections.insert(connection).second) throw std::runtime_error("duplicate sysutil connection");
    // Encode a CMND root with one integer attribute, result=0. This success
    // acknowledges only the native connection recorded above.
    std::vector<std::uint8_t> response(128,0);
    auto rp=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j) response[a+j]=static_cast<std::uint8_t>(v>>(24-j*8));};
    rp(0,0x434d4e44);rp(4,0x110);rp(8,64);rp(12,44);rp(16,112);rp(24,112);rp(28,15);
    rp(32,128);rp(40,128);rp(48,128);rp(68,1);
    for(unsigned a=72;a<92;a+=4) rp(a,UINT32_MAX);
    rp(92,8);rp(96,1);
    const char names[]="connect\0result\0";std::copy_n(names,15,response.begin()+112);
    // Consume the request and publish a complete reply in the same duplex ring.
    publish(response);
    return true;
}
'''
