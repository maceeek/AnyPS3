# PPU: PowerPC, ABI and the HLE boundary

**Fact (public architecture):** The CELL PPE is a 64-bit PowerPC implementation with both a 32-bit and a 64-bit mode. In 32-bit mode, some address interpretations and branch tests change; this does not mean that all GPRs are narrowed to 32 bits. [Cell Architecture v1.02](../../local/reference/cell-docs/pdf/cell-architecture-v1.02.pdf), physical PDF page 47, section 4. The document refers to PowerPC Books I–III, which this library has not downloaded yet.

**Fact (reference implementation):** RPCS3 `local/reference/rpcs3/rpcs3/Emu/Cell/PPUThread.h:168` records 32 GPRs, 32 FPRs and 32 vector registers. This is the state of RPCS3 at the revision in [SOURCES.md](SOURCES.md), not a complete normative ISA specification.

## Map of local sources

| Source | Purpose and boundary |
| --- | --- |
| [Handbook v1.1](../../local/reference/cell-docs/pdf/cell-handbook-v1.1.pdf), chapter 2, PDF from page 49 | PPE overview; Appendix A offers an instruction overview |
| `local/reference/rpcs3/rpcs3/Emu/Cell/PPUOpcodes.h` | instruction decoding |
| `local/reference/rpcs3/rpcs3/Emu/Cell/PPUInterpreter.cpp` | reference for the semantics of specific instructions |
| `local/reference/rpcs3/rpcs3/Emu/Cell/PPUThread.h` | register and thread state |
| `local/reference/qemu/target/ppc/translate.c`, `int_helper.c` | supplementary PowerPC implementation reference; compare the specific target CPU/mode |
| `local/reference/PSL1GHT/ppu/include/lv2/thread.h`, `sys/thread.h` | public PS3 thread interface |
| `local/reference/PSL1GHT/ppu/include/io/pad.h` | public gamepad interface; guest runtime evidence is required for the menu |

Read QEMU in accordance with its `AGENTS.md` and `docs/devel/code-provenance.rst`: here it is used to research interfaces/algorithms, without upstream contributions and without editing the reference.

## OPD, callback and the actual stop

Before adding a static callback target, verify the original OPD bytes, entry, TOC and the identity of the ELF/PRX image. The descriptor address and the code address are different values; take them from the specific image, not from another firmware version. In the record, state the source of the addresses, the segment range, the disassembly and the runtime caller. This is a working rule of AnyPS3, not a claim that the given callback has been tested.

Record the HLE return and the guest reaction separately: a known `NOT_IMPLEMENTED`/error may be handled by the guest, but by itself it does not confirm that the service completed. On an `unmapped read`, record the fault address, PC, LR, GPRs and the call chain; distinguish null + offset from a corrupted pointer from the actual registers. Do not determine the cause from a low fault address alone.

The exact current ACIT stop is maintained in [CURRENT.md](CURRENT.md), with a link to the actual log.

## PPU floating divide and the subsequent game clamp

**Verified by native tests 245:** disabled-trap FP divide and add/subtract
handle infinities, ZX and invalid VXZDZ/VXIDI/VXISI with the correct classification
and sticky FPSCR. The original sequence `0x69f2b0..0x69f2c0` after 154/+0
produces +Infinity, the original FSEL limits the value to 0.75 and stores it via the guest
big-endian STFS. The change is in the runtime; the original game instructions are preserved.
This is a test of the ISA sequence, not evidence of the menu; the actual 245 is
still loading at the time of writing. Detailed sources, scope and limitations of the download of the original Book I,
the regression and the runtime: [FP experiment 245](experiments/2026-10-14-fp-zero-divide-245.md).
