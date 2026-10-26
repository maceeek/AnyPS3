"""Visual companions keep subpixel history through native alias transitions.

The oracle maps texels through guest byte addresses (offset, pitch and the RSX
2x1/2x2 sample interleave), independent of the readback word tables that the
runtime planner uses.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void geometry_cache(){
 const bool structural=std::strcmp(std::getenv("ANYPS3_RSX_HISTORY_GEOMETRY_CACHE"),"0")!=0;
 setenv("ANYPS3_RENDER_SCALE","2",1);
 PpuState s;setup_draw(s,128,128,512,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 rsxgraphics::Surface a{},c{};a.width=c.width=128;a.height=c.height=128;a.samples=c.samples=1;
 a.format=c.format=8;a.target.bpp=c.target.bpp=4;a.target.pitch=c.target.pitch=512;
 a.image=vk.graphics_image(*session.batch,VK_FORMAT_R8G8B8A8_UNORM,128,128,1);c.image=a.image;
 a.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();c.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
 for(unsigned i=0;i<16384;++i){a.readback->words.push_back(i*4);c.readback->words.push_back(i*4);}
 using Pair=rsxgraphics::Session::VisualHistoryPair;
 // Fixed FNV32 collision, both ranges wholly inside the tables. Hash equality is not pair equality.
 const std::vector<Pair> p{{34136,39020,336}},q{{26564,27964,828}};
 assert(session.visual_history_key(a,c,p)==session.visual_history_key(a,c,q));
 auto original=session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,p);assert(original.texels==84);
 auto collision=session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,q);assert(collision.texels==207 && collision.regions!=original.regions);
 const auto hits=session.visual_history_structural_hits;
 a.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>(*a.readback);
 c.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>(*c.readback);
 auto reused=session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,p);
 assert((reused.regions==original.regions)==structural && session.visual_history_structural_hits==hits+unsigned(structural));
 assert(reused.texels==original.texels);
 // A new immutable word permutation with identical dimensions/pairs must not reuse stale coordinates.
 auto changed=std::make_shared<rsxgraphics::Surface::ReadbackPlan>(*a.readback);
 std::swap(changed->words[34136/4],changed->words[34136/4+1]);a.readback=std::move(changed);
 const auto misses=session.visual_history_plan_misses;
 auto mapped=session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,p);assert(session.visual_history_plan_misses==misses+1);
 auto source_at=[](const rsxgraphics::Session::VisualHistoryUse& use,unsigned x,unsigned y){
  // Regions are in visual units (scale 2): native texel (x,y) is the visual block at (2x,2y); report its native source texel.
  x*=2;y*=2;
  for(const auto& r:*use.regions)if(r.dstSubresource.baseArrayLayer==0 && x>=unsigned(r.dstOffset.x) && y>=unsigned(r.dstOffset.y) &&
     x+1<unsigned(r.dstOffset.x)+r.extent.width && y+1<unsigned(r.dstOffset.y)+r.extent.height)
   return unsigned(r.srcOffset.y+y-r.dstOffset.y)/2*128+unsigned(r.srcOffset.x+x-r.dstOffset.x)/2;
  throw std::runtime_error("missing requested history texel");};
 assert(source_at(original,(39020/4)%128,(39020/4)/128)==34136/4);
 assert(source_at(mapped,(39020/4)%128,(39020/4)/128)==34136/4+1);
 // Cold row coalescing must be independent of physical-range traversal order.
 a.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>(*c.readback);
 for(unsigned order=0;order<4;++order){
  std::vector<Pair> rows;
  for(unsigned i=0;i<128;++i){const unsigned y=order==1?(i%64)*2+i/64:order==2?127-i:i;
   rows.push_back({(order==3?127-y:y)*512,y*512,512});}
  auto layout=session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,rows);
  assert(layout.complete && layout.texels==16384 && layout.regions);
  assert(layout.regions->size()==(order==3?128u:1u));
  for(unsigned y=0;y<128;++y)for(unsigned x=0;x<128;++x)
   assert(source_at(layout,x,y)==(order==3?127-y:y)*128+x);
 }
 for(unsigned i=0;i<70;++i){session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,{{i*4,i*4,4}});assert(session.visual_history_plans.size()<=64);}
 // Large tables force the byte bound before the entry bound; old returned regions stay usable after eviction.
 a.width=c.width=a.height=c.height=1024;a.target.pitch=c.target.pitch=4096;
 a.image=vk.graphics_image(*session.batch,VK_FORMAT_R8G8B8A8_UNORM,1024,1024,1);c.image=a.image;
 a.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();c.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
 for(unsigned i=0;i<1024*1024;++i){a.readback->words.push_back(i*4);c.readback->words.push_back(i*4);}
 for(unsigned i=0;i<10;++i){session.visual_history_plan(a,c,VK_FORMAT_R8G8B8A8_UNORM,{{i*4,i*4,4}});
  assert(session.visual_history_plan_bytes<=64u*1024*1024 && session.visual_history_plans.size()<=64);}
 assert((!structural || session.visual_history_plans.size()<10) && original.texels==84 && original.regions && !original.regions->empty());
 vk.graphics_finish(*session.batch);unsetenv("ANYPS3_RENDER_SCALE");
 std::puts("PASS history exact pairs, recreated geometry, changed words, retained regions and bounded storage");
}
struct Layout {unsigned offset,pitch,width,height,samples;};
// Guest byte address of native texel (layer,row,col) of a 4-byte surface.
static unsigned texel_address(const Layout& l,unsigned layer,unsigned row,unsigned col){
 const unsigned nx=l.samples>1?2:1,ny=l.samples==4?2:1;
 return l.offset+(row*ny+layer/nx)*l.pitch+(col*nx+layer%nx)*4;
}
// Inverse: the texel of `a` that owns guest byte address `at`, if any.
static bool owner(const Layout& a,unsigned at,unsigned& layer,unsigned& row,unsigned& col){
 if(at<a.offset)return false;
 const unsigned nx=a.samples>1?2:1,ny=a.samples==4?2:1,rel=at-a.offset,yy=rel/a.pitch,xx=rel%a.pitch;
 if(xx%4 || yy>=a.height*ny || xx>=a.width*nx*4)return false;
 const unsigned x=xx/4;layer=(yy%ny)*nx+x%nx;row=yy/ny;col=x/nx;return true;
}
static std::uint64_t path(const char* name){auto& p=rsxgraphics::fallback_stats().paths;auto it=p.find(name);return it==p.end()?0:it->second;}
// Visual pixel (layer P, row Y, column X) of a surface rendered at twice the extent lies at
// flattened visual grid column X*nx+P%nx, row Y*ny+P/nx; native grid texel = grid/2.
static void visual_cell(const Layout& l,unsigned P,unsigned Y,unsigned X,unsigned& layer,unsigned& row,unsigned& col,unsigned& rx,unsigned& ry){
 const unsigned nx=l.samples>1?2:1,ny=l.samples==4?2:1,G=X*nx+P%nx,R=Y*ny+P/nx,g=G/2,r=R/2;
 rx=G%2;ry=R%2;layer=(r%ny)*nx+g%nx;row=r/ny;col=g/nx;
}
static unsigned visual_index(const Layout& l,unsigned layer,unsigned row,unsigned col,unsigned rx,unsigned ry){
 const unsigned nx=l.samples>1?2:1,ny=l.samples==4?2:1,G=(col*nx+layer%nx)*2+rx,R=(row*ny+layer/nx)*2+ry;
 return ((((R%ny)*nx+G%nx)*2*l.height+R/ny)*2*l.width+G/nx);
}
static void history(const char* name,Layout a,Layout c,const char* expected_path,bool roundtrip,bool mapped=true){
 setenv("ANYPS3_RENDER_SCALE","2",1);setenv("ANYPS3_RSX_FALLBACK_STATS","1",1);
 PpuState s;setup_draw(s,8,8,64,false);auto& gpu=s.rsx;
 gpu.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.renderer;
 rsxgraphics::Session session(vk,s);session.observe();gpu.graphics_session=&session;
 auto ta=rsxdraw::bind_target(s,0xfeed0000,a.offset,a.pitch,a.width*(a.samples>1?2:1),a.height*(a.samples==4?2:1));
 auto tc=rsxdraw::bind_target(s,0xfeed0000,c.offset,c.pitch,c.width*(c.samples>1?2:1),c.height*(c.samples==4?2:1));
 auto& src=session.bind(ta,a.width,a.height,a.samples,8,false);auto source=src.image;assert(source->visual);
 const unsigned aw=a.width,ah=a.height,cw=c.width,ch=c.height;
 std::vector<std::uint8_t> native(aw*ah*a.samples*4),visual(aw*ah*a.samples*16);
 for(unsigned p=0;p<a.samples;++p)for(unsigned y=0;y<ah;++y)for(unsigned x=0;x<aw;++x){auto* t=native.data()+((p*ah+y)*aw+x)*4;t[0]=x;t[1]=y;t[2]=p;t[3]=255;}
 // Every subpixel differs from its native cell (alpha 254) and its neighbours.
 for(unsigned p=0;p<a.samples;++p)for(unsigned y=0;y<2*ah;++y)for(unsigned x=0;x<2*aw;++x){auto* t=visual.data()+((p*2*ah+y)*2*aw+x)*4;t[0]=100+x;t[1]=100+y;t[2]=10+p;t[3]=254;}
 vk.graphics_transfer(*session.batch,source,VK_IMAGE_ASPECT_COLOR_BIT,4,native.data());
 vk.graphics_transfer(*session.batch,source->visual,VK_IMAGE_ASPECT_COLOR_BIT,4,visual.data());session.dirty_surface(src);
 const auto copies=vk.graphics_visual_history_copies,syncs=vk.graphics_visual_native_copies,used=path(expected_path);
 auto bound=session.bind(tc,cw,ch,c.samples,8,false).image;
 assert(path(expected_path)==used+1 && vk.graphics_visual_history_copies==copies+(mapped?1:0));
 const bool complete=vk.graphics_visual_native_copies==syncs;
 if(roundtrip){ // c -> a -> c is a bijection: every subpixel must come back.
  session.bind(ta,a.width,a.height,a.samples,8,false);bound=session.bind(tc,cw,ch,c.samples,8,false).image;
  assert(vk.graphics_visual_history_copies==copies+3 && session.visual_history_plan_hits>=1);
 }
 const auto low=vk.graphics_transfer(*session.batch,bound,VK_IMAGE_ASPECT_COLOR_BIT,4);
 const auto high=vk.graphics_transfer(*session.batch,bound->visual,VK_IMAGE_ASPECT_COLOR_BIT,4);
 vk.graphics_finish(*session.batch);const auto* bytes=static_cast<const std::uint8_t*>(session.batch->map);
 unsigned covered=0;
 for(unsigned p=0;p<c.samples;++p)for(unsigned y=0;y<ch;++y)for(unsigned x=0;x<cw;++x){
  unsigned lp=0,ly=0,lx=0;const bool hit=owner(a,texel_address(c,p,y,x),lp,ly,lx);covered+=hit;
  const auto* n=bytes+low+((p*ch+y)*cw+x)*4;
  if(hit && std::memcmp(n,native.data()+((lp*ah+ly)*aw+lx)*4,4))throw std::runtime_error(std::string(name)+": native bytes differ");
 }
 for(unsigned p=0;p<c.samples;++p)for(unsigned y=0;y<2*ch;++y)for(unsigned x=0;x<2*cw;++x){
  unsigned layer=0,row=0,col=0,rx=0,ry=0,lp=0,ly=0,lx=0;visual_cell(c,p,y,x,layer,row,col,rx,ry);
  const bool hit=mapped && owner(a,texel_address(c,layer,row,col),lp,ly,lx);
  const auto* v=bytes+high+((p*2*ch+y)*2*cw+x)*4;
  // Mapped: the source visual pixel at the same flattened visual grid position. Unmapped: per-plane nearest native reconstruction.
  const auto* e=hit?visual.data()+visual_index(a,lp,ly,lx,rx,ry)*4:bytes+low+((p*ch+y/2)*cw+x/2)*4;
  if(std::memcmp(v,e,4)){std::fprintf(stderr,"%s plane=%u visual=%u,%u hit=%d actual=%u,%u,%u,%u expected=%u,%u,%u,%u\n",name,p,x,y,int(hit),v[0],v[1],v[2],v[3],e[0],e[1],e[2],e[3]);
   throw std::runtime_error(std::string(name)+": visual history mismatch");}
 }
 assert(covered>0 && complete==(mapped && covered==cw*ch*c.samples));
 session.flush(false);gpu.graphics_session=nullptr;unsetenv("ANYPS3_RENDER_SCALE");
 std::printf("PASS history %s covered=%u/%u complete=%d regions_copied=%llu\n",name,covered,cw*ch*c.samples,int(complete),(unsigned long long)(vk.graphics_visual_history_copies-copies));
}
// D24S8 sampled as ARGB8 (typeless depth) at scale 2: each converted visual
// texel decodes its own depth/stencil subpixel, not the nearest native texel.
// SPIR-V OpFDiv permits 2.5 ULP for this normal denominator (255).
// https://docs.vulkan.org/spec/latest/appendices/spirvenv.html#spirvenv-precision-operation
// Compare to double-precision byte/255, using the smaller adjacent float spacing.
// Native/visual byte-copy history oracles above remain bit-exact.
static bool normalized_byte(float actual,unsigned byte){
 const double exact=double(byte)/255.0;const float rounded=float(exact);
 const double ulp=std::min(double(std::nextafter(rounded,INFINITY))-rounded,
                           double(rounded)-std::nextafter(rounded,-INFINITY));
 return std::isfinite(actual) && std::abs(double(actual)-exact)<=2.5*ulp;
}
static void typeless_depth(unsigned samples){
 setenv("ANYPS3_RENDER_SCALE","2",1);
 PpuState s;setup_draw(s,8,8,8*samples*4,false);auto& r=s.rsx.registers;
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8*samples;++x)s.memory.store(0xc0500000+y*8*samples*4+x*4,4,0x01020300u+x+16*y);
 s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x500000,8*samples*4,8*samples,8);
 auto& depth=session.bind(target,8,8,samples,2,true);auto image=depth.image;assert(image->visual && image->visual->format==VK_FORMAT_D24_UNORM_S8_UINT);
 const unsigned vw=16,vh=16;std::vector<std::uint32_t> words(vw*vh*samples);std::vector<std::uint8_t> stencil(vw*vh*samples);
 for(unsigned p=0;p<samples;++p)for(unsigned y=0;y<vh;++y)for(unsigned x=0;x<vw;++x){const unsigned i=(p*vh+y)*vw+x;
  words[i]=0x400000u+(x<<12)+(y<<4)+p;stencil[i]=std::uint8_t(0x30+x*7+y*13+p*3);}
 vk.graphics_transfer(*session.batch,image->visual,VK_IMAGE_ASPECT_DEPTH_BIT,4,words.data());
 vk.graphics_transfer(*session.batch,image->visual,VK_IMAGE_ASPECT_STENCIL_BIT,1,stencil.data());session.dirty_surface(depth);
 r[0x1a00]=0x500000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=((8*samples)<<16)|8;r[0x1840]=8*samples*4;
 const auto aspects=vk.graphics_visual_aspect_copies;
 auto converted=session.texture_metadata(0).resident_image;assert(converted && converted->visual && vk.graphics_visual_aspect_copies==aspects+2);
 const auto offset=vk.graphics_transfer(*session.batch,converted->visual,VK_IMAGE_ASPECT_COLOR_BIT,16);vk.graphics_finish(*session.batch);
 const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+offset);
 const unsigned cw=16*samples;
 for(unsigned y=0;y<16;++y)for(unsigned x=0;x<cw;++x){
  // Visual grid column x of the level is visual pixel x/samples, plane x%samples (surface rendered at 16x16).
  const unsigned plane=x%samples,i=(plane*vh+y)*vw+x/samples;
  const std::uint32_t v=((words[i]&0xffffffu)<<8)|stencil[i];
  const unsigned expected[4]={(v>>16)&255,(v>>8)&255,v&255,v>>24};
  for(unsigned c=0;c<4;++c)if(!normalized_byte(pixels[(y*cw+x)*4+c],expected[c])){std::fprintf(stderr,"typeless samples=%u x=%u y=%u c=%u actual=%a expected=%a\n",samples,x,y,c,pixels[(y*cw+x)*4+c],double(expected[c])/255.0);throw std::runtime_error("typeless depth visual mismatch");}
 }
 session.flush(false);s.rsx.graphics_session=nullptr;unsetenv("ANYPS3_RENDER_SCALE");
 std::printf("PASS history typeless-depth samples=%u visual depth/stencil subpixels decoded\n",samples);
}
int main(){try{
 // Fallback statistics latch on first use; enable them before any session.
 setvbuf(stdout,nullptr,_IONBF,0);setenv("ANYPS3_RSX_FALLBACK_STATS","1",1);
 for(unsigned byte=0;byte<256;++byte){assert(normalized_byte(float(byte)/255.f,byte));
  assert(!normalized_byte(float((byte+1)%256)/255.f,byte));assert(!normalized_byte(NAN,byte));}
 geometry_cache();typeless_depth(1);typeless_depth(2);
 history("equal-footprint",{0x100000,64,16,4,1},{0x100000,32,8,8,1},"surface_equal_rebind_gpu",true);
 history("equal-footprint-msaa2",{0x100000,64,8,4,2},{0x100000,32,4,8,2},"surface_equal_rebind_gpu",true);
 history("merge-rows",{0x100000,32,8,8,1},{0x100080,32,8,8,1},"surface_alias_merge_gpu",false);
 history("merge-columns",{0x100000,64,8,8,1},{0x100010,64,8,8,1},"surface_alias_merge_gpu",false);
 history("merge-msaa4",{0x100000,64,8,4,4},{0x100040,64,8,4,4},"surface_alias_merge_gpu",false);
 // Odd guest-pixel shift: sample phases differ, rows wrap into the next row.
 history("merge-msaa2-odd",{0x100000,64,8,8,2},{0x100004,64,8,8,2},"surface_alias_merge_gpu",false);
 // One-sample owner under a two-sample surface (per-texel runs).
 // A sample-count change has no whole-pixel block form at the visual extent: native reconstruction.
 history("merge-samples-1to2",{0x100000,64,16,8,1},{0x100040,64,8,8,2},"surface_alias_merge_gpu",false,false);
 }catch(const std::exception& e){std::fprintf(stderr,"HISTORY TEST STOP %s\n",e.what());return 1;}}
'''


class VisualHistoryTests(unittest.TestCase):
    def test_alias_transitions_keep_subpixels(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0]+MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root/'test.cpp'), *LIBRARIES, '-o', str(root/'test')],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                       VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            for mode, compact in (('1','1'), ('0','1'), ('1','0'), ('0','0')):
                with self.subTest(geometry_cache=mode, compact_runs=compact):
                    env['ANYPS3_RSX_HISTORY_GEOMETRY_CACHE'] = mode
                    env['ANYPS3_RSX_HISTORY_COMPACT_RUNS'] = compact
                    result = subprocess.run([str(root/'test')], capture_output=True, text=True, env=env, timeout=300)
                    output = result.stdout+result.stderr
                    self.assertEqual(result.returncode, 0, output)
                    self.assertNotIn('Validation Error', output)
                    self.assertNotIn('Validation Warning', output)
                    self.assertEqual(result.stdout.count('PASS history'), 10, output)
                    print(result.stdout, flush=True)



if __name__ == '__main__':
    unittest.main()
