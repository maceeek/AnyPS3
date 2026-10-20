"""RSX scaled transfer: the row-wise and batch paths equal the per-pixel reference path byte for byte (native oracles).

rsxblit::scaled has a reference implementation (one guest Range per pixel, force_pixel_path), a row-wise path
(one Range per touched row, linear untiled SRCCOPY) and a batch path (everything else: bound tiles on either side,
Morton-swizzled destinations, SRCCOPY_AND). The differential test drives them on identical randomized transfers --
formats, pitches with padding, 1:1 / up / down / mirrored scales, filtering, clip rectangles, VRAM and IO operands,
overlapping in-place copies, bound tiles (several pitches and banks, source and destination), swizzled destinations,
zero-alpha discard, invalid parameters -- under serial and concurrent guest memory, and requires identical destination
bytes, untouched neighbours and identical errors. Analytic cases pin the expected bytes independently of every path:
exact 1:1 copy with row padding, 2x nearest upscale, a copy into and out of a bound tile (physical addresses derived
by hand from the RSX tile formula), a Morton-swizzled destination and SRCCOPY_AND discarding zero-alpha pixels.
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

CPP = r'''
#include <cassert>
static void setup(PpuState& s){
 auto& g=s.rsx;g.local_size=0x80000;s.memory.map(0xc0000000,g.local_size,6,nullptr,0);
 s.memory.map(0x70000000,0x300000,6,nullptr,0);g.io[0]=0x70000000;g.io[1]=0x70100000;g.io[2]=0x70200000;
 s.memory.map(0x70300000,0x10000,4,nullptr,0);g.io[3]=0x70300000;   // IO page 3 is read-only: destination writes fault
 s.memory.map(0x10000,0x2000,6,nullptr,0);g.dma=0x10000;g.fifo_enabled=true;
}
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
static unsigned src_bpp_of(unsigned f){return f==8?1:(f==1||f==2||f==7||f==9)?2:4;}
struct Case {
 unsigned src_format,dst_format,filter,origin,w,h,src_pitch,ow,oh,dst_pitch,ox,oy,cx,cy,cw,ch,point;
 std::int32_t dx,dy;bool src_io,dst_io,in_place,tile,src_tile,swz;unsigned src_offset,dst_offset;
 unsigned op,tile_pitch,tile_bank,src_tile_pitch,src_tile_bank;
 unsigned io1;   // IO page 1: 0 contiguous with page 0, 1 discontiguous, 2 unmapped hole
};
static void program_case(PpuState& s,const Case& c){
 auto& R=s.rsx.registers;
 R={{0xc184,c.src_io?0xfeed0001u:0xfeed0000u},{0xc198,c.swz?0x31337a73u:0x313371c3u},{0xc2fc,1},{0xc300,c.src_format},{0xc304,c.op},
    {0xc308,(c.cy<<16)|c.cx},{0xc310,(c.oy<<16)|c.ox},{0xc30c,(c.ch<<16)|c.cw},{0xc314,(c.oh<<16)|c.ow},
    {0xc318,std::uint32_t(c.dx)},{0xc31c,std::uint32_t(c.dy)},{0xc400,(c.h<<16)|c.w},
    {0xc404,(c.filter<<24)|(c.origin<<16)|c.src_pitch},{0xc408,c.src_offset},
    {0x6188,c.dst_io?0xfeed0001u:0xfeed0000u},{0x6300,c.dst_format},{0x6304,c.dst_pitch<<16},{0x630c,c.dst_offset}};
 if(c.swz){R[0x8300]=(5u<<24)|(6u<<16)|c.dst_format;R[0x8184]=c.dst_io?0xfeed0001u:0xfeed0000u;R[0x8304]=c.dst_offset;}
 s.rsx.io[1]=c.io1==0?0x70100000u:c.io1==1?0x70200000u:UINT32_MAX;
 auto& t=s.rsx.tiles[0];t=NativeRsx::Tile{};
 if(c.tile){t.bound=true;t.location=c.dst_io?1:0;t.offset=c.dst_offset&~0xffffu;t.size=0x10000;t.pitch=c.tile_pitch;t.bank=c.tile_bank;}
 auto& q=s.rsx.tiles[1];q=NativeRsx::Tile{};
 if(c.src_tile){q.bound=true;q.location=c.src_io?1:0;q.offset=c.src_offset&~0xffffu;q.size=0x10000;q.pitch=c.src_tile_pitch;q.bank=c.src_tile_bank;}
}
// Run one transfer; returns the error text ("" on success).
static std::string run(PpuState& s,const Case& c){
 program_case(s,c);
 try{rsxblit::scaled(s,c.point);}catch(const std::runtime_error& e){return e.what();}
 return "";
}
// Observed guest regions: linear IO, VRAM, both sides of the IO page 0/1 edge, the discontiguous page-1 alias, the read-only page.
struct Window {std::uint64_t address;unsigned size;bool writable;};
static const Window windows[]={{0x70000000,0x10000,true},{0xc0000000,0x20000,true},{0x700fe000,0x4000,true},{0x70200000,0x2000,true},{0x70300000,0x1000,false}};
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
 c.w=1+lcg(r)%40;c.h=1+lcg(r)%24;
 const unsigned bpp=src_bpp_of(c.src_format);
 c.src_pitch=c.w*bpp+(lcg(r)%3==0?4*(lcg(r)%5):0);
 c.ow=1+lcg(r)%40;c.oh=1+lcg(r)%24;c.ox=lcg(r)%4;c.oy=lcg(r)%3;
 const unsigned dbpp=c.dst_format==4?2:4;
 c.dst_pitch=(c.ox+c.ow)*dbpp+(lcg(r)%3==0?4*(lcg(r)%5):0);
 c.cx=lcg(r)%6;c.cy=lcg(r)%4;
 c.cw=1+lcg(r)%(c.ow+3);c.ch=1+lcg(r)%(c.oh+2);
 c.dx=scales[lcg(r)%8];c.dy=scales[lcg(r)%8];
 c.point=(lcg(r)%6)|((lcg(r)%4)<<16);
 c.src_io=lcg(r)&1;c.dst_io=lcg(r)&1;
 c.src_offset=0x1000*(c.src_io?1:1)+4*(lcg(r)%16);
 c.dst_offset=c.dst_io?0x4000+4*(lcg(r)%16):0x1000+4*(lcg(r)%16);
 c.in_place=lcg(r)%7==0;
 if(c.in_place){c.dst_io=c.src_io;c.dst_offset=c.src_offset+4*(lcg(r)%40);}
 c.tile=lcg(r)%12==0;
 static const unsigned pitches[]={256,512,768,1024,256,512,1280,2048};   // 1280/2048 push physical blocks past the 64 KiB tile: identical errors
 c.tile_pitch=pitches[lcg(r)%8];c.tile_bank=lcg(r)%4;c.src_tile_pitch=pitches[lcg(r)%8];c.src_tile_bank=lcg(r)%4;
 c.op=lcg(r)%5==0?0u:3u;
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
 if(c.src_tile && c.tile && c.src_io==c.dst_io)c.src_tile=false;   // two tiles over one pixel is an (identical) error, not the interesting case
 // Invalid parameters are part of the sweep: zero pitch, pitch below the row, unsupported combinations.
 if(lcg(r)%40==0)c.src_pitch=c.w*bpp>1?c.w*bpp-1:0;
 return c;
}
static void differential(bool concurrent){
 PpuState fast,slow;setup(fast);setup(slow);
 if(concurrent){fast.memory.enable_concurrency();slow.memory.enable_concurrency();}
 std::uint32_t r=concurrent?0x1234567u:0x7654321u;unsigned ok=0,failed=0,tiled=0;std::map<std::string,unsigned> errors;
 const auto start=rsxblit::rows_executed,start_batch=rsxblit::batch_executed;
 for(unsigned n=0;n<700;++n){
  const auto c=random_case(r);fill(fast,slow,r+n);
  rsxblit::force_pixel_path=false;const auto e_fast=run(fast,c);
  rsxblit::force_pixel_path=true;const auto e_slow=run(slow,c);
  rsxblit::force_pixel_path=false;
  if(e_fast!=e_slow)throw std::runtime_error("case "+std::to_string(n)+": fast error '"+e_fast+"' reference '"+e_slow+"'");
  const auto a=dump(fast),b=dump(slow);
  if(a!=b){unsigned at=0;while(a[at]==b[at])++at;throw std::runtime_error("case "+std::to_string(n)+": destination differs at byte "+std::to_string(at)+" (fmt "+std::to_string(c.src_format)+"->"+std::to_string(c.dst_format)+")");}
  e_fast.empty()?++ok:++failed;tiled+=c.tile||c.src_tile;if(!e_fast.empty())++errors[e_fast.substr(0,26)];
 }
 const auto taken=rsxblit::rows_executed-start,batched=rsxblit::batch_executed-start_batch;
 std::printf("INFO %s: %u transfers ok, %u rejected identically, %u with a bound tile, row-wise path taken %llu times, batch path %llu times\n",concurrent?"concurrent":"serial",ok,failed,tiled,(unsigned long long)taken,(unsigned long long)batched);
 for(const auto& [text,count]:errors)std::printf("INFO   %u x %s\n",count,text.c_str());
 std::fflush(stdout);
 assert(ok>450 && failed>5 && taken>100 && batched>150);
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
  c.dx=c.dy=0x100000;c.src_io=true;c.dst_io=false;c.src_offset=0x1000;c.dst_offset=0;c.tile=true;c.tile_pitch=256;c.tile_bank=0;
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
  d.dx=d.dy=0x100000;d.src_io=false;d.dst_io=true;d.src_offset=0;d.dst_offset=0x2000;d.src_tile=true;d.src_tile_pitch=256;d.src_tile_bank=0;
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
int main(){
 try{analytic();analytic_layouts();differential(false);std::puts("PASS row-wise equals per-pixel (serial)");differential(true);std::puts("PASS row-wise equals per-pixel (concurrent)");}
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
            (root / 'blit.cpp').write_text(source + CPP)
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
            self.assertEqual(run.stdout.count('PASS '), 4, run.stdout)


if __name__ == '__main__':
    unittest.main()
