"""Validate the integrated GuestMemory observer against native GPU writes.

The production GuestMemory provides the observer.
This covers fresh API access and retained Range operations, not arbitrary raw
pointers retained across a GPU draw. See memory-audit.md before integration.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from anyps3.runtime import MEMORY_CPP
from tests.test_rsx_vk import LIBRARIES
from tests.test_rsx_vk_resident import native_source

MAIN = r'''
int main(){try{
 PpuState gpu;resident_setup(gpu,8,0,1);gpu.rsx.renderer=std::make_unique<NativeVulkan>();auto& vk=*gpu.rsx.renderer;
 rsxvk::ResidentSession batch(vk,gpu);test_resident=&batch;setenv("ANYPS3_RSX_THREADS","4",1);setenv("ANYPS3_RSX_VK_TEST","1",1);
 // Retain Range before the GPU write; read() must still synchronize later.
 auto retained=gpu.memory.range(0xc0100000,8,false);rsx_execute_draw(gpu);
 unsigned callbacks=0;gpu.memory.host_access_observer=[&](const void* p,std::uint64_t size,bool write){++callbacks;batch.host_access(p,size,write);};
 std::uint8_t bytes[8]{};retained.read(0,bytes,8);assert(bytes[0]==0x80 && bytes[1]==0xff && bytes[2]==0x80 && bytes[3]==0x40);assert(callbacks>0);
 assert(gpu.memory.load(0xc0100000,4)==0x80ff8040);
 gpu.memory.store(0xc0100001,1,0x22);assert(batch.cache.surfaces.empty());assert(gpu.memory.load(0xc0100000,4)==0x80228040);
 // Atomic reservation access uses access_impl() too.
 auto reservation=gpu.memory.reservation_token(0xc0100000,4);const auto version=reservation.second;
 gpu.memory.store(0xc0100000,4,0x12345678);assert(gpu.memory.reservation_token(0xc0100000,4).second!=version);
 // Observer exceptions must not leave recursion suppression enabled.
 gpu.memory.host_access_observer=[](const void*,std::uint64_t,bool){throw std::runtime_error("observer failure");};
 bool failed=false;try{gpu.memory.load(0xc0100000,4);}catch(const std::runtime_error&){failed=true;}assert(failed);
 callbacks=0;gpu.memory.host_access_observer=[&](const void*,std::uint64_t,bool){++callbacks;};
 assert(gpu.memory.load(0xc0100000,4)==0x12345678 && callbacks>0);gpu.memory.host_access_observer={};
 batch.flush();test_resident=nullptr;std::puts("PASS integrated observer: retained Range, BE load/store, reservation, exception recovery");
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

@unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
class IntegratedMemoryHookTests(unittest.TestCase):
    def test_integrated_hook_with_actual_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=native_source()
            self.assertEqual(source.count(MEMORY_CPP),1)
            source=source.replace('int main(int argc,char** argv)', 'int resident_test_main(int argc,char** argv)')
            source=source.replace('coherence();depth_cases();nonfinite_batch();snapshot_sampling();transport_guards();half_round_cases();','coherence();depth_cases();nonfinite_batch();snapshot_sampling();transport_guards();half_round_cases();return 0;')
            (root/'test.cpp').write_text(source+'\n'+MAIN)
            result=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stderr)
            env=dict(os.environ,VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=90)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stdout+run.stderr)
            self.assertNotIn('Validation Warning',run.stdout+run.stderr)
