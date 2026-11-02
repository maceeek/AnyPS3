# Cloud round HIRES8: generated SPU code and PPU page dispatch — 2026-11-01

**Author/model:** Claude (cloud container) as coordinator with three Opus subagents: J (generated SPU code, first cut),
J2 (follow-up on J's profile: calls that return into generated code) and K (PPU page dispatch of the main guest thread);
branch `ccr-b1a3987e-aypb5o`, based on d1177e9 (HIRES7 merged + comment fix). Agent J ended twice with work uncommitted;
the coordinator finished its queue (test fix, three mutation runs) and committed its cut as c74b9c9; J2 continued from there.
**Scope:** the user's 1440p 60 FPS goal. After HIRES7 the main guest thread (~14.8 ms per heavy frame on the PC, ~3.9 ms of
it spin-waiting for an SPU job, 57 % translated page code) was the likely critical path. The container has **no game data,
no `local/` tree and no NVIDIA GPU** (4 vCPU, lavapipe only): no game build, run, FPS or frame figure was produced. Every
runtime statement is a native unit test, a callgrind count on a synthetic bench (instructions are not time) or a
thread-CPU timing of a synthetic job.

## Test environment (cloud only)

As in [HIRES7](2026-11-01-cloud-hires7.md#test-environment-cloud-only) (synthetic SPU job `bench/spu7_job_bench.py`; decision
metric "non-FP Ir" = SPU-thread Ir minus `spu_single_vector`, because valgrind has no AVX-512, plus native AVX-512 time).
New this round:
- **PPU bench (K, 94621fa, 6be0c45):** `bench/ppu_kernels.s` gained five game-shaped kernels (the eight old kernels keep
  their bytes): `k_xcall` (cross-page `bl`/`blr` to a leaf with a frame), `k_calltree` (non-leaf cross-page callee calling
  leaves in two pages), `k_virtual` (C++ virtual call through vtable and function descriptor, four classes in three pages),
  `k_switch` (GCC jump-table switch through `bctr`), `k_mix` (record loop with `rlwinm`/`rlwimi`, two CR fields).
  `BENCH_CALLGRIND=DIV` runs each kernel once under callgrind (collection only inside `anyps3_entry`) and splits host Ir per
  guest instruction by emitter construct (body, block head, page entry, page switch, exits, `ppu_dispatch`, runtime);
  `BENCH_BRANCH=1` adds callgrind's branch simulation.
- **Finding (verified, K):** under valgrind the PPUFAST inline flat guest view was never active — valgrind rejects the
  `mremap(old, 0, ...)` duplication (EINVAL), so every guest load/store of every earlier PPU callgrind figure took the slow
  path (k_copy 187 Ir per guest instruction). The bench interposes `mremap` in callgrind mode only (`-DBENCH_MREMAP_SHIM`,
  same memfd pages through `/proc/self/map_files`); native runs (`BENCH_INLINE_STATS=1`) confirm the inline path (k_copy:
  0 slow loads/stores). Earlier PPU callgrind numbers in the knowledge base measured the admitted slow path, not the code
  the game runs.
- **SPU harness tools (J, J2):** `iclass.py` (callgrind `--dump-instr=yes` classes of the executed `spu_fn_*` host
  instructions: register-file loads/stores, constant byte swaps, stack homes, LS accesses, compare/branch), `fsize.py`
  (host bytes of the generated functions), `j9/split.py` (bench `b.cpp` split so a variant of the generated functions
  recompiles in ~3 min; reproduces the unsplit numbers exactly). Scratchpad scripts, not in the repository.

## Changes

### Agent J: SPU self loops (c74b9c9, merged with J2 as abaeba4)

| commit | change | switch | tests |
| --- | --- | --- | --- |
| c74b9c9 | A superblock whose conditional branch targets its own start is emitted as a C++ loop: after the guarded entry, registers the body reads before setting them stay in host locals in the representation the previous iteration ended with; registers set before every exit are not stored at the back edge. The back edge continues only when the next entry's guard would take its fast path; otherwise registers are flushed and the block is entered through its label. Every point that can observe the register file (self-store exit, inline-WRCH interrupt boundary, lifted bodies without a template, the exit flush) sees architectural values; the generator asserts both properties on its second pass | `ANYPS3_GEN_SPU_SELF_LOOP=0` (generation time: needs its own build) | `test_spu_block`: state equals the exact interpreter at every slice end (exact and soft budgets ending inside the loop, hard step limits) and an independent Python model; an early self-store exit in iteration 2 stores iteration 1's registers; generated form. Mutations: early-exit registers not carried, carried registers not dirty (generator asserts off), back edge without the r6 update — each fails |

### Agent J2: no call returning into generated store checks (d23ca42, 58ba277, merged as abaeba4)

| commit | change | exactness | tests |
| --- | --- | --- | --- |
| d23ca42 | Each generated STQ marks its 16-byte line in `t.ls_forget` (256 × u64 at the end of `NativeSpu::Thread`) under the old condition `!dma_writes.empty()` instead of calling `spu_ls_forget`; `spu_ls_drain` applies the marks before every `spu_ls_write` (insert or forget) and at the start of `spu_dynamic_record`; `spu_dynamic_fault` applies them to a copy | Only the DMA provenance diagnostic map changes, and only with `ANYPS3_SPU_DYNAMIC_LOG` (without it the map stays empty and nothing is marked). Forgets only remove bytes and commute; every insert and every reader drains first, so each sees the map of eager forgets. Guest state untouched. The self-store exit J2 also measured (x3, same gain) was rejected: re-entry mid-block interprets to the next leader, which writes "interpreted" log rows for translated code and moves interrupt sampling points | `test_spudyn_runtime`: native STQ self loop with the log set at process start, hand-computed spans through every reader (fault text, next forget, GET after store, dynamic-log row, runs across mark words incl. a full word); log off → map empty. Mutations M1–M5 fail (template store without mark; no drain in `spu_dynamic_record`; fault text without pending marks; no drain before an insert; only the first line of a run applied); one equivalent mutant survived (explained in RESULT-SPUGEN8b) |
| 58ba277 | The prologue sets `slim = prov ? 0x40000 : flen+15`, so each store check is one compare `((a+15-flo)&0x3ffff) < slim`; self loops compare against `lslim`, a copy of `slim` behind an empty `asm` (kept in a register in call-free loops); the self-loop back edge `ex<lim && (vm&mask)==mask` becomes `ex<llim` with `llim` fixed before the loop | With provenance on the compare is always true and the old exact branch runs inside; without, it is exactly the hit test. `lim` and `vm` are re-derived only at block ends and after calls, which a self-loop body does not contain (every body exit returns); the generator asserts that no body line assigns them | `test_spu_block`: a self loop whose 64-byte region is unproven runs the exact guard on every entry (11 `spu_block_enter` by hand, 0 with the region intact; registers/steps/pc); generated form updated. Mutations C1 (`llim=lim`), C2 (`slim` ignores `prov`), C3 (`lslim=0`) each fail |

### Agent K: PPU page dispatch (merged as b68e12a)

| commit | change | switch | tests |
| --- | --- | --- | --- |
| cb0a410 (A) | Page dispatch switch on word slots: `ppu_page_slot(pc, base) = rotr32(pc-base, 2)` — one jump table per page instead of compare trees; a page with a misaligned head keeps `switch(pc_v)` | — | `test_ppu_local_dispatch` (misaligned 0x10042 must not run the 0x10040 block; mutation: plain shift fails) |
| 7ff0f2c (B), 9f89c35 | Host-call return prediction: `bl` to another translated page and `bctrl` out of the page whose continuation C is a head of the calling page push C (`ppu_ret_stack[64]` in `PpuCpu`) and call the callee; a `bclr` pops only when its actual next PC equals the top entry, and the caller continues at `ppu_C` with the same head checks. `anyps3_entry` restores its base depth before every dispatch and on exit; a full stack keeps the old tail call | `ANYPS3_PPU_RET_DEPTH=0..64` (default 64; 0 = old tail calls) | new `test_ppu_ret_predict` (mutations: pop accepting any top entry, call site without the depth check — both fail: `r3==12 && steps==11`, LR+8 return) |
| 594823a (D) | `bclr`/`bcctr` publish PC/steps only on the target-range stop (no other reader on the normal path; the throw branch publishes the same values first) | — | `test_ppu_block` (mutation: throw without publication fails) |
| b67f468 (E) | Page functions keep the addresses of the `steps`/`reschedule` reference members (bound at construction, same objects read and written at the same points) | — | `test_ppugen1` (mutation: head ignoring `*resched_v` fails) |
| 94621fa, 6be0c45 | Bench kernels, callgrind split and branch modes (above) | — | — |

## Synthetic measurements

### SPU synthetic job (coordinator integration: base d1177e9 vs merge abaeba4, cloud scratchpad `h8/bench.log` (not preserved))

| metric | d1177e9 | abaeba4 | change |
| --- | ---: | ---: | ---: |
| native AVX-512 time, ns/iteration (3 alternating rounds, quiet) | 15 723 / 15 798 / 15 875 | 9 615 / 9 513 / 9 714 | **−39 %** |
| SPU-thread Ir/iteration (x86-64-v3, `spu_step` inclusive) | 182 484 | 143 557 | −21.3 % |
| non-FP Ir/iteration (minus `spu_single_vector` 68 176 in both) | 114 308 | 75 381 | **−34.1 %** |
| generated code `spu_fn_*` self Ir/iteration (iclass) | 84 214 | 45 186 | −46.3 % |
| — stack homes (rbp/rsp operands) | 24 763 | 9 505 | |
| — register-file `g[]` stores / loads | 6 347 / 2 452 | 247 / 212 | |
| — constant `pshufb` (byte swaps) | 7 477 | 4 124 | |
| `spu_fn_*` host bytes, AVX-512 / v3 | 1 138 806 / 1 227 028 | 1 111 653 / 1 195 408 | −2.4 % / −2.6 % |
| output digest (native and v3, 300 iterations) | 2dbd95c1 | 2dbd95c1 | identical |

Per agent (from their result files, same harness): J's self loops 83.95 k → 59.70 k generated Ir (−28.9 %); J2's two cuts
59.70 k → 47.31 k → 45.18 k (with the game's SPU flags `-fno-crossjumping -fno-tree-vrp`: 58.57 k → 44.56 k). The
remaining non-FP cost on abaeba4: generated code 45.2 k (SIMD loop 25.7 k, FP loop 8.2 k, main 6.1 k, leaf 5.1 k), DMA path
≈ 20 k (`GuestMemory::transfer` 6.7 k, `atomic_read` 5.9 k, `atomic_write` 2.3 k, the rest admission/spans/notifications),
`spu_chain_call` 4.0 k, `spu_write_channel` 2.1 k.

### PPU bench (agent K, callgrind DIV=20, host Ir per guest instruction)

| kernel | base d1177e9 | +A | +B | +D | +E | merge abaeba4 | change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| k_xcall | 18.55 | 17.59 | 16.18 | 16.09 | 15.94 | 15.95 | **−14.0 %** |
| k_calltree | 17.14 | 17.24 | 16.19 | 16.03 | 15.92 | 15.93 | −7.1 % |
| k_virtual | 19.51 | 18.80 | 18.24 | 18.08 | 18.04 | 18.05 | −7.5 % |
| k_switch | 10.55 | 9.90 | 9.99 | 9.76 | 9.77 | 9.78 | −7.3 % |
| k_calls | 13.15 | 12.02 | 12.28 | 12.03 | 11.96 | 11.98 | −8.9 % |
| k_branchy | 9.67 | 9.63 | 9.64 | 9.64 | 9.57 | 9.58 | −0.9 % |
| k_copy | 17.10 | 17.26 | 17.07 | 17.07 | 17.08 | 17.11 | +0.1 % |
| k_struct | 14.38 | 14.35 | 14.31 | 14.36 | 14.18 | 14.20 | −1.3 % |
| k_mix | 12.43 | 12.36 | 12.37 | 12.37 | 12.27 | 12.28 | −1.2 % |
| k_float | 60.69 | 60.73 | 60.84 | 60.83 | 60.59 | 60.60 | −0.1 % |
| k_vmx | 19.99 | 20.15 | 20.17 | 20.17 | 20.28 | 20.29 | +1.5 % |
| k_spin | 4.25 | 4.21 | 4.23 | 4.23 | 4.13 | 4.14 | −2.6 % |
| page-function text, bytes (344 guest instructions) | 33 499 | 33 566 | 34 055 | 34 317 | 32 749 | 32 765 | −2.2 % |

The per-commit columns are K's; the merge column is the coordinator's run on the merged snapshot (cloud scratchpad `h8/bench.log` (not preserved)), which
reproduces K's final tip exactly (every kernel and the page text), so the SPU merge did not touch the PPU path. The
`runtime` share (~2.6 Ir/instr, the 256-step slice loop of the bench) is identical in all kernels; page-function part
only: k_xcall 15.97 → 13.35 (−16.4 %), k_calltree 14.57 → 13.35 (−8.4 %). Target −25 % on the call/return/indirect
kernels: **not reached**.

Native (K, 4 shared vCPUs, best of 9, 3 alternating rounds, median; noisy): k_xcall 1.371 → 1.176 ns/instr (−14 %), k_calltree
−9.5 %, k_virtual −5 %, k_calls −10 %, k_struct −6 %, k_spin −6 %, k_switch/k_branchy/k_mix ±1 %; k_copy and k_vmx +3–6 %
while their Ir is unchanged (k_copy moves 1.04–1.25 between builds with identical Ir: layout). Same build with
`ANYPS3_PPU_RET_DEPTH=0`: k_xcall 1.272, k_calltree 1.178 — cut B alone ~7.5 % on the cross-page call kernels.

## Findings

- **Verified (J, J2): a call that returns into a hot generated path makes GCC home every live vector value on the stack.**
  The SysV ABI makes all xmm/ymm/zmm registers caller-saved; one `if(prov)spu_ls_forget(...)` per store (a diagnostic,
  off by default) was enough to give the whole function stack homes: removing only that call took stack round trips
  23.0 k → 9.4 k Ir/iteration. `[[gnu::cold]]` on the callee changes nothing (identical Ir). Rule for generated SPU code:
  no call that returns into a hot path; rare work is deferred without a call or leaves through an exit.
- **Verified (J2):** what is left in the SIMD self loop (49 host instructions per 13 SPU instructions) is register
  allocation (3 copies of carried values, a constant rematerialisation, one spilled invariant); no further exact
  instruction-removing cut was found there. The AVX-512 FP loop still homes its values because `spu_vfp_slow` (exact slow
  lanes, ~6 % of the critical lane in the game) returns into it — **hypothesis:** a measurable part of the FP loop time on
  the PC; no call-free exact form without code growth was found.
- **Verified (K):** per cross-page call + return the remaining host cost is the callee page prologue/epilogue (~17 Ir, 6
  callee-saved registers of a large page function), entry loads + jump table (~8), push/call/check + pop/return (~20);
  every block head ~8 Ir; every inline guest load/store ~9–13 Ir (`s.memory` → `inline_view.table`/`base` reloaded at every
  access). A block-local copy of the view was measured and rejected (−1 % Ir for +1.4 % page text). **Hypothesis (K):**
  GCC 14+ `no_callee_saved_registers` on page functions would remove the ~17 Ir prologue/epilogue; untested (GCC 13 here).
- **Derived, unverified:** if the game's awaited job scales like the synthetic one (−39 % native), the main thread's
  ~3.9 ms SPU wait would shrink by well over 1 ms per heavy frame; together with K's dispatch cuts this is the first cloud
  round aimed at the main thread's ~14.8 ms. Only the PC can show it.

## Coordinator review (before the merges)

- K's branch (merged as b68e12a): the return-prediction invariant (depth = live host frames above `anyps3_entry`'s base,
  pop only on an exact next-PC match, base restored per dispatch), the jump-table bijection and the steps/reschedule
  reference addresses were checked against the code; the page-prologue caching of `slice_v`/`trace_v`/`steps_p`/
  `resched_v` reads the same objects at the same points.
- J2's branch (merged with J's cut as abaeba4): the deferred forgets reach every reader of `dma_writes` in the tree (only
  `spu_ls_write`, `spu_dynamic_record`, `spu_dynamic_fault` and the thread reset, which clears the marks too); every STQ
  address passed to the mark is 16-byte aligned (`&0x3fff0`); `values.slim` is set on the fresh `BlockValues` of each self
  loop only; the `lim`/`vm` assertion regex matches `=`, `|=`, `&=` assignments and not `llim`/`lslim`. A peer SPU PUT now
  also drains the target thread's marks from the peer thread — the same pausing assumption the code already made for
  `dma_writes`, and only with the diagnostic log on.

## Regression tests

On each agent's branch (verified, one module at a time, lavapipe where Vulkan is used):
- **J2's tip 58ba277 (includes J's c74b9c9), 21 SPU modules OK:** test_spudyn_runtime, test_spu_block, test_spudyn_split,
  test_spudyn2_content, test_spudyn_isa, test_spufast3_leaders, test_spufast3_wrap, test_spureloc_split, test_spu_pool_quantum,
  test_spu_pool_poll, test_spufast_runtime, test_spufast2_blocks, test_spuperf2_blocks, test_spuperf2_simd,
  test_mainslack_reloc_index, test_spu_wrch_interrupt, test_spureloc_runtime, test_spufast2_vector, test_spu_gaps,
  test_spuput_peer_ls, test_spu.
- **K's tip 9f89c35, OK:** test_ppu_ret_predict (new), test_ppu_local_dispatch, test_ppu_block, test_ppugen1,
  test_ppufast_codegen, test_ppufast_memory, test_ppu_atexit, test_ppu_rotate_count, test_ppu_spin_hint, test_ppu_string_trap,
  test_jump_tables, test_execution, test_par13_memory, test_hlefast1, test_anyps3, test_integration_groups, test_fp_special,
  test_floating, test_byte_reverse. Known base failures in the same category: test_ppu_fp_lazy (1) and test_ppuperf3 (3)
  fail compiling `fp_fused_sum` in floating.py (untouched); test_program: two 30 s compile timeouts under load, its two
  updated case-form assertions pass.

On the merged snapshot abaeba4 (coordinator, cloud scratchpad `h8/tests/summary.txt` (not preserved), verified): test_ppu_ret_predict, test_spudyn_runtime,
test_spu_block, test_ppu_local_dispatch, test_ppu_block, test_execution, test_anyps3, test_integration_groups, test_spu,
test_par13_memory — all OK. The full suite is deferred to the end of HIRES9 (it will cover HIRES8 + HIRES9).

## Risks

- `NativeSpu::Thread` grows by ~2 KB (the mark array, at the end of the struct).
- Generated SPU code now contains an empty `asm` statement (GCC and clang support it).
- PPU host stack: return prediction nests up to 64 page-function frames per dispatch (frames unwind at every 256-step
  slice); `ANYPS3_PPU_RET_DEPTH=0` restores tail calls. Prediction lives within one slice only.
- The PPU page switch is one shared jump-table `jmp` per page; callgrind's history-less predictor counts more indirect
  misses on k_calltree/k_virtual (model only; real predictors use history — hypothesis: neutral).
- k_vmx +1.5 % Ir (register allocation around the new page-wide locals); k_copy/k_vmx native +3 % here (layout-sensitive).
- GCC 13 here (musttail via the shim's sibling calls); the PC's newer GCC may lay the page functions out differently.

## First next actions on the real PC

- One integration build of the branch tip; QUIET alternating 1440p walks against the HIRES7 tip (or d1177e9): `main_ppu`
  ms/frame in heavy frames, the main thread's SPU-wait share (delay 0x666068, job flag 0x5564d4) and the awaited lane's
  time, `PERF PPU_THREAD` ns/instr of thread 1, sampler shares of `anyps3_dispatch_page`, page entry and page code.
- Same binary with `ANYPS3_PPU_RET_DEPTH=0` (cut B only). The SPU cuts are unconditional except the self loops
  (`ANYPS3_GEN_SPU_SELF_LOOP=0` at generation time: a second build).
- A run with `ANYPS3_SPU_DYNAMIC_LOG` set: the log rows must match the base build's.
- Compile cost: ppu-*.o / spu-*.o total `.text` and wall time of the ~190 units each vs the base (bench: PPU page text
  −2.2 %, SPU generated code −2.4 %).
