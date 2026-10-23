"""Native cellAudio rings and firmware 3.01 audio output transport.

ABI/lifecycle: RPCS3 ffba2bf cellAudio.h:88 and cellAudio.cpp:1520-1925.
Only the public 2/8-channel, 8/16/32-block, attr=0/INITLEVEL path is supported.
Notify events use the existing LV2 queues. Unknown routing remains an explicit stop.
"""

TYPES = r'''
#include <dlfcn.h>
#include <cstring>
#include <cmath>
#include <deque>
struct EventQueueState;
struct NativeAudio {
    struct Port {
        unsigned status=0x1010,channels=0,blocks=0,position=0;
        std::uint32_t address=0,index=0,size=0;float level=1.0f,target_level=1.0f,level_step=0;
        unsigned level_remaining=0;
        std::uint64_t tag=0,timestamp_ticks=0;bool has_timestamp=false;
        struct Stamp {std::uint64_t tag,ticks;};
        std::deque<Stamp> history;
    };
    // SDL2 SDL_audio.h:177; copied ABI avoids requiring SDL development headers.
    struct Spec {
        int freq;std::uint16_t format;std::uint8_t channels,silence;
        std::uint16_t samples,padding;std::uint32_t size;
        void (*callback)(void*,std::uint8_t*,int);void* userdata;
    };
    std::array<Port,8> ports{};
    struct Notify {std::uint64_t key,source,start;std::uint32_t flags;std::shared_ptr<EventQueueState> queue;};
    std::vector<Notify> notifications;
    unsigned output_channels=8,downmixer=0,copy_control=0,host_channels=8;
    std::uint64_t counter=0,notify_dropped=0,nonzero_frames=0;
    std::FILE* capture=nullptr;std::uint32_t capture_bytes=0;unsigned capture_channels=0;
    void capture_header(){
        // Standard little-endian 16-bit PCM WAV, captured after host routing.
        std::array<unsigned char,44> h{};
        auto le=[&](unsigned at,std::uint32_t n,unsigned size){for(unsigned i=0;i<size;++i)h[at+i]=n>>(i*8);};
        std::memcpy(h.data(),"RIFF",4);le(4,36+capture_bytes,4);std::memcpy(h.data()+8,"WAVEfmt ",8);
        le(16,16,4);le(20,1,2);le(22,host_channels,2);le(24,48000,4);
        le(28,48000*host_channels*2,4);le(32,host_channels*2,2);le(34,16,2);
        std::memcpy(h.data()+36,"data",4);le(40,capture_bytes,4);
        if(std::fseek(capture,0,SEEK_SET) || std::fwrite(h.data(),1,h.size(),capture)!=h.size() ||
           std::fflush(capture) || std::fseek(capture,0,SEEK_END))throw std::runtime_error("audio WAV header write failed");
    }
    void open_capture(){
        if(capture){if(capture_channels!=host_channels)throw std::runtime_error("audio WAV channel count changed within a capture");return;}
        const auto* path=std::getenv("ANYPS3_AUDIO_CAPTURE");if(!path || !*path)return;
        // Never truncate a previous artifact, including accidental sink reopen.
        capture=std::fopen(path,"wbx");
        if(!capture)throw std::runtime_error(std::string("audio WAV capture open failed: ")+path);
        capture_bytes=0;capture_channels=host_channels;
        try{capture_header();}catch(...){std::fclose(capture);capture=nullptr;throw;}
        std::fprintf(stderr,"native audio capture PCM16 rate=48000 channels=%u path=%s\n",host_channels,path);
    }
    void capture_pcm(const float* data,unsigned samples){
        if(!capture)return;
        if(samples>(UINT32_MAX-36-capture_bytes)/2)throw std::runtime_error("audio WAV capture exceeds RIFF size limit");
        std::vector<unsigned char> bytes(samples*2);
        for(unsigned i=0;i<samples;++i){
            const auto v=static_cast<std::int16_t>(std::lround(std::max(-1.0f,std::min(1.0f,data[i]))*32767.0f));
            bytes[i*2]=std::uint16_t(v)&255;bytes[i*2+1]=std::uint16_t(v)>>8;
        }
        if(std::fwrite(bytes.data(),1,bytes.size(),capture)!=bytes.size())throw std::runtime_error("audio WAV PCM write failed");
        capture_bytes+=bytes.size();capture_header();
    }
    bool sink_ready=false;
    unsigned maximum=0,running=0;std::uint32_t indices=0,device=0;
    std::uint64_t next_tick=0,submitted_frames=0,late_dropped_frames=0;
    // Host-output diagnostics for PERF AUDIO: worst period lateness and SDL queue depth since the previous report.
    mutable std::uint64_t interval_late_ticks=0,interval_queued_bytes=0;
    bool null_sink=false,null_logged=false,subsystem=false;
    void* library=nullptr;
    int (*init)(std::uint32_t)=nullptr;void (*quit)(std::uint32_t)=nullptr;
    std::uint32_t (*open)(const char*,int,const Spec*,Spec*,int)=nullptr;
    void (*close)(std::uint32_t)=nullptr;void (*pause)(std::uint32_t,int)=nullptr;
    int (*queue)(std::uint32_t,const void*,std::uint32_t)=nullptr;
    std::uint32_t (*queued)(std::uint32_t)=nullptr;const char* (*error)()=nullptr;
    std::uint32_t parameter(unsigned,std::vector<std::uint8_t>&);
    NativeAudio()=default;NativeAudio(const NativeAudio&)=delete;
    NativeAudio& operator=(const NativeAudio&)=delete;
    template<class T> T symbol(const char* name){
        auto* address=dlsym(library,name);
        if(!address)throw std::runtime_error(std::string("missing SDL2 audio symbol: ")+name);
        return reinterpret_cast<T>(address);
    }
    void shutdown_sink(){
        sink_ready=false;
        if(device){close(device);device=0;}
        if(subsystem){quit(0x10);subsystem=false;}
    }
    ~NativeAudio(){shutdown_sink();if(capture)std::fclose(capture);if(library)dlclose(library);}
    unsigned requested_channels(){
        const auto* channels=std::getenv("ANYPS3_AUDIO_HOST_CHANNELS");
        if(!channels || std::string(channels)=="8")return 8;
        if(std::string(channels)=="2")return 2;
        throw std::runtime_error("ANYPS3_AUDIO_HOST_CHANNELS must be 2 or 8");
    }
    void open_sink(){
        if(sink_ready)return;
        const auto* selected=std::getenv("ANYPS3_AUDIO_SINK");
        if(!selected)selected="null";
        if(std::string(selected)=="null"){
            null_sink=true;
            if(!null_logged){
                std::fprintf(stderr,"native audio: diagnostic null sink; PCM discarded, not evidence of audible audio\n");
                null_logged=true;
            }
            host_channels=requested_channels();
            open_capture();sink_ready=true;return;
        }
        if(std::string(selected)!="sdl")throw std::runtime_error("unsupported ANYPS3_AUDIO_SINK (expected sdl or null)");
        // Machine rule (user, 2026-10-17): this PC must never emit audible sound. SDL may only use its silent
        // drivers; a real output device needs the operator's explicit ANYPS3_AUDIO_AUDIBLE=1 opt-in.
        const char* driver=std::getenv("SDL_AUDIODRIVER");const char* audible=std::getenv("ANYPS3_AUDIO_AUDIBLE");
        const bool silent=driver && (std::string(driver)=="dummy" || std::string(driver)=="disk");
        if(!silent && !(audible && std::string(audible)=="1"))
            throw std::runtime_error("SDL2 audio to a real output device is disabled: use SDL_AUDIODRIVER=dummy or disk "
                                     "(audible output requires an explicit ANYPS3_AUDIO_AUDIBLE=1)");
        const unsigned requested=requested_channels();
        null_sink=false;
        if(!library){
            library=dlopen("libSDL2-2.0.so.0",RTLD_NOW|RTLD_LOCAL);
            if(!library)throw std::runtime_error("native audio requires SDL2 shared library");
        }
        init=symbol<decltype(init)>("SDL_InitSubSystem");quit=symbol<decltype(quit)>("SDL_QuitSubSystem");
        open=symbol<decltype(open)>("SDL_OpenAudioDevice");close=symbol<decltype(close)>("SDL_CloseAudioDevice");
        pause=symbol<decltype(pause)>("SDL_PauseAudioDevice");queue=symbol<decltype(queue)>("SDL_QueueAudio");
        queued=symbol<decltype(queued)>("SDL_GetQueuedAudioSize");error=symbol<decltype(error)>("SDL_GetError");
        if(init(0x10))throw std::runtime_error(std::string("SDL2 audio initialization failed: ")+error());
        subsystem=true;
        const std::uint16_t endian_probe=1;
        const std::uint16_t format=(*reinterpret_cast<const std::uint8_t*>(&endian_probe))?0x8120:0x9120;
        Spec desired{48000,format,static_cast<std::uint8_t>(requested),0,256,0,0,nullptr,nullptr},obtained{};
        device=open(nullptr,0,&desired,&obtained,0);
        if(!device){
            desired.channels=2;
            device=open(nullptr,0,&desired,&obtained,0);
        }
        if(!device){
            const std::string message=std::string("SDL2 audio device open failed: ")+error();
            shutdown_sink();throw std::runtime_error(message);
        }
        if(obtained.freq!=48000 || obtained.format!=format || (obtained.channels!=8 && obtained.channels!=2)){
            shutdown_sink();throw std::runtime_error("SDL2 audio did not provide 48 kHz float32 stereo/8-channel output");
        }
        host_channels=obtained.channels;
        try{open_capture();}catch(...){shutdown_sink();throw;}
        sink_ready=true;
        pause(device,0);
        std::fprintf(stderr,"native audio initialized sink=SDL2 rate=48000 channels=%u (8->2 downmix if needed)\n",host_channels);
    }
    // late: the period's playback time is already AUDIO_LATE_LIMIT past (the host stalled audio service). Its guest
    // effects (block consumption, tags, notify events) still happen; only host output skips it, as the hardware
    // would have played it during the stall. Queueing it would add the stall to the host latency permanently and a
    // stall >= 2 s tripped the queue stop (user run 20261023T134218).
    void submit(const std::array<float,256*8>& pcm,bool late=false){
        if(late){late_dropped_frames+=256;return;}
        std::array<float,256*2> stereo{};
        const bool downmix=host_channels==2 || output_channels==2;
        if(downmix)for(unsigned i=0;i<256;++i){stereo[i*2]=pcm[i*8];stereo[i*2+1]=pcm[i*8+1];}
        const float* data=pcm.data();unsigned bytes=sizeof(pcm);
        std::array<float,256*8> routed{};
        if(downmix){
            if(host_channels==2){data=stereo.data();bytes=sizeof(stereo);}
            else {for(unsigned i=0;i<256;++i){routed[i*8]=stereo[i*2];routed[i*8+1]=stereo[i*2+1];}data=routed.data();}
        }
        // Queueing successful PCM is separate evidence from device enumeration.
        unsigned nonzero=0;for(unsigned i=0;i<bytes/4;++i)if(data[i]!=0)++nonzero;
        if(!null_sink){
            const auto depth=queued(device);interval_queued_bytes=std::max<std::uint64_t>(interval_queued_bytes,depth);
            if(depth>48000u*host_channels*4*2)throw std::runtime_error("SDL2 audio output queue exceeds two seconds");
            if(queue(device,data,bytes))throw std::runtime_error(std::string("SDL2 audio queue failed: ")+error());
        }
        capture_pcm(data,bytes/4);
        if(nonzero){
            if(!nonzero_frames)std::fprintf(stderr,"native audio PCM nonzero sink=%s frames=256 nonzero_samples=%u\n",null_sink?"null (DISCARDED)":"SDL2",nonzero);
            nonzero_frames+=256;
        }
        submitted_frames+=256;
    }
};
'''

CPP = r'''
static constexpr std::uint32_t AUDIO_ALREADY_INIT=0x80310701,AUDIO_NOT_INIT=0x80310703,
    AUDIO_PARAM=0x80310704,AUDIO_PORT_FULL=0x80310705,AUDIO_ALREADY_RUN=0x80310706,
    AUDIO_NOT_OPEN=0x80310707,AUDIO_NOT_RUN=0x80310708,AUDIO_SHAREDMEMORY=0x8031070b;
static constexpr std::uint64_t AUDIO_PERIOD_TICKS=TIMEBASE_HZ*256/48000;
// A period submitted more than 50 ms after its end is no longer sent to the host (NativeAudio::submit).
static constexpr std::uint64_t AUDIO_LATE_LIMIT_TICKS=TIMEBASE_HZ/20;
static_assert(TIMEBASE_HZ*256%48000==0,"audio period must be exact in timebase ticks");

// Same budget, page alignment and reservation-aware search as sys_memory_allocate.
// Audio owns these mappings; guest sys_memory_free must not free internal rings.
static std::uint32_t audio_allocate(PpuState& s,std::uint32_t size){
    const std::uint64_t aligned=(std::uint64_t(size)+0xffff)&~UINT64_C(0xffff);
    const auto used=main_memory_used(s);
    if(used>0x10000000 || aligned>0x10000000-used)return 0;
    std::uint64_t base=0x10000000;
    for(;;){
        auto next=base;
        for(const auto& region:s.memory.regions)
            if(base<region.base+region.bytes->size() && region.base<base+aligned)next=std::max(next,region.base+region.bytes->size());
        for(const auto& area:s.mmapper.areas)
            if(base<area.base+area.size && area.base<base+aligned)next=std::max(next,area.base+area.size);
        if(next==base)break;
        base=(next+0xffff)&~UINT64_C(0xffff);
    }
    if(base+aligned>0xc0000000)return 0;
    try{s.memory.map(base,aligned,6,nullptr,0);}catch(const std::bad_alloc&){return 0;}
    return static_cast<std::uint32_t>(base);
}
static void audio_free(PpuState& s,std::uint32_t address){
    GuestMemory::ConfigurationPermit configuration(s.memory);
    auto& m=s.memory;
    const auto it=std::find_if(m.regions.begin(),m.regions.end(),[&](const auto& region){return region.base==address;});
    if(it==m.regions.end())throw std::runtime_error("missing native audio allocation");
    m.mark_write(it->bytes->data(),it->bytes->size());
    m.allocated-=it->bytes->size();m.regions.erase(it);
}
static void audio_close(PpuState& s,NativeAudio::Port& p){
    if(p.status==2)--s.audio.running;
    audio_free(s,p.address);p.status=0x1010;
    // Preserve config fields on closed ports, as cellAudioGetPortConfig does.
}
[[maybe_unused]] static void audio_advance(PpuState& s,std::uint64_t now){
    auto& a=s.audio;auto& m=s.memory;
    if(!a.maximum || (!a.running && a.notifications.empty()))return;
    while(now>=a.next_tick){
        std::array<float,256*8> mixed{};
        // RPCS3 guest layout and SDL_audio.h: FL FR FC LFE rearL rearR sideL sideR.
        constexpr unsigned output_channel[]={0,1,2,3,4,5,6,7};
        for(auto& p:a.ports)if(p.status==2){
            const auto block=p.address+p.position*p.channels*256*4;
            std::vector<std::uint8_t> snapshot;
            const std::uint8_t* input;
            if(m.concurrent()){snapshot.resize(p.channels*256*4);m.transfer(block,snapshot.data(),snapshot.size(),false);input=snapshot.data();}
            else input=m.access(block,p.channels*256*4,false);
            for(unsigned frame=0;frame<256;++frame){
              if(p.level_remaining){p.level+=p.level_step;if(!--p.level_remaining)p.level=p.target_level;}
              for(unsigned channel=0;channel<p.channels;++channel){
                const auto* b=input+(frame*p.channels+channel)*4;
                const std::uint32_t bits=(std::uint32_t(b[0])<<24)|(std::uint32_t(b[1])<<16)|(std::uint32_t(b[2])<<8)|b[3];
                float value;std::memcpy(&value,&bits,4);
                if(!std::isfinite(value))throw std::runtime_error("non-finite guest cellAudio PCM sample");
                value*=p.level;
                if(p.channels==8 && (a.output_channels==2 || a.host_channels==2)){
                    // RPCS3 ffba2bf cellAudio.cpp:1438: exclude LFE, Dolby fold.
                    if(channel<2)mixed[frame*8+channel]+=value*0.707f;
                    else if(channel==2){mixed[frame*8]+=value*0.5f;mixed[frame*8+1]+=value*0.5f;}
                    else if(channel>=4)mixed[frame*8+(channel&1)]+=value*0.5f;
                }else mixed[frame*8+output_channel[channel]]+=value;
                for(unsigned c=0;c<8;++c)if(!std::isfinite(mixed[frame*8+c]))throw std::runtime_error("non-finite cellAudio mixed sample");
              }
            }
        }
        const auto late=now-a.next_tick;a.interval_late_ticks=std::max(a.interval_late_ticks,late);
        a.submit(mixed,late>AUDIO_LATE_LIMIT_TICKS);
        for(auto& p:a.ports)if(p.status==2){
            const auto block=p.address+p.position*p.channels*256*4;
            if(m.concurrent()){std::vector<std::uint8_t> zero(p.channels*256*4);m.transfer(block,zero.data(),zero.size(),true);}
            else std::fill_n(m.access(block,p.channels*256*4,true),p.channels*256*4,0);
            p.timestamp_ticks=a.next_tick;p.has_timestamp=true;
            p.history.push_back({p.tag,a.next_tick});
            if(p.history.size()>15)p.history.pop_front();
            ++p.tag;
            p.position=(p.position+1)%p.blocks;m.store(p.index,8,p.position);
        }
        ++a.counter;
        for(const auto& n:a.notifications){
            unsigned period=1;if(n.flags&0x08000000)period*=2;if(n.flags&0x10000000)period*=4;
            if((a.counter-n.start)%period)continue;
            const auto result=n.queue->send({n.source,0,0,0});
            // LV2 send to a full queue fails EBUSY; never fabricate deferred ticks.
            if(result==0x8001000a){if(!a.notify_dropped)std::fprintf(stderr,"native audio notify queue full; event dropped (EBUSY)\n");++a.notify_dropped;}
            else if(result!=0 && result!=0x80010016)throw std::runtime_error("native audio event send failed");
        }
        a.next_tick+=AUDIO_PERIOD_TICKS;
    }
}
[[maybe_unused]] static void audio_run(PpuState& s){
    if(s.audio.maximum)audio_advance(s,timebase_ticks());
}
[[maybe_unused]] static std::uint32_t audio_service(PpuState& s,unsigned operation){
    auto& a=s.audio;auto& r=s.r;auto& m=s.memory;
    if(operation>6)throw std::runtime_error("unsupported native audio operation");
    if(operation==0){
        if(a.maximum)return AUDIO_ALREADY_INIT;
        a.open_sink();
        try{a.indices=audio_allocate(s,0x10000);}catch(...){a.shutdown_sink();throw;}
        if(!a.indices){a.shutdown_sink();return MM_ENOMEM;}
        a.maximum=s.sdk_version<=0x35ffff?4:8;a.running=0;a.submitted_frames=0;a.late_dropped_frames=0;
        a.counter=0;a.notify_dropped=0;a.nonzero_frames=0;a.next_tick=timebase_ticks()+AUDIO_PERIOD_TICKS;
        for(unsigned i=0;i<8;++i)a.ports[i].index=a.indices+(i+1)*16;
        return 0;
    }
    if(!a.maximum)return AUDIO_NOT_INIT;
    audio_run(s);
    if(operation==1){
        for(auto& p:a.ports)if(p.status!=0x1010)audio_close(s,p);
        audio_free(s,a.indices);a.indices=0;a.maximum=0;a.notifications.clear();a.shutdown_sink();return 0;
    }
    if(operation==2){
        const auto param=std::uint32_t(r[3]),out=std::uint32_t(r[4]);
        if(!param || !out)return AUDIO_PARAM;
        m.access(param,32,false);m.access(out,4,true);
        const auto channels=m.load(param,8),blocks=m.load(param+8,8),attr=m.load(param+16,8);
        if((channels!=2 && channels!=8) || (blocks!=8 && blocks!=16 && blocks!=32))return AUDIO_PARAM;
        if(attr&~UINT64_C(0x1000))throw std::runtime_error("unsupported cellAudio port routing/attributes");
        float level=1.0f;
        if(attr&0x1000){const auto bits=static_cast<std::uint32_t>(m.load(param+24,4));std::memcpy(&level,&bits,4);}
        if(!std::isfinite(level))throw std::runtime_error("non-finite cellAudio initial level");
        for(unsigned i=0;i<a.maximum;++i)if(a.ports[i].status==0x1010){
            const auto size=static_cast<std::uint32_t>(channels*blocks*256*4);
            const auto address=audio_allocate(s,size);if(!address)return AUDIO_SHAREDMEMORY;
            auto& p=a.ports[i];p.address=address;p.size=size;p.channels=channels;p.blocks=blocks;
            p.position=0;p.level=p.target_level=level;p.level_remaining=0;p.tag=0;p.history.clear();p.timestamp_ticks=0;p.has_timestamp=false;p.status=1;m.store(p.index,8,0);m.store(out,4,i);return 0;
        }
        return AUDIO_PORT_FULL;
    }
    const auto number=std::uint32_t(r[3]);if(number>=8)return AUDIO_PARAM;
    auto& p=a.ports[number];
    if(operation==3){
        const auto out=std::uint32_t(r[4]);if(!out)return AUDIO_PARAM;
        m.access(out,32,true);m.store(out,4,p.index);m.store(out+4,4,p.status);
        m.store(out+8,8,p.channels);m.store(out+16,8,p.blocks);
        m.store(out+24,4,p.size);m.store(out+28,4,p.address);return 0;
    }
    if(operation==4){
        if(p.status==0x1010)return AUDIO_NOT_OPEN;
        if(p.status==2)return AUDIO_ALREADY_RUN;
        std::fill_n(m.access(p.address,p.size,true),p.size,0);
        if(!a.running && a.notifications.empty())a.next_tick=timebase_ticks()+AUDIO_PERIOD_TICKS;
        p.status=2;++a.running;return 0;
    }
    if(operation==5){
        if(p.status!=2)return AUDIO_NOT_RUN;
        p.status=1;--a.running;return 0;
    }
    if(p.status==0x1010)return AUDIO_NOT_OPEN;
    audio_close(s,p);return 0;
}
'''

from .native_audio_api import CPP as API_CPP
CPP += API_CPP
