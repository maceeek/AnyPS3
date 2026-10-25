"""HIRES-C Vulkan recording/submission paths: exact shader-text identity across distinct source owners, one shared
immutable slice per native/visual draw pair."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static unsigned draw_color(NativeVulkan& vk,const std::shared_ptr<const std::string>& vs,const std::shared_ptr<const std::string>& fs){
 auto b=vk.graphics_batch(4*1024*1024);
 NativeVulkan::GraphicsState s{};s.width=s.height=4;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{4,4}};s.blend[0].colorWriteMask=15;
 auto image=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{image};
 std::array<std::array<float,2>,4> positions{{{.5f,.5f},{0,0},{0,0},{0,0}}};std::vector<unsigned> data(16);
 vk.graphics_draw(*b,s,images,{},*vs,*fs,data,3,positions,0,0,0,1,{},0,UINT64_MAX,{},vs,fs);
 const auto at=vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);
 unsigned first=0;std::memcpy(&first,static_cast<const char*>(b->map)+at,4);
 for(unsigned n=1;n<16;++n){unsigned v=0;std::memcpy(&v,static_cast<const char*>(b->map)+at+4*n,4);assert(v==first);}
 vk.graphics_reset(*b);return first;
}
static void canonical_owner_oracle(){
 // Small identity budget: owners are evicted and re-identified while their pipelines stay cached.
 setenv("ANYPS3_RSX_IDENTITY_LIMIT","3",1);NativeVulkan vk;unsetenv("ANYPS3_RSX_IDENTITY_LIMIT");
 const auto vs=std::make_shared<const std::string>("#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}\n");
 const std::string green="#version 450\nlayout(location=0) out vec4 c;void main(){c=vec4(0,1,0,1);}\n";
 // Same length, one byte apart: a different program (red).
 const std::string red=  "#version 450\nlayout(location=0) out vec4 c;void main(){c=vec4(1,0,0,1);}\n";
 assert(green.size()==red.size() && green!=red);
 const unsigned GREEN=0xff00ff00u,RED=0xff0000ffu;
 auto owner=[](const std::string& t){return std::make_shared<const std::string>(t);};
 auto g1=owner(green);assert(draw_color(vk,vs,g1)==GREEN);
 const auto pipelines=vk.compile_stats.graphics_pipelines;assert(pipelines==1);
 // A distinct owner object of the same text: one exact comparison when identified, then pointer equality per lookup.
 auto g2=owner(green);assert(draw_color(vk,vs,g2)==GREEN);assert(vk.compile_stats.graphics_pipelines==pipelines);
 const auto compares=vk.shader_text_compares;
 for(unsigned i=0;i<5;++i)assert(draw_color(vk,vs,g2)==GREEN);
 assert(vk.shader_text_compares==compares);assert(vk.compile_stats.graphics_pipelines==pipelines);
 // Different text never aliases, whatever the owner history.
 auto r1=owner(red);assert(draw_color(vk,vs,r1)==RED);assert(vk.compile_stats.graphics_pipelines==pipelines+1);
 // Churn owners beyond the identity budget (3): every draw still selects the program of its own exact text.
 std::vector<std::shared_ptr<const std::string>> keep;
 for(unsigned i=0;i<12;++i){auto o=owner(i%3==1?red:green);keep.push_back(o);assert(draw_color(vk,vs,o)==(i%3==1?RED:GREEN));
  if(i%4==3)keep.clear();}
 assert(draw_color(vk,vs,g1)==GREEN && draw_color(vk,vs,r1)==RED && draw_color(vk,vs,g2)==GREEN);
 assert(vk.compile_stats.graphics_pipelines==pipelines+1);
 std::printf("PASS canonical shader owners: green/red exact across %llu text compares, %llu pipelines\n",(unsigned long long)vk.shader_text_compares,(unsigned long long)vk.compile_stats.graphics_pipelines);
}
static void paired_slice_oracle(){
 setenv("ANYPS3_RENDER_SCALE","2",1);NativeVulkan vk;unsetenv("ANYPS3_RENDER_SCALE");
 NativeVulkan::GraphicsState s{};s.width=s.height=4;s.samples=1;s.colors=1;s.scale_visual=true;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{4,4}};s.blend[0].colorWriteMask=15;
 const std::string vs="#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}\n";
 // w[0]: failure bits of the visual (raster_scale 2) half, w[1]: of the native half; w[2]: green level of both.
 const std::string fs=R"GLSL(#version 450
 layout(std430,binding=0) buffer Data{uint w[];};
 layout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane;layout(offset=56) vec2 raster_scale_;};
 layout(location=0) out vec4 c;
 void main(){atomicOr(w[7],raster_scale_.x>1.5?w[0]:w[1]);c=vec4(0,float(w[2])/255.,0,1);}
 )GLSL";
 std::array<std::array<float,2>,4> positions{{{.5f,.5f},{0,0},{0,0},{0,0}}};
 auto run=[&](unsigned visual_fail,unsigned native_fail,std::string* error){
  auto b=vk.graphics_batch(4*1024*1024);auto image=vk.graphics_image(*b,s.color,4,4,1);assert(image->visual && image->visual->width==8);
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{image};std::vector<unsigned> data(16+48);data[0]=visual_fail;data[1]=native_fail;data[2]=0xa5;
  const auto used=b->used;const auto at=vk.graphics_draw(*b,s,images,{},vs,fs,data,3,positions);
  assert(b->used-used<data.size()*4+256 && b->paired_status_offsets.size()==1 && b->paired_status_offsets[0]==at+28 && b->visual_status_offsets.empty());
  const auto native=vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4),visual=vk.graphics_transfer(*b,image->visual,VK_IMAGE_ASPECT_COLOR_BIT,4);
  try{vk.graphics_finish(*b);}catch(const std::runtime_error& e){*error=e.what();return;}
  for(unsigned n=0;n<16;++n){unsigned v=0;std::memcpy(&v,static_cast<const char*>(b->map)+native+4*n,4);assert(v==0xff00a500u);}
  for(unsigned n=0;n<64;++n){unsigned v=0;std::memcpy(&v,static_cast<const char*>(b->map)+visual+4*n,4);assert(v==0xff00a500u);}
  unsigned word=0;std::memcpy(&word,static_cast<const char*>(b->map)+at+28,4);assert(word==0);
 };
 std::string error;run(0,0,&error);assert(error.empty());
 run(8,0,&error);assert(error.find("paired shader failed status=8")!=std::string::npos);error.clear();
 run(0,2,&error);assert(error.find("paired shader failed status=2")!=std::string::npos);
 std::puts("PASS paired native/visual slice: one slice, both images exact, visual-only and native-only failures stop at collection");
}
static void framebuffer_cache_oracle(){
 NativeVulkan vk;
 NativeVulkan::GraphicsState s{};s.width=s.height=4;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{4,4}};s.blend[0].colorWriteMask=15;
 const std::string vs="#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}\n";
 const std::string fs="#version 450\nlayout(std430,binding=0) buffer Data{uint w[];};layout(location=0) out vec4 c;void main(){c=vec4(float(w[0])/255.,0,0,1);}\n";
 std::array<std::array<float,2>,4> positions{{{.5f,.5f},{0,0},{0,0},{0,0}}};
 auto draw=[&](NativeVulkan::GraphicsBatch& b,const std::shared_ptr<NativeVulkan::GraphicsImage>& image,unsigned red){
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{image};std::vector<unsigned> data(16);data[0]=red;
  vk.graphics_draw(b,s,images,{},vs,fs,data,3,positions);};
 auto check=[&](NativeVulkan::GraphicsBatch& b,std::uint64_t at,unsigned red){for(unsigned n=0;n<16;++n){unsigned v=0;std::memcpy(&v,static_cast<const char*>(b.map)+at+4*n,4);assert(v==(0xff000000u|red));}};
 auto b1=vk.graphics_batch(4*1024*1024),b2=vk.graphics_batch(4*1024*1024);
 auto image=vk.graphics_image(*b1,s.color,4,4,1);
 const auto created=::RsxPerfCounters::values[::RsxPerfCounters::vk_framebuffers].load();
 draw(*b1,image,0x11);auto at=vk.graphics_transfer(*b1,image,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b1);check(*b1,at,0x11);
 // Another arena reuses the device-wide handle.
 draw(*b2,image,0x22);at=vk.graphics_transfer(*b2,image,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b2);check(*b2,at,0x22);
 assert(::RsxPerfCounters::values[::RsxPerfCounters::vk_framebuffers].load()==created+1 && b2->framebuffer_hits==1 && b2->framebuffer_misses==0);
 vk.graphics_reset(*b1);vk.graphics_reset(*b2);
 // The cache does not own the image: only this test holds it after retirement.
 assert(image.use_count()==1 && vk.graphics_framebuffer_cache_size()==1);
 // A dead image's entry never matches again, even when a new image's view handles repeat.
 image.reset();
 for(unsigned i=0;i<4;++i){auto fresh=vk.graphics_image(*b1,s.color,4,4,1);draw(*b1,fresh,0x30+i);at=vk.graphics_transfer(*b1,fresh,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b1);check(*b1,at,0x30+i);vk.graphics_reset(*b1);}
 assert(::RsxPerfCounters::values[::RsxPerfCounters::vk_framebuffers].load()==created+5);
 std::printf("PASS device-wide framebuffer cache: cross-arena hit, weak image ownership, dead-image entries never reused (%zu cached)\n",vk.graphics_framebuffer_cache_size());
}
int main(){try{canonical_owner_oracle();paired_slice_oracle();framebuffer_cache_oracle();}catch(const std::exception& e){std::fprintf(stderr,"SUBMIT TEST STOP %s\n",e.what());return 1;}}
'''


class RsxVkSubmitTests(unittest.TestCase):
    def test_native_oracles(self):
        if os.environ.get('ANYPS3_REQUIRE_VULKAN') != '1':
            self.skipTest('ANYPS3_REQUIRE_VULKAN=1 runs the native Vulkan oracle')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cpp = root/'test.cpp'; binary = root/'test'
            cpp.write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(cpp), *LIBRARIES, '-o', str(binary)],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr[-4000:])
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr[-4000:])
            self.assertIn('PASS canonical shader owners', run.stdout)
            self.assertIn('PASS paired native/visual slice', run.stdout)
            self.assertIn('PASS device-wide framebuffer cache', run.stdout)


if __name__ == '__main__':
    unittest.main()
