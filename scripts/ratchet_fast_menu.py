"""Diagnostic START-only driver through the original game's input path.

Requires an exclusively controlled virtual-pad control.txt and PERF_REPORT=1.
Menu detection is a performance heuristic, not visual or physical-pad evidence.
--early-title permits START on stable ~64 draws/flip splash candidates.
These include notices and logos, not just the title. This is automated boot
navigation, not a verified movie skip. Verify the resulting menu visually.
No guest memory, code, assets or savedata are modified.
"""
import argparse
import math
from pathlib import Path
import signal
import time

def set_start(path, pressed):
    # SDL virtual-pad wrapper protocol: button mask, LX LY RX RY LT RT.
    # START is button 6; sticks are centred and triggers released.
    temporary = path.with_name(path.name + ".next")
    temporary.write_text(f"{64 if pressed else 0} 0 0 0 0 -32768 -32768\n")
    temporary.replace(path)


def classify(line):
    if not line.startswith("PERF interval_s="):
        return None
    try:
        values = dict(word.split("=", 1) for word in line.split() if "=" in word)
        flips, rate, draws, spu = (float(values[key]) for key in
                                  ("flips", "flips_per_s", "draws_per_s", "spu_steps_per_s"))
        if not all(math.isfinite(v) and v >= 0 for v in (flips, rate, draws, spu)):
            return None
    except (KeyError, ValueError):
        return None
    ratio = draws / rate if rate > .5 else 0
    if rate > 5 and 74 < ratio < 84:
        return "menu"
    if spu > 40_000_000 and draws < 30:
        return "video"
    if flips > 300 and rate > 5 and 62 < ratio < 66:
        return "title"
    return "screen" if flips > 2600 else "boot"


def drive(directory, timeout=540, early_title=False, hold=1, gap=4):
    control = directory / "control.txt"
    if not control.is_file():
        raise ValueError(f"Missing virtual pad control: {control}")
    log = directory / "stderr.log"
    deadline = time.monotonic() + timeout
    offset, pending = 0, b""
    menu_count = title_count = 0
    release_at = None
    next_press = 0

    def report(message):
        print(time.strftime("%H:%M:%S"), message, flush=True)

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if release_at is not None and now >= release_at:
                set_start(control, False)
                release_at = None
                next_press = now + gap
            try:
                with log.open("rb") as stream:
                    if stream.seek(0, 2) < offset:
                        raise RuntimeError("stderr.log truncated; refusing stale run input")
                    stream.seek(offset)
                    data = stream.read()
                    offset = stream.tell()
            except FileNotFoundError:
                data = b""
            pending += data
            lines = pending.split(b"\n")
            pending = lines.pop()
            states = []
            for raw in lines:
                line = raw.decode("utf-8", errors="replace")
                if "STOP:" in line or line.startswith("STOP "):
                    report("guest STOP; driver stopped")
                    return 2
                state = classify(line)
                if state:
                    states.append(state)
                    menu_count = menu_count + 1 if state == "menu" else 0
                    title_count = title_count + 1 if state == "title" else 0
            # Act only on newly emitted intervals. Re-reading an unchanged log
            # must neither confirm a menu nor repeat START on a dead process.
            if states:
                state = states[-1]
                if menu_count >= 2:
                    report("MENU (two fresh PERF samples; visual verification required)")
                    return 0
                can_press = state in ("video", "screen")
                if state == "title":
                    # The conservative path still uses the existing flip guard.
                    latest = next(raw.decode("utf-8", errors="replace") for raw in reversed(lines)
                                  if raw.startswith(b"PERF interval_s="))
                    flips = float(dict(w.split("=", 1) for w in latest.split() if "=" in w)["flips"])
                    can_press = flips > 2600 or (early_title and title_count >= 2)
                if can_press and release_at is None and now >= next_press:
                    report(f"{state} -> START (hold {hold:g} s)")
                    set_start(control, True)
                    release_at = now + hold
                    title_count = 0
            time.sleep(.1)
        report("timeout; menu not verified")
        return 1
    finally:
        if release_at is not None:
            set_start(control, False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--timeout", type=float, default=540)
    parser.add_argument("--early-title", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be positive and finite")
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        return drive(args.directory, args.timeout, args.early_title)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
