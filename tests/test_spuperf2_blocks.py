"""Checked runs must preserve every original fetch and retirement boundary."""
import shutil
import unittest
from tests import test_spu_block
from anyps3.spu import generate_parts
from tests.test_spu_block import image


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPerf2BlockTests(unittest.TestCase):
    def native(self, words, body, overlays=()):
        test_spu_block.SpuBlockTests().native(words, body, overlays)

    def test_all_resume_positions_soft_hard_boundaries_and_bad_suffix(self):
        words = {0x100+i*4: 0x1c004183 for i in range(16)}
        words[0x140] = 1
        _, functions, _ = generate_parts((image(words),))
        self.assertIn('spu_region_eq<64>(ls,(flo+0x0u)&0x3ffffu,code+0x0u)', functions[0][1])
        self.assertIn('spu_block_slow(s,t,block,ls,flo,code,0x44u,a,0x0u,64,16,lim,vm,', functions[0][1])
        self.native(words, r'''
 for(unsigned offset=0;offset<16;++offset)for(unsigned budget=1;budget<=17-offset;++budget){
  t.pc=0x100+offset*4;t.steps=0;t.gpr={};
  assert(spu_run(s,budget));
  assert(t.steps==budget && t.pc==0x100+(offset+budget)*4);
  assert(spu_word(t.gpr[3],0)==std::min(budget,16-offset));
 }
 t.pc=0x100;t.steps=0;t.gpr={};ls[0x134]=2;ls[0x135]=ls[0x136]=ls[0x137]=0;
 fault([&]{spu_step(s,t,ls,100);});
 assert(t.steps==13 && t.pc==0x134 && spu_word(t.gpr[3],0)==13);ls[0x134]=0x1c;ls[0x135]=0;ls[0x136]=0x41;ls[0x137]=0x83;
 for(unsigned limit=1;limit<=16;++limit){
  t.pc=0x100;t.steps=100;t.gpr={};
  spu_step(s,t,ls,100,100+limit);
  assert(t.steps==100+limit && t.pc==0x100+limit*4 && spu_word(t.gpr[3],0)==limit);
 }
''')

    def test_stq_then_dma_into_checked_run_and_blocking_channel_prefix(self):
        # STQA r5,0x110; four AI; WRCH DMA; four AI; blocking RDCH.
        words = {0x100: 0x20802205, 0x104: 0x40200000, 0x108: 0x40200000, 0x10c: 0x40200000,
                 **{0x110+i*4: 0x1c004183 for i in range(4)}, 0x120: 0x21a00a86,
                 **{0x130+i*4: 0x1c004204 for i in range(4)}, 0x124: 0x40200000, 0x128: 0x40200000, 0x12c: 0x40200000, 0x140: 0x01a00e87, 0x144: 1}
        patch = bytes.fromhex('1c008183') * 4  # AI r3,r3,2
        patch2 = bytes.fromhex('1c00c204') * 4  # AI r4,r4,3
        self.native(words, r'''
 const std::uint8_t stq[]={0x1c,0,0x81,0x83,0x1c,0,0x81,0x83,0x1c,0,0x81,0x83,0x1c,0,0x81,0x83};
 const std::uint8_t dma[]={0x1c,0,0xc2,4,0x1c,0,0xc2,4,0x1c,0,0xc2,4,0x1c,0,0xc2,4};
 std::copy_n(stq,16,t.gpr[5].begin());std::copy_n(dma,16,mem.access(0x10100,16,true));
 t.dma={0x130,0,0x10100,16,0};spu_word(t.gpr[6],0,0x40);
 assert(spu_run(s,100) && t.steps==16 && t.pc==0x140);
 assert(spu_word(t.gpr[3],0)==8 && spu_word(t.gpr[4],0)==12);
 assert(!spu_run(s,100) && t.steps==16);
 t.in_mailbox={0x12345678};assert(spu_run(s,100) && t.steps==18 && t.pc==0x148);
 assert(spu_word(t.gpr[7],0)==0x12345678);
''', ((0x110, patch), (0x130, patch2)))

    def test_peer_ls_window_write_is_seen_on_next_run(self):
        words = {0x100+i*4: 0x1c004183 for i in range(8)}
        self.native(words, r'''
 assert(spu_run(s,8) && spu_word(t.gpr[3],0)==8);
 // Use the actual peer DMA path; target is the first lane's LS window.
 // Adding a peer may relocate Thread objects, so reacquire the target.
 group_ref.threads.emplace_back();group_ref.context.resize(0x80000);auto& target=group_ref.threads[0];
 auto& peer=group_ref.threads[1];peer.number=1;target.number=0;
 const std::uint8_t patch[]={0x1c,0,0x81,0x83};
 spu_transfer(s,peer,0xf0000100,const_cast<std::uint8_t*>(patch),4,true);
 target.pc=0x100;
 while(target.steps<16)spu_step(s,target,group_ref.context.data(),16-target.steps);
 assert(target.pc==0x120 && target.steps==16 && spu_word(target.gpr[3],0)==17);
''')

    def test_checked_run_at_ls_end_wraps_pc_to_zero(self):
        self.native({0x3fff0+i*4: 0x1c004183 for i in range(4)}, r'''
 assert(spu_run(s,4) && t.steps==4 && t.pc==0 && spu_word(t.gpr[3],0)==4);
 fault([&]{spu_run(s,1);});assert(t.steps==4 && t.pc==0);
''')


if __name__ == '__main__':
    unittest.main()
