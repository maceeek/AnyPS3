"""SPUFAST runtime regressions: superblock dispatch state, budgets and limits."""
import shutil
import unittest
from tests import test_spu_block


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuFastRuntimeTests(unittest.TestCase):
    def test_declined_block_does_not_disable_later_relocated_dispatch(self):
        # 20 x AI r3,r3,1 then STOP 1, original at 0x600, executed at 0x20000.
        code = bytes.fromhex('1c004183') * 20 + bytes.fromhex('00000001')
        test_spu_block.SpuBlockTests().native({0x100: 1}, r'''
 const std::uint8_t code[]={''' + ','.join(map(str, code)) + r'''};
 std::copy_n(code,sizeof(code),ls+0x20000);t.pc=0x20000;
 // The 20-word block crosses the exact hard limit 5: declined, exact interpreted prefix.
 spu_step(s,t,ls,100,5);assert(t.steps==5 && t.pc==0x20014 && spu_word(t.gpr[3],0)==5);
 const auto interpreted=t.interpreted_instructions.value.load();assert(interpreted==5);
 // A later ordinary slice must again use the relocated superblock (no stale decline).
 t.pc=0x20000;t.steps=0;t.gpr={};
 bool stopped=false;try{spu_step(s,t,ls,1000);}catch(const SpuSliceEnd&){stopped=true;}
 assert(stopped && t.steps==20 && t.pc==0x20054 && spu_word(t.gpr[3],0)==20);
 assert(t.interpreted_instructions.value.load()==interpreted && t.relocated_entries.value.load()>0);
''', ((0x600, code),))

    def test_scheduler_slice_overshoot_is_bounded_and_small_budgets_are_exact(self):
        # 40 x AI then BR back: one 41-instruction block.
        words = {0x100 + 4 * i: 0x1c004183 for i in range(40)}
        words[0x1a0] = 0x327ffb00  # BR -40 words
        test_spu_block.SpuBlockTests().native(words, r'''
 for(unsigned budget=1;budget<64;++budget){t.pc=0x100;t.steps=0;
  spu_step(s,t,ls,budget);assert(t.steps==budget);}
 t.pc=0x100;t.steps=0;spu_step(s,t,ls,64);assert(t.steps>=64 && t.steps<64+41);
 t.pc=0x100;t.steps=0;spu_step(s,t,ls,1000,100);assert(t.steps==100); // hard limit exact
''')


if __name__ == '__main__':
    unittest.main()
