"""Native split-range contracts: literal bytes, aliases, permissions and tokens."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.runtime import MEMORY_CPP


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class GuestMemoryRsxmemTests(unittest.TestCase):
    def test_split_ranges_and_scoped_snapshots(self):
        body = r'''
#include <cassert>
template<class F> static std::string fault(F f){try{f();}catch(const std::runtime_error& e){return e.what();}assert(false);return {};}
int main(){
 assert(crc32({})==0);GuestMemory m;m.map(0x40000,0x100,6,nullptr,0);m.map(0x40100,0x100,6,nullptr,0);
 m.regions[0].origin="first allocation";m.regions[1].origin="second allocation";
 m.store(0x400fc,4,0x11223344);m.store(0x40100,4,0x55667788);
 auto* held=m.access(0x40000,1,false);const auto hint=m.last_region;
 const auto error=fault([&]{m.access(0x400fc,8,false);});
 assert(error.find("00040000-00040100 flags=6 first allocation")!=std::string::npos);
 assert(error.find("00040100-00040200 flags=6 second allocation")!=std::string::npos);
 assert(m.last_region==hint);
 const auto before0=m.reservation_token(0x40080),before1=m.reservation_token(0x40100);
 const auto versions=m.write_versions;
 auto span=m.range(0x400fc,8,true);assert(!span.pointer(0,8));
 assert(m.write_versions==versions);std::uint8_t bytes[8];span.read(0,bytes,8);
 const std::uint8_t expected[]={0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88};
 assert(std::equal(bytes,bytes+8,expected));
 // Existing host pointers and a different effective address alias keep live backing.
 m.regions.push_back({0x60000,6,m.regions[1].bytes});
 {
  GuestMemory::SplitReadScope read(m);fault([&]{m.access(0x90000,0,false);});const auto* snapshot=m.access(0x400fc,8,false);
  assert(std::equal(snapshot,snapshot+8,expected));
  const std::uint8_t replacement[]={0xa1,0xb2,0xc3,0xd4,0xe5,0xf6,7,8};
  span.write(0,replacement,8);
  assert(m.load(0x400fc,4)==0xa1b2c3d4 && m.load(0x60000,4)==0xe5f60708);
  assert(std::equal(snapshot,snapshot+8,expected)); // immutable previous snapshot
  const auto* fresh=m.access(0x400fc,8,false);assert(std::equal(fresh,fresh+8,replacement));
  {GuestMemory::SplitReadScope nested(m);assert(m.load(0x400ff,2)==0xd4e5);}
  assert(m.load(0x400ff,2)==0xd4e5);
  fault([&]{m.access(0x400fc,8,true);}); // no implicit write scratch
  fault([&]{m.reservation_token(0x400fc,8);}); // no scratch reservations
 }
 assert(!m.split_reads && held==m.access(0x40000,1,false));
 assert(m.reservation_token(0x40080)!=before0 && m.reservation_token(0x40100)!=before1);
 const auto saved=m.write_versions;const auto left=m.load(0x400fc,4);
 m.regions[1].flags=4;fault([&]{m.range(0x400fc,8,true);});assert(m.write_versions==saved && m.load(0x400fc,4)==left);
 m.regions[1].flags=7;fault([&]{m.range(0x400fc,8,true);});assert(m.write_versions==saved);
 m.regions[1].flags=2;fault([&]{GuestMemory::SplitReadScope reads(m);m.access(0x400fc,8,false);});assert(!m.split_reads);
 m.regions.erase(m.regions.begin()+1);fault([&]{m.range(0x400fc,8,false);});
 fault([&]{m.range(UINT32_MAX,2,false);});fault([&]{span.read(7,bytes,2);});
 // Range pins storage, but no access succeeds through an unmapped address.
 assert(m.load(0x60000,4)==0xe5f60708);fault([&]{m.access(0x40100,4,false);});
 std::puts("PASS split bytes, scoped/nested lifetime, stable pointers, aliases, permissions, holes, reservation versions");
}
'''
        with tempfile.TemporaryDirectory(prefix='guest-rsxmem-') as d:
            root = Path(d)
            (root/'test.cpp').write_text('#include <cstdint>\n#include <cinttypes>\n#include <cstdio>\n'+MEMORY_CPP+body)
            build = subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-o',str(root/'test')], capture_output=True,text=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            run = subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)

    def test_mmapper_allocation_provenance_and_original_split_span(self):
        from tests.test_runtime_objects import RuntimeObjectsTests
        RuntimeObjectsTests().run_native(r'''
 assert(call(330,0x10000000,15,0x40000000,0x10000)==0 && mem.load(0x10000,4)==0x40000000);
 for(unsigned i=0;i<3;++i){
  assert(call(332,UINT64_C(0xffff000000000000),0x100000,0x400,0x10004)==0);
  const auto id=mem.load(0x10004,4);assert(call(334,0x49a00000+i*0x100000,id,0x40000)==0);
 }
 const auto before=mem.allocated;auto* held=mem.access(0x49ac8c00,1,false);
 const auto stop=[&](){try{mem.access(0x49ac8c00,1638400,false);}catch(const std::runtime_error& e){return std::string(e.what());}assert(false);return std::string();}();
 assert(stop.find("49a00000-49b00000 flags=6 sys_mmapper_map_memory object=")!=std::string::npos);
 assert(stop.find("49b00000-49c00000 flags=6 sys_mmapper_map_memory object=")!=std::string::npos);
 assert(stop.find("49c00000-49d00000 flags=6 sys_mmapper_map_memory object=")!=std::string::npos);
 assert(stop.find("container=0 area=1073741824")!=std::string::npos);
 auto span=mem.range(0x49ac8c00,1638400,true);assert(span.parts.size()==3);
 assert(span.parts[0].size==0x37400 && span.parts[1].size==0x100000 && span.parts[2].size==0x58c00);
 const std::uint8_t expected[]={0x12,0x34,0x56,0x78};span.write(0,expected,4);span.write(0x18fffc,expected,4);
 assert(mem.load(0x49ac8c00,4)==0x12345678 && mem.load(0x49c58bfc,4)==0x12345678);
 assert(mem.allocated==before && held==mem.access(0x49ac8c00,1,false));
''')
