"""Fixed native oracles for lazy uploads and physical page-indexed coherence."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from anyps3.rsx_capture import REPLAY_CPP
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void page_observer(){
 GuestMemory m;auto bytes=std::make_shared<GuestBacking>(65536);m.regions.push_back({0x10000,6,bytes});m.regions.push_back({0x80000000,6,bytes});
 unsigned reads=0,writes=0;m.host_access_observer=[&](const void*,std::uint64_t,bool w){if(w)++writes;else ++reads;};
 m.host_access_observer.filtered=true;m.host_access_observer.watch(bytes->data()+0x8000,16);
 GuestMemory::HostObserver copied;copied=m.host_access_observer;assert(copied.filtered && copied.pages==m.host_access_observer.pages);
 for(unsigned i=0;i<1000;++i)m.store(0x10000,4,i);
 assert(writes==0 && reads==0);
 m.store(0x80008000,4,0x12345678);assert(writes==1);assert(m.load(0x18000,4)==0x12345678 && reads==1);
 auto range=m.range(0x17ff0,32,false);const auto r=reads;std::uint8_t out[32];range.read(0,out,32);assert(reads==r+1);
 m.host_access_observer.watch(bytes->data()+0x8000,16);m.host_access_observer.watch(bytes->data()+0x8000,16,false);m.load(0x18000,4);assert(reads==r+2);
 m.host_access_observer.watch(bytes->data()+0x8000,16,false);m.load(0x18000,4);assert(reads==r+2);
 // Legacy replacement resets filtering; exceptions restore recursion state.
 m.host_access_observer=[](const void*,std::uint64_t,bool){throw std::runtime_error("expected");};bool failed=false;try{m.load(0x10000,4);}catch(const std::runtime_error&){failed=true;}assert(failed && !m.in_host_access_observer);
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++reads;};m.load(0x10000,4);assert(reads==r+3);
 std::puts("PASS page filter: alias, retained split span, refcount, replacement and exceptions");
}
static void lazy_tiled(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;
 auto& tile=s.rsx.tiles[0];tile.bound=true;tile.offset=0x80000;tile.size=0x10000;tile.pitch=256;
 R[0x1a00]=tile.offset;R[0x1a18]=0x80008;R[0x1840]=32;
 // Eight literal RSX tile rows; texture pitch differs from memory tile pitch.
 const unsigned rows[]={0,0x100,0x280,0x380,0x400,0x500,0x680,0x780};
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)s.memory.store(0xc0080000+rows[y]+4*x,4,0xff112233);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 auto t=session.texture_metadata(0);assert(t.deferred && !t.untiled && t.plan->gathered && !t.levels[0].data);
 const auto n=rsxdraw::host_stats().texture_materialize;auto image=session.texture_image(t);assert(rsxdraw::host_stats().texture_materialize==n+1);
 const auto cpu=rsxdraw::host_stats().cpu_texel;
 for(unsigned i=0;i<100;++i){auto next=session.texture_metadata(0);assert(next.plan==t.plan && session.texture_image(next)==image);}
 assert(session.raw_hits==100 && session.raw_misses==1 && rsxdraw::host_stats().texture_materialize==n+1 && rsxdraw::host_stats().cpu_texel==cpu);
 auto copy=t;rsxdraw::materialize_texture(s,copy);auto c=rsxdraw::texel(copy,copy.levels[0],7,7);assert(c[0]==17/255.f && c[1]==34/255.f && c[2]==51/255.f && c[3]==1);
 // Exact host alias writes, and a write to an unrelated tracked page gap.
 auto backing=s.memory.regions[1].bytes;s.memory.regions.push_back({0x81000000,6,backing});const auto invalid=session.raw_invalidations;
 s.memory.store(0x81080080,4,0xffeeeeee);assert(session.raw_invalidations==invalid); // gap outside all source runs
 s.memory.store(0x81080000,4,0xff224466);assert(session.raw_invalidations==invalid+1);
 auto next=session.texture_metadata(0);assert(session.texture_image(next)!=image);
 // Cached metadata must still reject a changed pitch/permission/tile layout.
 R[0x1840]=1;bool failed=false;try{session.texture_metadata(0);}catch(const std::runtime_error&){failed=true;}assert(failed);R[0x1840]=32;
 s.memory.regions[1].flags=2;failed=false;try{session.texture_metadata(0);}catch(const std::runtime_error&){failed=true;}assert(failed);s.memory.regions[1].flags=6;
 tile.compression=1;failed=false;try{session.texture_metadata(0);}catch(const std::runtime_error&){failed=true;}assert(failed);tile.compression=0;
 // Earlier guest overlaps must still win even when neighbouring 32B tile
 // runs reused a cached physical region. A deny interval cannot be skipped.
 auto prefix=std::make_shared<GuestBacking>(32);s.memory.regions.insert(s.memory.regions.begin()+1,{0xc0080100,2,prefix});
 failed=false;try{session.texture_metadata(0);}catch(const std::runtime_error&){failed=true;}assert(failed);
 s.memory.regions[1].flags=6;(*prefix)[0]=0xff;(*prefix)[1]=0xab;(*prefix)[2]=0xcd;(*prefix)[3]=0xef;
 auto overlap=session.texture_metadata(0);rsxdraw::materialize_texture(s,overlap);auto value=rsxdraw::texel(overlap,overlap.levels[0],0,1);assert(value[0]==171/255.f && value[1]==205/255.f && value[2]==239/255.f);
 s.memory.regions.erase(s.memory.regions.begin()+1);
 session.flush();s.rsx.graphics_session=nullptr;
 std::puts("PASS lazy tiled upload, fixed row oracle, exact physical invalidation and metadata errors");
}
static void split_and_resident(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;
 // Split the four-pixel source across separate physical allocations.
 auto old=s.memory.regions[1];auto a=std::make_shared<GuestBacking>(old.bytes->begin(),old.bytes->begin()+0x2008);
 auto b=std::make_shared<GuestBacking>(old.bytes->begin()+0x2008,old.bytes->end());s.memory.regions[1]={old.base,6,a};s.memory.regions.push_back({old.base+0x2008,6,b});
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 auto t=session.texture_metadata(0);assert(t.deferred && t.plan->gathered && t.plan->runs.size()==2);auto image=session.texture_image(t);assert(session.texture_image(session.texture_metadata(0))==image);
 const auto invalid=session.raw_invalidations;s.memory.store(0xc0002008,4,0xff010203);assert(session.raw_invalidations==invalid+1);assert(session.texture_image(session.texture_metadata(0))!=image);
 setenv("ANYPS3_RSX_BACKEND","gpu",1);s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);
 // Resident RTT must neither read the old guest pixels nor submit a fence.
 R[0x210]=0x300000;R[0x1a00]=0x100000;R[0x1a18]=0x80008;R[0x1840]=32;
 const auto submits=session.batch->submissions,decodes=rsxdraw::host_stats().texture_materialize,miss=session.raw_misses;
 auto resident=session.texture_metadata(0);assert(resident.resident_image);
 for(unsigned i=0;i<10;++i){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
 assert(session.batch->submissions==submits && session.raw_misses==miss && rsxdraw::host_stats().texture_materialize==decodes);
 session.flush();assert(near_argb(s.memory.load(0xc0300000,4),0xff112233));s.rsx.graphics_session=nullptr;
 std::puts("PASS split backing invalidation and resident RTT without per-draw fence/readback");
}
static void report_reset(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.registers[0x17cc]=1;
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 rsx_execute_draw(s);const auto submits=session.batch->submissions;
 rsx_graphics_reset_report(s,1);s.rsx.report_values[0]=0;
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);assert(session.batch->submissions==submits);
 session.flush();assert(s.rsx.draw.pixels==128 && s.rsx.report_values[0]==64);
 // Match scanout's direct finish/readback followed by an otherwise empty flush.
 s.rsx.renderer->graphics_transfer(*session.batch,session.surfaces[0].image,VK_IMAGE_ASPECT_COLOR_BIT,4);
 s.rsx.renderer->graphics_finish(*session.batch);session.flush(false);assert(session.batch->used==0);
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush(false);s.rsx.graphics_session=nullptr;
 std::puts("PASS deferred report reset: two draws, 128 lifetime pixels, 64 post-reset pixels");
}
static void capture_roundtrip(const std::string& directory){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.draw.primitive=0;s.rsx.draw.ranges.clear();auto& g=s.rsx;
 g.context=1;g.device=0x70006000;g.dma=0x70007000;g.driver=0x70008000;g.reports=0x7000a000;
 // Preserve physical alias identity as well as big-endian memory bytes.
 s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});
 setenv("ANYPS3_RSX_CAPTURE",directory.c_str(),1);rsx_capture_flip(s);assert(g.perf_capture.active);
 unsigned at=0;auto packet=[&](unsigned reg,unsigned value){s.memory.store(0x70000000+at,4,(1u<<18)|reg);s.memory.store(0x70000004+at,4,value);at+=8;};
 packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);
 s.memory.store(g.dma+0x40,4,at);s.memory.store(g.dma+0x44,4,0);g.fifo_enabled=true;assert(rsx_run(s,8));rsx_capture_flip(s);assert(g.perf_capture.done && g.perf_capture.commands_count==3);
 unsetenv("ANYPS3_RSX_CAPTURE");
 PpuState replay;std::ifstream state(std::filesystem::path(directory)/"state.bin",std::ios::binary),commands(std::filesystem::path(directory)/"commands.bin",std::ios::binary);
 rsx_replay_capture(replay,state,commands);
 assert(replay.memory.regions[1].bytes==replay.memory.regions[2].bytes);
 assert(replay.rsx.draw.draws==1);assert(near_argb(replay.memory.load(0xc0100000,4),0x80ff8040));
 std::istringstream truncated("ANY");PpuState invalid;bool failed=false;try{rsx_capture_state(invalid,nullptr,&truncated);}catch(const std::runtime_error&){failed=true;}assert(failed);
 std::puts("PASS capture BE state/shared backing/FIFO replay, fixed pixel oracle and truncation");
}
static void gpu_write_invalidates(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& R=s.rsx.registers;
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002100+i*8+c*4,4,0);
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 auto initial=session.texture_image(session.texture_metadata(0));const auto invalid=session.raw_invalidations;
 R[0x210]=0x2000;fpword(s,0,(1u<<24)|(15u<<9)|1u);fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
 const float green[]={0,1,0,1};for(unsigned c=0;c<4;++c)fpword(s,4+c,rsxdraw::float_bits(green[c]));
 rsx_execute_draw(s);assert(session.raw_invalidations==invalid+1);
 // This subregion is a valid GPU view. It must observe the new generation
 // without consulting the old upload, even before any CPU access/readback.
 R[0x210]=0x100000;fpword(s,0,(23u<<24)|(15u<<9)|(4u<<13)|1u);fpword(s,1,1u|(0xe4u<<9)|(7u<<18));
 auto latest=session.texture_metadata(0);assert(latest.resident_image && latest.resident_image!=initial);
 s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0xff00ff00));s.rsx.graphics_session=nullptr;
 std::puts("PASS GPU writes invalidate cached uploads before incompatible texture reuse");
}
int main(int argc,char** argv){try{setenv("ANYPS3_RSX_HOST_PROFILE","1",1);page_observer();lazy_tiled();split_and_resident();report_reset();gpu_write_invalidates();if(argc>1)capture_roundtrip(argv[1]);}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+REPLAY_CPP+MAIN

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class HostCostTests(unittest.TestCase):
    def test_lazy_physical_coherence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'test.cpp').write_text(native_source())
            p=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(p.returncode,0,p.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            p=subprocess.run([str(root/'test'),str(root/'capture')],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertNotIn('Validation Error',p.stdout+p.stderr);self.assertNotIn('Validation Warning',p.stdout+p.stderr)
            self.assertEqual(p.stdout.count('PASS '),6)
