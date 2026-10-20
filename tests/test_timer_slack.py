"""Timer slack: Linux gives a thread 50 us of timer slack by default, so a timed host wait ends 30-50 us late.

The generated program lowers the slack of its first thread (and so of every thread it creates later) only on request:
ANYPS3_TIMER_SLACK_NS=N selects N ns; unset or 0 keeps the inherited slack (opt-in because the level measured worse with 1 ns).
The inherited initial value is whatever the test runner's process has (the kernel default is 50000 ns, but a service manager can
change it), so the probe reports it and the expectations compare against that measurement. Explicit values are literal.
"""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.program import TIMER_SLACK_CPP, compile_program
from anyps3.prx import make_image

PROBE = r'''
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <thread>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>
// Failure injection for the prctl inside the snippet: FAKE_PRCTL_FAIL=1 makes PR_SET_TIMERSLACK fail with EPERM.
static int fake_prctl(int option, unsigned long a = 0, unsigned long b = 0, unsigned long c = 0, unsigned long d = 0) {
    if (option == PR_SET_TIMERSLACK && std::getenv("FAKE_PRCTL_FAIL")) { errno = EPERM; return -1; }
    return int(syscall(SYS_prctl, option, a, b, c, d));
}
#define prctl fake_prctl
''' + TIMER_SLACK_CPP + r'''
int main() {
    const long before = prctl(PR_GET_TIMERSLACK);
    anyps3_timer_slack();
    long worker = -1;
    std::thread t([&] { worker = prctl(PR_GET_TIMERSLACK); });  // created after the call: inherits the new value
    t.join();
    std::printf("%ld %ld %ld\n", before, long(prctl(PR_GET_TIMERSLACK)), worker);
}
'''


@unittest.skipUnless(shutil.which('c++') and os.name == 'posix', 'Requires native C++ compiler on Linux')
class TimerSlack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        (root / 'probe.cpp').write_text(PROBE)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', str(root / 'probe.cpp'),
                                '-o', str(root / 'probe')], capture_output=True, text=True, timeout=300)
        assert build.returncode == 0, build.stderr[-4000:]
        cls.probe = str(root / 'probe')

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_probe(self, value=None, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ('ANYPS3_TIMER_SLACK_NS', 'FAKE_PRCTL_FAIL')}
        if value is not None:
            env['ANYPS3_TIMER_SLACK_NS'] = value
        env.update(extra)
        return subprocess.run([self.probe], capture_output=True, text=True, timeout=60, env=env)

    def initial(self):
        run = self.run_probe()
        self.assertEqual(run.returncode, 0, run.stderr)
        return int(run.stdout.split()[0])

    def test_default_keeps_the_inherited_slack(self):
        run = self.run_probe()
        self.assertEqual(run.returncode, 0, run.stderr)
        before, after, worker = map(int, run.stdout.split())
        self.assertGreater(before, 0)
        self.assertEqual((after, worker), (before, before))

    def test_one_nanosecond_is_inherited_by_later_threads(self):
        before = self.initial()
        run = self.run_probe('1')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split())), [before, 1, 1])

    def test_explicit_value(self):
        before = self.initial()
        run = self.run_probe('777')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split())), [before, 777, 777])

    def test_zero_keeps_the_inherited_slack(self):
        before = self.initial()
        run = self.run_probe('0')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split())), [before, before, before])

    def test_invalid_and_overflowing_values_are_explicit_errors(self):
        for bad in ('', 'fast', '12x', '-1', '-1 ', ' 5', '18446744073709551616', '99999999999999999999999999'):
            run = self.run_probe(bad)
            self.assertEqual(run.returncode, 2, (bad, run.stdout, run.stderr))
            self.assertIn('invalid ANYPS3_TIMER_SLACK_NS', run.stderr)
            self.assertEqual(run.stdout, '')

    def test_largest_unsigned_long_is_accepted_by_the_parser(self):
        # 2^64-1 is a valid unsigned long: the parser must not call it an overflow (the kernel decides what it does with it).
        run = self.run_probe('18446744073709551615')
        self.assertNotIn('invalid ANYPS3_TIMER_SLACK_NS', run.stderr)

    def test_prctl_failure_is_reported_not_ignored(self):
        run = self.run_probe('5', FAKE_PRCTL_FAIL='1')
        self.assertEqual(run.returncode, 2, (run.stdout, run.stderr))
        self.assertIn('prctl(PR_SET_TIMERSLACK) failed', run.stderr)
        self.assertIn('Operation not permitted', run.stderr)
        self.assertEqual(run.stdout, '')
        # Nothing requested: prctl is never called, so the injected failure is irrelevant.
        self.assertEqual(self.run_probe(None, FAKE_PRCTL_FAIL='1').returncode, 0)

    def test_program_main_sets_slack_before_anything_else(self):
        code = struct.pack('>I', 0x4e800020)  # blr
        image = make_image([(0x10000, 5, code), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True).cpp()
        main = source.split('int main(int argc, char** argv) {')[1]
        self.assertTrue(main.lstrip().startswith('anyps3_timer_slack();'), main[:200])
        self.assertEqual(source.count('static void anyps3_timer_slack()'), 1)
