"""Native observable state checks for the bounded startup services."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program


@unittest.skipUnless(shutil.which('c++'), 'Native service tests require c++.')
class ServicesTests(unittest.TestCase):
    def test_tls_mutex_allocation_and_syscall_contracts(self):
        data = program(BLR)
        source = compile_program(ElfImage.parse(data), process_entry=True).cpp()
        # Test the exact emitted runtime. Only replace its CLI main with assertions.
        source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        source += r'''
#include <cassert>
template <class F> void must_fault(F fn) {
    bool fault = false; try { fn(); } catch (const std::runtime_error&) { fault = true; }
    assert(fault);
}
int main() {
    PpuState state; auto& mem = state.memory; auto& r = state.r;
    mem.map(0x20000, 0x1000, 6, nullptr, 0);
    mem.store(0x20ffc,4,0x616263ff);mem.store(0x20ff8,4,0x61626301);
    assert(guest_strncmp(state,0,0,0)==0);
    assert(guest_strncmp(state,0x20ffc,0x20ff8,3)==0);
    assert(guest_strncmp(state,0x20ffc,0x20ff8,4)==254);
    assert(std::int64_t(guest_strncmp(state,0x20ff8,0x20ffc,4))==-254);
    mem.store(0x20ffd,1,0);mem.store(0x20ff9,1,0);
    assert(guest_strncmp(state,0x20ffc,0x20ff8,UINT32_MAX)==0);
    mem.store(0x20000, 4, 0x01234567);
    r[3] = 1; r[4] = 0x20000; r[5] = 4; r[6] = 12;
    initialize_tls(state);
    const auto tls = r[13] - 0x7000;
    assert(mem.load(tls, 4) == 0x01234567);
    assert(mem.load(tls + 4, 8) == 0);
    assert(mem.load(tls - 0x30, 8) == 0);
    const auto allocated = mem.allocated;
    initialize_tls(state); assert(mem.allocated == allocated);
    r[13] = 0; r[3] = 2;
    must_fault([&]{ initialize_tls(state); });
    r[3] = 1; r[5] = 13;
    must_fault([&]{ initialize_tls(state); });

    mem.store(0x20100, 4, 2); mem.store(0x20104, 4, 16);
    r[3] = 0x20200; r[4] = 0x20100; lwmutex_create(state);
    assert(r[3] == 0 && mem.load(0x20200, 8) == UINT64_C(0xffffffff00000000));
    assert(mem.load(0x20208, 4) == 18 && mem.load(0x20210, 4) == 0x95000000);
    r[3] = 0x20200; lwmutex_lock(state); assert(mem.load(0x20200, 4) == 1);
    r[3] = 0x20200; lwmutex_lock(state); assert(mem.load(0x2020c, 4) == 1);
    r[3] = 0x20200; lwmutex_unlock(state); assert(mem.load(0x20200, 4) == 1);
    r[3] = 0x20200; lwmutex_unlock(state); assert(mem.load(0x20200, 4) == UINT32_MAX);
    // Firmware 3.01 liblv2 sys_lwmutex_unlock: owner != caller (here: free) -> CELL_EPERM, mutex unchanged.
    r[3] = 0x20200; lwmutex_unlock(state); assert(r[3] == 0x80010009 && mem.load(0x20200, 4) == UINT32_MAX);
    r[3] = 0x20200; r[4] = 0x20100; must_fault([&]{ lwmutex_create(state); });
    mem.store(0x20104, 4, 32);
    r[3] = 0x20300; r[4] = 0x20100; lwmutex_create(state);
    r[3] = 0x20300; lwmutex_lock(state);
    r[3] = 0x20300; lwmutex_lock(state); assert(r[3]==0x80010008);
    r[3] = 0x20300; assert(lwmutex_destroy(state) == 0x8001000a); // Owned by caller.
    r[3] = 0x20300; lwmutex_unlock(state);
    mem.store(0x20304, 4, 1); r[3] = 0x20300; assert(lwmutex_destroy(state) == 0x8001000a); // Waiter.
    mem.store(0x20304, 4, 0); r[3] = 0x20300; assert(lwmutex_destroy(state) == 0);
    assert(mem.load(0x20300, 4) == 0xfffffffe);
    r[3] = 0x20300; assert(lwmutex_destroy(state) == 0x80010005);
    r[3] = 0x20300; must_fault([&]{ lwmutex_lock(state); });

    r[11] = 352; r[3] = 0x20400; syscall(state);
    assert(r[3] == 0 && mem.load(0x20400, 4) == 256 * 1024 * 1024);
    assert(mem.load(0x20404, 4) == 256 * 1024 * 1024 - mem.allocated);
    r[11] = 147; syscall(state); assert(r[3] == TIMEBASE_HZ);
    r[11] = 1; syscall(state); assert(r[3] == 1);
    state.sdk_version=0x270001; r[11]=25; r[3]=1; r[4]=0x20438; syscall(state);
    assert(r[3]==0 && mem.load(0x20438,4)==0x270001);
    r[3]=99; must_fault([&]{ syscall(state); });
    // sys_timer_usleep suspends only the caller (timed wait); the retried call returns 0.
    r[11]=141; r[3]=1000; bool slept=false;
    try { syscall(state); } catch (const PpuBlocked&) { slept=true; }
    assert(slept && state.sleep_microseconds==1000 && state.wait_kind==12);
    state.sync_ext.waits.at(state.thread_id).deadline=1; sync_ext_poll(state);
    assert(!state.wait_kind);
    r[11]=141; r[3]=1000; syscall(state);
    assert(r[3]==0 && state.sleep_microseconds==1000 && state.sync_ext.waits.empty());
    r[3]=1000001; must_fault([&]{ syscall(state); });
    r[11] = 144; r[3] = 0x20410; r[4] = 0x20414; syscall(state);
    assert(r[3] == 0 && mem.load(0x20410,4) == 0 && mem.load(0x20414,4) == 0);
    const auto before = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    r[11] = 145; r[3] = 0x20420; r[4] = 0x20428; syscall(state);
    const auto after = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    assert(r[3] == 0 && mem.load(0x20420,8) >= static_cast<std::uint64_t>(before));
    assert(mem.load(0x20420,8) <= static_cast<std::uint64_t>(after) && mem.load(0x20428,8) < 1000000000);
    mem.store(0x20430,4,0x5454590a);
    r[11] = 403; r[3] = 0; r[4] = 0x20430; r[5] = 4; r[6] = 0x20434; syscall(state);
    assert(r[3] == 0 && mem.load(0x20434,4) == 4);
    r[3] = 16; must_fault([&]{ syscall(state); });
    r[11] = 999; must_fault([&]{ syscall(state); });
    must_fault([&]{ mem.map(0x20008, 8, 6, nullptr, 0); });
    must_fault([&]{ mem.map(UINT32_MAX, 8, 6, nullptr, 0); });
    must_fault([&]{ mem.allocate(1, 3); });
    const auto a = mem.allocate(8, 64);
    assert(a % 64 == 0 && mem.load(a, 8) == 0);
    assert(mem.load(tls, 4) == 0x01234567);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cpp, binary = root/'services.cpp', root/'services'
            cpp.write_text(source)
            build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(cpp),'-o',str(binary)],
                                   capture_output=True,text=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            run = subprocess.run([str(binary)],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(run.stdout,'TTY\n')


@unittest.skipUnless(shutil.which('c++'), 'Native service tests require c++.')
class SleepBudgetTests(unittest.TestCase):
    def test_budget_parse_default_and_rejects_invalid(self):
        from tests import test_runtime_objects
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;assert(sleep_budget_microseconds(nullptr)==5000000);
 assert(sleep_budget_microseconds("600000000")==600000000 && sleep_budget_microseconds("18446744073709551615")==UINT64_MAX);
 for(const char* v:{"","0","-1","12a","18446744073709551616"})fault([&]{sleep_budget_microseconds(v);});
''')


@unittest.skipUnless(shutil.which('c++'), 'Native service tests require c++.')
class ThreadPriorityLifetimeTests(unittest.TestCase):
    def test_exited_detached_thread_is_gone_and_joinable_zombie_remains(self):
        # ACIT 0x547d58 polls sys_ppu_thread_get_priority until ESRCH after "Check Thread" exits.
        from tests import test_runtime_objects
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));
 PpuContext detached;detached.thread_id=23;detached.priority=1001;s.ppu_threads.push_back(detached);
 PpuContext zombie;zombie.thread_id=24;zombie.priority=700;zombie.joinable=true;s.ppu_threads.push_back(zombie);
 assert(call(48,23,0x10100)==0 && mem.load(0x10100,4)==1001);
 s.ppu_threads[1].returned=true;s.ppu_threads[2].returned=true;mem.store(0x10100,4,0x77);
 assert(call(48,23,0x10100)==0x80010005 && mem.load(0x10100,4)==0x77);
 assert(call(47,23,5)==0x80010005);
 assert(call(48,24,0x10100)==0 && mem.load(0x10100,4)==700);
 assert(call(48,1,0x10100)==0);
''')
