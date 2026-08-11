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

#: The vessel's speed limit: **5 knots**, which is a Njord rule rather than a
#: number anyone tunes.  Mirrored from ``ligmax-pi/config.py`` (
#: ``VESSEL_SPEED_LIMIT_MS``), where it is deliberately not overridable from the
#: environment.
#:
#: This was 3.0 m/s here for as long as it was 3.0 on the vessel, and stayed
#: behind when the boat dropped to the 5 kn limit on 2026-08-09.  The drift did
#: exactly what the docstring above warns of and nothing else: the editor
#: accepted 2.8, the server returned 200, and the boat refused **the whole
#: plan** a second later with an ack nobody was looking at - at 08:15, on a
#: dock, with the course being typed in.  ``plan.py`` on the vessel refuses
#: rather than clamps, on purpose, so this bound is the only thing that can
#: catch it while it is still being typed.
VESSEL_SPEED_LIMIT_MS = 2.5722


# The eight behaviours, in the order they are offered in the editor's dropdown -
# which is roughly the order a Njord course uses them.  `label` is what the
# operator picks from; `help` is what the row's title text says; `settles` marks
# the ones that are a place to *arrive at and stop*, which the vessel treats
# differently (a tighter acceptance radius, and it refuses to count them as
# passed just because the boat swept past).
#
# There are **two** pairs of docking roles and that is deliberate.  `dock*` finds a
# berth as a gap between two structures and keeps the ordinary obstacle avoidance
# on; `park*` finds it as three lines making a rectangle with open corners, parks
# on the middle of that rectangle plus a static per-type depth offset, and ignores
# the world model entirely - no buoy colours, no clearances, no avoidance.  Neither
# has met the water, so both are offered and the operator picks per waypoint on the
# day.  See `ligmax-pi/nodes/self_driving/behaviours/parking.py`.
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
    "park": {
        "label": "Park (bow-in, lines)",
        "help": (
            "Find three lines making a square with open corners, sit on the "
            "middle of it for 10 s, then REVERSE out. Buoys are ignored "
            "entirely. 'Park offset' moves the dot deeper in; positive is "
            "towards the closed end."
        ),
        "settles": True,
        "default_hold_s": 10.0,
    },
    "park_parallel": {
        "label": "Park (alongside, lines)",
        "help": (
            "The same three lines, 4 m along the dock instead of 2, entered "
            "alongside: sit on the middle for 10 s, then continue forward. Has "
            "its own park offset, separate from the bow-in one."
        ),
        "settles": True,
        "default_hold_s": 10.0,
    },
    # The two roles that can actually work as of 2026-08-11: both lidars are down,
    # so every role above that needs one will sit searching until an operator takes
    # over.  Same manoeuvre, berth found from the dock's three AR markers instead.
    "park_tag": {
        "label": "Dock on AR tags (bow-in)",
        "help": (
            "Task 3.1 with the cameras instead of the lidar. Finds the berth "
            "from its three 18 cm AR tags, enters bow-first, holds 10 s, then "
            "REVERSES out. Picks between the two berths by which one's END tag "
            "it can see - a boat in a berth hides that tag - and 'Berth' "
            "overrules it. Buoys are ignored entirely. THE ONLY DOCKING ROLE "
            "WITH A WORKING SENSOR."
        ),
        "settles": True,
        "default_hold_s": 10.0,
    },
    "park_tag_parallel": {
        "label": "Dock on AR tags (alongside)",
        "help": (
            "Task 3.2 with the cameras. The same three tags 4.13 m apart "
            "instead of 2 m, entered bow-first down the normal and then turned "
            "90 degrees inside: hold 10 s parallel, then continue forward. "
            "There is only one alongside berth, so no choosing."
        ),
        "settles": True,
        "default_hold_s": 10.0,
    },
}

#: Roles that find their berth from the AR tags.  Mirrors ``plan.TAG_ROLES`` on the
#: vessel.
TAG_ROLES = frozenset({"park_tag", "park_tag_parallel"})

#: Which berths each tag role may be pointed at by name.  Mirrors the keys of
#: ``perception/artags.BOW_IN_BERTHS`` and ``PARALLEL_BERTHS`` on the vessel; the
#: ids behind them are the vessel's business and are not duplicated here.
BERTHS: dict[str, tuple[str, ...]] = {
    "park_tag": ("berth 1", "berth 2"),
    "park_tag_parallel": ("alongside",),
}

#: Optional per-waypoint numbers, and the range the vessel will accept.  Sending
#: something outside these is refused there too, so refusing here only moves the
#: message forward in time.
_LIMITS: dict[str, tuple[float, float]] = {
    "speed": (0.05, VESSEL_SPEED_LIMIT_MS),
    "radius": (0.3, 50.0),
    "hold_s": (0.0, 600.0),
    "berth_width_m": (0.5, 10.0),
    # How deep into a parking space to sit, metres from its middle, positive
    # towards the closed end.  Signed, because "half a metre short of the middle"
    # is as ordinary a request as "half a metre deeper".  The vessel clamps it to
    # the space it actually measured as well, and says on the panel when it had to.
    "park_offset_m": (-3.0, 3.0),
}


def role_table() -> dict[str, dict[str, Any]]:
    """The role list for `/api/session`, so the editor renders from one source."""
    return {
        name: {
            "label": spec["label"],
            "help": spec["help"],
            "settles": spec["settles"],
            "default_hold_s": spec["default_hold_s"],
            # Present only on the tag roles, and it is what makes the berth
            # selector render from one source instead of a fourth copy of two
            # strings the vessel will refuse if they are wrong.
            **({"berths": list(BERTHS[name])} if name in BERTHS else {}),
        }
        for name, spec in ROLES.items()
    }


def limits_table() -> dict[str, dict[str, float]]:
    """The numeric bounds for `/api/session`, so the editor's inputs match them.

    Sent rather than hardcoded in the browser for the same reason ``role_table``
    is: it was a third copy of a number that already lives in two repos, and it
    was the copy the operator actually types into.  An ``<input max>`` that says
    3 while the boat's ceiling is 2.5722 is a form that invites the one value it
    will then reject.
    """
    return {field: {"min": low, "max": high} for field, (low, high) in _LIMITS.items()}


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
        if field == "speed" and number > high:
            # Worded like the vessel's own refusal (`nodes/self_driving/plan.py`)
            # rather than as a bare range, because "outside 0.05..2.5722" tells
            # an operator under time pressure nothing at all - and a number over
            # the limit is almost always knots typed into a m/s box.
            return None, (
                f"waypoint {position}: speed {number:g} m/s is over the 5 knot "
                f"limit ({high:.2f} m/s) - lower it"
            )
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

    if item.get("park_probe_deg") is not None:
        probe = _finite(item["park_probe_deg"])
        if probe is None or not (0.0 <= probe <= 360.0):
            return None, (
                f"waypoint {position}: park_probe_deg must be a bearing 0..360"
            )
        out["park_probe_deg"] = probe

    # Which berth to take, for the tag roles only.  Refused rather than dropped for
    # the reason the vessel gives: a misspelt berth name would fall through to "let
    # the tags decide", which is indistinguishable from not having asked — the worst
    # outcome for an override whose whole purpose is overruling the tags.
    if str(item.get("berth") or "").strip():
        berth = str(item["berth"]).strip().lower()
        if role not in TAG_ROLES:
            return None, (
                f"waypoint {position}: 'berth' only means something for "
                f"{' or '.join(sorted(TAG_ROLES))}, and this one is '{role}'"
            )
        allowed = BERTHS[role]
        if berth not in allowed:
            return None, (
                f"waypoint {position}: '{item['berth']}' is not a berth for "
                f"{role} ({', '.join(allowed)})"
            )
        out["berth"] = berth

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
