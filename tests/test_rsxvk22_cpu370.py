"""Exact GPU alias transitions and bounded non-owning RTT layout certificates."""
import os,subprocess,tempfile,unittest
from pathlib import Path
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES
MAIN=r'''
static void equal_grid370(){
 for(bool enabled:{true,false})for(bool depth:{false,true})for(unsigned before:{1u,2u,4u})for(unsigned after:{1u,2u,4u}){
  if(before==after)continue;
  setenv("ANYPS3_RSX_GPU_REBIND",enabled?"1":"0",1);
  PpuState s;setup_draw(s,8,6,40,false);s.memory.enable_concurrency();s.memory.enable_page_broker();NativeVulkan vk;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
  for(unsigned y=0;y<6;++y)for(unsigned x=0;x<10;++x)s.memory.store(0xc0100000+y*40+x*4,4,0xdecafbad);
  const auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,40,8,6,4);
  const auto nx=before>1?2u:1u,ny=before==4?2u:1u,w=8/nx,h=6/ny;
  auto& old=session.bind(target,w,h,before,depth?2:8,depth);std::vector<std::uint8_t> pixels(w*h*before*4),stencil(w*h*before);
  for(unsigned n=0;n<w*h*before;++n){if(depth){const std::uint32_t d=(n+1)*1009;std::memcpy(pixels.data()+4*n,&d,4);stencil[n]=n+17;}
   else{pixels[4*n]=n+1;pixels[4*n+1]=2*n+3;pixels[4*n+2]=3*n+5;pixels[4*n+3]=255-n;}}
  vk.graphics_transfer(*session.batch,old.image,depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());
  if(depth){vk.graphics_transfer(*session.batch,old.image,VK_IMAGE_ASPECT_STENCIL_BIT,1,stencil.data());}
  session.dirty_surface(old);
  const auto reads=vk.graphics_readback_dispatches,downloads=session.batch->download_bytes,submits=session.async_submits,retires=session.async_retires;
  const auto dx=after>1?2u:1u,dy=after==4?2u:1u;auto& current=session.bind(target,8/dx,6/dy,after,depth?2:8,depth);
  assert(current.samples==after);
  if(enabled){assert(current.dirty && session.surfaces.size()==1);}
  if(enabled){assert(vk.graphics_readback_dispatches==reads && session.batch->download_bytes==downloads && session.async_submits==submits && session.async_retires==retires);}
  session.flush();
  for(unsigned y=0;y<6;++y){for(unsigned x=0;x<8;++x){const auto layer=(y%ny)*nx+x%nx,n=(layer*h+y/ny)*w+x/nx;
    const auto expected=depth?((n+1)*1009u<<8)|(n+17):((255-n)<<24)|((n+1)<<16)|((2*n+3)<<8)|(3*n+5);
    assert(s.memory.load(0xc0100000+y*40+x*4,4)==expected);}
   for(unsigned x=8;x<10;++x)assert(s.memory.load(0xc0100000+y*40+x*4,4)==0xdecafbad);}
  s.rsx.graphics_session=nullptr;
 }
 unsetenv("ANYPS3_RSX_GPU_REBIND");
 std::puts("PASS equal physical footprint: all MSAA1/2/4 transitions, exact ARGB/depth24/stencil8, row padding, no host roundtrip");
}
static void footprint_errors370(){
 auto bytes=std::make_shared<GuestBacking>(64);rsxgraphics::Surface a{},c{};a.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();c.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
 a.readback->words={0,4,8,12};c.readback->words={12,8,4,0};
 a.readback->runs={{bytes->data(),16,0,0}};c.readback->runs={{bytes->data(),8,0,0},{bytes->data()+8,8,8,8}};
 assert(rsxgraphics::Session::same_packed_footprint(a,c));
 c.readback->runs[1].data+=4;assert(!rsxgraphics::Session::same_packed_footprint(a,c));c.readback->runs[1].data-=4;
 c.readback->runs[1].offset=4;assert(!rsxgraphics::Session::same_packed_footprint(a,c));c.readback->runs[1].offset=8;
 c.readback->words.pop_back();assert(!rsxgraphics::Session::same_packed_footprint(a,c));c.readback->words.push_back(0);
 c.readback->runs[0].size=7;assert(!rsxgraphics::Session::same_packed_footprint(a,c));
 NativeVulkan vk;auto batch=vk.graphics_batch();auto image=vk.graphics_image(*batch,VK_FORMAT_R8G8B8A8_UNORM,1,1,1);std::shared_ptr<NativeVulkan::GraphicsBuffer> table;std::string error;
 try{vk.graphics_unpack_upload(*batch,image,8,false,4,4,1,std::vector<unsigned>{0},table,nullptr,batch->used+4);}catch(const std::runtime_error& e){error=e.what();}
 assert(error=="RSX device surface upload input bounds");
 std::puts("PASS alias witness: partitions match, holes/misalignment/offset/size reject, invalid device input stops before recording");
}
static void proofs370(){
 PpuState s;setup_draw(s,8,6,40,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 std::vector<std::shared_ptr<rsxdraw::TexturePlan>> textures;std::vector<std::shared_ptr<rsxgraphics::Surface::ReadbackPlan>> layouts;
 for(unsigned n=0;n<4;++n){auto t=std::make_shared<rsxdraw::TexturePlan>();auto p=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();t->runs.resize(300000);p->words.resize(2000000);
  session.remember_rtt_proof(t,p,n);textures.push_back(t);layouts.push_back(p);}
 for(unsigned n=0;n<4;++n){assert(session.rtt_proved(textures[n],layouts[n],n));assert(!session.rtt_proved(textures[n],layouts[n],n+1));}
 auto t=textures[0];auto p=layouts[0];assert(!session.rtt_proved(std::make_shared<rsxdraw::TexturePlan>(),p,0));assert(!session.rtt_proved(t,std::make_shared<rsxgraphics::Surface::ReadbackPlan>(),0));
 std::weak_ptr<rsxdraw::TexturePlan> wt=textures.back();std::weak_ptr<rsxgraphics::Surface::ReadbackPlan> wp=layouts.back();textures.pop_back();layouts.pop_back();assert(wt.expired() && wp.expired());
 for(unsigned n=0;n<300;++n){auto a=std::make_shared<rsxdraw::TexturePlan>();auto c=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();session.remember_rtt_proof(a,c,n);assert(session.rtt_proved(a,c,n));}
 assert(session.rtt_proofs.size()<=256);
 std::puts("PASS RTT proof cache: large live certificates persist, exact identities/origin, weak lifetimes, finite256 entries");
}

static void pool_workset370(unsigned count,unsigned width,unsigned height){
 PpuState s;setup_draw(s,4,4,16,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 std::vector<std::shared_ptr<NativeVulkan::GraphicsImage>> held;
 std::vector<const NativeVulkan::GraphicsImage*> identities;
 for(unsigned i=0;i<count;++i){auto image=session.surface_image(VK_FORMAT_R8G8B8A8_UNORM,width+4*i,height,1);identities.push_back(image.get());held.push_back(std::move(image));}
 vk.graphics_finish(*session.batch);vk.graphics_reset(*session.batch);held.clear();
 const auto misses=session.surface_image_misses,hits=session.surface_image_hits;
 for(unsigned i=0;i<count;++i){auto image=session.surface_image(VK_FORMAT_R8G8B8A8_UNORM,width+4*i,height,1);assert(image.get()==identities[i]);}
 assert(session.surface_image_misses==misses && session.surface_image_hits==hits+count);
 assert(session.surface_images.size()<=64 && session.surface_image_bytes<=96*1024*1024);
 vk.graphics_finish(*session.batch);
}
static void pools370(){
 // Separate independent working sets cross the old count and byte thresholds.
 // Every target stays retained until real command completion, then must reuse
 // its original image without another Vulkan allocation in the warm cycle.
 pool_workset370(40,16,8);pool_workset370(20,1024,1024);
 std::puts("PASS bounded pool: 40 distinct small targets and 20 large targets reuse the complete warm working set without allocations");
}


static void bulk16_case370(bool swizzled){
 PpuState s;setup_draw(s,4,4,16,false);s.memory.enable_concurrency();s.memory.enable_page_broker();NativeVulkan vk;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 for(unsigned i=0;i<64;++i)s.memory.store(0xc0100000+i,1,0x5a);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,swizzled?8:16,4,4,2);
 if(swizzled){target.swizzle_width=target.swizzle_height=4;}
 auto watched=s.memory.watch_cpu_dirty(s.memory.range(0xc0100000,2,false));
 auto hole=s.memory.watch_cpu_dirty(s.memory.range(0xc0100000+(swizzled?32:8),2,false));
 watched->consume();hole->consume();const auto reservation=s.memory.parallel_token(0xc0100000,4);
 // Register CPU inputs before binding: a later CPU read legitimately retires the active Surface.
 auto& surface=session.bind(target,4,4,1,1,true);
 // Match flush's busy guard for this internal payload helper.
 {const auto previous=session.busy;session.busy=true;
  {GuestMemory::ObserverPause pause(s.memory);rsxgraphics::SurfaceBytes bulk(surface,true,true);
   assert(bulk.runs.size()==(swizzled?1u:4u) && bulk.spans.size()==bulk.runs.size() && bulk.bytes.size()==32);}
  session.busy=previous;
 }
 assert(session.surfaces.size()==1);
 const std::array<std::uint16_t,16> pixels{{0x0000,0x0001,0x0023,0x0123,0x7fff,0x8000,0xfffe,0xffff,0x1357,0x2468,0xaaaa,0x5555,0x1020,0x3040,0xabcd,0xdcba}};
 vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_DEPTH_BIT,2,pixels.data());session.dirty_surface(surface);session.flush();
 if(swizzled){
  // Fixed physical Morton order, independent of Target::address/readback map.
  const std::array<std::uint16_t,16> expected{{0x0000,0x0001,0x7fff,0x8000,0x0023,0x0123,0xfffe,0xffff,0x1357,0x2468,0x1020,0x3040,0xaaaa,0x5555,0xabcd,0xdcba}};
  for(unsigned i=0;i<16;++i)assert(s.memory.load(0xc0100000+2*i,2)==expected[i]);
  for(unsigned i=32;i<64;++i)assert(s.memory.load(0xc0100000+i,1)==0x5a);
 }else for(unsigned y=0;y<4;++y){for(unsigned x=0;x<4;++x)assert(s.memory.load(0xc0100000+y*16+2*x,2)==pixels[4*y+x]);for(unsigned x=8;x<16;++x)assert(s.memory.load(0xc0100000+y*16+x,1)==0x5a);}
 assert(s.memory.parallel_token(0xc0100000,4)!=reservation);assert(watched->consume());assert(!hole->consume());
 s.memory.unwatch_cpu_dirty(watched);s.memory.unwatch_cpu_dirty(hole);s.rsx.graphics_session=nullptr;
}
static void bulk16_370(){
 bulk16_case370(true);bulk16_case370(false);
 PpuState s;setup_draw(s,128,128,256,false);s.memory.enable_concurrency();s.memory.enable_page_broker();NativeVulkan vk;rsxgraphics::Session session(vk,s);
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,256,128,128,2);target.swizzle_width=target.swizzle_height=128;
 auto& surface=session.bind(target,128,128,1,1,true);
 {GuestMemory::ObserverPause pause(s.memory);rsxgraphics::SurfaceBytes bulk(surface,true,true);assert(bulk.runs.size()==1 && bulk.spans.size()==1 && bulk.bytes.size()==32768);}
 std::puts("PASS bulk D16: exact endian/Morton/depth boundaries, padding/holes, reservation invalidation, exact dirty watches, 16384 pixel runs become one physical transaction");
}

int main(){try{NativeVulkan probe;if(std::getenv("ANYPS3_REQUIRE_NVIDIA") && probe.device_name.find("NVIDIA")==std::string::npos)throw std::runtime_error("NVIDIA required");equal_grid370();footprint_errors370();proofs370();pools370();bulk16_370();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {','class NativeVulkan {\npublic:',1)+MAIN
class Cpu370Tests(unittest.TestCase):
 def test_exact_alias_and_proof_lifetimes(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);cpp=p/'test.cpp';cpp.write_text(native_source());r=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(p/'test')],capture_output=True,text=True,timeout=240);self.assertEqual(r.returncode,0,r.stderr)
   env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1');env.pop('ANYPS3_RSX_GPU_REBIND',None)
   if not env.get('ANYPS3_REQUIRE_NVIDIA'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
   r=subprocess.run([str(p/'test')],env=env,capture_output=True,text=True,timeout=240);self.assertEqual(r.returncode,0,r.stdout+r.stderr);self.assertNotIn('Validation Error',r.stderr);self.assertNotIn('Validation Warning',r.stderr);self.assertEqual(r.stdout.count('PASS '),5)
