"""Named LED-pattern presets for `/led_control`, on disk on the ground station.

Mirrors the split in `tuning.py`, for the same reason:

1. **`validate_frames()`** - the same frame/pixel/hold_ms shape `/api/command`'s
   `set_lights_pattern` accepts and `ligmax-pi/nodes/io_manager/lights.py`'s
   `_parse_pattern()` parses on the vessel. Factored out here so `/led_control`'s
   "save" path and its "send to boat" path validate against one definition
   instead of two copies that could drift apart.

2. **`EffectStore`** - named snapshots of a pattern, kept in one JSON file, the
   same load-whole/rewrite-whole/write-tmp-then-rename shape as
   `tuning.ProfileStore`. `/led_control` itself holds no state between loads -
   a pattern authored in the browser lives only in that tab until Save writes
   it here - so without this, closing the tab lost every effect that had not
   just been sent to the boat. This is the second thing in `ligmax-server`
   that writes to disk; `tuning.ProfileStore` is the first, and both exist for
   the same reason: some state here is expensive to recreate and cheap to
   store, which is the wrong trade for RAM-only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

# Mirrors ligmax-pi/nodes/io_manager/lights.py's NUM_LEDS/MAX_PATTERN_FRAMES/
# MIN_HOLD_S/MAX_HOLD_S defaults, exactly like server.py's own copy of these
# used to (now delegated here) - kept in step so a pattern that validates on
# this box is one the vessel will actually accept.
NUM_LEDS = 101
MAX_FRAMES = 60
MIN_HOLD_MS = 20
MAX_HOLD_MS = 60_000
_HEX_COLOUR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")

MAX_EFFECTS = 40
MAX_EFFECT_NAME = 40


def validate_frames(frames: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """`frames` off the wire -> cleaned `[{"pixels": ..., "hold_ms": float}, ...]`,
    or `(None, why)`.
    """
    if not isinstance(frames, list) or not frames:
        return None, "'frames' must be a non-empty list"
    if len(frames) > MAX_FRAMES:
        return None, f"a pattern may have at most {MAX_FRAMES} frames"
    cleaned: list[dict[str, Any]] = []
    for i, entry in enumerate(frames):
        if not isinstance(entry, dict):
            return None, f"frame {i} is not an object"
        try:
            hold_ms = float(entry.get("hold_ms"))
        except (TypeError, ValueError):
            return None, f"frame {i} has a bad hold_ms"
        if not (MIN_HOLD_MS <= hold_ms <= MAX_HOLD_MS):
            return None, (
                f"frame {i} hold_ms must be between {MIN_HOLD_MS} and {MAX_HOLD_MS}"
            )
        pixels = entry.get("pixels")
        if isinstance(pixels, str):
            swatch = [pixels]
        elif isinstance(pixels, list):
            if len(pixels) != NUM_LEDS:
                return None, f"frame {i} has {len(pixels)} pixels, want {NUM_LEDS}"
            swatch = pixels
        else:
            return None, f"frame {i} pixels must be a hex string or a list"
        for value in swatch:
            if not _HEX_COLOUR_RE.match(str(value)):
                return None, f"frame {i} has a bad colour {value!r}"
        cleaned.append({"pixels": pixels, "hold_ms": hold_ms})
    return cleaned, None


class EffectStore:
    """Named LED patterns, kept in one JSON file on this box."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._effects: dict[str, dict[str, Any]] = {}
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Keep going with no saved effects rather than refusing to start:
            # this file's job is remembering patterns, not gating the server.
            self.last_error = f"could not read {self.path}: {exc}"
            return
        effects = raw.get("effects") if isinstance(raw, dict) else None
        if not isinstance(effects, dict):
            self.last_error = f"{self.path} has no 'effects' object"
            return
        for name, entry in effects.items():
            if not isinstance(entry, dict):
                continue
            frames, why = validate_frames(entry.get("frames"))
            if why is not None:
                continue  # a hand-edited or stale file; skip rather than refuse the rest
            self._effects[str(name)[:MAX_EFFECT_NAME]] = {
                "frames": frames,
                "saved_at": entry.get("saved_at"),
                "saved_by": str(entry.get("saved_by") or "")[:64],
            }

    def _flush(self) -> None:
        payload = {"version": 1, "effects": self._effects}
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
                    "frames": entry["frames"],
                    "count": len(entry["frames"]),
                }
                for name, entry in sorted(self._effects.items())
            ]

    def get(self, name: str) -> list[dict[str, Any]] | None:
        with self._lock:
            entry = self._effects.get(str(name)[:MAX_EFFECT_NAME])
            return list(entry["frames"]) if entry else None

    def save(
        self, name: str, frames: Any, by: str = ""
    ) -> tuple[dict[str, Any] | None, str | None]:
        clean_name = " ".join(str(name or "").split())[:MAX_EFFECT_NAME]
        if not clean_name:
            return None, "an effect needs a name"
        cleaned, why = validate_frames(frames)
        if why is not None:
            return None, why
        with self._lock:
            if clean_name not in self._effects and len(self._effects) >= MAX_EFFECTS:
                return None, f"at most {MAX_EFFECTS} saved effects; delete one first"
            self._effects[clean_name] = {
                "frames": cleaned,
                "saved_at": time.time(),
                "saved_by": str(by)[:64],
            }
            try:
                self._flush()
            except OSError as exc:
                del self._effects[clean_name]
                return None, f"could not write {self.path}: {exc}"
            return {"name": clean_name, "count": len(cleaned)}, None

    def delete(self, name: str) -> tuple[bool, str | None]:
        key = str(name or "")[:MAX_EFFECT_NAME]
        with self._lock:
            if key not in self._effects:
                return False, f"no effect called '{key}'"
            removed = self._effects.pop(key)
            try:
                self._flush()
            except OSError as exc:
                self._effects[key] = removed
                return False, f"could not write {self.path}: {exc}"
            return True, None
