"""Protected capture and conditional report native oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES
MAIN = r'''
static void capture22(const std::filesystem::path& root){
 PpuState s;auto& m=s.memory;m.map(0x10000,0x21000,6,nullptr,0);
 auto pin=m.regions[0].bytes;m.regions.push_back({0x80000000,6,pin});m.mapping_changed();
 m.store(0x11004,4,0x12345678);m.store(0x30ffc,4,0xdecafbad);
 m.enable_concurrency();m.enable_page_broker();
 auto dirty=m.watch_cpu_dirty(m.range(0x11000,4,false));assert(dirty);dirty->consume();
 const auto token=m.reservation_token(0x11004,4);
 assert(!mprotect(pin->data()+0x1000,4096,PROT_NONE));std::exception_ptr error;
 std::thread worker([&]{try{GuestMemory::ObserverPause pause(m);std::ofstream f(root/"protected.bin",std::ios::binary);rsx_capture_state(s,&f,nullptr);f.flush();assert(f);}catch(...){error=std::current_exception();}});worker.join();
 const int fd=open((root/"probe.bin").c_str(),O_WRONLY|O_CREAT|O_TRUNC,0600);assert(fd>=0);errno=0;
 assert(::write(fd,pin->data()+0x1000,4)==-1 && errno==EFAULT);close(fd);
 assert(!mprotect(pin->data()+0x1000,4096,PROT_READ|PROT_WRITE));if(error)std::rethrow_exception(error);
 assert(!dirty->consume());assert(m.reservation_token(0x11004,4)==token);
 PpuState restored;std::ifstream f(root/"protected.bin",std::ios::binary);rsx_capture_state(restored,nullptr,&f);
 assert(restored.memory.regions.size()==2 && restored.memory.regions[0].bytes==restored.memory.regions[1].bytes);
 assert(restored.memory.load(0x80001004,4)==0x12345678 && restored.memory.load(0x30ffc,4)==0xdecafbad);
 const auto* bytes=restored.memory.regions[0].bytes->data()+0x1004;
 assert(bytes[0]==0x12 && bytes[1]==0x34 && bytes[2]==0x56 && bytes[3]==0x78);
 auto& c=s.rsx.perf_capture;c.configured=true;c.active=true;c.root=root.string();
 c.commands=std::make_unique<std::ofstream>(root/"absent"/"commands.bin",std::ios::binary);
 rsx_capture_method(s,0x100,0xdecafbad);assert(c.done && !c.active && !c.commands && c.commands_count==0);
 assert(!std::filesystem::exists(root/"complete.json"));m.store(0x11004,4,0xabcdef01);assert(m.load(0x11004,4)==0xabcdef01);
 PpuState invalid;setenv("ANYPS3_RSX_CAPTURE",root.c_str(),1);setenv("ANYPS3_RSX_CAPTURE_FRAMES","9",1);
 rsx_capture_flip(invalid);assert(invalid.rsx.perf_capture.done && !invalid.rsx.perf_capture.active);
 unsetenv("ANYPS3_RSX_CAPTURE_FRAMES");unsetenv("ANYPS3_RSX_CAPTURE");
 std::puts("PASS protected backing: BE aliases, worker reader, raw PROT_NONE and dirty/reservation unchanged; I/O/config contained");
}
static void capture_gpu_failure22(const std::filesystem::path& root){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.draw.primitive=0;s.rsx.draw.ranges.clear();
 s.rsx.renderer=std::make_unique<NativeVulkan>();auto session=std::make_shared<rsxgraphics::Session>(*s.rsx.renderer,s);s.rsx.graphics_owner=session;
 session->failed=true;auto dir=root/"gpu-failure";setenv("ANYPS3_RSX_CAPTURE",dir.c_str(),1);
 bool stopped=false;try{rsx_capture_flip(s);}catch(const std::runtime_error& error){stopped=std::string(error.what()).find("poisoned")!=std::string::npos;}
 assert(stopped && !s.rsx.perf_capture.done && !s.rsx.perf_capture.active);
 assert(!std::filesystem::exists(dir/"complete.json") && !std::filesystem::exists(dir/"error.txt"));
 unsetenv("ANYPS3_RSX_CAPTURE");session->failed=false;s.rsx.graphics_owner.reset();
 std::puts("PASS capture retains real GPU failure as explicit runtime stop");
}

static void conditional22(){
 const auto* old_cond=std::getenv("ANYPS3_RSX_GPU_CONDITIONAL");const std::string old_value=old_cond?old_cond:"";setenv("ANYPS3_RSX_GPU_CONDITIONAL","0",1);
 PpuState s;setup_draw(s,8,8,32,false);auto& g=s.rsx;auto& m=s.memory;
 g.reports=0x70008000;g.registers[0x1a8]=0x66626660;
 m.regions.push_back({0x81000000,6,m.regions[0].bytes});m.mapping_changed();
 g.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*g.renderer,s);
 session.observe();g.graphics_session=&session;session.submit_draws=0;setenv("ANYPS3_RSX_BACKEND","gpu",1);
 g.registers[0x17cc]=1;rsx_execute_draw(s); // 8x8 quad, precisely 64 visible samples
 const auto downloads=session.batch->download_bytes;const auto submits=session.async_submits;
 m.store(g.reports+0x1428,4,1);
 for(unsigned n=0;n<64;++n)rsx_set_render_enable(s,0x02000020);
 assert(!g.render_disabled && session.async_submits==submits && session.batch->download_bytes==downloads);
 assert(!session.pending.empty() && session.surfaces.front().dirty);
 // Exact report production must complete; ordinary polling may see old zero.
 m.store(g.reports+0x1438,4,0);assert(session.defer_report(g.reports+0x1430,1,false,false));
 rsx_set_render_enable(s,0x02000030);assert(!g.render_disabled);
 assert(m.load(g.reports+0x1438,4)==64 && session.async_submits==submits+1);
 assert(session.batch->download_bytes==downloads && session.surfaces.front().dirty);
 // Physical alias of an in-flight producer is the same dependency. The report
 // contains a fixed base but no additional geometry; a later prefix remains queued.
 g.report_values[0]=7;assert(session.defer_report(g.reports+0x1440,1,false,false));session.submit_async("test_prefix");
 g.report_values[0]=0;assert(session.defer_report(g.reports+0x1450,1,false,false));
 g.reports=0x81008000;rsx_set_render_enable(s,0x02000040);assert(!g.render_disabled);
 assert(m.load(0x70009448,4)==7 && session.labels.size()==1 && session.labels[0].address==0x70009450);
 g.reports=0x70008000;rsx_set_render_enable(s,0x02000050);assert(g.render_disabled);
 // All original target bytes still materialize through the usual observer.
 assert(near_argb(m.load(0xc0100000,4),0x80ff8040));g.graphics_session=nullptr;session.flush();
 if(old_value.empty())unsetenv("ANYPS3_RSX_GPU_CONDITIONAL");else setenv("ANYPS3_RSX_GPU_CONDITIONAL",old_value.c_str(),1);
 std::puts("PASS conditional report: no unrelated submit/readback; 64 pixel query; BE zero; alias prefix wait, later producer retained");
}


static void masked_packed22(){
 for(bool indexed:{false,true})for(unsigned aa:{0u,3u,4u,5u}){
  const unsigned samples=aa==0?1:aa==3?2:4,nx=samples>1?2:1,ny=samples==4?2:1;
  PpuState s;setup_draw(s,8,8,64,false);auto& g=s.rsx;auto& r=g.registers;auto& m=s.memory;
  r[0x208]=(r[0x208]&~31u)|3u|(aa<<12);r[0x1fec]=1;r[0x324]=0;r[0x370]=0;
  r[0x20c]=8*nx*2+8;r[0x22c]=8*nx*4+8;r[0xa74]=1;r[0xa70]=1;r[0xa6c]=0x207;
  r[0x328]=1;r[0x32c]=255;r[0x330]=0x207;r[0x334]=0;r[0x338]=255;r[0x33c]=r[0x340]=0x1e00;r[0x344]=0x1e02;
  r[0x17cc]=1;
  // Two identical full-screen quads: each sample passes twice. Top-left
  // ownership splits each quad into two triangles without edge double hits.
  for(unsigned i=0;i<4;++i){for(unsigned c=0;c<4;++c)m.store(0x70002000+(i+4)*16+c*4,4,m.load(0x70002000+i*16+c*4,4));
   for(unsigned c=0;c<2;++c)m.store(0x70002100+(i+4)*8+c*4,4,m.load(0x70002100+i*8+c*4,4));}
  const unsigned blue=0x3f800000;m.store(0xc0001018,4,(blue<<16)|(blue>>16));
  for(unsigned y=0;y<8*ny;++y){for(unsigned x=0;x<8*nx;++x){m.store(0xc0100000+y*r[0x20c]+x*2,2,0xbeef);m.store(0xc0500000+y*r[0x22c]+x*4,4,0xffffff5a);}
   m.store(0xc0100000+y*r[0x20c]+8*nx*2,4,0x12345678);m.store(0xc0500000+y*r[0x22c]+8*nx*4,4,0xdecafbad);}
  g.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*g.renderer,s);session.observe();g.graphics_session=&session;session.submit_draws=0;
  const unsigned indices[]={0,1,2,0,2,3,4,5,6,4,6,7};
  if(indexed){r[0x1820]=0x11;r[0x181c]=0x3000;for(unsigned i=0;i<12;++i)m.store(0x70003000+i*2,2,indices[i]);}
  g.draw.indexed=indexed;g.draw.primitive=indexed?5:8;g.draw.ranges={{0,indexed?12u:8u}};setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(s);session.flush();
  assert(g.renderer->graphics_draws==1 && g.report_values[0]==128*samples);
  for(unsigned y=0;y<8*ny;++y){for(unsigned x=0;x<8*nx;++x){assert(m.load(0xc0100000+y*r[0x20c]+x*2,2)==0xbeef);assert(m.load(0xc0500000+y*r[0x22c]+x*4,4)==0x4000005c);}
   assert(m.load(0xc0100000+y*r[0x20c]+8*nx*2,4)==0x12345678);assert(m.load(0xc0500000+y*r[0x22c]+8*nx*4,4)==0xdecafbad);}
  // A single enabled channel restores per-primitive history. Quantization of
  // blue1.0 is exactly31, so the fixed RGB565 result is 0xbeff.
  r[0x324]=1;g.draw.indexed=indexed;g.draw.primitive=indexed?5:8;g.draw.ranges={{0,indexed?12u:8u}};rsx_execute_draw(s);session.flush();
  assert(g.renderer->graphics_draws==5 && g.report_values[0]==256*samples);
  for(unsigned y=0;y<8*ny;++y)for(unsigned x=0;x<8*nx;++x){assert(m.load(0xc0100000+y*r[0x20c]+x*2,2)==0xbeff);assert(m.load(0xc0500000+y*r[0x22c]+x*4,4)==0x4000005e);}
  g.graphics_session=nullptr;
 }
 std::puts("PASS packed zero-mask batch: exact RGB565, Z24/stencil order, report128/sample, row padding, BE16 indexed triangles/array quads, MSAA1/2/4 and partial-mask serial path");
}


static void metadata_lru22(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& r=s.rsx.registers;auto& m=s.memory;
 r[0x1a18]=0x00010001;r[0x1840]=4;
 for(unsigned i=0;i<513;++i)m.store(0xc0004000+i*64,4,0xff000000u+i);
 NativeVulkan vk;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto texture=[&](unsigned i){r[0x1a00]=0x4000+i*64;return session.texture_metadata(0);};
 for(unsigned i=0;i<512;++i){auto t=texture(i);assert(rsxdraw::read_be(t.plan->runs.front().data,4)==0xff000000u+i);}
 assert(session.texture_metadata_misses==512 && session.texture_plans.size()==512);
 auto hot=texture(0);assert(session.texture_metadata_hits==1);
 (void)texture(512);assert(session.texture_metadata_misses==513 && session.texture_plans.size()==512);
 auto again=texture(0);assert(again.plan==hot.plan && session.texture_metadata_misses==513 && session.texture_metadata_hits==2);
 (void)texture(1);assert(session.texture_metadata_misses==514 && session.texture_plans.size()==512);
 // Metadata retention never snapshots texture payload. A new BE producer byte
 // is immediately visible through the same exact physical plan.
 m.store(0xc0004000,4,0xcafebabe);again=texture(0);assert(again.plan==hot.plan && rsxdraw::read_be(again.plan->runs.front().data,4)==0xcafebabe);
 assert(session.texture_plan_bytes<=64*1024*1024);s.rsx.graphics_session=nullptr;
 std::puts("PASS metadata LRU: hot lowest address survives key513, cold key1 misses, fixed512/64MiB bounds and live BE payload");
}

// Per-unit memo of the last metadata lookup: hits keep LRU order and sampling state live; an eviction of the memoized entry
// (by another unit's miss) must never be served from the memo.
static void metadata_memo22(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& r=s.rsx.registers;auto& m=s.memory;
 r[0x1a18]=0x00010001;r[0x1840]=4;
 for(unsigned reg:{0x1a04u,0x1a08u,0x1a0cu,0x1a10u,0x1a14u,0x1a18u,0x1a1cu}){r[reg+32]=r[reg];}
 r[0x1844]=r[0x1840];
 for(unsigned i=0;i<513;++i)m.store(0xc0004000+i*64,4,0xff000000u+i);
 NativeVulkan vk;rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto texture=[&](unsigned unit,unsigned i){r[0x1a00+32*unit]=0x4000+i*64;return session.texture_metadata(unit);};
 auto t0=texture(0,0);auto t1=texture(0,0);assert(session.texture_metadata_misses==1 && session.texture_metadata_hits==1 && t1.plan==t0.plan);
 // sampling registers are not part of the key: a memo hit still applies the current filter
 r[0x1a14]=(2u<<24)|(1u<<16);auto f=texture(0,0);assert(f.mag==2 && f.min==1 && f.plan==t0.plan);
 r[0x1a14]=(1u<<24)|(2u<<16);f=texture(0,0);assert(f.mag==1 && f.min==2 && session.texture_metadata_hits==3);
 auto u=texture(1,0);assert(u.plan==t0.plan && session.texture_metadata_misses==1 && session.texture_metadata_hits==4);
 // key 0 becomes the least recently used of 512 entries; a miss on unit 1 evicts it while unit 0's memo still names it
 for(unsigned i=1;i<512;++i)(void)texture(1,i);
 assert(session.texture_metadata_misses==512 && session.texture_plans.size()==512);
 (void)texture(1,512);assert(session.texture_metadata_misses==513 && session.texture_plans.size()==512 && !session.texture_plans.count({0x4000,r[0x1a04],r[0x1a18],r[0x1840]}));
 m.store(0xc0004000,4,0xcafebabe);
 auto again=texture(0,0);assert(session.texture_metadata_misses==514 && rsxdraw::read_be(again.plan->runs.front().data,4)==0xcafebabe);
 auto hit=texture(0,0);assert(session.texture_metadata_misses==514 && hit.plan==again.plan);
 s.rsx.graphics_session=nullptr;
 std::puts("PASS metadata memo: repeated key hits with live sampling state, shared across units, evicted entry is a miss");
}

int main(int argc,char** argv){try{if(argc!=2)return 2;capture22(argv[1]);capture_gpu_failure22(argv[1]);conditional22();masked_packed22();metadata_lru22();metadata_memo22();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return '#include <fcntl.h>\n'+graphics_source().split('int main(int argc,char** argv)')[0]+MAIN
class Rsxvk22Tests(unittest.TestCase):
    def test_capture_and_report_oracles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cpp=root/'test.cpp';binary=root/'test';cpp.write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('ANYPS3_REQUIRE_NVIDIA'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            run=subprocess.run([str(binary),str(root)],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stderr);self.assertNotIn('Validation Warning',run.stderr)
            self.assertEqual(run.stdout.count('PASS '),6)
