# Resolution-scale milestone evidence

The quiet original Great Clock run803 averages45.5198FPS at2560×1440
with the existing recorder option enabled (`ANYPS3_RSX_RECORDER=1`).
Original PT_LOAD bytes match; no guest STOP occurred. Harness SIGKILL(-9)
ended completed runs. Stable60FPS and physical-gamepad acceptance remain open.

The two crops show the same flip from804, frame9. Native crop:280×180 at
(500,140), enlarged2× with nearest filtering. Visual crop:560×360 at(1000,280),
no resampling. Both come from original game pixels. Some edges/detail improve,
but broad world softness remains. Pixel differences alone do not prove
full-resolution scene detail. See quality-loss.md and sampling-quality.md.

804 paired-capture diagnostic double-counted GPU frame markers; this is fixed
in805 with an independent24-flip regression. Neither paired run is an FPS
acceptance benchmark. The quiet803 result is unaffected.

The complete802 suite covered932 cases:925 passed and7 skipped after one
unchanged five-second concurrent SPU fixture passed an uncontended rerun.
The original suite report remains failed. Later flip instrumentation, GUI,
startup and paired-capture additions have separate targeted test logs; these
are not represented as a full-suite run of the final branch.

Large binaries, original game bytes, saves and local-only logs are excluded.
Source and full local evidence paths are in the linked experiment record.

Final build805 reached the original level with matching native/scaled captures,
original PT_LOAD identity and no guest STOP; see final-805-verification.json.
The source hash comparison and rebased validation scope are in
source-validation.json. GUI14 and manager132 checks passed, with4 manager skips.
