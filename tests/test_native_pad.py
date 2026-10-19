"""Exercise the actual SDL2 input path with a virtual device, without game data."""
import ctypes.util
import shutil
import unittest
from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++') and ctypes.util.find_library('SDL2'), 'Requires C++ and SDL2')
class NativePadTests(unittest.TestCase):
    def test_virtual_controller_input_hotplug_and_lifecycle(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;
 assert(pad_service(s,1)==0x80121104);r[3]=0;assert(pad_service(s,0)==0x80121102);
 auto& p=s.pad;p.load();assert(p.init(0x2000)==0);
 const auto attach=p.symbol<int(*)(int,int,int,int)>("SDL_JoystickAttachVirtual");
 const auto detach=p.symbol<int(*)(int)>("SDL_JoystickDetachVirtual");
 const auto joyopen=p.symbol<void*(*)(int)>("SDL_JoystickOpen");
 const auto joyclose=p.symbol<void(*)(void*)>("SDL_JoystickClose");
 const auto setbutton=p.symbol<int(*)(void*,int,std::uint8_t)>("SDL_JoystickSetVirtualButton");
 const auto setaxis=p.symbol<int(*)(void*,int,std::int16_t)>("SDL_JoystickSetVirtualAxis");
 const auto device=attach(1,6,15,0);assert(device>=0);
 auto* joystick=joyopen(device);assert(joystick);
 assert(setaxis(joystick,4,-32768)==0 && setaxis(joystick,5,-32768)==0);
 r[3]=7;assert(pad_service(s,0)==0 && pad_service(s,0)==0x80121103);
 unsigned port=7;for(unsigned i=0;i<7;++i)if(p.ports[i].instance==p.device_id(device))port=i;
 assert(port<7);
 r[3]=0x10100;assert(pad_service(s,2)==0);
 assert(mem.load(0x10100,4)==7 && mem.load(0x1010c+port*4,4)==3);
 assert(mem.load(0x10144+port*4,4)==9);
 assert(pad_service(s,2)==0 && mem.load(0x1010c+port*4,4)==1);
 r[3]=port;r[4]=0x10200;assert(pad_service(s,4)==0 && mem.load(0x10200,4)==8);
 assert(mem.load(0x10206,2)==0x74 && mem.load(0x1020a,2)==0);
 assert(mem.load(0x10210,2)==128 && mem.load(0x10212,2)==128);
 assert(pad_service(s,4)==0 && mem.load(0x10200,4)==0);
 assert(setbutton(joystick,0,1)==0 && setbutton(joystick,6,1)==0 && setaxis(joystick,0,32767)==0);
 assert(pad_service(s,4)==0 && mem.load(0x10200,4)==8);
 assert(mem.load(0x10208,2)==8 && mem.load(0x1020a,2)==0x40 && mem.load(0x10210,2)==255);
 r[4]=2;assert(pad_service(s,3)==0);r[4]=0x10200;
 assert(pad_service(s,4)==0 && mem.load(0x10200,4)==20 && mem.load(0x10220,2)==255);
 assert(setbutton(joystick,0,0)==0 && setbutton(joystick,6,0)==0 && setaxis(joystick,0,-32768)==0);
 assert(pad_service(s,4)==0 && mem.load(0x1020a,2)==0 && mem.load(0x10210,2)==0);
 joyclose(joystick);assert(detach(device)==0);
 r[3]=0x10100;assert(pad_service(s,2)==0 && mem.load(0x1010c+port*4,4)==2);
 r[3]=port;r[4]=0x10200;mem.store(0x10200,4,99);
 assert(pad_service(s,4)==0x80121107 && mem.load(0x10200,4)==99);
 assert(pad_service(s,1)==0 && pad_service(s,1)==0x80121104);
 p.quit(0x2000);
''')
