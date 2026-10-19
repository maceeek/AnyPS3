# Native start of Ratchet and the firmware libraries

Verified 2026-10-14 on Linux x86-64, build `native-bundle-121`. **The game is not playable and did not produce a game frame.**
The goal of "getting into the game" remains unmet.

The subsequent graphics milestone added [Vulkan surface clear](vulkan-rendering.md),
verified by a separate synthetic RSX FIFO and a check of all pixels. The
numbers below describe the game start of build 121, before this backend was added.
The current [progress toward the menu](menu-progress.md) already includes the game clear
and four captured black frames; the menu is still missing.

## What actually ran

The original game code and thirteen firmware libraries are translated before execution to
C++17 and the host compiler builds them for x86-64. The same applies to the SPU
bootstrap, the SPURS firmware and two further blocks of the original game task manager.
At run time, pre-translated operations are selected. There is no PPU/SPU
instruction decoder, JIT or RPCS3 backend. Registers and big-endian memory remain an explicit
program state; this is not a reconstruction of the original source code.

The run so far exceeded **102 million PPU steps**. The original engine finished
a demanding initialization loop that writes procedural texture data to VRAM
and continued to further initialization. The six SPU contexts completed
**24,360 instructions** in total. Step counts are not a percentage of a finished port.
PPU steps also include import transitions and module returns.

The original engine printed:

```text
Build Config - Final

total mem:        256.000
application mem:  234.948
global alloc:     233.000
system heap:      1.948
```

At start, the engine obtained 233 MiB of global memory and 249 MiB of VRAM with real
host storage. The SysUtil thread later released 3 MiB of main memory
for the system container.
Nine PPU contexts were created: the main thread, `SpursHdlr1`, `SpursHdlr0`,
`PuJobDispatch`, `_gcm_intr_thread`, `igSpursHdlr1`, `igSpursHdlr0`,
`igSpuPrintHandler` and `SysUtilThread()`. They have separate registers, TLS and priorities; supported
blocking operations hand control to another context.

## Inputs and firmware

The release is BCES00511 01.00, the analysis EBOOT has SHA-256
`6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`.
The origin and completeness of the disc are described in the [memory milestone](ratchet-acit-memory-milestone.md).

Firmware **3.01** comes from the attached `PS3UPDAT.PUP`. The offline extractor
verified the HMAC of seven PUP entries and of the `dev_flash` sections taken from it: 905 files,
162,818,260 bytes. ECDSA signatures are not verified. Neither keys nor game files are
part of the distributed code.

The combined image contains `libnet`, `libssl`, `libhttp`, `libsysutil`,
`libsysutil_userinfo`, `libmic`, `libfs`, `libnetctl`, `libsysutil_np`,
`librtc`, `libsysutil_np_trophy`, the resident `libsre` and the resident `libgcm_sys`.
The original `module_start` of GCM is executed before the game. Initialization of the required
modules with IDs 0, 1, 2, 3, 4, 9, 14, 20, 21, 22, 34, 50 and 61493 finished.
This is not verification of all APIs of these modules.

The parser of the original `cellSysutil` processed native responses to the IPC connection,
the query for and the change of the video output configuration, the language, the confirmation button and
the start metadata. The metadata come from the real PARAM.SFO of the attached disc.

`cellSysCacheMount` and `cellSysCacheClear` have a native public API. The cache
`BCES00511` was actually attached to `/dev_hdd1`, with a working host
directory under the explicit `ANYPS3_CACHE_ROOT`. The first attach creates an empty
directory and returns CLEARED; a repeated attach of the same ID within the process keeps
the content and returns RELAYED. Clearing actually removes the content of this directory.
The cache is not carried over between processes and its directories remain as diagnostic
artifacts. The internal VSH request `cacheInit` is still not implemented.

Memory containers reserve an actual budget of main memory, report
the size and the free part and return the budget on cancellation. A container
with memory in use cannot be cancelled. Allocation from a container will require another service.

## Graphics initialization

The original `cellGcmInit` created a graphics context and a handler thread. The native
runtime maps the device at `0x60000000`, DMA at `0x60100000`, driver information
at `0x60200000` and reports at `0x60300000`. The I/O area of size
233 MiB from offset zero refers to the real main memory from `0x40000000`.
VRAM starts at `0xc0000000` and is accounted separately from main memory.

The FIFO executes command order, GET/PUT, jump/call/return, references and semaphores.
A semaphore wait is released only after an actual change of the relevant memory. An unknown
command stops the run. The initialization so far processed **331 packets** and stored
**511 configuration register values**, GET = PUT = `0x1430`.
Merely storing a register does not confirm rendering of its effect.

The engine set an RGB output of 1280 × 720, pitch 5120 and two display buffers:
ID 1 at offset `0`, ID 2 at `0x398000`. It also set the following tiles:

| ID | Offset in VRAM | Size | Pitch | Compression |
|---|---|---|---|---|
| 2 | `0x0000000` | `0x880000` | 5120 | 7 |
| 1 | `0x0880000` | `0x580000` | 8192 | 7 |
| 0 | `0x0e00000` | `0x580000` | 8192 | 11 |
| 4 | `0x1380000` | `0x2c0000` | 4096 | 11 |
| 5 | `0x1640000` | `0x2c0000` | 4096 | 0 |

Two Z-cull regions have dimensions 960 × 704. These are verified descriptions of the regions;
tile compression and the actual Z-cull computations are not yet implemented.
Actual drawing, shader upload and non-empty blits are not implemented.
The new Vulkan backend supports only a full colour clear of a linear ARGB8 surface
without MSAA and tiles; it does not yet cover the game's compressed surfaces.
A zero clear and a blit with an empty rectangle have no
visual effect. No host game frame has been created and no game window opened.

`cellGcmSetFlipMode` and its second variant have a native implementation of the public
API: they check arguments and set the output policy. They do not execute the internal
undocumented call `sys_rsx_attribute(677)`, which remains unsupported.
This is not evidence of a completed flip or an implementation of presentation.

## Input devices

`cellPadInit`, `End`, `GetInfo2`, `SetPortSetting` and `GetData` use SDL2
on the host. They support attach/detach, real buttons, analog axes,
change flags and trigger pressure values. Motion and vibration are not advertised.
In the verified start no physical controller was connected and the service correctly reported
**zero devices**. The integration test attaches a real virtual SDL device,
drives its inputs and verifies the PS3 messages and the detach. This is not a test of game control.

## SPU and the original game task manager

The group `CellSpursKernelGroup` has one thread. Another group,
`igCellSpursKernelGroup` of type `0x18`, has five worker threads without context
switching. They reserve physical SPUs and do not account a nonexistent storage of a saved
context into main memory. Each has its own real 256 KiB local memory.

The bootstrap has LS entry `0x838`. Images and overlays are bound in the profile
to SHA-256. Each executed operation checks the exact original word; for
overlapping overlays the known variants are translated in advance. Changed or new
code cannot launch an interpreter or run-time automatic translation.

| Source | LS target | Bytes | Meaning |
|---|---|---|---|
| `0x20268880` | `0x00a00` | `0x1f80` | Original SPURS firmware |
| `0x00ad9d00` | `0x00a00` | `0x230` | Original game workload |
| `0x00ad9780` | `0x3f580` | `0x510` | Original game task manager |

GETLLAR/PUTLLC track real reservations including writes through aliases.
Waiting for the loss of a reservation leaves the PC on the instruction and releases the processor.
SNR channels support PPU notifications, overwrite/OR, zero values and blocking
reads. The states so far are 4,981 completed instructions in the first thread,
four times 3,916 and once 3,715 in the game group. The game task manager waits at
LS `0x3f760` for an actual change of memory; it is not forcibly marked as completed.

The scheduler is cooperative and DMA is synchronous. It is not timing-faithful to the PS3. Further
events, mailboxes, the full ISA and the complete thread lifecycle are still missing.

## Math and its limits

Scalar FMADD/FMSUB and the negative variants have a single rounding and are
verified against an independent rational computation. VMX includes further loads,
permutes, splats, integer arithmetic, comparisons, conversions, addition and FMA.
It preserves the scalar FPSCR and updates CR6/VSCR separately. The enumerated operations
also handle infinities and NaN; other unimplemented cases stop the run.

`vrefp` uses an exactly rounded reciprocal. It does not reproduce the
Cell estimate table, so its last bits and the subsequent computations may
differ from the PS3. Unsupported subnormal results, overflow and the VMX
non-Java mode remain explicit errors. The original texture routine thus
passed natively, but its output is not verified against a memory snapshot of a real PS3.

## Reproduction

With the local data present and a new output directory:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --profile profiles/ratchet-acit.json \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --max-steps 200000000 \
  -o build/ratchet-native

c++ -std=c++17 -O0 -Wall -Wextra -Werror \
  build/ratchet-native/program.cpp -o build/ratchet-native/program

mkdir -p local/ratchet/cache
ANYPS3_DISC_ROOT=local/ratchet/game-complete \
  ANYPS3_CACHE_ROOT=local/ratchet/cache \
  ./build/ratchet-native/program build/ratchet-native/program.elf

python -m unittest discover -s tests -q
```

The run requires the SDL2 shared library for Linux. The default limit of one million
steps is not enough for the original texture generation. The `--trace` option records every
PPU step; for this long pass it would produce several gigabytes of diagnostics.

**127 tests passed.** The native tests include contexts and TLS, priorities,
PPU/SPU reservations including ABA, DMA, SNR, overlapping static overlays,
resident PRX initialization, SDL input, the RSX FIFO and memory configuration,
scalar FMA and VMX. The tests do not require game files and are not a playability test.

## Current stop and artifacts

```text
STOP pc=0x201142e8 lr=0x20113aa8 steps=102817041:
unsupported native sysutil request family: initGameData
```

This is the original GDAT request `initGameData`, 176 bytes on channel 1,
in the new thread `SysUtilThread()`. The engine had previously actually unmapped and released
three 1 MiB objects and created a 3 MiB memory container. The following work must
implement the check and creation of game data over the real storage,
including the responses and callbacks expected by the original firmware. Success is not
returned now. The program ends with code **1**. After that, the renderer, file
services for game content, sound and other reached interfaces still remain.

The run performed **102,817,041 PPU steps**, **24,360 SPU instructions** and **331 RSX
packets**. The complete PPU trace is disabled for this long run; we therefore do not state
the number of distinct executed addresses. The SPU and FIFO states are printed at the stop.

Local artifacts in `local/ratchet/native-bundle-121/`:

- `program.cpp`, `program.elf`, `program`: the AOT source, the memory image and the binary.
- `build.json`: identities of the inputs, modules, static targets and overlays.
- `verification.json`: artifact hashes, the exact stop, counters and a comparison
  of all original game PT_LOAD bytes with the combined image.
- `stderr.log`: imports, syscalls, thread switching and the complete SPU trace.
- `stdout.log`: the engine's real output.

The log of the complete tests is `local/ratchet/tests-native-121.log`. The summary evidence
also lists the hashes of the source modules and of the profile. The original EBOOT remained unchanged
and all of its file-backed PT_LOAD bytes are preserved in the combined
image. The SHA-256 of the combined image is
`2a14cfb8f0ce93297384e7dc3663b1e0e5b008723aa4e30409deaef6e090422b`.
Intermediate builds are working diagnostics; the current reproducible state is build 121.

## Local technical reference

The user supplied RPCS3 into `local/reference/rpcs3`. Verified clean revision:
`ffba2bf09ef3980638f9327b25240d81414034ab`. It is used for reading the ABI and ISA,
not as a running or linked backend. The references include `PPUInterpreter.cpp`,
`SPUThread.cpp`, `SPUOpcodes.h`, `lv2/sys_spu.cpp`, `lv2/sys_rsx.cpp`,
`Modules/cellPad.h`, `Modules/cellGcmSys.cpp` and the RSX definitions.

Primary sources:
[RPCS3 at the verified revision](https://github.com/RPCS3/rpcs3/tree/ffba2bf09ef3980638f9327b25240d81414034ab),
[SPU ISA v1.2 IBM/Sony/Toshiba](https://arcb.csc.ncsu.edu/~mueller/cluster/ps3/SDK3.0/docs/arch/SPU_ISA_v1.2_27Jan2007_pub.pdf)
and [PPU assembler IBM](https://www.ibm.com/docs/ssw_aix_71/assembler/assembler_pdf.pdf).
