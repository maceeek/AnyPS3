"""PAR13 physical hazards, bounded map batches and independent ordering oracles."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source

ARTIFACTS = Path('local/ratchet/codex/par13/tests')


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Parallel13Memory(unittest.TestCase):
    def native(self, name, body, *, full=False, instrument=None):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        code = source(body, full=full)
        if instrument:
            code = instrument(code)
        cpp = ARTIFACTS / (name + '.cpp')
        cpp.write_text(code)
        command = ['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                   '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))]
        p = subprocess.run(command, capture_output=True, timeout=300)
        cpp.with_suffix('.build.log').write_bytes(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-12000:])
        env = {**os.environ, 'SDL_AUDIODRIVER': 'dummy', 'LP_NUM_THREADS': '4',
               'ANYPS3_SPU_THREADS': '0', 'ANYPS3_SPU_WORKER': '0', 'ANYPS3_RSX_WORKER': '0'}
        env.pop('ANYPS3_AUDIO_AUDIBLE', None)
        p = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, timeout=90, env=env)
        cpp.with_suffix('.run.log').write_bytes(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-12000:])

    def test_pending_map_hazard_blocks_raw_writer(self):
        self.native('pending-map', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 if(!m.parallel->asymmetric)return 0;
 std::atomic<bool> ready{false},retire{false},entered{false};
 std::thread cpu([&]{(void)m.load(0x10000,4);auto& local=m.local_state();
  assert(m.try_enter_scalar_reader(local));assert(!m.parallel->readers[local.slot].active.load());
  ready=true;while(!retire.load())std::this_thread::yield();m.leave_scalar_reader(local);m.release_reader_slot();});
 while(!ready.load())std::this_thread::yield();
 std::thread controller([&]{while(!m.parallel->exclusive.load())std::this_thread::yield();
  assert(!entered.load());retire=true;});
 {GuestMemory::RawPermit raw(m);entered=true;m.regions[0].flags=4;m.mapping_changed();}
 cpu.join();controller.join();fault([&]{m.store(0x10000,4,1);});
''')

    def test_first_ppu_reservation_drains_compact_untracked_write(self):
        self.native('first-reservation', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});
 auto pin=m.regions[0].bytes;m.enable_concurrency();m.enable_page_broker();
 if(!m.parallel->asymmetric)return 0;
 std::atomic<bool> ready{false},retire{false};auto state=m.parallel->snapshot->maps[0].state;
 std::thread writer([&]{m.store(0x20000,4,0);
  {GuestMemory::ScalarAccess access(m,0x20000,4,true,true,true);assert(access.map && !access.tracked->load());
   ready=true;while(!retire.load())std::this_thread::yield();GuestMemory::atomic_scalar_write(access.payload,4,0x11223344);}
  m.release_reader_slot();});
 while(!ready.load())std::this_thread::yield();
 std::thread controller([&]{while(!state->tracked[0].load())std::this_thread::yield();retire=true;});
 std::pair<std::uintptr_t,std::uint64_t> token;assert(m.load_reserved(0x10000,4,token)==0x11223344);
 writer.join();controller.join();m.store(0x20001,1,0x22);assert(!m.store_conditional(0x10000,4,0,token));
 std::array<std::uint8_t,128> bytes{};m.getllar(0x10000,bytes.data(),token);
 assert(bytes[0]==0x11 && bytes[1]==0x22 && bytes[2]==0x33 && bytes[3]==0x44);
 m.store(0x2007f,1,0xa5);assert(!m.putllc(0x10000,bytes.data(),token));
''')

    def test_gpu_claim_drains_tagged_scalar_and_keeps_payload(self):
        self.native('claim-tag', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 if(!m.parallel->asymmetric)return 0;
 auto pin=m.regions[0].bytes;auto range=m.range(0x10000,4,true);
 auto* cell=m.page_broker.cell(reinterpret_cast<std::uintptr_t>(pin->data())>>12);
 std::atomic<bool> ready{false},retire{false};
 std::thread cpu([&]{m.store(0x10000,4,0);
  {GuestMemory::ScalarAccess access(m,0x10000,4,true,true,true);assert(access.map);ready=true;
   while(!retire.load()){std::this_thread::yield();}GuestMemory::atomic_scalar_write(access.payload,4,0xaabbccdd);}
  m.release_reader_slot();});
 while(!ready.load())std::this_thread::yield();
 std::thread controller([&]{while(!(cell->state.load()&GuestMemory::PageBroker::claimed))std::this_thread::yield();retire=true;});
 {GuestMemory::GpuPages claim(m,{{pin->data(),4,pin}});
  assert((claim.snapshot(range,0,4)==std::vector<std::uint8_t>{0xaa,0xbb,0xcc,0xdd}));}
 cpu.join();controller.join();assert(m.load(0x10000,4)==0xaabbccdd);
''')

    def test_batch_revokes_at_raw_yield_and_mapping_mutation(self):
        self.native('batch-map', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 if(!m.parallel->asymmetric)return 0;
 auto& local=m.local_state();auto old=m.regions[0].bytes;
 {GuestMemory::ScalarBatch batch(m);m.store(0x10000,4,0x11223344);assert(m.load(0x10000,4)==0x11223344);
  assert(local.scalar_batch_active && m.parallel->readers[local.slot].active.load());
  {GuestMemory::RawYield yield(m);assert(!local.scalar_batch_active && !m.parallel->readers[local.slot].active.load());}
  assert(m.load(0x10000,4)==0x11223344 && local.scalar_batch_active);
  {GuestMemory::RawPermit raw(m);assert(!local.scalar_batch_active);m.regions[0].flags=4;m.mapping_changed();}
  fault([&]{m.store(0x10000,4,0);});assert(m.load(0x10000,4)==0x11223344);
  {GuestMemory::RawPermit raw(m);m.regions.clear();m.mapping_changed();}
  m.map(0x10000,8192,6,nullptr,0);assert(m.load(0x10000,4)==0);m.store(0x10000,4,0xaabbccdd);
  assert(m.load(0x10000,4)==0xaabbccdd && old->coherent_data()[0]==0x11);
  {GuestMemory::ScalarBatch nested(m);assert(m.load(0x10000,4)==0xaabbccdd);}
  assert(local.scalar_batch_active);
  m.release_reader_slot();assert(!local.scalar_batch_active && local.slot==UINT32_MAX);
  assert(m.load(0x10000,4)==0xaabbccdd);assert(m.load(0x10000,4)==0xaabbccdd);
 }
 assert(!local.scalar_batch_active && !m.parallel->readers[local.slot].active.load());
 {GuestMemory::ScalarBatch batch(m);assert(m.load(0x10000,4)==0xaabbccdd);
  fault([&]{m.load(UINT64_C(0x100000000),1);});}
 assert(!local.scalar_batch_active);
''')

    def test_live_observer_summaries_alias_dirty_and_lifecycle(self):
        self.native('summaries', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});auto pin=m.regions[0].bytes;
 m.host_access_observer=[](const void*,std::uint64_t,bool){};m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 m.host_access_observer.watch(pin->data()+63,2); // Replay pre-existing watches on enable.
 m.enable_concurrency();m.enable_page_broker();m.renderer_enabled=true;m.renderer_barrier=[](bool){};
 unsigned calls=0;m.renderer_access=[&](const void*,std::uint64_t,bool){assert(!m.local_state().scalar_batch_active);++calls;};
 {GuestMemory::ScalarBatch batch(m);
  m.store(0x20000,8,UINT64_C(0x1122334455667788));assert(m.load(0x10000,8)==UINT64_C(0x1122334455667788));assert(calls==0);
  (void)m.load(0x2003f,2);assert(calls>0);m.host_access_observer.watch(pin->data()+63,2,false);
  for(unsigned i=0;i<100;++i){auto before=calls;(void)m.load(0x20000,4);assert(calls==before);
   m.host_access_observer.watch(pin->data(),4);(void)m.load(0x20000,4);assert(calls>before);
   m.host_access_observer.watch(pin->data(),4,false);}
  auto range=m.range(0x10000,8,true);auto dirty=m.watch_cpu_dirty(range);assert(dirty->consume());
  m.store(0x20000,8,UINT64_C(0xaabbccddeeff0011));assert(dirty->consume());assert(!dirty->consume());
  m.store(0x20008,8,0);assert(!dirty->consume());m.unwatch_cpu_dirty(dirty);
  assert(!m.page_broker.cell(reinterpret_cast<std::uintptr_t>(pin->data())>>12)->dirty.load());
  {GuestMemory::RawPermit raw(m);m.host_access_observer=[](const void*,std::uint64_t,bool){};}
  auto before=calls;(void)m.load(0x20000,4);assert(calls>before); // Unfiltered observer bypasses summary.
  {GuestMemory::RawPermit raw(m);GuestMemory::HostObserver replacement;replacement=[](const void*,std::uint64_t,bool){};
   replacement.filtered=true;replacement.supports_exact_bytes=true;replacement.watch(pin->data()+8,4);m.host_access_observer=std::move(replacement);}
  before=calls;(void)m.load(0x20008,4);assert(calls>before);
  // Watches can predate mapping a retained physical backing. Choose an
  // unprepared 2MiB host radix block, not a lucky nearby prepared leaf.
  auto future=std::make_shared<GuestBacking>(0x400000,0);unsigned offset=0;
  while(offset<future->size() && m.page_broker.cell((reinterpret_cast<std::uintptr_t>(future->data())+offset)>>12))offset+=0x200000;
  assert(offset<future->size());m.host_access_observer.watch(future->data()+offset,4);
  {GuestMemory::RawPermit raw(m);m.regions.push_back({0x300000,6,future});m.mapping_changed();}
  before=calls;(void)m.load(0x300000+offset,4);assert(calls>before);
 }
 m.renderer_enabled=false;
''')

    def test_ppu_spu_dma_rsx_claim_release_acquire_stress(self):
        self.native('three-actors', r'''
 GuestMemory m;m.map(0x10000,0x6000,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();auto pin=m.regions[0].bytes;
 auto produced=m.range(0x12000,8,true),ack=m.range(0x14000,4,true);m.renderer_enabled=true;
 std::atomic<bool> ready{false};constexpr unsigned count=1000;
 std::thread rsx([&]{m.renderer_thread=std::this_thread::get_id();m.page_broker.delegated_owner=m.renderer_thread;ready=true;
  for(unsigned n=1;n<=count;++n){while(m.load(0x13000,4)!=n)std::this_thread::yield();
   m.local_state().renderer_claim=true;
   {GuestMemory::GpuPages claim(m,{{pin->data()+8192,8,pin}});auto bytes=claim.snapshot(produced,0,8);
    assert(bytes[0]==0x11 && bytes[1]==0x22 && bytes[2]==0x33 && bytes[3]==0x44);
    assert(bytes[4]==0 && bytes[5]==0 && bytes[6]==(n>>8) && bytes[7]==(n&255));}
   const std::uint8_t bytes[]={0,0,std::uint8_t(n>>8),std::uint8_t(n)};
   {GuestMemory::GpuPages claim(m,{{pin->data()+16384,4,pin}});claim.commit(ack,0,bytes,4);}
  }m.release_reader_slot();});
 std::thread spu([&]{for(unsigned n=1;n<=count;++n){while(m.load(0x11000,4)!=n)std::this_thread::yield();
   std::uint8_t bytes[8]{};m.transfer(0x10000,bytes,8,false);
   assert(bytes[0]==0x11 && bytes[3]==0x44 && bytes[6]==(n>>8) && bytes[7]==(n&255));
   m.transfer(0x12000,bytes,8,true);m.store(0x13000,4,n);
  }m.release_reader_slot();});
 while(!ready.load())std::this_thread::yield();
 for(unsigned n=1;n<=count;++n){GuestMemory::ScalarBatch batch(m);m.store(0x10000,8,UINT64_C(0x1122334400000000)|n);
  m.store(0x11000,4,n);while(m.load(0x14000,4)!=n)std::this_thread::yield();}
 spu.join();rsx.join();assert(m.load(0x14000,4)==1000);m.renderer_enabled=false;
''')

    def test_split_permissions_and_non_asymmetric_fallback(self):
        self.native('split-fallback', r'''
 GuestMemory m;m.map(0x10003,125,6,nullptr,0);m.map(0x10080,128,4,nullptr,0);
 m.map(0x20000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 for(unsigned mode=0;mode<2;++mode){if(mode)m.parallel->asymmetric=false;
  GuestMemory::ScalarBatch batch(m);m.store(0x2003f,8,UINT64_C(0x0123456789abcdef));
  assert(m.load(0x2003f,8)==UINT64_C(0x0123456789abcdef));
  m.store(0x20ffd,8,UINT64_C(0x1122334455667788));assert(m.load(0x20ffd,8)==UINT64_C(0x1122334455667788));
  fault([&]{m.store(0x1007f,2,0xffff);});assert(m.load(0x1007f,1)==0);
  std::uint8_t bytes[]={1,2,3,4};fault([&]{m.transfer(0x1007e,bytes,4,true);});assert(m.load(0x1007e,2)==0);
 }
''')

    def test_real_worker_raw_wait_revokes_batch_before_service(self):
        self.native('worker-revoke', r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","1",1);
 PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);SpuWorkerSession life(s);
 if(m.parallel->asymmetric){
  auto& q=*s.process.rsx_worker.queue;std::atomic<bool> entered{false};
  {GuestMemory::ScalarBatch batch(m);m.store(0x10000,4,0x11223344);assert(m.load(0x10000,4)==0x11223344);
   assert(m.local_state().scalar_batch_active);
   auto request=q.post(RsxOwnerQueue::Kind::HostAccess,[&]{GuestMemory::RawPermit raw(m);entered=true;});
   const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
   while(!m.parallel->exclusive.load()){assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}
   {SpuServiceScope service(s,true);assert(!m.local_state().scalar_batch_active);}
   q.await(request);assert(entered.load());assert(m.load(0x10000,4)==0x11223344);
  }
 }
 life.finish();
''', full=True)

    def test_synthetic_aot_dispatch_hook_preserves_steps_and_results(self):
        import struct
        from anyps3.prx import make_image
        from anyps3.program import compile_program
        # addi/xor/stw/lwz/add/cmp/bc loop: two memory ops per seven guest
        # instructions; independent sum and instruction count, no game code.
        words = [0x38c00000, 0x38600000, 0x38630001,
                 (31 << 26) | (4 << 21) | (4 << 16) | (3 << 11) | (316 << 1),
                 0x90620000, 0x80a20000,
                 (31 << 26) | (6 << 21) | (6 << 16) | (5 << 11) | (266 << 1),
                 0x7c233800, 0x4180ffe8, 0x4e800020]
        image = make_image([(0x10000, 5, struct.pack('>10I', *words)),
                            (0x20000, 6, struct.pack('>II', 0x10000, 0))], entry=0x20000)
        generated = compile_program(image, process_entry=True).cpp(max_steps=100000)
        anchor = 'try { ExecutionPpuPage ppu_perf_page(state.execution,state.thread_id,state.steps); { anyps3_dispatch_page(state);'
        self.assertEqual(generated.count(anchor), 1)
        generated = generated.replace(anchor, anchor.replace('anyps3_dispatch_page', 'GuestMemory::ScalarBatch scalar_batch(state.memory); anyps3_dispatch_page'))
        generated = generated.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        def instrument(code):
            return generated + code[code.rindex('\n#include <cassert>'):]
        self.native('aot-dispatch', r'''
 PpuState state;auto& m=state.memory;m.map(0x50000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 state.r[2]=0x50000;state.r[4]=0;state.r[7]=10000;state.pc=0x10000;state.lr=RETURN_SENTINEL;
 anyps3_entry(state,false);assert(state.returned && state.steps==70003);
 assert(state.r[3]==10000 && state.r[4]==10000 && state.r[6]==50005000);
 assert(m.load(0x50000,4)==10000 && !m.local_state().scalar_batch_active);
''', instrument=instrument)

    def test_word_aligned_doubleword_reads_two_words(self):
        # Independent expectation: the guest bytes are 0x10+i, so the big-endian doubleword at offset k is the constant
        # computed here in Python. Every 4-byte-aligned (not 8-byte-aligned) start takes the two-word read; every other
        # misaligned start keeps the byte path. Both the static helper and concurrent loads (ScalarAccess) are checked.
        expect = ''.join(f'{int.from_bytes(bytes(0x10 + i for i in range(k, k + 8)), "big"):#x}ull,' for k in range(24))
        self.native('dword-halves', r'''
 static const std::uint64_t expect[24]={''' + expect + r'''};
 alignas(64) std::uint8_t bytes[32];for(unsigned i=0;i<32;++i)bytes[i]=std::uint8_t(0x10+i);
 for(unsigned k=0;k<24;++k)assert(GuestMemory::atomic_scalar_read(bytes+k,8)==expect[k]);
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);for(unsigned i=0;i<32;++i)m.store(0x10ff0+i,1,0x10+i);
 m.enable_concurrency();m.enable_page_broker();
 std::thread reader([&]{for(unsigned k=0;k<24;++k)assert(m.load(0x10ff0+k,8)==expect[k]);m.release_reader_slot();});reader.join();
 for(unsigned k=0;k<24;++k)assert(m.load(0x10ff0+k,8)==expect[k]);
''')

    def test_broker_cell_cache_matches_cold_lookup_across_blocks(self):
        # 12 backings of 3 MiB occupy distinct 2 MiB host blocks, so the direct-mapped TLS cache entries collide. A cold
        # lookup in a fresh thread (empty cache: radix walk) is the reference; an interleaved order on one warm thread must
        # return the same cell for every page, and a claim published on one page is seen through that page's cell only.
        self.native('cell-cache', r'''
 GuestMemory m;for(unsigned i=0;i<12;++i)m.map(0x1000000+i*0x400000,0x300000,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 std::vector<std::uintptr_t> pages;
 for(const auto& r:m.regions){const auto at=reinterpret_cast<std::uintptr_t>(r.bytes->data())>>12;pages.push_back(at);pages.push_back(at+512);pages.push_back(at+(r.bytes->size()>>12)-1);}
 std::vector<GuestMemory::PageBroker::Cell*> cold(pages.size());
 for(std::size_t i=0;i<pages.size();++i){std::thread t([&]{cold[i]=m.page_broker.cell(pages[i]);});t.join();assert(cold[i]);}
 for(std::size_t i=0;i<pages.size();++i)for(std::size_t j=0;j<i;++j)assert(cold[i]!=cold[j]);
 for(unsigned round=0;round<64;++round)for(std::size_t i=0;i<pages.size();++i){const auto k=(i*7+round*5)%pages.size();assert(m.page_broker.cell(pages[k])==cold[k]);}
 auto pin=m.regions[5].bytes;
 {GuestMemory::GpuPages claim(m,{{pin->data(),4,pin}});
  for(std::size_t i=0;i<pages.size();++i)assert(bool(m.page_broker.cell(pages[i])->state.load()&GuestMemory::PageBroker::claimed)==(i==15));}
''')

    def page_read_case(self, name, body, switch='1'):
        previous = os.environ.get('ANYPS3_GUEST_PAGE_READ')
        os.environ['ANYPS3_GUEST_PAGE_READ'] = switch
        try:
            self.native(name, body)
        finally:
            if previous is None:
                os.environ.pop('ANYPS3_GUEST_PAGE_READ', None)
            else:
                os.environ['ANYPS3_GUEST_PAGE_READ'] = previous

    # Guest bytes of the one-page read tests: byte i of the 8 KiB map at 0x10000 holds (i*7+3)&0xff (independent of the code).
    PAGE_SETUP = r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);for(unsigned i=0;i<8192;++i)m.store(0x10000+i,1,(i*7+3)&0xff);
 m.enable_concurrency();m.enable_page_broker();
 auto expect=[](unsigned i){return std::uint8_t((i*7+3)&0xff);};
 auto same=[&](const std::uint8_t* got,unsigned at,unsigned n){for(unsigned i=0;i<n;++i)if(got[i]!=expect(at-0x10000+i))return false;return true;};
 (void)same;
'''

    def test_one_page_reads_without_lease(self):
        # transfer and Range::read inside one guest page complete on the compact path (page_reads counts them after the
        # first read filled the scalar map cache); a span across the page boundary keeps the Lease path. Same bytes
        # either way, and ANYPS3_GUEST_PAGE_READ=0 takes the Lease path for all of them.
        for switch in ('1', '0'):
            self.page_read_case('page-read-' + switch, self.PAGE_SETUP + r'''
 const bool on=std::getenv("ANYPS3_GUEST_PAGE_READ")[0]=='1';
 std::thread reader([&]{std::uint8_t b[4096];
  m.transfer(0x10000,b,4,false);m.transfer(0x11000,b,4,false);assert(same(b,0x11000,4));const auto warm=GuestMemory::page_reads;
  const unsigned spans[][2]={{0x10000,4096},{0x10004,64},{0x10ffc,4},{0x10123,77},{0x11000,1},{0x11f00,256}};
  for(const auto& s:spans){std::fill_n(b,sizeof(b),0);const auto before=GuestMemory::page_reads;m.transfer(s[0],b,s[1],false);
   assert(same(b,s[0],s[1]));assert(GuestMemory::page_reads==before+(on?1:0));}
  const auto before=GuestMemory::page_reads;m.transfer(0x10ff0,b,32,false);assert(same(b,0x10ff0,32));assert(GuestMemory::page_reads==before);
  auto range=m.range(0x10f00,512,false);range.read(0x40,b,64);assert(same(b,0x10f40,64));
  range.read(0xf8,b,16);assert(same(b,0x10ff8,16));   // crosses 0x11000: Lease path
  assert(GuestMemory::page_reads==before+(on?1:0));(void)warm;
  m.release_reader_slot();});
 reader.join();
''', switch)

    def test_one_page_read_declines_observed_bytes(self):
        # A byte-exact synchronous watch on part of the page: a read touching it runs the observer callback (Lease path),
        # a read of other bytes of the same page completes without one.
        self.page_read_case('page-read-observed', self.PAGE_SETUP + r'''
 auto pin=m.regions[0].bytes;std::atomic<unsigned> calls{0};
 {GuestMemory::RawPermit raw(m);m.host_access_observer=[&](const void* p,std::uint64_t n,bool write){
   assert(!write && p<pin->data()+0x108 && static_cast<const std::uint8_t*>(p)+n>pin->data()+0x100);++calls;};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
  m.host_access_observer.watch(pin->data()+0x100,8);}
 std::uint8_t b[64];m.transfer(0x10000,b,4,false);
 auto before=GuestMemory::page_reads;m.transfer(0x10200,b,64,false);assert(same(b,0x10200,64) && GuestMemory::page_reads==before+1 && calls==0);
 m.transfer(0x100c0,b,64,false);assert(same(b,0x100c0,64) && GuestMemory::page_reads==before+2 && calls==0);   // ends at 0x100 (excl.)
 m.transfer(0x100f8,b,16,false);assert(same(b,0x100f8,16) && calls==1 && GuestMemory::page_reads==before+2);
 auto range=m.range(0x10000,4096,false);calls=0;range.read(0x104,b,8);assert(same(b,0x10104,8) && calls==1 && GuestMemory::page_reads==before+2);
 range.read(0x400,b,8);assert(same(b,0x10400,8) && GuestMemory::page_reads==before+3);
''')

    def test_one_page_read_waits_for_claim_and_raw_writer(self):
        # A GPU claim and then an exclusive raw writer hold the page while a foreign reader asks for it: the read must not
        # complete before each releases, and it returns the bytes written under that ownership (new values, independent of
        # the read path). No deadlock: both owners finish without waiting for the reader.
        self.page_read_case('page-read-owners', self.PAGE_SETUP + r'''
 auto pin=m.regions[0].bytes;
 for(unsigned round=0;round<2;++round){
  std::atomic<bool> warm{false},go{false},released{false};std::atomic<int> seen{-1};
  std::thread reader([&]{std::uint8_t c[64];m.transfer(0x10400,c,64,false);const auto before=GuestMemory::page_reads;
   m.transfer(0x10400,c,64,false);assert(GuestMemory::page_reads==before+1);   // warm: the compact path is taken
   warm=true;while(!go.load())std::this_thread::yield();
   m.transfer(0x10400,c,64,false);seen=released.load()?c[0]:-2;
   for(unsigned i=1;i<64;++i){assert(c[i]==std::uint8_t(0xa0+round+i));}m.release_reader_slot();});
  while(!warm.load())std::this_thread::yield();
  std::optional<GuestMemory::GpuPages> claim;std::optional<GuestMemory::RawPermit> raw;
  if(round==0){claim.emplace(m,std::vector<GuestMemory::HostSpan>{{pin->data()+0x400,64,pin}});
   for(unsigned i=0;i<64;++i)claim->pointer(0)[i]=std::uint8_t(0xa0+round+i);}
  else{raw.emplace(m);auto* p=m.access(0x10400,64,true);for(unsigned i=0;i<64;++i)p[i]=std::uint8_t(0xa0+round+i);}
  go=true;std::this_thread::sleep_for(std::chrono::milliseconds(50));
  released=true;if(round==0)claim.reset();else raw.reset();
  reader.join();assert(seen.load()==int(0xa0+round));
 }
''')

    def test_one_page_read_faults_and_stale_range(self):
        # Unmapped, unreadable and stale-epoch reads still stop with the Lease path's errors after the scalar map cache
        # was warmed on the same page.
        self.page_read_case('page-read-faults', self.PAGE_SETUP + r'''
 std::uint8_t b[64];m.transfer(0x10000,b,64,false);m.transfer(0x11000,b,64,false);
 fault([&]{m.transfer(0x20000,b,16,false);});
 auto range=m.range(0x11000,64,false);range.read(0,b,16);assert(same(b,0x11000,16));
 {GuestMemory::RawPermit raw(m);m.regions[0].flags=2;m.mapping_changed();}
 fault([&]{m.transfer(0x10000,b,64,false);});
 {GuestMemory::RawPermit raw(m);m.regions[0].flags=6;m.mapping_changed();}
 m.transfer(0x10000,b,64,false);assert(same(b,0x10000,64));
 // The Range's backing is replaced at the same address: its pinned parts are stale and the read must refuse them.
 {GuestMemory::RawPermit raw(m);m.regions[0].bytes=std::make_shared<GuestBacking>(8192,0);m.mapping_changed();}
 m.transfer(0x11000,b,16,false);for(unsigned i=0;i<16;++i)assert(b[i]==0);
 bool stale=false;try{range.read(0,b,16);}catch(const std::runtime_error& e){stale=std::string(e.what()).find("stale guest range")!=std::string::npos;}
 assert(stale);
''')

    def cursor_case(self, name, body, switch='1'):
        previous = os.environ.get('ANYPS3_RSX_RENDER_BATCH')
        os.environ['ANYPS3_RSX_RENDER_BATCH'] = switch
        try:
            self.native(name, body)
        finally:
            if previous is None:
                os.environ.pop('ANYPS3_RSX_RENDER_BATCH', None)
            else:
                os.environ['ANYPS3_RSX_RENDER_BATCH'] = previous

    # Renderer page cursor: an 8 KiB map at 0x10000 whose word i holds 0x5a000000+i (independent of the code), a second map
    # at 0x20000 and a write-only map at 0x30000. `renderer` runs a body on its own thread registered as the renderer.
    CURSOR_SETUP = r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);for(unsigned i=0;i<2048;++i)m.store(0x10000+i*4,4,0x5a000000u+i);
 m.map(0x20000,4096,6,nullptr,0);m.map(0x30000,4096,2,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 std::atomic<unsigned> host_calls{0};
 m.renderer_access=[&](const void*,std::uint64_t,bool){++host_calls;};
 auto word=[](unsigned i){return std::uint64_t(0x5a000000u+i);};(void)word;
 auto renderer=[&](auto body){std::atomic<bool> bound{false};
  std::thread t([&m,&bound,body]{m.renderer_thread=std::this_thread::get_id();bound=true;body();m.release_reader_slot();});
  while(!bound.load()){std::this_thread::yield();}return t;};
 m.renderer_enabled=true;
'''

    def test_render_cursor_reads_live_words_and_revokes_for_other_apis(self):
        # Served loads return the words m.load returns; any other GuestMemory API on the thread (a store, a transfer, a
        # RawPermit of the renderer itself) revokes the cursor first, so its hazard is never overwritten or held across
        # them; reads crossing the host page, unmapped and unreadable addresses are not served and still fault through
        # m.load; ANYPS3_RSX_RENDER_BATCH=0 serves nothing. A foreign (non-renderer) thread never gets a cursor.
        for switch in ('1', '0'):
            self.cursor_case('cursor-basic-' + switch, self.CURSOR_SETUP + r'''
 const bool on=std::getenv("ANYPS3_RSX_RENDER_BATCH")[0]=='1';
 auto t=renderer([&]{auto& local=m.local_state();m.store(0x20000,4,6);   // warm: the store below admits on its first try
  {GuestMemory::RenderCursor cursor(m);std::uint64_t v=0;
   for(unsigned i=0;i<2048;++i){const std::uint64_t a=0x10000+i*4;if(!cursor.load(a,4,v))v=m.load(a,4);assert(v==word(i));}
   assert(GuestMemory::cursor_reads==(on?2046u:0u));   // the first read of each page fills the scalar map cache
   for(unsigned i=0;i+1<2048;i+=2){const std::uint64_t a=0x10000+i*4;if(!cursor.load(a,8,v))v=m.load(a,8);assert(v==((word(i)<<32)|word(i+1)));}
   for(unsigned i=1;i+1<1024;i+=2){const std::uint64_t a=0x10000+i*4;if(!cursor.load(a,8,v))v=m.load(a,8);assert(v==((word(i)<<32)|word(i+1)));}
   std::uint8_t bytes[64];assert(cursor.read(0x10100,bytes,64)==on);
   if(on)for(unsigned i=0;i<64;++i){assert(bytes[i]==std::uint8_t(word(0x40+i/4)>>(24-8*(i%4))));}
   assert(local.cursor_active==on && (m.parallel->readers[local.slot].page.load()!=UINTPTR_MAX)==on);
   m.store(0x20000,4,7);assert(!local.cursor_active && m.parallel->readers[local.slot].page.load()==UINTPTR_MAX);
   assert(cursor.load(0x10000,4,v)==on);m.transfer(0x10004,bytes,8,false);assert(!local.cursor_active);
   assert(cursor.load(0x10000,4,v)==on);{GuestMemory::RawPermit raw(m);assert(!local.cursor_active);assert(m.load(0x10008,4)==word(2));}
   assert(cursor.load(0x10ffc,8,v)==false && !local.cursor_active);   // crosses the page: not served
   assert(cursor.load(0x11000,4,v)==on && v==(on?word(1024):v));
   assert(!cursor.load(0x40000,4,v));fault([&]{(void)m.load(0x40000,4);});
   assert(!cursor.load(0x30000,4,v));fault([&]{(void)m.load(0x30000,4);});
   assert(!local.cursor_active && (cursor.load(0x11004,4,v) || true));}
  assert(!local.cursor_active && m.parallel->readers[local.slot].page.load()==UINTPTR_MAX);});
 t.join();m.renderer_enabled=false;
 std::thread foreign([&]{GuestMemory::RenderCursor cursor(m);std::uint64_t v;(void)m.load(0x10000,4);assert(!cursor.load(0x10000,4,v));m.release_reader_slot();});foreign.join();
''', switch)

    def test_render_cursor_yields_to_raw_writer_and_claim(self):
        # The cursor holds its page while the renderer keeps reading one word. The owner then takes an exclusive RawPermit
        # (round 0) or a GPU claim of that page (round 1): each is granted while the renderer is still in its read loop (no
        # deadlock), no read is served by the cursor while the owner holds the page, the renderer makes no progress while
        # the raw writer holds the gate, and it reads the word the owner wrote under its ownership (independent value).
        self.cursor_case('cursor-owners', self.CURSOR_SETUP + r'''
 auto pin=m.regions[0].bytes;
 for(unsigned round=0;round<2;++round){
  std::atomic<bool> pinned{false},holding{false};std::atomic<unsigned> served_while_held{0},reads{0};
  const std::uint32_t fresh=0xc0de0000u+round;
  auto t=renderer([&]{{GuestMemory::RenderCursor cursor(m);std::uint64_t v=0;
    const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(30);
    do{assert(std::chrono::steady_clock::now()<until);const auto before=GuestMemory::cursor_reads;
     if(!cursor.load(0x10400,4,v))v=m.load(0x10400,4);
     if(GuestMemory::cursor_reads!=before){if(holding.load())++served_while_held;pinned=true;}++reads;
    }while(v!=fresh);}});
  while(!pinned.load())std::this_thread::yield();
  if(round==0){GuestMemory::RawPermit raw(m);holding=true;const auto r=reads.load();
   std::this_thread::sleep_for(std::chrono::milliseconds(30));assert(reads.load()<=r+1);
   auto* p=m.access(0x10400,4,true);p[0]=std::uint8_t(fresh>>24);p[1]=std::uint8_t(fresh>>16);p[2]=std::uint8_t(fresh>>8);p[3]=std::uint8_t(fresh);holding=false;}
  else{GuestMemory::GpuPages claim(m,{{pin->data()+0x400,4,pin}});holding=true;
   std::this_thread::sleep_for(std::chrono::milliseconds(30));
   auto* p=claim.pointer(0);p[0]=std::uint8_t(fresh>>24);p[1]=std::uint8_t(fresh>>16);p[2]=std::uint8_t(fresh>>8);p[3]=std::uint8_t(fresh);holding=false;}
  t.join();assert(served_while_held.load()==0);
 }
''')

    def test_render_cursor_respects_new_watch(self):
        # A byte-exact watch is published on the pinned page by the owner while the renderer holds the cursor (watch()
        # takes no exclusive admission): the next read of the page is not served and the watched bytes reach the
        # renderer's observer path (renderer_access) exactly once; other pages are still served.
        self.cursor_case('cursor-watch', self.CURSOR_SETUP + r'''
 auto pin=m.regions[0].bytes;
 {GuestMemory::RawPermit raw(m);m.host_access_observer=[](const void*,std::uint64_t,bool){};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;}
 std::atomic<bool> pinned{false},watched{false};
 auto t=renderer([&]{GuestMemory::RenderCursor cursor(m);std::uint64_t v=0;
  (void)m.load(0x10200,4);(void)m.load(0x11000,4);assert(cursor.load(0x10200,4,v) && v==word(0x80));pinned=true;
  while(!watched.load())std::this_thread::yield();
  const auto before=GuestMemory::cursor_reads;
  assert(!cursor.load(0x10204,4,v));v=m.load(0x10204,4);assert(v==word(0x81) && host_calls==0);   // same page, other bytes
  assert(!cursor.load(0x10300,4,v));v=m.load(0x10300,4);assert(v==word(0xc0) && host_calls==1);   // the watched word
  assert(cursor.load(0x11000,4,v) && v==word(0x400) && GuestMemory::cursor_reads==before+1);});
 while(!pinned.load())std::this_thread::yield();
 m.host_access_observer.watch(pin->data()+0x300,4);watched=true;
 t.join();m.renderer_enabled=false;
''')

    def range_case(self, name, body, switch='1'):
        previous = os.environ.get('ANYPS3_GUEST_RANGE_ADMISSION')
        os.environ['ANYPS3_GUEST_RANGE_ADMISSION'] = switch
        try:
            self.native(name, body)
        finally:
            if previous is None:
                os.environ.pop('ANYPS3_GUEST_RANGE_ADMISSION', None)
            else:
                os.environ['ANYPS3_GUEST_RANGE_ADMISSION'] = previous

    # Two adjacent maps (0x10000 8 KiB, 0x12000 4 KiB), a read-only map at 0x20000; a renderer thread creates the Ranges
    # (the game's draw-input configuration); renderer_access counts observer deliveries.
    RANGE_SETUP = r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.map(0x12000,4096,6,nullptr,0);m.map(0x20000,4096,4,nullptr,0);
 m.enable_concurrency();m.enable_page_broker();auto pin=m.regions[0].bytes;
 std::atomic<unsigned> host_calls{0};m.renderer_access=[&](const void*,std::uint64_t,bool){++host_calls;};
 auto renderer=[&](auto body){std::atomic<bool> bound{false};
  std::thread t([&m,&bound,body]{m.renderer_thread=std::this_thread::get_id();bound=true;body();m.release_reader_slot();});
  while(!bound.load()){std::this_thread::yield();}return t;};
 m.renderer_enabled=true;
 auto warm=[&]{(void)m.load(0x10000,4);(void)m.load(0x11000,4);(void)m.load(0x12000,4);(void)m.load(0x20000,4);};
'''

    def test_range_creation_without_lease(self):
        # A Range inside one mapping is created on the compact admission (admitted_ranges counts it); its single part names
        # the mapping's backing at the offset computed here from the map layout. A brokered span over two host pages or
        # two mappings keeps the Lease path (same parts); unmapped and unwritable spans still fault; switch 0 creates
        # every Range on the Lease path.
        for switch in ('1', '0'):
            self.range_case('range-basic-' + switch, self.RANGE_SETUP + r'''
 const bool on=std::getenv("ANYPS3_GUEST_RANGE_ADMISSION")[0]=='1';
 auto t=renderer([&]{warm();const auto before=GuestMemory::admitted_ranges;
  auto r=m.range(0x10010,64,false);
  assert(r.parts.size()==1 && r.parts[0].offset==0 && r.parts[0].size==64 && r.parts[0].data==pin->data()+0x10 && r.parts[0].owner==pin);
  assert(r.parts[0].mapping_id==m.regions[0].identity && r.size==64 && !r.writable && r.address==0x10010 && r.epoch==m.parallel->epoch);
  auto w=m.range(0x10ff0,0x1010,true,false);   // translation-only over two host pages of one mapping
  assert(w.parts.size()==1 && w.parts[0].data==pin->data()+0xff0 && w.parts[0].size==0x1010 && w.writable);
  assert(GuestMemory::admitted_ranges==before+(on?2:0));
  auto b=m.range(0x10ff0,32,false);   // brokered over two host pages: Lease path
  assert(b.parts.size()==1 && b.parts[0].data==pin->data()+0xff0);
  auto two=m.range(0x11ff0,32,false,false);   // two mappings: Lease path, two parts
  assert(two.parts.size()==2 && two.parts[0].size==16 && two.parts[1].owner==m.regions[1].bytes && two.parts[1].offset==16);
  assert(GuestMemory::admitted_ranges==before+(on?2:0));
  fault([&]{(void)m.range(0x30000,16,false);});fault([&]{(void)m.range(0x20000,16,true);});
  auto ro=m.range(0x20000,16,false);assert(ro.parts.size()==1 && ro.parts[0].owner==m.regions[2].bytes);
  assert(GuestMemory::admitted_ranges==before+(on?3:0));});
 t.join();m.renderer_enabled=false;
''', switch)

    def test_range_creation_waits_for_claim_and_reaches_observer(self):
        # Creation of a brokered Range on a page the owner has claimed returns only after the release; a creation over
        # watched bytes delivers them to the observer path (renderer_access) once, while unwatched bytes of the same
        # page are created without a delivery.
        self.range_case('range-owners', self.RANGE_SETUP + r'''
 std::atomic<bool> warmed{false},go{false},released{false};std::atomic<int> after{-1};
 auto t=renderer([&]{warm();(void)m.range(0x10400,16,false);warmed=true;while(!go.load())std::this_thread::yield();
  const auto before=GuestMemory::admitted_ranges;auto r=m.range(0x10400,16,false);after=released.load()?1:0;
  assert(r.parts.size()==1 && r.parts[0].data==pin->data()+0x400 && GuestMemory::admitted_ranges==before);});
 while(!warmed.load())std::this_thread::yield();
 {GuestMemory::GpuPages claim(m,{{pin->data()+0x400,16,pin}});go=true;std::this_thread::sleep_for(std::chrono::milliseconds(30));released=true;}
 t.join();assert(after.load()==1);
 {GuestMemory::RawPermit raw(m);m.host_access_observer=[](const void*,std::uint64_t,bool){};
  m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(pin->data()+0x500,4);}
 auto u=renderer([&]{warm();const auto before=GuestMemory::admitted_ranges;
  {HostByteAccess exact(true);auto r=m.range(0x10600,64,false);assert(host_calls==0 && GuestMemory::admitted_ranges==before+1);
   auto v=m.range(0x104f0,32,false);assert(host_calls==1 && GuestMemory::admitted_ranges==before+1 && v.parts[0].data==pin->data()+0x4f0);}});
 u.join();m.renderer_enabled=false;
''')
