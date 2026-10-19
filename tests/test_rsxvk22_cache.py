"""Bounded texture residency, queued image generations and CPU tile span oracles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_perf import REFERENCE_TILE
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void cache_budget22(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& g=s.rsx;auto& r=g.registers;auto& m=s.memory;
 m.regions[1].bytes->resize(80*1024*1024);g.local_size=80*1024*1024;
 // One 64-MiB FP32 source, nine sampler identities with exactly the same
 // nearest single-level result. Pressure is BYTES, well below count512.
 r[0x1a00]=0x800000;r[0x1a04]=0x00010029|(0xbbu<<8);r[0x1a18]=(2048u<<16)|2048;r[0x1840]=2048*16;
 m.store(0xc0800000,4,0x3f800000);m.store(0xc080000c,4,0x3f800000);
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)m.store(0x70002100+i*8+c*4,4,0);
 g.renderer=std::make_unique<NativeVulkan>();auto& vk=*g.renderer;rsxgraphics::Session session(vk,s);g.graphics_session=&session;session.observe();setenv("ANYPS3_RSX_BACKEND","gpu",1);
 for(unsigned i=0;i<9;++i){r[0x1a14]=0x01010000|i;g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();
  if(m.load(0xc0100000,4)!=0xffff0000){auto t=rsxdraw::decode_texture(s,rsxdraw::Reg{g},0);auto c=rsxdraw::texel(t,t.levels[0],0,0);std::fprintf(stderr,"budget i=%u got=%08llx texel=%g,%g,%g,%g first=%08llx UV=%08llx\n",i,(unsigned long long)m.load(0xc0100000,4),c[0],c[1],c[2],c[3],(unsigned long long)m.load(0xc0800000,4),(unsigned long long)m.load(0x70002100,4));throw std::runtime_error("byte-budget exact red oracle");}
  assert(session.raw_bytes<=UINT64_C(512)*1024*1024 && session.raw_textures.size()<=8);
  for(const auto& e:session.raw_textures)assert(e.image && e.cost==e.image->memory_bytes && e.cost>=UINT64_C(64)*1024*1024);
 }
 assert(session.raw_evictions>=1 && session.raw_misses==9);
 const auto misses=session.raw_misses;g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();assert(session.raw_misses==misses);
 r[0x1a14]=0x01010000;g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);session.flush();assert(session.raw_misses==misses+1 && m.load(0xc0100000,4)==0xffff0000);
 g.graphics_session=nullptr;
 std::puts("PASS real 64MiB FP32 images:512MiB byte eviction below count limit, exact red, hot hit and cold reload");
}
static void queued_reuse22(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& g=s.rsx;auto& r=g.registers;auto& m=s.memory;
 r[0x1a18]=0x00010001;r[0x1840]=4;r[0x1a14]=0x01010000;
 for(unsigned i=0;i<4;++i)for(unsigned c=0;c<2;++c)m.store(0x70002100+i*8+c*4,4,0);
 m.enable_concurrency();m.restart_coherence();m.enable_page_broker();
 g.renderer=std::make_unique<NativeVulkan>();auto& vk=*g.renderer;rsxgraphics::Session session(vk,s);g.graphics_session=&session;session.observe();session.submit_draws=0;setenv("ANYPS3_RSX_BACKEND","gpu",1);
 std::shared_ptr<NativeVulkan::SampledImage> original;const unsigned colors[]={0xffff0000,0xff00ff00,0xff0000ff};
 for(unsigned i=0;i<3;++i){m.store(0xc0002000,4,colors[i]);r[0x210]=0x100000+i*0x100000;g.draw.primitive=8;g.draw.ranges={{0,4}};rsx_execute_draw(s);
  assert(session.raw_textures.size()==1 && session.raw_misses==i+1);auto image=session.raw_textures[0].image;
  if(!i)original=image;else assert(image==original);
  // Exercise both prior commands in this batch and prior submitted commands.
  if(i==0)session.submit_async("reuse_test");
 }
 session.flush();assert(vk.sampled_image_creations==1 && vk.sampled_image_reuses==2);
 for(unsigned i=0;i<3;++i)for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8;++x)assert(m.load(0xc0100000+i*0x100000+y*32+x*4,4)==colors[i]);
 // An incompatible immutable descriptor must fail before mutating the image.
 VkSamplerCreateInfo sampler{};sampler.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;sampler.magFilter=sampler.minFilter=VK_FILTER_NEAREST;sampler.addressModeU=sampler.addressModeV=sampler.addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
 bool stopped=false;try{vk.graphics_sampled_image(*session.batch,original->format,2,1,1,1,1,{}, {},sampler,UINT64_MAX,original);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("identity mismatch")!=std::string::npos;}assert(stopped);
 g.graphics_session=nullptr;
 std::puts("PASS queued RGB generations use one allocation: earlier draw bytes exact across submit, incompatible reuse stops");
}
static void tile_spans22(){
 NativeRsx::Tile tile;tile.offset=0x880000;tile.size=0x580000;tile.pitch=8192;tile.bank=1;
 const std::pair<unsigned,unsigned> golden[]={{0,0x884000},{4,0x884004},{32,0x884100},{64,0x884280},{256,0x88c200}};
 RsxTileAddress cache;for(auto p:golden)assert(cache.address(tile,p.first)==p.second);
 auto result=[](auto&& f){try{return std::string("value:")+std::to_string(f());}catch(const std::runtime_error& e){return std::string("error:")+e.what();}};
 for(unsigned pitch:{256u,512u,768u,1024u,1280u,1792u,2816u,3328u,7168u,8192u,15360u})for(unsigned bank=0;bank<4;++bank){
  tile.pitch=pitch;tile.bank=bank;
  for(unsigned at=0;at<1024*1024;at+=4){assert(result([&]{return original_tile_offset(tile,at);})==result([&]{return cache.address(tile,at);}));}
  for(unsigned at:{0u,1u,2u,3u,tile.size-32,tile.size-4,tile.size-1,tile.size,UINT32_MAX})assert(result([&]{return original_tile_offset(tile,at);})==result([&]{return cache.address(tile,at);}));
 }
 for(unsigned pitch:{0u,1u,255u,7680u}){tile.pitch=pitch;for(unsigned at:{0u,tile.size})assert(result([&]{return original_tile_offset(tile,at);})==result([&]{return cache.address(tile,at);}));}
 // Mutate the exact tile key after a cached Target 32-byte run. No stale
 // physical prefix may survive the bank or pitch mutation.
 tile.pitch=8192;tile.bank=0;rsxdraw::Target target;target.offset=tile.offset;target.base_offset=tile.offset;target.pitch=8192;target.tile=&tile;
 for(unsigned bank:{0u,1u,2u,3u,0u}){tile.bank=bank;assert(target.address(0,0)==original_tile_offset(tile,0)-tile.offset);}
 tile.pitch=7680;bool stopped=false;try{target.address(0,0);}catch(const std::runtime_error& e){stopped=std::string(e.what())=="unsupported RSX tile pitch";}assert(stopped);
 std::puts("PASS tile spans: fixed physical vectors,11 pitches/all banks/millions of addresses,unaligned/boundary/errors and mutable Target cache");
}
int main(){try{tile_spans22();queued_reuse22();cache_budget22();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0]+REFERENCE_TILE.replace('rsx_tiled_offset(', 'original_tile_offset(')+MAIN

class Rsxvk22CacheTests(unittest.TestCase):
    def test_byte_budget_pending_generations_and_tile_spans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cpp=root/'test.cpp';binary=root/'test';cpp.write_text(native_source())
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)],capture_output=True,text=True,timeout=240)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,SDL_AUDIODRIVER='dummy',ANYPS3_AUDIO_SINK='null',LP_NUM_THREADS='4',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('ANYPS3_REQUIRE_NVIDIA'):env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            run=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=240)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stderr);self.assertNotIn('Validation Warning',run.stderr)
            self.assertEqual(run.stdout.count('PASS '),3)
