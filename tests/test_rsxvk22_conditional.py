"""GPU-latched report predicates: exact samples, prefixes, aliases and false faults."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES
MAIN = r'''
static void conditional_setup22(PpuState& s,unsigned aa,bool visible){
 setup_draw(s,8,8,80,false);auto& r=s.rsx.registers;const unsigned nx=aa?2:1,ny=aa>=4?2:1;
 r[0x208]|=aa<<12;r[0x20c]=r[0x22c]=8*nx*4+4;r[0x324]=0;r[0x17cc]=1;r[0xa74]=1;r[0xa70]=0;r[0xa6c]=visible?0x207:0x200;
 s.rsx.reports=0x70008000;r[0x1a8]=0x66626660;
 const float green[]={0,1,0,1};for(unsigned c=0;c<4;++c)fpword(s,4+c,rsxdraw::float_bits(green[c]));
 for(unsigned target:{0x200000u,0x300000u})for(unsigned y=0;y<8*ny;++y){for(unsigned x=0;x<8*nx;++x)s.memory.store(0xc0000000+target+y*r[0x20c]+x*4,4,0x10203040);
  s.memory.store(0xc0000000+target+y*r[0x20c]+8*nx*4,4,0xdecafbad);}
 auto backing=s.memory.regions[0].bytes;s.memory.regions.push_back({0x81000000,6,backing});s.memory.mapping_changed();
 s.rsx.renderer=std::make_unique<NativeVulkan>();assert(s.rsx.renderer->graphics_conditional_rendering());
}
static void conditional_draw22(PpuState& s,unsigned offset){auto& g=s.rsx;g.registers[0x324]=0x01010101;g.registers[0xa74]=0;g.registers[0x17cc]=0;g.registers[0x210]=offset;g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);}
static void conditional_image22(PpuState& s,unsigned aa,unsigned offset,unsigned expected){const unsigned nx=aa?2:1,ny=aa>=4?2:1;
 for(unsigned y=0;y<8*ny;++y){for(unsigned x=0;x<8*nx;++x)assert(s.memory.load(0xc0000000+offset+y*s.rsx.registers[0x20c]+x*4,4)==expected);
  assert(s.memory.load(0xc0000000+offset+y*s.rsx.registers[0x20c]+8*nx*4,4)==0xdecafbad);}}
static void gpu_conditions22(){
 setenv("ANYPS3_RSX_GPU_CONDITIONAL","1",1);setenv("ANYPS3_RSX_BACKEND","gpu",1);
 for(unsigned aa:{0u,3u,4u,5u})for(bool visible:{false,true})for(bool flight:{false,true})for(unsigned mode:{0u,1u,2u}){
  if(mode==1 && !visible)continue; // exact uint32 wrap case needs a nonzero query
  PpuState s;conditional_setup22(s,aa,visible);auto& g=s.rsx;const unsigned samples=aa==0?1:aa==3?2:4,n=visible?64*samples:0;
  const unsigned base=mode==1?0u-n:0,type=mode==2?5:1,value=type==5?(n?0u:65535u):base+n;
  s.memory.store(g.reports+0x1428,4,value?0:1);
  rsxgraphics::Session session(*g.renderer,s);session.observe();g.graphics_session=&session;session.submit_draws=0;
  rsx_execute_draw(s);if(flight)session.submit_async("predicate_source");
  g.report_values[0]=base;assert(session.defer_report(g.reports+0x1420,type,true,true));
  const auto submits=session.async_submits,retires=session.async_retires;
  g.reports=0x81008000;rsx_set_render_enable(s,0x02000020);
  assert(session.conditional && session.async_submits==submits && session.async_retires==retires && !g.render_disabled);
  // RESET_REPORT changes live attribution, never an already latched prefix.
  for(auto& p:session.pending){p.report=false;}
  for(auto& f:session.flights)for(auto& p:f.pending){p.report=false;}
  g.report_values[0]=0;
  conditional_draw22(s,0x200000);session.flush();
  assert(s.memory.load(0x70009428,4)==value && s.memory.load(0x7000942c,4)==0);
  conditional_image22(s,aa,0x200000,value?0xff00ff00:0x10203040);
  assert(g.renderer->conditional_predicates==1 && g.renderer->conditional_draws==1 && session.predicate_arenas.size()<=4);
  rsx_set_render_enable(s,0x01000000);assert(!session.conditional && !g.render_disabled);g.graphics_session=nullptr;
 }
 std::puts("PASS GPU query predicates: zero/nonzero,exact32bitwrap,inverted stats,MSAA1/2/4,current/inflight prefixes,BE physical aliases,RESET attribution,padding,no CPU wait");
}
static void conditional_faults22(){
 for(bool visible:{false,true}){
  PpuState s;conditional_setup22(s,0,visible);auto& g=s.rsx;rsxgraphics::Session session(*g.renderer,s);session.observe();g.graphics_session=&session;session.submit_draws=0;
  rsx_execute_draw(s);assert(session.defer_report(g.reports+0x1420,1,false,false));rsx_set_render_enable(s,0x02000020);assert(session.conditional);
  fpword(s,0,(63u<<24)|(15u<<9)|1u);std::string error;
  try{conditional_draw22(s,0x200000);}catch(const std::runtime_error& e){error=e.what();}
  if(visible)assert(error=="RSX GPU draw unsupported: fragment_program; diagnostic CPU fallback requires ANYPS3_RSX_GPU_FALLBACK=1" && !g.render_disabled);else assert(error.empty() && g.render_disabled);
  assert(!session.conditional);session.flush();conditional_image22(s,0,0x200000,0x10203040);g.graphics_session=nullptr;
 }
 {PpuState s;conditional_setup22(s,0,false);auto& g=s.rsx;rsxgraphics::Session session(*g.renderer,s);session.observe();g.graphics_session=&session;session.submit_draws=0;
  rsx_execute_draw(s);assert(session.defer_report(g.reports+0x1420,1,false,false));rsx_set_render_enable(s,0x02000020);assert(session.conditional);
  session.failed=true;fpword(s,0,(63u<<24)|(15u<<9)|1u);std::string error;
  try{conditional_draw22(s,0x200000);}catch(const std::runtime_error& e){error=e.what();}
  assert(error=="RSX graphics session poisoned" && session.conditional && !g.render_disabled);g.graphics_session=nullptr;}
 std::puts("PASS false GPU predicate suppresses unexecuted guest FP validation;true predicate retains exact explicit unsupported stop");
}
static void conditional_latch22(){
 PpuState s;conditional_setup22(s,0,true);auto& g=s.rsx;rsxgraphics::Session session(*g.renderer,s);session.observe();g.graphics_session=&session;session.submit_draws=0;
 rsx_execute_draw(s);assert(session.defer_report(g.reports+0x1420,1,false,false));rsx_set_render_enable(s,0x02000020);assert(session.conditional);
 // Retire the producer first: a CPU store before completion may correctly
 // be overwritten by the later GPU report publication. The latched predicate
 // must survive a store that is demonstrably AFTER that publication.
 session.flush();assert(session.conditional);
 s.memory.store(g.reports+0x1428,4,0);conditional_draw22(s,0x200000);session.flush();conditional_image22(s,0,0x200000,0xff00ff00);
 rsx_set_render_enable(s,0x02000020);assert(!session.conditional && g.render_disabled);conditional_draw22(s,0x300000);conditional_image22(s,0,0x300000,0x10203040);
 rsx_set_render_enable(s,0x01000000);conditional_draw22(s,0x300000);session.flush();conditional_image22(s,0,0x300000,0xff00ff00);
 // A capture synchronization boundary resolves the resident GPU predicate to a concrete flag.
 g.registers[0x324]=0;g.registers[0xa74]=1;g.registers[0xa6c]=0x200;g.registers[0x17cc]=1;g.report_values[0]=0;
 g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.defer_report(g.reports+0x1430,1,false,false));rsx_set_render_enable(s,0x02000030);assert(session.conditional);
 rsx_capture_sync(s);assert(!session.conditional && g.render_disabled);g.graphics_session=nullptr;
 std::puts("PASS predicate latch survives later CPU report writes;next mode2 reloads,mode1 resets,capture resolves concrete flag");
}

static void conditional_mixed_prefix22(){
 PpuState s;conditional_setup22(s,0,true);auto& g=s.rsx;rsxgraphics::Session session(*g.renderer,s);session.observe();g.graphics_session=&session;session.submit_draws=0;
 g.report_values[0]=UINT64_C(0xffffff80);rsx_execute_draw(s);session.submit_async("mixed_first");const auto first=session.flights.front().batch->completion;
 g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.defer_report(g.reports+0x1420,1,false,false));
 // The report froze two query results (64+64). The first becomes CPU-known
 // after that snapshot. A third visible draw is later and is NOT attributed.
 session.retire(true,first);assert(g.report_values[0]==UINT64_C(0xffffffc0));
 g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);
 rsx_set_render_enable(s,0x02000020);assert(session.conditional);
 conditional_draw22(s,0x200000);session.flush();
 assert(s.memory.load(g.reports+0x1428,4)==0);conditional_image22(s,0,0x200000,0x10203040);
 assert(g.report_values[0]==0x40u);rsx_set_render_enable(s,0x01000000);g.graphics_session=nullptr;
 std::puts("PASS mixed predicate prefix: retired64+pending64+base wraps0;later64 is excluded from GPU predicate and BE report");
}

int main(){try{gpu_conditions22();conditional_faults22();conditional_latch22();conditional_mixed_prefix22();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+MAIN
class Rsxvk22ConditionalTests(unittest.TestCase):
    def test_gpu_report_predicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cpp=root/'test.cpp';binary=root/'test';cpp.write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',ANYPS3_RSX_GPU_FALLBACK='0',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('ANYPS3_REQUIRE_NVIDIA'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            run=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=240)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stderr);self.assertNotIn('Validation Warning',run.stderr)
            self.assertEqual(run.stdout.count('PASS '),4)
