"""Fixed physical Morton/MRT/primitive oracles; no generated reference pixels."""
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
static void setup(PpuState& s,unsigned w=8,unsigned h=4){setup_draw(s,w,h,w*4,false);}
static void redraw(PpuState& s){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
static void constant(PpuState& s,const Vec4& c){for(unsigned i=0;i<4;++i){const auto w=float_bits(c[i]);s.memory.store(0xc0001010+i*4,4,(w<<16)|(w>>16));}}
static const unsigned morton[4][8]={{0,1,4,5,16,17,20,21},{2,3,6,7,18,19,22,23},{8,9,12,13,24,25,28,29},{10,11,14,15,26,27,30,31}};
// Hand-calculated physical bytes for expanded 32-bit elements (not pixel Morton).
static unsigned physical(unsigned x,unsigned y,unsigned bpp,unsigned byte){
 if(bpp<=4)return morton[y][x]*bpp+byte;
 const unsigned row[]={0,8,32,40};
 if(bpp==8){const unsigned col[]={0,16,64,80,128,144,192,208};return col[x]+row[y]+byte;}
 const unsigned word[]={0,4,16,20};return 64*x+row[y]+word[byte/4]+byte%4;
}
static void swizzle(){
 struct E{unsigned f,bpp;std::array<unsigned char,16> bytes;};
 const E cases[]={
 {1,2,{0x7e,8}},{2,2,{0xfe,8}},{3,2,{0xfc,8}},{4,4,{0,255,128,64}},{5,4,{255,255,128,64}},
 {8,4,{128,255,128,64}},{9,1,{64}},{10,2,{128,64}},{11,8,{0x3c,0,0x38,0,0x34,0,0x38,0}},
 {12,16,{0x3f,0x80,0,0,0x3f,0,0,0,0x3e,0x80,0,0,0x3f,0,0,0}},{13,4,{0x3f,0x80,0,0}},
 {14,4,{0,64,128,255}},{15,4,{255,64,128,255}},{16,4,{128,64,128,255}}};
 for(const auto& e:cases){
  PpuState s;setup(s);auto& R=s.rsx.registers;
  R[0x208]=0x02030240|e.f;R[0x20c]=3; // deliberately invalid linear pitch; ignored for swizzle
  std::fill_n(s.memory.access(0xc0100000,32*e.bpp+32,true),32*e.bpp+32,0xa5);
  for(unsigned y=0;y<4;++y)for(unsigned x=0;x<8;++x){
   R[0x8c0]=0x10000|x;R[0x8c4]=0x10000|y;redraw(s);
   for(unsigned b=0;b<e.bpp;++b)assert(s.memory.load(0xc0100000+physical(x,y,e.bpp,b),1)==e.bytes[b]);
  }
  for(unsigned b=0;b<32;++b)assert(s.memory.load(0xc0100000+32*e.bpp+b,1)==0xa5);
  // Draw -> original swizzled texture reader, including noncontiguous fp32 pixels.
  unsigned tex=e.f==3?0x84:e.f==8?0x85:e.f==11?0x9a:e.f==12?0x9b:e.f==13?0x9c:0;
  if(tex){R[0x1a00]=0x100000;R[0x1a04]=0x10029|(tex<<8);R[0x1a08]=0x303;R[0x1a0c]=0x80000000;
   R[0x1a10]=0xaae4;R[0x1a14]=0x02020000;R[0x1a18]=0x80004;R[0x1840]=3;
   const auto t=decode_texture(s,Reg{s.rsx},0);
   for(unsigned y=0;y<4;++y)for(unsigned x=0;x<8;++x){const auto c=texel(t,t.levels[0],x,y);
    assert(c[0]==1);if(tex==0x9c)continue;
    assert(std::fabs(c[1]-(tex==0x84?32.f/63:tex==0x85?128.f/255:.5f))<1.e-6f);
    assert(std::fabs(c[2]-(tex==0x84?8.f/31:tex==0x85?64.f/255:.25f))<1.e-6f);
   }
  }
 }
 // Smaller clip still addresses the full log2 surface stride.
 {PpuState s;setup(s,2,2);s.rsx.registers[0x208]=0x02030248;redraw(s);
  assert(s.memory.load(0xc0100008,4)==0x80ff8040);assert(s.memory.load(0xc0100020,4)==0);}
 std::puts("PASS hand Morton addresses, 14 formats, wide blocks, ignored pitch, draw->texture");
}
static void depth(){
 for(unsigned f:{1u,2u}){
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x02030208|(f<<5);R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;
  const unsigned bpp=f==1?2:4;R[0x22c]=0;
  for(unsigned i=0;i<32;++i)s.memory.store(0xc0500000+i*bpp,bpp,f==1?0xffff:0xffffff5a);
  if(f==1)R[0x328]=1; // Z16 has no stencil; incomplete stencil state is irrelevant.
  redraw(s);
  for(unsigned y=0;y<4;++y)for(unsigned x=0;x<8;++x)
   assert(s.memory.load(0xc0500000+morton[y][x]*bpp,bpp)==(f==1?0x4000:0x4000005a));
  constant(s,{0,0,0,0});redraw(s); // LESS rejects equality
  assert(s.memory.load(0xc0100000,4)==0x80ff8040);
 }
 {PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x02030228;R[0xa70]=1;R[0xa68]=1;R[0xa7c]=float_bits(1);redraw(s);
  assert(s.memory.load(0xc0500000,2)==0x4001);}
 std::puts("PASS swizzled Z16/Z24S8 fixed BE values, LESS equality and no Z16 stencil");
}
static void msaa_and_stop291(){
 {PpuState s;setup(s,128,128);auto& R=s.rsx.registers;R[0x208]=0x07070223;R[0x20c]=R[0x22c]=1;
  R[0xa74]=R[0xa70]=1;R[0xa6c]=0x207;redraw(s);
  for(unsigned index:{0u,3u,14u,16383u}){assert(s.memory.load(0xc0100000+index*2,2)==0xfc08);assert(s.memory.load(0xc0500000+index*2,2)==0x4000);}
 }
 for(unsigned aa:{3u,4u,5u}){
  PpuState s;setup(s,2,2);auto& R=s.rsx.registers;R[0x208]=0x01010223|(aa<<12);R[0x20c]=R[0x22c]=1;
  R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;
  const float pos[3][2]={{0,0},{1,0},{0,1}};
  for(unsigned i=0;i<3;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002000+i*16+c*4,4,float_bits(pos[i][c]));
  const unsigned nx=2,ny=aa==3?1:2;
  for(unsigned i=0;i<4*nx*ny;++i)s.memory.store(0xc0500000+i*2,2,0xffff);
  s.rsx.draw.primitive=5;s.rsx.draw.ranges={{0,3}};rsx_execute_draw(s);
  for(unsigned y=0;y<2*ny;++y)for(unsigned x=0;x<4;++x){
   const bool hit=aa==3?x==1&&y==0:aa==4?x==0&&y==0:x==0&&y<2;
   assert(s.memory.load(0xc0100000+morton[y][x]*2,2)==(hit?0xfc08:0));
   assert(s.memory.load(0xc0500000+morton[y][x]*2,2)==(hit?0x4000:0xffff));
  }
 }
 std::puts("PASS exact draw state 291 and swizzled 2x/4x Z16 per-sample coverage");
}
static void outputs(PpuState& s,bool half){
 const unsigned dest[]={0,2,3,4};const Vec4 colors[]={{1,0,0,1},{0,1,0,.5f},{0,0,1,.25f},{1,1,1,0}};
 unsigned at=0;auto word=[&](unsigned w){s.memory.store(0xc0001000+4*at++,4,(w<<16)|(w>>16));};
 for(unsigned i=0;i<4;++i){word((1u<<24)|(15u<<9)|(dest[i]<<(half?2:1))|(half?128u:0u)|(i==3));word(2|(0xe4u<<9)|(7u<<18));word(0);word(0);
  for(auto c:colors[i])word(float_bits(c));}
 s.rsx.registers[0x1d60]=half?0:0x40;
}
static void mrt(){
 for(unsigned mode:{1u,2u,0x13u,0x17u,0x1fu})for(bool half:{false,true})for(bool swz:{false,true}){
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x220]=mode;outputs(s,half);
  const unsigned n=mode<3?1:mode==0x13?2:mode==0x17?3:4;
  R[0x18c]=R[0x1b4]=R[0x1b8]=0xfeed0000;R[0x218]=0x200000;R[0x288]=0x300000;R[0x28c]=0x400000;
  R[0x21c]=R[0x280]=R[0x284]=32;R[0x370]=0xfff0;
  if(swz)R[0x208]=0x02030248;
  redraw(s);const unsigned golden[]={0xffff0000,0x8000ff00,0x400000ff,0x00ffffff};
  for(unsigned i=0;i<n;++i){const unsigned address=0xc0100000+((mode==2?1:i)*0x100000);
   for(unsigned p=0;p<32;++p)assert(s.memory.load(address+4*p,4)==golden[i]);}
  // Separate masks and blend enables by output slot; B-only uses R0 and primary state.
  R[0x324]=0x100;R[0x370]=0x8420;R[0x310]=0;R[0x36c]=2;R[0x314]=0;R[0x318]=0x00010001;R[0x320]=0x80068006;
  for(unsigned i=0;i<n;++i){const auto a=0xc0100000+((mode==2?1:i)*0x100000);for(unsigned p=0;p<32;++p)s.memory.store(a+4*p,4,0x11223344);}
  redraw(s);const unsigned masked[]={0x11220044,0x11223344,0x11220044,0x112233ff};
  for(unsigned i=0;i<n;++i)assert(s.memory.load(0xc0100000+((mode==2?1:i)*0x100000),4)==masked[i]);
 }
 std::puts("PASS A/B-only/AB/ABC/ABCD, R0/R2/R3/R4 and H0/H4/H6/H8, masks and blend");
}
static void vertices(PpuState& s,const std::vector<std::array<float,2>>& points){
 for(unsigned i=0;i<points.size();++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002000+16*i+4*c,4,float_bits(points[i][c]));
 s.rsx.draw.ranges={{0,unsigned(points.size())}};
}
static void primitives(){
 for(unsigned mode:{2u,3u,4u}){
  PpuState s;setup(s,8,8);auto& R=s.rsx.registers;R[0x183c]=1;R[0x1830]=0x408;R[0x3b8]=8;
  s.rsx.draw.primitive=mode;vertices(s,{{1.5,2.5},{5.5,2.5},{5.5,5.5}});rsx_execute_draw(s);
  for(unsigned x=0;x<8;++x)assert(s.memory.load(0xc0100000+(2*8+x)*4,4)==((x>=1&&x<5)||(mode!=2&&x==5)?0x80ff8040:0));
  if(mode!=2)for(unsigned y=3;y<5;++y)assert(s.memory.load(0xc0100000+(y*8+5)*4,4)==0x80ff8040);
 }
 // A diagonal line has exactly one diamond exit per row and excludes the end.
 {PpuState s;setup(s,8,8);s.rsx.draw.primitive=2;vertices(s,{{1.5,1.5},{5.5,5.5}});rsx_execute_draw(s);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(s.memory.load(0xc0100000+(y*8+x)*4,4)==(x==y&&x>=1&&x<5?0x80ff8040:0));}
 // Reversed lines retain their own final endpoint exclusion.
 {PpuState s;setup(s,8,8);s.rsx.draw.primitive=2;vertices(s,{{5.5,2.5},{1.5,2.5}});rsx_execute_draw(s);
  for(unsigned x=0;x<8;++x)assert(s.memory.load(0xc0100000+(2*8+x)*4,4)==(x>=2&&x<=5?0x80ff8040:0));}
 // Clipping the outgoing endpoint must not reverse the surviving segment.
 {PpuState s;setup(s,8,8);s.rsx.draw.primitive=2;vertices(s,{{1.5,2.5},{5.5,2.5}});
  s.memory.store(0x70002008,4,float_bits(0));s.memory.store(0x70002018,4,float_bits(2));rsx_execute_draw(s);
  for(unsigned x=0;x<8;++x)assert(s.memory.load(0xc0100000+(2*8+x)*4,4)==(x>=1&&x<3?0x80ff8040:0));}
 for(unsigned width:{3u,4u}){
  PpuState s;setup(s,8,8);s.rsx.registers[0x3b8]=width*8;s.rsx.draw.primitive=2;vertices(s,{{1.5,2.5},{5.5,2.5}});rsx_execute_draw(s);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){const bool hit=x>=1&&x<5&&y>=(width==3?1u:0u)&&y<4;
   assert(s.memory.load(0xc0100000+(y*8+x)*4,4)==(hit?0x80ff8040:0));}
 }
 // Smooth interpolation uses projection onto the line, not its expansion quad.
 {PpuState s;setup(s,8,8);s.rsx.draw.primitive=2;vertices(s,{{1.5,2.5},{5.5,2.5}});
  s.memory.store(0x70002100,4,0);s.memory.store(0x70002104,4,0);s.memory.store(0x70002108,4,float_bits(1));s.memory.store(0x7000210c,4,0);
  const unsigned words[]={(1u<<24)|(15u<<9)|(4u<<13)|1u,1|(0xe4u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)s.memory.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
  rsx_execute_draw(s);const unsigned red[]={0,64,128,191};
  for(unsigned x=1;x<5;++x)assert(s.memory.load(0xc0100000+(2*8+x)*4,4)==(0xff000000|(red[x-1]<<16)));
 }
 // Point sprite TEX0 replacement supplies normalized window coordinates.
 {PpuState s;setup(s,8,8);auto& R=s.rsx.registers;R[0x1ff4]=0;R[0x1ee0]=float_bits(4);R[0x1ee8]=0x101;
  const unsigned words[]={(1u<<24)|(15u<<9)|(4u<<13)|1u,1|(0xe4u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)s.memory.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
  s.rsx.draw.primitive=1;vertices(s,{{4,4}});rsx_execute_draw(s);
  assert(s.memory.load(0xc0100000+(2*8+2)*4,4)==0x002020ff);assert(s.memory.load(0xc0100000+(5*8+5)*4,4)==0x00dfdfff);
 }
 // Dither enable on a 16-bit target: quantized without dither (RPCS3 VK never
 // applies it), so the bytes equal those of the same draw with dither off.
 {PpuState a,b;setup(a);setup(b);a.rsx.registers[0x208]=b.rsx.registers[0x208]=0x143;a.rsx.registers[0x300]=1;b.rsx.registers[0x300]=0;
  rsx_execute_draw(a);rsx_execute_draw(b);
  for(unsigned i=0;i<8*8*2;i+=2)assert(a.memory.load(0xc0100000+i,2)==b.memory.load(0xc0100000+i,2));
  assert(a.rsx.draw.pixels==b.rsx.draw.pixels && a.rsx.draw.pixels);}
 // Zero surface clip: invalid framebuffer, the draw writes nothing.
 {PpuState z;setup(z);const auto before=z.memory.load(0xc0100000,4);z.rsx.registers[0x200]&=0xffff;
  rsx_execute_draw(z);assert(z.memory.load(0xc0100000,4)==before && !z.rsx.draw.pixels && z.rsx.draw.ranges.empty());}
 std::puts("PASS aliased line masks and endpoint exclusion, point sprite, dither-off quantization, zero clip");
}
static void changed(){
 // Correct gamma mask bit0=R. A constant texel gives independent fixed RGB bytes.
 {PpuState s;setup(s);auto& R=s.rsx.registers;
  s.memory.store(0xc0008000,4,0xff808080);R[0x1a00]=0x8000;R[0x1a04]=0x1a529;R[0x1a08]=0x100303;
  R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x02020000;R[0x1a18]=0x10001;R[0x1840]=4;
  const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1|(0xe4u<<9)|(7u<<18),0,0};
  for(unsigned i=0;i<4;++i)s.memory.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
  redraw(s);assert(s.memory.load(0xc0100000,4)==0xff378080);
 }
 {PpuState s;setup(s);auto& R=s.rsx.registers;auto& tile=s.rsx.tiles[0];tile.bound=true;tile.offset=0x100000;tile.size=0x100000;tile.pitch=256;
  R[0x1a00]=0x100000;R[0x1a04]=0x1a529;R[0x1a08]=0x303;R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;
  R[0x1a14]=0x02020000;R[0x1a18]=0x80008;R[0x1840]=32;
  // Hand offsets for the eight 32-byte texture rows within a pitch-256 tile.
  // Texture pitch stays 32; it must not be replaced with the tile pitch.
  const unsigned rows[]={0,0x100,0x280,0x380,0x400,0x500,0x680,0x780};
  const unsigned packed[]={0xffff0000,0xff00ff00,0xff0000ff,0xffffffff,0x00000000,0xffffff00,0xffff00ff,0xff00ffff};
  const Vec4 colors[]={{1,0,0,1},{0,1,0,1},{0,0,1,1},{1,1,1,1},{0,0,0,0},{1,1,0,1},{1,0,1,1},{0,1,1,1}};
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)s.memory.store(0xc0100000+rows[y]+4*x,4,packed[(x+y)%8]);
  const auto before=*s.memory.regions[1].bytes;const auto versions=s.memory.write_versions;
  const auto t=decode_texture(s,Reg{s.rsx},0);assert(t.untiled);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(texel(t,t.levels[0],x,y)==colors[(x+y)%8]);
  assert(before==*s.memory.regions[1].bytes);assert(versions==s.memory.write_versions);
 }
 std::puts("PASS corrected gamma bytes and fixed tiled texels with differing pitch/no writes");
}
static void validation(){
 for(unsigned n=0;n<5;++n){
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x02030248;
  const char* error="";
  if(n==0){R[0x208]=0x02100248;error="invalid RSX swizzled draw dimensions";}
  if(n==1){R[0x208]=0x02010248;error="RSX draw clip exceeds swizzled surface dimensions";}
  if(n==2){R[0x2b8]=1;error="unsupported RSX swizzled draw window offset";}
  if(n==3){R[0x220]=3;error="unsupported RSX draw color target set 3";}
  if(n==4){R[0x220]=0x13;error="unsupported RSX draw surface DMA context";}
  const auto before=*s.memory.regions[1].bytes;std::string actual;
  try{redraw(s);}catch(const std::runtime_error& e){actual=e.what();}
  assert(actual==error);assert(before==*s.memory.regions[1].bytes);
 }
 {PpuState s;setup(s,256,128);auto& R=s.rsx.registers;R[0x208]=0x07080248;R[0x220]=0x13;
  R[0x18c]=0xfeed0000;R[0x218]=R[0x210];R[0x21c]=1;R[0x370]=0xf0;outputs(s,false);
  const auto before=test_dispatches;setenv("ANYPS3_RSX_THREADS","4",1);redraw(s);assert(test_dispatches==before);
  assert(s.memory.load(0xc0100000,4)==0x8000ff00); // ordered alias writes: B after A
 }
 {PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x0203024c;
  s.memory.reservation_token(0xc0100000);const auto versions=s.memory.write_versions;
  redraw(s);assert(s.memory.write_versions.size()==versions.size());
  for(const auto& item:versions){
   const auto found=std::find_if(s.memory.write_versions.begin(),s.memory.write_versions.end(),[&](const auto& entry){return entry.first==item.first;});
   assert(found!=s.memory.write_versions.end() && found->second>item.second);
  }
  assert(s.memory.load(0xc0100000,4)==0x3f800000);
 }
 std::puts("PASS swizzle/MRT bounds and exact errors, unchanged error buffers, alias order and physical observer keys");
}
static void parallel(){
 for(unsigned format:{3u,8u,11u,12u})for(unsigned target:{1u,2u,0x1fu}){
  PpuState s;setup(s,256,128);auto& R=s.rsx.registers;R[0x208]=0x07080240|format;R[0x220]=target;
  R[0x18c]=R[0x1b4]=R[0x1b8]=0xfeed0000;R[0x218]=0x200000;R[0x288]=0x300000;R[0x28c]=0x400000;
  R[0x21c]=R[0x280]=R[0x284]=1;R[0x370]=0xfff0;R[0xa70]=R[0xa74]=1;R[0xa6c]=0x207;R[0x22c]=1;
  outputs(s,false);const auto initial=*s.memory.regions[1].bytes;const auto draw=s.rsx.draw;
  setenv("ANYPS3_RSX_THREADS","1",1);rsx_execute_draw(s);const auto expected=*s.memory.regions[1].bytes;const auto count=s.rsx.draw;
  *s.memory.regions[1].bytes=initial;s.rsx.draw=draw;setenv("ANYPS3_RSX_THREADS","4",1);const auto before=test_dispatches;rsx_execute_draw(s);
  assert(test_dispatches==before+1);assert(expected==*s.memory.regions[1].bytes);assert(count.pixels==s.rsx.draw.pixels&&count.triangles==s.rsx.draw.triangles);
 }
 std::puts("PASS 12 swizzle/MRT/float/Z24 actual 4-worker replays, complete buffers and counts");
}
int main(int argc,char** argv){assert(argc==2);const std::string mode=argv[1];
 if(mode=="swizzle")swizzle();else if(mode=="depth")depth();else if(mode=="mrt")mrt();else if(mode=="primitives")primitives();else if(mode=="changed")changed();else if(mode=="validation")validation();else if(mode=="msaa")msaa_and_stop291();else parallel();}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class RsxSwizzleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='rsxswz-')
        root = Path(cls.tmp.name)
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source = source.replace('namespace rsxdraw {', 'static unsigned test_dispatches=0;\nnamespace rsxdraw {', 1)
        source = 'static thread_local bool test_rsx_worker=false;\n' + source
        source = source.replace('counts[i]=rasterize(first*2,last*2,true);', 'test_rsx_worker=true;counts[i]=rasterize(first*2,last*2,true);test_rsx_worker=false;')
        for signature in ['void mark_write(const std::uint8_t* pointer,unsigned size) {', 'std::uint8_t* access(std::uint64_t address, unsigned size, bool write) {']:
            assert signature in source
            source = source.replace(signature,signature+'if(test_rsx_worker)std::abort();')
        source = source.replace('static thread_local DrawPool pool;', '++test_dispatches;static thread_local DrawPool pool;')
        harness = HARNESS.split('static void observe(')[0].replace('GAME_VP_WORDS', ','.join(f'{w}u' for w in GAME_VP))
        (root/'test.cpp').write_text(source+'\n#include <cassert>\n'+harness+MAIN)
        build = subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-pthread','-o',str(root/'test')], capture_output=True,text=True,timeout=120)
        if build.returncode:
            cls.tmp.cleanup()
            raise AssertionError(build.stderr)
        cls.exe = root/'test'

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_case(self, mode):
        run = subprocess.run([str(self.exe),mode],capture_output=True,text=True,timeout=60,env={**os.environ,'ANYPS3_RSX_THREADS':'1'})
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertIn('PASS',run.stdout)

    def test_swizzle_formats_and_texture(self): self.run_case('swizzle')
    def test_swizzle_depth(self): self.run_case('depth')
    def test_mrt_exports_masks_and_blend(self): self.run_case('mrt')
    def test_lines_points_and_dither(self): self.run_case('primitives')
    def test_corrected_legacy_semantics(self): self.run_case('changed')
    def test_parallel_swizzle_mrt(self): self.run_case('parallel')

    def test_stop291_and_swizzled_msaa(self): self.run_case('msaa')
    def test_validation_aliases_and_observers(self): self.run_case('validation')
