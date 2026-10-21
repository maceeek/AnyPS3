# GUIFIX verification — 2026-10-21

Candidate: `feat/gui-fix`, based on `feat/gui-keys` at `5a91ddb`. All changes are in
this worktree; no push, installed-app update, user-library change, runtime/profile
change, or original-input modification is part of this task. Fix commit `c4666ae` contains every fix and regression listed below; the separate
evidence commit contains this report and artifacts.

## Defects and regression coverage

All rows below are **verified** from source, reproduction, or a failing baseline
regression. Test names are abbreviated within the linked module.

| Defect | Fix | Independent regression |
| --- | --- | --- |
| Failed ISO import blocked adoption permanently | Replace only disposable incomplete publication; retain userdata/builds/live jobs | [recovery](../../../../tests/manager/test_guifix_recovery.py): `adoption_replaces_failed_import_and_cleans_staging`, `replacement_refuses_userdata_builds_and_live_job` |
| Failed adoption blocked import / interrupted operations had no GUI exit | Stored source/options, legacy history recovery, `titles resume`, GUI Resume/Remove; removal preserves userdata | recovery: `failed_adoption_can_be_replaced_by_import_without_published_userdata`, `failed_import_is_resumable_and_keeps_verified_extraction`, `legacy_incomplete_import_recovers_source_from_job`, `resume_cli_replays_source_and_options`; existing interrupted-adoption tests |
| Finished/failed/cancelled jobs accumulated forever | CLI multi-ID remove/clear; GUI multiselect; validate whole selection before deleting | recovery: `multiremove_deletes_only_terminal_history_never_title`, `mixed_selection_is_atomic_and_rejects_running_or_queued`, `clear_retains_active_jobs_and_live_terminal_worker` |
| Killed supervisor made surviving tools look finished/removable | Same lifecycle FD inherited from submission through worker/tool/game; checked launcher/scope; conservative unknown-child errors | [jobs](../../../../tests/manager/test_guifix_jobs.py): `killed_supervisor_keeps_live_tool_and_cancel_stops_descendants`, `unknown_inherited_child_keeps_history_and_cannot_claim_cancellation` |
| Submission interruption before PID publication escaped the worker lock | Acquire before Popen and pass the same open-file description into Python startup | jobs: `worker_holds_lock_before_spawn_publication_and_python_worker_entry`, `cancel_during_bootstrap_records_request_without_terminalizing`, `dead_submitter_before_spawn_is_failed_and_removable` |
| Popen failure left permanently queued/busy title | Persist failure/event before returning `worker-start` | jobs: `submission_failure_is_terminal_removable_and_does_not_block_title` |
| Cleanup could contend with unrelated title metadata writes | Dedicated history lock | jobs: `history_cleanup_does_not_lock_unrelated_title_metadata` |
| Orphan Cancel could target a newer game for the same title | Bind recovery Stop to original run ID and checked identities/scope | jobs: `orphan_run_cancellation_is_bound_to_original_run` |
| ISO path appeared as tiny form label beside an oversized chooser | Labeled expanding field + normal-width button; full-path tooltip | [Qt](../../../../tests/manager/test_guifix_gui.py): `selected_iso_path_expands_instead_of_becoming_a_narrow_label` |
| Imported title had unexplained disabled Play | Prominent Build button, inline next step, disabled-action tooltips | Qt: `imported_title_explains_build_then_play` |
| Empty/removed titles left stale details and enabled actions | Reset selection, tables, image and title controls | Qt: `empty_library_gates_actions_and_clears_removed_title` |
| Active/terminal jobs and unsupported controllers enabled wrong actions | State/selection gates for Cancel, removal, run/build/save/build management and rumble | Qt: busy-title, multiselect, build-activate-pin-prune and controller tests |
| Failed/cancelled jobs left titles busy until manual refresh | Error preserves diagnostics and refreshes; polled terminal transitions refresh too | Qt: `failed_follower_preserves_error_and_refreshes_title_controls`, `polled_terminal_job_refreshes_title_after_missed_follower` |
| Foreground games could not be stopped, or exit left stale controls | Live run records expose running state; periodic guarded title polls independent of jobs | Qt: `foreground_live_run_without_detached_job_enables_stop`, `unchanged_empty_jobs_poll_detects_foreground_run_exit`, `title_polls_do_not_overlap`; recovery: foreground-run test |
| Title A selection could mutate title B while replies were pending | Immediately clear tables/selections on title switch; reject stale replies | Qt: `title_switch_clears_actions_before_delayed_cli_replies`; mutation evidence [log](gui-selection-mutation.log) |
| Incomplete-to-ready transition could reuse another title's Settings | Associate settings widget with actual title ID | Qt: `recovered_title_does_not_inherit_another_titles_settings` |
| Rollback/rebuild kept old build's setting capabilities | Reload schema on active-build change | Qt: `same_title_build_change_refreshes_setting_capabilities` |
| Applying a setting jumped to another group | Preserve active group during schema render | Qt: `settings_audio_rumble_and_advanced_schema` |
| Wizard could submit repeatedly; pasted key input was visible | Busy submission gate, actionable retry, masked key editors | [Qt/key tests](../../../../tests/manager/test_gui_keys.py), Qt wizard/all-pages test and [GUI protocol tests](../../../../tests/gui/test_gui.py) |
| Closing GUI spawned refresh requests and destroyed running QProcesses | Suppress callbacks before shutdown, stop/reap GUI clients, preserve detached workers | GUI protocol close regression and [baseline reproduction](gui-close-baseline.log) |
| Guard lambdas escaped Qt receiver lifetime; delayed settings replies/writes used deleted widgets | Direct QObject-bound callbacks, Call-level shutdown suppression; direct set/reset reload slots | GUI protocol deleted-settings and deleted-settings-write tests; [baseline](gui-deleted-settings-baseline.log) |
| Export followed selected/ancestor save symlinks and left partial ZIPs | Reject links throughout path; complete temporary archive; exclusive publication and cleanup | [operations](../../../../tests/manager/test_guifix_operations.py): symlink/failed-export tests |
| USB filesystems without hardlinks could not export | Unsupported-link fallback uses exclusive copying; no overwrite and partial cleanup | operations: three `export_without_hardlinks` / `failed_export_copy_without_hardlinks` tests |
| Nested empty save directories disappeared | Export explicit directory entries | operations: `save_roundtrip_preserves_nested_empty_directories` |
| Malformed ZIP metadata crashed with raw Python errors | Validate object/schema/title/save ID before mutation | operations: `import_rejects_metadata_without_touching_userdata` |
| Restoring an empty snapshot did not clear later saves | Archive explicit empty savedata/trophy roots; retain pre-restore backup | operations: `restore_of_empty_snapshot_clears_later_saves_and_preserves_pre_restore_backup` |
| Live run records lacked flip/frame/FPS evidence until exit | Persistent incremental stderr reader publishes about once per second, sharing final parser | operations: live-before-Stop fixture and partial-line/no-double-counting test |
| Stop lost flip/capture boot evidence; rates alone could claim boot; later boot downgraded play | Shared finalization records cumulative counters, requires flips, preserves verified-play | operations: manual-stop, lost-supervisor, cumulative-counter, rate-only and verified-play tests |
| Manual Stop bypassed run-history retention | Prune completed records in finalization, preserving active runs | operations: manual-stop and lost-supervisor retention tests |

[Independent Astra review and rechecks](ASTRA-REVIEW.md) found no remaining
confirmed findings in the reviewed candidate. Root subsequently also removed
Settings Apply/Inherit receiver-lifetime lambdas and added a real QProcess/CLI
walkthrough regression.

## Visual walkthrough

Offscreen Fusion captures use the actual application stylesheet and isolated
QSettings. Baseline GUI modules came from `5a91ddb` in an external temporary
snapshot. No key values were entered into screenshots. Every wizard page, key
form, main window tab and major empty/incomplete/imported/built/stale/running/error
state was rendered and personally inspected. Additional captures show resumed
recovery, terminal-job multiselection and an empty controller list.

The confirmed ISO layout defect widened the editable path from roughly 125 px to
489 px at 700 px wizard width, while reducing Choose from roughly 528 px to 113 px.
The full path is also available as a tooltip.

- [ISO selection before](before/14-wizard-iso-selected.png) / [after](after/14-wizard-iso-selected.png)
- [Incomplete title before](before/02-incomplete.png) / [resumable after](after/02-incomplete-resumable.png)
- [Imported title before](before/03-imported-no-build.png) / [Build explanation after](after/03-imported-no-build.png)
- [Terminal-job multiselection](after/10-jobs-multiselect.png)
- [Real temporary-library build progress](real-building.png)
- [Complete screenshot inventory](screenshots.md)

**Not reproduced:** compositor-specific stale paint/popups. Offscreen captures
showed no unrepainted regions. Xvfb/xvfb-run were unavailable, so no xcb/Xvfb result
is claimed. The layout/state fixes are verified; they are not proof of resolving
a separate driver/compositor artifact described only as a “graphical relic”.

Reproduce synthetic captures from an installed development environment:

```bash
export SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null
bash /home/martink/AnyPS3/local/ratchet/tools/quiet_wait.sh
systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen GUIFIX_PHASE=after \
  /var/tmp/anyps3-guifix-venv/bin/python -m unittest discover \
  -s docs/manager/evidence/guifix -p capture.py -v
```

## Validation and original-game acceptance

Final results and temporary-library cleanup are recorded in [acceptance.md](acceptance.md).
All actual game launches explicitly disable audio; synthetic enabled-audio checks
only compose environments and never launch an audible process.
