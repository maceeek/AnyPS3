"""Independent reservation, pin lifetime and exact raw invalidation oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void target_tracking_(){
 PpuState s;setup_draw(s,8,8,32,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 const auto unrelated=s.memory.reservation_token(0xc0003000,4);
 session.validate_texture_context();rsxdraw::fifo_target_cache=&session.target_cache;
 auto bind=[&]{return rsxdraw::bind_target(s,0xfeed0000,0x100000,32,8,8);};
 auto first=bind();assert(!first.observed && s.memory.reservation_token(0xc0003000,4)==unrelated);
 auto second=bind();assert(second.mapping==first.mapping && !second.observed);
 // A new reservation after a previous negative cache result is observed.
 const auto token=s.memory.reservation_token(0xc0100000,4);
 auto third=bind();assert(third.mapping==first.mapping && third.observed);
 assert(s.memory.reservation_token(0xc0100000,4).second==token.second+1);
 auto* pixel=third.at(0,0);third.mark(s.memory,0,0,pixel);
 assert(s.memory.reservation_token(0xc0100000,4).second==token.second+2);
 assert(s.memory.reservation_token(0xc0003000,4)==unrelated);
 // Replaced keys with unchanged count are refreshed at the next FIFO turn.
 s.memory.write_versions=decltype(s.memory.write_versions){};(void)s.memory.reservation_token(0xc0004000,4);(void)s.memory.reservation_token(0xc0005000,4);
 session.context_validated=false;session.validate_texture_context();assert(!bind().observed);
 // A CPU alias shares the physical reservation line, not its guest address.
 s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});s.memory.mapping_changed();
 (void)s.memory.reservation_token(0x81100000,4);session.validate_texture_context();assert(bind().observed);
 // IO relocation must discard cached physical mappings.
 s.rsx.io[0]=0x81100000;session.validate_texture_context();
 auto io=rsxdraw::bind_target(s,0xfeed0001,0,32,8,8);assert(io.base==first.base);
 s.rsx.io[0]=0x10000000;session.validate_texture_context();bool rejected=false;
 try{(void)rsxdraw::bind_target(s,0xfeed0001,0,32,8,8);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 rsxdraw::fifo_target_cache=nullptr;
 std::puts("PASS target cache live reservations, alias identity, same-count refresh and IO relocation");
}
static void raw_pages_(){
 PpuState s;setup_draw(s,8,8,32,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 auto* p=s.memory.regions[1].bytes->data()+0x200000;
 auto add=[&](unsigned at,unsigned size,bool alias){rsxgraphics::Session::RawTexture e{};e.source=p+at;e.size=size;e.cost=size;
  if(alias){e.origin=p+at+4;e.origin_size=size;}session.raw_bytes+=size;session.raw_textures.push_back(std::move(e));session.index_raw(session.raw_textures.back());return session.raw_textures.back().id;};
 const auto a=add(0,8192,true),b=add(9000,128,false),c=add(64,32,false);
 // One entry may touch the same pages through source and origin; unique IDs.
 for(const auto& page:session.raw_pages)assert(std::count(page.second.begin(),page.second.end(),a)<=1);
 session.invalidate_raw(p+48,1,"oracle");assert(session.raw_positions.count(a)==0 && session.raw_positions.count(b)==1 && session.raw_positions.count(c)==1 && session.raw_invalidations==1);
 session.invalidate_raw(p+63,1,"oracle");assert(session.raw_positions.count(c)==1); // exact bytes, same page
 session.invalidate_raw(p+64,1,"oracle");assert(session.raw_positions.count(c)==0 && session.raw_positions.count(b)==1 && session.raw_invalidations==2);
 session.invalidate_raw(p,16384,"oracle");assert(session.raw_textures.empty() && session.raw_pages.empty() && session.raw_positions.empty() && session.raw_bytes==0 && session.raw_invalidations==3);
 // Warm candidates storage survives repeated invalidation without tree nodes.
 add(0,8192,true);session.invalidate_raw(p,16384,"oracle");const auto capacity=session.invalidation_candidates.capacity();add(0,8192,true);session.invalidate_raw(p,16384,"oracle");assert(session.invalidation_candidates.capacity()==capacity);
 std::puts("PASS duplicate raw pages, exact boundaries, moved positions and retained invalidation storage");
}
static void pipeline_pins_(){
 struct Resource {unsigned id;explicit Resource(unsigned n):id(n){}};
 NativeVulkan::GraphicsPins<Resource> pins;std::vector<std::weak_ptr<Resource>> weak;
 for(unsigned n=0;n<600;++n){auto resource=std::make_shared<Resource>(n);weak.push_back(resource);for(unsigned j=0;j<4;++j)pins.push_back(resource);}
 assert(pins.size()==600 && pins.bytes==0);for(auto& resource:weak)assert(!resource.expired());
 const auto capacity=pins.owners.capacity(),slots=pins.slots.size();pins.clear();for(auto& resource:weak)assert(resource.expired());
 for(unsigned n=0;n<600;++n){auto resource=std::make_shared<Resource>(n);pins.push_back(resource);pins.push_back(resource);}
 assert(pins.size()==600 && pins.owners.capacity()==capacity && pins.slots.size()==slots);
 std::puts("PASS pipeline pin uniqueness, growth, completion lifetime and allocation reuse");
}
static void draw_reads_(){
 PpuState s;auto& m=s.memory;m.map(0x10000,4096,6,nullptr,0);m.map(0x20000,4096,6,nullptr,0);m.map(0x21000,4096,6,nullptr,0);m.map(0x30000,4096,6,nullptr,0);
 m.regions.push_back({0x40000,6,m.regions[0].bytes});m.mapping_changed();
 m.store(0x10000,4,0x11223344);m.store(0x20ffc,4,0x55667788);m.store(0x21000,4,0x99aabbcc);m.store(0x30000,4,0xddeeff00);m.enable_concurrency();
 auto a=m.range(0x40000,4,false),b=m.range(0x20ffc,8,false),c=m.range(0x30000,4,false);
 rsxdraw::DrawReadBatch reads;reads.add(a,0);reads.add(b,8);reads.add(c,20);std::array<std::uint8_t,24> out;out.fill(0x5a);reads.read(out.data());
 const std::array<std::uint8_t,24> expected{0x11,0x22,0x33,0x44,0x5a,0x5a,0x5a,0x5a,0x55,0x66,0x77,0x88,0x99,0xaa,0xbb,0xcc,0x5a,0x5a,0x5a,0x5a,0xdd,0xee,0xff,0};assert(out==expected);
 m.store(0x10000,4,0x01234567);reads.read(out.data());assert(out[0]==1 && out[1]==0x23 && out[2]==0x45 && out[3]==0x67);
 // Exact pins must be revalidated even when reused from certification.
 {GuestMemory::RawPermit permit(m);m.regions.back().flags=2;m.mapping_changed();}
 bool rejected=false;try{reads.read(out.data());}catch(const std::runtime_error&){rejected=true;}assert(rejected);
 std::puts("PASS one draw input claim, split BE bytes, alias mutation, destination holes and changed rights");
}
static void draw_read_coherence_(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);PpuState s;setup_draw(s,8,8,32,false);s.memory.enable_concurrency();
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();s.rsx.graphics_session=&session;
 rsx_execute_draw(s);assert(!session.surfaces.empty());auto input=s.memory.range(0xc0100000,4,false);
 rsxdraw::DrawReadBatch read;read.add(input,0);std::array<std::uint8_t,4> bytes{};read.read(bytes.data());
 assert(near_argb(rsxdraw::read_be(bytes.data(),4),0x80ff8040));s.rsx.graphics_session=nullptr;session.flush();
 std::puts("PASS draw claim observes actual pending Vulkan output before snapshot");
}
static void session_backend_(){
 PpuState s;setup_draw(s,8,8,32,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);s.rsx.renderer=std::make_unique<NativeVulkan>();
 s.rsx.graphics_session=&session;setenv("ANYPS3_RSX_BACKEND","invalid",1);rsx_execute_draw(s);session.flush();s.rsx.graphics_session=nullptr;
 assert(vk.graphics_draws==1);for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(near_argb(s.memory.load(0xc0100000+y*32+4*x,4),0x80ff8040));
 s.rsx.draw.ranges={{0,4}};bool rejected=false;try{rsx_execute_draw(s);}catch(const std::runtime_error&){rejected=true;}assert(rejected);setenv("ANYPS3_RSX_BACKEND","gpu",1);
 std::puts("PASS admitted Session backend and independent standalone environment validation");
}
// Texture-context validation (once per FIFO turn): the cached plans are dropped (generation bump) exactly when one of its inputs
// changed since the last validation: local size, an IO page or its flags, a tile field, a mapping's base/rights/identity/backing.
// Independent expectation: +1 per changed validation, +0 for an unchanged one, and none while the FIFO scope holds it validated.
static void texture_context_(){
 PpuState s;setup_draw(s,8,8,32,false);NativeVulkan vk;rsxgraphics::Session session(vk,s);
 auto& g=s.rsx;session.validate_texture_context();
 std::uint64_t expect=session.texture_plans_generation;
 auto check=[&](bool changed,const char* what){session.context_validated=false;session.validate_texture_context();expect+=changed;
  if(session.texture_plans_generation!=expect)throw std::runtime_error(std::string("texture context ")+what+": generation "+std::to_string(session.texture_plans_generation)+" expected "+std::to_string(expect));};
 check(false,"unchanged");check(false,"unchanged again");
 const auto io=g.io[3];g.io[3]=0x81000000;check(true,"io page");check(false,"io page kept");g.io[3]=io;check(true,"io page restored");
 g.io_flags[5]^=1;check(true,"io flags");g.io_flags[5]^=1;check(true,"io flags restored");
 g.tiles[2].pitch+=64;check(true,"tile pitch");g.tiles[14].bound=!g.tiles[14].bound;check(true,"last tile bound");
 g.local_size-=0x1000;check(true,"local size");check(false,"local size kept");
 s.memory.regions.push_back({0x81000000,6,std::make_shared<GuestBacking>(0x1000)});check(true,"added mapping");
 s.memory.regions.back().flags=2;check(true,"mapping rights");
 s.memory.regions.back().bytes=std::make_shared<GuestBacking>(0x1000);check(true,"mapping backing");check(false,"mapping kept");
 s.memory.regions.pop_back();check(true,"removed mapping");
 // Inside a FIFO scope the validated context is reused without a check.
 session.fifo_context=true;session.context_validated=false;session.validate_texture_context();
 g.io[7]=0x81000000;session.validate_texture_context();if(session.texture_plans_generation!=expect)throw std::runtime_error("validated FIFO context was re-checked");
 session.fifo_context=false;check(true,"io page after the FIFO scope");
 std::puts("PASS texture context validation drops plans exactly when an input changed");
}
int main(){try{texture_context_();target_tracking_();raw_pages_();pipeline_pins_();draw_reads_();draw_read_coherence_();session_backend_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+MAIN

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class GraphicsCostsTests(unittest.TestCase):
    def test_native_graphics_costs(self):
        with tempfile.TemporaryDirectory(prefix='anyps3-graphics-costs-') as tmp:
            cpp=Path(tmp)/'test.cpp';binary=Path(tmp)/'test';cpp.write_text(native_source())
            result=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run([str(binary)],env=dict(os.environ,LP_NUM_THREADS='4'),capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout.count('PASS '),7,result.stdout)
