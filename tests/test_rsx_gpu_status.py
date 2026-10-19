"""Fixed GPU error words, sample counts, packed reuse and retirement ordering."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void status_oracle(){
 NativeVulkan vk;if(std::getenv("ANYPS3_REQUIRE_NVIDIA") && vk.device_name.find("NVIDIA")==std::string::npos)throw std::runtime_error("NVIDIA oracle selected another device");std::printf("STATUS_DEVICE %s\n",vk.device_name.c_str());auto b=vk.graphics_batch(4*1024*1024);
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.samples=2;s.colors=1;
 s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{0,0},{8,8}};s.blend[0].colorWriteMask=15;
 auto image=vk.graphics_image(*b,s.color,8,8,2);
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{image};
 std::array<std::array<float,2>,4> positions{{{.75f,.75f},{.25f,.25f},{0,0},{0,0}}};
 const std::string vs=R"GLSL(#version 450
 layout(std430,binding=0) buffer Data{uint w[];};
 void main(){atomicOr(w[7],w[0]);vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}
 )GLSL";
 const std::string fs=R"GLSL(#version 450
 layout(std430,binding=0) buffer Data{uint w[];};layout(location=0) out vec4 c;
 void main(){atomicOr(w[7],w[1]);c=vec4(0,1,0,1);}
 )GLSL";
 std::vector<unsigned> data(16);data[0]=16;data[1]=2;
 auto draw=[&](std::uint64_t reuse=UINT64_MAX){return vk.graphics_draw(*b,s,images,{},vs,fs,data,3,positions,0,0,0,1,{},0,reuse);};
 const auto first=draw();data[0]=0;data[1]=4;const auto second=draw();
 data[0]=0;data[1]=0;draw();
 draw(first); // Packed reuse of the same atomic word and copy destination.
 const auto pixels=vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4);
 const auto completion=vk.graphics_submit(*b);assert(completion && b->submitted);vk.graphics_collect(*b);
 auto word=[&](std::uint64_t at){unsigned v=0;std::memcpy(&v,static_cast<const char*>(b->map)+at,4);return v;};
 assert(word(first+28)==18);assert(word(second+28)==4);
 assert(b->visibility_count==8 && b->visibility.size()==8);
 for(auto n:b->visibility)assert(n==64);
 for(unsigned n=0;n<128;++n)assert(word(pixels+4*n)==0xff00ff00u);
 assert(b->download_bytes==128*4+4*4); // Four logical status requests, three unique copies.
 vk.graphics_reset(*b);assert(b->status_offsets.empty());
 data[0]=data[1]=0;const auto clean=draw();vk.graphics_finish(*b);assert(word(clean+28)==0);
 vk.graphics_reset(*b);
 VkClearColorValue clear{};clear.float32[0]=clear.float32[3]=1;
 vk.graphics_clear_image(*b,image,clear,0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 const auto cleared=vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4);
 vk.graphics_finish(*b);for(unsigned n=0;n<128;++n)assert(word(cleared+4*n)==0xff0000ffu);
 std::puts("PASS status VS|FS=18, FS=4, success=0, reused atomic word, exact RGBA, 8 sample queries, reset, clear-only batch");
}
static void status_commit_oracle(){
 PpuState state;setup_draw(state,8,8,32,false);state.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*state.rsx.renderer,state);
 unsigned poisoned=2;const auto at=session.vk.graphics_upload(*session.batch,&poisoned,4);
 std::vector<rsxgraphics::Session::Pending> work{{unsigned(at),0,1,true}};
 session.batch->visibility={99};auto results=std::make_shared<std::vector<std::uint64_t>>(1,777);
 std::vector<rsxgraphics::Session::Label> labels;
 bool failed=false;try{session.retire_work(*session.batch,work,results,labels);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("execution budget exhausted")!=std::string::npos;}
 assert(failed);assert(state.rsx.draw.pixels==0 && state.rsx.report_values[0]==0 && results->at(0)==777);
 std::puts("PASS failed shader status rejects retirement before reports/guest commit");
}
int main(){try{status_oracle();status_commit_oracle();}catch(const std::exception& e){std::fprintf(stderr,"STATUS TEST STOP %s\n",e.what());return 1;}}
'''

class GpuStatusTests(unittest.TestCase):
    def test_deferred_status_fixed_oracles(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0]+MAIN)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_AUDIO_AUDIBLE',None)
            if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            if env.get('ANYPS3_REQUIRE_NVIDIA'):print(result.stdout,flush=True)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)
            self.assertIn('PASS status VS|FS=18',result.stdout)
            self.assertIn('PASS failed shader status',result.stdout)
