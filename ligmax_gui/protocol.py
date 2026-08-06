"""Wire format between the vessel and the ground-station dashboard.

Everything the boat sends is one JSON object ("frame").  Every field is
optional except that a frame must contain *something*; the server merges each
frame into the live snapshot, so the boat can push a full state at 10 Hz or
dribble in partial updates from different subsystems independently.

    {
      "seq": 1041,
      "t": 1769552134.482,                    # unix seconds, boat clock
      "status": "AUTONOMOUS",                 # who is in charge - see VESSEL_STATUS
      "mode": "AUTO",                         # the autopilot's own mode name
      "estop": false,
      "available_modes": ["MANUAL", "AUTONOMOUS", "HOLD", "DOCKING"],

      "origin": {"lat": 63.43049, "lon": 10.39506},   # GPS of grid (0, 0)
      "grid_bearing": 0.0,                    # compass bearing of +y, degrees
      "upstream_direction": [0.0, 1.0],       # unit vector, grid coords

      "boat": {                               # null clears it from the chart
        "position": [12.4, 38.1],             # metres, grid coords
        "velocity": [0.2, 1.8],               # m/s
        "heading": [0.1, 0.99],               # unit vector (or heading_deg)
        "radius": 1.1                         # own safety radius, metres
      },

      "tracks": [
        {"track_id": 7, "position": [20.0, 55.0], "type": 1,
         "confidence": 0.93, "avoid_radius": 3.0}
      ],

      "path": {"points": [[12, 38], [15, 46]], "target_index": 1},
      "scan": {"points": [[1.2, 4.5], ...], "source": "front_lidar"},

      "telemetry": {"battery": {...}, "gimbal": {...}, ...},
      "logs": [{"t": ..., "level": "INFO", "name": "planner", "msg": "..."}]
    }

Coordinate convention
---------------------
Grid coordinates are metres, right-handed, **+x = east, +y = north** by
default.  If your grid is rotated relative to true north, send
``grid_bearing`` (the compass bearing that +y points along) and the map
underlay will be rotated to match.  ``origin`` georeferences grid (0, 0) —
that is ``Boat.original_gps_position`` — and is what lets the dashboard put
the vessel on a real map.

``origin`` and ``boat.position`` are the two fields the chart is drawn from, and
they are **not** interchangeable with ``telemetry.gps.lat/lon``: the map works in
metres and nothing on the server converts degrees into them.  The vessel owns
that conversion (``ligmax-pi/nodes/io_manager/navigation.py``), because it is the
vessel that decides where its grid is zeroed.

Reply
-----
Both ingest transports reply with any queued operator commands, so the boat
never needs to poll a second endpoint:

    {"commands": [{"id": "c-8", "name": "set_mode", "args": {"mode": "HOLD"},
                   "issued_at": 1769552140.1}]}
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# --- ObstacleType -----------------------------------------------------------
#
# The vessel's `shared_settings` is authoritative.  We import it when we can,
# but keep a literal mirror so the dashboard also runs standalone (e.g. on a
# ground-station laptop that has no numpy).  `check_shared_settings_sync()`
# fails loudly if the two ever drift apart, which is the whole point of having
# the mirror at all.

_MIRRORED_OBSTACLE_TYPES: dict[str, int] = {
    "UNKNOWN": 0,
    "RED": 1,
    "GREEN": 2,
    "NORTH": 3,
    "SOUTH": 4,
    "WEST": 5,
    "EAST": 6,
    "BOAT": 7,
    "LAND": 8,
    "DOCKING_CENTER": 9,
}

_MIRRORED_WRONG_SIDE_LENGTH = 20.0

try:  # pragma: no cover - depends on the environment
    import shared_settings as _shared

    OBSTACLE_TYPES: dict[str, int] = {m.name: m.value for m in _shared.ObstacleType}
    WRONG_SIDE_LENGTH: float = float(_shared.OBSTICAL_WRONG_SIDE_LENGTH)
    SHARED_SETTINGS_AVAILABLE = True
except Exception:  # numpy missing, file moved, whatever - degrade gracefully
    _shared = None
    OBSTACLE_TYPES = dict(_MIRRORED_OBSTACLE_TYPES)
    WRONG_SIDE_LENGTH = _MIRRORED_WRONG_SIDE_LENGTH
    SHARED_SETTINGS_AVAILABLE = False

OBSTACLE_NAMES: dict[int, str] = {v: k for k, v in OBSTACLE_TYPES.items()}


# --- Vessel status ----------------------------------------------------------
#
# `mode` is whatever the autopilot calls itself, which is useful and completely
# unstandardised - ArduPilot says "AUTO", "MANUAL", "HOLD", and a planner might
# say "docking".  `status` is the separate, closed question the Njord rules ask:
# *who is in charge of this boat right now.*  Five answers, and the vessel is
# always in exactly one of them.
#
# It is a closed vocabulary because two things downstream are switched off it and
# neither may ever be ambiguous: the operator's status indicator, and the colour
# of the lights on the hull.  The mapping is decided once, on the vessel, in
# `ligmax-pi/nodes/io_manager/status.py`, so the hull and the dashboard cannot
# disagree - the light colour rides up as `telemetry.lights` and the dashboard
# shows it next to the status it expected, which is how a mismatch gets noticed.
#
#   KILLED          kill switch pulled, propulsion power cut          RED
#   REMOTE          a human is steering, from RC or the shore client  YELLOW
#   AUTONOMOUS      running on its own navigation                     GREEN
#   STANDBY         powered, links up, deliberately not driving       breathing white
#   OUT_OF_CONTROL  nobody is steering it and it is not stopped       red strobe
#
# OUT_OF_CONTROL is the one that earns its place. The other four are states you
# chose; this is the state you discover, and a boat that cannot say it out loud
# just shows its last good status forever.
VESSEL_STATUS = (
    "AUTONOMOUS",
    "REMOTE",
    "STANDBY",
    "OUT_OF_CONTROL",
    "KILLED",
)

_STATUS_ALIASES = {
    "AUTO": "AUTONOMOUS",
    "AUTONOMY": "AUTONOMOUS",
    "SELF_DRIVING": "AUTONOMOUS",
    "MANUAL": "REMOTE",
    "RC": "REMOTE",
    "REMOTE_CONTROL": "REMOTE",
    "TELEOP": "REMOTE",
    "IDLE": "STANDBY",
    "HOLD": "STANDBY",
    "READY": "STANDBY",
    "ESTOP": "KILLED",
    "E_STOP": "KILLED",
    "KILL": "KILLED",
    "STOPPED": "KILLED",
    "LOST": "OUT_OF_CONTROL",
    "RUNAWAY": "OUT_OF_CONTROL",
    "UNKNOWN": "OUT_OF_CONTROL",
}


def normalise_status(value: Any) -> str | None:
    """Coerce a status name into `VESSEL_STATUS`, or None if it is unreadable.

    Aliases are accepted so a node that has only ever known the word "MANUAL"
    still lands in the right bucket.  Anything genuinely unrecognised returns
    None rather than a guess: the dashboard then falls back to deriving the
    status itself, which is more honest than showing a made-up one.
    """
    if value is None:
        return None
    name = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if not name:
        return None
    if name in VESSEL_STATUS:
        return name
    return _STATUS_ALIASES.get(name)


def check_shared_settings_sync() -> list[str]:
    """Return human-readable warnings if the mirror has drifted.

    Called at server startup.  An empty list means the dashboard and the
    vessel agree on every obstacle type and on the wrong-side length.
    """
    if not SHARED_SETTINGS_AVAILABLE:
        return [
            "shared_settings.py could not be imported - using the built-in "
            "mirror of ObstacleType. Obstacle names may be out of date."
        ]

    warnings: list[str] = []
    if OBSTACLE_TYPES != _MIRRORED_OBSTACLE_TYPES:
        added = set(OBSTACLE_TYPES) - set(_MIRRORED_OBSTACLE_TYPES)
        removed = set(_MIRRORED_OBSTACLE_TYPES) - set(OBSTACLE_TYPES)
        changed = {
            k
            for k in set(OBSTACLE_TYPES) & set(_MIRRORED_OBSTACLE_TYPES)
            if OBSTACLE_TYPES[k] != _MIRRORED_OBSTACLE_TYPES[k]
        }
        warnings.append(
            "ObstacleType in shared_settings.py differs from the mirror in "
            f"ligmax_gui/protocol.py (added={sorted(added)}, "
            f"removed={sorted(removed)}, renumbered={sorted(changed)}). "
            "Update _MIRRORED_OBSTACLE_TYPES and the colour table in "
            "web/js/obstacles.js."
        )
    if abs(WRONG_SIDE_LENGTH - _MIRRORED_WRONG_SIDE_LENGTH) > 1e-9:
        warnings.append(
            f"OBSTICAL_WRONG_SIDE_LENGTH is {WRONG_SIDE_LENGTH} m in "
            f"shared_settings.py but the mirror says "
            f"{_MIRRORED_WRONG_SIDE_LENGTH} m. The live value is being used."
        )
    return warnings


# --- No-go geometry ---------------------------------------------------------
#
# Two things make a region undrivable:
#
#   1. `avoid_radius` - a disc around the object.
#   2. the "wrong side" - a corridor of length OBSTICAL_WRONG_SIDE_LENGTH
#      extending from the object in the direction you must not pass.
#
# The direction depends on the mark (IALA region A, which is what Njord uses):
#
#   RED    lateral, keep to port going upstream -> the channel is to starboard
#          of the buoy, so the no-go corridor points to *port* of upstream.
#   GREEN  mirror image of RED.
#   NORTH  a north cardinal is passed on its north side, so the corridor
#          points south.  Likewise for SOUTH / EAST / WEST.
#   BOAT   you may not cut across its bow: the corridor follows its own
#          heading (or velocity, if that is all we have).
#   LAND / DOCKING_CENTER / UNKNOWN
#          radius only, no directional corridor.
#
# The frontend re-implements this in web/js/nogo.js so it can draw the zones
# without a round trip.  Keep the two in step - or better, have the planner
# send an explicit `no_go` override per track and the dashboard will draw
# exactly what the planner believes:
#
#     {"track_id": 7, ..., "no_go": {"dir": [1, 0], "length": 20}}
#     {"track_id": 8, ..., "no_go": {"polygon": [[x, y], ...]}}


def _unit(vec: Iterable[float] | None) -> tuple[float, float] | None:
    if vec is None:
        return None
    try:
        x, y = float(vec[0]), float(vec[1])  # type: ignore[index]
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    norm = math.hypot(x, y)
    if norm < 1e-9 or not math.isfinite(norm):
        return None
    return (x / norm, y / norm)


def wrong_side_direction(
    obstacle_type: int | str,
    upstream_direction: Iterable[float] | None = (0.0, 1.0),
    track_heading: Iterable[float] | None = None,
) -> tuple[float, float] | None:
    """Unit vector the no-go corridor extends along, or None if there isn't one.

    ``upstream_direction`` matters only for the lateral marks (RED/GREEN);
    ``track_heading`` only for other vessels.
    """
    name = (
        OBSTACLE_NAMES.get(int(obstacle_type), "UNKNOWN")
        if not isinstance(obstacle_type, str)
        else obstacle_type.upper()
    )

    if name in ("RED", "GREEN"):
        upstream = _unit(upstream_direction) or (0.0, 1.0)
        ux, uy = upstream
        # Rotate +90 deg (counter-clockwise) to get "port side of upstream".
        port = (-uy, ux)
        return port if name == "RED" else (-port[0], -port[1])

    if name == "NORTH":
        return (0.0, -1.0)
    if name == "SOUTH":
        return (0.0, 1.0)
    if name == "EAST":
        return (-1.0, 0.0)
    if name == "WEST":
        return (1.0, 0.0)

    if name == "BOAT":
        return _unit(track_heading)

    return None


# --- Frame helpers ----------------------------------------------------------


def _num(value: Any) -> float | None:
    """Coerce to a JSON-safe float, rejecting NaN/inf (which break JSON)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _point(value: Any) -> list[float] | None:
    """Accept [x, y], (x, y), numpy arrays, or {"x": .., "y": ..}."""
    if value is None:
        return None
    if isinstance(value, dict):
        x, y = _num(value.get("x")), _num(value.get("y"))
        return [x, y] if x is not None and y is not None else None
    if hasattr(value, "tolist"):  # numpy array / scalar
        value = value.tolist()
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = _num(value[0]), _num(value[1])
        return [x, y] if x is not None and y is not None else None
    return None


def _points(value: Any, limit: int | None = None) -> list[list[float]]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        return []
    out: list[list[float]] = []
    step = 1
    if limit and len(value) > limit:
        # Even decimation keeps the shape of a lidar scan recognisable.
        step = math.ceil(len(value) / limit)
    for item in value[::step]:
        pt = _point(item)
        if pt is not None:
            out.append(pt)
    return out


def normalise_track(raw: Any, index: int = 0) -> dict[str, Any] | None:
    """Coerce one detection into the canonical track shape.

    Tolerant on input (numpy arrays, string type names, missing confidence)
    and strict on output, so the frontend never has to guess.
    """
    if not isinstance(raw, dict):
        return None

    position = _point(raw.get("position"))
    if position is None:
        return None

    raw_type = raw.get("type", raw.get("obstacle_type", 0))
    if isinstance(raw_type, str):
        type_value = OBSTACLE_TYPES.get(raw_type.strip().upper(), 0)
    elif hasattr(raw_type, "value"):  # an ObstacleType enum member
        type_value = int(raw_type.value)
    else:
        type_value = int(_num(raw_type) or 0)

    track: dict[str, Any] = {
        "track_id": raw.get("track_id", raw.get("id", index)),
        "position": position,
        "type": type_value,
        "type_name": OBSTACLE_NAMES.get(type_value, f"TYPE_{type_value}"),
        "confidence": max(0.0, min(1.0, _num(raw.get("confidence")) or 0.0)),
        "avoid_radius": max(0.0, _num(raw.get("avoid_radius")) or 0.0),
    }

    heading = _unit(_point(raw.get("heading")))
    velocity = _point(raw.get("velocity"))
    if heading is None and velocity is not None:
        heading = _unit(velocity)
    if heading is not None:
        track["heading"] = list(heading)
    if velocity is not None:
        track["velocity"] = velocity

    for optional in ("age", "hits", "misses", "source", "label", "radius"):
        if optional in raw:
            value = raw[optional]
            track[optional] = _num(value) if optional != "source" else str(value)

    # Explicit planner override wins over the rule above.
    no_go = raw.get("no_go")
    if isinstance(no_go, dict):
        out: dict[str, Any] = {}
        direction = _unit(_point(no_go.get("dir")))
        if direction is not None:
            out["dir"] = list(direction)
        length = _num(no_go.get("length"))
        if length is not None:
            out["length"] = length
        polygon = _points(no_go.get("polygon"))
        if len(polygon) >= 3:
            out["polygon"] = polygon
        if out:
            track["no_go"] = out

    return track


def _normalise_path(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):  # bare list of points
        raw = {"points": raw}
    if not isinstance(raw, dict):
        return None
    points = _points(raw.get("points"))
    if not points:
        return None
    path: dict[str, Any] = {
        "points": points,
        "kind": str(raw.get("kind", "planned")),
    }
    if raw.get("label") is not None:
        path["label"] = str(raw["label"])
    target = _num(raw.get("target_index"))
    if target is not None:
        path["target_index"] = int(target)
    cost = _num(raw.get("cost"))
    if cost is not None:
        path["cost"] = cost
    return path


def normalise_frame(raw: dict[str, Any], max_scan_points: int = 1500) -> dict[str, Any]:
    """Validate and canonicalise an inbound frame.

    Unknown keys are dropped rather than forwarded, except inside
    ``telemetry`` where arbitrary nested values are the whole point — the
    dashboard renders fields it has never seen before automatically.
    """
    frame: dict[str, Any] = {}

    if (seq := _num(raw.get("seq"))) is not None:
        frame["seq"] = int(seq)
    if (t := _num(raw.get("t", raw.get("time", raw.get("timestamp"))))) is not None:
        frame["t"] = t

    if raw.get("mode") is not None:
        frame["mode"] = str(raw["mode"])
    # An unrecognised status is dropped, not passed through: `status` drives the
    # operator's indicator and the hull lights, and a value neither end knows the
    # meaning of is worse than no value at all (the dashboard derives one).
    if (status := normalise_status(raw.get("status"))) is not None:
        frame["status"] = status
    if "estop" in raw or "e_stop" in raw:
        frame["estop"] = bool(raw.get("estop", raw.get("e_stop")))
    if isinstance(raw.get("available_modes"), (list, tuple)):
        frame["available_modes"] = [str(m) for m in raw["available_modes"]][:24]
    if raw.get("mission") is not None:
        frame["mission"] = str(raw["mission"])
    if raw.get("status_text") is not None:
        frame["status_text"] = str(raw["status_text"])[:400]

    origin = raw.get("origin")
    if isinstance(origin, dict):
        lat, lon = _num(origin.get("lat")), _num(origin.get("lon"))
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            frame["origin"] = {"lat": lat, "lon": lon}
    elif (pt := _point(origin)) is not None and abs(pt[0]) <= 90:
        # Tolerate `original_gps_position` as a bare [lat, lon] array.
        frame["origin"] = {"lat": pt[0], "lon": pt[1]}

    if (bearing := _num(raw.get("grid_bearing"))) is not None:
        frame["grid_bearing"] = bearing % 360.0
    if (upstream := _unit(_point(raw.get("upstream_direction")))) is not None:
        frame["upstream_direction"] = list(upstream)

    boat = raw.get("boat")
    # An explicit null clears the vessel from the chart. Frames merge, so a
    # vessel that has lost its fix cannot un-say a position by omitting the key -
    # and a boat drawn where it was thirty seconds ago looks correct, which is
    # worse than an empty chart. `ligmax-pi`'s navigation.world() sends this.
    if "boat" in raw and boat is None:
        frame["boat"] = None
    elif isinstance(boat, dict):
        out: dict[str, Any] = {}
        for key in ("position", "velocity", "heading"):
            if (pt := _point(boat.get(key))) is not None:
                out[key] = pt
        if "heading" in out and (unit := _unit(out["heading"])) is not None:
            out["heading"] = list(unit)
        elif (heading_deg := _num(boat.get("heading_deg"))) is not None:
            rad = math.radians(90.0 - heading_deg)  # compass -> grid vector
            out["heading"] = [math.cos(rad), math.sin(rad)]
        if (radius := _num(boat.get("radius"))) is not None:
            out["radius"] = max(0.0, radius)
        if out:
            frame["boat"] = out

    if isinstance(raw.get("tracks"), (list, tuple)):
        tracks = [
            track
            for index, item in enumerate(raw["tracks"][:600])
            if (track := normalise_track(item, index)) is not None
        ]
        frame["tracks"] = tracks

    paths: list[dict[str, Any]] = []
    if (primary := _normalise_path(raw.get("path"))) is not None:
        paths.append(primary)
    if isinstance(raw.get("paths"), (list, tuple)):
        for item in raw["paths"][:8]:
            if (path := _normalise_path(item)) is not None:
                paths.append(path)
    if paths or "path" in raw or "paths" in raw:
        frame["paths"] = paths

    scan = raw.get("scan")
    if scan is not None:
        if isinstance(scan, (list, tuple)):
            scan = {"points": scan}
        if isinstance(scan, dict):
            points = _points(scan.get("points"), limit=max_scan_points)
            frame["scan"] = {
                "points": points,
                "source": str(scan.get("source", "lidar")),
            }

    if isinstance(raw.get("telemetry"), dict):
        frame["telemetry"] = _sanitise_telemetry(raw["telemetry"])

    logs = raw.get("logs", raw.get("log"))
    if logs is not None:
        if isinstance(logs, (str, dict)):
            logs = [logs]
        frame["logs"] = [
            entry
            for item in list(logs)[:500]
            if (entry := normalise_log(item)) is not None
        ]

    return frame


_TELEMETRY_MAX_DEPTH = 4


def _sanitise_telemetry(value: Any, depth: int = 0) -> Any:
    """Keep nested dicts/lists of JSON-safe scalars, drop everything else.

    Telemetry is intentionally schema-free so the boat can add a field and
    have it show up on the dashboard with no GUI change.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value[:500] if isinstance(value, str) else value
    if isinstance(value, (int, float)):
        return _num(value)
    if hasattr(value, "tolist"):
        return _sanitise_telemetry(value.tolist(), depth)
    if isinstance(value, dict):
        if depth >= _TELEMETRY_MAX_DEPTH:
            return None
        return {
            str(k)[:64]: _sanitise_telemetry(v, depth + 1)
            for k, v in list(value.items())[:120]
        }
    if isinstance(value, (list, tuple)):
        if depth >= _TELEMETRY_MAX_DEPTH:
            return None
        return [_sanitise_telemetry(v, depth + 1) for v in list(value)[:120]]
    return None


LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
_LEVEL_ALIASES = {
    "WARNING": "WARN",
    "ERR": "ERROR",
    "FATAL": "CRITICAL",
    "TRACE": "DEBUG",
    "NOTSET": "DEBUG",
}


def normalise_log(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"msg": raw}
    if not isinstance(raw, dict):
        return None
    message = raw.get("msg", raw.get("message", raw.get("text")))
    if message is None:
        return None
    level = str(raw.get("level", "INFO")).strip().upper()
    level = _LEVEL_ALIASES.get(level, level)
    if level not in LOG_LEVELS:
        level = "INFO"
    entry: dict[str, Any] = {
        "level": level,
        "name": str(raw.get("name", raw.get("logger", "boat")))[:64],
        "msg": str(message)[:4000],
    }
    if (t := _num(raw.get("t", raw.get("time", raw.get("timestamp"))))) is not None:
        entry["t"] = t
    return entry
