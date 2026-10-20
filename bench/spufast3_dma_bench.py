#!/usr/bin/env python3
"""Micro-benchmark of GuestMemory::transfer (the SPU DMA data path) in concurrent mode: ns per call for GET (read) and PUT (write)
of several sizes, aligned within one page and straddling pages.  Diagnostic, not a game measurement.
Usage (tree root): python bench/spufast3_dma_bench.py [--iters 2000000] [--keep DIR]"""
import argparse, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anyps3.runtime import MEMORY_CPP

ap = argparse.ArgumentParser(); ap.add_argument('--iters', type=int, default=2000000); ap.add_argument('--keep')
a = ap.parse_args()
src = '#include <cstdint>\n#include <cstdio>\n#include <cinttypes>\n#include <chrono>\n' + MEMORY_CPP + r'''
int main(){
 GuestMemory m;m.map(0x10000000,1<<20,6,nullptr,0);m.enable_concurrency();
 alignas(128) static std::uint8_t buf[16384];
 struct Case{const char* name;unsigned size;std::uint64_t offset;};
 const Case cases[]={{"128 B",128,0x100},{"512 B",512,0x200},{"2 KiB",2048,0x800},{"4 KiB page",4096,0x1000},{"4 KiB 2 pages",4096,0x1800},{"16 KiB 5 pages",16384,0x2800}};
 for(const bool put:{false,true})for(const auto& c:cases){
  const unsigned n=c.size>=4096?a_iters/8:a_iters;
  const auto t0=std::chrono::steady_clock::now();
  for(unsigned i=0;i<n;++i)m.transfer(0x10000000+c.offset+(i&3)*0x10000,buf,c.size,put);
  const auto t1=std::chrono::steady_clock::now();
  std::printf("%s %-16s %8.1f ns/call\n",put?"PUT":"GET",c.name,std::chrono::duration<double,std::nano>(t1-t0).count()/n);
 }
}
'''.replace('a_iters', str(a.iters))
d = Path(a.keep) if a.keep else Path(tempfile.mkdtemp())
d.mkdir(exist_ok=True)
(d / 'b.cpp').write_text(src)
r = subprocess.run(['c++', '-std=c++17', '-O2', '-march=native', '-ffp-contract=off', '-pthread', str(d / 'b.cpp'), '-o', str(d / 'b')], capture_output=True, text=True)
if r.returncode: sys.exit(r.stderr[-3000:])
print(subprocess.run([str(d / 'b')], capture_output=True, text=True).stdout)
