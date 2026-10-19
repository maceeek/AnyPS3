"""Read-only inventory of original SPU words and bounded direct control flow.

Run with ``python -m anyps3.spu_scan --bundle local/ratchet/native-bundle-288``.
No code/data inference is made from the number of zero words. Unvisited words
are *unresolved*, never proven unreachable: DMA, indirect calls and job entry
points require additional evidence. No cross-product of overlay variants is
used to invent paths between mutually exclusive images.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import struct

from .elf import ElfImage
from .errors import AnyPS3Error
from .spu import lift, signed


def discover_spu_images(sources):
    """Discover self-contained BE ELF32/EM_SPU executables, without game rules.

    Sources are (identity, decoded bytes); encrypted/compressed inputs must be
    decoded by their existing reader first. Only declared file-backed PT_LOAD
    instructions are candidates. No raw-job signature or extent is guessed.
    Returns (images, evidence). Trailing section/debug bytes are not required
    for translation; identity covers the exact header/segment extent consumed.
    """
    images, evidence, seen = [], [], set()
    for name, raw in sources:
        raw = bytes(raw)
        source_hash = hashlib.sha256(raw).hexdigest()
        at = raw.find(b'\x7fELF\x01\x02\x01')
        while at >= 0:
            try:
                # LS is 256 KiB. A header/table plus file-backed segments may
                # include notes/alignment, bounded here to avoid false headers
                # claiming arbitrary amounts of a containing file.
                image = ElfImage.parse(raw[at:at + 4 * 1024 * 1024])
                loads = [s for s in image.segments if s.type == 1]
                if ((image.machine, image.bits, image.byte_order, image.type) != (23, 32, 'big', 2)
                        or not loads or len(image.segments) > 256
                        or image.entry % 4
                        or any(s.virtual_address + s.memory_size > 0x40000
                               or (s.flags & 1 and (s.virtual_address % 4 or s.file_size % 4))
                               for s in loads)):
                    raise AnyPS3Error('Not a bounded SPU executable')
                image.read_virtual(image.entry, 4, executable=True)
                phoff = int.from_bytes(image.data[28:32], 'big')
                phnum = int.from_bytes(image.data[44:46], 'big')
                end = max([52, phoff + phnum * 32,
                           *(s.offset + s.file_size for s in image.segments if s.file_size)])
                payload = image.data[:end]
                digest = hashlib.sha256(payload).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    images.append(ElfImage.parse(payload))
                    evidence.append(dict(kind='embedded_spu_elf', source=name,
                        source_sha256=source_hash, offset=at, size=end, sha256=digest,
                        entry=image.entry,
                        executable_segments=[dict(address=s.virtual_address, size=s.file_size,
                            sha256=hashlib.sha256(payload[s.offset:s.offset+s.file_size]).hexdigest())
                            for s in loads if s.flags & 1]))
            except AnyPS3Error:
                pass  # A magic match alone never establishes an executable.
            at = raw.find(b'\x7fELF\x01\x02\x01', at + 1)
    return tuple(images), evidence


def cache_from_dynamic_log(path):
    """Verified observed cells, keyed solely by SHA-256 of their exact bytes.

    This is a translation input, never a replacement for guest LS. A cell has
    no inferred function entry/extent; the relocation dispatcher can enter at
    any word and revalidates bytes. DMA EA and game identity are not cache keys.
    """
    cells = {}
    with Path(path).open() as stream:
        for lineno, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                data = bytes.fromhex(row['data_hex'])
                base, pc, size = row['ls_base'], row['pc'], row['size']
                if (row['schema_version'] != 1 or type(base) is not int or type(pc) is not int
                        or type(size) is not int or base < 0 or base % 4 or size != 64
                        or base + size > 0x40000 or len(data) != size or pc % 4
                        or not base <= pc < base + size
                        or int.from_bytes(data[pc-base:pc-base+4], 'big') != row['word']
                        or hashlib.sha256(data).hexdigest() != row['sha256']):
                    raise ValueError('invalid range, word or digest')
                cells[row['sha256']] = data
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise AnyPS3Error(f'Invalid SPU cache record at line {lineno}: {error}') from error
    return dict(schema_version=1, kind='spu_observed_cells',
                cells=[dict(sha256=h, data_hex=data.hex()) for h, data in sorted(cells.items())])


def cached_spu_image(cache):
    """Pack exact cache bytes into translation-only ELF metadata (never booted).

    Disjoint cells have independent canonical PCs. Relative operands use the
    real runtime PC in the single lifter. Native runs cannot cross cell borders
    (each cell is its own overlay); only a validated byte match can select one.
    """
    if cache.get('schema_version') != 1 or cache.get('kind') != 'spu_observed_cells':
        raise AnyPS3Error('Unsupported SPU content cache')
    images, overlays, bank, seen = [], [], [], set()
    for row in cache['cells']:
        data = bytes.fromhex(row['data_hex'])
        if len(data) != 64 or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise AnyPS3Error('SPU content cache identity mismatch')
        if row['sha256'] in seen:
            continue
        seen.add(row['sha256'])
        if len(bank) == 0x40000 // 128:
            images.append(ElfImage(b'', 32, 'big', 0, 0, 23, 0, ()))
            overlays.append(tuple(bank)); bank = []
        # A 64-byte gap ensures private native runs never cross observations.
        bank.append((len(bank) * 128, data))
    if bank:
        images.append(ElfImage(b'', 32, 'big', 0, 0, 23, 0, ()))
        overlays.append(tuple(bank))
    return tuple(images), tuple(overlays)


def augment_spu_program(images, overlays, sources=(), cache=None):
    """General build adapter; keeps explicitly supplied images/overlays intact."""
    images, overlays = list(images), list(overlays)
    overlays.extend(() for _ in range(len(images) - len(overlays)))
    found, evidence = discover_spu_images(sources)
    def identity(image):
        return (image.entry, tuple((s.virtual_address, s.memory_size, s.flags,
                                   image.data[s.offset:s.offset+s.file_size])
                                  for s in image.segments if s.type == 1))
    known = {identity(i) for i in images}
    for image in found:
        key = identity(image)
        if key not in known:
            known.add(key); images.append(image); overlays.append(())
    if cache is not None:
        more_images, more_overlays = cached_spu_image(cache)
        images.extend(more_images); overlays.extend(more_overlays)
    return tuple(images), tuple(overlays), evidence


def opcode_table(path):
    """Read the pinned reference decoder, including variable-width opcodes."""
    table, instructions = {}, []
    for magnitude, value, name in re.findall(
            r'\{\s*(\d+),\s*(0x[0-9a-f]+), GET\((\w+)\)', Path(path).read_text()):
        magnitude, value = int(magnitude), int(value, 16)
        instructions.append((name, magnitude, value))
        for low in range(1 << magnitude):
            table[(value << magnitude) | low] = name
    if not instructions:
        raise ValueError('Reference opcode table is empty')
    return table, instructions


def direct_flow(words, roots, names):
    """Potential direct paths; conditions/call returns are conservatively joined.

    Each source is separate. Unknown indirect destinations and out-of-source
    edges are recorded rather than silently treated as covered translations.
    A root is a candidate unless its original ELF entry is independently known.
    """
    visited, parent, edges, indirect = set(), {}, set(), set()
    pending = list(roots)
    while pending:
        pc = pending.pop()
        if pc in visited or pc not in words:
            continue
        visited.add(pc)
        word = words[pc]
        name = names.get(word >> 21, 'UNK')
        next_pc = (pc + 4) & 0x3fffc
        successors = []
        if name in ('BR', 'BRA', 'BRSL', 'BRASL', 'BRZ', 'BRNZ', 'BRHZ', 'BRHNZ'):
            base = 0 if name in ('BRA', 'BRASL') else pc
            successors.append((base + signed((word >> 7) & 65535, 16) * 4) & 0x3fffc)
            if name not in ('BR', 'BRA'):
                successors.append(next_pc)
        elif name in ('BI', 'BISL', 'BIZ', 'BINZ', 'BIHZ', 'BIHNZ', 'BISLED', 'IRET'):
            indirect.add(pc)
            if name not in ('BI', 'IRET'):
                successors.append(next_pc)
        elif name == 'STOP':
            # Only returning, already modeled LV2 services may fall through.
            if word & 0x3fff in (1, 0x100, 0x110, 0x111):
                successors.append(next_pc)
        elif name not in ('UNK', 'STOPD'):
            # HALT instructions may be false; preserve their fallthrough.
            successors.append(next_pc)
        for target in successors:
            if target not in words:
                edges.add((pc, target))
            elif target not in visited:
                parent.setdefault(target, pc)
                pending.append(target)
    return visited, parent, sorted(edges), sorted(indirect)


def spans(values):
    """Lossless inclusive offset ranges with stride four."""
    result = []
    for value in sorted(set(values)):
        if result and value == result[-1][1] + 4:
            result[-1][1] = value
        else:
            result.append([value, value])
    return [[hex(a), hex(b)] for a, b in result]


def embedded_layout(program, address, base):
    """Recover section/entry evidence from adjacent *original* ELF headers."""
    for delta in (0x80, 0x100):
        try:
            header = program.read_virtual(address - delta, 52)
            if header[:7] != b'\x7fELF\x01\x02\x01' or int.from_bytes(header[18:20], 'big') != 23:
                continue
            phoff = int.from_bytes(header[28:32], 'big')
            phsize, phnum, shsize, shnum, shstrndx = struct.unpack_from('>5H', header, 42)
            if phsize != 32 or shsize != 40 or not 0 < phnum <= 128 or not 0 < shnum <= 128 or shstrndx >= shnum:
                continue
            ph = program.read_virtual(address - delta + phoff, phsize * phnum)
            segments = [struct.unpack_from('>8I', ph, i * 32) for i in range(phnum)]
            if not any(s[0] == 1 and s[1] == delta and s[2] == base for s in segments):
                continue
            shoff = int.from_bytes(header[32:36], 'big')
            raw = program.read_virtual(address - delta + shoff, shsize * shnum)
            sections = [struct.unpack_from('>10I', raw, i * 40) for i in range(shnum)]
            strings = program.read_virtual(address - delta + sections[shstrndx][4], sections[shstrndx][5])
            layout = [dict(name=strings[s[0]:].split(b'\0', 1)[0].decode('ascii'),
                type=s[1], flags=s[2], address=s[3], size=s[5]) for s in sections if s[2] & 2]
            return dict(elf_ea=address - delta, entry=int.from_bytes(header[24:28], 'big'),
                section_headers_sha256=hashlib.sha256(raw).hexdigest(), sections=layout)
        except (AnyPS3Error, UnicodeDecodeError, struct.error):
            continue
    return None


def inventory(bundle, reference):
    bundle = Path(bundle)
    profile_bytes = (bundle / 'profile.json').read_bytes()
    profile = json.loads(profile_bytes)
    program_bytes = (bundle / 'program.elf').read_bytes()
    program = ElfImage.parse(program_bytes)
    names, instructions = opcode_table(reference)
    sources = []
    for image_index, spec in enumerate(profile['embedded_spu_images']):
        payload = program.read_virtual(spec['address'], spec['size'])
        if hashlib.sha256(payload).hexdigest() != spec['sha256']:
            raise ValueError('Original SPU ELF hash mismatch')
        image = ElfImage.parse(payload)
        if (image.machine, image.bits, image.byte_order) != (23, 32, 'big'):
            raise ValueError('Expected original BE ELF32 SPU image')
        for segment in image.segments:
            if segment.type == 1 and segment.flags & 1:
                sources.append(dict(image=image_index, kind='elf',
                    ea=spec['address'] + segment.offset, base=segment.virtual_address,
                    data=payload[segment.offset:segment.offset + segment.file_size],
                    roots=[image.entry], root_evidence='original ELF entry'))
        for index, overlay in enumerate(spec.get('overlays', [])):
            payload = program.read_virtual(overlay['address'], overlay['size'])
            if hashlib.sha256(payload).hexdigest() != overlay['sha256']:
                raise ValueError(f'Original overlay {index} hash mismatch')
            base = overlay['local_address']
            # Candidate starts are deliberately labeled, not asserted as real
            # entries. Header-skipping roots only serve comparative analysis.
            offsets = [0, 0x10, 0x20, 0x30]
            layout = embedded_layout(program, overlay['address'], base)
            roots = [base + off for off in offsets if off + 4 <= len(payload)]
            evidence = 'candidate base,+0x10,+0x20,+0x30; not proven entries'
            if layout:
                roots, evidence = [layout['entry']], 'original embedded ELF entry'
            sources.append(dict(image=image_index, overlay=index, kind='overlay',
                ea=overlay['address'], base=base, data=payload,
                roots=roots, root_evidence=evidence, layout=layout,
                note=overlay.get('note', '')))
    totals, issues, channel_counts = Counter(), Counter(), Counter()
    unique_issues = defaultdict(set)
    records = []
    for source in sources:
        data, base = source.pop('data'), source['base']
        if base % 4 or len(data) % 4:
            raise ValueError('Misaligned SPU source')
        words = {base + off: int.from_bytes(data[off:off + 4], 'big')
                 for off in range(0, len(data), 4)}
        visited, parents, edges, indirect = direct_flow(words, source['roots'], names)
        by_issue, counts, channels = defaultdict(list), Counter(), Counter()
        issue_words, section_issues = defaultdict(Counter), defaultdict(Counter)
        path_examples = {}
        for pc, word in words.items():
            name = names.get(word >> 21, 'UNK')
            counts[name] += 1
            if name in ('RDCH', 'WRCH', 'RCHCNT'):
                channels[f'{name}:{(word >> 7) & 127}'] += 1
            try:
                lift(pc, word)
                reason = 'conditional halt' if name in ('HGT', 'HLGT', 'HEQ', 'HGTI', 'HLGTI', 'HEQI') else None
                if name in ('STOP', 'STOPD'):
                    code = word & 0x3fff if name == 'STOP' else 0x3fff
                    if code not in (1, 2, 0x100, 0x101, 0x102, 0x110, 0x111):
                        reason = f'SPU stop-and-signal service 0x{code:x} is not implemented'
            except AnyPS3Error as error:
                reason = str(error)
            if reason:
                by_issue[reason].append(pc - base)
                unique_issues[reason].add((source['image'], pc, word))
                issue_words[reason][f'0x{word:08x}'] += 1
                section = next((s['name'] for s in (source.get('layout') or {}).get('sections', [])
                    if s['address'] <= pc < s['address'] + s['size']), 'no section evidence')
                section_issues[reason][section] += 1
                if pc in visited and reason not in path_examples:
                    path, seen, cursor = [], set(), pc
                    while cursor not in seen:
                        path.append(hex(cursor)); seen.add(cursor)
                        if cursor in source['roots'] or cursor not in parents:
                            break
                        cursor = parents[cursor]
                    path_examples[reason] = list(reversed(path))
        totals.update(counts); channel_counts.update(channels)
        issue_records = {}
        for reason, offsets in sorted(by_issue.items()):
            reached = [off for off in offsets if base + off in visited]
            issues[reason] += len(offsets)
            issue_records[reason] = dict(count=len(offsets), offsets=spans(offsets),
                words=dict(issue_words[reason]), sections=dict(section_issues[reason]),
                direct_candidate_paths=spans(reached), example_path=path_examples.get(reason))
        records.append(dict(**source, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            instruction_counts=dict(sorted(counts.items())), channels=dict(sorted(channels.items())),
            direct_visited=len(visited), indirect_sites=[hex(x) for x in indirect],
            external_edges=[[hex(a), hex(b)] for a, b in edges], issues=issue_records))
    coverage = []
    for name, magnitude, value in instructions:
        word = value << (21 + magnitude)
        if name in ('CFLTS', 'CFLTU'):
            word |= 173 << 14
        if name in ('CSFLT', 'CUFLT'):
            word |= 155 << 14
        try:
            lift(0x100, word)
            state = 'lifted'
        except AnyPS3Error as error:
            state = str(error)
        coverage.append(dict(name=name, word=hex(word), state=state))
    return dict(bundle=str(bundle), profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        program_sha256=hashlib.sha256(program_bytes).hexdigest(),
        decoder_sha256=hashlib.sha256(Path(__file__).with_name('spu.py').read_bytes()).hexdigest(),
        reference_sha256=hashlib.sha256(Path(reference).read_bytes()).hexdigest(),
        limitation='All words scanned; direct candidate paths are overapproximations. '
                   'Unvisited is unresolved, not proven data/unreachable. Indirect edges and '
                   'DMA transitions are not resolved; no execution or menu evidence.',
        words=sum(totals.values()), counts=dict(sorted(totals.items())),
        issues=dict(sorted(issues.items())), channels=dict(sorted(channel_counts.items())),
        unique_issues={k: len(v) for k, v in sorted(unique_issues.items())},
        isa_coverage=coverage, sources=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--reference', default='local/reference/rpcs3/rpcs3/Emu/Cell/SPUOpcodes.h')
    args = parser.parse_args()
    print(json.dumps(inventory(args.bundle, args.reference), indent=2))


if __name__ == '__main__':
    main()
