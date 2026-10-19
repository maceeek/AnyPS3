"""PSF 1.01 reader/writer. The PSF tables and integers are little endian.

Guest CellSaveData structures use big endian; that does not change PSF's
on-disk byte order. Binary fields retain their exact bytes and capacities.
"""

import struct

from .errors import AnyPS3Error
from .self_file import span


def parse_sfo(data: bytes) -> dict:
    magic, version, key_offset, value_offset, count = struct.unpack("<5I", span(data, 0, 20, "SFO header"))
    if magic != 0x46535000 or version != 0x101:
        raise AnyPS3Error("Expected PARAM.SFO version 1.01.")
    if not 20 + count * 16 <= key_offset <= value_offset <= len(data):
        raise AnyPS3Error("Invalid SFO table offsets.")
    keys = data[key_offset:value_offset]
    result = {}
    for index in range(count):
        key, fmt, size, capacity, offset = struct.unpack_from("<HHIII", data, 20 + index * 16)
        end = keys.find(b"\0", key)
        if key >= len(keys) or end < 0 or size > capacity:
            raise AnyPS3Error("Invalid SFO key or value size.")
        try:
            name = keys[key:end].decode("utf-8")
            value = span(data, value_offset + offset, capacity, "SFO value")[:size]
            if fmt == 0x404 and size == capacity == 4:
                decoded = struct.unpack("<I", value)[0]
            elif fmt == 0x204:
                if not value.endswith(b"\0"):
                    raise AnyPS3Error("SFO string has no terminator.")
                decoded = value.split(b"\0", 1)[0].decode("utf-8")
            elif fmt == 0x004:
                decoded = {"hex": value.hex()}
            else:
                raise AnyPS3Error(f"Unsupported SFO value format 0x{fmt:x}.")
        except UnicodeError as exc:
            raise AnyPS3Error("Invalid UTF-8 in SFO.") from exc
        if not name or name in result:
            raise AnyPS3Error("Empty or duplicate SFO key.")
        result[name] = decoded
    return result


def encode_sfo(values: dict, *, capacities: dict | None = None) -> bytes:
    """Encode integers, UTF-8 strings, and binary bytes (or parse_sfo hex).

    Capacity is independent of logical length. Savedata callers should pass
    the fixed PS3 capacities: TITLE/SUB_TITLE=128, DETAIL/PARAMS=1024,
    SAVEDATA_DIRECTORY=32, SAVEDATA_LIST_PARAM=8, CATEGORY=4, PARAMS2=12.
    """
    capacities = capacities or {}
    keys, payload, entries = bytearray(), bytearray(), []
    for name, value in sorted(values.items()):
        if not isinstance(name, str) or not name or '\0' in name:
            raise AnyPS3Error('Invalid SFO key.')
        key = name.encode('utf-8') + b'\0'
        if len(keys) > 0xffff:
            raise AnyPS3Error('SFO key table exceeds 16-bit offsets.')
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xffffffff:
            fmt, data = 0x404, struct.pack('<I', value)
        elif isinstance(value, str) and '\0' not in value:
            fmt, data = 0x204, value.encode('utf-8') + b'\0'
        elif isinstance(value, (bytes, bytearray)):
            fmt, data = 4, bytes(value)
        elif isinstance(value, dict) and set(value) == {'hex'}:
            try:
                fmt, data = 4, bytes.fromhex(value['hex'])
            except (ValueError, TypeError) as exc:
                raise AnyPS3Error('Invalid SFO binary hex.') from exc
        else:
            raise AnyPS3Error(f'Invalid SFO value for {name}.')
        cap = capacities.get(name, len(data))
        if not isinstance(cap, int) or cap < len(data) or cap > 16 * 1024 * 1024 or (fmt == 0x404 and cap != 4):
            raise AnyPS3Error(f'Invalid SFO capacity for {name}.')
        entries.append(struct.pack('<HHIII', len(keys), fmt, len(data), cap, len(payload)))
        keys.extend(key)
        payload.extend(data)
        payload.extend(bytes(cap - len(data)))
        payload.extend(bytes(-len(payload) % 4))
    key_at = 20 + 16 * len(entries)
    data_at = (key_at + len(keys) + 3) & ~3
    return (struct.pack('<5I', 0x46535000, 0x101, key_at, data_at, len(entries)) +
            b''.join(entries) + keys + bytes(data_at - key_at - len(keys)) + payload)
