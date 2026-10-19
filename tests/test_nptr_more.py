"""NPTR 3.01 queries, captured run 279 tree, and persistent trophy state.

Expected field paths/types are taken from the original firmware parsers
0x20220760, 0x20220cbc, 0x2022063c, 0x20220a04 and 0x202211d8.
Fixtures and wire decoding are independent of native_trophy.encode/Request.
"""
from pathlib import Path
import re
import shutil
import unittest
from tests import test_runtime_objects

# Exact 304-byte request logged at the build 279 stop (context=handle=1).
GAME_INFO = bytes.fromhex('''
4e505452 00000110 00000040 000000b4 00000100 00000000 00000100 0000002b
00000130 00000000 00000130 00000000 00000130 00000000 00000000 00000000
00000000 00000006 ffffffff ffffffff ffffffff 0000007c 00000098 00000002
00000001 00000001 00000000 0000000a 00000001 00000001 00000000 ffffffff
00000000 00000000 00000000 ffffffff 00000000 00000000 00000000 ffffffff
00000000 00000000 00000000 ffffffff 00000000 00000000 00000000 00000016
00000000 00000000 ffffffff 00000098 ffffffff ffffffff 0000001f 00000000
00000000 0000007c ffffffff ffffffff ffffffff 00000000 00000000 00000000
3200636f 6e746578 74006d65 6d5f636f 6e746578 74006761 6d654461 74610067
616d6544 65746169 6c730000 00000000
''')

SETUP = r'''
 (void)call;char root[]="/tmp/anyps3-nptr-more-XXXXXX";assert(::mkdtemp(root));
 const std::filesystem::path tmp(root),dir=tmp/"home/00000001/trophy/TEST00001_00";
 std::filesystem::create_directories(dir);s.fs.hdd_root=tmp;
 const char* conf=R"XML(<trophyconf version="1.0"><npcommid>TEST00001_00</npcommid>
 <trophy id="000" hidden="no" ttype="P" pid="-1"/>
 <trophy id="001" hidden="yes" ttype="B" pid="000"/>
 <trophy id="002" hidden="no" ttype="S" pid="000"/>
 <trophy id="003" hidden="no" ttype="G" pid="000"/>
 <trophy id="004" hidden="no" ttype="B" pid="000"/></trophyconf>)XML";
 const char* labels=R"XML(<?xml version="1.0" encoding="UTF-8"?>
 <!-- metadata fixture, not game data --><trophyconf version="1.0"><npcommid>TEST00001_00</npcommid>
 <title-name>Test &amp; &#x17D;lu&#357;&#253;</title-name><title-detail>Disk &lt;truth&gt;</title-detail>
 <trophy id="000" hidden="no" ttype="P"><name>All</name><detail>Collect all.</detail></trophy>
 <trophy id="001" hidden="yes" ttype="B"><name>Hidden</name><detail>Secret.</detail></trophy>
 <trophy id="002" hidden="no" ttype="S"><name>Silver &amp; Ice</name><detail>Win &quot;this&quot;.</detail></trophy>
 <trophy id="003" hidden="no" ttype="G"><name>Gold</name><detail>Gold detail.</detail></trophy>
 <trophy id="004" hidden="no" ttype="B"><name>Bronze</name><detail>Bronze detail.</detail></trophy>
 </trophyconf>)XML";
 std::ofstream(dir/"TROPCONF.SFM")<<conf;std::ofstream(dir/"TROP_01.SFM")<<labels;
 s.trophy.contexts[0]="TEST00001_00";s.trophy.handles[0]=true;s.trophy.registered[0]=true;
 s.sysutil_connections.insert(UINT64_C(1)<<32);s.sysutil_connections.insert(2);
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-8*j);};
 const unsigned capacity=0x7c00;unsigned active=0x8000;
 auto load=[&](const std::vector<std::uint8_t>& b,unsigned channel=1,unsigned start=0){
   active=channel*0x8000;put(active+12,start);put(active+16,b.size());put(active+20,2);put(active+24,1);
   for(unsigned i=0;i<b.size();++i)bytes[active+0x400+(start+i)%capacity]=b[i];
 };
 // Independent request encoder: root at 64, strings at 0x200, blob at 0x300.
 auto request=[&](const char* command,unsigned context=1,unsigned handle=1,unsigned id=0,unsigned channel=1,
                  bool details=true,bool data=true){
   std::vector<std::uint8_t> b(0x310);auto set=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)b[a+j]=v>>(24-8*j);};
   unsigned names=0;auto name=[&](const char* p){const unsigned a=names;do{b[0x200+names++]=*p;}while(*p++);return a;};
   const std::string cmd(command);const bool abort=cmd=="abort";const unsigned leaves=abort?1:cmd=="3"?4:cmd=="7"?3:2;
   set(0,0x4e505452);set(4,0x110);set(8,64);set(16,0x200);set(24,0x200);set(32,0x300);set(40,0x300);set(48,0x300);
   set(64,name(command));set(68,leaves);for(unsigned a=72;a<92;a+=4)set(a,UINT32_MAX);
   auto leaf=[&](unsigned i,const char* n,unsigned ty,unsigned a,unsigned z=0){const auto at=92+16*i;set(at,name(n));set(at+4,ty);set(at+8,a);set(at+12,z);};
   unsigned n=0;if(!abort)leaf(n++,"context",1,context);leaf(n++,"mem_context",1,handle);
   if(cmd=="7")leaf(n++,"flag_id",1,id);
   if(cmd=="3"){leaf(n++,"flag_array",6,0,16);leaf(n++,"count",1,1);set(0x300+(id/32)*4,1u<<(id%32));set(52,16);}
   unsigned next=28+16*leaves,prev=UINT32_MAX;
   if(cmd=="2" || cmd=="3")for(unsigned i=0;i<2;++i){if((i==0 && !data) || (i==1 && !details))continue;
     const auto a=64+next;set(a,name(cmd=="2"?(i?"gameDetails":"gameData"):(i?"flagDetails":"flagData")));
     set(a+8,0);set(a+12,prev);set(a+16,UINT32_MAX);set(a+20,UINT32_MAX);set(a+24,UINT32_MAX);
     if(prev==UINT32_MAX)set(84,next);else set(64+prev+16,next);set(88,next);prev=next;next+=28;}
   set(12,next);set(28,names);load(b,channel);
 };
 auto service=[&]{assert(service_sysutil_connect(s,active?0x86000400:0x86000000));};
 // Decode reply independently; no native_trophy helpers or algorithms used.
 auto reply=[&]{std::vector<std::uint8_t> b(word(active+16));const auto start=word(active+12);
   for(unsigned i=0;i<b.size();++i)b[i]=bytes[active+0x400+(start+i)%capacity];
   return b;};
 auto u32=[](const std::vector<std::uint8_t>& b,unsigned a){assert(a+4<=b.size());
   return(std::uint32_t(b[a])<<24)|(std::uint32_t(b[a+1])<<16)|(std::uint32_t(b[a+2])<<8)|b[a+3];};
 auto name_of=[&](const std::vector<std::uint8_t>& b,unsigned offset){const unsigned at=u32(b,24)+offset;
   assert(at<b.size());return std::string(reinterpret_cast<const char*>(b.data()+at));};
 auto find=[&](const std::vector<std::uint8_t>& b,unsigned node,const char* name,unsigned type){
   const unsigned at=64+node;for(unsigned i=0;i<u32(b,at+4);++i){const auto e=at+28+16*i;
     if(name_of(b,u32(b,e))==name){assert(u32(b,e+4)==type);return e;}}assert(false);return 0u;};
 auto child=[&](const std::vector<std::uint8_t>& b,const char* name){
   unsigned prev=UINT32_MAX;for(unsigned n=u32(b,84);n!=UINT32_MAX;n=u32(b,64+n+16)){
     assert(u32(b,64+n+8)==0 && u32(b,64+n+12)==prev);prev=n;
     if(name_of(b,u32(b,64+n))==name)return n;}
   assert(false);return 0u;};
 auto val=[&](const std::vector<std::uint8_t>& b,unsigned n,const char* key){return u32(b,find(b,n,key,1)+8);};
 auto text=[&](const std::vector<std::uint8_t>& b,unsigned n,const char* key){auto e=find(b,n,key,3);auto t=name_of(b,u32(b,e+8));assert(t.size()==u32(b,e+12));return t;};
 auto result=[&]{const auto b=reply();return val(b,0,"result");};
 auto disk=[&]{std::ifstream f(dir/"TROPUSR.DAT",std::ios::binary);return std::vector<std::uint8_t>(std::istreambuf_iterator<char>(f),{});};
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class NptrMoreTests(unittest.TestCase):
    def run_native(self, body):
        test_runtime_objects.RuntimeObjectsTests().run_native(SETUP + body + r'''
 (void)request;(void)service;(void)reply;(void)child;(void)text;(void)val;(void)result;(void)disk;
 std::filesystem::remove_all(tmp);
''')

    def test_captured_game_info_and_persisted_unlock_with_restart(self):
        capture = ','.join(str(b) for b in GAME_INFO)
        self.run_native(r'''
 const std::vector<std::uint8_t> captured={''' + capture + r'''};
 load(captured,1,capacity-100);service();auto b=reply();assert(result()==0 && word(active+24)==2);
 assert(name_of(b,u32(b,64))=="2");auto d=child(b,"gameDetails"),g=child(b,"gameData");
 assert(val(b,d,"count")==5 && val(b,d,"platinum")==1 && val(b,d,"gold")==1 && val(b,d,"silver")==1 && val(b,d,"bronze")==2);
 assert(text(b,d,"game_title")=="Test & Žluťý" && text(b,d,"game_description")=="Disk <truth>");
 for(const auto* key:{"count","platinum","gold","silver","bronze"})assert(val(b,g,key)==0);
 assert(!std::filesystem::exists(dir/"TROPUSR.DAT"));
 request("3",1,1,2);service();b=reply();d=child(b,"flagDetails");g=child(b,"flagData");
 assert(val(b,0,"count")==1 && val(b,d,"flag_id")==2 && val(b,d,"flag_type")==3 && val(b,d,"hidden")==0);
 assert(text(b,d,"name")=="Silver & Ice" && text(b,d,"description")=="Win \"this\".");
 assert(val(b,g,"flag_id")==2 && val(b,g,"unlocked")==0);auto e=find(b,g,"timestamp",6);assert(u32(b,e+12)==8);
 assert(u32(b,u32(b,48)+u32(b,e+8))==0 && u32(b,u32(b,48)+u32(b,e+8)+4)==0);
 request("3",1,1,1);service();assert(result()==0x80022913); // Hidden while locked.
 request("3",1,1,127);service();assert(result()==0x80022920);
 request("7",1,1,0);service();assert(result()==0x80022914); // Cannot unlock platinum directly.
 s.trophy.read_only[0]=true;request("7",1,1,2);service();assert(result()==0x8002290e);s.trophy.read_only[0]=false;
 request("7",1,1,2);service();b=reply();assert(result()==0 && val(b,child(b,"platinumId"),"flag_id")==UINT32_MAX);
 auto saved=disk();assert(saved.size()==1152); // 0x70 + 5 * (0x60 + 0x70).
 assert(u32(saved,0)==0x818f54ad && u32(saved,4)==0x10000 && u32(saved,8)==2);
 assert(u32(saved,0x30)==4 && u32(saved,0x34)==0x50 && u32(saved,0x3c)==5 && u32(saved,0x44)==0x70);
 assert(u32(saved,0x50)==6 && u32(saved,0x54)==0x60 && u32(saved,0x5c)==5 && u32(saved,0x64)==0x250);
 assert(u32(saved,0x330)==6 && u32(saved,0x338)==2 && u32(saved,0x340)==2 && u32(saved,0x344)==1);
 assert(u32(saved,0x350)!=0 && u32(saved,0x350)==u32(saved,0x358) && u32(saved,0x354)==u32(saved,0x35c));
 // Recreate runtime object: state is loaded from disk, not a shadow bool array.
 s.trophy=NativeTrophy{};s.trophy.contexts[0]="TEST00001_00";s.trophy.handles[0]=true;s.trophy.registered[0]=true;
 request("m");service();b=reply();assert(result()==0 && val(b,0,"count")==5);e=find(b,0,"flag_array",6);
 assert(u32(b,e+12)==16 && u32(b,u32(b,48)+u32(b,e+8))==4 && u32(b,u32(b,48)+u32(b,e+8)+4)==0);
 request("q");service();assert(val(reply(),0,"percent")==20);
 request("7",1,1,2);service();assert(result()==0x80022915 && disk()==saved);
 load(captured);service();b=reply();g=child(b,"gameData");assert(val(b,g,"count")==1 && val(b,g,"silver")==1 && val(b,g,"bronze")==0);
 request("3",1,1,2);service();b=reply();g=child(b,"flagData");assert(val(b,g,"unlocked")==1);e=find(b,g,"timestamp",6);
 assert(u32(b,u32(b,48)+u32(b,e+8))==u32(saved,0x358));
 request("7",1,1,1);service();assert(result()==0);
 request("q");service();assert(val(reply(),0,"percent")==30); // 45 / 150 points, not 2 / 5 trophies.
 for(auto id:{3,4}){request("7",1,1,id);service();assert(result()==0);}
 b=reply();assert(val(b,child(b,"platinumId"),"flag_id")==0);
 request("q");service();assert(val(reply(),0,"percent")==100);
 request("m");service();b=reply();e=find(b,0,"flag_array",6);assert(u32(b,u32(b,48)+u32(b,e+8))==31);
 request("3",1,1,1);service();b=reply();assert(result()==0 && val(b,child(b,"flagDetails"),"hidden")==1);
''')

    def test_abort_errors_malformed_tree_and_storage_do_not_change_ring(self):
        self.run_native(r'''
 request("2",0);service();assert(result()==0x8002290a);
 request("2",1,4);service();assert(result()==0x80022911);
 s.trophy.registered[0]=false;request("2");service();assert(result()==0x80022904);s.trophy.registered[0]=true;
 request("abort",1,1,0,1);auto before=bytes;fault([&]{service();});assert(bytes==before && !s.trophy.aborted[0]);
 request("abort",1,4,0,0);service();assert(result()==0x80022911);
 request("abort",1,1,0,0);service();assert(result()==0 && s.trophy.handles[0] && s.trophy.aborted[0]);
 request("2");service();assert(result()==0x80022910);
 request("7",1,1,2);service();assert(result()==0x80022910 && !std::filesystem::exists(dir/"TROPUSR.DAT"));
 s.trophy.aborted[0]=false;
 request("2");put(active+0x400+84,UINT32_MAX);before=bytes;fault([&]{service();});assert(bytes==before);
 request("3",1,1,2);put(active+0x400+96,6);before=bytes;fault([&]{service();});assert(bytes==before);
 request("3",1,1,2);put(active+0x400+148,129);before=bytes;fault([&]{service();});assert(bytes==before);
 request("3",1,1,2);put(active+0x400+148,2);before=bytes;fault([&]{service();});assert(bytes==before);
 request("2");put(active+0x400+24,UINT32_MAX);before=bytes;fault([&]{service();});assert(bytes==before);
 request("2");put(active+0x400+4,0x111);before=bytes;fault([&]{service();});assert(bytes==before);
 // Localized fallback is actual TROP.SFM; no invented title if neither exists.
 std::filesystem::rename(dir/"TROP_01.SFM",dir/"TROP.SFM");request("2");service();assert(result()==0);
 std::filesystem::remove(dir/"TROP.SFM");request("2");before=bytes;fault([&]{service();});assert(bytes==before);
 std::ofstream(dir/"TROP_01.SFM")<<labels;
 std::ofstream(dir/"TROPCONF.SFM")<<"<trophyconf>";request("2");before=bytes;fault([&]{service();});assert(bytes==before);
 std::ofstream(dir/"TROPCONF.SFM")<<conf;
 // Unknown/corrupt stored state is not converted to all-locked success.
 std::ofstream(dir/"TROPUSR.DAT")<<"existing-unknown-format";auto saved=disk();request("7",1,1,2);before=bytes;
 fault([&]{service();});assert(bytes==before && disk()==saved);
 std::filesystem::remove(dir/"TROPUSR.DAT");request("7",1,1,2);service();saved=disk();
 fault([&]{native_trophy::save(dir/"nonexistent",saved);});assert(disk()==saved);
 // The table size is fixed by the on-disk reference, not inferred from a loop.
 for(int i=0;i<5;++i)assert(u32(saved,0x250+0x70*i+16)==unsigned(i));
 auto damaged=saved;damaged[0x54+3]=0x61;std::ofstream(dir/"TROPUSR.DAT",std::ios::binary|std::ios::trunc).write(reinterpret_cast<const char*>(damaged.data()),damaged.size());
 request("m");before=bytes;fault([&]{service();});assert(bytes==before && disk()==damaged);
''')

    def test_game_fixture_counts_and_text_when_local_disc_available(self):
        installed = Path('local/ratchet/hdd0/home/00000001/trophy/NPWR00941_00')
        if not (installed / 'TROP_01.SFM').is_file():
            self.skipTest('Local ACIT metadata unavailable')
        self.run_native(r'''
 for(const auto* file:{"TROPCONF.SFM","TROP_01.SFM"})std::filesystem::copy_file(
   std::filesystem::path("local/ratchet/hdd0/home/00000001/trophy/NPWR00941_00")/file,dir/file,std::filesystem::copy_options::overwrite_existing);
 auto path=dir.parent_path()/"NPWR00941_00";std::filesystem::rename(dir,path);s.trophy.contexts[0]="NPWR00941_00";
 request("2");service();auto b=reply();auto d=child(b,"gameDetails"),g=child(b,"gameData");
 assert(val(b,d,"count")==37 && val(b,d,"platinum")==1 && val(b,d,"gold")==5 && val(b,d,"silver")==6 && val(b,d,"bronze")==25);
 assert(text(b,d,"game_title")=="Ratchet & Clank: A Crack in Time" && val(b,g,"count")==0);
 request("3",1,1,0);service();b=reply();assert(text(b,child(b,"flagDetails"),"name")=="The Lombax Triumphant");
 assert(!std::filesystem::exists(path/"TROPUSR.DAT"));
 request("7",1,1,32);service();assert(result()==0);
 request("m");service();b=reply();auto e=find(b,0,"flag_array",6);
 assert(u32(b,u32(b,48)+u32(b,e+8))==0 && u32(b,u32(b,48)+u32(b,e+8)+4)==1);
 request("3",1,1,32);service();assert(result()==0);
 request("q");service();assert(val(reply(),0,"percent")==8); // 90 / 1005 points, floor.
''')

    def test_capture_matches_retained_trace(self):
        trace = Path('local/ratchet/codex/nptr/trace-279.txt')
        if not trace.is_file():
            self.skipTest('Local captured trace unavailable')
        words = re.search(r'NPTR request words: ([0-9a-f ]+)', trace.read_text()).group(1)
        self.assertEqual(GAME_INFO, bytes.fromhex(words))
