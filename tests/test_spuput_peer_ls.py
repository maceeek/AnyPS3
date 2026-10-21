"""SPUPUT: a pool lane's GET from a peer's LS (thread group EA window 0xf0000000 + n*0x100000) skips the owner RPC + peer pause
only while the peer is provably quiescent (parked or between quanta, not paused by an owner scope, not inside its own owner_call),
or lets a running peer copy the bytes itself at its next quantum boundary. Every other state keeps the owner path; every result
reflects the peer LS at a quantum boundary.

Independent expectations: fixed big-endian patterns written by the test (not by the code under test); owner-side LS writes that
happen while the peer is paused/inside its owner call must be visible to the reader (they would be missed by an unsynchronised
direct read); a peer mid-step never exposes a half-written pair of quadwords.
"""
import unittest

from tests import test_par15_rpc as par15

COMMON = par15.SETUP + r'''
 [[maybe_unused]] auto store_be=[](std::uint8_t* at,std::uint32_t v){at[0]=std::uint8_t(v>>24);at[1]=std::uint8_t(v>>16);at[2]=std::uint8_t(v>>8);at[3]=std::uint8_t(v);};
 for(unsigned i=0;i<3;++i)g.threads[i].pool_lane=pool.lanes[i].get();
 auto* peer_ls=g.context.data()+0x40000;
 [[maybe_unused]] auto fill=[&](std::uint8_t* at,unsigned n,std::uint32_t seed){for(unsigned i=0;i<n;i+=4)store_be(at+i,seed+i);};
 [[maybe_unused]] auto same=[&](const std::uint8_t* at,unsigned n,std::uint32_t seed){for(unsigned i=0;i<n;i+=4)if(spu_load_word(at+i)!=seed+i)return false;return true;};
 [[maybe_unused]] auto metric=[&]{return pool.rpc_metrics[{SpuOwnerOperation::Transfer,0}].calls;};
'''

PARKED = COMMON + r'''
 fill(peer_ls+0x100,0x40,0x11223300);fill(peer_ls+0x7700,16,0xa0b0c000);
 std::atomic<bool> parked{false};
 peer.step=[&](unsigned){peer.event_driven=true;parked=true;return false;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;
  while(!parked.load())std::this_thread::yield();
  for(;;){std::lock_guard<std::mutex> lock(peer.mutex);if(!peer.active)break;}
  auto& t=g.threads[0];auto* ls=g.context.data();
  spu_transfer(s,t,0xf0107700,ls+0x200,16,false);
  spu_transfer(s,t,0xf0100100,ls+0x300,0x40,false);
  assert(same(ls+0x200,16,0xa0b0c000) && same(ls+0x300,0x40,0x11223300));
  done=true;return false;};
 peer.launch();unrelated.launch();caller.launch();
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 release=true;pool.shutdown();
 assert(NativeSpuWorker::peer_direct_reads.load()==2);
 assert(metric()==0);
 std::puts("PASS parked peer LS read directly, exact bytes, no owner RPC");
'''

# The peer runs a step that writes two quadwords with a pause in between; a reader may only observe both or neither.
ACTIVE = COMMON + r'''
 std::atomic<std::uint32_t> generation{0};
 fill(peer_ls+0x400,32,0);
 peer.step=[&](unsigned){if(release.load())return false;const auto n=generation.fetch_add(1)+1;
  for(unsigned i=0;i<4;++i)store_be(peer_ls+0x400+4*i,n);
  std::this_thread::sleep_for(std::chrono::microseconds(50));
  for(unsigned i=0;i<4;++i)store_be(peer_ls+0x410+4*i,n);
  return true;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;
  while(generation.load()<2)std::this_thread::yield();
  auto& t=g.threads[0];auto* ls=g.context.data();std::uint32_t last=0;
  for(unsigned k=0;k<200;++k){
   spu_transfer(s,t,0xf0100400,ls+0x500,32,false);
   const auto first=spu_load_word(ls+0x500);
   for(unsigned i=0;i<8;++i)assert(spu_load_word(ls+0x500+4*i)==first);
   assert(first>=last);last=first;
  }
  done=true;return false;};
 peer.launch();unrelated.launch();caller.launch();
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 release=true;pool.shutdown();
 assert(metric()+NativeSpuWorker::peer_direct_reads.load()+NativeSpuWorker::peer_boundary_reads.load()==200);
 assert(NativeSpuWorker::peer_boundary_reads.load()>0);
 std::puts("PASS running peer: quantum-boundary snapshots only (served by the peer at its boundary)");
'''

# The peer waits inside its own owner_call (queued, not yet served: the owner has not taken any lock for it); serving it writes the
# peer's LS. A reader attempting meanwhile must take the owner path (served after the peer's request) and see the written bytes.
OWNER_CALL = COMMON + r'''
 fill(peer_ls+0x600,16,0x01010100);
 std::atomic<bool> attempting{false},in_call{false};
 peer.step=[&](unsigned){peer.event_driven=true;if(release.load() || done.load() || in_call.load())return false;
  in_call=true;
  assert(spu_owner_call(s,g.threads[1],[&]{fill(peer_ls+0x600,16,0x02020200);},{SpuOwnerOperation::Channel,30,0}));
  return false;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;
  while(!in_call.load())std::this_thread::yield();
  for(;;){std::lock_guard<std::mutex> lock(peer.mutex);if(peer.owner_rpc_depth)break;}
  auto& t=g.threads[0];auto* ls=g.context.data();
  attempting=true;spu_transfer(s,t,0xf0100600,ls+0x700,16,false);
  assert(same(ls+0x700,16,0x02020200));
  done=true;return false;};
 peer.launch();unrelated.launch();caller.launch();
 while(!attempting.load())std::this_thread::yield();
 std::this_thread::sleep_for(std::chrono::milliseconds(30));
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 release=true;pool.shutdown();
 assert(NativeSpuWorker::peer_direct_reads.load()==0);
 assert(metric()==1);
 std::puts("PASS peer inside its owner call is not read directly");
'''

# An owner scope pauses the peer and writes its LS; a concurrent reader must wait for the owner path.
OWNER_PAUSE = COMMON + r'''
 fill(peer_ls+0x800,16,0x05050500);
 std::atomic<bool> parked{false},paused{false},attempting{false};
 peer.step=[&](unsigned){peer.event_driven=true;parked=true;return false;};
 caller.step=[&](unsigned){caller.event_driven=true;if(done.load())return false;
  while(!paused.load())std::this_thread::yield();
  auto& t=g.threads[0];auto* ls=g.context.data();
  attempting=true;spu_transfer(s,t,0xf0100800,ls+0x900,16,false);
  assert(same(ls+0x900,16,0x06060600));
  done=true;return false;};
 peer.launch();unrelated.launch();
 while(!parked.load())std::this_thread::yield();
 caller.launch();
 {SpuOwnerScope scope(s,peer.lane_thread_id,true,false,false);paused=true;
  while(!attempting.load())std::this_thread::yield();
  std::this_thread::sleep_for(std::chrono::milliseconds(30));fill(peer_ls+0x800,16,0x06060600);}
 while(!done.load()){pool.drain_owner();std::this_thread::yield();}
 release=true;pool.shutdown();
 assert(NativeSpuWorker::peer_direct_reads.load()==0);
 assert(metric()==1);
 std::puts("PASS owner-paused peer is not read directly");
'''


# Two running lanes read each other's LS: neither may wait forever for the other's quantum boundary (bounded wait, owner fallback).
MUTUAL = COMMON + r'''
 auto* caller_ls=g.context.data();
 fill(caller_ls+0x1000,16,0x0a0a0a00);fill(peer_ls+0x1000,16,0x0b0b0b00);
 std::atomic<unsigned> finished{0};
 auto body=[&](NativeSpuWorker& self,unsigned me,std::uint32_t other_ea,std::uint32_t expect){
  return [&,me,other_ea,expect](unsigned){self.event_driven=true;
   if(release.load())return false;
   static thread_local unsigned k=0;
   if(k>=300)return false;
   auto& t=g.threads[me];auto* ls=g.context.data()+me*0x40000;
   spu_transfer(s,t,other_ea,ls+0x2000,16,false);assert(same(ls+0x2000,16,expect));
   if(++k==300)++finished;
   return true;};};
 caller.step=body(caller,0,0xf0101000,0x0b0b0b00);
 peer.step=body(peer,1,0xf0001000,0x0a0a0a00);
 peer.launch();unrelated.launch();caller.launch();
 const auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(10);
 while(finished.load()<2){assert(std::chrono::steady_clock::now()<limit);pool.drain_owner();std::this_thread::yield();}
 done=true;release=true;pool.shutdown();
 assert(metric()+NativeSpuWorker::peer_direct_reads.load()+NativeSpuWorker::peer_boundary_reads.load()==600);
 std::puts("PASS mutual running peers complete through bounded waits");
'''

# Pool shutdown destroys the lanes while the guest thread records survive (a later session relaunches lanes one at a time): no record may
# keep pointing at a destroyed lane, or the next session's first peer-LS GET dereferences it. A pointer that names another (newer) lane
# is not touched.
RESTART = COMMON + r'''
 peer.step=[&](unsigned){peer.event_driven=true;return false;};
 caller.step=[&](unsigned){caller.event_driven=true;return false;};
 NativeSpuWorker newer;g.threads[2].pool_lane=&newer;(void)peer_ls;
 peer.launch();unrelated.launch();caller.launch();
 release=true;pool.shutdown();
 assert(pool.lanes.empty() && s.spu.groups.size()==1);
 assert(g.threads[0].pool_lane==nullptr && g.threads[1].pool_lane==nullptr && g.threads[2].pool_lane==&newer);
 g.threads[2].pool_lane=nullptr;
 std::puts("PASS pool shutdown unpublishes destroyed lanes from surviving thread records");
'''

# spu_pool_refresh: the second lane's start fails (injected thread-creation error) while the first lane already runs. No thread record may
# name a worker the pool does not own (the failed one is destroyed), the failed thread's wake-ups must not reach it, and a retry
# completes the pool. Only pool-owned, launched lanes are ever published.
REFRESH_FAILURE = r'''
 PpuState s;auto& pool=s.process.spu_worker;pool.enabled=true;pool.pool_mode=true;
 pool.coordinator_clock.enabled=true;pool.refresh_pending=false;pool.pauses=1;   // lanes park (acknowledged) without stepping
 NativeSpu::Group initial;initial.id=7;initial.state=6;initial.threads.resize(3);initial.context.resize(3*0x40000);
 for(unsigned i=0;i<3;++i){initial.threads[i].id=17+i;initial.threads[i].number=i;}
 s.spu.groups.push_back(std::move(initial));auto& g=s.spu.groups.back();
 unsigned launches=0;
 NativeSpuWorker::launch_fault=[&](NativeSpuWorker&){if(++launches==2)throw std::system_error(std::make_error_code(std::errc::resource_unavailable_try_again));};
 bool failed=false;
 try{spu_pool_refresh(s);}catch(const std::system_error&){failed=true;}
 assert(failed && launches==2 && pool.lanes.size()==1 && pool.lanes[0]->lane_thread_id==17);
 for(unsigned i=0;i<3;++i){void* p=g.threads[i].pool_lane;bool owned=!p;for(auto& lane:pool.lanes)owned=owned || p==lane.get();assert(owned);}
 assert(g.threads[0].pool_lane==pool.lanes[0].get() && !g.threads[1].pool_lane && !g.threads[2].pool_lane);
 g.threads[1].in_mailbox.push_back(5);g.threads[1].snr[0].push(6,false);    // wake-ups of the failed lane were withdrawn
 NativeSpuWorker::launch_fault=nullptr;
 spu_pool_refresh(s);
 assert(pool.lanes.size()==3);
 for(unsigned i=0;i<3;++i){bool found=false;for(auto& lane:pool.lanes)found=found || (g.threads[i].pool_lane==lane.get() && lane->context==&g.threads[i]);assert(found);}
 pool.shutdown();
 for(unsigned i=0;i<3;++i)assert(!g.threads[i].pool_lane);
 std::puts("PASS a failed lane start publishes nothing, withdraws its wake-ups, and a retry completes the pool");
'''


class SpuPutPeerLs(par15.Par15RpcContracts):
    def test_refresh_launch_failure_publishes_nothing(self):
        self.native('spuput-peer-refresh-failure', REFRESH_FAILURE)

    def test_shutdown_unpublishes_lanes(self):
        self.native('spuput-peer-restart', RESTART)

    def test_mutual_running_peers_terminate(self):
        self.native('spuput-peer-mutual', MUTUAL)

    def test_parked_peer_direct(self):
        self.native('spuput-peer-parked', PARKED)

    def test_running_peer_boundary_snapshots(self):
        self.native('spuput-peer-active', ACTIVE)

    def test_peer_in_owner_call_not_direct(self):
        self.native('spuput-peer-owner-call', OWNER_CALL)

    def test_owner_paused_peer_not_direct(self):
        self.native('spuput-peer-owner-pause', OWNER_PAUSE)

    # The inherited PAR15 tests run in their own module; do not repeat them here.
    test_signal_mailbox_and_sibling_progress = None
    test_ls_dma_pauses_only_target = None
    test_control_and_unknown_keep_conservative_pause = None


if __name__ == '__main__':
    unittest.main()
