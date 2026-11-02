# Cloud round HIRES6: RSX worker draw unit and GPU work at scale 2 — 2026-10-31

**Author/model:** Claude (cloud container) as coordinator with two Opus subagents, F (RSX worker CPU) and G (GPU work at
render scale 2); branch `ccr-b1a3987e-aypb5o`, based on 46aa60f (HIRES5 merged + its record). **Scope:** the user's 1440p
60 FPS goal. After HIRES5 the derived heavy-frame RSX worker was ≈ 13.2 ms (no headroom) and the scale-2 GPU (≈ 15.4 ms
on the PC in HIRES2) had not been worked on in the cloud. The container has **no game data, no `local/` tree and no NVIDIA
GPU** (4 vCPU, lavapipe only): no game build, run, FPS or frame figure was produced. Every runtime statement is a native
unit test on **lavapipe**, a callgrind count on a synthetic bench (instructions are not time) or a GPU work count on a
synthetic scale-2 stream (lavapipe GPU time does not predict NVIDIA time).

## Test environment (cloud only)

As in [HIRES5](2026-10-31-cloud-hires5.md#test-environment-cloud-only). New this round:
- **Game-shaped bench mode (F, 00a6174):** `bench/rsx_fifo_actions_bench.py tracked=1` re-creates the register bank and the
  four shader arrays as never-borrowed objects and sends the harness state through the FIFO in one setup turn (registers as
  single methods, program via 0x1e9c/0xb80, constants via 0x1efc/0x1f00); it checks that the result equals the harness
  state and prints `exposed_blocks=0 program_borrowed=0 constants_borrowed=0`. Existing modes are unchanged. Validated
  against HIRES5's no-exposure diagnostic: 33,487 vs 33,335 Ir per indexed draw (+0.45 %). This replaces the diagnostic
  `noexpose.py` as the primary worker metric.
- **Draw unit** = per indexed draw + b0/2000 (b0 = a no-draw frame of 2000 units of state, reports, releases, surfaces,
  32 turns).
- **Scale-2 GPU stream (G, `tests/test_rsx_gpu_work.py`):** `ANYPS3_RENDER_SCALE=2`, a 2-sample scene with D24S8 depth,
  guest clears, depth-tested draws, a surface copy, a native upload with its visual rebuild, a guest-layout unpack upload,
  device data uploads and a single-sample post target; every native and visual pixel is compared with a CPU model in 6
  switch modes with Vulkan sync validation on (`VK_LAYER_KHRONOS_validation`, `VK_LAYER_VALIDATE_SYNC=1`).

## Changes

### Agent F: RSX worker CPU (merged as 8126bbc)

No new switches: every cut is exact and unconditional; a doubt about one cut is settled by reverting its commit.

| commit | change | exactness (full argument in the code comment) | test / mutation check |
| --- | --- | --- | --- |
| 00a6174 | bench: game-shaped `tracked=1` mode | existing modes emit no setup segment | bench self-check |
| 111927b | `GuestMemory::Range` keeps a single part inline (no heap block per creation) | container change only: contiguous storage, order, per-part pins | test_par13_memory `range_parts` (mutant: second part not moved) |
| 486cb4a | no GET publication before BEGIN | BEGIN with a primitive only edits host draw state or stops (nested begin; the catch publishes the same GET); END still publishes before the draw | test_rsx_fifo_plain (mutant: masks "3 15", expected "3 7 15") |
| 94d76b0 | an index-reuse hit hands a triangle list its cached, immutable shared triangles | entry = complete triangles + 0..2 trailing indices = `assemble()`; pinned by `RsxDrawState::shared_triangles` until the next draw; copied into the scratch lists before a CPU fallback | test_rsx2_draw `triangle_list()==expect` |
| b1df5b2 | zero only the blob head words that were written | non-alias path writes only words 0..47 and each sampled unit's 32-word block below word 560; any other writer forces the full zero | test_rsx_perf_draw `blob_descriptors` ("stale blob word 48") |
| 1d4f566 | the triangle list rides as the draw slice's tail (no `data.insert`) | `data[13]` = blob size after the last blob word; no shader reads w[13] for `data[15]==1`; capacity/overflow checks include the tail | existing pixel/stream tests (not mutation-checked) |
| 1d9a24b | adjacent upload slices share one copy region | only touching regions with src==dst merge; slices disjoint and increasing | test_rsx_gpu_batch `merged_uploads` |
| 0345db6 | FIFO method tables resolved once per run; inline 0x1efc load position | tables immutable after construction; `rsx_reg` == `value_or(r,0)` | existing FIFO tests |
| 2a453cc | texture-context head compared in one OR-of-XOR pass | same verdict, same stored format | existing tests |
| fca7da5 | the direct draw passes a shared empty raw-buffer array | that array was never written on this path | existing tests |
| 4cd2777 | constant-window registers (0x1efc, 0x1f00–0x1f7c) left out of the register-block epochs; `refresh_registers` stores 0 for them | enumeration of every register read in the generated GLSL: all literals outside the window, computed reads 0xb40+4t (t<10), 0x2c0/0x2c4+8i (i<8), 0x1480+4(y&31), 0x32c..0x344(+0/32), direct `w[16u+]` only 0x1fc0/0x1478/0x1ff4; `refresh_registers` is the only consumer of bank epochs; the bank keeps every value for CPU readers | test_rsx_register_epochs (mutant: window not zeroed); test_rsx_perf_draw `register_prefix` oracle updated (7424d72) |
| ad943da | all-plain FIFO packets stored without per-method dispatch | the same stores in order; capture off; a resumed packet (`packet_index`) is never all plain | test_rsx_fifo_plain ("register bank differs between the fast path and the chain") |
| 22331c0 | `[[gnu::always_inline]]` on `RsxRegisters::write` | inlining only (fixes 4cd2777's codegen: GCC had stopped inlining `write`, b0 +0.9 M) | — |
| 7424d72, 2ed1bf3 | test oracle for the constant window; `[[maybe_unused]]` for builds without ANYPS3_RSX_VK | — | — |

### Agent G: GPU work at scale 2 (merged as b577d49)

| commit | change | switch (default) | per-frame counts on the stream |
| --- | --- | --- | --- |
| a285134 | `PERF RENDER_SCALE_WORK` counters (scale > 1, every 120 frames: barriers, barriers_elided, pipeline_barriers, pass_native/visual/attachments, copy_buffer(_bytes), copy_image(_texels), buffer_image(_texels), blits/blit_texels, clears/clear_texels, dispatches); `ANYPS3_RSX_VK_MARKS`: a clear/copy/blit/raw barrier without its own mark gets one before the next mark | diagnostic | marks: clears 16 → 24 of 24, image copies/blits 12 → 24 of 24 |
| 6a2eef6 | skip a full barrier directly after the batch's previous one (no command recorded between: `graphics_command_serial` unchanged) | `ANYPS3_RSX_BARRIER_DEDUPE=1` | full barriers 17 → 16 |
| cec55b7 | one leading barrier per clear/copy group (visual half, depth after colour of one guest clear) | `ANYPS3_RSX_PAIR_BARRIERS=1` | 16 → 11; a guest colour+depth clear at scale 2: 4 → 1 |
| b4c7a3a + c03e1ad | a render-pass end counts as the last full barrier only when everything since the last full barrier is render passes, each begun directly after the previous end (b4c7a3a's unrestricted rule failed sync validation in test_rsx_visual_batch: clear → unrelated pass → copy, WAW; fixed by c03e1ad) | `ANYPS3_RSX_PASS_BARRIER=1` (off with `ANYPS3_RSX_BARRIER_HOST=1`) | 11 → 10 |
| e752fd6 | `site_<GraphicsSite>=commands/texels` for image copies/blits in `PERF RENDER_SCALE_WORK` | diagnostic | — |
| abaf95c | visual 2× MSAA plane→sample load path: an eligible draw creates its working images; planes holding the content are loaded by one 2-sample pass (`gl_SampleID` selects the plane; depth via `gl_FragDepth`; D24S8 stencil by 8 bit-plane REPLACE draws with per-sample discard); needs sampleRateShading | `ANYPS3_RENDER_SCALE_MSAA_LOAD=0` (with `ANYPS3_RENDER_SCALE_MSAA=1`); **NVIDIA test pending** | off: no change |

`ANYPS3_RSX_BARRIER_DEDUPE=0` turns all barrier skipping off (also the pass-end rule). Stream totals (verified, lavapipe):
**full barriers 17 → 10 per frame** (17 = all three cuts off, derived from the per-mode counts), 13 pipeline barriers in
all with the defaults (10 full + 3 image/buffer barriers); pass begins (9 native, 9 visual), copies, clears and
pixels unchanged. Per mode: defaults 10; PASS_BARRIER=0 11; PAIR_BARRIERS=0 15; PAIR=0 + PASS=0 16; DEDUPE=0 12.

## Synthetic measurements

### RSX worker (coordinator, merged 8126bbc; two alternating rounds of three builds in the same session)

Builds: base = 46aa60f + the `tracked=1` bench file of 00a6174; F = cloud/hires6-worker 2ed1bf3; merge = 8126bbc.
Callgrind, worker thread, 3 frames after 2 warm, `-O2 -march=x86-64-v3 -ffp-contract=off -g -DBENCH_CALLGRIND`.
Ir; round 1 / round 2.

| build | tracked per indexed draw | tracked b0 (M/frame) | **tracked unit** | default per indexed draw | default b0 (M/frame) | **default unit** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| base 46aa60f | 33,297 / 33,416 | 16.642 / 16.613 | **41,617 / 41,722** | 40,388 / 40,208 | 16.632 / 16.643 | **48,704 / 48,529** |
| F 2ed1bf3 | 30,132 / 30,160 | 14.949 / 14.967 | 37,606 / 37,643 | 37,941 / 38,023 | 15.007 / 15.002 | 45,444 / 45,523 |
| merge 8126bbc | 30,195 / 29,983 | 14.929 / 14.936 | **37,659 / 37,450** | 38,120 / 38,097 | 14.891 / 14.952 | **45,573 / 45,573** |

- **Verified (bench):** merge vs base, means of both rounds: tracked unit 41,670 → 37,555 (**−9.9 %**; per indexed draw
  −9.8 %, b0 −10.2 %); default unit 48,617 → 45,569 (−6.3 %). F's own per-commit table (each commit built from its own
  snapshot) is in its result file; F's tip measured 37,281 tracked there (22331c0, 0.9 % below this session's figure for
  2ed1bf3 — run-to-run spread under a different load). Target was −25 % (≤ 31 k): **not reached**.
- **Verified (bench):** G's GPU-side changes cost the worker thread nothing measurable (merge vs F within ±0.3 % in both
  modes; the command serial adds one locked increment per recorded action command).
- **Derived (HIRES2 calibration, default mode, 2.9 ms + 9.1 µs per 86.1 k Ir unit):** heavy frame ≈ **12.5 ms at 2000
  draws, ≈ 14.5 ms at 2400 draws** (HIRES5: 13.2 / 15.2 ms). Only if the game's geometry is as static as the bench's and
  instructions scale to time.

### GPU (G, verified on the stream only)

Full barriers 17 → 10 per frame; marks now attribute every clear and copy to itself. **The −2.5 ms GPU target is not
demonstrated** (no NVIDIA here). The only change aimed at the large items (second visual sample plane ≈ 2.3 ms on the PC) is
the MSAA load path, default off, unverified on NVIDIA.

## Findings

- **Native render-pass begin (PC: 12.6 µs vs 1.55 µs visual; verified from source, hypothesis for the PC):** both begins use
  the same render pass object (LOAD/STORE, GENERAL→GENERAL, the same external dependencies, full render area) and the same
  framebuffer cache; the begin records no barrier or query. Marks attribute the time between two marks to the later one, and
  before a285134 the visual half of every clear and surface copy, visual rebuild blits, texture uploads and image-creation
  barriers had no mark: on the stream 8 of 24 clears and 12 of 24 image copies/blits. They are recorded just before the next
  native draw, so their GPU time (and the begin dependency's wait for them) was booked as "native pass_begin". **Hypothesis:**
  with a marks build of this branch, native `pass_begin` falls to about the visual figure and `clear:clear` /
  `*:copy_image` / `tex_upload:barrier` grow by the same total. If native begins stay ~12 µs, the hypothesis is refuted
  (next suspects: depth/stencil LOAD in GENERAL layout, occlusion query traffic).
- **Copies (verified from source):** inventory of every copy/blit per frame at scale 2 (clears, surface copies incl. SRGB alias,
  feedback snapshot and packed history, visual history, visual rebuilds, uploads, readbacks, typeless aspect copies,
  texture uploads, device data, present): no destination is provably fully overwritten before it is read, and none repeats
  unchanged content without an existing cache. **No copy was removed.** The PC should first check packed `history_copies`,
  feedback snapshots and SRGB/UNORM alias copies in `PERF RENDER_SCALE_WORK site_*`.
- **Per-pass GPU drain (derived, not changed):** every pass begin/end still drains (ALL_COMMANDS external dependencies);
  plane passes P0 → P1 of one run need no drain, but dropping it needs a dependency-free render pass object plus explicit
  hazard tracking; estimated ≤ 0.1–0.3 ms/frame.
- **Worker remainder (derived from F's tip profile):** about 9 k of the 29.8 k Ir per indexed draw is lavapipe driver work
  (`vkResetCommandBuffer` ~2.2 k, query and copy calls) or callgrind artefacts (recorder spin under serialised threads); the
  rest is ~40 items of 100–900 Ir (three Range creations ~1.6 k with a required observer point, the 9.6 KB vertex copy
  ~1.3 k, ~250 register presence reads ~1.2 k, pipeline and fragment-source keys, texture metadata per sampled unit).
  Per-item trimming has run out of large items; HIRES7 moves work off the worker thread instead.

## Coordinator review (before the merges)

- **G, barrier dedupe:** `graphics_command_serial` is bumped by the `rsx_vk_resources.py` wrappers of every action command
  used outside a pass (copies, blits, clears, fills, dispatches, query resets/copies, pipeline barriers) and by every pass
  begin/end; the unwrapped `vkCmd*` names in `anyps3/` are binds, dynamic state, push constants, draws and queries inside
  passes, timestamps and conditional-rendering begin/end (a read of a predicate written by a counted command). The serial is
  global, so another thread's recording only disables a skip. `CommandSerialTests` fails on an unclassified `vkCmd*` name.
- **G, pass-end rule:** the end dependency is `0 → EXTERNAL`, src ALL_GRAPHICS + colour/depth attachment writes, dst
  ALL_COMMANDS + MEMORY_READ|WRITE; the rule applies only when each pass in the chain began directly after the previous end
  (serial +1) and ended directly after its begin (no counted command inside). The only shader storage writes in passes are
  per-draw status words in the batch data buffer, read by the host after the submit; no later command in the batch writes
  them. Recorded as a risk: a future pass with weaker dependencies or other shader stores must not use the rule.
- **G, group lead:** the clear path checks image identity (a repeated image keeps its barrier); `graphics_copy`'s visual half
  takes its own barrier whenever an MSAA working image exists (`graphics_ms_any`), since `graphics_ms_planes` may record an
  expand pass before it. `graphics_group_lead(false)` stops on an open pass, queued visual jobs or deferred plane draws.
- **G, MSAA load path off:** `graphics_ms_samples()` is 2 without the test hook, every changed loop runs `layers` (= 2)
  times as before, and `ms_load` exists only with the switch on, so the eligibility block is unchanged with the default.
- **F, constant window:** `RsxRegisters::epochs` is read only by `Session::refresh_registers` (grep), which zeroes exactly
  the slots 0x1efc (block 30) and 0x1f00–0x1f7c (first 32 words of block 31) after copying a block; `operator[]`, `erase`
  and `clear` still bump every epoch (conservative).
- **F, shared triangles:** the entry's vector is immutable and `shared_ptr`-pinned by the draw state; the CPU fallback copies
  it into the scratch lists before use; the device-geometry materializer never shares (`share_triangles=false`).
- **F, all-plain packets:** taken only when every register of the packet has a plain-table entry, the packet is in the bank,
  not resumed and no capture is active; it performs the same `registers.write` calls in the same order as the per-word loop.

## Regression tests (lavapipe)

- **Agents (before the merges):** G ran 24 modules on an archive snapshot of its tip abaf95c, all OK (test_rsx_gpu_work,
  test_rsx_visual_msaa — NVIDIA part skipped, no ICD —, test_rsx_visual_batch, test_rsx_render_scale,
  test_rsx_visual_history, test_rsx_vk_submit, test_rsx_recorder, test_rsx_graphics, test_rsx_pipeline, test_rsx_vk,
  test_rsx_vk_resident, test_rsx_vk_memory_pool, test_rsx_rtt, test_rsx_tex, test_rsx2_draw, test_rsx_perf_draw,
  test_rsx_depth_blit, test_rsx_depth_owner, test_rsx_drain_bound, test_rsx_gpu_batch, test_rsx_gpu_status,
  test_rsxfast_draw, test_rsxvk21, test_rsxvk22_cpu370). F ran its area list on a snapshot of 22331c0 (fifo_plain,
  register_epochs, rsx2_draw, graphics_cache, constant_chunks, gpu_batch, graphics_costs, rsx_vk, rtt, vp_flow,
  fifo_contract, gpu_upload, gpu_vertex, host_cost, visual_batch, vk_resident, rsxfast_draw, render_scale, index_decode in
  all 4 compiler variants, guest_memory_guard): all OK; the failures found there were fixed on the branch and re-run OK
  (test_rsx_perf_draw `register_prefix` oracle → 7424d72; `-Werror=unused-variable` in builds without ANYPS3_RSX_VK in
  test_rsx_tex, test_rsx_tex3, test_par11_memory and two tests each of test_par13_memory/test_par10_memory → 2ed1bf3).
- **Coordinator, merged snapshot 8126bbc (`git archive`, one process per module, two lanes, lavapipe):** the union of both
  area lists, **40 modules, all OK** — test_rsx_gpu_work, test_rsx_visual_msaa (1 skip: the NVIDIA 2× comparison, no ICD),
  test_rsx_visual_batch, test_rsx_render_scale, test_rsx_visual_history, test_rsx_vk_submit, test_rsx_recorder,
  test_rsx_graphics, test_rsx_pipeline, test_rsx_vk, test_rsx_vk_resident, test_rsx_vk_memory_pool, test_rsx_rtt,
  test_rsx_tex, test_rsx2_draw, test_rsx_perf_draw, test_rsx_depth_blit, test_rsx_depth_owner, test_rsx_drain_bound,
  test_rsx_gpu_batch, test_rsx_gpu_status, test_rsxfast_draw, test_rsxvk21, test_rsxvk22_cpu370, test_rsx_fifo_plain,
  test_rsx_register_epochs, test_rsx_graphics_cache, test_rsx_constant_chunks, test_rsx_graphics_costs, test_rsx_vp_flow,
  test_rsx_fifo_contract, test_rsx_gpu_upload, test_rsx_gpu_vertex, test_rsx_host_cost, test_rsx_index_decode,
  test_rsx_tex3, test_guest_memory_guard, test_par11_memory, test_par13_memory, test_par10_memory.
- **Full suite:** deferred to the end of HIRES7 so that one run covers HIRES6 and HIRES7 (4 vCPUs shared with two agents).

## Risks

- **Barrier dedupe (derived):** depends on every action command going through the `rsx_vk_resources.py` wrappers. A future
  `::vkCmd*` call or a function-pointer command outside a pass would make a skipped barrier unsafe; the static test only
  checks names, not qualification. The pass-end rule depends on the current render-pass dependencies.
- **MSAA load path (hypothesis):** a load costs one full-screen 2-sample pass per image (+8 bit passes for D24S8); a target
  that alternates one draw and one read pays load + expand per draw. Depth passes through float conversions (visual image
  only; native bytes unaffected).
- **Constant window (derived):** a future shader reading a window register through the register blob would read 0 (comments
  at both sites name the proof).
- **Index reuse (derived):** a draw keeps the entry's triangle vector alive until the next draw (one list of memory).
- **Upload region merge (verified on the bench):** +430 Ir/draw on lavapipe from codegen/timing shifts although it removes
  282 k Ir/frame of `vkCmdCopyBuffer`; whether it pays on NVIDIA is unmeasured.
- Counters: plain relaxed load+store; concurrent recording of two batches can lose an increment in the PERF figures
  (diagnostic only; the serial itself is a locked increment).
- Instructions and lavapipe work counts are not time; nothing here was measured on the PC.

## First next actions on the real PC

1. One integration build of `ccr-b1a3987e-aypb5o` (HIRES3–HIRES6); QUIET alternating 1440p walks vs 1031/1034: rsx_worker
   ms/frame and µs/draw in heavy frames, main-thread ms, GPU busy ms/frame.
2. Same binary, `ANYPS3_RSX_VK_MARKS=1` scale-2 walk (diagnostic): `PERF GPU_MARKS` vs 1031sm1 — does native `pass_begin`
   fall to ~1.5 µs while clear/copy marks grow (item "Native render-pass begin")? `PERF RENDER_SCALE_WORK` per frame:
   barriers, barriers_elided, site_* copy figures.
3. Same binary, alternating: defaults vs `ANYPS3_RSX_BARRIER_DEDUPE=0` (GPU busy ms/frame; images identical); then
   `ANYPS3_RSX_PASS_BARRIER=0` and `ANYPS3_RSX_PAIR_BARRIERS=0` separately to split the saving.
4. `python -m unittest tests.test_rsx_visual_msaa` on the NVIDIA device (the 2× load comparison runs automatically there),
   then `ANYPS3_RENDER_SCALE_MSAA=1 ANYPS3_RENDER_SCALE_MSAA_LOAD=1` vs `MSAA=1 LOAD=0` vs `MSAA=0` walks: GPU busy,
   `PERF RENDER_SCALE_MSAA loads/expands/plane_draws`, 1:1 crops.
