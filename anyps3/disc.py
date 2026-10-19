"""Bounded ISO9660 inventory and extraction, including explicit partial images."""

import hashlib
import json
from pathlib import Path
import struct

from .errors import AnyPS3Error

SECTOR = 2048


def both(data: bytes, offset: int, width: int) -> int:
    left = int.from_bytes(data[offset:offset + width], "little")
    right = int.from_bytes(data[offset + width:offset + 2 * width], "big")
    if left != right:
        raise AnyPS3Error("ISO9660 both-endian field mismatch.")
    return left


def inventory(path: Path) -> dict:
    available = path.stat().st_size
    with path.open("rb") as stream:
        def read(offset, size):
            if size > 64 * 1024 * 1024 or offset + size > available:
                raise AnyPS3Error("ISO directory/descriptor is outside the available image or too large.")
            stream.seek(offset)
            data = stream.read(size)
            if len(data) != size:
                raise AnyPS3Error("ISO input changed or is truncated.")
            return data

        pvd = None
        for sector in range(16, 80):
            descriptor = read(sector * SECTOR, SECTOR)
            if descriptor[1:7] != b"CD001\x01":
                raise AnyPS3Error("Expected an ISO9660 volume descriptor.")
            if descriptor[0] == 1:
                pvd = descriptor
                break
            if descriptor[0] == 255:
                break
        if pvd is None or both(pvd, 128, 2) != SECTOR:
            raise AnyPS3Error("Only ISO9660 images with 2048-byte sectors are supported.")
        declared = both(pvd, 80, 4) * SECTOR
        entries, seen, paths = [], set(), set()

        def walk(lba, size, parent, depth):
            if depth > 64 or len(entries) > 200000:
                raise AnyPS3Error("ISO directory traversal limit exceeded.")
            if (lba, size) in seen:
                raise AnyPS3Error("Repeated/cyclic ISO directory extent.")
            seen.add((lba, size))
            if lba * SECTOR + size > declared:
                raise AnyPS3Error("ISO directory extends beyond the declared volume.")
            raw = read(lba * SECTOR, size)
            offset = 0
            pending = None
            while offset < len(raw):
                length = raw[offset]
                if not length:
                    offset = (offset // SECTOR + 1) * SECTOR
                    continue
                if length < 34 or offset + length > len(raw) or offset % SECTOR + length > SECTOR:
                    raise AnyPS3Error("Invalid ISO directory record length.")
                record = raw[offset:offset + length]
                offset += length
                if record[1] or record[26] or record[27]:
                    raise AnyPS3Error("ISO extended attributes and interleaved files are unsupported.")
                name_length = record[32]
                if 33 + name_length > len(record):
                    raise AnyPS3Error("Truncated ISO filename.")
                identifier = record[33:33 + name_length]
                if identifier in (b"\0", b"\1"):
                    continue
                try:
                    name = identifier.decode("ascii").removesuffix(";1")
                except UnicodeError as exc:
                    raise AnyPS3Error("Non-ASCII primary ISO filename is unsupported.") from exc
                if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\0")):
                    raise AnyPS3Error("Unsafe ISO filename.")
                member = parent + name
                flags = record[25]
                block, length = both(record, 2, 4), both(record, 10, 4)
                if block * SECTOR + length > declared:
                    raise AnyPS3Error("ISO file extends beyond declared volume.")
                extent = {"lba": block, "size": length}
                if pending is not None:
                    if member != pending["path"] or flags & 2:
                        raise AnyPS3Error("Broken multi-extent ISO file sequence.")
                    pending["extents"].append(extent)
                    pending["size"] += length
                    pending["available"] &= block * SECTOR + length <= available
                    if not flags & 0x80:
                        pending = None
                    continue
                if member in paths:
                    raise AnyPS3Error("Duplicate ISO path.")
                paths.add(member)
                entry = {"path": member, "lba": block, "size": length, "directory": bool(flags & 2),
                         "available": block * SECTOR + length <= available, "extents": [extent]}
                entries.append(entry)
                if flags & 2:
                    if flags & 0x80:
                        raise AnyPS3Error("Multi-extent directories are unsupported.")
                    walk(block, length, member + "/", depth + 1)
                elif flags & 0x80:
                    pending = entry
            if pending is not None:
                raise AnyPS3Error("Unterminated multi-extent ISO file.")

        walk(both(pvd, 158, 4), both(pvd, 166, 4), "", 0)
    files = [e for e in entries if not e["directory"]]
    return {"schema_version": 1, "volume": pvd[40:72].decode("ascii", "replace").strip(),
            "available_bytes": available, "declared_bytes": declared,
            "image_complete_by_size": available >= declared, "file_count": len(files),
            "available_file_count": sum(e["available"] for e in files), "entries": entries,
            "note": "Availability is a byte-range check, not a checksum verification or decryption test."}


def extract(path: Path, destination: Path, *, allow_partial: bool = False) -> dict:
    report = inventory(path)
    if not report["image_complete_by_size"] and not allow_partial:
        raise AnyPS3Error("ISO image is partial; use --allow-partial to extract only complete file ranges.")
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    extracted, missing = [], []
    with path.open("rb") as stream:
        for entry in report["entries"]:
            if entry["directory"]:
                continue
            if not entry["available"]:
                missing.append(entry["path"])
                continue
            target = destination / entry["path"]
            if not target.resolve().is_relative_to(root):
                raise AnyPS3Error("Extraction path escapes the destination.")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with target.open("xb") as output:
                for extent in entry["extents"]:
                    stream.seek(extent["lba"] * SECTOR)
                    remaining = extent["size"]
                    while remaining:
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise AnyPS3Error("ISO became truncated during extraction.")
                        output.write(block)
                        digest.update(block)
                        remaining -= len(block)
            extracted.append({"path": entry["path"], "size": entry["size"], "sha256": digest.hexdigest()})
    summary = {"schema_version": 1, "source": str(path), "image_complete_by_size": report["image_complete_by_size"],
               "extracted_count": len(extracted), "missing_count": len(missing),
               "extracted": extracted, "missing": missing,
               "note": "File hashes identify extracted bytes; source archive integrity must be checked separately."}
    with (destination / "anyps3-extraction.json").open("x", encoding="utf-8") as out:
        json.dump(summary, out, indent=2)
        out.write("\n")
    return summary
