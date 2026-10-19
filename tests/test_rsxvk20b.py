"""Bulk surface coherence, GPU BC RTT and nonblocking semaphore oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void bulk20b(bool benchmark){
 for(unsigned layout=0;layout<(benchmark?1u:3u);++layout)for(unsigned samples:{1u,2u,4u}){
  if(benchmark && samples!=2)continue;
  const unsigned width=benchmark?960:8,height=benchmark?704:4,pitch=benchmark?8192:256;
  PpuState s;setup_draw(s,width,height,pitch,layout==0);s.rsx.tiles[0].size=0x700000;s.rsx.tiles[1].bound=false;
  auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,pitch,width*(samples>1?2:1),height*(samples==4?2:1));
  if(layout==2){target.swizzle_width=width*(samples>1?2:1);target.swizzle_height=height*(samples==4?2:1);}
  s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);
  s.memory.regions.push_back({0xd0000000,6,s.memory.regions[1].bytes});s.memory.mapping_changed();
  s.memory.enable_concurrency();s.memory.enable_page_broker();session.observe();
  auto& surface=session.bind(target,width,height,samples,8,false,false,false,true);
  // All source pixels are independently fixed BE ARGB. Pad/hole sentinels
  // and alias reservations must survive/observe only actual surface bytes.
  {GuestMemory::ObserverPause pause(s.memory);for(const auto& span:surface.physical){std::vector<std::uint8_t> fill(span.end-span.begin);for(unsigned i=0;i<fill.size();++i)fill[i]=i%4==0?0x80:i%4==1?0x12:i%4==2?0x34:0x56;rsxdraw::pinned_host_write(s.memory,reinterpret_cast<std::uint8_t*>(span.begin),fill.data(),fill.size());}}
  std::vector<std::uint8_t> rgba(std::uint64_t(width)*height*samples*4);
  std::pair<std::uintptr_t,std::uint64_t> reservation;
  {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load_reserved(0xd0100000,4,reservation)==0x80123456);s.memory.store(0xc0009000,4,0xdecafbad);}
  const unsigned reps=benchmark?7:1;double read_ms=0,write_ms=0;
  for(unsigned rep=0;rep<reps;++rep){const auto token=s.memory.page_broker.next_token;auto start=std::chrono::steady_clock::now();
   {GuestMemory::ObserverPause pause(s.memory);rsxgraphics::load_pixels(surface,rgba.data(),nullptr);}
   const auto mid=std::chrono::steady_clock::now();
   for(unsigned i=0;i<rgba.size();i+=4){assert(rgba[i]==0x12 && rgba[i+1]==0x34 && rgba[i+2]==0x56 && rgba[i+3]==0x80);}
   {GuestMemory::ObserverPause pause(s.memory);rsxgraphics::commit_pixels(s.memory,surface,rgba.data(),nullptr);}
   const auto end=std::chrono::steady_clock::now();assert(s.memory.page_broker.next_token==token+2);if(rep){read_ms+=std::chrono::duration<double,std::milli>(mid-start).count();write_ms+=std::chrono::duration<double,std::milli>(end-mid).count();}
  }
  {GuestMemory::ObserverPause pause(s.memory);assert(!s.memory.store_conditional(0xd0100000,4,0,reservation));assert(s.memory.load(0xc0009000,4)==0xdecafbad);}
  // GPU bytes were not published by mere upload. Force actual CPU readback
  // after a Vulkan clear, then check the fixed word at a known tiled origin.
  if(!benchmark){s.rsx.registers[0x208]=0x148|((samples==1?0:samples==2?3:4)<<12);s.rsx.registers[0x8c0]=width<<16;s.rsx.registers[0x8c4]=height<<16;s.rsx.registers[0x1d90]=0xff2468ac;
   if(layout==0){assert(rsxgraphics::clear(session,0xf0));session.flush();assert(s.memory.load(0xc0100000,4)==0xff2468ac);}
  }else std::printf("BENCH bulk960x704_msaa2_tiled upload_ms=%.6f writeback_ms=%.6f repeats=%u physical_runs=%zu\n",read_ms/(reps-1),write_ms/(reps-1),reps-1,surface.physical.size());
  session.flush(false);
 }
 std::puts("PASS bulk surface BE bytes, tiled/linear/swizzled sample planes and Vulkan publication");
}
static void bc20b(){
 for(unsigned format:{0x86u,0x87u,0x88u}){
  PpuState s;setup_draw(s,64,4,256,true);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
  rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
  auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,256,64,4);auto& surface=session.bind(target,64,4,1,8,false,false,false,true);
  const unsigned block=format==0x86?8:16;std::vector<std::uint8_t> raw(1024,0);
  for(unsigned b=0;b<4;++b){auto* p=raw.data()+b*block;if(format==0x87)std::memset(p,255,8);if(format==0x88)p[0]=255;
   p+=format==0x86?0:8;p[0]=0;p[1]=0xf8;p[2]=0xe0;p[3]=7;}
  std::vector<std::uint8_t> rgba(1024);for(unsigned i=0;i<256;++i){rgba[4*i]=raw[4*i+1];rgba[4*i+1]=raw[4*i+2];rgba[4*i+2]=raw[4*i+3];rgba[4*i+3]=raw[4*i];}
  vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba.data());session.dirty_surface(surface);
  auto& r=s.rsx.registers;r[0x1a00]=0x100000;r[0x1a04]=0x00010029|((format|0x20)<<8);r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x02020000;r[0x1a18]=0x00080008;r[0x1840]=2*block;
  auto texture=session.texture_metadata(0);assert(texture.resident_image);assert(surface.dirty);assert(session.raw_textures.empty());
  // Read ONLY the converted VkImage, never the source's guest backing.
  const auto at=vk.graphics_transfer(*session.batch,texture.resident_image,VK_IMAGE_ASPECT_COLOR_BIT,16);vk.graphics_finish(*session.batch);
  const float expected[]={1,0,0,1};for(unsigned i=0;i<64;++i)assert(std::memcmp(static_cast<const char*>(session.batch->map)+at+16*i,expected,16)==0);
  {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(0xc0100000,4)==0);}
  vk.graphics_reset(*session.batch);session.flush();assert(s.memory.load(0xc0100000,4)==(format==0x86?0x00f8e007:format==0x87?0xffffffff:0xff000000));
  s.rsx.graphics_session=nullptr;
 }
 std::puts("PASS BC1/2/3 RTT from dirty tiled VkImage, fixed red/alpha, guest source stays stale until CPU readback");
}
static void labels20b(bool parallel){
 PpuState s;setup_draw(s,8,4,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 s.memory.store(0x70008000,4,0x11223344);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 if(parallel){s.memory.mapping_changed();s.memory.enable_concurrency();s.memory.enable_page_broker();}
 VkSemaphore gate=VK_NULL_HANDLE;VkSemaphoreTypeCreateInfo type{};type.sType=VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;type.semaphoreType=VK_SEMAPHORE_TYPE_TIMELINE;
 VkSemaphoreCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;ci.pNext=&type;assert(vkCreateSemaphore(vk.device,&ci,nullptr,&gate)==VK_SUCCESS);
 std::uint64_t value=1;VkTimelineSemaphoreSubmitInfo ti{};ti.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;ti.waitSemaphoreValueCount=1;ti.pWaitSemaphoreValues=&value;
 VkPipelineStageFlags stage=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;VkSubmitInfo wait{};wait.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;wait.pNext=&ti;wait.waitSemaphoreCount=1;wait.pWaitSemaphores=&gate;wait.pWaitDstStageMask=&stage;assert(vkQueueSubmit(vk.queue,1,&wait,VK_NULL_HANDLE)==VK_SUCCESS);
 assert(session.defer_label(0x70008000,0xaabbccdd));session.leave_fifo();
 // Actual observed CPU loads/stores through a physical alias while the GPU
 // is provably blocked. A synchronous poll would hit the test timeout.
 for(unsigned i=0;i<10;++i)assert(s.memory.load(0x71008000,4)==0x11223344);
 s.memory.store(0x71008000,4,0x55667788);assert(s.memory.load(0x70008000,4)==0x55667788);
 s.memory.store(0x70008040,4,0xdeadbeef);assert(session.has_labels());assert(session.batch->wait_ms==0);
 assert(session.defer_label(0x70008000,0x12345678));session.submit_async("test_second_prefix");
 assert(s.memory.load(0x70008000,4)==0x55667788);
 VkSemaphoreSignalInfo signal{};signal.sType=VK_STRUCTURE_TYPE_SEMAPHORE_SIGNAL_INFO;signal.semaphore=gate;signal.value=1;assert(vkSignalSemaphore(vk.device,&signal)==VK_SUCCESS);
 const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);bool done=false;
 do{session.retire(false);done=s.memory.load(0x71008000,4)==0x12345678;}while(!done && std::chrono::steady_clock::now()<deadline);
 assert(done && !session.has_labels());assert(s.memory.load(0x70008040,4)==0xdeadbeef);
 session.flush();s.memory.store(0x70008000,4,0x87654321);assert(s.memory.load(0x71008000,4)==0x87654321);
 vkDestroySemaphore(vk.device,gate,nullptr);s.rsx.graphics_session=nullptr;
 std::puts("PASS blocked-GPU semaphore polling/CPU overwrite and same-page write never wait; ordered BE release progresses");
}
int main(int argc,char**){try{if(argc>1){bulk20b(true);return 0;}bulk20b(false);bc20b();labels20b(false);labels20b(true);}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {','class NativeVulkan {\npublic:',1)+MAIN

class Rsxvk20bTests(unittest.TestCase):
    def test_bulk_surface_rtt_and_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cpp=root/'test.cpp';binary=root/'test';cpp.write_text(native_source())
            built=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(built.returncode,0,built.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            run=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stdout+run.stderr);self.assertNotIn('Validation Warning',run.stdout+run.stderr)
            self.assertEqual(run.stdout.count('PASS '),4)
