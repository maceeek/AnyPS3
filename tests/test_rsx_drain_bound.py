"""Bounded host-request drain of the RSX owner (rsx_game_worker.py rsx_owner_drain) with independent oracles.

The owner serves page-synchronisation requests of other threads at FIFO packet boundaries and in the vsync wait. Producers (SPU lanes
touching guarded pages) refill the queue while it is served, so a drain that loops until the queue is empty could keep the owner
there indefinitely and starve FIFO progress, labels and the vblank deadline. Expectations, all derived from the contract and not from
the implementation:
  * prefix: a poll serves exactly the drainable requests queued when it started (here 3 of a producer that always keeps 3 more
    queued), in queue order, never past an untagged/other-kind request (barrier), and a drain inside a served request is a no-op;
  * sustained: with a producer that guarantees a non-empty queue at every poll, a real FIFO of 1500 colour clears still reaches PUT and
    the final surface equals the last clear colour (every pixel; the colour is the oracle, no reference run);
  * guarded read: a guest read of the GPU-authoritative surface from another thread while the FIFO runs returns, byte for byte, the
    uniform colour of a clear that had executed at service time: k_before <= k <= k_after, bracketed by GET read before and after the
    read; with the drain off it is served only after the run (k == N).
"""
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

ARTIFACTS = Path('local/ratchet/codex/frametime/tests')
MAIN = r'''
static constexpr unsigned kClears=1500;
static std::uint32_t clear_color(unsigned k){return 0x40000000u+k*0x00010203u;}   // distinct for k < 2^24, never zero
// kClears groups of {clear colour k+1, clear surface}; the surface is 32x16 A8R8G8B8, so every pixel of the clear k holds clear_color(k) big-endian.
static unsigned setup_clears(PpuState& s){
 setup_draw(s,32,16,128,false);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x80000000,0x10000,6,nullptr,0);g.dma=0x70008000;g.reports=0x80000000;g.fifo_enabled=true;g.draw.primitive=0;g.draw.ranges.clear();
 g.registers[0x17cc]=1;g.registers[0x1a8]=0x66626660;g.registers[0x8c0]=32u<<16;g.registers[0x8c4]=16u<<16;   // scissor = whole surface
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned k=1;k<=kClears;++k){packet(0x1d90,clear_color(k));packet(0x1d94,0xf3);}
 m.store(g.dma+0x44,4,0x100);m.store(g.dma+0x40,4,put);return put;
}
static unsigned clears_done(std::uint64_t get){return unsigned((get-0x100)/16);}
// Index (1-based) of the clear whose colour fills the whole surface, 0 if the surface is not uniform or the colour is unknown.
static std::string dump(const std::vector<std::uint8_t>& p){char b[16];std::string t;for(unsigned i=0;i<32 && i<p.size();++i){std::snprintf(b,sizeof b,"%02x",p[i]);t+=b;if(i%4==3)t+=' ';}return t;}
static unsigned uniform_clear(const std::vector<std::uint8_t>& pixels){
 const std::uint32_t first=(std::uint32_t(pixels[0])<<24)|(std::uint32_t(pixels[1])<<16)|(std::uint32_t(pixels[2])<<8)|pixels[3];
 for(std::size_t i=0;i<pixels.size();i+=4)if(!(pixels[i]==pixels[0]&&pixels[i+1]==pixels[1]&&pixels[i+2]==pixels[2]&&pixels[i+3]==pixels[3]))return 0;
 for(unsigned k=1;k<=kClears;++k)if(clear_color(k)==first)return k;
 return 0;
}
static void start(PpuState& s,unsigned groups_dummy=0){(void)groups_dummy;
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","0",1);(void)s;
}
static std::uint64_t now_ms(){return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
using Hook=std::function<void()>;

// Phases A and B: direct polls on the owner thread.
static void prefix(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;setup_clears(s);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);
 assert(!rsx_worker_request(s,64).completed);w.queue->drain();
 auto* f=static_cast<PpuState*>(w.facade.get());
 std::atomic<bool> go{false},producing{true};std::atomic<unsigned> served{0},need{0},posted{0};
 std::mutex order_mutex;std::vector<unsigned> order;unsigned refill_id=100;std::atomic<bool> refill_timeout{false};
 const unsigned refill_limit=12;   // requests number 1..12 each make the producer queue one more
 std::function<Hook(unsigned)> make;
 make=[&](unsigned id)->Hook{return [&,id]{
  {std::lock_guard<std::mutex> lock(order_mutex);order.push_back(id);}
  const unsigned n=++served;
  if(n<=refill_limit){need.store(n);const auto until=now_ms()+5000;while(producing && posted.load()<n && now_ms()<until)std::this_thread::yield();   // the successor is queued before this request returns
   if(producing && posted.load()<n)refill_timeout=true;}
 };};
 std::thread producer([&]{while(producing){
  if(posted.load()<need.load()){w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(refill_id++),0,1);++posted;}else std::this_thread::yield();}});
 // ---- A: prefix, barrier and recursion guard ----
 unsigned served_a=99,nested=99;bool d3_ran_inside=false;
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  while(!go.load())std::this_thread::yield();
  unsigned count=0;rsx_owner_drain(w,f,&count);served_a=count;
  {std::lock_guard<std::mutex> lock(order_mutex);d3_ran_inside=std::find(order.begin(),order.end(),3u)!=order.end();}
 });
 // D1 calls the drain from inside a served request: the recursion guard makes it a no-op
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{{std::lock_guard<std::mutex> lock(order_mutex);order.push_back(1);}unsigned inner=0;rsx_owner_drain(w,f,&inner);nested=inner;++served;},0,1);
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(2),0,1);
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{std::lock_guard<std::mutex> lock(order_mutex);order.push_back(50);},0,0);   // untagged: barrier
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(3),0,1);
 go=true;
 // the owner returns from the blocker, then serves barrier and D3 normally; wait until everything including all refills is done
 {const auto until=now_ms()+20000;while(now_ms()<until){std::lock_guard<std::mutex> lock(order_mutex);if(std::find(order.begin(),order.end(),3u)!=order.end() && served.load()>=refill_limit+2)break;}std::this_thread::yield();}
 producing=false;producer.join();w.queue->drain();
 if(refill_timeout)throw std::runtime_error("phase A: the producer did not queue the successor within 5 s (the premise 'the queue is never empty' was not established)");
 if(served_a!=2 || nested!=0 || d3_ran_inside)throw std::runtime_error("phase A: drain served "+std::to_string(served_a)+" requests (expected the 2 before the barrier), nested drain served "+std::to_string(nested)+(d3_ran_inside?", D3 ran behind the barrier":""));
 if(order.size()<5 || order[0]!=1 || order[1]!=2 || order[2]!=50 || order[3]!=3)throw std::runtime_error("phase A: service order is not queue order");
 std::puts("PASS prefix: a poll serves the drainable requests before the barrier only, in order; a nested drain serves nothing");

 // ---- B: sustained refill: every poll serves exactly the 3 queued requests, never the ones queued meanwhile ----
 go=false;producing=true;served=0;need=0;posted=0;order.clear();refill_id=100;
 std::vector<unsigned> counts;std::atomic<unsigned> polls_done{0};
 std::thread producer2([&]{while(producing){
  if(posted.load()<need.load()){w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(refill_id++),0,1);++posted;}else std::this_thread::yield();}});
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  while(!go.load())std::this_thread::yield();
  for(unsigned poll=0;poll<5;++poll){unsigned count=0;rsx_owner_drain(w,f,&count);counts.push_back(count);polls_done.store(unsigned(counts.size()));}
 });
 // refill_limit 12 and 3 initial requests: polls of 3 requests whose 12 refills fill the 4 later polls; no request is left behind
 for(unsigned id=1;id<=3;++id){w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(id),0,1);}
 go=true;
 {const auto until=now_ms()+20000;while(now_ms()<until){if(served.load()>=15 && polls_done.load()==5)break;std::this_thread::yield();}}
 producing=false;producer2.join();w.queue->drain();
 if(refill_timeout)throw std::runtime_error("phase B: the producer did not queue the successor within 5 s (the premise 'the queue is never empty' was not established)");
 std::string text;for(const auto c:counts)text+=" "+std::to_string(c);
 if(counts!=std::vector<unsigned>{3,3,3,3,3})throw std::runtime_error("phase B: requests served per poll:"+text+" (expected 3 3 3 3 3)");
 std::vector<unsigned> expected_order{1,2,3};for(unsigned id=100;id<112;++id)expected_order.push_back(id);
 if(order!=expected_order)throw std::runtime_error("phase B: service order is not queue order");
 life.finish();
 std::puts("PASS sustained refill: every poll serves exactly the requests queued at its start (3 3 3 3 3), in queue order");
}

// Phase C: a producer that guarantees a non-empty queue at every poll cannot stop a real FIFO.
static void sustained(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;const auto put=setup_clears(s);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);
 std::atomic<bool> producing{true},refill_timeout{false};std::atomic<unsigned> served{0},need{0},posted{0};
 auto make=[&]()->Hook{return [&]{const unsigned n=++served;need.store(n+1);
  const auto until=now_ms()+5000;while(producing && posted.load()<n+1 && now_ms()<until)std::this_thread::yield();
  if(producing && posted.load()<n+1)refill_timeout=true;};};   // the next request is queued before this one returns
 std::thread producer([&]{while(producing){if(posted.load()<need.load()){w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(),0,1);++posted;}else std::this_thread::yield();}});
 rsx_host_waiters.fetch_add(1);                                      // a requester is blocked on a guarded page: the owner polls at every packet
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(),0,1);++posted;
 w.queue->post(RsxOwnerQueue::Kind::HostAccess,make(),0,1);++posted;
 assert(!rsx_worker_request(s,1<<20).completed);
 const auto until=now_ms()+20000;bool finished=false;
 while(now_ms()<until){if(s.memory.load(0x70008044,4)==put){finished=true;break;}std::this_thread::yield();}
 const unsigned served_mid_run=served.load();
 producing=false;producer.join();rsx_host_waiters.fetch_sub(1);
 w.queue->drain();
 if(refill_timeout)throw std::runtime_error("sustained: the producer did not queue the successor within 5 s (the premise 'the queue is never empty' was not established)");
 if(!finished)throw std::runtime_error("sustained producer starved the FIFO: GET stayed at "+std::to_string(s.memory.load(0x70008044,4))+" of "+std::to_string(put));
 std::vector<std::uint8_t> pixels(32*16*4);s.memory.transfer(0xc0100000,pixels.data(),pixels.size(),false);
 if(uniform_clear(pixels)!=kClears)throw std::runtime_error("final surface is not the last clear colour: "+dump(pixels)+" expected "+std::to_string(clear_color(kClears)));
 if(served_mid_run<10)throw std::runtime_error("the producer's requests were not served during the run ("+std::to_string(served_mid_run)+")");
 life.finish();
 std::printf("PASS sustained producer: the FIFO reached PUT while %u producer requests were served; final surface is the last clear colour\n",served_mid_run);
}

// Phase D: a guarded guest read from another thread while the FIFO runs is served between packets and returns uniform clear bytes.
// Returns false when the read was served after the run although the run was under way when it started: a descheduled reader and a drain
// that never served mid-run look the same in one execution, so the caller (exit status 77) repeats the execution and fails only if every attempt is inconclusive.
static bool guarded_read(bool drain_on){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;const auto put=setup_clears(s);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);
 assert(!rsx_worker_request(s,1<<20).completed);
 while(s.memory.load(0x70008044,4)<0x100+16*50)std::this_thread::yield();   // the run is under way: at least 50 clears executed
 const auto get_before=s.memory.load(0x70008044,4);
 std::vector<std::uint8_t> pixels(32*16*4);
 s.memory.transfer(0xc0100000,pixels.data(),pixels.size(),false);            // guarded: the observer asks the owner to materialise the GPU surface
 const auto get_after=s.memory.load(0x70008044,4);
 const unsigned k=uniform_clear(pixels),k_before=clears_done(get_before),k_after=clears_done(get_after);
 if(!k)throw std::runtime_error("guarded read returned a surface that is not one clear colour");
 if(k<k_before || k>k_after)throw std::runtime_error("guarded read returned clear "+std::to_string(k)+" outside the bracket ["+std::to_string(k_before)+","+std::to_string(k_after)+"]");
 w.queue->drain();assert(s.memory.load(0x70008044,4)==put);
 std::vector<std::uint8_t> last(pixels.size());s.memory.transfer(0xc0100000,last.data(),last.size(),false);
 if(uniform_clear(last)!=kClears)throw std::runtime_error("final surface is not the last clear colour");
 life.finish();
 if(drain_on){if(k>=kClears){std::printf("INCONCLUSIVE guarded read, drain on: served after the run (clear %u in [%u,%u])\n",k,k_before,k_after);return false;}
  std::printf("PASS guarded read, drain on: served mid-run, clear %u in [%u,%u] of %u\n",k,k_before,k_after,kClears);}
 else{if(k!=kClears)throw std::runtime_error("drain off: the read returned clear "+std::to_string(k)+" instead of the final one");
  std::printf("PASS guarded read, drain off: served after the run (clear %u)\n",k);}
 return true;
}

// Phase E: a real FIFO with flips. Frame i: clear(colour i); label A_i (front-end release, deferred behind the GPU prefix); flip gate
// release (immediate); queue display + flip; label B_i. Publication order is a contract of the FIFO: A_i, then the completion of flip i
// (gate cleared, one flip event), then B_i. A guest thread that reads in REVERSE program order (B, events, gate, A) must therefore never
// see B_j without the completed flips <= j and all labels A_i, i <= j. The first 8 flips take the synchronous capture path of
// rsx_graphics_flip_async (ANYPS3_FRAME_DIR, huge stride); every later flip is deferred on the GPU timeline.
static constexpr unsigned kFlips=24;
static std::uint32_t gate_word(unsigned i){return 0x50000001u+i;}   // the 32-bit semaphore release word of flip i (stored big endian at the gate, high half of the 8-byte word)
static unsigned setup_flips(PpuState& s,unsigned clear_mask=0xf3){
 setup_draw(s,32,16,128,false);auto& g=s.rsx;auto& m=s.memory;
 m.map(0x80000000,0x10000,6,nullptr,0);m.map(0x72000000,0x3000,6,nullptr,0);
 g.dma=0x70008000;g.reports=0x80000000;g.driver=0x72000000;g.fifo_enabled=true;g.context=1;g.draw.primitive=0;g.draw.ranges.clear();
 g.registers[0x17cc]=1;g.registers[0x1a8]=0x66626660;g.registers[0x8c0]=32u<<16;g.registers[0x8c4]=16u<<16;
 g.event_queue=123;g.event_port=456;g.display_buffers[1]={32,16,128,0x100000};g.flip_modes[1]=2;
 s.events.queues.push_back({123,4096,0,{}});s.events.ports.push_back({456,1,123,789});
 m.store(g.driver+0x12c0,4,0x50);                          // queue-display (0x40) and flip (0x10) events of head 1; no vblank events
 m.store(g.reports+0x10,8,UINT64_MAX);
 unsigned put=0x100;auto packet=[&](unsigned reg,unsigned value){m.store(0x70000000+put,4,(1u<<18)|reg);m.store(0x70000000+put+4,4,value);put+=8;};
 for(unsigned i=0;i<kFlips;++i){
  packet(0x1d90,clear_color(i+1));packet(0x1d94,clear_mask);
  packet(0x60,0x66606660);packet(0x64,0x200+16*i);packet(0x6c,0x1000+i);                       // label A_i
  packet(0x64,0x10);packet(0x6c,gate_word(i));                                       // flip gate: written at once, cleared by the flip
  packet(0xe944,1);packet(0xe924,0x8000010f);
  packet(0x64,0x400+16*i);packet(0x6c,0x2000+i);                                               // label B_i
 }
 m.store(g.dma+0x44,4,0x100);m.store(g.dma+0x40,4,put);return put;
}
static void flip_order(bool capture=false,unsigned clear_mask=0xf3){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;const auto put=setup_flips(s,clear_mask);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);auto& m=s.memory;
 const auto& queue=s.events.queues.back();
 s.rsx.vblank_epoch_ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();   // vsync flips wait for real vblank deadlines
 const auto started=now_ms();
 assert(!rsx_worker_request(s,12).completed);   // short runs, re-posted below: every run start advances the vblank clock like the game's PUT-driven runs
 unsigned samples=0,seen=0;int highest=-1;std::string violation;
 const auto until=now_ms()+60000;
 while(now_ms()<until && m.load(0x70008044,4)!=put){
  (void)rsx_worker_request(s,12);                                                                        // pumps the coordinator (owner events, failures) and posts the next run
  int j=-1;for(int i=int(kFlips)-1;i>=0;--i)if(m.load(0x80000000+0x400+16*i,4)==0x2000u+i){j=i;break;}   // B first: latest in program order
  const std::size_t events=queue.data.size();                                                           // then the flip events
  const auto gate=m.load(0x80000010,8);                                                                  // then the gate
  std::vector<unsigned> missing;if(j>=0)for(int i=0;i<=j;++i)if(m.load(0x80000000+0x200+16*i,4)!=0x1000u+i)missing.push_back(i);   // A last: earliest
  ++samples;if(samples%20000==0)std::fprintf(stderr,"INFO flip_order t=%llu ms GET=%llx PUT=%x flips=%llu events=%zu highest B=%d\n",(unsigned long long)(now_ms()+60000-until),(unsigned long long)m.load(0x70008044,4),put,(unsigned long long)s.rsx.flips,events,j);
  if(j>=0){++seen;
   if(events<2u*(j+1))violation="B_"+std::to_string(j)+" visible but only "+std::to_string(events)+" events published (need "+std::to_string(2*(j+1))+")";
   else if((gate>>32)>=gate_word(0) && (gate>>32)<=gate_word(unsigned(j)))   // the release word of a flip <= j: its completion has not cleared the gate
    violation="B_"+std::to_string(j)+" visible while the gate still holds the release word of flip "+std::to_string((gate>>32)-gate_word(0));
   if(violation.empty() && gate==UINT64_MAX)violation="B_"+std::to_string(j)+" visible while the gate still holds its initial value";
   if(violation.empty() && !missing.empty())violation="B_"+std::to_string(j)+" visible but label A_"+std::to_string(missing[0])+" is not";
   if(j<highest)violation="label B_"+std::to_string(j)+" became the latest after B_"+std::to_string(highest);
  }
  if(j>highest)highest=j;
  if(!violation.empty())break;
  std::this_thread::sleep_for(std::chrono::microseconds(300));
 }
 // GET reaching PUT does not mean every completion is published: the last labels (and flip events) follow on the GPU timeline and
 // their publication may need the owner (event delivery). Keep pumping until the last label B is final (bounded), then drain.
 for(const auto settle=now_ms()+10000;violation.empty() && now_ms()<settle && m.load(0x80000000+0x400+16*(kFlips-1),4)!=0x2000u+kFlips-1;){
  (void)rsx_worker_request(s,12);std::this_thread::sleep_for(std::chrono::microseconds(300));}
 w.queue->drain();
 if(!violation.empty())throw std::runtime_error("publication order violated: "+violation);
 if(m.load(0x70008044,4)!=put)throw std::runtime_error("FIFO did not finish");
 for(unsigned i=0;i<kFlips;++i){
  if(m.load(0x80000000+0x200+16*i,4)!=0x1000u+i || m.load(0x80000000+0x400+16*i,4)!=0x2000u+i)throw std::runtime_error("final label of frame "+std::to_string(i)+" is wrong");}
 if(m.load(0x80000010,8)!=0)throw std::runtime_error("flip gate not cleared by the last flip");
 if(s.rsx.flips!=kFlips || queue.data.size()!=2*kFlips)throw std::runtime_error("flips "+std::to_string(s.rsx.flips)+" events "+std::to_string(queue.data.size())+" (expected "+std::to_string(kFlips)+" and "+std::to_string(2*kFlips)+")");
 if(capture){   // stride 4: flips 12, 16, 20, 24 ride on the deferred flip; their files appear once the owner retired those flights
  const auto last=std::filesystem::path(std::getenv("ANYPS3_FRAME_DIR"))/"frame-000012.ppm";
  for(const auto until=now_ms()+20000;now_ms()<until && !std::filesystem::exists(last);std::this_thread::sleep_for(std::chrono::milliseconds(2)))(void)rsx_worker_request(s,12);
  if(s.rsx.async_captures!=4 || s.rsx.captured_frames!=12)throw std::runtime_error("captures: async "+std::to_string(s.rsx.async_captures)+" total "+std::to_string(s.rsx.captured_frames)+" (expected 4 and 12)");
  if(!std::filesystem::exists(last))throw std::runtime_error("the last stride capture was not written");
 }
 life.finish();
 std::printf("PASS flip order: %u flips in %llu ms, %u samples (%u with a visible B label): A_i < flip i (gate, event) < B_i in every reverse-order sample\n",kFlips,(unsigned long long)(now_ms()-started),samples,seen);
}
// Phase F: deadlock freedom of a deferred flip event. The event (kind-2 completion label) is published through the RSX coordinator
// (owner_call), so the owner must be able to make progress at that moment; in particular a plain owner read of the label page must
// complete. Hook (test-only, patched in before the event publication, renderer thread): ask the owner to read a label of the report
// page and wait for that read to return. If the publication still held the GPU page claim of the label page, the owner's read would
// wait in the page broker for the renderer, which waits for the owner: the hook ends the process (exit 4) instead of hanging.
static void event_claim(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;const auto put=setup_flips(s);s.memory.regions.push_back({0x71000000,6,s.memory.regions[0].bytes});
 setenv("ANYPS3_RSX_WORKER","1",1);
 // Exception safety: the hook's state outlives the worker (declared before `life`); on unwinding `abandon` is set first (the hook then
 // returns at once instead of waiting for a server that stopped), `life` stops and joins the worker, and only then is the hook cleared.
 std::atomic<unsigned> asked{0},served{0};std::atomic<bool> abandon{false};
 struct ClearHook {~ClearHook(){rsx_label_event_test_hook={};}} clear_hook;
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;assert(w.enabled);auto& m=s.memory;
 struct Abandon {std::atomic<bool>& flag;~Abandon(){flag=true;}} abandon_on_exit{abandon};
 rsx_label_event_test_hook=[&]{const unsigned n=asked.fetch_add(1)+1;const auto deadline=now_ms()+10000;
  while(served.load()<n && !abandon.load()){if(now_ms()>deadline){std::fprintf(stderr,"FAIL owner read of the label page did not return while flip event %u was being published\n",n);std::fflush(stderr);std::_Exit(4);}
   std::this_thread::yield();}};
 s.rsx.vblank_epoch_ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
 assert(!rsx_worker_request(s,12).completed);
 // Completions (and their events) are published after GET reached PUT: keep serving the hook until the last label B_{n-1} is visible.
 // B_{n-1} follows the last flip in program order and is published after that flip's events (flip_order invariant), so once it is
 // visible no hook call is pending or can still start; only then is the hook cleared on the owner (acknowledged request) and the
 // worker finished, before the captured locals leave scope.
 unsigned probes=0;bool last_b=false;const auto until=now_ms()+60000;
 while(now_ms()<until && (m.load(0x70008044,4)!=put || served.load()<asked.load() || !last_b)){
  (void)rsx_worker_request(s,12);
  if(served.load()<asked.load()){(void)m.load(0x80000400,4);served.fetch_add(1);++probes;}   // label B_0: same page as the flip reports
  else last_b=m.load(0x80000000+0x400+16*(kFlips-1),4)==0x2000u+kFlips-1;
  std::this_thread::sleep_for(std::chrono::microseconds(100));
 }
 if(!last_b || served.load()<asked.load())throw std::runtime_error("last label not published within 60 s");
 w.queue->drain();w.queue->await(w.queue->post(RsxOwnerQueue::Kind::HostAccess,[]{rsx_label_event_test_hook={};}));
 if(m.load(0x70008044,4)!=put)throw std::runtime_error("FIFO did not finish");
 if(!probes)throw std::runtime_error("no flip event was published from a completion label");
 life.finish();
 std::printf("PASS event claim: %u deferred flip events, the owner read the label page during each publication\n",probes);
}
// Serial RSX (ANYPS3_RSX_WORKER=0): the FIFO runs on this thread through rsx_run; stride captures still ride on deferred flips.
// rsx_finalize (the generated main calls it after anyps3_entry) must drain the session and collect the last capture write.
static void serial_capture(){
 setenv("ANYPS3_RSX_BACKEND","gpu",1);setenv("ANYPS3_RSX_WORKER","0",1);setenv("ANYPS3_SPU_THREADS","0",1);
 PpuState s;const auto put=setup_flips(s);assert(!s.process.rsx_worker.enabled);
 s.rsx.vblank_epoch_ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
 for(const auto until=now_ms()+60000;now_ms()<until && s.memory.load(0x70008044,4)!=put;)(void)rsx_run(s,12);
 if(s.memory.load(0x70008044,4)!=put)throw std::runtime_error("FIFO did not finish");
 if(s.rsx.flips!=kFlips || s.rsx.async_captures!=4 || s.rsx.captured_frames!=12)throw std::runtime_error("serial flips "+std::to_string(s.rsx.flips)+" async captures "+std::to_string(s.rsx.async_captures)+" total "+std::to_string(s.rsx.captured_frames));
 rsx_finalize(s);
 if(s.rsx.graphics_owner)throw std::runtime_error("graphics owner not released");
 std::printf("PASS serial capture finalized\n");
}
int main(int argc,char** argv){try{
 const std::string mode=argc>1?argv[1]:"";
 if(mode=="prefix")prefix();else if(mode=="sustained")sustained();
 else if(mode=="flip_order")flip_order();else if(mode=="flip_capture")flip_order(true);else if(mode=="flip_capture_masked")flip_order(true,0x70);else if(mode=="serial_capture")serial_capture();else if(mode=="read_on")return guarded_read(true)?0:77;else if(mode=="read_off")guarded_read(false);else if(mode=="event_claim")event_claim();
 else return 3;
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


class BoundedDrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp = ARTIFACTS / 'drain_bound.cpp'
        cls.exe = cpp.with_suffix('')
        source = graphics_source().split('int main(int argc,char** argv)')[0]
        event = 'state.event(label.queue,label.event_name,label.event_flags);'
        assert source.count(event) == 1
        # Test-only hook immediately before a deferred flip event is published (event_claim mode); empty in every other mode.
        source = ('#include <functional>\ninline std::function<void()> rsx_label_event_test_hook;\n' +
                  source.replace(event, '{if(rsx_label_event_test_hook)rsx_label_event_test_hook();}' + event))
        cpp.write_text(source + MAIN)
        result = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN',
                                 '-DANYPS3_RSX_VK', str(cpp), *LIBRARIES, '-o', str(cls.exe)], capture_output=True, text=True, timeout=600)
        cpp.with_suffix('.build.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr[-12000:]

    def run_mode(self, mode, attempts=1, **extra):
        env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                   ANYPS3_SPU_WORKER='0')
        env.pop('ANYPS3_RSX_HOST_DRAIN', None)
        env.pop('ANYPS3_RSX_FLIP_EARLY', None)
        env.update(extra)
        for attempt in range(attempts):
            result = subprocess.run([str(self.exe), mode], capture_output=True, text=True, env=env, timeout=180)
            if result.returncode != 77:        # 77: the execution could not tell a late reader from a drain that never serves mid-run
                break
        else:
            self.fail(f'{attempts} attempts were inconclusive (every read was served after the run): {result.stdout}')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-6000:])
        self.assertIn('PASS ', result.stdout)
        return result.stdout

    def test_poll_serves_only_the_queued_prefix_and_respects_barriers(self):
        out = self.run_mode('prefix')
        self.assertEqual(out.count('PASS '), 2, out)

    def test_sustained_producer_cannot_starve_the_fifo(self):
        self.run_mode('sustained')

    def test_guarded_read_is_materialised_between_packets(self):
        self.run_mode('read_on', attempts=5)

    def test_flip_publication_order_with_releases_on_both_sides(self):
        directory = ARTIFACTS / 'flip-frames'
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        self.run_mode('flip_order', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='1000000')

    def test_flip_publication_order_without_early_submission(self):
        directory = ARTIFACTS / 'flip-frames-late'
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        self.run_mode('flip_order', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='1000000', ANYPS3_RSX_FLIP_EARLY='0')

    def test_owner_reads_label_page_while_flip_event_is_published(self):
        for name, extra in (('event-frames', {}), ('event-frames-late', {'ANYPS3_RSX_FLIP_EARLY': '0'})):
            directory = ARTIFACTS / name
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True)
            out = self.run_mode('event_claim', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='1000000', **extra)
            self.assertIn('PASS event claim', out)

    def test_stride_captures_ride_on_deferred_flips(self):
        # Flips 1-8 are captured synchronously, flips 12/16/20/24 (stride 4) asynchronously; the publication-order invariants of
        # flip_order hold for both. Independent expectation: flip n scans out frame n-1, cleared to clear_color(n) =
        # 0x40000000 + n*0x010203 (A8R8G8B8), so every pixel of its P6 file is (n, 2n, 3n).
        directory = ARTIFACTS / 'flip-frames-stride'
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        self.run_mode('flip_capture', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='4')
        flips = list(range(1, 9)) + [12, 16, 20, 24]
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [f'frame-{k:06d}.ppm' for k in range(1, 13)])
        for number, flip in enumerate(flips, 1):
            data = (directory / f'frame-{number:06d}.ppm').read_bytes()
            header = b'P6\n# AnyPS3 native display buffer 1\n32 16\n255\n'
            self.assertEqual(data[:len(header)], header, number)
            self.assertEqual(data[len(header):], bytes([flip, 2 * flip, 3 * flip]) * (32 * 16), f'frame {number} (flip {flip})')

    def test_stride_capture_with_occupied_staging(self):
        # Review-rsxhead-1 finding 1: a stride capture needs width*height*4 staging bytes in the flip's batch. This binary has
        # graphics batches of 2300 bytes: one 32x16 capture (2048) fits only at offset 0, because staging offsets are 256-aligned
        # and a colour-only clear without alpha (mask 0x70) is a masked clear that puts its draw payload into the same
        # batch first. The capture must still be taken (fresh batch or synchronous path), never stop with "staging exhausted".
        # FLIP_EARLY=0: the frame's commands are still in the flip's batch (flip_early would submit them before the vblank wait).
        # Same pixel oracle as above (alpha is not part of the P6 file).
        cpp = ARTIFACTS / 'drain_bound_staging.cpp'
        exe = cpp.with_suffix('')
        cpp.write_text((ARTIFACTS / 'drain_bound.cpp').read_text())
        result = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-DANYPS3_VULKAN',
                                 '-DANYPS3_RSX_VK', '-DANYPS3_RSX_GRAPHICS_STAGING_BYTES=2300', str(cpp), *LIBRARIES, '-o', str(exe)],
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-6000:])
        directory = ARTIFACTS / 'flip-frames-staging'
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        saved, self.exe = self.exe, exe
        try:
            self.run_mode('flip_capture_masked', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='4', ANYPS3_RSX_FLIP_EARLY='0')
        finally:
            self.exe = saved
        flips = list(range(1, 9)) + [12, 16, 20, 24]
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [f'frame-{k:06d}.ppm' for k in range(1, 13)])
        for number, flip in enumerate(flips, 1):
            data = (directory / f'frame-{number:06d}.ppm').read_bytes()
            self.assertTrue(data.endswith(bytes([flip, 2 * flip, 3 * flip]) * (32 * 16)), f'frame {number} (flip {flip})')

    def test_failed_last_stride_capture_stops_the_run(self):
        # Review-rsxhead-1 finding 2: the last asynchronous capture (frame 12 = flip 24) cannot be linked to its name (the file
        # already exists). No later capture collects that failure, so the release of the graphics owner must: the run fails with
        # the writer's error and the existing file is untouched.
        directory = ARTIFACTS / 'flip-frames-fail'
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        (directory / 'frame-000012.ppm').write_bytes(b'occupied')
        env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                   ANYPS3_SPU_WORKER='0', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='4')
        env.pop('ANYPS3_RSX_HOST_DRAIN', None)
        env.pop('ANYPS3_RSX_FLIP_EARLY', None)
        result = subprocess.run([str(self.exe), 'flip_capture'], capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr[-4000:])
        self.assertIn('cannot create new RSX frame capture', result.stderr)
        self.assertEqual((directory / 'frame-000012.ppm').read_bytes(), b'occupied')

    def test_serial_stride_captures_are_finalized(self):
        # Re-review rsxw4-1 #4: with ANYPS3_RSX_WORKER=0 the last asynchronous capture is completed and collected by rsx_finalize:
        # all 12 frames exist with the (n, 2n, 3n) oracle; when frame-000012.ppm already exists the run fails with the writer error.
        for name, occupied in (('flip-frames-serial', False), ('flip-frames-serial-fail', True)):
            directory = ARTIFACTS / name
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True)
            if occupied:
                (directory / 'frame-000012.ppm').write_bytes(b'occupied')
            env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json',
                       ANYPS3_SPU_WORKER='0', ANYPS3_FRAME_DIR=str(directory.resolve()), ANYPS3_FRAME_STRIDE='4')
            env.pop('ANYPS3_RSX_HOST_DRAIN', None)
            env.pop('ANYPS3_RSX_FLIP_EARLY', None)
            result = subprocess.run([str(self.exe), 'serial_capture'], capture_output=True, text=True, env=env, timeout=180)
            if occupied:
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr[-4000:])
                self.assertIn('cannot create new RSX frame capture', result.stderr)
                self.assertEqual((directory / 'frame-000012.ppm').read_bytes(), b'occupied')
                continue
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-4000:])
            self.assertIn('PASS serial capture finalized', result.stdout)
            flips = list(range(1, 9)) + [12, 16, 20, 24]
            self.assertEqual(sorted(p.name for p in directory.iterdir()), [f'frame-{k:06d}.ppm' for k in range(1, 13)])
            for number, flip in enumerate(flips, 1):
                data = (directory / f'frame-{number:06d}.ppm').read_bytes()
                self.assertTrue(data.endswith(bytes([flip, 2 * flip, 3 * flip]) * (32 * 16)), f'frame {number} (flip {flip})')

    def test_guarded_read_without_drain_waits_for_the_run(self):
        self.run_mode('read_off', ANYPS3_RSX_HOST_DRAIN='0')


if __name__ == '__main__':
    unittest.main()
