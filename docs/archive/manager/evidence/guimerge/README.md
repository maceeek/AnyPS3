# GUIMERGE verification — 2026-10-21

Resolved `feat/pad-motion` (`ebced7e`) into `feat/gui-pad` (`6bc7ff0`,
containing `feat/gui-audio`). The merge was already in progress at takeover.

## Conflict resolutions

- `anyps3_manager/pads.py`: retain SDL >= 2.0.18 diagnostics and the
  `incompatible-sdl` error before initialization; combine capability helpers and
  the accelerometer + gyro `motion` field. The runtime retains its separate
  optional-symbol fallback for older SDL.
- `anyps3_manager/settings.py`: retain default-on `audio.enabled`, legacy
  lock/sink migration, overrides and silent tool environments; add default-on
  `input.rumble`, exported only to builds supporting `ANYPS3_PAD_RUMBLE`.
- `docs/manager/DUALSENSE.md`: combine implemented guest features, input mapping,
  motion scales, versioned provenance, SDL requirements, udev/hidraw permissions,
  synthetic verification and physical-controller/game limitations. Remove obsolete
  missing-motion/rumble/PS statements.
- `docs/manager/README.md`: retain the audio toggle documentation and describe
  implemented motion/rumble/PS input and separate manager/runtime SDL requirements.
- `tests/manager/test_manager.py`: retain audio default/migration/override and
  silence tests; combine the rumble setting and source-capability tests.
- `tests/manager/test_pads.py`: combine missing-symbol/controller-only
  initialization tests and capability-query tests. The old audio-lock and silence
  test names follow the audio branch's updated names and expectations.

The automatically merged runtime, GUI, GUI tests, native motion tests and
mutation harness remain byte-identical to `feat/pad-motion`. Its original evidence
under `../padmotion/` describes that branch, not the merged source. The current
source and log hashes are recorded in `manifest.json` here.

## Commands and scope

From `/home/martink/AnyPS3/local/ratchet/work/guimerge/tree`:

```bash
# Reuse the existing PySide6 6.12.0 environment; system Python has no PySide6.
systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env PATH=/home/martink/AnyPS3/local/ratchet/work/manager-gui/tree/.venv/bin:/usr/local/bin:/usr/bin:/bin \
  SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
  python3 -m unittest tests.manager.test_manager tests.manager.test_native_flags \
    tests.manager.test_pads tests.gui.test_gui -v > manager-gui.log 2>&1

systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
  python3 -m unittest tests.test_native_pad_motion tests.test_native_pad \
    tests.test_native_hid tests.test_vsh_ui tests.test_savedata_vsh -v \
    > native.log 2>&1
```

**Verified:** 41 manager/Qt tests and 24 native tests passed, both with exit
code 0, no failures, errors or skips.

The actual log destinations are this evidence directory. Results and return codes
are recorded in `manifest.json`. Qt uses offscreen fixtures and stub CLI calls;
manager tests compile/run synthetic fixtures, never the game. Enabled audio is
checked only by inspecting environment dictionaries; every launched test child
uses dummy/null audio. Native tests use fake SDL or virtual devices and tiny BLR
fixtures. No physical controller, audible output or original-game build/run was
attempted. No changes to `main`, push, profile or `docs/knowledge/` were made.

This scoped verification covers the merged manager/Qt and native pad integration
behavior. The full repository suite, opt-in installer suite and mutation harness
were not rerun. Physical USB/Bluetooth, calibration, GUIDE delivery and original
menu/game use of motion/rumble remain unverified. Existing unrelated team game
processes were left alone.
