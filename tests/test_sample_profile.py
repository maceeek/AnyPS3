"""Native sampling, role identity, exact workload result and crash-safe wire ABI."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from anyps3.sample_profile import TYPES

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'local/ratchet/tools/sample_report.py'
spec = importlib.util.spec_from_file_location('anyps3_sample_report_test', REPORT)
reporter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reporter
spec.loader.exec_module(reporter)

def raw_records(path):
    """Records including the timestamp slot addresses[7] (read_samples keeps only addresses[:depth])."""
    data = path.read_bytes()
    offset, result = reporter.HEADER.size, []
    while offset + reporter.CHUNK.size <= len(data):
        _, length, count, *_ = reporter.CHUNK.unpack_from(data, offset)
        payload = data[offset + reporter.CHUNK.size:offset + reporter.CHUNK.size + length]
        for values in reporter.RECORD.iter_unpack(payload):
            sequence, overrun, steps, pc, guest_tid, depth, flags, *addresses = values
            result.append(dict(sequence=sequence, overrun=overrun, depth=depth, stamp=addresses[7], addresses=addresses))
        offset += reporter.CHUNK.size + length + 16
    return result


HARNESS = r'''
#include <iostream>
#include <sys/mman.h>
#include <cerrno>
extern "C" [[gnu::noinline]] std::uint64_t sample_hot_known(std::uint64_t n){
    volatile std::uint64_t bound=n;asm volatile("" : "+m"(bound));
    n=bound;std::uint64_t value=0;
    for(std::uint64_t i=0;i<n;++i){value+=i;asm volatile("" : "+r"(value));}
    return value;
}
extern "C" [[gnu::noinline]] std::uint64_t sample_caller_known(std::uint64_t n){
    return sample_hot_known(n)+1;
}
static std::uint64_t clock_ns(clockid_t id){struct timespec t{};clock_gettime(id,&t);return std::uint64_t(t.tv_sec)*1000000000+t.tv_nsec;}
int main(int argc,char** argv){
 try{
    const std::string mode=argc>1?argv[1]:"hot";
    const std::uint64_t n=argc>2?std::strtoull(argv[2],nullptr,10):UINT64_C(600000000);
    if(mode=="conflict"){
        struct sigaction action{};action.sa_handler=SIG_IGN;sigemptyset(&action.sa_mask);
        sigaction(SIGRTMIN+6,&action,nullptr);
    }
    if(mode=="roles"){
        std::vector<std::thread> threads;
        for(auto role:{anyps3_sample::Role::Ppu,anyps3_sample::Role::Rsx,anyps3_sample::Role::Spu}){
            threads.emplace_back([role,n]{std::uint32_t pc=0x12345000,tid=77;std::uint64_t steps=987654321;
                anyps3_sample::Thread sampling(role,&pc,&tid,&steps);
                const auto value=sample_caller_known(n);if(value!=n*(n-1)/2+1)std::abort();});
        }
        for(auto& thread:threads)thread.join();
        return 0;
    }
    std::uint32_t pc=0x12345000,tid=77;std::uint64_t steps=987654321;
    if(mode=="blocked"){
        // Delivery of the sampling signal blocked for 50 ms: the kernel coalesces the missed ticks into one notification
        // with an overrun count, so a sample's position in time cannot be derived from its sequence number.
        anyps3_sample::Thread sampling(anyps3_sample::Role::Ppu,&pc,&tid,&steps);
        sigset_t set;sigemptyset(&set);sigaddset(&set,SIGRTMIN+6);
        pthread_sigmask(SIG_BLOCK,&set,nullptr);
        const auto t_block=clock_ns(CLOCK_MONOTONIC);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        const auto t_unblock=clock_ns(CLOCK_MONOTONIC);
        pthread_sigmask(SIG_UNBLOCK,&set,nullptr);
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        std::printf("{\"block\":%llu,\"unblock\":%llu,\"end\":%llu}\n",(unsigned long long)t_block,(unsigned long long)t_unblock,
            (unsigned long long)clock_ns(CLOCK_MONOTONIC));
        return 0;
    }
    if(mode=="guest" || mode=="guest-fault" || mode=="errno"){
        // Fake guest address space: a 4 GiB PROT_NONE reservation with one readable page at guest 0x20000000.
        // PPC64 stack: 0(r1) = back chain, 16(parent) = return address saved by the callee.
        const std::uint64_t reserve=UINT64_C(1)<<32;
        auto* base=static_cast<std::uint8_t*>(mmap(nullptr,reserve,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE,-1,0));
        if(base==MAP_FAILED || mprotect(base+0x20000000,0x1000,PROT_READ|PROT_WRITE))return 4;
        auto put=[&](std::uint64_t ea,std::uint64_t v){for(unsigned i=0;i<8;++i)base[ea+i]=std::uint8_t(v>>(56-8*i));};
        std::uint64_t lr=0x666070,r1=0x20000100;
        if(mode=="guest"){put(0x20000100,0x20000200);put(0x20000210,0x696d54);put(0x20000200,0x20000300);put(0x20000310,0x123458);put(0x20000300,0);}
        else{r1=0x20000f00;put(0x20000f00,0x20001000);}  // parent frame (guest-fault, errno): lies in the unreadable next page
        const std::uint8_t* base_pointer=base;
        anyps3_sample::Thread sampling(anyps3_sample::Role::Ppu,&pc,&tid,&steps,&lr,&r1,&base_pointer);
        // The failed process_vm_readv of the handler sets EFAULT; the interrupted code must still see 777. A volatile access
        // keeps the compiler from proving errno unchanged across the (visible) workload function.
        volatile int* error_slot=&errno;
        *error_slot=777;
        const auto result=sample_caller_known(n);
        const int seen=*error_slot;
        if(result!=n*(n-1)/2+1)return 3;
        return mode=="errno" && seen!=777?5:0;
    }
    anyps3_sample::Thread sampling(anyps3_sample::Role::Ppu,&pc,&tid,&steps);
    if(mode=="wait"){
        std::puts("READY");std::fflush(stdout);
        std::uint64_t result=0;for(;;){result^=sample_caller_known(n);asm volatile("" : "+r"(result));}
    }
    if(mode=="sleep"){std::this_thread::sleep_for(std::chrono::milliseconds(200));return 0;}
    const auto wall=clock_ns(CLOCK_MONOTONIC),process=clock_ns(CLOCK_PROCESS_CPUTIME_ID),thread=clock_ns(CLOCK_THREAD_CPUTIME_ID);
    const auto result=sample_caller_known(n);
    const auto thread_end=clock_ns(CLOCK_THREAD_CPUTIME_ID),process_end=clock_ns(CLOCK_PROCESS_CPUTIME_ID),wall_end=clock_ns(CLOCK_MONOTONIC);
    std::printf("{\"n\":%llu,\"result\":%llu,\"thread_ns\":%llu,\"process_ns\":%llu,\"wall_ns\":%llu}\n",
        (unsigned long long)n,(unsigned long long)result,(unsigned long long)(thread_end-thread),
        (unsigned long long)(process_end-process),(unsigned long long)(wall_end-wall));
    return result==n*(n-1)/2+1?0:3;
 }catch(const std::exception& e){std::fprintf(stderr,"%s\n",e.what());return 2;}
}
'''


@unittest.skipUnless(shutil.which('g++') and shutil.which('nm') and shutil.which('addr2line'), 'requires native ELF toolchain')
class SampleProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='anyps3-prof1-')
        cls.root = Path(cls.temp.name)
        cls.program = cls.root / 'program'
        source = cls.root / 'sample.cpp'
        source.write_text(TYPES + HARNESS)
        cls.command = ['g++', '-std=c++17', '-O2', '-g', '-Wall', '-Wextra', '-Werror', '-pthread',
                       '-fno-omit-frame-pointer', '-mno-omit-leaf-frame-pointer', '-fno-optimize-sibling-calls',
                       str(source), '-o', str(cls.program)]
        run = subprocess.run(cls.command, capture_output=True, text=True, timeout=90)
        if run.returncode:
            raise AssertionError(run.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def environment(self, directory=None, **settings):
        env = {k: v for k, v in os.environ.items() if not k.startswith('ANYPS3_SAMPLE_PROFILE')}
        env['SDL_AUDIODRIVER'] = 'dummy'
        env.pop('ANYPS3_AUDIO_AUDIBLE', None)
        if directory is not None:
            env['ANYPS3_SAMPLE_PROFILE'] = str(directory)
        env.update(settings)
        return env

    def run_profile(self, mode='hot', **settings):
        directory = self.root / f'{self._testMethodName}-{time.monotonic_ns()}'
        run = subprocess.run([str(self.program), mode], env=self.environment(directory, **settings),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return directory, run

    def test_native_hot_rip_guest_and_inclusive_caller(self):
        directory, run = self.run_profile(ANYPS3_SAMPLE_PROFILE_FRAMES='1')
        observed = json.loads(run.stdout)
        self.assertEqual(observed['result'], 179999999700000001)
        result = reporter.report(directory, self.program)
        self.assertGreater(result['samples'], 20)
        flat = next(row for row in result['flat'] if 'sample_hot_known' in row['name'])
        self.assertGreater(flat['percent'], 70)
        self.assertTrue(any('sample_caller_known' in row['name'] for row in result['inclusive']))
        guest = result['guest_pages']
        self.assertEqual(len(guest), 1)
        self.assertEqual(guest[0]['name'], 'thread=77 game/other:page_12345')
        file = reporter.read_samples(next(directory.glob('thread-*.samples')))
        self.assertTrue(all(r['guest_steps'] == 987654321 for r in file['records']))
        self.assertEqual(file['period_ns'], 200000)
        self.assertEqual(file['dropped'], 0)
        self.assertEqual(file['tail_bytes'], 0)

    def test_off_has_identical_result_and_no_files_or_extra_threads(self):
        directory = self.root / 'disabled-never-created'
        run = subprocess.run([str(self.program), 'hot'], env=self.environment(),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['result'], 179999999700000001)
        self.assertFalse(directory.exists())
        process = subprocess.Popen([str(self.program), 'wait', '10000000'],
                                   env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'READY')
            self.assertEqual(len(list(Path(f'/proc/{process.pid}/task').iterdir())), 1)
        finally:
            process.kill()
            process.communicate(timeout=5)
        # Even malformed diagnostic settings are not inspected when disabled.
        run = subprocess.run([str(self.program), 'sleep'], env=self.environment(ANYPS3_SAMPLE_PROFILE_US='invalid'),
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_thread_cpu_timer_roles_filter_and_sleep(self):
        directory, _ = self.run_profile('roles')
        result = reporter.report(directory, self.program)
        self.assertEqual(set(result['roles']), {'ppu', 'rsx', 'spu'})
        tids = []
        for stats in result['roles'].values():
            self.assertGreater(stats['samples'], 20)
            tids.extend(stats['threads'])
        self.assertEqual(len(set(tids)), 3)
        directory, _ = self.run_profile('roles', ANYPS3_SAMPLE_PROFILE_ROLES='rsx,spu')
        self.assertEqual(set(reporter.report(directory, self.program)['roles']), {'rsx', 'spu'})
        directory, _ = self.run_profile('sleep')
        self.assertEqual(reporter.report(directory, self.program)['samples'], 0)

    def test_wall_clock_mode_samples_a_sleeping_thread(self):
        # Default CPU-time sampling sees nothing while the thread sleeps (test above); wall mode samples where it sleeps.
        directory, _ = self.run_profile('sleep', ANYPS3_SAMPLE_PROFILE_WALL='ppu')
        file = reporter.read_samples(next(directory.glob('thread-*.samples')))
        # 200 ms of sleep at a 200 us period is about 1000 samples; allow generous scheduling slack.
        self.assertGreater(len(file['records']), 200)
        self.assertTrue(all(r['guest_steps'] == 987654321 and r['flags'] & 1 for r in file['records']))
        # Wall mode stamps every record with its CLOCK_MONOTONIC time (addresses[7], depth capped at 7).
        stamps = [raw['stamp'] for raw in raw_records(next(directory.glob('thread-*.samples')))]
        now_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        self.assertEqual(stamps, sorted(stamps))
        self.assertGreater(stamps[0], now_ns - 60 * 10**9)
        self.assertLess(stamps[-1], now_ns)
        self.assertGreater(stamps[-1] - stamps[0], 150 * 10**6)  # about 200 ms of sleep
        # Roles not listed keep thread CPU time.
        directory, _ = self.run_profile('sleep', ANYPS3_SAMPLE_PROFILE_WALL='spu')
        self.assertEqual(reporter.report(directory, self.program)['samples'], 0)
        run = subprocess.run([str(self.program), 'sleep'], env=self.environment(self.root / 'bad-wall', ANYPS3_SAMPLE_PROFILE_WALL='gpu'),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 2)
        self.assertIn('invalid wall role list', run.stderr)

    def test_guest_stack_records_lr_and_back_chain_and_survives_unreadable_frames(self):
        directory, _ = self.run_profile('guest', ANYPS3_SAMPLE_PROFILE_GUEST_STACK='1')
        file = reporter.read_samples(next(directory.glob('thread-*.samples')))
        self.assertGreater(len(file['records']), 20)
        for record in file['records']:
            self.assertEqual(record['addresses'][1:], [0x666070, 0x696d54, 0x123458])  # live LR, then two saved return addresses
            self.assertEqual(record['flags'], 3)
        # The parent frame lives in an unreadable page: the chain ends there (EFAULT is not a crash), LR is kept.
        directory, _ = self.run_profile('guest-fault', ANYPS3_SAMPLE_PROFILE_GUEST_STACK='1')
        file = reporter.read_samples(next(directory.glob('thread-*.samples')))
        self.assertGreater(len(file['records']), 20)
        self.assertTrue(all(r['addresses'][1:] == [0x666070] for r in file['records']))
        # Off by default: only the interrupted host RIP.
        directory, _ = self.run_profile('guest')
        file = reporter.read_samples(next(directory.glob('thread-*.samples')))
        self.assertTrue(all(len(r['addresses']) == 1 and r['flags'] == 1 for r in file['records']))

    def test_stamps_follow_real_time_when_delivery_is_delayed(self):
        directory, run = self.run_profile('blocked', ANYPS3_SAMPLE_PROFILE_WALL='ppu')
        marks = json.loads(run.stdout)
        records = raw_records(next(directory.glob('thread-*.samples')))
        self.assertTrue(records)
        stamps = [r['stamp'] for r in records]
        self.assertEqual(stamps, sorted(stamps))
        # While delivery was blocked nothing was recorded; the first record afterwards carries the coalesced ticks as an
        # overrun and its stamp is the delivery time (the sequence number alone would place it near the start).
        self.assertTrue(all(not (marks['block'] + 2 * 10**6 < t < marks['unblock'] - 2 * 10**6) for t in stamps))
        first_after = next(r for r in records if r['stamp'] >= marks['unblock'])
        self.assertLess(first_after['stamp'] - marks['unblock'], 5 * 10**6)
        self.assertGreater(first_after['overrun'], 100)  # 50 ms / 200 us = 250 ticks coalesced
        self.assertLessEqual(stamps[-1], marks['end'])
        # Stamps are off in CPU-time mode, which keeps the full 8-frame depth.
        directory, _ = self.run_profile()
        self.assertTrue(all(r['stamp'] == 0 for r in raw_records(next(directory.glob('thread-*.samples')))))

    def test_errno_of_the_interrupted_thread_survives_failing_guest_stack_reads(self):
        for mode in ('errno', 'guest'):  # unreadable parent frame (EFAULT inside the handler) / readable chain
            directory, run = self.run_profile(mode, ANYPS3_SAMPLE_PROFILE_GUEST_STACK='1')
            self.assertGreater(len(reporter.read_samples(next(directory.glob('thread-*.samples')))['records']), 20)

    def test_ring_drop_accounting_and_rip_only_default(self):
        directory, _ = self.run_profile(ANYPS3_SAMPLE_PROFILE_CAPACITY='16', ANYPS3_SAMPLE_PROFILE_FLUSH_MS='5000')
        file = reporter.read_samples(next(directory.glob('thread-*.samples')))
        self.assertGreater(file['dropped'], 0)
        self.assertEqual(file['attempted'], len(file['records']) + file['dropped'])
        self.assertTrue(all(len(r['addresses']) == 1 for r in file['records']))

    def test_sigkill_and_every_torn_chunk_boundary(self):
        directory = self.root / 'kill'
        process = subprocess.Popen([str(self.program), 'wait', '10000000'],
                                   env=self.environment(directory), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'READY')
            deadline = time.monotonic() + 10
            preserved = None
            while time.monotonic() < deadline:
                paths = list(directory.glob('thread-*.samples'))
                if paths:
                    candidate = reporter.read_samples(paths[0])
                    if len(candidate['records']) >= 20:
                        preserved = candidate
                        break
                time.sleep(.02)
            self.assertIsNotNone(preserved)
            process.kill()
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            after = reporter.report(directory, self.program)
            self.assertGreaterEqual(after['samples'], len(preserved['records']))
            path = next(directory.glob('thread-*.samples'))
            data = path.read_bytes()
            # Independently check the advertised ABI before exercising truncation.
            self.assertEqual(data[:8], b'APS3SP01')
            self.assertEqual(reporter.HEADER.size, 56)
            self.assertEqual(reporter.RECORD.size, 104)
            prefix_end = 56
            chunk_start = 56
            while prefix_end < len(data):
                count = int.from_bytes(data[prefix_end + 12:prefix_end + 16], 'little')
                size = 40 + count * 104 + 16
                if prefix_end + size > len(data):
                    break
                chunk_start = prefix_end
                prefix_end += size
                if count:
                    break
            fixture = self.root / 'torn.samples'
            chunk = data[chunk_start:prefix_end]
            prefix = data[:prefix_end]
            expected = len(reporter.read_samples(self._prefix_fixture(prefix))['records'])
            cuts = set(range(40)) | {40, 41, 143, 144}
            cuts |= set(range(len(chunk) - 17, len(chunk)))
            for cut in sorted(c for c in cuts if 0 <= c < len(chunk)):
                fixture.write_bytes(prefix + chunk[:cut])
                parsed = reporter.read_samples(fixture)
                self.assertEqual(len(parsed['records']), expected)
                self.assertEqual(parsed['tail_bytes'], cut)
            self.assertGreater(expected, 0)
            corrupt = bytearray(prefix)
            corrupt[chunk_start + 40] ^= 1
            fixture.write_bytes(corrupt)
            with self.assertRaisesRegex(ValueError, 'corrupt committed'):
                reporter.read_samples(fixture)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def _prefix_fixture(self, prefix):
        path = self.root / 'prefix.samples'
        path.write_bytes(prefix)
        return path

    def test_invalid_configuration_and_program_identity(self):
        for settings in ({'ANYPS3_SAMPLE_PROFILE_US': '0'}, {'ANYPS3_SAMPLE_PROFILE_ROLES': 'gpu'},
                         {'ANYPS3_SAMPLE_PROFILE_FRAMES': '2'}):
            directory = self.root / f'bad-{time.monotonic_ns()}'
            run = subprocess.run([str(self.program), 'sleep'], env=self.environment(directory, **settings),
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 2)
            self.assertIn('SAMPLE_PROFILE:', run.stderr)
        directory, _ = self.run_profile('sleep')
        copy = self.root / 'different-program'
        shutil.copy2(self.program, copy)
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            reporter.report(directory, copy)
        run = subprocess.run([str(self.program), 'sleep'], env=self.environment(directory),
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 2)
        conflict = subprocess.run([str(self.program), 'conflict'],
                                  env=self.environment(self.root / 'conflict'), capture_output=True, text=True, timeout=5)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('already in use', conflict.stderr)

    def test_checksum_known_vector_and_incomplete_header(self):
        self.assertEqual(reporter.fnv64(b'hello'), 0xa430d84680aabd0b)
        path = self.root / 'short.samples'
        path.write_bytes(b'APS3SP')
        parsed = reporter.read_samples(path)
        self.assertTrue(parsed['incomplete_header'])
        self.assertEqual(parsed['records'], [])


if __name__ == '__main__':
    unittest.main()
