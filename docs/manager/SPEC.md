# AnyPS3 Manager: game library, CLI and Qt GUI (specification)

Date: 2026-10-20. Status: **brief for the implementing agent**, nothing here is implemented yet.
Facts about the current code are marked **verified** (checked in the source/artifacts on this date) or **to verify**.
Everything else is a requirement or a proposal.

## 0. Read first

1. `AGENTS.md` applies in full. The rules that matter most here:
   - never patch original game bytes;
   - unknown behaviour stops explicitly and must be surfaced to the user, not hidden;
   - every build goes into a new directory with a source snapshot and hashes;
   - real runs as evidence;
   - write-up in `docs/knowledge/CURRENT.md`, `docs/menu-progress.md` and a topic record.
2. **Machine rules on the development PC (absolute):**
   - **No audible sound, ever.** Every run uses `SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`. Never set `ANYPS3_AUDIO_AUDIBLE`. No paplay/aplay/speaker-test.
   - **Memory caps.** Every compile, test suite and game run goes in `systemd-run --user --scope -p MemoryMax=… -p MemorySwapMax=0`. An unscoped build once OOM-killed the session.
   - **CPU sets.** Compiles and tests run on `AllowedCPUs=6-11,18-23`; measured game runs use `0-5,12-17`.
   - **No forcing GPU clocks or power settings.**
   - **Kill by PID**, never with `pkill -f`.
3. Project goal context: the original Ratchet & Clank: A Crack in Time (BCES00511) reaches its menu and a level with the virtual pad. Best steady run so far is 54 FPS (build 603).
   - **Physical gamepad navigation is still unverified.** It is part of the project's completion criterion, and section 8 closes it.
   - **Only this title has a profile** (`profiles/ratchet-acit.json`). Any other disc is expected to stop explicitly somewhere. The manager must never claim compatibility it has not observed.
4. **Path ownership.** Performance work on the runtime (`anyps3/rsx_*`, `spu_*`, `par_*`) runs in parallel on other branches.
   - The manager and GUI live in **new top-level packages** and do not edit `anyps3/`. The one exception is the `--extra-flags` option in `anyps3/native_build.py` (section 5).
   - Runtime changes needed by this spec (sections 6.3, 8) are separate work packages on their own branch, with native tests and a game build.
   - Work in a git worktree (`local/ratchet/tools/make_gitworktree.sh`), not on `main`.
   - `local/ratchet/` is evidence from earlier runs: read it, copy from it, **never move or modify it**.

## 1. Goals and non-goals

Goals:

1. **Import** a PS3 disc ISO. Import means extract, decrypt, translate and compile it into a playable native build, as a library entry with its own **persistent storage** (HDD data, save data, trophies, user name).
2. **Rebuild** a title (new runtime, new profile, new compiler flags) **without losing or touching its persistent data**. Activation and rollback between builds.
3. **Launch** with settings: video, audio sink, performance switches, memory/CPU limits.
4. **Gamepads:** finish support for XInput-class controllers (Xbox 360/One/Series and compatible) and the PS5 DualSense, with a settings page for port assignment, mapping, dead zones, rumble.
5. **The GUI is a thin wrapper.** Everything works from a CLI with machine-readable output. The GUI has no hard dependency on runtime internals.

Non-goals for v1:
- Windows/macOS. The runtime is Linux-only: futex, mmap, prctl, systemd.
- PSN/PKG titles (need RAP/klicensee), game updates, DLC.
- Network play.
- Importing saves made on a real console (PARAM.PFD/secure files are not supported by the runtime).
- Bundling keys, firmware or game data.
- Per-game patches or hacks.
- Automatic profile generation for unknown titles. That is long-term runtime work; the manager only reports the stop.

## 2. Architecture

```
┌────────────────────┐   spawns, reads JSON / JSON Lines   ┌──────────────────────────┐   subprocess   ┌──────────────┐
│ anyps3_gui (Qt 6)  │ ──────────────────────────────────▶ │ anyps3_manager (CLI)     │ ─────────────▶ │ anyps3 (the  │
│ no `import anyps3` │                                     │ stdlib only, owns state  │                │ translator + │
└────────────────────┘                                     │ on disk, jobs, settings) │                │ runtime gen) │
                                                           └──────────────────────────┘                └──────────────┘
```

- **`anyps3_manager/`** (new top-level package, entry point `anyps3-manager`) owns the library on disk, jobs, settings and launching.
  - Python standard library only. No Qt, no SDL import at module level.
  - It drives the existing tools as subprocesses (section 4) and never imports runtime generator internals for its core logic.
  - Small pure helpers (SFO parsing, ISO inventory) may be imported from `anyps3` if that is simpler than shelling out.
- **Why not under `anyps3/`?** Build records hash the `anyps3/` sources (`build.json` `source_sha256`). Manager edits must not make every game build look stale.
- **`anyps3_gui/`** (new top-level package, entry point `anyps3-gui`) is Qt 6.
  - It talks to the manager **only** through `QProcess` and the CLI contract below.
  - A test enforces that no GUI module imports `anyps3` or `anyps3_manager`.
  - Recommendation: **PySide6**, installed in a venv. It is not installed on this PC; the system has `qt6-base 6.12.0`. A C++/Qt 6 GUI would satisfy the same contract.
- **The disk is the source of truth.** The GUI never writes library files. Any other frontend (a TUI, a script, a future in-game overlay) can use the same CLI.

### 2.1 CLI contract

- Every command accepts `--json`. The result is one JSON object on stdout with `"schema": "anyps3.manager/1"`. Human-readable logs go to stderr.
- Long-running commands (`import`, `build`, `run`) with `--events` emit **JSON Lines** on stdout:
  - `{"event":"stage","stage":"extract","index":2,"count":7}`
  - `{"event":"progress","stage":"compile","done":131,"total":214,"unit":"objects"}`
  - `{"event":"log","level":"info","text":"…"}`
  - `{"event":"result",…}` or `{"event":"error","code":"…","message":"…","detail":{…}}`

  Unknown fields must be ignored by consumers. Adding fields is compatible; renaming or removing them needs a schema bump.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 1 | error |
  | 2 | usage |
  | 3 | guest STOP (run ended on an explicit runtime stop) |
  | 4 | lock busy |
  | 5 | cancelled |
  | 6 | missing input (key, firmware, ISO) |
- **Jobs.** `import` and `build` run as **detached jobs** that survive GUI exit. Each job has a directory with `job.json` (state, pid, stage, timestamps), `events.jsonl` and `log.txt`.
  - `jobs list`, `jobs show ID`, `jobs follow ID` (replays `events.jsonl`, then tails), `jobs cancel ID` (kills the job's scope by PID).
  - The GUI reattaches after a restart with `jobs follow`.
- **Locks** (`flock` files in `<library>/locks/`):
  - one library-metadata write lock;
  - one compile lock per machine, at most one game compile at a time (same idea as `local/ratchet/.compile.lock`);
  - one run lock per title, at most one running instance of a title.

  Command overview (names are a proposal; keep them stable once released):

```
anyps3-manager init [--library DIR]
anyps3-manager doctor                         # checks: Vulkan ICD, SDL2, compiler, systemd --user scopes, keys, disk space
anyps3-manager keys set app|pup PATH          # stores a reference + sha256; never prints key bytes
anyps3-manager firmware list | add PUP | add-from-disc TITLE
anyps3-manager import ISO [--no-build] [--replace-disc TITLE]
anyps3-manager titles [list|show TITLE|remove TITLE [--delete-userdata]]
anyps3-manager build TITLE [--reason TEXT] [--no-activate]
anyps3-manager builds TITLE | activate TITLE BUILD | pin/unpin TITLE BUILD | prune TITLE --keep N
anyps3-manager run TITLE [--build BUILD] [--set KEY=VALUE ...]   # foreground; --detach for GUI
anyps3-manager stop TITLE
anyps3-manager runs TITLE | runs show TITLE RUN
anyps3-manager saves list TITLE | backup TITLE | restore TITLE BACKUP | export TITLE SAVE OUT | import TITLE ZIP
anyps3-manager settings schema | get [--title T] | set [--title T] KEY VALUE | reset [--title T] KEY
anyps3-manager pads list | monitor | rumble-test PAD
anyps3-manager adopt-ratchet LOCAL_RATCHET_DIR  # one-off migration of the existing setup, copy-only (section 11)
```

## 3. Library layout

Default root: `$XDG_DATA_HOME/anyps3` (`~/.local/share/anyps3`), configurable. It must be outside the repository.

```
<library>/
  library.json                      # schema version, titles index
  settings.json                     # global settings (section 9)
  locks/  jobs/<id>/  object-cache/ # object-cache: shared native_build object cache (exact content keys)
  keys.json                         # paths + sha256 of user-supplied key files; file mode 0600
  firmware/<version>-<sha8>/        # dev_flash/... + extraction.json, shared by titles
  titles/<TITLE_ID>/
    title.json                      # identity (section 4), active build, pinned builds, play time
    settings.json                   # per-title overrides
    disc/                           # extracted ISO, read-only after import (+ anyps3-extraction.json)
    eboot/                          # EBOOT.analysis.elf + unself report
    builds/<NNNN>-<UTC stamp>/       # IMMUTABLE once finished (section 5)
    userdata/                       # PERSISTENT: never written by import-rebuild/build/activate/prune
      hdd0/game/<TITLE_ID>/...      # data the game installs itself on first boot
      hdd0/home/00000001/savedata/  # save data
      hdd0/home/00000001/trophy/
      hdd0/home/00000001/localusername
    cache/                          # ANYPS3_CACHE_ROOT; disposable, may be cleared before a launch
    backups/<UTC stamp>-<reason>.tar.zst
    runs/<UTC stamp>/               # stdout.log, stderr.log, run.json; rotated (keep N)
```

**Verified layout facts** the manager must reproduce:
- The runtime reads `ANYPS3_DISC_ROOT` (extracted disc; the current runs use `ANYPS3_DISC_CASEFOLD=1`), `ANYPS3_HDD_ROOT` and `ANYPS3_CACHE_ROOT` (`anyps3/program.py:324–326`).
- Save data lives under `<hdd>/home/%08u/savedata` (`native_savedata.py:217`).
- `<hdd>/home/00000001/localusername` must exist, be non-empty and be at most 63 bytes. Otherwise the run stops (`sysutil_transport.py:611–618`). The manager seeds it from the setting `user.name`.
- The existing Ratchet HDD (`local/ratchet/hdd0`) contains `game/BCES00511/{PARAM.SFO,ICON0.PNG,PIC1.PNG,USRDIR/...}`, `home/00000001/{savedata/BCES00511_SAVE_1,trophy/NPWR00941_00,localusername}`.

**Invariant (tested):** the content hash of `userdata/` is identical before and after `build`, `activate`, `prune` and `import --replace-disc`. Only a running game and `saves restore`/`saves import` write it. `titles remove` keeps `userdata/` unless `--delete-userdata` is given. Even then it writes a final backup first unless `--no-backup` is also given.

## 4. Import pipeline

`import ISO` is a job with these stages. Each stage writes its outputs to a staging directory, and the title becomes `ready`/`imported` only after all its stages succeed. A failed import leaves an `incomplete` title that `import` can resume or `titles remove` can delete.

1. **Identify.** Compute the ISO's SHA-256 and run the ISO9660 inventory (existing: `anyps3 disc-inventory`).
   - Read `PS3_GAME/PARAM.SFO` (existing: `anyps3 sfo`) for title id, title, version and category. v1 accepts disc games only.
   - Refuse an image that is not complete by size. The existing extractor reports `image_complete_by_size`. The GUI never offers `--allow-partial`.
2. **Extract** to `disc/` (existing: `anyps3 disc-extract ISO -o DIR`). Then make it read-only. Disc size plus a free-space check up front.
3. **Firmware.** The runtime needs an extracted `dev_flash/sys/external` (`build_game --firmware`). It is shared per firmware version, and the project uses 3.01. Sources, in order:
   1. firmware already in the library;
   2. the disc's own `PS3_UPDATE/PS3UPDAT.PUP`.
      - **Verified:** the Ratchet disc's PUP has SHA-256 `508129a1…cecdf`, identical to the 3.01 PUP behind `local/ratchet/firmware-verified/extraction.json`.
   3. a PUP file chosen by the user.

   Extraction: `python -m anyps3.firmware PUP --key-file PUPKEY -o DIR`. It needs a user-supplied PUP key file; the existing one is `local/ratchet/firmware-keys.json`.
4. **Decrypt EBOOT:** `anyps3 unself disc/PS3_GAME/USRDIR/EBOOT.BIN --key-file APPKEY -o eboot/EBOOT.analysis.elf`. The key file is user-supplied (existing: `local/ratchet/app-key.json`, fields `type, revision, erk, riv, source`). Record the SELF and ELF hashes.
5. **Profile.** Look up `profiles/*.json` by `executable_sha256` (existing check: `anyps3/profile.py` `check_profile`). There are two outcomes:
   - **match:** the title is a known target. It uses that profile and shows the profile's `status` text.
   - **no match:** the title is imported as **untested**.

     A build may still be attempted with a minimal profile that has `executable_sha256: null`. If the generator rejects a minimal profile, the build is refused with that message.

     The GUI must say plainly that an explicit stop is expected. The manager never writes guessed seeds or overlays into a profile.

     **To verify:** whether `build_game` accepts a minimal profile; it may need the generic profile fields to be empty lists.
6. **Seed userdata:** create `userdata/hdd0/home/00000001/{savedata,trophy}` and `localusername`. Nothing else: the game creates its own HDD data at first boot.
7. **Build** (section 5) unless `--no-build`.

**Re-import.**
- The same ISO hash for an existing title is a no-op that reports "already imported".
- A different ISO for the same title id is refused unless `--replace-disc TITLE`. That replaces `disc/` and `eboot/`, keeps `userdata/` and marks the active build stale.

## 5. Build, rebuild, activation

`build TITLE` creates `builds/<NNNN>-<stamp>/` and never reuses a directory.

1. **Source snapshot.** Copy the `anyps3/*.py` tree used for the build into `builds/<id>/source/`, plus the profile as `profile.json`, and record their SHA-256. Run the generator from that snapshot (`env PYTHONPATH=<snapshot> python -P -m anyps3.build_game …`), never from the live tree. This is the same pattern as `local/ratchet/tools/snapshot_build.sh`.
2. **Translate:**

   ```
   python -P -m anyps3.build_game <eboot> --firmware <fw>/dev_flash/sys/external --key-file <appkey> --profile <snapshot profile> --max-steps 1000000000000 -o builds/<id>/bundle
   ```

   Run it in a scope with the build memory setting (current scripts: 12 GiB).
3. **Compile:**

   ```
   python -P -m anyps3.native_build --vulkan builds/<id>/bundle -o builds/<id>/native -j <jobs>
   ```

   - Environment: `ANYPS3_OBJECT_CACHE=<library>/object-cache` (honoured by `native_build.py:69–75`).
   - Run it in a scope with the compile memory setting (current scripts: 22–28 GiB at `-j 12`) and the compile CPU set.
   - **Do not** use the diagnostic `virtual_pad.py build` wrapper for player builds. It is a test harness.
   - **Gap (verified):** every measured build (472, 603, 642) was compiled with `-march=native -ffp-contract=off`. That came from `ANYPS3_EXTRA_CXXFLAGS`, which only the git-ignored diagnostic `local/ratchet/tools/virtual_pad.py:200–204` reads. `python -m anyps3.native_build --vulkan` links the same RSX Vulkan libraries but has no extra-flags option.
   - WP1 therefore adds a small `--extra-flags` option to `anyps3/native_build.py`, recorded in `compile.json` `compiler_flags` like the wrapper does. A player build without it would not match the measured performance.
   - Progress comes from the compiler unit count.
   - **To verify:** that `native_build` exposes per-unit completion; otherwise count the objects that appear.
4. **Verify the build** without running it:
   - every non-empty original PT_LOAD segment equals `program.elf` (the check `local/ratchet/run_native.py` does today);
   - the snapshot source hashes equal `build.json` `source_sha256`.

   The status becomes `built`. A build becomes `verified-boot` only after a launch from it reaches its first flip, and `verified-play` only after the user (or an acceptance test) confirms play.
5. **Record** `build.json`, `compile.json`, the manager's `record.json` and `logs/`. `record.json` holds:
   - reason;
   - git revision and dirty flag if available;
   - snapshot hash and profile hash;
   - firmware id;
   - compiler version and flags;
   - **host CPU model and flags** (builds use `-march=native` and are not portable);
   - duration and peak memory;
   - result.
6. **Activate** on success unless `--no-activate`. Activation is an atomic `title.json` update. The previous active build stays available for `activate` (rollback). `prune --keep N` never deletes the active build or a pinned one.

**Staleness.** `titles show` and the GUI report "rebuild recommended" with the reason when:
- the current `anyps3/` source hash differs from the active build's;
- the profile hash changed;
- the host CPU changed;
- the firmware changed.

**Costs (verified for Ratchet, current scripts):**
- A full compile takes about 25–30 min at `-j 12`. Runtime-only edits that hit the object cache are much faster.
- Build 603 uses 1.4 GiB in `native-virtual-603` plus 0.95 GiB in `native-bundle-603`.
- Setting `build.keep_intermediates` (default false) deletes generated C++ and objects after a verified build.
- **To verify:** which files `program` needs at run time besides `program.elf`.
- `doctor` and the import wizard show disk estimates.

## 6. Launch

### 6.1 Command

`run TITLE` takes the run lock, optionally backs up saves (setting `saves.backup_before_run`, keep N), clears `cache/` and composes the environment from settings. It then starts:

```
systemd-run --user --scope --unit=anyps3-<title>-<stamp> -p MemoryMax=<run.memory_max> -p MemorySwapMax=0 \
  -p AllowedCPUs=<run.cpus> env <composed env> <build>/native/program <build>/native/program.elf
```

- stdout and stderr go to `runs/<stamp>/`.
- The manager records the scope unit and the `program` PID found in the scope's `cgroup.procs`. This is how `measure_level.sh` finds it today.
- If systemd user scopes are unavailable, `run` refuses with code 6. An explicit `--unscoped` flag exists for other machines and is never the default.

### 6.2 Composed environment (verified switch names)

| Purpose | Variables |
|---|---|
| Storage | `ANYPS3_DISC_ROOT=<title>/disc`, `ANYPS3_DISC_CASEFOLD=1`, `ANYPS3_HDD_ROOT=<title>/userdata/hdd0`, `ANYPS3_CACHE_ROOT=<title>/cache` |
| Presentation | `ANYPS3_PRESENT=sdl`, `ANYPS3_PRESENT_MODE=fifo\|mailbox\|immediate` (optional), `ANYPS3_VK_DEVICE=gpu`, `ANYPS3_RSX_BACKEND=gpu`, `VK_INSTANCE_LAYERS=` (empty for play; validation only in a diagnostic setting) |
| Audio | `SDL_AUDIODRIVER=dummy`, `ANYPS3_AUDIO_SINK=null` unless the audible lock (section 9) is open |
| Threads (current measured defaults) | `ANYPS3_RSX_WORKER=1`, `ANYPS3_SPU_THREADS=1` |
| Input | `SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1`, plus section 8 |
| Status | `ANYPS3_PERF_REPORT=1` (FPS lines `PERF interval … flips_per_s=…` in stderr, parsed for the run summary) |
| Diagnostic caps | `ANYPS3_SPU_MAX_STEPS` and `ANYPS3_SLEEP_BUDGET_US` (section 6.3) |

### 6.3 Runtime follow-ups needed for a player launch (runtime work package, not the GUI's)

- **Diagnostic caps stop normal play.**
  - `ANYPS3_SPU_MAX_STEPS` defaults to 10 million instructions per SPU thread.
  - `ANYPS3_SLEEP_BUDGET_US` defaults to 5 s of total guest sleep (`services.py:380`).
  - The measurement scripts override them with `1000000000000` and `300000000000`. The manager does the same and records it.

  The proper fix is an explicit "unlimited" value, or a play mode in the runtime, with a test.
- **Window.** The SDL window is created hidden at 64×64 and sized by the runtime (`rsx_vk_present.py:42`). Closing it ends the run with an explicit error. No fullscreen or window-size option exists yet. A follow-up adds `ANYPS3_WINDOW_MODE=windowed|fullscreen|borderless` and an initial size.
- **Shutdown.** Current runs end by SIGKILL (verification records return code −9). `stop` sends SIGTERM, waits (setting, default 5 s), then sends SIGKILL to the recorded PID. The GUI warns that stopping during a game save can corrupt it, and recommends saving in-game first. A graceful runtime shutdown is a follow-up.

### 6.4 Run record

`runs/<stamp>/run.json` holds:
- build id, start and end time, exit code;
- the **STOP line** if present: `STOP pc=… lr=… steps=…: reason`, parsed the same way as `run_native.py`;
- the last runtime error line;
- FPS summary: mean and median of the `PERF interval` rates;
- play time.

The GUI shows a guest STOP prominently with its reason and log excerpt. It is not a crash dialog to dismiss: it is the runtime telling us what is unsupported.

## 7. Save data

- **`saves list TITLE`:** one entry per directory in `savedata/`, with `PARAM.SFO` title, subtitle and detail (existing SFO reader), `ICON0.PNG`, size and modification time.
- **`saves backup`:** a tar archive of `userdata/hdd0/home/00000001/{savedata,trophy}` plus `hdd0/game/<TITLE_ID>` if setting `saves.backup_game_data` is on (it can be large).
  - Taken automatically before `run` (setting), before `activate` to a build with a different source hash, and before `restore`.
  - Keep the last N automatic backups. Manual backups are never pruned.
- **`saves restore` / `import`** only while the title is not running, behind the run lock.
- **`saves export`** writes a portable zip of one save directory. **`saves import`** accepts only zips produced by `export` in v1.

## 8. Gamepads

### 8.1 Current state (verified, `anyps3/native_pad.py`)

- **Backend.** SDL2 GameController via `dlopen("libSDL2-2.0.so.0")`. On this PC that is `sdl2-compat 2.32.74` over `SDL3 3.4.18`.
- **Ports and mapping.** Up to 7 ports, assigned in SDL enumeration order with hotplug polling. The button and axis mapping is fixed.
- **Advertised capabilities.** `PS3_CONFORMITY | HP_ANALOG_STICK` only. The game ignores ports without the conformity bit (`native_pad.py:118–122`).
- **Pressure.** Pressure-sensitive values are 0/255 from the digital state; L2/R2 come from the triggers.
- **Rumble.** `cellPadSetActDirect` returns `UNSUPPORTED_GAMEPAD`. No actuator is advertised.
- **Motion.** Requesting sensor mode throws "native controller motion input is not implemented", an explicit stop.
- **Evidence and diagnostics.**
  - Menu and level evidence so far uses the diagnostic SDL virtual pad (`local/ratchet/tools/virtual_pad.py`) or a uinput pad (`uinput_pad.py`).
  - **No physical controller has been verified in the game.**
  - `ANYPS3_PAD_TRACE=1` logs every reported pad state.

### 8.2 Requirements

- **P1: device classes.** XInput-class controllers (Xbox 360/One/Series via xpad/xone/xpadneo, Steam Input virtual pads) and DualSense over USB and Bluetooth (SDL HIDAPI driver; pass `SDL_JOYSTICK_HIDAPI_PS5=1`).
  - "XInput" means the device class. Linux has no XInput API.
  - SDL's GameController layer covers both classes, so keep SDL as the backend.
- **P2: port assignment.** A configurable device→port table keyed by SDL GUID plus serial where available, with "first connected" as the default. It persists across sessions and survives hotplug and reconnects.
- **P3: mapping.** A table from SDL buttons and axes to PS3 inputs: cross/circle/square/triangle, L1/R1/L2/R2/L3/R3, select/start, D-pad, both sticks.
  - Positional defaults: A→Cross, B→Circle, X→Square, Y→Triangle, Back/Create→Select, Start/Options→Start. The PS/Guide button is reserved for a future manager overlay and is not mapped.
  - Per port: stick dead zone (inner/outer), axis inversion, and the trigger threshold that sets the digital L2/R2 bits.
- **P4: rumble.** Implement `cellPadSetActDirect` through `SDL_GameControllerRumble`. PS3 motor 0 is the small on/off motor; motor 1 is the large motor, 0–255.
  - Advertise the actuator capability **only** for a port whose device reports rumble support and whose rumble setting is on.
  - Confirm the bit layout of the capability and actuator fields from the SDK/RPCS3 references before implementing.
- **P5: motion (DualSense, lower priority).** SDL sensor API (accelerometer and gyro) mapped to the cellPad sensor fields when a game enables sensor mode.
  - Exact value scaling must come from the documentation, with a recorded source.
  - Until implemented, the current explicit stop stays. It is not replaced by zeros.
  - Ratchet ACIT has not been seen requesting it.
- **P6: DualSense extras.** Lightbar colour and player LEDs per port as a cosmetic setting. Adaptive triggers are out of scope.
- **P7: configuration transport.** The manager writes `pads.json` for the run and passes `ANYPS3_PAD_CONFIG=<path>`.
  - The runtime validates it strictly and stops with a clear message on an invalid file.
  - No config means today's behaviour.
  - The user's own `SDL_GAMECONTROLLERCONFIG` mappings can be passed through as a setting.
- **P8: manager tools.**
  - `pads list` reports connected controllers: name, GUID, type, rumble and sensor support, battery.
  - `pads monitor` streams live input as JSON Lines.
  - `pads rumble-test` exists.

  These live in the manager (SDL through `ctypes`, loaded lazily) so the GUI does not link SDL. `pads rumble-test` is silent hardware feedback and allowed. Nothing may produce audio, including DualSense speaker or haptics audio; keep the SDL audio subsystem uninitialised.
- **P9: tests.**
  - Native regression tests with independent expectations for mapping, dead zones, port assignment, actuator advertisement, and rumble values sent to an SDL virtual controller. Extend `tests/test_native_pad.py`; SDL virtual joysticks are already used by `virtual_pad.py self-test`.
  - The runtime part (P2–P7) changes `anyps3/native_pad.py`. It is a separate work package on its own branch, with a fresh game build and a real run per `AGENTS.md`.

### 8.3 Acceptance with physical controllers (closes the project's open gamepad criterion)

With a real **Xbox-class controller** and, separately, a **DualSense** (USB and Bluetooth):
1. Launch Ratchet through the manager.
2. Navigate the original main menu.
3. Load the existing save.
4. Move in the level.

Evidence:
- `ANYPS3_PAD_TRACE=1` logs showing the reported states;
- captured frames before and after each navigation step;
- the run record.

State separately what was verified with the virtual pad, with each physical controller, and over each connection type. Write it up in CURRENT, menu-progress and a topic record.

## 9. Settings

Settings are **layered**: built-in defaults, then global `settings.json`, then per-title `settings.json`, then one-off `run --set`.

`settings schema --json` publishes every key with:
- type, range or choices, default;
- description;
- scope: `launch` or `build` (a change marks the build stale);
- group;
- flags: `advanced`, `diagnostic`, `experimental`;
- for runtime switches, the environment variable it maps to.

**The GUI renders settings generically from this schema** and has no hard-coded knowledge of keys. New runtime switches then need only a manager change.

Runtime switches change between builds. At build time the manager records which `ANYPS3_*` variables the snapshot's sources read; a scan of `getenv("ANYPS3_…")` strings is enough. The schema marks a switch **unavailable for the active build** instead of passing a variable the build ignores.

Initial groups and keys (defaults in brackets are this PC's current practice, for the reader's orientation):

| Group | Keys |
|---|---|
| **General** | library path; `user.name` (written to `localusername`) [`User`]; key file paths; firmware |
| **Build** | jobs [12]; translate memory max [12G]; compile memory max [24G]; compile CPU set [all; this PC 6-11,18-23]; keep intermediates [off]; object cache size limit |
| **Performance (launch)** | game memory max [24G]; swap max [0]; game CPU set [all; this PC 0-5,12-17]; RSX worker [on]; SPU threads [on]; `ANYPS3_RSX_THREADS`; `ANYPS3_RSX_MEMORY_CACHE_MB`; timer slack (`ANYPS3_TIMER_SLACK_NS`); present mode; FPS overlay or log [on] |
| **Experimental** | switches from current performance branches when present in the build: `ANYPS3_RSX_HOST_DRAIN`, `ANYPS3_RSX_FLIP_EARLY`, `ANYPS3_RSX_RECORDER` (asynchronous recorder: **off**, known teal-frame issue) |
| **Video** | window mode and size (after the 6.3 runtime follow-up) |
| **Audio** | sink [`null`]; **audible lock** [locked] |
| **Input** | section 8 |
| **Saves** | backup before run [on]; keep N [10]; include game data [off] |
| **Diagnostics** (hidden by default) | Vulkan validation, frame capture stride (`ANYPS3_FRAME_DIR`, `ANYPS3_FRAME_STRIDE`), `ANYPS3_PAD_TRACE`, SPU/RSX trace switches |

**Audible lock.** Global setting `audio.audible_allowed`, default **false**. It can only be changed with `settings set --global audio.audible_allowed true --i-understand` on the CLI and is not editable in the GUI. While it is false the manager always forces `SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null`. On the development PC it stays false.

The **memory settings** are the systemd `MemoryMax` values for translation, compile and game runs. The GUI shows host RAM next to them and warns when compile max plus game max exceeds it. An OOM-killed run is reported as such: detect it from the scope's result and the kernel `oom_kill` count of the cgroup.

## 10. GUI (Qt 6)

Screens:

1. **Library:** a grid or list with ICON0, title, title id, version and status.
   - Status values: incomplete, building, built, verified-boot, verified-play, stale, failed.
   - Also: the profile's compatibility note, last played, play time. PIC1 is the background on selection.
2. **Import wizard:**
   1. choose ISO;
   2. identity and free-space check;
   3. firmware source (library, disc's PS3_UPDATE, file);
   4. keys (only when missing);
   5. confirm;
   6. progress per stage with the job log;
   7. build progress.

   Closing the GUI does not cancel the job.
3. **Title page:**
   - Play/Stop;
   - active build with status and staleness reason, a "Rebuild" button with a reason field;
   - **Builds** tab: history, activate/rollback, pin, prune, logs;
   - **Saves** tab: list, backup now, restore, export/import;
   - **Runs** tab: history with FPS summary, STOP reason and log viewer;
   - **Settings** tab: per-title overrides.
4. **Jobs panel:** running and finished jobs, progress, cancel, open log.
5. **Settings:** generated from the schema, with tabs per group. The Diagnostics and Experimental groups sit behind an "advanced" toggle.
6. **Controllers:** connected devices (`pads list`), live input view (`pads monitor`), port assignment by drag and drop, mapping editor, dead-zone sliders with live preview, rumble test.

Behaviour:
- Never block the UI thread. Every CLI call is a `QProcess`.
- Show CLI error messages verbatim, with the error code.
- Tolerate unknown JSON fields.
- Remember window geometry with `QSettings`. That is the only GUI-owned state.

Later (not v1): controller navigation of the GUI itself (big-picture mode).

## 11. Migration of the existing setup

`adopt-ratchet local/ratchet` creates the BCES00511 title from what exists, **copy-only**:
- `game-complete/` becomes `disc/`;
- `EBOOT.analysis.elf` becomes `eboot/`;
- `firmware-verified/` becomes `firmware/3.01-508129a1/`;
- `hdd0/` becomes `userdata/hdd0/`;
- key files are referenced by path.

It then verifies the hashes against the recorded manifests (`anyps3-extraction.json`, `extraction.json`, profile `executable_sha256`). After that, a normal `build` produces the first manager build. Existing `native-virtual-*` directories are not registered: they are diagnostic builds with the virtual pad wrapper.

## 12. Tests and acceptance

- **Manager unit tests**, without compiling a game:
  - a synthetic ISO9660 image;
  - fake translator and compiler commands via a `--toolchain` override that points at test stubs;
  - lock contention;
  - a job resumed after the GUI exits.

  Run them in a memory-capped scope like every suite.
- **Userdata invariant test:** hash the `userdata/` tree before and after build, activate, prune and `import --replace-disc`; the hashes must be identical.
- **Settings tests:**
  - layering;
  - schema validation and rejection of out-of-range values;
  - the audible lock: **no code path may produce an audible configuration while it is locked**. Test every command that launches anything.
- **GUI tests:**
  - an import scan proving no `anyps3`/`anyps3_manager` imports;
  - an offscreen (`QT_QPA_PLATFORM=offscreen`) smoke test against a stub CLI that emits recorded JSON Lines.
- **Pad runtime tests:** section 8.2 P9.
- **End-to-end acceptance on this PC** (real game, silent, scoped, done by an executing agent with the user's consent to occupy the machine):
  1. `adopt-ratchet`, or import of `local/ratchet/disc.complete.iso` into a fresh library with copied keys;
  2. build, then launch;
  3. reach the original menu and load the save;
  4. **rebuild** into a new build;
  5. relaunch: the same save must still load, and the `userdata/` hash must be unchanged by the rebuild;
  6. physical controller acceptance (section 8.3).

## 13. Work packages and order

| WP | Content | Edits | Depends on |
|---|---|---|---|
| 1 | Manager core: library, locks, jobs, import, build/activate/prune, run/stop, run records, saves, `adopt-ratchet`, `doctor` | `anyps3_manager/`, `tests/manager/`, `pyproject.toml`, `anyps3/native_build.py` (`--extra-flags` only) | — |
| 2 | Settings schema, env composition, switch availability per build, audible lock | `anyps3_manager/` | WP1 |
| 3 | Qt GUI | `anyps3_gui/`, `tests/gui/` | WP1–2 CLI contract (can start on stubs) |
| 4 | Runtime player mode: caps "unlimited", window mode, graceful shutdown | `anyps3/` (runtime, own branch) | coordinate with performance branches |
| 5 | Pad runtime P2–P7 plus native tests; `pads` manager commands | `anyps3/native_pad.py`, `anyps3_manager/` | WP1; physical controllers on the PC |
| 6 | End-to-end and physical-controller acceptance, write-up | docs | WP1–5 |

## 14. Open decisions (defaults to use unless the user decides otherwise)

| Decision | Default |
|---|---|
| GUI toolkit | PySide6 (Qt 6, LGPL) |
| Library location | `~/.local/share/anyps3` |
| Keep the ISO after extraction | no; store its hash and path only |
| Exit and save safety | SIGTERM → wait → SIGKILL until the runtime has graceful shutdown |
| Controller-navigable GUI | not in v1 |
