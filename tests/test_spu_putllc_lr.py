"""PUTLLC failure raises the lock-line reservation-lost event (concurrent writers, ACIT run 347load)."""
import shutil
import unittest

from tests import test_runtime_objects
from unittest.mock import patch
from anyps3 import spu


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class PutllcReservationLostTests(unittest.TestCase):
    def test_failed_putllc_always_leaves_lr_pending_under_concurrent_writes(self):
        # Fixed ordering forces loss AFTER the final event sample, immediately
        # before the real conditional store. No collision-rate/timing oracle.
        needle = 'success=reserved && s.memory.putllc(address,ls+local,t.reservation,false);'
        core = spu.CORE_CPP
        self.assertEqual(core.count(needle), 1)
        hooked = ('inline std::function<void()> putllc_test_before_commit;\n' +
                  core.replace(needle,
                    'success=reserved && (putllc_test_before_commit(),'
                    's.memory.putllc(address,ls+local,t.reservation,false));'))
        with patch.object(spu, 'CORE_CPP', hooked):
            test_runtime_objects.RuntimeObjectsTests().run_native(r'''

 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 mem.enable_concurrency();
 constexpr unsigned rounds=16384;
 std::atomic<unsigned> request{0},completed{0},writes{0};
 std::thread writer([&]{for(unsigned n=1;n<=rounds;++n){
  while(request.load(std::memory_order_acquire)!=n)std::this_thread::yield();
  mem.store(0x10100,4,n);++writes;completed.store(n,std::memory_order_release);
 }});
 unsigned next=0;
 putllc_test_before_commit=[&]{request.store(++next,std::memory_order_release);
  while(completed.load(std::memory_order_acquire)!=next)std::this_thread::yield();};
 spu_write_channel(s,t,ls,1,0x400);
 for(unsigned n=0;n<rounds;++n){
  spu_write_channel(s,t,ls,2,0x400);
  spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x10100);
  spu_write_channel(s,t,ls,21,0xd0);assert(spu_read_channel(s,t,27)==4);
  // The hook runs after collect_events but before the real version compare.
  spu_write_channel(s,t,ls,21,0xb4);assert(spu_read_channel(s,t,27)==1);
  assert((t.event_status&0x400) && t.event_ready);
  assert(t.reservation_address==UINT64_MAX);
  assert(spu_read_channel(s,t,0)==0x400);
 }
 writer.join();putllc_test_before_commit={};
 assert(writes==rounds && next==rounds && mem.load(0x10100,4)==16384);
 std::printf("forced_failures=16384 lost=0 writes=%u\n",writes.load());
''')


if __name__ == '__main__':
    unittest.main()
