"""RSX scaled transfer: the row-wise and batch paths equal the per-pixel reference path byte for byte (native oracles).

rsxblit::scaled has a reference implementation (one guest Range per pixel, force_pixel_path), a row-wise path
(one Range per touched row, linear untiled SRCCOPY) and a batch path (everything else: bound tiles on either side,
Morton-swizzled destinations, SRCCOPY_AND). The differential test drives them on identical randomized transfers --
formats, pitches with padding, 1:1 / up / down / mirrored scales, filtering, clip rectangles, VRAM and IO operands,
overlapping in-place copies, bound tiles (several pitches and banks, source and destination), swizzled destinations,
zero-alpha discard, invalid parameters -- under serial and concurrent guest memory, and requires identical destination
bytes, untouched neighbours and identical errors. Analytic cases pin the expected bytes independently of every path:
exact 1:1 copy with row padding, 2x nearest upscale, a copy into and out of a bound tile (physical addresses derived
by hand from the RSX tile formula), a Morton-swizzled destination and SRCCOPY_AND discarding zero-alpha pixels. The oracle is
the frozen pre-change implementation in tests/rsx_blit_frozen.py (own addressing, per-pixel loop), not code shared with the change.
Further cases pin the review findings of cd781d8: aliased destination mappings keep the reference's raster order, observers see
only touched bytes and their failures propagate, sparse downscales plan only their taps.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.rsx_blit_frozen import CPP as FROZEN_CPP

CPP = r'''
#include <cassert>
static void setup(PpuState& s,unsigned vram=0x80000){
 auto& g=s.rsx;g.local_size=vram;s.memory.map(0xc0000000,g.local_size,6,nullptr,0);
 s.memory.map(0x70000000,0x300000,6,nullptr,0);g.io[0]=0x70000000;g.io[1]=0x70100000;g.io[2]=0x70200000;
 s.memory.map(0x70300000,0x10000,4,nullptr,0);g.io[3]=0x70300000;   // IO page 3 is read-only: destination writes fault
 s.memory.map(0x10000,0x2000,6,nullptr,0);g.dma=0x10000;g.fifo_enabled=true;
}
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
static unsigned src_bpp_of(unsigned f){return f==8?1:(f==1||f==2||f==7||f==9)?2:4;}
struct Case {
 unsigned src_format,dst_format,filter,origin,w,h,src_pitch,ow,oh,dst_pitch,ox,oy,cx,cy,cw,ch,point;
 std::int32_t dx,dy;bool src_io,dst_io,in_place,tile,src_tile,swz;unsigned src_offset,dst_offset;
 unsigned swz_w,swz_h,op,tile_pitch,tile_bank,src_tile_pitch,src_tile_bank,tile_size,src_tile_size;
 bool dst_ahead,src_ahead;   // the tile starts at the next 64 KiB boundary, inside the image (linear pixels first, tiled pixels after)
 unsigned io1;   // IO page 1: 0 contiguous with page 0, 1 discontiguous, 2 unmapped hole, 3 alias of page 0
};
static void program_case(PpuState& s,const Case& c){
 auto& R=s.rsx.registers;
 R={{0xc184,c.src_io?0xfeed0001u:0xfeed0000u},{0xc198,c.swz?0x31337a73u:0x313371c3u},{0xc2fc,1},{0xc300,c.src_format},{0xc304,c.op},
    {0xc308,(c.cy<<16)|c.cx},{0xc310,(c.oy<<16)|c.ox},{0xc30c,(c.ch<<16)|c.cw},{0xc314,(c.oh<<16)|c.ow},
    {0xc318,std::uint32_t(c.dx)},{0xc31c,std::uint32_t(c.dy)},{0xc400,(c.h<<16)|c.w},
    {0xc404,(c.filter<<24)|(c.origin<<16)|c.src_pitch},{0xc408,c.src_offset},
    {0x6188,c.dst_io?0xfeed0001u:0xfeed0000u},{0x6300,c.dst_format},{0x6304,c.dst_pitch<<16},{0x630c,c.dst_offset}};
 if(c.swz){R[0x8300]=((c.swz_h?c.swz_h:5u)<<24)|((c.swz_w?c.swz_w:6u)<<16)|c.dst_format;R[0x8184]=c.dst_io?0xfeed0001u:0xfeed0000u;R[0x8304]=c.dst_offset;}
 s.rsx.io[1]=c.io1==0?0x70100000u:c.io1==1?0x70200000u:c.io1==3?0x70000000u:UINT32_MAX;
 auto& t=s.rsx.tiles[0];t=NativeRsx::Tile{};
 if(c.tile){t.bound=true;t.location=c.dst_io?1:0;t.offset=c.dst_ahead?(c.dst_offset+0xffffu)&~0xffffu:c.dst_offset&~0xffffu;t.size=c.tile_size;t.pitch=c.tile_pitch;t.bank=c.tile_bank;}
 auto& q=s.rsx.tiles[1];q=NativeRsx::Tile{};
 if(c.src_tile){q.bound=true;q.location=c.src_io?1:0;q.offset=c.src_ahead?(c.src_offset+0xffffu)&~0xffffu:c.src_offset&~0xffffu;q.size=c.src_tile_size;q.pitch=c.src_tile_pitch;q.bank=c.src_tile_bank;}
}
// Run one transfer; returns the error text ("" on success).
static std::string run(PpuState& s,const Case& c){
 program_case(s,c);
 try{rsxblit::scaled(s,c.point);}catch(const std::runtime_error& e){return e.what();}
 return "";
}
// The oracle: the frozen pre-change implementation (tests/rsx_blit_frozen.py) with its own addressing, forced onto its per-pixel path.
static std::string run_ref(PpuState& s,const Case& c){
 program_case(s,c);
 rsxblit_ref::force_pixel_path=true;std::string error;
 try{rsxblit_ref::scaled(s,c.point);}catch(const std::runtime_error& e){error=e.what();}
 rsxblit_ref::force_pixel_path=false;return error;
}
// Observed guest regions: linear IO, VRAM, both sides of the IO page 0/1 edge, the discontiguous page-1 alias, the read-only page.
struct Window {std::uint64_t address;unsigned size;bool writable;};
static const Window windows[]={{0x70000000,0x30000,true},{0xc0000000,0x40000,true},{0x700fe000,0x4000,true},{0x70200000,0x2000,true},{0x70300000,0x1000,false}};
static std::vector<std::uint8_t> dump(PpuState& s){
 std::vector<std::uint8_t> out;
 for(const auto& w:windows){const auto at=out.size();out.resize(at+w.size);s.memory.transfer(w.address,out.data()+at,w.size,false);}
 return out;
}
static void fill(PpuState& a,PpuState& b,std::uint32_t seed){
 std::uint32_t r=seed;
 for(const auto& w:windows){if(!w.writable)continue;
  std::vector<std::uint8_t> bytes(w.size);for(auto& x:bytes)x=std::uint8_t(lcg(r));
  for(auto* s:{&a,&b})s->memory.transfer(w.address,bytes.data(),w.size,true);}
}
static Case random_case(std::uint32_t& r){
 static const unsigned sources[]={1,2,3,4,7,8,9,12,13};
 static const std::int32_t scales[]={0x100000,0x80000,0x200000,0x1e0000,0x155555,-0x100000,0x180000,0x40000};
 Case c{};
 c.src_format=sources[lcg(r)%9];
 const unsigned choice=lcg(r)%4;
 c.dst_format=choice==0?4:choice==1?11:10;
 c.filter=lcg(r)%3==0;c.origin=1+lcg(r)%2;
 if(c.dst_format==11){c.src_format=lcg(r)&1?3:12;c.filter=0;}   // Y32 is an untyped 32-bit SRCCOPY
 // A third of the cases are 1:1 unfiltered copies with identical byte layout (the run-wise identity path's domain).
 const bool copy_bias=lcg(r)%3==0;
 if(copy_bias){const unsigned pick=lcg(r)%3;c.src_format=pick==1?7:3;c.dst_format=pick==0?10:pick==1?4:11;c.filter=0;c.origin=2;}
 c.w=1+lcg(r)%40;c.h=1+lcg(r)%24;
 const unsigned bpp=src_bpp_of(c.src_format);
 c.src_pitch=c.w*bpp+(lcg(r)%3==0?4*(lcg(r)%5):0);
 c.ow=1+lcg(r)%40;c.oh=1+lcg(r)%24;c.ox=lcg(r)%4;c.oy=lcg(r)%3;
 if(copy_bias){c.ow=c.w;c.oh=c.h;}
 const unsigned dbpp=c.dst_format==4?2:4;
 c.dst_pitch=(c.ox+c.ow)*dbpp+(lcg(r)%3==0?4*(lcg(r)%5):0);
 c.cx=lcg(r)%6;c.cy=lcg(r)%4;
 c.cw=1+lcg(r)%(c.ow+3);c.ch=1+lcg(r)%(c.oh+2);
 c.dx=scales[lcg(r)%8];c.dy=scales[lcg(r)%8];
 c.point=(lcg(r)%6)|((lcg(r)%4)<<16);
 if(copy_bias){c.dx=c.dy=0x100000;c.point=0;}
 c.src_io=lcg(r)&1;c.dst_io=lcg(r)&1;
 c.src_offset=0x1000*(c.src_io?1:1)+4*(lcg(r)%16);
 c.dst_offset=c.dst_io?0x4000+4*(lcg(r)%16):0x1000+4*(lcg(r)%16);
 c.in_place=lcg(r)%7==0;
 if(c.in_place){c.dst_io=c.src_io;c.dst_offset=c.src_offset+4*(lcg(r)%40);}
 c.tile=lcg(r)%12==0;
 static const unsigned pitches[]={256,512,768,1024,256,512,1280,2048};   // 1280/2048 push physical blocks past the 64 KiB tile: identical errors
 c.tile_pitch=pitches[lcg(r)%8];c.tile_bank=lcg(r)%4;c.src_tile_pitch=pitches[lcg(r)%8];c.src_tile_bank=lcg(r)%4;
 c.op=lcg(r)%5==0?0u:3u;
 // A quarter of the tiles end inside the image: tiled and linear pixels mix, and a 32-byte group can straddle the tile end.
 c.tile_size=lcg(r)%4==0?(c.dst_offset&0xffffu)+4*(1+lcg(r)%96):0x10000u;
 c.src_tile_size=lcg(r)%4==0?(c.src_offset&0xffffu)+4*(1+lcg(r)%96):0x10000u;
 c.swz=lcg(r)%9==0;
 if(lcg(r)%3==0)c.tile=true;
 // Mapping edges: rows that straddle the 1 MiB IO page boundary (contiguous / discontiguous / unmapped next page),
 // a destination in the read-only IO page, a source reaching past the end of VRAM.
 c.io1=0;
 if(lcg(r)%5==0){
  const unsigned edge=1+lcg(r)%5;c.io1=lcg(r)%3;
  if(edge==1){c.src_io=true;c.in_place=false;c.src_offset=0x100000-4*(1+lcg(r)%40);}
  else if(edge==2){c.dst_io=true;c.in_place=false;c.dst_offset=0x100000-4*(1+lcg(r)%40);}
  else if(edge==3){c.dst_io=true;c.in_place=false;c.dst_offset=0x300000+0x100+4*(lcg(r)%16);}
  else if(edge==4){c.src_io=false;c.src_offset=0x7ffc0-4*(lcg(r)%30);}
  else {c.dst_io=false;c.in_place=false;c.dst_offset=0x7ffc0-4*(lcg(r)%30);}
 }
 c.src_tile=lcg(r)%3==0;
 c.dst_ahead=c.tile && lcg(r)%5==0;c.src_ahead=c.src_tile && lcg(r)%5==0;
 if(c.dst_ahead && !c.in_place && c.dst_offset<0x10000)c.dst_offset=0x10000-4*(1+lcg(r)%30);
 else c.dst_ahead=false;
 if(c.src_ahead && !c.in_place && c.src_offset<0x10000)c.src_offset=0x10000-4*(1+lcg(r)%30);
 else c.src_ahead=false;
 if(c.src_tile && c.tile && c.src_io==c.dst_io)c.src_tile=false;   // two tiles over one pixel is an (identical) error, not the interesting case
 // Invalid parameters are part of the sweep: zero pitch, pitch below the row, unsupported combinations.
 if(lcg(r)%40==0)c.src_pitch=c.w*bpp>1?c.w*bpp-1:0;
 return c;
}
static void differential(bool concurrent){
 PpuState fast,slow;setup(fast);setup(slow);
 if(concurrent){fast.memory.enable_concurrency();slow.memory.enable_concurrency();}
 std::uint32_t r=concurrent?0x1234567u:0x7654321u;unsigned ok=0,failed=0,tiled=0;std::map<std::string,unsigned> errors;
 const auto start=rsxblit::rows_executed,start_batch=rsxblit::batch_executed,start_copy=rsxblit::batch_copy_executed;
 for(unsigned n=0;n<1400;++n){
  const auto c=random_case(r);fill(fast,slow,r+n);
  rsxblit::force_pixel_path=false;const auto e_fast=run(fast,c);
  const auto e_slow=run_ref(slow,c);
  if(e_fast!=e_slow)throw std::runtime_error("case "+std::to_string(n)+": fast error '"+e_fast+"' reference '"+e_slow+"'");
  const auto a=dump(fast),b=dump(slow);
  if(a!=b){unsigned at=0;while(a[at]==b[at])++at;throw std::runtime_error("case "+std::to_string(n)+": destination differs at byte "+std::to_string(at)+" (fmt "+std::to_string(c.src_format)+"->"+std::to_string(c.dst_format)+")");}
  e_fast.empty()?++ok:++failed;tiled+=c.tile||c.src_tile;if(!e_fast.empty())++errors[e_fast.substr(0,26)];
 }
 const auto taken=rsxblit::rows_executed-start,batched=rsxblit::batch_executed-start_batch,copies=rsxblit::batch_copy_executed-start_copy;
 std::printf("INFO %s: %u transfers ok, %u rejected identically, %u with a bound tile, row-wise path taken %llu times, batch path %llu times (%llu run-wise copies)\n",concurrent?"concurrent":"serial",ok,failed,tiled,(unsigned long long)taken,(unsigned long long)batched,(unsigned long long)copies);
 for(const auto& [text,count]:errors)std::printf("INFO   %u x %s\n",count,text.c_str());
 std::fflush(stdout);
 assert(ok>900 && failed>5 && taken>80 && batched>150 && copies>100);
 rsxblit::force_pixel_path=false;
}
static void analytic(){
 for(bool concurrent:{false,true}){
  PpuState s;setup(s);if(concurrent)s.memory.enable_concurrency();auto& m=s.memory;
  // 1:1 A8R8G8B8 copy of 5x3 pixels, source pitch 32 (12 bytes of padding), destination pitch 24 (4 bytes of padding).
  std::vector<std::uint8_t> background(0x20),expected(0x20);
  for(unsigned i=0;i<0x100;++i)m.store(0xc0001000+i,1,0xa0+(i&7));
  for(unsigned y=0;y<3;++y)for(unsigned x=0;x<5;++x)for(unsigned k=0;k<4;++k)m.store(0x70001000+y*32+x*4+k,1,0x10*y+0x03*x+k);
  Case c{};c.op=3;c.src_format=3;c.dst_format=10;c.origin=2;c.w=5;c.h=3;c.src_pitch=32;c.ow=5;c.oh=3;c.dst_pitch=24;c.cw=5;c.ch=3;c.dx=c.dy=0x100000;c.point=0;
  c.src_io=true;c.dst_io=false;c.src_offset=0x1000;c.dst_offset=0x1000;
  const auto before=rsxblit::rows_executed;
  assert(run(s,c).empty());
  for(unsigned y=0;y<3;++y)for(unsigned x=0;x<6;++x)for(unsigned k=0;k<4;++k){
   const unsigned at=y*24+x*4+k;const unsigned got=unsigned(m.load(0xc0001000+at,1));
   const unsigned want=x<5?0x10*y+0x03*x+k:0xa0+(at&7);   // padding column keeps its old bytes
   if(got!=want)throw std::runtime_error("1:1 copy byte "+std::to_string(at)+" is "+std::to_string(got)+" expected "+std::to_string(want));}
  assert(unsigned(m.load(0xc0001000+72,1))==0xa0+(72&7));   // the byte after the last row is untouched
  assert(rsxblit::rows_executed==before+1);
  // 2x nearest upscale of a 2x1 image (red, green) to 4x2: each source pixel doubles in both directions.
  const std::uint32_t red=0xffff0000,green=0xff00ff00;
  m.store(0x70001000,4,red);m.store(0x70001004,4,green);
  Case u=c;u.w=2;u.h=1;u.src_pitch=8;u.ow=4;u.oh=2;u.dst_pitch=16;u.cw=4;u.ch=2;u.dx=u.dy=0x80000;u.origin=2;u.dst_offset=0x2000;
  assert(run(s,u).empty());
  const std::uint32_t expect[2][4]={{red,red,green,green},{red,red,green,green}};
  for(unsigned y=0;y<2;++y)for(unsigned x=0;x<4;++x)if(m.load(0xc0002000+y*16+x*4,4)!=expect[y][x])
   throw std::runtime_error("upscale pixel "+std::to_string(x)+","+std::to_string(y)+" is "+std::to_string(m.load(0xc0002000+y*16+x*4,4)));
 }
 std::puts("PASS analytic 1:1 copy with padding and 2x nearest upscale (serial and concurrent)");
}
// Hand-derived physical offsets for a tile with pitch 256 (one 256-byte column), bank 0, offset 0, pixels x=0..7 (one 32-byte
// group) of rows 0..8: byte_address & 31 is kept, (row & 3) << 5 and ((row >> 2) & 1) << 7 select the sub-row inside the first
// 128 bytes, row 8 sets bit 11 (row & 56) << 8 = 0x800 and the final XOR copies bit 11 into bit 10 -> 0xc00. These constants come from
// the RSX tile formula worked out on paper, not from running rsx_tiled_offset.
static const unsigned tile_rows[9]={0x000,0x020,0x040,0x060,0x080,0x0a0,0x0c0,0x0e0,0xc00};
static std::uint8_t pattern(unsigned x,unsigned y,unsigned k){return std::uint8_t((y*37+x*5+k*11+1)&0xff);}
static void analytic_layouts(){
 for(bool concurrent:{false,true}){
  PpuState s;setup(s);if(concurrent)s.memory.enable_concurrency();auto& m=s.memory;
  const auto fill_background=[&](std::uint64_t base,unsigned size,std::uint8_t value){for(unsigned i=0;i<size;++i)m.store(base+i,1,value);};
  // (1) linear IO source -> bound tile destination in VRAM.
  fill_background(0xc0000000,0x1000,0xa5);
  for(unsigned y=0;y<9;++y)for(unsigned x=0;x<8;++x)for(unsigned k=0;k<4;++k)m.store(0x70001000+y*32+x*4+k,1,pattern(x,y,k));
  Case c{};c.op=3;c.src_format=3;c.dst_format=10;c.origin=2;c.w=8;c.h=9;c.src_pitch=32;c.ow=8;c.oh=9;c.dst_pitch=256;c.cw=8;c.ch=9;
  c.dx=c.dy=0x100000;c.src_io=true;c.dst_io=false;c.src_offset=0x1000;c.dst_offset=0;c.tile=true;c.tile_pitch=256;c.tile_bank=0;c.tile_size=c.src_tile_size=0x10000;
  const auto before=rsxblit::batch_executed;
  assert(run(s,c).empty());
  assert(rsxblit::batch_executed==before+1);
  std::map<unsigned,std::uint8_t> expected;
  for(unsigned y=0;y<9;++y)for(unsigned x=0;x<8;++x)for(unsigned k=0;k<4;++k)expected[tile_rows[y]+4*x+k]=pattern(x,y,k);
  for(unsigned at=0;at<0x1000;++at){
   const auto it=expected.find(at);const unsigned want=it==expected.end()?0xa5:it->second;
   if(unsigned(m.load(0xc0000000+at,1))!=want)throw std::runtime_error("tiled destination byte "+std::to_string(at)+" is "+std::to_string(unsigned(m.load(0xc0000000+at,1)))+" expected "+std::to_string(want));}
  // (2) bound tile source in VRAM (the pattern stored at the hand-derived physical offsets) -> linear IO destination.
  fill_background(0x70002000,0x400,0x5a);
  Case d{};d.op=3;d.src_format=3;d.dst_format=10;d.origin=2;d.w=8;d.h=9;d.src_pitch=256;d.ow=8;d.oh=9;d.dst_pitch=32;d.cw=8;d.ch=9;
  d.dx=d.dy=0x100000;d.src_io=false;d.dst_io=true;d.src_offset=0;d.dst_offset=0x2000;d.src_tile=true;d.src_tile_pitch=256;d.src_tile_bank=0;d.tile_size=d.src_tile_size=0x10000;
  const auto before2=rsxblit::batch_executed;
  assert(run(s,d).empty());
  assert(rsxblit::batch_executed==before2+1);
  for(unsigned y=0;y<9;++y)for(unsigned x=0;x<8;++x)for(unsigned k=0;k<4;++k)
   if(unsigned(m.load(0x70002000+y*32+x*4+k,1))!=pattern(x,y,k))throw std::runtime_error("tiled source pixel "+std::to_string(x)+","+std::to_string(y)+" byte "+std::to_string(k)+" is "+std::to_string(unsigned(m.load(0x70002000+y*32+x*4+k,1))));
  assert(unsigned(m.load(0x70002000+9*32,1))==0x5a);   // the row after the last one is untouched
  // (3) Morton swizzle: destination (x,y) lives at index (x&1)|(y&1)<<1|(x&2)<<1|(y&2)<<2 of a 64x32 image at VRAM offset 0x3000.
  fill_background(0xc0003000,0x100,0x33);
  for(unsigned y=0;y<4;++y)for(unsigned x=0;x<4;++x)for(unsigned k=0;k<4;++k)m.store(0x70001000+y*16+x*4+k,1,pattern(x,y,k));
  Case e{};e.op=3;e.src_format=3;e.dst_format=10;e.origin=2;e.w=4;e.h=4;e.src_pitch=16;e.ow=4;e.oh=4;e.dst_pitch=16;e.cw=4;e.ch=4;
  e.dx=e.dy=0x100000;e.src_io=true;e.dst_io=false;e.src_offset=0x1000;e.dst_offset=0x3000;e.swz=true;
  const auto before3=rsxblit::batch_executed;
  assert(run(s,e).empty());
  assert(rsxblit::batch_executed==before3+1);
  for(unsigned y=0;y<4;++y)for(unsigned x=0;x<4;++x){
   const unsigned index=(x&1)|((y&1)<<1)|((x&2)<<1)|((y&2)<<2);
   for(unsigned k=0;k<4;++k)if(unsigned(m.load(0xc0003000+index*4+k,1))!=pattern(x,y,k))throw std::runtime_error("swizzled pixel "+std::to_string(x)+","+std::to_string(y)+" is wrong");}
  assert(unsigned(m.load(0xc0003000+16*4,1))==0x33);   // morton indices 0..15 only
  // (4) SRCCOPY_AND: pixels with source alpha 0 keep the destination bytes.
  fill_background(0xc0004000,0x40,0x5a);
  const unsigned alpha[4]={0xff,0x00,0xff,0x00};
  for(unsigned x=0;x<4;++x){m.store(0x70001000+x*4,1,alpha[x]);for(unsigned k=1;k<4;++k)m.store(0x70001000+x*4+k,1,0x10*x+k);}
  Case f{};f.op=0;f.src_format=3;f.dst_format=10;f.origin=2;f.w=4;f.h=1;f.src_pitch=16;f.ow=4;f.oh=1;f.dst_pitch=16;f.cw=4;f.ch=1;
  f.dx=f.dy=0x100000;f.src_io=true;f.dst_io=false;f.src_offset=0x1000;f.dst_offset=0x4000;
  const auto before4=rsxblit::batch_executed;
  assert(run(s,f).empty());
  assert(rsxblit::batch_executed==before4+1);
  for(unsigned x=0;x<4;++x)for(unsigned k=0;k<4;++k){
   const unsigned want=alpha[x]?(k==0?0xffu:0x10*x+k):0x5a;
   if(unsigned(m.load(0xc0004000+x*4+k,1))!=want)throw std::runtime_error("SRCCOPY_AND pixel "+std::to_string(x)+" byte "+std::to_string(k)+" is "+std::to_string(unsigned(m.load(0xc0004000+x*4+k,1))));}
 }
 std::puts("PASS analytic tile (both directions), Morton swizzle and SRCCOPY_AND on the batch path (serial and concurrent)");
}

// Review findings of cd781d8, each with an independent expectation and the frozen reference as oracle.
static void require_same(PpuState& fast,PpuState& slow,const Case& c,const char* what){
 const auto e_fast=run(fast,c),e_slow=run_ref(slow,c);
 if(e_fast!=e_slow)throw std::runtime_error(std::string(what)+": error '"+e_fast+"' reference '"+e_slow+"'");
 const auto a=dump(fast),b=dump(slow);
 if(a!=b){unsigned at=0;while(a[at]==b[at])++at;throw std::runtime_error(std::string(what)+": destination differs from the reference at byte "+std::to_string(at));}
}
// (1) Two destination pixels whose DMA offsets (0 and 0x100000, one IO page apart) map to the same backing bytes: the raster order of
// the reference decides the surviving value (the later pixel, x=512). Sorting the writes by DMA offset would leave the other one.
static void analytic_alias(){
 for(bool concurrent:{false,true}){
  PpuState a,b;setup(a);setup(b);
  if(concurrent){a.memory.enable_concurrency();b.memory.enable_concurrency();}
  for(auto* s:{&a,&b}){
   for(unsigned x=0;x<513;++x){
    const bool opaque=x==0 || x==512;
    s->memory.store(0xc0001000+x*4,1,opaque?0xff:0x00);
    for(unsigned k=1;k<4;++k)s->memory.store(0xc0001000+x*4+k,1,x==0?0x10+k:x==512?0x40+k:0x70+k);
   }
  }
  Case e{};e.op=0;e.src_format=3;e.dst_format=10;e.origin=2;e.w=513;e.h=1;e.src_pitch=513*4;e.ow=513;e.oh=1;e.cw=513;e.ch=1;
  e.dx=e.dy=0x100000;e.src_io=false;e.dst_io=true;e.src_offset=0x1000;e.dst_offset=0;e.swz=true;e.swz_w=12;e.swz_h=12;e.io1=3;   // io1=3: IO page 1 is an alias of page 0
  require_same(a,b,e,"aliased destination");
  const std::uint8_t want[4]={0xff,0x41,0x42,0x43};   // pixel 512 is written after pixel 0
  for(unsigned k=0;k<4;++k)if(unsigned(a.memory.load(0x70000000+k,1))!=want[k])
   throw std::runtime_error("aliased destination byte "+std::to_string(k)+" is "+std::to_string(unsigned(a.memory.load(0x70000000+k,1)))+" expected the later pixel");
 }
 std::puts("PASS destination aliases keep the reference's raster order (serial and concurrent)");
}
// (1b) The review's case, where DMA-offset order and raster order disagree: Morton destination 512x256 (log_w 9, log_h 8),
// SRCCOPY_AND with only A=(256,0) and B=(0,128) opaque. By the Morton interleave (x bit i -> index bit 2i, y bit i -> bit 2i+1)
// A lands at byte 0x40000 and B at 0x20000; the guest pages EA+0x20000 and EA+0x40000 share one 64 KiB backing. The reference writes
// A first (row 0) and B last (row 128), so B's colour survives; a write order sorted by DMA offset would leave A's.
static void analytic_alias_raster(){
 for(bool concurrent:{false,true}){
  PpuState a,b;setup(a);setup(b);
  for(auto* s:{&a,&b}){
   auto& mem=s->memory;
   auto shared=std::make_shared<GuestBacking>(0x10000,0);
   mem.map(0x70400000,0x20000,6,nullptr,0);mem.map(0x70430000,0x10000,6,nullptr,0);mem.map(0x70450000,0x30000,6,nullptr,0);
   mem.regions.push_back({0x70420000,6,shared});mem.regions.push_back({0x70440000,6,shared});mem.mapping_changed();
   s->rsx.io[4]=0x70400000;
   if(concurrent)mem.enable_concurrency();
   for(unsigned y=0;y<129;++y)for(unsigned x=0;x<257;++x){
    const bool is_a=x==256 && y==0,is_b=x==0 && y==128;const std::uint64_t at=0xc0001000+y*1028+x*4;
    mem.store(at,1,is_a||is_b?0xff:0x00);for(unsigned k=1;k<4;++k)mem.store(at+k,1,is_a?0x10+k:is_b?0x40+k:0x70+k);}
  }
  Case e{};e.op=0;e.src_format=3;e.dst_format=10;e.origin=2;e.w=257;e.h=129;e.src_pitch=1028;e.ow=257;e.oh=129;e.cw=257;e.ch=129;
  e.dx=e.dy=0x100000;e.src_io=false;e.dst_io=true;e.src_offset=0x1000;e.dst_offset=0x400000;e.swz=true;e.swz_w=9;e.swz_h=8;
  require_same(a,b,e,"aliased destination (raster order)");
  const std::uint8_t want[4]={0xff,0x41,0x42,0x43};   // B, written after A
  for(auto* s:{&a,&b})for(std::uint64_t base:{0x70420000ull,0x70440000ull})for(unsigned k=0;k<4;++k)
   if(unsigned(s->memory.load(base+k,1))!=want[k])
    throw std::runtime_error(std::string(s==&a?"":"reference: ")+"aliased destination byte "+std::to_string(base+k)+" is "+std::to_string(unsigned(s->memory.load(base+k,1)))+" expected B's");
 }
 std::puts("PASS destination aliases out of DMA-offset order keep the reference's raster order (serial and concurrent)");
}
// Observable effects (review findings 2/3 of cd781d8 and 1-4 of review-warmup-blit-2): the production transfer and the frozen
// reference run the same case, each with its own recording observer; errors, the callback traces (guest address, size, write)
// and the guest windows must be identical. The observer may be filtered to watched bytes, throw once at a given callback, or
// change a guest byte at a given callback (through its host pointer, as a coherence readback would).
struct Call {std::uint64_t at,size;bool write;bool operator==(const Call& o)const{return at==o.at && size==o.size && write==o.write;}};
struct Effects {std::string error;std::vector<Call> calls;std::vector<std::uint8_t> memory;};
struct ObserverSpec {
 std::vector<std::pair<std::uint64_t,unsigned>> watch;   // empty: unfiltered (every access is of interest)
 int throw_at=-1;                                         // one-shot exception at this callback index
 int poke_at=-1;std::uint64_t poke_address=0;std::uint8_t poke_value=0;   // at this callback, store a byte (host pointer)
 int unwatch_at=-1;   // at this callback, remove every watch (as a coherence synchronization drops its interest)
 int add_at=-1;std::vector<std::pair<std::uint64_t,unsigned>> add{};   // at this callback, add these watches (interest grows)
 std::uint64_t touch_at=0;unsigned touch_size=0;int touch_throw=-1;  // one-shot exception at the n-th callback overlapping this range
};
static std::uint8_t* host_of(PpuState& s,std::uint64_t address){
 for(const auto& r:s.memory.regions)if(address>=r.base && address-r.base<r.bytes->size())return r.bytes->data()+(address-r.base);
 throw std::runtime_error("test: unmapped guest address");
}
static Effects observed(PpuState& s,const Case& c,const ObserverSpec& spec,bool reference){
 Effects out;int index=0,touches=0;
 auto guest=[&](const void* p,std::uint64_t& at){const auto* q=static_cast<const std::uint8_t*>(p);
  for(const auto& r:s.memory.regions){const auto* base=r.bytes->data();if(q>=base && q<base+r.bytes->size()){at=r.base+(q-base);return true;}}
  return false;};
 s.memory.host_access_observer=[&](const void* p,std::uint64_t n,bool write){
  std::uint64_t at=0;if(!guest(p,at))at=~0ull;
  out.calls.push_back({at,n,write});const int me=index++;
  if(me==spec.poke_at)*host_of(s,spec.poke_address)=spec.poke_value;
  if(me==spec.unwatch_at)for(const auto& [w,n]:spec.watch)s.memory.host_access_observer.watch(host_of(s,w),n,false);
  if(me==spec.add_at)for(const auto& [w,n]:spec.add)s.memory.host_access_observer.watch(host_of(s,w),n);
  if(spec.touch_size && at<spec.touch_at+spec.touch_size && spec.touch_at<at+n && touches++==spec.touch_throw)throw std::runtime_error("injected destination observer failure");
  if(me==spec.throw_at)throw std::runtime_error("injected observer failure");};
 if(!spec.watch.empty()){s.memory.host_access_observer.filtered=true;for(const auto& [at,n]:spec.watch)s.memory.host_access_observer.watch(host_of(s,at),n);}
 out.error=reference?run_ref(s,c):run(s,c);
 s.memory.host_access_observer={};
 out.memory=dump(s);return out;
}
static std::string describe(const std::vector<Call>& calls){
 std::string text;for(const auto& c:calls){char b[64];std::snprintf(b,sizeof b,"%s%llx+%llu ",c.write?"w":"r",(unsigned long long)c.at,(unsigned long long)c.size);text+=b;}
 return text.empty()?"(none)":text;
}
// Returns how many fast-path transfers (row-wise + batch) completed for the production run.
static std::uint64_t same_effects(const Case& c,const ObserverSpec& spec,const std::function<void(PpuState&)>& init,const char* what){
 PpuState a,b;setup(a);setup(b);init(a);init(b);
 const auto fast_before=rsxblit::rows_executed+rsxblit::batch_executed;
 const auto x=observed(a,c,spec,false);const auto fast=rsxblit::rows_executed+rsxblit::batch_executed-fast_before;
 const auto y=observed(b,c,spec,true);
 if(x.error!=y.error)throw std::runtime_error(std::string(what)+": error '"+x.error+"' reference '"+y.error+"'");
 if(!(x.calls==y.calls))throw std::runtime_error(std::string(what)+": callbacks "+describe(x.calls)+"reference "+describe(y.calls));
 if(x.memory!=y.memory){std::size_t at=0;while(x.memory[at]==y.memory[at])++at;throw std::runtime_error(std::string(what)+": memory differs from the reference at dump byte "+std::to_string(at));}
 return fast;
}
static void analytic_observer(){
 // 1x2 opaque SRCCOPY_AND, linear IO source rows 0x70001000/0x70003000 (pitch 8 KiB) -> VRAM rows 0xc0001000/0xc0003000.
 const auto init=[](PpuState& s){for(unsigned i=0;i<4;++i){
  s.memory.store(0x70001000+i,1,i==0?0xff:0x20+i);s.memory.store(0x70003000+i,1,i==0?0xff:0x30+i);
  for(unsigned k=0;k<0x4000;k+=0x400)s.memory.store(0xc0001000+k+i,1,0x5a);}};
 Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=1;c.h=2;c.src_pitch=8192;c.ow=1;c.oh=2;c.cw=1;c.ch=2;c.dst_pitch=8192;
 c.dx=c.dy=0x100000;c.src_io=true;c.src_offset=0x1000;c.dst_io=false;c.dst_offset=0x1000;
 // Unfiltered observer: every access is observable, so the exact reference sequence must appear (no fast path).
 if(same_effects(c,{},init,"unfiltered observer"))throw std::runtime_error("a fast path ran although every access is observed");
 // A watched gap byte the reference never touches: the fast path may run, no callback on either side.
 if(same_effects(c,{{{0x70002000,4}}},init,"watched gap byte")!=1)throw std::runtime_error("the batch path declined a transfer whose touched bytes are unobserved");
 // A watched touched byte (second row): its row runs through the reference (row 0 may be a batch segment); callbacks, errors
 // and bytes equal the reference's.
 same_effects(c,{{{0x70003000,4}}},init,"watched source byte");
 same_effects(c,{{{0xc0003000,4}}},init,"watched destination byte");
 // Callback order: the first destination acquisition rewrites the second source pixel (alpha 0 -> discarded in the reference).
 {ObserverSpec poke;poke.poke_at=2;poke.poke_address=0x70003000;poke.poke_value=0;same_effects(c,poke,init,"observer rewrites a later source pixel");}
 // Interest that the reference's first access removes (a coherence sync): after one reference row the remainder is admitted and
 // finished by the batch path; the callback also rewrites the second row's source colour, which both must read.
 {ObserverSpec sync;sync.watch={{0x70001000,4}};sync.unwatch_at=0;sync.poke_at=0;sync.poke_address=0x70003001;sync.poke_value=0x99;
  const auto escapes=rsxblit::prefix_escapes;same_effects(c,sync,init,"interest removed by the first access");
  if(rsxblit::prefix_escapes!=escapes+1)throw std::runtime_error("the remainder after a removed interest did not take the batch path");
  // The same with interest that stays: no escape, the reference finishes.
  ObserverSpec stay;stay.watch={{0x70001000,4},{0x70003000,4}};stay.unwatch_at=-1;
  const auto escapes2=rsxblit::prefix_escapes;same_effects(c,stay,init,"persistent interest");
  if(rsxblit::prefix_escapes!=escapes2)throw std::runtime_error("a remainder with observed bytes took the batch path");}
 // Three rows, only the middle source row watched (persistent): rows 0 and 2 are batch segments, row 1 the reference; the middle
 // row's callback rewrites row 2's source colour, which row 2 (read after row 1 in raster order) must see on both sides.
 {Case t=c;t.h=3;t.oh=3;t.ch=3;
  const auto init3=[&init](PpuState& s){init(s);for(unsigned i=0;i<4;++i)s.memory.store(0x70005000+i,1,i==0?0xff:0x50+i);};
  ObserverSpec middle;middle.watch={{0x70003000,4}};middle.poke_at=0;middle.poke_address=0x70005002;middle.poke_value=0x88;
  const auto escapes=rsxblit::prefix_escapes;same_effects(t,middle,init3,"observed middle row");
  if(rsxblit::prefix_escapes!=escapes+1)throw std::runtime_error("the unobserved rows around an observed one were not batched");
  // The same callback rewriting row 0's source: row 0 was read before it on both sides (old colour stays).
  middle.poke_address=0x70001002;same_effects(t,middle,init3,"observed middle row rewrites an earlier row");}
 // One-shot observer failures at every position propagate exactly like the reference's.
 for(int at=0;at<6;++at){ObserverSpec one;one.throw_at=at;same_effects(c,one,init,"one-shot observer failure");}
 // First fault: destination on the read-only IO page 3; the observer throws on the second source row. The reference acquires the
 // first destination (permission fault) before it reads the second source row.
 {Case f=c;f.dst_io=true;f.dst_offset=0x300000;f.dst_pitch=8192;ObserverSpec one;one.throw_at=-1;
  for(int at=-1;at<4;++at){one.throw_at=at;same_effects(f,one,init,"first fault (read-only destination)");}}
 // Row path: untiled identity SRCCOPY 2x2 IO -> VRAM, one-shot failures and a watched destination byte.
 {Case r{};r.op=3;r.src_format=3;r.dst_format=10;r.origin=2;r.w=2;r.h=2;r.src_pitch=8;r.ow=2;r.oh=2;r.cw=2;r.ch=2;r.dst_pitch=8;
  r.dx=r.dy=0x100000;r.src_io=true;r.src_offset=0x1000;r.dst_io=false;r.dst_offset=0x2000;
  const auto rows=[](PpuState& s){for(unsigned i=0;i<16;++i)s.memory.store(0x70001000+i,1,0x40+i);};
  if(same_effects(r,{{{0x70020000,4}}},rows,"row path, unrelated watch")!=1)throw std::runtime_error("the row path declined an unobserved transfer");
  for(int at=0;at<4;++at){ObserverSpec one;one.throw_at=at;same_effects(r,one,rows,"row path one-shot failure");}
  same_effects(r,{{{0xc0002008,4}}},rows,"row path, watched destination");}
 // Row path covering read: 2048x1 -> 2x1 nearest downscale (dx 0x7ff00000) taps 0x70000800 and 0x700027fc; a watched byte at
 // 0x70001800 between them is never touched by the reference, so no callback may report it.
 {Case d{};d.op=3;d.src_format=3;d.dst_format=10;d.origin=2;d.w=2048;d.h=1;d.src_pitch=8192;d.ow=2;d.oh=1;d.cw=2;d.ch=1;d.dst_pitch=8;
  d.dx=d.dy=0x7ff00000;d.src_io=true;d.src_offset=0x800;d.dst_io=false;d.dst_offset=0x2000;
  const auto taps=[](PpuState& s){for(unsigned k=0;k<4;++k){s.memory.store(0x70000800+k,1,0x60+k);s.memory.store(0x700027fc+k,1,0x70+k);}};
  const auto rows_before=rsxblit::rows_executed;
  same_effects(d,{{{0x70001800,4}}},taps,"covering read over a watched untouched byte");
  if(rsxblit::rows_executed!=rows_before)throw std::runtime_error("the row path read across a watched byte");}
 std::puts("PASS observer callbacks, one-shot failures and first faults equal the frozen reference's");
}
// Review-3 finding 2: a callback of a later reference row ADDS interest in the destination of an earlier batch row segment. The
// reference acquired that destination before the watch existed and writes it in phase B pixel by pixel (serial: mark_write + a
// direct 4-byte copy each, one write notification per pixel); a re-acquiring 8-byte batch write would report other callbacks.
// 2x2 opaque SRCCOPY_AND, rows on separate pages; only source row 1 is watched at the start, so row 0 is a batch segment.
static void analytic_growing_interest(){
 const auto init=[](PpuState& s){for(unsigned x=0;x<2;++x)for(unsigned i=0;i<4;++i){
  s.memory.store(0x70001000+x*4+i,1,i==0?0xff:0x20+x*4+i);s.memory.store(0x70003000+x*4+i,1,i==0?0xff:0x40+x*4+i);
  s.memory.store(0xc0001000+x*4+i,1,0x5a);s.memory.store(0xc0003000+x*4+i,1,0x5a);}};
 Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=2;c.h=2;c.src_pitch=8192;c.ow=2;c.oh=2;c.cw=2;c.ch=2;c.dst_pitch=8192;
 c.dx=c.dy=0x100000;c.src_io=true;c.src_offset=0x1000;c.dst_io=false;c.dst_offset=0x1000;
 ObserverSpec grow;grow.watch={{0x70003000,8}};grow.add_at=0;grow.add={{0xc0001000,8}};
 const auto fallbacks=rsxblit::deferred_fallbacks,escapes=rsxblit::prefix_escapes;
 same_effects(c,grow,init,"interest added on an earlier batch segment's destination");
 if(rsxblit::prefix_escapes!=escapes+1 || rsxblit::deferred_fallbacks!=fallbacks+1)
  throw std::runtime_error("row 0 was not a batch segment written with the reference's phase-B writes");
 // Independent expectation of the trace tail: the two destination row 0 pixels are written one by one (4 bytes each, in order).
 {PpuState a;setup(a);init(a);const auto x=observed(a,c,grow,false);
  if(x.calls.size()<2 || !(x.calls[x.calls.size()-2]==Call{0xc0001000,4,true}) || !(x.calls.back()==Call{0xc0001004,4,true}))
   throw std::runtime_error("growing interest: last callbacks "+describe(x.calls)+"expected w c0001000+4 w c0001004+4");}
 // Partial failure: the second notification concerning destination row 0 throws. The reference has written pixel (0,0), not (1,0).
 ObserverSpec fail=grow;fail.touch_at=0xc0001000;fail.touch_size=8;fail.touch_throw=1;
 same_effects(c,fail,init,"second destination-row notification fails");
 {PpuState a;setup(a);init(a);const auto x=observed(a,c,fail,false);
  if(x.error!="injected destination observer failure" || unsigned(a.memory.load(0xc0001001,1))!=0x21 || unsigned(a.memory.load(0xc0001005,1))!=0x5a)
   throw std::runtime_error("partial failure: error '"+x.error+"' or wrong pixels written");}
 // Interest added on an untouched page (observation is page-granular without exact bytes) leaves the deferred segment fast.
 ObserverSpec gap=grow;gap.add={{0xc0002000,8}};
 const auto fallbacks2=rsxblit::deferred_fallbacks;same_effects(c,gap,init,"interest added on an untouched gap");
 if(rsxblit::deferred_fallbacks!=fallbacks2)throw std::runtime_error("an unaffected deferred segment fell back");
 std::puts("PASS interest added during reference rows: deferred batch segments write like the reference's phase B");
}
// Review-3 finding 1: admission must not service page-broker claims (coherence, pump, wait) nor swallow their failures. Concurrent
// memory with the broker; GPU claims on source pages; the coherence handler records the order of serviced pages and releases the
// claim (or throws once). Errors, serviced-page order and guest bytes equal the frozen reference's.
static void analytic_broker(){
 struct Variant {const char* what;bool claim0,claim1,throw_first,read_only_dst,mirrored;};
 const Variant variants[]={
  {"claimed first source page, coherence fails once",true,false,true,false,false},
  {"claimed second source page, read-only destination faults first",false,true,false,true,false},
  {"claimed first source page, released by coherence",true,false,false,false,false},
  {"two claimed source pages read in mirrored raster order",true,true,false,false,true}};
 for(const auto& v:variants){
  std::string error[2];std::vector<int> order[2];std::vector<std::uint8_t> memory[2];
  for(int side=0;side<2;++side){
   PpuState s;setup(s);auto& m=s.memory;
   for(unsigned i=0;i<4;++i){m.store(0x70001000+i,1,i==0?0xff:0x20+i);m.store(0x70003000+i,1,i==0?0xff:0x30+i);
    for(unsigned k=0;k<0x4000;k+=0x400)m.store(0xc0001000+k+i,1,0x5a);}
   m.enable_concurrency();m.enable_page_broker();
   std::shared_ptr<GuestBacking> io;for(const auto& r:m.regions)if(r.base==0x70000000)io=r.bytes;
   std::unique_ptr<GuestMemory::GpuPages> claims[2];
   if(v.claim0)claims[0]=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{host_of(s,0x70001000),4,io}});
   if(v.claim1)claims[1]=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{host_of(s,0x70003000),4,io}});
   bool thrown=false;
   m.page_broker.coherence=[&](const std::vector<GuestMemory::HostSpan>& spans,bool){
    int which=-1;for(int i=0;i<2;++i)if(claims[i])for(const auto& span:spans)
     if(reinterpret_cast<std::uintptr_t>(span.pointer)>>12==reinterpret_cast<std::uintptr_t>(host_of(s,i?0x70003000:0x70001000))>>12)which=i;
    order[side].push_back(which);
    if(v.throw_first && !thrown){thrown=true;throw std::runtime_error("injected coherence failure");}
    if(which>=0)claims[which].reset();};
   Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=1;c.h=2;c.src_pitch=8192;c.ow=1;c.oh=2;c.cw=1;c.ch=2;c.dst_pitch=8192;
   c.dx=c.dy=0x100000;c.src_io=true;c.src_offset=0x1000;c.dst_io=false;c.dst_offset=0x1000;
   if(v.read_only_dst){c.dst_io=true;c.dst_offset=0x300000;}
   if(v.mirrored){c.dy=-0x100000;c.point=16u<<16;}   // point is in 1/16 pixel: v = 1.0, output row 0 reads source row 1, row 1 reads row 0
   error[side]=side?run_ref(s,c):run(s,c);
   m.page_broker.coherence={};claims[0].reset();claims[1].reset();
   memory[side]=dump(s);
  }
  if(error[0]!=error[1])throw std::runtime_error(std::string(v.what)+": error '"+error[0]+"' reference '"+error[1]+"'");
  if(order[0]!=order[1]){std::string a,b;for(int i:order[0])a+=std::to_string(i)+" ";for(int i:order[1])b+=std::to_string(i)+" ";
   throw std::runtime_error(std::string(v.what)+": serviced pages "+a+"reference "+b);}
  if(memory[0]!=memory[1])throw std::runtime_error(std::string(v.what)+": memory differs from the reference");
 }
 std::puts("PASS page-broker claims are declined by admission and serviced only by the reference's accesses");
}
// Protected pages (HostGuard, as RSX surfaces in guest memory): the first access faults, disarms the guard and runs its
// synchronization. The batch declines guarded bytes; after one reference row the guard is gone (source guard) and the remainder
// is admitted. A destination guard faults only in the write phase; a remainder on other pages is admitted and written after the
// reference's prefix pixels (as in the reference), a remainder on the guarded page is not: the reference finishes.
static void analytic_guard(){
 for(int variant=0;variant<4;++variant){   // 0: source row 0 page guarded, 1: destination row 0 page, 2: one page holding both destination rows,
  // 3: as 0 in concurrent memory with exact-byte observation (Range accesses use the coherent alias: no fault on either side)
  PpuState a,b;setup(a);setup(b);
  if(variant==3)for(auto* t:{&a,&b}){t->memory.host_access_observer.supports_exact_bytes=true;t->memory.enable_concurrency();}
  const auto batches=rsxblit::batch_executed;
  std::string error[2];std::vector<std::uint8_t> memory[2];unsigned syncs[2]={0,0};
  const auto escapes=rsxblit::prefix_escapes;
  for(int side=0;side<2;++side){
   PpuState& s=side?b:a;
   for(unsigned i=0;i<4;++i){s.memory.store(0x70001000+i,1,i==0?0xff:0x20+i);s.memory.store(0x70003000+i,1,i==0?0xff:0x30+i);
    for(unsigned k=0;k<0x4000;k+=0x400)s.memory.store(0xc0001000+k+i,1,0x5a);}
   Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=1;c.h=2;c.src_pitch=8192;c.ow=1;c.oh=2;c.cw=1;c.ch=2;c.dst_pitch=8192;
   c.dx=c.dy=0x100000;c.src_io=true;c.src_offset=0x1000;c.dst_io=false;c.dst_offset=0x1000;if(variant==2)c.dst_pitch=8;
   const std::uint64_t guarded=variant==1 || variant==2?0xc0001000:0x70001000;
   std::shared_ptr<GuestBacking> owner;for(const auto& r:s.memory.regions)if(guarded>=r.base && guarded-r.base<r.bytes->size())owner=r.bytes;
   {GuestMemory::HostGuard guard;
    guard.synchronize=[&,side]{++syncs[side];*host_of(s,0x70003001)=0x99;*host_of(s,0xc0001010)=0x77;*host_of(s,0xc0003001)=0x66;};
    if(!guard.add(host_of(s,guarded),4,*owner))throw std::runtime_error("test: guard.add failed");
    guard.arm();
    error[side]=side?run_ref(s,c):run(s,c);
    guard.disarm();}
   memory[side]=dump(s);
  }
  if(error[0]!=error[1])throw std::runtime_error("guard variant "+std::to_string(variant)+": error '"+error[0]+"' reference '"+error[1]+"'");
  if(syncs[0]!=(variant==3?0u:1u) || syncs[1]!=syncs[0])throw std::runtime_error("guard variant "+std::to_string(variant)+": synchronizations "+std::to_string(syncs[0])+" reference "+std::to_string(syncs[1]));
  if(memory[0]!=memory[1])throw std::runtime_error("guard variant "+std::to_string(variant)+": memory differs from the reference");
  if(variant==0 && unsigned(a.memory.load(0xc0003001,1))!=0x99)throw std::runtime_error("guard variant "+std::to_string(variant)+": second pixel did not read the synchronized source");
  if(rsxblit::prefix_escapes!=escapes+(variant>=2?0:1))throw std::runtime_error("guard variant "+std::to_string(variant)+": unexpected remainder escape count");
  if(variant==3 && rsxblit::batch_executed!=batches+1)throw std::runtime_error("guard variant 3: the batch path did not run although no access can fault");
 }
 std::puts("PASS guarded source/destination pages: remainder batch or reference, equal to the frozen reference");
}
// (4) A 2x2 nearest downscale of a 2048x2048 source taps four pixels: the planner must evaluate those addresses (and the four
// destination pixels), not the 4 Mi pixel rectangle. rsxblit::locate_calls counts every Image::locate evaluation (inside the function).
static void analytic_sparse(){
 PpuState a,b;setup(a,0x1100000);setup(b,0x1100000);
 const unsigned taps[4][2]={{0,0},{2047,0},{0,2047},{2047,2047}};
 for(auto* s:{&a,&b})for(unsigned i=0;i<4;++i){
  const std::uint64_t at=0xc0000000+0x2000+std::uint64_t(taps[i][1])*8192+taps[i][0]*4;
  s->memory.store(at,1,0xff);for(unsigned k=1;k<4;++k)s->memory.store(at+k,1,0x10*(i+1)+k);}
 Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=2048;c.h=2048;c.src_pitch=8192;c.ow=2;c.oh=2;c.cw=2;c.ch=2;c.dst_pitch=8;
 c.dx=c.dy=0x7ff00000;c.src_io=false;c.src_offset=0x2000;c.dst_io=false;c.dst_offset=0x1000;
 const auto before=rsxblit::locate_calls,batches=rsxblit::batch_executed;
 const auto e=run(a,c);if(!e.empty())throw std::runtime_error("sparse downscale failed: "+e);
 const auto work=rsxblit::locate_calls-before;   // counted inside Image::locate: at least the 4 taps + 4 destination pixels
 if(rsxblit::batch_executed!=batches+1 || work<8 || work>16)throw std::runtime_error("sparse downscale evaluated "+std::to_string(work)+" addresses (batch path executed "+std::to_string(rsxblit::batch_executed-batches)+" times)");
 const auto er=run_ref(b,c);if(!er.empty())throw std::runtime_error("reference failed: "+er);
 for(unsigned i=0;i<4;++i)for(unsigned k=0;k<4;++k){
  const unsigned want=k==0?0xffu:0x10*(i+1)+k;const std::uint64_t at=0xc0001000+(i/2)*8+(i%2)*4+k;
  if(unsigned(a.memory.load(at,1))!=want || unsigned(b.memory.load(at,1))!=want)throw std::runtime_error("sparse downscale pixel "+std::to_string(i)+" byte "+std::to_string(k)+" is wrong");}
 std::puts("PASS sparse downscale plans only the taps it uses");
}
int main(){
 try{analytic();analytic_layouts();analytic_alias();analytic_alias_raster();analytic_observer();analytic_growing_interest();analytic_broker();analytic_guard();analytic_sparse();differential(false);std::puts("PASS row-wise equals per-pixel (serial)");differential(true);std::puts("PASS row-wise equals per-pixel (concurrent)");}
 catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/vulkan/vulkan.h').exists(),
                     'Requires C++ and Vulkan development headers')
class RsxBlitRowsTests(unittest.TestCase):
    def test_row_wise_transfer_equals_per_pixel_reference(self):
        with tempfile.TemporaryDirectory(prefix='anyps3-rsxblitrows-') as directory:
            root = Path(directory)
            source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
            source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
            (root / 'blit.cpp').write_text(source + FROZEN_CPP + CPP)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread',
                                    '-DANYPS3_VULKAN', str(root / 'blit.cpp'), '-lvulkan', '-o', str(root / 'blit')],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = os.environ.copy()
            lavapipe = Path('/usr/share/vulkan/icd.d/lvp_icd.json')
            if lavapipe.exists() and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env['VK_DRIVER_FILES'] = str(lavapipe)
            run = subprocess.run([str(root / 'blit')], capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(run.stdout.count('PASS '), 11, run.stdout)


if __name__ == '__main__':
    unittest.main()
