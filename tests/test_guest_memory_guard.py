"""Host page guards cover pointers retained before GPU ownership."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from anyps3.runtime import MEMORY_CPP

BODY=r'''
#include <cassert>
#include <thread>
#include <atomic>
#include <sys/wait.h>
#include <sys/resource.h>
static void prior_fault(int){_exit(88);}
int main(){
 assert(crc32({})==0);
 const rlimit no_core{0,0};setrlimit(RLIMIT_CORE,&no_core);
 struct sigaction prior{};prior.sa_handler=prior_fault;sigemptyset(&prior.sa_mask);assert(sigaction(SIGSEGV,&prior,nullptr)==0);
 GuestMemory memory;const auto page=GuestMemory::HostGuard::page_size();memory.map(0x10000,page*5,6,nullptr,0);
 auto& owner=*memory.regions[0].bytes;const auto base=reinterpret_cast<std::uintptr_t>(owner.data());
 auto* protected_bytes=reinterpret_cast<std::uint8_t*>((base+page-1)&~(page-1));protected_bytes+=page;
 volatile auto* retained=protected_bytes;protected_bytes[0]=0x12;protected_bytes[1]=0x34;
 volatile sig_atomic_t syncs=0;GuestMemory::HostGuard guard;
 guard.synchronize=[&]{++syncs;protected_bytes[0]=0xab;protected_bytes[1]=0xcd;};
 assert(guard.add(protected_bytes,16,owner));assert(guard.add(protected_bytes+8,16,owner));guard.arm();assert(guard.spans.size()==1);
 assert(retained[0]==0xab && retained[1]==0xcd);
 // Handler-mutated bookkeeping needs the normal signal compiler fence;
 // volatile applies to the guest load, not the vector metadata inspected next.
 std::atomic_signal_fence(std::memory_order_seq_cst);assert(syncs==1 && guard.spans.empty());
 assert(owner.front()==0 && owner.back()==0);assert(guard.add(protected_bytes,16,owner));guard.arm();
 retained[0]=0xef;std::atomic_signal_fence(std::memory_order_seq_cst);assert(syncs==2 && protected_bytes[0]==0xef && protected_bytes[1]==0xcd);
 // Reject any range whose rounded page would escape the pinned allocation.
 GuestBacking tiny(17);assert(!guard.add(tiny.data(),17,tiny));
 // Independent guards: destroying one cannot remove the other's handler.
 {GuestMemory::HostGuard temporary;assert(temporary.add(protected_bytes,16,owner));temporary.arm();}
 assert(protected_bytes[0]==0xef);
 auto child=[&](int mode){const auto pid=fork();assert(pid>=0);if(!pid){
  if(mode==0){raise(SIGSEGV);_exit(1);}
  guard.synchronize=[] {throw std::runtime_error("deliberate synchronization failure");};
  assert(guard.add(protected_bytes,16,owner));guard.arm();
  if(mode==1){const auto byte=retained[0];(void)byte;_exit(2);}
  std::thread worker([&]{const auto byte=retained[0];(void)byte;});worker.join();_exit(3);
 }int status=0;assert(waitpid(pid,&status,0)==pid);assert(WIFEXITED(status));return WEXITSTATUS(status);};
 assert(child(0)==88);assert(child(1)==125);assert(child(2)==125);
 std::puts("PASS merged host pages, raw read/write, pinned bounds, destructor, previous handler, explicit failed-sync and foreign-thread stops");
}
'''

@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class GuestMemoryGuardTests(unittest.TestCase):
    def test_retained_raw_pointer_faults_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'test.cpp').write_text('#include <cstdint>\n#include <cinttypes>\n#include <cstdio>\n'+MEMORY_CPP+BODY)
            build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(root/'test.cpp'),'-o',str(root/'test')],capture_output=True,text=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
