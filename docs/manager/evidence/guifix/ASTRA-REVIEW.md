# Independent GPT-6-Astra review — GUIFIX

2026-10-21, read-only native subagent, effort xhigh. Scope: current GUIFIX diff
against `5a91ddb`, manager/GUI packages and regression tests. Reviewer ran no
builds/tests and made no changes. The coordinator owns runtime acceptance.

Verified findings and resolution:

1. Selected rows from title A remained actionable under newly selected title B
   while asynchronous replies were pending. Clear all tables/selections on title
   switch; delayed/reordered-reply test proves rows and actions stay empty.
2. Worker-only history lock did not survive supervisor death. Pass the same
   inherited lock descriptor from the submitter through Python startup, worker
   and tool/game processes; record launcher identity and intended unique scope.
   A real subprocess test interrupts submission after Popen but before PID
   publication. Removal refuses while the inherited child is alive.
3. Unconditional hard-link ZIP publication failed on filesystems such as exFAT.
   Exclusive-create copy fallback handles unsupported-link errors, preserves
   existing destinations and cleans partial output on failure. Its in-progress
   visibility is documented.
4. Popen failure left a permanently queued job and blocked title actions.
   Persist terminal failure and an event before returning an actionable error.
5. Same-title build activation kept old settings capabilities. Reload schema and
   values on active-build change while preserving the settings group.
6. Identical job lists did not refresh a foreground game after Stop. Poll title
   state on every interval and suppress overlapping title requests.
7. History cleanup held shared metadata while deleting logs and could fail
   unrelated title operations. Use a separate history lock; regression performs a
   title update during deletion.
8. Orphan cancellation could select a newer run for the same title. Bind Stop to
   the original run ID and its checked process identities/unique scope.
9. A shutdown callback guard used lambdas around bound Qt slots, losing receiver
   lifetime tracking. Reproduce delayed replies after Settings deletion, restore
   direct slots, and suppress emissions inside Call during shutdown.

The reviewer also checked follow-up iterations: conservative bootstrap
cancellation, refusal to declare unknown inherited children cancelled, and the
shared descriptor protecting the interval before worker entry. Initial automatic
submitter reconciliation was rejected until that inherited-lock contract existed.

Final reviewer response:

> Final read-only recheck: no remaining confirmed findings in the reviewed candidate.

The reviewer inspected passing 8-test lifecycle and 38-test Qt evidence, including
interrupted startup and deleted receivers. Later coordinator changes are covered
by the final report and validation logs.

A final read-only recheck of the incremental live-counter parser and direct
Settings Apply/Inherit callbacks reported no confirmed findings. It verified
partial-line buffering, consuming each complete line once, shared final parsing,
and Qt receiver disconnection after deletion. No reviewer tests/builds were run.
