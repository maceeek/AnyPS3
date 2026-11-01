# Loader callbacks and real SDL virtual input (244)

Date: 2026-10-14. The original ACIT menu and gamepad navigation are not yet verified.

## Final verified run 242

Session 31385 terminated, wrapper exit 0, game returncode 1. New stop
`pc=0x53a244`, `lr=0x57a7e0`, 274,257,993 PPU steps. 355 reads,
16,463,030 bytes, SPU 0x100 755,183,648 instructions and waiting for an event,
12,232 flips, 24,456 draws, 28 captured frames, presentation to the window 0.
The own `verification.json` of 242 and all six artifact hashes were verified
again; both original PT_LOAD segments match.

The loader reached the body of the third type after the readiness method `0x962404`.
R31=8, r8=`0xc53140`; this is not yet the completion of all six types.
R9=`0xd0efb0`, r6/CTR=`0x53a244`, TOC=`0xd34a88`, r7=`0xf46db0`.
The original `0x57a7a0..0x57a7dc` assembles the arguments count=`0x136`,
element-size=4, flags=`0x20` and calls the callback through this original OPD.

## Verified callbacks and profile 244

The original initialization `0x32c40c..0x32c464` loads seven OPDs from the constant-pool
cells `0xc94440..0xc94458` and calls the individual setters. These install them
into the control block `0xf46da4` at offsets +0xc, +0x10, +0x14, +0x1c,
+0x24, +0x28, +0x2c. The first corresponds to the currently reached field `0xf46db0`.
This is not a guess based on neighboring OPDs: the evidence contains the original load, BL,
setter, TOC/bias and store for each item.

```bash
PYTHONPATH=. python local/ratchet/tools/verify_loader_callbacks.py --apply > local/ratchet/target-244-evidence.json 2> local/ratchet/callbacks-244.log
```

The pinned original ELF SHA-256
`6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`,
all seven OPDs, TOC `0xd34a88` and executable code bytes were verified. The profile contains a
new `static_function_tables` item with its origin and an actually observed
`observed_indirect_entries` record. Seven seeds added: `0x53a244`,
`0x53a644`, `0x53a520`, `0x53be88`, `0x94f3b4`, `0x4ec0f4`, `0x4ebecc`.
The other callbacks are verified from the registration, not passed off as executed.
The game bytes are not changed, no new host services were introduced.

243 was started before the profile check succeeded and was interrupted via session
63268, exit 130. A faulty helper assert required only the callee successor,
whereas BL has both the callee and the return continuation. It failed before the profile was modified;
243 contains the old profile and the game did not run in it. The artifacts are preserved,
`native-bundle-243/TERMINATED.txt`. The corrected check verifies both targets.

## Diagnostic virtual gamepad

`local/ratchet/tools/virtual_pad.py` creates an explicitly opt-in diagnostic
wrapper. It copies the original translated C++ as `program.base.cpp` without changing
bytes; it renames only the host main via the preprocessor and before calling it
attaches an SDL2 virtual GameController. It does not patch or replace any guest code, memory, menu or
asset. The normal runtime SDL backend remains the
source of `cellPad` results. `diagnostic-manifest.json` records the origin and
the wrapper/base/binary hashes; the original build metadata is additionally preserved.

Control file: seven decimal fields `mask LX LY RX RY LT RT`; 15 SDL
buttons, six int16 axes. Neutral state: `0 0 0 0 0 -32768 -32768`.
The subcommand `set` ensures an atomic write. The buttons have the names down/up/left/right,
cross/circle/square/triangle, start/select, l1/r1/l3/r3. Invalid data
explicitly stops the diagnostics; no implicit neutralization on error.

Source API: the installed `/usr/include/SDL2/SDL_joystick.h:132` (locking),
`:370` (attach), `:467` (axis), `:485` (button); header version 2.32.74
from `SDL_version.h:59`. [SDL2 LockJoysticks](https://wiki.libsdl.org/SDL2/SDL_LockJoysticks)
requires a lock when accessed from multiple threads, [SetVirtualButton](https://wiki.libsdl.org/SDL2/SDL_JoystickSetVirtualButton)
publishes the set values at the next joystick update. The input worker uses the
SDL lock and the values are consumed by the original `NativePad::poll`. On cleanup
it looks up the device by its stable instance ID, not by the old index.

```bash
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py self-test local/ratchet/virtual-pad-selftest-2
```

Verified natively: init/connected of the virtual device, exact guest big-endian
`cellPad` button words (down+cross), left stick +32767→255 and −32768→0,
neutral 128, L2 pressure 255, length 20, release and lifecycle. Log
`local/ratchet/virtual-pad-selftest-2.log`, sources/binary/manifest in the directory of the same
name. **This is an input test, not the menu or a physical gamepad.**
The earlier selftest-1 also verified five error cases (mask out of range,
missing axis, trailing token, missing opt-in, unknown name without overwriting the
control file): `virtual-pad-selftest-1/error-checks.json`. The reader has not changed since
then; the lock/index lifecycle changed and passed selftest-2 again.

## New build and the real game in progress

The runtime hash set corresponds to the successful whole suite of 174 tests with mandatory Vulkan
from 239; no further runtime changes were made. Repeating this suite was not needed.
The new profile tests the real following run.

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 5000000000 -o local/ratchet/native-bundle-244 > local/ratchet/build-244.log 2>&1
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-244 local/ratchet/native-virtual-244 > local/ratchet/build-virtual-244.log 2>&1
ANYPS3_VIRTUAL_PAD_CONTROL=/home/martink/AnyPS3/local/ratchet/native-virtual-244/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-244 5000000000 > local/ratchet/run-virtual-244.log 2>&1
```

`native-bundle-244` contains the original new build/snapshot, it is not separately
compiled or run. The compiled and running program is in
`native-virtual-244`; compiler flags `-O2 -pthread -DANYPS3_VULKAN -lvulkan`.
Session 60472 confirmed alive. At 16:42:15 UTC 212 reads, 10,907,854 bytes,
draw 3328, 10 frames. The real game reports `ports=7 connected=1 backend=SDL2`.
The wrapper/base/binary hashes and the match of base with the original `program.cpp` were verified.
The current runtime sources and profile correspond to 244; the final verification is still missing.
Passing `0x53a244`, the menu and navigation with the virtual controller are not yet verified;
a physical gamepad was not tested. The run so far renders the loading icon.

After the menu is reached, the input can be changed without patching the run:

```bash
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-244/control.txt --buttons down
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-244/control.txt
```

These commands have not been used in the game run so far; they are only a verification tool.
First next action: poll the **same session 60472**, read
`python local/ratchet/tools/observe_run.py native-virtual-244 --record`,
verify the own final `verification.json` together with the diagnostic manifest
and address the real next stop. The old `native-virtual-242` was never run,
has its own `NOT_RUN.txt` and lacks both the current profile and the lifecycle fix.

At 16:46:23 UTC 262 reads, 12,858,941 bytes read, draw 10752,
17 frames. `frame-000005.png` visually confirms the original loading planet.
The own `live-identity.json` compares the original PT_LOAD, the source/profile hashes,
the base translation with the original bundle 244 and the hashes of all diagnostic artifacts;
everything matches. This live evidence does not prove the menu/nav or the final stop.

## Final real result of 244 (16:59 UTC)

**Verified:** session 60472 ended, game returncode 1. The previous stop
`0x53a244` was passed; the game opened both original archives
`packed/levels/front_end/level_cached.psarc` and `level_uncached.psarc`.
444 reads, 17,897,130 bytes; SPU 0x100 completed 878,746,602 instructions.
15,259 flips, 30,510 draws, 33 frames; host presentation 0.
`frame-000033.png` (the last PPM converted losslessly) visually still
shows the original loading planet. The menu and navigation remain unverified.

Real stop `pc=0x69f2b4`, `lr=0x69f060`, 308,205,078 PPU steps:
`FP division by zero is unsupported`. The original ELF word `ed8d3024`
divides f13 (`0x4063400000000000`, 154.0) / f6 (`0`, +0.0) into f12,
single precision; FPSCR `0x82004000`. The stop comes from
`anyps3/floating.py:153`, not from a missing static target.
Next step: verify the PPU results/flags/exception mode in the original
manuals, implement with native regressions, a new build and a real run.

The own `verification.json` and the independent `terminal-identity.json` verify
all six artifact hashes, three hashes of the diagnostic manifest,
the byte-identical base C++ against native-bundle-244 and all original PT_LOAD
bytes against EBOOT.analysis.elf. The controller stayed neutral; gamepad navigation
and a physical controller are not proven. At the time of this writing there is no live game handle.
