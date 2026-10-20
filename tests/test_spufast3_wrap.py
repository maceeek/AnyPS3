"""SPUFAST3: relocated native functions whose translated range wraps around the end of the 256 KB local storage.

Build 580/600 regression: a source function that starts below its loaded code (the code is linked at 0x1980, the loaded copy sits at
LS 0x120, i.e. the function window [lo+delta, lo+delta+flen) starts at 0x3f7a0 and wraps) was refused by the prologue
(`fb+flen>0x40000` -> dynamic_miss), so a SPURS kernel loop ran in the interpreter (4.5 FPS).  The expectations below are fixed
arithmetic results of the loops, independent of the generator."""
import shutil
import unittest

from tests import test_spu_block
from tests.spu_asm import Asm

SRC_A = 0x1980   # loop A: r3 counts 1..50, r4 = 2*r3
SRC_B = 0x1860   # loop B: r6 counts by 3 up to 30, r7 = 5 * iterations
SRC_C = 0x1400   # self-modifying loop of 70 instructions (stqa into its own relocated copy)
FIRST, LAST = 0x1000, 0x19c0


def words():
    w = {pc: 0x40200000 for pc in range(FIRST, LAST, 4)}   # nop filler: the function window starts well below the loops
    a = Asm(SRC_A)
    a.label('top'); a.ai(3, 3, 1); a.ai(4, 4, 2); a.ceqi(5, 3, 50); a.brz(5, 'top'); a.label('end'); a.br('end')
    b = Asm(SRC_B)
    b.label('top'); b.ai(6, 6, 3); b.ai(7, 7, 5); b.ceqi(8, 6, 30); b.brz(8, 'top'); b.label('end'); b.br('end')
    c = Asm(SRC_C)
    c.label('top'); c.il(9, 1); c.stqa(10, 0x120)
    for _ in range(66):
        c.ai(11, 11, 1)
    c.br('top')
    for asm in (a, b, c):
        w.update({asm.base + 4 * i: x for i, x in enumerate(asm.finish())})
    return w


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuWrapTests(unittest.TestCase):
    def native(self, body):
        test_spu_block.SpuBlockTests().native(words(), body)

    def test_wrapped_relocated_loops_run_natively_and_exactly(self):
        # Placements: wrapped windows (L < 0x980 for loop A) and unwrapped ones; loop B additionally with a window whose wrap
        # point lies inside a 64-byte validation region (L_B = 0x10 -> wrap at function offset 0x850).
        self.native(r'''
 const std::vector<std::uint8_t> a(ls+0x1980,ls+0x1980+20),b(ls+0x1860,ls+0x1860+20);   // 4 loop words + the self-branch
 auto run=[&](const std::vector<std::uint8_t>& code,unsigned place,unsigned budget){
  std::fill(ls,ls+0x40000,0);std::copy(code.begin(),code.end(),ls+place);
  t.gpr={};t.pc=place;t.steps=0;t.reloc_delta=0;t.relocated_entries=0;t.interpreted_instructions=0;t.interrupts_enabled=false;
  assert(spu_run(s,budget) && t.steps==budget);   // loop, then the self-branch spins until the budget is gone
 };
 for(const unsigned place:{0x100u,0x120u,0x7c4u,0x978u,0x980u,0x2c0u,0x20000u,0x3ffe0u}){
  run(a,place,300);
  assert(spu_word(t.gpr[3],0)==50 && spu_word(t.gpr[4],0)==100 && spu_word(t.gpr[5],0)==0xffffffffu);
  assert(t.pc==((place+16)&0x3fffc));
  if(!(t.interpreted_instructions.value.load()==0 && t.relocated_entries.value.load()>0)){std::fprintf(stderr,"PLACE %x interpreted=%llu relocated=%llu\n",place,(unsigned long long)t.interpreted_instructions.value.load(),(unsigned long long)t.relocated_entries.value.load());std::abort();}
 }
 for(const unsigned place:{0x10u,0x20u,0x40u,0x100u,0x3ff00u}){
  run(b,place,300);
  assert(spu_word(t.gpr[6],0)==30 && spu_word(t.gpr[7],0)==50 && spu_word(t.gpr[8],0)==0xffffffffu);
  assert(t.pc==((place+16)&0x3fffc));
  if(!(t.interpreted_instructions.value.load()==0 && t.relocated_entries.value.load()>0)){std::fprintf(stderr,"PLACE %x interpreted=%llu relocated=%llu\n",place,(unsigned long long)t.interpreted_instructions.value.load(),(unsigned long long)t.relocated_entries.value.load());std::abort();}
 }
''')

    def test_store_into_own_wrapped_range_is_noticed(self):
        # The loop is linked at 0x1400, loaded at LS 0x120 (wrapped window).  Its stqa writes r10 over the loaded copy of
        # `il r9,1` (LS 0x120).  One pass is 69 instructions: il, stqa, 66 x ai, br.  Instruction 70 is the il of the second pass,
        # reached inside the same native call (budget 70 >= 64: the exact entry guard is not run again), so only the
        # self-store check can make it execute the stored `il r9,7` instead of the stale translated `il r9,1`.
        self.native(r'''
 const std::vector<std::uint8_t> c(ls+0x1400,ls+0x1400+70*4);
 std::fill(ls,ls+0x40000,0);std::copy(c.begin(),c.end(),ls+0x120);
 const std::uint32_t il7=(0x081u<<23)|(7u<<7)|9u;
 t.gpr={};t.pc=0x120;t.steps=0;t.reloc_delta=0;t.relocated_entries=0;t.interpreted_instructions=0;t.interrupts_enabled=false;
 spu_word(t.gpr[10],0,il7);for(unsigned w=1;w<4;++w)spu_word(t.gpr[10],w,spu_load_word(ls+0x120+4*w));
 assert(spu_run(s,70) && t.steps==70);
 assert(spu_word(t.gpr[9],0)==7 && spu_word(t.gpr[11],0)==66 && t.pc==0x124);
 assert(t.relocated_entries.value.load()>0);
''')


if __name__ == '__main__':
    unittest.main()
