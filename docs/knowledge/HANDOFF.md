# Handoff to the next instance

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
