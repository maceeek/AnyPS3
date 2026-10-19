"""Game RSX owner: native FIFO, BE visibility, event affinity and lifecycle."""
from pathlib import Path
import os
import subprocess
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

ARTIFACTS = Path('local/ratchet/codex/par6/tests')
MAIN = r'''
static unsigned setup_fifo(PpuState& s){
 setup_draw(s,32,16,128,false);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x80000000,0x10000,6,nullptr,0);g.dma=0x70008000;g.reports=0x80000000;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();
 g.registers[0x17cc]=1;g.registers[0x1a8]=0x66626660;
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<2;++i){packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);}
 packet(0x1800,1u<<24);m.store(g.dma+0x44,4,0x100);m.store(g.dma+0x40,4,put);return put;
}
static void fifo(bool lanes){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState serial;const auto put=setup_fifo(serial);assert(rsx_run(serial,1024));
 std::vector<std::uint8_t> expected(32*16*4),actual(expected.size());serial.memory.transfer(0xc0100000,expected.data(),expected.size(),false);
 assert(serial.memory.load(0x80001408,4)==1024 && near_argb(serial.memory.load(0xc0100000,4),0x80ff8040));
 PpuState s;assert(setup_fifo(s)==put);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS",lanes?"1":"0",1);
 const auto main_tid=std::this_thread::get_id();SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled && s.memory.concurrent());
 assert(!rsx_worker_request(s,1024).completed);w.queue->drain();
 auto complete=[&]{w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get())->retire(true);});};complete();
 assert(s.memory.load(0x70008044,4)==put && s.memory.load(0x80001408,4)==1024);
 s.memory.transfer(0xc0100000,actual.data(),actual.size(),false);assert(expected==actual);
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(std::this_thread::get_id()!=main_tid);
  auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());
  assert(!session.state.coordinator_maps);assert(session.defer_label(0x70008400,0x11223344));session.leave_fifo();});
 // The PPU can publish neighbouring FIFO/control bytes without retiring a label.
 s.memory.store(0x70008010,4,0xabcdef01);assert(s.memory.load(0x71008010,4)==0xabcdef01);
 complete();
 assert(s.memory.load(0x71008400,4)==0x11223344);
 // Actual SPU-style foreign byte reader follows the same owner queue.
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());assert(session.defer_label(0x70008410,0xaabbccdd));session.leave_fifo();});
 complete();
 std::atomic<bool> done{false};std::exception_ptr failure;std::thread reader([&]{try{assert(s.memory.load(0x71008410,4)==0xaabbccdd);s.memory.release_reader_slot();}catch(...){failure=std::current_exception();}done.store(true);});
 while(!done.load()){rsx_worker_pump(s);std::this_thread::yield();}reader.join();if(failure)std::rethrow_exception(failure);

 // A real protected retained pointer faults on the PPU host thread. The
 // signal frame only writes a pipe mailbox/futex; the RSX owner uses the
 // already pinned mapping to publish exact BE bytes and then retries it.
 auto fault_range=s.memory.range(0x70009000,4,true);
 std::unique_ptr<GuestMemory::HostGuard> raw_guard;std::atomic<bool> fault_serviced{false};
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{
  raw_guard=std::make_unique<GuestMemory::HostGuard>();auto& pin=fault_range.parts[0];
  assert(raw_guard->add(pin.data,4,*pin.owner));
  raw_guard->synchronize=[&]{assert(w.on_owner());auto issuer=HostAccessContext::sample();assert(issuer.engine==HostAccessOrigin::Engine::Ppu && issuer.guest_id==44 && issuer.guest_pc==0x2028756c && issuer.operation_id==123);const std::uint8_t bytes[]={0xca,0xfe,0xba,0xbe};rsxdraw::ClaimTransfer transfer(fault_range,0,4);transfer.write(0,bytes,4);fault_serviced=true;};raw_guard->arm();
 });
 std::atomic<bool> host_callback{false};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{w.host->invoke([&]{assert(std::this_thread::get_id()==main_tid);host_callback=true;});});
 for(;;){bool pending;{std::lock_guard<std::mutex> lock(w.queue->mutex);pending=!w.queue->coordinator.empty();}if(pending)break;std::this_thread::yield();}
 volatile const std::uint8_t* retained=fault_range.parts[0].data;
 {std::uint32_t tid=44,pc=0x2028756c;std::uint64_t steps=123;HostAccessContext issuer(HostAccessOrigin::Engine::Ppu,tid,pc,&steps);
  assert(retained[0]==0xca && retained[1]==0xfe && fault_serviced.load() && host_callback.load());}
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{raw_guard.reset();});

 // The fault mailbox may trail an already completed unprotect/MapBarrier.
 // Force that order: the owner disarms after the signal mailbox is published
 // and before its queued HostAccess can run. A resolved permitted read retries.
 s.memory.store(0x70009000,4,0x66112233);
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{raw_guard=std::make_unique<GuestMemory::HostGuard>();auto& pin=fault_range.parts[0];assert(raw_guard->add(pin.data,4,*pin.owner));raw_guard->synchronize=[]{assert(false);};raw_guard->arm();});
 std::atomic<bool> disarming{false};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{disarming=true;bool queued=false;const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
  while(!queued && std::chrono::steady_clock::now()<deadline)for(const auto& slot:w.faults->slots)queued|=slot.state.load(std::memory_order_acquire)==2;
  assert(queued);raw_guard->disarm();
 });
 while(!disarming.load()){std::this_thread::yield();}
 assert(retained[0]==0x66);
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{raw_guard.reset();});
 // Completion events execute on the coordinator; GPU completion fields
 // preserve neighbouring status bits and use the original BE layout.
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{
  w.host->invoke([&]{assert(std::this_thread::get_id()==main_tid);s.events.queues.push_back({23,4,0,{}});s.reschedule=false;});
  auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());
  assert(session.defer_flip(0x70008500,0x70008600,3,23,UINT64_C(0x123456789abcdef0),8));session.flush(false);session.leave_fifo();
 });
 assert(s.memory.load(0x70008508,4)==0x80000000 && s.memory.load(0x70008510,4)==3 && s.memory.load(0x70008600,8)==0);
 assert(s.events.queues.back().data.size()==1 && s.events.queues.back().data[0][0]==UINT64_C(0x123456789abcdef0) && s.reschedule);

 // Force an actual main-thread raw read of a pending GPU flip report.
 // Disable idle retirement until the foreign fault has reached its mailbox,
 // the fault is observed; completion/event publication is then explicit.
 auto pending_report=s.memory.range(0x70008b00,4,false);s.memory.store(0x70008b00,4,0xfeedface);
 const auto events_before=s.events.queues.back().data.size();std::function<void()> saved_service;std::atomic<bool> armed{false};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());
  saved_service=w.queue->service;w.queue->service={};assert(session.defer_flip(0x70008a00,0x70008b00,2,23,UINT64_C(0x123456789abcdef0),8));session.leave_fifo();armed=true;
  bool queued=false;const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
  while(!queued && std::chrono::steady_clock::now()<deadline)for(const auto& slot:w.faults->slots)queued|=slot.state.load(std::memory_order_acquire)==2;
  assert(queued);
 });
 while(!armed.load()){std::this_thread::yield();}
 volatile const std::uint8_t* flip_report=pending_report.parts[0].data;const auto pending_byte=flip_report[0];(void)pending_byte;complete();assert(flip_report[0]==0);
 rsx_worker_pump(s);assert(s.memory.load(0x70008a08,4)==0x80000000 && s.memory.load(0x70008a10,4)==2 && s.memory.load(0x70008b00,8)==0);
 assert(s.events.queues.back().data.size()==events_before+1);
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{w.queue->service=std::move(saved_service);});
 // Map publication must first retire resident resources. Alias identity and
 // an original Range survive unrelated additions, but not a rights change.
 auto pinned=s.memory.range(0x70008400,4,true);s.memory.map(0x90000000,4096,6,nullptr,0);s.memory.store(0x90000000,4,0xcafebabe);
 {GuestMemory::ConfigurationPermit config(s.memory);GuestMemory::RawPermit raw(s.memory);s.memory.regions[0].flags=4;s.memory.mapping_changed();}
 bool stale=false;try{std::uint8_t b[4]={};pinned.write(0,b,4);}catch(const std::runtime_error&){stale=true;}assert(stale);
 life.finish();assert(!w.enabled && !w.queue->error && !s.rsx.renderer && !s.rsx.graphics_owner);
 // Resident module entries can start and join another worker on the same process.
 {SpuWorkerSession next(s);assert(w.enabled);next.finish();}
 std::puts(lanes?"PASS FIFO game owner + SPU pool: pixels/report/GET/labels/alias/map/teardown/restart":"PASS FIFO game owner serial SPU: pixels/report/GET/labels/alias/map/teardown/restart");
}

static void sdl_host(){
 setenv("ANYPS3_PRESENT","sdl",1);setenv("SDL_VIDEODRIVER","dummy",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;setup_draw(s,2,1,8,false);s.rsx.display_buffers[0]={2,1,8,0x100000};s.memory.store(0xc0100000,4,0x00112233);s.memory.store(0xc0100004,4,0x00445566);
 const auto main_tid=std::this_thread::get_id();SpuWorkerSession session(s);auto& w=s.process.rsx_worker;
 const auto dispatch=s.present.host_dispatch;unsigned calls=0;
 s.present.host_dispatch=[&](const std::function<void()>& invoke){dispatch([&]{assert(std::this_thread::get_id()==main_tid);++calls;invoke();});};
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{rsx_present_display(*static_cast<PpuState*>(w.facade.get()),0);});
 assert(calls==2 && s.present.presented_frames==1 && s.present.pixels==std::vector<std::uint8_t>({0x11,0x22,0x33,255,0x44,0x55,0x66,255}));
 session.finish();s.present.shutdown();unsetenv("ANYPS3_PRESENT");
 std::puts("PASS real SDL dummy scanout on main thread while RSX owner converts pixels");
}
int main(){try{fifo(false);fifo(true);sdl_host();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

class Parallel6GameOwner(unittest.TestCase):
    def test_real_game_owner_fifo_and_memory(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp=ARTIFACTS/'game-owner.cpp';exe=cpp.with_suffix('')
        cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0]+MAIN)
        result=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(exe)],capture_output=True,text=True,timeout=300)
        cpp.with_suffix('.build.log').write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode,0,result.stderr[-12000:])
        env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1',ANYPS3_SPU_WORKER='0')
        result=subprocess.run([str(exe)],capture_output=True,text=True,env=env,timeout=90)
        cpp.with_suffix('.run.log').write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr[-12000:])
        self.assertNotIn('Validation Error',result.stderr);self.assertNotIn('Validation Warning',result.stderr)
        self.assertEqual(result.stdout.count('PASS '),3)

CPU_MAIN = r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;auto& m=s.memory;m.map(0x10000,0x10000,6,nullptr,0);m.map(0x60000000,0x400000,6,nullptr,0);
 auto& g=s.rsx;g.device=0x60000000;g.dma=0x60100000;g.driver=0x60200000;g.reports=0x60300000;
 g.io[0]=0x10000;g.event_queue=17;g.event_port=19;
 s.events.queues.push_back({17,4,0,{}});s.events.ports.push_back({19,1,17,UINT64_C(0x1122334455667788)});
 m.store(g.driver+0x12c0,4,0x802);const auto tid=std::this_thread::get_id();
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;
 // Source-independent 60000/1001Hz timebase values, exact BE driver fields.
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{
  g.context=0x55555555;g.fifo_enabled=true;g.vblank_epoch_ns=1000000000;
  assert(rsx_tick_vblank(*static_cast<PpuState*>(w.facade.get()),1016683334));
  assert(m.load(g.driver+0x1114,4)==81131329);
  assert(m.load(g.driver+0x1118,8)==1016683 && m.load(g.driver+0x1128,8)==1);
  assert(rsx_tick_vblank(*static_cast<PpuState*>(w.facade.get()),1033366667));
  w.queue->owner_call([&]{assert(std::this_thread::get_id()==tid);assert(s.events.queues[0].data.size()==1);assert(s.events.queues[0].data[0][0]==UINT64_C(0x1122334455667788) && s.events.queues[0].data[0][2]==0x802);});
  g.context=0;g.fifo_enabled=false;g.vblank_epoch_ns=0;g.vblank_ticks=0;
 });
 // A timed service must run even without another producer/PUT submission.
 std::atomic<unsigned> timed{0};w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{w.queue->service=[&]{++timed;};w.queue->deadline=[]{return std::chrono::steady_clock::now()+std::chrono::milliseconds(2);};});
 bool teardown_main=false;w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{auto original=w.queue->idle_teardown;w.queue->idle_teardown=[&,original]{w.queue->owner_call([&]{assert(std::this_thread::get_id()==tid);teardown_main=true;});original();};});
 const auto before=timed.load();std::this_thread::sleep_for(std::chrono::milliseconds(15));assert(timed.load()>before);
 // Main and owner work overlap: neither side waits for the other's gate.
 std::atomic<bool> owner_entered{false},main_entered{false};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{owner_entered=true;while(!main_entered.load())std::this_thread::yield();});
 const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(2);
 while(!owner_entered.load() && std::chrono::steady_clock::now()<until)std::this_thread::yield();
 assert(owner_entered.load());main_entered=true;w.queue->drain();
 // Unknown FIFO packet is the same explicit failure, never a successful skip.
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{g.fifo_enabled=true;g.vblank_epoch_ns=0;g.context=0;});
 m.store(0x10000,4,0xe0000000);m.store(g.dma+0x40,4,4);m.store(g.dma+0x44,4,0);
 std::string actual;rsx_worker_request(s,8);try{w.queue->drain();}catch(const std::runtime_error& e){actual=e.what();}
 assert(!actual.empty());try{life.finish();}catch(const std::runtime_error& e){assert(actual==e.what());}
 assert(teardown_main && s.supervisor.first_error && s.supervisor.failed_engine==ExecutionEngine::Rsx);
 PpuState reference;reference.memory.map(0x10000,0x10000,6,nullptr,0);reference.memory.map(0x60000000,0x400000,6,nullptr,0);
 reference.rsx.dma=0x60100000;reference.rsx.io[0]=0x10000;reference.rsx.fifo_enabled=true;reference.memory.store(0x10000,4,0xe0000000);reference.memory.store(reference.rsx.dma+0x40,4,4);
 std::string expected;try{rsx_run(reference,8);}catch(const std::runtime_error& e){expected=e.what();}assert(actual==expected);
 std::puts("PASS owner vblank/deadline/coordinator affinity/overlap/exception parity and teardown");
'''

class Parallel6CpuContracts(unittest.TestCase):
    def test_events_clock_overlap_and_exception(self):
        from tests.test_par1 import source
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        cpp=ARTIFACTS/'events.cpp';exe=cpp.with_suffix('');cpp.write_text(source(CPU_MAIN,full=True))
        build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(exe)],capture_output=True,text=True,timeout=240)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode,0,build.stderr[-12000:])
        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=30)
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr[-12000:])

    def native_case(self,name,body,*,full=False,words=()):
        from tests.test_par1 import source
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        cpp=ARTIFACTS/(name+'.cpp');exe=cpp.with_suffix('');cpp.write_text(source(body,full=full,words=words))
        build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(exe)],capture_output=True,text=True,timeout=240)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr);self.assertEqual(build.returncode,0,build.stderr[-12000:])
        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=45)
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr);self.assertEqual(run.returncode,0,run.stdout+run.stderr[-12000:])

    def test_exact_watch_overlap_removal_duplicates_and_page_boundary(self):
        self.native_case('exact-watch',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);auto* p=m.regions[0].bytes->data();auto& observer=m.host_access_observer;
 observer=[](const void*,std::uint64_t,bool){};observer.filtered=true;observer.supports_exact_bytes=true;
 observer.watch(p+16,4);observer.watch(p+18,8);observer.watch(p+16,4);
 {HostByteAccess exact(true);assert(observer.interested(p+16,1) && observer.interested(p+25,1) && !observer.interested(p+26,1));assert(!observer.interested(p,4));
  observer.watch(p+16,4,false);assert(observer.interested(p+16,1));observer.watch(p+16,4,false);assert(!observer.interested(p+17,1) && observer.interested(p+18,1));
  observer.watch(p+18,8,false);assert(!observer.interested(p+18,1));
  observer.watch(p+4094,4);assert(observer.interested(p+4095,2) && !observer.interested(p+4093,1) && !observer.interested(p+4098,1));
  auto copy=observer;observer.watch(p+4094,4,false);assert(!observer.interested(p+4095,2) && copy.interested(p+4095,2));
 }
 observer.watch(p+16,4);assert(observer.interested(p+256,1)); // Retained raw pointer remains page guarded.
''')

    def test_six_active_spu_lanes_dma_get_and_put_with_rsx_owner(self):
        from tests.test_par2 import groups,LOOP
        body='setenv("ANYPS3_RSX_WORKER","1",1);'+groups(LOOP,counts=(3,3))+r'''
 std::atomic<unsigned> completed{0};bool published=false;
 auto label=mem.range(0x10000,4,true);
 {SpuOwnerScope control(s,0,false,false);
  s.process.rsx_worker.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{GuestMemory::RawPermit metadata(mem);
   mem.host_access_observer=[&](const void* p,std::uint64_t n,bool){
    if(published || !label.overlaps(p,n))return;
    assert(s.process.rsx_worker.on_owner());std::unique_ptr<GuestMemory::GpuPages> claim;
    mem.renderer_pin([&]{claim=std::make_unique<GuestMemory::GpuPages>(mem,std::vector<GuestMemory::HostSpan>{{label.parts[0].data,4,label.parts[0].owner}});});claim->adopt();
    const std::uint8_t bytes[]={0xaa,0xbb,0xcc,0xdd};claim->commit(label,0,bytes,4);published=true;
   };mem.host_access_observer.filtered=true;mem.host_access_observer.supports_exact_bytes=true;mem.host_access_observer.watch(label.parts[0].data,4);
  });
  unsigned number=0;
  for(auto& lane:worker.lanes){auto* t=lane->context;auto* ls=lane->group->context.data()+t->number*0x40000;auto facade=std::make_shared<PpuState>(s.process);auto sent=std::make_shared<bool>(false);const auto index=number++;
   lane->step=[&,t,ls,facade,sent,index](unsigned){NativeSpuWorker::current->event_driven=true;if(*sent)return false;*sent=true;
    t->dma={0x200,0,0x10000,4,0};spu_write_channel(*facade,*t,ls,21,0x40);
    assert(ls[0x200]==0xaa && ls[0x201]==0xbb && ls[0x202]==0xcc && ls[0x203]==0xdd);
    ls[0x203]+=index;t->dma={0x200,0,0x11000+index*128,4,0};spu_write_channel(*facade,*t,ls,21,0x20);++completed;return true;
   };
  }
  assert(number==6);
 }
 until([&]{return completed.load()==6;});
 for(unsigned i=0;i<6;++i)assert(mem.load(0x11000+i*128,4)==0xaabbccdd+i);
 s.process.rsx_worker.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{GuestMemory::RawPermit metadata(mem);mem.host_access_observer=GuestMemory::HostObserver{};});
 session.finish();assert(!worker.alive() && !s.process.rsx_worker.enabled);
 std::puts("PASS six SPU lanes DMA GET waits for RSX label, PUT publishes original BE bytes");
'''
        self.native_case('six-lanes',body,full=True,words=LOOP)

    def test_audio_tick_uses_validated_bytes_while_worker_runs(self):
        self.native_case('audio',r'''
 setenv("ANYPS3_AUDIO_SINK","null",1);setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;auto& mem=s.memory;mem.map(0x10000,8192,6,nullptr,0);
 assert(audio_service(s,0)==0);mem.store(0x10100,8,2);mem.store(0x10108,8,8);s.r[3]=0x10100;s.r[4]=0x10180;assert(audio_service(s,2)==0);
 s.r[3]=0;assert(audio_service(s,4)==0);auto& a=s.audio;auto& p=a.ports[0];a.next_tick=1000000000;
 mem.store(p.address,4,0x3e800000);mem.store(p.address+2048,4,0x3f000000);mem.store(p.index+8,8,UINT64_C(0x1122334455667788));
 SpuWorkerSession session(s);assert(!mem.local_state().raw);audio_advance(s,1000000000);
 assert(a.submitted_frames==256 && mem.load(p.index,8)==1 && mem.load(p.address,4)==0 && mem.load(p.address+2048,4)==0x3f000000 && mem.load(p.index+8,8)==UINT64_C(0x1122334455667788));
 session.finish();
''',full=True)

    def test_original_exception_survives_raw_yield_restoration(self):
        self.native_case('yield-failure',r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;s.memory.map(0x10000,4096,6,nullptr,0);SpuWorkerSession session(s);auto& w=s.process.rsx_worker;bool original=false;
 try{GuestMemory::RawPermit raw(s.memory);GuestMemory::RawYield yield(s.memory);
  w.queue->call(RsxOwnerQueue::Kind::HostAccess,[]{throw std::runtime_error("independent worker failure during yielded HLE");});
 }catch(const std::runtime_error& e){original=std::string(e.what())=="independent worker failure during yielded HLE";}
 assert(original && !w.enabled && !s.memory.local_state().raw);
 bool repeated=false;try{rsx_worker_pump(s);}catch(const std::runtime_error& e){repeated=std::string(e.what())=="independent worker failure during yielded HLE";}assert(repeated);
''',full=True)

    def test_bootstrap_failure_joins_renderer_and_preserves_stop(self):
        self.native_case('bootstrap-failure',r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;auto& m=s.memory;m.map(0x60000000,0x400000,6,nullptr,0);auto& g=s.rsx;g.context=1;g.fifo_enabled=true;g.driver=0x60200000;g.vblank_epoch_ns=1;
 m.store(g.driver+0x12c0,4,0x802);bool stopped=false;
 try{SpuWorkerSession life(s);}catch(const std::runtime_error& e){stopped=std::string(e.what())=="RSX vblank event port is disconnected";}
 assert(stopped && !s.process.rsx_worker.enabled && !s.process.rsx_worker.queue->thread.joinable() && !m.renderer_enabled);
''',full=True)

    def test_async_fault_event_error_and_accepted_delivery_during_shutdown(self):
        self.native_case('fault-events',r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;s.memory.map(0x10000,4096,6,nullptr,0);SpuWorkerSession life(s);auto& w=s.process.rsx_worker;bool stopped=false;
 try{w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{auto& f=*static_cast<PpuState*>(w.facade.get());auto& flag=s.memory.local_state().fault_claim;flag=true;
   struct Reset{bool& flag;~Reset(){flag=false;}} reset{flag};rsx_event_publish(f,23,UINT64_C(0x1122334455667788),8,"RSX deferred flip event cannot be delivered");
  });rsx_worker_pump(s);
 }catch(const std::runtime_error& e){stopped=std::string(e.what())=="RSX deferred flip event cannot be delivered";}
 assert(stopped && s.events.queues.empty() && s.supervisor.failed_engine==ExecutionEngine::Rsx);
 try{life.finish();}catch(const std::runtime_error& e){assert(std::string(e.what())=="RSX deferred flip event cannot be delivered");}
 RsxOwnerQueue owner;std::atomic<bool> posted{false};unsigned delivered=0;const auto main_tid=std::this_thread::get_id();owner.launch();
 owner.post(RsxOwnerQueue::Kind::HostAccess,[&]{owner.owner_post([&]{assert(std::this_thread::get_id()==main_tid);++delivered;});posted=true;});
 while(!posted.load()){std::this_thread::yield();}owner.shutdown();assert(delivered==1 && !owner.error && owner.coordinator.empty());
''',full=True)
