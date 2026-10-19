"""PPUFAST M1: flat guest view, inline admission table and section protocol.

Expected values are written as literal big-endian bytes or come from the
unchanged admitted byte API; nothing is recomputed with the inline helpers.
"""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source

ARTIFACTS = Path('local/ratchet/codex/ppufast/tests')


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuFastMemory(unittest.TestCase):
    def native(self, name, body):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp = ARTIFACTS / (name + '.cpp')
        cpp.write_text(source(body).replace('#include <cassert>', '#include <cassert>\n#include <chrono>'))
        command = ['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                   '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))]
        p = subprocess.run(command, capture_output=True, timeout=300)
        cpp.with_suffix('.build.log').write_bytes(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-12000:])
        env = {**os.environ, 'SDL_AUDIODRIVER': 'dummy', 'ANYPS3_AUDIO_SINK': 'null'}
        env.pop('ANYPS3_AUDIO_AUDIBLE', None)
        p = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, timeout=120, env=env)
        cpp.with_suffix('.run.log').write_bytes(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-12000:])
        if b'SKIP-NO-MEMBARRIER' in p.stdout:
            self.skipTest('host lacks private expedited membarrier; inline view stays disabled')
        return p

    SETUP = r'''
 GuestMemory m;
 m.map(0x10000,0x3000,6,nullptr,0);
 m.map(0x20000,0x1800,6,nullptr,0);
 m.map(0x30000,0x1000,5,nullptr,0);
 m.map(0x40000,0x1000,4,nullptr,0);
 m.regions.push_back({0x50000,6,m.regions[0].bytes});
 m.map(0x60800,0x800,6,nullptr,0);
 m.enable_concurrency();m.enable_page_broker();
 if(!m.parallel->asymmetric){std::puts("SKIP-NO-MEMBARRIER");return 0;}
 assert(m.inline_enabled.load(std::memory_order_acquire));
 auto pin=m.regions[0].bytes;const auto host=reinterpret_cast<std::uintptr_t>(pin->data())>>12;(void)host;
 std::uint64_t v=0;(void)v;
'''

    def test_endianness_bounds_permissions_and_aliases(self):
        self.native('layout', self.SETUP + r'''
 assert(!m.inline_view.active);assert(!m.inline_try_load<4>(0x10000,v)); // blocked table outside a section
 m.store(0x10000,8,UINT64_C(0x0102030405060708));
 m.inline_resume();assert(m.inline_view.active);
 assert(m.inline_try_load<8>(0x10000,v) && v==UINT64_C(0x0102030405060708));
 assert(m.inline_try_load<4>(0x10004,v) && v==0x05060708);
 assert(m.inline_try_load<2>(0x10001,v) && v==0x0203);
 assert(m.inline_try_load<1>(0x10007,v) && v==0x08);
 assert(m.inline_try_load<8>(0x50000,v) && v==UINT64_C(0x0102030405060708)); // physical alias
 assert(m.inline_try_store<4>(0x10103,0xa1b2c3d4));
 assert(m.inline_try_store<2>(0x10201,0xbeef) && m.inline_try_store<1>(0x10300,0x7f));
 assert(m.inline_try_store<8>(0x10ff8,UINT64_C(0x1122334455667788)));
 m.inline_suspend();assert(!m.inline_view.active);
 std::array<std::uint8_t,4> b{};m.transfer(0x10103,b.data(),4,false);
 assert(b[0]==0xa1 && b[1]==0xb2 && b[2]==0xc3 && b[3]==0xd4);
 assert(m.load(0x50103,4)==0xa1b2c3d4 && m.load(0x10201,2)==0xbeef && m.load(0x50300,1)==0x7f);
 assert(m.load(0x10ff8,8)==UINT64_C(0x1122334455667788) && m.load(0x10ffc,4)==0x55667788);
 m.inline_resume();
 assert(!m.inline_try_load<4>(0x10ffe,v) && !m.inline_try_store<2>(0x10fff,1)); // page crossing
 assert(m.inline_try_load<1>(0x10fff,v) && v==0x88);
 assert(!m.inline_try_load<4>(0xfffffffe,v) && !m.inline_try_load<1>(UINT64_C(0x100000000),v));
 assert(!m.inline_try_load<4>(0x70000,v) && !m.inline_try_load<4>(0x13000,v));      // unmapped
 assert(m.inline_try_load<4>(0x20ffc,v) && !m.inline_try_load<4>(0x21000,v));       // partial final page
 assert(!m.inline_try_store<4>(0x30000,1) && m.inline_try_load<4>(0x30000,v));      // code: no store
 assert(!m.inline_try_store<4>(0x40000,1) && m.inline_try_load<4>(0x40000,v));      // read-only
 assert(!m.inline_try_load<4>(0x60800,v));                                          // unaligned map
 m.inline_suspend();
 fault([&]{m.store(0x30000,4,1);});fault([&]{m.load(0x70000,4);});fault([&]{m.store(0x40000,4,1);});
 assert(m.load(0x21000,4)==0 && m.load(0x60800,4)==0);fault([&]{m.load(0x217fe,4);});
 // Remap at the same address: the view follows the new backing.
 {GuestMemory::RawPermit raw(m);m.regions.erase(std::find_if(m.regions.begin(),m.regions.end(),[](const auto& r){return r.base==0x40000;}));}
 m.mapping_changed();m.inline_resume();assert(!m.inline_try_load<4>(0x40000,v));m.inline_suspend();
 m.map(0x40000,0x1000,6,nullptr,0);m.store(0x40000,4,0x12345678);
 m.inline_resume();assert(m.inline_try_load<4>(0x40000,v) && v==0x12345678 && m.inline_try_store<4>(0x40004,9));
 m.inline_suspend();assert(m.load(0x40004,4)==9 && m.inline_flat.remaps>=6);
''')

    def test_reservations_leave_fast_path_and_wait_for_sections(self):
        self.native('reservation', self.SETUP + r'''
 std::pair<std::uintptr_t,std::uint64_t> token;assert(m.load_reserved(0x11000,4,token)==0);
 m.inline_resume();
 assert(!m.inline_try_store<4>(0x11080,5) && !m.inline_try_store<4>(0x51080,5)); // page + alias tracked
 assert(m.inline_try_load<4>(0x11000,v));                                            // loads stay inline
 const std::uint8_t five[4]={0,0,0,5};
 assert(m.inline_medium_store(0x11080,4,five));   // untracked line of a tracked page
 assert(!m.inline_medium_store(0x11000,4,five));  // reserved line keeps the admitted store
 m.inline_suspend();
 assert(m.store_conditional(0x11000,4,3,token) && m.load(0x11000,4)==3);
 assert(m.load_reserved(0x11000,4,token)==3);m.store(0x51000,4,7);assert(!m.store_conditional(0x11000,4,9,token));
 assert(m.load(0x11080,4)==5 && m.load(0x11000,4)==7);
 // A foreign first reservation waits until the owner's section ends; the
 // owner's in-section store is then visible in its reservation snapshot.
 m.inline_resume();assert(m.inline_try_store<4>(0x12000,0x77));
 std::atomic<bool> got{false};std::array<std::uint8_t,128> line{};
 std::thread spu([&]{std::pair<std::uintptr_t,std::uint64_t> t;m.getllar(0x12000,line.data(),t);got=true;m.release_reader_slot();});
 std::this_thread::sleep_for(std::chrono::milliseconds(100));
 assert(!got.load());
 m.inline_suspend();spu.join();assert(got.load() && line[3]==0x77);
 m.inline_resume();assert(!m.inline_try_store<4>(0x12000,1));m.inline_suspend();
''')

    def test_claims_observers_dirty_raw_and_concurrent_admitted_users(self):
        self.native('claims', self.SETUP + r'''
 // GPU claim from a delegated thread waits for the section, blocks inline
 // access on every alias and is cleared lazily after release.
 m.store(0x10010,4,0x0badf00d);
 m.inline_resume();assert(m.inline_try_load<4>(0x10010,v) && v==0x0badf00d);
 std::atomic<int> phase{0};
 std::thread gpu([&]{m.page_broker.delegated_owner=std::this_thread::get_id();phase=1;
  auto token=m.page_broker.acquire({host},true);phase=2;while(phase.load()!=3)std::this_thread::yield();
  m.page_broker.release({host},token);phase=4;m.release_reader_slot();});
 while(phase.load()<1)std::this_thread::yield();
 std::this_thread::sleep_for(std::chrono::milliseconds(100));assert(phase.load()==1);
 assert(!m.inline_try_load<4>(0x10010,v) && !m.inline_try_load<4>(0x50010,v) && !m.inline_try_store<4>(0x10010,1));
 m.inline_suspend();while(phase.load()<2)std::this_thread::yield();phase=3;while(phase.load()<4)std::this_thread::yield();gpu.join();
 m.inline_resume();assert(!m.inline_try_load<4>(0x10010,v));
 std::uint8_t out[4]{};assert(m.inline_medium_load(0x10010,4,out) && out[0]==0x0b && out[3]==0x0d);
 assert(m.inline_try_load<4>(0x10010,v) && !m.inline_try_load<4>(0x50010,v) && m.inline_counters.claim_clears==1);
 m.inline_suspend();
 // RSX synchronous watch: only the watched page leaves the inline path and
 // the admitted path still calls the observer.
 std::vector<bool> calls;
 {GuestMemory::RawPermit raw(m);m.host_access_observer=[&](const void*,std::uint64_t,bool w){calls.push_back(w);};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;}
 {GuestMemory::MetadataPermit meta(m);m.host_access_observer.watch(pin->data()+0x100,4);}
 m.inline_resume();assert(m.inline_view.active);
 assert(!m.inline_try_load<4>(0x10100,v) && !m.inline_try_store<4>(0x10180,1) && !m.inline_try_load<4>(0x50104,v));
 assert(m.inline_try_load<4>(0x11000,v));
 assert(!m.inline_medium_store(0x10180,4,out));
 m.inline_suspend();(void)m.load(0x10100,4);assert(!calls.empty() && !calls.back());const auto loads=calls.size();
 m.store(0x10100,4,1);assert(calls.size()>loads && calls.back());
 {GuestMemory::MetadataPermit meta(m);m.host_access_observer.watch(pin->data()+0x100,4,false);}
 m.inline_resume();assert(m.inline_try_load<4>(0x10100,v) && v==1);m.inline_suspend();
 // An unfiltered observer disables sections entirely.
 {GuestMemory::RawPermit raw(m);m.host_access_observer.filtered=false;}
 m.inline_resume();assert(!m.inline_view.active);
 {GuestMemory::RawPermit raw(m);m.host_access_observer=nullptr;}
 // Deferred CPU dirty watch: medium store publishes after the payload.
 auto watch=m.watch_cpu_dirty(m.range(0x10200,8,false));assert(watch->consume());
 m.inline_resume();assert(m.inline_view.active);
 assert(!m.inline_try_store<4>(0x10204,1) && m.inline_try_load<4>(0x10204,v));
 const std::uint8_t one[4]={0,0,0,1};
 assert(m.inline_medium_store(0x10204,4,one) && watch->consume());
 assert(m.inline_medium_store(0x10300,4,one) && !watch->consume());
 m.inline_suspend();assert(m.load(0x10204,4)==1);m.unwatch_cpu_dirty(watch);
 m.inline_resume();assert(m.inline_try_store<4>(0x10204,2));m.inline_suspend();
 // A renderer RawPermit excludes the section and vice versa.
 std::atomic<int> raw_phase{0};
 m.inline_resume();
 std::atomic<bool> go{false};
 std::thread renderer([&]{m.renderer_thread=std::this_thread::get_id();raw_phase=1;while(!go.load())std::this_thread::yield();
  {GuestMemory::RawPermit raw(m);raw_phase=2;std::this_thread::sleep_for(std::chrono::milliseconds(50));raw_phase=3;}
  m.release_reader_slot();});
 while(raw_phase.load()<1)std::this_thread::yield();
 m.renderer_enabled=true;go=true;
 std::this_thread::sleep_for(std::chrono::milliseconds(100));assert(raw_phase.load()==1);
 m.inline_suspend();while(raw_phase.load()<2)std::this_thread::yield();
 m.inline_resume();assert(raw_phase.load()==3 && m.inline_view.active);   // waited at the closed gate
 renderer.join();m.inline_suspend();m.renderer_enabled=false;
 // Admitted users on other threads run concurrently with inline sections.
 std::atomic<bool> stop{false};std::atomic<unsigned> rounds{0};
 std::thread worker([&]{while(!stop.load()){m.store(0x10400,4,m.load(0x10400,4)+1);++rounds;}m.release_reader_slot();});
 for(unsigned i=0;i<2000;++i){m.inline_resume();std::uint64_t x=0;assert(m.inline_try_load<4>(0x10400,x));
  assert(m.inline_try_store<4>(0x10800,i));m.inline_suspend();
  if(i%500==0){m.map(0x80000,0x1000,6,nullptr,0);{GuestMemory::RawPermit raw(m);m.regions.pop_back();}m.mapping_changed();}}
 while(rounds.load()<1000)std::this_thread::yield();
 stop=true;worker.join();assert(m.load(0x10400,4)==rounds.load() && m.load(0x10800,4)==1999);
''')


if __name__ == '__main__':
    unittest.main()
