"""NPDR drmrequest over the native sysutil ring, using the request captured in run 276."""
import shutil
import unittest

from tests import test_runtime_objects

# native-virtual-276 trace (ANYPS3_SYSUTIL_TRACE): sceNpDrm reqtype 0 for
# /dev_hdd0/game/BCAS20098/USRDIR/preorder_discovery.edat, 448 bytes on channel 0.
REQUEST = bytes.fromhex('4e50445200000110000000400000003c0000008000000000000000800000001a000000a000000000000000a000000000000000a00000012000000000000000000000000000000002ffffffffffffffffffffffffffffffffffffffff0000000b000000010000000000000000000000130000000600000000000001180000000064726d72657175657374007265717479706500726571617267000000000000000000000172283952059151d386a7e6a810be96722f6465765f686464302f67616d652f4243415332303039382f5553524449522f7072656f726465725f646973636f766572792e65646174000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000')


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class NpDrmTests(unittest.TestCase):
    def test_missing_edat_reports_enoent_and_existing_or_other_shapes_stop(self):
        load = ''.join(f'bytes[0x400+{i}]={b};' for i, b in enumerate(REQUEST))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto request=[&]{put(12,0);put(16,448);put(20,2);put(24,1);''' + load + r'''};
 const auto root=std::filesystem::temp_directory_path()/("anyps3-npdrm-"+std::to_string(::getpid()));
 std::filesystem::remove_all(root);std::filesystem::create_directories(root);s.fs.configure_hdd(root.c_str());
 request();assert(service_sysutil_connect(s,0x86000000));
 const unsigned reply=0x400+word(12);
 assert(word(reply)==0x4e504452 && word(reply+68)==1 && word(reply+84)==UINT32_MAX);
 assert(std::string(reinterpret_cast<const char*>(bytes.data()+reply+word(reply+24)+word(reply+92)))=="result");
 assert(word(reply+96)==1 && word(reply+100)==0x80010006);                     // CELL_ENOENT
 std::filesystem::create_directories(root/"game/BCAS20098/USRDIR");
 {std::ofstream f(root/"game/BCAS20098/USRDIR/preorder_discovery.edat");f<<"NPD";}
 request();auto before=bytes;fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
 request();put(0x400+100,2);before=bytes;fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
 std::filesystem::remove_all(root);
''')


if __name__ == '__main__':
    unittest.main()
