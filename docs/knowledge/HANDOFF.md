# Handoff to the next instance

## Cloud round HIRES4 — 2026-10-30/31 (cloud container; goal NOT reached, nothing measured in the game)

Branch `ccr-b1a3987e-aypb5o` (pushed; HIRES3 de6bc4f + merges of `cloud/hires4-fifo` (fast-forward, b602751),
`cloud/hires4-draw` (ceab1ba) and `cloud/hires4-mem` (b694994)). Read the [record](experiments/2026-10-30-cloud-hires4.md):
per-draw cuts (agent A), FIFO decode/non-draw cuts (agent B), renderer guest-memory admission (agent C), the combined
callgrind table, the suite result and the coordinator's review notes. The agent branches remain local to the cloud
container (not pushed); their commits are all in the pushed branch. No process of this round is live.

First next actions on the real PC: (1) one integration build; QUIET alternating 1440p walks vs 1031/1034 — rsx_worker
ms/frame and µs/draw in heavy frames, main-thread RsxWait, `PERF GUEST_MEMORY`; (2) the same binary with
`ANYPS3_RSX_RENDER_BATCH=0`, then all three memory switches 0 (`ANYPS3_GUEST_PAGE_READ`, `ANYPS3_RSX_RENDER_BATCH`,
`ANYPS3_GUEST_RANGE_ADMISSION`), and `ANYPS3_RSX_FIFO_PAIR=0`; (3) GCC 15 compile time/size; (4) still open from HIRES3:
pool A/B, 1035 MSAA, the recorder teal-frame check (if clean, recorder on by default in runtime and GUI); (5) if the worker
is still > 13 ms, the per-draw designs in the record and HIRES3's two-thread worker.

## Cloud round HIRES3 — 2026-10-28 (cloud container; goal NOT reached, nothing measured in the game)

Branch `ccr-8139488c-79xg7b` (pushed; based on main 95756e2). Read the
[record](experiments/2026-10-28-cloud-hires3.md): device-memory pool, DrawJob borrowed pointers, report floors,
vertex-constant chunks, typed-RTT literals, `bench/rsx_fifo_bench.py`. Cloud items done from the 2026-10-27 list: 1 (suite: 17 pre-existing failures also on base, 1 regression fixed;
see record), 2 (three of the five per-draw cuts; FIFO decode measured and left unchanged), 5 (pool). Not started: 3
(two-thread worker; the record adds the derived constraint that a decode-only front saves ~1 µs/draw), 4 (visual MSAA load
path, constants via UBO, pass-begin cost — finding documented), 6 (horizontal 8/3). No process of this round is live.

First next actions on the real PC: (1) one integration build of the branch; QUIET alternating 1440p walks against
1031/1034; the same binary with `ANYPS3_RSX_MEMORY_POOL=0`; (2) full frame ring + `hitch_marks.py` to see whether stutter
classes A/B disappear (B persisting refutes the first-touch hypothesis); (3) scale-1 walks for rsx_worker ms/frame and
µs/draw; (4) `ANYPS3_RSX_RTT_LITERALS=0/1` with `ANYPS3_RSX_VK_MARKS`.

## HIRES2 wave — 2026-10-27 evening (ended on user instruction; goal NOT reached)

Read the top of [CURRENT.md](CURRENT.md) and the
[HIRES2 record](experiments/2026-10-25-hires-wave.md#hires2-wave-result-2026-10-27-evening-coordinator-claude-opus-55-agents-opus-55-high-goal-not-reached).
Branch `claude/hires2-wave` merges `fast2/hires2w` + `fast2/hires2s` into main f92681c (no overlapping files). Rules
for the agents: `local/ratchet/work/COMMON-HIRES2.md`; agent handoffs (with exact commands and tools):
`local/ratchet/work/hires2w/RESULT.md` (instance 2 section), `local/ratchet/work/hires2s/RESULT.md` (instance 5 section).
On the user's instruction all queued detached builds/runs were stopped at session end; runs cut short are invalid
(listed in the record). No agent, build or game run is live.

First next actions: (0) repeat 1002 vs 960 alternating at scale 1 (1002a1s1 looked slower, probably disturbed — must be
resolved before merging cf7a6ef to main) and measure the already-built 1035 (MSAA on/off) vs 1031; (1) build the merged branch as one integration build and measure it alternating with 1031 at scale 2
(QUIET, `work/hires2s/measure-walk-*.sh` pattern), then with `ANYPS3_RENDER_SCALE_MSAA=1`; (2) main-thread SPU-job wait
(~3.9 ms/heavy frame): analyse `work/hires2w/tools/wallsplit.py 1000w1s1` (what the SPU lanes run while thread 1 spins; a first attempt
under `timeout 1500` produced no output — run it without the timeout, redirected to `work/hires2w/logs/`);
(3) RSX worker heavy frames 21 ms → ≤ 13 ms (ranked cuts in hires2w/RESULT.md, or a front/back thread split);
(4) GPU at scale 2: second sample plane via MSAA, native pass-begin cost; (5) attribute the 140–180 ms walking hitches
with a full frame ring (not pipeline compiles). Open user decision: horizontal scale 8/3 for full 2560-column detail.

### Cloud instance next steps (no game data, no NVIDIA GPU; lavapipe + validation only) — 2026-10-27

Status 2026-10-31: items 1 and 5 done in HIRES3; item 2 largely done (HIRES3: DrawJob borrowed pointers, report floors;
HIRES4: FIFO decode and actions, renderer guest-memory admission; the texture-unit memo is in HIRES5); items 3, 4 and 6 open.

A cloud instance cannot run the game or measure FPS. It should deliver **exact, gated, unit-tested source changes on its
own `claude/*` branch based on `claude/hires2-wave`**, each with a native regression test (independent expectation) and
a micro-benchmark where it targets CPU cost; the real PC then builds and measures them alternating. Priorities, from the
verified HIRES2 evidence (`local/ratchet/work/hires2{w,s}/RESULT.md` exist only on the PC; the record
`experiments/2026-10-25-hires-wave.md` has the numbers):

1. **Full regression suite on `claude/hires2-wave` (lavapipe).** Only targeted modules ran after the merge. Report
   failures with the first differing value; fix or rebut each.
2. **RSX worker per-draw CPU cuts (target: heavy frames 21 → ≤ 13 ms, ~9.1 µs/draw marginal; each cut is ~0.3–1 ms):**
   - per-draw `shared_ptr` / `GraphicsImage` array copies into `graphics_draw`/DrawJob (~5.4 % of the worker): pass
     borrowed references or a pooled draw descriptor; keep lifetimes explicit (the recorder thread consumes them);
   - `texture_image` per-unit memo (like the texture-metadata memo 7375cb6, generation-bumped on erase/clear) and caching
     the fragment-shader string that `rtt_image` rebuilds;
   - batch label/report publication (~230 occlusion reports/frame: Label construction, vector growth, GpuPages claim per
     publish) into one publish per FIFO run, keeping guest-visible order and values exact;
   - `rsx_fifo_values` FIFO word reads (~2.6 %) and the decode of non-draw methods (~23 % of heavy-frame worker time):
     add a per-method cost counter (gated) and a faster register-only path;
   - clears (~2.8 %).
3. **Design + prototype behind a switch: a two-thread RSX worker (front: FIFO decode/state/guest reads; back:
   prepare/record).** Estimated alone to leave ~16–17 ms in heavy frames, so it combines with item 2. Write the ordering
   rules (Session, guest-memory admission, labels/reports, HostObserver) first; tests must cover label/report ordering
   and guest-visible memory under concurrent writes.
4. **GPU at scale 2 (correctness testable on lavapipe):**
   - visual 2× MSAA (a57960b): add the plane→sample load path so a target that is not fully cleared each frame can still
     use the working image (lavapipe lacks 2× MSAA — test the fallback path there, mark the NVIDIA check as pending);
   - typed-RTT raw-byte decoder shader: per-byte integer div/mod addressing at 3840×1408 — restructure (exact output,
     compare against the current shader byte-for-byte on lavapipe);
   - shader constants read through a storage buffer with dependent loads → uniform-buffer layout (exact; image tests);
   - native render-pass begin costs 12.6 µs vs 1.55 µs visual (1031sm1): read the code for per-begin framebuffer/image
     work, load ops and barriers; propose a fix with a test.
5. **Stutter class A/B (Vulkan device memory, testable on lavapipe):** the 384 MB memory cache in
   `rsx_vk_resources.py` reuses exact-size blocks only and holds ~4 visual images at scale 2; frees evict via
   vkFreeMemory (up to 16.8 ms per call) and ~240 new allocations occur in streaming frames. Implement sub-allocation
   from large blocks (size classes / a buddy or linear allocator per memory type) with bounded, deferred frees off the
   worker's critical path; unit-test aliasing/lifetime with fences and the allocation counts.
6. **Design note only (user decision pending): horizontal scale 8/3** so 1440p shows 2560 genuine columns (today the
   960×704 scene renders at 1920×1408 and is resampled 4:3). Describe what anisotropic visual companions would change
   (sample grid, typed RTT, history copies, presentation) and the expected GPU cost.

Not suitable for the cloud: the main-thread SPU-job wait (needs the game and the `wallsplit.py` analysis of
`1000w1s1`), PPU codegen changes that need the original ELF, the 1002/1035 measurements and stutter attribution.

## Older handoffs

- 2026-10-21 to 2026-10-25 (wave 6, resolution scaling, cloud follow-up, FIX7, performance milestone, HIRES wave):
  [history/HANDOFF-2026-10-21-to-10-25.md](history/HANDOFF-2026-10-21-to-10-25.md) (moved 2026-10-31).
- FAST2 wave 4, installer, manager worktree, the sandbox assessment and the handoffs of 2026-10-14 to 2026-10-20:
  [history/HANDOFF-2026-10-14-to-10-20.md](history/HANDOFF-2026-10-14-to-10-20.md).
- Runtime handoffs 198–246 (superseded): [246](history/runtime-handoff-246.md), [237](history/runtime-handoff-237.md),
  [198](history/runtime-handoff-198.md). Full previous wording of this file: `local/ratchet/HANDOFF-before-cleanup-2026-10-14.md`.

## Template for an experiment or handoff

```text
Date and author/model:
Goal and completion criterion:
Source state: commit + uncommitted files/hashes
Input: profile, game identification, relevant configuration
Verified fact: what exactly was observed
Sources: file:line @ commit / PDF title-version page N + SHA256
Runtime: exact command, log, exit/timeout, artifacts and counters
Changes: what is done and where
Hypothesis: reason, alternative, distinguishing test
Refuted: attempt and reason
Open: what we do not know yet
Next step: concrete first action
Limitations: what the test does not cover
```

## Check before takeover

- The paths actually exist; the reference checkout has a recorded revision.
- A fact comes from a specific source or a runtime artifact.
- The result matches the criterion of the original menu and gamepad; a diagnostic presentation is not mistaken for guest rendering.
- Failed attempts and hypotheses are visibly separated from confirmed behavior.
- The next step addresses the real last blocker, not a long-removed error.
