"""PAR8 physical reservation index and atomic page admission contracts."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_par1 import source

ARTIFACTS = Path('local/ratchet/codex/par8/tests')
ALLOCATIONS = r'''
inline thread_local bool count_allocations=false;
inline thread_local unsigned allocation_count=0;
[[gnu::noinline]] void* operator new(std::size_t n){
 if(count_allocations)++allocation_count;
 if(auto* p=std::malloc(n?n:1))return p;
 throw std::bad_alloc();
}
[[gnu::noinline]] void operator delete(void* p)noexcept{std::free(p);}
[[gnu::noinline]] void operator delete(void* p,std::size_t)noexcept{std::free(p);}
'''

@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Parallel8Contracts(unittest.TestCase):
    def native(self, name, body, allocations=False):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        code = source(body)
        if allocations:
            code = code.replace('int main(){(void)crc32;', ALLOCATIONS+'\nint main(){(void)crc32;')
        cpp = ARTIFACTS/(name+'.cpp')
        cpp.write_text(code)
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))], capture_output=True, text=True, timeout=240)
        cpp.with_suffix('.build.log').write_text(build.stdout+build.stderr)
        self.assertEqual(build.returncode, 0, build.stderr[-10000:])
        run = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, text=True, timeout=45, env={**os.environ, 'ANYPS3_RSX_WORKER':'0', 'ANYPS3_SPU_THREADS':'0'})
        cpp.with_suffix('.run.log').write_text(run.stdout+run.stderr)
        self.assertEqual(run.returncode, 0, run.stderr[-10000:])

    def test_serial_radix_alias_aba_cross_line_overflow_and_copy(self):
        self.native('serial-index', r'''
 GuestMemory m;m.map(0x10000,131072,6,nullptr,0);m.regions.push_back({0x40000,6,m.regions[0].bytes});m.mapping_changed();
 auto* base=m.regions[0].bytes->data();unsigned calls=0;
 m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(base+65536,4);
 // Reserve many distinct physical leaves. Stores may not hash or allocate.
 for(unsigned n=0;n<1024;++n)(void)m.reservation_token(0x10000+n*128,4);
 auto a=m.reservation_token(0x10000,4),b=m.reservation_token(0x10080,4),c=m.reservation_token(0x1ff80,4),d=m.reservation_token(0x20000,4);
 m.store(0x4007f,2,0xa1b2);assert(!m.store_conditional(0x10000,4,0,a) && !m.store_conditional(0x10080,4,0,b));
 calls=0;m.store(0x4ffff,2,0xc3d4);assert(calls==1);assert(!m.store_conditional(0x1ff80,4,0,c) && !m.store_conditional(0x20000,4,0,d));
 auto token=m.reservation_token(0x10100,4);m.store(0x40100,4,7);m.store(0x10100,4,0);assert(!m.store_conditional(0x10100,4,1,token));
 assert(base[127]==0xa1 && base[128]==0xb2 && base[65535]==0xc3 && base[65536]==0xd4);
 GuestMemory copy=m;auto original=m.reservation_token(0x10200,4);auto copied=copy.reservation_token(0x10200,4);
 copy.store(0x10200,4,0);assert(m.reservation_token(0x10200,4)==original && copy.reservation_token(0x10200,4)!=copied);
 auto line=reinterpret_cast<std::uintptr_t>(base+512)/128;m.write_versions[line]=UINT64_MAX;fault([&]{m.store(0x10200,4,0xff);});assert(base[512]==0);
 // Existing draw differential oracles export/import ordinary unordered maps.
 std::unordered_map<std::uintptr_t,std::uint64_t> exported=m.write_versions;exported[line]=6;m.write_versions=exported;
 assert(m.write_versions.at(line)==6);m.store(0x10200,4,0);assert(m.write_versions.at(line)==7 && exported.at(line)==6);
 m.write_versions=decltype(m.write_versions){};assert(m.write_versions.empty());m.store(0x10200,4,0);assert(m.write_versions.empty());
 // A negative leaf cache must be invalidated by a newly registered line.
 GuestMemory n;n.map(0x80000,262144,6,nullptr,0);(void)n.reservation_token(0x80000,4);n.store(0xb0000,4,0);auto fresh=n.reservation_token(0xb0000,4);n.store(0xb0000,4,0);assert(!n.store_conditional(0xb0000,4,1,fresh));
''')

    def test_active_observer_broker_and_claim_hot_access_allocates_nothing(self):
        self.native('no-allocation', r'''
 GuestMemory m;m.map(0x10000,16384,6,nullptr,0);auto pin=m.regions[0].bytes;
 unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;};m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;m.host_access_observer.watch(pin->data()+8192,4);
 (void)m.reservation_token(0x10000,4);m.store(0x10000,4,0);(void)m.load(0x10000,4);count_allocations=true;
 for(unsigned i=0;i<10000;++i){m.store(0x10000,4,i);assert(m.load(0x10000,4)==i);}count_allocations=false;assert(allocation_count==0);
 m.enable_concurrency();m.enable_page_broker();auto claim=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{pin->data()+8192,4,pin}});
 // Full Lease on both threads, as with RSX_WORKER, rather than owner shortcut.
 m.renderer_enabled=true;m.store(0x10000,4,0);std::atomic<bool> warm{false},go{false},done{false};
 std::thread foreign([&]{m.store(0x11000,4,0);(void)m.load(0x11000,4);warm=true;while(!go.load())std::this_thread::yield();
  allocation_count=0;count_allocations=true;for(unsigned i=0;i<10000;++i){m.store(0x11000,4,i);assert(m.load(0x11000,4)==i);}count_allocations=false;assert(allocation_count==0);done=true;m.release_reader_slot();});
 while(!warm.load())std::this_thread::yield();
 {std::lock_guard<std::mutex> block(m.page_broker.mutex);go=true;allocation_count=0;count_allocations=true;
  for(unsigned i=0;i<10000;++i){m.store(0x10000,4,i);assert(m.load(0x10000,4)==i);}count_allocations=false;assert(allocation_count==0);
  const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);while(!done.load() && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();assert(done);}
 foreign.join();m.renderer_enabled=false;claim.reset();assert(calls==0 && m.load(0x10000,4)==9999 && m.load(0x11000,4)==9999);
 std::puts("PASS no allocations or broker mutex on 40000 active-observer/broker accesses");
''', allocations=True)

    def test_claim_pending_blocks_new_cpu_and_drains_old_reader(self):
        self.native('claim-drain', r'''
 GuestMemory m;m.map(0x10000,8192,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();auto pin=m.regions[0].bytes;
 auto range=m.range(0x10000,4,true);
 std::atomic<bool> admitted{false},release{false},late_done{false},stop_late{false};
 std::thread reader([&]{{GuestMemory::Lease lease(m,0x10000,4,false);admitted=true;while(!release.load())std::this_thread::yield();assert(m.load(0x10000,4)==0);}m.release_reader_slot();});
 while(!admitted.load())std::this_thread::yield();
 auto* cell=m.page_broker.cell(reinterpret_cast<std::uintptr_t>(pin->data())>>12);
 std::thread late([&]{while(!(cell->state.load()&GuestMemory::PageBroker::claimed))std::this_thread::yield();
  // A new CPU transaction cannot join after the exclusive bit is set.
  GuestMemory::CpuPages attempt(m.page_broker,std::vector<GuestMemory::HostSpan>{{pin->data(),4,pin}});assert(!attempt.admitted);release=true;
  while(!stop_late.load()){std::this_thread::yield();}assert(m.load(0x10000,4)==0x11223344);late_done=true;m.release_reader_slot();});
 GuestMemory::GpuPages claim(m,{{pin->data(),4,pin}});reader.join();assert(cell->state.load()==GuestMemory::PageBroker::claimed);
 const std::uint8_t value[]={0x11,0x22,0x33,0x44};claim.commit(range,0,value,4);claim.reset();stop_late=true;late.join();assert(late_done);
''')

    def test_partial_page_set_rolls_back_and_large_split_batches(self):
        self.native('multi-page', r'''
 GuestMemory m;for(unsigned i=0;i<12;++i)m.map(0x10000+i*4096,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
 auto pin=m.regions[5].bytes;GuestMemory::GpuPages claim(m,{{pin->data(),4,pin}});
 auto spans=m.parallel_spans(0x10000,12*4096,false);
 {GuestMemory::CpuPages attempt(m.page_broker,spans);assert(!attempt.admitted);}
 for(unsigned i=0;i<12;++i){auto* cell=m.page_broker.cell(reinterpret_cast<std::uintptr_t>(m.regions[i].bytes->data())>>12);assert((cell->state.load()&~GuestMemory::PageBroker::claimed)==0);}
 claim.reset();std::vector<std::uint8_t> input(12*4096,0xa5),output(input.size());m.transfer(0x10000,input.data(),input.size(),true);m.transfer(0x10000,output.data(),output.size(),false);assert(input==output);
 {GuestMemory::Lease batch(m,0x10000,12*4096,false,true);assert(m.load(0x1bffc,4)==0xa5a5a5a5);}
 auto invalid=spans;invalid.push_back({pin->data()+4095,2,pin});fault([&]{GuestMemory::CpuPages attempt(m.page_broker,invalid);});
 for(auto& region:m.regions)assert(m.page_broker.cell(reinterpret_cast<std::uintptr_t>(region.bytes->data())>>12)->state.load()==0);
''')

    def test_pending_claim_poison_retires_token_without_reader_leak(self):
        self.native('poison-drain', r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();auto pin=m.regions[0].bytes;
 std::atomic<bool> ready{false},leave{false};std::thread reader([&]{{GuestMemory::Lease lease(m,0x10000,4,false);ready=true;while(!leave.load())std::this_thread::yield();}m.release_reader_slot();});
 while(!ready.load()){std::this_thread::yield();}auto* entry=m.page_broker.cell(reinterpret_cast<std::uintptr_t>(pin->data())>>12);
 std::thread poison([&]{while(!(entry->state.load()&GuestMemory::PageBroker::claimed))std::this_thread::yield();m.page_broker.stop(std::make_exception_ptr(std::runtime_error("PAR8 fixed poison")));});
 bool same=false;try{GuestMemory::GpuPages claim(m,{{pin->data(),4,pin}});}catch(const std::runtime_error& e){same=std::string(e.what())=="PAR8 fixed poison";}
 poison.join();assert(same && m.page_broker.owners==0 && !(entry->state.load()&GuestMemory::PageBroker::claimed));leave=true;reader.join();assert(entry->state.load()==0);fault([&]{m.load(0x10000,4);});
''')

    def test_map_publication_and_broker_lifetime_no_stale_leaf(self):
        self.native('radix-lifetime', r'''
 for(unsigned run=0;run<4;++run){GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();m.enable_page_broker();
  std::atomic<bool> quit{false},ready{false};std::thread reader([&]{m.store(0x10000,4,0xaabbccdd);ready=true;while(!quit.load()){assert(m.load(0x10000,4)==0xaabbccdd);}m.release_reader_slot();});
  while(!ready.load()){std::this_thread::yield();}for(unsigned i=0;i<20;++i)m.map(0x20000+i*4096,4096,6,nullptr,0);
  quit=true;reader.join();for(unsigned i=0;i<20;++i){m.store(0x20000+i*4096,4,0x11223344);assert(m.load(0x20000+i*4096,4)==0x11223344);}
 }
''')
