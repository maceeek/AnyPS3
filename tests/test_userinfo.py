"""cellUserInfoGetStat over the native sysutil ring, using the request captured in run 344load."""
import shutil
import unittest

from tests import test_runtime_objects

# native-virtual-344load stderr "sysutil request words" (USRI getStat, elmUserId=0 = current user), 144 bytes.
REQUEST = bytes.fromhex(
    '5553524900000110000000400000002c00000070000000000000007000000012'
    '0000009000000000000000900000000000000090000000000000000000000000'
    '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000008'
    '0000000100000000000000000000000067657453746174 00656c6d5573657249'.replace(' ', '') +
    '64000000000000000000000000000000')


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class UserInfoTests(unittest.TestCase):
    def test_getstat_reads_localusername_and_rejects_other_shapes(self):
        self.assertEqual(len(REQUEST), 144)
        load = ''.join(f'bytes[0x8400+{i}]={b};' for i, b in enumerate(REQUEST))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& bytes=*s.mmapper.objects.front().bytes;
 char hdd[]="/tmp/anyps3-userinfo-XXXXXX";assert(::mkdtemp(hdd));s.fs.configure_hdd(hdd);
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto request=[&]{put(0x8000,0x400);put(0x8004,0x7c00);put(0x8008,144);put(0x800c,0);put(0x8010,144);put(0x8014,2);put(0x8018,1);''' + load + r'''};
 auto text=[&](unsigned reply,unsigned name){return std::string(reinterpret_cast<const char*>(bytes.data()+reply+word(reply+24)+name));};
 // A console without the user's localusername is not a state the original VSH produces: explicit stop, ring untouched.
 std::filesystem::create_directories(std::filesystem::path(hdd)/"home/00000001");
 request();auto before=bytes;fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before);
 { std::ofstream f(std::filesystem::path(hdd)/"home/00000001/localusername",std::ios::binary);f<<"Ratchet"; }
 request();put(0x8400+96,6);before=bytes;fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before); // leaf type
 request();put(0x8400+100,7);before=bytes;fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before); // user 7 absent
 request();assert(service_sysutil_connect(s,0x86000400));
 const unsigned reply=0x8400+word(0x800c);assert(word(0x8014)==2 && word(0x8018)==2);
 assert(word(reply)==0x55535249 && word(reply+4)==0x110 && word(reply+8)==64);
 assert(text(reply,word(reply+64))=="getStat" && word(reply+68)==3);
 assert(text(reply,word(reply+92))=="elmResult" && word(reply+96)==1 && word(reply+100)==0);
 assert(text(reply,word(reply+108))=="elmSelectedUsrId" && word(reply+112)==1 && word(reply+116)==1);
 assert(text(reply,word(reply+124))=="elmSelectedUsrName" && word(reply+128)==3);
 assert(text(reply,word(reply+132))=="Ratchet" && word(reply+136)==7);
 std::filesystem::remove_all(hdd);
''')


if __name__ == '__main__':
    unittest.main()
