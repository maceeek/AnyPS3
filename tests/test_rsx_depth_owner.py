"""Resident scaled depth copies obey the guest memory and observer contract."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static unsigned depth_word(unsigned x,unsigned y,unsigned generation,unsigned visual=0){return (x*65539u+y*257u+generation*1234567u+visual*731u)&0xffffffu;}
static unsigned stencil_byte(unsigned x,unsigned y,unsigned generation,unsigned visual=0){return (x*29u+y*43u+generation*71u+visual*97u)&255u;}
static void owner_case(unsigned mode){
 setenv("ANYPS3_RENDER_SCALE","2",1);setenv("ANYPS3_RSX_GPU_SCALED_DEPTH","1",1);
 const bool concurrent=mode==2 || mode==10 || mode==12,linear=mode==1;
 PpuState s;auto& m=s.memory;auto& gpu=s.rsx;gpu.local_size=0x800000;
 m.map(0xc0100000,0x10000,6,nullptr,0);m.map(0xc0200000,0x10000,6,nullptr,0);
 std::vector<std::uint8_t> fill(0x10000,0xa5);m.transfer(0xc0200000,fill.data(),fill.size(),true);
 if(concurrent)m.enable_concurrency();
 if(mode==12)m.enable_page_broker();
 GuestMemory::RawPermit raw(m);
 unsigned callbacks=0;
 if(mode==8)m.host_access_observer=[&](const void*,std::uint64_t,bool){++callbacks;};
 auto& tile=gpu.tiles[0];if(!linear){tile.bound=true;tile.location=0;tile.offset=0x100000;tile.size=0x10000;tile.pitch=256;tile.bank=1;tile.compression=9;}
 gpu.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.renderer;
 rsxgraphics::Session session(vk,s);session.observe();gpu.graphics_session=&session;
 if(concurrent){m.renderer_enabled=true;m.renderer_thread=std::this_thread::get_id();
  m.renderer_access=[&](const void* p,std::uint64_t n,bool write){if(m.observer_paused())return;
   GuestMemory::ObserverPause pause(m);
   if(m.host_access_observer && (m.host_access_observer.interested(p,n) || m.raw_cpu_dirty_interested(p,n,write)))m.host_access_observer.callback(p,n,write);};}
 auto from=rsxdraw::bind_target(s,0xfeed0000,0x100000,256,32,16);
 auto& original=session.bind(from,32,16,1,2,true);auto image=original.image;
 auto& R=gpu.registers;R[0xc184]=0xfeed0000;R[0xc198]=0x313371c3;R[0xc2fc]=1;R[0xc300]=3;R[0xc304]=3;
 R[0xc308]=R[0xc310]=0;R[0xc30c]=R[0xc314]=(8u<<16)|16;R[0xc318]=1966080;R[0xc31c]=2097152;
 R[0xc400]=(16u<<16)|32;R[0xc404]=(2u<<16)|256;R[0xc408]=0x100000;
 R[0x6188]=0xfeed0000;R[0x6300]=10;R[0x6304]=64u<<16;R[0x630c]=0x200000;
 rsxblit::Image input;input.context=0xfeed0000;input.offset=0x100000;input.pitch=256;input.bpp=4;input.width=32;input.height=16;
 auto output=input;output.offset=0x200000;output.pitch=64;output.width=16;output.height=8;
 for(unsigned generation=0;generation<2;++generation){
  for(unsigned pass=0;pass<2;++pass){const unsigned k=pass?2:1,w=32*k,h=16*k;
   std::vector<std::uint32_t> depth(w*h);std::vector<std::uint8_t> stencil(w*h);
   for(unsigned y=0;y<h;++y)for(unsigned x=0;x<w;++x){depth[y*w+x]=depth_word(x,y,generation,pass);stencil[y*w+x]=stencil_byte(x,y,generation,pass);}
   auto im=pass?image->visual:image;
   vk.graphics_transfer(*session.batch,im,VK_IMAGE_ASPECT_DEPTH_BIT,4,depth.data());
   vk.graphics_transfer(*session.batch,im,VK_IMAGE_ASPECT_STENCIL_BIT,1,stencil.data());
  }
  for(auto& surface:session.surfaces)if(surface.image==image)session.dirty_surface(surface);
  if(mode==3)for(auto& r:m.regions)if(r.base==0xc0100000)r.flags=4; // source read-only: must still succeed
  if(mode>=4){
   if(mode==4)for(auto& r:m.regions)if(r.base==0xc0200000)r.flags=4;
   if(mode==5)tile.bank^=1;
   if(mode==6)output.offset=input.offset;
   if(mode==7)input.context=0xfeed0001;
   if(mode==9)m.host_access_observer=[](const void*,std::uint64_t,bool){};
   std::shared_ptr<GuestMemory::CpuDirtyWatch> dirty;
   std::unique_ptr<GuestMemory::GpuPages> claim;
   std::optional<GuestMemory::RawYield> yield;
   if(mode==10)dirty=m.watch_cpu_dirty(m.range(0xc0200000,512,false));
   if(mode==11){GuestMemory::ObserverPause p(m);const auto r=m.range(0xc0200000,4,false);m.write_versions[reinterpret_cast<std::uintptr_t>(r.parts[0].data)/128]=0;}
   if(mode==12){yield.emplace(m);auto pin=m.regions[0].bytes;claim=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{pin->data(),4096,pin}});}
   if(mode==13){m.regions[1].bytes=m.regions[0].bytes;m.mapping_changed();}
   const auto used=session.batch->used,count=session.surfaces.size(),download=session.batch->download_bytes;
   assert(!rsxblit::scaled_depth_gpu(s,input,output,1966080,2097152));
   assert(session.batch->used==used && session.surfaces.size()==count && session.batch->download_bytes==download);
   claim.reset();yield.reset();
   if(dirty)m.unwatch_cpu_dirty(dirty);
   if(mode==5)tile.bank^=1;
   // Prior external observers and reservation marks must remain visible on fallback.
   if(mode==8 || mode==11){rsxblit::scaled(s,0);
    assert(m.load(0xc0200000,4)==((depth_word(0,0,0)<<8)|stencil_byte(0,0,0)));
    if(mode==8)assert(callbacks>0);
    if(mode==11){GuestMemory::ObserverPause p(m);auto r=m.range(0xc0200000,4,false);
     // Architectural requirement: a token acquired before the write is invalidated.
     // The established CPU batching paths may mark a granule once per run.
     assert(m.write_versions[reinterpret_cast<std::uintptr_t>(r.parts[0].data)/128]>0);}
   }
   if(mode==4){bool fault=false;try{rsxblit::scaled(s,0);}catch(const std::runtime_error&){fault=true;}
    assert(fault && m.load(0xc0200000,4)==0xa5a5a5a5);}
   session.flush(false);gpu.graphics_session=nullptr;std::printf("PASS depth-owner decline mode=%u\n",mode);return;
  }
  const auto before=rsxgraphics::fallback_stats().paths["scaled_depth_gpu"],download=session.batch->download_bytes;
  rsxblit::scaled(s,0);
  assert(rsxgraphics::fallback_stats().paths["scaled_depth_gpu"]==before+1);
  assert(session.batch->download_bytes==download);
  auto dst=std::find_if(session.surfaces.begin(),session.surfaces.end(),[](const auto& surface){return surface.target.offset==0x200000;});
  assert(dst!=session.surfaces.end() && dst->dirty && dst->depth);
  const auto z=vk.graphics_transfer(*session.batch,dst->image->visual,VK_IMAGE_ASPECT_DEPTH_BIT,4);
  const auto st=vk.graphics_transfer(*session.batch,dst->image->visual,VK_IMAGE_ASPECT_STENCIL_BIT,1);
  vk.graphics_finish(*session.batch);
  const auto* bytes=static_cast<const std::uint8_t*>(session.batch->map);
  for(unsigned y=0;y<16;++y)for(unsigned x=0;x<32;++x){std::uint32_t d;std::memcpy(&d,bytes+z+(y*32+x)*4,4);
   assert((d&0xffffff)==depth_word(x*15/8,y*2,generation,1));assert(bytes[st+y*32+x]==stencil_byte(x*15/8,y*2,generation,1));}
  session.flush(false); // reset the batch; retain GPU ownership, no host pixel commit
 }
 // First CPU reader must get the last GPU copy, and padding stays untouched.
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<16;++x){const auto actual=m.load(0xc0200000+y*64+x*4,4);
  const auto expected=(depth_word(x*15/8,y*2,1)<<8)|stencil_byte(x*15/8,y*2,1);
  if(actual!=expected){std::fprintf(stderr,"mode=%u xy=%u,%u actual=%08llx expected=%08x\n",mode,x,y,(unsigned long long)actual,expected);throw std::runtime_error("CPU reader got stale depth bytes");}}
 assert(m.load(0xc0200200,4)==0xa5a5a5a5);
 // A later CPU write invalidates the image; it cannot be hidden by retained GPU bytes.
 m.store(0xc0200000,4,0x12345678);assert(m.load(0xc0200000,4)==0x12345678);
 session.flush(false);gpu.graphics_session=nullptr;std::printf("PASS depth-owner success mode=%u\n",mode);
}
int main(){try{setvbuf(stdout,nullptr,_IONBF,0);setenv("ANYPS3_RSX_FALLBACK_STATS","1",1);
 for(unsigned mode=0;mode<14;++mode)owner_case(mode);
 }catch(const std::exception& e){std::fprintf(stderr,"DEPTH OWNER STOP %s\n",e.what());return 1;}}
'''


class DepthOwnerTests(unittest.TestCase):
    def test_guest_memory_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                                    str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                       VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            result = subprocess.run([str(root / 'test')], capture_output=True, text=True, env=env, timeout=300)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn('Validation Error', output)
            self.assertNotIn('Validation Warning', output)
            self.assertEqual(result.stdout.count('PASS depth-owner'), 14, output)
            print(result.stdout, flush=True)
