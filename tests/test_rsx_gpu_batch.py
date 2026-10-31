"""Fixed pixel/depth/stencil/query oracles for immutable uploads and pass reuse."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN=r'''
static void batch_oracle(unsigned samples){
 NativeVulkan vk;if(std::getenv("ANYPS3_REQUIRE_NVIDIA") && vk.device_name.find("NVIDIA")==std::string::npos)throw std::runtime_error("NVIDIA oracle selected another device");std::printf("BATCH_DEVICE %s\n",vk.device_name.c_str());auto b=vk.graphics_batch(4*1024*1024);
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.samples=samples;s.colors=1;
 s.color=VK_FORMAT_R8G8B8A8_UNORM;s.depth=VK_FORMAT_D24_UNORM_S8_UINT;s.scissor={{0,0},{8,8}};
 auto& blend=s.blend[0];blend.colorWriteMask=15;blend.blendEnable=true;
 blend.srcColorBlendFactor=blend.dstColorBlendFactor=blend.srcAlphaBlendFactor=blend.dstAlphaBlendFactor=VK_BLEND_FACTOR_ONE;
 blend.colorBlendOp=blend.alphaBlendOp=VK_BLEND_OP_ADD;
 s.z.sType=VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;s.z.depthTestEnable=s.z.depthWriteEnable=true;s.z.depthCompareOp=VK_COMPARE_OP_LESS;
 s.z.stencilTestEnable=true;s.z.front=s.z.back={VK_STENCIL_OP_KEEP,VK_STENCIL_OP_REPLACE,VK_STENCIL_OP_KEEP,VK_COMPARE_OP_ALWAYS,255,255,1};
 auto color=vk.graphics_image(*b,s.color,8,8,samples),depth=vk.graphics_image(*b,s.depth,8,8,samples,true);
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color};
 VkClearColorValue clear{};clear.float32[3]=1;vk.graphics_clear_image(*b,color,clear,0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 vk.graphics_clear_image(*b,depth,{},1,0,VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT);
 const std::string vs=R"GLSL(#version 450
 layout(std430,binding=0) readonly buffer Data{uint w[];};
 void main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,uintBitsToFloat(w[1]),1);}
 )GLSL";
 const std::string fs=R"GLSL(#version 450
 layout(std430,binding=0) readonly buffer Data{uint w[];};layout(std430,binding=37) readonly buffer State{uint st[];};
 layout(push_constant) uniform Push{vec4 extent;uint plane;};layout(location=0) out vec4 c;
 void main(){c=unpackUnorm4x8(st[w[0]]);if((plane&1u)!=0u)c.r=0;}
 )GLSL";
 std::array<unsigned,34> words{};words.fill(0xff0000ffu);
 std::array<NativeVulkan::GraphicsStateBlock,34> blocks{};for(unsigned i=0;i<34;++i)blocks[i]={1,&words[i],1};
 std::array<std::array<float,2>,4> positions{};for(auto& p:positions)p={.5f,.5f};
 std::vector<unsigned> data(16);std::vector<std::uint64_t> draw_offsets;
 auto draw=[&](unsigned z){vk.graphics_state_blocks(*b,blocks);data[0]=b->state_offsets[0];data[1]=z;draw_offsets.push_back(vk.graphics_draw(*b,s,images,depth,vs,fs,data,3,positions));};
 draw(0x3f000000); // red, z=.5, stencil=1
 words[0]=0xff00ff00u;blocks[0].version=2;
 s.z.front=s.z.back={VK_STENCIL_OP_KEEP,VK_STENCIL_OP_INCREMENT_AND_CLAMP,VK_STENCIL_OP_KEEP,VK_COMPARE_OP_EQUAL,255,255,1};
 draw(0x3e800000); // green, z=.25, stencil=2
 words[0]=0xffff0000u;blocks[0].version=3;
 s.z.front=s.z.back={VK_STENCIL_OP_KEEP,VK_STENCIL_OP_KEEP,VK_STENCIL_OP_INCREMENT_AND_CLAMP,VK_COMPARE_OP_EQUAL,255,255,2};
 draw(0x3f400000); // blue rejected by depth, stencil=3
 words.fill(0);data.assign(16,0); // Later host mutations cannot change recorded generations.
 // Plane 0 of all three draws shares one pass; planes >=1 are deferred as runs
 // and recorded (one pass per plane) when the transfer below ends that pass.
 b->sync();assert(b->render_passes==1 && b->render_pass_reuses==2 && b->plane_draws.size()==(samples==1?0u:3u));
 const auto rgba=vk.graphics_transfer(*b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);
 assert(b->render_passes==samples && b->plane_draws.empty());
 const auto z=vk.graphics_transfer(*b,depth,VK_IMAGE_ASPECT_DEPTH_BIT,4);
 const auto stencil=vk.graphics_transfer(*b,depth,VK_IMAGE_ASPECT_STENCIL_BIT,1);
 assert(!b->active_framebuffer);vk.graphics_finish(*b);assert(b->upload_commands==1);
 auto word=[&](std::uint64_t at){unsigned v;std::memcpy(&v,static_cast<const char*>(b->map)+at,4);return v;};
 assert(b->visibility_count==3*samples);
 for(unsigned plane=0;plane<samples;++plane){
  assert(b->visibility[plane]==64 && b->visibility[samples+plane]==64 && b->visibility[2*samples+plane]==0);
  for(unsigned i=0;i<64;++i){const auto pixel=plane*64+i;
   assert(word(rgba+pixel*4)==(plane%2?0xff00ff00u:0xff00ffffu));
   assert((word(z+pixel*4)&0xffffffu)==0x400000u);
   assert(static_cast<const unsigned char*>(b->map)[stencil+pixel]==3);
  }
 }
 for(auto at:draw_offsets)assert(word(at+28)==0);
 assert(b->recorded_order==(samples==1?std::vector<unsigned>{0,4,8}:samples==2?std::vector<unsigned>{0,1,4,5,8,9}:std::vector<unsigned>{0,1,2,3,4,5,6,7,8,9,10,11}));
 vk.graphics_reset(*b);assert(b->uploads.empty() && !b->active_framebuffer && b->status_offsets.empty());
 VkClearColorValue red{};red.float32[0]=red.float32[3]=1;vk.graphics_clear_image(*b,color,red,0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 const auto again=vk.graphics_transfer(*b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);
 for(unsigned i=0;i<64*samples;++i)assert(word(again+i*4)==0xff0000ffu);
 std::printf("PASS batch samples=%u immutable generations, compatible pipelines, depth/stencil, exact queries and plane order\n",samples);
}
// Adjacent immutable slices share one copy region (graphics_upload_region): three 64-word state blocks, a reserved slice and
// the draw blob. Independent expectations: the region list is the set of maximal runs of the returned offsets that touch
// (computed here from the offsets and sizes), and the pixel the shader assembles from the last word of block 0, the first of
// block 1 and the last of block 2 (R, G, B channels) plus a word of the reserved slice (alpha) is 0xffffffff on the device.
static void merged_uploads(){
 NativeVulkan vk;auto b=vk.graphics_batch(4*1024*1024);
 NativeVulkan::GraphicsState s{};s.width=s.height=4;s.samples=1;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{4,4}};s.blend[0].colorWriteMask=15;
 s.z.sType=VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
 auto color=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color};
 std::array<std::array<unsigned,64>,3> words{};for(unsigned k=0;k<3;++k)words[k].fill(0xffu<<(8*k));
 const std::array<NativeVulkan::GraphicsStateBlock,3> blocks{{{1,words[0].data(),64},{1,words[1].data(),64},{1,words[2].data(),64}}};
 assert(b->uploads.empty());vk.graphics_state_blocks(*b,blocks);
 const auto slice=vk.graphics_reserve_upload(*b,100);std::memset(static_cast<char*>(b->map)+slice,0,100);
 const unsigned alpha=0xff000000u;std::memcpy(static_cast<char*>(b->map)+slice+96,&alpha,4);
 std::vector<std::pair<std::uint64_t,std::uint64_t>> slices;for(unsigned k=0;k<3;++k)slices.push_back({b->state_offsets[k]*4,256});slices.push_back({slice,100});
 const std::string vs=R"GLSL(#version 450
 void main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}
 )GLSL";
 const std::string fs=R"GLSL(#version 450
 layout(std430,binding=0) readonly buffer Data{uint w[];};layout(std430,binding=37) readonly buffer State{uint st[];};
 layout(location=0) out vec4 c;
 void main(){c=unpackUnorm4x8(st[w[0]+63u]|st[w[1]]|st[w[2]+63u]|st[w[3]]);}
 )GLSL";
 std::vector<unsigned> data(16,0);data[0]=b->state_offsets[0];data[1]=b->state_offsets[1];data[2]=b->state_offsets[2];data[3]=unsigned((slice+96)/4);
 std::array<std::array<float,2>,4> positions{};for(auto& p:positions)p={.5f,.5f};
 const auto at=vk.graphics_draw(*b,s,images,{},vs,fs,data,3,positions);slices.push_back({at,data.size()*4});
 std::vector<VkBufferCopy> expect;for(const auto& [offset,size]:slices){
  if(!expect.empty() && expect.back().srcOffset+expect.back().size==offset)expect.back().size+=size;else expect.push_back({offset,offset,size});}
 assert(b->uploads.size()==expect.size() && expect.size()<slices.size());
 for(std::size_t i=0;i<expect.size();++i)assert(b->uploads[i].srcOffset==expect[i].srcOffset && b->uploads[i].dstOffset==expect[i].dstOffset && b->uploads[i].size==expect[i].size);
 const auto rgba=vk.graphics_transfer(*b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);
 for(unsigned i=0;i<16;++i){unsigned v;std::memcpy(&v,static_cast<const char*>(b->map)+rgba+4*i,4);assert(v==0xffffffffu);}
 assert(std::uint32_t(*reinterpret_cast<const std::uint32_t*>(static_cast<const char*>(b->map)+at+28))==0);
 std::printf("PASS adjacent upload slices share copy regions (%zu regions for %zu slices), device words exact\n",expect.size(),slices.size());
}
int main(){try{for(unsigned samples:{1u,2u,4u})batch_oracle(samples);merged_uploads();}catch(const std::exception& e){std::fprintf(stderr,"BATCH TEST STOP %s\n",e.what());return 1;}}
'''
class GpuBatchTests(unittest.TestCase):
 def test_batch_fixed_oracles(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);source=native_source().split('int main(int argc,char** argv)')[0]
   marker='for(unsigned layer=0;layer<passes;++layer){';self.assertEqual(source.count(marker),1)
   source=source.replace(marker,marker+'b.recorded_order.push_back(unsigned(b.draws)*4+layer);')
   source=source.replace('std::uint64_t capacity=0,used=0;','std::uint64_t capacity=0,used=0;std::vector<unsigned> recorded_order;')
   (root/'test.cpp').write_text(source+MAIN)
   build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
   self.assertEqual(build.returncode,0,build.stderr)
   env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1');env.pop('ANYPS3_AUDIO_AUDIBLE',None)
   if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
   result=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=180)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   if env.get('ANYPS3_REQUIRE_NVIDIA'):print(result.stdout,flush=True)
   self.assertNotIn('Validation Error',result.stdout+result.stderr);self.assertNotIn('Validation Warning',result.stdout+result.stderr)
   for samples in (1,2,4):self.assertIn(f'PASS batch samples={samples}',result.stdout)
   self.assertIn('PASS adjacent upload slices share copy regions',result.stdout)
