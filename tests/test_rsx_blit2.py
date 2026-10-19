"""Native RSX transfers: literal BE byte oracles, no game or algorithm oracle."""
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
 auto& g=s.rsx;g.local_size=0x80000;s.memory.map(0xc0000000,g.local_size,6,nullptr,0);
 s.memory.map(0x70000000,0x300000,6,nullptr,0);g.io[0]=0x70000000;g.io[1]=0x70100000;g.io[2]=0x70200000;
 s.memory.map(0x10000,0x2000,6,nullptr,0);g.dma=0x10000;g.fifo_enabled=true;
 auto& R=g.registers;
 R={{0xc184,0xfeed0001},{0xc198,0x313371c3},{0xc2fc,1},{0xc300,3},{0xc304,3},
 {0xc308,0},{0xc310,0},{0xc30c,0x10002},{0xc314,0x10002},{0xc318,0x100000},{0xc31c,0x100000},
 {0xc400,0x10002},{0xc404,0x20008},{0xc408,0x1000},
 {0x6188,0xfeed0000},{0x6300,10},{0x6304,8u<<16},{0x630c,0x1000},
 {0xa19c,0x313371c3},{0xa2f8,1},{0xa2fc,3},{0xa300,4},{0xa304,0},{0xa308,0x10002},{0xa30c,0x10002}};
}
static void copy_test(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 R[0x2184]=R[0x2188]=0xfeed0001;R[0x230c]=0x2000;R[0x2310]=0x2001;
 R[0x2314]=R[0x2318]=8;R[0x231c]=3;R[0x2320]=2;R[0x2324]=0x303;
 m.store(0x70002000,8,UINT64_C(0x0102030405060708));m.store(0x70002008,8,UINT64_C(0x1112131415161718));
 rsxblit::copy(s,0);assert(m.load(0x70002000,8)==UINT64_C(0x0101030404060707));
 assert(m.load(0x70002008,8)==UINT64_C(0x1111131414161717));
 R[0x230c]=0x2008;R[0x2310]=0x3000;R[0x2314]=0xfffffff8;R[0x2318]=8;R[0x2324]=0x103;
 rsxblit::copy(s,0);assert(m.load(0x70003000,4)==0x11141700);assert(m.load(0x70003008,4)==0x01040700);
 const auto token=m.reservation_token(0x70003000);const auto old=m.load(0x70003000,8);
 fault([&]{rsxblit::copy(s,1);});assert(m.load(0x70003000,8)==old && m.reservation_token(0x70003000)==token);
 for(unsigned fmt:{0x100u,0x105u,0x100101u}){R[0x2324]=fmt;fault([&]{rsxblit::copy(s,0);});}
 R[0x2324]=0x303;R[0x2310]=0x2ffff9;R[0x2320]=2;R[0x2318]=8;
 const auto bad_token=m.reservation_token(0x702fff80);fault([&]{rsxblit::copy(s,0);});
 assert(m.load(0x702ffff9,7)==0 && m.reservation_token(0x702fff80)==bad_token);
 // Raw M2MF must NOT detile. Two bytes at physical offsets remain exactly two bytes.
 auto& t=s.rsx.tiles[0];t.bound=true;t.offset=0x10000;t.size=0x10000;t.pitch=256;t.bank=1;
 m.store(0xc0010000,4,0x12345678);m.store(0xc0014000,4,0xabcdef01);
 R[0x2184]=0xfeed0000;R[0x230c]=0x10000;R[0x2310]=0x3000;R[0x231c]=4;R[0x2320]=1;R[0x2324]=0x101;
 rsxblit::copy(s,0);assert(m.load(0x70003000,4)==0x12345678);
 std::puts("PASS stride3, negative pitches, overlap, raw tiled M2MF and atomic faults");
}
static void formats(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 // Formats and byte order are distinct from target/draw format numbering.
 struct Case{unsigned format,pitch;std::uint64_t input,output;};
 const Case cases[]={
 {1,4,0x7c0003e0,UINT64_C(0x00ff00000000ff00)},
 {2,4,0x7c0003e0,UINT64_C(0xffff0000ff00ff00)},
 {3,8,UINT64_C(0x1122334455667788),UINT64_C(0x1122334455667788)},
 {4,8,UINT64_C(0x1122334455667788),UINT64_C(0xff223344ff667788)},
 {7,4,0xf80007e0,UINT64_C(0xffff0000ff00ff00)},
 {8,2,0x2080,UINT64_C(0xff202020ff808080)},
 {9,4,0x402080c0,UINT64_C(0x4020202080c0c0c0)},
 {12,8,UINT64_C(0x1122334455667788),UINT64_C(0x1144332255887766)},
 {13,8,UINT64_C(0x1122334455667788),UINT64_C(0xff443322ff887766)}};
 for(const auto& c:cases){R[0xc300]=c.format;R[0xc404]=0x20000|c.pitch;m.store(0x70001000,c.pitch,c.input);
  rsxblit::scaled(s,0);assert(m.load(0xc0001000,8)==c.output);}
 // Truncate maps mid tones through the field boundaries (no rounding).
 R[0xc300]=3;R[0xc404]=0x20008;R[0x6300]=4;R[0x6304]=4u<<16;
 m.store(0x70001000,8,UINT64_C(0xff8c449cfffcfcfc));rsxblit::scaled(s,0);
 assert(m.load(0xc0001000,4)==0x8a33ffff);
 // Full-precision copy needs no dither/reduction; both defined conversion modes preserve bytes.
 R[0x6300]=10;R[0x6304]=8u<<16;
 for(unsigned conversion:{0u,1u,2u}){R[0xc2fc]=conversion;rsxblit::scaled(s,0);
  assert(m.load(0xc0001000,8)==UINT64_C(0xff8c449cfffcfcfc));}
 R[0x6300]=4;R[0x6304]=4u<<16;const auto before=m.load(0xc0001000,8);
 for(unsigned conversion:{0u,2u,99u}){R[0xc2fc]=conversion;fault([&]{rsxblit::scaled(s,0);});assert(m.load(0xc0001000,8)==before);}
 R[0xc2fc]=0;R[0xc300]=7;R[0xc404]=0x01020004;
 fault([&]{rsxblit::scaled(s,8);});assert(m.load(0xc0001000,8)==before);
 R[0xc404]=0x20004;rsxblit::scaled(s,0); // exact RGB565 copy introduces no reduction
 R[0xc2fc]=1;
 for(unsigned format:{0u,5u,6u,10u,11u,14u}){R[0xc300]=format;fault([&]{rsxblit::scaled(s,0);});}
 std::puts("PASS nine source formats, literal BE bytes, truncate and full-precision conversion");
}
static void tiles(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 auto& t=s.rsx.tiles[0];t.bound=true;t.offset=0x10000;t.size=0x10000;t.pitch=256;t.bank=1;
 // Literal physical golden addresses: (x0,y0),(x1,y0),(x0,y1),(x1,y1).
 m.store(0xc0014000,8,UINT64_C(0xffff0000ff00ff00));m.store(0xc0014020,8,UINT64_C(0xff0000ffffffffff));
 R[0xc184]=0xfeed0000;R[0xc408]=0x10000;R[0xc400]=0x20002;R[0xc404]=0x01020100;
 R[0xc30c]=R[0xc314]=0x10001;R[0xc318]=R[0xc31c]=0x200000;
 rsxblit::scaled(s,0x80008);assert(m.load(0xc0001000,4)==0xff808080);
 // Detile into another tile, no conversion; padding stays untouched.
 auto& d=s.rsx.tiles[1];d.bound=true;d.offset=0x20000;d.size=0x10000;d.pitch=256;
 R[0x630c]=0x20000;R[0x6304]=256u<<16;R[0xc404]=0x20100;
 R[0xc30c]=R[0xc314]=0x20002;R[0xc318]=R[0xc31c]=0x100000;
 rsxblit::scaled(s,0);assert(m.load(0xc0020000,8)==UINT64_C(0xffff0000ff00ff00));
 assert(m.load(0xc0020020,8)==UINT64_C(0xff0000ffffffffff));assert(m.load(0xc0020008,8)==0);
 // A subimage's pitch may differ from tile pitch: byte offset still defines its actual tile coordinate.
 R[0xc400]=0x10002;R[0xc404]=0x20008;R[0xc30c]=R[0xc314]=0x10002;
 rsxblit::scaled(s,0);assert(m.load(0xc0020000,8)==UINT64_C(0xffff0000ff00ff00));
 // Main-memory tiles use the same permutation before IO translation.
 t.location=1;R[0xc184]=0xfeed0001;m.store(0x70014000,8,UINT64_C(0x1234567887654321));
 rsxblit::scaled(s,0);assert(m.load(0xc0020000,8)==UINT64_C(0x1234567887654321));
 // Unknown tag layouts (not stored expanded) stop atomically on either side.
 const auto old=m.load(0xc0020000,8);const auto token=m.reservation_token(0xc0020000);
 for(unsigned c:{1u,8u,13u}){
  t.compression=c;fault([&]{rsxblit::scaled(s,0);});assert(m.load(0xc0020000,8)==old && m.reservation_token(0xc0020000)==token);
  t.compression=0;d.compression=c;fault([&]{rsxblit::scaled(s,0);});assert(m.load(0xc0020000,8)==old);d.compression=0;
 }
 // Color 7 and depth 9-12 are stored expanded: same physical bytes as uncompressed.
 m.store(0x70014000,8,UINT64_C(0x0badf00d13579bdf));t.compression=7;d.compression=11;
 rsxblit::scaled(s,0);assert(m.load(0xc0020000,8)==UINT64_C(0x0badf00d13579bdf));t.compression=d.compression=0;
 // Overlapping tile descriptors must not pick one arbitrarily.
 s.rsx.tiles[2]=d;fault([&]{rsxblit::scaled(s,0);});s.rsx.tiles[2].bound=false;
 // RGB565 tile byte lanes, physical first two pixels x=0,1.
 R[0xc300]=7;R[0xc404]=0x20004;m.store(0x70014000,4,0xf80007e0);
 rsxblit::scaled(s,0);assert(m.load(0xc0020000,8)==UINT64_C(0xffff0000ff00ff00));
 // A later bad mapped row must not commit the first tiled row.
 R[0xc300]=3;R[0xc184]=0xfeed0000;R[0xc408]=0x10000;R[0xc400]=0x20002;R[0xc404]=0x20100;
 R[0xc30c]=R[0xc314]=0x20002;R[0x6188]=0xfeed0001;R[0x630c]=0x2ffff8;R[0x6304]=8u<<16;
 const auto version=m.reservation_token(0x702fff80);fault([&]{rsxblit::scaled(s,0);});
 assert(m.load(0x702ffff8,8)==0 && m.reservation_token(0x702fff80)==version);
 // Swizzled 16x16 ARGB8 destination inside a compressed tile (0x20000, pitch 256, bank 2): pixels (2..3,7..8)
 // have Morton byte offsets 184,188,528,532; physical addresses from RPCS3 ffba2bf0 tiled_dma_copy.hpp.
 d.bank=2;d.compression=7;R[0xc198]=0x31337a73;R[0x8184]=0xfeed0000;R[0x8304]=0x20000;R[0x8300]=0x0404000a;
 R[0xc184]=0xfeed0001;R[0xc408]=0x1000;R[0xc300]=3;R[0xc400]=0x20002;R[0xc404]=0x20008;
 R[0xc30c]=R[0xc314]=0x20002;R[0xc308]=R[0xc310]=0x70002;R[0xc318]=R[0xc31c]=0x100000;
 m.store(0x70001000,8,UINT64_C(0xa0a1a2a3b0b1b2b3));m.store(0x70001008,8,UINT64_C(0xc0c1c2c3d0d1d2d3));
 rsxblit::scaled(s,0);
 assert(m.load(0xc0028518,8)==UINT64_C(0xa0a1a2a3b0b1b2b3));assert(m.load(0xc0028050,8)==UINT64_C(0xc0c1c2c3d0d1d2d3));
 std::puts("PASS tiled local/main, tile-to-tile, pitch subimages, swizzled tile, compression, RGB565 and atomic faults");
}
static void operations(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 R[0xc304]=0;m.store(0x70001000,8,UINT64_C(0x0011223301556677));m.store(0xc0001000,8,UINT64_C(0xdeadbeefcafebabe));
 rsxblit::scaled(s,0);assert(m.load(0xc0001000,8)==UINT64_C(0xdeadbeef01556677));
 R[0xc300]=1;R[0xc404]=0x20004;m.store(0x70001000,4,0x7c0083e0);rsxblit::scaled(s,0);
 assert(m.load(0xc0001000,8)==UINT64_C(0xdeadbeefff00ff00));
 const auto old=m.load(0xc0001000,8);
 for(unsigned operation:{1u,2u,4u,5u,6u}){R[0xc304]=operation;fault([&]{rsxblit::scaled(s,0);});assert(m.load(0xc0001000,8)==old);}
 R[0xc304]=0;R[0xc188]=0x1234;fault([&]{rsxblit::scaled(s,0);});R[0xc188]=0;
 R[0xc304]=3;R[0xc300]=3;R[0xc404]=0x20008;
 // Opaque Y32 is a bit copy, including X8/ABGR source hints; never forced alpha or luminance.
 R[0x6300]=11;m.store(0x70001000,8,UINT64_C(0x0123456789abcdef));
 for(unsigned format:{3u,4u,12u,13u}){R[0xc300]=format;rsxblit::scaled(s,0);assert(m.load(0xc0001000,8)==UINT64_C(0x0123456789abcdef));}
 R[0xc404]=0x01020008;fault([&]{rsxblit::scaled(s,0);});R[0xc404]=0x20008;
 // Reverse in place across two different DMA contexts that alias host memory.
 s.rsx.io[1]=0xc0000000;R[0xc408]=0x101000;R[0xc318]=0xfff00000;
 rsxblit::scaled(s,16);assert(m.load(0xc0001000,8)==UINT64_C(0x89abcdef01234567));
 std::puts("PASS zero-alpha discard, explicit operation dependencies, Y32 raw data and aliased reverse");
}
static void cpu(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 // Native libgcm default is IFC ARGB8=4, NOT SIFM ARGB8=3.
 rsxblit::cpu(s,0xa400,0x12345678);rsxblit::cpu(s,0xa404,0x9abcdef0);
 assert(m.load(0xc0001000,8)==UINT64_C(0x123456789abcdef0));
 R[0xa300]=5;rsxblit::cpu(s,0xa400,0x01234567);assert(m.load(0xc0001000,4)==0xff234567);
 R[0x6300]=11;rsxblit::cpu(s,0xa400,0x01234567);assert(m.load(0xc0001000,4)==0x01234567);
 // Real nonincrementing FIFO: last arg replaces the same word, padding method ignored.
 m.store(0x70000000,4,0x40000000|(2u<<18)|0xa400);m.store(0x70000004,4,0x12345678);m.store(0x70000008,4,0x9abcdef0);
 m.store(s.rsx.dma+0x40,4,12);assert(rsx_run(s,1));assert(m.load(0xc0001000,4)==0x9abcdef0);
 R[0xa300]=4;R[0x6300]=10;R[0xa2fc]=0;rsxblit::cpu(s,0xa400,0x00112233);
 assert(m.load(0xc0001000,4)==0x9abcdef0);rsxblit::cpu(s,0xa400,0x01112233);assert(m.load(0xc0001000,4)==0x01112233);
 // Legacy packed, multirow and context state remain explicit faults; no invented streaming cursor.
 const auto old=m.load(0xc0001000,8);const auto token=m.reservation_token(0xc0001000);
 R[0xa2fc]=3;
 for(unsigned format:{1u,2u,3u,99u}){R[0xa300]=format;fault([&]{rsxblit::cpu(s,0xa400,0);});}
 R[0xa300]=4;R[0xa308]=R[0xa30c]=0x20002;fault([&]{rsxblit::cpu(s,0xa400,0);});R[0xa308]=R[0xa30c]=0x10002;
 for(unsigned reg:{0xa184u,0xa188u}){R[reg]=1;fault([&]{rsxblit::cpu(s,0xa400,0);});R[reg]=0;}
 R[0xa19c]=0x31337a73;R[0x8184]=0xfeed0000;R[0x8300]=0x0001000a;R[0x8304]=0x1000;
 fault([&]{rsxblit::cpu(s,0xa400,0);});assert(m.load(0xc0001000,8)==old && m.reservation_token(0xc0001000)==token);
 std::puts("PASS IFC format numbering, ARGB/XRGB/Y32, FIFO replace, zero alpha and explicit context/streaming guards");
}
static void sampling(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 m.store(0x70001000,8,UINT64_C(0xff000000ff400000));R[0xc30c]=R[0xc314]=0x10001;
 R[0xc404]=0x01020008;rsxblit::scaled(s,8);assert(m.load(0xc0001000,4)==0xff200000);
 // Center(1.0) and corner(.5) identify the same half-way point.
 R[0xc404]=0x01010008;rsxblit::scaled(s,16);assert(m.load(0xc0001000,4)==0xff200000);
 R[0xc404]=0x01010008;rsxblit::scaled(s,0);assert(m.load(0xc0001000,4)==0xff000000);
 for(unsigned bad:{0x01000008u,0x01030008u,0x02020008u}){R[0xc404]=bad;fault([&]{rsxblit::scaled(s,0);});}
 // Halfword swizzle 4x2: physical order A B E F C D G H, non-square Morton.
 R[0xc198]=0x31337a73;R[0x8184]=0xfeed0000;R[0x8300]=0x01020004;R[0x8304]=0x2000;
 R[0xc300]=7;R[0xc408]=0x2000;R[0xc404]=0x20008;R[0xc400]=0x20004;R[0xc30c]=R[0xc314]=0x20004;
 m.store(0x70002000,8,UINT64_C(0xf80007e0001fffff));m.store(0x70002008,8,UINT64_C(0x00000841108218c3));
 rsxblit::scaled(s,0);assert(m.load(0xc0002000,8)==UINT64_C(0xf80007e000000841));
 assert(m.load(0xc0002008,8)==UINT64_C(0x001fffff108218c3));
 std::puts("PASS center/corner, FOH, negative edge clamp, invalid interpolation and 16-bit Morton");
}
static void vulkan(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 try{s.rsx.renderer=std::make_unique<NativeVulkan>();}
 catch(const std::runtime_error& e){std::fprintf(stderr,"Vulkan unavailable: %s\n",e.what());std::exit(77);}
 // Real 2x1 and 2x2 MSAA clears, then FOH resolves from the expanded rows.
 for(unsigned aa:{3u,4u,5u}){
  const unsigned ny=aa>=4?2:1;
  R[0x194]=R[0x198]=0xfeed0000;R[0x200]=2u<<16;R[0x204]=1u<<16;R[0x208]=0x148|(aa<<12);
  R[0x220]=1;R[0x210]=0x4000;R[0x214]=0x8000;R[0x20c]=R[0x22c]=16;
  R[0x8c0]=2u<<16;R[0x8c4]=1u<<16;R[0x1d90]=0xff204060;R[0x1d8c]=0xabcdef12;R[0x32c]=255;
  rsx_clear_surface(s,0xf3);assert(s.rsx.renderer->clears>0);
  assert(m.load(0xc0004000,8)==UINT64_C(0xff204060ff204060));assert(m.load(0xc0004010,4)==(ny==2?0xff204060:0));
  m.store(0xc0004000,8,UINT64_C(0xff102030ff304050));m.store(0xc0004008,8,UINT64_C(0xff506070ff708090));
  if(ny==2){m.store(0xc0004010,8,UINT64_C(0xff304050ff506070));m.store(0xc0004018,8,UINT64_C(0xff708090ff90a0b0));}
  R[0xc184]=0xfeed0000;R[0xc408]=0x4000;R[0xc404]=0x01020010;R[0xc400]=(ny<<16)|4;
  R[0xc318]=0x200000;R[0xc31c]=ny<<20;R[0xc30c]=R[0xc314]=0x10002;
  rsxblit::scaled(s,8|(ny==2?8u<<16:0));
  assert(m.load(0xc0001000,8)==(ny==1?UINT64_C(0xff203040ff607080):UINT64_C(0xff304050ff708090)));
 }
 std::printf("PASS expanded MSAA resolve with Vulkan submissions=%llu\n",static_cast<unsigned long long>(s.rsx.renderer->clears));
}
int main(int argc,char** argv){
 assert(argc==2);const std::string name=argv[1];
 if(name=="copy")copy_test();else if(name=="formats")formats();else if(name=="tiles")tiles();
 else if(name=="operations")operations();else if(name=="cpu")cpu();else if(name=="sampling")sampling();
 else if(name=="vulkan")vulkan();else assert(false);
}
'''


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/vulkan/vulkan.h').exists(),
                     'Requires C++ and Vulkan development headers')
class RsxBlit2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='anyps3-rsxblit2-')
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        cls.binary = root / 'blit'
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        (root / 'blit.cpp').write_text(source + CPP)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                '-DANYPS3_VULKAN', str(root / 'blit.cpp'), '-lvulkan', '-o', str(cls.binary)],
                               capture_output=True, text=True, timeout=180)
        if build.returncode:
            raise AssertionError(build.stderr)

    def run_case(self, case):
        env = os.environ.copy()
        lavapipe = Path('/usr/share/vulkan/icd.d/lvp_icd.json')
        if lavapipe.exists() and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
            env['VK_DRIVER_FILES'] = str(lavapipe)
        validation = Path('/usr/share/vulkan/explicit_layer.d/VkLayer_khronos_validation.json')
        if validation.exists():
            env['VK_INSTANCE_LAYERS'] = ':'.join(filter(None, [env.get('VK_INSTANCE_LAYERS', ''), 'VK_LAYER_KHRONOS_validation']))
            env['VK_LAYER_VALIDATE_SYNC'] = '1'
        run = subprocess.run([str(self.binary), case], capture_output=True, text=True, env=env, timeout=30)
        if run.returncode == 77 and env.get('ANYPS3_REQUIRE_VULKAN') != '1':
            self.skipTest(run.stderr)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('PASS ', run.stdout)
        self.assertNotIn('Validation Error', run.stdout + run.stderr)
        self.assertNotIn('Validation Warning', run.stdout + run.stderr)
        if case == 'vulkan':
            self.assertIn('Vulkan submissions=', run.stdout)
            self.assertIn('native Vulkan device=', run.stderr)

    def test_stride3_physical_bytes_and_atomic_faults(self):
        self.run_case('copy')

    def test_source_formats_and_conversion(self):
        self.run_case('formats')

    def test_tiled_images_golden_addresses_and_faults(self):
        self.run_case('tiles')

    def test_alpha_discard_y32_and_dma_aliases(self):
        self.run_case('operations')

    def test_cpu_format_numbering_and_fifo(self):
        self.run_case('cpu')

    def test_sampling_origins_and_halfword_swizzle(self):
        self.run_case('sampling')

    def test_msaa_resolve_with_real_vulkan(self):
        self.run_case('vulkan')
