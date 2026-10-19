"""Reconstruct an analysis ELF from verified APP SELF load segments.

Format references: RPCS3 Crypto/unself.{h,cpp}. No key material is bundled.
Unmapped original ELF bytes are unavailable; this is not a byte-exact ELF dump.
"""

import hashlib
import hmac
import json
from pathlib import Path
import struct
import zlib

from .elf import ElfImage
from .errors import AnyPS3Error


def span(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise AnyPS3Error(f"{label}: range exceeds input size.")
    return data[offset:offset + size]


def unpack_self(data: bytes, key_file: Path, *, allow_prx: bool = False) -> tuple[bytes, dict]:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise AnyPS3Error("SELF extraction requires the optional cryptography package.") from exc
    sce = struct.unpack(">IIHHIQQ", span(data, 0, 32, "SCE header"))
    magic, version, revision, kind, meta_offset, header_size, original_size = sce
    if magic != 0x53434500 or version != 2 or kind != 1:
        raise AnyPS3Error("Expected a version 2 SELF container.")
    if revision & 0x8000:
        raise AnyPS3Error("Debug/fSELF is not handled by this retail APP extractor.")
    ext = struct.unpack(">10Q", span(data, 32, 80, "SELF extended header"))
    _, appoff, elfoff, phoff, shoff, _, _, controloff, controlsize, _ = ext
    _, _, app_type, _, _ = struct.unpack(">QIIQQ", span(data, appoff, 32, "APP header"))
    if app_type != 4:
        raise AnyPS3Error("Only APP SELF is supported; NPDRM and other SELF categories are unsupported.")
    control = span(data, controloff, controlsize, "SELF control info")
    pos = 0
    original_sha1 = None
    while pos < len(control):
        ctype, size, _ = struct.unpack(">IIQ", span(control, pos, 16, "Control header"))
        if size < 16:
            raise AnyPS3Error("Invalid SELF control record size.")
        block = span(control, pos, size, "Control record")
        if ctype == 3:
            raise AnyPS3Error("NPDRM control info is not supported by this APP extractor.")
        if ctype == 2 and size == 64:
            original_sha1 = block[36:56].hex()
        pos += size
    try:
        keyspec = json.loads(key_file.read_text(encoding="utf-8"))
        if keyspec["revision"] != revision or keyspec["type"] != app_type:
            raise AnyPS3Error("Key file revision/type does not match the SELF header.")
        root_key, root_iv = bytes.fromhex(keyspec["erk"]), bytes.fromhex(keyspec["riv"])
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise AnyPS3Error("Invalid SELF key file; expected type, revision, erk and riv.") from exc
    if len(root_key) != 32 or len(root_iv) != 16:
        raise AnyPS3Error("SELF key file requires a 32-byte ERK and 16-byte RIV.")

    def decrypt(payload, key, mode):
        worker = Cipher(algorithms.AES(key), mode).decryptor()
        return worker.update(payload) + worker.finalize()

    info_offset = 32 + meta_offset
    if header_size < info_offset + 96:
        raise AnyPS3Error("Invalid SELF metadata/header size.")
    info = decrypt(span(data, info_offset, 64, "Metadata info"), root_key, modes.CBC(root_iv))
    if info[16:32] != bytes(16) or info[48:64] != bytes(16):
        raise AnyPS3Error("SELF metadata padding check failed (wrong key or damaged input).")
    metadata = decrypt(span(data, info_offset + 64, header_size - info_offset - 64, "Metadata"),
                       info[:16], modes.CTR(info[32:48]))
    _, _, count, key_count, _, _, _ = struct.unpack(">Q6I", span(metadata, 0, 32, "Metadata header"))
    table = span(metadata, 32, 48 * count, "Metadata section table")
    keys = span(metadata, 32 + 48 * count, 16 * key_count, "Metadata key table")
    header = span(data, elfoff, 64, "Embedded ELF header")
    if header[:6] != b"\x7fELF\x02\x02":
        raise AnyPS3Error("Only big-endian ELF64 SELF payloads are supported.")
    fields = struct.unpack_from(">HHIQQQIHHHHHH", header, 16)
    out_phoff, out_shoff = fields[4:6]
    phsize, phcount, shsize, shcount = fields[8:12]
    if phsize != 56 or (shcount and shsize != 64) or phcount == 65535:
        raise AnyPS3Error("Unsupported embedded ELF table sizes.")
    phbytes = span(data, phoff, phsize * phcount, "Embedded program headers")
    if not 64 <= original_size <= 512 * 1024 * 1024:
        raise AnyPS3Error("Declared ELF size is outside the supported 64-byte to 512-MiB range.")
    output = bytearray(original_size)

    def put(offset, payload):
        if offset < 0 or offset + len(payload) > original_size:
            raise AnyPS3Error("Reconstructed ELF range exceeds declared size.")
        output[offset:offset + len(payload)] = payload

    reports, covered = [], []
    verified_shdr = not shcount
    seen_ph = set()
    for i in range(count):
        off, size, stype, index, hashed, hi, enc, ki, vi, compression = struct.unpack_from(">QQ8I", table, 48 * i)
        record = {"index": i, "type": stype, "size": size, "hmac_verified": False, "included": False}
        # Type 3 contains auxiliary build information, not a loadable ELF segment.
        if stype not in (1, 2):
            record["note"] = "Auxiliary SELF section omitted; not verified or mapped into ELF."
            reports.append(record)
            continue
        payload = span(data, off, size, f"SELF section {i}")
        if enc == 3:
            key = span(keys, ki * 16, 16, "Section AES key")
            iv = span(keys, vi * 16, 16, "Section AES IV")
            payload = decrypt(payload, key, modes.CTR(iv))
        elif enc != 1:
            raise AnyPS3Error(f"Unsupported encryption flag {enc} in section {i}.")
        if hashed != 2:
            raise AnyPS3Error(f"Section {i} does not carry a supported HMAC.")
        expected = span(keys, hi * 16, 20, "Section digest")
        hkey = span(keys, hi * 16 + 32, 64, "Section HMAC key")
        if not hmac.compare_digest(hmac.new(hkey, payload, "sha1").digest(), expected):
            raise AnyPS3Error(f"SELF section {i} HMAC failed; output refused.")
        record["hmac_verified"] = True
        if stype == 2:
            if index >= phcount or index in seen_ph:
                raise AnyPS3Error("Invalid or duplicate SELF program-header index.")
            seen_ph.add(index)
            ph = struct.unpack_from(">II6Q", phbytes, index * 56)
            target, expected_size = ph[2], ph[5]
        else:
            target, expected_size = out_shoff, shcount * shsize
        if expected_size > original_size:
            raise AnyPS3Error("Section expansion exceeds declared ELF size.")
        if compression == 2:
            decoder = zlib.decompressobj()
            try:
                payload = decoder.decompress(payload, expected_size + 1)
            except zlib.error as exc:
                raise AnyPS3Error(f"Invalid compressed SELF section {i}.") from exc
            if not decoder.eof or decoder.unused_data:
                raise AnyPS3Error("Incomplete or oversized compressed SELF section.")
        elif compression != 1:
            raise AnyPS3Error(f"Unsupported compression flag {compression}.")
        if len(payload) != expected_size:
            raise AnyPS3Error(f"SELF section {i} size differs from the ELF table.")
        if stype == 1:
            if payload != span(data, shoff, expected_size, "Section headers"):
                raise AnyPS3Error("SELF section-header copies disagree.")
            verified_shdr = True
        put(target, payload)
        if stype == 2 and payload:
            covered.append((target, target + len(payload)))
        record["included"] = True
        reports.append(record)
    if not verified_shdr:
        raise AnyPS3Error("No verified ELF section-header table found.")
    for i in range(phcount):
        ph = struct.unpack_from(">II6Q", phbytes, i * 56)
        if ph[5] and not any(lo <= ph[2] and ph[2] + ph[5] <= hi for lo, hi in covered):
            raise AnyPS3Error(f"Program segment {i} is not covered by verified payload data.")
    # Headers copied from SELF are needed to describe the loadable image.
    put(0, header)
    put(out_phoff, phbytes)
    result = bytes(output)
    image = ElfImage.parse(result)
    if allow_prx and image.type == 0xffa4:
        if (image.bits, image.byte_order, image.machine) != (64, 'big', 21):
            raise AnyPS3Error('PRX extraction requires big-endian ELF64 PowerPC64.')
    else:
        image.require_ppu_executable()
    return result, {
        "schema_version": 1, "self_sha256": hashlib.sha256(data).hexdigest(),
        "analysis_elf_sha256": hashlib.sha256(result).hexdigest(),
        "original_elf_sha1": original_sha1,
        "original_elf_digest_matches": hashlib.sha1(result).hexdigest() == original_sha1,
        "sections": reports, "ecdsa_signature_verified": False,
        "elf_type": image.type, "requires_relocation": image.type == 0xffa4,
        "note": "Analysis ELF: verified load segments and section headers; unrepresented ELF bytes are zero-filled. "
                "Not a byte-exact reconstruction of the original ELF; auxiliary SELF data is omitted.",
    }
