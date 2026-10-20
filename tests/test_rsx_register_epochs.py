"""RSX register bank change tracking vs the Session's serialized register blocks.

Expectations are the literal register values written by the test; the rebuild
counter shows which blocks the Session re-derived. Exposed (mutably borrowed)
blocks must still be verified by value on every refresh.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void epochs_(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*s.rsx.renderer,s);std::vector<unsigned> data;
 auto& bank=s.rsx.registers;
 // A fresh bank copy: no block is exposed, so only tracked writes change epochs.
 RsxRegisters copy=bank;bank=copy;for(unsigned b=0;b<RsxRegisters::blocks;++b)assert(!bank.block_exposed(b) || copy.block_exposed(b));
 bank.exposed.fill(0);
 session.serialize_registers(data,8,8);const auto base=session.register_blocks_rebuilt;
 // Unchanged bank: no block is rebuilt.
 session.serialize_registers(data,8,8);assert(session.register_blocks_rebuilt==base);
 // A tracked write with the same value does not bump the epoch.
 assert(bank.count(0x208));const auto e2=bank.epochs[0x208/256];bank.write(0x208,std::as_const(bank).at(0x208));
 assert(bank.epochs[0x208/256]==e2);session.serialize_registers(data,8,8);assert(session.register_blocks_rebuilt==base);
 // Tracked FIFO-style writes in two different blocks.
 bank.write(0x1d00,0x12345678);bank.write(0x404,0xcafef00d);
 session.serialize_registers(data,8,8);
 assert(data[16+0x1d00/4]==0x12345678 && data[16+0x404/4]==0xcafef00d);
 assert(session.register_blocks_rebuilt==base+2);
 // erase restores the default/zero; clear marks every block changed.
 bank.erase(0x404);session.serialize_registers(data,8,8);assert(data[16+0x404/4]==0 && session.register_blocks_rebuilt==base+3);
 // A mutable borrow exposes its block: later writes through the retained
 // reference (no further bank call) are still observed.
 auto& retained=bank[0x1d04];retained=7;session.serialize_registers(data,8,8);assert(data[16+0x1d04/4]==7);
 assert(bank.block_exposed(0x1d04/256));
 retained=9;session.serialize_registers(data,8,8);assert(data[16+0x1d04/4]==9);
 // Copy assignment gives a new identity: the session re-derives every block.
 RsxRegisters other=bank;other.write(0x1d00,0xabcdef01);const auto before=session.register_blocks_rebuilt;
 bank=other;session.serialize_registers(data,8,8);assert(data[16+0x1d00/4]==0xabcdef01 && data[16+0x1d04/4]==9);
 assert(session.register_blocks_rebuilt>before);
 // The FIFO decoder writes through the tracked path: a method write is visible.
 const auto clear_before=session.register_blocks_rebuilt;bank.clear();session.serialize_registers(data,8,8);
 assert(data[16+0x1d00/4]==0 && data[16+0x308/4]==0x207 && session.register_blocks_rebuilt>clear_before);
 std::puts("PASS tracked epochs, exposed borrows, identity on copy and clear/erase");
}
int main(){try{epochs_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class RsxRegisterEpochs(unittest.TestCase):
    def test_register_epochs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cpp = root/'test.cpp'
            cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(cpp), *LIBRARIES, '-o', str(root/'test')],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            env = dict(os.environ, LP_NUM_THREADS='2', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            run = subprocess.run([str(root/'test')], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('PASS tracked epochs', run.stdout)
