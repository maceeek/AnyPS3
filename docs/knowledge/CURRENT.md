# Current state — 2026-11-01

This page is the whole current state. It keeps the last game measurement on the PC (HIRES2, 2026-10-27) and the cloud
rounds since (HIRES3–HIRES9, source changes measured only synthetically). Everything older is in [../archive/](../archive/README.md)
and is not needed for orientation.

## Goal and acceptance

- **Goal (user, since 2026-10-25):** the original ACIT level at true 1440p (`ANYPS3_RENDER_SCALE=2`) at a stable 60 FPS, without
  stutter, playable with a physical gamepad.
- **Acceptance (root `AGENTS.md`):** two alternating QUIET runs of the same build, each mean >= 59.7 FPS, >= 99 % of flips
  within one vblank, main thread and RSX worker each <= ~13 ms per frame, correct level image, no STOP.
- **Not verified:** stable 60 FPS at 1440p; physical gamepad acceptance (virtual and uinput pads are verified).

## Where the game is (verified on the PC)

Boot, videos (START skips), `front_end` loading, title screen, the **original main menu** (~40 s with the fast menu), LOAD
with the virtual pad and the first level `great_clock_a`, all through static PPU/SPU translation and the own Vulkan RSX
(PC: Ryzen 9 9900X, RTX 4070 SUPER, Linux).
- **720p (scale 1):** standing still holds the 60 Hz cap. Build 706 (2026-10-21) did 59.71 / 59.69 FPS, ~99.8 % one-vblank, main ~11 ms
  and worker ~12 ms. Walking in heavy views misses (HIRES2: ~44–46 FPS).
- **1440p (scale 2) walking:** build 1031 did **39.91 / 40.58 FPS** (HIRES2, below). The image is the game's 960×704 scene rendered at
  1920×1408 and resampled 4:3 horizontally, with no comb and no block replication (verified over whole frames). Full 2560-column
  detail would need a horizontal scale of 8/3, which is **a user decision, open**.

## Last PC measurement — HIRES2, 2026-10-27 (verified; main 95756e2 = PR #14)

Decision-grade, QUIET, alternating, scale-2 timed virtual-pad walk, no STOP, original PT_LOAD exact:

| build | content | runs | FPS | GPU busy ms/frame |
| --- | --- | --- | --- | --- |
| 960 | main f92681c | 960sa1 / 960sa2 | 37.27 / 35.89 | 18.64 / 19.29 |
| 1031 | 960 + visual typeless aspect by a draw (9912280) + static fragment discards | 1031a1 / 1031a2 | 39.91 / 40.58 | 15.52 / 15.32 |
| 1034 | 1031 + fast RGBA8 visual typed-RTT half, barriers without HOST bits (single pair, not a decision) | 1034a1 / 1034a2 | 38.19 / 40.43 | 14.50 / 13.94 |

**Blockers (verified):**
1. **RSX worker CPU:** heavy views have ~1800–2400 draws. The worker is busy ~20–22 ms there (≈ 2.9 ms + 9.1 µs per draw, flat
   profile, no item > 7 %).
2. **Main guest thread:** ~14.8 ms in the same work span. ~26 % of it is spin-waiting for SPU jobs (delay 0x666068 17.5 %, job flag
   0x5564d4 5.6 %, engine counter 0x696c10 3.0 %), ≈ 3.9 ms per heavy frame. 57 % is translated page code and ~6.5 % the
   dispatcher; there are no observer faults. The guest never runs ahead past the flip.
3. **GPU at scale 2:** 15.65 ms (1031sm1). Visual draws take 7.26 ms (1692 per frame), native draws 2.80, native render-pass begins 1.02
   (12.6 µs each vs 1.55 µs visual, unexplained), copies ~2.3 and ~250 barriers ~0.6. The second visual sample plane costs 2.3 ms.
4. **Stutter:** hitches repeat with the walker loop at both scales; pipeline creation is ruled out.
   - **Class A (RSX worker, 80–160 ms frames with ~360 draws):** at scale 2, ~55 ms is device-memory alloc/free. The other
     60–100 ms is unattributed time in the `fifo` stage.
   - **Class B (GPU, texture-streaming frames, 130–160 ms):** every GPU operation is ~10× slower, coinciding with ~240 new
     allocations per frame. The cause is unknown (first touch? clock drop?).
   - **Class C (main thread, ~140 ms frames, 4–5 per 80 s):** the main thread runs with no block event and no GPU output.

**Still open from HIRES2:**
- 1035 (visual 2× MSAA on/off) was compiled but never measured.
- 1002 (unrolled atomic copy cf7a6ef, since merged to main) looked slower in one disturbed run. Its copy body was rewritten in
  HIRES7 and HIRES9, so measuring the branch tip at scale 1 against 960 answers the question.

Record: [HIRES/HIRES2](experiments/2026-10-25-hires-wave.md). PC-only evidence:
`local/ratchet/work/hires2{w,s}/RESULT.md`.

## Cloud rounds HIRES3–HIRES9 — 2026-10-28 … 11-01 (branch `ccr-b1a3987e-aypb5o`; nothing measured in the game)

The cloud container has no game data, no `local/` tree and no NVIDIA GPU. Every change is exact or gated, has a native
regression test with an independent oracle and mutation checks, and was measured only with callgrind or thread-CPU time on
synthetic benches (instructions are not time). The branch is main 95756e2 plus these rounds (111 commits) and the documentation cleanup.

| PC blocker | cloud changes (round) | synthetic result (verified, cloud) |
| --- | --- | --- |
| RSX worker CPU | DrawJob borrowed pointers, report floors, vertex-constant chunks (H3); FIFO decode/actions, renderer-thread guest-memory admission, paired FIFO loads (H4); geometry reuse under CPU-dirty watches, O(log n) watch registry, index/state path (H5); single-part inline `Range`, shared cached triangles, constant-window registers, no GET publication before BEGIN (H6); batch reset/begin, submit and plane clears as draw-recorder jobs (H7, recorder on only) | per indexed draw 69.1 k → 53.6 k Ir (H4), 53.5 k → 40.5 k (H5, −24 %); game-shaped draw unit 41.7 k → 37.6 k (H6, −9.9 %), 37.4 k → 33.2 k (H7, −11.3 %); worker frame with no draws −51 % (H4) |
| GPU at scale 2 | typed-RTT decoder addressing as literals (H3); full barriers 17 → 10 per frame, GPU marks for clears/copies, `PERF RENDER_SCALE_WORK`, visual 2× MSAA plane→sample load path (H6, default off) | pixel-exact on lavapipe with sync validation silent; GPU time is not measurable here |
| Stutter class A | device-memory pool: 256 MiB blocks, best fit, coalescing (H3) | lavapipe: 64 ranges from 7 driver allocations vs 68 |
| Main thread: SPU-job wait | DMA path without heap spans, 16-byte atomic body, FP underflow lanes, BRSL chaining (H7); generated SPU code without stack homes, self loops in host locals (H8); shared chained-call scope, 64-byte DMA copy loop, untracked DMA writes without bumps/notify, SHUFB lanes (H9) | synthetic job non-FP Ir 136.5 k → 113.9 k (H7) → 75.4 k (H8) → **68.6 k** (H9): −50 %. Native AVX-512 15.8 k → 9.6 k (H8) → **8.7 k ns/iteration** (H9): −45 % since H7's tip (H7 itself ~−12 %, noisy). Output digest identical. |
| Main thread: page code | one jump table per page, host-call return prediction (H8); one-compare inline range test, one pointer per access (blocker table below the flat view), MOVBE, one-byte `movzbl`/`movb`, budget-free block heads (H9) | call kernels −7..−14 % Ir (H8); memory kernels −14..−17 %, call kernels −11..−18 %, page text −11 % (H9); an inline access is 11 host instructions (was 18–19) |

**Derived, unverified:**
- **Worker:** heavy-frame worker ≈ 11.6 ms at 2000 draws and ≈ 13.3 ms at 2400. This holds with the recorder on, if Ir scale to
  time and the game's geometry is as static as the bench's.
- **Main thread:** ≈ 12–13 ms. Assumptions: the awaited SPU job shrinks like the synthetic one (the 3.9 ms wait falls by ~1.7–2 ms),
  and page code (57 %) gains the −7..−14 % measured natively on the kernels.
- **GPU:** ≈ 14–14.5 ms at scale 2 (1034 plus barrier cuts). That fits the 16.7 ms frame with little headroom.
- **Stutter:** classes B and C are not addressed.

Records (detail on demand, one per round): [H3](experiments/2026-10-28-cloud-hires3.md),
[H4](experiments/2026-10-30-cloud-hires4.md), [H5](experiments/2026-10-31-cloud-hires5.md),
[H6](experiments/2026-10-31-cloud-hires6.md), [H7](experiments/2026-11-01-cloud-hires7.md),
[H8](experiments/2026-11-01-cloud-hires8.md), [H9](experiments/2026-11-01-cloud-hires9.md).

## A/B switches on the branch (defaults verified in the code, 2026-11-01)

Each switch is read once per process; any value other than `0`/`1` stops the run. The table lists those meant for the PC's A/B runs.

| switch | default | 0 / 1 means | round |
| --- | --- | --- | --- |
| `ANYPS3_RSX_MEMORY_POOL` | 1 | 0 = driver allocation per resource (old) | H3 |
| `ANYPS3_RSX_RTT_LITERALS` | 1 | 0 = typed-RTT addressing through constants | H3 |
| `ANYPS3_GUEST_PAGE_READ`, `ANYPS3_RSX_RENDER_BATCH`, `ANYPS3_GUEST_RANGE_ADMISSION` | 1 | 0 = old renderer-thread guest-memory admission | H4 |
| `ANYPS3_RSX_FIFO_PAIR` | 1 | 0 = no paired FIFO loads | H4 |
| `ANYPS3_RSX_GEOMETRY_REUSE` | 1 | 0 = copy vertex inputs/index spans every draw | H5 |
| `ANYPS3_RSX_RTT_PREFILTER`, `ANYPS3_RSX_TEXTURE_IMAGE_MEMO` | 1 | 0 = old texture/RTT lookup | H5 |
| `ANYPS3_RSX_BARRIER_DEDUPE`, `ANYPS3_RSX_PAIR_BARRIERS`, `ANYPS3_RSX_PASS_BARRIER` | 1 | 0 = old barriers | H6 |
| `ANYPS3_RENDER_SCALE_MSAA` / `ANYPS3_RENDER_SCALE_MSAA_LOAD` | 0 / 0 | 1 = visual 2× MSAA working images / their plane→sample load path (NVIDIA test pending) | HIRES2 / H6 |
| `ANYPS3_RSX_RECORDER` | 0 (GUI too) | 1 = asynchronous draw recorder (the walking wrapper sets 1; 624b teal-frame question open) | older |
| `ANYPS3_RSX_RECORDER_SUBMIT` | 1 | 0 = inline submit (only matters with the recorder on) | H7 |
| `ANYPS3_SPU_CHAIN` / `ANYPS3_SPU_CHAIN_SHARED` | 1 / 1 | 0 = BRSL through the dispatcher / per-call scope | H7 / H9 |
| `ANYPS3_GUEST_WRITE_TRACKED_ONLY` | 1 | 0 = DMA writes bump/notify every line | H9 |
| `ANYPS3_PPU_RET_DEPTH` | 64 | 0 = old tail calls out of a page | H8 |
| `ANYPS3_GEN_SPU_SELF_LOOP` (generation time) | 1 | 0 = old self-loop form (needs its own build) | H8 |

The HIRES9 PPU memory and block-head cuts and the HIRES8 jump tables have no switch. For them the A/B is two builds.

## First next actions on the PC (one plan; replaces the per-round lists)

1. **Targeted NVIDIA tests on the branch tip:** `test_rsx_visual_msaa`, `test_rsx_async_upload`, `test_rsx_recorder`,
   `test_rsx_gpu_work`. Then the full suite (the cloud container cannot build some modules; see [HANDOFF](HANDOFF.md)).
2. **One integration build N of the tip** (new number, source snapshot). Record compile wall time and the `.text` of the
   ppu-*/spu-* units against 1034.
3. **QUIET alternating 1440p walks, N vs 1034 (or 1031), two each**, with the walking wrapper's `ANYPS3_RSX_RECORDER=1`. Record:
   FPS and one-vblank %; `main_ppu`, `rsx_worker`, rsx-record and GPU busy ms/frame; µs/draw in heavy frames; the SPU-wait shares
   (0x666068, 0x5564d4, 0x696c10) and the awaited ig lane's time (sampler); the worst frames. Check the image for the recorder's teal frames. Also run a scale-1 pair
   against 960, which settles the 1002 question.
4. **If not at the target, one switch at a time, alternating, with the same binary** (switches in the table above):
   - worker: `ANYPS3_RSX_GEOMETRY_REUSE=0`, `ANYPS3_RSX_RECORDER_SUBMIT=0`, the three H4 memory switches together;
   - main thread: `ANYPS3_SPU_CHAIN=0`, `ANYPS3_SPU_CHAIN_SHARED=0`, `ANYPS3_GUEST_WRITE_TRACKED_ONLY=0`, `ANYPS3_PPU_RET_DEPTH=0`;
   - GPU: one `ANYPS3_RSX_VK_MARKS=1` walk (does the 12.6 µs native pass begin move to clears/copies in `PERF GPU_MARKS`?),
     then `ANYPS3_RSX_BARRIER_DEDUPE=0`, then MSAA on with load 1 vs 0 vs MSAA off.
5. **Stutter:** full frame ring with `hitch_marks.py`, `ANYPS3_RSX_MEMORY_POOL=1` vs 0. Class A should vanish with the pool; if
   class B persists, the first-touch hypothesis is refuted.
6. **Exactness spot checks:**
   - animated/skinned geometry with geometry reuse on (visual);
   - a run that hits `--max-steps` stops with the same message, PC and steps as before;
   - `ANYPS3_SPU_DYNAMIC_LOG` rows match the base build.
7. **If the recorder shows no teal frames on the tip,** make it the default in the runtime and the GUI
   (`anyps3_manager/settings.py` `experimental.recorder`).

## Next levers once measured (derived from the cloud analysis)

- **Worker:** a prepare-stage split of the RSX worker (designed in [H7](experiments/2026-11-01-cloud-hires7.md), −22 % estimated,
  not implemented); a two-thread worker (front: FIFO decode and state; back: prepare and record).
- **Main thread:** register allocation across PPU blocks or a cheaper CR-field representation. Each `cmpwi` costs ~10 host
  instructions, and GCC reloads registers at every block head ([H9](experiments/2026-11-01-cloud-hires9.md)). For the SPU job:
  a one-page DMA write fast path (~1–1.5 k Ir per iteration) and the FP slow path's stack homes.
- **GPU:** the native pass-begin cost; the second visual sample plane (MSAA load path); horizontal scale 8/3 only on the user's
  decision.

## Refuted — do not repeat (with the round that showed it)

- **Measurement methodology:**
  - HOST_PROFILE distortion is only ~3 %; SMT/CPU placement and recorder spin are not levers (HIRES2).
  - `ANYPS3_SPU_PROFILE` distorts the SPU and the clocks; use only `ANYPS3_PERF_REPORT=1` (older).
  - valgrind rejects the flat view's `mremap` duplication. PPU callgrind figures before H8 measured the slow memory path, so
    `bench/ppu_bench.py` shims it (`-DBENCH_MREMAP_SHIM`) (H8).
- **GPU:**
  - Pipeline compiles do not cause the 140–180 ms walking hitches (46 ms of creation in all of 1031a2) (HIRES2).
  - Multiview or layered visual planes do not help: 960sx1/sx2 GPU 20.8 / 19.4 ms (HIRES2).
  - Static discards blocking early-Z are not a big lever (HIRES2).
  - Locking GPU clocks is not a fix: the low P-state follows intermittent feeding (FAST, 10-19). PRESENT_EARLY was reverted;
    never merge `fast2/frametime-early-present` (FAST2, 10-21).
  - A boot-time pipeline prebuild saves only the warm cost (771 pipelines ≈ 42 ms) and cannot help unseen shaders; the
    real defect was the disk-cache cap (FIX7, 10-23).
  - Compiles are not the bulk of the post-load slow phase; it was CPU NV3089 scaled blits, since moved to the GPU (FAST2).
  - Hazard tracking would not help the native→visual→native pass case (H3).
  - No scale-2 copy is removable (H6).
- **RSX worker:**
  - Incremental HostObserver bitmaps (1001) are neutral at both scales (HIRES2).
  - At scale 1, barrier de-duplication and occlusion-query elision gave nothing (FAST2). The HIRES6 barrier cuts target
    scale 2 and are unmeasured.
  - The FIFO prefetch window is slower: `ANYPS3_RSX_FIFO_WINDOW=1` 66.1 vs 27.3 M Ir/frame (H3, H4).
  - Reading several pages one at a time under a moving hazard is not equivalent (H4).
  - The FIFO bench's setup borrows the register bank and constants (~6.9 k Ir/draw the game does not pay); use mode `tracked=1` (H5).
- **SPU and the main thread's SPU wait:**
  - The wait is an ig lane *running* the awaited job: 94 % lane compute; parked ~1 %, wake-to-run 1.8–4.6 µs. Parking and
    wake latency are not the lever (FAST2 wake trace).
  - Lane 0x100 (CellSpursKernel0, ~70 % of SPU instructions) is not critical: halving its rate changed neither FPS nor
    main_idle (FAST2).
  - SPU register residency across blocks/functions gave no gain; cross-function calls became 2.5× slower (FAST2). HIRES8's
    self-loop locals are a different scope.
  - A guest timer slack of 1 ns adds +1.3 ms main busy, so it stays opt-in (FAST2).
  - `[[gnu::cold]]` does not stop GCC homing live vectors around a call that returns into hot generated code (H8).
  - A 64-byte aligned SPU register file has no effect; it was reverted (H9).
- **PPU:**
  - A block-local copy of the inline view: −1 % Ir for +1.4 % text (H8).
  - Register locals across a self loop's back edge: text +5.7 % (H9).
  - Deferring register write-back to block end: GCC reloads at every head anyway (H9).
  - The one-compare range test for byte ranges: k_vmx +2.5 % (H9).

## Standing decisions

- **No patches to game bytes (user, 10-23), not even opt-in.** The only RPCS3 patch for 01.00 is "Unlock FPS" (frame-time
  constants at 0xca2500/0xca4170/0xc9438c); it and recomp-port hooks/limiters are excluded. Evidence:
  [FIX7 record](../archive/knowledge/experiments/2026-10-23-fix7.md#rpcs3-game-patches-for-acit--researched-not-adopted-user-decision).
- **No substitutes:** no substitute menu, no pre-unpacked assets, no other emulator.
- **Explicit stops:** unknown behaviour stops the run explicitly.
- **Recorder off by default:** in the runtime and the GUI, until the 624b teal frames are checked; walks measure with it on.
- **Visual 2× MSAA off by default** until the NVIDIA test and an A/B.
- **GUI launches:**
  - always set `ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1`; the game is unplayable without them (`anyps3_manager/settings.py`);
  - skip intro (`input.skip_intro`, through `ANYPS3_PAD_INJECT`) is off by default;
  - the fast menu (early START on the title) is the default.
- **Exactness:** the GPU path need not be bit-exact with the CPU reference rasterizer (tests use documented tolerances). At
  render scale, native images stay authoritative beside the visual companions.
- **Shared guest memory:** no plain (non-atomic) AVX accesses without an exclusion protocol, since that is a C++ data race
  (sandbox analysis, 10-20).
- **Presentation:** guest VSYNC presents through MAILBOX with >= 3 images (FIFO plus the compositor gave 20 FPS).
- **Caches:** the shader disk cache is capped at 32768 files / 4096 MiB, the source cache at 1024 entries / 64 MiB (measured
  working sets).
- **Silence:** every agent-started process forces `SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null` (root `AGENTS.md`).

## Game runtime facts for performance work (verified on the PC, archived records 10-16 … 10-21)

- **Frame structure:**
  - The level runs in VSYNC mode with no interval (≤ 59.94 FPS; G=*(0xca3f84), mode word G+84 = 0, 2 while loading).
  - Flips are vblank-quantised, so every frame must stay under 16.7 ms.
  - At native 60 FPS the main thread is 8.9 ms of work + 3.4 ms SPU wait + 3.5 ms limiter slack. The pacing sleep is a
    usleep(50) loop at 0x5c56d4; the limiter 0x5c5228 binds at the vblank period.
- **Native headroom (706, `ANYPS3_VBLANK_HZ`):** 75 Hz gives 55.9 % one-vblank and 90 Hz half rate, i.e. a frame cost of
  ~12.3–13.5 ms.
- **Wait loops of the main thread:**
  - the job flag at 0x5564d4 spins until `(int16)*(r3+0x18) < 0` on `or r31,r31,r31` hints;
  - 0x666068 is the 16-hint delay block;
  - 0x696c10 spins ~1 ms, then calls `sys_timer_usleep(30)`.
- **SPU lanes:**
  - The critical lanes are the igCellSpursKernelGroup lanes 0x2000200/0x3000200/0x4000200.
  - They advance the completion frontiers 0xfc7d00/0xfc7e00 by MFC PUTLLUC (SPU pc 0x3f9e8). The awaited job seen was the
    FP-heavy microprogram 0x27.
  - Jobs load at variable LS bases, so interpreted SPU entries exist (SPUDYN).
- **Pad:** thread 1 reads `cellPadGetData` once per frame (0x65d92c). An edge lasts one frame, so a tap shorter than a frame
  hitch is lost; auto-repeat starts after > 30 held frames.
- **GPU clocks:** the RTX 4070 SUPER stays at P8/P5 when fed in bursts (one replay frame 103 ms at P8 vs 10.5 ms at P0).
  This confounds stutter class B.
- **Original behaviour, not bugs:** `lwmutex unlock by non-owner … lr=0x0099e23c`; a second START during title→menu selects
  NEW GAME.

## Known open problems (last known status)

- **Pause blur:** the GPU feedback snapshot is merged (main 124b7d4) but no in-game pause test is recorded. Depth feedback is
  still an explicit stop.
- **Savedata:** CIRCLE in the savedata LOAD list gives "Savedata error 0x8002b403 (callback 1)"; the cancel reply is suspect
  (FIX7).
- **Visual history:** CPU/SPU writes discard visual history, and clean-owner merges and unsupported RTT conversions rebuild
  from native pixels. The effect on the final image is not traced.
- **Worker bursts:** occasional RSX-worker bursts at native resolution (20–38 ms/frame for ~16 s, 706d/705b), cause unknown.
- **Never run:** the 883 texture-eviction diagnostic and a quiet `ANYPS3_RSX_RESIDENT_SAMPLER_DYNAMIC=0/1` A/B.
- **Estimate instructions:** PPU fres/frsqrte/vrefp/vrsqrtefp are exactly rounded, not the Cell estimate table; SPU
  frest/frsqest use RPCS3's table (derived). ZPASS/report semantics with statistics disabled copy an RPCS3 approximation
  (hypothesis).
- **Physical gamepad:** never verified in game. uinput → SDL → cellPad is verified; SDL needs window focus or
  `SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1`.
- **Wake trace:** `ANYPS3_WAKETRACE` is not in this branch (only the PC branch `fast2/spuwake`); use the sampler or merge
  that branch.

## Commands (PC)

```bash
# Source snapshot + build (always a new N). From a snapshot use PYTHONPATH=local/ratchet/source-snapshot-N python -P …
python -P -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 1000000000000 -o local/ratchet/native-bundle-N
ANYPS3_EXTRA_CXXFLAGS='-march=native -ffp-contract=off' ANYPS3_RSX_VK_BUILD=1 PYTHONPATH=. \
  python local/ratchet/tools/virtual_pad.py build -j 16 local/ratchet/native-bundle-N local/ratchet/native-virtual-N
# 1440p walk (QUIET, pattern of the HIRES2 walk scripts) and its report (writes local/ratchet/walking-RUN.json):
QUIET=1 bash local/ratchet/work/hires2s/measure-walk-1034.sh RUN        # copy and adapt for build N
python local/ratchet/report-walking.py RUN
# Stutter / marks analysis: local/ratchet/work/hires2s/tools/{hitch_marks.py,marks_worst.py}; SPU-wait split:
# local/ratchet/work/hires2w/tools/wallsplit.py; call stacks: local/ratchet/tools/sample_callers.py
# Other PC tools: report-walk-hitches.py; tools/level_warmup.py RUN_DIR (level-summary.json skips the 40 s post-load
# phase); tools/nav_check.sh, tools/uinput_pad.py (PAD=uinput), tools/play_physical.sh N for pads;
# ANYPS3_CAPTURE_NATIVE_PAIR=1 + ANYPS3_FRAME_TRIGGER for same-flip native/visual captures (diagnostic, blocks on readback)
# Menu run + measurement (verification.json):
bash local/ratchet/tools/measure_menu.sh N 0-5,12-17 ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1
```

**Measurement rules:**
- Measure with `ANYPS3_PERF_REPORT=1` only.
- Run the game on CPUs 0–5,12–17 and agents/suite on 6–11,18–23 (second CCD), every run under
  `systemd-run --user --scope -p MemoryMax=24G`.
- End a run with `kill -KILL` (SDL catches SIGINT/TERM), and kill by PID after a cmdline check (`pkill -f` also hits your own
  shell).
- Keep throughput and diagnostic runs apart: the wake trace costs 2–4 FPS and the sampler shifts FPS. Exclude the first run
  after a shader-identity change (it compiles cold).
- Walks use `measure-walk-N.sh`: QUIET, WARM20/WIN90, `walk-timed.py`, recorder=1. Each run gets a private CoW HDD from
  `walk-hdd-template-20261025/` (`ANYPS3_HDD_ROOT`); concurrent runs must never share an HDD.
- The PC agent shell is zsh, so an unquoted `$FILES` stays one word; use bash arrays.
- Builds cited as baselines: 706, 960, 1031, 1034 (1035 for MSAA). They must survive any `local/ratchet` cleanup.
- Logs over 1 MiB are `.zst`. Old source snapshots are in `local/ratchet/archive/source-snapshots-*.tar.zst`, restored with
  `zstd -dc --long=27 … | tar -xf - source-snapshot-N` (cleanup of 2026-10-25:
  [archived note](../archive/knowledge/history/CURRENT-2026-10-21-to-10-25.md#local-data-cleanup--2026-10-25-user-request)).

Split compilation: `build_game` writes `units.json`, `anyps3.hpp`, `program.cpp` (runtime, main) and ~96 `ppu-*.cpp` +
~96 `spu-*.cpp`, compiled in parallel in ~4 min.
