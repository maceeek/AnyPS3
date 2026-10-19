"""HLE services share memory admission, never a FIFO/map handoff."""
from pathlib import Path
import os
import subprocess
import unittest

ARTIFACTS = Path('local/ratchet/codex/par7/tests')

CPU_MAIN = r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);m.regions.push_back({0x20000,6,m.regions[0].bytes});m.mapping_changed();
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;
 auto unrelated_range=m.range(0x11000,4,true);
 auto unrelated=std::make_unique<GuestMemory::GpuPages>(m,std::vector<GuestMemory::HostSpan>{{unrelated_range.parts[0].data,4,unrelated_range.parts[0].owner}});
 std::atomic<bool> entered{false},finished{false};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{entered=true;
  const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
  while(!finished.load() && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
  assert(finished.load()); // HLE must complete before this owner operation.
 });
 while(!entered.load())std::this_thread::yield();
 auto barrier_count=w.map_barriers.load();auto host_count=w.host_requests.load();
 for(unsigned i=0;i<1000;++i){SpuServiceScope service(s);GuestMemory::RawPermit nested(m);
  m.store(0x10000,4,0x11223344);auto* p=m.access(0x20004,4,true);p[0]=0xca;p[1]=0xfe;p[2]=0xba;p[3]=0xbe;
  {GuestMemory::RawYield yield(m);assert(!m.local_state().raw);}
 }
 assert(w.map_barriers==barrier_count && w.host_requests==host_count);finished=true;w.queue->drain();unrelated.reset();
 assert(m.load(0x20000,4)==0x11223344 && m.load(0x10004,4)==0xcafebabe);
 // Both exclusive publishers used to store true instead of acquiring it.
 // Their native critical sections must never overlap (also run under TSan).
 unsigned payload=0;std::atomic<bool> done{false};
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{for(unsigned i=0;i<3000;++i){if(i&1){GuestMemory::Lease read(m,0x10000,4,false);++payload;}else{GuestMemory::RawPermit metadata(m);++payload;}}done=true;});
 for(unsigned i=0;i<3000;++i){SpuServiceScope service(s);++payload;}
 while(!done.load()){rsx_worker_pump(s);std::this_thread::yield();}w.queue->drain();assert(payload==6000);
 // A map transaction remains paused across a nested byte/coherence yield.
 {GuestMemory::ConfigurationPermit configuration(m);GuestMemory::RawPermit raw(m);
  {GuestMemory::RawYield yield(m);w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(w.paused);});}
  m.regions.push_back({0x30000,6,std::make_shared<GuestBacking>(4096,0)});m.mapping_changed();
  {GuestMemory::RawYield yield(m);w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(w.paused);});}
 }
 w.queue->drain();assert(w.map_barriers==barrier_count+1 && m.load(0x30000,4)==0);
 life.finish();
 // A failed owner during a yielded configuration clears its callbacks.
 // Destructors must preserve the first error, even after that cancellation.
 PpuState bad;bad.memory.map(0x10000,4096,6,nullptr,0);SpuWorkerSession bad_life(bad);bool exact=false;
 try{GuestMemory::ConfigurationPermit config(bad.memory);GuestMemory::RawPermit raw(bad.memory);GuestMemory::RawYield yield(bad.memory);
  bad.process.rsx_worker.queue->call(RsxOwnerQueue::Kind::HostAccess,[]{throw std::runtime_error("configuration owner failure");});
 }catch(const std::runtime_error& e){exact=std::string(e.what())=="configuration owner failure";}
 assert(exact && !bad.process.rsx_worker.enabled && !bad.memory.configuration_depth && !bad.memory.local_state().raw);
 std::puts("PASS HLE independent of busy owner, exclusive publishers, alias BE, nested map handoff and configuration poison");
'''

GPU_MAIN = r'''
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;setup_fifo(s);s.rsx.registers[0xa74]=1;s.rsx.registers[0xa70]=1;s.rsx.registers[0xa6c]=0x207;auto& m=s.memory;auto retained_color=m.range(0xc0100000,4,false);SpuWorkerSession life(s);auto& w=s.process.rsx_worker;
 // Drain acknowledges requests already queued; a request can be elided while
 // an older tick is active. Prove the submitted FIFO prefix actually ran.
 auto finish_fifo=[&]{const auto put=m.load(s.rsx.dma+0x40,4);
  const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(5);
  do{rsx_worker_request(s,1024);w.queue->drain();if(m.load(s.rsx.dma+0x44,4)==put)return;
   assert(std::chrono::steady_clock::now()<until);std::this_thread::yield();}while(true);
 };

 finish_fifo();
 auto* original=static_cast<rsxgraphics::Session*>(s.rsx.graphics_owner.get());assert(original);
 auto* original_batch=original->batch.get();const auto barriers=w.map_barriers.load(),requests=w.host_requests.load();
 unsigned surfaces=0;w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{surfaces=original->surfaces.size();assert(surfaces>0);});
 for(unsigned i=0;i<1000;++i){SpuServiceScope service(s);m.store(0x70001000,4,i);assert(m.access(0x70001000,4,false)[3]==std::uint8_t(i));}
 assert(w.map_barriers==barriers && w.host_requests==requests);
 // A true Session guard fault must retire only its physical page. The
 // independent depth target is on another page and must remain resident.
 volatile const std::uint8_t* color=retained_color.parts[0].data;
 {SpuServiceScope service(s);assert((color[0]==0x80 || color[0]==0x7f) && color[1]==0xff);}
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(original->surfaces.size()+1==surfaces && original->batch.get()==original_batch);});
 // Redraw the original target so the subsequent scalar-write oracle begins
 // with an actual dirty resident GPU color image again.
 unsigned again=m.load(s.rsx.dma+0x40,4);
 auto again_packet=[&](unsigned reg,unsigned value){m.store(0x70000000+again,4,(1u<<18)|reg);m.store(0x70000000+again+4,4,value);again+=8;};
 again_packet(0x1808,8);again_packet(0x1814,3u<<24);again_packet(0x1808,0);m.store(s.rsx.dma+0x40,4,again);finish_fifo();
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(s.rsx.graphics_owner.get()==original && original->batch.get()==original_batch && original->surfaces.size()==surfaces);
  assert(original->defer_label(0x70008400,0x11223344));original->leave_fifo();});
 // Both validated scalar and raw pointer APIs see completed bytes inside HLE.
 // An ordinary poll is allowed to be old until this prefix completes.
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{original->retire(true);});
 {SpuServiceScope service(s);assert(m.load(0x70008400,4)==0x11223344);}
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(original->defer_label(0x70008410,0x55667788));original->leave_fifo();});
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{original->retire(true);});
 {SpuServiceScope service(s);const auto* p=m.access(0x70008410,4,false);assert(p[0]==0x55 && p[1]==0x66 && p[2]==0x77 && p[3]==0x88);}
 assert(w.map_barriers==barriers);
 // An unrelated map addition and removal preserve resident images and batch.
 m.map(0x90000000,4096,6,nullptr,0);w.queue->drain();
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(s.rsx.graphics_owner.get()==original && original->surfaces.size()==surfaces && original->batch.get()==original_batch);});
 {GuestMemory::ConfigurationPermit config(m);GuestMemory::RawPermit raw(m);m.regions.pop_back();m.mapping_changed();}w.queue->drain();
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(original->surfaces.size()==surfaces && original->batch.get()==original_batch);});
 // A second main-memory color target exercises IO unmap with actual dirty
 // GPU bytes. The old VRAM color/depth images must survive that invalidation.
 m.map(0x90000000,0x100000,6,nullptr,0);
 {SpuServiceScope service(s);GuestMemory::ConfigurationPermit config(m);s.rsx.context=0x55555555;}
 {SpuServiceScope service(s);s.r[11]=672;s.r[3]=0x55555555;s.r[4]=0x100000;s.r[5]=0x90000000;s.r[6]=0x100000;s.r[7]=0;assert(rsx_syscall(s)==0);}
 unsigned put=m.load(s.rsx.dma+0x40,4);
 auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 packet(0x194,0xfeed0001);packet(0x210,0x100000);packet(0x1808,8);packet(0x1814,3u<<24);packet(0x1808,0);m.store(s.rsx.dma+0x40,4,put);
 finish_fifo();
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(original->surfaces.size()>surfaces);});
 {SpuServiceScope service(s);s.r[11]=673;s.r[3]=0x55555555;s.r[4]=0x100000;s.r[5]=0x100000;assert(rsx_syscall(s)==0);}
 w.queue->drain();assert(near_argb(m.load(0x90000000,4),0x80ff8040));
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(s.rsx.graphics_owner.get()==original && original->surfaces.size()==surfaces && original->batch.get()==original_batch);});
 {GuestMemory::ConfigurationPermit config(m);GuestMemory::RawPermit raw(m);m.mark_write(m.regions.back().bytes->data(),m.regions.back().bytes->size());m.regions.pop_back();m.mapping_changed();}
 // Protected retained pointer inside a real HLE service: release byte
 // exclusion in the signal wait; the owner must acquire metadata to publish.
 auto range=m.range(0x70009000,4,true);volatile const std::uint8_t* retained=range.parts[0].data;
 std::unique_ptr<GuestMemory::HostGuard> guard;
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{guard=std::make_unique<GuestMemory::HostGuard>();assert(guard->add(range.parts[0].data,4,*range.parts[0].owner));
  guard->synchronize=[&]{GuestMemory::RawPermit metadata(m);assert(w.on_owner());range.mark(0,4);auto* p=range.parts[0].data;p[0]=0xab;p[1]=0xcd;p[2]=0xef;p[3]=0x01;};guard->arm();});
 {SpuServiceScope service(s);assert(m.local_state().raw && retained[0]==0xab && retained[1]==0xcd && m.local_state().raw);}
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{guard.reset();});
 // A scalar CPU write INSIDE HLE must materialize its image first,
 // preserve the adjacent GPU pixel and leave the depth image resident.
 {SpuServiceScope service(s);m.store(0xc0100000,4,0x11223344);}
 assert(m.load(0xc0100000,4)==0x11223344 && near_argb(m.load(0xc0100004,4),0x80ff8040));
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(!original->surfaces.empty() && original->surfaces.size()<surfaces);});
 // Rights revocation of the GPU color/depth backing: materialize using the
 // OLD snapshot, invalidate its resident images, preserve Session/device.
 auto pinned=m.range(0xc0100000,4,true);
 {GuestMemory::ConfigurationPermit config(m);GuestMemory::RawPermit raw(m);
  for(auto& r:m.regions){if(r.base==0xc0000000)r.flags=4;}m.mapping_changed();
 }
 w.queue->drain();bool stale=false;try{std::uint8_t bytes[4]{};pinned.write(0,bytes,4);}catch(const std::runtime_error&){stale=true;}assert(stale);
 assert(m.load(0xc0100000,4)==0x11223344);
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{assert(s.rsx.graphics_owner.get()==original && original->batch.get()==original_batch && original->surfaces.empty());});
 life.finish();std::puts("PASS resident Session/batch/images, raw HLE read/fault, selective map invalidation and revoked identity");
'''


FAULT_FAILURE = r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;auto& m=s.memory;m.map(0x10000,8192,6,nullptr,0);auto range=m.range(0x10000,4,true);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;std::unique_ptr<GuestMemory::HostGuard> guard;
 w.queue->call(RsxOwnerQueue::Kind::HostAccess,[&]{guard=std::make_unique<GuestMemory::HostGuard>();assert(guard->add(range.parts[0].data,4,*range.parts[0].owner));guard->synchronize=[]{throw std::runtime_error("independent raw HLE fault failure");};guard->arm();});
 {SpuServiceScope service(s);volatile const std::uint8_t* p=range.parts[0].data;auto value=p[0];(void)value;}
 assert(false);
'''


def native_code(gpu=False, failure=False):
    if gpu:
        from tests.test_rsx_graphics import native_source
        from tests.test_par6 import MAIN
        setup = MAIN[:MAIN.index('static void fifo(bool lanes)')]
        return native_source().split('int main(int argc,char** argv)')[0]+setup+'\nint main(){'+GPU_MAIN+'}\n'
    from tests.test_par1 import source
    return source(FAULT_FAILURE if failure else CPU_MAIN, full=True)


class Parallel7ServiceContracts(unittest.TestCase):
    def run_case(self, name, gpu=False, failure=False):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp=ARTIFACTS/(name+'.cpp');exe=cpp.with_suffix('');cpp.write_text(native_code(gpu, failure))
        cmd=['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-function','-pthread']
        if gpu:
            from tests.test_rsx_vk import LIBRARIES
            cmd += ['-DANYPS3_VULKAN','-DANYPS3_RSX_VK',str(cpp),*LIBRARIES]
        else: cmd += [str(cpp)]
        result=subprocess.run([*cmd,'-o',str(exe)],capture_output=True,text=True,timeout=300)
        cpp.with_suffix('.build.log').write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode,0,result.stderr[-12000:])
        env=dict(os.environ,ANYPS3_SPU_WORKER='0',LP_NUM_THREADS='4')
        if gpu: env.update(ANYPS3_VK_DEVICE='lavapipe',VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation',VK_LAYER_VALIDATE_SYNC='1')
        result=subprocess.run([str(exe)],env=env,capture_output=True,text=True,timeout=60)
        cpp.with_suffix('.run.log').write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode,125 if failure else 0,result.stdout+result.stderr[-12000:])
        if failure:self.assertIn("independent raw HLE fault failure",result.stderr)
        self.assertNotIn('Validation Error',result.stderr)
        self.assertNotIn('Validation Warning',result.stderr)

    def test_services_do_not_wait_for_unrelated_rsx(self):
        self.run_case('services')

    def test_resident_session_raw_fault_and_selective_maps(self):
        self.run_case('resident',gpu=True)

    def test_raw_hle_fault_owner_failure_exits_without_joining_own_handler(self):
        self.run_case("fault-failure",failure=True)
