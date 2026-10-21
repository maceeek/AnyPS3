"""Capability diagnostics must work with the advertised SDL 2.0.14 floor."""
from types import SimpleNamespace
import unittest

from anyps3_manager.pads import Pads


class PadDiagnosticsTests(unittest.TestCase):
    def test_rumble_detection_requires_support_query(self):
        pads = Pads.__new__(Pads)
        calls = []
        def rumble(*args):
            calls.append(args)
            return 0
        pads.sdl = SimpleNamespace(SDL_GameControllerRumble=rumble)
        self.assertFalse(pads.has_rumble(123))
        self.assertEqual(calls, [])
        pads.sdl.SDL_GameControllerHasRumble = lambda _: 1
        self.assertTrue(pads.has_rumble(123))
        pads.sdl.SDL_GameControllerHasRumble = lambda _: 0
        self.assertFalse(pads.has_rumble(123))
        self.assertEqual(calls, [])
        pads.sdl = SimpleNamespace()
        self.assertFalse(pads.has_rumble(123))
        self.assertFalse(pads.has_sensor(123, 1))

    def test_sensor_capabilities_are_independent(self):
        pads = Pads.__new__(Pads)
        pads.sdl = SimpleNamespace(SDL_GameControllerHasSensor=lambda _, kind: kind == 1)
        self.assertTrue(pads.has_sensor(123, 1))
        self.assertFalse(pads.has_sensor(123, 2))
