"""Object cache of the split native build: reuse only on an exact content key (no game, small fixtures)."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from anyps3.native_build import compile_units, object_key


@unittest.skipUnless(shutil.which('c++'), 'Requires a C++ compiler')
class ObjectCacheTests(unittest.TestCase):
    def write(self, root, header, value):
        (root/'shared.hpp').write_text(header)
        (root/'program.cpp').write_text('#include "shared.hpp"\nint unit();\nint main(){return unit()==VALUE?0:1;}\n')
        (root/'unit.cpp').write_text(f'#include "shared.hpp"\nint unit(){{return {value};}}\n')

    def build(self, root, cache, name):
        with mock.patch.dict(os.environ, {'ANYPS3_OBJECT_CACHE': str(cache)}):
            report = compile_units([root/'program.cpp', root/'unit.cpp'], root/name, flags=('-std=c++17', '-O0'),
                                   objects=root/(name+'-objects'))
        return report, subprocess.run([str(root/name)]).returncode

    def test_hit_requires_identical_unit_and_local_include(self):
        with tempfile.TemporaryDirectory() as d:
            root, cache = Path(d), Path(d)/'cache'
            self.write(root, '#define VALUE 7\n', 7)
            report, code = self.build(root, cache, 'a')
            self.assertEqual((report['object_cache_hits'], code), ([], 0))
            report, code = self.build(root, cache, 'b')
            self.assertEqual((report['object_cache_hits'], code), (['program.cpp', 'unit.cpp'], 0))
            # Only the unit body changes: program.cpp is reused, unit.cpp recompiled (exit 1 proves the new body).
            (root/'unit.cpp').write_text('#include "shared.hpp"\nint unit(){return 8;}\n')
            report, code = self.build(root, cache, 'c')
            self.assertEqual((report['object_cache_hits'], code), (['program.cpp'], 1))
            # A header change invalidates every unit that includes it.
            self.write(root, '#define VALUE 9\n', 9)
            report, code = self.build(root, cache, 'd')
            self.assertEqual((report['object_cache_hits'], code), ([], 0))

    def test_key_depends_on_flags_compiler_and_nested_include(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'inner.hpp').write_text('int x;\n')
            (root/'outer.hpp').write_text('#include "inner.hpp"\n')
            (root/'u.cpp').write_text('#include "outer.hpp"\n#include <vector>\n')
            base = object_key(root/'u.cpp', ['-O2'], b'gcc 1')
            self.assertEqual(base, object_key(root/'u.cpp', ['-O2'], b'gcc 1'))
            self.assertNotEqual(base, object_key(root/'u.cpp', ['-O3'], b'gcc 1'))
            self.assertNotEqual(base, object_key(root/'u.cpp', ['-O2'], b'gcc 2'))
            (root/'inner.hpp').write_text('int y;\n')
            self.assertNotEqual(base, object_key(root/'u.cpp', ['-O2'], b'gcc 1'))

    def test_key_covers_include_path_headers_and_macros(self):
        # A header found only through -I (not next to the unit) and a -D macro must both change the key.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'inc').mkdir()
            (root/'inc'/'value.hpp').write_text('#define VALUE 1\nint f(){return VALUE;}\n')
            (root/'u.cpp').write_text('#include "value.hpp"\n')
            flags = ['-O2', f'-I{root/"inc"}']
            base = object_key(root/'u.cpp', flags, b'gcc')
            (root/'inc'/'value.hpp').write_text('#define VALUE 2\nint f(){return VALUE;}\n')
            changed = object_key(root/'u.cpp', flags, b'gcc')
            self.assertNotEqual(base, changed)
            (root/'u2.cpp').write_text('int g(){return LEVEL;}\n')
            self.assertNotEqual(object_key(root/'u2.cpp', ['-DLEVEL=1'], b'gcc'),
                                object_key(root/'u2.cpp', ['-DLEVEL=2'], b'gcc'))


if __name__ == '__main__':
    unittest.main()
