"""Native NPTR trophy transport requests."""
import shutil
import unittest
from tests import test_runtime_objects

# Tree header and nodes of the sceNpTrophyCreateContext request captured from
# the original BCES00511 TrophyInitThread (native-bundle-199 stderr).
CREATE_TREE = (
    '4e505452000001100000004000000088000000d000000000000000d00000003f'
    '0000011000000000000001100000000000000110000000d00000000000000000'
    '0000000000000003ffffffffffffffffffffffff0000004c0000004c0000002a'
    '0000000600000020000000a00000003700000006000000c000000008ffffffff'
    '000000000000000000000000000000020000000200000000ffffffffffffffff'
    'ffffffffffffffff0000000b0000000600000000000000090000001b00000006'
    '00000010000000010000000000000000')


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class TrophyTests(unittest.TestCase):
    def test_create_context_validates_signature_conf_and_context_limit(self):
        tree = ','.join(f'0x{b:02x}' for b in bytes.fromhex(CREATE_TREE))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char root[]="/tmp/anyps3-trophy-test-XXXXXX";assert(::mkdtemp(root));
 const std::filesystem::path disc(root);
 std::filesystem::create_directories(disc/"PS3_GAME/TROPDIR/NPWR00941_00");
 std::ofstream(disc/"PS3_GAME/TROPDIR/NPWR00941_00/TROPHY.TRP")<<"x";
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 const unsigned base=0x8000,data=base+1024;
 const std::uint8_t tree[]={''' + tree + r'''};
 auto request=[&](const char* id,unsigned num,std::uint32_t magic,std::uint8_t option){
   std::fill(bytes.begin()+data,bytes.end(),0);put(base+12,0);put(base+16,480);put(base+20,2);put(base+24,1);
   std::copy_n(tree,sizeof tree,bytes.begin()+data);
   const char names[]="f\0npCommId\0np_comm_id_data\0np_comm_id_num\0np_comm_sign\0options\0";
   std::copy_n(names,63,bytes.begin()+data+0xd0);
   std::copy_n(id,9,bytes.begin()+data+0x110);bytes[data+0x120]=num;
   put(data+0x130,magic);bytes[data+0x134]=1;bytes[data+0x1d7]=option;
 };
 auto reply=[&](const char* name){
   const auto at=data+word(base+12);
   assert(word(at)==0x4e505452 && word(at+68)==2);
   assert(std::string(reinterpret_cast<const char*>(bytes.data()+at+word(at+24)))=="f");
   for(unsigned i=0;i<2;++i)if(std::string(reinterpret_cast<const char*>(bytes.data()+at+word(at+24)+word(at+92+16*i)))==name){
     assert(word(at+96+16*i)==1);return word(at+100+16*i);}
   assert(false);return 0u;
 };
 request("NPWR00941",0,0xb9dde13b,0);
 fault([&]{service_sysutil_connect(s,0x86000400);}); // No client connection.
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 fault([&]{service_sysutil_connect(s,0x86000400);}); // No disc mount.
 s.fs.disc=disc;
 for(unsigned expected=1;expected<=4;++expected){
   request("NPWR00941",0,0xb9dde13b,expected==4);assert(service_sysutil_connect(s,0x86000400));
   assert(reply("result")==0 && reply("context")==expected && s.trophy.contexts[expected-1]=="NPWR00941_00");
 }
 assert(s.trophy.read_only[3] && !s.trophy.read_only[0]);
 request("NPWR00941",0,0xb9dde13b,0);assert(service_sysutil_connect(s,0x86000400));
 assert(reply("result")==0x80022907 && reply("context")==0);
 s.trophy=NativeTrophy{};
 request("NPWR00941",0,0xb9dde13a,0);assert(service_sysutil_connect(s,0x86000400));
 assert(reply("result")==0x80022918 && reply("context")==0);
 request("NPWR00941",1,0xb9dde13b,0);assert(service_sysutil_connect(s,0x86000400));
 assert(reply("result")==0x8002291b);
 request("NPWR00941",0,0xb9dde13b,2);assert(service_sysutil_connect(s,0x86000400));
 assert(reply("result")==0x80022903 && s.trophy.contexts[0].empty());
 request("NPWR00941",0,0xb9dde13b,0);put(data+0x5c+4,1);auto before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before); // Wrong leaf type.
 std::filesystem::remove_all(disc);
''')

    def test_handle_space_status_install_and_progress_events(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char root[]="/tmp/anyps3-trophy-reg-XXXXXX";assert(::mkdtemp(root));
 const std::filesystem::path base_dir(root),disc=base_dir/"disc",hdd=base_dir/"hdd";
 std::filesystem::create_directories(disc/"PS3_GAME/TROPDIR/NPWR00941_00");std::filesystem::create_directories(hdd);
 { // TRP v1: header, two entries, payloads "CONF" and "PNG!".
   std::vector<std::uint8_t> trp(0x40+2*0x40+8,0);
   auto be=[&](unsigned a,std::uint64_t v,unsigned n){for(unsigned i=0;i<n;++i)trp[a+i]=v>>(8*(n-1-i));};
   be(0,0xdca24d00,4);be(4,1,4);be(8,trp.size(),8);be(16,2,4);be(20,0x40,4);
   std::copy_n("TROPCONF.SFM",12,trp.begin()+0x40);be(0x60,0xc0,8);be(0x68,4,8);
   std::copy_n("TROP000.PNG",11,trp.begin()+0x80);be(0xa0,0xc4,8);be(0xa8,4,8);
   std::copy_n("CONFPNG!",8,trp.begin()+0xc0);
   std::ofstream(disc/"PS3_GAME/TROPDIR/NPWR00941_00/TROPHY.TRP",std::ios::binary).write(reinterpret_cast<const char*>(trp.data()),trp.size());
 }
 s.fs.disc=disc;s.fs.hdd_root=hdd;
 s.sysutil_connections.insert(UINT64_C(1)<<32);s.sysutil_connections.insert(2);
 s.events.queues.push_back({0x8d000000,3,UINT64_C(0x8006010000000102),{}});
 s.trophy.contexts[0]="NPWR00941_00";
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 const unsigned base=0x8000,data=base+1024;
 // Request: root struct (7 words), leaf slots; strings at 0x100, binary at 0x180.
 auto request=[&](const char* command,std::uint32_t context,std::uint32_t handle,bool options){
   std::fill(bytes.begin()+data,bytes.end(),0);
   const unsigned leaves=context||handle?(options?3:2):1;
   put(data,0x4e505452);put(data+4,0x110);put(data+8,64);put(data+12,28+16*leaves);
   put(data+16,0x100);put(data+24,0x100);put(data+28,40);put(data+32,0x180);put(data+40,0x180);put(data+48,0x180);put(data+52,16);
   put(data+64,0);put(data+68,leaves);for(unsigned a=72;a<92;a+=4)put(data+a,UINT32_MAX);
   const char names[]="?\0context\0mem_context\0options\0";std::copy_n(names,30,bytes.begin()+data+0x100);bytes[data+0x100]=command[0];
   if(leaves==1){put(data+92,UINT32_MAX);}
   else{put(data+92,2);put(data+96,1);put(data+100,context);put(data+108,10);put(data+112,1);put(data+116,handle);
        if(options){put(data+124,22);put(data+128,6);put(data+132,0);put(data+136,8);}}
   put(base+12,0);put(base+16,0x190);put(base+20,2);put(base+24,1);
 };
 auto reply_at=[&]{return data+word(base+12);};
 auto leaf=[&](const char* name)->std::pair<std::uint32_t,std::uint32_t>{
   const auto at=reply_at();assert(word(at)==0x4e505452);
   for(unsigned i=0;i<word(at+68);++i){const auto e=at+92+16*i;
     if(std::string(reinterpret_cast<const char*>(bytes.data()+at+word(at+24)+word(e)))==name)return{word(e+4),word(e+8)};}
   assert(false);return{0,0};
 };
 request("8",0,0,false);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0 && leaf("mem_context").second==1);
 request("o",1,2,false);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0x80022911); // Handle 2 was never created.
 request("o",1,1,false);assert(service_sysutil_connect(s,0x86000400));
 { const auto at=reply_at();const auto v=leaf("req_space");assert(v.first==6 && word(at+92+16+12)==8);
   assert(word(at+word(at+48)+v.second)==0 && word(at+word(at+48)+v.second+4)==8); } // 0xc8-0x40-2*0x40.
 request("h",1,1,true);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0 && leaf("status").second==1);
 request("i",1,1,true);assert(service_sysutil_connect(s,0x86000400));
 { const auto at=reply_at();assert(leaf("result").second==0 && word(at+84)==44 && word(at+88)==44);
   assert(std::string(reinterpret_cast<const char*>(bytes.data()+at+word(at+24)+word(at+64+44)))=="platinumId");
   assert(word(at+64+44+8)==0 && word(at+64+44+20)==UINT32_MAX); }
 const auto installed=hdd/"home/00000001/trophy/NPWR00941_00";
 { std::ifstream f(installed/"TROP000.PNG");std::string v;f>>v;assert(v=="PNG!"); }
 // Four events (setup, progress 1/1, finalize, complete); capacity three.
 auto& q=s.events.queues[0];
 assert(q.data.size()==3 && s.events.deferred.size()==1);
 assert(q.data[0][1]==299 && q.data[0][2]==((UINT64_C(1)<<32)|5) && q.data[1][2]==((UINT64_C(1)<<32)|6) && q.data[1][3]==((UINT64_C(1)<<32)|1));
 r[11]=131;r[3]=0x8d000000;r[4]=0x10100;r[5]=3;r[6]=0x10200;syscall(s);
 assert(r[3]==0 && mem.load(0x10200,4)==3 && mem.load(0x10100+8,8)==299 && mem.load(0x10100+64+16,8)==((UINT64_C(1)<<32)|7));
 r[11]=131;r[3]=0x8d000000;r[4]=0x10100;r[5]=3;r[6]=0x10200;syscall(s);
 assert(mem.load(0x10200,4)==1 && mem.load(0x10100+16,8)==((UINT64_C(1)<<32)|8) && s.events.deferred.empty());
 request("h",1,1,true);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("status").second==3);
 request("o",1,1,false);assert(service_sysutil_connect(s,0x86000400));
 { const auto at=reply_at();const auto v=leaf("req_space");assert(word(at+word(at+48)+v.second+4)==0); }
 request("h",1,1,true);bytes[data+0x180+7]=1;auto before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before); // Nonzero options are not ignored.
 request("1",1,1,false);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0 && s.trophy.contexts[0].empty() && !s.trophy.registered[0]);
 request("1",1,1,false);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0x8002290a);
 request("9",1,1,false);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0 && !s.trophy.handles[0]);
 request("9",1,1,false);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0x80022911);
 request("8",0,0,false);assert(service_sysutil_connect(s,0x86000400));assert(s.trophy.handles[0]);
 s.trophy.contexts[2]="X_00";
 request("term",0,0,false);bytes[data+0x100]='t';std::copy_n("term",5,bytes.begin()+data+0x100);
 put(data+64,0);assert(service_sysutil_connect(s,0x86000400));
 assert(leaf("result").second==0 && !s.trophy.handles[0] && s.trophy.contexts[2].empty());
 std::filesystem::remove_all(base_dir);
''')
