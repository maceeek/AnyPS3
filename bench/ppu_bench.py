"""PPU translation micro-benchmark: assembles ppu_kernels.s, translates with AnyPS3, times native code.

Usage: python3 bench/ppu_bench.py ROOT OUT_DIR [CXXFLAGS...]  (ROOT = AnyPS3 tree to import,
so two trees can be compared with the same kernels; needs llvm-mc/llvm-objcopy/llvm-objdump).
Environment: BENCH_ONLY=k_name, BENCH_FPSCR=0x02000000 (initial FPSCR), BENCH_THREADS=1 (a second,
blocked PPU thread), BENCH_SERIAL=1 (no concurrent memory protocol).
Prints ns per guest instruction per kernel. Not a game measurement.
"""
import os, re, struct, subprocess, sys, json, time
root, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
flags = sys.argv[3:] or ['-O2', '-march=native', '-ffp-contract=off']
sys.path.insert(0, root)
from anyps3.program import compile_program
from anyps3.prx import make_image
here = os.path.dirname(os.path.abspath(__file__))
os.makedirs(out, exist_ok=True)
subprocess.run(['llvm-mc', '--triple=powerpc64-unknown-linux-gnu', '-filetype=obj', os.path.join(here, 'ppu_kernels.s'),
                '-o', os.path.join(out, 'kernels.o')], check=True, stderr=subprocess.DEVNULL)
subprocess.run(['llvm-objcopy', '-O', 'binary', '--only-section=.text', os.path.join(out, 'kernels.o'), os.path.join(out, 'kernels.bin')], check=True)
code = open(os.path.join(out, 'kernels.bin'), 'rb').read()
syms = {}
for line in subprocess.run(['llvm-objdump', '-t', os.path.join(out, 'kernels.o')], capture_output=True, text=True).stdout.splitlines():
    m = re.match(r'([0-9a-f]+) g\s+\.text\s+\S+ (k_\w+)', line)
    if m: syms[m.group(2)] = int(m.group(1), 16)
BASE = 0x10000
code = code + bytes((-len(code)) & 0xfff)
image = make_image([(BASE, 5, code), (0x40000, 6, struct.pack('>II', BASE, 0))], entry=0x40000)
translated = compile_program(image, process_entry=True, extra_entries=tuple(BASE + a for a in syms.values()))
src = translated.cpp(max_steps=10**12).replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
iters = dict(k_copy=200, k_calls=200000, k_branchy=300000, k_float=1000, k_vmx=1000, k_struct=400, k_spin=300000, k_atomic=300000)
calls = []
for name, addr in sorted(syms.items()):
    calls.append(f'run(code,"{name}",0x{BASE+addr:x},{iters.get(name,1000)});')
payload = ','.join(str(b) for b in code)
src += r'''
#include <chrono>
static void setup(PpuState& s,const std::uint8_t* code,std::size_t n){
    s.memory.map(0x10000,(n+0xfff)&~std::size_t(0xfff),5,code,n);
    std::vector<std::uint8_t> data(0x10000);
    for(unsigned i=0;i<0x10000;i+=8){const std::uint32_t a=0x3f000000u,b=0x3e800000u|((i/8)&1?0xff00u:0u);
        for(unsigned k=0;k<4;++k){data[i+k]=std::uint8_t(a>>(24-8*k));data[i+4+k]=std::uint8_t(b>>(24-8*k));}}
    // vperm control at 0x207ff0: swap word pairs (lanes stay finite floats).
    const std::uint8_t control[16]={4,5,6,7,0,1,2,3,12,13,14,15,8,9,10,11};for(unsigned k=0;k<16;++k)data[0x7ff0+k]=control[k];
    s.memory.map(0x200000,0x10000,6,data.data(),data.size());
    s.memory.map(0x300000,0x10000,6,nullptr,0);
    if(std::getenv("BENCH_SERIAL")==nullptr){s.memory.enable_concurrency();s.memory.enable_page_broker();}
}
static void run(const std::uint8_t* code,const char* name,std::uint32_t pc,std::uint64_t iters){
    if(const char* only=std::getenv("BENCH_ONLY"))if(std::strcmp(only,name))return;
    double best=1e30;std::uint64_t steps=0;
    for(int rep=0;rep<5;++rep){
        PpuState s;setup(s,code,''' + str(len(code)) + r''');s.pc=pc;s.r[1]=0x30ff00;s.r[3]=iters;s.lr=RETURN_SENTINEL;
        if(const char* f=std::getenv("BENCH_FPSCR"))s.fpscr=std::uint32_t(std::strtoul(f,nullptr,0));
        if(std::getenv("BENCH_THREADS")){s.kernel.semaphores.push_back({99,0,1});PpuContext other;other.thread_id=2;other.pc=pc;other.wait_kind=2;other.wait_id=99;
            s.ppu_threads={static_cast<PpuContext&>(s),other};}
        const auto t0=std::chrono::steady_clock::now();anyps3_entry(s,false);
        const double ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-t0).count();
        if(!s.returned){std::fprintf(stderr,"%s did not return\n",name);std::exit(2);}
        steps=s.steps;best=std::min(best,ns/double(s.steps));
    }
    std::printf("%-10s %12llu instr  %.3f ns/instr\n",name,(unsigned long long)steps,best);
}
int main(){static const std::uint8_t code[]={''' + payload + '};\n' + '\n'.join(calls) + '\n}\n'
open(os.path.join(out, 'bench.cpp'), 'w').write(src)
t = time.time()
subprocess.run(['c++', '-std=c++17', *flags, '-pthread', os.path.join(out, 'bench.cpp'), '-o', os.path.join(out, 'bench')], check=True)
print(f'compiled in {time.time()-t:.0f}s', file=sys.stderr)
r = subprocess.run([os.path.join(out, 'bench')], capture_output=True, text=True, env=dict(os.environ, ANYPS3_PERF_REPORT='0'))
print(r.stdout, r.stderr[-3000:])
