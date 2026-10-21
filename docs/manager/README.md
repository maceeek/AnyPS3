# AnyPS3 Manager

Linux CLI and PySide6 desktop wrapper implementing the manager/GUI work packages
in [SPEC.md](SPEC.md). The runtime remains a separate package and process.

## Install and start

For the desktop application, use the [GUI quickstart](../../README.md#quickstart-install-the-desktop-app):

```bash
curl -fsSL https://raw.githubusercontent.com/maceeek/AnyPS3/main/scripts/install.sh | bash
~/.local/bin/anyps3-gui
```

Rerun the installation command to update from `main`, then reopen the GUI. Installation is per-user;
no sudo or virtual-environment activation is needed. The installer checks Python, Qt and game profiles
before switching the app to a new source snapshot. Old snapshots remain available to existing jobs.
App updates preserve game builds and saves; choose **Rebuild** to use a newer runtime for a title.

The installed app uses `$XDG_DATA_HOME/anyps3-app` (default `~/.local/share/anyps3-app`),
a separate library at `$XDG_DATA_HOME/anyps3-library`, and preferences under
`${XDG_CONFIG_HOME:-~/.config}/anyps3-app`. Launchers use their recorded installation paths and clear
development Python/Qt paths. They never import packages from the directory you launch them in.
The installer refuses to replace an unrelated command or desktop entry with the same name.

To inspect the installer before running it, download [scripts/install.sh](../../scripts/install.sh).
From a checkout you can run `bash scripts/install.sh` directly. Advanced options:

```bash
bash scripts/install.sh --help
bash scripts/install.sh --repo https://github.com/maceeek/AnyPS3.git --ref main --python python3
```

`--ref` selects a branch or tag. `--repo /path/to/checkout` installs a separate clone of its committed
branch; uncommitted changes are excluded. Neither form modifies the original checkout. Use the same
options on subsequent updates. Older releases are retained rather than automatically pruned.
Pip's inherited environment and configuration are ignored so they cannot redirect installation into
a development environment. `--wheelhouse /path/to/wheels` uses predownloaded dependency/build-backend
wheels instead of PyPI. If an installation path contains `$`, choose an `XDG_DATA_HOME` without it;
setuptools treats dollar signs as variable references during editable installation.

If the GUI cannot load its X11/Wayland plugin, install the distribution's native Qt dependencies
(including `xcb-cursor` for X11); see [Qt's Linux requirements](https://doc.qt.io/qt-6/linux-requirements.html).
For build/run prerequisites and available resources:

```bash
~/.local/bin/anyps3-manager doctor --json
```

The doctor reports missing native dependencies; it does not install them.

## Developing from a checkout

Use a checkout-local environment and a development library outside all source checkouts:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gui,self]'
.venv/bin/anyps3-gui --library ~/.local/share/anyps3-dev
```

Direct package entry points default to `$XDG_DATA_HOME/anyps3` or `~/.local/share/anyps3`;
the installed app's launchers explicitly select `anyps3-library` instead. Use `.venv/bin/` for development
commands so shell PATH lookup cannot select the installed app. Keep development data outside both the
worktree and primary repository, and do not point tests at the installed app's library. To choose another:

```bash
.venv/bin/anyps3-gui --library /path/outside/repository/library
.venv/bin/anyps3-manager --library /path/outside/repository/library init --json
```

The GUI uses `QProcess` for every manager operation. It never imports manager or
runtime modules, and only persists window geometry through `QSettings`.
PySide6 is optional for CLI-only installations.

## First title

Use **Import ISO**, or adopt an existing Ratchet setup with **Adopt Ratchet**.
Adoption copies the disc, executable, firmware and HDD; it does not register
diagnostic virtual-pad builds or modify the source directory.

Equivalent CLI:

```bash
anyps3-manager keys set app /path/to/app-key.json
anyps3-manager keys set pup /path/to/firmware-keys.json
anyps3-manager inspect-iso /path/to/game.iso --json
anyps3-manager import /path/to/game.iso --json
# The result contains a detached job ID.
anyps3-manager jobs follow JOB_ID
anyps3-manager titles show BCES00511 --json
anyps3-manager run BCES00511
```

Or:

```bash
anyps3-manager adopt-ratchet /path/to/local/ratchet --events
anyps3-manager build BCES00511 --reason 'First player build' --events
```

`--events` follows a detached job until it ends. Disconnecting its CLI follower or
closing the GUI leaves the worker running. Reattach through `jobs list` and
`jobs follow ID`. `jobs cancel ID` cancels the worker and its active tool processes.
Do not infer success from a stale PID: process identities include the kernel boot
ID and process start time.
Detached workers use the selected `--source` checkout for profiles and source
snapshots. Tool/game processes inherit their operation locks; a killed supervisor
cannot make a still-running game available for restore, prune or another launch.

## Builds and saves

Every build gets its own source/profile snapshot, bundle, executable, verification
record, compiler flags, CPU identity and logs. The original nonempty PT_LOAD bytes
and translator source hashes must match before activation. Finished build trees
are read-only. Boot evidence is stored with runs, separately from immutable builds.

`activate TITLE BUILD` rolls back without replacing userdata. `pin` protects an
older build. `prune TITLE --keep N` preserves active and pinned builds.
Reimporting a changed ISO requires `--replace-disc TITLE` and marks the active
build stale. Interrupted imports retain verified extraction for retry.
Adoption validates keys before publishing a title and journals publication, so an
interruption between directory renames can be resumed by repeating the command.

Save commands: `saves list`, `backup`, `backups`, `restore`, `export`, `import`.
Restore/import require the run lock and first create a backup. ZIP import accepts
only AnyPS3 exports for the same title; archive traversal and links are rejected.
Backups use standard-library Zstandard on Python 3.14+, with a gzip fallback on
older supported Python versions. Manual backups are never automatically pruned.
Removing a title keeps userdata unless `--delete-userdata` is explicitly supplied;
even then a final backup is kept unless `--no-backup` is supplied.

## Settings and launch limits

`settings schema --json` describes all fields. Settings layer defaults, global
values, per-title overrides, and one-off `run --set KEY=VALUE` values. The GUI
renders this schema and disables switches absent from the active source snapshot.
Advanced/diagnostic groups are hidden by default.

The audible lock defaults to locked. Ambient `ANYPS3_*`, `SDL_*`, `VK_*` and
`LD_PRELOAD` variables are removed before composing a launch. Locked launches and
all build/import tools force `SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`.
On the development PC **keep the lock locked**. The GUI cannot unlock it.

Translation, compilation and game launches require systemd user scopes with
`MemoryMax` and `MemorySwapMax=0`. On this PC the defaults are the specification's
CPU sets. `--unscoped` is an explicit escape hatch for other machines and test
fixtures; it is not a fallback when systemd is missing.

`stop TITLE` sends SIGTERM, waits, then SIGKILL to the recorded process identity.
The GUI warns to save in-game first. Run records preserve stdout/stderr, runtime
STOP, errors, FPS intervals, exit status, memory observations and elapsed time.
Verified boot means observed flips, **not** a verified original menu or gameplay.

## Runtime-dependent features

This worktree does not implement SPEC WP4 or pad-runtime P2–P7. Current runtime
window/fullscreen settings and custom pad configuration are therefore unavailable.
Controller enumeration, live input and rumble diagnostics use lazy SDL2 ctypes,
without initialising audio. The controller screen states the missing runtime
capabilities; default SDL mapping remains in use.

Physical Xbox and DualSense gameplay acceptance, the original-game
build → play → rebuild → reload-save acceptance, unlimited runtime budgets and
graceful runtime shutdown remain separate work. No synthetic test claims those.

## Verification

Installer regression checks use real Git clones, Python environments, dependency wheels and Qt,
with temporary application/library directories. Predownload the dependencies first, then run from
the checkout (choose a temporary directory outside any Git repository):

```bash
# WHEELS is an absolute path containing PySide6, cryptography, setuptools>=68
# and their transitive wheels for this Python version and platform.
systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \
  env TMPDIR=/var/tmp ANYPS3_INSTALLER_WHEELS="$WHEELS" \
  python3 -m unittest tests.manager.test_installer -v
```

These opt-in tests cover installation, update, failed-update preservation, GUI/CLI launch under a
conflicting development environment, independent data, launcher collisions and truncated downloads.
See the [installer evidence](../knowledge/experiments/2026-10-21-gui-installer.md).

Run on the development PC:

```bash
systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \
  -p AllowedCPUs=6-11,18-23 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest \
  tests.manager.test_manager tests.manager.test_native_flags tests.gui.test_gui -v
```

The tests use a synthetic ISO, fake translation/compiler commands via `--toolchain`,
and a tiny real C++ compiler fixture. They exercise save invariants, locks, silent
environments, import retry, detached-job replay, scoped launch/STOP handling, archive
rejection, immutable-byte verification and the Qt protocol/import boundary.
Focused regressions also cover supervisor death, cancellation during launch,
alternate source snapshots, runtime helper capabilities, adoption recovery, and
firmware-job completion in the import wizard.
`tests.gui.preview` also exercises the real CLI and captures a synthetic title
screen. A missing systemd user manager skips only the scoped integration test.

The independent GPT-6-Astra review and its recheck are recorded in
[ASTRA-REVIEW.md](ASTRA-REVIEW.md).

Qt interface references:
[QProcess](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html),
[QObject lifetime](https://doc.qt.io/qt-6/qobject.html).
Controller diagnostics use the
[SDL2 GameController API](https://wiki.libsdl.org/SDL2/SDL_GameControllerRumble).
