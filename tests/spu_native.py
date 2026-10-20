"""Scenario-binary helper for native SPU runtime tests.

One native binary per test class: every scenario is a block of C++ selected by the SPU_POOL_CASE environment
variable, so the ~1.8 MB generated translation unit is compiled once per module run and each test runs one
scenario. Expectations live in the scenarios (known stored values, handshake order, clocks), never in a second
implementation of the code under test.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.test_par1 import source


def build(scenarios, *, globals_cpp='', words=(), name='spu-native'):
    """Compile every scenario into one binary; returns (directory, executable). Raises AssertionError on failure."""
    cases = ''.join(f' if(std::string(case_name)=="{key}"){{{body}\n return 0;}}\n' for key, body in scenarios.items())
    body = ' const char* case_name=std::getenv("SPU_POOL_CASE");assert(case_name);\n' + cases + \
           ' std::fprintf(stderr,"unknown case %s\\n",case_name);return 2;\n'
    code = source(body, full=True, words=words).replace('int main(){(void)crc32;', globals_cpp + '\nint main(){(void)crc32;')
    directory = Path(tempfile.mkdtemp(prefix=name + '-'))
    cpp = directory / (name + '.cpp')
    cpp.write_text(code)
    exe = directory / name
    flags = os.environ.get('ANYPS3_SPU_NATIVE_CXXFLAGS', '-O2').split()
    result = subprocess.run(['c++', '-std=c++17', *flags, '-Wall', '-Wextra', '-Werror', '-pthread', str(cpp), '-o', str(exe)],
                            capture_output=True, text=True, timeout=600)
    (directory / 'build.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        shutil.rmtree(directory, ignore_errors=True)
        raise AssertionError(result.stderr[-10000:])
    return directory, exe


def run(directory, exe, case, timeout=90):
    """Run one scenario; returns (returncode, stdout, stderr)."""
    env = {**os.environ, 'SPU_POOL_CASE': case, 'ANYPS3_SPU_THREADS': '0', 'ANYPS3_SPU_WORKER': '0',
           'ANYPS3_PERF_REPORT': '0', 'SDL_AUDIODRIVER': 'dummy', 'ANYPS3_AUDIO_SINK': 'null',
           'ANYPS3_SPU_MAX_STEPS': '1000000000000'}
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=timeout, env=env)
    (directory / f'{case}.log').write_text(result.stdout + result.stderr)
    return result.returncode, result.stdout, result.stderr
