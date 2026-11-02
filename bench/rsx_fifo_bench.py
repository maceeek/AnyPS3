"""Synthetic RSX FIFO decode benchmark: host CPU cost per FIFO packet of rsx_run_impl.

Runs the production FIFO consumer (rsx_run_impl) over a synthetic command stream shaped like the state setup of
one game draw (vertex-constant upload through 0x1efc, texture registers, vertex array offsets/formats, fragment
program offset, blend/depth state, BEGIN/END without vertices, so no draw executes). This is not a game
measurement: it isolates the per-packet decode cost (header and value reads from guest memory, method dispatch,
register bank writes) that the HIRES2 wave attributed ~23 % of heavy-frame RSX worker time to.

Modes: `serial` (non-concurrent guest memory, consumer on the memory-owner thread) and `renderer` (concurrent guest
memory, consumer on a registered renderer thread like the game's RSX worker, so every guest read takes the
foreign-reader admission path).

Usage: python3 bench/rsx_fifo_bench.py [-o OUT_DIR] [--units N] [CXXFLAGS...]
The binary takes `UNITS [MODE [REPS]]` (MODE 0..3 = renderer<<1|window, -1 all) and prints one line per (mode, window) pair: ns/packet and ns/draw-unit (best of 5).
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_rsx_graphics import native_source as graphics_source  # noqa: E402
from tests.test_rsx_vk import LIBRARIES  # noqa: E402

MAIN = r'''
#include <chrono>
static const std::uint32_t BASE=0x20000;
struct Stream {std::vector<std::uint32_t> words;unsigned packets=0;
 void method(std::uint32_t reg,std::initializer_list<std::uint32_t> values,bool increment=true){
  words.push_back(((unsigned(values.size()))<<18)|(increment?0u:0x40000000u)|reg);for(auto v:values)words.push_back(v);++packets;}
 void method(std::uint32_t reg,const std::vector<std::uint32_t>& values){
  words.push_back(((unsigned(values.size()))<<18)|reg);for(auto v:values)words.push_back(v);++packets;}};
// One "draw unit": the state packets a typical draw setup sends (counts per GCM convenience calls).
static void unit(Stream& st,unsigned n){
 std::vector<std::uint32_t> constants{(n*4)%400};for(unsigned i=0;i<16;++i)constants.push_back(0x3f800000u+n+i);
 st.method(0x1efc,constants);                                                      // load position + one 4x4 matrix
 st.method(0x1a00,{0x1000u+n*64,0x18229u,0x30101u,0x80008000u,0x2aa0u,0x1u,0x00400040u,0});   // texture 0 offset..border
 st.method(0x1840,{0x40});                                                         // texture 0 control3 (pitch)
 st.method(0x1680,{0x100000u+n*32,0x110000u+n*32});                                // vertex array offsets 0,1
 st.method(0x1740,{0x1032u,0x822u});                                               // vertex array formats 0,1
 st.method(0x8e4,{0x40001u});                                                      // fragment program offset
 st.method(0x1d60,{0x02000400u});                                                  // shader control
 st.method(0x310,{n&1});st.method(0x324,{0x01010101u});st.method(0xa74,{1});       // blend enable, color mask, depth test
 st.method(0x1808,{5});st.method(0x1808,{0});                                      // BEGIN/END without vertices: no draw
}
struct Run {double ns_packet,ns_unit;std::uint64_t fills;};
static int reps=5;
static Run measure(bool renderer,bool windowed,unsigned units){
 rsx_fifo_window=windowed;
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
 m.map(0x70010000,0xf0000,6,nullptr,0);s.rsx.fifo_enabled=true;s.rsx.dma=0x70008000;s.rsx.device=0x70000000;s.rsx.draw.primitive=0;
 Stream st;for(unsigned n=0;n<units;++n)unit(st,n);
 const std::uint32_t end=BASE+std::uint32_t(st.words.size())*4;
 if(end>0x100000)throw std::runtime_error("stream exceeds the mapped command buffer");
 {std::uint32_t at=BASE;for(auto w:st.words){m.store(0x70000000+at,4,w);at+=4;}}
 auto consume=[&]{
  m.store(s.rsx.dma+0x44,4,BASE);m.store(s.rsx.dma+0x40,4,end);
  const auto t0=std::chrono::steady_clock::now();
  for(unsigned k=0;k<64 && m.load(s.rsx.dma+0x44,4)!=end;++k)(void)rsx_run_impl(s,1u<<30);
  const double ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-t0).count();
  if(m.load(s.rsx.dma+0x44,4)!=end)throw std::runtime_error("consumer did not reach PUT");
  return ns;};
 double best=1e300;const auto fills=rsx_fifo_window_fills;
 if(!renderer){for(int rep=0;rep<reps;++rep)best=std::min(best,consume());}
 else{
  m.enable_concurrency();m.enable_page_broker();
  std::exception_ptr error;
  std::thread worker([&]{try{
    m.renderer_thread=std::this_thread::get_id();m.renderer_enabled=true;
    for(int rep=0;rep<reps;++rep)best=std::min(best,consume());
   }catch(...){error=std::current_exception();}});
  worker.join();m.renderer_enabled=false;
  if(error)std::rethrow_exception(error);
 }
 return {best/st.packets,best/units,(rsx_fifo_window_fills-fills)/reps};
}
int main(int argc,char** argv){try{
 const unsigned units=argc>1?unsigned(std::atoi(argv[1])):2000;
 const int only=argc>2?std::atoi(argv[2]):-1;if(argc>3)reps=std::atoi(argv[3]);   // 0..3 = (renderer<<1)|window, -1 = all
 for(bool renderer:{false,true})for(bool windowed:{false,true}){if(only>=0 && only!=(int(renderer)<<1|int(windowed)))continue;
  const auto r=measure(renderer,windowed,units);
  std::printf("%-8s window=%d %8.1f ns/packet %9.1f ns/unit fills/run=%llu\n",renderer?"renderer":"serial",int(windowed),r.ns_packet,r.ns_unit,(unsigned long long)r.fills);std::fflush(stdout);}
}catch(const std::exception& e){std::fprintf(stderr,"STOP %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--out', type=Path, default=Path('build/rsx-fifo-bench'))
    parser.add_argument('--units', type=int, default=2000)
    args, flags = parser.parse_known_args()
    flags = flags or ['-O2', '-march=native', '-ffp-contract=off']
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/'bench.cpp').write_text(native_source())
    started = time.time()
    subprocess.run(['c++', '-std=c++17', *flags, '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                    str(args.out/'bench.cpp'), *LIBRARIES, '-o', str(args.out/'bench')], check=True)
    print(f'compiled in {time.time()-started:.0f}s', file=sys.stderr)
    env = dict(os.environ, LP_NUM_THREADS='1', ANYPS3_PERF_REPORT='0')
    if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
        env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
    run = subprocess.run([str(args.out/'bench'), str(args.units)], env=env, capture_output=True, text=True)
    print(run.stdout, end='')
    if run.returncode:
        print(run.stderr[-4000:], file=sys.stderr)
        sys.exit(run.returncode)


if __name__ == '__main__':
    main()
