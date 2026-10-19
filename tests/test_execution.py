"""Native serial execution contract, using fixed times and observable side effects."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.execution import TYPES, CPP, adapt_engines, instrument_gpu_waits


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class ExecutionTests(unittest.TestCase):
    def native(self, source):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                                    str(root/'test.cpp'), '-o', str(root/'test')],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            return run

    def test_exact_exclusive_nanoseconds_and_inline_request_contract(self):
        self.native(r'''
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <map>
#include <optional>
#include <string>
#include <stdexcept>
#include <cassert>
''' + TYPES + r'''
struct PpuState {
    ExecutionTiming execution; ExecutionSupervisor supervisor;
    std::uint32_t pc=0x12345678,thread_id=7;
    unsigned seen=0,budget=0;
};
''' + CPP + r'''
static bool spu_run_inline(PpuState& s,unsigned budget){s.seen=s.seen*10+1;s.budget=budget;return budget==42;}
static bool rsx_run_inline(PpuState& s,unsigned budget){s.seen=s.seen*10+2;if(budget==99)throw std::runtime_error("exact engine fault");return false;}
static void audio_run_inline(PpuState& s){s.seen=s.seen*10+3;}
int main(){
    using Phase=ExecutionPhase;
    ExecutionTiming timing;timing.enabled=true;
    const auto at=[](unsigned ns){return ExecutionTiming::Clock::time_point(std::chrono::nanoseconds(ns));};
    timing.transition(Phase::Ppu,at(0));timing.transition(Phase::Spu,at(10));
    timing.transition(Phase::Rsx,at(40));timing.transition(Phase::GpuWait,at(100));
    timing.transition(Phase::Rsx,at(105));timing.transition(Phase::Ppu,at(120));
    const auto sample=timing.snapshot(at(150));
    assert(sample[0]==40 && sample[1]==30 && sample[2]==75 && sample[6]==5);
    std::uint64_t total=0;for(auto ns:sample)total+=ns;assert(total==150);
    assert(timing.ns[0]==10); // snapshot does not commit or double count an open phase
    timing.transition(Phase::Count,at(150));assert(timing.ns==sample);

    PpuState s;s.execution.enabled=false;
    auto result=engine_step(s,{ExecutionEngine::Spu,42});
    assert(result.completed && result.progress && s.seen==1 && s.budget==42);
    result=engine_step(s,{ExecutionEngine::Rsx,17});assert(result.completed && !result.progress && s.seen==12);
    result=engine_step(s,{ExecutionEngine::Audio,0});assert(result.completed && !result.progress && s.seen==123);
    assert(s.supervisor.sequence==3 && !s.supervisor.first_error && !execution_timing);
    for(auto ns:s.execution.ns)assert(ns==0);
    bool stopped=false;try{rsx_run(s,99);}catch(const std::runtime_error& e){stopped=std::string(e.what())=="exact engine fault";}
    assert(stopped && s.supervisor.failed_sequence==4 && s.supervisor.failed_pc==0x12345678 && s.supervisor.failed_thread==7);
    const auto first=s.supervisor.first_error;
    try{engine_step(s,{static_cast<ExecutionEngine>(99),0});assert(false);}catch(const std::runtime_error& e){assert(std::string(e.what())=="unknown execution engine");}
    assert(s.supervisor.first_error==first && s.supervisor.failed_sequence==4);
    try{std::rethrow_exception(first);}catch(const std::runtime_error& e){assert(std::string(e.what())=="exact engine fault");}
    s.execution.enabled=true;
    {ExecutionScope ppu(s.execution,Phase::Ppu);
      assert(execution_timing==&s.execution);
      {ExecutionScope flip(s.execution,Phase::FlipWait);assert(s.execution.phase==Phase::FlipWait);
       (void)spu_run(s,0);assert(s.execution.phase==Phase::FlipWait);}
      assert(s.execution.phase==Phase::Ppu);
      const auto wait=[](unsigned value){assert(execution_timing->phase==Phase::GpuWait);return value+1;};
      assert(execution_gpu_wait(wait,41)==42 && s.execution.phase==Phase::Ppu);
      try{rsx_run(s,99);}catch(const std::runtime_error&){}assert(s.execution.phase==Phase::Ppu);
      ExecutionTiming other;other.enabled=true;
      {ExecutionScope nested(other,Phase::Audio);assert(s.execution.phase==Phase::Count && execution_timing==&other);}
      assert(s.execution.phase==Phase::Ppu && execution_timing==&s.execution);
    }
    assert(!execution_timing && s.execution.phase==Phase::Count);
}
''')

    def test_process_facades_share_services_but_keep_registers_and_contexts(self):
        from tests.test_ppu_block import PpuBlockTests
        from anyps3.program import compile_program
        from anyps3.elf import ElfImage
        from tests.test_anyps3 import BLR, program
        translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
        PpuBlockTests().native(translated, r'''
 ProcessState owner;PpuState a(owner),b(owner);
 assert(a.thread_atexit==b.thread_atexit && a.thread_atexit==owner.thread_atexit);
 assert(&a.memory==&b.memory && &a.rsx==&b.rsx && &a.spu==&b.spu && &a.kernel==&b.kernel);
 a.memory.map(0x20000,0x1000,6,nullptr,0);a.memory.store(0x20000,4,0x12345678);
 assert(b.memory.load(0x20000,4)==0x12345678 && b.memory.access(0x20000,4,false)[0]==0x12);
 a.r[3]=41;b.r[3]=99;a.pc=0x10000;b.pc=0x10004;
 a.ppu_slice_end=256;b.ppu_slice_end=512;a.ppu_trace=true;b.ppu_trace=false;
 a.steps=123;a.loaded_modules.insert(7);assert(b.steps==123 && b.loaded_modules.count(7));
 PpuContext saved=static_cast<PpuContext&>(a);static_cast<PpuContext&>(b)=saved;
 assert(b.r[3]==41 && b.pc==0x10000 && !b.ppu_trace && b.ppu_slice_end==512);
 assert(&b.memory==&owner.memory && b.steps==123 && b.loaded_modules.count(7));
 assert(!engine_step(a,{ExecutionEngine::Spu,1024}).progress);
 assert(!engine_step(a,{ExecutionEngine::Rsx,1024}).progress);
 const auto reply=engine_step(a,{ExecutionEngine::Audio,0});assert(reply.completed && !reply.progress);
 assert(owner.supervisor.sequence==3 && a.steps==123 && a.r[3]==41);
''')

    def test_perf_report_has_exclusive_interval_fields(self):
        from tests.test_ppu_block import PpuBlockTests
        from anyps3.program import compile_program
        from anyps3.elf import ElfImage
        from tests.test_anyps3 import BLR, program
        translated = compile_program(ElfImage.parse(program(BLR)), process_entry=True)
        run = PpuBlockTests().native(translated, r'''
 PpuState s;s.execution.enabled=true;
 const auto now=ExecutionTiming::Clock::now();
 const auto before=now-std::chrono::seconds(3);
 s.execution.last=ExecutionTiming::Sample{before,100,0,2,10,{}};
 s.steps=142;s.rsx.flips=5;s.rsx.draw.draws=40;
 s.execution.ns={100,200,300,400,500,600,700,800};
 anyps3_perf_report(s);assert(s.execution.last->ppu==142);
 // A second call in the same interval emits nothing.
 anyps3_perf_report(s);
''')
        lines = [line for line in run.stderr.splitlines() if line.startswith('PERF')]
        self.assertEqual(len(lines), 3, run.stderr)
        self.assertIn('ppu_steps=142', lines[0])
        self.assertEqual(lines[1], 'PERF WALL ppu_ns=100 spu_ns=200 rsx_ns=300 audio_ns=400 '
                         'idle_ns=500 flip_wait_ns=600 gpu_wait_ns=700 coordinator_ns=800')
        self.assertEqual(lines[2], 'PERF SPU_DYNAMIC relocated_entries=0 interpreted_instructions=0')

    def test_composition_adapts_definitions_without_touching_calls_or_diagnostics(self):
        source = 'static bool spu_run(PpuState& s,unsigned b){return spu_run(s,b);}'
        self.assertEqual(adapt_engines(source),
                         'static bool spu_run_inline(PpuState& s,unsigned b){return spu_run(s,b);}')
        source = 'check(vkWaitForFences(d,1,f,true,0),"vkWaitForFences(error)"); // vkDeviceWaitIdle(d)\n'
        self.assertEqual(instrument_gpu_waits(source),
                         'check(execution_gpu_wait(vkWaitForFences,d,1,f,true,0),"vkWaitForFences(error)"); // vkDeviceWaitIdle(d)\n')
        # RSXVK14 timeline waits are GPU waits too, not RSX CPU time.
        source = 'check(vkWaitSemaphores(device,&wi,UINT64_MAX),"graphics timeline wait");'
        self.assertEqual(instrument_gpu_waits(source),
                         'check(execution_gpu_wait(vkWaitSemaphores,device,&wi,UINT64_MAX),"graphics timeline wait");')


if __name__ == '__main__':
    unittest.main()
