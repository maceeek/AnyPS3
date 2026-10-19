"""Independent native oracles for RSX generations, exact bounds and payload ownership."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
static void bounds_(){
 using rsxgraphics::IndexBounds;
 const std::vector<std::array<unsigned,3>> ids{{17,19,17},{31,UINT_MAX-1,19}};
 IndexBounds bounds(ids);
 assert(bounds.get(1,false)==std::make_pair(17u,UINT_MAX-1));
 assert(bounds.get(4,false)==std::make_pair(4u,1073741823u));
 assert(bounds.get(12,true)==std::make_pair(2u,7u));
 assert(bounds.get(12,true)==std::make_pair(2u,7u) && bounds.modulo.size()==1);
 const std::vector<std::array<unsigned,3>> sparse{{17,19,17}};
 IndexBounds exact(sparse);assert(exact.get(12,true)==std::make_pair(5u,7u));
 const std::vector<std::array<unsigned,3>> empty;IndexBounds zero(empty);assert(zero.get(2,true)==std::make_pair(UINT_MAX,0u));
 std::puts("PASS exact sparse modulo/divisor/full-width/empty bounds");
}
static void generations_(){
 RsxShaderArray<unsigned,4> words;auto v=words.version();words.write(0,0);assert(words.version()==v);
 words.write(0,0x80000000);assert(words.version()!=v);v=words.version();
 auto* retained=words.data();retained[1]=0x7fc12345;assert(words.version()!=v);v=words.version();
 retained[1]=0x3f800000;assert(words.version()!=v);v=words.version();assert(words.version()==v);
 RsxShaderArray<unsigned,4> other;other.write(0,0x11223344);words=other;assert(words.version()!=v);v=words.version();
 retained[2]=0xaabbccdd;assert(words.version()!=v && std::as_const(words)[0]==0x11223344);
 auto copy=words;retained[0]=0xdeadbeef;assert(std::as_const(copy)[0]==0x11223344);
 PpuState s;setup_draw(s,8,8,32,false);rsxgraphics::VertexProgramCache cache;
 const auto old=cache.source(s.rsx,0);assert(old && cache.source(s.rsx,0)==old);
 auto* program=s.rsx.transform_program.data();auto* written=s.rsx.transform_program_written.data();
 written[0]=false;assert(!cache.source(s.rsx,0));written[0]=true;assert(cache.source(s.rsx,0));
 const auto saved=program[1];program[1]=(program[1]&~(31u<<22))|(25u<<22);cache.refresh(s.rsx);assert(cache.texture_mask&1u);
 program[1]=saved;cache.refresh(s.rsx);assert(cache.texture_mask==0);
 s.rsx.registers[0x1ea0]=511;assert(!cache.source(s.rsx,0));s.rsx.registers[0x1ea0]=0;assert(cache.source(s.rsx,0));
 s.rsx.renderer=std::make_unique<NativeVulkan>();rsxgraphics::Session session(*s.rsx.renderer,s);std::vector<unsigned> data;
 session.append_vertex_constants(data);assert(data.size()==2340 && data[24]==0x40800000 && data[1872+6]==15);
 auto* constants=s.rsx.transform_constants.data();constants[24]=0x80000000;data.clear();session.append_vertex_constants(data);assert(data[24]==0x80000000);
 constants[24]=0x7fc12345;data.clear();session.append_vertex_constants(data);assert(data[24]==0x7fc12345);
 s.rsx.transform_constants_written[24]=false;data.clear();session.append_vertex_constants(data);assert(data[1872+6]==14);
 session.serialize_registers(data,8,8);assert(data[16+0x1ff0/4]==s.rsx.registers[0x1ff0] && data[16+0x308/4]==0x207 && data[0]==8 && data[3]==2576);
 const auto rebuilt=session.register_blocks_rebuilt;session.serialize_registers(data,9,7);assert(session.register_blocks_rebuilt==rebuilt && data[0]==9 && data[1]==7);
 auto& alpha=s.rsx.registers[0x308];alpha=0;session.serialize_registers(data,8,8);assert(data[16+0x308/4]==0 && session.register_blocks_rebuilt==rebuilt+1);
 alpha=0x201;session.serialize_registers(data,8,8);assert(data[16+0x308/4]==0x201);s.rsx.registers.erase(0x308);session.serialize_registers(data,8,8);assert(data[16+0x1ff0/4]==s.rsx.registers[0x1ff0] && data[16+0x308/4]==0x207);
 s.rsx.registers[0x8cc]=0;session.serialize_registers(data,8,8);assert(data[9]==1);s.rsx.registers.erase(0x8cc);session.serialize_registers(data,8,8);assert(data[9]==0);
 std::puts("PASS generations, retained borrows, copy, shader START/masks and exact constant/register blocks");
}
static void claims_(){
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;m.regions.push_back({0x71000000,6,m.regions[0].bytes});m.enable_concurrency();
 m.store(0x70008000,8,0x1122334455667788);auto range=m.range(0x71008002,4,true);
 const std::uint8_t bytes[]={0xaa,0xbb,0xcc,0xdd};
 {GuestMemory::RawPermit legacy(m);const auto depth=m.local_state().raw;
  {rsxdraw::ClaimTransfer transfer(range,0,4);assert(!m.local_state().raw);transfer.write(0,bytes,4);}
  assert(m.local_state().raw==depth);}
 assert(m.load(0x70008000,8)==0x1122aabbccdd7788);
 auto word=m.range(0x71008000,4,true);{rsxdraw::ClaimTransfer transfer(word,0,4);assert(transfer.fetch_or32(0,0x80000001)==0x1122aabb);}
 assert(m.load(0x70008000,8)==0x9122aabbccdd7788);
 auto stale=m.range(0x70008010,4,true);{GuestMemory::RawPermit raw(m);m.regions[0].flags=4;m.mapping_changed();}
 bool stopped=false;try{rsxdraw::ClaimTransfer transfer(stale,0,4);transfer.write(0,bytes,4);}catch(const std::runtime_error&){stopped=true;}
 assert(stopped && m.load(0x71008010,4)==0);
 std::puts("PASS BE exact claims, neighbour preservation, alias, inherited permit and stale rights");
}
static void concurrent_completion_(){
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;m.store(0x70000080,4,0xcafebabe);m.enable_concurrency();
 NativeRsx private_gpu;private_gpu.local_size=s.rsx.local_size;NativeVulkan vk;
 // This Session has no PpuState reference; map inventory and issuer are snapshots.
 rsxgraphics::RendererContext context{m,private_gpu,0x12345678,77,{},m.regions,false};
 rsxgraphics::Session session(vk,std::move(context));session.observe();private_gpu.graphics_session=&session;
 std::mutex mutex;std::condition_variable changed;bool start=false,done=false;std::uint64_t actual=0;std::exception_ptr error;
 std::thread reader([&]{try{{std::unique_lock<std::mutex> lock(mutex);changed.wait(lock,[&]{return start;});}
   actual=m.load(0x70000080,4);m.release_reader_slot();
  }catch(...){error=std::current_exception();}
  {std::lock_guard<std::mutex> lock(mutex);done=true;}changed.notify_all();});
 bool progressed=false;
 session.state.event=[&](unsigned queue,std::uint64_t name,std::uint64_t flags){
  assert(!m.local_state().raw && queue==19 && name==0x1122334455667788 && flags==8);
  {std::lock_guard<std::mutex> lock(mutex);start=true;}changed.notify_all();
  std::unique_lock<std::mutex> lock(mutex);progressed=changed.wait_for(lock,std::chrono::seconds(3),[&]{return done;});
 };
 {GuestMemory::RawPermit raw(m);assert(session.defer_flip(0x70008400,0x70009400,3,19,0x1122334455667788,8));session.flush(false);}
 reader.join();if(error)std::rethrow_exception(error);assert(progressed && actual==0xcafebabe);
 assert(m.load(0x70008408,4)==0x80000000 && m.load(0x70008410,4)==3 && m.load(0x70009400,8)==0);
 private_gpu.graphics_session=nullptr;
 std::puts("PASS private renderer facade, snapshot maps and unrelated reader during claimed completion");
}

static void fifo_(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;auto& g=s.rsx;
 m.map(0x80000000,0x10000,6,nullptr,0);g.dma=0x70008000;g.reports=0x80000000;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();g.registers[0x17cc]=1;g.registers[0x1a8]=0x66626660;
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);packet(0x1800,1u<<24);
 m.store(g.dma+0x44,4,0x100);m.store(g.dma+0x40,4,put);m.enable_concurrency();
 {GuestMemory::RawPermit legacy(m);assert(rsx_run(s,1024) && m.local_state().raw==1);}
 // Polling reports may observe their old value until GPU completion.
 static_cast<rsxgraphics::Session*>(g.graphics_owner.get())->flush(false);
 const auto report=m.load(0x80001408,4);std::fprintf(stderr,"FIFO report=%llu pixels=%llu get=%llu put=%u\n",(unsigned long long)report,(unsigned long long)g.draw.pixels,(unsigned long long)m.load(g.dma+0x44,4),put);assert(m.load(g.dma+0x44,4)==put && report==64);
 assert(near_argb(m.load(0xc0100000,4),0x80ff8040));
 std::puts("PASS original FIFO packet path with inherited raw scope, Vulkan pixel and BE report");
}

static void sparse_surface_(){
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;m.store(0xc0011000,4,0x55667788);m.enable_concurrency();
 NativeVulkan vk;rsxgraphics::Session session(vk,s);auto target=rsxdraw::bind_target(s,0xfeed0000,0x10000,8192,8,2);
 auto& surface=session.bind(target,8,2,1,8,false);auto claim=rsxgraphics::surface_claim(surface);assert(claim && claim->claim);
 std::mutex mutex;std::condition_variable changed;bool done=false;std::uint64_t actual=0;
 std::thread reader([&]{actual=m.load(0xc0011000,4);m.release_reader_slot();{std::lock_guard<std::mutex> lock(mutex);done=true;}changed.notify_all();});
 bool progressed;{std::unique_lock<std::mutex> lock(mutex);progressed=changed.wait_for(lock,std::chrono::seconds(3),[&]{return done;});}
 const std::uint8_t pixel[]={0x11,0x22,0x33,0x44};claim->write(0,pixel,4);
 bool stopped=false;try{claim->write(4096,pixel,4);}catch(const std::runtime_error&){stopped=true;}
 claim.reset();reader.join();assert(progressed && actual==0x55667788 && stopped);
 assert(m.load(0xc0010000,4)==0x11223344 && m.load(0xc0011000,4)==0x55667788);
 std::puts("PASS sparse surface claim leaves padding pages available and rejects undeclared payload");
}
static void immutable_texture_(){
 PpuState s;setup_draw(s,8,8,32,false);texture_fp(s);auto& m=s.memory;m.enable_concurrency();
 const rsxdraw::Reg r{s.rsx};auto t=rsxdraw::decode_texture(s,r,0,false,true);assert(t.deferred);
 rsxdraw::materialize_texture(s,t);assert(t.untiled);const auto* bytes=static_cast<const rsxdraw::Texture&>(t).levels[0].data;
 std::uint8_t old[4];std::copy_n(bytes,4,old);const auto& run=t.plan->runs[0];
 auto range=rsxdraw::pinned_host_range(m,run.data,4,true);const std::uint8_t changed[]={0x01,0x23,0x45,0x67};range.write(0,changed,4);
 assert(std::memcmp(bytes,old,4)==0);auto second=rsxdraw::decode_texture(s,r,0,false,true);rsxdraw::materialize_texture(s,second);
 assert(std::memcmp(static_cast<const rsxdraw::Texture&>(second).levels[0].data,changed,4)==0);
 auto stale=rsxdraw::decode_texture(s,r,0,false,true);
 {GuestMemory::RawPermit raw(m);m.regions.push_back({0x81000000,6,m.regions[1].bytes});m.regions[1].flags=2;m.mapping_changed();}
 bool stopped=false;try{rsxdraw::materialize_texture(s,stale);}catch(const std::runtime_error&){stopped=true;}assert(stopped);
 std::puts("PASS immutable texture bytes, later snapshots and original map rights despite a readable alias");
}
int main(){try{bounds_();generations_();claims_();concurrent_completion_();fifo_();sparse_surface_();immutable_texture_();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''
class RsxVk17Tests(unittest.TestCase):
    def test_generations_bounds_claims_and_private_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cpp=root/'test.cpp';cpp.write_text(graphics_source().split('int main(int argc,char** argv)')[0]+MAIN)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread','-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES,'-o',str(root/'test')],capture_output=True,text=True,timeout=240)
            self.assertEqual(build.returncode,0,build.stderr)
            env=dict(os.environ,LP_NUM_THREADS='4',ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
            run=subprocess.run([str(root/'test')],env=env,capture_output=True,text=True,timeout=90)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertNotIn('Validation Error',run.stderr);self.assertNotIn('Validation Warning',run.stderr)
            self.assertEqual(run.stdout.count('PASS '),7)
