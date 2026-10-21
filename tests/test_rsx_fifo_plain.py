"""RSX FIFO consumer: the table fast path for plain state registers equals the full execution chain (native oracle).

Every method register 0..0xfffc (class-1 plain registers, but also the special ones: transform program/constant uploads, vertex-data
registers, 0x1710/0x1714, zcull, copy/channel objects ... that must keep walking the chain) is written through single-method packets with
random values, and through multi-method incrementing and non-incrementing packets that straddle plain and special registers. The same stream
runs once with rsx_fifo_plain_fast=true and once with the flag false (the reference: every method walks the chain); the register bank,
transform program/constants, generation counters and the packet count must be bit-identical. The set of plain registers is additionally
pinned to an independent expectation derived from the register map (class 1 minus an explicitly listed set of special registers).
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
static const std::uint32_t BASE=0x20000;
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>8;}
static void setup_fifo(PpuState& s){
 setup_draw(s,8,8,32,false);auto& m=s.memory;
 m.map(0x70010000,0xf0000,6,nullptr,0);s.rsx.fifo_enabled=true;s.rsx.dma=0x70008000;
}
static void put(PpuState& s,std::uint32_t at,std::uint32_t word){s.memory.store(0x70000000+at,4,word);}
struct Stream {std::vector<std::uint32_t> words;
 void method(std::uint32_t reg,std::initializer_list<std::uint32_t> values,bool increment=true){
  words.push_back(((unsigned(values.size()))<<18)|(increment?0u:0x40000000u)|reg);for(auto v:values)words.push_back(v);}};
static Stream build(){
 Stream st;std::uint32_t r=1234;
 // every register once, random value (skip the registers whose execution needs a full draw/blit/semaphore environment)
 for(std::uint32_t reg=0;reg<0x10000;reg+=4){
  if(rsx_method_class(reg)==0)continue;
  if((reg>=0xb80 && reg<=0xbfc) || (reg>=0x1f00 && reg<=0x1f7c) || reg==0x1efc || reg==0x1e9c || reg==0x1ef8)continue;   // program/constant uploads and their load positions: explicit packets below
  if(reg==0 || reg==0xe000 || reg==0x2000 || reg==0x6000 || reg==0x8000 || reg==0xa000 || reg==0xc000)continue;   // object bindings: only their fixed values are legal
  if(reg==0x68 || reg==0x6c || reg==0x50 || reg==0x1d70 || reg==0x1d74 || reg==0x1800 || reg==0x1d94 || reg==0x2328 || reg==0xc40c || (reg>=0xa400 && reg<0xc000))continue;   // guest memory / surface / blit actions
  if(reg==0xe920 || reg==0xe924 || reg==0xe940 || reg==0xe944 || reg==0x1e98 || reg==0x17c8 || reg==0xa1c || reg==0x1fd8 || reg==0x1438 || reg==0x234 || reg==0x1710 || reg==0x1714)continue;   // display / report / render-enable / sync actions
  if(reg==0x1808 || reg==0x180c || reg==0x1810 || reg==0x1814 || reg==0x1818 || reg==0x1824)continue;   // begin/end/draw/inline array
  if((reg>=0xa80 && reg<0xb00) || (reg>=0x1500 && reg<0x1600) || (reg>=0x1880 && reg<0x1a00) || (reg>=0x1c00 && reg<0x1d00) || (reg>=0x1e40 && reg<0x1e80))continue;   // vertex data: needs begin/end (always walks the chain; pinned by extracted_are_special)
  st.method(reg,{lcg(r)});
 }
 st.method(0x1710,{1});st.method(0x1714,{2});st.method(0x234,{1});st.method(0x1438,{1});
 // incrementing / non-incrementing multi-method packets across plain and special registers
 st.method(0x1efc,{3});st.method(0x1f00,{0x3f800000,0x40000000,0x40400000,0x40800000});
 st.method(0x1ef8,{0});st.method(0xb80,{0x11111111,0x22222222,0x33333333,0x44444444,0x55555555});   // program upload through the load position
 st.method(0x300,{1,2,3,4,5,6,7,8},true);st.method(0x300,{9,10,11},false);st.method(0x400,{0xaaaa,0xbbbb,0xcccc},true);
 return st;
}
struct Result {std::vector<std::pair<std::uint32_t,std::uint32_t>> registers;std::vector<std::uint32_t> program,constants;std::uint64_t commands,zcull,vertex_cache,packets;unsigned plain=0;};
static Result run(bool fast){
 rsx_fifo_plain_fast=fast;
 PpuState s;setup_fifo(s);auto& gpu=s.rsx;gpu.device=0x70000000;
 const auto st=build();std::uint32_t at=BASE;for(auto w:st.words){put(s,at,w);at+=4;}
 s.memory.store(gpu.dma+0x44,4,BASE);s.memory.store(gpu.dma+0x40,4,at);
 // single run: the consumer returns when GET reaches PUT
 for(unsigned n=0;n<64 && s.memory.load(gpu.dma+0x44,4)!=at;++n)(void)rsx_run_impl(s,1000000);
 if(s.memory.load(gpu.dma+0x44,4)!=at)throw std::runtime_error("consumer did not reach PUT");
 Result out;const auto& bank=std::as_const(gpu).registers;for(auto it=bank.begin();it!=bank.end();++it)out.registers.push_back({it->first,it->second});
 for(unsigned i=0;i<gpu.transform_program.size();++i)out.program.push_back(std::as_const(gpu).transform_program[i]);
 for(unsigned i=0;i<gpu.transform_constants.size();++i)out.constants.push_back(std::as_const(gpu).transform_constants[i]);
 out.commands=gpu.commands;out.zcull=gpu.zcull_generation;out.vertex_cache=gpu.vertex_cache_generation;
 for(std::uint32_t reg=0;reg<0x10000;reg+=4)if(rsx_plain_table()[reg>>2])++out.plain;
 rsx_fifo_plain_fast=true;return out;
}
static void equal_paths(){
 rsx_fifo_window=false;const auto unwindowed=run(true);rsx_fifo_window=true;
 const auto fills=rsx_fifo_window_fills;const auto fast=run(true),slow=run(false);
 if(rsx_fifo_window_fills==fills)throw std::runtime_error("the FIFO window was never filled");
 if(fast.registers!=unwindowed.registers || fast.program!=unwindowed.program || fast.constants!=unwindowed.constants || fast.commands!=unwindowed.commands)
  throw std::runtime_error("the FIFO window changes the executed stream");
 if(fast.registers!=slow.registers)throw std::runtime_error("register bank differs between the fast path and the chain");
 if(fast.program!=slow.program || fast.constants!=slow.constants)throw std::runtime_error("transform program/constants differ");
 if(fast.commands!=slow.commands || fast.zcull!=slow.zcull || fast.vertex_cache!=slow.vertex_cache)throw std::runtime_error("counters differ");
 if(fast.registers.size()<400)throw std::runtime_error("test stream wrote too few registers: "+std::to_string(fast.registers.size()));
 std::printf("PASS fast path equals the chain: %zu registers, %llu packets, %u plain registers\n",fast.registers.size(),(unsigned long long)fast.commands,fast.plain);
}
static void plain_set(){
 // Independent expectation: plain == supported, not an action, and not one of the registers the consumer treats individually.
 static const std::uint32_t special[]={0,0x50,0x68,0x6c,0x100,0x110,0x234,0x2000,0x6000,0x8000,0xa000,0xc000,0xe000,0x1d94,0xa1c,0x1fd8,0x1710,0x1714,0x1800,0x1808,0x180c,0x1810,0x1814,0x1818,0x1824,
  0x1e98,0x17c8,0x1438,0x2328,0xc40c,0x1d70,0x1d74,0xe920,0xe924,0xe940,0xe944};
 unsigned plain=0,checked=0;
 for(std::uint32_t reg=0;reg<0x10000;reg+=4){
  bool expect=rsx_method_class(reg)==1;
  for(auto x:special)if(x==reg)expect=false;
  if((reg>=0xb80 && reg<=0xbfc) || (reg>=0x1f00 && reg<=0x1f7c) || (reg>=0xa400 && reg<0xc000))expect=false;
  if((reg>=0xa80 && reg<0xb00) || (reg>=0x1900 && reg<0x1a00) || (reg>=0x1e40 && reg<0x1e80) || (reg>=0x1880 && reg<0x1900) || (reg>=0x1500 && reg<0x1600) || (reg>=0x1c00 && reg<0x1d00))expect=false;
  if(bool(rsx_plain_table()[reg>>2])!=expect)throw std::runtime_error("plain table differs from the expectation at register "+std::to_string(reg));
  ++checked;plain+=expect;
 }
 if(plain<500)throw std::runtime_error("too few plain registers");
 std::printf("PASS plain register table equals the independent expectation (%u of %u addresses)\n",plain,checked);
}
static void extracted_are_special(){
 // Every register the sources of the chain / vertex-data handler / action table name individually is NOT in the plain table.
 unsigned n=0;
 for(auto reg:EXTRACTED_EQUAL){if(rsx_plain_table()[reg>>2])throw std::runtime_error("register "+std::to_string(reg)+" is treated individually by the chain but is in the plain table");++n;}
 for(const auto& range:EXTRACTED_RANGES)for(std::uint32_t reg=range[0];reg<range[1];reg+=4){if(rsx_plain_table()[reg>>2])throw std::runtime_error("register "+std::to_string(reg)+" of an individually treated range is in the plain table");++n;}
 if(n<300)throw std::runtime_error("too few extracted registers");
 std::printf("PASS every register named by the chain, the vertex-data handler and the action table (%u addresses) walks the chain\n",n);
}
// FIFO window: a front-end semaphore release (0x6c, device context) stores into the command buffer AHEAD of GET, over the value of a
// later plain packet that the window has already read. The consumer must execute the stored value (independent expectation: the
// released value, not the original word), with the window on and off, and with plain packets (window hits) before the release.
static void window_release_into_fifo(){
 for(bool windowed:{false,true}){
  rsx_fifo_window=windowed;
  PpuState s;setup_fifo(s);auto& gpu=s.rsx;gpu.device=0x70000000;
  Stream st;for(unsigned i=0;i<8;++i)st.method(0x300+4*i,{0x100+i});   // plain packets read through the window
  st.method(0x60,{0x56616660});
  const std::uint32_t patch_at=BASE+std::uint32_t(st.words.size()+4)*4+4;   // value word of the packet after 0x64 and 0x6c
  st.method(0x64,{patch_at});st.method(0x6c,{0xdeadbeef});st.method(0x340,{0x12345678});
  for(unsigned i=0;i<4;++i)st.method(0x344+4*i,{0x200+i});
  std::uint32_t at=BASE;for(auto w:st.words){put(s,at,w);at+=4;}
  if(s.memory.load(0x70000000+patch_at,4)!=0x12345678)throw std::runtime_error("test: patch position does not hold the 0x340 value");
  s.memory.store(gpu.dma+0x44,4,BASE);s.memory.store(gpu.dma+0x40,4,at);
  const auto fills=rsx_fifo_window_fills;
  for(unsigned n=0;n<64 && s.memory.load(gpu.dma+0x44,4)!=at;++n)(void)rsx_run_impl(s,1000000);
  if(s.memory.load(gpu.dma+0x44,4)!=at)throw std::runtime_error("consumer did not reach PUT");
  if(s.memory.load(0x70000000+patch_at,4)!=0xdeadbeef)throw std::runtime_error("the release did not store into the command buffer");
  if(rsx_reg(gpu,0x340)!=0xdeadbeef)throw std::runtime_error(std::string(windowed?"windowed":"unwindowed")+": register 0x340 = "+std::to_string(rsx_reg(gpu,0x340))+", expected the released value");
  if(rsx_reg(gpu,0x31c)!=0x107 || rsx_reg(gpu,0x350)!=0x203)throw std::runtime_error("plain packets around the release were not executed");
  if(windowed && rsx_fifo_window_fills<fills+2)throw std::runtime_error("the window was not refilled after the release");
 }
 rsx_fifo_window=true;
 std::puts("PASS a release into the command buffer ahead of GET is executed (FIFO window invalidated by non-plain methods)");
}
static std::uint8_t* host_byte(PpuState& s,std::uint64_t address){
 for(const auto& r:s.memory.regions)if(address>=r.base && address-r.base<r.bytes->size())return r.bytes->data()+(address-r.base);
 throw std::runtime_error("test: unmapped guest address");
}
static void store_be(std::uint8_t* p,std::uint32_t v){p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v;}
// review-rsxw4-1 #2: callbacks between packets that rewrite a later command word. (a) gpu.trace publishes GET after every packet;
// a filtered observer watching only GET rewrites the value of the third plain packet on the first GET write. (b) an owner request
// (rsx_owner_hook, rsx_host_waiters) served before the second packet rewrites the third packet's value. Independent expectation:
// the rewritten value executes, with the window on and off.
static void window_callbacks(){
 for(int variant=0;variant<2;++variant)for(bool windowed:{false,true}){
  rsx_fifo_window=windowed;
  PpuState s;setup_fifo(s);auto& gpu=s.rsx;gpu.device=0x70000000;
  Stream st;st.method(0x300,{1});st.method(0x304,{2});st.method(0x308,{3});st.method(0x30c,{4});
  std::uint32_t at=BASE;for(auto w:st.words){put(s,at,w);at+=4;}
  const std::uint64_t third=0x70000000+BASE+5*4;   // value word of the 0x308 packet
  s.memory.store(gpu.dma+0x44,4,BASE);s.memory.store(gpu.dma+0x40,4,at);
  int calls=0;
  if(variant==0){
   gpu.trace=true;
   s.memory.host_access_observer=[&](const void*,std::uint64_t,bool write){if(write && calls++==0)store_be(host_byte(s,third),0x77);};
   s.memory.host_access_observer.filtered=true;s.memory.host_access_observer.watch(host_byte(s,gpu.dma+0x44),4);
  }else{
   rsx_owner_hook()=[&]{if(++calls==2){store_be(host_byte(s,third),0x77);rsx_host_waiters=0;}};rsx_host_waiters=1;
  }
  for(unsigned n=0;n<64 && s.memory.load(gpu.dma+0x44,4)!=at;++n)(void)rsx_run_impl(s,1000000);
  gpu.trace=false;s.memory.host_access_observer={};rsx_owner_hook()=nullptr;rsx_host_waiters=0;
  if(s.memory.load(gpu.dma+0x44,4)!=at)throw std::runtime_error("consumer did not reach PUT");
  if(rsx_reg(gpu,0x308)!=0x77 || rsx_reg(gpu,0x30c)!=4)throw std::runtime_error(std::string(variant?"owner request":"traced GET observer")+(windowed?" (window)":" (no window)")+
   ": register 0x308 = "+std::to_string(rsx_reg(gpu,0x308))+", expected the rewritten 0x77");
 }
 rsx_fifo_window=true;
 std::puts("PASS command words rewritten by a GET-publication callback or an owner request execute (window invalidated)");
}
// review-rsxw4-1 #3: a claimed page inside the window range whose coherence service fails once (then releases the claim). The fill
// must not service the claim (declined); the per-word path services it at the original packet, so the first failure propagates
// there exactly as without the window: same error, same registers.
static void window_claimed_page(){
 std::string error[2];std::uint32_t regs[2][8]{};unsigned services[2]{};
 for(int side=0;side<2;++side){
  rsx_fifo_window=side==1;
  PpuState s;setup_fifo(s);auto& gpu=s.rsx;gpu.device=0x70000000;
  const std::uint32_t base=0x20fe0;   // the window starting here crosses into the next 4 KiB page (0x21000)
  Stream st;for(unsigned i=0;i<8;++i)st.method(0x300+4*i,{0x10+i});   // 64 bytes: packets 4..7 on the claimed page
  std::uint32_t at=base;for(auto w:st.words){put(s,at,w);at+=4;}
  auto& m=s.memory;m.store(gpu.dma+0x44,4,base);m.store(gpu.dma+0x40,4,at);
  m.enable_concurrency();m.enable_page_broker();
  std::shared_ptr<GuestBacking> backing;for(const auto& r:m.regions)if(r.base==0x70010000)backing=r.bytes;
  auto claim=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{host_byte(s,0x70021000),4,backing}});
  bool thrown=false;
  m.page_broker.coherence=[&](const std::vector<GuestMemory::HostSpan>&,bool){++services[side];if(!thrown){thrown=true;throw std::runtime_error("injected coherence failure");}claim.reset();};
  try{(void)rsx_run_impl(s,1000000);}catch(const std::runtime_error& e){error[side]=e.what();}
  m.page_broker.coherence={};claim.reset();
  for(unsigned i=0;i<8;++i)regs[side][i]=rsx_reg(gpu,0x300+4*i);
 }
 rsx_fifo_window=true;
 if(error[0]!=error[1] || error[0]!="injected coherence failure")throw std::runtime_error("claimed page: error '"+error[1]+"' (window) vs '"+error[0]+"' (no window)");
 if(services[0]!=services[1])throw std::runtime_error("claimed page: coherence services "+std::to_string(services[1])+" (window) vs "+std::to_string(services[0]));
 for(unsigned i=0;i<8;++i)if(regs[0][i]!=regs[1][i])throw std::runtime_error("claimed page: register state differs");
 if(services[0]!=1 || regs[0][3]!=0x13 || regs[0][4]==0x14)throw std::runtime_error("claimed page: the failure did not stop at the first packet on the claimed page");
 std::puts("PASS a claimed page inside the window range is serviced only by the per-word path (first coherence failure propagates)");
}
int main(){try{extracted_are_special();plain_set();equal_paths();window_release_into_fifo();window_callbacks();window_claimed_page();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def chain_literals():
    """Registers the execution chain and the vertex-data handler treat individually, extracted from the sources (independent of the C++ list)."""
    import re
    root = Path(__file__).resolve().parent.parent / 'anyps3'
    rsx = (root / 'rsx.py').read_text()
    start = rsx.index('for(unsigned i=gpu.packet_index;i<count;++i){')
    chain = rsx[start:rsx.index('gpu.packet_index=0;', start)]
    draw = (root / 'rsx_draw.py').read_text()
    first = draw.index('static bool rsx_vertex_data_register')
    handler = draw[first:first + 6000]
    equal, ranges = set(), set()
    for text in (chain, handler):
        equal.update(int(x, 16) for x in re.findall(r'reg==\s*(0x[0-9a-fA-F]+)', text))
        for lo, op, hi in re.findall(r'reg>=(0x[0-9a-fA-F]+)\s*&&\s*reg(<=|<)(0x[0-9a-fA-F]+)', text):
            ranges.add((int(lo, 16), int(hi, 16) + (4 if op == '<=' else 0)))
    action = rsx[rsx.index('static bool rsx_method_action'):rsx.index('static unsigned rsx_method_class')]
    equal.update(int(x, 16) for x in re.findall(r'case (0x[0-9a-fA-F]+)', action))
    if 'reg>=0xa400 && reg<0xc000' in action:
        ranges.add((0xa400, 0xc000))
    equal.add(0)   # reg==0 is spelled without a hex literal
    return sorted(equal), sorted(ranges)


def native_source():
    equal, ranges = chain_literals()
    generated = ('static const std::uint32_t EXTRACTED_EQUAL[]={' + ','.join(hex(x) for x in equal) + '};\n'
                 'static const std::uint32_t EXTRACTED_RANGES[][2]={' + ','.join('{%s,%s}' % (hex(a), hex(b)) for a, b in ranges) + '};\n')
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + generated + MAIN


class RsxFifoPlainTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_plain_register_fast_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 6, result.stdout)


if __name__ == '__main__':
    unittest.main()
