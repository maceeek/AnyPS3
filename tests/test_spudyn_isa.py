"""Run existing independent ISA oracles through runtime operands as well as AOT.

The fixtures keep their fixed constants, Python big integers and Fraction
oracles. Patching changes only the transport of an encoded word, not expected
results. Separate differential coverage exercises every allocated opcode row.
"""
import random
import shutil
import unittest
from unittest.mock import patch

from anyps3.spu import lift
from anyps3.errors import AnyPS3Error
from tests import test_runtime_objects, test_spu, test_spu_float, test_spu_perf
from tests import test_spuperf2_simd, test_spu_gaps


def invoke(word):
    return f'dyn(UINT32_C({word}));'


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuDynamicIsaTests(unittest.TestCase):
    def test_independent_integer_fp_shuffle_and_control_oracles(self):
        bodies = []
        setup = r'''
 {NativeSpu::Thread t;std::vector<std::uint8_t> scratch(0x40000);
 [[maybe_unused]] auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};
 [[maybe_unused]] auto dyn=[&](std::uint32_t word){
  t.pc=0x100;t.block_budget=1;t.block_limit=UINT64_MAX;t.block_trace=false;
  for(unsigned j=0;j<4;++j)scratch[0x100+j]=word>>(24-j*8);
  spu_interpret(s,t,scratch.data());
 };
'''
        def collect(body):
            body=body.replace('const auto result=spu_single_vector(flags,a,b,c,cases[start].kind);', r'''
 t.gpr[1]=a;t.gpr[2]=b;t.gpr[3]=c;t.fpscr=flags;
 const auto kind=cases[start].kind;
 const std::uint32_t opcodes[]={0x58800000,0x58a00000,0x58c00000,0xe0800000,0xf0800000,0xd0800000};
 dyn(opcodes[kind]|(2u<<14)|(1u<<7)|(kind<3?4u:3u));
 const auto result=t.gpr[4];flags=t.fpscr;
''')
            # Some integer fixtures already introduce their Thread.
            bodies.append(setup+body.replace('NativeSpu::Thread t;', '')+'}\n')
        integer = test_spu.SpuTests()
        integer.run_native = collect
        names = [name for name in dir(integer) if name.startswith('test_') and any(
            token in name for token in ('quadword_shifts', 'byte_sums', 'unsigned_halfword_multiply',
                'integer_multiply_variants', 'halfword_rotates', 'halfword_arithmetic',
                'fpscr_read_write', 'word_rotates', 'integer_aliases_shuffle'))]
        with patch.object(test_spu, 'instruction', invoke):
            for name in names:
                getattr(integer, name)()
        floating = test_spu_float.SpuFloatTests()
        floating.run_native = collect
        names = [name for name in dir(floating) if name.startswith('test_') and not any(
            token in name for token in ('conditional_halts', 'rchcnt', 'frest_frsqest'))]
        with patch.object(test_spu_float, 'instruction', invoke):
            for name in names:
                getattr(floating, name)()
            channels=test_spu_float.SpuEstimateAndChannelCountTests()
            channels.run_native=collect
            channels.test_frest_frsqest_with_fi_reach_twelve_bit_accuracy()
            channels.test_rchcnt_reports_modeled_channel_state_and_stops_on_unmodeled()
        control=test_spu_gaps.SpuGapNativeTests()
        control.native=collect
        with patch.object(test_spu_gaps,'instruction',invoke):
            for name in ('test_cell_has_no_sprs_all_reads_zero_and_writes_leave_state_unchanged',
                         'test_bisled_link_alias_count_and_branch_interrupt_flags',
                         'test_conditional_branch_flags_change_only_when_taken',
                         'test_srr0_channels_require_sync_c_and_iret_ignores_ra'):
                getattr(control,name)()
        simd = test_spuperf2_simd.SpuPerf2SimdTests()
        simd.native = collect
        def runtime_lift(pc, word):
            return '(void)g;(void)out;'+invoke(word)
        with patch.object(test_spuperf2_simd, 'lift', runtime_lift):
            for name in dir(simd):
                if name.startswith('test_'):
                    getattr(simd, name)()
        # Exact rational random corpus, plus host FP environment isolation.
        perf = test_spu_perf.SpuPerfFloatTests()
        with patch.object(test_spu_float.SpuFloatTests, 'run_native', lambda self, body: collect(body)), \
             patch.object(test_spu_perf, 'lift', runtime_lift):
            for name in ('test_rational_corpus_all_six_operations_and_fpscr',
                         'test_fixed_overflow_underflow_extended_and_per_lane_fpscr',
                         'test_mxcsr_isolation_rz_ftz_daz_sticky_flags_and_aliases'):
                getattr(perf, name)()
        self.assertGreaterEqual(len(bodies), 28)
        test_runtime_objects.RuntimeObjectsTests().run_native('(void)call;'+''.join(bodies))

    def test_all_opcode_rows_random_fields_pc_and_static_equivalence(self):
        rng = random.Random(0x53505544594e)
        cases = []
        # Avoid externally blocking operations; those have scheduler tests.
        for op in range(2048):
            if op in (0, 0xd, 0xf, 0x10d, 0x140, 2, 3, 0x1aa, 0x1ab):
                continue
            for low in (0, 0x1fffff, rng.getrandbits(21)):
                word = op << 21 | low
                pc = rng.choice((0, 0x100, 0x1234, 0x3fffc))
                try:
                    body = lift(pc, word)
                except AnyPS3Error:
                    continue
                cases.append((pc, word, body))
        self.assertGreater(len(cases), 3000)
        # Each case compares all registers, FPSCR, LS, PC and exceptions.
        body = r'''
#pragma GCC diagnostic ignored "-Wtype-limits"
 (void)call;
 auto compare=[&](std::uint32_t pc,std::uint32_t word,auto execute){
  NativeSpu::Thread initial;std::vector<std::uint8_t> original(0x40000);
  std::uint32_t seed=word^pc;
  for(auto& r:initial.gpr)for(auto& v:r){seed=seed*1664525u+1013904223u;v=seed>>24;}
  for(unsigned i=0;i<original.size();++i)original[i]=i*17+3;
  for(unsigned i=0;i<4;++i)original[pc+i]=word>>(24-8*i);
  initial.pc=pc;initial.srr0=0x120;initial.srr0_valid=true;
  auto expected=initial,actual=initial;auto ls1=original,ls2=original;
  bool fault1=false,fault2=false;
  try{execute(expected,ls1.data());}catch(const std::runtime_error&){fault1=true;}
  try{spu_interpret(s,actual,ls2.data());}catch(const std::runtime_error&){fault2=true;}
  if(fault1!=fault2 || expected.gpr!=actual.gpr || expected.fpscr!=actual.fpscr || expected.pc!=actual.pc || ls1!=ls2 ||
     expected.interrupts_enabled!=actual.interrupts_enabled || actual.steps!=unsigned(!fault2)){
    std::fprintf(stderr,"DIFF pc=%05x word=%08x expected_pc=%05x actual_pc=%05x\n",pc,word,expected.pc,actual.pc);std::abort();
  }
 };
'''
        for pc, word, semantic in cases:
            body += (f'compare({pc},UINT32_C({word}),[&](NativeSpu::Thread& t,std::uint8_t* ls){{'
                     'auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};(void)g;(void)ls;'
                     + semantic+'});\n')
        test_runtime_objects.RuntimeObjectsTests().run_native(body)


if __name__ == '__main__':
    unittest.main()
