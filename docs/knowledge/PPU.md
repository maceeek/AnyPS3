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

## Host cost of the translation: cloud optimization round (2026-10-19/20)

Scope: AnyPS3 runtime and its native tests; nothing here is verified in the game. Evidence, benchmarks and limitations:
[cloud optimization record, PPU](experiments/2026-10-19-cloud-optimization.md#ppu).

- **Lazy FR/FI/FPRF (verified by `tests/test_ppu_fp_lazy.py`, exactness conditions):** only with XX already set, round-to-nearest
  (RN=0b00) and the five exception enables VE/OE/UE/ZE/XE clear (`(fpscr&0x020000fb)==0x02000000`), and in-range operands (zero or normal, exponent in [-400,400], no zero divisor, no negative sqrt;
  binary32 range and midpoint guards) can an add/mul/div/sqrt/fused operation not change any sticky bit, so only FR/FI/FPRF change. The exact
  FPSCR update is replayed before any observer (mffs, mcrfs/mtfs*, compares, conversions, estimates, frsp, VMX float scopes, any
  exact arithmetic, `anyps3_entry` exit including STOP); Rc=1 forms read only FX/FEX/VX/OX. While XX is clear the lazy path never
  applies and a lifted FP operation is ~13% slower (synthetic benchmark); that a game sets XX early and rarely clears it is **derived**.
- **Spin hints (derived benefit, verified step count):** `or n,n,n` with n in {1,6,28,29,30,31} issues one host `pause` per
  block that contains it; guest steps and registers are unchanged (`tests/test_ppu_spin_hint.py`).
- **rlwinm/rlwnm/rlwimi with MB>ME (semantic fix, verified by `tests/test_ppu_rotate_count.py`, red on the previous lifter):** the
  rotated word is duplicated into both halves (ROTL32) and MASK(MB+32, ME+32) wraps, so the upper word is not zero. The Book I PDF is not in
  the cloud container, so no PDF page is cited yet; look the page up before quoting it as a manual fact.
- **Reservation fast path (verified by `tests/test_reservation_fast_path.py`, fails on a mutant that skips the version bump):**
  taken only when the access and its whole 128-byte granule lie in one mapping; same stripe set, token and permission checks as the
  general path; a granule clipped by its mapping keeps the general path. Synthetic lwarx/stwcx. kernel 70.7 → 40.5 ns per guest
  instruction ([record, Memory](experiments/2026-10-19-cloud-optimization.md#memory-ppu-and-spu-reservations)).
