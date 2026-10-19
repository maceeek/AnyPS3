"""PAR3 native contracts. All artifacts stay inside the assigned PAR3 tree."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source

ARTIFACTS = Path('local/ratchet/codex/par3/tests')


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Parallel3Contracts(unittest.TestCase):
    def native(self, name, body, *, full=False, words=(), transform=None):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        code = source(body, full=full, words=words)
        if transform:
            code = transform(code)
        cpp = ARTIFACTS/(name+'.cpp'); exe = cpp.with_suffix('')
        cpp.write_text(code)
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(exe)], capture_output=True, text=True, timeout=240)
        (ARTIFACTS/(name+'-build.log')).write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode, 0, build.stderr[-12000:])
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env={**os.environ, 'ANYPS3_SPU_THREADS':'0', 'ANYPS3_SPU_WORKER':'0', 'ANYPS3_PERF_REPORT':'0'})
        (ARTIFACTS/(name+'-run.log')).write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode, 0, run.stderr[-12000:])

    def test_radix_observer_counts_copies_alias_and_parallel_readers(self):
        self.native('observer', r'''
 GuestMemory m;m.map(0x10000,0x600000,6,nullptr,0);
 auto b=m.regions.front().bytes;unsigned writes=0;
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++writes;};
 auto& o=m.host_access_observer;o.filtered=true;
 assert(!o.interested(b->data(),0x600000));
 o.watch(b->data()+4095,2);o.watch(b->data()+4096,1);
 assert(o.interested(b->data()+1,1) && o.interested(b->data()+8191,1));
 assert(!o.interested(b->data()+8192,1));o.watch(b->data()+4095,2,false);
 assert(!o.interested(b->data(),1) && o.interested(b->data()+4096,1));
 GuestMemory::HostObserver copied=o;
 o.watch(b->data()+4096,1,false);assert(!o.interested(b->data(),0x600000));
 assert(copied.interested(b->data()+4096,1));
 o.watch(b->data()+0x400000,1); // another leaf and absent-to-present cache invalidation
 assert(o.interested(b->data()+0x400000,1));
 m.regions.push_back({0x1000000,6,b});m.store(0x1400000,4,0x12345678);assert(writes==1);
 std::vector<std::thread> readers;
 for(unsigned i=0;i<6;++i)readers.emplace_back([&,i]{for(unsigned n=0;n<10000;++n){assert(o.interested(b->data()+0x400000,1));assert(!o.interested(b->data()+i*4096,1));}});
 for(auto& t:readers)t.join();
 fault([&]{o.watch(reinterpret_cast<void*>(UINTPTR_MAX),2);});
 o={};assert(!o && !o.filtered);
''')

    def test_lane_pause_services_coherence_queued_after_sleep(self):
        # Instrument only the wait boundary: notifier must acquire the same
        # coordinator wake mutex after this marker, so it cannot enqueue before CV wait
        # has atomically released it. No sleep/timing oracle is involved.
        def instrument(code):
            prefix = '#include <atomic>\nstatic std::atomic<bool> par3_wait_armed{false};\n'
            needle = 'progress.changed.wait(lock,[&]{return progress.generation!=seen;});'
            self.assertEqual(code.count(needle), 1)
            return prefix+code.replace(needle, 'par3_wait_armed=true;'+needle)
        self.native('late-coherence', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 NativeSpuWorker pool;pool.pool_mode=true;pool.enabled=true;
 auto ptr=std::make_unique<NativeSpuWorker>();auto& lane=*ptr;lane.parent=&pool;
 std::atomic<bool> started{false},enqueue{false};unsigned calls=0;
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};
 m.parallel->wake_owner=[&]{pool.wake_coordinator();};
 lane.pump=[&]{m.service_coherence();};
 lane.step=[&](unsigned){started=true;while(!enqueue.load())std::this_thread::yield();m.store(0x10000,4,0x11223344);return true;};
 pool.lanes.push_back(std::move(ptr));lane.launch();
 while(!started.load())std::this_thread::yield();
 std::thread notifier([&]{while(!par3_wait_armed.load())std::this_thread::yield();{std::lock_guard<std::mutex> l(pool.owner_progress->mutex);}enqueue=true;});
 lane.pause();notifier.join();
 assert(calls==1 && m.load(0x10000,4)==0x11223344);
 {std::lock_guard<std::mutex> l(lane.mutex);assert(!lane.active && lane.pauses==1);}
 pool.shutdown();m.stop_coherence();
''', full=True, transform=instrument)

    def test_page_ownership_alias_raw_reservation_and_unrelated_progress(self):
        self.native('pages', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);auto backing=m.regions[0].bytes;
 m.regions.push_back({0x20000,6,backing});m.enable_concurrency();m.enable_page_broker();
 m.store(0x10000,4,0x12345678);std::pair<std::uintptr_t,std::uint64_t> reservation;
 assert(m.load_reserved(0x10000,4,reservation)==0x12345678);
 auto spans=std::vector<GuestMemory::HostSpan>{{backing->data(),4096,backing}};
 GuestMemory::GpuPages gpu(m,spans);
 fault([&]{GuestMemory::RawPermit legacy(m);});
 fault([&]{m.map(0x30000,4096,6,nullptr,0);});
 std::atomic<bool> entered{false},done{false};std::atomic<unsigned> unrelated{0};
 std::thread reader([&]{entered=true;assert(m.load(0x20000,4)==0x12345678);done=true;});
 std::thread peer([&]{for(unsigned i=0;i<4000;++i)m.store(0x11000,4,i);unrelated=4000;});
 while(!entered.load() || unrelated.load()!=4000)std::this_thread::yield();
 assert(!done.load() && m.load(0x21000,4)==3999);
 gpu.reset();reader.join();peer.join();assert(done.load());
 {GuestMemory::RawPages raw(m,spans);raw.mark(0);raw.pointer(0)[0]=0x12;} // same-value write must invalidate
 assert(!m.store_conditional(0x10000,4,99,reservation));
 assert(m.load(0x20000,4)==0x12345678);
 m.map(0x30000,4096,6,nullptr,0);
 fault([&]{GuestMemory::GpuPages bad(m,{{backing->data()+8191,2,backing}});});
''')

    def test_page_claim_waits_for_existing_cpu_and_pumps_owner(self):
        self.native('pages-pending', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 auto b=m.regions[0].bytes;std::atomic<bool> active{false},release{false},retired{false};
 std::thread cpu([&]{{GuestMemory::Lease pin(m,0x10000,4,false);active=true;while(!release.load())std::this_thread::yield();assert(m.load(0x10000,4)==0);}retired=true;});
 while(!active.load())std::this_thread::yield();
 unsigned pumps=0;m.page_broker.pump=[&]{++pumps;release=true;};
 GuestMemory::GpuPages gpu(m,{{b->data(),4096,b}});cpu.join();assert(pumps>0 && retired.load());
 // Two aliases of one backing page collapse to a single physical claim.
 assert(gpu.pages.size()==1 && gpu.token!=0);
 gpu.reset();m.page_broker.pump={};
 std::atomic<bool> entered{false},failed{false};GuestMemory::GpuPages pending(m,{{b->data(),4096,b}});
 std::thread waiter([&]{entered=true;try{m.store(0x10000,4,99);}catch(const std::runtime_error& e){failed=std::string(e.what())=="broker cancelled";}});
 while(!entered.load())std::this_thread::yield();
 m.page_broker.stop(std::make_exception_ptr(std::runtime_error("broker cancelled")));waiter.join();assert(failed.load());pending.reset();
''')

    def test_atomic_services_allow_spu_dma_without_raw_exclusion(self):
        self.native('atomic-services', r'''
 setenv("ANYPS3_SPU_THREADS","1",1);PpuState s;auto& m=s.memory;m.map(0x10000,4096,6,nullptr,0);
 SpuWorkerSession session(s);
 {SpuServiceScope service(s,true);assert(!m.parallel->exclusive.load() && !m.local_state().raw);
  m.store(0x10000,4,1);m.store(0x10004,4,1);s.r[11]=128;s.r[3]=0x10020;s.r[4]=0x10000;s.r[5]=0;s.r[6]=8;
  assert(event_syscall(s)==0 && m.load(0x10020,4)==0x8d000000);
  std::thread dma([&]{m.store(0x10100,4,0xaabbccdd);});dma.join();assert(m.load(0x10100,4)==0xaabbccdd);
 }
 assert(spu_atomic_syscall(128) && spu_atomic_syscall(102) && !spu_atomic_syscall(801));session.finish();
''', full=True)

    def test_perf_snapshots_do_not_pause_running_aot_lanes(self):
        from tests.test_par2 import groups, LOOP
        self.native('perf', groups(LOOP, counts=(1,1))+r'''
 s.execution.enabled=true;
 for(unsigned n=0;n<1000;++n){anyps3_perf_report(s);const auto metrics=worker.metrics();(void)metrics;}
 for(auto& lane:worker.lanes){std::lock_guard<std::mutex> lock(lane->mutex);assert(lane->pauses==0);}
 until([&]{return s.spu.groups[1].threads[0].published_steps.value.load(std::memory_order_relaxed)>1024;});
 // Destroy must acknowledge the selected group only, with no raw memory gate.
 const auto first=s.spu.groups[0].id;assert(call(177,first,0)==0);
 assert(call(171,first)==0);
 assert(s.spu.groups.size()==1 && !mem.parallel->exclusive.load());
 assert(call(177,s.spu.groups[0].id,0)==0);session.finish();
''', full=True, words=LOOP)

    def test_page_coherence_readback_and_mapping_pump(self):
        self.native('pages-coherence', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);auto b=m.regions[0].bytes;
 m.regions.push_back({0x20000,6,b});m.enable_concurrency();m.enable_page_broker();
 m.store(0x10000,4,0x11223344);std::pair<std::uintptr_t,std::uint64_t> token;
 m.load_reserved(0x10000,4,token);
 GuestMemory::GpuPages gpu(m,{{b->data(),4096,b}});GuestMemory::HostGuard guard;
 assert(guard.add(b->data(),4096,*b));guard.arm();
 const auto owner=std::this_thread::get_id();unsigned readbacks=0;
 m.page_broker.coherence=[&](const std::vector<GuestMemory::HostSpan>& spans,bool write){
  assert(std::this_thread::get_id()==owner && !write && spans.size()==1 && spans[0].size==4);
  guard.disarm();gpu.mark(0);const unsigned char rendered[]={0xaa,0xbb,0xcc,0xdd};
  std::memcpy(gpu.pointer(0),rendered,4);gpu.reset();++readbacks;
 };
 std::atomic<bool> done{false};std::thread cpu([&]{assert(m.load(0x20000,4)==0xaabbccdd);done=true;});
 while(!done.load()){m.service_coherence();std::this_thread::yield();}cpu.join();
 assert(readbacks==1 && !m.store_conditional(0x10000,4,0,token));
 m.page_broker.coherence={};
 // Map mutation pumps a pending flight to completion before publishing maps.
 GuestMemory::GpuPages pending(m,{{b->data(),4096,b}});unsigned pumps=0;
 m.page_broker.pump=[&]{assert(std::this_thread::get_id()==owner);++pumps;pending.reset();};
 m.map(0x30000,4096,6,nullptr,0);assert(pumps==1 && m.load(0x30000,4)==0);
 m.page_broker.pump={};
''')

    def test_nested_cpu_lease_cannot_bypass_owned_page(self):
        self.native('pages-nested', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);auto b=m.regions[0].bytes;m.enable_concurrency();m.enable_page_broker();
 GuestMemory::GpuPages gpu(m,{{b->data()+4096,4096,b}});
 {GuestMemory::Lease outer(m,0x10000,4,false);assert(m.load(0x10000,4)==0);
  bool stopped=false;try{m.store(0x11000,4,0xdeadbeef);}catch(const std::runtime_error& e){stopped=std::string(e.what())=="nested CPU lease expands page set; predeclare complete batch";}assert(stopped);
 }
 gpu.reset();assert(m.load(0x11000,4)==0);
''')

    def test_direct_event_producers_finish_without_coordinator_rpc(self):
        from tests.test_par2 import groups, LOOP
        body=groups(LOOP, counts=(3,3)).replace(' for(auto& g:s.spu.groups)assert(call(173,g.id)==0);','')
        self.native('direct-events', body+r'''
 s.events.queues.push_back({8,6,0,{}});auto handle=s.events.queues.front().data.state;
 for(auto& g:s.spu.groups)for(auto& t:g.threads){t.ports[7]=0;s.r[11]=191;s.r[3]=t.id;s.r[4]=8;s.r[5]=1;s.r[6]=7;assert(syscall_spu_ext(s) && s.r[3]==0 && t.event_targets[7]==handle);}
 std::atomic<unsigned> completed{0};
 for(auto& lane:worker.lanes)(void)lane;
 // Start creates real lanes; custom steps exercise real channel delivery.
 for(auto& g:s.spu.groups)assert(call(173,g.id)==0);
 {SpuOwnerScope control(s);for(auto& lane:worker.lanes){auto* context=lane->context;auto facade=std::make_shared<PpuState>(s.process);auto sent=std::make_shared<bool>(false);
  lane->step=[&,context,facade,sent](unsigned){if(*sent)return false;*sent=true;
   spu_write_channel(*facade,*context,nullptr,28,0xaabbccdd);
   spu_write_channel(*facade,*context,nullptr,30,0x07123456);
   assert(spu_read_channel(*facade,*context,29)==0);++completed;return true;};}}
 const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 // Deliberately do not service() or drain the owner until all six finish.
 while(completed.load()!=6){if(std::chrono::steady_clock::now()>deadline)throw std::runtime_error("direct event watchdog");std::this_thread::yield();}
 {std::lock_guard<std::mutex> lock(worker.mutex);assert(worker.owner_requests.empty());}
 assert(s.events.queues.front().data.size()==6);
 for(const auto& event:s.events.queues.front().data){assert(event[0]==UINT64_C(0xffffffff53505501) && event[2]==UINT64_C(0x0000000700123456) && event[3]==0xaabbccdd);}
 for(auto& g:s.spu.groups){assert(call(177,g.id,0)==0);}
 session.finish();
''',full=True,words=LOOP)

    def test_queue_pin_survives_erase_and_mailbox_reply_preserves_ppu_writes(self):
        self.native('queue-pins', r'''
 PpuState s;auto& e=s.events;e.queues.push_back({7,1,0,{}});auto first=e.queues[0].data.state;
 assert(first->send({1,2,3,4})==0);assert(first->send({5,6,7,8})==0x8001000a);
 e.queues.erase(e.queues.begin());e.queues.push_back({7,1,0,{}});
 assert(first->send({9,10,11,12})==0x80010016 && e.queues[0].data.empty());
 SpuInbox box;assert(box.reserve_reply());
 std::thread ppu([&]{for(unsigned n=1;n<=4;++n)box.push_overwrite(n);});ppu.join();
 box.finish_reply(0x8001000a);assert((box==std::vector<std::uint32_t>{0x8001000a,1,2,4}));
 assert(!box.reserve_reply() && (box==std::vector<std::uint32_t>{0x8001000a}));
''',full=True)

    def test_force_queue_destroy_cancels_ppu_and_suspended_spu_receive(self):
        self.native('queue-cancel', r'''
 PpuState s;s.memory.map(0x10000,4096,6,nullptr,0);auto& r=s.r;
 s.events.queues.push_back({7,2,0,{}});PpuContext waiter;waiter.thread_id=2;waiter.wait_kind=1;waiter.wait_id=7;s.ppu_threads.push_back(waiter);
 NativeSpu::Group g;g.id=8;g.state=5;g.receive_thread=9;g.threads.emplace_back();auto& t=g.threads.back();t.id=9;t.waiting_receive=true;t.receive_key=12;t.queue_bindings[0]={12,7};t.pc=0x100;
 s.spu.groups.push_back(std::move(g));
 r[11]=129;r[3]=7;r[4]=0;assert(event_syscall(s)==0x8001000a && s.events.queues.size()==1);
 r[4]=1;assert(event_syscall(s)==0 && s.events.queues.empty());
 assert(s.ppu_threads[0].wait_kind==0 && s.ppu_threads[0].cancelled_event==7);
 auto& group=s.spu.groups[0];assert(group.state==4 && !group.threads[0].waiting_receive && group.threads[0].pc==0x104 && group.threads[0].steps==1);
 assert((group.threads[0].in_mailbox==std::vector<std::uint32_t>{0x80010013}));
 s.cancelled_event=s.ppu_threads[0].cancelled_event;r[11]=130;r[3]=7;
 assert(event_syscall(s)==0x80010013 && s.cancelled_event==0);assert(event_syscall(s)==0x80010005);
''',full=True)

    def test_serial_memory_copy_preserves_backings_without_copying_broker(self):
        self.native('memory-copy', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.store(0x10000,4,0x12345678);
 GuestMemory other;other=m;assert(other.load(0x10000,4)==0x12345678);
 other.store(0x10000,4,0x55667788);assert(m.load(0x10000,4)==0x55667788);
 GuestMemory active;active.map(0x20000,4096,6,nullptr,0);active.enable_concurrency();active.enable_page_broker();
 fault([&]{GuestMemory invalid=active;(void)invalid;});assert(active.load(0x20000,4)==0);
''')

    def test_queue_erase_remove_closes_only_removed_pins(self):
        self.native('queue-erase-remove', r'''
 PpuState s;auto& queues=s.events.queues;
 for(unsigned n=1;n<=4;++n)queues.push_back({n,2,0,{}});
 auto first=queues[0].data.state,second=queues[1].data.state,third=queues[2].data.state,last=queues[3].data.state;
 queues.erase(std::remove_if(queues.begin(),queues.end(),[](const auto& q){return q.id==2 || q.id==4;}),queues.end());
 assert(queues.size()==2 && queues[0].id==1 && queues[1].id==3);
 assert(first->send({1,2,3,4})==0 && third->send({5,6,7,8})==0);
 assert(second->send({1,2,3,4})==0x80010016 && last->send({1,2,3,4})==0x80010016);
 assert(queues[1].data.front()[3]==8 && unsigned(queues[1].capacity)==2);
 queues.clear();assert(first->send({1,2,3,4})==0x80010016 && third->send({1,2,3,4})==0x80010016);
''',full=True)

    def test_rpc_uses_pinned_group_while_other_groups_change(self):
        from tests.test_par2 import groups, LOOP
        self.native('rpc-registry',groups(LOOP,counts=(1,))+r'''
 auto* original=&s.spu.groups.front();auto facade=std::make_shared<PpuState>(s.process);std::atomic<unsigned> calls{0};
 {SpuOwnerScope owner(s);auto* context=&original->threads[0];worker.lanes[0]->step=[&,context,facade](unsigned){
   assert(spu_owner_call(*facade,*context,[&]{assert(&spu_owner(s,*context)==original);++calls;}));return false;
 };}
 for(unsigned n=0;n<1000;++n){NativeSpu::Group temporary;temporary.id=0x600000+n;temporary.count=1;temporary.context.resize(0x40000);
  s.spu.groups.push_back(std::move(temporary));
  s.spu.groups.erase(std::prev(s.spu.groups.end()));
  if(!(n&7))worker.service();
 }
 until([&]{return calls.load()>=64;});assert(s.spu.groups.size()==1 && &s.spu.groups.front()==original);
 assert(call(177,original->id,0)==0);session.finish();
''',full=True,words=LOOP)
