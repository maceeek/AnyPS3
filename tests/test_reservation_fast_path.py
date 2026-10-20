"""Concurrent-memory reservations: the single-mapping fast path and the clipped-granule general path.

GuestMemory::load_reserved/store_conditional (PPU lwarx/stwcx.) and getllar/putllc (SPU) take the fast path when
the 128-byte granule lies inside one mapping, otherwise the general path. Expectations are architectural: a
store to the reserved granule before the conditional store makes it fail without storing, a store to another
granule does not, a successful conditional store consumes the token, and concurrent increments through both
reservation forms lose no update (the final count is the number of increments).
"""
import shutil
import unittest

from tests import spu_native

SCENARIOS = {}

SCENARIOS['rsv-paths'] = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,0x10000,6,nullptr,0);m.map(0x60000,0xc38,6,nullptr,0);m.map(0x70040,0x1000,6,nullptr,0);
 m.enable_concurrency();
 auto fault=[](const std::function<void()>& f){bool failed=false;try{f();}catch(const std::runtime_error&){failed=true;}assert(failed);};
 std::pair<std::uintptr_t,std::uint64_t> k;
 // A. lwarx/stwcx. inside one mapping (fast path).
 m.store(0x10100,4,0x01020304);assert(m.load_reserved(0x10100,4,k)==0x01020304);
 assert(m.store_conditional(0x10100,4,0xa1b2c3d4,k) && m.load(0x10100,4)==0xa1b2c3d4);
 assert(!m.store_conditional(0x10100,4,5,k) && m.load(0x10100,4)==0xa1b2c3d4);          // token consumed
 m.load_reserved(0x10100,4,k);m.store(0x1017c,4,7);
 assert(!m.store_conditional(0x10100,4,0x55,k) && m.load(0x10100,4)==0xa1b2c3d4);       // same granule: lost
 m.load_reserved(0x10100,4,k);m.store(0x10180,4,9);
 assert(m.store_conditional(0x10100,4,0x66,k) && m.load(0x10100,4)==0x66);              // next granule: kept
 m.load_reserved(0x10108,8,k);assert(m.store_conditional(0x10108,8,UINT64_C(0x1122334455667788),k));
 {std::uint8_t b[8];m.transfer(0x10108,b,8,false);assert(b[0]==0x11 && b[3]==0x44 && b[7]==0x88);} // big-endian bytes
 assert(m.reservation_token(0x10104,4)==m.reservation_token(0x10100));                    // word and line forms agree
 // B. getllar/putllc (fast path): data, stale token, foreign store, unconditional form.
 std::uint8_t in[128],line[128],back[128];for(unsigned i=0;i<128;++i)in[i]=std::uint8_t(i*3+1);
 m.getllar(0x10200,line,k);assert(m.putllc(0x10200,in,k,false));
 m.transfer(0x10200,back,128,false);assert(!std::memcmp(back,in,128));
 assert(!m.putllc(0x10200,line,k,false));m.transfer(0x10200,back,128,false);assert(!std::memcmp(back,in,128));
 m.getllar(0x10200,line,k);assert(!std::memcmp(line,in,128));m.store(0x1027f,1,0);assert(!m.putllc(0x10200,in,k,false));
 assert(m.putllc(0x10200,line,{},true));m.transfer(0x10200,back,128,false);assert(!std::memcmp(back,line,128));
 // C. Granule clipped by the end of its mapping (general path), as in test_memory's firmware case.
 m.store(0x60c2c,4,0x01020304);assert(m.load_reserved(0x60c2c,4,k)==0x01020304);
 assert(m.store_conditional(0x60c2c,4,0xa1b2c3d4,k) && m.load(0x60c2c,4)==0xa1b2c3d4);
 m.load_reserved(0x60c2c,4,k);m.store(0x60c34,4,7);assert(!m.store_conditional(0x60c2c,4,0x55,k));
 m.load_reserved(0x60c2c,4,k);m.store(0x60b00,4,7);assert(m.store_conditional(0x60c2c,4,0x56,k) && m.load(0x60c2c,4)==0x56);
 fault([&]{m.reservation_token(0x60c00);});
 // D. Mapping base inside a granule: guest granule 0x70100 is inside the mapping but straddles two host lines.
 m.load_reserved(0x70100,4,k);m.store(0x70178,4,1);assert(!m.store_conditional(0x70100,4,2,k));   // second half
 m.load_reserved(0x70104,4,k);m.store(0x70100,1,1);assert(!m.store_conditional(0x70104,4,2,k));   // first half
 m.load_reserved(0x70100,4,k);m.store(0x70400,4,1);assert(m.store_conditional(0x70100,4,3,k) && m.load(0x70100,4)==3);
 m.getllar(0x70100,line,k);m.store(0x7017f,1,9);assert(!m.putllc(0x70100,in,k,false));
 m.getllar(0x70100,line,k);assert(m.putllc(0x70100,in,k,false));m.transfer(0x70100,back,128,false);assert(!std::memcmp(back,in,128));
 // The mapping's first, clipped granule (0x70000-0x7007f starts before the map) uses the general path.
 m.load_reserved(0x70040,4,k);assert(m.store_conditional(0x70040,4,0x77,k) && m.load(0x70040,4)==0x77);
 // E. Permissions: a read-only mapping faults on the conditional store, the reservation itself works.
 m.map(0x90000,0x1000,4,nullptr,0);m.load_reserved(0x90100,4,k);fault([&]{m.store_conditional(0x90100,4,1,k);});
 fault([&]{m.putllc(0x90100,in,{},true);});
 std::puts("RSV_PATHS");
'''

SCENARIOS['rsv-stress'] = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,0x10000,6,nullptr,0);m.map(0x60000,0xc38,6,nullptr,0);m.enable_concurrency();
 constexpr unsigned per=20000;
 auto ppu=[&](unsigned address){for(unsigned i=0;i<per;++i)for(;;){std::pair<std::uintptr_t,std::uint64_t> t;
   const auto v=m.load_reserved(address,4,t);if(m.store_conditional(address,4,v+1,t))break;}};
 auto spu=[&](unsigned address){std::uint8_t l[128];const unsigned o=address&127;for(unsigned i=0;i<per;++i)for(;;){std::pair<std::uintptr_t,std::uint64_t> t;
   m.getllar(address&~127u,l,t);std::uint32_t v=(std::uint32_t(l[o])<<24)|(l[o+1]<<16)|(l[o+2]<<8)|l[o+3];++v;
   l[o]=std::uint8_t(v>>24);l[o+1]=std::uint8_t(v>>16);l[o+2]=std::uint8_t(v>>8);l[o+3]=std::uint8_t(v);if(m.putllc(address&~127u,l,t,false))break;}};
 // Fast path: two lwarx/stwcx. and two getllar/putllc agents increment one word; a neighbour word in the same
 // granule is incremented by a fifth agent (its stores only cost retries, never updates).
 m.store(0x10500,4,0);m.store(0x10540,4,0);
 {std::vector<std::thread> threads;for(unsigned i=0;i<2;++i){threads.emplace_back(ppu,0x10500);threads.emplace_back(spu,0x10500);}
  threads.emplace_back(ppu,0x10540);for(auto& t:threads)t.join();}
 assert(m.load(0x10500,4)==4*per && m.load(0x10540,4)==per);
 // General path (granule clipped by the mapping end): two lwarx/stwcx. agents.
 m.store(0x60c2c,4,0);
 {std::thread a(ppu,0x60c2c),b(ppu,0x60c2c);a.join();b.join();}
 assert(m.load(0x60c2c,4)==2*per);
 std::puts("RSV_STRESS");
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class ReservationFastPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory, cls.exe = spu_native.build(SCENARIOS, name='reservation-fast-path')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def scenario(self, name, marker):
        code, out, err = spu_native.run(self.directory, self.exe, name)
        self.assertEqual(code, 0, f'{name}: {err[-6000:]}')
        self.assertIn(marker, out)

    def test_fast_and_general_paths_match_architecture(self):
        self.scenario('rsv-paths', 'RSV_PATHS')

    def test_concurrent_increments_lose_no_update(self):
        self.scenario('rsv-stress', 'RSV_STRESS')


if __name__ == '__main__':
    unittest.main()
