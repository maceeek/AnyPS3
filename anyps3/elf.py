"""Read ELF headers and segments without allocating guest address space."""

from dataclasses import asdict, dataclass
import hashlib
import struct

from .errors import AnyPS3Error

PT_LOAD = 1
PF_X = 1
EM_PPC64 = 21
EM_SPU = 23


@dataclass(frozen=True)
class Segment:
    index: int
    type: int
    flags: int
    offset: int
    virtual_address: int
    file_size: int
    memory_size: int
    alignment: int

    @property
    def permissions(self) -> str:
        return "".join(c if self.flags & bit else "-" for c, bit in zip("RWX", (4, 2, 1)))


@dataclass(frozen=True)
class ElfImage:
    data: bytes
    bits: int
    byte_order: str
    os_abi: int
    type: int
    machine: int
    entry: int
    segments: tuple[Segment, ...]

    @classmethod
    def parse(cls, data: bytes) -> "ElfImage":
        if data[:4] == b"SCE\x00":
            raise AnyPS3Error("SELF container detected; provide an ELF or use 'anyps3 unself' for supported APP containers.")
        if data[:4] != b"\x7fELF":
            raise AnyPS3Error("Not an ELF file (expected 7f 45 4c 46).")
        if len(data) < 16:
            raise AnyPS3Error("Truncated ELF identification header.")
        if data[4] not in (1, 2) or data[5] not in (1, 2):
            raise AnyPS3Error("Unsupported ELF class or byte order.")
        if data[6] != 1:
            raise AnyPS3Error("Unsupported ELF identification version.")
        bits = 32 if data[4] == 1 else 64
        order = "little" if data[5] == 1 else "big"
        endian = "<" if order == "little" else ">"
        header = struct.Struct(endian + ("HHIIIIIHHHHHH" if bits == 32 else "HHIQQQIHHHHHH"))
        header_size = 16 + header.size
        if len(data) < header_size:
            raise AnyPS3Error("Truncated ELF header.")
        (kind, machine, version, entry, phoff, _shoff, _flags, ehsize,
         phentsize, phnum, _shentsize, _shnum, _shstrndx) = header.unpack_from(data, 16)
        if version != 1 or ehsize != header_size:
            raise AnyPS3Error("Invalid ELF header version or size.")
        if phnum == 0xFFFF:
            raise AnyPS3Error("Extended ELF program-header numbering is not supported.")
        ph = struct.Struct(endian + ("IIIIIIII" if bits == 32 else "IIQQQQQQ"))
        if phnum:
            if phentsize != ph.size:
                raise AnyPS3Error("Unexpected ELF program-header entry size.")
            if phoff < header_size or phoff + phnum * phentsize > len(data):
                raise AnyPS3Error("Program-header table is outside the file or overlaps the ELF header.")
        segments = []
        address_limit = 1 << bits
        for index in range(phnum):
            values = ph.unpack_from(data, phoff + index * phentsize)
            if bits == 32:
                ptype, offset, va, _pa, filesz, memsz, flags, align = values
            else:
                ptype, flags, offset, va, _pa, filesz, memsz, align = values
            if ptype == 0:  # PT_NULL has no defined payload fields.
                continue
            if filesz and (offset > len(data) or filesz > len(data) - offset):
                raise AnyPS3Error(f"Segment {index}: file range is outside the file.")
            if ptype == PT_LOAD:
                if filesz > memsz:
                    raise AnyPS3Error(f"Segment {index}: file size exceeds memory size.")
                if va + memsz > address_limit:
                    raise AnyPS3Error(f"Segment {index}: virtual address range overflows ELF{bits}.")
                if align not in (0, 1):
                    if align & (align - 1) or (memsz and va % align != offset % align):
                        raise AnyPS3Error(f"Segment {index}: invalid load alignment.")
            segments.append(Segment(index, ptype, flags, offset, va, filesz, memsz, align))
        return cls(data, bits, order, data[7], kind, machine, entry, tuple(segments))

    def read_virtual(self, address: int, size: int, *, executable: bool = False) -> bytes:
        """Read a range fully backed by one PT_LOAD; never synthesize BSS code."""
        if address < 0 or size <= 0:
            raise AnyPS3Error("Virtual reads require a nonnegative address and positive size.")
        overlaps = [s for s in self.segments if s.type == PT_LOAD and s.memory_size
                    and address < s.virtual_address + s.memory_size
                    and s.virtual_address < address + size]
        if len(overlaps) > 1:
            raise AnyPS3Error(f"Ambiguous or split PT_LOAD mapping at 0x{address:x}.")
        if not overlaps:
            raise AnyPS3Error(f"Unmapped virtual address 0x{address:x}.")
        seg = overlaps[0]
        relative = address - seg.virtual_address
        if relative < 0 or relative + size > seg.file_size:
            raise AnyPS3Error(f"Range at 0x{address:x} is not fully file-backed (BSS or segment boundary).")
        if executable and not seg.flags & PF_X:
            raise AnyPS3Error(f"Address 0x{address:x} is not in an executable PT_LOAD segment.")
        offset = seg.offset + relative
        return self.data[offset:offset + size]

    def require_ppu_executable(self) -> None:
        if (self.bits, self.byte_order, self.machine) != (64, "big", EM_PPC64):
            raise AnyPS3Error("PPU recompilation requires ELF64 big-endian PowerPC64 (machine 21).")
        if self.type != 2:
            raise AnyPS3Error("PPU prototype accepts only ET_EXEC; PRX/relocatable images are not supported.")
        for seg in self.segments:
            if seg.type == PT_LOAD and seg.virtual_address + seg.memory_size > 1 << 32:
                raise AnyPS3Error("PPU prototype requires load segments within the PS3 32-bit address space.")

    def resolve_entry(self, *, address: int | None = None, kind: str = "descriptor") -> tuple[int, int]:
        self.require_ppu_executable()
        address = self.entry if address is None else address
        if address < 0 or address >= 1 << 32 or address % 4:
            raise AnyPS3Error("Entry address must be a 4-byte-aligned PS3 virtual address.")
        if kind == "descriptor":
            code, toc = struct.unpack(">II", self.read_virtual(address, 8))
        elif kind == "code":
            code, toc = address, 0
        else:
            raise AnyPS3Error(f"Unknown entry kind: {kind}.")
        if code % 4:
            raise AnyPS3Error(f"Unaligned PPU code entry 0x{code:x}.")
        self.read_virtual(code, 4, executable=True)
        return code, toc

    def report(self) -> dict:
        notes = ["Inspection does not establish PS3 game compatibility.",
                 "This inspection does not analyze sections, imports or embedded SPUs; use 'analyze' for those. Relocations are unsupported."]
        entry_info = None
        try:
            code, toc = self.resolve_entry()
            entry_info = {"code_address": code, "toc": toc, "interpretation": "PS3 OPD (two big-endian u32 words)"}
        except AnyPS3Error as exc:
            notes.append(str(exc))
        return {
            "schema_version": 1,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "size": len(self.data),
            "elf": {"bits": self.bits, "byte_order": self.byte_order,
                    "os_abi": self.os_abi, "type": self.type, "machine": self.machine,
                    "machine_name": {EM_PPC64: "PowerPC64 / PPU candidate", EM_SPU: "SPU"}.get(self.machine, "other"),
                    "entry": self.entry},
            "segments": [{**asdict(s), "permissions": s.permissions} for s in self.segments],
            "ppu_entry": entry_info,
            "notes": notes,
        }

