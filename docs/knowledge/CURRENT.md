# Current state and goal

## Summary — 2026-10-31

- **Goal (user, since 2026-10-25):** the original ACIT level at true 1440p (render scale 2) and a stable 60 FPS, playable
  with a physical gamepad; acceptance in the root `AGENTS.md` (mean >= 59.7 FPS, >= 99 % one-vblank flips, main thread and
  RSX worker each <= ~13 ms per frame, correct image, no STOP).
- **Last game evidence (PC, verified):** native resolution holds the 60 Hz cap with ~3–4 ms headroom (build 706,
  2026-10-21); 1440p walking 39.91 / 40.58 FPS (build 1031, 2026-10-27, HIRES2). Blockers measured then: RSX worker
  ~20–22 ms in heavy frames (≈ 2.9 ms + 9.1 µs per draw), main guest thread ~14.8 ms (~3.9 ms SPU-job wait), GPU at
  scale 2 ~15.4 ms.
- **Not yet measured in the game:** the cloud rounds HIRES3 (device-memory pool, per-draw cuts) and HIRES4 (RSX worker
  CPU, synthetic −37 % worker instructions per frame; derived heavy frame ~15–16 ms), both on branch
  `ccr-b1a3987e-aypb5o`; HIRES5 (per-draw geometry data and state path) is running in the cloud on top of it.
- **First next action on the PC:** one integration build of `ccr-b1a3987e-aypb5o`, QUIET alternating 1440p walks against
  1031/1034, then the switch A/B runs listed in the HIRES4 and HIRES3 sections below.
- **Builds cited as baselines or pending measurement:** 706 (60 FPS reference), 960 (= main f92681c at 1440p), 1031, 1034
  (1440p), 1002 and 1035 (to be measured, HIRES2). Which binaries still exist on the PC is not recorded after the
  2026-10-25 cleanup ([history](history/CURRENT-2026-10-21-to-10-25.md#local-data-cleanup--2026-10-25-user-request)); a
  further `local/ratchet` cleanup must run on the PC and keep these.
- **Not verified:** physical gamepad acceptance at 1440p, stable 60 FPS at 1440p.

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
