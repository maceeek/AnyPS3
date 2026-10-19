"""Independent packed clear, queue-order and exact-byte snapshot oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
struct ClearScope20 {
 PpuState& s;rsxgraphics::Session session;
 explicit ClearScope20(PpuState& state):s(state),session(*state.rsx.renderer,state){session.observe();s.rsx.graphics_session=&session;s.rsx.graphics_clear=[this](unsigned mask){return rsxgraphics::clear(session,mask);};}
 ~ClearScope20(){s.rsx.graphics_session=nullptr;s.rsx.graphics_clear={};}
};
static void packed20(){
 struct Case{unsigned format,full,partial,mask;};
 // Expected raw BE words, independently specified for every supported packed format.
 const Case cases[]={{1,0x4cdd,0x54dd,0x10},{2,0xccdd,0xd4dd,0x10},{3,0xccdd,0x54dd,0x10},
  {4,0xaabbccdd,0xaa34ccdd,0x10},{5,0xaabbccdd,0xaa34ccdd,0x10},{8,0xaabbccdd,0xaa34ccdd,0x10},
  {9,0xdd,0x78,0x40},{10,0xccdd,0x56dd,0x20},{14,0xaabbccdd,0xaa34ccdd,0x10},
  {15,0xaabbccdd,0xaa34ccdd,0x10},{16,0xaabbccdd,0xaa34ccdd,0x10}};
 for(const auto& c:cases){PpuState s;setup_draw(s,8,4,64,false);auto& r=s.rsx.registers;r[0x208]=0x140|c.format;
  r[0x8c0]=8<<16;r[0x8c4]=4<<16;r[0x1d90]=0xaabbccdd;s.rsx.renderer=std::make_unique<NativeVulkan>();ClearScope20 scope(s);
  const unsigned bpp=rsxdraw::color_bytes(c.format);s.memory.store(0xc0100000, bpp,0x11223344);
  rsx_clear_surface(s,0xf0);assert(s.rsx.cpu_clears==0 && s.rsx.renderer->graphics_full_clears==1);
  {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(0xc0100000,bpp)==(bpp==1?0x44u:bpp==2?0x3344u:0x11223344u));}
  scope.session.flush();assert(s.memory.load(0xc0100000,bpp)==c.full);
  if(bpp<4 && rsxgraphics::color_format(c.format)==VK_FORMAT_R32_UINT){
   auto& vk=*s.rsx.renderer;const auto at=vk.graphics_transfer(*scope.session.batch,scope.session.surfaces.front().image,VK_IMAGE_ASPECT_COLOR_BIT,4);
   vk.graphics_finish(*scope.session.batch);unsigned raw=0;std::memcpy(&raw,static_cast<const char*>(scope.session.batch->map)+at,4);assert(raw==c.full);vk.graphics_reset(*scope.session.batch);
  }
  r[0x1d90]=0x12345678;r[0x8c0]=(3<<16)|1;r[0x8c4]=(2<<16)|1;rsx_clear_surface(s,c.mask);scope.session.flush();
  assert(s.rsx.cpu_clears==0);assert(s.memory.load(0xc0100000,bpp)==c.full);
  assert(s.memory.load(0xc0100000+64+bpp,bpp)==c.partial);assert(s.memory.load(0xc0100000+64+4*bpp,bpp)==c.full);
 }
 std::puts("PASS packed BE full/masked GPU clears, lazy publication and scissor for 11 formats");
}
static void layouts20(){
 for(unsigned df:{1u,2u})for(unsigned aa:{0u,3u,4u,5u})for(unsigned layout=0;layout<3;++layout){
  PpuState s;setup_draw(s,8,4,256,layout==2);auto& r=s.rsx.registers;
  const unsigned nx=aa?2:1,ny=aa>=4?2:1;
  r[0x208]=0x108|(df<<5)|(aa<<12);r[0x8c0]=8<<16;r[0x8c4]=4<<16;r[0x32c]=255;r[0x1d90]=0x193b5d7f;r[0x1d8c]=0xffffff35;
  if(layout==1)r[0x208]=(r[0x208]&~0xf00u)|0x200u|(3u<<16)|(2u<<24);
  s.rsx.renderer=std::make_unique<NativeVulkan>();ClearScope20 scope(s);rsx_clear_surface(s,0xf3);scope.session.flush();
  assert(s.rsx.cpu_clears==0 && s.rsx.renderer->graphics_full_clears==2);
  assert(s.memory.load(0xc0100000,4)==0x193b5d7f);assert(s.memory.load(layout==2?0xc0504000:0xc0500000,df==1?2:4)==(df==1?0xff35u:0xffffff35u));
  if(layout==0){assert(s.memory.load(0xc0100000+(4*ny-1)*256+(8*nx-1)*4,4)==0x193b5d7f);assert(s.memory.load(0xc0100000+8*nx*4,4)==0);}
  if(layout==1){assert(s.memory.load(0xc0100000+8*4*nx*ny*4-4,4)==0x193b5d7f);assert(s.memory.load(0xc0100000+8*4*nx*ny*4,4)==0);}
 }
 std::puts("PASS linear/swizzled/tiled color/Z16/Z24S8 clears, sample planes 1/2/4 and padding");
}
static void window20(){
 PpuState s;setup_draw(s,8,4,64,false);auto& r=s.rsx.registers;r[0x194]=0xfeed0001;r[0x210]=0x8000;
 r[0x2b8]=(1<<16)|2;r[0x8c0]=8<<16;r[0x8c4]=4<<16;r[0x1d90]=0xff2468ac;
 s.memory.store(0x70008044,4,0x87654321);s.memory.store(0x70008068,4,0x11223344);
 s.rsx.renderer=std::make_unique<NativeVulkan>();ClearScope20 scope(s);rsx_clear_surface(s,0xf0);scope.session.flush();
 assert(s.rsx.cpu_clears==0);assert(s.memory.load(0x70008048,4)==0xff2468ac);assert(s.memory.load(0x70008124,4)==0xff2468ac);
 assert(s.memory.load(0x70008044,4)==0x87654321);assert(s.memory.load(0x70008068,4)==0x11223344);
 // A partial stencil clear preserves all depth bits and unselected stencil bits.
 r[0x2b8]=0;r[0x32c]=15;r[0x1d8c]=0xffffff35;s.memory.store(0xc0500000,4,0x123456aa);
 rsx_clear_surface(s,2);scope.session.flush();assert(s.memory.load(0xc0500000,4)==0x123456a5);
 r[0x220]=0;r[0x3b0]=0x101000;r[0x32c]=255;
 for(unsigned raw:{0x010001u,0x7f0000u,0x7fffffu,0xfefffeu}){r[0x1d8c]=(raw<<8)|0x91;rsx_clear_surface(s,3);scope.session.flush();assert(s.memory.load(0xc0500000,4)==((std::uint64_t(raw)<<8)|0x91));}
 r[0x1d8c]=0xff000091;bool rejected=false;try{rsx_clear_surface(s,1);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 std::puts("PASS IO/window offsets, partial stencil and finite F24 raw storage; nonfinite stop");
}
static void watches20(){
 {PpuState late;setup_draw(late,8,4,64,false);late.rsx.registers[0x8c0]=8<<16;late.rsx.registers[0x8c4]=4<<16;late.rsx.registers[0x1d90]=0xff102030;
  late.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session dormant(*late.rsx.renderer,late);
  auto target=rsxdraw::bind_target(late,0xfeed0000,0x100000,64,8,4);dormant.bind(target,8,4,1,8,false);assert(late.memory.host_access_observer.pages.empty());
  dormant.observe();assert(late.memory.host_access_observer.pages.size()==1 && late.memory.host_access_observer.pages.begin()->second==4);
  assert(rsxgraphics::clear(dormant,0xf0));dormant.leave_fifo();assert(late.memory.load(0xc0100000,4)==0xff102030);dormant.flush();
 }
 PpuState s;setup_draw(s,8,4,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();ClearScope20 scope(s);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,64,8,4);scope.session.bind(target,8,4,1,8,false);
 auto& observer=s.memory.host_access_observer;const auto revision=observer.revision;const auto pages=observer.pages;
 scope.session.refresh_surface_watches();scope.session.refresh_surface_watches();assert(observer.revision==revision && observer.pages==pages);
 auto other=rsxdraw::bind_target(s,0xfeed0000,0x200000,64,8,4);scope.session.bind(other,8,4,1,8,false);
 assert(observer.pages.size()==pages.size()+1);scope.session.surfaces.erase(scope.session.surfaces.begin());scope.session.refresh_surface_watches();
 assert(observer.pages.size()==1);assert(observer.pages.begin()->second==4);scope.session.flush();scope.session.clear_surfaces();assert(observer.pages.empty());
 std::puts("PASS surface watch unchanged spans retain exact counts and removals unwatch");
}
static void gated20(){
 PpuState s;setup_draw(s,8,4,64,false);auto& r=s.rsx.registers;r[0x8c0]=8<<16;r[0x8c4]=4<<16;r[0x1d90]=0xff112233;
 s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;ClearScope20 scope(s);auto& session=scope.session;
 // Metadata and immutable data are ready before blocking execution.
 s.memory.store(0x70008000,4,0x01020304);s.memory.store(0x70008040,8,0x1122334455667788);
 auto mapping=s.memory.range(0x70008040,8,false);const auto part=mapping.parts.front();
 rsxdraw::Texture texture;texture.valid=true;texture.deferred=true;texture.plan=std::make_shared<rsxdraw::TexturePlan>();
 texture.plan->gathered=true;texture.plan->bytes=8;texture.plan->runs.push_back({part.data,8,0,part.owner,0x70008040,part.mapping_id});
 texture.levels.push_back({2,1,8,nullptr});
 VkSemaphore gate=VK_NULL_HANDLE;VkSemaphoreTypeCreateInfo type{};type.sType=VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;type.semaphoreType=VK_SEMAPHORE_TYPE_TIMELINE;
 VkSemaphoreCreateInfo create{};create.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;create.pNext=&type;assert(vkCreateSemaphore(vk.device,&create,nullptr,&gate)==VK_SUCCESS);
 const std::uint64_t one=1;VkTimelineSemaphoreSubmitInfo timeline{};timeline.sType=VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;timeline.waitSemaphoreValueCount=1;timeline.pWaitSemaphoreValues=&one;
 const VkPipelineStageFlags stage=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;VkSubmitInfo wait{};wait.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;wait.pNext=&timeline;wait.waitSemaphoreCount=1;wait.pWaitSemaphores=&gate;wait.pWaitDstStageMask=&stage;
 assert(vkQueueSubmit(vk.queue,1,&wait,VK_NULL_HANDLE)==VK_SUCCESS);
 rsx_clear_surface(s,0xf0);rsx_release_semaphore(s,0x70008000,0x89abcdef,true);session.leave_fifo();
 session.retire(false);assert(session.flights.size()==1);assert(session.batch->wait_ms==0);
 // Exact texture bytes on the label's protected page do not wait for it.
 rsxdraw::materialize_texture(s,texture);const std::uint8_t expected[]={0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88};assert(std::memcmp(texture.levels[0].data,expected,8)==0);
 r[0x1d90]=0xff445566;rsx_clear_surface(s,0xf0);rsx_release_semaphore(s,0x70008000,0x76543210,true);session.submit_async("test_capacity");session.retire(false);
 assert(session.flights.size()==2 && session.batch->wait_ms==0);
 {GuestMemory::ObserverPause pause(s.memory);assert(s.memory.load(0x70008000,4)==0x01020304);}
 VkSemaphoreSignalInfo signal{};signal.sType=VK_STRUCTURE_TYPE_SEMAPHORE_SIGNAL_INFO;signal.semaphore=gate;signal.value=1;assert(vkSignalSemaphore(vk.device,&signal)==VK_SUCCESS);
 session.flush();assert(s.memory.load(0x70008000,4)==0x76543210);assert(s.memory.load(0xc0100000,4)==0xff445566);
 assert(session.flights.empty());vkDestroySemaphore(vk.device,gate,nullptr);
 std::puts("PASS gated nonblocking release/retire, same-page texture snapshot and ordered final BE labels");
}
static void fp20(){
 PpuState s;setup_draw(s,8,4,64,false);auto fp=rsxdraw::load_fragment_program(s,rsxdraw::Reg{s.rsx});
 assert(fp.size()==1 && fp[0].d==0x01001e01 && fp[0].has_constant);
 assert(rsxdraw::float_bits(fp[0].constant[0])==0x3f800000 && rsxdraw::float_bits(fp[0].constant[3])==0x3f000000);
 // The 16-byte instruction straddles an IO megabyte at unrelated guest pages.
 auto words=s.memory.range(0xc0001000,32,false);std::uint8_t bytes[32];words.read(0,bytes,32);
 s.memory.map(0x72000000,0x100000,6,nullptr,0);s.memory.map(0x73000000,0x100000,6,nullptr,0);
 s.rsx.io[1]=0x72000000;s.rsx.io[2]=0x73000000;
 s.memory.range(0x720ffff8,8,true).write(0,bytes,8);s.memory.range(0x73000000,24,true).write(0,bytes+8,24);s.rsx.registers[0x8e4]=0x1ffffa;
 auto split=rsxdraw::load_fragment_program(s,rsxdraw::Reg{s.rsx});assert(split.size()==1 && split[0].d==0x01001e01);
 assert(rsxdraw::float_bits(split[0].constant[2])==0x3e800000);
 s.memory.regions.back().flags=0;s.memory.mapping_changed();bool rejected=false;try{(void)rsxdraw::load_fragment_program(s,rsxdraw::Reg{s.rsx});}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 std::puts("PASS FP instruction/inline constant BE blocks, discontiguous IO and permission rejection");
}

static void texture_batch20(){
 PpuState s;setup_draw(s,8,4,64,false);auto& m=s.memory;
 m.regions.push_back({0x71000000,6,m.regions[0].bytes});m.enable_concurrency();m.enable_page_broker();
 m.store(0x70003000,4,0x11223344);m.store(0x70003004,4,0x55667788);
 rsxdraw::Texture t;t.valid=true;t.deferred=true;t.plan=std::make_shared<rsxdraw::TexturePlan>();t.plan->gathered=true;t.plan->bytes=8;
 for(unsigned i=0;i<2;++i){auto range=m.range(i?0x71003004:0x70003000,4,false);const auto& part=range.parts.front();t.plan->runs.push_back({part.data,4,4*i,part.owner,range.address,part.mapping_id});}
 t.levels.push_back({2,1,8,nullptr});rsxdraw::materialize_texture(s,t);
 const std::uint8_t expected[]={0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88};assert(std::memcmp(t.levels[0].data,expected,8)==0);
 {GuestMemory::RawPermit raw(m);m.regions.back().flags=0;m.mapping_changed();}
 t.deferred=true;bool rejected=false;try{rsxdraw::materialize_texture(s,t);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 assert(m.load(0x70003000,8)==UINT64_C(0x1122334455667788));
 std::puts("PASS one-claim texture snapshots across physical aliases and revoked read permission");
}
int main(){try{packed20();layouts20();window20();watches20();gated20();fp20();texture_batch20();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {','class NativeVulkan {\npublic:',1)+MAIN

class Rsxvk20Tests(unittest.TestCase):
    def test_independent_clear_order_and_snapshot_oracles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cpp=root/'test.cpp';binary=root/'test';cpp.write_text(native_source())
            result=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(result.returncode,0,result.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('PASS '),7)
            self.assertNotIn('Validation Error',result.stderr)
            self.assertNotIn('Validation Warning',result.stderr)
