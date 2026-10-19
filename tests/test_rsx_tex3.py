"""RSXTEX3 literal sample/address oracles, with explicit test-only draw integration."""
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
from tests.test_rsx_tex import MAIN as TEXTURE_HELPERS

# These three changes belong to the rsx_draw.py owner. Do not edit that file.
# Per-lane kill must respect both the control-flow active mask and instruction
# predicate, even if all destination components are masked or disabled.
DRAW_INTEGRATION = (
    ('std::array<Vec4,4> result{};',
     'std::array<Vec4,4> result{};std::array<bool,4> texture_kill{};'),
    ('sample_texture(t,x[l][0],x[l][1],lod,x[l][2],x[l][2]);',
     'sample_texture(t,x[l][0],x[l][1],lod,x[l][2],x[l][2],&texture_kill[l]);'),
    ('if(opcode==18){',
     'if(texture_kill[l] && (pass[0]||pass[1]||pass[2]||pass[3]))ctx.killed=true;\n            if(opcode==18){'),
)

MAIN = r'''
static void alpha(){
 PpuState s;init(s);auto& R=s.rsx.registers;auto& m=s.memory;
 R[0x1a0c]|=4;m.store(0xc0008000,4,0x00ff0000);
 auto t=decode(s);assert(t.alpha_kill);bool killed=false;
 eq(sample_texture(t,.5f,.5f,0,0,0,&killed),{1,0,0,0});assert(killed);
 bool failed=false;try{sample_texture(t,.5f,.5f);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("discard integration")!=std::string::npos;}assert(failed);
 m.store(0xc0008000,4,0x01ff0000);killed=false;
 near(sample_texture(decode(s),.5f,.5f,0,0,0,&killed)[3],1/255.f);assert(!killed);
 // Alpha remap ONE prevents kill despite a zero stored A; ZERO forces kill.
 m.store(0xc0008000,4,0x00ff0000);R[0x1a10]=0xa9e4;killed=false;
 eq(sample_texture(decode(s),.5f,.5f,0,0,0,&killed),{1,0,0,1});assert(!killed);
 R[0x1a10]=0xa8e4;killed=false;sample_texture(decode(s),.5f,.5f,0,0,0,&killed);assert(killed);
 // Filtered alpha, not each point tap: transparent + opaque -> .5 survives.
 init(s,0xa5,2,1);R[0x1a0c]|=4;R[0x1a14]=0x02020000;
 m.store(0xc0008000,4,0x00ff0000);m.store(0xc0008004,4,0xff0000ff);killed=false;
 eq(sample_texture(decode(s),.5f,.5f,0,0,0,&killed),{.5f,0,.5f,.5f});assert(!killed);
 // Test threshold with true fp32 alpha, including negative values and zero.
 init(s,0xbb);R[0x1a0c]|=4;R[0x1a08]|=8u<<20;
 for(unsigned i=0;i<3;++i)m.store(0xc0008000+i*4,4,float_bits(.5f));
 for(float a:{0.f,-0.f,-1.f,0.0000005f,0.000001f,0.000002f}){
  m.store(0xc000800c,4,float_bits(a));killed=false;
  sample_texture(decode(s),.5f,.5f,0,0,0,&killed);assert(killed==(a<0.000001f));
 }
 // Filtered UNORM alpha=2^-19 is above the threshold before gamma, below
 // it afterwards. This distinguishes the pipeline stage of alpha kill.
 init(s,0xa5,2,1);R[0x1a0c]|=4;R[0x1a08]|=8u<<20;R[0x1a14]=0x02020000;
 m.store(0xc0008000,4,0x00ffffff);m.store(0xc0008004,4,0xffffffff);killed=false;
 const float corrected=sample_texture(decode(s),.25f+0x1p-20f,.5f,0,0,0,&killed)[3];
 assert(corrected>0 && corrected<0.000001f && !killed);
 // BX2 makes a surviving sample negative, also after the kill check.
 init(s);R[0x1a0c]|=4;R[0x1a08]|=0x1000;m.store(0xc0008000,4,0x40ff0000);killed=false;
 assert(sample_texture(decode(s),.5f,.5f,0,0,0,&killed)[3]<0);assert(!killed);
 // Border alpha and a depth-comparison result are also post-filter samples.
 init(s);R[0x1a0c]|=4;R[0x1a08]=0x304;R[0x1a1c]=0x00ffffff;killed=false;
 sample_texture(decode(s),-1,.5f,0,0,0,&killed);assert(killed);
 init(s,0xb2);R[0x1a0c]|=4;R[0x1a08]|=1u<<28;m.store(0xc0008000,2,0x8000);killed=false;
 sample_texture(decode(s),.5f,.5f,0,.75f,0,&killed);assert(killed);
 // Vertex control does not enable fragment discard.
 init(s,0xbc);for(unsigned i=0;i<8;++i)R[0x900+i*4]=R[0x1a00+i*4];R[0x90c]|=4;
 assert(!decode_texture(s,Reg{s.rsx},0,true).alpha_kill);
 std::puts("PASS alpha threshold/remap/filter/order/border/shadow/vertex and missing integration stop");
}
static void aniso(){
 const unsigned levels[]={1,2,4,6,8,10,12,16};
 for(unsigned n=0;n<8;++n)for(unsigned bias=0;bias<16;++bias){
  PpuState s;init(s,0xa5,2,1,2);auto& R=s.rsx.registers;
  R[0x1a0c]|=n<<4;R[0x1a08]|=bias<<4;R[0x1a14]=0x02060000;
  s.memory.store(0xc0008000,4,0xffff0000);s.memory.store(0xc0008004,4,0xff0000ff);s.memory.store(0xc0008008,4,0xff00ff00);
  auto t=decode(s);assert(t.max_aniso==levels[n] && t.aniso_bias==bias);
  eq(sample_texture(t,.5f,.5f),{.5f,0,.5f,1});
  eq(sample_texture(t,.5f,.5f,.5f),{.25f,.5f,.25f,1});
  eq(sample_texture(t,.5f,.5f,1.f),{0,1,0,1});
 }
 std::puts("PASS all 128 aniso/bias encodings use documented isotropic filter/mip approximation");
}
static NativeRsx::Tile& tile(PpuState& s,unsigned i,unsigned off,unsigned bank=0,unsigned loc=0){
 auto& t=s.rsx.tiles[i];t.bound=true;t.location=loc;t.offset=off;t.size=0x10000;t.pitch=256;t.bank=bank;return t;
}
static void tiles(){
 PpuState s;init(s,0xa5,2,2);auto& R=s.rsx.registers;auto& m=s.memory;
 auto& a=tile(s,0,0x10000);R[0x1a00]=0x10020;R[0x1840]=32;
 // Hand offsets for tile base=0x10000, pitch=256, bank=0:
 // linear +0x20 -> +0x100, +0x40 -> +0x280. Texture row stride=32.
 m.store(0xc0010100,4,0xffff0000);m.store(0xc0010104,4,0xff00ff00);
 m.store(0xc0010280,4,0xff0000ff);m.store(0xc0010284,4,0xffffffff);
 auto t=decode(s);assert(t.untiled);eq(sample_texture(t,.25f,.25f),{1,0,0,1});eq(sample_texture(t,.75f,.25f),{0,1,0,1});
 eq(sample_texture(t,.25f,.75f),{0,0,1,1});eq(sample_texture(t,.75f,.75f),{1,1,1,1});
 // Morton is applied before the same tile permutation (not rejected).
 R[0x1a04]&=~0x2000u;R[0x1840]=0;
 m.store(0xc0010108,4,0xff0000ff);m.store(0xc001010c,4,0xffffffff);
 t=decode(s);eq(sample_texture(t,.25f,.75f),{0,0,1,1});eq(sample_texture(t,.75f,.75f),{1,1,1,1});
 // A BE 16-bit texel straddles two preserved 32-byte runs.
 init(s,0xa4);R[0x1a00]=0x1001f;m.store(0xc001001f,1,0xf8);m.store(0xc0010100,1,0);
 eq(sample_texture(decode(s),.5f,.5f),{1,0,0,1});
 // Wide Morton words: pixel (1,1) in a 2x2 RGBA32F image has
 // logical offsets 0x28,0x2c,0x38,0x3c -> physical 0x108,10c,118,11c.
 init(s,0x9b,2,2);R[0x1a00]=0x10000;
 const unsigned fp_at[]={0x10108,0x1010c,0x10118,0x1011c};
 const float fp_value[]={.5f,.25f,1.f,.125f};
 for(unsigned i=0;i<4;++i)m.store(0xc0000000+fp_at[i],4,float_bits(fp_value[i]));
 eq(sample_texture(decode(s),.75f,.75f),{.5f,.25f,1,.125f});
 // DXT payload stays little-endian through the tiler; one red DXT1 block.
 init(s,0xa6,4,4);R[0x1a00]=0x10020;m.store(0xc0010100,4,0x00f81f00);m.store(0xc0010104,4,0);
 eq(sample_texture(decode(s),.5f,.5f),{1,0,0,1});
 // Two adjacent tiles with different bank and pitch. The first row crosses
 // from row255/x224 at +0xfb60 to bank1 of the second tile at +0x4000.
 init(s,0xa5,16,1);R[0x1a00]=0x1ffe0;auto& b=tile(s,1,0x20000,1);b.pitch=512;
 m.store(0xc001fb60,4,0xffff0000);m.store(0xc001fb7c,4,0xff00ff00);
 m.store(0xc0024000,4,0xff0000ff);m.store(0xc002401c,4,0xffffffff);
 t=decode(s);eq(sample_texture(t,0,.5f),{1,0,0,1});eq(sample_texture(t,7.5f/16,.5f),{0,1,0,1});
 eq(sample_texture(t,8.5f/16,.5f),{0,0,1,1});eq(sample_texture(t,1,.5f),{1,1,1,1});
 // Complete second mip in another tile; level offsets stay texture offsets.
 init(s,0xa5,4,2,2);R[0x1a00]=0x1ffe0;R[0x1a14]=0x01030000;
 m.store(0xc0024000,4,0xff0000ff);m.store(0xc0024004,4,0xff0000ff);
 t=decode(s);eq(sample_texture(t,.5f,.5f,1),{0,0,1,1});
 // Untiled -> tiled -> untiled spans retain byte lanes, including sub-word
 // texels across 32-byte runs. B8 has fixed alpha=1.
 b.bound=false;init(s,0xa1,64,1);R[0x1a00]=0xffe0;
 m.store(0xc000ffff,1,17);m.store(0xc0010000,1,85);m.store(0xc001001f,1,221);
 t=decode(s);near(sample_texture(t,31.5f/64,.5f)[0],17/255.f);near(sample_texture(t,32.5f/64,.5f)[0],85/255.f);near(sample_texture(t,63.5f/64,.5f)[0],221/255.f);
 // Defined compression modes share expanded sample storage in this runtime.
 init(s);R[0x1a00]=0x10000;m.store(0xc0010000,4,0xff123456);
 for(unsigned comp:{0u,7u,8u,9u,10u,11u,12u}){a.compression=comp;eq(sample_texture(decode(s),.5f,.5f),{18/255.f,52/255.f,86/255.f,1});}
 std::puts("PASS fixed tiled offsets/subrect/pitch/Morton/mips/multiple tiles/gaps/compression model");
}
static void main_tiles(){
 PpuState s;init(s,0xa5,2,2);auto& R=s.rsx.registers;auto& m=s.memory;
 // Deliberately no mapping for the original logical start; only the actual
 // permuted source bytes are mapped. Full tile access would fail here.
 R[0x1a04]=(R[0x1a04]&~3u)|2;R[0x1a00]=0x10020;R[0x1840]=32;
 tile(s,0,0x10000,0,1);m.map(0x70010100,8,6,nullptr,0);m.map(0x70010280,8,6,nullptr,0);
 // Logical pitch=32 copies padding too; provide it independently of the
 // texels so no contiguous host access larger than a 32-byte run is needed.
 m.map(0x70010108,24,6,nullptr,0);m.map(0x70010288,24,6,nullptr,0);
 m.store(0x70010100,4,0xffff0000);m.store(0x70010104,4,0xff00ff00);
 m.store(0x70010280,4,0xff0000ff);m.store(0x70010284,4,0xffffffff);
 auto t=decode(s);eq(sample_texture(t,.25f,.25f),{1,0,0,1});eq(sample_texture(t,.75f,.75f),{1,1,1,1});
 const auto low=reinterpret_cast<std::uintptr_t>(t.storage);
 for(auto address:{0x70010100u,0x70010280u}){const auto host=reinterpret_cast<std::uintptr_t>(m.access(address,4,false));assert(host>=low && host+4<=low+t.storage_size);}
 // Two tiles across an IO page boundary mapped to discontiguous EAs. The
 // logical texture range and the entire tile have no contiguous mapping.
 init(s,0xa5,16,1);R[0x1a04]=(R[0x1a04]&~3u)|2;R[0x1a00]=0xfffe0;
 tile(s,0,0xf0000,0,1);tile(s,1,0x100000,1,1);s.rsx.io[1]=0x71000000;
 m.map(0x700ffb60,32,6,nullptr,0);m.map(0x71004000,32,6,nullptr,0);
 m.store(0x700ffb60,4,0xffff0000);m.store(0x71004000,4,0xff0000ff);
 t=decode(s);eq(sample_texture(t,0,.5f),{1,0,0,1});eq(sample_texture(t,8.5f/16,.5f),{0,0,1,1});
 std::puts("PASS main-memory tiled reads and alias envelope over separate guest backings");
}
static void errors(){
 for(unsigned n=0;n<8;++n){
  PpuState s;init(s);s.rsx.registers[0x1a00]=0x10000;auto& a=tile(s,0,0x10000);
  if(n==0)tile(s,1,0x10000); // ambiguous overlap
  if(n==1)a.compression=6;
  if(n==2)a.pitch=0;
  if(n==3)a.bank=4;
  if(n==4){s.rsx.registers[0x1a00]=0x7fffff;s.rsx.registers[0x1a18]=0x10002;}
  if(n==5)a.size=4,a.bank=1; // permutation leaves configured tile
  if(n==6){a.location=1;s.rsx.registers[0x1a04]=(s.rsx.registers[0x1a04]&~3u)|2;} // actual source unmapped
  if(n==7)s.memory.regions[1].flags=0; // source is not readable
  const auto before=*s.memory.regions[1].bytes;bool failed=false;
  try{decode(s);}catch(const std::runtime_error&){failed=true;}
  assert(failed);assert(before==*s.memory.regions[1].bytes);
 }
 std::puts("PASS explicit overlap/compression/pitch/bank/aperture/span stops with no guest writes");
}
static void draw_alpha(){
 // Execute TEX, TXP, TXD, TXL, TXB against a transparent texture; all lanes
 // discard. The sample is still evaluated when the write mask is zero.
 for(unsigned op:{23u,24u,25u,47u,49u})for(unsigned mask:{0u,15u}){
  PpuState s;init(s);s.rsx.registers[0x1a0c]|=4;s.memory.store(0xc0008000,4,0x00ff0000);
  std::array<Texture,16> tex{};tex[0]=decode(s);std::array<FpContext,4> q{};
  for(auto& c:q){c.textures=&tex;c.inputs[4]={.5f,.5f,0,1};}
  FpInst inst{};inst.d=(op<<24)|(mask<<9)|(4u<<13)|1;inst.s0=1|(0xe4u<<9)|(7u<<18);
  run_fragment_quad_linear({inst},q);for(const auto& c:q)assert(c.killed);
  // Conditional false lanes and inactive lanes must remain alive.
  for(auto& c:q)c.killed=false;
  inst.s0=1|(0xe4u<<9)|(4u<<18); // GT, all-zero condition register => false
  run_fragment_quad_linear({inst},q);for(const auto& c:q)assert(!c.killed);
  inst.s0=1|(0xe4u<<9)|(7u<<18);run_fragment_quad_linear({inst},q,1);
  assert(q[0].killed);for(unsigned l=1;l<4;++l)assert(!q[l].killed);
 }
 // Draw-level integration: alpha kill prevents both color and depth writes.
 for(unsigned threads:{1u,4u}){
  PpuState s;setup_draw(s,160,140,640,false);init(s);auto& R=s.rsx.registers;auto& m=s.memory;
  R[0x1a0c]|=4;m.store(0xc0008000,4,0x00ff0000);R[0xa74]=R[0xa70]=1;R[0xa6c]=0x207;
  const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1|(0xe4u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)m.store(0xc0001000+i*4,4,(words[i]<<16)|(words[i]>>16));
  setenv("ANYPS3_RSX_THREADS",threads==1?"1":"4",1);rsx_execute_draw(s);
  for(unsigned i=0;i<160*140;++i){assert(m.load(0xc0100000+i*4,4)==0);assert(m.load(0xc0500000+i*4,4)==0);}
  m.store(0xc0008000,4,0xffff0000);s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);
  assert(m.load(0xc0100000,4)==0xffff0000);assert(m.load(0xc0500000,4)==0x40000000);
 }
 std::puts("PASS test-only fragment discard integration, 5 TEX opcodes/predicates/helpers/color/depth/parallel");
}
int main(int argc,char** argv){assert(argc==2);const std::string mode=argv[1];
 if(mode=="alpha")alpha();else if(mode=="aniso")aniso();else if(mode=="tiles")tiles();else if(mode=="main")main_tiles();else if(mode=="draw")draw_alpha();else errors();}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class RsxTexture3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='rsxtex3-')
        root = Path(cls.tmp.name)
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        for before, after in DRAW_INTEGRATION:
            # Accept real integration when the owner has already applied it.
            if after in source:
                continue
            if source.count(before) != 1:
                raise AssertionError('Draw integration location changed: ' + before)
            source = source.replace(before, after, 1)
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        harness = HARNESS.split('static void observe(')[0].replace('GAME_VP_WORDS', ','.join(f'{w}u' for w in GAME_VP))
        helpers = TEXTURE_HELPERS.split('static void signed_remap(')[0]
        (root/'test.cpp').write_text(source+'\n#include <cassert>\n'+harness+helpers+MAIN)
        build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-pthread','-o',str(root/'test')],capture_output=True,text=True,timeout=120)
        if build.returncode:
            cls.tmp.cleanup()
            raise AssertionError(build.stderr)
        cls.exe = root/'test'

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, mode):
        run = subprocess.run([str(self.exe),mode],capture_output=True,text=True,timeout=45,env={**os.environ,'ANYPS3_RSX_THREADS':'1'})
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertIn('PASS',run.stdout)

    def test_alpha_kill_sample_stage(self): self.run_case('alpha')
    def test_isotropic_aniso_approximation(self): self.run_case('aniso')
    def test_tiled_layouts_and_expanded_compression(self): self.run_case('tiles')
    def test_main_memory_tiles_and_aliases(self): self.run_case('main')
    def test_explicit_errors_preserve_memory(self): self.run_case('errors')
    def test_fragment_discard_integration(self): self.run_case('draw')
