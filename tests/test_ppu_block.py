"""Independent native checks for page-local PPU blocks and resumable stops."""
from dataclasses import replace
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.program import compile_program
from anyps3.prx import make_image
from tests.test_anyps3 import BLR


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuBlockTests(unittest.TestCase):
    def native(self, translated, body, *, limit=20000, trace=False):
        source=translated.cpp(max_steps=limit,trace=trace).replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source+='\n#include <cassert>\nint main(){\n'+body+'\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'test.cpp').write_text(source)
            build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-o',str(root/'test')],capture_output=True,text=True,timeout=300)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            return run

    def test_middle_target_blocked_syscall_fault_preemption_and_exact_steps(self):
        # Canonical instruction words; no expectations computed from the lifter.
        code={
            0x10000:[0x38600001,0x2c030001,0x41820008,0x38600063,0x38630029,BLR],
            0x11000:[0x38c60001,0x3960005c,0x44000002,0x3860002a,BLR], # semaphore wait
            0x12000:[0x38600007,0x38800001,0x3960005e,0x44000002,0x48000000], # post then spin
            0x13000:[0x38600001,0x38630001,0x80600000,BLR],
            0x14000:[0x7c602028,0x48000004,0x7ca0212d,BLR], # lwarx; b; stwcx.; blr
            0x15000:[0x60000000]*70+[BLR],
            0x16000:[0x90a40000,0x48000000], # other PPU writes reserved word
            0x17000:[0x60000000,0x48000000], # other PPU leaves reservation alone
            0x18000:[0x38a0fffc,0x7ca803a6,BLR], # li r5,-4; mtlr r5; blr to a 64-bit target
        }
        segments=[(a,5,struct.pack('>'+str(len(w))+'I',*w)) for a,w in code.items()]
        segments.append((0x20000,6,struct.pack('>II',0x10000,0)+bytes(0x100)))
        translated=compile_program(make_image(segments,entry=0x20000),process_entry=True,extra_entries=tuple(code))
        source=translated.cpp()
        self.assertIn('/* 0x10010 */ goto ppu_10010;',source)
        self.assertNotIn('/* 0x13004 */',source) # ordinary instruction has no dispatch entry
        self.assertLess(translated.report()['ppu_dispatch_entries'],len(translated.operations))
        self.native(translated,r'''
 {PpuState s;s.pc=0x10000;anyps3_entry(s,false);assert(s.r[3]==42 && s.steps==5 && s.returned);}
 // Two instructions complete, the faulting third is counted and reports its own PC.
 {PpuState s;s.pc=0x13000;bool failed=false;try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("unmapped")!=std::string::npos;}
 assert(failed && s.pc==0x13008 && s.steps==3 && s.r[3]==2 && !s.returned);}
 // A blocked sc must retry at sc, without replaying the earlier increment.
 {PpuState s;s.pc=0x11000;s.r[3]=7;s.kernel.semaphores.push_back({7,0,1});
 PpuContext other;other.thread_id=2;other.pc=0x12000;s.ppu_threads={static_cast<PpuContext&>(s),other};
 anyps3_entry(s,false);assert(s.thread_id==1 && s.r[3]==42 && s.r[6]==1 && s.steps==4099 && s.kernel.semaphores[0].value==0);}
 // Preempt between lwarx and stwcx. An actual scheduled guest store invalidates
 // the token; a different scheduled context without a store preserves it.
 for(unsigned worker:{0x16000u,0x17000u}){PpuState s;s.pc=0x14000;s.steps=4095;
 s.memory.map(0x20000,0x1000,6,nullptr,0);s.r[4]=0x20000;s.r[5]=0x55;s.cr=0xabcdef01;s.xer=UINT64_C(1)<<31;
 s.memory.store(0x20000,4,0x12345678);PpuContext other;other.thread_id=2;other.pc=worker;other.r[4]=0x20000;other.r[5]=0x77;
 s.ppu_threads={static_cast<PpuContext&>(s),other};anyps3_entry(s,false);
 assert(s.steps==8194 && s.thread_id==1 && s.reservation_address==UINT64_MAX);
 assert(s.r[3]==0x12345678 && s.cr==(worker==0x16000?0x1bcdef01u:0x3bcdef01u));
 assert(s.memory.load(0x20000,4)==(worker==0x16000?0x77u:0x55u));}
 // PPUFAST: ordinary builds check the hard budget at block heads. A block
 // that would cross it is not entered; PC/steps are those of its head.
 {PpuState s;s.pc=0x15000;s.steps=19997;bool failed=false;try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what())=="execution step limit reached";}
 assert(failed && s.steps==19997 && s.pc==0x15000);}
 // A bclr to a target above 32 bits stops at the bclr, counted, with LR written by mtlr.
 {PpuState s;s.pc=0x18000;bool failed=false;try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what())=="branch target outside 32-bit space";}
 assert(failed && s.pc==0x18008 && s.steps==3 && s.lr==UINT64_C(0xfffffffffffffffc) && s.r[5]==UINT64_C(0xfffffffffffffffc));}
 // Inside the last 63 steps before the budget a block that fits still runs
 // (19950+3 <= 20000-3 at both heads) and returns: 5 steps, as from 0.
 {PpuState s;s.pc=0x10000;s.steps=19950;anyps3_entry(s,false);assert(s.r[3]==42 && s.steps==19955 && s.returned);}
 // Trace requests need a trace build; they are never silently ignored.
 {PpuState s;s.pc=0x10000;bool failed=false;try{anyps3_entry(s,true);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("trace build")!=std::string::npos;}
 assert(failed && s.steps==0);}
''')

    def test_head_bound_keeps_budget_slice_and_reschedule_exact(self):
        # Rule (independent of the head bound): with budget 64 a block of c
        # instructions entered at steps s runs iff s+c <= 64, else the run stops
        # at its head with steps s and PC of the head. Block A has 8 instructions
        # (li; addi x6; b B), block B 3 (addi; addi; blr).
        code={0x10000:[0x38600001]+[0x38630001]*6+[0x48000004], # ...; b +4 (0x1001c -> 0x10020)
              0x10020:[0x38630001,0x38630001,BLR]}
        segments=[(0x10000,5,struct.pack('>11I',*code[0x10000],*code[0x10020]))]
        segments.append((0x20000,6,struct.pack('>II',0x10000,0)+bytes(0x100)))
        translated=compile_program(make_image(segments,entry=0x20000),process_entry=True,extra_entries=(0x10000,0x10020))
        self.native(translated,r'''
 struct Case {std::uint64_t steps;bool returned;std::uint64_t end;std::uint32_t pc;std::uint64_t r3;};
 const Case cases[]={{40,true,51,0,9},{53,true,64,0,9},{54,false,62,0x10020,7},{55,false,63,0x10020,7},{56,false,64,0x10020,7},
                     {57,false,57,0x10000,0},{60,false,60,0x10000,0},{63,false,63,0x10000,0}};
 for(const auto& c:cases){PpuState s;s.pc=0x10000;s.steps=c.steps;bool stopped=false;
  try{anyps3_entry(s,false);}catch(const std::runtime_error& e){stopped=std::string(e.what())=="execution step limit reached";}
  std::fprintf(stderr,"steps=%llu returned=%d stopped=%d end=%llu pc=%x r3=%llu\n",(unsigned long long)c.steps,int(s.returned),int(stopped),(unsigned long long)s.steps,s.pc,(unsigned long long)s.r[3]);
  assert(s.returned==c.returned && stopped==!c.returned && s.steps==c.end && s.r[3]==c.r3 && (c.returned || s.pc==c.pc));}
 // Direct page calls with the head bound at the slice end: the slice cut and a
 // pending reschedule still stop at the very first head, nothing executed.
 {PpuState s;s.pc=0x10000;s.ppu_slice_end=s.ppu_head_end=8;s.reschedule=true;page_10(s);
  assert(s.steps==0 && s.pc==0x10000 && s.r[3]==0 && !s.returned);}
 {PpuState s;s.pc=0x10000;s.steps=5;s.ppu_slice_end=s.ppu_head_end=5;page_10(s);
  assert(s.steps==5 && s.pc==0x10000 && s.r[3]==0 && !s.returned);}
 // Slice end inside block A: A completes, B's head cuts (block-boundary slice).
 {PpuState s;s.pc=0x10000;s.ppu_slice_end=s.ppu_head_end=4;page_10(s);
  assert(s.steps==8 && s.pc==0x10020 && s.r[3]==7 && !s.returned);}
 // A head at or above its bound exits before the block, publishing its PC and
 // recording its instruction count (8 for A, 3 for B) for the budget decision.
 {PpuState s;s.pc=0x10000;s.ppu_head_end=0;page_10(s);
  assert(s.steps==0 && s.pc==0x10000 && s.r[3]==0 && s.ppu_head_exit==((UINT64_C(8)<<32)|0x10000));}
 {PpuState s;s.pc=0x10000;s.steps=20;s.ppu_head_end=21;page_10(s);
  assert(s.steps==28 && s.pc==0x10020 && s.r[3]==7 && s.ppu_head_exit==((UINT64_C(3)<<32)|0x10020));}
 // Without the anyps3_entry bounds (PpuCpu defaults) a direct call runs through.
 {PpuState s;s.pc=0x10000;page_10(s);assert(s.steps==11 && s.r[3]==9 && s.returned);}
''',limit=64)

    def test_verified_jump_table_dispatches_all_targets(self):
        from tests.test_jump_tables import image
        original=image();segments=[]
        for segment in original.segments:
            payload=original.read_virtual(segment.virtual_address,segment.memory_size)
            if segment.virtual_address==0x20000:
                payload=bytearray(payload);struct.pack_into('>II',payload,0x10,0x10000,0x28000);payload=bytes(payload)
            segments.append((segment.virtual_address,segment.flags,payload))
        translated=compile_program(make_image(segments,entry=0x20010),process_entry=True)
        self.assertEqual(translated.jump_tables[0]['targets'],[0x10034,0x10038,0x1003c])
        # Map the exact fixture bytes, including signed relative table words.
        mapping=''
        for address,flags,payload in segments:
            mapping+='{const std::uint8_t bytes[]={'+','.join(map(str,payload))+'};s.memory.map('+str(address)+','+str(len(payload))+','+str(flags)+',bytes,sizeof(bytes));}\n'
        self.native(translated,'for(unsigned index=0;index<4;++index){PpuState s;'+mapping+r'''
 s.pc=0x10000;s.r[2]=0x28000;s.r[3]=index;anyps3_entry(s,false);
 assert(s.returned && s.r[3]==(index==0?0x10034u:index==1?0x10038u:index==2?0x1003cu:3u));
 assert(s.steps==(index<3?11u:4u));}
''')

    def test_trace_keeps_every_instruction_pc_and_registers(self):
        from anyps3.elf import ElfImage
        from tests.test_anyps3 import program
        translated=compile_program(ElfImage.parse(program(0x38600001,0x38630029,BLR)))
        run=self.native(translated,'{PpuState s;s.pc=0x10000;anyps3_entry(s,true);assert(s.steps==3 && s.r[3]==42);}'
            # Trace builds keep the exact per-instruction budget cut.
            '{PpuState s;s.pc=0x10000;s.steps=19998;bool failed=false;try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what())=="execution step limit reached";}'
            'assert(failed && s.steps==20000 && s.pc==0x10008 && s.r[3]==42);}',trace=True)
        lines=[line for line in run.stderr.splitlines() if line.startswith('trace ')]
        self.assertEqual(len(lines),3)
        self.assertIn('pc=0x00010000',lines[0]);self.assertIn('r3=0x0000000000000000',lines[0])
        self.assertIn('pc=0x00010004',lines[1]);self.assertIn('r3=0x0000000000000001',lines[1])
        self.assertIn('pc=0x00010008',lines[2]);self.assertIn('r3=0x000000000000002a',lines[2])
