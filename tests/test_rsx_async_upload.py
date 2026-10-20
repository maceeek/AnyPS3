"""Batch preludes on the dedicated DMA queue: ordering, concurrent batches and reuse (independent pixel oracle)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN=r'''
// Pattern written by the CPU into an immutable staging slice; the fragment shader of every pixel reads a word 32 KiB away from the
// previous pixel's, so a prelude that is late, partial or lost is visible in at least one pixel. Expected values are computed here.
static unsigned pattern(unsigned tag,unsigned index){return (index*0x9E3779B1u)^(tag*0x85EBCA6Bu)^0x01020304u;}
static const char* const ORACLE_VS=R"GLSL(#version 450
 layout(std430,binding=0) readonly buffer Data{uint w[];};
 void main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}
 )GLSL";
static const char* const ORACLE_FS=R"GLSL(#version 450
 layout(std430,binding=0) readonly buffer Data{uint w[];};layout(std430,binding=37) readonly buffer State{uint st[];};
 layout(location=0) out vec4 c;
 void main(){uvec2 p=uvec2(gl_FragCoord.xy);c=unpackUnorm4x8(st[w[0]+(p.y*8u+p.x)*8192u]);}
 )GLSL";
static void async_oracle(bool require_async){
 NativeVulkan vk;
 std::printf("ASYNC_DEVICE %s async=%d\n",vk.device_name.c_str(),int(vk.graphics_async_upload()));
 if(require_async && !vk.graphics_async_upload())throw std::runtime_error("device has no dedicated transfer queue; async prelude not exercised");
 const unsigned stride=8192,words=64*stride; // 2 MiB slice per batch
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{8,8}};s.blend[0].colorWriteMask=15;
 const std::string vs=ORACLE_VS,fs=ORACLE_FS;
 std::array<std::array<float,2>,4> positions{};for(auto& p:positions)p={.5f,.5f};
 std::unique_ptr<NativeVulkan::GraphicsBatch> batch[2]={vk.graphics_batch(16*1024*1024),vk.graphics_batch(16*1024*1024)};
 std::shared_ptr<NativeVulkan::GraphicsImage> color[2];
 for(unsigned i=0;i<2;++i)color[i]=vk.graphics_image(*batch[i],s.color,8,8,1);
 std::uint64_t readback[2]{};
 auto record=[&](unsigned i,unsigned tag){
  auto& b=*batch[i];
  const auto at=vk.graphics_reserve_upload(b,std::uint64_t(words)*4);
  auto* out=reinterpret_cast<unsigned*>(static_cast<char*>(b.map)+at);for(unsigned k=0;k<words;++k)out[k]=pattern(tag,k);
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color[i]};
  std::vector<unsigned> data(16,0);data[0]=unsigned(at/4);
  vk.graphics_draw(b,s,images,{},vs,fs,data,3,positions);
  readback[i]=vk.graphics_transfer(b,color[i],VK_IMAGE_ASPECT_COLOR_BIT,4);
 };
 auto verify=[&](unsigned i,unsigned tag){
  auto& b=*batch[i];
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){unsigned v;std::memcpy(&v,static_cast<const char*>(b.map)+readback[i]+(y*8+x)*4,4);
   if(v!=pattern(tag,(y*8+x)*stride))throw std::runtime_error("prelude data mismatch batch="+std::to_string(i)+" tag="+std::to_string(tag)+" pixel="+std::to_string(y*8+x));}
 };
 const auto before=vk.graphics_upload_submissions();
 for(unsigned round=0;round<6;++round){
  // Two batches in flight: the second one's prelude is submitted while the first still executes.
  record(0,round*2);vk.graphics_submit(*batch[0]);
  record(1,round*2+1);vk.graphics_submit(*batch[1]);
  vk.graphics_collect(*batch[0]);verify(0,round*2);
  vk.graphics_collect(*batch[1]);verify(1,round*2+1);
  vk.graphics_reset(*batch[0]);vk.graphics_reset(*batch[1]);
 }
 if(vk.graphics_async_upload() && vk.graphics_upload_submissions()-before!=12)throw std::runtime_error("expected one DMA-queue prelude per batch");
 if(!vk.graphics_async_upload() && vk.graphics_upload_submissions()!=before)throw std::runtime_error("no DMA queue yet upload submissions counted");
 std::printf("PASS async upload rounds=6 batches=2 words=%u async=%d\n",words,int(vk.graphics_async_upload()));
}

// Failure injection between the DMA-queue prelude submission and the graphics submission (review finding 1): the batch is destroyed
// while its prelude is still pending, then the device-local memory is recycled by the next batch. Without the wait in ~GraphicsBatch
// the validation layer reports VUID-vkDestroyBuffer-buffer-00922 / vkFreeCommandBuffers-00047; with it the output stays clean and the
// recycled memory carries correct data.
static void fault_oracle(){
 NativeVulkan vk;
 std::printf("FAULT_DEVICE %s async=%d\n",vk.device_name.c_str(),int(vk.graphics_async_upload()));
 if(!vk.graphics_async_upload()){
  if(std::getenv("ANYPS3_REQUIRE_NVIDIA"))throw std::runtime_error("device has no dedicated transfer queue; failure injection not exercised");
  std::printf("PASS fault injection skipped (no dedicated transfer queue) async=0\n");return;}
 const unsigned stride=8192,words=64*stride;
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{8,8}};s.blend[0].colorWriteMask=15;
 std::array<std::array<float,2>,4> positions{};for(auto& p:positions)p={.5f,.5f};
 auto record=[&](NativeVulkan::GraphicsBatch& b,unsigned tag,std::uint64_t pad_words){
  auto color=vk.graphics_image(b,s.color,8,8,1);
  const auto pad=vk.graphics_reserve_upload(b,pad_words*4); // large dummy slice: the DMA copy stays pending for a while
  std::memset(static_cast<char*>(b.map)+pad,0x5a,pad_words*4);
  const auto at=vk.graphics_reserve_upload(b,std::uint64_t(words)*4);
  auto* out=reinterpret_cast<unsigned*>(static_cast<char*>(b.map)+at);for(unsigned k=0;k<words;++k)out[k]=pattern(tag,k);
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color};
  std::vector<unsigned> data(16,0);data[0]=unsigned(at/4);
  vk.graphics_draw(b,s,images,{},ORACLE_VS,ORACLE_FS,data,3,positions);
  return vk.graphics_transfer(b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);
 };
 for(unsigned round=0;round<4;++round){
  auto batch=vk.graphics_batch(48*1024*1024);
  record(*batch,100+round,8*1024*1024);
  const auto before=vk.graphics_upload_submissions();
  vk.graphics_test_submit_fault=true;
  bool failed=false;
  try{vk.graphics_submit(*batch);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("injected")!=std::string::npos;}
  if(!failed)throw std::runtime_error("injected graphics submit failure did not surface");
  if(vk.graphics_upload_submissions()!=before+1)throw std::runtime_error("the DMA prelude was not submitted before the injected failure");
  batch.reset(); // prelude possibly still pending here
 }
 // The recycler hands the (now released) device-local memory to a fresh batch: a normal round must still be exact.
 auto batch=vk.graphics_batch(48*1024*1024);
 const auto readback=record(*batch,7,8*1024*1024);
 vk.graphics_submit(*batch);vk.graphics_collect(*batch);
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){unsigned v;std::memcpy(&v,static_cast<const char*>(batch->map)+readback+(y*8+x)*4,4);
  if(v!=pattern(7,(y*8+x)*stride))throw std::runtime_error("recycled batch data mismatch pixel="+std::to_string(y*8+x));}
 vk.graphics_reset(*batch);
 std::printf("PASS fault injection rounds=4 async=1\n");
}
// Failure of the second ANYPS3_RSX_GPU_LOOP graphics submission while the first is still pending (diagnostic path, review-gpufast2-1):
// the destructor must not free pending command buffers/buffers/recycled memory. Needs ANYPS3_RSX_GPU_LOOP>=2 in the environment.
static void loop_fault_oracle(){
 NativeVulkan vk;
 std::printf("LOOP_FAULT_DEVICE %s async=%d\n",vk.device_name.c_str(),int(vk.graphics_async_upload()));
 const unsigned stride=8192,words=64*stride;
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{8,8}};s.blend[0].colorWriteMask=15;
 std::array<std::array<float,2>,4> positions{};for(auto& p:positions)p={.5f,.5f};
 for(unsigned round=0;round<3;++round){
  auto batch=vk.graphics_batch(48*1024*1024);auto& b=*batch;
  auto color=vk.graphics_image(b,s.color,8,8,1);
  const auto at=vk.graphics_reserve_upload(b,std::uint64_t(words)*4);
  auto* out=reinterpret_cast<unsigned*>(static_cast<char*>(b.map)+at);for(unsigned k=0;k<words;++k)out[k]=pattern(200+round,k);
  std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color};
  std::vector<unsigned> data(16,0);data[0]=unsigned(at/4);
  vk.graphics_draw(b,s,images,{},ORACLE_VS,ORACLE_FS,data,3,positions);
  vk.graphics_transfer(b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);
  vk.graphics_test_loop_fault=2;bool failed=false;
  try{vk.graphics_submit(b);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("injected graphics loop")!=std::string::npos;}
  if(!failed)throw std::runtime_error("injected loop submit failure did not surface");
  batch.reset();
 }
 std::printf("PASS loop fault injection rounds=3 async=%d\n",int(vk.graphics_async_upload()));
}
int main(int argc,char** argv){try{if(argc>1 && !std::strcmp(argv[1],"fault")){fault_oracle();return 0;}if(argc>1 && !std::strcmp(argv[1],"loopfault")){loop_fault_oracle();return 0;}const char* a=std::getenv("ANYPS3_RSX_ASYNC_UPLOAD");async_oracle(std::getenv("ANYPS3_REQUIRE_NVIDIA")!=nullptr && !(a && !std::strcmp(a,"0")));}catch(const std::exception& e){std::fprintf(stderr,"ASYNC UPLOAD TEST STOP %s\n",e.what());return 1;}}
'''
class AsyncUploadTests(unittest.TestCase):
 def run_variant(self,async_upload,args=(),passed='PASS async upload',extra_env=None):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);source=native_source().split('int main(int argc,char** argv)')[0]
   (root/'test.cpp').write_text(source+MAIN)
   build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=300)
   self.assertEqual(build.returncode,0,build.stderr)
   env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1');env.pop('ANYPS3_AUDIO_AUDIBLE',None)
   if async_upload is not None:env['ANYPS3_RSX_ASYNC_UPLOAD']=async_upload
   if extra_env:env.update(extra_env)
   if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
   elif env.get('ANYPS3_REQUIRE_NVIDIA'):env.update(ANYPS3_VK_DEVICE='gpu')
   result=subprocess.run([str(root/'test'),*args],capture_output=True,text=True,env=env,timeout=300)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertNotIn('Validation Error',result.stdout+result.stderr);self.assertNotIn('Validation Warning',result.stdout+result.stderr)
   self.assertIn(passed,result.stdout)
   return result.stdout
 def test_default_queue_layout(self):
  out=self.run_variant(None)
  if os.environ.get('ANYPS3_REQUIRE_NVIDIA'):self.assertIn('async=1',out)  # the RTX has a transfer-only family
 def test_submit_failure_after_prelude(self):
  # Review finding 1: a graphics submit that fails after the DMA prelude was submitted must not free/recycle pending resources.
  out=self.run_variant(None,('fault',),'PASS fault injection')
  if os.environ.get('ANYPS3_REQUIRE_NVIDIA'):self.assertIn('async=1',out)
 def test_gpu_loop_submit_failure(self):
  # review-gpufast2-1: a failing diagnostic loop submission leaves earlier iterations pending; both queues are drained before release.
  out=self.run_variant(None,('loopfault',),'PASS loop fault injection',{'ANYPS3_RSX_GPU_LOOP':'4'})
  if os.environ.get('ANYPS3_REQUIRE_NVIDIA'):self.assertIn('async=1',out)
 def test_single_queue_fallback(self):
  out=self.run_variant('0');self.assertIn('async=0',out)
if __name__=='__main__':unittest.main()
