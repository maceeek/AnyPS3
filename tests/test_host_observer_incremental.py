"""HostObserver::watch keeps its page bitmaps, counts, page summary and watched flag equal to a brute-force union of the live
per-page watch keys (random add/remove of short and page-crossing spans, polling and synchronous keys, duplicate keys,
removal of absent keys). The expectation is recomputed from the model's key multiset, not from the observer."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_par1 import source

BODY = r'''
 GuestMemory m;m.map(0x10000,4*4096,6,nullptr,0);auto pin=m.regions[0].bytes;
 m.host_access_observer=[](const void*,std::uint64_t,bool){};m.host_access_observer.filtered=true;m.host_access_observer.supports_exact_bytes=true;
 auto& o=m.host_access_observer;std::map<std::uintptr_t,bool> summary;o.page_summary=[&](std::uintptr_t page,bool w){summary[page]=w;};
 const auto base=reinterpret_cast<std::uintptr_t>(pin->data());
 const auto first_page=base>>12,last_page=(base+4*4096-1)>>12;
 // Model: multiset of per-page keys (page, begin, end, polling) -> count. A call touches every page of its span; removing a key
 // that is not present on a page changes nothing on that page.
 std::map<std::tuple<std::uintptr_t,unsigned,unsigned,bool>,unsigned> keys;
 auto apply=[&](std::uintptr_t at,std::uint64_t n,bool add,bool poll){
  for(auto page=at>>12;page<=((at+n-1)>>12);++page){
   const std::tuple<std::uintptr_t,unsigned,unsigned,bool> k{page,unsigned(std::max(at,page<<12)-(page<<12)),unsigned(std::min<std::uintptr_t>(at+n,(page+1)<<12)-(page<<12)),poll};
   if(add)++keys[k];else{auto it=keys.find(k);if(it!=keys.end() && !--it->second)keys.erase(it);}}
  o.watch(reinterpret_cast<const void*>(at),n,add,poll);
 };
 unsigned checks=0;
 auto check=[&]{
  ++checks;
  for(std::uintptr_t page=first_page;page<=last_page;++page){
   std::array<std::uint64_t,64> all{},sync{};unsigned total=0;
   for(const auto& [k,count]:keys){if(std::get<0>(k)!=page)continue;total+=count;
    for(unsigned b=std::get<1>(k);b<std::get<2>(k);++b){all[b/64]|=UINT64_C(1)<<(b%64);if(!std::get<3>(k))sync[b/64]|=UINT64_C(1)<<(b%64);}}
   const auto* node=o.leaf(page);const auto* cell=node?node->visible_cells[page&511].load():nullptr;
   if(!cell){assert(!total);continue;}
   assert(cell->count.load()==total);
   for(unsigned w=0;w<64;++w)assert(cell->all[w].load()==all[w] && cell->sync[w].load()==sync[w]);
   assert(o.pages.count(page)==(total!=0));
   if(summary.count(page))assert(summary[page]==(total!=0));
   // Reader view: exact-byte queries agree with the brute-force bits at every byte.
   for(unsigned b=0;b<4096;b+=61){const void* p=reinterpret_cast<const void*>((page<<12)+b);
    assert(o.interested(p,1,true)==bool((all[b/64]>>(b%64))&1) && o.synchronous(p,1,true)==bool((sync[b/64]>>(b%64))&1));}
  }
  assert(o.watched.load()==!keys.empty());
 };
 struct W{std::uintptr_t at;std::uint64_t n;bool poll;};std::vector<W> live;std::uint32_t r=12345;
 auto rnd=[&](std::uint32_t k){r=r*1664525u+1013904223u;return (r>>8)%k;};
 for(unsigned step=0;step<8000;++step){
  const unsigned kind=rnd(100);
  if(live.empty() || kind<50){
   W w{base+rnd(4*4096-1),0,rnd(2)!=0};
   // Report-like 16-byte slots dominate (the game's case); also odd short spans, page-crossing spans and whole pages.
   const unsigned shape=rnd(10);
   if(shape<5){w.at&=~std::uintptr_t(15);w.n=16;}else if(shape<8)w.n=1+rnd(200);else w.n=1+rnd(9000);
   if(w.at+w.n>base+4*4096)w.n=base+4*4096-w.at;
   if(!live.empty() && rnd(4)==0)w=live[rnd(live.size())];   // duplicate key: count > 1
   apply(w.at,w.n,true,w.poll);live.push_back(w);
  }else if(kind<92){const auto i=rnd(live.size());const auto w=live[i];live.erase(live.begin()+i);apply(w.at,w.n,false,w.poll);}
  else{const auto at=base+rnd(4*4096-40);apply(at,1+rnd(40),false,rnd(2)!=0);}   // removal of a key that may be absent
  if(step%5==0 || step<300)check();
 }
 for(const auto& w:live)apply(w.at,w.n,false,w.poll);
 live.clear();check();assert(keys.empty());
 std::printf("PASS incremental host observer bitmaps equal the brute-force union (%u checks)\n",checks);
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class HostObserverIncrementalTests(unittest.TestCase):
    def test_random_watch_union(self):
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / 't.cpp'
            cpp.write_text(source(BODY).replace('#include <cassert>', '#include <cassert>\n#include <map>\n#include <tuple>', 1))
            p = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))],
                               capture_output=True, text=True, timeout=600)
            self.assertEqual(p.returncode, 0, p.stderr[-8000:])
            p = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, text=True, timeout=300,
                               env={**os.environ, 'SDL_AUDIODRIVER': 'dummy'})
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn('PASS incremental', p.stdout)


if __name__ == '__main__':
    unittest.main()
