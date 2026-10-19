"""Deterministic SPU pause / RSX owner handoff, using the real memory bridge."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import unittest

from tests.test_par1 import source


ARTIFACTS = Path(os.environ.get('ANYPS3_PAR14_ARTIFACTS',
                              'local/ratchet/codex/par14/tests'))

HANDOFF = r'''
 auto scenario=[](bool poison){
 setenv("ANYPS3_RSX_WORKER","1",1);
 PpuState s;auto& m=s.memory;m.map(0x10000,4096,6,nullptr,0);
 m.regions.push_back({0x20000,6,m.regions[0].bytes});
 rsx_worker_start(s);auto& rsx=s.process.rsx_worker;auto& q=*rsx.queue;
 auto& pool=s.process.spu_worker;pool.enabled=true;pool.pool_mode=true;
 NativeSpu::Group g;g.id=7;g.state=6;g.scheduled=true;g.threads.resize(2);
 g.threads[0].id=17;g.threads[1].id=18;s.spu.groups.push_back(std::move(g));
 auto& group=s.spu.groups.back();
 std::atomic<bool> fifo_entered{false},allow_flip{false},dma_done{false},rpc_done{false};
 std::atomic<unsigned> phase{0},coherence_calls{0};bool armed=false,control_done=false;
 const auto main_tid=std::this_thread::get_id();
 // No renderer/GPU stand-in claims: phase 1 models a completed frame. The
 // actual coordinator transport must publish its event before phase 3.
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{
  q.run_fifo=[&](unsigned){fifo_entered=true;
   while(!allow_flip.load())std::this_thread::yield();
   assert(phase.exchange(1)==0);
   rsx_worker_events(s,[&]{assert(std::this_thread::get_id()==main_tid);
    assert(phase.exchange(2)==1);});
  };
  m.host_access_observer=[&](const void*,std::uint64_t n,bool write){
   assert(rsx.on_owner() && n==4 && write);++coherence_calls;
   assert(phase.load()==2);phase=3;
   if(poison)throw std::runtime_error("PAR14 exact coherence failure");
  };
  m.host_access_observer.watch(m.regions[0].bytes->data(),4);
 });
 q.post(RsxOwnerQueue::Kind::RunFifo,{},1);
 while(!fifo_entered.load())std::this_thread::yield();
 for(unsigned i=0;i<2;++i){auto p=std::make_unique<NativeSpuWorker>();
  p->parent=&pool;p->group_id=7;p->lane_thread_id=17+i;
  p->context=&group.threads[i];p->group=&group;p->pump=[&]{pool.service();};
  pool.lanes.push_back(std::move(p));}
 auto& caller=*pool.lanes[0];auto& writer=*pool.lanes[1];
 writer.step=[&](unsigned){writer.event_driven=true;
  const std::uint8_t payload[]={0x12,0x34,0x56,0x78};
  if(!dma_done.load()){
   // This is the production BE DMA / physical-alias / Lease path.
   spu_transfer_main(m,0x20000,const_cast<std::uint8_t*>(payload),4,true);
   assert(phase.exchange(4)==3);dma_done=true;m.release_reader_slot();
  }return false;
 };
 caller.step=[&](unsigned){caller.event_driven=true;
  if(!rpc_done.load()){
   assert(spu_owner_call(s,group.threads[0],[&]{
    assert(phase.exchange(5)==4);control_done=true;
   }));rpc_done=true;
  }return false;
 };
 pool.pump=[&]{rsx_worker_pump(s);
  if(!armed){
   // Deliver the RSX request AFTER the last pump and BEFORE pause parks.
   // This handshake forces the lost-wakeup window without timing sleeps.
   armed=true;allow_flip=true;
   while(!q.coordinator_pending.load(std::memory_order_acquire))std::this_thread::yield();
   std::fputs("PAR14 late flip queued; DMA waits for RSX; owner control waits for DMA lane\n",stderr);
  }
 };
 writer.launch();
 for(;;){std::lock_guard<std::mutex> lock(q.mutex);if(!q.queue.empty())break;}
 caller.launch();
 while(!pool.owner_pending.load(std::memory_order_acquire))std::this_thread::yield();
 pool.drain_owner();
 if(poison){
  pool.shutdown();bool exact=false;
  try{pool.check();}catch(const std::runtime_error& e){exact=std::string(e.what())=="PAR14 exact coherence failure";}
  assert(exact && !control_done && !dma_done && !rpc_done && phase==3 && coherence_calls==1);
  rsx.stop();m.host_access_observer=GuestMemory::HostObserver{};
  assert(m.load(0x10000,4)==0);return;
 }
 assert(control_done && dma_done && coherence_calls==1 && phase==5);
 pool.shutdown();assert(rpc_done && !pool.owner_pending);
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{m.host_access_observer=GuestMemory::HostObserver{};});
 assert(m.load(0x10000,4)==0x12345678 && m.load(0x20000,4)==0x12345678);
 rsx.stop();
 };
 scenario(false);scenario(true);
 std::puts("PASS frame -> flip event -> coherence -> BE DMA -> paused owner control");
 std::puts("PASS exact coherence failure cancels DMA/control without writing bytes");
'''


CROSS_LANE = r'''
 NativeSpuWorker pool;pool.enabled=true;pool.pool_mode=true;
 for(unsigned i=0;i<2;++i){auto p=std::make_unique<NativeSpuWorker>();p->parent=&pool;
  p->pump=[&]{pool.service();};pool.lanes.push_back(std::move(p));}
 auto& target=*pool.lanes[0];auto& producer=*pool.lanes[1];
 std::atomic<bool> active{false},publish{false},release{false},rpc_done{false};unsigned effects=0;
 target.step=[&](unsigned){target.event_driven=true;active=true;
  while(!release.load()){std::this_thread::yield();}return false;};
 producer.step=[&](unsigned){producer.event_driven=true;
  while(!publish.load())std::this_thread::yield();
  producer.owner_call([&]{assert(effects++==0);release=true;});rpc_done=true;return false;};
 target.launch();producer.launch();while(!active.load())std::this_thread::yield();
 pool.pump=[&]{if(!publish.load()){publish=true;
  while(!pool.owner_pending.load(std::memory_order_acquire))std::this_thread::yield();}};
 target.pause();assert(release && effects==1);target.resume();pool.shutdown();assert(rpc_done);
 std::puts("PASS late owner RPC from another lane wakes the coordinator pause");
'''


SYNC_TICKET = r'''
 setenv("ANYPS3_RSX_WORKER","1",1);PpuState s;rsx_worker_start(s);
 auto& q=*s.process.rsx_worker.queue;auto& lane=s.process.spu_worker;
 std::atomic<bool> entered{false},go{false};unsigned effects=0;bool armed=false;
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{q.run_fifo=[&](unsigned){entered=true;
  while(!go.load())std::this_thread::yield();
  q.owner_call([&]{assert(effects++==0);});
 };});
 q.post(RsxOwnerQueue::Kind::RunFifo,{},1);while(!entered.load())std::this_thread::yield();
 lane.pauses=1;lane.event_driven=true;
 lane.step=[&](unsigned n){assert(n==7);q.call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(effects++==1);});return true;};
 lane.pump=[&]{rsx_worker_pump(s);if(!armed){armed=true;go=true;
  while(!q.coordinator_pending.load(std::memory_order_acquire))std::this_thread::yield();}};
 lane.launch();auto reply=lane.request(7);assert(reply.completed && reply.progress && effects==2 && lane.completed==1);
 lane.shutdown();s.process.rsx_worker.stop();
 // NativeSpuWorker is destroyed before NativeRsxWorker in Process. The RSX
 // teardown callback still needs a live wake channel during that interval.
 bool teardown_done=false;
 {PpuState final;rsx_worker_start(final);auto& tail=*final.process.rsx_worker.queue;
  tail.call(RsxOwnerQueue::Kind::HostAccess,[&]{auto original=tail.idle_teardown;
   tail.idle_teardown=[&,original]{tail.owner_call([&]{teardown_done=true;});original();};});}
 assert(teardown_done);
 std::puts("PASS synchronous SPU ticket pumps late RSX owner callbacks");
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Par14DeadlockContracts(unittest.TestCase):
    def native(self, name, body):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp = ARTIFACTS / (name + '.cpp')
        exe = cpp.with_suffix('')
        cpp.write_text(source(body, full=True))
        flags = shlex.split(os.environ.get('ANYPS3_PAR14_CXXFLAGS', '-O1'))
        command = ['c++', '-std=c++17', *flags, '-Wall', '-Wextra', '-Werror',
                   '-pthread', str(cpp), '-o', str(exe)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=300)
        cpp.with_suffix('.build.log').write_text(built.stdout + built.stderr)
        self.assertEqual(built.returncode, 0, built.stderr[-12000:])
        env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null',
                   ANYPS3_SPU_THREADS='0', ANYPS3_SPU_WORKER='0',
                   ANYPS3_RSX_WORKER='0', ANYPS3_PERF_REPORT='0')
        try:
            ran = subprocess.run([str(exe.resolve())], env=env, capture_output=True,
                                 text=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            cpp.with_suffix('.run.log').write_text(
                (error.stdout or b'').decode(errors='replace') +
                (error.stderr or b'').decode(errors='replace') +
                'TIMEOUT (10s): deterministic handoff did not complete\n')
            raise error
        cpp.with_suffix('.run.log').write_text(ran.stdout + ran.stderr)
        self.assertEqual(ran.returncode, 0, ran.stderr[-12000:])

    def test_pause_pumps_late_rsx_flip_before_watched_dma(self):
        self.native('late-flip-dma', HANDOFF)

    def test_pause_pumps_late_owner_request_from_another_lane(self):
        self.native('cross-lane', CROSS_LANE)

    def test_synchronous_ticket_pumps_late_rsx_owner_request(self):
        self.native('sync-ticket', SYNC_TICKET)


if __name__ == '__main__':
    unittest.main()
