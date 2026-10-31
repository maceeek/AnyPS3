"""Synthetic RSX FIFO action benchmark: host cost of the non-draw FIFO work on the real RSX worker.

Runs a game-shaped command stream through the production RSX owner (ANYPS3_RSX_WORKER=1, SpuWorkerSession: the worker
thread, concurrent guest memory with page broker, rsx_run with its graphics Session on a real Vulkan device, lavapipe by
default). One "frame" is UNITS draw units of state packets (vertex constants, texture/vertex/shader/blend registers,
BEGIN/END) with, per frame (defaults shaped like a heavy level frame of HIRES2, not measured from the game):
  * REPORTS occlusion queries: RESET_REPORT 0x17c8 before a unit, GET_REPORT 0x1800 (ZPASS) after it (0x17cc enabled);
  * BACKEND back-end semaphore releases 0x1d6c/0x1d70 and FRONTEND front-end releases 0x64/0x6c (values change per frame);
  * SURFACES surface switches (0x210/0x214 colour/zeta offsets) each followed by a clear 0x1d94 and ZCULL zcull syncs 0xa1c;
  * TURNS PUT kicks (FIFO runs, each ending in leave_fifo), the last one after a jump back to the frame start.
Draw units carry no vertices unless DRAW_EVERY > 0 (then every DRAW_EVERY-th unit draws one 8x8 quad), so the draw path
itself is not the point. This is not a game measurement: profile the worker thread with callgrind
(`valgrind --tool=callgrind --separate-threads=yes ./bench ...`, build with -DBENCH_CALLGRIND): the binary zeroes the
counters of the worker and main threads after WARM frames and dumps both after the measured frames, so the first dump of
the worker thread (the file whose profile holds rsx_run_impl) is steady-state cost only.

Usage: python3 bench/rsx_fifo_actions_bench.py [-o OUT_DIR] [--no-run] [CXXFLAGS...]
The binary takes key=value arguments: frames= warm= units= reports= backend= frontend= surfaces= zcull= turns= draw_every= indexed= dynamic=
indexed=1 makes each drawing unit game-shaped: an indexed triangle list (1200 BE16 indices in local memory over a 20x20 vertex
grid with positions and UVs in IO memory) with a sampled texture and the fragment program in local memory, all read by the
draw from concurrent guest memory (the draw bench uses a non-concurrent GuestMemory). dynamic=1 additionally rewrites the vertex
positions from the main thread before every frame (rewritten geometry; default 0 keeps the stream identical).
and prints ns/frame (wall, lavapipe included) and the worker-relevant counters.
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
#ifdef BENCH_CALLGRIND
#include <valgrind/callgrind.h>
#define BENCH_ZERO() CALLGRIND_ZERO_STATS
#define BENCH_DUMP() CALLGRIND_DUMP_STATS
#else
#define BENCH_ZERO() do{}while(0)
#define BENCH_DUMP() do{}while(0)
#endif
static const std::uint32_t BASE=0x20000,IO=0x70000000,REPORTS=0x80000000;
struct Config {unsigned frames=6,warm=2,units=2000,reports=230,backend=16,frontend=8,surfaces=32,zcull=8,turns=32,draw_every=0,indexed=0,dynamic=0;};
struct Stream {
 PpuState& s;std::uint32_t at=BASE;std::vector<std::uint32_t> turn_ends;
 std::vector<std::pair<std::uint32_t,std::uint32_t>> values;   // (IO offset of a semaphore value word, base value)
 void word(std::uint32_t w){if(at+4>0x100000)throw std::runtime_error("stream exceeds the command buffer");s.memory.store(IO+at,4,w);at+=4;}
 void method(std::uint32_t reg,std::initializer_list<std::uint32_t> list){word((unsigned(list.size())<<18)|reg);for(auto v:list)word(v);}
 void method(std::uint32_t reg,const std::vector<std::uint32_t>& list){word((unsigned(list.size())<<18)|reg);for(auto v:list)word(v);}
 void release(std::uint32_t reg,std::uint32_t base){word((1u<<18)|reg);values.push_back({at,base});word(base);}
};
static bool every(unsigned n,unsigned count,unsigned total){   // count events evenly spread over total units
 return count && (std::uint64_t(n)*count/total)!=(std::uint64_t(n+1)*count/total);}
static void build(Stream& st,const Config& c){
 auto& R=st.s.rsx.registers;
 st.method(0x60,{0x66606660});st.method(0x1a4,{0x66606660});st.method(0x1a8,{0x66626660});st.method(0x17cc,{1});st.method(0x1804,{1});
 unsigned report=0,backend=0,frontend=0,surface=0;
 for(unsigned n=0;n<c.units;++n){
  if(every(n,c.surfaces,c.units)){const unsigned k=surface++&1;
   st.method(0x210,{0x100000u+k*0x80000});st.method(0x214,{0x500000u+k*0x80000});st.method(0x1d90,{0xff000000u|n});st.method(0x1d94,{0xf3});}
  if(every(n,c.zcull,c.units))st.method(0xa1c,{0});
  const bool query=every(n,c.reports,c.units);
  if(query)st.method(0x17c8,{1});
  std::vector<std::uint32_t> constants{100+(n*4)%200};for(unsigned i=0;i<16;++i)constants.push_back(0x3f800000u+n+i);
  st.method(0x1efc,constants);                                                         // load position + one 4x4 matrix (unused slots)
  st.method(0x1a00+15*0x20,{0x1000u+n*64,0x18229u,0x30101u,0x80008000u,0x2aa0u,0x1u,0x00400040u,0});   // texture 15 (not sampled)
  st.method(0x1680,{R[0x1680],R[0x1684]});st.method(0x1740,{R[0x1740],R[0x1744]});     // vertex arrays as configured
  st.method(0x8e4,{R[0x8e4]});st.method(0x1d60,{R[0x1d60]});
  st.method(0x310,{n&1});st.method(0x324,{0x01010101u});st.method(0xa74,{0});
  st.method(0x1808,{c.indexed?5u:8u});
  if(c.draw_every && n%c.draw_every==0){
   if(c.indexed){st.method(0x181c,{0x4000,0x10});   // 1200 indices: five DRAW_INDEX_ARRAY words (one packet each: an incrementing packet would write 0x1828..0x1834)
    for(const auto w:{255u<<24,256u|(255u<<24),512u|(255u<<24),768u|(255u<<24),1024u|(175u<<24)})st.method(0x1824,{w});}
   else st.method(0x1814,{3u<<24});}
  st.method(0x1808,{0});
  if(query)st.method(0x1800,{(1u<<24)|((report++%2048)*16)});
  if(every(n,c.backend,c.units)){st.method(0x1d6c,{0x100u+(backend%32)*16});st.release(0x1d70,0x01000000u*(backend%32+1));++backend;}
  if(every(n,c.frontend,c.units)){st.method(0x64,{0x400u+(frontend%32)*16});st.release(0x6c,0x02000000u*(frontend%32+1));++frontend;}
  if(every(n,c.turns,c.units) && n+1<c.units)st.turn_ends.push_back(st.at);
 }
 st.word(0x20000000u|BASE);st.turn_ends.push_back(BASE);   // jump back: the last kick wraps to the frame start
}
int main(int argc,char** argv){try{
 Config c;
 for(int i=1;i<argc;++i){const std::string a=argv[i];const auto eq=a.find('=');if(eq==std::string::npos)throw std::runtime_error("argument "+a);
  const auto k=a.substr(0,eq);const unsigned v=unsigned(std::stoul(a.substr(eq+1)));
  if(k=="frames")c.frames=v;else if(k=="warm")c.warm=v;else if(k=="units")c.units=v;else if(k=="reports")c.reports=v;else if(k=="backend")c.backend=v;
  else if(k=="frontend")c.frontend=v;else if(k=="surfaces")c.surfaces=v;else if(k=="zcull")c.zcull=v;else if(k=="turns")c.turns=v;else if(k=="draw_every")c.draw_every=v;
  else if(k=="indexed")c.indexed=v;else if(k=="dynamic")c.dynamic=v;
  else throw std::runtime_error("unknown argument "+k);}
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;setup_draw(s,8,8,32,false);auto& g=s.rsx;auto& m=s.memory;
 if(c.indexed){   // the draw bench's indexed_mesh data (scenario 4), scaled to the 8x8 target
  texture_fp(s);auto bits=[](float f){std::uint32_t w;std::memcpy(&w,&f,4);return w;};
  for(unsigned v=0;v<400;++v){const float x=float(v%20)*0.4f,y=float(v/20)*0.4f;
   const float p[4]={x,y,0.25f,1.f};for(unsigned k=0;k<4;++k)m.store(0x70002000+v*16+k*4,4,bits(p[k]));
   for(unsigned k=0;k<2;++k)m.store(0x70004000+v*8+k*4,4,bits(k?y/8.f:x/8.f));}
  g.registers[0x1684]=0x80004000;   // UV array
  for(unsigned t=0;t<400;++t){const unsigned a=(t*7)%380,idx[3]={a,a+1,a+20};for(unsigned k=0;k<3;++k)m.store(0xc0004000+(t*3+k)*2,2,idx[k]);}}
 m.map(0x70010000,0xf0000,6,nullptr,0);m.map(REPORTS,0x10000,6,nullptr,0);
 g.dma=0x70008000;g.reports=REPORTS;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();
 g.registers[0x8c0]=8u<<16;g.registers[0x8c4]=8u<<16;
 Stream st{s};build(st,c);
 m.store(g.dma+0x44,4,BASE);m.store(g.dma+0x40,4,BASE);
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;if(!w.enabled)throw std::runtime_error("RSX worker not enabled");
 auto frame=[&](unsigned f){
  for(const auto& [offset,base]:st.values)m.store(IO+offset,4,base+f);   // a new value per frame: no same-value release shortcut
  // dynamic=1 (with indexed=1): the main thread rewrites the 400 vertex positions (same values, 4-byte stores) before every
  // frame, the worst case for geometry reuse (every frame's first draw finds its vertex copy written).
  if(c.dynamic)for(unsigned a=0;a<400*16;a+=4)m.store(0x70002000+a,4,m.load(0x70002000+a,4));
  for(const auto end:st.turn_ends){m.store(g.dma+0x40,4,end);
   for(const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(600);m.load(g.dma+0x44,4)!=end;){
    (void)rsx_worker_request(s,1u<<30);if(std::chrono::steady_clock::now()>until)throw std::runtime_error("FIFO did not reach PUT");std::this_thread::yield();}}
  w.queue->drain();};
 for(unsigned f=0;f<c.warm;++f)frame(f);
 const auto packets=g.commands;const auto draws=g.draw.draws;
 // Callgrind counters are per thread: zero and dump on the RSX owner (worker) thread itself, and on this thread.
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[]{BENCH_ZERO();});BENCH_ZERO();const auto t0=std::chrono::steady_clock::now();
 for(unsigned f=0;f<c.frames;++f)frame(c.warm+f);
 const double ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-t0).count();
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[]{BENCH_DUMP();});BENCH_DUMP();
 // Oracle: the last frame's releases and reports are published with this frame's values.
 unsigned checked=0;
 {std::map<std::uint32_t,std::uint32_t> expect;   // address -> last value released there in program order
  std::uint32_t at=BASE;std::uint32_t semaphore_offset=0,backend_offset=0;
  while(true){const auto h=std::uint32_t(m.load(IO+at,4));if((h&0xe0000003)==0x20000000)break;const unsigned count=(h>>18)&0x7ff,reg=h&0xfffc;
   for(unsigned i=0;i<count;++i){const auto v=std::uint32_t(m.load(IO+at+4+4*i,4));
    if(reg==0x64)semaphore_offset=v;else if(reg==0x1d6c)backend_offset=v;
    else if(reg==0x6c)expect[REPORTS+semaphore_offset]=v;
    else if(reg==0x1d70)expect[REPORTS+backend_offset]=(v&0xff00ff00u)|((v&0xffu)<<16)|((v>>16)&0xffu);}
   at+=4+4*count;}
  // Deferred labels of the last frame are published when their flight retires (service ticks): pump until they are final.
  auto final=[&]{for(const auto& [address,value]:expect)if(m.load(address,4)!=value)return false;return true;};
  for(const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(120);!final() && std::chrono::steady_clock::now()<until;){
   (void)rsx_worker_request(s,1u<<30);std::this_thread::sleep_for(std::chrono::milliseconds(1));}
  for(const auto& [address,value]:expect){if(m.load(address,4)!=value)throw std::runtime_error("release at "+std::to_string(address)+" holds "+std::to_string(m.load(address,4))+" expected "+std::to_string(value));++checked;}}
 life.finish();
 std::printf("frames=%u units=%u reports=%u backend=%u frontend=%u surfaces=%u zcull=%u turns=%u draw_every=%u packets/frame=%.0f %.3f ms/frame releases_checked=%u\n",
  c.frames,c.units,c.reports,c.backend,c.frontend,c.surfaces,c.zcull,c.turns,c.draw_every,double(g.commands-packets)/c.frames,ns/c.frames/1e6,checked);
 std::printf("indexed=%u draws/frame=%.0f\n",c.indexed,double(g.draw.draws-draws)/c.frames);
}catch(const std::exception& e){std::fprintf(stderr,"STOP %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--out', type=Path, default=Path('build/rsx-fifo-actions-bench'))
    parser.add_argument('--no-run', action='store_true')
    args, flags = parser.parse_known_args()
    flags = flags or ['-O2', '-march=native', '-ffp-contract=off']
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/'bench.cpp').write_text(native_source())
    started = time.time()
    subprocess.run(['c++', '-std=c++17', *flags, '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                    str(args.out/'bench.cpp'), *LIBRARIES, '-o', str(args.out/'bench')], check=True)
    print(f'compiled in {time.time()-started:.0f}s', file=sys.stderr)
    if args.no_run:
        return
    env = dict(os.environ, LP_NUM_THREADS='1', ANYPS3_PERF_REPORT='0', ANYPS3_SPU_WORKER='0',
               SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null')
    if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
        env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
    run = subprocess.run([str(args.out/'bench')], env=env, capture_output=True, text=True)
    print(run.stdout, end='')
    if run.returncode:
        print(run.stderr[-4000:], file=sys.stderr)
        sys.exit(run.returncode)


if __name__ == '__main__':
    main()
