"""Fixed GPU VP predicate/address oracles, including NaN and checked boundaries."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES
MAIN = r'''
static std::array<unsigned,4> instruction_(unsigned op,unsigned output,unsigned input,unsigned raw=0x1b02,bool end=false){
 return {(63u<<15)|(7u<<10)|(1u<<30),(op<<22)|((raw>>9)&255)|(input<<8),(raw&511)<<23,(15u<<13)|(63u<<7)|(output<<2)|unsigned(end)};
}
static void instruction_(NativeRsx& g,unsigned at,std::array<unsigned,4> words){for(unsigned c=0;c<4;++c){g.transform_program[at*4+c]=words[c];g.transform_program_written[at*4+c]=true;}}
static std::vector<unsigned> inputs_(){
 std::vector<unsigned> words(256+468*5,0);words[0]=1;words[2]=256;
 const float values[]={-1.25f,0,1,std::numeric_limits<float>::quiet_NaN(),.125f,.25f,.5f,1};
 for(unsigned c=0;c<8;++c)words[1+2*4+c]=rsxdraw::float_bits(values[c]);
 const float constant[]={2,3,4,5};for(unsigned c=0;c<4;++c)words[256+c]=rsxdraw::float_bits(constant[c]);words[256+468*4]=15;return words;
}
static void predicates_(NativeVulkan& vk){
 NativeRsx g;auto cc=instruction_(1,31,2);cc[0]&=~(1u<<30);instruction_(g,0,cc);
 auto pred=instruction_(1,1,3,0x1b02,true);pred[0]=(pred[0]&~(7u<<10))|(1u<<10)|(1u<<13)|(0x1bu<<2);instruction_(g,1,pred);
 auto source=rsxvk::vertex_shader(g,0,true,true);assert(source);auto words=inputs_();vk.compute(*source,words,1);
 const unsigned at=65+4;const float expected[]={.125f,0,0,1};for(unsigned c=0;c<4;++c)assert(rsxdraw::bits_float(words[at+c])==expected[c]);assert(words[7]==0);
 std::puts("PASS per-component CC swizzle/predicate: negative, zero, positive and NaN");
}
static void indexed_(NativeVulkan& vk){
 for(unsigned mode=0;mode<5;++mode){NativeRsx g;auto arl=instruction_(13,31,2);arl[0]&=~((63u<<15)|(1u<<30));arl[3]&=~(7u<<13);instruction_(g,0,arl);
  auto load=instruction_(1,0,3,mode==1 || mode==3?0x1b02:0x1b03,true);
  if(mode==1 || mode==3)load[0]|=1u<<27;
  else{load[1]|=2u<<12;load[3]|=2;}
  instruction_(g,1,load);auto source=rsxvk::vertex_shader(g,0,true,true);assert(source);
  auto words=inputs_();if(mode==1)words[9]=rsxdraw::float_bits(-1.f);if(mode==2)words[9]=rsxdraw::float_bits(466.f);if(mode==3)words[9]=rsxdraw::float_bits(-17.f);if(mode==4)words[256+468*4]=0;
  vk.compute(*source,words,1);
  if(mode>=2)assert(words[7]==16);
  else if(mode==1){const float expected[]={-1,0,1};for(unsigned c=0;c<3;++c)assert(rsxdraw::bits_float(words[65+c])==expected[c]);assert(std::isnan(rsxdraw::bits_float(words[68])));}
  else{const float expected[]={2,3,4,5};for(unsigned c=0;c<4;++c)assert(rsxdraw::bits_float(words[65+c])==expected[c]);assert(words[7]==0);}
 }
 std::puts("PASS ARL floor and indexed input/constant wrap, invalid input, undocumented bank and unwritten constant");
}
int main(){try{NativeVulkan vk;predicates_(vk);indexed_(vk);}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+MAIN
class GpuVertexTests(unittest.TestCase):
    def test_predicates_and_indexed_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'test.cpp').write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(root/'test.cpp'),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=180)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            run=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stdout+run.stderr);self.assertNotIn('Validation Warning',run.stdout+run.stderr)
            self.assertEqual(run.stdout.count('PASS '),2)
