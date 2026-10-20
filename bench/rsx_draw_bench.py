"""Synthetic RSX draw benchmark: CPU cost per draw of the Vulkan graphics path.

Runs the production draw entry (rsx_execute_draw) with a real Vulkan session
(Lavapipe by default) on tiny surfaces, so that the GPU work is negligible and
the wall time per draw approximates the RSX worker's host cost. This is not a
game measurement; it is a reproducible relative benchmark for host-side work.

Usage: python3 bench/rsx_draw_bench.py [-o OUT_DIR] [--draws N] [--only K] [CXXFLAGS...]
(The binary takes `DRAWS [SCENARIO]`; profile it with perf to separate host code from the driver.)
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_rsx_perf_draw import native_source as warm_source  # noqa: E402
from tests.test_rsx_vk import LIBRARIES  # noqa: E402

MAIN = r'''
#include <chrono>
#include <new>
// Test setup writes through mutable borrows; the game's FIFO path does not.
// Rebuild the arrays/bank so the benchmark starts like the game (no exposure).
template<class A> static void unexpose(A& a){A copy(a);a.~A();new(&a)A(copy);}
// scenario: 0 identical draws, 1 a fragment constant changes every draw,
// 2 = 1 with 2x MSAA (diagonal centered), 3 = 1 alternating two textures,
// 4 = 1 with an indexed triangle list (1200 BE16 indices over 400 vertices).
static double scenario(unsigned kind,unsigned draws){
 PpuState s;
 if(kind==2){setup_draw(s,16,16,1024,false);s.rsx.registers[0x208]|=3u<<12;}else setup_draw(s,16,16,64,false);
 texture_fp(s);
 if(kind==3){for(unsigned i=0;i<4;++i)s.memory.store(0xc0003000+i*4,4,0xff000000u|(i*0x111111u));}
 if(kind==4){
  // 400 vertices on a 20x20 grid inside the 16x16 target (positions xyzw at
  // 0x70002000 stride 16, uv at 0x70004000 stride 8), 1200 indices at local 0x4000.
  auto bits=[](float f){std::uint32_t w;std::memcpy(&w,&f,4);return w;};
  for(unsigned v=0;v<400;++v){const float x=float(v%20)*0.8f,y=float(v/20)*0.8f;
   const float p[4]={x,y,0.25f,1.f};for(unsigned c=0;c<4;++c)s.memory.store(0x70002000+v*16+c*4,4,bits(p[c]));
   for(unsigned c=0;c<2;++c)s.memory.store(0x70004000+v*8+c*4,4,bits(c?y/16.f:x/16.f));}
  auto& R=s.rsx.registers;R[0x1684]=0x80004000;
  for(unsigned t=0;t<400;++t){const unsigned a=(t*7)%380,idx[3]={a,a+1,a+20};
   for(unsigned k=0;k<3;++k)s.memory.store(0xc0004000+(t*3+k)*2,2,idx[k]);}
  s.rsx.draw.indexed=true;R[0x181c]=0x4000;R[0x1820]=0x10;s.rsx.draw.primitive=5;
 }
 unexpose(s.rsx.transform_program);unexpose(s.rsx.transform_program_written);
 unexpose(s.rsx.transform_constants);unexpose(s.rsx.transform_constants_written);
 {RsxRegisters bank(s.rsx.registers);bank.exposed.fill(0);s.rsx.registers=bank;}
 s.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 // Mirror rsx_run's FIFO scope (rsx.py): one texture-context validation and
 // guard re-arm per FIFO turn; a turn covers 64 draws here.
 auto turn=[&](bool enter){
  if(!enter){session.leave_fifo();rsxdraw::fifo_target_cache=nullptr;session.fifo_context=false;session.context_validated=false;return;}
  session.enter_fifo();session.fifo_context=true;session.context_validated=false;session.validate_texture_context();
  rsxdraw::fifo_target_cache=&session.target_cache;};
 auto run=[&](unsigned n){
  if(n%64==0){if(n)turn(false);turn(true);}
  if(kind>=1)fpword(s,5,rsxdraw::float_bits(float(n%7)/8.f));
  if(kind==3)s.rsx.registers.write(0x1a00,(n&1)?0x3000:0x2000);
  s.rsx.draw.ranges={{0,kind==4?1200u:4u}};rsx_execute_draw(s);};
 for(unsigned n=0;n<64;++n)run(n);turn(false);session.flush();
 const auto t0=std::chrono::steady_clock::now();
 for(unsigned n=0;n<draws;++n)run(n);
 turn(false);session.flush();
 const double us=std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-t0).count()/draws;
 s.rsx.graphics_session=nullptr;
 return us;
}
int main(int argc,char** argv){try{
 const unsigned draws=argc>1?unsigned(std::atoi(argv[1])):4000;
 const int only=argc>2?std::atoi(argv[2]):-1;
 const char* names[]={"identical","constant","constant_msaa2","two_textures","indexed_mesh"};
 for(unsigned kind=0;kind<5;++kind){if(only>=0 && unsigned(only)!=kind)continue;double best=1e30;for(int rep=0;rep<3;++rep)best=std::min(best,scenario(kind,draws));
  std::printf("%-16s %8.2f us/draw\n",names[kind],best);std::fflush(stdout);}
}catch(const std::exception& e){std::fprintf(stderr,"STOP %s\n",e.what());return 1;}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--out', type=Path, default=Path('build/rsx-draw-bench'))
    parser.add_argument('--draws', type=int, default=4000)
    parser.add_argument('--only', type=int, default=-1, help='run one scenario (0-4)')
    args, flags = parser.parse_known_args()
    flags = flags or ['-O2', '-march=native', '-ffp-contract=off']
    args.out.mkdir(parents=True, exist_ok=True)
    source = warm_source().split('int main(){try{level_values()')[0] + MAIN
    (args.out/'bench.cpp').write_text(source)
    started = time.time()
    subprocess.run(['c++', '-std=c++17', *flags, '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                    str(args.out/'bench.cpp'), *LIBRARIES, '-o', str(args.out/'bench')], check=True)
    print(f'compiled in {time.time()-started:.0f}s', file=sys.stderr)
    env = dict(os.environ, LP_NUM_THREADS='1', ANYPS3_PERF_REPORT='0')
    if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
        env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
    run = subprocess.run([str(args.out/'bench'), str(args.draws), str(args.only)], env=env, capture_output=True, text=True)
    print(run.stdout, end='')
    if run.returncode:
        print(run.stderr[-4000:], file=sys.stderr)
        sys.exit(run.returncode)


if __name__ == '__main__':
    main()
