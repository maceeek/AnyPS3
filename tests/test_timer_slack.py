"""Timer slack: Linux gives a thread 50 us of timer slack, so a timed host wait ends 30-50 us late.

The generated program lowers the slack of its first thread (and so of every thread it creates later) only on request:
ANYPS3_TIMER_SLACK_NS=N selects N ns; unset or 0 keeps the kernel default (opt-in because the level measured worse with 1 ns). Expected values are the kernel's
literal numbers (default 50000 ns, PR_GET_TIMERSLACK), not derived from the implementation.
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
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <thread>
#include <sys/prctl.h>
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

    def run_probe(self, value=None):
        env = {k: v for k, v in os.environ.items() if k != 'ANYPS3_TIMER_SLACK_NS'}
        if value is not None:
            env['ANYPS3_TIMER_SLACK_NS'] = value
        return subprocess.run([self.probe], capture_output=True, text=True, timeout=60, env=env)

    def test_default_keeps_the_kernel_slack(self):
        run = self.run_probe()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split())), [50000, 50000, 50000])  # kernel default of a fresh process

    def test_one_nanosecond_is_inherited_by_later_threads(self):
        run = self.run_probe('1')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split())), [50000, 1, 1])

    def test_explicit_value(self):
        run = self.run_probe('777')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split()))[1:], [777, 777])

    def test_zero_keeps_kernel_default(self):
        run = self.run_probe('0')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(list(map(int, run.stdout.split())), [50000, 50000, 50000])

    def test_invalid_value_is_an_explicit_error(self):
        for bad in ('', 'fast', '12x', '-1', '-1 ', ' 5'):
            run = self.run_probe(bad)
            self.assertEqual(run.returncode, 2, (bad, run.stdout, run.stderr))
            self.assertIn('invalid ANYPS3_TIMER_SLACK_NS', run.stderr)
            self.assertEqual(run.stdout, '')

    def test_program_main_sets_slack_before_anything_else(self):
        code = struct.pack('>I', 0x4e800020)  # blr
        image = make_image([(0x10000, 5, code), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True).cpp()
        main = source.split('int main(int argc, char** argv) {')[1]
        self.assertTrue(main.lstrip().startswith('anyps3_timer_slack();'), main[:200])
        self.assertEqual(source.count('static void anyps3_timer_slack()'), 1)
