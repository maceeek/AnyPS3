# Sandbox analysis: current state, integration risks and the next performance work

Date: 2026-10-20. Scope: inspect the current source and existing artifacts, analyze saved samples, propose fixes.
No runtime/profile changes, builds, regression tests, new game execution, or host configuration changes in this round.
The user requested that the findings be recorded in CURRENT/progress documentation.

**Decision:** finish a small, corrected integration of the existing SPU and frame-pacing improvements before starting another
large optimization. Keep the late AVX DMA patch and the asynchronous recorder out of that first candidate.
This is a proposal, not an applied fix or a claim of 60 FPS.

## Verified checkout and artifact state

The initial tree was clean on `main` at `abf245064fd8c31990ca3fa5f96f53820feff179`.
Relative to build 472, current runtime code changes only `anyps3/runtime.py` (ZeroTable, `6ce888b`); there is also its new test.
Thus **main is not identical to snapshot 472**, and neither SPUFAST3 nor FRAMETIME is on main.
The earlier headlines in CURRENT/HANDOFF were stale.

| Tree / artifact | Verified state | What it establishes |
| --- | --- | --- |
| `native-virtual-472a` | 46.684 FPS over 60.021 s; source snapshot 472 | Baseline before the separate wave-3 candidates |
| `native-virtual-603a` | **54.045 FPS** over 60.060 s; source snapshot 603, SPU code through `87dce76` | Best of the checked steady runs without the sampling profiler; median two-second rate 55.95, range 31.5–60 |
| `native-virtual-603w` | 55.107 FPS over 60.028 s; same build 603, wall sampler enabled | Timing structure; do not rank its higher rate as a separate optimization |
| `native-virtual-642a` | 52.405 FPS over 60.030 s; source snapshot 642 | Independent frame-pacing candidate; not combined with 603 |
| `fast2/spufast3` | `b2a0544`, clean | Includes an additional AVX DMA change **not present in 603** |
| `fast2/integ473` | `7da3eb3`, clean | main + RSXCPU3 + FRAMETIME; SPUFAST3 absent; no build 473 established |

**Verified in this analysis:** all 121/121/122 source hashes for snapshots 472/603/642 match their build manifests;
their saved profiles match the build hashes. All recorded artifact hashes in the four runs above match the files now present.
The original nonempty EBOOT PT_LOAD segments were independently compared with each run's `program.elf`: all identical.
The four additional SPU overlays in the 603 profile match the original EBOOT source bytes:
`0xdc6b40`/8544 bytes, `0xdd00c0`/5344, `0xdc5840`/2400, `0xdcaf40`/992.
This check establishes their source identity; it does not independently reproduce every historical DMA relocation observation.

Each run's verifier records return code **−9**, `stop=null`: the saved measurement ended by SIGKILL, not normal game completion.
The 603a saved frame was inspected (lossless PPM-to-PNG conversion); it shows Clank in the rendered level.
This is saved-frame evidence, not a new presentation/input test.
Virtual-pad menu/load evidence comes from the existing runs. **Physical gamepad and 60 FPS with headroom remain unverified.**
The minimum/maximum FPS values above are **two-second interval rates**, not individual-frame minima/maxima.

**Verified suite evidence:** `suite-472/summary.json` contains 813 tests, no source drift and no skips, but `passed=false`;
`test_ppu_fp_lazy` is the failing module. The existing record attributes its error to a 300 s execution timeout and documents
an isolated pass and the subsequent ZeroTable correction. Neither that isolated result nor the targeted integration logs
establish a full-suite pass of a combined 473 candidate. This round did not rerun tests.

**Sandbox limit:** `/proc` exposes only sandbox processes; no `/dev/nvidia*`, `/dev/dri/*`, or `/dev/input/js*` was visible.
Consequently the prior statement “no host processes are running” was not verified here. No host game/build was restarted or killed.

## New offline result: 603w wait attribution

Evidence and repeatable reader:
[evidence.json](../../../local/ratchet/analysis-2026-10-20-sandbox/evidence.json),
[analysis script](../../../local/ratchet/analysis-2026-10-20-sandbox/analyze.py),
[attribution output](../../../local/ratchet/analysis-2026-10-20-sandbox/603w-wait-attribution.txt).

Command, from the project root:

```bash
PYTHONDONTWRITEBYTECODE=1 python local/ratchet/analysis-2026-10-20-sandbox/analyze.py \
  > local/ratchet/analysis-2026-10-20-sandbox/analysis.log 2>&1
```

This analyzes the saved capture; it does not launch the game. The window is PPU steps
`24059772944..49805918209`, using the cumulative value **before** the last thirty intervals as the lower bound.
Its 60.028 s PERF aggregate reproduces the saved summary. Sample timestamps cover 60.027 s.
The earlier `spufast3_window.py` uses `v[-30]` as its lower bound and would omit the first interval of a thirty-interval window;
use `v[-31]` for these logs after confirming that their last thirty intervals match the summary.

**Verified analysis output; derived time estimate:** 57,146 samples of guest PPU thread 1 at a 1 ms period:

| PC region | Samples | Estimated wall ms/frame | All five ig lanes sampled and classified as parked |
| --- | ---: | ---: | ---: |
| Engine-counter wait (`0x696c10..0x696e20`, `0x666040..0x666100`) | 10,549 | **3.19** | 4,807 / 10,549 = **45.6%** |
| Job-flag wait (`0x5564c0..0x556600`) | 3,477 | **1.05** | 647 / 3,477 = **18.6%** |
| Both regions | 14,026 | **4.24** | Not a direct measurement of the producer's execution time |

All five ig lanes have samples in 99.75% of engine-counter bins and 99.14% of job-flag bins; neither region has a bin
without any ig sample. Thus the “zero running lanes” observation is not mostly a missing-samples artifact.
The seven sampler files passed committed-chunk checksum, sequence and footer validation: zero dropped records and
zero incomplete trailing bytes. There were four recorded PPU timer overruns in the selected window, one inside a wait region.
The 57,146 thread-1 samples do not cover every millisecond of the window, so retain the sampling uncertainty.

Limitations: 1 ms bins cannot resolve sub-millisecond work or wakeups; host-symbol classification is approximate,
and current system-library symbols may not be a perfect historical identity match.
“Parked” means classified by the reader's sleep/futex/syscall symbol rule, not a hardware idle measurement.
These are guest-PC associations and simultaneous samples, **not proof that a specific SPU job causes each wait**.
The capture has no guest stack. Neither all engine-counter waits nor all parked intervals may be assigned to RSX.

**Derived consequence:** the ≤2 ms SPU-wait target is still not demonstrated. Further SPU execution speedups can help,
but attributing all 4.24 ms to SPU computation is unsupported. Trace the completion counter producer, publication,
owner request and PPU wakeup for the parked intervals before optimizing another arithmetic helper.
The measured SPU improvement and frame-pacing improvement overlap in these dependencies; their FPS gains cannot be added.

## Fixes to address before promoting the combined candidate

The first four RSX/timeline items below recheck existing Sol review findings against `7da3eb3`;
they are **derived from inspected code**, not newly reproduced runtime failures.
The late DMA issue is an additional review concern from this analysis.

1. **Keep `b2a0544` AVX DMA accesses out of the first merge (high priority, derived).**
   At `fast2/spufast3` `anyps3/par_memory.py:526–555`, the AVX2 path replaces GNU atomic shared-memory loads/stores with
   ordinary `_mm256_loadu_si256`/`_mm256_storeu_si256`, plus signal fences. `runtime.py:975–985` admits concurrent reads;
   the write-side `LineLocks` do not make readers acquire the same lock. A PPU atomic store can therefore overlap an SPU
   plain vector read, or a PPU atomic read can overlap an SPU vector write.
   The C++ rule still treats an unordered conflicting access with one non-atomic side as a data race; GCC's atomic builtins
   assume a race-free program. A signal fence orders a thread against its own signal handlers, not another thread.
   The x86 alignment argument in the patch does not supply the missing compiler/memory-model guarantee.
   Sources: [C++ draft, intro.races](https://eel.is/c++draft/intro.races),
   [GCC atomic builtins](https://gcc.gnu.org/onlinedocs/gcc/_005f_005fatomic-Builtins.html).
   **Fix proposal:** retain the 603 scalar atomic shared-side operations; unroll them or vectorize only the private destination.
   Permit plain vector accesses only under an established exclusion protocol covering every conflicting accessor.
   Validate concurrent DMA/PPU access, aliases, mixed access sizes and cache-line/page boundaries, with race detection where supported.
   Do not require a whole DMA transfer to be atomic: preserve the existing granularity. No observed game corruption is claimed.

2. **Finish the fragment-program read correction (medium priority, derived).**
   Main's `anyps3/rsx_draw.py:600–607` speculatively reads a 256-byte chunk while discovering an unknown program extent,
   touching bytes beyond END and potentially triggering unrelated observer work.
   RSXCPU3 fixes the extent, but `integ473/anyps3/rsx_draw.py:600–609` performs two separate 8-byte loads and catches/retries
   exceptions. If only the first half is readable, its observer side effects occur before the second half faults.
   **Fix proposal:** one admission/validation of all 16 required bytes before either load; retain the exact Range path where
   needed. Preserve constant blocks attached to END and IO-page crossings. Add the eight-readable/eight-unmapped case with
   zero observer notifications before failure, plus exact spans on successful reads. Do not swallow and replay observer errors.

3. **Count GPU VP instructions, not switch iterations (medium priority, derived).**
   `integ473/anyps3/rsx_vk_vertex.py:46–51` increments `steps_` outside the generated switch, while several guest instructions
   can execute through fall-through. CPU `run_vertex_program` counts each instruction against 65,536.
   **Fix proposal:** charge each executed instruction before its side effects, including fall-through, taken and untaken
   branches, calls and RET. Test 65,536/65,537 boundaries and a multi-instruction loop against an independent CPU expectation.

4. **Bound mid-FIFO host-request draining and test real coherence (medium priority, derived).**
   `integ473/anyps3/rsx_game_worker.py:53–73` drains until the queue is empty. Producers can replenish it while acknowledgements
   are issued; a finite queue capacity does not bound the total loop. FIFO labels and the vblank deadline can be starved.
   **Fix proposal:** service a bounded count or the initially admitted contiguous request prefix, preserving front ordering,
   barriers and recursive-drain protection; then return to FIFO/deadline service.
   Add sustained-producer progress tests, actual guarded GPU-byte materialization at callback time, and real FIFO flips with
   releases/reports on both sides. The existing GET/timing tests do not establish those contracts.

5. **Repair timeline publication before relying on exact percentile claims (medium priority, diagnostic only).**
   `integ473/anyps3/frametime.py:95–98` reserves a slot by incrementing `head` before writing its ordinary fields;
   `bench/frametime/ftring.py:34–45` accepts nonzero `ns/kind` as a committed event.
   **New verified exposure:** `ft-642a.bin` has head **9,093,360**, capacity **8,388,608**: it wrapped.
   `ft-640a.bin` did not wrap (head 7,749,446). This does **not** demonstrate a torn event, but the risk is relevant to the
   existing 86.4% single-vblank result. Its independent PERF FPS measurement is unaffected.
   **Fix proposal:** per-thread rings or a protocol with exclusive slot ownership and validated completion/generation.
   A final commit field alone does not prevent a very late writer overwriting a newer slot.
   Test termination mid-write and a delayed writer across wrap. Recompute frame percentiles from valid events.

6. **Check inline SPU WRCH interrupt equivalence (medium priority, derived divergence; game impact unproven).**
   `fast2/spufast3` `anyps3/spu_simd.py:392–399` inlines channels 16–20/22 without `t.irq_next=0`;
   the generic `spu_write_channel` sets it at `spu.py:1223`.
   `spu_block.py:70–74` also classifies these channels as ordinary instructions, with a cached interrupt limit at `:544–550`.
   Profiling ON uses the generic helper, so profiling can change this behavior.
   **Fix proposal:** preserve the generic interrupt-check boundary when interrupts are enabled, or establish and test an
   equivalent policy. Merely setting `irq_next` without updating the cached limit/block boundary is insufficient.
   Compare native/interpreted execution with a pending asynchronous event, checking handler entry, PC/SRR0 and retired steps;
   repeat with profiling off/on. This is a runtime-contract concern, not a newly established PS3 architectural timing rule.

Lower-priority derived items: gate the unconditional service timing in `integ473/anyps3/rsx_game_worker.py:200` behind its
diagnostic switch (measure the effect); reject signed/overflowing timer-slack input at `:292`.
The async recorder remains default OFF. Run 624b's teal frames remain unresolved; don't make it part of the first speedup claim.

## Prioritized performance experiments

1. **Corrected integration of already measured work.** Start from `fast2/integ473` after the fixes above, include SPU work
   through `87dce76` (the 603 source state), and retain the current ZeroTable change. Review the actual merge; do not merge the
   current SPU tip blindly because it adds the unbuilt DMA patch. Keep `ANYPS3_RSX_RECORDER=0`.
   **Hypothesis:** reduced SPU wait plus more responsive RSX owner produces a better frame deadline distribution.
   **Alternative:** both changes remove the same wait and the combined gain is small, or contention causes a regression.
   Compare against 603 and the pacing candidate on the same quiet machine; first validate targeted semantics, then a new
   SHA-baselined build and real run, then the full suite including actual Vulkan before promotion.

2. **Resolve the parked-lane completion delay.** Record low-cost timestamps for issue/completion-counter writes, producer identity,
   page-sync request enqueue/service, SPU wake and PPU observation. Restrict the capture to a short steady window.
   **Hypotheses:** delayed owner service, wakeup/scheduling latency, or a different producer/dependency.
   **Distinguishing result:** an ordered issue→producer-complete→publish→consumer-resume interval explains the delay.
   Do not eliminate a guest sleep, fabricate completion, or assume a parked lane has finished its job.

3. **Target remaining CPU rendering cost after integration.** 603a has RSX worker 13.12 ms/frame and main PPU 12.00 ms/frame,
   with main idle 5.88 ms/frame. These overlap; do not sum them as a serial frame.
   GPU VP branches and FIFO register handling already exist in RSXCPU3; test those before another recorder redesign.
   Optimize only a remaining measured stage: repeated surface bindings/watches, geometry preparation or allocations.
   Preserve guest snapshot timing and resource lifetime. Vulkan command-pool and queue ownership must remain explicit when
   moving recording between threads; see [Khronos threading guide](https://docs.vulkan.org/guide/latest/threading.html).

4. **Treat post-load warm-up separately.** The existing logs associate the slow phase with P8/P5 clocks and synchronous cold
   work; the proposed feedback loop is still a hypothesis, not proven GPU power-management causality.
   Main `anyps3/rsx_graphics.py:1050–1072` starts `flush()` with unconditional `retire(true)` and later waits for readback.
   A promising design is a resource-specific completion prefix and pooled cold allocations, retaining correct dirty-byte
   publication and ordered labels. Do not simply delete waits. Measure first-30-second FPS and time to sustained playability,
   alongside steady FPS and GPU clocks; no clock/power setting was changed here.

**Measurement contract:** alternate repeated quiet-machine A/B runs, same original save/camera, CPU affinity, warm-up and
runtime switches; no simultaneous suite/build work. Keep the sampler/stats runs separate from throughput runs.
Collect frame-time p50/p95/p99, missed-vblank fraction, steady two-second rates, cold-start time, image/input correctness,
and exact build/profile hashes. A two-second interval at 60 FPS does not establish stable 60 FPS.
From 603a, 60 FPS requires about 11% more throughput (18.50 → 16.67 ms mean interval); quantized vblank deadlines and tails
make that arithmetic an indication of scale, **not a promised optimization gain**.

## Handoff

First next action: repair the integration findings and the SPU interrupt boundary, select the measured SPU commits without
the AVX DMA addition, and run targeted tests in a fresh candidate tree. Confirm host processes and available build numbers
outside the sandbox before a new real-game run. Build 473 is only the recorded intended integration, not a validated artifact.
All future execution must remain silent (`SDL_AUDIODRIVER=dummy`, `ANYPS3_AUDIO_SINK=null`).
No background analysis task from this round remains running at completion.

Final documentation validation: 97 local Markdown links across this record, CURRENT, HANDOFF, INDEX and menu-progress
resolve; `git diff --check` passes. Artifact/source/segment/overlay assertions pass. `git diff --name-only -- anyps3 tests profiles`
is empty. The analysis artifacts remain under `local/ratchet/analysis-2026-10-20-sandbox/`; documentation is uncommitted.
