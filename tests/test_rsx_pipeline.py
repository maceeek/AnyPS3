"""Timeline-prefix ownership, frame completion and independent native oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void registers_(){
 RsxRegisters bank{{0x1d6c,0},{0xfffc,0x89abcdef},{0,0x11223344}};assert(bank.size()==3 && bank.count(0x1d6c) && !bank.count(0x100));
 const std::array<unsigned,3> keys{0,0x1d6c,0xfffc};unsigned i=0;for(const auto& entry:bank)assert(entry.first==keys.at(i++));
 bank[0x100]=0x55667788;assert(bank.find(0x100)->second==0x55667788 && bank.erase(0x100)==1 && bank.erase(0x100)==0);
 bank[0x100]=0;assert(bank.at(0x100)==0);const RsxRegisters& view=bank;assert(view.at(0xfffc)==0x89abcdef);
 bool missing=false;try{(void)view.at(4);}catch(const std::out_of_range&){missing=true;}assert(missing);
 bool unaligned=false;try{bank[1]=0;}catch(const std::runtime_error&){unaligned=true;}assert(unaligned);
 bank.clear();assert(bank.empty() && bank.begin()==bank.end());bank[0xfffc]=1;assert(bank.begin()->first==0xfffc);bank[0xfffc]=2;assert(bank.size()==1 && bank.at(0xfffc)==2);
 std::puts("PASS flat register presence distinguishes reset zero, erase, sorted iteration and highest method");
}
static void prefix_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 const unsigned a=0x70008000,b=0x70009000;s.memory.store(a,4,0x11223344);s.memory.store(b,4,0x55667788);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,32,8,8);auto image=session.bind(target,8,8,1,8,false).image;
 std::vector<std::uint8_t> pixels(8*8*4);for(unsigned i=0;i<64;++i){pixels[4*i]=0x11;pixels[4*i+1]=0x22;pixels[4*i+2]=0x33;pixels[4*i+3]=0xff;}
 vk.graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());
 assert(session.defer_label(a,0xa1b2c3d4));session.submit_async("oracle_prefix");assert(session.flights.size()==1 && session.flights.front().batch->submitted);
 for(auto& v:pixels)v=0x44;
 vk.graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());
 assert(session.defer_label(b,0x89abcdef));session.dirty_surface(session.surfaces.front());
 // Complete only the older prefix; a CPU poll itself no longer waits.
 session.retire(true,session.flights.front().batch->completion);
 // The older CPU reader must not submit/reset the newer active recording.
 assert(s.memory.load(a,4)==0xa1b2c3d4);assert(session.batch->recording && !session.batch->submitted && session.labels.size()==1);
 {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(b,4)==0x55667788);}
 session.flush();assert(s.memory.load(b,4)==0x89abcdef);assert(s.memory.load(0xc0010000,4)==0x44444444);
 s.rsx.graphics_session=nullptr;std::puts("PASS exact older timeline prefix leaves newer command/data/label pending");
}
static void query_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.registers[0x17cc]=1;s.rsx.registers[0x1a8]=0xfeed0000;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});setenv("ANYPS3_RSX_BACKEND","gpu",1);
 rsx_execute_draw(s);rsx_get_report(s,0x01020000);session.submit_async("oracle_report1");
 rsx_graphics_reset_report(s,1);s.rsx.report_values[0]=0;s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);rsx_get_report(s,0x01021000);session.submit_async("oracle_report2");
 assert(session.flights.size()==2);session.retire(true);assert(s.memory.load(0xc0020008,4)==64);assert(s.memory.load(0xc0021008,4)==64);
 assert(s.rsx.report_values[0]==64 && s.rsx.draw.pixels==128);assert(s.memory.load(0xc002000c,4)==0 && s.memory.load(0xc002100c,4)==0);
 assert(s.memory.load(0xc0020000,8)>0 && s.memory.load(0xc0021000,8)>s.memory.load(0xc0020000,8));
 session.flush();s.rsx.graphics_session=nullptr;s.rsx.graphics_owner.reset();std::puts("PASS report snapshots across submissions/reset retain fixed 64-pixel prefixes and BE timestamp/status");
}
static void visibility_(){
 // ZPASS counting off for the first draw, on for the second: the report counts only the second draw's 64 pixels in both
 // modes. With ANYPS3_RSX_LAZY_VISIBILITY=1 the uncounted draw records no occlusion query (and draw.pixels skips it).
 const bool lazy=rsxgraphics::rsx_lazy_visibility();
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.registers[0x17cc]=0;s.rsx.registers[0x1a8]=0xfeed0000;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});setenv("ANYPS3_RSX_BACKEND","gpu",1);
 rsx_execute_draw(s);assert(session.batch->visibility_count==(lazy?0u:1u));
 s.rsx.registers[0x17cc]=1;s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.batch->visibility_count==(lazy?1u:2u));
 rsx_get_report(s,0x01020000);s.rsx.registers[0x17cc]=0;s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);
 rsx_get_report(s,0x01021000);session.flush();
 assert(s.memory.load(0xc0020008,4)==64 && s.memory.load(0xc0021008,4)==64 && s.rsx.report_values[0]==64);
 assert(s.rsx.draw.pixels==(lazy?64u:192u));
 s.rsx.graphics_session=nullptr;s.rsx.graphics_owner.reset();
 std::printf("PASS ZPASS-off draws do not count into reports (lazy visibility %d)\n",lazy?1:0);
}
static void empty_prefix_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.registers[0x17cc]=1;s.rsx.registers[0x1a8]=0xfeed0000;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);assert(session.defer_label(0x70008000,0x11223344));session.submit_async("oracle_data");
 // Two later completions have no new GPU work; their precise report snapshot
 // still includes the one in-flight 64-pixel draw, even after RESET_REPORT.
 rsx_get_report(s,0x01020000);assert(session.defer_label(0x70009000,0xaabbccdd));++session.batch->frames;session.submit_async("oracle_empty_flip");
 assert(session.flights.size()==1 && session.batch->submissions==1 && session.batch->frames==0 && session.flights.back().batch->frames==1);
 rsx_graphics_reset_report(s,1);s.rsx.report_values[0]=0;rsx_get_report(s,0x01021000);session.submit_async("oracle_empty_report");
 assert(session.batch->submissions==1);session.flush(false);
 assert(s.memory.load(0x70008000,4)==0x11223344 && s.memory.load(0x70009000,4)==0xaabbccdd);
 assert(s.memory.load(0xc0020008,4)==64 && s.memory.load(0xc0021008,4)==0 && s.rsx.report_values[0]==0 && s.rsx.draw.pixels==64);
 assert(s.memory.load(0xc002000c,4)==0 && s.memory.load(0xc002100c,4)==0);
 assert(s.memory.load(0xc0020000,8)<s.memory.load(0xc0021000,8));
 // Reusing a prefix must keep deferred host metadata bounded as well.
 s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);
 for(unsigned i=0;i<300;++i){assert(session.defer_label(0x7000a000+4*i,0x12340000+i));session.submit_async("oracle_bounded_merge");
  assert(session.flights.size()<=2);for(const auto& flight:session.flights)assert(flight.labels.size()<=256);}
 session.flush(false);for(unsigned i=0;i<300;++i)assert(s.memory.load(0x7000a000+4*i,4)==0x12340000+i);
 s.rsx.graphics_session=nullptr;s.rsx.graphics_owner.reset();std::puts("PASS empty flip/label/report reuse exact submitted prefix without losing reset/query ordering");
}
static void ring_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 std::set<const void*> arenas;
 for(unsigned i=0;i<12;++i){arenas.insert(session.batch.get());const unsigned value=i+1;
  const auto at=s.rsx.renderer->graphics_upload(*session.batch,&value,4);(void)at;
  assert(session.defer_label(0x70008000+4*i,0x12340000+i));session.submit_async("oracle_ring");assert(session.flights.size()<=2);}
 assert(arenas.size()==3);session.flush(false);assert(session.flights.empty());
 for(unsigned i=0;i<12;++i)assert(s.memory.load(0x70008000+4*i,4)==0x12340000+i);
 assert(session.batch->submissions==12);s.rsx.graphics_session=nullptr;std::puts("PASS bounded three-arena wrap preserves all in-flight labels and staging resources");
}
static void error_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 s.memory.store(0x70008000,4,0x01020304);std::vector<unsigned> words(16,0);words[7]=2;
 auto at=s.rsx.renderer->graphics_upload(*session.batch,words.data(),words.size()*4);session.pending.push_back({unsigned(at+28),0,0,false});
 assert(session.defer_label(0x70008000,0xffffffff));session.submit_async("oracle_error");bool stopped=false;
 try{session.retire(true);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("execution budget")!=std::string::npos;}
 assert(stopped && session.failed);{GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(0x70008000,4)==0x01020304);}
 s.rsx.graphics_session=nullptr;std::puts("PASS shader error poisons prefix before any successful guest completion");
}
static void flip_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});
 auto& g=s.rsx;g.driver=0x70004000;g.reports=0x70006000;g.event_queue=42;g.event_port=43;g.flip_modes={1,1};g.flips=8;g.display_buffers[0]={8,8,32,0x10000};
 s.events.queues.push_back({42,4,0,{}});s.events.ports.push_back({43,1,42,0xabcdef});s.memory.store(g.driver+0x12c0,4,16);
 const auto address=std::uint64_t(g.driver)+0x10f8;s.memory.store(address+8,4,0);s.memory.store(g.reports+0x10,8,UINT64_MAX);s.memory.store(g.reports+0x18,8,UINT64_MAX);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,32,8,8);auto& surface=session.bind(target,8,8,1,8,false);
 std::vector<std::uint8_t> pixels(256,0x66);s.rsx.renderer->graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(surface);
 rsx_flip_display(s,1,0x10000);assert(g.flips==9 && g.captured_frames==0 && !session.flights.empty());
 {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(address+8,4)==0);assert(s.memory.load(g.reports+0x10,8)==UINT64_MAX);}
 assert(s.events.queues.back().data.empty());s.rsx.graphics_session=nullptr;session.leave_fifo();
 // Completion publishes flip bytes and event; the CPU poll itself may be old.
 session.retire(true);
 assert(s.memory.load(address+8,4)==0x80000000 && s.memory.load(address,8)>0 && s.memory.load(address+16,4)==0);
 assert(s.memory.load(g.reports+0x10,8)==0 && s.memory.load(g.reports+0x18,8)==0);
 assert(s.events.queues.back().data.size()==1 && s.events.queues.back().data[0][0]==0xabcdef && s.events.queues.back().data[0][2]==16);
 session.flush();assert(s.memory.load(0xc0010000,4)==0x66666666);assert(s.rsx.renderer->graphics_profile_frames>=1 && s.rsx.renderer->graphics_busy_ns>0);
 s.rsx.graphics_owner.reset();std::puts("PASS unsaved real flip defers driver/report/event completion until GPU timeline and measures GPU nanoseconds");
}

static void streaming_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 s.rsx.registers[0x17cc]=1;s.rsx.registers[0x1a8]=0xfeed0000;const float rgba[]={.25f,.5f,.75f,1.f};for(unsigned c=0;c<4;++c)fpword(s,4+c,rsxdraw::float_bits(rgba[c]));
 rsxgraphics::Session session(vk,s);session.submit_draws=1;session.submit_extend=1;session.observe();s.rsx.graphics_session=&session;
 s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});setenv("ANYPS3_RSX_BACKEND","gpu",1);
 // Block execution of this queue until the CPU explicitly releases it. A
 // heuristic that waits while all arenas are occupied deadlocks this oracle.
 VkSemaphore gate=VK_NULL_HANDLE;VkSemaphoreTypeCreateInfo type{};type.sType=VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;type.semaphoreType=VK_SEMAPHORE_TYPE_TIMELINE;
 VkSemaphoreCreateInfo create{};create.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;create.pNext=&type;assert(vkCreateSemaphore(vk.device,&create,nullptr,&gate)==VK_SUCCESS);
 const std::uint64_t one=1;VkTimelineSemaphoreSubmitInfo timeline{};timeline.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;timeline.waitSemaphoreValueCount=1;timeline.pWaitSemaphoreValues=&one;
 const VkPipelineStageFlags stage=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;VkSubmitInfo wait{};wait.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;wait.pNext=&timeline;wait.waitSemaphoreCount=1;wait.pWaitSemaphores=&gate;wait.pWaitDstStageMask=&stage;
 assert(vkQueueSubmit(vk.queue,1,&wait,VK_NULL_HANDLE)==VK_SUCCESS);
 rsx_execute_draw(s);assert(session.flights.size()==1 && session.batch->submissions==1);
 assert(session.defer_label(0x70008000,0x89abcdef));rsx_get_report(s,0x01020000);
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.flights.size()==2 && session.batch->submissions==2);
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.flights.size()==2 && session.batch->recording && session.batch->submissions==2);
 {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(0x70008000,4)==0);assert(s.memory.load(0xc0020008,4)==0);}
 VkSemaphoreSignalInfo signal{};signal.sType=VK_STRUCTURE_TYPE_SEMAPHORE_SIGNAL_INFO;signal.semaphore=gate;signal.value=1;assert(vkSignalSemaphore(vk.device,&signal)==VK_SUCCESS);
 session.flush();assert(s.memory.load(0x70008000,4)==0x89abcdef);assert(s.memory.load(0xc0020008,4)==64);assert(s.rsx.draw.pixels==192);
 assert(near_argb(s.memory.load(0xc0100000,4),0xff4080bf));
 vkDestroySemaphore(vk.device,gate,nullptr);s.rsx.graphics_session=nullptr;s.rsx.graphics_owner.reset();
 std::puts("PASS intra-frame submits feed a gated GPU without heuristic waits and preserve report prefix/BE label/pixels");
}
static void leases_(){
 PpuState s;setup_draw(s,8,8,32,false);auto backing=s.memory.regions[0].bytes;
 s.memory.regions.push_back({0x81000000,6,backing});s.memory.enable_concurrency();s.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 {GuestMemory::RawPermit raw(s.memory);assert(session.defer_label(0x70008000,0xa1b2c3d4));}
 std::pair<std::uintptr_t,std::uint64_t> token;{GuestMemory::ObserverPause pause(s.memory);token=s.memory.reservation_token(0x81008000,4);}session.submit_async("lease_label");session.retire(true);
 assert(s.memory.load(0x81008000,4)==0xa1b2c3d4 && s.memory.reservation_token(0x81008000,4)!=token);
 {GuestMemory::RawPermit raw(s.memory);assert(session.defer_label(0x70009000,0x11223344));
  s.memory.regions[0].flags=4;s.memory.mapping_changed();}
 session.submit_async("stale_label");bool stopped=false;try{session.retire(true);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("stale guest range")!=std::string::npos;}
 assert(stopped && session.failed);{GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(0x81009000,4)==0);}
 s.rsx.graphics_session=nullptr;std::puts("PASS deferred label lease preserves alias reservation invalidation and rejects replaced mapping before publication");
}

static void packed_leases_(){
 PpuState s;setup_draw(s,8,8,64,false);auto backing=s.memory.regions[1].bytes;s.memory.regions.push_back({0x81000000,6,backing});
 s.memory.enable_concurrency();s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 s.memory.store(0x81010030,4,0xdeadbeef);auto padding=s.memory.reservation_token(0x81010080,4);
 {GuestMemory::RawPermit raw(s.memory);auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,256,8,8);
  auto& surface=session.bind(target,8,8,1,8,false);std::vector<std::uint8_t> pixels(256);for(unsigned i=0;i<64;++i){pixels[4*i]=0x11;pixels[4*i+1]=0x22;pixels[4*i+2]=0x33;pixels[4*i+3]=0x44;}
  s.rsx.renderer->graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(surface);assert(surface.readback);}
 // Capture versions after bind, which deliberately preserves the legacy
 // whole-range reservation invalidation. Commit must touch only row payload.
 std::pair<std::uintptr_t,std::uint64_t> data;{GuestMemory::RawPermit raw(s.memory);GuestMemory::ObserverPause pause(s.memory);data=s.memory.reservation_token(0x81010000,4);padding=s.memory.reservation_token(0x81010080,4);}
 session.flush();assert(s.memory.load(0x81010000,4)==0x44112233 && s.memory.load(0x81010700,4)==0x44112233);
 assert(s.memory.load(0x81010030,4)==0xdeadbeef);assert(s.memory.reservation_token(0x81010000,4)!=data && s.memory.reservation_token(0x81010080,4)==padding);
 s.rsx.graphics_session=nullptr;std::puts("PASS packed readback uses parallel lease writes with alias versions and untouched row padding");
}
int main(int argc,char**){try{setenv("ANYPS3_RSX_VK_PROFILE","1",1);
 if(argc>1){query_();empty_prefix_();streaming_();visibility_();return 0;}  // second run: ANYPS3_RSX_LAZY_VISIBILITY=1
 registers_();prefix_();query_();visibility_();empty_prefix_();ring_();error_();flip_();streaming_();leases_();packed_leases_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN

class PipelineTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_timeline_prefixes_queries_lifetime_and_flip(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'test.cpp').write_text(native_source())
            command=['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')]
            result=subprocess.run(command,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_FRAME_DIR=str(root),VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            result=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('PASS '),11)
            self.assertIn('lazy visibility 0',result.stdout)
            result=subprocess.run([str(root/'test'),'lazy'],env=dict(env,ANYPS3_RSX_LAZY_VISIBILITY='1'),capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('PASS '),4)
            self.assertIn('lazy visibility 1',result.stdout)
