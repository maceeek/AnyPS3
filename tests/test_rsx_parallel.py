"""Native differential draws against the frozen sequential renderer.

Test-only probes establish worker execution and prohibit GuestMemory calls in
workers. Opt-in production counters are checked separately by test_rsx_perf_tiles.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_perf import DIFFERENTIAL_MAIN, native_source


# Reuse the frozen fixture's independent byte/version/error oracle, expanding
# its random geometries beyond the parallel threshold. Small cases remain in
# test_rsx_perf; the explicit cases below also assert fast/fallback dispatch.
# Its adjacent-region case also retains the fixed BE bytes and complete
# byte/version oracle, while only the frozen binder gets a smaller tile view.
LARGE_RANDOM = DIFFERENTIAL_MAIN.replace('int main(', 'static int random_cases(')
LARGE_RANDOM = LARGE_RANDOM.replace('width=17+n%67,height=9+n%39',
                                    'width=149+n%67,height=137+n%39')
LARGE_RANDOM = LARGE_RANDOM.replace('tiled?512:width*4+12',
                                    'tiled?((width*4+255)/256)*256:width*4+12')
LARGE_RANDOM = LARGE_RANDOM.replace('observe(m,n%3);', 'observe(m,n%4==0?2:0);')
# Renamed main is a normal function and must explicitly return.
LARGE_RANDOM = LARGE_RANDOM.rsplit('}', 1)[0] + 'return 0;\n}\n'

PARALLEL_MAIN = r'''
static void texture_draw(PpuState& s,unsigned variant=0){
    auto& m=s.memory;auto& R=s.rsx.registers;
    const unsigned words[]={(23u<<24)|(15u<<9)|(4u<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
    for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
    R[0x1a00]=0x8000;R[0x1a04]=0x00030029|(0xa5<<8);R[0x1a08]=0x0101+variant*0x101;
    R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a10]=0xaae4;
    R[0x1a14]=(6u<<16)|(2u<<24);R[0x1a18]=(8u<<16)|8;R[0x1840]=32;
    for(unsigned i=0;i<448;++i)(*m.regions[1].bytes)[0x8000+i]=(i*79+31)&255;
}
static void overlapping(PpuState& s){s.rsx.draw.ranges={{0,4},{0,4},{0,4}};}
static void check_dispatch(PpuState& s,unsigned id,bool eligible){
    const auto before=test_parallel_draws.load();test_worker_mask=0;
    differential(s,id);
    const auto threads=rsxdraw::draw_thread_count();
    const bool used=test_parallel_draws.load()!=before;
    assert(used==(eligible && threads>1));
    if(used)assert(test_worker_mask.load()==((UINT64_C(1)<<threads)-1));
}
int main(int argc,char** argv){
    assert(argc==2);const std::string mode=argv[1];
    if(mode=="random"){
        char a[]="draws";char* args[]={argv[0],a};
        assert(!random_cases(2,args));
        if(rsxdraw::draw_thread_count()>1)assert(test_parallel_draws>0);
    }else if(mode=="stripes"){
        for(unsigned n=0;n<48;++n){
            PpuState s;const bool tiled=n&1;const unsigned w=161+n%3,h=133+n%7;
            setup_draw(s,w,h,tiled?768:w*4+12,tiled);auto& R=s.rsx.registers;auto& m=s.memory;
            for(unsigned i=0;i<300000;++i){(*m.regions[1].bytes)[0x100000+i]=(i*37+n)&255;(*m.regions[1].bytes)[0x500000+i]=255;}
            overlapping(s); // Same pixels are blended/depth-tested three times.
            R[0xa70]=n%3!=0;R[0xa74]=n%4!=0;R[0xa6c]=0x200+n%8;
            R[0x304]=n%3==0;R[0x308]=0x200+(n/3)%8;R[0x30c]=(n*73)&255;
            R[0x324]=(n%16&1?0x01000000:0)|(n%16&2?0x00010000:0)|(n%16&4?0x100:0)|(n%16&8?1:0);
            R[0x310]=n%3!=0;R[0x314]=0x03020302;R[0x318]=0x03030303;
            const unsigned eq[]={0x8006,0x800a,0x800b,0x8007,0x8008};R[0x320]=eq[n%5]|(eq[(n+2)%5]<<16);
            R[0x8c0]=((w-3)<<16)|1;R[0x8c4]=((h-3)<<16)|1; // odd scissor start and end
            if(n%2)texture_draw(s,n%6);
            // PPU observers outside both targets are allowed, without versions
            // being written or even looked up by raster workers.
            if(n%3==0)m.reservation_token(0x70002000);
            check_dispatch(s,4000+n,true);
        }
        // Varying, half outputs, derivatives and helper lanes use the
        // same full quads across every stripe boundary.
        for(unsigned attr=0;attr<15;++attr)for(unsigned variant=0;variant<4;++variant){
            PpuState s;setup_draw(s,163,141,768,true);auto& m=s.memory;overlapping(s);
            const unsigned opcode=variant==2?21:variant==3?22:1;
            const unsigned words[]={(opcode<<24)|(15u<<9)|(attr<<13)|1u,1u|(0xe4u<<9)|(7u<<18),0,0};
            for(unsigned i=0;i<4;++i)m.store(0xc0001000+4*i,4,(words[i]<<16)|(words[i]>>16));
            if(variant==1)s.rsx.registers[0x1d60]=0;
            check_dispatch(s,5000+attr*4+variant,true);
        }
        // Exercise pool growth, shrink, reuse and a new thread-local renderer.
        const auto configured=rsxdraw::draw_thread_count();
        std::atomic<unsigned> jobs{0};rsxdraw::DrawPool pool;
        for(unsigned count:{1u,configured,2u,configured,1u,3u}){
            jobs=0;pool.run(count,[&](unsigned){++jobs;});assert(jobs==count);
        }
        for(unsigned round=0;round<5;++round){
            std::string message;
            try{pool.run(7,[&](unsigned i){if(i==1 || i==5)throw std::runtime_error(std::to_string(i));});}
            catch(const std::runtime_error& e){message=e.what();}
            assert(message=="1"); // independent of which worker throws first
        }
        jobs=0;pool.run(3,[&](unsigned){++jobs;});assert(jobs==3);
        for(const auto mode:{FE_DOWNWARD,FE_UPWARD,FE_TOWARDZERO,FE_TONEAREST}){
            assert(!std::fesetround(mode));
            pool.run(3,[&](unsigned){assert(std::fegetround()==mode);});
            PpuState s;setup_draw(s,163,141,768,true);texture_draw(s);
            check_dispatch(s,5800+mode,true);
        }
        std::thread nested([]{PpuState s;setup_draw(s,163,141,768,true);differential(s,5999);});nested.join();
        std::puts("PASS 112 forced parallel draws, overlap, quads, rounding, pool reuse and exceptions");
    }else if(mode=="fallback"){
        for(unsigned n=0;n<31;++n){
            PpuState s;setup_draw(s,163,141,768,true);auto& m=s.memory;auto& R=s.rsx.registers;auto& g=s.rsx;
            R[0xa70]=1;R[0xa6c]=0x207;overlapping(s);
            bool must_error=false;
            if(n==0)observe(m,1);
            if(n==1)m.reservation_token(0xc0100080);
            if(n==2)m.reservation_token(0xc0500080);
            if(n==3)R[0x214]=R[0x210];
            if(n==4){R[0x214]=R[0x210]+4;g.tiles[1].bound=false;}
            if(n==5){texture_draw(s);R[0x1a00]=R[0x210];g.tiles[0].bound=false;}
            if(n==6){texture_draw(s);R[0x1a00]=R[0x214];g.tiles[1].bound=false;}
            if(n==7){ // Main guest mapping aliases a writable target host allocation.
                auto original=m.regions[0].bytes;const auto base=m.regions[0].base;
                m.regions[0].bytes=m.regions[1].bytes;m.regions[0].base=0x6ff00000;
                std::copy(original->begin(),original->end(),m.regions[0].bytes->begin()+base-m.regions[0].base);
            }
            if(n==8){R[0x310]=1;R[0x314]=1;R[0x318]=0;R[0x320]=0;must_error=true;}
            if(n==9){R[0xa74]=1;R[0xa6c]=0;must_error=true;}
            if(n==10){R[0x304]=1;R[0x308]=0;must_error=true;}
            if(n==11){texture_draw(s);R[0x1a08]=0x0707;must_error=true;}
            if(n==12){g.tiles[0].size=768*141+7;g.tiles[0].bank=0;must_error=true;}
            if(n==13){g.tiles[0].bank=4;must_error=true;}
            if(n==14){g.tiles[0].pitch=1024;must_error=true;}
            if(n==15){m.regions[1].bytes->resize(0x200000);must_error=true;}
            if(n==16){m.regions[1].flags=4;must_error=true;}
            if(n==17){m.regions[1].flags=7;must_error=true;}
            if(n==18){ // A later triangle faults after earlier triangles wrote.
                g.draw.primitive=5;g.draw.ranges={{0,3},{0,3},{4096,3}};must_error=true;
            }
            if(n==19){ // Unsupported FP opcode, lazily faults in first quad.
                const unsigned w=(127u<<24)|(15u<<9)|1u;
                m.store(0xc0001000,4,(w<<16)|(w>>16));must_error=true;
            }
            if(n==20){const unsigned w=(1u<<24)|(15u<<9)|(15u<<13)|1u;
                const unsigned src=1u|(0xe4u<<9)|(7u<<18);
                m.store(0xc0001000,4,(w<<16)|(w>>16));m.store(0xc0001004,4,(src<<16)|(src>>16));must_error=true;}
            if(n==21){R[0x8c4]=(17u<<16)|3;}
            if(n==22){texture_draw(s);R[0x1840]=4;must_error=true;}
            if(n==23){R[0x328]=1;must_error=true;}
            if(n==24){ // Bad later vertex read must keep the committed prefix.
                for(unsigned i=0;i<3;++i)for(unsigned c=0;c<4;++c)
                    m.store(0x7000ffc0+i*16+c*4,4,m.load(0x70002000+i*16+c*4,4));
                R[0x1680]=0x8000ffc0;g.draw.primitive=5;g.draw.ranges={{0,3},{4,3}};must_error=true;
            }
            if(n==25){R[0x310]=1;R[0x314]=0x99999999;R[0x318]=0;R[0x320]=0x80068006;must_error=true;}
            if(n==26){ // Unsupported scale and full register >47.
                m.store(0xc0001008,4,0x00004000);must_error=true;
            }
            if(n==27){const unsigned w=(1u<<24)|(15u<<9)|(63u<<1)|1u;
                m.store(0xc0001000,4,(w<<16)|(w>>16));must_error=true;}
            if(n==28){R[0x8c0]=(3u<<16)|3;}
            if(n==29){texture_draw(s);m.regions[1].bytes->resize(0x8080);must_error=true;}
            if(n==30){ // Tiny geometry in a large scissor must stay sequential.
                for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c){
                    const float f=(i==1 || i==2) && !c?3.f:c && i>=2?3.f:1.f;
                    std::uint32_t b;std::memcpy(&b,&f,4);m.store(0x70002000+i*16+c*4,4,b);
                }
            }
            const auto initial=snapshot(s);const auto count=test_parallel_draws.load();
            const auto error=differential(s,6000+n,0,n==5 || n==6);
            if(test_parallel_draws.load()!=count){std::fprintf(stderr,"unexpected parallel fallback case %u\n",n);std::abort();}
            if(must_error)assert(!error.empty());
            if(n==8 || n==12 || n==18 || n==24)assert(snapshot(s).bytes!=initial.bytes);
        }
        // Reuse legacy fault/address coverage, with exact errors and versions.
        char faults[]="faults";char* args[]={argv[0],faults};assert(!random_cases(2,args));
        std::puts("PASS 31 forced fallbacks plus adjacent-region draw and 13 legacy errors, exact partial memory and versions");
    }else if(mode=="invalid"){
        PpuState s;setup_draw(s,163,141,768,true);const auto initial=snapshot(s);
        const auto error=execute(s,false);
        assert(error=="invalid ANYPS3_RSX_THREADS: expected positive integer");
        const auto after=snapshot(s);assert(after.bytes==initial.bytes && after.versions==initial.versions);
        assert(after.draw.triangles==0 && after.draw.draws==0 && after.draw.ranges==initial.draw.ranges);
        assert(after.last_region==initial.last_region);std::puts("PASS explicit invalid thread count before draw mutation");
    }else if(mode=="default"){
        assert(rsxdraw::draw_thread_count()==std::max(1u,std::thread::hardware_concurrency()));
        std::puts("PASS hardware concurrency default");
    }else return 2;
    return 0;
}
'''


def parallel_source(body=PARALLEL_MAIN, *, probes=True):
    source = native_source(LARGE_RANDOM + body)
    if not probes:
        return source
    source = ('#include <atomic>\nstatic std::atomic<unsigned> test_parallel_draws{0};\n'
              'static std::atomic<unsigned long long> test_worker_mask{0};\n'
              'static thread_local bool test_rsx_worker=false;\n' + source)
    source = source.replace('static thread_local DrawPool pool;',
                            '++test_parallel_draws;static thread_local DrawPool pool;')
    source = source.replace('const auto count=rasterize(first,last,true,left,right);',
                            'if(i<64){test_worker_mask.fetch_or(1ull<<i);}'
                            'test_rsx_worker=true;const auto count=rasterize(first,last,true,left,right);'
                            'test_rsx_worker=false;')
    # These abort even if an accidental worker mark would touch an empty map.
    source = source.replace('void mark_write(const std::uint8_t* pointer,unsigned size) {',
                            'void mark_write(const std::uint8_t* pointer,unsigned size) {'
                            'if(test_rsx_worker)std::abort();')
    source = source.replace('std::uint8_t* access(std::uint64_t address, unsigned size, bool write) {',
                            'std::uint8_t* access(std::uint64_t address, unsigned size, bool write) {'
                            'if(test_rsx_worker)std::abort();')
    return source


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class RsxParallelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        source = cls.root / 'parallel.cpp'
        source.write_text(parallel_source())
        cls.binary = cls.root / 'parallel'
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-pthread', '-Wall',
                                '-Wextra', '-Werror', str(source), '-o', str(cls.binary)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            cls.directory.cleanup()
            raise AssertionError(build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_mode(self, mode, threads):
        env = dict(os.environ)
        if threads is None:
            env.pop('ANYPS3_RSX_THREADS', None)
        else:
            env['ANYPS3_RSX_THREADS'] = str(threads)
        run = subprocess.run([str(self.binary), mode], env=env, capture_output=True,
                             text=True, timeout=180)
        self.assertEqual(run.returncode, 0, f'{mode} threads={threads}\n' + run.stdout + run.stderr)
        self.assertIn('PASS', run.stdout)

    def test_random_and_boundary_draws(self):
        for count in (1, 2, 3, 7, 24):
            with self.subTest(threads=count):
                self.run_mode('random', count)

    def test_forced_parallel_stripes_overlap_and_pool(self):
        for count in (1, 2, 3, 7, 24):
            with self.subTest(threads=count):
                self.run_mode('stripes', count)

    def test_fallback_errors_and_partial_writes(self):
        for count in (1, 2, 3, 7, 24):
            with self.subTest(threads=count):
                self.run_mode('fallback', count)

    def test_thread_count_validation(self):
        for value in ('', '0', '-1', '+2', '2 ', ' 2', '1.5', 'abc', '4294967296',
                      '99999999999999999999999999999999'):
            with self.subTest(value=value):
                self.run_mode('invalid', value)
        self.run_mode('stripes', '0003')
        self.run_mode('default', None)
