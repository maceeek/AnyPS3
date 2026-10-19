"""Native parallel memory/SPU contracts; expectations use fixed payloads/counts."""
from dataclasses import replace
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage, Segment
from anyps3.program import compile_program
from anyps3.runtime import MEMORY_CPP
from tests.test_anyps3 import BLR, program

ARTIFACTS = Path('local/ratchet/codex/par3/par1-regression')


def source(body, *, full=False, words=()):
    if full:
        translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
        images = ()
        if words:
            payload = b''.join(w.to_bytes(4, 'big') for w in words)
            images = (ElfImage(payload, 32, 'big', 0, 2, 23, 0x100,
                               (Segment(0, 1, 7, 0, 0x100, len(payload), len(payload), 4),)),)
        code = replace(translated, spu_images=images).cpp()
        code = code.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
    else:
        code = '#include <cstdint>\n#include <cstdio>\n#include <cinttypes>\n' + MEMORY_CPP
    return code + r'''
#include <cassert>
#include <atomic>
template<class F>void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){(void)crc32;
''' + body + '\n}\n'


MEMORY_CASE = r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});
 m.enable_concurrency();
 fault([&]{m.access(0x10000,4,false);});
 constexpr unsigned count=8;std::atomic<unsigned> ready{0},wins{0};std::atomic<bool> go{false};
 std::vector<std::thread> threads;
 for(unsigned i=0;i<count;++i)threads.emplace_back([&,i]{
  std::pair<std::uintptr_t,std::uint64_t> token;
  assert(m.load_reserved(i&1?0x20000:0x10000,4,token)==0x11223344);
  ready.fetch_add(1);while(!go.load())std::this_thread::yield();
  if(m.store_conditional(i&1?0x20000:0x10000,4,0x55667788,token))wins.fetch_add(1);
 });
 m.store(0x10000,4,0x11223344);
 // Threads must observe the initialized word before participating.
 while(ready.load()!=count){std::this_thread::yield();}go=true;
 for(auto& t:threads){t.join();}assert(wins==1 && m.load(0x10000,4)==0x55667788);
 std::pair<std::uintptr_t,std::uint64_t> token;m.load_reserved(0x10000,4,token);
 std::thread aba([&]{m.store(0x20000,4,99);m.store(0x20000,4,0x55667788);});aba.join();
 assert(!m.store_conditional(0x10000,4,0,token)); // ABA, including physical alias
 std::array<std::uint8_t,128> bytes{};m.getllar(0x10000,bytes.data(),token);
 assert(bytes[0]==0x55 && bytes[1]==0x66 && bytes[2]==0x77 && bytes[3]==0x88);
 std::thread ordinary([&]{m.store(0x2007f,1,0xa5);});ordinary.join();
 assert(!m.putllc(0x10000,bytes.data(),token));
 bytes.fill(0x3c);assert(m.putllc(0x20000,bytes.data(),token,true));
 assert(m.load(0x10000,8)==UINT64_C(0x3c3c3c3c3c3c3c3c));
 // PPU and MFC share one compare/commit protocol: exactly one wins.
 wins=0;ready=0;go=false;threads.clear();
 for(unsigned i=0;i<count;++i)threads.emplace_back([&,i]{
  std::array<std::uint8_t,128> b{};std::pair<std::uintptr_t,std::uint64_t> t;
  if(i&1)m.getllar(0x10000,b.data(),t);else m.load_reserved(0x20000,4,t);
  ++ready;while(!go.load())std::this_thread::yield();
  if(i&1){b.fill(0x7e);if(m.putllc(0x10000,b.data(),t))++wins;}
  else if(m.store_conditional(0x20000,4,0xabcdef01,t))++wins;
 });
 while(ready!=count){std::this_thread::yield();}go=true;for(auto& t:threads){t.join();}assert(wins==1);
 // Repeated contended increments have an independently known final value.
 m.store(0x10000,4,0);threads.clear();
 for(unsigned i=0;i<count;++i)threads.emplace_back([&]{for(unsigned n=0;n<500;++n){
  for(;;){std::pair<std::uintptr_t,std::uint64_t> t;auto v=m.load_reserved(0x10000,4,t);if(m.store_conditional(0x10000,4,v+1,t))break;}
 }});
 for(auto& t:threads){t.join();}assert(m.load(0x10000,4)==4000);
 // Failed split DMA validates all bytes before commit or reservation changes.
 m.map(0x30000,8,6,nullptr,0);m.map(0x30008,8,4,nullptr,0);
 auto before=m.reservation_token(0x10000,4);std::array<std::uint8_t,16> input{};input.fill(0xff);
 fault([&]{m.transfer(0x30000,input.data(),16,true);});assert(m.load(0x30000,8)==0 && before==m.reservation_token(0x10000,4));
 // Pinning is not permission to use a revoked mapping.
 auto range=m.range(0x30000,8,false);m.map(0x40000,8,6,nullptr,0);std::array<std::uint8_t,8> output{};
 range.read(0,output.data(),8); // unrelated map keeps this range valid
 {GuestMemory::RawPermit raw(m);auto old=m.regions[m.regions.size()-3];
  m.regions.erase(m.regions.end()-3);m.regions.push_back({old.base,old.flags,old.bytes,old.origin});m.mapping_changed();}
 fault([&]{range.read(0,output.data(),8);}); // remapping identical backing has a new identity
'''
# Initialize before starting contenders (avoid a scheduling-dependent fixture).
MEMORY_CASE = MEMORY_CASE.replace(' constexpr unsigned count=8;', ' m.store(0x10000,4,0x11223344);\n constexpr unsigned count=8;').replace(' m.store(0x10000,4,0x11223344);\n // Threads', ' // Threads')


WORKER_CASE = r'''
 setenv("ANYPS3_SPU_WORKER","1",1);setenv("ANYPS3_SPU_MAX_STEPS","1000000000000",1);
 PpuState s;auto& mem=s.memory;mem.map(0x10000,4096,6,nullptr,0);
 NativeSpu::Group group;group.id=0x4000100;group.state=6;group.count=1;group.context.resize(0x40000);group.threads.emplace_back();
 auto& initial=group.threads[0];initial.id=0x100;initial.entry=0x100;
 const std::uint32_t words[]={0x1c004081,0x327fff80}; // ai r1,r1,1; br -4
 for(unsigned i=0;i<2;++i)for(unsigned b=0;b<4;++b)group.context[0x100+4*i+b]=words[i]>>(24-8*b);
 initial.initial_ls=std::make_shared<std::vector<std::uint8_t>>(group.context);group.state=1;
 s.spu.groups.push_back(std::move(group));spu_prepare(s,s.spu.groups[0].threads[0],s.spu.groups[0].context.data());
 SpuWorkerSession session(s);auto& worker=s.process.spu_worker;
 std::uint64_t first_steps=0;
 {SpuOwnerScope owner(s);s.r[11]=173;s.r[3]=s.spu.groups[0].id;assert(spu_syscall(s)==0 && s.spu.groups[0].threads[0].steps==1024);
  first_steps=s.spu.groups[0].threads[0].steps;assert(spu_run(s,100));
  assert(s.spu.groups[0].threads[0].steps==first_steps+100);
  assert(!spu_run(s,0) && s.spu.groups[0].threads[0].steps==first_steps+100);}
 // While coordinator performs real guest stores, SPU executes its AOT loop.
 for(unsigned i=0;i<10000;++i){mem.store(0x10000,4,i);assert(mem.load(0x10000,4)==i);}
 {SpuOwnerScope owner(s);auto& g=s.spu.groups[0];assert(g.threads[0].steps>first_steps+100);
  // Suspend/terminate and context access transfer only at acknowledged safe points.
  s.r[11]=177;s.r[3]=g.id;s.r[4]=123;assert(syscall_spu_ext(s));
 }
 {SpuOwnerScope owner(s);auto& g=s.spu.groups[0];assert(g.state==1 && g.join_cause && g.exit_status==123);
  auto n=g.threads[0].steps;spu_run(s,100);assert(g.threads[0].steps==n);
  s.r[11]=178;s.r[3]=g.id;s.r[4]=0x10010;s.r[5]=0x10014;assert(spu_syscall(s)==0);
  assert(mem.load(0x10010,4)==4 && mem.load(0x10014,4)==123);
 }
 // Safe map/alias removal during a running worker; no stale translation cache.
 {SpuOwnerScope owner(s);mem.map(0x20000,4096,6,nullptr,0);mem.regions.push_back({0x30000,6,mem.regions.back().bytes});mem.mapping_changed();}
 assert(mem.load(0x30000,4)==0);
 {SpuOwnerScope owner(s);mem.regions.erase(mem.regions.begin()+1);mem.mapping_changed();}
 fault([&]{mem.load(0x20000,4);});assert(mem.load(0x30000,4)==0);
 session.finish();assert(!worker.thread.joinable());
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class ParallelContracts(unittest.TestCase):
    def native(self, name, body, *, full=False, words=()):
        code = source(body, full=full, words=words)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS/(name+'.cpp')).write_text(code)
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'test.cpp').write_text(code)
            build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(p/'test.cpp'),'-o',str(p/'test')],capture_output=True,text=True,timeout=180)
            (ARTIFACTS/(name+'-build.log')).write_text(build.stdout+build.stderr)
            self.assertEqual(build.returncode,0,build.stderr)
            env={**os.environ,'ANYPS3_SPU_WORKER':'0','ANYPS3_PERF_REPORT':'0'}
            run=subprocess.run([str(p/'test')],capture_output=True,text=True,timeout=30,env=env)
            (ARTIFACTS/(name+'-run.log')).write_text(run.stdout+run.stderr)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_reservations_dma_aliases_aba_and_stale_ranges(self):
        self.native('memory',MEMORY_CASE)

    def test_actual_aot_worker_control_join_and_mapping(self):
        self.native('worker',WORKER_CASE,full=True,words=(0x1c004081,0x327fff80))

    def test_coherence_owner_queue_no_lost_wakeup_and_error(self):
        self.native('coherence',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 const auto owner=std::this_thread::get_id();unsigned reads=0,writes=0;
 m.host_access_observer=[&](const void*,std::uint64_t n,bool write){assert(std::this_thread::get_id()==owner && n==4);if(write)++writes;else ++reads;};
 std::atomic<bool> done{false};
 std::thread t([&]{for(unsigned i=0;i<100;++i){m.store(0x10000,4,i);assert(m.load(0x10000,4)==i);}done=true;});
 while(!done.load()){m.service_coherence();std::this_thread::yield();}t.join();assert(reads==100 && writes==100);
 m.host_access_observer=[](const void*,std::uint64_t,bool){throw std::runtime_error("exact coherence failure");};done=false;
 std::thread failure([&]{try{m.store(0x10000,4,99);assert(false);}catch(const std::runtime_error& e){assert(std::string(e.what())=="exact coherence failure");}done=true;});
 while(!done.load()){m.service_coherence();std::this_thread::yield();}failure.join();
 m.host_access_observer={};assert(m.load(0x10000,4)==99);
''')

    def test_worker_mailbox_snr_events_and_protected_dma(self):
        self.native('channels-guard',r'''
 setenv("ANYPS3_SPU_WORKER","1",1);PpuState s;auto& mem=s.memory;mem.map(0x10000,8192,6,nullptr,0);
 NativeSpu::Group g;g.id=7;g.state=1;g.context.resize(0x40000);g.threads.emplace_back();g.threads[0].id=0x100;g.threads[0].ports[7]=8;s.spu.groups.push_back(std::move(g));
 s.events.queues.push_back({8,1,0,{}});SpuWorkerSession session(s);auto& w=s.process.spu_worker;
 auto facade=std::make_shared<PpuState>(s.process);std::atomic<unsigned> consumed{0};
 {SpuOwnerScope owner(s);w.step=[&,facade](unsigned){
   auto& t=facade->spu.groups[0].threads[0];if(!t.signal_ready[0] || t.in_mailbox.empty())return false;
   const auto a=spu_read_channel(*facade,t,3),b=spu_read_channel(*facade,t,29);
   spu_write_channel(*facade,t,facade->spu.groups[0].context.data(),28,a^b);
   spu_write_channel(*facade,t,facade->spu.groups[0].context.data(),30,0x07000055);
   assert(spu_read_channel(*facade,t,29)==0);++consumed;return true;
 };}
 for(unsigned i=1;i<=100;++i){
  SpuOwnerScope owner(s);auto& t=s.spu.groups[0].threads[0];t.in_mailbox.push_back(0x12340000+i);
  s.r[11]=184;s.r[3]=t.id;s.r[4]=0;s.r[5]=i;assert(spu_syscall(s)==0);spu_run(s,1024);
  assert(consumed==i && !t.signal_ready[0] && t.in_mailbox.empty());auto& q=s.events.queues[0];assert(q.data.size()==1);
  assert(q.data[0][0]==UINT64_C(0xffffffff53505501) && q.data[0][1]==0x100 && q.data[0][2]==UINT64_C(0x700000055) && q.data[0][3]==(i^(0x12340000+i)));q.data.clear();
 }
 // A single PPU may block on an event produced by the SPU worker.
 bool blocked=false;try{SpuOwnerScope owner(s);s.r[11]=130;s.r[3]=8;s.r[5]=0;event_syscall(s);}catch(const PpuBlocked&){blocked=true;}assert(blocked && s.wait_kind==1);
 {SpuOwnerScope owner(s);auto& t=s.spu.groups[0].threads[0];t.in_mailbox.push_back(0xa5);t.signals[0]=0x5a;t.signal_ready[0]=true;s.spu.groups[0].state=6;}
 ppu_schedule(s,true);assert(!s.wait_kind);
 {SpuOwnerScope owner(s);assert(s.events.queues[0].data.size()==1 && s.events.queues[0].data[0][3]==0xff);s.events.queues[0].data.clear();s.spu.groups[0].state=1;}
 // A real foreign-thread DMA reaches the owner before touching PROT_NONE.
 // Synchronization callback runs on owner with no data/queue lock held.
 GuestMemory::HostGuard guard;const auto owner_tid=std::this_thread::get_id();unsigned flushes=0;
 auto backing=mem.regions[0].bytes;auto* protected_bytes=backing->data();
 {SpuOwnerScope owner(s);
  guard.synchronize=[&]{assert(std::this_thread::get_id()==owner_tid);guard.disarm();++flushes;};
  mem.host_access_observer=[&](const void* p,std::uint64_t n,bool){if(guard.touches_any(p,n))guard.synchronize();};
  assert(guard.add(protected_bytes,4096,*backing));guard.arm();consumed=0;
  w.step=[&,facade](unsigned){if(consumed.load())return false;std::array<std::uint8_t,4> b{{0x12,0x34,0x56,0x78}};spu_transfer_main(facade->memory,0x10000,b.data(),4,true);++consumed;return true;};
 }
 for(;;){SpuOwnerScope owner(s);if(consumed.load())break;spu_run(s,1024);}
 {SpuOwnerScope owner(s);assert(flushes==1 && mem.load(0x10000,4)==0x12345678);mem.host_access_observer={};w.step=[](unsigned){return false;};}
 session.finish();
''',full=True)

    def test_worker_sessions_relaunch_per_entry_and_failure_stays_stopped(self):
        # Build 322w stopped at step 2 with "SPU worker session already stopped":
        # resident module_start entries end their session before the game entry.
        self.native('relaunch',r'''
 setenv("ANYPS3_SPU_WORKER","1",1);setenv("ANYPS3_SPU_MAX_STEPS","1000000000000",1);
 PpuState s;NativeSpu::Group group;group.id=0x4000100;group.state=6;group.count=1;group.context.resize(0x40000);group.threads.emplace_back();
 auto& initial=group.threads[0];initial.id=0x100;initial.entry=0x100;
 const std::uint32_t words[]={0x1c004081,0x327fff80}; // ai r1,r1,1; br -4
 for(unsigned i=0;i<2;++i)for(unsigned b=0;b<4;++b)group.context[0x100+4*i+b]=words[i]>>(24-8*b);
 initial.initial_ls=std::make_shared<std::vector<std::uint8_t>>(group.context);group.state=1;
 s.spu.groups.push_back(std::move(group));spu_prepare(s,s.spu.groups[0].threads[0],s.spu.groups[0].context.data());
 {SpuWorkerSession module_start(s);module_start.finish();}
 assert(s.process.spu_worker.enabled && !s.process.spu_worker.thread.joinable());
 {SpuWorkerSession game(s);assert(s.process.spu_worker.thread.joinable());
  {SpuOwnerScope owner(s);s.r[11]=173;s.r[3]=s.spu.groups[0].id;assert(spu_syscall(s)==0);}
  const auto before=[&]{SpuOwnerScope owner(s);return s.spu.groups[0].threads[0].steps;}();
  for(unsigned i=0;i<200 && [&]{SpuOwnerScope owner(s);return s.spu.groups[0].threads[0].steps;}()==before;++i)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  {SpuOwnerScope owner(s);assert(s.spu.groups[0].threads[0].steps>before);}
  game.finish();}
 // A failed worker is not relaunched by a later entry; the original error stays.
 PpuState f;NativeSpu::Group g;g.state=6;g.context.resize(0x40000);g.threads.emplace_back();f.spu.groups.push_back(std::move(g));
 {SpuWorkerSession first(f);try{SpuOwnerScope owner(f);spu_run(f,1024);}catch(const std::runtime_error&){}}
 bool kept=false;try{SpuWorkerSession second(f);}catch(const std::runtime_error& e){kept=std::string(e.what())=="unknown native SPU image";}
 assert(kept && !f.process.spu_worker.thread.joinable());
''',full=True,words=(0x1c004081,0x327fff80))

    def test_worker_exception_propagates_and_joins(self):
        self.native('exception',r'''
 setenv("ANYPS3_SPU_WORKER","1",1);PpuState s;NativeSpu::Group g;g.state=6;g.context.resize(0x40000);g.threads.emplace_back();s.spu.groups.push_back(std::move(g));
 SpuWorkerSession session(s);bool stopped=false;
 try{SpuOwnerScope owner(s);spu_run(s,1024);}
 catch(const std::runtime_error& e){stopped=std::string(e.what())=="unknown native SPU image";}
 assert(stopped && s.supervisor.first_error);session.state.process.spu_worker.shutdown();assert(!s.process.spu_worker.thread.joinable());
 try{std::rethrow_exception(s.supervisor.first_error);}catch(const std::runtime_error& e){assert(std::string(e.what())=="unknown native SPU image");}
''',full=True)


if __name__=='__main__':
    unittest.main()
