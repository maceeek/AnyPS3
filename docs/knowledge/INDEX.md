# AnyPS3 project knowledge base

Entry point for the next model, a new run or a human. A record here does not, by itself, verify the emulator's function.

## Reading order

1. The root `AGENTS.md` (rules, acceptance, audio rule).
2. [CURRENT.md](CURRENT.md): the goal, the last PC measurement, the cloud changes since, the A/B switches, **the PC
   measurement plan**, refuted levers and standing decisions. It is the whole current state.
3. [HANDOFF.md](HANDOFF.md): branches, live processes, how a cloud round works and what the container cannot test.
4. A record below only when working on its area.

## Records (newest first; each describes its own snapshot)

- 2026-11-09 [HIRES9-PC](experiments/2026-11-09-hires9-pc.md): **the last game measurement on the PC** — first game run of the
  cloud rounds; the H5 geometry reuse thrashed (11 FPS) and is off by default; 1440p walk 43.1 / 43.2 FPS (1034: 39.8 / 40.2),
  720p 52.3 / 53.2; pool and test fixes; recorder teal check clean; disk-cleanup plan prepared.
- 2026-11-01 [cloud HIRES9](experiments/2026-11-01-cloud-hires9.md): PPU inline guest accesses through one pointer, MOVBE,
  budget-free block heads (memory kernels −14..−17 % Ir); SPU job remainder: shared chained-call scope, DMA copy, untracked DMA
  writes, SHUFB lanes (−9.8 % native); full suite.
- 2026-11-01 [cloud HIRES8](experiments/2026-11-01-cloud-hires8.md): generated SPU code without stack homes, self loops in host
  locals (job −39 % native); PPU page jump tables, host-call return prediction.
- 2026-11-01 [cloud HIRES7](experiments/2026-11-01-cloud-hires7.md): SPU job DMA path, FP underflow lanes, BRSL chaining;
  RSX worker work moved onto the draw recorder; the prepare-stage design; full suite.
- 2026-10-31 [cloud HIRES6](experiments/2026-10-31-cloud-hires6.md): worker draw unit −9.9 % (game-shaped bench mode), GPU
  barrier cuts 17 → 10, GPU mark attribution, visual 2× MSAA load path.
- 2026-10-31 [cloud HIRES5](experiments/2026-10-31-cloud-hires5.md): geometry reuse under CPU-dirty watches, state path
  (−24 % worker Ir per indexed draw).
- 2026-10-30 [cloud HIRES4](experiments/2026-10-30-cloud-hires4.md): FIFO decode, renderer-thread guest-memory admission
  (−37 % worker Ir per frame).
- 2026-10-28 [cloud HIRES3](experiments/2026-10-28-cloud-hires3.md): device-memory pool, DrawJob pointers, report floors,
  typed-RTT literals, the FIFO bench.
- 2026-10-25/27 [HIRES and HIRES2 waves](experiments/2026-10-25-hires-wave.md): the previous game measurement on the PC
  (1440p walking 1031: 39.91 / 40.58 FPS), GPU split, stutter classes, blockers.

## Topic pages (stable knowledge)

| Area | Page | What to look for |
| --- | --- | --- |
| CELL and MFC | [CELL.md](CELL.md) | local vs main memory, DMA, synchronization, public manuals |
| SPU | [SPU.md](SPU.md) | ISA, channels, mailbox, tag completion, instruction reference |
| PPU | [PPU.md](PPU.md) | registers, ABI/OPD, PowerPC reference, HLE boundaries |
| RSX | [RSX.md](RSX.md) | FIFO, methods, draw, shaders, surface/flip, NV40/RSX differences |
| Source provenance | [SOURCES.md](SOURCES.md) | actual paths, revisions, manual manifest, limitations |
| Writing rules | [WORKFLOW.md](WORKFLOW.md) | status labels, evidence, durable handoff |

Code-level overview: [../architecture.md](../architecture.md) (RSX/Vulkan structure in [RSX.md](RSX.md));
GUI/manager: [../manager/README.md](../manager/README.md), [../manager/DUALSENSE.md](../manager/DUALSENSE.md); brief history:
[../menu-progress.md](../menu-progress.md).

## Archive

[../archive/](../archive/README.md) holds everything superseded on 2026-11-01:
- the full progress log;
- the CURRENT/HANDOFF history;
- the records of 2026-10-14 … 2026-10-25 (bring-up, menu, level, FAST/FAST2 waves, 60 FPS at native resolution, resolution
  scaling, the 2026-10-25 walking work);
- the old milestone pages;
- the manager's verification evidence.

Read it only for provenance of a specific older fact.

## Searching

From the project root: `rg -n 'tag|mailbox|DMA' docs/knowledge local/reference/cell-docs/md` (the archive is outside
`docs/knowledge`, so it is not searched by default). For an exact instruction: `rg -n 'Shuffle Bytes|Read Channel'
local/reference/cell-docs/md`. The manuals' MD has a `PDF page N` heading for each physical page; verify graphical tables and
bit diagrams in the PDF. PDFs and derived MD live in the PC's local library `local/reference/cell-docs/` (manifest with
SHA-256); local reference clones are not part of the version-controlled knowledge base.
