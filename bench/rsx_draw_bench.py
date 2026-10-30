"""Synthetic RSX draw benchmark: CPU cost per draw of the Vulkan graphics path.

Runs the production draw entry (rsx_execute_draw) with a real Vulkan session
(Lavapipe by default) on tiny surfaces, so that the GPU work is negligible and
the wall time per draw approximates the RSX worker's host cost. This is not a
game measurement; it is a reproducible relative benchmark for host-side work.

Usage: python3 bench/rsx_draw_bench.py [-o OUT_DIR] [--draws N] [--only K] [--callgrind] [CXXFLAGS...]
(The binary takes `DRAWS [SCENARIO]`; profile it with perf to separate host code from the driver.)

--callgrind builds with the valgrind client macros (statistics zeroed after the warm-up, collected over the timed loop
only), runs the binary under callgrind with ANYPS3_RSX_RECORDER=1 (Vulkan command recording on its own thread, as in
the game) and prints the drawing thread's instructions per draw of the last (steady-state) repetition. Valgrind cannot
execute AVX-512: pass e.g. -O2 -march=x86-64-v3 -ffp-contract=off. Its dumps stay in OUT_DIR (cg.out.*) for
callgrind_annotate --inclusive=yes.
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
// 4 = 1 with an indexed triangle list (1200 BE16 indices over 400 vertices),
// 5 game-like: per draw 8 vertex-constant vec4 through 0x1efc/0x1f00, both
// vertex attribute offsets switch between three meshes, unit 0 cycles three and
// unit 1 two textures, one inline FP constant changes, 300 BE16 indices; blend
// toggles every 16th and depth test every 32nd draw.
#ifdef ANYPS3_BENCH_CALLGRIND
#include <valgrind/callgrind.h>
#define BENCH_COLLECT_BEGIN CALLGRIND_ZERO_STATS;CALLGRIND_TOGGLE_COLLECT;
#define BENCH_COLLECT_END CALLGRIND_TOGGLE_COLLECT;CALLGRIND_DUMP_STATS;
#else
#define BENCH_COLLECT_BEGIN
#define BENCH_COLLECT_END
#endif
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
 auto bits=[](float f){std::uint32_t w;std::memcpy(&w,&f,4);return w;};
 if(kind==5){
  // Three copies of the scenario-4 grid: positions at IO 0x2000/0x4000/0x6000
  // (stride 16), uv at 0x8000/0x9000/0xa000 (stride 8); 300 indices at local 0x4000.
  for(unsigned k=0;k<3;++k)for(unsigned v=0;v<400;++v){const float x=float(v%20)*0.8f,y=float(v/20)*0.8f+float(k)*0.1f;
   const float p[4]={x,y,0.25f+0.125f*float(k),1.f};for(unsigned c=0;c<4;++c)s.memory.store(0x70002000+k*0x2000+v*16+c*4,4,bits(p[c]));
   for(unsigned c=0;c<2;++c)s.memory.store(0x70008000+k*0x1000+v*8+c*4,4,bits(c?y/16.f:x/16.f));}
  auto& R=s.rsx.registers;
  for(unsigned t=0;t<100;++t){const unsigned a=(t*7)%380,idx[3]={a,a+1,a+20};
   for(unsigned k=0;k<3;++k)s.memory.store(0xc0004000+(t*3+k)*2,2,idx[k]);}
  s.rsx.draw.indexed=true;R[0x181c]=0x4000;R[0x1820]=0x10;s.rsx.draw.primitive=5;
  // FP: TEX R1=unit1(TEX0); TEX R0=unit0(TEX0); MAD R0=R0*R1+c (inline constant block, END).
  fpword(s,0,(23u<<24)|(1u<<17)|(4u<<13)|(15u<<9)|(1u<<1));fpword(s,1,1u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
  fpword(s,4,(23u<<24)|(4u<<13)|(15u<<9));fpword(s,5,1u|(0xe4u<<9)|(7u<<18));fpword(s,6,0);fpword(s,7,0);
  fpword(s,8,(4u<<24)|(15u<<9)|1u);fpword(s,9,(0xe4u<<9)|(7u<<18));fpword(s,10,(1u<<2)|(0xe4u<<9));fpword(s,11,2u|(0xe4u<<9));
  for(unsigned c=0;c<4;++c)fpword(s,12+c,bits(c==3?0.f:0.125f));
  // Unit 1 copies unit 0's 2x2 linear ARGB8 layout; five texel sets (unit 0: 0x2000/0x3000/0x3800, unit 1: 0x5000/0x5800).
  for(unsigned r=0;r<0x20;r+=4)R[0x1a20+r]=R[0x1a00+r];R[0x1844]=R[0x1840];
  const unsigned sets[]={0x2000,0x3000,0x3800,0x5000,0x5800};
  for(unsigned k=0;k<5;++k)for(unsigned i=0;i<4;++i)s.memory.store(0xc0000000+sets[k]+i*4,4,0xff000000u|((k*0x30+i*0x11)*0x010101u));
  R[0x314]=0x00010302;R[0x318]=0x00000303;R[0x320]=0x80068006;R[0xa6c]=0x203;R[0xa70]=1;
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
  if(kind>=1 && kind<=4)fpword(s,5,rsxdraw::float_bits(float(n%7)/8.f));
  if(kind==3)s.rsx.registers.write(0x1a00,(n&1)?0x3000:0x2000);
  if(kind==5){auto& R=s.rsx.registers;
   // FIFO order of a game draw: constants (the 0x1f00 method stores the register and the bank word), attribute
   // offsets, texture offsets, state toggles, then the indexed range.
   R.write(0x1efc,40);
   for(unsigned w=0;w<32;++w){const auto v=bits(float((n*32+w)%61)/64.f);R.write(0x1f00+4*w,v);
    s.rsx.transform_constants.write(160+w,v);s.rsx.transform_constants_written.write(160+w,true);}
   R.write(0x1680,0x80002000+(n%3)*0x2000);R.write(0x1684,0x80008000+(n%3)*0x1000);
   R.write(0x1a00,n%3==0?0x2000:n%3==1?0x3000:0x3800);R.write(0x1a20,(n&1)?0x5800:0x5000);
   R.write(0x310,(n/16)&1);R.write(0xa74,(n/32)&1);
   fpword(s,13,bits(float(n%5)/8.f));}
  s.rsx.draw.ranges={{0,kind==4?1200u:kind==5?300u:4u}};rsx_execute_draw(s);};
 for(unsigned n=0;n<64;++n)run(n);turn(false);session.flush();
 BENCH_COLLECT_BEGIN
 const auto t0=std::chrono::steady_clock::now();
 for(unsigned n=0;n<draws;++n)run(n);
 turn(false);session.flush();
 BENCH_COLLECT_END
 const double us=std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-t0).count()/draws;
 s.rsx.graphics_session=nullptr;
 return us;
}
int main(int argc,char** argv){try{
 const unsigned draws=argc>1?unsigned(std::atoi(argv[1])):4000;
 const int only=argc>2?std::atoi(argv[2]):-1;
 const char* names[]={"identical","constant","constant_msaa2","two_textures","indexed_mesh","game_like"};
 for(unsigned kind=0;kind<6;++kind){if(only>=0 && unsigned(only)!=kind)continue;double best=1e30;for(int rep=0;rep<3;++rep)best=std::min(best,scenario(kind,draws));
  std::printf("%-16s %8.2f us/draw\n",names[kind],best);std::fflush(stdout);}
}catch(const std::exception& e){std::fprintf(stderr,"STOP %s\n",e.what());return 1;}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--out', type=Path, default=Path('build/rsx-draw-bench'))
    parser.add_argument('--draws', type=int, default=4000)
    parser.add_argument('--only', type=int, default=-1, help='run one scenario (0-5)')
    parser.add_argument('--callgrind', action='store_true', help='instructions per draw of the drawing thread under callgrind')
    args, flags = parser.parse_known_args()
    flags = flags or ['-O2', '-march=native', '-ffp-contract=off']
    args.out.mkdir(parents=True, exist_ok=True)
    source = warm_source().split('int main(){try{level_values()')[0] + MAIN
    defines = ['-DANYPS3_BENCH_CALLGRIND'] if args.callgrind else []
    command = ['c++', '-std=c++17', *flags, *defines, '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
               str(args.out/'bench.cpp'), *LIBRARIES, '-o', str(args.out/'bench')]
    # The same source and command reuse the binary (one build, several scenarios).
    stamp = source + '\n//' + ' '.join(command)
    if not ((args.out/'bench').exists() and (args.out/'bench.stamp').exists() and (args.out/'bench.stamp').read_text() == stamp):
        (args.out/'bench.cpp').write_text(source)
        started = time.time()
        subprocess.run(command, check=True)
        (args.out/'bench.stamp').write_text(stamp)
        print(f'compiled in {time.time()-started:.0f}s', file=sys.stderr)
    env = dict(os.environ, LP_NUM_THREADS='1', ANYPS3_PERF_REPORT='0')
    if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
        env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
    command = [str(args.out/'bench'), str(args.draws), str(args.only)]
    if args.callgrind:
        if args.only < 0:
            parser.error('--callgrind needs --only K')
        env.setdefault('ANYPS3_RSX_RECORDER', '1')
        for old in args.out.glob('cg.out*'):
            old.unlink()
        command = ['valgrind', '--tool=callgrind', '--separate-threads=yes', '--collect-atstart=no',
                   f'--callgrind-out-file={args.out/"cg.out"}', *command]
    run = subprocess.run(command, env=env, capture_output=True, text=True)
    print(run.stdout, end='')
    if run.returncode:
        print(run.stderr[-4000:], file=sys.stderr)
        sys.exit(run.returncode)
    if args.callgrind:
        # Thread 1 runs rsx_execute_draw (the RSX worker's role); dump 3 is the last repetition's timed loop.
        for part in (1, 2, 3):
            dump = args.out/f'cg.out.{part}-01'
            total = next(int(line.split()[1]) for line in dump.read_text().splitlines() if line.startswith('summary:'))
            print(f'callgrind repetition {part}: {total} Ir, {total/args.draws:.0f} Ir/draw ({dump})')


if __name__ == '__main__':
    main()
