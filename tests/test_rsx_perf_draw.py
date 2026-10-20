"""RSX warm preparation: fixed BE, value/lifetime and dynamic-state oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void level_values(){
 rsxdraw::Texture a;a.levels.resize(1);a.levels[0].width=17;a.levels[0].height=23;a.levels.freeze();
 auto b=a;const auto& ca=a;const auto& cb=b;assert(&ca.levels[0]==&cb.levels[0]);
 b.levels[0].width=31;assert(ca.levels[0].width==17 && cb.levels[0].width==31 && cb.levels[0].height==23);
 auto c=a;c.levels.push_back({});c.levels.back().height=47;assert(ca.levels.size()==1 && c.levels.size()==2 && c.levels.back().height==47);
 auto d=a;for(auto& l:d.levels)l.height=53;assert(ca.levels[0].height==23 && static_cast<const rsxdraw::Texture&>(d).levels[0].height==53);
 auto& retained=a.levels[0];auto e=a;retained.width=67;assert(static_cast<const rsxdraw::Texture&>(e).levels[0].width==17);
 auto iterator=a.levels.begin();rsxdraw::Texture f;f=a;iterator->height=71;assert(static_cast<const rsxdraw::Texture&>(f).levels[0].height==23);
 std::puts("PASS shared texture levels preserve independent mutable values");
}
static void image_pins(){
 NativeVulkan::GraphicsImagePins pins;std::weak_ptr<NativeVulkan::GraphicsImage> weak;
 {auto image=std::make_shared<NativeVulkan::GraphicsImage>(nullptr,VK_FORMAT_R8G8B8A8_UNORM,4,4,1,false);image->memory_bytes=4096;weak=image;
 for(unsigned n=0;n<10000;++n){pins.push_back(image);}assert(pins.size()==1 && pins.bytes==4096);}
 assert(!weak.expired());
 for(unsigned n=0;n<600;++n){auto image=std::make_shared<NativeVulkan::GraphicsImage>(nullptr,VK_FORMAT_R8G8B8A8_UNORM,4,4,1,false);image->memory_bytes=256;pins.push_back(image);pins.push_back(image);}
 assert(pins.size()==601 && pins.bytes==157696);pins.clear();assert(weak.expired() && pins.empty() && pins.bytes==0);
 std::puts("PASS batch pin uniqueness, growth, exact bytes and retirement lifetime");
}
static void indexed_reads(){
 setenv("ANYPS3_RSX_BACKEND","cpu",1);
 PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;s.rsx.draw.indexed=true;R[0x181c]=0x3000;R[0x1820]=0x10;
 const unsigned initial[]={0,1,2,3};for(unsigned n=0;n<4;++n)s.memory.store(0xc0003000+2*n,2,initial[n]);
 auto run=[&]{s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);};
 run();assert(s.rsx.draw.scratch_indices==std::vector<unsigned>({0,1,2,3}));assert(s.memory.load(0xc0100000,4)==0x80ff8040);
 const auto capacity=s.rsx.draw.scratch_indices.capacity();
 // Current memory, not cached values, controls every following draw.
 s.memory.store(0xc0003000,2,3);run();assert(s.rsx.draw.scratch_indices[0]==3 && s.rsx.draw.scratch_indices.capacity()==capacity);
 // Both index widths decode guest BE, base index and restart literally.
 R[0x1820]=0;R[0x173c]=1;R[0x1dac]=1;R[0x1db0]=0x12345678;
 const unsigned words[]={0,2,0x12345678,1};for(unsigned n=0;n<4;++n)s.memory.store(0xc0003000+4*n,4,words[n]);
 run();assert(s.rsx.draw.scratch_indices==std::vector<unsigned>({1,3,2}));
 R[0x173c]=0;R[0x1dac]=0;
 for(unsigned n=0;n<4;++n){s.memory.store(0xc0003000+4*n,4,initial[n]);}
 // Split the input exactly inside a 32-bit index. Snapshot must gather BE
 // bytes, and remapping must be observed even after scratch reuse.
 auto source=s.memory.regions[1];const auto cut=0x3003u;
 auto a=std::make_shared<GuestBacking>(source.bytes->begin(),source.bytes->begin()+cut);
 auto b=std::make_shared<GuestBacking>(source.bytes->begin()+cut,source.bytes->end());
 s.memory.regions[1]={source.base,6,a};s.memory.regions.push_back({source.base+cut,6,b});
 run();assert(s.rsx.draw.scratch_indices==std::vector<unsigned>({0,1,2,3}));
 s.memory.regions.back().flags=2;bool failed=false;try{run();}catch(const std::runtime_error&){failed=true;}assert(failed);s.memory.regions.back().flags=6;
 R[0x173c]=UINT_MAX;failed=false;try{run();}catch(const std::runtime_error&){failed=true;}assert(failed);
 R[0x173c]=0;R[0x181c]=0x3000;s.rsx.draw.ranges={{0,0}};rsx_execute_draw(s);assert(s.rsx.draw.scratch_indices.empty());
 R[0x181c]=UINT_MAX;s.rsx.draw.ranges={{0,0}};failed=false;try{rsx_execute_draw(s);}catch(const std::runtime_error&){failed=true;}assert(failed);
 std::puts("PASS bulk indices: literal BE16/32, memory/remap/permission, split, restart, overflow and empty");
}
static void shader_reads(){
 PpuState s;setup_draw(s,8,8,32,false);const rsxdraw::Reg r{s.rsx};
 auto first=rsxdraw::load_fragment_program(s,r);assert(first.size()==1 && first[0].has_constant && first[0].constant[0]==1 && first[0].constant[1]==.5f);
 fpword(s,5,0x3e800000);auto second=rsxdraw::load_fragment_program(s,r);assert(second[0].constant[1]==.25f && first[0].constant[1]==.5f);
 auto old=s.memory.regions[1];const unsigned cut=0x1007;
 auto a=std::make_shared<GuestBacking>(old.bytes->begin(),old.bytes->begin()+cut),b=std::make_shared<GuestBacking>(old.bytes->begin()+cut,old.bytes->end());
 s.memory.regions[1]={old.base,6,a};s.memory.regions.push_back({old.base+cut,6,b});
 {GuestMemory::SplitReadScope split(s.memory);auto next=rsxdraw::load_fragment_program(s,r);assert(next[0].d==0x01001e01 && next[0].constant[1]==.25f);}
 std::puts("PASS original instruction words: literal halfword-swapped code/constants, memory write and split");
}
static void write_membership(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);session.observe();
 auto t=session.texture_metadata(0);session.texture_image(t);assert(session.raw_misses==1);
 rsxgraphics::Surface surface{};surface.readback=std::make_shared<rsxgraphics::Surface::ReadbackPlan>();
 const auto at=reinterpret_cast<std::uintptr_t>(t.plan->runs[0].data);surface.physical.push_back({at,at+4});
 session.dirty_surface(surface);assert(session.raw_invalidations==1 && session.raw_textures.empty());
 session.dirty_surface(surface);assert(session.raw_invalidations==1);
 // A new upload must invalidate the proof even with the identical mapping,
 // layout and source bytes. The second GPU write must erase it again.
 session.texture_image(t);session.dirty_surface(surface);assert(session.raw_misses==2 && session.raw_invalidations==2 && session.raw_textures.empty());
 // CPU alias writes still invalidate newly cached images independently.
 session.texture_image(t);s.memory.regions.push_back({0x81000000,6,s.memory.regions[1].bytes});s.memory.store(0x81002000,4,0xffaabbcc);
 assert(session.raw_invalidations==3);session.texture_image(session.texture_metadata(0));session.dirty_surface(surface);assert(session.raw_invalidations==4);
 session.flush();assert(s.memory.load(0xc0002000,4)==0xffaabbcc);
 std::puts("PASS GPU write proof: repeated layout, new membership and CPU alias invalidation");
}
static void dynamic_state(){
 PpuState s;setup_draw(s,8,8,32,false);s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);s.rsx.graphics_session=&session;session.observe();
 setenv("ANYPS3_RSX_BACKEND","gpu",1);
 auto run=[&]{s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);};
 run();session.batch->sync();const auto emits=session.batch->dynamic_emits,binds=session.batch->pipeline_binds;run();session.batch->sync();
 assert(session.batch->dynamic_emits==emits && session.batch->dynamic_reuses>=11 && session.batch->pipeline_binds==binds);
 const auto descriptors=session.batch->descriptor_misses,pipelines=s.rsx.renderer->compile_stats.graphics_pipelines;
 s.rsx.registers[0x310]=1;s.rsx.registers[0x314]=0x10001;s.rsx.registers[0x318]=0;s.rsx.registers[0x320]=0x80068006;run();session.batch->sync();
 assert(s.rsx.renderer->compile_stats.graphics_pipelines==pipelines+1 && session.batch->descriptor_misses==descriptors);s.rsx.registers[0x310]=0;
 session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040));
 // Command reset must re-emit state even when values/pipeline are identical.
 assert(!session.batch->dynamic_valid && session.batch->bound_graphics_pipeline==VK_NULL_HANDLE);run();session.batch->sync();assert(session.batch->dynamic_valid);
 session.flush();
 // Change a dynamic scissor and the shader constant independently. Only the
 // right half changes, with a fixed final guest pixel at each side.
 s.rsx.registers[0x8c0]=(4u<<16)|4u;fpword(s,4,0);fpword(s,5,0x3f800000);fpword(s,6,0);fpword(s,7,0x3f800000);
 run();session.flush();assert(near_argb(s.memory.load(0xc0100000,4),0x80ff8040) && s.memory.load(0xc0100010,4)==0xff00ff00);
 s.rsx.graphics_session=nullptr;
 std::puts("PASS dynamic reuse, reset and changed scissor/constant fixed pixels");
}
int main(){try{level_values();image_pins();indexed_reads();shader_reads();write_membership();dynamic_state();}catch(const std::exception& e){std::fprintf(stderr,"STOP %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN


class WarmDrawTests(unittest.TestCase):
    def test_fixed_oracles(self):
        if not shutil.which('c++'):
            self.skipTest('c++ required')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);cpp=root/'test.cpp';cpp.write_text(native_source());binary=root/'test'
            command=['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(binary)]
            p=subprocess.run(command,capture_output=True,text=True,timeout=240)
            self.assertEqual(p.returncode,0,p.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_RSX_SUBMIT_DRAWS='0',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',ANYPS3_VK_DEVICE='lavapipe')
            p=subprocess.run([str(binary)],env=env,capture_output=True,text=True,timeout=120)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertEqual(p.stdout.count('PASS '),6)
            self.assertNotIn('Validation Error',p.stderr);self.assertNotIn('Validation Warning',p.stderr)
