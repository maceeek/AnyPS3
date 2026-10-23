"""Real FIFO residency and retained-pointer coherence using actual Vulkan draws."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

RESIDENCY = r'''
static void graphics_fifo_residency(unsigned width=32,unsigned height=16,unsigned draws=8,bool bench=false){
 PpuState s;setup_draw(s,width,height,width*4,false);auto& gpu=s.rsx;auto& R=gpu.registers;auto& m=s.memory;
 // Large tests need separated color/depth extents, with immutable map size.
 if(bench){R[0x214]=0x500000;}
 m.map(0x60000000,0x1000,6,nullptr,0);m.map(0x80000000,0x10000,6,nullptr,0);
 gpu.dma=0x60000000;gpu.reports=0x80000000;gpu.fifo_enabled=true;gpu.draw.primitive=0;gpu.draw.ranges.clear();
 auto retained=m.range(0xc0100000,16,false);volatile auto* raw=retained.parts[0].data;
 auto token=m.reservation_token(0xc0100000,4);
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<draws;++i){packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);}
 m.store(gpu.dma+0x44,4,0x100);m.store(gpu.dma+0x40,4,put);setenv("ANYPS3_RSX_BACKEND","gpu",1);
 if(bench){
  // Warm shaders/pipelines separately; the measured FIFO still owns a fresh
  // image batch, so every submission/readback assertion remains independent.
  gpu.renderer=std::make_unique<NativeVulkan>();gpu.draw.primitive=8;gpu.draw.ranges={{0,4}};rsx_execute_draw(s);gpu.draw.primitive=0;
 }
 const auto started=std::chrono::steady_clock::now();
 // Each call consumes a SINGLE method packet, like frequent scheduler turns.
 std::uint64_t stable_protection=0;unsigned turns=0;while(m.load(gpu.dma+0x44,4)!=put){assert(rsx_run(s,1));++turns;
  m.store(0x70008000,4,turns);assert(m.load(0x70008000,4)==turns);
  auto* live=static_cast<rsxgraphics::Session*>(gpu.graphics_owner.get());
  if(turns==3)stable_protection=live->guard.protection_calls;
  if(turns>3)assert(live->guard.protection_calls==stable_protection);
 }
 auto& session=*static_cast<rsxgraphics::Session*>(gpu.graphics_owner.get());
 assert(gpu.renderer->graphics_draws==draws+(bench?1u:0u));assert(session.batch->submissions==0);
 assert(session.batch->download_bytes==4*draws);assert(!session.guard.spans.empty());
 const auto recorded=std::chrono::steady_clock::now();
 // Capture reads the GPU image directly; guest backing stays dirty/resident.
 gpu.display_buffers[0]={width,height,width*4,0x100000};std::vector<std::uint8_t> rgba;
 assert(rsx_graphics_scanout(s,0,&rgba,false));assert(rgba[0]==255 && near_byte(rgba[1],128) && near_byte(rgba[2],64) && rgba[3]==255);
 assert(!session.surfaces.empty() && session.surfaces[0].dirty);
 const auto captured=std::chrono::steady_clock::now();
 // A raw retained pointer has no observer call: SIGSEGV must synchronize it.
 assert(near_byte(raw[0],128) && raw[1]==255 && near_byte(raw[2],128) && near_byte(raw[3],64));
 assert(session.surfaces.empty() && session.guard.spans.empty());
 const auto synchronized=std::chrono::steady_clock::now();
 assert(m.reservation_token(0xc0100000,4).second!=token.second);
 if(bench){const auto& b=*session.batch;
  std::printf("RESIDENCY_BENCH {\"width\":%u,\"height\":%u,\"draws\":%u,\"fifo_turns\":%u,\"record_ms\":%.6f,\"capture_ms\":%.6f,\"guest_sync_ms\":%.6f,\"commit_ms\":%.6f,\"gpu_draw_ms\":%.6f,\"submissions\":%llu,\"download_bytes\":%llu}\n",
   width,height,draws,turns,std::chrono::duration<double,std::milli>(recorded-started).count(),std::chrono::duration<double,std::milli>(captured-recorded).count(),
   std::chrono::duration<double,std::milli>(synchronized-captured).count(),session.commit_ms,b.draw_ms,(unsigned long long)b.submissions,(unsigned long long)b.download_bytes);
  return;
 }
 // Re-arm with a clean resident image, then raw partial write must invalidate
 // it before a masked GPU draw can reload/preserve that byte.
 auto queue=[&]{put=0x100;packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);m.store(gpu.dma+0x44,4,0x100);m.store(gpu.dma+0x40,4,put);assert(rsx_run(s,3));};
 queue();session.guard.disarm();session.flush();session.leave_fifo();assert(!session.surfaces.empty());
 raw[0]=0x11;assert(session.surfaces.empty());R[0x324]=0;queue();assert(raw[0]==0x11);R[0x324]=0x01010101;
 // Retained Range, main-memory alias and real SPU DMA call the observer.
 m.regions.push_back({0x90000000,6,m.regions[1].bytes});
 queue();std::uint8_t bytes[16]{};retained.read(0,bytes,16);assert(near_byte(bytes[0],128));
 queue();assert(near_argb(m.load(0x90100000,4),0x80ff8040));
 queue();NativeSpu::Thread spu{};spu_transfer(s,spu,0xc0100000,bytes,16,false);assert(near_byte(bytes[0],128) && bytes[1]==255);
 queue();bytes[1]=0x22;spu_transfer(s,spu,0xc0100000,bytes,16,true);assert(near_argb(m.load(0xc0100000,4),0x80228040));
 // Physical alias reservations must notice a later GPU write.
 token=m.reservation_token(0x90100000,4);queue();assert(m.reservation_token(0x90100000,4).second!=token.second);
 // Selective API readback retains a separate dirty image on different pages.
 queue();R[0x210]=0x200000;queue();const auto downloads=session.batch->download_bytes;
 assert(near_argb(m.load(0xc0100000,4),0x80ff8040));
 assert(session.batch->download_bytes-downloads==width*height*4);
 assert(session.surfaces.size()==1 && session.surfaces.front().target.offset==0x200000);
 assert(near_argb(m.load(0xc0200000,4),0x80ff8040));R[0x210]=0x100000;
 // Two independent surfaces share a HOST page. API access must unprotect
 // both before handing a pointer to a kernel I/O operation (no SIGSEGV on EFAULT).
 queue();R[0x210]=0x100800;queue();const auto* io=m.access(0xc0100000,4,false);
 int fds[2];assert(pipe(fds)==0);assert(::write(fds[1],io,4)==4);std::uint8_t received[4]{};
 assert(::read(fds[0],received,4)==4);close(fds[0]);close(fds[1]);assert(near_byte(received[0],128));
 session.flush();session.surfaces.clear();R[0x210]=0x100000;
 // Guest backings start on a host page: a surface at local offset 0 owns
 // whole host pages, stays resident and protected, and a CPU read commits it.
 R[0x210]=0;queue();assert(session.surfaces.size()==1 && session.surfaces.front().protectable && !session.guard.spans.empty());
 assert(near_argb(m.load(0xc0000000,4),0x80ff8040));session.flush();session.surfaces.clear();
 // Surface in an allocator edge page: a backing whose end is not a host page
 // multiple cannot protect the partial last page (adjacent heap storage).
 // The FIFO commits it and drops ownership before returning instead.
 {const auto size=m.regions[1].bytes->size()-0x400;m.regions[1].bytes=std::make_shared<GuestBacking>(m.regions[1].bytes->begin(),m.regions[1].bytes->begin()+size);
  R[0x210]=size-width*height*4;queue();assert(session.guard.spans.empty() && session.surfaces.empty());
  assert(near_argb(m.load(0xc0000000+size-width*height*4,4),0x80ff8040));}
 std::puts("PASS resident short FIFO turns, unrelated PPU memory, direct GPU capture, raw read/write, alias, Range, real SPU DMA, reservations, allocator edge fallback");
}
int main(int argc,char** argv){try{
 (void)argc;(void)argv;graphics_fifo_residency();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def native_source():
    source = graphics_source()
    return source[:source.index('int main(int argc,char** argv){try{')] + RESIDENCY


@unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
class ResidencyTests(unittest.TestCase):
    def test_short_fifo_turns_and_pointer_coherence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            # Other graphics probes are also called to retain -Werror coverage.
            source=native_source().replace('(void)argc;(void)argv;graphics_fifo_residency();',
                '(void)argc;(void)argv;graphics_fifo_residency();graphics_differential();graphics_formats();graphics_fallback();graphics_coherence();graphics_sample_edges();graphics_mrt();graphics_fifo();graphics_depth_compare();graphics_texture_layout_reinterpret();graphics_dynamic_constants();graphics_vertex_feedback_fallback();graphics_fragment_feedback_snapshot();graphics_fragment_feedback_resident();graphics_aa_control();graphics_null_texture();graphics_nonfinite_position();')
            (root/'test.cpp').write_text(source)
            build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,ANYPS3_RSX_GPU_FALLBACK='1',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=180)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stdout+run.stderr)
            self.assertNotIn('Validation Warning',run.stdout+run.stderr)
            self.assertIn('PASS resident short FIFO',run.stdout)
