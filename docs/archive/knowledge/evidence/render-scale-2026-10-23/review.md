# Final integration review — 2026-10-23

Read-only review of `feat/render-scale` at `local/ratchet/work/render-scale/tree` against33030a9. Reviewed source diff, new capture/portable helper tests, and prior paired/batching/GUI/filter/sampling reviews. No builds/tests/game launches or runtime edits. Main owns live804 diagnostic and all edits.

## Confirmed finding — source fix reviewed, focused test pending

**P2 — paired capture counts the same scanout twice in GPU frame telemetry.**

Locations: `anyps3/rsx_display.py:155` invokes `rsx_display_pixels` for the comparison after the visual scanout at143; `rsx_display_pixels:9` invokes `rsx_graphics_scanout` again. `anyps3/rsx.py:455` increments `session->batch->frames` on each successful capture scanout. `rsx_vk_graphics.py:1309` aggregates those markers into `graphics_profile_frames`, which is the denominator of PERF GPU completed-frame measurements.

Trigger: `ANYPS3_CAPTURE_NATIVE_PAIR=1` and a supported resident scanout, including first-eight/operator captures. One pair adds an extra GPU frame marker versus the identical unpaired path. Guest flips, guest bytes, query results and file pairing remain unchanged; this is diagnostic accounting, not an image corruption finding. GPU milliseconds/completed-frame inferred across these captures are biased low. Existing paired test checks file dimensions/content and guest counters but not GPU profile-frame accounting.

Smallest fix: explicitly suppress the additional marker for comparison-only scanout, or capture both native and child under one scanout invocation. Preserve the public/default path's previous marker behavior. Add an independent assertion that enabling comparison capture does not change the completed GPU frame count relative to the equivalent unpaired capture sequence. Do not infer performance from804 pair windows until excluded/corrected. This need not invalidate earlier803 unpaired timing.

## Other review conclusions

No other confirmed new renderer/GUI correctness blocker found in the bounded final pass. Prior reviewed fixes remain present: native queries excluded from visual jobs; separate mutable shader status; preflight staging for paired payloads; deferred visual queue drains and conditional unwind poisoning; timestamps begin after transitions; child/parent pins; typed RTT alignment gates; actual capture extents. GUI typed choices and capability-gated environment propagation are unchanged from the independently reviewed version. Flip-only trace gates remain before clocks/peeks and carry header mode metadata.

Portable `scripts/ratchet_fast_menu.py` is explicitly diagnostic and uses only virtual START control files. It rejects invalid timeout values, stops on guest STOP, consumes complete new log lines once, and releases an active START on timeout/exception/SIGTERM. Eight offline tests cover protocol/classification/incomplete lines/confirmation/release. Initial existing log contents are processed once; “fresh” here means unread records, not proof of a live process. Operators must use a fresh run directory and exclusive control as documented; the helper's menu heuristic is not menu/gamepad acceptance. It neither patches guest assets nor implements a substitute menu.

The review used source inspection and saved test evidence only. Parent reports latest two capture tests and eight portable startup tests passed; this reviewer did not rerun them. Full runtime/source snapshot evidence and live804 outcome remain main-agent handoff responsibilities.

## Known limitation, not a newly discovered native correctness regression

General nondiscard merge/equal-footprint aliases, unsupported RTT conversions, and host invalidations can reconstruct visual children from authoritative native images. This can erase high-resolution history even with2560×1440 output. Supported raster/typed RTT tests prove capability, not final-world detail retention. Manual resident sampling already uses actual bound image dimensions; no new quantization defect was found. See `resscale-quality-loss.md` and `resscale-sampling-quality.md` for exact mechanisms and pending lineage evidence.

Commit/push may describe the implemented milestone and tests, but must retain the explicit limits: above720p stable60FPS is unachieved, final-world quality improvement needs same-frame evidence, and physical controller verification was not performed here. These are goal limitations, not reasons to mislabel or discard the completed engineering milestone.

## Follow-up verification of main-agent fix

Re-read current source after main-agent correction: `rsx_graphics_scanout` now has optional `count_frame=true`, guards both marker increments, and comparison-only capture calls it withfalse (`rsx_display.py:157`). Existing callers retain their previous behavior. Fallback executes only after unsuccessful resident lookup, before any marker was added. This addresses the confirmed diagnostic defect without changing native image selection or guest counters.

The paired fixture now enables PERF reporting and asserts, after `life.finish()`, that `graphics_profile_frames==kFlips` (24), independent of capture count. Source fix and assertion reviewed; focused tests are running under the main agent, not rerun here. **No remaining confirmed integration blocker from this review**, conditional on that focused validation passing. Already-built804 retains the old diagnostic accounting and its pair-window GPU denominator must remain excluded; main plans a fresh805 build.

## Resolved validation and upstream GUI compatibility

Confirmed revised test reads subprocess stderr `PERF GPU ... frames=...`, requires at least one report, and asserts the last cumulative frame count equals24. It no longer dereferences the renderer after lifecycle teardown. Saved `paired-capture-tests-reviewed.log` reports both paired and scaled synchronous/asynchronous capture tests passed (2 tests,24.223s). The accounting finding is **resolved**. The earlier teardown dereference was test-only and is absent from the revised check.

Reviewed `git diff 33030a9 origin/main -- anyps3_gui/settings.py anyps3_manager/settings.py` for upstream54cea81 integration. Upstream changes Settings.load to accept callback results, passes the bound load method directly, preserves active tab group across rerender, and adds global-only tooltip text. Our typed-choice change is confined to combo construction/selection/getter; it is semantically compatible with those changes. Retain all upstream callback/tab/tooltip changes and our itemData/currentData logic. No upstream manager/settings diff appeared in this comparison. No predicted semantic conflict in these two files; still inspect actual rebase result and run the planned focused GUI tests, since source comparison is not a completed rebase validation.
