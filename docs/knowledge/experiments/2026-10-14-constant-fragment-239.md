# Constant fragment shader during ACIT loading

Date: 2026-10-14. Goal: the original ACIT menu and gamepad navigation; not yet met.

## Verified performance blocker and change

Run 238 alternately renders a QUAD with 921,600 background pixels and a QUAD with 17,161 pixels
of the loading icon (`native-bundle-238/stderr.log`, the first sixteen `native RSX draw`).
The single-instruction original constant MOV was interpreted anew for every quad,
including creating registers and interpolating unused inputs.

`anyps3/rsx_draw.py:709` recognizes exclusively a shader with a single MOV
from an inline constant. On the first covered quad it evaluates it with the existing interpreter;
that preserves predicates, masks, precision, H/R aliasing and explicit errors.
`rsx_draw.py:757` does not create registers and interpolated inputs for this branch for every
quad. Coverage, the top-left rule, culling, clipping, depth/alpha, blending, masks,
guest memory writes and the counts of rendered pixels remain actually performed.
Other shaders continue through the general path. The game bytes do not change.

Source hash of the changed `rsx_draw.py` from the snapshot of build 239:
`70ef9fff4439764ce625009fc90ce582fec28499ee0e3d1faf4b0c79fc825fec`.
It is the only runtime file changed relative to 238; the profile stayed the same.
The previous implementation for diagnostic comparison:
`local/ratchet/rsx_draw-before-239.py`.

## Verified tests and measurements

- `tests/test_rsx.py:250`: native expectation of eight exact color pixels,
  scissor, source-alpha blending, masked blue components, LESS depth, preservation of the
  stencil byte and rejection of further fragments via depth/alpha. The same shader
  with a leading NOP verifies the general path against the same independent expectation.
- `python -m unittest tests.test_rsx.RsxDrawTests -q` → 6 tests, OK (10.481 s),
  `local/ratchet/tests-rsx-239.log`.
- `ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q` →
  **174 tests, OK (205.634 s), no skips**;
  `local/ratchet/tests-native-239.log`. The Vulkan test thus actually ran.
- `PYTHONPATH=. python local/ratchet/tools/benchmark_constant_draw.py` →
  twenty draws of 1024×512, 10,485,760 pixels; old branch 611,131 µs,
  new 267,785 µs (~2.28× faster). Both programs verify the exact pixel count
  and every resulting byte of the entire black surface. Sources, binaries, logs and hashes:
  `local/ratchet/draw-benchmark-239/result.json`. It is a synthetic renderer measurement,
  not a game run or evidence of the menu; the speed of the whole game cannot be inferred from it.

## New build and further verification

The new `native-bundle-239` was created by the standard `anyps3.build_game` with the original profile,
firmware and `--max-steps 5000000000`, then `c++ -std=c++17 -O2 -Wall -Wextra -Werror
-DANYPS3_VULKAN … -lvulkan`. Compilation successful in 201.298 s. Logs:
`local/ratchet/build-239.log`, `native-bundle-239/compile.log`.
All source hashes and the profile snapshot match the current files.

Run 238 remains the comparison real run of the old renderer; at 15:31:43 UTC it had
315 reads, 14,947,223 loaded bytes, draw 18,432 and 162 frames, session `36487`
confirmed alive. It passed the last read of 235/236 (270 reads, 13,187,843 bytes).
**Verified:** the larger budget moved the original loading past the old SPU limit.
The counts of draws and frames are still not the menu.

Originally planned command (not executed):

```bash
ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-bundle-239 5000000000 > local/ratchet/run-239.log 2>&1
```

Sparser frame recording changes only the diagnostic interval; no guest draw or flip is
skipped. Then verify the run's own `verification.json`, the PT_LOAD bytes,
the artifact hashes, the stop and the captured pixels. This game verification had not yet
taken place at the time of writing. The virtual controller, the physical gamepad and menu navigation are not verified.

**Update after the end of 238:** a new static stop `0x962700` was reached. Build
239 was not run, it has its own `NOT_RUN.txt`; it was replaced by build 240
with the same tested implementation and an extended verified profile. The first
real game verification of the optimization will therefore be 240, see
[record 240](2026-10-14-vtable-240.md).
