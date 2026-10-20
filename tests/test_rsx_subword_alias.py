"""GPU alias merge / unpack for 1- and 2-byte surface formats (independent byte oracle, real Vulkan).

A guest region starts with an analytic background. Old surfaces are filled (dirty) with analytic texels whose guest bytes the test
computes itself (big-endian values; RGBA8 = ARGB bytes rotated); a new surface of another format/pitch/offset is bound over them.
Expected: (1) the new image holds the big-endian decode of (background overlaid with the old surfaces' bytes) over ITS footprint,
(2) after a flush the guest region equals that overlay exactly. Layouts the device path does not take (unaligned rows/offset) must
still be exact through the readback path and are asserted to take it.
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
struct Fmt {unsigned format,bpp;bool depth;};
static const Fmt RGBA8{8,4,false},B8{9,1,false},G8B8{10,2,false},X1R5{1,2,false},R5G6B5{3,2,false},D16F{1,2,true};
struct Layout {Fmt f;unsigned offset,pitch,w,h,samples;};
struct Case {const char* name;std::vector<Layout> olds;Layout fresh;bool merge;};
static const unsigned BASE=0x10000,REGION=0x3000;
static unsigned nxs(unsigned s){return s>1?2:1;}
static unsigned nys(unsigned s){return s==4?2:1;}
static unsigned texel_bytes(Fmt f){return (f.format==3 || f.depth)?2:4;}
static void to_texel(Fmt f,const std::uint8_t* g,std::uint8_t* t){
 if(f.format==8 && !f.depth){t[0]=g[1];t[1]=g[2];t[2]=g[3];t[3]=g[0];return;}
 if(f.depth || f.format==3){t[0]=g[1];t[1]=g[0];return;}
 for(unsigned k=0;k<4;++k)t[k]=k<f.bpp?g[f.bpp-1-k]:0;
}
static void to_guest(Fmt f,const std::uint8_t* t,std::uint8_t* g){
 if(f.format==8 && !f.depth){g[1]=t[0];g[2]=t[1];g[3]=t[2];g[0]=t[3];return;}
 if(f.depth || f.format==3){g[1]=t[0];g[0]=t[1];return;}
 for(unsigned k=0;k<f.bpp;++k)g[f.bpp-1-k]=t[k];
}
// Region-relative byte address of the sample (layer,py,px): the RSX sample grid places plane l at column px*nx+l%nx, row py*ny+l/nx.
static unsigned guest_at(const Layout& l,unsigned layer,unsigned py,unsigned px){
 const unsigned nx=nxs(l.samples),ny=nys(l.samples),x=px*nx+layer%nx,yy=py*ny+layer/nx;
 return l.offset-BASE+yy*l.pitch+x*l.f.bpp;
}
static std::uint8_t hashed(unsigned seed,unsigned a,unsigned b,unsigned c,unsigned k){
 unsigned h=seed*0x9E3779B1u^a*0x85EBCA6Bu^b*0xC2B2AE35u^c*0x27D4EB2Fu^k*0x165667B1u;h^=h>>15;h*=0x2C1B3C6Du;h^=h>>12;h*=0x297A2D39u;h^=h>>15;return std::uint8_t(h);
}
static VkImageAspectFlags aspect_of(Fmt f){return f.depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT;}
static rsxdraw::Target target_of(PpuState& s,const Layout& l){
 return rsxdraw::bind_target(s,0xfeed0000,l.offset,l.pitch,l.w*nxs(l.samples),l.h*nys(l.samples),l.f.bpp);
}
// The batch's device scratch is reused across batches: fill its first 4 MiB with 0xA5 through a finished batch, so an output region the
// shader assumes to be zero (16-bit texels assembled with atomicOr) is only zero if the recorded commands zeroed it.
static void poison(NativeVulkan& vk,rsxgraphics::Session& session){
 auto& b=*session.batch;const auto at=vk.graphics_reserve_upload(b,4u<<20);std::memset(static_cast<char*>(b.map)+at,0xa5,4u<<20);session.flush(false);
}
static void run_case(const Case& c){
 const auto fail=[&](const std::string& what){throw std::runtime_error(std::string(c.name)+": "+what);};
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 std::vector<std::uint8_t> expect(REGION);
 for(unsigned i=0;i<REGION;++i){expect[i]=std::uint8_t(i*37+11);s.memory.store(0xc0000000u+BASE+i,1,expect[i]);}
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 poison(vk,session);
 unsigned seed=1;
 for(const auto& a:c.olds){
  auto t=target_of(s,a);auto& surface=session.bind(t,a.w,a.h,a.samples,a.f.format,a.f.depth);
  const unsigned tb=texel_bytes(a.f);std::vector<std::uint8_t> texels(std::size_t(a.w)*a.h*a.samples*tb);
  for(unsigned layer=0;layer<a.samples;++layer)for(unsigned py=0;py<a.h;++py)for(unsigned px=0;px<a.w;++px){
   std::uint8_t g[4]{};for(unsigned k=0;k<a.f.bpp;++k)g[k]=hashed(seed,layer,py,px,k);
   to_texel(a.f,g,texels.data()+((std::size_t(layer)*a.h+py)*a.w+px)*tb);
   const unsigned at=guest_at(a,layer,py,px);if(at+a.f.bpp>REGION)fail("old surface outside the test region");
   for(unsigned k=0;k<a.f.bpp;++k)expect[at+k]=g[k];}
  vk.graphics_transfer(*session.batch,surface.image,aspect_of(a.f),tb,texels.data());session.dirty_surface(surface);++seed;
 }
 const auto merges0=RsxPerfCounters::values[RsxPerfCounters::alias_merges].load(),flushes0=RsxPerfCounters::values[RsxPerfCounters::alias_flushes].load();
 const auto unpacks0=vk.graphics_surface_uploads;
 const auto& b=c.fresh;auto t=target_of(s,b);auto& fresh=session.bind(t,b.w,b.h,b.samples,b.f.format,b.f.depth);
 const auto image=fresh.image;
 const auto merges=RsxPerfCounters::values[RsxPerfCounters::alias_merges].load()-merges0,flushes=RsxPerfCounters::values[RsxPerfCounters::alias_flushes].load()-flushes0;
 const auto unpacks=vk.graphics_surface_uploads-unpacks0;
 if(c.merge){if(merges!=1 || flushes!=0 || unpacks!=1)fail("expected one device merge and no readback flush (merges="+std::to_string(merges)+" flushes="+std::to_string(flushes)+" unpacks="+std::to_string(unpacks)+")");}
 else if(merges!=0)fail("layout must not use the device merge");
 // (1) the fresh image holds the big-endian decode of the expected bytes over its own footprint.
 session.flush(false);
 {auto rb=vk.graphics_batch(16*1024*1024);const unsigned tb=texel_bytes(b.f);
  const auto at=vk.graphics_transfer(*rb,image,aspect_of(b.f),tb);vk.graphics_finish(*rb);
  const auto* out=static_cast<const std::uint8_t*>(rb->map)+at;
  for(unsigned layer=0;layer<b.samples;++layer)for(unsigned py=0;py<b.h;++py)for(unsigned px=0;px<b.w;++px){
   const unsigned from=guest_at(b,layer,py,px);if(from+b.f.bpp>REGION)fail("fresh surface outside the test region");
   std::uint8_t want[4]{};to_texel(b.f,expect.data()+from,want);
   const auto* got=out+((std::size_t(layer)*b.h+py)*b.w+px)*tb;
   for(unsigned k=0;k<tb;++k)if(got[k]!=want[k])fail("texel mismatch layer="+std::to_string(layer)+" x="+std::to_string(px)+" y="+std::to_string(py)+" byte="+std::to_string(k)+" got="+std::to_string(got[k])+" want="+std::to_string(want[k]));}
 }
 // (2) after the flush the guest region is exactly background + old surfaces' bytes (the fresh surface rewrote its own footprint).
 session.flush();
 {GuestMemory::ObserverPause pause(s.memory);
  for(unsigned i=0;i<REGION;++i)if(std::uint8_t(s.memory.load(0xc0000000u+BASE+i,1))!=expect[i])fail("guest byte mismatch at +"+std::to_string(i));}
 s.rsx.graphics_session=nullptr;
 std::printf("PASS subword alias %s merges=%llu flushes=%llu\n",c.name,(unsigned long long)merges,(unsigned long long)flushes);
}
static void upload_only(const char* name,Layout b,bool device){
 // No alias: the first bind of a surface loads the guest background; whole-word and sub-word layouts take the device unpack.
 Case c{name,{},b,false};
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 std::vector<std::uint8_t> expect(REGION);for(unsigned i=0;i<REGION;++i){expect[i]=std::uint8_t(i*37+11);s.memory.store(0xc0000000u+BASE+i,1,expect[i]);}
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 poison(vk,session);
 const auto unpacks0=vk.graphics_surface_uploads;
 auto t=target_of(s,b);auto& fresh=session.bind(t,b.w,b.h,b.samples,b.f.format,b.f.depth);const auto image=fresh.image;
 if((vk.graphics_surface_uploads-unpacks0)!=(device?1u:0u))throw std::runtime_error(std::string(name)+": unexpected unpack count");
 session.flush(false);
 auto rb=vk.graphics_batch(16*1024*1024);const unsigned tb=texel_bytes(b.f);const auto at=vk.graphics_transfer(*rb,image,aspect_of(b.f),tb);vk.graphics_finish(*rb);
 const auto* out=static_cast<const std::uint8_t*>(rb->map)+at;
 for(unsigned layer=0;layer<b.samples;++layer)for(unsigned py=0;py<b.h;++py)for(unsigned px=0;px<b.w;++px){
  std::uint8_t want[4]{};to_texel(b.f,expect.data()+guest_at(b,layer,py,px),want);
  const auto* got=out+((std::size_t(layer)*b.h+py)*b.w+px)*tb;
  for(unsigned k=0;k<tb;++k)if(got[k]!=want[k])throw std::runtime_error(std::string(name)+": upload texel mismatch x="+std::to_string(px)+" y="+std::to_string(py));}
 s.rsx.graphics_session=nullptr;std::printf("PASS subword upload %s device=%d\n",name,int(device));(void)c;
}
int main(){try{
 const std::vector<Case> cases={
  // 1/2-byte NEW surface over whole-word / sub-word / mixed OLD surfaces (device merge expected)
  {"rgba8_to_b8",{{RGBA8,BASE,64,8,8,1}},{B8,BASE+16,64,16,12,1},true},
  {"b8_to_b8_shifted",{{B8,BASE,32,16,8,1}},{B8,BASE+4+2*32,32,12,6,1},true},
  {"b8_pitch_change",{{B8,BASE,32,16,8,1}},{B8,BASE+8,48,12,10,1},true},
  {"g8b8_to_g8b8",{{G8B8,BASE,32,8,8,1}},{G8B8,BASE+4,32,8,8,1},true},
  {"x1r5g5b5_over_rgba8",{{RGBA8,BASE,64,8,8,1}},{X1R5,BASE+8,64,12,8,1},true},
  {"r5g6b5_16bit_texels",{{R5G6B5,BASE,32,8,8,1}},{R5G6B5,BASE+4,32,8,8,1},true},
  {"r5g6b5_over_rgba8",{{RGBA8,BASE,64,8,8,1}},{R5G6B5,BASE+8,64,12,8,1},true},
  {"rgba8_over_b8",{{B8,BASE,32,16,8,1}},{RGBA8,BASE+8,64,6,8,1},true},
  {"d16_over_g8b8",{{G8B8,BASE,32,8,8,1}},{D16F,BASE+4,32,8,8,1},true},
  {"d16_over_d16",{{D16F,BASE,32,12,8,1}},{D16F,BASE+8,64,8,8,1},true},
  {"two_olds_b8",{{B8,BASE,32,16,4,1},{RGBA8,BASE+32*4+16,64,4,4,1}},{B8,BASE+8,32,24,6,1},true},
  {"unaligned_old_b8",{{B8,BASE+1,33,16,8,1}},{B8,BASE+16,32,16,12,1},true},
  {"msaa2_b8",{{B8,BASE,64,16,8,2}},{B8,BASE+8,64,8,8,2},true},
  {"msaa2_r5g6b5_odd_plane_width",{{R5G6B5,BASE,64,6,8,2}},{R5G6B5,BASE+4,64,3,8,2},true},
  {"msaa4_g8b8",{{G8B8,BASE,64,4,4,4}},{G8B8,BASE+4,64,4,4,4},true},
  // layouts the device path must not take: still exact through the readback path
  {"row_not_word_multiple_b8",{{B8,BASE,32,16,8,1}},{B8,BASE+4,32,14,8,1},false},
  {"odd_offset_b8",{{B8,BASE,32,16,8,1}},{B8,BASE+2,32,16,8,1},false},
  {"odd_width_r5g6b5",{{R5G6B5,BASE,32,8,8,1}},{R5G6B5,BASE+4,32,7,8,1},false},
 };
 for(const auto& c:cases)run_case(c);
 upload_only("b8",{B8,BASE+4,32,16,8,1},true);
 upload_only("g8b8",{G8B8,BASE,32,8,8,1},true);
 upload_only("r5g6b5",{R5G6B5,BASE+8,32,8,8,1},true);
 upload_only("d16",{D16F,BASE,32,8,8,1},true);
 upload_only("b8_msaa2",{B8,BASE,64,8,8,2},true);
 upload_only("b8_unaligned_rows",{B8,BASE+4,32,14,8,1},false);
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class SubwordAliasTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_subword_alias_merge_and_unpack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_FRAME_DIR=str(root), VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            if os.environ.get('ANYPS3_REQUIRE_NVIDIA'):
                env.update(ANYPS3_VK_DEVICE='gpu')
            elif not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('Validation Error', result.stdout + result.stderr)
            self.assertNotIn('Validation Warning', result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS subword alias'), 18, result.stdout)
            self.assertEqual(result.stdout.count('PASS subword upload'), 6, result.stdout)


if __name__ == '__main__':
    unittest.main()
