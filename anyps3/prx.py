"""Offline relocation and linkage inventory for PPU PRX modules.

Produces an analysis ET_EXEC with independently placed load segments. It does
not initialize libraries, resolve imported symbols, or load modules at runtime.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import struct

from .analysis import cstring
from .elf import ElfImage
from .errors import AnyPS3Error


@dataclass(frozen=True)
class RelocatedModule:
    image: ElfImage
    report: dict


def make_image(segments: list[tuple[int, int, bytes]], entry: int = 0) -> ElfImage:
    """Construct a sectionless ELF64 with explicit address/flags/payload tuples."""
    output = bytearray((64 + 56 * len(segments) + 15) & ~15)
    output[:16] = b'\x7fELF\x02\x02\x01\x66' + bytes(8)
    struct.pack_into('>HHIQQQIHHHHHH', output, 16, 2, 21, 1, entry, 64, 0, 0, 64, 56, len(segments), 0, 0, 0)
    for i, (address, flags, payload) in enumerate(segments):
        output.extend(bytes((-len(output)) & 15))
        offset = len(output)
        output.extend(payload)
        struct.pack_into('>II6Q', output, 64 + i * 56, 1, flags & 7, offset, address, 0, len(payload), len(payload), 16)
    return ElfImage.parse(bytes(output))


def linkage(image: ElfImage, start: int, end: int, *, exports: bool) -> list[dict]:
    if end < start or end - start > 1024 * 1024:
        raise AnyPS3Error('Invalid PRX linkage table range.')
    result = []
    while start < end:
        raw = image.read_virtual(start, 28)
        size = raw[0] or 44
        if size < 28 or start + size > end:
            raise AnyPS3Error('Invalid PRX linkage record size.')
        image.read_virtual(start, size)
        version, attributes, count, variables, tls = struct.unpack_from('>5H', raw, 2)
        name, nids, addresses = struct.unpack_from('>3I', raw, 16)
        entries = []
        for i in range(count):
            nid = int.from_bytes(image.read_virtual(nids + i * 4, 4), 'big')
            address = int.from_bytes(image.read_virtual(addresses + i * 4, 4), 'big')
            item = {'nid': f'0x{nid:08x}', 'descriptor' if exports else 'stub': address}
            if exports:
                code, toc = image.resolve_entry(address=address)
                item.update(code=code, toc=toc)
            else:
                image.read_virtual(address, 4, executable=True)
                # Attribute 0x2000: a second NID-sized array follows the NIDs and
                # points at (type, address, addend) lists that the loader patches
                # with the import's linked descriptor address.
                if attributes & 0x2000:
                    refs = int.from_bytes(image.read_virtual(nids + (count + i) * 4, 4), 'big')
                    if refs:
                        item['references'] = import_references(image, refs)
            entries.append(item)
        result.append({'library': cstring(image, name) if name else '', 'descriptor_address': start,
                       'version': version, 'attributes': attributes, 'function_count': count,
                       'variable_count': variables, 'tls_variable_count': tls, 'functions': entries})
        start += size
    return result


def import_references(image: ElfImage, start: int) -> list[dict]:
    result = []
    for i in range(4097):
        kind, address, addend = struct.unpack('>3I', image.read_virtual(start + i * 12, 12))
        if not kind:
            return result
        # Only the absolute 32-bit form is verified against original modules.
        if kind != 1:
            raise AnyPS3Error(f'Unsupported PRX import reference type {kind}.')
        if address % 4:
            raise AnyPS3Error('Unaligned PRX import reference.')
        image.read_virtual(address, 4)
        result.append({'type': kind, 'address': address, 'addend': addend})
    raise AnyPS3Error('PRX import reference list is unterminated.')


def link_references(module: RelocatedModule, bindings: dict) -> tuple[ElfImage, list[dict]]:
    """Patch import references with linked export descriptors, as the loader does."""
    writes, linked = {}, []
    for library in module.report['imports']:
        for function in library['functions']:
            if not function.get('references'):
                continue
            key = (library['library'], function['nid'])
            if key not in bindings:
                raise AnyPS3Error(f'Unlinked PRX import reference {key[0]} NID={key[1]}.')
            export = bindings[key][1]
            for ref in function['references']:
                value = export['descriptor'] + ref['addend']
                if value > 0xffffffff or ref['address'] in writes:
                    raise AnyPS3Error('Invalid PRX import reference target.')
                writes[ref['address']] = value
                linked.append({'library': key[0], 'nid': key[1], 'address': ref['address'],
                               'value': value, 'code': export['code']})
    if not writes:
        return module.image, linked
    segments = []
    for s in module.image.segments:
        payload = bytearray(module.image.data[s.offset:s.offset+s.file_size])
        for address, value in writes.items():
            if s.virtual_address <= address < s.virtual_address + s.file_size:
                payload[address-s.virtual_address:address-s.virtual_address+4] = value.to_bytes(4, 'big')
        segments.append((s.virtual_address, s.flags, bytes(payload)))
    return make_image(segments, module.image.entry), linked


def relocate(image: ElfImage, *, base: int = 0x20000000) -> RelocatedModule:
    if (image.bits, image.byte_order, image.machine, image.type) != (64, 'big', 21, 0xffa4):
        raise AnyPS3Error('Expected big-endian ELF64 PPU PRX (type 0xffa4).')
    loads = [s for s in image.segments if s.type == 1]
    if not loads or loads[0].index != 0 or len(loads) > 16:
        raise AnyPS3Error('Unsupported PRX load segment arrangement.')
    if any(s.type not in (1, 0x700000a4) for s in image.segments):
        raise AnyPS3Error('Unsupported additional PRX segment type.')
    if base < 0x10000 or base & 0xffff or sum(s.memory_size for s in loads) > 64 * 1024 * 1024:
        raise AnyPS3Error('Invalid PRX allocation base/size.')
    bases, buffers = {}, {}
    cursor = base
    for s in loads:
        if not s.memory_size or cursor + s.memory_size > 0xc0000000:
            raise AnyPS3Error('Invalid PRX allocation range.')
        bases[s.index] = cursor
        buffers[s.index] = bytearray(image.data[s.offset:s.offset+s.file_size]) + bytearray(s.memory_size-s.file_size)
        cursor = (cursor + s.memory_size + 0xffff) & ~0xffff
    counts = Counter()
    for segment in image.segments:
        if segment.type != 0x700000a4:
            continue
        if segment.file_size % 24:
            raise AnyPS3Error('Truncated PRX relocation table.')
        for pos in range(segment.offset, segment.offset + segment.file_size, 24):
            offset, reserved, value_index, address_index, kind, addend = struct.unpack_from('>QHBBIQ', image.data, pos)
            if reserved or address_index not in bases or (value_index != 255 and value_index not in bases):
                raise AnyPS3Error('Invalid PRX relocation segment index/reserved field.')
            widths = {1:4, 4:2, 5:2, 6:2, 10:4, 11:4, 38:8, 44:8, 57:2}
            if kind not in widths:
                raise AnyPS3Error(f'Unsupported PRX relocation type {kind}.')
            width = widths[kind]
            destination = buffers[address_index]
            if offset + width > len(destination) or offset % width:
                raise AnyPS3Error('PRX relocation destination is invalid.')
            address = bases[address_index] + offset
            value = (0 if value_index == 255 else bases[value_index]) + addend
            previous = int.from_bytes(destination[offset:offset+width], 'big')
            if kind == 5:
                value >>= 16
            elif kind == 6:
                value = (value + 0x8000) >> 16
            elif kind in (10, 11):
                delta = value - address
                bits = 26 if kind == 10 else 16
                if delta & 3 or not -(1 << (bits-1)) <= delta < (1 << (bits-1)):
                    raise AnyPS3Error('PRX branch relocation out of range.')
                mask = 0x3fffffc if kind == 10 else 0xfffc
                value = (previous & ~mask) | (delta & mask)
            elif kind == 44:
                value -= address
            elif kind == 57:
                if value & 3:
                    raise AnyPS3Error('Unaligned PRX DS relocation.')
                value = (previous & 3) | (value & 0xfffc)
            elif kind == 1 and value > 0xffffffff:
                raise AnyPS3Error('PRX 32-bit address overflow.')
            destination[offset:offset+width] = (value & ((1 << (width*8))-1)).to_bytes(width, 'big')
            counts[kind] += 1
    mapped = [(bases[s.index], s.flags, bytes(buffers[s.index])) for s in loads]
    relocated = make_image(mapped)
    phoff = struct.unpack_from('>Q', image.data, 32)[0]
    physical = struct.unpack_from('>Q', image.data, phoff + 24)[0]
    if not loads[0].offset <= physical < loads[0].offset + loads[0].file_size:
        raise AnyPS3Error('PRX module information is outside the first file segment.')
    info = bases[0] + physical - loads[0].offset
    raw = relocated.read_virtual(info, 52)
    name = raw[4:32].split(b'\0',1)[0].decode('utf-8')
    toc, export_start, export_end, import_start, import_end = struct.unpack_from('>5I', raw, 32)
    exports = linkage(relocated, export_start, export_end, exports=True)
    imports = linkage(relocated, import_start, import_end, exports=False)
    specials = {f['nid']:f for library in exports if library['attributes'] & 0x8000
                for f in library['functions']}
    entry = specials.get('0xbc9a0086', {}).get('descriptor', 0)
    relocated = make_image(mapped, entry)
    constructors = constructor_entries(relocated, toc)
    return RelocatedModule(relocated, {'schema_version': 1, 'source_sha256': hashlib.sha256(image.data).hexdigest(),
        'relocated_sha256': hashlib.sha256(relocated.data).hexdigest(), 'name': name, 'toc': toc,
        'base': base, 'next_base': cursor, 'segment_bases': bases, 'relocation_counts': dict(counts),
        'exports': exports, 'imports': imports, 'special_exports': specials,
        'static_constructor_entries': constructors,
        'note': 'Offline relocation only. Imports are unresolved and module initialization has not run.'})


def constructor_entries(relocated: ElfImage, toc: int) -> list[int]:
    constructors = []
    # GCC's reverse constructor walker uses a module-specific TOC slot.
    # Recognize its instruction sequence, including the -4 cursor step;
    # then validate all OPDs rather than guessing from arbitrary pointers.
    slots = set()
    tail = bytes.fromhex('7c0802a6f821ff81fbe100783be9fffc')
    for _, flags, payload in [(s.virtual_address,s.flags,relocated.read_virtual(s.virtual_address,s.file_size))
                              for s in relocated.segments if s.type==1 and s.file_size]:
        if not flags & 1:
            continue
        for at in range(0, len(payload)-19, 4):
            word = int.from_bytes(payload[at:at+4], 'big')
            if word >> 16 == 0x8122 and payload[at+4:at+20] == tail:
                displacement = (word & 0xffff) - (0x10000 if word & 0x8000 else 0)
                slots.add(toc + displacement)
    for slot in sorted(slots):
        end = int.from_bytes(relocated.read_virtual(slot,4),'big')
        for i in range(1,1025):
            descriptor = int.from_bytes(relocated.read_virtual(end - i*4,4),'big')
            if descriptor == 0xffffffff:
                break
            code, function_toc = relocated.resolve_entry(address=descriptor)
            if function_toc != toc:
                raise AnyPS3Error('Unexpected PRX constructor TOC.')
            constructors.append(code)
        else:
            raise AnyPS3Error('PRX constructor table is unterminated.')
    return constructors
