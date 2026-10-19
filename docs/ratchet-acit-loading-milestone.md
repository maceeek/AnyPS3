# Milestone: native loading and SPU decompression

Verified 2026-10-14, Ratchet & Clank: A Crack in Time, BCES00511 01.00,
build `native-bundle-190`. **Neither the menu nor game geometry is
rendered yet.** The completed whole is the path from the actual reading of the archive through the
original SPU decompression to the DMA of the result and the notification of the main processor.

## Verified result

The original FIOS opens `packed/game/game.psarc`, loads the index and data and creates
actual system cache files. The statically translated original SPU program
decompresses all three blocks of `game.toc` and hands them over via DMA to main memory.
Mailbox events on port 17 let the PPU take over the results. The game then
also opens `global_cached.psarc` and `global_uncached.psarc`.

After the third completed SPU event, a diagnostic observer copied
136,620 bytes from guest address `0x409acb40`. It did not change game memory or code.
The output **matches byte for byte** an independent PSARC decompression via zlib:

```text
SHA-256 game.toc:
b540c6930178ed93d1850c7210069dd612df73a17a5cde49f42ad9693630c08d
```

Build 190 also got past the earlier DMA error on the range
`0x409fee80 + 0x4000`, which crosses the boundary of adjacent mappings.
It reached 110,610,176 PPU steps and ended at the diagnostic limit of
10 million instructions of the first SPU. For now the limit protects the experimental run;
for further loading it will be necessary to adjust it and continue the diagnostics.

Since the previous commit, the following were also added: completion of the game data initialization,
termination and join of a PPU thread, lightweight condition variables, a custom Vulkan
clear of tiled ARGB8/Z24S8 surfaces and headless buffer swaps. The four
captured frames of 1280 × 720 are black; the counts of clears and flips are not evidence of the
menu. The static translation supplements verified original callbacks and SPU overlays,
not patches of the game code or an RPCS3 emulation backend.

## Safeguards and scope

- DMA verifies the entire range, the permissions and the prohibition of writing into code in advance.
  A hole or a disallowed neighboring block does not cause a partial write. Successful writes
  update reservations also across memory aliases.
- A full event queue pauses the SPU and retains the message for retry.
  A send with a reply returns the actual state of the port/queue; events are not invented.
- The filesystem reads and writes real files. Symlinks and `..` are rejected.
  `ANYPS3_DISC_CASEFOLD=1` is an explicit adaptation of the uppercase ISO9660 names
  of our extract; it does not apply to HDD/cache and rejects ambiguous names.
- PPU/SPU execute pre-translated code. The SPU verifies the exact instruction
  word of a known image/overlay. Unknown instructions and services still fail.
- SPU interrupts are still disabled; enabling them and isolated groups
  are not implemented. Vulkan does not yet draw game shaders or geometry.
  SDL2 input is tested separately; navigation of the real menu is not verified.

## Reproduction

Complete tests without game data:

```bash
ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q
```

The game run requires the original local inputs: the analysis EBOOT,
verified firmware 3.01, a local key file and a complete disc extract.
These files are not part of Git. The output directory must be new:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json \
  --profile profiles/ratchet-acit.json --max-steps 200000000 \
  -o local/ratchet/loading-milestone
c++ -std=c++17 -Og -Wall -Wextra -Werror -DANYPS3_VULKAN \
  local/ratchet/loading-milestone/program.cpp -lvulkan \
  -o local/ratchet/loading-milestone/program
mkdir -p local/ratchet/loading-milestone/frames local/ratchet/cache local/ratchet/hdd0
ANYPS3_DISC_ROOT=local/ratchet/game-complete \
ANYPS3_DISC_CASEFOLD=1 ANYPS3_CACHE_ROOT=local/ratchet/cache \
ANYPS3_HDD_ROOT=local/ratchet/hdd0 \
ANYPS3_FRAME_DIR=local/ratchet/loading-milestone/frames \
  local/ratchet/loading-milestone/program \
  local/ratchet/loading-milestone/program.elf
```

The verification run used Lavapipe and Khronos validation including synchronization.
A detailed SPU trace can be enabled with `ANYPS3_SPU_TRACE=1`; the default run logs
DMA, events and the stop without every single instruction.

## Local evidence

- `local/ratchet/native-bundle-190/`: the build, the profile snapshot, logs,
  four captured frames, `verification.json` and the source hashes at build time.
- `decompression-verification.json` in the same directory: the match of the entire
  `game.toc`, the hash and the identity of the diagnostic program.
- `diagnostic.cpp` in the same directory: a reading observer added only
  for data comparison. The regular runtime receives no pre-decompressed output.
- `local/ratchet/tests-native-190.log`: the complete regression suite.
- [Detailed procedure](menu-progress.md) and [Vulkan verification](vulkan-rendering.md).

Further work begins with the SPU run budget when reading the global archives.
After that it is necessary to track the game's actual requests for further services and RSX
commands, add drawing, and only then verify the menu and the gamepad.
