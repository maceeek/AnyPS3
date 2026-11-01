"""PPU translation micro-benchmark: assembles ppu_kernels.s, translates with AnyPS3, times native code.

Usage: python3 bench/ppu_bench.py ROOT OUT_DIR [CXXFLAGS...]  (ROOT = AnyPS3 tree to import,
so two trees can be compared with the same kernels; needs llvm-mc/llvm-objcopy/llvm-objdump).
Environment: BENCH_ONLY=k_name, BENCH_FPSCR=0x02000000 (initial FPSCR), BENCH_THREADS=1 (a second,
blocked PPU thread), BENCH_SERIAL=1 (no concurrent memory protocol), BENCH_REPS=5 (best of N runs).
BENCH_CALLGRIND=DIV: compile with -g and run each kernel once under callgrind (iterations / DIV, collection
only inside anyps3_entry); prints host Ir per guest instruction and its split into page-function parts
(block bodies, block heads, page entry/switch, indirect/cross-page exits), ppu_dispatch and the runtime.
BENCH_BRANCH=1 adds callgrind's branch simulation per guest instruction (Bcm mispredicted conditional, Bi/Bim
indirect jumps/calls executed/mispredicted; returns are not indirect branches there).
Symbols e_* are indirect-call targets (extra translation entries); only k_* kernels run.
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
    m = re.match(r'([0-9a-f]+) g\s+\.text\s+\S+ ([ke]_\w+)', line)
    if m: syms[m.group(2)] = int(m.group(1), 16)
BASE = 0x10000
code = code + bytes((-len(code)) & 0xfff)
image = make_image([(BASE, 5, code), (0x40000, 6, struct.pack('>II', BASE, 0))], entry=0x40000)
translated = compile_program(image, process_entry=True, extra_entries=tuple(BASE + a for a in syms.values()))
src = translated.cpp(max_steps=10**12).replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
iters = dict(k_copy=200, k_calls=200000, k_branchy=300000, k_float=1000, k_vmx=1000, k_struct=400, k_spin=300000, k_atomic=300000,
             k_xcall=300000, k_calltree=150000, k_virtual=300000, k_switch=300000, k_mix=400)
div = int(os.environ.get('BENCH_CALLGRIND', '0') or 0)
calls = []
for name, addr in sorted(syms.items()):
    if name.startswith('k_'):
        calls.append(f'run(code,"{name}",0x{BASE+addr:x},{max(1, iters.get(name,1000) // max(div, 1))});')
payload = ','.join(str(b) for b in code)
src += r'''
#include <chrono>
#ifdef BENCH_MREMAP_SHIM
// Callgrind mode only: valgrind rejects mremap(old, 0, ...) duplication of a shared mapping, which the
// inline flat guest view uses. Map the same file pages at the target instead (same aliasing result).
#include <cstdarg>
#include <fcntl.h>
extern "C" void* mremap(void* old,std::size_t old_size,std::size_t new_size,int flags,...) noexcept {
    va_list ap;va_start(ap,flags);void* target=(flags&MREMAP_FIXED)?va_arg(ap,void*):nullptr;va_end(ap);
    if(old_size==0 && (flags&MREMAP_FIXED)){
        FILE* maps=std::fopen("/proc/self/maps","r");char line[512];
        while(maps && std::fgets(line,sizeof line,maps)){unsigned long lo,hi,off;char perm[8];
            if(std::sscanf(line,"%lx-%lx %7s %lx",&lo,&hi,perm,&off)!=4 || reinterpret_cast<unsigned long>(old)<lo || reinterpret_cast<unsigned long>(old)>=hi)continue;
            std::fclose(maps);char path[64];std::snprintf(path,sizeof path,"/proc/self/map_files/%lx-%lx",lo,hi);const int fd=open(path,O_RDWR);if(fd<0)return MAP_FAILED;
            void* p=mmap(target,new_size,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_FIXED,fd,off+(reinterpret_cast<unsigned long>(old)-lo));close(fd);return p;}
        if(maps)std::fclose(maps);errno=EINVAL;return MAP_FAILED;}
    return reinterpret_cast<void*>(syscall(SYS_mremap,old,old_size,new_size,flags,target));
}
#endif
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
    const int reps=std::getenv("BENCH_REPS")?std::atoi(std::getenv("BENCH_REPS")):5;
    for(int rep=0;rep<reps;++rep){
        PpuState s;setup(s,code,''' + str(len(code)) + r''');s.pc=pc;s.r[1]=0x30ff00;s.r[3]=iters;s.lr=RETURN_SENTINEL;
        if(const char* f=std::getenv("BENCH_FPSCR"))s.fpscr=std::uint32_t(std::strtoul(f,nullptr,0));
        if(std::getenv("BENCH_THREADS")){s.kernel.semaphores.push_back({99,0,1});PpuContext other;other.thread_id=2;other.pc=pc;other.wait_kind=2;other.wait_id=99;
            s.ppu_threads={static_cast<PpuContext&>(s),other};}
        const auto t0=std::chrono::steady_clock::now();anyps3_entry(s,false);
        const double ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-t0).count();
        if(!s.returned){std::fprintf(stderr,"%s did not return\n",name);std::exit(2);}
        steps=s.steps;best=std::min(best,ns/double(s.steps));
        if(std::getenv("BENCH_INLINE_STATS")){const auto& c=s.memory.inline_counters;std::fprintf(stderr,"%s inline enabled=%d resumes=%llu blocked=%llu slow_loads=%llu slow_stores=%llu medium_loads=%llu medium_stores=%llu\n",
            name,int(s.memory.inline_enabled.load(std::memory_order_relaxed)),(unsigned long long)c.resumes,(unsigned long long)c.blocked_resumes,(unsigned long long)c.slow_loads,(unsigned long long)c.slow_stores,(unsigned long long)c.medium_loads,(unsigned long long)c.medium_stores);}
    }
    std::printf("%-10s %12llu instr  %.3f ns/instr\n",name,(unsigned long long)steps,best);
}
int main(){static const std::uint8_t code[]={''' + payload + '};\n' + '\n'.join(calls) + '\n}\n'
open(os.path.join(out, 'bench.cpp'), 'w').write(src)
t = time.time()
subprocess.run(['c++', '-std=c++17', *flags, *(['-g', '-DBENCH_MREMAP_SHIM'] if div else []), '-pthread', os.path.join(out, 'bench.cpp'), '-o', os.path.join(out, 'bench')], check=True)
print(f'compiled in {time.time()-t:.0f}s', file=sys.stderr)
size = subprocess.run(['size', '-A', os.path.join(out, 'bench')], capture_output=True, text=True).stdout
pages = subprocess.run(['nm', '-S', '--defined-only', os.path.join(out, 'bench')], capture_output=True, text=True).stdout
page_bytes = sum(int(f[1], 16) for f in (l.split() for l in pages.splitlines()) if len(f) == 4 and re.fullmatch(r'_Z\d+page_[0-9a-f]+R8PpuState(\.cold)?', f[3]))
print(f'page functions: {page_bytes} bytes host code for {translated.report()["translated_instructions"]} guest instructions', file=sys.stderr)
if not div:
    r = subprocess.run([os.path.join(out, 'bench')], capture_output=True, text=True, env=dict(os.environ, ANYPS3_PERF_REPORT='0'))
    print(r.stdout, r.stderr[-3000:])
    sys.exit(0)

src_lines = src.split('\n')
PAGE_FN = re.compile(r'page_[0-9a-f]+\(')
def category(fn, fl, line_no):
    # Page-function cost by the emitter construct on the line (ppu_emit.py / program.py one-line forms);
    # lines of inlined helpers (memory fast path, CR/branch helpers) count as block bodies.
    if fn.startswith('anyps3_dispatch_page'):
        return 'dispatch'
    if not PAGE_FN.match(fn):
        return 'runtime'
    t = src_lines[line_no - 1].strip() if fl.endswith('bench.cpp') and 0 < line_no <= len(src_lines) else ''
    if 'steps_v>=slice_v' in t or 'ppu_step_limit' in t or t.startswith('steps_v+='):
        return 'head'
    if t.startswith('switch (') or t.startswith('case 0x') or t.startswith('default: state.pc=pc_v') or t.startswith('ppu_dispatch_local') or \
            'const std::uint32_t offset=pc-base' in t:
        return 'switch'
    if t.startswith(('void page_', '[[maybe_unused]] auto& r=state.r', 'std::uint64_t steps_v=', 'std::uint64_t* const steps_p')) or \
            t.startswith('std::uint32_t pc_v=state.pc') or t.startswith('out:') or t == '}' or t.startswith(('[[maybe_unused]] const std::uint64_t head_v', '[[maybe_unused]] const bool* const resched_v')):
        return 'entry'
    if 'goto ppu_dispatch_local' in t or 'return ppu_dispatch(state)' in t or 'return page_' in t or t.startswith('if(pc_v==0x') or \
            t.startswith('{pc_v=0x') or 'ppu_ret_' in t:
        return 'exit'
    if 'anyps3_pages_' in t or 'const std::uint32_t page=state.pc>>12' in t or 'return anyps3_page_missing' in t:
        return 'dispatch'
    return 'body'

def parse(path):
    # Self cost per (file, line) and per function from an uncompressed callgrind file.
    per_line, per_fn, base, fl, fn, skip = {}, {}, '', '', '', False
    for raw in open(path):
        raw = raw.rstrip('\n')
        if raw.startswith('fl='):
            base = fl = raw[3:]
            continue
        if raw.startswith(('fi=', 'fe=')):
            fl = raw[3:]
            continue
        if raw.startswith('fn='):
            fn, fl = raw[3:], base
            continue
        if raw.startswith('calls='):
            skip = True
            continue
        if raw[:1].isdigit():
            if skip:
                skip = False
                continue
            parts = raw.split()
            if len(parts) >= 2:
                key = (fn, fl, int(parts[0]))
                per_line[key] = per_line.get(key, 0) + int(parts[1])
                per_fn[fn] = per_fn.get(fn, 0) + int(parts[1])
            continue
        if raw.startswith(('cfn=', 'cfl=', 'cfi=', 'cob=', 'ob=')):
            continue
    return per_line, per_fn

branch = bool(os.environ.get('BENCH_BRANCH'))
print(f'{"kernel":<11} {"Ir/instr":>8} {"body":>6} {"head":>6} {"entry":>6} {"switch":>6} {"exit":>6} {"disp":>6} {"runtime":>7}' +
      (f' {"Bcm":>6} {"Bi":>6} {"Bim":>6}' if branch else ''))
for name in sorted(n for n in syms if n.startswith('k_')):
    if os.environ.get('BENCH_ONLY') and os.environ['BENCH_ONLY'] != name:
        continue
    cg = os.path.join(out, f'callgrind.{name}')
    r = subprocess.run(['valgrind', '--tool=callgrind', '--toggle-collect=anyps3_entry*', '--compress-strings=no', '--compress-pos=no',
                        *(['--branch-sim=yes'] if branch else []),
                        f'--callgrind-out-file={cg}', os.path.join(out, 'bench')], capture_output=True, text=True,
                       env=dict(os.environ, ANYPS3_PERF_REPORT='0', BENCH_ONLY=name, BENCH_REPS='1'))
    m = re.search(r'(\d+) instr', r.stdout)
    if r.returncode or not m:
        print(name, 'failed', r.stdout, r.stderr[-2000:])
        continue
    steps = int(m.group(1))
    per_line, per_fn = parse(cg)
    split = {}
    for (fn, fl, line), ir in per_line.items():
        cat = category(fn, fl, line)
        split[cat] = split.get(cat, 0) + ir
    total = sum(split.values())
    cols = ' '.join(f'{split.get(c, 0) / steps:6.2f}' for c in ('body', 'head', 'entry', 'switch', 'exit', 'dispatch'))
    events = {}
    if branch:  # callgrind's simulated predictor: mispredicted conditional, indirect executed / mispredicted
        lines = open(cg).read().splitlines()
        names = next(l.split()[1:] for l in lines if l.startswith('events:'))
        events = dict(zip(names, map(int, next(l.split()[1:] for l in lines if l.startswith(('summary:', 'totals:'))))))
    print(f'{name:<11} {total / steps:8.2f} {cols} {split.get("runtime", 0) / steps:7.2f}' +
          (''.join(f' {events.get(e, 0) / steps:6.3f}' for e in ('Bcm', 'Bi', 'Bim')) if branch else ''))
    if os.environ.get('BENCH_FUNCS'):
        for fn, ir in sorted(per_fn.items(), key=lambda kv: -kv[1])[:8]:
            print(f'    {ir / steps:7.3f}  {fn[:110]}')
