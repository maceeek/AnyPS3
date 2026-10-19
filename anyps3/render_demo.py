"""Generate a self-contained Vulkan RSX FIFO diagnostic, without game assets.

python -m anyps3.render_demo -o build/rsx-vulkan.cpp
c++ -std=c++17 -O1 -DANYPS3_VULKAN build/rsx-vulkan.cpp -lvulkan -o build/rsx-vulkan
./build/rsx-vulkan build/rsx-vulkan.ppm
"""
import argparse
from pathlib import Path

from .demo import make_demo
from .elf import ElfImage
from .program import compile_program


DEMO_CPP = r'''
#include <cassert>
#ifndef ANYPS3_VULKAN
#error This diagnostic requires -DANYPS3_VULKAN and -lvulkan
#endif
template<class F> static void expect_failure(F action){
    bool failed=false;try{action();}catch(const std::runtime_error&){failed=true;}
    if(!failed)throw std::runtime_error("unsupported RSX operation unexpectedly succeeded");
}
int main(int argc,char** argv){
    if(argc!=2){std::fprintf(stderr,"Usage: rsx-vulkan NEW_OUTPUT.ppm\n");return 2;}
    try{
        PpuState s;auto& gpu=s.rsx;auto& mem=s.memory;
        try{gpu.renderer=std::make_unique<NativeVulkan>();}
        catch(const std::runtime_error& error){std::fprintf(stderr,"Vulkan unavailable: %s\n",error.what());return 77;}
        constexpr unsigned width=320,height=180,pitch=width*4+16,offset=64;
        gpu.local_size=0x100000;gpu.dma=0x10000;gpu.context=123;
        mem.map(gpu.dma,0x1000,6,nullptr,0);
        mem.map(0x70000000,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;
        mem.map(0xc0000000,gpu.local_size,6,nullptr,0,true);
        auto* vram=mem.access(0xc0000000,gpu.local_size,true);
        std::fill_n(vram,gpu.local_size,0xa5);
        unsigned put=0;
        auto method=[&](unsigned reg,unsigned value){
            mem.store(0x70000000+put,4,0x40000|reg);mem.store(0x70000004+put,4,value);put+=8;
            mem.store(gpu.dma+0x40,4,put);
        };
        auto drain=[&]{gpu.fifo_enabled=true;rsx_run(s,1024);assert(mem.load(gpu.dma+0x44,4)==put);};
        method(0x194,0xfeed0000);method(0x200,width<<16);method(0x204,height<<16);
        method(0x208,0x108);method(0x20c,pitch);method(0x210,offset);method(0x220,1);
        method(0x8c0,width<<16);method(0x8c4,height<<16);
        method(0x1d90,0xff182438);method(0x1d94,0xf0);drain();
        // Distinct RGBA components expose byte-swapping errors, including alpha.
        method(0x8c0,(80u<<16)|20);method(0x8c4,(120u<<16)|30);
        method(0x1d90,0x7fe13562);method(0x1d94,0xf0);drain();
        method(0x8c0,(80u<<16)|120);method(0x1d90,0x9347c981);method(0x1d94,0xf0);drain();
        method(0x8c0,(80u<<16)|220);method(0x1d90,0xc53b79ef);method(0x1d94,0xf0);drain();
        // Clip to the surface's right and bottom edges; do not touch padding.
        method(0x8c0,(50u<<16)|310);method(0x8c4,(50u<<16)|170);
        method(0x1d90,0xffe9bc43);method(0x1d94,0xf0);drain();
        assert(gpu.rendered_clears==5 && gpu.renderer->clears==5);
        for(unsigned y=0;y<height;++y){
            for(unsigned x=0;x<width;++x){
                unsigned expected=0xff182438;
                if(y>=30 && y<150){
                    if(x>=20 && x<100)expected=0x7fe13562;
                    if(x>=120 && x<200)expected=0x9347c981;
                    if(x>=220 && x<300)expected=0xc53b79ef;
                }
                if(x>=310 && y>=170)expected=0xffe9bc43;
                if(mem.load(0xc0000000u+offset+y*pitch+x*4,4)!=expected)
                    throw std::runtime_error("Vulkan readback pixel mismatch at "+std::to_string(x)+","+std::to_string(y));
            }
            for(unsigned b=width*4;b<pitch;++b)assert(vram[offset+y*pitch+b]==0xa5);
        }
        for(unsigned b=0;b<offset;++b)assert(vram[b]==0xa5);
        for(unsigned b=offset+height*pitch;b<gpu.local_size;++b)assert(vram[b]==0xa5);
        const std::vector<std::uint8_t> snapshot(vram,vram+gpu.local_size);
        // No-op and rejection paths must neither submit work nor change VRAM.
        rsx_clear_surface(s,0);
        gpu.registers[0x8c0]=(50u<<16)|400;rsx_clear_surface(s,0xf0);
        gpu.registers[0x8c0]=width<<16;
        expect_failure([&]{rsx_clear_surface(s,0x4);});
        gpu.tiles[0].bound=true;gpu.tiles[0].size=0x100000;
        expect_failure([&]{rsx_clear_surface(s,0xf0);});gpu.tiles[0].bound=false;
        gpu.registers[0x210]=0xffff0;
        expect_failure([&]{rsx_clear_surface(s,0xf0);});gpu.registers[0x210]=offset;
        gpu.registers[0x194]=0xfeed0002;
        expect_failure([&]{rsx_clear_surface(s,0xf0);});gpu.registers[0x194]=0xfeed0000;
        gpu.registers[0x208]=0x1108;
        expect_failure([&]{rsx_clear_surface(s,0xf0);});gpu.registers[0x208]=0x108;
        gpu.registers[0x208]=0x106;
        expect_failure([&]{rsx_clear_surface(s,0xf0);});gpu.registers[0x208]=0x108;
        gpu.registers[0x220]=0x13;
        expect_failure([&]{rsx_clear_surface(s,0xf0);});gpu.registers[0x220]=1;
        // Failure through FIFO must leave GET on the offending clear command.
        method(0x1d94,1);const auto get=mem.load(gpu.dma+0x44,4);
        expect_failure(drain);assert(mem.load(gpu.dma+0x44,4)==get);
        assert(gpu.renderer->clears==5 && gpu.rendered_clears==5);
        assert(std::equal(snapshot.begin(),snapshot.end(),vram));
        {
            // The game's first clear combines tiled color and packed Z24S8,
            // with two horizontal samples per pixel. Exercise a scissored
            // version against nonzero storage to detect wrong sample addresses.
            PpuState tiled;auto& g=tiled.rsx;auto& memory=tiled.memory;
            g.local_size=0x40000;memory.map(0xc0000000,g.local_size,6,nullptr,0,true);
            auto* raw=memory.access(0xc0000000,g.local_size,true);std::fill_n(raw,g.local_size,0x96);
            for(unsigned i=0;i<2;++i){auto& t=g.tiles[i];t.bound=true;t.offset=(i+1)*0x10000;
                t.size=0x10000;t.pitch=256;t.bank=i+1;t.compression=i?11:7;}
            g.registers={{0x194,0xfeed0000},{0x198,0xfeed0000},{0x200,8<<16},{0x204,8<<16},
                {0x208,0x3148},{0x20c,256},{0x210,0x10000},{0x214,0x20000},{0x220,1},{0x22c,256},
                {0x32c,255},{0x8c0,(4<<16)|2},{0x8c4,(3<<16)|1},{0x1d8c,0x123456ab},{0x1d90,0x7f349ae1}};
            rsx_clear_surface(tiled,0xf3);
            assert(g.rendered_clears==1 && g.renderer->clears==2);
            std::vector<std::uint8_t> expected(g.local_size,0x96);
            for(unsigned i=0;i<2;++i)for(unsigned y=1;y<4;++y)for(unsigned x=4;x<12;++x){
                const unsigned at=rsx_tiled_offset(g.tiles[i],y*256+x*4);
                const unsigned value=i?0x123456ab:0x7f349ae1;
                for(unsigned b=0;b<4;++b)expected[at+b]=value>>(24-b*8);
            }
            if(!std::equal(expected.begin(),expected.end(),raw))throw std::runtime_error("tiled MSAA color/depth readback mismatch");
            // Both attachments are validated before any guest memory changes.
            g.registers[0x214]=0x3fff0;expect_failure([&]{rsx_clear_surface(tiled,0xf3);});
            assert(g.rendered_clears==1 && std::equal(expected.begin(),expected.end(),raw));
            g.display_buffers[1]={16,8,256,0x10000};
            const auto scanout=rsx_display_pixels(tiled,1);
            for(unsigned y=0;y<8;++y)for(unsigned x=0;x<16;++x){
                const unsigned color=y>=1 && y<4 && x>=4 && x<12?0x349ae1:0x969696;
                const auto at=(y*16+x)*4;
                assert(scanout[at]==(color>>16) && scanout[at+1]==((color>>8)&255) && scanout[at+2]==(color&255) && scanout[at+3]==255);
            }
        }
        if(!std::filesystem::exists(argv[1])){
            const auto frames=std::filesystem::path(std::string(argv[1])+".frames");
            if(!std::filesystem::create_directory(frames))throw std::runtime_error("scanout test directory already exists");
            if(setenv("ANYPS3_FRAME_DIR",frames.c_str(),1))throw std::runtime_error("cannot configure scanout test output");
            mem.map(0x11000,0x3000,6,nullptr,0);gpu.driver=0x11000;gpu.reports=0x13000;
            gpu.event_queue=123;gpu.event_port=456;gpu.display_buffers[1]={width,height,pitch,offset};
            s.events.queues.push_back({123,2,0,{}});s.events.ports.push_back({456,1,123,789});
            mem.store(gpu.driver+0x12c0,4,0x10);mem.store(gpu.reports+0x10,8,UINT64_MAX);
            rsx_queue_display(s,1,1);rsx_flip_display(s,1,0x8000010f);
            assert(gpu.captured_frames==1 && gpu.renderer->readbacks==1 && gpu.renderer->clears==5);
            assert(mem.load(gpu.driver+0x1108,4)==1 && mem.load(gpu.driver+0x1100,4)==0xc0000002);
            assert(mem.load(gpu.reports+0x10,8)==0 && s.events.queues.back().data.size()==1);
            assert(s.events.queues.back().data[0][2]==0x10);
            assert(std::equal(snapshot.begin(),snapshot.end(),vram));
        }
        // Exclusive creation protects previous captures. Pixels come from the
        // Vulkan readback in guest VRAM, not from the expected-value calculation.
        auto close_file=[](FILE* f){std::fclose(f);};
        std::unique_ptr<FILE,decltype(close_file)> file(std::fopen(argv[1],"wbx"),close_file);
        if(!file)throw std::runtime_error("cannot create new PPM output");
        std::fprintf(file.get(),"P6\n# AnyPS3 synthetic RSX Vulkan diagnostic, not a game frame\n%u %u\n255\n",width,height);
        for(unsigned y=0;y<height;++y)for(unsigned x=0;x<width;++x)
            if(std::fwrite(vram+offset+y*pitch+x*4+1,1,3,file.get())!=3)throw std::runtime_error("PPM write failed");
        if(std::fflush(file.get()))throw std::runtime_error("PPM flush failed");
        std::printf("PASS Vulkan RSX: %u pixels, 5 clears, pitch padding, clipping, rejection and FIFO ordering; tiled 2x color/depth clear; synthetic diagnostic\n",width*height);
        return 0;
    }catch(const std::exception& error){std::fprintf(stderr,"FAIL: %s\n",error.what());return 1;}
}
'''


def source() -> str:
    runtime = compile_program(ElfImage.parse(make_demo()), process_entry=True).cpp()
    runtime = runtime.replace('int main(int argc, char** argv)',
                              'int original_main(int argc, char** argv)')
    return runtime + DEMO_CPP


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x') as output:
        output.write(source())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
