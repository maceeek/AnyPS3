# Current state and goal

## Summary — 2026-11-01

- **Goal (user, since 2026-10-25):** the original ACIT level at true 1440p (render scale 2) and a stable 60 FPS, playable
  with a physical gamepad; acceptance in the root `AGENTS.md` (mean >= 59.7 FPS, >= 99 % one-vblank flips, main thread and
  RSX worker each <= ~13 ms per frame, correct image, no STOP).
- **Last game evidence (PC, verified):** native resolution holds the 60 Hz cap with ~3–4 ms headroom (build 706,
  2026-10-21); 1440p walking 39.91 / 40.58 FPS (build 1031, 2026-10-27, HIRES2). Blockers measured then: RSX worker
  ~20–22 ms in heavy frames (≈ 2.9 ms + 9.1 µs per draw), main guest thread ~14.8 ms (~3.9 ms SPU-job wait), GPU at
  scale 2 ~15.4 ms.
- **Not yet measured in the game:** the cloud rounds HIRES3 (device-memory pool, per-draw cuts), HIRES4 (RSX worker
  CPU, synthetic −37 % worker instructions per frame) and HIRES5 (per-draw geometry reuse and state path, synthetic
  −24 % instructions per indexed draw, game-shaped −29 %; derived heavy-frame worker ≈ 13 ms only if the game's geometry
  is as static as the bench's) and HIRES6 (worker draw unit −9.9 % on the game-shaped bench, derived ≈ 12.5 ms at 2000
  draws / ≈ 14.5 ms at 2400; exact barrier cuts 17 → 10 per frame on a scale-2 stream; visual 2× MSAA load path default
  off, NVIDIA test pending) and HIRES7 (SPU synthetic job −16.6 % host instructions; RSX worker draw unit −11.3 % by
  moving batch reset/begin, submit and plane clears onto the draw recorder; derived worker ≈ 11.6 ms at 2000 draws /
  ≈ 13.3 ms at 2400) and HIRES8 (main guest thread: synthetic SPU job −39 % native AVX-512 time / −34 % non-FP host Ir,
  mostly generated SPU code without stack homes; PPU cross-page call kernels −14 %, other call kernels −7..−9 % Ir), all on
  branch `ccr-b1a3987e-aypb5o`. Derived, unverified: main thread ≈ 13 ms per heavy frame if the game's awaited SPU job
  scales like the synthetic one.
- **First next action on the PC:** one integration build of `ccr-b1a3987e-aypb5o`, QUIET alternating 1440p walks against
  1031/1034, then the same binary with `ANYPS3_RSX_VK_MARKS=1` (does the 12.6 µs "native pass begin" move to clears/copies?),
  `ANYPS3_RSX_BARRIER_DEDUPE=0`, `ANYPS3_RSX_GEOMETRY_REUSE=0`, `ANYPS3_RSX_RECORDER_SUBMIT=0`, `ANYPS3_SPU_CHAIN=0`,
  `ANYPS3_PPU_RET_DEPTH=0`,
  `test_rsx_visual_msaa` on NVIDIA, and the switch A/B runs listed in the HIRES7–HIRES3 sections below (walks with
  `ANYPS3_RSX_RECORDER=1`, as the walking wrapper sets it).
- **Builds cited as baselines or pending measurement:** 706 (60 FPS reference), 960 (= main f92681c at 1440p), 1031, 1034
  (1440p), 1002 and 1035 (to be measured, HIRES2). Which binaries still exist on the PC is not recorded after the
  2026-10-25 cleanup ([history](history/CURRENT-2026-10-21-to-10-25.md#local-data-cleanup--2026-10-25-user-request)); a
  further `local/ratchet` cleanup must run on the PC and keep these.
- **Not verified:** physical gamepad acceptance at 1440p, stable 60 FPS at 1440p.

## Cloud round HIRES8 — 2026-11-01 (cloud container: no game, no NVIDIA; branch `ccr-b1a3987e-aypb5o`)

Three Opus agents on top of HIRES7 (d1177e9), both aimed at the main guest thread: generated SPU code of the awaited job
(J, then J2) and PPU page dispatch (K); every cut has a native test and mutation checks; **no game build, run or FPS figure**.
- **SPU (verified, synthetic job, coordinator integration d1177e9 vs abaeba4):** native AVX-512 time 15.7–15.9 k →
  **9.5–9.7 k ns/iteration (−39 %)**, non-FP host Ir 114.3 k → 75.4 k (−34 %), generated code 84.2 k → 45.2 k (−46 %),
  `spu_fn_*` host bytes −2.4 %, digest identical. Self loops keep SPU registers in host locals (`ANYPS3_GEN_SPU_SELF_LOOP=0`
  at generation time restores the old form); store checks no longer call the provenance diagnostic. **Finding:** a call
  that returns into a hot generated path makes GCC home every live vector on the stack (`[[gnu::cold]]` does not help).
- **PPU (verified, callgrind, new game-shaped kernels):** per page one jump table instead of compare trees; host-call return
  prediction for calls out of the page (`ANYPS3_PPU_RET_DEPTH`, 0 = old tail calls): k_xcall −14 %, k_calltree/k_virtual/
  k_switch/k_calls −7..−9 %, memory kernels ±1 %, page text −2.2 %; target −25 % missed. Every earlier PPU callgrind figure
  measured the slow memory path (valgrind rejects the flat view's `mremap` duplication; the bench now shims it).
- **Tests:** each agent's area list OK on its branch; 10 cross-area modules OK on the merge; full suite deferred to HIRES9.
Record: [cloud HIRES8](experiments/2026-11-01-cloud-hires8.md).

## Cloud round HIRES7 — 2026-10-31/11-01 (cloud container: no game, no NVIDIA; branch `ccr-b1a3987e-aypb5o`)

Two Opus agents on top of HIRES6 (8126bbc): SPU job speed for the main thread's SPU wait (H) and moving work off the RSX
worker thread (I); every cut has a native test (independent oracle) and mutation checks; **no game build, run or FPS figure**.
- **SPU (verified, callgrind on a new synthetic job through the production lifter and runtime, `bench/spu7_job_bench.py`):**
  non-FP host instructions per job iteration 136,500 → **113,884 (−16.6 %)**; DMA path −47 % (`LineLocks` without heap
  vectors, span-free concurrent writes, 16-byte atomic copy body on AVX); proven FP underflow lanes decided in the vector
  path; BRSL calls between native functions chained without the dispatcher (`ANYPS3_SPU_CHAIN=1`, 0 disables). Native
  AVX-512 time about −12 % (noisy here).
- **RSX worker (verified, FIFO bench, recorder on):** game-shaped draw unit 37,399 → **33,155 Ir (−11.3 %)**, default mode
  −9.5 %; the draw recorder takes ~5.2 k per unit (≈ 11.3 k, a third of the worker). Batch reset/begin, the submit (incl.
  the async DMA-queue prelude NVIDIA uses) and plane clears run as queued recorder jobs (`ANYPS3_RSX_RECORDER_SUBMIT=1`,
  0 = inline submit); only with `ANYPS3_RSX_RECORDER=1` (runtime default still off: 624b teal-frame question).
- **Derived, unverified:** worker heavy frame ≈ 11.6 ms at 2000 draws, ≈ 13.3 ms at 2400; the main guest thread (~14.8 ms,
  ~3.9 ms of it SPU wait) is the likely next critical path. A prepare-stage split of the worker (−22 % estimated) is designed,
  not implemented.
- **Suite (203 modules, covers HIRES6 + HIRES7):** 179 OK, no regression; one false positive in HIRES6's command-name scan
  (a comment) fixed in d1177e9; three load-dependent failures pass alone (record).
Record: [cloud HIRES7](experiments/2026-11-01-cloud-hires7.md).

## Cloud round HIRES6 — 2026-10-31 (cloud container: no game, no NVIDIA; branch `ccr-b1a3987e-aypb5o`)

Two Opus agents on top of HIRES5 (46aa60f): RSX worker CPU (F) and GPU work at scale 2 (G); every cut has a native
lavapipe test (independent oracle) and mutation checks; **no game build, run or FPS figure**.
- **Measured (callgrind, worker thread, new game-shaped bench mode `tracked=1`, merge 8126bbc vs same-session base, two
  alternating rounds):** draw unit (per indexed draw + b0/2000) 41,670 → **37,555 Ir (−9.9 %)**; default bench mode
  48,617 → 45,569 (−6.3 %). Target −25 % not reached: the rest is ~40 items of 100–900 Ir plus ~9 k Ir of lavapipe
  driver work per draw.
- **Derived, unverified:** heavy-frame worker ≈ 12.5 ms at 2000 draws, ≈ 14.5 ms at 2400 draws (HIRES5: 13.2 / 15.2).
- **GPU (verified on a scale-2 synthetic stream, pixel-exact, sync validation silent):** full barriers 17 → 10 per frame
  (switches `ANYPS3_RSX_BARRIER_DEDUPE`, `ANYPS3_RSX_PAIR_BARRIERS`, `ANYPS3_RSX_PASS_BARRIER`, default 1); clears and
  copies without their own GPU mark were booked to the next native pass begin — **hypothesis** for the PC's unexplained
  12.6 µs native begins; `PERF RENDER_SCALE_WORK` work counters per frame; no copy is removable. Visual 2× MSAA
  plane→sample load path `ANYPS3_RENDER_SCALE_MSAA_LOAD=1` (default 0, **NVIDIA test pending**) — the only change aimed at
  the second visual plane (2.3 ms). The −2.5 ms GPU target is not demonstrated here.
- **Tests:** each agent's area modules OK on its branch; on the merge the union of both area lists, 40 modules, all OK
  (lavapipe); full suite deferred to the end of HIRES7.
Record: [cloud HIRES6](experiments/2026-10-31-cloud-hires6.md). First next actions on the PC: the integration build
walks, a marks walk, `ANYPS3_RSX_BARRIER_DEDUPE=0` A/B and the NVIDIA MSAA test (record, "First next actions").

## Cloud round HIRES5 — 2026-10-31 (cloud container: no game, no NVIDIA; branch `ccr-b1a3987e-aypb5o`)

Per-draw cost of the RSX worker on top of HIRES4 (990c799): two Opus agents, geometry data path (D) and state path (E);
every cut has a native lavapipe test (independent oracle) and mutation checks; **no game build, run or FPS figure**.
- **Measured (callgrind, worker thread, concurrent FIFO bench, merge 1c102a6, same-session base):** per indexed draw
  53,459 → **40,453 Ir (−24.3 %)**, worker frame with 500 indexed draws 43.37 → 36.84 M (−15.1 %); game-shaped diagnostic
  (bench without its setup's register/constant borrows) 46,734 → 33,335 (−28.7 %); exact cuts alone (all new switches 0)
  48,390 (−9.5 %); all positions rewritten every frame 43,778 (−18.1 %).
- **Main change:** geometry reuse — unchanged vertex input groups and decoded index spans are served from host copies
  validated by CPU-dirty watches (`ANYPS3_RSX_GEOMETRY_REUSE`, default 1); the dirty-watch registry now marks writers'
  watches in O(log n) (a store into a watched page cost 1.6 µs at 600 watches before). Exact: one transfer + AVX2 decode
  for adjacent index ranges, index extent from the decode, state-slot version mask, one-pass fragment key; switches
  `ANYPS3_RSX_RTT_PREFILTER`, `ANYPS3_RSX_TEXTURE_IMAGE_MEMO` (default 1).
- **Derived, unverified:** heavy-frame worker ≈ 13.2 ms at ~2000 draws (HIRES4 ~15–16 ms) **if** the game's geometry is as
  static as the bench's and instructions scale to time; main thread (~14.8 ms) and scale-2 GPU (~15 ms) unchanged.
- **Found:** the FIFO bench's setup borrows the register bank and constant arrays, forcing exact-compare paths the game does
  not take (~6.9 k Ir/draw of the bench).
- **Suite (lavapipe, 202 modules):** 180 OK, no regression: HIRES4's 18 failing modules fail unchanged; 4 more time
  out on a test's own compile limit, which the base exceeds equally in this container (timed alone, base vs merge).
Record: [cloud HIRES5](experiments/2026-10-31-cloud-hires5.md). First next action on the PC: one integration build,
alternating QUIET 1440p walks vs 1031/1034, then the same binary with `ANYPS3_RSX_GEOMETRY_REUSE=0` (worker and main
thread ms, `PERF RSX_COUNTERS` geometry/index hits, `PERF PPU_INLINE` medium/slow stores).

## Cloud round HIRES4 — 2026-10-30/31 (cloud container: no game, no NVIDIA; branch `ccr-b1a3987e-aypb5o`)

RSX worker CPU, the 1440p critical path (HIRES2: ~20–22 ms in heavy frames, target <= ~13 ms). Three Opus agents
(per-draw path, FIFO/non-draw, renderer guest-memory access) on top of HIRES3 (de6bc4f); every cut exact, with a native
lavapipe test (independent oracle) and mutation checks; **no game build, run or FPS figure**.
- **Measured (callgrind, worker thread, synthetic game-shaped streams on the real RSX worker with concurrent memory):**
  no draws 33.96 → 16.61 M Ir/frame (−51 %), with 500 indexed draws 68.49 → 43.41 M (−37 %), per indexed draw
  69.1 k → 53.6 k (−22 %); draw bench scenario 5 at scale 2: 50.7 k → 41.4 k Ir/draw (−18 %, non-concurrent memory).
- **Derived, unverified:** heavy-frame worker ~15–16 ms (from ~21) if instructions scale to time — a large step, still
  above the target. Main thread (~14.8 ms) and scale-2 GPU (~15 ms) unchanged by this round.
- **Switches for PC A/B (all default 1):** `ANYPS3_RSX_RENDER_BATCH` (renderer page cursor), `ANYPS3_GUEST_PAGE_READ`,
  `ANYPS3_GUEST_RANGE_ADMISSION`, `ANYPS3_RSX_FIFO_PAIR`; the rest is unconditional.
- **Found:** ~75 % of the worker's between-draw cost was FIFO decode, dominated by renderer-thread guest-memory admission
  (~190–240 Ir per scalar, ~820 per one-page transfer); half the paired FIFO loads went bytewise (4-byte-aligned doublewords);
  the broker's one-entry cell cache missed on 98 % of GET stores. Remaining per-draw cost needs designs (vertex upload reuse,
  bound-texture memo, state-block scan, FP constant rewrite).
- **Suite (lavapipe, 202 modules, 915 tests):** no regression; the 18 failing modules fail on the base too (GCC 13 `-Werror`,
  missing `local/` tools, SDL offscreen without Vulkan, the container's compile speed against 30/60 s test limits).
Record: [cloud HIRES4](experiments/2026-10-30-cloud-hires4.md). First next action on the PC: one integration build,
alternating QUIET 1440p walks vs 1031/1034 (rsx_worker ms/frame and µs/draw in heavy frames), then the same binary with
`ANYPS3_RSX_RENDER_BATCH=0` and with all three memory switches 0.

## Cloud round HIRES3 — 2026-10-28 (cloud container: no game, no NVIDIA; branch `ccr-8139488c-79xg7b`)

Source-level changes on top of main 95756e2, each with a native lavapipe test (independent oracle) and mutation checks;
**no game build, run or FPS figure** — the PC must build and measure alternating QUIET runs.
- **Device-memory pool** (HIRES2 stutter class A, possibly B): device-local allocations become ranges of 256 MiB blocks
  (best fit, coalescing, size index); no driver alloc/free in streaming frames. A/B: `ANYPS3_RSX_MEMORY_POOL=0`.
  Lavapipe (test hook): 64 ranges from 7 driver allocations vs 68, byte-identical guest output.
- **Worker CPU cuts (exact):** DrawJob borrows batch-pinned resources (no cross-thread refcount traffic); RESET_REPORT/ZPASS
  reports scan only draws since the last reset (was O(draws in flight) per report); vertex-constant refresh skips unchanged
  128-word chunks; typed-RTT decoder addressing as shader literals (`ANYPS3_RSX_RTT_LITERALS=0` for A/B).
- **Measured synthetically:** plain FIFO decode ~1 µs per draw in the game's memory configuration; the FIFO window is slower
  (refuted as a default). **Found:** every render pass carries full external dependencies (each pass switch drains the GPU);
  hazard tracking would not help the native→visual→native case (documented, not changed).
Record: [cloud HIRES3](experiments/2026-10-28-cloud-hires3.md). First next action on the PC: one integration build, alternating
QUIET 1440p walks vs 1031/1034 and the same binary with `ANYPS3_RSX_MEMORY_POOL=0`, plus a full ring with `hitch_marks.py`.

## HIRES2 wave — 2026-10-27 evening (goal NOT reached; branch `claude/hires2-wave`)

1440p walking: build 1031 holds **39.91 / 40.58 FPS** vs 960 (= main f92681c) 37.27 / 35.89 (QUIET, alternating), GPU
15.4 vs 19.0 ms/frame. Scale 1 walks at ~44 FPS; standing still is 60 at scale 1. **Remaining blockers (verified):**
(1) CPU critical path in heavy views (~2000 draws): RSX worker ~20–22 ms (≈2.9 ms + 9.1 µs/draw, flat profile) and
the main guest thread ~14.8 ms, of which ~3.9 ms is spin-waiting for SPU jobs; (2) GPU at scale 2 ~15.4 ms (visual
draws 7.3, second sample plane 2.3, native pass begins 1.0, copies 2.3). Merged but unmeasured in the game: fast RGBA8
visual RTT + barriers without HOST bits (1034: about level with 1031 in one pair), native-switch replay change, visual 2×
MSAA (default off; build 1035 compiled, not measured), unrolled atomic copy (1002: one disturbed-looking slower run —
re-measure before merging to main).
Record, tables and next actions: [HIRES2 wave](experiments/2026-10-25-hires-wave.md#hires2-wave-result-2026-10-27-evening-coordinator-claude-opus-55-agents-opus-55-high-goal-not-reached).

## Older state records

- 2026-10-21 to 2026-10-25 (wave 6 and its 60 FPS table, resolution-scaling milestone, cloud follow-up, FIX7, performance
  milestone, the HIRES wave, the local data cleanups of 2026-10-21 and 2026-10-25):
  [history/CURRENT-2026-10-21-to-10-25.md](history/CURRENT-2026-10-21-to-10-25.md) (moved 2026-10-31).
- FAST2 wave 4, the 2026-10-20 sandbox assessment, installer/manager notes, the historical notes, the history of real stops
  256–339 and the old "first next actions" (2026-10-14 to 2026-10-21):
  [history/CURRENT-2026-10-14-to-10-21.md](history/CURRENT-2026-10-14-to-10-21.md).

## Where the game is

The original game passes through boot, videos (skippable with START), the `front_end` loading, the title screen and reaches the
**original main menu** (NEW GAME / LOAD / COMMUNITY / OPTIONS / DELETE) ~2 min after start; the menu responds to the
virtual pad (down arrow → LOAD). Rendering: custom Vulkan RSX (NVIDIA RTX 4070 SUPER), PPU/SPU statically translated.
Performance of the original level: see the summary at the top (60 FPS at native resolution since build 706; ~40 FPS at
1440p in build 1031).

## Split compilation (since 256)

`build_game` writes `units.json` + `anyps3.hpp`, `program.cpp` (runtime, main),
96 `ppu-*.cpp` and 96 `spu-*.cpp`; `virtual_pad.py build -j 24` compiles in parallel in **~4 min**
(previously a single 458 MB TU, LTO 7–15+ min). See [record 256](experiments/2026-10-15-split-units-256.md).

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
