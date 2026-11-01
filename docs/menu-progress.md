# Progress — brief history

One line per milestone; the current state and the next step are in [knowledge/CURRENT.md](knowledge/CURRENT.md). The full
progress log up to 2026-11-01 (every build, stop and wave) is archived as
[archive/menu-progress-to-2026-11-01.md](archive/menu-progress-to-2026-11-01.md).

| date | builds | milestone (verified on the PC unless marked cloud) |
| --- | --- | --- |
| 2026-10-14 | ≤ 255 | All 34,512 OPD functions of the original EBOOT translated statically (2.75 M instructions). The game reaches the loading planet; one ~458 MB translation unit is the build brake. |
| 2026-10-15 | 256–284 | The translation is split into ~193 parallel units (~4 min builds). 138 SPU jobs are derived from the original data references; syscall/HLE and RSX method sweeps follow. |
| 2026-10-15/16 | 285–311 | First original front-end screens; Vulkan graphics on the RTX 4070 SUPER. **309: the original main menu**, virtual pad DOWN → LOAD (~0.7 FPS). |
| 2026-10-16/17 | 312–337 | Menu 0.7 → 15.7 FPS (322) → 38–40 (331: one host thread per SPU, lock-free guest memory, RSX worker) → **57.5–59.5 FPS (337)**, virtual-pad navigation 10/10. |
| 2026-10-17 | 338–345 | NEW GAME path to the system save dialog; first real save to the HDD (344); LOAD from the system list (345). Virtual and uinput pads work in the menu. |
| 2026-10-17/18 | 359–377 | **LOAD → `great_clock_a`, a playable level** with the virtual and uinput pads (363). Level 1.5 → 14.8 FPS (377). |
| 2026-10-19 | 380–460 | FAST wave: level 14.25 → 22.59 FPS (460). |
| 2026-10-20 | 472–660 | FAST2 waves: 46.68 FPS (472), 55.96 (660a). CLI game library, PySide6 GUI and desktop installer. |
| 2026-10-21 | 706 | **Native resolution holds the 60 Hz cap:** 59.71 / 59.69 FPS, ~99.8 % one-vblank, ~3–4 ms headroom (75 Hz emulated not held). DualSense motion, rumble and PS button; GUI audio toggle. |
| 2026-10-23 | 801–810 | Render scale with GUI selection: 1440p 14.22 FPS (801) → 45.52 standing (803, recorder on). FIX7: START crash fixed (810). |
| 2026-10-25 | 870–882 | 1440p walking: GPU depth copies, bounded caches, sampler shader reuse. 882 walks at 33.15 FPS (720p 44.68). The 1440p image was found to be 720p-derived (sample-grid bug, fixed in HIRES). |
| 2026-10-25/27 | 960–1035 | HIRES/HIRES2 waves: 1440p walking 37.3 / 35.9 (960) → **39.9 / 40.6 FPS (1031)**, GPU 19 → 15.4 ms. Blockers: RSX worker ~21 ms, main thread ~14.8 ms, GPU ~15.4 ms, stutter. Merged to main (PR #14). **Last game measurement.** |
| 2026-10-28 … 11-01 | — (cloud) | HIRES3–HIRES9 on branch `ccr-b1a3987e-aypb5o`, no game run: RSX worker per-draw cuts, GPU barrier cuts, device-memory pool, SPU job −50 % non-FP Ir, PPU page code −11..−18 % Ir. Unmeasured in the game. |
| 2026-11-01 | — | Documentation cleanup: superseded material moved to [archive/](archive/README.md). |

Not reached yet: stable 60 FPS at 1440p, and acceptance with a physical gamepad.
