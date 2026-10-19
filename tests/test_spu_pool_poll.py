"""Native regressions for SPU busy-poll parking on pool lanes (spu_poll_note in anyps3/spu.py).

Every scenario runs real AOT SPU code (the polling programs assembled below) on real lane threads, or on the real
pool; the expectations come from the scenario itself (known stored values, handshake order, wall/CPU clocks), never
from a second implementation of the code under test. Parking may only delay: each wake source (watched line,
mailbox, signal, lost reservation, decrementer deadline, timeout) is exercised with a 10 s timeout where only
the source can explain a prompt wake.
"""
import shutil
import unittest

from tests import spu_asm as A
from tests import spu_native

# Roles (r3 word 0); r4 word 0 = target value / iteration count.
GETLLAR_POLL, EVENT_POLL, GETLLAR_COUNT, GETLLAR_PUT_POLL, EVENT_COUNT, LR_POLL, DELAYED_WRITER = 1, 2, 3, 4, 5, 6, 7


def program():
    a = A.Asm(0x100)
    names = ('gl_poll', 'ev_poll', 'gl_count', 'gl_put_poll', 'ev_count', 'lr_poll', 'delayed_writer')
    for role, name in enumerate(names, 1):
        a.ceqi(5, 3, role)
        a.brnz(5, name)
    a.br('gl_poll')

    def setup():  # r10 = LS 0x1000, r15 = EAH 0, r11 = EA 0x10100, r14 = EA 0x10200
        a.il(10, 0x1000); a.il(15, 0); a.ila(11, 0x10100); a.ila(14, 0x10200)

    def dma(ea_reg, cmd_reg):
        a.wrch(A.MFC_LSA, 10); a.wrch(A.MFC_EAH, 15); a.wrch(A.MFC_EAL, ea_reg); a.wrch(A.MFC_CMD, cmd_reg)
        a.rdch(21, A.MFC_RD_ATOMIC_STAT)

    def finish(value_reg):
        a.wrch(A.SPU_WR_OUT_MBOX, value_reg)
        a.stop(0x102)

    # Spin on GETLLAR until word 0 of the line equals the target.
    a.label('gl_poll'); setup(); a.il(12, A.GETLLAR)
    a.label('glp'); dma(11, 12); a.lqa(20, 0x1000); a.ceq(22, 20, 4); a.brz(22, 'glp')
    finish(20)
    # rchcnt channel 0 until an event is ready, then report the event bits.
    a.label('ev_poll')
    a.label('evp'); a.rchcnt(6, A.SPU_RD_EVENT_STAT); a.brz(6, 'evp')
    a.rdch(6, A.SPU_RD_EVENT_STAT); finish(6)
    # GETLLAR on the same line every iteration, but a counter register changes: never a fixed point.
    a.label('gl_count'); setup(); a.il(12, A.GETLLAR); a.il(30, 0)
    a.label('glc'); a.ai(30, 30, 1); dma(11, 12); a.ceq(22, 30, 4); a.brz(22, 'glc')
    finish(30)
    # Fixed-point register file, but every iteration PUTLLUCs another line: externally visible activity.
    a.label('gl_put_poll'); setup(); a.il(12, A.GETLLAR); a.il(13, A.PUTLLUC)
    a.label('glu'); dma(11, 12); dma(14, 13); a.lqa(20, 0x1000); a.ceq(22, 20, 4); a.brz(22, 'glu')
    finish(20)
    # rchcnt channel 0 with a changing counter.
    a.label('ev_count'); a.il(30, 0)
    a.label('evc'); a.ai(30, 30, 1); a.rchcnt(6, A.SPU_RD_EVENT_STAT); a.ceq(22, 30, 4); a.brz(22, 'evc')
    finish(30)
    # Hold a reservation with the lost-reservation event enabled and busy-poll rchcnt channel 0.
    a.label('lr_poll'); setup(); a.il(12, A.GETLLAR); a.il(5, 0x400); a.wrch(A.SPU_WR_EVENT_MASK, 5)
    dma(11, 12)
    a.label('lrw'); a.rchcnt(6, A.SPU_RD_EVENT_STAT); a.brz(6, 'lrw')
    a.rdch(6, A.SPU_RD_EVENT_STAT); finish(6)
    # Busy-wait r6 iterations (guest compute, no channel traffic), then PUTLLUC the value 7 into the polled line.
    a.label('delayed_writer'); setup(); a.il(12, A.PUTLLUC); a.il(17, 7); a.stqa(17, 0x1000); a.ai(31, 6, 0)
    a.label('dwl'); a.ai(31, 31, -1); a.brnz(31, 'dwl')
    dma(11, 12)
    finish(17)
    return a.finish()


GLOBALS = r'''
#include <ctime>
#include <functional>
static const std::uint32_t WORDS[]={%(words)s};
// One SPU thread on one standalone pool-style lane (spu_run_thread is the lane step, as in a real pool).
struct Rig {
    PpuState& s;NativeSpu::Group* group=nullptr;NativeSpu::Thread* t=nullptr;std::unique_ptr<NativeSpuWorker> lane;
    Rig(PpuState& state,std::uint64_t role,std::uint64_t target,unsigned event_mask=0):s(state){
        NativeSpu::Group g;g.id=0x4000100+unsigned(s.spu.groups.size());g.count=1;g.state=6;g.scheduled=true;
        g.context.resize(0x40000);g.threads.emplace_back();
        auto& initial=g.threads[0];initial.entry=0x100;initial.id=0x123+unsigned(s.spu.groups.size());
        initial.arguments[0]=role<<32;initial.arguments[1]=target<<32;
        for(unsigned i=0;i<sizeof(WORDS)/sizeof(WORDS[0]);++i)for(unsigned b=0;b<4;++b)g.context[0x100+i*4+b]=WORDS[i]>>(24-8*b);
        s.spu.groups.push_back(std::move(g));group=&s.spu.groups.back();t=&group->threads[0];
        spu_prepare(s,*t,group->context.data());t->decrementer_running=false;t->event_mask=event_mask;
        lane=std::make_unique<NativeSpuWorker>();lane->context=t;lane->group=group;
        lane->step=[this](unsigned budget){return spu_run_thread(s,*group,0,budget);};
        t->in_mailbox.set_wakeup([this]{lane->wake();});for(auto& snr:t->snr)snr.set_wakeup([this]{lane->wake();});
    }
    ~Rig(){lane->shutdown();t->in_mailbox.set_wakeup({});for(auto& snr:t->snr)snr.set_wakeup({});s.spu.groups.clear();}
    void launch(){lane->launch();}
    bool stopped()const{return __atomic_load_n(&t->stopped,__ATOMIC_ACQUIRE);}
    std::uint64_t parks()const{return __atomic_load_n(&t->idle_poll.total_parks,__ATOMIC_RELAXED);}
    double cpu()const{clockid_t c;assert(!pthread_getcpuclockid(lane->thread.native_handle(),&c));timespec ts{};clock_gettime(c,&ts);return ts.tv_sec+ts.tv_nsec*1e-9;}
};
static double now_s(){return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static bool wait_for(const std::function<bool()>& predicate,double seconds){
    const double end=now_s()+seconds;
    while(!predicate()){if(now_s()>end)return false;std::this_thread::sleep_for(std::chrono::milliseconds(1));}
    return true;
}
// After its first park the lane must be asleep: no quantum, no timeout wake (timeouts are 10 s in the
// wake-source scenarios) and (almost) no CPU. Returns the park count.
static std::uint64_t expect_asleep(Rig& r){
    assert(wait_for([&]{return r.parks()>=1;},5));
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    const auto parks=r.parks();const auto completed=r.lane->completed.load();const double cpu=r.cpu();
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    assert(r.parks()==parks && r.lane->completed.load()==completed && !r.stopped());
    assert(r.cpu()-cpu<0.02);
    return parks;
}
static void long_timeouts(){spu_poll_park_ns=10000000000ull;spu_poll_park_max_ns=10000000000ull;}
'''

SCENARIOS = {}

SCENARIOS['line-wake'] = r'''
 long_timeouts();PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,1,7);r.launch();
 const auto parks=expect_asleep(r);
 assert(parks==1 && r.t->idle_poll.parked_kind==NativeSpu::Thread::PollState::Getllar);
 // The poller is asleep with a 10 s timeout: only the reservation-line notification can wake it, and the
 // retry must read the stored value (7 = the target) and finish.
 const double store=now_s();m.store(0x10100,4,7);
 assert(wait_for([&]{return r.stopped();},3));assert(now_s()-store<3.0);
 assert(r.t->exit_status==7 && r.parks()==1);
'''

SCENARIOS['unrelated-write-keeps-sleeping'] = r'''
 long_timeouts();PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,1,7);r.launch();expect_asleep(r);
 // Writes to other granules do not notify the watched line; the poller keeps sleeping (no spurious progress).
 m.store(0x10180,4,5);m.store(0x10200,4,6);
 std::this_thread::sleep_for(std::chrono::milliseconds(100));
 assert(!r.stopped() && r.parks()==1);
 m.store(0x10100,4,7);assert(wait_for([&]{return r.stopped();},3));assert(r.t->exit_status==7);
'''

SCENARIOS['mailbox-wake'] = r'''
 long_timeouts();PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,2,0,0x10);r.launch();expect_asleep(r);
 const double push=now_s();r.t->in_mailbox.push_back(0x55);
 assert(wait_for([&]{return r.stopped();},3));assert(now_s()-push<3.0);
 assert(r.t->exit_status==0x10 && r.t->in_mailbox.size()==1); // event reported; the mailbox word stays queued
'''

SCENARIOS['signal-wake'] = r'''
 long_timeouts();PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 for(unsigned which=0;which<2;++which){
  Rig r(s,2,0,which?0x100:0x200);r.launch();expect_asleep(r);
  const double push=now_s();r.t->snr[which].push(0xabcdef01,false);
  assert(wait_for([&]{return r.stopped();},3));assert(now_s()-push<3.0);
  assert(r.t->exit_status==(which?0x100u:0x200u));
 }
'''

SCENARIOS['lr-wake'] = r'''
 long_timeouts();PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,6,0);r.launch();expect_asleep(r);
 assert(r.t->idle_poll.parked_kind==NativeSpu::Thread::PollState::EventCount && r.t->reservation_address==0x10100);
 const double store=now_s();m.store(0x10100,4,1);
 assert(wait_for([&]{return r.stopped();},3));assert(now_s()-store<3.0);
 assert(r.t->exit_status==0x400);
'''

SCENARIOS['decrementer-wake'] = r'''
 long_timeouts();PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,2,0,0x20);
 // 40 ms on the 79.8 MHz time base. The park must end at the TM deadline, not at its 10 s timeout.
 r.t->decrementer=TIMEBASE_HZ/25;r.t->decrementer_tick=timebase_ticks();r.t->decrementer_running=true;
 const double start=now_s();r.launch();
 assert(wait_for([&]{return r.stopped();},3));const double elapsed=now_s()-start;
 assert(elapsed>=0.035 && elapsed<3.0);
 assert(r.t->exit_status==0x20 && r.parks()>=1);
'''

SCENARIOS['timeout-wake'] = r'''
 spu_poll_park_ns=2000000000ull;spu_poll_park_max_ns=2000000000ull; // 2 s
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,1,7);r.launch();
 assert(wait_for([&]{return r.parks()>=1;},5));
 // Change the polled bytes WITHOUT any coherence notification: nothing watched can wake the poller, so
 // only the park timeout lets the retry observe it. It must not wake early...
 __atomic_store_n(m.regions[0].bytes->data()+0x100+3,std::uint8_t(7),__ATOMIC_RELEASE);
 std::this_thread::sleep_for(std::chrono::milliseconds(150));
 assert(!r.stopped());
 // ...and must wake by itself, execute the poll for real once and finish with the new value.
 assert(wait_for([&]{return r.stopped();},10));
 assert(r.t->exit_status==7 && r.parks()==1);
'''

SCENARIOS['cpu-while-parked'] = r'''
 // Default timeouts (50 us first park, doubling up to 800 us).
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,1,7);r.launch();
 assert(wait_for([&]{return r.parks()>=1;},5));
 const double cpu0=r.cpu(),wall0=now_s();const auto parks0=r.parks();
 std::this_thread::sleep_for(std::chrono::milliseconds(400));
 const double cpu=r.cpu()-cpu0,wall=now_s()-wall0;const auto parks=r.parks()-parks0;
 std::printf("PARKED_POLLER cpu=%.4f wall=%.4f parks=%llu\n",cpu,wall,(unsigned long long)parks);
 assert(cpu<0.10*wall);            // far below one busy core
 assert(parks>=20 && parks<8000);  // timeouts wake it periodically, with backoff
 const double store=now_s();m.store(0x10100,4,7);
 assert(wait_for([&]{return r.stopped();},3));assert(now_s()-store<3.0 && r.t->exit_status==7);
'''

SCENARIOS['spin-without-parking'] = r'''
 // ANYPS3_SPU_POLL_PARK=0 equivalent: the same poller keeps spinning and still ends with the same result.
 spu_poll_park_on=false;
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 Rig r(s,1,7);r.launch();
 std::this_thread::sleep_for(std::chrono::milliseconds(150));
 const double cpu0=r.cpu(),wall0=now_s();std::this_thread::sleep_for(std::chrono::milliseconds(200));
 assert(r.parks()==0 && !r.stopped() && r.cpu()-cpu0>0.5*(now_s()-wall0));
 m.store(0x10100,4,7);assert(wait_for([&]{return r.stopped();},3));assert(r.t->exit_status==7 && r.parks()==0);
'''

SCENARIOS['no-park-without-fixed-point'] = r'''
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 {Rig r(s,3,200000);r.launch();assert(wait_for([&]{return r.stopped();},20));assert(r.t->exit_status==200000 && r.parks()==0);}
 {Rig r(s,5,300000);r.launch();assert(wait_for([&]{return r.stopped();},20));assert(r.t->exit_status==300000 && r.parks()==0);}
 // Fixed registers, but a PUTLLUC to another line every iteration: externally visible, never parked.
 {Rig r(s,4,7);r.launch();std::this_thread::sleep_for(std::chrono::milliseconds(150));
  assert(r.parks()==0 && !r.stopped());m.store(0x10100,4,7);assert(wait_for([&]{return r.stopped();},3));
  assert(r.t->exit_status==7 && r.parks()==0 && m.load(0x10204,4)==0);}
'''

SCENARIOS['pool-only'] = r'''
 // Serial spu_run (the coordinator-thread scheduler) must never park: spin until the step budget ends.
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);
 NativeSpu::Group g;g.id=0x4000100;g.count=1;g.state=6;g.scheduled=true;g.context.resize(0x40000);g.threads.emplace_back();
 auto& initial=g.threads[0];initial.entry=0x100;initial.id=0x123;initial.arguments[0]=UINT64_C(1)<<32;initial.arguments[1]=UINT64_C(7)<<32;
 for(unsigned i=0;i<sizeof(WORDS)/sizeof(WORDS[0]);++i)for(unsigned b=0;b<4;++b)g.context[0x100+i*4+b]=WORDS[i]>>(24-8*b);
 s.spu.groups.push_back(std::move(g));auto& group=s.spu.groups.back();auto& t=group.threads[0];
 spu_prepare(s,t,group.context.data());t.decrementer_running=false;
 assert(!t.idle_poll.enabled);
 for(unsigned n=0;n<64;++n)assert(spu_run(s,4096));
 assert(t.idle_poll.total_parks==0 && !t.stopped && t.steps>=64*4096);
 m.store(0x10100,4,7);for(unsigned n=0;n<64 && !t.stopped;++n)spu_run(s,4096);
 assert(t.stopped && t.exit_status==7);
'''

SCENARIOS['spin-before-park'] = r'''
 // A dependency that resolves while the fixed point is still inside its spin window is never parked, so it
 // pays no sleep/wake latency; only a longer wait parks, and not before the window has elapsed.
 spu_poll_spin_ns=400000000ull;long_timeouts();
 PpuState s;auto& m=s.memory;m.map(0x10000,0x2000,6,nullptr,0);m.enable_concurrency();
 {Rig r(s,1,7);r.launch();std::this_thread::sleep_for(std::chrono::milliseconds(150));
  assert(r.parks()==0 && !r.stopped());
  m.store(0x10100,4,7);assert(wait_for([&]{return r.stopped();},3));assert(r.t->exit_status==7 && r.parks()==0);}
 m.store(0x10100,4,0); // the first poller consumed 7; arm a fresh wait
 {Rig r(s,1,7);const double start=now_s();r.launch();
  assert(wait_for([&]{return r.parks()>=1;},5));assert(now_s()-start>=0.38);
  m.store(0x10100,4,7);assert(wait_for([&]{return r.stopped();},3));assert(r.t->exit_status==7);}
'''

SCENARIOS['pool-e2e'] = r'''
 // The real pool: ANYPS3_SPU_THREADS=1 lanes created by spu_pool_refresh, one group of two SPU threads.
 // Thread 0 polls the line by GETLLAR until it holds 7; thread 1 computes ~tens of ms and then PUTLLUCs 7.
 setenv("ANYPS3_SPU_THREADS","1",1);setenv("ANYPS3_SPU_WORKER","0",1);
 PpuState s;auto& mem=s.memory;mem.map(0x10000,8192,6,nullptr,0);
 NativeSpu::Group g;g.id=0x4000100;g.state=1;g.count=2;g.priority=100;g.context.resize(0x40000*g.count);
 const std::uint64_t roles[2]={1,7};
 for(unsigned i=0;i<2;++i){
  NativeSpu::Thread t;t.id=(i<<24)|(g.id&0xffffff);t.number=i;t.entry=0x100;
  t.arguments[0]=roles[i]<<32;t.arguments[1]=UINT64_C(7)<<32;t.arguments[3]=UINT64_C(25000000)<<32;
  auto ls=std::make_shared<std::vector<std::uint8_t>>(0x40000,0);
  for(unsigned w=0;w<sizeof(WORDS)/sizeof(WORDS[0]);++w)for(unsigned b=0;b<4;++b)(*ls)[0x100+4*w+b]=WORDS[w]>>(24-8*b);
  t.initial_ls=ls;g.threads.push_back(std::move(t));
 }
 s.spu.groups.push_back(std::move(g));
 SpuWorkerSession session(s);auto& worker=s.process.spu_worker;assert(worker.pool_mode);
 auto call=[&](unsigned op,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0){
  SpuServiceScope service(s);s.r[11]=op;s.r[3]=a;s.r[4]=b;s.r[5]=c;
  if(op==190 || op==174 || op==175 || op==177){assert(syscall_spu_ext(s));return std::uint32_t(s.r[3]);}
  return spu_syscall(s);
 };
 auto& group=s.spu.groups.back();const double begin=now_s();
 assert(call(173,group.id)==0);
 while(group.state!=1){worker.service();worker.check();assert(now_s()-begin<60);std::this_thread::sleep_for(std::chrono::milliseconds(1));}
 const double wall=now_s()-begin;
 assert(mem.load(0x10100,4)==7 && group.threads[0].exit_status==7 && group.threads[1].exit_status==7);
 double poller_cpu=-1;for(auto& lane:worker.lanes)if(lane->lane_thread_id==group.threads[0].id){clockid_t c;assert(!pthread_getcpuclockid(lane->thread.native_handle(),&c));timespec ts{};clock_gettime(c,&ts);poller_cpu=ts.tv_sec+ts.tv_nsec*1e-9;}
 const auto parks=group.threads[0].idle_poll.total_parks;
 std::printf("POOL_E2E wall=%.3f poller_cpu=%.4f parks=%llu\n",wall,poller_cpu,(unsigned long long)parks);
 assert(parks>=1 && poller_cpu>=0 && poller_cpu<0.5*wall);
'''

SCENARIOS['pool-terminate'] = r'''
 // A parked poller never blocks group control: terminate + join must complete and the session must shut down
 // (every lane joined) even though the poller waits on a line that is never written.
 setenv("ANYPS3_SPU_THREADS","1",1);setenv("ANYPS3_SPU_WORKER","0",1);
 PpuState s;auto& mem=s.memory;mem.map(0x10000,8192,6,nullptr,0);
 NativeSpu::Group g;g.id=0x4000100;g.state=1;g.count=1;g.priority=100;g.context.resize(0x40000);
 {NativeSpu::Thread t;t.id=g.id&0xffffff;t.number=0;t.entry=0x100;
  t.arguments[0]=UINT64_C(1)<<32;t.arguments[1]=UINT64_C(7)<<32;
  auto ls=std::make_shared<std::vector<std::uint8_t>>(0x40000,0);
  for(unsigned w=0;w<sizeof(WORDS)/sizeof(WORDS[0]);++w)for(unsigned b=0;b<4;++b)(*ls)[0x100+4*w+b]=WORDS[w]>>(24-8*b);
  t.initial_ls=ls;g.threads.push_back(std::move(t));}
 s.spu.groups.push_back(std::move(g));
 {
  SpuWorkerSession session(s);auto& worker=s.process.spu_worker;assert(worker.pool_mode);
  auto call=[&](unsigned op,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0){
   SpuServiceScope service(s);s.r[11]=op;s.r[3]=a;s.r[4]=b;s.r[5]=c;
   if(op==190 || op==174 || op==175 || op==177){assert(syscall_spu_ext(s));return std::uint32_t(s.r[3]);}
   return spu_syscall(s);
  };
  auto& group=s.spu.groups.back();
  assert(call(173,group.id)==0);
  assert(wait_for([&]{worker.service();worker.check();return __atomic_load_n(&group.threads[0].idle_poll.total_parks,__ATOMIC_RELAXED)>=1;},10));
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  const double begin=now_s();
  assert(call(177,group.id,123)==0);
  assert(call(178,group.id,0x10010,0x10014)==0 && mem.load(0x10010,4)==4 && mem.load(0x10014,4)==123);
  assert(now_s()-begin<3.0);
 }
 // The session destructor joined every lane; reaching here proves a parked lane does not hang shutdown.
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPoolPollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        words = program()
        cls.directory, cls.exe = spu_native.build(
            SCENARIOS, globals_cpp=GLOBALS % {'words': ','.join(hex(w) for w in words)}, words=words, name='spu-pool-poll')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def scenario(self, name):
        code, out, err = spu_native.run(self.directory, self.exe, name)
        self.assertEqual(code, 0, f'{name}: {err[-6000:]}')
        return out

    def test_getllar_poller_parks_and_wakes_on_line_write(self):
        self.scenario('line-wake')

    def test_unrelated_writes_do_not_wake_parked_poller(self):
        self.scenario('unrelated-write-keeps-sleeping')

    def test_event_poller_wakes_on_mailbox(self):
        self.scenario('mailbox-wake')

    def test_event_poller_wakes_on_signal(self):
        self.scenario('signal-wake')

    def test_event_poller_wakes_on_lost_reservation(self):
        self.scenario('lr-wake')

    def test_event_poller_wakes_at_decrementer_deadline(self):
        self.scenario('decrementer-wake')

    def test_unwatched_state_is_observed_after_park_timeout(self):
        self.scenario('timeout-wake')

    def test_parked_poller_uses_far_less_cpu_than_wall_time(self):
        self.assertIn('PARKED_POLLER', self.scenario('cpu-while-parked'))

    def test_parking_switch_keeps_results_and_spins(self):
        self.scenario('spin-without-parking')

    def test_loops_with_progress_or_side_effects_never_park(self):
        self.scenario('no-park-without-fixed-point')

    def test_serial_scheduler_never_parks(self):
        self.scenario('pool-only')

    def test_fixed_point_spins_before_parking(self):
        self.scenario('spin-before-park')

    def test_real_pool_poller_parks_while_peer_computes_and_sees_its_write(self):
        self.assertIn('POOL_E2E', self.scenario('pool-e2e'))

    def test_group_terminate_and_shutdown_with_parked_poller(self):
        self.scenario('pool-terminate')

    def test_encoding_helpers_match_production_lifter(self):
        # The hand assembler is only trusted because the production lifter decodes it to the intended operations.
        from anyps3.spu import lift
        for word, needle in ((A.rr(0x10d, 0, 21, 12), 'spu_write_channel(s,t,ls,21,'),
                              (A.rr(0x00d, 0, 27, 21), 'spu_read_channel(s,t,27)'),
                              (A.rr(0x00f, 0, 0, 6), 'spu_channel_count(s,t,0)')):
            self.assertIn(needle, lift(0x100, word))


if __name__ == '__main__':
    unittest.main()
