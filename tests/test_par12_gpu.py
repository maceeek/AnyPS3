"""Pending Vulkan completion must make progress without another FIFO PUT."""
from pathlib import Path
import os,shutil,subprocess,unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES
from tests.test_par6 import MAIN

BODY=r'''
int main(){try{
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","1",1);
 PpuState s;const auto put=setup_fifo(s);SpuWorkerSession life(s);auto& w=s.process.rsx_worker;auto& q=*w.queue;
 const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(s.memory.load(s.rsx.dma+0x44,4)!=put){rsx_run(s,1024);assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}
 q.drain();
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{
  auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());
  assert(session.defer_label(0x70008400,0xaabbccdd));session.leave_fifo();
  assert(!session.flights.empty() && !s.rsx.context);
  // Independent deadline oracle: an in-flight GPU prefix needs a finite wake
  // even with no vblank context and no further PPU PUT/RunFifo publication.
  const auto now=std::chrono::steady_clock::now(),due=q.deadline();
  assert(due>=now && due<=now+std::chrono::milliseconds(1));
 });
 const auto requests=w.host_requests.load();std::uint64_t sequence;
 {std::lock_guard<std::mutex> lock(q.mutex);sequence=q.next_sequence;}
 const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 while(s.memory.load(0x70008400,4)!=0xaabbccdd){
  rsx_worker_pump(s);assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::microseconds(100));
 }
 assert(w.host_requests.load()==requests);
 {std::lock_guard<std::mutex> lock(q.mutex);assert(q.next_sequence==sequence);}
 q.call(RsxOwnerQueue::Kind::HostAccess,[&]{auto& session=*static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());assert(session.flights.empty() && session.async_retires>0);});
 life.finish();std::puts("PASS original GPU prefix retirement, exact BE label and finite deadline without PUT/RPC");
 }catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(),'Requires Vulkan/glslang')
class Parallel12Gpu(unittest.TestCase):
    def test_pending_gpu_prefix_has_own_deadline_without_fifo_ticks(self):
        directory=Path('local/ratchet/codex/par12/gpu-tests');directory.mkdir(parents=True,exist_ok=True)
        cpp=directory/'idle-gpu.cpp';cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0]+MAIN[:MAIN.index('static void fifo(bool lanes)')]+BODY)
        p=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(cpp.with_suffix(''))],capture_output=True,timeout=300)
        cpp.with_suffix('.build.log').write_bytes(p.stdout+p.stderr);self.assertEqual(p.returncode,0,p.stderr.decode()[-12000:])
        env={**os.environ,'ANYPS3_SPU_WORKER':'0','ANYPS3_SPU_THREADS':'0','LP_NUM_THREADS':'3','VK_DRIVER_FILES':'/usr/share/vulkan/icd.d/lvp_icd.json','ANYPS3_VK_DEVICE':'lavapipe','VK_INSTANCE_LAYERS':'VK_LAYER_KHRONOS_validation','VK_LAYER_VALIDATE_SYNC':'1'}
        p=subprocess.run([str(cpp.with_suffix(''))],capture_output=True,timeout=90,env=env)
        cpp.with_suffix('.run.log').write_bytes(p.stdout+p.stderr);self.assertEqual(p.returncode,0,p.stderr.decode()[-12000:])
        self.assertNotIn(b'Validation Error',p.stderr);self.assertNotIn(b'Validation Warning',p.stderr)
