# Cloud round HIRES5: per-draw geometry data and state path — 2026-10-31

**Author/model:** Claude (cloud container) as coordinator with two Opus subagents, D (geometry data path) and E (state
path); branch `ccr-b1a3987e-aypb5o`, based on 990c799 (HIRES4 code b694994 + its record). **Scope:** the user's 1440p
60 FPS goal. HIRES4 left the RSX worker at a derived ~15–16 ms in heavy frames (target <= ~13 ms) and measured the per-draw
cost on the concurrent FIFO bench at 53.6 k worker instructions per indexed draw; this round targets that per-draw cost.
The container has **no game data, no `local/` tree and no NVIDIA GPU** (4 vCPU): no game build, run, FPS or frame figure
was produced. Every runtime statement is a native unit test on **lavapipe** or a callgrind count on a synthetic bench
(worker thread instructions, `-O2 -march=x86-64-v3`); instruction counts are not time. The real PC must build and measure
alternating QUIET runs before any FPS claim.

## Test environment (cloud only)

As in [HIRES4](2026-10-30-cloud-hires4.md#test-environment-cloud-only): lavapipe, GCC 13.3 with the scratch `c++` shim,
audio forced off (`SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`), one process per test module, `nice -n 5`.
Measurement: `bench/rsx_fifo_actions_bench.py` (real RSX worker, concurrent memory + page broker, recorder on, lavapipe
`LP_NUM_THREADS=1`), callgrind `--separate-threads=yes`, worker-thread dump, 3 frames after 2 warm; `d4i` =
`frames=3 draw_every=4 indexed=1` (500 indexed draws per frame), `b0` = `frames=3` (no draws); **per indexed draw =
(d4i − b0) / 500**. Run-to-run noise of these counts ~0.3 %.

## Changes (each commit has its own native test with an independent oracle and mutation checks)

### Agent D: geometry data path (merged as 1c102a6)

| commit | change | switch (A/B) | test (independent oracle) |
| --- | --- | --- | --- |
| 09bdae1 | Adjacent index ranges (one list split into <= 256-index `DRAW_INDEX_ARRAY` words) read with **one** transfer and decoded in one pass; any exception re-runs the unchanged per-range loop from the start (same error at the same range). AVX2 BE16 decode (16 indices per step, restart lanes masked for min and max). The decode returns the unrestarted minimum, which replaces the second `index_extent` scan in `IndexBounds` (recorded only for a restart-free triangle list whose count is a multiple of 3) | — (exact) | `test_rsx_index_decode` (4 builds: baseline, native, `-msse4.1`, `-mavx2`; restart lanes, random lists vs the per-element definition); `test_rsx_perf_draw` `merged_index_ranges` (triangle lists + extent vs lists computed in the test; error parity incl. "range 2 unmapped, range 3 outside VRAM") |
| 2783f8a | **Vertex input reuse:** `Session::read_inputs` keeps a host copy of each single-mapping vertex input group, keyed by guest address and size, pinned to (host pointer, backing, mapping identity) of the Range the draw creates anyway; validated by a CPU-dirty watch registered and consumed before the copy is read | `ANYPS3_RSX_GEOMETRY_REUSE=0` | `test_rsx2_draw` `geometry_reuse_` (analytic rectangle per image: repeated draw = hits; PPU stores, foreign-thread transfer (SPU PUT path), `ClaimTransfer` GPU write, RawPermit raw-pointer write, a write between two draws of one batch, a surface made GPU-authoritative over the span, unmap+map, rewrite limit, format change, switch off) |
| 17029c0 | The published CPU-dirty registry also indexes all watched spans (sorted, running maximum of ends): `publish_cpu_write_watched` / `publish_gpu_write` mark overlapping watches in O(log n + hits) instead of testing every watch | — (exact) | `test_par11_memory` (600 watches, 4000 random stores/transfers/GPU publications, every watch's bit vs the interval overlap computed in the test) |
| fd2007d | **Index list reuse:** an unchanged contiguous index span serves its decoded list, valid only for the same width, restart, restart index and base index; a use creates the span's translation Range first (map/permission check, observer coherence point), then consumes the dirty bit | same switch | `test_rsx2_draw` `index_reuse_` (uploaded triangle list vs the list computed from the stored values: PPU, transfer, GPU claim and raw writes, write within a batch, base change and back, 32-bit indices at the same address, restart, unmap+map, switch off) |
| a77bf15 | Bench: `dynamic=1` (main thread rewrites all 400 vertex positions before every frame); default streams unchanged | — | diagnostic |
| cfca6a9 | `PERF RSX_COUNTERS` `geometry_hits/misses/rereads`, `index_hits/misses` | — | — |

### Agent E: state path (merged as 8a03614)

| commit | change | switch (A/B) | test (independent oracle) |
| --- | --- | --- | --- |
| cfc3b8c | `bind_draw_blocks`: the 49 state slots compared by a branchless version mask instead of building a 49-entry descriptor array per draw (same slots, order, bytes, upload/hit counts; a version 0 still reaches the stop); `refresh_registers` visits only fresh, exposed or epoch-moved blocks | — (exact) | `test_rsx_graphics_cache` `state_slots_` (upload/hit counts per change kind; all 2048 uploaded register words vs bank and the RPCS3 reset table) |
| 6c1b92f | `rtt_image` first looks the texture's first byte up in `surface_watches` (the exact union of every member surface's physical spans, rebuilt after every insertion/removal) | `ANYPS3_RSX_RTT_PREFILTER=0` | `test_rsx_rtt` `owner_lookup_order` (texture sampled before its surface exists, after a draw creates it (read-back pixel), unrelated bytes, after removal) |
| 23035d2 | `fragment_source` key in one pass over the units; tracked constant chunks found by a branchless mask | — (exact) | `test_rsx_perf_draw` `fragment_keys`; `test_rsx_constant_chunks` |
| 549f94d | `texture_image` lookup memo per identity slot, valid while `raw_revision` is unchanged (every raw-cache change that the scan reads bumps it); the CPU-dirty check still runs on every call | `ANYPS3_RSX_TEXTURE_IMAGE_MEMO=0` | `test_rsx_perf_draw` `texture_writes` (all 8×8 pixels vs the colour the test wrote: CPU texel writes, write back to the old value, offset change, same-value store) |

## Synthetic measurements (worker thread instructions; diagnostic, not game evidence)

### Per agent (each against its own base build of 990c799)

| agent / build | d4i M Ir/frame | b0 M Ir/frame | per indexed draw |
| --- | ---: | ---: | ---: |
| D base 990c799 | 43.446 | 16.620 | 53,651 |
| D 09bdae1 (index cuts) | 41.199 | 16.597 | 49,206 (−8.3 %) |
| D 2783f8a (+ vertex reuse) | 39.346 | 16.597 | 45,497 |
| D fd2007d (+ index reuse) | 37.638 | 16.614 | 42,047 |
| D tip, `ANYPS3_RSX_GEOMETRY_REUSE=0` | 41.323 | 16.598 | 49,450 |
| D tip `dynamic=1` (positions rewritten every frame), reuse 1 / 0 | 39.093 / 41.340 | 16.598 | 44,990 / 49,484 |
| E base 990c799 | 43.394 | 16.646 | 53,496 |
| E tip 549f94d | 42.454 | 16.613 | 51,682 (−3.4 %) |
| E base / tip, game-shaped (no-exposure diagnostic, see Findings) | 39.901 / — | 16.583 / — | 46,636 / 44,885 (23035d2) |

Writer cost of the dirty registry (D, wall clock, `GuestMemory::store` of 4 bytes into a page holding watched bytes elsewhere;
pages without watched bytes 19–21 ns): 600 watches 1632 → 37 ns, 64 watches 144 → 34 ns, 1 watch 31 → 30 ns (17029c0).

### Combined D + E (coordinator, merge 1c102a6, one bench build; base = agent E's 990c799 binary re-run in the same session)

Verified, callgrind worker thread; the release-value oracle passed (24/24) in every run; logs and dumps in the cloud
scratchpad `h5/` (not preserved).

| run (merge 1c102a6 unless noted) | d4i M Ir/frame | b0 M Ir/frame | per indexed draw | vs base |
| --- | ---: | ---: | ---: | ---: |
| base 990c799 (re-run) | 43.370 | 16.640 | 53,459 | — |
| **defaults** (reuse, prefilter and memo on) | **36.835** | 16.609 | **40,453** | **−24.3 %** (frame −15.1 %) |
| `ANYPS3_RSX_GEOMETRY_REUSE=0 ANYPS3_RSX_RTT_PREFILTER=0 ANYPS3_RSX_TEXTURE_IMAGE_MEMO=0` (exact cuts only) | 40.803 | 16.609 | 48,390 | −9.5 % |
| defaults, `dynamic=1` (all positions rewritten before every frame) | 38.498 | 16.609 | 43,778 | −18.1 % |
| game-shaped base (no-exposure diagnostic of 990c799) | 39.931 | 16.564 | 46,734 | — |
| **game-shaped defaults** (no-exposure diagnostic of 1c102a6) | **33.256** | 16.588 | **33,335** | **−28.7 %** (frame −16.7 %) |

The two agents' savings add up almost exactly (D −11.6 k, E −1.8 k on their own bases; combined −13.0 k); the no-draw
frame is unchanged (16.6 M). **Derived estimate for a heavy game frame** (HIRES4's method, not measured: per draw unit
8.3 k between-draw Ir + the per-draw cost, scaled from HIRES2's ≈ 2.9 ms + 9.1 µs/draw at 86.1 k Ir per unit): 8.3 k +
40.5 k = 48.8 k Ir per unit → ≈ 5.15 µs/draw → **≈ 13.2 ms at ~2000 draws**, i.e. at the ~13 ms target **only if** the
game's geometry is as static as the bench's and instructions scale to time (both hypotheses); with every buffer rewritten
(`dynamic=1`) ≈ 13.9 ms, with the switches off ≈ 14.9 ms (HIRES4: ~15–16 ms). Unchanged by this round: main guest thread (~14.8 ms in HIRES2,
~3.9 ms of it SPU-job wait) and the scale-2 GPU (~15 ms), both also above the target.

## Findings

- **Bench artifact (verified on the bench, derived for the game; E):** the FIFO bench's setup writes the register bank and
  the transform program/constant arrays through mutable `operator[]` before the worker starts, which marks 11 register
  blocks and both shader arrays exposed for the bank's lifetime. Every draw then takes the exact compare paths (register
  block memcmps ~1.0 k, constant chunk memcmps ~2.0 k, whole-array memcmp in `RsxShaderArray::version()` ~1.8 k Ir/draw).
  A code search finds no such borrow in the runtime (only `render_demo.py`; FIFO writes use `write()`), so the game takes
  the tracked paths. The diagnostic `noexpose.py` (cloud scratchpad, never committed) recompiles the same generated
  `bench.cpp` with exposure disabled; valid only because all borrows happen before the worker starts. A bench mode that
  writes through `registers.write()` would measure the game shape directly (not added).
- **Dirty-watch writer cost (verified, D):** before 17029c0 every PPU/`GuestMemory::store` into a page with watched bytes
  tested every registered watch (1.6 µs per store at 600 watches; the raw texture cache alone allows 512). This existed
  before HIRES5 and would have doubled with the geometry entries; now O(log n).
- **Remaining per-draw cost (derived from the agents' profiles):** data path ≈ 6.3 k of 42.1 k at D's tip (three
  `GuestMemory::range` creations ≈ 0.64 k each are the largest item); state path on the game-shaped tip: `bind_draw_blocks`
  1.6 k (mostly ~9 genuine 256-byte block uploads), `serialize_draw_registers` 1.3 k, `texture_metadata` 0.85 k,
  `fragment_source` 0.79 k, `texture_image` 0.62 k, `graphics_draw` front half ~2.6 k. Each remaining idea is below
  ~1.5 % per draw; both agents stopped under the task's stop rule. Hypothesis, not done: every transform-constant upload
  stores the raw registers 0x1efc/0x1f00–0x1f7c, so register blocks 30/31 are rebuilt and re-uploaded on every draw that
  changed constants (~0.6–0.7 k/draw in the game) — excluding them needs proof that no generated shader reads those words.

## Coordinator review (before the merges)

- E: `bind_draw_blocks` checked against `graphics_state_blocks` (rsx_vk_graphics.py: no per-call work besides the slot range
  check; an invalid version still reaches the stop via the single-slot call; all 49 slots are real blocks since
  `constant_chunks` = 15). RTT prefilter: `surface_watches` is rebuilt from scratch from the members' `physical` spans
  after every insertion/removal, and `physical` is only written on surfaces before insertion (`prepare_protection` callers:
  the local in `bind`, `*created` in the blit path). Texture memo: `raw_dirty` is sticky (a consumed watch sets `invalid`
  and bumps `raw_revision`), so the full scan after a failed memo check sees the same invalid entry; `trusted` is only
  written on entries under construction.
- D: the registry index marks exactly the watches `CpuDirtyWatch::overlaps` marked (half-open spans, running maximum);
  every renderer-side guest writer that invalidates the raw texture cache explicitly (label publication, surface commits,
  edge writes, alias commits) writes through `ClaimTransfer`/`GpuPages::commit`/`SurfaceBytes`, which publish
  `publish_gpu_write` on concurrent memory, so the geometry watches see them; geometry reuse is active only on concurrent
  memory. A hit still creates the Range: `GuestMemory::range` runs the observer in its Lease, and the HIRES4 admission fast
  path (`range_admitted`) steps aside when a surface watch makes the observer synchronous, so a GPU-owned surface over the
  span is retired (and republished) before the bit is consumed (D's case (f)). `read_inputs` keeps `DrawReadBatch::read`'s
  per-input `Range::read` order on misses. The map barrier and guard faults clear the raw cache but not the geometry
  entries; the geometry entries do not rely on HostGuard protection, and a remap never matches their pinned identity.

## Regression tests (lavapipe)

- **Agents (before the merges):** D ran 29 area modules on its branch (all OK); E ran 34 modules on its branch (29 OK, and the
  five modules that fail identically on the base in this container: `test_rsx_fp_cache`,
  `test_rsx_sampler_identity`, `test_rsx_resident_sampler`, `test_rsx_perf_decode`, `test_rsx_pipeline_cache`, same
  assertion on 549f94d and on `git archive 990c799`).
- **Coordinator, merged snapshot 1c102a6, one module at a time:** `test_rsx_perf_draw` (both agents' cases, 15 PASS),
  `test_rsx2_draw`, `test_rsx_graphics_cache`, `test_rsx_index_decode` (4 compiler variants), `test_rsx_rtt`: all OK.
- **Full suite (202 modules, `git archive` of 1c102a6, 3 processes): still running at the time of this commit** (54 of 202
  done at 10:47 UTC, no assertion failure). Five modules failed so far, all with `TimeoutExpired` on a test's own compile
  limit: `test_guest_memory_rsxmem` (30 s), `test_guest_printf` (60 s), `test_spufast_blocks` (900 s),
  `test_native_audio`, `test_native_audio_full` (60 s; both also in HIRES4). `test_guest_memory_rsxmem` passed in HIRES4,
  so its compile was timed with the limit lifted, alternating under the same load: base 990c799 30.4 / 27.0 s, merge
  33.4 / 27.4 s, both OK — the compile sits at the 30 s limit in this container (the coordinator's timing runs added a
  fourth process during `test_guest_printf` and part of `test_spufast_blocks`). Every timed-out module is re-run alone
  after the suite; the result replaces this paragraph.

## Risks

- **Geometry static-ness (hypothesis):** about −7.5 k of D's −11.6 k Ir/draw is reuse; it depends on the game drawing
  unchanged index/vertex bytes. Skinned/animated buffers rewritten every frame fall back to the direct path after their
  second rewrite (bench `dynamic=1`: still below reuse off).
- **PPU store cost on watched pages (hypothesis):** stores into a page that holds cached geometry take the inline medium path
  for as long as the entry lives, also when the watched bytes themselves are never written (no rewrite limit then).
  Measured only for `GuestMemory::store`, not for translated PPU code.
- **Pinned backings (derived):** geometry entries keep their guest backings alive until evicted (<= 512 entries, <= 64 MiB,
  LRU, sweep of invalidated or unused entries).
- **Shared exposures (derived):** raw-pointer writes are invalidated through `Session::host_access`, which returns early while
  the session is busy (same as the raw texture cache); index hits assume only the renderer takes GPU page claims (as the
  existing vertex Range path).
- **Future code (derived):** a new `Session::surfaces` insertion without `refresh_surface_watches` would break the RTT
  prefilter; an in-place change of a stored raw-cache entry without a `raw_revision` bump would leave the texture memo stale.
- Instructions are not time; nothing here was measured on the PC.

## First next actions on the real PC

1. One integration build of `ccr-b1a3987e-aypb5o` (HIRES3 + HIRES4 + HIRES5); QUIET alternating 1440p walks vs 1031/1034:
   rsx_worker ms/frame and µs/draw in heavy frames, main-thread ms and RsxWait.
2. Same binary, alternating: `ANYPS3_RSX_GEOMETRY_REUSE=0` vs 1. A faster reuse-off run, or a main-thread ms increase with
   reuse on, refutes the default. With a `-DANYPS3_RSX_PERF_COUNTERS` build: `PERF RSX_COUNTERS geometry_hits/misses/rereads,
   index_hits/misses` (the game's hit rate) and `PERF PPU_INLINE medium_stores/slow_stores` reuse 1 vs 0 (writer cost);
   `PERF GUEST_MEMORY` slow_leases.
3. `ANYPS3_RSX_RTT_PREFILTER=0 ANYPS3_RSX_TEXTURE_IMAGE_MEMO=0` vs defaults (expected identical images; worker slightly faster
   with the defaults, more so with many retained surfaces).
4. Visual check of animated/skinned geometry with reuse on; then the HIRES4/HIRES3 switch A/B runs still open.
