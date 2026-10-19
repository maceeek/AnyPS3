"""The host VSH raster survives actual Vulkan transfer/readback unchanged."""
import os
import struct
import zlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(),'Requires Vulkan and glslang')
class SavedataOverlayTests(unittest.TestCase):
    def test_overlay_raster_vulkan_upload_readback_and_hidden_identity(self):
        source=compile_program(ElfImage.parse(program(BLR)),process_entry=True).cpp()
        source=source.replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')
        def chunk(tag, data):
            return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>2I5B',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(bytes([0,17,34,51])))+chunk(b'IEND',b'')
        source+=r''' 
#include <cassert>
int main(){
 using namespace anyps3_vsh_overlay;
 std::vector<std::uint8_t> guest(640*480*4,77),frame=guest;
 publish({});composite(frame,640,480);assert(frame==guest);
 auto icon=decode_icon(std::vector<std::uint8_t>{PNG_FIXTURE});assert(icon.width==1 && icon.height==1 && icon.pixels==std::vector<std::uint8_t>({17,34,51,255}));
 Snapshot s;s.visible=true;s.lines={"SAVE DATA","> SLOT ONE","DETAIL","2026-10-17"};s.selected=1;publish(s);
 auto rgba=raster(640,480),bgra=raster(640,480,true);
 assert(rgba.x==16 && rgba.width==608 && rgba.y>0 && rgba.pixels.size()==bgra.pixels.size());
 // Independent fixed colour oracle: opaque panel border RGB=(96,160,240).
 assert(rgba.pixels[0]==96 && rgba.pixels[1]==160 && rgba.pixels[2]==240 && rgba.pixels[3]==255);
 assert(bgra.pixels[0]==240 && bgra.pixels[1]==160 && bgra.pixels[2]==96 && bgra.pixels[3]==255);
 composite(frame,640,480);assert(guest==std::vector<std::uint8_t>(640*480*4,77));
 assert(frame[0]==77 && frame[(rgba.y*640+rgba.x)*4]==96);
 NativeVulkan vk;auto batch=vk.graphics_batch();
 auto image=vk.graphics_image(*batch,VK_FORMAT_R8G8B8A8_UNORM,rgba.width,rgba.height,1);
 vk.graphics_transfer(*batch,image,VK_IMAGE_ASPECT_COLOR_BIT,4,rgba.pixels.data());
 const auto at=vk.graphics_transfer(*batch,image,VK_IMAGE_ASPECT_COLOR_BIT,4);
 vk.graphics_finish(*batch);
 assert(std::memcmp(static_cast<char*>(batch->map)+at,rgba.pixels.data(),rgba.pixels.size())==0);
 publish({});assert(raster(640,480).pixels.empty());std::puts("PASS savedata overlay Vulkan readback");
}
'''
        source=source.replace('PNG_FIXTURE',','.join(map(str,png)))
        with tempfile.TemporaryDirectory() as tmp:
            cpp=Path(tmp)/'overlay.cpp';exe=Path(tmp)/'overlay';cpp.write_text(source)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread',
                                  '-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),'-lvulkan','-lglslang','-lSPIRV',
                                  '-lglslang-default-resource-limits','-o',str(exe)],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe',
                     VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_PRESENT',None)
            run=subprocess.run([str(exe)],env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('PASS savedata overlay Vulkan readback',run.stdout)
            self.assertNotIn('Validation Error',run.stdout+run.stderr)
            self.assertNotIn('Validation Warning',run.stdout+run.stderr)
