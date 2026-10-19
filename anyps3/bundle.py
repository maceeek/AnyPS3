"""Build a single offline execution image with original game bytes and PRXs."""

import struct

from .elf import ElfImage
from .errors import AnyPS3Error


def combine(game: ElfImage, modules: tuple[ElfImage, ...]) -> ElfImage:
    game.require_ppu_executable()
    inputs = (game, *modules)
    segments = [(im, s) for im in inputs for s in im.segments]
    if len(segments) > 1024:
        raise AnyPS3Error('Too many bundle segments.')
    regions = []
    for _, s in segments:
        if s.type != 1 or not s.memory_size:
            continue
        lo, hi = s.virtual_address, s.virtual_address + s.memory_size
        if hi > 1 << 32 or any(lo < b and a < hi for a,b in regions):
            raise AnyPS3Error('Bundle load segments overlap or exceed 32-bit space.')
        regions.append((lo,hi))
    # New container headers live outside the mapped game bytes. Preserve every
    # original PT_LOAD byte, including the game's own embedded ELF header.
    output = bytearray(64 + 56 * len(segments))
    output[:16] = game.data[:16]
    struct.pack_into('>HHIQQQIHHHHHH',output,16,2,21,1,game.entry,64,0,0,64,56,len(segments),0,0,0)
    for i,(im,s) in enumerate(segments):
        alignment=max(1,s.alignment)
        if alignment > 1024 * 1024 or alignment & (alignment-1):
            raise AnyPS3Error('Unsupported bundle segment alignment.')
        output.extend(bytes((s.virtual_address-len(output)) % alignment))
        offset=len(output)
        output.extend(im.data[s.offset:s.offset+s.file_size])
        struct.pack_into('>II6Q',output,64+i*56,s.type,s.flags,offset,s.virtual_address,0,
                         s.file_size,s.memory_size,s.alignment)
    return ElfImage.parse(bytes(output))
