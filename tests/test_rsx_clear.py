"""Packed RSX clear semantics with actual Vulkan submission and golden bytes."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program


CPP = r'''
#include <cassert>
template<class F> static void fault(F action){
 bool failed=false;try{action();}catch(const std::runtime_error&){failed=true;}assert(failed);
}
static void setup(PpuState& s){
 auto& g=s.rsx;g.local_size=0x80000;s.memory.map(0xc0000000,g.local_size,6,nullptr,0,true);
 std::fill_n(s.memory.access(0xc0000000,g.local_size,true),g.local_size,0xa5);
 g.registers={{0x194,0xfeed0000},{0x18c,0xfeed0000},{0x1b4,0xfeed0000},{0x1b8,0xfeed0000},{0x198,0xfeed0000},
 {0x200,4<<16},{0x204,2<<16},{0x208,0x148},{0x220,1},
 {0x210,0x100},{0x218,0x2000},{0x288,0x3000},{0x28c,0x4000},{0x214,0x5000},
 {0x20c,32},{0x21c,40},{0x280,48},{0x284,56},{0x22c,32},
 {0x8c0,4<<16},{0x8c4,2<<16},{0x32c,255},{0x1d90,0x12345678},{0x1d8c,0x89abcdef}};
}
int main(int argc,char** argv){
 assert(argc==2);PpuState s;setup(s);auto& g=s.rsx;auto& m=s.memory;
 try{g.renderer=std::make_unique<NativeVulkan>();}
 catch(const std::runtime_error& e){std::fprintf(stderr,"Vulkan unavailable: %s\n",e.what());return 77;}
 const std::string test=argv[1];
 if(test=="formats"){
  // Literal BE bytes: 16-bit clear consumes G:B, rather than quantizing R:G:B.
  const unsigned formats[]={1,2,3,4,5,8,9,10,14,15,16};
  const unsigned bytes[]={2,2,2,4,4,4,1,2,4,4,4};
  const unsigned expected[]={0x5678,0xd678,0x5678,0x12345678,0x12345678,0x12345678,0x78,0x5678,0x12345678,0x12345678,0x12345678};
  for(unsigned i=0;i<11;++i){
   std::fill_n(m.access(0xc0000000,g.local_size,true),g.local_size,0xa5);
   g.registers[0x208]=0x140|formats[i];rsx_clear_surface(s,0xf0);
   for(unsigned y=0;y<2;++y)for(unsigned x=0;x<4;++x)assert(m.load(0xc0000100+y*32+x*bytes[i],bytes[i])==expected[i]);
   assert(m.load(0xc00000ff,1)==0xa5 && m.load(0xc0000100+4*bytes[i],1)==0xa5);
  }
  // Floating color CLEAR_SURFACE is a documented no-op; depth still executes.
  const auto submits=g.renderer->clears;
  for(unsigned f:{11u,12u,13u}){g.registers[0x208]=0x140|f;rsx_clear_surface(s,0xf0);assert(g.renderer->clears==submits);}
  rsx_clear_surface(s,0xf3);assert(m.load(0xc0005000,4)==0x89abcdef && g.renderer->clears==submits+1);
 }else if(test=="masks"){
  // CLEAR_SURFACE has its own masks, independent of color/depth draw masks.
  g.registers[0x324]=0;g.registers[0x370]=0;g.registers[0xa70]=0;
  rsx_clear_surface(s,0x10);assert(m.load(0xc0000100,4)==0xa534a5a5);
  rsx_clear_surface(s,0x20);assert(m.load(0xc0000100,4)==0xa53456a5);
  rsx_clear_surface(s,0x40);assert(m.load(0xc0000100,4)==0xa5345678);
  rsx_clear_surface(s,0x80);assert(m.load(0xc0000100,4)==0x12345678);
  g.registers[0x208]=0x150; // ABGR: raw clear word and mask bits stay in register byte order.
  rsx_clear_surface(s,0x10);assert(m.load(0xc0000100,4)==0x12345678);
  g.registers[0x208]=0x143;g.registers[0x1d90]=0x00001234;
  m.store(0xc0000100,2,0xffff);rsx_clear_surface(s,0x10);assert(m.load(0xc0000100,2)==0x17ff);
  rsx_clear_surface(s,0x20);assert(m.load(0xc0000100,2)==0x123f);
  rsx_clear_surface(s,0x40);assert(m.load(0xc0000100,2)==0x1234);
  g.registers[0x208]=0x148;g.registers[0x32c]=0x0f;g.registers[0x348]=0;
  rsx_clear_surface(s,2);assert(m.load(0xc0005000,4)==0xa5a5a5af);
  rsx_clear_surface(s,1);assert(m.load(0xc0005000,4)==0x89abcdaf);
  g.registers[0x32c]=0;g.registers[0x348]=255;const auto before=g.renderer->clears;
  rsx_clear_surface(s,2);assert(g.renderer->clears==before && m.load(0xc0005000,4)==0x89abcdaf);
  g.registers[0x208]=0x128;rsx_clear_surface(s,1);assert(m.load(0xc0005000,2)==0xcdef);
  const auto count=g.renderer->clears;rsx_clear_surface(s,2);assert(g.renderer->clears==count);
  // Float Z16 shares packed E4M12 storage, F24 stores E8M16 with S8.
  g.registers[0x3b0]=0x101000;g.registers[0x1d8c]=0xabcd1234;rsx_clear_surface(s,1);assert(m.load(0xc0005000,2)==0x1234);
  m.store(0xc0005000,4,0xaabbcc5a);g.registers[0x208]=0x148;g.registers[0x1d8c]=0x7f0000ff;rsx_clear_surface(s,1);assert(m.load(0xc0005000,4)==0x7f00005a);
  g.registers[0x1d8c]=0xff000000;fault([&]{rsx_clear_surface(s,1);});assert(m.load(0xc0005000,4)==0x7f00005a);
 }else if(test=="mrt"){
  for(unsigned target:{1u,2u,0x13u,0x17u,0x1fu}){
   std::fill_n(m.access(0xc0000000,g.local_size,true),g.local_size,0xa5);g.registers[0x220]=target;
   rsx_clear_surface(s,0xf0);
   assert(m.load(0xc0000100,4)==(target==2?0xa5a5a5a5:0x12345678));
   assert(m.load(0xc0002000,4)==(target==1?0xa5a5a5a5:0x12345678));
   assert(m.load(0xc0003000,4)==(target>=0x17?0x12345678:0xa5a5a5a5));
   assert(m.load(0xc0004000,4)==(target==0x1f?0x12345678:0xa5a5a5a5));
   if(target==0x1f){assert(m.load(0xc0003020,4)==0xa5a5a5a5);assert(m.load(0xc0003030,4)==0x12345678);}
  }
  g.registers[0x28c]=g.local_size-4;const auto before=g.renderer->clears;
  const std::vector<std::uint8_t> snapshot(m.access(0xc0000000,g.local_size,false),m.access(0xc0000000,g.local_size,false)+g.local_size);
  fault([&]{rsx_clear_surface(s,0xf3);});assert(g.renderer->clears==before);
  assert(std::equal(snapshot.begin(),snapshot.end(),m.access(0xc0000000,g.local_size,false)));
  g.registers[0x28c]=0x100;fault([&]{rsx_clear_surface(s,0xf0);});assert(g.renderer->clears==before);
 }else if(test=="layout"){
  // Clip origin is not an extra offset in the reference framebuffer layout.
  g.registers[0x200]=(4<<16)|7;g.registers[0x204]=(2<<16)|9;
  g.registers[0x2b8]=(1<<16)|1;g.registers[0x8c0]=(2<<16)|1;g.registers[0x8c4]=(1<<16)|1;
  rsx_clear_surface(s,0xf0);assert(m.load(0xc0000148,4)==0x12345678);assert(m.load(0xc0000144,4)==0xa5a5a5a5);
  for(unsigned aa:{0u,3u,4u,5u}){
   std::fill_n(m.access(0xc0000000,g.local_size,true),g.local_size,0xa5);
   g.registers[0x208]=0x148|(aa<<12);g.registers[0x2b8]=0;
   const unsigned nx=aa?2:1,ny=aa>=4?2:1;
   rsx_clear_surface(s,0xf3);
   for(unsigned y=0;y<4;++y)for(unsigned x=0;x<8;++x){
    const bool hit=x>=nx && x<3*nx && y>=ny && y<2*ny;
    assert(m.load(0xc0000100+y*32+x*4,4)==(hit?0x12345678:0xa5a5a5a5));
    assert(m.load(0xc0005000+y*32+x*4,4)==(hit?0x89abcdef:0xa5a5a5a5));
   }
  }
  std::fill_n(m.access(0xc0000000,g.local_size,true),g.local_size,0xa5);
  g.registers[0x208]=0x01020248;g.registers[0x200]=4<<16;g.registers[0x204]=2<<16;
  rsx_clear_surface(s,0xf0);
  // Morton 4x2: rows [0,1,4,5], [2,3,6,7], scissor x=[1,3), y=1.
  for(unsigned i=0;i<8;++i)assert(m.load(0xc0000100+i*4,4)==((i==3 || i==6)?0x12345678:0xa5a5a5a5));
  
  // Backing 4x4, clip only 2x4: log2 storage dimensions still determine Morton.
  std::fill_n(m.access(0xc0000100,64,true),64,0xa5);
  g.registers[0x208]=0x02020248;g.registers[0x200]=2<<16;g.registers[0x204]=4<<16;
  g.registers[0x8c0]=2<<16;g.registers[0x8c4]=4<<16;rsx_clear_surface(s,0xf0);
  for(unsigned i=0;i<16;++i)assert(m.load(0xc0000100+i*4,4)==((i<4 || (i>=8&&i<12))?0x12345678:0xa5a5a5a5));
  g.registers[0x200]=5<<16;fault([&]{rsx_clear_surface(s,0xf0);});

 }else if(test=="mainmem"){
  // Color surface in main memory (DMA 0xfeed0001) through IO page 0 -> host 0x70000000.
  m.map(0x70000000,0x200000,6,nullptr,0);std::fill_n(m.access(0x70000000,0x200000,true),0x200000,0x5a);
  g.io.fill(UINT32_MAX);g.io[0]=0x70000000;g.io[1]=0x70100000;
  g.registers[0x194]=0xfeed0001;rsx_clear_surface(s,0xf0);
  for(unsigned y=0;y<2;++y)for(unsigned x=0;x<4;++x)assert(m.load(0x70000100+y*32+x*4,4)==0x12345678);
  assert(m.load(0x700000ff,1)==0x5a && m.load(0x70000110,1)==0x5a && m.load(0xc0000100,4)==0xa5a5a5a5);
  // A surface crossing into a non-adjacent IO page must stop before writing anything.
  g.io[1]=0x70180000;g.registers[0x210]=0xfffe0;std::fill_n(m.access(0x70000000,0x200000,true),0x200000,0x5a);
  fault([&]{rsx_clear_surface(s,0xf0);});assert(m.load(0x700fffe0,4)==0x5a5a5a5a);
  g.registers[0x210]=0x100;g.io[1]=UINT32_MAX;g.io[0]=UINT32_MAX;fault([&]{rsx_clear_surface(s,0xf0);});   // unmapped IO
  g.registers[0x194]=0xfeed0002;fault([&]{rsx_clear_surface(s,0xf0);});                                 // unknown context
 }else if(test=="zeroclip"){
  // Zero surface clip width or height: invalid framebuffer, clear is a no-op (RPCS3).
  const auto submits=g.renderer->clears;
  g.registers[0x200]=0;rsx_clear_surface(s,0xf3);
  g.registers[0x200]=4<<16;g.registers[0x204]=0;rsx_clear_surface(s,0xf3);
  assert(g.renderer->clears==submits && m.load(0xc0000100,4)==0xa5a5a5a5 && m.load(0xc0005000,4)==0xa5a5a5a5);
  g.registers[0x204]=5000u<<16;fault([&]{rsx_clear_surface(s,0xf3);});   // > 4096 still stops
 }else if(test=="game291"){
  // Actual 291 state: R565 / Z16 / SWIZZLE / 1x / log2=7x7.
  g.registers[0x208]=0x07070223;g.registers[0x200]=128<<16;g.registers[0x204]=128<<16;
  g.registers[0x214]=0x10000;g.registers[0x22c]=0;g.registers[0x8c0]=128<<16;g.registers[0x8c4]=128<<16;
  g.registers[0x32c]=0;g.registers[0x1d8c]=0x89abcdef;
  rsx_clear_surface(s,3);
  for(unsigned i=0;i<16384;++i)assert(m.load(0xc0010000+i*2,2)==0xcdef);
  assert(m.load(0xc000ffff,1)==0xa5 && m.load(0xc0018000,1)==0xa5);
  g.registers[0x8c0]=(2<<16)|1;g.registers[0x8c4]=(2<<16)|1;g.registers[0x1d8c]=0xabcd1234;
  rsx_clear_surface(s,3);
  // Morton indices for (1,1),(2,1),(1,2),(2,2): 3,6,9,12.
  for(unsigned i=0;i<16384;++i)assert(m.load(0xc0010000+i*2,2)==((i==3||i==6||i==9||i==12)?0x1234:0xcdef));
  g.registers[0x1a00]=0x10000;g.registers[0x1a04]=0x10029|(0x92<<8);g.registers[0x1a08]=0x303;
  g.registers[0x1a0c]=0x80000000;g.registers[0x1a10]=0xaae4;g.registers[0x1a14]=0x01010000;g.registers[0x1a18]=(128<<16)|128;
  auto texture=rsxdraw::decode_texture(s,rsxdraw::Reg{g},0);
  for(unsigned y=0;y<128;++y)for(unsigned x=0;x<128;++x){
   const float value=(x>=1&&x<3&&y>=1&&y<3)?4660/65535.f:52719/65535.f;
   assert((rsxdraw::texel(texture,texture.levels[0],x,y)==rsxdraw::Vec4{value,value,value,value}));
  }
 }else if(test=="tiles"){

  g.tiles[0].bound=true;g.tiles[0].offset=0x10000;g.tiles[0].size=0x10000;g.tiles[0].pitch=256;g.tiles[0].bank=1;g.tiles[0].compression=7;
  g.registers[0x210]=0x10000;g.registers[0x20c]=256;g.registers[0x208]=0x5148;
  g.registers[0x8c0]=(1<<16)|1;g.registers[0x8c4]=1<<16;
  rsx_clear_surface(s,0xf0);
  // Independently fixed tiled golden offsets, 4x samples at x=2,3/y=0,1.
  for(unsigned at:{0x14008u,0x1400cu,0x14028u,0x1402cu})assert(m.load(0xc0000000+at,4)==0x12345678);
  unsigned changed=0;for(unsigned i=0;i<g.local_size;++i)if(m.load(0xc0000000+i,1)!=0xa5)++changed;assert(changed==16);
  g.registers[0x208]=0x149; // B8 must use byte address within a permuted 32B run.
  rsx_clear_surface(s,0xf0);assert(m.load(0xc0014001,1)==0x78);
  const auto before=g.renderer->clears;g.tiles[0].compression=6;
  fault([&]{rsx_clear_surface(s,0xf0);});assert(g.renderer->clears==before);
  g.tiles[0].compression=7;g.registers[0x1d98]=0x00010000;
  fault([&]{rsx_clear_surface(s,0xf0);});g.registers.erase(0x1d98);
  fault([&]{rsx_clear_surface(s,4);});
 }else assert(false);
 std::printf("PASS RSX clear %s Vulkan submissions=%llu\n",test.c_str(),static_cast<unsigned long long>(g.renderer->clears));
}
'''


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/vulkan/vulkan.h').exists(),
                     'Requires C++ and Vulkan development headers')
class RsxClearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='anyps3-rsxclear-')
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        cls.binary = root / 'clear'
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        (root / 'clear.cpp').write_text(source + CPP)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                '-DANYPS3_VULKAN', str(root / 'clear.cpp'), '-lvulkan', '-o', str(cls.binary)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            raise AssertionError(build.stderr)

    def run_case(self, case):
        env = os.environ.copy()
        lavapipe = Path('/usr/share/vulkan/icd.d/lvp_icd.json')
        if lavapipe.exists() and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
            env['VK_DRIVER_FILES'] = str(lavapipe)
        validation = Path('/usr/share/vulkan/explicit_layer.d/VkLayer_khronos_validation.json')
        if validation.exists():
            env['VK_INSTANCE_LAYERS'] = 'VK_LAYER_KHRONOS_validation'
            env['VK_LAYER_VALIDATE_SYNC'] = '1'
        run = subprocess.run([str(self.binary), case], capture_output=True, text=True, env=env, timeout=60)
        if run.returncode == 77 and env.get('ANYPS3_REQUIRE_VULKAN') != '1':
            self.skipTest(run.stderr)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn('Validation Error', run.stdout + run.stderr)
        self.assertNotIn('Validation Warning', run.stdout + run.stderr)
        self.assertIn('native Vulkan device=', run.stderr)
        self.assertIn('PASS RSX clear ' + case, run.stdout)

    def test_formats(self):
        self.run_case('formats')

    def test_masks_and_depth(self):
        self.run_case('masks')

    def test_mrt_and_atomic_validation(self):
        self.run_case('mrt')

    def test_msaa_window_origin_and_swizzle(self):
        self.run_case('layout')

    def test_tiled_golden_addresses_and_errors(self):
        self.run_case('tiles')

    def test_zero_clip_is_noop(self):
        self.run_case('zeroclip')

    def test_main_memory_surface(self):
        self.run_case('mainmem')

    def test_game_291_swizzled_z16(self):
        self.run_case('game291')
