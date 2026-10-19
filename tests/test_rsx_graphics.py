"""Real Vulkan graphics vs independent packed oracles and the CPU reference."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_vk import native_source as compute_source, LIBRARIES

MAIN = r'''
// Host UNORM conversion of exact half-way values is implementation-defined
// (Vulkan allows either neighbour; NVIDIA RTX 4070 SUPER yields 0x7f for 0.5,
// the CPU reference 0x80): packed 8-bit oracles accept +-1 per byte.
static bool near_argb(std::uint64_t v,std::uint32_t e,unsigned tol=1){
 for(unsigned c=0;c<4;++c){const int a=int((v>>(8*c))&255),b=int((e>>(8*c))&255);if(a-b>int(tol)||b-a>int(tol))return false;}
 return true;
}
static bool near_byte(unsigned a,unsigned b){return (a>b?a-b:b-a)<=1;}
// RGB565: each channel +-1 in its own field.
static bool near_565(std::uint64_t v,std::uint32_t e){
 const int f[3][2]={{11,31},{5,63},{0,31}};
 for(const auto& c:f){const int a=int((v>>c[0])&c[1]),b=int((e>>c[0])&c[1]);if(a-b>1||b-a>1)return false;}
 return true;
}
static void graphics_differential(){
 for(unsigned mode=0;mode<34;++mode){
  if(mode==18 || mode==28)continue; // explicit CPU fallback tested separately
  PpuState cpu,gpu;setup_draw(cpu,128,128,512,mode==33);setup_draw(gpu,128,128,512,mode==33);
  customize(cpu,mode);customize(gpu,mode);
  if(mode>=29 && mode<=31)for(auto* state:{&cpu,&gpu}){texture_fp(*state);customize(*state,mode-21);}
  if(mode>=32)for(auto* state:{&cpu,&gpu}){if(mode==32)customize(*state,22);texture_fp(*state);}
  setenv("ANYPS3_RSX_BACKEND","cpu",1);unsetenv("ANYPS3_RSX_VK_TEST");rsx_execute_draw(cpu);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  // Mode 16 needs the optional depthBounds feature; without it the explicit
  // CPU fallback draws and the differential below still compares it.
  const bool device_mode=mode!=12 && (mode!=16 || gpu.rsx.renderer->graphics_depth_bounds());
  if(device_mode && !gpu.rsx.renderer->graphics_draws)throw std::runtime_error("no HW draw mode="+std::to_string(mode));
  const unsigned aa=(cpu.rsx.registers[0x208]>>12)&15,nx=aa?2:1,ny=aa>=4?2:1;
  const auto color=gpu.rsx.registers[0x208]&31;
  const unsigned sw=128*nx,sh=128*ny,db=((gpu.rsx.registers[0x208]>>5)&7)==1?2:4;
  unsigned max_delta=0,differences=0,depth_delta=0;
  const auto ca=mode==25?0x70100000:0xc0100000;
  auto ct=rsxdraw::bind_target(cpu,mode==25?0xfeed0001:0xfeed0000,mode==25?0x100000:0x100000,cpu.rsx.registers[0x20c],sw,sh,4,(mode==22 || mode==32)?128:0,(mode==22 || mode==32)?128:0);
  auto gt=rsxdraw::bind_target(gpu,mode==25?0xfeed0001:0xfeed0000,mode==25?0x100000:0x100000,gpu.rsx.registers[0x20c],sw,sh,4,(mode==22 || mode==32)?128:0,(mode==22 || mode==32)?128:0);
  (void)ca;
  for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw;++x){
   const auto* a=ct.at(x,y);const auto* b=gt.at(x,y);
   for(unsigned c=0;c<4;++c){const unsigned d=a[c]>b[c]?a[c]-b[c]:b[c]-a[c];max_delta=std::max(max_delta,d);differences+=d!=0;}
   const auto za=cpu.memory.load(0xc0500000+y*cpu.rsx.registers[0x22c]+x*db,db),zb=gpu.memory.load(0xc0500000+y*gpu.rsx.registers[0x22c]+x*db,db);
   const unsigned da=db==2?za:za>>8,dd=db==2?zb:zb>>8;depth_delta=std::max(depth_delta,da>dd?da-dd:dd-da);
   if(db==4 && (za&255)!=(zb&255))throw std::runtime_error("stencil mismatch mode="+std::to_string(mode));
  }
  if(max_delta>2 || depth_delta>1)throw std::runtime_error("differential mode="+std::to_string(mode)+" color delta="+std::to_string(max_delta)+" depth delta="+std::to_string(depth_delta));
  if(mode==0)assert(near_argb(gpu.memory.load(0xc0100000,4),0x80ff8040));
  if(mode==1){const auto packed=gpu.memory.load(0xc0100000,4);assert(near_argb(packed&255,0x11));assert(near_argb((packed>>24)&255,0x80));}
  assert(color==8); // format coverage has separate independent packed oracles
  std::printf("GRAPHICS mode=%u color_delta=%u depth_delta=%u differing_bytes=%u\n",mode,max_delta,depth_delta,differences);
 }
}
static void graphics_formats(){
 for(unsigned f:{3u,8u,11u,12u,13u})for(unsigned samples:{0u,3u,4u,5u}){
  PpuState s;setup_draw(s,17,13,17*16,false);auto& R=s.rsx.registers;
  const unsigned cb=rsxdraw::color_bytes(f),nx=samples?2:1,ny=samples>=4?2:1;
  R[0x208]=(R[0x208]&~31u)|f|(samples<<12);R[0x20c]=17*nx*cb+cb;R[0x22c]=17*nx*4+4;
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);assert(s.rsx.renderer->graphics_draws>=1);
  const auto expected=f==3?0xfc08u:f==8?0x80ff8040u:f==11?0x3c003800u:0x3f800000u;
  {const auto got=s.memory.load(0xc0100000,f==3?2:4);assert(f==3?near_565(got,expected):f==8?near_argb(got,expected):got==expected);}
  if(f==11)assert(near_argb(s.memory.load(0xc0100004,4),0x34003800));
  for(unsigned y=0;y<13*ny;++y)assert(s.memory.load(0xc0100000+y*R[0x20c]+17*nx*cb,cb)==0);
  std::printf("FORMAT f=%u aa=%u packed=%08x\n",f,samples,expected);
 }
}
static void graphics_fallback(){
 for(unsigned reg:{0x374u,0x147cu}){PpuState s;setup_draw(s,8,8,32,false);s.rsx.registers[reg]=1;
  if(reg==0x374)s.rsx.registers[0x378]=0x1506;
  if(reg==0x380){s.rsx.registers[0x384]=0;s.rsx.registers[0x388]=0x3f800000;}
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);assert(s.rsx.renderer->graphics_draws>=1);}
 std::puts("PASS graphics logic and stipple execute on GPU");
}


static void graphics_sample_edges(){
 for(unsigned aa:{3u,4u,5u}){
  PpuState s;setup_draw(s,32,16,256,false);auto& R=s.rsx.registers;R[0x208]|=aa<<12;R[0x20c]=R[0x22c]=256;
  const float positions[3][2]={{0,0},{31.5f,0},{0,15.75f}};
  for(unsigned i=0;i<3;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002000+i*16+c*4,4,rsxdraw::float_bits(positions[i][c]));
  s.rsx.draw.primitive=5;s.rsx.draw.ranges={{0,3}};setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
  // At pixel (0,15), x+2y<31.5 gives three DIFFERENT sample masks.
  // All distances are strictly nonzero: no tolerance of an edge tie is used.
  const unsigned expected=aa==3?2u:aa==4?3u:5u,n=aa==3?2:4;
  unsigned mask=0;for(unsigned sample=0;sample<n;++sample)if(s.memory.load(0xc0100000+(15*(n==4?2:1)+sample/2)*256+(sample%2)*4,4)!=0 && near_argb(s.memory.load(0xc0100000+(15*(n==4?2:1)+sample/2)*256+(sample%2)*4,4),0x80ff8040))mask|=1u<<sample;
  if(mask!=expected)throw std::runtime_error("sample layout aa="+std::to_string(aa)+" mask="+std::to_string(mask));
 }
 std::puts("PASS independent 2x/centered4x/rotated4x sample layout edge masks");
}
static void graphics_mrt(){
 for(unsigned format:{8u,11u,9u,16u}){
  PpuState cpu,gpu;
  for(auto* state:{&cpu,&gpu}){setup_draw(*state,31,17,1024,false);auto& R=state->rsx.registers;
   R[0x208]=(R[0x208]&~31u)|format;R[0x220]=0x1f;R[0x18c]=R[0x1b4]=R[0x1b8]=0xfeed0000;
   R[0x218]=0x200000;R[0x288]=0x300000;R[0x28c]=0x400000;R[0x21c]=R[0x280]=R[0x284]=1024;R[0x370]=0xffff;
   R[0x310]=1;R[0x36c]=10;R[0x314]=0x03020302;R[0x318]=0x03030303;R[0x320]=0x80068006;
   const unsigned outs[]={0,2,3,4};for(unsigned i=0;i<4;++i){const unsigned at=i*8;
    fpword(*state,at,(1u<<24)|(outs[i]<<1)|(15u<<9)|(i==3?1:0));fpword(*state,at+1,2u|(0xe4u<<9)|(7u<<18));fpword(*state,at+2,0);fpword(*state,at+3,0);
    const float color[]={float(i+1)/4,.5f,.25f,.5f};for(unsigned c=0;c<4;++c)fpword(*state,at+4+c,rsxdraw::float_bits(color[c]));}}
  setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  for(unsigned t=0;t<4;++t){const unsigned address=0xc0100000+t*0x100000,bpp=rsxdraw::color_bytes(format);
   for(unsigned y=0;y<17;++y)for(unsigned x=0;x<31*bpp;++x){const auto c=cpu.memory.load(address+y*1024+x,1),g=gpu.memory.load(address+y*1024+x,1);assert(format==8?near_byte(c,g):c==g);}
   if(format==8){
    const unsigned expected[]={0x40402010,0x40804020,0x80bf8040,0x40802010};
    // Per-target independent blend enable is asserted by the differential
    // comparison; a fixed non-blended output provides a separate packed oracle.
    if(t==2)assert(near_argb(gpu.memory.load(address,4),expected[t]));}
  }
 }
 std::puts("PASS four independent MRT outputs and blend states, UNORM/fp16");
}
static void graphics_coherence(){
 PpuState s;setup_draw(s,32,16,128,false);auto& R=s.rsx.registers;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);
 auto retained=s.memory.range(0xc0100000,8,false);auto* raw=retained.parts[0].data;
 s.rsx.graphics_session=&session;s.rsx.graphics_clear=[&](unsigned mask){return rsxgraphics::clear(session,mask);};
 s.memory.host_access_observer=[&](const void* p,std::uint64_t n,bool write){session.host_access(p,n,write);};
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
 assert(session.batch->download_bytes==4 && raw[0]==0);
 // API read commits before publishing BE data, retained Range remains live.
 std::array<std::uint8_t,8> bytes{};retained.read(0,bytes.data(),8);assert(near_byte(bytes[0],0x80) && bytes[1]==0xff);
 auto token=s.memory.reservation_token(0xc0100000,4);
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);std::uint8_t partial=0x22;s.memory.transfer(0xc0100001,&partial,1,true);
 assert(near_argb(s.memory.load(0xc0100000,4),0x80228040));assert(s.memory.reservation_token(0xc0100000,4).second!=token.second);
 // Aliased guest addresses resolve to the same host owner.
 auto backing=s.memory.regions[1].bytes;s.memory.regions.push_back({0x80000000,6,backing});
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);assert(near_argb(s.memory.load(0x80100000,4),0x80ff8040));
 s.memory.store(0x80100000,1,0x11);assert(near_argb(s.memory.load(0xc0100000,4),0x11ff8040));
 // GPU clear obeys independent component and stencil masks, ignores draw blend.
 R[0x8c0]=32u<<16;R[0x8c4]=16u<<16;R[0x1d90]=0x12345678;R[0x1d8c]=0x4000007a;R[0x32c]=0x0f;
 R[0x310]=1;rsx_clear_surface(s,0x13);session.flush();
 assert(near_argb(s.memory.load(0xc0100000,4),0x11348040));assert(near_argb(s.memory.load(0xc0500000,4),0x4000000a));
 R[0x310]=0;R[0x1d90]=0x80706050;rsx_clear_surface(s,0xf0);
 // Keep both source/destination resident for direct memory and image copies.
 R[0x210]=0x200000;R[0x1d90]=0xff010203;rsx_clear_surface(s,0xf0);
 const auto downloads=session.batch->download_bytes;
 R[0x2184]=R[0x2188]=0xfeed0000;R[0x230c]=0x100000;R[0x2310]=0x200000;R[0x2314]=R[0x2318]=128;
 R[0x231c]=128;R[0x2320]=16;R[0x2324]=0x101;rsxblit::copy(s,0);
 assert(session.batch->download_bytes==downloads);session.flush();assert(near_argb(s.memory.load(0xc0200000,4),0x80706050));
 // NV3089 identity copy has its own register protocol and GPU branch.
 R[0xc30c]=R[0xc314]=(16u<<16)|32;R[0xc304]=3;R[0xc2fc]=1;R[0xc300]=3;R[0xc400]=(16u<<16)|32;
 R[0xc404]=(2u<<16)|128;R[0xc184]=0xfeed0000;R[0xc408]=0x100000;R[0xc310]=R[0xc308]=0;
 R[0xc318]=R[0xc31c]=1u<<20;R[0xc198]=0x313371c3;R[0x6300]=10;R[0x6304]=128u<<16;R[0x6188]=0xfeed0000;R[0x630c]=0x200000;
 const auto copy_downloads=session.batch->download_bytes;rsxblit::scaled(s,0);assert(session.batch->download_bytes==copy_downloads);
 // A CPU fallback must see every earlier GPU write, then invalidate ownership.
 R[0x374]=1;R[0x378]=0x1506;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);
 assert(near_argb(s.memory.load(0xc0200000,4),0x008fe010));assert(session.surfaces.empty());R[0x374]=0;
 // A resident surface is sampled through a GPU snapshot, without readback.
 R[0x210]=0x100000;R[0x1d90]=0x80604020;rsx_clear_surface(s,0xf0);
 texture_fp(s);R[0x1a00]=0x100000;R[0x1a18]=(32u<<16)|16;R[0x1840]=128;
 R[0x210]=0x300000;s.rsx.draw.ranges={{0,4}};const auto before=session.batch->download_bytes;rsx_execute_draw(s);
 assert(session.batch->download_bytes==before+4);session.flush();assert(near_argb(s.memory.load(0xc0300000,4),0x80604020));
 // Exception leaves neither a stale observer guard nor committed invalid data.
 s.memory.host_access_observer=[](const void*,std::uint64_t,bool){throw std::runtime_error("expected observer fault");};
 bool failed=false;try{s.memory.load(0xc0300000,4);}catch(const std::runtime_error&){failed=true;}assert(failed && !s.memory.in_host_access_observer);
 s.memory.host_access_observer={};session.flush();s.rsx.graphics_session=nullptr;s.rsx.graphics_clear={};
 assert(near_byte(raw[0],0x80)); // raw pointers are current at the CPU/SPU ownership boundary
 std::puts("PASS graphics coherence, DMA, retained Range, alias, reservation, GPU clear/copy/texture, exception");
}





static void graphics_vertex_feedback_fallback(){
 PpuState cpu,gpu;
 for(auto* s:{&cpu,&gpu}){setup_draw(*s,32,16,128,false);
  std::array<std::uint8_t,64> positions{};s->memory.transfer(0x70002000,positions.data(),positions.size(),false);
  s->memory.transfer(0xc0100000,positions.data(),positions.size(),true);s->rsx.registers[0x1680]=0x100000;}
 auto run=[](PpuState& s,const char* backend){setenv("ANYPS3_RSX_BACKEND",backend,1);std::string stop;
  try{rsx_execute_draw(s);}catch(const std::runtime_error& e){stop=e.what();}return stop;};
 const auto a=run(cpu,"cpu"),b=run(gpu,"gpu");assert(a==b);assert(gpu.rsx.renderer && gpu.rsx.renderer->graphics_draws==0);
 assert(*cpu.memory.regions[1].bytes==*gpu.memory.regions[1].bytes);
 assert(cpu.rsx.draw.pixels==gpu.rsx.draw.pixels && cpu.rsx.draw.triangles==gpu.rsx.draw.triangles);
 std::puts("PASS vertex/output alias keeps sequential primitive feedback and exact CPU committed prefix/stop");
}

static void graphics_fragment_feedback_fallback(){
 PpuState cpu,gpu;
 for(auto* s:{&cpu,&gpu}){setup_draw(*s,32,16,128,false);texture_fp(*s);
  std::array<std::uint8_t,16> pixels{};s->memory.transfer(0xc0002000,pixels.data(),pixels.size(),false);
  s->memory.transfer(0xc0100000,pixels.data(),pixels.size(),true);s->rsx.registers[0x1a00]=0x100000;}
 setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
 assert(gpu.rsx.renderer && gpu.rsx.renderer->graphics_draws==0);
 assert(*cpu.memory.regions[1].bytes==*gpu.memory.regions[1].bytes);assert(cpu.rsx.draw.pixels==gpu.rsx.draw.pixels);
 std::puts("PASS fragment/output alias keeps sequential texture feedback and exact CPU bytes");
}
static void graphics_dynamic_constants(){
 PpuState cpu,gpu;
 for(auto* s:{&cpu,&gpu}){setup_draw(*s,64,32,256,false);
  for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c){const auto at=0x70002000+i*16+c*4;
   const float value=rsxdraw::bits_float(s->memory.load(at,4))*.5f+(c?8.f:16.f);s->memory.store(at,4,rsxdraw::float_bits(value));}}
 std::size_t pipelines=0;
 for(unsigned stage=0;stage<4;++stage){
  for(auto* s:{&cpu,&gpu}){for(unsigned y=0;y<32;++y)for(unsigned x=0;x<64;++x)s->memory.store(0xc0100000+y*256+x*4,4,0);
   if(stage>=1){const float color[]={.25f,.75f,.125f,1};for(unsigned c=0;c<4;++c)fpword(*s,4+c,rsxdraw::float_bits(color[c]));}
   if(stage>=2)s->rsx.transform_constants[24]=rsxdraw::float_bits(33.f);
   if(stage>=3)s->rsx.registers[0xa30]=rsxdraw::float_bits(24.f);
   s->rsx.draw.ranges={{0,4}};}
  setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  const auto size=gpu.rsx.renderer->graphics_cache_size();if(stage==0)pipelines=size;else assert(size==pipelines);
  for(unsigned y=0;y<32;++y)for(unsigned x=0;x<64;++x)assert(near_argb(cpu.memory.load(0xc0100000+y*256+x*4,4),std::uint32_t(gpu.memory.load(0xc0100000+y*256+x*4,4))));
  assert(near_argb(gpu.memory.load(0xc0100000+16*256+32*4,4),stage?0xff40bf20:0x80ff8040));
 }
 std::puts("PASS dynamic FP constants, VP matrix and viewport change image without pipeline-cache miss");
}
static void graphics_texture_layout_reinterpret(){
 PpuState s;setup_draw(s,64,16,256,false);auto& R=s.rsx.registers;
 R[0x208]=(6u<<24)|(6u<<16)|0x248; // visible 64x16 inside Morton storage 64x64
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;
 s.memory.host_access_observer=[&](const void* p,std::uint64_t n,bool write){session.host_access(p,n,write);};
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
 texture_fp(s);R[0x1a00]=0x100000;R[0x1a04]=0x00010029|(0x85u<<8);R[0x1a18]=(64u<<16)|16;
 const auto texture=rsxdraw::decode_texture(s,rsxdraw::Reg{s.rsx},0);
 // A 64x16 Morton texture has different high-bit interleave from the 64x64
 // render target. Exact physical oracles, not a duplicate swizzle algorithm:
 assert(!texture.resident_image);assert(s.memory.load(0xc0100800,4)==0);assert(near_argb(s.memory.load(0xc0101000,4),0x80ff8040));
 assert(rsxdraw::texel(texture,texture.levels[0],0,0)[0]==1);assert(rsxdraw::texel(texture,texture.levels[0],32,0)[0]==0);
 session.flush();s.memory.host_access_observer={};s.rsx.graphics_session=nullptr;
 std::puts("PASS incompatible Morton image/texture storage dimensions synchronize and decode physical bytes");
}
static void graphics_depth_compare(){
 const bool expected[]={false,false,true,true,false,false,true,true};
 for(unsigned df:{1u,2u})for(unsigned func=0;func<8;++func){PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;
  R[0x208]=(R[0x208]&~0xe0u)|(df<<5);R[0xa74]=1;R[0xa70]=1;R[0xa6c]=0x200+func;
  const unsigned bpp=df==1?2:4,old=df==1?0x4000u:0x4000005au;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)s.memory.store(0xc0500000+y*32+x*bpp,bpp,old);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
  const auto color=s.memory.load(0xc0100000,4);
  if(near_argb(color,0x80ff8040)!=expected[func])throw std::runtime_error("depth compare df="+std::to_string(df)+" func="+std::to_string(func));
  assert(s.memory.load(0xc0500000,bpp)==old);
 }
 std::puts("PASS all eight depth comparisons with exact quantized Z16/Z24 equality and stencil preservation");
}
[[maybe_unused]] static void graphics_descriptor_budget(){
 PpuState s;setup_draw(s,1,1,4,false);s.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.submit_draws=0;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 // A descriptor set reserves ALL layout bindings, including unused samplers.
 // 850 sets exceed the old 16384 sampler pool with 20 samplers/set.
 for(unsigned i=0;i<850;++i){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
 assert(s.rsx.renderer->graphics_draws==850);assert(session.batch->submissions==0);
 assert(session.batch->visibility_count==850);session.flush();s.rsx.graphics_session=nullptr;
 assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040));
 std::puts("PASS 850 graphics draw descriptor sets in one unsubmitted batch");
}
// NV4097_SET_ANTI_ALIASING_CONTROL 0x1d7c (bit 4 alpha-to-coverage, bit 8 alpha-to-one, bits 16-31 sample
// mask). setup_draw writes ARGB 0x80ff8040 (alpha exactly 0.5) over the whole surface. Sample s of pixel
// (x,y) is stored at (2x+s%2, y*ny+s/2) (graphics_sample_edges).
static void graphics_aa_control(){
 auto run=[](unsigned aa,unsigned control,unsigned size){
  auto s=std::make_unique<PpuState>();const unsigned nx=aa?2:1;
  setup_draw(*s,size,size,size*nx*4,false);auto& R=s->rsx.registers;R[0x208]|=aa<<12;R[0x1d7c]=control;
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(*s);assert(s->rsx.renderer->graphics_draws==1);return s;};
 auto sample=[](PpuState& s,unsigned size,unsigned ny,unsigned x,unsigned y,unsigned n){
  return s.memory.load(0xc0100000+(y*ny+n/2)*size*2*4+(2*x+n%2)*4,4);};
 // 4 samples, alpha 0.5: exactly two covered samples per pixel (independent of the dither offset),
 // and with this ordered pattern always samples 0 and 1.
 {auto s=run(4,0xffff0010,8);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)for(unsigned n=0;n<4;++n){const auto v=sample(*s,8,2,x,y,n);
   if(n<2?!near_argb(v,0x80ff8040):v!=0)throw std::runtime_error("a2c 4x sample "+std::to_string(n)+" value "+std::to_string(v));}}
 // Sample mask 0x0005: samples 0 and 2 only, no coverage change from alpha.
 {auto s=run(4,0x00050000,8);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)for(unsigned n=0;n<4;++n){const auto v=sample(*s,8,2,x,y,n);
   if(n%2==0?!near_argb(v,0x80ff8040):v!=0)throw std::runtime_error("sample mask sample "+std::to_string(n));}}
 // One sample per pixel, alpha 0.5: half of every 4x4 block is covered.
 {auto s=run(0,0xffff0010,8);
  for(unsigned by=0;by<8;by+=4)for(unsigned bx=0;bx<8;bx+=4){unsigned covered=0;
   for(unsigned y=by;y<by+4;++y)for(unsigned x=bx;x<bx+4;++x){const auto v=s->memory.load(0xc0100000+y*32+x*4,4);
    if(v){assert(near_argb(v,0x80ff8040));++covered;}}
   if(covered!=8)throw std::runtime_error("a2c 1x coverage "+std::to_string(covered));}}
 // Alpha-to-one: stored alpha 0xff, colour unchanged; alpha-to-coverage disabled so all pixels are written.
 {auto s=run(0,0xffff0100,4);
  for(unsigned i=0;i<16;++i)if(!near_argb(s->memory.load(0xc0100000+i*4,4),0xffff8040))throw std::runtime_error("a2one");}
 // The CPU renderer has no 0x1d7c model: an explicit stop, not a silent draw.
 {PpuState s;setup_draw(s,4,4,16,false);s.rsx.registers[0x1d7c]=0xffff0010;setenv("ANYPS3_RSX_BACKEND","cpu",1);
  bool stopped=false;try{rsx_execute_draw(s);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("CPU renderer")!=std::string::npos;}
  assert(stopped);}
 std::puts("PASS anti-aliasing control: alpha-to-coverage 4x/1x, sample mask, alpha-to-one, CPU stop");
}
// A fragment program sampling a disabled unit reads transparent black (RPCS3 null image): TEX/TXP/TXL/TXB
// all produce ARGB 0x00000000 over a pre-filled sentinel; the CPU renderer stops explicitly.
static void graphics_null_texture(){
 for(unsigned opcode:{23u,24u,47u,49u}){
  PpuState s;setup_draw(s,8,8,32,false);texture_fp(s,opcode);s.rsx.registers[0x1a0c]=0;
  for(unsigned i=0;i<64;++i)s.memory.store(0xc0100000+i*4,4,0x12345678);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);assert(s.rsx.renderer->graphics_draws==1);
  for(unsigned i=0;i<64;++i)if(s.memory.load(0xc0100000+i*4,4)!=0)throw std::runtime_error("null texture opcode "+std::to_string(opcode));
 }
 {PpuState s;setup_draw(s,4,4,16,false);texture_fp(s);s.rsx.registers[0x1a0c]=0;setenv("ANYPS3_RSX_BACKEND","cpu",1);
  bool stopped=false;try{rsx_execute_draw(s);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("disabled texture unit")!=std::string::npos;}
  assert(stopped);}
 std::puts("PASS disabled texture unit samples transparent black on GPU, CPU stop");
}
// A triangle with a NaN/Inf vertex position draws nothing; the other triangle of the quad is unaffected.
// Independent oracle: the same quad with vertex 1 collapsed onto vertex 0 (a zero-area triangle covers no pixel).
static void graphics_nonfinite_position(){
 auto image=[](const char* backend,unsigned component,std::uint32_t value){
  auto s=std::make_unique<PpuState>();setup_draw(*s,8,8,32,false);
  for(unsigned i=0;i<64;++i)s->memory.store(0xc0100000+i*4,4,0x12345678);
  s->memory.store(0x70002000+16+component*4,4,value);
  setenv("ANYPS3_RSX_BACKEND",backend,1);rsx_execute_draw(*s);
  if(std::string(backend)=="gpu" && s->rsx.renderer->graphics_draws!=1)throw std::runtime_error("non-finite test: no HW draw");
  std::vector<std::uint32_t> pixels(64);for(unsigned i=0;i<64;++i)pixels[i]=s->memory.load(0xc0100000+i*4,4);return pixels;};
 for(const char* backend:{"gpu","cpu"}){
  const auto expected=image(backend,0,0);unsigned drawn=0;for(auto v:expected)drawn+=v!=0x12345678;
  if(drawn<28 || drawn>36)throw std::runtime_error(std::string("non-finite oracle coverage ")+backend+" "+std::to_string(drawn));
  for(std::uint32_t bad:{0x7fc00000u,0xffc00001u,0x7f800000u,0xff800000u})for(unsigned c:{0u,1u})
   if(image(backend,c,bad)!=expected)throw std::runtime_error(std::string("non-finite position ")+backend+" component "+std::to_string(c)+" bits "+std::to_string(bad));
 }
 std::puts("PASS non-finite vertex position discards only its triangle on GPU and CPU");
}
static void graphics_fifo(){
 PpuState s;setup_draw(s,32,16,128,false);auto& gpu=s.rsx;auto& R=gpu.registers;auto& m=s.memory;
 m.map(0x60000000,0x1000,6,nullptr,0);m.map(0x80000000,0x10000,6,nullptr,0);
 gpu.dma=0x60000000;gpu.reports=0x80000000;gpu.fifo_enabled=true;gpu.draw.primitive=0;gpu.draw.ranges.clear();
 R[0x17cc]=1;R[0x1a8]=0x66626660;
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<2;++i){packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);}
 packet(0x1800,1u<<24); // GET_REPORT publishes both preceding draws
 m.store(gpu.dma+0x44,4,0x100);m.store(gpu.dma+0x40,4,put);
 auto* retained_raw=m.access(0xc0100000,4,false);unsetenv("ANYPS3_RSX_BACKEND");
 assert(rsx_run(s,1024));assert(gpu.renderer->graphics_draws==2);assert(gpu.graphics_session==nullptr);
 assert(m.host_access_observer);assert(near_byte(retained_raw[0],0x80) && retained_raw[1]==0xff);
 assert(m.load(gpu.reports+0x1408,4)==1024);assert(gpu.draw.pixels==1024);assert(gpu.report_values[0]==1024);
 // Next scope reloads clean images: raw mmapper/backing writes cannot leave a
 // stale image between CPU turns. GPU state is never dirty outside rsx_run.
 retained_raw[0]=0x11;gpu.registers[0x324]=0;put=0x200;
 packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);
 m.store(gpu.dma+0x44,4,0x200);m.store(gpu.dma+0x40,4,put);assert(rsx_run(s,1024));assert(retained_raw[0]==0x11);
 // Draw then protocol error: valid queued output is completed, the unknown
 // command still stops and the observer/session is restored on unwind.
 R[0x324]=0x01010101;put=0x300;packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);packet(0,0xdeadbeef);
 m.store(gpu.dma+0x44,4,0x300);m.store(gpu.dma+0x40,4,put);bool stopped=false;
 try{rsx_run(s,1024);}catch(const std::runtime_error&){stopped=true;}
 assert(stopped && !gpu.graphics_session && m.host_access_observer && near_byte(retained_raw[0],0x80));
 std::puts("PASS production FIFO GPU batching, reports, retained raw backing, scope reload and error unwind");
}
int main(int argc,char** argv){try{
 setenv("ANYPS3_RSX_GPU_FALLBACK","1",1);
 if(argc>1 && std::string(argv[1])=="probe"){NativeVulkan vk;return 0;}
 graphics_differential();graphics_formats();graphics_fallback();graphics_coherence();graphics_sample_edges();graphics_mrt();graphics_fifo();graphics_depth_compare();graphics_texture_layout_reinterpret();graphics_dynamic_constants();graphics_vertex_feedback_fallback();graphics_fragment_feedback_fallback();graphics_descriptor_budget();graphics_aa_control();graphics_null_texture();graphics_nonfinite_position();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    # Preserve the existing compute-only regression harness separately.
    source=compute_source().split('static void differential(){')[0]
    source=source.replace('backend?backend:"cpu"','backend?backend:"gpu"')
    return source + MAIN

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class GraphicsTests(unittest.TestCase):
    def test_graphics_differential_formats_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.cpp').write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            result=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('Validation Error',result.stdout+result.stderr)
            self.assertNotIn('Validation Warning',result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('FORMAT f='),20)
            self.assertIn('PASS non-finite vertex position discards only its triangle',result.stdout)
