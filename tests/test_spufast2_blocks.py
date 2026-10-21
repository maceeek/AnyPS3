"""SPUFAST2: generated-code behaviour that the per-opcode corpora of test_spufast_blocks cannot reach (multi-block control flow,
channel instructions, validation state across stores and effects), with fixed independent expectations."""
import shutil
import unittest

from tests import test_spu_block
from tests.spu_asm import Asm, GETLLAR

STOP = 0


def program(asm):
    return {asm.base + 4 * i: w for i, w in enumerate(asm.finish())}


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuFast2BlockTests(unittest.TestCase):
    def native(self, words, body, overlays=()):
        test_spu_block.SpuBlockTests().native(words, body, overlays)

    def test_mfc_parameter_channel_writes_stay_inside_the_block(self):
        a = Asm()
        for reg, value in zip((3, 4, 5, 6, 7), (0x1230, 0, 0x4560, 0x80, 5)):
            a.il(reg, value)
        for channel, reg in zip(range(16, 21), (3, 4, 5, 6, 7)):
            a.wrch(channel, reg)
        a.ai(8, 3, 0x10)      # a following arithmetic instruction in the same block uses the architectural register
        a.wrch(16, 8)         # rewrite of channel 16 with a computed value
        a.wrch(22, 3)         # tag mask
        words = program(a)
        self.native(words, r'''
 assert(spu_run(s,13) && t.steps==13 && t.pc==0x100+13*4);
 assert(t.dma[0]==0x1240 && t.dma[1]==0 && t.dma[2]==0x4560 && t.dma[3]==0x80 && t.dma[4]==5 && t.tag_mask==0x1230);
 assert(spu_word(t.gpr[8],0)==0x1240 && spu_word(t.gpr[3],0)==0x1230);
''')

    def test_store_into_own_code_range_takes_effect_on_the_next_instruction_and_validation_restarts(self):
        # Loop body: il r6,<v>; stqa r5,<code address of the il>; br -> the next pass must execute the stored word.
        a = Asm()
        a.label('top')
        a.il(6, 1)             # 0x100 : rewritten by the store below
        a.stqa(5, 0x100)       # overwrites 0x100..0x10f with r5 (quadword)
        a.br('top')
        a.il(0, 0)
        words = program(a)
        self.native(words, r'''
 // r5 = {il r6,9 ; the loop's own stqa and br unchanged ; the following word}, i.e. only the first instruction changes
 const std::uint32_t il9=(0x081u<<23)|(9u<<7)|6u;
 spu_word(t.gpr[5],0,il9);for(unsigned w=1;w<4;++w)spu_word(t.gpr[5],w,spu_load_word(ls+0x100+4*w));
 // Budget >= 64: the call runs many passes without the exact entry guard, so only the validated-region state can
 // notice the store.  Pass 1 executes the original il r6,1; every later pass must execute the stored il r6,9.
 assert(spu_run(s,200) && t.steps>=6);
 assert(spu_word(t.gpr[6],0)==9);
''')

    def test_short_block_is_entered_natively_when_the_bytes_after_it_differ(self):
        # AI, AI, BR -8 form a 12-byte block; the translated source has a 4th word that the LS does not (overlay boundary).
        # The dispatcher must still enter the native function: it validates exactly the bytes of the block it executes.
        words = {0x100: 0x1c004183, 0x104: 0x1c004183, 0x108: 0x327fff00, 0x10c: 0x1c004185}
        self.native(words, r'''
 const std::uint32_t other=0x1c004186;
 for(unsigned i=0;i<4;++i)ls[0x10c+i]=other>>(24-8*i);
 t.pc=0x100;t.steps=0;t.interpreted_instructions=0;
 assert(spu_run(s,300) && t.steps>=300);
 assert(t.interpreted_instructions.value.load()==0);
 assert(spu_word(t.gpr[3],0)>=190 && spu_word(t.gpr[3],0)<=2*(t.steps/3+1));
''')


if __name__ == '__main__':
    unittest.main()
