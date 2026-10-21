"""ANYPS3_VBLANK_HZ (VBLANKHZ task): emulated vblank rate arithmetic and grammar (native, compiles anyps3.rsx.VBLANK_CPP alone).

Default (unset, 60000/1001): tick numbers and tick times must equal the former hardcoded 60/1.001 expressions bit for bit, over
random and boundary elapsed times up to ~11 days, with no drift (tick 60000*k is exactly k*1001 s). Other rates (90, 75,
120000/1001) are checked against exact floors computed here in Python big integers (independent of the C++ split arithmetic),
and tick n is counted at tick_ns(n) (exact) or one nanosecond later (floored). Grammar/range errors are rejected with a message.
"""
import random
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from anyps3.rsx_vblank import VBLANK_CPP


def cases():
    rnd = random.Random(20261021)
    lines = []
    for text, rate in (('90', Fraction(90)), ('75', Fraction(75)), ('120000/1001', Fraction(120000, 1001)), ('240/4', Fraction(60))):
        for _ in range(400):
            e = rnd.choice([rnd.randrange(0, 10**9), rnd.randrange(0, 10**15), rnd.randrange(0, 10**12)])
            n = rnd.randrange(0, 10**8)
            lines.append('{"%s",%dull,%dull,%dull,%dull},' % (text, e, (e * rate.numerator) // (10**9 * rate.denominator),
                                                             n, (n * 10**9 * rate.denominator) // rate.numerator))
    return '\n'.join(lines)


MAIN = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <random>
''' + VBLANK_CPP + r'''
struct Expect {const char* text;std::uint64_t elapsed,ticks,n,tick_ns;};
static const Expect EXPECT[]={
@CASES@
};
static void fail(const std::string& m){std::fprintf(stderr,"FAIL %s\n",m.c_str());std::exit(1);}
int main(){
 const RsxVblankRate def;
 if(def.custom || def.num!=60000 || def.den!=1001)fail("default rate is not 60000/1001");
 // The former expressions (rsx.py rsx_tick_vblank / flip mode 2, rsx_game_worker.py deadline), verbatim.
 auto old_ticks=[](std::uint64_t elapsed){return (elapsed/1001000000)*60+(elapsed%1001000000)*60/1001000000;};
 auto old_tick_ns=[](std::uint64_t ticks){return (ticks/60)*1001000000+(ticks%60)*1001000000/60;};
 std::mt19937_64 rng(7);unsigned checked=0;
 for(unsigned i=0;i<2000000;++i){
  const std::uint64_t e=i<1000000?std::uint64_t(i)*1000003:rng()%UINT64_C(1000000000000000);   // up to ~11.6 days
  if(def.ticks(e)!=old_ticks(e))fail("default ticks differ at elapsed "+std::to_string(e));
  const std::uint64_t n=i<1000000?i:rng()%UINT64_C(100000000);
  if(def.tick_ns(n)!=old_tick_ns(n))fail("default tick time differs at tick "+std::to_string(n));
  checked+=2;}
 for(std::uint64_t k=0;k<200000;k+=37)if(def.tick_ns(60000*k)!=k*UINT64_C(1001000000000))fail("default drifts at k="+std::to_string(k));
 std::printf("PASS default 60000/1001 equals the former 60/1.001 arithmetic (%u values, no drift)\n",checked);
 unsigned n=0;
 for(const auto& x:EXPECT){RsxVblankRate r;const auto error=rsx_parse_vblank_hz(x.text,r);if(!error.empty())fail(std::string(x.text)+": "+error);
  if(r.ticks(x.elapsed)!=x.ticks)fail(std::string(x.text)+": ticks("+std::to_string(x.elapsed)+")="+std::to_string(r.ticks(x.elapsed))+" expected "+std::to_string(x.ticks));
  if(r.tick_ns(x.n)!=x.tick_ns)fail(std::string(x.text)+": tick_ns("+std::to_string(x.n)+") expected "+std::to_string(x.tick_ns));
  // tick_ns is floor(n/rate): tick n is counted at that time if it is exact, otherwise one nanosecond later (as before).
  const auto at=r.tick_ns(x.n),seen=r.ticks(at);
  if((seen!=x.n && seen+1!=x.n) || r.ticks(at+1)!=x.n)fail(std::string(x.text)+": tick boundary mismatch at "+std::to_string(x.n));
  ++n;}
 {RsxVblankRate r;rsx_parse_vblank_hz("90",r);if(!r.custom || r.tick_ns(90)!=UINT64_C(1000000000) || r.tick_ns(1)!=11111111)fail("90 Hz tick times");
  rsx_parse_vblank_hz("120000/2002",r);if(r.custom || r.num!=60000 || r.den!=1001)fail("120000/2002 does not reduce to the default");}
 std::printf("PASS 90, 75, 120000/1001 and 240/4 Hz equal exact floors (%u cases)\n",n);
 const char* bad[]={"","-60","+60"," 60","60 ","60.0","60/","/60","0","60/0","0/5","1/2","1001","1000001/1000000","99999999999999999999999","60/1/1","6e1","0x3c"};
 for(const char* b:bad){RsxVblankRate r;if(rsx_parse_vblank_hz(b,r).empty())fail(std::string("accepted invalid '")+b+"'");}
 {RsxVblankRate r;for(const char* ok:{"1","1000","60000/1001","59","3/2"})if(!rsx_parse_vblank_hz(ok,r).empty())fail(std::string("rejected valid '")+ok+"'");}
 std::puts("PASS grammar and range errors are rejected");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
class RsxVblankHzTests(unittest.TestCase):
    def test_vblank_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'vblank.cpp').write_text(MAIN.replace('@CASES@', cases()))
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', str(root / 'vblank.cpp'), '-o', str(root / 'vblank')],
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / 'vblank')], capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(run.stdout.count('PASS '), 3, run.stdout)


if __name__ == '__main__':
    unittest.main()
