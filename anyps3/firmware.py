"""Offline PUP/firmware package extraction with explicit external key material.

Container layout reference: RPCS3 Loader/PUP.h and Crypto/unself.h.
All included payload HMACs are checked; ECDSA signatures are not checked.
This module never installs firmware into the host or executes its code.
"""

import argparse
import hashlib
import hmac
import io
import json
from pathlib import Path, PurePosixPath
import struct
import tarfile
import zlib

from .errors import AnyPS3Error
from .self_file import span

LIMIT = 512 * 1024 * 1024


def pup_entries(data: bytes, key: bytes) -> dict[int, bytes]:
    magic, version, _, count, header, size = struct.unpack('>8s5Q', span(data, 0, 48, 'PUP header'))
    if magic != b'SCEUF\0\0\0' or version != 1 or not 0 < count <= 256:
        raise AnyPS3Error('Unsupported PUP header.')
    if not 48 + count * 64 <= header <= len(data) or size > len(data) - header:
        raise AnyPS3Error('Invalid PUP table/data sizes.')
    result, ranges = {}, []
    for i in range(count):
        ident, offset, length = struct.unpack_from('>3Q', data, 48 + i * 32)
        hash_id, digest = struct.unpack_from('>Q20s', data, 48 + count * 32 + i * 32)
        if ident in result or hash_id != i or offset < header or length > LIMIT or offset + length > header + size:
            raise AnyPS3Error('Invalid PUP entry or hash index.')
        if any(offset < end and begin < offset + length for begin, end in ranges):
            raise AnyPS3Error('Overlapping PUP entries.')
        payload = span(data, offset, length, 'PUP entry')
        if not hmac.compare_digest(hmac.new(key, payload, 'sha1').digest(), digest):
            raise AnyPS3Error(f'PUP entry {ident:x} HMAC failed.')
        result[ident] = payload
        ranges.append((offset, offset + length))
    return result


def package_sections(data: bytes, key: bytes, iv: bytes) -> list[bytes]:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def decrypt(payload, cipher_key, mode):
        worker = Cipher(algorithms.AES(cipher_key), mode).decryptor()
        return worker.update(payload) + worker.finalize()

    magic, version, revision, kind, meta, header, _ = struct.unpack('>IIHHIQQ', span(data, 0, 32, 'SCE header'))
    if magic != 0x53434500 or version != 2 or kind != 3 or revision & 0x8000:
        raise AnyPS3Error('Expected retail SCE firmware package.')
    info_offset = meta + 32
    if not info_offset + 96 <= header <= len(data):
        raise AnyPS3Error('Invalid SCE metadata sizes.')
    info = decrypt(span(data, info_offset, 64, 'Metadata info'), key, modes.CBC(iv))
    if info[16:32] != bytes(16) or info[48:64] != bytes(16):
        raise AnyPS3Error('Firmware metadata padding failed.')
    metadata = decrypt(data[info_offset + 64:header], info[:16], modes.CTR(info[32:48]))
    _, _, count, key_count, _, _, _ = struct.unpack('>Q6I', span(metadata, 0, 32, 'Metadata header'))
    if not 0 < count <= 256:
        raise AnyPS3Error('Invalid firmware section count.')
    table = span(metadata, 32, 48 * count, 'Section table')
    keys = span(metadata, 32 + 48 * count, 16 * key_count, 'Section keys')
    sections = []
    for i in range(count):
        offset, size, _, _, hashed, hi, enc, ki, vi, compression = struct.unpack_from('>QQ8I', table, i * 48)
        if offset < header or size > LIMIT:
            raise AnyPS3Error('Invalid firmware section range.')
        payload = span(data, offset, size, 'Firmware section')
        if enc == 3:
            payload = decrypt(payload, span(keys, ki * 16, 16, 'AES key'), modes.CTR(span(keys, vi * 16, 16, 'AES IV')))
        elif enc != 1:
            raise AnyPS3Error('Unsupported firmware encryption.')
        if hashed != 2:
            raise AnyPS3Error('Firmware section lacks HMAC.')
        expected = span(keys, hi * 16, 20, 'Digest')
        hkey = span(keys, hi * 16 + 32, 64, 'HMAC key')
        if not hmac.compare_digest(hmac.new(hkey, payload, 'sha1').digest(), expected):
            raise AnyPS3Error(f'Firmware section {i} HMAC failed.')
        if compression == 2:
            decoder = zlib.decompressobj()
            payload = decoder.decompress(payload, LIMIT + 1)
            if len(payload) > LIMIT or not decoder.eof or decoder.unused_data:
                raise AnyPS3Error('Invalid or oversized firmware compression.')
        elif compression != 1:
            raise AnyPS3Error('Unsupported firmware compression.')
        sections.append(payload)
        if sum(map(len, sections)) > LIMIT:
            raise AnyPS3Error('Expanded firmware package exceeds limit.')
    return sections


def tar_files(data: bytes) -> dict[str, bytes]:
    result, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for entry in archive:
            parts = entry.name.split('/')
            if entry.name.startswith('/') or '\\' in entry.name or any(p in ('.', '..') for p in parts):
                raise AnyPS3Error('Unsafe firmware archive path.')
            if entry.isdir():
                continue
            if not entry.isfile() or entry.name in result or not all(parts):
                raise AnyPS3Error('Unsupported/duplicate firmware archive member.')
            total += entry.size
            if entry.size < 0 or total > LIMIT or len(result) >= 10000:
                raise AnyPS3Error('Firmware archive exceeds extraction limits.')
            stream = archive.extractfile(entry)
            assert stream is not None
            payload = stream.read(entry.size + 1)
            if len(payload) != entry.size:
                raise AnyPS3Error('Truncated firmware archive member.')
            result[entry.name] = payload
    return result


def extract(pup: Path, output: Path, key_file: Path) -> dict:
    if pup.stat().st_size > LIMIT:
        raise AnyPS3Error('PUP exceeds supported size limit.')
    spec = json.loads(key_file.read_text())
    key, iv, hkey = (bytes.fromhex(spec[n]) for n in ('erk', 'riv', 'pup_hmac'))
    if (len(key), len(iv), len(hkey)) != (32, 16, 64):
        raise AnyPS3Error('Firmware key file has invalid key lengths.')
    data = pup.read_bytes()
    entries = pup_entries(data, hkey)
    if 0x100 not in entries or 0x300 not in entries:
        raise AnyPS3Error('PUP lacks version/update archive.')
    packages = tar_files(entries[0x300])
    output.mkdir(parents=True, exist_ok=False)
    records, total = [], 0
    for name, payload in packages.items():
        if not name.startswith('dev_flash_'):
            continue
        sections = package_sections(payload, key, iv)
        if len(sections) != 3:
            raise AnyPS3Error('Expected three sections in firmware archive package.')
        for path, content in tar_files(sections[2]).items():
            if PurePosixPath(path).parts[0] != 'dev_flash':
                raise AnyPS3Error('Firmware member is outside dev_flash.')
            total += len(content)
            if total > 1024 * 1024 * 1024 or len(records) >= 10000:
                raise AnyPS3Error('Total firmware extraction exceeds limits.')
            target = output / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as handle:
                handle.write(content)
            records.append({'path': path, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'package': name})
    if not records:
        raise AnyPS3Error('No firmware files extracted.')
    report = {'schema_version': 1, 'pup_sha256': hashlib.sha256(data).hexdigest(),
              'version': entries[0x100].decode('ascii').strip(), 'pup_hmac_verified': True,
              'extracted_section_hmacs_verified': True, 'ecdsa_signature_verified': False,
              'files': records, 'file_count': len(records)}
    (output / 'extraction.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pup', type=Path)
    parser.add_argument('--key-file', required=True, type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = extract(args.pup, args.output, args.key_file)
    except (AnyPS3Error, OSError, ValueError, KeyError, TypeError, ImportError, tarfile.TarError, zlib.error) as exc:
        parser.exit(1, f'Firmware extraction failed (partial output may remain): {exc}\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'files'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
