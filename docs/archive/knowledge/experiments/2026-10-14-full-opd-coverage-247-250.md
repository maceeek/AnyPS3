# Full coverage of the original OPDs, a more general switch detector, and VMX/FP (247–250)

Date 2026-10-14, Claude Opus 5.5. Goal: the original ACIT menu and gamepad navigation —
**not reached yet**. This record addresses the systemic cause of the repeated stops
`target has no statically translated block` (one ~25-minute run per target).

## 247: independent run of the prepared profile 246

`native-virtual-247` = the `virtual_pad.py build` wrapper over the unchanged
`native-bundle-246`; `program.base.cpp`, ELF and profile are byte-identical to 246,
and the binary even has the same hash `4b88dfb3…` as 246. Neutral pad.

```bash
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-246 local/ratchet/native-virtual-247
ANYPS3_VIRTUAL_PAD_CONTROL=$PWD/local/ratchet/native-virtual-247/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-247 5000000000 > local/ratchet/run-virtual-247.log 2>&1
```

**Verified:** the original case 13 `0x69dd30` from 246 was passed. New stop
`pc=0x6a2544`, `lr=0x6a39a4`, CTR `0x6a2544`, 308,706,302 PPU steps,
`target has no statically translated block`; returncode 1. 444 reads /
17,897,130 bytes, SPU 0x100 878,746,803 instructions, 15,274 flips, 30,540 draws,
33 frames, host presentation 0. `verification.json`: both PT_LOADs exact_match.
The last frame `native-virtual-247/frame-000033.png` is still the loading planet.
The target `0x6a2544` is a slot of another byte-indexed switch table with dispatch `0x6a1aa0`
(14 entries); the new detector (below) finds it automatically in 249/250.

## Original `.opd` as a source of static targets (verified)

`readelf -S local/ratchet/EBOOT.analysis.elf` (SHA256 `6debdca0…`): section [22]
`0xca6d38`, size `0x85d50`, offset `0xc96d38`, WA, align 8. All 68,522
eight-byte descriptors have TOC `0xd34a88` (= the TOC of the entry OPD) and executable
aligned code; 34,512 unique code addresses. The earlier manually verified OPDs
(`0xd11570`, `0xd0efb0`, …) lie in this section.

Change: `anyps3/build_game.py:opd_section_seeds` — the profile `static_opd_section`
(address, size, toc, sha256 `8fde4865…`) is re-verified on every build
(section hash, TOC of every entry = process TOC, aligned executable code).
Seeds only add translation targets; the jump is always determined by the original guest code.
Ceiling on translated instructions 1 M → 4 M (`program.py`, `build_game.py`).
Tests: `tests/test_opd_seeds.py` (unique/sorted, foreign TOC, misalignment,
non-executable, past the end of code, identity, process TOC, size, alignment).

Result: 294,384 → 2,735,839 translated instructions (250), C++ ~424 MB.

## More general switch table detector (`anyps3/jump_tables.py`)

On all 363 `bctr` of the original text: previously 159 tables, now 337, **no
earlier table lost or changed** (comparison of the old and new detector).
The manually verified table 246 `0x69db38` is now detected with the same 14 slots.
Extensions, all with evidence from data (`word(base+d) == bctr+4`):

- scaling `rlwinm rT,rI,2,MB,29`, the mask must cover indices 0..K;
- exact def-use intervals of the chain instead of forbidding a write anywhere;
- whitelist with explicit writes of GPRs **and CR fields** (fix: `ldu` writes RA);
- index via `mr` or a value-preserving `extsw`/`extsh` (K<0x8000)/`extsb`
  (K<0x80)/`clrlwi`/`clrldi`; bound on `clrlwi rK,rI` when SCALE reads only the retained bits;
- base directly from the TOC (`lwz rB,d(r2)`); prologue load before the epilogue `blr`;
- tables up to 4096 entries and targets up to 1 MiB (the original table `0x3dfa58` has 2239
  entries, offsets up to `0x3987c`, all aligned and executable).

Tables only add translation targets; the runtime `bcctr` jumps to the actual CTR.
26 `bctr` remain: 20 multi-block (bound in another block), 1 `-O0` code through the stack,
1 memset computed jump without a table, the rest; these still stop explicitly.
Tests: `tests/test_jump_tables.py::GeneralizedJumpTableTests` from the original words
`0x1e6e4`, `0x19d48`, `0xb3714` (encoders verified against the ELF bytes), rejection of a
CR field overwrite, narrow masks, extsb above 0x7f, incorrect bases and an independent `clrlwi`.

## New PPU instructions (found in the newly translated traps)

| Instruction | Occurrences | Semantics / source | Test |
| --- | ---: | --- | --- |
| lfsx/lfsux/lfdx/lfdux/stfsx/stfsux/stfdx/stfdux | 134+75+… | like D-form | `test_indexed_fp_loads_and_stores` |
| fsqrts | 41 | `fp_sqrt` with a single rounding to binary32 | `test_fsqrts_rounds_once_to_single` (rational oracle) |
| fres, frsqrte | 10, 22 | estimate: correctly rounded result (compat. choice as with vrefp), XX unchanged, FR/FI undefined | `test_fres_and_frsqrte_estimates` |
| mtfsf | 1 | fields per FLM, FEX/VX recomputed summaries | `test_mtfsf_fields_and_summaries` |
| dcbst | 1 | no functional effect (coherent host memory) | — |
| vmaxfp/vminfp | 11/33 | NaN vA→vB quiet, max(+0,−0)=+0 (RPCS3 `PPUInterpreter.cpp:599-615`, `simd.hpp:1436`) | `test_float_maximum_and_minimum` |
| vmrg*, vupk[hl]s[bh], vcmpequ[bh][.], vmax/vmin[su][bhw], vrl[bhw], vpk[us][hw][us]s, lvsl/lvsr | 1–5 | opcodes per RPCS3 `PPUOpcodes.h:212-340,413,430` | `test_integer_element_families_and_shift_vectors` |
| vrsqrtefp | 2 | estimate (compat. choice) | `test_reciprocal_square_root_estimate` |

Remaining traps of 250 (333): intentional `tw 31` (109×) and `twi` at invalid places
(159×), unimplemented imports (63, mainly sysPrxForUser/cellAudio), a few
VMX. All of them stop explicitly when reached.

Whole suite: `ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q`
→ 191 tests OK (`local/ratchet/tests-250.log`).

## Builds 248–250

- **248**: OPD seeds only (old detector), 2,598,962 instructions, 625 traps.
  Compilation without LTO ~9 min / ~21 GB RSS, deliberately stopped (obsolete);
  `NOT_RUN.txt`.
- **249**: + new detector and FP indexed. `virtual_pad.py build --lto` (new
  option, flags in the manifest): GCC 16 ICE SIGSEGV in `get_loop_body` (pass ch)
  on `page_73` during LTRANS after 6:35; `NOT_RUN.txt`.
- **250**: + all instructions above, 2,735,839 instructions, 333 traps, 336 tables
  (incl. `0x6a1aa0` → `0x6a2544`). The wrapper compiled with `ulimit -s unlimited` and LTO.

**Hypothesis:** the ICE in 249 is GCC stack exhaustion during recursion over a giant function;
the successful compilation of 250 with an unlimited stack will distinguish it.

## 250: real run (verified)

`native-virtual-250` (LTO wrapper, `ulimit -s unlimited` — compilation 6:30 without ICE, the
GCC stack hypothesis confirmed). The game passed the stop point of 247 and all earlier ones:
**478,661,064 PPU steps** (previously 308 M), 29,798 flips, 59,588 draws, 57 frames, HDD cache
writes (`/dev_hdd1/cache.dat`), loading of `front_end/level_cached.psarc`; the last frame
is still the loading planet. Both PT_LOADs exact_match. Stop:
`pc=0x2018df64` (syscall 802 cellFsRead in libfs), `lr=0x710770`:
`unmapped or split guest memory access address=0x42effd2a size=11698 write=1` — a buffer
spanning two adjacent, separately mapped regions (boundary `0x42f00000`). It is not a missing block.

## 251/252: fixes after 250

- `anyps3/native_fs.py`: cellFsRead/Write via `GuestMemory::transfer` (validation of the whole range
  across regions); test `tests/test_native_fs.py` (read/write across the boundary, unmapped second half → stop).
- `anyps3/services.py` `guest_copy` (memcpy/memset) and the new `anyps3/guest_libc.py` (_sys_memcmp, memmove,
  strchr, strrchr, strncpy, strncasecmp per RPCS3 `sys_libc_.cpp:99-370`) likewise via `transfer`;
  test `tests/test_guest_libc.py`.
- **cellAudio** (7 NIDs) was implemented by the codex subagent (GPT-6.1 Sol high) per the assignment
  `local/ratchet/codex/task-audio.md`; result and review: `local/ratchet/codex/audio-result.md`,
  `anyps3/native_audio.py`, `tests/test_native_audio.py` (9 tests). The sink is selected only
  explicitly via `ANYPS3_AUDIO_SINK=sdl|null`; runs 252+ use `null` = a diagnostic silent sink (not evidence of audio).
- Profile: +23 switch tables / +309 seeds from the subagent research `local/ratchet/codex/switches-250.md`
  (22 verified, 3 unclear = translation targets only; slots independently recomputed by `add_jump_table.py`;
  `0x6eec40` and `0x7102f0` were already in the profile and match). Log `local/ratchet/profile-jump-tables-251.log`.
- Performance (252): `GuestMemory::mark_write` previously incremented the `std::map` version for every written
  128B line; now the version is incremented only for lines observed by a reservation (tokens are compared only between
  a reservation and a conditional store). `access` has a cache of the last region validated against the current
  vector. Test `tests/test_memory.py::RegionCacheAndReservationTests`.
- Whole suite over the sources of 251: 202 tests OK (`local/ratchet/tests-251.log`); after the performance change the targeted
  suites memory/runtime_objects/spu/lightweight_sync/program 81 OK + the new test OK.
- 251 deliberately not run (`NOT_RUN.txt`), replaced by 252.

Two more subagent research notes: `docs/knowledge/experiments/2026-10-14-import-research-250.md`
(all 63 unimplemented imports of 250, ABI, priorities) and `local/ratchet/codex/switches-250.md`.

## 252–255 (verified runs and changes)

- **252** (split-safe memory, libc, cellAudio null sink, performance): ~2–5× faster (203 reads in 38 s;
  the stop of 250 passed in ~16 min instead of ~38). Stop `pc=0x637098`, 541,422,867 steps:
  `FP infinite arithmetic is unsupported` — the original normalization of a zero vector
  (`fsqrt 0`, `fdiv 1/0=+∞` with ZX, then `fmuls 0×∞`). 35,434 flips, 67 frames, PT_LOAD exact.
  Fix: `fp_multiply`/`fp_fused` with infinities (∞×0 → default QNaN + VXIMZ + VX, ∞−∞ → VXISI),
  test `test_multiply_and_fused_with_infinities` incl. the exact original instruction and FPSCR `0x86008000→0xa6111000`.
- **253**: FP passed (FPSCR `0xa6124000` carries VXIMZ); stop at 541,456,384 steps
  `SPU target has no static translation`: SPU `0x1000200` at LS `0x10500` — the game SPU kernel
  (the game's own SPU ELF `0xbf4480`, LS `0x3000`) loaded the DMA job **CollNarrow** `0xb8b180` (0x10be0 B) to LS `0x100`;
  earlier also **CollBroad** `0xb9bd80` (0xc680 B). Names from the original constant table `0xca147c` →
  strings `0xac2638`/`0xac2648`.
- **254**: + `fcmpu/fcmpo` with NaN/∞ (FU, VXSNAN, VXVC) and saturating `fctiw[z]/fctid[z]` (VXCVI);
  compiled, not run (`NOT_RUN.txt`), replaced by 255. Test `test_compare_and_integer_conversion_specials`.
- **Imports (codex, `local/ratchet/codex/imports2-result.md`)**: `sys_lwmutex_trylock`, atexit registry,
  `_sys_malloc/_sys_free`, SPU image ownership, module unload boundary; heap left as a trap (undocumented arguments).
  225 tests OK.
- **Window (codex, `local/ratchet/codex/present-result.md`)**: `ANYPS3_PRESENT=sdl` presents the actual guest scanout
  to an SDL2 window (60 Hz throttle, `presented_frames` real, closing the window = explicit end).
- **Profile 255**: 130 SPU code blocks from the original area `0xb00000–0xbf4480` (first instruction `ila`,
  zero padding, trailer `ffffffff…`) as overlays at LS `0x100` + 2 embedded SPU ELF jobs
  (`0xbfdc00`, `0xc0c300`, LS `0x4000`); each with the SHA-256 of the original bytes. LS `0x100` is the observed base of jobs;
  the other bases are a **hypothesis**, every word is verified at run time (otherwise an explicit stop).
  Log `local/ratchet/profile-spu-jobs-255.log`, backup `local/ratchet/profile-before-spu-jobs-255.json`.

## Termination (2026-10-14 ~20:30 UTC)

255: the LTO compilation of the wrapper was interrupted after 15.5 min on a user instruction to end the work; one LTRANS
partition ran for >7 min on its own (hypothesis: giant `spu_image_0_page_*` from 130 overlays at LS `0x100`).
Not run. Main recommendation: split the generated translation into multiple TUs and the SPU pages into smaller functions.
