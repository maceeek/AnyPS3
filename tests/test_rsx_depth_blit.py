"""Packed D24S8 nearest copies: exact bytes and independent rational coordinates."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void copy_case(unsigned scale,unsigned sw,unsigned sh,unsigned dw,unsigned dh,
                      unsigned xn,unsigned xd,unsigned yn,unsigned yd,bool depth){
 setenv("ANYPS3_RENDER_SCALE",std::to_string(scale).c_str(),1);
 NativeVulkan vk;auto batch=vk.graphics_batch();auto& b=*batch;
 auto source=vk.graphics_image(b,VK_FORMAT_D24_UNORM_S8_UINT,sw,sh,1,true);
 auto target=vk.graphics_image(b,depth?VK_FORMAT_D24_UNORM_S8_UINT:VK_FORMAT_R8G8B8A8_UNORM,dw,dh,1,depth);
 for(unsigned generation=0;generation<2;++generation){
  std::array<std::vector<std::uint32_t>,2> words;
  std::array<std::vector<std::uint8_t>,2> stencil;
  for(unsigned pass=0;pass<(scale>1?2u:1u);++pass){
   const unsigned k=pass?scale:1,w=sw*k,h=sh*k;
   words[pass].resize(w*h);stencil[pass].resize(w*h);
   for(unsigned y=0;y<h;++y)for(unsigned x=0;x<w;++x){const unsigned i=y*w+x;
    words[pass][i]=(i%17==0?0xffffffu:i%19==0?0u:(x*65539u+y*257u+generation*1234567u+pass*731u)&0xffffffu);
    stencil[pass][i]=(x*29u+y*43u+generation*71u+pass*97u)&255u;
   }
   auto image=pass?source->visual:source;
   vk.graphics_transfer(b,image,VK_IMAGE_ASPECT_DEPTH_BIT,4,words[pass].data());
   vk.graphics_transfer(b,image,VK_IMAGE_ASPECT_STENCIL_BIT,1,stencil[pass].data());
  }
  const auto downloads=b.download_bytes;
  assert(vk.graphics_scaled_depth_bytes(b,source,target,(1048576u/xd)*xn,(1048576u/yd)*yn));
  // Neither pixels nor draw status words are downloaded by this device-only primitive.
  assert(b.download_bytes==downloads);
  std::shared_ptr<NativeVulkan::GraphicsImage> chained;
  if(depth && sw<100){
   chained=vk.graphics_image(b,VK_FORMAT_R8G8B8A8_UNORM,dw,dh,1);
   // Consume the newly produced depth surface before any submission/readback.
   assert(vk.graphics_scaled_depth_bytes(b,target,chained,1048576,1048576));
   assert(b.download_bytes==downloads);
  }
  const auto aspect=depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT;
  const auto low=vk.graphics_transfer(b,target,aspect,4);
  const auto ls=depth?vk.graphics_transfer(b,target,VK_IMAGE_ASPECT_STENCIL_BIT,1):0;
  const auto high=scale>1?vk.graphics_transfer(b,target->visual,aspect,4):low;
  const auto hs=depth && scale>1?vk.graphics_transfer(b,target->visual,VK_IMAGE_ASPECT_STENCIL_BIT,1):ls;
  const auto chain_low=chained?vk.graphics_transfer(b,chained,VK_IMAGE_ASPECT_COLOR_BIT,4):0;
  const auto chain_high=chained && scale>1?vk.graphics_transfer(b,chained->visual,VK_IMAGE_ASPECT_COLOR_BIT,4):chain_low;
  vk.graphics_finish(b);
  for(unsigned pass=0;pass<(scale>1?2u:1u);++pass){
   const unsigned k=pass?scale:1,w=dw*k,h=dh*k;
   const auto* bytes=static_cast<const std::uint8_t*>(b.map)+(pass?high:low);
   for(unsigned y=0;y<h;++y)for(unsigned x=0;x<w;++x){
    // Rational arithmetic is independent of the emitted fixed-point GLSL.
    const unsigned sx=std::min(x*xn/xd,sw*k-1),sy=std::min(y*yn/yd,sh*k-1),i=sy*sw*k+sx;
    const auto d=words[pass][i];const std::uint8_t expected[]={std::uint8_t(d>>8),std::uint8_t(d),stencil[pass][i],std::uint8_t(d>>16)};
    const auto at=y*w+x;bool mismatch;
    if(depth){std::uint32_t actual=0;std::memcpy(&actual,bytes+at*4,4);
     const auto* st=static_cast<const std::uint8_t*>(b.map)+(pass?hs:ls);
     mismatch=(actual&0xffffffu)!=d || st[at]!=stencil[pass][i];
    }else mismatch=std::memcmp(bytes+at*4,expected,4)!=0;
    if(chained){const auto* c=static_cast<const std::uint8_t*>(b.map)+(pass?chain_high:chain_low);
     mismatch=mismatch || std::memcmp(c+at*4,expected,4)!=0;}
    if(mismatch){std::fprintf(stderr,"scale=%u pass=%u xy=%u,%u generation=%u\n",scale,pass,x,y,generation);throw std::runtime_error("depth scaled copy bytes differ");}
   }
  }
  vk.graphics_reset(b);
 }
 // Unsupported inputs must decline before touching batch counters.
 const auto uploads=b.upload_bytes,downloads=b.download_bytes,draws=b.draws;
 assert(!vk.graphics_scaled_depth_bytes(b,source,target,0,1048576));
 assert(!vk.graphics_scaled_depth_bytes(b,source,target,UINT32_MAX,1048576));
 if(!depth)assert(!vk.graphics_scaled_depth_bytes(b,target,source,1048576,1048576));
 assert(!vk.graphics_scaled_depth_bytes(b,source,source,1048576,1048576));
 assert(!vk.graphics_scaled_depth_bytes(b,source,target,INT32_MAX,1048576));
 const auto used=b.used;b.used=b.capacity;
 assert(!vk.graphics_scaled_depth_bytes(b,source,target,1048576,1048576));
 assert(b.used==b.capacity);b.used=used;
 assert(b.upload_bytes==uploads && b.download_bytes==downloads && b.draws==draws);
 std::printf("PASS depth-blit depth=%u scale=%u %ux%u -> %ux%u ratios=%u/%u,%u/%u\n",unsigned(depth),scale,sw,sh,dw,dh,xn,xd,yn,yd);
}
int main(){try{
 for(bool depth:{false,true}){
 for(unsigned scale=1;scale<=4;++scale){
  copy_case(scale,32,24,16,12,15,8,2,1,depth);
  copy_case(scale,7,5,19,13,1,2,1,2,depth); // enlargement and clamped last columns/rows
 }
 copy_case(2,960,704,512,352,15,8,2,1,depth); // measured game shape, no guest addresses
 }
 }catch(const std::exception& e){std::fprintf(stderr,"DEPTH BLIT TEST STOP %s\n",e.what());return 1;}}
'''


class DepthBlitTests(unittest.TestCase):
    def test_exact_depth_bytes_and_scaled_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                                    str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                       VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            result = subprocess.run([str(root / 'test')], capture_output=True, text=True, env=env, timeout=300)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn('Validation Error', output)
            self.assertNotIn('Validation Warning', output)
            self.assertEqual(result.stdout.count('PASS depth-blit'), 18, output)
            print(result.stdout, flush=True)
