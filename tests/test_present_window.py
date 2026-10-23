"""FIX7: one presentation window. A scanout without a GPU-resident surface goes to the Vulkan WSI window.

Before the fix such frames opened the SDL renderer sink ("AnyPS3 guest scanout") next to the Vulkan WSI window
("AnyPS3 Vulkan guest scanout"); run 805au1 listed both windows for the whole run. Real SDL (offscreen video driver,
no visible window) and real Vulkan (lavapipe) are used; nothing is mocked.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
int main(){try{
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 setenv("ANYPS3_PRESENT","sdl",1);setenv("SDL_VIDEODRIVER","offscreen",1);
 PpuState s;setup_draw(s,32,16,128,false);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x80000000,0x10000,6,nullptr,0);g.dma=0x70008000;g.reports=0x80000000;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();
 g.registers[0x17cc]=1;g.registers[0x1a8]=0x66626660;
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<2;++i){packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);}
 packet(0x1800,1u<<24);m.store(g.dma+0x44,4,0x100);m.store(g.dma+0x40,4,put);
 assert(rsx_run(s,1024) && g.renderer && g.graphics_owner && s.present.enabled); // real FIFO work creates the Vulkan session
 // CPU-written X8R8G8B8 buffer outside every GPU surface (the draw target is 0x100000..0x100800).
 g.display_buffers[0]={2,1,8,0x180000};m.store(0xc0180000,4,0x00112233);m.store(0xc0180004,4,0x00445566);
 assert(!rsx_graphics_scanout(s,0,nullptr,true));                                 // no resident surface: CPU path
 rsx_present_display(s,0);rsx_present_display(s,0);
 assert(s.present.presented_frames==2);   // both presented through the Vulkan WSI ...
 assert(!s.present.window && !s.present.renderer && !s.present.subsystem);       // ... the SDL renderer window never opened
 static_cast<rsxgraphics::Session*>(g.graphics_owner.get())->retire(true);
 std::puts("PASS CPU scanout presented by the Vulkan WSI; no second window");
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class SingleWindowTests(unittest.TestCase):
    def test_cpu_scanout_uses_vulkan_wsi_window_not_sdl_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            cpp = Path(directory)/'present.cpp'; exe = cpp.with_suffix('')
            cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(cpp), *LIBRARIES, '-o', str(exe)],
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0, build.stderr[-12000:])
            env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                       VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', ANYPS3_SPU_WORKER='0', SDL_AUDIODRIVER='dummy')
            result = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-12000:])
            self.assertIn('PASS CPU scanout presented by the Vulkan WSI', result.stdout)
            self.assertIn('Vulkan WSI present mode=', result.stderr)
            self.assertNotIn('Validation Error', result.stderr)


if __name__ == '__main__':
    unittest.main()
