"""Independent spatial-coordinate oracles for ordinary 1D/2D sampling."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void check_color(const rsxdraw::Vec4& c,const rsxdraw::Vec4& expected,const char* label){
 if(c!=expected)throw std::runtime_error(std::string(label)+" unexpected sample");
}
static void cpu_coordinates(){
 using namespace rsxdraw;
 const float bad[]={bits_float(0x7fc12345),bits_float(0x7f800000),bits_float(0xff800000),1073741824.f,-1073741824.f};
 const Vec4 green{0,1,0,1},black{};
 for(unsigned dimension:{1u,2u,3u}){
  Texture t;t.dimension=dimension;t.mip_count=1;t.format=0x85;t.min=t.mag=2;t.wrap_s=t.wrap_t=t.wrap_r=4;
  t.remap_control={2,2,2,2};t.remap_channel={0,1,2,3};t.levels.resize(1);
  const unsigned char bytes[]={255,0,255,0};auto& l=t.levels[0];l.width=l.height=l.depth=1;l.pitch=4;l.data=bytes;
  for(float poison:bad){
   check_color(sample_texture(t,.5f,.5f,0,0,poison),dimension==3?black:green,"CPU unused/active Z");
   if(dimension==1)check_color(sample_texture(t,.5f,poison,0,0,poison),green,"CPU 1D ignores Y/Z");
   check_color(sample_texture(t,poison,.5f),black,"CPU active X remains invalid");
   if(dimension>1)check_color(sample_texture(t,.5f,poison),black,"CPU active Y remains invalid");
  }
 }
 // A valid large cube direction projects to the +Z face. Its original Z is
 // not a spatial coordinate of the selected 2D face.
 {Texture t;t.dimension=2;t.cube=true;t.mip_count=1;t.format=0x85;t.min=t.mag=1;t.wrap_s=t.wrap_t=t.wrap_r=3;
  t.remap_control={2,2,2,2};t.remap_channel={0,1,2,3};t.levels.resize(6);
  const unsigned char bytes[]={255,0,255,0};for(auto& l:t.levels){l.width=l.height=l.depth=1;l.pitch=4;l.data=bytes;}
  check_color(sample_texture(t,1,0,0,0,1073741824.f),green,"CPU projected cube ignores original Z");}
 // Z is still the comparison reference of a 2D depth texture. Ignoring its
 // spatial lane must not discard or replace the actual reference value.
 {Texture t;t.dimension=2;t.mip_count=1;t.format=0x90;t.shadow=1;t.min=t.mag=1;t.wrap_s=t.wrap_t=3;
  t.remap_control={2,2,2,2};t.remap_channel={0,1,2,3};t.levels.resize(1);
  const unsigned char bytes[]={128,0,0,0};auto& l=t.levels[0];l.width=l.height=l.depth=1;l.pitch=4;l.data=bytes;
  check_color(sample_texture(t,.5f,.5f,0,bits_float(0xff800000),bits_float(0xff800000)),Vec4{1,1,1,1},"CPU shadow keeps negative infinity reference");
  check_color(sample_texture(t,.5f,.5f,0,bits_float(0x7f800000),bits_float(0x7f800000)),black,"CPU shadow keeps positive infinity reference");}
 std::puts("PASS CPU 1D/2D unused coordinates, active 3D/invalid coordinates, cube projection");
}
static void coordinate_fp(PpuState& s,unsigned opcode,const rsxdraw::Vec4& coord){
 using namespace rsxdraw;
 // Fixed constant coordinate; TXD has zero gradients, TXL/TXB zero bias.
 fpword(s,0,(opcode<<24)|(15u<<9)|1u);fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,(0xe4u<<9));fpword(s,3,(0xe4u<<9));
 for(unsigned c=0;c<4;++c)fpword(s,4+c,float_bits(coord[c]));
}
static void gpu_coordinates(){
 using namespace rsxdraw;
 const float bad[]={bits_float(0x7fc12345),bits_float(0x7f800000),bits_float(0xff800000),1073741824.f,-1073741824.f};
 for(bool resident:{false,true})for(unsigned dimension:{1u,2u,3u}){
  if(resident && dimension!=2)continue;
  for(unsigned opcode:{23u,24u,25u,47u,49u})for(float poison:bad){
   PpuState s;setup_draw(s,4,4,64,false);auto& r=s.rsx.registers;
   setenv("ANYPS3_RSX_BACKEND","gpu",1);
   if(resident){
    // Prime an actual resident ARGB8 render target with exact green.
    r[0x210]=0x200000;
    for(unsigned c=0;c<4;++c)fpword(s,4+c,c==1 || c==3?0x3f800000:0);
    rsx_execute_draw(s);r[0x210]=0x100000;s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};
   }
   r[0x208]=(r[0x208]&~31u)|12;r[0x1d60]=0x40;
   r[0x1a00]=resident?0x200000:0x2000;r[0x1a04]=0x1a509|(dimension<<4);
   r[0x1a08]=0x30303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x2020000;
   r[0x1a18]=resident?0x40004:0x10001;r[0x1840]=(dimension==3?1u<<20:0u)|(resident?64u:4u);
   if(!resident)s.memory.store(0xc0002000,4,0xff00ff00);
   const Vec4 coord{.5f,dimension==1?poison:.5f,poison,opcode==24?1.f:0.f};
   coordinate_fp(s,opcode,coord);rsx_execute_draw(s);
   const unsigned expected[]={0,dimension==3?0u:0x3f800000u,0,dimension==3?0u:0x3f800000u};
   for(unsigned c=0;c<4;++c){const auto actual=s.memory.load(0xc0100000+c*4,4);
    if(actual!=expected[c])throw std::runtime_error("GPU coordinates dim="+std::to_string(dimension)+" op="+std::to_string(opcode)+" resident="+std::to_string(resident)+" component="+std::to_string(c));}
   assert(s.rsx.renderer->graphics_draws>0);
  }
 }
 std::puts("PASS native Vulkan TEX/TXP/TXD/TXL/TXB: uploaded and resident textures, unused Y/Z, active 3D Z");
}
int main(int argc,char** argv){try{
 if(argc!=2)throw std::runtime_error("cpu | gpu");
 if(std::string(argv[1])=="cpu")cpu_coordinates();else gpu_coordinates();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
class UnusedTextureCoordinateTests(unittest.TestCase):
    def test_native_cpu_and_vulkan_spatial_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = graphics_source().split('int main(int argc,char** argv)')[0] + MAIN
            (root/'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-g0', '-Wall', '-Wextra', '-Werror',
                                    '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                                    str(root/'test.cpp'), *LIBRARIES, '-o', str(root/'test')],
                                   capture_output=True, text=True, timeout=240)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                       ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                       VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation')
            for mode in ['cpu', 'gpu']:
                result = subprocess.run([str(root/'test'), mode], env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, mode+': '+result.stdout+result.stderr)
                self.assertNotIn('Validation Error', result.stderr)
                self.assertNotIn('Validation Warning', result.stderr)
