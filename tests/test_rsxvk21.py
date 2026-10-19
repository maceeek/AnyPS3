"""Deferred input ordering and generation-keyed Vulkan RTT storage oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static std::uint32_t image_word(rsxgraphics::Session& session,const std::shared_ptr<NativeVulkan::GraphicsImage>& image){
 auto at=session.vk.graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,4);session.vk.graphics_finish(*session.batch);
 const auto* p=static_cast<const unsigned char*>(session.batch->map)+at;
 const auto value=(unsigned(p[3])<<24)|(unsigned(p[0])<<16)|(unsigned(p[1])<<8)|p[2];session.vk.graphics_reset(*session.batch);return value;
}
static void deferred21(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);
 s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});s.memory.mapping_changed();
 s.memory.enable_concurrency();s.memory.enable_page_broker();NativeVulkan vk;
 {rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto t=session.texture_metadata(0);auto image=session.texture_image(t);auto& e=session.raw_textures.back();
 assert(e.dirty_inputs && e.trusted);auto dirty=e.dirty_inputs;const auto id=e.id,identity=session.raw_identity;
 const auto count=s.memory.cpu_dirty_watches.size();assert(count>0);
 // A writer does not call the synchronous RSX observer for CPU-only bytes.
 const auto* pointer=t.plan->runs[0].data;
 assert(!s.memory.host_access_observer.interested(pointer,4));
 assert(image_word(session,image)==0xff112233);
 const auto invalid=session.raw_invalidations,allocations=vk.sampled_image_creations,reuses=vk.sampled_image_reuses;
 s.memory.store(0x81002000,4,0xff224466);assert(session.raw_invalidations==invalid);
 auto next=session.texture_image(t);assert(image_word(session,next)==0xff224466);
 assert(next==image && session.raw_textures.size()==1 && session.raw_textures[0].id==id);
 assert(vk.sampled_image_creations==allocations && vk.sampled_image_reuses==reuses+1);
 assert(session.raw_textures[0].dirty_inputs==dirty && s.memory.cpu_dirty_watches.size()==count);
 // A producer publishes after the byte snapshot but before upload install.
 // Test-only scheduling hook lives solely in generated test source.
 rsxvk21_after_snapshot=[&]{std::thread writer([&]{s.memory.store(0x81002000,4,0xff102030);});writer.join();rsxvk21_after_snapshot={};};
 s.memory.store(0x81002000,4,0xff557799);
 auto raced=session.texture_image(t);assert(image_word(session,raced)==0xff557799);
 next=session.texture_image(t);assert(image_word(session,next)==0xff102030);
 assert(s.memory.cpu_dirty_watches.size()==count);
 // Exact same-page non-overlap cannot invalidate the input.
 s.memory.store(0xc0002080,4,0xdecafbad);assert(session.texture_image(t)==next);
 // Consume-before-snapshot: a write after consumption must survive as dirty.
 assert(!dirty->consume());s.memory.store(0x81002000,4,0xffabcdef);
 assert(dirty->consume());s.memory.store(0xc0002000,4,0xff13579b);
 auto newest=session.texture_image(t);assert(image_word(session,newest)==0xff13579b);
 // GPU invalidation retains membership/token identity over 64 generations.
 rsxgraphics::Surface overlap{};overlap.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
 const auto at=reinterpret_cast<std::uintptr_t>(pointer);overlap.physical.push_back({at,at+4});
 for(unsigned n=0;n<64;++n){session.dirty_surface(overlap);assert(session.raw_textures.size()==1);
  auto current=session.texture_image(t);assert(image_word(session,current)==0xff13579b);
  assert(session.raw_identity==identity && session.raw_textures[0].id==id && s.memory.cpu_dirty_watches.size()==count);}
 // Retained raw writes still fault, retire the cache, and become visible.
 session.leave_fifo();auto* raw=const_cast<volatile unsigned char*>(pointer);raw[0]=0x80;
 session.enter_fifo();assert(image_word(session,session.texture_image(t))==0x8013579b);
 // A replacement in an exactly-full cache is not a 513th member. Fill with
 // actual GPU buffers, independent ranges and dirty tokens, not dummy entries.
 const auto full_id=session.raw_textures[0].id;
 for(unsigned n=0;n<511;++n){rsxdraw::Texture input;input.valid=true;input.storage_size=4;
  input.levels.push_back({1,1,4,s.memory.regions[1].bytes->data()+0x80000+16*n});session.texture_buffer(input);}
 assert(session.raw_textures.size()==512);const auto full_watches=s.memory.cpu_dirty_watches.size(),evictions=session.raw_evictions,full_identity=session.raw_identity;
 session.dirty_surface(overlap);assert(image_word(session,session.texture_image(t))==0x8013579b);
 assert(session.raw_positions.count(full_id)==1 && session.raw_textures.size()==512 && session.raw_identity==full_identity);
 assert(s.memory.cpu_dirty_watches.size()==full_watches && session.raw_evictions==evictions);session.clear_raw(nullptr);
 // A sample-grid/layout split with the same physical union changes no watch.
 auto dummy_mapping=std::make_shared<GuestMemory::Range>(s.memory.range(0xc0300000,64,true));
 rsxgraphics::Surface dummy{};dummy.target.mapping=dummy_mapping;
 auto begin=reinterpret_cast<std::uintptr_t>(dummy_mapping->parts[0].data);
 dummy.physical={{begin,begin+64}};session.surfaces.push_back(dummy);session.refresh_surface_watches();
 const auto revision=s.memory.host_access_observer.revision;
 session.surfaces.back().physical={{begin,begin+32},{begin+32,begin+64}};session.refresh_surface_watches();
 assert(s.memory.host_access_observer.revision==revision);session.clear_surfaces();
 // Real GPU-authoritative surfaces retain synchronous CPU coherence.
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,32,8,8);auto& surface=session.bind(target,8,8,1,8,false,false,false,true);
 assert(s.memory.host_access_observer.interested(reinterpret_cast<const void*>(surface.physical[0].begin),4));
 s.rsx.graphics_session=nullptr;session.flush();dirty.reset();
 }
 assert(s.memory.cpu_dirty_watches.empty());
 // A cache created before observer installation must acquire raw protection
 // when a later upload becomes trusted, even though its ID/token are reused.
 {rsxgraphics::Session late(vk,s);auto t=late.texture_metadata(0);late.texture_image(t);assert(!late.raw_textures[0].trusted);
  late.observe();late.leave_fifo();const auto id=late.raw_textures[0].id;
  s.memory.store(0xc0002000,4,0xff556677);late.enter_fifo();auto image=late.texture_image(t);assert(image_word(late,image)==0xff556677);
  late.leave_fifo();assert(late.raw_textures[0].trusted && late.raw_textures[0].id==id);assert(late.guard.touches_any(t.plan->runs[0].data,4));
  auto* raw=const_cast<volatile unsigned char*>(t.plan->runs[0].data);raw[0]=0x40;late.enter_fifo();assert(image_word(late,late.texture_image(t))==0x40556677);late.flush();}
 assert(s.memory.cpu_dirty_watches.empty());
 std::puts("PASS deferred textures: alias BE writes, no synchronous RPC, consume ordering, stable IDs/watches, GPU-only watches and teardown");
}
static void buffer21(){
 PpuState s;setup_draw(s,8,4,64,false);s.memory.enable_concurrency();s.memory.enable_page_broker();NativeVulkan vk;
 {rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 rsxdraw::Texture t;t.valid=true;t.storage_size=16;t.levels.push_back({2,2,8,s.memory.regions[1].bytes->data()+0x2000});
 auto read=[&](const std::shared_ptr<NativeVulkan::GraphicsBuffer>& buffer){
  vk.graphics_begin(*session.batch);const auto at=vk.graphics_space(*session.batch,16);vk.graphics_barrier(*session.batch);
  VkBufferCopy copy{0,at,16};vkCmdCopyBuffer(session.batch->command,buffer->buffer,session.batch->buffer,1,&copy);vk.graphics_barrier(*session.batch);
  vk.graphics_finish(*session.batch);const auto word=rsxdraw::read_be(static_cast<const std::uint8_t*>(session.batch->map)+at,4);vk.graphics_reset(*session.batch);return word;
 };
 s.memory.store(0xc0002000,4,0x12345678);auto a=session.texture_buffer(t);assert(read(a)==0x12345678);
 const auto id=session.raw_textures[0].id,count=s.memory.cpu_dirty_watches.size();assert(count==1);
 assert(session.texture_buffer(t)==a);s.memory.store(0xc0002000,4,0xaabbccdd);
 auto b=session.texture_buffer(t);assert(read(b)==0xaabbccdd && b!=a);assert(session.raw_textures[0].id==id && s.memory.cpu_dirty_watches.size()==count);
 session.invalidate_raw(t.levels[0].data,4,"gpu_write");auto c=session.texture_buffer(t);assert(read(c)==0xaabbccdd && session.raw_textures[0].id==id);
 session.flush();s.rsx.graphics_session=nullptr;
 }
 assert(s.memory.cpu_dirty_watches.empty());
 std::puts("PASS raw buffers: exact BE writes, dirty-before-hit, stable token/ID through GPU invalidation and teardown");
}
static void rtt21(){
 for(unsigned format:{8u,11u,2u}){
 PpuState s;setup_draw(s,8,4,64,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto& r=s.rsx.registers;r[0x1a00]=0x100000;r[0x1a04]=0x00010029|(0xa5u<<8);r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x02020000;r[0x1a18]=0x00040004;r[0x1840]=64;
 const bool depth=format==2;const unsigned bpp=format==11?8:4;
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,64,8,4,bpp);
 auto& surface=session.bind(target,8,4,1,format,depth,false,false,true);
 std::shared_ptr<NativeVulkan::GraphicsImage> view;std::uint64_t creations=0;std::vector<std::uint64_t> outputs;
 for(unsigned n=0;n<12;++n){
  // Sample-grid/alias rebinds can replace a VkImage while preserving its
  // immutable layout. Only this explicit source allocation may increase.
  if(n)surface.image=vk.graphics_image(*session.batch,surface.image->format,surface.width,surface.height,surface.samples,surface.depth);
  VkClearColorValue color{};color.float32[0]=(n&1)?1:0;color.float32[1]=0;color.float32[2]=0;color.float32[3]=1;
  vk.graphics_clear_image(*session.batch,surface.image,color,(n&1)?1:0,0,depth?VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT:VK_IMAGE_ASPECT_COLOR_BIT);session.dirty_surface(surface);
  auto texture=session.texture_metadata(0);assert(texture.resident_image);auto current=texture.resident_image;
  if(n){assert(view==current);assert(vk.graphics_image_creations==creations+n);}else{view=current;creations=vk.graphics_image_creations;}
  // The same-generation hit performs no conversion or image allocation.
  assert(session.texture_metadata(0).resident_image==current);
  outputs.push_back(vk.graphics_transfer(*session.batch,current,VK_IMAGE_ASPECT_COLOR_BIT,16));
 }
 // Queue all generations before completion: earlier sampled contents must
 // survive subsequent writes of the same cached conversion/aspect images.
 vk.graphics_finish(*session.batch);
 for(unsigned n=0;n<outputs.size();++n){
  const auto* f=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+outputs[n]);
  if(format==8){assert(f[0]==((n&1)?1.f:0.f) && f[1]==0 && f[2]==0 && f[3]==1);}
  // F16 raw bytes 0x3c00/0x0000 become ARGB8 channels, independent of codec.
  if(format==11){
   assert(f[0]==0 && f[1]==0 && f[2]==0);
   if(n&1){
    // Vulkan 1.2.198, spirvenv "Precision of core SPIR-V Instructions":
    // OpFDiv permits 2.5 ULP; exact CPU float equality is not portable.
    // https://raw.githubusercontent.com/KhronosGroup/Vulkan-Docs/v1.2.198/appendices/spirvenv.txt
    // Keep the independent raw ARGB byte oracle EXACT (F16 0x3c00 -> alpha60).
    // The ULP at 60/255 is 2^-26. Bound against the double-precision ratio,
    // not against a device-specific observed float or a loose color epsilon.
    assert(std::isfinite(f[3]) && std::lround(double(f[3])*255.0)==60);
    assert(std::fabs(double(f[3])-60.0/255.0)<=2.5*0x1p-26);
   }else assert(f[3]==0);
  }
  if(depth){assert(f[0]==((n&1)?1.f:0.f) && f[1]==((n&1)?1.f:0.f) && f[2]==0 && f[3]==((n&1)?1.f:0.f));}
 }
 vk.graphics_reset(*session.batch);
 assert(session.rtt_views.size()==1);session.flush();s.rsx.graphics_session=nullptr;
 }
 std::puts("PASS RTT: fixed RGBA/F16/depth bytes, freshness each generation, stable VkImage/aspect storage and same-generation hit");
}
static void idle21(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 PpuState s;setup_draw(s,8,4,64,false);s.memory.enable_concurrency();s.memory.enable_page_broker();
 auto& g=s.rsx;g.fifo_enabled=true;g.dma=0x7000f000;g.registers[0x8c0]=8<<16;g.registers[0x8c4]=4<<16;g.registers[0x1d90]=0xff2468ac;
 s.memory.store(g.dma+0x40,4,0x4000);s.memory.store(g.dma+0x44,4,0x4000);
 rsxvk21_fifo_after_check=[&]{
  // Publish a real CLEAR_SURFACE packet AFTER the reader saw an empty FIFO.
  s.memory.store(0x70004000,4,(1u<<18)|0x1d94);s.memory.store(0x70004004,4,0xf0);s.memory.store(g.dma+0x40,4,0x4008);
  rsxvk21_fifo_after_check={};
 };
 (void)rsx_run(s,8);assert(s.memory.load(g.dma+0x44,4)==0x4000 && g.commands==0 && g.cpu_clears==0);
 assert(rsx_run(s,8));assert(s.memory.load(g.dma+0x44,4)==0x4008 && g.commands==1 && g.cpu_clears==0);
 auto& session=*static_cast<rsxgraphics::Session*>(g.graphics_owner.get());session.flush();
 assert(s.memory.load(0xc0100000,4)==0xff2468ac);
 assert(g.renderer->graphics_full_clears==1);session.guard.disarm();g.graphics_owner.reset();
 std::puts("PASS FIFO late PUT after empty precheck: no unscoped CPU clear; next pass Vulkan bytes and exact packet count");
}
int main(){try{NativeVulkan probe;if(std::getenv("ANYPS3_REQUIRE_NVIDIA") && probe.device_name.find("NVIDIA")==std::string::npos)throw std::runtime_error("NVIDIA required");std::printf("DEVICE %s\n",probe.device_name.c_str());deferred21();buffer21();rtt21();idle21();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    code=graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {','class NativeVulkan {\npublic:',1)
    code=code.replace('    static void graphics_barrier(', 'public:\n    static void graphics_barrier(',1)
    marker='materialize_texture(state,t);source=t.levels[0].data;'
    assert code.count(marker)==1
    code=code.replace(marker,'materialize_texture(state,t);if(rsxvk21_after_snapshot){auto invoke=rsxvk21_after_snapshot;invoke();}source=t.levels[0].data;',1)
    marker='const bool fifo_work=s.memory.load(s.rsx.dma+0x40,4)!=s.memory.load(s.rsx.dma+0x44,4);'
    assert code.count(marker)==1
    code=code.replace(marker,marker+'if(rsxvk21_fifo_after_check){auto invoke=rsxvk21_fifo_after_check;invoke();}',1)
    return '#include <functional>\nstatic std::function<void()> rsxvk21_after_snapshot,rsxvk21_fifo_after_check;\n'+code+MAIN

class Rsxvk21Tests(unittest.TestCase):
    def test_deferred_dirty_and_rtt_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cpp=root/'test.cpp';binary=root/'test';cpp.write_text(native_source())
            built=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(built.returncode,0,built.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES') and not env.get('ANYPS3_REQUIRE_NVIDIA'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            run=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stdout+run.stderr);self.assertNotIn('Validation Warning',run.stdout+run.stderr)
            self.assertEqual(run.stdout.count('PASS '),4)
