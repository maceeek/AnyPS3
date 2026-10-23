# Current state and goal

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

## FAST2 wave 4 handoff — 2026-10-20 ~22:25 (ended on user instruction; Claude Opus 5.5 + Sonnet 5.5 agents, gpt-6-astra reviews)

**Goal (60 FPS with headroom) not reached.** Best measured steady run: **`660a` 55.96 FPS mean** (median 58.5, two-second min 47.5,
max 60; repeat `660b` 55.06), correct level frame, no STOP. Build 660 = `fast2/integ473`@7da3eb3 (main 6ce888b + RSXCPU3 + FRAMETIME)
+ `fast2/warmup` cd781d8 (rsxblit batch path) + d1d8417 (diagnostics); it does **not** contain the SPU work of 603.
Cold phase after level load in 660a: first 30 s **32.7 FPS**, sustained >= 30 FPS after **12 s** (472a: 13.5 FPS / 36 s).
Cause (WARMUP, verified): CPU-side NV3089 scaled blits on the per-pixel path, not GPU waits.
`603a` 54.04 FPS (SPU translation) remains the best build with the SPU work. **`main` runtime = 472 + ZeroTable (46.7 FPS class);
no runtime promotion this wave.**

**Open at handoff (first next actions):**
1. **Build 473** (`fast2/integ473` bee67ff = SPU through 87dce76 + RSXCPU3 + FRAMETIME + review fixes 1a–1f + both review rounds +
   warmup cd781d8/d1d8417) was **still compiling** at handoff (scope `anyps3-compile-473`, log `local/ratchet/compile-473.log`, last
   lines `rc=…` and the source-hash check). Measure: `measure_with_gpu.sh 473 473a ANYPS3_RSX_HOST_PROFILE=1`; compare with 660a/603a.
2. **Blit batch review (gpt-6-astra, `local/ratchet/work/_reviews/review-warmup-blit-1.md`): 1 High (destination aliases), 3 Medium
   (gap notifications, swallowed observer failure, sparse enumeration) — NOT fixed in any commit.** Fixes are written but uncommitted in
   `local/ratchet/work/warmup/tree` (backup `local/ratchet/work/warmup/wip-review-fixes.patch`, `wip-rsx_blit_frozen.py`); one observer
   test has a test bug; mutation checks pending. Commit them, re-merge `fast2/warmup` into the integration and rebuild before promotion.
3. SPUWAKE: the main-thread SPU wait (5.09 ms/frame in 680a) is **94 % lane compute**, not parked lanes; lanes lose 10–26 % of run
   wall time off CPU. 681a (traced, 48.71 FPS, perturbed): 5.69 ms/frame off CPU inside lane runs = owner calls 0.97 (lane 0x1000200
   polls lane 0x200's LS ~106×/frame, each pausing the peer), pauses 0.41, reader gate 0.04 (gate/raw permits **ruled out**), 4.50
   uninstrumented (3.03 on the critical path, lanes 0x2/3/4000200). **First fix (derived, strong):** lane PUTs into RSX local memory
   (EA 0xc2xxxxxx) block on a synchronous `queue->call(HostAccess)` to the RSX owner (`GuestMemory::notify_parallel` →
   `renderer_access`, `anyps3/rsx_game_worker.py:134–142`): ~75/44 µs off CPU per PUT on lanes 0x3/0x4000200, ~43 PUTs/frame ≈ 1.0 ms/frame
   on the critical path. Verify with a `HostAccessWait` trace event, then make the request non-blocking with exact ordering semantics.
   Details: `local/ratchet/work/spuwake/RESULT.md` (branch `fast2/spuwake` 626d9c8, build 681).
4. Then: full suite on the final integration, gpt-6-astra review of the merge, promotion to main.

Agents' handoffs: `local/ratchet/work/{integ473,warmup,spuwake}/{STATUS,RESULT}.md`. Record:
[FAST2 record, wave 4](experiments/2026-10-20-fast2-wave.md). Also new on main this wave: the game-library manager CLI + Qt GUI
([usage](../manager/README.md), synthetic tests only; no original-game or physical-controller acceptance yet).
No game run is live at handoff; build 473 may still be compiling (it ends by itself).

## Latest assessment — 2026-10-20, sandbox analysis

**Goal remains unmet:** the original game with functional gamepad control and 60 FPS with headroom.
**Best checked steady run without the sampling profiler: build 603, `native-virtual-603a`, 54.045 FPS**
over 60.060 s (median two-second rate 55.95, range 31.5–60). The saved original level frame was inspected.
This is existing-run evidence rechecked now, not a new game run. Physical gamepad remains unverified.

**Verified source state:** initial clean `main` = `abf2450`; runtime = build 472 plus ZeroTable (`6ce888b`),
not snapshot 472 verbatim. SPUFAST3 and FRAMETIME are separate branches.
`fast2/integ473` = `7da3eb3` includes RSXCPU3 + FRAMETIME, not SPUFAST3, and is not a verified game build.
The independent pacing candidate 642a reached 52.405 FPS; do not add its gain to 603's gain.
Snapshot source hashes for 472/603/642, artifact hashes of 472a/603a/603w/642a and original PT_LOAD bytes
were rechecked successfully. The four additional 603 SPU overlay source hashes match original EBOOT bytes.

**New offline analysis:** the existing 603w capture attributes approximately **3.19 ms/frame to the engine-counter
wait and 1.05 ms/frame to the job-flag wait** (sampling estimates). In 45.6% of engine-counter wait samples,
all five ig lanes were sampled and classified as parked; missing lane samples do not explain this observation.
The ≤2 ms target is not demonstrated. Trace completion publication and wakeup latency before attributing every wait
to slow SPU arithmetic. Seven sample files passed committed-chunk integrity checks, with no dropped records or torn tails.

**First next actions (proposed, not implemented):**

1. Fix integration 473's FP whole-block admission, per-instruction GPU VP budget, bounded host-request drain and
   real page-coherence/flip-order tests. Check SPU inline WRCH interrupt-boundary equivalence.
2. Include the SPU changes through `87dce76` (measured build 603), **keep the later `b2a0544` AVX DMA change out**
   pending a valid concurrent-memory protocol. Plain vector accesses plus signal fences do not replace atomic shared accesses.
3. Repair the frametime ring's completion protocol before relying on precise percentile results:
   **642a's ring actually wrapped** (9,093,360 reservations / 8,388,608 slots). This does not invalidate its separate FPS log.
4. Keep the asynchronous recorder OFF (624b teal frames unresolved). Validate targeted semantics, build a fresh snapshot,
   measure repeated quiet-machine A/B runs and cold start, then complete the full suite including Vulkan before promotion.

**Tests:** suite 472 did run: 813 tests, zero skips/drift, one failing module (`test_ppu_fp_lazy`, recorded timeout).
The existing isolated pass and ZeroTable fix do not constitute a full-suite pass of the combined candidate.
No tests, builds or game runs were performed in this analysis.
**Processes:** host process/GPU/input visibility is restricted by the sandbox; host inactivity was not verified.
Runtime/profile unchanged by this round; documentation and offline analysis artifacts added.

Full findings, source locations, fixes, verification contracts and commands:
[sandbox analysis](experiments/2026-10-20-sandbox-analysis.md).
Evidence: [analysis JSON](../../local/ratchet/analysis-2026-10-20-sandbox/evidence.json).

## Desktop installer — 2026-10-21

**Verified in an isolated test home:** the GUI-first README and per-user installer (`scripts/install.sh`) were prepared on
branch `feat/gui-installer` (base `3e0f5f9`) and merged into `main` on the user's instruction (worktree removed).
Four real installer integration tests pass: install/update, failure preservation, development isolation,
launcher conflicts and truncated downloads. The app uses private source/venv snapshots, a separate
`anyps3-library` and separate preferences. Runtime/profile are unchanged; no new game or controller acceptance.
Merged locally, not pushed: the README's curl command works only after `main` is published. [Commands, evidence and limits](experiments/2026-10-21-gui-installer.md).

## Manager worktree — 2026-10-20

**Verified manager milestone:** CLI and PySide6 wrapper implemented on
`fast2/manager-gui` (base `abf2450`), fast-forwarded into `main` at `30c322d`; 30 scoped manager/GUI tests and a real-CLI
offscreen integration preview pass. Original-game and physical-controller
acceptance were not performed. Runtime player-mode and pad-config work packages
remain separate. The original game/menu/gamepad performance goal is unchanged.
See the [manager record](experiments/2026-10-20-manager-gui.md)
and [usage](../manager/README.md). Runtime notes below are historical;
this wrapper does not establish a new best game build or FPS result.

## Historical notes — superseded by the assessment above

The older “live tree”, “first next action”, test and process statements below describe their original rounds.
They are retained as history and must not override the latest assessment.

**2026-10-20 FAST2 wave — ended on user instruction ~15:15 (Claude Opus 5.5 + Sonnet 5.5 subagents, Codex gpt-6.1-sol reviews; PC with
the game and the RTX 4070 SUPER).** Goal: the game at 60 FPS with headroom — **not reached**. **Best verified build: 472 = live `main`
b3281ed (live tree = `source-snapshot-472`), measured `472a`: 46.68 FPS mean (median 45.97, min 32.0, max 58.0), correct image, no
STOP** — rsx_worker 13.4 ms/frame, main thread busy 14.0 ms (~6.6 ms of it waiting for SPU jobs on the ig lanes), GPU busy 6.3 + 0.6 ms
(DMA prelude), no GPU finish waits. Day's path: 460a 22.59 → 470a 29.70 → 471a 31.18 → 472a 46.68 FPS. Frames are VSYNC-quantized:
60 FPS needs every path under 16.7 ms on every frame. Not done: the whole suite over 472 (over 471: 806 tests, the 2 failures fixed on
main); physical gamepad not verified this round (virtual pad drives menu → LOAD in every measurement). No agent, build or game runs.
Record: [FAST2 record](experiments/2026-10-20-fast2-wave.md); handoff: [HANDOFF.md](HANDOFF.md).

**2026-10-19/20 cloud optimization round (Claude Opus 5.5, cloud container without game data or GPU).**
User goal unchanged: the game at 60 FPS with headroom. Record with all evidence, benchmarks and limitations:
[cloud optimization record](experiments/2026-10-19-cloud-optimization.md). Branch `claude/happy-franklin-2eocfp`,
commits `43c6b27..4b3723a` (HEAD when this was written; `43c6b27` = snapshot 460).
**The best verified game build is still `native-virtual-460` (22.59 FPS in the original level). This round has NO game
build and NO FPS figure:** the container has 4 vCPU, no GPU (Vulkan = Lavapipe) and no game data. 60 FPS not reached;
menu/gamepad status unchanged (physical gamepad not verified). The working tree of this branch is snapshot 460 plus the
changes below, so it is no longer identical to snapshot 460 or to any built and measured game tree.

What changed (status: **verified** = by the named native regression test with an independent expectation, **derived** =
by reasoning only; nothing is verified in the game):

- **PPU** ([record, PPU](experiments/2026-10-19-cloud-optimization.md#ppu)): host SIMD paths for the VMX subset
  (verified, `tests/test_vmx_simd.py`); **semantic fix** of rlwinm/rlwnm/rlwimi with MB>ME (wrap mask, verified,
  `tests/test_ppu_rotate_count.py` red on the previous lifter); lazy FR/FI/FPRF (verified by `tests/test_ppu_fp_lazy.py`;
  applies only once XX is set, measured +13% on a synthetic float kernel while XX is clear); spin hints, one host `pause`
  per block (guest steps unchanged, verified; benefit derived); same-page indirect dispatch (verified; no measurable
  effect in the synthetic call kernel); 8-aligned 16-byte loads as two 8-byte atomic loads (covered by existing tests,
  no dedicated one).
- **SPU** ([record, SPU](experiments/2026-10-19-cloud-optimization.md#spu)): unbound-image stop restored; pool-lane
  slice 4096; exact change-counter filter for the reservation-lost check (exactness derived, behaviour verified by
  `tests/test_spu_reservation_filter.py`); busy-poll parking (verified for results and wake sources by
  `tests/test_spu_pool_poll.py`; kill switch `ANYPS3_SPU_POLL_PARK=0`; that the level's CellSpursKernel0 matches a
  parked pattern is a **hypothesis**).
- **Memory** ([record, Memory](experiments/2026-10-19-cloud-optimization.md#memory-ppu-and-spu-reservations); PPU
  lwarx/stwcx. and SPU getllar/putllc): single-mapping reservation fast path, verified by
  `tests/test_reservation_fast_path.py`; synthetic `k_atomic` 70.7 → 40.5 ns per guest instruction.
- **RSX** ([record, RSX](experiments/2026-10-19-cloud-optimization.md#rsx)): register-bank change epochs, SIMD index
  decode, fragment prologue interpolating only used inputs (each verified by its test; the prologue effect is GPU-side
  and not measurable here); **MSAA plane runs** (verified for pixels and validation, effect unmeasured; kill switch
  `ANYPS3_RSX_PLANE_RUNS=0`); the stale `tests/test_rsx_gpu_upload.py` oracle fixed (test only, no runtime change);
  occlusion-query elision is a **candidate only, not implemented**.
- **Runtime fix** ([toolchain pitfall](experiments/2026-10-19-cloud-optimization.md#environment-limits-verified)):
  the RSX owner thread waited on `steady_clock::time_point::max()`, which overflows on the cloud's conda libstdc++ and
  made `test_par6` hang in 6 of 8 cases (diagnosis verified with gdb on the cloud toolchain); the owner loop now waits
  without a timeout when there is no deadline, as `par_worker.py` already did for SPU lanes.
- **Scheduler** ([record](experiments/2026-10-19-cloud-optimization.md#scheduler)): an Audio engine slot whose next tick
  is in the future returns immediately; the record states no dedicated test for it.

**Verification actually done:** native regression tests of the changed areas and synthetic micro-benchmarks on a shared
cloud CPU (relative only, see [Measurements](experiments/2026-10-19-cloud-optimization.md#measurements-synthetic-this-cloud-cpu)).
**Not done:** no game build, no game run, no FPS figure, no frame comparison, no physical gamepad.
**Full regression suite of this round:** frozen worktree of `4b3723a` (same code as the final branch), `run_suite.py -j3`, real Vulkan (Lavapipe), split by a container restart (139 + 19 modules): **158 modules, 782 tests, 774 pass**. The 8 failing tests and 2 modules failing at import are environmental or pre-existing: tools under `local/ratchet/` that this container lacks (test_sample_profile, test_sample_profile_integration, test_spudyn_profile, test_spuperf2_capture ×2); the 30 s compile limit with this toolchain (test_program ×2, test_services ×1, all pass with a longer limit); two failures identical on a worktree of `43c6b27` and passing on the PC in the 386 suite (test_rsx_perf_decode `special_lanes`, test_savedata_vsh non-variadic `assert`). Details in the [record](experiments/2026-10-19-cloud-optimization.md#full-regression-suite).

**Strongest open hypothesis:** part of the ~30 ms/frame GPU busy time in the level is render-pass switching;
the level replay had ~1712 render passes per frame (verified from the comment at `anyps3/vulkan.py:222`; 76 with
multiview), and MSAA plane runs should cut passes to roughly the multiview count. Unverified; distinguishing test on the
PC: the same level window with `ANYPS3_RSX_PLANE_RUNS=0` and `=1`, comparing `PERF GPU busy_ns`, render passes per
frame and FPS ([RSX item 5](experiments/2026-10-19-cloud-optimization.md#rsx)). Caveat from the code comment
`anyps3/vulkan.py:222-224` (historical RTX 4070 SUPER measurement, not repeated in this round): the opt-in multiview
variant with 76 passes raised GPU wait 5.6 → 30 ms/frame with no CPU gain, so fewer passes alone has not been shown to
lower GPU time.

**First next action on the user's PC:** build a new unused `N` (record section
[How to verify on the PC](experiments/2026-10-19-cloud-optimization.md#how-to-verify-on-the-pc): `build_game` →
`virtual_pad.py build` → `local/ratchet/tools/measure_level.sh N Na`), compare `native-virtual-Na/level-summary.json`
with 460a (22.59 FPS; RSX worker 28.3 ms, main_idle 23.2 ms, GPU busy 30.0 ms per frame) and check that
`level-frame.ppm` matches. Then A/B runs of the same level window without rebuilding:
`ANYPS3_RSX_PLANE_RUNS=0` vs `=1` and `ANYPS3_SPU_POLL_PARK=0` vs `=1`. Run the whole suite there with
`ANYPS3_REQUIRE_VULKAN=1`. Audio stays silent (`SDL_AUDIODRIVER=dummy`, `ANYPS3_AUDIO_SINK=null`, never
`ANYPS3_AUDIO_AUDIBLE=1`). Handoff details: [HANDOFF.md](HANDOFF.md).

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
