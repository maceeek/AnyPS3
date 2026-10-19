"""Real Vulkan exact-byte labels: unrelated FIFO bytes keep a pending prefix."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

ARTIFACTS=Path('local/ratchet/codex/par4/rsx-tests')
MAIN=r'''
static void exact_labels(bool parallel){
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
 // The alias shares the primary backing. Preserve stable physical identity.
 m.regions.push_back({0x71000000,6,m.regions[0].bytes});
 if(parallel)m.enable_concurrency();
 s.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*s.rsx.renderer,s);
 {GuestMemory::RawPermit raw(m);session.observe();s.rsx.graphics_session=&session;
  assert(session.defer_label(0x70008400,0x11223344));
  assert(session.defer_label(0x70008410,0x55667788));
  s.rsx.graphics_session=nullptr;session.leave_fifo();}
 auto owner=m.regions[0].bytes;auto* p=owner->data()+0x8400;
 assert(session.guard.touches(p,4));const auto submissions=session.batch->submissions;
 // Independent fixed FIFO controls in the *same* page: no label submit/wait,
 // no raw guard disarm, and both pending values remain uncommitted.
 m.store(0x70008010,4,0x12345678);m.store(0x70008040,4,0xabcdef00);
 assert(m.load(0x71008010,4)==0x12345678 && m.load(0x70008040,4)==0xabcdef00);
 std::uint8_t dma[16]{};m.transfer(0x71008040,dma,4,false);
 assert(dma[0]==0xab && dma[1]==0xcd && dma[2]==0xef && dma[3]==0);
 assert(session.has_labels() && session.flights.size()==1 && session.batch->submissions==submissions);
 assert(session.guard.touches(p,4));
 assert(owner->coherent(p)[0]==0); // Fixture inspection only; no guest bypass.
 // A poll may be old; explicitly complete the prefix before checking BE bytes.
 std::uint32_t id=17,pc=0x20287568;HostAccessContext origin(HostAccessOrigin::Engine::Ppu,id,pc);
 (void)m.load(0x71008400,4);session.retire(true);
 assert(m.load(0x71008400,4)==0x11223344 && m.load(0x70008410,4)==0x55667788);
 assert(session.labels.empty() && session.flights.empty());
 // Subword neighbour: merging one atomic word cannot overwrite the label.
 {GuestMemory::RawPermit raw(m);s.rsx.graphics_session=&session;assert(session.defer_label(0x70008400,0xaabbccdd));s.rsx.graphics_session=nullptr;session.leave_fifo();}
 m.store(0x70008404,1,0x7f);assert(session.has_labels());
 session.retire(true);
 assert(m.load(0x70008400,4)==0xaabbccdd && m.load(0x70008404,1)==0x7f);
 // Raw retained pointers keep page granularity and synchronize explicitly.
 {GuestMemory::RawPermit raw(m);s.rsx.graphics_session=&session;assert(session.defer_label(0x70008400,0xdeadbeef));s.rsx.graphics_session=nullptr;session.leave_fifo();
  volatile const std::uint8_t* retained=owner->data()+0x8010;
  assert(retained[0]==0x12);std::atomic_signal_fence(std::memory_order_seq_cst);session.retire(true);assert(session.labels.empty());}
 std::puts(parallel?"PASS exact parallel labels, alias, FIFO, DMA and raw guard":"PASS exact serial labels, alias, FIFO, DMA and raw guard");
}

static unsigned setup_replay(PpuState& s){
 setup_draw(s,32,16,128,false);auto& gpu=s.rsx;auto& m=s.memory;
 m.map(0x80000000,0x10000,6,nullptr,0);gpu.dma=0x70008000;gpu.reports=0x80000000;gpu.fifo_enabled=true;gpu.draw.primitive=0;gpu.draw.ranges.clear();
 gpu.registers[0x17cc]=1;gpu.registers[0x1a8]=0x66626660;
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<2;++i){packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);}
 packet(0x1800,1u<<24);m.store(gpu.dma+0x44,4,0x100);m.store(gpu.dma+0x40,4,put);return put;
}
static void worker_replay(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 PpuState serial;const auto put=setup_replay(serial);assert(rsx_run(serial,1024));
 std::vector<std::uint8_t> expected(32*16*4),actual(expected.size());serial.memory.transfer(0xc0100000,expected.data(),expected.size(),false);
 assert(serial.memory.load(0x80001408,4)==1024);assert(near_argb(serial.memory.load(0xc0100000,4),0x80ff8040));
 RsxOwnerQueue owner;std::unique_ptr<PpuState> state;const auto main_tid=std::this_thread::get_id();
 owner.service=[&]{if(state)state->memory.service_coherence();};
 owner.run_fifo=[&](unsigned budget){assert(std::this_thread::get_id()!=main_tid);GuestMemory::RawPermit raw(state->memory);rsx_run(*state,budget);};
 bool destroyed_on_owner=false;
 owner.idle_teardown=[&]{if(state){state->memory.stop_coherence(owner.error);state->memory.parallel->wake_owner={};state.reset();}destroyed_on_owner=std::this_thread::get_id()!=main_tid;};owner.launch();
 owner.call(RsxOwnerQueue::Kind::MapBarrier,[&]{state=std::make_unique<PpuState>();assert(setup_replay(*state)==put);state->memory.enable_concurrency();state->memory.parallel->wake_owner=[&]{owner.poke();};});
 // Only byte APIs cross the boundary. All FIFO/Session/Vulkan state, its
 // register facade and teardown stay private to the actual owner thread.
 auto& memory=state->memory;memory.store(0x70008040,4,put);
 owner.post(RsxOwnerQueue::Kind::RunFifo,{},1024);owner.drain();
 assert(memory.load(0x70008044,4)==put);
 owner.call(RsxOwnerQueue::Kind::HostAccess,[&]{static_cast<rsxgraphics::Session*>(state->rsx.graphics_owner.get())->retire(true);});
 assert(memory.load(0x80001408,4)==1024); // Foreign readback request serviced by the RSX owner.
 memory.transfer(0xc0100000,actual.data(),actual.size(),false);assert(expected==actual);
 owner.call(RsxOwnerQueue::Kind::HostAccess,[&]{GuestMemory::RawPermit raw(memory);auto& session=*static_cast<rsxgraphics::Session*>(state->rsx.graphics_owner.get());state->rsx.graphics_session=&session;assert(session.defer_label(0x70008400,0x11223344));state->rsx.graphics_session=nullptr;session.leave_fifo();});
 memory.store(0x70008010,4,0xabcdef01);assert(memory.load(0x70008010,4)==0xabcdef01);
 owner.call(RsxOwnerQueue::Kind::HostAccess,[&]{static_cast<rsxgraphics::Session*>(state->rsx.graphics_owner.get())->retire(true);});
 assert(memory.load(0x70008400,4)==0x11223344);
 owner.call(RsxOwnerQueue::Kind::Drain,[&]{assert(state->rsx.renderer->graphics_draws==2 && state->rsx.draw.pixels==1024);});
 owner.shutdown();assert(destroyed_on_owner && !owner.error);
 std::puts("PASS serial/owner real Vulkan FIFO pixels, GET, report, labels, foreign coherence and teardown");
}
int main(){try{exact_labels(false);exact_labels(true);worker_replay();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(),'Requires Vulkan/glslang')
class Parallel4Rsx(unittest.TestCase):
    def test_fifo_label_false_sharing_real_vulkan(self):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        cpp=ARTIFACTS/'exact-labels.cpp';exe=cpp.with_suffix('')
        cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0]+MAIN)
        build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(exe)],capture_output=True,text=True,timeout=300)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr);self.assertEqual(build.returncode,0,build.stderr[-12000:])
        env={**os.environ,'LP_NUM_THREADS':'4','VK_INSTANCE_LAYERS':'VK_LAYER_KHRONOS_validation','VK_LAYER_VALIDATE_SYNC':'1','ANYPS3_RSX_ACCESS_TRACE':'1'}
        if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=90,env=env)
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr);self.assertEqual(run.returncode,0,run.stdout+run.stderr[-12000:])
        self.assertNotIn('Validation Error',run.stderr);self.assertNotIn('Validation Warning',run.stderr)
        self.assertEqual(run.stdout.count('PASS '),3)
        self.assertIn('origin=ppu origin_id=17 origin_pc=0x20287568 origin_ea=0x71008400',run.stderr)
