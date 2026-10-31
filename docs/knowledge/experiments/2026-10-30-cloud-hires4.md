# Cloud round HIRES4: RSX worker CPU per draw and between draws — 2026-10-30/31

**Author/model:** Claude (cloud container) as coordinator with Opus subagents A (per-draw path), B (FIFO/non-draw) and C
(renderer guest-memory access); branch `ccr-b1a3987e-aypb5o`, based on de6bc4f (HIRES3 tip of `ccr-8139488c-79xg7b` +
its suite fix). **Scope:** the user's 1440p 60 FPS goal; the PC evidence of HIRES2 names the RSX worker thread (~20–22 ms
in heavy frames, ≈ 2.9 ms + 9.1 µs per draw, flat profile) as the CPU critical path, target <= ~13 ms. The container has
**no game data, no `local/` tree and no NVIDIA GPU** (4 vCPU, 15 GB): no game build, run, FPS or frame figure was
produced. Every runtime statement below is a native unit test on **lavapipe** or a callgrind count on a synthetic bench
(worker thread instructions, `-O2 -march=x86-64-v3`); instruction counts are not time (callgrind undercounts atomics and
cache misses). The real PC must build and measure alternating QUIET runs before any FPS claim.

## Test environment (cloud only)

As in [HIRES3](2026-10-28-cloud-hires3.md#test-environment-cloud-only) (lavapipe, glslang, SDL2, valgrind; GCC 13.3 with
the scratch `c++` shim for `-Wno-attributes -foptimize-sibling-calls -lSPIRV-Tools-opt -lSPIRV-Tools`), audio forced off,
`LP_NUM_THREADS=1`, one process per test module. Valgrind cannot run AVX-512, so benches are compiled `-march=x86-64-v3`.

## Changes (each commit has its own test and mutation check; all exact)

| commit | change | switch (A/B) | test (independent oracle) |
| --- | --- | --- | --- |
| 8e46858 | `bench/rsx_draw_bench.py`: game-like scenario 5 (8 vertex-constant vec4, 3 meshes' attribute offsets, 2 units × 3+2 textures, inline FP constant, 300 BE16 indices, blend/depth toggles) and `--callgrind` (worker Ir/draw, counters zeroed after warm-up) | — | diagnostic |
| 9b54e56 | `Reg` reads inline (`RsxRegisters::value_or` = const `find`), register-prefix rebuild over set bits only, no zero-fill copies (triangle list insert, draw-blob zeroing with 16-byte stores) | — | `test_rsx_perf_draw` (register reads vs a `std::map` model incl. erase/borrow/unaligned; prefix vs reset-default table; triangle list; stale blob words) |
| b32d317 | Pipeline key fields packed once; memo of the last `graphics_pipeline_fast` lookup (version bumped at every insert/assign/erase); no snapshot copy for never-borrowed register blocks (`register_values_stale`) | — | `test_rsx_perf_draw` (compile counts with 1024/1-entry caches + pixels; stale borrowed snapshot) |
| 073b874 | The draw's 16 texture units reused per thread (guard resets the touched units at scope end, also on exceptions; nested draw gets its own array); no empty `optional` of 16 Textures; disabled-attribute words written directly | — | `test_rsx_perf_draw` (stale units after a stopping draw) |
| d3d0466 | Texture metadata entry keeps its configured sampling keyed by the five sampling words (base+8/0xc/0x10/0x14/0x1c; stored only after `configure_texture_sampling` succeeded); border colour converted once per unit | — | `test_rsx_perf_draw` (sampling words vs register encoding; invalid filter still stops) |
| 5cf923c, cff80c6 | `bench/rsx_fifo_actions_bench.py`: game-shaped non-draw stream on the real RSX worker (`SpuWorkerSession`, concurrent memory + page broker, recorder, lavapipe), callgrind dumps on the worker thread, release-value oracle after the deferred labels | — | diagnostic |
| 45f4a6a | FIFO decode: `perf_capture.active` tested inline (no capture call per method), hoisted method-class table, inline `rsx_address` (cold throw), transform-constant upload 0x1f00–0x1f7c direct (constant + raw register: the chain's exact effects for that range) | — | `test_rsx_fifo_plain` (game-shaped constant uploads vs expected bank) |
| 78dfc00 | FIFO header and the following word in one 8-byte guest load when both are published (`PUT-GET >= 8`) and on one 4 KiB page | `ANYPS3_RSX_FIFO_PAIR=0` (separate loads) | `test_rsx_fifo_plain` `paired_reads` (3000 packets across page ends, PUT kicks mid-packet, serial and renderer thread; an observer proves nothing past PUT is read) |
| a0eaec4 | Per-turn texture-context check compares in place (no ~700-word vector per turn); emptied label/pending vectors of retired flights reused (<= 4 each, capacity only); `defer_report` moves its mapping | — | `test_rsx_graphics_costs` `texture_context_` (plans dropped exactly when an input changed) |
| b602751 | Packet validation as one scan over the class table; the per-word stop path runs only for a packet with an unsupported register | — | `test_rsx_fifo_plain` `unsupported_in_packet` (stops before any word of the packet executes) |

Review by the coordinator (derived from the code, not only the agents' reports): the 0x1f00–0x1f7c fast path matches the
chain (class 1, `rsx_vertex_data_register` returns false for the range, constant write then the final register store);
`rsx_capture_method_checked` returns at once without an active capture; `configure_texture_sampling` reads only the five
memoized words plus the entry's format, and metadata entries are never mutated after insertion; every texture-unit
assignment in `rsx_execute_draw` marks the unit first (the CPU renderer path marks all 16); `RsxRegisters::find` const is
`count(r)?slot:end`, i.e. `value_or`; the three `graphics_pipeline_fast` mutation sites bump the version.

Mutation checks (each made its test fail, then restored): `value_or` ignoring presence; defaults applied to the wrong
block; one triangle too many; no blob zeroing; pipeline memo ignoring the key; stale register snapshot trusted;
texture-unit guard not resetting; unit not marked touched; sampling memo ignoring the words; constant fast path without
the raw register store; pairing without the `PUT-GET >= 8` bound; texture context compared by length only; validation
scan accepting every packet.

## Synthetic measurements (worker thread instructions; diagnostic, not game evidence)

### Per draw (`bench/rsx_draw_bench.py --callgrind`, recorder on, thread 1, dump 3, noise ~0.3 %)

| commit | sc1 constant | sc3 two_textures | sc4 indexed_mesh | sc5 game_like | sc5 scale 2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| de6bc4f base | 29239 | 30580 | 46523 | 45945 | 50742 |
| 9b54e56 | 26501 | 27353 | 40870 | 41090 | |
| b32d317 | | | | 39085 | |
| 073b874 | | | | 37456 | |
| d3d0466 | 23390 (−20.0 %) | 23984 (−21.6 %) | 37832 (−18.7 %) | 36666 (−20.2 %) | 41391 (−18.4 %) |

The target was −35 %; not reached. Limit of this bench: its GuestMemory is **not concurrent** (no page broker), so the
game's admission cost per draw is not in these figures (agent C measures it, below).

### Between draws (`bench/rsx_fifo_actions_bench.py`, 3 frames after 2 warm)

Stream per frame (shaped from HIRES2 counts, not a game capture): 2000 draw units of state packets, 230 occlusion queries
(0x17c8 + 0x1800), 16 back-end + 8 front-end releases, 32 surface switches + clears, 8 zcull syncs, 32 PUT kicks.

| stream | de6bc4f | b602751 | change |
| --- | ---: | ---: | ---: |
| no draws | 35.03 M Ir/frame | 24.11 M | −31.2 % |
| one 8×8 quad per 8 units (250 draws), whole worker | 46.24 M | 35.30 M | −23.7 % |
| same, worker minus `rsx_execute_draw` | 36.92 M | 26.06 M | −29.4 % |

Per action (frame minus the same stream without that action, so including its own packet decode), before → after a0eaec4:
draw-unit state decode ~14.6 k → ~10.4 k per unit; occlusion query 13.3 k → 11.2 k; FIFO turn 42 k → 21 k; surface switch +
clear 32 k → 23 k; label release 14 k → 11 k; zcull sync ~4 k (≈ 18 k with draws pending) unchanged.

### Merged A+B on the game's memory configuration (coordinator, ceab1ba)

`bench/rsx_fifo_actions_bench.py` built from the merged tree (`-O2 -march=x86-64-v3 -ffp-contract=off -g
-DBENCH_CALLGRIND`), `frames=3`, recorder on; release oracle passed in both runs (`releases_checked=24`).

| stream | de6bc4f | ceab1ba (A+B) | change |
| --- | ---: | ---: | ---: |
| no draws | 35.03 M Ir/frame | 24.13 M | −31.1 % |
| `draw_every=8` (250 draws), whole worker | 46.24 M | 33.42 M | −27.7 % |
| `draw_every=8`, `rsx_execute_draw` inclusive | 9.32 M (37.3 k/draw) | 7.43 M (29.7 k/draw) | −20.3 % |

Derived estimate for a heavy game frame (~2000 draw units, ~2000 draws; HIRES2: ≈ 2.9 ms + 9.1 µs per draw ≈ 21 ms):
the per-draw path at −18…−20 % and the between-draw decode at ~−30 % give roughly −3.5…−4.5 ms, i.e. ~16.5–17.5 ms —
**not yet the ~13 ms target**, and only if instruction counts translate to time on the PC (unverified).

## Findings

- **Verified (bench):** ~75 % of the worker's non-draw cost is the FIFO decode itself, not the actions; the decode is
  dominated by renderer-thread guest-memory reads: ~190–240 Ir per 4/8-byte load (ScalarAccess admission), ~820 Ir per
  `GuestMemory::transfer` of a packet with >= 4 words, ~275 Ir per GET store. After the cuts ~43 % of the remaining 24.1 M
  Ir/frame is this admission machinery (`par_memory.py`/`runtime.py`) — agent C's scope.
- **Refuted again (bench):** `ANYPS3_RSX_FIFO_WINDOW=1` costs 66.1 M Ir/frame vs 27.3 M without (refill after every non-plain
  method and GET store), consistent with HIRES3.
- **Measurement artifact (verified):** callgrind counts `rep stosb` per byte; a memset >= 2 KiB (glibc's ERMS threshold)
  costs ~1 Ir/byte in the profile but ~100 cycles on hardware. Scenario 4 carries ~5 k Ir of this (`scratch_indices`
  resize) in base and final; a per-thread decode buffer that removed it (33473 Ir/draw) was reverted as an artifact-only gain.
- **Remaining per-draw cost needs designs, not micro-cuts (derived from the final scenario-5 profile):** vertex upload reuse
  (~3–4 k/draw; device-resident copy keyed by address/span + CPU-dirty watch), per-unit bound-texture memo (~1.5–2 k; needs
  generations for resident and RTT lookups), the 49-slot `graphics_state_blocks` scan (~0.8 k), inline FP constant rewrite
  copying the whole prepared program (~1 k). ~4.1 k/draw of every figure is bench-side writes (~1.7 k) and `Session::flush`.
- **Hypothesis, not changed:** `vkResetCommandPool` on each batch's own pool instead of `vkResetCommandBuffer` (dominant in
  lavapipe's `graphics_reset`); benefit on NVIDIA unmeasured.

## Agent C: renderer-thread guest-memory access (branch `cloud/hires4-mem`, based on b602751)

Bench: B's FIFO action bench on concurrent memory + broker; C added `indexed=1` (every DRAW_EVERY-th unit an indexed
triangle list: 1200 BE16 indices in local memory, 20×20 vertices with positions over two pages + UVs in IO memory, a sampled
texture, FP in local memory). Per draw = (worker Ir/frame with draws − same build without draws) / 500.

| commit | change | switch (default 1) | Ir/frame before → after |
| --- | --- | --- | --- |
| 6195d31 | 8-byte read at a 4-byte boundary = two aligned acquire word loads (was 8 byte loads + byte loop; half of the paired FIFO loads); `PageBroker::cell` 4-entry associative TLS leaf cache (1 entry missed on 98 % of GET stores: the control page and the FIFO page are in different 2 MiB blocks) | — (exact) | 24.11 → 22.51 M |
| 10fead8 | One-page `transfer` / `Range::read` reads on the compact scalar admission (`read_page`: pending hazard, scalar map cache, observer lookup, page hazard before the claim bit) without Lease, span vector, CpuPages or a second translation | `ANYPS3_GUEST_PAGE_READ` | 22.51 → 20.50 M |
| f3f269a | Renderer page cursor (`GuestMemory::RenderCursor`): the FIFO consumer keeps one host page's compact admission between header/value reads; revoked before every method other than a plain store or a transform-constant upload, the owner hook, a perf capture, every GuestMemory API on that thread and on scope exit | `ANYPS3_RSX_RENDER_BATCH` | 20.50 → 16.80 M |
| c4abc87 | bench: `indexed=1` game-shaped indexed draws | — | diagnostic |
| f390501 | One-mapping `GuestMemory::range` creation on the compact admission (creation reads no payload; brokered creation declines on a claimed page) | `ANYPS3_GUEST_RANGE_ADMISSION` | 16.80 → 16.68 M |
| 4df77d6 | The admitted fast paths refill a scalar-map-cache miss from the admitted snapshot; brokered creation checks the claim bit of every page (<= 64) | — | draws: 49.35 → 48.32 M |

Totals (verified, bench): no draws **24.11 → 16.61 M Ir/frame (−31.1 %)**; 500 indexed draws/frame **58.01 → 48.32 M
(−16.7 %)**. Same tip binary with switches off: page read 0 → 22.73 M (vs 20.50 at that commit), cursor 0 → 20.63 M (vs
16.80), range admission 0 with draws → 48.77 M, all three 0 with draws → 57.04 M. Per draw on concurrent memory: whole
worker 67.8 k → 63.4 k Ir; **admission share (guest memory minus inherent payload copies) ~9.6 k (14 %) → ~5.0 k (8 %)**.

Correctness (agent C's argument, reviewed by the coordinator against `Lease`, `CpuPages::try_cpu`, `GpuPages` and the FIFO
loop): every fast path uses the existing compact ScalarAccess protocol (no new waiter type) and completes only accesses the
Lease path completes without side effects; anything else (errors, observer callbacks, claim waits and coherence requests,
stale Range epochs, multi-mapping spans) declines to the unchanged Lease path. `try_cpu` fails only on the `claimed` bit, so
a claim-bit check is the brokered creation's whole broker semantics (creation reads no byte; the later read admits again).
The cursor's hazard is only held while the FIFO loop runs plain stores and constant uploads; a waiting RawPermit or a claim on
the pinned page gets it at the renderer's next read; `GpuPages` revokes before a claim, so the renderer never waits for its
own hazard. The doubleword-as-two-words read is one admissible schedule of the former byte sequence.

**Refuted (derived from the code):** reading several pages one page at a time under a moving hazard is not equivalent (a
claim could complete between the pages: old page 1 + new page 2); multi-page reads stay on the Lease path.
**Not done:** a cursor-held GET store (~1.0 M Ir/frame; it would need a second held hazard, changing every quiescence scan).

Tests: 11 new tests in `tests/test_par13_memory.py` (independent expectations: Python-computed big-endian values, map-layout
offsets, owner-written values), covering an exclusive raw writer and a GPU claim arriving while the cursor or a read holds the
page (granted, no deadlock, nothing served while held, the new bytes read afterwards), a watch published on the pinned page,
observer interest and claims taking the Lease path, unmapped/write-only/stale-Range accesses still faulting, each switch at 0.
Mutations (each failed its test, restored): halves swapped; cache hit without the block compare; claim / observer / epoch /
switch checks removed (page read); exclusive check removed (renderer deadlock caught by the 30 s deadline); cursor claim
check removed; observer summary removed; ScalarAccess revoke removed; range claim / observer / write permission removed; no
refill; claim bit of the first page only. Not mutation-checked: the explicit cursor revoke before non-plain methods
(defence in depth: every blocking path already revokes through a GuestMemory call) and the refill's span checks (redundant
with the validating lookup by design).

Risks (agent C): the cursor can delay an owner RawPermit or a claim on the FIFO page by up to one stretch of plain packets
(PC: watch main-thread RsxWait and `PERF GUEST_MEMORY`); `read_page` also serves SPU DMA GETs and PPU HLE reads (same
protocol as their scalars; speed effect there untested); a claimer starting during a brokered creation no longer waits for
it (argued unobservable); the per-draw gain of the refill depends on scalar-map-cache collisions of the bench's layout
(hypothesis for the game); the `stat_slow_leases` PERF counter now counts fewer leases.

## Combined A+B+C (coordinator; merge b694994), same bench file for every row

C's version of `bench/rsx_fifo_actions_bench.py` (with `indexed=1`) built from each tree (`-O2 -march=x86-64-v3
-ffp-contract=off -g -DBENCH_CALLGRIND`), `frames=3`, recorder on, worker thread; release oracle passed in every run.
Base = `git archive de6bc4f` + that bench file; A+B = ceab1ba + that bench file.

| worker Ir/frame | de6bc4f | A+B (ceab1ba) | A+B+C (b694994) | change |
| --- | ---: | ---: | ---: | ---: |
| no draws (2000 units of state, reports, releases, surfaces, 32 turns) | 33.96 M | 24.15 M | 16.61 M | **−51.1 %** |
| 500 indexed draws (`draw_every=4 indexed=1`) | 68.49 M | 53.03 M | 43.41 M | **−36.6 %** |
| per indexed draw ((with − without)/500) | 69.1 k | 57.8 k | 53.6 k | −22.4 % |
| 250 8×8 quads (`draw_every=8`, B's stream; B's bench base 46.24 M) | — | 33.42 M | 25.40 M | −45.1 % vs 46.24 M |

Derived estimate for a heavy game frame (not measured; assumes the bench mix and that instructions scale to time): per draw
unit with its draw ≈ 17.0 k + 69.1 k = 86.1 k Ir before, 8.3 k + 53.6 k = 61.9 k after (−28 %). Applied to HIRES2's
≈ 2.9 ms + 9.1 µs/draw at ~2000 draws (~21 ms), the worker would be at **~15–16 ms** (−28 % of the per-draw part, fixed
part unchanged) — a large step, but **still above the ~13 ms target**; the PC measurement decides.

## Regression suite (lavapipe, cloud container)

**Verified:** full run on the final code tree b694994 (202 modules, 915 tests, 4 skips; 3 parallel modules,
`ANYPS3_REQUIRE_VULKAN=1`, lavapipe, `nice -n 5`; first pass cut by the 2 h tool limit after 141 modules, the remaining 61
resumed on the same tree), logs in the session scratchpad only. 18 modules failed; 17 are failures recorded on the
untouched base in [HIRES3](2026-10-28-cloud-hires3.md#regression-suite-lavapipe-cloud-container) (same tests, same
messages), and `test_native_audio` is a compile timeout shown on the base in this round, so **none is a regression of this
round**:
- environment: `test_sample_profile`, `test_sample_profile_integration`, `test_spudyn_profile`, `test_spuperf2_capture`
  (tools under `local/`), `test_present_window` (SDL offscreen driver without Vulkan), `test_ppu_fp_lazy`, `test_ppuperf3`
  (GCC 13 `-Werror=maybe-uninitialized` in `fp_fused_sum`), `test_savedata_vsh` (GCC 13 `assert` macro with 208 arguments),
  `test_rsx_residency` (GCC 13 `-Werror=unused-function` for `graphics_static_discard`);
- compile timeouts under the parallel load: `test_program` (2 × 30 s), `test_services` (30 s), `test_native_audio`,
  `test_native_audio_full` (60 s each). Re-run alone afterwards: `test_native_audio_full` OK; `test_program`, `test_services`
  and `test_native_audio` still exceed their 30 s / 60 s compile limits with nothing else running (the first two as on the
  base in HIRES3). The one `test_native_audio` test (`sdk_port_capacity_and_guest_memory_accounting`) was then run with its compile
  timeout lifted by a diagnostic driver, alternating base de6bc4f / tip: compile 64.7 / 63.5 / 66.8 / 65.7 s, the test passes
  on both — the container's compile speed, not compile-time growth of this round;
- same assertion as on the base: `test_rsx_fp_cache`, `test_rsx_sampler_identity`, `test_rsx_resident_sampler`,
  `test_rsx_perf_decode`, `test_rsx_pipeline_cache` (`test_rsx_resident_sampler` re-checked on de6bc4f in this round:
  identical `raw && *raw==*fs` assertion).
`test_rsx_drain_bound` (base failure in HIRES3) passed. Before the merges, A+B (ceab1ba) passed 21 area modules and all
71 RSX modules except the same six; agent C ran the par/memory/RSX modules on its tip (all OK).

## Risks

- `Reg` is `always_inline` at ~150 sites: larger code; check compile time and size with GCC 15 on the PC.
- The pipeline memo holds a pointer to a `graphics_pipeline_fast` element: any future erase site must bump
  `graphics_pipeline_fast_version`.
- A register block written back to its previous value between two draws now costs one redundant 256-byte state upload.
- The paired FIFO read (78dfc00) reads the published word after a header one step earlier; an observer of that word sees
  one 8-byte read instead of two 4-byte reads (`ANYPS3_RSX_FIFO_PAIR=0` restores them).
- The bench streams are shaped from HIRES2 counts, not captured from the game.

## First next actions on the real PC

1. **One integration build** of `ccr-b1a3987e-aypb5o` (b694994 + docs) and QUIET alternating 1440p walks (render scale 2,
   `ANYPS3_RSX_RECORDER=1` as in HIRES2) against the HIRES2/HIRES3 builds (1031/1034 or an HIRES3 integration build).
   Compare FPS, one-vblank share, **rsx_worker ms/frame and µs/draw in heavy frames** (HIRES2: ≈ 2.9 ms + 9.1 µs/draw), the
   main thread's RsxWait, and the `PERF GUEST_MEMORY` lines. Also a scale-1 walk.
2. **Same binary, switch A/B** (alternating, QUIET): `ANYPS3_RSX_RENDER_BATCH=0` (renderer cursor); all three memory
   switches `ANYPS3_GUEST_PAGE_READ=0 ANYPS3_RSX_RENDER_BATCH=0 ANYPS3_GUEST_RANGE_ADMISSION=0`; `ANYPS3_RSX_FIFO_PAIR=0`.
   A switch-off run that is faster or a new main-thread wait refutes that default. Agents A's and B's other cuts and 6195d31
   are unconditional (A/B only against the base build).
3. **Build health:** compile time and binary size with GCC 15 (A's `always_inline` `Reg` at ~150 sites); the native suite
   on the PC (NVIDIA + validation), especially the six RSX modules that fail in the container on the base too
   (`test_rsx_fp_cache`, `test_rsx_perf_decode`, `test_rsx_pipeline_cache`, `test_rsx_residency`, `test_rsx_resident_sampler`,
   `test_rsx_sampler_identity`).
4. **Still open from HIRES3/HIRES2:** `ANYPS3_RSX_MEMORY_POOL=0` A/B and the hitch classes; 1035 (visual MSAA) on/off;
   a visual check of `ANYPS3_RSX_RECORDER=1` for the 624b teal frames — if clean, make the recorder the default in the
   runtime and the GUI (`anyps3_manager/settings.py` `experimental.recorder`), because every 1440p figure is taken with it.
5. **If the worker is still > 13 ms** (expected from the derived ~15–16 ms): the designs listed under Findings (vertex upload
   reuse, per-unit bound-texture memo, state-block scan, FP constant rewrite), a cursor-held GET store, and HIRES3's
   two-thread worker item. The other two blockers are unchanged by this round: the main guest thread (~14.8 ms, ~3.9 ms
   SPU-job wait) and the scale-2 GPU (~15 ms).
