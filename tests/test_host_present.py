"""Native scanout tests against real SDL2 dummy video, without game data."""
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.host_present import TYPES as PRESENT_TYPES
from anyps3.native_audio import TYPES as AUDIO_TYPES
from anyps3.native_pad import TYPES as PAD_TYPES, CPP as PAD_CPP
from anyps3.rsx import TYPES as RSX_TYPES
from anyps3.rsx_display import CPP as DISPLAY_CPP
from anyps3.rsx_surface import CPP as SURFACE_CPP
from anyps3.runtime import MEMORY_CPP
from anyps3.vulkan import CPP as VULKAN_CPP


NATIVE = r'''
template<class F> void fault(F f,const char* message){
    bool hit=false;try{f();}catch(const std::runtime_error& e){hit=std::string(e.what()).find(message)!=std::string::npos;}
    assert(hit);
}
static void configure(PpuState& s,unsigned w=3,unsigned h=2,unsigned pitch=20,unsigned offset=16){
    s.rsx.local_size=0xe00000;s.memory.map(0xc0000000,s.rsx.local_size,6,nullptr,0);
    s.rsx.display_buffers[0]={w,h,pitch,offset};
}
static void linear(PpuState& s){
    configure(s);
    // Opaque output; distinct channels and X bytes, nonzero offset and row padding.
    const unsigned words[]={0x00112233,0x7fff0012,0x8044aaff,0xff123456,0x992001fe,0x01008002};
    for(unsigned i=0;i<6;++i)s.memory.store(0xc0000010+(i/3)*20+(i%3)*4,4,words[i]);
}
static const std::vector<std::uint8_t> linear_expected={
    0x11,0x22,0x33,255, 255,0,0x12,255, 0x44,0xaa,255,255,
    0x12,0x34,0x56,255, 0x20,1,0xfe,255, 0,0x80,2,255};
static void check_texture(NativePresent& p,const std::vector<std::uint8_t>& expected){
    assert(p.pixels==expected);
    // Read the real uploaded SDL texture through the renderer before Present:
    // SDL explicitly makes post-Present backbuffer contents undefined.
    auto read=p.symbol<int(*)(void*,const void*,std::uint32_t,void*,int)>("SDL_RenderReadPixels");
    assert(p.clear(p.renderer)==0 && p.copy(p.renderer,p.texture,nullptr,nullptr)==0);
    std::vector<std::uint8_t> actual(expected.size());
    assert(read(p.renderer,nullptr,p.rgba_format(),actual.data(),p.width*4)==0);
    assert(actual==expected);
    // Independent SDL_PIXELFORMAT_RGB24 (SDL_pixels.h): do not use the upload
    // format to interpret the result, which could conceal a channel-order bug.
    std::vector<std::uint8_t> rgb(expected.size()/4*3);
    assert(read(p.renderer,nullptr,0x17101803,rgb.data(),p.width*3)==0);
    for(unsigned i=0;i<expected.size()/4;++i)for(unsigned c=0;c<3;++c)
        assert(rgb[i*3+c]==expected[i*4+c]);
}
int main(int argc,char** argv){
    assert(argc==2);(void)crc32;PpuState s;s.memory.map(0x10000,0x2000,6,nullptr,0);
    auto& p=s.present;const std::string mode=argv[1];
    try{
        if(mode=="disabled"){
            assert(!p.enabled && !p.library && !p.window && p.pixels.empty());
            linear(s);fault([&]{rsx_capture_display(s,0);},"ANYPS3_FRAME_DIR");
            assert(s.rsx.flips==0 && p.presented_frames==0 && !p.library);return 0;
        }
        assert(p.enabled);
        if(mode=="bad_driver"){linear(s);rsx_capture_display(s,0);return 9;}
        if(mode=="linear" || mode=="throttle" || mode=="quit" || mode=="window_close" || mode=="failures"){
            linear(s);rsx_capture_display(s,0);
            assert(p.presented_frames==1 && s.rsx.flips==1 && s.rsx.captured_frames==0 && !s.rsx.renderer);
            assert(rsx_display_pixels(s,0)==linear_expected);check_texture(p,linear_expected);
            if(mode=="linear"){
                auto was=p.symbol<std::uint32_t(*)(std::uint32_t)>("SDL_WasInit");
                const auto* storage=p.pixels.data();p.last_present=NativePresent::Clock::now()+std::chrono::hours(1);
                for(unsigned i=0;i<10;++i)rsx_capture_display(s,0);
                assert(p.pixels.data()==storage && s.rsx.flips==11 && p.presented_frames==1);
                s.rsx.display_buffers[0]={2,1,20,16};rsx_capture_display(s,0);
                assert(p.width==2 && p.height==1 && p.pixels.size()==8);
                p.shutdown();assert(!p.window && !p.renderer && !p.texture && !(was(0x20)&0x20));
            }else if(mode=="throttle"){
                const auto t=p.last_present;
                assert(!p.present_if_due(t+std::chrono::nanoseconds(16666666)) && p.presented_frames==1);
                assert(p.present_if_due(t+std::chrono::nanoseconds(16666667)) && p.presented_frames==2);
                assert(!p.present_if_due(t+std::chrono::nanoseconds(33333333)));
                assert(p.present_if_due(t+std::chrono::nanoseconds(33333334)) && p.presented_frames==3);
                p.last_present=NativePresent::Clock::now()+std::chrono::hours(1);
                s.memory.store(0xc0000010,4,0x9abcdef0);rsx_capture_display(s,0);
                auto expected=linear_expected;expected[0]=0xbc;expected[1]=0xde;expected[2]=0xf0;
                check_texture(p,expected);assert(p.presented_frames==3 && s.rsx.flips==2);
            }else if(mode=="quit" || mode=="window_close"){
                auto push=p.symbol<int(*)(NativePresent::Event*)>("SDL_PushEvent");
                NativePresent::Event e{};
                if(mode=="quit")e.type=0x100;
                else{e.window.type=0x200;e.window.window_id=p.window_id;e.window.event=14;}
                assert(push(&e)==1);rsx_capture_display(s,0);return 9;
            }else{
                fault([&]{rsx_present_display(s,8);},"invalid RSX display buffer");
                s.video.format=1;fault([&]{rsx_capture_display(s,0);},"unsupported RSX scanout format");s.video.format=0;
                s.rsx.display_buffers[0].pitch=8;
                fault([&]{rsx_capture_display(s,0);},"invalid RSX scanout");s.rsx.display_buffers[0].pitch=20;
                s.rsx.display_buffers[0].offset=s.rsx.local_size-4;
                fault([&]{rsx_capture_display(s,0);},"outside VRAM");s.rsx.display_buffers[0].offset=16;
                auto was=p.symbol<std::uint32_t(*)(std::uint32_t)>("SDL_WasInit");
                p.shutdown();p.create_window=[](const char*,int,int,int,int,std::uint32_t)->void*{return nullptr;};
                fault([&]{p.open(3,2);},"window open failed");assert(!p.subsystem && !p.window && !(was(0x20)&0x20));
                p.create_window=p.symbol<decltype(p.create_window)>("SDL_CreateWindow");
                p.create_renderer=[](void*,int,std::uint32_t)->void*{return nullptr;};
                fault([&]{p.open(3,2);},"renderer open failed");assert(!p.subsystem && !p.window && !p.renderer);
                p.create_renderer=p.symbol<decltype(p.create_renderer)>("SDL_CreateRenderer");
                p.create_texture=[](void*,std::uint32_t,int,int,int)->void*{return nullptr;};
                fault([&]{p.open(3,2);},"streaming texture open failed");assert(!p.subsystem && !p.window && !p.renderer && !p.texture);
                assert(p.presented_frames==1);
            }
            return 0;
        }
        if(mode=="tiled"){
            configure(s,65,129,8192,0x880000);
            auto& tile=s.rsx.tiles[0];tile.bound=true;tile.offset=0x880000;tile.size=0x580000;tile.pitch=8192;tile.bank=1;
            // Fixed reference-derived golden addresses from test_rsx.py, NOT the converter.
            // Columns include the 256B boundary; rows cover tile-block/bank boundaries.
            struct Sample{unsigned x,y,physical;};
            const Sample samples[]={{0,0,0x884000},{1,0,0x884004},{8,0,0x884100},{16,0,0x884280},
                {64,0,0x88c200},{0,1,0x884020},{0,4,0x884080},{0,8,0x884c00},
                {0,16,0x885200},{0,64,0x908000},{0,128,0x98c000}};
            std::vector<std::uint8_t> expected(65*129*4,0);
            for(unsigned i=3;i<expected.size();i+=4)expected[i]=255;
            unsigned i=0;
            for(auto sample:samples){
                const auto red=++i,green=0x80+i,blue=0xff-i;
                s.memory.store(0xc0000000+sample.physical,4,0x7f000000|(red<<16)|(green<<8)|blue);
                const auto at=(sample.y*65+sample.x)*4;expected[at]=red;expected[at+1]=green;expected[at+2]=blue;
            }
            rsx_capture_display(s,0);check_texture(p,expected);assert(rsx_display_pixels(s,0)==expected);
            tile.compression=7;rsx_capture_display(s,0);check_texture(p,expected);
            tile.compression=1;fault([&]{rsx_capture_display(s,0);},"unsupported RSX scanout tile layout");
            return 0;
        }
        if(mode=="coexist"){
            linear(s);auto& pad=s.pad;pad.load();assert(pad.init(0x2000)==0);
            auto attach=pad.symbol<int(*)(int,int,int,int)>("SDL_JoystickAttachVirtual");
            auto detach=pad.symbol<int(*)(int)>("SDL_JoystickDetachVirtual");
            auto joyopen=pad.symbol<void*(*)(int)>("SDL_JoystickOpen");
            auto joyclose=pad.symbol<void(*)(void*)>("SDL_JoystickClose");
            auto setbutton=pad.symbol<int(*)(void*,int,std::uint8_t)>("SDL_JoystickSetVirtualButton");
            const auto device=attach(1,6,15,0);assert(device>=0);auto* joy=joyopen(device);assert(joy);
            s.r[3]=1;assert(pad_service(s,0)==0 && pad.poll()==1);
            assert(setbutton(joy,0,1)==0);
            NativeAudio audio;audio.open_sink();std::array<float,256*8> silence{};
            rsx_capture_display(s,0);
            auto was=p.symbol<std::uint32_t(*)(std::uint32_t)>("SDL_WasInit");
            auto push=p.symbol<int(*)(NativePresent::Event*)>("SDL_PushEvent");
            NativePresent::Event e{};e.type=0x600;assert(push(&e)==1);
            p.poll_events();assert(p.peep(&e,1,2,0x600,0x600)==1 && e.type==0x600);
            s.r[3]=0;s.r[4]=0x10000;assert(pad_service(s,4)==0 && s.memory.load(0x1000a,2)==0x40);
            // Two video owners exercise SDL video/event refcounts; quit never calls SDL_Quit.
            NativePresent second;second.open(3,2);p.shutdown();assert(was(0x20)&0x20);
            second.poll_events();second.shutdown();assert(!(was(0x20)&0x20));
            assert((was(0x2000)&0x2000) && (was(0x10)&0x10));
            assert(setbutton(joy,0,0)==0);s.r[3]=0;s.r[4]=0x10000;
            assert(pad_service(s,4)==0 && s.memory.load(0x1000a,2)==0);audio.submit(silence);
            rsx_capture_display(s,0);assert(p.window);
            assert(pad_service(s,1)==0);joyclose(joy);assert(detach(device)==0);pad.quit(0x2000);
            audio.shutdown_sink();p.poll_events();rsx_capture_display(s,0);p.shutdown();
            assert(!(was(0x2000)&0x2000) && !(was(0x10)&0x10) && !(was(0x20)&0x20));
            return 0;
        }
        return 8;
    }catch(const std::exception& e){
        std::fprintf(stderr,"STOP: %s\n",e.what());
        if(mode=="quit" || mode=="window_close")assert(p.closed && !p.window && !p.subsystem && p.presented_frames==1);
        if(mode=="bad_driver")assert(!p.subsystem && !p.window && p.presented_frames==0);
        return 1;
    }
}
'''


@unittest.skipUnless(shutil.which('c++') and ctypes.util.find_library('SDL2'), 'Requires C++ and SDL2')
class HostPresentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='anyps3-present-')
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.binary = root / 'test'
        # Compile only scanout, SDL sinks and memory, not the game or full runtime.
        source = '\n'.join([
            '#include <array>\n#include <chrono>\n#include <cstdint>\n#include <cinttypes>\n#include <cstdio>\n'
            '#include <cstdlib>\n#include <filesystem>\n#include <cassert>',
            MEMORY_CPP, PRESENT_TYPES, PAD_TYPES, AUDIO_TYPES, VULKAN_CPP, RSX_TYPES,
            'struct PpuState { GuestMemory memory; NativeRsx rsx; NativePresent present; NativePad pad;'
            'std::array<std::uint64_t,32> r{};std::uint32_t sdk_version=0;struct {unsigned format=0;} video;};',
            SURFACE_CPP.split('[[maybe_unused]] static void rsx_clear_surface')[0],
            DISPLAY_CPP, PAD_CPP, NATIVE,
        ])
        (root / 'test.cpp').write_text(source)
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                                '-DANYPS3_VULKAN', str(root / 'test.cpp'), '-lvulkan',
                                '-o', str(cls.binary)], capture_output=True, text=True, timeout=60)
        if build.returncode:
            raise AssertionError(build.stderr)

    def run_native(self, mode, *, present='sdl', driver='dummy', returncode=0, message=None):
        env = dict(os.environ, SDL_VIDEODRIVER=driver, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='sdl')
        for name in ('ANYPS3_PRESENT', 'ANYPS3_FRAME_DIR', 'ANYPS3_FRAME_STRIDE', 'SDL_RENDER_DRIVER'):
            env.pop(name, None)
        if present is not None:
            env['ANYPS3_PRESENT'] = present
        run = subprocess.run([str(self.binary), mode], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, returncode, run.stderr)
        if message:
            self.assertIn(message, run.stderr)

    def test_disabled_needs_no_sdl_load_and_preserves_headless_errors(self):
        self.run_native('disabled', present=None, driver='does-not-exist')
        self.run_native('disabled', present='other', driver='does-not-exist')

    def test_linear_scanout_readback_storage_reuse_and_resize(self):
        self.run_native('linear')

    def test_tiled_scanout_independent_golden_pixels_and_readback(self):
        self.run_native('tiled')

    def test_throttle_boundary_counts_and_upload_of_skipped_frame(self):
        self.run_native('throttle')

    def test_quit_is_explicit_stop_and_releases_video(self):
        self.run_native('quit', returncode=1, message='STOP: SDL2 presentation window closed; run terminated')

    def test_window_close_is_explicit_stop_and_releases_video(self):
        self.run_native('window_close', returncode=1, message='STOP: SDL2 presentation window closed; run terminated')

    def test_missing_video_driver_is_explicit_stop(self):
        self.run_native('bad_driver', driver='anyps3-no-such-driver', returncode=1,
                        message='STOP: SDL2 presentation video initialization failed:')

    def test_open_failures_and_invalid_scanout_never_count_as_presented(self):
        self.run_native('failures')

    def test_pad_events_input_audio_and_subsystem_lifetimes_coexist(self):
        self.run_native('coexist')

    def test_generator_embeds_presenter_and_actual_stop_counter(self):
        from anyps3.elf import ElfImage
        from anyps3.program import compile_program
        from tests.test_anyps3 import BLR, program
        cpp = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        self.assertIn('NativePresent present;', cpp)
        self.assertIn('state.rsx.rendered_clears,state.present.presented_frames', cpp)
        self.assertNotIn(' presented_frames=0\\n"', cpp)
