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
// (2) Only the bytes the reference touches may be reported to an observer: a 1x2 copy with 8 KiB pitches must not notify the gap.
// (3) An observer failure must stop the transfer (the reference propagates it), never be retried through another path.
static void analytic_observer(){
 PpuState a,b;setup(a);setup(b);
 const auto* io_base=a.memory.range(0x70000000,1,false).parts[0].data;const auto* vram_base=a.memory.range(0xc0000000,1,false).parts[0].data;
 for(auto* s:{&a,&b})for(unsigned i=0;i<4;++i){
  s->memory.store(0x70001000+i,1,i==0?0xff:0x20+i);s->memory.store(0x70003000+i,1,i==0?0xff:0x30+i);
  for(unsigned k=0;k<0x4000;k+=0x400)s->memory.store(0xc0001000+k+i,1,0x5a);}
 Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=1;c.h=2;c.src_pitch=8192;c.ow=1;c.oh=2;c.cw=1;c.ch=2;c.dst_pitch=8192;
 c.dx=c.dy=0x100000;c.src_io=true;c.src_offset=0x1000;c.dst_io=false;c.dst_offset=0x1000;
 struct Seen {std::uint64_t at,size;bool write;};std::vector<Seen> seen;
 bool fail_once=false;
 auto guest=[&](const void* p,std::uint64_t& at){
  const auto* q=static_cast<const std::uint8_t*>(p);
  if(q>=io_base && q<io_base+0x300000){at=0x70000000+(q-io_base);return true;}
  if(q>=vram_base && q<vram_base+0x80000){at=0xc0000000+(q-vram_base);return true;}
  return false;};
 const auto observer=[&](const void* p,std::uint64_t n,bool write){
  std::uint64_t at=0;if(!guest(p,at))return;
  seen.push_back({at,n,write});
  if(fail_once && !write){fail_once=false;throw std::runtime_error("injected observer failure");}};
 // (2) coverage: the observer watches only the transfer itself, not the memory dumps of the comparison.
 a.memory.host_access_observer=observer;
 const auto e_fast=run(a,c);
 a.memory.host_access_observer={};
 const auto e_slow=run_ref(b,c);
 if(!e_fast.empty() || e_fast!=e_slow)throw std::runtime_error("observer coverage: error '"+e_fast+"' reference '"+e_slow+"'");
 if(dump(a)!=dump(b))throw std::runtime_error("observer coverage: destination differs from the reference");
 if(seen.empty())throw std::runtime_error("the observer saw no access");
 const std::uint64_t allowed[4]={0x70001000,0x70003000,0xc0001000,0xc0003000};
 for(const auto& e:seen){bool inside=false;for(auto base:allowed)if(e.at>=base && e.at+e.size<=base+4)inside=true;
  if(!inside)throw std::runtime_error("observer notified guest bytes ["+std::to_string(e.at)+",+"+std::to_string(e.size)+") the transfer never touches");}
 // (3) propagation: the first observed read fails once; the destination must stay untouched and the same error must surface.
 for(unsigned k=0;k<4;++k){a.memory.store(0xc0001000+k,1,0x5a);b.memory.store(0xc0001000+k,1,0x5a);a.memory.store(0xc0003000+k,1,0x5a);b.memory.store(0xc0003000+k,1,0x5a);}
 seen.clear();fail_once=true;
 a.memory.host_access_observer=observer;
 const auto error=run(a,c);
 a.memory.host_access_observer={};
 if(error!="injected observer failure")throw std::runtime_error("observer failure was swallowed or replaced: '"+error+"'");
 b.memory.host_access_observer=[&](const void*,std::uint64_t,bool write){if(!write)throw std::runtime_error("injected observer failure");};
 const auto reference=run_ref(b,c);
 b.memory.host_access_observer={};
 if(reference!=error)throw std::runtime_error("reference error '"+reference+"'");
 for(unsigned k=0;k<4;++k)if(unsigned(a.memory.load(0xc0001000+k,1))!=0x5a || unsigned(a.memory.load(0xc0003000+k,1))!=0x5a)
  throw std::runtime_error("a failed transfer wrote destination bytes");
 std::puts("PASS observer sees exactly the touched bytes and its failure propagates");
}
// (4) A 2x2 nearest downscale of a 2048x2048 source taps four pixels: the planner must enumerate those, not the 4 Mi pixel rectangle.
static void analytic_sparse(){
 PpuState a,b;setup(a,0x1100000);setup(b,0x1100000);
 const unsigned taps[4][2]={{0,0},{2047,0},{0,2047},{2047,2047}};
 for(auto* s:{&a,&b})for(unsigned i=0;i<4;++i){
  const std::uint64_t at=0xc0000000+0x2000+std::uint64_t(taps[i][1])*8192+taps[i][0]*4;
  s->memory.store(at,1,0xff);for(unsigned k=1;k<4;++k)s->memory.store(at+k,1,0x10*(i+1)+k);}
 Case c{};c.op=0;c.src_format=3;c.dst_format=10;c.origin=2;c.w=2048;c.h=2048;c.src_pitch=8192;c.ow=2;c.oh=2;c.cw=2;c.ch=2;c.dst_pitch=8;
 c.dx=c.dy=0x7ff00000;c.src_io=false;c.src_offset=0x2000;c.dst_io=false;c.dst_offset=0x1000;
 const auto before=rsxblit::batch_tap_work,batches=rsxblit::batch_executed;
 const auto e=run(a,c);if(!e.empty())throw std::runtime_error("sparse downscale failed: "+e);
 const auto work=rsxblit::batch_tap_work-before;
 if(rsxblit::batch_executed!=batches+1 || work>8)throw std::runtime_error("sparse downscale planned "+std::to_string(work)+" taps (batch path executed "+std::to_string(rsxblit::batch_executed-batches)+" times)");
 const auto er=run_ref(b,c);if(!er.empty())throw std::runtime_error("reference failed: "+er);
 for(unsigned i=0;i<4;++i)for(unsigned k=0;k<4;++k){
  const unsigned want=k==0?0xffu:0x10*(i+1)+k;const std::uint64_t at=0xc0001000+(i/2)*8+(i%2)*4+k;
  if(unsigned(a.memory.load(at,1))!=want || unsigned(b.memory.load(at,1))!=want)throw std::runtime_error("sparse downscale pixel "+std::to_string(i)+" byte "+std::to_string(k)+" is wrong");}
 std::puts("PASS sparse downscale plans only the taps it uses");
}
int main(){
 try{analytic();analytic_layouts();analytic_alias();analytic_alias_raster();analytic_observer();analytic_sparse();differential(false);std::puts("PASS row-wise equals per-pixel (serial)");differential(true);std::puts("PASS row-wise equals per-pixel (concurrent)");}
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
            self.assertEqual(run.stdout.count('PASS '), 8, run.stdout)


if __name__ == '__main__':
    unittest.main()
