# Independent Astra review — 2026-10-20

Reviewed the working candidate on `fast2/manager-gui`, base
`abf245064fd8c31990ca3fa5f96f53820feff179`, including the untracked
`anyps3_manager/`, `anyps3_gui/`, manager/GUI tests and documentation.
Scope: SPEC WP1–3 plus manager controller diagnostics. WP4 and runtime pad
P2–P7, original-game acceptance and physical-controller acceptance are outside
this review. Source was not edited.

## Findings

- **R1 · P1 · verified — `anyps3_manager/runs.py:89–94`, `anyps3_manager/storage.py:87–95`:** Cancellation after `Popen` but before the protected loop leaves the program alive while both title locks are released; supervisor death also releases these manager-only locks while its child continues. A subsequent restore, prune or second run can proceed against a live game. Protect the entire child lifetime immediately on launch, terminate and reap on every managed failure, and retain lock exclusion through child lifetime or reconcile live recorded identities/scopes before permitting another operation.
- **R2 · P1 · verified — `anyps3_manager/pipeline.py:416–417`, `anyps3_manager/cli.py:154–155`:** The capability scan misses `ANYPS3_RSX_WORKER`, which the current runtime reads through `anyps3_trace_enabled`, so the default `performance.rsx_worker=true` becomes unavailable and is omitted from every manager launch. Recognize the runtime's environment-reader helpers in a shared capability scanner and test the emitted launch environment against the actual source.
- **R3 · P1 · verified — `anyps3_manager/jobs.py:91–93`, `116`:** `--source` is recorded but not passed to the detached worker or restored there; build/import jobs use the installed manager's checkout instead of the requested runtime/profile source. Propagate the resolved source to the worker and verify its snapshot/profile selection using two distinguishable source fixtures.
- **R4 · P2 · verified — `anyps3_gui/import_wizard.py:137–143`:** Adding a firmware PUP never reaches disc import: the final `Job.emit("result")` still contains `job`, so `saved()` repeatedly runs `jobs follow` for the already completed job. Distinguish submission acknowledgements from terminal result events, consume the returned firmware ID, and advance exactly once.
- **R5 · P2 · verified — `anyps3_manager/pipeline.py:511–518`:** Adoption publishes `disc`, `eboot` and `userdata` before validating key references and registering the title. A missing key leaves an unindexed title directory; after fixing the key, retry fails with `ENOTEMPTY`, and `titles remove` cannot remove it because `title.json` is absent. Validate prerequisites before publication and make the publication/register sequence recoverable, including interruption between directory renames.

## Reproduction evidence

All probes used temporary synthetic files and silent environments
(`SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`), with bytecode writes disabled.
No game, compiler, GPU workload or test suite was started. Temporary processes
were explicitly terminated/reaped where owned, and temporary fixtures removed.

1. **R1, managed cancellation:** constructed a tiny registered title with a
   verified executable containing only a Python sleep, disabled automatic backup,
   and called the actual `runs.run(..., unscoped=True)`. Injected
   `ManagerError("cancelled", ..., 5)` from the first child-identity lookup,
   reproducing the installed SIGTERM handler's exception immediately after
   `Popen`. After unwinding, the child was alive and the run lock was acquirable:
   `{"error":"cancelled","child_alive_after_run_unwound":true,"run_lock_reacquired":true}`.
   This verifies a cleanup gap without relying on systemd signal forwarding.
2. **R1, supervisor death:** a forked fixture held the actual `storage.lock`
   and launched a sleeping subprocess with the same default `Popen` descriptor
   behavior as `runs.run`. After SIGKILL of that fixture's known PID, the child
   remained alive and another holder acquired the lock:
   `{"worker_killed":true,"run_lock_reacquired":true,"game_fixture_still_alive":true}`.
   The same descriptor-lifetime issue applies to the compile lock. A real
   systemd/game crash was not induced.
3. **R2:** called the actual `cli.available`, `settings.schema` and
   `settings.run_environment` against this checkout:
   `{"setting":true,"available":false,"run_env":null}` for the RSX worker.
   `anyps3/rsx_game_worker.py:75` returns without starting the worker unless
   `anyps3_trace_enabled("ANYPS3_RSX_WORKER")` is true; the helper calls
   `getenv(name)` in `anyps3/runtime.py:105`. No FPS magnitude is inferred.
4. **R3:** supplied a distinct `lib.source`, invoked the actual `jobs.start`
   with `Popen` intercepted, and inspected the persisted record and argv.
   The record contained the distinct source; argv was
   `python -P -m anyps3_manager --library LIB _worker JOB`, without `--source`.
   `work()` never uses the saved `source` field. No real build was needed.
5. **R4:** executed the actual parsed `ImportWizard.accept` method with a tiny
   asynchronous CLI fake, then delivered a queued acknowledgement and genuine
   terminal-result-shaped messages containing both `job` and `firmware`.
   Observed `firmware add`, followed by four successive `jobs follow` requests
   for the same ID and no `import`. This isolates the callback logic; it was not
   a hardware/firmware extraction test.
6. **R5:** used the actual adoption method with tiny copy-only fixtures and
   mocked identity/profile parsing. First attempt without `app-key.json` failed
   with `Key file not found` after publishing directories. Added both key files
   and retried with a fresh job ID: `OSError: [Errno 39] Directory not empty`
   while renaming staged `disc` to the existing destination.
   `title_registered=false`, `userdata_present=true`. The original source
   fixture remained intact.

## Additional review limits

- Read the supplied successful 22-test scoped suite log and real-CLI offscreen
  preview log. They do not exercise these five failure paths.
- Early Stop before a `program_process` is recorded falls back to signaling only
  `launcher_process` (`runs.py:28–30`, `108`). Whether the actual systemd launcher
  forwards termination in every startup phase was **not verified**. Add a
  startup/cancellation regression and require confirmed child exit before
  releasing locks; the demonstrated R1 failure does not depend on this question.
- The default GUI command uses `python -m anyps3_manager`
  (`anyps3_gui/app.py:457`), unlike workers' `python -P`. Launching from another
  checkout can select its manager through the current directory. Use safe-path
  module invocation for consistent package resolution; this was established
  from the argv, not a malicious-module execution probe.
- No new finding is asserted for deferred runtime window/pad controls or missing
  original-game/physical-controller acceptance; those limits are disclosed.
- No source fixes, full regression rerun or original-game validation were
  performed by this reviewer. Recheck the corrected candidate and add focused
  regressions for R1–R5 before treating wrapper verification as complete.

## Corrected-candidate recheck — 2026-10-20

**R1–R5 are resolved for the demonstrated failure cases. No new confirmed
correctness finding was identified in this focused recheck.** The findings above
describe the earlier candidate and are retained as the audit history.

| Finding | Corrected behavior and reviewed regression |
| --- | --- |
| R1 | `runs.run` protects launch and subsequent identity/record writes with cleanup; the child inherits operation/run lock descriptors. `Pipeline.locked` propagates operation/run/machine lock descriptors to tool launches. `stop_tree` checks observed process exit after escalation; Stop records an intent before launch and can terminate a surviving recorded scope/tree. `Library.run_lock` also checks recorded running process/scope liveness. Reviewed regressions demonstrate immediate post-launch cancellation/reaping, supervisor SIGKILL with locks held until the surviving child is stopped, and cancellation before child launch. |
| R2 | Shared `capabilities.environment_reads` recognizes literal reads through generic environment helpers, and both live schema discovery and snapshot records use it. Independently queried the corrected scanner: RSX-worker availability is now `true`. The regression checks the actual runtime source and emitted `ANYPS3_RSX_WORKER=1`; alternate-source build coverage checks its recorded capability too. |
| R3 | Detached worker argv now includes the resolved `--source`. The regression imports and builds using a distinct source marker and matching profile, then checks the actual saved snapshot and profile bytes. |
| R4 | The wizard follows a submission response only when it is not a terminal `result`. The Qt regression delivers a final firmware result containing `job`, verifies exactly one import request with the returned firmware ID, and verifies exactly one submission signal. |
| R5 | Adoption validates keys and username before publication, registers an incomplete title before copying, verifies copied userdata, and journals directory publication. Recovery completes only the remaining renames and rechecks the copied executable/userdata before marking the title imported. Regressions cover missing-key retry and interruption between publication renames, preserving source and destination userdata hashes. |

Also verified the default GUI command now uses `python -P -m anyps3_manager`.
The submitting process uses a daemon reaper for its detached worker; it does not
wait for job completion before returning the submission result.

**Validation evidence read:** `/tmp/anyps3-manager-suite-11.log`,
SHA-256 `97b32468a45fd175cd1457d86c906a46cb44e951d52a3fce12591ead84e9be8a`.
The log records 30 passing tests in 17.136 seconds, with no skipped tests or
resource warnings. This reviewer inspected the eight new regression cases and
their implementation paths; the suite was not redundantly rerun.

**Candidate identity:** SHA-256 of ordered `path:sha256(file)\n` entries is
`0bdbffd67ca3b06e78bdca7e8d72340c1da887622bd136dfb5823a5c4e29ffcc`,
for these files in order:
`anyps3_manager/processes.py`, `anyps3_manager/storage.py`,
`anyps3_manager/runs.py`, `anyps3_manager/jobs.py`,
`anyps3_manager/pipeline.py`, `anyps3_manager/capabilities.py`,
`anyps3_manager/cli.py`, `anyps3_gui/import_wizard.py`, `anyps3_gui/app.py`,
`tests/manager/test_manager.py`, `tests/gui/test_gui.py`.

**Limits remain:** synthetic launch/locking evidence does not establish original
game behavior, physical controller support, or every possible systemd startup
interleaving. The source review and supplied scoped integration evidence support
the corrected wrapper paths; no original game, full runtime suite, GPU workload,
or hardware acceptance was run by this reviewer. Only this report was edited.
