"""Visual MSAA (ANYPS3_RENDER_SCALE_MSAA): a 2-sample visual image drawn as true 2x MSAA must give the same planes as the
plane-by-plane path (the independent reference: separate passes with shifted geometry); native bytes stay identical."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void scene(const char* path){
 setenv("ANYPS3_RENDER_SCALE","2",1);
 NativeVulkan vk;auto b=vk.graphics_batch(8*1024*1024);
 NativeVulkan::GraphicsState s{};s.width=s.height=16;s.samples=2;s.colors=1;s.scale_visual=true;s.ms_ok=true;
 s.color=VK_FORMAT_R8G8B8A8_UNORM;s.depth=VK_FORMAT_D24_UNORM_S8_UINT;s.scissor={{0,0},{16,16}};s.blend[0].colorWriteMask=15;
 s.z.depthTestEnable=s.z.depthWriteEnable=true;s.z.depthCompareOp=VK_COMPARE_OP_LESS;
 // RSX 2x positions (rsx_graphics.py aa==3) = the Vulkan standard 2x locations at the visual raster.
 const std::array<std::array<float,2>,4> positions{{{.75f,.75f},{.25f,.25f},{0,0},{0,0}}};
 const std::string fs=R"GLSL(#version 450
 layout(push_constant) uniform Params{vec2 extent;vec2 shift;uint plane;layout(offset=56) vec2 raster_scale_;};
 layout(location=0) out vec4 c;
 void main(){vec2 p=gl_FragCoord.xy/raster_scale_;float z=gl_FragCoord.z-dFdx(gl_FragCoord.z)*shift.x-dFdy(gl_FragCoord.z)*shift.y;
  c=vec4(fract(p.x*.37+p.y*.11),fract(p.y*.23),z,.25);}
 )GLSL";
 const auto vs=rsxgraphics::vertex_source();
 auto tri=[&](float x0,float y0,float x1,float y1,float x2,float y2,float z0,float z1,float z2){
  std::vector<unsigned> data(16+3*68);data[3]=16;const float p[3][3]={{x0,y0,z0},{x1,y1,z1},{x2,y2,z2}};
  for(unsigned i=0;i<3;++i){const unsigned at=16+i*68;for(unsigned k=0;k<3;++k)data[at+k]=rsxdraw::float_bits(p[i][k]);data[at+3]=rsxdraw::float_bits(1.f);}
  return data;};
 auto color=vk.graphics_image(*b,s.color,16,16,2),depth=vk.graphics_image(*b,s.depth,16,16,2,true);
 std::array<std::shared_ptr<NativeVulkan::GraphicsImage>,4> images{color};
 const VkClearColorValue black{};
 auto clear=[&](float z,unsigned stencil){vk.graphics_clear_image(*b,color,black,0,0,VK_IMAGE_ASPECT_COLOR_BIT);
  vk.graphics_clear_image(*b,depth,black,z,stencil,VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT);};
 auto stencil_incr=[&](bool on){s.z.stencilTestEnable=on;auto& f=s.z.front;f.compareOp=VK_COMPARE_OP_ALWAYS;f.passOp=VK_STENCIL_OP_INCREMENT_AND_CLAMP;
  f.failOp=f.depthFailOp=VK_STENCIL_OP_KEEP;f.compareMask=f.writeMask=255;f.reference=0;s.z.back=f;};
 auto blend=[&](bool on){auto& a=s.blend[0];a.blendEnable=on;a.srcColorBlendFactor=a.dstColorBlendFactor=a.srcAlphaBlendFactor=a.dstAlphaBlendFactor=VK_BLEND_FACTOR_ONE;a.colorBlendOp=a.alphaBlendOp=VK_BLEND_OP_ADD;};
 // Frame 0 marks the targets wanted (MSAA on): the next full clear creates and seeds the working images.
 clear(1,0);vk.graphics_draw(*b,s,images,depth,vs,fs,tri(0.3f,0.2f,15.6f,1.1f,2.4f,14.7f,.5f,.5f,.5f),3,positions);
 std::vector<std::uint64_t> at;
 for(unsigned frame=0;frame<2;++frame){
  clear(frame?.75f:1.f,frame?7u:3u);
  blend(false);stencil_incr(false);
  vk.graphics_draw(*b,s,images,depth,vs,fs,tri(0.3f,0.2f,15.6f,1.1f,2.4f,14.7f,.6f,.2f,.9f),3,positions);
  stencil_incr(true);
  vk.graphics_draw(*b,s,images,depth,vs,fs,tri(15.8f,0.4f,14.9f,15.3f,1.3f,8.6f,.1f,.8f,.4f),3,positions);
  blend(true);stencil_incr(false);
  vk.graphics_draw(*b,s,images,depth,vs,fs,tri(7.1f,-2.f,17.f,9.3f,-1.f,16.2f,.3f,.3f,.3f),3,positions);
  if(!frame){blend(false);stencil_incr(true);
   // A read in the middle (expand), then a draw on the expanded planes before the next clear.
   at.push_back(vk.graphics_transfer(*b,color->visual,VK_IMAGE_ASPECT_COLOR_BIT,4));
   vk.graphics_draw(*b,s,images,depth,vs,fs,tri(3.3f,3.1f,12.6f,4.4f,8.2f,13.9f,.05f,.05f,.05f),3,positions);}
  for(auto& image:{color->visual,depth->visual})at.push_back(vk.graphics_transfer(*b,image,image->depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,4));
  at.push_back(vk.graphics_transfer(*b,depth->visual,VK_IMAGE_ASPECT_STENCIL_BIT,1));
  for(auto& image:{color,depth})at.push_back(vk.graphics_transfer(*b,image,image->depth?VK_IMAGE_ASPECT_DEPTH_BIT:VK_IMAGE_ASPECT_COLOR_BIT,4));
  at.push_back(vk.graphics_transfer(*b,depth,VK_IMAGE_ASPECT_STENCIL_BIT,1));
 }
 vk.graphics_finish(*b);
 const auto* bytes=static_cast<const unsigned char*>(b->map);
 // Per frame: [mid colour (frame 0 only)] visual colour, visual depth, visual stencil, native colour, native depth, native stencil.
 const std::uint64_t visual=32*32*2,native=16*16*2;
 std::FILE* out=std::fopen(path,"wb");unsigned n=0;
 for(unsigned frame=0;frame<2;++frame){
  if(!frame)std::fwrite(bytes+at[n++],1,visual*4,out);
  std::fwrite(bytes+at[n++],1,visual*4,out);std::fwrite(bytes+at[n++],1,visual*4,out);std::fwrite(bytes+at[n++],1,visual,out);
  std::fwrite(bytes+at[n++],1,native*4,out);std::fwrite(bytes+at[n++],1,native*4,out);std::fwrite(bytes+at[n++],1,native,out);}
 std::fclose(out);
 std::printf("MSAA images=%llu clears=%llu draws=%llu plane_draws=%llu expands=%llu bit_expands=%llu visual_draws=%llu\n",(unsigned long long)vk.graphics_ms_images.load(),
  (unsigned long long)vk.graphics_ms_clears.load(),(unsigned long long)vk.graphics_ms_draws.load(),(unsigned long long)vk.graphics_ms_plane_draws.load(),
  (unsigned long long)vk.graphics_ms_expands.load(),(unsigned long long)vk.graphics_ms_bit_expands.load(),(unsigned long long)vk.graphics_visual_draws);
}
int main(int,char** argv){try{scene(argv[1]);}catch(const std::exception& e){std::fprintf(stderr,"MSAA TEST STOP %s\n",e.what());return 1;}}
'''

VISUAL, NATIVE = 32 * 32 * 2, 16 * 16 * 2


def sections():
    out = []
    for frame in range(2):
        if not frame:
            out.append(('mid colour', VISUAL * 4, 'color'))
        out += [(f'f{frame} visual colour', VISUAL * 4, 'color'), (f'f{frame} visual depth', VISUAL * 4, 'depth'),
                (f'f{frame} visual stencil', VISUAL, 'stencil'), (f'f{frame} native colour', NATIVE * 4, 'exact'),
                (f'f{frame} native depth', NATIVE * 4, 'exact'), (f'f{frame} native stencil', NATIVE, 'exact')]
    return out


class VisualMsaaTests(unittest.TestCase):
    def run_scene(self, root, env, msaa):
        path = root / f'out-{msaa}.bin'
        result = subprocess.run([str(root / 'test'), str(path)], capture_output=True, text=True,
                                env=dict(env, ANYPS3_RENDER_SCALE_MSAA=str(msaa)), timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Validation Error', result.stdout + result.stderr)
        self.assertNotIn('Validation Warning', result.stdout + result.stderr)
        line = next(l for l in result.stdout.splitlines() if l.startswith('MSAA '))
        return path.read_bytes(), {k: int(v) for k, v in (f.split('=') for f in line.split()[1:])}

    def compare(self, planes, msaa, device):
        offset = 0
        for name, size, kind in sections():
            a, m = planes[offset:offset + size], msaa[offset:offset + size]
            offset += size
            if kind in ('exact', 'stencil'):
                self.assertEqual(a, m, f'{device}: {name} differs')
            elif kind == 'depth':
                worst = max(abs((int.from_bytes(a[i:i + 4], 'little') & 0xffffff) - (int.from_bytes(m[i:i + 4], 'little') & 0xffffff))
                            for i in range(0, size, 4))
                self.assertLessEqual(worst, 1, f'{device}: {name} depth differs by {worst} units')
            else:
                # Same samples; only an exact edge tie may rasterize differently (shifted geometry vs sample location).
                bad = sum(a[i:i + 4] != m[i:i + 4] for i in range(0, size, 4))
                self.assertLessEqual(bad, size // 4 // 200, f'{device}: {name}: {bad} of {size // 4} samples differ')
        self.assertEqual(offset, len(planes))

    def test_msaa_matches_planes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')],
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0, build.stderr)
            base = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                        VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1')
            base.pop('ANYPS3_AUDIO_AUDIBLE', None)
            for key in ('VK_DRIVER_FILES', 'VK_ICD_FILENAMES', 'ANYPS3_VK_DEVICE'):
                base.pop(key, None)
            # lavapipe has no 2x sample count: the switch must fall back to the planes exactly (no working image).
            lvp = dict(base, ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            planes, stats = self.run_scene(root, lvp, 0)
            msaa, stats = self.run_scene(root, lvp, 1)
            self.assertEqual(stats['images'], 0, stats)
            self.assertEqual(planes, msaa)
            print(f'PASS lavapipe fallback (no 2x): identical planes {stats}', flush=True)
            nvidia = '/usr/share/vulkan/icd.d/nvidia_icd.json'
            if not Path(nvidia).exists():
                if os.environ.get('ANYPS3_REQUIRE_NVIDIA'):
                    self.fail('NVIDIA ICD required')
                self.skipTest('no NVIDIA ICD for the 2x path')
            nv = dict(base, VK_DRIVER_FILES=nvidia)
            planes, plane_stats = self.run_scene(root, nv, 0)
            msaa, stats = self.run_scene(root, nv, 1)
            self.assertEqual(plane_stats['images'], 0, plane_stats)
            # 2 working images, 4 clears into them, 6 MSAA draws (3 per frame), the frame-0 draw before the
            # images exist and the one after the mid read are plane draws; expands: mid read colour, the plane
            # draw's depth, then colour+depth reads per frame end; stencil was written by MSAA draws.
            self.assertEqual(stats['images'], 2, stats)
            self.assertEqual(stats['draws'], 6, stats)
            self.assertGreaterEqual(stats['expands'], 4, stats)
            self.assertGreaterEqual(stats['bit_expands'], 1, stats)
            self.compare(planes, msaa, 'NVIDIA')
            print(f'PASS NVIDIA 2x MSAA visual planes == plane-by-plane planes, native bytes identical {stats}', flush=True)


if __name__ == '__main__':
    unittest.main()
