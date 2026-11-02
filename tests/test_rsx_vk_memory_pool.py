"""Vulkan device-memory pool: sub-allocated ranges never alias and carry exactly their owner's bytes (native oracle, real Vulkan).

The pool (rsx_vk_resources.py) serves device-local allocations as ranges of large driver blocks. Lavapipe has no
device-local-only memory type, so the test enables the HOST_COHERENT test hook (ANYPS3_RSX_MEMORY_POOL_TYPES=coherent)
and small blocks (ANYPS3_RSX_MEMORY_POOL_MB=1) so that one test exercises multi-range blocks, best-fit reuse of freed
holes, coalescing, the exact-allocation path for requests above half a block and the release of excess empty blocks.

Oracles are independent of the allocator: every surface is filled with its own byte pattern, surfaces are released in an
interleaved order while others stay alive, new surfaces are placed into the freed space, and the guest bytes committed by
the session must equal each surface's own pattern; the same rounds with the pool off (ANYPS3_RSX_MEMORY_POOL=0) must write
byte-identical guest memory. The allocator's structure (ranges tile each block, free neighbours coalesced, counts) is
checked by NativeVulkan::memory_pool_report after every step.
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
static std::uint64_t counter(RsxPerfCounters::Id id){return RsxPerfCounters::values[id].load();}
static std::uint8_t pattern(unsigned round,unsigned id,std::size_t i){return std::uint8_t(i*13+id*71+round*29+1+(i>>8));}
struct Placed {unsigned id,base,width,height;std::shared_ptr<NativeVulkan::GraphicsImage> image;};
struct Result {std::vector<std::uint8_t> guest;std::uint64_t ranges=0,driver_allocs=0,driver_frees=0,max_blocks=0,max_live=0;NativeVulkan::PoolReport end;};
static Result rounds(bool pool){
 setenv("ANYPS3_RSX_MEMORY_POOL",pool?"1":"0",1);setenv("ANYPS3_RSX_MEMORY_POOL_TYPES","coherent",1);setenv("ANYPS3_RSX_MEMORY_POOL_MB","1",1);
 setenv("ANYPS3_RSX_MEMORY_CACHE_MB","1",1);   // retained empty blocks: at most one 1 MiB block
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 Result out;
 auto session_owner=std::make_unique<rsxgraphics::Session>(vk,s);auto& session=*session_owner;session.observe();s.rsx.graphics_session=&session;const auto ranges0=counter(RsxPerfCounters::vk_pool_ranges),allocs0=counter(RsxPerfCounters::vk_memory_allocs),frees0=counter(RsxPerfCounters::vk_memory_frees);
 auto check=[&]{const auto r=NativeVulkan::memory_pool_report(vk.device);out.max_blocks=std::max(out.max_blocks,r.blocks);out.max_live=std::max(out.max_live,r.live);
  if(!pool && r.blocks){throw std::runtime_error("pool off but pool blocks exist");}return r;};
 // Extents (bytes = 4*w*h): 1 KiB .. 256 KiB pooled with 1 MiB blocks; 1024x256 (1 MiB) takes the exact path.
 const unsigned extents[][2]={{16,16},{64,32},{128,128},{32,8},{256,64},{8,8},{96,48},{256,256},{24,40},{1024,256},{64,64},{200,8}};
 std::vector<Placed> alive;unsigned next_base=0x10000,id=0;
 auto place=[&](unsigned round,unsigned w,unsigned h){
  Placed p{id++,next_base,w,h,{}};next_base+=(w*h*4+0xffffu)&~0xffffu;if(next_base>0x7f0000)throw std::runtime_error("test: guest local memory exhausted");
  auto t=rsxdraw::bind_target(s,0xfeed0000,p.base,w*4,w,h);auto& surface=session.bind(t,w,h,1,8,false);p.image=surface.image;
  std::vector<std::uint8_t> pixels(std::size_t(w)*h*4);for(std::size_t i=0;i<pixels.size();++i)pixels[i]=pattern(round,p.id,i);
  vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(surface);
  alive.push_back(std::move(p));};
 auto verify=[&](unsigned round,const Placed& p){GuestMemory::ObserverPause pause(s.memory);
  // Surface bytes are A8R8G8B8 big endian; the image texel bytes [R,G,B,A] land as guest bytes [A,R,G,B].
  for(std::size_t i=0;i<std::size_t(p.width)*p.height*4;++i){const auto texel=i&~std::size_t(3),c=i&3;
   const auto expect=pattern(round,p.id,texel+(c==0?3:c-1));
   const auto got=std::uint8_t(s.memory.load(0xc0000000u+p.base+i,1));
   if(got!=expect)throw std::runtime_error("surface "+std::to_string(p.id)+" byte "+std::to_string(i)+" = "+std::to_string(got)+", expected "+std::to_string(expect));}};
 // Called after a session flush: the surface is committed, so dropping it loses no bytes.
 auto drop=[&](const Placed& p){
  session.surfaces.erase(std::remove_if(session.surfaces.begin(),session.surfaces.end(),[&](const auto& x){return x.image==p.image;}),session.surfaces.end());
  session.refresh_surface_watches();};
 for(unsigned round=0;round<3;++round){
  for(const auto& e:extents)place(round,e[0],e[1]);
  session.flush();check();
  // Release every other surface (verified on release) while its neighbours stay alive: holes between live ranges.
  std::vector<Placed> keep;
  for(std::size_t i=0;i<alive.size();++i){auto& p=alive[i];if(i%2){session.flush();verify(round,p);drop(p);p.image.reset();session.flush();check();}else keep.push_back(std::move(p));}
  alive=std::move(keep);
  // New surfaces of other sizes go into the freed space; the survivors must keep their bytes.
  const unsigned refill[][2]={{48,16},{128,64},{8,200},{40,40},{160,96}};
  for(const auto& e:refill)place(round,e[0],e[1]);
  session.flush();check();
  // Every surface alive here was filled in this round (the previous round released all of its surfaces).
  for(const auto& p:alive)verify(round,p);
  for(auto& p:alive){drop(p);p.image.reset();}
  alive.clear();session.flush();check();
 }
 session.clear_surfaces();session.flush();
 {GuestMemory::ObserverPause pause(s.memory);for(unsigned i=0x10000;i<next_base;++i)out.guest.push_back(std::uint8_t(s.memory.load(0xc0000000u+i,1)));}
 // The session's caches (recycled surface images, batches) die with it: every range it took must come back.
 s.rsx.graphics_session=nullptr;session_owner.reset();out.end=check();
 out.ranges=counter(RsxPerfCounters::vk_pool_ranges)-ranges0;out.driver_allocs=counter(RsxPerfCounters::vk_memory_allocs)-allocs0;out.driver_frees=counter(RsxPerfCounters::vk_memory_frees)-frees0;
 return out;
}
static void pool_rounds(){
 const auto off=rounds(false),on=rounds(true);
 if(off.guest.size()!=on.guest.size() || off.guest!=on.guest)throw std::runtime_error("guest bytes differ between pool on and off");
 if(off.ranges || off.max_blocks)throw std::runtime_error("pool off still sub-allocated");
 if(!on.ranges || on.max_blocks<2)throw std::runtime_error("pool on: too few ranges/blocks ("+std::to_string(on.ranges)+"/"+std::to_string(on.max_blocks)+")");
 // Ranges outnumber the driver allocations: ranges shared blocks and freed holes were reused.
 if(on.driver_allocs*2>off.driver_allocs)throw std::runtime_error("pool on: "+std::to_string(on.driver_allocs)+" driver allocations vs "+std::to_string(off.driver_allocs)+" without the pool");
 // Every range came back with the session; at most one empty block is retained (limit 1 MiB = one block).
 if(on.end.live || on.end.blocks>1)throw std::runtime_error("pool on: after the session "+std::to_string(on.end.live)+" live ranges, "+std::to_string(on.end.blocks)+" retained blocks (limit 1)");
 if(!on.driver_frees)throw std::runtime_error("pool on: excess empty blocks were never released");
 std::printf("PASS pooled ranges keep their own bytes (ranges=%llu driver allocs %llu vs %llu, peak blocks %llu, peak live %llu)\n",(unsigned long long)on.ranges,
  (unsigned long long)on.driver_allocs,(unsigned long long)off.driver_allocs,(unsigned long long)on.max_blocks,(unsigned long long)on.max_live);
}
static void invalid_settings(){
 const char* cases[][2]={{"ANYPS3_RSX_MEMORY_POOL","2"},{"ANYPS3_RSX_MEMORY_POOL_MB","0"},{"ANYPS3_RSX_MEMORY_POOL_MB","4097"},{"ANYPS3_RSX_MEMORY_POOL_TYPES","all"}};
 for(const auto& c:cases){
  setenv("ANYPS3_RSX_MEMORY_POOL","1",1);setenv("ANYPS3_RSX_MEMORY_POOL_MB","1",1);setenv("ANYPS3_RSX_MEMORY_POOL_TYPES","coherent",1);setenv(c[0],c[1],1);
  bool failed=false;try{NativeVulkan vk;}catch(const std::runtime_error& e){failed=std::string(e.what()).find(c[0])!=std::string::npos;}
  if(!failed)throw std::runtime_error(std::string("accepted ")+c[0]+"="+c[1]);
 }
 setenv("ANYPS3_RSX_MEMORY_POOL","1",1);setenv("ANYPS3_RSX_MEMORY_POOL_MB","1",1);
 std::puts("PASS invalid pool settings stop explicitly");
}
int main(){try{pool_rounds();invalid_settings();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxVkMemoryPoolTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_pooled_device_memory_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, LP_NUM_THREADS='4', VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('Validation Error', result.stdout + result.stderr)
            self.assertNotIn('Validation Warning', result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 2, result.stdout)


if __name__ == '__main__':
    unittest.main()
