"""Native RSX teardown and explicitly bounded offline BSD socket behavior."""
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program


# Use the generated runtime, its real syscall dispatcher and guest BE memory.
# All expected results/layout bytes below are fixed independently of helpers.
INVOKE = r'''
 (void)call;
 auto invoke=[&](unsigned num,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,
                 std::uint64_t d=0,std::uint64_t e=0,std::uint64_t f=0,std::uint64_t g=0){
     r[11]=num;r[3]=a;r[4]=b;r[5]=c;r[6]=d;r[7]=e;r[8]=f;r[9]=g;syscall(s);return r[3];
 };
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Lv2RsxNetExtTests(unittest.TestCase):
    def run_native(self, body):
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source += r'''
#include <cassert>
template<class F> void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main(){PpuState s;auto& r=s.r;auto& mem=s.memory;mem.map(0x10000,0x2000,6,nullptr,0);
 auto call=[]{};
''' + INVOKE + body + '\n}\n'
        with tempfile.TemporaryDirectory(prefix='rsxnet-test-') as directory:
            path = Path(directory)
            (path / 'test.cpp').write_text(source)
            # Same flags as the shared native helper. Allow concurrent batch
            # workers to compile without its short 30s wall-clock limit.
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    str(path / 'test.cpp'), '-o', str(path / 'test')],
                                   capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(path / 'test')], capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_rsx_unmap_free_reallocate_and_transactional_failures(self):
        self.run_native(r'''
 assert(invoke(669,0x5a5a5a5b)==0x80010004);
 assert(invoke(671,0x55555555)==0x80010002);
 assert(invoke(673,0,0,0x100000)==0x80010002);
 assert(invoke(675,0x10000,0,8)==0 && mem.load(0x10000,8)==0x60000000);
 assert(invoke(668,0x10010,0x10018,0x200000,0x80000,0x300000,15,8)==0);
 assert(mem.load(0x10010,4)==0x5a5a5a5b && mem.load(0x10018,8)==0xc0000000);
 assert(invoke(670,0x10020,0x10028,0x10030,0x10038,0x5a5a5a5b,0x20)==0);
 assert(mem.load(0x10020,4)==0x55555555 && mem.load(0x10028,8)==0x60100000);
 assert(mem.load(0x10030,8)==0x60200000 && mem.load(0x10038,8)==0x60300000);
 assert(mem.device_allocated==0x600000);
 mem.map(0x100000,0x300000,6,nullptr,0);
 assert(invoke(672,0x55555555,0x100000,0x100000,0x200000)==0);
 assert(rsx_address(s.rsx,0x100001)==0x100001 && rsx_address(s.rsx,0x200002)==0x200002);
 for(const auto io:{UINT64_C(1),UINT64_C(0x10000000),UINT64_MAX-UINT64_C(0xfffff)})
     assert(invoke(673,0x55555555,io,0x100000)==0x80010002);
 assert(invoke(673,0x55555555,0,0)==0x80010002);
 assert(invoke(673,0x55555555,0,0x100001)==0x80010002);
 assert(invoke(673,0x55555555,0xff00000,0x200000)==0x80010002);
 assert(invoke(673,0x55555554,0x100000,0x100000)==0x80010002);
 assert(rsx_address(s.rsx,0x100001)==0x100001);
 assert(invoke(673,0x55555555,0x100000,0x100000)==0);
 fault([&]{rsx_address(s.rsx,0x100001);});
 assert(rsx_address(s.rsx,0x200002)==0x200002 && mem.load(0x100000,4)==0);
 assert(invoke(673,0x55555555,0x100000,0x100000)==0); // idempotent range
 assert(s.rsx.io_flags[1]==0);
 fault([&]{invoke(669,0x5a5a5a5b);});fault([&]{invoke(676,8);});
 fault([&]{invoke(677,0x202,1,1);});fault([&]{invoke(677,2,1,0);});fault([&]{invoke(677,0x105);});
 // Pending FIFO work is not silently dropped by teardown or iounmap.
 s.rsx.fifo_enabled=true;mem.store(0x60100040,4,4);
 fault([&]{invoke(671,0x55555555);});fault([&]{invoke(673,0x55555555,0x200000,0x100000);});
 assert(s.rsx.context==0x55555555 && rsx_address(s.rsx,0x200000)==0x200000 && mem.device_allocated==0x600000);
 mem.store(0x60100044,4,4);s.rsx.draw.primitive=5;
 fault([&]{invoke(671,0x55555555);});s.rsx.draw.primitive=0;
 s.rsx.return_offset=0;fault([&]{invoke(671,0x55555555);});s.rsx.return_offset=UINT32_MAX;
 const auto queue=s.rsx.event_queue,port=s.rsx.event_port;
 PpuContext t;t.thread_id=2;t.pc=0x10500;t.wait_kind=1;t.wait_id=queue;t.r[4]=0x1234;s.ppu_threads.push_back(t);
 s.events.ports.push_back({0x1234,1,queue,0});s.rsx.flips=7;s.rsx.draw.draws=13;
 assert(invoke(671,UINT64_C(0xdeadbeef55555555))==0);
 assert(s.ppu_threads.back().wait_kind==0 && s.ppu_threads.back().pc==0x10504);
 assert(s.ppu_threads.back().r[3]==0x80010013 && s.ppu_threads.back().r[4]==0x1234);
 assert(s.rsx.context==0 && !s.rsx.fifo_enabled && s.rsx.flips==7 && s.rsx.draw.draws==13 && s.rsx.io[2]==UINT32_MAX);
 for(const auto& q:s.events.queues)assert(q.id!=queue);
 for(const auto& p:s.events.ports){assert(p.id!=port);if(p.id==0x1234)assert(!p.queue);}
 assert(mem.device_allocated==0x300000);fault([&]{mem.load(0x60100000,4);});
 assert(invoke(671,0x55555555)==0x80010002);
 assert(invoke(670,0x10020,0x10028,0x10030,0x10038,0x5a5a5a5b,0)==0);
 assert(s.rsx.event_queue!=queue && s.rsx.io[2]==UINT32_MAX);
 assert(invoke(671,0x55555555)==0);
 assert(invoke(669,0x5a5a5a5b)==0 && mem.device_allocated==0x100000);
 fault([&]{mem.load(0xc0000000,4);});assert(!s.rsx.memory_handle && !s.rsx.local_size);
 fault([&]{invoke(676,7);});assert(s.rsx.device==0x60000000);
 assert(invoke(676,8)==0 && !s.rsx.device && mem.device_allocated==0);
 fault([&]{mem.load(0x60000000,4);});assert(invoke(676,8)==0);
 assert(invoke(675,0x10000,0,8)==0 && mem.load(0x10000,8)==0x60000000);
''')

    def test_socket_lifecycle_limits_and_infoctl_big_endian(self):
        self.run_native(r'''
 assert(invoke(713,2,99,0)==UINT64_C(0xffffffffffffffd5)); // -EPROTONOSUPPORT 43
 fault([&]{invoke(713,24,2,0);});fault([&]{invoke(713,2,3,0);});
 fault([&]{invoke(713,2,2,6);});assert(s.rsx_net_ext.sockets.empty());
 assert(invoke(713,UINT64_C(0x1122334400000002),2,0)==24);
 assert(invoke(713,2,1,6)==25);
 mem.store(0x10100,8,UINT64_MAX);mem.store(0x1010c,4,0);mem.store(0x10110,4,0);
 assert(invoke(722,6,0x10100)==2); // NULL info output = count query
 mem.store(0x1010c,4,0x10200);mem.store(0x10110,4,2);
 std::fill_n(mem.access(0x10200,80,true),80,0xa5);
 assert(invoke(722,6,0x10100)==2);
 assert(mem.load(0x10200,8)==UINT64_C(0x0000001800000011));
 assert(mem.load(0x10220,8)==UINT64_C(0x0000000200000019));
 assert(mem.load(0x10228,4)==6 && mem.load(0x10244,4)==2);
 assert(mem.load(0x10248,8)==UINT64_C(0xa5a5a5a5a5a5a5a5)); // exact 2 x 36B output
 assert(mem.load(0x10210,8)==0 && mem.load(0x10218,8)==0); // no addresses or queues
 mem.store(0x10100,4,24);mem.store(0x10110,4,1);assert(invoke(722,6,0x10100)==0);
 mem.store(0x10100,4,999);assert(invoke(722,6,0x10100)==UINT64_C(0xfffffffffffffff7));
 assert(invoke(722,6,0)==UINT64_C(0xffffffffffffffea));
 fault([&]{invoke(722,11,0x10100);});
 assert(invoke(714,UINT64_C(0xffffffff00000018))==0);
 assert(invoke(714,24)==UINT64_C(0xfffffffffffffff7));
 assert(invoke(713,2,2,17)==24); // lowest free descriptor, separate namespace
 for(unsigned i=26;i<1024;++i)assert(invoke(713,2,2,0)==i);
 assert(invoke(713,2,2,0)==UINT64_C(0xffffffffffffffe8)); // -EMFILE 24
 assert(invoke(721,4)==1000 && s.rsx_net_ext.sockets.empty());
''')

    def test_no_fake_transfers_options_flags_and_unknown_calls(self):
        self.run_native(r'''
 assert(invoke(713,2,2,0)==24 && invoke(713,2,1,0)==25);
 mem.store(0x10100,8,UINT64_C(0xa1a2a3a4a5a6a7a8));
 mem.store(0x10140,8,UINT64_C(0x1002005008080808));mem.store(0x10148,8,0);
 fault([&]{invoke(710,24,0xdead0000,8,0,0x10140,16);});
 assert(invoke(710,24,0x10100,8,0,0x10140,16)==UINT64_C(0xffffffffffffffcf)); // -EADDRNOTAVAIL 49
 mem.store(0x10144,4,0xc0a80101);
 fault([&]{invoke(710,24,0x10100,8,0,0x10140,16);});
 assert(invoke(710,24,0x10100,8)==UINT64_C(0xffffffffffffffd9)); // -EDESTADDRREQ 39
 fault([&]{invoke(710,25,0x10100,8);}); // Host stream errors are not assumed to be PS3 errors.
 assert(invoke(710,24,0x10100,8,0,0x10140,15)==UINT64_C(0xffffffffffffffea));
 mem.store(0x10141,1,24);assert(invoke(710,24,0x10100,8,0,0x10140,16)==UINT64_C(0xffffffffffffffd1));
 fault([&]{invoke(710,24,0x10100,8,1);});
 assert(invoke(707,24,0x10100,8,0x80)==UINT64_C(0xffffffffffffffdd)); // -EWOULDBLOCK 35
 assert(invoke(707,25,0x10100,8)==UINT64_C(0xffffffffffffffc7));
 assert(invoke(707,24,0,8,0x80)==UINT64_C(0xffffffffffffffea));
 assert(invoke(707,24,0x10100,0,0x80)==UINT64_C(0xffffffffffffffea));
 assert(invoke(707,24,0x10100,8,0x80,0x10140,0)==UINT64_C(0xffffffffffffffea));
 fault([&]{invoke(707,24,0x10100,8,4);});
 mem.store(0x10120,4,0x80000000);
 assert(invoke(711,24,0xffff,0x1100,0x10120,4)==0);
 assert(s.rsx_net_ext.sockets.at(24).nbio==INT32_MIN);
 assert(invoke(707,24,0x10100,8)==UINT64_C(0xffffffffffffffdd));
 assert(invoke(711,24,0xffff,0x1100,0x10120,3)==UINT64_C(0xffffffffffffffea));
 fault([&]{invoke(711,24,0xffff,0x1001,0x10120,4);});
 assert(invoke(712,24,3)==UINT64_C(0xffffffffffffffea));
 assert(invoke(712,24,2)==UINT64_C(0xffffffffffffffc7));
 for(const auto num:{707,709,710}){
     if(num==707)assert(invoke(num,99,0x10100,8)==UINT64_C(0xfffffffffffffff7));
     else assert(invoke(num,99,0x10100,0)==UINT64_C(0xfffffffffffffff7));
 }
 for(const auto num:{708,716,717,718,719,720,723,724,725})fault([&]{invoke(num,24);});
 assert(mem.load(0x10100,8)==UINT64_C(0xa1a2a3a4a5a6a7a8));
 r[11]=706;r[3]=0x12345678;assert(!syscall_rsx_net_ext(s) && r[3]==0x12345678);
''')

    def test_blocked_receive_abort_close_and_scheduler(self):
        self.run_native(r'''
 assert(invoke(713,2,2,0)==24);
 s.pc=0x10500;s.ppu_threads.push_back(static_cast<PpuContext&>(s));
 PpuContext peer;peer.thread_id=2;peer.pc=0x10600;s.ppu_threads.push_back(peer);
 mem.store(0x10100,8,UINT64_C(0x1122334455667788));
 mem.store(0x10140,8,UINT64_C(0xaabbccddeeff0011));mem.store(0x10120,4,16);
 bool blocked=false;try{invoke(707,24,0x10100,8,0,0x10140,0x10120);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==707 && s.wait_id==24 && s.pc==0x10500);
 ppu_schedule(s,true);assert(s.thread_id==2);
 assert(invoke(721,0,24,1)==0 && s.rsx_net_ext.sockets.size()==1);
 assert(invoke(721,0,24,1)==UINT64_C(0xfffffffffffffffe)); // strict, no waiters
 ppu_schedule(s,true);assert(s.thread_id==1 && s.wait_kind==0 && s.pc==0x10504);
 assert(r[3]==UINT64_C(0xfffffffffffffffc) && r[4]==0x10100); // -EINTR 4
 assert(mem.load(0x10100,8)==UINT64_C(0x1122334455667788));
 assert(mem.load(0x10140,8)==UINT64_C(0xaabbccddeeff0011) && mem.load(0x10120,4)==16);
 blocked=false;try{invoke(707,24,0x10100,8);}catch(const PpuBlocked&){blocked=true;}assert(blocked);
 ppu_schedule(s,true);assert(s.thread_id==2);assert(invoke(714,24)==0);
 ppu_schedule(s,true);assert(s.thread_id==1 && r[3]==UINT64_C(0xfffffffffffffffc) && s.pc==0x10508);
 assert(invoke(721,0,24,0)==UINT64_C(0xfffffffffffffff7));
 assert(invoke(721,5)==UINT64_C(0xffffffffffffffea));
 fault([&]{invoke(721,1);});
''')

    def test_poll_invalid_fds_be_layout_and_preserved_input_on_stop(self):
        self.run_native(r'''
 assert(invoke(715,0,0,100)==0);
 mem.store(0x10100,8,UINT64_C(0xffffffff0001aabb));
 mem.store(0x10108,8,UINT64_C(0x000003ff0004ccdd));
 assert(invoke(715,0x10100,2,0)==1);
 assert(mem.load(0x10100,8)==UINT64_C(0xffffffff00010000));
 assert(mem.load(0x10108,8)==UINT64_C(0x000003ff00040020));
 assert(invoke(713,2,2,0)==24);mem.store(0x10108,8,UINT64_C(0x000000180001ccdd));
 fault([&]{invoke(715,0x10100,2,0);});
 assert(mem.load(0x10108,8)==UINT64_C(0x000000180001ccdd));
 fault([&]{invoke(715,0x10100,1,1);});
''')


if __name__ == '__main__':
    unittest.main()
