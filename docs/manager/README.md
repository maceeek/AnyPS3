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

### Key setup

The **PUP key** decrypts the firmware update `PS3UPDAT.PUP`. The **APP key**
decrypts the game's `EBOOT.BIN` SELF executable. These are PS3 system keys,
the same for every user, not per-game. APP keys select the revision recorded in
the SELF header; Ratchet uses type **4**, revision **1**.
Obtain them from your own console or published sources such as RPCS3's
`rpcs3/Crypto/key_vault.{h,cpp}`. AnyPS3 includes no key bytes.

The wizard's **Firmware and keys** page explains both files. Choose a JSON file,
or open **JSON format / paste hex values…** to enter the fields directly.
Pasted values are sent to the manager over stdin, never command arguments;
the manager writes JSON into the library's `private-keys/` directory (0700),
with files restricted to the current user (0600). User-selected JSON files
remain referenced at their original path; keep them outside the repository,
restrict their permissions, and register them again if they change.

The exact required JSON shapes are below. Angle-bracket text denotes a placeholder,
not a key. Hex strings accept upper/lowercase digits, without `0x` or spaces.

PUP key file:

```json
{"erk":"<64 hex digits>","riv":"<32 hex digits>","pup_hmac":"<128 hex digits>"}
```

APP key file (revision must match the selected SELF):

```json
{"type":4,"revision":1,"erk":"<64 hex digits>","riv":"<32 hex digits>"}
```

Choose any external firmware PUP **before** entering or downloading keys.
The manager validates the PUP's structure and entry HMACs, then decrypts and checks
an encrypted `dev_flash` package using ERK/RIV. APP validation decrypts the SELF
metadata, checks padding, revision/type and section/key-table structure. These are
key setup checks, not ECDSA signature verification or full game import verification.
The selected ISO supplies its EBOOT and disc PUP; an external PUP overrides the disc PUP.
Without a target, only the JSON format and hex lengths are checked, and the result
explicitly says **Format only**. File selections are validated on **Finish**;
paste and download validate immediately and show the result on the key page.
Wrong keys give an actionable `invalid-key` error; no entry is saved for a failed check.

CLI equivalents (select the same library as your GUI):

```bash
anyps3-manager keys set pup /private/firmware-keys.json --pup /path/PS3UPDAT.PUP
anyps3-manager keys set app /private/app-key.json --iso /path/game.iso
# Or use --self /path/EBOOT.BIN instead of --iso.
# Pasted JSON may also be read privately from stdin with keys set app|pup --stdin.
```

**Download from RPCS3…** is optional and runs only after a confirmation naming the
files, source and your responsibility for complying with local law. It downloads
only the public `key_vault.h` and `key_vault.cpp` source files from
`https://raw.githubusercontent.com/RPCS3/rpcs3/ffba2bf09ef3980638f9327b25240d81414034ab/rpcs3/Crypto/`.
Both SHA-256 digests must match constants computed from that exact local reference
revision. Redirects and unpinned fallback URLs are refused. Keys are parsed locally;
downloaded source is not retained. The APP revision is read from the selected EBOOT;
without an EBOOT the default is Ratchet revision 1, with format-only validation.
Both keys must pass before either is saved. Network, hash, parser or validation
errors preserve existing keys. Private JSON records the pinned source URL; the key
registry also records validation scope. CLI invocation itself explicitly opts in:

```bash
anyps3-manager keys fetch-rpcs3 --iso /path/game.iso --pup /path/PS3UPDAT.PUP
```

`missing-key` explains the key's purpose, exact format, sources and next command;
`key-changed` asks you to register the file again. `key-target` means select a
complete readable ISO/PUP/SELF. `key-dependency` means install the `[self]` extra.
`key-download` means check HTTPS connectivity and retry; `key-source-hash` or
`key-source-format` means retry or report the source mismatch. Never bypass the
hash check or paste key values into bug reports or logs.

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

## Recovering imports and cleaning job history

An unfinished title shows **Resume import** and **Remove incomplete title**.
Resume repeats its recorded source/options and reuses verified extraction. For
older records, the manager recovers the source from import-job history when
possible; otherwise **Choose ISO to resume…** opens the wizard. Adoption is also
resumed from its recorded source, including interrupted publication journals.

A new import or adoption may replace an incomplete entry only when it has **no
userdata, no builds and no active title job/game**. Complete titles still require
explicit disc replacement; adoption refuses to overwrite them. If an incomplete
entry already has userdata, resume its original operation, or remove its entry
while preserving userdata and then import the ISO. Adoption never overwrites
retained userdata. Title removal preserves saves and backups by default.

```bash
anyps3-manager titles resume BCES00511 --events
anyps3-manager titles remove BCES00511
anyps3-manager jobs remove JOB_ID [OTHER_JOB_ID ...]
anyps3-manager jobs clear
```

Select several completed, failed or cancelled jobs and choose **Remove selected**,
or use **Clear finished**. Removal affects only job history and logs. An explicit
selection containing any active job is rejected without removing the others.
Active jobs offer **Cancel**, including a surviving recorded tool after supervisor
failure. Inherited lifecycle locks protect both startup and tool/game execution;
an unidentified surviving child keeps its job history and produces an actionable
`orphan-active` error rather than pretending cancellation succeeded.

After import/adoption, **Build** is the next step. Disabled Play explains that a
native build is required; active operations show progress in Jobs. Title controls
refresh after success, errors, cancellation and foreground-game exit. Switching
titles clears selected rows immediately so delayed replies cannot act on another
title. Changing the active build reloads setting availability.

Run records update observed flips, capture counts and FPS while running, and retain them after Stop,
including recovery after supervisor loss. `verified-boot` requires observed flips;
it does not establish an original menu, gameplay or physical-controller input.
Stopped runs obey the completed-run retention setting. Save exports preserve empty
directories and reject symlinks. ZIP publication uses an atomic hard link when
supported; FAT/exFAT-style filesystems use exclusive copying with cleanup on
failure (the destination is visible during that fallback copy).

The [GUIFIX report and screenshots](evidence/guifix/README.md) record the defects,
regressions, original-game acceptance and remaining visual-platform limits.

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

### Internal rendering resolution

In **Settings → Video → Internal rendering resolution**, select Native (1×),
2×, 3× or 4×. The value multiplies both dimensions: Ratchet's1280×720 becomes
2560×1440 at2×. The setting is saved globally or per title and takes effect on
the next launch. CLI: `run TITLE --set video.render_scale=2`.

A build containing render-scale support is required; older builds disable this
setting. Scaling preserves native guest readback and adds higher-resolution
visual rendering. It does not replace original textures. Some surface aliases
and unsupported texture conversions still reconstruct native detail, and GPU
cost rises with resolution. The verified2× Great Clock run averaged45.52FPS;
full-scene high-resolution detail and stable60FPS are not yet established.

### Game audio

**Game audio is on by default for Play.** In **Settings → Audio**, use **Enable
game audio** (`audio.enabled`) globally or for a title. A title can inherit the
global value; a one-off CLI override uses `run TITLE --set audio.enabled=false`.
Enabled Play launches select `ANYPS3_AUDIO_SINK=sdl` and
`ANYPS3_AUDIO_AUDIBLE=1`, allowing SDL to select its output driver. Disabled Play
launches force `SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null` without an audible
flag. The runtime's explicit audible-output guard remains in place.

Legacy settings files still load. The old `audio.audible_allowed` and `audio.sink`
are interpreted together using their old defaults (false and null), including
global values inherited by a title: only an unlocked SDL choice becomes enabled.
Thus a saved lock or null sink preserves silence; a file with no audio keys uses
the new default. Explicit `audio.enabled` wins within its layer. Migration occurs
on read without rewriting files; the next edit/reset of `audio.enabled` removes
that scope's legacy audio keys. Unrelated edits retain them. The GUI reports
migrated overrides so **Inherit**/**Reset** also works on legacy files. The retired
keys and `--i-understand` are no longer settings commands; doctor reports
`audio_enabled` instead of `audio_locked`.

Ambient `ANYPS3_*`, `SDL_*`, `VK_*` and `LD_PRELOAD` variables are removed before
composing a launch. Build/import tools, detached supervisors and other non-Play
processes always force dummy/null audio, regardless of the Play setting.
On the development PC, set `audio.enabled=false` before Play; inherited shell
dummy/null variables do not override the player's saved audio choice. Development
tests must compose enabled environments only, and force silence for every real
subprocess. Never launch the game or GUI with audio enabled on this PC.

Translation, compilation and game launches require systemd user scopes with
`MemoryMax` and `MemorySwapMax=0`. On this PC the defaults are the specification's
CPU sets. `--unscoped` is an explicit escape hatch for other machines and test
fixtures; it is not a fallback when systemd is missing.

`stop TITLE` sends SIGTERM, waits, then SIGKILL to the recorded process identity.
The GUI warns to save in-game first. Run records preserve stdout/stderr, runtime
STOP, errors, FPS intervals, exit status, memory observations and elapsed time.
Verified boot means observed flips, **not** a verified original menu or gameplay.

### Skip intro

**Settings → Input → Skip intro** (`input.skip_intro`, default off) appears only
for titles with an intro-skip recipe in `anyps3_manager/intro_skip.py`; currently
Ratchet & Clank: A Crack in Time (BCES00511). When on, the run supervisor presses
START through the game's own controller input path during the startup notices,
logos and title screen until the main menu is detected. It stops at the menu,
after 3 minutes, on a runtime STOP, or as soon as you press any controller button.
Nothing in the game, its files or saves is changed; the game decides what START
does. Detection uses the runtime's FPS/draw reports (forced on while active), not
the picture, so it is a heuristic. The run record stores the outcome under
`intro_skip` (reason `menu`, `timeout`, `player-input`, `guest-stop` or
`run-ended`). Requires a build whose runtime reads `ANYPS3_PAD_INJECT`; older
builds show the option as unavailable until **Rebuild**.

`ANYPS3_PAD_INJECT=FILE` is a generic runtime channel: the file holds one line
`v1 PRESENT DIGITAL1 DIGITAL2` (decimal `CELL_PAD_CTRL_*` bits). Bits are OR-ed
onto controller port 0 without replacing a physical pad; `PRESENT=1` keeps port 0
connected when no physical pad is attached. A malformed or missing file stops
the run explicitly.

## Runtime-dependent features

Runtime window/fullscreen settings and custom pad configuration remain unavailable.
PADMOTION implements game-side SDL rumble and SIXAXIS motion, plus the existing
PS bit through SDL GUIDE. `input.rumble` defaults on and can be disabled globally
or per title; it is available only for builds containing `ANYPS3_PAD_RUMBLE`.
The controller screen shows rumble and combined accelerometer/gyro capability.
Basic DualSense input and motion require SDL >= 2.0.14; confirmed rumble capability
requires SDL >= 2.0.18. Manager diagnostics require SDL >= 2.0.18 and report
missing required symbols before initializing the controller subsystem. Diagnostics
initialise no audio. Default SDL mapping remains in use. See
[DualSense behavior, scales and limits](DUALSENSE.md)
and [PADMOTION verification](evidence/padmotion/README.md). Existing game builds
need rebuilding by their owner before they include this runtime change.

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
Key setup tests generate synthetic encrypted headers and mock all downloads;
see [GUIKEYS verification and limits](evidence/guikeys.md).
`tests.gui.preview` also exercises the real CLI and captures a synthetic title
screen. A missing systemd user manager skips only the scoped integration test.

The independent GPT-6-Astra review and its recheck are recorded in
[ASTRA-REVIEW.md](ASTRA-REVIEW.md).

Qt interface references:
[QProcess](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html),
[QObject lifetime](https://doc.qt.io/qt-6/qobject.html).
Controller diagnostics use the
[SDL2 GameController API](https://wiki.libsdl.org/SDL2/SDL_GameControllerRumble).
