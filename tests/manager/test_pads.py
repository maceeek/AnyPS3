"""SDL diagnostics boundary tests; no SDL library or hardware is opened."""

import os
import unittest
from unittest.mock import Mock, patch

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
