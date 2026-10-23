"""Offline driver checks; these do not prove original-game menu navigation."""
from pathlib import Path
import tempfile
import unittest

from scripts.ratchet_fast_menu import classify, drive, set_start


def sample(flips=600, rate=60, draws=3840, spu=160_000_000):
    return (f"PERF interval_s=2.000 flips={flips} flips_per_s={rate} "
            f"draws_per_s={draws} spu_steps_per_s={spu}\n")


class FastMenuTest(unittest.TestCase):
    def test_start_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'control.txt'
            set_start(path, True)
            self.assertEqual(path.read_text(), '64 0 0 0 0 -32768 -32768\n')
            set_start(path, False)
            self.assertEqual(path.read_text(), '0 0 0 0 0 -32768 -32768\n')

    def test_sample_classes(self):
        self.assertEqual(classify(sample()), "title")
        self.assertEqual(classify(sample(draws=4680)), "menu")
        self.assertEqual(classify(sample(draws=0)), "video")
        self.assertEqual(classify(sample(flips=100, draws=100)), "boot")
        self.assertEqual(classify(sample(flips=2700, draws=100)), "screen")
        for line in ("PERF interval flips=3", sample(rate=float("nan")), sample(draws=-1)):
            self.assertIsNone(classify(line))

    def run_fixture(self, log, **options):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            control = directory / "control.txt"
            set_start(control, False)
            (directory / "stderr.log").write_text(log)
            result = drive(directory, timeout=.25, **options)
            self.assertEqual(control.read_text(), "0 0 0 0 0 -32768 -32768\n")
            return result

    def test_one_menu_sample_is_not_confirmation(self):
        self.assertEqual(self.run_fixture(sample(draws=4680)), 1)

    def test_two_distinct_menu_samples_confirm(self):
        self.assertEqual(self.run_fixture(sample(draws=4680) + sample(flips=720, draws=4680)), 0)

    def test_incomplete_line_is_not_confirmation(self):
        self.assertEqual(self.run_fixture(sample(draws=4680) + sample(draws=4680).rstrip()), 1)

    def test_timeout_releases_start(self):
        self.assertEqual(self.run_fixture(sample(draws=0)), 1)

    def test_guest_stop_aborts(self):
        self.assertEqual(self.run_fixture("STOP: unsupported instruction\n" + sample(draws=0)), 2)

    def test_early_title_timeout_releases_start(self):
        self.assertEqual(self.run_fixture(sample() + sample(flips=720), early_title=True), 1)


if __name__ == "__main__":
    unittest.main()
