"""Static PPU image survey: sections, OPD entries, imports and embedded SPUs."""

from collections import Counter
import struct

from .elf import ElfImage
from .errors import AnyPS3Error
from .ppu import recompile
from .self_file import span


def sections(image: ElfImage) -> list[dict]:
    image.require_ppu_executable()
    offset = struct.unpack_from(">Q", image.data, 40)[0]
    size, count, names_index = struct.unpack_from(">HHH", image.data, 58)
    if not offset and not count:
        return []
    if size != 64 or not count or names_index == 0xFFFF:
        raise AnyPS3Error("Unsupported ELF section table or extended numbering.")
    table = span(image.data, offset, size * count, "ELF section table")
    result = []
    for i in range(count):
        name, kind, flags, va, file_offset, length, link, info, align, entsize = struct.unpack_from(">II4QII2Q", table, i * 64)
        if kind not in (0, 8):
            span(image.data, file_offset, length, f"ELF section {i}")
        result.append({"index": i, "name_offset": name, "type": kind, "flags": flags,
                       "address": va, "offset": file_offset, "size": length, "entry_size": entsize})
    if names_index >= count:
        raise AnyPS3Error("ELF section name table index is outside the table.")
    names = b""
    if names_index:
        s = result[names_index]
        if s["type"] != 3:
            raise AnyPS3Error("ELF section name table is not STRTAB.")
        names = span(image.data, s["offset"], s["size"], "Section names")
    for s in result:
        n = s.pop("name_offset")
        end = names.find(b"\0", n)
        s["name"] = names[n:end].decode("utf-8", "replace") if n < len(names) and end >= n else None
    return result


def opd_entries(image: ElfImage, table: list[dict]) -> tuple[list[dict], int | None]:
    matches = [s for s in table if s["type"] == 1 and s["flags"] & 2
               and s["address"] <= image.entry < s["address"] + s["size"]]
    if not matches:
        return [], None
    section = min(matches, key=lambda s: s["size"])
    if section["size"] % 8 or section["size"] > 16 * 1024 * 1024:
        raise AnyPS3Error("Entry-containing section is not a supported OPD table.")
    raw = image.read_virtual(section["address"], section["size"])
    result = []
    for offset in range(0, len(raw), 8):
        code, toc = struct.unpack_from(">II", raw, offset)
        if code % 4:
            continue
        try:
            image.read_virtual(code, 4, executable=True)
        except AnyPS3Error:
            continue
        result.append({"descriptor": section["address"] + offset, "code": code, "toc": toc})
    return result, section["index"]


def cstring(image: ElfImage, address: int, limit: int = 256) -> str:
    value = bytearray()
    for index in range(limit):
        byte = image.read_virtual(address + index, 1)
        if byte == b"\0":
            try:
                return value.decode("utf-8")
            except UnicodeError as exc:
                raise AnyPS3Error("Invalid import library name.") from exc
        value.extend(byte)
    raise AnyPS3Error("Import library name is not terminated.")


def imports(image: ElfImage) -> list[dict]:
    params = [s for s in image.segments if s.type == 0x60000002]
    if not params:
        return []
    if len(params) != 1:
        raise AnyPS3Error("Multiple process PRX parameter segments are unsupported.")
    header = image.read_virtual(params[0].virtual_address, 32)
    if struct.unpack_from(">I", header, 4)[0] != 0x1B434CEC:
        raise AnyPS3Error("Invalid process PRX parameter magic.")
    start, end = struct.unpack_from(">II", header, 24)
    if end < start or end - start > 1024 * 1024:
        raise AnyPS3Error("Invalid import library table range.")
    result = []
    address = start
    while address < end:
        if end - address < 28:
            raise AnyPS3Error("Truncated import library record.")
        raw = image.read_virtual(address, 28)
        record_size = raw[0]
        if record_size < 28 or record_size > end - address:
            raise AnyPS3Error("Invalid import library record size.")
        image.read_virtual(address, record_size)
        count = struct.unpack_from(">H", raw, 6)[0]
        variables, tls_variables = struct.unpack_from(">HH", raw, 8)
        name_address, nid_address, stub_address = struct.unpack_from(">III", raw, 16)
        name = cstring(image, name_address)
        if count:
            nids = image.read_virtual(nid_address, count * 4)
            stubs = image.read_virtual(stub_address, count * 4)
        else:
            nids = stubs = b""
        result.append({"library": name, "descriptor_address": address,
                       "function_count": count, "variable_count": variables,
                       "tls_variable_count": tls_variables,
                       "functions": [{"nid": f"0x{nid:08x}", "stub": struct.unpack_from(">I", stubs, i * 4)[0]}
                                     for i, (nid,) in enumerate(struct.iter_unpack(">I", nids))]})
        address += record_size
    return result


def embedded_spus(image: ElfImage) -> list[dict]:
    result = []
    offset = 0
    while True:
        offset = image.data.find(b"\x7fELF", offset + 1)
        if offset < 0:
            return result
        if image.data[offset + 4:offset + 6] != b"\x01\x02":
            continue
        try:
            child = ElfImage.parse(image.data[offset:])
        except AnyPS3Error:
            continue
        if child.machine == 23:
            result.append({"file_offset": offset, "entry": child.entry,
                           "load_segments": sum(s.type == 1 for s in child.segments),
                           "note": "Structurally valid embedded SPU ELF; execution not tested."})


def analyze(image: ElfImage) -> dict:
    table = sections(image)
    descriptors, section_index = opd_entries(image, table)
    libraries = imports(image)
    # Duplicated descriptors may share a code address. Coverage is over unique code entries.
    unique = {e["code"]: e for e in reversed(descriptors)}
    supported, blockers = [], Counter()
    for descriptor in unique.values():
        try:
            function = recompile(image, entry=descriptor["descriptor"], max_instructions=256)
            supported.append({**descriptor, "instructions": len(function.instructions)})
        except AnyPS3Error as exc:
            blockers[str(exc).split(" at ")[0]] += 1
    try:
        recompile(image, max_instructions=256)
        entry_blocker = None
    except AnyPS3Error as exc:
        entry_blocker = str(exc)
    report = image.report()
    report.update({"sections": table, "opd_section_index": section_index,
                   "opd_descriptor_count": len(descriptors), "unique_code_entry_count": len(unique),
                   "imports": libraries, "import_function_count": sum(lib["function_count"] for lib in libraries),
                   "embedded_spu_images": embedded_spus(image),
                   "recompiler_survey": {"instruction_limit_per_entry": 256,
                       "supported_entry_count": len(supported), "supported_entries": supported,
                       "first_blockers": [{"reason": reason, "entries": count} for reason, count in blockers.most_common()],
                       "entry_blocker": entry_blocker}})
    report["notes"] = ["Static analysis only; no game boot or compatibility claim.",
                       "OPD-derived entries are not an exhaustive function inventory. Imports retain NIDs; names are unresolved.",
                       "Supported entry count includes trivial returns and stubs, and is not a gameplay coverage percentage.",
                       "Import variables/TLS are counted, not resolved; embedded SPUs are validated, not executed."]
    return report
