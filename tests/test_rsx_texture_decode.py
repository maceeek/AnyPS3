"""Fixed BE texture codecs through compute -> sampled VkImage -> real draw."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void fixed_(){
 using namespace rsxdraw;
 struct Case {unsigned format,a,b,c,d,expected;bool alpha=false,signed_=false;};
 const Case cases[]={
  {0x83,0xa1230000,0,0,0,0xaa112233},
  {0x84,0x07e00000,0,0,0,0xff00ff00},
  {0x85,0x80445566,0,0,0,0x80445566},
  {0x9a,0x3c003800,0x34003c00,0,0,0xffff8040},
  {0x9b,0x3f800000,0x3f000000,0x3e800000,0x3f800000,0xffff8040},
  {0x90,0x8000002a,0,0,0,0x80808080},
  {0x92,0x80000000,0,0,0,0x80808080},
  // BC1 little-endian blue endpoint, forced compute path by alpha kill.
  {0x86,0x1f000000,0,0,0,0xff0000ff,true},
  {0x85,0xff801122,0,0,0,0xff001122,false,true}
 };
 auto device=std::make_unique<NativeVulkan>();
 for(const auto& test:cases){PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;
  R[0x1a04]=0x00010029|((test.format|0x20u)<<8);R[0x1a18]=0x00040004;R[0x1840]=64;R[0x1a14]=(test.signed_?0x20000000u:0u)|0x01010000;
  if(test.alpha)R[0x1a0c]|=4;
  if(test.format>=0x90 && test.format<=0x93)R[0x1a08]&=0x0fffffff;
  for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002100+i*8+c*4,4,0);
  s.memory.store(0xc0002000,4,test.a);s.memory.store(0xc0002004,4,test.b);s.memory.store(0xc0002008,4,test.c);s.memory.store(0xc000200c,4,test.d);
  s.rsx.renderer=std::move(device);const auto before=host_stats().cpu_texel,dispatches=s.rsx.renderer->graphics_texture_decodes;
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
  const auto result=s.memory.load(0xc0100000+4*32+4*4,4);
  if(!near_argb(result,test.expected))throw std::runtime_error("fixed codec="+std::to_string(test.format)+" got="+std::to_string(result)+" expected="+std::to_string(test.expected));
  assert(host_stats().cpu_texel==before && s.rsx.renderer->graphics_texture_decodes>dispatches);
  device=std::move(s.rsx.renderer);std::printf("PASS fixed GPU texture format=%x bytes=%08x expected_ARGB=%08x no CPU texel\n",test.format,test.a,test.expected);
 }
}
static void capacity_(bool resize){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;
 R[0x1a00]=0x600000;R[0x1a18]=0x02000200;R[0x1840]=2048;
 std::vector<std::uint8_t> bytes(1024*1024);for(unsigned i=0;i<bytes.size();i+=4){bytes[i]=0x80;bytes[i+1]=0xaa;bytes[i+2]=0xbb;bytes[i+3]=0xcc;}
 s.memory.transfer(0xc0600000,bytes.data(),bytes.size(),true);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.batch=s.rsx.renderer->graphics_batch(1024*1024);if(!resize)session.upload_arena_limit=1024*1024;
 session.observe();s.rsx.graphics_session=&session;const auto texels=rsxdraw::host_stats().cpu_texel;
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);session.flush();
 assert(near_argb(s.memory.load(0xc0100000+4*32+4*4,4),0x80aabbcc));
 if(resize){assert(session.batch->capacity>=2*1024*1024 && rsxdraw::host_stats().cpu_texel==texels && s.rsx.renderer->graphics_texture_decodes>0);}
 else{assert(session.batch->capacity==1024*1024 && rsxdraw::host_stats().cpu_texel-texels==262144);}
 s.rsx.graphics_session=nullptr;std::puts(resize?"PASS GPU arena growth preserves supported large textures without CPU texels":"PASS constrained SSBO capacity preserves prior supported upload and fixed pixels");
}
int main(){try{setenv("ANYPS3_RSX_HOST_PROFILE","1",1);fixed_();capacity_(true);capacity_(false);}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN

class TextureDecodeTests(unittest.TestCase):
    def test_fixed_native_compute_codecs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'test.cpp').write_text(native_source())
            command=['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')]
            result=subprocess.run(command,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            result=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('PASS '),11)
