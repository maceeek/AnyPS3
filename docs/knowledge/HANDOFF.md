# Handoff to the next instance

## Cloud round HIRES3 — 2026-10-28 (cloud container; goal NOT reached, nothing measured in the game)

Branch `ccr-8139488c-79xg7b` (pushed; based on main 95756e2). Read the
[record](experiments/2026-10-28-cloud-hires3.md): device-memory pool, DrawJob borrowed pointers, report floors,
vertex-constant chunks, typed-RTT literals, `bench/rsx_fifo_bench.py`. Cloud items done from the 2026-10-27 list: 1 (suite: 17 pre-existing failures also on base, 1 regression fixed;
see record), 2 (three of the five per-draw cuts; FIFO decode measured and left unchanged), 5 (pool). Not started: 3
(two-thread worker; the record adds the derived constraint that a decode-only front saves ~1 µs/draw), 4 (visual MSAA load
path, constants via UBO, pass-begin cost — finding documented), 6 (horizontal 8/3). No process of this round is live.

First next actions on the real PC: (1) one integration build of the branch; QUIET alternating 1440p walks against
1031/1034; the same binary with `ANYPS3_RSX_MEMORY_POOL=0`; (2) full frame ring + `hitch_marks.py` to see whether stutter
classes A/B disappear (B persisting refutes the first-touch hypothesis); (3) scale-1 walks for rsx_worker ms/frame and
µs/draw; (4) `ANYPS3_RSX_RTT_LITERALS=0/1` with `ANYPS3_RSX_VK_MARKS`.

## HIRES2 wave — 2026-10-27 evening (ended on user instruction; goal NOT reached)

Read the top of [CURRENT.md](CURRENT.md) and the
[HIRES2 record](experiments/2026-10-25-hires-wave.md#hires2-wave-result-2026-10-27-evening-coordinator-claude-opus-55-agents-opus-55-high-goal-not-reached).
Branch `claude/hires2-wave` merges `fast2/hires2w` + `fast2/hires2s` into main f92681c (no overlapping files). Rules
for the agents: `local/ratchet/work/COMMON-HIRES2.md`; agent handoffs (with exact commands and tools):
`local/ratchet/work/hires2w/RESULT.md` (instance 2 section), `local/ratchet/work/hires2s/RESULT.md` (instance 5 section).
On the user's instruction all queued detached builds/runs were stopped at session end; runs cut short are invalid
(listed in the record). No agent, build or game run is live.

First next actions: (0) repeat 1002 vs 960 alternating at scale 1 (1002a1s1 looked slower, probably disturbed — must be
resolved before merging cf7a6ef to main) and measure the already-built 1035 (MSAA on/off) vs 1031; (1) build the merged branch as one integration build and measure it alternating with 1031 at scale 2
(QUIET, `work/hires2s/measure-walk-*.sh` pattern), then with `ANYPS3_RENDER_SCALE_MSAA=1`; (2) main-thread SPU-job wait
(~3.9 ms/heavy frame): analyse `work/hires2w/tools/wallsplit.py 1000w1s1` (what the SPU lanes run while thread 1 spins; a first attempt
under `timeout 1500` produced no output — run it without the timeout, redirected to `work/hires2w/logs/`);
(3) RSX worker heavy frames 21 ms → ≤ 13 ms (ranked cuts in hires2w/RESULT.md, or a front/back thread split);
(4) GPU at scale 2: second sample plane via MSAA, native pass-begin cost; (5) attribute the 140–180 ms walking hitches
with a full frame ring (not pipeline compiles). Open user decision: horizontal scale 8/3 for full 2560-column detail.

### Cloud instance next steps (no game data, no NVIDIA GPU; lavapipe + validation only) — 2026-10-27

A cloud instance cannot run the game or measure FPS. It should deliver **exact, gated, unit-tested source changes on its
own `claude/*` branch based on `claude/hires2-wave`**, each with a native regression test (independent expectation) and
a micro-benchmark where it targets CPU cost; the real PC then builds and measures them alternating. Priorities, from the
verified HIRES2 evidence (`local/ratchet/work/hires2{w,s}/RESULT.md` exist only on the PC; the record
`experiments/2026-10-25-hires-wave.md` has the numbers):

1. **Full regression suite on `claude/hires2-wave` (lavapipe).** Only targeted modules ran after the merge. Report
   failures with the first differing value; fix or rebut each.
2. **RSX worker per-draw CPU cuts (target: heavy frames 21 → ≤ 13 ms, ~9.1 µs/draw marginal; each cut is ~0.3–1 ms):**
   - per-draw `shared_ptr` / `GraphicsImage` array copies into `graphics_draw`/DrawJob (~5.4 % of the worker): pass
     borrowed references or a pooled draw descriptor; keep lifetimes explicit (the recorder thread consumes them);
   - `texture_image` per-unit memo (like the texture-metadata memo 7375cb6, generation-bumped on erase/clear) and caching
     the fragment-shader string that `rtt_image` rebuilds;
   - batch label/report publication (~230 occlusion reports/frame: Label construction, vector growth, GpuPages claim per
     publish) into one publish per FIFO run, keeping guest-visible order and values exact;
   - `rsx_fifo_values` FIFO word reads (~2.6 %) and the decode of non-draw methods (~23 % of heavy-frame worker time):
     add a per-method cost counter (gated) and a faster register-only path;
   - clears (~2.8 %).
3. **Design + prototype behind a switch: a two-thread RSX worker (front: FIFO decode/state/guest reads; back:
   prepare/record).** Estimated alone to leave ~16–17 ms in heavy frames, so it combines with item 2. Write the ordering
   rules (Session, guest-memory admission, labels/reports, HostObserver) first; tests must cover label/report ordering
   and guest-visible memory under concurrent writes.
4. **GPU at scale 2 (correctness testable on lavapipe):**
   - visual 2× MSAA (a57960b): add the plane→sample load path so a target that is not fully cleared each frame can still
     use the working image (lavapipe lacks 2× MSAA — test the fallback path there, mark the NVIDIA check as pending);
   - typed-RTT raw-byte decoder shader: per-byte integer div/mod addressing at 3840×1408 — restructure (exact output,
     compare against the current shader byte-for-byte on lavapipe);
   - shader constants read through a storage buffer with dependent loads → uniform-buffer layout (exact; image tests);
   - native render-pass begin costs 12.6 µs vs 1.55 µs visual (1031sm1): read the code for per-begin framebuffer/image
     work, load ops and barriers; propose a fix with a test.
5. **Stutter class A/B (Vulkan device memory, testable on lavapipe):** the 384 MB memory cache in
   `rsx_vk_resources.py` reuses exact-size blocks only and holds ~4 visual images at scale 2; frees evict via
   vkFreeMemory (up to 16.8 ms per call) and ~240 new allocations occur in streaming frames. Implement sub-allocation
   from large blocks (size classes / a buddy or linear allocator per memory type) with bounded, deferred frees off the
   worker's critical path; unit-test aliasing/lifetime with fences and the allocation counts.
6. **Design note only (user decision pending): horizontal scale 8/3** so 1440p shows 2560 genuine columns (today the
   960×704 scene renders at 1920×1408 and is resampled 4:3). Describe what anisotropic visual companions would change
   (sample grid, typed RTT, history copies, presentation) and the expected GPU cost.

Not suitable for the cloud: the main-thread SPU-job wait (needs the game and the `wallsplit.py` analysis of
`1000w1s1`), PPU codegen changes that need the original ELF, the 1002/1035 measurements and stutter attribution.

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


## Wave 6 — 2026-10-21 (ended on user instruction ~17:15)

Read the top of [CURRENT.md](CURRENT.md). Branch `claude/wave6-60fps` (pushed; the user merges it into main) = main + integration
5c266e2 + GUI audio/SIXAXIS/rumble; its runtime is build 706 (~59.7 FPS, ~3–4 ms headroom, 75 Hz not held). The user tests the
physical DualSense through the GUI (install from main, Adopt Ratchet, Build, Play — **GUI audio is on by default; switch it off on
this PC**). Fallback for playing the measured binary without the GUI: `local/ratchet/tools/play_physical.sh 706` (silent, SDL pads only).
No agent, build, suite or game run is live. Agent handoffs: `local/ratchet/work/{integ473,spuput,warmup,guiaudio,padmotion,guimerge}/`
(INTEG6 stopped at the API session limit after building 705; the coordinator finished its suite/measurements). Rules for the next wave:
AGENTS.md (QUIET runs, hardware, audio) and `local/ratchet/work/COMMON-FAST2.md` "Wave 6".

## Older handoffs (2026-10-14 to 2026-10-21)

FAST2 wave 4, installer, manager worktree, the sandbox assessment and the handoffs of 2026-10-14 to
2026-10-20 are in [history/HANDOFF-2026-10-14-to-10-20.md](history/HANDOFF-2026-10-14-to-10-20.md).

## Template for an experiment or handoff

```text
Date and author/model:
Goal and completion criterion:
Source state: commit + uncommitted files/hashes
Input: profile, game identification, relevant configuration
Verified fact: what exactly was observed
Sources: file:line @ commit / PDF title-version page N + SHA256
Runtime: exact command, log, exit/timeout, artifacts and counters
Changes: what is done and where
Hypothesis: reason, alternative, distinguishing test
Refuted: attempt and reason
Open: what we do not know yet
Next step: concrete first action
Limitations: what the test does not cover
```

## Check before takeover

- The paths actually exist; the reference checkout has a recorded revision.
- A fact comes from a specific source or a runtime artifact.
- The result matches the criterion of the original menu and gamepad; a diagnostic presentation is not mistaken for guest rendering.
- Failed attempts and hypotheses are visibly separated from confirmed behavior.
- The next step addresses the real last blocker, not a long-removed error.


## Older handoffs

Handoffs 238–246 (superseded): [runtime-handoff-246.md](runtime-handoff-246.md),
[runtime-handoff-237.md](runtime-handoff-237.md), [runtime-handoff-198.md](runtime-handoff-198.md).
Full previous wording of this file: `local/ratchet/HANDOFF-before-cleanup-2026-10-14.md`.
