"""Opt-in FIFO/memory capture for offline performance replay, never a correctness oracle."""
TYPES = r'''
struct RsxPerfCapture {
    bool configured=false,active=false,done=false,replay=false;
    std::uint64_t first=1,frames=1,seen=0,commands_count=0;
    std::string root,trigger;
    std::unique_ptr<std::ofstream> commands;
};
'''
CPP = r'''
static std::uint64_t rsx_capture_option(const char* name,std::uint64_t fallback){
    const auto* value=std::getenv(name);if(!value)return fallback;
    char* end=nullptr;const auto n=std::strtoull(value,&end,10);
    if(!*value || *value=='-' || *end || !n)throw std::runtime_error(std::string("invalid ")+name);
    return n;
}
// Snapshot a backing through its exact coherent view. A kernel write(2)
// cannot fault in mprotect(PROT_NONE) pages via the userspace observer. Byte
// loads remain atomic with concurrent producers; the capture is explicitly
// an initial image, not an atomic PPU/SPU machine snapshot. No watches, raw
// protections, reservations or dirty tokens are changed by this read.
static void rsx_capture_write_backing(std::ostream& out,const GuestBacking& backing){
    std::array<std::uint8_t,65536> bytes;
    for(std::uint64_t at=0;at<backing.size();){
        const auto n=unsigned(std::min<std::uint64_t>(bytes.size(),backing.size()-at));
        GuestMemory::atomic_read(backing.coherent_data()+at,bytes.data(),n);
        out.write(reinterpret_cast<const char*>(bytes.data()),n);
        if(!out)throw std::runtime_error("unwritable RSX capture backing");
        at+=n;
    }
}
static void rsx_capture_failed(PpuState& s,const std::exception& error){
    auto& c=s.rsx.perf_capture;c.commands.reset();c.active=false;c.done=true;
    std::fprintf(stderr,"RSX PERF CAPTURE disabled: %s\n",error.what());
    // Capture is diagnostic. I/O/configuration failures cannot stop the game
    // or publish a complete manifest for a partial recording.
    try{if(!c.root.empty()){const auto root=std::filesystem::path(c.root);
        std::ofstream failed(root/"error.txt",std::ios::app);failed<<error.what()<<'\n';}}
    catch(const std::exception&){}
}
// Versioned BE64 metadata, raw guest byte payloads, shared backing identities.
// This explicitly serializes fields; it never dumps host struct padding/pointers.
[[maybe_unused]] static void rsx_capture_state(PpuState& state,std::ostream* out,std::istream* in){
    auto& g=state.rsx;auto& m=state.memory;
    auto number=[&](auto& value){std::uint64_t n=out?std::uint64_t(value):0;
        if(out){char bytes[8];for(unsigned i=0;i<8;++i)bytes[i]=char(n>>(56-8*i));out->write(bytes,8);}
        else{char bytes[8];in->read(bytes,8);if(!*in)throw std::runtime_error("truncated RSX capture number");for(unsigned i=0;i<8;++i)n=(n<<8)|static_cast<unsigned char>(bytes[i]);value=static_cast<std::decay_t<decltype(value)>>(n);}
        if((out && !*out) || (in && !*in))throw std::runtime_error("truncated or unwritable RSX capture");
    };
    std::uint64_t magic=0x414e595253583130,version=1;number(magic);number(version);
    if(magic!=0x414e595253583130 || version!=1)throw std::runtime_error("unsupported RSX capture version");
    std::vector<std::shared_ptr<GuestBacking>> owners;
    if(out)for(const auto& r:m.regions)if(std::find(owners.begin(),owners.end(),r.bytes)==owners.end())owners.push_back(r.bytes);
    std::uint64_t count=owners.size();number(count);if(count>65536)throw std::runtime_error("RSX capture backing count limit");
    std::uint64_t total=0;const auto limit=rsx_capture_option("ANYPS3_RSX_CAPTURE_MAX_BYTES",UINT64_C(2147483648));
    for(std::uint64_t i=0;i<count;++i){std::uint64_t size=out?owners[i]->size():0;number(size);
        if(size>limit-total)throw std::runtime_error("RSX capture memory byte limit");
        total+=size;
        if(in)owners.push_back(std::make_shared<GuestBacking>(size));
        if(out)rsx_capture_write_backing(*out,*owners[i]);else in->read(reinterpret_cast<char*>(owners[i]->data()),size);
        if((out && !*out) || (in && !*in))throw std::runtime_error("truncated or unwritable RSX capture backing");}
    count=m.regions.size();number(count);if(count>65536)throw std::runtime_error("RSX capture region count limit");
    if(in)m.regions.clear();
    for(std::uint64_t i=0;i<count;++i){std::uint64_t base=out?m.regions[i].base:0,flags=out?m.regions[i].flags:0,id=0;
        if(out)id=std::find(owners.begin(),owners.end(),m.regions[i].bytes)-owners.begin();
        number(base);number(flags);number(id);if(id>=owners.size() || base>UINT32_MAX || owners[id]->size()>UINT64_C(0x100000000)-base)throw std::runtime_error("invalid RSX capture mapping");
        if(in)m.regions.push_back({base,unsigned(flags),owners[id],"RSX perf capture"});}
    number(g.device);number(g.local_size);number(g.memory_handle);number(g.context);number(g.dma);number(g.driver);number(g.reports);
    for(auto& v:g.io){number(v);}
    for(auto& v:g.io_flags){number(v);}
    for(auto& t:g.tiles){number(t.bound);number(t.location);number(t.offset);number(t.size);number(t.pitch);number(t.compression);number(t.base);number(t.bank);number(t.range_word);number(t.format_word);}
    for(auto& d:g.display_buffers){number(d.width);number(d.height);number(d.pitch);number(d.offset);}
    for(auto& z:g.zculls){number(z.bound);number(z.width);number(z.height);number(z.start);number(z.offset);number(z.region_word);number(z.offset_word);number(z.status_word);}
    count=g.registers.size();number(count);if(count>65536)throw std::runtime_error("RSX capture register count limit");
    if(out)for(const auto& r:g.registers){auto key=r.first,value=r.second;number(key);number(value);}
    else{g.registers.clear();for(std::uint64_t i=0;i<count;++i){unsigned key=0,value=0;number(key);number(value);g.registers[key]=value;}}
    for(unsigned i=0;i<g.transform_program.size();++i){auto v=std::as_const(g).transform_program[i];number(v);if(in)g.transform_program.write(i,v);}
    for(unsigned i=0;i<g.transform_program_written.size();++i){auto v=std::as_const(g).transform_program_written[i];number(v);if(in)g.transform_program_written.write(i,v);}
    for(unsigned i=0;i<g.transform_constants.size();++i){auto v=std::as_const(g).transform_constants[i];number(v);if(in)g.transform_constants.write(i,v);}
    for(unsigned i=0;i<g.transform_constants_written.size();++i){auto v=std::as_const(g).transform_constants_written[i];number(v);if(in)g.transform_constants_written.write(i,v);}
    for(auto& v:g.report_values){number(v);}
    number(g.report_timestamp);number(g.zcull_generation);number(g.vertex_cache_generation);number(g.transform_program_generation);
    number(g.display_queue_generation);number(g.display_queue_acknowledged);number(state.video.format);
    number(g.draw.indexed);
    for(auto& a:g.draw.immediate){number(a.count);number(a.type);for(auto& word:a.words){number(word);}}
    // Captures start at a completed flip/draw boundary, with no partial draw.
    if(out && (g.draw.primitive || !g.draw.ranges.empty()))throw std::runtime_error("RSX capture requires a completed draw boundary");
}
static void rsx_capture_method_checked(PpuState& s,unsigned reg,unsigned value){
    auto& c=s.rsx.perf_capture;if(!c.active)return;
    if(c.commands_count>=1000000)throw std::runtime_error("RSX capture command limit");
    char bytes[8];for(unsigned i=0;i<4;++i){bytes[i]=char(reg>>(24-8*i));bytes[4+i]=char(value>>(24-8*i));}
    c.commands->write(bytes,8);if(!*c.commands)throw std::runtime_error("RSX capture command write failed");++c.commands_count;
}
static void rsx_capture_flip_checked(PpuState& s,bool& synchronizing){
    synchronizing=false;
    auto& c=s.rsx.perf_capture;if(c.replay)return;
    if(!c.configured){c.configured=true;const auto* root=std::getenv("ANYPS3_RSX_CAPTURE");if(!root || !*root){c.done=true;return;}c.root=root;
        c.first=rsx_capture_option("ANYPS3_RSX_CAPTURE_FLIP",1);c.frames=rsx_capture_option("ANYPS3_RSX_CAPTURE_FRAMES",1);if(c.frames>8)throw std::runtime_error("RSX capture frame limit is 8");
        // ANYPS3_RSX_CAPTURE_TRIGGER=<file>: start after the first completed flip at which the file exists
        // (operator-chosen scene, e.g. a menu reached interactively); replaces ANYPS3_RSX_CAPTURE_FLIP.
        if(const auto* trigger=std::getenv("ANYPS3_RSX_CAPTURE_TRIGGER"))c.trigger=trigger;}
    if(c.done)return;
    ++c.seen;
    if(c.active && c.seen-c.first==c.frames){c.commands->flush();if(!*c.commands)throw std::runtime_error("RSX capture command flush failed");c.commands.reset();c.active=false;c.done=true;
        std::ofstream manifest(std::filesystem::path(c.root)/"complete.json");manifest<<"{\"version\":1,\"frames\":"<<c.frames<<",\"commands\":"<<c.commands_count<<",\"correctness_evidence\":false,\"dynamic_memory\":\"initial snapshot only\"}\n";
        manifest.flush();if(!manifest)throw std::runtime_error("RSX capture manifest write failed");
        std::fprintf(stderr,"RSX PERF CAPTURE complete frames=%llu commands=%llu root=%s\n",(unsigned long long)c.frames,(unsigned long long)c.commands_count,c.root.c_str());return;}
    if(!c.active && !c.trigger.empty()){if(!std::filesystem::exists(c.trigger))return;c.first=c.seen;}
    if(c.active || c.seen!=c.first)return;
    std::filesystem::create_directories(c.root);const auto root=std::filesystem::path(c.root);
    if(std::filesystem::exists(root/"state.bin") || std::filesystem::exists(root/"commands.bin"))throw std::runtime_error("RSX capture refuses to overwrite artifacts");
#ifdef ANYPS3_RSX_VK
    synchronizing=true;rsx_capture_sync(s);synchronizing=false;
#endif
    GuestMemory::ObserverPause pause(s.memory);std::ofstream snapshot(root/"state.bin",std::ios::binary);rsx_capture_state(s,&snapshot,nullptr);snapshot.flush();if(!snapshot)throw std::runtime_error("RSX capture snapshot flush failed");
    c.commands=std::make_unique<std::ofstream>(root/"commands.bin",std::ios::binary);if(!*c.commands)throw std::runtime_error("RSX capture commands open failed");c.active=true;
    std::fprintf(stderr,"RSX PERF CAPTURE started flip=%llu root=%s\n",(unsigned long long)c.first,c.root.c_str());
}
static void rsx_capture_method(PpuState& s,unsigned reg,unsigned value){
    try{rsx_capture_method_checked(s,reg,value);}catch(const std::exception& e){rsx_capture_failed(s,e);}
}
static void rsx_capture_flip(PpuState& s){
    bool synchronizing=false;
    try{rsx_capture_flip_checked(s,synchronizing);}catch(const std::exception& e){
        // A diagnostic I/O failure is contained; a failure completing actual
        // GPU work remains a runtime stop (including unsupported shader status).
        if(synchronizing)throw;
        rsx_capture_failed(s,e);
    }
}
'''

# Appended only by the offline tool/test, after the normal runtime definition.
REPLAY_CPP = r'''
struct RsxReplayTiming {std::uint64_t draws,submits,host_ns,flush_ns;};
static RsxReplayTiming rsx_replay_capture(PpuState& s,std::istream& snapshot,std::istream& commands){
    rsx_capture_state(s,nullptr,&snapshot);auto& g=s.rsx;auto& m=s.memory;
    std::vector<std::uint8_t> fifo;char bytes[8];
    while(commands.read(bytes,8)){
        if(fifo.size()>=8000000)throw std::runtime_error("RSX replay command limit");
        unsigned reg=0,value=0;for(unsigned i=0;i<4;++i){reg=(reg<<8)|static_cast<unsigned char>(bytes[i]);value=(value<<8)|static_cast<unsigned char>(bytes[4+i]);}
        if(reg>0xfffc || reg%4)throw std::runtime_error("invalid RSX replay method");
        const unsigned header=(1u<<18)|reg;for(unsigned v:{header,value})for(unsigned i=0;i<4;++i)fifo.push_back(v>>(24-i*8));
    }
    if(commands.gcount() || !commands.eof())throw std::runtime_error("truncated RSX capture command");
    if(fifo.empty())throw std::runtime_error("empty RSX capture command stream");
    const unsigned size=(fifo.size()+0xfffff)&~0xfffffu,pages=size>>20;
    unsigned io=UINT_MAX;
    for(unsigned first=0;first+pages<=256;++first){bool free=true;for(unsigned p=0;p<pages;++p)if(g.io[first+p]!=UINT32_MAX){free=false;break;}if(free){io=first<<20;break;}}
    if(io==UINT_MAX)throw std::runtime_error("RSX replay has no unused IO aperture for FIFO");
    std::uint64_t base=0;
    for(std::uint64_t at=0x1000000;at+size<=UINT32_MAX;at+=0x100000){bool free=true;for(const auto& r:m.regions)if(at<r.base+r.bytes->size() && r.base<at+size){free=false;break;}if(free){base=at;break;}}
    if(!base)throw std::runtime_error("RSX replay has no guest address for FIFO");
    m.map(base,size,6,fifo.data(),fifo.size());for(unsigned p=0;p<pages;++p)g.io[(io>>20)+p]=base+(p<<20);
    // Replay measures the renderer, not CPU events, vblank pacing or PPU
    // semaphore producers. Acquires use the explicitly labelled perf shim.
    g.perf_capture.replay=true;g.fifo_enabled=true;g.packet_index=0;g.return_offset=UINT32_MAX;g.flip_modes={1,1};g.vsync=false;
    m.store(std::uint64_t(g.driver)+0x12c0,4,0);m.store(std::uint64_t(g.dma)+0x44,4,io);m.store(std::uint64_t(g.dma)+0x40,4,io+fifo.size());
    const auto begin=std::chrono::steady_clock::now();
#ifdef ANYPS3_RSX_VK
    const auto flush_start=rsxgraphics::fallback_stats().gpu_flush_ns;
#endif
    if(!rsx_run(s,fifo.size()/8+1) || m.load(std::uint64_t(g.dma)+0x44,4)!=io+fifo.size())throw std::runtime_error("RSX replay did not consume all commands");
#ifdef ANYPS3_RSX_VK
    rsx_graphics_flush(s,"replay_end");
    const auto* session=static_cast<rsxgraphics::Session*>(g.graphics_owner.get());
    const auto submits=session?session->batch->submissions:0,flush_ns=rsxgraphics::fallback_stats().gpu_flush_ns-flush_start;
#else
    const std::uint64_t submits=0,flush_ns=0;
#endif
    const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count();
    return {g.draw.draws,submits,std::uint64_t(elapsed),flush_ns};
}
'''
