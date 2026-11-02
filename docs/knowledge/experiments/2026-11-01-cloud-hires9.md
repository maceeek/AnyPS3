# Cloud round HIRES9: PPU inline guest memory, block heads and the rest of the SPU job — 2026-11-01

**Author/model:** Claude (cloud container) as coordinator with two Opus subagents: L (inline guest loads/stores and
block heads in translated PPU code) and N (the rest of the awaited SPU job: chained calls, DMA path, FP slow path);
branch `ccr-b1a3987e-aypb5o`, both based on abaeba4 (HIRES8 merged), merged as 81dd09c (L) and e896fed (N).
**Scope:** the user's 1440p 60 FPS goal; both target the main guest thread (~14.8 ms per heavy frame on the PC, must be
<= ~13 ms): L its translated page code (57 % of it), N the SPU job it waits for (~3.9 ms per heavy frame, PC, HIRES2).
The container has **no game data, no `local/` tree and no NVIDIA GPU** (4 vCPU, lavapipe only): no game build, run, FPS
or frame figure was produced. Every runtime statement is a native unit test, a callgrind count on a synthetic bench
(instructions are not time) or a thread-CPU timing of a synthetic job.

## Test environment (cloud only)

As in [HIRES8](2026-11-01-cloud-hires8.md#test-environment-cloud-only): SPU synthetic job `bench/spu7_job_bench.py`
(decision metric "non-FP Ir" = SPU-thread Ir minus `spu_single_vector`, because valgrind has no AVX-512, plus native
AVX-512 time); PPU bench `bench/ppu_bench.py` with `BENCH_CALLGRIND=20` (`-O2 -march=x86-64-v3 -ffp-contract=off`,
`-DBENCH_MREMAP_SHIM`; x86-64-v3 includes MOVBE and BMI2), host Ir per guest instruction split by emitter construct, page
text = `page_*` hot+cold bytes for the bench's 344 guest instructions.

## Changes

### Agent L: PPU inline guest memory and block heads (d7cb28a..7dd2cf4, merged as 81dd09c)

All unconditional and exact (no runtime switch; trace builds keep the old block heads).

| commit | change | exactness | tests |
| --- | --- | --- | --- |
| d7cb28a | Scalar range test in one compare: `(ea & 0xffffffff00000fff) <= 4096-N` replaces `(ea>>32)==0 && (ea&4095)<=4096-N`; N==1 keeps only the high test; byte ranges keep the split form (changing them cost k_vmx +2.5 %) | a set high bit makes the masked value >= 2^32 > 4096-N; with the high half zero it is `ea&4095` (proof in the comment of `inline_scalar_span`) | `test_ppufast_memory`: every width at the page edge, every high bit set on a mapped low half |
| 88c3cfc | One pointer per access: the 1 MiB blocker table is mapped directly below the flat view in the same reservation (`table[i]` at `base-pages+i`), so an access loads only `inline_view.base`; outside a section the base is `blocked_base()` (one past a static all-0xff table), so the same test sends every access to the slow path | every writer and reader of the table checked: publish, `inline_mark_host`, `clear_claim`, resume/leave, medium paths, `inline_try_*`, reset (unmaps table+view), address-space copies (start without a view), the sample profiler's `&inline_flat.base` | `test_ppufast_memory`: layout test, a blocker published mid-run, blocked view; `test_ppufast_codegen`: mid-run reservation blocker in translated code, an effective address above 4 GiB |
| 2d79876 | With `__MOVBE__`, 2/4/8-byte inline reads/writes are one `asm volatile` `movbe` on exactly N bytes; other builds keep the volatile form | `asm volatile` is never merged, dropped, duplicated or moved across another volatile access; its `m` operand names exactly the N guest bytes (GCC behaviour; not checked with clang) | the memory and codegen tests rerun as `-mmovbe` builds |
| 1c39d80 | Block heads: `steps_v>=head_v \|\| *resched_v` with no per-block budget compare; the exit records `count<<32\|pc` of that head (`ppu_head_exit`). `anyps3_entry` sets `ppu_head_end = min(slice end, K)` with `K = max_steps - max_count + 1` (below K no block can cross the budget); from K on it admits one block per dispatch by the recorded count (an unknown head exits once without a step to record it); a block that would cross the budget stops with the same message, PC and steps after the same engine polls | `head_bound()` is evaluated after every block is emitted (one emitter, one dispatcher loop, reentrant: every iteration recomputes `ppu_head_end`, so a nested module-loading entry can only make it smaller or admit the block it checked); the recorded count is a fact of the translation, so a context switch cannot make it stale | `test_ppu_block`: head bound/budget test (slice, reschedule, recorded exits, admission at K) |
| 7dd2cf4 | One-byte accesses as `movzbl`/`movb` with base+index addressing, value-range hints on the asm reads (without them k_struct rose to 12.37) | same single access; the ranges state what `movzbl`/32-bit `movbe` already guarantee | as above |

Tried and rejected (L): keeping register locals across a self loop's back edge (k_copy −3.6 %, page text +5.7 %, k_spin
worse); the head bound with the old exact checks in a per-head cold path (−0.3..−0.6 Ir/instr, +3.5 % text); deferring
register write-back to block end (analysis only: GCC reloads registers at every head even without an intervening call).

### Agent N: the rest of the SPU job (71f2dc3..77ec2a5, merged as e896fed)

| commit | change | switch | exactness | tests |
| --- | --- | --- | --- | --- |
| 71f2dc3 | Chained BRSL calls: the callee's `SpuBlockScope` continues the caller's (`t.chain_scope`, taken by the callee's first statement) instead of a commit, a second scope and a rearm; the outermost scope commits once; the callee is called directly instead of through `spu_native_call`. ~300 → ~227 Ir per leaf call | `ANYPS3_SPU_CHAIN_SHARED=0` = old per-call form | one origin for steps/budget, so the callee's guards compare the same offsets a fresh scope would (limit < 2^31 keeps the clamped hard bound unreachable); a generation change inside the callee keeps only what the per-call protocol had committed (`kept`); one before the call takes the per-call form. Diagnostics only: `published_steps` is stored once per outermost scope | `test_spu_block`: 13 hand-written per-call cases (normal, overshoot, generation changes, exceptions, nesting, origin sample, depth/exit bounds, miss) in both forms; mutations c1–c4 killed |
| 4fffdc7, 77ec2a5 | DMA path: `atomic_read`/`atomic_write` move 64 bytes (four 16-byte accesses) per iteration; `LineLocks` clears its 4096-bit map lazily (no 512-byte clear per command); page and cell lists hold 8 entries inline (a 16 KiB transfer spans at most 5 pages: no allocation per command); `cpu_keys` skips the sort for one span. 77ec2a5 masks an index (identity) against a GCC 13 `-Warray-bounds` false positive under `-O2 -Werror` | — | same ascending sequence of 16-byte atomic accesses; same stripe set and order (every word of the grown range is cleared or already meaningful); one span's pages are generated ascending and distinct | `test_atomic_copy_unrolled` (alignments 0..15 × sizes 0..300, LineLocks against a std::set incl. multi-span/wrap); mutations d1–d3 killed |
| 1a220a3, 63ed4fa | A DMA write part none of whose 128-byte lines a reservation ever tracked skips its version bumps and `ReservationNotifications::notify` (a locked RMW per line), as an untracked scalar store already does (`ScalarWrite` + `if(write.tracked)bump_scalar`) | `ANYPS3_GUEST_WRITE_TRACKED_ONLY=0` = bump every part | `tracked` is sticky and set only by `track_reservation`, whose callers hold the line's stripe, which the write holds (flag read after the stripe acquire); versions are read only by the two reservation-token paths, which track the span first; the lost-event filter (`observe`) and `ReservationWatch` are on SPU reservation lines, tracked at `getllar`; a part with any tracked line bumps all its lines. Edge: "guest write version overflow" can no longer occur for an untracked line | `test_atomic_copy_unrolled` write test: tracked lines bump, untracked keep versions and touch counters, rerun with the switch at 0; mutations t1, t2b killed |
| d2b7f3e | SHUFB with both sources held only as word- or halfword-swapped lanes selects with the control's low two bits flipped (`^3`/`^1`) instead of byte-swapping both sources | — | the flip changes only bits 0..1 of each control byte (in-quadword index); the source bit 4 and the special-control bits 5..7 are untouched | `test_spu_block`: SHUFB against the SPU ISA v1.2 written out in Python (all index classes, 0x80/0xc0/0xe0 specials); mutations s1b, s2b killed |
| e788898 / 505db6d | 64-byte aligned SPU register file — **reverted** (identical v3 code, no native effect over 5 rounds) | — | — | — |

Not cut (N): the AVX-512 FP slow path's stack homes (only an asm trampoline saving zmm/k registers or a per-site
continuation copy would avoid them: ABI/unwind-specific or growing every FP site); the DMA stripe locks (writer protocol);
the general per-command DMA bookkeeping (~150–330 Ir per command; a one-page write fast path like `read_page` would save
~1–1.5 k Ir/iteration but must replicate the full-reader admission exactly); the callee's own HostAccess origin (~14 Ir per
call); the fn_1 SIMD loop (48 host instructions per 13 SPU instructions: representation and register allocation).

## Synthetic measurements

### SPU synthetic job (coordinator integration: base abaeba4 vs merge e896fed, cloud scratchpad `h9/bench.log` (not preserved))

| metric | abaeba4 | e896fed | change |
| --- | ---: | ---: | ---: |
| native AVX-512 time, ns/iteration (3 alternating rounds) | 9 465 / 9 661 / 9 966 | 8 681 / 8 849 / 8 713 | **−9.8 %** (means 9 697 → 8 748) |
| SPU-thread Ir/iteration (x86-64-v3, `spu_step` inclusive) | 143 557 | 136 824 | −4.7 % |
| non-FP Ir/iteration (minus `spu_single_vector` 68 176 in both) | 75 381 | 68 648 | **−8.9 %** |
| generated code `spu_fn_*` self Ir/iteration (iclass) | 45 186 | 44 070 | −2.5 % |
| — constant `pshufb` (byte swaps) | 4 124 | 3 094 | (SHUFB lanes) |
| `spu_chain_call` + `spu_native_call` self | 3 950 + 833 | 2 924 + 107 | −37 % |
| `GuestMemory::transfer` / `atomic_read` / `atomic_write` / `notify` self | 6 686 / 5 924 / 2 278 / 843 | 6 274 / 4 583 / 1 818 / 0 | −19 % |
| `spu_fn_*` host bytes, AVX-512 / v3 | 1 111 653 / 1 195 408 | 1 114 518 / 1 194 994 | +0.26 % / −0.03 % |
| output digest (native and v3, 300 iterations) | 2dbd95c1 | 2dbd95c1 | identical |

N's own alternation (4 rounds, same harness): base 9 591, tip 8 732 ns/iteration (−9.0 %); tip with
`ANYPS3_SPU_CHAIN_SHARED=0` 8 910 (the chain cut ≈ 2 %), with `ANYPS3_GUEST_WRITE_TRACKED_ONLY=0` 9 125 (the untracked-write cut
≈ 4.3 %). Per commit (N, non-FP Ir by N's script, base 74 958): 71f2dc3 72 591, 4fffdc7 69 929, 1a220a3+63ed4fa 68 872, d2b7f3e
68 373. Targets (non-FP −25 %, native −15 %): **not reached**. Since HIRES7's base d1177e9: native 15.8 k → 8.7 k ns/iteration
(−45 %), non-FP 114.3 k → 68.6 k Ir/iteration (−40 %).

### PPU bench (callgrind DIV=20, host Ir per guest instruction)

| kernel | base abaeba4 | +d7cb28a | +88c3cfc | +2d79876 | +1c39d80 | merge e896fed (= L's tip 7dd2cf4) | change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| k_copy | 17.11 | 16.52 | 15.90 | 15.30 | 14.33 | 14.33 | **−16.2 %** |
| k_mix | 12.28 | 11.78 | 11.60 | 11.11 | 10.69 | 10.58 | −13.8 % |
| k_struct | 14.20 | 13.44 | 13.33 | 12.76 | 11.94 | 11.84 | **−16.6 %** |
| k_virtual | 18.05 | 16.98 | 16.83 | 15.78 | 15.29 | 15.23 | −15.6 % |
| k_xcall | 15.95 | 15.45 | 15.19 | 14.78 | 14.14 | 14.14 | −11.3 % |
| k_calltree | 15.93 | 15.15 | 15.01 | 14.45 | 13.85 | 13.85 | −13.1 % |
| k_calls | 11.98 | 11.48 | 11.21 | 10.72 | 9.83 | 9.83 | −17.9 % |
| k_switch | 9.78 | 9.48 | 9.54 | 9.24 | 8.57 | 8.57 | −12.4 % |
| k_branchy | 9.58 | | | | | 8.77 | −8.5 % |
| k_spin | 4.14 | | | | | 3.93 | −5.1 % |
| k_float | 60.60 | | | | | 59.09 | −2.5 % |
| k_vmx | 20.29 | | | | | 19.92 | −1.8 % |
| k_atomic | 466.64 | | | | | 465.66 | −0.2 % |
| page-function text, bytes (344 guest instructions) | 32 765 | 32 288 | 31 274 | 32 342 | 29 097 | 29 173 | **−11.0 %** |

The per-commit columns are L's; the base and merge columns are the coordinator's runs on the snapshots (cloud scratchpad
`h9/bench.log` (not preserved)): the merge reproduces L's tip exactly in every kernel and the page text, so the SPU merge did
not touch the PPU path. Split of the merge (base → merge): block heads k_copy 1.62 → 1.02, k_xcall 2.02 → 1.27, k_mix 1.74 →
1.02; page body k_copy 12.60 → 10.40, k_struct 9.47 → 8.00, k_virtual 10.66 → 8.30. The ~2.6 Ir/instr `runtime` share (the
bench's 256-step slice loop) is unchanged; without it the page-function part dropped 18–21 % on the memory kernels.
Target −25 % on k_copy/k_mix/k_struct/k_virtual: **not reached**.

Native (L, `-march=native`, median of 3 alternating rounds, 4 shared vCPUs; noisy): k_copy −8.5 %, k_mix −11 %, k_struct
−14.5 %, k_virtual −7.4 %, k_calls −21 %, k_vmx +3 % (no scalar access in its loop; layout).

## Findings

- **Verified (L, disassembly):** an inline PPU guest access is now 11 host instructions (was 18–19). What remains in the
  memory kernels is mostly not memory code: register reloads and spills at every block head, write-through register
  stores, and ~10 instructions for each `cmpwi` CR-field update. Further PPU gains need register allocation across blocks
  or a cheaper CR representation, not a cheaper access.
- **Verified (N):** per chained SPU call the remaining cost is ~227 Ir (spu_chain_call 88, the callee's scope copy and
  hand-back ~18, its HostAccess origin ~14, prologue/epilogue and stack protector); the DMA path still pays ~65 stripe-lock
  pairs per job iteration (writer protocol, kept) and ~150–330 Ir of admission per command.
- **Verified (N):** the untracked-write cut alone is worth ~4.3 % of the native job time (65 locked touch RMWs per
  iteration gone) — locked RMWs cost far more natively than their Ir share.
- **Derived, unverified:** the HIRES8 + HIRES9 SPU cuts together bring the synthetic job from ~15.8 k to ~8.7 k ns/iteration
  (−45 %). If the game's awaited job scales the same way, the main thread's ~3.9 ms SPU wait shrinks by ~1.7 ms per heavy
  frame; L's cuts reduce page-code instructions by ~14–17 % on memory kernels. Whether `main_ppu` reaches <= ~13 ms only the
  PC can show.

## Coordinator review (before the merges)

- L's branch (merged as 81dd09c): the one-compare range proof; every user of the old `inline_view.table`/`inline_flat.table`
  members (none left outside `table()`/`table_of()`; tests, bench and tools grepped); the reservation layout (table mapped
  read/write at the start of the PROT_NONE reservation, `reset()` unmaps both); the inactive base (one past a static all-0xff
  table: every guest page index < 2^20 reads a blocked byte, the data part is never reached, and the span test runs before the
  table read). Block heads: `head_bound()` is evaluated in `program.py` after every `emit_block` (one `PageEmitter`); the only
  dispatcher is the `anyps3_entry` loop, which recomputes `ppu_head_end` on every iteration (also in the nested module-loading
  entry), so every stored value is either bounded by K or admits exactly the block whose count it checked; a stale value can
  only cause an extra exit. The in-page `bl`/`bctrl` host calls re-read `ppu_head_end` in the callee prologue (same rule).
- N's branch (merged as e896fed): the untracked-write cut against every writer of `tracked` (`track_reservation` only, its
  five call sites hold the stripe) and every reader of `versions` (the two reservation-token paths, both after tracking the
  span) and of the touch counters (`observe` in the SPU lost-event filter and the worker's `ReservationWatch`, both on the
  SPU reservation line tracked at `getllar`); the DMA write holds the stripes (`LineLocks`) from the flag read through the
  copy. The shared chained-call scope: every emitted SPU function's first statement is `SpuBlockScope block(t)`, whose
  constructor takes and clears `t.chain_scope`, and chain callees are always emitted functions, so the pointer is consumed
  before anything can throw or return. LineLocks lazy clear: every word of the grown range is either cleared or already
  meaningful (gaps included). SHUFB: the flip touches only control bits 0..1. No textual conflict between the two branches
  (`par_memory.py` hunks are disjoint: L the inline view, N `atomic_read`/`atomic_write`/`LineLocks`/`leased_pages`).

## Regression tests

- **Agent L (tip 7dd2cf4, one module at a time, `nice -n 5`):**
  - New tests with independent expectations (literal big-endian bytes, ISA encodings, the budget rule "a block of c
    instructions entered at steps s runs iff s+c <= limit"):
    - `test_ppufast_memory`: span edges at every width, all 32 high bits, a blocked base, a blocker published mid-section,
      plus a `-mmovbe` class;
    - `test_ppufast_codegen`: a mid-run reservation blocker in translated code, an effective address above 4 GiB, both ISA
      variants;
    - `test_ppu_block`: head bound, budget, slice and reschedule through `anyps3_entry`.
  - OK: `test_ppufast_memory`, `test_ppufast_codegen`, `test_ppu_block`, `test_ppugen1`, `test_ppu_ret_predict`,
    `test_ppu_local_dispatch`, `test_ppu_atexit`, `test_ppu_rotate_count`, `test_ppu_spin_hint`, `test_ppu_string_trap`,
    `test_jump_tables`, `test_execution`, `test_anyps3`, `test_hlefast1`, `test_byte_reverse`, `test_floating`,
    `test_fp_special`, `test_par9_coordinator`, `test_integration_groups` (`test_par13_memory` at commit 2).
  - Mutation checks: every planted break of the five commits (mask, bound, blocked table, leave, asm width, head rules, byte
    hint) fails a test.
- **Agent N (tip 77ec2a5):**
  - New tests:
    - `test_spu_block`: 13 hand-written per-call chain cases in both scope forms, and SHUFB against SPU ISA v1.2 written out
      in Python;
    - `test_atomic_copy_unrolled`: alignments 0..15 × sizes 0..300, LineLocks against a `std::set`, tracked/untracked
      writes with the switch at both values.
  - On 505db6d four modules did not compile: a GCC 13 `-Warray-bounds` false positive under `-O2 -Werror`, fixed in 77ec2a5.
    On 77ec2a5, 21 SPU/memory modules are OK; `test_par2` stopped at the time budget with no failure. The rest of N's area
    list is covered by the full suite below.
  - All mutation checks were killed by behavioural assertions.
- **Full suite on the merge e896fed** (204 modules, `git archive` snapshot, one process per module, 3 in parallel,
  `nice -n 5`, `ANYPS3_REQUIRE_VULKAN=1`, lavapipe; ~8.9 h of module time, finished 2026-11-01 21:48 UTC): **188 OK,
  16 failing, no regression found.**
  - **All 16 fail exactly as in the HIRES7 suite** (same failing tests and the same FAIL/ERROR lines, compared mechanically
    with `cmp_fail.sh`). They are the container-only modules listed in [HANDOFF](../HANDOFF.md):
    - `local/` tools missing: `test_sample_profile`, `test_sample_profile_integration`, `test_spudyn_profile`,
      `test_spuperf2_capture`;
    - SDL offscreen: `test_present_window`;
    - GCC 13 `-Werror`: `test_ppu_fp_lazy`, `test_ppuperf3`, `test_savedata_vsh`, `test_rsx_residency`;
    - lavapipe cache asserts: `test_rsx_fp_cache`, `test_rsx_sampler_identity`, `test_rsx_resident_sampler`,
      `test_rsx_perf_decode`, `test_rsx_pipeline_cache`;
    - compile limits: `test_program`, `test_services`.
  - **8 HIRES7 failures now pass** with unchanged limits: `test_spufast_blocks`, `test_native_audio`,
    `test_native_audio_full`, `test_vulkan` (compile limits in HIRES7), `test_rsx_gpu_work`, `test_rsx_perf`,
    `test_spu_pool_poll`, `test_spuperf2_simd` (load-dependent in HIRES7). They vary with container load, so this is not
    evidence of a fix.
  - **Agent N's area:** all 20 modules it had not run are OK (`test_par2/4/6/8`, `test_spu`, `test_spu_gaps`,
    `test_spudyn_*`, `test_spufast2_blocks`, `test_spufast3_leaders`, `test_spuperf2_blocks`, `test_spureloc_split`,
    `test_spu_pool_quantum`, `test_spu_pool_poll`, `test_mainslack_reloc_index`, `test_guest_memory_rsxmem`,
    `test_rsx_subword_alias`, `test_rsx_gpu_batch`).
  - The one module new since HIRES7, `test_ppu_ret_predict`, is OK.
  - Not covered: anything that needs the game, the PC's NVIDIA driver or `local/`; the PC's suite remains the authority.

## Risks

- PPU: the step-budget stop for the last `max_count` steps is now thrown from `anyps3_entry` instead of inside the page
  (same PC, steps and message); page functions called without `anyps3_entry` (tests only) run with no budget or slice
  limit; the `asm volatile` ordering argument relies on GCC; MOVBE has about the uop count of mov+bswap (hypothesis:
  neutral or better natively). The bench budget needs a 64-bit immediate in the head compare; the game build's fits imm32.
- PPU: k_vmx native +3 % (it has no scalar access in its loop; HIRES8's K saw the same layout swing).
- SPU: the shared scope relies on every generated return path syncing `block.executed` (already required by the per-call
  commit) and on `t.chain_scope` being consumed by the callee's first statement (every emitted function's first statement
  is `SpuBlockScope block(t)`); the untracked-write cut relies on every `track_reservation` caller holding the stripe — a
  future caller without it would break the cut.
- SPU: AVX-512 generated code +2 865 bytes (+0.26 %): ~500 short branches became near branches (GCC block placement after
  the scope change, not more instructions; frame displacements refuted as the cause); v3 −414 bytes.

## First next actions on the real PC

Consolidated with the open items of every round into one ordered plan in
[CURRENT](../CURRENT.md#first-next-actions-on-the-pc-one-plan-replaces-the-per-round-lists); the HIRES9-specific items:


- One integration build of the branch tip; QUIET alternating 1440p walks against abaeba4 (HIRES8) or d1177e9 (HIRES7):
  `main_ppu` ms/frame in heavy frames, the main thread's SPU-wait share (delay 0x666068, job flag 0x5564d4) and the
  awaited lane's time, `PERF PPU_THREAD` ns/instr of thread 1, sampler shares of page code, `GuestMemory::transfer`,
  `ReservationNotifications::notify`, `spu_chain_call`.
- Same binary with `ANYPS3_SPU_CHAIN_SHARED=0`, with `ANYPS3_GUEST_WRITE_TRACKED_ONLY=0` and with
  `ANYPS3_PPU_RET_DEPTH=0` (HIRES8 cut B); the PPU memory/head cuts have no switch (A/B = the two builds).
- A run that hits `--max-steps` must stop exactly as before (message, PC, steps).
- Compile cost: total `.text` and wall time of the ~190 ppu-*.o and spu-*.o units vs the base.
