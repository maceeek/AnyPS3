"""Independent wrapping and coordinate-mode colors for resident textures."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void resident_oracle(bool dynamic){
 PpuState guest;setup_draw(guest,2,2,8,false);texture_fp(guest);
 NativeVulkan vk;rsxgraphics::Session session(vk,guest);auto& b=*session.batch;
 NativeVulkan::GraphicsState fixed{};fixed.width=fixed.height=2;fixed.raster_width=fixed.raster_height=2;fixed.colors=1;
 fixed.scissor={{0,0},{2,2}};fixed.blend[0].colorWriteMask=15;
 auto source=vk.graphics_image(b,fixed.color,8,8,1),destination=vk.graphics_image(b,fixed.color,2,2,1);
 std::vector<std::uint8_t> pixels(8*8*4);
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){const auto at=(y*8+x)*4;pixels[at]=x*16;pixels[at+1]=y*16;pixels[at+2]=40;pixels[at+3]=255;}
 vk.graphics_transfer(b,source,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());
 const auto fp=rsxdraw::load_fragment_program(guest,rsxdraw::Reg{guest.rsx});
 const auto vs=rsxgraphics::vertex_source();const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{.5f,.5f},{.5f,.5f},{.5f,.5f}}};
 // Hand-calculated wrapping of -2 and 10 in an eight-texel image.
 // Mode 4 denotes the border color; 6/8 mirror the negative coordinate.
 const unsigned negative[]={0,6,1,0,0,0,1,0,1},positive[]={0,2,5,7,0,7,7,7,7};
 std::shared_ptr<const std::string> first;std::uint64_t modules=0,pipelines=0;unsigned cases=0;
 for(bool outside:{false,true})for(bool rectangle:{false,true})for(unsigned wrap=1;wrap<=8;++wrap){
  const unsigned wrap_y=9-wrap;
  std::vector<unsigned> data;session.serialize_draw_registers(data,2,2);const unsigned start=data.size();data[3]=start;data.resize(start+3*68);
  const float points[3][2]={{0,0},{4,0},{0,4}};
  const float u=outside?-.25f:.25f,v=outside?1.25f:.75f;
  for(unsigned i=0;i<3;++i){const auto at=start+i*68;data[at]=rsxdraw::float_bits(points[i][0]);data[at+1]=rsxdraw::float_bits(points[i][1]);data[at+3]=rsxdraw::float_bits(1.f);
   data[at+32]=rsxdraw::float_bits(u*(rectangle?4.f:1.f));data[at+33]=rsxdraw::float_bits(v*(rectangle?4.f:1.f));data[at+35]=rsxdraw::float_bits(1.f);}
  std::array<rsxdraw::Texture,16> textures{};auto& t=textures[0];t.valid=true;t.format=0x85;t.dimension=2;t.resident_image=source;
  t.unnormalized=rectangle;t.min=t.mag=1;t.mip_count=1;t.wrap_s=wrap;t.wrap_t=wrap_y;t.wrap_r=wrap;t.levels.push_back({4,4,16});
  const unsigned d=48,level=data.size();data.resize(level+4);data[d]=level;data[d+1]=1;data[d+2]=rectangle;data[d+3]=wrap;data[d+4]=wrap_y;data[d+5]=data[d+6]=1;data[d+27]=2;data[d+30]=wrap;
  const float border[]={.25f,.5f,.75f,1.f};for(unsigned c=0;c<4;++c){data[d+15+c]=c;data[d+19+c]=2;data[d+23+c]=rsxdraw::float_bits(border[c]);}
  data[level+1]=data[level+2]=4;data[level+3]=1;
  auto fs=rsxgraphics::fragment_source(fp,rsxdraw::Reg{guest.rsx},1,textures);assert(fs);
  auto raw=rsxgraphics::fragment_source_uncached(fp,rsxdraw::Reg{guest.rsx},1,textures);assert(raw && *raw==*fs);
  if(!first)first=fs;else if(dynamic)assert(first==fs && *first==*raw);
  session.refresh_vertex_constants();session.refresh_fragment_constants(fp);session.bind_draw_blocks(data,false);
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> colors{destination};std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> inputs{};inputs[0]=source;
  vk.graphics_draw(b,fixed,colors,{},vs,*fs,data,3,positions,0,0,0,1,inputs);
  const auto read=vk.graphics_transfer(b,destination,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(b);
  const bool border_hit=outside && (wrap==4 || wrap_y==4);
  const unsigned x=outside?negative[wrap]:2,y=outside?positive[wrap_y]:6;
  const unsigned expected[]={border_hit?64u:x*16,border_hit?128u:y*16,border_hit?191u:40u,255};
  const auto* actual=static_cast<const std::uint8_t*>(b.map)+read;
  for(unsigned p=0;p<4;++p)for(unsigned c=0;c<4;++c)if(std::abs(int(actual[p*4+c])-int(expected[c]))>1){
   std::fprintf(stderr,"resident case dynamic=%u outside=%u rect=%u wrap=%u,%u channel=%u expected=%u actual=%u\n",unsigned(dynamic),unsigned(outside),unsigned(rectangle),wrap,wrap_y,c,expected[c],unsigned(actual[p*4+c]));throw std::runtime_error("resident sampler color mismatch");}
  if(!cases){modules=vk.compile_stats.shader_modules;pipelines=vk.compile_stats.graphics_pipelines;}
  else if(dynamic)assert(vk.compile_stats.shader_modules==modules && vk.compile_stats.graphics_pipelines==pipelines);
  ++cases;vk.graphics_reset(b);
 }
 assert(cases==32);std::printf("PASS resident sampler dynamic=%u cases=%u modules=%llu pipelines=%llu\n",unsigned(dynamic),cases,(unsigned long long)vk.compile_stats.shader_modules,(unsigned long long)vk.compile_stats.graphics_pipelines);
}
int main(int argc,char** argv){try{assert(argc==2);resident_oracle(std::string(argv[1])=="1");}catch(const std::exception& e){std::fprintf(stderr,"RESIDENT SAMPLER STOP %s\n",e.what());return 1;}}
'''

class ResidentSamplerTests(unittest.TestCase):
    def test_wrapping_rectangle_and_scaled_texels(self):
        with tempfile.TemporaryDirectory(prefix='anyps3-resident-sampler-') as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')], capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr)
            for mode in ['0', '1']:
                env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='2', VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1', ANYPS3_RSX_RESIDENT_SAMPLER_DYNAMIC=mode)
                env.pop('ANYPS3_AUDIO_AUDIBLE', None)
                result = subprocess.run([str(root / 'test'), mode], capture_output=True, text=True, env=env, timeout=180)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertNotIn('Validation Error', output)
                self.assertNotIn('Validation Warning', output)
                print(result.stdout, flush=True)
