"""RSX owner responsiveness: page-synchronisation requests are served inside FIFO runs and the vsync wait; GPU work and labels that
precede a flip are submitted/published before the vblank (anyps3/rsx.py rsx_wait_vblank, rsx_owner_poll; rsx_game_worker.py).

Independent expectations (not derived from the implementation's algorithm):
  * midrun: a drainable request posted while the owner is in the middle of a long FIFO run observes GET < PUT when it executes
    (served between packets); with ANYPS3_RSX_HOST_DRAIN=0 it observes GET == PUT (served after the run). Pixels and the final
    GET/report of the run equal the serial reference in both cases.
  * vsync: the same request posted 5 ms into a 40 ms vsync wait executes long before the deadline (default) or only after it
    (ANYPS3_RSX_HOST_DRAIN=0); a label queued before the wait is published before the deadline by default and still pending when the
    request runs with ANYPS3_RSX_FLIP_EARLY=0.
  * timer slack: ANYPS3_RSX_TIMER_SLACK_NS accepts exactly the digit strings that fit unsigned long (the grammar of
    ANYPS3_TIMER_SLACK_NS); signed, blank-led, trailing-garbage, empty and overflowing values stop the process with status 2.
  * diagnostic gating: the service-tick clocks, the per-draw recording time (record_back_ms) and the flight latency EWMA do not run
    unless their switch is set (ANYPS3_PERF_REPORT=1, ANYPS3_RSX_POLL=adaptive) and do run when it is.
"""
from pathlib import Path
import os
import subprocess
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

ARTIFACTS = Path('local/ratchet/codex/frametime/tests')
MAIN = r'''
static unsigned setup_fifo(PpuState& s,unsigned groups){
 setup_draw(s,32,16,128,false);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x80000000,0x10000,6,nullptr,0);g.dma=0x70008000;g.reports=0x80000000;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();
 g.registers[0x17cc]=1;g.registers[0x1a8]=0x66626660;
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<groups;++i){packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);}
 packet(0x1800,1u<<24);m.store(g.dma+0x44,4,0x100);m.store(g.dma+0x40,4,put);return put;
}
static std::uint64_t mono_ns(){return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
// Both scenarios share the owner start-up of test_par6: serial reference, then the worker with the same FIFO.
static void midrun(bool drain_on){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 const unsigned groups=2500;
 PpuState serial;const auto put=setup_fifo(serial,groups);assert(rsx_run(serial,1<<20));
 std::vector<std::uint8_t> expected(32*16*4),actual(expected.size());serial.memory.transfer(0xc0100000,expected.data(),expected.size(),false);
 const auto serial_report=serial.memory.load(0x80001408,4);
 PpuState s;assert(setup_fifo(s,groups)==put);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);
 assert(!rsx_worker_request(s,1<<20).completed);
 // wait for visible progress, then request page synchronisation like an SPU lane touching a guarded page would
 while(s.memory.load(0x70008044,4)==0x100)std::this_thread::yield();
 std::uint64_t get_at_service=0;bool served=false;
 rsx_host_request(w,[&]{get_at_service=s.memory.load(0x70008044,4);served=true;});
 assert(served);
 w.queue->drain();
 const auto final_get=s.memory.load(0x70008044,4);
 s.memory.transfer(0xc0100000,actual.data(),actual.size(),false);
 assert(final_get==put && s.memory.load(0x80001408,4)==serial_report && expected==actual);
 life.finish();
 if(drain_on){assert(get_at_service<put);std::printf("PASS midrun drain on: served at GET=%llx < PUT=%x, pixels/report/GET identical to serial\n",(unsigned long long)get_at_service,put);}
 else{assert(get_at_service==put);std::printf("PASS midrun drain off: served after the run (GET==PUT), pixels identical\n");}
}
static void vsync(const char* mode){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;const auto put=setup_fifo(s,2);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);
 assert(!rsx_worker_request(s,1024).completed);w.queue->drain();assert(s.memory.load(0x70008044,4)==put);   // session exists, idle
 auto* f=static_cast<PpuState*>(w.facade.get());
 std::atomic<std::uint64_t> wait_start{0},wait_end{0},deadline{0};std::atomic<long> owner_slack{-1};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
  owner_slack=prctl(PR_GET_TIMERSLACK);
  session.enter_fifo();f->rsx.graphics_session=&session;     // as inside a FIFO run
  assert(session.defer_label(0x70008400,0x55667788));
  const auto until_ns=mono_ns()+40'000'000;deadline=until_ns;wait_start=mono_ns();
  rsx_wait_vblank(*f,std::chrono::steady_clock::time_point(std::chrono::nanoseconds(until_ns)));
  wait_end=mono_ns();f->rsx.graphics_session=nullptr;session.leave_fifo();
 });
 while(!wait_start.load())std::this_thread::yield();
 std::this_thread::sleep_for(std::chrono::milliseconds(5));
 std::uint64_t served_ns=0;bool label_pending=false;std::uint64_t label_value=0;
 rsx_host_request(w,[&]{auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
  served_ns=mono_ns();label_pending=session.has_labels();label_value=f->memory.load(0x71008400,4);});
 w.queue->drain();
 assert(wait_end.load()>=deadline.load());               // the wait itself still ends at the deadline
 const double served_after_ms=(served_ns-wait_start.load())/1e6;
 const std::string m=mode;
 if(m=="default"){assert(served_ns<deadline.load()-20'000'000);assert(!label_pending && label_value==0x55667788);}
 else if(m=="drain_off"){assert(served_ns>=deadline.load());}
 else if(m=="early_off"){assert(served_ns<deadline.load()-20'000'000);assert(label_pending && label_value==0);}
 else if(m=="slack_off" || m=="slack_env"){assert(served_ns<deadline.load()-20'000'000);}
 else assert(false);
 const long main_slack=prctl(PR_GET_TIMERSLACK);
 if(m=="slack_off")assert(owner_slack.load()==main_slack);
 else if(m=="slack_env")assert(owner_slack.load()==long(std::strtoul(std::getenv("ANYPS3_RSX_TIMER_SLACK_NS"),nullptr,10)));
 else assert(owner_slack.load()==1);   // the owner thread alone gets 1 ns
 assert(main_slack!=1 || m=="slack_off" || m=="slack_env");                   // the process's other threads keep the kernel default
 life.finish();
 std::printf("PASS vsync %s: request served %.1f ms into a 40 ms wait, label_pending=%d\n",mode,served_after_ms,int(label_pending));
}
// Diagnostic timing is off unless its switch is set: counters stay exactly zero / at their initial value without it and move with it.
static void gate(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 const unsigned groups=400;
 PpuState s;const auto put=setup_fifo(s,groups);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);
 assert(!rsx_worker_request(s,1<<20).completed);
 const auto until=mono_ns()+20'000'000'000ull;
 while(s.memory.load(0x70008044,4)!=put){assert(mono_ns()<until);std::this_thread::yield();}
 w.queue->drain();
 std::vector<std::uint8_t> pixels(32*16*4);s.memory.transfer(0xc0100000,pixels.data(),pixels.size(),false);   // guarded read: retires every flight
 std::this_thread::sleep_for(std::chrono::milliseconds(30));w.queue->drain();
 auto* f=static_cast<PpuState*>(w.facade.get());
 auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
 const char* report_env=std::getenv("ANYPS3_PERF_REPORT");const char* poll_env=std::getenv("ANYPS3_RSX_POLL");
 const bool report=report_env && std::string(report_env)=="1",adaptive=poll_env && std::string(poll_env)=="adaptive";
 assert(session.async_submits>0 && session.async_retires==session.async_submits);
 const double back=session.batch->record_back_ms;
 if(report){assert(back>0.0);assert(rsx_service_calls().load()>0 && rsx_service_ns().load()>0);}
 else{assert(back==0.0);assert(rsx_service_calls().load()==0 && rsx_service_ns().load()==0);}
 const double flight=session.flight_ns;    // copied: the session dies with the worker session
 if(adaptive)assert(flight!=300000.0);else assert(flight==300000.0);
 life.finish();
 std::printf("PASS gate report=%d adaptive=%d: record_back_ms=%.3f service_calls=%llu flight_ns=%.0f\n",int(report),int(adaptive),back,(unsigned long long)rsx_service_calls().load(),flight);
}
int main(int argc,char** argv){try{
 const std::string mode=argc>1?argv[1]:"";
 if(mode=="midrun_on")midrun(true);else if(mode=="midrun_off")midrun(false);
 else if(mode=="vsync_default")vsync("default");else if(mode=="vsync_drain_off")vsync("drain_off");else if(mode=="vsync_early_off")vsync("early_off");else if(mode=="vsync_slack_off")vsync("slack_off");else if(mode=="vsync_slack_env")vsync("slack_env");else if(mode=="gate")gate();
 else return 3;
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


class OwnerResponsiveness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp = ARTIFACTS / 'drain.cpp'
        cls.exe = cpp.with_suffix('')
        cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0] + MAIN)
        result = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN',
                                 '-DANYPS3_RSX_VK', str(cpp), *LIBRARIES, '-o', str(cls.exe)], capture_output=True, text=True, timeout=600)
        cpp.with_suffix('.build.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr[-12000:]

    def run_mode(self, mode, **extra):
        env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                   ANYPS3_SPU_WORKER='0')
        env.pop('ANYPS3_RSX_HOST_DRAIN', None)
        env.pop('ANYPS3_RSX_FLIP_EARLY', None)
        env.pop('ANYPS3_PERF_REPORT', None)
        env.pop('ANYPS3_RSX_POLL', None)
        env.pop('ANYPS3_RSX_TIMER_SLACK_NS', None)
        env.update(extra)
        result = subprocess.run([str(self.exe), mode], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-6000:])
        self.assertIn('PASS ', result.stdout)
        return result.stdout

    def test_midrun_request_is_served_between_packets(self):
        self.run_mode('midrun_on')

    def test_midrun_request_waits_without_drain(self):
        self.run_mode('midrun_off', ANYPS3_RSX_HOST_DRAIN='0')

    def test_vsync_wait_serves_requests_and_publishes_labels_early(self):
        self.run_mode('vsync_default')

    def test_vsync_wait_without_drain_serves_after_deadline(self):
        self.run_mode('vsync_drain_off', ANYPS3_RSX_HOST_DRAIN='0')

    def test_vsync_wait_without_early_submit_keeps_label_pending(self):
        self.run_mode('vsync_early_off', ANYPS3_RSX_FLIP_EARLY='0')

    def test_timer_slack_grammar(self):
        # exact digit strings that fit unsigned long are applied to the owner thread alone; everything else stops the process (status 2)
        out = self.run_mode('vsync_slack_env', ANYPS3_RSX_TIMER_SLACK_NS='7')
        self.assertIn('PASS ', out)
        for bad in ('-1', '+5', ' 5', '5 ', '5x', '', '0x10', '18446744073709551616', '99999999999999999999999999'):
            with self.subTest(value=bad):
                env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                           ANYPS3_SPU_WORKER='0', ANYPS3_RSX_TIMER_SLACK_NS=bad)
                result = subprocess.run([str(self.exe), 'vsync_default'], capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr[-2000:])
                self.assertIn('invalid ANYPS3_RSX_TIMER_SLACK_NS', result.stderr)

    def test_diagnostic_timing_is_off_without_its_switch(self):
        self.run_mode('gate')
        self.run_mode('gate', ANYPS3_PERF_REPORT='1')
        self.run_mode('gate', ANYPS3_RSX_POLL='adaptive')
        self.run_mode('gate', ANYPS3_PERF_REPORT='1', ANYPS3_RSX_POLL='adaptive')

    def test_owner_timer_slack_is_private_and_switchable(self):
        # default: owner thread 1 ns, other threads untouched (checked inside vsync_default); switch off: inherited value
        self.run_mode('vsync_slack_off', ANYPS3_RSX_TIMER_SLACK_NS='0')


if __name__ == '__main__':
    unittest.main()
