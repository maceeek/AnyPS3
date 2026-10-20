"""ppu_schedule micro-benchmark with a game-like thread population (not a game measurement).

Usage: python3 bench/sched_bench.py ROOT OUT_DIR [CXXFLAGS...]
Population modelled on a THREAD DUMP of the level (374load): thread 1 active; 4 event-queue receivers (wait kind 1),
2 SPU group joins (5), 1 cond waiter (4), 10 lwcond waiters (8), 1 sleeping thread (sync_ext usleep, kind 12 with a far
deadline), 14 semaphore waiters (2), among 24 semaphores and 12 event queues. Scenario `idle`: nobody else is runnable
(the common preemption point); scenario `other`: thread 21 is runnable (a real switch every call).
Prints ns per ppu_schedule(state) call.
"""
import os, struct, subprocess, sys
root, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
flags = sys.argv[3:] or ['-O2', '-march=native', '-ffp-contract=off']
sys.path.insert(0, root)
from anyps3.program import compile_program
from anyps3.prx import make_image
os.makedirs(out, exist_ok=True)
code = struct.pack('>I', 0x4e800020)
image = make_image([(0x10000, 5, code), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
src = compile_program(image, process_entry=True).cpp().replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
src += r'''
#include <chrono>
static void populate(PpuState& s,bool other_runnable){
    s.ppu_threads.clear();
    for(unsigned i=0;i<24;++i)s.kernel.semaphores.push_back({0x96000000u+i*0x100,0,1});
    for(unsigned i=0;i<12;++i)s.events.queues.push_back({0x8d000000u+i*0x100,127,0x1000+i});
    auto add=[&](unsigned id,unsigned kind,std::uint64_t wait_id,unsigned prio){
        PpuContext t;t.thread_id=id;t.wait_kind=kind;t.wait_id=wait_id;t.priority=prio;t.pc=0x10000;s.ppu_threads.push_back(t);};
    s.thread_id=1;s.priority=1000;
    add(1,0,0,1000);
    unsigned id=2;
    for(unsigned i=0;i<4;++i)add(id++,1,0x8d000000u+i*0x100,100);
    for(unsigned i=0;i<2;++i)add(id++,5,0x4000100u+i*0x100,0);
    add(id++,4,0x86000b00,100);
    for(unsigned i=0;i<10;++i)add(id++,8,0x408706d8u+i*8,99);
    add(21,other_runnable?0:12,0,10);
    if(!other_runnable)s.sync_ext.waits.emplace(21,Lv2SyncExt::Wait{12,0,10,1,sync_ext_now()+3600ull*1000000,0,0,0});
    for(unsigned i=0;i<14;++i)add(22+i,2,0x96000000u+(i+3)*0x100,401);
    static_cast<PpuContext&>(s)=s.ppu_threads[0];
}
static void bench(const char* name,bool other){
    double best=1e30;
    for(int rep=0;rep<5;++rep){
        PpuState s;populate(s,other);
        const int n=200000;
        const auto t0=std::chrono::steady_clock::now();
        for(int i=0;i<n;++i){s.reschedule=true;ppu_schedule(s);}
        best=std::min(best,std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-t0).count()/n);
    }
    std::printf("%-8s %.1f ns/call\n",name,best);
}
int main(){bench("idle",false);bench("other",true);}
'''
open(os.path.join(out, 'sched.cpp'), 'w').write(src)
subprocess.run(['c++', '-std=c++17', *flags, '-pthread', os.path.join(out, 'sched.cpp'), '-o', os.path.join(out, 'sched')], check=True)
r = subprocess.run([os.path.join(out, 'sched')], capture_output=True, text=True, env=dict(os.environ, ANYPS3_PERF_REPORT='0'))
print(r.stdout, r.stderr[-3000:])
