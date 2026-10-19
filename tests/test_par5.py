"""PAR5: watched scalar, parked admission and shared epoch native oracles."""
from pathlib import Path
import os,shutil,subprocess,unittest
from tests.test_par1 import source
ARTIFACTS=Path('local/ratchet/codex/par5/tests')
@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class Parallel5Contracts(unittest.TestCase):
 def native(self,name,body):
  ARTIFACTS.mkdir(parents=True,exist_ok=True);cpp=ARTIFACTS/(name+'.cpp');cpp.write_text(source(body))
  result=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(cpp),'-o',str(cpp.with_suffix(''))],capture_output=True,text=True,timeout=240)
  cpp.with_suffix('.build.log').write_text(result.stdout+result.stderr);self.assertEqual(result.returncode,0,result.stderr[-8000:])
  run=subprocess.run([str(cpp.with_suffix(''))],capture_output=True,text=True,timeout=45)
  cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr);self.assertEqual(run.returncode,0,run.stderr[-8000:])
 def test_watched_cached_scalar_alias_endian_origin_and_reservations(self):
  self.native('scalar',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});m.mapping_changed();
 auto* base=m.regions[0].bytes->data();std::vector<HostAccessOrigin> origin;unsigned calls=0;
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;origin.push_back(HostAccessContext::sample());};
 m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(base,4096);
 std::uint32_t tid=7,pc=0x2028756c;std::uint64_t op=123;HostAccessContext context(HostAccessOrigin::Engine::Ppu,tid,pc,&op);
 m.store(0x11000,8,UINT64_C(0x0123456789abcdef)); // Cache miss and hit must agree.
 m.store(0x11000,4,0x11223344);assert(m.load(0x21000,8)==UINT64_C(0x1122334489abcdef) && calls==0);
 auto token=m.reservation_token(0x11000,4);m.store(0x21001,2,0xaabb);assert(!m.store_conditional(0x11000,4,0,token));
 assert(m.load(0x11000,8)==UINT64_C(0x11aabb4489abcdef));
 token=m.reservation_token(0x11000,4);m.store(0x11080,4,8);assert(m.store_conditional(0x11000,4,0x55667788,token));
 assert(m.load(0x21000,4)==0x55667788 && calls==0);
 m.store(0x10000,4,0x11223344);m.store(0x10000,4,0x55667788);assert(m.load(0x20000,4)==0x55667788);
 assert(calls==3 && origin[1].guest_pc==pc && origin[1].ea==0x10000 && origin[1].operation_id==123 && origin[2].ea==0x20000);
 // Watch changes must invalidate a previously uninteresting cached address.
 m.host_access_observer.watch(base+4096,4);const auto old=calls;m.store(0x21000,1,0xab);assert(calls==old+1);
 m.host_access_observer.watch(base+4096,4,false);m.store(0x21000,1,0xcd);assert(calls==old+1);
 assert(base[4096]==0xcd && base[4097]==0x66); // Both views share actual BE bytes.
 // Cross-page and cross-line stores retain every dependency/version.
 auto a=m.reservation_token(0x11000,4);m.store(0x1107f,2,0x1234);assert(!m.store_conditional(0x11000,4,0,a));
 m.store(0x10fff,2,0xa1b2);assert(calls==old+2 && m.load(0x21000,1)==0xb2);
 fault([&]{m.store(UINT64_C(0xffffffff),8,0);});
 auto cached=m.load(0x21000,4);m.regions[1].flags=4;m.mapping_changed();fault([&]{m.store(0x21000,4,0);});assert(m.load(0x11000,4)==cached);
 m.regions[1].flags=7;m.mapping_changed();fault([&]{m.store(0x21000,4,0);});
''')
 def test_raw_readers_park_and_nested_yield_wakes(self):
  self.native('admission',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 std::atomic<bool> entered{false},read{false};std::thread reader;std::uint64_t cpu_ns=UINT64_MAX;
 {
  GuestMemory::RawPermit raw(m);GuestMemory::RawPermit nested(m);
  reader=std::thread([&]{entered=true;assert(m.load(0x10000,4)==0x11223344);read=true;});
  while(!entered.load()){std::this_thread::yield();}std::this_thread::sleep_for(std::chrono::milliseconds(30));
  clockid_t clock;assert(!pthread_getcpuclockid(reader.native_handle(),&clock));timespec a{},b{};assert(!clock_gettime(clock,&a));
  std::this_thread::sleep_for(std::chrono::milliseconds(150));assert(!clock_gettime(clock,&b));
  cpu_ns=(b.tv_sec-a.tv_sec)*UINT64_C(1000000000)+b.tv_nsec-a.tv_nsec;assert(!read && cpu_ns<5000000);
  m.store(0x10000,4,0x11223344);
  {GuestMemory::RawYield yield(m);reader.join();assert(read && !m.parallel->exclusive);}
  assert(m.parallel->exclusive && m.local_state().raw==2);
 }
 assert(!m.parallel->exclusive);std::printf("PARKED_READER cpu_ns=%llu wall_ns=150000000\n",(unsigned long long)cpu_ns);
 // Repeat the admission/release race, including reads which precede raw.
 std::atomic<bool> stop{false};std::atomic<unsigned> reads{0};
 std::thread racing([&]{while(!stop.load()){assert(m.load(0x10000,4)==0x11223344);++reads;}});
 for(unsigned i=0;i<2000;++i){GuestMemory::RawPermit raw(m);m.store(0x10000,4,0x11223344);}
 stop=true;racing.join();assert(!m.parallel->exclusive);
''')
 def test_raw_waits_for_existing_reader_without_spinning(self):
  self.native('drain',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();std::atomic<bool> entered{false};
 std::thread reader([&]{GuestMemory::Lease lease(m,0x10000,4,false);entered=true;std::this_thread::sleep_for(std::chrono::milliseconds(150));});
 while(!entered.load()){std::this_thread::yield();}timespec a{},b{};assert(!clock_gettime(CLOCK_THREAD_CPUTIME_ID,&a));
 {GuestMemory::RawPermit raw(m);assert(!clock_gettime(CLOCK_THREAD_CPUTIME_ID,&b));}
 reader.join();auto cpu_ns=(b.tv_sec-a.tv_sec)*UINT64_C(1000000000)+b.tv_nsec-a.tv_nsec;
 assert(cpu_ns<5000000);std::printf("PARKED_OWNER cpu_ns=%llu\n",(unsigned long long)cpu_ns);
''')
 def test_range_epoch_shared_publish_revoke_and_copy(self):
  self.native('epoch',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.store(0x10000,4,0x11223344);
 auto r=m.range(0x10000,4,false);auto old_epoch=static_cast<std::uint64_t>(r.epoch);m.map(0x20000,4096,6,nullptr,0);
 assert(old_epoch!=m.parallel->epoch);std::vector<std::thread> readers;
 for(unsigned i=0;i<4;++i)readers.emplace_back([&]{for(unsigned n=0;n<1000;++n){std::uint8_t bytes[4]{};r.read(0,bytes,4);assert(bytes[0]==0x11 && bytes[1]==0x22 && bytes[2]==0x33 && bytes[3]==0x44);}});
 for(auto& t:readers){t.join();}assert(static_cast<std::uint64_t>(r.epoch)==m.parallel->epoch);
 auto copy=r;assert(copy.epoch==r.epoch);copy.epoch=0;assert(static_cast<std::uint64_t>(r.epoch)==m.parallel->epoch);
 {GuestMemory::RawPermit raw(m);m.regions[0].identity=GuestMemory::region_identity();m.mapping_changed();}
 std::uint8_t bytes[4]{};fault([&]{r.read(0,bytes,4);});assert(static_cast<std::uint64_t>(r.epoch)!=m.parallel->epoch);
''')
 def test_owner_scalar_with_active_broker_unrelated_and_conflicting_claims(self):
  self.native('owner-broker',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);auto pin=m.regions[0].bytes;
 m.regions.push_back({0x20000,6,pin});m.mapping_changed();m.enable_concurrency();m.enable_page_broker();
 unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};m.host_access_observer.filtered=true;
 m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(pin->data()+4096,4);
 m.store(0x10000,4,0x11223344);auto token=m.reservation_token(0x10000,4);
 auto other=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{pin->data()+4096,4,pin}});
 for(unsigned i=0;i<1000;++i){m.store(0x20000,4,0x55667788);assert(m.load(0x10000,4)==0x55667788);}
 assert(calls==0 && !m.store_conditional(0x10000,4,0,token));other.reset();
 // Prime a negative index hit before acquiring that same physical page.
 assert(m.load(0x10000,4)==0x55667788);auto range=m.range(0x10000,4,true);
 auto claim=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{pin->data(),4,pin}});
 std::thread gpu([&,owned=std::move(claim)]()mutable{owned->adopt();
  std::this_thread::sleep_for(std::chrono::milliseconds(30));const std::uint8_t bytes[]={0xaa,0xbb,0xcc,0xdd};owned->commit(range,0,bytes,4);owned.reset();});
 assert(m.load(0x20000,4)==0xaabbccdd);gpu.join();assert(m.load(0x10000,4)==0xaabbccdd && calls==0);
 {GuestMemory::Lease lease(m,0x10000,4,false);fault([&]{m.load(0x11000,4);});}
 // Unclaimed shortcuts still propagate broker poison. Direct foreign claim
 // acquisition cannot invalidate the owner-only negative index contract.
 std::thread foreign([&]{fault([&]{m.page_broker.acquire({});});});foreign.join();
 m.page_broker.stop(std::make_exception_ptr(std::runtime_error("fixed broker poison")));
 fault([&]{m.load(0x10000,4);});
''')
 def test_owner_callback_map_revalidation_and_new_broker_poison(self):
  self.native('owner-callback',r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();unsigned calls=0;
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;m.regions[0].flags=4;m.mapping_changed();};
 fault([&]{m.store(0x10000,4,0x11223344);});assert(calls==1 && m.regions[0].bytes->at(0)==0);
 m.host_access_observer={};{GuestMemory::RawPermit raw(m);m.regions[0].flags=6;m.mapping_changed();}
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;m.enable_page_broker();m.page_broker.stop(std::make_exception_ptr(std::runtime_error("callback poison")));};
 fault([&]{m.store(0x10000,4,0x11223344);});assert(calls==2 && m.regions[0].bytes->at(0)==0);
''')
 def test_fixed_width_dispatch_be_cross_word_lines_and_dynamic_api(self):
  self.native('fixed-widths',r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);auto pin=m.regions[0].bytes;
 m.regions.push_back({0x20000,6,pin});m.mapping_changed();m.enable_concurrency();m.enable_page_broker();
 unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};
 m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(pin->data()+4096,4);
 m.store(0x10001,1,0xaa);m.store(0x10002,2,0x1122);m.store(0x10004,4,0x33445566);m.store(0x10008,8,UINT64_C(0x778899aabbccddee));
 const std::uint8_t expected[]={0,0xaa,0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,0x99,0xaa,0xbb,0xcc,0xdd,0xee};
 assert(std::equal(std::begin(expected),std::end(expected),pin->data()));
 assert(m.load(0x20001,1)==0xaa && m.load(0x20002,2)==0x1122 && m.load(0x20004,4)==0x33445566 && m.load(0x20008,8)==UINT64_C(0x778899aabbccddee));
 auto first=m.reservation_token(0x10000,4),second=m.reservation_token(0x10080,4);
 m.store(0x2007d,8,UINT64_C(0x0123456789abcdef));
 assert(m.load(0x1007d,8)==UINT64_C(0x0123456789abcdef) && m.load(0x1007f,2)==0x4567);
 assert(pin->at(125)==0x01 && pin->at(132)==0xef);
 assert(!m.store_conditional(0x10000,4,0,first) && !m.store_conditional(0x10080,4,0,second));
 m.store(0x10101,3,0xa1b2c3);assert(m.load(0x20101,3)==0xa1b2c3);
 // Preserve the non-template dynamic API, including address-of-member uses.
 auto get=&GuestMemory::load_parallel;auto put=&GuestMemory::store_parallel;
 (m.*put)(0x20104,4,0x10203040);assert((m.*get)(0x10104,4)==0x10203040);
 fault([&]{m.store(0x11ffc,8,UINT64_MAX);});fault([&]{m.store(0x10000,9,0);});
 {GuestMemory::RawPermit raw(m);m.regions[0].flags=4;m.mapping_changed();}
 fault([&]{m.store(0x10008,8,0);});assert(m.load(0x20008,8)==UINT64_C(0x778899aabbccddee) && calls==0);
''')
