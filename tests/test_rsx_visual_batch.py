"""Deferred visual draws preserve an independent pixel model and native queries."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_rsx_recorder import MAIN as RECORDER_MAIN
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void paired_sequence(const char* recorder,unsigned samples,bool batch){
 setenv("ANYPS3_RENDER_SCALE","2",1);setenv("ANYPS3_RENDER_SCALE_BATCH",batch?"1":"0",1);
 Gpu g(recorder);g.s.samples=samples;g.s.scale_visual=true;
 for(auto& image:g.image)image=g.vk.graphics_image(*g.b,g.s.color,16,16,samples);
 Model expected{};for(unsigned t=0;t<2;++t)g.clear(t,0);
 auto draw=[&](unsigned target,unsigned x,unsigned y,unsigned w,unsigned h,unsigned color,bool visible=true){
  g.draw(target,x,y,x+w,y+h,color);
  if(visible)for(unsigned yy=y;yy<y+h;++yy)for(unsigned xx=x;xx<x+w;++xx)expected[target][yy*16+xx]=color;
 };
 auto check=[&]{
  std::uint64_t offsets[2][2];
  for(unsigned t=0;t<2;++t){offsets[t][0]=g.vk.graphics_transfer(*g.b,g.image[t],VK_IMAGE_ASPECT_COLOR_BIT,4);
   offsets[t][1]=g.vk.graphics_transfer(*g.b,g.image[t]->visual,VK_IMAGE_ASPECT_COLOR_BIT,4);}
  g.vk.graphics_finish(*g.b);assert(g.b->visual_jobs.empty());
  for(unsigned t=0;t<2;++t)for(unsigned scale=1;scale<=2;++scale){
   const auto* pixels=reinterpret_cast<const std::uint32_t*>(static_cast<const char*>(g.b->map)+offsets[t][scale-1]);
   for(unsigned plane=0;plane<samples;++plane)for(unsigned y=0;y<16*scale;++y)for(unsigned x=0;x<16*scale;++x)
    if(pixels[(plane*16*scale+y)*16*scale+x]!=expected[t][(y/scale)*16+x/scale])
     throw std::runtime_error("paired draw order or immutable state mismatch scale="+std::to_string(scale)+" plane="+std::to_string(plane));
  }
 };
 // Exceeds both the 128 visual-job cap and the 256 recorder ring entries.
 std::uint64_t expected_samples=0;const auto queued_before=g.vk.graphics_visual_queued_total,drains_before=g.vk.graphics_visual_drains_total;
 const auto lookups_before=g.vk.compile_stats.graphics_hits+g.vk.compile_stats.graphics_misses;
 for(unsigned i=0;i<200;++i){unsigned x=i%13,y=(i*7)%13;draw(0,x,y,4,4,0xff000000u+i*271u);expected_samples+=16*samples;}
 // One pipeline lookup per paired draw: the visual half reuses the native pipeline.
 assert(g.vk.compile_stats.graphics_hits+g.vk.compile_stats.graphics_misses-lookups_before==200 && !g.vk.graphics_paired_pipeline);
 check();std::uint64_t actual_samples=0;for(auto n:g.b->visibility)actual_samples+=n;
 assert(actual_samples==expected_samples);
 const auto passes=g.b->render_passes;
 // Collection folds the batch's queue counters into run totals (the PERF
 // RENDER_SCALE report); they must survive the batch being collected.
 if(batch){assert(passes<=4*samples);assert(g.b->visual_run_max==0 && g.b->visual_drains==0);
  assert(g.vk.graphics_visual_run_max==128 && g.vk.graphics_visual_drains_total-drains_before==2 && g.vk.graphics_visual_queued_total-queued_before==200);}
 else assert(passes==400*samples && g.vk.graphics_visual_queued_total==queued_before);
 g.vk.graphics_reset(*g.b);
 // Copy, clear, upload, target switches and predicate changes are dependencies.
 draw(0,0,0,16,16,0xff0000ffu);
 g.vk.graphics_copy(*g.b,g.image[0],g.image[1]);expected[1]=expected[0];
 g.clear(0,0xff112233u);expected[0].fill(0xff112233u);
 draw(1,2,3,7,8,0xff00ff00u);
 std::array<std::uint32_t,256> upload;upload.fill(0xffabcdefu);
 std::vector<std::uint32_t> planes(samples*256,0xffabcdefu);
 g.vk.graphics_transfer(*g.b,g.image[0],VK_IMAGE_ASPECT_COLOR_BIT,4,planes.data());expected[0]=upload;
 draw(0,1,2,3,4,0xff663399u);
 if(!g.vk.graphics_conditional_rendering())throw std::runtime_error("conditional extension required for paired order regression");
 auto predicate=g.vk.graphics_predicate_buffer();
 for(unsigned enabled:{0u,1u}){
  g.vk.graphics_predicate(*g.b,predicate,0,{},enabled,1,true,true);
  {NativeVulkan::ConditionalScope scope(g.vk,*g.b,predicate,0);draw(1,4,5,6,7,0xff998877u,enabled);}
  draw(0,10,10,2,2,0xff987654u);
 }
 check();g.vk.graphics_reset(*g.b);
 std::printf("PASS visual batching=%u recorder=%s samples=%u passes=%llu native queries and independent pixels\n",unsigned(batch),recorder,samples,(unsigned long long)passes);
}
static void conditional_failure(bool unwinding){
 setenv("ANYPS3_RENDER_SCALE","2",1);setenv("ANYPS3_RENDER_SCALE_BATCH","1",1);
 Gpu g("0");g.s.scale_visual=true;auto predicate=g.vk.graphics_predicate_buffer();
 g.vk.graphics_predicate(*g.b,predicate,0,{},1,1,true,true);
 bool caught=false;
 try{
  NativeVulkan::ConditionalScope scope(g.vk,*g.b,predicate,0);
  g.draw(0,0,0,16,16,0xff00ff00u);assert(g.b->visual_jobs.size()==1);
  // Malformed deferred metadata exercises replay failure without a GPU fault.
  g.b->visual_jobs[0].s.raster_height=0;
  if(unwinding)throw std::runtime_error("original failure sentinel");
 }catch(const std::runtime_error& e){caught=true;if(unwinding)assert(std::string(e.what())=="original failure sentinel");}
 assert(caught && g.b->failed && !g.b->inline_draws);
 std::printf("PASS conditional replay failure unwinding=%u propagates without terminate\n",unsigned(unwinding));
}
int main(){try{for(const char* recorder:{"0","1"})for(unsigned samples:{1u,2u,4u})for(bool batch:{false,true})paired_sequence(recorder,samples,batch);conditional_failure(false);conditional_failure(true);}
catch(const std::exception& e){std::fprintf(stderr,"FAIL visual batching %s\n",e.what());return 1;}}
'''


class VisualBatchTests(unittest.TestCase):
 def test_order_pixels_queries_and_pass_count(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   source=native_source().split('int main(int argc,char** argv)')[0]
   source+=RECORDER_MAIN.split('int main()')[0]+MAIN
   (root/'test.cpp').write_text(source)
   command=['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')]
   result=subprocess.run(command,capture_output=True,text=True,timeout=180)
   self.assertEqual(result.returncode,0,result.stderr)
   env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1',ANYPS3_RSX_LAYERED='0',ANYPS3_RSX_MULTIVIEW='0')
   env.pop('ANYPS3_AUDIO_AUDIBLE',None)
   if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
    env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
   result=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=180)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertNotIn('Validation Error',result.stdout+result.stderr)
   self.assertNotIn('Validation Warning',result.stdout+result.stderr)
   self.assertEqual(result.stdout.count('PASS visual batching='),12,result.stdout)
   print(result.stdout,flush=True)
