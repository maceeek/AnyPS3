"""Native cellPad ABI against an isolated, dynamically loaded fake SDL2.

Golden sensor values come from dimensional examples, not runtime helpers.
No real SDL device, audio, game bytes or full-runtime compilation is used.
"""
import os
import resource
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.native_pad import TYPES, CPP

FAKE_SDL = r'''
#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <mutex>
#include <vector>
static std::atomic<int> flags[2],failures{0};
static std::atomic<unsigned> guide{0},cross{0},enabled[2][3],closes{0};
static std::mutex log_mutex;
static std::vector<std::array<unsigned,4>> calls;
static float accel[3]{0,9.80665f,0},gyro[3]{0,0,0};
static int port(void* p){return int(reinterpret_cast<std::uintptr_t>(p))-1;}
extern "C" {
void fake_config(int p,int f){flags[p]=f;}
void fake_fail(int f){failures=f;}
void fake_buttons(int ps,int x){guide=ps;cross=x;}
void fake_motion(float x,float y,float z,float gx,float gy,float gz){
 accel[0]=x;accel[1]=y;accel[2]=z;gyro[0]=gx;gyro[1]=gy;gyro[2]=gz;
}
unsigned fake_enabled(int p,int s){return enabled[p][s];}
unsigned fake_closed(){return closes;}
unsigned fake_calls(){std::lock_guard<std::mutex> l(log_mutex);return calls.size();}
unsigned fake_call(unsigned n,unsigned c){std::lock_guard<std::mutex> l(log_mutex);return calls.at(n).at(c);}
int SDL_InitSubSystem(std::uint32_t f){assert(f==0x2000);return 0;}
void SDL_QuitSubSystem(std::uint32_t f){assert(f==0x2000);}
int SDL_NumJoysticks(){return 2;}
int SDL_IsGameController(int i){return (flags[i]&1)!=0;}
int SDL_JoystickGetDeviceInstanceID(int i){return i+100;}
void* SDL_GameControllerOpen(int i){return reinterpret_cast<void*>(std::uintptr_t(i+1));}
void SDL_GameControllerClose(void*){++closes;}
void SDL_GameControllerUpdate(){}
int SDL_GameControllerGetAttached(void* p){return flags[port(p)]&1;}
std::uint8_t SDL_GameControllerGetButton(void*,int b){return b==5?guide.load():b==0?cross.load():0;}
std::int16_t SDL_GameControllerGetAxis(void*,int){return 0;}
#ifndef NO_OPTIONAL
#ifndef LEGACY_RUMBLE
int SDL_GameControllerHasRumble(void* p){return (flags[port(p)]&2)!=0;}
#endif
int SDL_GameControllerRumble(void* p,std::uint16_t low,std::uint16_t high,std::uint32_t ms){
 std::lock_guard<std::mutex> l(log_mutex);calls.push_back({unsigned(port(p)),low,high,ms});
 return (failures&1) || !(flags[port(p)]&2)?-1:0;
}
int SDL_GameControllerHasSensor(void* p,int type){return (flags[port(p)]&(type==1?4:8))!=0;}
int SDL_GameControllerSetSensorEnabled(void* p,int type,int on){
 if(failures&(type==1?2:4))return -1;
 enabled[port(p)][type]=on;return 0;
}
int SDL_GameControllerGetSensorData(void* p,int type,float* data,int n){
 assert(n==3 && enabled[port(p)][type]);
 if(failures&(type==1?8:16))return -1;
 for(int i=0;i<3;++i)data[i]=(type==1?accel:gyro)[i];return 0;
}
#endif
}
'''

PREAMBLE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
struct Memory {
 std::array<std::uint8_t,1024> bytes{};
 std::uint8_t* access(unsigned a,unsigned n,bool){if(a+n>bytes.size())throw std::runtime_error("bounds");return bytes.data()+a;}
 void store(unsigned a,unsigned n,std::uint64_t v){access(a,n,true);for(unsigned i=0;i<n;++i)bytes[a+n-1-i]=v>>(8*i);}
 std::uint64_t load(unsigned a,unsigned n){access(a,n,false);std::uint64_t v=0;for(unsigned i=0;i<n;++i)v=(v<<8)|bytes[a+i];return v;}
};
'''

NATIVE = r'''
int main(int argc,char** argv){
 assert(argc==2);PpuState s;auto& p=s.pad;auto& m=s.memory;const std::string mode=argv[1];
 const auto call=[&](unsigned op,unsigned a=0,unsigned b=0){s.r[3]=a;s.r[4]=b;return pad_service(s,op);};
 assert(call(5,0,128)==0x80121104);
 p.load();
 const auto config=p.symbol<void(*)(int,int)>("fake_config");
 const auto fail=p.symbol<void(*)(int)>("fake_fail");
 const auto buttons=p.symbol<void(*)(int,int)>("fake_buttons");
 const auto motion=p.symbol<void(*)(float,float,float,float,float,float)>("fake_motion");
 const auto enabled=p.symbol<unsigned(*)(int,int)>("fake_enabled");
 const auto calls=p.symbol<unsigned(*)()>("fake_calls");
 const auto event=p.symbol<unsigned(*)(unsigned,unsigned)>("fake_call");
 config(0,15);config(1,1);assert(call(0,2)==0);
 const auto caps=[&](unsigned port){assert(call(2,256)==0);return m.load(324+port*4,4);};
 const auto setting=[&](unsigned bits){return call(3,0,bits);};
 const auto data=[&]{return call(4,0,512);};
 const auto word=[&](unsigned n){return m.load(516+n*2,2);};
 const auto motors=[&](unsigned small,unsigned large){m.store(128,8,0);m.store(128,1,small);m.store(129,1,large);return call(5,0,128);};
 if(mode=="capabilities"){
   assert(caps(0)==29 && caps(1)==9);
   assert(setting(4)==0 && enabled(0,1)==1 && enabled(0,2)==1);
   assert(call(3,1,4)==0 && call(4,1,512)==0x80121108);
   assert(call(3,126,4)==0 && call(3,127,4)==0x80121102);
   config(0,0);assert(setting(6)==0); // disconnected setting still accepted
   assert(caps(0)==0);config(0,7);assert(caps(0)==25); // only accel
   assert(data()==0x80121108);
   config(0,0);p.poll();config(0,11);assert(caps(0)==25 && data()==0x80121108); // only gyro
   config(0,0);p.poll();config(0,15);assert(caps(0)==29);
   assert(enabled(0,1)==1 && enabled(0,2)==1 && data()==0); // stored setting on reconnect
 }else if(mode=="missing" || mode=="muted"){
   assert(caps(0)==(mode=="missing"?9:13) && motors(1,255)==0x80121108);
   assert(setting(4)==0);
   if(mode=="missing")assert(data()==0x80121108);
   else assert(data()==0);
 }else if(mode=="legacy"){
   assert(!p.has_rumble && p.rumble && caps(0)==13 && caps(1)==9);
   assert(motors(1,255)==0x80121108 && calls()==0);
 }else if(mode=="rumble"){
   assert(call(5,127,128)==0x80121102 && call(5,0,0)==0x80121102);
   assert(call(5,2,128)==0x80121107 && call(5,1,128)==0x80121108);
   s.sdk_version=0x200000;m.store(135,1,1);assert(call(5,0,128)==0x80121102 && calls()==0);
   s.sdk_version=0x1fffff;assert(call(5,0,128)==0);s.sdk_version=0;
   assert(motors(2,128)==0);const auto first=calls()-1;
   assert(event(first,0)==0 && event(first,1)==32896 && event(first,2)==65535 && event(first,3)==1000);
   // No guest poll: a PS3 actuator stays latched beyond SDL's 1s lease.
   std::this_thread::sleep_for(std::chrono::milliseconds(1150));
   assert(calls()>=first+4 && event(calls()-1,1)==32896 && event(calls()-1,2)==65535);
   assert(motors(0,1)==0);assert(event(calls()-1,1)==257 && event(calls()-1,2)==0);
   assert(motors(1,255)==0);assert(event(calls()-1,1)==65535 && event(calls()-1,2)==65535);
   assert(motors(0,0)==0);auto stopped=calls();assert(event(stopped-1,1)==0 && event(stopped-1,2)==0 && event(stopped-1,3)==0);
   std::this_thread::sleep_for(std::chrono::milliseconds(300));assert(calls()==stopped);
   fail(1);assert(motors(1,1)==0x80121108);fail(0);
   assert(motors(1,255)==0);config(0,0);p.poll();assert(event(calls()-1,1)==0 && event(calls()-1,2)==0);
   config(0,15);p.poll();stopped=calls();std::this_thread::sleep_for(std::chrono::milliseconds(300));assert(calls()==stopped);
   assert(motors(1,255)==0);assert(call(1)==0);assert(event(calls()-1,1)==0 && event(calls()-1,2)==0);
   stopped=calls();std::this_thread::sleep_for(std::chrono::milliseconds(300));assert(calls()==stopped);
   assert(call(0,2)==0 && motors(1,255)==0); // worker can restart after End/Init
 }else if(mode=="shutdown_failure"){
   assert(motors(1,255)==0);fail(1);
   assert(call(1)==0x80121101 && p.maximum==0 && !p.feedback.joinable());
   fail(0);
 }else if(mode=="feedback_failure"){
   assert(motors(1,255)==0);fail(1);
   std::this_thread::sleep_for(std::chrono::milliseconds(600));
   bool hit=false;try{p.poll();}catch(const std::runtime_error&){hit=true;}assert(hit);
 }else if(mode=="motion"){
   m.store(560,4,0xdeadbeef);assert(data()==0 && m.load(512,4)==8 && word(1)==0x74);
   assert(m.load(560,4)==0xdeadbeef); // sensor-off output span remains 20 words
   buttons(1,1);assert(data()==0 && word(2)==0x100 && word(3)==0x40);
   assert(m.bytes[520]==1 && m.bytes[521]==0); // DIGITAL1 big endian
   assert(setting(2)==0 && data()==0 && m.load(512,4)==20 && word(1)==0x7a && word(14)==255);
   assert(setting(4)==0 && data()==0 && m.load(512,4)==24 && word(1)==0x7c && word(14)==0);
   // Resting face-up: SDL +1g on Y => PS3 Y = 512-113 = 399.
   assert(word(20)==512 && word(21)==399 && word(22)==512 && word(23)==512);
   assert(m.bytes[558]==1 && m.bytes[559]==143); // Y = 0x018f
   motion(-9.80665f,0,9.80665f,100,1.5707963267948966f,-100);
   assert(data()==0 && word(20)==625 && word(21)==512 && word(22)==399 && word(23)==635);
   motion(9.80665f,-9.80665f,0,-200,-1.5707963267948966f,200);
   assert(data()==0 && word(20)==399 && word(21)==625 && word(22)==512 && word(23)==389);
   motion(100,-100,0,0,100,0);assert(data()==0 && word(20)==0 && word(21)==1023 && word(23)==1023);
   motion(0,0,-100,0,-100,0);assert(data()==0 && word(22)==1023 && word(23)==0);
   // Half g gives 56.5 counts; PS3 truncates, not rounds.
   motion(4.903325f,0,0,0,0,0);assert(data()==0 && word(20)==455);
   p.ports[0].last_report=std::chrono::steady_clock::now()-std::chrono::milliseconds(11);
   assert(data()==0 && m.load(512,4)==24); // unchanged sensor report ~100Hz
   assert(setting(6)==0 && data()==0 && word(14)==255);
   assert(setting(0)==0 && !enabled(0,1) && !enabled(0,2));
   assert(data()==0 && m.load(512,4)==8 && word(2)==0x100);
 }else if(mode=="sensor_errors"){
   fail(2);assert(setting(4)==0x80121108 && p.ports[0].setting==0);
   fail(4);assert(setting(4)==0x80121108 && !enabled(0,1) && !enabled(0,2));
   fail(0);assert(setting(4)==0);
   for(int f:{8,16}){fail(f);m.store(512,4,0xabcdef01);assert(data()==0x80121106 && m.load(512,4)==0xabcdef01);}
   fail(0);motion(std::numeric_limits<float>::quiet_NaN(),0,0,0,0,0);assert(data()==0x80121106);
   motion(0,0,0,0,std::numeric_limits<float>::infinity(),0);assert(data()==0x80121106);
   fail(4);assert(setting(0)==0x80121108 && p.ports[0].setting==4 && enabled(0,1) && enabled(0,2));
   fail(0);assert(setting(0)==0);
 }else return 99;
 p.shutdown();assert(!p.feedback.joinable());
}
'''


def source(types=TYPES, cpp=CPP):
    return "\n".join((PREAMBLE, types,
        "struct PpuState { NativePad pad; Memory memory; std::array<std::uint64_t,32> r{}; unsigned sdk_version=0; };",
        cpp, NATIVE))


@unittest.skipUnless(shutil.which("c++"), "Requires C++")
class PadMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="anyps3-padmotion-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        (cls.root / "fake.cpp").write_text(FAKE_SDL)
        for name, defines in (("full", []), ("legacy", ["-DLEGACY_RUMBLE"]), ("missing", ["-DNO_OPTIONAL"])):
            directory = cls.root / name
            directory.mkdir()
            cls.compile(["-shared", "-fPIC", *defines, str(cls.root / "fake.cpp"), "-o", str(directory / "libSDL2-2.0.so.0")])
        cls.binary = cls.build_source(source(), "test")

    @classmethod
    def compile(cls, args):
        result = subprocess.run(["c++", "-std=c++17", "-O1", "-pthread", *args],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr)

    @classmethod
    def build_source(cls, text, name):
        path = cls.root / (name + ".cpp")
        path.write_text(text)
        binary = cls.root / name
        cls.compile(["-Wall", "-Wextra", "-Werror", str(path), "-ldl", "-o", str(binary)])
        return binary

    @classmethod
    def run_case(cls, mode, binary=None, library="full", muted=False):
        env = dict(os.environ, LD_LIBRARY_PATH=str(cls.root / library),
                   SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null", ANYPS3_PAD_RUMBLE="0" if muted else "1")
        return subprocess.run([str(binary or cls.binary), mode], env=env,
                              capture_output=True, text=True, timeout=10,
                              preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))

    def test_capabilities_and_reconnect(self):
        self.assert_pass("capabilities")

    def test_rumble_values_latching_stops_and_errors(self):
        self.assert_pass("rumble")

    def test_shutdown_failure_is_not_reported_as_success(self):
        self.assert_pass("shutdown_failure")

    def test_asynchronous_rumble_error_is_not_hidden(self):
        self.assert_pass("feedback_failure")

    def test_motion_layout_scales_endianness_and_ps(self):
        self.assert_pass("motion")

    def test_sensor_enable_read_errors_and_nonfinite_data(self):
        self.assert_pass("sensor_errors")

    def test_optional_symbols_and_rumble_policy(self):
        self.assert_pass("legacy", library="legacy")
        self.assert_pass("missing", library="missing")
        self.assert_pass("muted", muted=True)

    def assert_pass(self, mode, **kwargs):
        result = self.run_case(mode, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
