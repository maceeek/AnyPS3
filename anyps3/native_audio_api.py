"""Audio API extensions and PARA audio payloads.

Firmware 3.01 layouts: local/ratchet/codex/audiofull/evidence/audio-api.md.
Ex notify extensions are derived from RPCS3 ffba2bf, not firmware 3.01.
"""

# NIDs use the PS3 export name hash; checked against original export tables.
import hashlib


def _nid(name):
    return '0x' + hashlib.sha1(name.encode() + bytes.fromhex('6759659904250490566427499489741a')).digest()[:4][::-1].hex()


_NAMES = ['cellAudioGetPortTimestamp', 'cellAudioGetPortBlockTag', 'cellAudioSetPortLevel',
          'cellAudioCreateNotifyEventQueue', 'cellAudioSetNotifyEventQueue', 'cellAudioRemoveNotifyEventQueue',
          'cellAudioAddData', 'cellAudioAdd2chData', 'cellAudioAdd6chData',
          'cellAudioCreateNotifyEventQueueEx', 'cellAudioSetNotifyEventQueueEx', 'cellAudioRemoveNotifyEventQueueEx']
IMPORTS = {('cellAudio', _nid(name)): (name, f'r[3]=sign_word(audio_extended_service(state,{op}));')
           for op, name in enumerate(_NAMES)}

CPP = r'''
static constexpr std::uint32_t AUDIO_OUT_PARAM=0x8002b242,AUDIO_OUT_RANGE=0x8002b243,
    AUDIO_OUT_NO_DEVICE=0x8002b244,AUDIO_OUT_UNSUPPORTED=0x8002b245,AUDIO_OUT_MODE=0x8002b246;
static bool audio_output_available(NativeAudio& a){
    try{a.open_sink();return true;}
    catch(const std::runtime_error& e){
        const std::string message=e.what();
        if(message.find("SDL2 audio")==std::string::npos && message.find("native audio requires SDL2")==std::string::npos)throw;
        std::fprintf(stderr,"native audio output unavailable: %s\n",e.what());return false;
    }
}
std::uint32_t NativeAudio::parameter(unsigned id,std::vector<std::uint8_t>& p){
    auto& a=*this;
    unsigned expected;
    switch(id){
    case 0x4c05:case 0x4c0a:expected=8;break;
    case 0x4c06:expected=144;break;
    case 0x4c07:case 0x4c09:case 0x4c0b:expected=28;break;
    default:throw std::runtime_error("unknown audio PARA id");
    }
    if(p.size()!=expected)throw std::runtime_error("invalid firmware audio PARA payload size");
    auto get=[&](unsigned n){return(std::uint32_t(p.at(n))<<24)|(std::uint32_t(p.at(n+1))<<16)|(std::uint32_t(p.at(n+2))<<8)|p.at(n+3);};
    auto put=[&](unsigned n,std::uint32_t v){for(unsigned j=0;j<4;++j)p.at(n+j)=v>>(24-8*j);};
    const auto output=get(0);
    if(output>1)return AUDIO_OUT_PARAM;
    if(id==0x4c05){
        const unsigned count=output?0:audio_output_available(a)?1:0;put(4,count);
        std::fprintf(stderr,"native audio output=%u configured_devices=%u sink=%s\n",output,count,a.null_sink?"null (DIAGNOSTIC, PCM DISCARDED)":"SDL2");
        return 0;
    }
    if(id==0x4c09 || id==0x4c0b || id==0x4c0a){
        if(output)return AUDIO_OUT_UNSUPPORTED;
        if(id==0x4c0a){if(get(4)>2)return AUDIO_OUT_PARAM;a.copy_control=get(4);return 0;}
        if(id==0x4c09){
            const auto downmix=get(16);
            if(get(20) || get(24)>1)return AUDIO_OUT_PARAM;
            if((p[4]!=2 && p[4]!=8) || p[5])return AUDIO_OUT_MODE;
            // Only NONE is implemented; never acknowledge an ignored matrix.
            if(downmix) return 0x8002b241;
            if(!audio_output_available(a))return AUDIO_OUT_NO_DEVICE;
            a.output_channels=p[4];a.downmixer=downmix;
            std::fprintf(stderr,"native audio configured LPCM channels=%u rate=48000 host_channels=%u sink=%s\n",a.output_channels,a.host_channels,a.null_sink?"null (DIAGNOSTIC)":"SDL2");
            return 0;
        }
        std::fill(p.begin()+4,p.end(),0);p[4]=a.output_channels;put(16,a.downmixer);return 0;
    }
    if(id!=0x4c06 && id!=0x4c07)throw std::runtime_error("unknown audio PARA id");
    const auto device=get(4);
    if(output || device || !audio_output_available(a))return AUDIO_OUT_RANGE;
    std::fill(p.begin()+8,p.end(),0);
    if(id==0x4c06){
        // CellAudioOutDeviceInfo: HDMI, stereo/7.1 LPCM at 48 kHz.
        p[8]=0;p[9]=2;p[10]=1; // portType, modeCount, available
        // Latency is a nominal host queue block (6 ms), not measured hardware latency.
        p[15]=6;
        p[16]=0;p[17]=2;p[18]=4;put(20,1);
        p[24]=0;p[25]=8;p[26]=4;put(28,0x40000000);
    }else{
        p[8]=0;p[9]=0;put(16,a.downmixer);
        p[20]=0;p[21]=a.output_channels;p[22]=4;put(24,a.output_channels==2?1:0x40000000);
    }
    return 0;
}
[[maybe_unused]] static std::uint32_t audio_out_parameter(PpuState& s,unsigned id,std::vector<std::uint8_t>& p){
    return s.audio.parameter(id,p);
}
[[maybe_unused]] static std::uint32_t audio_out_service(PpuState& s,unsigned op){
    auto& r=s.r;auto& m=s.memory;
    if(op>7)throw std::runtime_error("unknown audio out operation");
    if(op>=6){
        if(r[3]>1)return AUDIO_OUT_PARAM;
        if(r[3])return 0;
        // Firmware 3.01 ignores option and tests fs as a bitmask.
        if(r[4]!=0 || !(r[5]&4) || !audio_output_available(s.audio))return 0;
        if(op==6)return 8;
        return r[6]==2 || r[6]==8?std::uint32_t(r[6]):0;
    }
    const unsigned ids[]={0x4c05,0x4c06,0x4c07,0x4c09,0x4c0b,0x4c0a};
    const unsigned lengths[]={8,144,28,28,28,8};std::vector<std::uint8_t> p(lengths[op]);
    auto put=[&](unsigned n,std::uint32_t v){for(unsigned j=0;j<4;++j)p.at(n+j)=v>>(24-j*8);};
    put(0,r[3]);
    if(op==1 || op==2)put(4,r[4]);
    if(op==5)put(4,r[4]);
    if(op==3){
        if(!r[4])return AUDIO_OUT_PARAM;
        m.transfer(r[4],p.data()+4,16,false);
        put(24,r[6]);
    }
    const auto out=op==1 || op==2?r[5]:r[4];
    const unsigned size=op==1?136:op==2?20:16;
    if(op==1 || op==2 || op==4){if(!out)return AUDIO_OUT_PARAM;m.validate(out,size,true);}
    const auto result=audio_out_parameter(s,ids[op],p);if(result)return result;
    if(op==0)return(std::uint32_t(p[4])<<24)|(std::uint32_t(p[5])<<16)|(std::uint32_t(p[6])<<8)|p[7];
    if(op==1 || op==2 || op==4)m.transfer(out,p.data()+(op==4?4:8),size,true);
    return 0;
}
static float audio_argument_float(PpuState& s){double value;const auto bits=s.fpr[1];std::memcpy(&value,&bits,8);return static_cast<float>(value);}
static float audio_load_float(PpuState& s,std::uint32_t ea){const auto bits=std::uint32_t(s.memory.load(ea,4));float v;std::memcpy(&v,&bits,4);return v;}
static void audio_store_float(PpuState& s,std::uint32_t ea,float v){std::uint32_t bits;std::memcpy(&bits,&v,4);s.memory.store(ea,4,bits);}
[[maybe_unused]] static std::uint32_t audio_extended_service(PpuState& s,unsigned op){
    auto& a=s.audio;auto& r=s.r;auto& m=s.memory;
    if(op>11)throw std::runtime_error("unknown extended audio operation");
    if(op==3 || op==9){
        const auto flags=op==9?std::uint32_t(r[5]):0;
        if(flags&~1u)return AUDIO_PARAM;
        if(!r[3] || !r[4])return AUDIO_PARAM;
        m.validate(r[3],4,true);m.validate(r[4],8,true);
        // Original 3.01 creates a depth-2 FIFO with one of 100 reserved keys.
        for(unsigned n=0;n<100;++n){
            const auto key=UINT64_C(0x80004d494f323221)+n;
            if(std::any_of(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return q.key==key;}))continue;
            if(s.events.next_queue>UINT32_MAX-0x100)throw std::runtime_error("audio event queue ID exhausted");
            const auto id=s.events.next_queue;s.events.next_queue+=0x100;
            s.events.queues.push_back({id,2,key,{},flags?2u:1u});m.store(r[3],4,id);m.store(r[4],8,key);return 0;
        }
        return 0x8031070d;
    }
    if(!a.maximum)return AUDIO_NOT_INIT;
    audio_run(s);
    if(op==4 || op==5 || op==10 || op==11){
        const bool extended=op>=10;const auto flags=extended?std::uint32_t(r[4]):0;
        if(flags&0x07ffffff)return AUDIO_PARAM;
        // Headphone/NOMIX/BEFOREMIX require additional protocols, not ignored flags.
        if(flags&0xe0000000)throw std::runtime_error("unsupported cellAudio notify headphone/NOMIX/beforemix protocol");
        const auto key=r[3];
        auto it=std::find_if(a.notifications.begin(),a.notifications.end(),[&](const auto& n){return n.key==key && n.queue->exists;});
        if(op==5 || op==11){if(it==a.notifications.end() || it->flags!=flags)return 0x80310709;a.notifications.erase(it);return 0;}
        auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return q.key==key;});
        if(!key || q==s.events.queues.end() || it!=a.notifications.end())return 0x80310709;
        a.notifications.erase(std::remove_if(a.notifications.begin(),a.notifications.end(),[](const auto& n){return !n.queue->exists;}),a.notifications.end());
        if(a.notifications.size()>=64)return 0x80310709;
        if(s.events.next_port>UINT32_MAX-0x100)throw std::runtime_error("audio notify source ID exhausted");
        const auto source=UINT64_C(0x100000000)|s.events.next_port;s.events.next_port+=0x100;
        if(!a.running && a.notifications.empty())a.next_tick=timebase_ticks()+AUDIO_PERIOD_TICKS;
        a.notifications.push_back({key,source,a.counter,flags,q->data.state});
        std::fprintf(stderr,"native audio notify key=%016llx source=%016llx flags=%08x\n",(unsigned long long)key,(unsigned long long)source,flags);
        return 0;
    }
    const auto number=std::uint32_t(r[3]);if(number>=a.maximum)return AUDIO_PARAM;
    auto& p=a.ports[number];
    if(p.status==0x1010)return AUDIO_NOT_OPEN;
    if(op==0 || op==1){
        const auto out=std::uint32_t(r[5]);if(!out)return AUDIO_PARAM;
        std::uint64_t value;
        if(op==0){
            const auto tag=r[4];
            const auto stamp=std::find_if(p.history.begin(),p.history.end(),[&](const auto& h){return h.tag==tag;});
            if(stamp==p.history.end())return 0x8031070f;
            const auto ticks=stamp->ticks;
            value=(ticks/TIMEBASE_HZ)*1000000+(ticks%TIMEBASE_HZ)*1000000/TIMEBASE_HZ;
        }else{
            if(r[4]>=p.blocks)return AUDIO_PARAM;
            value=p.tag+(r[4]+p.blocks-p.position)%p.blocks;
        }
        m.store(out,8,value);return 0;
    }
    if(op==2){
        const auto level=audio_argument_float(s);
        if(!std::isfinite(level) || level<0)return AUDIO_PARAM;
        p.target_level=level;p.level_step=(level-p.level)/624.0f;p.level_remaining=624;return 0;
    }
    if(op<6 || op>8)throw std::runtime_error("unhandled extended audio operation");
    const auto source=std::uint32_t(r[4]);
    if(!source || (source&3) || (op!=8 && r[5]!=256))return AUDIO_PARAM;
    if(op==8 && p.channels!=8)return AUDIO_PARAM;
    const unsigned channels=op==6?p.channels:op==7?2:6;
    float volume=audio_argument_float(s);volume=std::isfinite(volume)?std::max(-16.0f,std::min(16.0f,volume)):0;
    // Snapshot the source before writing, including source/destination overlap.
    std::vector<float> input(256*channels);
    m.validate(source,input.size()*4,false);
    for(unsigned i=0;i<input.size();++i){input[i]=audio_load_float(s,source+i*4);if(!std::isfinite(input[i]))throw std::runtime_error("non-finite cellAudioAddData source");}
    const auto destination=p.address+p.position*p.channels*256*4;
    constexpr unsigned six_channel[]={0,2,1,4,5,3};
    for(unsigned frame=0;frame<256;++frame)for(unsigned c=0;c<channels;++c){
        const auto at=destination+(frame*p.channels+(op==8?six_channel[c]:c))*4;
        const auto value=audio_load_float(s,at)+input[frame*channels+c]*volume;
        if(!std::isfinite(value))throw std::runtime_error("non-finite cellAudioAddData result");
        audio_store_float(s,at,value);
    }
    return 0;
}
'''
