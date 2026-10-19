"""PAR12 inline memory and idle coordinator contracts, with fixed native oracles."""
from pathlib import Path
import os,shutil,subprocess,unittest
from tests.test_par1 import source

ARTIFACTS=Path('local/ratchet/codex/par12/tests')

@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class Parallel12Runtime(unittest.TestCase):
    def native(self,name,body,*,full=False,instrument=False):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        code=source(body,full=full)
        if instrument:
            code=code.replace('struct GuestMemory {','static thread_local unsigned par12_slow_loads=0,par12_slow_stores=0;\nstruct GuestMemory {')
            code=code.replace('load_parallel_impl(std::uint64_t address,unsigned size){','load_parallel_impl(std::uint64_t address,unsigned size){++par12_slow_loads;')
            code=code.replace('store_parallel_impl(std::uint64_t address,unsigned size,std::uint64_t value){','store_parallel_impl(std::uint64_t address,unsigned size,std::uint64_t value){++par12_slow_stores;')
            code=code.replace('store_scalar_admitted(const ParallelMemory::Mapping& map,std::uint64_t address,unsigned size,std::uint64_t value){','store_scalar_admitted(const ParallelMemory::Mapping& map,std::uint64_t address,unsigned size,std::uint64_t value){++par12_slow_stores;')
        cpp=ARTIFACTS/(name+'.cpp');cpp.write_text(code)
        p=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(cpp.with_suffix(''))],capture_output=True,timeout=300)
        cpp.with_suffix('.build.log').write_bytes(p.stdout+p.stderr);self.assertEqual(p.returncode,0,p.stderr.decode()[-12000:])
        p=subprocess.run([str(cpp.with_suffix(''))],capture_output=True,timeout=90,env={**os.environ,'ANYPS3_SPU_THREADS':'0','ANYPS3_SPU_WORKER':'0','ANYPS3_RSX_WORKER':'0'})
        cpp.with_suffix('.run.log').write_bytes(p.stdout+p.stderr);self.assertEqual(p.returncode,0,p.stderr.decode()[-12000:])

    def test_cached_widths_alias_be_and_tracked_slow_fallback(self):
        self.native('inline-widths',r'''
 GuestMemory m;m.map(0x10000,0x4000,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});
 auto backing=m.regions[0].bytes;m.host_access_observer=[](const void*,std::uint64_t,bool){assert(false);};
 m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 // Cold translation warms the cache; hot code must not call outlined helpers.
 m.store(0x10000,8,0);m.store(0x20000,8,0);
 const auto loads=par12_slow_loads,stores=par12_slow_stores;
 for(unsigned n=0;n<128;++n){
  m.store(0x10000,8,UINT64_C(0x1122334455667788));assert(m.load(0x20000,8)==UINT64_C(0x1122334455667788));
  m.store(0x20001,1,0xaa);assert(m.load(0x10001,1)==0xaa);
  m.store(0x10002,2,0xbbcc);assert(m.load(0x20002,2)==0xbbcc);
  m.store(0x20004,4,0xddeeff00);assert(m.load(0x10004,4)==0xddeeff00);
 }
 assert(par12_slow_loads==loads && par12_slow_stores==stores);
 const std::uint8_t expected[]={0x11,0xaa,0xbb,0xcc,0xdd,0xee,0xff,0};
 for(unsigned n=0;n<8;++n)assert(backing->coherent_data()[n]==expected[n]);
 std::pair<std::uintptr_t,std::uint64_t> token;m.load_reserved(0x10000,4,token);
 const auto tracked=par12_slow_stores;m.store(0x20001,1,0xaa);
 assert(par12_slow_stores>tracked && !m.store_conditional(0x10000,4,0,token));
 // Both unaligned and cross-page accesses keep their original byte semantics.
 m.store(0x10ffd,8,UINT64_C(0x0123456789abcdef));assert(m.load(0x20ffd,8)==UINT64_C(0x0123456789abcdef));
 m.renderer_enabled=false;
''',instrument=True)

    def test_scalar_watch_bits_boundaries_and_dynamic_publication(self):
        self.native('scalar-bits',r'''
 alignas(4096) std::array<std::uint8_t,8192> bytes{};auto* p=bytes.data();
 GuestMemory::HostObserver observer;observer=[](const void*,std::uint64_t,bool){};
 observer.filtered=true;observer.supports_exact_bytes=true;
 observer.watch(p+63,2);observer.watch_poll(p+127,2);observer.watch(p+4095,1);
 for(unsigned n=1;n<=8;++n)for(unsigned offset=0;offset+n<=4096;++offset){
  const bool synchronous=(offset<65 && offset+n>63) || (offset<=4095 && offset+n>4095);
  const bool all=synchronous || (offset<129 && offset+n>127);
  assert(observer.scalar_interested(p+offset,n,true)==synchronous);
  assert(observer.scalar_interested(p+offset,n,false)==all);
 }
 // Warm negative cells cannot hide a subsequent watch on the same page.
 for(unsigned n=0;n<1000;++n){assert(!observer.scalar_interested(p+256,8,true));
  observer.watch(p+263,1);assert(observer.scalar_interested(p+256,8,true));observer.watch(p+263,1,false);}
 observer.supports_exact_bytes=false;assert(observer.scalar_interested(p+256,8,true));
''')

    def test_inline_cache_revocation_permissions_overflow_and_observer(self):
        self.native('inline-revocation',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.map(0xfffff000,4096,6,nullptr,0);
 m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 m.store(0x10000,4,0x11223344);assert(m.load(0x10000,4)==0x11223344);
 unsigned calls=0;auto backing=m.regions[0].bytes;
 m.renderer_barrier=[](bool){};m.renderer_access=[&](const void*,std::uint64_t,bool){++calls;};
 {GuestMemory::RawPermit raw(m);m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(backing->data(),4);}
 assert(m.load(0x10000,4)==0x11223344 && calls>0);
 {GuestMemory::RawPermit raw(m);m.host_access_observer={};m.regions[0].flags=4;m.mapping_changed();}
 fault([&]{m.store(0x10000,4,0);});assert(m.load(0x10000,4)==0x11223344);
 fault([&]{m.load(UINT64_C(0x100000000),1);});fault([&]{m.store(0xffffffff,2,0);});
 fault([&]{m.load(0x11fff,4);});
 {GuestMemory::RawPermit raw(m);m.regions.erase(m.regions.begin());m.mapping_changed();}
 m.map(0x10000,8192,6,nullptr,0);assert(m.load(0x10000,4)==0);
 m.store(0x10000,4,0xaabbccdd);assert(backing->coherent_data()[0]==0x11);
 m.renderer_enabled=false;
''')

    def test_empty_coordinator_does_not_acquire_held_queue_mutexes(self):
        self.native('idle-locks',r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","1",1);
 PpuState s;s.memory.map(0x10000,4096,6,nullptr,0);SpuWorkerSession life(s);
 auto& q=*s.process.rsx_worker.queue;auto& mem=*s.memory.parallel;
 // Stop owner service at an acknowledged point, so locks can be held without
 // a fixture lock-order inversion involving the live owner.
 std::atomic<bool> entered{false},release{false};
 auto r=q.post(RsxOwnerQueue::Kind::HostAccess,[&]{entered=true;while(!release.load())std::this_thread::yield();});
 while(!entered.load())std::this_thread::yield();
 {std::unique_lock<std::mutex> a(q.mutex);std::unique_lock<std::mutex> b(mem.queue_mutex);
  for(unsigned n=0;n<10000;++n){q.check();q.pump_coordinator();s.memory.service_coherence();rsx_worker_pump(s);SpuServiceScope service(s,true);}
 }
 release=true;q.await(r);life.finish();
''',full=True)

    def test_fifo_doorbell_budget_retry_configuration_and_idle_deadline(self):
        self.native('fifo-doorbell',r'''
 setenv("ANYPS3_RSX_WORKER","1",1);PpuState s;auto& m=s.memory;
 m.map(0x10000,0x10000,6,nullptr,0);m.map(0x60000000,0x400000,6,nullptr,0);
 auto& g=s.rsx;g.dma=0x60100000;g.io[0]=0x10000;g.fifo_enabled=true;
 // Two fixed NOP packets. A budget of one must leave the second for retry.
 m.store(0x10000,4,0);m.store(0x10004,4,0);m.store(g.dma+0x40,4,8);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;auto& q=*w.queue;
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{rsx_worker_publish(s);});
 auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(m.load(g.dma+0x44,4)!=8){rsx_run(s,1);assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}
 q.drain(); // Last completion retires fifo_queued before checking idle.
 rsx_run(s,1);q.drain();std::uint64_t sequence;
 {std::lock_guard<std::mutex> lock(q.mutex);sequence=q.next_sequence;}
 for(unsigned n=0;n<10000;++n)rsx_run(s,1);
 {std::lock_guard<std::mutex> lock(q.mutex);assert(q.next_sequence==sequence);}
 // New PUT plus one outstanding tick, no duplicate tick allocation.
 std::atomic<bool> entered{false},release{false};
 auto block=q.post(RsxOwnerQueue::Kind::HostAccess,[&]{entered=true;while(!release.load())std::this_thread::yield();});
 while(!entered.load()){std::this_thread::yield();}
 m.store(g.dma+0x40,4,12);rsx_run(s,1);
 {std::lock_guard<std::mutex> lock(q.mutex);sequence=q.next_sequence;}
 for(unsigned n=0;n<1000;++n)rsx_run(s,1);
 {std::lock_guard<std::mutex> lock(q.mutex);assert(q.next_sequence==sequence);}
 release=true;q.await(block);
 until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(m.load(g.dma+0x44,4)!=12){rsx_run(s,1);assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}
 // Configuration disables the published address before unmapping. No stale
 // old control address may be loaded during a main-thread map transaction.
 {GuestMemory::ConfigurationPermit config(m);GuestMemory::RawPermit raw(m);
  assert(w.fifo_put_address.load()==0);g.fifo_enabled=false;}
 q.drain();
 {std::unique_lock<std::mutex> lock(q.mutex);sequence=q.next_sequence;}
 for(unsigned n=0;n<1000;++n)rsx_run(s,1);
 {std::unique_lock<std::mutex> lock(q.mutex);assert(q.next_sequence==sequence);}
 life.finish();
''',full=True)
