"""PAR15 native tests. Fixed BE payloads and forced cross-lane handshakes."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import unittest
from tests.test_par1 import source

ARTIFACTS = Path(os.environ.get('ANYPS3_PAR15_ARTIFACTS', 'local/ratchet/codex/par15/tests'))

SETUP = r'''
 PpuState s;auto& pool=s.process.spu_worker;pool.enabled=true;pool.pool_mode=true;
 pool.coordinator_clock.enabled=true;pool.refresh_pending=false;
 NativeSpu::Group initial;initial.id=7;initial.state=6;initial.scheduled=true;
 initial.threads.resize(3);initial.context.resize(3*0x40000);
 for(unsigned i=0;i<3;++i){initial.threads[i].id=17+i;initial.threads[i].number=i;}
 s.spu.groups.push_back(std::move(initial));auto& g=s.spu.groups.back();
 for(unsigned i=0;i<3;++i){auto p=std::make_unique<NativeSpuWorker>();
  p->parent=&pool;p->group_id=7;p->lane_thread_id=17+i;
  p->context=&g.threads[i];p->group=&g;p->pump=[&]{pool.service();};
  pool.lanes.push_back(std::move(p));}
 auto& caller=*pool.lanes[0];auto& peer=*pool.lanes[1];auto& unrelated=*pool.lanes[2];
 std::atomic<bool> done{false},release{false},entered{false};
 std::atomic<unsigned> ping{0},pong{0};
 unrelated.step=[&](unsigned){unrelated.event_driven=true;entered=true;
  while(!release.load()){if(ping.load()!=pong.load())pong=ping.load();std::this_thread::yield();}return false;};
 [[maybe_unused]] auto exchange=[&](unsigned n){ping=n;while(pong.load()!=n)std::this_thread::yield();};
'''

SIGNALS = SETUP + r'''
 s.events.queues.emplace_back(0x8d000000,4,0);g.threads[0].ports[3]=0x8d000000;
 std::atomic<bool> signaled{false};
 peer.step=[&](unsigned){peer.event_driven=true;std::uint32_t value;
  while(!g.threads[1].snr[0].pop(value))std::this_thread::yield();
  assert(value==0x12345678);signaled=true;
  while(!release.load()){std::this_thread::yield();}return false;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;auto& t=g.threads[0];auto* ls=g.context.data();
  while(!entered.load())std::this_thread::yield();
  ls[0]=0x12;ls[1]=0x34;ls[2]=0x56;ls[3]=0x78;
  // Production call-site descriptor and original channel backend.
  spu_transfer(s,t,0xf015400c,ls,4,true);
  while(!signaled.load())std::this_thread::yield();
  assert(spu_owner_call(s,t,[&]{
   assert(peer.pauses==0 && unrelated.pauses==0);exchange(1);
  },{SpuOwnerOperation::Channel,30,0x83000001}));
  for(unsigned n=0;n<2;++n){
   spu_write_channel(s,t,ls,28,0xaabbcc00+n);
   spu_write_channel(s,t,ls,30,0x03000010+n);
   assert(spu_read_channel(s,t,29)==0); // completed event reply
  }
  done=true;return false;};
 peer.launch();unrelated.launch();caller.launch();
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 release=true;pool.shutdown();
 assert(s.events.queues[0].data.size()==2);
 const auto first=s.events.queues[0].data[0],second=s.events.queues[0].data[1];
 assert((first==EventQueueState::Event{UINT64_C(0xffffffff53505501),17,UINT64_C(0x300000010),0xaabbcc00}));
 assert(second[2]==UINT64_C(0x300000011) && second[3]==0xaabbcc01);
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,2}].calls==1));
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,2}].paused_lanes==0));
 assert((pool.rpc_metrics[{SpuOwnerOperation::Channel,0x1e03}].calls==2));
 for(const auto& entry:pool.rpc_metrics)assert(entry.second.paused_lanes==0);
 assert(!pool.refresh_pending);pool.perf_report(std::chrono::steady_clock::now());
 std::puts("PASS BE SNR, sibling progress during owner RPC, event FIFO and mailbox replies");
'''

LS_TARGET = SETUP + r'''
 peer.step=[&](unsigned){return true;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;while(!entered.load())std::this_thread::yield();
  auto& t=g.threads[0];auto* ls=g.context.data();
  ls[0x80]=0xde;ls[0x81]=0xad;ls[0x82]=0xbe;ls[0x83]=0xef;
  spu_transfer(s,t,0xf0100100,ls+0x80,4,true);
  assert(spu_owner_call(s,t,[&]{
   {std::lock_guard<std::mutex> lock(peer.mutex);assert(peer.pauses==1 && !peer.active);}
   assert(unrelated.pauses==0);
   exchange(1);spu_transfer(s,t,0xf0100100,ls+0x90,4,false);
  },{SpuOwnerOperation::Transfer,0xf0100100,4,false}));
  assert(spu_load_word(ls+0x90)==0xdeadbeef);
  spu_transfer(s,t,0xf0000200,ls+0x80,4,true); // self LS needs no pause
  assert(spu_load_word(ls+0x200)==0xdeadbeef);
  done=true;return false;};
 peer.launch();unrelated.launch();caller.launch();
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 release=true;pool.shutdown();
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,1}].calls==2));
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,1}].paused_lanes==1));
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,0}].calls==1));
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,0}].paused_lanes==1));
 pool.perf_report(std::chrono::steady_clock::now());
 std::puts("PASS peer LS DMA acknowledges only target; self LS has no pause; BE payload exact");
'''

CONTROL = SETUP + r'''
 peer.step=[&](unsigned){return false;};
 unrelated.step=[&](unsigned){return false;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;
  assert(spu_owner_call(s,g.threads[0],[&]{
   for(auto* lane:{&caller,&peer,&unrelated}){
    std::lock_guard<std::mutex> lock(lane->mutex);assert(lane->pauses>=1 && !lane->active);
   }
  },{SpuOwnerOperation::Stop,0x101}));
  bool exact=false;
  try{spu_transfer(s,g.threads[0],0xe0000000,g.context.data(),4,true);}
  catch(const std::runtime_error& e){exact=std::string(e.what())=="SPU DMA to raw SPU MMIO is not implemented";}
  assert(exact);
  g.threads[0].out_mailbox=0x87654321;g.threads[0].out_mailbox_full=true;
  bool exited=false;try{spu_stop(s,g.threads[0],0x101);}catch(const SpuSliceEnd&){exited=true;}
  assert(exited && g.state==1 && g.join_cause==1 && g.exit_status==0x87654321);
  for(const auto& t:g.threads)assert(t.stopped);
  done=true;return false;};
 peer.launch();unrelated.launch();caller.launch();
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 pool.shutdown();assert((pool.rpc_metrics[{SpuOwnerOperation::Stop,0x101}].paused_lanes==6));
 assert((pool.rpc_metrics[{SpuOwnerOperation::Transfer,4}].paused_lanes==3));
 pool.perf_report(std::chrono::steady_clock::now());
 std::puts("PASS STOP retains full-group pause and unknown MMIO preserves exact stop");
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Par15RpcContracts(unittest.TestCase):
    def native(self, name, body):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp=ARTIFACTS/(name+'.cpp');binary=cpp.with_suffix('')
        code=source(body,full=True)
        # Integration call-site hooks, kept out of read-only spu.py.
        calls=(
          ('spu_owner_call(s,t,[&]{spu_transfer(s,t,address,local,size,put);})',
           'spu_owner_call(s,t,[&]{spu_transfer(s,t,address,local,size,put);},{SpuOwnerOperation::Transfer,address,size,put})'),
          ('spu_owner_call(s,t,[&]{spu_stop(s,t,code);})',
           'spu_owner_call(s,t,[&]{spu_stop(s,t,code);},{SpuOwnerOperation::Stop,code})'),
          ('spu_owner_call(s,t,[&]{spu_write_channel(s,t,ls,channel,value);})',
           'spu_owner_call(s,t,[&]{spu_write_channel(s,t,ls,channel,value);},{SpuOwnerOperation::Channel,channel,value})'))
        for old,new in calls:code=code.replace(old,new)
        cpp.write_text(code)
        command=['c++','-std=c++17',*shlex.split(os.environ.get('ANYPS3_PAR15_CXXFLAGS','-O1')),
                 '-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(binary)]
        build=subprocess.run(command,capture_output=True,text=True,timeout=300)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode,0,build.stderr[-14000:])
        env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',
                 ANYPS3_SPU_THREADS='0',ANYPS3_SPU_WORKER='0',ANYPS3_RSX_WORKER='0',ANYPS3_PERF_REPORT='0')
        run=subprocess.run([str(binary.resolve())],env=env,capture_output=True,text=True,timeout=15)
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode,0,run.stderr[-14000:])

    def test_signal_mailbox_and_sibling_progress(self):self.native('signals',SIGNALS)
    def test_ls_dma_pauses_only_target(self):self.native('ls-target',LS_TARGET)
    def test_control_and_unknown_keep_conservative_pause(self):self.native('control',CONTROL)

if __name__=='__main__':unittest.main()
