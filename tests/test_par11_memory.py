"""PAR11 atomic watch publication, deferred writes and completion ordering."""
from pathlib import Path
import os,shutil,subprocess,unittest
from tests.test_par1 import source

ARTIFACTS=Path('local/ratchet/codex/par11/tests')

@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class Parallel11Memory(unittest.TestCase):
    def native(self,name,body,*,full=False):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        cpp=ARTIFACTS/(name+'.cpp');cpp.write_text(source(body,full=full))
        p=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(cpp.with_suffix(''))],capture_output=True,timeout=300)
        cpp.with_suffix('.build.log').write_bytes(p.stdout+p.stderr)
        self.assertEqual(p.returncode,0,p.stderr.decode()[-12000:])
        p=subprocess.run([str(cpp.with_suffix(''))],capture_output=True,timeout=90,env={**os.environ,'ANYPS3_SPU_THREADS':'0','ANYPS3_SPU_WORKER':'0','ANYPS3_RSX_WORKER':'0'})
        cpp.with_suffix('.run.log').write_bytes(p.stdout+p.stderr)
        self.assertEqual(p.returncode,0,p.stderr.decode()[-12000:])

    def test_watch_churn_never_closes_gate_with_live_reader(self):
        self.native('watch-churn',r'''
 GuestMemory m;m.map(0x10000,0x8000,6,nullptr,0);auto pin=m.regions[0].bytes;
 m.host_access_observer=[](const void*,std::uint64_t,bool){assert(false);};
 m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 std::atomic<bool> entered{false},published{false};
 std::thread rsx([&]{m.renderer_thread=std::this_thread::get_id();entered=true;
  while(!published.load())std::this_thread::yield();
  for(unsigned n=0;n<2000;++n){m.renderer_metadata([&]{
   assert(!m.parallel->exclusive.load());auto& observer=m.host_access_observer;
   observer.watch(pin->data()+4094,4);observer.watch(pin->data()+4095,1);
   observer.watch(pin->data()+4094,4,false);observer.watch(pin->data()+4095,1,false);
  });}
  published=false;
 });
 while(!entered.load())std::this_thread::yield();
 {GuestMemory::Lease lease(m,0x17000,4,false);published=true;
  const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(10);
  while(published.load()){assert(!m.parallel->exclusive.load());assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}}
 rsx.join();assert(m.parallel->raw_generation.load()==1);m.renderer_enabled=false;
''')

    def test_exact_poll_bitmap_alias_overlap_and_lifetime(self):
        self.native('bitmap',r'''
 GuestMemory::HostObserver o;o=[](const void*,std::uint64_t,bool){};o.filtered=true;o.supports_exact_bytes=true;
 alignas(4096) std::array<std::uint8_t,8192> data{};auto* p=data.data();
 // Fixed boundary probes, repeated/overlapping registrations and raw fallback.
 o.watch_poll(p+63,3);o.watch(p+64,1);o.watch(p+64,1);
 {HostByteAccess bytes(true);assert(o.interested(p+63,1));assert(!o.synchronous(p+63,1));assert(o.synchronous(p+64,1));
  o.watch(p+64,1,false);assert(o.synchronous(p+64,1));o.watch(p+64,1,false);assert(!o.synchronous(p+64,1));
  assert(!o.interested(p+62,1) && !o.interested(p+66,1));o.watch_poll(p+63,3,false);assert(!o.interested(p+63,3));}
 o.watch(p+4094,4);auto copy=o;o.watch(p+4094,4,false);
 {HostByteAccess bytes(true);assert(!o.interested(p+4095,2));assert(copy.interested(p+4095,2));}
 o.watch_poll(p+16,4);assert(o.synchronous(p+256,1));
 o=[](const void*,std::uint64_t,bool){};o.filtered=true;o.supports_exact_bytes=true;
 {HostByteAccess bytes(true);assert(!o.interested(p+16,4));o.watch(p+16,4);assert(o.synchronous(p+16,4));}
 fault([&]{o.watch(reinterpret_cast<void*>(UINTPTR_MAX-1),4);});
''')

    def test_dirty_registration_churn_with_cpu_and_gpu_publication(self):
        self.native('dirty-churn',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 auto range=m.range(0x10000,8,true);auto pin=m.regions[0].bytes;m.renderer_enabled=true;
 std::atomic<bool> ready{false},done{false};std::atomic<unsigned> writes{0};
 std::thread rsx([&]{m.renderer_thread=std::this_thread::get_id();ready=true;
  for(unsigned i=0;i<2000;++i){auto watch=m.watch_cpu_dirty(range);assert(watch->consume());
   if(i%2==0){auto previous=writes.load();const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(10);
    while(writes.load()<previous+2){assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}
    assert(watch->consume());}
   m.unwatch_cpu_dirty(watch);
  }done=true;
 });
 while(!ready.load())std::this_thread::yield();
 while(!done.load()){m.store(0x10000,8,UINT64_C(0x1122334455667788));++writes;}
 rsx.join();assert(!m.parallel->exclusive.load());assert(m.load(0x10000,8)==UINT64_C(0x1122334455667788));
 auto token=m.watch_cpu_dirty(range);assert(token->consume());
 {GuestMemory::GpuPages claim(m,{{pin->data(),8,pin}});const std::uint8_t bytes[]={0x88,0x77,0x66,0x55,0x44,0x33,0x22,0x11};claim.commit(range,0,bytes,8);}
 assert(token->consume());assert(m.load(0x10000,8)==UINT64_C(0x8877665544332211));
 m.unwatch_cpu_dirty(token);fault([&]{m.unwatch_cpu_dirty(token);});m.renderer_enabled=false;
''')

    def test_polling_completion_and_dirty_store_do_not_rpc(self):
        self.native('poll-dirty',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});auto pin=m.regions[0].bytes;
 m.host_access_observer=[](const void*,std::uint64_t,bool){assert(false);};m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 m.enable_concurrency();m.enable_page_broker();
 auto label=m.range(0x10040,4,true);auto input=m.range(0x10000,4,true);auto dirty=m.watch_cpu_dirty(input);assert(dirty->consume());
 m.host_access_observer.watch_poll(pin->data()+64,4);
 m.renderer_enabled=true;m.renderer_access=[](const void*,std::uint64_t,bool){assert(false);};
 // Owner can be indefinitely occupied: no queue progress is needed for these writes.
 m.store(0x20000,4,0x11223344);m.store(0x20040,4,0x55667788);
 assert(dirty->consume());assert(!dirty->consume());assert(m.load(0x10040,4)==0x55667788);
 std::atomic<bool> commit{false},done{false};
 std::thread gpu([&]{m.renderer_thread=std::this_thread::get_id();m.page_broker.delegated_owner=m.renderer_thread;while(!commit.load())std::this_thread::yield();
  {m.local_state().renderer_claim=true;GuestMemory::GpuPages claim(m,{{pin->data()+64,4,pin}});const std::uint8_t bytes[]={0xaa,0xbb,0xcc,0xdd};claim.commit(label,0,bytes,4);}done=true;});
 for(unsigned n=0;n<10000;++n)assert(m.load(0x10040,4)==0x55667788);
 commit=true;while(!done.load())std::this_thread::yield();gpu.join();assert(m.load(0x20040,4)==0xaabbccdd);
 m.unwatch_cpu_dirty(dirty);m.renderer_enabled=false;
''')

    def test_synchronous_gpu_watch_overrides_poll_and_dirty(self):
        self.native('sync-overlap',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);auto pin=m.regions[0].bytes;unsigned calls=0;
 m.host_access_observer=[](const void*,std::uint64_t,bool){};m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 m.host_access_observer.watch_poll(pin->data()+64,4);m.host_access_observer.watch(pin->data()+65,1);
 m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;
 m.renderer_access=[&](const void* p,std::uint64_t n,bool){assert(p==pin->data()+65 && n==1);++calls;};
 m.store(0x10040,1,0x11);assert(calls==0);m.store(0x10041,1,0x22);assert(calls==1);
 assert(m.load(0x10040,1)==0x11);assert(m.load(0x10041,1)==0x22 && calls==2);m.renderer_enabled=false;
''')

    def test_batched_dirty_ranges_preserve_holes_aliases_and_stale_map(self):
        self.native('dirty-batch',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});m.enable_concurrency();m.enable_page_broker();
 auto a=m.range(0x10000,32,true),b=m.range(0x10020,32,true),c=m.range(0x11000,32,true);
 auto token=m.watch_cpu_dirty(std::vector<GuestMemory::Range>{c,a,b,a});assert(token->spans.size()==2 && token->consume());
 m.store(0x20040,4,0x11223344);assert(!token->consume());m.store(0x2003f,1,0xa5);assert(token->consume());
 m.store(0x21000,4,0x55667788);assert(token->consume());
 m.map(0x30000,4096,6,nullptr,0);assert(m.snapshot(c,0,4)==std::vector<std::uint8_t>({0x55,0x66,0x77,0x88}));
 {GuestMemory::RawPermit raw(m);m.regions.erase(m.regions.begin());m.mapping_changed();}
 fault([&]{m.watch_cpu_dirty(a);});m.unwatch_cpu_dirty(token);
 fault([&]{m.watch_cpu_dirty(std::vector<GuestMemory::Range>{});});
''')

    def test_raw_kernel_buffer_preflight_uses_dirty_registry_on_real_owner(self):
        self.native('raw-kernel',r'''
#if defined(__linux__)
 setenv("ANYPS3_RSX_WORKER","1",1);PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);
 auto pin=m.regions[0].bytes;auto range=m.range(0x10000,4,true);GuestMemory::HostGuard guard(false);
 unsigned callbacks=0;SpuWorkerSession life(s);auto& owner=s.process.rsx_worker;
 std::shared_ptr<GuestMemory::CpuDirtyWatch> dirty;
 owner.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{
  {GuestMemory::RawPermit raw(m);m.host_access_observer=[&](const void* p,std::uint64_t n,bool write){assert(write && owner.on_owner());++callbacks;guard.release(p,n);};
   m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;}
  dirty=m.watch_cpu_dirty(range);assert(dirty->consume());assert(m.host_access_observer.pages.empty());
  assert(guard.add(pin->data(),4,*pin,true));guard.arm();
 });
 m.store(0x10000,4,0x11223344);assert(callbacks==0 && dirty->consume());
 int descriptors[2];assert(!pipe(descriptors));const std::uint8_t bytes[]={0xaa,0xbb,0xcc,0xdd};assert(write(descriptors[1],bytes,4)==4);
 {GuestMemory::RawPermit raw(m);auto* pointer=m.access(0x10000,4,true);assert(callbacks==1);assert(read(descriptors[0],pointer,4)==4);}
 assert(m.load(0x10000,4)==0xaabbccdd);close(descriptors[0]);close(descriptors[1]);
 owner.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{m.unwatch_cpu_dirty(dirty);guard.disarm();});life.finish();
#endif
''',full=True)

    def test_cpu_dirty_payload_ignores_shared_snapshot_publication_lock(self):
        self.native('registry-no-lock',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 auto token=m.watch_cpu_dirty(m.range(0x10000,4,true));assert(token->consume());
#ifdef __GLIBCXX__
 // Hold the actual libstdc++ shared_ptr publication lock. A scalar reader
 // must use its immutable list hazard rather than the GPU snapshot lock.
 {std::_Sp_locker held(&m.gpu_dirty_watches);m.store(0x10000,4,0x11223344);}
#else
 m.store(0x10000,4,0x11223344);
#endif
 assert(token->consume());assert(m.load(0x10000,4)==0x11223344);m.unwatch_cpu_dirty(token);
''')
