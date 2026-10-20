"""Parallel native compilation of split AOT translation units.

The unit sources come from Program.cpp_units(); every unit is compiled with
identical flags (PpuState layout depends on -DANYPS3_VULKAN) and then linked.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import time

from .errors import AnyPS3Error

FLAGS = ('-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread')


def write_units(directory: Path, units: dict[str, str]) -> dict:
    """Write unit files and return their manifest (name -> sha256)."""
    if 'program.cpp' not in units or 'anyps3.hpp' not in units:
        raise AnyPS3Error('Split translation requires program.cpp and anyps3.hpp.')
    manifest = {}
    for name, text in sorted(units.items()):
        data = text.encode()
        (directory/name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    (directory/'units.json').write_text(json.dumps(dict(schema_version=1, units=manifest), indent=2)+'\n')
    return manifest


def unit_sources(directory: Path) -> list[Path]:
    manifest = json.loads((directory/'units.json').read_text())['units']
    for name, digest in manifest.items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != digest:
            raise AnyPS3Error(f'Translation unit {name} does not match units.json.')
    return [directory/name for name in sorted(manifest) if name.endswith('.cpp')]


def compiler_identity(compile_flags: list[str]) -> bytes:
    """Compiler version plus every predefined macro under these flags (resolves -march=native to the host ISA)."""
    version = subprocess.run(['c++', '--version'], capture_output=True, check=True).stdout
    macros = subprocess.run(['c++', *compile_flags, '-dM', '-E', '-x', 'c++', os.devnull],
                            capture_output=True, check=True).stdout
    return version+b'\0'+macros


def object_key(source: Path, compile_flags: list[str], compiler: bytes) -> str:
    """Content key of one object: compiler identity, flags and the fully preprocessed unit.

    Preprocessing covers every header the compiler actually reads (quoted, -I, system) and all macros.
    """
    digest = hashlib.sha256(compiler+b'\0'+'\0'.join(compile_flags).encode()+b'\0')
    with subprocess.Popen(['c++', *compile_flags, '-E', '-P', str(source), '-o', '-'],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        for chunk in iter(lambda: process.stdout.read(1 << 20), b''):
            digest.update(chunk)
    if process.returncode:
        raise AnyPS3Error(f'Preprocessing {source} for the object cache failed.')
    return digest.hexdigest()


def compile_units(sources: list[Path], output: Path, *, flags=FLAGS, defines=(), libraries=(),
                  jobs: int | None = None, objects: Path | None = None) -> dict:
    """Compile each source in parallel to objects, link, return a timing report.

    ANYPS3_OBJECT_CACHE=<dir> reuses an object only when its object_key matches exactly (same compiler identity,
    flags and preprocessed unit); hits are listed in the report.
    """
    objects = objects or output.parent/'objects'
    objects.mkdir(exist_ok=True)
    compile_flags = [*flags, *(f'-D{d}' for d in defines)]
    cache = Path(os.environ['ANYPS3_OBJECT_CACHE']) if os.environ.get('ANYPS3_OBJECT_CACHE') else None
    compiler = b''
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        compiler = compiler_identity(compile_flags)
    # GCC's CFG/VRP passes are superlinear on dense guarded SPU switches.
    # Preserve explicit caller choices; never change PPU/program flags.
    version = subprocess.run(['c++', '--version'], capture_output=True, text=True).stdout.lower()
    spu_extra_flags = []
    if 'clang' not in version and any(name in version for name in ('gcc', 'g++', 'free software foundation')):
        for option in ('crossjumping', 'tree-vrp'):
            if not any(f in (f'-f{option}', f'-fno-{option}') for f in compile_flags):
                spu_extra_flags.append(f'-fno-{option}')
    hits = []
    def build(source):
        start = time.monotonic()
        target = objects/(source.stem+'.o')
        # ANYPS3_NATIVE_PROGRAM_CXXFLAGS (diagnostics only, e.g. "-fno-omit-frame-pointer" for sampler call stacks of the
        # RSX thread) applies to program.cpp alone; unset in normal builds, so object-cache keys stay unchanged.
        program_flags = os.environ.get('ANYPS3_NATIVE_PROGRAM_CXXFLAGS', '').split() if source.name == 'program.cpp' else []
        unit_flags = [*compile_flags, *program_flags, *(spu_extra_flags if source.name.startswith('spu-') else ())]
        key = object_key(source, unit_flags, compiler) if cache else None
        if key and (cache/(key+'.o')).is_file():
            shutil.copyfile(cache/(key+'.o'), target)
            (objects/(source.stem+'.log')).write_text(f'object cache hit {key}\n')
            hits.append(source.name)
            return source.name, 0, time.monotonic()-start, target
        result = subprocess.run(['c++', *unit_flags, '-c', str(source), '-o', str(target)], capture_output=True)
        (objects/(source.stem+'.log')).write_bytes(result.stdout+result.stderr)
        if key and result.returncode == 0:
            temporary = cache/f'{key}.{os.getpid()}.{source.stem}.tmp'
            shutil.copyfile(target, temporary)
            os.replace(temporary, cache/(key+'.o'))
        return source.name, result.returncode, time.monotonic()-start, target
    started = time.monotonic()
    # Largest units first so the slowest compile starts immediately.
    ordered = sorted(sources, key=lambda path: -path.stat().st_size)
    with ThreadPoolExecutor(max_workers=jobs or os.cpu_count() or 1) as pool:
        results = list(pool.map(build, ordered))
    failed = [name for name, code, _, _ in results if code]
    if failed:
        raise AnyPS3Error('Native unit compilation failed: '+', '.join(failed)+f' (logs in {objects})')
    link_start = time.monotonic()
    result = subprocess.run(['c++', *compile_flags, *(str(t) for *_, t in results), *libraries, '-o', str(output)],
                            capture_output=True)
    (objects/'link.log').write_bytes(result.stdout+result.stderr)
    if result.returncode:
        raise AnyPS3Error(f'Native link failed (log {objects/"link.log"})')
    return dict(compiler_flags=compile_flags, spu_compiler_flags=[*compile_flags, *spu_extra_flags], libraries=list(libraries),
                object_cache=str(cache) if cache else None, object_cache_hits=sorted(hits),
                units={name: round(seconds, 1) for name, _, seconds, _ in results},
                link_seconds=round(time.monotonic()-link_start, 1),
                total_seconds=round(time.monotonic()-started, 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path, help='Directory with units.json')
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('-j', '--jobs', type=int)
    parser.add_argument('--vulkan', action='store_true', help='-DANYPS3_VULKAN and -lvulkan')
    parser.add_argument('--extra-flags', default='', help='Additional shell-quoted C++ flags (recorded in compile.json)')
    parser.add_argument("--rsx-vk", action="store_true", help="RSX Vulkan graphics backend (default with --vulkan)")
    parser.add_argument("--rsx-cpu", action="store_true", help="Build Vulkan clear only; omit graphics shaders")
    args = parser.parse_args()
    if args.rsx_vk:
        args.vulkan = True
    rsx_vk = args.vulkan and not args.rsx_cpu
    report = compile_units(unit_sources(args.bundle), args.output or args.bundle/'program', jobs=args.jobs,
                           flags=(*FLAGS, *shlex.split(args.extra_flags)),
                           defines=('ANYPS3_VULKAN', 'ANYPS3_RSX_VK') if rsx_vk else ('ANYPS3_VULKAN',) if args.vulkan else (),
                           libraries=('-lvulkan', '-lglslang', '-lSPIRV', '-lglslang-default-resource-limits') if rsx_vk else ('-lvulkan',) if args.vulkan else ())
    (args.bundle/'compile.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k: report[k] for k in ('link_seconds', 'total_seconds')}))


if __name__ == '__main__':
    main()
