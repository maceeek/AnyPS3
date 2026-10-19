"""Fixed BE pixel oracles for split storage, MSAA, float blending and stats."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_parallel import parallel_source

MAIN = r'''
int main(){
 (void)&random_cases;
 PpuState s;setup_draw(s,161,141,2816,false);auto& m=s.memory;auto& R=s.rsx.registers;
 // Split a pixel itself, not just a row: each owner remains a distinct vector.
 const auto original=m.regions[1];m.regions.erase(m.regions.begin()+1);
 const unsigned cuts[]={0,0x100003,0x110001,0x120000,0x800000};
 for(unsigned i=0;i<4;++i)m.map(0xc0000000+cuts[i],cuts[i+1]-cuts[i],6,original.bytes->data()+cuts[i],cuts[i+1]-cuts[i]);
 R[0x208]=0x314b;R[0x310]=1;R[0x314]=0x03020302;R[0x318]=0x03030303;R[0x320]=0x80068006;
 test_worker_mask=0;rsx_execute_draw(s);
 const auto threads=std::min(rsxdraw::draw_thread_count(),71u);
 assert(test_parallel_draws==(threads>1?1u:0u));
 if(threads>1)assert(test_worker_mask==((UINT64_C(1)<<threads)-1));
 assert(s.rsx.draw.pixels==161*141*2);
 // Each sample starts at zero; SRC_ALPHA=.5, so the result is .5,.25,.125,.25.
 const unsigned half[]={0x3800,0x3400,0x3000,0x3400};
 std::uint64_t hash=0;
 for(unsigned y=0;y<141;++y)for(unsigned x=0;x<322;++x)for(unsigned c=0;c<4;++c){
   const auto at=0xc0100000+y*2816+x*8+c*2;
   const auto v=(m.load(at,1)<<8)|m.load(at+1,1);assert(v==half[c]);hash=hash*131+v;
 }
 // A malformed second draw must report its original lazy error.
 m.store(0xc0001000,4,0x1e013f00);s.rsx.draw.ranges={{0,4}};
 std::string error;try{rsx_execute_draw(s);}catch(const std::runtime_error& e){error=e.what();}
 assert(error=="unsupported RSX FP opcode 63");
 std::printf("PASS %016llx\n",(unsigned long long)hash);
}
'''

@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class RsxPerfTilesTests(unittest.TestCase):
    def test_split_pixel_workers_msaa_half_blend_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);file=root/'tiles.cpp';file.write_text(parallel_source(MAIN))
            build=subprocess.run(['c++','-std=c++17','-O2','-pthread','-Wall','-Wextra','-Werror',str(file),'-o',str(root/'tiles')],capture_output=True,text=True,timeout=120)
            self.assertEqual(build.returncode,0,build.stderr)
            expected=None
            for threads in (1,24):
                for stats in ('0','1'):
                    env=dict(os.environ,ANYPS3_RSX_THREADS=str(threads),ANYPS3_RSX_STATS=stats,ANYPS3_RSX_STATS_EVERY='1')
                    run=subprocess.run([str(root/'tiles')],env=env,capture_output=True,text=True,timeout=120)
                    self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                    if expected is None:expected=run.stdout
                    self.assertEqual(run.stdout,expected)
                    if stats=='1':
                        self.assertIn('RSX stats draws=2 errors=1 pixels=45402',run.stderr)
                        if threads==24:self.assertIn('fragment:1',run.stderr)
                        self.assertIn('parallel:1' if threads==24 else 'one_thread:2',run.stderr)
                    else:self.assertNotIn('RSX stats',run.stderr)
