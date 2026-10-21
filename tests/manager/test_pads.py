"""SDL diagnostics boundary tests; no SDL library or hardware is opened."""

import os
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from anyps3_manager.pads import Pads
from anyps3_manager.storage import ManagerError


class PadLibraryTests(unittest.TestCase):
    def setUp(self):
        silent = patch.dict(os.environ, SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null")
        silent.start()
        self.addCleanup(silent.stop)

    def test_old_sdl_reports_required_version_before_initializing(self):
        library = Mock()
        del library.SDL_GameControllerHasRumble
        with patch("anyps3_manager.pads.C.CDLL", return_value=library):
            with self.assertRaises(ManagerError) as caught:
                Pads()
        self.assertEqual(caught.exception.code, "incompatible-sdl")
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertIn("2.0.18", str(caught.exception))
        self.assertIn("SDL_GameControllerHasRumble", str(caught.exception))
        library.SDL_InitSubSystem.assert_not_called()

    def test_diagnostics_initialize_controller_subsystem_only(self):
        library = Mock()
        library.SDL_InitSubSystem.return_value = 0
        with patch("anyps3_manager.pads.C.CDLL", return_value=library):
            pads = Pads()
            pads.close()
        library.SDL_InitSubSystem.assert_called_once_with(0x2000)
        library.SDL_QuitSubSystem.assert_called_once_with(0x2000)
        self.assertEqual(os.environ["SDL_JOYSTICK_HIDAPI_PS5"], "1")
        self.assertEqual(os.environ["SDL_AUDIODRIVER"], "dummy")
        self.assertEqual(os.environ["ANYPS3_AUDIO_SINK"], "null")


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
