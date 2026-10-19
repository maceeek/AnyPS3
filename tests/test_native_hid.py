"""cellKb/cellMouse library state and cellPadSetActDirect without an actuator."""
import ctypes.util
import shutil
import unittest

from anyps3.services import IMPORTS
from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class NativeHidTests(unittest.TestCase):
    def test_game_imports_are_bound(self):
        # NIDs of the original ACIT sys_io import stubs 0xa4a7dc..0xa4a91c.
        for nid, name in (('0x433f6ec0', 'cellKbInit'), ('0xbfce3285', 'cellKbEnd'), ('0xa5f85e4d', 'cellKbSetCodeType'),
                          ('0xdeefdfa7', 'cellKbSetReadMode'), ('0xc9030138', 'cellMouseInit'),
                          ('0xe10183ce', 'cellMouseEnd'), ('0xf65544ee', 'cellPadSetActDirect')):
            self.assertEqual(IMPORTS[('sys_io', nid)][0], name)

    def test_keyboard_and_mouse_lifecycle_parameters_and_errors(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& h=s.hid;
 // Before init every keyboard call reports CELL_KB_ERROR_UNINITIALIZED.
 assert(hid_service(s,1)==0x80121004);r[3]=0;r[4]=1;assert(hid_service(s,2)==0x80121004 && hid_service(s,3)==0x80121004);
 r[3]=0;assert(hid_service(s,0)==0x80121002);r[3]=128;assert(hid_service(s,0)==0x80121002 && h.keyboards==0);
 r[3]=UINT64_C(0xffffffff00000002);assert(hid_service(s,0)==0 && h.keyboards==2);   // u32 argument
 r[3]=1;assert(hid_service(s,0)==0x80121003 && h.keyboards==2);
 r[3]=1;r[4]=1;assert(hid_service(s,2)==0 && h.code_type[1]==1 && h.read_mode[1]==0);
 r[3]=126;r[4]=1;assert(hid_service(s,3)==0 && h.read_mode[126]==1);
 r[3]=127;r[4]=0;assert(hid_service(s,2)==0x80121002 && hid_service(s,3)==0x80121002);
 r[3]=0;r[4]=2;assert(hid_service(s,2)==0x80121002 && hid_service(s,3)==0x80121002 && h.code_type[0]==0);
 assert(hid_service(s,1)==0 && h.keyboards==0 && hid_service(s,1)==0x80121004);
 r[3]=3;assert(hid_service(s,0)==0 && h.code_type[1]==0 && h.read_mode[126]==0);  // re-init resets config
 // Mouse library is independent.
 assert(hid_service(s,5)==0x80121204);r[3]=0;assert(hid_service(s,4)==0x80121202);
 r[3]=127;assert(hid_service(s,4)==0 && h.mice==127 && hid_service(s,4)==0x80121203);
 assert(hid_service(s,5)==0 && hid_service(s,5)==0x80121204 && h.keyboards==3);
 fault([&]{hid_service(s,6);});
''')

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_set_act_direct_reports_no_device_or_missing_actuator(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& p=s.pad;
 r[3]=0;r[4]=0x10100;assert(pad_service(s,5)==0x80121104);
 p.load();assert(p.init(0x2000)==0);
 const auto attach=p.symbol<int(*)(int,int,int,int)>("SDL_JoystickAttachVirtual");
 const auto detach=p.symbol<int(*)(int)>("SDL_JoystickDetachVirtual");
 r[3]=7;assert(pad_service(s,0)==0);
 // No controller: CELL_PAD_ERROR_NO_DEVICE; bad port / NULL param: INVALID_PARAMETER.
 r[3]=0;r[4]=0x10100;assert(pad_service(s,5)==0x80121107);
 r[3]=127;assert(pad_service(s,5)==0x80121102);r[3]=0;r[4]=0;assert(pad_service(s,5)==0x80121102);
 const auto device=attach(1,6,15,0);assert(device>=0);
 unsigned port=7;r[3]=0x10200;assert(pad_service(s,2)==0);
 for(unsigned i=0;i<7;++i)if(p.ports[i].instance==p.device_id(device))port=i;
 assert(port<7 && mem.load(0x10244+port*4,4)==9);   // capability: PS3 conformity (1) + HP analog (8); no press/sensor/actuator
 mem.store(0x10100,8,UINT64_C(0xff80000000000000));r[3]=port;r[4]=0x10100;
 assert(pad_service(s,5)==0x80121108);              // CELL_PAD_ERROR_UNSUPPORTED_GAMEPAD
 // Reserved bytes are checked only for SDK versions above 0x1fffff (signed).
 mem.store(0x10106,1,1);s.sdk_version=0x1fffff;assert(pad_service(s,5)==0x80121108);
 s.sdk_version=0x200000;assert(pad_service(s,5)==0x80121102);
 s.sdk_version=0x80000000u;assert(pad_service(s,5)==0x80121108);
 assert(mem.load(0x10100,8)==UINT64_C(0xff80000000000100));  // parameter block not modified
 assert(detach(device)==0);s.sdk_version=0;mem.store(0x10106,1,0);assert(pad_service(s,5)==0x80121107);
 assert(pad_service(s,1)==0);p.quit(0x2000);
''')


if __name__ == '__main__':
    unittest.main()
