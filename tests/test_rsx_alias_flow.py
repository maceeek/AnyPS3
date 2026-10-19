"""RSXVK6 shared color/depth bytes and dynamic FP helper-quad regression."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_parity import native_source as parity_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void alias_tests(){
 std::unique_ptr<NativeVulkan> renderer;
 for(unsigned mode=0;mode<37;++mode){PpuState cpu,gpu;gpu.rsx.renderer=std::move(renderer);const auto before=gpu.rsx.renderer?gpu.rsx.renderer->compute_dispatches:0;
  for(auto* s:{&cpu,&gpu}){setup_draw(*s,8,8,mode==16?512:128,mode==16);auto& r=s->rsx.registers;
   r[0x214]=r[0x210];r[0x22c]=r[0x20c];r[0xa74]=1;r[0xa6c]=0x207;r[0xa70]=mode!=0;
   // Destination-only blending must observe the newly written Z, not old color.
   if(mode==1 || mode==2){r[0x310]=1;r[0x314]=0;r[0x318]=0x10001;r[0x320]=0x80068006;}
   if(mode==2 || mode==3){r[0xa6c]=0x201;s->rsx.draw.ranges={{0,4},{0,4}};}
   if(mode==3)r[0x324]=0x100;
   if(mode==4)r[0x214]+=4;
   if(mode==5)r[0x210]+=4;
   if(mode==6)r[0x22c]+=4;
   if(mode==7){r[0x208]=(r[0x208]&~255u)|3u|0x20u;r[0x20c]=r[0x22c]=32;}
   if(mode==8){r[0x208]=(r[0x208]&~31u)|9u;r[0x20c]=32;r[0x22c]=40;}
   if(mode==9){r[0x208]=(r[0x208]&~31u)|10u;r[0x20c]=32;r[0x210]+=2;}
   if(mode==10 || mode==11){r[0x208]=(r[0x208]&~31u)|(mode==10?11:12);r[0x20c]=256;}
   if(mode==12){r[0x208]=(r[0x208]&~31u)|13u;}
   if(mode==13 || mode==14 || mode==15){const unsigned aa=mode==13?3:mode==14?4:5;r[0x208]|=aa<<12;r[0x20c]=r[0x22c]=256;}
   if(mode==17){r[0x208]=(r[0x208]&~0xf00u)|0x200u|(3u<<16)|(3u<<24);}
   if(mode==18){r[0x328]=1;r[0x32c]=255;r[0x330]=0x207;r[0x334]=0x7b;r[0x338]=255;r[0x33c]=0x1e00;r[0x340]=0x8507;r[0x344]=0x1e01;r[0x324]=0;}
   if(mode==19){r[0x328]=1;r[0x32c]=255;r[0x330]=0x200;r[0x334]=0;r[0x338]=255;r[0x33c]=0x8507;r[0x340]=0x8507;r[0x344]=0x1e00;}
   if(mode==20){r[0x380]=1;r[0x384]=0;r[0x388]=rsxdraw::float_bits(.1f);}
   if(mode==21){r[0x374]=1;r[0x378]=0x1506;}
   if(mode==22){r[0x1fec]=1;r[0x310]=1;r[0x314]=0x03020302;r[0x318]=0x03030303;r[0x320]=0x80068006;}
   if(mode==23){r[0x2c0]=(5u<<16)|1;r[0x2c4]=(6u<<16)|2;r[0x2bc]=1;r[0x147c]=1;for(unsigned i=0;i<32;++i)r[0x1480+4*i]=0xaaaaaaaa;}
   if(mode==24){texture_fp(*s);}
   if(mode==25){r[0xa68]=1;r[0xa78]=0;r[0xa7c]=rsxdraw::float_bits(2.f);r[0x324]=0;}
   if(mode==26){r[0x220]=0x13;r[0x218]=r[0x210]+4;r[0x21c]=r[0x20c];r[0x18c]=0xfeed0000;r[0x370]=0xf0;}
   if(mode==27){r[0x208]=(r[0x208]&~31u)|3u;r[0x20c]=64;r[0x22c]=68;r[0x374]=1;r[0x378]=0x150f;}
   if(mode==28){r[0x208]=(r[0x208]&~31u)|11u;r[0x20c]=128;r[0x208]=(r[0x208]&~0xf00u)|0x200u|(3u<<16)|(3u<<24);}
   if(mode==29){r[0x208]=(r[0x208]&~31u)|12u;r[0x20c]=128;r[0x208]=(r[0x208]&~0xf00u)|0x200u|(3u<<16)|(3u<<24);}
   if(mode==30 || mode==31){r[0xa70]=0;r[0xa6c]=mode==30?0x200:0x201;}
   if(mode==32 || mode==33){s->rsx.draw.primitive=mode==32?1:2;s->rsx.draw.ranges={{0,mode==32?1u:2u}};r[0x1ff4]=0x3fffdf;r[0x1ee0]=rsxdraw::float_bits(3);r[0x3b8]=8;if(mode==33){const float p[2][2]={{1,3},{7,3}};for(unsigned v=0;v<2;++v)for(unsigned c=0;c<2;++c)s->memory.store(0x70002000+16*v+4*c,4,rsxdraw::float_bits(p[v][c]));}}
   if(mode==34){s->memory.regions.push_back({0x80000000,6,s->memory.regions[1].bytes});s->rsx.io[1]=0x80100000;r[0x198]=0xfeed0001;}
   if(mode==35){auto saved=*s->memory.regions[1].bytes;s->memory.regions.erase(s->memory.regions.begin()+1);
    const unsigned split=0x100002;s->memory.map(0xc0000000,split,6,saved.data(),split);s->memory.map(0xc0000000+split,saved.size()-split,6,saved.data()+split,saved.size()-split);}
   if(mode==36)r[0x214]+=32; // overlap only in pitched padding, no pixel bytes alias
   // Entire alias union starts at known BE bytes, including row padding.
   if(mode==35){const unsigned bytes[]={255,255,255,0x5a};for(unsigned i=0;i<8192;++i)s->memory.store(0xc0100000+i,1,bytes[i&3]);}
   else for(unsigned i=0;i<8192;i+=4)s->memory.store(0xc0100000+i,4,0xffffff5a);
  }
  setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  if(mode==0)assert(gpu.rsx.renderer->graphics_draws>0 && gpu.rsx.renderer->compute_dispatches==0);
  else if((rsxdraw::Reg{gpu.rsx}.get(0x324,0x01010101)&0x01010101u)==0 && !rsxdraw::Reg{gpu.rsx}(0x370))assert(gpu.rsx.renderer->graphics_draws>0 && gpu.rsx.renderer->compute_dispatches==before);
  else if(gpu.rsx.renderer->compute_dispatches!=before+1)throw std::runtime_error("alias did not execute GPU compute mode="+std::to_string(mode));
  unsigned delta=0;for(unsigned i=0;i<(mode==16?65536u:8192u);++i){const unsigned a=cpu.memory.load(0xc0100000+i,1),b=gpu.memory.load(0xc0100000+i,1);delta=std::max(delta,a>b?a-b:b-a);}
  if(delta>(mode==24 || mode==22?1u:0u))throw std::runtime_error("alias mismatch mode="+std::to_string(mode)+" delta="+std::to_string(delta));
  assert(cpu.rsx.draw.pixels==gpu.rsx.draw.pixels);assert(cpu.rsx.draw.triangles==gpu.rsx.draw.triangles);
  if(mode==32 || mode==33)assert(gpu.rsx.draw.pixels>0);
  if(mode==1)assert(gpu.memory.load(0xc0100000,4)==0x4000005a); // Z=.25 then dst-only blend
  if(mode==4){assert(gpu.memory.load(0xc0100000,4)==0x80ff8040);assert(gpu.memory.load(0xc0100020,4)==0x4000005a);assert(gpu.memory.load(0xc0100024,4)==0xffffff5a);} // fixed cross-quad byte-alias and padding oracles
  if(mode==18)assert(gpu.memory.load(0xc0100000,4)==0x4000007b); // BE Z24 + stencil replace
  if(mode==19)assert(gpu.memory.load(0xc0100000,4)==0xffffff5b); // stencil fail, color suppressed
  if(mode==30)assert(gpu.memory.load(0xc0100000,4)==0xffffff5a); // NEVER, no writes
  std::printf("RSXVK6 alias=%u delta=%u pixels=%llu\n",mode,delta,(unsigned long long)gpu.rsx.draw.pixels);
  renderer=std::move(gpu.rsx.renderer);
 }
}
static void flow_program(PpuState& s,unsigned mode){
 // IF tests initial CC==0, all helper lanes take the same dynamic branch.
 // DDX occurs inside (mode 1), outside (mode 0), or after divergent ALU (2).
 using namespace rsxdraw;std::vector<FpInst> fp;
 const unsigned sw=0xe4u<<9,exec=7u<<18;
 auto mov=[&](unsigned dst,Vec4 value,bool end=false){FpInst i{(1u<<24)|(dst<<1)|(15u<<9)|unsigned(end),2u|sw|exec,0,0,value,true};return i;};
 auto flow=[&](unsigned op,unsigned test,unsigned other,unsigned after){return FpInst{((op&63)<<24),test<<18,0x80000000u|other,after,{},false};};
 if(mode==2){auto i=mov(0,{-1,-1,-1,-1});i.d=(3u<<24)|(1u<<9)|256;i.s0=1u|exec;i.s1=2;i.s2=0;fp.push_back(i);}
 const unsigned base=mode==2?8:0;
 fp.push_back(flow(66,mode==2?1u:2u,base+12,base+20));
 fp.push_back(mov(0,{.25,.5,.75,1}));
 fp.push_back(mov(0,{.75,.5,.25,1}));
 FpInst derivative{(21u<<24)|(15u<<9)|1u,1u|sw|exec,0,0,{},false};derivative.d|=4u<<13;
 if(mode==2)derivative.s0=sw|exec;
 if(mode==1){fp.insert(fp.begin()+1,derivative);fp[1].d&=~1u;fp[0].s1=0x80000000u|16;fp[0].s2=24;}
 fp.push_back(derivative);
 unsigned word=0;for(const auto& i:fp){fpword(s,word++,i.d);fpword(s,word++,i.s0);fpword(s,word++,i.s1);fpword(s,word++,i.s2);if(i.has_constant)for(float v:i.constant)fpword(s,word++,float_bits(v));}
}
static void flow_tests(){
 std::unique_ptr<NativeVulkan> renderer;
 for(unsigned mode=0;mode<7;++mode){PpuState cpu,gpu;gpu.rsx.renderer=std::move(renderer);const auto before=gpu.rsx.renderer?gpu.rsx.renderer->graphics_draws:0;for(auto* s:{&cpu,&gpu}){setup_draw(*s,8,8,128,false);
   if(mode==3 || mode==4)texture_fp(*s);
   flow_program(*s,mode==3?1u:mode==4?0u:mode);
   if(mode==3 || mode==4){auto fp=rsxdraw::load_fragment_program(*s,rsxdraw::Reg{s->rsx});for(const auto& i:fp)if(((i.d>>24)&63)==21)fpword(*s,i.word_offset,(i.d&~0x3f000000u)|(23u<<24));}
   if(mode==5){s->rsx.draw.primitive=2;s->rsx.draw.ranges={{0,2}};s->rsx.registers[0x3b8]=8;const float p[2][2]={{1,3},{7,3}};for(unsigned v=0;v<2;++v)for(unsigned c=0;c<2;++c)s->memory.store(0x70002000+16*v+4*c,4,rsxdraw::float_bits(p[v][c]));}
   if(mode==6){s->rsx.registers[0x208]|=3u<<12;s->rsx.registers[0x20c]=128;}
  }
  setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  assert(gpu.rsx.renderer->graphics_draws>before);
  for(unsigned i=0;i<8192;++i){const auto a=cpu.memory.load(0xc0100000+i,1),b=gpu.memory.load(0xc0100000+i,1);assert(near_byte(a,b));}
  assert(cpu.rsx.draw.pixels==gpu.rsx.draw.pixels);if(mode==5)assert(gpu.rsx.draw.pixels>0);
  if(mode==2)assert(near_argb(gpu.memory.load(0xc0100000,4),0x00800000));
  if(mode==0)assert(near_argb(gpu.memory.load(0xc0100000,4),0x00300000));
  std::printf("RSXVK6 flow=%u\n",mode);renderer=std::move(gpu.rsx.renderer);
 }
}
static void divergence_tests(){
 for(bool aliased:{false,true})for(bool gpu_backend:{false,true}){PpuState s;setup_draw(s,8,8,128,false);
  const unsigned exec=7u<<18,sw=0xe4u<<9;
  fpword(s,0,(3u<<24)|(1u<<9)|256);fpword(s,1,1u|exec);fpword(s,2,2u);fpword(s,3,0);
  for(unsigned i=0;i<4;++i)fpword(s,4+i,rsxdraw::float_bits(-1));
  fpword(s,8,2u<<24);fpword(s,9,1u<<18);fpword(s,10,0x80000000u|16u);fpword(s,11,24);
  fpword(s,12,(21u<<24)|(15u<<9));fpword(s,13,1u|exec|sw);fpword(s,14,0);fpword(s,15,0);
  fpword(s,16,(1u<<24)|(15u<<9));fpword(s,17,2u|exec|sw);fpword(s,18,0);fpword(s,19,0);
  for(unsigned i=0;i<4;++i)fpword(s,20+i,rsxdraw::float_bits(.25f));
  fpword(s,24,(1u<<24)|(15u<<9)|1u);fpword(s,25,2u|exec|sw);fpword(s,26,0);fpword(s,27,0);
  for(unsigned i=0;i<4;++i)fpword(s,28+i,rsxdraw::float_bits(.5f));
  if(aliased){s.rsx.registers[0x214]=s.rsx.registers[0x210];s.rsx.registers[0xa70]=1;}
  setenv("ANYPS3_RSX_BACKEND",gpu_backend?"gpu":"cpu",1);bool failed=false;
  try{rsx_execute_draw(s);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("divergent")!=std::string::npos;}
  assert(failed);for(unsigned i=0;i<1024;++i)assert(s.memory.load(0xc0100000+i,1)==0);
 }
 std::puts("RSXVK6 divergent derivatives stop before guest commit on CPU/graphics/alias");
}
static void neutral_depth_validation(){
 for(bool gpu:{false,true}){PpuState s;setup_draw(s,8,8,128,false);
  s.memory.regions.push_back({0x80000000,6,s.memory.regions[1].bytes});s.rsx.io[1]=0x80100000;
  auto& r=s.rsx.registers;r[0x198]=0xfeed0001;r[0x214]=r[0x210];r[0xa74]=1;r[0xa6c]=0x207;r[0xa70]=0;
  auto& tile=s.rsx.tiles[0];tile.bound=true;tile.location=1;tile.offset=0x100000;tile.size=0x10000;tile.pitch=128;tile.bank=0;
  setenv("ANYPS3_RSX_BACKEND",gpu?"gpu":"cpu",1);bool failed=false;
  try{rsx_execute_draw(s);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("tile address")!=std::string::npos;}
  assert(failed && s.memory.load(0xc0100000,4)==0);
 }
 std::puts("RSXVK6 neutral depth validates the original storage before detaching");
}
static void rejection_tests(){
 using namespace rsxdraw;PpuState s;setup_draw(s,8,8,128,false);
 fpword(s,2,4u<<28); // reserved result scale, CPU must still raise its error
 setenv("ANYPS3_RSX_BACKEND","gpu",1);bool failed=false;
 try{rsx_execute_draw(s);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("scale")!=std::string::npos;}
 assert(failed && s.memory.load(0xc0100000,4)==0);
 rsxvk::FragmentFailure f;auto fp=load_fragment_program(s,Reg{s.rsx});assert(!rsxvk::fragment(fp,false,false,&f));assert(f.instruction==0 && f.reason=="scale");
 assert(rsxgraphics::fallback_stats().cpu_fallback_draws==2 && rsxgraphics::fallback_stats().cpu_fallback_ns>0);
 std::puts("RSXVK6 explicit rejection and fallback timing");
}
int main(){setenv("ANYPS3_RSX_GPU_FALLBACK","1",1);std::setvbuf(stdout,nullptr,_IOLBF,0);try{alias_tests();flow_tests();divergence_tests();neutral_depth_validation();rejection_tests();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}return 0;}
'''


def native_source():
    return parity_source().replace('int main(){', 'int previous_main(){') + MAIN


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class AliasFlowTests(unittest.TestCase):
    def test_shared_bytes_and_dynamic_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(native_source())
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')], capture_output=True, text=True, timeout=180)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1', ANYPS3_RSX_FALLBACK_STATS='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True, env=env, timeout=180)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn('Validation Error', run.stdout + run.stderr)
            self.assertNotIn('Validation Warning', run.stdout + run.stderr)
            for text in ('path_component_attachment_alias=', 'components=4 largest_quads=4 total_quads=16', 'path_neutral_depth_alias=1', 'path_scalar_fragment=', 'cpu_fallback_host_ms=', 'gpu_attempt_host_ms=', 'RSX FALLBACK DETAIL reason=color_depth_alias', 'RSX FALLBACK DETAIL reason=fragment_program cause=scale'):
                self.assertIn(text, run.stderr)
