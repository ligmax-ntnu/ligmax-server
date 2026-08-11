"""Full-resolution stills: an operator asks, the Jetson answers, this keeps them.

    browser  POST /api/camera/capture          "take one, both cameras"
                    |
                    v  a flag on a poll the Jetson already makes
    jetson   GET  /api/camera/config           -> {"capture": {"id": 7, ...}}
             POST /api/camera/capture/upload   full-res JPEG, one per camera
                    |
                    v
    disk     stills/20260811-143205-cam0.jpg  + .json sidecar

Three things about this are deliberate and none of them is the obvious choice.

**It is a request the vessel collects, not a command sent to it.** Nothing on
shore can open a connection to `ligmax-json.local` — the boat is behind a 4G
carrier NAT and every link in this fleet is outbound from the vessel. The Jetson
already polls `/api/camera/config` every five seconds
(`ligmax-edge/cloud_camera.py`), so a capture is one more field on the reply to a
request that was happening anyway. The cost is latency: pressing the button and
getting a picture is a poll period plus however long a couple of megabytes take
to climb a 4G uplink, so **seconds, not milliseconds**. The UI says so rather
than pretending otherwise.

**It goes on disk, not into the RAM relay.** `camera.py` holds exactly one frame
per camera and overwrites it, which is right for a live view and useless for
this: a full-resolution OV5647 frame is 2592x1944 and lands 1.5-3 MB at q92,
five times `camera.MAX_FRAME_BYTES`, and the whole point is that it is still
there tomorrow. This is the third thing in `ligmax-server` that writes to disk
after `tuning.ProfileStore` and `lights_effects.EffectStore`, and the first that
writes anything big — `trips.TripStore` is the shape it follows.

**The frame is the whole sensor, uncropped.** The detector runs on a 2:1 band
(`sender.py --crop-w`, 2048x1024 by default) swung 15 degrees off each lens's
axis, and that band is not where the AR tags are: the cameras look port and
starboard (`ligmax-edge/rig.json`, yaw -/+75), so anything ahead of the bow sits
at about 75 degrees off both optical axes, near the edge of the 88 degree
calibrated cone. Cropping would throw away exactly the pixels a docking run
needs. The calibration in `calibrate/calib/cam*.json` is fitted to the full
2592x1944 frame as well, so a full-frame still is the one image an ArUco pose can
be measured on without re-deriving a principal point.

**One press captures both cameras at the same instant**, and that matters for
more than tidiness. The two are not a stereo pair, but their fields do meet
across the bow with about 13 degrees of overlap, and a marker visible in *both*
frames is the only cross-check that exists on the hand-measured mount yaws in
`rig.json` — `rig.json` itself says the cameras "do not overlap enough to
register against each other (measured: no correlation peak)", which was true for
scene features and is not true for a coded marker with a known id. That check is
only worth anything if the two frames are from the same moment, so they share a
request id and a group name.

**Each still is filed with what the boat was doing**, and that is added on this
side rather than by the Jetson - which does not know. Attitude and position come
off the Pixhawk to the Pi and reach shore on the telemetry link; the picture comes
off the Jetson on this one. This server is the first place the two meet.
`server._vessel_state_for` does the stapling and records the gap: it is the
*nearest* telemetry frame, not a synchronised one, measured across two clocks one
of which has no RTC. Worth having anyway, because a fix type and a position are
what make a photograph checkable rather than merely viewable - shoot a tag, move a
measured distance on RTK, shoot it again, and the ranges you compute from the two
have to differ by what the baseline says.

Everything here is keyed by file name and indexed in memory at start-up, so a
directory somebody has copied stills into by hand is read the same as one this
wrote.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

# A full sensor frame at q92 is 1.5-3 MB. This is well clear of that and no
# further: the limit is here to stop a mis-wired sender filling the disk, not to
# size the normal case.
MAX_STILL_BYTES = 16 * 1024 * 1024

# What the ground station will hold before it starts refusing. The box has
# ~200 GB free (see config.trip_store's note), so these are generous; the point
# is that a stuck sender cannot fill a disk the dashboard is also running from.
MAX_STILLS = 500
MAX_STORE_BYTES = 6 * 1024 * 1024 * 1024

# JPEG quality the Jetson is asked for. High, because these are measurement
# images: an 18 cm ArUco tag at 6 m is about 25 px across, and JPEG ringing on a
# 25 px marker is the difference between a corner refined to a third of a pixel
# and a corner refined to two.
DEFAULT_QUALITY = 92
QUALITY_RANGE = (60, 98)

# How long a request stays outstanding. The Jetson polls every 5 s
# (cloud_camera.POLL_PERIOD), so this is generous. What it guards against is a
# request sitting in the queue for the rest of the session and then firing the
# moment somebody starts the Jetson two hours later, handing back a picture of
# somewhere else entirely.
REQUEST_TIMEOUT_S = 180.0

MAX_NOTE = 120
# File names this will serve. Anchored and explicit rather than a blacklist,
# because this is the one place a browser-supplied string becomes a path.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _clamp_quality(value: Any) -> int:
    low, high = QUALITY_RANGE
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return DEFAULT_QUALITY


class StillStore:
    """Outstanding capture request, plus every still already on disk."""

    def __init__(self, path: str | Path) -> None:
        self.root = Path(path)
        self._lock = threading.Lock()
        # name -> sidecar dict. Built once from the directory, then kept in step
        # by accept()/delete(), so listing does not stat 500 files per poll.
        self._index: dict[str, dict[str, Any]] = {}
        self._request: dict[str, Any] | None = None
        self._seq = 0
        self._received = 0
        self._rejected = 0
        self._last_error: str | None = None
        self.last_error: str | None = None
        self._scan()

    # -- disk ---------------------------------------------------------------

    def _scan(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Not fatal. A dashboard that cannot store stills should still show
            # telemetry, so this is a warning on the panel rather than a refusal
            # to start - the same call `trips.TripStore` makes.
            self.last_error = f"could not create {self.root}: {exc}"
            return
        try:
            entries = sorted(self.root.glob("*.jpg"))
        except OSError as exc:
            self.last_error = f"could not read {self.root}: {exc}"
            return
        for image in entries:
            self._index[image.name] = self._read_meta(image)

    def _read_meta(self, image: Path) -> dict[str, Any]:
        """The sidecar for `image`, or what can be worked out without one.

        A missing or broken sidecar is not an error: the picture is the artefact
        and the metadata is a convenience, so a still copied in by hand still
        lists, downloads and deletes.
        """
        meta: dict[str, Any] = {}
        sidecar = image.with_suffix(".json")
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            meta = {}
        try:
            stat = image.stat()
        except OSError:
            stat = None
        meta.setdefault("name", image.name)
        meta.setdefault("stored_at", stat.st_mtime if stat else None)
        meta["bytes"] = stat.st_size if stat else meta.get("bytes")
        return meta

    def _held_bytes(self) -> int:
        return sum(int(entry.get("bytes") or 0) for entry in self._index.values())

    # -- the request --------------------------------------------------------

    def request(
        self,
        cameras: Any = None,
        quality: Any = None,
        note: str = "",
        by: str = "operator",
    ) -> dict[str, Any]:
        """Ask the vessel for one full-resolution frame per camera.

        A second request supersedes the first rather than queueing behind it.
        Two megabytes each up a 4G link is slow enough that an operator pressing
        the button twice means "I want one now", not "I want two".
        """
        wanted = [str(c)[:8] for c in (cameras or ("0", "1"))][:4] or ["0", "1"]
        with self._lock:
            self._seq += 1
            self._request = {
                "id": self._seq,
                "cameras": wanted,
                "quality": _clamp_quality(quality),
                "note": " ".join(str(note or "").split())[:MAX_NOTE],
                "requested_at": time.time(),
                "requested_by": str(by)[:64],
                # Group name is stamped now, at the request, so both cameras'
                # files sort together and carry the instant an operator asked
                # rather than the instant each upload happened to land.
                "group": self._group_name(),
                "received": [],
            }
            return self._describe_request(self._request)

    def _group_name(self) -> str:
        """A UTC stamp both cameras' files share, unique in the index."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base, suffix = stamp, 1
        while any(name.startswith(f"{stamp}-") for name in self._index):
            suffix += 1
            stamp = f"{base}-{suffix}"
        return stamp

    def cancel(self) -> None:
        with self._lock:
            self._request = None

    def pending(self) -> dict[str, Any] | None:
        """What the Jetson should grab, or None. Rides on the config poll.

        Cameras already uploaded are dropped from the list, so a second camera
        that is slow does not make the first one send twice.
        """
        with self._lock:
            request = self._request
            if request is None:
                return None
            if time.time() - request["requested_at"] > REQUEST_TIMEOUT_S:
                self._request = None
                self._last_error = (
                    f"capture {request['id']} expired after "
                    f"{REQUEST_TIMEOUT_S:.0f} s with "
                    f"{len(request['received'])}/{len(request['cameras'])} frames"
                )
                return None
            outstanding = [
                camera
                for camera in request["cameras"]
                if camera not in request["received"]
            ]
            if not outstanding:
                self._request = None
                return None
            return {
                "id": request["id"],
                "cameras": outstanding,
                "quality": request["quality"],
            }

    @staticmethod
    def _describe_request(request: dict[str, Any] | None) -> dict[str, Any] | None:
        if request is None:
            return None
        return {
            "id": request["id"],
            "cameras": list(request["cameras"]),
            "received": list(request["received"]),
            "quality": request["quality"],
            "note": request["note"],
            "requested_at": request["requested_at"],
            "requested_by": request["requested_by"],
            "age": round(time.time() - request["requested_at"], 1),
        }

    # -- ingest -------------------------------------------------------------

    def accept(
        self, camera: str, request_id: Any, data: bytes, meta: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Store one full-resolution JPEG. `(info, None)` or `(None, why)`."""
        camera = str(camera or "0")[:8]
        if not data:
            self._rejected += 1
            return None, "empty body"
        if len(data) > MAX_STILL_BYTES:
            self._rejected += 1
            return None, f"{len(data)} B is over the {MAX_STILL_BYTES} B limit"
        if not data.startswith(b"\xff\xd8\xff"):
            self._rejected += 1
            return None, "body is not a JPEG"

        try:
            wanted_id = int(request_id)
        except (TypeError, ValueError):
            wanted_id = 0

        with self._lock:
            request = self._request
            # An upload for a request that has already been superseded or has
            # expired is still worth keeping - the picture exists and somebody
            # asked for it - but it is filed under its own stamp rather than
            # claiming a group it does not belong to.
            matched = request is not None and wanted_id == request["id"]
            group = request["group"] if matched else self._group_name()
            note = request["note"] if matched else ""
            by = request["requested_by"] if matched else ""

            if len(self._index) >= MAX_STILLS:
                self._rejected += 1
                return None, (
                    f"already holding {MAX_STILLS} stills; delete some first"
                )
            if self._held_bytes() + len(data) > MAX_STORE_BYTES:
                self._rejected += 1
                return None, (
                    f"the store is at its {MAX_STORE_BYTES // (1 << 30)} GB "
                    f"limit; delete some first"
                )

            name = f"{group}-cam{camera}.jpg"
            image = self.root / name
            record: dict[str, Any] = {
                "name": name,
                "group": group,
                "camera": camera,
                "request_id": wanted_id,
                "note": note,
                "requested_by": by,
                "stored_at": time.time(),
                "bytes": len(data),
                # Whatever the Jetson said about the frame. Passed through
                # rather than filtered: this is the record of how the picture
                # was made, and the next person to fit a marker pose to it
                # needs the sensor mode, the rotation and the calibration name
                # more than this module needs an opinion about them.
                **{k: v for k, v in meta.items() if k not in ("name", "bytes")},
            }
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                # Write to a temporary and rename, so a browser listing the
                # directory can never open a half-written JPEG.
                temporary = image.with_suffix(".jpg.part")
                temporary.write_bytes(data)
                temporary.replace(image)
                image.with_suffix(".json").write_text(
                    json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
                )
            except OSError as exc:
                self._rejected += 1
                self._last_error = f"could not write {image}: {exc}"
                return None, self._last_error

            self._index[name] = record
            self._received += 1
            self._last_error = None
            if matched:
                request["received"].append(camera)
                if len(request["received"]) >= len(request["cameras"]):
                    self._request = None
            return dict(record), None

    # -- read paths ---------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """Newest first, which is the order somebody who just pressed the
        button wants to see."""
        with self._lock:
            entries = list(self._index.values())
        return sorted(
            (dict(entry) for entry in entries),
            key=lambda entry: str(entry.get("name") or ""),
            reverse=True,
        )

    def path_for(self, name: str) -> Path | None:
        """The file for `name`, or None if it is not one of ours.

        The name is matched against the index rather than only pattern-checked:
        a path this never wrote is not served whatever it looks like.
        """
        if not _NAME_RE.match(str(name or "")):
            return None
        with self._lock:
            if name not in self._index:
                return None
        path = self.root / name
        return path if path.is_file() else None

    def delete(self, name: str) -> tuple[bool, str | None]:
        if not _NAME_RE.match(str(name or "")):
            return False, "not a still this server holds"
        with self._lock:
            if name not in self._index:
                return False, f"no still called '{name}'"
            image = self.root / name
            try:
                image.unlink(missing_ok=True)
                image.with_suffix(".json").unlink(missing_ok=True)
            except OSError as exc:
                return False, f"could not delete {image}: {exc}"
            self._index.pop(name, None)
            return True, None

    def state(self) -> dict[str, Any]:
        """Everything the capture panel needs except the pixels."""
        with self._lock:
            request = self._describe_request(self._request)
            held, size = len(self._index), self._held_bytes()
            received, rejected = self._received, self._rejected
            error = self._last_error
        return {
            "pending": request,
            "held": held,
            "bytes_held": size,
            "received": received,
            "rejected": rejected,
            "last_error": error or self.last_error,
            "limits": {
                "max_stills": MAX_STILLS,
                "max_bytes": MAX_STORE_BYTES,
                "max_still_bytes": MAX_STILL_BYTES,
                "quality": list(QUALITY_RANGE),
                "timeout_s": REQUEST_TIMEOUT_S,
            },
            "stills": self.list(),
        }
