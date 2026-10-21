# GUI quickstart and isolated desktop installer — 2026-10-21

Scope: a GUI-first README and a Bash installer/updater for the Linux desktop app.
The user explicitly required isolation from development and changes in a separate worktree.
Prepared in worktree `local/ratchet/work/gui-installer/tree` (branch `feat/gui-installer`, base `3e0f5f9`); merged into `main`
by the coordinator on the user's instruction 2026-10-21 and the worktree removed. This does not advance runtime acceptance.

## Implementation (verified in source and integration tests)

- [README](../../../README.md) starts with install, desktop launch, import, Play and update instructions.
- [Installer](../../../scripts/install.sh) creates a fresh Git clone without shared objects and its own
  Python venv on every installation/update. Editable package registration points only into that private
  snapshot so the existing manager can find profiles and snapshot its runtime sources.
- Launchers pin the selected release, use isolated Python imports and a fixed tool PATH, clear development
  Python/Qt/library variables, and select separate installed library and preference paths. Pip's inherited
  configuration/environment cannot redirect writes to a development environment.
- The app, library and configuration default to `~/.local/share/anyps3-app`,
  `~/.local/share/anyps3-library`, and `~/.config/anyps3-app`. Existing direct-checkout defaults remain intact.
- Validation imports Qt and the actual manager, creates an offscreen QApplication and checks JSON profiles.
  One atomic symlink switch activates a validated release. Prior releases remain for existing jobs.
  No game data is initialized or migrated by the installer. Conflicting launchers are preserved.
- A complete Bash function must arrive before the installer starts, so a truncated download does not
  partially execute installation. An installation lock serializes concurrent invocations.

## Verification (verified, 4 tests passed, 0 skipped)

Test: [tests/manager/test_installer.py](../../../tests/manager/test_installer.py).
Saved summary: [installer-suite.log](../../manager/evidence/installer-suite.log).
Source and evidence hashes: [installer-verification.json](../../manager/evidence/installer-verification.json).
At the merge the hashes of `scripts/install.sh`, the test, both READMEs and the suite log still matched; the knowledge
documents (CURRENT, HANDOFF, INDEX, menu-progress, this record) were edited afterwards only to state the merged state.
Full per-install pip/clone logs are local-only under `local/ratchet/evidence-gui-installer-2026-10-21/` in the primary repository
(the downloaded dependency wheels were deleted with the worktree; re-download them to rerun offline).

From the repository root, with the wheels downloaded into a directory WHEELS:

```bash
systemd-run --user --scope --quiet \
  -p MemoryMax=4G -p MemorySwapMax=0 -p AllowedCPUs=6-11,18-23 \
  env TMPDIR=/var/tmp \
  ANYPS3_INSTALLER_WHEELS=$WHEELS \
  ANYPS3_INSTALLER_LOG_DIR=/home/martink/AnyPS3/local/ratchet/evidence-gui-installer-2026-10-21 \
  python3 -m unittest tests.manager.test_installer -v
bash -n scripts/install.sh
git diff --check
```

The test uses real dependency wheels (PySide6 6.12.0, cryptography 50.0.2, setuptools 84.2.1),
real Git clones and venvs, installed CLI commands, a real offscreen GUI, and desktop-file validation.
It verifies an update to a different commit; a dependency-resolution failure leaves the active version
and save sentinels unchanged. A deliberately broken development checkout plus conflicting PYTHONHOME,
PYTHONPATH, VIRTUAL_ENV, Qt paths and pip target/configuration do not contaminate the installed app.
Separate development library/preferences and the source checkout remain untouched.

Environment findings: `/tmp/.git` exists on this host, so the manager correctly rejected a test library
under that repository. Final tests used `/var/tmp` outside repository ancestry, in a 4 GB systemd user
scope with swap disabled. Setuptools expands `$` in installation prefixes; the installer detects this
before writing and explains the XDG_DATA_HOME alternative. Quoted/spaced/percent-containing paths and
dollar signs in launcher HOME paths are covered.

## Limits and handoff

No application was installed in the user's actual home; all test homes were removed. No source runtime,
profile, generated game artifacts or original game bytes were changed. No game compilation, gameplay,
physical controller or visible desktop-session launch was tested. Existing runtime performance evidence
and acceptance limitations in CURRENT remain unchanged. All test processes finished; host-wide process
visibility inside the sandbox is restricted.

The GitHub quickstart URL becomes available after integration and publication; this worktree has not been
merged or pushed. The local installer can already be run with `bash scripts/install.sh`. It installs the
remote `main` by default; `--repo`/`--ref` explicitly select a different committed source. Distribution
native dependencies and GPU/systemd readiness are prerequisites for building/playing, not installed by
this per-user script. Old application releases are retained and are not automatically pruned.
