"""Optional SDL2 scanout sink; no SDL SDK or load without ANYPS3_PRESENT=sdl.

SDL2 public ABI: SDL_events.h (WindowEvent/Event, PeepEvents), SDL_pixels.h
(RGBA32), SDL_render.h (streaming textures), SDL_video.h (window flags).
Only video is acquired/released here; pad and audio own their subsystem refs.
"""

from .vsh_overlay import TYPES as OVERLAY_TYPES

TYPES = OVERLAY_TYPES + r'''
#include <dlfcn.h>
#include <cstring>
struct NativePresent {
    using Clock=std::chrono::steady_clock;
    struct WindowEvent {
        std::uint32_t type,timestamp,window_id;
        std::uint8_t event,padding1,padding2,padding3;
        std::int32_t data1,data2;
    };
    union Event {
        std::uint32_t type;WindowEvent window;void* alignment;
        std::uint8_t padding[sizeof(void*)<=8?56:sizeof(void*)==16?64:3*sizeof(void*)];
    };
    // SDL_PIXELFORMAT_RGBA32: RGBA bytes regardless of host integer endian.
    static std::uint32_t rgba_format(){
        const std::uint16_t probe=1;
        return *reinterpret_cast<const std::uint8_t*>(&probe)?0x16762004u:0x16462004u;
    }
    bool enabled=false,subsystem=false,closed=false;
    void* library=nullptr;void* window=nullptr;void* renderer=nullptr;void* texture=nullptr;
    unsigned width=0,height=0;std::uint32_t window_id=0;
    std::vector<std::uint8_t> pixels;
    std::uint64_t presented_frames=0;Clock::time_point last_present{};
    std::function<void(const std::function<void()>&)> host_dispatch;
    void host_call(const std::function<void()>& invoke){if(host_dispatch)host_dispatch(invoke);else invoke();}
    int (*init)(std::uint32_t)=nullptr;void (*quit)(std::uint32_t)=nullptr;
    const char* (*error)()=nullptr;
    void* (*create_window)(const char*,int,int,int,int,std::uint32_t)=nullptr;
    void (*destroy_window)(void*)=nullptr;std::uint32_t (*get_window_id)(void*)=nullptr;
    void* (*create_renderer)(void*,int,std::uint32_t)=nullptr;void (*destroy_renderer)(void*)=nullptr;
    int (*logical_size)(void*,int,int)=nullptr;
    void* (*create_texture)(void*,std::uint32_t,int,int,int)=nullptr;void (*destroy_texture)(void*)=nullptr;
    int (*update_texture)(void*,const void*,const void*,int)=nullptr;
    int (*clear)(void*)=nullptr;int (*copy)(void*,void*,const void*,const void*)=nullptr;
    void (*present)(void*)=nullptr;void (*pump)()=nullptr;
    int (*peep)(Event*,int,int,std::uint32_t,std::uint32_t)=nullptr;
    NativePresent(){const auto* v=std::getenv("ANYPS3_PRESENT");enabled=v && std::strcmp(v,"sdl")==0;}
    NativePresent(const NativePresent&)=delete;NativePresent& operator=(const NativePresent&)=delete;
    template<class T> T symbol(const char* name){
        auto* address=dlsym(library,name);
        if(!address)throw std::runtime_error(std::string("missing SDL2 presentation symbol: ")+name);
        return reinterpret_cast<T>(address);
    }
    [[noreturn]] void fail(const char* operation){
        throw std::runtime_error(std::string("SDL2 presentation ")+operation+": "+error());
    }
    void shutdown(){
        if(texture){destroy_texture(texture);texture=nullptr;}
        if(renderer){destroy_renderer(renderer);renderer=nullptr;}
        if(window){destroy_window(window);window=nullptr;}
        if(subsystem){quit(0x20);subsystem=false;}
        width=height=0;window_id=0;
    }
    ~NativePresent(){shutdown();if(library)dlclose(library);}
    void open(unsigned w,unsigned h){
        if(closed)throw std::runtime_error("SDL2 presentation window closed; run terminated");
        if(!w || !h || w>4096 || h>4096)throw std::runtime_error("invalid SDL2 presentation dimensions");
        if(!library){
            library=dlopen("libSDL2-2.0.so.0",RTLD_NOW|RTLD_LOCAL);
            if(!library)throw std::runtime_error("SDL2 presentation requires SDL2 shared library");
            init=symbol<decltype(init)>("SDL_InitSubSystem");quit=symbol<decltype(quit)>("SDL_QuitSubSystem");
            error=symbol<decltype(error)>("SDL_GetError");
            create_window=symbol<decltype(create_window)>("SDL_CreateWindow");
            destroy_window=symbol<decltype(destroy_window)>("SDL_DestroyWindow");
            get_window_id=symbol<decltype(get_window_id)>("SDL_GetWindowID");
            create_renderer=symbol<decltype(create_renderer)>("SDL_CreateRenderer");
            destroy_renderer=symbol<decltype(destroy_renderer)>("SDL_DestroyRenderer");
            logical_size=symbol<decltype(logical_size)>("SDL_RenderSetLogicalSize");
            create_texture=symbol<decltype(create_texture)>("SDL_CreateTexture");
            destroy_texture=symbol<decltype(destroy_texture)>("SDL_DestroyTexture");
            update_texture=symbol<decltype(update_texture)>("SDL_UpdateTexture");
            clear=symbol<decltype(clear)>("SDL_RenderClear");copy=symbol<decltype(copy)>("SDL_RenderCopy");
            present=symbol<decltype(present)>("SDL_RenderPresent");
            pump=symbol<decltype(pump)>("SDL_PumpEvents");peep=symbol<decltype(peep)>("SDL_PeepEvents");
        }
        try{
            if(!subsystem){if(init(0x20))fail("video initialization failed");subsystem=true;}
            if(!window){
                window=create_window("AnyPS3 guest scanout",0x2fff0000,0x2fff0000,w,h,0x20);
                if(!window)fail("window open failed");
                window_id=get_window_id(window);
                // No vsync wait on the guest thread. Dummy/offscreen can use software.
                renderer=create_renderer(window,-1,2);
                if(!renderer)renderer=create_renderer(window,-1,1);
                if(!renderer)fail("renderer open failed");
            }
            if(w!=width || h!=height){
                if(texture){destroy_texture(texture);texture=nullptr;}
                texture=create_texture(renderer,rgba_format(),1,w,h);
                if(!texture)fail("streaming texture open failed");
                if(logical_size(renderer,w,h))fail("logical size failed");
                pixels.resize(std::size_t(w)*h*4);width=w;height=h;
            }
        }catch(...){shutdown();throw;}
    }
    void poll_events(){
        pump();Event e{};
        for(const auto type:{0x100u,0x200u}){
            int count;
            while((count=peep(&e,1,2,type,type))>0){ // SDL_GETEVENT, only QUIT/WINDOWEVENT.
                if(e.type==0x100 || (e.window.event==14 && e.window.window_id==window_id)){
                    closed=true;shutdown();
                    throw std::runtime_error("SDL2 presentation window closed; run terminated");
                }
            }
            if(count<0)fail("event read failed");
        }
    }
    void upload(){
        if(update_texture(texture,nullptr,pixels.data(),width*4))fail("texture upload failed");
    }
    bool present_if_due(Clock::time_point now){
        if(presented_frames && now-last_present<std::chrono::nanoseconds(16666667))return false;
        if(clear(renderer))fail("render clear failed");
        if(copy(renderer,texture,nullptr,nullptr))fail("render copy failed");
        present(renderer);++presented_frames;last_present=now;return true;
    }
};
'''

CPP = r'''
[[maybe_unused]] static void rsx_present_display(PpuState& s,unsigned id){
    auto& p=s.present;
    if(s.video.format!=0)throw std::runtime_error("unsupported RSX scanout format");
    if(id>=s.rsx.display_buffers.size())throw std::runtime_error("invalid RSX display buffer");
    const auto& buffer=s.rsx.display_buffers[id];
    p.host_call([&]{p.open(buffer.width,buffer.height);p.poll_events();});
    // Reuse the capture converter and storage; upload every flip, present at most 60 Hz.
    rsx_display_pixels(s,id,p.pixels);anyps3_vsh_overlay::composite(p.pixels,buffer.width,buffer.height);p.host_call([&]{p.upload();p.present_if_due(NativePresent::Clock::now());});
}
'''
