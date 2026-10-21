"""Published settings schema and the single environment composition boundary."""

import json
import os
import re

from .storage import ManagerError, atomic_json, read_json


def field(default, description, group, *, scope="launch", env=None, **extra):
    kind = "boolean" if type(default) is bool else "integer" if type(default) is int else "string"
    return dict(type=kind, default=default, description=description, group=group,
                scope=scope, env=env, advanced=False, diagnostic=False,
                experimental=False, **extra)


FIELDS = {
    "user.name": field("User", "Name seeded for newly imported titles (existing saves are unchanged).", "General", format="username"),
    "firmware.id": field("", "Firmware library identifier; empty selects the imported title's firmware.", "General", scope="build"),
    "build.jobs": field(12, "Parallel compiler jobs.", "Build", scope="build", minimum=1, maximum=128),
    "build.translate_memory_max": field("12G", "Translation memory limit.", "Build", scope="build", format="memory"),
    "build.compile_memory_max": field("24G", "Compilation memory limit.", "Build", scope="build", format="memory", concurrent_memory=True),
    "build.cpus": field("6-11,18-23" if (os.cpu_count() or 1) >= 24 else "", "Compiler CPU set; empty allows all CPUs.", "Build", scope="build", format="cpus"),
    "build.extra_flags": field("-march=native -ffp-contract=off", "Additional C++ compiler flags.", "Build", scope="build"),
    "build.keep_intermediates": field(False, "Keep generated C++ and object files.", "Build", scope="build"),
    "build.object_cache_gib": field(40, "Maximum shared object-cache size in GiB.", "Build", scope="build", minimum=0, maximum=4096),
    "run.memory_max": field("24G", "Game memory limit.", "Performance", format="memory", concurrent_memory=True),
    "run.cpus": field("0-5,12-17" if (os.cpu_count() or 1) >= 24 else "", "Game CPU set; empty allows all CPUs.", "Performance", format="cpus"),
    "run.stop_timeout": field(5, "Seconds between SIGTERM and SIGKILL.", "General", minimum=1, maximum=60),
    "run.keep": field(20, "Number of completed run records to retain.", "General", minimum=1, maximum=10000),
    "performance.rsx_worker": field(True, "Run RSX on a worker.", "Performance", env="ANYPS3_RSX_WORKER"),
    "performance.spu_threads": field(True, "Parallel SPU workers.", "Performance", env="ANYPS3_SPU_THREADS"),
    "performance.rsx_threads": field(0, "RSX CPU threads (0 uses runtime default).", "Performance", env="ANYPS3_RSX_THREADS", minimum=0, maximum=128),
    "performance.rsx_memory_cache_mb": field(256, "RSX memory cache in MiB.", "Performance", env="ANYPS3_RSX_MEMORY_CACHE_MB", minimum=0, maximum=16384),
    "performance.timer_slack_ns": field(50000, "Host timer slack in nanoseconds.", "Performance", env="ANYPS3_TIMER_SLACK_NS", minimum=1, maximum=1000000000),
    "video.present_mode": field("fifo", "Presentation mode.", "Video", env="ANYPS3_PRESENT_MODE", choices=["fifo", "mailbox", "immediate"]),
    "video.window_mode": field("windowed", "Window mode (requires player-mode runtime).", "Video", env="ANYPS3_WINDOW_MODE", choices=["windowed", "fullscreen", "borderless"]),
    "video.width": field(1280, "Initial window width.", "Video", env="ANYPS3_WINDOW_WIDTH", minimum=320, maximum=7680),
    "video.height": field(720, "Initial window height.", "Video", env="ANYPS3_WINDOW_HEIGHT", minimum=240, maximum=4320),
    "audio.enabled": field(True, "Play game audio through SDL. Turn off for silent playback.", "Audio", label="Enable game audio"),
    "input.sdl_mappings": field("", "SDL GameController mapping database entries.", "Input", env="SDL_GAMECONTROLLERCONFIG"),
    "input.ps5_hidapi": field(True, "Enable SDL DualSense HIDAPI support.", "Input", env="SDL_JOYSTICK_HIDAPI_PS5"),
    "input.pad_config": field('{"ports":[]}', "Port assignments and mappings (requires pad-config runtime).", "Input", env="ANYPS3_PAD_CONFIG", format="pad-config"),
    "saves.backup_before_run": field(True, "Back up saves before launching.", "Saves"),
    "saves.keep": field(10, "Automatic backups to retain; manual backups are kept.", "Saves", minimum=1, maximum=1000),
    "saves.backup_game_data": field(False, "Include installed game data in backups.", "Saves"),
    "diagnostics.perf_report": field(True, "Write FPS interval reports.", "Diagnostics", env="ANYPS3_PERF_REPORT"),
    "diagnostics.pad_trace": field(False, "Log reported controller input.", "Diagnostics", env="ANYPS3_PAD_TRACE"),
    "diagnostics.vulkan_validation": field(False, "Enable Vulkan validation layer.", "Diagnostics"),
    "diagnostics.capture_stride": field(0, "Capture every Nth frame; 0 disables captures.", "Diagnostics", minimum=0, maximum=1000000),
    "diagnostics.spu_max_steps": field(1000000000000, "SPU instruction budget per thread.", "Diagnostics", env="ANYPS3_SPU_MAX_STEPS", minimum=1, maximum=18446744073709551615),
    "diagnostics.sleep_budget_us": field(300000000000, "Guest sleep budget in microseconds.", "Diagnostics", env="ANYPS3_SLEEP_BUDGET_US", minimum=1, maximum=18446744073709551615),
    "experimental.host_drain": field(False, "Experimental host-request drain.", "Experimental", env="ANYPS3_RSX_HOST_DRAIN"),
    "experimental.flip_early": field(False, "Experimental early flip.", "Experimental", env="ANYPS3_RSX_FLIP_EARLY"),
    "experimental.recorder": field(False, "Asynchronous recorder; known teal-frame issue, keep off.", "Experimental", env="ANYPS3_RSX_RECORDER"),
}
for _f in FIELDS.values():
    if _f["group"] in ("Diagnostics", "Experimental"):
        _f.update(advanced=True, diagnostic=_f["group"] == "Diagnostics", experimental=_f["group"] == "Experimental")


def validate(key, value):
    if key not in FIELDS:
        raise ManagerError("unknown-setting", f"Unknown setting: {key}", 2)
    spec = FIELDS[key]
    expected = {"boolean": bool, "integer": int, "string": str}[spec["type"]]
    if type(value) is not expected:
        raise ManagerError("invalid-setting", f"{key} must be {spec['type']}.", 2)
    if "choices" in spec and value not in spec["choices"]:
        raise ManagerError("invalid-setting", f"{key}: choose from {spec['choices']}.", 2)
    if type(value) is int and not spec.get("minimum", value) <= value <= spec.get("maximum", value):
        raise ManagerError("invalid-setting", f"{key} is outside its allowed range.", 2)
    fmt = spec.get("format")
    if fmt == "memory" and not re.fullmatch(r"[1-9][0-9]*[MG]", value):
        raise ManagerError("invalid-setting", f"{key}: use a positive memory limit such as 12G.", 2)
    if fmt == "cpus" and value:
        if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
            raise ManagerError("invalid-setting", f"{key}: invalid CPU set.", 2)
        for part in value.split(","):
            ends = [int(n) for n in part.split("-")]
            if max(ends) >= (os.cpu_count() or 1) or ends[0] > ends[-1]:
                raise ManagerError("invalid-setting", f"{key}: CPU set outside this host.", 2)
    if fmt == "username" and (not value.strip() or len(value.encode()) > 63 or "\0" in value):
        raise ManagerError("invalid-setting", "User name must contain 1–63 UTF-8 bytes and no NUL.", 2)
    if fmt == "pad-config":
        try:
            config = json.loads(value)
            if not isinstance(config, dict) or not isinstance(config.get("ports"), list):
                raise ValueError("expected an object with ports array")
            if len(config["ports"]) > 7:
                raise ValueError("at most seven ports")
        except ValueError as exc:
            raise ManagerError("invalid-setting", f"Invalid pad configuration: {exc}", 2) from exc
    return value


def parse_value(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _stored_layers(lib, title=None):
    paths = [lib.root/"settings.json"]
    if title:
        lib.title(title)
        paths.append(lib.title_dir(title)/"settings.json")
    layers = []
    # Interpret legacy lock + sink together, including a title's inherited
    # global lock. Reading settings never rewrites a user's file.
    allowed, sink = False, "null"
    for path in paths:
        layer = read_json(path, {})
        if not isinstance(layer, dict):
            raise ManagerError("invalid-settings", "Settings must be a JSON object.")
        layer = dict(layer)
        legacy = "audio.audible_allowed" in layer or "audio.sink" in layer
        if "audio.audible_allowed" in layer:
            allowed = layer.pop("audio.audible_allowed")
            if type(allowed) is not bool:
                raise ManagerError("invalid-setting", "audio.audible_allowed must be boolean.", 2)
        if "audio.sink" in layer:
            sink = layer.pop("audio.sink")
            if type(sink) is not str or sink not in ("null", "sdl"):
                raise ManagerError("invalid-setting", "audio.sink must be null or sdl.", 2)
        if "audio.enabled" in layer:
            allowed = validate("audio.enabled", layer["audio.enabled"])
            sink = "sdl" if allowed else "null"
        elif legacy:
            layer["audio.enabled"] = allowed and sink == "sdl"
        layers.append(layer)
    return layers


def stored_values(lib, title=None):
    """Overrides for the GUI, using the same legacy migration as effective()."""
    return _stored_layers(lib, title)[-1]


def effective(lib, title=None, overrides=None):
    values = {k: f["default"] for k, f in FIELDS.items()}
    for layer in [*_stored_layers(lib, title), overrides if overrides is not None else {}]:
        if not isinstance(layer, dict):
            raise ManagerError("invalid-settings", "Settings must be a JSON object.")
        for key, value in layer.items():
            validate(key, value)
            values[key] = value
    return values


def set_value(lib, key, value, title=None, reset=False):
    if key not in FIELDS:
        validate(key, value)
    if FIELDS[key].get("global_only") and title:
        raise ManagerError("global-setting", f"{key} is global-only.", 2)
    if not reset:
        validate(key, value)
    path = lib.title_dir(title)/"settings.json" if title else lib.root/"settings.json"
    if title:
        lib.title(title)
    with lib.metadata():
        data = read_json(path, {})
        if key == "audio.enabled":
            data.pop("audio.audible_allowed", None)
            data.pop("audio.sink", None)
        if reset:
            data.pop(key, None)
        else:
            data[key] = value
        atomic_json(path, data)
    return dict(key=key, value=effective(lib, title)[key])


def schema(available=None):
    return {key: dict(spec, available=(available is None or not spec["env"] or
                                      not spec["env"].startswith("ANYPS3_") or spec["env"] in available))
            for key, spec in FIELDS.items()}


def clean_environment():
    # Ambient diagnostics, virtual-pad hooks and audible flags must never leak
    # from a developer's shell into a manager launch or detached worker.
    result = {k: v for k, v in os.environ.items()
              if not k.startswith(("ANYPS3_", "SDL_", "VK_")) and k not in ("LD_PRELOAD", "PYTHONPATH")}
    result.update(SDL_AUDIODRIVER="dummy", ANYPS3_AUDIO_SINK="null")
    return result


def run_environment(lib, title, settings, available, run_dir):
    env = clean_environment()
    root = lib.title_dir(title)
    env.update(ANYPS3_DISC_ROOT=str(root/"disc"), ANYPS3_DISC_CASEFOLD="1",
               ANYPS3_HDD_ROOT=str(root/"userdata/hdd0"), ANYPS3_CACHE_ROOT=str(root/"cache"),
               ANYPS3_PRESENT="sdl", ANYPS3_VK_DEVICE="gpu", ANYPS3_RSX_BACKEND="gpu",
               SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS="1", VK_INSTANCE_LAYERS="")
    for key, spec in FIELDS.items():
        variable = spec["env"]
        if not variable or variable == "ANYPS3_PAD_CONFIG":
            continue
        if variable.startswith("ANYPS3_") and variable not in available:
            continue
        value = settings[key]
        if key == "performance.rsx_threads" and value == 0:
            continue
        env[variable] = str(int(value)) if type(value) is bool else str(value)
    if "ANYPS3_PAD_CONFIG" in available:
        atomic_json(run_dir/"pads.json", json.loads(settings["input.pad_config"]))
        env["ANYPS3_PAD_CONFIG"] = str(run_dir/"pads.json")
    if settings["diagnostics.vulkan_validation"]:
        env["VK_INSTANCE_LAYERS"] = "VK_LAYER_KHRONOS_validation"
    if settings["diagnostics.capture_stride"]:
        (run_dir/"frames").mkdir(exist_ok=True)
        env.update(ANYPS3_FRAME_DIR=str(run_dir/"frames"),
                   ANYPS3_FRAME_STRIDE=str(settings["diagnostics.capture_stride"]))
    if settings["audio.enabled"]:
        env.pop("SDL_AUDIODRIVER")
        env.update(ANYPS3_AUDIO_SINK="sdl", ANYPS3_AUDIO_AUDIBLE="1")
    return env
