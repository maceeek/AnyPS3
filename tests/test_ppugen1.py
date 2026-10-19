"""Independent native boundary cases for pure-register batch retirement."""
from dataclasses import replace
import unittest

from anyps3.flow import Operation, lift
from anyps3.prx import make_image
from anyps3.program import compile_program
from tests.test_anyps3 import BLR
from tests import test_ppu_block
import struct


class PpuGenerationBatchTests(unittest.TestCase):
    def test_retirement_boundaries_faults_blocking_and_context_observation(self):
        code = {
            0x10000: [0x38600001, 0x38630002, 0x38630003, 0x68630001,
                      0x60630008, 0x38630001, 0x38630010, 0x68630001, BLR],
            0x11000: [0x38600001, 0x38630001, 0x39000000, 0x39200000, 0x39400000, 0x39800000, 0x80600000, BLR],
            0x12000: [0x38600001, 0x38630001, 0x39000000, 0x39200000, 0x39400000, 0x39800000, 0x80640000, BLR],
            0x13000: [0x38600001, 0x38630001, 0x38800003, 0x48000004, BLR],
            0x14000: [0x38600001, 0x38630001, 0x39000000, 0x39200000, 0x39400000, 0x39800000, 0x90a40000, BLR],
            # Safe update with RA=RT, record, compare and branch stay ordered.
            0x15000: [0x3860ffff, 0x38630001, 0x70630000, 0x2c030000,
                      0x41820008, 0x38600063, BLR],
            0x16000: [0x80640000, 0x80a40004, 0x90640008, 0x80c40008, BLR],
        }
        segments = [(a, 5, struct.pack('>' + str(len(words)) + 'I', *words))
                    for a, words in code.items()]
        segments.append((0x20000, 6, struct.pack('>II', 0x10000, 0) + bytes(0x100)))
        p = compile_program(make_image(segments, entry=0x20000),
                            process_entry=True, extra_entries=tuple(code))
        # A test-only observable operation exercises a mid-block PpuBlocked.
        # The production lifter does not emit it; unknown operations default
        # to a checkpoint rather than being declared safe by omission.
        operations = tuple(replace(o, mnemonic='test blocked', cpp='throw PpuBlocked{};')
                           if o.address == 0x12018 else o for o in p.operations)
        p = replace(p, operations=operations)
        # PPUFAST: exact mid-block budget cuts and per-instruction trace are
        # properties of trace builds; ordinary builds cut at block heads
        # (tests/test_ppu_block.py). Everything else here holds in both.
        run = test_ppu_block.PpuBlockTests().native(p, r'''
 const std::uint64_t values[]={0,1,3,6,7,15,16,32,33};
 // Every possible prefix of a eight-instruction register prefix, including zero.
 for(unsigned remaining=0;remaining<=8;++remaining){
  PpuState s;s.pc=0x10000;s.steps=64-remaining;s.ppu_slice_end=UINT64_MAX;
  bool stopped=false;try{page_10(s);}catch(const std::runtime_error& e){
   stopped=std::string(e.what())=="execution step limit reached";}
  assert(stopped && s.steps==64 && s.pc==0x10000+4*remaining);
  assert(s.r[3]==values[remaining] && !s.returned);
 }
 {PpuState s;s.pc=0x10000;s.steps=55;s.ppu_slice_end=UINT64_MAX;page_10(s);
  assert(s.steps==64 && s.r[3]==33 && s.returned && s.pc==RETURN_SENTINEL);}
 {PpuState s;s.pc=0x11000;s.steps=11;s.ppu_slice_end=UINT64_MAX;
  bool stopped=false;try{page_11(s);}catch(const std::runtime_error& e){
   stopped=std::string(e.what()).find("unmapped")!=std::string::npos;}
  assert(stopped && s.steps==18 && s.pc==0x11018 && s.r[3]==2);}
 {PpuState s;s.pc=0x12000;s.steps=11;s.ppu_slice_end=UINT64_MAX;
  bool blocked=false;try{page_12(s);}catch(const PpuBlocked&){blocked=true;}
  assert(blocked && s.steps==18 && s.pc==0x12018 && s.r[3]==2);}
 // Slice end and reschedule remain block-boundary checks. Baseline permits
 // this straight-line block to finish before the next dispatch head.
 {PpuState s;s.pc=0x13000;s.ppu_slice_end=2;page_13(s);
  assert(s.steps==4 && s.pc==0x13010 && s.r[3]==2 && s.r[4]==3 && !s.returned);}
 {PpuState s;s.pc=0x13000;s.ppu_slice_end=100;s.reschedule=true;page_13(s);
  assert(s.steps==0 && s.pc==0x13000 && s.r[3]==0);}
 {PpuState s;s.pc=0x13000;s.steps=2;s.ppu_slice_end=2;page_13(s);
  assert(s.steps==2 && s.pc==0x13000 && s.r[3]==0);}
 // Memory observation sees the store's own PC/count and BE bytes, never a
 // speculative count including the following return instruction.
 {PpuState s;s.pc=0x14000;s.ppu_slice_end=UINT64_MAX;s.r[4]=0x40000;s.r[5]=0x12345678;
  s.memory.map(0x40000,4096,6,nullptr,0);unsigned seen=0;
  s.memory.host_access_observer=[&](const void*,std::size_t,bool write){
   if(write){++seen;assert(s.pc==0x14018 && s.steps==7 && s.r[3]==2);}};
  page_14(s);assert(seen && s.steps==8 && s.returned);
  const auto* bytes=s.memory.access(0x40000,4,false);
  assert(bytes[0]==0x12 && bytes[1]==0x34 && bytes[2]==0x56 && bytes[3]==0x78);}
 {PpuState s;s.pc=0x15000;s.ppu_slice_end=UINT64_MAX;s.xer=UINT64_C(1)<<31;
  page_15(s);assert(s.returned && s.steps==6 && s.r[3]==0 && s.cr==0x30000000);}
 // An observer-heavy block retains precise per-instruction retirement.
 {PpuState s;s.pc=0x16000;s.ppu_slice_end=UINT64_MAX;s.steps=62;s.r[4]=0x40000;
  s.memory.map(0x40000,4096,6,nullptr,0);s.memory.store(0x40000,4,0x12345678);s.memory.store(0x40004,4,0xaabbccdd);
  unsigned seen=0;s.memory.host_access_observer=[&](const void*,std::size_t,bool){
   ++seen;assert(s.steps==63 || s.steps==64);assert(s.pc==0x16000+4*(s.steps-63));};
  bool stopped=false;try{page_16(s);}catch(const std::runtime_error& e){stopped=std::string(e.what())=="execution step limit reached";}
  s.memory.host_access_observer={};
  assert(stopped && seen && s.steps==64 && s.pc==0x16008 && s.r[3]==0x12345678 && s.r[5]==0xaabbccdd);
  assert(s.memory.load(0x40008,4)==0);}
 // Trace at a partial hard limit prints just the executed instructions.
 {PpuState s;s.pc=0x10000;s.steps=61;s.ppu_slice_end=UINT64_MAX;s.ppu_trace=true;
  bool stopped=false;try{page_10(s);}catch(const std::runtime_error&){stopped=true;}
  assert(stopped && s.steps==64 && s.pc==0x1000c && s.r[3]==6);}
''', limit=64, trace=True)
        lines = [line for line in run.stderr.splitlines() if line.startswith('trace ')]
        self.assertEqual(len(lines), 3)
        self.assertIn('pc=0x00010000', lines[0])
        self.assertIn('r3=0x0000000000000000', lines[0])
        self.assertIn('pc=0x00010004', lines[1])
        self.assertIn('r3=0x0000000000000001', lines[1])
        self.assertIn('pc=0x00010008', lines[2])
        self.assertIn('r3=0x0000000000000003', lines[2])

    def test_unknown_and_observable_instructions_are_not_batchable(self):
        for word in (0x80600000, 0x7c602028, 0xfc61102a, BLR, 0x48000004, 0x7c6c42e6):
            with self.subTest(word=hex(word)):
                self.assertIsNone(lift(word, 0x10000).batch_body())
        unknown = Operation(0x10000, 0, 'future helper',
                            'observe(state); state.pc = 0x10004;', (0x10004,))
        self.assertIsNone(unknown.batch_body())
        self.assertIsNone(replace(lift(0x38600001, 0x10000),
                                  cpp='new_effect(state);').batch_body())
        self.assertIsNone(replace(lift(0x38600001, 0x10000),
                                  cpp='new_effect(state); state.pc = 0x10004;').batch_body())
