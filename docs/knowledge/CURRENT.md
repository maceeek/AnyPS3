# Current state and goal

**2026-10-19 afternoon — new round (Claude Opus 5.5), user goal: the game at 60 FPS with headroom.**
Verified: in the `great_clock_a` level the game runs in mode 0 (VSYNC without a cap, `G+84=0`, `G+24=0`) → target 59.94 FPS;
~15 FPS is a performance plateau, not a game cap. Baseline measurement `measure_level.sh 380 380base`: **14.25 FPS** (median 14.97),
main PPU 43 ms/frame (2.06 ns/instr, 21 M instr/frame), RSX worker 38.7 ms/frame (~39 µs/draw), SPU ~10 ns/instr.
**End of session 2026-10-19 ~22:00: best verified build `native-virtual-460` (= 386 + RSX2) — 22.59 FPS**
(median 23.0) in the original level, correct image, no STOP; 386 = 21.35 FPS. The bottleneck is the RSX worker (~28 ms/frame),
followed by the GPU at ~30 ms/frame at low clocks. Suite over 386: 757 tests, 751 OK (3 = one SPU regression fixed in the
SPUFAST tree, 3 = compilation timeouts). 60 FPS not reached. No agent or build is running.
**Live tree = snapshot 460** (promoted 2026-10-19 ~22:40 at the user's instruction; source hashes = build 460) and it is the
first git commit (`origin` https://github.com/maceeek/AnyPS3.git, branch `main`). Known: 3 tests `test_par1/2`
(missing "unknown native SPU image" check, fixed in `local/ratchet/work/spufast/tree`).
FAST wave (subagents in `local/ratchet/work/`, closed); plan, tools, evidence and first next actions:
[FAST record](experiments/2026-10-19-fast-wave.md). The state below (handoff 380) remains valid as the baseline.

Updated 2026-10-19, final handoff (Codex + GPT-6.1-sol High subagents).
**At the user's instruction this round is being ended and the continuation is handed to another AI environment. The active
goal was cancelled by the user; the project requirement of the original game with controls and headroom for 60 FPS is
therefore not met.**

**Last verified build: `native-virtual-380`, sources `source-snapshot-380`.**
380 = 377 + HLEFAST1 + scheduler deadline fix + corrected expectation of the atexit test.
The live runtime/tests/profile match snapshot 380; 117 source hashes and runtime artifacts verified.
Real run `native-virtual-380load`: original menu → LOAD → `great_clock_a`,
**15.02 FPS over 67 intervals in 134.067 s**, min 13.5/max 16.0. Clank demonstrably moves
with the virtual pad (`evidence-input-montage.png`). After the measurement and input, controlled termination by SIGKILL;
`verification.json`: returncode −9, stop null, both original PT_LOAD segments identical, 34 captures.
Build 379 in the same scene 14.64 FPS/120.08 s. A difference of ~2.6% from a single run per variant is not conclusive evidence of a performance gain.
The menu, the real level and virtual input are verified; **60 FPS and a physical gamepad are not verified**.
Uinput via SDL was verified earlier in 363; this round did not repeat it.

**Concrete blocker:** performance of the original level. PIPE2 showed the SPU to be the producer of the engine completion counters
group2 and 4 via MFC PUTLLUC. During the group4 wait the SPU executed original microprogram 0x27
(EA 0xbbf480, LS 0x26c00, all 5264 B identical to the EBOOT), FP normalization code.
In this group `spu_single_vector` + clones 27.95% CPU; the full link to the earlier 27 ms wait is still not proven.
The original claim "the RSX fence is the cause of the wait" is not substantiated.

**Prepared, not integrated:** SPUFP2 (27 targeted tests PASS, synthetic AOT 1.31–1.37×),
PPUMEM2 (validation and sanitizers completed, untracked AOT ~2×, but hot-tracked +46.6% time).
Neither candidate is part of 380 and neither has a game FPS figure. **The full regression suite of the merged 380 was not run.**

**First next action in the new environment:** take over [HANDOFF.md](HANDOFF.md), verify the current processes and SHA,
then assess the small SPUFP2 delivery against snapshot 380; after any integration a new full suite and a real run
of the same level. Further options are provided by the [web research of PS3 recomp projects](experiments/2026-10-19-web-ps3-recomp.md).
Exact results, commands, artifacts and limitations: [record 379–380](experiments/2026-10-19-performance-379.md).
Host audit 2026-10-19 09:59 UTC: no game of our own, compilation, debugger or running AnyPS3 scope;
`local/ratchet/handoff380-process-audit.json`. Verify the state again upon the new takeover.

**Absolute machine rule (user, 2026-10-17): never any audible sound from this PC (a baby is sleeping in the room).**
Sound only via SDL `dummy`/`disk`; `native_audio.py` without `ANYPS3_AUDIO_AUDIBLE=1` refuses the real
device (never set it). The PipeWire sinks are muted. Applies to the main agent and all subagents.

## Where the game is

The original game passes through boot, videos (skippable with START), the `front_end` loading, the title screen and reaches the
**original main menu** (NEW GAME / LOAD / COMMUNITY / OPTIONS / DELETE) ~2 min after start; the menu responds to the
virtual pad (down arrow → LOAD). Rendering: custom Vulkan RSX (NVIDIA RTX 4070 SUPER), PPU/SPU statically translated.
The historical performance of the menu in build 339 was a median 59.94 FPS; this figure is not the performance of the level in build 380, which has 15.02 FPS.

## Split compilation (since 256)

`build_game` writes `units.json` + `anyps3.hpp`, `program.cpp` (runtime, main),
96 `ppu-*.cpp` and 96 `spu-*.cpp`; `virtual_pad.py build -j 24` compiles in parallel in **~4 min**
(previously a single 458 MB TU, LTO 7–15+ min). See [record 256](experiments/2026-10-15-split-units-256.md).

## History of real stops (256–339; the current state is above)

- **256**: `SPU code differs from static image/overlays` (SPU 0x200, LS 0x7270): job CollBroad
  `0xb9bd80` was missing from the profile, CollNarrow truncated. Profile fix 257 (138 jobs from the original data references,
  `local/ratchet/tools/spu_job_overlays.py`).
- **257**: `pc=0x202a9a4c` `lwarx r9,0,r31` (r31 `0x202b0c2c`), 541,769,959 steps: the reservation granule
  extends past the end of the data segment of the firmware module (`0x202b0000+0xc38`) → `unmapped or split guest memory
  access size=128`. Fix 258: a PPU reservation requires mapping only of the bytes read; a granule shared
  with another region = explicit stop; test `tests/test_memory.py::PartialGranuleReservationTests`.
- **258**: `sys_config_open` (syscall 516) from the native libio — the game calls `cellKbInit/cellMouseInit`
  and `cellPadSetActDirect` outside the HLE pad. Fix 260: `anyps3/native_hid.py` (Kb/Mouse library state, 0 devices),
  `cellPadSetActDirect` without an actuator → `UNSUPPORTED_GAMEPAD`; tests `tests/test_native_hid.py`.
  Concurrently codex: `_sys_*printf` and a bit-identical RSX draw speedup (`local/ratchet/codex/*-result.md`).
- **260**: 1,214 M steps in ~11 min; SPU FP stop → codex 42 instructions + frest/frsqest LUT + rchcnt (261).
- **261–267**: DMA lists, empty list, SPU group window `0xF0000000`, SPU module table `0xde3600`
  (LS base = hypothesis), `NTCL getstate` (netctl disconnected), parallel RSX (codex). From 263 the run does ~24 M steps/s
  (884 M in 36 s). Details: [record 256–267](experiments/2026-10-15-split-units-256.md).
- **268–280** (parallel batch, see record): heap, NPMG offline, firmware OPD seeding, switch tables
  with module TOC and `lwax`, `lswi/stswi`, `tw/twi`, NPDR drmrequest (missing EDAT → `ENOENT`), atexit callbacks
  as guest calls (`ATEXIT_RETURN` trampoline), 5+2 parallel codex groups (FS/SYNC/SPU/RSXNET/SAVE,
  INTEGRATE, IMPORTS3) via `anyps3/lv2_*_ext.py`.
- **281–284**: NPTR trophies, configurable sleep cap, `get_priority` → ESRCH for a terminated detached
  thread (the game uses it to detect the end of "Check Thread"), NV4097 semaphores 0x1d70/0x1d74.
- **284**: 1,034,431,488 steps, `nonempty RSX image transfer requires a native blitter` → codex RSXSWEEP
  (NV0039/NV3089/NV308A, 272 methods) and VSHUI (MSGD/savedata dialogs driven by the pad).
- **285–286**: regression to 150 M — new stop on Z-cull statistics (`GET_REPORT` type 2). 287: semantics of types 2–5
  taken from RPCS3 `RSXZCULL.cpp:337-373` (**derived**, counter approximation; here 0).
- **287–288**: `map::at` → 0x1d6c (`SET_SEMAPHORE_OFFSET`) is never written by the game. From 289 unwritten RSX
  registers have reset value 0 (RPCS3 `rsx_methods.cpp:137`; the other nonzero default values there are
  `cellGcmInit` commands, which the native libgcm executes here). Tests in `tests/test_rsx.py`.
- **289** (last): 1,042,889,216 steps, `RSX fragment depth export is unsupported`. The game already loads
  **`packed/levels/front_end/level_{cached,uncached}.psarc`** (menu level), checks the PARAM.SFO of other Ratchet games
  and began rendering into 960×704 MSAA (samples=2). Captured frames so far show the loading planet.
- After 289: PPU `vadd{u,s}{b,h,w}s`, `vsub{u,s}{b,h}s`, `vsubsws` (test `test_ppu_string_trap.py`).
  Codex **RSXGAPS** is running (MSAA, target formats, primitives, clipping, complete FP/VP opcodes incl. depth export,
  textures) and **SPUGAPS** (stop codes 0x0/0x10/0x203/0x3fff, instruction 0x51cef5e8, static SPU scan).
  Source baseline before the agents: `local/ratchet/source-baseline-290/`.
- **290–293**: swizzled 128×128 clear solved (RSXCLEAR/RSXSWZ/RSXBLIT2 done); 293: 1,048 M steps, texture remap stop → codex RSXTEX is running. Earlier:
  RSXCLEAR, RSXSWZ, RSXBLIT2 (see record 256–291); RSXGAPS and SPUGAPS done.
- **294–296**: 296 normal 1,121 M steps (split RSX read stop `0x49ac8c00`); **296-diag (RSX skip, not evidence)
  showed the original screen "Would you like to turn subtitles on? YES/NO"**. The game ignored the virtual pad there →
  cause: missing `CELL_PAD_CAPABILITY_PS3_CONFORMITY` (fixed, not yet built). RSXTEX3, RSXMEM are running.
- **297-diag (0 RSX skips)**: the virtual pad navigates the original subtitles screen (right → NO, cross → "initial
  setup… CONTINUE"); after CONTINUE a stop on an untranslated switch in libsysutil → detector extended (298). The normal run 298
  with `ANYPS3_PAD_TRACE=1` is running. Main menu not yet reached.
- **298 (normal run) — verified navigation by the virtual pad on the original subtitles screen** (right → NO, left → YES,
  cross → "initial setup" → cross → stop NPCU `cmTerm`). Main menu not yet reached; FPS ~0.5 flips/s.
- **Performance**: ~99 % CPU in the SW renderer, 0.5 flips/s. Priority: **GPU (Vulkan, RTX 4070 SUPER) backend** —
  codex RSXVK; CPU optimization RSXPERF. 299 (NPCU cmTerm) is running.
- **300**: stop `updateGameData` (installation of game data "initial setup") → codex GAMEDATA; the GPU backend works
  bit-for-bit correctly on the RTX 4070 SUPER, but so far slowly → codex RSXVK2 (surface cache).
- **303–306 (2026-10-16)**: the scheduler emulates **2 HW PPU threads** (the main thread no longer starves); SPU jobs "Bink",
  "PostProcess", "Composite", "SyncJobs" added to the profile; zero clip = no-op; dithering without dither.
  **GPU renderer (Vulkan graphics, RTX 4070 SUPER)** enabled: `ANYPS3_RSX_VK_BUILD=1` at compile time,
  `ANYPS3_VK_DEVICE=gpu ANYPS3_RSX_BACKEND=gpu VK_INSTANCE_LAYERS= ANYPS3_FRAME_STRIDE=600` at run time; loading up to
  ~46 flips/s, the original epilepsy warning displayed (305). Remaining brake: GPU fallback to CPU for the states
  logic op/fog/stipple/shader packer/two-side/window → codex RSXVK5 (parity). Readback via HOST_CACHED memory.
- **309 (2026-10-16): original main menu reached and navigated by the virtual pad** (NEW GAME → LOAD with the down arrow;
  START on the title screen). But the menu runs at ~0.7 FPS (target 60) → profile + codex RSXVK7.
- **311 (2026-10-16): OOM** — systemd-oomd killed the terminal scope at 07:55 (session, run 311, codex RSXVK8).
  Cause **verified** during 311m/311h/311f: on the 12th frame of loading, `vkCreateGraphicsPipelines` makes NVIDIA compile a
  fragment shader of 125 kB GLSL (169 FP instructions, emulation of 4 quad lanes per invocation) → ≥ 20 GB RAM; `spirv-opt -O`
  reports an ID overflow on it. 309 sent these FP shaders to the CPU fallback. The alias draw (color = depth) is purely depth-only
  (`color_mask=0`). Target FPS: selector `0x661c50` (mode 0 = VSYNC ≤ 59.94; mode 2 = 29.97; …), see the record.
  RSXVK8 done (LRU cache, persistent SPIR-V/pipeline cache, 474/474 tests); **codex RSXVK9** is running (direct GLSL
  FP like RPCS3, VkImage textures, neutral color alias). Diagnostic builds 311d/f/i from the snapshot
  `local/ratchet/source-snapshot-311d` (not evidence), integration base `local/ratchet/source-snapshot-312`.
  The game and the agents now run in `systemd-run --user --scope -p MemoryMax=…`.
- **313 (2026-10-16 14:45, snapshot `source-snapshot-313` = RSXVK8+RSXVK9+PPUBLOCK):** normal run (no diag.
  skips, CPU fallback disabled) passes through loading past the earlier FS 27 within 2 min, RSS ~1.1 GB, 27 pipelines.
  `PERF`: ~2 flips/s, ~125 draws/s, PPU ~1.7 M instr./s; host ~1.7 ms + flush ~2.3 ms per draw. Profile 313p:
  `decode_texture` 34 % (CPU decoding of textures on every draw, `rsx_draw.py` ~line 1207) → **codex RSXVK10**.
  Full suite over snapshot 313: 481 tests, 479 OK; `test_anyps3` OK after adding `profiles/` to the copy,
  `test_rsx_scalar_fp` exceeded the compilation time limit under load (3.3 s vs 0.9 s without load) — repeat in quiet conditions.
- **316c (2026-10-16 16:05–16:30, snapshot `source-snapshot-316`, normal GPU run):** fixed `usleep` + RSXVK10 →
  **title screen in ~15 min and main menu in ~20 min** (309: ~45 min), videos skipped by the virtual START,
  menu frame `native-virtual-316c/frames/frame-000022.ppm`, virtual down arrow read (`digital1=0040`).
  **Menu ~1.7 FPS** (~78 draws/flip). Menu profile `local/ratchet/codex/rsxvk/game-profiles/menu-316c.*`:
  `GuestMemory::range` 26 %, `ioctl` 10 %, texel/texture_image/lroundf ~18 %; counters: texture miss 436 k / hit 74 k,
  `invalidate_gpu_write` 173 k, flush unprotectable 222 s, host_read 148 s → render-to-texture via the CPU.
  **Codex RSXVK11** (RTT on the GPU, unprotectable flush, GPU VP, exact FS) and **SPUBLOCK** are running.
- **317 (RSXVK11+SPUBLOCK):** title screen and menu ~5.9 FPS (4× compared to 316c). **Target FPS = 59.94 verified at
  runtime** (`G+84=0` VSYNC without a cap). ~10× is missing: RSX host ~45 %, SPU FP/DMA ~23 %, all on a single host thread.
  Codex **SPUPERF** is running; next: RSXVK12 (steady-state draw cost in the menu, menu capture via `ANYPS3_RSX_CAPTURE_TRIGGER`),
  then parallelization of SPU/RSX onto more host threads.
- **318:** menu 6.1–6.4 FPS. **319** (RSXVK12+SPUPERF+PAR0): menu **9.75–9.99 FPS**, title screen after ~4 min.
  **320** (aligned `GuestBacking`, allocator-edge readbacks gone): menu **11.0–11.3 FPS**, DOWN → LOAD verified.
  `PERF WALL` per frame: RSX CPU ~37 ms, GPU wait ~36 ms, SPU 8.5 ms, PPU 7.6 ms. **The GPU runs in P8 at 210 MHz**
  (the driver treats bursty load as idle); with the GPU pulled up to P0 (concurrent vkcube) menu ~15 FPS, gpu_wait 5.6× smaller.
  Hold START on the title screen for ~2 s; after ~60 s of inactivity the title screen starts the attract video (START → back).
  Terminate the program with `kill -KILL <program pid>` (SDL catches SIGINT/TERM) → the wrapper writes `verification.json`.
- **322** (RSXVK13 + PAR1, `-march=native`): menu **15.66 FPS** ~2 min after start; RSX CPU 21.9 ms, GPU wait
  25.1 ms (`label_reader`), SPU 9.1, PPU 7.9 ms/frame. **323** (SPU worker relaunch fix): with
  `ANYPS3_SPU_WORKER=1` only 9.99 FPS (PAR1 memory locks) → PAR2. To the menu: `local/ratchet/tools/to_menu.sh DIR`.
- **325s** (PAR2 + RSXVK14 + pool and wakeup fixes, serially, clean measurement): menu **21.94 FPS**, PPU 7.3 / SPU 5.3 /
  RSX 16.2 / GPU wait 16.0 ms per frame; DOWN → LOAD. `ANYPS3_SPU_THREADS=1` reaches the menu, but ~10 FPS (the lanes actively
  poll the reservation event, false sharing of labels with the FIFO) → PAR3/RSXVK15 and the follow-up wave.
- **326** (PAR3 + RSXVK15): serially menu **24.42 FPS** (PPU 6.8 / SPU 4.8 / RSX 17.0 / GPU wait 9.6 / vblank 3.0 ms);
  `SPU_THREADS=1` 10.75 FPS (polling of the LR event). PAR4 (SPU wait, false sharing, RSX worker) and RSXVK16 (≤ 20 µs/draw) are running.
- **327** (PAR4 + RSXVK16): serially 21.83 FPS (PPU regression 7.46 → 9.39 ns/instr with an active observer);
  `SPU_THREADS=1` 15.18 FPS (lanes park; `sched_yield` 33 % because of the legacy RawPermit). Best verified: **326
  serially 24.42 FPS**. Measurement: `local/ratchet/tools/measure_menu.sh`. PAR5 + RSXVK17 are running.
- **328** (PAR5 + RSXVK17, suite 564/564): **serially menu 24.59 FPS** — best verified state (PPU 6.6 / SPU 5.4 /
  RSX 16.5 / GPU wait 9.5 / vblank 3.1 ms). `SPU_THREADS=1` 15.78 FPS. **329** (PAR6 RSX worker): default 22.34 FPS;
  `RSX_WORKER=1` gets stuck on loading (every HLE service → MapBarrier → Session rebuild). **330** (PAR7+RSXVK18,
  576/576): serially 23.98 FPS; `RSX_WORKER=1` to the menu 14.98 FPS (memory protocol ~55 % CPU). PAR8 + RSXVK19 are running.
- Physical gamepad: not connected to the computer (`/dev/input/js0` = ASRock LED Controller) → unverifiable.
- **331–337** (PAR8–PAR11, RSXVK19–21): menu 38–40 → **57.5–59.5 FPS**, navigation 10/10 — see [record 332+](experiments/2026-10-17-menu-fps-332.md).
- **338** (PAR12 inline scalar load/store): cannot be compiled — TU > 10.7 GB in GCC; OOM killed the session (17:07) and scope 338r.
  **339** = 338 + `runtime.py` (fast path out-of-line, `load()/store()` without `always_inline`): TU 1.0 GB / 24 s,
  build 8 min / 16.9 GB (−j16), PAR12 6/6 + PAR10 memory 10/10; menu median 59.94 FPS, PPU 11.1 ms/frame.
- **337game (2026-10-17 18:23)**: menu → NEW GAME → difficulty selection → brightness → `Save Thread` →
  `STOP pc=0x201142e8: SAVE missing attribute: elmSearchType` = `openSaveList` (the only builder; `getDirList` always sends
  `elmSearchType=0`, `0x2011cfa0`; `openSaveList` only in one branch `0x2011f514`). → codex SAVEFULL
  (`local/ratchet/codex/batch/task-savefull.md`); diagnostic 339d (attribute dump, not a fix).

## First next actions

1. Verify the handed-over files and processes; 379load and 380load are terminated, do not recycle old PIDs.
2. Read `codex/batch/spufp2-result.md` and `codex/spufp2/verification.json` under `local/ratchet/`.
   The candidate changes only `spu.py` and adds a test. Do any integration in a new snapshot, verify the baseline SHA.
3. After integration, the full regression suite of the immutable snapshot including the real Vulkan, a new game build,
   the same scene and a steady-state measurement. No FPS promise from a microbenchmark.
4. Before integration, evaluate PPUMEM2 by the hot-tracked regression and the real frequency of fallbacks;
   transfer only the delivery and a minimal program.py hook. Do not repeat the completed isolated validation without reason.
5. For further PIPE2 work, the complete wall-time link between a specific job and the wait is missing. Neither the GDB snapshot nor
   the global profile proves that the event polling of this group dominates. The research offers further general mechanisms.

## Commands

```bash
# Source snapshot + build (always a new N), translation with the host ISA:
# From a snapshot: PYTHONPATH=local/ratchet/source-snapshot-N python -P … (without -P `python -m` picks up the live tree from cwd!)
python -P -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 1000000000000 -o local/ratchet/native-bundle-N
ANYPS3_EXTRA_CXXFLAGS='-march=native -ffp-contract=off' ANYPS3_RSX_VK_BUILD=1 PYTHONPATH=. \
  python local/ratchet/tools/virtual_pad.py build -j 16 local/ratchet/native-bundle-N local/ratchet/native-virtual-N
# Run to the menu + measurement + DOWN + SIGKILL (writes verification.json); the same binary in a copy of the directory for other modes:
bash local/ratchet/tools/measure_menu.sh N 0-5,12-17 ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1
python local/ratchet/tools/menu_stats.py N 10
# Full suite (4 processes, real Vulkan):
LP_NUM_THREADS=4 python local/ratchet/codex/rsxvk/rsxvk12/run_suite.py local/ratchet/suite-N
```

Measurement rules: only `ANYPS3_PERF_REPORT=1` (`ANYPS3_SPU_PROFILE` distorts the SPU and also the clocks); game on CPUs 0–5,12–17,
agents/suite on 6–11,18–23 (second CCD). Terminate the program with `kill -KILL` (SDL catches SIGINT/TERM). Every run
under `systemd-run --user --scope -p MemoryMax=24G`. Agents: `codex exec … -m gpt-6.1-sol` in a scope with `MemoryMax`.
`pgrep/pkill -f` with a pattern from your own command also hits your own shell — kill by PID with a cmdline check.
