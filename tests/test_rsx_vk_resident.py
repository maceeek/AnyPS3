"""Resident GPU batches: native differential and independent storage oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_vk import native_source as original_source, LIBRARIES

MAIN = r'''
static void resident_setup(PpuState& s,unsigned format,unsigned aa,unsigned targets,bool tiled=false,bool swizzled=false){
 setup_draw(s,128,128,2048,tiled);auto& R=s.rsx.registers;
 R[0x208]=(R[0x208]&~31u)|format|(aa<<12);R[0x220]=targets==1?1:targets==2?0x13:targets==3?0x17:0x1f;
 if(swizzled)R[0x208]=(7u<<24)|(7u<<16)|(R[0x208]&~0xf00u)|0x200;
 for(unsigned context:{0x18cu,0x1b4u,0x1b8u})R[context]=0xfeed0000;
 R[0x218]=0x200000;R[0x288]=0x300000;R[0x28c]=0x400000;
 R[0x21c]=R[0x280]=R[0x284]=2048;R[0x370]=0xffff;
 R[0x17cc]=1;
 // Each MRT output has a distinct constant; fp16 includes signed/HDR values.
 const unsigned outs[4]={0,2,3,4};
 for(unsigned i=0;i<targets;++i){unsigned at=i*8;
  fpword(s,at,(1u<<24)|(outs[i]<<1)|(15u<<9)|(i+1==targets?1u:0u));
  fpword(s,at+1,2u|(0xe4u<<9)|(7u<<18));fpword(s,at+2,0);fpword(s,at+3,0);
  const float v[4]={format>=11&&format<=13?-2.f:1.f,float(i+1)*.5f,.25f,.5f};
  for(unsigned c=0;c<4;++c)fpword(s,at+4+c,rsxdraw::float_bits(v[c]));
 }
}
static void compare_memory(const PpuState& a,const PpuState& b){
 assert(a.memory.regions.size()==b.memory.regions.size());
 for(unsigned i=0;i<a.memory.regions.size();++i)if(*a.memory.regions[i].bytes!=*b.memory.regions[i].bytes)throw std::runtime_error("resident memory mismatch region="+std::to_string(i));
 assert(a.rsx.draw.pixels==b.rsx.draw.pixels);assert(a.rsx.draw.triangles==b.rsx.draw.triangles);
 assert(a.rsx.report_values==b.rsx.report_values);
}
static void batch_case(unsigned format,unsigned aa,unsigned targets,bool tiled=false,bool swizzled=false,bool blend=false){
 PpuState cpu,gpu;resident_setup(cpu,format,aa,targets,tiled,swizzled);resident_setup(gpu,format,aa,targets,tiled,swizzled);
 if(blend)for(auto* state:{&cpu,&gpu}){auto& R=state->rsx.registers;R[0x310]=1;R[0x36c]=14;R[0x314]=0x03020302;R[0x318]=0x03030303;R[0x320]=0x80068006;}
 gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;
 for(unsigned i=0;i<3;++i){
  unsetenv("ANYPS3_RSX_VK_TEST");cpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(cpu);
  setenv("ANYPS3_RSX_VK_TEST","1",1);gpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(gpu);
 }
 assert(batch.arena->dispatches==3);assert(batch.arena->submissions==0);assert(batch.cache.surface_uploads==targets);
 assert(gpu.memory.load(0xc0100000,4)==0); // guest stays untouched until explicit synchronization
 batch.flush();test_resident=nullptr;compare_memory(cpu,gpu);
 if(format==8 && aa==0 && targets==1 && !blend)assert(gpu.memory.load(0xc0100000,4)==0x80ff8040);
 if(format==11 && !swizzled && !blend){assert(gpu.memory.load(0xc0100000,8)==UINT64_C(0xc000380034003800));} // -2,.5,.25,.5 BE binary16
 assert(batch.cache.surface_readbacks==targets);
 std::printf("PASS resident format=%u aa=%u mrt=%u tiled=%u swizzled=%u submissions=%llu uploads=%llu\n",format,aa,targets,tiled,swizzled,(unsigned long long)batch.arena->submissions,(unsigned long long)batch.cache.surface_uploads);
}
static void coherence(){
 PpuState cpu,gpu;resident_setup(cpu,8,0,1);resident_setup(gpu,8,0,1);gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;
 setenv("ANYPS3_RSX_VK_TEST","1",1);rsx_execute_draw(gpu);
 auto* host=gpu.memory.regions[1].bytes->data()+0x100000;
 batch.host_access(host,4,false);assert(gpu.memory.load(0xc0100000,4)==0x80ff8040);assert(batch.cache.surfaces.size()==1);
 batch.host_access(host+1,1,true);gpu.memory.store(0xc0100001,1,0x11);assert(batch.cache.surfaces.empty());
 // Partial CPU write retained by masked GPU blue-only update.
 gpu.rsx.registers[0x324]=1;gpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(gpu);batch.flush();test_resident=nullptr;
 assert(gpu.memory.load(0xc0100000,4)==0x80118040);
 // Same host span, different guest mapping: coherence is keyed by host ownership.
 gpu.memory.regions.push_back({0x71000000,6,gpu.memory.regions[1].bytes,"alias"});
 batch.host_access(gpu.memory.access(0x71100000,4,false),4,false);assert(gpu.memory.load(0x71100000,4)==0x80118040);
 // Exact content texture cache must distinguish even a single changed word.
 const unsigned first=batch.cache.texture({1,2,3,4}),same=batch.cache.texture({1,2,3,4}),changed=batch.cache.texture({1,2,3,5});
 assert(first==same && changed!=first);batch.flush();
 std::puts("PASS explicit host read/write partial alias and exact-content cache");
}

static void depth_cases(){
 for(unsigned mode:{8u,13u,14u,15u,17u,18u}){
  PpuState cpu,gpu;setup_draw(cpu,128,128,512,false);setup_draw(gpu,128,128,512,false);customize(cpu,mode);customize(gpu,mode);
  gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;
  for(unsigned i=0;i<3;++i){unsetenv("ANYPS3_RSX_VK_TEST");cpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(cpu);
   setenv("ANYPS3_RSX_VK_TEST","1",1);gpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(gpu);}
  assert(batch.arena->dispatches==3);assert(batch.arena->submissions==0);batch.flush();test_resident=nullptr;compare_memory(cpu,gpu);
  std::printf("PASS resident depth/stencil/logic mode=%u\n",mode);
 }
}
static void nonfinite_batch(){
 {PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){setup_draw(*s,128,128,512,false);customize(*s,28);}
 unsetenv("ANYPS3_RSX_VK_TEST");rsx_execute_draw(cpu);
 gpu.rsx.renderer=std::make_unique<NativeVulkan>();rsxvk::ResidentSession batch(*gpu.rsx.renderer,gpu);test_resident=&batch;
 setenv("ANYPS3_RSX_VK_TEST","1",1);rsx_execute_draw(gpu);batch.flush();test_resident=nullptr;
 compare_memory(cpu,gpu);assert(gpu.memory.load(0xc0100000,4)==0xffffffff);
 std::puts("PASS IEEE infinity is valid FP output and clamps like CPU");}
 {PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){setup_draw(*s,128,128,512,false);texture_fp(*s,47);
 // The main-agent coordinate contract is a zero sample, including residency.
 fpword(*s,0,(47u<<24)|(15u<<9)|1u);fpword(*s,1,2u|(0xe4u<<9)|(7u<<18));fpword(*s,2,0);fpword(*s,3,0);
 for(unsigned c=0;c<4;++c)fpword(*s,4+c,c==0?0x7f800000:0);}
 unsetenv("ANYPS3_RSX_VK_TEST");rsx_execute_draw(cpu);
 gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;
 setenv("ANYPS3_RSX_VK_TEST","1",1);rsx_execute_draw(gpu);assert(batch.arena->dispatches==1);batch.flush();test_resident=nullptr;
 compare_memory(cpu,gpu);assert(gpu.memory.load(0xc0100000,4)==0);
 std::puts("PASS deferred non-finite texture coordinates sample zero like CPU");}
 // A genuinely divergent derivative still rejects the batch before guest commit.
 PpuState gpu;setup_draw(gpu,2,2,16,false);const unsigned exec=7u<<18,sw=0xe4u<<9;
 fpword(gpu,0,(3u<<24)|(1u<<9)|256);fpword(gpu,1,1u|exec);fpword(gpu,2,2u);fpword(gpu,3,0);
 for(unsigned i=0;i<4;++i)fpword(gpu,4+i,rsxdraw::float_bits(-1));
 fpword(gpu,8,2u<<24);fpword(gpu,9,1u<<18);fpword(gpu,10,0x80000000u|16);fpword(gpu,11,24);
 fpword(gpu,12,(21u<<24)|(15u<<9));fpword(gpu,13,1u|exec|sw);fpword(gpu,14,0);fpword(gpu,15,0);
 fpword(gpu,16,(1u<<24)|(15u<<9));fpword(gpu,17,2u|exec|sw);fpword(gpu,18,0);fpword(gpu,19,0);
 for(unsigned i=0;i<4;++i)fpword(gpu,20+i,rsxdraw::float_bits(.25));
 fpword(gpu,24,(1u<<24)|(15u<<9)|1u);fpword(gpu,25,2u|exec|sw);fpword(gpu,26,0);fpword(gpu,27,0);
 for(unsigned i=0;i<4;++i)fpword(gpu,28+i,rsxdraw::float_bits(.5));
 const auto before=*gpu.memory.regions[1].bytes;gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;
 rsxvk::ResidentSession batch(vk,gpu);const rsxdraw::Reg r{gpu.rsx};const auto fp=rsxdraw::load_fragment_program(gpu,r);
 const auto color=rsxdraw::bind_target(gpu,r(0x194),r(0x210),r(0x20c),2,2,4);std::array<rsxdraw::Texture,16> textures{};
 std::vector<std::array<rsxdraw::ShadedVertex,3>> triangles(1);triangles[0][0].clip={0,0,.25,1};triangles[0][1].clip={2,0,.25,1};triangles[0][2].clip={0,2,.25,1};
 std::uint64_t shaded=0;assert(rsxvk::draw(vk,gpu,triangles,fp,textures,color,{},2,2,0,0,2,2,shaded,nullptr,&batch));
 bool rejected=false;try{batch.flush();}catch(const std::runtime_error& e){rejected=std::string(e.what()).find("GPU result")!=std::string::npos;}
 assert(rejected && batch.failed);assert(*gpu.memory.regions[1].bytes==before);
 std::puts("PASS deferred divergent derivative fails before guest commit");
}
static void snapshot_sampling(){
 PpuState gpu;resident_setup(gpu,11,0,1);gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;
 rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;setenv("ANYPS3_RSX_VK_TEST","1",1);rsx_execute_draw(gpu);batch.finish();
 const auto before=batch.arena->download_bytes;
 const unsigned descriptor=batch.snapshot_texture(batch.cache.surfaces[0].offset,128,128,11);
 assert(batch.arena->download_bytes==before);assert(gpu.memory.load(0xc0100000,8)==0); // zero surface readback
 const auto base=vk.resident_allocate(*batch.arena,2580);std::vector<unsigned> data(2580,0);
 data[2064]=descriptor;data[2065]=1;data[2067]=data[2068]=3;data[2069]=data[2070]=1;data[2073]=rsxdraw::float_bits(16);
 for(unsigned c=0;c<4;++c){data[2079+c]=c;data[2083+c]=2;}
 batch.cache.upload(base,data);
 const std::string kernel=rsxvk::preamble()+R"GLSL(
 void main(){if(gl_GlobalInvocationID.x!=0u)return;bool killed=false;
 vec4 c=textureSample(0u,vec4(.5,.5,0,1),0,killed);for(uint i=0u;i<4u;++i)w[2576u+i]=floatBitsToUint(c[i]);}
 )GLSL";
 vk.resident_dispatch(*batch.arena,rsxvk::resident_source(kernel),base,1);vk.resident_download(*batch.arena,base+2576,4);batch.cache.finish();
 const auto sampled=vk.resident_read(*batch.arena,base+2576,4);
 assert(sampled[0]==0xc0000000 && sampled[1]==0x3f000000 && sampled[2]==0x3e800000 && sampled[3]==0x3f000000);
 batch.flush();test_resident=nullptr;std::puts("PASS GPU-only FP16 surface snapshot -> texture sampling; zero surface download");
}
static void transport_guards(){
 NativeVulkan vk;auto a=vk.resident_arena(4096);const unsigned src=vk.resident_allocate(*a,4),dst=vk.resident_allocate(*a,4);
 vk.resident_upload(*a,src,{1,2,3,4},0);bool rejected=false;try{vk.resident_upload(*a,dst,{5,6,7,8},0);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 vk.resident_copy(*a,src,dst,4);vk.resident_download(*a,dst,4);rejected=false;try{(void)vk.resident_read(*a,dst,4);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 vk.resident_finish(*a);assert(vk.resident_read(*a,dst,4)==std::vector<unsigned>({1,2,3,4}));vk.resident_free(*a,src);assert(vk.resident_allocate(*a,4)==src);
 rejected=false;try{vk.resident_copy(*a,dst,dst+1,3);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 // Exceed the cache capacity while commands reference old pipelines.
 // Shared pins and the bounded fence boundary must retain the final result.
 for(unsigned i=0;i<129;++i){const std::string shader="#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};void main(){if(gl_GlobalInvocationID.x==0u)w[0]="+std::to_string(i)+"u;}";
  vk.resident_dispatch(*a,shader,src,1);}
 assert(vk.compute_cache_size()<=64 && vk.compile_stats.compute_evictions>=65);vk.resident_download(*a,src,1);vk.resident_finish(*a);assert(vk.resident_read(*a,src,1)[0]==128);
 std::vector<unsigned> words(1,0);vk.compute("#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};void main(){if(gl_GlobalInvocationID.x==0u)w[0]=77u+0u;}",words,1);
 assert(words[0]==77 && vk.compute_cache_size()<=64);
 std::puts("PASS staging reuse, pending read, overlapping copy guards, allocation reuse and deferred pipeline eviction");
}

static void half_round_cases(){
 PpuState cpu,gpu;resident_setup(cpu,11,0,1);resident_setup(gpu,11,0,1);
 gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;
 const float values[2][4]={{1.f+1.f/2048,1.f+3.f/2048,-0.f,std::ldexp(1.f,-24)},
  {65504.f,0.f,std::ldexp(1.f,-25),-std::ldexp(3.f,-25)}};
 const std::uint64_t oracle[2]={UINT64_C(0x3c003c0280000001),UINT64_C(0x7bff000000008002)};
 for(unsigned which=0;which<2;++which){
  for(auto* state:{&cpu,&gpu})for(unsigned c=0;c<4;++c)fpword(*state,4+c,rsxdraw::float_bits(values[which][c]));
  unsetenv("ANYPS3_RSX_VK_TEST");cpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(cpu);
  setenv("ANYPS3_RSX_VK_TEST","1",1);gpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(gpu);batch.flush();
  compare_memory(cpu,gpu);assert(gpu.memory.load(0xc0100000,8)==oracle[which]);
 }
 test_resident=nullptr;std::puts("PASS independent FP16 round-to-even ties, signed zero, min subnormal and largest finite oracles");
}
static void benchmark_resident(){
 PpuState cpu,gpu;setup_draw(cpu,640,360,2560,false);setup_draw(gpu,640,360,2560,false);texture_fp(cpu);texture_fp(gpu);
 gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;rsxvk::ResidentSession batch(vk,gpu,32*1024*1024);test_resident=&batch;
 // Compile once, then time a fresh batch with resident surfaces and textures.
 setenv("ANYPS3_RSX_VK_TEST","1",1);rsx_execute_draw(gpu);batch.finish();
 const auto uploads=batch.arena->upload_bytes,downloads=batch.arena->download_bytes,submissions=batch.arena->submissions;
 const auto upload_gpu=batch.arena->upload_gpu_ms,dispatch_gpu=batch.arena->dispatch_gpu_ms,readback_gpu=batch.arena->readback_gpu_ms;
 const auto upload_ms=batch.arena->upload_cpu_ms,record_ms=batch.arena->record_cpu_ms,wait_ms=batch.arena->wait_cpu_ms,pipeline_ms=batch.arena->pipeline_cpu_ms;
 auto t=std::chrono::steady_clock::now();
 const unsigned n=16;
 for(unsigned i=0;i<n;++i){gpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(gpu);}batch.finish();
 const double seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-t).count();
 const auto status_readbacks=batch.arena->download_bytes-downloads;
 const auto draw_submissions=batch.arena->submissions-submissions;
 const double cpu_prepare_upper=seconds*1000-(batch.arena->upload_cpu_ms-upload_ms)-(batch.arena->record_cpu_ms-record_ms)-(batch.arena->wait_cpu_ms-wait_ms)-(batch.arena->pipeline_cpu_ms-pipeline_ms);
 t=std::chrono::steady_clock::now();batch.flush();const double flush_seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-t).count();
 const auto final_readback=batch.arena->download_bytes-downloads-status_readbacks;
 test_resident=nullptr;unsetenv("ANYPS3_RSX_VK_TEST");cpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(cpu);t=std::chrono::steady_clock::now();
 for(unsigned i=0;i<n;++i){cpu.rsx.draw.ranges={{0,4}};rsx_execute_draw(cpu);}const double cpu_seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-t).count();
 // Texture interpolation may differ by one UNORM bit, as in the original suite.
 unsigned max_delta=0;for(unsigned y=0;y<360;++y)for(unsigned x=0;x<640*4;++x){unsigned a=cpu.memory.load(0xc0100000+y*2560+x,1),b=gpu.memory.load(0xc0100000+y*2560+x,1);max_delta=std::max(max_delta,a>b?a-b:b-a);}assert(max_delta<=1);
 std::printf("BENCH resident draws=%u draw_seconds=%.6f final_surface_sync_seconds=%.6f total_seconds=%.6f Mpix_s=%.3f cpu_seconds_16=%.6f upload_bytes=%llu status_count_readback_bytes=%llu final_surface_readback_bytes=%llu draw_submissions=%llu total_submissions=%llu surface_uploads=%llu texture_uploads=%llu texture_hits=%llu upload_cpu_ms=%.3f record_cpu_ms=%.3f submit_wait_ms=%.3f pipeline_cpu_ms=%.3f upload_gpu_ms=%.3f dispatch_gpu_ms=%.3f readback_gpu_ms=%.3f cpu_prepare_upper_ms=%.3f max_delta=%u\n",n,seconds,flush_seconds,seconds+flush_seconds,n*.2304/(seconds+flush_seconds),cpu_seconds,(unsigned long long)(batch.arena->upload_bytes-uploads),(unsigned long long)status_readbacks,(unsigned long long)final_readback,(unsigned long long)draw_submissions,(unsigned long long)(batch.arena->submissions-submissions),(unsigned long long)batch.cache.surface_uploads,(unsigned long long)batch.cache.texture_uploads,(unsigned long long)batch.cache.texture_hits,batch.arena->upload_cpu_ms-upload_ms,batch.arena->record_cpu_ms-record_ms,batch.arena->wait_cpu_ms-wait_ms,batch.arena->pipeline_cpu_ms-pipeline_ms,batch.arena->upload_gpu_ms-upload_gpu,batch.arena->dispatch_gpu_ms-dispatch_gpu,batch.arena->readback_gpu_ms-readback_gpu,cpu_prepare_upper,max_delta);
}
int main(int argc,char** argv){try{
 setenv("ANYPS3_RSX_THREADS","4",1);setenv("ANYPS3_RSX_VK_PROFILE","1",1);
 if(argc>1 && std::string(argv[1])=="benchmark"){benchmark_resident();return 0;}
 for(unsigned format:{1u,2u,3u,4u,5u,8u,9u,10u,11u,12u,13u,14u,15u,16u})batch_case(format,0,1);
 batch_case(11,3,1);batch_case(8,3,2);batch_case(11,0,4);batch_case(8,0,1,true);batch_case(8,0,1,false,true);batch_case(11,0,1,false,true);
 batch_case(8,0,1,false,false,true);batch_case(11,3,2,false,false,true);
 coherence();depth_cases();nonfinite_batch();snapshot_sampling();transport_guards();half_round_cases();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    source = original_source()
    marker = '// Returns true when reg is a SET_VERTEX_DATA*_M register write it handled.'
    source = source.replace(marker, 'static rsxvk::ResidentSession* test_resident=nullptr;\n' + marker)
    # Only test hook accepts the explicit scope; production integration stays synchronous.
    source = source.replace('width,height,x0,y0,x1,y1,count)){', 'width,height,x0,y0,x1,y1,count,&colors,test_resident)){')
    source = source.replace('differential();vertex_test();fragment_tests();fallback();invalid_coordinates();', 'differential();vertex_test();fragment_tests();fallback();invalid_coordinates();return 0;')
    source = source.replace('int main(int argc,char** argv)', 'int previous_test_main(int argc,char** argv)')
    return source + '\n' + MAIN

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class ResidentTests(unittest.TestCase):
    def test_batch_and_coherence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(native_source())
            result = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root/'test.cpp'), *LIBRARIES, '-o', str(root/'test')], capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn('Validation Error', run.stdout + run.stderr)
            self.assertNotIn('Validation Warning', run.stdout + run.stderr)
            self.assertEqual(run.stdout.count('PASS resident format='), 22)
