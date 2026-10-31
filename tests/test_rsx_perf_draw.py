"""RSX warm preparation: fixed BE, value/lifetime and dynamic-state oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void level_values(){
 rsxdraw::Texture a;a.levels.resize(1);a.levels[0].width=17;a.levels[0].height=23;a.levels.freeze();
 auto b=a;const auto& ca=a;const auto& cb=b;assert(&ca.levels[0]==&cb.levels[0]);
 b.levels[0].width=31;assert(ca.levels[0].width==17 && cb.levels[0].width==31 && cb.levels[0].height==23);
 auto c=a;c.levels.push_back({});c.levels.back().height=47;assert(ca.levels.size()==1 && c.levels.size()==2 && c.levels.back().height==47);
 auto d=a;for(auto& l:d.levels)l.height=53;assert(ca.levels[0].height==23 && static_cast<const rsxdraw::Texture&>(d).levels[0].height==53);
 auto& retained=a.levels[0];auto e=a;retained.width=67;assert(static_cast<const rsxdraw::Texture&>(e).levels[0].width==17);
 auto iterator=a.levels.begin();rsxdraw::Texture f;f=a;iterator->height=71;assert(static_cast<const rsxdraw::Texture&>(f).levels[0].height==23);
 std::puts("PASS shared texture levels preserve independent mutable values");
}
static void image_pins(){
 NativeVulkan::GraphicsImagePins pins;std::weak_ptr<NativeVulkan::GraphicsImage> weak;
 {auto image=std::make_shared<NativeVulkan::GraphicsImage>(nullptr,VK_FORMAT_R8G8B8A8_UNORM,4,4,1,false);image->memory_bytes=4096;weak=image;
 for(unsigned n=0;n<10000;++n){pins.push_back(image);}assert(pins.size()==1 && pins.bytes==4096);}
 assert(!weak.expired());
 for(unsigned n=0;n<600;++n){auto image=std::make_shared<NativeVulkan::GraphicsImage>(nullptr,VK_FORMAT_R8G8B8A8_UNORM,4,4,1,false);image->memory_bytes=256;pins.push_back(image);pins.push_back(image);}
 assert(pins.size()==601 && pins.bytes==157696);pins.clear();assert(weak.expired() && pins.empty() && pins.bytes==0);
 std::puts("PASS batch pin uniqueness, growth, exact bytes and retirement lifetime");
}
static void indexed_reads(){
 setenv("ANYPS3_RSX_BACKEND","cpu",1);
 PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;s.rsx.draw.indexed=true;R[0x181c]=0x3000;R[0x1820]=0x10;
 const unsigned initial[]={0,1,2,3};for(unsigned n=0;n<4;++n)s.memory.store(0xc0003000+2*n,2,initial[n]);
 auto run=[&]{s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);};
 run();assert(s.rsx.draw.scratch_indices==std::vector<unsigned>({0,1,2,3}));assert(s.memory.load(0xc0100000,4)==0x80ff8040);
 const auto capacity=s.rsx.draw.scratch_indices.capacity();
 // Current memory, not cached values, controls every following draw.
 s.memory.store(0xc0003000,2,3);run();assert(s.rsx.draw.scratch_indices[0]==3 && s.rsx.draw.scratch_indices.capacity()==capacity);
 // Both index widths decode guest BE, base index and restart literally.
 R[0x1820]=0;R[0x173c]=1;R[0x1dac]=1;R[0x1db0]=0x12345678;
 const unsigned words[]={0,2,0x12345678,1};for(unsigned n=0;n<4;++n)s.memory.store(0xc0003000+4*n,4,words[n]);
 run();assert(s.rsx.draw.scratch_indices==std::vector<unsigned>({1,3,2}));
 R[0x173c]=0;R[0x1dac]=0;
 for(unsigned n=0;n<4;++n){s.memory.store(0xc0003000+4*n,4,initial[n]);}
 // Split the input exactly inside a 32-bit index. Snapshot must gather BE
 // bytes, and remapping must be observed even after scratch reuse.
 auto source=s.memory.regions[1];const auto cut=0x3003u;
 auto a=std::make_shared<GuestBacking>(source.bytes->begin(),source.bytes->begin()+cut);
 auto b=std::make_shared<GuestBacking>(source.bytes->begin()+cut,source.bytes->end());
 s.memory.regions[1]={source.base,6,a};s.memory.regions.push_back({source.base+cut,6,b});
 run();assert(s.rsx.draw.scratch_indices==std::vector<unsigned>({0,1,2,3}));
 s.memory.regions.back().flags=2;bool failed=false;try{run();}catch(const std::runtime_error&){failed=true;}assert(failed);s.memory.regions.back().flags=6;
 R[0x173c]=UINT_MAX;failed=false;try{run();}catch(const std::runtime_error&){failed=true;}assert(failed);
 R[0x173c]=0;R[0x181c]=0x3000;s.rsx.draw.ranges={{0,0}};rsx_execute_draw(s);assert(s.rsx.draw.scratch_indices.empty());
 R[0x181c]=UINT_MAX;s.rsx.draw.ranges={{0,0}};failed=false;try{rsx_execute_draw(s);}catch(const std::runtime_error&){failed=true;}assert(failed);
 std::puts("PASS bulk indices: literal BE16/32, memory/remap/permission, split, restart, overflow and empty");
}
static void shader_reads(){
 PpuState s;setup_draw(s,8,8,32,false);const rsxdraw::Reg r{s.rsx};
 auto first=rsxdraw::load_fragment_program(s,r);assert(first.size()==1 && first[0].has_constant && first[0].constant[0]==1 && first[0].constant[1]==.5f);
 fpword(s,5,0x3e800000);auto second=rsxdraw::load_fragment_program(s,r);assert(second[0].constant[1]==.25f && first[0].constant[1]==.5f);
 auto old=s.memory.regions[1];const unsigned cut=0x1007;
 auto a=std::make_shared<GuestBacking>(old.bytes->begin(),old.bytes->begin()+cut),b=std::make_shared<GuestBacking>(old.bytes->begin()+cut,old.bytes->end());
 s.memory.regions[1]={old.base,6,a};s.memory.regions.push_back({old.base+cut,6,b});
 {GuestMemory::SplitReadScope split(s.memory);auto next=rsxdraw::load_fragment_program(s,r);assert(next[0].d==0x01001e01 && next[0].constant[1]==.25f);}
 std::puts("PASS original instruction words: literal halfword-swapped code/constants, memory write and split");
}
static void write_membership(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();
 auto t=session.texture_metadata(0);session.texture_image(t);assert(session.raw_misses==1);
 rsxgraphics::Surface surface{};surface.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
 const auto at=reinterpret_cast<std::uintptr_t>(t.plan->runs[0].data);surface.physical.push_back({at,at+4});
 session.dirty_surface(surface);assert(session.raw_invalidations==1 && session.raw_textures.empty());
 session.dirty_surface(surface);assert(session.raw_invalidations==1);
 // A new upload must invalidate the proof even with the identical mapping,
 // layout and source bytes. The second GPU write must erase it again.
 session.texture_image(t);session.dirty_surface(surface);assert(session.raw_misses==2 && session.raw_invalidations==2 && session.raw_textures.empty());
 // CPU alias writes still invalidate newly cached images independently.
 session.texture_image(t);s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});s.memory.store(0x81002000,4,0xffaabbcc);
 assert(session.raw_invalidations==3);session.texture_image(session.texture_metadata(0));session.dirty_surface(surface);assert(session.raw_invalidations==4);
 session.flush();assert(s.memory.load(0xc0002000,4)==0xffaabbcc);
 std::puts("PASS GPU write proof: repeated layout, new membership and CPU alias invalidation");
}
static void dynamic_state(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 auto run=[&]{s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);};
 run();session.batch->sync();const auto emits=session.batch->dynamic_emits,binds=session.batch->pipeline_binds;run();session.batch->sync();
 assert(session.batch->dynamic_emits==emits && session.batch->dynamic_reuses>=11 && session.batch->pipeline_binds==binds);
 const auto descriptors=session.batch->descriptor_misses,pipelines=s.rsx.renderer->compile_stats.graphics_pipelines;
 s.rsx.registers[0x310]=1;s.rsx.registers[0x314]=0x10001;s.rsx.registers[0x318]=0;s.rsx.registers[0x320]=0x80068006;run();session.batch->sync();
 assert(s.rsx.renderer->compile_stats.graphics_pipelines==pipelines+1 && session.batch->descriptor_misses==descriptors);s.rsx.registers[0x310]=0;
 session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040));
 // Command reset must re-emit state even when values/pipeline are identical.
 assert(!session.batch->dynamic_valid && session.batch->bound_graphics_pipeline==VK_NULL_HANDLE);run();session.batch->sync();assert(session.batch->dynamic_valid);
 session.flush();
 // Change a dynamic scissor and the shader constant independently. Only the
 // right half changes, with a fixed final guest pixel at each side.
 s.rsx.registers[0x8c0]=(4u<<16)|4u;fpword(s,4,0);fpword(s,5,0x3f800000);fpword(s,6,0);fpword(s,7,0x3f800000);
 run();session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040) && s.memory.load(0xc0100010,4)==0xff00ff00);
 s.rsx.graphics_session=nullptr;
 std::puts("PASS dynamic reuse, reset and changed scissor/constant fixed pixels");
}
// HIRES4-DRAW oracles (independent of the cached/inline implementations):
// register reads against a std::map model of presence, the per-draw register prefix against the documented reset
// defaults, triangle-list assembly against a hand-built list, and the per-draw blob against stale descriptor words.
static void register_reads(){
 PpuState s;auto& R=s.rsx.registers;std::map<std::uint32_t,std::uint32_t> model;std::uint32_t seed=12345;
 auto next=[&]{seed=seed*1103515245u+12345u;return seed>>8;};
 for(unsigned n=0;n<20000;++n){const std::uint32_t r=(next()%0x4000)*4,v=next();
  switch(next()%4){case 0:case 1:R.write(r,v);model[r]=v;break;case 2:R[r]=v;model[r]=v;break;case 3:R.erase(r);model.erase(r);break;}}
 const rsxdraw::Reg reg{s.rsx};
 for(std::uint32_t r=0;r<0x10000;r+=4){const auto it=model.find(r);const std::uint32_t expect=it==model.end()?0:it->second;
  assert(reg(r)==expect && reg.get(r,0xdeadbeef)==(it==model.end()?0xdeadbeefu:it->second));}
 // An erased register keeps its old slot value internally; it must read as absent. Unaligned/out-of-bank never present.
 R.write(0x1a00,77);R.erase(0x1a00);assert(reg(0x1a00)==0 && reg.get(0x1a00,5)==5);
 volatile std::uint32_t odd=0x1a02,far=0x10000;assert(reg(odd)==0 && reg.get(far,9)==9);
 std::puts("PASS register reads: presence, erase, unaligned and fallback against a map model");
}
static void register_prefix(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);
 auto& R=s.rsx.registers;std::uint32_t seed=777;auto next=[&]{seed=seed*1103515245u+12345u;return seed>>8;};
 // Reset values of registers the shaders read when absent (rsx_methods.cpp defaults used by the GPU path).
 const std::map<std::uint32_t,std::uint32_t> defaults{{0xa6c,0x207},{0x394,0},{0x398,0x3f800000},{0x368,0x1d01},{0x1834,0x901},{0x388,0x3f800000},{0x308,0x207},{0x324,0x01010101},{0x378,0x1503},{0x1d7c,0xffff0000}};
 for(unsigned round=0;round<40;++round){
  for(unsigned n=0;n<200;++n){const std::uint32_t r=(next()%0x800)*4,v=next();
   if(next()%5==0)R.erase(r);else if(next()%7==0)R[r]=v;else R.write(r,v);}
  if(round%3==0)for(const auto& d:defaults)R.erase(d.first);
  if(round%10==9){RsxRegisters copy=R;R=copy;if(round%20==19)R.exposed.fill(0);}   // new identity, sometimes nothing borrowed
  session.refresh_registers();
  for(std::uint32_t r=0;r<0x2000;r+=4){std::uint32_t expect=0;
   if(R.count(r))expect=std::as_const(R).at(r);else if(defaults.count(r))expect=defaults.at(r);
   if(session.register_prefix[16+r/4]!=expect){std::fprintf(stderr,"prefix %04x=%08x expected %08x\n",r,session.register_prefix[16+r/4],expect);assert(false);}}
 }
 // A borrowed block compares against its last snapshot; that snapshot must not survive a period of tracked (never borrowed)
 // rebuilds: borrowed 5 -> new identity, unborrowed, tracked 6 -> borrowed back to 5 must read 5.
 R[0x1d00]=5;session.refresh_registers();assert(session.register_prefix[16+0x1d00/4]==5);
 {RsxRegisters copy=R;R=copy;}R.exposed.fill(0);
 R.write(0x1d00,6);session.refresh_registers();assert(session.register_prefix[16+0x1d00/4]==6);
 R[0x1d00]=5;session.refresh_registers();assert(session.register_prefix[16+0x1d00/4]==5);
 std::puts("PASS register prefix: present values, absent zero and reset defaults after random writes/erases");
}
static void triangle_list(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 auto& R=s.rsx.registers;s.rsx.draw.indexed=true;R[0x181c]=0x3000;R[0x1820]=0x10;s.rsx.draw.primitive=5;
 const unsigned first[]={0,1,2,2,3,0,1};for(unsigned n=0;n<7;++n)s.memory.store(0xc0003000+2*n,2,first[n]);
 s.rsx.draw.ranges={{0,7}};rsx_execute_draw(s);   // 7 indices: two triangles, the trailing index is dropped
 const std::vector<std::array<unsigned,3>> two{{0,1,2},{2,3,0}};assert(s.rsx.draw.scratch_triangles==two);
 const unsigned second[]={3,2,1};for(unsigned n=0;n<3;++n)s.memory.store(0xc0003000+2*n,2,second[n]);
 s.rsx.draw.ranges={{0,3}};rsx_execute_draw(s);   // a shorter list replaces (never extends) the previous one
 const std::vector<std::array<unsigned,3>> one{{3,2,1}};assert(s.rsx.draw.scratch_triangles==one);
 s.rsx.draw.ranges={{0,2}};rsx_execute_draw(s);assert(s.rsx.draw.scratch_triangles.empty());
 session.flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS triangle list: n/3 complete triangles, replaced per draw");
}
static void blob_descriptors(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 auto run=[&]{s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);};
 run();const auto& data=session.draw_data;bool described=false;for(unsigned w=48;w<80;++w)described|=data[w]!=0;assert(described);
 // The constant MOV program samples nothing: every texture descriptor word of the next blob is zero.
 fpword(s,0,(1u<<24)|(15u<<9)|1u);fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
 run();for(unsigned w=48;w<48+16*32;++w)if(data[w]){std::fprintf(stderr,"stale blob word %u=%08x\n",w,data[w]);assert(false);}
 session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040));
 // Texture units of a previous draw never survive into the next (also after a draw that throws once units are bound):
 // TEX R1=unit1; TEX R0=unit0 (END) binds two units, the single-unit program after it must leave unit 1's words zero.
 auto& R=s.rsx.registers;for(unsigned r=0;r<0x20;r+=4)R[0x1a20+r]=R[0x1a00+r];R[0x1844]=R[0x1840];
 auto two_units=[&]{fpword(s,0,(23u<<24)|(1u<<17)|(4u<<13)|(15u<<9)|(1u<<1));fpword(s,1,1u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
  fpword(s,4,(23u<<24)|(4u<<13)|(15u<<9)|1u);fpword(s,5,1u|(0xe4u<<9)|(7u<<18));fpword(s,6,0);fpword(s,7,0);};
 for(unsigned pass=0;pass<2;++pass){
  two_units();
  if(pass==0){run();bool unit1=false;for(unsigned w=80;w<112;++w)unit1|=data[w]!=0;assert(unit1);}
  else{R[0xb40]=0x100;bool failed=false;try{run();}catch(const std::runtime_error&){failed=true;}assert(failed);R[0xb40]=0;}   // texcoord control stop after binding
  texture_fp(s);run();for(unsigned w=80;w<112;++w)if(data[w]){std::fprintf(stderr,"pass %u stale unit-1 word %u=%08x\n",pass,w,data[w]);assert(false);}
 }
 session.flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS draw blob: descriptors and texture units of a previous draw never leak into the next");
}
static void pipeline_lookup(){
 for(const char* limit:{"1024","1"}){
  setenv("ANYPS3_RSX_PIPELINE_LIMIT",limit,1);const bool single=!std::strcmp(limit,"1");
  PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
  setenv("ANYPS3_RSX_BACKEND","gpu",1);auto& R=s.rsx.registers;auto& vk=*s.rsx.renderer;
  // Alternate no blend / blend ZERO,ZERO (result 0): two fixed states, one shader pair.
  auto run=[&](bool zero){R[0x310]=zero;R[0x314]=0;R[0x318]=0;R[0x320]=0x80068006;s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();return s.memory.load(0xc0100000,4);};
  const auto before=vk.compile_stats.graphics_pipelines;
  for(unsigned n=0;n<6;++n){const bool zero=n&1;const auto pixel=run(zero);
   if(zero?pixel!=0:!near_argb(pixel,0x80ff8040)){std::fprintf(stderr,"draw %u pixel %08x\n",n,unsigned(pixel));assert(false);}
   // Oracle: a 1024-entry cache compiles each state once; a one-entry cache evicts the other state at every switch.
   const std::uint64_t created=vk.compile_stats.graphics_pipelines-before,expect=single?n+1:std::min(n+1,2u);
   if(created!=expect){std::fprintf(stderr,"limit %s draw %u: %llu pipelines, expected %llu\n",limit,n,(unsigned long long)created,(unsigned long long)expect);assert(false);}}
  s.rsx.graphics_session=nullptr;
 }
 unsetenv("ANYPS3_RSX_PIPELINE_LIMIT");
 std::puts("PASS pipeline lookup: one pipeline per state, eviction at every switch with a one-entry cache");
}
static void sampling_words(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);auto& R=s.rsx.registers;const auto& data=session.draw_data;
 auto run=[&]{s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);};
 // Unit 0 descriptor (blob word 48+): +3/+4/+30 wrap s/t/r = TEXTURE_ADDRESS nibbles 0/8/16, +5/+6 min/mag = FILTER bits 16-18/24-26.
 // The layout (offset/format/rect/control3) never changes here: the same metadata entry serves every draw.
 const unsigned cases[][2]={{0x00030303,0x02020000},{0x00010101,0x02020000},{0x00010101,0x01010000},{0x00020301,0x02010000},{0x00030303,0x02020000}};
 for(const auto& c:cases){R[0x1a08]=c[0];R[0x1a14]=c[1];run();
  const unsigned wrap_s=c[0]&15,wrap_t=(c[0]>>8)&15,wrap_r=(c[0]>>16)&15,min=(c[1]>>16)&7,mag=(c[1]>>24)&7;
  if(data[48+3]!=wrap_s || data[48+4]!=wrap_t || data[48+30]!=wrap_r || data[48+5]!=min || data[48+6]!=mag){
   std::fprintf(stderr,"sampling %08x/%08x: wrap %u %u %u min %u mag %u\n",c[0],c[1],data[48+3],data[48+4],data[48+30],data[48+5],data[48+6]);assert(false);}}
 // An unsupported filter still stops on every draw, also right after a cached configuration of the same layout.
 for(unsigned n=0;n<2;++n){R[0x1a14]=0x03030000;bool failed=false;try{run();}catch(const std::runtime_error&){failed=true;}assert(failed);}
 R[0x1a14]=0x02020000;run();session.flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS texture sampling words follow every address/filter change of one layout; invalid filters still stop");
}
static void fragment_keys(){
 // fragment_source keys every unit's validity (mask word: bit n valid, bit 16+n null texture) and the valid units' fields.
 // Inputs that differ only in that mask must get their own cache entry; equal inputs reuse one.
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);NativeVulkan vk;rsxgraphics::Session session(vk,s);const rsxdraw::Reg r{s.rsx};
 const auto fp=rsxdraw::load_fragment_program(s,r);
 std::array<rsxdraw::Texture,16> a{},b{},c{};a[0]=session.texture_metadata(0);assert(a[0].valid);
 b[0].null_texture=true;c=a;c[5].null_texture=true;
 auto source=[&](const std::array<rsxdraw::Texture,16>& units){auto out=rsxgraphics::fragment_source(fp,r,1,units,false,nullptr,8,8);assert(out);return out;};
 const auto sa=source(a);assert(source(a)==sa);
 const auto sb=source(b),sc=source(c);assert(sb!=sa && sc!=sa && sc!=sb);
 assert(source(a)==sa && source(c)==sc && source(b)==sb);
 std::puts("PASS fragment source keys separate valid/null texture units, equal inputs share one source");
}
static void texture_writes(){
 // A sampled 2x2 texture of one colour: every pixel of the 8x8 target shows it. Repeated draws reuse the uploaded image; a CPU
 // write of new texels (also back to the old ones), a new offset with the same bytes and a format change reach the next draw.
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);auto& R=s.rsx.registers;
 auto fill=[&](unsigned at,std::uint32_t argb){for(unsigned i=0;i<4;++i)s.memory.store(at+i*4,4,argb);};
 auto draw=[&](std::uint32_t expect){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)if(!near_argb(s.memory.load(0xc0100000+y*32+x*4,4),expect)){
   std::fprintf(stderr,"texture_writes pixel %u,%u %08llx expected %08x\n",x,y,(unsigned long long)s.memory.load(0xc0100000+y*32+x*4,4),expect);assert(false);}};
 fill(0xc0002000,0xff336699);draw(0xff336699);const auto hits=session.raw_hits;draw(0xff336699);draw(0xff336699);assert(session.raw_hits==hits+2);
 fill(0xc0002000,0xff00ff00);draw(0xff00ff00);draw(0xff00ff00);
 fill(0xc0002000,0xff336699);draw(0xff336699);
 fill(0xc0003000,0xffff0000);R[0x1a00]=0x3000;draw(0xffff0000);R[0x1a00]=0x2000;draw(0xff336699);
 s.memory.store(0xc0002004,4,0xff336699);draw(0xff336699);                 // same value: content unchanged
 session.flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS sampled texture image reuse follows CPU texel writes, offset changes and same-value stores");
}
int main(){try{level_values();image_pins();indexed_reads();shader_reads();write_membership();dynamic_state();register_reads();register_prefix();triangle_list();blob_descriptors();pipeline_lookup();sampling_words();fragment_keys();texture_writes();}catch(const std::exception& e){std::fprintf(stderr,"STOP %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN


class WarmDrawTests(unittest.TestCase):
    def test_fixed_oracles(self):
        if not shutil.which('c++'):
            self.skipTest('c++ required')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);cpp=root/'test.cpp';cpp.write_text(native_source());binary=root/'test'
            command=['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)]
            p=subprocess.run(command,capture_output=True,text=True,timeout=240)
            self.assertEqual(p.returncode,0,p.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_RSX_SUBMIT_DRAWS='0',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            p=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertEqual(p.stdout.count('PASS '),14)
            self.assertNotIn('Validation Error',p.stderr);self.assertNotIn('Validation Warning',p.stderr)
