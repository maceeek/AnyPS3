# Archive (superseded on 2026-11-01)

Not current state. The live knowledge base is [../knowledge/INDEX.md](../knowledge/INDEX.md). Read a file here only for the
provenance of a specific older fact; its numbers and "next actions" describe its own date, and later work has replaced most
of them.

Moved here with `git mv` on 2026-11-01 (file history is kept; `git log --follow`; the progress log and the architecture page were replaced by new pages at their old paths, so their history stays under `docs/menu-progress.md` and `docs/architecture.md`). The tree before the cleanup is commit
`e896fed` of branch `ccr-b1a3987e-aypb5o`, for example `git show e896fed:docs/knowledge/CURRENT.md`. The layout mirrors `docs/`. Relative
links inside these files were rebased to the new locations. Anchors into CURRENT/HANDOFF refer to the pre-cleanup text, which
is kept in `knowledge/history/*-2026-10-25-to-11-01.md`.

| path | content |
| --- | --- |
| [menu-progress-to-2026-11-01.md](menu-progress-to-2026-11-01.md) | the full progress log (every build, stop and wave from the first boot to HIRES8) |
| [knowledge/history/](knowledge/history/) | CURRENT/HANDOFF texts: 2026-10-14 … 2026-10-25, and the full pre-cleanup [CURRENT](knowledge/history/CURRENT-2026-10-25-to-11-01.md) / [HANDOFF](knowledge/history/HANDOFF-2026-10-25-to-11-01.md) of 2026-10-25 … 11-01 (per-round sections HIRES2–HIRES8 with their own next-action lists); runtime handoffs 198–246 |
| [knowledge/experiments/](knowledge/experiments/) | records of 2026-10-14 … 2026-10-25: bring-up stops 238–250, split units 256, OOM/alias 311, menu FPS 332, in-game 338, FAST/FAST2 waves (incl. wave 6, build 706, 60 FPS at native resolution), sandbox analysis, manager/GUI/installer, render scale, FIX7, 1440p walking work of 2026-10-25 |
| [knowledge/evidence/](knowledge/evidence/) | render-scale evidence of 2026-10-23 |
| [knowledge/documentation-validation.md](knowledge/documentation-validation.md) | validation of the local manual library (2026-10-14) |
| [architecture-2026-10-14.md](architecture-2026-10-14.md), [vulkan-rendering-2026-10-14.md](vulkan-rendering-2026-10-14.md) | the first architecture page and the Vulkan clear milestone (builds 123–138); replaced by [../architecture.md](../architecture.md) and [../knowledge/RSX.md](../knowledge/RSX.md) |
| [ratchet-acit-startup.md](ratchet-acit-startup.md), `ratchet-acit-*milestone.md` | early start-up and loading milestones (2026-10-14) |
| [history/](history/) | the Czech README of 2026-10-14 |
| [manager/](manager/) | GUI/manager verification evidence (screenshots, logs, review) |

`profiles/ratchet-acit.json`'s `notes` (a 2026-10-14 text) still name `docs/ratchet-acit-loading-milestone.md` and
`docs/vulkan-rendering.md`, both now here. The profile is left unchanged on purpose: a notes-only edit would change the profile
snapshot of every later build.
