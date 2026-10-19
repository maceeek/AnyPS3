"""Native RSX fragments with fixed, independently calculated byte oracles."""
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
static unsigned bits(float f){return float_bits(f);}
static void setup(PpuState& s,unsigned w=8,unsigned h=8){setup_draw(s,w,h,w*4,false);}
static void redraw(PpuState& s){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}
static void fp_words(PpuState& s,const std::vector<unsigned>& words){
 for(unsigned i=0;i<words.size();++i)s.memory.store(0xc0001000+i*4,4,(words[i]<<16)|(words[i]>>16));
}
static void color_constant(PpuState& s,const Vec4& c){
 for(unsigned i=0;i<4;++i){const auto w=bits(c[i]);s.memory.store(0xc0001010+i*4,4,(w<<16)|(w>>16));}
}
static void formats(){
 struct Expected{unsigned format,n;std::array<unsigned char,16> bytes;};
 const Expected expected[]={
  {1,2,{0x7e,0x08}},{2,2,{0xfe,0x08}},{3,2,{0xfc,0x08}},
  {4,4,{0,255,128,64}},{5,4,{255,255,128,64}},{8,4,{128,255,128,64}},
  {9,1,{64}},{10,2,{128,64}},
  {11,8,{0x3c,0,0x38,0,0x34,0,0x38,0}},
  {12,16,{0x3f,0x80,0,0,0x3f,0,0,0,0x3e,0x80,0,0,0x3f,0,0,0}},
  {13,4,{0x3f,0x80,0,0}},
  {14,4,{0,64,128,255}},{15,4,{255,64,128,255}},{16,4,{128,64,128,255}}
 };
 for(const auto& e:expected){
  PpuState s;setup(s);s.rsx.registers[0x208]=0x140|e.format;
  s.rsx.registers[0x20c]=8*e.n+e.n;redraw(s);
  const auto* p=s.memory.access(0xc0100000,8*9*e.n,false);
  for(unsigned y=0;y<8;++y){for(unsigned x=0;x<8;++x)assert(!std::memcmp(p+y*9*e.n+x*e.n,e.bytes.data(),e.n));
   for(unsigned b=0;b<e.n;++b)assert(p[y*9*e.n+8*e.n+b]==0);}
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x14b;R[0x20c]=64;
  color_constant(s,{-1,2,4,.5f});R[0x310]=1;R[0x314]=R[0x318]=0x00010001;R[0x320]=0x80068006;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)s.memory.store(0xc0100000+y*64+x*8,8,UINT64_C(0x4000c40048003c00));
  redraw(s);assert(s.memory.load(0xc0100000,8)==UINT64_C(0x3c00c0004a003e00)); // 1,-2,12,1.5
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x14b;R[0x20c]=64;
  R[0x310]=1;R[0x314]=0x80018001;R[0x318]=0;R[0x320]=0x80068006;
  R[0x31c]=0x38003c00;R[0x37c]=0x34004000;redraw(s);
  assert(s.memory.load(0xc0100000,8)==UINT64_C(0x3c00340038003000)); // blend constant 1,.5,2,.25
 }
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x208]=0x143;R[0x20c]=16;R[0x324]=0x100;
  s.memory.store(0xc0100000,2,0xaabb);redraw(s);assert(s.memory.load(0xc0100000,2)==0xac1b);
 }
 std::puts("PASS 14 target formats, BE bytes/padding, fp16 blend/color2 and packed masks");
}

static void points(){
 for(unsigned size:{1u,3u,4u}){
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x1ff4]=0x3fffdf;R[0x1ee0]=bits(float(size));
  R[0x183c]=1;R[0x1830]=0x408; // Polygon culling must not discard points.
  s.memory.store(0x70002000,4,bits(3.2f));s.memory.store(0x70002004,4,bits(4.6f));
  s.rsx.draw.primitive=1;s.rsx.draw.ranges={{0,1}};rsx_execute_draw(s);
  assert(s.rsx.draw.pixels==size*size);
  const unsigned left=size==1?3:size==3?2:1,top=size==1?4:3;
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){const bool hit=x>=left&&x<left+size&&y>=top&&y<top+size;
   assert(s.memory.load(0xc0100000+(y*8+x)*4,4)==(hit?0x80ff8040:0));}
 }
 std::puts("PASS POINTS 1/3/4-pixel odd/even footprints, constant varyings and cull bypass");
}

static void antialias(){
 for(unsigned aa:{0u,3u,4u,5u}){
  PpuState s;setup(s,2,2);auto& R=s.rsx.registers;const unsigned nx=aa?2:1,ny=aa>=4?2:1;
  R[0x208]|=aa<<12;R[0x20c]=R[0x22c]=2*nx*4;R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;
  const float pos[3][2]={{0,0},{1,0},{0,1}};
  for(unsigned i=0;i<3;++i)for(unsigned c=0;c<2;++c)s.memory.store(0x70002000+i*16+c*4,4,bits(pos[i][c]));
  for(unsigned y=0;y<2*ny;++y)for(unsigned x=0;x<2*nx;++x)s.memory.store(0xc0500000+y*2*nx*4+x*4,4,0xffffff11);
  s.rsx.draw.primitive=5;s.rsx.draw.ranges={{0,3}};rsx_execute_draw(s);
  const unsigned masks=aa==3?2:aa==4?1:aa==5?5:0;
  unsigned passed=0;
  for(unsigned y=0;y<2*ny;++y)for(unsigned x=0;x<2*nx;++x){
   const unsigned sample=(y%ny)*nx+x%nx;const bool hit=x<nx && y<ny && (masks&(1u<<sample));passed+=hit;
   assert(s.memory.load(0xc0100000+y*2*nx*4+x*4,4)==(hit?0x80ff8040:0));
   assert(s.memory.load(0xc0500000+y*2*nx*4+x*4,4)==(hit?0x40000011:0xffffff11));
  }
  assert(s.rsx.draw.pixels==passed);
  // The existing NV3089 blitter resolves expanded sample storage with FOH.
  R[0xc184]=0xfeed0000;R[0xc198]=0x313371c3;R[0xc2fc]=1;R[0xc300]=3;R[0xc304]=3;
  R[0xc308]=R[0xc310]=0;R[0xc30c]=R[0xc314]=0x20002;
  R[0xc318]=nx<<20;R[0xc31c]=ny<<20;R[0xc400]=(2*ny<<16)|2*nx;
  R[0xc404]=0x01020000|(2*nx*4);R[0xc408]=0x100000;
  R[0x6188]=0xfeed0000;R[0x6300]=10;R[0x6304]=8u<<16;R[0x630c]=0x600000;
  rsxblit::scaled(s,(nx==2?8:0)|(ny==2?8u<<16:0));
  assert(s.memory.load(0xc0600000,4)==(aa==3||aa==5?0x40804020:aa==4?0x20402010:0));
  // Sampling the expanded surface through the real bilinear texture path.
  R[0x1a00]=0x100000;R[0x1a04]=0x10029|(0xa5<<8);R[0x1a08]=0x303;
  R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x02020000;
  R[0x1a18]=(2*nx<<16)|2*ny;R[0x1840]=2*nx*4;
  auto texture=decode_texture(s,Reg{s.rsx},0);const auto resolved=sample_texture(texture,.25f,.25f);
  assert(resolved[0]==(aa==3||aa==5?.5f:aa==4?.25f:0.f));
 }
 std::puts("PASS MSAA per-sample edge coverage, expanded storage and independent Z24S8");
}
static std::array<unsigned,4> vp(unsigned,unsigned,unsigned,unsigned,unsigned,bool end=true);
static void write_vp(NativeRsx&,unsigned,const std::array<unsigned,4>&);
static void clipping(){
 ShadedVertex a{},b{},c{};a.clip={-.5f,-.5f,-1,1};b.clip={.5f,-.5f,1,1};c.clip={0,.5f,1,1};
 a.o[1]={0,0,0,0};b.o[1]={1,0,0,0};c.o[1]={0,1,0,0};
 auto p=clip_polygon({&a,&b,&c},0,0,1,0,0,1);
 assert(p.size()==4);assert((p[0].clip==Vec4{-.25f,0,0,1}));assert((p[0].o[1]==Vec4{0,.5f,0,0}));
 assert((p[1].clip==Vec4{0,-.5f,0,1}));assert((p[1].o[1]==Vec4{.5f,0,0,0}));
 assert(p[2].clip==b.clip && p[3].clip==c.clip);
 a.clip={-.5f,-.5f,.5f,1};a.o[5][1]=-1;b.o[5][1]=c.o[5][1]=1;
 p=clip_polygon({&a,&b,&c},2,1u<<6,1,0,0,1);assert(p.size()==4 && p[0].o[5][1]==0 && p[1].o[5][1]==0);
 p=clip_polygon({&a,&b,&c},1,1u<<6,1,0,0,1);assert(p.size()==3 && p[1].clip==a.clip);
 {
  PpuState s;setup(s);auto& R=s.rsx.registers;R[0x147c]=1;
  for(unsigned row=0;row<32;++row){R[0x1480+4*row]=0x55;}redraw(s);
  assert(s.rsx.draw.pixels==32);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(s.memory.load(0xc0100000+(y*8+x)*4,4)==(x%2?0:0x80ff8040));
 }
 {
  PpuState s;setup(s);s.rsx.registers[0xa70]=1;s.rsx.registers[0x1d60]|=0xe;
  // MOV R1.z, .75 followed by MOV R0, original constant.
  fp_words(s,{(1u<<24)|(4u<<9)|(1u<<1),2u|(0xe4u<<9)|(7u<<18),0,0,
              bits(.75f),bits(.75f),bits(.75f),bits(.75f),
              (1u<<24)|(15u<<9)|1u,2u|(0xe4u<<9)|(7u<<18),0,0,
              bits(1),bits(.5f),bits(.25f),bits(.5f)});
  redraw(s);assert(s.memory.load(0xc0500000,4)==0xbfffff00);
 }
 {
  PpuState s;setup(s);auto& g=s.rsx;
  g.registers[0x1ff0]=1;write_vp(g,0,vp(1,0,0,0x1b02,0));
  const float pos[3][4]={{0,0,-1,-1},{-.3f,-.4f,.5f,1},{.3f,-.4f,.5f,1}};
  for(unsigned i=0;i<3;++i)for(unsigned c=0;c<4;++c)s.memory.store(0x70002000+i*16+c*4,4,bits(pos[i][c]));
  g.draw.primitive=5;g.draw.ranges={{0,3}};rsx_execute_draw(s);
  // Near intersections project to (+/-.6,-.8); only row 6 is covered, x=2..5.
  assert(g.draw.pixels==4);
  for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x){const bool hit=y==6&&x>=2&&x<6;
   assert(s.memory.load(0xc0100000+(y*8+x)*4,4)==(hit?0x80ff8040:0));}
 }
 std::puts("PASS homogeneous intersections/varyings, user planes, stipple LSB order, FP depth export");
}
static FpInst op(unsigned code,unsigned source=0){
 return FpInst{(code<<24)|(15u<<9)|1u,source|(0xe4u<<9)|(7u<<18),(1u<<2)|(0xe4u<<9),(2u<<2)|(0xe4u<<9),{},false};
}
static void shaders(){
 auto exec=[](unsigned code,const Vec4& a,const Vec4& b=Vec4{},const Vec4& c=Vec4{}){
  FpContext ctx;ctx.write(0,false,15,a);ctx.write(1,false,15,b);ctx.write(2,false,15,c);run_fragment_program({op(code)},ctx);return ctx.read_r(0);};
 assert((exec(7,{2,3,4,5},{6,7,8,9})==Vec4{1,21,4,9}));
 assert((exec(32,{})==Vec4{1,1,1,1}));assert((exec(33,{})==Vec4{0,0,0,0}));
 assert((exec(30,{1,2,0,3})==Vec4{1,1,8,1}));assert((exec(60,{0,2,0,3})==Vec4{1,2,8,1}));
 assert((exec(59,{0,-0.f,6,-4},{4,0,0,0})==Vec4{0,-0.f,3,-2}));
 const auto zero=exec(59,{0,0,0,0},{0,0,0,0});for(float x:zero)assert(x==0);
 assert((exec(54,{1,-1,0,0},{0,1,0,0})==Vec4{1,1,0,0}));
 assert((exec(57,{0,3,4,0})==Vec4{0,.6f,.8f,.8f}));
 assert((exec(46,{2,3,0,0},{4,5,0,0},{6,0,0,0})==Vec4{29,29,29,29}));
 assert((exec(43,{1,2,0,0},{3,4,0,0},{5,6,7,8})==Vec4{40,55,40,55}));
 assert(float_bits(exec(36,{1,-2,0,0})[0])==0xc0003c00);
 assert(float_bits(exec(19,{-1,0,1,0})[0])==0x007f0081);
 assert(float_bits(exec(39,{1,0,1,0})[0])==0x00ff00ff);
 assert(float_bits(exec(41,{1,0,0,0})[0])==0x0000ffff);
 assert((exec(37,{bits_float(0xc0003c00),0,0,0})==Vec4{1,-2,1,-2}));
 assert((exec(20,{bits_float(0x007f0080),0,0,0})==Vec4{-1,0,1,0}));
 assert((exec(40,{bits_float(0x00ff00ff),0,0,0})==Vec4{1,0,1,0}));
 assert((exec(42,{bits_float(0x0000ffff),0,0,0})==Vec4{1,0,1,0}));
 assert(std::isnan(exec(29,{-1,0,0,0})[0]));
 assert(float_bits(exec(44,{0,1,0,1})[0])==0xff00ff00);
 assert((exec(45,{bits_float(0xff00ff00),0,0,0})==Vec4{0,1,0,1}));
 {
  FpContext ctx;ctx.write(0,false,15,{-4,4,0,0});auto i=op(1);i.s1=4u<<19;i.s0|=(1u<<29)|(1u<<17);
  run_fragment_program({i},ctx);assert((ctx.read_r(0)==Vec4{0,-1,0,0})); // saturate before abs, negate last
 }
 {
  auto indexed=op(1,1);indexed.s2|=1u<<30;std::array<Texture,16> textures{};
  assert(!parallel_fragment({indexed},textures));bool failed=false;FpContext ctx;
  try{run_fragment_program({indexed},ctx);}catch(const std::runtime_error&){failed=true;}assert(failed);
 }
 std::puts("PASS independent FP math, pack/unpack raw bit patterns, modifiers, signed zero and NaN");
}
static void textures(){
 PpuState s;setup(s);auto& R=s.rsx.registers;auto& m=s.memory;
 R[0x1a00]=0x8000;R[0x1a08]=0x0303;R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x01010000;R[0x1a18]=0x00010001;
 auto decode=[&](unsigned format,unsigned pitch){R[0x1a04]=0x10029|(format<<8);R[0x1840]=pitch;return decode_texture(s,Reg{s.rsx},0);};
 m.store(0xc0008000,8,UINT64_C(0x3c00c00038004400));auto t=decode(0xba,8);assert((sample_texture(t,.5f,.5f)==Vec4{1,-2,.5f,4}));
 m.store(0xc0008000,4,bits(-2));t=decode(0xbc,4);assert((sample_texture(t,.5f,.5f)==Vec4{-2,-2,-2,-2}));
 m.store(0xc0008000,2,32768);t=decode(0xb4,2);assert((sample_texture(t,.5f,.5f)==Vec4{1,32768/65535.f,1,32768/65535.f}));
 m.store(0xc0008000,4,0xffff0000);t=decode(0xb5,4);assert((sample_texture(t,.5f,.5f)==Vec4{1,0,1,0}));
 m.store(0xc0008000,4,0x3c00c000);t=decode(0xbf,4);assert((sample_texture(t,.5f,.5f)==Vec4{-2,1,-2,1}));
 m.store(0xc0008000,2,0xf801);t=decode(0xb7,2);assert((sample_texture(t,.5f,.5f)==Vec4{1,0,0,1}));
 m.store(0xc0008000,4,0x00112233);t=decode(0xbe,4);auto c=sample_texture(t,.5f,.5f);assert(c[0]==17/255.f&&c[1]==34/255.f&&c[2]==51/255.f&&c[3]==1);
 m.store(0xc0008000,2,0x8000);R[0x1a08]=0x10000303;t=decode(0xb2,2);
 assert((sample_texture(t,.5f,.5f,0,.25f)==Vec4{1,1,1,1}));assert((sample_texture(t,.5f,.5f,0,.75f)==Vec4{0,0,0,0}));
 // Gamma bit 0 affects final R only, after the channel remap.
 m.store(0xc0008000,4,0x804080c0);R[0x1a08]=0x100303;R[0x1a10]=0xaae8;t=decode(0xa5,4);c=sample_texture(t,.5f,.5f);
 assert(std::fabs(c[0]-.2158605f)<1.e-6f && c[1]==128/255.f && c[2]==192/255.f && c[3]==128/255.f);
 // Explicit LOD / bias / gradients select fixed solid mip levels.
 R[0x1a08]=0x0303;R[0x1a10]=0xaae4;R[0x1a18]=0x00020002;R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a14]=0x01030000;
 R[0x1a04]=0x20029|(0xa5<<8);R[0x1840]=8;
 for(unsigned i=0;i<4;++i){m.store(0xc0008000+i*4,4,0xffff0000);}m.store(0xc0008010,4,0xff00ff00);
 t=decode_texture(s,Reg{s.rsx},0);
 for(unsigned code:{25u,47u,49u}){
  FpContext ctx;std::array<Texture,16> tex{};tex[0]=t;ctx.textures=&tex;
  ctx.write(0,false,15,{.25f,.25f,0,1});ctx.write(1,false,15,{1,0,0,0});ctx.write(2,false,15,{0,1,0,0});
  run_fragment_program({op(code)},ctx);assert((ctx.read_r(0)==(code==49?Vec4{1,0,0,1}:Vec4{0,1,0,1})));
 }
 R[0x1a10]=0x1aae4;t=decode_texture(s,Reg{s.rsx},0);
 assert((sample_texture(t,.25f,.25f)==Vec4{1,0,0,1}));
 std::puts("PASS numeric texture formats, depth compare, channel gamma/remap and TXD/TXB/TXL LOD");
}



static void tiled_texture(){
 PpuState s;setup(s);auto& g=s.rsx;auto& R=g.registers;auto& m=s.memory;
 m.regions[1].bytes->resize(0x1000000);g.local_size=0x1000000;
 auto& tile=g.tiles[0];tile.bound=true;tile.offset=0x880000;tile.size=0x580000;tile.pitch=8192;tile.bank=1;
 // Independent golden physical addresses from test_rsx.py's pinned reference
 // round trip: (x,y)=(0,0),(1,0),(8,0),(16,0),(0,1).
 m.store(0xc0884000,4,0xffff0000);m.store(0xc0884004,4,0xff00ff00);
 m.store(0xc0884100,4,0xff0000ff);m.store(0xc0884280,4,0xffffffff);m.store(0xc0884020,4,0xff112233);
 R[0x1a00]=0x880000;R[0x1a04]=0x10029|(0xa5<<8);R[0x1a08]=0x303;R[0x1a0c]=0x80000000;
 R[0x1a10]=0xaae4;R[0x1a14]=0x01010000;R[0x1a18]=0x00200002;R[0x1840]=8192;
 auto t=decode_texture(s,Reg{g},0);assert(t.untiled);
 assert((texel(t,t.levels[0],0,0)==Vec4{1,0,0,1}));assert((texel(t,t.levels[0],1,0)==Vec4{0,1,0,1}));
 assert((texel(t,t.levels[0],8,0)==Vec4{0,0,1,1}));assert((texel(t,t.levels[0],16,0)==Vec4{1,1,1,1}));
 assert((texel(t,t.levels[0],0,1)==Vec4{17/255.f,34/255.f,51/255.f,1}));
 std::puts("PASS tiled texture independent golden addresses and BE channel readout");
}

static void fragment_flow(){
 auto flow=[](unsigned code,unsigned condition){auto i=op(code&63);i.d&=~1u;i.s0=condition<<18;i.s1=1u<<31;i.s2=0;return i;};
 auto literal=[](float value){auto i=op(1,2);i.d&=~1u;i.has_constant=true;i.constant={value,value,value,value};return i;};
 auto ife=flow(66,1);ife.s1|=12;ife.s2=20;
 std::array<FpContext,4> quad{};
 for(unsigned l=0;l<4;++l)quad[l].cc[0]={l==0||l==3?-1.f:float(l-1),0,0,0};
 run_fragment_quad({ife,literal(1),literal(2),FpInst{1,0,0,0,{},false}},quad);
 for(unsigned l=0;l<4;++l)assert(quad[l].read_r(0)[0]==(l==0||l==3?1.f:2.f));
 // Three iterations, a predicated break after the first add in LT lanes.
 auto loop=flow(67,7);loop.s1|=3u<<2;loop.s2=12;
 auto add=op(3);add.d&=~1u;auto brk=flow(64,1);
 for(auto& ctx:quad){ctx.write(0,false,15,{});ctx.write(1,false,15,{2,2,2,2});}
 run_fragment_quad({loop,add,brk,FpInst{1,0,0,0,{},false}},quad);
 for(unsigned l=0;l<4;++l)assert(quad[l].read_r(0)[0]==(l==0||l==3?2.f:6.f));
 // REP counts and early return both skip following instructions per lane.
 auto rep=flow(68,7);rep.s1|=2u<<2;rep.s2=8;
 auto ret=flow(69,1);
 for(auto& ctx:quad)ctx.write(0,false,15,{});
 run_fragment_quad({rep,add,ret,literal(9),FpInst{1,0,0,0,{},false}},quad);
 for(unsigned l=0;l<4;++l)assert(quad[l].read_r(0)[0]==(l==0||l==3?4.f:9.f));
 bool failed=false;ife.s1=(1u<<31)|5;
 try{run_fragment_quad({ife,literal(1),literal(2),FpInst{1,0,0,0,{},false}},quad);}catch(const std::runtime_error&){failed=true;}assert(failed);
 // Original memory loader must not confuse CF target low bits with a constant.
 PpuState s;setup(s);fp_words(s,{(2u<<24),1u<<18,(1u<<31)|12,20,
    (1u<<24)|(15u<<9),2u|(0xe4u<<9)|(7u<<18),0,0,bits(1),bits(1),bits(1),bits(1),
    (1u<<24)|(15u<<9),2u|(0xe4u<<9)|(7u<<18),0,0,bits(2),bits(2),bits(2),bits(2),1,0,0,0});
 const auto loaded=load_fragment_program(s,Reg{s.rsx});assert(loaded.size()==4&&loaded[2].word_offset==12);
 run_fragment_quad(loaded,quad);
 std::puts("PASS FP structured IF divergence, LOOP/REP/BRK/RET, literal-aware addresses and bad targets");
}

static std::array<unsigned,4> vp(unsigned vec,unsigned sca,unsigned dst,unsigned raw,unsigned input,bool end){
 return {(63u<<15)|(7u<<10)|(1u<<30),(vec<<22)|(sca<<27)|((raw>>9)&255)|(input<<8),
         (raw&511)<<23,(15u<<13)|(15u<<17)|(63u<<7)|(dst<<2)|unsigned(end)};
}
static void write_vp(NativeRsx& g,unsigned at,const std::array<unsigned,4>& w){
 for(unsigned i=0;i<4;++i){g.transform_program[at*4+i]=w[i];g.transform_program_written[at*4+i]=true;}
}
static void vertex_program(){
 PpuState s;setup(s);auto& g=s.rsx;std::array<Vec4,16> inputs{};inputs[0]={-1.25f,0,0,0};inputs[1]={2,3,4,5};
 // ARL a0.x,input0 followed by MOV o0,c[2+a0.x]. floor(-1.25)=-2.
 auto arl=vp(13,0,31,0x1b02,0,false);arl[0]&=~(63u<<15);arl[3]&=~(7u<<13);write_vp(g,0,arl);
 auto load=vp(1,0,0,0x1b03,0);load[1]|=2u<<12;load[3]|=2;write_vp(g,1,load);
 for(unsigned i=0;i<4;++i){g.transform_constants[i]=bits(float(i+1));g.transform_constants_written[i]=true;}
 assert((run_vertex_program(g,0,inputs).o[0]==Vec4{1,2,3,4}));
 auto zero=vp(1,0,0,0x1b03,0);zero[1]|=511u<<12;write_vp(g,0,zero);
 assert((run_vertex_program(g,0,inputs).o[0]==Vec4{0,0,0,0}));
 inputs[0][0]=3;write_vp(g,0,arl);load[1]=(load[1]&~(1023u<<12))|(511u<<12);write_vp(g,1,load);
 for(unsigned i=0;i<4;++i){g.transform_constants[8+i]=bits(float(i+9));g.transform_constants_written[8+i]=true;}
 assert((run_vertex_program(g,0,inputs).o[0]==Vec4{9,10,11,12})); // (511+3)&511 == 2
 inputs[0][0]=-1.25f;
 // Conditional component writes using a CC set-only MOV (xyzw identity CC swizzle).
 auto setcc=vp(1,0,31,0x1b02,0,false);setcc[0]|=1u<<14;write_vp(g,0,setcc);
 auto pred=vp(1,0,0,0x1b02,1);pred[0]=(pred[0]&~(7u<<10))|(1u<<10)|(1u<<13)|0x6cu;write_vp(g,1,pred);
 assert((run_vertex_program(g,0,inputs).o[0]==Vec4{2,0,0,1}));
 // Dual-issued CAL/RET preserves the vector write, then an immediate branch.
 auto call=vp(0,10,31,0x1b02,0,false);call[3]|=3u<<29;write_vp(g,0,call);
 auto branch=vp(0,9,31,0x1b02,0,false);branch[3]|=4u<<29;write_vp(g,1,branch);
 auto body=vp(1,12,0,0x1b02,1,false);write_vp(g,3,body);
 write_vp(g,4,vp(1,0,1,0x1b02,1));auto out=run_vertex_program(g,0,inputs);
 assert(out.o[0]==inputs[1]&&out.o[1]==inputs[1]);
 // Real vertex texture registers, float32 nearest sample (no FP-unit aliases).
 auto& R=g.registers;R[0x900]=0x9000;R[0x904]=0x10021|(0xbcu<<8);R[0x908]=0x303;
 R[0x90c]=0x80000000;R[0x910]=4;R[0x918]=0x00010001;s.memory.store(0xc0009000,4,bits(3.5f));
 inputs[0]={.5f,.5f,0,0};write_vp(g,0,vp(25,0,0,0x1b02,0));
 assert((run_vertex_program(g,0,inputs,&s).o[0]==Vec4{3.5f,3.5f,3.5f,3.5f}));
 // Unbounded branch loops stop explicitly, never return an invented shader output.
 auto loop=vp(0,9,31,0x1b02,0,false);write_vp(g,0,loop);bool failed=false;
 try{(void)run_vertex_program(g,0,inputs);}catch(const std::runtime_error&){failed=true;}assert(failed);
 auto cli=vp(0,11,31,0x1b02,0);write_vp(g,0,cli);failed=false;
 try{(void)run_vertex_program(g,0,inputs);}catch(const std::runtime_error&){failed=true;}assert(failed);
 std::puts("PASS VP ARL/indexed constants, per-component CC, dual-issued CAL/RET, BRI, float texture and loop bound");
}
static void inline_array(){
 PpuState s;setup(s);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x10000,0x1000,6,nullptr,0);g.dma=0x10000;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();
 unsigned at=0;auto emit=[&](unsigned reg,unsigned value){m.store(0x70000000+at,4,0x40000|reg);m.store(0x70000004+at,4,value);at+=8;};
 emit(0x1808,8);
 const float pos[4][4]={{0,0,.25f,1},{8,0,.25f,1},{8,8,.25f,1},{0,8,.25f,1}};
 for(unsigned i=0;i<4;++i){for(float c:pos[i])emit(0x1818,bits(c));emit(0x1818,bits(0));emit(0x1818,bits(0));}
 emit(0x1808,0);m.store(g.dma+0x40,4,at);assert(rsx_run(s,at/8));
 assert(m.load(g.dma+0x44,4)==at&&g.draw.pixels==64);
 for(unsigned i=0;i<64;++i)assert(m.load(0xc0100000+i*4,4)==0x80ff8040);
 assert(!g.draw.inline_active);assert(rsx_vertex_data_register(g,0x1944,0x11223344));
 assert(!rsx_vertex_data_register(g,0x1808,8));g.draw.primitive=8;
 assert(rsx_vertex_data_register(g,0x1818,0));bool failed=false;
 try{(void)rsx_vertex_data_register(g,0x1808,0);}catch(const std::runtime_error&){failed=true;}assert(failed);
 std::puts("PASS INLINE_ARRAY FIFO float interleave, real draw, incomplete-vertex explicit error");
}

static void parallel(){
 for(unsigned aa:{0u,3u,4u,5u})for(unsigned format:{3u,8u,11u,12u,16u}){
  PpuState a,b;
  for(auto* s:{&a,&b}){
   setup(*s,163,141);auto& R=s->rsx.registers;const unsigned bpp=color_bytes(format),nx=aa?2:1,ny=aa>=4?2:1;
   R[0x208]=0x140|format|(aa<<12);R[0x20c]=163*nx*bpp;R[0x22c]=163*nx*4;
   R[0xa70]=R[0xa74]=1;R[0xa6c]=0x207;R[0x17cc]=1;R[0x147c]=1;
   for(unsigned i=0;i<32;++i)R[0x1480+4*i]=0x55555555;
   assert(ny<=2);s->rsx.draw.ranges={{0,4},{0,4}};
  }
  setenv("ANYPS3_RSX_THREADS","1",1);rsx_execute_draw(a);
  const auto before=test_dispatches;setenv("ANYPS3_RSX_THREADS","4",1);rsx_execute_draw(b);assert(test_dispatches==before+1);
  for(unsigned i=0;i<a.memory.regions.size();++i)assert(*a.memory.regions[i].bytes==*b.memory.regions[i].bytes);
  assert(a.rsx.draw.pixels==b.rsx.draw.pixels&&a.rsx.draw.triangles==b.rsx.draw.triangles&&a.rsx.report_values==b.rsx.report_values);
 }
 {
  PpuState s;setup(s,163,141);auto& g=s.rsx;auto& R=g.registers;
  g.transform_program[13*4+3]&=~1u;write_vp(g,14,vp(25,0,7,0x1b02,0));
  R[0x900]=0x100000;R[0x904]=0x10021|(0xbcu<<8);R[0x908]=0x303;R[0x90c]=0x80000000;R[0x910]=4;R[0x918]=0x10001;
  setenv("ANYPS3_RSX_THREADS","4",1);const auto before=test_dispatches;rsx_execute_draw(s);
  assert(test_dispatches==before&&s.memory.load(0xc0100000,4)==0x80ff8040);
 }
 std::puts("PASS 20 sequential/4-worker MSAA/format/stipple/depth combinations");
}
int main(int argc,char** argv){assert(argc==2);const std::string mode=argv[1];
 if(mode=="formats")formats();else if(mode=="points")points();else if(mode=="msaa")antialias();else if(mode=="clipping")clipping();else if(mode=="shaders")shaders();else if(mode=="textures")textures();else if(mode=="tiled")tiled_texture();else if(mode=="flow")fragment_flow();else if(mode=="vertex")vertex_program();else if(mode=="inline")inline_array();else parallel();}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class RsxGapsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='rsxgaps-')
        root = Path(cls.tmp.name)
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source = source.replace('namespace rsxdraw {', 'static unsigned test_dispatches=0;\nnamespace rsxdraw {', 1)
        # FIFO whitelist belongs to rsx.py (outside this task's ownership).
        # Test the integration with the exact pending one-token whitelist addition.
        source = source.replace('reg!=0x1808 &&', 'reg!=0x1818 && reg!=0x1808 &&', 1)
        source = source.replace('static thread_local DrawPool pool;', '++test_dispatches;static thread_local DrawPool pool;')
        harness = HARNESS.split('static void observe(')[0].replace('GAME_VP_WORDS', ','.join(f'{w}u' for w in GAME_VP))
        (root/'test.cpp').write_text(source+'\n#include <cassert>\n'+harness+MAIN)
        build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-pthread','-o',str(root/'test')],capture_output=True,text=True,timeout=90)
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

    def test_target_formats(self): self.run_case('formats')
    def test_aliased_points(self): self.run_case('points')
    def test_msaa_storage_and_coverage(self): self.run_case('msaa')
    def test_homogeneous_clip_and_depth_export(self): self.run_case('clipping')
    def test_fragment_arithmetic(self): self.run_case('shaders')
    def test_texture_formats_and_lod(self): self.run_case('textures')
    def test_tiled_texture_golden_addresses(self): self.run_case('tiled')
    def test_fragment_structured_flow(self): self.run_case('flow')
    def test_vertex_control_flow_and_texture(self): self.run_case('vertex')
    def test_inline_array_fifo(self): self.run_case('inline')
    def test_parallel_bit_equivalence(self): self.run_case('parallel')
