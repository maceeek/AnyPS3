"""PAR10b: deterministic StoreLoad retirement schedule and real worker stress."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest

from tests.test_par1 import source

ARTIFACTS = Path('local/ratchet/codex/par10/par10b/tests')


def buffered_retirement(code):
    """Model one x86 store-buffer delay, without changing the wait predicate.

    The release of Reader.active may remain buffered across the following
    waiter-count load. A mutex acquisition drains it. The hooks expose that
    legal StoreLoad schedule deterministically; no production hooks are added.
    """
    prefix = r'''
#include <atomic>
#include <thread>
static std::atomic<bool> par10b_departing{false},par10b_scanned{false},par10b_retired{false};
static std::atomic<std::atomic<bool>*> par10b_pending{nullptr};
static std::atomic<std::atomic<std::uintptr_t>*> par10b_page_pending{nullptr};
static thread_local bool par10b_buffer=false,par10b_flush=false;
'''
    # Instrument both the original general admission and PAR12's asymmetric
    # scalar retirement. Keep the same delayed release / mutex-drain oracle;
    # source placement or a noinline annotation must not select its coverage.
    for name in ('leave_reader', 'leave_scalar_reader'):
        declaration = 'void ' + name + '(LocalAccess& local){'
        if declaration not in code:
            assert name == 'leave_scalar_reader'  # Older reference snapshots.
            continue
        start = code.index(declaration)
        end = code.index('\n    }', start)
        body = code[start:end]
        retire = ('if(parallel->asymmetric)parallel->readers[local.slot].active.store(false,std::memory_order_release);'
                  if name == 'leave_reader' else
                  'parallel->readers[local.slot].page.store(UINTPTR_MAX,std::memory_order_release);')
        assert body.count(retire) == 1
        condition = 'parallel->asymmetric && par10b_buffer' if name == 'leave_reader' else 'par10b_buffer'
        pending = ('par10b_pending.store(&parallel->readers[local.slot].active);' if name == 'leave_reader' else
                   'par10b_page_pending.store(&parallel->readers[local.slot].page);')
        delayed = ('if(' + condition + '){\n'
                   '            par10b_buffer=false;par10b_departing=true;\n'
                   '            while(!par10b_scanned.load())std::this_thread::yield();\n'
                   '            ' + pending + 'par10b_flush=true;\n'
                   '        }else ' + retire)
        body = body.replace(retire, delayed)
        body += '\n        if(par10b_pending.load() || par10b_page_pending.load())par10b_retired=true;'
        code = code[:start] + body + code[end:]
    barrier = 'try{m.asymmetric_barrier();}catch(...){m.release_exclusive();throw;}'
    assert code.count(barrier) == 1
    code = code.replace(barrier, barrier + r'''
        if(par10b_departing.load()){
            par10b_scanned=true;while(!par10b_retired.load())std::this_thread::yield();
        }
''')
    lock = 'std::lock_guard<std::mutex> lock(parallel->admission_mutex);'
    assert code.count(lock) == 1
    code = code.replace(lock, r'''
        if(par10b_flush){if(auto* flag=par10b_pending.load())flag->store(false,std::memory_order_release);if(auto* page=par10b_page_pending.load())page->store(UINTPTR_MAX,std::memory_order_release);par10b_flush=false;}
''' + lock)
    return prefix + code


WORKERS = r'''
 setenv("ANYPS3_RSX_WORKER","1",1);setenv("ANYPS3_SPU_THREADS","1",1);
 PpuState s;auto& m=s.memory;m.map(0x10000,0x8000,6,nullptr,0);
 SpuWorkerSession life(s);auto& w=s.process.rsx_worker;
 auto f=std::static_pointer_cast<PpuState>(w.facade);
 w.queue->call(RsxOwnerQueue::Kind::MapBarrier,[&]{w.paused=true;
  f->rsx.context=1;f->rsx.driver=0x12000;f->rsx.fifo_enabled=true;f->rsx.vblank_epoch_ns=1;});
 std::atomic<bool> done{false},lane_start{false},lane_go{false},lane_done{false};
 NativeSpuWorker lane;
 lane.step=[&](unsigned){
  if(lane_done.load())return false;
  lane_start=true;while(!lane_go.load())std::this_thread::yield();
  std::array<std::uint8_t,128> bytes;bytes.fill(0x5a);
  assert(m.putllc(0x10000,bytes.data(),{},true));lane_done=true;return false;
 };
 lane.launch();while(!lane_start.load())std::this_thread::yield();
'''

BUFFERED_CASE = WORKERS + r'''
 // A watchdog publishes the delayed release without notification, as the
 // store buffer would eventually do. Old code remains asleep with NO active
 // readers and both workers at wait_reader_gate (the reported game shape).
 std::thread watchdog([&]{
  const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
  while(!done.load() && std::chrono::steady_clock::now()<deadline){
   if(par10b_retired.load())lane_go=true;
   if(par10b_pending.load() && m.parallel->admission_waiters.load()>=2){
    par10b_pending.load()->store(false,std::memory_order_release);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!done.load()){
     unsigned active=0;for(unsigned n=0;n<m.parallel->reader_limit.load();++n)active+=m.parallel->readers[n].active.load();
     fprintf(stderr,"PAR10b stranded: exclusive=%u active=%u waiters=%u\n",unsigned(m.parallel->exclusive.load()),active,m.parallel->admission_waiters.load());
     std::_Exit(23);
    }
   }
   std::this_thread::yield();
  }
  if(!done.load())std::_Exit(24);
 });
 auto tick=w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  par10b_buffer=m.parallel->asymmetric;
  assert(rsx_tick_vblank(*f,1001000001));
 });
 if(m.parallel->asymmetric){while(!par10b_departing.load())std::this_thread::yield();}
 {SpuServiceScope service(s);assert(m.parallel->exclusive.load());m.store(0x11000,4,0x11223344);}
 w.queue->await(tick);while(!lane_done.load())std::this_thread::yield();
 assert(m.load(0x10000,8)==UINT64_C(0x5a5a5a5a5a5a5a5a));
 assert(m.load(0x11000,4)==0x11223344 && m.load(0x13128,8)==60);
 done=true;watchdog.join();lane.shutdown();life.finish();
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Parallel10Deadlock(unittest.TestCase):
    def native(self, name, body, *, instrument=False):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cpp = ARTIFACTS / (name + '.cpp')
        code = source(body, full=True)
        cpp.write_text(buffered_retirement(code) if instrument else code)
        build = subprocess.run(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra',
                                '-Werror', '-pthread', str(cpp), '-o', str(cpp.with_suffix(''))],
                               capture_output=True, timeout=300)
        cpp.with_suffix('.build.log').write_bytes(build.stdout + build.stderr)
        self.assertEqual(build.returncode, 0, build.stderr.decode()[-12000:])
        run = subprocess.run([str(cpp.with_suffix(''))], capture_output=True, timeout=30,
                             env={**os.environ, 'ANYPS3_SPU_THREADS': '0',
                                  'ANYPS3_SPU_WORKER': '0', 'ANYPS3_RSX_WORKER': '0'})
        cpp.with_suffix('.run.log').write_bytes(run.stdout + run.stderr)
        self.assertEqual(run.returncode, 0, run.stderr.decode()[-12000:])

    def test_buffered_retirement_cannot_lose_raw_owner_wakeup(self):
        self.native('buffered-retirement', BUFFERED_CASE, instrument=True)

    def test_vblank_and_putllc_progress_between_service_scopes(self):
        self.native('service-stress', WORKERS + r'''
 lane_go=true;
 auto tick=w.queue->post(RsxOwnerQueue::Kind::HostAccess,[&]{
  for(unsigned n=1;n<=2000;++n)assert(rsx_tick_vblank(*f,1+UINT64_C(1001000000)*n));
 });
 for(unsigned n=0;n<10000;++n){SpuServiceScope service(s);m.store(0x11000,4,0x11223344);}
 w.queue->await(tick);while(!lane_done.load())std::this_thread::yield();
 assert(m.load(0x13128,8)==120000);
 assert(m.load(0x10000,8)==UINT64_C(0x5a5a5a5a5a5a5a5a));
 assert(m.load(0x11000,4)==0x11223344);
 done=true;lane.shutdown();life.finish();
''')
