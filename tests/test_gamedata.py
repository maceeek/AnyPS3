"""Game-data transport lifetime and host-backed metadata."""
import shutil
import unittest
from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class GameDataTests(unittest.TestCase):
    def test_new_data_metadata_uses_host_mount_and_rejects_symlinks(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char parent[]="/tmp/anyps3-game-data-test-XXXXXX";assert(::mkdtemp(parent));
 s.fs.configure_hdd(parent);
 fault([&]{s.fs.game_data_stat("TEST00001");});
 s.fs.game_data.container=1;s.fs.game_data.no_calc=true;
 const auto data=s.fs.game_data_stat("TEST00001");
 auto word=[&](unsigned a){return(std::uint32_t(data[a])<<24)|(std::uint32_t(data[a+1])<<16)|(std::uint32_t(data[a+2])<<8)|data[a+3];};
 assert(data.size()==0x1450 && word(0)>0 && word(4)==1 && word(0x1404)==UINT32_MAX && word(0x1408)==0);
 assert(std::string(reinterpret_cast<const char*>(data.data()+8))=="/dev_hdd0/game/TEST00001");
 assert(std::string(reinterpret_cast<const char*>(data.data()+0x427))=="/dev_hdd0/game/TEST00001/USRDIR");
 assert(!std::filesystem::exists(std::filesystem::path(parent)/"game"));
 fault([&]{s.fs.game_data_stat("../escape");});
 std::filesystem::create_directory(std::filesystem::path(parent)/"other");
 std::filesystem::create_directory_symlink(std::filesystem::path(parent)/"other",std::filesystem::path(parent)/"game");
 fault([&]{s.fs.game_data_stat("TEST00001");});
 std::filesystem::remove_all(parent);
''')

    def test_session_charges_container_and_releases_it_on_end(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 const unsigned base=0x8000,data=base+1024;
 auto request=[&](bool end,unsigned container=0x3f000000){
   std::fill(bytes.begin()+data,bytes.end(),0);put(base+12,0);put(base+16,end?112:176);put(base+20,2);put(base+24,1);
   put(data,0x47444154);put(data+4,0x110);put(data+8,64);put(data+12,end?28:60);put(data+68,end?0:2);
   put(data+24,end?96:128);put(data+28,end?12:43);
   put(data+48,end?112:176);for(unsigned at=72;at<92;at+=4)put(data+at,UINT32_MAX);
   if(end){std::copy_n("endGameData",12,bytes.begin()+data+96);return;}
   put(data+92,13);put(data+96,1);put(data+100,container);put(data+108,29);put(data+112,1);put(data+116,1);
   const char names[]="initGameData\0elmMemContainer\0elmNoCalcFlag\0";
   std::copy_n(names,43,bytes.begin()+data+128);
 };
 request(false);auto before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before && !s.fs.game_data.container);
 s.fs.hdd_root="/tmp";
 assert(service_sysutil_connect(s,0x86000400));
 assert(word(data+word(base+12)+100)==0x8002b604 && !s.fs.game_data.container);
 assert(call(324,0x10000,0x300000)==0);
 request(false);assert(service_sysutil_connect(s,0x86000400));
 const auto reply=data+word(base+12);
 assert(word(reply+100)==0 && word(reply+68)==1);
 assert(std::string(reinterpret_cast<const char*>(bytes.data()+reply+word(reply+24)))=="elmMemContainer");
 assert(s.fs.game_data.container==0x3f000000 && s.fs.game_data.no_calc);
 assert(s.fs.game_data.workspace.size()==0x10000 && s.mmapper.containers[0].used==0x10000);
 assert(call(325,0x3f000000)==MM_EBUSY);
 request(false);before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before && s.mmapper.containers[0].used==0x10000);
 request(true);assert(service_sysutil_connect(s,0x86000400));
 assert(s.fs.game_data.workspace.empty() && !s.fs.game_data.container && s.mmapper.containers[0].used==0);
 assert(call(325,0x3f000000)==0);
 request(true);before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before);
''')
