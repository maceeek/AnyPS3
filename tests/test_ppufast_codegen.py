"""PPUFAST: translated PPU memory forms through the inline view, serial and parallel.

Instruction words are canonical encodings; expected registers and bytes are
literal values (big-endian guest order), independent of the lifter.
"""
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.program import compile_program
from anyps3.prx import make_image

BLR = 0x4E800020
CODE = {
    0x10000: [0x3c800002,  # lis r4,2
              0xe8a40000,  # ld r5,0(r4)
              0x80c40004,  # lwz r6,4(r4)
              0xa8e40008,  # lha r7,8(r4)
              0x8904000f,  # lbz r8,15(r4)
              0x7d20242c,  # lwbrx r9,0,r4
              0x90c40020,  # stw r6,0x20(r4)
              0xb0e40024,  # sth r7,0x24(r4)
              0x99040026,  # stb r8,0x26(r4)
              0xf8a40028,  # std r5,0x28(r4)
              0x39440030,  # addi r10,r4,0x30
              0x7cc0552c,  # stwbrx r6,0,r10
              0xc0240010,  # lfs f1,0x10(r4)
              0xd8240038,  # stfd f1,0x38(r4)
              0x7c2020ce,  # lvx v1,0,r4
              0x39640040,  # addi r11,r4,0x40
              0x7c2059ce,  # stvx v1,0,r11
              0x39840080,  # addi r12,r4,0x80
              0x7c0067ec,  # dcbz 0,r12
              0x81e40001,  # lwz r15,1(r4) (unaligned, same page)
              0x7da02028,  # lwarx r13,0,r4
              0x39ad0001,  # addi r13,r13,1
              0x7da0212d,  # stwcx. r13,0,r4
              0x81c40ffe,  # lwz r14,0xffe(r4) (crosses a guest page)
              BLR],
    0x11000: [0x38600001, 0x3e007000, 0x80700000, BLR],   # li r3,1; lis r16,0x7000; lwz r3,0(r16)
    0x12000: [0x3e200001, 0x90710000, BLR],               # lis r17,1; stw r3,0(r17) into code
    # Cross-page call/return loop across many scheduling slices:
    # li r3,0; li r4,0; mflr r31; loop: bl 0x14000; addi r4,r4,1; cmpwi r4,1000; blt loop; mtlr r31; blr
    0x13000: [0x38600000, 0x38800000, 0x7fe802a6, 0x48000ff5, 0x38840001, 0x2c0403e8, 0x4180fff4, 0x7fe803a6, BLR],
    0x14000: [0x38630003, BLR],                           # addi r3,r3,3; blr
}


def data():
    d = bytearray(0x2000)
    d[0:16] = bytes.fromhex('0123456789abcdeffedcba9876543210')
    d[0x10:0x14] = bytes.fromhex('3f800000')
    d[0x80:0x100] = b'\x5a' * 0x80
    d[0xffe:0x1002] = bytes.fromhex('aabbccdd')
    return bytes(d)


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuFastCodegen(unittest.TestCase):
    def test_memory_forms_serial_and_parallel(self):
        segments = [(a, 5, struct.pack('>' + str(len(w)) + 'I', *w)) for a, w in CODE.items()]
        image = make_image(segments + [(0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        translated = compile_program(image, process_entry=True, extra_entries=tuple(CODE))
        source = translated.cpp().replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        self.assertIn('ppu_try_load<8>(state,ppu_a,ppu_v)', source)
        self.assertNotIn('state.memory.load(ea', source.split('void anyps3_entry')[0].split('void page_')[-1])
        payload = ','.join(str(b) for b in data())
        code = ','.join(str(b) for b in segments[0][2])
        source += r'''
#include <cassert>
static void setup(PpuState& s,bool parallel){
    static const std::uint8_t bytes[]={''' + payload + r'''};
    static const std::uint8_t code[]={''' + code + r'''};
    s.memory.map(0x10000,0x3000,7,code,sizeof(code));s.memory.map(0x20000,sizeof(bytes),6,bytes,sizeof(bytes));
    if(parallel){s.memory.enable_concurrency();s.memory.enable_page_broker();}
}
static void check(bool parallel){
    {PpuState s;setup(s,parallel);s.pc=0x10000;anyps3_entry(s,false);
    assert(s.returned && s.steps==25);
    assert(s.r[5]==UINT64_C(0x0123456789abcdef) && s.r[6]==0x89abcdef && s.r[7]==UINT64_C(0xfffffffffffffedc));
    assert(s.r[8]==0x10 && s.r[9]==0x67452301 && s.r[15]==0x23456789 && s.r[14]==0xaabbccdd);
    assert(s.r[13]==0x01234568 && (s.cr>>28)==2 && s.fpr[1]==UINT64_C(0x3ff0000000000000));
    const std::uint8_t expect20[]={0x89,0xab,0xcd,0xef,0xfe,0xdc,0x10,0x00,0x01,0x23,0x45,0x67,0x89,0xab,0xcd,0xef,
                                   0xef,0xcd,0xab,0x89,0,0,0,0,0x3f,0xf0,0,0,0,0,0,0};
    for(unsigned i=0;i<32;++i)assert(s.memory.load(0x20020+i,1)==expect20[i]);
    const std::uint8_t first[]={0x01,0x23,0x45,0x68,0x89,0xab,0xcd,0xef,0xfe,0xdc,0xba,0x98,0x76,0x54,0x32,0x10};
    for(unsigned i=0;i<16;++i)assert(s.memory.load(0x20000+i,1)==first[i] && s.memory.load(0x20040+i,1)==(i==3?0x67:first[i]));
    for(unsigned i=0;i<0x80;++i)assert(s.memory.load(0x20080+i,1)==0);
    assert(s.memory.load(0x20100,1)==0 && s.memory.load(0x2007f,1)==0);
    if(parallel){const auto& c=s.memory.inline_counters;
        std::printf("resumes=%llu slow_loads=%llu slow_stores=%llu medium=%llu/%llu\n",(unsigned long long)c.resumes,(unsigned long long)c.slow_loads,
            (unsigned long long)c.slow_stores,(unsigned long long)c.medium_loads,(unsigned long long)c.medium_stores);
        assert(c.resumes>=1 && c.slow_loads==1 && c.slow_stores==0 && !s.memory.inline_view.active);}}
    {PpuState s;setup(s,parallel);s.pc=0x13000;anyps3_entry(s,false);
    std::fprintf(stderr,"loop parallel=%d returned=%d r3=%llu r4=%llu steps=%llu lr=%llx cr=%x pc=%x\n",int(parallel),int(s.returned),(unsigned long long)s.r[3],(unsigned long long)s.r[4],(unsigned long long)s.steps,(unsigned long long)s.lr,s.cr,s.pc);
    assert(s.returned && s.r[3]==3000 && s.r[4]==1000 && s.steps==6005 && s.lr==RETURN_SENTINEL && (s.cr>>28)==2);}
    // Exact stop: the faulting instruction is counted and reports its own PC.
    {PpuState s;setup(s,parallel);s.pc=0x11000;bool failed=false;
    try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("unmapped")!=std::string::npos;}
    assert(failed && s.pc==0x11008 && s.steps==3 && s.r[3]==1 && !s.returned && !s.memory.inline_view.active);}
    {PpuState s;setup(s,parallel);s.pc=0x12000;s.r[3]=5;bool failed=false;
    try{anyps3_entry(s,false);}catch(const std::runtime_error& e){failed=std::string(e.what()).find("self-modifying")!=std::string::npos;std::fprintf(stderr,"smc parallel=%d what=%s pc=%x steps=%llu\n",int(parallel),e.what(),s.pc,(unsigned long long)s.steps);}
    std::fprintf(stderr,"after smc failed=%d pc=%x steps=%llu returned=%d\n",int(failed),s.pc,(unsigned long long)s.steps,int(s.returned));
    assert(failed && s.pc==0x12004 && s.steps==2 && s.memory.load(0x10000,4)==0x3c800002);}
}
int main(){check(false);check(true);std::puts("ok");}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', str(root / 'test.cpp'),
                                    '-o', str(root / 'test')], capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr[-8000:])
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True, timeout=60,
                                 env={'SDL_AUDIODRIVER': 'dummy', 'ANYPS3_AUDIO_SINK': 'null', 'PATH': '/usr/bin:/bin'})
            self.assertEqual(run.returncode, 0, run.stderr[-8000:] + run.stdout[-2000:])
            self.assertIn('ok', run.stdout)


if __name__ == '__main__':
    unittest.main()
