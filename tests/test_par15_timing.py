"""Independent fixed-time oracle for redundant scope elision and PPU id splits."""
from tests import test_execution
from anyps3.execution import TYPES
import unittest

class Par15TimingContracts(unittest.TestCase):
    def test_nested_same_phase_costs_no_clock_and_keeps_exact_totals(self):
        test_execution.ExecutionTests().native(r'''
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <map>
#include <optional>
#include <string>
#include <cassert>
struct FixedClock {
 using time_point=std::chrono::steady_clock::time_point;
 inline static unsigned value=0;
 static time_point now(){return time_point(std::chrono::nanoseconds(value));}
};
''' + TYPES.replace('using Clock=std::chrono::steady_clock;', 'using Clock=FixedClock;') + r'''
int main(){
 ExecutionTiming timing;timing.enabled=true;timing.clock.enabled=true;
 timing.select_ppu(7,0);
 {
  ExecutionScope ppu(timing,ExecutionPhase::Ppu);
  FixedClock::value=10;
  for(unsigned i=0;i<100000;++i){ExecutionScope nested(timing,ExecutionPhase::Ppu);}
  assert(timing.clock.reads==1 && timing.elided_clock_reads==200001);
  FixedClock::value=20;
  {ExecutionScope spu(ExecutionPhase::Spu);FixedClock::value=30;}
  FixedClock::value=40;timing.select_ppu(8,10);
  FixedClock::value=50;
 }
 assert(timing.phase==ExecutionPhase::Count && !execution_timing);
 assert(timing.ns[0]==40 && timing.ns[1]==10);
 assert(timing.ppu_metrics[7].ns==30 && timing.ppu_metrics[8].ns==10);
 assert(timing.ppu_metrics[7].instructions==10 && timing.ppu_metrics[8].instructions==0);
 assert(timing.clock.reads==5 && timing.clock.probes==1);
 // Two process facades retain exact attribution across nested reentry.
 ExecutionTiming other;other.enabled=true;other.clock.enabled=true;
 FixedClock::value=60;
 {ExecutionScope ppu(timing,ExecutionPhase::Ppu);FixedClock::value=70;
  {ExecutionScope inner(other,ExecutionPhase::Audio);FixedClock::value=90;}
  FixedClock::value=100;}
 assert(timing.ns[0]==60 && other.ns[3]==20 && !execution_timing);
}
''')

if __name__=='__main__':unittest.main()
