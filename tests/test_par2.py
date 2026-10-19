"""PAR2 generated native contracts, with fixed counts/payloads as oracles."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source, MEMORY_CASE
from anyps3.flow import lift as lift_ppu

ARTIFACTS = Path('local/ratchet/codex/par3/par2-regression')
LOOP = (0x1c004081, 0x327fff80)  # ai r1,r1,1; br -4
PING = ((0xd<<21)|(3<<7)|1, (0xd<<21)|(29<<7)|2,
        (0x241<<21)|(2<<14)|(1<<7)|3,
        (0x10d<<21)|(28<<7)|3, (0x10d<<21)|(30<<7)|4,
        (0xd<<21)|(29<<7)|5, (0x64<<23)|((-6 & 0xffff)<<7))


VMX_CASE = r'''
 m.map(0x700000,4096,6,nullptr,0);threads.clear();
 for(unsigned id=0;id<6;++id)threads.emplace_back([&,id]{
  struct Cpu {GuestMemory& memory;std::array<std::array<std::uint8_t,16>,32> vr{};std::uint64_t r[32]{};std::uint32_t pc=0;};
  Cpu state{m};auto& r=state.r;r[4]=0x700000+id*128;r[5]=0;
  std::array<std::uint8_t,16> expected{{0,0x12,0x34,0x56,0x78,0x9a,0xbc,0xde,0xf0,1,2,3,4,5,6,7}};expected[0]=id;
  for(unsigned n=0;n<1000;++n){state.vr[3]=expected;
'''
VMX_CASE += '{'+lift_ppu((31<<26)|(3<<21)|(4<<16)|(5<<11)|(231<<1),0x800000).cpp+'}'
VMX_CASE += '{'+lift_ppu((31<<26)|(6<<21)|(4<<16)|(5<<11)|(103<<1),0x800004).cpp+'}'
VMX_CASE += r'''
   assert(state.vr[6]==expected && state.pc==0x800008);
  }
 });
 for(auto& t:threads)t.join();
'''


def groups(words, counts=(3, 3)):
    payload = ','.join(hex(w) for w in words)
    return r'''
 setenv("ANYPS3_SPU_THREADS","1",1);setenv("ANYPS3_SPU_WORKER","0",1);
 setenv("ANYPS3_SPU_MAX_STEPS","1000000000000",1);
 PpuState s;auto& mem=s.memory;mem.map(0x10000,8192,6,nullptr,0);
 const unsigned counts[]={''' + ','.join(map(str,counts)) + r'''};
 const std::uint32_t words[]={''' + payload + r'''};
 for(unsigned n=0;n<sizeof(counts)/sizeof(counts[0]);++n){
  NativeSpu::Group g;g.id=0x4000100+n*0x100;g.state=1;g.count=counts[n];g.priority=100;g.context.resize(0x40000*g.count);
  for(unsigned i=0;i<g.count;++i){NativeSpu::Thread t;t.id=(i<<24)|(g.id&0xffffff);t.number=i;t.entry=0x100;t.ports[7]=8;
   t.arguments[1]=UINT64_C(0x0700005500000000);
   auto ls=std::make_shared<std::vector<std::uint8_t>>(0x40000,0);
   for(unsigned w=0;w<sizeof(words)/sizeof(words[0]);++w)for(unsigned b=0;b<4;++b)(*ls)[0x100+4*w+b]=words[w]>>(24-8*b);
   t.initial_ls=ls;g.threads.push_back(std::move(t));}
  s.spu.groups.push_back(std::move(g));
 }
 SpuWorkerSession session(s);auto& worker=s.process.spu_worker;assert(worker.pool_mode);
 auto call=[&](unsigned op,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0){
  SpuServiceScope service(s);s.r[11]=op;s.r[3]=a;s.r[4]=b;s.r[5]=c;
  if(op==190 || op==174 || op==175 || op==177){assert(syscall_spu_ext(s));return std::uint32_t(s.r[3]);}
  return spu_syscall(s);
 };
 for(auto& g:s.spu.groups)assert(call(173,g.id)==0);
 [[maybe_unused]] auto until=[&](auto predicate){const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(10);
  while(!predicate()){worker.service();worker.check();if(std::chrono::steady_clock::now()>deadline)throw std::runtime_error("PAR2 native watchdog");std::this_thread::yield();}
 };
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Parallel2Contracts(unittest.TestCase):
    def native(self, name, body, *, words=(), full=False):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        code=source(body,full=full,words=words)
        cpp=ARTIFACTS/(name+'.cpp');exe=ARTIFACTS/name;cpp.write_text(code)
        build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(exe)],capture_output=True,text=True,timeout=240)
        (ARTIFACTS/(name+'-build.log')).write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode,0,build.stderr[-12000:])
        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=60,env={**os.environ,'ANYPS3_SPU_THREADS':'0','ANYPS3_SPU_WORKER':'0','ANYPS3_PERF_REPORT':'0'})
        (ARTIFACTS/(name+'-run.log')).write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode,0,run.stderr[-12000:])

    def test_atomic_memory_reservations_and_mapping(self):
        self.native('memory', MEMORY_CASE + r'''
 // Concurrent mapping publication against active readers, 128 allocations;
 // every load sees a complete, permission-checked immutable map snapshot.
 std::atomic<bool> finish{false};std::atomic<unsigned> reads{0};
 std::thread reader([&]{while(!finish.load()){assert(m.load(0x10000,4)==4000);++reads;}});
 while(!reads.load())std::this_thread::yield();
 for(unsigned i=0;i<128;++i){m.map(0x100000+i*4096,4096,6,nullptr,0);GuestMemory::RawPermit raw(m);m.regions.erase(m.regions.end()-1);m.mapping_changed();}
 finish=true;reader.join();assert(reads>0);
 std::uint8_t zero=0;m.transfer(UINT32_MAX,&zero,0,false);
 fault([&]{m.transfer(UINT64_C(0x100000000),&zero,0,false);});
 fault([&]{m.transfer(UINT64_MAX,&zero,0,true);});
 // Reuse TLS registrations beyond the bounded number of live reader slots.
 for(unsigned i=0;i<128;++i){std::thread once([&]{assert(m.load(0x10000,4)==4000);});once.join();}
 // One guest reservation can overlap two physical lines with a skewed
 // map base; writes to either physical half must invalidate the token.
 m.map(0x500001,256,6,nullptr,0);m.store(0x500084,4,0x12345678);
 std::pair<std::uintptr_t,std::uint64_t> skew;
 assert(m.load_reserved(0x500084,4,skew)==0x12345678);m.store(0x500080,1,1);
 assert(!m.store_conditional(0x500084,4,99,skew));
 m.load_reserved(0x500084,4,skew);m.store(0x5000ff,1,2);assert(!m.store_conditional(0x500084,4,99,skew));
 m.load_reserved(0x500084,4,skew);m.store(0x500084,4,0x12345678);assert(!m.store_conditional(0x500084,4,99,skew));
 // Eight independent byte writers share one atomic host word. Destination
 // stripes must protect the merge without dropping a neighbor's update.
 m.map(0x800000,4096,6,nullptr,0);threads.clear();
 for(unsigned id=0;id<8;++id)threads.emplace_back([&,id]{for(unsigned n=0;n<2000;++n)m.store(0x800000+id,1,id+1);});
 for(auto& t:threads){t.join();}assert(m.load(0x800000,8)==UINT64_C(0x0102030405060708));
 // Aligned atomic word cannot tear even against bulk/VMX accesses.
 m.store(0x10000,8,0);std::atomic<bool> done{false};std::thread writer([&]{for(unsigned i=0;i<20000;++i)m.store(0x10000,8,i&1?UINT64_C(0x5555555555555555):UINT64_C(0xaaaaaaaaaaaaaaaa));done=true;});
 while(!done.load()){const auto v=m.load(0x10000,8);assert(v==UINT64_C(0x5555555555555555)||v==UINT64_C(0xaaaaaaaaaaaaaaaa)||v==0);}writer.join();
''' + VMX_CASE)

    def test_six_independent_aot_workers_control_and_service_overlap(self):
        self.native('six-workers',groups(LOOP)+r'''
 assert(worker.lanes.size()==6);
 until([&]{SpuOwnerScope owner(s);return std::all_of(s.spu.groups.begin(),s.spu.groups.end(),[](const auto& g){return std::all_of(g.threads.begin(),g.threads.end(),[](const auto& t){return t.steps>=4096;});});});
 std::uint64_t before[6]{};{SpuOwnerScope owner(s);unsigned i=0;for(auto& g:s.spu.groups)for(auto& t:g.threads)before[i++]=t.steps;}
 // Hold an unrelated raw service scope for substantial PPU work. It does
 // not pause any SPU executor; only an actual shared-memory access waits.
 {SpuServiceScope service(s);volatile unsigned sum=0;for(unsigned i=0;i<1000000;++i)sum+=i;(void)sum;}
 until([&]{SpuOwnerScope owner(s);unsigned i=0;for(auto& g:s.spu.groups)for(auto& t:g.threads)if(t.steps<=before[i++])return false;return true;});
 const auto metrics=worker.metrics();assert(metrics.first>0 && metrics.second>0);
 const auto gid=s.spu.groups[0].id;assert(call(174,gid)==0);
 std::uint64_t suspended[3]{};{SpuOwnerScope owner(s);for(unsigned i=0;i<3;++i)suspended[i]=s.spu.groups[0].threads[i].steps;before[3]=s.spu.groups[1].threads[0].steps;}
 until([&]{SpuOwnerScope owner(s,s.spu.groups[1].id);return s.spu.groups[1].threads[0].steps>=before[3]+4096;});
 {SpuOwnerScope owner(s,gid);for(unsigned i=0;i<3;++i)assert(s.spu.groups[0].threads[i].steps==suspended[i]);}
 assert(call(175,gid)==0);until([&]{SpuOwnerScope owner(s,gid);return s.spu.groups[0].threads[0].steps>suspended[0];});
 for(auto& g:s.spu.groups){assert(call(177,g.id,123)==0);assert(call(178,g.id,0x10010,0x10014)==0);assert(mem.load(0x10010,4)==4 && mem.load(0x10014,4)==123);}
 // Destroy a terminal group while the session and sibling group handles
 // remain alive; subsequent engine ticks must ignore retired host lanes.
 const auto retired=s.spu.groups.front().id;assert(call(171,retired)==0);
 assert(s.spu.groups.size()==1);spu_run(s,1024);
 session.finish();assert(worker.lanes.empty());
''',full=True,words=LOOP)

    def test_mailbox_snr_and_event_pingpong_six_workers(self):
        self.native('pingpong',groups(PING)+r'''
 // Six independent sends fit; full FIFO EBUSY is verified separately.
 s.events.queues.push_back({8,8,0,{}});
 for(unsigned round=1;round<=100;++round){
  if(round==51){for(auto& g:s.spu.groups)assert(call(177,g.id,11)==0);for(auto& g:s.spu.groups)assert(call(173,g.id)==0);}
  for(auto& g:s.spu.groups)for(auto& t:g.threads){assert(call(190,t.id,0x12340000+round)==0);assert(call(184,t.id,0,round)==0);}
  unsigned received=0;std::vector<std::uint32_t> seen;
  until([&]{SpuServiceScope service(s);auto& q=s.events.queues[0];
   while(!q.data.empty()){const auto event=q.data.front();q.data.erase(q.data.begin());
    assert(event[0]==UINT64_C(0xffffffff53505501) && event[2]==UINT64_C(0x700000055));
    assert(event[3]==(round^(0x12340000+round)));assert(std::find(seen.begin(),seen.end(),std::uint32_t(event[1]))==seen.end());seen.push_back(event[1]);++received;}
   return received==6;
  });
 }
 for(auto& g:s.spu.groups)assert(call(177,g.id,77)==0);
 session.finish();
''',full=True,words=PING)

    def test_pool_protected_aot_dma_and_unrelated_group_progress(self):
        il=lambda r,v: 0x40800000|((v&0xffff)<<7)|r
        wr=lambda ch,r:(0x10d<<21)|(ch<<7)|r
        dma=(il(3,0x200),wr(16,3),0x41000084,wr(18,4),il(5,16),wr(19,5),il(6,0),wr(17,6),wr(20,6),il(7,0x20),wr(21,7),wr(28,6),0x102)
        # ILHU r4,1 gives EA 0x10000. The initial LS at 0x200 is zero.
        body=groups(dma,counts=(1,)).replace(' for(auto& g:s.spu.groups)assert(call(173,g.id)==0);', '')
        self.native('pool-guard',body+r'''
 GuestMemory::HostGuard guard;const auto owner_tid=std::this_thread::get_id();unsigned flushes=0;
 auto backing=mem.regions[0].bytes;mem.store(0x10000,8,UINT64_C(0xdeadbeefdeadbeef));
 guard.synchronize=[&]{assert(std::this_thread::get_id()==owner_tid);guard.disarm();++flushes;};
 {SpuServiceScope service(s);
  mem.host_access_observer=[&](const void* p,std::uint64_t n,bool){if(guard.touches_any(p,n))guard.synchronize();};
  assert(guard.add(backing->data(),4096,*backing));guard.arm();
 }
 assert(call(173,s.spu.groups[0].id)==0);
 until([&]{SpuServiceScope service(s);return s.spu.groups[0].join_cause!=0;});
 assert(flushes==1 && mem.load(0x10000,8)==0 && mem.load(0x10008,8)==0);
 {SpuServiceScope service(s);mem.host_access_observer={};}
 assert(call(178,s.spu.groups[0].id,0x10010,0x10014)==0);assert(mem.load(0x10010,4)==2 && mem.load(0x10014,4)==0);
 session.finish();
''',full=True,words=dma)

    def test_oversubscribed_group_priority_and_yield(self):
        self.native('priority',groups(LOOP,counts=(4,4))+r'''
 const auto first=s.spu.groups[0].id,second=s.spu.groups[1].id;
 {SpuOwnerScope control(s);s.spu.groups[0].priority=16;s.spu.groups[1].priority=200;worker.refresh();
  unsigned scheduled=0;for(auto& lane:worker.lanes)if(lane->scheduled.load()){++scheduled;assert(lane->group_id==first);}assert(scheduled==4);
 }
 std::uint64_t before=0;{SpuOwnerScope control(s,first);before=s.spu.groups[0].threads[0].steps;}
 until([&]{SpuOwnerScope control(s,first);return s.spu.groups[0].threads[0].steps>=before+2048;});
 {SpuServiceScope service(s);s.r[11]=179;s.r[3]=second;s.r[4]=16;assert(syscall_spu_ext(s) && s.r[3]==0);}
 {SpuServiceScope service(s);s.r[11]=176;s.r[3]=first;assert(syscall_spu_ext(s) && s.r[3]==0);}
 // Both equal-priority groups get hardware slots; an actual SPU loop in the
 // previously lower-priority group progresses after yield/timeslicing.
 until([&]{SpuOwnerScope control(s,second);return s.spu.groups[1].threads[0].steps>=4096;});
 for(auto& g:s.spu.groups){assert(call(177,g.id,1)==0);}
 session.finish();
''',full=True,words=LOOP)

    def test_waiting_group_receive_and_join(self):
        wr=lambda ch,r:(0x10d<<21)|(ch<<7)|r
        rd=lambda ch,r:(0xd<<21)|(ch<<7)|r
        words=(wr(28,3),0x110,rd(29,4),rd(29,5),rd(29,6),rd(29,7),wr(28,4),0x102)
        body=groups(words,counts=(2,)).replace('t.arguments[1]=UINT64_C(0x0700005500000000);','t.arguments[0]=UINT64_C(0x0000123400000000);t.queue_bindings[0]={0x1234,8};').replace(' for(auto& g:s.spu.groups)assert(call(173,g.id)==0);','')
        self.native('receive',body+r'''
 s.events.queues.push_back({8,2,0,{},2});const auto gid=s.spu.groups[0].id;
 assert(call(173,gid)==0);
 until([&]{SpuServiceScope service(s);return s.spu.groups[0].state==3;});
 // Original STOP receive suspends the whole group until a real event arrives.
 {SpuServiceScope service(s);s.events.queues[0].data.push_back({0,11,22,33});}
 until([&]{SpuServiceScope service(s);auto& g=s.spu.groups[0];return g.state==3 && s.events.queues[0].data.empty();});
 {SpuServiceScope service(s);s.events.queues[0].data.push_back({0,44,55,66});}
 until([&]{SpuServiceScope service(s);return s.spu.groups[0].join_cause==2;});
 {SpuOwnerScope owner(s,gid);std::vector<unsigned> values;for(auto& t:s.spu.groups[0].threads){assert(t.stopped && t.exit_status_ready && t.exit_status==0);values.push_back(spu_word(t.gpr[5],0));}std::sort(values.begin(),values.end());assert((values==std::vector<unsigned>{11,44}));}
 assert(call(178,gid,0x10010,0x10014)==0 && mem.load(0x10010,4)==2);session.finish();
''',full=True,words=words)

    def test_restart_while_rpc_is_parked_does_not_retire_old_frame(self):
        self.native('parked-restart',groups(PING,counts=(1,))+r'''
 s.events.queues.push_back({8,4,0,{}});const auto gid=s.spu.groups[0].id,tid=s.spu.groups[0].threads[0].id;
 assert(call(190,tid,0x12340001)==0);assert(call(184,tid,0,1)==0);
 // Deliberately do not pump the owner until the SPU published its event RPC.
 const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 for(;;){bool queued=false;{std::lock_guard<std::mutex> lock(worker.mutex);queued=!worker.owner_requests.empty();}if(queued)break;
  if(std::chrono::steady_clock::now()>deadline)throw std::runtime_error("parked RPC watchdog");
  std::this_thread::yield();}
 std::uint64_t completed=0;
 {SpuOwnerScope control(s,gid); // handles RPC, but keeps its AOT frame parked
  auto& lane=*worker.lanes[0];{std::lock_guard<std::mutex> lock(lane.mutex);completed=lane.completed;}
  assert(s.events.queues[0].data.size()==1);
  assert(call(177,gid,0)==0);assert(call(173,gid)==0);
  assert(s.spu.groups[0].threads[0].pc==0x100 && s.spu.groups[0].threads[0].steps==0);
 }
 until([&]{auto& lane=*worker.lanes[0];std::lock_guard<std::mutex> lock(lane.mutex);return lane.completed>completed;});
 {SpuOwnerScope control(s,gid);auto& t=s.spu.groups[0].threads[0];assert(t.pc==0x100 && t.steps==0 && t.in_mailbox.empty());}
 assert(call(177,gid,0)==0);session.finish();
''',full=True,words=PING)

    def test_owner_rpc_failure_preserves_original_context_before_control(self):
        body=groups(PING,counts=(1,)).replace('UINT64_C(0x0700005500000000)','UINT64_C(0x8000005500000000)')
        self.native('rpc-error',body+r'''
 const auto gid=s.spu.groups[0].id,tid=s.spu.groups[0].threads[0].id;
 assert(call(190,tid,1)==0);assert(call(184,tid,0,1)==0);
 const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 for(;;){bool queued=false;{std::lock_guard<std::mutex> lock(worker.mutex);queued=!worker.owner_requests.empty();}if(queued)break;
  if(std::chrono::steady_clock::now()>deadline)throw std::runtime_error("RPC error watchdog");
  std::this_thread::yield();}
 bool exact=false;try{SpuOwnerScope control(s,gid);}catch(const std::runtime_error& e){exact=std::string(e.what())=="unimplemented SPU interrupt mailbox command 128";}
 assert(exact && s.supervisor.first_error && s.supervisor.failed_thread==tid && s.supervisor.failed_pc==0x110);
 assert(s.spu.groups[0].state==6); // control did not silently reset/terminate the failed frame
 worker.shutdown();exact=false;
 try{SpuWorkerSession retry(s);}catch(const std::runtime_error& e){exact=std::string(e.what())=="unimplemented SPU interrupt mailbox command 128";}assert(exact);
''',full=True,words=PING)

    def test_legacy_owner_preflight_does_not_hold_gate_while_pausing_dma(self):
        self.native('legacy-owner-preflight',r'''
 setenv("ANYPS3_SPU_THREADS","0",1);setenv("ANYPS3_SPU_WORKER","1",1);
 PpuState s;s.memory.map(0x10000,4096,6,nullptr,0);SpuWorkerSession session(s);auto& w=s.process.spu_worker;
 std::atomic<unsigned> phase{0};std::atomic<bool> go{false};const auto original_pump=w.pump;
 {SpuOwnerScope control(s);
  s.memory.host_access_observer=[](const void*,std::uint64_t,bool){};
  w.pump=[&]{go=true;original_pump();};
  w.step=[&](unsigned){if(phase.load()==2)return false;phase=1;while(!go.load())std::this_thread::yield();s.memory.store(0x10000,4,0x11223344);phase=2;return true;};
 }
 while(phase.load()!=1)std::this_thread::yield();
 // Owner preflight enters the legacy pause with an active DMA producer.
 // The pump releases that producer; holding RawPermit here would deadlock.
 assert(s.memory.load(0x10000,4)==0x11223344 && phase.load()==2);
 {SpuOwnerScope control(s);s.memory.host_access_observer={};w.pump=original_pump;}
 session.finish();assert(!s.memory.parallel->owner_coherence && !s.memory.parallel->wake_owner);
''',full=True)

    def test_destroy_middle_group_preserves_running_neighbors(self):
        self.native('middle-destroy',groups(LOOP,counts=(1,1,1))+r'''
 auto* first=&s.spu.groups[0];auto* last=&s.spu.groups[2];const auto middle=s.spu.groups[1].id;
 std::uint64_t first_steps=0,last_steps=0;{SpuOwnerScope owner(s);first_steps=first->threads[0].steps;last_steps=last->threads[0].steps;}
 assert(call(177,middle,5)==0);assert(call(178,middle,0x10010,0x10014)==0);assert(call(171,middle)==0);
 assert(s.spu.groups.size()==2 && &s.spu.groups[0]==first && &s.spu.groups[1]==last);
 until([&]{SpuOwnerScope owner(s);return first->threads[0].steps>=first_steps+2048 && last->threads[0].steps>=last_steps+2048;});
 assert(call(177,first->id,0)==0);assert(call(177,last->id,0)==0);session.finish();
''',full=True,words=LOOP)

    def test_pool_exception_and_entry_restart(self):
        self.native('pool-exception',r'''
 setenv("ANYPS3_SPU_THREADS","1",1);PpuState s;
 {SpuWorkerSession module(s);module.finish();}
 NativeSpu::Group g;g.id=7;g.state=6;g.count=1;g.context.resize(0x40000);g.threads.emplace_back();g.threads[0].id=42;s.spu.groups.push_back(std::move(g));
 bool exact=false;
 {SpuWorkerSession game(s);const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
  try{for(;;){spu_run(s,1024);if(std::chrono::steady_clock::now()>until)throw std::runtime_error("exception watchdog");std::this_thread::yield();}}
  catch(const std::runtime_error& e){exact=std::string(e.what())=="unknown native SPU image";}
 }
 assert(exact && s.supervisor.first_error && s.supervisor.failed_thread==42);
 exact=false;try{SpuWorkerSession again(s);}catch(const std::runtime_error& e){exact=std::string(e.what())=="unknown native SPU image";}assert(exact);
''',full=True)

if __name__=='__main__':unittest.main()
