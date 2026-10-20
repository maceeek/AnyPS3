"""RSX FIFO prefetch window: exact packet semantics, PUT/GET ordering and ring wrap (native oracles).

The consumer (rsx_run_impl) reads the command stream through a prefetch window. Oracles are independent of that
implementation: every packet is generated together with its analytic effect (the last value written to each
register, the number of FIFO headers), so the expected register file and command count come from the generator,
not from a second run of the consumer.

* serial_equivalence_: increment / non-increment / empty / large packets, old and new jumps over junk words
  (0xffffffff is an invalid header: consuming a skipped word stops the run), call/return, packets that
  cross the window capacity.
* window_cost_: the same stream consumed by a foreign thread under concurrent guest memory takes at most
  one slow guest lease per 4 packets (the old per-packet loads took two per packet).
* ring_: a real producer thread writes a wrapping ring under the libgcm protocol (bytes first, PUT
  afterwards, jump at the wrap, free space bounded by the published GET) while the consumer runs; PUT is
  also published inside packets. Stale old-lap bytes parse as valid packets, so any byte used from the
  window that was not published (or lies beyond the wrap jump) changes the command count or the registers.
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
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
struct FifoModel {std::map<std::uint32_t,std::uint32_t> regs;std::uint64_t headers=0;};
// One random register-write packet (registers 0x1a00..0x1bfc: plain tracked state, no side effect) and its effect.
static void packet(std::vector<std::uint32_t>& words,FifoModel& model,std::uint32_t& rng,std::uint32_t& serial){
 const unsigned kind=lcg(rng)%128;++model.headers;
 if(kind<6){words.push_back(0x1a00+4*(lcg(rng)%128));return;}                          // count 0: no effect
 if(kind<56){const unsigned first=lcg(rng)%128,n=1+lcg(rng)%std::min(24u,128-first);   // increment
  words.push_back((n<<18)|(0x1a00+first*4));
  for(unsigned i=0;i<n;++i){words.push_back(serial);model.regs[0x1a00+(first+i)*4]=serial++;}return;}
 if(kind<120){const unsigned reg=0x1a00+4*(lcg(rng)%128),n=1+lcg(rng)%12;              // non-increment
  words.push_back((n<<18)|0x40000000|reg);
  for(unsigned i=0;i<n;++i){words.push_back(serial);model.regs[reg]=serial++;}return;}
 const unsigned reg=0x1a00+4*(lcg(rng)%128);                                           // large non-increment
 const unsigned n=kind<127?100+lcg(rng)%400:2047;                                      // 2047 words do not fit the window
 words.push_back((n<<18)|0x40000000|reg);
 for(unsigned i=0;i<n;++i){words.push_back(serial);model.regs[reg]=serial++;}
}
static const std::uint32_t FIFO_BASE=0x20000,FIFO_SPAN=0x80000;
static void fifo_setup(PpuState& s){
 setup_draw(s,8,8,32,false);auto& m=s.memory;
 m.map(0x70010000,0xf0000,6,nullptr,0);s.rsx.fifo_enabled=true;s.rsx.dma=0x70008000;
 for(std::uint32_t a=0;a<FIFO_SPAN;a+=4)m.store(0x70000000+FIFO_BASE+a,4,0xffffffff);   // unwritten marker: invalid header
}
// Linear program with jumps over junk and a call/return into a subroutine area; returns the final PUT.
static std::uint32_t build_stream(PpuState& s,FifoModel& model){
 auto& m=s.memory;std::uint32_t rng=12345,serial=1;
 const std::uint32_t sub_base=FIFO_BASE+FIFO_SPAN/2;std::uint32_t sub=sub_base,pos=FIFO_BASE;
 auto put_words=[&](std::uint32_t at,const std::vector<std::uint32_t>& w){for(unsigned i=0;i<w.size();++i)m.store(0x70000000+at+4*i,4,w[i]);};
 unsigned count=0;
 while(pos<FIFO_BASE+FIFO_SPAN/2-0x4000 && sub<FIFO_BASE+FIFO_SPAN-0x10000 && count<3000){
  std::vector<std::uint32_t> w;const unsigned what=count%97;
  if(what==40 || what==41){        // jump forward over junk (old and new encoding)
   const std::uint32_t gap=1+lcg(rng)%300,target=pos+4+gap*4;
   w.push_back(what==40?0x20000000u|target:target|1u);++model.headers;put_words(pos,w);pos=target;
  }else if(what==70){              // call a subroutine and return
   std::vector<std::uint32_t> body;const unsigned n=1+lcg(rng)%6;for(unsigned i=0;i<n;++i)packet(body,model,rng,serial);
   body.push_back(0x00020000);model.headers+=2;                         // return + call header
   put_words(sub,body);w.push_back(sub|2u);put_words(pos,w);pos+=4;sub+=4*std::uint32_t(body.size())+4*(lcg(rng)%40);   // gap between subroutines
  }else{packet(w,model,rng,serial);put_words(pos,w);pos+=4*std::uint32_t(w.size());}
  ++count;
 }
 return pos;
}
static void check_registers(PpuState& s,const FifoModel& model,std::size_t baseline){
 const auto& bank=std::as_const(s.rsx).registers;
 for(const auto& [reg,value]:model.regs){const auto it=bank.find(reg);
  if(it==bank.end())throw std::runtime_error("register "+std::to_string(reg)+" was never written");
  if(it->second!=value)throw std::runtime_error("register "+std::to_string(reg)+" has "+std::to_string(it->second)+" expected "+std::to_string(value));}
 if(bank.size()!=baseline+model.regs.size())throw std::runtime_error("unexpected register writes");
}
static void serial_equivalence_(){
 PpuState s;fifo_setup(s);auto& m=s.memory;auto& gpu=s.rsx;
 const auto baseline=gpu.registers.size();
 FifoModel model;const auto put=build_stream(s,model);
 m.store(gpu.dma+0x44,4,FIFO_BASE);m.store(gpu.dma+0x40,4,put);
 assert(model.headers>800);
 assert(rsx_run_impl(s,1u<<24));
 assert(m.load(gpu.dma+0x44,4)==put);
 if(gpu.commands!=model.headers)throw std::runtime_error("executed "+std::to_string(gpu.commands)+" headers, expected "+std::to_string(model.headers));
 check_registers(s,model,baseline);
 std::printf("INFO serial stream: %llu headers, %u bytes\n",(unsigned long long)model.headers,(unsigned)(put-FIFO_BASE));
 std::puts("PASS FIFO window: mixed packets, jumps over junk, call/return and window-crossing packets execute exactly");
}
static void window_cost_(){
 PpuState s;fifo_setup(s);auto& m=s.memory;auto& gpu=s.rsx;
 const auto baseline=gpu.registers.size();
 FifoModel model;const auto put=build_stream(s,model);
 m.store(gpu.dma+0x44,4,FIFO_BASE);m.store(gpu.dma+0x40,4,put);
 m.enable_concurrency();
 std::string error;std::uint64_t leases=0;
 std::thread consumer([&]{try{const auto before=GuestMemory::stat_slow_leases.load();
   rsx_run_impl(s,1u<<24);leases=GuestMemory::stat_slow_leases.load()-before;}catch(const std::exception& e){error=e.what();}
  m.release_reader_slot();});
 consumer.join();
 if(!error.empty())throw std::runtime_error(error);
 assert(m.load(gpu.dma+0x44,4)==put && gpu.commands==model.headers);
 check_registers(s,model,baseline);
 std::printf("INFO foreign consumer: %llu headers, %llu slow leases\n",(unsigned long long)model.headers,(unsigned long long)leases);
 assert(leases*4<=model.headers);
 std::puts("PASS FIFO window: a foreign consumer takes at most one slow guest lease per 4 packets with the same effects");
}
static void ring_(unsigned mid_packet_percent){
 PpuState s;fifo_setup(s);auto& m=s.memory;auto& gpu=s.rsx;
 const std::uint32_t ring0=FIFO_BASE,ring_end=FIFO_BASE+0x8000;
 const auto baseline=gpu.registers.size();
 m.store(gpu.dma+0x44,4,ring0);m.store(gpu.dma+0x40,4,ring0);
 m.enable_concurrency();
 std::atomic<bool> done{false},failed{false};std::string error;FifoModel model;std::uint32_t final_put=ring0;
 unsigned wraps=0;
 std::thread consumer([&]{try{
   while(!failed.load()){
    rsx_run_impl(s,16);
    if(done.load() && m.load(gpu.dma+0x44,4)==m.load(gpu.dma+0x40,4))break;
   }}catch(const std::exception& e){error=e.what();failed=true;}
  m.release_reader_slot();});
 std::thread producer([&]{try{
   std::uint32_t rng=777,serial=1,P=ring0;const auto start=std::chrono::steady_clock::now();
   auto store_words=[&](std::uint32_t at,const std::vector<std::uint32_t>& w,unsigned from,unsigned to){for(unsigned i=from;i<to;++i)m.store(0x70000000+at+4*i,4,w[i]);};
   for(unsigned k=0;k<7000 && !failed.load();++k){
    std::vector<std::uint32_t> w;packet(w,model,rng,serial);const std::uint32_t need=4*std::uint32_t(w.size());
    for(;;){
     if(failed.load())return;
     if(std::chrono::steady_clock::now()-start>std::chrono::seconds(120))throw std::runtime_error("producer timeout");
     const auto G=std::uint32_t(m.load(gpu.dma+0x44,4));
     if(G<=P){
      if(P+need+4<=ring_end)break;
      if(G>=ring0+need+8){      // wrap: jump at the end of this lap (published with the new PUT), continue at the ring start
       const bool old_form=lcg(rng)&1;m.store(0x70000000+P,4,old_form?0x20000000u|ring0:ring0|1u);++model.headers;
       P=ring0;++wraps;m.store(gpu.dma+0x40,4,P);break;
      }
     }else if(P+need+8<=G)break;
     std::this_thread::yield();
    }
    const bool split=lcg(rng)%100<mid_packet_percent && w.size()>2;
    if(split){const unsigned half=unsigned(w.size()/2);
     m.store(0x70000000+P,4,w[0]);m.store(gpu.dma+0x40,4,P+4);                          // header published, values not yet
     std::this_thread::yield();store_words(P,w,1,half);m.store(gpu.dma+0x40,4,P+4*half);
     store_words(P,w,half,unsigned(w.size()));}
    else store_words(P,w,0,unsigned(w.size()));
    P+=need;m.store(gpu.dma+0x40,4,P);
   }
   final_put=P;done=true;}catch(const std::exception& e){error=e.what();failed=true;}
  m.release_reader_slot();});
 producer.join();consumer.join();
 if(!error.empty())throw std::runtime_error(error);
 assert(m.load(gpu.dma+0x44,4)==final_put && m.load(gpu.dma+0x40,4)==final_put);
 if(gpu.commands!=model.headers)throw std::runtime_error("executed "+std::to_string(gpu.commands)+" headers, expected "+std::to_string(model.headers));
 check_registers(s,model,baseline);
 assert(wraps>=20);
 std::printf("INFO ring mid=%u%%: %llu headers, %u wraps\n",mid_packet_percent,(unsigned long long)model.headers,wraps);
 std::puts("PASS FIFO window: producer/consumer ring with wraps and mid-packet PUT executes every published packet exactly once");
}
// A jump must drop the prefetched window: the RSX itself rewrites (semaphore release) the header at the jump
// target after the window already holds its old junk bytes. A stale window would execute 0xffffffff.
static void jump_invalidates_(){
 PpuState s;fifo_setup(s);auto& m=s.memory;auto& gpu=s.rsx;
 gpu.device=0x70000000;
 const std::uint32_t P0=FIFO_BASE,target=FIFO_BASE+0x100,header=(1u<<18)|0x1a00;
 std::vector<std::uint32_t> w{(1u<<18)|0x60,0x56616660,(1u<<18)|0x64,target,(1u<<18)|0x6c,header,0x20000000u|target};
 for(unsigned i=0;i<w.size();++i)m.store(0x70000000+P0+4*i,4,w[i]);
 m.store(0x70000000+target+4,4,0x1234);          // the value word behind the (not yet written) header
 const std::uint32_t put=target+8;
 m.store(gpu.dma+0x44,4,P0);m.store(gpu.dma+0x40,4,put);
 assert(rsx_run_impl(s,64));
 assert(m.load(gpu.dma+0x44,4)==put && gpu.commands==5);
 const auto& bank=std::as_const(s.rsx).registers;
 assert(bank.find(0x1a00)!=bank.end() && bank.find(0x1a00)->second==0x1234);
 std::puts("PASS FIFO window: a jump target rewritten by the RSX after the prefetch is read afresh");
}
int main(){try{serial_equivalence_();jump_invalidates_();window_cost_();ring_(0);ring_(25);}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxFifoWindowTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_rsx_fifo_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, LP_NUM_THREADS='2', ANYPS3_FRAME_DIR=str(root), ANYPS3_VK_DEVICE='lavapipe',
                       VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 5, result.stdout)


if __name__ == '__main__':
    unittest.main()
