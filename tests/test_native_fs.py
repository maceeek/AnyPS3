"""Synthetic disc metadata tests: no game or firmware files required."""
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from anyps3.native_fs import TYPES
from tests import test_runtime_objects


def disc_sfo():
    keys=b'TITLE_ID\0PARENTAL_LEVEL\0CATEGORY\0'
    values=b'TEST00001\0'+struct.pack('<I',3)+b'DG\0'
    entries=struct.pack('<HHIII',0,0x204,10,10,0)
    entries+=struct.pack('<HHIII',9,0x404,4,4,10)
    entries+=struct.pack('<HHIII',24,0x204,3,3,14)
    return struct.pack('<5I',0x46535000,0x101,68,68+len(keys),3)+entries+keys+values


@unittest.skipUnless(shutil.which('c++'),'Requires C++ compiler')
class NativeDiscTests(unittest.TestCase):
    def test_explicit_iso_case_mapping_preserves_exact_names_and_isolation(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char parent[]="/tmp/anyps3-case-test-XXXXXX";assert(::mkdtemp(parent));
 s.fs.disc=parent;s.fs.hdd_root=parent;
 const auto dir=std::filesystem::path(parent)/"PACKED";std::filesystem::create_directory(dir);
 {std::ofstream file(dir/"GAME.PSARC");file<<"real archive bytes";}
 assert(!std::filesystem::exists(s.fs.resolve("/dev_bdvd/packed/game.psarc").host));
 s.fs.disc_casefold=true;
 assert(s.fs.resolve("/dev_bdvd/packed/game.psarc").host==dir/"GAME.PSARC");
 assert(!std::filesystem::exists(s.fs.resolve("/dev_hdd0/packed/game.psarc").host));
 {std::ofstream file(dir/"Case");file<<"one";}{std::ofstream file(dir/"cASE");file<<"two";}
 assert(s.fs.resolve("/dev_bdvd/packed/Case").host==dir/"Case");
 fault([&]{s.fs.resolve("/dev_bdvd/packed/case");});
 std::filesystem::create_symlink("/etc/passwd",dir/"LINK");
 bool rejected=false;try{s.fs.resolve("/dev_bdvd/packed/link");}catch(const NativeFilesystem::Error& e){rejected=e.code==0x80010009;}
 assert(rejected);std::filesystem::remove_all(parent);
''')

    def test_file_descriptors_real_io_offsets_eof_and_readonly_disc(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 char parent[]="/tmp/anyps3-io-test-XXXXXX";assert(::mkdtemp(parent));
 s.fs.cache=parent;s.fs.disc=parent;
 auto path=[&](const char* name){std::copy_n(name,std::string(name).size()+1,mem.access(0x10000,std::string(name).size()+1,true));};
 path("/dev_hdd1/data");r[7]=0;r[8]=0;
 assert(call(801,0x10000,0100|2,0x10100,0600)==0);const auto fd=mem.load(0x10100,4);
 assert(fd>=3 && s.fs.files.size()==1);
 std::copy_n("abcde",5,mem.access(0x10200,5,true));
 assert(call(803,fd,0x10200,5,0x10108)==0 && mem.load(0x10108,8)==5);
 assert(call(819,fd)==0 && call(820,fd)==0);
 assert(call(818,fd,-2,2,0x10108)==0 && mem.load(0x10108,8)==3);
 assert(call(802,fd,0x10200,5,0x10108)==0 && mem.load(0x10108,8)==2);
 assert(mem.load(0x10200,2)==0x6465);
 assert(call(802,fd,0x10200,5,0x10108)==0 && mem.load(0x10108,8)==0);
 assert(call(809,fd,0x10300)==0 && mem.load(0x10324,8)==5 && mem.load(0x1032c,8)==32768);
 assert(call(818,fd,-1,0,0x10108)==0x80010002);
 assert(call(804,fd)==0 && s.fs.files.empty());assert(call(804,fd)==0x8001002a);
 path("/dev_bdvd/data");
 assert(call(801,0x10000,0,0x10100)==0);const auto reader=mem.load(0x10100,4);
 assert(call(803,reader,0x10200,1,0x10108)==0x8001002a);
 assert(call(819,reader)==0x8001002a && call(820,reader)==0x8001002a);
 assert(call(802,reader,0,1,0x10108)==0x8001000d);
 assert(call(802,reader,0x10200,5,0x10108)==0 && mem.load(0x10200,4)==0x61626364);
 assert(call(801,0x10000,01000|2,0x10100)==0x80010026);
 assert(std::filesystem::file_size(std::filesystem::path(parent)/"data")==5);
 assert(call(804,reader)==0);
 path("/dev_hdd1/data");assert(call(801,0x10000,0100|0200|2,0x10100)==0x80010014);
 path("/dev_hdd1/missing");mem.store(0x10100,4,0xdeadbeef);
 assert(call(801,0x10000,0,0x10100)==0x80010006 && mem.load(0x10100,4)==0xdeadbeef);
 path("/dev_hdd1");assert(call(801,0x10000,0,0x10100)==0x80010012);
 // Buffers spanning two adjacent, separately mapped regions (original ACIT
 // cellFsRead at 0x42effd2a crossed 0x42f00000). Bytes land in both regions.
 mem.map(0x30000,0x1000,6,nullptr,0);mem.map(0x31000,0x1000,6,nullptr,0);
 for(unsigned i=0;i<8;++i)mem.store(0x30ffc+i,1,0x41+i);
 path("/dev_hdd1/split");assert(call(801,0x10000,0100|2,0x10100,0600)==0);const auto split=mem.load(0x10100,4);
 assert(call(803,split,0x30ffc,8,0x10108)==0 && mem.load(0x10108,8)==8);
 assert(call(818,split,0,0,0x10108)==0);
 for(unsigned i=0;i<8;++i)mem.store(0x30ffc+i,1,0);
 assert(call(802,split,0x30ffd,8,0x10108)==0 && mem.load(0x10108,8)==8);
 assert(mem.load(0x30ffc,1)==0 && mem.load(0x30ffd,3)==0x414243 && mem.load(0x31000,4)==0x44454647 && mem.load(0x31004,1)==0x48);
 assert(call(818,split,0,0,0x10108)==0);
 fault([&]{call(802,split,0x31ffc,8,0x10108);}); // second half unmapped: explicit stop
 assert(call(804,split)==0);
 std::filesystem::remove_all(parent);
''')

    def test_stat_real_files_mounts_errors_and_isolation(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 assert(NativeFilesystem::host_error(EACCES)==0x80010029 && NativeFilesystem::host_error(EPERM)==0x80010009);
 assert(NativeFilesystem::host_error(EMFILE)==0x8001002c && NativeFilesystem::host_error(ENFILE)==0x80010022);
 char parent[]="/tmp/anyps3-stat-test-XXXXXX";assert(::mkdtemp(parent));
 s.fs.disc=parent;s.fs.hdd_root=parent;
 {std::ofstream file(std::filesystem::path(parent)/"asset");file<<"hello";}
 auto stat=[&](const char* path){std::copy_n(path,std::string(path).size()+1,mem.access(0x10000,std::string(path).size()+1,true));return call(808,0x10000,0x10100);};
 assert(stat("/dev_bdvd/asset")==0);
 assert(mem.load(0x10100,4)==0100444 && mem.load(0x10104,4)==UINT32_MAX);
 assert(mem.load(0x10124,8)==5 && mem.load(0x1012c,8)==2048);
 assert(stat("/dev_hdd0")==0 && mem.load(0x10100,4)==0040777 && mem.load(0x10124,8)==4096);
 assert(call(822,0x10000,0x10200,0x10208,0x10210)==0);
 assert(mem.load(0x10200,8)==512 && mem.load(0x10208,8)==4096 && mem.load(0x10210,8)==512);
 const auto before=std::filesystem::space(parent);
 assert(call(840,0x10000,0x10200,0x10208)==0);
 assert(mem.load(0x10200,8)==before.capacity && mem.load(0x10208,8)>0 && mem.load(0x10208,8)<=before.capacity);
 assert(stat("/dev_bdvd")==0 && call(840,0x10000,0x10200,0x10208)==0);
 assert(mem.load(0x10200,8)==0 && mem.load(0x10208,8)==0);
 assert(stat("/dev_bdvd/missing")==0x80010006);
 assert(stat("/dev_hdd1")==0x8001003a && stat("/dev_unknown")==0x8001003a);
 assert(stat("/dev_bdvd/asset/child")==0x8001002e && stat("/dev_bdvd/asset/.")==0x8001002e);
 assert(stat("/dev_bdvd/../asset")==0x80010009);
 std::filesystem::create_symlink("/etc/passwd",std::filesystem::path(parent)/"link");
 assert(stat("/dev_bdvd/link")==0x80010009);
 assert(call(808,0,0x10100)==0x8001000d);
 std::filesystem::remove_all(parent);
''')

    def test_mount_capability_falls_back_without_fabricating_device(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 const auto arg=0x10000;
 mem.store(arg,4,32);mem.store(arg+4,4,16);mem.store(arg+8,4,24);
 mem.store(arg+12,4,10);mem.store(arg+16,4,0x10100);
 std::copy_n("/dev_hdd1",10,mem.access(0x10100,10,true));
 mem.store(arg+24,4,0xdeadbeef);mem.store(arg+28,4,0xdeadbeef);
 assert(call(817,UINT32_MAX,0xc0000006,arg,32)==0);
 assert(mem.load(arg+24,4)==0x80010037 && mem.load(arg+28,4)==0);
 mem.store(arg+24,4,0xdeadbeef);
 assert(call(817,UINT32_MAX,0xc0000006,arg,31)==0x80010002);
 assert(mem.load(arg+24,4)==0xdeadbeef);
 mem.store(arg+8,4,28);
 assert(call(817,UINT32_MAX,0xc0000006,arg,32)==0x80010002);
 fault([&]{call(817,UINT32_MAX,0xc0000007,arg,32);});
''')

    def test_cache_mount_relay_clear_and_path_isolation(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char parent[]="/tmp/anyps3-cache-test-XXXXXX";assert(::mkdtemp(parent));
 s.fs.configure_cache(parent);assert(s.fs.clear_cache()==0x8002bc04);
 auto mount=[&](const char* name){std::fill_n(mem.access(0x10000,1092,true),1092,0);std::copy_n(name,std::string(name).size(),mem.access(0x10000,std::string(name).size(),true));s.r[3]=0x10000;return native_cache_mount(s);};
 s.r[3]=0;assert(native_cache_mount(s)==0x8002bc03);
 assert(mount("../outside")==0x8002bc03 && s.fs.cache.empty());
 assert(mount("TEST_CACHE")==0 && std::filesystem::is_directory(s.fs.cache));
 assert(std::string(reinterpret_cast<char*>(mem.access(0x10020,10,false)))=="/dev_hdd1");
 const auto first=s.fs.cache;
 {std::ofstream file(first/"data");file<<"preserved";}
 assert(mount("TEST_CACHE")==1 && s.fs.cache==first && std::filesystem::file_size(first/"data")==9);
 {std::ofstream file(std::filesystem::path(parent)/"outside");file<<"safe";}
 std::filesystem::create_symlink(std::filesystem::path(parent)/"outside",first/"link");
 assert(s.fs.clear_cache()==0 && std::filesystem::is_empty(first));
 assert(std::filesystem::file_size(std::filesystem::path(parent)/"outside")==4);
 {std::ofstream file(first/"data");file<<"discarded";}
 assert(mount("SECOND")==0 && s.fs.cache!=first && !std::filesystem::exists(first));
 const auto second=s.fs.cache;
 std::fill_n(mem.access(0x10000,32,true),32,'A');s.r[3]=0x10000;
 assert(native_cache_mount(s)==0x8002bc03 && s.fs.cache==second);
 assert(s.fs.mount_cache("")==0);const auto unnamed=s.fs.cache;
 assert(s.fs.mount_cache("")==0 && s.fs.cache!=unnamed && !std::filesystem::exists(unnamed));
 std::filesystem::remove_all(parent);
''')

    def test_real_file_metadata_and_malformed_sfo(self):
        source='''#include <array>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <set>
#include <vector>
#include <algorithm>
#include <cassert>
'''+TYPES+r'''
int main(int argc,char** argv) {
    assert(argc==2);NativeFilesystem fs;
    try {fs.mount(argv[1]);}
    catch(const std::exception&) {assert(fs.disc.empty());return 1;}
    assert(std::string(reinterpret_cast<const char*>(fs.boot_info.data()))=="TEST00001");
    assert(fs.boot_info[12]==0 && fs.boot_info[15]==3);
    for(unsigned i=16;i<32;++i)assert(fs.boot_info[i]==0);
}
'''
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'PS3_GAME').mkdir();cpp=root/'test.cpp';binary=root/'test';cpp.write_text(source)
            build=subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(cpp),'-o',str(binary)],capture_output=True,text=True,timeout=300)
            self.assertEqual(build.returncode,0,build.stderr)
            original=disc_sfo()
            invalid_id=bytearray(original);invalid_id[-17]=ord('/')
            invalid_offset=bytearray(original);struct.pack_into('<I',invalid_offset,32,0xffffffff)
            for data,expected in [(original,0),(original[:-1],1),(bytes(invalid_id),1),(bytes(invalid_offset),1)]:
                (root/'PS3_GAME/PARAM.SFO').write_bytes(data)
                run=subprocess.run([str(binary),str(root)],capture_output=True,text=True,timeout=5)
                self.assertEqual(run.returncode,expected,run.stderr)
