# Rules for working with knowledge

## How a verified record is created

State every technical conclusion with the status **fact (verified)**, **derived**, **hypothesis**, **decision** or **open question**, in line with the root `AGENTS.md`. Attach the scope: public CELL architecture, a specific reference implementation, AnyPS3 statically, or a specific runtime run. Agreement between two emulators is not, by itself, evidence of real hardware.

A fact from a manual must have the title, version, physical PDF page, optionally the printed page/section, the path to the original PDF and the SHA256 from the manifest. A source-code fact has `repo-relative-path:line`, the revision of the reference clone, and an explanation of what exactly the given line demonstrates. When the revision changes, look the symbol up again; do not keep old lines around as presumed valid.

A runtime fact has a reproducible command, date, revision or hash of the changed files, the profile/game without copying protected data, the log, exit status/timeout and relevant counters/artifacts. State whether an actual guest draw, host presentation, clear or flip occurred, or whether only an image file was produced. A black frame is not evidence of a rendered menu.

A hypothesis must have a reason, an alternative and a further distinguishing test. Promote it to a fact only after obtaining evidence. Mark a refuted hypothesis and keep its brief reason so that the next run does not repeat it. An error of a public mirror belongs in the source manifest, not in the list of causes of the game's failure.

## Working loop

1. Read the current state and the latest evidence, determine the nearest blocking guest step.
2. Find the specific instruction, HLE function, address, FIFO method or missing state. Verify against the manual, then against the relevant reference implementation.
3. Write down the hypothesis and the expected change in the observation. Choose a small change with clear verification; do not bypass the real guest branch by claiming that the menu works.
4. Run appropriate tests and the real profile/run, compare the same counters and output.
5. Record the result, the origin of the evidence, limitations and the next step. Link the new record from CURRENT.

## Durable handoff

CURRENT contains only the current goal, the last valid evidence, active questions and the next step; long logs belong in artifacts. The CELL/SPU/PPU/RSX topics contain more stable conclusions and a map of sources. Records of individual experiments can be stored as `docs/knowledge/experiments/YYYY-MM-DD-short-name.md` and linked from CURRENT.

Record a change of the deciding blocker as you go; do not wait until the model ends. Before a handoff, distinguish the changes made, the verification actually run, and the plan. Do not report a test as successful merely because it was prepared. The end of the goal is counted as the original ACIT menu and verified gamepad interaction, not merely reaching boot, a callback or a flip.

Do not write project knowledge into the user's system memories. Do not include secrets, keys, game content, firmware or a proprietary SDK. Publicly available manuals in the local library retain their original copyright; assess their own terms before public redistribution.
