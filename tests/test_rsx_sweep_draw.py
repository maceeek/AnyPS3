"""Fixed expected fragments plus sequential/parallel comparison for new state."""
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
static unsigned bits(float f){unsigned w;std::memcpy(&w,&f,4);return w;}
static void stencil_state(PpuState& s){
 auto& R=s.rsx.registers;R[0x328]=1;
 for(unsigned off:{0u,32u}){
  R[0x32c+off]=255;R[0x330+off]=0x207;R[0x334+off]=0xa5;R[0x338+off]=255;
  R[0x33c+off]=R[0x340+off]=R[0x344+off]=0x1e00;
 }
}
static void setup(PpuState& s,unsigned w=8,unsigned h=8){setup_draw(s,w,h,w*4,false);}
static void redraw(PpuState& s){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
static void fixed(){
 using namespace rsxdraw;
 // Explicit truth tables, covering saturating and wrapping endpoints.
 const unsigned ops[]={0,0x1e00,0x1e01,0x1e02,0x1e03,0x150a,0x8507,0x8508};
 const unsigned at_ff[]={0,255,0x35,255,254,0,0,254},at_zero[]={0,0,0x35,1,0,255,1,255};
 for(unsigned i=0;i<8;++i){assert(stencil_op(ops[i],255,0x35)==at_ff[i]);assert(stencil_op(ops[i],0,0x35)==at_zero[i]);}
 const unsigned truth[]={0x00,0x88,0x44,0xcc,0x22,0xaa,0x66,0xee,0x11,0x99,0x55,0xdd,0x33,0xbb,0x77,0xff};
 for(unsigned i=0;i<16;++i)assert((logic_op(0x1500+i,0xcc,0xaa)&255)==truth[i]);
 auto f=fog_value(0x2601,0.5f,1.f,0.5f);assert(f[0]==0.5f && f[1]==0.25f && f[2]==0 && f[3]==0);
 f=fog_value(0x804,-0.5f,1.f,0.5f);assert(f[0]==-0.5f && f[1]==0.25f);
 f=fog_value(0x800,0.5f,1.f,1.f);assert(f[1]==1);f=fog_value(0x801,0.5f,1.f,1.f);assert(f[1]==1);
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;stencil_state(s);
  R[0x32c]=15;R[0x344]=0x1e01;R[0x17cc]=1;
  m.store(0xc0500000,4,0x1234563c);redraw(s);
  assert(m.load(0xc0500000,4)==0x12345635 && m.load(0xc0100000,4)==0x80ff8040);
  assert(s.rsx.report_values[0]==64 && s.rsx.draw.pixels==64);
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;stencil_state(s);
  R[0x330]=0x202;R[0x33c]=0x1e02;m.store(0xc0500000,4,3);redraw(s);
  assert(m.load(0xc0500000,4)==4 && m.load(0xc0100000,4)==0 && s.rsx.draw.pixels==0);
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;stencil_state(s);
  R[0xa74]=1;R[0xa6c]=0x201;R[0x340]=0x1e02;m.store(0xc0500000,4,5);redraw(s);
  assert(m.load(0xc0500000,4)==6 && m.load(0xc0100000,4)==0);
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;stencil_state(s);R[0x344]=0x1e01;
  R[0x304]=1;R[0x308]=0x204;R[0x30c]=200;redraw(s);
  assert(s.memory.load(0xc0500000,4)==0 && s.rsx.draw.pixels==0); // alpha fails before stencil replace
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;stencil_state(s);R[0x344]=0x1e01;
  R[0x380]=1;R[0x384]=bits(0.2f);R[0x388]=bits(0.5f);s.memory.store(0xc0500000,4,0x20000011);redraw(s);
  assert(s.memory.load(0xc0500000,4)==0x20000011 && s.rsx.draw.pixels==0);
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0xa70]=1;R[0xa68]=1;R[0xa7c]=bits(8.f);redraw(s);
  assert(s.memory.load(0xc0500000,4)==0x40000800); // z=.25 + eight Z24 units
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x374]=1;R[0x378]=0x1506;
  R[0x310]=1;R[0x324]=0x00010100;s.memory.store(0xc0100000,4,0xaa55aa55);redraw(s);
  assert(s.memory.load(0xc0100000,4)==0xaaaa2a55); // XOR after quantization, RG mask, blend bypassed
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x2c0]=0x00040002;R[0x2c4]=0x00050003;redraw(s);
  assert(s.rsx.draw.pixels==9 && s.memory.load(0xc0100068,4)==0x80ff8040 && s.memory.load(0xc0100000,4)==0);
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
  // MOV fog.yyyy -> R0. Fog coordinate defaults to zero, linear factor is .5.
  const unsigned words[]={(1u<<24)|(15u<<9)|(3u<<13)|1u,1u|(0x55u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)m.store(0xc0001000+i*4,4,(words[i]<<16)|(words[i]>>16));
  R[0x8cc]=0x2601;R[0x8d0]=bits(1.5f);R[0x8d4]=0;redraw(s);
  assert(m.load(0xc0100000,4)==0x80808080);
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x1740]=0x00021042;
  auto v=fetch_attribute(s,Reg{s.rsx},0,3);assert(v[0]==8 && v[1]==0); // 3/2 -> vertex 1
  R[0x1fc0]=1;v=fetch_attribute(s,Reg{s.rsx},0,4);assert(v[0]==0 && v[1]==0); // 4%2 -> vertex 0
 }
 for(unsigned width:{2u,4u}){
  PpuState s;setup(s);auto& R=s.rsx.registers;auto& d=s.rsx.draw;
  d.primitive=6;d.indexed=true;d.ranges={{0,7}};R[0x1820]=width==2?0x11:1;R[0x181c]=0x3000;R[0x1dac]=1;R[0x1db0]=0xffffffff;
  const unsigned ix[]={0,1,2,width==2?0xffffu:0xffffffffu,0,2,3};
  for(unsigned i=0;i<7;++i)s.memory.store(0x70003000+width*i,width,ix[i]);
  rsx_execute_draw(s);assert(d.triangles==2 && d.pixels==64);
 }

 {
  PpuState s;setup(s);auto& g=s.rsx;
  rsx_vertex_data_register(g,0x1908,0x80007fff);auto v=register_attribute(g.draw.immediate[2]);
  assert(v[0]==32767 && v[1]==-32768 && v[2]==0 && v[3]==1);
  rsx_vertex_data_register(g,0xa90,0x7fff8000);rsx_vertex_data_register(g,0xa94,0x00004000);
  v=register_attribute(g.draw.immediate[2]);assert(v[0]==-1 && v[1]==1 && v[2]==16384.f/32767.f && v[3]==0);
 }
 {
  PpuState s;setup(s);s.rsx.registers[0x2b8]=0x00010001;redraw(s);
  assert(s.memory.load(0xc0100000,4)==0 && s.memory.load(0xc0100024,4)==0x80ff8040 && s.rsx.draw.pixels==64);
 }
 {
  PpuState s;setup(s);auto& g=s.rsx;auto& R=g.registers;auto& m=s.memory;
  // Append a MOV from attribute 2 into back diffuse (o3). Front o1 stays black.
  g.transform_program[13*4+3]&=~1u;
  const unsigned words[]={0x401f9c6c,0x0040020d,0x8106c083,0x6041ff8d};
  for(unsigned i=0;i<4;++i){g.transform_program[14*4+i]=words[i];g.transform_program_written[14*4+i]=true;}
  R[0x1ff0]|=4;R[0x1748]=0;rsx_vertex_data_register(g,0x1948,0x00ff00ff);
  const unsigned fp[]={(1u<<24)|(15u<<9)|(1u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)m.store(0xc0001000+i*4,4,(fp[i]<<16)|(fp[i]>>16));
  R[0x142c]=1;R[0x1834]=0x901;redraw(s);assert(m.load(0xc0100000,4)==0xff00ff00);
  R[0x1834]=0x900;redraw(s);assert(m.load(0xc0100000,4)==0xff000000);
 }

 {
  PpuState s;setup(s);auto& g=s.rsx;auto& m=s.memory;
  m.map(0x10000,0x1000,6,nullptr,0);g.dma=0x10000;g.fifo_enabled=true;
  g.draw.primitive=0;g.draw.ranges.clear();unsigned at=0;
  auto emit=[&](unsigned reg,unsigned value){m.store(0x70000000+at,4,0x40000|reg);m.store(0x70000004+at,4,value);at+=8;};
  emit(0x1808,5);emit(0x180c,0x00010000);emit(0x1810,2);emit(0x180c,0x00020000);emit(0x1810,3);emit(0x1808,0);
  m.store(g.dma+0x40,4,at);assert(rsx_run(s,20) && m.load(g.dma+0x44,4)==at);
  assert(g.draw.triangles==2 && g.draw.pixels==64 && g.draw.elements.empty());
 }

 {
  PpuState s;setup(s);s.rsx.registers[0x1fec]=1;redraw(s);
  assert(s.memory.load(0xc0100000,4)==0x80ffbc89); // Linear RGB (1,.5,.25) -> sRGB (255,188,137), alpha untouched.
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x1fec]=1;R[0x310]=1;R[0x314]=0x03020302;R[0x318]=0x03030303;R[0x320]=0x80068006;
  s.memory.store(0xc0100000,4,0xff000000);redraw(s);
  assert(s.memory.load(0xc0100000,4)==0xbfbc8963); // .5 * linear RGB, then sRGB encode; alpha=.75 ->191.
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
  const unsigned fp[]={(1u<<24)|(15u<<9)|(4u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)m.store(0xc0001000+i*4,4,(fp[i]<<16)|(fp[i]>>16));
  R[0x1ff4]=3;bool failed=false;try{redraw(s);}catch(const std::runtime_error&){failed=true;}
  assert(failed && m.load(0xc0100000,4)==0); // Masked TEX0 cannot silently be read anyway.
 }
 std::puts("PASS fixed stencil/logic/fog truth tables, fragment effects/ordering/bounds/offset/masks/windows/restart/dividers");
}
static void parallel(){
 // Each state has a manually asserted small counterpart above; here compare
 // complete guest storage after large overlapping draws with 1 and 4 threads.
 for(unsigned n=0;n<16;++n){
  PpuState a,b;setup(a,163,141);setup(b,163,141);
  for(auto* s:{&a,&b}){
   auto& R=s->rsx.registers;auto& m=s->memory;stencil_state(*s);
   R[0x348]=1;R[0x344]=0x8507;R[0x364]=0x8508;R[0x334]=7;R[0x354]=11;
   R[0xa70]=1;R[0xa74]=1;R[0xa6c]=0x203;
   if(n&1){R[0x374]=1;R[0x378]=0x1500+n;}
   if(n&2){R[0x380]=1;R[0x384]=0;R[0x388]=bits(1.f);}
   if(n&4){R[0x1fec]=1;R[0xa68]=1;R[0xa78]=bits(.5f);R[0xa7c]=bits(2.f);}
   if(n&8){R[0x2bc]=1;R[0x2c0]=0x00200008;R[0x2c4]=0x00300010;}
   for(unsigned i=0;i<163*141;++i)m.store(0xc0500000+i*4,4,0xffffff00|i%256);
   s->rsx.draw.ranges={{0,4},{0,4},{0,4}};R[0x17cc]=1;
  }
  setenv("ANYPS3_RSX_THREADS","1",1);rsx_execute_draw(a);
  const auto before=test_dispatches;setenv("ANYPS3_RSX_THREADS","4",1);rsx_execute_draw(b);
  assert(test_dispatches==before+1);
  assert(a.memory.regions.size()==b.memory.regions.size());
  for(unsigned i=0;i<a.memory.regions.size();++i)assert(*a.memory.regions[i].bytes==*b.memory.regions[i].bytes);
  assert(a.rsx.draw.pixels==b.rsx.draw.pixels && a.rsx.draw.triangles==b.rsx.draw.triangles && a.rsx.report_values==b.rsx.report_values);
 }
 std::puts("PASS 16 large sequential/4-worker fragment draws with stencil, logic, bounds, offset, windows, overlapping primitives");
}

static void textures(){
 using namespace rsxdraw;PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 R[0x1a00]=0x8000;R[0x1a08]=0x0303;R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;
 R[0x1a14]=0x01010000;R[0x1a18]=0x00010001;
 auto decode=[&](unsigned format,unsigned pitch){R[0x1a04]=0x10029|(format<<8);R[0x1840]=pitch;return decode_texture(s,Reg{s.rsx},0);};
 m.store(0xc0008000,1,128);auto t=decode(0xa1,1);auto c=sample_texture(t,.5f,.5f);
 assert(c[0]==128/255.f && c[1]==128/255.f && c[2]==128/255.f && c[3]==1);
 m.store(0xc0008000,2,0xfc00);t=decode(0xa2,2);c=sample_texture(t,.5f,.5f);assert(c[0]==1 && c[1]==0 && c[2]==0 && c[3]==1);
 m.store(0xc0008000,2,0x8f42);t=decode(0xa3,2);c=sample_texture(t,.5f,.5f);assert(c[0]==1 && c[1]==4/15.f && c[2]==2/15.f && c[3]==8/15.f);
 m.store(0xc0008000,2,0x07e0);t=decode(0xa4,2);c=sample_texture(t,.5f,.5f);assert(c[0]==0 && c[1]==1 && c[2]==0 && c[3]==1);
 m.store(0xc0008000,2,0x8040);t=decode(0xab,2);c=sample_texture(t,.5f,.5f);assert(c[0]==64/255.f && c[1]==128/255.f && c[2]==64/255.f && c[3]==128/255.f);
 R[0x1a18]=0x00040002;const unsigned morton[]={1,2,5,6,3,4,7,8};
 for(unsigned i=0;i<8;++i)m.store(0xc0008000+i*4,4,0xff000000|morton[i]);
 t=decode(0x85,0);
 for(unsigned y=0;y<2;++y)for(unsigned x=0;x<4;++x){c=texel(t,t.levels[0],x,y);assert(c[0]==0 && c[1]==0 && c[2]==float(y*4+x+1)/255.f && c[3]==1);}
 std::puts("PASS BE B8/A1R5G5B5/A4R4G4B4/R5G6B5/G8B8 textures and rectangular swizzled ARGB8");
}
int main(int argc,char** argv){assert(argc==2);if(std::string(argv[1])=="fixed")fixed();else if(std::string(argv[1])=="textures")textures();else parallel();}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class RsxSweepDrawTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='rsxsweep-draw-')
        root = Path(cls.tmp.name)
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source = source.replace('namespace rsxdraw {', 'static unsigned test_dispatches=0;\nnamespace rsxdraw {', 1)
        source = source.replace('static thread_local DrawPool pool;', '++test_dispatches;static thread_local DrawPool pool;')
        harness = HARNESS.split('static void observe(')[0].replace('GAME_VP_WORDS', ','.join(f'{w}u' for w in GAME_VP))
        (root/'test.cpp').write_text(source+'\n#include <cassert>\n'+harness+MAIN)
        build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-pthread','-o',str(root/'test')],capture_output=True,text=True,timeout=60)
        if build.returncode:
            cls.tmp.cleanup()
            raise AssertionError(build.stderr)
        cls.exe = root/'test'

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, mode):
        run = subprocess.run([str(self.exe),mode],capture_output=True,text=True,timeout=30,env={**os.environ,'ANYPS3_RSX_THREADS':'1'})
        self.assertEqual(run.returncode,0,run.stderr)

    def test_independent_fixed_fragments(self):
        self.run_case('fixed')

    def test_texture_formats_and_swizzle(self):
        self.run_case('textures')

    def test_parallel_fragment_equivalence(self):
        self.run_case('parallel')
