"""Real rasterization at distinct guest/display extents; independent pixel oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void scissor_oracle(){
 for(unsigned native:{7u,8u,13u})for(unsigned raster:{11u,16u,26u})
 for(unsigned begin=0;begin<=native+1;++begin)for(unsigned count=0;count<=native+1;++count){
  NativeVulkan::GraphicsState s{};s.width=s.height=native;s.raster_width=s.raster_height=raster;s.scissor={{int(begin),int(begin)},{count,count}};
  const auto r=NativeVulkan::graphics_raster_scissor(s);
  for(unsigned pixel=0;pixel<raster;++pixel){
   const double guest=(pixel+.5)*native/raster;
   const bool expected=guest>=begin && guest<double(begin)+count;
   const bool actual=pixel>=unsigned(r.offset.x) && pixel<unsigned(r.offset.x)+r.extent.width;
   assert(expected==actual);
  }
 }
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.raster_width=16;
 bool threw=false;try{(void)NativeVulkan::graphics_raster_scissor(s);}catch(const std::runtime_error&){threw=true;}assert(threw);
 s.raster_height=16;s.scissor.offset.x=-1;threw=false;
 try{(void)NativeVulkan::graphics_raster_scissor(s);}catch(const std::runtime_error&){threw=true;}assert(threw);
 std::puts("PASS independent fractional scissor pixel-center oracle and invalid extents");
}
static void raster_oracle(unsigned raster,unsigned samples,bool paired=false){
 if(paired)setenv("ANYPS3_RENDER_SCALE","2",1);
 NativeVulkan vk;auto b=vk.graphics_batch(4*1024*1024);
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.samples=samples;s.colors=1;
 s.scale_visual=paired;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scissor={{1,1},{6,6}};s.blend[0].colorWriteMask=15;
 const std::array<std::array<float,2>,4> positions{{{.25f,.25f},{.75f,.75f},{.25f,.75f},{.75f,.25f}}};
 const std::string fs=R"GLSL(#version 450
 layout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane;layout(offset=56) vec2 raster_scale_;};
 layout(location=0) out vec4 c;
 void main(){c=vec4(gl_FragCoord.xy/raster_scale_/extent,0,1);}
 )GLSL";
 const auto vs=rsxgraphics::vertex_source();std::vector<unsigned> data(16+3*68);data[3]=16;
 const float points[3][2]={{0,0},{8,0},{0,8}};
 for(unsigned i=0;i<3;++i){const unsigned at=16+i*68;data[at]=rsxdraw::float_bits(points[i][0]);data[at+1]=rsxdraw::float_bits(points[i][1]);data[at+2]=rsxdraw::float_bits(.5f);data[at+3]=rsxdraw::float_bits(1.f);}
 auto native=vk.graphics_image(*b,s.color,8,8,samples),visual=paired?native->visual:vk.graphics_image(*b,s.color,raster,raster,samples);
 vk.graphics_clear_image(*b,native,{},0,0,VK_IMAGE_ASPECT_COLOR_BIT);vk.graphics_clear_image(*b,visual,{},0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{native};
 if(paired){
  const auto used=b->used;const auto queries=b->visibility_count;const auto draws=vk.graphics_draws;
  for(bool reuse:{false,true}){
   b->used=b->capacity-(reuse?512:1024);const auto before=b->used;bool refused=false;
   try{vk.graphics_draw(*b,s,images,{},vs,fs,data,3,positions,0,0,0,1,{},0,reuse?0:UINT64_MAX);}
   catch(const std::runtime_error& e){refused=std::string(e.what()).find("before recording")!=std::string::npos;}
   assert(refused && b->used==before && b->visibility_count==queries && vk.graphics_draws==draws);
  }
  b->used=used;
 }
 vk.graphics_draw(*b,s,images,{},vs,fs,data,3,positions);
 const auto before=vk.graphics_transfer(*b,native,VK_IMAGE_ASPECT_COLOR_BIT,4);
 if(!paired){s.raster_width=s.raster_height=raster;images[0]=visual;
 vk.graphics_draw(*b,s,images,{},vs,fs,data,3,positions);}
 else assert(b->visibility_count==samples && vk.graphics_visual_draws==1);
 const auto after=vk.graphics_transfer(*b,native,VK_IMAGE_ASPECT_COLOR_BIT,4),high=vk.graphics_transfer(*b,visual,VK_IMAGE_ASPECT_COLOR_BIT,4);
 vk.graphics_finish(*b);
 const auto* bytes=static_cast<const unsigned char*>(b->map);
 assert(std::memcmp(bytes+before,bytes+after,8*8*samples*4)==0);
 for(unsigned size:{8u,raster}){
  const auto at=size==8?before:high;const double ratio=double(size)/8;
  for(unsigned plane=0;plane<samples;++plane)for(unsigned y=0;y<size;++y)for(unsigned x=0;x<size;++x){
   const double gx=(x+.5)/ratio,gy=(y+.5)/ratio;
   const double edge=(x+positions[plane][0]+y+positions[plane][1])/ratio;
   if(std::abs(edge-8)<1e-6)continue; // Exclude an exact raster edge tie.
   const bool hit=gx>=1 && gx<7 && gy>=1 && gy<7 && edge<8;
   const auto* p=bytes+at+((plane*size+y)*size+x)*4;
   if(!hit){assert(p[0]==0 && p[1]==0 && p[2]==0 && p[3]==0);continue;}
   assert(std::abs(int(p[0])-int(std::lround(gx/8*255)))<=1);
   assert(std::abs(int(p[1])-int(std::lround(gy/8*255)))<=1);assert(p[2]==0 && p[3]==255);
  }
 }
 // Adjacent fragments inside one native pixel must contain distinct detail.
 if(raster==16){const auto* a=bytes+high+(3*raster+2)*4;const auto* c=a+4;assert(a[3]==255 && c[3]==255 && a[0]!=c[0]);}
 std::printf("PASS raster=%u samples=%u native bytes, higher-resolution coverage/interpolation\n",raster,samples);
 if(paired)unsetenv("ANYPS3_RENDER_SCALE");
}
static void generated_oracle(unsigned raster,unsigned input){
 PpuState guest;setup_draw(guest,8,8,32,false);guest.rsx.registers[0x208]=(guest.rsx.registers[0x208]&~31u)|12u;guest.rsx.registers[0x1d60]=0x40;
 fpword(guest,0,(1u<<24)|(15u<<9)|(input<<13)|1u);fpword(guest,1,1u|(0xe4u<<9)|(7u<<18));fpword(guest,2,0);fpword(guest,3,0);
 NativeVulkan vk;rsxgraphics::Session session(vk,guest);auto& b=*session.batch;
 const rsxdraw::Reg reg{guest.rsx};auto fp=rsxdraw::load_fragment_program(guest,reg);
 std::array<rsxdraw::Texture,16> textures{};auto fs=rsxgraphics::fragment_source(fp,reg,1,textures);assert(fs);
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.raster_width=s.raster_height=raster;s.samples=4;s.colors=1;s.color=VK_FORMAT_R32G32B32A32_SFLOAT;s.scissor={{0,0},{8,8}};s.blend[0].colorWriteMask=15;
 auto color=vk.graphics_image(b,s.color,raster,raster,4);vk.graphics_clear_image(b,color,{},0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 std::vector<unsigned> data;session.serialize_draw_registers(data,8,8);const unsigned start=data.size();data[3]=start;data.resize(start+3*68);
 const float points[3][4]={{0,0,.1f,1.f},{8,0,.3f,.5f},{0,8,.6f,.25f}};
 const float colors[3][4]={{.1f,.2f,.3f,1.f},{.9f,.3f,.1f,1.f},{.2f,.8f,.4f,1.f}};
 for(unsigned i=0;i<3;++i)for(unsigned c=0;c<4;++c){data[start+i*68+c]=rsxdraw::float_bits(points[i][c]);data[start+i*68+8+c]=rsxdraw::float_bits(colors[i][c]);}
 session.refresh_vertex_constants();session.refresh_fragment_constants(fp);session.bind_draw_blocks(data,false);
 const std::array<std::array<float,2>,4> positions{{{.25f,.25f},{.75f,.25f},{.25f,.75f},{.75f,.75f}}};
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color};
 vk.graphics_draw(b,s,images,{},rsxgraphics::vertex_source(),*fs,data,3,positions,0,0,0,1,{},0,UINT64_MAX,{}, {},fs,nullptr,0,true);
 const auto at=vk.graphics_transfer(b,color,VK_IMAGE_ASPECT_COLOR_BIT,16);vk.graphics_finish(b);
 const auto* pixels=reinterpret_cast<const float*>(static_cast<const char*>(b.map)+at);const double ratio=double(raster)/8;
 for(unsigned plane=0;plane<4;++plane)for(unsigned y=1;y<raster/3;++y)for(unsigned x=1;x<raster/3;++x){
  const double gx=(x+.5)/ratio,gy=(y+.5)/ratio,lambda[3]={1-(gx+gy)/8,gx/8,gy/8};
  double iw=0,z=0;for(unsigned i=0;i<3;++i){iw+=lambda[i]*points[i][3];z+=lambda[i]*points[i][2];}
  for(unsigned c=0;c<4;++c){double expected=0;
   if(input==0)expected=c==0?gx:c==1?gy:c==2?z:iw;
   else{for(unsigned i=0;i<3;++i)expected+=lambda[i]*points[i][3]*colors[i][c];expected/=iw;}
   const auto actual=pixels[((plane*raster+y)*raster+x)*4+c];
   if(std::abs(actual-expected)>0.00002)throw std::runtime_error("generated interpolation mismatch input="+std::to_string(input)+" actual="+std::to_string(actual)+" expected="+std::to_string(expected));
  }
 }
 std::printf("PASS generated raster=%u input=%u layered=%u multiview=%u perspective/depth/position\n",raster,input,unsigned(vk.graphics_layered_output),unsigned(vk.graphics_multiview));
}
static void texture_oracle(bool rectangle){
 PpuState guest;setup_draw(guest,8,8,32,false);texture_fp(guest);guest.rsx.registers[0x1d60]=0x40;
 NativeVulkan vk;rsxgraphics::Session session(vk,guest);auto& b=*session.batch;
 NativeVulkan::GraphicsState s{};s.width=s.height=8;s.raster_width=s.raster_height=16;s.colors=1;s.scissor={{0,0},{8,8}};s.blend[0].colorWriteMask=15;
 auto source=vk.graphics_image(b,s.color,16,16,1),destination=vk.graphics_image(b,s.color,16,16,1);
 vk.graphics_clear_image(b,source,{},0,0,VK_IMAGE_ASPECT_COLOR_BIT);vk.graphics_clear_image(b,destination,{},0,0,VK_IMAGE_ASPECT_COLOR_BIT);
 std::vector<unsigned> data;session.serialize_draw_registers(data,8,8);const unsigned start=data.size();data[3]=start;data.resize(start+3*68);
 const float points[3][2]={{0,0},{16,0},{0,16}};
 for(unsigned i=0;i<3;++i){const auto at=start+i*68;data[at]=rsxdraw::float_bits(points[i][0]);data[at+1]=rsxdraw::float_bits(points[i][1]);data[at+3]=rsxdraw::float_bits(1.f);
  data[at+32]=rsxdraw::float_bits(points[i][0]/(rectangle?1.f:8.f));data[at+33]=rsxdraw::float_bits(points[i][1]/(rectangle?1.f:8.f));data[at+35]=rsxdraw::float_bits(1.f);}
 const std::array<std::array<float,2>,4> positions{{{.5f,.5f},{.5f,.5f},{.5f,.5f},{.5f,.5f}}};
 const auto vs=rsxgraphics::vertex_source();const std::string producer="#version 450\nlayout(location=0) out vec4 c;void main(){c=vec4(gl_FragCoord.xy/16.0,0,1);}";
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{source};vk.graphics_draw(b,s,images,{},vs,producer,data,3,positions);
 std::array<rsxdraw::Texture,16> textures{};auto& t=textures[0];t.valid=true;t.format=0x85;t.resident_image=source;t.unnormalized=rectangle;t.min=t.mag=1;t.wrap_s=t.wrap_t=3;t.levels.push_back({8,8,32});
 const unsigned descriptor=48,level=data.size();data.resize(level+4);data[descriptor]=level;data[descriptor+1]=1;data[descriptor+2]=rectangle;data[descriptor+3]=data[descriptor+4]=3;data[descriptor+5]=data[descriptor+6]=1;data[descriptor+27]=2;data[descriptor+30]=3;
 for(unsigned c=0;c<4;++c){data[descriptor+15+c]=c;data[descriptor+19+c]=2;}
 data[level+1]=data[level+2]=8;data[level+3]=1;
 const rsxdraw::Reg reg{guest.rsx};auto fp=rsxdraw::load_fragment_program(guest,reg);auto fs=rsxgraphics::fragment_source(fp,reg,1,textures);assert(fs);
 session.refresh_vertex_constants();session.refresh_fragment_constants(fp);session.bind_draw_blocks(data,false);
 images[0]=destination;std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,20> inputs{};inputs[0]=source;
 vk.graphics_draw(b,s,images,{},vs,*fs,data,3,positions,0,0,0,1,inputs);
 const auto from=vk.graphics_transfer(b,source,VK_IMAGE_ASPECT_COLOR_BIT,4),to=vk.graphics_transfer(b,destination,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(b);
 const auto* bytes=static_cast<const unsigned char*>(b.map);
 for(unsigned p=0;p<16*16*4;++p)if(std::abs(int(bytes[from+p])-int(bytes[to+p]))>1)throw std::runtime_error("scaled RTT lost producer detail rectangle="+std::to_string(rectangle)+" byte="+std::to_string(p));
 std::printf("PASS scaled RTT producer/consumer rectangle=%u subpixel detail preserved\n",unsigned(rectangle));
}
static void alias_detail_oracle(){
 setenv("ANYPS3_RENDER_SCALE","2",1);
 PpuState s;setup_draw(s,8,8,256,false);auto& gpu=s.rsx;auto& r=gpu.registers;
 auto& tile=gpu.tiles[0];tile.bound=true;tile.location=0;tile.offset=0x100000;tile.size=0x10000;tile.pitch=256;
 gpu.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.renderer;
 rsxgraphics::Session session(vk,s);session.observe();gpu.graphics_session=&session;
 auto wide=rsxdraw::bind_target(s,0xfeed0000,0x100000,256,16,8);
 auto narrow=rsxdraw::bind_target(s,0xfeed0000,0x100000,256,8,8);
 auto& src=session.bind(wide,8,8,2,8,false);auto original=src.image;
 std::vector<std::uint8_t> native(8*8*2*4),visual(16*16*2*4);
 for(unsigned plane=0;plane<2;++plane)for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){
  const unsigned at=((plane*8+y)*8+x)*4;native[at]=10+x;native[at+1]=20+y;native[at+2]=30+plane;native[at+3]=255;}
 for(unsigned plane=0;plane<2;++plane)for(unsigned y=0;y<16;++y)for(unsigned x=0;x<16;++x){
  const unsigned at=((plane*16+y)*16+x)*4;visual[at]=x;visual[at+1]=y;visual[at+2]=80+plane;visual[at+3]=255;}
 vk.graphics_transfer(*session.batch,original,VK_IMAGE_ASPECT_COLOR_BIT,4,native.data());
 vk.graphics_transfer(*session.batch,original->visual,VK_IMAGE_ASPECT_COLOR_BIT,4,visual.data());session.dirty_surface(src);
 // Actual Session texture conversion flattens the visual sample planes like the guest grid.
 r[0x1a00]=0x100000;r[0x1a04]=0x0001a529;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;
 r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=(16u<<16)|8;r[0x1840]=256;
 auto converted=session.texture_metadata(0).resident_image;assert(converted && converted->visual);
 const auto rtt=vk.graphics_transfer(*session.batch,converted->visual,VK_IMAGE_ASPECT_COLOR_BIT,16);
 // Retained right-hand remainder must survive 2->1->2 with all subpixels.
 auto small=session.bind(narrow,8,8,1,8,false).image;
 const auto reduced=vk.graphics_transfer(*session.batch,small->visual,VK_IMAGE_ASPECT_COLOR_BIT,4);
 auto restored=session.bind(wide,8,8,2,8,false).image;
 const auto low=vk.graphics_transfer(*session.batch,restored,VK_IMAGE_ASPECT_COLOR_BIT,4);
 const auto high=vk.graphics_transfer(*session.batch,restored->visual,VK_IMAGE_ASPECT_COLOR_BIT,4);
 vk.graphics_finish(*session.batch);const auto* bytes=static_cast<const std::uint8_t*>(session.batch->map);
 assert(std::memcmp(bytes+low,native.data(),native.size())==0);
 for(unsigned i=0;i<visual.size();++i)if(bytes[high+i]!=visual[i]){
  std::fprintf(stderr,"roundtrip byte=%u actual=%u expected=%u copies=%llu draws=%llu\n",i,unsigned(bytes[high+i]),unsigned(visual[i]),(unsigned long long)vk.graphics_visual_native_copies,(unsigned long long)vk.graphics_visual_draws);
  for(const auto& path:rsxgraphics::fallback_stats().paths)std::fprintf(stderr,"path %s=%llu\n",path.first.c_str(),(unsigned long long)path.second);
  throw std::runtime_error("visual sample-grid roundtrip mismatch");}
 for(unsigned y=0;y<16;++y)for(unsigned x=0;x<32;++x){
  // Visual grid column x of a 2-sample surface rendered at 16 px: pixel x/2, sample x%2.
  const unsigned plane=x%2,source_x=x/2;
  const auto* expected=visual.data()+((plane*16+y)*16+source_x)*4;
  const auto* actual=reinterpret_cast<const float*>(bytes+rtt)+(y*32+x)*4;
  for(unsigned c=0;c<4;++c)assert(std::fabs(actual[c]-expected[c]/255.f)<1.e-6f);
  if(x<16)assert(std::memcmp(bytes+reduced+(y*16+x)*4,expected,4)==0);
 }
 session.flush(false);gpu.graphics_session=nullptr;unsetenv("ANYPS3_RENDER_SCALE");
 std::puts("PASS scaled typed RTT and sample-grid roundtrip retain every subpixel and native byte");
}
// Converted texture of one surface; at scale 2 the visual companion carries `fine` (2x extent),
// the native image `coarse`. Returns the converted visual (scale 2) or native (scale 1) texels.
static std::vector<float> convert_surface(unsigned scale,bool depth,unsigned size,const std::vector<std::uint32_t>& coarse,const std::vector<std::uint32_t>& fine){
 setenv("ANYPS3_RENDER_SCALE",scale==2?"2":"1",1);
 PpuState s;setup_draw(s,8,8,256,false);auto& gpu=s.rsx;auto& r=gpu.registers;
 gpu.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.renderer;
 rsxgraphics::Session session(vk,s);session.observe();gpu.graphics_session=&session;
 const unsigned bpp=depth?2:1;
 auto target=rsxdraw::bind_target(s,0xfeed0000,0x100000,size*bpp,size,size,bpp,depth?size:0,depth?size:0);
 auto& surface=session.bind(target,size,size,1,depth?1:9,depth);auto image=surface.image;
 auto put=[&](const std::shared_ptr<NativeVulkan::GraphicsImage>& to,const std::vector<std::uint32_t>& v){
  if(depth){std::vector<std::uint16_t> h(v.begin(),v.end());vk.graphics_transfer(*session.batch,to,VK_IMAGE_ASPECT_DEPTH_BIT,2,h.data());}
  else vk.graphics_transfer(*session.batch,to,VK_IMAGE_ASPECT_COLOR_BIT,4,v.data());};
 put(image,coarse);if(scale==2){assert(image->visual);put(image->visual,fine);}
 session.dirty_surface(surface);
 // Unit 0: swizzled DEPTH16 (0x92) or linear B8 (0x81|LN), 2D, one level.
 r[0x1a00]=0x100000;r[0x1a04]=depth?0x00019229u:0x0001a129u;r[0x1a08]=0x00030303;r[0x1a0c]=0x80000000;r[0x1a10]=0xaae4;r[0x1a14]=0x01010000;r[0x1a18]=(size<<16)|size;r[0x1840]=size*bpp;
 auto converted=session.texture_metadata(0).resident_image;assert(converted);
 const auto result=scale==2?converted->visual:converted;assert(result && result->width==size*scale);
 const auto at=vk.graphics_transfer(*session.batch,result,VK_IMAGE_ASPECT_COLOR_BIT,16);vk.graphics_finish(*session.batch);
 const auto* f=reinterpret_cast<const float*>(static_cast<const char*>(session.batch->map)+at);
 std::vector<float> out(f,f+std::size_t(result->width)*result->height*4);
 session.flush(false);gpu.graphics_session=nullptr;unsetenv("ANYPS3_RENDER_SCALE");
 return out;
}
// Swizzled shadow-map style depth (same Morton extent, origin 0) and packed B8 targets:
// the visual conversion of an 8x8 surface whose visual companion holds 16x16 values must equal
// the native (scale 1) conversion of a 16x16 surface holding those values.
static void typed_visual_oracle(){
 for(bool depth:{true,false}){
  std::vector<std::uint32_t> fine(256),coarse(64);
  for(unsigned i=0;i<256;++i)fine[i]=depth?(0x1000u+i*0xa7u)&0xffffu:(i*37u+11u)&255u;
  for(unsigned i=0;i<64;++i)coarse[i]=depth?0x0100u+i:200u-i;
  const auto actual=convert_surface(2,depth,8,coarse,fine),expected=convert_surface(1,depth,16,fine,fine);
  assert(actual.size()==expected.size());
  for(std::size_t i=0;i<actual.size();++i)if(actual[i]!=expected[i]){
   std::fprintf(stderr,"typed visual depth=%d texel=%zu actual=%a expected=%a\n",int(depth),i/4,actual[i],expected[i]);throw std::runtime_error("typed visual conversion lost subpixels");}
  std::printf("PASS typed visual RTT %s keeps every visual subpixel\n",depth?"swizzled DEPTH16":"packed B8");
 }
}
static void dual_oracle(){
 setenv("ANYPS3_RSX_FALLBACK_STATS","1",1);alias_detail_oracle();typed_visual_oracle();
 for(unsigned samples:{1u,2u,4u})raster_oracle(16,samples,true);
 for(unsigned mode:{0u,1u,8u,14u,17u,22u,24u}){
  PpuState native,scaled;setup_draw(native,32,16,128,false);setup_draw(scaled,32,16,128,false);customize(native,mode);customize(scaled,mode);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RENDER_SCALE","1",1);rsx_execute_draw(native);
  setenv("ANYPS3_RENDER_SCALE","2",1);rsx_execute_draw(scaled);
  // A native read forces the real surface flush. Compare full guest backing,
  // including depth/stencil and row padding, against a scale-one execution.
  (void)native.memory.load(0xc0100000,4);(void)scaled.memory.load(0xc0100000,4);
  assert(*native.memory.regions[1].bytes==*scaled.memory.regions[1].bytes);
  assert(native.rsx.draw.pixels==scaled.rsx.draw.pixels);
  assert(scaled.rsx.renderer->graphics_visual_draws>0);
  std::printf("PASS dual native bytes and query count mode=%u visual_draws=%llu\n",mode,(unsigned long long)scaled.rsx.renderer->graphics_visual_draws);
 }
 unsetenv("ANYPS3_RENDER_SCALE");
}
int main(int argc,char**){try{if(argc>3){dual_oracle();}else if(argc>2){texture_oracle(false);texture_oracle(true);}else if(argc>1){for(unsigned size:{12u,16u})for(unsigned input:{0u,1u})generated_oracle(size,input);}
 else{scissor_oracle();for(unsigned size:{12u,16u})for(unsigned samples:{1u,2u,4u})raster_oracle(size,samples);}}
 catch(const std::exception& e){std::fprintf(stderr,"SCALE TEST STOP %s\n",e.what());return 1;}}
'''

class RenderScaleTests(unittest.TestCase):
 def test_physical_raster_and_native_coordinates(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   (root/'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0]+MAIN)
   build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
   self.assertEqual(build.returncode,0,build.stderr)
   env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
   env.pop('ANYPS3_AUDIO_AUDIBLE',None)
   if not env.get('ANYPS3_REQUIRE_NVIDIA') and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
    env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
   result=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=180)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertNotIn('Validation Error',result.stdout+result.stderr)
   self.assertNotIn('Validation Warning',result.stdout+result.stderr)
   self.assertEqual(result.stdout.count('PASS raster='),6,result.stdout)
   print(result.stdout,flush=True)

   for layered,multiview in ((0,0),(1,0),(0,1)):
    env.update(ANYPS3_RSX_LAYERED=str(layered),ANYPS3_RSX_MULTIVIEW=str(multiview))
    result=subprocess.run([str(root/'test'),'generated'],capture_output=True,text=True,env=env,timeout=180)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    self.assertNotIn('Validation Error',result.stdout+result.stderr)
    self.assertNotIn('Validation Warning',result.stdout+result.stderr)
    self.assertEqual(result.stdout.count('PASS generated'),4,result.stdout)
    print(result.stdout,flush=True)

   env.update(ANYPS3_RSX_LAYERED='0',ANYPS3_RSX_MULTIVIEW='0')
   result=subprocess.run([str(root/'test'),'rtt','detail'],capture_output=True,text=True,env=env,timeout=180)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertNotIn('Validation Error',result.stdout+result.stderr)
   self.assertNotIn('Validation Warning',result.stdout+result.stderr)
   self.assertEqual(result.stdout.count('PASS scaled RTT'),2,result.stdout)
   print(result.stdout,flush=True)

   result=subprocess.run([str(root/'test'),'dual','native','bytes'],capture_output=True,text=True,env=env,timeout=180)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertNotIn('Validation Error',result.stdout+result.stderr)
   self.assertNotIn('Validation Warning',result.stdout+result.stderr)
   self.assertEqual(result.stdout.count('PASS dual'),7,result.stdout)
   self.assertEqual(result.stdout.count('PASS typed visual RTT'),2,result.stdout)
   print(result.stdout,flush=True)
