"""Original synthetic PS3-layout ELF; no Sony or game code is included."""

import struct

CODE_ADDRESS = 0x10000
DESCRIPTOR_ADDRESS = 0x20000
CODE_OFFSET = 0x100
DESCRIPTOR_OFFSET = 0x200


def make_demo() -> bytes:
    # li r3,40; li r4,2; add r3,r3,r4; blr -> r3=42.
    code = struct.pack(">4I", 0x38600028, 0x38800002, 0x7C632214, 0x4E800020)
    ident = b"\x7fELF" + bytes([2, 2, 1, 102, 0]) + bytes(7)
    header = ident + struct.pack(">HHIQQQIHHHHHH", 2, 21, 1, DESCRIPTOR_ADDRESS,
                                 64, 0, 0, 64, 56, 2, 0, 0, 0)
    code_ph = struct.pack(">IIQQQQQQ", 1, 5, CODE_OFFSET, CODE_ADDRESS, CODE_ADDRESS,
                          len(code), len(code), 0x100)
    data_ph = struct.pack(">IIQQQQQQ", 1, 6, DESCRIPTOR_OFFSET, DESCRIPTOR_ADDRESS,
                          DESCRIPTOR_ADDRESS, 8, 0x100, 0x100)
    result = bytearray(DESCRIPTOR_OFFSET + 8)
    result[:len(header)] = header
    result[64:176] = code_ph + data_ph
    result[CODE_OFFSET:CODE_OFFSET + len(code)] = code
    result[DESCRIPTOR_OFFSET:] = struct.pack(">II", CODE_ADDRESS, DESCRIPTOR_ADDRESS + 0x80)
    return bytes(result)

