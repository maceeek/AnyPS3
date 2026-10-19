"""Recover the first ISO member from local ZIP headers, without a central directory."""

from pathlib import Path
import struct
import zlib

from .errors import AnyPS3Error


def recover_iso(source: Path, output: Path, *, allow_partial: bool = False) -> dict:
    with source.open("rb") as stream:
        source_size = source.stat().st_size
        while True:
            header = stream.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise AnyPS3Error("No ISO member found in supported ZIP local headers.")
            _, _, flags, method, _, _, crc_expected, compressed, size, nlen, xlen = struct.unpack("<IHHHHHIIIHH", header)
            raw_name, extra = stream.read(nlen), stream.read(xlen)
            if len(raw_name) != nlen or len(extra) != xlen:
                raise AnyPS3Error("Truncated ZIP local header.")
            if flags & (1 | 8 | 64):
                raise AnyPS3Error("Encrypted ZIP members and streaming data descriptors are unsupported.")
            try:
                name = raw_name.decode("utf-8" if flags & 2048 else "cp437")
            except UnicodeError as exc:
                raise AnyPS3Error("Invalid ZIP filename encoding.") from exc
            pos = 0
            while pos < len(extra):
                if pos + 4 > len(extra):
                    raise AnyPS3Error("Truncated ZIP extra field.")
                tag, length = struct.unpack_from("<HH", extra, pos)
                pos += 4
                if pos + length > len(extra):
                    raise AnyPS3Error("Invalid ZIP extra field length.")
                field = extra[pos:pos + length]
                pos += length
                if tag == 1:
                    cursor = 0
                    values = []
                    for value in (size, compressed):
                        if value == 0xFFFFFFFF:
                            if cursor + 8 > len(field):
                                raise AnyPS3Error("Missing ZIP64 size.")
                            value = struct.unpack_from("<Q", field, cursor)[0]
                            cursor += 8
                        values.append(value)
                    size, compressed = values
            if size == 0xFFFFFFFF or compressed == 0xFFFFFFFF:
                raise AnyPS3Error("ZIP64 member has no size extension.")
            offset = stream.tell()
            if name.lower().endswith(".iso"):
                break
            if offset + compressed > source_size:
                raise AnyPS3Error("An earlier ZIP member is truncated before the ISO.")
            stream.seek(compressed, 1)
        if method not in (0, 8):
            raise AnyPS3Error("Only stored and deflated ISO members are supported.")
        available = max(0, min(compressed, source_size - offset))
        if available < compressed and not allow_partial:
            raise AnyPS3Error("ZIP member is incomplete; use --allow-partial for a recovery snapshot.")
        if size > 128 * 1024 ** 3:
            raise AnyPS3Error("ISO exceeds the 128-GiB extraction limit.")
        decoder = zlib.decompressobj(-15) if method == 8 else None
        count, crc, consumed = 0, 0, 0
        with output.open("xb") as destination:
            while consumed < available:
                block = stream.read(min(1024 * 1024, available - consumed))
                if not block:
                    raise AnyPS3Error("ZIP input changed while reading.")
                consumed += len(block)
                while block:
                    try:
                        expanded = decoder.decompress(block, 1024 * 1024) if decoder else block
                    except zlib.error as exc:
                        raise AnyPS3Error("Damaged ZIP deflate stream; recovered output is incomplete.") from exc
                    block = decoder.unconsumed_tail if decoder else b""
                    count += len(expanded)
                    if count > size:
                        raise AnyPS3Error("ZIP member expanded past its declared size.")
                    destination.write(expanded)
                    crc = zlib.crc32(expanded, crc)
        stream_complete = decoder.eof and not decoder.unused_data if decoder else consumed == compressed
        verified = stream_complete and count == size and crc == crc_expected
        if consumed == compressed and not verified:
            raise AnyPS3Error("ZIP member CRC/size/stream verification failed; recovered output must not be trusted.")
        return {"schema_version": 1, "source": str(source), "member": name,
                "data_offset": offset, "expected_compressed_size": compressed,
                "compressed_bytes_read": consumed, "expected_size": size, "recovered_size": count,
                "member_crc_verified": verified, "source_archive_complete": None,
                "note": "Only this member is checked. Partial recovery has no CRC guarantee; "
                        "the source is a size-bounded snapshot and its central directory is not checked."}
