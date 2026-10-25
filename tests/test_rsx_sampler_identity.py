"""Sampler state remains live while implicit-only GLSL identities stay stable."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void source_identity(){
 PpuState s;setup_draw(s,4,4,16,false);texture_fp(s);
 const rsxdraw::Reg reg{s.rsx};auto fp=rsxdraw::load_fragment_program(s,reg);
 std::array<rsxdraw::Texture,16> textures{};auto& t=textures[0];
 t.valid=true;t.format=0x85;t.dimension=2;t.wrap_s=t.wrap_t=t.wrap_r=3;t.min=t.mag=1;t.mip_count=1;t.levels.push_back({2,2,8});
 for(unsigned op:{23u,24u,49u}){
  fp[0].d=(fp[0].d&0x00ffffffu)|(op<<24);
  auto first=rsxgraphics::fragment_source(fp,reg,1,textures);assert(first);
  for(unsigned mip:{1u,2u,5u,9u})for(unsigned min=1;min<=6;++min)for(unsigned mag:{1u,2u}){
   t.mip_count=mip;t.min=min;t.mag=mag;
   const auto raw=rsxgraphics::fragment_source_uncached(fp,reg,1,textures);
   const auto hit=rsxgraphics::fragment_source(fp,reg,1,textures);
   assert(raw && *raw==*first && hit==first);
  }
 }
 // Explicit LOD/derivatives still depend on specialized manual filtering,
 // including a unit used by both implicit and explicit instructions.
 for(unsigned op:{25u,47u}){
  for(bool mixed:{false,true}){
   auto code=fp;code[0].d=(code[0].d&0x00ffffffu)|(op<<24);
   if(mixed){code.push_back(code[0]);code[0].d=(code[0].d&0x00fffffeu)|(23u<<24);code[1].word_offset=4;}
   t.min=4;auto a=rsxgraphics::fragment_source(code,reg,1,textures);t.min=6;auto b=rsxgraphics::fragment_source(code,reg,1,textures);
   assert(a && b && a!=b && *a!=*b);
  }
 }
 fp[0].d=(fp[0].d&0x00ffffffu)|(23u<<24);
 // Border-wrap, cube, shadow, alpha kill, and degenerate volume layouts
 // must retain the manual path and its filtering identity.
 for(unsigned mode=0;mode<5;++mode){auto altered=textures;auto& a=altered[0];
  if(mode==0)a.wrap_s=4;
  if(mode==1)a.cube=true;
  if(mode==2)a.shadow=1;
  if(mode==3)a.alpha_kill=true;
  if(mode==4){a.dimension=3;a.levels[0].depth=1;}
  a.min=4;auto before=rsxgraphics::fragment_source(fp,reg,1,altered);a.min=6;auto after=rsxgraphics::fragment_source(fp,reg,1,altered);
  assert(before && after && before!=after && *before!=*after);
 }
 std::puts("PASS sampler identity: 144 implicit states, explicit/mixed/manual distinctions");
}
static void live_sampler(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 PpuState s;setup_draw(s,4,4,16,false);texture_fp(s);auto& r=s.rsx.registers;
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002100+i*8+c*4,4,rsxdraw::float_bits(.375f));
 const unsigned colors[]={0xff000000,0xffff0000,0xff00ff00,0xff0000ff,0xffffffff};
 for(unsigned i=0;i<5;++i)s.memory.store(0xc0002000+4*i,4,colors[i]);
 std::uint64_t modules=0,pipelines=0;
 for(unsigned pass=0;pass<6;++pass){const bool linear=pass%2;const unsigned mip=pass<2?1:2,min=pass<4?4:6;
  r[0x1a04]=(mip<<16)|0x29|(0xa5u<<8);r[0x1a14]=((linear?2u:1u)<<24)|(min<<16);
  s.rsx.draw.ranges={{0,4}};const auto draws=s.rsx.draw.draws;
  rsx_execute_draw(s);assert(s.rsx.renderer && s.rsx.renderer->graphics_draws && s.rsx.draw.draws==draws+1);
  // At UV=(3/8,3/8), nearest selects black. Linear weights are
  // 9/16 black, 3/16 red, 3/16 green, 1/16 blue, independently derived.
  for(unsigned y=0;y<4;++y)for(unsigned x=0;x<4;++x){const auto actual=s.memory.load(0xc0100000+y*16+x*4,4);if(!near_argb(actual,linear?0xff303010u:0xff000000u)){
   std::fprintf(stderr,"sampler pass=%u xy=%u,%u actual=%08llx linear=%u\n",pass,x,y,(unsigned long long)actual,unsigned(linear));throw std::runtime_error("sampler color mismatch");}}
  const auto& stats=s.rsx.renderer->compile_stats;
  if(!pass){modules=stats.shader_modules;pipelines=stats.graphics_pipelines;}
  else assert(stats.shader_modules==modules && stats.graphics_pipelines==pipelines);
 }
 std::puts("PASS live sampler: six exact-color draws, one shader/pipeline identity");
}
int main(){try{source_identity();live_sampler();}catch(const std::exception& e){std::fprintf(stderr,"SAMPLER TEST STOP %s\n",e.what());return 1;}}
'''

class SamplerIdentityTests(unittest.TestCase):
    def test_sampler_changes_reuse_shader_and_preserve_colors(self):
        with tempfile.TemporaryDirectory(prefix='anyps3-sampler-') as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')], capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='2', VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            result = subprocess.run([str(root / 'test')], capture_output=True, text=True, env=env, timeout=180)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn('Validation Error', output)
            self.assertNotIn('Validation Warning', output)
            print(result.stdout, flush=True)
