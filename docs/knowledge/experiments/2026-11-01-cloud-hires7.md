# Cloud round HIRES7: SPU job speed and RSX worker work on the draw recorder — 2026-10-31/11-01

**Author/model:** Claude (cloud container) as coordinator with two Opus subagents, H (SPU job speed) and I (moving work off
the RSX worker thread); branch `ccr-b1a3987e-aypb5o`, based on 8126bbc (HIRES6 merged). Both agents were stopped once by
an API usage limit (2026-10-31 ~20:30–21:00 UTC) and resumed from their saved state on 2026-11-01 ~06:10 UTC.
**Scope:** the user's 1440p 60 FPS goal. HIRES6 left the derived heavy-frame RSX worker at ≈ 12.5 ms (2000 draws) /
≈ 14.5 ms (2400 draws), and the main guest thread (~14.8 ms on the PC, ~3.9 ms of it spin-waiting for SPU jobs) had had no
cut in any cloud round. The container has **no game data, no `local/` tree and no NVIDIA GPU** (4 vCPU, lavapipe only): no
game build, run, FPS or frame figure was produced. Every runtime statement is a native unit test (lavapipe for RSX), a
callgrind count on a synthetic bench (instructions are not time) or a thread-CPU timing of a synthetic SPU job.

## Test environment (cloud only)

As in [HIRES6](2026-10-31-cloud-hires6.md#test-environment-cloud-only). New this round:
- **Synthetic SPU job (H, 292584f, `bench/spu7_job_bench.py`):** one SPU program through the production lifter
  (`compile_program` → `spu_block` superblocks, 4 native functions) and runtime (`spu_run`, MFC channels,
  `GuestMemory::transfer` with concurrency and the page broker) on a non-owner host thread, as a pool lane runs it. Per
  iteration (7690 SPU instructions, identical work each time, output digest printed): DMA GET 16 KiB/2 KiB/512 B/128 B, GETL of
  4 elements, PUT 2 KiB/512 B/128 B/4 KiB, PUTL of 2 elements, tag waits; a SIMD integer function called by BRSL; an FP
  function called by BISL (fm/fma/fa/fms on data where 1/16 of the words are 2^-124-class, so some products underflow); 32
  cross-function and 8 local leaf calls. Modes `time` (thread CPU ns per iteration, best of repetitions) and `count`
  (callgrind). **Caveat (verified):** valgrind has no AVX-512, so in the x86-64-v3 callgrind build every FA/FM/FMA goes
  through `spu_single_vector` (73 k of 209 k Ir/iteration), a path the PC's `-march=native` AVX-512 build does not take; the
  decision metric is therefore SPU-thread Ir **minus** `spu_single_vector` ("non-FP Ir") plus native AVX-512 timing.
  Baseline profile vs the PC split of lane 0x2000200 (build 713: 53 % generated code, 19 % DMA copy loop, 8 % dispatcher,
  6 % FP slow): generated code ~64 %, DMA ~33 % (heavier than the PC), dispatcher ~6 % incl. function prologues, FP slow 1.5 %.
- **Recorder thread figures (I):** callgrind's dump requests cover only the calling thread, so the recorder's per-frame cost
  is (whole-run dump of a 6-frame run − the 3-frame run) / 3, then the same unit formula (`$S/w7/thr.sh` in the cloud
  scratchpad). The FIFO bench runs with `ANYPS3_RSX_RECORDER=1`, as the PC's walking wrapper does.

## Changes

### Agent H: SPU job speed (merged as 7a98527)

| commit | change | switch | exactness (full argument in the code comment) | test / mutation check |
| --- | --- | --- | --- | --- |
| 292584f | bench: synthetic SPU job | — | — | — |
| 73f287a | DMA copy path: `LineLocks` collects its stripes (host line % 4096) in a 4096-bit map instead of two heap vectors + sort/unique; concurrent writes keep their spans without the HostSpan vector and its `shared_ptr` copies and bump versions through `Mapping::state`; concurrent multi-page reads go through `read_admitted`; `atomic_read`/`atomic_write` use an aligned 16-byte VMOVDQA body on x86 with AVX | — | same stripe set, locked in the same ascending order (the global multi-stripe order); same translations, permission/self-modifying-code checks and errors before any stripe, version or byte change; order stripes → version bumps (+notifications) → bytes unchanged; 16-byte aligned accesses are atomic on CPUs that enumerate AVX (Intel SDM vol. 3A 9.1.1, AMD APM vol. 2 7.3.2 — taken from the agent's reading, **not re-checked against the PDFs**), so they only allow interleavings the two 8-byte accesses allow; overlapping buffers keep the one-word interleaving | test_atomic_copy_unrolled (all alignments 0..15 × sizes 0..300 vs a byte loop, overlap patterns, LineLocks set vs a line-by-line enumeration incl. wrap at 4096, write-path versions/reservation/faults); mutants: no wrapped run, no overlap guard, version bound `<`, 16-byte load at p+8 — all killed |
| fd47db3 | FP: lanes with ordinary inputs whose round-toward-zero result is nonzero and below 2^-126 (or, for FM, zero from two nonzero inputs) get +0 and FPSCR flags 3 directly in `spu_vfp` (AVX-512) and `spu_single_vector` instead of the exact limb path | — | \|RZ(x)\| < 2^-126 ⇔ \|x\| < 2^-126 (RZ monotone, 2^-126 representable); a product of nonzero values is nonzero; `spu_single_round` returns +0 with flags 3 for these and raises no DIFF for ordinary inputs; every other unsafe lane still takes the exact path | test_spu_float (hand-written ISA constants), test_spufast2_vector (per-lane `spu_single_math` oracle over 6 × 60 000 underflow-prone groups + fixed cancellation groups); mutants: scalar/vector flag 3 → 2 killed; zero-result rule for all kinds survived the random sweep, killed after the fixed cancellation groups were added (in the commit) |
| b8e07ab | BRSL into another native function of the same source runs that function nested (`spu_chain_call`) instead of returning to `spu_step` | `ANYPS3_SPU_CHAIN=1` (0 disables at run time) | caller publishes its retired steps first (`SpuBlockScope::commit`); the callee is entered only under `spu_step`'s conditions (budget, step limit, no control exit) through `spu_native_call` and validates its own bytes; no progress, an exhausted budget, a control exit or a control-generation change returns to the dispatcher; otherwise the caller re-derives its scope (`rearm`), drops every validated region and dispatches through its local switch; nesting ≤ 16 host frames; an LS code write makes the callee miss as before | test_spu_block (mutual recursion, 80 nested guest calls, hand-counted 634 steps, exact budgets 1..13, fallback after a code write: 314 steps); mutants: no `rearm`, no `commit` — killed |

### Agent I: RSX worker work on the draw recorder (merged as c6e633d)

All cuts act only with the draw recorder on (`ANYPS3_RSX_RECORDER=1`; the runtime default is still off because of the open
624b teal-frame question — the PC's walking wrapper sets it). With the recorder off every path is the old inline code.

| commit | change | switch | ownership rule (written next to the code) |
| --- | --- | --- | --- |
| 11e880d | a batch's reset (its own command buffer, descriptor pools and cache, framebuffer pins, bound state) and the command-buffer half of a front-half begin run as queued Reset/Begin jobs on the recorder | — | jobs counted in `pending_jobs`, ring order with the draws; only frontend-only callers defer a begin, every caller that records itself syncs and begins inline; a Reset failure stops at the next sync/enqueue (`recorder_failed`) |
| 40ef023 | `graphics_submit`: the worker books the timeline value, completion and submitted state; the batch tail, upload prelude and `vkQueueSubmit` run as a Submit job | `ANYPS3_RSX_RECORDER_SUBMIT=1` (0 = inline submit) | from the submit on, the batch's command buffers, status offsets, uploads and staging belong to the recorder until `graphics_collect` settles it; every other queue user drains the recorder first (`recorder_queue_order`: inline submits, compute, resident and legacy submissions, present-resize idle wait); a poll of a batch whose Submit is still queued reads "not complete"; `submit_failed` keeps the destructor from waiting on a never-submitted fence; not deferred: batches with timestamps, marks, inline draws or a present semaphore |
| 145cc5e | the Submit job also owns the async DMA-queue prelude (the default where a transfer-only queue family exists, i.e. NVIDIA): each batch gets its own transfer command pool | — | the transfer queue is submitted to only by Submit jobs (and the GPU-loop diagnostic, which runs without the recorder); upload serial/wait fields are read only after a drain/settle |
| c659232 | plane clears (visual MSAA off) are queued Clear jobs instead of a recorder drain on the worker | — | the frontend begins (front), pins the image and keeps the bookkeeping; the paired visual clear is the next job (`lead=false`), as inline |
| 07f683d, c25ff48 | tests: queue order of an inline submission recorded before a queued one; test_rsx_perf_draw syncs before reading recorder-owned state | — | — |

`GraphicsBatch::sync()` now drains every queued job (frontend recording may touch device-wide recorder state that another
batch's queued Submit also touches); the new `settle()` drains only while this batch has queued jobs and is used by
`graphics_collect`, `graphics_reset`, `graphics_move_stats`. New TEST HOOK `ANYPS3_RSX_TEST_ASYNC_SAME_QUEUE=1`: on a device
without a transfer-only family (lavapipe) the async-prelude code path runs with the graphics queue as the "DMA queue".

## Synthetic measurements

Coordinator, one session, base 8126bbc vs merge 7a98527 (H + I), each built from a `git archive` snapshot.

### SPU synthetic job (`bench/spu7_job_bench.py`)

| build | SPU-thread Ir/iteration (x86-64-v3, callgrind, 300 iterations) | `spu_single_vector` incl. | **non-FP Ir/iteration** | `GuestMemory::transfer` incl. | `spu_write_channel` incl. (DMA) |
| --- | ---: | ---: | ---: | ---: | ---: |
| base 8126bbc | 209,483 | 72,983 | **136,500** | 40,845 | 44,606 |
| merge 7a98527 | 183,260 | 69,376 | **113,884 (−16.6 %)** | 19,968 | 23,730 |

- **Verified (callgrind):** identical to agent H's per-commit figures (the counts are deterministic); DMA path −47 %,
  `LineLocks` construction (15.9 k incl., two heap vectors + sort/unique per PUT) gone from the profile; `spu_step` +
  `spu_native_call` self 4.0 k → 1.0 k, new `spu_chain_call` 3.95 k (33 chained calls per iteration). Target −25 % not reached.
- **Native AVX-512 thread-CPU time (ns/iteration, best of 7, three alternating rounds; this session was very noisy):** base
  28,415 / 35,161 / 35,762; merge 24,308 / 18,840 / 29,594; merge with `ANYPS3_SPU_CHAIN=0` 33,643 / 24,123 / 30,646. Agent H's
  quieter session: base 21,571 (mean of 3), tip 18,961 (−12.1 %), tip with chaining off 19.8–20.7 k. **Derived:** about −12 %
  native on this job; the spread here (±20 %) does not allow a decision on the chaining switch — the PC should A/B it.
- Output digest identical for base, every commit and the tip with chaining off (native run, agent H).

### RSX worker and recorder (FIFO action bench, recorder on, callgrind, two alternating rounds)

| build | tracked per indexed draw | tracked b0 (M/frame) | **worker tracked unit** | default per indexed draw | default b0 (M/frame) | **worker default unit** | recorder unit (tracked / default, round 1) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base 8126bbc | 29,966 / 29,890 | 14.941 / 14.946 | **37,436 / 37,362** | 37,998 / 37,842 | 14.930 / 14.902 | **45,463 / 45,293** | 6,092 / 6,214 |
| merge 7a98527 | 25,902 / 25,873 | 14.529 / 14.542 | **33,166 / 33,144** | 33,837 / 33,806 | 14.502 / 14.490 | **41,087 / 41,051** | 11,299 / 11,282 |

- **Verified (bench):** worker tracked unit 37,399 → 33,155 (**−11.3 %**; per indexed draw −13.5 %, b0 −2.7 %); default unit
  45,378 → 41,069 (−9.5 %). The recorder thread takes ~5.2 k more per unit and stays at about a third of the worker. Agent I's
  own figures (each commit from its own snapshot): 37,632 → 33,392. Target ≤ 28 k not reached. The base equals HIRES6's merge
  (37,555 there; same code, different session).
- **Derived (HIRES2 calibration, default mode):** worker heavy frame ≈ **11.6 ms at 2000 draws, ≈ 13.3 ms at 2400 draws**
  (HIRES6: 12.5 / 14.5); recorder ≈ 2.4 ms at 2000 draws by the same factor. Only if instructions scale to time and the game's
  geometry is as static as the bench's; the moved driver calls (vkQueueSubmit, command-buffer resets) are likely more expensive
  in µs on NVIDIA than on lavapipe, which favours the move on the worker and costs the recorder (hypothesis).

## Findings

- **Prepare-stage design (I, derived, not implemented):** the bench frame has 556 Session-touching or guest-visible methods
  (32 surface switch + clear, 8 zcull, 230 report resets, 230 GET_REPORT, 16 back-end + 8 front-end releases, 32 turn ends).
  A stage that drained at each of them would see runs of 1.67 draws (default shape) or 3.89 (every unit a draw): no overlap.
  The design that overlaps splits the worker into D (FIFO decode, register bank, GET, semaphore acquire, flips, host requests)
  and P (`rsx_execute_draw` and every Session method: surface binds/clears, report/label deferral, retire, submit), with a
  per-draw packet (copy-on-write register-block and constant-chunk snapshot by the existing block epochs, draw state, the
  NativeRsx fields `prepared` reads). D drains P only ~32 times per frame (turn-end GET publication, immediate guest stores,
  semaphore acquires and memory a pending release targets, flips/display events, host requests, synchronous reads of Session
  state). Guest-memory reads on P are exact because between two guest-visible RSX events the CPU cannot observe the RSX's
  reads. Estimate: critical thread 33.3 k → ~26 k Ir per unit, P critical. First exact slice: `rsx_execute_draw` against the
  snapshot instead of the live state; it was not attempted because `prepared` reads the live state in hundreds of places
  and the Session methods must move in the same step to overlap at all. HIRES3 derived the same ordering rules
  ([HIRES3](2026-10-28-cloud-hires3.md), "Two-thread RSX worker").
- **Moved work costs more on the recorder (verified on lavapipe, hypothesis for NVIDIA):** isolated with
  `ANYPS3_RSX_RECORDER_SUBMIT=0`, the Submit jobs add ~2.2 M Ir/frame on the recorder for ~0.8 M saved on the worker; the
  recorder's lavapipe `vkCmd*` calls in `graphics_record_draw` cost ~60 % more per call when the same thread records, submits
  and resets (lavapipe-internal allocation/locking suspected). The trade is still right: the worker is the critical thread,
  the recorder stays at about a third of it.
- **SPU remainder (H, derived from the tip profile):** of the remaining non-FP 113.9 k Ir/iteration, ~84 k is generated code
  (the SIMD block costs ~122 Ir per 13 SPU instructions: byte swaps of the big-endian register file at block boundaries,
  per-block guards, self-store checks — representation-bound; register residency across blocks was refuted in FAST2 wave 2),
  ~23.7 k DMA (8.2 k of it the copies; per-line stripe locks and touch counters are required by the reservation protocol
  unless DMA writes adopt the ScalarWrite hazard protocol for untracked lines — candidate, needs its own design and review),
  ~6 k dispatcher and chaining.

## Coordinator review (before the merges)

- **I, submit move:** every `vkQueueSubmit`/`vkQueueWaitIdle` site in `anyps3/` was checked: compute
  (`rsx_vk_compute.py`), resident (`rsx_vk_resident.py`), the legacy path (`vulkan.py`, under `ANYPS3_RSX_VK`), present resize
  (`rsx_vk_present.py`) call `recorder_queue_order()` first; presentation waits on a present-semaphore batch, which is never
  deferred and drains before its inline submit; the GPU-loop diagnostic runs without the recorder. Timeline signal values
  therefore reach the queue in increasing order. The destructors' `vkDeviceWaitIdle` calls only wait for submitted work.
- **I, Clear job:** taken only with visual MSAA off (no `graphics_ms_prepare` / working-image branch), and records the same
  `graphics_group_lead` + `vkCmdClear*` as the inline path on the recorder-owned state.
- **H, chaining:** `spu_step` samples interrupts only at its own start and after channel instructions in the interpreter, not
  between native entries (`while(spu_native_enter(...))`), so a nested native call skips no interrupt sample; the entry
  conditions match `spu_step`'s loop; the callee validates its own bytes as in `spu_native_enter`; the continuation drops all
  validated regions (`vm=0`). Generated code grows by one call sequence per chained BRSL site (compile cost noted as a risk).
- **H, DMA path:** the write path keeps the old order (all checks and translations → stripe locks → version bumps and
  notifications → bytes with `publish_cpu_write`); `LineLocks` keeps its constructor for `std::vector<HostSpan>`, so its RSX
  users (par_gpu, par_pages, rsx_graphics_codec) are unchanged apart from the cheaper set collection.

## Regression tests

- **Agents (before the merges):** H ran 20 SPU modules on its tip plus test_atomic_copy_unrolled, test_spu_float,
  test_par11_memory, test_par13_memory, test_guest_memory_guard on the final code of each cut (all OK); I ran ~40 RSX modules on
  c25ff48 with the recorder on and off and the clear/submit subset on its tip, plus test_rsx_async_upload through the new
  same-queue hook (all OK).
- **Full suite** (203 modules, one process per module, 3 in parallel, `git archive` of the merge 7a98527; covers HIRES6 and
  HIRES7; `local/` absent as before): **179 OK, 24 failing, no regression found.**
  - **20 fail exactly as in HIRES5** (same failing tests and the same error lines, compared mechanically): the 17 HIRES4 modules
    (GCC 13 `-Werror` cases, the five lavapipe-sensitive RSX cache modules, SDL offscreen without Vulkan, missing
    `local/ratchet` tools, compile timeouts of `test_native_audio`, `test_program`, `test_services`), and the compile timeouts
    of `test_spufast_blocks` (900 s limit) and `test_vulkan` (60 s); `test_native_audio_full` again fails only by compile
    timeouts (one subtest instead of three).
  - **`test_rsx_gpu_work`** (HIRES6's module): its static check that every `vkCmd*` name in `anyps3/` is classified for the
    barrier dedupe read the wildcard `vkCmdClear*` in agent I's new Clear-job comment as a command `Clear`. Fixed by naming
    both commands in the comment (d1177e9, no generated-code change); the module then passed on a snapshot of d1177e9.
  - **Load-dependent, re-run alone:** `test_rsx_perf` (90 s compile limit at setup) OK alone in 81 s;
    `test_spuperf2_simd` (compile limit) OK alone in 80 s; `test_spu_pool_poll` (a spinning lane must get > 50 % CPU of wall
    time; failed at load ~5–6) OK alone in 76 s. `test_spufast_blocks` timed alone with the limits lifted
    (`integ/time_audio_compile.py`, base 8126bbc then merge, under the HIRES8 agents' load): its four compiles 503 / 898 / 264 /
    398 s on the base vs 474 / 892 / 260 / 372 s on the merge, module OK on both — SPU chaining does not lengthen this compile
    measurably; the native-ISA compile sits at its 900 s limit on the base as well.
  - Two HIRES5 timeouts now pass: `test_guest_memory_rsxmem`, `test_guest_printf`.
  - The suite ran next to the HIRES8 agents for its last ~1.5 h (load average ~5–6 on 4 vCPUs).

## Risks

- **16-byte atomicity (derived):** rests on the vendor guarantee for AVX-enumerating CPUs (not re-checked against the PDFs
  here); every PC build uses `-march=native` on an AVX-512 CPU.
- **Chaining (derived):** up to 16 nested host frames of generated SPU functions on the lane stack (std::thread default 8 MiB);
  one extra call sequence per chained BRSL site in the generated code (compile time of the game's units not measured).
- **FP underflow cut (hypothesis):** helps only if the game's slow lanes are mostly subnormal-nonzero results or zero FM
  products; the PC profile does not say which lanes leave the fast path.
- **Recorder moves (derived):** active only with `ANYPS3_RSX_RECORDER=1`; the async-prelude Submit path was tested only through
  the same-queue hook (two real queues only on the PC); errors of moved work surface at the next sync/enqueue (same message);
  `sync()` now also waits for other batches' queued jobs (host timing only); the 624b teal-frame question is untouched while
  these cuts add work to the recorder path.
- Instructions and synthetic timings are not game time; nothing here was measured on the PC.

## First next actions on the real PC

1. One integration build of `ccr-b1a3987e-aypb5o` (HIRES3–HIRES7); QUIET alternating 1440p walks with
   `ANYPS3_RSX_RECORDER=1` vs 1031/1034: rsx_worker and rsx-record ms/frame and µs/draw in heavy frames, main-thread ms and
   its SPU wait, GPU busy ms/frame; check the image with the recorder on (teal-frame question).
2. Same binary: `ANYPS3_RSX_RECORDER_SUBMIT=0` (submit inline) and `ANYPS3_SPU_CHAIN=0` (SPU chaining off), alternating.
3. With `ANYPS3_WAKETRACE` or the sampler: lane 0x2000200 ms/frame, the main thread's SPU wait (engine counter + job flag),
   the lane's shares of `GuestMemory::transfer` / `LineLocks` / `atomic_*`, `spu_vfp_slow`, `spu_chain_call`.
4. `python bench/spu7_job_bench.py --march native` on 8126bbc and on the tip (cloud: see the table above).
5. `tests.test_rsx_async_upload` and `tests.test_rsx_recorder` once on the NVIDIA device (real DMA queue), plus the HIRES6
   items (marks walk, `ANYPS3_RSX_BARRIER_DEDUPE=0`, `test_rsx_visual_msaa` on NVIDIA).
