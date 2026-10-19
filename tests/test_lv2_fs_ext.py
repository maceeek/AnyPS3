"""Native LV2 FS regressions with fixed ABI expectations and temporary mounts."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.runtime import MEMORY_CPP
from anyps3.native_fs import TYPES as FS_TYPES, CPP as FS_CPP
from anyps3.lv2_fs_ext import TYPES as EXT_TYPES, CPP as EXT_CPP


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Lv2FsExtTests(unittest.TestCase):
    def run_fs(self, body):
        with tempfile.TemporaryDirectory(prefix='anyps3-fs-ext-') as root:
            for mount in ('hdd', 'cache', 'disc'):
                (Path(root) / mount).mkdir()
            setup = r'''
 const std::filesystem::path root=ROOT;
 s.fs.hdd_root=root/"hdd";s.fs.cache=root/"cache";s.fs.disc=root/"disc";
 auto path=[&](const char* name,unsigned addr=0x10000){
   std::copy_n(name,std::string(name).size()+1,mem.access(addr,std::string(name).size()+1,true));return addr;
 };
 auto call6=[&](unsigned nr,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,std::uint64_t d=0,std::uint64_t e=0,std::uint64_t f=0){
   r[7]=e;r[8]=f;return call(nr,a,b,c,d);
 };
 (void)path;(void)call6;
'''.replace('ROOT', json.dumps(root))
            source = ('#include <cstdint>\n#include <cinttypes>\n#include <array>\n#include <set>\n'
                      '#include <climits>\n#include <cassert>\n' + MEMORY_CPP + FS_TYPES + EXT_TYPES + r'''
struct PpuState {std::array<std::uint64_t,32> r{};GuestMemory memory;NativeFilesystem fs;Lv2FsExt fs_ext;};
''' + FS_CPP + EXT_CPP + r'''
template<class F> void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){
 (void)crc32;PpuState s;auto& r=s.r;auto& mem=s.memory;mem.map(0x10000,0x2000,6,nullptr,0);
 auto call=[&](unsigned number,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,std::uint64_t d=0){
   r[11]=number;r[3]=a;r[4]=b;r[5]=c;r[6]=d;
   switch(number){
   case 801:case 802:case 803:case 804:case 808:case 809:case 817:case 818:case 819:case 820:case 822:case 840:
     r[3]=native_fs_syscall(s);break;
   default:if(!syscall_fs_ext(s))throw std::runtime_error("unhandled test syscall");
   }
   return r[3];
 };
''' + setup + body + '\n}\n')
            cpp = Path(root) / 'test.cpp'
            binary = Path(root) / 'test'
            cpp.write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    str(cpp), '-o', str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_directory_abi_eof_snapshot_and_shared_descriptor_namespace(self):
        self.run_fs(r'''
 {std::ofstream out(root/"hdd/z");out<<"z";}
 std::filesystem::create_directory(root/"hdd/a");
 assert(call(805,path("/dev_hdd0"),0x10400)==0);const auto dir=mem.load(0x10400,4);assert(dir==3);
 {std::ofstream out(root/"hdd/after-open");out<<"snapshot excludes later entries";}
 assert(call6(801,path("/dev_hdd0/z"),0,0x10400)==0);const auto file=mem.load(0x10400,4);assert(file==4);
 assert(call(807,file)==0x8001002a && call(804,dir)==0x8001002a);
 assert(call(806,dir,0,0x10508)==0x8001000d);
 fault([&]{call(806,dir,0x11fff,0x10508);});
 const char* names[]={".","..","a","z"};const unsigned types[]={1,1,1,2};
 for(unsigned i=0;i<4;++i){
   std::fill_n(mem.access(0x10600,259,true),259,0xcc);
   assert(call(806,dir,0x10600,0x10508)==0 && mem.load(0x10508,8)==258);
   assert(mem.load(0x10600,1)==types[i] && mem.load(0x10601,1)==std::string(names[i]).size());
   assert(std::string(reinterpret_cast<char*>(mem.access(0x10602,256,false)))==names[i]);
   assert(mem.load(0x10701,1)==0 && mem.load(0x10702,1)==0xcc);
 }
 assert(mem.load(0x10508,4)==0 && mem.load(0x1050c,4)==258);
 assert(call(806,dir,0x10600,0x10508)==0 && mem.load(0x10508,8)==0);
 assert(mem.load(0x10600,3)==0);
 assert(call(807,dir)==0 && call(807,dir)==0x8001002a);
 // Allocation works in both directions and closing reuses a common slot.
 assert(call(805,path("/dev_hdd1"),0x10400)==0 && mem.load(0x10400,4)==3);
 assert(call(804,file)==0);
 assert(call6(801,path("/dev_hdd0/z"),0,0x10400)==0 && mem.load(0x10400,4)==4);
 assert(call(806,3,0x10600,0x10508)==0);
 // nread aliases name bytes: final BE count must win over the dirent copy.
 assert(call(806,3,0x10600,0x10602)==0 && mem.load(0x10600,2)==0x0102 && mem.load(0x10602,8)==258);
 assert(call(806,3,0x10600,0x10508)==0 && mem.load(0x10508,8)==0);
 assert(mem.load(0x10600,4)==0x01022e2e); // cache EOF retains '..'
 assert(call(807,3)==0 && call(804,4)==0);
''')

    def test_mutations_utime_truncate_and_live_fd_after_rename_unlink(self):
        self.run_fs(r'''
 assert(call(811,path("/dev_hdd0/new"),0700)==0);
 assert(std::filesystem::is_directory(root/"hdd/new"));
 assert(call(811,0x10000,0700)==0x80010014);
 assert(call6(801,path("/dev_hdd0/new/data"),0100|2,0x10400)==0);const auto fd=mem.load(0x10400,4);
 std::copy_n("abcdef",6,mem.access(0x10500,6,true));assert(call(803,fd,0x10500,6,0x10408)==0);
 assert(call(831,0x10000,3)==0 && std::filesystem::file_size(root/"hdd/new/data")==3);
 assert(call(832,fd,8)==0 && std::filesystem::file_size(root/"hdd/new/data")==8);
 assert(call(818,fd,0,0,0x10408)==0 && call(802,fd,0x10500,8,0x10408)==0);
 assert(mem.load(0x10500,8)==UINT64_C(0x6162630000000000));
 assert(call(831,path("/dev_hdd0/absent"),3)==0x80010006);
 assert(call(831,path("/dev_hdd0/new/data"),UINT64_MAX)==0x80010002);
 assert(call(832,fd,UINT64_MAX)==0x80010002 && std::filesystem::file_size(root/"hdd/new/data")==8);
 mem.store(0x10500,8,0x01020304);mem.store(0x10508,8,0x10203040);
 assert(call(815,path("/dev_hdd0/new/data"),0x10500)==0);
 struct stat info{};assert(::stat((root/"hdd/new/data").c_str(),&info)==0);
 assert(info.st_atime==0x01020304 && info.st_mtime==0x10203040);
 assert(call(809,fd,0x10600)==0 && mem.load(0x1060c,8)==0x01020304 && mem.load(0x10614,8)==0x10203040);
 assert(call(815,0x10000,0)==0x8001000d);
 assert(call(810,path("/dev_hdd0/new/data"),path("/dev_hdd0/new/link",0x10200))==0);
 assert(::stat((root/"hdd/new/link").c_str(),&info)==0 && info.st_nlink==2);
 assert(call(813,path("/dev_hdd0/new"))==0x80010036);
 assert(call(814,0x10000)==0x80010012);
 assert(call(812,path("/dev_hdd0/new/data"),path("/dev_hdd0/new/link",0x10200))==0x80010014);
 assert(call(812,path("/dev_hdd0/new/data"),path("/dev_hdd0/new/moved",0x10200))==0);
 assert(!std::filesystem::exists(root/"hdd/new/data"));
 assert(call(814,path("/dev_hdd0/new/moved"))==0 && call(814,path("/dev_hdd0/new/link"))==0);
 assert(call(832,fd,2)==0 && call(809,fd,0x10600)==0 && mem.load(0x10624,8)==2);
 assert(call(804,fd)==0 && call(832,fd,0)==0x8001002a);
 assert(call(813,path("/dev_hdd0/new"))==0 && !std::filesystem::exists(root/"hdd/new"));
''')

    def test_readonly_errors_mount_boundaries_and_casefold(self):
        self.run_fs(r'''
 {std::ofstream out(root/"disc/ASSET");out<<"disc bytes";}
 s.fs.disc_casefold=true;
 assert(call(816,path("/dev_bdvd/asset"),0)==0);
 for(const auto nr:{811,813,814,815,831,834,835})assert(call(nr,path("/dev_bdvd/asset"),0)==0x80010026);
 assert(call(810,path("/dev_bdvd/asset"),path("/dev_bdvd/link",0x10200))==0x80010026);
 assert(call(812,path("/dev_bdvd/asset"),path("/dev_bdvd/moved",0x10200))==0x80010026);
 assert(call(833,path("relative"),path("/dev_bdvd/link",0x10200))==0x80010026);
 assert(std::filesystem::file_size(root/"disc/ASSET")==10);
 assert(call6(801,path("/dev_bdvd/asset"),0,0x10400)==0);const auto fd=mem.load(0x10400,4);
 assert(call(832,fd,0)==0x8001002a && call(804,fd)==0);
 assert(call(810,path("/dev_hdd0/a"),path("/dev_hdd1/a",0x10200))==0x80010030);
 assert(call(812,path("/dev_hdd0/a"),path("/dev_hdd1/a",0x10200))==0x80010030);
 assert(call(813,path("/dev_hdd0/./"))==0x80010009 && std::filesystem::is_directory(root/"hdd"));
 assert(call(812,path("/dev_hdd0"),path("/dev_hdd0/moved",0x10200))==0x80010009);
 assert(call(811,path("/dev_hdd0/../escape"),0700)==0x80010009);
 std::filesystem::create_symlink(root/"disc/ASSET",root/"hdd/escape");
 assert(call(831,path("/dev_hdd0/escape"),0)==0x80010009);
 assert(std::filesystem::file_size(root/"disc/ASSET")==10);
 assert(call(805,path("/dev_hdd0/escape"),0x10400)==0x80010009);
 assert(call(805,path("/dev_bdvd/asset"),0x10400)==0x8001002e);
 assert(call(805,path("/dev_hdd1/missing"),0x10400)==0x80010006);
''')

    def test_fd_name_and_block_size_be_outputs_aliases_and_errors(self):
        self.run_fs(r'''
 {std::ofstream out(root/"hdd/data");out<<"1234";}
 assert(call6(801,path("/dev_hdd0/data"),0,0x10400)==0);const auto fd=mem.load(0x10400,4);
 assert(call(832,fd,2)==0x8001002a);
 assert(call6(821,fd,0x10500,0x10508,0x10510,0x10518)==0);
 assert(mem.load(0x10500,8)==512 && mem.load(0x10508,8)==4096 && mem.load(0x10510,8)==512 && mem.load(0x10518,4)==0);
 assert(mem.load(0x10500,4)==0 && mem.load(0x10504,4)==0x200);
 assert(call6(821,fd,0x10500,0x10500,0x10500,0x10500)==0 && mem.load(0x10500,8)==512);
 mem.store(0x10500,8,0xaabbccdd);
 assert(call6(821,fd,0x10500,0x10508,0x10510,0)==0x8001000d && mem.load(0x10500,8)==0xaabbccdd);
 assert(call6(800,6,0,0x10400,4,0x10600,64)==0);
 assert(std::string(reinterpret_cast<char*>(mem.access(0x10600,64,false)))=="/dev_hdd0/data");
 assert(call6(800,6,0,0x10400,4,0x10600,5)==0 && mem.load(0x10600,5)==UINT64_C(0x2f64657600));
 assert(call6(800,6,0,0x10400,4,0x10400,64)==0); // output aliases descriptor input
 assert(std::string(reinterpret_cast<char*>(mem.access(0x10400,64,false)))=="/dev_hdd0/data");
 assert(call(804,fd)==0);mem.store(0x10400,4,fd);
 assert(call6(800,6,0,0x10400,4,0x10600,64)==0x8001002a);
 assert(call(805,path("/dev_hdd1"),0x10400)==0);
 assert(call6(800,6,0,0x10400,4,0x10600,64)==0);
 assert(std::string(reinterpret_cast<char*>(mem.access(0x10600,64,false)))=="/dev_hdd1/");
 assert(call(807,mem.load(0x10400,4))==0);
''')

    def test_unsupported_protocols_stop_without_outputs_or_host_mutation(self):
        self.run_fs(r'''
 {std::ofstream out(root/"hdd/data");out<<"preserved";}
 path("/dev_hdd0/data");mem.store(0x10400,8,UINT64_C(0xdeadbeefcafebabe));
 for(const auto nr:{823,824,825,826,827,828,829,830,845,846,847}){
   fault([&]{call(nr,0x10000,0x10400);});
   assert(mem.load(0x10400,8)==UINT64_C(0xdeadbeefcafebabe));
 }
 fault([&]{call6(800,5,0,0x10400,4,0x10500,32);});
 fault([&]{call(816,0x10000,4);});
 fault([&]{call(834,0x10000,0400);});fault([&]{call(835,0x10000,1,2);});
 fault([&]{call(833,path("relative"),path("/dev_hdd0/link",0x10200));});
 assert(!std::filesystem::exists(root/"hdd/link") && std::filesystem::file_size(root/"hdd/data")==9);
 r[11]=799;r[3]=0x12345678;assert(!syscall_fs_ext(s) && r[3]==0x12345678);
 assert(call(816,path("/dev_hdd0/missing"),0)==0x80010006);
 assert(call(816,0,0)==0x8001000d);
''')

    def test_descriptor_limit_and_long_dirent_names(self):
        self.run_fs(r'''
 const std::string name(255,'x');{std::ofstream out(root/"hdd"/name);out<<"x";}
 for(unsigned i=3;i<256;++i){assert(call(805,path("/dev_hdd0"),0x10400)==0 && mem.load(0x10400,4)==i);}
 mem.store(0x10400,4,0xdeadbeef);
 assert(call(805,path("/dev_hdd0"),0x10400)==0x8001002c && mem.load(0x10400,4)==0xdeadbeef);
 assert(call6(801,path(("/dev_hdd0/"+name).c_str()),0,0x10400)==0x8001002c);
 assert(call(806,3,0x10600,0x10508)==0 && call(806,3,0x10600,0x10508)==0);
 assert(call(806,3,0x10600,0x10508)==0 && mem.load(0x10600,2)==0x02ff && mem.load(0x10701,1)==0);
 assert(call(807,3)==0);
 assert(call6(801,path(("/dev_hdd0/"+name).c_str()),0,0x10400)==0 && mem.load(0x10400,4)==3);
 assert(call(804,3)==0);
 for(unsigned i=4;i<256;++i)assert(call(807,i)==0);
''')

    def test_utime_preserves_high_64bit_seconds_and_negative_time(self):
        self.run_fs(r'''
 {std::ofstream out(root/"hdd/time");out<<"t";}
 path("/dev_hdd0/time");
 mem.store(0x10500,8,UINT64_C(0x101020304));mem.store(0x10508,8,UINT64_C(0x110203040));
 assert(call(815,0x10000,0x10500)==0);
 struct stat info{};assert(::stat((root/"hdd/time").c_str(),&info)==0);
 assert(info.st_atime==INT64_C(0x101020304) && info.st_mtime==INT64_C(0x110203040));
 assert(call(808,0x10000,0x10600)==0);
 assert(mem.load(0x1060c,8)==UINT64_C(0x101020304) && mem.load(0x10614,8)==UINT64_C(0x110203040));
 mem.store(0x10500,8,UINT64_MAX);
 assert(call(815,0x10000,0x10500)==0 && call(808,0x10000,0x10600)==0);
 assert(mem.load(0x1060c,8)==UINT64_MAX && mem.load(0x10614,8)==UINT64_C(0x110203040));
''')
