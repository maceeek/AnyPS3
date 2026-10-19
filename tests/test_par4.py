"""PAR4 independent native reservation, parking, origin and byte-ownership oracles."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source
from anyps3.rsx_worker import TYPES as RSX_OWNER_TYPES

ARTIFACTS=Path('local/ratchet/codex/par4/tests')

@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class Parallel4Contracts(unittest.TestCase):
    def native(self,name,body,*,full=False,words=(),transform=None):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        code=source(body,full=full,words=words)
        if not full:code=code.replace('int main(){(void)crc32;',RSX_OWNER_TYPES+'\nint main(){(void)crc32;')
        if transform:code=transform(code)
        cpp=ARTIFACTS/(name+'.cpp');exe=cpp.with_suffix('');cpp.write_text(code)
        flags=os.environ.get('ANYPS3_PAR4_CXXFLAGS','-O2').split()
        build=subprocess.run(['c++','-std=c++17',*flags,'-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(exe)],capture_output=True,text=True,timeout=300)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode,0,build.stderr[-10000:])
        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=45,env={**os.environ,'ANYPS3_SPU_THREADS':'0','ANYPS3_SPU_WORKER':'0','ANYPS3_PERF_REPORT':'0'})
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode,0,run.stderr[-10000:])

    def test_reservation_notifications_all_writers_alias_and_unsubscribe(self):
        self.native('notifications',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});m.enable_concurrency();
 std::uint8_t bytes[128]{};std::pair<std::uintptr_t,std::uint64_t> token;m.getllar(0x10000,bytes,token);
 std::atomic<unsigned> wakes{0};auto watch=std::make_unique<GuestMemory::ReservationWatch>(m.parallel->notifications,token.first,[&]{++wakes;});
 m.store(0x20080,4,1);assert(wakes==0); // Same page, different reservation line.
 m.store(0x20000,4,0);assert(wakes==1); // Same-value store must notify.
 m.transfer(0x2007f,bytes,1,true);assert(wakes==2);
 m.getllar(0x10000,bytes,token);assert(m.putllc(0x20000,bytes,token));assert(wakes==3);
 assert(!m.putllc(0x10000,bytes,token));assert(wakes==3); // Failed store changes nothing.
 assert(m.putllc(0x10000,bytes,{},true));assert(wakes==4);
 m.load_reserved(0x10000,4,token);assert(m.store_conditional(0x20000,4,0x11223344,token));assert(wakes==5);
 {GuestMemory::RawPermit raw(m);m.mark_write(m.regions[0].bytes->data(),4);}assert(wakes==6);
 m.mapping_changed();assert(wakes==7); // Invalidated maps wake a parked reader.
 watch.reset();m.store(0x10000,4,7);assert(wakes==7 && m.parallel->notifications->count==0);
''')

    def test_arm_recheck_prevents_lost_wakeup_and_lane_has_no_idle_poll(self):
        self.native('parking',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 NativeSpuWorker lane;std::atomic<unsigned> attempts{0};std::atomic<bool> arm{false},done{false};
 std::uint8_t bytes[128]{};std::pair<std::uintptr_t,std::uint64_t> token;m.getllar(0x10000,bytes,token);
 lane.step=[&](unsigned){lane.event_driven=true;++attempts;lane.watch_reservation(m,token.first);arm=true;
  if(m.reservation_token(0x10000)!=token){done=true;}return false;};lane.launch();
 while(!arm.load())std::this_thread::yield();
 // Wait for the lane to finish its first step and enter CV sleep.
 {std::unique_lock<std::mutex> l(lane.mutex);lane.changed.wait_for(l,std::chrono::milliseconds(20),[&]{return lane.completed.load()>=1;});}
 const auto before=attempts.load();clockid_t clock;assert(!pthread_getcpuclockid(lane.thread.native_handle(),&clock));
 timespec first{},last{};assert(!clock_gettime(clock,&first));const auto idle_start=std::chrono::steady_clock::now();
 std::this_thread::sleep_for(std::chrono::milliseconds(200));assert(!clock_gettime(clock,&last));assert(attempts==before);
 const auto cpu_ns=(last.tv_sec-first.tv_sec)*UINT64_C(1000000000)+last.tv_nsec-first.tv_nsec;
 const auto wall_ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-idle_start).count();
 std::printf("IDLE_LANE cpu_ns=%llu wall_ns=%llu attempts=%u\n",(unsigned long long)cpu_ns,(unsigned long long)wall_ns,before);
 assert(cpu_ns<5000000);
 m.store(0x10000,4,0x11223344);
 const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
 while(!done.load() && std::chrono::steady_clock::now()<deadline){std::this_thread::yield();}assert(done);
 lane.shutdown();assert(!lane.reservation_watch && m.parallel->notifications->count==0);
 // A write before subscribe is detected by the predicate, without a wake.
 auto old=m.reservation_token(0x10000);m.store(0x10000,4,0x11223344);
 GuestMemory::ReservationWatch late(m.parallel->notifications,old.first,[]{});assert(m.reservation_token(0x10000)!=old);
''',full=True)

    def test_pool_tick_does_not_wake_parked_lane(self):
        self.native('tick',r'''
 NativeSpuWorker pool;pool.enabled=true;pool.pool_mode=true;
 auto p=std::make_unique<NativeSpuWorker>();auto& lane=*p;lane.group_id=1;std::atomic<unsigned> calls{0};
 lane.step=[&](unsigned){lane.event_driven=true;++calls;return false;};pool.lanes.push_back(std::move(p));lane.launch();
 while(!calls.load()){std::this_thread::yield();}std::this_thread::sleep_for(std::chrono::milliseconds(20));const auto before=calls.load();
 for(unsigned n=0;n<10000;++n)pool.request(1024);
 std::this_thread::sleep_for(std::chrono::milliseconds(30));assert(calls==before);pool.shutdown();
''',full=True)

    def test_aot_event_lr_snr_mailbox_and_decrementer_wakeup(self):
        # rdch r3,SPU_RdEventStat; STOP thread exit. The outbound status is
        # fixture setup, while the event value must be produced by real AOT.
        self.native('aot-events',r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 const std::uint32_t words[]={0x01a00003,0x00000102};
 auto run=[&](unsigned kind){
  NativeSpu::Group g;g.id=7;g.count=1;g.state=6;g.scheduled=true;g.context.resize(0x40000);g.threads.emplace_back();
  auto& initial=g.threads[0];initial.entry=0x100;initial.id=0x123;
  for(unsigned i=0;i<2;++i)for(unsigned b=0;b<4;++b)g.context[0x100+i*4+b]=words[i]>>(24-8*b);
  s.spu.groups.push_back(std::move(g));auto& group=s.spu.groups.back();auto& t=group.threads[0];
  spu_prepare(s,t,group.context.data());t.decrementer_running=false;t.out_mailbox=0;t.out_mailbox_full=true;
  t.event_mask=kind;
  if(kind==0x400){std::uint8_t bytes[128]{};m.getllar(0x10000,bytes,t.reservation);t.reservation_address=0x10000;}
  if(kind==0x20){t.decrementer=7980000;t.decrementer_tick=timebase_ticks();t.decrementer_running=true;}
  NativeSpuWorker lane;lane.context=&t;lane.group=&group;lane.step=[&](unsigned budget){return spu_run_thread(s,group,0,budget);};
  t.in_mailbox.set_wakeup([&]{lane.wake();});for(auto& snr:t.snr)snr.set_wakeup([&]{lane.wake();});lane.launch();
  if(kind!=0x20){
   const auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(2);
   while(lane.completed<1 && std::chrono::steady_clock::now()<limit){std::this_thread::yield();}
   assert(lane.completed>=1);const auto attempts=lane.completed.load();std::this_thread::sleep_for(std::chrono::milliseconds(25));assert(lane.completed==attempts);
   if(kind==0x400)m.store(0x10000,4,0x11223344);
   else if(kind==0x200)t.snr[0].push(0,false);
   else if(kind==0x100)t.snr[1].push(0xabcdef01,false);
   else t.in_mailbox.push_back(0x55);
  }
  const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
  while(group.state==6 && std::chrono::steady_clock::now()<deadline){std::this_thread::yield();}
  lane.pause();lane.check();assert(t.stopped && spu_word(t.gpr[3],0)==kind);lane.shutdown();
  t.in_mailbox.set_wakeup({});for(auto& snr:t.snr)snr.set_wakeup({});s.spu.groups.clear();
 };
 for(unsigned kind:{0x400u,0x200u,0x100u,0x10u,0x20u})run(kind);
''',full=True,words=(0x01a00003,0x00000102))

    def test_origin_survives_memory_rpc_and_nested_context(self):
        self.native('origin',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 std::atomic<bool> done{false};std::vector<HostAccessOrigin> origins;
 m.host_access_observer=[&](const void*,std::uint64_t,bool){origins.push_back(HostAccessContext::sample());};
 std::thread producer([&]{
  std::uint32_t id=0x123,pc=0x456;std::uint64_t sequence=7;
  HostAccessContext spu(HostAccessOrigin::Engine::Spu,id,pc,&sequence);
  m.store(0x10000,4,0x11223344);pc=0x45a;sequence=8;
  {auto value=HostAccessContext::sample();value.engine=HostAccessOrigin::Engine::Dma;HostAccessContext dma(value);m.store(0x10004,4,0x55667788);}
  pc=0x45e;m.store(0x10008,4,0xaabbccdd);done=true;
 });
 while(!done.load()){m.service_coherence();std::this_thread::yield();}producer.join();
 assert(origins.size()==3);
 assert(origins[0].engine==HostAccessOrigin::Engine::Spu && origins[0].guest_id==0x123 && origins[0].guest_pc==0x456 && origins[0].operation_id==7 && origins[0].ea==0x10000);
 assert(origins[1].engine==HostAccessOrigin::Engine::Dma && origins[1].guest_pc==0x45a && origins[1].ea==0x10004);
 assert(origins[2].engine==HostAccessOrigin::Engine::Spu && origins[2].guest_pc==0x45e && origins[2].ea==0x10008);
 assert(HostAccessContext::sample().engine==HostAccessOrigin::Engine::Host);
 {std::uint32_t tid=4,pc=0x120;std::uint64_t retired=100;unsigned in_block=7;
  HostAccessContext block(HostAccessOrigin::Engine::Spu,tid,pc,&retired,&in_block);assert(HostAccessContext::sample().operation_id==107);
  in_block=8;pc=0x124;assert(HostAccessContext::sample().operation_id==108 && HostAccessContext::sample().guest_pc==0x124);}
''')

    def test_transferred_gpu_claim_exact_commit_snapshot_and_be_fetch_or(self):
        self.native('gpu-commit',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});m.enable_concurrency();m.enable_page_broker();
 GuestMemory::Range range;std::vector<GuestMemory::HostSpan> spans;
 {GuestMemory::RawPermit raw(m);range=m.range(0x10000,8,true);spans.push_back({range.parts[0].data,8,range.parts[0].owner});}
 auto before=m.reservation_token(0x10000);
 auto claim=std::make_unique<GuestMemory::GpuPages>(m,spans);
 std::atomic<unsigned> notifications{0};GuestMemory::ReservationWatch watch(m.parallel->notifications,before.first,[&]{++notifications;});
 std::thread rsx([&,owned=std::move(claim)]()mutable{
  fault([&]{owned->commit(range,0,reinterpret_cast<const std::uint8_t*>("abcd"),4);});
  owned->adopt();const std::uint8_t bytes[]={0x11,0x22,0x33,0x44,0xaa,0xbb,0xcc,0xdd};owned->commit(range,0,bytes,8);
  assert(owned->fetch_or32(range,0,0x80000080)==0x11223344);
  assert(owned->snapshot(range,0,8)==std::vector<std::uint8_t>({0x91,0x22,0x33,0xc4,0xaa,0xbb,0xcc,0xdd}));
  fault([&]{owned->pointer(0);});
  auto outside=range;outside.address+=8;outside.parts[0].data+=8;
  fault([&]{owned->commit(outside,0,bytes,4);});
  auto bad=range;bad.writable=false;fault([&]{owned->commit(bad,0,bytes,4);});
  auto stale=range;stale.epoch=0;stale.parts[0].mapping_id+=1;fault([&]{owned->commit(stale,0,bytes,4);});
  assert(notifications==2);owned.reset();
 });rsx.join();
 assert(m.load(0x20000,4)==0x912233c4 && m.load(0x10004,4)==0xaabbccdd);
 assert(before!=m.reservation_token(0x10000) && m.page_broker.owners==0);
''')

    def test_rsx_owner_fifo_label_flip_parity_and_coordinator_affinity(self):
        self.native('rsx-owner-parity',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();
 std::vector<unsigned> serial,parallel;const auto main_tid=std::this_thread::get_id();
 for(unsigned i=0;i<128;++i){const unsigned word=(i*17)^0x12345678;serial.push_back(word);m.store(0x10100+i*4,4,word);}
 unsigned get=0,flips=0;bool destroyed_on_owner=false;RsxOwnerQueue q;
 q.run_fifo=[&](unsigned budget){assert(std::this_thread::get_id()!=main_tid);
  const unsigned put=m.load(0x10040,4);
  for(unsigned n=0;n<budget && get<put;++n){const unsigned word=m.load(0x10100+get*4,4);parallel.push_back(word);
   if((get%16)==15){m.store(0x10080,4,word);++flips;q.owner_call([&]{assert(std::this_thread::get_id()==main_tid);});}
   ++get;m.store(0x10044,4,get);}
 };
 q.idle_teardown=[&]{destroyed_on_owner=std::this_thread::get_id()!=main_tid;};q.launch();
 for(unsigned i=1;i<=128;++i){m.store(0x10040,4,i);q.post(RsxOwnerQueue::Kind::RunFifo,{},128);}
 q.drain();assert(serial==parallel && flips==8 && m.load(0x10044,4)==128 && m.load(0x10080,4)==((127*17)^0x12345678));
 q.call(RsxOwnerQueue::Kind::MapBarrier,[&]{assert(q.current==&q);});
 q.shutdown();assert(destroyed_on_owner && q.stopped && !q.error && q.completed_sequence==q.next_sequence);
''')

    def test_rsx_owner_first_error_poison_and_shutdown_waiters(self):
        self.native('rsx-owner-errors',r'''
 RsxOwnerQueue q;std::atomic<bool> entered{false},go{false};std::atomic<unsigned> later{0};q.launch();
 HostAccessOrigin origin;origin.engine=HostAccessOrigin::Engine::Dma;origin.guest_id=9;origin.guest_pc=0x678;HostAccessContext context(origin);
 auto first=q.post(RsxOwnerQueue::Kind::HostAccess,[&]{entered=true;while(!go.load()){std::this_thread::yield();}throw std::runtime_error("fixed RSX poison");});
 while(!entered.load()){std::this_thread::yield();}
 std::vector<std::shared_ptr<RsxOwnerQueue::Request>> pending;for(unsigned i=0;i<64;++i)pending.push_back(q.post(RsxOwnerQueue::Kind::HostAccess,[&]{++later;}));
 {std::lock_guard<std::mutex> l(q.mutex);assert(q.queue.size()==64);}
 std::thread release([&]{go=true;});fault([&]{q.await(first);});release.join();
 for(const auto& p:pending)fault([&]{q.await(p);});
 fault([&]{q.call(RsxOwnerQueue::Kind::HostAccess,[&]{++later;});});q.shutdown();
 assert(later==0 && q.failed_sequence==1 && q.failed_origin.engine==HostAccessOrigin::Engine::Dma && q.failed_origin.guest_id==9 && q.failed_origin.guest_pc==0x678);
 try{std::rethrow_exception(q.error);}catch(const std::runtime_error& e){assert(std::string(e.what())=="fixed RSX poison");}
 // Cancellation also wakes an owner waiting on the coordinator; no callback
 // may outlive its captured stack when shutdown joins the owner.
 RsxOwnerQueue cancelled;cancelled.launch();auto request=cancelled.post(RsxOwnerQueue::Kind::HostAccess,[&]{cancelled.owner_call([&]{++later;});});
 const auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(2);
 for(;;){bool ready;{std::lock_guard<std::mutex> l(cancelled.mutex);ready=!cancelled.coordinator.empty();}if(ready)break;assert(std::chrono::steady_clock::now()<limit);std::this_thread::yield();}
 cancelled.shutdown();fault([&]{cancelled.await(request);});assert(later==0);
''')

    def test_channel_event_empty_to_nonempty_edges(self):
        self.native('channel-edges',r'''
 PpuState s;NativeSpu::Thread t;t.id=1;t.event_mask=0x310;t.decrementer_running=false;
 t.snr[0].push(0,false);spu_collect_events(s,t);assert(t.event_ready && t.event_status==0x200);
 t.event_status=0;t.event_ready=false;t.snr[0].push(1,false);spu_collect_events(s,t);assert(!t.event_ready && t.event_status==0);
 std::uint32_t value=0;assert(t.snr[0].pop(value) && value==1);
 t.snr[0].push(2,true);spu_collect_events(s,t);assert(t.event_ready && t.event_status==0x200);
 t.event_status=0;t.event_ready=false;t.in_mailbox.push_back(0);spu_collect_events(s,t);assert(t.event_ready && t.event_status==0x10);
 t.event_status=0;t.event_ready=false;t.in_mailbox.push_overwrite(1);spu_collect_events(s,t);assert(!t.event_ready && t.event_status==0);
 assert(t.in_mailbox.pop(value) && value==0);assert(t.in_mailbox.pop(value) && value==1);
 t.in_mailbox.push_overwrite(2);spu_collect_events(s,t);assert(t.event_ready && t.event_status==0x10);
''',full=True)

    def test_ordinary_guard_without_exact_observer_is_not_bypassed(self):
        self.native('raw-only-guard',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);auto& bytes=*m.regions[0].bytes;
 GuestMemory::HostGuard guard;volatile sig_atomic_t calls=0;
 guard.synchronize=[&]{++calls;bytes[0]=0xaa;};assert(guard.add(bytes.data(),4,bytes));guard.arm();
 assert(m.load(0x10000,1)==0xaa && calls==1);
''')

    def test_rsx_owner_graceful_stop_and_recursive_submission_stop(self):
        self.native('rsx-owner-stop',r'''
 RsxOwnerQueue q;q.launch();unsigned count=0;
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{++count;fault([&]{q.post(RsxOwnerQueue::Kind::Drain);});});
 q.await(q.post(RsxOwnerQueue::Kind::Stop,[&]{++count;}));q.shutdown();
 assert(count==2 && q.stopped && !q.error);fault([&]{q.post(RsxOwnerQueue::Kind::RunFifo,{},1);});
''')

    def test_serial_scalar_cache_still_validates_maps_observers_and_reservations(self):
        self.native('scalar-cache',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.map(0x11000,4,6,nullptr,0);
 m.store(0x10000,4,0x11223344);assert(m.load(0x10000,4)==0x11223344);
 for(unsigned i=0;i<8;++i){m.store(0x10001,2,0xaabb);assert(m.load(0x10000,4)==0x11aabb44);}
 unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};
 assert(m.load(0x10000,4)==0x11aabb44 && calls==1);m.store(0x10000,4,0x11223344);assert(calls==2);m.host_access_observer={};
 auto token=m.reservation_token(0x10000,4);m.store(0x10000,4,0x11223344);assert(token!=m.reservation_token(0x10000,4));
 m.regions[0].flags=4;fault([&]{m.store(0x10000,1,0);});assert(m.load(0x10000,4)==0x11223344);
 m.regions[0].flags=2;fault([&]{m.load(0x10000,1);});m.regions[0].flags=7;fault([&]{m.store(0x10000,1,0);});m.regions[0].flags=6;
 m.store(0x11000,4,0x55667788);fault([&]{m.load(0x11001,4);});fault([&]{m.store(0x11001,4,0);});assert(m.load(0x11000,4)==0x55667788);
 fault([&]{m.load(UINT64_C(0x100000000),4);});
 m.regions[1].bytes=std::make_shared<GuestBacking>(2,0xee);assert(m.load(0x11000,2)==0xeeee);fault([&]{m.load(0x11000,4);});
''')
