"""Independent sparse-index and immutable graphics-state invalidation oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void sparse_indices_(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 for(unsigned type:{0u,1u}){
  PpuState s;setup_draw(s,8,8,32,false);auto& r=s.rsx.registers;
  // The four corners live at nonconsecutive indices; unused vertices are
  // deliberately outside the viewport. No CPU renderer supplies the oracle.
  const unsigned ids[]={9,11,15,19};
  for(unsigned i=0;i<20;++i)for(unsigned c=0;c<4;++c)s.memory.store(0x70004000+i*16+c*4,4,rsxdraw::float_bits(c<2?-100.f:c==2?.25f:1.f));
  for(unsigned i=0;i<4;++i)for(unsigned c=0;c<4;++c)s.memory.store(0x70004000+ids[i]*16+c*4,4,s.memory.load(0x70002000+i*16+c*4,4));
  r[0x1ff0]=1;r[0x1680]=0x80004000;r[0x1820]=type<<4;r[0x181c]=0x6000;r[0x173c]=3;
  const unsigned sequence[]={6,8,12,6,12,16};
  for(unsigned i=0;i<6;++i)s.memory.store(0xc0006000+i*(type?2:4),type?2:4,sequence[i]);
  s.rsx.draw.primitive=5;s.rsx.draw.indexed=true;s.rsx.draw.ranges={{0,6}};
  s.rsx.renderer=std::make_unique<NativeVulkan>();assert(s.rsx.renderer->graphics_vertex_fetch());
  rsx_execute_draw(s);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(near_argb(s.memory.load(0xc0100000+y*32+x*4,4),0x80ff8040));
 }
 std::puts("PASS fixed sparse BE16/32 indices with nonzero base and complete viewport coverage");
}

static void state_blocks_(){
 PpuState s;setup_draw(s,8,8,32,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 auto& r=s.rsx.registers;std::vector<unsigned> data;
 auto fp=rsxdraw::load_fragment_program(s,rsxdraw::Reg{s.rsx});
 auto bind=[&]{session.serialize_draw_registers(data,8,8);session.refresh_vertex_constants();session.refresh_fragment_constants(fp);session.bind_draw_blocks(data,true);};
 auto word=[&](unsigned at){unsigned v;std::memcpy(&v,static_cast<const char*>(session.batch->map)+4*at,4);return v;};
 // 32 register blocks, 15 VP constant chunks (32 vec4 each), the written-mask
 // block, the FP constant block and the per-draw chunk table (data[2]).
 auto vc=[&](unsigned at){return word(word(data[2]+at/128)+at%128);};auto vmask=[&](unsigned n){return word(word(data[2]+15)+n);};
 bind();assert(data.size()==560 && data[3]==560 && session.batch->state_block_uploads==50);
 assert(word(data[16+0x308/256]+(0x308/4)%64)==0x207);
 assert(word(data[16+31]+(0x1ff4/4)%64)==0x3fffff);
 assert(vc(24)==0x40800000 && vmask(6)==15);
 const auto old_alpha=data[16+0x308/256]+(0x308/4)%64,old_vp=word(data[2])+24;
 bind();assert(session.batch->state_block_uploads==50 && session.batch->state_block_hits==50);
 auto& alpha=r[0x308];alpha=0x201;bind();assert(session.batch->state_block_uploads==51);
 assert(word(data[16+0x308/256]+(0x308/4)%64)==0x201 && word(old_alpha)==0x207);
 alpha=0x202;bind();assert(session.batch->state_block_uploads==52 && word(data[16+0x308/256]+(0x308/4)%64)==0x202);
 r.erase(0x308);bind();assert(session.batch->state_block_uploads==53 && word(data[16+0x308/256]+(0x308/4)%64)==0x207);
 // A constant write re-uploads exactly its chunk and the chunk table.
 auto* constants=s.rsx.transform_constants.data();constants[24]=0x80000000;bind();assert(session.batch->state_block_uploads==55 && vc(24)==0x80000000 && word(old_vp)==0x40800000);
 assert(vc(1000)==s.rsx.transform_constants[1000]);
 constants[24]=0x7fc12345;bind();assert(session.batch->state_block_uploads==57 && vc(24)==0x7fc12345);
 constants[1871]=0x12345678;bind();assert(session.batch->state_block_uploads==59 && vc(1871)==0x12345678 && vc(24)==0x7fc12345);
 auto* written=s.rsx.transform_constants_written.data();written[24]=false;bind();assert(session.batch->state_block_uploads==61 && vmask(6)==14);
 fp[0].constant[1]=rsxdraw::bits_float(0x80000000);bind();assert(session.batch->state_block_uploads==62 && word(data[14]+1)==0x80000000);
 fp[0].constant[1]=rsxdraw::bits_float(0x7fc54321);bind();assert(session.batch->state_block_uploads==63 && word(data[14]+1)==0x7fc54321);
 r[0x1ff4]=0;bind();assert(session.batch->state_block_uploads==64 && word(data[47]+(0x1ff4/4)%64)==0);
 r.erase(0x1ff4);bind();assert(session.batch->state_block_uploads==65 && word(data[47]+(0x1ff4/4)%64)==0x3fffff);
 vk.graphics_finish(*session.batch);vk.graphics_reset(*session.batch);bind();assert(session.batch->state_block_uploads==115);
 vk.graphics_finish(*session.batch);
 // A texture-layout relocation must not rewrite the numerically equal FP
 // constant offset at instruction 516. Check the generated consumer address.
 std::vector<rsxdraw::FpInst> long_fp(517);
 for(auto& i:long_fp){i.d=(1u<<24)|(15u<<9);i.s0=2u|(0xe4u<<9)|(7u<<18);i.has_constant=true;}
 long_fp.back().d|=1;
 const auto shader=rsxgraphics::fragment_source(long_fp,rsxdraw::Reg{s.rsx},1,{});
 assert(shader && shader->find("state4(w[14]+2064u)")!=std::string::npos);
 std::puts("PASS immutable register/VP/FP blocks, exact bits, retained references, defaults and fence reset");
}
static void queued_constants_(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);PpuState s;setup_draw(s,8,8,32,false);auto& r=s.rsx.registers;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 auto color=[&](std::array<float,4> c){for(unsigned i=0;i<4;++i){unsigned v=rsxdraw::float_bits(c[i]);s.memory.store(0xc0001010+4*i,4,(v<<16)|(v>>16));}};
 r[0x8c0]=4u<<16;r[0x8c4]=8u<<16;color({1,0,0,1});rsx_execute_draw(s);
 const auto source=s.rsx.renderer->compile_stats.graphics_pipelines;
 r[0x8c0]=(4u<<16)|4;color({0,0,1,1});s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);
 assert(s.rsx.renderer->compile_stats.graphics_pipelines==source);session.flush(true);
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(s.memory.load(0xc0100000+y*32+4*x,4)==(x<4?0xffff0000u:0xff0000ffu));
 s.rsx.graphics_session=nullptr;
 std::puts("PASS queued draws keep independent exact red/blue constants without pipeline recompilation");
}
static void inverse_upload_(){
 PpuState s;setup_draw(s,4,4,16,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 // Fixed Morton order for a 4x4 surface. The expected logical positions are
 // constants independent of Target::address and the inverse upload algorithm.
 const unsigned offsets[]={0,4,16,20,8,12,24,28,32,36,48,52,40,44,56,60};
 for(unsigned i=0;i<16;++i)s.memory.store(0xc0100000+4*i,4,0x11220000u+i);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,16,4,4,4,4,4);
 auto& surface=session.bind(target,4,4,1,8,false);std::array<std::uint8_t,64> bytes{};rsxgraphics::load_pixels(surface,bytes.data(),nullptr);
 for(unsigned i=0;i<16;++i){assert(bytes[4*i]==0x22 && bytes[4*i+1]==0 && bytes[4*i+2]==offsets[i]/4 && bytes[4*i+3]==0x11);}
 const auto gpu_at=vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*session.batch);
 for(unsigned i=0;i<16;++i){const auto* gpu=static_cast<const std::uint8_t*>(session.batch->map)+gpu_at+4*i;assert(gpu[0]==0x22 && gpu[1]==0 && gpu[2]==offsets[i]/4 && gpu[3]==0x11);}
 // The layout survives a CPU alias write, its pixels must never be cached.
 s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});s.memory.store(0x81100010,4,0xaabbccdd);
 rsxgraphics::load_pixels(surface,bytes.data(),nullptr);assert(bytes[8]==0xbb && bytes[9]==0xcc && bytes[10]==0xdd && bytes[11]==0xaa);
 session.flush(false);s.memory.enable_concurrency();
 // An old layout may not authorize reads after the original mapping loses rights.
 {GuestMemory::RawPermit raw(s.memory);s.memory.regions[1].flags=2;s.memory.mapping_changed();}
 bool stopped=false;try{rsxgraphics::load_pixels(surface,bytes.data(),nullptr);}catch(const std::runtime_error&){stopped=true;}
 assert(stopped);
 std::puts("PASS fixed Morton inverse upload, changed alias pixels and stale original map rights");
}

static void image_pool_(){
 PpuState s;setup_draw(s,4,4,16,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 auto first=session.surface_image(VK_FORMAT_R8G8B8A8_UNORM,4,4,1);const auto* identity=first.get();auto held=first;
 NativeVulkan::GraphicsState fixed{};fixed.width=fixed.height=4;fixed.colors=1;fixed.color=VK_FORMAT_R8G8B8A8_UNORM;fixed.scissor={{0,0},{4,4}};fixed.blend[0].colorWriteMask=15;
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> colors{};colors[0]=first;
 const std::array<std::array<float,2>,4> positions{};
 vk.graphics_draw(*session.batch,fixed,colors,{},"#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,0,1);}","#version 450\nlayout(location=0) out vec4 c;void main(){c=vec4(1,0,0,1);}",std::vector<unsigned>(16),3,positions);
 first.reset();colors={};auto second=session.surface_image(VK_FORMAT_R8G8B8A8_UNORM,4,4,1);assert(second.get()!=identity && session.surface_image_hits==0);
 vk.graphics_finish(*session.batch);vk.graphics_reset(*session.batch);second.reset();
 auto third=session.surface_image(VK_FORMAT_R8G8B8A8_UNORM,4,4,1);assert(third.get()!=identity && session.surface_image_hits==1);
 // The held RTT/texture identity prevents reuse after completion too.
 held.reset();auto reused=session.surface_image(VK_FORMAT_R8G8B8A8_UNORM,4,4,1);assert(reused.get()==identity && session.surface_image_hits==2);
 assert(session.surface_images.size()==2 && session.surface_image_bytes<=64*1024*1024);
 vk.graphics_finish(*session.batch);
 std::puts("PASS surface image pool retains active commands and texture identities until actual release");
}
static void upload_bits_(){
 for(unsigned samples:{1u,2u,4u})for(unsigned format:{8u,11u,12u,13u,14u,102u}){
  const bool depth=format==102;const unsigned f=depth?2:format,bpp=format==11?8:format==12?16:4,nx=samples>1?2:1,ny=samples==4?2:1,pitch=2*nx*bpp;
  PpuState s;setup_draw(s,2,2,pitch,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
  auto source=[&](unsigned pixel,unsigned lane){return depth?0x12340070u+pixel*0x101u:format==8?0x11223340u+pixel:format==11?0x80007e01u+pixel:0x80000000u+pixel*0x100u+lane;};
  for(unsigned y=0;y<2*ny;++y)for(unsigned x=0;x<2*nx;++x)for(unsigned lane=0;lane<bpp/4;++lane)s.memory.store(0xc0100000+y*pitch+x*bpp+4*lane,4,source(y*2*nx+x,lane));
  auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,pitch,2*nx,2*ny,bpp);
  auto& surface=session.bind(target,2,2,samples,f,depth);assert(vk.graphics_surface_uploads==1);
  const auto at=vk.graphics_transfer(*session.batch,surface.image,depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,bpp);
  const auto st=depth?vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_STENCIL_BIT,1):0;vk.graphics_finish(*session.batch);
  for(unsigned layer=0;layer<samples;++layer)for(unsigned y=0;y<2;++y)for(unsigned x=0;x<2;++x){const unsigned pixel=((y*ny+layer/nx)*2*nx+x*nx+layer%nx),out=(layer*2+y)*2+x;
   for(unsigned lane=0;lane<bpp/4;++lane){const unsigned v=source(pixel,lane);unsigned expected=depth?v>>8:format==8?((v&255u)<<16)|((v>>16)&255u)|((v>>24)<<24)|(v&0xff00u):format==11?(v>>16)|(v<<16):v,actual;
    std::memcpy(&actual,static_cast<const char*>(session.batch->map)+at+out*bpp+4*lane,4);assert(depth?(actual&0xffffffu)==expected:actual==expected);}
   if(depth)assert(*(static_cast<const std::uint8_t*>(session.batch->map)+st+out)==(source(pixel,0)&255u));
  }
 }
 std::puts("PASS fixed GPU inverse upload RGBA/fp16/fp32/packed/depth-stencil bits and 1/2/4 sample planes");
}

static void device_sequence_(){
 NativeVulkan vk;
 const std::vector<std::vector<unsigned>> expected{{17,18,31,32,33,34},{17,18,31,31,18,32,31,32,33,33,32,34},{17,18,31,17,31,32,17,32,33,17,33,34},{17,18,31,17,31,32},{17,18,32,17,32,31,31,32,34,31,34,33},{17,18,31,17,31,32,17,32,33,17,33,34}};
 for(unsigned mode=5;mode<=10;++mode){
  RsxDrawState draw;draw.primitive=mode;draw.ranges={{17,2},{31,4}};rsxgraphics::DeviceGeometry geometry(draw);assert(geometry.valid && geometry.vertices==expected[mode-5].size());
  assert(geometry.bounds(1,false)==std::make_pair(17u,mode==8?32u:34u));
  assert(geometry.bounds(4,true)==std::make_pair(0u,3u));assert(geometry.bounds(12,true)==std::make_pair(5u,mode==8?8u:10u));
  assert(geometry.bounds(4,false)==std::make_pair(4u,8u));
  std::vector<unsigned> words(256);words[13]=16;words[15]=2;words[16]=mode;words[17]=2;words[18]=17;words[19]=2;words[20]=31;words[21]=4;
  std::string source="#version 450\nlayout(local_size_x=1) in;layout(std430,binding=0) buffer Data{uint w[];};\nvec4 load4(uint a){return uintBitsToFloat(uvec4(w[a],w[a+1u],w[a+2u],w[a+3u]));}\n";
  source+="const uint state_[1]=uint[1](0u);\n";  // vertex_byte() reads binding 37 in the draw shader; device_vertex_ never does
  source+=rsxgraphics::raw_vertex_glsl;source+="void main(){uint id=gl_GlobalInvocationID.x;w[128u+id]=device_vertex_(id);}";
  vk.compute(source,words,geometry.vertices);
  assert(words[7]==0);for(unsigned i=0;i<expected[mode-5].size();++i)assert(words[128+i]==expected[mode-5][i]);
 }
 RsxDrawState zero;zero.primitive=8;zero.ranges={{UINT_MAX-1,2}};rsxgraphics::DeviceGeometry empty(zero);assert(empty.valid && !empty.vertices && empty.bounds(2,true)==std::make_pair(UINT_MAX,0u));
 zero.ranges={{UINT_MAX-1,4}};assert(!rsxgraphics::DeviceGeometry(zero).valid);
 zero.primitive=6;zero.ranges={{0,UINT_MAX}};assert(!rsxgraphics::DeviceGeometry(zero).valid);
 setenv("ANYPS3_RSX_BACKEND","gpu",1);PpuState s;setup_draw(s,8,8,32,false);rsx_execute_draw(s);
 assert(s.rsx.draw.scratch_indices.empty() && s.rsx.draw.scratch_triangles.empty());
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(near_argb(s.memory.load(0xc0100000+y*32+x*4,4),0x80ff8040));
 std::puts("PASS fixed device topology for all six array primitives, odd strips, range boundaries, exact bounds and actual draw");
}
int main(){try{sparse_indices_();state_blocks_();queued_constants_();inverse_upload_();image_pool_();upload_bits_();device_sequence_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+MAIN

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class GraphicsCacheTests(unittest.TestCase):
    def test_native_state_caches(self):
        with tempfile.TemporaryDirectory(prefix='anyps3-graphics-cache-') as tmp:
            cpp=Path(tmp)/'test.cpp'; binary=Path(tmp)/'test';cpp.write_text(native_source())
            result=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4')
            result=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('PASS fixed sparse',result.stdout)
