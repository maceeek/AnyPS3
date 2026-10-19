# Public PS3 → PC recompilations and their relevance for AnyPS3

Research date: **2026-10-19**. A documentation task; no foreign port was built
or run and no change to the AnyPS3 runtime/profile was made.

**Result: yes, public static PS3 recompilations exist with released packages
and documented author reports about a real game. The strongest finding is
Drakengard 3 Recompiled; another is Soulcalibur II HD Online.** It is incorrect
to confuse them with Xbox 360 ports. A playable part and a released alpha are, however, not
evidence of a flawless run through the entire game.

## How to read the evidence

- **Verified here:** an existing public repository, a specific source file,
  the license, a fixed Git SHA and release metadata from the GitHub API.
- **Author's claim:** the reached game scene, FPS and scope of tests published
  in the primary README, technical record or release notes. This survey did not
  independently reproduce their run.
- **Derived / hypothesis:** possible use in AnyPS3. None of the optimizations
  listed below is evidence of a Ratchet speedup.

The search covered `PS3Recomp`, `CellRecomp`, PlayStation 3 static recompilation,
specific games and follow-up repositories. Catalogs and social results served
only to locate primary sources; the conclusions below rest on the repositories and their
releases. A separate verifiable project `CellRecomp` with a finished PS3 port
was not found in this scope. That is not evidence that no other project exists.

## Specific ports

All rows in this table are based on the **PS3 version**, not the Xbox version.
PPU = PowerPC CPU, SPU = Cell local programs, RSX = original graphics path.
The game results listed are **author's claims**; the public availability of the code and the revisions were verified.

| Project and original game | Actually described state and limitations | Architecture / host | Public code and license |
| --- | --- | --- | --- |
| [Drakengard 3 Recompiled](https://github.com/soyjxck/dod3-recomp), **BLUS31197 + update 1.01** | Released **v0.2.5-alpha** for Windows, v0.2.4-alpha for Mac. The author reports the first chapter, combat and hub, 60 FPS at 4K on RTX 4090/i9-13900K and 60 FPS/1080p on M1 Pro. Later chapters of the new generation of the translation have not been worked through. The Windows 10 fix was verified by simulating stricter copy rules on Windows 11, not directly on Windows 10. | PPU+SPU → C++; HLE libraries; RSX → D3D12 / Metal. Vulkan experimental and slower. Windows x64/AVX2, macOS arm64. | **MIT**, generator/runtime and native parts are public, your own copy of the game is required. [README at a fixed SHA](https://github.com/soyjxck/dod3-recomp/blob/96df4c364a20c306724a48886a43f8f14ce5bedd/README.md), [release](https://github.com/soyjxck/dod3-recomp/releases/tag/v0.2.5-alpha). |
| [Soulcalibur II HD Online](https://github.com/n0madic/Soulcalibur2HD-recomp), **NPUB31366**, 01.00/01.01 | Released launcher **v0.1.0**; the author describes regular combat on Mac and, on Windows/Arc140V after building the shaders, 60 FPS. Linux only software Vulkan (~10 FPS), a real Linux GPU is not verified. Online does not work; an occasional faulty transition frame, A/V and decoding limitations. | PPU+5 SPU programs → C/C++ only on the user's PC; ps3recomp; Metal / Vulkan, SDL. Windows, macOS arm64, limited Linux. | Source **GPL-2.0-or-later**, the binary as a whole **GPL-3.0-or-later** because of the combination with Apache 2 parts; distinguished exactly in the README. Code/patches/launcher public, not the generated game. [Fixed README revision](https://github.com/n0madic/Soulcalibur2HD-recomp/blob/be2dd0640bc0e7486c4a3a47d027bd643fd0cd90/README.md), [release](https://github.com/n0madic/Soulcalibur2HD-recomp/releases/tag/v0.1.0). |
| [The Simpsons Arcade Game PS3](https://github.com/sp00nznet/simpsonsarcade-ps3), **NPUB30563** | The author reports intro, menu, saving, Stage 1 onward, pad and online co-op; 35–57 FPS in game, 60 in the menu. Source build, a run through the entire game is not verified; when FPS drops, gaps in the audio. | Statically translated PS3 PPU/SPU wrapper; RSX → D3D12, Windows/clang-cl. **Inside it is a recompiled PS3 arcade emulator:** the original 6809 arcade code continues to be interpreted from ROM. | **MIT** verified in LICENSE. Configuration, runner and relift tooling public; it needs the user's own PSN data and the capture of dynamically assembled SPU jobs. [Fixed README](https://github.com/sp00nznet/simpsonsarcade-ps3/blob/5371f2575b9dc9d2a7d2c3c615e4072bb85a9800/README.md). |
| [Yakuza: Dead Souls Recompiled](https://github.com/canersaka/Yakuza-Dead-Souls-Recompiled), **BLUS30826 v1.00** | Explicitly WIP. The author reports intro → New Game → gameplay, office tutorial 30 FPS, a crowded outdoor start ~20 FPS. This evidences neither a finished flawless port nor a binary release. | PPU, shader PRX and SPU → C++; own HLE/SPURS; RSX VP/FP → HLSL/D3D12, Windows x64. | **MIT**, public fork of the runtime, lifters and the game runner. The game C++ is generated from the user's own copy. [Fixed README](https://github.com/canersaka/Yakuza-Dead-Souls-Recompiled/blob/95135736b2e20ecf19e67457bbabc2926c3b355f/README.md). |
| [God of War II HD](https://github.com/andrebrumdev/gow2-recomp), **NPUA80491** | The author reports Rhodes 45–60 FPS, the fight with the Colossus playable with a bone-matrix error; audio has timing limitations. The other mobile platforms listed are an experiment. | PPU+7 SPU programs → C/C++, ps3recomp fork; Metal/MSL, VideoToolbox, CoreAudio; Apple Silicon. | **MIT**, public setup kit, function boundaries, patches, test scripts; no game bytes. [Fixed README](https://github.com/andrebrumdev/gow2-recomp/blob/a787912c7b2e2f61bb387c9c22722a93e6c5ee3c/README.md). |
| [Tornado Outbreak](https://github.com/sp00nznet/tornado), **BLUS30371** | Intro → New Game/save → first level `jb_intro`, tornado movement, 10–16 FPS. **After a few seconds the white transition does not disappear.** A documented game milestone, not a recommendation of a finished port. | PPU + a small raw-SPU memcpy program and Wwise SPURS jobs; ps3recomp, D3D12/Windows. | **MIT**, runner and build/relift tools public. [Fixed README](https://github.com/sp00nznet/tornado/blob/2cc6c0fb660b62aab235e06ac8e0fa64106ec265/README.md). |

**Verified:** the GitHub API really returns the assets of Drakengard3 and Soulcalibur2HD;
it is not just a link to a future release. Drakengard3 v0.2.5-alpha published
2026-10-16, SHA `96df4c364a20c306724a48886a43f8f14ce5bedd`, asset
`dod3-recomp-v0.2.5-alpha-win64.zip`. SHA256 of the zip stated by the author:
`62fad10015b95a482b5f720263cb67cef4066439ae1432a91485a2b53f55f535`.
The binary was neither downloaded nor verified. Soulcalibur v0.1.0 published
2026-10-16: Windows x86_64, Linux x86_64 and macOS arm64 packages.

Further relevant but weaker evidence: [inFamous author's PR287](https://github.com/sp00nznet/ps3recomp/pull/287)
describes gameplay on a full LLE branch; [Killzone3 author's technical issue214](https://github.com/sp00nznet/ps3recomp/issues/214)
describes the first level and dozens of general fixes (BCUS98234, PPU+SPU/Havok,
RSX deferred, Windows build under Proton). It is not verified availability of finished
ports. Conversely, [juans-collab/infamous-2-recomp](https://github.com/juans-collab/infamous-2-recomp)
itself explicitly says **Not playable**; a port's name alone is not evidence.

## Common foundation and fixed revisions

**Verified:** [sp00nznet/ps3recomp](https://github.com/sp00nznet/ps3recomp)
is a public **MIT** toolkit with PPU/SPU lifters, a memory/LV2/HLE runtime and its own
RSX implementation. **v0.15.0** was released 2026-10-16; the upstream tree read:
`39a36296511c445707adabcd312f0b740cfbd694`. [Release notes](https://github.com/sp00nznet/ps3recomp/releases/tag/v0.15.0)
separate the consolidated fixes from the deferred memory domains change #296, which
broke the VF5 boot. A release SDK is not a release of every game.

Drakengard3 uses a different [soyjxck/ps3recomp fork](https://github.com/soyjxck/ps3recomp),
gitlink exactly **`bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb`**. Do not confuse its
`ppu_vm_fast.h` or `rsx_vulkan_engine.c` with today's upstream, which has
`rsx_vulkan_backend.c` and a different structure. The data in various documents may
come from different stages: for example, the older parts of `PPU_RECOMP.md` still
describe the initial Uncharted boot, while the section about local registers
already measures Drakengard3. The decisive thing is the specific implementation and revision.

## Five areas for follow-up work in AnyPS3

The ordering is based on the local [measurement 379+](2026-10-19-performance-379.md):
PPU memory ~35% CPU, group4 single FP ~28%, CPU part of RSX ~40 ms.
PIPE2 showed the producer of the engine counter through SPU PUTLLUC; mere similarity
to a foreign wait for an RSX fence does not make it an RSX problem.

### 1. PPU page fast path and merging of memory checks

**Verified code:** [fork/runtime/ppu/ppu_vm_fast.h](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/runtime/ppu/ppu_vm_fast.h)
lines 1–48, 83–151 and 167–248; [ppu_vm_page.h](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/runtime/ppu/ppu_vm_page.h).
64 KiB page flags decide on the short path; reservations, MMIO, watching
and diagnostics have a slow path. Stack/prolog and a contiguous run of accesses share a check.
**Author's claim in a comment:** a variant with several branches was 9–11% CPU
worse than a helper call, even though it had fewer instructions.
**Hypothesis:** very relevant for PPUMEM2, first compare its epochs with this
page representation. **Do not adopt directly:** flat mapping, volatile accesses
and the host race/lifetime model are not evidence of safety for AnyPS3. Unmap,
dirty tracking and PPU/SPU reservations and their concurrency must be preserved.

### 2. Registers in locals and precise dirty/liveness analyses of the translator

**Verified code:** [fork/tools/ppu_nv_dirty.py](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/tools/ppu_nv_dirty.py)
lines 1–89, [PPU_RECOMP.md section 91–183](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/docs/PPU_RECOMP.md#registers-in-locals---nonvolatile-locals).
PPU GPR/FPR/VMX and separate CR/XER locals are saved on a call only if
they are changed and needed; the call graph has explicit ABI boundaries and a verification mode.
**Author's claim:** release 0.2.4 reduced the CPU of the two main threads on Windows
by about a third; it is not an isolated result of a single transformation.
**Hypothesis:** adapt into the `anyps3` generator only after checking its current
blocks. Beware scheduler preemption, HLE callbacks, mid-function entries and
state saving before a stop; AnyPS3 rules may require more writebacks.

### 3. Exact SPU FP fast paths and an independent oracle

**Verified code:** [upstream/runtime/spu/spu_helpers.h](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/runtime/spu/spu_helpers.h)
lines 570–624 and the following NEON part; [spu_float_referee.py](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/runtime/spu/tests/spu_float_referee.py).
Normal operands use a double intermediate result and a controlled truncation,
unfavorable lanes an exact fallback. [PR287](https://github.com/sp00nznet/ps3recomp/pull/287)
was merged 2026-10-16, merge `41c19c8`; the interpreter/lifter tests compare
against RPCS3 and any float differences are adjudicated by exact arithmetic.
**Hypothesis:** the oracle and edge cases will help SPUFP2. The reported 1.6–4× applies
to **ARM NEON**, it cannot be promised on our x86. MXCSR/extended-range/zero
semantics still need to be proven against the manual and our tests.

### 4. Write-watch cache and batching of RSX work

**Verified code:** [fork/runtime/platform/vm_watch.c](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/runtime/platform/vm_watch.c)
and [fork/libs/video/rsx_vulkan_engine.c](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/libs/video/rsx_vulkan_engine.c)
lines 1–33: collection of draw records, staging chunks, a common rendering pass, timeline
semaphore. [Drakengard3 technical record](https://github.com/soyjxck/dod3-recomp/blob/96df4c364a20c306724a48886a43f8f14ce5bedd/docs/TECHNICAL.md#rendering)
describes validation of the texture/vertex cache via writes and suballocation.
**Hypothesis:** reduce the work on the CPU RSX thread, do not confuse it with GPU time.
The fork's Vulkan is here **Windows-only**; upstream Linux
[rsx_vulkan_backend.c](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/libs/video/rsx_vulkan_backend.c)
is a more direct POSIX reference. First audit the existing AnyPS3 cache, guest DMA,
readback and fence ordering; transferring the whole backend makes no sense.

### 5. SPURS admission, persistent workers and wake-up invariants

**Verified code:** [upstream/libs/spurs/cellSpurs.c](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/libs/spurs/cellSpurs.c)
lines 1660–1767; [PR291](https://github.com/sp00nznet/ps3recomp/pull/291).
The coordinator assigns work to persistent workers with a limit on the number of SPUs and
workload contention, separates DMA contexts and reservations.
**Hypothesis:** usable as a check for producer/consumer starvation and
lost wakeup. For Ratchet, first determine the guest job and the critical path
of the PUTLLUC counter. Our real LLE SPU job manager is not their HLE scheduler;
replacing it is not a general optimization. Increasing the number of workers without measurement
or removing a polling loop is not a well-founded fix.

## What not to adopt and what was not a PS3 result

**Verified:** Drakengard3 contains, besides the general runtime, game-specific hooks,
a modified limiter, replacements of selected SPU tasks, rewritten menu/Unreal bytecode
and an asset overlay ([TECHNICAL.md](https://github.com/soyjxck/dod3-recomp/blob/96df4c364a20c306724a48886a43f8f14ce5bedd/docs/TECHNICAL.md#the-settings-menu)).
Its overall FPS cannot be attributed solely to static translation, and these replacements
are not a suitable way to meet the AnyPS3 conditions. The general
algorithms and the test methodology above are relevant.

[UnleashedRecomp](https://github.com/hedge-dev/UnleashedRecomp) really ports
**Xbox 360** Sonic Unleashed: Xenon PowerPC via
[XenonRecomp](https://github.com/hedge-dev/XenonRecomp), Xenos shaders via
XenosRecomp; GPL3. It does not address Cell SPU or RSX. N64Recomp in turn addresses MIPS N64.
These successes are not evidence of a successful PS3 recompilation. [RPCS3](https://github.com/RPCS3/rpcs3)
is a PS3 emulator with JIT/AOT and the most mature behavioral reference; it is not
a standalone static game port. The RPCS3 license is a combination of GPL2-only and other
licenses by file, not MIT. The upstream's claim of clean-room origin
does not remove the need to verify the license of the specific adopted revision; PR287 itself
mentions the removal of older GPL-derived float code.

## Artifacts and the next first action

The public files read and the GitHub API responses are in
`local/ratchet/codex/web-ps3-recomp/`, the inventory `manifest.json` includes SHA256,
URLs and revisions; `validation.json` checks the inventory and the links of the local artifacts.
No game files, release binaries or firmware were downloaded. No
tests, installations, builds or game runs were necessary for this research.

**First next action within this research:** compare the separate PPUMEM2
candidate with `ppu_vm_fast.h`/`ppu_vm_page.h` and propose a single isolated
prototype of minimizing per-access checks while preserving the current concurrency
invariants. Measure the same `great_clock_a` scene; a synthetic ns/access figure
alone is not enough. The SPUFP2 oracle is another direct path. **The order of the actual
implementation is determined by the main [HANDOFF](../HANDOFF.md): first a small SPUFP2
candidate; PPUMEM2 separately because of its hot-tracked regression.** The research alone
did not change the actual state of the 60 FPS goal nor did it decide on the integration of the candidates.
