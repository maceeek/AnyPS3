"""SPUPUT: the SPU reservation-lost check (spu_collect_events) asks a byte-exact observer only about the reserved 128-byte line.

Independent expectations (hand-placed watches, not derived from the code under test):
- polling watches (RSX labels/reports) inside the reserved line and any watch on another line of the same page: no observer
  call, the published-change shortcut stays valid (one full compare), the reservation survives;
- a later write to the line through the CPU path still loses the reservation;
- a synchronous watch on one byte of the reserved line: every check runs the observer (it may materialise a deferred write),
  and a materialising callback that writes the line loses the reservation.
"""
import unittest

from tests import test_par9_coordinator as par9

BODY = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();
 NativeSpuWorker lane;NativeSpuWorker::current=&lane;
 NativeSpu::Thread t;t.id=1;t.event_mask=0x400;t.decrementer_running=false;
 auto* page=m.regions[0].bytes->data();       // guest 0x10000: one host page holds lines 0x10000..0x10f80
 unsigned callbacks=0;bool materialize=false;
 {GuestMemory::RawPermit metadata(m);
  m.host_access_observer=[&](const void*,std::uint64_t,bool){++callbacks;
   if(materialize){materialize=false;GuestMemory::ObserverPause pause(m);m.store(0x10204,4,0x55667788);}};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;}
 {GuestMemory::MetadataPermit metadata(m);
  m.host_access_observer.watch_poll(page+0x210,16,true);   // report inside the reserved line 0x10200
  m.host_access_observer.watch_poll(page+0x400,16,true);   // report on another line of the page
  m.host_access_observer.watch(page+0x800,64,true);}       // synchronous watch on another line of the page
 std::uint8_t bytes[128]{};
 m.getllar(0x10200,bytes,t.reservation);t.reservation_address=0x10200;
 lane.watch_reservation(m,t.reservation.first);
 for(unsigned i=0;i<100;++i)spu_collect_events(s,t);
 assert(callbacks==0 && !t.event_ready);
 assert(token_calls==1);
 m.store(0x10280,4,7);spu_collect_events(s,t);assert(!t.event_ready && callbacks==0);   // next line: not ours
 m.store(0x10208,4,9);spu_collect_events(s,t);assert(t.event_ready && t.event_status==0x400 && callbacks==0);
 // One synchronous byte inside the line: every check runs the observer, which may lose the reservation.
 t.event_ready=false;t.event_status=0;m.getllar(0x10200,bytes,t.reservation);t.reservation_address=0x10200;
 {GuestMemory::MetadataPermit metadata(m);m.host_access_observer.watch(page+0x27f,1,true);}
 const auto before=token_calls;callbacks=0;
 for(unsigned i=0;i<10;++i)spu_collect_events(s,t);
 assert(token_calls==before+10 && callbacks==10 && !t.event_ready);
 materialize=true;spu_collect_events(s,t);
 assert(!materialize && t.event_ready && t.event_status==0x400 && m.load(0x10204,4)==0x55667788);
 {GuestMemory::RawPermit metadata(m);m.host_access_observer=GuestMemory::HostObserver{};}
 NativeSpuWorker::current=nullptr;lane.reservation_watch.reset();
 std::puts("PASS byte-exact reservation observer interest: polls/other lines skip the owner, own-line sync watch keeps it");
'''


class SpuPutReservation(par9.Par9Contracts):
    def test_reservation_check_is_byte_exact(self):
        def instrument(code):
            target = 's.memory.reservation_token(t.reservation_address)'
            start = code.index('static void spu_collect_events(')
            end = code.index('// Local counters', start)
            return code[:start] + 'static unsigned token_calls=0;\n' + code[start:end].replace(target, '(++token_calls,' + target + ')') + code[end:]
        self.native('spuput-reservation', BODY, instrument)


# Run only this module's test; the inherited PAR9 contracts have their own module.
for _name in [n for n in dir(par9.Par9Contracts) if n.startswith('test_')]:
    setattr(SpuPutReservation, _name, None)

if __name__ == '__main__':
    unittest.main()
