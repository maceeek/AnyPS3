"""Native capture and optional host scanout of actual guest display pixels."""

from .host_present import CPP as PRESENT_CPP

CPP = r'''
[[maybe_unused]] static void rsx_display_pixels(PpuState& s,unsigned id,std::vector<std::uint8_t>& rgba){
    auto& gpu=s.rsx;
#ifdef ANYPS3_RSX_VK
    if(rsx_graphics_scanout(s,id,&rgba,false))return;
#endif
    if(id>=gpu.display_buffers.size())throw std::runtime_error("invalid RSX display buffer");
    const auto& buffer=gpu.display_buffers[id];
    if(!buffer.width || !buffer.height || buffer.width>4096 || buffer.height>4096 ||
       buffer.pitch<buffer.width*4 || buffer.pitch%4 || buffer.offset%4)
        throw std::runtime_error("invalid RSX scanout dimensions or pitch");
    const auto span=std::uint64_t(buffer.height-1)*buffer.pitch+buffer.width*4;
    if(buffer.offset>gpu.local_size || span>gpu.local_size-buffer.offset)
        throw std::runtime_error("RSX scanout outside VRAM");
    const NativeRsx::Tile* tiled=nullptr;
    for(const auto& tile:gpu.tiles)if(tile.bound && !tile.location && buffer.offset<std::uint64_t(tile.offset)+tile.size && tile.offset<std::uint64_t(buffer.offset)+span){
        if(tiled || buffer.offset<tile.offset || std::uint64_t(buffer.offset)+span>std::uint64_t(tile.offset)+tile.size ||
           tile.pitch!=buffer.pitch || (tile.compression!=0 && tile.compression!=7))
            throw std::runtime_error("unsupported RSX scanout tile layout");
        tiled=&tile;
    }
    const auto base=tiled?tiled->offset:buffer.offset;
    const auto memory=s.memory.range(UINT64_C(0xc0000000)+base,tiled?tiled->size:span,false);
    rgba.resize(std::size_t(buffer.width)*buffer.height*4);
    for(unsigned y=0;y<buffer.height;++y)for(unsigned x=0;x<buffer.width;++x){
        const auto linear=buffer.offset+y*buffer.pitch+x*4;
        const auto at=tiled?rsx_tiled_offset(*tiled,linear-tiled->offset):linear;
        std::uint8_t p[4];memory.read(at-base,p,4);auto* out=rgba.data()+(std::size_t(y)*buffer.width+x)*4;
        out[0]=p[1];out[1]=p[2];out[2]=p[3];out[3]=255; // X8R8G8B8 scanout is opaque.
    }
}
[[maybe_unused]] static std::vector<std::uint8_t> rsx_display_pixels(PpuState& s,unsigned id){
    std::vector<std::uint8_t> rgba;rsx_display_pixels(s,id,rgba);return rgba;
}
''' + PRESENT_CPP + r'''
[[maybe_unused]] static unsigned long long rsx_frame_stride(){
    static const unsigned long long stride=[]{
        const char* v=std::getenv("ANYPS3_FRAME_STRIDE");
        if(!v || !*v)return 60ull;
        char* end=nullptr;const auto n=std::strtoull(v,&end,10);
        if(*end || !n)throw std::runtime_error("invalid ANYPS3_FRAME_STRIDE");
        return n;
    }();
    return stride;
}
// DIAGNOSTIC: ANYPS3_FRAME_TRIGGER=<file> saves the next flip outside the stride
// while the file exists, then removes it (operator evidence at low frame rates).
[[maybe_unused]] static bool rsx_frame_trigger(bool consume){
    static const char* path=std::getenv("ANYPS3_FRAME_TRIGGER");
    if(!path || !*path)return false;
    // Diagnostic only: filesystem errors mean "no request", never a guest stop.
    std::error_code error;
    if(!std::filesystem::exists(path,error) || error)return false;
    if(consume)std::filesystem::remove(path,error);
    return true;
}
[[maybe_unused]] static std::string rsx_capture_name(std::uint64_t number){
    char name[32];std::snprintf(name,sizeof(name),"frame-%06llu.ppm",static_cast<unsigned long long>(number));return name;
}
// One binary PPM (P6) of an RGBA scanout: the first three bytes of every pixel, created exclusively ("wbx").
[[maybe_unused]] static void rsx_capture_write(const std::filesystem::path& path,const std::vector<std::uint8_t>& rgba,unsigned id,unsigned width,unsigned height){
    if(rgba.size()!=std::size_t(width)*height*4)throw std::runtime_error("RSX frame capture size mismatch");
    std::vector<std::uint8_t> rgb(rgba.size()/4*3);
    for(std::size_t i=0,o=0;i<rgba.size();i+=4,o+=3){rgb[o]=rgba[i];rgb[o+1]=rgba[i+1];rgb[o+2]=rgba[i+2];}
    auto close=[](FILE* f){std::fclose(f);};
    std::unique_ptr<FILE,decltype(close)> file(std::fopen(path.c_str(),"wbx"),close);
    if(!file)throw std::runtime_error("cannot create new RSX frame capture");
    std::fprintf(file.get(),"P6\n# AnyPS3 native display buffer %u\n%u %u\n255\n",id,width,height);
    if(!rgb.empty() && std::fwrite(rgb.data(),1,rgb.size(),file.get())!=rgb.size())throw std::runtime_error("RSX frame write failed");
    if(std::fflush(file.get()))throw std::runtime_error("RSX frame flush failed");
}
// Stride captures of the asynchronous flip path (rsx_graphics_flip_async): the pixels were copied on the GPU timeline of the flip;
// the file is written by one background task so the RSX owner never waits for the disk. The bytes go to a hidden ".frame-N.ppm.part"
// that is hard-linked to its final name only when complete (a killed process never leaves a truncated frame-N.ppm). A failure of
// the previous write is rethrown at the next capture or, for the last one, when the graphics owner is released
// (rsx_worker_release -> rsx_capture_writer_join); both are explicit stops.
[[maybe_unused]] static std::future<void>& rsx_capture_writer(){static std::future<void> pending;return pending;}
[[maybe_unused]] static void rsx_capture_write_async(std::filesystem::path path,std::vector<std::uint8_t> rgba,unsigned id,unsigned width,unsigned height,std::uint64_t number){
    auto& pending=rsx_capture_writer();
    if(pending.valid())pending.get();
    pending=std::async(std::launch::async,[path=std::move(path),rgba=std::move(rgba),id,width,height,number]{
        try{
            auto part=path;part.replace_filename("."+path.filename().string()+".part");
            rsx_capture_write(part,rgba,id,width,height);
            if(::link(part.c_str(),path.c_str())!=0)throw std::runtime_error("cannot create new RSX frame capture");
            ::unlink(part.c_str());
            std::fprintf(stderr,"RSX captured frame=%llu buffer=%u size=%ux%u file=%s\n",static_cast<unsigned long long>(number),id,width,height,path.c_str());
        }catch(const std::exception& e){std::fprintf(stderr,"RSX frame capture %s failed: %s\n",path.c_str(),e.what());throw;}
    });
}
// Collects the last background capture write: its failure is an explicit stop (called after the GPU completion hooks drained).
[[maybe_unused]] static void rsx_capture_writer_join(){auto& pending=rsx_capture_writer();if(pending.valid())pending.get();}
static void rsx_capture_display(PpuState& s,unsigned id){
#ifndef ANYPS3_VULKAN
    (void)s;(void)id;
    throw std::runtime_error("RSX display capture requires a Vulkan build");
#else
    auto& gpu=s.rsx;
#ifdef ANYPS3_RSX_VK
    // Flip completion is ordered after GPU execution even when capture stride
    // suppresses this frame. No guest surface download is needed for a fence.
    rsx_graphics_flush(s,"display_capture");
#endif
    if(s.present.enabled){
#ifdef ANYPS3_RSX_VK
        if(!rsx_graphics_scanout(s,id,nullptr,true))
#endif
        rsx_present_display(s,id);
    }
    const char* directory=std::getenv("ANYPS3_FRAME_DIR");
    if((!directory || !*directory) && s.present.enabled){++gpu.flips;return;}
    if(!directory || !*directory)throw std::runtime_error("headless RSX flip requires ANYPS3_FRAME_DIR");
    const auto root=std::filesystem::canonical(directory);
    if(!std::filesystem::is_directory(root))throw std::runtime_error("RSX frame output is not a directory");
    // Every flip is scanned out; files are written for the first eight flips
    // and then every ANYPS3_FRAME_STRIDE-th flip (default 60) to bound disk use.
    ++gpu.flips;
    const auto stride=rsx_frame_stride();
    // Consume a pending request even when the stride saves this flip anyway (one request = one saved flip).
    const bool requested=rsx_frame_trigger(true);
    if(gpu.flips>8 && gpu.flips%stride && !requested)return;
    if(gpu.captured_frames>=256)throw std::runtime_error("headless RSX capture budget exhausted (256 frames)");
    if(s.video.format!=0)throw std::runtime_error("unsupported RSX scanout format");
    auto rgba=rsx_display_pixels(s,id);const auto& buffer=gpu.display_buffers[id];
    if(!gpu.renderer)gpu.renderer=std::make_unique<NativeVulkan>(s.rsx.host);
#ifndef ANYPS3_RSX_VK
    // Preserve the standalone clear-backend diagnostic's explicit Vulkan
    // readback contract. The graphics backend already captured its GPU image.
    gpu.renderer->readback(rgba,buffer.width,buffer.height);
#endif
    const auto path=root/rsx_capture_name(gpu.captured_frames+1);
    rsx_capture_write(path,rgba,id,buffer.width,buffer.height);
    ++gpu.captured_frames;
    std::fprintf(stderr,"RSX captured frame=%llu buffer=%u size=%ux%u file=%s\n",
        static_cast<unsigned long long>(gpu.captured_frames),id,buffer.width,buffer.height,path.c_str());
#endif
}
'''
