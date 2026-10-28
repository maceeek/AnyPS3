# Cloud round HIRES3: device-memory pool, per-draw CPU cuts — 2026-10-28

**Author/model:** Claude (cloud container), branch `ccr-8139488c-79xg7b` based on main 95756e2 (= `claude/hires2-wave`
merged). **Scope:** the cloud-instance list in [HANDOFF](../HANDOFF.md#cloud-instance-next-steps-no-game-data-no-nvidia-gpu-lavapipe--validation-only--2026-10-27).
The container has **no game data, no `local/` tree and no NVIDIA GPU** (4 vCPU, 15 GB): no game build, run, FPS or
frame figure was produced. Every runtime statement below is a native unit test on **lavapipe (llvmpipe, LLVM 20.1.2)**,
mostly with `VK_LAYER_KHRONOS_validation` + sync validation, or a synthetic micro-benchmark. The real PC must build and
measure (alternating QUIET runs) before any FPS or stutter claim.

## Test environment (cloud only)

`apt-get install mesa-vulkan-drivers vulkan-validationlayers glslang-dev glslang-tools libvulkan-dev libsdl2-dev
spirv-tools vulkan-tools valgrind linux-tools-generic`. GCC 13.3 lacks `[[gnu::musttail]]`: a scratch `c++` shim (outside
the repository) adds `-Wno-attributes -foptimize-sibling-calls -lSPIRV-Tools-opt -lSPIRV-Tools`, as in the 2026-10-23 cloud
round. Audio forced off (`SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`). Suite runner: one process per test module
(`python -m unittest -v tests.MODULE`), `ANYPS3_REQUIRE_VULKAN=1`, lavapipe, `LP_NUM_THREADS=2`.

## Changes (each commit has its own test; all exact or gated)

| commit | change | switch (A/B) | test (independent oracle) |
| --- | --- | --- | --- |
| aa3a872 | **Device-memory pool**: device-local allocations of at most half a block become best-fit ranges of 256 MiB driver blocks, coalesced on free; empty blocks retained up to `ANYPS3_RSX_MEMORY_CACHE_MB` | `ANYPS3_RSX_MEMORY_POOL=0` (exact-size recycler alone), `ANYPS3_RSX_MEMORY_POOL_MB` (block size) | `test_rsx_vk_memory_pool` (new), `test_rsx2_draw` direct reuse updated |
| 82734f7 | `DrawJob` borrows the batch-pinned images/buffers instead of 41 owning `shared_ptr` copies per draw half | — (exact lifetime argument, below) | `test_rsx_recorder`, render-scale/visual tests in the suite |
| fed12fd | RESET_REPORT / ZPASS reports scan only the draws since the last reset (report floors) instead of every pending draw of every flight | — (exact) | `test_rsx_deferred_labels` `reports_across_flights_` (new) |
| 5f799f8 | Vertex-constant refresh skips unchanged 128-word chunks by write version (`RsxShaderArray` chunk versions + identity) | — (exact; borrowed banks keep the value compare) | `test_rsx_constant_chunks` (new) |
| 2b4532d | Typed-RTT generic decoder: addressing words w[1..6] become shader literals (div/mod by constants) | `ANYPS3_RSX_RTT_LITERALS=0` | `test_rsx_rtt` on/off identical, wrong literal fails |
| 2b4532d | `bench/rsx_fifo_bench.py`: FIFO decode cost per packet, serial and as a renderer thread on concurrent memory + page broker | — | diagnostic only |


### 1. Device-memory pool (HIRES2 stutter class A, possibly B)

**Verified source mechanism (before):** `rsx_vk_resources.py` recycled device-local blocks by exact (type, size) only, 384 MB
total. At 1440p the visual images are 64–86 MB, so the recycler held ~4 of them, frees evicted others through
`vkFreeMemory` (single calls up to 16.8 ms, HIRES2-S instance 5) and ~240 new allocations per streaming frame missed.

**Implemented (`rsx_vk_resources.py`, all inside the existing static `NativeVulkan` wrappers, so no call site changed):**
- `vkGetImageMemoryRequirements`/`vkGetBufferMemoryRequirements` wrappers record the result per thread; the next
  `vkAllocateMemory` of the same size on that thread knows the alignment. Only such allocations are pooled (everything else
  takes the old path). Offsets are aligned to max(alignment, `bufferImageGranularity`), sizes rounded to the granularity.
- A pool range is returned as a pool handle (address of its `Sub` record). The `vkBindImageMemory`/`vkBindBufferMemory`/
  `vkMapMemory`/`vkUnmapMemory`/`vkFreeMemory` wrappers translate it to (block, offset); flush/invalidate of a pool handle
  is an explicit stop (pooled host-visible memory is always HOST_COHERENT). A driver handle equal to a live pool handle is an
  explicit stop. Every bind re-reads the object's requirements and stops if the range cannot hold it.
- Free = return to the block's offset-ordered free map with coalescing (no driver call). Empty blocks beyond
  max(`ANYPS3_RSX_MEMORY_CACHE_MB`, one block) are really freed; out-of-memory releases cached and empty blocks and retries.
- Lifetime rule unchanged: a range is freed exactly where the old code freed the whole allocation (after the GPU finished
  with it), so a reused range never overlaps live GPU work.
- Counters: `vk_pool_ranges`, `vk_pool_blocks` (appended to `PERF RSX_COUNTERS`); `vk_memory_allocs` counts real driver
  allocations (blocks included). `NativeVulkan::memory_pool_report(device)` checks the structure (test hook/diagnostic).
- Test hook `ANYPS3_RSX_MEMORY_POOL_TYPES=coherent` also pools HOST_COHERENT types: lavapipe has no device-local-only type.

**Verified (lavapipe + validation):** `test_rsx_vk_memory_pool`: three rounds of 17 surfaces (1 KiB–256 KiB pooled with
1 MiB test blocks, one 1 MiB surface on the exact path), interleaved releases and refills while others stay alive; every
surface's committed guest bytes equal its own pattern; pool on/off guest memory byte-identical; 64 ranges from **7 driver
allocations vs 68** without the pool; after the session dies all ranges are back and ≤ 1 empty block is retained.
Mutations (overlapping split, no coalescing, bind without the range offset) fail. With the hook forcing **all** lavapipe
memory into the pool (8 MiB blocks), 12 heavy modules pass: render_scale, visual_history, pipeline, graphics, vk_submit,
recorder, rsx2_draw, visual_msaa, vk_resident, rtt, alias_flow, memory_pool.

**Derived (unmeasured):** at 1440p the pool removes the driver alloc/free calls from streaming frames; HIRES2's class-A
~55 ms of vkAllocate/vkFree per hitch should disappear. Class B (every GPU op ~10× slower with ~240 new allocations per
frame) is consistent with first-touch cost of new device memory; if so the pool also removes it (hypothesis; distinguishing
test below). Memory cost: up to one 256 MiB block of fragmentation per memory type, plus retained empty pool blocks
(≤ max(`ANYPS3_RSX_MEMORY_CACHE_MB`, one block)) on top of the exact recycler's own ≤ `ANYPS3_RSX_MEMORY_CACHE_MB`.

### 2. DrawJob borrows pinned resources

**Verified source argument:** `graphics_draw` pushes every attachment (`i<s.colors`), the depth image, every non-null
texture and raw buffer into `GraphicsBatch::pins/buffer_pins` before publishing the job; pins are released only by
`graphics_reset`/`~GraphicsBatch`, both after `b.sync()` (recorder drained), and visual jobs queued in the batch are
replayed before that. So the job's raw pointers stay valid; framebuffer cache entries take `weak_from_this()`
(`GraphicsImage` now derives from `enable_shared_from_this`). Removes per non-null slot one atomic increment on the worker
and one decrement on the recorder thread, per draw half (HIRES2-W attributed ~5.4 % of the worker to these copies;
saving unmeasured).

### 3. Report floors (RESET_REPORT, ZPASS)

**Verified source mechanism (before):** `defer_report` copied, for every report, every report-flagged pending draw of
every flight and of the current batch; `rsx_graphics_reset_report` rewrote the flag of all of them: O(draws in flight) per
report and per reset, with ~230 reports per heavy frame. Draws are only appended and a reset un-attributes exactly the
draws before it, so `pending_report_floor`/`Flight::report_floor` replace the rewrite (O(flights)) and the scan starts at
the floor. `retire_work` applies the same floor to the cumulative counter. **Test:** resets interleaved with submissions,
expectation 64 samples per full-surface quad; three mutations fail.

### 4. Vertex-constant chunks

`refresh_vertex_constants` memcmp'd all 7.5 KB of the bank whenever any constant changed (every draw that uploads a
matrix). `RsxShaderArray::write` now bumps a per-128-element chunk version (value changes only) and the array has an
identity (new on copy/assignment); unchanged chunks of the same never-borrowed bank are skipped. Test oracle: cache equals
the bank, and exactly the chunks that differ from a shadow copy get a new upload version, through FIFO writes, same-value
rewrites, assignment and borrows; mutations (ignore borrow / ignore identity) fail.

### 5. Typed-RTT literals

The generic decoder's `raw_byte` divided by `w[1..6]` (pitch, bpp, sample grid, swizzle extents) per byte per texel at
up to 3840×1408. Those words are frozen with `view.fs` (`view.data` is written only together with it), so they are literals
now; the visual-typed pattern replacement runs first. `test_rsx_rtt` passes with literals on and off, a wrong pitch literal
fails it, `test_rsx_render_scale` passes.

## Regression suite (lavapipe, cloud container)

**Verified:** full run on the final tree (1a37cdb + docs; 202 modules, 904 tests, 4 skips; 4 parallel modules,
`ANYPS3_REQUIRE_VULKAN=1`), logs in the session scratchpad only. 18 modules failed; 17 of them fail identically on the
untouched base 95756e2 re-run under the same conditions, so they are **not** regressions of this round:
- environment: `test_sample_profile`, `test_sample_profile_integration`, `test_spudyn_profile`, `test_spuperf2_capture` (need
  tools under `local/`), `test_present_window` (SDL offscreen driver without Vulkan), `test_program`, `test_services` (30 s
  compile timeouts under load), `test_ppu_fp_lazy`, `test_ppuperf3`, `test_rsx_residency`, `test_savedata_vsh` (GCC 13
  `-Werror` diagnostics / assert macro);
- same assertion on base: `test_rsx_fp_cache` (constant_rewrites), `test_rsx_sampler_identity` and
  `test_rsx_resident_sampler` (source identity), `test_rsx_perf_decode` (special_lanes), `test_rsx_pipeline_cache`
  (read-only save not failing — the container runs as root), `test_rsx_drain_bound` (guarded-read bracket off by one clear,
  without the FIFO window; timing under a loaded 4-vCPU host).
  These deserve a look on the PC (they may also fail there with GCC 15/NVIDIA or be container artifacts).
- **One regression, fixed:** `test_rsx_gpu_status` calls `Session::retire_work` with the old signature; the report floor
  parameter now defaults to 0 (every draw counts). Re-run passes, together with deferred_labels, constant_chunks,
  memory_pool, rsx2_draw, rtt, recorder; the pool-forced heavy set (hook, 8 MiB blocks: render_scale, visual_history,
  pipeline, graphics, vk_submit, recorder, rsx2_draw, visual_msaa, vk_resident, rtt, alias_flow, deferred_labels) passes on
  the final pool code.

## Synthetic measurements (cloud container, noisy 4 vCPU; diagnostic, not game evidence)

- **Draw path, instructions per draw** (`bench/rsx_draw_bench.py` scenario 3 two textures, `ANYPS3_RSX_RECORDER=1`,
  callgrind zeroed after warm-up, worker thread only): ~30k instructions per draw; no item > 7 % (prepared 69 % inclusive,
  graphics_draw 10 %, bind_draw_blocks 5.4 %, graphics_pipeline 5 %, register reads 5 %, refresh_registers 4.7 %,
  vector zero-fills 4.8 %, texture_metadata 3.6 %, fragment-program load + bytes re-read ~8 %, allocator ~10 %).
  Lavapipe dominates wall time (36 µs/draw), so the bench is only an instruction-count tool here.
- **FIFO decode** (`bench/rsx_fifo_bench.py`, 12 packets per draw unit, no draw executes): serial 51–64 ns/packet;
  renderer thread on concurrent memory with page broker (the game's configuration) **~80–120 ns/packet ≈ 1 µs per draw
  unit**; without the page broker 150 ns (bench artifact, corrected). Profile: guest scalar loads/transfer ~37 %, decode
  chain ~60 %. **Refuted for this stream:** the opt-in FIFO window is slower (213 ns/packet with 4 KiB fills, 102 ns with
  256-byte fills) because every non-plain method (constants, BEGIN/END) forces a refill — no reason to make it default.
  **Derived:** plain FIFO decode is ~2 ms of a 2000-draw frame, so HIRES2's "~23 % non-draw decode" must be dominated by
  specific actions (reports/labels/semaphores, zcull sync 0xa1c = a submit, blits), not by register stores.

## Findings without a change (for the PC team)

- **Every render pass is a full GPU drain (verified source, `rsx_vk_graphics.py` render-pass creation):** both external
  subpass dependencies are `EXTERNAL(ALL_COMMANDS, MEMORY_WRITE) → pass` and `pass → EXTERNAL(ALL_COMMANDS)`. This
  matches "native pass begins 12.6 µs vs 1.55 µs visual" (a native begin waits for the preceding heavy visual run).
  **Derived, why not changed:** hazard-tracked dependencies would not remove the common case: native N → visual run V →
  native N2 on the same target needs N→N2 ordering, and any barrier also waits for V. Gains only where the next pass is
  independent of every un-barriered earlier pass; needs a GPU measurement of how often that happens (count passes whose
  attachments/textures do not intersect the images written/sampled since the last barrier) before implementing.
- **Fragment-program bytes are re-read from guest memory every draw** (`load_fragment_program_shared`: bulk transfer +
  memcmp, ~8 % of synthetic per-draw instructions). A CPU-dirty-watch memo (as the raw texture cache uses) could skip it;
  needs care for GPU-authoritative overlap. Not implemented.
- **Per-draw heap traffic** ~14 frees per draw in the synthetic draw (Range parts vectors, Texture level vectors, scratch
  vectors); individually ≤ 1 %.

- **Two-thread RSX worker (cloud item 3), derived constraint:** a split whose front thread only decodes the FIFO moves
  ~1 µs of ~9 µs per draw (plain decode, above) off the worker. To approach the HIRES2 estimate (~16–17 ms in heavy frames)
  the front must also own `rsx_execute_draw`'s state capture (register snapshot, index/vertex extents, texture metadata,
  fragment-program bytes) and hand a self-contained draw packet to the back (Session: bind, texture images, prepared,
  graphics_draw). Ordering rules that follow from the source read: GET may only be published up to the back's completed
  position; every guest-visible action (labels, reports, semaphore release/acquire, REF, flips, events, host requests)
  drains the back first; a release that targets the command buffer drains before further decode. Not prototyped.

## Design note: horizontal scale 8/3 (cloud item 6; user decision pending)

**Verified source facts:** the render scale is one integer `NativeVulkan::graphics_render_scale` used for both axes:
visual companions are `width*scale × height*scale` (`graphics_image` pair creation, `graphics_visual_history` extent checks
`visual->width==width*scale`), visual draws set `raster_width/height = width/height*scale`, history/merge copies scale
native `VkImageCopy` regions by the same integer, typed-RTT visual reads map a native grid texel to an S-wide block
(`visual_xyl`), and every cache budget scales by `1+scale²`.

**Derived consequences of a horizontal factor 8/3 (vertical 2):**
- Native texel edges stop coinciding with visual pixels (3 native columns = 8 visual columns). Every exact block copy
  (alias merge / equal-footprint rebind history, native→visual sync, sample-grid rebinds) becomes a resampling step or must
  be restricted to 3-column-aligned spans; the history planner's whole-texel guarantee no longer holds per texel.
- The MSAA sample-grid rule `G = X·nx + sample` scaled by an integer would need a rational mapping; the game's 2× diagonal
  resolve reads the grid as ARGB8, so the visual grid must stay plane-ordered or the comb artifact returns (HIRES-A).
- Presentation would then be 1:1 at 2560 columns (no 4:3 horizontal resample) — the visible gain.
- GPU cost: visual pixels ×4/3 versus scale 2 (2560×1408 instead of 1920×1408): visual draws ~7.3 → ~9.7 ms/frame at
  1440p walking (1031sm1 split), memory ×4/3 for visual images.
- Smallest viable step: separate `scale_x`/`scale_y` in `NativeVulkan` (all integer paths keep `scale_x==scale_y`), then a
  rational `scale_x` only for the visual raster and presentation, with native↔visual copies falling back to the native
  reconstruction for non-aligned spans (counted per reason like `PERF RENDER_SCALE_SYNC`).

## Not done / limits

No game build, no FPS, no NVIDIA run, no hitch ring. The pool's interaction with NVIDIA memory types (single
DEVICE_LOCAL type expected) and its effect on compression are unverified. Two-thread RSX worker (cloud item 3), visual
MSAA plane→sample load path and horizontal scale 8/3 (items 4/6) not started.

## First next actions on the real PC

1. Build the branch (one integration build) and run alternating QUIET 1440p walks against 1031/1034, then the same build
   with `ANYPS3_RSX_MEMORY_POOL=0` (same binary) — compare FPS, worst frames and `vk_memory_allocs/frees`,
   `vk_pool_ranges/blocks` per window.
2. Full frame ring + `hitch_marks.py` on the pool build: classes A (vkFree/Alloc spans) and B (GPU slowdown frames) should
   vanish if the pool hypothesis holds; if B remains, first-touch is refuted.
3. Scale-1 walk alternating with 960/1031 for the CPU cuts (DrawJob, report floors, constant chunks): rsx_worker ms/frame and
   µs/draw (heavy frames).
4. `ANYPS3_RSX_RTT_LITERALS=0` vs 1 at scale 2 with `ANYPS3_RSX_VK_MARKS` for the decoder's GPU time.
