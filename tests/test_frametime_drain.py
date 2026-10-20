"""RSX owner responsiveness: page-synchronisation requests are served inside FIFO runs and the vsync wait; GPU work and labels that
precede a flip are submitted/published before the vblank (anyps3/rsx.py rsx_wait_vblank, rsx_owner_poll; rsx_game_worker.py).

Independent expectations (not derived from the implementation's algorithm):
  * midrun: a drainable request posted while the owner is in the middle of a long FIFO run observes GET < PUT when it executes
    (served between packets); with ANYPS3_RSX_HOST_DRAIN=0 it observes GET == PUT (served after the run). Pixels and the final
    GET/report of the run equal the serial reference in both cases.
  * vsync: the same request posted 5 ms into a 40 ms vsync wait executes long before the deadline (default) or only after it
    (ANYPS3_RSX_HOST_DRAIN=0); a label queued before the wait is published before the deadline by default and still pending when the
    request runs with ANYPS3_RSX_FLIP_EARLY=0.
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
 std::atomic<std::uint64_t> wait_start{0},wait_end{0},deadline{0};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  auto& session=*static_cast<rsxgraphics::Session*>(f->rsx.graphics_owner.get());
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
 else assert(false);
 life.finish();
 std::printf("PASS vsync %s: request served %.1f ms into a 40 ms wait, label_pending=%d\n",mode,served_after_ms,int(label_pending));
}
int main(int argc,char** argv){try{
 const std::string mode=argc>1?argv[1]:"";
 if(mode=="midrun_on")midrun(true);else if(mode=="midrun_off")midrun(false);
 else if(mode=="vsync_default")vsync("default");else if(mode=="vsync_drain_off")vsync("drain_off");else if(mode=="vsync_early_off")vsync("early_off");
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


if __name__ == '__main__':
    unittest.main()
