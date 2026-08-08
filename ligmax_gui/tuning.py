"""The stabilisation tuning the operator may change, and the saved profiles.

Two separate things live here, and they answer two different halves of "save and
load the gains from the dashboard":

1. **`TUNABLES`** - the whitelist of flight-controller parameters the console
   offers, with the range each write must fall inside and the words the panel
   labels it with. Validated here before a `set_param` is queued, so a bad value
   is a 400 the operator reads rather than a command the vessel silently refuses
   a second later.

   This is a **mirror of `ligmax-pi/nodes/io_manager/tuning.py`**, in the same
   way `protocol.py` mirrors the vessel's `ObstacleType`. The vessel's copy is
   the one that matters - it is what actually refuses to write - and this one
   exists so the dashboard can validate early and render sensibly. Keep the
   names and ranges in step; `check_tuning_sync()` cannot help here because the
   two files are in different repos and never run in the same process.

2. **`ProfileStore`** - named snapshots of the whole set, on disk on the ground
   station. The vessel already saves each individual value: ArduPilot's
   PARAM_SET is a set-and-save, so a gain written from here is in the flight
   controller's own storage and survives every reboot in the chain. What that
   does *not* survive is a parameter reset on the Pixhawk, a firmware flash, or
   a swapped flight controller - all of which happen, and each of which would
   otherwise throw away a bench-tuned set that exists nowhere else. Hence a copy
   on shore, and one click to put it back.

   This was the only thing in `ligmax-server` that wrote to disk until
   `lights_effects.EffectStore` joined it (same shape, for `/led_control`'s
   saved patterns). Everything else is RAM-only and loses its state on
   restart, which is a documented and deliberate trade for the log ring and
   the command audit. It is the wrong trade for the gains: they are a piece of
   state here that is expensive to recreate and cheap to store.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

# RC input channels a trim knob may be assigned to. 1..8 are the sticks and the
# aux switches on this boat, 15 and 16 are the amas' ride-height command, so a
# knob belongs on 9..14. Both Lua scripts enforce this themselves; the dashboard
# says so in the field's help text instead of letting someone find out.
MIN_RC_CHANNEL = 9
MAX_RC_CHANNEL = 16

MAX_PROFILES = 24
MAX_PROFILE_NAME = 40


class Tunable:
    """One parameter the console offers, and everything the panel needs to draw it.

    `kind` decides the widget: `number` is a plain float field, `channel` is the
    "0, or 9..16" RC-channel rule, `choice` is a dropdown over `options`.
    `writable=False` renders the value with no field at all.
    """

    def __init__(
        self,
        name: str,
        group: str,
        label: str,
        *,
        low: float,
        high: float,
        unit: str = "",
        step: float = 0.01,
        integer: bool = False,
        kind: str = "number",
        options: tuple[tuple[int, str], ...] = (),
        writable: bool = True,
        warn: bool = False,
        help: str = "",
    ) -> None:
        self.name = name
        self.group = group
        self.label = label
        self.low = float(low)
        self.high = float(high)
        self.unit = unit
        self.step = step
        self.integer = integer
        self.kind = kind
        self.options = options
        self.writable = writable
        # `warn` marks a field whose effect is motion rather than a coefficient:
        # the panel says so next to it, and the confirmation names it.
        self.warn = warn
        self.help = help

    def to_ui(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": self.name,
            "group": self.group,
            "label": self.label,
            "low": self.low,
            "high": self.high,
            "unit": self.unit,
            "step": self.step,
            "integer": self.integer,
            "kind": self.kind,
            "writable": self.writable,
            "warn": self.warn,
            "help": self.help,
        }
        if self.options:
            spec["options"] = [{"value": v, "label": l} for v, l in self.options]
        return spec

    def clean(self, value: Any) -> tuple[float | None, str | None]:
        """`(number, None)` if this may be sent, `(None, why)` if it may not."""
        if not self.writable:
            return None, f"{self.name} is read-only from the dashboard"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"'{self.name}' must be a number"
        if not math.isfinite(number):
            return None, f"'{self.name}' must be a finite number"
        if self.integer:
            number = float(round(number))
        if self.kind == "channel":
            if number != 0.0 and not (MIN_RC_CHANNEL <= number <= MAX_RC_CHANNEL):
                return None, (
                    f"'{self.name}' is an RC input channel: 0 turns the knob off, "
                    f"otherwise {MIN_RC_CHANNEL}-{MAX_RC_CHANNEL}. Channels 1-8 are "
                    "the sticks and the aux switches."
                )
            return number, None
        if not (self.low <= number <= self.high):
            return None, (
                f"'{self.name}' must be between {self.low:g} and {self.high:g}"
            )
        return number, None


# Two groups, because they are two independent loops on two different actuators
# and nobody tunes both at once.
GROUPS: tuple[dict[str, str], ...] = (
    {
        "key": "roll",
        "title": "Roll — the amas",
        "script": "amas.lua",
        "note": (
            "A PD loop on the flight controller driving both ama actuators through "
            "the translator ESP32, which reads its pulses as velocities. Gains are "
            "degrees in, microseconds out."
        ),
    },
    {
        "key": "pitch",
        "title": "Pitch — the battery slider",
        "script": "battery_slider.lua",
        "note": (
            "A PID moving the 1.8 kWh pack fore and aft. The slider ESP32 reads its "
            "pulse as an absolute position, so gains are in fractions of rail travel "
            "per degree. BSLD_ENABLE 0 parks the pack and does nothing else."
        ),
    },
)

TUNABLES: tuple[Tunable, ...] = (
    # --- amas.lua ------------------------------------------------------------
    Tunable(
        "SCR_USER1", "roll", "Roll Kp", low=0.0, high=500.0, unit="µs/°", step=0.5,
        help="Proportional gain. Output is clamped to ±500 µs, so above 500 a "
             "one-degree error already saturates the actuators.",
    ),
    Tunable(
        "SCR_USER2", "roll", "Roll Kd", low=0.0, high=500.0, unit="µs/(°/s)", step=0.5,
        help="Damping, on the filtered roll rate (FILTER_ALPHA 0.15 in amas.lua).",
    ),
    Tunable(
        "SCR_USER3", "roll", "Trim knob channel", low=0, high=MAX_RC_CHANNEL,
        integer=True, kind="channel", step=1,
        help=f"RC input the pilot's roll-trim knob is on. 0 = off, otherwise "
             f"{MIN_RC_CHANNEL}-{MAX_RC_CHANNEL}. Needs the range below to be "
             f"non-zero as well before it does anything.",
    ),
    Tunable(
        "SCR_USER4", "roll", "Trim knob range", low=0.0, high=20.0, unit="°", step=0.1,
        help="Degrees of target roll at full knob deflection.",
    ),
    Tunable(
        "SCR_USER5", "roll", "Roll trim from here", low=-10.0, high=10.0, unit="°",
        step=0.1,
        help="Adds to the pilot's knob rather than replacing it. Use it when the "
             "hull sits level but the AHRS insists it does not.",
    ),
    Tunable(
        "SCR_USER6", "roll", "Ride-height trim from here", low=-250.0, high=250.0,
        unit="µs", step=5, warn=True,
        help="The translator reads its pulse as a velocity, so this is a standing "
             "speed command: both amas creep in this direction for as long as it "
             "is set, and it persists across a reboot. Inside ±25 µs it does "
             "nothing (the translator's deadband). Set it back to 0 when the hull "
             "is where you want it.",
    ),
    # --- battery_slider.lua --------------------------------------------------
    Tunable(
        "BSLD_ENABLE", "pitch", "Pitch loop", low=0, high=2, integer=True,
        kind="choice", step=1,
        options=((0, "0 — parked at trim, open loop"),
                 (1, "1 — closed loop while armed"),
                 (2, "2 — closed loop always (bench)")),
        warn=True,
        help="2 moves the pack with the propellers idle, which is what bench "
             "tuning needs and not what you want alongside a pontoon.",
    ),
    Tunable(
        "BSLD_KP", "pitch", "Pitch Kp", low=0.0, high=5.0, unit="travel/°", step=0.005,
        help="Fraction of rail travel per degree of pitch error. Travel is ±1, so "
             "0.05 is 5 % of the rail per degree.",
    ),
    Tunable(
        "BSLD_KI", "pitch", "Pitch Ki", low=0.0, high=5.0, unit="travel/°s", step=0.005,
        help="Integral term. Capped by the integral cap below, with conditional "
             "integration so sitting on the soft limit does not wind it up.",
    ),
    Tunable(
        "BSLD_KD", "pitch", "Pitch Kd", low=0.0, high=5.0, unit="travel/(°/s)",
        step=0.005,
        help="Damping, on the measured pitch rate — not on the error — so moving "
             "the target does not kick the output.",
    ),
    Tunable(
        "BSLD_IMAX", "pitch", "Integral cap", low=0.0, high=1.0, unit="travel",
        step=0.01,
        help="Hard limit on the integral term, as a fraction of travel.",
    ),
    Tunable(
        "BSLD_TRIM", "pitch", "Level-float position", low=-1.0, high=1.0,
        unit="travel", step=0.01,
        help="Where the pack sits for the hull to float level: -1 full aft, "
             "0 rail centre, +1 full forward. Also where the loop parks when it "
             "is switched off or a tick fails.",
    ),
    Tunable(
        "BSLD_LIMIT", "pitch", "Soft travel limit", low=0.0, high=1.0, unit="travel",
        step=0.01,
        help="Keeps the demand off the endstops. The firmware enforces its own "
             "envelope too; this is the margin for the optical zero being wrong.",
    ),
    Tunable(
        "BSLD_SIGN", "pitch", "Direction sign", low=-1, high=1, integer=True,
        writable=False,
        help="+1 or -1, and read-only from here on purpose: a wrong sign is a "
             "divergent loop driving the pack into an endstop. Set it on the "
             "bench with the hull supported — battery_slider.lua, BEFORE FIRST "
             "RUN step 3.",
    ),
    Tunable(
        "BSLD_TRM_CH", "pitch", "Trim knob channel", low=0, high=MAX_RC_CHANNEL,
        integer=True, kind="channel", step=1,
        help=f"RC input the pilot's pitch-trim knob is on. 0 = off, otherwise "
             f"{MIN_RC_CHANNEL}-{MAX_RC_CHANNEL}.",
    ),
    Tunable(
        "BSLD_TRM_DEG", "pitch", "Trim knob range", low=0.0, high=20.0, unit="°",
        step=0.1,
        help="Degrees of target pitch at full knob deflection.",
    ),
    Tunable(
        "BSLD_TRM_OFS", "pitch", "Pitch trim from here", low=-10.0, high=10.0,
        unit="°", step=0.1,
        help="Adds to the pilot's knob. Moves where the loop settles, so unlike "
             "the amas' height trim the pack takes up a new position and stays "
             "there rather than creeping.",
    ),
)

BY_NAME: dict[str, Tunable] = {spec.name: spec for spec in TUNABLES}

WRITABLE = tuple(spec.name for spec in TUNABLES if spec.writable)


def spec_payload() -> dict[str, Any]:
    """The table the frontend renders itself from, so nothing is hardcoded twice."""
    return {
        "groups": list(GROUPS),
        "params": [spec.to_ui() for spec in TUNABLES],
        "min_rc_channel": MIN_RC_CHANNEL,
        "max_rc_channel": MAX_RC_CHANNEL,
    }


def validate(name: Any, value: Any) -> tuple[str | None, float | None, str | None]:
    """`(name, value, None)` if this `set_param` may be queued, else the reason."""
    key = str(name or "").strip().upper()
    spec = BY_NAME.get(key)
    if spec is None:
        return None, None, (
            f"'{key}' is not a tunable parameter. The console offers: "
            f"{', '.join(WRITABLE)}"
        )
    cleaned, why = spec.clean(value)
    if why is not None:
        return None, None, why
    return key, cleaned, None


class ProfileStore:
    """Named snapshots of the tuning set, kept in one JSON file on this box.

    Small, rarely written, and read on every dashboard load, so it is held in
    memory and rewritten whole. Writes go to a temporary file and are renamed
    over the original, so a crash mid-save cannot leave a half-written profile
    that would be indistinguishable from a corrupted one.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._profiles: dict[str, dict[str, Any]] = {}
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Keep going with no profiles rather than refusing to start: the
            # dashboard's job is flying a boat, not reading this file.
            self.last_error = f"could not read {self.path}: {exc}"
            return
        profiles = raw.get("profiles") if isinstance(raw, dict) else None
        if not isinstance(profiles, dict):
            self.last_error = f"{self.path} has no 'profiles' object"
            return
        for name, entry in profiles.items():
            if not isinstance(entry, dict):
                continue
            values = {
                key: float(item)
                for key, item in (entry.get("values") or {}).items()
                if key in BY_NAME and isinstance(item, (int, float))
            }
            if not values:
                continue
            self._profiles[str(name)[:MAX_PROFILE_NAME]] = {
                "values": values,
                "saved_at": entry.get("saved_at"),
                "saved_by": str(entry.get("saved_by") or "")[:64],
                "note": str(entry.get("note") or "")[:200],
            }

    def _flush(self) -> None:
        payload = {"version": 1, "profiles": self._profiles}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": name,
                    "saved_at": entry.get("saved_at"),
                    "saved_by": entry.get("saved_by"),
                    "note": entry.get("note"),
                    "values": dict(entry["values"]),
                    "count": len(entry["values"]),
                }
                for name, entry in sorted(self._profiles.items())
            ]

    def get(self, name: str) -> dict[str, float] | None:
        with self._lock:
            entry = self._profiles.get(str(name)[:MAX_PROFILE_NAME])
            return dict(entry["values"]) if entry else None

    def save(
        self, name: str, values: dict[str, Any], by: str = "", note: str = ""
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Store a snapshot. Only whitelisted, writable, in-range values are kept."""
        clean_name = " ".join(str(name or "").split())[:MAX_PROFILE_NAME]
        if not clean_name:
            return None, "a profile needs a name"
        kept: dict[str, float] = {}
        for key, value in (values or {}).items():
            spec = BY_NAME.get(str(key).strip().upper())
            # Read-only parameters are recorded but never applied - BSLD_SIGN in
            # a saved profile is a note about the boat it was tuned on, not
            # something this server will ever write back.
            if spec is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                kept[spec.name] = number
        if not kept:
            return None, "nothing to save: no tuning values have been read yet"
        with self._lock:
            if clean_name not in self._profiles and len(self._profiles) >= MAX_PROFILES:
                return None, f"at most {MAX_PROFILES} profiles; delete one first"
            self._profiles[clean_name] = {
                "values": kept,
                "saved_at": time.time(),
                "saved_by": str(by)[:64],
                "note": str(note or "")[:200],
            }
            try:
                self._flush()
            except OSError as exc:
                del self._profiles[clean_name]
                return None, f"could not write {self.path}: {exc}"
            return {"name": clean_name, "count": len(kept)}, None

    def delete(self, name: str) -> tuple[bool, str | None]:
        key = str(name or "")[:MAX_PROFILE_NAME]
        with self._lock:
            if key not in self._profiles:
                return False, f"no profile called '{key}'"
            removed = self._profiles.pop(key)
            try:
                self._flush()
            except OSError as exc:
                self._profiles[key] = removed
                return False, f"could not write {self.path}: {exc}"
            return True, None
