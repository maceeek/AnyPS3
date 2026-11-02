# GUIFIX validation and original-game acceptance

Manager acceptance **passed**. Fixes and tests: commit `c4666ae` on `feat/gui-fix`.
The original-game intro rendered, Stop completed, and the temporary library was deleted.

## Environment and boundaries

[Environment](environment.json): Python 3.14.8, PySide6/Qt 6.12.0, Linux/CachyOS.
The isolated development environment is `/var/tmp/anyps3-guifix-venv`, installed
editable from this worktree, with optional Qt/cryptography dependencies. The
installed app and user's library were never used. All started processes force
`SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`; real game launches additionally
supply `audio.enabled=false`.

Tests/captures use user scopes with MemoryMax=8G, MemorySwapMax=0 and the offscreen
Qt platform. Manager translation/compile/game tools use their own required
systemd scopes and configured CPU/memory limits. Heavy batches wait through the
read-only main-checkout `quiet_wait.sh`. `.quiet` was absent before build launch.

## Commands and artifacts

Executed from `/home/martink/AnyPS3/local/ratchet/work/guifix/tree`:

```bash
export SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null
PY=/var/tmp/anyps3-guifix-venv/bin/python
LIB=/var/tmp/anyps3-e2e-lib
SRC=/home/martink/AnyPS3/local/ratchet/work/guifix/tree
bash /home/martink/AnyPS3/local/ratchet/tools/quiet_wait.sh
systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  "$PY" -m anyps3_manager --library "$LIB" --source "$SRC" \
  adopt-ratchet /home/martink/AnyPS3/local/ratchet --events
"$PY" -m anyps3_manager --library "$LIB" settings set --global audio.enabled false
systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  "$PY" -m anyps3_manager --library "$LIB" --source "$SRC" \
  build BCES00511 --reason 'GUIFIX original-game acceptance' --events
```

**Verified adoption:** succeeded, job `20261021T153528208030Z-f28f0ce4`, from
15:35:28 to 15:36:16 UTC (~49 seconds). It copied and reverified the original disc,
firmware, EBOOT and userdata and produced an imported title whose next step is
Build. [Events](e2e-adopt.log).

Build job: `20261021T153750336723Z-0b62916a`; artifact directory
`titles/BCES00511/builds/0001-20261021T153750378281Z-aa77a141` in the temporary
library. Runtime source and profile snapshots were taken at 15:37:50 UTC;
translation finished at 15:39:30 and compilation has 193 units. [Events](e2e-build.log).
Later lifecycle/GUI refinements do not change the runtime/profile snapshot.

The read-only [real GUI observer](real_gui.py) uses actual QProcess manager calls
and isolated QSettings. [Building screenshot](real-building.png) shows actual
compiler progress. Closing the observer leaves the detached build running.
[Observer log](real-gui-building.log).

[Source-input hashes](source-inputs.json) record the original HDD and original
ELF/disc/firmware manifest identities for the final unchanged-source check. They
contain hashes and names, no file contents or keys.

## Final validation

| Final validation | Result | Evidence |
| --- | --- | --- |
| Manager suite | 129 tests, 125 passed, 4 opt-in installer skips | [log](manager-suite.log) |
| Qt/protocol suite | 11 passed, including real QProcess/CLI synthetic recovery/build/run/saves/jobs walkthrough | [log](gui-integration-suite.log) |
| Total | **140 tests, 136 passed, 4 skips, no failures** | The two logs above are disjoint |
| Lifecycle subset | 9 passed, including scoped orphan recovery and pre-PID-publication child | [log](job-lifetime-tests.log) |
| Qt focused development suite | 39 passed, including delayed deleted receivers and shutdown | [log](gui-suite.log); these are included in the final suites |
| Real GUI observations | Building, built, running and stopped all passed | `real-gui-*.log`, corresponding PNGs |
| Captures | 24 before, 27 after, plus 5 original-library/game PNGs; personally inspected | [inventory](screenshots.md) |

**Verified build:** succeeded at 15:59:53 UTC, 1322.79 seconds (~22 min) including
translation. All 193 native units compiled. Original nonempty PT_LOAD segments
match exactly; native executable/ELF hashes and translator source hashes passed
verification. Source/profile snapshots match final runtime/profile files; manager
fixes do not alter those packages. Peak compilation memory was 22,329,544,704 bytes.
[Build record](e2e-build-record.json), [verification](e2e-build-verification.json),
[compiler report](e2e-build-compile.json), [snapshot check](e2e-snapshot-check.json).

The final native binary SHA-256 is recorded in verification; no binary/game data
is committed. The now-deleted artifact build ID was
`0001-20261021T153750378281Z-aa77a141`.

**Verified original-game run:** `20261021T160155069710Z-7b7cc3ef`, 16:01:55 UTC,
48.4645803 seconds. The live record showed **581 flips, 13 captures**, 16 FPS
samples and no STOP before Stop. The final record retained **897 flips, 15
captures**, 23 FPS samples, `verification_status=verified-boot`, and
`stopped_by_user=true`. Mean FPS was 18.6339 over this cold intro/boot window;
this is not a level-performance or headroom measurement.

The inspected [1280×720 original frame](original-game-frame.png) shows the Sony
Computer Entertainment intro and Ratchet & Clank copyright text. Its source was
`frame-000013.ppm`; [frame hashes/dimensions](e2e-frame.json) preserve provenance.
The live GUI [recognized the foreground game and enabled Stop](real-running.png).
[Live record](e2e-run-live.json), [final record](e2e-run-final.json),
[stdout](e2e-game-stdout.log), [stderr](e2e-game-stderr.log).

Actual launch/stop commands, under the same silent 8G outer scopes:

```bash
/var/tmp/anyps3-guifix-venv/bin/anyps3-manager --library /var/tmp/anyps3-e2e-lib \
  run BCES00511 --set audio.enabled=false --set diagnostics.capture_stride=120
/var/tmp/anyps3-guifix-venv/bin/anyps3-manager --library /var/tmp/anyps3-e2e-lib \
  stop BCES00511
```

**Verified Stop:** request CLI exited 0. Foreground launch CLI exited 5 with
`cancelled: Run stopped by the user.` The native program exited 1 and its final
STOP describes `SDL Vulkan presentation window closed; run terminated`, after
the requested shutdown. This expected native shutdown diagnostic is preserved;
no unexpected guest STOP was observed before the request. The GUI returned to
[Play enabled / Stop disabled](real-stopped.png).
[Stop request](e2e-stop.log), [launch result](e2e-run.log).

**Verified silence:** both global and explicit launch audio settings were false.
The native log explicitly reports null output and nonzero PCM discarded by the
null sink; no audible device was opened. See stderr lines containing
`diagnostic null sink` / `sink=null (DISCARDED)`.

**Verified cleanup/source boundary:** original HDD file hashes, original ELF,
disc manifest and firmware manifest all match the pre-run source record. All
recorded game processes/scopes exited and jobs were terminal before deletion.
`/var/tmp/anyps3-e2e-lib` was removed, including copied userdata, firmware, builds
and cache. [Checks](e2e-cleanup-check.json), [final title](e2e-title-final.json),
[final jobs](e2e-jobs-final.json). No user's installed app/library or original
source inputs were modified. The separate development venv remains for
reproducing the synthetic tests; no game/library data remains there.

Initial overlapping synthetic manager batches produced nonblocking global
compile-lock failures; the coordinator serialized validation afterward. The early
105-test batch had one sparse-fixture running-state issue; the focused regression
passed after fixing missing-identity reconciliation. Those attempts are not the
final acceptance result. Deliberately failing mutation/deleted-widget/shutdown
baseline logs demonstrate the corresponding regressions rather than final failures.

## Limits

Boot evidence from original-game flips/captures does not prove a navigable menu,
a playable level, physical input or the project's 60 FPS/headroom acceptance.
No audible-output acceptance is performed. Xvfb/xcb and compositor-specific stale
paint remain unverified. The four installer tests require explicit wheelhouse
fixtures; the user installation is outside this task's authorized scope.

The adoption/build supervisors were started before the later lifecycle refinements;
the final manager code was validated by the full synthetic CLI/Qt suites, scoped
worker-lifetime tests, final real launch/Stop, and real GUI observations. The
compiled runtime and profile are identical to final source.

On filesystems without hardlinks, save-export exclusive copying is visible until
complete, while failed copies are removed and existing destinations remain intact.
An unidentified surviving child keeps the job lock/history and yields an actionable
error; the manager does not claim that such a child was stopped.
