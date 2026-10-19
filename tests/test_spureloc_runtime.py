"""Relocation transport checked against fixed results and the shared interpreter."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from tests import test_spu_block


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuRelocationTests(unittest.TestCase):
    def test_entry_counters_include_single_stop_and_blocked_retry(self):
        test_spu_block.SpuBlockTests().native({0x100:1},r'''
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-i*8);};
 put(0x20000,1);t.pc=0x20000;
 assert(spu_run(s,100) && t.steps==1 && t.pc==0x20004);
 assert(t.relocated_entries.value.load()==1 && !t.interpreted_instructions.value.load());
 put(0x20004,0x40800383);spu_step(s,t,ls);
 assert(t.steps==2 && t.interpreted_instructions.value.load()==1 && spu_word(t.gpr[3],0)==7);
 put(0x20008,0x02000000);fault([&]{spu_step(s,t,ls);});
 assert(t.steps==2 && t.interpreted_instructions.value.load()==1);
 put(0x20100,0x01a00e84);t.pc=0x20100;
 assert(!spu_run(s,1) && t.steps==2 && t.relocated_entries.value.load()==2);
 assert(!spu_run(s,1) && t.steps==2 && t.relocated_entries.value.load()==3);
 t.in_mailbox={0xdeadbeef};assert(spu_run(s,1) && t.steps==3 && t.pc==0x20104);
 assert(spu_word(t.gpr[4],0)==0xdeadbeef && t.relocated_entries.value.load()==4);
 t.pc=0x20000;t.block_budget=1;t.block_limit=UINT64_MAX;
 try{spu_interpret(s,t,ls);assert(false);}catch(const SpuSliceEnd&){++t.steps;}
 assert(t.steps==4 && t.interpreted_instructions.value.load()==2);
''',((0x600,bytes.fromhex('01a00e84')),))

    def test_private_run_chain_prefix_fault_trace_and_wrapped_mapping(self):
        words={0x100+i*4:0x1c004183 for i in range(64)}
        words[0x200]=0x327fe000  # BR -256
        test_spu_block.SpuBlockTests().native(words,r'''
 const auto code=std::vector<std::uint8_t>(ls+0x100,ls+0x204);
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-i*8);};
 for(const unsigned base:{0x10680u,0x80u,0x3ffc0u}){
  for(unsigned i=0;i<code.size();++i)ls[(base+i)&0x3ffff]=code[i];
  t.steps=0;t.gpr={};t.pc=base;t.reloc_delta=0;
  // Entry offset and limit interrupt a sequence of four private runs.
  spu_step(s,t,ls,49,49);assert(t.steps==49 && t.pc==((base+196)&0x3fffc));
  assert(spu_word(t.gpr[3],0)==49);
  spu_step(s,t,ls,4,53,true);assert(t.steps==53 && t.pc==((base+212)&0x3fffc));
  // No previous memcmp survives the replacement, even in a resumed run.
  put((base+212)&0x3fffc,0x02000000);
  fault([&]{spu_step(s,t,ls,100);});assert(t.steps==53 && t.pc==((base+212)&0x3fffc));
  put((base+212)&0x3fffc,0x1c004183);t.pc=base;t.steps=0;t.gpr={};
  while(t.steps<65)spu_step(s,t,ls,65-unsigned(t.steps));
  assert(t.steps==65 && t.pc==base && spu_word(t.gpr[3],0)==64);
 }
''')

    def test_pic_links_pc_relative_ls_ila_channels_and_overlapping_bases(self):
        # Fixed IL, ILA, LQR, BRSL, STQR, WRCH, STOP, NOP, AI, BI.
        # The call crosses the original 4 KiB function boundary.
        words={0xff0:0x40800383,0xff4:0x4291a288,0xff8:0x33801106,
               0xffc:0x33000285,0x1000:0x23801806,0x1004:0x21a00e03,
               0x1008:1,0x100c:0x40200000,0x1010:0x1c00c183,0x1014:0x35000280}
        test_spu_block.SpuBlockTests().native(words,r'''
 const auto code=std::vector<std::uint8_t>(ls+0xff0,ls+0x1018);
 for(const unsigned base:{0xff0u,0x10680u,0x5680u,0x3ffb0u,0x104u}){
  std::fill(ls,ls+0x40000,0);std::copy(code.begin(),code.end(),ls+base);
  const unsigned input=(base+0x90)&0x3fff0,output=(base+0xd0)&0x3fff0;
  for(unsigned i=0;i<16;++i)ls[input+i]=0xa0+i;
  t.gpr={};t.pc=base;t.steps=0;t.reloc_delta=0;t.out_mailbox_full=false;t.interrupts_enabled=false;
  t.relocated_entries=0;t.interpreted_instructions=0;
  auto oracle=t;auto reference=group_ref.context;bool done=false,oracle_done=false;
  for(unsigned slice=0;!done && slice<30;++slice){
   // Budgets 1, 41, 81: exact small slices interpret, larger ones enter relocated superblocks.
   try{spu_step(s,t,ls,(slice%3)*40+1);}catch(const SpuSliceEnd&){++t.steps;done=true;}
   while(oracle.steps<t.steps){
    oracle.block_budget=1;oracle.block_limit=UINT64_MAX;
    try{spu_interpret(s,oracle,reference.data());}catch(const SpuSliceEnd&){++oracle.steps;oracle_done=true;}
   }
   assert(t.pc==oracle.pc && t.gpr==oracle.gpr && t.fpscr==oracle.fpscr);
   assert(t.out_mailbox_full==oracle.out_mailbox_full && t.out_mailbox==oracle.out_mailbox);
   assert(group_ref.context==reference);
  }
  assert(done && oracle_done && t.steps==9 && t.pc==((base+0x1c)&0x3fffc));
  assert(spu_word(t.gpr[3],0)==10 && spu_word(t.gpr[5],0)==((base+0x10)&0x3fffc));
  assert(spu_word(t.gpr[8],0)==0x12345 && t.out_mailbox_full && t.out_mailbox==10);
  for(unsigned i=0;i<16;++i){assert(t.gpr[6][i]==0xa0+i);assert(ls[output+i]==0xa0+i);}
  assert(base==0xff0 || t.relocated_entries.value.load()>0);
 }
''',((0x5680,bytes.fromhex('40800183408001834080018340800183')),))

    def test_absolute_brasl_and_indirect_bisl_keep_absolute_targets(self):
        test_spu_block.SpuBlockTests().native(
            {0x100:0x31008005,0x104:1,0x400:0x35200386,0x500:0x35000280},r'''
 std::copy_n(ls+0x100,8,ls+0x10680);t.pc=0x10680;spu_word(t.gpr[7],0,0x500);
 assert(spu_run(s,100) && t.steps==4 && t.pc==0x10688);
 assert(spu_word(t.gpr[5],0)==0x10684 && spu_word(t.gpr[6],0)==0x404);
 assert(t.relocated_entries.value.load()>0);
''')

    def test_exact_limits_wraparound_interrupts_and_unsupported_stop(self):
        test_spu_block.SpuBlockTests().native(
            {0x100:0x1c004183,0x104:0x327fff80,0x108:0},r'''
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-i*8);};
 std::copy_n(ls+0x100,12,ls+0x20000);t.pc=0x20000;
 spu_step(s,t,ls,7,5);assert(t.steps==5 && t.pc==0x20004 && spu_word(t.gpr[3],0)==3);
 fault([&]{spu_step(s,t,ls,1,5);});assert(t.steps==5 && t.pc==0x20004);
 spu_step(s,t,ls,1,6);assert(t.steps==6 && t.pc==0x20000);
 put(0,0x35400000);t.event_mask=t.event_status=0x10;t.event_ready=true;t.interrupts_enabled=true;
 spu_step(s,t,ls);assert(t.steps==7 && t.pc==0x20000 && !t.interrupts_enabled);
 spu_step(s,t,ls);assert(t.steps==8 && t.pc==0x20004 && spu_word(t.gpr[3],0)==4);
 t.pc=0x20008;fault([&]{spu_step(s,t,ls,8);});assert(t.steps==8 && t.pc==0x20008);
 put(0x3fffc,0x1c004183);t.pc=0x3fffc;t.steps=UINT64_MAX-1;
 spu_step(s,t,ls,3,UINT64_MAX);assert(t.steps==UINT64_MAX && t.pc==0);
 fault([&]{spu_step(s,t,ls,1);});assert(t.steps==UINT64_MAX && t.pc==0);
 t.steps=0;t.pc=0x40000;fault([&]{spu_step(s,t,ls);});assert(!t.steps);
 t.pc=0x20001;fault([&]{spu_step(s,t,ls);});assert(!t.steps);
''')

    def test_self_modification_store_dma_and_blocking_channel_revalidate(self):
        words={0x100:0x1c004183,0x104:0x23800185,0x108:0x1c004183,
               0x10c:0x40200000,0x110:0x1c004204,0x114:0x01a00e86,
               0x118:1,0x11c:0x40200000}
        test_spu_block.SpuBlockTests().native(words,r'''
 std::copy_n(ls+0x100,32,ls+0x10680);t.pc=0x10680;
 const std::uint8_t patch[]={0x1c,0x01,0xc2,4,0x01,0xa0,0x0e,0x86,0,0,0,1,0x40,0x20,0,0};
 std::copy_n(patch,16,t.gpr[5].begin());
 assert(spu_run(s,100) && t.steps==5 && t.pc==0x10694);
 assert(spu_word(t.gpr[3],0)==2 && spu_word(t.gpr[4],0)==7);
 assert(!spu_run(s,100) && t.steps==5);
 t.in_mailbox={0xdeadbeef};assert(spu_run(s,100) && t.steps==7 && t.pc==0x1069c);
 assert(spu_word(t.gpr[6],0)==0xdeadbeef && t.interpreted_instructions.value.load()>0);
 mem.store(0x10100,4,0x1c024183);mem.store(0x10104,4,0x02000000);
 for(unsigned i=8;i<16;++i)mem.store(0x10100+i,1,0);
 t.dma={0x10680,0,0x10100,16,0};spu_write_channel(s,t,ls,21,0x40);t.pc=0x10680;
 fault([&]{spu_run(s,100);});assert(t.steps==8 && t.pc==0x10684 && spu_word(t.gpr[3],0)==11);
''')

    def test_candidate_full_bytes_ambiguity_logging_and_perf_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            log=Path(directory)/'dynamic.jsonl'
            body='setenv("ANYPS3_SPU_DYNAMIC_LOG",'+json.dumps(str(log))+',1);'+r'''
 const std::uint8_t code[]={0x1c,0,0x41,0x83,0x1c,1,0xc2,4,0x40,0x20,0,0,0,0,0,1};
 std::copy_n(code,16,ls+0x20000);t.pc=0x20000;std::uint32_t original=0;
 assert(spu_reloc_find(t,ls,original) && original==0x600);
 assert(spu_run(s,100) && t.steps==4 && t.pc==0x20010);
 assert(spu_word(t.gpr[3],0)==1 && spu_word(t.gpr[4],0)==7);
 assert(t.relocated_entries.value.load()>0 && !t.interpreted_instructions.value.load());
 ls[0x20007]=5;t.pc=0x20000;t.reloc_delta=0;
 assert(!spu_reloc_find(t,ls,original));spu_step(s,t,ls);
 assert(t.steps==5 && t.interpreted_instructions.value.load()==1 && spu_word(t.gpr[3],0)==2);
 spu_dynamic_perf_report(s);
'''
            test_spu_block.SpuBlockTests().native(
                {0x100:0x1c004183,0x104:0x1c008204,0x108:0x40200000,0x10c:1},body,
                ((0x600,bytes.fromhex('1c0041831c01c2044020000000000001')),))
            rows=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([r['execution'] for r in rows],['relocated','interpreted'])
            self.assertNotEqual(rows[0]['sha256'],rows[1]['sha256'])


if __name__=='__main__':unittest.main()
