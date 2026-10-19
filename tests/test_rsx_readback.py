"""Fixed byte oracles for GPU readback and persistent host page protection."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN=r'''
static void intervals(){
 const auto page=GuestMemory::HostGuard::page_size();GuestBacking memory(page*8);
 auto at=(reinterpret_cast<std::uintptr_t>(memory.data())+page-1)&~(page-1);
 auto* data=reinterpret_cast<std::uint8_t*>(at);GuestMemory::HostGuard guard;
 assert(guard.add(data+page*2,page,memory));assert(guard.add(data,page,memory));assert(guard.add(data+page,page,memory));
 guard.arm();assert(guard.spans.size()==1 && guard.protection_calls==1);
 for(unsigned i=0;i<1000;++i){guard.arm();}assert(guard.protection_calls==1);
 guard.release(data+page+2,1);assert(guard.protection_calls==2 && !guard.touches(data+page,page));data[page]=0x55;
 guard.add_span({at+page,at+page*2});guard.arm();assert(guard.protection_calls==3);
 guard.disarm();assert(guard.protection_calls==4 && data[page]==0x55);
 unsigned faults=0;guard.synchronize=[&]{++faults;data[0]=0x67;};assert(guard.add(data,1,memory));guard.arm();
 volatile auto* retained=data;assert(retained[0]==0x67 && faults==1);retained[0]=0x23;assert(data[0]==0x23);
 assert(guard.add(data,page,memory,true));guard.arm();const auto count=faults;assert(retained[0]==0x23 && faults==count);
 retained[0]=0x77;assert(faults==count+1 && data[0]==0x77);
 std::puts("PASS persistent intervals, exact syscall counts, partial release, retained pointer fault");
}
static void pack_bits(){
 PpuState s;s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 auto batch=vk.graphics_batch();
 // Fixed raw half-float bytes include negative zero, signaling NaN payload,
 // subnormal, and 1.0. They must never pass through a float conversion.
 const std::uint16_t half[]={0x8000,0x7c01,0x0001,0x3c00};
 auto image=vk.graphics_image(*batch,VK_FORMAT_R16G16B16A16_SFLOAT,1,1,1);vk.graphics_transfer(*batch,image,VK_IMAGE_ASPECT_COLOR_BIT,8,half);
 std::vector<unsigned> gather{4,0};std::shared_ptr<NativeVulkan::GraphicsBuffer> map;
 auto result=vk.graphics_pack_readback(*batch,image,11,false,8,8,1,gather,map);vk.graphics_finish(*batch);
 const std::uint8_t expected[]={0x00,0x01,0x3c,0x00,0x80,0x00,0x7c,0x01};assert(std::memcmp(static_cast<const char*>(batch->map)+result,expected,8)==0);vk.graphics_reset(*batch);
 // Two planes interleave into one guest row; reversed physical word order is
 // deliberate, so a linear memcpy/byte-swap-only implementation fails.
 auto planes=vk.graphics_image(*batch,VK_FORMAT_R8G8B8A8_UNORM,2,1,2);
 const std::uint8_t rgba[]={1,2,3,4, 5,6,7,8, 9,10,11,12, 13,14,15,16};vk.graphics_transfer(*batch,planes,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba);
 gather={12,0,8,4};map.reset();result=vk.graphics_pack_readback(*batch,planes,8,false,4,16,2,gather,map);vk.graphics_finish(*batch);
 const std::uint8_t pixels[]={16,13,14,15, 4,1,2,3, 8,5,6,7, 12,9,10,11};assert(std::memcmp(static_cast<const char*>(batch->map)+result,pixels,16)==0);vk.graphics_reset(*batch);
 // One 16-bit texel: high output bytes are padding and CPU must not copy them.
 auto small=vk.graphics_image(*batch,VK_FORMAT_D16_UNORM,1,1,1,true);const std::uint16_t depth=0x1234;vk.graphics_transfer(*batch,small,VK_IMAGE_ASPECT_DEPTH_BIT,2,&depth);
 gather={0};map.reset();result=vk.graphics_pack_readback(*batch,small,1,true,2,2,1,gather,map);vk.graphics_finish(*batch);
 const std::uint8_t edge[]={0x12,0x34,0,0};assert(std::memcmp(static_cast<const char*>(batch->map)+result,edge,4)==0);
 std::puts("PASS physical-order GPU gather: raw FP16 payloads, MSAA planes, depth edge");
}

static void sample_grid(bool split=false){
 PpuState s;setup_draw(s,8,8,256,false);auto& gpu=s.rsx;auto& m=s.memory;
 auto word=[&](unsigned address){std::uint8_t bytes[4];m.range(address,4,false).read(0,bytes,4);return rsxdraw::read_be(bytes,4);};
 auto& tile=gpu.tiles[0];tile.bound=true;tile.location=0;tile.offset=0x80000;tile.size=0x10000;tile.pitch=256;
 if(split){const auto old=m.regions[1];const unsigned cut=0x80001;auto first=std::make_shared<GuestBacking>(old.bytes->begin(),old.bytes->begin()+cut),last=std::make_shared<GuestBacking>(old.bytes->begin()+cut,old.bytes->end());m.regions[1]={old.base,6,first};m.regions.push_back({old.base+cut,6,last});}
 gpu.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.renderer;rsxgraphics::Session session(vk,s);session.observe();gpu.graphics_session=&session;
 auto t=rsxdraw::bind_target(s,0xfeed0000,0x80000,256,16,8);
 auto& source=session.bind(t,8,8,2,8,false);
 std::vector<std::uint8_t> pixels(8*8*2*4);
 for(unsigned i=0;i<128;++i){const std::uint8_t a[]={0x11,0x22,0x33,0x44},b[]={0x55,0x66,0x77,0x88};std::memcpy(pixels.data()+i*4,i<64?a:b,4);}
 vk.graphics_transfer(*session.batch,source.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(source);
 m.store(0xc0082000,4,0xdeadbeef);const auto before=session.batch->download_bytes;
 const auto submissions=session.batch->submissions;
 auto small=rsxdraw::bind_target(s,0xfeed0000,0x80000,256,8,8);session.bind(small,8,8,1,8,false);
 if(!split)assert(session.batch->submissions==submissions); // remainder has no reader yet
 session.flush();
 // Literal RSX tile addresses: first 32B at +0, next 32B at +0x100.
 // Reducing 2x->1x must preserve the old pixels outside the new width.
 for(unsigned x=0;x<8;++x){const unsigned expected=x%2?0x88556677:0x44112233;assert(word(0xc0080000+x*4)==expected);assert(word(0xc0080100+x*4)==expected);}
 assert(word(0xc0082000)==0xdeadbeef);
 // Only 8*8*4 uncovered bytes plus the new 8*8*4 target are read back;
 // internal graphics status words are 4 bytes each, never a full 2x image.
 if(!split)assert(session.batch->download_bytes-before==8*8*4*2+4);
 else assert(session.batch->download_bytes-before>=8*8*4*2);
 auto& current=session.bind(small,8,8,1,8,false);std::vector<std::uint8_t> changed(8*8*4);
 for(unsigned i=0;i<64;++i){const std::uint8_t rgba[]={0xab,0xcd,0xef,0x01};std::memcpy(changed.data()+4*i,rgba,4);}
 vk.graphics_transfer(*session.batch,current.image,VK_IMAGE_ASPECT_COLOR_BIT,4,changed.data());session.dirty_surface(current);
 const auto previous=session.batch->download_bytes;session.bind(t,8,8,2,8,false);if(!split)assert(session.batch->download_bytes==previous+4);else assert(session.batch->download_bytes>=previous+8*8*4);
 session.flush();for(unsigned x=0;x<8;++x){assert(word(0xc0080000+x*4)==0x01abcdef);assert(word(0xc0080100+x*4)==(x%2?0x88556677:0x44112233));}
 gpu.graphics_session=nullptr;
 std::puts("PASS sample-grid alias: fixed physical bytes, preserved remainder, partial readback, GPU merge");
}

static void allocator_edges(){
 PpuState s;setup_draw(s,128,64,512,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto target=rsxdraw::bind_target(s,0xfeed0000,0,512,128,64);auto& surface=session.bind(target,128,64,1,8,false);
 // The first allocation page contains allocator metadata and cannot be
 // protected. Test whichever actual allocation-edge spans this host creates.
 assert(surface.protectable);if(surface.exposed.empty()){s.rsx.graphics_session=nullptr;std::puts("PASS allocator edge backing happened to be page-aligned");return;}
 std::vector<std::uint8_t> rgba(128*64*4);for(unsigned i=0;i<128*64;++i){const std::uint8_t value[]={0x11,0x22,0x33,0x44};std::memcpy(rgba.data()+4*i,value,4);}
 vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba.data());session.dirty_surface(surface);
 const auto edge=surface.exposed.front();volatile auto* raw=reinterpret_cast<std::uint8_t*>(edge.begin);auto* inside=reinterpret_cast<volatile std::uint8_t*>(surface.protection.front().begin);
 s.rsx.graphics_session=nullptr;const auto download=session.batch->download_bytes;session.leave_fifo();
 assert(session.surfaces.size()==1 && !session.guard.spans.empty());assert(session.batch->download_bytes-download<=8192);
 assert(raw[0]==0x44 && raw[1]==0x11 && raw[2]==0x22 && raw[3]==0x33);
 const auto stable=session.batch->download_bytes;for(unsigned i=0;i<8;++i)session.leave_fifo();assert(session.batch->download_bytes==stable);
 // A raw edge write followed immediately by an interior protected read must
 // not be overwritten by the signal handler's synchronization.
 raw[0]=0x12;const auto value=inside[0];(void)value;assert(raw[0]==0x12 && raw[1]==0x11 && session.surfaces.empty());
 // Explicit FIFO entry must detect another raw write, without a fault.
 s.rsx.graphics_session=&session;auto& next=session.bind(target,128,64,1,8,false);vk.graphics_transfer(*session.batch,next.image,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba.data());session.dirty_surface(next);s.rsx.graphics_session=nullptr;session.leave_fifo();raw[1]=0xab;
 s.video.format=0;s.rsx.graphics_owner=std::shared_ptr<void>(&session,[](void*){});s.rsx.display_buffers[0]={128,64,512,0};std::vector<std::uint8_t> capture;assert(!rsx_graphics_scanout(s,0,&capture,false));assert(session.surfaces.empty() && raw[0]==0x44 && raw[1]==0xab && raw[2]==0x22 && raw[3]==0x33);
 s.rsx.graphics_owner.reset();
 std::puts("PASS allocator edge partial commit, persistent interior, no repeated readback, raw writes before fault/FIFO");
}

static void snapshot_pages(){
 // Backings start on a host page; ending the local backing inside a page makes
 // the texture's last host page unguardable, so its snapshot stays untrusted.
 for(bool buffer:{false,true}){
  PpuState s;setup_draw(s,8,64,256,false);{auto& b=s.memory.regions[1].bytes;b=std::make_shared<GuestBacking>(b->begin(),b->begin()+0x7ffff0);}
  const unsigned at=buffer?0x7fe000:0x7fbf80;s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
  rsxdraw::Texture texture;
  if(buffer){texture.levels.resize(1);texture.levels[0].data=s.memory.regions[1].bytes->data()+at;texture.storage_size=4100;session.texture_buffer(texture);}
  else{auto& r=s.rsx.registers;r[0x1a00]=at;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=(8u<<16)|64u;r[0x1840]=256;
   for(unsigned y=0;y<64;++y)for(unsigned x=0;x<8;++x)s.memory.store(0xc0000000+at+y*256+x*4,4,0xff112233);
   texture=session.texture_metadata(0);session.texture_image(texture);}
  assert(!session.raw_textures.empty() && !session.raw_textures.back().trusted);
  const unsigned offset=at+(buffer?0x1040:64),height=buffer?8:64;auto target=rsxdraw::bind_target(s,0xfeed0000,offset,256,8,height);auto& surface=session.bind(target,8,height,1,8,false);
  std::vector<std::uint8_t> pixels(8*height*4,0x55);vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(surface);
  s.rsx.graphics_session=nullptr;session.leave_fifo();session.guard.synchronize=[]{assert(false && "snapshot synchronization must precede any cache entry reference");};
  s.rsx.graphics_session=&session;
  if(buffer)assert(session.texture_buffer(texture));else assert(session.texture_image(texture));
  session.guard.disarm();session.flush();s.rsx.graphics_session=nullptr;
  if(!buffer){assert(s.memory.load(0xc0000000+at,4)==0xff112233);assert(s.memory.load(0xc0003f00+at,4)==0xff112233);}
 }
 std::puts("PASS untrusted image/buffer snapshots synchronize shared pages before acquiring cache references");
}

static void large_readback(){
 PpuState s;setup_draw(s,3840,2160,15360,false);s.rsx.local_size=64*1024*1024;s.memory.regions[1].bytes->resize(s.rsx.local_size);
 s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,15360,3840,2160);auto& surface=session.bind(target,3840,2160,1,8,false);
 std::vector<std::uint8_t> rgba(3840u*2160u*4u);for(unsigned i=0;i<3840u*2160u;++i){const std::uint8_t value[]={0x11,0x22,0x33,0x44};std::memcpy(rgba.data()+4*i,value,4);}
 // Complete the initial upload to keep this probe independent of upload
 // capacity policy. A 33MB readback fits; a gather table+output would not.
 session.flush(false);vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba.data());session.dirty_surface(surface);session.flush();
 assert(vk.graphics_readback_dispatches==0);assert(s.memory.load(0xc0010000,4)==0x44112233);assert(s.memory.load(0xc0010000+3840u*2160u*4u-4,4)==0x44112233);s.rsx.graphics_session=nullptr;
 std::puts("PASS 4K readback preserves legacy staging limits when GPU gather would exceed capacity");
}

static void protected_backing_lifetime(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 rsxdraw::Texture texture;texture.levels.resize(1);texture.levels[0].data=s.memory.regions[1].bytes->data()+0x2000;texture.storage_size=16;
 session.texture_buffer(texture);assert(session.raw_textures.back().trusted);std::weak_ptr<GuestBacking> backing=s.memory.regions[1].bytes;
 s.rsx.graphics_session=nullptr;session.leave_fifo();assert(!session.guard.armed_readonly.empty());
 s.memory.regions.erase(s.memory.regions.begin()+1);session.clear_raw(nullptr);
 assert(!backing.expired());session.leave_fifo();assert(backing.expired() && session.guard.armed_readonly.empty());
 std::puts("PASS protected backing stays pinned through unmap/eviction until permissions are released");
}
int main(){try{intervals();pack_bits();sample_grid();sample_grid(true);allocator_edges();snapshot_pages();large_readback();protected_backing_lifetime();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+MAIN

class ReadbackTests(unittest.TestCase):
    def test_persistent_guard_and_byte_exact_gpu_gather(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.cpp').write_text(native_source())
            p=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(p.returncode,0,p.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            p=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertNotIn('Validation Error',p.stdout+p.stderr);self.assertNotIn('Validation Warning',p.stdout+p.stderr)
            self.assertEqual(p.stdout.count('PASS '),8)
