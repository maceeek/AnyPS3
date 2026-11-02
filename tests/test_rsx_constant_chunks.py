"""Vertex-constant refresh skips chunks by write version and never misses a change (native oracle).

Session::refresh_vertex_constants mirrors the 468-vec4 transform-constant bank into the per-draw upload cache in
128-word chunks and bumps a chunk's upload version only when its words change. Chunks whose RsxShaderArray write
version is unchanged are not compared. Oracles independent of that bookkeeping: after every step the cache equals the
bank word for word, and exactly the chunks whose words differ from a shadow copy of the previous cache get a new upload
version. Steps: FIFO-style writes (write()), same-value rewrites, a copy assignment (new identity), and a mutable borrow
(operator[]: the bank is compared by value from then on).
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
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>8;}
int main(){try{
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();
 rsxgraphics::Session session(*s.rsx.renderer,s);
 constexpr unsigned words=468*4,chunks=rsxgraphics::Session::constant_chunks;
 auto& bank=s.rsx.transform_constants;
 // setup_draw wrote through operator[] (a borrow): start from a fresh, never-borrowed bank with the same words.
 {RsxShaderArray<std::uint32_t,512*4> fresh;for(unsigned i=0;i<words;++i)fresh.write(i,std::as_const(bank)[i]);bank.~RsxShaderArray();new(&bank)RsxShaderArray<std::uint32_t,512*4>(fresh);}
 if(bank.borrowed())throw std::runtime_error("test: bank still borrowed");
 std::vector<std::uint32_t> shadow;std::array<std::uint64_t,chunks+1> versions{};
 auto step=[&](const char* name){
  session.refresh_vertex_constants();
  for(unsigned i=0;i<words;++i)if(session.vertex_constants_cache[i]!=std::as_const(bank)[i])
   throw std::runtime_error(std::string(name)+": cache word "+std::to_string(i)+" differs from the bank");
  for(unsigned c=0;c<chunks;++c){
   bool changed=shadow.empty();
   for(unsigned i=c*128;i<std::min(words,c*128+128) && !changed;++i)changed=shadow[i]!=std::as_const(bank)[i];
   const bool bumped=session.constant_chunk_versions[c]!=versions[c];
   if(changed!=bumped)throw std::runtime_error(std::string(name)+": chunk "+std::to_string(c)+(changed?" changed without":" unchanged with")+" a new upload version");
   versions[c]=session.constant_chunk_versions[c];}
  shadow.assign(session.vertex_constants_cache.begin(),session.vertex_constants_cache.begin()+words);};
 step("initial");
 std::uint32_t x=7;
 for(unsigned round=0;round<200;++round){
  const unsigned n=1+lcg(x)%40;
  for(unsigned k=0;k<n;++k){const unsigned at=lcg(x)%words;
   // a third of the writes rewrite the value already there (no change, no version)
   bank.write(at,(lcg(x)%3)?lcg(x):std::as_const(bank)[at]);s.rsx.transform_constants_written.write(at,true);}
  step("fifo writes");
 }
 for(unsigned i=0;i<words;i+=97){bank.write(i,std::as_const(bank)[i]);}
 step("same-value rewrites");
 // Copy assignment (bank still never borrowed): new identity, contents of another bank.
 RsxShaderArray<std::uint32_t,512*4> other;for(unsigned i=0;i<words;++i){other.write(i,i*2654435761u);}
 bank=other;
 step("assignment");
 // A mutable borrow writes behind the bank's back: from now on every chunk must be compared by value.
 bank[1000]^=0x5a5a5a5au;
 step("borrow");
 bank[5]^=1u;
 step("borrow again");
 std::puts("PASS constant cache equals the bank and bumps exactly the changed chunks");
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxConstantChunkTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_constant_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ)
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 1, result.stdout)


if __name__ == '__main__':
    unittest.main()
