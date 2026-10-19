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
   (red on the previous lifter).
3. **Lazy FR/FI/FPRF** (`anyps3/floating.py` `fp_lazy*`, `fp_materialize`; `PpuContext::fp_lazy_*` in `threads.py`).
   With XX already set, RN rounding and all exception enables clear, an add/mul/div/sqrt/fused operation on in-range
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
   (two 8-byte stores followed by a 16-byte load). Covered by test_vector/test_ppu_block/test_par*.

### SPU

1. **Unbound image stop** (`spu.py` `spu_step`, `1f2e4b2`): a thread that `spu_prepare` never bound stops with
   "unknown native SPU image" (restores `test_par1`/`test_par2`, the SPUFAST 421 regression).
2. *SPU agent results (quantum, constant-time reservation check, polling-loop parking): see below once integrated.*

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
   **Hypothesis:** a large share of the ~30 ms GPU busy time per frame in the level is render-pass switching
   (load/store of the attachments per pass); plane runs keep the fast per-plane layout and should cut passes to
   roughly the multiview count. **Distinguishing test on the PC:** the same level window with
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
  future returns immediately (no work, no lock round trip).

## Measurements (synthetic, this cloud CPU)

*Filled in after the final comparison run.*

## Not done / open

- **No game build or FPS measurement** — the user must build and measure on the PC (commands below).
- PPU `k_copy`/`k_struct` showed a possible ~5% slowdown in single runs; a bisect under load was inconclusive.
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
GPU busy 30.0 ms per frame) and check that `level-frame.ppm` matches. Run-time switch for an A/B run without rebuilding:
`ANYPS3_RSX_PLANE_RUNS=0` (one pass switch per MSAA plane, if integrated). Compile-time diagnostics only:
`-DANYPS3_VMX_FORCE_SCALAR` (exact VMX lanes) and `-DANYPS3_FP_FORCE_SOFT` (no lazy FPSCR and no host FP fast path).
Audio stays silent: `SDL_AUDIODRIVER=dummy`, `ANYPS3_AUDIO_SINK=null`, never `ANYPS3_AUDIO_AUDIBLE=1`.
