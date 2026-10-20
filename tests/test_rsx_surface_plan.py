"""rsxgraphics::Session::clipped_plan equals its original word-by-word definition (native differential oracle).

The reference below is the literal original implementation (per-word push_back, run-major / span-minor order). The
optimized one reserves exactly and copies whole words when the clip start is word aligned; both must produce the same
runs and the same word table, including unaligned run offsets/clip starts (sub-word remainder) and spans that
miss, partially cover or fully cover runs, overlap each other and arrive unsorted.
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
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
static std::shared_ptr<rsxgraphics::Surface::ReadbackPlan> reference_plan(const rsxgraphics::Surface& surface,const std::vector<GuestMemory::HostGuard::Span>& spans){
    auto plan=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
    for(const auto& run:surface.readback->runs)for(const auto& span:spans){
        const auto base=reinterpret_cast<std::uintptr_t>(run.data),first=std::max(base,span.begin),last=std::min(base+run.size,span.end);
        if(first>=last)continue;
        const unsigned delta=first-base,offset=plan->words.size()*4,count=last-first;
        for(unsigned byte=0;byte<count;byte+=4){const auto at=run.offset+delta+byte;plan->words.push_back(surface.readback->words[at/4]+at%4);}
        plan->runs.push_back({reinterpret_cast<std::uint8_t*>(first),count,offset,unsigned(run.guest_offset+delta)});
    }
    return plan;
}
static void differential(){
 std::vector<std::uint8_t> memory(1<<16);std::uint32_t r=99;unsigned compared=0,nonempty=0;
 for(unsigned round=0;round<400;++round){
  rsxgraphics::Target target{};rsxgraphics::Surface surface{target,1,1,1,0,false,false,{},{}};
  surface.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
  auto& plan=*surface.readback;const unsigned run_count=1+lcg(r)%12;unsigned at=lcg(r)%64,word_bytes=0;
  for(unsigned i=0;i<run_count;++i){
   const unsigned size=1+lcg(r)%200;unsigned data=at+lcg(r)%40;
   const unsigned offset=(lcg(r)%3==0)?word_bytes+1+lcg(r)%3:word_bytes;   // some runs start at a non-word-aligned plan offset
   plan.runs.push_back({memory.data()+data,size,offset,lcg(r)%5000});
   word_bytes=std::max(word_bytes,offset+size+3);at=data+size;
  }
  plan.words.resize(word_bytes/4+4);for(auto& w:plan.words)w=lcg(r);
  std::vector<GuestMemory::HostGuard::Span> spans;const unsigned span_count=lcg(r)%5;
  for(unsigned i=0;i<span_count;++i){const auto begin=reinterpret_cast<std::uintptr_t>(memory.data())+lcg(r)%600;spans.push_back({begin,begin+1+lcg(r)%300});}
  const auto expected=reference_plan(surface,spans),actual=rsxgraphics::Session::clipped_plan(surface,spans);
  assert(expected->words==actual->words && expected->runs.size()==actual->runs.size());
  for(std::size_t i=0;i<expected->runs.size();++i){const auto& a=expected->runs[i];const auto& b=actual->runs[i];
   assert(a.data==b.data && a.size==b.size && a.offset==b.offset && a.guest_offset==b.guest_offset);}
  ++compared;nonempty+=!expected->runs.empty();
 }
 assert(nonempty>150);
 std::printf("PASS clipped_plan equals the original word-by-word plan (%u cases, %u non-empty)\n",compared,nonempty);
}
int main(){try{differential();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxSurfacePlanTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_clipped_plan_equals_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 1, result.stdout)


if __name__ == '__main__':
    unittest.main()
