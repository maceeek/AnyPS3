"""GPU raw uploads: fixed byte oracles, reference parity and cache coherence."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void gathered_cache(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);
 auto t=rsxdraw::decode_texture(s,rsxdraw::Reg{s.rsx},0);
 auto gather=[&]{auto copy=t;copy.untiled=std::make_shared<GuestBacking>(t.levels[0].data,t.levels[0].data+t.storage_size);copy.levels[0].data=copy.untiled->data();return copy;};
 auto a=gather();auto image=session.texture_image(a);auto b=gather();assert(a.levels[0].data!=b.levels[0].data);assert(session.texture_image(b)==image);assert(session.raw_hits==1 && session.raw_misses==1);
 // A fresh gathered/split/tiled snapshot keeps its physical source identity.
 // Changed guest bytes invalidate it even without an observer on the gather.
 s.memory.store(0xc0002000,4,0xff224466);auto c=gather();assert(session.texture_image(c)!=image);assert(session.raw_invalidations==1 && session.raw_misses==2);
 session.flush();
 // A native RGBA8 upload uses four bytes per texel, not a pessimistic
 // sixteen-byte FP32 estimate. 2048 squared is exactly 16 MiB.
 rsxdraw::Texture large=t;large.untiled=std::make_shared<GuestBacking>(16*1024*1024);
 large.levels[0].data=large.untiled->data();large.levels[0].width=large.levels[0].height=large.levels[0].backing_width=large.levels[0].backing_height=2048;large.levels[0].pitch=8192;large.storage_size=large.untiled->size();
 auto uploaded=session.texture_image(large);assert(uploaded->width==2048 && uploaded->height==2048 && uploaded->format==VK_FORMAT_R8G8B8A8_UNORM);session.flush();
 std::puts("PASS native 2048x2048 RGBA8 upload within staging capacity");
 std::puts("PASS gathered snapshots reuse image and detect physical source writes");
}
static void repeat_draw(PpuState& s){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
static void upload_cache(){
 PpuState s;setup_draw(s,32,16,128,false);texture_fp(s);
 // The entire quad reads the first texel: fixed BE oracle, independent of CPU.
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002100+i*8+c*4,4,0);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);
 s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 const auto cpu_vp=rsxdraw::host_stats().cpu_vertex,cpu_clip=rsxdraw::host_stats().cpu_clip;
 rsx_execute_draw(s);const auto cpu_texel=rsxdraw::host_stats().cpu_texel;assert(cpu_texel==0 && s.rsx.renderer->graphics_texture_decodes>=1);repeat_draw(s);repeat_draw(s);
 assert(session.raw_misses==1 && session.raw_hits==2);
 session.batch->sync();assert(session.batch->descriptor_misses==1 && session.batch->descriptor_hits==2);
 assert(session.batch->framebuffer_misses==1 && session.batch->framebuffer_hits==2);
 assert(s.rsx.renderer->graphics_draws==3 && rsxdraw::host_stats().cpu_vertex==cpu_vp && rsxdraw::host_stats().cpu_texel==cpu_texel && rsxdraw::host_stats().cpu_clip==cpu_clip);
 session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0xff112233));
 // A shared host backing under a different EA invalidates the same resource.
 auto backing=s.memory.regions[1].bytes;s.memory.regions.push_back({0x81000000,6,backing});
 s.memory.store(0x81002000,4,0xff224466);repeat_draw(s);session.flush();assert(session.raw_misses==2 && session.raw_invalidations>=1);
 assert(near_argb(s.memory.load(0xc0100000,4),0xff224466));
 auto* retained=backing->data()+0x2000;s.rsx.graphics_session=nullptr;session.leave_fifo();
 retained[1]=0x77; // retained raw pointer; the guard must invalidate the cache.
 session.guard.disarm();s.rsx.graphics_session=&session;repeat_draw(s);session.flush();
 assert(near_argb(s.memory.load(0xc0100000,4),0xff774466));assert(session.raw_misses==3);
 repeat_draw(s);session.flush(false);
 // Inactive resident render target is a texture without a snapshot copy.
 auto image=rsxdraw::rsx_graphics_texture(s,0x100000,0,32,16,128,0x85,false);
 assert(image && image==session.surfaces.back().image);
 // An alias-compute commit bypasses the observer; it must invalidate uploads.
 auto& R=s.rsx.registers;R[0x210]=R[0x214]=0x2000;R[0xa74]=R[0xa70]=1;R[0xa6c]=0x207;
 fpword(s,0,(1u<<24)|(15u<<9)|1u);fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
 const float color[]={1,.5f,.25f,.5f};for(unsigned c=0;c<4;++c)fpword(s,4+c,rsxdraw::float_bits(color[c]));
 repeat_draw(s);R[0x210]=0x100000;R[0xa74]=R[0xa70]=0;
 fpword(s,0,(23u<<24)|(15u<<9)|(4u<<13)|1u);fpword(s,1,1u|(0xe4u<<9)|(7u<<18));
 const auto misses=session.raw_misses;repeat_draw(s);session.flush();assert(session.raw_misses==misses+1);
 assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040));
 s.rsx.graphics_session=nullptr;session.leave_fifo();
 const auto* syscall_buffer=s.memory.access(0xc0002000,16,false);int descriptors[2];assert(pipe(descriptors)==0);
 assert(::write(descriptors[1],syscall_buffer,16)==16);std::uint8_t copied[16];assert(::read(descriptors[0],copied,16)==16);close(descriptors[0]);close(descriptors[1]);
 assert(copied[0]==0x80 && copied[1]==0xff);
 std::puts("PASS raw upload cache, alias write, retained pointer and resident image");
}
static void raw_formats(){
 auto device=std::make_unique<NativeVulkan>();
 for(unsigned f:{0x81u,0x82u,0x83u,0x84u,0x85u,0x86u,0x87u,0x88u,0x8bu,0x8du,0x8eu,0x8fu,0x90u,0x91u,0x92u,0x93u,0x94u,0x95u,0x97u,0x98u,0x99u,0x9au,0x9bu,0x9cu,0x9du,0x9eu,0x9fu}){
  PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){setup_draw(*s,8,8,32,false);texture_fp(*s);auto& R=s->rsx.registers;
   R[0x1a04]=0x00010029|((f|0x20u)<<8);R[0x1a18]=0x00040004;R[0x1840]=64;R[0x1a14]=0x01010000;
   for(unsigned n=0;n<256;++n)s->memory.store(0xc0002000+n,1,(n*19+71)&255);
   // Finite positive FP encodings, including half4 and half2 pairs.
   if(f==0x9a || f==0x9b || f==0x9c || f==0x9f || f==0x91 || f==0x93)for(unsigned n=0;n<256;n+=4)s->memory.store(0xc0002000+n,4,0x38003800);
   if(f==0x94 || f==0x95 || f==0x98 || f==0x99 || f==0x9f)R[0x1a10]=0xaae4;
  }
  gpu.rsx.renderer=std::move(device);setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  assert(gpu.rsx.renderer->graphics_draws>0);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<32;++x){auto a=cpu.memory.load(0xc0100000+y*32+x,1),b=gpu.memory.load(0xc0100000+y*32+x,1);if(!near_byte(a,b))throw std::runtime_error("raw format parity f="+std::to_string(f));}
  device=std::move(gpu.rsx.renderer);std::printf("PASS raw texture format=%x\n",f);std::fflush(stdout);
 }
}

static void bc_oracles(){
 NativeVulkan vk;
 for(unsigned format:{0x87u,0x88u})for(unsigned alpha_case=0;alpha_case<(format==0x87?1u:2u);++alpha_case){
  PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;
  R[0x1a04]=0x00010029|((format|0x20u)<<8);R[0x1a18]=0x00040004;R[0x1840]=16;
  // Literal little-endian BC endpoints: 0x0421,0xe73c. All rows select
  // color indices 2,3,0,1; the normalized interpolation has fixed rational
  // values, independent of the CPU texture decoder and native BC sampler.
  const std::uint8_t color[]={0x21,0x04,0x3c,0xe7,0x4e,0x4e,0x4e,0x4e};
  for(unsigned i=0;i<8;++i)s.memory.store(0xc0002008+i,1,color[i]);
  if(format==0x87)for(unsigned i=0;i<8;++i)s.memory.store(0xc0002000+i,1,0x99);
  else{const std::uint8_t alpha[]={std::uint8_t(alpha_case?201:10),std::uint8_t(alpha_case?17:200),0x92,0x24,0x49,0x92,0x24,0x49};
   for(unsigned i=0;i<8;++i)s.memory.store(0xc0002000+i,1,alpha[i]);}
  rsxgraphics::Session session(vk,s);const auto cpu=rsxdraw::host_stats().cpu_texel;
  auto image=session.texture_image(rsxdraw::decode_texture(s,rsxdraw::Reg{s.rsx},0));assert(image->format==VK_FORMAT_R32G32B32A32_SFLOAT);
  const auto read=vk.graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,16);vk.graphics_finish(*session.batch);
  const auto* values=reinterpret_cast<const float*>(static_cast<const std::uint8_t*>(session.batch->map)+read);
  const float expected[]={28.f/93.f,123.f/189.f,30.f/93.f,format==0x87?9.f/15.f:alpha_case?1223.f/1785.f:48.f/255.f};
  for(unsigned c=0;c<4;++c)assert(std::abs(values[c]-expected[c])<0.000001f);
  assert(std::abs(values[4]-56.f/93.f)<0.000001f && std::abs(values[5]-147.f/189.f)<0.000001f && std::abs(values[6]-57.f/93.f)<0.000001f);
  assert(rsxdraw::host_stats().cpu_texel==cpu);session.flush(false);
 }
 std::puts("PASS fixed BC2/BC3 color and both alpha interpolation modes use GPU arithmetic with no native BC precision dependency");
}
static void raw_attributes(){
 auto device=std::make_unique<NativeVulkan>();
 for(unsigned type:{1u,2u,3u,4u,5u,6u,7u})for(unsigned frequency:{0u,2u}){
  PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){setup_draw(*s,16,8,64,false);texture_fp(*s);auto& R=s->rsx.registers;
   // UV attributes exercise all formats, compressed signed 11/11/10 and divisor/modulo.
   const unsigned width=type==2?4:(type==1 || type==3 || type==5)?2:1;
   R[0x1744]=(frequency<<16)|(16u<<8)|(3u<<4)|type;R[0x1fc0]=frequency?2:0;
   for(unsigned i=0;i<4;++i)for(unsigned c=0;c<3;++c)s->memory.store(0x70002100+i*16+c*width,width,type==2?rsxdraw::float_bits((i&1)?.75f:.25f):type==3?0x3800:(i&1)?0x40:0x20);
   if(type==6)for(unsigned i=0;i<4;++i)s->memory.store(0x70002100+i*16,4,0x40080080+i*0x20000);
  }
  gpu.rsx.renderer=std::move(device);setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  for(unsigned i=0;i<16*8*4;++i)assert(near_byte(cpu.memory.load(0xc0100000+i,1),gpu.memory.load(0xc0100000+i,1)));
  device=std::move(gpu.rsx.renderer);
 }
 std::puts("PASS BE raw vertex formats, stride and frequency/modulo");
}
static void raw_attribute_oracles(){
 NativeVulkan vk;
 // Each input has a fixed exact output. No CPU fetch/conversion is used.
 struct Case{unsigned type,word;std::array<float,4> expected;};
 const Case cases[]={{1,0x80007fff,{-1,1,0,1}},{2,0x3f400000,{.75f,0,0,1}},{3,0x38000000,{.5f,0,0,1}},
 {4,0x00ff0000,{0,1,0,1}},{5,0x80007fff,{-32768,32767,0,1}},{6,0x3ff,{1,0,0,1}},{7,0x20fe0000,{32,254,0,1}}};
 for(const auto& c:cases){std::vector<unsigned> words(256);words[12]=16;words[16]=(16u<<8)|((c.type==2 || c.type==3?1u:2u)<<4)|c.type;words[17]=200*4;words[19]=16;
  // Exact BE byte stream stored in the native little-endian SSBO word.
  words[200]=((c.word&255)<<24)|((c.word&0xff00)<<8)|((c.word>>8)&0xff00)|(c.word>>24);
  std::string source="#version 450\nlayout(local_size_x=1) in;layout(std430,binding=0) buffer Data{uint w[];};\nvec4 load4(uint a){return uintBitsToFloat(uvec4(w[a],w[a+1u],w[a+2u],w[a+3u]));}\n";
  // raw_vertex_glsl reads vertex bytes from the state arena (binding 37 in a draw); this
  // single-buffer oracle keeps the byte stream in w[], so state_ names the same buffer.
  source+="#define state_ w\n";source+=rsxgraphics::raw_vertex_glsl;source+="void main(){uvec4 v=floatBitsToUint(attribute_(0u,0u));for(uint i=0u;i<4u;++i)w[240u+i]=v[i];}";
  vk.compute(source,words,1);for(unsigned i=0;i<4;++i)assert(rsxdraw::bits_float(words[240+i])==c.expected[i]);
 }
 std::puts("PASS independent GPU vertex-format oracles");
}
static void indexed_and_strict(){
 for(unsigned type:{0u,1u}){PpuState s;setup_draw(s,16,8,64,false);auto& R=s.rsx.registers;
  R[0x1820]=type<<4;R[0x181c]=0x6000;R[0x173c]=1;R[0x1680]=0x80001ff0;R[0x1684]=0x800020f8;
  const unsigned ids[]={0,1,2,0,2,3};for(unsigned i=0;i<6;++i)s.memory.store(0xc0006000+i*(type?2:4),type?2:4,ids[i]);
  s.rsx.draw.primitive=5;s.rsx.draw.indexed=true;s.rsx.draw.ranges={{0,6}};
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040));
 }
 {PpuState s;setup_draw(s,32,16,128,false);std::array<std::uint8_t,64> vertices{};s.memory.transfer(0x70002000,vertices.data(),64,false);s.memory.transfer(0xc0100000,vertices.data(),64,true);s.rsx.registers[0x1680]=0x100000;
  const auto before=*s.memory.regions[1].bytes;const auto vp=rsxdraw::host_stats().cpu_vertex;
  unsetenv("ANYPS3_RSX_GPU_FALLBACK");bool stopped=false;try{rsx_execute_draw(s);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("sequential_vertex_feedback")!=std::string::npos;}
  assert(stopped && *s.memory.regions[1].bytes==before && rsxdraw::host_stats().cpu_vertex==vp);
 }
 std::puts("PASS BE16/32 index buffer, base index and explicit GPU rejection");
}
static void menu_benchmark(unsigned draws,unsigned frames){
 PpuState s;const unsigned width=960,height=544;setup_draw(s,width,height,width*16,false);texture_fp(s);auto& R=s.rsx.registers;
 s.memory.regions[1].bytes->resize(32*1024*1024);s.rsx.local_size=32*1024*1024;
 R[0x208]=(R[0x208]&~31u)|11u|(3u<<12);R[0x20c]=width*16;
 R[0x310]=1;R[0x314]=0x03020302;R[0x318]=0x03030303;R[0x320]=0x80068006;
 R[0x1a18]=0x00400040;R[0x1840]=256;
 for(unsigned i=0;i<4*64*64;++i)s.memory.store(0xc0002000+4*i,4,0x807f4020);
 // Alternative input layout: same BE vertices at a different stride/base.
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<4;++c)s.memory.store(0x70004000+i*32+c*4,4,s.memory.load(0x70002000+i*16+c*4,4));
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 // Warm up pipelines and uploads outside measured frames.
 rsx_execute_draw(s);session.flush(false);
 const auto compile0=s.rsx.renderer->compile_stats;
 const double upload0=session.batch->upload_ms,draw0=session.batch->draw_ms,download0=session.batch->download_ms,wait0=session.batch->wait_ms,pipeline0=session.batch->pipeline_ms,record0=session.batch->record_ms;
 rsxdraw::host_stats().select(rsxdraw::HostStage::idle);rsxdraw::host_stats().ns.fill(0);
 auto thread_ns=[](){timespec value{};if(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&value))throw std::runtime_error("thread CPU clock failed");return std::uint64_t(value.tv_sec)*1000000000u+value.tv_nsec;};
 double cpu_ns=0,thread_cpu_ns=0;const auto start=std::chrono::steady_clock::now();
 for(unsigned frame=0;frame<frames;++frame){const auto cpu_start=thread_ns();const auto t=std::chrono::steady_clock::now();
  for(unsigned d=0;d<draws;++d){
   const unsigned id=d+frame*draws,base=(id&1u)?0x100:0;R[0x1738]=base;R[0x1740]=(id&2u)?0x2042:0x1042;R[0x1680]=0x80000000u+((id&2u)?0x4000:0x2000)-base;R[0x1684]=0x80002100-base;
   R[0x1a00]=0x2000+(id%4)*0x4000;R[0x1a18]=(id&1u)?0x00400040:0x00200020;R[0x31c]=id*2654435761u;
   s.rsx.transform_constants[26]=rsxdraw::float_bits(width/2.f+float(id%8)*.001f);
   R[0x8c0]=(24u<<16)|((d*29)%(width-24));R[0x8c4]=(16u<<16)|((d*17)%(height-16));repeat_draw(s);}
  thread_cpu_ns+=thread_ns()-cpu_start;cpu_ns+=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-t).count();session.flush(false);
 }
 const auto seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
 const auto compile1=s.rsx.renderer->compile_stats;
 assert(compile1.shader_modules==compile0.shader_modules && compile1.graphics_pipelines==compile0.graphics_pipelines && compile1.compute_pipelines==compile0.compute_pipelines && compile1.spirv_compiles==compile0.spirv_compiles);
 std::printf("MENU_COMPILE modules=%llu graphics_pipelines=%llu compute_pipelines=%llu spirv_compiles=%llu cpu_ms_per_frame=%.3f thread_cpu_ms_per_frame=%.3f\n",(unsigned long long)(compile1.shader_modules-compile0.shader_modules),(unsigned long long)(compile1.graphics_pipelines-compile0.graphics_pipelines),(unsigned long long)(compile1.compute_pipelines-compile0.compute_pipelines),(unsigned long long)(compile1.spirv_compiles-compile0.spirv_compiles),cpu_ns/(frames*1.e6),thread_cpu_ns/(frames*1.e6));
 std::printf("MENU_BENCH draws=%u frames=%u cpu_us_per_draw=%.3f wall_fps=%.3f seconds=%.6f raw_hits=%llu raw_misses=%llu raw_upload_bytes=%llu submissions=%llu descriptor_hits=%llu descriptor_misses=%llu framebuffer_hits=%llu framebuffer_misses=%llu upload_ms=%.3f draw_ms=%.3f download_ms=%.3f wait_ms=%.3f pipeline_ms=%.3f record_ms=%.3f\n",draws,frames,cpu_ns/(double(draws)*frames*1000),frames/seconds,seconds,(unsigned long long)session.raw_hits,(unsigned long long)session.raw_misses,(unsigned long long)session.raw_bytes,(unsigned long long)session.batch->submissions,(unsigned long long)session.batch->descriptor_hits,(unsigned long long)session.batch->descriptor_misses,(unsigned long long)session.batch->framebuffer_hits,(unsigned long long)session.batch->framebuffer_misses,session.batch->upload_ms-upload0,session.batch->draw_ms-draw0,session.batch->download_ms-download0,session.batch->wait_ms-wait0,session.batch->pipeline_ms-pipeline0,session.batch->record_ms-record0);
 s.rsx.graphics_session=nullptr;
}
int main(int argc,char** argv){try{
 setenv("ANYPS3_RSX_HOST_PROFILE","1",1);
 if(argc>1 && std::string(argv[1])=="bench"){menu_benchmark(argc>2?std::stoul(argv[2]):256,argc>3?std::stoul(argv[3]):5);return 0;}
 gathered_cache();upload_cache();raw_formats();bc_oracles();raw_attributes();raw_attribute_oracles();indexed_and_strict();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class GpuUploadTests(unittest.TestCase):
    def test_raw_uploads_and_coherence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.cpp').write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1',LP_NUM_THREADS='4')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            result=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=300)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('PASS raw texture format='),27)
