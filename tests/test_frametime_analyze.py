"""bench/frametime/analyze.py on a synthetic ring with a known timeline (independent expectation: built by hand here)."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bench' / 'frametime'))
import ftring as ft  # noqa: E402

VBLANK_NS = 16_683_333


def synthetic():
    """12 flips on vblank ticks 1..; flips 4 and 9 each miss one vblank (period 2). tid 100 = worker, 200 = main."""
    events = []
    t0 = 1_000_000_000
    ticks = [1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 14]
    for i, tick in enumerate(ticks):
        deadline = t0 + tick * VBLANK_NS
        # worker: busy 12 ms of draws in the 16.7 ms before the flip request, 3 ms before the deadline for a normal frame
        req = deadline - 3_000_000
        events.append(ft.Event(req - 12_000_000, ft.FIFO_RUN, 100, 300, 12_000_000, i, 0))
        events.append(ft.Event(req, ft.FLIP_REQ, 100, i % 3, 2, tick - 1, 0))
        events.append(ft.Event(req, ft.FLIP_VSYNC, 100, deadline, 3_000_000, tick, 0))
        events.append(ft.Event(deadline + 200_000, ft.FLIP_ASYNC, 100, 1, 0, i % 3, 0))
        events.append(ft.Event(deadline + 1_500_000, ft.LABEL_PUB, 100, 0xC000, i % 3, 1, i))
        events.append(ft.Event(deadline, ft.VBLANK, 100, tick, 40_000, 0, 0))
    # main thread: usleep(50) loop of guest thread 1 at lr 0x5c5734, woken by switches
    for n in range(100):
        t = t0 + 20_000_000 + n * 100_000
        events.append(ft.Event(t, ft.BLOCK, 200, (0x5c5734 << 32) | 0x5c5730, 50, 1 | (12 << 32), 0))
        events.append(ft.Event(t + 90_000, ft.SWITCH, 200, 1, 1, 0x5c5738, 0))
    events.append(ft.Event(t0 + 500_000, ft.POST_FIFO, 200, 0x100, 1024, 0, 0))
    return events


class AnalyzeTests(unittest.TestCase):
    def test_periods_and_main_waits(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'ring.bin'
            ft.write_ring(path, synthetic())
            result = subprocess.run([sys.executable, str(ROOT / 'bench' / 'frametime' / 'analyze.py'), str(path), '--seconds', '60'],
                                    capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout
            # 12 flips -> 11 measured frames (the first has no predecessor); periods: 1,1,1,2,1,1,1,2,1,1,1 -> 9 fast, 2 slow
            self.assertIn('FRAMES 11:', out)
            self.assertIn('1 vblank: 9 (81.8%)', out)
            self.assertIn('2 vblanks: 2 (18.2%)', out)
            # the usleep call site is attributed with its lr/pc and the measured wake time (90 us per block)
            self.assertIn('005c5734  005c5730    12    100', out)
            self.assertIn('90.0', out)
            # worker busy in the work span: exactly 12 ms of FifoRun in every 1-vblank frame
            self.assertIn('worker busy in FifoRun ms', out)

    def test_ring_reader_filters_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'ring.bin'
            ft.write_ring(path, synthetic())
            _, all_events = ft.read_ring(path)
            _, last = ft.read_ring(path, seconds=0.05)
            self.assertLess(len(last), len(all_events))
            self.assertTrue(all(e.ns >= max(x.ns for x in all_events) - 50_000_000 - 3_000_000 for e in last))


if __name__ == '__main__':
    unittest.main()
