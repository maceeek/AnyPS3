# Current state and goal

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

## HIRES wave: true 1440p at a stable 60 FPS — 2026-10-25 evening (goal NOT reached)

Baseline 882 walking (QUIET): 1440p 33.15 FPS / rsx_worker 16.1 ms; 720p 44.68 FPS / 10.6 ms — the RSX worker's
per-draw CPU cost (~11.5 µs) is the critical path, and the 1440p image was derived from native pixels (MSAA sample-grid
layout bug → zig-zag comb). Fixes from three agents (visual MSAA layout, per-draw CPU caches, Vulkan pair/shader/
framebuffer costs) are merged and tested on branch `hires/integ`; **no game measurement of them yet**. Integration build
960 + run 960q1 were queued detached at session end. Record and next actions:
[HIRES wave](experiments/2026-10-25-hires-wave.md).

## Performance milestone closed — 2026-10-25

The user cleared the goal and requested closing the milestones and committing. Root matches tested snapshot 882 (348 Python files; `local/ratchet/integration-manifest-882.json`). This is an implementation/evidence checkpoint: **higher-resolution 60 FPS and zero movement shader stutter are not achieved**.

Implemented: exact SPIR-V 1.0/1.3 cache validation; bounded blit/history/source reuse; coherent GPU depth-byte copies; reuse of shaders across implicit filter/mip changes and resident wrap/coordinate changes. Native pixel/coherence and actual Vulkan tests pass; shader identity reuse is also visible in the original game. 880's warmed CPU-blit/readback cost drops to 0.689 s / 84 surfaces versus 13.872 s / 1890 in the earlier diagnostic, without a controlled FPS claim.

Latest real run `882residentdiag1`: original level at 2560x1440, 31.154 FPS over the whole walk, 32.957% one-vblank, worst 1149.310 ms; exact PT_LOAD/no STOP. Warmed window has zero GLSL compiles, but whole movement contains five first-use compiles and expensive pipeline creation. Image artifacts and physical-gamepad acceptance remain open. Dynamic sampler GPU cost needs quiet same-binary comparison.

Full integrated-881 suite was stopped on closeout: 82/210 modules, 603/1071 tests completed, four skips, no failures in completed modules, no source drift. This is **not a full suite pass** and does not cover the added 882 change; its focused/broader Vulkan tests are recorded separately. Build 883 is a verified, unintegrated metadata-only texture-eviction diagnostic, with no game run.

All builds, game runs and the test scope started for this work are finished or stopped; no assigned subagents remain. If resumed: complete final-source regression validation, repeat quiet 882 static/dynamic comparisons, then test the texture-eviction hypothesis with 883. [Full evidence, commands and limitations](experiments/2026-10-25-scale2-walking.md).

## FIX7: user-reported playability problems — 2026-10-23 (branch `claude/fix7`)

The user played through the GUI (release of 2026-10-21) and reported nine problems. Record:
[FIX7](experiments/2026-10-23-fix7.md).

- **START in game:** timed `sys_semaphore_wait` + saturating LV2 deadlines (4d8b6a9). Build 810: the semaphore STOP is
  gone; the pause menu's blur draws then stop on `sequential_color_texture_feedback`. With the diagnostic CPU fallback
  (810b) the original pause menu renders and unpauses. GPU snapshot fix on `fast2/fix7-pause` 0f7addf — **unbuilt**.
- **GUI:** worker toggles removed (always on); `host_drain`/`flip_early` toggles removed (the GUI had been disabling
  both); unimplemented settings hidden; clear key-download result; **Skip intro** (`ANYPS3_PAD_INJECT`, default off,
  no game run yet).
- **Two windows / save-menu audio:** fixed and merged (d6647f6); run 820a verified one Vulkan window, menu ~60 FPS and
  no silent audio periods with the savedata dialogs open.
- **Stutter:** CPU scaled blits dominate; `fast2/fix7-stutter` (blit planner + shader cache cap) — **unbuilt**.
- **RPCS3 patches:** only "Unlock FPS" exists for 01.00; user decided to keep the no-patch rule.

**First next action:** build `fast2/fix7-pause` (+ `fast2/fix7-stutter`) on top of `claude/fix7`, run the pause probe
(`local/ratchet/work/fix7-pause/pause_probe.sh`) and a walking run (`local/ratchet/work/fix7-stutter/tools/walk.sh`).

## Cloud follow-up (no game, no GPU) — 2026-10-23

Branch `claude/jolly-pasteur-z3etd7` (base `8c66615`), source-level only, verified with
native Vulkan unit tests on lavapipe + validation; **no game build, run or FPS figure**:

- Alias merge / equal-footprint rebind now copy whole-texel subpixel blocks from the old
  visual companion instead of nearest-upscaling native pixels (native path untouched;
  unmapped texels keep native reconstruction; fully mapped transitions skip the sync;
  `ANYPS3_RENDER_SCALE_HISTORY=0` disables it for A/B; ~1 ms host cost per plan-cache miss).
- Typeless D24S8-as-ARGB8 RTT keeps visual depth/stencil subpixels (one-word aspects).
- `PERF RENDER_SCALE` queue/drain counters are run totals (were printed as 0); new
  `history_copies`/`history_texels`.
- A visual draw reuses its native draw's pipeline (one lookup per pair; saving unmeasured).

First next action on the real PC: new build from this branch, quiet scale-2 run (compare
803: 45.52 FPS), same-flip native/visual capture. Stable 60 FPS at scale 2 and the
final-image dependency chain remain unverified.
[Record](experiments/2026-10-23-cloud-render-scale.md).

## Resolution-scaling milestone — 2026-10-23

Implemented on `feat/render-scale`, based on current `origin/main`:

- Native guest rendering plus visual companions at integer scales 1–4; typed
  render-to-texture sampling, capture and presentation use the scaled images.
- Visual draw batching reduces render-pass switching. Quiet original Great Clock
  run803 at2560×1440 averaged **45.52FPS**, with recorder enabled. Stable60FPS
  and full-scene high-resolution detail remain unverified.
- GUI **Video → Internal rendering resolution** persists global/title overrides
  and passes the typed value to supported builds. Installed release unchanged.
- `scripts/ratchet_fast_menu.py` automates START through the original input path.
  This accelerates boot navigation; a complete intro bypass is not implemented.
- Same-flip native/visual captures confirm a modest visual improvement. Known
  merge/rebind and unsupported RTT paths still reconstruct native detail. Their
  exact contribution to the final world image remains untraced.

Validation: complete802 runtime suite reconciled to925 passes+7 skips after one
unchanged concurrency fixture passed its uncontended rerun; the original failed
report is retained. Later changes have focused tests. After rebasing, all GUI and
manager tests passed (146 passes,4 skips). Final review has no remaining confirmed
blocker. Original game bytes are unchanged; physical gamepad was not tested.

Final build805 reached the original Great Clock with matching native/scaled
captures, exact original PT_LOAD bytes and no guest STOP. No runtime jobs remain
active. User requested completion and branch publication of these milestones. Next
rendering investigation: trace the final scene's image dependencies and preserve
visual history through the implicated alias/conversion; then measure quiet FPS.
[Evidence and exact scope](experiments/2026-10-23-render-scale.md).


## Wave 6 result — 2026-10-21 ~17:10 (Opus 5.5 coordinator; Opus 5.5 medium agents; gpt-6.1-sol / gpt-6-astra)

**State: the original level holds the 60 Hz VSYNC cap with ~3–4 ms headroom per frame; the strict acceptance is met on the
per-frame criteria, not on the two-second minimum.** Branch `claude/wave6-60fps` = `main` + integration `fast2/integ473` 5c266e2
+ GUI branch `feat/gui-pad` (audio toggle, SIXAXIS/rumble/PS button). Its exact runtime is **build 706** (snapshot verified equal to
the branch's `anyps3/` and profile).

| runs (QUIET, original level great_clock_a) | FPS mean | worst 2 s | flips in one vblank | main_ppu / rsx_worker ms/frame |
| --- | --- | --- | --- | --- |
| 706a / 706b (branch build) | 59.71 / 59.69 | 57.43 / 55.42 | — | 11.0 / 12.2 |
| 706c / 706d (branch build, ring) | 59.51 / 56.79 | 55.49 / 32.95 | 99.5 / 95.7 % | 11.0–11.9 / 12.4–13.1 |
| 705a–d (integration 5c266e2 = 706 without the GUI/pad branch) | 59.77 / 58.39 / 59.91 / 59.89 | 58.4 / 42.0 / 59.0 / 58.0 | 99.8 / 98.6 / 99.9 / 99.9 % | 10.6–11.1 / 11.8–12.5 |
| 703c / 703d (reference) | — / 59.94 | — / 59.49 | 99.6 / 100.0 % | 11.1 / 12.2 |
| 472a (main before this branch) | 46.68 | 32.0 | — | 14.0 / 13.4 |

706d: the first 16 s of its window were RSX-worker-bound bursts (worker busy 20–38 ms/frame, misses.py `work/integ473/i6/miss-706d.txt`),
then a steady 60; 705b had a similar burst at the end of its window → occasional worker bursts in some runs, not specific to the GUI/pad
branch; cause open (next step: ring + RSX stage profile of such a burst). Work span p50 12.6–12.9 ms, p99 15.6–16.5 ms (706c/d).
Acceptance (AGENTS.md): mean >= 59.7, >= 99 % one-vblank flips, main thread and RSX worker <= ~13 ms — met in most runs; the worst
two-second interval (>= 59.5) is not met consistently (single dips, partly from desktop use during the runs). **Headroom measured with
`ANYPS3_VBLANK_HZ` (671b/c): 75 Hz is not held (55.9 % one-vblank), 90 Hz runs at half rate** — frame cost ~12.3–13.5 ms.
Pipeline-cache persistence: run-wide pipeline creation 184–241 ms vs ~800 ms before.

**Tests:** full suite on 5c266e2: 185 modules, 877 tests, 0 failures, 0 skips (the 25 `tests/manager` tests are not part of the
partitioned run: 38 run separately on the branch, OK, 4 opt-in installer skips); native pad tests 8/8 OK on the branch.
**Reviews:** gpt-6-astra reviews spuput-1, rsxhead-1, fpspu-2, rsxw4-1, warmup-blit-4 — every finding fixed with a test and a
mutation check (`local/ratchet/work/_reviews/`). Open, documented: FIFO window ships OFF (`ANYPS3_RSX_FIFO_WINDOW=1`; a drain-bound test
failed 4/6 with it, cause unknown); the FP fast path 559472e has no demonstrated gain (kept: exact per review, 703/705 measured best with it).
**Not verified:** physical gamepad (DualSense) and GUI build/play with the real game — the user tests next; audible audio (never on this PC
by agents). **Hardware:** RAM bit flips on this PC (verified, see the wave record).

**Local data cleanup (2026-10-21, user-approved):** ~323 GB of `local/ratchet` deleted (452 → 127 GB): all per-run game cache
scratch dirs `cache/anyps3-*` (`cache/vk` kept), all `native-bundle-*` except 706, binaries/generated C++/objects and all but the last two
frame dumps of every `native-virtual-*` except builds 380, 386, 460, 472, 478, 603, 660, 703, 705, 706 (run summaries, logs, evidence
images kept), object-cache entries older than 24 h, frametime rings except 703c/d, 705a–d, 706c/d, all but the 6 newest
`sample-profile-*`, agents' `work/*/prof` dumps, all agent git worktrees and `work/*/base` copies (branches and STATUS/RESULT notes kept;
MAINSLACK2's untracked scripts in `work/spuput/untracked/`). Manifest: `local/ratchet/cleanup-2026-10-21-plan.tsv`; tool
`local/ratchet/tools/cleanup_2026_10_21.py`. Game inputs, source snapshots, `codex/` captures, suites and docs untouched. Older run/build
numbers cited in records may therefore lack their binaries; rebuild from `source-snapshot-N` if needed.

**GUI follow-up (branch `claude/gui-keys-fix`, 2026-10-21):** key setup explained in the wizard and CLI errors (JSON format, purpose,
sources), private hex entry, validation against the selected PUP/EBOOT, opt-in "Download from RPCS3" (pinned commit ffba2bf, SHA-256
verified, confirmation dialog; no keys in the repository); recovery from failed imports (Resume/Remove unfinished titles, adoption may
replace an unfinished title without userdata/builds), job removal/clear, explanations for disabled actions (Build before Play), ISO-step
layout and many state-refresh fixes. Tests: 132 manager + 11 GUI pass (4 opt-in installer skips). Real-game acceptance in a temporary
library: adopt-ratchet OK, all 193 native units built in 22 min, run rendered the original intro (897 flips, 15 captures, audio off).
Reviews: gpt-6-astra on GUIFIX (findings fixed by the agent) and on the keys commit (review-guikeys-1: 2 Medium + 1 Low, fixed with
mutation-checked tests). Not verified: live HTTPS download, the exact compositor paint artifact the user saw (not reproduced offscreen),
physical controller. Evidence: `docs/manager/evidence/guifix/`, `docs/manager/evidence/guikeys.md`.

Open GUI item (user test 2026-10-21, addressed by the follow-up above): ISO import stops at "missing-key: Set the user-supplied pup key file first" without saying what the
file is; on this PC use **Adopt Ratchet** (reuses `local/ratchet/firmware-keys.json`, `app-key.json` and the decrypted
`firmware-verified/`) or set the PUP key to `local/ratchet/firmware-keys.json`. The message/wizard should explain the PUP key file.

Next candidates (unverified ideas): chain SPU native functions without the dispatcher, SPU FP slow path, 16-byte `atomic_read`, the FIFO
window bracket failure, then render resolution scaling (`local/ratchet/work/_tasks/RESSCALE.md`). Record:
[FAST2 record, wave 6](experiments/2026-10-20-fast2-wave.md#wave-6-results-2026-10-21).

## Local data cleanup — 2026-10-25 (user request)

**Verified:** `local/ratchet` 169 → 81 GB (`df /home` 604 → 521 GB used). Tool `local/ratchet/tools/cleanup_2026_10_25.py`,
manifest `local/ratchet/cleanup-2026-10-25-plan.tsv`, log `cleanup-2026-10-25-apply.log` (freed 67.4 GB).
- Deleted (reproducible or duplicates): binaries/C++/objects of every build except **472, 706, 805, 820, 860, 870**; every
  `native-bundle-*` except 860/870; byte-identical binary copies in run dirs (checked against build N); `cache/anyps3-*`
  (`cache/vk` kept); nested `local/ratchet` copies inside `codex/*` workspaces (no code/test references them); `__pycache__`;
  `work/suite*/tree`; `sample-profile-*` except the 2 newest; `waketrace-*`.
- Compressed losslessly with zstd (read with `zstdcat`): run/build `stderr.log`/`stdout.log`/`to-menu.log` > 1 MiB, `ft-*.bin`
  rings, run `frametime.bin`, `sdl-disk.raw`. Older records citing these paths mean the `.zst` file.
- Source snapshots: git history starts at the build-460 state (`ca7604a`), so snapshots 256–459 are **not** in git. All
  snapshots except 472, 706, 805, 820, 860, 870 are in `local/ratchet/archive/source-snapshots-until-2026-10-25.tar.zst`
  (5.4 MB, sha256 in `.sha256`, directory list in `.list`; verified by extraction + `diff -r`, 365/365 identical). Restore:
  `zstd -dc --long=27 archive/source-snapshots-until-2026-10-25.tar.zst | tar -xf - source-snapshot-N`.
- Git: worktrees of merged, clean branches removed (branches kept); `work/render-scale/gui-tree` kept (uncommitted edits
  that differ from main).
- Not touched: game inputs, firmware, hdd0, frames/evidence images, codex captures/tests, notes, build 870 (compiling) and
  its snapshot/bundle. Object cache pruned after 870 linked: 22 → 5.4 GB, entries before 2026-10-23 deleted (4932, 16.6 GB);
  870 had 0 cache hits (full recompile) and its 193 fresh objects are kept.
- **Round b (evening, after Codex's 871–883 work ended; `ANYPS3_CLEANUP_ROUND=b`, plan `cleanup-2026-10-25b-plan.tsv`):**
  freed 30.0 GB (binaries of 860 and 871–881, bundles except 882/883, run-dir binary copies, `cache/anyps3-*`) plus 4.7 GB
  object-cache entries older than 2026-10-25. Builds with binaries now: **472, 706, 805, 820, 870, 882 (= commit 31d50d8,
  snapshot verified file-identical), 883** (882 + texture-cache counters). Snapshots 860 and 871–881 are in
  `archive/source-snapshots-860-881.tar.zst` (verified by extraction + `diff -r`). Result: `local/ratchet` 88 GB,
  `df /home` 526 GB used. Evidence logs, frames, rings and JSON of every run are kept.

## Older state records

FAST2 wave 4, the 2026-10-20 sandbox assessment, installer/manager notes, the historical notes, the
history of real stops 256–339 and the old "first next actions" (2026-10-14 to 2026-10-21) are in
[history/CURRENT-2026-10-14-to-10-21.md](history/CURRENT-2026-10-14-to-10-21.md).

## Where the game is

The original game passes through boot, videos (skippable with START), the `front_end` loading, the title screen and reaches the
**original main menu** (NEW GAME / LOAD / COMMUNITY / OPTIONS / DELETE) ~2 min after start; the menu responds to the
virtual pad (down arrow → LOAD). Rendering: custom Vulkan RSX (NVIDIA RTX 4070 SUPER), PPU/SPU statically translated.
The historical performance of the menu in build 339 was a median 59.94 FPS; this figure is not the performance of the level in build 380, which has 15.02 FPS.

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
