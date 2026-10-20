"""Independent FP alias/depth oracles and bounded shader compilation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void scalar_alias(){
 PpuState s;setup_draw(s,8,8,128,false);auto& r=s.rsx.registers;
 r[0x208]=(r[0x208]&~31u)|12;r[0x1d60]=0x40;
 // R0 = bit containers, then H0.x = .5. The untouched high half is 2.
 fpword(s,0,(1u<<24)|(15u<<9));fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
 const unsigned bits[]={0x40003c00,0x40804200,0x3f800000,0x3f800000};for(unsigned c=0;c<4;++c)fpword(s,4+c,bits[c]);
 fpword(s,8,(1u<<24)|(1u<<9)|128u|1u);fpword(s,9,2u|(0xe4u<<9)|(7u<<18));fpword(s,10,0);fpword(s,11,0);
 for(unsigned c=0;c<4;++c)fpword(s,12+c,0x3f000000);
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
 assert(s.memory.load(0xc0100000,4)==0x40003800);assert(s.memory.load(0xc0100004,4)==0x40804200);
 assert(s.memory.load(0xc0100008,4)==0x3f800000);std::puts("PASS masked H0/R0 physical bit alias");
}
static void neutral_color(){
 for(unsigned target:{1u,0x13u})for(unsigned logic:{0u,1u}){
  PpuState s;setup_draw(s,8,8,32,false);auto& r=s.rsx.registers;customize(s,17);
  r[0x220]=target;r[0x324]=0;r[0x370]=0;r[0x374]=logic;r[0x378]=0x1506;r[0x310]=1;r[0x36c]=14;
  r[0x210]=r[0x214];r[0x194]=r[0x198];r[0x218]=r[0x214];r[0x21c]=r[0x22c];r[0x18c]=r[0x198];
  r[0xa6c]=0x203; // LEQUAL: exported .75 passes against 1.
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){s.memory.store(0xc0500000+y*32+x*4,4,0xffffff5a);s.memory.store(0xc0100000+y*32+x*4,4,0x12345678);}
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
  assert(s.rsx.renderer->graphics_draws>0);
  assert(s.memory.load(0xc0500000,4)==0xbfffff5a);
  assert(s.memory.load(0xc0100000,4)==0x12345678);
 }
 // Target A is neutral and aliases depth; B writes independently.
 {PpuState s;setup_draw(s,8,8,32,false);customize(s,17);auto& r=s.rsx.registers;
  r[0x220]=0x13;r[0x324]=0;r[0x370]=0xf0;r[0x210]=r[0x214];r[0x18c]=r[0x194];r[0x218]=0x100000;r[0x21c]=32;r[0xa6c]=0x203;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){s.memory.store(0xc0500000+y*32+x*4,4,0xffffff5a);s.memory.store(0xc0100000+y*32+x*4,4,0x12345678);}
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);assert(s.memory.load(0xc0500000,4)==0xbfffff5a);assert(s.memory.load(0xc0100000,4)==0);
 }
 std::puts("PASS mask-zero color/MRT/logic/blend, partial MRT, exact exported Z24 BE and stencil");
}
static void inactive_cube_helper(){
 for(bool take:{false,true})for(bool gpu:{false,true}){
  PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.registers[0x1a04]|=4;s.rsx.registers[0x1d60]=0x40;
  // Zero direction is invalid when executed; a uniformly inactive IF must
  // not raise a texture fault or poison the final color.
  fpword(s,0,2u<<24);fpword(s,1,(take?2u:1u)<<18);fpword(s,2,0x80000008);fpword(s,3,8);
  fpword(s,4,(23u<<24)|(15u<<9));fpword(s,5,(0xe4u<<9)|(7u<<18));fpword(s,6,0);fpword(s,7,0);
  fpword(s,8,(1u<<24)|(15u<<9)|1u);fpword(s,9,2u|(0xe4u<<9)|(7u<<18));fpword(s,10,0);fpword(s,11,0);
  const unsigned color[]={0x3e800000,0x3f000000,0x3f400000,0x3f800000};for(unsigned c=0;c<4;++c)fpword(s,12+c,color[c]);
  setenv("ANYPS3_RSX_BACKEND",gpu?"gpu":"cpu",1);bool failed=false;
  try{rsx_execute_draw(s);}catch(const std::runtime_error&){failed=true;}
  assert(failed==take);
  if(take)assert(s.memory.load(0xc0100000,4)==0);else assert(near_argb(s.memory.load(0xc0100000,4),0xff4080bf));
 }
 std::puts("PASS inactive cube helper skips faults, active invalid cube stops before commit");
}
static void source_oracles(){
 PpuState s;setup_draw(s,8,8,32,false);const auto code=rsxdraw::load_fragment_program(s,rsxdraw::Reg{s.rsx});std::array<rsxdraw::Texture,16> textures{};
 auto src=rsxgraphics::fragment_source_uncached(code,rsxdraw::Reg{s.rsx},1,textures);
 assert(src && src->find("precise vec4 R0")!=std::string::npos && src->find("uvec4 r[")==std::string::npos && src->find("void step")==std::string::npos && src->find("budget=")==std::string::npos && src->find("wr(q")==std::string::npos && src->find("rd(q")==std::string::npos);
 rsxvk::FragmentFailure failure;rsxdraw::FpInst bad{(65u&63u)<<24,(7u<<18),0x80000000,0,{},false};
 assert(!rsxgraphics::scalar_fragment({bad},&failure) && failure.reason=="CAL_unsupported_by_CPU_reference");
 std::puts("PASS scalar GLSL structure and explicit CAL reject");
}
int main(){try{scalar_alias();neutral_color();inactive_cube_helper();source_oracles();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN

def bounded(command, *, seconds=60, memory=8*1024**3, **kwargs):
    return subprocess.run(['timeout', str(seconds), 'prlimit', f'--as={memory}', f'--cpu={seconds}', '--', *command], timeout=seconds+5, **kwargs)

@unittest.skipUnless(shutil.which('c++') and shutil.which('prlimit'), 'Requires native compiler and resource limiter')
class ScalarFragmentTests(unittest.TestCase):
    def test_register_alias_depth_and_source_oracles(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.cpp').write_text(native_source())
            build=bounded(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],seconds=180,memory=8*1024**3,capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation')
            result=bounded([str(root/'test')],seconds=60,capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)

    def test_compiler_proxy_bounds(self):
        root=Path(__file__).resolve().parents[1]
        script=root/'local/ratchet/codex/rsxvk/rsxvk11/run_corpus.py'
        corpus=root/'local/ratchet/codex/rsxvk/rsxvk9/corpus-311g-01'
        if not script.exists():
            # The corpus runner is local, unversioned tooling (local/ is never committed).
            self.skipTest('local corpus runner not present in this checkout')
        with tempfile.TemporaryDirectory() as d:
            command=[os.sys.executable,str(script),str(Path(d)/'proxy')]
            if not list(corpus.glob('*.fpbin')):command.append('--synthetic-only')
            result=bounded(command,seconds=240,memory=8*1024**3,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
