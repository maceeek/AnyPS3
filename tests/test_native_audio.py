"""Native cellAudio ABI, ring ownership, PCM sink and guest polling regressions."""
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from anyps3.services import IMPORTS
from tests.test_anyps3 import BLR, program
from anyps3.demo import CODE_ADDRESS


FAILING_SDL = r'''
static unsigned fixture_open_count=0;
extern "C" {
int SDL_InitSubSystem(std::uint32_t){return 0;}
void SDL_QuitSubSystem(std::uint32_t){}
std::uint32_t SDL_OpenAudioDevice(const char*,int,const NativeAudio::Spec*,NativeAudio::Spec*,int){++fixture_open_count;return 0;}
void SDL_CloseAudioDevice(std::uint32_t){}
void SDL_PauseAudioDevice(std::uint32_t,int){}
int SDL_QueueAudio(std::uint32_t,const void*,std::uint32_t){return -1;}
std::uint32_t SDL_GetQueuedAudioSize(std::uint32_t){return 0;}
const char* SDL_GetError(){return "synthetic unavailable fixture";}
}
'''


SETUP = r'''
 (void)call;
 [[maybe_unused]] auto audio=[&](unsigned op,std::uint64_t x=0,std::uint64_t y=0){r[3]=x;r[4]=y;return audio_service(s,op);};
 auto param=[&](unsigned channels,unsigned blocks,std::uint64_t attr=0,std::uint32_t level=0x3f800000){
   mem.store(0x10100,8,channels);mem.store(0x10108,8,blocks);mem.store(0x10110,8,attr);
   mem.store(0x10118,4,level);mem.store(0x1011c,4,0xaabbccdd);
 };
 (void)param;
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class NativeAudioTests(unittest.TestCase):
    def run_native(self, body, *, words=(BLR,), null=True, prefix=""):
        translated = compile_program(ElfImage.parse(program(*words)), process_entry=True)
        source = translated.cpp(max_steps=500000000).replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source += prefix
        source += r'''
#include <cassert>
template<class F> void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){
 PpuState s;auto& r=s.r;auto& mem=s.memory;mem.map(0x10000,0x2000,6,nullptr,0);
 auto call=[&](unsigned number,std::uint64_t x=0,std::uint64_t y=0){r[11]=number;r[3]=x;r[4]=y;syscall(s);return r[3];};
'''
        if null:
            source += 'setenv("ANYPS3_AUDIO_SINK","null",1);\n'
        source += SETUP + body + '\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-rdynamic', '-Wall', '-Wextra', '-Werror',
                                    str(root / 'test.cpp'), '-o', str(root / 'test')],
                                   capture_output=True, text=True, timeout=60, env=dict(os.environ, SDL_AUDIODRIVER="dummy"))
            self.assertEqual(build.returncode, 0, build.stderr)
            run_env = dict(os.environ, SDL_AUDIODRIVER='dummy')
            run_env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            run_env.pop('ANYPS3_AUDIO_CAPTURE', None)
            run_env.pop('ANYPS3_AUDIO_HOST_CHANNELS', None)
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True, timeout=10, env=run_env)
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stderr

    def test_acit_configs_byte_layout_lifecycle_and_errors(self):
        stderr = self.run_native(r'''
 for(unsigned op=1;op<=6;++op)assert(audio(op)==0x80310703);
 const auto baseline=main_memory_used(s);s.sdk_version=0x270001;
 assert(audio(0)==0 && audio(0)==0x80310701 && s.audio.maximum==4);
 assert(audio(2,0,0x10180)==0x80310704 && audio(2,0x10100,0)==0x80310704);
 assert(audio(3,8,0x10200)==0x80310704 && audio(3,0,0)==0x80310704);
 for(unsigned op=4;op<=6;++op)assert(audio(op,8)==0x80310704);
 assert(audio(4,0)==0x80310707 && audio(5,0)==0x80310708 && audio(6,0)==0x80310707);
 for(auto channels:{1u,6u,0u}){param(channels,8);assert(audio(2,0x10100,0x10180)==0x80310704);}
 for(auto blocks:{0u,2u,4u,7u,9u}){param(2,blocks);assert(audio(2,0x10100,0x10180)==0x80310704);}
 // Both original callsites, including both channel branches of the second.
 const unsigned channels[]={8,2,8},blocks[]={8,16,16};
 for(unsigned n=0;n<3;++n){
   param(channels[n],blocks[n],n?0x1000:0,n?0x3f800000:0x7fc00000);
   assert(audio(2,0x10100,0x10180)==0);const auto id=mem.load(0x10180,4);assert(id==0);
   auto& p=s.audio.ports[id];assert(p.level==1.0f && mem.load(p.index,8)==0);
   std::fill_n(mem.access(0x10200,32,true),32,0xcc);assert(audio(3,id,0x10200)==0);
   assert(mem.load(0x10200,4)==p.index && mem.load(0x10204,4)==1);
   assert(mem.load(0x10208,8)==channels[n] && mem.load(0x10210,8)==blocks[n]);
   assert(mem.load(0x10218,4)==channels[n]*blocks[n]*1024 && mem.load(0x1021c,4)==p.address);
   const auto* config=mem.access(0x10200,32,false);
   assert(config[8]==0 && config[14]==0 && config[15]==channels[n]);
   assert(config[16]==0 && config[22]==0 && config[23]==blocks[n]);
   assert(config[24]==0 && config[25]==(p.size>>16) && config[26]==((p.size>>8)&255) && config[27]==0);
   assert(main_memory_used(s)==baseline+0x20000+(n==2?0x10000:0));
   mem.store(p.address,4,0x3f800000);assert(audio(4,id)==0 && mem.load(p.address,4)==0);
   s.audio.next_tick=UINT64_MAX/2;assert(audio(4,id)==0x80310706);
   assert(audio(3,id,0x10200)==0 && mem.load(0x10204,4)==2);
   assert(audio(5,id)==0 && audio(5,id)==0x80310708);
   assert(audio(3,id,0x10200)==0 && mem.load(0x10204,4)==1);
   assert(audio(4,id)==0);s.audio.next_tick=UINT64_MAX/2;
   const auto address=p.address,index=p.index;
   assert(audio(6,id)==0 && s.audio.running==0 && audio(6,id)==0x80310707);
   fault([&]{mem.load(address,4);});assert(mem.load(index,8)==0);
   assert(audio(3,id,0x10200)==0 && mem.load(0x10204,4)==0x1010);
   assert(mem.load(0x1021c,4)==address && mem.load(0x10208,8)==channels[n]);
 }
 assert(main_memory_used(s)==baseline+0x10000);
 param(2,8);assert(audio(2,0x10100,0x10180)==0);assert(audio(4,0)==0);
 s.audio.next_tick=UINT64_MAX/2;const auto indices=s.audio.indices,ring=s.audio.ports[0].address;
 assert(audio(1)==0 && main_memory_used(s)==baseline && s.audio.running==0);
 fault([&]{mem.load(indices,8);});fault([&]{mem.load(ring,4);});
 assert(audio(1)==0x80310703 && audio(0)==0 && audio(1)==0);
''')
        self.assertEqual(stderr.count('diagnostic null sink'), 1)

    def test_sdk_port_capacity_and_guest_memory_accounting(self):
        self.run_native(r'''
 const auto baseline=main_memory_used(s);
 for(auto sdk:{0x270001u,0x35ffffu,0x360000u}){
   s.sdk_version=sdk;assert(audio(0)==0);const unsigned count=sdk<=0x35ffff?4:8;
   param(2,8);
   for(unsigned i=0;i<count;++i){assert(audio(2,0x10100,0x10180)==0 && mem.load(0x10180,4)==i);}
   mem.store(0x10180,4,0xdeadbeef);assert(audio(2,0x10100,0x10180)==0x80310705);
   assert(mem.load(0x10180,4)==0xdeadbeef && main_memory_used(s)==baseline+(count+1)*0x10000);
   assert(audio(3,7,0x10200)==0 && mem.load(0x10204,4)==(count==8?1:0x1010));
   assert(audio(6,1)==0 && audio(2,0x10100,0x10180)==0 && mem.load(0x10180,4)==1);
   assert(call(349,s.audio.indices)==MM_EINVAL && call(349,s.audio.ports[1].address)==MM_EINVAL);
   assert(audio(1)==0 && main_memory_used(s)==baseline);
 }
 // A reservation must not be mistaken for free address space.
 s.mmapper.areas.push_back({0x10000000,0x10000000,0x10000});assert(audio(0)==0);
 assert(s.audio.indices==0x20000000);param(8,32);assert(audio(2,0x10100,0x10180)==0);
 assert(s.audio.ports[0].address==0x20010000 && s.audio.ports[0].size==0x40000);
 assert(main_memory_used(s)==baseline+0x50000);assert(audio(1)==0);
 s.mmapper.container_reserved=0x10000000-baseline;
 assert(audio(0)==MM_ENOMEM && s.audio.maximum==0 && main_memory_used(s)==0x10000000);
 s.mmapper.container_reserved-=0x10000;assert(audio(0)==0);
 param(2,8);mem.store(0x10180,4,0xdeadbeef);
 assert(audio(2,0x10100,0x10180)==0x8031070b && mem.load(0x10180,4)==0xdeadbeef);
 assert(s.audio.ports[0].status==0x1010 && audio(1)==0);
''')

    def test_be64_index_wrap_block_clear_stop_and_restart(self):
        self.run_native(r'''
 assert(audio(0)==0);param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& a=s.audio;auto& p=a.ports[0];a.next_tick=1000000000;
 for(unsigned i=0;i<8;++i)mem.store(p.address+i*2048,4,0x3e800000);
 mem.store(p.index+8,8,UINT64_C(0x123456789abcdef0));
 audio_advance(s,999999999);assert(mem.load(p.index,8)==0 && a.submitted_frames==0);
 audio_advance(s,1000000000);assert(mem.load(p.index,8)==1 && a.submitted_frames==256);
 const auto* index=mem.access(p.index,8,false);
 for(unsigned i=0;i<7;++i)assert(index[i]==0);
 assert(index[7]==1 && mem.load(p.index+4,4)==1);
 for(unsigned i=0;i<2048;++i)assert(mem.load(p.address+i,1)==0);
 assert(mem.load(p.address+2048,4)==0x3e800000 && mem.load(p.index+8,8)==UINT64_C(0x123456789abcdef0));
 audio_advance(s,1000000000+7*425600);assert(mem.load(p.index,8)==0 && a.submitted_frames==2048);
 for(unsigned i=0;i<p.size;++i)assert(mem.load(p.address+i,1)==0);
 // Mark the deadline as future to keep lifecycle calls independent of host test speed.
 a.next_tick=UINT64_MAX/2;assert(audio(5,0)==0);const auto frames=a.submitted_frames;
 audio_advance(s,UINT64_MAX/2);assert(mem.load(p.index,8)==0 && a.submitted_frames==frames);
 assert(audio(4,0)==0);a.next_tick=UINT64_MAX/2;
 audio_advance(s,UINT64_MAX/2);assert(mem.load(p.index,8)==1);
 a.next_tick=UINT64_MAX/2;assert(audio(5,0)==0 && audio(4,0)==0 && mem.load(p.index,8)==1);
 a.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''')

    def test_sink_selection_unsupported_attributes_and_pointer_failures(self):
        self.run_native(r'''
 unsetenv("ANYPS3_AUDIO_SINK");
 assert(audio(0)==0 && s.audio.null_sink && !s.audio.device && !s.audio.subsystem);
 assert(audio(1)==0);
 setenv("ANYPS3_AUDIO_SINK","bogus",1);fault([&]{audio(0);});
 setenv("ANYPS3_AUDIO_SINK","null",1);assert(audio(0)==0);
 param(2,8,0x10);fault([&]{audio(2,0x10100,0x10180);});assert(s.audio.ports[0].status==0x1010);
 param(2,8,0x1000,0x7f800000);fault([&]{audio(2,0x10100,0x10180);});
 param(2,8);const auto used=main_memory_used(s);
 fault([&]{audio(2,0x11ff0,0x10180);});fault([&]{audio(2,0x10100,0x12000);});
 assert(main_memory_used(s)==used && s.audio.ports[0].status==0x1010);
 fault([&]{audio(3,0,0x11ff0);});fault([&]{audio(9);});assert(audio(1)==0);
''', null=False)

    def test_eight_channel_sixteen_block_wrap_and_stopped_port_is_not_consumed(self):
        self.run_native(r'''
 assert(audio(0)==0);param(8,16,0x1000);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& a=s.audio;auto& p=a.ports[0];a.next_tick=UINT64_MAX/2;
 param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,1)==0);
 auto& stopped=a.ports[1];assert(audio(5,1)==0);
 mem.store(stopped.address,4,0x3f000000);
 for(unsigned i=0;i<16;++i)mem.store(p.address+i*8192+8192-4,4,0x3e800000);
 const auto start=a.next_tick;
 // One call catches up 15 periods; periods k=0..4 end 14-k periods (74.7..53.3 ms) before now, beyond the 50 ms host
 // limit: their guest effects stay, but they are not queued to the host (late_dropped_frames).
 audio_advance(s,start+14*425600);assert(mem.load(p.index,8)==15 && a.submitted_frames==10*256 && a.late_dropped_frames==5*256);
 assert(mem.load(p.address+15*8192+8192-4,4)==0x3e800000);
 audio_advance(s,start+15*425600);assert(mem.load(p.index,8)==0 && a.submitted_frames==11*256 && a.late_dropped_frames==5*256);
 for(unsigned i=0;i<p.size;++i)assert(mem.load(p.address+i,1)==0);
 assert(mem.load(stopped.index,8)==0 && mem.load(stopped.address,4)==0x3f000000);
 a.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''')

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_sdl_actual_queue_pcm_endian_channel_mix_and_level(self):
        self.run_native(r'''
 setenv("SDL_AUDIODRIVER","dummy",1);setenv("ANYPS3_AUDIO_SINK","sdl",1);
 assert(audio(0)==0);auto& a=s.audio;assert(a.device && !a.null_sink);
 a.pause(a.device,1);
 param(8,16,0x1000,0x3f000000);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 a.next_tick=UINT64_MAX/2;
 param(2,16,0x1000,0x3f800000);assert(audio(2,0x10100,0x10180)==0 && audio(4,1)==0);
 auto& p=a.ports[0];auto& stereo=a.ports[1];
 const unsigned bits[]={0x3e000000,0xbe000000,0x3e800000,0x3f000000,0x3f800000,0xbf800000,0x3fc00000,0xbfc00000};
 for(unsigned i=0;i<8;++i)mem.store(p.address+i*4,4,bits[i]);
 mem.store(stereo.address,4,0x3e800000);mem.store(stereo.address+4,4,0xbe800000);
 // Capture exactly what submit passes to the real SDL_QueueAudio function.
 static int (*real_queue)(std::uint32_t,const void*,std::uint32_t)=nullptr;real_queue=a.queue;
 static std::array<float,2048> captured{};
 a.queue=+[](std::uint32_t device,const void* data,std::uint32_t size){
   assert(size==8192);std::memcpy(captured.data(),data,size);return real_queue(device,data,size);
 };
 audio_advance(s,UINT64_MAX/2);
 const float expected[]={0.3125f,-0.3125f,0.125f,0.25f,0.5f,-0.5f,0.75f,-0.75f};
 for(unsigned i=0;i<8;++i)assert(captured[i]==expected[i]);
 for(unsigned i=8;i<2048;++i)assert(captured[i]==0.0f);
 assert(a.queued(a.device)==8192 && a.submitted_frames==256);
 assert(mem.load(p.index,8)==1 && mem.load(stereo.index,8)==1 && mem.load(p.address,4)==0);
 mem.store(p.address+8192,4,0x7fc00000);const auto deadline=a.next_tick;
 fault([&]{audio_advance(s,deadline);});assert(mem.load(p.index,8)==1 && a.queued(a.device)==8192);
 mem.store(p.address+8192,4,0);a.next_tick=UINT64_MAX/2;
 assert(audio(1)==0 && !a.device && !a.subsystem);
''', null=False)

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_sdl_sink_refuses_real_output_devices_without_explicit_opt_in(self):
        # No audible output may ever come from the development PC: without a silent SDL driver and
        # without ANYPS3_AUDIO_AUDIBLE=1 the sink must not even initialize SDL audio.
        self.run_native(r'''
 unsetenv("ANYPS3_AUDIO_AUDIBLE");setenv("ANYPS3_AUDIO_SINK","sdl",1);
 const auto baseline=main_memory_used(s);
 for(const char* driver:{(const char*)nullptr,"pipewire","pulseaudio","alsa",""}){
   if(driver)setenv("SDL_AUDIODRIVER",driver,1);else unsetenv("SDL_AUDIODRIVER");
   try{audio(0);assert(false);}catch(const std::runtime_error& e){assert(std::string(e.what()).find("real output device is disabled")!=std::string::npos);}
   assert(!s.audio.maximum && !s.audio.indices && !s.audio.device && !s.audio.subsystem);
 }
 // The forbidden audible opt-in is never set, including negative test values.
 setenv("SDL_AUDIODRIVER","dummy",1);
 assert(main_memory_used(s)==baseline);
''', null=False)

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_sdl_open_failure_is_explicit_and_leaves_no_guest_allocations(self):
        self.run_native(r'''
 setenv("SDL_AUDIODRIVER","dummy",1);setenv("ANYPS3_AUDIO_SINK","sdl",1);
 s.audio.library=dlopen(nullptr,RTLD_NOW); // Explicit, silent failure fixture.
 const auto baseline=main_memory_used(s);
 try{audio(0);assert(false);}catch(const std::runtime_error& e){assert(std::string(e.what()).find("SDL2 audio")!=std::string::npos);}
 assert(!s.audio.maximum && !s.audio.indices && !s.audio.device && !s.audio.subsystem);
 assert(main_memory_used(s)==baseline && fixture_open_count==2);
''', null=False, prefix=FAILING_SDL)

    def test_translated_guest_polling_progresses_without_service_calls(self):
        # lwz r3,4(r4); cmpwi r3,0; beq -8; blr -- original BE64 low-word polling.
        self.run_native(r'''
 assert(audio(0)==0);param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& p=s.audio.ports[0];mem.store(p.address,4,0x3f000000);
 s.audio.next_tick=timebase_ticks()+798000; // Ten milliseconds, measured on guest clock.
 r[4]=p.index;r[1]=0x11000;s.lr=RETURN_SENTINEL;
 s.pc=CODE_FOR_TEST;anyps3_entry(s,false);
 assert(s.returned && r[3]!=0 && s.steps>=256 && mem.load(p.address,4)==0);
 assert(s.audio.submitted_frames>=256);s.audio.next_tick=UINT64_MAX/2;assert(audio(1)==0);
'''.replace('CODE_FOR_TEST', hex(CODE_ADDRESS)),
                        words=(0x80640004, 0x2c030000, 0x4182fff8, BLR))

    def test_seven_import_bindings_keep_signed_error_abi(self):
        bindings = [('0x0b168f92', 'cellAudioInit'), ('0xca5ac370', 'cellAudioQuit'),
                    ('0xcd7bc431', 'cellAudioPortOpen'), ('0x74a66af0', 'cellAudioGetPortConfig'),
                    ('0x89be28f2', 'cellAudioPortStart'), ('0x5b1e2c73', 'cellAudioPortStop'),
                    ('0x4129fe2d', 'cellAudioPortClose')]
        for operation, (nid, name) in enumerate(bindings):
            self.assertEqual(IMPORTS[('cellAudio', nid)],
                             (name, f'r[3]=sign_word(audio_service(state,{operation}));'))
