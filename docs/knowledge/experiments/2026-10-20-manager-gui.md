# Manager and Qt wrapper — 2026-10-20

**Scope:** SPEC work packages 1–3 plus manager-side controller diagnostics.
Isolated worktree `local/ratchet/work/manager-gui/tree`, branch
`fast2/manager-gui`, base `abf245064fd8c31990ca3fa5f96f53820feff179`.
The primary checkout's existing documentation changes were left untouched.

**Verified:** new `anyps3_manager/` and `anyps3_gui/` packages; console entry points
`anyps3-manager` and `anyps3-gui`; optional PySide6 dependency. The only runtime-tree
edit is `anyps3/native_build.py --extra-flags`, passed as an argument array and
recorded by the existing compiler report.

The manager owns the disk library, metadata/run/compile locks, detached workers
and replayable JSON Lines, staged ISO imports, copy-only adoption, source-snapshot
builds, activation/pinning/pruning, layered settings, save backups and run evidence.
Successful builds check original PT_LOAD bytes and source hashes. Userdata has a
separate lifetime from disc/build outputs. Qt uses asynchronous CLI calls only;
settings are schema-rendered and errors preserve their code and detail.

**Verified tests:** scoped manager/GUI suite, 30 tests passed, no skips, with
`MemoryMax=4G`, `MemorySwapMax=0`, `AllowedCPUs=6-11,18-23`, dummy/null audio.
The scoped integration case launched synthetic tools through actual systemd
scopes and surfaced the synthetic guest STOP with exit category 3.
A tiny C++ fixture proved `--extra-flags` is applied and recorded.
Tests hash userdata through build, activation, pruning and replacement-disc
import; cover descendant cancellation, stale PIDs, import retry, restore locks,
archive traversal, detached-job replay and Qt offscreen/protocol behavior.

**Review fixes:** GPT-6-Astra identified five issues in the first candidate:
cleanup and lock lifetime after launch/supervisor failure; missed RSX worker
capability through a runtime helper; detached jobs losing `--source`; a firmware
completion callback looping; and non-recoverable adoption publication. The
corrected candidate adds regressions for each, including an actual killed
synthetic supervisor, surviving child exclusion, and interrupted directory
publication. The run-launch tests verify that cleanup completes before locks
become available. See [Astra report](../../manager/ASTRA-REVIEW.md).
**Verified recheck:** Astra resolved R1–R5 for the demonstrated cases and found no
new confirmed issue. The reviewed source hashes still match the final candidate.

**Verified GUI integration:** the real CLI populated a synthetic title, build
history and generated settings in an offscreen Qt window. This caught and fixed
unnecessary metadata write locking in read commands and Qt `QVariantMap`
overflow of the schema's uint64 maximum. Object signals retain JSON integers.
Earlier failures exposed staged-rename permissions, a signal shadowing
`QObject.event`, a self-retaining Qt cleanup callback, and test-harness deferred
destruction. The successful suite and preview exited normally.

Commands and usage: [manager README](../../manager/README.md).
Evidence: [suite after review](../../manager/evidence/suite-after-review.log),
[GUI integration](../../manager/evidence/preview-after-review.log),
[synthetic title screenshot](../../manager/evidence/preview-after-review.png),
[final source/evidence hashes](../../manager/evidence/verified-candidate.json).
The earlier review-candidate manifest and its 22-test logs are retained separately.

**Limits:** no original-game build/run or FPS claim from this work. The stored
603a verifier was inspected for context; it is earlier runtime evidence, not
manager acceptance. Physical Xbox/DualSense navigation remains unverified.
WP4 player mode and pad-runtime P2–P7 remain separate work packages under the
specification's path ownership. Their settings are visibly unavailable against
this runtime. Controller diagnostics do not prove in-game input.

**Next acceptance:** with permission to occupy the PC, copy-adopt into an external
library, build/play silently, rebuild, reload the same save and compare userdata
hashes. Then verify physical-controller navigation through the original game's
input path. A synthetic fixture or first flip does not prove menu/play success.
