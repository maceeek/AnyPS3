# CELL: orientation and memory boundaries

## Verified public sources

[Cell Architecture v1.02](../../local/reference/cell-docs/pdf/cell-architecture-v1.02.pdf), physical PDF pages 27–31, describes the PPE, SPE, local storage and MFC. [Handbook v1.1](../../local/reference/cell-docs/pdf/cell-handbook-v1.1.pdf), pages 63–64, describes a concrete SPE as SPU + MFC and a 256 KB local memory. Keep the general ISA separate from the implementation size of the LS; the SPU ISA permits other implementations.

**Fact (public architecture):** SPU load/store operates on local memory. Transfers to/from main memory are mediated by the MFC. Its SPU channel interface is not the same thing as the PPE's proxy MMIO interface. Architecture v1.02, PDF pages 29–31; [text conversion](../../local/reference/cell-docs/md/cell-architecture-v1.02.md#pdf-page-29).

**Fact (public architecture):** `get` copies from main memory to the LS; `put` from the LS to main memory. Architecture v1.02, PDF pages 64–66, sections 7.5/7.6. Do not replace fence/barrier or tag group completion with automatic completion of every transfer: read the ordering rules in section 7.9, especially PDF pages 73–76.

**Fact (Handbook v1.1):** the MFC parameters are written before the command opcode; writing the opcode enqueues the complete command. PDF page 452, section 17.2.1. When debugging, record the LSA, EA, size, tag, command, queue capacity and evidence of completion, not only the changed bytes.

## Practical source map

- SPU instructions: [SPU.md](SPU.md), ISA v1.2.
- Channel IDs and state: `local/reference/rpcs3/rpcs3/Emu/Cell/SPUThread.h:61`, see the reference commit in [SOURCES.md](SOURCES.md).
- MMIO registers: [Registers v1.5](../../local/reference/cell-docs/pdf/cell-registers-v1.5.pdf), mainly the index on pages 3–10; verify meaning and bits in the original.
- Public system interface of PSL1GHT: `local/reference/PSL1GHT/ppu/include/lv2/spu.h` and `ppu/include/spurs/`.

## Questions for AnyPS3

There is no newly performed runtime test here yet. For a specific waiting SPU, verify the PC, channel, entry count, MFC tag mask/update/status, LS and guest scheduler. Distinguish the hypothesis "the SPU hangs" from a correctly blocking channel read and from an unimplemented wakeup. Verify PS3 access to the available SPEs, LV2 syscalls and SPURS separately; the public Cell manual does not describe all PS3 firmware services.

The PowerXCell Handbook v1.11 is kept as a supplement; the changed double precision instructions and DDR2 specifics are not automatically properties of the PS3 CELL.
