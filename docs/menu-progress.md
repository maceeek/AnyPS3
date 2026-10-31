# Progress toward the menu

**2026-10-21 wave 6:** the original level holds the 60 Hz VSYNC cap (build 706: 59.71/59.69 FPS, ~99.8 % of flips in one vblank in
the integration runs, main thread ~11 ms and RSX worker ~12 ms per frame; 472a on main was 46.68). 75 Hz emulated vblank not held.
GUI: default-on audio toggle; runtime: DualSense SIXAXIS, rumble, PS button. Physical controller still unverified.
See [CURRENT](knowledge/CURRENT.md).

**2026-10-21 GUI installer:** README now leads with desktop installation and the GUI workflow.
The `feat/gui-installer` branch (merged into main) adds per-user install/update, separate app/library/preferences,
and four passing real installer integration tests. No runtime changes or game/controller run.
See [installer record](knowledge/experiments/2026-10-21-gui-installer.md).

**2026-10-20 manager worktree:** CLI-backed game library and PySide6 wrapper added
on `fast2/manager-gui`, with scoped synthetic tests and a real-CLI offscreen
preview. All 30 manager/GUI tests pass. GPT-6-Astra's five findings were fixed and
rechecked. Save preservation, build verification and detached jobs are tested.
No new original-game run or physical-controller acceptance.
See [manager record](knowledge/experiments/2026-10-20-manager-gui.md).

Goal: the original menu of Ratchet & Clank: A Crack in Time and navigation with a gamepad.
The menu has not yet been reached. The PPU/SPU continue to run exclusively from static translation;
the Vulkan backend is our own, RPCS3 serves only as a local technical reference.

## Game data and thread lifecycle

Builds 124–127 of 2026-10-14 got past the original blockage at
`GDAT initGameData`. The original firmware successively:

1. Opened a service session with an existing memory container. The native service
   reserves a real 64 KiB of working space from its budget; the container
   cannot be released for the duration of the session.
2. Requested `getGameDataStat` for `BCES00511`. The service reads the real existence of the
   metadata and the free space under the explicit `ANYPS3_HDD_ROOT`. For a new game
   storage it returns the corresponding state and paths. The internal wire structure has 0x1450
   bytes, the public callback 0x1448; the conversion is performed by the original firmware.
3. Called the original game callback `0x66dc78` via OPD `0xd18230`. This
   callback was included in the static translation in advance, only after checking the
   ELF identity and the real descriptor. The callback takes over the paths and the free
   space and itself returns `OK_CANCEL`; the service does not pretend to create data.
4. Closed the session via `endGameData` and released its budget.
5. Finished `SysUtilThread()` via `sys_ppu_thread_exit`. The runtime releases the
   owned stack/TLS and preserves the return status for `sys_ppu_thread_join`.
   The main thread actually performed the join and continued with graphics initialization.

Supported so far is the query for a new storage without PARAM.SFO, without computing the
size and without an error dialog. Existing metadata, their update
and other modes still fail explicitly. Directory symlinks and traversal
outside the mounted root are rejected. No support for unknown requests is
simulated by a successful return.

For further reproduction, in addition to the disk and cache, the following is needed:

```bash
mkdir -p local/ratchet/hdd0
export ANYPS3_HDD_ROOT=local/ratchet/hdd0
```

## Graphics commands

Build 128 got past 348 RSX packets. The vertex shader constants are
stored bit-exactly into 512 four-component registers, respecting
`TRANSFORM_CONSTANT_LOAD` and also non-incrementing FIFO writes. The subsequent
vertex cache invalidation preserves the invalidation generation; there is as yet no
cache of rendered vertices.

Build 131 performed the first real game `CLEAR_SURFACE` through Vulkan:
960 × 704, two horizontal samples, ARGB8 and Z24S8, mask `0xf3`.
The tiled addressing of both surfaces respects the pitch and the bank. The clear stores
the full Z24S8 value bit-exactly through an RGBA8 attachment; this is not yet
support for depth testing during drawing. Compressible surfaces
are kept in an uncompressed representation, compressed data is not decoded.

The tile address conversion was compared with the local reference for 20 combinations of
pitch/bank. The tests also verify two samples, scissor, Z24S8 and preservation of the
surrounding memory. The whole suite at this point: 133 tests, all passed, Vulkan
was mandatory; the image test passed also with the Khronos synchronization check.

Run 131 then stopped at `GET_REPORT` (`0x1800`, value
`0x02020a00`) after 408 RSX packets and 102,838,528 PPU steps.
`rendered clears=1 presented_frames=0`: a real GPU operation, as yet no
game geometry, presented frame or menu. The standalone
[Vulkan image test](vulkan-rendering.md) remains a diagnostic.

## Reports, queue and output timing

Subsequent runs added GPU reports in the local report area, VRAM
and mapped main memory. They write real monotonic time and counter values;
after reset and without supported draw calls the counters are zero.

`DRIVER_QUEUE` processes the real display buffer and sends only permitted
events to the original firmware. It does not thereby confirm a completed flip. Vblank is derived
from real monotonic time, at 60000/1001 Hz according to the video configuration.
The original game callback `0x661730` (OPD `0xd17890`) is translated in advance.
The whole suite passed after these changes: 136 tests including the mandatory Vulkan test.

Build 136 showed a wait on the device `device+0x30`. The local reference
in `NV47/HW/nv406e.cpp` designates this register as display queue synchronization
and unconditionally overwrites a release of zero with one. Our implementation binds the
acknowledgement to a not-yet-acknowledged generation of the actually processed queue.
This is a protocol model derived from the FIFO and the reference, not yet verified on PS3
hardware; it is not a write to a separate flip-completion semaphore.

## First real buffer swaps

Build 138 completed four game flips alternating buffers 1 and 2.
The output sink loaded their real 1280 × 720 content via Vulkan,
waited for the fence, wrote a PPM and only then reported completion to the firmware.
All four frames are black (all 2,764,800 RGB bytes of each
frame are zero). This is neither the menu nor evidence of geometry drawing.

The reproduction additionally uses `ANYPS3_FRAME_DIR`, pointing to an existing empty
directory. Files `frame-000001.ppm` etc. are never overwritten; the diagnostic
limit is 256 frames. Missing output, a Vulkan error or a write error does not confirm the
flip and does not release its semaphore. This is headless output, not a swapchain to a
window: the diagnostics separate `captured_frames` from `presented_frames=0`.

Run 139 got past the original virtual method `0x32bb74` and stopped at the
call of `cellSysmoduleInitialize`. The export was verified directly in the original
firmware 3.01: the function at offset `0x312c` merely returns zero. The handler of this
export therefore does not pretend to load further modules; those continue to require
real images and their static translation.

## File manager initialization

Runs 142–145 loaded the real `libio` (ID 23), started its original
`module_start` and created the game `TrophyInitThread`. The reached vtables
of the state manager and the allocator were verified against the original OPDs in the EBOOT and
added among the inputs of the static translation.

`lightweight_sync.py` provides creation, destruction, signal, broadcast
and infinite wait for lightweight condition variables. A wait releases the owned
mutex, after the signal it must reacquire it and restores the original recursion depth.
A blocked thread must not complete the wait until another owner releases the mutex.
The tests cover this race, FIFO/priority selection, a non-persistent signal
and rejection of destroying a condition in use. Timeouts remain explicitly
unsupported.

Build 145 reached the original FILE-output callback `0x9098e8` at
102,888,256 PPU steps. Four black frames are still captured.
`local/ratchet/native-bundle-145/verification.json` verifies preservation of
all original PT_LOAD bytes, the hashes of the program and of the frames. Newer builds
also retain the exact `profile.json` used and the source hashes at generation time.
The whole suite passed here with 139 tests and the mandatory Vulkan backend.
The following run 146 already printed the original message `FIOS 1.3.1 initialized`
and entered the file operations backend.

## First native file operations

Runs 147–155 statically translated the reached FIOS callbacks, including its
media workers and scheduler. The worker threads really wait on a
lightweight condition and process a request after the signal. `sys_process_is_stack`
classifies addresses by the upper four bits (`0xd`), as the library expects.

Build 157 performed a real `stat("/dev_hdd1")`. Build 159 then
opened/created `cache.idx` and `cache.dat` in a unique cache directory
and the original game printed `Creating new disk-based cache files`. At this point
the loading of game assets had not yet taken place. The identity of the original PT_LOAD bytes
and the four still-black frames are verified in
`local/ratchet/native-bundle-159/verification.json`.

`native_fs.py` provides guest descriptors 3–255, real open/read/write,
close, seek and stat/fstat. `/dev_bdvd` is read-only, `/dev_hdd1` is the
session cache and `/dev_hdd0` an explicit writable root. Symlinks and
`..` are rejected, nonexistent files return an error. The special query of the
firmware `fcntl 0xc0000006` returns an embedded ENOTSUP; the firmware itself uses
the ordinary LV2 operations. Extended/encrypted open and unknown fcntl still
end with a diagnostic. A single transfer has a diagnostic limit of 16 MiB.

A further run added `_sys_strncmp` and reached a query for the storage geometry.
The sector/block sizes correspond to the configuration of the PS3 mount; free space
is determined from the real host storage.

Build 161 already wrote 512 bytes each into both cache files. The original
SPURS subsequently loaded another firmware overlay (EA `0x20266b00`, LS `0xa00`,
7456 bytes). Runs 163–166 added the actually reached SPU shifts of a whole
128-bit register, SUMB and MPYU. The tests check the boundaries of bit counts,
unsigned arithmetic and overlapping source/destination registers.

In run 166 SPURS also loaded via DMA the executable segment of the original
game SPU ELF at EA `0xbf4480` (entry LS `0x3050`, code EA `0xbf4580`,
LS `0x3000`, length `0x8c20`). The segment is included in the next AOT build
after verifying the ELF and the hash; an unknown instruction or a change of its word still
stops the run. The whole suite at point 166 passed: **145 tests**, Vulkan mandatory.
`native-bundle-166/verification.json` retains the evidence of this run.
The output remains at four black frames, with no menu and no game geometry.

Build 171 already executed the original game SPU code (reached LS `0x9c58`).
Further runs added halfword shifts/rotations and arithmetic, the remaining variants
of quadword shifts and FSCRRD/FSCRWR with the real state of each thread and masks
of reserved bits. Unsupported arithmetic FP instructions continue to fail.
The SPU emitter uses smaller native functions per page, just like the PPU;
the selection still only calls a pre-translated block and verifies the original word.

Run 174 reached 103,219,646 PPU steps. The game SPU waits for another
event after 27,500 instructions. FIOS wrote fourteen 64 KiB blocks into the index of the
cache and really probed a nonexistent installation directory under `/dev_hdd0`.
The original `fios::dearchiver` functions follow; the first read of the game archive
is not yet evidenced at this point. The whole suite at point 173 had 147 tests,
the new FPSCR test additionally passed separately. Run 174 has its own
`verification.json`, all four captured frames are still black.

The suite at point 177 passed with 148 tests and the mandatory Vulkan. Run 178 evidenced the
first attempt to open `packed/game/game.psarc` from the disc. Our extractor,
however, uses primary ISO9660 names, which in this image are in capital
letters. For this particular disc representation the following is needed:

```bash
export ANYPS3_DISC_CASEFOLD=1
```

This is an explicit adaptation of host names, not a change of the game code
or a general claim about the behavior of PS3 filesystems. The exact name takes precedence,
an unambiguous ASCII case-fold allows opening an existing file. A collision
stops the run, symlinks and traversal outside the root continue to be rejected. HDD and cache
remain case-sensitive. A targeted suite of six FS tests passed.

**Run 179 for the first time really opened and read the game archive**
`/dev_bdvd/PS3_GAME/USRDIR/packed/game/game.psarc`. The original FIOS parser
loaded the 32-byte `PSAR` header and 68 bytes of the index, then called the original
comparison function when sorting the entries. Its OPD `0xd1f150` refers to
`0x717558`; the following build includes it in the AOT. This is not yet
the loading of textures or a rendered menu. The report of run 179 contains the archive hash, a
record of the reads, preservation of the original game segments and four black frames.

Runs 180–182 completed the reading of the whole archive GAME.PSARC (19,811 bytes)
and writing into the real cache. In `cache.dat` the archive starts at offset 512
and its bytes exactly match the original file. Added are real
`fsync`/`fdatasync` and PPU MCRF with a test of all pairs of CR fields.

Runs 183–184 handed a compressed block to the original SPU program. The natively
translated decompression already executes table operations and integer multiplication;
its completion and output are not yet verified. A limit of 100,000 SPU instructions
stopped the run at LS `0x8928`, therefore the following build has a diagnostic
cap of 10 million instructions per SPU thread. Detailed tracing is enabled
with `ANYPS3_SPU_TRACE=1`; DMA, waits and errors are logged even without it.

The whole suite at point 183 passed with 150 tests and the mandatory Vulkan. The subsequent
standalone multiplication test checks signs, upper/lower halves,
accumulation and register aliases. The display continues to consist of four black frames.

The whole suite at point 185 passed with **151 tests**. Further builds added
reads of the SPU status (so far non-isolated threads with interrupts disabled),
branches with interrupt disable and real mailbox events SPU → PPU.
Interrupt enabling, isolated SPUs and unknown mailbox commands still fail.
A full queue suspends the sender and preserves its message for retry;
an unconnected port returns ENOTCONN for a request with a reply.

Run 188 transferred the first 64 KiB block of the decompression back into main memory,
delivered eight events through port 17 and reached **109,994,752 PPU steps**.
The game has already also opened `global_cached.psarc` and `global_uncached.psarc`.
The SPU completed over 7.85 million instructions; the following stop is at a DMA
to an unrecognized or split memory range. The DMA counts alone are not
evidence of correct unpacked bytes. The reference `game.toc` has 136,620 bytes,
SHA-256 `b540c6930178ed93d1850c7210069dd612df73a17a5cde49f42ad9693630c08d`;
an independent comparison with the native output is still outstanding.
The report `native-bundle-188/verification.json` confirms preservation of the original
game segments and still only four black frames.

**Milestone 190: the output of the SPU decompression is verified byte by byte.**
A diagnostic read observer captured all 136,620 bytes of `game.toc`
after the third SPU event. They match an independent zlib decoding of the archive
and the above hash. The runtime also handles DMA across adjacent memory
mappings; before copying it verifies the whole range and permissions. The game run got past
the earlier error at `0x409fee80 + 0x4000` and reached 110,610,176 PPU steps.
The current stop is the diagnostic limit of 10 million SPU instructions, not the menu.
The whole suite passed with **153 tests** and the mandatory Vulkan.
The summary, limitations and reproduction are in the [loading milestone](ratchet-acit-loading-milestone.md).


## Continuation after the SPU budget and floating point

Run 191 uses an explicit `ANYPS3_SPU_MAX_STEPS=500000000` instead of
the default limit of 10 million instructions per thread. The budget accepts only
positive decimal uint64 values; zero, signs, overflow and non-numeric
inputs are rejected. A native test executes the originally translated loop across the
old boundary and verifies a stop before the instruction that exceeds the new budget.

The first SPU in run 191 completed 30,455,452 instructions and waits for an event.
The PPU reached 115,888,424 steps and stopped at `FSUBS` in `0x3acfc0`:
the original game loaded a single-precision NaN `0xffffffff`. NaN support now
preserves the payload, sign and operand priority, quiets a signaling NaN and
sets VXSNAN/VX/FX, FPRF and FI/FR. Subtraction does not change the sign of the input
NaN. The test contains the exact instruction word from this stop.

Run 192 got past this stop and reached a callback via OPD `0xd0af48`:
code `0x4eb05c`, TOC `0xd34a88`. Its ten-item table of relative
branches at `0x4eb088` and the targets were verified against the original ELF and included in
the static translation. Run 193 was then stopped by another `FSUBS` at `0x3af508`:
`0 - 2^-142`, i.e. an exact negative binary32 subnormal result.

The runtime now supports subnormal operands and results. Before the final
rounding it shifts the unrounded significand and keeps the sticky remainder;
this is not double rounding. An exact subnormal does not set UX, a tiny inexact
sets UX and XX. FPRF distinguishes sign, zero and subnormal result.
The rational reference tests cover both precisions, the smallest values,
midpoints, the transition to the normal range and signed zero.
NaN priority and detection of tininess before rounding were also compared
with the [primary source QEMU PPC](https://github.com/qemu/qemu/blob/master/target/ppc/cpu_init.c).
Infinities without supported NaN propagation, overflow, alternative rounding
modes and enabled FP traps still fail explicitly.

The whole suite after the NaN changes passed with 155 tests and the mandatory Vulkan;
the extended rational tests of subnormal values passed standalone.
Runs 191–193 have their own `verification.json` with a check of the original
PT_LOAD bytes and artifact hashes. They continue to capture four black frames.
Neither the menu nor gamepad navigation is thereby verified yet.


Run 194 got past even the subnormal `FSUBS` and reached 117,709,281 PPU steps.
The reached game object with vtable `0xc38280` has fourteen original OPDs;
slot 2 refers via `0xcdbe00` to `0x2e8814`. Build 195 includes
the whole verified table, including all code targets, without changing the game bytes.

Run 195 stopped at the internal sysutil parameter `0x4c05`, length 8.
The original firmware export `cellAudioOutGetNumberOfDevice` (NID `0xe5e2b09d`,
code `0x2011949c`) assembles precisely this query and reads the device count from the second
word of the response. The native runtime has no configured audio backend, therefore it
returns count zero; an invalid output returns `0x8002b242`. Run 196 got past this query
and reached 117,861,953 steps, then asked for `set 0x4c09`, length 28.
The export `cellAudioOutConfigure` (NID `0x4692ab35`) was verified in the same
firmware; for a missing backend the runtime returns an explicit NOT_IMPLEMENTED,
for a secondary output UNSUPPORTED_AUDIO_OUT. It does not confirm configuration or
playback. The original firmware and the game decide how to handle the error.

The whole suite at point 196 passed with **156 tests**, Vulkan mandatory. The subsequent
targeted test also verifies the audio configuration error and the correct name `set`
in the response. Runs 194–196 preserve the original PT_LOAD bytes and still only
four black frames; the current evidence is in their own `verification.json`.


Run 197 proves that the original game took over the audio configuration error
`0x8002b240` and continued. The PPU actually scheduled `TrophyInitThread`,
for which the next build includes the verified OPD `0xd08f60`/entry `0x4c59b8`.
Run 198 already executes this original thread; the stop is in the original `libsysutil`
at PC `0x2011056c`, LR `0x20221a98`, a read from the invalid OPD `0x1b8`.
This is no longer a missing static entry. The PPU completed 118,208,831 steps,
SPU `0x100` 30,465,692 instructions. The full suite passed with 156 tests and the mandatory
Vulkan in 97.714 s. The original game PT_LOAD bytes and the current sources/profile
match the snapshot of build 198; the four frames remain black.

The requested handoff between models is governed by the root [AGENTS.md](../AGENTS.md).
The [knowledge base](knowledge/INDEX.md) contains local CELL/SPU/PPU/RSX
references, original PDFs and page-by-page Markdown conversions with a manifest.
[Runtime handoff 198](knowledge/history/runtime-handoff-198.md) lists the exact
artifacts, reproduction and the first next diagnostic action. The menu and gamepad
navigation are still not reached; a documentation milestone is not a substitute for them.


2026-10-14 (199–204): PRX import refs linked, native trophy service (NPTR), SPU runs during PPU sleep; stop 204 = untranslated vtable method `0x3304b4`, vtable `0xc3c428` added to the profile, build 205 pending. Menu/gamepad not reached.

2026-10-14 (216–237): RSX software renderer, first guest-rendered frame (bundle 228, loading icon), thousands of flips of the loading screen; run 237 interrupted for handoff. Menu/gamepad not reached. See docs/knowledge/runtime-handoff-237.md.

2026-10-14 (238): sources/profile/artifacts of 237 verified against hashes, new build 238
successfully compiled and started with budgets of 5 billion. The original loading icon visually
confirmed, reading continues; the run is live at the time of writing, without final verification.
Menu/gamepad not reached. See [record 238](knowledge/experiments/2026-10-14-loading-238.md).

2026-10-14 (238–240): 238 actually got past the old SPU limit (755 M SPU, 274 M PPU),
loaded 16.46 MB and stopped at the untranslated `0x962700`. The original 14-slot
vtable `0xc53248`, OPD/TOC/code ranges verified and the profile extended. The renderer
evaluates a constant MOV shader once while preserving all pixel operations;
174 tests with mandatory Vulkan OK, synthetic measurement ~2.28× faster.
239 compiled, not run; the new 240 also contains the verified table and the real
game is running in session 63143. The loading icon visually verified after the change,
the original PT_LOAD and the new source/profile hashes match. Getting past the new
stop/menu/gamepad not yet verified. See [240](knowledge/experiments/2026-10-14-vtable-240.md).

2026-10-14 (240–242): 240 completed the first iteration of the loader and stopped at the second type
`0x9626a4`, vtable `0xc53208`. The original six-item constructor verified all
six vtables (85 OPD slots); the profile extended by the second and the four remaining tables
with exact provenance. Runtime unchanged since 174 tests with Vulkan. 241 is live in session
55131; 242 compiled, game not yet started. Menu/gamepad not verified.
See [241/242](knowledge/experiments/2026-10-14-loader-vtables-241-242.md).

2026-10-14 (end of 241, run 242): 241 confirmed the first two iterations of the loader and stopped
at the third type `0x962404`, which the broader profile 242 already contains. PT_LOAD and its own
hash verification of 241 passed; after its end 242 was started in session 31385. The goal of the original
menu and gamepad navigation remains unverified, game 242 is live.

2026-10-14 (242–244): 242 passed the readiness of the third type and stopped in its body
at the callback `0x53a244`. The original registration of seven callbacks verified, profile
extended. 243 started prematurely and aborted before the game; 244 built correctly
and the real game runs in the diagnostic SDL wrapper `native-virtual-244`, session 60472,
connected=1. The native virtual input test (buttons/axes/pressure) and protocol errors
verified; the original translation/ELF unchanged and hashed. The menu and virtual/physical
navigation are not yet proven. See [244](knowledge/experiments/2026-10-14-virtual-pad-and-callbacks-244.md).

2026-10-14 (end of 244): the game got past the callback stop and opened both front_end
archives. 444 reads / 17,897,130 bytes, 878.75 M SPU instructions; actual stop
`0x69f2b4` after 308.21 M PPU steps: unsupported FP division 154.0 / +0.0.
Session 60472 terminated, game returncode 1. PT_LOAD, artifact and diagnostic
hashes independently verified. The last frame is still the loading planet; menu/nav
not reached. Next step: PPU division-by-zero semantics and a native regression.

2026-10-14 (245): FP division by zero returns an architectural infinity with FPSCR;
subsequent subtraction with infinity supported. The native regression executes the exact
original five instructions and verifies the guest-stored value 0.75, flags, endianness
and aliases; six FP tests and all 175 tests with Vulkan OK. New 245 compiled,
ELF/source/profile/base/manifest verified and the original game live in session 6682
(`native-virtual-245`). Profile unchanged, pad neutral; menu/nav not yet verified.
See [245](knowledge/experiments/2026-10-14-fp-zero-divide-245.md).

2026-10-14 (end of 245, new 246): the real game 245 executed the supported
zero-divide (FPSCR ZX) and stopped at the internal switch case 13 `0x69dd30`.
Final original PT_LOAD/artifact/base/manifest verification OK, last frame
still the loading planet. The whole original 14-entry table `0x69db38`,
the unsigned-byte bound, signed-relative offsets and all 12 distinct targets verified.
Profile +12 seeds, runtime identical to the tested 245. New 246 compiled,
input identity verified and the real game live in session 6422, pad neutral.
Menu/nav still not proven. See [246](knowledge/experiments/2026-10-14-byte-switch-246.md).

2026-10-14 (handoff 246): the user cancelled the automatic goal and requested a handoff.
246 terminated with Ctrl-C (session 6422 exit 130, guest rc unknown) at 266 reads /
13,048,193 bytes, 17 captures, still the loading planet. No new semantic
stop, case 13 not yet verified. The interruption manifest independently verifies PT_LOAD,
source/profile/base/diagnostic hashes, logs and captures; logs do not grow after the end.
No live build/run handle issued by us. Menu/nav not fulfilled. Full verifiable
[handoff 246](knowledge/history/runtime-handoff-246.md), current CURRENT/HANDOFF/INDEX
updated; the next run only upon authorization, a new unused directory 247.

## 247–255 (2026-10-14 evening, Claude Opus 5.5 + codex GPT-6.1 Sol subagents)

247 passed case 13; translation of all 34,512 OPD functions from the original `.opd` (2.75 M instructions),
switch detector 159→337/363 + 23 tables reconstructed by a subagent, new PPU/VMX/FP instructions,
split-safe FS/memcpy, libc, cellAudio (null sink), trylock/atexit/malloc, SDL window (`ANYPS3_PRESENT=sdl`),
~2–5× faster `GuestMemory`. Furthest 252/253: 541 M steps, 67 frames, still the loading planet;
stop 253 = untranslated SPU job CollNarrow. 255 (with SPU overlays) not fully compiled — compilation of
a single ~458 MB TU is the main brake. See [record](knowledge/experiments/2026-10-14-full-opd-coverage-247-250.md).
Neither the menu nor gamepad navigation reached yet.

## 256–260 (2026-10-15, Claude Opus 5.5 + codex GPT-6.1 Sol subagents)

The generated translation split into 193 parallel units (`units.json`; build ~4 min instead of an unfinishable
LTO TU). 256 passed CollNarrow, stop at the missing job CollBroad → profile 257 derives 138 SPU jobs from the original
data references. 257 stop: `lwarx` at the end of the libio segment → PPU reservation only over the bytes read (258).
258 stop: `sys_config_open` from native libio (cellKb/cellMouse) → HLE Kb/Mouse without devices
and `cellPadSetActDirect` without an actuator (260). Codex: `_sys_*printf`, bit-identical RSX draw speedup.
**260 furthest: 1,214 M PPU steps in ~11 min**, still the loading planet; stop at an SPU FP instruction
(`fcgt`), a static scan found thousands of missing SPU FP instructions — implementation delegated.
See [record 256–260](knowledge/experiments/2026-10-15-split-units-256.md). Neither the menu nor gamepad navigation reached yet.

## 261–284 (2026-10-15, Claude Opus 5.5 + codex GPT-6.1 Sol subagents)

A blanket approach instead of waiting for individual stops: a static scan of 99 missing syscalls
and parallel agents FS/SYNC/SPU/RSXNET/SAVE/IMPORTS3/VSHUI/RSXSWEEP. Added: 42 SPU FP instructions
(frest/frsqest LUT from RPCS3, derived), DMA lists, SPU group window, SPU module table, `_sys_heap_*`,
NPMG/NPDR offline (missing EDAT → ENOENT), trophies (NPTR), atexit as guest calls, ESRCH for a
terminated thread (283), NV4097 semaphores 0x1d70/0x1d74 (284), RSX rasterization 3–5× faster
(~24 M steps/s).
**284: 1,034 M steps, stop `nonempty RSX image transfer requires a native blitter`**
(NV3089), still the loading planet. After 284: RSXSWEEP (NV0039/NV3089/NV308A + 272 methods, 373 tests OK),
VSH dialogs MSGD/savedata controlled by the pad, vmule/vmulo, mmapper metadata. See
[record 256–284](knowledge/experiments/2026-10-15-split-units-256.md). Neither the menu nor navigation reached yet.

## 285–311 (2026-10-15/16, Claude Opus 5.5 + codex GPT-6.1 Sol)

Details in [record 256–305](knowledge/experiments/2026-10-15-split-units-256.md) and
[record 311](knowledge/experiments/2026-10-16-oom-alias-fps-311.md). 296–298: first original screens of the
front end and navigation by the virtual pad (subtitles YES/NO). 302–306: two HW PPU threads, new SPU jobs, GPU
renderer (Vulkan graphics) on the RTX 4070 SUPER. **309: original main menu, virtual down arrow NEW GAME → LOAD,
START read** (frames `native-virtual-309/evidence-mainmenu-*.ppm`), ~0.7 FPS. 310/311 (RSXVK6/7) got stuck on
loading; 311 caused an OOM of the entire session — cause: NVIDIA compilation of a 125 kB fragment shader (quad emulation).
RSXVK9 is running (direct GLSL like RPCS3). Target FPS and physical gamepad not yet verified.

## 312–323 (2026-10-16, Claude Opus 5.5 + codex GPT-6.1 Sol)

Details in [record 311](knowledge/experiments/2026-10-16-oom-alias-fps-311.md). Target FPS of the game verified at runtime:
**59.94** (VSYNC without a cap). Performance of the original main menu: 309 ~0.7 → 316c ~1.7 → 317 5.9 → 318 6.1–6.4 →
319 9.75–9.99 → 320 11.0–11.3 → **322 15.66 FPS** (title screen in ~2 min instead of ~15). Main steps: direct GLSL without
compilation explosions (RSXVK9), GPU texture decoding/RTT/VP (RSXVK10–11), per-thread `usleep`, fast SPU blocks and FP/DMA
(SPUBLOCK, SPUPERF), persistent page protection (RSXVK12), aligned guest backings (320), deferred labels/reports
(RSXVK13), `-march=native`. Navigation by the virtual pad DOWN → LOAD verified in 318–322. Found: the GPU runs in P8
(210–285 MHz) due to bursty load; the conservative parallel SPU worker (PAR1) is slower in the game. PAR2 (lock-free
memory, a thread per SPU) and RSXVK14 (Vulkan pipelining) are running. Physical gamepad is not connected.

## 324–331 (2026-10-17, Claude Opus 5.5 + codex GPT-6.1 Sol)

Details in [record 311](knowledge/experiments/2026-10-16-oom-alias-fps-311.md). Concurrent architecture modeled on the Cell:
one SPU thread per SPU (`ANYPS3_SPU_THREADS=1`, PAR2–PAR5, parking of blocking channels), memory without a global
lock (atomic guest words, reservations via 128 B versions, page broker), game RSX worker (`ANYPS3_RSX_WORKER=1`,
PAR6–PAR7), Vulkan renderer with timeline submits, continuous submits and compute texture decoding (RSXVK14–19).
Serial mode 326 → 331: 24.4 → 25.8 FPS; **fully parallel 331tw: original menu 38–40 FPS**, navigation DOWN → LOAD
by the virtual pad verified. Target 59.94 FPS not yet reached; physical gamepad not connected.

## 332–337 (2026-10-17, Claude Opus 5.5 + codex GPT-6.1 Sol)

Details in [record 332+](knowledge/experiments/2026-10-17-menu-fps-332.md). Per-thread game profiles (eu-stack,
`profile_menu.sh`, `stall_probe.sh`, `nav_check.sh`) showed that the main thread spent only 3 % in translated PPU code:
PAR9 (cheap coordinator, blocking without exceptions), PAR10 (asymmetric hazards, PPU 16.9 → 5.6 ns/instr in the synthetic test),
RSXVK20/20b (GPU clears, one claim for surfaces, non-blocking labels), fix of the O(S²) `GpuPages::commit_spans`
(~25 s stalls), PAR11 (metadata without gate/membarrier, CPU dirty without RPC) and RSXVK21 (FIFO race with CPU clear,
stable watches, RTT reuse). **337: original menu 57.5–59.5 FPS (full 59.94 Hz rate in most intervals), RSX
thread 6 ms/frame, navigation by the virtual pad 10/10.** The apparent loss of presses was the game's auto-repeat at a 1 s hold.
Physical gamepad not connected.

## 338–345 (2026-10-17 evening, Claude Opus 5.5 + codex GPT-6.1 Sol)

Details in [record 338+](knowledge/experiments/2026-10-17-ingame-338.md). Build 338 (PAR12) could not be compiled
(> 10 GB per TU, OOM took down the session) → 339 (fast path out-of-line, menu PPU 11.1 ms/frame). Beyond the menu: NEW GAME →
original difficulty and brightness selection → system save dialog. SAVEFULL (codex) + integration: schema per the firmware, dialog
overlay, pad hook, fix of the channel condition mapping (PacketWrite waited for 0x86000500), mandatory `elmProgress` →
**first real save to HDD** (344), boot ListAutoLoad and **LOAD from the system list** (345, USRI getStat).
Window: FIFO → MAILBOX, menu in the window 20 → 59.9 FPS. The intro clip runs at 8–9 FPS (SPU) → SPUPERF2; at its end
`SPU code differs from static image/overlays` (SPU code outside the static image) → SPUDYN. Gamepad: virtual and kernel
uinput (evdev → SDL → cellPad) verified in the menu; physical HW not connected. Sound: the game reports 0 devices; development only silently
(absolute rule: never any audible sound from the PC) → AUDIOFULL2.

**2026-10-17/18 — playable level and performance (builds 359–374):** LOAD → `great_clock_a` → playable level, Clank controlled
by the virtual and the uinput gamepad (363). Level performance: ~1.5 FPS (359) → 4.6–5.3 (369) → 9.1 (372/373, PAR14 deadlock fix,
PPUGEN1 blocks) → **~10 FPS (374, PAR15 selective SPU RPC + PROF1 built-in sampler)**; target 59.94. A frame ≈ PPU 50 ms
+ RSX 55 ms without overlap → PIPE1, PPU memory helpers ~35 % → PPUMEM1, CPU RSX → RSXVK22 v24. Detail
`docs/knowledge/experiments/2026-10-17-ingame-338.md`.
End of 2026-10-18: **377 ~14.8 FPS in the level** (RSXVK22 v24 CPU RSX −42 %, SPUDYN2 relocated SPU code instead of an interpreter,
fix of the concurrent `sys_ppu_thread_exit`). The game waits ~36 % of the time on an engine counter (0x696c10, `usleep(30)`) → PIPE1.
The original designation as an RSX fence was not evidenced; PIPE2 subsequently identified SPU producers group2 and 4.


**2026-10-19 — continuation 379+:** original 377 artifacts/source/PT_LOAD re-verified;
379 = 377 + HLEFAST1, build rc0 and source match, original level 14.64 FPS/120 s.
380 = 379 + timer fix and atexit test, **15.02 FPS/134 s**, original level and virtual movement captured.
Both runs terminated by SIGKILL after the measurement, verification −9/stop null/PT_LOAD both match; 60 FPS not reached.
PIPE2 proves group2 and 4 engine completion counter to be an SPU MFC PUTLLUC.
In 380 group4 executes the original microprogram 0x27, all 5264 B verified against the EBOOT;
the full link to the dominant 27 ms wait remains open.
Confirmed scheduler fix: audio 100 µs must not mask an earlier PPU timer; 11 native scenarios PASS.
The obsolete atexit test now checks correct blocking of wait_kind 13 and state preservation, 2/2 PASS.
SPUFP2 delivery 27 targeted tests PASS, PPUMEM2 completed isolated tests/sanitizers; both not integrated,
game benefit not verified. The whole suite of the merged 380 not run. This round ends for continuation in another AI environment.
See [record 379+](knowledge/experiments/2026-10-19-performance-379.md).
Before the final handoff, the requested [research of PS3 recompilations](knowledge/experiments/2026-10-19-web-ps3-recomp.md) was completed:
6 specific ports including Drakengard3 and Soulcalibur II HD, 5 areas for further general optimizations,
34 public sources verified; none of their code was integrated in this round.

## 2026-10-19 afternoon — FAST wave (Claude Opus 5.5)

User goal: the game at 60 FPS with headroom. Verified that the level runs in game mode 0 (target 59.94 FPS, no cap).
Baseline measurement with the new `measure_level.sh`: build 380 **14.25 FPS** in `great_clock_a`. Three subagents (PPU translation+memory,
SPU blocks+FP, RSX draw cost) in isolated trees. See [FAST record](knowledge/experiments/2026-10-19-fast-wave.md).

End of session 2026-10-19 ~22:00: integration 386 = 21.35 FPS, **460 (386 + RSX2) = 22.59 FPS** in the original level
(PPU 2.06 → 0.42 ns/instr, SPU 12.6 → 2.2 ns/instr, RSX worker 38.7 → 28.3 ms/frame). 60 FPS not reached;
further progress in the [FAST record](knowledge/experiments/2026-10-19-fast-wave.md).

## 2026-10-19/20 — cloud optimization round (Claude Opus 5.5, cloud container without game data or GPU)

User goal unchanged (60 FPS with headroom). Branch `claude/happy-franklin-2eocfp`, commits `43c6b27..4b3723a` (`43c6b27` = snapshot 460).
**No game build, no game run and no FPS figure:** the best verified build is still `native-virtual-460`, 22.59 FPS in the original level.
Changes (each verified only by native regression tests, none in the game): PPU VMX host SIMD, rlwinm/rlwnm/rlwimi MB>ME wrap-mask
semantic fix, lazy FR/FI/FPRF, spin hints, same-page indirect dispatch; SPU pool-lane slice 4096, exact change-counter filter for the
reservation-lost check, busy-poll parking (`ANYPS3_SPU_POLL_PARK=0`); single-mapping reservation fast path (lwarx/stwcx., getllar/putllc);
RSX register epochs, SIMD index decode, MSAA plane runs (`ANYPS3_RSX_PLANE_RUNS=0`) and a stale test oracle fixed; RSX owner thread no
longer spins on `wait_until(steady_clock::time_point::max())` with the cloud toolchain's libstdc++. Synthetic cloud-CPU benchmarks only
(for example VMX kernel 18.3–18.9 → 1.29–1.33 ns per guest instruction, atomic kernel 68.9–71.1 → 39.6–41.8, 2× MSAA draw wall time
276.3 → 97.9 µs under Lavapipe). Open hypothesis: MSAA plane runs cut part of the ~30 ms/frame GPU busy time. First next
action on the user's PC: new build N, `measure_level.sh N Na`, A/B runs with `ANYPS3_RSX_PLANE_RUNS=0/1` and `ANYPS3_SPU_POLL_PARK=0/1`.
See [cloud optimization record](knowledge/experiments/2026-10-19-cloud-optimization.md) and [CURRENT](knowledge/CURRENT.md).

**2026-10-20 FAST2 wave (Claude Opus 5.5 + Sonnet 5.5 agents, Codex gpt-6.1-sol reviews; PC with the game).** Original level
`great_clock_a` 22.59 → **46.68 FPS** (build 472 = main b3281ed, `native-virtual-472a`, correct image, no STOP; median 45.97, max 58.0).
Steps: cloud round measured (470a 29.70: MSAA plane runs etc., GPU 31 → 13 ms); GPU uploads on the DMA queue; RSX vertex reads without a
per-draw membarrier claim (rsx_worker 22.9 → 18.1); GPU alias merge for 1/2-byte surfaces removing 5 readbacks + finish waits per frame;
FP loader/blit/plan caches (rsx_worker → 13.4); scheduler fast pass. Refuted: FIFO prefetch window, timer slack 1 ns, register residency
across SPU blocks; lane 0x100 is not on the critical path (the main thread waits ~6.6 ms/frame for ig SPU jobs). 60 FPS with headroom
not reached; whole suite over 472 not run. See [FAST2 record](knowledge/experiments/2026-10-20-fast2-wave.md) and [HANDOFF](knowledge/HANDOFF.md).

**2026-10-20 sandbox analysis (Codex, documentation and offline evidence only).** Rechecked the later artifacts:
603a **54.045 FPS**, independent pacing candidate 642a **52.405 FPS**, integration 473 unbuilt.
Main is 472 + ZeroTable; the suite over 472 did run (813 tests, one timeout module).
Source/artifact hashes and original PT_LOAD bytes match. New analysis of saved 603w samples estimates
3.19 + 1.05 = **4.24 ms/frame** in the engine-counter/job-flag wait regions; 45.6% of engine-counter wait samples
have all five ig lanes sampled and classified as parked. Investigate completion publication/wakeup as well as SPU execution.
Documented integration fixes, the unbuilt AVX DMA concurrency concern, and actual wrap of 642a's diagnostic ring.
Proposed next candidate: corrected integration + SPU through `87dce76`, recorder OFF, without the late AVX DMA patch.
No new game/test/build or runtime/profile changes. Host processes inaccessible in this sandbox.
See [analysis](knowledge/experiments/2026-10-20-sandbox-analysis.md) and the updated [CURRENT](knowledge/CURRENT.md).

**2026-10-20 FAST2 wave 4 (to ~22:25, ended on user instruction).** 660a **55.96 FPS** (integration of RSXCPU3 +
FRAMETIME + rsxblit batch path; first 30 s 32.7 FPS, sustained 30 FPS after 12 s). The post-load slow phase was CPU-side
scaled blits, not GPU waits. Main-thread SPU wait is 94 % lane compute (wake trace). Build 473 (all combined + review fixes)
unmeasured; blit batch review findings (1 High) not yet committed. See [CURRENT](knowledge/CURRENT.md).

2026-10-23: higher-resolution work on feat/render-scale (baseline 706).
Original menu visually verified at 37 s using earlier virtual START; original
segments unchanged. Raster extents, guest coordinates and two-pass RTT detail
were tested on NVIDIA. Game rendering above 720p and stable 60 FPS remain pending.
See knowledge/experiments/2026-10-23-render-scale.md.

2026-10-23: build801 original Great Clock verified at2560x1440,14.22FPS; same binary native59.87FPS. Fast menu is default (36–42s). Confirmed render-pass alternation bottleneck; deferred visual batching under targeted validation, game result pending. See knowledge/experiments/2026-10-23-render-scale.md.

2026-10-23: batching802 diagnostics39.16FPS at1440p,41.23FPS with recorder; noSTOP/PT_LOAD exact, backgroundjobs on otherCCD so notquietacceptance. GUI render scale integration added to requiredscope.803 flip-timing build and802suite stilllive; one SPUfixturetimeout needsisolatedrecheck.

2026-10-23: quiet803 1440p45.5198FPS, original Great Clock inspected; no60FPS acceptance. Investigating confirmed native reconstruction paths before claiming full-resolution detail. GUI source integrated, desktop release unchanged.

2026-10-23 milestone final: build805 original Great Clock, same-flip720p/1440p captures inspected, original bytes exact/noSTOP. Feature branch rebased onto54cea81; final capture regression and complete GUI/manager checks pass. Best quiet scaled result remains45.52FPS; detail preservation and stable60FPS remain open.

2026-10-23 cloud follow-up (no GPU/game in the container): visual companions now keep subpixel history through alias merge, equal-footprint rebind and typeless-depth RTT; RENDER_SCALE queue counters fixed; paired draw reuses the native pipeline. Verified only by lavapipe Vulkan unit tests with validation; no new FPS or game evidence. See knowledge/experiments/2026-10-23-cloud-render-scale.md.

2026-10-23 FIX7: START crash = untimed-only semaphore wait (fixed, 810); pause blur needs texture-feedback GPU path
(fast2/fix7-pause, unbuilt). GUI toggles/keys/skip intro; save-menu audio + two windows (fast2/fix7-audio); stutter =
CPU scaled blits (fast2/fix7-stutter). See knowledge/experiments/2026-10-23-fix7.md.


2026-10-25: resumed higher-resolution walking performance work from merged main 124b7d4. Baseline native blit/Vulkan/cache checks pass; fresh build 870 compiling. Geometry-only blit-plan cache candidate under validation; no new game FPS result. See [record](knowledge/experiments/2026-10-25-scale2-walking.md).

2026-10-25 cleanup (two rounds): local/ratchet 169 → 88 GB after Codex 871–883 (old build binaries, bundles, duplicates, scratch caches, object cache before 10-23; logs/rings zstd-compressed; source snapshots except 472/706/805/820/860/870 archived in local/ratchet/archive/). CURRENT/HANDOFF sections before 2026-10-21 moved to knowledge/history/. See knowledge/CURRENT.md.

2026-10-25 follow-up: first merged/candidate 1440p walking diagnostic 871 measured 23.23 FPS over the whole walk (concurrent suite, not acceptance), exact original PT_LOAD/no STOP. Isolated fixes verify SPIR-V 1.3 disk-cache reuse and removal of eight-entry blit-plan thrashing (11 plans/transfer); native/Vulkan tests pass, game builds 873/874 pending. See `docs/knowledge/experiments/2026-10-25-scale2-walking.md`. Goal remains 60 FPS at higher resolution without movement shader stutter.

- 2026-10-25: scale-2 walking diagnostic 871layoutdiag1 reached original Great Clock (26.22 FPS with concurrent build/suite; not acceptance). CPU samples motivated isolated history-geometry reuse; candidate 875 passes NVIDIA native/subpixel checks in both modes and detects fixed-hash/word-map regressions. Frozen 875 queued after 873/874; original game bytes and installed release unchanged. [Evidence](knowledge/experiments/2026-10-25-scale2-walking.md).

- 2026-10-25: timestamped CPU samples locate ~200 samples of history planning inside several 236–368-ms walking frames. Frozen candidate 876 coalesces exact adjacent rows before sorting; independent 675,840-texel benchmark drops cold planning from ~53 ms to ~4.2 ms, all four-mode NVIDIA image tests pass. Build 873 completed; game diagnostic verifies blit hits/cache rejection fix, full walk pending. [Evidence](knowledge/experiments/2026-10-25-scale2-walking.md).

- 2026-10-25: 873cache64diag1 completed at 1440p, 24.81 FPS whole walk under concurrent load, exact original PT_LOAD/no STOP. Verified >99.7% blit-plan hits and zero shader-cache rejection, but ten measured-window shader compiles and substantial CPU-blit/readback cost remain. 874 built; 875 building / 876 queued. Goal remains open. [Evidence](knowledge/experiments/2026-10-25-scale2-walking.md).

- 2026-10-25: 875historydiag1 verifies history/source-cache reuse, 25.73 FPS at 1440p under concurrent load; warmed GLSL compile zero but compiler samples remain during first movement. 876 cold planner and 877 bounded source-surface diagnostics built; 877 game run live. Two pre-existing feedback-oracle failures corrected in isolated tests and mutation-checked; root suite still running. [Evidence](knowledge/experiments/2026-10-25-scale2-walking.md).

- 2026-10-25: walk 877 verified at 1440p / 29.151 FPS under suite load, exact original segments/no STOP; shader compilation persists during movement. Identified tiled D24S8→packed-byte scaled copies. Isolated GPU primitive 878 passes exact native/scaled image oracles; direct depth-output variant 879 passes 18 exact image cases plus chained GPU consumption/capacity rejection, not wired into game yet. [Evidence](knowledge/experiments/2026-10-25-scale2-walking.md).

- 2026-10-25: candidate 880 GPU depth-copy ownership passes exact image/coherence tests and is compiling from a frozen snapshot; baseline 871 finishes with seven explained test failures, retained in full. Root integrates the tested runtime and fixture corrections. Five of six newly compiled 877 fragment shaders traced to unused sampler specialization; isolated 881 tests in progress. No new FPS or zero-stutter acceptance. See [walking record](knowledge/experiments/2026-10-25-scale2-walking.md).

- 2026-10-25: candidate 881 passes shader-identity and actual GPU color/counter tests plus graphics/cache regression tests. Implicit filter/mip changes reuse shaders; default source cache retains the measured working set. Frozen build queued after 880; full 1071-test integrated suite started. No new game FPS result yet.

2026-10-25: Process-local traces of original 1440p movement identify a 1498-ms frame with 672-ms GLSL work and 519-ms pipeline creation. All 40 newly cached sources match older identities after the tested 881/882 sampler-state changes. Isolated 882 passes independent resident colors (32 cases per mode) and broader graphics tests; game builds and root 881 suite remain live. No 60-FPS or zero-stutter claim. See [walking record](knowledge/experiments/2026-10-25-scale2-walking.md#882-resident-state-reuse-and-exact-compiler-traces).

2026-10-25: Build 880 original 1440p walk verified: 34.889 FPS under concurrent jobs, no STOP/exact PT_LOAD; GPU depth-copy path active, warmed CPU blits 0.689 s / 84 readbacks (earlier diagnostic 13.872 s / 1890). Eleven movement GLSL compiles remain. 881 built/verified and running; 882 building, root suite active. Goal not achieved. [Evidence](knowledge/experiments/2026-10-25-scale2-walking.md#880-real-game-result-881-compiled).

2026-10-25 closeout requested by user: commit performance milestone through verified build 882; original 1440p whole walk 31.154 FPS, five first-use GLSL compiles, exact PT_LOAD/no STOP. Full suite intentionally stopped at 603/1071 tests (four skips, no completed failures), final-source full validation pending. All own jobs closed; 60 FPS/zero-stutter goal not achieved. [Closeout](knowledge/experiments/2026-10-25-scale2-walking.md#milestone-closeout-requested-by-user).

2026-10-25 evening HIRES wave: 1440p walking baseline 882 = 33.15 FPS (720p 44.68); 1440p image was 720p-derived (MSAA sample-grid layout bug, fixed on hires/integ, unmeasured). Per-draw CPU and Vulkan cost cuts merged, unmeasured; build 960 queued. See knowledge/experiments/2026-10-25-hires-wave.md.

- 2026-10-27: HIRES2 wave (two Opus agents, fresh instances per ~300k context; power outage at 16:53). 1440p walking 1031 39.91/40.58 FPS vs main 37.27/35.89, GPU 15.4 vs 19.0 ms; remaining blockers RSX worker (~21 ms heavy frames), main guest thread (~14.8 ms, ~3.9 ms SPU-job wait) and scale-2 GPU (~15.4 ms). Goal open. Branch `claude/hires2-wave`. [Record](knowledge/experiments/2026-10-25-hires-wave.md).

- 2026-10-28: cloud round HIRES3 (no game/GPU): device-memory pool for the 1440p alloc/free hitches, three exact per-draw CPU
  cuts and typed-RTT literals on branch `ccr-8139488c-79xg7b`, lavapipe-tested with mutation checks; nothing measured in the
  game. [Record](knowledge/experiments/2026-10-28-cloud-hires3.md).
- 2026-10-30/31: cloud round HIRES4 (no game/GPU): three Opus agents cut the RSX worker's host instructions exactly —
  per-draw path, FIFO decode and actions, renderer-thread guest-memory admission (page cursor, one-page reads, Range
  creation; switches default on). Callgrind on a game-shaped stream: −37 % worker Ir/frame with 500 indexed draws, −51 %
  between draws; derived heavy-frame worker ~15–16 ms, still above 13 ms; nothing measured in the game. Branch
  `ccr-b1a3987e-aypb5o`. [Record](knowledge/experiments/2026-10-30-cloud-hires4.md).
- 2026-10-31: knowledge-base cleanup (user request): CURRENT/HANDOFF sections of 2026-10-21 to 2026-10-25 and the superseded
  runtime handoffs 198–246 moved to `docs/knowledge/history/` (links rebased), a dated summary at the top of CURRENT; cloud
  container: merged agent worktrees/branches and finished-round bench builds removed. The PC's `local/ratchet` builds were
  not reachable from the cloud session and are unchanged.
