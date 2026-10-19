"""HLE admission contracts: fixed BE bytes/errors, actual owner queues, real CAS."""
from dataclasses import replace
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.flow import Operation
from anyps3.program import compile_program
from anyps3.services import IMPORTS, FAST_IMPORTS
from tests.test_anyps3 import BLR, program

NAMES = ['sys_ppu_thread_get_id', 'sys_time_get_system_time', 'sys_lwmutex_lock',
         'sys_lwmutex_unlock', '_sys_strlen', '_sys_strcmp', '_sys_strncmp',
         '_sys_memcpy', '_sys_memset', '_sys_memcmp', '_sys_memmove', '_sys_strcpy']


def translated():
    base = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
    bodies = {name: body for name, body in IMPORTS.values()}
    ops = []
    for index, name in enumerate(NAMES):
        body = 'state.memory.store(r[1]+40,8,r[2]); ' + bodies[name]
        if name == 'sys_lwmutex_lock':
            body += ' if(state.ppu_block_pending)return;'
        body += ' state.pc=checked_target(state.lr&~UINT64_C(3));'
        ops.append(Operation(0x30000 + 4*index, 0, name+' (single main thread)', body, ()))
    ops.append(Operation(0x30100, 0x44000002, 'sc',
                         'syscall(state);if(state.ppu_block_pending)return;state.pc=0x30104;', ()))
    return replace(base, operations=base.operations+tuple(ops))


MAIN = r'''
#include <cassert>
static void prepare(PpuState& s){
 s.execution.enabled=false;s.memory.map(0x10000,8192,6,nullptr,0);
 s.r[1]=0x11000;s.r[2]=0x12345678;s.lr=0x40000;
}
static void getid(PpuState& s){s.r[3]=0x10100;anyps3_op_30000(s);}
static void lock(PpuState& s){s.r[3]=0x10400;s.r[4]=0;anyps3_op_30008(s);}
static void unlock(PpuState& s){s.r[3]=0x10400;anyps3_op_3000c(s);}
static void mutex(PpuState& s,unsigned recursive=32){
 s.memory.store(0x10300,4,2);s.memory.store(0x10304,4,recursive);
 s.r[3]=0x10400;s.r[4]=0x10300;lwmutex_create(s);
}
static int semantics(){
 PpuState s;prepare(s);auto& m=s.memory;m.enable_concurrency();s.thread_id=0x12345678;
 getid(s);assert(s.r[3]==0 && s.pc==0x40000 && !m.local_state().raw);
 const unsigned char id[]={0,0,0,0,0x12,0x34,0x56,0x78};
 for(unsigned i=0;i<8;++i)assert(m.load(0x10100+i,1)==id[i]);
 assert(m.load(0x11028,8)==0x12345678);
 anyps3_op_30004(s);const auto first=s.r[3];anyps3_op_30004(s);assert(s.r[3]>=first);
 m.store(0x10200,8,UINT64_C(0x4162ff0000000000));m.store(0x10220,8,UINT64_C(0x4162010000000000));
 s.r[3]=0x10200;anyps3_op_30010(s);assert(s.r[3]==3);
 s.r[3]=0x10200;s.r[4]=0x10220;anyps3_op_30014(s);assert(s.r[3]==1);
 s.r[3]=0x10200;s.r[4]=0x10220;s.r[5]=3;anyps3_op_30018(s);assert(s.r[3]==254);
 s.r[3]=0;s.r[4]=0;s.r[5]=0;anyps3_op_30018(s);assert(s.r[3]==0);
 s.r[3]=0x10500;s.r[4]=0x10200;s.r[5]=4;anyps3_op_3001c(s);assert(s.r[3]==0x10500 && m.load(0x10500,4)==0x4162ff00);
 s.r[3]=0x10501;s.r[4]=0x5a;s.r[5]=2;anyps3_op_30020(s);assert(s.r[3]==0x10501 && m.load(0x10500,4)==0x415a5a00);
 s.r[3]=0x10200;s.r[4]=0x10220;s.r[5]=3;anyps3_op_30024(s);assert(s.r[3]==1);
 s.r[3]=0x10201;s.r[4]=0x10200;s.r[5]=3;anyps3_op_30028(s);assert(s.r[3]==0x10201 && m.load(0x10200,4)==0x414162ff);
 // Validated transfers span adjacent mappings. A bad full destination leaves it untouched.
 m.map(0x20000,4,6,nullptr,0);m.map(0x20004,4,6,nullptr,0);
 s.r[3]=0x20002;s.r[4]=0xab;s.r[5]=4;anyps3_op_30020(s);assert(m.load(0x20000,4)==0xabab && m.load(0x20004,4)==0xabab0000);
 s.r[3]=0x20006;s.r[4]=0x55;s.r[5]=4;bool bad=false;
 try{anyps3_op_30020(s);}catch(const std::runtime_error&){bad=true;}
 assert(bad && m.load(0x20004,4)==0xabab0000 && !m.local_state().raw);
 s.r[3]=0;bad=false;try{anyps3_op_30000(s);}catch(const std::runtime_error&){bad=true;}assert(bad);
 // Raw strcpy remains admitted when an actual RSX worker is present.
 setenv("ANYPS3_RSX_WORKER","1",1);rsx_worker_start(s);
 m.store(0x10200,4,0x61626300);s.r[3]=0x10600;s.r[4]=0x10200;anyps3_op_3002c(s);
 assert(m.load(0x10600,4)==0x61626300 && !m.local_state().raw);s.process.rsx_worker.stop();
 std::puts("PASS fixed BE id, return/TOC, time, strings, transfers/alias, faults, raw fallback");return 0;
}
static int mutex_case(){
 PpuState s;prepare(s);auto& m=s.memory;mutex(s);m.enable_concurrency();
 s.reservation_address=0x10700;s.reservation={123,456};s.cr=0x12345678;s.xer=0x87654321;
 lock(s);assert(s.r[3]==0 && m.load(0x10400,8)==UINT64_C(0x0000000100000000));
 assert(s.reservation_address==0x10700 && s.reservation==std::make_pair(std::uintptr_t(123),UINT64_C(456)) && s.cr==0x12345678 && s.xer==0x87654321);
 const unsigned char held[]={0,0,0,1,0,0,0,0};for(unsigned i=0;i<8;++i)assert(m.load(0x10400+i,1)==held[i]);
 lock(s);assert(s.r[3]==0x80010008 && m.load(0x10400,8)==UINT64_C(0x0000000100000000));
 s.thread_id=2;unlock(s);assert(s.r[3]==0x80010009 && m.load(0x10400,4)==1);
 s.thread_id=1;unlock(s);assert(s.r[3]==0 && m.load(0x10400,8)==UINT64_C(0xffffffff00000000));
 m.store(0x10408,4,18);lock(s);lock(s);assert(s.r[3]==0 && m.load(0x1040c,4)==1);
 unlock(s);assert(s.r[3]==0 && m.load(0x1040c,4)==0 && m.load(0x10400,4)==1);unlock(s);
 // Existing waiter accounting / cooperative scheduling is preserved on fallback.
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));PpuContext peer=static_cast<PpuContext&>(s);peer.thread_id=2;s.ppu_threads.push_back(peer);
 m.store(0x10400,4,2);bool blocked=false;
 try{lock(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==7 && s.wait_id==0x10400 && s.lwmutex_waiting==0x10400 && m.load(0x10404,4)==1);
 ppu_schedule(s,true);assert(s.thread_id==2);unlock(s);assert(s.r[3]==0 && s.reschedule);
 ppu_schedule(s);assert(s.thread_id==1);lock(s);assert(s.r[3]==0 && !s.lwmutex_waiting && m.load(0x10404,4)==0);unlock(s);
 m.store(0x10400,4,2);s.r[3]=0x10400;s.r[4]=500;blocked=false;
 try{anyps3_op_30008(s);}catch(const PpuBlocked&){blocked=true;}assert(blocked && m.load(0x10404,4)==1);
 s.wait_kind=0;s.wait_timed_out=true;s.r[3]=0x10400;s.r[4]=500;anyps3_op_30008(s);
 assert(s.r[3]==0x8001000b && !s.lwmutex_waiting && !s.wait_deadline && m.load(0x10404,4)==0 && m.load(0x10400,4)==2);
 s.r[3]=0x10900;bool bad=false;try{anyps3_op_30008(s);}catch(const std::runtime_error&){bad=true;}assert(bad && !m.local_state().raw);
 std::puts("PASS mutex BE, EDEADLK/EPERM, recursion, contended scheduling, timeout, architectural reservation");return 0;
}
static int atomic_case(){
 ProcessState process;PpuState a(process),b(process);prepare(a);mutex(a);auto& m=a.memory;
 m.regions.push_back({0x20000,6,m.regions[0].bytes});m.enable_concurrency();m.enable_page_broker();b.thread_id=2;
 b.r[1]=0x11080;b.lr=0x40000;
 std::pair<std::uintptr_t,std::uint64_t> before;m.load_reserved(0x20400,8,before);
 a.r[3]=0x10400;assert(lwmutex_fast(a,false));a.r[3]=0x10400;assert(lwmutex_fast(a,true));
 assert(!m.store_conditional(0x20400,8,UINT64_C(0x0000000300000000),before));
 std::atomic<bool> go{false};
 // The second contender uses the firmware's own reservation protocol on an alias,
 // not the HLE helper. The independently known final protected counter is 2000.
 std::thread spu([&]{while(!go.load())std::this_thread::yield();
  for(unsigned n=0;n<1000;++n){for(;;){std::pair<std::uintptr_t,std::uint64_t> t;
   if(m.load_reserved(0x20400,4,t)==UINT32_MAX && m.store_conditional(0x20400,4,2,t))break;
   std::this_thread::yield();}
   const auto v=m.load(0x20800,4);m.store(0x20800,4,v+1);
   for(;;){std::pair<std::uintptr_t,std::uint64_t> t;assert(m.load_reserved(0x20400,8,t)==UINT64_C(0x0000000200000000));
    if(m.store_conditional(0x20400,8,UINT64_C(0xffffffff00000000),t))break;}
  }m.release_reader_slot();});go=true;
 for(unsigned n=0;n<1000;++n){a.r[3]=0x10400;while(!lwmutex_fast(a,false)){a.r[3]=0x10400;std::this_thread::yield();}
  const auto v=m.load(0x10800,4);m.store(0x10800,4,v+1);a.r[3]=0x10400;
  while(!lwmutex_fast(a,true)){a.r[3]=0x10400;std::this_thread::yield();}}
 spu.join();assert(m.load(0x10800,4)==2000 && m.load(0x10400,8)==UINT64_C(0xffffffff00000000));
 const unsigned char expected[]={0,0,7,0xd0};for(unsigned i=0;i<4;++i)assert(m.load(0x10800+i,1)==expected[i]);
 std::puts("PASS HLE vs firmware reservation contender, physical alias, exactly 2000 critical sections");return 0;
}
static int rmw_coherence(){
 PpuState s;prepare(s);mutex(s);auto& m=s.memory;m.enable_concurrency();m.enable_page_broker();
 m.store(0x10400,4,17);std::vector<char> observed;bool materialize=true;
 {GuestMemory::RawPermit metadata(m);
  m.host_access_observer=[&](const void*,std::uint64_t,bool write){observed.push_back(write?'W':'R');
   if(!write && materialize){materialize=false;GuestMemory::ObserverPause pause(m);m.store(0x10400,8,UINT64_C(0xffffffff00000000));}
  };
  m.host_access_observer.watch(m.regions[0].bytes->data()+0x400,8);
 }
 s.r[3]=0x10400;assert(lwmutex_fast(s,false));assert(!materialize && m.load(0x10400,4)==1);
 assert(!observed.empty() && observed.front()=='R' && std::find(observed.begin(),observed.end(),'W')!=observed.end());
 s.r[3]=0x10400;assert(lwmutex_fast(s,true));
 {GuestMemory::RawPermit metadata(m);m.host_access_observer=GuestMemory::HostObserver{};m.regions[0].flags=4;m.mapping_changed();}
 s.r[3]=0x10400;bool bad=false;try{lwmutex_fast(s,false);}catch(const std::runtime_error&){bad=true;}assert(bad);
 assert(m.load(0x10400,8)==UINT64_C(0xffffffff00000000));
 {GuestMemory::RawPermit metadata(m);m.regions[0].flags=2;m.mapping_changed();}
 s.r[3]=0x10400;bad=false;try{lwmutex_fast(s,false);}catch(const std::runtime_error&){bad=true;}assert(bad);
 {GuestMemory::RawPermit metadata(m);m.regions[0].flags=6;m.mapping_changed();}
 assert(m.load(0x10400,8)==UINT64_C(0xffffffff00000000) && !m.local_state().raw);
 std::puts("PASS watched RMW reads coherence before writing; read/write permissions checked without commit");return 0;
}
static int progress_case(bool pool_mode){
 setenv("ANYPS3_RSX_WORKER","1",1);PpuState s;prepare(s);rsx_worker_start(s);auto& m=s.memory;
 auto& w=s.process.spu_worker;auto& q=*s.process.rsx_worker.queue;
 unsigned phase=0;std::atomic<bool> rpc_done{false};
 if(pool_mode){
  w.enabled=true;w.pool_mode=true;w.refresh_pending=false;
  auto lane=std::make_unique<NativeSpuWorker>();lane->parent=&w;lane->event_driven=true;
  auto* l=lane.get();l->step=[&](unsigned){l->owner_call([&]{assert(!m.local_state().raw && phase==0);phase=1;m.store(0x10200,4,0x11223344);});rpc_done=true;return false;};
  w.lanes.push_back(std::move(lane));l->launch();
  while(!w.owner_pending.load(std::memory_order_acquire))std::this_thread::yield();
  w.pump=[&]{rsx_worker_pump(s);m.service_coherence();};
 }else phase=1;
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{q.run_fifo=[&](unsigned){q.owner_call([&]{assert(!m.local_state().raw && phase==1);phase=2;m.store(0x10204,4,0xaabbccdd);});};});
 q.post(RsxOwnerQueue::Kind::RunFifo,{},1);while(!q.coordinator_pending.load(std::memory_order_acquire))std::this_thread::yield();
 hle_light_ticks=0;for(unsigned n=0;n<63;++n)getid(s);assert(phase==(pool_mode?0u:1u));getid(s);
 assert(phase==2 && m.load(0x10204,4)==0xaabbccdd && !m.local_state().raw && !w.pauses);
 if(pool_mode){assert(m.load(0x10200,4)==0x11223344);w.shutdown();assert(rpc_done);}
 s.process.rsx_worker.stop();std::puts("PASS queued SPU/RSX owner callbacks retire in order at import 64, no raw exclusion");return 0;
}
static int failure_case(){
 PpuState s;prepare(s);s.memory.enable_concurrency();auto& w=s.process.spu_worker;
 w.enabled=true;w.pool_mode=true;w.refresh_pending=false;w.error=std::make_exception_ptr(std::runtime_error("HLE exact worker failure"));w.failed=true;
 hle_light_ticks=0;for(unsigned n=0;n<63;++n)getid(s);bool bad=false;
 try{getid(s);}catch(const std::runtime_error& e){bad=std::string(e.what())=="HLE exact worker failure";}assert(bad && !s.memory.local_state().raw);
 w.failed=false;w.error=nullptr;std::puts("PASS terminal worker failure propagates by import 64");return 0;
}
static int metrics_case(bool enabled){
 PpuState s;prepare(s);s.execution.enabled=enabled;setenv("ANYPS3_HLE_SCOPE_TIMING","1",1);
 for(unsigned n=0;n<2043;++n)getid(s);
 for(unsigned n=0;n<7;++n){s.r[11]=99999;bool bad=false;try{anyps3_op_30100(s);}catch(const std::runtime_error& e){bad=std::string(e.what())=="unimplemented LV2 syscall 99999";}assert(bad);}
 for(unsigned n=0;n<3;++n){s.r[11]=48;s.r[3]=1;s.r[4]=0x10200;anyps3_op_30100(s);assert(s.r[3]==0 && s.memory.load(0x10200,4)==1000);}
 auto* metric=hle_syscall_metric(99999);assert(metric->total.calls==(enabled?7:0));
 if(enabled){assert(metric->total.samples==1);anyps3_perf_report(s);hle_perf_report(s);}
 else {hle_perf_report(s);for(auto* item=hle_metric_head;item;item=item->next)assert(!item->total.calls);}
 std::puts("PASS exact import/syscall counters, sampled admission, disabled counters");return 0;
}
static int benchmark(){
 for(bool single:{false,true}){
  PpuState s;prepare(s);auto& m=s.memory;m.enable_concurrency();auto& w=s.process.spu_worker;
  unsigned pumps=0;w.pump=[&]{++pumps;};
  if(single){w.step=[](unsigned){return false;};w.event_driven=true;w.launch();}
  else {w.enabled=true;w.pool_mode=true;w.refresh_pending=false;}
  const unsigned count=single?20000:200000;
  for(unsigned repeat=0;repeat<3;++repeat)for(bool light:{false,true}){
   const auto before=std::chrono::steady_clock::now();const auto initial=pumps;
   for(unsigned n=0;n<count;++n){
    if(light){HleImportScope scope(s,nullptr,true);m.store(0x10100,8,1);}
    else {SpuServiceScope scope(s);m.store(0x10100,8,1);}
   }
   const auto ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-before).count();
   assert(m.load(0x10100,8)==1);std::printf("BENCH mode=%s light=%u calls=%u total_ns=%lld ns_per_call=%.2f pumps=%u\n",single?"single_worker":"pool_empty",light,count,(long long)ns,double(ns)/count,pumps-initial);
  }
 }
 return 0;
}
int main(int argc,char** argv){assert(argc==2);const std::string mode=argv[1];
 if(mode=="semantics")return semantics();
 if(mode=="mutex")return mutex_case();
 if(mode=="atomic")return atomic_case();
 if(mode=="rmw_coherence")return rmw_coherence();
 if(mode=="pool")return progress_case(true);
 if(mode=="rsx")return progress_case(false);
 if(mode=="failure")return failure_case();
 if(mode=="metrics")return metrics_case(true);
 if(mode=="disabled")return metrics_case(false);
 if(mode=="bench")return benchmark();
 return 2;
}
'''


class HleFastGeneration(unittest.TestCase):
    def test_only_audited_imports_are_light(self):
        source = translated().cpp()
        for index, name in enumerate(NAMES):
            function = source.split(f'void anyps3_op_{0x30000+4*index:x}(PpuState& state) {{', 1)[1].split('\n}', 1)[0]
            expected = 'true' if name in FAST_IMPORTS else 'false'
            self.assertIn(f'HleImportScope owner(state,&metric,{expected});', function)
        self.assertNotIn('_sys_strcpy', FAST_IMPORTS)
        self.assertNotIn('_sys_strchr', FAST_IMPORTS)
        self.assertNotIn('_sys_strncpy', FAST_IMPORTS)
        self.assertIn('PpuDeferredScope disposition(state);', source.split('void anyps3_op_30008(PpuState& state) {', 1)[1].split('\n}', 1)[0])
        self.assertIn('hle_syscall_metric(state.r[11])', source)


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class HleFastNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='anyps3-hlefast1-')
        cls.root = Path(cls.directory.name)
        source = translated().cpp().replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        # Validate the proposed one-line PERF integration without editing the
        # startup/report section that belongs to the concurrent PROF1 task.
        source = source.replace('    rsx_worker_perf(state);', '    rsx_worker_perf(state);hle_perf_report(state);')
        cpp = cls.root/'test.cpp'; cpp.write_text(source+MAIN)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                '-pthread', str(cpp), '-o', str(cls.root/'test')], capture_output=True, text=True, timeout=300)
        cls.build_log = build.stdout+build.stderr
        if build.returncode:
            cls.directory.cleanup()
            raise AssertionError(cls.build_log)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_case(self, name):
        env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                   ANYPS3_SPU_THREADS='0', ANYPS3_SPU_WORKER='0', ANYPS3_RSX_WORKER='0', ANYPS3_PERF_REPORT='0')
        env.pop('ANYPS3_AUDIO_AUDIBLE', None)
        result = subprocess.run([str(self.root/'test'), name], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        return result

    def test_01_be_imports_and_raw_fallback(self): self.run_case('semantics')
    def test_02_mutex_errors_recursion_contention_and_timeout(self): self.run_case('mutex')
    def test_03_atomic_alias_contender(self): self.run_case('atomic')
    def test_04_spu_and_rsx_owner_progress(self): self.run_case('pool')
    def test_05_rsx_only_owner_progress(self): self.run_case('rsx')
    def test_06_bounded_terminal_failure(self): self.run_case('failure')
    def test_07_perf_counters_and_sampled_scope_cost(self):
        result = self.run_case('metrics')
        lines = [line for line in result.stderr.splitlines() if line.startswith('PERF HLE')]
        self.assertEqual(len(lines), 3, result.stderr)
        self.assertTrue(any('name=sys_ppu_thread_get_id number=0 calls=2043 calls_total=2043 scope_samples=3 ' in line for line in lines))
        self.assertTrue(any('number=99999 calls=7 calls_total=7 scope_samples=1 ' in line for line in lines))
        self.assertTrue(any('number=48 calls=3 calls_total=3 scope_samples=1 ' in line for line in lines))
    def test_09_rmw_permissions_and_watched_coherence(self): self.run_case('rmw_coherence')
    def test_08_disabled_metrics(self):
        self.assertNotIn('PERF HLE', self.run_case('disabled').stderr)


if __name__ == '__main__': unittest.main()
