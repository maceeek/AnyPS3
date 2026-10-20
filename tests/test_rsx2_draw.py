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
// Same four vertices: separate buffers (baseline), attribute 1 touching the end of attribute 0's
// predecessor (adjacent, attribute 0 sits at a non-zero offset in the shared range), and attribute 1
// overlapping attribute 0's bytes. All three must render the same pixels; the shared layouts must
// read each guest byte once (vertex_bytes is the union of the attribute spans).
static void vertex_groups_(){
 auto render=[&](int layout,std::uint64_t& vertex_bytes){
  PpuState s;setup_draw(s,16,8,64,false);auto& R=s.rsx.registers;auto& m=s.memory;
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
 // The quad covers the whole 16x8 target: not an empty comparison.
 unsigned lit=0;for(auto p:base)lit+=p!=0;assert(lit>=100);
 assert(adjacent==base && overlap==base);
 assert(b0==64+32);   // attr0 16*3+16, attr1 8*3+8: disjoint buffers
 assert(b1==64+32);   // adjacent spans: union is the sum
 assert(b2==64);      // attr1 (offset 8, stride 16: 16*3+8) lies inside attr0's span
 std::puts("PASS interleaved vertex attributes share one guest range with identical pixels");
}
// Device-local memory is recycled by (type,size). Oracle: the same rounds with the recycler
// disabled (ANYPS3_RSX_MEMORY_CACHE_MB=0) must write byte-identical guest memory.
static std::vector<std::uint8_t> memory_rounds_(bool recycle,std::uint64_t& reuses,bool& cacheable){
 if(recycle)unsetenv("ANYPS3_RSX_MEMORY_CACHE_MB");else setenv("ANYPS3_RSX_MEMORY_CACHE_MB","0",1);
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
static void memory_reuse_(){
 std::uint64_t off_reuses=0,on_reuses=0;bool off_cacheable=false,on_cacheable=false;
 const auto off=memory_rounds_(false,off_reuses,off_cacheable);const auto on=memory_rounds_(true,on_reuses,on_cacheable);
 assert(off_reuses==0 && off.size()==on.size() && off==on);
 // The written pixels reached guest memory (not a vacuous equality of untouched rows).
 bool changed=false;for(unsigned i=0;i<64;++i)changed|=on[i]!=std::uint8_t((0x11110000u+(i&~3u))>>(24-8*(i&3)));assert(changed);
 if(!on_cacheable)assert(on_reuses==0);  // reuse count on a discrete GPU depends on batch retirement timing: reported, not asserted
 std::printf("INFO memory cacheable=%d reuses=%llu\n",int(on_cacheable),(unsigned long long)on_reuses);
 std::puts("PASS recycled device memory carries only the new owner's bytes");
}
int main(){try{vertex_groups_();memory_reuse_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
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
                self.assertEqual(result.stdout.count('PASS '), 2, result.stdout)


if __name__ == '__main__':
    unittest.main()
