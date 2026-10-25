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
