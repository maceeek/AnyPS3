"""Pool-lane slice length and clock accounting (par_worker.py)."""
import shutil
import unittest

from tests import spu_native

GLOBALS = r'''
#include <functional>
static double now_s(){return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static bool wait_for(const std::function<bool()>& predicate,double seconds){
    const double end=now_s()+seconds;
    while(!predicate()){if(now_s()>end)return false;std::this_thread::sleep_for(std::chrono::milliseconds(1));}
    return true;
}
'''

SCENARIOS = {}

SCENARIOS['quantum'] = r'''
 static_assert(NativeSpuWorker::pool_lane_quantum==4096 && NativeSpuWorker::pool_tick_budget==1024);
 assert(NativeSpuWorker::lane_budget(1)==1 && NativeSpuWorker::lane_budget(17)==17 && NativeSpuWorker::lane_budget(1023)==1023);
 assert(NativeSpuWorker::lane_budget(1024)==4096 && NativeSpuWorker::lane_budget(4096)==4096 && NativeSpuWorker::lane_budget(96000)==4096);
 // The coordinator's tick budget (1024 per spu_run) selects the standard lane slice; explicit small budgets stay exact.
 NativeSpuWorker pool;pool.pool_mode=true;pool.enabled=true;pool.refresh_pending=false;pool.refresh=[]{};pool.pump=[]{};
 for(unsigned i=0;i<2;++i){auto p=std::make_unique<NativeSpuWorker>();p->parent=&pool;p->group_id=i+1;pool.lanes.push_back(std::move(p));}
 pool.request(17);assert(pool.pool_budget==17 && pool.lanes[0]->budget==17 && pool.lanes[1]->budget==17);
 pool.request(1024);assert(pool.lanes[0]->budget==4096 && pool.lanes[1]->budget==4096);
 pool.request(100000);assert(pool.lanes[0]->budget==4096);
 pool.request(1);assert(pool.lanes[1]->budget==1);
 pool.request(1024);assert(pool.lanes[1]->budget==4096);
 // A running lane really receives that budget as its step quantum.
 std::vector<unsigned> seen;
 auto& lane=*pool.lanes[0];lane.step=[&](unsigned quantum){seen.push_back(quantum);return seen.size()<3;};lane.launch();
 assert(wait_for([&]{return lane.completed.load()>=3;},5));pool.shutdown();
 assert(seen.size()>=3 && seen[0]==4096 && seen[1]==4096 && seen[2]==4096);
'''

SCENARIOS['clock-gating'] = r'''
 // worker_ns (the ANYPS3_PERF_REPORT wall counter) is only measured when that report is enabled.
 auto measure=[&](bool perf){
  if(perf)setenv("ANYPS3_PERF_REPORT","1",1);else setenv("ANYPS3_PERF_REPORT","0",1);
  NativeSpuWorker lane;std::atomic<unsigned> calls{0};
  lane.step=[&](unsigned){std::this_thread::sleep_for(std::chrono::milliseconds(2));return ++calls<5;};lane.launch();
  assert(wait_for([&]{return lane.completed.load()>=5;},5));lane.shutdown();
  return std::make_pair(lane.worker_ns.load(),lane.completed.load());
 };
 const auto off=measure(false),on=measure(true);
 assert(off.first==0 && off.second>=5);                 // no clock, quanta still counted
 assert(on.first>=5*2000000ull && on.second>=5);        // 5 quanta of at least 2 ms each
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPoolQuantumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory, cls.exe = spu_native.build(SCENARIOS, globals_cpp=GLOBALS, name='spu-pool-quantum')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def scenario(self, name):
        code, out, err = spu_native.run(self.directory, self.exe, name)
        self.assertEqual(code, 0, f'{name}: {err[-6000:]}')
        return out

    def test_pool_lane_quantum_is_4096_and_small_budgets_stay_exact(self):
        self.scenario('quantum')

    def test_lane_loop_reads_host_clock_only_for_perf_report(self):
        self.scenario('clock-gating')


if __name__ == '__main__':
    unittest.main()
