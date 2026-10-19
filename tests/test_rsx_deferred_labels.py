"""Completion labels: real Vulkan work, BE bytes and guarded first readers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void labels(bool raw){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();
 auto& vk=*s.rsx.renderer;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});
 const unsigned address=0x70008000;s.memory.store(address,4,0x01020304);auto* retained=s.memory.access(address,4,false);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,32,8,8);auto& surface=session.bind(target,8,8,1,8,false);
 std::vector<std::uint8_t> rgba(8*8*4);for(unsigned i=0;i<64;++i){const std::uint8_t c[]={0x11,0x22,0x33,0x44};std::memcpy(rgba.data()+i*4,c,4);}
 vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba.data());session.dirty_surface(surface);
 rsx_release_semaphore(s,address,0xaabbccdd,true);rsx_release_semaphore(s,address,0x89abcdef,true);
 assert(session.labels.size()==2 && session.batch->submissions==0 && retained[0]==1);
 s.rsx.graphics_session=nullptr;session.leave_fifo();assert(session.guard.touches(retained,4));
 // A producer on the same protected page must not manufacture a host read
 // just to test the same-value optimization.
 s.rsx.graphics_session=&session;rsx_release_semaphore(s,address+4,0);
 assert(session.batch->submissions==1);s.rsx.graphics_session=nullptr;session.leave_fifo();
 // Completion is explicit here; polling itself may observe old bytes.
 session.flush(false);
 if(raw){volatile const std::uint8_t* p=retained;assert(p[0]==0x89 && p[1]==0xab && p[2]==0xcd && p[3]==0xef);}
 else assert(s.memory.load(address,4)==0x89abcdef);
 assert(session.labels.empty() && session.flights.empty() && session.batch->submissions>=1);
 assert(s.memory.load(0xc0010000,4)==0x44112233);
 s.rsx.graphics_owner.reset();std::puts(raw?"PASS retained pointer sees completed BE label publication":"PASS completed repeated BE labels retain queue order");
}
static void label_write_and_alias(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});
 s.memory.store(0x70008000,4,0x11223344);assert(session.defer_label(0x70008000,0xaabbccdd));
 s.rsx.graphics_session=nullptr;session.leave_fifo();session.flush(false);s.memory.store(0x70008000,4,0x55667788);
 assert(!session.has_labels() && s.memory.load(0x70008000,4)==0x55667788);
 // A store BEFORE completion is covered by the blocked-GPU oracle in
 // test_rsxvk20b; here the later CPU store must remain authoritative.
 // A physical alias through another guest mapping observes the queued write.
 s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});s.rsx.graphics_session=&session;assert(session.defer_label(0x70008000,0xdeadbeef));
 s.rsx.graphics_session=nullptr;session.leave_fifo();session.flush(false);assert(s.memory.load(0x71008000,4)==0xdeadbeef);
 // A destination sharing resident bytes takes the original coherent path.
 s.rsx.graphics_session=&session;auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,32,8,8);session.bind(target,8,8,1,8,false);
 assert(!session.defer_label(0xc0010000,0x12345678));rsx_release_semaphore(s,0xc0010000,0x12345678,true);assert(s.memory.load(0xc0010000,4)==0x12345678);
 // A split destination remains supported by the synchronous fallback.
 auto owner=std::make_shared<GuestBacking>(2);s.memory.regions.push_back({0x72000000,6,owner});s.memory.regions.push_back({0x72000002,6,std::make_shared<GuestBacking>(2)});
 assert(!session.defer_label(0x72000000,0x89abcdef));
 s.rsx.graphics_session=nullptr;session.guard.disarm();session.flush();s.rsx.graphics_owner.reset();std::puts("PASS CPU overwrite, physical alias, surface overlap and split fallback");
}

static void immediate_(){
 setenv("ANYPS3_RSX_HOST_PROFILE","1",1);PpuState s;setup_draw(s,8,8,32,false);
 for(unsigned v=0;v<4;++v){std::array<RsxDrawState::Immediate,16> attributes{};
  attributes[0].count=4;attributes[0].type=0;
  const float x=(v==1 || v==2)?8.f:-8.f,y=v>=2?8.f:0.f;
  const float position[]={x,y,.25f,1};for(unsigned c=0;c<4;++c)attributes[0].words[c]=rsxdraw::float_bits(position[c]);
  attributes[1].count=2;attributes[1].type=0;s.rsx.draw.immediate_vertices.push_back(attributes);
 }
 const auto vp=rsxdraw::host_stats().cpu_vertex,clip=rsxdraw::host_stats().cpu_clip;
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
 assert(near_argb(s.memory.load(0xc0100000+4*32+4*4,4),0x80ff8040));
 assert(rsxdraw::host_stats().cpu_vertex==vp && rsxdraw::host_stats().cpu_clip==clip);
 std::puts("PASS immediate GPU VP and clipping with fixed interior color");
}

static void reports_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.registers[0x17cc]=1;s.rsx.registers[0x1a8]=0xfeed0000;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});setenv("ANYPS3_RSX_BACKEND","gpu",1);
 rsx_execute_draw(s);rsx_get_report(s,0x01020000);assert(session.batch->submissions==0);
 // A later reset/new draw must not relabel an already issued query prefix.
 rsx_graphics_reset_report(s,1);s.rsx.report_values[0]=0;
 s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);rsx_get_report(s,0x01020010);
 rsx_get_report(s,0x02020020);rsx_get_report(s,0x05020030);
 assert(session.batch->submissions==0);s.rsx.graphics_session=nullptr;session.leave_fifo();
 session.flush(false);assert(s.memory.load(0xc0020008,4)==64);assert(session.batch->submissions==1);
 assert(s.memory.load(0xc0020018,4)==64);assert(s.memory.load(0xc0020028,4)==0);assert(s.memory.load(0xc0020038,4)==0);
 const auto timestamp=s.memory.load(0xc0020000,8);assert(timestamp>0 && s.memory.load(0xc0020010,8)>timestamp);
 for(unsigned offset:{0u,16u,32u,48u})assert(s.memory.load(0xc002000c+offset,4)==0);
 assert(session.labels.empty());
 // A same-value label still overrides bytes covered by an earlier pending
 // report, even when its address is inside (not equal to) the report range.
 s.rsx.graphics_session=&session;assert(session.defer_report(0xc0020040,1,false,false));
 rsx_release_semaphore(s,0xc0020048,0,true);s.rsx.graphics_session=nullptr;session.leave_fifo();session.flush(false);
 assert(s.memory.load(0xc0020048,4)==0);
 s.rsx.graphics_owner.reset();std::puts("PASS deferred report prefix survives reset/new draw, fixed visibility and BE timestamp/status");
}
int main(){try{labels(false);labels(true);label_write_and_alias();immediate_();reports_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN

class DeferredLabelsTests(unittest.TestCase):
    def test_first_readers_and_aliases(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.cpp').write_text(native_source())
            p=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(p.returncode,0,p.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            p=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertNotIn('Validation Error',p.stdout+p.stderr);self.assertNotIn('Validation Warning',p.stdout+p.stderr)
            self.assertEqual(p.stdout.count('PASS '),5)
