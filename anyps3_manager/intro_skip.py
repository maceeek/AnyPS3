"""Title-declared intro skipping through the original cellPad input path.

A recipe classifies the runtime's PERF interval reports and presses a button
through the generic host injection channel (ANYPS3_PAD_INJECT, native_pad.py).
Nothing in guest memory, code, assets or saves is changed: the game itself
decides what the button does. The driver yields for good on the first
physical button press, on a guest STOP, after its timeout, or once the
recipe's menu signature was seen in two consecutive fresh reports.
"""

import math
import os
from pathlib import Path
import time

# CELL_PAD_CTRL_* (PSL1GHT io/pad.h): digital1 word 2, digital2 word 3.
BUTTONS = {"start": (0x0008, 0), "cross": (0, 0x0040)}

# Per-title data (the only game-specific part). The thresholds are rendering
# telemetry heuristics, not visual state detection; their evidence:
# local/ratchet/tools/fast_menu.md, docs/knowledge/experiments/2026-10-23-render-scale.md
# (runs 705a-d, 706fastmenu1 and 800a reached the original menu with START only).
RECIPES = {
    "BCES00511": dict(
        name="Ratchet & Clank: A Crack in Time",
        button="start",
        menu_draws_per_flip=(74, 84),
        title_draws_per_flip=(62, 66),
        video_spu_steps_per_s=40_000_000,
        video_max_draws_per_s=30,
        screen_min_flips=2600,
        early_title=True,
        hold_s=1.0,
        gap_s=4.0,
        timeout_s=180.0,
    ),
}


def recipe(title):
    return RECIPES.get(title)


def classify(line, spec):
    """Map one 'PERF interval_s=...' line to a coarse startup state."""
    if not line.startswith("PERF interval_s="):
        return None
    try:
        values = dict(word.split("=", 1) for word in line.split() if "=" in word)
        flips, rate, draws, spu = (float(values[key]) for key in
                                  ("flips", "flips_per_s", "draws_per_s", "spu_steps_per_s"))
    except (KeyError, ValueError):
        return None
    if not all(math.isfinite(v) and v >= 0 for v in (flips, rate, draws, spu)):
        return None
    ratio = draws / rate if rate > .5 else 0
    low, high = spec["menu_draws_per_flip"]
    if rate > 5 and low < ratio < high:
        return "menu", flips
    if spu > spec["video_spu_steps_per_s"] and draws < spec["video_max_draws_per_s"]:
        return "video", flips
    low, high = spec["title_draws_per_flip"]
    if flips > 300 and rate > 5 and low < ratio < high:
        return "title", flips
    return ("screen" if flips > spec["screen_min_flips"] else "boot"), flips


def write_state(path, present, digital1=0, digital2=0):
    path = Path(path)
    temporary = path.with_name(path.name+".next")
    temporary.write_text(f"v1 {int(present)} {digital1} {digital2}\n")
    os.replace(temporary, path)


class IntroSkip:
    """Feed stderr lines with line(); call tick() periodically."""

    def __init__(self, path, spec, clock=time.monotonic):
        self.path, self.spec, self.clock = Path(path), spec, clock
        self.started = clock()
        self.state, self.reason = "active", None
        self.presses = 0
        self.menu_count = self.title_count = 0
        self.latest = None
        self.release_at = None
        self.next_press = 0.0
        self.ended = None
        write_state(self.path, True)

    @property
    def active(self):
        return self.state == "active"

    def line(self, line):
        if not self.active:
            return
        if "native pad inject physical input" in line:
            self.finish("player-input")
            return
        if "STOP pc=" in line or "STOP:" in line:
            self.finish("guest-stop")
            return
        result = classify(line, self.spec)
        if result:
            state, flips = result
            self.menu_count = self.menu_count+1 if state == "menu" else 0
            self.title_count = self.title_count+1 if state == "title" else 0
            self.latest = (state, flips)

    def tick(self):
        if not self.active:
            return
        now = self.clock()
        if self.release_at is not None and now >= self.release_at:
            write_state(self.path, True)
            self.release_at = None
            self.next_press = now+self.spec["gap_s"]
        if now-self.started >= self.spec["timeout_s"]:
            self.finish("timeout")
            return
        # Act only on newly reported intervals, never on a stale one.
        if self.latest is None:
            return
        state, flips = self.latest
        self.latest = None
        if self.menu_count >= 2:
            self.finish("menu")
            return
        can_press = state in ("video", "screen")
        if state == "title":
            can_press = flips > self.spec["screen_min_flips"] or (self.spec["early_title"] and self.title_count >= 2)
        if can_press and self.release_at is None and now >= self.next_press:
            write_state(self.path, True, *BUTTONS[self.spec["button"]])
            self.presses += 1
            self.release_at = now+self.spec["hold_s"]
            self.title_count = 0

    def finish(self, reason):
        if not self.active:
            return
        self.state, self.reason, self.ended = "finished", reason, self.clock()
        self.release_at = None
        # Release port 0: it stays connected only through a physical pad.
        try:
            write_state(self.path, False)
        except OSError:
            pass

    def summary(self):
        end = self.ended if self.ended is not None else self.clock()
        return dict(recipe=self.spec["name"], state=self.state, reason=self.reason,
                    presses=self.presses, seconds=round(end-self.started, 3))
