# Cloud follow-up: visual history and scaled-path cost, 2026-10-23

**Scope:** source-level work on the resolution-scaling gaps left by the
[render-scale milestone](2026-10-23-render-scale.md), done in a Claude Code
cloud container (branch `claude/jolly-pasteur-z3etd7`, base `8c66615`). The
container has **no GPU, no game data and no `local/` tree**: no original-game
build, run, FPS figure or frame capture was produced. Every result below is a
native Vulkan unit test on **lavapipe (llvmpipe, Mesa 25.2.8)** with
`VK_LAYER_KHRONOS_validation` and sync validation. NVIDIA/RTX and real-game
verification remain open.

## Test environment (cloud container only)

- Installed for the run: `mesa-vulkan-drivers`, `vulkan-validationlayers`,
  `glslang-dev`, `libvulkan-dev`, `libsdl2-dev`, `spirv-tools`.
- The container compiler is GCC 13.3, which lacks `[[gnu::musttail]]` (GCC 15).
  Tests used a scratch `c++` shim that adds `-Wno-attributes
  -foptimize-sibling-calls`, and `-lSPIRV-Tools-opt -lSPIRV-Tools` (Ubuntu's
  static glslang needs them). The shim was outside the repository; no
  repository build flag changed. With GCC 13 the dispatch tail calls are
  optimizer sibling calls, not guaranteed `musttail`; the tiny test programs do
  not depend on that.
- Audio forced off: `SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`.

## 1. Visual history through alias merge and equal-footprint rebind

**Verified source mechanism (before):** `Session::rebind_merge`
(`anyps3/rsx_graphics.py`) and `rebind_equal_footprint`
(`anyps3/rsx_graphics_rebind.py`) pack the old native image, permute bytes and
unpack into the new native image; `graphics_unpack_upload` then nearest-blits
the whole native image into the new visual companion. Every subpixel of the
old companion was discarded, the leading hypothesis of
[quality-loss.md](../evidence/render-scale-2026-10-23/quality-loss.md).

**Implemented:** `anyps3/rsx_graphics_visual_history.py` (Session members) plans,
from the same physical-byte intersections the native merge copies, which
destination texels receive **all** of their bytes from **one whole** source
texel (same guest format and bpp in {4, 8, 16}, 4-byte aligned words,
contiguous words for 8/16-byte texels). Texel coordinates use the pack/unpack
shaders' layout (sample planes interleaved 2x1/2x2 in guest rows). The planner
walks segments where both word tables are logically contiguous inside one
guest row. Each segment emits one texel run, or one per sample phase when both
sides interleave 2x with equal pixel parity; otherwise it emits per-texel
runs. Runs are sorted and merged vertically into `VkImageCopy` regions in
native units. `NativeVulkan::graphics_visual_history`
(`anyps3/rsx_vk_graphics.py`) scales them by the render scale and copies the
subpixel blocks between the paired companions (colour, or depth and stencil
aspects separately). Plans are cached per (old readback plan, new readback
plan, layouts, pair hash) through weak pointers, LRU 64. A plan with more than
65536 regions is refused.

**Measured host cost (container, contended CPU, standalone copy of the loop,
1280x720 identity):** a first dense per-texel version took ~17–18 ms per cache
miss (720/1440 regions). The run-based planner takes ~0.9–1.2 ms (1 region for
1 sample, 2 for 2 samples). A miss costs O(words) compares. Merge remainders
get a fresh `clipped_plan` pointer, so a merge whose old owner is a remainder
always misses (**derived, unmeasured in the game**). Per-frame misses on
720p-sized remainders would cost about 1 ms each on the RSX worker.
`ANYPS3_RENDER_SCALE_HISTORY=0` disables the feature for an A/B measurement
(default 1, validated 0/1).

- Native bytes, guest readback and queries are unchanged: the native
  pack/merge/unpack path is untouched.
- Unmapped texels (guest background, partial texels, other formats, clean
  owners) keep the native reconstruction: the whole-child sync still runs first
  and the history copies overwrite only mapped cells.
- When the plan maps **every** destination texel (typical equal-footprint
  rebind, or a merge fully covered by dirty owners), the whole-child native
  sync is skipped (`graphics_unpack_upload(..., sync_visual=false)`). One
  fewer full-resolution blit per such transition.
- Only **dirty** old owners contribute, matching the native merge's own copy
  set. A clean owner's guest bytes equal its native image, so its companion
  could contribute too, but that was not proven here; it stays
  native-reconstructed.

**Test (verified):** `tests/test_rsx_visual_history.py`, independent oracle
from guest byte addresses (offset, pitch, sample interleave), not from the
runtime's word tables. Each old companion holds subpixels absent from its
native image (alpha 254 vs 255) and different from their neighbours.

| case | old -> new layout | covered | result |
| --- | --- | --- | --- |
| equal-footprint | 16x4 pitch 64 -> 8x8 pitch 32, roundtrip A->B->A->B | 64/64 | every subpixel back, sync skipped, plan cache hit |
| equal-footprint-msaa2 | 8x4 s2 pitch 64 -> 4x8 s2 pitch 32, roundtrip | 64/64 | as above, planes permuted |
| merge-rows | 8x8 pitch 32 -> same, +128 bytes | 32/64 | rows 0-3 keep old subpixels, rest native reconstruction |
| merge-columns | 8x8 pitch 64 -> same, +16 bytes | 32/64 | in-row partial overlap; padding columns from guest background |
| merge-msaa4 | 8x4 s4 pitch 64 -> same, +64 bytes | 112/128 | half-row shift permutes sample planes |
| merge-msaa2-odd | 8x8 s2 pitch 64 -> same, +4 bytes | 127/128 | phase mismatch and row wrap (per-texel runs) |
| merge-samples-1to2 | 16x8 s1 pitch 64 -> 8x8 s2, +64 bytes | 112/128 | different sample counts (per-texel runs) |

Mutation checks (logs in the cloud scratchpad only): disabling the planner fails
the path/counter assertion; shifting the scaled source offset by one subpixel
fails the pixel oracle (`equal-footprint: visual history mismatch`).

**Not established:** whether these transitions are what the final Great Clock
image depends on. That needs the same-flip native/visual capture
(`ANYPS3_CAPTURE_NATIVE_PAIR=1`) on a real run. `PERF RENDER_SCALE` now
reports `history_copies` and `history_texels`; compare `native_copies` with
build 805's ~19.5/frame.

## 2. Typeless depth sampled as ARGB8 keeps subpixels

**Verified source mechanism (before):** `rtt_image`
(`anyps3/rsx_graphics_rtt.py`) excluded `typeless_depth` (D24S8 surface read as
texture format 0x85) from `visual_typed`. `graphics_aspect_image` rebuilt the
R32_UINT/R8_UINT word images' companions from native pixels. Post-processing
that reads depth this way therefore saw native-resolution depth.

**Implemented:** when one word per texel is produced (depth -> R32_UINT,
stencil -> R8_UINT, R32F), `graphics_aspect_image` also reinterprets the
source's visual companion through the same device-local transfer buffer, at a
16-byte-aligned offset after the native bytes, into the result's companion. It
does not call the native sync. Formats with several words per texel (RGBA16F,
RGBA32F) still rebuild: their word images interleave words horizontally, so a
scaled copy is not their companion layout. `typeless_depth` with both word
companions now uses the existing `visual_typed` conversion (its texel fetches
for depth and stencil go through `visual_xy`).

**Test (verified):** `typeless_depth(1)` and `typeless_depth(2)` in the same
file: the converted texture's visual companion decodes, per subpixel, the
depth/stencil words uploaded into the D24S8 surface's companion; expected ARGB
bytes follow the existing 1x test's byte order. Mutation (force
`visual_depth=false`) fails with `typeless depth visual mismatch`.

## 3. `PERF RENDER_SCALE` queue/drain counters

**Verified bug:** `graphics_move_stats` moves a batch's `visual_queued`,
`visual_drains` and `visual_run_max` to the next batch on `submit_async`,
before the original batch is collected and printed, so the report showed 0.
**Fix:** collection folds the batch's counters into run totals
(`graphics_visual_queued_total`, `graphics_visual_drains_total`,
`graphics_visual_run_max`) and zeroes them. Each count is reported once, by
whichever batch holds it when collected. `tests/test_rsx_visual_batch.py` now
asserts the totals after collection: 200 queued, 2 drains, max run 128.

## 4. Scaled draw: one pipeline lookup per pair

**Derived:** the graphics pipeline key (`graphics_pipeline`) contains no
`visual_pass` or raster-extent field, and a visual pair has the attachments'
layer count, so its pipeline is the native one. The native draw now hands its
pipeline to its visual half (`graphics_paired_pipeline`, reset by a scope
guard). The batching test asserts 200 lookups for 200 paired draws (400
before). **The saving is unmeasured:** it removes two shader-identity map
lookups and one fast-key map lookup per visual draw on the RSX worker. It is
not a demonstrated FPS change.

**Considered and rejected:** sharing the native draw blob with the visual half
(which would skip a second staging copy) would merge the two shader failure
words. A native failure would then be reported as `RSX visual shader failed`.
Both are fatal today, but the attribution would be wrong, so this is not done
without a separate status slot.

## Stable 60 FPS at scale 2: state

No change in this record is measured on the game. The last quiet evidence is
still run 803: 45.52 FPS at 2560x1440 with the recorder, GPU busy ~14.4 ms and
RSX worker ~13.7–15.8 ms/frame. The first next action on the real PC: build
from this branch (new number), run `measure_with_gpu.sh` with `QUIET=1`,
scale 2, recorder 1, host/fallback profiles. Compare `native_copies`,
`history_*` and pass counts with 803/805, and capture a same-flip native/visual
pair. Then decide from GPU marks whether the GPU or the RSX worker is the
binding cost.

## Regression scope run here

Final source (commit on this branch), lavapipe + validation, compiler shim as
above, one module per process: **24 of 25 RSX/Vulkan modules pass** (35
cases, 1 skip in `test_rsx_scalar_fp`): visual_history (9 cases incl. two
typeless-depth), visual_batch, render_scale, drain_bound (13), rtt,
alias_flow, subword_alias, graphics, gpu_batch, gpu_status, gpu_upload,
async_upload (4), graphics_cache, graphics_costs, parity, recorder, residency,
scalar_fp, unused_coords, rsxfast_draw, rsxvk21, rsxvk22, rsxvk22_cpu370 and
par4_rsx.

**One environmental failure:** `test_rsx_pipeline_cache` `save_retry` reports
"the read-only background save was never collected as failed". The test makes
the cache directory owner-read-only and expects the save to fail; the container
runs as uid 0, which bypasses directory permissions. It exercises pipeline-cache
persistence, which this change does not touch. Rerun it as a normal user on
the real PC.

**Not run:** the complete 930-case suite, `tests/manager`, `tests/gui` (no
changes there) and any NVIDIA run. The last full-suite reconciliation is
still suite 802.
