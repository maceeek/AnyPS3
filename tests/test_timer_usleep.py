import unittest

from tests import test_runtime_objects


class TimerUsleepTests(unittest.TestCase):
    def test_usleep_blocks_only_the_calling_thread(self):
        # LV2 sys_timer_usleep (141) suspends only its caller (RPCS3 sys_timer.cpp:450-483);
        # a ready peer must run immediately instead of the whole emulator sleeping.
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 s.thread_id=1;s.priority=1000;s.ppu_threads.push_back(static_cast<PpuContext&>(s));
 PpuContext peer;peer.thread_id=2;peer.priority=1000;s.ppu_threads.push_back(peer);
 const auto start=std::chrono::steady_clock::now();
 bool blocked=false;try{call(141,500000);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==12 && s.sync_ext.waits.count(1)==1);
 ppu_schedule(s,true);
 // Half a second of guest sleep did not stall the host: the peer was switched in at once.
 assert(s.thread_id==2 && std::chrono::steady_clock::now()-start<std::chrono::milliseconds(100));
 // Expire the sleep, then block the peer; thread 1 resumes and its retried syscall returns 0.
 s.sync_ext.waits.at(1).deadline=1;
 s.wait_kind=99;ppu_schedule(s,true);
 assert(s.thread_id==1 && !s.wait_kind);
 assert(call(141,500000)==0 && s.sync_ext.waits.count(1)==0);
 // A zero sleep only yields: no wait, success, reschedule requested.
 s.reschedule=false;assert(call(141,0)==0 && s.reschedule && !s.wait_kind && s.sync_ext.waits.empty());
 // During module initialization no other PPU thread can be scheduled: the host waits instead.
 s.module_loading=true;const auto loading=std::chrono::steady_clock::now();
 assert(call(141,3000)==0 && !s.wait_kind && s.sync_ext.waits.empty() &&
        std::chrono::steady_clock::now()-loading>=std::chrono::microseconds(3000));
 s.module_loading=false;
 // Sleeps above one second remain an explicit diagnostic stop (unchanged policy).
 bool stopped=false;try{call(141,1000001);}catch(const std::runtime_error&){stopped=true;}
 assert(stopped);
''')


if __name__ == '__main__':
    unittest.main()
