"""GPU work per frame of a scale-2 paired stream: counts and exact pixels against an independent CPU model.

The stream has the pass structure of a game frame at ANYPS3_RENDER_SCALE=2 (native and visual halves of every draw, 2-sample
sample planes, depth-tested draws, colour+depth clears, a surface copy, a native upload with its visual rebuild, a
single-sample post target). Per frame the test prints the work counters (PERF RENDER_SCALE_WORK names: full barriers,
elided barriers, pipeline barriers, native/visual pass begins, copies/clears/blits with bytes or texels). The last frame reads
back every native and visual image; every pixel must equal the CPU model (scissor rectangles, depth LESS/ALWAYS on cleared
depth, copy, upload). Visual pixel (x,y) of plane p equals native pixel (x/2,y/2): every draw covers whole pixels. Vulkan
synchronization validation must stay silent in every mode.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.test_rsx_graphics import native_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
static const char* VS=R"GLSL(#version 450
layout(std430,binding=0) readonly buffer Data{uint w[];};
void main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2-1,uintBitsToFloat(w[1]),1);}
)GLSL";
static const char* FS=R"GLSL(#version 450
layout(std430,binding=0) readonly buffer Data{uint w[];};layout(std430,binding=37) readonly buffer State{uint st[];};
layout(location=0) out vec4 c;
void main(){c=unpackUnorm4x8(st[w[0]]);}
)GLSL";
using Img=std::shared_ptr<NativeVulkan::GraphicsImage>;
constexpr unsigned N=32;
struct Model {std::vector<std::uint32_t> color=std::vector<std::uint32_t>(N*N,0);std::vector<unsigned char> written=std::vector<unsigned char>(N*N,0);};
struct Stream {
 NativeVulkan vk;std::unique_ptr<NativeVulkan::GraphicsBatch> b;
 Img a,z,c,d,e,post;Model ma,mc,md,me,mpost;std::shared_ptr<NativeVulkan::GraphicsBuffer> words;std::vector<unsigned> data=std::vector<unsigned>(16,0);std::uint32_t word=0;std::uint64_t version=0;
 std::array<std::array<float,2>,4> positions{{{.75f,.75f},{.25f,.25f},{.5f,.5f},{.5f,.5f}}};
 Stream(){b=vk.graphics_batch(16*1024*1024);
  a=vk.graphics_image(*b,VK_FORMAT_R8G8B8A8_UNORM,N,N,2);z=vk.graphics_image(*b,VK_FORMAT_D24_UNORM_S8_UINT,N,N,2,true);
  c=vk.graphics_image(*b,VK_FORMAT_R8G8B8A8_UNORM,N,N,2);d=vk.graphics_image(*b,VK_FORMAT_R8G8B8A8_UNORM,N,N,1);
  e=vk.graphics_image(*b,VK_FORMAT_R8G8B8A8_UNORM,N,N,1);post=vk.graphics_image(*b,VK_FORMAT_R8G8B8A8_UNORM,N,N,1);data[1]=0x3f000000;
  if(!a->visual || !z->visual || a->visual->width!=2*N)throw std::runtime_error("scale-2 companions missing");}
 static VkClearColorValue rgba(std::uint32_t color){VkClearColorValue v{};for(unsigned k=0;k<4;++k)v.float32[k]=float((color>>(8*k))&255)/255.f;return v;}
 void clear(const Img& image,Model& m,std::uint32_t color){vk.graphics_clear_image(*b,image,rgba(color),0,0,VK_IMAGE_ASPECT_COLOR_BIT);std::fill(m.color.begin(),m.color.end(),color);}
 // The guest clear of colour + depth (rsx_graphics.py): the depth clear shares the colour clear's leading barrier.
 void clear_depth(){vk.graphics_clear_image(*b,z,{},1.f,0,VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT,false);std::fill(ma.written.begin(),ma.written.end(),0);}
 // Full-screen triangle limited by the scissor; depth z=0.5 against the model's cleared (1.0) / written (0.5) depth.
 void draw(const Img& target,Model& m,unsigned samples,bool with_depth,bool less,std::uint32_t& r){
  unsigned x0=lcg(r)%N,x1=lcg(r)%N,y0=lcg(r)%N,y1=lcg(r)%N;if(x0>x1)std::swap(x0,x1);if(y0>y1)std::swap(y0,y1);++x1;++y1;
  const std::uint32_t color=0xff000000u|(lcg(r)&0xffffffu);
  NativeVulkan::GraphicsState s{};s.width=s.height=N;s.samples=samples;s.colors=1;s.color=VK_FORMAT_R8G8B8A8_UNORM;s.scale_visual=true;
  s.scissor={{int(x0),int(y0)},{x1-x0,y1-y0}};s.blend[0].colorWriteMask=15;s.z.sType=VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
  if(with_depth){s.depth=VK_FORMAT_D24_UNORM_S8_UINT;s.z.depthTestEnable=s.z.depthWriteEnable=VK_TRUE;s.z.depthCompareOp=less?VK_COMPARE_OP_LESS:VK_COMPARE_OP_ALWAYS;}
  word=color;std::array<NativeVulkan::GraphicsStateBlock,1> blocks{{{++version,&word,1}}};vk.graphics_state_blocks(*b,blocks);data[0]=unsigned(b->state_offsets[0]);
  std::array<Img,4> images{target};(void)vk.graphics_draw(*b,s,images,with_depth?z:Img{},VS,FS,data,3,positions);
  for(unsigned y=y0;y<y1;++y)for(unsigned x=x0;x<x1;++x){const auto i=y*N+x;if(with_depth && less && m.written[i])continue;m.color[i]=color;if(with_depth)m.written[i]=1;}
 }
 void frame(unsigned f,std::uint32_t& r){
  clear(a,ma,0xff102030u+f);clear_depth();                                  // scene: colour + depth clear
  for(unsigned i=0;i<8;++i)draw(a,ma,2,true,i&1,r);                          // depth-tested scene draws
  vk.graphics_copy(*b,a,c);mc=ma;                                            // surface copy (native + visual)
  for(unsigned i=0;i<4;++i)draw(c,mc,2,false,false,r);
  std::vector<std::uint32_t> pixels(N*N);for(auto& p:pixels)p=0xff000000u|(lcg(r)&0xffffffu);   // native upload + visual rebuild
  vk.graphics_transfer(*b,d,VK_IMAGE_ASPECT_COLOR_BIT,4,pixels.data());md.color=pixels;
  for(unsigned i=0;i<2;++i)draw(d,md,1,false,false,r);
  // Guest-layout surface upload (rebind/upload path): linear A8R8G8B8, word i = guest bytes A,R,G,B of pixel i; texel = R,G,B,A.
  std::vector<std::uint8_t> guest(N*N*4);std::vector<unsigned> table(N*N);
  for(unsigned i=0;i<N*N;++i){const std::uint32_t argb=0xff000000u|(lcg(r)&0xffffffu);table[i]=i*4;
   guest[i*4]=argb>>24;guest[i*4+1]=(argb>>16)&255;guest[i*4+2]=(argb>>8)&255;guest[i*4+3]=argb&255;
   me.color[i]=((argb>>16)&255)|(((argb>>8)&255)<<8)|((argb&255)<<16)|((argb>>24)<<24);}
  vk.graphics_unpack_upload(*b,e,8,false,4,N*4,1,table,words,guest.data());
  draw(e,me,1,false,false,r);
  clear(post,mpost,0xff000000u|f);for(unsigned i=0;i<3;++i)draw(post,mpost,1,false,false,r);   // single-sample post target
 }
 void check(const Img& image,const Model& m,const char* name){
  for(unsigned scale=1;scale<=2;++scale){const auto& v=scale==1?image:image->visual;
   const auto at=vk.graphics_transfer(*b,v,VK_IMAGE_ASPECT_COLOR_BIT,4);vk.graphics_finish(*b);
   const auto* px=reinterpret_cast<const std::uint32_t*>(static_cast<const char*>(b->map)+at);const unsigned w=N*scale;
   for(unsigned plane=0;plane<v->layers;++plane)for(unsigned y=0;y<w;++y)for(unsigned x=0;x<w;++x){
    const auto want=m.color[(y/scale)*N+x/scale],got=px[(plane*w+y)*w+x];
    if(got!=want){std::fprintf(stderr,"%s scale=%u plane=%u x=%u y=%u got=%08x want=%08x\n",name,scale,plane,x,y,got,want);throw std::runtime_error("GPU work stream pixel mismatch");}}
   vk.graphics_reset(*b);}
 }
};
int main(){try{
 setenv("ANYPS3_RENDER_SCALE","2",1);
 Stream g;std::uint32_t r=12345;const unsigned frames=4;
 std::array<std::uint64_t,NativeVulkan::WorkCount> before{},sum{},total{};
 for(unsigned f=0;f<frames;++f){
  for(unsigned k=0;k<NativeVulkan::WorkCount;++k)before[k]=NativeVulkan::graphics_work_value(k);
  g.frame(f,r);g.vk.graphics_finish(*g.b);
  std::printf("WORK frame=%u",f);
  for(unsigned k=0;k<NativeVulkan::WorkCount;++k){const auto n=NativeVulkan::graphics_work_value(k)-before[k];total[k]+=n;if(f)sum[k]+=n;std::printf(" %s=%llu",NativeVulkan::graphics_work_names[k],(unsigned long long)n);}
  std::printf("\n");
  g.vk.graphics_reset(*g.b);
 }
 std::printf("WORK mean");for(unsigned k=0;k<NativeVulkan::WorkCount;++k)std::printf(" %s=%.2f",NativeVulkan::graphics_work_names[k],double(sum[k])/(frames-1));std::printf("\n");
 // ANYPS3_RSX_VK_MARKS=1: every clear and every image copy/blit carries its own mark (kinds 8 clear, 6 copy_image), also
 // when no barrier follows it (visual clears, the last copy before a pass begin).
 std::uint64_t clear_marks=0,image_marks=0;for(unsigned site=0;site<16;++site){clear_marks+=g.vk.graphics_mark_count[site*16+8];image_marks+=g.vk.graphics_mark_count[site*16+6];}
 std::printf("MARKS clear=%llu image=%llu clears=%llu images=%llu\n",(unsigned long long)clear_marks,(unsigned long long)image_marks,(unsigned long long)total[NativeVulkan::WorkClears],
  (unsigned long long)(total[NativeVulkan::WorkCopyImage]+total[NativeVulkan::WorkBufferImage]+total[NativeVulkan::WorkBlits]));
 g.check(g.a,g.ma,"scene");g.check(g.c,g.mc,"copy");g.check(g.d,g.md,"upload");g.check(g.e,g.me,"unpack");g.check(g.post,g.mpost,"post");
 std::printf("PASS gpu work stream frames=%u native and visual pixels equal the model\n",frames);
}catch(const std::exception& e){std::fprintf(stderr,"FAIL gpu work %s\n",e.what());return 1;}}
'''


def work_means(stdout):
    line = next(l for l in stdout.splitlines() if l.startswith('WORK mean'))
    means = {k: float(v) for k, v in re.findall(r'(\w+)=([0-9.]+)', line)}
    marks = next(l for l in stdout.splitlines() if l.startswith('MARKS'))
    means.update({'mark_' + k: float(v) for k, v in re.findall(r'(\w+)=([0-9.]+)', marks)})
    return means


class GpuWorkTests(unittest.TestCase):
    def test_scale2_stream_counts_and_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(native_source().split('int main(int argc,char** argv)')[0] + MAIN)
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK',
                       str(root/'test.cpp'), *LIBRARIES, '-o', str(root/'test')]
            build = subprocess.run(command, capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0, build.stderr)
            base = dict(os.environ, SDL_AUDIODRIVER='dummy', ANYPS3_AUDIO_SINK='null', LP_NUM_THREADS='4',
                        VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1', ANYPS3_RSX_LAYERED='0', ANYPS3_RSX_MULTIVIEW='0')
            base.pop('ANYPS3_AUDIO_AUDIBLE', None)
            if not base.get('ANYPS3_REQUIRE_NVIDIA') and not base.get('VK_DRIVER_FILES') and not base.get('VK_ICD_FILENAMES'):
                base.update(ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            means = {}
            for name, extra in MODES:
                env = dict(base, **extra)
                result = subprocess.run([str(root/'test')], capture_output=True, text=True, env=env, timeout=300)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, name + '\n' + output)
                self.assertNotIn('Validation Error', output, name)
                self.assertNotIn('Validation Warning', output, name)
                self.assertIn('PASS gpu work stream', result.stdout, name)
                means[name] = work_means(result.stdout)
                print(name, result.stdout, flush=True)
            check_modes(self, means)


class CommandSerialTests(unittest.TestCase):
    """graphics_barrier skips a full barrier only if graphics_command_serial is unchanged since the batch's previous one: every
    action command that can be recorded outside a render pass must bump it (rsx_vk_resources.py wrapper). Commands that only
    occur inside a pass (whose begin is counted; graphics_barrier ends any pass first) or access no memory are listed here."""
    COUNTED = {'BeginRenderPass', 'EndRenderPass', 'CopyBuffer', 'CopyBufferToImage', 'CopyImageToBuffer', 'CopyImage', 'BlitImage',
               'FillBuffer', 'Dispatch', 'ClearColorImage', 'ClearDepthStencilImage', 'PipelineBarrier', 'ResetQueryPool', 'CopyQueryPoolResults'}
    UNCOUNTED = {'BindDescriptorSets', 'BindIndexBuffer', 'BindPipeline', 'PushConstants', 'SetBlendConstants', 'SetDepthBias', 'SetDepthBounds',
                 'SetScissor', 'SetStencilCompareMask', 'SetStencilReference', 'SetStencilWriteMask', 'SetViewport', 'WriteTimestamp',
                 'BeginQuery', 'EndQuery', 'Draw', 'DrawIndexed', 'ClearAttachments', 'BeginConditionalRenderingEXT', 'EndConditionalRenderingEXT'}

    def test_every_action_command_bumps_the_serial(self):
        package = Path(__file__).resolve().parents[1]/'anyps3'
        used = set()
        for path in package.glob('*.py'):
            used |= set(re.findall(r'vkCmd([A-Za-z0-9]+)', path.read_text()))
        unknown = used - self.COUNTED - self.UNCOUNTED
        self.assertFalse(unknown, f'classify these commands for the barrier dedupe: {sorted(unknown)}')
        wrappers = (package/'rsx_vk_resources.py').read_text()
        for name in self.COUNTED:
            start = wrappers.find(' vkCmd' + name + '(VkCommandBuffer')
            self.assertGreaterEqual(start, 0, name)
            body = wrappers[start:wrappers.index('::vkCmd' + name + '(', start)]
            self.assertIn('graphics_command_recorded();', body, name)


# (name, environment) per mode; check_modes compares their per-frame means.
MODES = [('defaults', {}), ('marks', {'ANYPS3_RSX_VK_MARKS': '1', 'ANYPS3_PERF_REPORT': '1'}),
         ('dedupe0', {'ANYPS3_RSX_BARRIER_DEDUPE': '0'}), ('pair0', {'ANYPS3_RSX_PAIR_BARRIERS': '0'}),
         ('pass0', {'ANYPS3_RSX_PASS_BARRIER': '0'}), ('pair0pass0', {'ANYPS3_RSX_PAIR_BARRIERS': '0', 'ANYPS3_RSX_PASS_BARRIER': '0'})]


def check_modes(test, means):
    test.assertGreater(means['defaults']['pass_visual'], 0)
    # Marks change no recorded work; each clear/copy/blit gets its own mark (attribution, not the next pass begin).
    for key, value in means['defaults'].items():
        if not key.startswith(('marks_', 'mark_')):
            test.assertEqual(means['marks'][key], value, key)
    # Each run of consecutive clears (scene colour + depth group, post) and of image copies/blits (surface copy pair, upload,
    # its visual rebuild, unpack upload, its visual rebuild) carries its own mark: 2 and 5 per frame over 4 frames (without
    # the pending-kind mark the runs not followed by a barrier have none: their time goes to the next pass begin).
    test.assertEqual(means['marks']['mark_clear'], 8)
    test.assertEqual(means['marks']['mark_image'], 20)
    # Barrier dedupe (pass ends not counted as barriers): per frame exactly one full barrier directly follows another with
    # nothing between them (the unpack upload's final barrier and its visual rebuild's leading barrier).
    test.assertEqual(means['dedupe0']['barriers_elided'], 0)
    test.assertEqual(means['pass0']['barriers_elided'], 1)
    test.assertEqual(means['pass0']['barriers'], means['dedupe0']['barriers'] - 1)
    test.assertEqual(means['pass0']['pipeline_barriers'], means['dedupe0']['pipeline_barriers'] - 1)
    # Grouped clears/copies: the visual halves of the three clears (scene colour, depth, post) and of the surface copy, and the
    # depth clear after the scene colour clear, record no barrier of their own: 5 fewer full barriers per frame.
    test.assertEqual(means['pass0']['barriers'], means['pair0pass0']['barriers'] - 5)
    # A pass end is the batch's last full barrier: the leading barriers of the ops that directly follow draws (scene copy after
    # the scene draws, upload after the copy's draws, unpack after the upload's draws, post clear after the unpack's draw)
    # are not recorded: 4 per frame.
    test.assertEqual(means['defaults']['barriers'], means['pass0']['barriers'] - 4)
    test.assertEqual(means['defaults']['barriers_elided'], 5)
    for mode in ('dedupe0', 'pair0', 'pass0', 'pair0pass0'):
        for key, value in means[mode].items():
            if key not in ('barriers', 'barriers_elided', 'pipeline_barriers') and not key.startswith('mark_'):
                test.assertEqual(means['defaults'][key], value, mode + ' ' + key)


if __name__ == '__main__':
    unittest.main()
