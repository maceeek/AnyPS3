"""Host input injection (ANYPS3_PAD_INJECT) through the real cellPad service.

Expectations come from the PS3 cellPad ABI, not from the implementation:
PSL1GHT io/pad.h CELL_PAD_CTRL_START=0x0008 (digital1, word 2),
CELL_PAD_CTRL_CROSS=0x0040 / CELL_PAD_CTRL_L2=0x0001 (digital2, word 3),
CELL_PAD_BTN_OFFSET_PRESS_CROSS=14, PRESS_L2=18; CellPadData is
s32 len + u16 button[64] (button[0..1] reserved/status). cellPadGetInfo2:
port_status bit0 CONNECTED, bit1 ASSIGN_CHANGES; capability 1|8 =
PS3 conformity + HP analog sticks (RPCS3 pad_types.h:136-140).
Real controllers are excluded with SDL's allow-list hint so a host gamepad
cannot change the expected port assignment.
"""
import ctypes.util
import shutil
import unittest
from tests import test_runtime_objects

PRELUDE = r'''
 (void)call;
 setenv("SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT","0x0000/0x0000",1);
 char dir_template[]="/tmp/anyps3-pad-inject-XXXXXX";assert(mkdtemp(dir_template));
 const std::string path=std::string(dir_template)+"/inject.txt";
 auto put=[&](const char* text){
   {std::ofstream f(path+".next");f<<text;}
   std::filesystem::rename(path+".next",path);
   std::this_thread::sleep_for(std::chrono::milliseconds(15)); // > 10ms refresh interval
 };
 auto info=[&](){r[3]=0x10100;assert(pad_service(s,2)==0);};
 auto data=[&](){r[3]=0;r[4]=0x10200;mem.store(0x10200,4,0xffffffff);return pad_service(s,4);};
 auto& p=s.pad;(void)p;(void)data;(void)info;(void)put;
'''


@unittest.skipUnless(shutil.which('c++') and ctypes.util.find_library('SDL2'), 'Requires C++ and SDL2')
class NativePadInjectTests(unittest.TestCase):
    def run_native(self, body):
        test_runtime_objects.RuntimeObjectsTests().run_native(
            PRELUDE + body + '\n std::filesystem::remove_all(dir_template);')

    def test_inactive_without_environment(self):
        self.run_native(r'''
 unsetenv("ANYPS3_PAD_INJECT");r[3]=7;assert(pad_service(s,0)==0);
 info();assert(mem.load(0x10104,4)==0 && mem.load(0x1010c,4)==0 && mem.load(0x10144,4)==0);
 assert(data()==0x80121107 && mem.load(0x10200,4)==0xffffffff);
 assert(pad_service(s,1)==0);
''')

    def test_injection_only_port_buttons_pressure_and_detach(self):
        self.run_native(r'''
 put("v1 1 0 0\n");setenv("ANYPS3_PAD_INJECT",path.c_str(),1);
 r[3]=7;assert(pad_service(s,0)==0);
 info();assert(mem.load(0x10104,4)==1);             // one connected port
 assert(mem.load(0x1010c,4)==3);                    // CONNECTED|ASSIGN_CHANGES
 assert(mem.load(0x10144,4)==9);                    // conformity + HP sticks, no actuator/sensor
 info();assert(mem.load(0x1010c,4)==1 && mem.load(0x10110,4)==0);
 assert(data()==0 && mem.load(0x10200,4)==8);       // 8 halfwords in digital mode
 assert(mem.load(0x10208,2)==0 && mem.load(0x1020a,2)==0);
 assert(mem.load(0x1020c,2)==128 && mem.load(0x1020e,2)==128 && mem.load(0x10210,2)==128 && mem.load(0x10212,2)==128);
 assert(data()==0 && mem.load(0x10200,4)==0);       // unchanged -> len 0
 put("v1 1 8 0\n");                                 // START
 assert(data()==0 && mem.load(0x10200,4)==8 && mem.load(0x10208,2)==0x0008 && mem.load(0x1020a,2)==0);
 put("v1 1 0 0\n");assert(data()==0 && mem.load(0x10208,2)==0);
 r[3]=0;r[4]=2;assert(pad_service(s,3)==0);         // CELL_PAD_SETTING_PRESS_ON
 put("v1 1 0 65\n");                                // CROSS + L2
 assert(data()==0 && mem.load(0x10200,4)==20 && mem.load(0x1020a,2)==0x41);
 assert(mem.load(0x10204+14*2,2)==255 && mem.load(0x10204+18*2,2)==255);
 assert(mem.load(0x10204+15*2,2)==0 && mem.load(0x10204+19*2,2)==0);
 r[3]=0;r[4]=0x10300;assert(pad_service(s,5)==0x80121108); // no actuator on an injected-only port
 put("v1 0 0 0\n");                                 // detach
 info();assert(mem.load(0x10104,4)==0 && mem.load(0x1010c,4)==2 && mem.load(0x10144,4)==0);
 assert(data()==0x80121107 && mem.load(0x10200,4)==0xffffffff);
 put("v1 1 0 0\n");info();assert(mem.load(0x1010c,4)==3); // re-attach is reported
 for(const char* bad:{"v2 1 0 0\n","v1 2 0 0\n","v1 1 512 0\n","v1 1 0 256\n","v1 1 0 0 9\n","v1 1 -1 0\n","v1 1\n",""}){
   put(bad);fault([&]{info();});
 }
 std::filesystem::remove(path);std::this_thread::sleep_for(std::chrono::milliseconds(15));
 fault([&]{info();});                               // vanished file is an explicit stop, not neutral input
 put("v1 0 0 0\n");assert(pad_service(s,1)==0);
''')

    def test_injection_ors_onto_physical_port_and_keeps_it_on_physical_detach(self):
        self.run_native(r'''
 put("v1 1 0 0\n");setenv("ANYPS3_PAD_INJECT",path.c_str(),1);
 p.load();assert(p.init(0x2000)==0);
 const auto attach=p.symbol<int(*)(int,int,int,int)>("SDL_JoystickAttachVirtual");
 const auto detach=p.symbol<int(*)(int)>("SDL_JoystickDetachVirtual");
 const auto joyopen=p.symbol<void*(*)(int)>("SDL_JoystickOpen");
 const auto joyclose=p.symbol<void(*)(void*)>("SDL_JoystickClose");
 const auto setbutton=p.symbol<int(*)(void*,int,std::uint8_t)>("SDL_JoystickSetVirtualButton");
 const auto setaxis=p.symbol<int(*)(void*,int,std::int16_t)>("SDL_JoystickSetVirtualAxis");
 const auto device=attach(1,6,15,0);assert(device>=0);
 auto* joystick=joyopen(device);assert(joystick);
 assert(setaxis(joystick,4,-32768)==0 && setaxis(joystick,5,-32768)==0);
 r[3]=7;assert(pad_service(s,0)==0);
 assert(p.ports[0].instance==p.device_id(device));  // the physical pad shares port 0
 info();assert(mem.load(0x10104,4)==1 && mem.load(0x1010c,4)==3);
 put("v1 1 8 0\n");assert(!p.inject.physical_reported);
 assert(data()==0 && mem.load(0x10208,2)==0x0008 && mem.load(0x1020a,2)==0);
 assert(!p.inject.physical_reported);               // injected bits are not "player input"
 assert(setbutton(joystick,0,1)==0);                // physical CROSS (SDL A)
 assert(data()==0 && mem.load(0x10208,2)==0x0008 && mem.load(0x1020a,2)==0x40);
 assert(p.inject.physical_reported);
 put("v1 1 0 0\n");assert(data()==0 && mem.load(0x10208,2)==0 && mem.load(0x1020a,2)==0x40);
 assert(setbutton(joystick,0,0)==0);assert(data()==0 && mem.load(0x1020a,2)==0);
 joyclose(joystick);assert(detach(device)==0);
 info();assert(mem.load(0x10104,4)==1 && (mem.load(0x1010c,4)&1)==1 && mem.load(0x10144,4)==9);
 assert(data()==0 && mem.load(0x10208,2)==0);       // still connected through injection
 put("v1 0 0 0\n");info();assert(mem.load(0x10104,4)==0 && mem.load(0x1010c,4)==2);
 assert(pad_service(s,1)==0);p.quit(0x2000);
''')


if __name__ == '__main__':
    unittest.main()
