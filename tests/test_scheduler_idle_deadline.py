"""The emitted ppu_schedule body with deterministic clock and engine boundaries.

No wall-clock tolerance: assert exact requested host sleeps. The real scheduler
selects/wakes contexts; external engines and the host clock are fixture seams.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


def load_threads(path):
    spec = importlib.util.spec_from_file_location('scheduler_fixture_threads', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>
constexpr std::uint64_t RETURN_SENTINEL=0xfffe0000;
__CONTEXT__
struct GuestMemory {
 struct RawYield { explicit RawYield(GuestMemory&){} };
 std::uint64_t load(std::uint64_t,unsigned){return 0;}
};
struct PpuState : PpuContext {
 struct Worker {bool enabled=false,refresh_pending=false;void service(){}void idle_wait(){}};
 struct {Worker spu_worker,rsx_worker;} process;
 struct Audio {unsigned maximum=0,running=0;std::vector<unsigned> notifications;} audio;
 struct Wait {bool done=false;std::uint64_t deadline=0;};
 struct {std::map<unsigned,Wait> waits;} sync_ext;
 struct Queue {std::uint64_t id=0;std::vector<unsigned> data;};
 struct Semaphore {std::uint64_t id=0,value=0;};
 struct Mutex {std::uint64_t id=0;unsigned owner=0;};
 struct {std::vector<Queue> queues;} events;
 struct {std::vector<Semaphore> semaphores;std::vector<Mutex> mutexes;} kernel;
 struct Group {std::uint64_t id=0,join_cause=0;unsigned state=0;};
 struct {std::vector<Group> groups;} spu;
 GuestMemory memory;std::set<std::uint64_t> mutexes;
 std::vector<PpuContext> ppu_threads;
 bool module_loading=false,reschedule=false;unsigned execution=0;
};
enum class ExecutionPhase {Coordinator,Idle,RsxWait};
struct SpuServiceScope {SpuServiceScope(PpuState&,bool){}};
struct ExecutionScope {ExecutionScope(unsigned&,ExecutionPhase){}};
static std::uint64_t clock_us=1000,engine_advance_us=0;
static bool peer_on_sleep=false;
static unsigned audio_calls=0,spu_calls=0,rsx_calls=0;
static PpuState* running=nullptr;
static std::vector<std::uint64_t> sleeps;
static std::uint64_t sync_ext_now(){return clock_us;}
static void audio_run(PpuState&){++audio_calls;}
static bool spu_run(PpuState&,unsigned){++spu_calls;return false;}
static bool rsx_run(PpuState&,unsigned){
 ++rsx_calls;if(rsx_calls==2)clock_us+=engine_advance_us;return false;
}
static void rsx_worker_pump(PpuState&){}
static void sync_ext_poll(PpuState& s){
 for(auto& pair:s.sync_ext.waits)if(!pair.second.done && pair.second.deadline && clock_us>=pair.second.deadline){
  pair.second.done=true;
  for(auto& t:s.ppu_threads)if(t.thread_id==pair.first)t.wait_kind=0;
 }
}
static void fixture_sleep(std::chrono::microseconds delay){
 assert(delay.count()>0);sleeps.push_back(delay.count());clock_us+=delay.count();
 // A separate simulated external event can wake the peer before the timer.
 if(peer_on_sleep)running->ppu_threads[1].wait_kind=0;
 assert(sleeps.size()<10);
}
__SCHEDULER__
static void reset(PpuState& s,bool audio,std::uint64_t deadline,bool peer){
 clock_us=1000;engine_advance_us=0;audio_calls=spu_calls=rsx_calls=0;sleeps.clear();
 peer_on_sleep=peer;running=&s;
 s.wait_kind=12;s.thread_id=1;s.audio.maximum=audio;s.audio.running=audio;
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));
 PpuContext other;other.thread_id=2;other.wait_kind=99;s.ppu_threads.push_back(other);
 if(deadline)s.sync_ext.waits.emplace(1,PpuState::Wait{false,deadline});
}
int main(){
 {PpuState s;reset(s,true,1030,false);ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{30});assert(s.thread_id==1 && !s.wait_kind);
  assert(s.sync_ext.waits.at(1).done && clock_us==1030);}
 {PpuState s;reset(s,true,1500,true);ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{100});assert(s.thread_id==2);
  assert(!s.sync_ext.waits.at(1).done && clock_us==1100);}
 {PpuState s;reset(s,true,1030,false);engine_advance_us=40;ppu_schedule(s,true);
  assert(sleeps.empty() && clock_us==1040 && s.thread_id==1);
  assert(s.sync_ext.waits.at(1).done);}
 {PpuState s;reset(s,true,0,true);ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{100} && s.thread_id==2);}
 {PpuState s;reset(s,true,0,true);s.audio.running=0;s.audio.notifications.push_back(7);ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{100});}
 {PpuState s;reset(s,true,0,true);s.sync_ext.waits.emplace(3,PpuState::Wait{true,1});
  s.sync_ext.waits.emplace(4,PpuState::Wait{false,0});ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{100});}
 {PpuState s;reset(s,false,1030,false);ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{30} && s.thread_id==1);}
 {PpuState s;reset(s,false,6000,true);ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{1000} && s.thread_id==2);
  assert(!s.sync_ext.waits.at(1).done);}
 {PpuState s;reset(s,false,1030,false);engine_advance_us=40;ppu_schedule(s,true);
  assert(sleeps.empty() && s.thread_id==1);}
 {PpuState s;reset(s,true,1070,false);s.ppu_threads[0].wait_kind=3;s.wait_kind=3;
  s.wait_deadline=s.ppu_threads[0].wait_deadline=1030;ppu_schedule(s,true);
  assert(sleeps==std::vector<std::uint64_t>{30} && s.thread_id==1 && s.wait_timed_out);
  assert(!s.sync_ext.waits.at(1).done);}
 {PpuState s;reset(s,false,0,false);bool stopped=false;
  try{ppu_schedule(s,true);}catch(const std::runtime_error&){stopped=true;}
  assert(stopped && sleeps.empty());}
 assert(audio_calls && spu_calls && rsx_calls);
 std::puts("PASS 11 scheduler deadline/audio/expiry/peer cases");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SchedulerIdleDeadlineTests(unittest.TestCase):
    def test_actual_scheduler_respects_earlier_deadline_with_audio(self):
        path = Path(os.environ.get('ANYPS3_SCHEDULER_TEST_SOURCE',
                                   Path(__file__).resolve().parents[1]/'anyps3/threads.py'))
        threads = load_threads(path)
        start = threads.CPP.index('[[maybe_unused]] static void ppu_schedule(')
        end = threads.CPP.index('\n#include <csignal>', start)
        scheduler = threads.CPP[start:end]
        # Replace only the host sleep boundary; clock/engines are named fixture seams.
        scheduler = scheduler.replace('std::this_thread::sleep_for(', 'fixture_sleep(')
        code = FIXTURE.replace('__CONTEXT__', threads.TYPES).replace('__SCHEDULER__', scheduler)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'scheduler.cpp').write_text(code)
            build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',
                                    str(root/'scheduler.cpp'),'-o',str(root/'scheduler')],
                                   capture_output=True,text=True,timeout=120)
            self.assertEqual(build.returncode,0,build.stderr)
            run = subprocess.run([str(root/'scheduler')],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('PASS 11 scheduler',run.stdout)


if __name__ == '__main__':
    unittest.main()
