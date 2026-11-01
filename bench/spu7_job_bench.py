#!/usr/bin/env python3
"""HIRES7 synthetic SPU job (diagnostic, not a game measurement): one SPU program shaped like the awaited ig-lane job, run
through the production lifter (compile_program -> spu_block superblocks) and the production runtime (spu_run, MFC channels,
GuestMemory::transfer in concurrent mode with the page broker enabled) on a NON-owner host thread, as a pool lane runs it.

Per iteration of the job loop (all addresses fixed, the input bytes never change, so every iteration does identical work):
  * DMA: GET 16 KiB (5 guest pages), 2 KiB, 512 B, 128 B; GETL list of 4 elements (1 KiB, 256 B, 128 B, 2 KiB);
    tag-mask/ALL wait; PUT 2 KiB, 512 B, 128 B, 4 KiB (2 pages); PUTL list of 2 elements; tag wait.
  * SIMD integer function (other 4 KiB native function, BRSL call, BI $lr return): lqd/a/xor/shufb/rotqbyi/and/stqd loop.
  * FP function, called INDIRECTLY (BISL through a register): lqd/fm/fma/fa/fms/fm/stqd loop over GET data in which 1/16 of the
    words are tiny (2^-124): their products underflow, so those lanes take the exact path (zero results without an exact-zero
    proof); a further share of lanes gives exact zeros (x + -x).
  * 32 leaf calls into a third 4 KiB native function, 8 local leaf calls inside the main function (BRSL/BI inside one function).
Modes: `time` (default) prints CPU ns per job iteration and per SPU instruction (min of repetitions, thread CPU clock);
`count` runs a fixed number of iterations once (for callgrind: valgrind --tool=callgrind --separate-threads=yes).
Usage (tree root): python bench/spu7_job_bench.py [--march native|x86-64-v3] [--iters N] [--mode time|count] [--keep DIR]
       [--simd Q] [--fp Q] [--build-only]"""
import argparse, os, subprocess, sys, tempfile
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_spu_block import image, setup


def rr(op, rb, ra, rt): return (op << 21) | ((rb & 127) << 14) | (ra << 7) | rt
def ri7(op, i, ra, rt): return (op << 21) | ((i & 127) << 14) | (ra << 7) | rt
def ri10(op, v, ra, rt): return (op << 24) | ((v & 0x3ff) << 14) | (ra << 7) | rt
def ri16(op, v, rt): return (op << 23) | ((v & 0xffff) << 7) | rt
def ri18(op, v, rt): return (op << 25) | ((v & 0x3ffff) << 7) | rt
def rrr(op, d, rb, ra, rc): return (op << 28) | (d << 21) | (rb << 14) | (ra << 7) | rc


class Asm:
    """Labels + relative branch fixups; one instance per 4 KiB native function."""
    def __init__(self, base): self.base, self.words, self.labels, self.fix = base, [], {}, []
    def here(self): return self.base + 4 * len(self.words)
    def label(self, name): self.labels[name] = self.here()
    def e(self, w): self.words.append(w & 0xffffffff)
    def branch(self, op, rt, target):  # target: label name or absolute address
        self.fix.append((len(self.words), op, rt, target)); self.e(0)
    def finish(self, labels):
        for i, op, rt, target in self.fix:
            to = target if isinstance(target, int) else labels[target]
            self.words[i] = ri16(op, (to - (self.base + 4 * i)) // 4, rt)
        return {self.base + 4 * i: w for i, w in enumerate(self.words)}
    # instructions
    def il(self, rt, v): self.e(ri16(0x081, v, rt))
    def ila(self, rt, v): self.e(ri18(0x21, v, rt))
    def ilhu(self, rt, v): self.e(ri16(0x082, v, rt))
    def iohl(self, rt, v): self.e(ri16(0x0c1, v, rt))
    def ai(self, rt, ra, v): self.e(ri10(0x1c, v, ra, rt))
    def a(self, rt, ra, rb): self.e(rr(0x0c0, rb, ra, rt))
    def xor(self, rt, ra, rb): self.e(rr(0x241, rb, ra, rt))
    def and_(self, rt, ra, rb): self.e(rr(0x0c1, rb, ra, rt))
    def rotqbyi(self, rt, ra, n): self.e(ri7(0x1fc, n, ra, rt))
    def shufb(self, d, a, b, c): self.e(rrr(0xb, d, b, a, c))
    def lqd(self, rt, ra, off): self.e(ri10(0x34, off // 16, ra, rt))
    def stqd(self, rt, ra, off): self.e(ri10(0x24, off // 16, ra, rt))
    def fa(self, rt, ra, rb): self.e(rr(0x2c4, rb, ra, rt))
    def fm(self, rt, ra, rb): self.e(rr(0x2c6, rb, ra, rt))
    def fma(self, d, a, b, c): self.e(rrr(0xe, d, b, a, c))
    def fms(self, d, a, b, c): self.e(rrr(0xf, d, b, a, c))
    def wrch(self, ch, rt): self.e(rr(0x10d, 0, ch, rt))
    def rdch(self, rt, ch): self.e(rr(0x00d, 0, ch, rt))
    def bi(self, ra): self.e(rr(0x1a8, 0, ra, 0))
    def bisl(self, rt, ra): self.e(rr(0x1a9, 0, ra, rt))
    def brsl(self, rt, target): self.branch(0x066, rt, target)
    def brnz(self, rt, target): self.branch(0x042, rt, target)
    def br(self, target): self.branch(0x064, 0, target)
    def ea(self, rt, value): self.ilhu(rt, value >> 16); self.iohl(rt, value & 0xffff)


MAIN, SIMD, FPF, LEAF = 0x100, 0x1100, 0x2100, 0x3100
EA = 0x30000000
LS_A, LS_B, LS_C, LS_D, LS_L, LS_LIST, LS_OUT, LS_FPOUT, LS_PLIST = 0x10000, 0x14000, 0x14800, 0x14c00, 0x15000, 0x1f000, 0x17000, 0x18000, 0x1f100
GETL = ((0x400, EA + 0x9000), (0x100, EA + 0x9800), (0x80, EA + 0xa000), (0x800, EA + 0xa800))
PUTL = ((0x400, EA + 0x88000), (0x200, EA + 0x89000))


def job(simd_q, fp_q):
    m, s, f, l = Asm(MAIN), Asm(SIMD), Asm(FPF), Asm(LEAF)

    def dma(cmd, lsa, ea, size, tag):
        m.ila(20, lsa); m.wrch(16, 20); m.il(21, 0); m.wrch(17, 21); m.ea(22, ea); m.wrch(18, 22)
        m.ila(23, size); m.wrch(19, 23); m.il(24, tag); m.wrch(20, 24); m.il(25, cmd); m.wrch(21, 25)

    def wait(mask):
        m.il(26, mask); m.wrch(22, 26); m.il(27, 2); m.wrch(23, 27); m.rdch(28, 24)

    m.label('loop')
    dma(0x40, LS_A, EA + 0x100, 0x4000, 1)       # 16 KiB over 5 pages
    dma(0x40, LS_B, EA + 0x6000, 0x800, 1)
    dma(0x40, LS_D, EA + 0x6a00, 0x200, 2)
    dma(0x40, LS_C, EA + 0x6c80, 0x80, 2)
    dma(0x44, LS_L, LS_LIST, 8 * len(GETL), 3)    # GETL: EAL = LS address of the list
    wait(0xe)
    # SIMD over the 16 KiB buffer (direct call into another native function)
    m.ila(3, LS_A); m.il(4, simd_q); m.ila(5, LS_OUT); m.brsl(0, SIMD)
    # FP over the 2 KiB + list buffers (indirect call)
    m.ila(3, LS_L); m.il(4, fp_q); m.ila(5, LS_FPOUT); m.ila(6, FPF); m.bisl(0, 6)
    for k in range(32):                             # leaf calls into a third native function
        m.ai(50, 50, k + 1); m.brsl(0, LEAF)
    for k in range(8):                              # local leaf calls (same native function)
        m.ai(51, 51, 3); m.brsl(0, 'local')
    dma(0x20, LS_OUT, EA + 0x80000, 0x800, 4)
    dma(0x20, LS_D, EA + 0x80a00, 0x200, 4)
    dma(0x20, LS_C, EA + 0x80c80, 0x80, 4)
    dma(0x20, LS_FPOUT, EA + 0x81800, 0x1000, 5)  # 4 KiB over 2 pages
    dma(0x24, LS_FPOUT, LS_PLIST, 8 * len(PUTL), 5)
    wait(0x30)
    m.ai(10, 10, -1); m.brnz(10, 'loop')
    m.e(0x00000000)                                 # stop 0 (never reached: r10 starts huge)
    m.label('local'); m.a(52, 52, 51); m.xor(53, 53, 52); m.bi(0)

    s.label('top')
    s.lqd(30, 3, 0); s.lqd(31, 3, 16); s.a(32, 30, 31); s.xor(33, 32, 30); s.shufb(34, 33, 32, 40); s.rotqbyi(35, 34, 4)
    s.and_(36, 35, 41); s.a(37, 36, 33); s.stqd(37, 5, 0); s.ai(3, 3, 32); s.ai(5, 5, 16); s.ai(4, 4, -1); s.brnz(4, 'top'); s.bi(0)

    f.label('top')
    f.lqd(30, 3, 0); f.lqd(31, 3, 16); f.lqd(32, 3, 32); f.fm(33, 30, 31); f.fma(34, 30, 32, 33); f.fa(35, 34, 31)
    f.fms(36, 35, 30, 32); f.fm(37, 36, 42); f.fa(38, 30, 43); f.stqd(37, 5, 0); f.stqd(34, 5, 16); f.stqd(38, 5, 32)
    f.ai(3, 3, 48); f.ai(5, 5, 48); f.ai(4, 4, -1); f.brnz(4, 'top'); f.bi(0)

    l.a(54, 54, 50); l.xor(55, 55, 54); l.ai(56, 56, 1); l.bi(0)

    words = {}
    for x in (m, s, f, l):
        words.update(x.finish({**m.labels, **x.labels}))
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--march', default='native'); ap.add_argument('--iters', type=int, default=2000)
    ap.add_argument('--mode', default='time'); ap.add_argument('--keep', default=''); ap.add_argument('--reps', type=int, default=7)
    ap.add_argument('--simd', type=int, default=512); ap.add_argument('--fp', type=int, default=40)
    ap.add_argument('--build-only', action='store_true'); ap.add_argument('--extra', default='')
    a = ap.parse_args()
    words = job(a.simd, a.fp)
    img = image(words)
    translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
    source = replace(translated, spu_images=(img,), spu_overlays=((),)).cpp()
    source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
    lists = ''.join(f'put_be(ls+0x{LS_LIST + 8 * i:x},0x{size:x});put_be(ls+0x{LS_LIST + 8 * i + 4:x},0x{ea:x});'
                    for i, (size, ea) in enumerate(GETL))
    lists += ''.join(f'put_be(ls+0x{LS_PLIST + 8 * i:x},0x{size:x});put_be(ls+0x{LS_PLIST + 8 * i + 4:x},0x{ea:x});'
                     for i, (size, ea) in enumerate(PUTL))
    source += r'''
#include <cassert>
#include <thread>
static void put_be(std::uint8_t* p,std::uint32_t x){p[0]=x>>24;p[1]=x>>16;p[2]=x>>8;p[3]=x;}
int main(int argc,char** argv){
 const bool count_mode=argc>1 && std::string(argv[1])=="count";
 const std::uint64_t iters=argc>2?std::strtoull(argv[2],nullptr,10):''' + str(a.iters) + r''';const int reps=argc>3?std::atoi(argv[3]):''' + str(a.reps) + r''';
 PpuState s;(void)s;
 s.memory.map(0x''' + f'{EA:x}' + r''',1u<<20,6,nullptr,0);
 {std::uint32_t seed=12345;std::vector<std::uint8_t> init(1u<<20);
  for(unsigned o=0;o<init.size();o+=4){seed=seed*1664525u+1013904223u;
   // normal floats of magnitude 2^-8..2^7; every 16th word tiny (2^-124 class): its products underflow (exact path)
   std::uint32_t x=(seed&0x807fffffu)|((0x77u+((seed>>23)&0xf))<<23);if(((o>>2)&15)==5)x=(seed&0x807fffffu)|(0x03u<<23);
   put_be(init.data()+o,x);}
  s.memory.transfer(0x''' + f'{EA:x}' + r''',init.data(),1u<<20,true);}
 s.memory.enable_concurrency();s.memory.enable_page_broker();
''' + setup(img).replace('(void)call;', '') + lists + r'''
 for(unsigned i=0;i<16;++i)t.gpr[40][i]=(i*7+3)&0x1f;            // shufb pattern
 for(unsigned w=0;w<4;++w){spu_word(t.gpr[41],w,0x7f7f7f7fu);spu_word(t.gpr[42],w,0x3f000000u);spu_word(t.gpr[43],w,0u);}
 // r43 lane 2 = -x of the matching input word would be data dependent; exact zeros come from fm by +0 (lane 1 of r43 = 0)
 std::fprintf(stderr,"asymmetric=%d broker=%d\n",int(s.memory.parallel->asymmetric),int(s.memory.page_broker.enabled.load()));
 const auto entry_gpr=t.gpr;
 auto run=[&](std::uint64_t n,double& ns,std::uint64_t& instr){
  t.pc=t.entry;t.steps=0;t.stopped=false;t.gpr=entry_gpr;spu_word(t.gpr[10],0,0x7fffffffu);
  // the job loop's back branch retires once per iteration; count iterations by the counter register
  timespec c0,c1;clock_gettime(CLOCK_THREAD_CPUTIME_ID,&c0);
  while(0x7fffffffu-spu_word(t.gpr[10],0)<n)spu_run(s,4096);
  clock_gettime(CLOCK_THREAD_CPUTIME_ID,&c1);
  ns=(c1.tv_sec-c0.tv_sec)*1e9+(c1.tv_nsec-c0.tv_nsec);instr=t.steps;
 };
 std::thread lane([&]{
  double ns=0;std::uint64_t instr=0;
  if(count_mode){run(iters,ns,instr);std::printf("COUNT iters=%" PRIu64 " spu_instr=%" PRIu64 "\n",iters,instr);return;}
  double best=1e30;std::uint64_t best_instr=0;
  for(int r=0;r<reps;++r){run(iters,ns,instr);if(ns<best){best=ns;best_instr=instr;}}
  std::printf("BENCH job %.1f ns/iteration %.3f ns/instr (%" PRIu64 " SPU instr/iteration)\n",best/iters,best/best_instr,best_instr/iters);
 });
 lane.join();
 // output digest (identical work per build: the PC-visible result bytes)
 std::vector<std::uint8_t> out(0x10000);s.memory.transfer(0x''' + f'{EA + 0x80000:x}' + r''',out.data(),0x10000,false);
 std::printf("DIGEST %08x\n",crc32(out));
}
'''
    p = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix='spu7-'))
    p.mkdir(parents=True, exist_ok=True)
    (p / 'b.cpp').write_text(source)
    march = ['-march=native'] if a.march == 'native' else [f'-march={a.march}']
    exe = p / 'b'
    build = subprocess.run(['c++', '-std=c++17', '-O2', *march, '-g', '-ffp-contract=off', '-pthread', *a.extra.split(),
                            str(p / 'b.cpp'), '-o', str(exe)], capture_output=True, text=True)
    if build.returncode:
        print(build.stderr[-4000:]); sys.exit(1)
    print('built', exe)
    if a.build_only:
        return
    env = dict(os.environ, ANYPS3_SPU_MAX_STEPS='100000000000', ANYPS3_SPU_THREADS='0', ANYPS3_SPU_WORKER='0',
               ANYPS3_PERF_REPORT='0', SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null')
    r = subprocess.run([str(exe), a.mode, str(a.iters), str(a.reps)], capture_output=True, text=True, timeout=1200, env=env)
    print(r.stdout, r.stderr[-3000:])


if __name__ == '__main__':
    main()
