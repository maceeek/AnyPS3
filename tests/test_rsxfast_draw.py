"""RSXFAST: per-draw CPU work removal keeps exact RSX semantics (native oracles, real Vulkan).

Oracles are independent of the optimized paths: guest bytes produced by the established
synchronous readback/commit path, or bytes written directly by the test itself.
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
static std::vector<std::uint8_t> local_bytes(PpuState& s,std::uint32_t offset,unsigned size){
 std::vector<std::uint8_t> out(size);GuestMemory::ObserverPause pause(s.memory);
 for(unsigned i=0;i<size;++i){out[i]=std::uint8_t(s.memory.load(0xc0000000u+offset+i,1));}
 return out;
}
static void fill_image(NativeVulkan& vk,rsxgraphics::Session& session,rsxgraphics::Surface& surface,unsigned seed){
 std::vector<std::uint8_t> pixels(std::size_t(surface.width)*surface.height*surface.samples*4);
 for(std::size_t i=0;i<pixels.size();++i)pixels[i]=std::uint8_t((i*37+seed*11+5)&255);
 vk.graphics_transfer(*session.batch,surface.image,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());session.dirty_surface(surface);
}
// A: 8x8 ARGB8 at 0x10000 (pitch 64). B: 4x16 ARGB8 at 0x10010 (pitch 64): it covers
// columns 4..7 of A's rows and 8 more rows below A. Background bytes are a test pattern.
static void alias_case_(bool merge,bool clear_new,std::vector<std::uint8_t>& result,std::uint64_t& merges,std::uint64_t& flushes){
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 for(unsigned i=0;i<64*24;i+=4)s.memory.store(0xc0010000u+i,4,0xa0000000u+i);
 if(!merge)setenv("ANYPS3_RSX_GPU_ALIAS_MERGE","0",1);else unsetenv("ANYPS3_RSX_GPU_ALIAS_MERGE");
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto a=rsxdraw::bind_target(s,0xfeed0000,0x10000,64,8,8);fill_image(vk,session,session.bind(a,8,8,1,8,false),1);
 const auto merges_before=RsxPerfCounters::values[RsxPerfCounters::alias_merges].load(),flushes_before=RsxPerfCounters::values[RsxPerfCounters::alias_flushes].load();
 auto b=rsxdraw::bind_target(s,0xfeed0000,0x10010,64,4,16);auto& second=session.bind(b,4,16,1,8,false);
 merges=RsxPerfCounters::values[RsxPerfCounters::alias_merges].load()-merges_before;flushes=RsxPerfCounters::values[RsxPerfCounters::alias_flushes].load()-flushes_before;
 if(clear_new){VkClearColorValue value{};for(auto& v:value.float32)v=1.f;vk.graphics_clear_image(*session.batch,second.image,value,0,0,VK_IMAGE_ASPECT_COLOR_BIT);session.dirty_surface(second);}
 session.flush();result=local_bytes(s,0x10000,64*24);
 s.rsx.graphics_session=nullptr;unsetenv("ANYPS3_RSX_GPU_ALIAS_MERGE");
}
static void alias_merge_(){
 std::vector<std::uint8_t> reference,merged,cleared,cleared_reference;std::uint64_t m0,f0,m1,f1,m2,f2,m3,f3;
 alias_case_(false,false,reference,m0,f0);alias_case_(true,false,merged,m1,f1);
 assert(m0==0 && f0==1);assert(m1==1 && f1==0);
 // The device merge commits exactly the bytes of the synchronous readback path.
 assert(merged==reference);
 alias_case_(false,true,cleared_reference,m2,f2);alias_case_(true,true,cleared,m3,f3);
 assert(m3==1 && f3==0 && cleared==cleared_reference);
 // Independent check of the cleared result: B's footprint is 0xff, A's remainder
 // keeps A's committed bytes, the rest keeps the background pattern.
 for(unsigned row=0;row<24;++row)for(unsigned x=0;x<64;++x){const unsigned at=row*64+x;
  const bool in_b=row<16 && x>=16 && x<32,in_a=row<8 && x<32;
  if(in_b)assert(cleared[at]==0xff);
  else if(in_a)assert(cleared[at]==reference[at]);
  else{const unsigned word=at&~3u,value=0xa0000000u+word;assert(cleared[at]==std::uint8_t(value>>(24-8*(at&3))));}}
 std::puts("PASS device alias merge equals synchronous readback bytes and keeps the old owner's remainder");
}
// A surface fully covered by a discarding (full clear) bind is never read back.
static void discard_cover_(){
 PpuState s;setup_draw(s,8,8,64,false);s.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*s.rsx.renderer;
 rsxgraphics::Session session(vk,s);session.observe();s.rsx.graphics_session=&session;
 auto a=rsxdraw::bind_target(s,0xfeed0000,0x10010,64,4,4);fill_image(vk,session,session.bind(a,4,4,1,8,false),3);
 const auto flushes=RsxPerfCounters::values[RsxPerfCounters::readback_flushes].load();
 auto b=rsxdraw::bind_target(s,0xfeed0000,0x10000,64,8,8);auto& second=session.bind(b,8,8,1,8,false,false,false,true);
 assert(RsxPerfCounters::values[RsxPerfCounters::readback_flushes].load()==flushes);
 VkClearColorValue value{};for(auto& v:value.float32)v=1.f;vk.graphics_clear_image(*session.batch,second.image,value,0,0,VK_IMAGE_ASPECT_COLOR_BIT);session.dirty_surface(second);
 session.flush();const auto bytes=local_bytes(s,0x10000,64*8);
 for(unsigned row=0;row<8;++row)for(unsigned x=0;x<32;++x)assert(bytes[row*64+x]==0xff);
 s.rsx.graphics_session=nullptr;std::puts("PASS discarding bind over a covered dirty surface skips its readback");
}
// Fragment program bytes rewritten at the same address are used by the next load.
static void fragment_cache_(){
 PpuState s;setup_draw(s,8,8,32,false);const rsxdraw::Reg r{s.rsx};
 const auto first=rsxdraw::load_fragment_program(s,r);const auto hits=RsxPerfCounters::values[RsxPerfCounters::fp_hits].load();
 const auto again=rsxdraw::load_fragment_program(s,r);assert(RsxPerfCounters::values[RsxPerfCounters::fp_hits].load()==hits+1);
 assert(again.size()==first.size() && again[0].d==first[0].d && again[0].constant==first[0].constant);
 fpword(s,5,rsxdraw::float_bits(.125f));const auto changed=rsxdraw::load_fragment_program(s,r);
 assert(changed[0].constant[1]==.125f && first[0].constant[1]==.5f);
 s.memory.regions[1].flags=0;s.memory.mapping_changed();bool stopped=false;
 try{(void)rsxdraw::load_fragment_program(s,r);}catch(const std::runtime_error&){stopped=true;}assert(stopped);
 std::puts("PASS fragment program cache follows rewritten bytes and keeps the unreadable-program stop");
}
// GET may lag behind state methods only: it is published before an action and on return.
static void fifo_get_(){
 PpuState s;setup_draw(s,8,8,32,false);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x40000000,0x100000,6,nullptr,0);g.io[1]=0x40000000;g.dma=0x70004000;g.fifo_enabled=true;
 std::vector<std::uint32_t> words;auto method=[&](unsigned reg,std::vector<std::uint32_t> values){words.push_back((unsigned(values.size())<<18)|reg);for(auto v:values)words.push_back(v);};
 g.reports=0x70008000;method(0x1d6c,{0x10});method(0x1a4,{0x66606660});method(0x1840,{1,2,3,4,5,6,7,8});method(0x1d70,{0x11223344});method(0x1844,{9});
 for(unsigned i=0;i<words.size();++i)m.store(0x40000000+4*i,4,words[i]);
 m.store(g.dma+0x44,4,0x100000);m.store(g.dma+0x40,4,0x100000+unsigned(words.size())*4);
 assert(rsx_run(s,100) && m.load(g.dma+0x44,4)==0x100000+words.size()*4);
 assert(g.registers.at(0x1840+28)==8 && g.registers.at(0x1844)==9);
 rsx_graphics_flush(s,"rsxfast_test");{GuestMemory::ObserverPause pause(m);assert(m.load(0x70008010,4)==0x11443322);}s.rsx.graphics_owner.reset();
 std::puts("PASS FIFO local cursor publishes GET at return with bulk packet values");
}
int main(){try{alias_merge_();discard_cover_();fragment_cache_();fifo_get_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxFastDrawTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_rsxfast_alias_merge_fragment_cache_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_FRAME_DIR=str(root), VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('Validation Error', result.stdout + result.stderr)
            self.assertNotIn('Validation Warning', result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 4, result.stdout)


if __name__ == '__main__':
    unittest.main()
