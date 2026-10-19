"""SPUFAST: superblock translation vs. the original per-instruction lifted bodies.

The oracle is anyps3.spu.lift (the established ISA semantics, itself checked
by independent fixed vectors in test_spu*, test_spudyn_isa); the candidate is the
generated native superblock with in-register templates.  Every allocated opcode
row is checked with random fields and register contents, and random sequences
check value caching, representations, aliases and write-back.  Each corpus runs
with the SSE2 baseline and with -march=native (AVX2/AVX-512 paths on this host).
"""
from dataclasses import replace
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from anyps3.elf import ElfImage, Segment
from anyps3.errors import AnyPS3Error
from anyps3.program import compile_program
from anyps3.spu import lift, lift_parts
from anyps3 import spu_block
from tests.test_anyps3 import BLR, program

STOP1 = 0x00000001
BLOCKING = (0, 0xd, 0xf, 0x10d, 0x140, 2, 3, 0x1aa, 0x1ab)


def build_and_run(test, images, body, flags):
    translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
    source = replace(translated, spu_images=images).cpp()
    source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
    source += r'''
#include <cassert>
#pragma GCC diagnostic ignored "-Wtype-limits"
int main(){
 PpuState s;(void)s;
''' + body + '\n std::puts("ok");\n}\n'
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / 't.cpp').write_text(source)
        build = subprocess.run(['c++', '-std=c++17', *flags, '-Wall', '-Wextra', '-Werror', '-pthread',
                                str(p / 't.cpp'), '-o', str(p / 't')], capture_output=True, text=True, timeout=900)
        test.assertEqual(build.returncode, 0, build.stderr[-4000:])
        run = subprocess.run([str(p / 't')], capture_output=True, text=True, timeout=300)
        test.assertEqual(run.returncode, 0, run.stderr[-4000:])
        test.assertIn('ok', run.stdout)


def image(words, base):
    data = b''.join(w.to_bytes(4, 'big') for w in words)
    return ElfImage(data, 32, 'big', 0, 2, 23, base, (Segment(0, 1, 7, 0, base, len(data), len(data), 4),))


HARNESS = r'''
 auto init=[&](NativeSpu::Thread& t,std::uint32_t seed){
  for(auto& r:t.gpr){
   seed=seed*1664525u+1013904223u;const unsigned mode=seed>>29;
   for(unsigned w=0;w<4;++w){seed=seed*1664525u+1013904223u;std::uint32_t x=seed;
    // Mostly ordinary floats and small integers, plus raw bit patterns.
    if(mode<3)x=(x&0x807fffffu)|((0x70u+((x>>23)&0x1f))<<23);
    else if(mode==3)x&=0x3fff0u;
    else if(mode==4)x&=0xffu;
    spu_word(r,w,x);}
  }
  for(unsigned i=0;i<16;++i)t.fpscr[i]=0;
  t.srr0=0x120;t.srr0_valid=true;
 };
 auto same=[&](const NativeSpu::Thread& a,const NativeSpu::Thread& b,const std::vector<std::uint8_t>& x,const std::vector<std::uint8_t>& y){
  return a.gpr==b.gpr && a.fpscr==b.fpscr && a.pc==b.pc && x==y && a.interrupts_enabled==b.interrupts_enabled;
 };
 auto report=[&](const char* what,unsigned pc,std::uint32_t word,const NativeSpu::Thread& a,const NativeSpu::Thread& b){
  std::fprintf(stderr,"DIFF %s pc=%05x word=%08x expected_pc=%05x actual_pc=%05x\n",what,pc,word,a.pc,b.pc);
  for(unsigned r=0;r<128;++r)if(a.gpr[r]!=b.gpr[r])std::fprintf(stderr," r%u exp %08x %08x %08x %08x got %08x %08x %08x %08x\n",r,
    spu_word(a.gpr[r],0),spu_word(a.gpr[r],1),spu_word(a.gpr[r],2),spu_word(a.gpr[r],3),spu_word(b.gpr[r],0),spu_word(b.gpr[r],1),spu_word(b.gpr[r],2),spu_word(b.gpr[r],3));
  if(a.fpscr!=b.fpscr)std::fprintf(stderr," fpscr exp %08x %08x %08x %08x got %08x %08x %08x %08x\n",spu_word(a.fpscr,0),spu_word(a.fpscr,1),spu_word(a.fpscr,2),spu_word(a.fpscr,3),spu_word(b.fpscr,0),spu_word(b.fpscr,1),spu_word(b.fpscr,2),spu_word(b.fpscr,3));
  std::abort();
 };
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuFastBlockTests(unittest.TestCase):
    def single_cases(self, seed):
        rng = random.Random(seed)
        cases = []
        for op in range(2048):
            if op in BLOCKING:
                continue
            for low in (0, 0x1fffff, rng.getrandbits(21), rng.getrandbits(21)):
                word = op << 21 | low
                try:
                    body = lift(0, word)
                except AnyPS3Error:
                    continue
                if any(token in body for token in ('throw', 'channel', 'spu_stop(', 'spu_dynamic_fault')):
                    continue
                cases.append(word)
        return cases

    def run_single(self, flags):
        """Each opcode as a one-instruction superblock, entered by spu_step(budget 1)."""
        cases = self.single_cases(0x5350554641)
        self.assertGreater(len(cases), 2500)
        base = 0x8000
        words = []
        for word in cases:
            words += [word, STOP1]
        img = image(words, base)
        body = HARNESS + r'''
 // spu_step refuses threads no spu_prepare bound to a translated image; these hand-built threads use image 0.
 NativeSpu::Thread proto;proto.image_index=0;std::vector<std::uint8_t> original(0x40000);
 for(unsigned i=0;i<original.size();++i)original[i]=std::uint8_t(i*29+7);
 const std::uint8_t code[]={''' + ','.join(map(str, img.data)) + r'''};
 std::copy_n(code,sizeof(code),original.data()+''' + str(base) + r''');
 auto check=[&](unsigned pc,std::uint32_t word,auto legacy){
  NativeSpu::Thread a=proto,b=proto;init(a,word^pc);init(b,word^pc);a.pc=b.pc=pc;
  auto ls1=original,ls2=original;
  bool f1=false,f2=false;
  try{auto& t=a;auto* ls=ls1.data();auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};(void)g;(void)ls;legacy(t,ls,g,out);}catch(const std::runtime_error&){f1=true;}
  try{spu_step(s,b,ls2.data(),1);}catch(const std::runtime_error&){f2=true;}
  if(f1!=f2 || !same(a,b,ls1,ls2) || (!f2 && b.steps!=1))report("single",pc,word,a,b);
 };
'''
        for i, word in enumerate(cases):
            pc = base + 8 * i
            body += (f' check({pc},UINT32_C({word}),[&](NativeSpu::Thread& t,std::uint8_t* ls,auto& g,SpuRegister& out){{'
                     f'(void)t;(void)ls;(void)g;(void)out;{lift(pc, word)}}});\n')
        build_and_run(self, (img,), body, flags)

    def run_sequences(self, flags, count=400):
        """Random straight-line blocks: caching, representation changes and write-back."""
        rng = random.Random(0x51454e43 ^ len(flags))
        pool = [w for w in self.single_cases(0x1234) if spu_block.decode(0, w, lift_parts).kind in ('plain', 'store')]
        base = 0x10000
        sequences = []
        for _ in range(count):
            n = rng.randint(2, 40)
            seq = []
            for _ in range(n):
                word = rng.choice(pool)
                # Concentrate on few registers so values flow between templates.
                regs = [rng.choice((3, 4, 5, 6, 7, 8, 80, 127)) for _ in range(4)]
                op = word >> 21
                if op >> 7 in (8, 0xb, 0xc, 0xd, 0xe, 0xf):
                    word = (word & ~((127 << 21) | (127 << 14) | (127 << 7) | 127)) | regs[0] << 21 | regs[1] << 14 | regs[2] << 7 | regs[3]
                else:
                    word = (word & ~(127 | (127 << 7))) | regs[0] | (regs[1] << 7)
                seq.append(word)
            sequences.append(seq)
        words = []
        starts = []
        for seq in sequences:
            starts.append(base + 4 * len(words))
            words += seq + [STOP1]
        img = image(words, base)
        body = HARNESS + r'''
 // spu_step refuses threads no spu_prepare bound to a translated image; these hand-built threads use image 0.
 NativeSpu::Thread proto;proto.image_index=0;std::vector<std::uint8_t> original(0x40000);
 for(unsigned i=0;i<original.size();++i)original[i]=std::uint8_t(i*31+5);
 const std::uint8_t code[]={''' + ','.join(map(str, img.data)) + r'''};
 std::copy_n(code,sizeof(code),original.data()+''' + str(base) + r''');
 auto check=[&](unsigned pc,unsigned n,auto legacy){
  NativeSpu::Thread a=proto,b=proto;init(a,pc*7+n);init(b,pc*7+n);a.pc=b.pc=pc;
  auto ls1=original,ls2=original;
  {auto& t=a;auto* ls=ls1.data();auto& g=t.gpr;(void)g;(void)ls;legacy(t,ls,g);}
  // The per-word oracle cannot model stores into its own later words (covered elsewhere).
  if(std::memcmp(ls1.data()+pc,original.data()+pc,n*4+4))return;
  bool stop=false;
  try{while(b.steps<n)spu_step(s,b,ls2.data(),1000);}catch(const SpuSliceEnd&){stop=true;}
  if(!stop || b.steps!=n || !same(a,b,ls1,ls2))report("sequence",pc,n,a,b);
 };
'''
        for seq, start in zip(sequences, starts):
            text = ''
            for k, word in enumerate(seq):
                text += '{[[maybe_unused]] SpuRegister out{};' + lift(start + 4 * k, word) + '}'
            text += f't.pc={start + 4 * len(seq) + 4};'
            body += (f' check({start},{len(seq)},[&](NativeSpu::Thread& t,std::uint8_t* ls,auto& g){{(void)t;(void)ls;(void)g;{text}}});\n')
        build_and_run(self, (img,), body, flags)

    def test_every_opcode_baseline_sse2(self):
        self.run_single(('-O1',))

    def test_every_opcode_native_isa(self):
        self.run_single(('-O2', '-march=native', '-ffp-contract=off'))

    def test_random_sequences_baseline_sse2(self):
        self.run_sequences(('-O1',))

    def test_random_sequences_native_isa(self):
        self.run_sequences(('-O2', '-march=native', '-ffp-contract=off'))


if __name__ == '__main__':
    unittest.main()
