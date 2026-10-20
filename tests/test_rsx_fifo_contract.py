"""RSX FIFO consumer contract (native oracles): guest writes made by an executed action are seen by the following
commands, and the consumer reads only published command bytes.

These are the regressions requested by the review of the (reverted) FIFO prefetch window: any future FIFO read-ahead must keep
them green. They hold for the scalar consumer in rsx.py.

* next_header_and_payload_rewritten_: a semaphore release (method 0x6c, which stores into guest memory) rewrites the NEXT
  packet header; a second release rewrites the PAYLOAD of the packet after that. The consumer must execute the rewritten bytes
  (header 0xffffffff would be an invalid-header stop; payload 0xdeadbeef would leave the wrong register value).
* reads_stay_in_published_tail_: with the ring wrapped (GET behind the wrap jump, PUT already at the ring start) and in the
  plain case (GET < PUT), an observer on the guest memory records every read; none may touch the unpublished intervals
  (the free area between PUT and GET, the bytes behind the wrap jump, and the bytes at and after PUT).
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
static void setup_fifo(PpuState& s){
 setup_draw(s,8,8,32,false);auto& m=s.memory;
 m.map(0x70010000,0xf0000,6,nullptr,0);s.rsx.fifo_enabled=true;s.rsx.dma=0x70008000;
 for(std::uint32_t a=0;a<0x20000;a+=4)m.store(0x70000000+BASE+a,4,0xffffffff);   // unwritten marker: invalid header
}
static void put(PpuState& s,std::uint32_t at,std::initializer_list<std::uint32_t> words){unsigned i=0;for(auto w:words)s.memory.store(0x70000000+at+4*i++,4,w);}
static std::uint32_t reg(PpuState& s,unsigned r){const auto& bank=std::as_const(s.rsx).registers;const auto it=bank.find(r);return it==bank.end()?0xdeadc0deu:it->second;}
static void next_header_and_payload_rewritten_(){
 PpuState s;setup_fifo(s);auto& m=s.memory;auto& gpu=s.rsx;gpu.device=0x70000000;
 const std::uint32_t header=(1u<<18)|0x1a00;
 put(s,BASE+0x00,{(1u<<18)|0x60,0x56616660});            // semaphore context = device area
 put(s,BASE+0x08,{(1u<<18)|0x64,BASE+0x18});             // offset of the next packet header
 put(s,BASE+0x10,{(1u<<18)|0x6c,header});                // release: stores a valid header over the 0xffffffff at BASE+0x18
 put(s,BASE+0x1c,{0x1111});                              // its payload
 put(s,BASE+0x20,{(1u<<18)|0x64,BASE+0x34});             // offset of a later packet's payload
 put(s,BASE+0x28,{(1u<<18)|0x6c,0x2222});                // release: stores the payload over the 0xffffffff at BASE+0x34
 put(s,BASE+0x30,{(1u<<18)|0x1a04});                     // valid header, payload BASE+0x34 still junk when the stream is published
 const std::uint32_t put_at=BASE+0x38;
 m.store(gpu.dma+0x44,4,BASE);m.store(gpu.dma+0x40,4,put_at);
 assert(rsx_run_impl(s,64));
 assert(m.load(gpu.dma+0x44,4)==put_at);
 if(reg(s,0x1a00)!=0x1111)throw std::runtime_error("rewritten next header was not executed (0x1a00="+std::to_string(reg(s,0x1a00))+")");
 if(reg(s,0x1a04)!=0x2222)throw std::runtime_error("rewritten payload was not seen (0x1a04="+std::to_string(reg(s,0x1a04))+")");
 assert(gpu.commands==7);   // ctx, offset, release, rewritten packet, offset, release, packet with rewritten payload
 std::puts("PASS an executed action's guest write to the next header and to a later payload is seen by the following commands");
}
// Records the guest-memory reads of one consumer run and checks them against host-pointer intervals.
struct Reads {
 std::vector<std::pair<std::uintptr_t,std::uint64_t>> reads;
 void check(const char* what,std::uintptr_t begin,std::uintptr_t end)const{
  for(const auto& r:reads)if(r.first<end && begin<r.first+r.second)throw std::runtime_error(std::string("read inside unpublished interval: ")+what);
 }
};
static std::uintptr_t host(PpuState& s,std::uint32_t io_offset){auto r=s.memory.range(0x70000000+std::uint64_t(io_offset),1,false);return reinterpret_cast<std::uintptr_t>(r.parts.at(0).data);}
static void reads_stay_in_published_tail_(bool wrapped){
 PpuState s;setup_fifo(s);auto& m=s.memory;auto& gpu=s.rsx;
 const std::uint32_t ring_end=BASE+0x4000;
 std::uint32_t get,put_at,jump_at;
 if(wrapped){   // tail: 3 packets then the wrap jump at the end of the lap; head: 2 packets at the ring start
  get=BASE+0x3f00;put(s,get,{(1u<<18)|0x1a00,1,(1u<<18)|0x1a04,2,(1u<<18)|0x1a08,3});jump_at=get+24;put(s,jump_at,{0x20000000u|BASE});
  put(s,BASE,{(1u<<18)|0x1a0c,4,(1u<<18)|0x1a10,5});put_at=BASE+16;
  // stale old-lap packets everywhere else must stay unread: fill the free area and the bytes behind the jump with valid-looking packets
  for(std::uint32_t a=put_at;a<get;a+=8)put(s,a,{(1u<<18)|0x1a14,0xbad0});
  for(std::uint32_t a=jump_at+4;a<ring_end+0x2000;a+=8)put(s,a,{(1u<<18)|0x1a18,0xbad1});
 }else{
  get=BASE;put(s,get,{(1u<<18)|0x1a00,1,(1u<<18)|0x1a04,2,(1u<<18)|0x1a08,3});put_at=get+24;jump_at=0;
  for(std::uint32_t a=put_at;a<BASE+0x2000;a+=8)put(s,a,{(1u<<18)|0x1a14,0xbad0});   // valid-looking bytes at and after PUT
 }
 m.store(gpu.dma+0x44,4,get);m.store(gpu.dma+0x40,4,put_at);
 // Forbidden intervals in host pointers, computed before the observer is installed.
 const auto put_host=host(s,put_at),get_host=host(s,get);
 const std::uintptr_t behind_jump=wrapped?host(s,jump_at+4):0,ring_end_host=host(s,ring_end);
 Reads log;
 s.memory.host_access_observer=[&](const void* p,std::uint64_t n,bool write){if(!write)log.reads.push_back({reinterpret_cast<std::uintptr_t>(p),n});};
 assert(rsx_run_impl(s,64));
 s.memory.host_access_observer=nullptr;
 assert(m.load(gpu.dma+0x44,4)==put_at);
 assert(!log.reads.empty());
 if(wrapped){
  log.check("free area between PUT and GET",put_host,get_host);
  log.check("bytes behind the wrap jump",behind_jump,ring_end_host+0x2000);
  if(reg(s,0x1a0c)!=4 || reg(s,0x1a10)!=5 || reg(s,0x1a14)!=0xdeadc0deu || reg(s,0x1a18)!=0xdeadc0deu)throw std::runtime_error("wrapped consumer executed the wrong packets");
 }else{
  log.check("bytes at and after PUT",put_host,put_host+0x2000-24);
  if(reg(s,0x1a14)!=0xdeadc0deu)throw std::runtime_error("consumer executed bytes past PUT");
 }
 if(reg(s,0x1a00)!=1 || reg(s,0x1a04)!=2 || reg(s,0x1a08)!=3)throw std::runtime_error("published packets were not executed");
 std::printf("PASS the consumer reads only published bytes (%s ring, %zu reads observed)\n",wrapped?"wrapped":"linear",log.reads.size());
}
int main(){try{next_header_and_payload_rewritten_();reads_stay_in_published_tail_(false);reads_stay_in_published_tail_(true);}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxFifoContractTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_fifo_consumer_contract(self):
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
