"""Exactness of the reservation-lost check in spu_collect_events (change-counter filter, par_wait.py).

The filter may skip the full GuestMemory::reservation_token() compare only while no write or map change has been
published; these scenarios hold the result identical to "compare every time" for every kind of writer, aliasing
line, mapping skew and concurrent interleaving, with expectations derived from the scenario's own ordering.
"""
import shutil
import unittest

from tests import spu_native

SCENARIOS = {}

SCENARIOS['res-exact'] = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,0x100000,6,nullptr,0);m.map(0x500001,256,6,nullptr,0);m.enable_concurrency();
 NativeSpu::Thread t;t.id=1;t.event_mask=0x400;t.decrementer_running=false;
 std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 auto getllar=[&](unsigned address){spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,17,0);spu_write_channel(s,t,ls,18,address);
  spu_write_channel(s,t,ls,21,0xd0);assert(spu_read_channel(s,t,27)==4);assert(t.reservation_address==address);};
 auto clear=[&]{t.event_status=0;t.event_ready=false;};
 auto lost=[&]{spu_collect_events(s,t);return t.reservation_address==UINT64_MAX && (t.event_status&0x400) && t.event_ready;};
 auto intact=[&]{spu_collect_events(s,t);return t.reservation_address!=UINT64_MAX && !(t.event_status&0x400) && !t.event_ready;};
 // 1. Nothing written: every check keeps the reservation.
 getllar(0x10100);for(unsigned i=0;i<1000;++i)assert(intact());
 // 2. Every kind of writer to the reserved line is a loss, including a same-value store and either end of the line.
 auto writer=[&](const std::function<void()>& write){std::thread w(write);w.join();};
 std::pair<std::uintptr_t,std::uint64_t> token;std::uint8_t line[128]{};
 const std::function<void()> writers[]={
  [&]{m.store(0x10100,4,0);},                                    // same value as the zero-filled memory
  [&]{m.store(0x1017f,1,0x5a);},                                 // last byte of the granule
  [&]{m.store(0x10100,8,UINT64_C(0x1122334455667788));},
  [&]{std::uint8_t b[3]={1,2,3};m.transfer(0x10150,b,3,true);},  // DMA-style transfer
  [&]{m.putllc(0x10100,line,{},true);},                          // PUTLLUC of another agent
  [&]{std::pair<std::uintptr_t,std::uint64_t> k;m.load_reserved(0x10100,4,k);m.store_conditional(0x10100,4,9,k);},
 };
 for(const auto& w:writers){clear();getllar(0x10100);assert(intact());writer(w);assert(lost());}
 // The raw-scope writer (RSX/HLE style) belongs to the memory owner thread.
 clear();getllar(0x10100);assert(intact());{GuestMemory::RawPermit raw(m);m.mark_write(m.regions[0].bytes->data()+0x100,4);}assert(lost());
 // 3. A write BEFORE the next GETLLAR is part of its token: no loss. Baseline reuse must not hide the next write.
 for(unsigned round=0;round<64;++round){
  clear();getllar(0x10100);assert(intact());
  writer([&]{m.store(0x10104,4,round);});assert(lost());                       // after the read: lost
  writer([&]{m.store(0x10108,4,round);});clear();getllar(0x10100);assert(intact()); // before the read: included
  writer([&]{m.store(0x1010c,4,round);});assert(lost());
 }
 // 4. Other lines never raise it: same hash bucket (4096 lines apart), a neighbouring bucket, the same page.
 clear();getllar(0x10100);
 writer([&]{m.store(0x10100+4096*128,4,1);});assert(intact());
 writer([&]{m.store(0x10180,4,1);m.store(0x10200,4,2);m.store(0x10f80,4,3);});assert(intact());
 writer([&]{m.store(0x10100+4096*128,4,2);m.store(0x1017f+1,1,2);});assert(intact());
 writer([&]{m.store(0x10100,4,3);});assert(lost());                            // and a later real write still counts
 // 5. A granule that straddles two physical lines (map base inside a granule): both halves invalidate.
 clear();getllar(0x500080);assert(intact());writer([&]{m.store(0x500080,1,1);});assert(lost());
 clear();getllar(0x500080);assert(intact());writer([&]{m.store(0x5000ff,1,2);});assert(lost());
 // 6. A map epoch change loses every reservation.
 clear();getllar(0x10100);assert(intact());m.mapping_changed();assert(lost());
 // 7. After a loss the reservation is gone: further checks stay silent and raise nothing new.
 clear();assert(intact()==false);assert(t.event_status==0);
 // 8. Handshake stress with a fixed order: the writer stores only after the GETLLAR returned.
 {constexpr unsigned rounds=20000;std::atomic<unsigned> request{0},done{0};
  std::thread w([&]{for(unsigned n=1;n<=rounds;++n){while(request.load(std::memory_order_acquire)!=n)std::this_thread::yield();
   if(n&1)m.store(0x10100,4,n);else m.store(0x10100+4096*128,4,n);done.store(n,std::memory_order_release);}});
  for(unsigned n=1;n<=rounds;++n){
   clear();getllar(0x10100);request.store(n,std::memory_order_release);while(done.load(std::memory_order_acquire)!=n)std::this_thread::yield();
   if(n&1)assert(lost());else assert(intact());
  }
  w.join();}
 // 9. Free-running concurrent writer. Oracle: a store that STARTED after the GETLLAR returned and COMPLETED
 // before the check began must be reported (started/completed are published around each store).
 {std::atomic<std::uint64_t> started{0},completed{0};std::atomic<bool> stop{false};
  std::thread w([&]{while(!stop.load(std::memory_order_relaxed)){started.fetch_add(1);m.store(0x10100,4,unsigned(started.load()));completed.fetch_add(1);
   for(unsigned i=0;i<16;++i)std::this_thread::yield();}});
  unsigned reported=0,silent=0,required=0;
  for(unsigned round=0;round<2000000 && (round<30000 || required<100);++round){
   clear();getllar(0x10100);const auto s1=started.load(std::memory_order_acquire);
   for(unsigned i=0;i<(round&15);++i)std::this_thread::yield();
   const auto c=completed.load(std::memory_order_acquire);
   spu_collect_events(s,t);
   const bool was_lost=t.reservation_address==UINT64_MAX;
   if(c>s1){assert(was_lost);++required;}
   (was_lost?reported:silent)++;
  }
  stop=true;w.join();assert(required>=100 && reported>0);
  std::printf("RES_STRESS reported=%u silent=%u required=%u\n",reported,silent,required);}
 // 10. Same stress against a writer that only ever touches a colliding other line: never a loss.
 {std::atomic<bool> stop{false};
  std::thread w([&]{unsigned i=0;while(!stop.load(std::memory_order_relaxed))m.store(0x10100+4096*128,4,++i);});
  for(unsigned round=0;round<20000;++round){clear();getllar(0x10100);for(unsigned i=0;i<4;++i)assert(intact());}
  stop=true;w.join();}
'''

SCENARIOS['res-serial'] = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);
 NativeSpu::Thread t;t.id=1;t.event_mask=0x400;t.decrementer_running=false;
 std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 assert(!m.concurrent());
 spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,17,0);spu_write_channel(s,t,ls,18,0x10100);spu_write_channel(s,t,ls,21,0xd0);
 assert(spu_read_channel(s,t,27)==4);spu_collect_events(s,t);assert(t.reservation_address==0x10100 && !t.event_ready);
 m.store(0x10104,4,1);spu_collect_events(s,t);assert(t.reservation_address==UINT64_MAX && (t.event_status&0x400) && t.event_ready);
'''


SCENARIOS['res-observer'] = r'''
 // The full compare is not only a version compare: a synchronous host-access observer (RSX/GPU coherence) runs
 // when it reaches the observer's range and may materialize a deferred write that loses the reservation. The
 // shortcut must therefore be off for a covered line and on otherwise. Observable: the full compare takes the
 // reserved line's stripe, so with that stripe held a check blocks exactly when the full compare runs.
 PpuState s;auto& m=s.memory;m.map(0x10000,0x100000,6,nullptr,0);m.enable_concurrency();
 NativeSpu::Thread t;t.id=1;t.event_mask=0x400;t.decrementer_running=false;
 std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 auto getllar=[&](unsigned address){spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,17,0);spu_write_channel(s,t,ls,18,address);
  spu_write_channel(s,t,ls,21,0xd0);assert(spu_read_channel(s,t,27)==4);assert(t.reservation_address==address);};
 auto now_s=[]{return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();};
 auto takes_full_compare=[&]{
  auto& stripe=m.parallel->lines[(t.reservation.first/128)%m.parallel->lines.size()];
  stripe.lock();std::atomic<bool> finished{false};
  std::thread checker([&]{spu_collect_events(s,t);finished.store(true,std::memory_order_release);});
  // Observer callbacks of a foreign thread's access run on the memory owner (this thread): keep servicing them.
  const double end=now_s()+0.3;
  while(!finished.load(std::memory_order_acquire) && now_s()<end){m.service_coherence();std::this_thread::sleep_for(std::chrono::milliseconds(1));}
  const bool blocked=!finished.load(std::memory_order_acquire);stripe.unlock();
  while(!finished.load(std::memory_order_acquire)){m.service_coherence();std::this_thread::yield();}
  checker.join();return blocked;
 };
 getllar(0x10100);spu_collect_events(s,t);assert(!t.event_ready);   // first check compares and arms the filter
 assert(!takes_full_compare());                                       // no observer: shortcut
 unsigned callbacks=0;bool materialize=false;
 {GuestMemory::RawPermit raw(m);
  m.host_access_observer=[&](const void*,std::uint64_t,bool){++callbacks;
   if(materialize){materialize=false;GuestMemory::ObserverPause pause(m);m.store(0x10100,4,0x11223344);}};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
  m.host_access_observer.watch(m.regions[0].bytes->data()+0x8000,4);}  // another page entirely
 assert(!takes_full_compare() && callbacks==0 && !t.event_ready);     // unrelated observer: shortcut stays
 m.host_access_observer.watch(m.regions[0].bytes->data()+0x100,4);    // now it covers the reserved granule
 assert(takes_full_compare() && !t.event_ready);                      // every check compares (and runs coherence)
 for(unsigned i=0;i<10;++i){spu_collect_events(s,t);assert(!t.event_ready);}
 assert(callbacks>=10);
 materialize=true;spu_collect_events(s,t);                            // the observer's deferred write loses it
 assert(!materialize && t.event_ready && t.event_status==0x400 && t.reservation_address==UINT64_MAX);
 assert(m.load(0x10100,4)==0x11223344);
'''

@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuReservationFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory, cls.exe = spu_native.build(SCENARIOS, name='spu-reservation-filter')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def scenario(self, name):
        code, out, err = spu_native.run(self.directory, self.exe, name)
        self.assertEqual(code, 0, f'{name}: {err[-6000:]}')
        return out

    def test_reservation_check_is_exact_for_every_writer_and_alias(self):
        self.assertIn('RES_STRESS', self.scenario('res-exact'))

    def test_covering_synchronous_observer_keeps_full_compare(self):
        self.scenario('res-observer')

    def test_serial_memory_reservation_check_unchanged(self):
        self.scenario('res-serial')


if __name__ == '__main__':
    unittest.main()
