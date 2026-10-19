"""RSX main-memory and feedback regressions with literal packed-byte oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_rsx import GAME_VP
from tests.test_rsx_perf import HARNESS

CPP = r'''
template<class F> static void fault(F f){bool stopped=false;try{f();}catch(const std::runtime_error&){stopped=true;}assert(stopped);}
static void redraw(PpuState& s){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
static void split(PpuState& s,unsigned address,unsigned size){s.memory.map(address,size/2,6,nullptr,0);s.memory.map(address+size/2,size-size/2,6,nullptr,0);}
static void outputs(PpuState& s){
 const unsigned dest[]={0,2,3,4};const rsxdraw::Vec4 colors[]={{1,0,0,1},{0,1,0,.5f},{0,0,1,.25f},{1,1,1,0}};
 unsigned at=0;auto word=[&](unsigned w){s.memory.store(0xc0001000+4*at++,4,(w<<16)|(w>>16));};
 for(unsigned i=0;i<4;++i){word((1u<<24)|(15u<<9)|(dest[i]<<1)|(i==3));word(2|(0xe4u<<9)|(7u<<18));word(0);word(0);
  for(float c:colors[i])word(rsxdraw::float_bits(c));}
}
static void main_draw(){
 PpuState s;setup_draw(s,8,16,32,false);auto& m=s.memory;auto& R=s.rsx.registers;s.rsx.io[1]=0x71000000;
 const unsigned offsets[]={0x100000,0x101000,0x102000,0x103000};
 const unsigned contexts[]={0x194,0x18c,0x1b4,0x1b8},regs[]={0x210,0x218,0x288,0x28c},pitches[]={0x20c,0x21c,0x280,0x284};
 for(unsigned i=0;i<4;++i){split(s,0x71000000+i*0x1000,0x200);R[contexts[i]]=0xfeed0001;R[regs[i]]=offsets[i];R[pitches[i]]=32;}
 split(s,0x71004000,0x200);R[0x198]=0xfeed0001;R[0x214]=0x104000;R[0x220]=0x1f;R[0x370]=0xfff0;
 R[0xa74]=R[0xa70]=1;R[0xa6c]=0x207;
 for(unsigned i=0;i<128;++i)m.store(0x71004000+4*i,4,0xffffff5a);
 outputs(s);const auto token=m.reservation_token(0x71000100),ztoken=m.reservation_token(0x71004100);auto* held=m.access(0x71000000,4,false);
 redraw(s);const unsigned expected[]={0xffff0000,0x8000ff00,0x400000ff,0x00ffffff};
 for(unsigned target=0;target<4;++target)for(unsigned i=0;i<128;++i)assert(m.load(0x71000000+target*0x1000+4*i,4)==expected[target]);
 for(unsigned i=0;i<128;++i)assert(m.load(0x71004000+4*i,4)==0x4000005a);
 assert(m.load(0xc0100000,4)==0 && held==m.access(0x71000000,4,false));
 assert(token!=m.reservation_token(0x71000100) && ztoken!=m.reservation_token(0x71004100));
 // Unknown DMA and IO discontinuity stop before changing the target bytes.
 R[0x194]=0xfeed0002;const auto old=m.load(0x71000000,4);fault([&]{redraw(s);});assert(m.load(0x71000000,4)==old);
 R[0x194]=0xfeed0001;s.rsx.io[2]=0x73000000;fault([&]{rsxdraw::bind_target(s,0xfeed0001,0x1ffff0,32,8,2);});
 std::puts("PASS main-memory MRT/Z across backing boundaries, packed BE outputs, stable pointers, tokens and invalid IO");
}
static void tiled(){
 PpuState s;setup_draw(s,4,2,256,false);auto& R=s.rsx.registers;auto& m=s.memory;s.rsx.io[1]=0x71000000;
 split(s,0x71000000,0x10000);auto& t=s.rsx.tiles[0];t.bound=true;t.location=1;t.offset=0x100000;t.size=0x10000;t.pitch=256;t.bank=1;
 R[0x194]=0xfeed0001;R[0x210]=0x100000;redraw(s);
 // Fixed physical bank-1 tile locations: first two rows, no algorithm oracle.
 for(unsigned i=0;i<4;++i){assert(m.load(0x71004000+i*4,4)==0x80ff8040);assert(m.load(0x71004020+i*4,4)==0x80ff8040);}
 assert(m.load(0x71000000,4)==0 && m.load(0x71004010,4)==0);
 // Main-memory Morton and expanded MSAA storage share the same mapping path.
 t.bound=false;R[0x208]=0x01020248|(3u<<12);R[0x200]=R[0x204]=2<<16;R[0x20c]=0;R[0x210]=0x108000;
 R[0x8c0]=R[0x8c4]=2<<16;redraw(s);
 for(unsigned i=0;i<8;++i)assert(m.load(0x71008000+4*i,4)==0x80ff8040);
 std::puts("PASS location-1 tile literal physical addresses and main-memory swizzle/MSAA");
}
static void split_pixel(){
 PpuState s;setup_draw(s,2,2,8,false);auto& R=s.rsx.registers;auto& m=s.memory;s.rsx.io[1]=0x71000000;
 m.map(0x71000000,3,6,nullptr,0);m.map(0x71000003,13,6,nullptr,0);R[0x194]=0xfeed0001;R[0x210]=0x100000;redraw(s);
 assert(m.load(0x71000000,3)==0x80ff80 && m.load(0x71000003,1)==0x40);
 for(unsigned i=1;i<4;++i)assert(m.load(0x71000000+4*i,4)==0x80ff8040);
 // Pixel split in depth/stencil must commit before fail/early-continue paths.
 PpuState z;setup_draw(z,2,2,8,false);auto& Z=z.rsx.registers;z.rsx.io[1]=0x72000000;
 z.memory.map(0x72000000,3,6,nullptr,0);z.memory.map(0x72000003,13,6,nullptr,0);Z[0x198]=0xfeed0001;Z[0x214]=0x100000;
 Z[0xa70]=Z[0xa74]=1;Z[0xa6c]=0x207;redraw(z);
 assert(z.memory.load(0x72000000,3)==0x400000 && z.memory.load(0x72000003,1)==0);
 std::puts("PASS physical color/depth pixel straddling backing boundaries");
}
static void feedback(){
 PpuState s;setup_draw(s,8,4,256,true);auto& R=s.rsx.registers;auto& m=s.memory;
 auto& tile=s.rsx.tiles[0];tile.size=0x10000;tile.bank=0;
 R[0x1a00]=0x100000;R[0x1a04]=0x1a529;R[0x1a08]=0x303;R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x01010000;R[0x1a18]=0x10001;R[0x1840]=256;
 const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
 for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
 // Every fragment samples the first pre-draw texel. Blending changes it on
 // the first write; re-reading live bytes would produce a gradient.
 m.store(0xc0100000,4,0xff804020);R[0x310]=1;R[0x314]=0x10001;R[0x318]=0x10001;R[0x320]=0x80068006;
 redraw(s);
 assert(m.load(0xc0100000,4)==0xffff8040);
 for(unsigned y=0;y<4;++y)for(unsigned x=0;x<8;++x)if(x||y){
  // First 4 rows x 8 pixels stay in the tile's low-address 32B runs.
  assert(m.load(0xc0100000+y*32+x*4,4)==0xff804020);
 }
 std::puts("PASS pre-draw tiled feedback snapshot survives blending/first-texel mutation");
}
static void gathered_texture(){
 PpuState s;setup_draw(s,8,4,32,false);auto& R=s.rsx.registers;auto& m=s.memory;s.rsx.io[1]=0x71000000;
 m.map(0x71000000,2,6,nullptr,0);m.map(0x71000002,2,6,nullptr,0);m.store(0x71000000,2,0xff12);m.store(0x71000002,2,0x3456);
 R[0x1a00]=0x100000;R[0x1a04]=0x1a52a;R[0x1a08]=0x303;R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x01010000;R[0x1a18]=0x10001;R[0x1840]=4;
 const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
 for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
 redraw(s);for(unsigned i=0;i<32;++i)assert(m.load(0xc0100000+i*4,4)==0xff123456);
 assert(!m.split_reads);fault([&]{m.access(0x71000000,4,false);});
 std::puts("PASS texture split-read through original decoder with draw-scoped snapshot lifetime");
}
static void blit(){
 PpuState s;setup_draw(s,2,2,8,false);auto& R=s.rsx.registers;auto& m=s.memory;s.rsx.io[1]=0x71000000;
 m.map(0x71000000,3,6,nullptr,0);m.map(0x71000003,13,6,nullptr,0);
 R[0x2184]=0xfeed0000;R[0x2188]=0xfeed0001;R[0x230c]=0x1000;R[0x2310]=0x100000;
 R[0x2314]=R[0x2318]=8;R[0x231c]=8;R[0x2320]=2;R[0x2324]=0x101;
 m.store(0xc0001000,8,UINT64_C(0x1122334455667788));m.store(0xc0001008,8,UINT64_C(0x99aabbccddeeff00));
 rsxblit::copy(s,0);assert(m.load(0x71000000,3)==0x112233 && m.load(0x71000003,5)==UINT64_C(0x4455667788));assert(m.load(0x71000008,8)==UINT64_C(0x99aabbccddeeff00));
 // NV308A can write a single packed word across two region backings.
 R[0x6188]=0xfeed0001;R[0x6300]=10;R[0x6304]=8u<<16;R[0x630c]=0x100000;
 R[0xa19c]=0x313371c3;R[0xa2f8]=1;R[0xa2fc]=3;R[0xa300]=4;R[0xa304]=0;R[0xa308]=R[0xa30c]=0x10002;
 rsxblit::cpu(s,0xa400,0x12345678);assert(m.load(0x71000000,3)==0x123456 && m.load(0x71000003,1)==0x78);
 std::puts("PASS NV0039 rows and NV308A pixel split writes");
}
static void clear(){
 PpuState s;setup_draw(s,8,16,32,false);auto& R=s.rsx.registers;auto& m=s.memory;s.rsx.io[1]=0x71000000;
 split(s,0x71000000,0x200);R[0x194]=0xfeed0001;R[0x210]=0x100000;R[0x1d90]=0x12345678;
 R[0x8c0]=8<<16;R[0x8c4]=16<<16;
 try{s.rsx.renderer=std::make_unique<NativeVulkan>();}catch(const std::runtime_error& e){std::fprintf(stderr,"Vulkan unavailable: %s\n",e.what());std::exit(77);}
 const auto token=m.reservation_token(0x71000100);rsx_clear_surface(s,0xf0);
 for(unsigned i=0;i<128;++i)assert(m.load(0x71000000+4*i,4)==0x12345678);
 assert(m.reservation_token(0x71000100)!=token && s.rsx.renderer->clears==1);
 m.regions.back().flags=4;const auto saved=m.reservation_token(0x71000000);fault([&]{rsx_clear_surface(s,0xf0);});assert(saved==m.reservation_token(0x71000000));
 std::puts("PASS actual Vulkan clear across guest backing boundaries and prevalidation");
}
int main(int argc,char** argv){assert(argc==2);std::string mode=argv[1];
 if(mode=="draw")main_draw();else if(mode=="tiled")tiled();else if(mode=="pixel")split_pixel();else if(mode=="feedback")feedback();else if(mode=="texture")gathered_texture();else if(mode=="blit")blit();else if(mode=="clear")clear();else assert(false);}
'''

@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/vulkan/vulkan.h').exists(), 'Requires C++ and Vulkan headers')
class RsxMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='rsxmem-')
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        source = compile_program(ElfImage.parse(program(BLR)),process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')
        harness = HARNESS.split('static void observe(')[0].replace('GAME_VP_WORDS',','.join(f'{word}u' for word in GAME_VP))
        (root/'test.cpp').write_text(source+'\n#include <cassert>\n'+harness+CPP)
        cls.binary = root/'test'
        build = subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-DANYPS3_VULKAN',str(root/'test.cpp'),'-pthread','-lvulkan','-o',str(cls.binary)],capture_output=True,text=True,timeout=120)
        if build.returncode:
            raise AssertionError(build.stderr)

    def run_mode(self,mode):
        run = subprocess.run([str(self.binary),mode],capture_output=True,text=True,timeout=30)
        if run.returncode == 77 and os.environ.get('ANYPS3_REQUIRE_VULKAN') != '1':
            self.skipTest(run.stderr)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertIn('PASS',run.stdout)

    def test_main_memory_mrt_depth(self): self.run_mode('draw')
    def test_main_memory_tiled_swizzled_msaa(self): self.run_mode('tiled')
    def test_pixel_straddles_backings(self): self.run_mode('pixel')
    def test_tiled_feedback_snapshot(self): self.run_mode('feedback')
    def test_split_texture_read_lifetime(self): self.run_mode('texture')
    def test_split_blitter(self): self.run_mode('blit')
    def test_split_clear_with_vulkan(self): self.run_mode('clear')
