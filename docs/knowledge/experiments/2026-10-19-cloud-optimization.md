# Cloud optimization round — PPU, SPU, RSX host cost (2026-10-19 evening, Claude Opus 5.5)

User goal (2026-10-19): **60 FPS with headroom on a modern PC**, and in future at least all Ratchet & Clank
games on PS3. Task of this round: "maximum optimization of all PPU, SPU and RSX components within the limits of the
cloud environment". Starting point: commit `43c6b27` (= snapshot 460, **22.59 FPS** in `great_clock_a` on the user's
PC, see the [FAST record](2026-10-19-fast-wave.md#state-at-the-end-of-the-session-2026-10-19-2200-and-the-first-next-actions)).
Subagents: Sonnet only (user instruction); one SPU agent in an isolated git worktree.

## Environment limits (verified)

- Cloud container: 4 vCPU `Intel(R) Xeon(R) Processor @ 2.80GHz`, no GPU (Vulkan = Lavapipe, no `depthBounds`),
  **no game data** (`local/ratchet/` contains only `codex/`, 151 MB). No game build, no game run and **no FPS figure**
  is possible here. Every number below is a synthetic micro-benchmark on this CPU; relative only.
- Toolchain: the system GCC 13 has no `[[gnu::musttail]]` (required by the PPUFAST emitter). GCC 15.3 from conda-forge
  in `/opt/gcc15`, wrapper `/usr/local/bin/g++-15` (sysroot `/`, host include/lib paths, static libstdc++/libgcc,
  appends `-lSPIRV-Tools-opt -lSPIRV-Tools` after `-lglslang` because Ubuntu's glslang is static); `c++` points to it.
  perf: `/usr/lib/linux-tools-6.8.0-147/perf`. PowerPC encodings: `llvm-mc --triple=powerpc64-unknown-linux-gnu`.
- **Verified (toolchain pitfall, fixed in the runtime):** the conda-forge libstdc++ is built against an old glibc
  sysroot without `pthread_cond_clockwait`, so `std::condition_variable` waits on `system_clock` and converts a
  steady time point. `wait_until(steady_clock::time_point::max())` overflows into the past, times out at once and
  the RSX owner thread (`rsx_worker.py` `RsxOwnerQueue::launch`) spun at 100% CPU without serving the queued
  request (gdb: main thread in `RsxOwnerQueue::await` from `rsx_worker_start`, owner thread in
  `steady_clock::now`/`system_clock::now`). test_par6 hung in 6 of 8 cases. The owner loop now waits without a
  timeout when there is no deadline, like `par_worker.py` already did for SPU lanes. Same semantics; it matters
  for any toolchain whose condition variable lacks a steady-clock wait. **Verified:** test_par6 8/8 after the fix
  (6/8 hang on a worktree of `43c6b27`); test_par4, test_par9_coordinator, test_par10_deadlock and test_par14_deadlock,
  which the SPU agent had reported as pre-existing timeouts, pass serially as well.
- Lavapipe executes command buffers synchronously on the submitting thread, so RSX wall time per draw includes
  driver rasterization; host code is separated with perf symbol attribution (`anyps3`/libc vs `libvulkan_lvp`).

## Changes

Status: **verified** = by the named native regression test with an independent expectation (literal values worked
out by hand, the architecture definition, or the exact implementation as oracle for a differential test);
**derived** = by reasoning/code reading only; nothing here is verified in the game.

### PPU

1. **VMX host SIMD** (`anyps3/vector.py`, commit `1f2e4b2`). SSE/SSSE3/FMA paths for vaddfp/vsubfp/vmaddfp/vnmsubfp,
   vrefp/vrsqrtefp, vmaxfp/vminfp, vcfsx/vcfux, float compares, vperm, vsel, vsldoi, merges, vadduwm/vsubuwm and logic.
   Float paths only in Java mode (VSCR NJ=0), host MXCSR `0x1f80` and finite operands; everything else stays in the
   exact per-lane code (`vmx_*_exact`, cold). **Verified:** `tests/test_vmx_simd.py` (literal IEEE values + 200k-vector
   differential against the exact lanes; SSE2, `-march=native` and `ANYPS3_VMX_FORCE_SCALAR` builds).
2. **rlwinm/rlwnm/rlwimi with MB>ME** (`anyps3/flow.py`, `1f2e4b2`): **semantic fix.** PowerPC Book I ROTL32 duplicates
   the rotated word into both halves and MASK(MB+32, ME+32) wraps, so the upper word is not zero. The previous lifter
   produced zero in the upper half. cntlzw/cntlzd use `__builtin_clz*`. **Verified:** `tests/test_ppu_rotate_count.py`
   (red on the previous lifter). **Limitation:** the Book I PDF (`local/reference/cell-docs/`) is not in this
   container, so no PDF page is cited here; look up the rlwinm/ROTL32/MASK pages before quoting this as a manual fact.
3. **Lazy FR/FI/FPRF** (`anyps3/floating.py` `fp_lazy*`, `fp_materialize`; `PpuContext::fp_lazy_*` in `threads.py`).
   With XX already set, round-to-nearest (FPSCR RN=0b00), NI clear or set (not in the mask) and the five exception enables
   VE/OE/UE/ZE/XE clear (`(fpscr&0x020000fb)==0x02000000`), an add/mul/div/sqrt/fused operation on in-range
   operands (zero or normal, exponent in [-400,400], no zero divisor, no negative sqrt; binary32 range and midpoint
   guards) cannot change any sticky bit, so only FR/FI/FPRF change. The host result is returned at once and the last
   operation is recorded; its exact FPSCR update is replayed (`fp_materialize_pending`, cold) before any observer:
   mffs (`fp_read_fpscr`), mcrfs/mtfs*, compares, conversions, estimates, frsp, VMX float scopes, any exact arithmetic,
   and `anyps3_entry` exit (also on STOP). Rc=1 forms read only FX/FEX/VX/OX, which a lazy operation cannot change.
   The pending state lives in `PpuContext`, so it travels with the thread on a context switch.
   **Verified:** `tests/test_ppu_fp_lazy.py` — 300k random chains (special values, out-of-range, stale FR/FI/FPRF,
   directed rounding, NI) compare result and FPSCR with the eager helpers; a translated program with literal FPSCR
   values (`0x02024000` after an inexact fadd, `0x02012000` after fcmpu keeps C from a −0 result, exit `0x02024000`).
   **Limitation (verified by benchmark):** while XX is still clear the lazy path never applies and a lifted FP
   operation is ~13% slower than before (extra mode check). XX is sticky; **derived:** a game sets it with its first
   inexact result and rarely clears it, so the steady state is the fast case.
4. **Spin hints** (`ppu_emit.py` `SPIN_HINTS`): `or n,n,n` with n in {1,6,28,29,30,31} (Cell PPE priority/delay hints,
   architecturally no-ops) issue one host `pause` per block that contains them. The level's main thread spends ~28% of
   its PPU time in such loops ([FAST record](2026-10-19-fast-wave.md#critical-path-the-main-thread-waits-for-the-spu-sampler-380load--eboot-disassembly)).
   **Derived:** frees the SMT sibling/power budget while the guest waits for SPU jobs; the guest condition is polled
   with at most one `pause` of extra latency per loop iteration. Guest steps are unchanged. **Verified:**
   `tests/test_ppu_spin_hint.py` (5002 steps, registers unchanged, exactly one hint in the loop block).
5. **Same-page indirect dispatch** (`ppu_emit.py`): bclr/bcctr whose target is in the current 4 KiB page jump to the
   page's own switch (`ppu_dispatch_local`) instead of returning to the dispatcher; an untranslated target still
   stops with the exact PC/steps. **Verified:** `tests/test_ppu_local_dispatch.py` (6005 steps of the cross-page call
   loop; missing block → pc 0x10048, steps 3).
6. **lvx/16-byte loads** (`par_memory.py` `atomic_read16`, `ppu_memory.py`): two 8-byte atomic loads into one 16-byte
   store instead of byte-wise copies, used only for 8-aligned 16-byte reads. Removes a store-forwarding stall
   (two 8-byte stores followed by a 16-byte load). **Derived** equivalence (same bytes, each 8-byte half single-copy
   atomic as before); no dedicated test, covered by test_vector, test_ppu_block and test_par*.

### SPU

1. **Unbound image stop** (`spu.py` `spu_step`, `1f2e4b2`): a thread that `spu_prepare` never bound stops with
   "unknown native SPU image" (restores `test_par1`/`test_par2`, the SPUFAST 421 regression).
2. **Pool-lane slice 4096** (`par_worker.py` `NativeSpuWorker::lane_budget`, Sonnet SPU agent): a pool lane
   free-runs between coordinator ticks; requests of at least the PPU tick budget (1024) now run 4096-instruction
   slices (smaller requests stay exact for stepping tests). The lane loop reads the host clock only with
   `ANYPS3_PERF_REPORT=1`. **Verified:** `tests/test_spu_pool_quantum.py`, test_par2 (its `worker_ns>0` assertion
   now expects 0 with the report off). Agent micro-benchmark: empty lane loop 103 → 59 ns per quantum, AOT compute
   loop +3.4% instructions/s.
3. **Exact reservation-lost filter** (`par_wait.py` `ReservationNotifications::touched`, `spu.py`
   `spu_collect_events`): `notify()`, which every version bump (`bump_scalar`, `bump_parallel`) and map-epoch change
   already calls, also increments one of 4096 cache-line-padded per-line-hash counters or a global counter. An SPU
   samples the counters before a successful `reservation_token` compare and skips the full compare (reader
   admission, stripe lock, tracking lookup) while they are unchanged for the same granule and memory. **Derived
   (main-agent review):** exact — the writer bumps the version under the stripe lock and publishes afterwards, the
   reader samples before its locked compare, so in every interleaving either the compare sees the new version or the
   next sample differs; collisions only force a full compare. A line covered by a synchronous host-access observer
   (RSX/GPU coherence materializes deferred writes during the compare) always takes the full compare.
   **Verified:** `tests/test_spu_reservation_filter.py` (every writer kind, same-bucket/neighbour lines, skewed
   granules, map epoch, observers, concurrent oracles; fails with a filter that ignores the counters),
   test_par9_coordinator (now expects one compare for an unchanged unobserved line, +10 with an observer).
   Agent micro-benchmark: held reservation without writer 70.7 → 10.1 ns per check; writer side unchanged.
   Limitation: a future version bump that bypasses `notify()` would break the filter silently (test_par4 pins that
   every current bump notifies).
4. **Busy-poll parking** (`spu.py` `spu_poll_note`, `spu_lane_prepare_wait`; pool lanes only): a lane that repeats
   GETLLAR of a line whose reservation is still intact, or `rchcnt` of channel 0 returning 0, at the same site with
   an identical register file/fpscr and only idempotent channel traffic in between, spins 50 µs and then throws
   `SpuBlocked` from the poll instruction before any side effect. The wait arms the line watch, mailbox/signal hooks,
   the decrementer deadline and a timeout of 100 µs doubling to 1.6 ms; the retry executes the poll for real, so
   results never change, only timing. `ANYPS3_SPU_POLL_PARK=0` disables it. **Verified:**
   `tests/test_spu_pool_poll.py` (15 cases: every wake source, timeout wake, CPU while parked, loops that must not
   park, the real pool, group terminate). Agent micro-benchmark: a GETLLAR or `rchcnt` poller with a writer every
   ~5 ms used 2.05 s → 0.14 s CPU per 2 s. **Hypothesis:** the level's CellSpursKernel0 (SPU 0x100), which burns a
   whole core in a wait loop ([FAST record](2026-10-19-fast-wave.md#state-at-the-end-of-the-session-2026-10-19-2200-and-the-first-next-actions)),
   matches one of the two patterns; unverified without the game. **Risk:** a poll loop whose only changing state is
   a counter in local store (registers identical at the poll site) would be slowed by the park timeout.
5. `tests/test_spufast_blocks.py`: the sweeps bind their threads to image 0 (the unbound-image stop of item 1 made
   all 4 fail on `1f2e4b2`).

### Memory (PPU and SPU reservations)

1. **Reservation fast path** (`par_memory.py` `reserved_line/reserved_track/reserved_token/reserved_bump`;
   `runtime.py` `load_reserved`, `store_conditional`, `getllar`, `putllc`, both `reservation_token` forms), following
   the agent's finding that every `getllar`/`putllc` allocated span and lock vectors and ran `track_reservation` twice
   (backing hash lookup + `weak_ptr::lock` each). When the access and its whole 128-byte granule lie in one mapping,
   the published `Mapping::state` (the same `BackingState` object that `parallel->backings` names, `publish_maps`)
   gives the tracked flags and versions directly; tracking still goes through `track_reservation` the first time;
   same stripe set (`ScalarLock`), same token, same permission checks in the same order. A granule clipped by its
   mapping keeps the general path. **Verified:** `tests/test_reservation_fast_path.py` (fast and general paths:
   lost/kept reservations, consumed token, big-endian bytes, word vs line tokens, a mapping base inside a granule,
   read-only faults; 5 concurrent agents mixing lwarx/stwcx. and getllar/putllc lose no increment), which **fails**
   on a mutant whose fast path skips the version bump. **Measured** (`bench/ppu_bench.py`, new kernel `k_atomic`:
   uncontended lwarx/addi/stwcx./bne/bdnz with concurrent memory): **70.7 → 40.5 ns per guest instruction**,
   i.e. ~350 → ~200 ns per lwarx+stwcx. pair. The remainder is the ordinary `load()` path (ScalarAccess with
   watch/observer handling), the stripe mutex and `inline_suspend/resume`; not changed.

### RSX

1. **Register bank change tracking** (`rsx_registers.py`, `rsx_graphics.py` `refresh_registers`): per-256-byte-block
   epochs bumped only when a tracked `write` changes presence or value; mutable borrows (`operator[]`, non-const
   `at/find/begin`) mark the block exposed and it is then always verified by value; copy/assignment gets a new identity
   (full rebuild). The Session skips unchanged, unexposed blocks. FIFO, capture and surface code write/read through
   the tracked/const paths. **Verified:** `tests/test_rsx_register_epochs.py`.
2. **Index decode** (`rsx_draw.py` `rsxdraw::decode_indices`, `index_extent`): SSSE3/SSE4.1 big-endian 16/32-bit
   decode with restart and base, scalar tail. **Verified:** `tests/test_rsx_index_decode.py` (literal cases +
   20k random cases against the per-element definition; baseline and `-march=native`).
3. **Fragment shader prologue interpolates only used inputs** (`rsx_graphics.py`): inputs referenced by the RSX
   fragment program (source type 1, input index bits 13–16) are interpolated; two-sided colour inputs keep the
   run-time front/back selection; input 0 (position) and 14 (facing) are always set. GPU-side change, **not measurable
   here**; **verified** equivalent by test_rsx_graphics, test_rsx_parity, test_rsx_unused_coords (bit-exact images).
4. Small per-draw items: env switches read once (`rsx_vk_cache.py`), worker progress flag `load` before `exchange`
   (`rsx_game_worker.py`), single-thread draw path when the graphics backend is active (`rsx_draw.py`).
5. **MSAA plane runs** (`rsx_vk_graphics.py` `GraphicsBatch::PlaneDraw`, `graphics_replay_planes`). The default MSAA
   path renders each sample plane as its own image in its own render pass (layered/multiview are opt-in because they
   raised GPU time on the RTX 4070 SUPER, `anyps3/vulkan.py:218-230`). Previously every MSAA draw switched passes once
   per plane: **verified** from the comment at `vulkan.py:222`, the level replay had **~1712 render passes per frame**
   (76 with multiview). Now plane 0 of consecutive draws on the same attachments stays in one pass; planes ≥1 are
   recorded as runs (one pass per plane) when that pass ends. Each plane keeps the original draw order; planes are
   distinct images; every command outside a pass (copy, clear, barrier, conditional-rendering scope, submit) already
   goes through `graphics_end_pass`/`graphics_barrier`, which replay pending planes first. `ANYPS3_RSX_PLANE_RUNS=0`
   restores the old recording. **Verified:** test_rsx_gpu_batch (1/2/4 samples: one plane-0 pass for three draws,
   `render_passes==samples` after the transfer, unchanged pixels/depth/stencil, Khronos validation with sync
   validation clean), test_rsx_graphics, test_rsx_parity, test_rsxvk20–22 (incl. conditional), test_rsx_rtt,
   test_rsx_alias_flow, test_rsx_readback and the other RSX modules of the batch.
   **Hypothesis (weak):** part of the ~30 ms GPU busy time per frame in the level is render-pass switching
   (load/store of the attachments per pass); plane runs keep the per-plane layout and should cut passes to roughly the
   multiview count. **Counter-evidence (verified only as the historical code comment `anyps3/vulkan.py:222-224`, RTX
   4070 SUPER replay, not repeated):** the opt-in multiview variant cut passes 1712 → 76/frame but raised GPU wait
   5.6 → 30 ms/frame with no CPU gain, so fewer passes alone has not been shown to lower GPU time; multiview also
   changes the attachment layout (one layered image), which plane runs do not. Lavapipe here shows the CPU side only
   (2× MSAA wall time per draw 276 → 98 µs, Measurements). **Distinguishing test on the PC:** the same level window with
   `ANYPS3_RSX_PLANE_RUNS=0` and `=1`, comparing `PERF GPU busy_ns`, render passes per frame (RSX perf counters)
   and FPS.
6. **Stale test fixed:** `tests/test_rsx_gpu_upload.py` (vertex-format oracle) pasted `raw_vertex_glsl` into its own
   compute shader; since build 460 that code reads the state arena (`state_`), so the module failed on 460 with
   `'state_' : undeclared identifier` (**verified** on a worktree of `43c6b27`). The oracle now maps `state_` to its
   single buffer; literal expectations unchanged; the module passes. No runtime change was needed: production vertex
   shaders insert `state_glsl` (`rsx_graphics.py` `hardware_vertex_uncached`).

**Candidate, not implemented:** each draw wraps every plane in a precise occlusion query; the counts are
guest-visible only while ZPASS pixel counting (register 0x17cc) is enabled (`rsx_graphics.py` `retire_work`,
`rsx.py` report reset), otherwise they feed the diagnostic `draw.pixels`, which 20 test modules use as an oracle.
Eliding queries while 0x17cc is 0 would need a separate diagnostic mode; worth it only if a PC profile shows query cost.

### Scheduler

- `par_worker.py` `compose_execution`: with SPU/RSX workers enabled, an Audio engine slot whose next tick is in the
  future returns immediately (no work, no lock round trip). **Derived:** `audio_advance` mixes only when its tick is
  due, and the skipped wrapper's `rsx_worker_pump` still runs on every RSX request (`rsx_worker_request`,
  `rsx_game_worker.py`) and through the SPU worker's pump callback (`par_worker.py`). No dedicated test; test_native_audio,
  test_native_audio_full, test_par1–4/6/7/9 pass with it.

## Measurements (synthetic, this cloud CPU)

Base = worktree of `43c6b27` (snapshot 460), final = this branch after the reservation fast path. Same kernels and
benchmark sources for both; the cloud host is shared and memory-bound kernels vary by more than ±10% between runs.

**PPU kernels** (`python3 bench/ppu_bench.py ROOT OUT`, ns per guest instruction, best of 5 per run, runs 2–3 of 3
alternating base/final; run 1 was noisier):

| kernel | base | final | note |
| --- | --- | --- | --- |
| k_atomic (lwarx/stwcx. loop) | 68.9–71.1 | 39.6–41.8 | reservation fast path, −42% |
| k_vmx (vmaddfp/vperm/vaddfp) | 18.3–18.9 | 1.29–1.33 | VMX SIMD, ~14× |
| k_float, FPSCR XX set (`BENCH_FPSCR=0x02000000`) | 3.33 | 2.45 | lazy FR/FI/FPRF, −26% |
| k_float, XX clear (FPSCR 0, results stay exact) | 3.30–3.36 | 3.75–3.79 | +13%: extra mode test, lazy path never applies |
| k_copy (lwzu/stwu) | 1.17–1.18 | 1.07–1.10 | within noise |
| k_struct (lwz/lhz/lbz, conditional stw) | 0.95 | 1.02–1.04 | within noise (repeat runs gave 1.03–2.35) |
| k_branchy / k_calls | 0.79–0.81 / 1.10 | 0.78–0.79 / 1.12–1.14 | unchanged |
| k_spin (`or 31,31,31` loop) | 0.26 | 2.03 | intended: one host `pause` per block |

**Verified:** same-page indirect dispatch has no measurable effect on k_calls (1.13 with and without it, separate
build with the `goto ppu_dispatch_local` line removed); kept because it is correct (test) and removes one dispatcher
lookup per same-page return. The earlier suspicion of a ~5% k_copy/k_struct slowdown is **refuted** as noise.

**RSX draws** (synthetic `bench/rsx_draw_bench.py` scenarios on 8×8 surfaces, Lavapipe, `LP_NUM_THREADS=1`; both
binaries built with `-O2 -march=native -ffp-contract=off -fno-omit-frame-pointer`, `perf record -F 4000 -g`,
6000 draws × 3 repetitions; host cost = samples in AnyPS3 code plus libc/kernel called from it inside
`rsx_execute_draw`/flush stacks, scratch tool `host_cost.py`):

| scenario | host µs/draw base → final | wall µs/draw base → final |
| --- | --- | --- |
| identical draws | 10.9 → 5.6 | 51.2 → 54.1 |
| fragment constant changes every draw | 11.0 → 6.3 | 58.6 → 48.3 |
| same with 2× MSAA | 7.3 → 6.5 | **276.3 → 97.9** |
| alternating two textures | 8.6 → 5.9 | 56.1 → 53.1 |

Wall time is dominated by Lavapipe rasterizing on the CPU (not representative of a GPU). The 2× MSAA wall time
drop is the plane-run change: render-pass begin/end per plane is expensive in Lavapipe too. An earlier session
measurement of the indexed-mesh scenario (1200 BE16 indices) gave host 11.5 → 8.9 µs/draw (SIMD index decode).
**Limitation:** none of these is a game frame; the level's RSX worker spends ~28 µs/draw on the PC (460a), with
work these synthetic draws do not have (vertex arrays, texture uploads, surface cache, readbacks).

## Not done / open

- **No game build or FPS measurement** — the user must build and measure on the PC (commands below).
- PPU `k_copy`/`k_struct` single-run differences are host noise (see Measurements).
- RSX2 estimate from the FAST round stays valid: ≤10 ms/frame needs the RSX work split across threads.

## How to verify on the PC

Nothing in this round has a game FPS figure. On the PC with the game inputs (unused build number `N`, e.g. 470):

```bash
git pull   # branch claude/happy-franklin-2eocfp
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json \
  --profile profiles/ratchet-acit.json --max-steps 500000000 \
  -o local/ratchet/native-bundle-N
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build -j 24 \
  local/ratchet/native-bundle-N local/ratchet/native-virtual-N
local/ratchet/tools/measure_level.sh N Na      # same level/window as 380base/386a/460a
ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q
```

Compare `native-virtual-Na/level-summary.json` with 460a (22.59 FPS; RSX worker 28.3 ms, main_idle 23.2 ms,
GPU busy 30.0 ms per frame) and check that `level-frame.ppm` matches. Run-time switches for A/B runs of the same
level window without rebuilding: `ANYPS3_RSX_PLANE_RUNS=0` (one pass switch per MSAA plane, the previous recording)
and `ANYPS3_SPU_POLL_PARK=0` (no busy-poll parking). Set them in the environment of `measure_level.sh`; that script is
not in this container, so check that it passes the environment to the game process. Compile-time diagnostics only:
`-DANYPS3_VMX_FORCE_SCALAR` (exact VMX lanes) and `-DANYPS3_FP_FORCE_SOFT` (no lazy FPSCR and no host FP fast path).
Audio stays silent: `SDL_AUDIODRIVER=dummy`, `ANYPS3_AUDIO_SINK=null`, never `ANYPS3_AUDIO_AUDIBLE=1`.
