"""PAR9 attention, failure and reservation-watch native contracts."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source

ARTIFACTS=Path('local/ratchet/codex/par9/tests')

def native_code(body,*,ppu=(),imports=()):
    from anyps3.program import compile_program
    from anyps3.elf import ElfImage
    from tests.test_anyps3 import BLR,program
    translated=compile_program(ElfImage.parse(program(*(ppu or (BLR,)))),process_entry=True,additional_imports=imports)
    return translated.cpp().replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')+r'''
#include <cassert>
template<class F>void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){(void)crc32;
'''+body+'\n}\n'


ATTENTION = r'''
 NativeSpuWorker pool;pool.pool_mode=true;pool.enabled=true;
 unsigned refreshes=0,pumps=0;pool.refresh=[&]{++refreshes;};pool.pump=[&]{++pumps;};
 for(unsigned i=0;i<6;++i){auto p=std::make_unique<NativeSpuWorker>();p->parent=&pool;p->group_id=i+1;pool.lanes.push_back(std::move(p));}
 pool.request(1024);assert(refreshes==1 && pumps==1);
 // An idle tick must not take *any* lane/owner/supervisor mutex or call pump.
 {std::vector<std::unique_lock<std::mutex>> locks;
  for(auto& p:pool.lanes)locks.emplace_back(p->mutex);
  std::unique_lock<std::mutex> parent(pool.mutex); // Same lane -> parent order as owner_call.
  for(unsigned i=0;i<62;++i){auto r=pool.request(1024);assert(!r.completed && !r.progress);}
 }
 assert(refreshes==1 && pumps==1);
 pool.request(1024);assert(refreshes==2 && pumps==2);
 pool.pool_completed=42;assert(pool.request(1024).progress);assert(!pool.request(1024).progress);
 // A publication during service survives the consumer exchange.
 pool.attention=true;pool.pump=[&]{++pumps;pool.attention=true;};pool.request(1024);assert(pool.attention.load());
 pool.pump=[&]{++pumps;};pool.request(1024);assert(!pool.attention.load());
 // Empty audio ticks preserve sequence and never inspect the owner queue.
 PpuState inactive;inactive.process.spu_worker.enabled=true;inactive.process.spu_worker.pool_mode=true;
 const auto seq=inactive.supervisor.sequence.load();
 {std::unique_lock<std::mutex> held(inactive.process.spu_worker.mutex);
  assert(!spu_run(inactive,1024));auto reply=engine_step(inactive,{ExecutionEngine::Audio,0});assert(reply.completed && !reply.progress);}
 assert(inactive.supervisor.sequence==seq+1);
 // A lane admitted after a non-default quantum inherits that budget.
 NativeSpu::Group suspended;suspended.id=17;suspended.state=4;suspended.count=1;
 suspended.context.resize(0x40000);suspended.threads.emplace_back();suspended.threads.back().id=19;
 inactive.spu.groups.push_back(std::move(suspended));
 auto& iw=inactive.process.spu_worker;iw.pool_budget=17;iw.pauses=1;
 iw.refresh=[&]{spu_pool_refresh(inactive);};iw.refresh_pending=false;
 {SpuOwnerScope control(inactive,17,false,false);assert(iw.refresh_pending);
  spu_run(inactive,0);assert(iw.lanes.size()==1 && iw.lanes[0]->budget==17);}

 iw.shutdown();
 // A blocking scheduler entry must refresh even without 64 PPU ticks.
 PpuState sched;auto& sw=sched.process.spu_worker;sw.enabled=true;sw.pool_mode=true;
 unsigned schedules=0;sw.refresh=[&]{++schedules;};sw.refresh_pending=false;
 sched.pc=0x10000;ppu_schedule(sched,true);assert(schedules==1);
 // One real RPC from each lane; no timeout polling generates wakeups.
 std::atomic<unsigned> done{0};unsigned value=0;
 for(auto& p:pool.lanes){auto* lane=p.get();lane->step=[&,lane](unsigned){lane->event_driven=true;
  if(!lane->completed.load()){lane->owner_call([&]{++value;});++done;}return false;};lane->launch();}
 auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(3);
 while(done.load()!=6 && std::chrono::steady_clock::now()<limit){pool.request(1024);std::this_thread::yield();}
 assert(done==6 && value==6 && pool.owner_pending==0);
 for(auto& p:pool.lanes){p->pause();}
 assert(pool.pool_completed==42); // Six parked false quanta are not guest progress.
 for(auto& p:pool.lanes){p->resume();}
 pool.shutdown();
 std::puts("PASS empty tick holds every mutex, bounded refresh, concurrent RPC publication");
'''

RESERVATION = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,4096,6,nullptr,0);
 m.regions.push_back({0x20000,6,m.regions[0].bytes});m.enable_concurrency();
 NativeSpuWorker lane;NativeSpuWorker::current=&lane;
 NativeSpu::Thread t;t.id=1;t.event_mask=0x400;t.decrementer_running=false;
 std::uint8_t bytes[128]{};m.getllar(0x10000,bytes,t.reservation);t.reservation_address=0x10000;
 lane.watch_reservation(m,t.reservation.first);spu_collect_events(s,t);assert(!t.event_ready);
 for(unsigned i=0;i<1000;++i)spu_collect_events(s,t);
 // No observer covers the line: only the first check compares; the published-change counters prove that
 // nothing changed since (test_spu_reservation_filter pins the exactness of that claim).
 assert(token_calls==1 && !t.event_ready);
 m.store(0x20080,4,7);spu_collect_events(s,t);assert(!t.event_ready);
 m.store(0x20000,4,0);spu_collect_events(s,t);assert(t.event_ready && t.event_status==0x400);
 // A future observer-backed write is not a CPU store notification yet. The
 // LR query must still trigger coherence, not trust a clean listener flag.
 t.event_ready=false;t.event_status=0;m.getllar(0x10000,bytes,t.reservation);t.reservation_address=0x10000;
 spu_collect_events(s,t);bool materialize=false;unsigned callbacks=0;
 {GuestMemory::RawPermit metadata(m);
  m.host_access_observer=[&](const void*,std::uint64_t,bool){++callbacks;
   if(materialize){materialize=false;GuestMemory::ObserverPause pause(m);m.store(0x20000,4,0x11223344);}
  };
 }
 // A synchronous observer on the line forbids the shortcut: every check compares (and so runs coherence).
 {const auto before=token_calls;for(unsigned i=0;i<10;++i)spu_collect_events(s,t);assert(token_calls==before+10 && !t.event_ready);}
 materialize=true;spu_collect_events(s,t);
 assert(callbacks>0 && !materialize && t.event_ready && t.event_status==0x400);
 assert(m.load(0x10000,4)==0x11223344);
 {GuestMemory::RawPermit metadata(m);m.host_access_observer=GuestMemory::HostObserver{};}
 t.event_ready=false;t.event_status=0;m.getllar(0x10080,bytes,t.reservation);t.reservation_address=0x10080;
 spu_collect_events(s,t);assert(!t.event_ready);m.mapping_changed();spu_collect_events(s,t);assert(t.event_ready);
 NativeSpuWorker::current=nullptr;lane.reservation_watch.reset();assert(m.parallel->notifications->count==0);
 std::puts("PASS LR alias/same-value/map and mandatory observer coherence; exact change-counter shortcut only without observer");
'''

FAILURE = r'''
 ExecutionSupervisor supervisor;std::vector<std::thread> producers;
 std::vector<std::uint64_t> seq(60000);
 for(unsigned i=0;i<6;++i)producers.emplace_back([&,i]{for(unsigned n=0;n<10000;++n)seq[i*10000+n]=supervisor.next_sequence();});
 for(auto& t:producers){t.join();}std::sort(seq.begin(),seq.end());
 for(unsigned i=0;i<60000;++i){assert(seq[i]==i+1);}assert(supervisor.sequence==60000);
 setenv("ANYPS3_SPU_THREADS","1",1);PpuState s;SpuWorkerSession life(s);auto& w=s.process.spu_worker;
 auto p=std::make_unique<NativeSpuWorker>();p->parent=&w;p->failure=w.failure;
 p->guest_pc=0x1234;p->guest_thread=0x5678;p->step=[](unsigned)->bool{throw std::runtime_error("par9 exact failure");};
 auto* lane=p.get();w.lanes.push_back(std::move(p));lane->launch();
 bool exact=false;auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(3);
 while(!exact && std::chrono::steady_clock::now()<limit){try{w.request(1024);}catch(const std::runtime_error& e){exact=std::string(e.what())=="par9 exact failure";}}
 assert(exact && s.supervisor.failed_pc==0x1234 && s.supervisor.failed_thread==0x5678);
 const auto first=s.supervisor.first_error;
 for(unsigned n=0;n<8;++n){bool repeated=false;
  try{w.request(1024);}catch(const std::runtime_error& e){repeated=std::string(e.what())=="par9 exact failure";}
  assert(repeated && s.supervisor.first_error==first && s.supervisor.failed_pc==0x1234 && s.supervisor.failed_thread==0x5678);
 }
 w.shutdown();
 std::puts("PASS atomic unique sequences and first-error provenance");
'''


TIMER = r'''
 assert(spu_atomic_syscall(141) && spu_atomic_syscall(142));
 setenv("ANYPS3_SPU_THREADS","1",1);PpuState s;s.memory.map(0x10000,4096,6,nullptr,0);SpuWorkerSession life(s);
 std::atomic<bool> ready{false},release{false},expired{false};
 std::thread reader([&]{GuestMemory::Lease bytes(s.memory,0x10000,4,false);ready=true;
  const auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(2);
  while(!release.load() && std::chrono::steady_clock::now()<limit)std::this_thread::yield();
  if(!release.load())expired=true;
 });
 while(!ready.load())std::this_thread::yield();
 for(unsigned op:{141u,142u}){
  s.r[11]=op;s.r[3]=op==141?30:1;bool blocked=false;
  {SpuServiceScope service(s,spu_atomic_syscall(op));assert(!s.memory.local_state().raw);
   try{syscall(s);}catch(const PpuBlocked&){blocked=true;}}
  assert(blocked && s.wait_kind==12 && s.sync_ext.waits.at(s.thread_id).kind==12);
  assert(s.sync_ext.waits.at(s.thread_id).deadline>0);
  s.sync_ext.waits.clear();s.wait_kind=0;
 }
 release=true;reader.join();assert(!expired);life.finish();
 std::puts("PASS usleep/sleep publish real timed waits without excluding an active reader");
'''


DEFERRED_ENTRY = r'''
 for(unsigned concurrent:{0u,1u}){
  setenv("ANYPS3_SPU_THREADS",concurrent?"1":"0",1);
  PpuState s;s.pc=0x10000;s.lr=RETURN_SENTINEL;block_throws=0;
  anyps3_entry(s,false);
  assert(s.returned && s.steps==5 && s.r[3]==0 && s.wait_kind==0);
  assert(!s.ppu_block_pending && !s.ppu_defer_allowed && s.sync_ext.waits.empty());
  assert(block_throws==(concurrent?0u:1u));
 }
 std::puts("PASS actual AOT usleep retries at the same PC, exact steps and zero parallel throws");
'''

DEFERRED_OBJECTS = r'''
 setenv("ANYPS3_SPU_THREADS","1",1);PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);SpuWorkerSession life(s);
 PpuContext peer;peer.thread_id=2;s.ppu_threads={static_cast<PpuContext&>(s),peer};
 s.events.queues.push_back({17,2,0,{}});s.r[11]=130;s.r[3]=17;s.r[5]=100000;
 s.pc=0x2220;{PpuDeferredScope disposition(s);s.r[3]=event_syscall(s);s.pc=0xdeadbeef;}
 assert(s.ppu_block_pending && !s.ppu_defer_allowed && s.pc==0x2220 && s.r[3]==17 && s.wait_kind==1);
 s.ppu_block_pending=false;s.wait_kind=0;
 s.events.queues[0].data.push_back({UINT64_C(0x1122334455667788),0x99,0xaa,0xbb});
 {PpuDeferredScope disposition(s);s.r[3]=event_syscall(s);}
 assert(!s.ppu_block_pending && s.r[3]==0 && s.r[4]==UINT64_C(0x1122334455667788) && s.r[7]==0xbb && s.events.queues[0].data.empty());
 {SpuServiceScope service(s);
  m.store(0x10200,4,2);m.store(0x10204,4,16);s.r[3]=0x10100;s.r[4]=0x10200;lwmutex_create(s);
  s.r[3]=0x10300;s.r[4]=0x10100;s.r[5]=0x10400;assert(lwcond_service(s,0)==0);
  s.r[3]=0x10100;s.r[4]=0;lwmutex_lock(s);s.r[3]=0x10100;lwmutex_lock(s);
  s.r[3]=0x10300;s.r[4]=100000;s.pc=0x3330;
  {PpuDeferredScope disposition(s);s.r[3]=lwcond_service(s,2);s.pc=0xdeadbeef;}
  assert(s.ppu_block_pending && s.pc==0x3330 && s.r[3]==0x10300 && s.wait_kind==8 && s.wait_depth==1);
  assert(m.load(0x10100,4)==UINT32_MAX && m.load(0x1010c,4)==0);
  s.ppu_block_pending=false;s.wait_kind=0;s.resume_lwcond=0x10300;m.store(0x10100,4,2);
  {PpuDeferredScope disposition(s);s.r[3]=lwcond_service(s,2);}
  assert(s.ppu_block_pending && s.wait_kind==7 && s.resume_lwcond==0x10300 && s.r[3]==0x10300 && s.r[4]==100000);
  assert(m.load(0x10104,4)==1 && m.load(0x1010c,4)==0);
  s.ppu_block_pending=false;s.wait_kind=0;m.store(0x10100,4,UINT32_MAX);
  {PpuDeferredScope disposition(s);s.r[3]=lwcond_service(s,2);}
  assert(!s.ppu_block_pending && s.r[3]==0 && s.r[4]==100000 && !s.resume_lwcond && !s.lwmutex_waiting);
  assert(m.load(0x10100,4)==1 && m.load(0x10104,4)==0 && m.load(0x1010c,4)==1);
 }
 life.finish();std::puts("PASS event payload, condition release/reacquire, recursion, one waiter and arguments");
'''

DEFERRED_IMPORT = r'''
 setenv("ANYPS3_SPU_THREADS","1",1);PpuState s;auto& m=s.memory;m.map(0x30000,4096,6,nullptr,0);SpuWorkerSession life(s);
 PpuContext peer;peer.thread_id=2;s.ppu_threads={static_cast<PpuContext&>(s),peer};
 s.mutexes.insert(0x30000);m.store(0x30000,4,2);m.store(0x30004,4,0);m.store(0x30008,4,18);
 s.pc=0x10000;s.r[1]=0x30200;s.r[2]=0x12345678;s.r[3]=0x30000;s.r[4]=0;s.lr=UINT64_C(0x100000004);
 page_10(s); // An invalid return LR must not be examined while blocked.
 assert(s.ppu_block_pending && !s.ppu_defer_allowed && s.pc==0x10000 && s.r[3]==0x30000 && s.steps==1);
 assert(m.load(0x30228,8)==0x12345678 && m.load(0x30004,4)==1);
 s.ppu_block_pending=false;s.wait_kind=0;m.store(0x30000,4,UINT32_MAX);s.lr=0x10004;
 page_10(s);assert(!s.ppu_block_pending && s.pc==0x10004 && s.r[3]==0 && s.steps==2);
 assert(m.load(0x30000,4)==1 && m.load(0x30004,4)==0);life.finish();
 std::puts("PASS actual HLE wrapper preserves blocked PC/args and defers invalid return target validation");
'''

@unittest.skipUnless(shutil.which('c++'),'requires C++')
class Par9Contracts(unittest.TestCase):
    def native(self,name,body,transform=None,*,ppu=(),imports=()):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        code=native_code(body,ppu=ppu,imports=imports)
        if transform:code=transform(code)
        cpp=ARTIFACTS/(name+'.cpp');cpp.write_text(code);exe=cpp.with_suffix('')
        build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(exe)],capture_output=True,text=True,timeout=300)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode,0,build.stderr[-10000:])
        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=30,env={**os.environ,'ANYPS3_SPU_THREADS':'0','ANYPS3_SPU_WORKER':'0','ANYPS3_RSX_WORKER':'0','ANYPS3_PERF_REPORT':'0'})
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode,0,run.stderr[-10000:])
    def test_aot_block_disposition_and_serial_exception_parity(self):
        def count(code):return code.replace('struct PpuBlocked {};','static unsigned block_throws=0;struct PpuBlocked {PpuBlocked(){++block_throws;}};')
        self.native('deferred-entry',DEFERRED_ENTRY,count,ppu=(0x3960008d,0x3860001e,0x44000002,0x4e800020))
    def test_deferred_event_and_condition_reacquire(self):self.native('deferred-objects',DEFERRED_OBJECTS)
    def test_deferred_import_return_target(self):
        self.native('deferred-import',DEFERRED_IMPORT,ppu=(0x4e800020,0x4e800020),imports=({'library':'sysPrxForUser','functions':[{'nid':'0x1573dc3f','stub':0x10000}]},))
    def test_rsx_request_pumps_coordinator_once(self):
        self.native('rsx-one-pump',r'''
 setenv("ANYPS3_SPU_THREADS","0",1);setenv("ANYPS3_RSX_WORKER","1",1);
 PpuState s;SpuWorkerSession life(s);unsigned pumps=0;
 s.process.rsx_worker.queue->coordinator_service=[&]{++pumps;};
 auto reply=engine_step(s,{ExecutionEngine::Rsx,1024});
 assert(!reply.completed && pumps==1);life.finish();
 std::puts("PASS one coordinator pump per asynchronous RSX request");
''')
    def test_timers_do_not_exclude_unrelated_reader(self):self.native('timer',TIMER)
    def test_empty_attention_and_six_lane_rpc(self):self.native('attention',ATTENTION)
    def test_atomic_sequences_and_failure(self):self.native('failure',FAILURE)
    def test_lr_watch_does_not_skip_observer_coherence(self):
        def instrument(code):
            target='s.memory.reservation_token(t.reservation_address)'
            # Only count the token probe in collect_events, not expected values.
            start=code.index('static void spu_collect_events(');end=code.index('// Local counters',start)
            return code[:start]+'static unsigned token_calls=0;\n'+code[start:end].replace(target,'(++token_calls,'+target+')')+code[end:]
        self.native('reservation',RESERVATION,instrument)
