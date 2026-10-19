"""Native LV2 ABI regressions, constants from firmware 3.01 / RPCS3 ffba2bf0."""
import shutil
import unittest

from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Lv2SyncExtTests(unittest.TestCase):
    def native(self, body):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;
 auto ext=[&](unsigned n,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,
              std::uint64_t d=0,std::uint64_t e=0){r[11]=n;r[3]=a;r[4]=b;r[5]=c;r[6]=d;r[7]=e;syscall(s);return r[3];};
 auto blocked=[&](auto f){bool hit=false;try{f();}catch(const PpuBlocked&){hit=true;}assert(hit);};
 (void)blocked;
'''+body)

    def test_flag_abi_modes_clear_errors_and_ipc(self):
        self.native(r'''
 mem.store(0x10100,4,1);mem.store(0x10104,4,0x200);mem.store(0x10114,4,0x20000);
 assert(ext(82,0x10000,0x10100,0x8000000000000005ULL)==0);
 const auto id=mem.load(0x10000,4);assert(id==0x98000000 && mem.load(0x10000,1)==0x98);
 assert(ext(86,id,3,1,0x10008)==0x8001000a && mem.load(0x10008,8)==0);
 assert(ext(86,id,1,0x12,0x10008)==0 && mem.load(0x10008,8)==0x8000000000000005ULL);
 assert(ext(139,id,0x10010)==0 && mem.load(0x10010,8)==0x8000000000000004ULL);
 assert(ext(118,id,4)==0 && ext(139,id,0x10010)==0 && mem.load(0x10010,8)==4);
 assert(ext(86,id,4,0x21,0x10008)==0 && mem.load(0x10008,8)==4);
 assert(ext(139,id,0x10010)==0 && mem.load(0x10010,8)==0);
 assert(ext(86,id,0,1,0x10008)==0); // AND over an empty bit set is true.
 for(unsigned mode:{0u,3u,0x31u,0x101u})assert(ext(86,id,1,mode,0x10008)==0x80010002);
 assert(ext(139,id,0)==0x8001000d);
 assert(ext(83,id)==0 && ext(139,id,0x10010)==0x80010005 && mem.load(0x10010,8)==0);
 assert(ext(82,0,0x10100)==0x8001000d);
 mem.store(0x10104,4,0x100);mem.store(0x10108,8,123);mem.store(0x10110,4,2);
 assert(ext(82,0x10000,0x10100)==0x80010005);
 mem.store(0x10110,4,1);assert(ext(82,0x10000,0x10100)==0);const auto shared=mem.load(0x10000,4);
 assert(ext(82,0x10004,0x10100)==0x80010014);
 mem.store(0x10110,4,2);assert(ext(82,0x10004,0x10100)==0);
 const auto alias=mem.load(0x10004,4);assert(alias!=shared);
 assert(ext(87,shared,0x1234)==0 && ext(139,alias,0x10010)==0 && mem.load(0x10010,8)==0x1234);
 assert(ext(83,shared)==0 && ext(139,shared,0x10010)==0x80010005);
 assert(ext(139,alias,0x10010)==0 && mem.load(0x10010,8)==0x1234 && ext(83,alias)==0);
 mem.store(0x10110,4,1);assert(ext(120,0x10000,0x10100)==0);const auto rw=mem.load(0x10000,4);
 mem.store(0x10110,4,2);assert(ext(120,0x10004,0x10100)==0);const auto rw_alias=mem.load(0x10004,4);assert(rw_alias!=rw);
 assert(ext(125,rw)==0 && ext(121,rw_alias)==0x8001000a);
 assert(ext(127,rw_alias)==0 && ext(121,rw)==0 && ext(123,rw)==0x80010005);
 assert(ext(123,rw_alias)==0 && ext(124,rw_alias)==0 && ext(121,rw_alias)==0);
 r[11]=84;assert(!syscall_sync_ext(s));r[11]=119;assert(!syscall_sync_ext(s));
''')

    def test_flag_scheduler_cancel_timeout_and_single_waiter(self):
        self.native(r'''
 mem.store(0x10100,4,1);mem.store(0x10104,4,0x200);mem.store(0x10114,4,0x10000);
 assert(ext(82,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);
 PpuContext peer;peer.thread_id=2;s.ppu_threads={static_cast<PpuContext&>(s),peer};
 blocked([&]{ext(85,id,4,0x11,0x10008);});assert(s.wait_kind==9);
 ppu_schedule(s,true);assert(s.thread_id==2);
 assert(ext(85,id,8,1,0x10010)==0x80010009 && ext(83,id)==0x8001000a);
 assert(ext(87,id,5)==0);ppu_schedule(s);assert(s.thread_id==1);
 // The bit was reserved and cleared by the signal, before CPU resumption.
 assert(s.sync_ext.flags.front().pattern==1);
 assert(ext(85,id,4,0x11,0x10008)==0 && mem.load(0x10008,8)==5);
 blocked([&]{ext(85,id,8,1,0x10008);});ppu_schedule(s,true);assert(s.thread_id==2);
 assert(ext(132,id,0x10020)==0 && mem.load(0x10020,4)==1);
 ppu_schedule(s);assert(s.thread_id==1);
 assert(ext(85,id,8,1,0x10008)==0x80010013 && mem.load(0x10008,8)==1);
 blocked([&]{ext(85,id,8,1,0x10008,1000);});
 s.sync_ext.waits.at(1).deadline=1;ppu_schedule(s,true);
 if(s.thread_id!=1)ppu_schedule(s);
 assert(s.thread_id==1 && ext(85,id,8,1,0x10008,1000)==0x8001000b);
 assert(ext(83,id)==0);
''')

    def test_flag_fifo_priority_and_multiple_clear(self):
        self.native(r'''
 mem.store(0x10100,4,1);mem.store(0x10104,4,0x200);mem.store(0x10114,4,0x20000);
 assert(ext(82,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);
 PpuContext b,c;b.thread_id=2;b.priority=100;c.thread_id=3;c.priority=10;
 s.ppu_threads={b,c};s.thread_id=2;s.priority=100;
 blocked([&]{ext(85,id,1,0x11,0x10008);});
 s.thread_id=3;s.priority=10;s.wait_kind=0;blocked([&]{ext(85,id,1,0x11,0x10010);});
 s.thread_id=1;s.wait_kind=0;assert(ext(87,id,1)==0);
 assert(s.sync_ext.waits.at(2).done && !s.sync_ext.waits.at(3).done);
 assert(ext(132,id)==0);
 s.sync_ext.waits.clear();s.sync_ext.flags.front().protocol=2;
 s.thread_id=2;s.priority=100;blocked([&]{ext(85,id,1,0x11,0x10008);});
 s.thread_id=3;s.priority=10;s.wait_kind=0;blocked([&]{ext(85,id,1,0x11,0x10010);});
 s.thread_id=1;s.wait_kind=0;assert(ext(87,id,1)==0);
 assert(!s.sync_ext.waits.at(2).done && s.sync_ext.waits.at(3).done);
''')

    def test_rwlock_readers_writer_preference_handoff_and_timeout(self):
        self.native(r'''
 mem.store(0x10100,4,2);mem.store(0x10104,4,0x200);
 assert(ext(120,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);assert(id==0x88000000);
 assert(ext(124,id)==0x80010009 && ext(127,id)==0x80010009);
 assert(ext(122,id)==0 && ext(123,id)==0 && ext(126,id)==0x8001000a);
 PpuContext peer;peer.thread_id=2;s.ppu_threads={static_cast<PpuContext&>(s),peer};
 s.thread_id=2;blocked([&]{ext(125,id,1000);});s.thread_id=1;s.wait_kind=0;
 assert(ext(123,id)==0x8001000a && ext(121,id)==0x8001000a);
 assert(ext(124,id)==0 && s.sync_ext.rwlocks.front().writer==0);
 assert(ext(124,id)==0 && s.sync_ext.rwlocks.front().writer==2);
 s.thread_id=2;assert(ext(125,id,1000)==0);
 assert(ext(125,id)==0x80010008 && ext(126,id)==0x80010008);
 s.thread_id=1;assert(ext(127,id)==0x80010009);
 blocked([&]{ext(122,id,1000);});s.sync_ext.waits.at(1).deadline=1;sync_ext_poll(s);
 assert(ext(122,id,1000)==0x8001000b);
 blocked([&]{ext(122,id);});s.thread_id=2;s.wait_kind=0;assert(ext(127,id)==0);
 assert(s.sync_ext.rwlocks.front().readers==1);
 s.thread_id=1;assert(ext(122,id)==0 && ext(124,id)==0 && ext(121,id)==0);
 assert(ext(122,id)==0x80010005);
''')

    def test_thread_stack_condition_ipc_sleep_tty_and_rtc(self):
        self.native(r'''
 assert(ext(18)==0 && ext(43)==0 && s.reschedule);
 mem.map(0xd0000000,0x100000,6,nullptr,0);
 assert(ext(49,0x10000)==0 && mem.load(0x10000,4)==0xd0000000 && mem.load(0x10004,4)==0x100000);
 assert(ext(351,0xd0000123,0x10008)==0 && mem.load(0x10008,8)==0x40000);
 assert(mem.load(0x10010,8)==8 && mem.load(0x10018,4)==4096 && mem.load(0x1001c,4)==0);
 assert(ext(351,0xf0000000,0x10008)==0x80010002);
 fault([&]{ext(351,0x10100,0x10008);}); // No fabricated page-size metadata.
 PpuContext peer;peer.thread_id=2;peer.joinable=true;s.ppu_threads={static_cast<PpuContext&>(s),peer};
 assert(ext(45,2)==0 && !s.ppu_threads.back().joinable && ext(45,2)==0x80010002);
 assert(ext(45,99)==0x80010005);s.ppu_threads.back().joinable=true;s.ppu_threads.back().joiner=1;
 assert(ext(45,2)==0x8001000a);
 s.ppu_threads.back().joiner=0;s.ppu_threads.back().returned=true;
 assert(ext(45,2)==0 && ext(45,2)==0x80010005);
 s.ppu_threads.push_back(peer);
 s.kernel.conditions.front().handles=1;const auto cond=s.kernel.conditions.front().id;
 assert(ext(110,cond,99)==0x80010005 && ext(110,cond,2)==0x80010009);
 s.ppu_threads.back().wait_kind=4;s.ppu_threads.back().wait_id=cond;
 assert(ext(110,cond,2)==0 && s.ppu_threads.back().resume_cond==cond);
 mem.store(0x10100,4,1);mem.store(0x10104,4,1);
 assert(ext(128,0x10020,0x10100,123,4)==0);
 assert(ext(134,0x10024,3,55)==0);const auto port=mem.load(0x10024,4);
 assert(ext(140,port,0)==0x80010002 && ext(140,port,9)==0x80010005);
 assert(ext(140,port,123)==0 && ext(140,port,123)==0x80010015);
 assert(ext(138,port,7,8,9)==0 && s.events.queues.back().data.front()[0]==55);
 assert(ext(142,0)==0);blocked([&]{ext(142,1);});
 assert(s.wait_kind==12 && s.sync_ext.waits.at(1).deadline>sync_ext_now());
 s.sync_ext.waits.at(1).deadline=1;sync_ext_poll(s);assert(ext(142,1)==0);
 assert(ext(143,60,1)==0x80010003 && ext(402,0,0,0,0)==0x8001002b);
 const auto unix_before=std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
 assert(ext(866,0x3002,0,0x10030,0x10038)==0);
 const auto rtc_unix=mem.load(0x10030,8)+946684800ULL;
 const auto unix_after=std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
 assert(rtc_unix>=std::uint64_t(unix_before) && rtc_unix<=std::uint64_t(unix_after));
 // Setting the virtual RTC to epoch 2000 must be observable immediately.
 assert(ext(866,0x3003,0)==0);
 assert(ext(866,0x3002,0,0x10030,0x10038)==0);
 assert(mem.load(0x10030,8)<=1 && mem.load(0x10038,8)==0);
 assert(ext(866,0x3002,2,0x10030,0x10038)==0x80010500);
 fault([&]{ext(866,0x3001,0,32);});
''')

    def test_container_shared_allocation_accounting_alias_and_page(self):
        self.native(r'''
 const auto initial=main_memory_used(s);
 assert(ext(324,0x10000,0x200000)==0);const auto cid=mem.load(0x10000,4);
 assert(ext(362,0xffff000000000000ULL,1,cid,0x200,0x10004)==0x80010010);
 assert(ext(362,0xffff000000000000ULL,0x10000,cid,0xc200,0x10004)==0x80010002);
 assert(ext(362,0xffff000000000000ULL,0x10000,99,0x200,0x10004)==0x80010005);
 assert(ext(362,123,0x10000,cid,0x200,0x10004)==0);const auto id=mem.load(0x10004,4);
 assert(s.mmapper.containers.front().used==0x10000 && main_memory_used(s)==initial+0x200000);
 assert(ext(362,123,0x10000,cid,0x200,0x10008)==0x80010014);
 assert(ext(362,124,0x200000,cid,0x400,0x10008)==0x80010004);
 assert(ext(325,cid)==0x8001000a);
 assert(ext(330,0x10000000,0x200,0,0x10008)==0);const auto base=mem.load(0x10008,4);
 assert(ext(334,base,id,0x40000)==0 && ext(334,base+0x10000,id,0x80000)==0);
 mem.store(base,4,0x12345678);assert(mem.load(base+0x10000,4)==0x12345678);
 assert(ext(351,base+1,0x10010)==0 && mem.load(0x10020,4)==0x10000);
 assert(ext(351,base+0x10000,0x10010)==0 && mem.load(0x10010,8)==0x80000);
 assert(ext(329,id)==0x8001000a);
 assert(ext(335,base,0x10008)==0 && ext(335,base+0x10000,0x10008)==0);
 assert(ext(329,id)==0 && s.mmapper.containers.front().used==0);
 assert(ext(325,cid)==0 && main_memory_used(s)==initial);
''')

    def test_mandatory_scheduler_waits_for_real_deadline_without_peer(self):
        self.native(r'''
 mem.store(0x10100,4,1);mem.store(0x10104,4,0x200);mem.store(0x10114,4,0x20000);
 assert(ext(82,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);
 const auto before=sync_ext_now();
 blocked([&]{ext(85,id,1,1,0x10008,2000);});
 ppu_schedule(s,true);
 assert(sync_ext_now()>=before+2000 && s.thread_id==1 && !s.wait_kind);
 assert(ext(85,id,1,1,0x10008,2000)==0x8001000b && mem.load(0x10008,8)==0);
''')

    def test_rwlock_fifo_priority_and_reader_batch(self):
        self.native(r'''
 mem.store(0x10100,4,1);mem.store(0x10104,4,0x200);
 assert(ext(120,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);
 assert(ext(125,id)==0);
 PpuContext b,c;b.thread_id=2;b.priority=100;c.thread_id=3;c.priority=10;s.ppu_threads={b,c};
 s.thread_id=2;s.priority=100;blocked([&]{ext(125,id);});
 s.thread_id=3;s.priority=10;s.wait_kind=0;blocked([&]{ext(125,id);});
 s.thread_id=1;s.wait_kind=0;assert(ext(127,id)==0 && s.sync_ext.rwlocks.front().writer==2);
 s.thread_id=2;assert(ext(125,id)==0 && ext(127,id)==0 && s.sync_ext.rwlocks.front().writer==3);
 s.thread_id=3;assert(ext(125,id)==0 && ext(127,id)==0);
 s.sync_ext.rwlocks.front().protocol=2;s.thread_id=1;assert(ext(125,id)==0);
 s.thread_id=2;blocked([&]{ext(125,id);});
 s.thread_id=3;s.wait_kind=0;blocked([&]{ext(125,id);});
 s.thread_id=1;s.wait_kind=0;assert(ext(127,id)==0 && s.sync_ext.rwlocks.front().writer==3);
 s.thread_id=3;assert(ext(125,id)==0 && ext(127,id)==0 && s.sync_ext.rwlocks.front().writer==2);
 s.thread_id=2;assert(ext(125,id)==0 && ext(127,id)==0);
 s.thread_id=1;assert(ext(125,id)==0);
 s.thread_id=2;blocked([&]{ext(122,id);});
 s.thread_id=3;s.wait_kind=0;blocked([&]{ext(122,id);});
 s.thread_id=1;s.wait_kind=0;assert(ext(127,id)==0 && s.sync_ext.rwlocks.front().readers==2);
 s.thread_id=2;assert(ext(122,id)==0 && ext(124,id)==0);
 s.thread_id=3;assert(ext(122,id)==0 && ext(124,id)==0 && ext(121,id)==0);
''')
