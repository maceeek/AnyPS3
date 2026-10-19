"""Main-thread WSI transport: native exception, lifetime and dispatch oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES
MAIN=r'''
int main(){
 const auto owner=std::this_thread::get_id();unsigned calls=0;
 VulkanHostService direct;direct.invoke([&]{assert(std::this_thread::get_id()==owner);++calls;});assert(calls==1);
 std::mutex mutex;std::condition_variable changed;std::function<void()> pending;bool ready=false,finished=false;
 auto service=std::make_shared<VulkanHostService>(owner,[&](std::function<void()> f){
  std::unique_lock<std::mutex> lock(mutex);assert(!pending);pending=std::move(f);ready=false;changed.notify_all();changed.wait(lock,[&]{return ready;});
 });
 bool caught=false,repeated=false;std::exception_ptr worker_error;
 std::thread worker([&]{try{
  NativeVulkan vk(service);
  service->invoke([&]{assert(std::this_thread::get_id()==owner);++calls;});
  try{service->invoke([]{throw std::runtime_error("fixed host failure");});}catch(const std::runtime_error& e){caught=std::string(e.what())=="fixed host failure";}
  VulkanHostService duplicate(owner,[&](std::function<void()> f){service->dispatch([f]{f();f();});});
  try{duplicate.invoke([&]{assert(std::this_thread::get_id()==owner);++calls;});}catch(const std::runtime_error& e){repeated=std::string(e.what()).find("repeated callback")!=std::string::npos;}
 }catch(...){worker_error=std::current_exception();}
 {std::lock_guard<std::mutex> lock(mutex);finished=true;}changed.notify_all();});
 for(;;){std::function<void()> f;{std::unique_lock<std::mutex> lock(mutex);changed.wait(lock,[&]{return bool(pending)||finished;});if(!pending)break;f=std::move(pending);pending={};}
  f();{std::lock_guard<std::mutex> lock(mutex);ready=true;}changed.notify_all();}
 worker.join();if(worker_error)std::rethrow_exception(worker_error);assert(caught && repeated && calls==3);
 // Missing, asynchronous and wrong-thread transports must execute no action.
 std::function<void()> late;bool missing=false,async=false,wrong=false;
 std::thread invalid([&]{
  VulkanHostService absent(owner);try{absent.invoke([&]{++calls;});}catch(const std::runtime_error&){missing=true;}
  VulkanHostService delayed(owner,[&](std::function<void()> f){late=std::move(f);});try{delayed.invoke([&]{++calls;});}catch(const std::runtime_error&){async=true;}
  VulkanHostService foreign(owner,[](std::function<void()> f){f();});try{foreign.invoke([&]{++calls;});}catch(const std::runtime_error&){wrong=true;}
 });invalid.join();late();assert(missing && async && wrong && calls==3);
 std::puts("PASS synchronous main-thread service, worker renderer, exception and late/foreign/repeated rejection");
}
'''
class VulkanHostServiceTests(unittest.TestCase):
    def test_main_thread_service_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cpp=root/'test.cpp';cpp.write_text(native_source().split('int main(int argc,char** argv)')[0]+MAIN)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=240)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json');env.pop('ANYPS3_PRESENT',None)
            run=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr);self.assertIn('PASS ',run.stdout)
