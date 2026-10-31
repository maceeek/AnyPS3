"""Independent GPU RTT byte, sample-grid, alias and allocator-edge oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void rtt_case(unsigned aa,bool tiled,bool alias,bool split,unsigned source_format,unsigned texture_format,bool srgb=false,bool swizzled=false,bool subrect=false){
 PpuState s;setup_draw(s,8,8,256,false);auto& r=s.rsx.registers;
 const unsigned nx=aa?2:1,ny=aa>=4?2:1,bpp=source_format==11?8:4;
 r[0x208]=(r[0x208]&~31u)|source_format|(aa<<12);r[0x20c]=r[0x22c]=256;
 if(srgb)r[0x1fec]=1;
 if(swizzled)r[0x208]=(3u<<24)|(3u<<16)|0x240|source_format;
 if(tiled){auto& tile=s.rsx.tiles[0];tile.bound=true;tile.location=0;tile.offset=0x80000;tile.size=0x10000;tile.pitch=256;r[0x210]=0x81000;}
 if(split){auto old=s.memory.regions[1];const unsigned cut=0x100040;auto a=std::make_shared<GuestBacking>(old.bytes->begin(),old.bytes->begin()+cut),b=std::make_shared<GuestBacking>(old.bytes->begin()+cut,old.bytes->end());s.memory.regions[1]={old.base,6,a};s.memory.regions.push_back({old.base+cut,6,b});}
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 rsx_execute_draw(s);const auto decodes=rsxdraw::host_stats().cpu_texel,submits=session.batch->submissions,readbacks=session.batch->download_bytes;
 // A physically aliased IO texture must identify the same resident bytes.
 const unsigned address=r[0x210]+(subrect?2*256+2*bpp:0);r[0x1a00]=address;
 if(alias){s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});s.rsx.io[1]=0x81100000;r[0x1a04]=0x0001a52a;}
 else r[0x1a04]=0x0001a529;
 r[0x1a04]=(r[0x1a04]&~0xff00u)|((texture_format|0x20u)<<8);
 r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;
 const unsigned tex_bpp=texture_format==0x9a?8:4,tw=subrect?4:8*nx*bpp/tex_bpp,th=subrect?3:8*ny;
 r[0x1a18]=(tw<<16)|th;r[0x1840]=256;
 if(swizzled){r[0x1a04]&=~0x2000u;r[0x1840]=0;}
 auto t=session.texture_metadata(0);assert(t.resident_image);
 assert(session.batch->submissions==submits && session.batch->download_bytes<=readbacks+4 && rsxdraw::host_stats().cpu_texel==decodes);
 const auto image=t.resident_image;assert(session.texture_metadata(0).resident_image==image);
 const auto data=s.rsx.renderer->graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,rsxgraphics::image_bpp(image->format));s.rsx.renderer->graphics_finish(*session.batch);
 const auto* p=static_cast<const std::uint8_t*>(session.batch->map)+data;
 if(image->format==VK_FORMAT_R32G32B32A32_SFLOAT){const auto* color=reinterpret_cast<const float*>(p);
  if(source_format==11 && texture_format==0x85){assert(color[0]==0.f && color[1]==56/255.f && color[2]==0.f && color[3]==60/255.f);}
  else if(srgb){assert(color[0]==1 && std::fabs(color[1]-188/255.f)<.004f && std::fabs(color[2]-137/255.f)<.004f && std::fabs(color[3]-.5f)<.003f);}
  else{assert(std::fabs(color[0]-1.f)<.001f && std::fabs(color[1]-.5f)<.003f && std::fabs(color[2]-.25f)<.003f && std::fabs(color[3]-.5f)<.003f);}
 }else assert(p[0]==255 && near_byte(p[1],128) && near_byte(p[2],64) && near_byte(p[3],128));
 session.flush(false);s.rsx.graphics_session=nullptr;
 std::printf("PASS RTT aa=%u tile=%u alias=%u split=%u source=%u texture=%u no CPU decode/readback/submit\n",aa,tiled,alias,split,source_format,texture_format);
}
static void expanded_sample_masks(){
 for(unsigned aa:{3u,4u,5u}){
  PpuState s;setup_draw(s,32,16,256,false);auto& r=s.rsx.registers;r[0x208]|=aa<<12;r[0x20c]=r[0x22c]=256;
  const float positions[3][2]={{0,0},{31.5f,0},{0,15.75f}};
  for(unsigned i=0;i<3;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002000+i*16+c*4,4,rsxdraw::float_bits(positions[i][c]));
  s.rsx.draw.primitive=5;s.rsx.draw.ranges={{0,3}};s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
  rsx_execute_draw(s);const unsigned ny=aa==3?1:2,n=aa==3?2:4;
  r[0x1a00]=0x100000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=(64u<<16)|(16*ny);r[0x1840]=256;
  auto image=session.texture_metadata(0).resident_image;assert(image && image->format==VK_FORMAT_R32G32B32A32_SFLOAT);
  const auto data=s.rsx.renderer->graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,16);s.rsx.renderer->graphics_finish(*session.batch);
  const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+data);unsigned mask=0;
  for(unsigned sample=0;sample<n;++sample)if(pixels[((15*ny+sample/2)*64+sample%2)*4+3]>.25f)mask|=1u<<sample;
  assert(mask==(aa==3?2u:aa==4?3u:5u));session.flush(false);s.rsx.graphics_session=nullptr;
 }
 std::puts("PASS independent expanded 2x/4x sample-edge masks 2/3/5 (no averaging)");
}
static void depth_view(){for(unsigned df:{1u,2u}){
 PpuState s;setup_draw(s,8,8,32,false);auto& r=s.rsx.registers;r[0xa70]=r[0xa74]=1;r[0xa6c]=0x207;r[0x208]=(r[0x208]&~0xe0u)|(df<<5);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;rsx_execute_draw(s);
 r[0x1a00]=r[0x214];r[0x1a04]=0x00010029|((df==1?0xb2u:0xb0u)<<8);r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=0x80008;r[0x1840]=32;
 auto image=session.texture_metadata(0).resident_image;assert(image);
 const auto data=s.rsx.renderer->graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,16);s.rsx.renderer->graphics_finish(*session.batch);
 const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+data);for(unsigned c=0;c<4;++c)assert(std::fabs(pixels[c]-(df==1?0.250003814755474f:0.250000014901162f))<1.e-7f);
 session.flush(false);s.rsx.graphics_session=nullptr;}
 std::puts("PASS Z16/Z24 sampled views preserve normalized depth through GPU conversion");
}
static void tiled_feedback_snapshot(){
 PpuState s;setup_draw(s,8,8,256,false);auto& r=s.rsx.registers;auto& tile=s.rsx.tiles[0];tile.bound=true;tile.offset=0x80000;tile.size=0x10000;tile.pitch=256;r[0x210]=0x81000;
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002100+i*8+c*4,4,0);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;rsx_execute_draw(s);
 r[0x1a00]=0x81000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=0x80008;r[0x1840]=256;
 fpword(s,0,(23u<<24)|(15u<<9)|(4u<<13));fpword(s,1,1u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
 fpword(s,4,(2u<<24)|(15u<<9)|1u);fpword(s,5,(0xe4u<<9)|(7u<<18));fpword(s,6,2u|(0xe4u<<9));fpword(s,7,0);for(unsigned c=0;c<4;++c)fpword(s,8+c,0x3f000000);
 const auto submits=session.batch->submissions,cpu=rsxdraw::host_stats().cpu_texel;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.batch->submissions==submits && rsxdraw::host_stats().cpu_texel==cpu);
 session.flush();auto target=rsxdraw::bind_target(s,0xfeed0000,0x81000,256,8,8);for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(near_argb(rsxdraw::read_be(target.at(x,y),4),0x40804020));s.rsx.graphics_session=nullptr;
 std::puts("PASS active tiled RTT feedback uses a pre-draw GPU snapshot for both primitives");
}
static void depth_typeless_bits(){
 const unsigned raw[]={0x000001a5,0x7fffff5a,0x8000013c,0xfffffe7f};
 const std::array<float,4> expected[]={{{0,1/255.f,165/255.f,0}},{{1,1,90/255.f,127/255.f}},{{0,1/255.f,60/255.f,128/255.f}},{{1,254/255.f,127/255.f,1}}};
 for(unsigned samples:{1u,2u}){
  PpuState s;setup_draw(s,8,8,8*samples*4,false);auto& r=s.rsx.registers;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8*samples;++x)s.memory.store(0xc0500000+y*8*samples*4+x*4,4,raw[x%4]);
  s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
  auto target=rsxdraw::bind_target(s,0xfeed0000,0x500000,8*samples*4,8*samples,8);
  auto& depth=session.bind(target,8,8,samples,2,true);session.dirty_surface(depth);
  r[0x1a00]=0x500000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=((8*samples)<<16)|8;r[0x1840]=8*samples*4;
  const auto submits=session.batch->submissions,downloads=session.batch->download_bytes,texel=rsxdraw::host_stats().cpu_texel;
  auto image=session.texture_metadata(0).resident_image;assert(image && session.batch->submissions==submits && session.batch->download_bytes==downloads+4 && rsxdraw::host_stats().cpu_texel==texel);
  const auto offset=s.rsx.renderer->graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,16);s.rsx.renderer->graphics_finish(*session.batch);
  const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+offset);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8*samples;++x)for(unsigned c=0;c<4;++c)assert(pixels[(y*8*samples+x)*4+c]==expected[x%4][c]);
  session.flush(false);s.rsx.graphics_session=nullptr;
 }
 // Actual depth writes preserve stencil and expose the original packed bits.
 PpuState s;setup_draw(s,8,8,32,false);auto& r=s.rsx.registers;r[0xa70]=r[0xa74]=1;r[0xa6c]=0x207;
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)s.memory.store(0xc0500000+y*32+x*4,4,0xffffff5a);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;rsx_execute_draw(s);
 r[0x1a00]=0x500000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=0x80008;r[0x1840]=32;
 auto image=session.texture_metadata(0).resident_image;assert(image);const auto offset=s.rsx.renderer->graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,16);s.rsx.renderer->graphics_finish(*session.batch);
 const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+offset);assert(pixels[0]==0 && pixels[1]==0 && pixels[2]==90/255.f && pixels[3]==64/255.f);
 session.flush(false);s.rsx.graphics_session=nullptr;
 std::puts("PASS D24/S8 typeless ARGB8 preserves all 24 depth bits and stencil, 1x/2x samples, GPU writes and no host transfer");
}
static void float_typeless_bits(){
 const unsigned half_words[]={0x7c010001,0x80007e55};
 const unsigned float_words[]={0x7f800001,0x80000000,0x00000001,0x7fc12345};
 const std::array<float,4> half_expected[]={{{1/255.f,0,1/255.f,124/255.f}},{{0,126/255.f,85/255.f,128/255.f}}};
 const std::array<float,4> float_expected[]={{{128/255.f,0,1/255.f,127/255.f}},{{0,0,0,128/255.f}},{{0,0,1/255.f,0}},{{193/255.f,35/255.f,69/255.f,127/255.f}}};
 for(unsigned format:{11u,12u,13u}){
  const unsigned bytes=format==11?8:format==12?16:4,words=bytes/4;
  PpuState s;setup_draw(s,8,8,8*bytes,false);auto& r=s.rsx.registers;r[0x208]=(r[0x208]&~31u)|format;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)for(unsigned n=0;n<words;++n)s.memory.store(0xc0100000+y*8*bytes+x*bytes+n*4,4,format==11?half_words[n]:float_words[n]);
  s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
  auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,8*bytes,8,8,bytes);auto& surface=session.bind(target,8,8,1,format,false);session.dirty_surface(surface);
  r[0x1a00]=0x100000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=((8*words)<<16)|8;r[0x1840]=8*bytes;
  const auto submits=session.batch->submissions,texel=rsxdraw::host_stats().cpu_texel;auto image=session.texture_metadata(0).resident_image;assert(image && session.batch->submissions==submits && rsxdraw::host_stats().cpu_texel==texel);
  const auto at=s.rsx.renderer->graphics_transfer(*session.batch,image,VK_IMAGE_ASPECT_COLOR_BIT,16);s.rsx.renderer->graphics_finish(*session.batch);
  const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+at);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8*words;++x)for(unsigned c=0;c<4;++c)assert(pixels[(y*8*words+x)*4+c]==(format==11?half_expected[x%words][c]:float_expected[x%words][c]));
  session.flush(false);s.rsx.graphics_session=nullptr;
 }
 std::puts("PASS FP16/FP32 typeless views preserve signaling/quiet NaN payload, signed zero and smallest subnormal as bytes");
}
static void selective_unprotectable(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);
 // A separate tiny owner cannot be safely protected. Its committed bytes
 // must not cause the unrelated large backing to be downloaded or evicted.
 const auto original_surfaces=session.surfaces.size();auto owner=std::make_shared<GuestBacking>(256);s.memory.regions.push_back({0x81000000,6,owner});s.rsx.io[8]=0x81000000;
 auto t=rsxdraw::bind_target(s,0xfeed0001,0x800000,32,8,8);auto& small=session.bind(t,8,8,1,8,false);session.dirty_surface(small);
 s.rsx.graphics_session=nullptr;session.leave_fifo();
 assert(session.surfaces.size()==original_surfaces); // original color + depth survive
 assert(session.surfaces[0].dirty);assert(rsxgraphics::fallback_stats().flush_readbacks["unprotectable"]==1);
 // A retained raw read still observes the correct GPU result.
 const auto* raw=s.memory.regions[1].bytes->data()+0x100000;assert(raw[0]>=127 && raw[0]<=128 && raw[1]==255);
 session.guard.disarm();session.flush();std::puts("PASS allocator-edge flush only downloads the affected surface, retained read stays coherent");
}
static void owner_lookup_order(){
 // rtt_image's surface lookup (ANYPS3_RSX_RTT_PREFILTER) across surface creation and removal at the texture's bytes.
 PpuState s;setup_draw(s,8,8,256,false);auto& r=s.rsx.registers;r[0x20c]=r[0x22c]=256;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 auto& paths=rsxgraphics::fallback_stats().paths;
 auto sample=[&](unsigned address){r[0x1a00]=address;r[0x1a04]=(0x0001a529u&~0xff00u)|((0x85u|0x20u)<<8);r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;
  r[0x1a18]=(8u<<16)|8;r[0x1840]=256;const auto before=paths["rtt_no_owner"];auto t=session.texture_metadata(0);return std::make_pair(t.resident_image,paths["rtt_no_owner"]-before);};
 const unsigned color=r[0x210],away=0x300000;
 auto first=sample(color);assert(!first.first && first.second==1);           // no surface owns the color bytes yet
 rsx_execute_draw(s);                                                           // creates the color surface over them
 auto owned=sample(color);assert(owned.first && owned.second==0);
 auto far=sample(away);assert(!far.first && far.second==1);                    // bytes no surface covers
 session.clear_surfaces();
 auto gone=sample(color);assert(!gone.first && gone.second==1);                // the owner was removed
 const auto data=s.rsx.renderer->graphics_transfer(*session.batch,owned.first,VK_IMAGE_ASPECT_COLOR_BIT,rsxgraphics::image_bpp(owned.first->format));s.rsx.renderer->graphics_finish(*session.batch);
 const auto* p=static_cast<const std::uint8_t*>(session.batch->map)+data;assert(p[0]==255 && near_byte(p[1],128) && near_byte(p[2],64) && near_byte(p[3],128));
 session.flush(false);s.rsx.graphics_session=nullptr;
 std::puts("PASS RTT owner lookup follows surface creation and removal at the sampled bytes");
}
int main(){try{setenv("ANYPS3_RSX_HOST_PROFILE","1",1);setenv("ANYPS3_RSX_FALLBACK_STATS","1",1);
 rtt_case(0,true,false,false,8,0x85);rtt_case(3,false,false,false,8,0x85);rtt_case(4,true,false,false,8,0x85);rtt_case(5,false,false,false,11,0x9a);rtt_case(0,false,true,false,8,0x85);rtt_case(0,false,false,true,8,0x85);rtt_case(0,false,false,false,11,0x85);rtt_case(0,false,false,false,8,0x85,true);rtt_case(0,false,false,false,8,0x85,false,true);rtt_case(0,false,false,false,11,0x85,false,true);rtt_case(0,true,false,false,8,0x85,false,false,true);expanded_sample_masks();depth_view();tiled_feedback_snapshot();depth_typeless_bits();float_typeless_bits();selective_unprotectable();owner_lookup_order();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+MAIN

class RenderTargetTextureTests(unittest.TestCase):
    def test_byte_layouts_and_selective_coherence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'test.cpp').write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=90)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stdout+run.stderr);self.assertNotIn('Validation Warning',run.stdout+run.stderr)
            self.assertEqual(run.stdout.count("PASS "),18)
