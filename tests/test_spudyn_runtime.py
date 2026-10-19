"""Dynamic fetch boundaries, real DMA provenance and precise retirement."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tests import test_spu_block, test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuDynamicRuntimeTests(unittest.TestCase):
    def test_rchcnt_fault_keeps_completed_prefix_in_static_and_dynamic_code(self):
        # RDCH from reserved channel 10 stops explicitly (no modeled read), unlike
        # RCHCNT of reserved channel 127 whose architectural capacity is zero. The
        # faulting channel op keeps the completed prefix in static and dynamic code.
        # (Channel 25, MFC_RdListStallStat, is modeled since stall-and-notify support.)
        test_spu_block.SpuBlockTests().native(
            {0x100:0x1c004183,0x104:0x01a00504,0x108:0x1c004183},r'''
 fault([&]{spu_run(s,3);});
 assert(t.steps==1 && t.pc==0x104 && spu_word(t.gpr[3],0)==1);
 fault([&]{spu_run(s,3);});assert(t.steps==1 && t.pc==0x104);
 const auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-8*i);};
 put(0x104,0x01e03f84); // Reserved channel 127: capacity zero, not a fault.
 assert(spu_run(s,2) && t.steps==3 && t.pc==0x10c && spu_word(t.gpr[3],0)==2);
 assert(spu_word(t.gpr[4],0)==0);
 put(0x20000,0x1c004183);put(0x20004,0x01a00504);put(0x20008,0x1c004183);t.pc=0x20000;
 fault([&]{spu_run(s,3);});
 assert(t.steps==4 && t.pc==0x20004 && spu_word(t.gpr[3],0)==3);
''')

    def test_self_modification_static_dynamic_branches_limits_and_resume(self):
        # Original AI +1 / branch; unknown AI +7 at the same PC. Dynamic
        # code at 0x200 branches back into the original static function.
        test_spu_block.SpuBlockTests().native(
            {0x100: 0x1c004183, 0x104: 0x327fff80}, r'''
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-i*8);};
 put(0x100,0x1c01c183); // AI r3,r3,7, unknown static variant
 assert(spu_run(s,5) && t.steps==5 && t.pc==0x104 && spu_word(t.gpr[3],0)==21);
 put(0x100,0x1c004183);t.pc=0x200;put(0x200,0x1c008204);put(0x204,0x327fdf80);
 assert(spu_run(s,4) && t.steps==9 && t.pc==0x100);
 assert(spu_word(t.gpr[3],0)==22 && spu_word(t.gpr[4],0)==2);
 t.pc=0x200;spu_step(s,t,ls,100,10);assert(t.steps==10 && t.pc==0x204);
 fault([&]{spu_step(s,t,ls,1,10);});assert(t.steps==10 && t.pc==0x204);
 put(0x3fffc,0x1c004204);t.pc=0x3fffc;spu_step(s,t,ls);assert(t.steps==11 && t.pc==0);
 t.pc=0x40000;fault([&]{spu_step(s,t,ls);});assert(t.pc==0x40000 && t.steps==11);
 t.pc=0x201;fault([&]{spu_step(s,t,ls);});assert(t.pc==0x201 && t.steps==11);
 // Dynamic STQA rewrites the very next instruction; it is never cached.
 put(0x200,0x20804205); // STQA r5,0x210
 put(0x204,0x1c004183);put(0x208,0x40200000);put(0x20c,0x40200000);
 const std::uint8_t patch[]={0x1c,0x01,0xc2,4,0,0,0,1,0,0,0,0,0,0,0,0};
 std::copy_n(patch,16,t.gpr[5].begin());t.pc=0x200;
 assert(spu_run(s,100) && t.steps==17 && t.pc==0x218 && spu_word(t.gpr[4],0)==12);
''')

    def test_dynamic_channels_stop_stopd_interrupt_and_reserved_operands(self):
        test_spu_block.SpuBlockTests().native({0x100: 0x40200000}, r'''
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-i*8);};
 t.pc=0x200;put(0x200,0x1c004183);put(0x204,0x01a00e84); // AI; RDCH mailbox
 put(0x208,0x21a00e04);put(0x20c,1); // WRCH out mailbox; STOP 1
 assert(spu_run(s,100) && t.steps==1 && t.pc==0x204);
 assert(!spu_run(s,100) && t.steps==1 && t.pc==0x204);
 t.in_mailbox={0xdeadbeef};assert(spu_run(s,100) && t.steps==4 && t.pc==0x210);
 assert(t.out_mailbox_full && t.out_mailbox==0xdeadbeef && spu_word(t.gpr[4],0)==0xdeadbeef);
 put(0x210,0x28000000); // STOPD, unsupported managed service
 fault([&]{spu_run(s,100);});assert(t.steps==4 && t.pc==0x210 && !t.stopped);
 put(0x210,0x76000000); // CSFLT scale 155, outside ISA range
 fault([&]{spu_run(s,1);});assert(t.steps==4 && t.pc==0x210);
 // Enabled event interrupts preempt an unknown-PC instruction. The handler
 // IRET retries the original PC and therefore uses the same interpreter.
 put(0,0x35400000);put(0x220,0x1c004183);t.pc=0x220;
 t.event_mask=t.event_status=0x10;t.event_ready=true;t.interrupts_enabled=true;
 spu_step(s,t,ls);assert(t.steps==5 && t.pc==0x220 && !t.interrupts_enabled);
 spu_step(s,t,ls);assert(t.steps==6 && t.pc==0x224 && spu_word(t.gpr[3],0)==2);
''')

    def test_dma_get_getl_atomic_origin_hash_dedup_and_explicit_unknown_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'dynamic.jsonl'
            body = 'setenv("ANYPS3_SPU_DYNAMIC_LOG",'+json.dumps(str(log))+',1);'
            body += r'''
 t.id=0x3000200;
 for(unsigned i=0;i<64;++i)mem.store(0x10100+i,1,i);
 mem.store(0x10100,4,0x1c01c183);mem.store(0x10104,4,0x02000000);
 t.dma={0x5600,0,0x10100,64,0};spu_write_channel(s,t,ls,21,0x40);
 t.pc=0x5600;assert(spu_run(s,1) && t.steps==1 && t.pc==0x5604 && spu_word(t.gpr[3],0)==7);
 t.pc=0x5600;assert(spu_run(s,1) && t.steps==2);
 bool caught=false;try{spu_run(s,1);}catch(const std::runtime_error& e){
  const std::string message=e.what();caught=true;
  for(const auto* text:{"SPU id=3000200","pc=05604","word=02000000","size=40","0000000000010100","0000000000010104"})
    assert(message.find(text)!=std::string::npos);
 }
 assert(caught && t.steps==2 && t.pc==0x5604);
 // Partial GET and GETL overwrite only their actual ranges.
 t.dma={0x5610,0,0x10200,16,0};spu_write_channel(s,t,ls,21,0x40);
 assert(t.dma_writes.size()==3);
 const std::uint8_t descriptor[]={0,0,0,16,0,1,3,0};std::copy_n(descriptor,8,ls+0x400);
 t.dma={0x5620,0,0x400,8,0};spu_write_channel(s,t,ls,21,0x44);
 assert(t.dma_writes.size()==4);
 t.dma={0x5700,0,0x10400,0,0};spu_write_channel(s,t,ls,21,0xd0);
 assert(t.dma_writes.back().base==0x5700 && t.dma_writes.back().ea==0x10400);
 // Changing a word in the same diagnostic cell produces a second hash.
 ls[0x5603]=0x84;t.pc=0x5600;spu_step(s,t,ls);
 // Repeated whole-span replacement changes its EA without losing unrelated
 // reservations; forgetting exactly that span leaves only the atomic GET.
 t.dma={0x5600,0,0x10300,64,0};spu_write_channel(s,t,ls,21,0x40);
 t.dma[2]=0x10400;spu_write_channel(s,t,ls,21,0x40);
 assert(t.dma_writes.size()==2 && t.dma_writes[0].base==0x5600 && t.dma_writes[0].ea==0x10400);
 spu_ls_forget(t,0x5600,64);assert(t.dma_writes.size()==1 && t.dma_writes[0].base==0x5700);
'''
            test_spu_block.SpuBlockTests().native({0x100:0x40200000}, body)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['last_dma'], [dict(ls_base=0x5600,size=64,ea=0x10100)])
            for row in rows:
                self.assertEqual(row['ls_base'], 0x5600)
                self.assertEqual(row['size'], 64)
                self.assertEqual(hashlib.sha256(bytes.fromhex(row['data_hex'])).hexdigest(), row['sha256'])
            self.assertNotEqual(rows[0]['sha256'],rows[1]['sha256'])

    def test_dynamic_log_cell_cache_keeps_content_and_origin_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'dynamic.jsonl'
            body = 'setenv("ANYPS3_SPU_DYNAMIC_LOG",'+json.dumps(str(log))+',1);'
            body += r'''
 t.id=0x100;
 for(unsigned i=0;i<64;++i){mem.store(0x10100+i,1,0);mem.store(0x10200+i,1,0);}
 // il r3,7 at both EAs; the second copy differs only by its source EA.
 mem.store(0x10100,4,0x40800383);mem.store(0x10200,4,0x40800383);
 t.dma={0x5600,0,0x10100,64,0};spu_write_channel(s,t,ls,21,0x40);
 for(int i=0;i<3;++i){t.pc=0x5600;spu_step(s,t,ls);assert(t.pc==0x5604 && spu_word(t.gpr[3],0)==7);}
 // Same bytes from another EA: provenance changed, one new record.
 t.dma={0x5600,0,0x10200,64,0};spu_write_channel(s,t,ls,21,0x40);
 t.pc=0x5600;spu_step(s,t,ls);t.pc=0x5600;spu_step(s,t,ls);
 // Back to the first source: already logged, no third record.
 t.dma={0x5600,0,0x10100,64,0};spu_write_channel(s,t,ls,21,0x40);
 t.pc=0x5600;spu_step(s,t,ls);
 // A zero cell (STOP 0 word) is still logged on its first execution.
 t.pc=0x5640;try{spu_step(s,t,ls);}catch(const std::runtime_error&){}
'''
            test_spu_block.SpuBlockTests().native({0x100:0x40200000}, body)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([(r['ls_base'], [d['ea'] for d in r['last_dma']]) for r in rows],
                             [(0x5600, [0x10100]), (0x5600, [0x10200]), (0x5640, [])])
            self.assertEqual(rows[0]['sha256'], rows[1]['sha256'])
            self.assertEqual(rows[2]['data_hex'], '00'*64)


    def test_provenance_map_is_off_without_dynamic_log(self):
        # Provenance serves only ANYPS3_SPU_DYNAMIC_LOG; without it GET/STQ leave the map empty
        # (no per-STQ map maintenance) while the DMA data itself still arrives.
        body = r'''
 unsetenv("ANYPS3_SPU_DYNAMIC_LOG");t.id=0x100;
 for(unsigned i=0;i<64;++i)mem.store(0x10100+i,1,i);
 t.dma={0x5600,0,0x10100,64,0};spu_write_channel(s,t,ls,21,0x40);
 assert(t.dma_writes.empty() && ls[0x5600+63]==63);
 spu_ls_forget(t,0x5600,16);assert(t.dma_writes.empty());
'''
        test_spu_block.SpuBlockTests().native({0x100:0x40200000}, body)

    def test_provenance_split_and_merge_keep_sorted_disjoint_spans(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'dynamic.jsonl'
            body = 'setenv("ANYPS3_SPU_DYNAMIC_LOG",'+json.dumps(str(log))+',1);'
            body += r'''
 t.id=0x100;
 auto expect=[&](std::initializer_list<std::array<std::uint64_t,3>> rows){
   assert(t.dma_writes.size()==rows.size());std::size_t i=0;
   for(const auto& row:rows){const auto& w=t.dma_writes[i++];assert(w.base==row[0] && w.end==row[1] && w.ea==row[2]);}
 };
 spu_ls_write(t,0x1000,0x100,0x20000);spu_ls_write(t,0x3000,0x100,0x40000);spu_ls_write(t,0x2000,0x100,0x30000);
 expect({{0x1000,0x1100,0x20000},{0x2000,0x2100,0x30000},{0x3000,0x3100,0x40000}});
 // A write spanning the tail of one span, a gap and the head of the next.
 spu_ls_write(t,0x2080,0x1000,0x50000);
 expect({{0x1000,0x1100,0x20000},{0x2000,0x2080,0x30000},{0x2080,0x3080,0x50000},{0x3080,0x3100,0x40080}});
 // Forget the middle of one span: both remainders keep their EA offsets.
 spu_ls_write(t,0x2100,0x10,UINT64_MAX);
 expect({{0x1000,0x1100,0x20000},{0x2000,0x2080,0x30000},{0x2080,0x2100,0x50000},{0x2110,0x3080,0x50090},{0x3080,0x3100,0x40080}});
 // Forget outside every span and exact-span replacement.
 spu_ls_write(t,0x8000,0x10,UINT64_MAX);spu_ls_write(t,0x1000,0x100,0x60000);
 expect({{0x1000,0x1100,0x60000},{0x2000,0x2080,0x30000},{0x2080,0x2100,0x50000},{0x2110,0x3080,0x50090},{0x3080,0x3100,0x40080}});
 spu_ls_write(t,0x0,0x40000,UINT64_MAX);expect({});
'''
            test_spu_block.SpuBlockTests().native({0x100:0x40200000}, body)


if __name__ == '__main__':
    unittest.main()
