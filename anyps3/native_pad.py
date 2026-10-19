"""Native SDL2 controller input and the supported cellPad ABI subset.

SDL is loaded on first cellPadInit, so offline translation needs no SDL SDK.
No controller is invented when the host has none. Motion and rumble are not
advertised. ABI reference: local RPCS3 cellPad.h and SDL2 public headers.
"""

TYPES = r'''
#include <dlfcn.h>
struct NativePad {
    struct Port {
        void* device=nullptr;int instance=-1;bool changed=false,reported=false;
        unsigned setting=0;std::array<std::uint16_t,20> previous{};
    };
    void* library=nullptr;unsigned maximum=0;std::array<Port,7> ports{};
    int (*init)(std::uint32_t)=nullptr;void (*quit)(std::uint32_t)=nullptr;
    int (*count)()=nullptr;int (*is_controller)(int)=nullptr;
    int (*device_id)(int)=nullptr;void* (*open)(int)=nullptr;
    void (*close)(void*)=nullptr;void (*update)()=nullptr;
    int (*attached)(void*)=nullptr;std::uint8_t (*button)(void*,int)=nullptr;
    std::int16_t (*axis)(void*,int)=nullptr;
    NativePad()=default;NativePad(const NativePad&)=delete;NativePad& operator=(const NativePad&)=delete;
    template<class T> T symbol(const char* name){
        auto* address=dlsym(library,name);
        if(!address)throw std::runtime_error(std::string("missing SDL2 input symbol: ")+name);
        return reinterpret_cast<T>(address);
    }
    void load(){
        if(library)return;
        library=dlopen("libSDL2-2.0.so.0",RTLD_NOW|RTLD_LOCAL);
        if(!library)throw std::runtime_error("native controller input requires SDL2 shared library");
        init=symbol<decltype(init)>("SDL_InitSubSystem");quit=symbol<decltype(quit)>("SDL_QuitSubSystem");
        count=symbol<decltype(count)>("SDL_NumJoysticks");is_controller=symbol<decltype(is_controller)>("SDL_IsGameController");
        device_id=symbol<decltype(device_id)>("SDL_JoystickGetDeviceInstanceID");
        open=symbol<decltype(open)>("SDL_GameControllerOpen");close=symbol<decltype(close)>("SDL_GameControllerClose");
        update=symbol<decltype(update)>("SDL_GameControllerUpdate");attached=symbol<decltype(attached)>("SDL_GameControllerGetAttached");
        button=symbol<decltype(button)>("SDL_GameControllerGetButton");axis=symbol<decltype(axis)>("SDL_GameControllerGetAxis");
    }
    void shutdown(){
        for(auto& p:ports){if(p.device)close(p.device);p=Port{};}
        if(maximum)quit(0x2000);
        maximum=0;
    }
    ~NativePad(){shutdown();if(library)dlclose(library);}
    unsigned poll(){
        update();
        for(auto& p:ports)if(p.device && !attached(p.device)){
            close(p.device);p.device=nullptr;p.instance=-1;p.changed=true;p.reported=false;
        }
        const auto total=count();
        if(total<0)throw std::runtime_error("SDL2 controller enumeration failed");
        for(int i=0;i<total;++i){
            if(!is_controller(i))continue;
            const auto id=device_id(i);if(id<0)continue;
            if(std::any_of(ports.begin(),ports.end(),[&](const auto& p){return p.device && p.instance==id;}))continue;
            for(unsigned j=0;j<std::min(maximum,7u);++j)if(!ports[j].device){
                auto& p=ports[j];p.device=open(i);
                if(!p.device)throw std::runtime_error("SDL2 controller open failed");
                p.instance=id;p.changed=true;p.reported=false;break;
            }
        }
        return std::count_if(ports.begin(),ports.end(),[](const auto& p){return p.device!=nullptr;});
    }
    std::array<std::uint16_t,20> sample(unsigned index){
        auto* d=ports[index].device;std::array<std::uint16_t,20> data{};
        const int digital1[]={4,7,8,6,11,14,12,13};
        const int digital2[]={-1,-1,9,10,3,1,0,2};
        for(unsigned i=0;i<8;++i){
            if(button(d,digital1[i]))data[2]|=1u<<i;
            if(i>=2 && button(d,digital2[i]))data[3]|=1u<<i;
        }
        const auto left=std::max(0,int(axis(d,4))),right=std::max(0,int(axis(d,5)));
        if(left>0)data[3]|=1;
        if(right>0)data[3]|=2;
        for(unsigned i=0;i<4;++i)data[4+i]=(int(axis(d,(i+2)%4))+32768)/256;
        const unsigned pressure_word[]={2,2,2,2,3,3,3,3,3,3};
        const unsigned pressure_bit[]={5,7,4,6,4,5,6,7,2,3};
        for(unsigned i=0;i<10;++i)data[8+i]=(data[pressure_word[i]]&(1u<<pressure_bit[i]))?255:0;
        data[18]=(left*255+16383)/32767;data[19]=(right*255+16383)/32767;
        return data;
    }
};
'''

CPP = r'''
// VSH dialog pad interception (native_vsh_ui.py). Minimal harnesses without
// settings.vsh_ui (present tests) have no system UI and see no interception.
template<class State,class=void> struct PadHasVshUi:std::false_type {};
template<class State> struct PadHasVshUi<State,std::void_t<decltype(std::declval<State&>().settings.vsh_ui)>>:std::true_type {};
template<class State> static unsigned pad_vsh_system_info(State& s){
    if constexpr(PadHasVshUi<State>::value)return vsh_ui_pad_system_info(s.settings.vsh_ui);else return 0;
}
template<class State> static bool pad_vsh_intercept(State& s,unsigned operation){
    if constexpr(PadHasVshUi<State>::value)return vsh_ui_pad_intercept(s,operation);else return false;
}
[[maybe_unused]] static std::uint32_t pad_service(PpuState& s,unsigned operation){
    auto& p=s.pad;auto& r=s.r;auto& m=s.memory;
    if(operation==0){
        if(p.maximum)return 0x80121103;
        if(!r[3] || r[3]>127)return 0x80121102;
        p.load();if(p.init(0x2000))return 0x80121105;
        p.maximum=r[3];const auto connected=p.poll();
        std::fprintf(stderr,"native pad initialized ports=%u connected=%u backend=SDL2\n",std::min(p.maximum,7u),connected);
        return 0;
    }
    if(!p.maximum)return 0x80121104;
    if(operation==1){p.shutdown();return 0;}
    if(operation==2){
        if(!r[3])return 0x80121102;
        const auto connected=p.poll();auto* out=m.access(r[3],124,true);std::fill_n(out,124,0);
        m.store(r[3],4,std::min(p.maximum,7u));m.store(r[3]+4,4,connected);
        // system_info: CELL_PAD_INFO_INTERCEPTED while an interactive VSH dialog owns the pad.
        m.store(r[3]+8,4,pad_vsh_system_info(s));
        for(unsigned i=0;i<std::min(p.maximum,7u);++i){
            auto& port=p.ports[i];
            m.store(r[3]+12+i*4,4,(port.device?1:0)|(port.changed?2:0));
            m.store(r[3]+40+i*4,4,port.setting);
            // PS3_CONFORMITY (1): the SDL mapping provides the full standard PS3
            // digital button set and both sticks; HP_ANALOG_STICK (8). No pressure,
            // motion or rumble is claimed (RPCS3 Io/pad_types.h:136-140). The game
            // (BCES00511 0x65d7f4) ignores ports without the conformity bit.
            if(port.device)m.store(r[3]+68+i*4,4,1|8);
            port.changed=false;
        }
        return 0;
    }
    const auto index=r[3];if(index>=127)return 0x80121102;
    if(operation==3){
        if(index>=7)return 0;
        if(r[4]&4)throw std::runtime_error("native controller motion input is not implemented");
        p.ports[index].setting=r[4]&2;p.ports[index].reported=false;return 0;
    }
    if(operation==5){ // cellPadSetActDirect(port, CellPadActParam{u8 motor[2]; u8 reserved[6];})
        if(!r[4])return 0x80121102;
        if(std::int32_t(s.sdk_version)>0x1fffff)
            for(unsigned i=2;i<8;++i)if(m.load(r[4]+i,1))return 0x80121102;
        p.poll();if(index>=std::min(p.maximum,7u) || !p.ports[index].device)return 0x80121107;
        return 0x80121108; // GetInfo2 advertises no actuator: CELL_PAD_ERROR_UNSUPPORTED_GAMEPAD.
    }
    if(operation!=4)throw std::runtime_error("unsupported native pad operation");
    if(!r[4])return 0x80121102;
    p.poll();if(index>=std::min(p.maximum,7u) || !p.ports[index].device)return 0x80121107;
    // VSH dialog: NO_CHANGE while intercepted, neutral data until release (RPCS3 cellPad.cpp:362-375).
    if(pad_vsh_intercept(s,operation))return 0;
    auto& port=p.ports[index];auto data=p.sample(index);
    const unsigned length=(port.setting&2)?20:8;
    data[1]=0x70|(length/2);
    if(!(port.setting&2))std::fill(data.begin()+8,data.end(),0);
    if(port.reported && data==port.previous){m.store(r[4],4,0);return 0;}
    m.access(r[4],44,true);m.store(r[4],4,length);
    for(unsigned i=0;i<20;++i)m.store(r[4]+4+i*2,2,data[i]);
    // Diagnostic only: ANYPS3_PAD_TRACE=1 logs every reported (changed) state.
    static const bool trace=[]{const char* v=std::getenv("ANYPS3_PAD_TRACE");return v && std::string(v)=="1";}();
    if(trace)std::fprintf(stderr,"native pad data port=%u len=%u digital1=%04x digital2=%04x sticks=%u,%u,%u,%u\n",
        unsigned(index),length,unsigned(data[2]),unsigned(data[3]),unsigned(data[4]),unsigned(data[5]),unsigned(data[6]),unsigned(data[7]));
    port.previous=data;port.reported=true;return 0;
}
'''
