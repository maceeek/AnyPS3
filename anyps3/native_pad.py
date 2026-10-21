"""Native SDL2 controller input and the supported cellPad ABI subset.

SDL is loaded on first cellPadInit, so offline translation needs no SDL SDK.
No controller or sensor readings are invented. Optional SDL symbols gate each
capability. PS3 ABI/scales: RPCS3 ffba2bf09ef3980638f9327b25240d81414034ab;
see docs/manager/DUALSENSE.md for versioned file:line provenance.
"""

TYPES = r'''
#include <dlfcn.h>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <mutex>
#include <thread>
struct NativePad {
    struct Port {
        void* device=nullptr;int instance=-1;bool changed=false,reported=false;
        unsigned setting=0;std::array<std::uint16_t,24> previous{};
        bool actuator=false,sensors=false,sensors_enabled=false,feedback_error=false;
        std::uint16_t low=0,high=0;
        std::chrono::steady_clock::time_point last_report{};
    };
    void* library=nullptr;unsigned maximum=0;std::array<Port,7> ports{};
    int (*init)(std::uint32_t)=nullptr;void (*quit)(std::uint32_t)=nullptr;
    int (*count)()=nullptr;int (*is_controller)(int)=nullptr;
    int (*device_id)(int)=nullptr;void* (*open)(int)=nullptr;
    void (*close)(void*)=nullptr;void (*update)()=nullptr;
    int (*attached)(void*)=nullptr;std::uint8_t (*button)(void*,int)=nullptr;
    std::int16_t (*axis)(void*,int)=nullptr;
    int (*rumble)(void*,std::uint16_t,std::uint16_t,std::uint32_t)=nullptr;
    int (*has_rumble)(void*)=nullptr;int (*has_sensor)(void*,int)=nullptr;
    int (*set_sensor)(void*,int,int)=nullptr;int (*get_sensor)(void*,int,float*,int)=nullptr;
    std::recursive_mutex mutex;
    std::thread feedback;std::atomic<bool> feedback_stop{false};
    std::mutex feedback_mutex;std::condition_variable feedback_wake;
    const bool rumble_allowed=[]{const char* v=std::getenv("ANYPS3_PAD_RUMBLE");return !v || std::string(v)!="0";}();
    // Host scheduling choices, not PS3 constants: renew a 1s SDL effect every
    // 250ms, including while the guest makes no more cellPad calls.
    static constexpr unsigned rumble_duration_ms=1000,rumble_refresh_ms=250;
    NativePad()=default;NativePad(const NativePad&)=delete;NativePad& operator=(const NativePad&)=delete;
    template<class T> T symbol(const char* name){
        auto* address=dlsym(library,name);
        if(!address)throw std::runtime_error(std::string("missing SDL2 input symbol: ")+name);
        return reinterpret_cast<T>(address);
    }
    template<class T> T optional_symbol(const char* name){return reinterpret_cast<T>(dlsym(library,name));}
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
        rumble=optional_symbol<decltype(rumble)>("SDL_GameControllerRumble");
        has_rumble=optional_symbol<decltype(has_rumble)>("SDL_GameControllerHasRumble");
        has_sensor=optional_symbol<decltype(has_sensor)>("SDL_GameControllerHasSensor");
        set_sensor=optional_symbol<decltype(set_sensor)>("SDL_GameControllerSetSensorEnabled");
        get_sensor=optional_symbol<decltype(get_sensor)>("SDL_GameControllerGetSensorData");
    }
    bool stop_effect(Port& p){
        const bool stopped=!(p.device && (p.low || p.high) && rumble) || rumble(p.device,0,0,0)==0;
        p.low=p.high=0;return stopped;
    }
    void start_feedback(){
        if(feedback.joinable())return;
        feedback_stop=false;
        try{feedback=std::thread([this]{
            std::unique_lock<std::mutex> wait(feedback_mutex);
            while(!feedback_wake.wait_for(wait,std::chrono::milliseconds(rumble_refresh_ms),[this]{return feedback_stop.load();})){
                // Shutdown joins with mutex held. Never wait for it here.
                std::unique_lock<std::recursive_mutex> lock(mutex,std::try_to_lock);
                if(!lock.owns_lock())continue;
                for(auto& p:ports)if(p.device && (p.low || p.high)){
                    if(!attached(p.device)){stop_effect(p);continue;}
                    if(rumble(p.device,p.low,p.high,rumble_duration_ms)){
                        stop_effect(p);p.feedback_error=true;
                    }
                }
            }
        });}catch(...){for(auto& p:ports)stop_effect(p);throw;}
    }
    bool sensor_mode(Port& p,bool enabled){
        if(!p.device || !attached(p.device) || !p.sensors || p.sensors_enabled==enabled)return true;
        const int on=enabled?1:0,old=p.sensors_enabled?1:0;
        if(set_sensor(p.device,1,on))return false; // SDL_SENSOR_ACCEL = 1
        if(set_sensor(p.device,2,on)){ // SDL_SENSOR_GYRO = 2
            if(set_sensor(p.device,1,old))throw std::runtime_error("SDL2 sensor mode rollback failed");
            return false;
        }
        p.sensors_enabled=enabled;return true;
    }
    bool shutdown(){
        std::lock_guard<std::recursive_mutex> lock(mutex);
        feedback_stop=true;feedback_wake.notify_all();if(feedback.joinable())feedback.join();
        bool stopped=true;
        for(auto& p:ports){stopped=stop_effect(p) && stopped;if(p.device)close(p.device);p=Port{};}
        if(maximum)quit(0x2000);
        maximum=0;return stopped;
    }
    ~NativePad(){shutdown();if(library)dlclose(library);}
    unsigned poll(){
        std::lock_guard<std::recursive_mutex> lock(mutex);
        update();
        for(auto& p:ports)if(p.device && !attached(p.device)){
            stop_effect(p);close(p.device);
            const auto setting=p.setting;p=Port{};p.setting=setting;p.changed=true;
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
                p.instance=id;p.changed=true;p.reported=false;
                // HasRumble arrived in 2.0.18. Do not probe with Rumble(0,0):
                // SDL release-2.0.14 src/joystick/SDL_joystick.c:807-814
                // returns success for unchanged amplitudes without a driver call.
                p.actuator=rumble_allowed && rumble && has_rumble && has_rumble(p.device);
                p.sensors=has_sensor && set_sensor && get_sensor && has_sensor(p.device,1) && has_sensor(p.device,2);
                if(!sensor_mode(p,(p.setting&4)!=0)){
                    close(p.device);p.device=nullptr;
                    throw std::runtime_error("SDL2 controller sensor activation failed on connect");
                }
                break;
            }
        }
        for(const auto& p:ports)if(p.feedback_error)throw std::runtime_error("SDL2 controller rumble refresh failed");
        return std::count_if(ports.begin(),ports.end(),[](const auto& p){return p.device!=nullptr;});
    }
    std::array<std::uint16_t,24> sample(unsigned index){
        std::lock_guard<std::recursive_mutex> lock(mutex);
        auto* d=ports[index].device;std::array<std::uint16_t,24> data{};
        const int digital1[]={4,7,8,6,11,14,12,13};
        const int digital2[]={-1,-1,9,10,3,1,0,2};
        for(unsigned i=0;i<8;++i){
            if(button(d,digital1[i]))data[2]|=1u<<i;
            if(i>=2 && button(d,digital2[i]))data[3]|=1u<<i;
        }
        // SDL_CONTROLLER_BUTTON_GUIDE = 5; RPCS3 Emu/Io/pad_types.h:114,
        // Digital1Flags carries CELL_PAD_CTRL_PS in word 2, bit 8.
        if(button(d,5))data[2]|=0x100;
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
    std::uint32_t motion(unsigned index,std::array<std::uint16_t,24>& data){
        auto& p=ports[index];if(!p.sensors || !p.sensors_enabled)return 0x80121108;
        float accel[3],gyro[3];
        if(get_sensor(p.device,1,accel,3) || get_sensor(p.device,2,gyro,3))return 0x80121106;
        for(unsigned i=0;i<3;++i)if(!std::isfinite(accel[i]) || !std::isfinite(gyro[i]))return 0x80121106;
        const auto ten_bit=[](float v){return std::uint16_t(std::clamp(v,0.0f,1023.0f));};
        // RPCS3 @ ffba2bf: Input/sdl_pad_handler.cpp:667-688;
        // Emu/Io/pad_types.h:362 (113/g), PadHandler.cpp:174-177 (truncate).
        // SDL2 SDL_sensor.h:71-109: m/s^2, gravity included; +X right,
        // +Y up, +Z toward player. PS3 acceleration inverts all three axes.
        // 9.80665f = SDL_STANDARD_GRAVITY (SDL_sensor.h:139).
        for(unsigned i=0;i<3;++i)data[20+i]=ten_bit(512.0f-(accel[i]/9.80665f)*113.0f);
        // Positive SDL Y yaw (rad/s) -> PS3 G: +123 per +90 degrees/s.
        constexpr float rad_to_degree=57.29577951308232f; // 180/pi, unit conversion
        data[23]=ten_bit(512.0f+gyro[1]*rad_to_degree*(123.0f/90.0f));
        return 0;
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
    std::lock_guard<std::recursive_mutex> lock(p.mutex);
    if(operation==0){
        if(p.maximum)return 0x80121103;
        if(!r[3] || r[3]>127)return 0x80121102;
        p.load();if(p.init(0x2000))return 0x80121105;
        p.maximum=r[3];const auto connected=p.poll();
        std::fprintf(stderr,"native pad initialized ports=%u connected=%u backend=SDL2\n",std::min(p.maximum,7u),connected);
        return 0;
    }
    if(!p.maximum)return 0x80121104;
    if(operation==1)return p.shutdown()?0:0x80121101; // FATAL if SDL could not stop an active motor
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
            // RPCS3 Emu/Io/pad_types.h:136-140: conformity=1, sensor=4,
            // HP sticks=8, actuator=16. SDL has no native PS3 pressure buttons.
            if(port.device)m.store(r[3]+68+i*4,4,1|8|(port.sensors?4:0)|(port.actuator?16:0));
            port.changed=false;
        }
        return 0;
    }
    const auto index=r[3];if(index>=127)return 0x80121102;
    if(operation==3){
        if(index>=7)return 0;
        // RPCS3 cellPad.cpp:1047-1066 accepts settings without a capability
        // or connection check (unlike cellPadSetSensorMode). No fake samples:
        // GetData returns UNSUPPORTED_GAMEPAD if SENSOR_ON cannot be supplied.
        auto& port=p.ports[index];
        if(!p.sensor_mode(port,(r[4]&4)!=0))return 0x80121108;
        port.setting=r[4]&6;port.reported=false;return 0;
    }
    if(operation==5){ // cellPadSetActDirect(port, CellPadActParam{u8 motor[2]; u8 reserved[6];})
        if(!r[4])return 0x80121102;
        if(std::int32_t(s.sdk_version)>0x1fffff)
            for(unsigned i=2;i<8;++i)if(m.load(r[4]+i,1))return 0x80121102;
        p.poll();if(index>=std::min(p.maximum,7u) || !p.ports[index].device)return 0x80121107;
        auto& port=p.ports[index];if(!port.actuator)return 0x80121108;
        // PSL1GHT io/pad.h:210-213: small = on/off, large = 0..255.
        // SDL low-frequency = large; high-frequency = small (0 or 65535).
        const auto low=std::uint16_t(m.load(r[4]+1,1)*257),high=std::uint16_t(m.load(r[4],1)?65535:0);
        if(p.rumble(port.device,low,high,(low || high)?NativePad::rumble_duration_ms:0))return 0x80121108;
        port.low=low;port.high=high;if(low || high)p.start_feedback();
        return 0;
    }
    if(operation!=4)throw std::runtime_error("unsupported native pad operation");
    if(!r[4])return 0x80121102;
    p.poll();if(index>=std::min(p.maximum,7u) || !p.ports[index].device)return 0x80121107;
    // VSH dialog: NO_CHANGE while intercepted, neutral data until release (RPCS3 cellPad.cpp:362-375).
    if(pad_vsh_intercept(s,operation))return 0;
    auto& port=p.ports[index];auto data=p.sample(index);
    if(port.setting&4){const auto error=p.motion(index,data);if(error)return error;}
    // RPCS3 cellPad.h:30-32, pad_types.h:274-277: 8 / 20 / 24 words.
    const unsigned length=(port.setting&4)?24:(port.setting&2)?20:8;
    data[1]=0x70|(length/2);
    if(!(port.setting&2))std::fill(data.begin()+8,data.begin()+20,0);
    const auto now=std::chrono::steady_clock::now();
    // RPCS3 cellPad.cpp:539-550: unchanged sensor data repeats every ~10ms.
    if(port.reported && data==port.previous && (!(port.setting&4) || now-port.last_report<std::chrono::milliseconds(10))){m.store(r[4],4,0);return 0;}
    const unsigned words=(port.setting&4)?24:20;
    m.access(r[4],4+words*2,true);m.store(r[4],4,length);
    for(unsigned i=0;i<words;++i)m.store(r[4]+4+i*2,2,data[i]);
    // Diagnostic only: ANYPS3_PAD_TRACE=1 logs every reported (changed) state.
    static const bool trace=[]{const char* v=std::getenv("ANYPS3_PAD_TRACE");return v && std::string(v)=="1";}();
    if(trace)std::fprintf(stderr,"native pad data port=%u len=%u digital1=%04x digital2=%04x sticks=%u,%u,%u,%u\n",
        unsigned(index),length,unsigned(data[2]),unsigned(data[3]),unsigned(data[4]),unsigned(data[5]),unsigned(data[6]),unsigned(data[7]));
    port.previous=data;port.reported=true;port.last_report=now;return 0;
}
'''
