"""Native texture regression oracles: literal texels/offsets, no reference decoder."""
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

MAIN = r'''
using namespace rsxdraw;
static void near(float a,float b){assert(std::fabs(a-b)<1.e-6f);}
static void eq(const Vec4& a,const Vec4& b){for(unsigned i=0;i<4;++i)near(a[i],b[i]);}
static void init(PpuState& s,unsigned format=0xa5,unsigned w=1,unsigned h=1,unsigned mips=1,unsigned dim=2,bool cube=false){
 if(s.memory.regions.empty())setup_draw(s,8,8,32,false);
 auto& R=s.rsx.registers;
 R[0x1a00]=0x8000;R[0x1a04]=(mips<<16)|(format<<8)|(dim<<4)|9|(cube?4:0);
 R[0x1a08]=0x30303;R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a10]=0xaae4;
 R[0x1a14]=0x01010000;R[0x1a18]=(w<<16)|h;R[0x1840]=0;
}
static Texture decode(PpuState& s){return decode_texture(s,Reg{s.rsx},0);}
static void signed_remap(){
 PpuState s;init(s);auto& R=s.rsx.registers;auto& m=s.memory;
 m.store(0xc0008000,4,0x807fff00);R[0x1a14]=0xf1010000;
 eq(sample_texture(decode(s),.5f,.5f),{1,-1/127.f,0,-1});
 // Signed mask follows the selected source, gamma follows the destination.
 m.store(0xc0008000,4,0xff808080);R[0x1a14]=0x21010000;R[0x1a08]=0x00331303;
 eq(sample_texture(decode(s),.5f,.5f),{-1,.2158605f,1/255.f,1});
 R[0x1a14]=0x41010000;R[0x1a08]=0x00131303;R[0x1a10]=0xaae8;
 eq(sample_texture(decode(s),.5f,.5f),{-1,-1,1/255.f,1});
 // Remap constants bypass both SNORM and BX2; R=ONE, B=ZERO.
 R[0x1a10]=0x26e4;R[0x1a14]=0xf1010000;R[0x1a08]=0x31303;
 eq(sample_texture(decode(s),.5f,.5f),{1,-1,0,-1/127.f});
 // SNORM is a property of the bytes before bilinear filtering, not a
 // reinterpretation of the positive UNORM interpolation result.
 init(s,0xa5,2,1);R[0x1a14]=0x22020000;
 m.store(0xc0008000,4,0xff7f0000);m.store(0xc0008004,4,0xff810000);
 eq(sample_texture(decode(s),.5f,.5f),{0,0,0,1});
 // Bias-only is BX2. Midpoint 128/255 maps to +1/255, endpoints to -1,+1.
 init(s);R[0x1a08]=0x31303;m.store(0xc0008000,4,0xff0080ff);
 eq(sample_texture(decode(s),.5f,.5f),{-1,1/255.f,1,1});
 std::puts("PASS SNORM masks/filtering, remapped precedence, constants and BX2");
}
static void formats(){
 PpuState s;auto& m=s.memory;auto& R=s.rsx.registers;
 init(s,0xb8);m.store(0xc0008000,2,0x20e0);
 eq(sample_texture(decode(s),.5f,.5f),{32/255.f,224/255.f,32/255.f,224/255.f});
 R[0x1a10]=0x1aae4;
 eq(sample_texture(decode(s),.5f,.5f),{32/255.f,32/255.f,32/255.f,224/255.f});
 R[0x1a10]=0xaa4e;
 eq(sample_texture(decode(s),.5f,.5f),{224/255.f,32/255.f,224/255.f,32/255.f});
 init(s,0xb9);m.store(0xc0008000,2,0x807f);R[0x1a14]|=0xf0000000;R[0x1a08]|=0x1000;
 eq(sample_texture(decode(s),.5f,.5f),{-1,1,-1,1});
 init(s,0xaf);m.store(0xc0008000,2,0x07f0);
 eq(sample_texture(decode(s),.5f,.5f),{1/63.f,1,16/31.f,1});
 init(s,0xad,2);m.store(0xc0008000,4,0x14283c50);
 eq(sample_texture(decode(s),.25f,.5f),{40/255.f,60/255.f,20/255.f,1});
 eq(sample_texture(decode(s),.75f,.5f),{80/255.f,60/255.f,20/255.f,1});
 init(s,0xae,3);m.store(0xc0008000,4,0x2814503c);m.store(0xc0008004,4,0x80ff0040);
 eq(sample_texture(decode(s),.25f,.5f),{40/255.f,60/255.f,20/255.f,1});
 eq(sample_texture(decode(s),.5f,.5f),{80/255.f,60/255.f,20/255.f,1});
 eq(sample_texture(decode(s),.9f,.5f),{128/255.f,64/255.f,1,1});
 // Two-word formats: upper remap word switches XYXY to XXXY.
 init(s,0xb5);m.store(0xc0008000,4,0x4000c000);
 eq(sample_texture(decode(s),.5f,.5f),{16384/65535.f,49152/65535.f,16384/65535.f,49152/65535.f});
 R[0x1a10]=0x1aae4;
 eq(sample_texture(decode(s),.5f,.5f),{16384/65535.f,16384/65535.f,16384/65535.f,49152/65535.f});
 R[0x1a14]=0xf1010000;
 eq(sample_texture(decode(s),.5f,.5f),{16384/32767.f,16384/32767.f,16384/32767.f,-16384/32767.f});
 init(s,0xbf);m.store(0xc0008000,4,0x3c00c000);R[0x1a10]=0x1aae4;
 eq(sample_texture(decode(s),.5f,.5f),{-2,-2,-2,1});
 std::puts("PASS HILO8/S8, packed odd-width pairs, R6G5B5, 16-bit SNORM and remap order");
}
static void depth(){
 PpuState s;auto& m=s.memory;auto& R=s.rsx.registers;
 init(s,0xb3);
 const unsigned raw[]={0,1,0x1000,0xf000,0xffff};
 const float expected[]={0,0x1p-27f,0x1p-15f,.5f,.9998779296875f};
 for(unsigned i=0;i<5;++i){m.store(0xc0008000,2,raw[i]);const auto c=sample_texture(decode(s),.5f,.5f);
  for(float value:c)assert(float_bits(value)==float_bits(expected[i]));}
 R[0x1a08]=0x10030303;m.store(0xc0008000,2,0xf000);
 eq(sample_texture(decode(s),.5f,.5f,0,.25f),{1,1,1,1});
 eq(sample_texture(decode(s),.5f,.5f,0,.75f),{0,0,0,0});
 init(s,0xb1);m.store(0xc0008000,4,0x7e00005a);
 for(float value:sample_texture(decode(s),.5f,.5f))assert(float_bits(value)==0x3f000000);
 m.store(0xc0008000,4,0x7f0000ff);eq(sample_texture(decode(s),.5f,.5f),{1,1,1,1});
 std::puts("PASS BE D16 E4M12 zero/subnormal/endpoints and D24 float/stencil separation");
}
static void layouts(){
 PpuState s;auto& m=s.memory;auto& R=s.rsx.registers;
 init(s,0x85,3,2);m.store(0xc0008000,4,0xff100000);m.store(0xc0008004,4,0xff200000);
 m.store(0xc0008010,4,0xff300000);m.store(0xc0008008,4,0xff400000);
 m.store(0xc000800c,4,0xff500000);m.store(0xc0008018,4,0xff600000);
 auto t=decode(s);assert(t.storage_size==28);
 eq(sample_texture(t,5/6.f,.75f),{96/255.f,0,0,1});eq(sample_texture(t,5/6.f,.25f),{48/255.f,0,0,1});
 init(s,0x85,4,2,3);for(unsigned i=0;i<8;++i)m.store(0xc0008000+4*i,4,0xffff0000);
 m.store(0xc0008020,4,0xff00ff00);m.store(0xc0008024,4,0xff0000ff);m.store(0xc0008028,4,0xffffffff);
 R[0x1a14]=0x01030000;t=decode(s);assert(t.storage_size==44);
 eq(sample_texture(t,.75f,.5f,1),{0,0,1,1});eq(sample_texture(t,.5f,.5f,2),{1,1,1,1});
 // 4x2x2 Morton XYZ: X high bit occupies bit3, Z low bit occupies bit2.
 init(s,0x85,4,2,1,3);R[0x1840]=2u<<20;
 m.store(0xc0008034,4,0xff123456); // x=3,y=0,z=1: 1+8+4=13
 t=decode(s);eq(sample_texture(t,.875f,.25f,0,0,.75f),{18/255.f,52/255.f,86/255.f,1});
 // Linear 3D slices, eight corner colors and trilinear centre.
 init(s,0xa5,2,2,1,3);R[0x1840]=(2u<<20)|8;R[0x1a14]=0x02020000;
 const unsigned corners[]={0xff000000,0xffff0000,0xff00ff00,0xffffff00,0xff0000ff,0xffff00ff,0xff00ffff,0xffffffff};
 for(unsigned i=0;i<8;++i)m.store(0xc0008000+i*4,4,corners[i]);
 t=decode(s);
 eq(sample_texture(t,.5f,.5f,0,0,.5f),{.5f,.5f,.5f,1});
 eq(sample_texture(t,.25f,.25f,0,0,.75f),{0,0,1,1});
 near(texture_gradient_length(t,{},{0,0,.5f,0}),1);
 // 128-bit pixels: expanded X word order in Morton storage.
 init(s,0x9b,2,2);const unsigned offsets[]={32,36,48,52};
 const unsigned f[]={0x3f800000,0x40000000,0x40400000,0x40800000};
 for(unsigned i=0;i<4;++i)m.store(0xc0008000+offsets[i],4,f[i]);
 eq(sample_texture(decode(s),.75f,.25f),{1,2,3,4});
 // VTC 3D DXT1 blocks: four adjacent Z blocks, then a two-slice tail.
 init(s,0xa6,4,4,1,3);R[0x1840]=(6u<<20)|8;
 const unsigned endpoints[]={0xf800,0x07e0,0x001f,0xffff,0x0000,0xffe0};
 const Vec4 dxt_expected[]={{1,0,0,1},{0,1,0,1},{0,0,1,1},{1,1,1,1},{0,0,0,1},{1,1,0,1}};
 for(unsigned i=0;i<6;++i){m.store(0xc0008000+i*8,1,endpoints[i]&255);m.store(0xc0008001+i*8,1,endpoints[i]>>8);m.store(0xc0008002+i*8,2,0);m.store(0xc0008004+i*8,4,0);}
 t=decode(s);for(unsigned i=0;i<6;++i)eq(sample_texture(t,.5f,.5f,0,0,(i+.5f)/6),dxt_expected[i]);
 // Swizzled HILO and non-LN border storage share the same Morton addressor.
 init(s,0x98,2,2);m.store(0xc0008006,2,0x40c0);
 eq(sample_texture(decode(s),.75f,.75f),{64/255.f,192/255.f,64/255.f,192/255.f});
 init(s,0x85);R[0x1a04]&=~8u;
 m.store(0xc00080c0,4,0xff123456); // 16x16 padded border: x=y=4 -> Morton index48
 eq(sample_texture(decode(s),.5f,.5f),{18/255.f,52/255.f,86/255.f,1});
 std::puts("PASS NPOT ceil Morton, packed mip offsets, 3D XYZ/slices/filter, wide texels");
}
static void cubes(){
 PpuState s;auto& m=s.memory;auto& R=s.rsx.registers;
 const unsigned colors[]={0xffff0000,0xff00ff00,0xff0000ff,0xffffff00,0xffff00ff,0xff00ffff};
 const Vec4 expected[]={{1,0,0,1},{0,1,0,1},{0,0,1,1},{1,1,0,1},{1,0,1,1},{0,1,1,1}};
 const Vec4 dirs[]={{1,0,0,0},{-1,0,0,0},{0,1,0,0},{0,-1,0,0},{0,0,1,0},{0,0,-1,0}};
 for(bool linear:{false,true}){
  init(s,linear?0xa5:0x85,2,2,2,2,true);R[0x1840]=8;R[0x1a14]=0x01030000;
  for(unsigned face=0;face<6;++face){const unsigned at=0xc0008000+face*(linear?24:128);
   for(unsigned i=0;i<4;++i)m.store(at+i*4,4,colors[face]);
   m.store(at+16,4,0xff804020);}
  const auto t=decode(s);assert(t.levels.size()==12);
  for(unsigned i=0;i<6;++i){eq(sample_texture(t,dirs[i][0],dirs[i][1],0,0,dirs[i][2]),expected[i]);
   eq(sample_texture(t,dirs[i][0],dirs[i][1],1,0,dirs[i][2]),{128/255.f,64/255.f,32/255.f,1});}
 }
 init(s,0x85,2,2,1,2,true);m.store(0xc0008000,4,0xffff0000);m.store(0xc0008004,4,0xff00ff00);
 m.store(0xc0008008,4,0xff0000ff);m.store(0xc000800c,4,0xffffffff);auto t=decode(s);
 eq(sample_texture(t,1,.5f,0,0,.5f),{1,0,0,1});eq(sample_texture(t,1,-.5f,0,0,-.5f),{1,1,1,1});
 near(texture_gradient_length(t,{1,0,0,0},{0,0,1,0}),1);
 // Fragment TEX actually carries the third coordinate to cube and 3D fetch.
 std::array<Texture,16> textures{};textures[0]=t;FpContext ctx;ctx.textures=&textures;ctx.write(0,false,15,{1,-.5f,-.5f,0});
 FpInst inst{};inst.d=(23u<<24)|(15u<<9)|1u;inst.s0=0|(0xe4u<<9)|(7u<<18);
 run_fragment_program({inst},ctx);eq(ctx.read_r(0),{1,1,1,1});
 std::puts("PASS six cube faces, LN/packed face-major mip chains, orientation and FP TEX");
}
static void lod_border(){
 PpuState s;init(s,0xa5,4,4,3);auto& R=s.rsx.registers;auto& m=s.memory;R[0x1840]=16;
 for(unsigned i=0;i<16;++i)m.store(0xc0008000+i*4,4,0xffff0000);
 for(unsigned i=0;i<8;++i)m.store(0xc0008040+i*4,4,0xff00ff00);
 m.store(0xc0008060,4,0xff0000ff);
 R[0x1a14]=0x01030100;auto t=decode(s);eq(sample_texture(t,.5f,.5f,0),{0,1,0,1});
 R[0x1a14]=0x01031f00;t=decode(s);eq(sample_texture(t,.5f,.5f,1),{1,0,0,1});near(t.bias,-1);
 R[0x1a14]=0x01030000;R[0x1a0c]=0x80000000|(256u<<19)|(256u<<7);
 eq(sample_texture(decode(s),.5f,.5f,8),{0,1,0,1});eq(sample_texture(decode(s),.5f,.5f,-8),{0,1,0,1});
 R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a14]=0x01050000;
 eq(sample_texture(decode(s),.5f,.5f,.5f),{.5f,.5f,0,1});
 init(s);R[0x1a08]=0x404;R[0x1a1c]=0x80402010;
 eq(sample_texture(decode(s),-1,.5f),{64/255.f,32/255.f,16/255.f,128/255.f});
 // Border SNORM uses biased rather than signed byte interpretation.
 R[0x1a14]|=0xf0000000;
 eq(sample_texture(decode(s),-1,.5f),{-64/127.f,-96/127.f,-112/127.f,0});
 // Signed flags must not reinterpret native floating point formats.
 init(s,0xbc);R[0x1a14]|=0xf0000000;R[0x1a08]|=0x00f01000;m.store(0xc0008000,4,0xc0000000);
 eq(sample_texture(decode(s),.5f,.5f),{-2,-2,-2,-2});
 std::puts("PASS signed LOD bias, min/max, mip interpolation, border color/SNORM and float flags");
}
static void validation(){
 for(unsigned n=2;n<8;++n){PpuState s;init(s);auto& R=s.rsx.registers;
  if(n==2)R[0x1a08]|=0x03000000;
  if(n==3)R[0x1a08]|=0x2000;
  if(n==4)R[0x1a10]=0xabe4;
  if(n==5){R[0x1a04]=0x1a53d;R[0x1840]=0;}if(n==6){R[0x1a04]|=4;R[0x1a18]=0x20001;}
  if(n==7){R[0x1a00]=0x7fffff;}
  const auto before=*s.memory.regions[1].bytes;bool failed=false;try{(void)decode(s);}catch(const std::runtime_error&){failed=true;}
  assert(failed);assert(before==*s.memory.regions[1].bytes);
 }
 std::puts("PASS explicit unknown semantics/invalid layouts and error buffer invariance");
}
static void parallel(){
 for(unsigned variant=0;variant<8;++variant){
  PpuState a,b;
  for(auto* s:{&a,&b}){
   setup_draw(*s,163,141,652,false);init(*s,variant==0?0xa5:variant==1?0x85:variant==2?0xb8:variant==3?0xb9:variant==4?0xad:variant==5?0xb3:0xa5,4,4);
   auto& R=s->rsx.registers;auto& m=s->memory;
   if(variant==0){R[0x1a14]=0x22020000;R[0x1a08]|=0x1000;}
   if(variant==1){R[0x1a04]|=2u<<16;R[0x1a14]=0x02060000;R[0x1a14]|=0x80;}
   if(variant==6){R[0x1a04]|=4;}
   if(variant==7){R[0x1a04]=(R[0x1a04]&~0xf0)|0x30;R[0x1840]=(4u<<20)|16;}
   if(variant==6 || variant==7){R[0x1744]=0x1042;R[0x1684]=0x80002100;
    const Vec4 coord=variant==6?Vec4{1,0,1,0}:Vec4{.5f,.5f,.5f,0};
    for(unsigned i=0;i<4;++i)for(unsigned c=0;c<4;++c)m.store(0x70002100+i*16+c*4,4,float_bits(coord[c]));}
   for(unsigned i=0;i<1024;++i)(*m.regions[1].bytes)[0x8000+i]=(i*31+variant*17)&255;
   const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1|(0xe4u<<9)|(7u<<18),0,0};
   for(unsigned i=0;i<4;++i)m.store(0xc0001000+i*4,4,(words[i]<<16)|(words[i]>>16));
  }
  const auto before=test_dispatches;setenv("ANYPS3_RSX_THREADS","1",1);rsx_execute_draw(a);assert(test_dispatches==before);
  setenv("ANYPS3_RSX_THREADS","4",1);rsx_execute_draw(b);assert(test_dispatches==before+1);
  for(unsigned i=0;i<a.memory.regions.size();++i)assert(*a.memory.regions[i].bytes==*b.memory.regions[i].bytes);
  assert(a.rsx.draw.pixels==b.rsx.draw.pixels && a.rsx.draw.triangles==b.rsx.draw.triangles);
 }
 std::puts("PASS eight texture states, actual 4-worker dispatch and exact memory/counters");
}
int main(int argc,char** argv){assert(argc==2);const std::string mode=argv[1];
 if(mode=="signed")signed_remap();else if(mode=="formats")formats();else if(mode=="depth")depth();else if(mode=="layouts")layouts();
 else if(mode=="cubes")cubes();else if(mode=="lod")lod_border();else if(mode=="parallel")parallel();else validation();}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class RsxTextureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='rsxtex-')
        root = Path(cls.tmp.name)
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('namespace rsxdraw {', 'static unsigned test_dispatches=0;\nnamespace rsxdraw {', 1)
        source = source.replace('static thread_local DrawPool pool;', '++test_dispatches;static thread_local DrawPool pool;')
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        harness = HARNESS.split('static void observe(')[0].replace('GAME_VP_WORDS', ','.join(f'{w}u' for w in GAME_VP))
        (root/'test.cpp').write_text(source+'\n#include <cassert>\n'+harness+MAIN)
        build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-pthread','-o',str(root/'test')],capture_output=True,text=True,timeout=120)
        if build.returncode:
            cls.tmp.cleanup()
            raise AssertionError(build.stderr)
        cls.exe = root/'test'

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_case(self, mode):
        run = subprocess.run([str(self.exe),mode],capture_output=True,text=True,timeout=45,env={**os.environ,'ANYPS3_RSX_THREADS':'1'})
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)

    def test_signed_gamma_biased_remap(self): self.run_case('signed')
    def test_packed_and_two_channel_formats(self): self.run_case('formats')
    def test_float_depth(self): self.run_case('depth')
    def test_swizzled_mip_npot_and_3d(self): self.run_case('layouts')
    def test_cube_faces_mips_and_shader(self): self.run_case('cubes')
    def test_lod_and_border(self): self.run_case('lod')
    def test_parallel_new_texture_states(self): self.run_case('parallel')
    def test_explicit_errors(self): self.run_case('validation')
