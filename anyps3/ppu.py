"""Small fail-closed PPU-to-C++ lifter, for integer function experiments."""

from dataclasses import dataclass
from collections import deque

from .elf import ElfImage
from .errors import AnyPS3Error


@dataclass(frozen=True)
class Instruction:
    address: int
    word: int
    mnemonic: str
    statement: str
    successors: tuple[int, ...]
    memory: bool = False


def memory_instruction(word: int, pc: int) -> Instruction:
    op, rt, ra = word >> 26, (word >> 21) & 31, (word >> 16) & 31
    # D-form integer loads/stores and DS-form ld/ldu/lwa/std/stdu.
    forms = {32: ("lwz", 4, False), 34: ("lbz", 1, False),
             36: ("stw", 4, True), 38: ("stb", 1, True),
             40: ("lhz", 2, False), 42: ("lha", 2, False), 44: ("sth", 2, True)}
    update, extend = False, False
    displacement = word & 0xFFFF
    if op in (58, 62):
        variant = word & 3
        if variant == 3 or (op == 62 and variant == 2):
            raise unsupported(word, pc)
        update = variant == 1
        extend = op == 58 and variant == 2
        mnemonic = ("lwa" if extend else "ld") if op == 58 else "std"
        size, store = (4 if extend else 8), op == 62
        displacement &= ~3
    else:
        mnemonic, size, store = forms[op & ~1]
        update = bool(op & 1)
        extend = mnemonic == "lha"
    if update:
        if not ra or (not store and ra == rt):
            raise AnyPS3Error(f"Invalid PPU update-form operands at 0x{pc:08x}.")
        mnemonic += "u"
    if displacement & 0x8000:
        displacement -= 0x10000
    base = f"r[{ra}]" if ra else "UINT64_C(0)"
    ea = f"{base} + UINT64_C(0x{displacement & 0xffffffffffffffff:016x})"
    lines = [f"{{ const std::uint64_t ea = {ea};"]
    if store:
        lines.append(f"state.memory.store(ea, {size}, r[{rt}]);")
    else:
        lines.append(f"const auto value = state.memory.load(ea, {size});")
        value = "value"
        if extend:
            sign = 1 << (size * 8 - 1)
            # Unsigned xor/sub implements sign extension without signed casts/overflow.
            value = f"(value ^ UINT64_C(0x{sign:x})) - UINT64_C(0x{sign:x})"
        lines.append(f"r[{rt}] = {value};")
    if update:
        lines.append(f"r[{ra}] = ea;")
    lines.append("}")
    return Instruction(pc, word, mnemonic, " ".join(lines), ((pc + 4) & 0xffffffff,), True)


def decode(word: int, pc: int) -> Instruction:
    op = word >> 26
    rt = (word >> 21) & 31
    ra = (word >> 16) & 31
    rb = (word >> 11) & 31
    imm = word & 0xFFFF
    signed = imm if imm < 0x8000 else imm - 0x10000
    next_pc = (pc + 4) & 0xFFFFFFFF
    successors = (next_pc,)
    if 32 <= op <= 45 or op in (58, 62):
        return memory_instruction(word, pc)
    if word == 0x4E800020:  # Exact unconditional blr, no link or condition variants.
        mnemonic, statement, successors = "blr", "return;", ()
    elif op in (14, 15):
        mnemonic = "addi" if op == 14 else "addis"
        value = (signed * (1 if op == 14 else 65536)) & 0xFFFFFFFFFFFFFFFF
        base = f"r[{ra}]" if ra else "UINT64_C(0)"
        statement = f"r[{rt}] = {base} + UINT64_C(0x{value:016x});"
    elif op in (24, 25, 26, 27):
        mnemonic = {24: "ori", 25: "oris", 26: "xori", 27: "xoris"}[op]
        value = imm << (16 if op in (25, 27) else 0)
        operator = "|" if op in (24, 25) else "^"
        statement = f"r[{ra}] = r[{rt}] {operator} UINT64_C(0x{value:x});"
    elif op == 31 and not word & 1:
        # Full ten-bit XO check rejects arithmetic OE=1; Rc=1 is rejected above.
        xo = (word >> 1) & 0x3FF
        if xo in (266, 40):
            mnemonic = "add" if xo == 266 else "subf"
            expr = f"r[{ra}] + r[{rb}]" if xo == 266 else f"r[{rb}] - r[{ra}]"
            statement = f"r[{rt}] = {expr};"
        elif xo in (28, 316, 444):
            mnemonic, operator = {28: ("and", "&"), 316: ("xor", "^"), 444: ("or", "|")}[xo]
            statement = f"r[{ra}] = r[{rt}] {operator} r[{rb}];"
        else:
            raise unsupported(word, pc)
    elif op == 18 and not word & 1:  # b/ba, no calls (LK=0).
        displacement = word & 0x03FFFFFC
        if displacement & 0x02000000:
            displacement -= 0x04000000
        target = (displacement if word & 2 else pc + displacement) & 0xFFFFFFFF
        mnemonic = "ba" if word & 2 else "b"
        statement, successors = "", (target,)
    else:
        raise unsupported(word, pc)
    return Instruction(pc, word, mnemonic, statement, successors)


def unsupported(word: int, pc: int) -> AnyPS3Error:
    return AnyPS3Error(
        f"Unsupported PPU instruction 0x{word:08x} at 0x{pc:08x}. "
        "No output generated; calls, conditional branches, indexed/atomic memory, CR/XER updates, "
        "floating point, VMX and syscalls are not implemented."
    )


@dataclass(frozen=True)
class RecompiledFunction:
    entry: int
    toc: int
    instructions: tuple[Instruction, ...]
    image: ElfImage

    def cpp(self) -> str:
        from .runtime import MEMORY_CPP, STACK_POINTER, memory_loader
        needs_memory = any(i.memory for i in self.instructions)
        lines = [
            "// Generated by AnyPS3: experimental single-function harness, not a game runtime.",
            "// Registers start at zero except r2 (TOC) and r1 (stack) in memory harnesses.",
            "// blr returns to this harness; the stack is synthetic, without process startup arguments.",
            "#include <cstdint>", "#include <cstdio>", "#include <cinttypes>",
        ]
        if needs_memory:
            lines.extend([MEMORY_CPP, memory_loader(self.image)])
        lines.extend([
            "", "struct PpuState { std::uint64_t r[32]{}; " +
            ("GuestMemory memory; std::uint32_t pc = 0; " if needs_memory else "") + "};", "",
            "void anyps3_entry(PpuState& state) {", "    auto& r = state.r;",
            "    (void)r;", f"    goto loc_{self.entry:08x};",
        ])
        for inst in sorted(self.instructions, key=lambda i: i.address):
            lines.append(f"loc_{inst.address:08x}: // {inst.word:08x} {inst.mnemonic}")
            if needs_memory:
                lines.append(f"    state.pc = UINT32_C(0x{inst.address:x});")
            if inst.statement:
                lines.append(f"    {inst.statement}")
            for target in inst.successors:
                lines.append(f"    goto loc_{target:08x};")
        lines.extend([
            "}", "", "int main(int argc, char** argv) {" if needs_memory else "int main() {",
            "    PpuState state;",
        ])
        if needs_memory:
            lines.extend(['    if (argc != 2) { std::fprintf(stderr, "Usage: harness SOURCE.elf\\n"); return 2; }',
                          "    try {"])
        lines.extend([
            f"    state.r[2] = UINT64_C(0x{self.toc:x});",
        ])
        if needs_memory:
            lines.extend(["    load_image(state.memory, argv[1]);",
                          f"    state.r[1] = UINT64_C(0x{STACK_POINTER:x});"])
        lines.extend([
            "    anyps3_entry(state);",
            '    std::printf("r3=%" PRIu64 "\\n", state.r[3]);',
            "    return 0;",
        ])
        if needs_memory:
            lines.extend(['    } catch (const std::exception& error) {',
                          '        std::fprintf(stderr, "anyps3 runtime at pc=0x%08" PRIx32 ": %s\\n", state.pc, error.what()); return 1;', "    }"])
        lines.extend(["}", ""])
        return "\n".join(lines)


def recompile(image: ElfImage, *, entry: int | None = None,
              entry_kind: str = "descriptor", max_instructions: int = 4096) -> RecompiledFunction:
    if not 1 <= max_instructions <= 1_000_000:
        raise AnyPS3Error("Instruction limit must be between 1 and 1000000.")
    code, toc = image.resolve_entry(address=entry, kind=entry_kind)
    pending = deque([code])
    found: dict[int, Instruction] = {}
    while pending:
        pc = pending.popleft()
        if pc in found:
            continue
        if len(found) >= max_instructions:
            raise AnyPS3Error(f"Instruction limit ({max_instructions}) reached at 0x{pc:08x}; no output generated.")
        raw = image.read_virtual(pc, 4, executable=True)
        inst = decode(int.from_bytes(raw, "big"), pc)
        found[pc] = inst
        pending.extend(inst.successors)
    if not any(i.mnemonic == "blr" for i in found.values()):
        raise AnyPS3Error("No reachable blr return; this prototype requires a returning function.")
    return RecompiledFunction(code, toc, tuple(found.values()), image)

