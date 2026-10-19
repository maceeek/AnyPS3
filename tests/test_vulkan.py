"""Real Vulkan execution and readback; Lavapipe supports headless CI."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.render_demo import source


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/vulkan/vulkan.h').exists(),
                     'Requires C++ and Vulkan development headers')
class VulkanTests(unittest.TestCase):
    def test_rsx_clear_readback_padding_clipping_and_rejected_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cpp = root / 'render.cpp'
            binary = root / 'render'
            capture = root / 'capture.ppm'
            cpp.write_text(source())
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra',
                                    '-Werror', '-DANYPS3_VULKAN', str(cpp), '-lvulkan',
                                    '-o', str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = os.environ.copy()
            # Prefer the deterministic CPU Vulkan driver in CI when installed.
            # An explicitly selected ICD is honored for hardware verification.
            lavapipe = Path('/usr/share/vulkan/icd.d/lvp_icd.json')
            if lavapipe.exists() and not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env['VK_DRIVER_FILES'] = str(lavapipe)
            validation = Path('/usr/share/vulkan/explicit_layer.d/VkLayer_khronos_validation.json')
            if validation.exists():
                layers = env.get('VK_INSTANCE_LAYERS', '').split(':')
                if 'VK_LAYER_KHRONOS_validation' not in layers:
                    layers.append('VK_LAYER_KHRONOS_validation')
                env['VK_INSTANCE_LAYERS'] = ':'.join(filter(None, layers))
                env['VK_LAYER_VALIDATE_SYNC'] = '1'
            run = subprocess.run([str(binary), str(capture)], env=env,
                                 capture_output=True, text=True, timeout=60)
            if run.returncode == 77:
                if env.get('ANYPS3_REQUIRE_VULKAN') == '1':
                    self.fail(run.stderr)
                self.skipTest('No usable Vulkan device: ' + run.stderr.strip())
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn('Validation Error', run.stdout + run.stderr)
            self.assertNotIn('Validation Warning', run.stdout + run.stderr)
            self.assertIn('PASS Vulkan RSX: 57600 pixels, 5 clears', run.stdout)
            self.assertIn('native Vulkan device=', run.stderr)
            data = capture.read_bytes()
            header = b'P6\n# AnyPS3 synthetic RSX Vulkan diagnostic, not a game frame\n320 180\n255\n'
            self.assertTrue(data.startswith(header))
            pixels = data[len(header):]
            self.assertEqual(len(pixels), 320 * 180 * 3)
            scanout = (root / 'capture.ppm.frames' / 'frame-000001.ppm').read_bytes()
            scanout_header = b'P6\n# AnyPS3 native display buffer 1\n320 180\n255\n'
            self.assertEqual(scanout, scanout_header + pixels)
            # Check the artifact independently of native assertions.
            for x, y, expected in [(0, 0, '182438'), (20, 30, 'e13562'),
                                   (120, 30, '47c981'), (220, 30, '3b79ef'),
                                   (319, 179, 'e9bc43'), (100, 100, '182438')]:
                offset = (y * 320 + x) * 3
                self.assertEqual(pixels[offset:offset + 3].hex(), expected)
            # A second invocation must preserve an existing capture.
            repeat = subprocess.run([str(binary), str(capture)], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(repeat.returncode, 1)
            self.assertIn('cannot create new PPM output', repeat.stderr)
            self.assertEqual(capture.read_bytes(), data)
