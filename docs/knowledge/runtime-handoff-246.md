# AnyPS3 handoff after 246

**Warning: later changes to the shared workspace.** During the final check, after 17:52 UTC, `anyps3/build_game.py`, `anyps3/program.py` and the profile changed (new `static_opd_section`); `native-virtual-247` artifacts also appeared. These changes are not the work of this run and are not verified by our 175 tests. Left untouched. The hashes/tests of this handoff refer to snapshot 246; the current worktree is no longer identical. Exact difference: `local/ratchet/handoff-246-worktree-drift.json`. The number 247 is no longer free. First verify the later changes and the state of any foreign processes; our session 6422 is terminated.

2026-10-14, Codex. The user cancelled the active automatic goal and asked to end
the work and hand it over completely to another AI agent. **The original menu of Ratchet & Clank:
A Crack in Time and gamepad navigation have not been reached.** If work
continues, preserve the entire original goal; do not label the loading screen as the menu.

## State to continue from

**Last actual semantic stop:** `local/ratchet/native-virtual-245`,
`pc=0x69dd30`, `lr=0x69dabc`, CTR `0x69dd30`, 308,156,479 PPU steps,
`target has no statically translated block`. It is case 13 of the original
fourteen-item switch. Registers r0=r27=13, r7=`0x69db38`,
r10=`0x34`, r3=r6=`0x1f8`, TOC r2=`0xd34a88`, r30=`0xcacf80`.
FPSCR `0x86002000` contains the sticky ZX: the original game really executed a
supported FP division by zero. The specific preceding FP PC is not directly traced.
Its own `verification.json` and the independent `terminal-identity.json` were verified.

**Prepared fix for this stop:** profile 246 contains the exactly verified table
`0x69db38`, count 14, dispatch `0x69db34`, 12 distinct target blocks and 12
new seeds. The base/TOC, the unsigned-byte bound 13, the signed big-endian int32
offsets and all original executable targets were verified. Case 13 `0x69dd30`
is translated in the new C++, **but its execution by game 246 is not yet verified**.

**The latest game 246 run was interrupted on user instruction.** Session 6422 was terminated
with Ctrl-C, wrapper exit 130. The direct guest return code was not recorded and cannot
be confused with 130. No `STOP pc=...`, no normal `verification.json`.
Last state: 266 reads, 13,048,193 bytes, draw 11520, 17 captures;
the last captured frame `native-virtual-246/frame-000017.png` is still the loading planet.
The controller was neutral the whole time; the game reported `connected=1 backend=SDL2`.

`native-virtual-246/interruption.json` independently verifies the original PT_LOAD,
the input and diagnostic hashes, the current source/profile, the byte-identical base C++,
the preserved logs and all 17 PPMs. It is a **verification of the interrupted run**, not a normal
terminal runtime or menu evidence. `TERMINATED.txt` marks the interruption.

**No build/run handle that we started is live any more.** 6422 exit 130;
65441/34840 compilation/build 246 exit 0; 6682 game 245 terminated;
50860/10130 compilation/tests 245 exit 0. The old observation exec cells are
finished; no subagent was assigned. `get_goal` returned `goal=null` at the handoff.
A separate shell `ps` in this environment has its own PID namespace and is not
reliable evidence of the state of other tool sessions. Do not use old logs as a
substitute for a live handle. The Git metadata is empty/unusable; no new
repository or commit was created.

## What has changed since 237

### Runtime and tests

1. `anyps3/rsx_draw.py`: a single-instruction FP MOV from an inline constant is evaluated
   once through the existing interpreter. It preserves coverage/top-left, clipping,
   depth/alpha, blending, masks and actual guest writes; other shaders go
   through the general path. The native regression `tests/test_rsx.py:250` verifies independent
   colors and pixels as well as the general branch. Synthetic benchmark of 20 draws at 1024×512:
   611,131 → 267,785 µs, ~2.28×. **This is not the speed of the whole game.**
2. `anyps3/floating.py`: division and add/sub support infinities; finite/zero
   sets ZX and the sign XOR, 0/0 and Infinity/Infinity set VXZDZ/VXIDI,
   opposite infinities in add/sub set VXISI. Correct FR/FI/FPRF, sticky FX
   on the 0→1 transition of the specific exception bit. The original finite algorithm is
   unchanged; unsupported rounding/NI/enabled traps still stop explicitly.
   **Other instructions with infinities are not generally solved by this.**
3. `tests/test_floating.py:34`: literal IEEE/FPSCR vectors, both precision
   modes, signs, subnormal/zero, NaN precedence, invalid cases, sticky
   flags, record CR1, destination aliases, rejection of unsupported modes without
   changing state. The exact original sequence of five FRSP/FDIVS/FSUB/FSEL/STFS from
   `0x69f2b0..0x69f2c0` correctly stores the guest big-endian `0x3f400000` (0.75).
   Before the fix, the same test failed on the original division-by-zero stop.

The original PPU/SPU bytes, the ELF and the game assets were not patched. No
substitute menu or foreign emulator was inserted. The renderer remains our own software RSX
implementation with our own Vulkan readback runtime.

### Profile and origin of static targets

| Build | Verified origin | New seeds |
| --- | --- | ---: |
| 240 | vtable `0xc53248`, 14 original OPDs, TOC `0xd34a88` | 14 |
| 241 | vtable `0xc53208`, 14 OPDs | 11 |
| 242 | original six-item ctor `0x57765c`, four remaining vtables | 42 |
| 244 | seven original callback registrations `0x32c40c..0x32c464` | 7 |
| 246 | internal byte-indexed switch `0x69db38`, 14 slots / 12 targets | 12 |

In total **86 unique new profile seeds against snapshot 237**. The number of
seeds is not the number of newly executed functions nor evidence that the whole loader is complete.
Six vtables: `0xc53248`, `0xc53208`, `0xc53140`, `0xc531c8`, `0xc53188`,
`0xc53100`; 85 verified OPD slots in total from the original constructor.
Seven callbacks: `0x53a244`, `0x53a644`, `0x53a520`, `0x53be88`,
`0x94f3b4`, `0x4ec0f4`, `0x4ebecc`, installed into the loader control block
`0xf46da4`. Detailed load/store/OPD/TOC/code evidence below.

### Virtual gamepad and local tools

`local/ratchet/tools/virtual_pad.py`: an explicit SDL2 diagnostic wrapper.
It copies the original translation byte for byte into `program.base.cpp`, renames the host main
via a macro and attaches a real SDL virtual device. The original
`NativePad` and guest `cellPad` interfaces remain the input path. Thread SDL
lock, stable instance-ID cleanup, atomic control file; errors stop
explicitly, it does not invent neutralization or a connected physical controller.

Control format `mask LX LY RX RY LT RT`, neutral
`0 0 0 0 0 -32768 -32768`. Names down/up/left/right, cross/circle/square/
triangle, start/select, l1/r1/l3/r3. Separate native tests verified down+cross,
stick ±32767/−32768 and neutral 128, L2 pressure 255, big-endian data of length 20,
release and lifecycle; five erroneous input cases. **Menu navigation not verified,
physical gamepad not verified.** Runs 244–246 were neutral only; no button
sequence has been performed in their menu yet.

Other local tools/evidence: `observe_run.py` (reads the log, does not by itself
prove a live process), `verify_loader_vtables.py`, `verify_loader_callbacks.py`,
`add_vtable.py`, `add_jump_table.py`, `benchmark_constant_draw.py`,
`ppm2png.py` and `rsx_vp_disasm.py`. The reference repositories were not changed.

## Real runs and unsuccessful attempts

| Run | Result | PPU steps | Reads / bytes | Captured frames |
| --- | --- | ---: | --- | ---: |
| 238 | untranslated `0x962700`, LR `0x577e00`, game rc 1 | 274,293,424 | 355 / 16,463,030 | 211 |
| 239 | compiled, game not run; `NOT_RUN.txt` | — | — | — |
| 240 | second type `0x9626a4`, LR `0x577e00`, r31=4, rc 1 | 274,249,019 | 355 / 16,463,030 | 28 |
| 241 | third type `0x962404`, LR `0x577e00`, r31=8, rc 1 | 274,258,927 | 355 / 16,463,030 | 28 |
| 242 | allocation callback `0x53a244`, LR `0x57a7e0`, rc 1 | 274,257,993 | 355 / 16,463,030 | 28 |
| 243 | premature build with the old profile interrupted, wrapper 130; game did not run | — | — | — |
| 244 | unsupported FP zero-divide `0x69f2b4`, LR `0x69f060`, rc 1 | 308,205,078 | 444 / 17,897,130 | 33 |
| 245 | new internal case `0x69dd30`, LR `0x69dabc`, rc 1 | 308,156,479 | 444 / 17,897,130 | 33 |
| 246 | user interruption Ctrl-C, wrapper 130, guest rc unknown | unknown | 266 / 13,048,193 | 17 |

244/245 opened both original `packed/levels/front_end/level_{cached,uncached}.psarc`.
SPU 0x100 in both 878,746,602 instructions, waiting event, no semantic SPU stop.
245: 15,252 flips / 30,496 draws / presentation to the window 0. All visually
verified last images are still the loading planet. Captures/clears/flips are not the menu.
239 was not run in the game; the tested optimization has actually been used from 240 onward.
`native-virtual-242` is a compiled old diagnostic device, the game was never
run, `NOT_RUN.txt`; do not use it in place of the current 246.

## Tests and source identity

| Verification | Result / log |
| --- | --- |
| RSX targeted 239 | 6 tests OK, 10.481 s; `tests-rsx-239.log` |
| Full suite 239 | 174 OK, 205.634 s, Vulkan required, no skips; `tests-native-239.log` |
| FP new test before the fix | expected failure at the original stop; `tests-fp-245-before.log` |
| FP final 245 | 6 OK, 10.194 s; `tests-fp-245-final.log` |
| Full suite 245 | **175 OK**, 202.918 s, Vulkan required, no skips; `tests-native-245.log`, `tests-native-245-result.json` |
| Virtual pad | `virtual-pad-selftest-2.log`, manifest/sources/binary in `virtual-pad-selftest-2/` |
| Pad errors | 5 cases; `virtual-pad-selftest-1/error-checks.json` |

No runtime semantic change since the successful full suite 245; 246 has identical source
hashes. Do not repeat tests without a change or a concrete doubt. Merely finishing
the documentation does not require another build/test. Hash inventory of all anyps3 sources,
tests and helpers used: `local/ratchet/handoff-246-source-hashes.json`.

| File / image | SHA256 |
| --- | --- |
| Original `EBOOT.analysis.elf` | `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1` |
| `profiles/ratchet-acit.json` | `ab6d1c4fe7067ae93608ede9b034f261334e8ad1c88ba36ba714b1a34bb81db7` |
| `anyps3/rsx_draw.py` | `70ef9fff4439764ce625009fc90ce582fec28499ee0e3d1faf4b0c79fc825fec` |
| `anyps3/floating.py` | `e295daf36fc5155ae1d357931e6102322c28a67b939eacbdc37542bf2597dcab` |

The normal `run_native.py` verification hash does not include the base C++; always
additionally verify `diagnostic-manifest.json` and the base against the original bundle translation.
The menu flags in the helper are hard-coded false; with a real menu it is not enough to flip the
flag, you must demonstrate the original visible content and the navigation changes via `cellPad`.

## First next actions and commands for a new unused N

1. After a new user authorization, read AGENTS, INDEX, CURRENT, this handoff;
   check the current files/hashes/profile/handles. **There is no live run now.**
2. In a new directory, execute the original game again with the prepared profile 246.
   Choose the number according to the current artifacts; 247 is already taken.
   Previous `frames/` are not overwritten: the helper requires a new directory.
3. Confirm the passage of case 13 and address the next actual stop. Loading in our
   last runs took roughly tens of minutes; do not interrupt after a mere
   observation timeout. The budget remains 5 billion PPU/SPU, which is not an ISA fix.

Replace N below with an actually free number only after checking later changes. The template was not executed.

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 5000000000 -o local/ratchet/native-bundle-N > local/ratchet/build-N.log 2>&1
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-N local/ratchet/native-virtual-N > local/ratchet/build-virtual-N.log 2>&1
ANYPS3_VIRTUAL_PAD_CONTROL=/home/martink/AnyPS3/local/ratchet/native-virtual-N/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-N 5000000000 > local/ratchet/run-virtual-N.log 2>&1
python local/ratchet/tools/observe_run.py native-virtual-N --record
```

The wrapper compiles `c++ -std=c++17 -O2 -Wall -Wextra -Werror -pthread
-DANYPS3_VULKAN ... -lvulkan` into `native-virtual-N/program`; the original
`native-bundle-N` contains the generated C++/ELF/profile/build and has no
compiled program of its own. `run_native.py` currently sets the real disc
`game-complete`, cache, HDD, SPU budget, Lavapipe and Vulkan validation.
Re-verify its content. Preserve stdout/stderr/returncode/stop/captures.

Once the real menu is reached, buttons can be sent through SDL and then released (the commands
have not yet been executed in the game menu):

```bash
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-N/control.txt --buttons down
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-N/control.txt
```

Demonstrate the change of selection in the original menu, return/activation as needed, the origin of the
input and before/after captured frames. Separate virtual navigation, the physical gamepad
and presentation to the window. **The window presentation backend is still missing**; the current
Vulkan path is headless scanout/readback/capture. The audio backend is also missing.
Further limitations of shaders/textures/stencil/MRT are in the older RSX experiment.

## Last in-progress research – do not use as a finished fix

A read-only scan of 64 translated original BCTRs found further byte-shift patterns;
`local/ratchet/translated-byte-switch-candidates-246.json` is only a catalog of
**hypotheses/candidates**, with no change to the profile/generator. The TOC/register flow
is not fully proven, nor is the correct count of all its tables.

| Dispatch / candidate table | Candidate count | Status |
| --- | ---: | --- |
| `0x50c774` / `0x50c778` | 6 | targets untranslated in 246, further verification needed |
| `0x50d810` / `0x50d814` | 5 | same |
| `0x7d87a4` / `0x7d87a8` | 5 | same |
| `0x9b52fc` / `0x9b5300` | 10 | same |
| `0x5f99a8`, `0x6dc070`, `0x90acd8`, `0x91628c` | unproven | a regex/pattern alone is not enough |

The known `0x69db38` and the older `0x90a294` are already in the profile. The automatic detector
remains strict canonical `rldic`; it was not extended to `rlwinm`. Do not add these candidates
without verifying the original guard/base/offset/count/targets and identity.

## Index of evidence and history

- [238: original actual stop and SPU](experiments/2026-10-14-loading-238.md)
- [239: renderer, benchmark, tests](experiments/2026-10-14-constant-fragment-239.md)
- [240: first vtable](experiments/2026-10-14-vtable-240.md)
- [241/242: six vtables](experiments/2026-10-14-loader-vtables-241-242.md)
- [244: callbacks, virtual pad, actual FP stop](experiments/2026-10-14-virtual-pad-and-callbacks-244.md)
- [245: FP semantics and regressions](experiments/2026-10-14-fp-zero-divide-245.md)
- [246: table and interruption](experiments/2026-10-14-byte-switch-246.md)
- [Previous progress 198–237](runtime-handoff-237.md), [brief history](../menu-progress.md)
- [RSX semantics and limitations](experiments/2026-10-14-rsx-software-draw.md),
  [PRX/trophy/PPU details](experiments/2026-10-14-prx-import-references.md)
- [Primary sources, exact paths and revisions of the references](SOURCES.md)

Evidence files in `local/ratchet/`: `target-240-evidence.txt`,
`target-241-evidence.txt`, `loader-vtables-242-evidence.json`,
`target-244-evidence.json`, `target-245-evidence.json`, `target-246-evidence.json`.
The original firmware 3.01 and the game images must stay the same; no silent substitutions.
The primary PDF library records versions/hash/page mapping. Book I v2.02 was
available via the web during the FP research; the local download failed on DNS and the screenshots
were mostly cache misses; **there is no new local Book I PDF hash**. Do not adopt
newer Power ISA or PowerXCell details as automatically valid for the PS3.
