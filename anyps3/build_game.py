"""Build the Ratchet diagnostic AOT bundle using locally extracted firmware.

Creates fresh artifacts only; does not compile, execute, or install anything.
"""

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .bundle import combine
from .elf import ElfImage
from .errors import AnyPS3Error
from .profile import check_profile
from .program import compile_program
from .native_build import write_units
from .modules import exported_bindings
from .prx import link_references, relocate
from .self_file import unpack_self
from .spu_scan import augment_spu_program

# These IDs are requested by the observed BCES00511 initialization path.
# HTTP/HTTP_UTIL/HTTPS share the real libhttp image and its two export tables.
MODULES = (
    ('libnet', (0,)), ('libssl', (3,)), ('libhttp', (1, 2, 4)),
    ('libsysutil', (21,)), ('libsysutil_userinfo', (50,)), ('libmic', (34,)),
    ('libfs', (14,)), ('libnetctl', (20,)), ('libsysutil_np', (22,)),
    ('librtc', (9,)), ('libsysutil_np_trophy', (0xf035,)),
    ('libsre', ()), # Resident SPURS/sync exports; this image has no module_start.
    ('libgcm_sys', ()), # Resident GCM library; its original module_start executes before the game.
    ('libio', (23,)), # Requested after startup flips; cellPad exports use native SDL2 input.
)


def opd_section_seeds(image: ElfImage, spec: dict) -> list[int]:
    """Code entries of the original .opd section, verified entry by entry.

    The section bytes must hash to the pinned digest, every descriptor must
    carry the process TOC, and every code address must be aligned executable
    code of the same image. Seeds only add translation targets; guest control
    flow still decides what executes.
    """
    address, size, toc = spec.get('address'), spec.get('size'), spec.get('toc')
    if any(type(x) is not int for x in (address, size, toc)) or address % 8 or size <= 0 or size % 8:
        raise AnyPS3Error('Invalid profile OPD section.')
    if image.resolve_entry()[1] != toc:
        raise AnyPS3Error('OPD section TOC differs from the process entry TOC.')
    data = image.read_virtual(address, size)
    if hashlib.sha256(data).hexdigest() != spec.get('sha256'):
        raise AnyPS3Error('OPD section identity mismatch.')
    codes = set()
    for offset in range(0, size, 8):
        code = int.from_bytes(data[offset:offset+4], 'big')
        if int.from_bytes(data[offset+4:offset+8], 'big') != toc:
            raise AnyPS3Error(f'OPD 0x{address+offset:x} has a foreign TOC.')
        if code % 4:
            raise AnyPS3Error(f'OPD 0x{address+offset:x} has unaligned code.')
        image.read_virtual(code, 4, executable=True)
        codes.add(code)
    return sorted(codes)


def module_opd_seeds(image: ElfImage, toc: int) -> list[int]:
    """Code of every {code, TOC} function descriptor in a relocated module's data.

    PPC64 PS3 function pointers are OPDs; a descriptor carrying exactly this
    module's TOC and pointing at aligned code of its executable segments is an
    indirect-call target. Seeds only add translation targets.
    """
    loads = [s for s in image.segments if s.type == 1 and s.file_size]
    code_ranges = [(s.virtual_address, s.virtual_address+s.file_size) for s in loads if s.flags & 1]
    codes = set()
    for s in loads:
        if s.flags & 1:
            continue
        data = image.data[s.offset:s.offset+s.file_size]
        for i in range(0, len(data)-7, 4):
            code = int.from_bytes(data[i:i+4], 'big')
            if int.from_bytes(data[i+4:i+8], 'big') == toc and not code % 4 and any(a <= code < b for a, b in code_ranges):
                codes.add(code)
    return sorted(codes)


def build(game: Path, firmware: Path, profile: Path, key: Path, output: Path, *, max_steps=1_000_000,
          translation_units=96, spu_content_cache: Path | None = None) -> dict:
    image = ElfImage.parse(game.read_bytes())
    digest = hashlib.sha256(image.data).hexdigest()
    identity = check_profile(profile, digest)
    if not identity['identity_verified']:
        raise AnyPS3Error('Native game bundle requires a pinned matching profile.')
    profile_bytes=profile.read_bytes()
    spec = json.loads(profile_bytes)
    if spec.get('title_id') != 'BCES00511':
        raise AnyPS3Error('This experimental module catalog is specific to BCES00511.')
    seeds = spec.get('static_seed_entries', [])
    if not isinstance(seeds,list) or len(seeds)>10000 or any(type(x) is not int for x in seeds):
        raise AnyPS3Error('Invalid profile static seeds.')
    if 'static_opd_section' in spec:
        seeds = [*seeds, *opd_section_seeds(image, spec['static_opd_section'])]
    output.mkdir(parents=True, exist_ok=False)
    catalog, modules, imports, integrity_reports = [], [], [], []
    base = 0x20000000
    for name, ids in MODULES:
        data, integrity = unpack_self((firmware/(name+'.sprx')).read_bytes(),key,allow_prx=True)
        module = relocate(ElfImage.parse(data),base=base)
        seed_spec = spec.get('firmware_static_seeds', {}).get(name)
        if seed_spec:
            if seed_spec.get('analysis_sha256') != hashlib.sha256(data).hexdigest():
                raise AnyPS3Error(f'Firmware static seed hash mismatch: {name}.')
            offsets = seed_spec.get('offsets')
            if not isinstance(offsets,list) or len(offsets)>10000 or any(type(x) is not int or x<0 or x%4 for x in offsets):
                raise AnyPS3Error(f'Invalid firmware seed offsets: {name}.')
            for offset in offsets:
                module.image.read_virtual(base+offset,4,executable=True)
                seeds.append(base+offset)
        if spec.get('firmware_opd_seeding'):
            opd = module_opd_seeds(module.image, module.report['toc'])
            seeds.extend(opd)
            integrity['opd_seed_count'] = len(opd)
        base = module.report['next_base']
        modules.append(module)
        seeds.extend(module.report['static_constructor_entries'])
        imports.extend(module.report['imports'])
        catalog.append({'ids':ids, 'name':name, 'module_name':module.report['name'], 'resident':not ids, 'exports':module.report['exports'],
                        'start':module.report['special_exports'].get('0xbc9a0086',{}).get('descriptor',0)})
        integrity_reports.append({'name':name, 'self':integrity, 'relocation':module.report})
    bindings, images = exported_bindings(catalog), []
    for module, integrity in zip(modules, integrity_reports):
        linked_image, linked = link_references(module, bindings)
        images.append(linked_image)
        integrity['import_references'] = linked
        seeds.extend(entry['code'] for entry in linked)
    combined = combine(image,tuple(images))
    spu_images=[];spu_overlays=[]
    for embedded in spec.get('embedded_spu_images',[]):
        payload=combined.read_virtual(embedded['address'],embedded['size'])
        if hashlib.sha256(payload).hexdigest()!=embedded['sha256']:
            raise AnyPS3Error('Embedded SPU image identity mismatch.')
        spu_images.append(ElfImage.parse(payload))
        overlays=[]
        for overlay in embedded.get('overlays',[]):
            payload=combined.read_virtual(overlay['address'],overlay['size'])
            if hashlib.sha256(payload).hexdigest()!=overlay['sha256']:
                raise AnyPS3Error('SPU overlay identity mismatch.')
            overlays.append((overlay['local_address'],payload))
        spu_overlays.append(tuple(overlays))
    # Automatic candidates use declared SPU ELF headers in already decoded
    # original PPU/PRX segments. Unknown raw job formats stay on the fallback.
    cache_bytes = spu_content_cache.read_bytes() if spu_content_cache else None
    content_cache = json.loads(cache_bytes) if cache_bytes is not None else None
    spu_images, spu_overlays, spu_discovery = augment_spu_program(
        spu_images, spu_overlays,
        [(f"combined-PT_LOAD-{s.index}", combined.data[s.offset:s.offset+s.file_size])
         for s in combined.segments if s.type == 1], content_cache)
    program = compile_program(combined,process_entry=True,diagnostic=True,max_instructions=4000000,
                              extra_entries=tuple(seeds),additional_imports=tuple(imports),native_modules=tuple(catalog),
                              code_tocs=tuple((r['relocation']['base'],r['relocation']['next_base'],r['relocation']['toc'])
                                              for r in integrity_reports))
    program=replace(program,spu_images=tuple(spu_images),spu_overlays=tuple(spu_overlays))
    units = program.cpp_units(max_steps=max_steps, units=translation_units)
    report = program.report()
    report.update(spu_discovery=spu_discovery,
                  spu_content_cache_sha256=hashlib.sha256(cache_bytes).hexdigest() if cache_bytes is not None else None,
                  original_game_sha256=digest,firmware_modules=integrity_reports,
                  profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
                  source_sha256={path.name:hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(Path(__file__).parent.glob('*.py'))})
    (output/'program.elf').write_bytes(combined.data)
    report['translation_units'] = write_units(output, units)
    (output/'build.json').write_text(json.dumps(report,indent=2)+'\n')
    (output/'profile.json').write_bytes(profile_bytes)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game',type=Path)
    parser.add_argument('--firmware',type=Path,required=True,help='Extracted dev_flash/sys/external directory')
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--key-file',type=Path,required=True)
    parser.add_argument('-o','--output',type=Path,required=True)
    parser.add_argument('--max-steps',type=int,default=1_000_000)
    parser.add_argument('--spu-content-cache',type=Path,help='Verified SHA-256 SPU observation cache')
    parser.add_argument('--units',type=int,default=96,help='parallel C++ translation units (see anyps3.native_build)')
    args=parser.parse_args()
    try:
        report=build(args.game,args.firmware,args.profile,args.key_file,args.output,max_steps=args.max_steps,translation_units=args.units,spu_content_cache=args.spu_content_cache)
    except (AnyPS3Error,OSError,ValueError,KeyError,TypeError) as exc:
        parser.exit(1,f'Native bundle build failed (partial output may remain): {exc}\n')
    print(json.dumps({k:report[k] for k in ('original_game_sha256','elf_sha256','translated_instructions','trap_count','native_modules')},indent=2))


if __name__=='__main__':
    main()
