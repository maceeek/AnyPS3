"""Asynchronous draw recorder (ANYPS3_RSX_RECORDER=1): command order, back-pressure, failure propagation, shutdown.

The model-based oracle applies random draws (scissor rectangle, color), clears and image copies to a CPU model of two 16x16 RGBA8 images; the
GPU result must equal the model exactly in both modes (inline recording and recorder thread), so any draw that is recorded out of order with
respect to a clear/copy (or lost, duplicated, recorded into the wrong batch) shows up as a pixel difference. Further cases: more draws than ring
slots (producer back-pressure), a recorder failure (the next enqueue/drain/finish rethrows it, nothing hangs), teardown with queued draws. The
validation layer (including its thread-safety checks) must stay silent.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
static const char* VS=R"GLSL(#version 450
layout(std430,binding=0) readonly buffer Data{uint w[];};
void main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,uintBitsToFloat(w[1]),1);}
)GLSL";
static const char* FS=R"GLSL(#version 450
layout(std430,binding=0) readonly buffer Data{uint w[];};layout(std430,binding=37) readonly buffer State{uint st[];};
layout(push_constant) uniform Push{vec4 extent;uint plane;};layout(location=0) out vec4 c;
void main(){c=unpackUnorm4x8(st[w[0]]);}
)GLSL";
// NativeVulkan is neither copyable nor movable: construct it with the environment already set.
struct Env {Env(const char* v){setenv("ANYPS3_RSX_RECORDER",v,1);}};
struct Gpu : Env {
 NativeVulkan vk;std::unique_ptr<NativeVulkan::GraphicsBatch> b;NativeVulkan::GraphicsState s{};
 std::shared_ptr<NativeVulkan::GraphicsImage> image[2];std::vector<unsigned> data=std::vector<unsigned>(16,0);unsigned word=0,version=0;
 std::array<std::array<float,2>,4> positions{};
 explicit Gpu(const char* recorder,unsigned capacity=8*1024*1024):Env(recorder),vk(){
  b=vk.graphics_batch(capacity);
  s.width=s.height=16;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{16,16}};
  s.blend[0].colorWriteMask=15;s.blend[0].blendEnable=false;
  s.z.sType=VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
  for(auto& p:positions)p={.5f,.5f};
  for(auto& i:image)i=vk.graphics_image(*b,s.color,16,16,1);
  data[1]=0x3f000000;
 }
 void draw(unsigned target,unsigned x0,unsigned y0,unsigned x1,unsigned y1,std::uint32_t color){
  s.scissor={{int(x0),int(y0)},{x1-x0,y1-y0}};word=color;
  std::array<NativeVulkan::GraphicsStateBlock,1> blocks{{{++version,&word,1}}};vk.graphics_state_blocks(*b,blocks);data[0]=unsigned(b->state_offsets[0]);
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{image[target]};
  (void)vk.graphics_draw(*b,s,images,{},VS,FS,data,3,positions);
 }
 void clear(unsigned target,std::uint32_t color){
  VkClearColorValue v{};for(unsigned c=0;c<4;++c)v.float32[c]=float((color>>(8*c))&255)/255.f;
  vk.graphics_clear_image(*b,image[target],v,0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 }
};
using Model=std::array<std::array<std::uint32_t,256>,2>;
static void read_back(Gpu& g,Model& got){
 const auto a=g.vk.graphics_transfer(*g.b,g.image[0],VK_IMAGE_ASPECT_COLOR_BIT,4),c=g.vk.graphics_transfer(*g.b,g.image[1],VK_IMAGE_ASPECT_COLOR_BIT,4);
 g.vk.graphics_finish(*g.b);
 std::memcpy(got[0].data(),static_cast<const char*>(g.b->map)+a,1024);std::memcpy(got[1].data(),static_cast<const char*>(g.b->map)+c,1024);
 g.vk.graphics_reset(*g.b);
}
static void model_sequence(const char* mode,unsigned seed,unsigned rounds,unsigned ops){
 Gpu g(mode);Model model{};std::uint32_t r=seed;unsigned draws=0;
 {Model got{};g.clear(0,0);g.clear(1,0);read_back(g,got);assert(got==model);}
 for(unsigned round=0;round<rounds;++round){
  for(unsigned n=0;n<ops;++n){
   const unsigned kind=lcg(r)%100,target=lcg(r)%2;
   if(kind<65){unsigned x0=lcg(r)%16,x1=lcg(r)%16,y0=lcg(r)%16,y1=lcg(r)%16;if(x0>x1)std::swap(x0,x1);if(y0>y1)std::swap(y0,y1);++x1;++y1;
    const std::uint32_t color=lcg(r)|(lcg(r)<<16);g.draw(target,x0,y0,x1,y1,color);++draws;
    for(unsigned y=y0;y<y1;++y)for(unsigned x=x0;x<x1;++x)model[target][y*16+x]=color;}
   else if(kind<80){const std::uint32_t color=lcg(r)|(lcg(r)<<16);g.clear(target,color);model[target].fill(color);}
   else{g.vk.graphics_copy(*g.b,g.image[target],g.image[1-target]);model[1-target]=model[target];}
  }
  Model got{};read_back(g,got);
  for(unsigned t=0;t<2;++t)for(unsigned i=0;i<256;++i)if(got[t][i]!=model[t][i])
   throw std::runtime_error(std::string(mode)+": round "+std::to_string(round)+" image "+std::to_string(t)+" pixel "+std::to_string(i)+" got "+std::to_string(got[t][i])+" expected "+std::to_string(model[t][i]));
 }
 std::printf("PASS recorder=%s model sequence: %u rounds x %u ops (%u draws) equal the CPU model\n",mode,rounds,ops,draws);
}
static void back_pressure(){
 Gpu g("1");   // 900 tiny draws in one batch: more than the 256 ring slots
 for(unsigned i=0;i<900;++i)g.draw(0,0,0,16,16,0xff000000u|i);
 g.b->sync();assert(g.b->draws==900 && g.b->visibility_count==900 && g.b->pending_jobs.load()==0);
 Model got{};read_back(g,got);for(unsigned i=0;i<256;++i)assert(got[0][i]==(0xff000000u|899u));
 std::puts("PASS recorder back-pressure: 900 draws through 256 slots, last draw wins, counters exact after sync");
}
// Batch A is submitted and then collected/reset by the frontend while the recorder is busy with batch B's queued draws: separate command
// pools keep the two apart (the validation layer reports concurrent use of one pool).
static void two_batches(){
 Gpu g("1");auto second=g.vk.graphics_batch(8*1024*1024);
 g.draw(0,0,0,16,16,0xff0000ffu);const auto first_completion=g.vk.graphics_submit(*g.b);(void)first_completion;
 auto first=std::move(g.b);g.b=std::move(second);
 for(unsigned round=0;round<4;++round){
  for(unsigned i=0;i<300;++i)g.draw(0,0,0,16,16,0xff000000u|(round*300+i));
  g.vk.graphics_collect(*first,true);g.vk.graphics_reset(*first);   // frontend work on the idle batch while B's draws are being recorded
  g.vk.graphics_move_stats(*first,*g.b);
  for(unsigned i=0;i<300;++i)g.draw(1,0,0,16,16,0xff000000u|(round*300+i));
  std::swap(first,g.b);   // alternate the roles: the batch with the queued draws is submitted, the other one becomes the recording batch
  g.vk.graphics_submit(*first);
 }
 g.vk.graphics_collect(*first,true);g.vk.graphics_reset(*first);
 g.draw(0,0,0,16,16,0xff00ffffu);Model got{};read_back(g,got);
 for(unsigned i=0;i<256;++i){assert(got[0][i]==0xff00ffffu && got[1][i]==0xff000000u+1199);}
 std::puts("PASS recorder with two batches: a submitted batch is collected and reset while the other one has queued draws");
}
static void failure(){
 Gpu g("1");g.vk.graphics_test_recorder_fault=5;
 bool stopped=false;std::string what;
 try{for(unsigned i=0;i<64;++i)g.draw(0,0,0,16,16,0xff0000ffu);Model got{};read_back(g,got);}
 catch(const std::runtime_error& e){stopped=true;what=e.what();}
 if(!stopped || what.find("recorder test fault")==std::string::npos)throw std::runtime_error("recorder failure was not reported: '"+what+"'");
 stopped=false;try{g.b->sync();}catch(const std::runtime_error&){stopped=true;}   // poisoned for good
 if(!stopped)throw std::runtime_error("recorder failure was forgotten");
 std::puts("PASS recorder failure: rethrown at the next enqueue/finish and by every later sync, no hang");
}
static void teardown(){
 {Gpu g("1");for(unsigned i=0;i<200;++i)g.draw(0,0,0,16,16,0xff00ff00u);}   // destructors with queued draws
 {Gpu g("1");g.draw(0,0,0,16,16,1);g.b.reset();}                              // batch destroyed with a queued draw
 std::puts("PASS recorder teardown with queued draws");
}
// RECORDER_BENCH=1: main-thread cost of a draw (frontend only) with and without the recorder; not an assertion.
static void bench(){
 for(const char* mode:{"0","1","0","1"}){
  Gpu g(mode);Model got{};double total=0;unsigned draws=0;std::uint32_t r=5;
  for(unsigned round=0;round<30;++round){
   const auto start=std::chrono::steady_clock::now();
   for(unsigned i=0;i<800;++i){const unsigned x0=lcg(r)%8,y0=lcg(r)%8;g.draw(i&1,x0,y0,x0+8,y0+8,0xff000000u|i);++draws;}
   total+=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
   read_back(g,got);
  }
  std::printf("BENCH recorder=%s main-thread %.3f us/draw over %u draws\n",mode,total*1e6/draws,draws);
 }
}
int main(){try{
 if(std::getenv("RECORDER_BENCH")){bench();return 0;}
 for(const char* mode:{"0","1"})model_sequence(mode,12345,4,400);
 model_sequence("1",777,6,700);
 back_pressure();two_batches();failure();teardown();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN


class RsxRecorderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_recorder_oracles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                       VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], capture_output=True, text=True, env=env, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('Validation Error', result.stdout + result.stderr)
            self.assertNotIn('Validation Warning', result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 7, result.stdout)


if __name__ == '__main__':
    unittest.main()
