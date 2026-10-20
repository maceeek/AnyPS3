# AnyPS3 project knowledge base

[Manager and Qt wrapper implementation](experiments/2026-10-20-manager-gui.md)
and [usage](../manager/README.md), isolated `fast2/manager-gui` worktree.
Synthetic wrapper verification does not change original-game acceptance.

This is the version-controlled entry point for the next model, a new run and a human. Read the goal state and runtime evidence in [CURRENT.md](CURRENT.md), the writing rules in [WORKFLOW.md](WORKFLOW.md), the handoff in [HANDOFF.md](HANDOFF.md). A record in the documentation does not, by itself, verify the emulator's function.

Latest state and handoff: [CURRENT.md](CURRENT.md) and [HANDOFF.md](HANDOFF.md)
(2026-10-19: build 380, original level `great_clock_a` at 15.02 FPS, movement with the virtual pad,
ended for handoff on user instruction; 60 FPS not reached, physical gamepad not verified).
[Results 379–380 and next steps](experiments/2026-10-19-performance-379.md),
[web research of real PS3 recomp projects](experiments/2026-10-19-web-ps3-recomp.md).
**2026-10-19 afternoon: FAST wave (target 60 FPS in the level, game mode 0 verified, default 14.25 FPS):**
[FAST record](experiments/2026-10-19-fast-wave.md).
**2026-10-20 FAST2 wave (best build 472 = main: 46.68 FPS in the level; ended on user instruction):** [FAST2 record](experiments/2026-10-20-fast2-wave.md).
**2026-10-19/20 cloud optimization round (PPU, SPU, RSX host cost; no game build and no FPS figure, best verified build
still 460 at 22.59 FPS):** [cloud optimization record](experiments/2026-10-19-cloud-optimization.md).

## Orientation

| Area | Overview | What to look for |
| --- | --- | --- |
| CELL and MFC | [CELL.md](CELL.md) | local vs main memory, DMA, synchronization, public manuals |
| SPU | [SPU.md](SPU.md) | ISA, channels, mailbox, tag completion, instruction reference |
| PPU | [PPU.md](PPU.md) | registers, ABI/OPD, PowerPC reference, HLE boundaries |
| RSX | [RSX.md](RSX.md) | FIFO, methods, draw, shaders, surface/flip, NV40/RSX differences |
| Source provenance | [SOURCES.md](SOURCES.md) | actual paths, revisions, manual manifest, limitations |

## Quick start for the next run

1. Read the root `AGENTS.md`, this page and CURRENT. Respect the user's latest instructions and the actual state of the working tree.
2. Open [../menu-progress.md](../menu-progress.md) for the current ACIT overview and the referenced logs. Verify aging information in the last real run; do not treat a run number from another model as automatically current.
3. Pick one specific open question. Before making a change, write down the hypothesis and the evidence that will confirm or refute it.
4. After verification, update CURRENT and the relevant topic page or a separate record. Write down the command, source state, profile and artifact paths.

## Searching

From the project root: `rg -n 'tag|mailbox|DMA' docs/knowledge local/reference/cell-docs/md`.
For an exact instruction use `rg -n 'Shuffle Bytes|Read Channel' local/reference/cell-docs/md`.
The manuals' MD has a `PDF page N` heading for each physical page; verify graphical tables and bit diagrams in the PDF.

PDFs and derived MD belong in the local library `local/reference/cell-docs/`; their existence and version are confirmed by the manifest and a check of the cover page. Local reference clones are not automatically part of the version-controlled knowledge base.
