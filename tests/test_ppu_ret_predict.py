"""Host-call return prediction of translated pages (ppu_emit.py ppu_ret_*).

Guest programs are encoded here from the PowerPC instruction formats; the
expected registers and step counts follow from the programs alone (counted
by hand per path), not from the emitter:

* 0x10000: 1000 cross-page bl/blr pairs (f: r3=(3*r3+1)&0xffff); calls are in
  flight at many 256-step slice cuts, whose returns cannot be predicted;
* 0x10100: the callee returns to LR+8 instead of its continuation (the
  prediction must be rejected: the two skipped addi never run);
* 0x10200: 100-deep recursion alternating between two pages through bl,
  beqlr and blr (deeper than the 64-entry stack, so the full-stack tail path
  and the later returns without host frames are used);
* 0x10300: a virtual-call-shaped bctrl into another page;
* 0x10400: the callee tail-branches (b) to a third page, which returns.
"""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.program import compile_program
from anyps3.prx import make_image


def d_form(op, rt, ra, imm):
    return (op << 26) | (rt << 21) | (ra << 16) | (imm & 0xffff)


def addi(rt, ra, imm): return d_form(14, rt, ra, imm)
def li(rt, imm): return addi(rt, 0, imm)
def mulli(rt, ra, imm): return d_form(7, rt, ra, imm)
def cmpwi(ra, imm): return d_form(11, 0, ra, imm)
def mflr(rt): return 0x7c0802a6 | (rt << 21)
def mtlr(rs): return 0x7c0803a6 | (rs << 21)
def mtctr(rs): return 0x7c0903a6 | (rs << 21)
def rlwinm(ra, rs, sh, mb, me): return (21 << 26) | (rs << 21) | (ra << 16) | (sh << 11) | (mb << 6) | (me << 1)
def b(pc, target, link=False): return (18 << 26) | ((target - pc) & 0x3fffffc) | int(link)
def bl(pc, target): return b(pc, target, True)
def bc(pc, bo, bi, target): return (16 << 26) | (bo << 21) | (bi << 16) | ((target - pc) & 0xfffc)


BLR, BEQLR, BCTRL = 0x4e800020, 0x4d820020, 0x4e800421
STDU_R1_M32, STD_R0_48, LD_R0_48 = (62 << 26) | (1 << 21) | (1 << 16) | (-32 & 0xfffc) | 1, (62 << 26) | (1 << 16) | 48, (58 << 26) | (1 << 16) | 48


def recursion(page, other):
    """rec: if r3==0 return; r3-=1; r6+=1; call the twin in `other`; r7+=1."""
    return [cmpwi(3, 0), BEQLR, mflr(0), STDU_R1_M32, STD_R0_48, addi(3, 3, -1), addi(6, 6, 1),
            bl(page + 0x1c, other), LD_R0_48, addi(1, 1, 32), mtlr(0), addi(7, 7, 1), BLR]


CODE = {
    0x10000: [mflr(31), li(3, 0), li(4, 0), bl(0x1000c, 0x11000), addi(4, 4, 1), cmpwi(4, 1000), bc(0x10018, 12, 0, 0x1000c),
              mtlr(31), BLR],
    0x10100: [mflr(31), li(3, 0), bl(0x10108, 0x11100), addi(3, 3, 100), addi(3, 3, 1000), addi(3, 3, 7), mtlr(31), BLR],
    0x10200: [mflr(31), li(3, 100), li(6, 0), li(7, 0), bl(0x10210, 0x12000), mtlr(31), BLR],
    0x10300: [mflr(31), li(3, 1), d_form(15, 9, 0, 1), d_form(24, 9, 9, 0x1200), mtctr(9), BCTRL, addi(3, 3, 10), mtlr(31), BLR],
    0x10400: [mflr(31), li(3, 2), bl(0x10408, 0x11300), addi(3, 3, 1), mtlr(31), BLR],
    0x11000: [mulli(3, 3, 3), addi(3, 3, 1), rlwinm(3, 3, 0, 16, 31), BLR],
    0x11100: [addi(3, 3, 5), mflr(5), addi(5, 5, 8), mtlr(5), BLR],
    0x11200: [addi(3, 3, 3), BLR],
    0x11300: [addi(3, 3, 4), b(0x11304, 0x12100)],
    0x12000: recursion(0x12000, 0x13000),
    0x12100: [mulli(3, 3, 5), BLR],
    0x13000: recursion(0x13000, 0x12000),
}


def expected():
    f = 0
    for _ in range(1000):
        f = (3 * f + 1) & 0xffff
    return dict(
        calls=(f, 8 * 1000 + 5),           # mflr li li + 1000 x (bl, f 4, addi cmpwi blt) + mtlr blr
        wrong=(5 + 7, 3 + 5 + 3),          # mflr li bl + f2 5 + addi(7) mtlr blr
        deep=(13 * 100 + 2 + 5 + 2),       # 100 levels x 13 + last cmpwi/beqlr + main 5 + mtlr blr
        bctrl=(1 + 3 + 10, 6 + 2 + 3),
        tail=((2 + 4) * 5 + 1, 3 + 2 + 2 + 3))


MAIN = r'''
#include <cassert>
static void map_all(PpuState& s){@MAPS@ s.memory.map(0x300000,0x10000,6,nullptr,0);}
static PpuState& run(PpuState& s,std::uint32_t pc){map_all(s);s.pc=pc;s.lr=RETURN_SENTINEL;s.r[1]=0x30ff00;anyps3_entry(s,false);
    assert(s.returned && s.lr==RETURN_SENTINEL && s.ppu_ret_depth==0);return s;}
int main(){
    {PpuState s;run(s,0x10000);assert(s.r[3]==@F@ && s.r[4]==1000 && s.steps==@CALLS@);}
    {PpuState s;run(s,0x10100);assert(s.r[3]==@WRONG3@ && s.steps==@WRONG@);}
    {PpuState s;run(s,0x10200);assert(s.r[3]==0 && s.r[6]==100 && s.r[7]==100 && s.r[1]==0x30ff00 && s.steps==@DEEP@);}
    {PpuState s;run(s,0x10300);assert(s.r[3]==@BCTRL3@ && s.ctr==0x11200 && s.steps==@BCTRL@);}
    {PpuState s;run(s,0x10400);assert(s.r[3]==@TAIL3@ && s.steps==@TAIL@);}
    std::puts("ok");
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class PpuReturnPrediction(unittest.TestCase):
    def test_predicted_returns_are_exact(self):
        segments, maps = [], []
        for page in sorted({a & ~0xfff for a in CODE}):
            words = bytearray(0x1000)
            for address, program in CODE.items():
                if address & ~0xfff == page:
                    words[address - page:address - page + 4 * len(program)] = struct.pack(f'>{len(program)}I', *program)
            segments.append((page, 5, bytes(words)))
            maps.append(f'{{static const std::uint8_t c[]={{{",".join(str(x) for x in words)}}};s.memory.map(0x{page:x},0x1000,5,c,sizeof(c));}}')
        segments.append((0x40000, 6, struct.pack('>II', 0x10000, 0)))
        entries = (0x10000, 0x10100, 0x10200, 0x10300, 0x10400, 0x10114, 0x11200)
        source = compile_program(make_image(segments, entry=0x40000), process_entry=True, extra_entries=entries).cpp().replace(
            'int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        self.assertIn('ppu_ret_push(state,0x10010u)', source)    # cross-page bl
        self.assertIn('ppu_ret_push(state,0x10318u)', source)    # bctrl
        self.assertIn('ppu_ret_pop(state,pc_v)', source)
        e = expected()
        main = MAIN.replace('@MAPS@', ''.join(maps))
        for key, value in dict(F=e['calls'][0], CALLS=e['calls'][1], WRONG3=e['wrong'][0], WRONG=e['wrong'][1], DEEP=e['deep'],
                               BCTRL3=e['bctrl'][0], BCTRL=e['bctrl'][1], TAIL3=e['tail'][0], TAIL=e['tail'][1]).items():
            main = main.replace(f'@{key}@', str(value))
        source += main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
                                    str(root/'test.cpp'), '-o', str(root/'test')], capture_output=True, text=True, timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            run = subprocess.run([str(root/'test')], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
            self.assertEqual(run.returncode, 0, run.stderr[-4000:])
            self.assertIn('ok', run.stdout)


if __name__ == '__main__':
    unittest.main()
