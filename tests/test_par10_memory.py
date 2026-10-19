"""PAR10 asymmetric page admission, exact byte atomics and reservation oracles."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest

from tests.test_par1 import source, MEMORY_CASE

ARTIFACTS = Path('local/ratchet/codex/par10/tests')


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Parallel10Memory(unittest.TestCase):
    def native(self, name, body, *, full=False):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp = ARTIFACTS / (name + '.cpp')
        cpp.write_text(source(body, full=full))
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra',
                                '-Werror', '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))],
                               capture_output=True, timeout=300)
        cpp.with_suffix('.build.log').write_bytes(build.stdout + build.stderr)
        self.assertEqual(build.returncode, 0, build.stderr.decode()[-12000:])
        run = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, timeout=60,
                             env={**os.environ, 'ANYPS3_SPU_THREADS': '0',
                                  'ANYPS3_SPU_WORKER': '0', 'ANYPS3_RSX_WORKER': '0'})
        cpp.with_suffix('.run.log').write_bytes(run.stdout + run.stderr)
        self.assertEqual(run.returncode, 0, run.stderr.decode()[-12000:])

    def test_untracked_scalar_uses_no_stripe_or_broker_mutex(self):
        self.native('untracked', r'''
 GuestMemory m;m.map(0x10000,16384,6,nullptr,0);auto pin=m.regions[0].bytes;
 m.host_access_observer=[](const void*,std::uint64_t,bool){assert(false);};
 m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 m.host_access_observer.watch(pin->data()+8192,4);
 m.enable_concurrency();m.enable_page_broker();
 GuestMemory::GpuPages claim(m,{{pin->data()+8192,4,pin}});
 m.renderer_enabled=true;m.store(0x10000,4,0);m.load(0x10000,4);
 if(m.parallel->asymmetric){
  // Any attempt to use either legacy hot lock deadlocks this bounded test.
  std::lock_guard<std::mutex> broker(m.page_broker.mutex);
  std::vector<std::unique_lock<GuestMemory::ParallelMemory::LineMutex>> locks;
  for(auto& line:m.parallel->lines)locks.emplace_back(line);
  for(unsigned n=0;n<10000;++n){m.store(0x10000,4,0x11223344);assert(m.load(0x10000,4)==0x11223344);}
 }
 assert(m.page_broker.cell(reinterpret_cast<std::uintptr_t>(pin->data())>>12)->state.load()==0);
 m.renderer_enabled=false;
''')

    def test_adjacent_subwords_and_bulk_fringes_keep_neighbours(self):
        self.native('subwords', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 std::atomic<unsigned> ready{0};std::atomic<bool> go{false};std::vector<std::thread> workers;
 for(unsigned n=0;n<8;++n)workers.emplace_back([&,n]{++ready;while(!go.load())std::this_thread::yield();
  for(unsigned i=0;i<10000;++i){m.store(0x10000+n,1,0xa0+n);}m.release_reader_slot();});
 while(ready.load()!=8){std::this_thread::yield();}go=true;for(auto& worker:workers)worker.join();
 assert(m.load(0x10000,8)==UINT64_C(0xa0a1a2a3a4a5a6a7));
 const std::uint8_t bytes[]={0x11,0x22,0x33};m.transfer(0x10003,const_cast<std::uint8_t*>(bytes),3,true);
 assert(m.load(0x10000,8)==UINT64_C(0xa0a1a2112233a6a7));
 m.store(0x1007f,2,0x8899);assert(m.load(0x1007e,4)==0x00889900);
 m.store(0x100ff,8,UINT64_C(0x0123456789abcdef));assert(m.load(0x100ff,8)==UINT64_C(0x0123456789abcdef));
 m.renderer_enabled=false;
''')

    def test_reservation_activation_alias_aba_and_competing_commits(self):
        self.native('reservations', MEMORY_CASE)

    def test_pending_claim_drains_published_scalar_hazard(self):
        self.native('scalar-drain', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 auto pin=m.regions[0].bytes;auto range=m.range(0x10000,4,true);
 std::atomic<bool> ready{false},leave{false};
 std::thread cpu([&]{{GuestMemory::Lease lease(m,0x10000,4,false);ready=true;
  while(!leave.load()){std::this_thread::yield();}assert(m.load(0x10000,4)==0);}m.release_reader_slot();});
 while(!ready.load())std::this_thread::yield();
 auto* cell=m.page_broker.cell(reinterpret_cast<std::uintptr_t>(pin->data())>>12);
 std::thread release([&]{while(!(cell->state.load()&GuestMemory::PageBroker::claimed))std::this_thread::yield();
  GuestMemory::CpuPages late(m.page_broker,std::vector<GuestMemory::HostSpan>{{pin->data(),4,pin}});assert(!late.admitted);leave=true;});
 GuestMemory::GpuPages claim(m,{{pin->data(),4,pin}});cpu.join();release.join();
 const std::uint8_t bytes[]={0x11,0x22,0x33,0x44};claim.commit(range,0,bytes,4);claim.reset();
 assert(m.load(0x10000,4)==0x11223344);
''')

    def test_claim_with_many_spans_commits_in_bounded_time(self):
        # Game run 334st: a texture claim with tens of thousands of runs made each
        # snapshot scan every declared span per covered step (~25 s RSX stalls).
        self.native('many-spans', r'''
 GuestMemory m;m.map(0x10000,0x100000,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 auto pin=m.regions[0].bytes;
 for(unsigned i=0;i<0x100000;i+=4)m.store(0x10000+i,4,0x10000+i); // each BE word holds its guest address
 std::vector<GuestMemory::HostSpan> spans;
 // 65536 declared 16-byte spans in reverse order; every third overlaps its successor by 8 bytes.
 for(unsigned i=65536;i--;)spans.push_back({pin->data()+i*16,(i%3==0 && i+1<65536)?24u:16u,pin});
 auto range=m.range(0x10000,0x100000,false);
 const auto start=std::chrono::steady_clock::now();
 {GuestMemory::GpuPages claim(m,spans);
  auto bytes=claim.snapshot(range,0,0x100000);assert(bytes.size()==0x100000);
  for(unsigned i=0;i<0x100000;i+=4)assert(((unsigned(bytes[i])<<24)|(unsigned(bytes[i+1])<<16)|(unsigned(bytes[i+2])<<8)|bytes[i+3])==0x10000+i);
  auto part=claim.snapshot(range,0x12340,0x1000);
  assert(part.size()==0x1000 && part[0]==0x00 && part[1]==0x02 && part[2]==0x23 && part[3]==0x40);}
 // One missing declared span (bytes 0xfc170..0xfc17f; its neighbours are 16 bytes) is still a hole.
 auto holed=spans;holed.erase(holed.begin()+1000);
 {GuestMemory::GpuPages claim(m,holed);bool rejected=false;
  try{(void)claim.snapshot(range,0,0x100000);}catch(const std::runtime_error& error){rejected=std::string(error.what())=="GPU commit outside declared spans";}
  assert(rejected);
  assert(claim.snapshot(range,0,0xfc170).size()==0xfc170);
  rejected=false;try{(void)claim.snapshot(range,0xfc17f,1);}catch(const std::runtime_error& error){rejected=std::string(error.what())=="GPU commit outside declared spans";}
  assert(rejected);
  assert(claim.snapshot(range,0xfc180,0x100).size()==0x100);}
 // Sorted coverage: milliseconds; the former per-step scan of all spans took ~5 s here.
 assert(std::chrono::steady_clock::now()-start<std::chrono::seconds(1));
''')

    def test_renderer_claim_needs_no_coordinator_pump(self):
        self.native('renderer-pin', r'''
 setenv("ANYPS3_RSX_WORKER","1",1);PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);
 m.store(0x10000,4,0x11223344);auto range=m.range(0x10000,4,true);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;std::atomic<bool> done{false};
 auto request=w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  std::unique_ptr<GuestMemory::GpuPages> claim;
  m.renderer_pin([&]{claim=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{range.parts[0].data,4,range.parts[0].owner}});});
  auto bytes=claim->snapshot(range,0,4);assert((bytes==std::vector<std::uint8_t>{0x11,0x22,0x33,0x44}));done=true;
 });
 // Deliberately do not pump coordinator work until the renderer has finished.
 const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(!done.load() && std::chrono::steady_clock::now()<until)std::this_thread::yield();
 assert(done.load());w.queue->await(request);life.finish();
''', full=True)

    def test_deferred_dirty_alias_snapshot_and_label_order(self):
        self.native('dirty-label', r'''
 setenv("ANYPS3_RSX_WORKER","1",1);PpuState s;auto& m=s.memory;m.map(0x10000,12288,6,nullptr,0);
 m.regions.push_back({0x20000,6,m.regions[0].bytes});m.mapping_changed();
 auto texture=m.range(0x10000,8,false);auto label=m.range(0x12000,4,true);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;
 std::shared_ptr<GuestMemory::CpuDirtyWatch> watch;
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{watch=m.watch_cpu_dirty(texture);assert(watch->consume());});
 std::atomic<bool> entered{false},resume{false},uploaded{false};
 auto request=w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{entered=true;
  while(!resume.load()){std::this_thread::yield();}
  assert(watch->consume());auto bytes=m.snapshot(texture,0,8);
  assert((bytes==std::vector<std::uint8_t>{0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88}));
  assert(!watch->consume());uploaded=true;
  std::unique_ptr<GuestMemory::GpuPages> claim;
  m.renderer_pin([&]{claim=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{label.parts[0].data,4,label.parts[0].owner}});});
  const std::uint8_t finished[]={0x80,0,0,7};claim->commit(label,0,finished,4);
 });
 while(!entered.load()){std::this_thread::yield();}
 // This write completes with the RSX owner deliberately unavailable.
 m.store(0x20000,8,UINT64_C(0x1122334455667788));assert(watch->dirty.load());resume=true;
 w.queue->await(request);assert(m.load(0x22000,4)==0x80000007 && uploaded.load());
 m.store(0x10020,4,1);assert(!watch->consume());
 m.store(0x10000,8,UINT64_C(0x1122334455667788));assert(watch->consume()); // same value is damage, too
 fault([&]{m.snapshot(texture,7,2);});
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{m.unwatch_cpu_dirty(watch);});life.finish();
''', full=True)

    def test_reservation_activation_waits_for_prior_untracked_store(self):
        self.native('activation', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 if(m.parallel->asymmetric){
  std::atomic<bool> active{false},retire{false};auto pin=m.regions[0].bytes;
  std::thread writer([&]{GuestMemory::Lease lease(m,0x10000,4,true);
   const auto& map=m.translate_parallel(0x10000,4,true);GuestMemory::ScalarWrite store(m,map,0x10000,4);assert(!store.tracked);active=true;
   while(!retire.load()){std::this_thread::yield();}GuestMemory::atomic_scalar_write(pin->data(),4,0x11223344);
  });
  while(!active.load()){std::this_thread::yield();}
  auto state=m.parallel->snapshot->maps[0].state;
  std::thread helper([&]{while(!state->tracked[0].load()){std::this_thread::yield();}retire=true;});
  std::pair<std::uintptr_t,std::uint64_t> token;assert(m.load_reserved(0x10000,4,token)==0x11223344);
  helper.join();writer.join();m.store(0x10000,4,0x11223344);assert(!m.store_conditional(0x10000,4,9,token));
 }
 m.renderer_enabled=false;
''')

    def test_unrelated_listener_does_not_lock_tracked_store(self):
        self.native('listener-filter', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 auto pin=m.regions[0].bytes;std::pair<std::uintptr_t,std::uint64_t> token;m.load_reserved(0x10000,4,token);
 auto listener=std::make_unique<GuestMemory::ReservationWatch>(m.parallel->notifications,reinterpret_cast<std::uintptr_t>(pin->data()+4096),[]{});
 {std::lock_guard<std::mutex> lock(m.parallel->notifications->mutex);m.store(0x10000,4,0x11223344);}
 assert(!m.store_conditional(0x10000,4,1,token));
 listener.reset();m.renderer_enabled=false;
''')

    def test_seq_cst_fallback_preserves_memory_contract(self):
        self.native('fallback', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.parallel->asymmetric=false;m.enable_page_broker();m.renderer_enabled=true;
 m.store(0x10000,8,UINT64_C(0x1122334455667788));std::pair<std::uintptr_t,std::uint64_t> token;
 m.load_reserved(0x10000,4,token);std::thread writer([&]{m.store(0x10000,4,0x11223344);m.release_reader_slot();});writer.join();
 assert(!m.store_conditional(0x10000,4,1,token) && m.load(0x10000,8)==UINT64_C(0x1122334455667788));m.renderer_enabled=false;
''')
