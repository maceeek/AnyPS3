"""GuestMemory::atomic_read / atomic_write (the bulk copy under transfer(), SPU DMA, PPU medium accesses and RSX reads) copy exactly
n bytes for every source/destination misalignment 0..15 and every size 0..300 (head bytes, unrolled 32-byte word body, 8-byte words,
tail bytes), leave every byte outside the destination span untouched, and agree with a plain byte-by-byte copy. The expectation is
a byte loop over a pattern, independent of the word/unroll structure; GuestMemory::transfer is also checked across a page split.
HIRES7: the same cases with the x86 AVX 16-byte body, overlapping copies (one-word-at-a-time interleaving kept), the LineLocks stripe
set against a line-by-line enumeration, and the concurrent transfer write path (versions, reservations, faults before any change)."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_par1 import source

BODY = r'''
 std::vector<std::uint8_t> src(400),dst(400),want(400);
 unsigned checks=0;
 for(unsigned so=0;so<16;++so)for(unsigned dof=0;dof<16;++dof)for(unsigned n=0;n<=300;++n){
  for(unsigned i=0;i<400;++i){src[i]=std::uint8_t(i*37+so*11+n);dst[i]=std::uint8_t(0xa5^i);}
  want=dst;for(unsigned i=0;i<n;++i)want[dof+i]=src[so+i];
  GuestMemory::atomic_read(src.data()+so,dst.data()+dof,n);assert(dst==want);
  for(unsigned i=0;i<400;++i){dst[i]=std::uint8_t(0x5a^(i*3));}
  want=dst;for(unsigned i=0;i<n;++i)want[dof+i]=src[so+i];
  GuestMemory::atomic_write(dst.data()+dof,src.data()+so,n);assert(dst==want);
  ++checks;
 }
 // Through the concurrent transfer path: a span that crosses a page boundary and a one-page span, read and write.
 GuestMemory m;m.map(0x10000,3*4096,6,nullptr,0);m.enable_concurrency();
 std::vector<std::uint8_t> in(3*4096),out(3*4096);for(unsigned i=0;i<3*4096;++i)in[i]=std::uint8_t(i*13+7);
 for(const auto& [at,n]:std::vector<std::pair<std::uint64_t,unsigned>>{{0x10ff3,4100},{0x11001,1000},{0x10000,3*4096-1},{0x12ffd,3}}){
  m.transfer(at,in.data(),n,true);std::fill(out.begin(),out.end(),0);m.transfer(at,out.data(),n,false);
  for(unsigned i=0;i<n;++i){assert(out[i]==in[i]);}
  for(unsigned i=n;i<3*4096;++i){assert(out[i]==0);}
  for(unsigned i=0;i<n;++i){assert(m.load(at+i,1)==in[i]);}
 }
 // Overlapping source/destination (one buffer): every source word is read just before its own store, so a source 8 bytes below
 // the destination (write) or a destination 8 bytes above the source (read) replicates the first 8 bytes with period 8. A copy in
 // wider steps would read source bytes before they are overwritten and give a different pattern.
 for(unsigned n:{32u,40u,64u,200u}){
  alignas(16) std::uint8_t b[512];for(unsigned i=0;i<512;++i)b[i]=std::uint8_t(i*7+1);
  GuestMemory::atomic_write(b+64,b+56,n);for(unsigned i=0;i<n;++i){assert(b[64+i]==std::uint8_t((56+i%8)*7+1));}
  for(unsigned i=0;i<512;++i)b[i]=std::uint8_t(i*5+3);
  GuestMemory::atomic_read(b+64,b+72,n);for(unsigned i=0;i<n;++i){assert(b[72+i]==std::uint8_t((64+i%8)*5+3));}
 }
 std::printf("PASS atomic copies equal byte copies (%u cases)\n",checks);
'''

# LineLocks: the stripe set (host line % 4096) of the spans, independently enumerated line by line; held only while alive.
LOCKS = r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.enable_concurrency();
 auto& lines=m.parallel->lines;
 auto held=[&](unsigned i){const bool was=lines[i].held.test_and_set(std::memory_order_acquire);if(!was)lines[i].held.clear(std::memory_order_release);return was;};
 using Span=GuestMemory::HostSpan;
 auto at=[](std::uintptr_t line,unsigned offset=0){return reinterpret_cast<std::uint8_t*>(line*128+offset);};
 const std::vector<std::vector<Span>> cases={
  {{at(4090*2+4090),10*128,nullptr}},                          // stripe 4090 + table wrap
  {{at(77,127),2,nullptr}},                                     // two lines from one byte pair
  {{at(5),0,nullptr}},                                          // empty span: nothing
  {{at(300,5),4096*128,nullptr}},                               // 4097 lines: every stripe
  {{at(8192),4096*128,nullptr}},                                // exactly 4096 lines: every stripe
  {{at(1000),3*128,nullptr},{at(1000+4096,64),200,nullptr},{at(64),128,nullptr},{at(4095),129,nullptr}},
  {{at(12345),16384,nullptr}},                                  // 16 KiB DMA PUT
 };
 for(const auto& spans:cases){
  std::set<unsigned> want;
  for(const auto& span:spans)if(span.size)for(auto line=reinterpret_cast<std::uintptr_t>(span.pointer)/128;line<=(reinterpret_cast<std::uintptr_t>(span.pointer)+span.size-1)/128;++line)want.insert(unsigned(line%4096));
  {GuestMemory::LineLocks locks(m,spans);
   for(unsigned i=0;i<4096;++i){assert(held(i)==(want.count(i)!=0));}}
  for(unsigned i=0;i<4096;++i){assert(!held(i));}
 }
 std::printf("PASS line locks\n");
'''

# Concurrent transfer write: span resolution across mappings, versions, reservations, faults before any change.
WRITE = r'''
 GuestMemory m;m.map(0x10000,4096,6,nullptr,0);m.map(0x11000,4096,6,nullptr,0);m.map(0x12000,4096,4,nullptr,0);m.map(0x14000,4096,6,nullptr,0);m.enable_concurrency();
 auto versions=[&](std::uint64_t base){for(const auto& map:m.parallel->snapshot->maps)if(map.region.base==base)return map.state->versions;assert(false);return std::vector<std::uint64_t>{};};
 std::vector<std::uint8_t> in(512);for(unsigned i=0;i<512;++i)in[i]=std::uint8_t(i*29+5);
 // HIRES9 (ANYPS3_GUEST_WRITE_TRACKED_ONLY, default 1): a part whose lines no reservation ever tracked keeps its versions and touch
 // counters; a part with a tracked line bumps all of its lines and touches them; 0 bumps every part (the HIRES7 expectations).
 const bool all=std::getenv("ANYPS3_GUEST_WRITE_TRACKED_ONLY") && !std::strcmp(std::getenv("ANYPS3_GUEST_WRITE_TRACKED_ONLY"),"0");
 auto touched=[&](std::uint64_t address){std::uintptr_t host=0;
  for(const auto& map:m.parallel->snapshot->maps)if(address>=map.region.base && address-map.region.base<map.region.bytes->size())host=reinterpret_cast<std::uintptr_t>(map.region.bytes->data()+(address-map.region.base));
  assert(host);return m.parallel->notifications->touched[(host/128)%4096].value.load();};
 std::pair<std::uintptr_t,std::uint64_t> token;m.load_reserved(0x11000,4,token);
 const auto touched_a31=touched(0x10f80),touched_b0=touched(0x11000);
 m.transfer(0x10f80,in.data(),256,true);                        // last line of mapping 1 (untracked) + first line of mapping 2 (tracked)
 for(unsigned i=0;i<256;++i){assert(m.load(0x10f80+i,1)==in[i]);}
 {const auto a=versions(0x10000),b=versions(0x11000);
  for(unsigned i=0;i<32;++i){assert(a[i]==(all && i==31?1u:0u));assert(b[i]==(i==0?1u:0u));}}
 assert(touched(0x10f80)==touched_a31+(all?1:0) && touched(0x11000)==touched_b0+1);
 assert(!m.store_conditional(0x11000,4,0x12345678,token));      // the PUT invalidated the reservation of that line
 m.transfer(0x10100,in.data(),300,true);                        // one mapping: bytes 0x100..0x22b = lines 2..4 of mapping 1, untracked
 {const auto a=versions(0x10000);for(unsigned i=0;i<32;++i){assert(a[i]==(all && ((i>=2 && i<=4) || i==31)?1u:0u));}}
 std::pair<std::uintptr_t,std::uint64_t> token3;m.load_reserved(0x10180,4,token3);   // tracks line 3 only
 m.transfer(0x10100,in.data(),300,true);                        // lines 2..4 again: the part has a tracked line, all three bump
 {const auto a=versions(0x10000);for(unsigned i=0;i<32;++i){assert(a[i]==((i>=2 && i<=4)?(all?2u:1u):all && i==31?1u:0u));}}
 assert(!m.store_conditional(0x10180,4,0x12345678,token3));
 // A span that continues into the read-only mapping faults before any byte or version changes.
 std::vector<std::uint8_t> before(256);m.transfer(0x11f80,before.data(),128,false);
 bool hit=false;try{m.transfer(0x11f80,in.data(),256,true);}catch(const std::runtime_error& e){hit=std::string(e.what())=="guest memory permission fault";}
 assert(hit);
 {std::vector<std::uint8_t> after(128);m.transfer(0x11f80,after.data(),128,false);assert(std::equal(after.begin(),after.end(),before.begin()));
  const auto b=versions(0x11000);assert(b[31]==0);}
 // A write that continues into the hole after mapping 4, and a read that continues into the hole after mapping 3, fault before any
 // change (bytes, versions, destination buffer).
 std::pair<std::uintptr_t,std::uint64_t> token4;m.load_reserved(0x14f80,4,token4);   // track line 31 of mapping 4: its bump is visible
 m.transfer(0x14ff0,in.data(),16,true);
 hit=false;try{m.transfer(0x14ff0,in.data()+100,32,true);}catch(const std::runtime_error&){hit=true;}
 assert(hit);
 for(unsigned i=0;i<16;++i){assert(m.load(0x14ff0+i,1)==in[i]);}
 {const auto d=versions(0x14000);for(unsigned i=0;i<32;++i){assert(d[i]==(i==31?1u:0u));}}
 std::vector<std::uint8_t> sink(256,0xee);hit=false;try{m.transfer(0x12f80,sink.data(),256,false);}catch(const std::runtime_error&){hit=true;}
 assert(hit);for(auto x:sink){assert(x==0xee);}
 std::printf("PASS transfer write\n");
'''


def _avx():
    try:
        return ' avx ' in Path('/proc/cpuinfo').read_text().replace('\n', ' ')
    except OSError:
        return False


# -O0/-O2 without -m flags: the 8-byte body. x86-64-v3 (AVX): the 16-byte VMOVDQA body (HIRES7) on a host that can run it.
FLAGS = [('-O0',), ('-O2',)] + ([('-O2', '-march=x86-64-v3')] if _avx() else [])


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class AtomicCopyUnrolledTests(unittest.TestCase):
    def run_body(self, body, marker, flags=FLAGS, env=()):
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / 't.cpp'
            cpp.write_text(source(body).replace('#include <cassert>', '#include <cassert>\n#include <vector>\n#include <utility>\n#include <set>', 1))
            for opt in flags:
                p = subprocess.run(['c++', '-std=c++17', *opt, '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))],
                                   capture_output=True, text=True, timeout=600)
                self.assertEqual(p.returncode, 0, p.stderr[-8000:])
                p = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, text=True, timeout=300,
                                   env={**os.environ, 'SDL_AUDIODRIVER': 'dummy', 'ANYPS3_AUDIO_SINK': 'null', **dict(env)})
                self.assertEqual(p.returncode, 0, ' '.join(opt) + ': ' + p.stdout + p.stderr)
                self.assertIn(marker, p.stdout)

    def test_all_alignments_and_sizes(self):
        self.run_body(BODY, 'PASS atomic copies')

    def test_line_locks_hold_exactly_the_stripes_of_the_spans(self):
        self.run_body(LOCKS, 'PASS line locks', FLAGS[1:])

    def test_concurrent_transfer_write_spans_versions_and_faults(self):
        self.run_body(WRITE, 'PASS transfer write', FLAGS[1:])
        self.run_body(WRITE, 'PASS transfer write', FLAGS[2:] or FLAGS[1:], env={'ANYPS3_GUEST_WRITE_TRACKED_ONLY': '0'})


if __name__ == '__main__':
    unittest.main()
