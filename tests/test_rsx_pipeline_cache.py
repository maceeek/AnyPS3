"""Stable structural keys, persistent caches, bounded LRU and GPU lifetime oracles."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import unittest

from tests.test_rsx_gpu_upload import native_source as upload_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static const std::string simple_vs=R"GLSL(#version 450
void main(){vec2 p=gl_VertexIndex==0?vec2(-1,-1):gl_VertexIndex==1?vec2(3,-1):vec2(-1,3);gl_Position=vec4(p,0,1);}
)GLSL";
static const std::string simple_fs=R"GLSL(#version 450
layout(std430,binding=0) readonly buffer D{uint w[];};
layout(location=0) out vec4 color;
void main(){color=vec4((w[0]&1u)==0u?1:0,(w[0]&1u)!=0u?1:0,0,1);}
)GLSL";
static NativeVulkan::GraphicsState simple_state(){NativeVulkan::GraphicsState s;s.width=s.height=4;s.scissor={{0,0},{4,4}};
 s.blend[0].colorWriteMask=15;s.raster.frontFace=VK_FRONT_FACE_COUNTER_CLOCKWISE;s.z.depthCompareOp=VK_COMPARE_OP_ALWAYS;return s;}
static const std::array<std::array<float,2>,4> sample_positions{{{.5f,.5f},{0,0},{0,0},{0,0}}};
static void stable_and_disk(bool warm,bool rejected=false){
 NativeVulkan vk;auto b=vk.graphics_batch(1024*1024);auto s=simple_state();
 auto image=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};images[0]=image;
 const auto original=vk.compile_stats;
 std::vector<unsigned> data(16);data[0]=0;
 vk.graphics_draw(*b,s,images,{},simple_vs,simple_fs,data,3,sample_positions);vk.graphics_finish(*b);vk.graphics_reset(*b);
 const auto initial=vk.compile_stats;
 for(unsigned i=0;i<40;++i){data[0]=i;s.scissor={{int(i%2),0},{3,4}};s.constants={float(i)/40,0,0,1};
  for(auto* face:{&s.z.front,&s.z.back}){face->compareMask=i;face->writeMask=255-i;face->reference=i*3;}
  // New owners of identical content must share the existing pipeline.
  auto vs=std::make_shared<const std::string>(simple_vs),fs=std::make_shared<const std::string>(simple_fs);
  vk.graphics_draw(*b,s,images,{},*vs,*fs,data,3,sample_positions,float(i),float(i),0,1,{},0,UINT64_MAX,{},vs,fs);
 }
 auto at=vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);
 const auto* pixel=static_cast<const unsigned char*>(b->map)+at+4;assert(pixel[0]==0 && pixel[1]==255 && pixel[2]==0 && pixel[3]==255);
 assert(vk.compile_stats.graphics_pipelines==initial.graphics_pipelines && vk.compile_stats.spirv_compiles==initial.spirv_compiles && vk.compile_stats.shader_modules==initial.shader_modules);
 assert(initial.graphics_pipelines-original.graphics_pipelines==1 && vk.compile_stats.graphics_hits>=40);
 vk.graphics_reset(*b);
 // Fixed function changes require a pipeline, but reuse the two VkShaderModules.
 s.blend[0].colorWriteMask=7;vk.graphics_draw(*b,s,images,{},simple_vs,simple_fs,data,3,sample_positions);vk.graphics_finish(*b);vk.graphics_reset(*b);
 assert(vk.compile_stats.graphics_pipelines==initial.graphics_pipelines+1 && vk.compile_stats.shader_modules==initial.shader_modules);
 // Parallel per-element work with a fixed exact output, reused across process restarts.
 std::vector<unsigned> words(257);const std::string compute="#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};void main(){uint i=gl_GlobalInvocationID.x;if(i<257u)w[i]=i*7u+3u;}";
 vk.compute(compute,words,5);for(unsigned i=0;i<257;++i)assert(words[i]==i*7+3);
 auto compiles=vk.compile_stats.spirv_compiles;vk.compute(compute,words,5);assert(vk.compile_stats.spirv_compiles==compiles);
 std::fprintf(stderr,"cache verification compiles=%llu hits=%llu loaded=%llu rejected=%llu io=%llu\n",(unsigned long long)vk.compile_stats.spirv_compiles,(unsigned long long)vk.compile_stats.spirv_disk_hits,(unsigned long long)vk.compile_stats.pipeline_cache_load_bytes,(unsigned long long)vk.compile_stats.cache_rejected,(unsigned long long)vk.compile_stats.cache_io_errors);
 if(warm){assert(vk.compile_stats.spirv_compiles==0 && vk.compile_stats.spirv_disk_hits==3 && (rejected?(vk.compile_stats.pipeline_cache_load_bytes==0 && vk.compile_stats.cache_rejected>0):vk.compile_stats.pipeline_cache_load_bytes>=32));}
 else assert(vk.compile_stats.spirv_compiles==3);
 std::printf("PASS stable keys modules=%llu pipelines=%llu spirv_compiles=%llu disk_hits=%llu\n",(unsigned long long)vk.compile_stats.shader_modules,(unsigned long long)vk.compile_stats.graphics_pipelines,(unsigned long long)vk.compile_stats.spirv_compiles,(unsigned long long)vk.compile_stats.spirv_disk_hits);
}
static void stencil_oracle(){
 NativeVulkan vk;auto b=vk.graphics_batch(1024*1024);auto s=simple_state();s.depth=VK_FORMAT_D24_UNORM_S8_UINT;s.z.stencilTestEnable=true;
 for(auto* f:{&s.z.front,&s.z.back}){f->compareOp=VK_COMPARE_OP_ALWAYS;f->passOp=VK_STENCIL_OP_REPLACE;f->failOp=f->depthFailOp=VK_STENCIL_OP_KEEP;f->compareMask=255;f->writeMask=255;f->reference=0xa5;}
 auto color=vk.graphics_image(*b,s.color,4,4,1),depth=vk.graphics_image(*b,s.depth,4,4,1,true);
 unsigned char zero[16]{};vk.graphics_transfer(*b,depth,VK_IMAGE_ASPECT_STENCIL_BIT,1,zero);
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};images[0]=color;std::vector<unsigned> data(16);
 vk.graphics_draw(*b,s,images,depth,simple_vs,simple_fs,data,3,sample_positions);
 for(auto* f:{&s.z.front,&s.z.back}){f->reference=0x3c;f->writeMask=0x0f;}
 vk.graphics_draw(*b,s,images,depth,simple_vs,simple_fs,data,3,sample_positions);
 auto at=vk.graphics_transfer(*b,depth,VK_IMAGE_ASPECT_STENCIL_BIT,1);vk.graphics_finish(*b);
 for(unsigned i=0;i<16;++i)assert(static_cast<const unsigned char*>(b->map)[at+i]==0xac);
 assert(vk.compile_stats.graphics_pipelines==1);vk.graphics_reset(*b);
 // The comparison must also use the changed dynamic mask/reference.
 for(auto* f:{&s.z.front,&s.z.back}){f->compareOp=VK_COMPARE_OP_EQUAL;f->reference=0x0c;f->compareMask=0x0f;f->writeMask=0;}
 data[0]=1;vk.graphics_draw(*b,s,images,depth,simple_vs,simple_fs,data,3,sample_positions);
 auto c=vk.graphics_transfer(*b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);
 assert(static_cast<const unsigned char*>(b->map)[c+1]==255);vk.graphics_reset(*b);
 for(auto* f:{&s.z.front,&s.z.back})f->compareMask=0xff;
 data[0]=0;vk.graphics_draw(*b,s,images,depth,simple_vs,simple_fs,data,3,sample_positions);
 c=vk.graphics_transfer(*b,color,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);assert(static_cast<const unsigned char*>(b->map)[c+1]==255);vk.graphics_reset(*b);
 assert(vk.compile_stats.graphics_pipelines==2);std::puts("PASS fixed stencil oracles ac, pass and reject after mask change");
}
static void bounded(unsigned keys){
 setenv("ANYPS3_RSX_PIPELINE_LIMIT","8",1);setenv("ANYPS3_RSX_MODULE_LIMIT","4",1);setenv("ANYPS3_RSX_FRAMEBUFFER_LIMIT","4",1);setenv("ANYPS3_RSX_COMPUTE_LIMIT","4",1);setenv("ANYPS3_RSX_IDENTITY_LIMIT","8",1);
 NativeVulkan vk;auto b=vk.graphics_batch(1024*1024);auto s=simple_state();
 struct Expected{std::uint64_t at;unsigned draw,key;};std::vector<Expected> expected;
 auto finish=[&]{vk.graphics_finish(*b);for(const auto& e:expected){for(unsigned pixel=0;pixel<16;++pixel){const auto* p=static_cast<const unsigned char*>(b->map)+e.at+pixel*4;assert(p[0]==(e.draw%2?0:255) && p[1]==(e.draw%2?255:0) && p[2]==((e.key>>pixel)&1u?255:0) && p[3]==255);}}expected.clear();vk.graphics_reset(*b);};
 for(unsigned i=0;i<keys+1;++i){const unsigned key=i==keys?0:i;auto image=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};images[0]=image;
  auto text=simple_fs;
  // Every key changes executable SPIR-V and the independently checked 4x4
  // blue pattern, rather than merely changing GLSL comments or debug names.
  text.insert(text.rfind('}'),"color.b=(("+std::to_string(key)+"u>>(uint(gl_FragCoord.x)+4u*uint(gl_FragCoord.y)))&1u)!=0u?1:0;");
  auto fs=std::make_shared<const std::string>(std::move(text)),vs=std::make_shared<const std::string>(simple_vs);
  std::vector<unsigned> data(16);data[0]=i;vk.graphics_draw(*b,s,images,{},*vs,*fs,data,3,sample_positions,0,0,0,1,{},0,UINT64_MAX,{},vs,fs);
  expected.push_back({vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4),i,key});
  assert(vk.graphics_cache_size()<=8 && b->framebuffers.size()<=4);
  if(i%16==15)finish();
 }
 finish();assert(vk.compile_stats.pipeline_evictions>=keys-8 && vk.compile_stats.module_evictions>=keys-4);
 auto& live=NativeVulkan::resource_stats();assert(live.kinds["pipelines"].peak_count<=28 && live.kinds["shader_modules"].peak_count<=6 && live.kinds["framebuffers"].peak_count<=21);
 std::printf("PASS bounded %u shader/pipeline/framebuffer keys, independent draw bytes after eviction\n",keys);
}
// Default bounds (no ANYPS3_RSX_*_LIMIT in the environment): a working set of `keys` distinct shader pairs, then key 0 again.
// Independent expectation: the repeat creates no VkPipeline/VkShaderModule (nothing was evicted) and still draws key 0's own
// 4x4 bit pattern. With the former 256-entry defaults, keys=300 evicts key 0 and the repeat recompiles it.
static void default_working_set(unsigned keys){
 NativeVulkan vk;auto b=vk.graphics_batch(1024*1024);auto s=simple_state();
 struct Expected{std::uint64_t at;unsigned key;};std::vector<Expected> expected;
 auto finish=[&]{vk.graphics_finish(*b);for(const auto& e:expected)for(unsigned pixel=0;pixel<16;++pixel){const auto* p=static_cast<const unsigned char*>(b->map)+e.at+pixel*4;
   assert(p[0]==255 && p[1]==0 && p[2]==((e.key>>pixel)&1u?255:0) && p[3]==255);}expected.clear();vk.graphics_reset(*b);};
 auto draw=[&](unsigned key){auto image=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};images[0]=image;
  auto text=simple_fs;text.insert(text.rfind('}'),"color.b=(("+std::to_string(key)+"u>>(uint(gl_FragCoord.x)+4u*uint(gl_FragCoord.y)))&1u)!=0u?1:0;");
  auto fs=std::make_shared<const std::string>(std::move(text)),vs=std::make_shared<const std::string>(simple_vs);
  std::vector<unsigned> data(16);vk.graphics_draw(*b,s,images,{},*vs,*fs,data,3,sample_positions,0,0,0,1,{},0,UINT64_MAX,{},vs,fs);
  expected.push_back({vk.graphics_transfer(*b,image,VK_IMAGE_ASPECT_COLOR_BIT,4),key});};
 for(unsigned key=0;key<keys;++key){draw(key);if(key%16==15)finish();}
 finish();const auto before=vk.compile_stats;
 draw(0);finish();
 assert(before.graphics_pipelines==keys && vk.compile_stats.graphics_pipelines==before.graphics_pipelines && vk.compile_stats.shader_modules==before.shader_modules);
 assert(vk.compile_stats.pipeline_evictions==0 && vk.compile_stats.module_evictions==0 && vk.graphics_cache_size()==keys);
 std::printf("PASS default bounds retain a %u pipeline working set\n",keys);
}
// `keys` distinct pipelines in one session; NativeVulkan's destructor saves the VkPipelineCache (ANYPS3_CACHE_ROOT).
static void persist(unsigned keys){
 NativeVulkan vk;auto b=vk.graphics_batch(1024*1024);auto s=simple_state();
 for(unsigned key=0;key<keys;++key){auto image=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};images[0]=image;
  auto text=simple_fs;text.insert(text.rfind('}'),"color.b=(("+std::to_string(key)+"u>>(uint(gl_FragCoord.x)+4u*uint(gl_FragCoord.y)))&1u)!=0u?1:0;");
  auto fs=std::make_shared<const std::string>(std::move(text)),vs=std::make_shared<const std::string>(simple_vs);
  std::vector<unsigned> data(16);vk.graphics_draw(*b,s,images,{},*vs,*fs,data,3,sample_positions,0,0,0,1,{},0,UINT64_MAX,{},vs,fs);
  if(key%16==15){vk.graphics_finish(*b);vk.graphics_reset(*b);}}
 vk.graphics_finish(*b);vk.graphics_reset(*b);assert(vk.compile_stats.graphics_pipelines==keys);
 std::printf("PASS persist %u pipelines rotations=%llu load_bytes=%llu\n",keys,(unsigned long long)vk.compile_stats.pipeline_cache_rotations,(unsigned long long)vk.compile_stats.pipeline_cache_load_bytes);
}
// A periodic background snapshot that fails (cache directory read-only) must leave the cache dirty, so a later save (the next periodic
// one or the forced save at shutdown, directory writable again) still writes it. Handshake: the directory stays read-only until the
// failed background save has been collected (cache_io_errors rises above the count after the synchronous SPIR-V writes); only then
// is write access restored. The old code (dirty cleared before the save) then never writes the file.
static void save_retry(){
 setenv("ANYPS3_RSX_HOST_PROFILE","1",1);const std::filesystem::path dir=std::filesystem::path(std::getenv("ANYPS3_CACHE_ROOT"))/"vk";
 {NativeVulkan vk;assert(std::filesystem::is_directory(dir));
  std::filesystem::permissions(dir,std::filesystem::perms::owner_read|std::filesystem::perms::owner_exec);
  auto b=vk.graphics_batch(1024*1024);auto s=simple_state();
  for(unsigned key=0;key<8;++key){auto image=vk.graphics_image(*b,s.color,4,4,1);std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{};images[0]=image;
   auto text=simple_fs;text.insert(text.rfind('}'),"color.b=(("+std::to_string(key)+"u>>(uint(gl_FragCoord.x)+4u*uint(gl_FragCoord.y)))&1u)!=0u?1:0;");
   auto fs=std::make_shared<const std::string>(std::move(text)),vs=std::make_shared<const std::string>(simple_vs);
   std::vector<unsigned> data(16);vk.graphics_draw(*b,s,images,{},*vs,*fs,data,3,sample_positions,0,0,0,1,{},0,UINT64_MAX,{},vs,fs);}
  vk.graphics_finish(*b);vk.graphics_reset(*b);
  const auto before=vk.compile_stats.cache_io_errors;
  // Each print (throttled to 2 s) launches the periodic save or collects a finished one without waiting.
  for(unsigned i=0;i<15 && vk.compile_stats.cache_io_errors==before;++i){std::this_thread::sleep_for(std::chrono::milliseconds(2100));vk.compile_stats_print();}
  if(vk.compile_stats.cache_io_errors==before)throw std::runtime_error("the read-only background save was never collected as failed");
  std::filesystem::permissions(dir,std::filesystem::perms::owner_all);
 }
 std::printf("PASS save retry\n");
}
static void compute_lru(unsigned keys){
 setenv("ANYPS3_RSX_COMPUTE_LIMIT","4",1);NativeVulkan vk;std::vector<unsigned> words(1);
 for(unsigned i=0;i<keys+1;++i){unsigned key=i==keys?0:i;
  auto source="#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};void main(){if(gl_GlobalInvocationID.x==0u)w[0]="+std::to_string(key)+"u;}";
  vk.compute(source,words,1);assert(words[0]==key && vk.compute_cache_size()<=4);
 }
 auto arena=vk.resident_arena(4096);auto at=vk.resident_allocate(*arena,16);vk.resident_upload(*arena,at,std::vector<unsigned>(16),0);
 for(unsigned i=0;i<40;++i){const auto source="#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};layout(push_constant) uniform P{uint base;};void main(){if(gl_GlobalInvocationID.x==0u)w[base]="+std::to_string(i)+"u;}";vk.resident_dispatch(*arena,source,at,1);}
 vk.resident_download(*arena,at,1);vk.resident_finish(*arena);assert(vk.resident_read(*arena,at,1)[0]==39);
 assert(vk.compute_cache_size()<=4 && vk.compile_stats.compute_evictions>keys-4);std::printf("PASS bounded %u compute keys and pending resident eviction\n",keys);
}
static void resident_working_set(){
 NativeVulkan vk;auto arena=vk.resident_arena(96);rsxvk::SurfaceCache cache(vk,*arena);
 unsigned first=0,second=0;
 {rsxvk::SurfaceCache::BuildScope scope(cache);first=cache.texture(std::vector<unsigned>(40,1));second=cache.texture(std::vector<unsigned>(40,2));
  bool exhausted=false;try{cache.texture(std::vector<unsigned>(24,3));}catch(const std::runtime_error&){exhausted=true;}
  assert(exhausted);cache.finish();vk.resident_download(*arena,first,1);vk.resident_download(*arena,second,1);cache.finish();assert(vk.resident_read(*arena,first,1)[0]==1 && vk.resident_read(*arena,second,1)[0]==2);
 }
 const auto third=cache.texture(std::vector<unsigned>(32,3));cache.finish();vk.resident_download(*arena,third,1);cache.finish();assert(vk.resident_read(*arena,third,1)[0]==3);
 std::puts("PASS current draw working set pinned, later LRU allocation reuses completed storage");
}
static void raw_lru(unsigned keys){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;R[0x1a18]=0x00010001;R[0x1840]=4;
 s.memory.regions[1].bytes->resize(32*1024*1024);s.rsx.local_size=32*1024*1024;
 for(unsigned i=0;i<keys;++i)s.memory.store(0xc0004000+i*64,4,i%2?0xff00ff00:0xffff0000);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 for(unsigned i=0;i<keys+1;++i){unsigned key=i==keys?0:i;R[0x1a00]=0x4000+key*64;repeat_draw(s);
  assert(session.raw_textures.size()<=512 && session.raw_bytes<=UINT64_C(512)*1024*1024);if(i%32==31 || i==keys){session.flush();assert(near_argb(s.memory.load(0xc0100000,4),key%2?0xff00ff00:0xffff0000));}}
 assert(session.raw_evictions>0);std::printf("PASS bounded %u raw texture keys, evictions=%llu\n",keys,(unsigned long long)session.raw_evictions);s.rsx.graphics_session=nullptr;
}
// fix7-stutter: one session writing `keys` distinct SPIR-V files keeps the cache directory within ANYPS3_RSX_DISK_CACHE_FILES /
// _MB at every write (checked after each compile, inside the same NativeVulkan: totals are kept in memory after one scan) and
// evicts the oldest files first (the newest key's file survives, the first key's is gone once the bound was exceeded).
static void disk_bound(unsigned keys){
 const std::filesystem::path dir=std::filesystem::path(std::getenv("ANYPS3_CACHE_ROOT"))/"vk";
 const auto* f=std::getenv("ANYPS3_RSX_DISK_CACHE_FILES");const auto* m=std::getenv("ANYPS3_RSX_DISK_CACHE_MB");
 const std::uint64_t file_limit=f?std::stoull(f):32768,byte_limit=(m?std::stoull(m):4096)<<20;
 NativeVulkan vk;std::vector<unsigned> words(1);std::uint64_t peak_files=0,peak_bytes=0;
 auto census=[&](std::uint64_t& files,std::uint64_t& bytes){files=bytes=0;for(const auto& e:std::filesystem::directory_iterator(dir)){const auto n=e.path().filename().string();
  if(n.rfind("spv-",0)==0 || n.rfind("pipeline-",0)==0){++files;bytes+=e.file_size();}}};
 std::vector<std::filesystem::path> written;
 for(unsigned key=0;key<keys;++key){
  const auto source="#version 450\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer D{uint w[];};void main(){if(gl_GlobalInvocationID.x==0u)w[0]="+std::to_string(key*7+3)+"u;}";
  vk.compute(source,words,1);assert(words[0]==key*7+3);
  std::uint64_t files=0,bytes=0;census(files,bytes);peak_files=std::max(peak_files,files);peak_bytes=std::max(peak_bytes,bytes);
  if(files>file_limit || bytes>byte_limit)throw std::runtime_error("disk cache over its bound after key "+std::to_string(key)+": files="+std::to_string(files)+" bytes="+std::to_string(bytes));
 }
 std::uint64_t files=0,bytes=0;census(files,bytes);
 std::printf("PASS disk bound %u keys files=%llu peak_files=%llu peak_bytes=%llu compiles=%llu\n",keys,(unsigned long long)files,(unsigned long long)peak_files,(unsigned long long)peak_bytes,(unsigned long long)vk.compile_stats.spirv_compiles);
}
int main(int argc,char** argv){try{
 if(argc>1 && std::string(argv[1])=="disk_bound")disk_bound(std::stoul(argv[2]));
 else if(argc>1 && std::string(argv[1])=="persist")persist(std::stoul(argv[2]));
 else if(argc>1 && std::string(argv[1])=="save_retry")save_retry();
 else if(argc>1 && std::string(argv[1])=="working_set")default_working_set(argc>2?std::stoul(argv[2]):300);
 else if(argc>1 && std::string(argv[1])=="stress"){bounded(argc>2?std::stoul(argv[2]):1100);raw_lru(1100);compute_lru(1100);resident_working_set();}
 else if(argc>1 && std::string(argv[1])=="stencil")stencil_oracle();
 else stable_and_disk(argc>1 && (std::string(argv[1])=="warm" || std::string(argv[1])=="reject"),argc>1 && std::string(argv[1])=="reject");
 for(const auto& p:NativeVulkan::resource_stats().kinds)assert(p.second.objects.empty());
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return upload_source().split('int main(int argc,char** argv)')[0] + MAIN


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class PipelineCacheTests(unittest.TestCase):
    def test_stable_persistent_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            build = subprocess.run(command, capture_output=True, text=True, timeout=240)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, ANYPS3_CACHE_ROOT=str(root / 'cache'), LP_NUM_THREADS='4', VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            def run(*args):
                result = subprocess.run([str(root / 'test'), *args], env=env, capture_output=True, text=True, timeout=300)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('Validation Error', result.stdout + result.stderr)
                self.assertNotIn('Validation Warning', result.stdout + result.stderr)
                return result.stdout
            run()
            self.assertIn('spirv_compiles=0 disk_hits=3', run('warm'))
            pipeline_file = next((root / 'cache/vk').glob('pipeline-*.bin'))
            blob = bytearray(pipeline_file.read_bytes())
            # Correct checksum, incompatible vendor: reject before calling Vulkan.
            struct.pack_into('<I', blob, 24, 0x12345678)
            checksum = 14695981039346656037
            for byte in blob[16:]:
                checksum = ((checksum ^ byte) * 1099511628211) & ((1 << 64) - 1)
            struct.pack_into('<Q', blob, 8, checksum)
            pipeline_file.write_bytes(blob)
            self.assertIn('spirv_compiles=0', run('reject'))
            cache_files = list((root / 'cache/vk').glob('spv-*.bin'))
            self.assertEqual(len(cache_files), 3)
            for p in cache_files:
                p.write_bytes(b'broken cache')
            self.assertIn('spirv_compiles=3', run())
            self.assertIn('fixed stencil oracles', run('stencil'))
            self.assertIn('1100 raw texture keys', run('stress'))
            self.assertLessEqual(len(list((root / 'cache/vk').glob('*.bin'))), 32768)
            # fix7-stutter: the default bound keeps more than the old 1024 files of one session; an explicit file or byte bound holds
            # after every write of one process (the running totals, not only the first scan).
            for name, limits, keys, expect_files in (('default', {}, 1100, 1100), ('files', {'ANYPS3_RSX_DISK_CACHE_FILES': '40'}, 120, None),
                                                     ('bytes', {'ANYPS3_RSX_DISK_CACHE_MB': '1'}, 120, None)):
                bound_env = dict(env, ANYPS3_CACHE_ROOT=str(root / f'bound-{name}'), **limits)
                result = subprocess.run([str(root / 'test'), 'disk_bound', str(keys)], env=bound_env, capture_output=True, text=True, timeout=600)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f'PASS disk bound {keys} keys', result.stdout)
                if expect_files:
                    self.assertGreaterEqual(len(list((root / f'bound-{name}' / 'vk').glob('spv-*.bin'))), expect_files)
            self.assertIn('retain a 300 pipeline working set', run('working_set', '300'))
            # Lifecycle of the persisted VkPipelineCache, independent of how a driver serializes it: a session creating 200 distinct
            # pipelines keeps one cache (no rotation; the old code replaced it after every 128 creations and persisted only the last
            # segment), saves it at shutdown, and the next session loads exactly the saved payload (file = 16-byte envelope + payload).
            persist_root = root / 'persist'
            env['ANYPS3_CACHE_ROOT'] = str(persist_root)
            self.assertIn('PASS persist 200 pipelines rotations=0 load_bytes=0', run('persist', '200'))
            saved = next((persist_root / 'vk').glob('pipeline-*.bin')).stat().st_size - 16
            self.assertIn(f'PASS persist 20 pipelines rotations=0 load_bytes={saved}', run('persist', '20'))
            # A failed background save stays dirty and is retried by the forced save at shutdown.
            retry_root = root / 'retry'
            env['ANYPS3_CACHE_ROOT'] = str(retry_root)
            out = run('save_retry')
            self.assertIn('PASS save retry', out)
            self.assertEqual(len(list((retry_root / 'vk').glob('pipeline-*.bin'))), 1)
            # Driver-specific measurement only (not asserted: a driver may deduplicate or keep a header-only cache): bytes persisted
            # for 100 vs 200 distinct pipelines on the hardware device.
            gpu_env = {k: v for k, v in os.environ.items() if k not in ('VK_DRIVER_FILES', 'VK_ICD_FILENAMES', 'VK_INSTANCE_LAYERS')}
            for keys in (100, 200):
                gpu_env.update(ANYPS3_CACHE_ROOT=str(root / f'persist-{keys}'), ANYPS3_VK_DEVICE='gpu')
                result = subprocess.run([str(root / 'test'), 'persist', str(keys)], env=gpu_env, capture_output=True, text=True, timeout=300)
                files = list((root / f'persist-{keys}' / 'vk').glob('pipeline-*.bin'))
                print(f'pipeline cache bytes for {keys} pipelines on the hardware device: {files[0].stat().st_size if files else None} (rc {result.returncode})')
            cache_root = env['ANYPS3_CACHE_ROOT']
            blocked = root / 'not-a-directory'
            blocked.write_text('cache is optional')
            env['ANYPS3_CACHE_ROOT'] = str(blocked)
            self.assertIn('spirv_compiles=3', run())
            env['ANYPS3_CACHE_ROOT'] = cache_root
