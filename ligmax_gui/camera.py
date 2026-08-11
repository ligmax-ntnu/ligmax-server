"""Camera frame relay: the Jetson pushes JPEGs here, browsers pull them.

Why this exists at all, given that `ligmax-edge` already has a perfectly good
viewer: that viewer needs a TCP connection to the Jetson, and from the water
there is none — the vessel is behind a 4G carrier NAT and nothing can reach it.
Everything the operator sees has to arrive as an *outbound* push from the boat,
which is what `/api/ingest` already does for telemetry and what this does for
pictures.

    ligmax-json.local  --POST /api/camera--> here --GET /api/camera/0.jpg--> browser

The split of duties is deliberate, and it is not the obvious one:

  * **Only the JPEG comes here.** The Jetson's detections and its front lidar go
    to `ligmax-pi3.local` instead, where the Pi merges them with the aft lidar
    and sends one fused world model up the telemetry link. So a box drawn on the
    map and the same box burned into a camera frame travel by different routes
    and arrive at different times — do not try to align them frame-by-frame.
  * **The stream defaults to OFF.** This link shares a 4G uplink with the
    telemetry the operator actually needs, and video is the one payload big
    enough to starve it. Nothing streams until somebody asks, and what they get
    is small: see DEFAULT_STREAM.

Everything here is RAM-only, like the rest of the dashboard. One frame per
camera is kept; a new frame overwrites the old one. There is no recording, and a
restart loses the picture along with the log ring.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# Two CSI cameras on the Jetson, aimed +-15 degrees apart (they are NOT a stereo
# pair - `ligmax-edge/sender.py`). A third id is accepted and simply appears as
# another tile, so a temporary camera needs no change here.
KNOWN_CAMERAS = ("0", "1")
MAX_CAMERAS = 4

# Biggest JPEG we will hold. Well above anything DEFAULT_STREAM can produce; the
# point is to refuse a full-resolution frame rather than to size the normal case.
MAX_FRAME_BYTES = 512 * 1024

# A frame older than this is not shown - a frozen picture of thirty seconds ago
# is worse than an empty panel, because it looks live.
FRAME_STALE_AFTER = 6.0

# What the Jetson is told to send when the stream is switched on. These are
# bandwidth choices, not properties of the hardware: the sensors do 2592x1944
# (docs/hardware.md) and we are asking for a postage stamp on purpose.
#
#   480x270 at q55 lands around 15-25 kB a frame, so 2 fps is roughly
#   0.3 Mbit/s of uplink. Telemetry is ~2 kB/s next to it.
#
# Raise `fps` before `max_width`: motion is what makes a camera feed useful for
# judging what the boat is doing, and detail is what costs bytes.
DEFAULT_STREAM: dict[str, Any] = {
    "enabled": False,
    "max_width": 480,
    "jpeg_quality": 55,
    "fps": 2.0,
    "cameras": list(KNOWN_CAMERAS),
    # Whether the Jetson should run the YOLO detector. The odd one out here -
    # every other field is about the picture on the uplink, and this one is about
    # inference on the vessel. It lives on this config because this config is the
    # only channel that reaches `ligmax-json.local`: the detections go to the Pi,
    # not here, so there is no other poll to hang it on.
    #
    # Defaults ON, and that default is the safe one in both directions: a fresh
    # server tells a Jetson to detect, which is how the boat races, and a Jetson
    # that never hears from the server detects too (`cloud_camera.detect`).
    #
    # Turning it off does NOT stop capture - the previews, the full-resolution
    # stills, the bearings and the lidar all keep running. It exists because
    # `sender.py` owns both CSI sensors and nothing else on that board can open
    # them, so "use the cameras as cameras" has to be a mode of the detector
    # process rather than a second process. See `stills.py`.
    "detect": True,
}

# Guard rails on what an operator can ask the Jetson for, so a slip in the UI
# cannot saturate the uplink the E-stop command has to come back through.
LIMITS = {
    "max_width": (160, 1280),
    "jpeg_quality": (25, 90),
    "fps": (0.2, 10.0),
}


def _clamp(name: str, value: float) -> float:
    low, high = LIMITS[name]
    return max(low, min(high, value))


class Frame:
    """One JPEG plus what the Jetson said about it."""

    __slots__ = ("data", "content_type", "received_at", "captured_at", "meta", "seq")

    def __init__(
        self,
        data: bytes,
        content_type: str,
        meta: dict[str, Any],
        captured_at: float | None,
        seq: int,
    ) -> None:
        self.data = data
        self.content_type = content_type
        self.meta = meta
        self.received_at = time.time()
        self.captured_at = captured_at
        self.seq = seq

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at


class CameraRelay:
    """Latest frame per camera, and the streaming config the Jetson polls for.

    Thread-safe: the Flask request threads that accept frames, the ones that
    serve them and the one that flips the config all touch this.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: dict[str, Frame] = {}
        self._stream: dict[str, Any] = dict(DEFAULT_STREAM)
        self._config_version = 1
        self._seq = 0
        self._frames_received = 0
        self._bytes_received = 0
        self._rejected = 0
        self._last_poll_at: float | None = None
        # A poll that arrived and was turned away. Kept apart from
        # `_last_poll_at` because the two produce the same empty panel and have
        # opposite causes: nothing asking (the Jetson is down) versus something
        # asking with the wrong key (the Jetson is up and misconfigured).
        self._refused = 0
        self._last_refusal_at: float | None = None
        self._last_refusal: str | None = None
        self._enabled_by: str | None = None
        self._enabled_at: float | None = None
        # Frame rate measured on arrival, per camera, over a short window.
        self._arrivals: dict[str, list[float]] = {}

    # -- ingest -------------------------------------------------------------

    def accept(
        self, camera: str, data: bytes, content_type: str, meta: dict[str, Any]
    ) -> tuple[bool, str]:
        """Store a frame. Returns `(ok, message)`; the message is for the log."""
        camera = str(camera or "0")[:8]
        if not data:
            self._rejected += 1
            return False, "empty body"
        if len(data) > MAX_FRAME_BYTES:
            self._rejected += 1
            return False, f"{len(data)} B is over the {MAX_FRAME_BYTES} B limit"
        # A JPEG starts FF D8 FF. Checking is cheap and stops a mis-wired sender
        # filling the panel with something a browser will not render.
        if not data.startswith(b"\xff\xd8\xff"):
            self._rejected += 1
            return False, "body is not a JPEG"

        captured = meta.get("t")
        try:
            captured = float(captured) if captured is not None else None
        except (TypeError, ValueError):
            captured = None

        now = time.time()
        with self._lock:
            if camera not in self._frames and len(self._frames) >= MAX_CAMERAS:
                self._rejected += 1
                return False, f"already holding {MAX_CAMERAS} cameras"
            self._seq += 1
            self._frames[camera] = Frame(
                data, content_type or "image/jpeg", meta, captured, self._seq
            )
            self._frames_received += 1
            self._bytes_received += len(data)

            arrivals = self._arrivals.setdefault(camera, [])
            arrivals.append(now)
            cutoff = now - 5.0
            while arrivals and arrivals[0] < cutoff:
                arrivals.pop(0)

        return True, f"cam{camera} {len(data)} B"

    # -- read paths ---------------------------------------------------------

    def frame(self, camera: str) -> Frame | None:
        with self._lock:
            frame = self._frames.get(str(camera))
        if frame is None or frame.age() > FRAME_STALE_AFTER:
            return None
        return frame

    def _hz(self, camera: str, now: float) -> float:
        arrivals = self._arrivals.get(camera) or []
        fresh = [t for t in arrivals if now - t < 5.0]
        if len(fresh) < 2:
            return 0.0
        span = fresh[-1] - fresh[0]
        return round((len(fresh) - 1) / span, 2) if span > 0 else 0.0

    def state(self) -> dict[str, Any]:
        """Everything the panel needs except the pixels themselves."""
        now = time.time()
        with self._lock:
            cameras = [
                {
                    "id": camera,
                    "age": round(frame.age(now), 2),
                    "live": frame.age(now) <= FRAME_STALE_AFTER,
                    "bytes": len(frame.data),
                    "hz": self._hz(camera, now),
                    "seq": frame.seq,
                    "width": frame.meta.get("width"),
                    "height": frame.meta.get("height"),
                    "label": frame.meta.get("label"),
                }
                for camera, frame in sorted(self._frames.items())
            ]
            return {
                "stream": dict(self._stream),
                "config_version": self._config_version,
                "cameras": cameras,
                "frames_received": self._frames_received,
                "bytes_received": self._bytes_received,
                "rejected": self._rejected,
                "last_poll_age": (
                    round(now - self._last_poll_at, 1)
                    if self._last_poll_at is not None
                    else None
                ),
                "refused": self._refused,
                "last_refusal": self._last_refusal,
                "last_refusal_age": (
                    round(now - self._last_refusal_at, 1)
                    if self._last_refusal_at is not None
                    else None
                ),
                "enabled_by": self._enabled_by,
                "limits": LIMITS,
            }

    # -- the config the Jetson polls ---------------------------------------

    def poll(self) -> dict[str, Any]:
        """What the Jetson should be doing. Records that it asked.

        `last_poll_age` in `state()` is how the panel distinguishes "the operator
        has not switched video on" from "the Jetson is not listening" - two things
        that look identical from a black tile.
        """
        with self._lock:
            self._last_poll_at = time.time()
            return {**self._stream, "config_version": self._config_version}

    def note_refused(self, what: str) -> None:
        """Something claiming to be the boat was turned away at the door.

        Called from the auth branches in `server.py`, before `poll()` is
        reached - which is the whole point: an unauthenticated poll never
        stamps `last_poll_at`, so without this the panel says "never asked"
        about a Jetson that is asking several times a minute with the wrong
        key. That sends whoever is debugging to the board instead of to
        `/etc/ligmax/node.env`.
        """
        with self._lock:
            self._refused += 1
            self._last_refusal_at = time.time()
            self._last_refusal = what

    def configure(self, changes: dict[str, Any], by: str = "operator") -> dict[str, Any]:
        """Apply an operator's changes and return the new config.

        Unknown keys are ignored rather than rejected, so a newer frontend cannot
        break against an older server.
        """
        with self._lock:
            stream = dict(self._stream)

            if "detect" in changes:
                # Not folded into the loop below: this one is a bool, and it is
                # deliberately independent of `enabled` - the case it exists for
                # is video off, detector off, cameras used for stills.
                stream["detect"] = bool(changes["detect"])

            if "enabled" in changes:
                enabled = bool(changes["enabled"])
                if enabled != stream["enabled"]:
                    self._enabled_by = by if enabled else None
                    self._enabled_at = time.time() if enabled else None
                    if not enabled:
                        # Drop the pictures too. Leaving the last frame up after
                        # someone switches video off reads as "still streaming".
                        self._frames.clear()
                        self._arrivals.clear()
                stream["enabled"] = enabled

            for key in ("max_width", "jpeg_quality", "fps"):
                if key not in changes:
                    continue
                try:
                    value = float(changes[key])
                except (TypeError, ValueError):
                    continue
                value = _clamp(key, value)
                stream[key] = value if key == "fps" else int(value)

            cameras = changes.get("cameras")
            if isinstance(cameras, (list, tuple)) and cameras:
                stream["cameras"] = [str(c)[:8] for c in cameras][:MAX_CAMERAS]

            if stream != self._stream:
                self._stream = stream
                self._config_version += 1
            return {**self._stream, "config_version": self._config_version}

    @property
    def enabled(self) -> bool:
        return bool(self._stream.get("enabled"))
