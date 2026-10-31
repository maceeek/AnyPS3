"""RSX2: vertex-input grouping and device-memory recycling keep exact RSX semantics (native oracles, real Vulkan).

Oracles are independent of the optimized paths: the same vertices in a different guest memory layout must
produce identical pixels, and recycled device memory must carry exactly the bytes written by the new owner.
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
static std::vector<std::uint32_t> draw_pixels(PpuState& s,unsigned words){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
 std::vector<std::uint32_t> out(words);GuestMemory::ObserverPause pause(s.memory);
 for(unsigned i=0;i<words;++i)out[i]=std::uint32_t(s.memory.load(0xc0100000u+4*i,4));
 return out;
}
static std::uint64_t counter(RsxPerfCounters::Id id){return RsxPerfCounters::values[id].load();}
// Same four vertices in different guest layouts: separate buffers (baseline), attribute 1 touching the end of
// attribute 0's predecessor (adjacent, attribute 0 at a non-zero offset in the shared range), and attribute 1
// overlapping attribute 0's bytes (it then reads the position records' z,w words). The fragment program outputs
// the interpolated attribute 1 (texcoord) as colour, so BOTH attributes decide the checked pixels; the expected
// pixels are computed analytically from the vertex values, independent of any renderer path:
//   layouts 0/1: u=-0.25+1.5*(x+.5)/16, v=-0.125+1.25*(y+.5)/8 (quad uv corners, affine over the whole target),
//   layout 2   : every vertex reads (z,w)=(0.25,1.0) -> constant u=.25,v=1.
// The shared layouts must read each guest byte once (vertex_bytes is the union of the attribute spans).
static unsigned near_channel(unsigned a,unsigned b){return a>b?a-b:b-a;}
static bool near_argb_pixel(std::uint32_t a,std::uint32_t b){for(unsigned shift=0;shift<32;shift+=8)if(near_channel((a>>shift)&255,(b>>shift)&255)>1)return false;return true;}
static std::uint32_t expected_uv_pixel(float u,float v){
 auto q=[](float f){return std::uint32_t(std::lround(255.f*std::min(1.f,std::max(0.f,f))));};
 return (255u<<24)|(q(u)<<16)|(q(v)<<8);   // A8R8G8B8 of (r,g,b,a)=(u,v,0,1)
}
static void vertex_groups_(){
 auto render=[&](int layout,std::uint64_t& vertex_bytes){
  PpuState s;setup_draw(s,16,8,64,false);auto& R=s.rsx.registers;auto& m=s.memory;
  // MOV R0,f[TEX0]: the texcoord varying (input 4) becomes the colour.
  auto fpw=[&](unsigned i,unsigned w){m.store(0xc0001000+i*4,4,(w<<16)|(w>>16));};
  fpw(0,(1u<<24)|(15u<<9)|(4u<<13)|1u);fpw(1,1u|(0xe4u<<9)|(7u<<18));fpw(2,0);fpw(3,0);
  if(layout==1){ // uv at 0x2000..0x2020 (stride 8), positions at 0x2020..0x2060 (stride 16)
   std::array<std::uint32_t,32> words{};for(unsigned i=0;i<4;++i){for(unsigned c=0;c<2;++c)words[i*2+c]=std::uint32_t(m.load(0x70002100+i*8+c*4,4));for(unsigned c=0;c<4;++c)words[8+i*4+c]=std::uint32_t(m.load(0x70002000+i*16+c*4,4));}
   for(unsigned i=0;i<24;++i)m.store(0x70002000+i*4,4,words[i]);
   R[0x1680]=0x80002020;R[0x1684]=0x80002000;
  }else if(layout==2){ // attribute 1 reads 8 bytes of every position record (offset 8: z,w)
   R[0x1684]=0x80002008;R[0x1744]=(16u<<8)|(2u<<4)|2u;
  }
  const auto before=counter(RsxPerfCounters::vertex_bytes);
  auto pixels=draw_pixels(s,16*8);vertex_bytes=counter(RsxPerfCounters::vertex_bytes)-before;
  return pixels;
 };
 std::uint64_t b0=0,b1=0,b2=0;
 const auto base=render(0,b0),adjacent=render(1,b1),overlap=render(2,b2);
 for(unsigned layout=0;layout<3;++layout){const auto& image=layout==0?base:layout==1?adjacent:overlap;unsigned wrong=0;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<16;++x){
   const float u=layout==2?.25f:-0.25f+1.5f*(x+.5f)/16,v=layout==2?1.f:-0.125f+1.25f*(y+.5f)/8;
   if(!near_argb_pixel(image[y*16+x],expected_uv_pixel(u,v)))++wrong;}
  if(wrong)std::printf("layout %u: %u of 128 pixels differ from the analytic uv colour (first %08x %08x %08x)\n",layout,wrong,image[0],image[15],image[127]);
  assert(wrong==0);}
 // Non-vacuous: the gradient layouts really vary with attribute 1 and the overlap layout is a different, constant picture.
 assert(base[0]!=base[15] && base[0]!=base[127] && base==adjacent && overlap!=base && overlap[0]==overlap[127]);
 assert(b0==64+32);   // attr0 16*3+16, attr1 8*3+8: disjoint buffers
 assert(b1==64+32);   // adjacent spans: union is the sum
 assert(b2==64);      // attr1 (offset 8, stride 16: 16*3+8) lies inside attr0's span
 std::puts("PASS interleaved vertex attributes share one guest range; analytic uv colours prove each attribute's offset");
}
// Device-local memory is recycled by (type,size). Oracle: the same rounds with the recycler
// disabled (ANYPS3_RSX_MEMORY_CACHE_MB=0) must write byte-identical guest memory.
static std::vector<std::uint8_t> memory_rounds_(bool recycle,std::uint64_t& reuses,bool& cacheable){
 setenv("ANYPS3_RSX_MEMORY_CACHE_MB",recycle?"64":"0",1);   // explicit: configure_memory_cache runs per device and must honour both
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 cacheable=false;for(std::uint32_t i=0;i<vk.memory.memoryTypeCount;++i){const auto f=vk.memory.memoryTypes[i].propertyFlags;
  cacheable|=(f&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) && !(f&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);}
 const auto before=counter(RsxPerfCounters::vk_memory_reuses);std::vector<std::uint8_t> all;
 for(unsigned round=0;round<8;++round){
  const unsigned base=0x10000+round*0x4000;
  for(unsigned i=0;i<64*8;i+=4)s.memory.store(0xc0000000u+base+i,4,0x11110000u+i);
  auto t=rsxdraw::bind_target(s,0xfeed0000,base,64,8,8);auto& surface=session.bind(t,8,8,1,8,false);
  std::vector<std::uint8_t> pixels(8*8*4);for(std::size_t i=0;i<pixels.size();++i)pixels[i]=std::uint8_t(i*7+round*29+1);
  vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(surface);
  session.flush();session.clear_surfaces();session.flush();
  GuestMemory::ObserverPause pause(s.memory);
  for(unsigned i=0;i<64*8;++i)all.push_back(std::uint8_t(s.memory.load(0xc0000000u+base+i,1)));
 }
 reuses=counter(RsxPerfCounters::vk_memory_reuses)-before;s.rsx.graphics_session=nullptr;return all;
}
// mode 0: limit 0 (recycler and pool off), 1: limit 64 MB, 2: variable unset (default must be restored). `reuses` = 1 when the
// second identical allocation was served without a driver allocation (pool range or exact recycled block), else 0.
static void direct_reuse_(int mode,std::uint64_t& reuses,bool& cacheable){
 if(mode==2)unsetenv("ANYPS3_RSX_MEMORY_CACHE_MB");else setenv("ANYPS3_RSX_MEMORY_CACHE_MB",mode==1?"64":"0",1);
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 // Recycled/pooled memory types: device-local-only, plus HOST_COHERENT ones under the pool test hook.
 const bool hook=std::getenv("ANYPS3_RSX_MEMORY_POOL_TYPES")!=nullptr;
 cacheable=false;for(std::uint32_t i=0;i<vk.memory.memoryTypeCount;++i){const auto f=vk.memory.memoryTypes[i].propertyFlags;
  cacheable|=((f&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) && !(f&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)) || (hook && (f&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT));}
 auto make=[&]{auto image=vk.graphics_image(*session.batch,VK_FORMAT_R8G8B8A8_UNORM,64,64,1);session.flush();return image;};
 auto first=make();
 first.reset();                      // the GPU is idle (flush waited): the memory returns to the pool/recycler
 const auto before=counter(RsxPerfCounters::vk_memory_allocs);
 auto second=make();                 // identical size/type: served without a driver allocation
 reuses=counter(RsxPerfCounters::vk_memory_allocs)==before?1:0;second.reset();s.rsx.graphics_session=nullptr;
}
static void memory_reuse_(){
 std::uint64_t off_reuses=0,on_reuses=0;bool off_cacheable=false,on_cacheable=false;
 const auto off=memory_rounds_(false,off_reuses,off_cacheable);const auto on=memory_rounds_(true,on_reuses,on_cacheable);
 assert(off_reuses==0 && off.size()==on.size() && off==on);
 // The written pixels reached guest memory (not a vacuous equality of untouched rows).
 bool changed=false;for(unsigned i=0;i<64;++i)changed|=on[i]!=std::uint8_t((0x11110000u+(i&~3u))>>(24-8*(i&3)));assert(changed);
 if(!on_cacheable)assert(on_reuses==0);
 std::printf("INFO memory cacheable=%d reuses=%llu\n",int(on_cacheable),(unsigned long long)on_reuses);
 // Deterministic reuse: a retired device-local image block is handed to the next identical allocation (cacheable
 // devices only), never when the recycler is off; lavapipe has no device-local-only memory type (reported).
 // The pool and the recycler both count: `reuses` is "no driver allocation for the second image".
 std::uint64_t enabled_reuses=0,disabled_reuses=0,default_reuses=0;bool c1=false,c2=false,c3=false;
 direct_reuse_(1,enabled_reuses,c1);direct_reuse_(0,disabled_reuses,c2);direct_reuse_(2,default_reuses,c3);   // 0 then unset: the singleton must not keep the 0
 assert(disabled_reuses==0);
 if(c1)assert(enabled_reuses==1 && default_reuses==1);else assert(enabled_reuses==0 && default_reuses==0);
 std::printf("INFO direct reuse cacheable=%d enabled=%llu disabled=%llu default=%llu\n",int(c1),(unsigned long long)enabled_reuses,(unsigned long long)disabled_reuses,(unsigned long long)default_reuses);
 std::puts("PASS recycled device memory carries only the new owner's bytes");
}
// Vertex bytes read under the lease protocol (no exclusive page claim): the same quad must render identical pixels
// from plain guest memory, serially and with parallel guest memory (Range::read leases), and also when the vertex
// page is owned by the GPU (a dirty surface whose guest bytes are stale): the read must retire the surface first.
// Oracle: the pixels of the plain-memory baseline; the GPU-owned image is built from the baseline's own vertex
// bytes through the A8R8G8B8 byte mapping (guest bytes [A,R,G,B] <- image texel bytes [R,G,B,A], as test_rsx_pipeline packed_leases_).
static std::vector<std::uint32_t> lease_vertex_(bool concurrent,bool gpu_owned,std::uint64_t& first_claims,std::uint64_t& repeat_claims){
 PpuState s;setup_draw(s,16,8,64,false);auto& m=s.memory;
 std::array<std::uint8_t,4096> page{};for(unsigned i=0;i<4096;++i)page[i]=std::uint8_t(m.load(0x70002000+i,1));
 if(concurrent)m.enable_concurrency();
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 if(gpu_owned){
  {GuestMemory::RawPermit raw(m);
   for(unsigned i=0;i<4096;i+=4)m.store(0x70002000+i,4,0);   // stale guest bytes: the real data lives only in the GPU image
   auto target=rsxdraw::bind_target(s,0xfeed0001,0x2000,64,16,64,4);
   auto& surface=session.bind(target,16,64,1,8,false);
   std::vector<std::uint8_t> texels(4096);   // A8R8G8B8: guest bytes [A,R,G,B] <- texel bytes [R,G,B,A]
   for(unsigned i=0;i<4096;i+=4){texels[i]=page[i+1];texels[i+1]=page[i+2];texels[i+2]=page[i+3];texels[i+3]=page[i];}
   s.rsx.renderer->graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,texels.data());session.dirty_surface(surface);
   assert(surface.dirty);}
 }
 // The first draw binds/uploads its targets (claims allowed). Its own pixels are the oracle comparison: a vertex read that
 // flushes the batch at the wrong moment (after the staging slice was reserved) corrupts exactly this first draw.
 auto before=GuestMemory::stat_gpu_claims.load();
 rsx_execute_draw(s);
 first_claims=GuestMemory::stat_gpu_claims.load()-before;
 session.flush();
 std::vector<std::uint32_t> out(16*8);
 {GuestMemory::ObserverPause pause(m);for(unsigned i=0;i<out.size();++i)out[i]=std::uint32_t(m.load(0xc0100000u+4*i,4));}
 // Identical draws after it only read vertices: no exclusive GPU page claim may be taken for them.
 before=GuestMemory::stat_gpu_claims.load();
 for(unsigned k=0;k<7;++k){s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
 repeat_claims=GuestMemory::stat_gpu_claims.load()-before;
 session.flush();
 s.rsx.graphics_session=nullptr;return out;
}
static void lease_vertex_reads_(){
 std::uint64_t f0=0,r0=0,f1=0,r1=0,f2=0,r2=0,f3=0,r3=0;
 const auto base=lease_vertex_(false,false,f0,r0),parallel=lease_vertex_(true,false,f1,r1),owned=lease_vertex_(false,true,f2,r2),owned_parallel=lease_vertex_(true,true,f3,r3);
 unsigned lit=0;for(auto p:base)lit+=p!=0;assert(lit>=100);   // the quad covers the target: not an empty comparison
 auto diff=[&](const char* name,const std::vector<std::uint32_t>& v){unsigned d=0,lit=0;for(unsigned i=0;i<v.size();++i){d+=v[i]!=base[i];lit+=v[i]!=0;}std::printf("INFO %s lit=%u differing=%u\n",name,lit,d);};
 diff("serial",base);diff("parallel",parallel);diff("owned",owned);diff("owned_parallel",owned_parallel);
 assert(parallel==base);
 assert(owned==base);
 assert(owned_parallel==base);
 assert(r1==0 && r3==0);   // repeated draws read vertices under shared leases: no exclusive GPU page claim per draw
 std::printf("INFO claims first=%llu/%llu repeat=%llu/%llu\n",(unsigned long long)f1,(unsigned long long)f3,(unsigned long long)r1,(unsigned long long)r3);
 std::puts("PASS vertex reads under shared leases: serial, parallel and GPU-owned vertex page render identical pixels without a GPU claim per draw");
}
// Ordering contract of the shared-lease vertex read (the exclusive page claim is gone): a bulk guest write that is
// ORDERED before the draw (the writer finishes its transfer, then releases a label that the FIFO side acquires) must
// be seen completely by the draw, and the draw's vertex snapshot must be finished before the label the writer waits
// for (released after the draw) is published, so the writer's next bulk write can never overlap it.
//   property A: write -> label L1 -> (acquire L1) draw sees exactly that write
//   property B: draw's snapshot complete -> label L2 -> (acquire L2) writer overwrites the buffer
// Four rounds, each round rewrites all 64 vertex bytes with a different rectangle. Oracle: the serial run (writer
// = main thread, no concurrency, no labels) of the same sequence, plus the analytic rectangle coverage of the last
// draw; the writer is a real foreign thread issuing GuestMemory::transfer bulk writes (the SPU PUT path).
static std::array<std::uint8_t,64> variant_bytes_(unsigned k){
 static const float rect[4][4]={{0,0,16,8},{0,0,8,8},{8,0,16,4},{4,2,12,6}};
 const float x0=rect[k][0],y0=rect[k][1],x1=rect[k][2],y1=rect[k][3];
 const float pos[4][2]={{x0,y0},{x1,y0},{x1,y1},{x0,y1}};std::array<std::uint8_t,64> bytes{};
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<4;++c){const float f=c<2?pos[i][c]:c==2?0.25f:1.f;std::uint32_t w;std::memcpy(&w,&f,4);for(unsigned b=0;b<4;++b)bytes[i*16+c*4+b]=std::uint8_t(w>>(24-8*b));}
 return bytes;
}
static std::vector<std::vector<std::uint32_t>> ordered_images_(bool concurrent){
 PpuState s;setup_draw(s,16,8,64,false);auto& m=s.memory;
 if(concurrent)m.enable_concurrency();
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 const std::uint64_t vertices=0x70002000,l1=0x70005000,l2=0x70005004;
 m.store(l1,4,0);m.store(l2,4,0);
 std::thread writer;
 if(concurrent)writer=std::thread([&]{
  for(unsigned k=0;k<4;++k){
   while(m.load(l2,4)!=k)std::this_thread::yield();               // B: wait for the draw's completion label
   auto bytes=variant_bytes_(k);m.transfer(vertices,bytes.data(),64,true);   // bulk write (SPU PUT path)
   m.store(l1,4,k+1);                                              // A: release the write
  }
  m.release_reader_slot();});
 std::vector<std::vector<std::uint32_t>> images;
 for(unsigned k=0;k<4;++k){
  if(concurrent){while(m.load(l1,4)!=k+1)std::this_thread::yield();}
  else{auto bytes=variant_bytes_(k);m.transfer(vertices,bytes.data(),64,true);}
  s.rsx.registers[0x210]=0x100000+k*0x4000;   // a fresh colour target per round: the rounds' pictures differ by their rectangle
  s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();
  std::vector<std::uint32_t> out(16*8);{GuestMemory::ObserverPause pause(m);for(unsigned i=0;i<out.size();++i)out[i]=std::uint32_t(m.load(0xc0100000u+k*0x4000+4*i,4));}
  images.push_back(out);
  if(concurrent)m.store(l2,4,k+1);
 }
 if(concurrent)writer.join();
 s.rsx.graphics_session=nullptr;return images;
}
static void ordered_bulk_writes_(){
 const auto serial=ordered_images_(false),parallel=ordered_images_(true);
 assert(serial.size()==4 && parallel.size()==4);
 for(unsigned k=0;k<4;++k){assert(serial[k]==parallel[k]);}
 // Non-vacuous and analytic: every round lit exactly the pixels of its own rectangle (independent of both runs).
 static const unsigned rect[4][4]={{0,0,16,8},{0,0,8,8},{8,0,16,4},{4,2,12,6}};
 for(unsigned k=0;k<4;++k){unsigned lit=0,outside=0;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<16;++x){const bool in=x>=rect[k][0] && x<rect[k][2] && y>=rect[k][1] && y<rect[k][3];
   const bool on=parallel[k][y*16+x]!=0;if(on)++lit;if(on && !in)++outside;}
  assert(lit==(rect[k][2]-rect[k][0])*(rect[k][3]-rect[k][1]) && outside==0);}
 std::puts("PASS ordered bulk guest writes (foreign writer thread, label handshakes) are seen completely and never overlap the vertex snapshot");
}
// Vertex input reuse (ANYPS3_RSX_GEOMETRY_REUSE, Session::read_inputs): a repeated draw of unchanged vertex bytes is
// served from the session's copy, and every kind of writer between two draws makes the next draw use the new bytes.
// Oracle: the analytic rectangle of variant_bytes_(k) (each image lights exactly the pixels of the rectangle whose
// bytes were in guest memory when it was drawn), independent of any renderer path. Each writer gets its own vertex
// address, so the per-address rewrite limit never hides a writer behind the direct path.
static bool lights_rect_(const std::vector<std::uint32_t>& image,unsigned k){
 static const unsigned rect[4][4]={{0,0,16,8},{0,0,8,8},{8,0,16,4},{4,2,12,6}};unsigned lit=0,outside=0;
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<16;++x){const bool in=x>=rect[k][0] && x<rect[k][2] && y>=rect[k][1] && y<rect[k][3];
  const bool on=image[y*16+x]!=0;lit+=on;outside+=on && !in;}
 return lit==(rect[k][2]-rect[k][0])*(rect[k][3]-rect[k][1]) && !outside;
}
static void geometry_reuse_(){
 PpuState s;setup_draw(s,16,8,64,false);auto& m=s.memory;auto& R=s.rsx.registers;m.enable_concurrency();
 s.rsx.renderer=std::make_unique<NativeVulkan>();auto session=std::make_unique<rsxgraphics::Session>(*s.rsx.renderer,s);session->observe();s.rsx.graphics_session=session.get();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 assert(session->geometry_reuse);   // default 1
 unsigned round=0;std::uint64_t at=0x70003000;
 auto draw=[&]{R[0x210]=0x100000+round*0x4000;s.rsx.draw.primitive=8;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);return round++;};
 auto image=[&](unsigned r){session->flush();std::vector<std::uint32_t> out(128);GuestMemory::ObserverPause pause(m);
  for(unsigned i=0;i<128;++i){out[i]=std::uint32_t(m.load(0xc0100000u+r*0x4000+4*i,4));}
  return out;};
 auto ppu=[&](unsigned k){const auto b=variant_bytes_(k);for(unsigned i=0;i<16;++i)m.store(at+4*i,4,(std::uint32_t(b[4*i])<<24)|(b[4*i+1]<<16)|(b[4*i+2]<<8)|b[4*i+3]);};
 auto start=[&](unsigned k){at+=0x100;R[0x1680]=0x80000000u|unsigned(at-0x70000000u);ppu(k);};
 auto key=[&](std::uint64_t address,unsigned size){return (address<<32)|size;};
 // (a) PPU stores; the repeated unchanged draw is a hit (positions and uv groups).
 start(0);auto r0=draw();const auto hits0=session->geometry_hits;auto r1=draw();
 assert(session->geometry_hits==hits0+2 && session->geometry_entries.count(key(at,64)));
 ppu(1);const auto rereads0=session->geometry_rereads;auto r2=draw();assert(session->geometry_rereads==rereads0+1);
 assert(lights_rect_(image(r0),0) && lights_rect_(image(r1),0) && lights_rect_(image(r2),1));
 // (b) SPU-DMA-style bulk write from a foreign thread (GuestMemory::transfer, the SPU PUT path), joined before the draw.
 start(0);r0=draw();r1=draw();
 std::thread([&]{const auto b=variant_bytes_(2);m.transfer(at,const_cast<std::uint8_t*>(b.data()),64,true);m.release_reader_slot();}).join();
 r2=draw();assert(lights_rect_(image(r0),0) && lights_rect_(image(r1),0) && lights_rect_(image(r2),2));
 // (c) RSX/GPU claim write into the cached span (ClaimTransfer: the blit/label commit path).
 start(0);r0=draw();r1=draw();
 {const auto range=m.range(at,64,true);rsxdraw::ClaimTransfer transfer(range,0,64);const auto b=variant_bytes_(3);transfer.write(0,b.data(),64);}
 r2=draw();assert(lights_rect_(image(r1),0) && lights_rect_(image(r2),3));
 // (d) raw-pointer write (RawPermit, the HLE/syscall buffer path): exposure of the writable pointer drops the entry.
 start(0);r0=draw();r1=draw();
 {GuestMemory::RawPermit raw(m);const auto range=m.range(at,64,true);auto* p=range.pointer(0,64);const auto b=variant_bytes_(1);std::memcpy(p,b.data(),64);}
 r2=draw();assert(lights_rect_(image(r1),0) && lights_rect_(image(r2),1));
 // (e) a write between two draws of one batch (no flush in between): each draw shows its own bytes.
 start(0);r0=draw();r1=draw();ppu(2);r2=draw();assert(lights_rect_(image(r1),0) && lights_rect_(image(r2),2));
 // (f) the vertex page becomes GPU-authoritative after the copy was taken (a dirty surface whose image holds variant 1):
 // the draw's Range creation retires it into guest memory, which invalidates the copy.
 start(0);r0=draw();r1=draw();assert(lights_rect_(image(r1),0));
 {const std::uint64_t page=at&~std::uint64_t(4095);std::array<std::uint8_t,4096> bytes{};
  {GuestMemory::ObserverPause pause(m);for(unsigned i=0;i<4096;++i)bytes[i]=std::uint8_t(m.load(page+i,1));}
  const auto v=variant_bytes_(1);std::copy(v.begin(),v.end(),bytes.begin()+(at-page));
  GuestMemory::RawPermit raw(m);
  auto target=rsxdraw::bind_target(s,0xfeed0001,unsigned(page-0x70000000u),64,16,64,4);auto& surface=session->bind(target,16,64,1,8,false);
  std::vector<std::uint8_t> texels(4096);for(unsigned i=0;i<4096;i+=4){texels[i]=bytes[i+1];texels[i+1]=bytes[i+2];texels[i+2]=bytes[i+3];texels[i+3]=bytes[i];}
  s.rsx.renderer->graphics_transfer(*session->batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,texels.data());session->dirty_surface(surface);}
 r2=draw();assert(lights_rect_(image(r2),1));
 // (g) unmap + map at the same address with a new backing holding variant 2: a new physical identity, never the copy.
 start(0);r0=draw();r1=draw();
 {auto it=std::find_if(m.regions.begin(),m.regions.end(),[](const auto& r){return r.base==0x70000000;});assert(it!=m.regions.end());
  std::vector<std::uint8_t> bytes(it->bytes->begin(),it->bytes->end());const auto size=bytes.size();
  {GuestMemory::ObserverPause pause(m);for(std::size_t i=0;i<size;++i)bytes[i]=std::uint8_t(m.load(0x70000000+i,1));}
  const auto v=variant_bytes_(2);std::copy(v.begin(),v.end(),bytes.begin()+(at-0x70000000u));
  m.allocated-=size;m.regions.erase(it);m.mapping_changed();m.map(0x70000000,size,6,bytes.data(),size);}
 r2=draw();assert(lights_rect_(image(r1),0) && lights_rect_(image(r2),2));
 // (h) a buffer rewritten before two draws is no longer cached: the direct path, still the current bytes.
 start(0);r0=draw();ppu(1);r1=draw();ppu(2);r2=draw();assert(!session->geometry_entries.count(key(at,64)));
 ppu(3);auto r3=draw();assert(!session->geometry_entries.count(key(at,64)));
 assert(lights_rect_(image(r0),0) && lights_rect_(image(r1),1) && lights_rect_(image(r2),2) && lights_rect_(image(r3),3));
 // (i) a format change at the same address (x,y only: 56-byte span) is its own entry and draws the same rectangle.
 start(1);r0=draw();R[0x1740]=0x1022;r1=draw();r2=draw();R[0x1740]=0x1042;
 assert(session->geometry_entries.count(key(at,56)) && lights_rect_(image(r0),1) && lights_rect_(image(r1),1) && lights_rect_(image(r2),1));
 // (j) switch off (per-session copy of ANYPS3_RSX_GEOMETRY_REUSE=0): no entry, no hit.
 session->flush();session.reset();session=std::make_unique<rsxgraphics::Session>(*s.rsx.renderer,s);session->observe();s.rsx.graphics_session=session.get();
 session->geometry_reuse=false;start(3);r0=draw();r1=draw();assert(session->geometry_entries.empty() && !session->geometry_hits);
 assert(lights_rect_(image(r0),3) && lights_rect_(image(r1),3));
 session->flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS vertex input reuse: hits for unchanged bytes; PPU, foreign transfer, GPU claim, raw pointer, same-batch, GPU-owned, remap, rewrite limit, format change and switch-off draw the current bytes");
}
// Index list reuse (ANYPS3_RSX_GEOMETRY_REUSE, Session::index_read/index_prepare/index_store): a repeated draw of an
// unchanged index span is served from the session's decoded list; every writer and every decode parameter change makes
// the next draw decode the current bytes. Oracle: the triangle list the draw uploads (scratch_triangles) against the
// list computed here from the values the test stored (+ base index); each scenario uses its own index address.
static void index_reuse_(){
 PpuState s;setup_draw(s,16,8,64,false);auto& m=s.memory;auto& R=s.rsx.registers;auto& d=s.rsx.draw;m.enable_concurrency();
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 std::uint64_t at=0xc0003000;unsigned width=2;
 auto be=[&](const std::vector<unsigned>& v){std::vector<std::uint8_t> b;for(auto x:v)for(unsigned k=0;k<width;++k)b.push_back(std::uint8_t(x>>(8*(width-1-k))));return b;};
 auto put=[&](const std::vector<unsigned>& v){for(unsigned i=0;i<v.size();++i)m.store(at+width*i,width,v[i]);};
 auto start=[&](std::uint64_t base,unsigned location,const std::vector<unsigned>& v){at=base;R[0x181c]=unsigned(at-(location?0x70000000u:0xc0000000u));R[0x1820]=(width==2?0x10u:0u)|location;put(v);};
 auto draw=[&](const std::vector<unsigned>& v,unsigned base=0){R[0x173c]=base;d.indexed=true;d.primitive=5;d.ranges={{0,3},{3,3}};rsx_execute_draw(s);
  std::vector<std::array<unsigned,3>> expect;for(std::size_t i=0;i+3<=v.size();i+=3)expect.push_back({v[i]+base,v[i+1]+base,v[i+2]+base});
  assert(d.scratch_triangles==expect);};
 const std::vector<unsigned> quad{0,1,2,0,2,3},other{2,1,0,3,2,0},third{1,2,3,1,3,0};
 // (a) unchanged span: the second draw is a hit; (b) PPU stores into it: decoded afresh.
 start(0xc0003100,0,quad);draw(quad);auto hits=session.index_hits;draw(quad);assert(session.index_hits==hits+1);
 put(other);draw(other);
 // (c) SPU-DMA-style bulk write from a foreign thread.
 start(0xc0003200,0,quad);draw(quad);draw(quad);
 std::thread([&]{auto b=be(third);m.transfer(at,b.data(),unsigned(b.size()),true);m.release_reader_slot();}).join();draw(third);
 // (d) RSX/GPU claim write (ClaimTransfer commit).
 start(0xc0003300,0,quad);draw(quad);draw(quad);
 {const auto range=m.range(at,12,true);rsxdraw::ClaimTransfer transfer(range,0,12);const auto b=be(other);transfer.write(0,b.data(),12);}draw(other);
 // (e) raw-pointer write (RawPermit, HLE/syscall buffers).
 start(0xc0003400,0,quad);draw(quad);draw(quad);
 {GuestMemory::RawPermit raw(m);const auto range=m.range(at,12,true);auto* p=range.pointer(0,12);const auto b=be(third);std::memcpy(p,b.data(),12);}draw(third);
 // (f) base index change at the same span, then back: each decodes with its own base (no stale +base).
 start(0xc0003500,0,quad);draw(quad);draw(quad,1);draw(quad,1);draw(quad);
 // (g) a write between two draws of one batch (no flush in between).
 start(0xc0003600,0,quad);draw(quad);draw(quad);put(other);draw(other);session.flush();
 // (h) 32-bit indices at the same address: another span size and width.
 width=4;start(0xc0003500,0,third);draw(third);draw(third);width=2;
 // (i) restart enabled: strips split at the restart value, no reuse of the restart-free list of the same span.
 {const std::vector<unsigned> strip{0,1,2,0xffff,2,3};start(0xc0003700,0,quad);draw(quad);put(strip);R[0x1dac]=1;R[0x1db0]=0xffff;
  R[0x173c]=0;d.indexed=true;d.primitive=5;d.ranges={{0,3},{3,3}};rsx_execute_draw(s);
  const std::vector<std::array<unsigned,3>> one{{0,1,2}};assert(d.scratch_triangles==one);R[0x1dac]=0;put(quad);draw(quad);}
 // (j) unmap + map of the IO mapping holding the span (location 1) with new bytes: a new physical identity.
 start(0x70003800,1,quad);draw(quad);draw(quad);
 {auto it=std::find_if(m.regions.begin(),m.regions.end(),[](const auto& r){return r.base==0x70000000;});assert(it!=m.regions.end());
  std::vector<std::uint8_t> bytes(it->bytes->size());const auto size=bytes.size();
  {GuestMemory::ObserverPause pause(m);for(std::size_t i=0;i<size;++i)bytes[i]=std::uint8_t(m.load(0x70000000+i,1));}
  const auto b=be(other);std::copy(b.begin(),b.end(),bytes.begin()+(at-0x70000000u));
  m.allocated-=size;m.regions.erase(it);m.mapping_changed();m.map(0x70000000,size,6,bytes.data(),size);}
 draw(other);
 // (k) switch off: no entry.
 session.flush();session.geometry_reuse=false;const auto entries=session.index_entries.size();hits=session.index_hits;
 start(0xc0003900,0,quad);draw(quad);draw(quad);assert(session.index_entries.size()==entries && session.index_hits==hits);
 session.flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS index list reuse: hits for unchanged spans; PPU, foreign transfer, GPU claim, raw pointer, same-batch writes, base/width/restart changes, remap and switch-off decode the current bytes");
}
int main(){try{vertex_groups_();memory_reuse_();lease_vertex_reads_();ordered_bulk_writes_();geometry_reuse_();index_reuse_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class Rsx2DrawTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_rsx2_vertex_groups_and_memory_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stderr)
            base = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_FRAME_DIR=str(root), VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            lavapipe = dict(base)
            if not lavapipe.get('VK_DRIVER_FILES') and not lavapipe.get('VK_ICD_FILENAMES'):
                lavapipe.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            runs = [lavapipe]
            if os.environ.get('ANYPS3_REQUIRE_NVIDIA') or os.environ.get('ANYPS3_RSX2_TEST_GPU'):
                runs.append(dict(base, ANYPS3_VK_DEVICE='gpu'))   # discrete GPU: device-local-only memory type, the recycling path
            for env in runs:
                result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=600)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('Validation Error', result.stdout + result.stderr)
                self.assertNotIn('Validation Warning', result.stdout + result.stderr)
                self.assertEqual(result.stdout.count('PASS '), 6, result.stdout)


if __name__ == '__main__':
    unittest.main()
