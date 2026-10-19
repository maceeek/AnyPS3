"""Condition signaling must release/reacquire the real guest mutex."""
import shutil
import unittest

from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class LightweightSyncTests(unittest.TestCase):
    def test_wait_reacquires_mutex_restores_recursion_and_destroy_is_busy(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;
 mem.store(0x10200,4,2);mem.store(0x10204,4,16);r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);
 r[3]=0x10300;r[4]=0x10100;r[5]=0x10400;assert(lwcond_service(s,0)==0);
 assert(mem.load(0x10300,4)==0x10100 && mem.load(0x10304,4)==0x97000000);
 assert(lwcond_service(s,0)==MM_EBUSY);
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));PpuContext peer;peer.thread_id=2;s.ppu_threads.push_back(peer);
 r[3]=0x10100;r[4]=0;lwmutex_lock(s);r[3]=0x10100;lwmutex_lock(s);assert(mem.load(0x1010c,4)==1);
 r[3]=0x10300;r[4]=0;bool blocked=false;try{lwcond_service(s,2);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==8 && mem.load(0x10100,4)==UINT32_MAX && mem.load(0x1010c,4)==0);
 ppu_schedule(s,true);assert(s.thread_id==2);
 r[3]=0x10100;r[4]=0;lwmutex_lock(s);r[3]=0x10300;assert(lwcond_service(s,3)==0);
 assert(lwcond_service(s,1)==MM_EBUSY);
 ppu_schedule(s);assert(s.thread_id==1 && s.resume_lwcond==0x10300);
 blocked=false;try{lwcond_service(s,2);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && r[3]==0x10300 && s.wait_kind==7 && mem.load(0x10104,4)==1);
 ppu_schedule(s,true);assert(s.thread_id==2);r[3]=0x10100;lwmutex_unlock(s);
 ppu_schedule(s);assert(s.thread_id==1 && !s.wait_kind);
 assert(lwcond_service(s,2)==0 && mem.load(0x10100,4)==1 && mem.load(0x1010c,4)==1);
 assert(mem.load(0x10104,4)==0 && !s.lwmutex_waiting && !s.resume_lwcond);
 r[3]=0x10300;assert(lwcond_service(s,1)==0);
 assert(mem.load(0x10304,4)==0xfffffffe && lwcond_service(s,3)==MM_ESRCH);
''')

    def test_unlock_by_non_owner_returns_eperm_without_side_effect(self):
        # Firmware 3.01 liblv2 sys_lwmutex_unlock returns CELL_EPERM (0x80010009) when owner != caller.
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 mem.store(0x10200,4,2);mem.store(0x10204,4,16);r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);
 s.thread_id=7;r[3]=0x10100;r[4]=0;lwmutex_lock(s);assert(r[3]==0 && mem.load(0x10100,4)==7);
 r[3]=0x10100;r[4]=0;lwmutex_lock(s);assert(r[3]==0 && mem.load(0x1010c,4)==1);
 s.thread_id=9;r[3]=0x10100;lwmutex_unlock(s);
 assert(r[3]==0x80010009 && mem.load(0x10100,4)==7 && mem.load(0x1010c,4)==1 && mem.load(0x10104,4)==0);
 r[3]=0x10900;fault([&]{lwmutex_unlock(s);});   // never created: explicit stop
 s.thread_id=7;r[3]=0x10100;lwmutex_unlock(s);assert(r[3]==0 && mem.load(0x10100,4)==7 && mem.load(0x1010c,4)==0);
 r[3]=0x10100;lwmutex_unlock(s);assert(r[3]==0 && mem.load(0x10100,4)==0xffffffff);
 s.thread_id=9;r[3]=0x10100;lwmutex_unlock(s);assert(r[3]==0x80010009 && mem.load(0x10100,4)==0xffffffff);
 (void)call;
''')

    def test_timed_waits_expire_with_etimedout(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 mem.store(0x10200,4,2);mem.store(0x10204,4,16);r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);
 r[3]=0x10300;r[4]=0x10100;r[5]=0x10400;assert(lwcond_service(s,0)==0);
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));PpuContext peer;peer.thread_id=2;peer.wait_kind=99;s.ppu_threads.push_back(peer);
 // lwcond: timed wait releases the mutex; expiry reacquires it and returns CELL_ETIMEDOUT.
 r[3]=0x10100;r[4]=0;lwmutex_lock(s);r[3]=0x10100;lwmutex_lock(s);
 r[3]=0x10300;r[4]=1000;bool blocked=false;try{lwcond_service(s,2);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==8 && s.wait_deadline && mem.load(0x10100,4)==UINT32_MAX);
 s.wait_deadline=1;ppu_schedule(s,true);
 assert(s.thread_id==1 && !s.wait_kind && s.wait_timed_out);
 r[3]=0x10300;r[4]=1000;assert(lwcond_service(s,2)==0x8001000b);
 assert(mem.load(0x10100,4)==1 && mem.load(0x1010c,4)==1 && !s.resume_lwcond && !s.wait_deadline && !s.wait_timed_out && r[4]==1000);
 // lwmutex: owned by thread 2, a timed lock blocks then times out and drops its waiter count.
 r[3]=0x10100;lwmutex_unlock(s);r[3]=0x10100;lwmutex_unlock(s);mem.store(0x10100,4,2);
 r[3]=0x10100;r[4]=500;blocked=false;try{lwmutex_lock(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==7 && mem.load(0x10104,4)==1 && s.wait_deadline);
 s.wait_kind=0;s.wait_timed_out=true;r[3]=0x10100;r[4]=500;lwmutex_lock(s);
 assert(r[3]==0x8001000b && mem.load(0x10104,4)==0 && !s.lwmutex_waiting && mem.load(0x10100,4)==2);
 // Event queue: timed receive on an empty queue blocks even without a peer, then times out.
 s.ppu_threads.clear();
 mem.store(0x10500,4,2);mem.store(0x10504,4,1);assert(call(128,0x10600,0x10500,77,2)==0);const auto queue=mem.load(0x10600,4);
 blocked=false;try{call(130,queue,0,250);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==1 && s.wait_deadline);
 s.wait_kind=0;s.wait_timed_out=true;assert(call(130,queue,0,250)==0x8001000b && !s.wait_deadline);
 fault([&]{call(130,queue,0,0);});   // untimed receive with no peer still stops explicitly
''')

    def test_signal_is_not_sticky_and_selects_fifo_or_priority(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;mem.store(0x10200,4,1);mem.store(0x10204,4,32);r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);
 r[3]=0x10300;r[4]=0x10100;r[5]=0x10400;assert(lwcond_service(s,0)==0);
 assert(lwcond_service(s,3)==0 && !s.reschedule);
 r[4]=0;assert(lwcond_service(s,2)==MM_EPERM);
 PpuContext first,second;first.thread_id=2;first.priority=100;first.wait_kind=8;first.wait_id=0x10300;first.wait_sequence=2;
 second=first;second.thread_id=3;second.priority=200;second.wait_sequence=1;
 s.ppu_threads={first,second};assert(lwcond_service(s,3)==0);
 assert(s.ppu_threads[0].wait_kind==8 && s.ppu_threads[1].resume_lwcond==0x10300);
 s.ppu_threads={first,second};mem.store(0x10108,4,34);assert(lwcond_service(s,3)==0);
 assert(s.ppu_threads[0].resume_lwcond==0x10300 && s.ppu_threads[1].wait_kind==8);
 assert(lwcond_service(s,4)==0 && s.ppu_threads[1].resume_lwcond==0x10300);
''')
