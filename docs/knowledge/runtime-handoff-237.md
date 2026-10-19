# Runtime handoff after build 237

Date: 2026-10-14, Claude Opus 5.5. Work interrupted on user instruction for handoff to another AI agent.
**The goal (original ACIT menu + gamepad navigation) has not been met.** The game is at the loading screen stage.

## Verified progress relative to build 198

1. Stop `0x1b8` removed: linking of the PRX import ref tables (attribute `0x2000`), `anyps3/prx.py`, `build_game.py`.
2. Native VSH trophy service (NPTR `f`,`8`,`o`,`1`,`9`,`h`,`i`,`term`), installation of TRP on the HDD, VSH events 299 via
   the sysutil queue with deferred delivery (`anyps3/sysutil_transport.py`, `events.py`).
3. The SPU runs during `sys_timer_usleep` (3200 instr./µs) and during the vsync wait in flip (`services.py`, `rsx.py`).
4. PPU: `fsqrt`, single-precision arithmetic from full double operands (implementation hypothesis), `stfs` = Power ISA SINGLE,
   `lwbrx/lhbrx/stwbrx/sthbrx`, `sys_lwmutex_destroy`.
5. Static targets: vtables `0xc3c428`, `0xc3c378`, statusCb `0xcd3b38`, trophy handlers 299/310, jump tables; automatic
   verified detection of switch tables (`anyps3/jump_tables.py`, `build.json` → `verified_jump_tables`). Translation budget 1,000,000.
6. **RSX software renderer** (`anyps3/rsx_draw.py`): VP/FP interpreter, textures (A8R8G8B8, DXT1/3/5, mipmaps, gamma,
   border texels), quad-lockstep derivatives, depth/alpha/blend. Detail: [experiments/2026-10-14-rsx-software-draw.md](experiments/2026-10-14-rsx-software-draw.md).
7. First guest-rendered image: `local/ratchet/native-bundle-228/frame-000005.png` (loading icon). Run 231/235:
   thousands of flips with an animated icon.

## Last real runs

| Build | Result |
| --- | --- |
| 235 (`-O2`) | stop `SPU execution step limit reached` (SPU 0x100, 500 M), 211,272,704 PPU steps, flips=5998, draws=11988, 107 captured frames |
| 236 (+SPU at vsync) | same stop at the same point (211,272,448 steps, flips=5999) |
| 237 (+budgets of 5 billion) | **interrupted on user instruction** (SIGKILL, returncode −9) after ~4 min: loading continued (221 reads, draw 4608) |
| 227, 232 | compilation failed (warning `misleading-indentation`), kept as failed artifacts |
| 233, 234 | deliberately terminated, superseded by faster builds (see `TERMINATED.txt`) |

## First next action

Create a new build number (238+) and let it run to completion with budgets of 5 billion (`local/ratchet/tools/cycle.sh N`, compiles with `-O2`).
Loading is long (~SPU decompression, file reads in chunks of ~45 KB); watch for the next real stop (probably new RSX
methods/formats, video/audio modules or the menu). Once the menu is reached, verify input via `cellPad` (the game already calls
`cellPadGetData`/`cellPadGetInfo2`).

## Reproduction

```bash
local/ratchet/tools/cycle.sh 238          # build + c++ -O2 + run_native.py with budgets 5e9
python3 local/ratchet/tools/ppm2png.py local/ratchet/native-bundle-238/frames/frame-000005.ppm out.png
python3 local/ratchet/tools/rsx_vp_disasm.py local/ratchet/native-bundle-218/stderr.log   # VP disasm from diagnostics
PYTHONPATH=. python local/ratchet/tools/add_vtable.py VTABLE SITE BUNDLE   # verified vtable into the profile
PYTHONPATH=. python local/ratchet/tools/add_jump_table.py BASE COUNT DISPATCH "evidence"
```

On an unsupported RSX method, stderr contains a state snapshot (`RSX state`, `RSX vp[]`, `RSX vc[]`, `RSX fp[]`).
The program ignores SIGTERM; terminate the run via PID with `kill -9` (beware: `pkill -f` also kills your own shell).

## Tests

Targeted suites passed along the way (logs `local/ratchet/tests-*-199…237.log`). Full regression suite with mandatory Vulkan:
`local/ratchet/tests-native-237.log`: **173 tests, 188.96 s, OK** (`ANYPS3_REQUIRE_VULKAN=1`, run after all runs had been terminated).

## Limitations and hypotheses

See the experiment records: H/R aliasing order, z-clip versus clip min/max, gamma before the filter, single-precision operands,
w≤0 clipping unsupported, swizzled uncompressed textures, stencil, MRT, zpass reports (no pixel counting yet).
The audio backend is missing. The profile `profiles/ratchet-acit.json` was extended (vtables, callbacks, jump tables, firmware seeds).
A Git repository is not available; no commit was made.
