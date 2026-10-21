# PADMOTION verification — 2026-10-21

Worktree `/home/martink/AnyPS3/local/ratchet/work/padmotion/tree`, branch
`feat/pad-motion`, base `73c93e3a10c54866d8a2f447de16fd44da90e044`.
Only small synthetic test harnesses were compiled; no game build/run, physical
controller test, host permission change or knowledge-base edit occurred.

The native tests use a fake `libSDL2-2.0.so.0` selected through a temporary
`LD_LIBRARY_PATH`, exercise the actual runtime dynamic loader and guest ABI, and
compile with warnings as errors. Independent golden values include +1 g Y → 399,
-1 g → 625, yaw ±90°/s → 635/389, saturation and big-endian bytes. Existing native
pad and VSH tests also use real SDL virtual controllers. Missing optional symbols,
missing one sensor, silent rumble policy, shutdown/restart, async renewal errors,
read/enable failures and output preservation are covered.

Commands from the worktree, with stdout/stderr redirected to the named logs:

```bash
env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  python -m unittest tests.test_native_pad_motion tests.test_native_pad \
    tests.test_native_hid tests.test_vsh_ui tests.test_savedata_vsh -v

env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  python -m tests.padmotion_mutations

# Existing isolated Qt environment (PySide6 6.12.0); this does not install anything.
PAD_QT_PY=/home/martink/AnyPS3/local/ratchet/work/manager-gui/tree/.venv/bin/python
env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
  "$PAD_QT_PY" -m unittest tests.manager.test_manager tests.manager.test_pads \
    tests.gui.test_gui -v

# Additional assertion added after that run: discover the new switch from source.
env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
  "$PAD_QT_PY" -m unittest \
    tests.manager.test_manager.ManagerTests.test_capabilities_emit_actual_runtime_worker_default -v
```

Evidence:

- `native.log`: 24 final scoped native regressions, all passed.
- `mutations.log`: 27 production-code variants, each separately compiled and
  rejected by a native assertion. Source files are never overwritten. Variants
  cover capability flags, both-sensor requirement, missing support query, rumble
  policy, motor order/amplitudes, renewal, zero/disconnect/End stops and End error,
  sensor enable/read, axis signs/scales/units/centers/offsets, yaw axis, saturation,
  report length, sensor-off output extent and PS bit.
- `manager-gui.log`: 33 manager, diagnostic and offscreen Qt tests, all passed.
- `manager-capability.log`: additional source-discovery assertion, passed.
- `manifest.json`: SHA-256 of runtime/manager/test inputs, profile (unmodified),
  reference revisions and these logs. GUI-only string wrapping after the Qt run
  has no behavior change.

Scope limits: no full repository suite or RSX/Vulkan validation was run for this
pad-only task. Native tests that load the full runtime do so using a tiny BLR
fixture, not the game's ELF. Physical USB/Bluetooth input, hardware calibration,
GUIDE delivery and the game's use of motion/rumble remain unverified. No
PADMOTION worker or test scope is left running at handoff. Existing team game
processes were observed and left alone. See [behavior and provenance](../../DUALSENSE.md).
