# HIRES wave: true 1440p at a stable 60 FPS — 2026-10-25 (Claude Opus 5.5 coordinator, Opus 5.5 high agents)

User goal: rock-solid 60 FPS at 1440p (`ANYPS3_RENDER_SCALE=2`) without stutter. The user also reported that the
1440p image looks like a stretched 720p window. Agent rules and the baseline table:
`local/ratchet/work/COMMON-HIRES.md`; agent results: `local/ratchet/work/hires{A,B,C}/RESULT.md`.

## Baseline, verified (build 882 = main 31d50d8, QUIET, original Great Clock, timed virtual-pad walk)

| run | scale | window FPS | 1-vblank % (walk) | worst frame | main_ppu / main_idle | rsx_worker / flip_wait / gpu_wait ms |
| --- | --- | --- | --- | --- | --- | --- |
| 882q1 | 2 | 33.15 | 41.2 | 413 ms | 11.5 / 17.8 | 16.1 / 7.3 / 3.2 |
| 882q1s1 | 1 | 44.68 | 72.1 | 138 ms | 10.2 / 11.5 | 10.6 / 9.6 / 0.5 |

Command: `QUIET=1 bash local/ratchet/measure-walk-882.sh RUN [ANYPS3_RENDER_SCALE=1]`; report
`python local/ratchet/report-walking.py RUN` (from the project root).

**Verified, full frame ring 882d1s1 (scale 1):** frames that miss a vblank process ~1800 draws (p50 2125) with the RSX
worker busy 20.8 ms; frames that make it ~570 draws / 7.3 ms → ~11.5 µs per draw. The main guest thread is blocked
~43 % of the window at lr 005cb798 / pc 005c5730 (waiting for RSX). **Derived:** the RSX worker's throughput is the
critical path at both scales; walking is not at 60 FPS even at 720p.

**Verified, RSX-thread profile with call stacks** (DIAGNOSTIC build 884 = 882 source + `-fno-omit-frame-pointer` for
program.cpp; run 884p1, 34.6 FPS; session `local/ratchet/sample-884p1`; tool `local/ratchet/tools/sample_callers.py`):
rsx_execute_draw 51 % inclusive, rsxgraphics::prepared 34 %, graphics_draw 8.3 %, Session::bind 7 %, texture_metadata
4.9 %, texture_image 4.3 %, retire 4 %, leave_fifo 3.8 %, load_fragment_program_shared 3.6 %, rebind_merge 3.4 %,
publish_labels 3.4 %, graphics_pipeline 3.2 %; leaves: guest-memory reads 16 %, mprotect/HostObserver/HostGuard 6 %,
NVIDIA driver 19 % (12 % in a second rsx-role thread).

**Verified, image quality:** a 1:1 crop of `native-virtual-882q1/frames/frame-000017.ppm` (2560×1440) shows 2-pixel
staircases and a one-pixel zig-zag comb along 3D edges, i.e. the presented scene is derived from native-resolution
pixels. PERF RENDER_SCALE 882q1: native_copies 46 640 (visual rebuilt from native pixels, ~8 per frame),
history_copies 19 949. A half-down/bilinear-up residual test is not sufficient evidence here (the comb artifact adds
fine-scale energy); inspect 1:1 crops.

## Work split (branches based on main 31d50d8)

- HIRES-A `hires/visual` (builds 900–919): eliminate per-frame visual reconstructions from native pixels, fix the comb;
  evaluate making the visual image authoritative (removes the double recording at scale 2).
- HIRES-B `hires/drawcpu` (920–939): RSX per-draw CPU cost (prepared/bind/texture metadata/fragment program/guest reads).
- HIRES-C `hires/vulkan` (940–959): Vulkan/driver cost, submission, 100–400 ms stutter frames.

## Results at session end (18:30) — code done, nothing measured in the game yet

All three branches are committed (agent handoffs with full detail: `local/ratchet/work/hires{A,B,C}/RESULT.md`) and
merged without conflicts into `hires/integ` (e1e61b6). Targeted native Vulkan tests of the merged tree pass on NVIDIA:
test_rsx_render_scale, test_rsx_vk_submit, test_rsx_fp_cache, test_rsxvk22, test_rsx_visual_history, test_rsx_gpu_upload
(6 modules, 336.8 s, `local/ratchet/work/hiresI/logs/test-integ-1.log`). Full suite not run.

**HIRES-A (`hires/visual` 5f0d0bf, 384fb8c) — cause of the 720p look / zig-zag comb.** Verified from pixels/trace,
mechanism derived from source: the game renders the scene at 960×704 with 2× MSAA (sample grid 1920×704) and resolves it
by reading the grid as ARGB8 (`RENDER_SCALE_RTT texture=1920x704 fmt=0x85 surface=960x704 samples=2`). The typed RTT
conversion, the 1↔2 sample-grid rebind shader and the history planner expanded each *native* sample cell into an S×S
block of one plane, so the game's resolve averaged the same sample of neighbouring pixels: half horizontal resolution and
alternating columns from diagonally offset sample planes. Fix: the visual image of an MSAA surface is that surface rendered
at S× (grid column G = X·nx + sample); native formulas unchanged (identical at scale 1). Sample-count-changing
merges/rebinds now rebuild from native pixels (exact; previously wrong block order). 384fb8c keeps shadow maps (swizzled
D16 32–1024 px) and B8 RTT reads at full resolution. Per-reason counters `PERF RENDER_SCALE_SYNC reason=… count= pixels=`
(always on, rare path) and opt-in `ANYPS3_RENDER_SCALE_TRACE=1` lineage. Diagnostic run 900d1 (stationary, not
comparable): per-frame rebuilds rebind_merge/upload 5.0 (bloom pyramid 240×176/128×96/64×48), rtt/draw_unscaled 3.5
(shadow maps, 960×704 B8, 16×16 BC1). Open: sample-count-changing equal rebinds (480×352×4 ↔ 960×704×1), BC1 from the
display buffer, bloom merge cost. Design note on "visual authoritative": a GPU downsample cannot reproduce native pixels
exactly (coverage, sample positions, occlusion counts); see hiresA/RESULT.md.

**HIRES-B (`hires/drawcpu` 9daec99, 7375cb6, e9e750c, 7f716ab).** FP structure reuse on constant-only rewrites,
per-unit texture-metadata memo, faster source-key hash / resident lookup skip, single bucket scan. Profile estimate
0.5–0.8 ms/frame at scale 1 (not measured). Build 921 (first three commits) built; A/B runs 921a1s1/882hb1s1/… queued by
`work/hiresB/ab-921.sh`. Next per B: incremental `HostObserver::watch` (draft patch, unverified), cheaper report
publication (~230 occlusion reports/frame), fewer shared_ptr copies into graphics_draw. HOST_PROFILE timers cost ~5 % of
the RSX worker themselves.

**HIRES-C (`hires/vulkan` 792502e, 9de34d4, 5186562, c6255f7).** Visual half reuses the native blob slice (~11 MB/frame
less upload at scale 2); canonical shader owners (pipeline hits compared whole GLSL texts, ~1.5 % of the worker);
device-wide weak framebuffer cache (was ~30/56 framebuffer creations per frame at scale 1/2); `ANYPS3_RSX_FLIGHTS`
(default 2, A/B only); opt-in driver-call spans in the full frame ring. Stutter classes (from existing artifacts):
(1) 85–140 ms guest-side PPU stalls with an idle RSX worker; (2) texture/surface streaming on the worker (ioctl, memcpy,
munmap, decode_texture, watches); (3) cold first-use vkCreateGraphicsPipelines (1038 ms once). Builds: 940 diagnostic
(commits 1–2), 941 decision build (full rebuild; any NativeVulkan change touches anyps3.hpp → ~21 min). Diagnostic run
940d1 queued; analyse with `local/ratchet/work/hiresC/hitch_attrib.py 940d1`.

**Integration build 960** (`hires/integ`, all three) queued detached at 18:30 by `local/ratchet/work/hiresI/build-960.sh`
(quiet_wait → compile lock → snapshot_build 960 → QUIET scale-2 walk `960q1` with `ANYPS3_RENDER_SCALE_TRACE=1` →
`local/ratchet/walking-960q1.json`). Logs: `work/hiresI/build-960.out`, `run-960q1.out`, `compile-960.log`.

**First next actions:** (1) verify 941/921 A/B and 960q1 numbers (compare 882q1 33.15 FPS / rsx_worker 16.1 ms,
882q1s1 44.68 / 10.6) and inspect 1:1 crops of 960q1 frames for the comb (`work/hiresA/crop.py`; same tile edge as
882q1 frame 17); (2) repeat QUIET runs alternating with 882; (3) the 60-FPS gap remains large: the RSX worker needs
~2× less CPU per draw — next structural candidates: HostObserver incremental watch, report publication, per-draw
shared_ptr/array churn, cold pipeline pre-creation, texture streaming off the worker.

## Integration build 960 measured (19:07)

**Verified:** build 960 (`hires/integ` e1e61b6; compile rc=0, source hashes match the snapshot), run 960q1 (QUIET,
scale 2, walking with the long-deadline walker, `ANYPS3_RENDER_SCALE_TRACE=1`): window **35.77 FPS** (median 30.5, min
23.5), entire walk 34.95 FPS, 41.4 % one-vblank, worst frame **185 ms**; ms/frame main_ppu 10.9 / main_idle 16.2,
rsx_worker **12.9** / flip_wait 8.1 / gpu_wait 3.4. Compared with 882q1 (33.15 FPS, worst 413 ms, rsx_worker 16.1): the
worker is 3.2 ms/frame cheaper; single run, noise ±1.5 FPS, repeat before deciding. No STOP, deliberate -9, both
original PT_LOAD segments exact. Run totals: RENDER_SCALE_SYNC rebind_merge/upload 15.6 k, rebind_equal/upload 12.3 k,
rtt 16×16 BC1 2.5 k, bind_fresh 51.
**Derived (one frame):** a 1:1 crop of frame-000017 (camera near a wall, different view than 882q1 frame 17) shows a
long diagonal gold edge without the zig-zag comb or 2-px staircase. A same-view comparison is still to do.
The goal (stable 60 FPS at 1440p, no stutter) remains **unmet**: the RSX worker still needs ~4 ms/frame less and
1-vblank frames are only 41 %.

**Queued at 19:12 (detached):** `local/ratchet/work/hiresI/ab-960.sh` runs alternating QUIET scale-2 walks 882q2, 960q2, 882q3, 960q3 (long-deadline walker) behind the still-running HIRES-B A/B `work/hiresB/ab-921.sh`; results in `local/ratchet/walking-RUN.json`, logs `work/hiresI/run-RUN.out`. Evaluate these first in the next session.

## Alternating A/B evaluated (2026-10-27, coordinator)

**Verified** (`local/ratchet/walking-RUN.json`, QUIET, scale 2 walking, long-deadline walker): 882q2 34.46 / 882q3 34.72
FPS (rsx_worker 14.5 / 14.3 ms) vs 960q2 35.75 / 960q3 37.06 (12.8 / 11.8 ms); with 960q1 35.77 the integration gains
~+2 FPS and ~2 ms worker time. gpu_wait rises 2.7 → 3.3–3.6 ms. HIRES-B A/B at scale 1: 921a2s1 43.10 vs 882hb1s1 /
882hb2s1 42.24 / 42.89 (worker 11.6 vs 12.2 / 11.9); scale 2: 921a1s2 36.54 vs 882hb1s2 36.12. **921a1s1 is invalid as a
walking result**: `entire_walk` is null — the walker did not move, and that stationary run held 59.93 FPS, 100 % one-vblank
(main_ppu 10.9, rsx_worker 10.7 ms). **Derived:** standing still is at 60 at scale 1; walking misses because heavy frames
(~1800–2100 draws) exceed the vblank on the RSX worker. Goal still unmet; HIRES2 wave started with two agents
(rules `local/ratchet/work/COMMON-HIRES2.md`: W = worker throughput builds 1000–1029, S = scale-2 overhead / GPU / image
builds 1030–1059).

## HIRES2 interim (2026-10-27 ~19:40, coordinator; details in `local/ratchet/work/hires2{w,s}/RESULT.md`)

- **Verified (HIRES2-S):** at scale 2 the GPU is busy ~19.3 ms per walking frame on 960 (scale 1 ~7.5 ms), so the GPU
  alone caps scale 2 below 60. Build 1031 (= 960 + visual typeless aspect by a draw 9912280 + static fragment discards)
  walked at 39.91 FPS with GPU 15.52 ms/frame (run 1031a1, single run; alternating repeats queued). Draws ~7.1 ms,
  non-draw GPU work (GENERAL layouts, LOAD/STORE passes, ~250 full barriers/frame, conversions) ~8 ms.
- **Verified (HIRES2-S, parity check over whole frames):** no comb and no block replication at scale 2. **Derived:** the
  2560×1440 output is the game's 960×704 scene rendered at 1920×1408 and resampled 4:3 horizontally (as the original
  resamples 960→1280); full 2560-column detail would need a horizontal scale of 8/3.
- **Verified (HIRES2-W, full ring 960wf2s1):** heavy walking views (~1800–2400 draws) keep the RSX worker busy
  ~20–22 ms at scale 1 (≈2.9 ms + 9.1 µs/draw) and the main guest thread runs ~14.8 ms inside the same work span — both
  are critical. HOST_PROFILE overhead ~3 %; SMT placement and recorder spin refuted as levers. d6eeb7e (incremental
  HostObserver bitmaps, exact, tested) is in build 1001, A/B in progress.

## HIRES2 wave result (2026-10-27 evening, coordinator Claude Opus 5.5, agents Opus 5.5 high; goal NOT reached)

Branch `claude/hires2-wave` = main f92681c + `fast2/hires2w` (HIRES2-W, 2 instances) + `fast2/hires2s` (HIRES2-S,
5 instances; each instance started fresh from its predecessor's RESULT.md at ~300k context). Full evidence and commands:
`local/ratchet/work/hires2w/RESULT.md`, `local/ratchet/work/hires2s/RESULT.md`. A power outage at 16:53 invalidated runs
960wf1s1 and 960sm1 (`work/hires2{w,s}/INTERRUPTED.txt`).

**Verified, decision-grade (QUIET, alternating, scale 2 walking, walker walked, no STOP, PT_LOAD exact):**

| build | content | runs | FPS | GPU busy ms/frame |
| --- | --- | --- | --- | --- |
| 960 | main f92681c | 960sa1 / 960sa2 | 37.27 / 35.89 | 18.64 / 19.29 |
| 1031 | 960 + 9912280 (visual typeless aspect by a draw) + static fragment discards | 1031a1 / 1031a2 | 39.91 / 40.58 | 15.52 / 15.32 |

1001 (960 + d6eeb7e incremental HostObserver bitmaps) is neutral at both scales (scale 1: 45.46 / 44.56 vs 43.69 / 44.65;
scale 2: 37.09 / 37.36 vs 37.61 / 37.28; µs/draw unchanged).

**Verified diagnostics:**
- GPU split at scale 2 (1031sm1, 15.65 ms): visual draws 7.26 (1692/frame), native draws 2.80, native render-pass begins
  1.02 (12.6 µs each vs 1.55 µs visual, unexplained), copies ~2.3, ~250 barriers ~0.6. The second visual sample plane
  costs 2.3 ms (DIAGNOSTIC 1032d1 plane-0-only 13.17 ms / 41.38 FPS vs 1032d0 15.49 / 38.65).
- Multiview / layered visual planes do not help (960sx1 20.83 ms, 960sx2 19.39 ms GPU).
- Image: no comb and no block replication over whole frames at scale 2 (`work/hires2s/tools/parity.py`); the 2560×1440
  output is the 960×704 scene rendered at 1920×1408 and resampled 4:3 horizontally (full 2560-column detail would need a
  horizontal scale of 8/3 — a user decision).
- CPU critical path at scale 1 (full ring 960wf2s1): heavy views (~1800–2400 draws) keep the RSX worker busy ~20–22 ms
  (≈2.9 ms + 9.1 µs/draw, flat profile, no item > 7 %) while the main guest thread runs ~14.8 ms in the same work span;
  the guest never runs ahead past the flip. Main guest thread profile (1000p1): ~26 % spin-waiting for SPU jobs
  (delay 0x666068 17.5 %, job flag 0x5564d4 5.6 %, engine counter 0x696c10 3.0 %) ≈ 3.9 ms/heavy frame (derived from
  sample shares); 57 % translated page code; dispatcher ~6.5 %; no observer faults/mprotect.
- Refuted: HOST_PROFILE distortion (~3 %), SMT/CPU placement, recorder spin, discards blocking early-Z as a big lever,
  pipeline compiles as the cause of the 140–180 ms walking hitches (46 ms of pipeline creation in all of 1031a2).

**Merged but NOT measured in the game** (each exact or gated; targeted Vulkan tests pass, see RESULT.md):
fast RGBA8 visual half of A8R8G8B8 typed-RTT reads (b1f45eb + fix 737da5f, default on; build 1034), graphics barriers
without HOST bits (2b61b53, default; `ANYPS3_RSX_BARRIER_HOST=1` restores), lazy visibility queries (default off),
window-clip drop e9e5110 (build 1033), visual 2× MSAA working images (a57960b, `ANYPS3_RENDER_SCALE_MSAA=1`, default off;
NVIDIA test bit-identical colour/stencil vs plane-by-plane), native switch keeps visual halves queued (782abef, default;
`ANYPS3_RENDER_SCALE_SWITCH_REPLAY=1` restores), unrolled GuestMemory atomic copy (cf7a6ef, build 1002).

**Late runs before the queue was stopped (verified, QUIET, walked, no STOP, PT_LOAD exact; single pairs — not decisions):**

| run | build | scale | FPS | rsx_worker / main_ppu / gpu_wait ms | draws/frame |
| --- | --- | --- | --- | --- | --- |
| 1034a1 / 1034a2 | 1034 = 1033 + fast RGBA8 visual RTT + barriers without HOST + lazy-visibility (off) | 2 | 38.19 / 40.43 | 13.20 / 11.20 / 1.53 · 11.62 / 10.50 / 1.63 | 1082 / 960 |
| 1031c1 | 1031 | 2 | 40.14 | 11.55 / 10.50 / 2.01 | 953 |
| 1002a1s1 | 1002 = 1001 + cf7a6ef unrolled atomic copy | 1 | 41.53 | 13.73 / 13.78 / 0.35 | 950 |
| 960a3s1 | 960 | 1 | 46.72 | 9.74 / 10.19 / 0.58 | 833 |

1034 is about level with 1031 (gpu_wait ~0.4 ms lower). **Open, must be resolved before merging to main:** 1002a1s1 is
much slower than 960a3s1 on both threads; it overlapped a CPU-heavy `wallsplit.py` analysis (unpinned Python) and is
probably disturbed, but a regression from cf7a6ef is not excluded — repeat 1002 vs 960 alternating at scale 1.
The queue was stopped at 21:18 on the user's instruction: 960a4s1 and 1031c2 are cut short (INVALID); 1034e1/l1/n1 and all
1035 runs (visual 2× MSAA on/off, switch replay) never started. Builds 1034 and 1035 are compiled (rc=0, source hashes
match) and can be measured directly with `work/hires2s/measure-walk-103{4,5}.sh`.

**Merged-tree tests (verified, branch `claude/hires2-wave` at ae9e1bd + docs):** NVIDIA — test_atomic_copy_unrolled,
test_host_observer_incremental, test_rsx_visual_msaa, test_rsx_render_scale, test_rsx_pipeline, test_rsx_graphics,
test_rsx_visual_history, test_rsx_vk_submit: OK (187.7 s); lavapipe — test_rsx_render_scale, test_rsx_visual_history,
test_rsx_pipeline, test_rsx_visual_msaa: OK (106.0 s). Logs/script `local/ratchet/work/hires2-merge/`. Full suite not run.
