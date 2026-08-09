#!/usr/bin/env python
"""Generate the example LED effects `/led_control` ships with.

    python tools/gen_light_effects.py            # rewrite the JSON
    python tools/gen_light_effects.py --check    # is the committed file current?

Writes `ligmax_gui/light-effects-examples.json`, which `lights_effects.py`
loads beside the operator's own saved effects. Unlike `light-effects.json`
that file is **source, not state**: it is committed, it is the same in every
checkout, and nothing at runtime writes to it.

Why generate it rather than hand-write it: a per-pixel frame is 100 colours
and a smooth animation is dozens of frames, so these patterns are arithmetic -
a hue ramp, a decaying tail, a cosine. Writing the arithmetic down keeps the
file reviewable (read this, not 150 kB of hex) and re-derivable when the strip
length or the group split changes. Output is deterministic - no timestamps, no
randomness - so re-running it on an unchanged script produces a byte-identical
file and an empty diff.

Two hardware facts shape every colour here, both from
`ligmax-pi/nodes/io_manager/lights.py`:

* **Per-pixel frames go out as `DATA`, one hex nibble per channel** - 16
  levels, not 256 (`_nibble()`/`data_frame()`). Every colour below is snapped
  to those 16 levels, so the browser preview in `/led_control` shows what the
  hull will actually show instead of a gradient the wire cannot carry.
* **A frame whose pixels are all one colour may be sent as the string
  shorthand**, which goes out as `COL` at full precision. "Teal breathe" uses
  it; the rest are genuinely per-pixel.

Deliberately absent: anything a bystander could mistake for the safety
colours. Solid red is the rules' promise that the thrusters are dead and the
red strobe means out of control (see `status.py` and CLAUDE.md), so no example
paints the whole hull red, and none strobes. `lights.py` refuses to show any
of this while status is `KILLED` regardless - but an example pattern is a
thing people run at the dock in front of judges, and it should not need that
backstop to be honest.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ligmax_gui import lights_effects  # noqa: E402

NUM_LEDS = lights_effects.NUM_LEDS  # 100

# The wire-index split `/led_control` draws and `lights_esp.ino` renders: the
# left side's 51 LEDs as 28 + 23, then the right side's own 21 + 28. The third
# group is 21, not 22 - one LED in that run failed on 2026-08-10 and was wired
# out of the chain rather than replaced (see lights_esp.ino's header). Only
# used by "Group walk", which exists to prove this ordering on the real hull.
GROUPS = (28, 23, 21, 28)

DEFAULT_OUT = REPO_ROOT / "ligmax_gui" / "light-effects-examples.json"


# --- colour ----------------------------------------------------------------


def _level(value: float) -> int:
    """A 0..1 channel -> the nearest of the 16 levels `DATA` can carry.

    `lights.py:_nibble()` rounds to a nibble and the ESP32 doubles it back
    (`0xC` -> `0xCC`), so the only 8-bit values that survive a per-pixel frame
    are the multiples of 17. Snapping here means the editor's preview and the
    hull agree.
    """
    return max(0, min(15, int(round(value * 15)))) * 17


def rgb(red: float, green: float, blue: float) -> str:
    return "%02X%02X%02X" % (_level(red), _level(green), _level(blue))


def hsv(hue: float, sat: float, val: float) -> str:
    return rgb(*colorsys.hsv_to_rgb(hue % 1.0, max(0.0, min(1.0, sat)), max(0.0, min(1.0, val))))


OFF = rgb(0, 0, 0)


# --- the effects -----------------------------------------------------------
#
# Each returns `[{"hold_ms": ..., "pixels": ...}, ...]`, the shape
# `set_lights_pattern` takes. Every animation is written so frame N wraps onto
# frame 0: spatial terms use whole cycles across the strip and temporal terms
# whole cycles across the loop, because the vessel loops the pattern forever
# and a seam shows up as a visible stutter once a cycle.


def rainbow_sweep(frames: int = 48) -> list[dict[str, Any]]:
    """One full hue wheel laid along the hull, travelling bow-ward.

    The hue at wire index `i` is `i/100 + f/frames`, so the wheel closes on
    itself in space and has advanced exactly one whole wheel by the last
    frame - the loop is seamless in both directions.
    """
    return [
        {
            "hold_ms": 40,
            "pixels": [hsv(i / NUM_LEDS + f / frames, 1.0, 1.0) for i in range(NUM_LEDS)],
        }
        for f in range(frames)
    ]


def aurora_drift(frames: int = 36) -> list[dict[str, Any]]:
    """Slow green-to-violet curtains, two waves crossing at different speeds.

    Hue and brightness are separate standing waves - 2 cycles of hue and 3 of
    brightness along the strip, drifting at 1x and -2x the loop rate. Nothing
    about it is periodic-looking, which is the point: it reads as weather
    rather than as a chase.
    """
    out = []
    for f in range(frames):
        phase = 2.0 * math.pi * f / frames
        pixels = []
        for i in range(NUM_LEDS):
            u = i / NUM_LEDS
            hue_wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * 2.0 * u + phase)
            lit = 0.5 + 0.5 * math.sin(2.0 * math.pi * 3.0 * u - 2.0 * phase)
            pixels.append(hsv(0.33 + 0.42 * hue_wave, 0.85, 0.12 + 0.88 * lit**2))
        out.append({"hold_ms": 110, "pixels": pixels})
    return out


def comet(frames: int = 40) -> list[dict[str, Any]]:
    """A white-hot head with an azure tail, running the strip and wrapping.

    The tail is an exponential decay over the 18 LEDs behind the head, and the
    head desaturates to near-white - so it also shows, at a glance, which end
    of the hull is wire index 0 and which way indices climb.
    """
    tail, falloff = 18.0, 6.0
    out = []
    for f in range(frames):
        head = f * NUM_LEDS / frames
        pixels = []
        for i in range(NUM_LEDS):
            behind = (head - i) % NUM_LEDS
            if behind > tail:
                pixels.append(OFF)
                continue
            pixels.append(
                hsv(0.56, min(1.0, 0.15 + behind / tail), math.exp(-behind / falloff))
            )
        out.append({"hold_ms": 45, "pixels": pixels})
    return out


def group_walk() -> list[dict[str, Any]]:
    """Each of the four wire groups in turn, then all four together.

    Not decoration - this is the pattern you run once, on the real hull, to
    learn which physical run of LEDs is wire index 0..27, 28..50, 51..71 and
    72..99. That mapping is asserted in `/led_control`'s header comment and
    in `lights_esp.ino`, and has never been checked against the built boat.
    Colours are cyan/magenta/blue/white on purpose: none of them is a status
    colour, so nobody reads a wiring test as a state.
    """
    colours = [rgb(0, 1, 1), rgb(1, 0, 1), rgb(0.2, 0.35, 1), rgb(1, 1, 1)]
    bounds, start = [], 0
    for size in GROUPS:
        bounds.append((start, start + size))
        start += size

    out = []
    for (first, last), colour in zip(bounds, colours):
        pixels = [colour if first <= i < last else OFF for i in range(NUM_LEDS)]
        out.append({"hold_ms": 700, "pixels": pixels})
    together = []
    for i in range(NUM_LEDS):
        for (first, last), colour in zip(bounds, colours):
            if first <= i < last:
                together.append(colour)
                break
        else:  # unreachable while sum(GROUPS) == NUM_LEDS; cheap insurance
            together.append(OFF)
    out.append({"hold_ms": 1400, "pixels": together})
    return out


def colour_bars() -> list[dict[str, Any]]:
    """One static test card: eight equal bars, SMPTE order.

    The dullest example and the most useful one after "Group walk" - it is how
    you tell a dead LED from a dead segment, check that the strip renders the
    six saturated colours at all, and see the `DATA` nibble quantisation with
    your own eyes. The red bar is a bar, not the hull: nobody reads a test card
    as a status.
    """
    bars = [
        rgb(1, 1, 1), rgb(1, 1, 0), rgb(0, 1, 1), rgb(0, 1, 0),
        rgb(1, 0, 1), rgb(1, 0, 0), rgb(0, 0, 1), OFF,
    ]
    pixels = [bars[min(len(bars) - 1, i * len(bars) // NUM_LEDS)] for i in range(NUM_LEDS)]
    return [{"hold_ms": 5000, "pixels": pixels}]


def index_ruler() -> list[dict[str, Any]]:
    """A static ruler along the strip: a tick every 5, a bright one every 10.

    For counting physical LEDs back to wire indices without touching a
    multimeter - point at a lit pixel, count ticks, and you know the index the
    editor is painting. Index 0 is amber and alone so the origin is never
    ambiguous.
    """
    pixels = []
    for i in range(NUM_LEDS):
        if i == 0:
            pixels.append(rgb(1, 0.6, 0))
        elif i % 10 == 0:
            pixels.append(rgb(1, 1, 1))
        elif i % 5 == 0:
            pixels.append(rgb(0, 0.2, 0.5))
        else:
            pixels.append(OFF)
    return [{"hold_ms": 5000, "pixels": pixels}]


def njord_gradient() -> list[dict[str, Any]]:
    """A static deep-blue-to-white gradient, bow-ward. The dock look.

    One frame, so it costs the vessel nothing to hold: no worker wake-ups
    beyond the redraw, no loop to drift. Good for photographs and for showing
    the strip is alive without implying anything about the boat's state.
    """
    pixels = []
    for i in range(NUM_LEDS):
        u = i / (NUM_LEDS - 1)
        pixels.append(hsv(0.62 - 0.05 * u, 1.0 - u**1.5, 0.35 + 0.65 * u))
    return [{"hold_ms": 5000, "pixels": pixels}]


def scanner(frames: int = 32) -> list[dict[str, Any]]:
    """A violet eye sweeping bow to stern and back, with a soft trail.

    Bounces rather than wraps, so the turn-around lands on the physical ends -
    which is the bit worth watching: if the sweep stalls or jumps at either
    end, the strip length or the group split is wrong.
    """
    width = 7.0
    out = []
    for f in range(frames):
        # A triangle wave over the loop: down the strip and back in `frames`.
        swing = 2.0 * f / frames
        pos = (swing if swing <= 1.0 else 2.0 - swing) * (NUM_LEDS - 1)
        pixels = []
        for i in range(NUM_LEDS):
            near = max(0.0, 1.0 - abs(i - pos) / width)
            pixels.append(hsv(0.78, 1.0 - 0.7 * near**3, near**2))
        out.append({"hold_ms": 40, "pixels": pixels})
    return out


def ocean_swell(frames: int = 28) -> list[dict[str, Any]]:
    """Teal-to-blue swell running along the hull - two waves, one slower.

    The same standing-wave trick as "Aurora drift" but tuned to look like
    water rather than sky: shorter wavelength, less hue travel, and a crest
    that brightens instead of a curtain that drifts.
    """
    out = []
    for f in range(frames):
        phase = 2.0 * math.pi * f / frames
        pixels = []
        for i in range(NUM_LEDS):
            u = i / NUM_LEDS
            crest = 0.5 + 0.5 * math.sin(2.0 * math.pi * 3.0 * u - phase)
            under = 0.5 + 0.5 * math.sin(2.0 * math.pi * 1.0 * u + 2.0 * phase)
            pixels.append(hsv(0.45 + 0.13 * under, 0.9, 0.1 + 0.9 * crest**3))
        out.append({"hold_ms": 70, "pixels": pixels})
    return out


def starfield(frames: int = 24, stars: int = 26) -> list[dict[str, Any]]:
    """Cool-white twinkles over a near-black blue, each star on its own cycle.

    The one effect with randomness in it, and it is seeded - the star
    positions and rates are fixed at generation time, so this file stays
    byte-stable across runs. Each star's period is a whole number of cycles
    per loop, which is what keeps the twinkle from stuttering at the wrap.
    """
    rng = random.Random(20260808)
    picks = rng.sample(range(NUM_LEDS), stars)
    plan = [(i, rng.choice((1, 1, 2, 3)), rng.random()) for i in picks]
    base = hsv(0.62, 1.0, 0.06)

    out = []
    for f in range(frames):
        pixels = [base] * NUM_LEDS
        for index, rate, offset in plan:
            swell = 0.5 + 0.5 * math.sin(2.0 * math.pi * (rate * f / frames + offset))
            pixels[index] = hsv(0.58, 0.25, swell**4)
        out.append({"hold_ms": 90, "pixels": pixels})
    return out


def theatre_chase(frames: int = 18) -> list[dict[str, Any]]:
    """Every third LED lit, marching one step a frame, hue cycling as it goes.

    The classic marquee. Sparse on purpose: about a third of the strip is lit,
    so it is the cheapest example in current draw and the easiest to read from
    a distance at the dock.
    """
    out = []
    for f in range(frames):
        colour = hsv(f / frames, 1.0, 1.0)
        pixels = [colour if (i + f) % 3 == 0 else OFF for i in range(NUM_LEDS)]
        out.append({"hold_ms": 90, "pixels": pixels})
    return out


def colour_wipe(frames_per_colour: int = 8) -> list[dict[str, Any]]:
    """Three colours, each wiping over the last from index 0.

    Deliberately blocky - the wipe front is a hard edge, so it shows the
    addressing order and any dead run as an interruption in a straight line.
    The third wipe is overwritten by the first, so the loop closes.
    """
    colours = [hsv(0.5, 1.0, 1.0), hsv(0.75, 1.0, 1.0), hsv(0.12, 1.0, 1.0)]
    out = []
    for index, colour in enumerate(colours):
        behind = colours[index - 1]
        for step in range(1, frames_per_colour + 1):
            front = round(step * NUM_LEDS / frames_per_colour)
            pixels = [colour if i < front else behind for i in range(NUM_LEDS)]
            out.append({"hold_ms": 90, "pixels": pixels})
    return out


def group_breathe(frames: int = 24) -> list[dict[str, Any]]:
    """The four wire groups breathing in their own colours, a quarter apart.

    "Group walk" with the hard edges taken off: the same four-way split, but
    always something lit, so it can be left running while people talk over it.
    """
    hues = [0.5, 0.78, 0.62, 0.35]
    bounds, start = [], 0
    for size in GROUPS:
        bounds.append((start, start + size))
        start += size

    out = []
    for f in range(frames):
        pixels = [OFF] * NUM_LEDS
        for group, ((first, last), hue) in enumerate(zip(bounds, hues)):
            swell = (1.0 - math.cos(2.0 * math.pi * (f / frames + group / 4.0))) / 2.0
            colour = hsv(hue, 0.95, 0.06 + 0.94 * swell**2)
            for i in range(first, last):
                pixels[i] = colour
        out.append({"hold_ms": 80, "pixels": pixels})
    return out


def violet_heartbeat(frames: int = 24) -> list[dict[str, Any]]:
    """Two quick thumps and a rest, whole-hull violet. Solid frames.

    Violet, and never white or red: a slow whole-hull white breathe is the
    standby status and whole-hull red is the killed one, so an example that
    pulses the entire strip must not sit near either colour.
    """
    thumps = ((0, 1.0), (1, 0.55), (2, 0.2), (4, 0.75), (5, 0.4), (6, 0.15))
    envelope = dict(thumps)
    return [
        {"hold_ms": 70, "pixels": hsv(0.8, 0.9, 0.04 + 0.96 * envelope.get(f, 0.0))}
        for f in range(frames)
    ]


def teal_breathe(frames: int = 32) -> list[dict[str, Any]]:
    """The whole hull breathing teal, and the one example in string form.

    Every frame is a single colour, so each is written as one `"RRGGBB"`
    instead of 100 copies of it - which is also what makes it go out as a
    full-precision `COL` frame rather than a nibble `DATA` one. Teal, not
    white: a slow white breathe is the standby status, and this should not be
    mistakable for it.
    """
    out = []
    for f in range(frames):
        swell = (1.0 - math.cos(2.0 * math.pi * f / frames)) / 2.0
        out.append({"hold_ms": 60, "pixels": hsv(0.47, 1.0, 0.08 + 0.92 * swell)})
    return out


# Name, one-line description (the GUI shows it as the option's tooltip), frames.
# Fifteen, in four rough families: three static test cards, three sweeps, four
# ambient loops, and the rest character. Names are what the operator sees, so
# they say what the pattern looks like, not which function made it.
EXAMPLES: list[tuple[str, str, list[dict[str, Any]]]] = [
    (
        "Aurora drift",
        "Slow green-to-violet curtains. Per-pixel, 36 frames, ~4 s loop.",
        aurora_drift(),
    ),
    (
        "Colour bars",
        "Static test card: eight SMPTE-order bars. One frame - for spotting a dead LED or a dead segment.",
        colour_bars(),
    ),
    (
        "Colour wipe",
        "Three colours wiping over each other from index 0. Per-pixel, 24 frames, ~2.2 s loop.",
        colour_wipe(),
    ),
    (
        "Comet",
        "A white head with an azure tail running the strip. Per-pixel, 40 frames, ~1.8 s loop.",
        comet(),
    ),
    (
        "Group breathe",
        "The four wire groups breathing in their own colours, a quarter cycle apart. 24 frames, ~1.9 s loop.",
        group_breathe(),
    ),
    (
        "Group walk",
        "Each of the four wire groups (28/23/21/28) alone, then all four - "
        "the pattern for checking wire order on the real hull.",
        group_walk(),
    ),
    (
        "Index ruler",
        "Static: a tick every 5 LEDs, a bright one every 10, amber at index 0. "
        "For counting physical LEDs back to wire indices.",
        index_ruler(),
    ),
    (
        "Njord gradient",
        "Static deep-blue-to-white gradient along the hull. One frame - the dock look.",
        njord_gradient(),
    ),
    (
        "Ocean swell",
        "Teal-to-blue swell running along the hull. Per-pixel, 28 frames, ~2 s loop.",
        ocean_swell(),
    ),
    (
        "Rainbow sweep",
        "A full hue wheel travelling along the hull. Per-pixel, 48 frames, ~1.9 s loop.",
        rainbow_sweep(),
    ),
    (
        "Scanner",
        "A violet eye sweeping bow to stern and back. Per-pixel, 32 frames, ~1.3 s loop.",
        scanner(),
    ),
    (
        "Starfield",
        "Cool-white twinkles over near-black blue, each star on its own cycle. 24 frames, ~2.2 s loop.",
        starfield(),
    ),
    (
        "Teal breathe",
        "The whole hull breathing teal. Solid frames (COL, full precision), 32 frames, ~1.9 s loop.",
        teal_breathe(),
    ),
    (
        "Theatre chase",
        "Every third LED lit and marching, hue cycling as it goes. 18 frames, ~1.6 s loop.",
        theatre_chase(),
    ),
    (
        "Violet heartbeat",
        "Two quick thumps and a rest, whole hull. Solid frames (COL), 24 frames, ~1.7 s loop.",
        violet_heartbeat(),
    ),
]


# --- output ----------------------------------------------------------------


def build() -> str:
    """The file's text, validated frame by frame before it is anyone's problem.

    Validation runs through `lights_effects.validate_frames()` - the same
    function the save path and `/api/command` use - so a generator bug becomes
    a failure here rather than a pattern the vessel silently rejects at the
    dock.
    """
    body = []
    for name, description, frames in EXAMPLES:
        cleaned, why = lights_effects.validate_frames(frames)
        if why is not None:
            raise SystemExit(f"'{name}' would not validate: {why}")
        if len(name) > lights_effects.MAX_EFFECT_NAME:
            raise SystemExit(f"'{name}' is longer than MAX_EFFECT_NAME")
        body.append((name, description, cleaned))

    lines = [
        "{",
        '  "version": 1,',
        '  "generated_by": "tools/gen_light_effects.py",',
        '  "effects": {',
    ]
    for index, (name, description, frames) in enumerate(body):
        lines.append(f"    {json.dumps(name)}: {{")
        lines.append(f'      "description": {json.dumps(description)},')
        lines.append('      "frames": [')
        for position, frame in enumerate(frames):
            # One frame per line, keys in wire order: 100 colours pretty-printed
            # one per line would be 6000 lines an effect and unreviewable.
            hold = frame["hold_ms"]
            ordered = {
                "hold_ms": int(hold) if float(hold).is_integer() else hold,
                "pixels": frame["pixels"],
            }
            tail = "," if position < len(frames) - 1 else ""
            lines.append("        " + json.dumps(ordered, separators=(",", ":")) + tail)
        lines.append("      ]")
        lines.append("    }" + ("," if index < len(body) - 1 else ""))
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", default=str(DEFAULT_OUT), help="output path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the file on disk is not what this script generates",
    )
    args = parser.parse_args()

    out = Path(args.out)
    text = build()
    frames = sum(len(effect[2]) for effect in EXAMPLES)

    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print(f"{out} is stale - re-run {Path(__file__).name}", file=sys.stderr)
            return 1
        print(f"{out} is current ({len(EXAMPLES)} effects, {frames} frames)")
        return 0

    out.write_text(text, encoding="utf-8")
    print(
        f"wrote {out} - {len(EXAMPLES)} effects, {frames} frames, "
        f"{len(text) / 1024:.0f} kB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
