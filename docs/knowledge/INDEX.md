# AnyPS3 project knowledge base

This is the version-controlled entry point for the next model, a new run and a human. Read the goal state and runtime evidence in [CURRENT.md](CURRENT.md), the writing rules in [WORKFLOW.md](WORKFLOW.md), the handoff in [HANDOFF.md](HANDOFF.md). A record in the documentation does not, by itself, verify the emulator's function.

Latest state and handoff: the top sections of [CURRENT.md](CURRENT.md) and [HANDOFF.md](HANDOFF.md). Sections older than
2026-10-21 moved to [history/](history/) on 2026-10-25.

## Records, newest first

Each record describes its own snapshot, not the current runtime.

- 2026-10-28 [cloud round HIRES3](experiments/2026-10-28-cloud-hires3.md): device-memory pool (stutter), per-draw CPU cuts
  (DrawJob pointers, report floors, constant chunks), typed-RTT literals, FIFO decode bench; lavapipe tests only, no game run.
- 2026-10-25 [higher-resolution performance milestone](experiments/2026-10-25-scale2-walking.md): GPU depth copies,
  bounded history/source caches and sampler shader reuse; original 1440p walking evidence, partial-suite closeout and
  remaining 60-FPS/zero-stutter acceptance limits.
- 2026-10-23 [FIX7 user-reported problems](experiments/2026-10-23-fix7.md): START crash, GUI settings, skip intro,
  save-menu audio, two windows, stutter, RPCS3 patches (not adopted).
- 2026-10-23 [cloud follow-up](experiments/2026-10-23-cloud-render-scale.md): visual history through alias merge/rebind and
  typeless depth, counter fix; lavapipe unit tests only, no game run.
- 2026-10-23 [resolution scaling milestone](experiments/2026-10-23-render-scale.md): 1440p 45.52 FPS, GUI resolution
  selection, batched visual draws, matched native/scaled captures; quality-loss paths and stable 60 FPS open.
- 2026-10-21 wave 6: build 706 holds the 60 Hz cap at ~59.7 FPS with ~3–4 ms headroom
  ([FAST2 record, wave 6](experiments/2026-10-20-fast2-wave.md#wave-6-results-2026-10-21));
  [GUI-first quickstart and desktop installer](experiments/2026-10-21-gui-installer.md).
- 2026-10-20 [manager and Qt wrapper](experiments/2026-10-20-manager-gui.md) and [usage](../manager/README.md);
  [sandbox analysis and offline wait attribution](experiments/2026-10-20-sandbox-analysis.md) (603a 54.045 FPS);
  [FAST2 wave](experiments/2026-10-20-fast2-wave.md) (472: 46.68 FPS).
- 2026-10-19 [FAST wave](experiments/2026-10-19-fast-wave.md) (default 14.25 FPS),
  [cloud optimization round](experiments/2026-10-19-cloud-optimization.md) (460: 22.59 FPS),
  [results 379–380](experiments/2026-10-19-performance-379.md),
  [web research of real PS3 recomp projects](experiments/2026-10-19-web-ps3-recomp.md).
- Older: [../menu-progress.md](../menu-progress.md) and the `experiments/` directory.

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
