# Higher-resolution rendering and faster startup, 2026-10-23

**Milestone branch:** original rendering above720p, visual draw batching, GUI configuration and quality diagnostics. Stable60FPS and full-scene high-resolution detail remain unverified. User requested completion, commit and branch publication of the current milestones.

**Verified baseline:** retained branch `claude/wave6-60fps` at 33030a9 contains
build 706 runtime; top-level main still contains older runtime. New isolated
implementation is `feat/render-scale` in `local/ratchet/work/render-scale/tree`.
The 706 executable matches saved 706a SHA-256. 705 snapshot hashes were also checked.
Prior integration worktrees were removed during the documented 2026-10-21 cleanup.

**Verified startup improvement:** original-game run `native-virtual-706fastmenu1`
reached the menu 37 seconds after launch. `frames/frame-000040.ppm` visibly shows
NEW GAME, LOAD, COMMUNITY, OPTIONS, DELETE. New helper sends virtual START through
the original cellPad path; no game bytes, assets or state are patched. The verifier
confirms original PT_LOAD identity and no guest STOP. Deliberate SIGKILL ended the
run after capture. `menu-acceptance.json` records visual evidence separately from
the generic verifier defaults. Physical gamepad was not tested.

Enable with `MENU_DRIVER=fast MENU_EARLY_TITLE=1` before the existing
`local/ratchet/tools/run_to_menu_ptrace.sh` or `measure_level.sh` command.
The helper and 7 passing offline tests are under `local/ratchet/tools/fast_menu.py`
and `test_fast_menu.py`; detailed commands/evidence in `fast_menu.md`.
Reproducible live launcher/log: `local/ratchet/work/render-scale/runs/menu-check.sh`
and `menu-check.log`. Compared with saved 705a-d, first driver START to menu changed
from 66–67 seconds to 28 seconds; this is not a repeated controlled A/B measurement.
This accelerates original boot navigation, not a saved-state/direct-menu restore.

**Verified rendering groundwork, not full game scaling:** Vulkan raster extent is
now independent of guest coordinates. Native image bytes remain unchanged in the
synthetic paired render test. Six 1.5x/2x, 1/2/4-plane tests prove added coverage/detail;
twelve generated shader tests cover perspective/depth/fragment position including
layered/multiview; a two-pass RTT test retains detail for normalized/rectangle
textures. Tests run on RTX 4070 SUPER with Vulkan validation. Exact logs and current
source paths: `local/ratchet/work/render-scale/STATUS.md`. Existing GPU batch,
graphics, RTT and GPU vertex regressions passed. A first push-range validation failure
was fixed before these passes. Full regression and original game on modified source
remain pending.

**Derived design:** filtering a scaled image cannot generally recover byte-identical
native coverage/derivatives/readbacks. Keep authoritative native images and add
visual companions; share immutable draw preparation. Astra research/review:
`local/ratchet/work/resscale-design.md`, `resscale-raster-review.md`.


## Paired visual implementation and review, 2026-10-23

**Implemented, awaiting an original-game scaled run:** `ANYPS3_RENDER_SCALE=2`
allocates 2x width/height visual companions (2560x1440 for a 1280x720 surface).
The native images remain authoritative for guest readback and query results.
Visual draws use separate status storage, skip native visibility queries, and
preserve guest coordinates. Upload, clear, copy, typed RTT, tiled sample-grid
rebind, presentation, and synchronous/asynchronous captures now select or update
the appropriate companion. Default scale remains 1; accepted configuration is
integer 1 through 4, subject to Vulkan device limits.

**Verified targeted GPU tests:** `paired-tests.log`, `alias-tests3.log` and
`capture-tests.log` under `local/ratchet/work/render-scale/`. Scaling tests use
NVIDIA with validation; existing RTT/drain fixtures select lavapipe themselves.
The added test covers actual Session typed RTT and tiled 2->1->2 roundtrip with
independent subpixel patterns and exact native-byte preservation. Seven paired
game-draw fixtures preserve full guest backing and native visibility counts.
Both synchronous and asynchronous capture fixtures produce the expected 64x32
output from a native 32x16 surface. Twelve scaling/drain checks passed together;
the new scaled-capture case passed separately. The final staging-preflight test
was added subsequently and is pending; the complete suite is also pending.

**Review:** `local/ratchet/work/resscale-paired-review.md` (Astra, read-only).
Confirmed capture and staging issues were addressed in the worktree. The latter
now budgets both blobs in Session and refuses insufficient space before either
draw is recorded; its targeted regression is pending.

**Known limitation, explicitly native fallback:** CPU/SPU accesses can discard
whole visual surface history. Equal-footprint/merge aliases and unsupported
raw-byte/typeless RTT conversions can reconstruct visual pixels from native
images. `PERF RENDER_SCALE` reports visual draws, native copies and host history
invalidations. A larger capture is not proof that every dependency retained
high-resolution detail. Preservation across partial host writes is unfinished;
any full-resolution acceptance requires actual scene detail evidence.

**Native checkpoint 800:** compilation succeeded with source hashes matching
its immutable snapshot (`compile-800.log`). It contains the earlier raster
foundation only. Run `800a` is in progress using the fast menu helper, dummy/null
audio, QUIET=1, 40 s warmup and 60 s measurement. Its diagnostic frame ring was
opened as relative path `1` and immediately moved to
`native-virtual-800a/frametime.bin` while the original mapping remained live.
This traced run must be distinguished from an uninstrumented FPS acceptance run.
Check actual processes and `measure-800a.log` before continuing. No high-resolution
original-game result or stable 60 FPS claim has been established.

**Updated run evidence:** 800a completed. `visual-acceptance.json` records the
inspected original Great Clock level frame; no STOP and original PT_LOAD bytes
unchanged. Traced mean 50.4507 FPS over 60.061 s (30 intervals), native 1280x720.
This does not establish stable60. The trace-overhead explanation is a hypothesis
until an uninstrumented comparison; it must not be assumed to explain every dip.

**Paired checkpoint frozen:** final targeted scaling tests passed in
`final-targeted.log`, including paired staging refusal before native recording
for both fresh and reused payloads. Immutable source snapshot/build801 now runs
(driver session8288; `build-801-driver.log`, root `build-801.log`, `compile-801.log`).
The complete module-partitioned suite runs on the frozen worktree (driver log
`suite-801-driver.log`, result directory `suite-801/`). Main source edits are
paused until this suite finishes, to keep source hashes stable. Neither a
scaled game run nor complete suite result exists yet. Next: inspect both results,
then run801 with ANYPS3_RENDER_SCALE=2 and the fast menu helper.

**Startup helper promoted to local default:** after 706fastmenu1 and800a reached
menu via the fast driver (37s and42s from launcher respectively),
`run_to_menu_ptrace.sh` now defaults to fast early-title navigation. Measurement
wrappers inherit it; `MENU_DRIVER=legacy` opts out and `MENU_EARLY_TITLE=0` uses
the conservative guard. Shell syntax checked after the defaults changed. Direct
start at the menu remains unimplemented. Further original-argument investigation
is recorded in `local/ratchet/work/intro-direct-menu-research.md`; unproven flag
strings must not be treated as supported skip behavior.

## Build801 results and next bottleneck

**Verified:** immutable build801 compiled successfully; source hashes match
snapshot801. Executable SHA256:
`10b92617feaea71d82b35507f83cc0af5886a64e4c3a4db8ec295d04d4bd85e3`.
`native-virtual-801scale2a` captures the original Great Clock at2560x1440.
Mean14.2210 FPS over60.191s; mainPPU12.265ms, RSXworker17.882ms,
GPUwait42.599ms/frame. Same executable `801native1`, scale1, measured
59.8741 FPS over60.058s; main10.582ms, worker12.429ms, GPUwait0.00579ms.
Both ran QUIET=1, WARM=40, WIN=60, dummy/null audio without FRAMETIME.
Both `visual-acceptance.json` files record direct frame inspection, unchanged
original PT_LOAD bytes, no guest STOP and deliberate SIGKILL after measurement.
This single native run does not establish the required repeated timing acceptance.

Exact measurement shape (unique RUN, SCALE=1 or2):
```bash
env -u ANYPS3_FRAMETIME QUIET=1 STRIDE=600 WARM=40 WIN=60 \
 SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
 bash local/ratchet/tools/measure_with_gpu.sh 801 RUN \
 ANYPS3_RENDER_SCALE=SCALE ANYPS3_RSX_HOST_PROFILE=1 ANYPS3_RSX_FALLBACK_STATS=1
```

**Verified bottleneck:** `local/ratchet/work/render-scale/pass-count-comparison.json`
shows ~3633 render passes/frame atscale2 versus~87 in native800a, with comparable
guest draw counts. Alternating each native draw with its visual companion closes
the pass and deferred MSAA planes. A recorder-owned visual queue now keeps up to
128 independent draws together and drains at resource/conditional/submission
boundaries. `ANYPS3_RENDER_SCALE_BATCH=0` provides an immediate-mode comparison.
The first independent GPU oracle (`batching-oracle.log`) passed all12 combinations
of recorder0/1, samples1/2/4 and batching0/1. For200 paired draws, passes fell
400->4 (or800->8,1600->16), preserving independent pixels and exact native queries.
This is synthetic evidence; game performance improvement remains unmeasured.

**Review follow-up:** timestamp nesting and throwing conditional cleanup were
identified and fixed. Expanded replay-failure, scale and recorder tests are in
progress in `batching-reviewed.log`; inspect actual process/log before continuing.
Full snapshot801 suite initially failed; original report is preserved. Separate
rechecks address missing local helper imports, GUI environment and a scheduling-
sensitive SPU assertion. The forthcoming reconciliation explicitly distinguishes
unit-test passes from an isolated SPU executable pass; do not claim the initial
suite was green. Current batching source is newer than snapshot801.

**Startup research:** original `-nopadintros` parser and bit0x20 write are proven,
but a startup-video consumer is not. Details and original instruction addresses
are in `local/ratchet/work/intro-direct-menu-research.md`. No argv or game-state
patch is included; the verified fast controller helper remains the default.

**Batching checkpoint802:** all three targeted methods passed in76.383s on
NVIDIA with validation (`batching-reviewed.log`), including both conditional
replay-failure paths. Review fixes rechecked. Immutable802 now building from801
plus rsx_vk_graphics.py,rsx_vk_conditional.py,vulkan.py and test_rsx_visual_batch.py;
driver session60644, build/compile logs under local/ratchet. First next action:
verify compile hashes, then quiet actual802scale2a. Suite801 reconciliation is
`suite-801-reconciled.json/.md`:923 unittest passes,7skips and1 isolated-native
fixture resolution,931cases total; original failed report preserved. It does
not cover802 or constitute one clean full-suite run.

**Build802 validation in progress:** full recursive suite runs in
`local/ratchet/work/render-scale/suite-802/`, session5173, scope
anyps3-scale-suite802 (24GiB,6workers, existing GUIvenv/offscreen). Runtime source
remains frozen until suite completion. Compile802 is concurrent in its28GiB
scope; a quiet game run must wait for both.

**Prepared measurement improvement, not integrated:** `flip-stage/` contains
ANYPS3_FRAMETIME_MODE=flips, retaining flip/vblank/status milestones while gating
scheduler/host-access clocks and wrappers early.13 standalone ring tests pass
in flip-stage-tests2.log; read-only review resscale-flip-filter-review.md has no
confirmed runtime bug. Version2 ring offset40 records0(full)/1(flips). The local
flip_acceptance.py reports explicit window coverage, missing events, tick-period
histogram and longest miss streak; CPU attribution is explicitly unavailable.
measure_level.sh now saves monotonic window bounds and selected PERF indices.
This staging source is outside802 and needs a fresh build before use.

**Build802 compiled:** rc0, source hashes match immutable snapshot; full suite
still running. New diagnostic build803 compiles in parallel with its remaining
regressions: snapshot803=802+the reviewed flips-only instrumentation from
flip-stage. It does not change the batching algorithm. Driver session34905,
root build-803.log/compile-803.log; source hashes/13 staged tests recorded in
flip-stage/PREPARED.json. Neither802 nor803 has a real-game run yet. The first
quiet measurement should use803 without FRAMETIME, followed by identical-source
flips-only runs if performance qualifies. The frozen worktree still equals802
and must not be changed until suite802 finishes. Integration/full final validation
of the instrumentation remains required before final handoff.

**Startup visual correction:** inspected706fastmenu1 frames13–31 show epilepsy
and autosave notices, Sony and Insomniac splash screens. The driver's early
~64draw/flip `title` label is a heuristic, not proof of the title. Actual PRESS
START appears in frames34/35; final START is11:28:35, menu visible inframe37,
confirmed11:28:39. No cinematic was visible in inspected early frames; the
`video` label does not prove FMV skipping. Substantial startup time is the
original loading/notices/splashes. This does not prove they are unskippable.
Exact capture hashes and PERF brackets are in local/ratchet/tools/fast_menu.md.

**User steering:** final scope also includes higher-resolution switching in the
GUI configurator, persisted per-game configuration and runtime argument/env
propagation. This is required before completion; GUI mapping task assigned to
intro_skip, initially read-only while suite802 remains frozen.

**First batching game diagnostic:**802scale2diag launched13:39:46 Europe/Prague,
QUIET=0,WARM=20,WIN=30,scale2,null/dummy audio, noFRAMETIME. Game usesCCD0;
compile803 and suite802 restricted toCCD1, compiler nice10. This is explicitly
not quiet acceptance; it provides early original-game/pass-count evidence.
Session77679, root measure-802scale2diag.log. Observe actualprocess before restart.

## Actual batching results802

**Verified diagnostic:**802scale2diag finished39.1566FPS over30.032s;
802scale2recdiag with ANYPS3_RSX_RECORDER=1 finished41.2260FPS. Both show the
original Great Clock at2560x1440, noSTOP, exact original PT_LOAD, deliberate
SIGKILL after measurement. Separate visual-acceptance.json files retain exact
frames/hashes. These were WARM20/WIN30,QUIET=0,gameCCD0 and compiler/testsCCD1;
not controlled quiet acceptance. Background scopes returned toCPUs0-23 after
the second run; compiler remains nice10.

Read-only normalized analysis resscale-cost-802.md/.json: last15intervals,
passes3637.62->157.96/frame, GPUbusy69.082->14.394ms, HostStage draw6.222->3.247ms,
GPUwait42.630->0.185ms. Both GPU14.394ms and RSXworker15.784ms are near the frame
budget; a CPU-only diagnosis is not established. The queue diagnostic counters
print zero because submit_async moves/zeros local stats before collection prints
them; pass counters independently prove batching. This reporting issue remains
unfixed in802/803 and must not be interpreted as inactive batching.

**Suite802 actual failure:** test_spu_putllc_lr.py native concurrent-write fixture
hit its five-second subprocess timeout. Preserve the original log and rerun that
specific unittest without competing jobs after compilation/suite finish; do not
claim a semantic regression or scheduling-only resolution before recheck. No
runtime changes are being made while suite802 is active.

**GUI scope:** intro_skip owns isolated feat/render-scale-gui worktree gui-tree,
based on render-scale HEAD33030a9, only manager/GUI configuration and focused
tests. Runtime/game profile remain main-owned. Existing integer1..4 supported;
no fractional runtime scale implemented. GUI tests may run in4GiB scope onCCD1
now that the game diagnostics ended. Integration/review still pending.

## Integration and quiet803 run

Full suite802 completed193modules/932cases in2416.625s,7skips,source drift[].
Only failure was the five-second SPU concurrent-write subprocess timeout.
An uncontended rerun of the exact unchanged unittest passed in12.163s
(putllc-recheck802.log, exit0). suite-802-reconciled.json records925passes+7skips
across the suite and recheck; original failed report remains preserved.

803 compilation completedrc0/source hashes match snapshot. Its reviewed trace
changes are now integrated into feat/render-scale, together with the independently
reviewed GUI patch. Integration log integration-803-gui.log:28testsPASS,1.908s,
no skips/errors. Tests include ring filtering and GUI/config/env plumbing.
GUI source: anyps3_manager/settings.py,anyps3_gui/settings.py; new focused tests
under tests/gui and tests/manager. GUI is in the rendering worktree, not installed
into the user's application release or promoted to root main. No commit/push.

Quiet803scale2quiet1 is live, session98183, QUIET1,WARM40,WIN60,scale2,recorder1,
noFRAMETIME,host/fallback profiles enabled,STRIDE600,null/dummy audio. All compiler,
suite and GUI test jobs finished before launch. Original menu reached13:55:42;
level detected13:56:03. Inspect actualprocess/log before any restart. Next: verify
quiet FPS and original frame, then choose next measured blocker or flip-only
acceptance runs. Stable60 and physical gamepad remain unverified.

## Quiet803 and image-quality investigation

Verified quiet build803 result: 45.5198 FPS over60.104s, min2s40.67,
main11.413ms/frame and RSX13.667ms/frame. Original Great Clock capture inspected,
PT_LOAD exact, no STOP; harness deliberately terminated the run (-9).
No game/build is live. GUI source integrated,28 combined targeted tests passed;
not installed or promoted. Suite802 reconciles to925 passes+7 skips after the
unchanged five-second concurrency fixture passed an uncontended rerun; original
red report is preserved. Higher-resolution stable60 remains unfinished.

Current blocker: image detail. General alias merge/equal-footprint rebind and
unsupported RTT rebuild visual companions from native pixels (verified source);
their contribution to final world quality is still a hypothesis. See
`local/ratchet/work/resscale-quality-loss.md`. A same-flip native/visual capture
diagnostic is being tested before a new build804; this is not a quality fix.

## Reusable startup and comparison diagnostics

The versioned startup helper is `scripts/ratchet_fast_menu.py`. For an already
running diagnostic virtual-pad build with `ANYPS3_PERF_REPORT=1`, run:

```bash
python scripts/ratchet_fast_menu.py local/ratchet/native-virtual-RUN --early-title
```

It exclusively owns that run's `control.txt`, sends START through the existing
SDL/cellPad path and releases held input on timeout, interruption or STOP.
Eight offline tests include independent START/neutral protocol expectations.
The `--early-title` name is historical:64-draw candidates include epilepsy and
autosave notices and logos. This is faster boot navigation; skipping the whole
intro or restoring directly into the menu has not been implemented.

`ANYPS3_CAPTURE_NATIVE_PAIR=1` saves `comparison-frame-NNNNNN.ppm` beside a
synchronous `frame-NNNNNN.ppm`: native pixels and visual pixels from the same
flip. It covers the first eight captures and `ANYPS3_FRAME_TRIGGER=<file>`
requests; asynchronous stride captures remain unchanged. Use a large
`ANYPS3_FRAME_STRIDE` and touch the trigger after level load. Paired capture
blocks for readback and is a diagnostic, not an FPS acceptance configuration.
The native buffer remains authoritative. Two Vulkan regression tests passed,
including exact per-flip colors and dimensions for both files.
