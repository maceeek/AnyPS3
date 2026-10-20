#!/usr/bin/env python3
"""Synthetic SPU translation micro-benchmark (diagnostic, not a game measurement): three generic kernels run through spu_run
on the generated native code of the CURRENT tree; prints ns per retired SPU instruction.
  fp     : unrolled vector FP loop (lqd/fm/fma/fa/shufb/fms/stqd), 14 instr/iteration, one loop branch
  branch : short blocks (3-4 instr) separated by conditional branches (integer ops)
  call   : brsl/bi call+return pair around a 10-instruction callee, plus loop branch
Usage (project root of a tree): python bench/spufast2_spu_bench.py [--march native|generic] [--instr 20000000] [--keep DIR]"""
import argparse, subprocess, sys, tempfile
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_spu_block import image, setup


def rr(op, rb, ra, rt): return (op << 21) | (rb << 14) | (ra << 7) | rt
def ri10(op, v, ra, rt): return (op << 24) | ((v & 0x3ff) << 14) | (ra << 7) | rt
def ri16(op, v, rt): return (op << 23) | ((v & 0xffff) << 7) | rt
def rrr(op, rt, rb, ra, rc): return (op << 28) | (rt << 21) | (rb << 14) | (ra << 7) | rc


FP, BR, CL, CE, IN, FO, XC, XE, BL = 0x10100, 0x10300, 0x10600, 0x10700, 0x10900, 0x10b00, 0x10c00, 0x12c00, 0x10e00


def kernels():
    w = {}
    def put(base, words):
        for i, x in enumerate(words): w[base + 4 * i] = x
    # fp loop at 0x100: r3 = src pointer, r4 = dst pointer, r5 = count
    fp = []
    for u in range(2):
        fp += [ri10(0x34, 3 * u, 3, 10), ri10(0x34, 3 * u + 1, 3, 11), ri10(0x34, 3 * u + 2, 3, 12),
               rr(0x2c6, 11, 10, 13), rrr(0xe, 14, 12, 10, 13), rr(0x2c4, 11, 14, 15), rrr(0xb, 16, 14, 15, 20),
               rrr(0xf, 17, 12, 16, 10), ri10(0x24, 256 + 2 * u, 4, 17), ri10(0x24, 257 + 2 * u, 4, 15)]
    fp += [ri10(0x1c, 96, 3, 3), rr(0x0c1, 21, 3, 3), ri10(0x1c, 64, 4, 4), rr(0x0c1, 21, 4, 4), ri10(0x1c, -1, 5, 5)]
    here = FP + 4 * len(fp)
    fp += [ri16(0x042, (FP - here) // 4, 5), 1]
    put(FP, fp)
    # variants: same loop with integer ops instead of FP ops; FP ops only (no memory ops)
    inn, fo = [], []
    for u in range(2):
        inn += [ri10(0x34, 3 * u, 3, 10), ri10(0x34, 3 * u + 1, 3, 11), ri10(0x34, 3 * u + 2, 3, 12),
                rr(0x0c0, 11, 10, 13), rr(0x0c1, 12, 13, 14), rr(0x241, 11, 14, 15), rrr(0xb, 16, 14, 15, 20),
                rr(0x0c0, 12, 16, 17), ri10(0x24, 256 + 2 * u, 4, 17), ri10(0x24, 257 + 2 * u, 4, 15)]
        fo += [rr(0x2c6, 11, 10, 13), rrr(0xe, 14, 12, 10, 13), rr(0x2c4, 11, 14, 15), rrr(0xb, 16, 14, 15, 20), rrr(0xf, 17, 12, 16, 10)] * 2
    for lst, base in ((inn, IN), (fo, FO)):
        lst += [ri10(0x1c, 96, 3, 3), rr(0x0c1, 21, 3, 3), ri10(0x1c, 64, 4, 4), rr(0x0c1, 21, 4, 4), ri10(0x1c, -1, 5, 5)]
        here = base + 4 * len(lst)
        lst += [ri16(0x042, (base - here) // 4, 5), 1]
        put(base, lst)
    # predictable short blocks (4 instr, always-taken forward branch over a dummy), r9 = 1
    bl = []
    for k in range(10):
        bl += [ri10(0x1c, 1, 3, 3), rr(0x0c0, 3, 6, 6), rr(0x241, 3, 6, 7), ri16(0x042, 2, 9), ri10(0x1c, 5, 8, 8)]
    bl += [ri10(0x1c, -1, 5, 5)]
    here = BL + 4 * len(bl)
    bl += [ri16(0x042, (BL - here) // 4, 5), 1]
    put(BL, bl)
    # branchy integer code at 0x300: r3 counter, r6 acc
    br = []
    base = BR
    for k in range(8):
        br += [ri10(0x1c, 1, 3, 3), ri10(0x14, 3 if k % 2 else 1, 3, 4), 0]  # ai, andi, brz placeholder
        br[-1] = ri16(0x040, 3, 4)                       # brz r4,+3 insns (skips the next two)
        br += [ri10(0x1c, k + 2, 6, 6), rr(0x0c0, 3, 6, 7)]
    br += [ri10(0x1c, -1, 5, 5)]
    here = base + 4 * len(br)
    br += [ri16(0x042, (base - here) // 4, 5), 1]
    put(base, br)
    # call/return at 0x600: loop calls callee at 0x700
    cl = [ri16(0x066, (CE - CL) // 4, 0)]       # brsl r0 -> callee
    cl += [ri10(0x1c, -1, 5, 5)]
    here = CL + 4 * len(cl)
    cl += [ri16(0x042, (CL - here) // 4, 5), 1]
    put(CL, cl)
    callee = [ri10(0x1c, 1, 6, 6), rr(0x0c0, 6, 6, 7), rr(0x0c1, 7, 6, 8), rr(0x241, 8, 7, 9), ri10(0x1c, 3, 9, 9),
              rr(0x0c0, 9, 6, 10), ri10(0x14, 255, 10, 10), rr(0x0c0, 10, 7, 11), rr(0x1a8, 0, 0, 0)]  # bi r0
    put(CE, callee)
    cx = [ri16(0x066, (XE - XC) // 4, 0), ri10(0x1c, -1, 5, 5)]
    here = XC + 4 * len(cx)
    cx += [ri16(0x042, (XC - here) // 4, 5), 1]
    put(XC, cx)
    put(XE, callee)   # same callee code in a different 4 KB function: two dispatcher transitions per iteration
    return w


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--march', default='native'); ap.add_argument('--instr', type=int, default=4000000)
    ap.add_argument('--keep', default=''); ap.add_argument('--extra', default='', help='extra compiler flags, e.g. "-fno-tree-loop-im"'); a = ap.parse_args()
    import os; env = dict(os.environ, ANYPS3_SPU_MAX_STEPS='100000000000')
    words = kernels()
    img = image(words)
    translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
    source = replace(translated, spu_images=(img,), spu_overlays=((),)).cpp()
    source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
    source += r'''
#include <cassert>
int main(){
 PpuState s;(void)s;
''' + setup(img).replace('(void)call;', '') + f'''
 const std::uint64_t N={a.instr};
 // data: pseudo-random normal floats in 0..0x1000
 std::uint32_t seed=12345;
 for(unsigned o=0;o<0x1000;o+=4){{seed=seed*1664525u+1013904223u;std::uint32_t x=(seed&0x807fffffu)|((0x70u+((seed>>23)&0xf))<<23);
  ls[o]=x>>24;ls[o+1]=x>>16;ls[o+2]=x>>8;ls[o+3]=x;}}
 struct K{{const char* name;unsigned pc;}} ks[]={{{{"fp",0x{FP:x}}},{{"branch",0x{BR:x}}},{{"call",0x{CL:x}}},{{"intloop",0x{IN:x}}},{{"fponly",0x{FO:x}}},{{"xcall",0x{XC:x}}},{{"blocks",0x{BL:x}}}}};
 for(auto& k:ks){{
  double best=1e9;
  for(int rep=0;rep<15;++rep){{
   t.pc=k.pc;t.steps=0;t.stopped=false;spu_word(t.gpr[3],0,0);spu_word(t.gpr[4],0,0);spu_word(t.gpr[5],0,0x7fffffff);
   spu_word(t.gpr[6],0,0);for(unsigned w=0;w<4;++w)spu_word(t.gpr[9],w,1);for(unsigned w=0;w<4;++w)spu_word(t.gpr[21],w,0xfff);std::fill(t.gpr[20].begin(),t.gpr[20].end(),0);for(unsigned i=0;i<16;++i)t.gpr[20][i]=(i*7+3)&0x1f;
   timespec c0,c1;clock_gettime(CLOCK_THREAD_CPUTIME_ID,&c0);
   while(t.steps<N)spu_run(s,4096);
   clock_gettime(CLOCK_THREAD_CPUTIME_ID,&c1);
   best=std::min(best,((c1.tv_sec-c0.tv_sec)*1e9+(c1.tv_nsec-c0.tv_nsec))/double(t.steps));
  }}
  std::printf("BENCH %s %.3f ns/instr\\n",k.name,best);
 }}
}}
'''
    with tempfile.TemporaryDirectory() as d:
        p = Path(a.keep or d); p.mkdir(exist_ok=True)
        (p / 'b.cpp').write_text(source)
        flags = (['-O2', '-march=native'] if a.march == 'native' else ['-O2']) + a.extra.split()
        build = subprocess.run(['c++', '-std=c++17', *flags, '-ffp-contract=off', '-pthread', str(p / 'b.cpp'), '-o', str(p / 'b')], capture_output=True, text=True)
        if build.returncode: print(build.stderr[-3000:]); sys.exit(1)
        run = subprocess.run([str(p / 'b')], capture_output=True, text=True, timeout=600, env=env)
        print(run.stdout, run.stderr[-2000:])

main()
