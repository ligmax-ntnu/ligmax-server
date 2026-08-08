"""The course, and what the boat is meant to do on each leg of it.

This is the ground-station mirror of
``ligmax-pi/nodes/self_driving/plan.py``.  The vessel's copy is the one that can
actually refuse to fly a plan; this one exists so a malformed course is a 400 the
operator reads **while typing it**, not an ack that comes back "failed" from
three hops away — the same division of labour ``tuning.py`` has with the vessel's
parameter whitelist, and for the same reason.

Why a role rides on every waypoint
----------------------------------
A Njord course is not a list of places, it is a list of places *plus what to do
between them*.  The same GPS point means "drive here and ignore everything" on
one leg and "drive here, but a red buoy on this leg must be left to port" on the
next, and no amount of coordinate says which.  Njord's own tasks are laid out
exactly that way (``ligmax-pi/njord.md``): Task 1 part 1 is blind GNSS, part 2 of
the same task adds cardinal marks, Task 2 adds a vessel that is trying to get in
the way, Task 3 is a dock.

So the operator lays the course once, each waypoint carrying its role, and the
boat swaps behaviour as it advances instead of anyone re-uploading anything
mid-run.

Keeping this in step with the vessel
------------------------------------
``ROLES`` here must carry the same names as ``plan.ROLES`` there, and the ranges
in ``_LIMITS`` the same bounds as its ``_optional_float`` calls.  Nothing checks
that automatically — different repos, never in one process — so it is written
down in ``docs/INDEX.md`` under *Things that live in two places*.  If they do
drift, the vessel wins: it refuses, and its reason is shown verbatim in the
command audit.
"""

from __future__ import annotations

import math
from typing import Any

#: How many waypoints a plan may carry.  The vessel's own limit, and its
#: reasoning: a Njord course is 8-15 intermediate points plus the numbered GPS
#: points, so anything approaching this is a paste that went wrong.
MAX_WAYPOINTS = 200

#: The longest a plan name may be, matching the vessel's `[:64]`.
MAX_NAME = 64

#: The longest a per-waypoint note may be, matching the vessel's `[:120]`.
MAX_NOTES = 120


# The six behaviours, in the order they are offered in the editor's dropdown -
# which is roughly the order a Njord course uses them.  `label` is what the
# operator picks from; `help` is what the row's title text says; `settles` marks
# the ones that are a place to *arrive at and stop*, which the vessel treats
# differently (a tighter acceptance radius, and it refuses to count them as
# passed just because the boat swept past).
ROLES: dict[str, dict[str, Any]] = {
    "transit": {
        "label": "Transit",
        "help": (
            "Drive to it on GNSS alone. Obstacles are still avoided; buoy "
            "colours are ignored. Task 1 part 1 is entirely this."
        ),
        "settles": False,
        "default_hold_s": 0.0,
    },
    "buoys": {
        "label": "Buoy rules",
        "help": (
            "Drive to it obeying the lateral marks and the cardinals: red to "
            "port and green to starboard along the direction of buoyage, and "
            "each cardinal passed on its named side. Task 1 part 2."
        ),
        "settles": False,
        "default_hold_s": 0.0,
    },
    "avoid": {
        "label": "Collision avoidance",
        "help": (
            "Drive to it watching for a vessel and giving way under COLREG - "
            "head-on and starboard-crossing turn to starboard, port-crossing "
            "stands on. Task 2, where the Otter is trying to get in the way."
        ),
        "settles": False,
        "default_hold_s": 0.0,
    },
    "hold": {
        "label": "Stop and hold",
        "help": (
            "Arrive, then station-keep. Hold time 0 means hold until told "
            "otherwise. This is the scored 'stop at GPS point 4 and stay "
            "stationary'."
        ),
        "settles": True,
        "default_hold_s": 0.0,
    },
    "dock": {
        "label": "Dock (bow-in)",
        "help": (
            "Find the berth, enter bow-first, hold, then REVERSE out. The 2 m "
            "berth of Task 3.1; the default 10 s hold is the rule's."
        ),
        "settles": True,
        "default_hold_s": 10.0,
    },
    "dock_parallel": {
        "label": "Dock (parallel)",
        "help": (
            "Come alongside, hold stationary parallel to the dock, then "
            "continue forward. Task 3.2; the default 5 s hold is the rule's."
        ),
        "settles": True,
        "default_hold_s": 5.0,
    },
}

#: Optional per-waypoint numbers, and the range the vessel will accept.  Sending
#: something outside these is refused there too, so refusing here only moves the
#: message forward in time.
_LIMITS: dict[str, tuple[float, float]] = {
    "speed": (0.05, 3.0),
    "radius": (0.3, 50.0),
    "hold_s": (0.0, 600.0),
    "berth_width_m": (0.5, 10.0),
}


def role_table() -> dict[str, dict[str, Any]]:
    """The role list for `/api/session`, so the editor renders from one source."""
    return {
        name: {
            "label": spec["label"],
            "help": spec["help"],
            "settles": spec["settles"],
            "default_hold_s": spec["default_hold_s"],
        }
        for name, spec in ROLES.items()
    }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _optional(item: dict[str, Any], field: str, position: int) -> tuple[Any, str | None]:
    """One optional numeric field, range-checked. `(value_or_None, why)`."""
    if item.get(field) is None:
        return None, None
    number = _finite(item[field])
    if number is None:
        return None, f"waypoint {position}: {field} is not a number"
    low, high = _LIMITS[field]
    if not (low <= number <= high):
        return None, (
            f"waypoint {position}: {field} of {number:g} is outside {low:g}..{high:g}"
        )
    return number, None


def _waypoint(item: Any, position: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        return None, f"waypoint {position} is not an object"

    role = str(item.get("role") or "transit").strip().lower()
    if role not in ROLES:
        return None, (
            f"waypoint {position}: '{role}' is not a role "
            f"({', '.join(ROLES)})"
        )

    out: dict[str, Any] = {"role": role}

    # Either form is accepted, and the *vessel* converts grid metres against
    # whatever origin is current - deliberately not this server, which does not
    # own the grid and would bake in an origin the boat may since have re-zeroed.
    # Latitude and longitude is the canonical form and the one the morning's
    # handout will be in, so it is checked first.
    if item.get("lat") is not None and item.get("lon") is not None:
        lat, lon = _finite(item["lat"]), _finite(item["lon"])
        if lat is None or lon is None:
            return None, f"waypoint {position}: lat/lon is not a number"
        if abs(lat) > 90.0 or abs(lon) > 180.0:
            return None, (
                f"waypoint {position}: {lat:g}, {lon:g} is not a position on Earth"
            )
        out["lat"], out["lon"] = lat, lon
    elif item.get("x") is not None and item.get("y") is not None:
        x, y = _finite(item["x"]), _finite(item["y"])
        if x is None or y is None:
            return None, f"waypoint {position}: x/y is not a number"
        out["x"], out["y"] = x, y
    else:
        return None, (
            f"waypoint {position} has neither lat/lon nor x/y - paste a "
            "coordinate pair, or click the point on the chart"
        )

    name = str(item.get("name") or item.get("id") or position)[:32].strip()
    out["name"] = name or str(position)

    for field in _LIMITS:
        value, why = _optional(item, field, position)
        if why is not None:
            return None, why
        if value is not None:
            out[field] = value

    if item.get("channel_bearing") is not None:
        bearing = _finite(item["channel_bearing"])
        if bearing is None:
            return None, f"waypoint {position}: channel_bearing is not a number"
        out["channel_bearing"] = bearing % 360.0

    notes = str(item.get("notes") or "")[:MAX_NOTES].strip()
    if notes:
        out["notes"] = notes

    return out, None


def validate(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Check a whole plan. Returns `(cleaned, None)` or `(None, why)`.

    Deliberately strict, and for the reason the vessel's copy gives: a plan is
    typed in under time pressure on a competition morning, and a silently
    dropped waypoint is far worse than a refused upload — the boat would run a
    course nobody laid, and it would look like it was working.
    """
    if not isinstance(payload, dict):
        return None, "the plan must be an object"

    raw = payload.get("waypoints")
    if not isinstance(raw, list) or not raw:
        return None, "a plan needs at least one waypoint"
    if len(raw) > MAX_WAYPOINTS:
        return None, (
            f"{len(raw)} waypoints is more than a Njord course has "
            f"(the limit is {MAX_WAYPOINTS})"
        )

    waypoints: list[dict[str, Any]] = []
    for position, item in enumerate(raw, start=1):
        waypoint, why = _waypoint(item, position)
        if why is not None:
            return None, why
        waypoints.append(waypoint)  # type: ignore[arg-type]

    cleaned: dict[str, Any] = {
        "name": (str(payload.get("name") or "plan")[:MAX_NAME].strip() or "plan"),
        "waypoints": waypoints,
    }

    # The direction of buoyage: sailing this way, red is to port.  Njord lays its
    # course with seaward = north, which is the default, but a leg that runs back
    # down the course inverts the sense — and a boat applying the outbound rule
    # on the return passes every gate on the wrong side, which is invisible until
    # it is already through.
    bearing = _finite(payload.get("channel_bearing"))
    cleaned["channel_bearing"] = 0.0 if bearing is None else bearing % 360.0

    # Where to resume, for NJORD §8.2's "re-enter behind the last passed
    # waypoint": the operator drives back by hand and re-uploads with this set
    # rather than watching the boat run the whole course again.
    start_at = payload.get("start_at")
    if start_at is not None:
        index = _finite(start_at)
        if index is None or index < 0 or index >= len(waypoints):
            return None, (
                f"start_at must be a waypoint number between 1 and {len(waypoints)}"
            )
        cleaned["start_at"] = int(index)

    return cleaned, None


def summarise(plan: dict[str, Any]) -> str:
    """One line for the command log: what was sent, without the coordinates.

    The audit trail is read after something has gone wrong, and 40 lat/lon pairs
    in it hide the line worth finding.  The roles are what distinguishes one
    upload from the next, so they are what gets logged.
    """
    counts: dict[str, int] = {}
    for waypoint in plan.get("waypoints") or []:
        role = str(waypoint.get("role", "transit"))
        counts[role] = counts.get(role, 0) + 1
    shape = ", ".join(f"{n}x {role}" for role, n in sorted(counts.items()))
    return f"{plan.get('name', 'plan')!r}: {len(plan.get('waypoints') or [])} wp ({shape})"
