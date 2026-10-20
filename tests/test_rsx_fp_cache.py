"""Fragment program loader: chunked reads, content sharing and bounded eviction equal the original 16-byte loader (native oracles).

The reference below is the literal original algorithm (one Range read per 16-byte block, no content cache). Random programs
(constant blocks, lengths 1..40, VRAM and IO locations, 256-byte-chunk and 1 MiB-page straddles, a program ending at the last
readable bytes, a program without END) must decode identically; equal bytes at different addresses share one prepared program;
more than 1024 distinct programs evict without ever returning a wrong program.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
using namespace rsxdraw;
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
struct Ref {unsigned d,s0,s1,s2;Vec4 constant;bool has_constant;unsigned word_offset;};
// The original loader, verbatim apart from returning plain records.
static std::vector<Ref> reference(PpuState& s,unsigned location,std::uint32_t start){
 std::vector<Ref> program;std::uint32_t offset=start;
 auto block=[&](std::uint32_t at){
  std::array<std::uint8_t,16> bytes{};rsxdraw::read_fragment_bytes(s,location,at,bytes.data(),16);
  std::array<unsigned,4> words{};
  for(unsigned i=0;i<4;++i){const auto w=read_be(bytes.data()+4*i,4);words[i]=(w<<16)|(w>>16);}return words;
 };
 for(unsigned n=0;n<4096;++n){
  const auto words=block(offset);Ref inst{words[0],words[1],words[2],words[3],{},false,(offset-start)/4};
  offset+=16;
  const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
  if(opcode<64 && (((inst.s0&3)==2) || ((inst.s1&3)==2) || ((inst.s2&3)==2))){
   const auto constants=block(offset);for(unsigned i=0;i<4;++i)inst.constant[i]=bits_float(constants[i]);inst.has_constant=true;offset+=16;}
  program.push_back(inst);
  if(inst.d&1)return program;
 }
 throw std::runtime_error("RSX fragment program has no end");
}
static void put_word(PpuState& s,std::uint64_t address,unsigned w){s.memory.store(address,4,(w<<16)|(w>>16));}
// Writes a random program; returns its byte length. `end` false leaves the last instruction without the END bit.
static unsigned write_program(PpuState& s,std::uint64_t address,std::uint32_t& r,unsigned instructions,bool end,unsigned salt,bool constants=true){
 unsigned at=0;
 for(unsigned i=0;i<instructions;++i){
  const bool constant=lcg(r)%3==0 && constants;
  unsigned d=(lcg(r)&0xffffff00u)&~(63u<<24)&~1u;d|=(lcg(r)%40)<<24;if(i+1==instructions && end)d|=1;
  unsigned s0=(lcg(r)&~3u)|(constant?2u:1u),s1=(lcg(r)&0x7fffffffu)&~3u,s2=(lcg(r)&~3u)|1u;   // s1 bit 31 clear: opcode < 64
  put_word(s,address+at,d);put_word(s,address+at+4,s0);put_word(s,address+at+8,s1);put_word(s,address+at+12,s2);at+=16;
  if(constant){for(unsigned k=0;k<4;++k)put_word(s,address+at+4*k,rsxdraw::float_bits(float(int(lcg(r)%2000)-1000)/8.f+float(salt)));at+=16;}
 }
 return at;
}
static void same(const std::vector<Ref>& expected,const std::vector<FpInst>& actual){
 assert(expected.size()==actual.size());
 for(std::size_t i=0;i<expected.size();++i){const auto& a=expected[i];const auto& b=actual[i];
  assert(a.d==b.d && a.s0==b.s0 && a.s1==b.s1 && a.s2==b.s2 && a.has_constant==b.has_constant && a.word_offset==b.word_offset);
  for(unsigned k=0;k<4;++k){std::uint32_t x,y;std::memcpy(&x,&a.constant[k],4);std::memcpy(&y,&b.constant[k],4);assert(x==y);}}
}
static std::uint32_t value_of(unsigned location,std::uint32_t offset){return (offset&~3u)|location;}
static void random_programs(){
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;auto& R=s.rsx.registers;
 m.map(0x70010000,0xf0000,6,nullptr,0);m.map(0x70100000,0x100000,6,nullptr,0);s.rsx.io[1]=0x70100000;
 std::uint32_t r=4242;unsigned compared=0,erred=0;
 for(unsigned round=0;round<300;++round){
  const unsigned location=1+lcg(r)%2;                                   // 1: VRAM, 2: main memory through the IO table
  std::uint32_t offset=0x2000+16*(lcg(r)%2000);
  if(location==2 && round%7==0)offset=0x100000-16*(1+lcg(r)%20);       // straddle the 1 MiB IO page edge
  const bool end=round%23!=0;
  const unsigned base=location==1?0xc0000000u:0x70000000u;
  const unsigned length=write_program(s,base+offset,r,1+lcg(r)%40,end,round);
  R[0x8e4]=value_of(location,offset);
  std::vector<Ref> expected;std::string expected_error,actual_error;
  try{expected=reference(s,location,offset);}catch(const std::runtime_error& e){expected_error=e.what();}
  std::shared_ptr<const std::vector<FpInst>> actual;
  try{actual=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});}catch(const std::runtime_error& e){actual_error=e.what();}
  if(expected_error!=actual_error)throw std::runtime_error("round "+std::to_string(round)+": error '"+actual_error+"' vs reference '"+expected_error+"'");
  if(expected_error.empty()){same(expected,*actual);++compared;(void)length;}else ++erred;
 }
 std::printf("INFO random programs: %u equal, %u identical errors\n",compared,erred);
 assert(compared>200 && erred>=1);
 std::puts("PASS fragment program loader equals the original 16-byte loader (random programs, page and chunk straddles)");
}
static void tail_of_memory(){
 // The END block is the last readable block: the 256-byte chunk read runs past the mapping and must fall back to exact reads.
 PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;std::uint32_t r=7;
 const std::uint32_t offset=0x800000-16*5;const auto length=write_program(s,0xc0000000u+offset,r,5,true,1,false);
 (void)length;R[0x8e4]=value_of(1,offset);
 const auto expected=reference(s,1,offset);const auto actual=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});same(expected,*actual);
 std::puts("PASS a program ending at the last readable bytes still loads");
}
static void sharing_and_eviction(){
 PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;std::uint32_t r=99;
 // Equal bytes at five addresses share one prepared program.
 std::vector<std::shared_ptr<const std::vector<FpInst>>> shared;
 for(unsigned copy=0;copy<5;++copy){std::uint32_t again=555;write_program(s,0xc0000000u+0x4000+copy*0x400,again,12,true,3);R[0x8e4]=value_of(1,0x4000+copy*0x400);
  shared.push_back(rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx}));}
 for(unsigned copy=1;copy<5;++copy)assert(shared[copy]==shared[0]);
 // A different program at a new address is not conflated with them.
 {std::uint32_t other=556;write_program(s,0xc0000000u+0x5000,other,12,true,3);R[0x8e4]=value_of(1,0x5000);
  const auto p=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});assert(p!=shared[0]);same(reference(s,1,0x5000),*p);}
 // 3000 distinct programs (more than the 1024-entry caches), then the early ones again.
 const unsigned count=3000;std::vector<std::uint32_t> seeds(count);
 for(unsigned i=0;i<count;++i){seeds[i]=1000+i;const std::uint32_t offset=0x10000+i*0x80;
  std::uint32_t seed=seeds[i];write_program(s,0xc0000000u+offset,seed,3+i%6,true,i);R[0x8e4]=value_of(1,offset);
  const auto p=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});if(i%97==0)same(reference(s,1,offset),*p);}
 for(unsigned i=0;i<count;i+=11){const std::uint32_t offset=0x10000+i*0x80;R[0x8e4]=value_of(1,offset);
  same(reference(s,1,offset),*rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx}));}
 // Rewriting bytes at a cached address is seen by the next load.
 {std::uint32_t seed=seeds[count-1];const std::uint32_t offset=0x10000+(count-1)*0x80;R[0x8e4]=value_of(1,offset);
  const auto before=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});
  std::uint32_t changed=seed+7;write_program(s,0xc0000000u+offset,changed,3+(count-1)%6,true,count-1);
  const auto after=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});assert(after!=before);same(reference(s,1,offset),*after);}
 (void)r;
 std::puts("PASS equal programs share one result; eviction past 1024 entries never returns a wrong program; rewrites are seen");
}
int main(){try{random_programs();tail_of_memory();sharing_and_eviction();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxFpCacheTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_fragment_program_loader_equals_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 3, result.stdout)


if __name__ == '__main__':
    unittest.main()
