#!/usr/bin/env python
"""A fake Ligmax that drives a fake Njord course, so the dashboard has
something to show before the real autonomy is wired in.

    python run.py                      # terminal 1
    python tools/sim_boat.py           # terminal 2

It responds to operator commands (mode changes, E-STOP, hold, go-to), so the
admin controls can be tested end to end.  Nothing here is a model of the real
vessel - it exists to exercise the GUI.

    python tools/sim_boat.py --http            # HTTP ingest instead of UDP
    python tools/sim_boat.py --hz 20 --no-scan
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ligmax_gui.client import GuiClient  # noqa: E402
from ligmax_gui.config import load_config  # noqa: E402
from ligmax_gui.protocol import OBSTACLE_TYPES  # noqa: E402

ORIGIN_LAT, ORIGIN_LON = 63.43049, 10.39506  # Trondheim harbour
LIDAR_RANGE = 65.0  # how far the simulated perception stack *tracks* a mark
# How far the two lidars actually see. The RPLidar C1's datasheet limit, and a
# very different number from LIDAR_RANGE above: on a Njord course most of the
# marks the tracker knows about are well outside it, and the plot should show
# that rather than imply the boat has 65 m of point cloud.
MAX_LIDAR_RANGE_M = 12.0
# Half-angle off the bow within which a lidar return can pick up a camera
# colour. An approximation for the sim only — the real answer is two fisheyes
# aimed 15 deg either side of forward, each with an 88 deg valid cone, looking
# at a 2:1 crop of the sensor (`ligmax-edge/protocol.py`). What matters here is
# that it is a cone rather than a band, so a mark going abeam goes grey.
CAMERA_CONE_DEG = 70.0
MODES = ["MANUAL", "AUTONOMOUS", "HOLD", "DOCKING", "RETURN_HOME"]

# The course: gate pairs up a channel, a cardinal mark, land to port, a
# crossing vessel and a dock at the far end.
GATES = [(-6.0, 20.0, 6.0, 20.0), (-7.0, 45.0, 5.0, 45.0),
         (-5.0, 70.0, 7.5, 70.0), (-8.0, 95.0, 4.0, 95.0),
         (-6.0, 120.0, 6.5, 120.0)]

# Roughly what the Jetson's cameras hand back for a mark of each type. Sensor-
# native RGB, not calibrated colour: on the real boat these are the same numbers
# the detection boxes were drawn from (`ligmax-edge/fusion.py`). A type not
# listed here comes through uncoloured, like the sea does.
SCAN_COLOURS: dict[int, tuple[int, int, int]] = {
    OBSTACLE_TYPES["RED"]: (198, 46, 38),
    OBSTACLE_TYPES["GREEN"]: (36, 168, 84),
    OBSTACLE_TYPES["NORTH"]: (232, 196, 64),
    OBSTACLE_TYPES["SOUTH"]: (232, 196, 64),
    OBSTACLE_TYPES["EAST"]: (232, 196, 64),
    OBSTACLE_TYPES["WEST"]: (232, 196, 64),
    OBSTACLE_TYPES["DOCKING_CENTER"]: (206, 206, 214),
}

COURSE: list[dict] = []
for _index, (rx, ry, gx, gy) in enumerate(GATES):
    COURSE.append({"track_id": 100 + _index * 2, "truth": (rx, ry),
                   "type": OBSTACLE_TYPES["RED"], "avoid_radius": 2.6})
    COURSE.append({"track_id": 101 + _index * 2, "truth": (gx, gy),
                   "type": OBSTACLE_TYPES["GREEN"], "avoid_radius": 2.6})
COURSE.append({"track_id": 200, "truth": (15.0, 84.0),
               "type": OBSTACLE_TYPES["NORTH"], "avoid_radius": 3.4})
COURSE.append({"track_id": 201, "truth": (-19.0, 108.0),
               "type": OBSTACLE_TYPES["WEST"], "avoid_radius": 3.0})
COURSE.append({"track_id": 300, "truth": (0.0, 152.0),
               "type": OBSTACLE_TYPES["DOCKING_CENTER"], "avoid_radius": 1.5})
for _index in range(9):  # a shoreline off to port
    COURSE.append({"track_id": 400 + _index,
                   "truth": (-27.0 - 1.6 * math.sin(_index), 12.0 * _index + 8.0),
                   "type": OBSTACLE_TYPES["LAND"], "avoid_radius": 4.2})

WAYPOINTS = [(0.0, 20.0), (-1.0, 45.0), (1.2, 70.0), (-2.0, 95.0),
             (0.5, 120.0), (0.0, 148.0), (0.0, 151.0)]

# The parking space at the end of the course: three sides of a 2 m square whose
# corners do not meet, opening south towards the boat. The real one is measured
# from the lidar every tick (`ligmax-pi/nodes/self_driving/perception/parking.py`);
# this one is a fixed rectangle, and it exists so the chart's parking overlay and
# the hold countdown beside the boat can be worked on with no vessel present.
#
# Grid metres, in the same frame as everything else here. `north_mouth` is the way
# in; the closed end - the one line with no partner, which the depth offset is
# measured from - is `depth_m` further north.
PARK_EAST_M = 0.0
PARK_MOUTH_NORTH_M = 150.0
PARK_MOUTH_M = 2.0
PARK_DEPTH_M = 2.0
PARK_CORNER_GAP_M = 0.15

# How long the boat sits on the dot. Ten seconds, which is what the vessel's
# `PARK_HOLD_S` defaults to.
PARK_HOLD_S = 10.0

# How close the boat has to be before the space is "found". Stands in for what the
# lidar can actually see into a 2 m box, which is about 4 m on the centreline -
# generous here, because the point is to have something on the chart to look at.
PARK_ACQUIRE_M = 14.0

# The ideal route, as the course would be handed over: a list of GNSS points
# through the middle of each gate. This is published as a second path with
# `kind: "reference"` so the dashboard can draw it against the line the planner
# actually chose, which is the comparison the Njord GUI requirement asks for.
#
# It is deliberately *not* the same list as WAYPOINTS. The planner nudges its
# targets off the gate centres to keep clear of the buoys' avoid radii, and if
# the two lines were identical the plot would prove nothing.
IDEAL_ROUTE = [(0.0, 0.0), *[((rx + gx) / 2.0, (ry + gy) / 2.0)
                             for rx, ry, gx, gy in GATES], (0.0, 151.0)]

# What each leg of that route is *for*. A Njord course is a list of places plus
# the rules in force between them, and the roles are what the chart colours its
# legs by - so the simulator has to carry them or the whole waypoint-role layer
# is invisible until a real boat is on the water. Shaped like an actual course:
# blind GNSS off the start, buoy rules up the channel, a collision-avoidance leg
# where the Otter would be, then the dock.
#
# Must be the same length as IDEAL_ROUTE, and mirrors the names in
# `ligmax_gui/plan.py`'s ROLES.
ROUTE_ROLES = ["transit", "buoys", "buoys", "avoid", "buoys", "hold", "park"]
ROUTE_NAMES = ["1", "1.1", "1.2", "2", "2.1", "3", "4"]

# How the simulated pack behaves. The real figures come off the Daly BMS over
# CAN (`ligmax-pi/nodes/io_manager/battery.py`); these exist to move the widgets.
PACK_CELLS = 12
PACK_CAPACITY_AH = 40.6
PACK_NOMINAL_V = 44.4  # 12S nominal, docs/hardware.md

# Battery-slider travel, from the calibration constants in battery_slider.ino:
# 3200 steps aft of the optical centre and 5000 forward. Expressed here in mm
# only because the dashboard field is in mm - the sketch counts steps, and the
# steps-per-mm figure is not recorded anywhere in git, so this is a stand-in.
RAIL_MIN_MM, RAIL_MAX_MM = -80.0, 125.0

# Status -> what the hull shows. A mirror of the authoritative table in
# `ligmax-pi/nodes/io_manager/lights.py`; the mode numbers are the `M<n>` commands
# `ligmax-subsystems/esp32s/lights_esp/lights_esp.ino` accepts. Kept here only so
# the simulator can drive the dashboard's lights cross-check - if these two ever
# disagree, the Pi is right.
LIGHT_MODES = {
    "AUTONOMOUS": 0,  # MODE_GREEN
    "REMOTE": 1,  # MODE_YELLOW
    "KILLED": 2,  # MODE_RED
    "OUT_OF_CONTROL": 5,  # MODE_F1_FOG, a 4 Hz red strobe
    "STANDBY": 7,  # MODE_BREATHING
}
LIGHT_COLOURS = {
    "AUTONOMOUS": "green",
    "REMOTE": "yellow",
    "KILLED": "red",
    "OUT_OF_CONTROL": "red-strobe",
    "STANDBY": "white",
}

# The stabilisation tuning, as the vessel reads it off the flight controller
# (`ligmax-pi/nodes/io_manager/tuning.py`). Standing in for real parameters, so
# the tuning panel on /control can be driven with no boat: `set_param` writes here
# and the value comes straight back up in `telemetry.tuning.values`, which is
# exactly the round trip the real one makes through ArduPilot's storage.
#
# The numbers are plausible, not measured - nothing on this vessel has been tuned
# yet, and the two scripts ship with every gain at zero on purpose.
TUNING_DEFAULTS = {
    "SCR_USER1": 18.0,   # roll Kp, us/deg
    "SCR_USER2": 4.5,    # roll Kd
    "SCR_USER3": 0.0,    # roll trim knob channel, 0 = off
    "SCR_USER4": 0.0,    # roll trim knob range, deg
    "SCR_USER5": 0.0,    # roll trim from the dashboard, deg
    "SCR_USER6": 0.0,    # ride-height trim from the dashboard, us
    "BSLD_ENABLE": 1.0,
    "BSLD_KP": 0.045,
    "BSLD_KI": 0.008,
    "BSLD_KD": 0.012,
    "BSLD_IMAX": 0.30,
    "BSLD_TRIM": 0.12,
    "BSLD_LIMIT": 0.95,
    "BSLD_SIGN": 1.0,
    "BSLD_TRM_CH": 0.0,
    "BSLD_TRM_DEG": 0.0,
    "BSLD_TRM_OFS": 0.0,
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def wrap180(degrees: float) -> float:
    """Signed angle difference, wrapped to (-180, 180]."""
    return ((degrees + 540.0) % 360.0) - 180.0


class Sim:
    def __init__(self, with_scan: bool = True) -> None:
        self.with_scan = with_scan
        self.t0 = time.time()
        self.x, self.y = 0.0, 0.0
        self.heading = math.pi / 2  # grid radians, pi/2 = +y = north
        self.speed = 0.0
        self.mode = "AUTONOMOUS"
        self.estop = False
        self.waypoint_index = 0
        self.goto: tuple[float, float] | None = None
        self.speed_limit = 3.2
        self.armed = True
        self.soc = 0.94
        self.consumed_wh = 0.0
        self.log_at = 0.0
        # Velocity is kept as a vector rather than derived from heading, because
        # the two differ: `step()` adds a set so COG is not the same as heading.
        self.vx, self.vy = 0.0, 0.0
        self.set_rad = 0.0
        # `lose_control` sets this, so the OUT_OF_CONTROL status and its red
        # strobe can be exercised without a real fault - see `apply()`, and note
        # the box that used to send it is gone.
        self.lost_until = 0.0
        # Stands in for the flight controller's parameter storage. `set_param`
        # writes here and `telemetry.tuning` reads it back, so the panel's whole
        # save-and-reload cycle works with no vessel and no Pixhawk.
        self.tuning = dict(TUNING_DEFAULTS)
        self.tuning_writes = 0
        self.last_param_write = None
        # The autonomy node's own state, so the autopilot panel and the
        # role-coloured course layer can be worked on with no boat present. The
        # cursor is separate from `waypoint_index` because the two count
        # different things: that one indexes the planner's nudged targets, this
        # one indexes the course as it was laid.
        self.autopilot_engaged = False
        self.autopilot_paused = False
        self.autopilot_stuck = False
        self.recording = False
        self.plan_index = 0
        # The parking hold at the end of the course. `None` until the boat reaches
        # the dot, then the moment it got there - which is what the countdown on
        # the chart is measured from.
        self.park_hold_from: float | None = None

    # -- motion -------------------------------------------------------------

    @property
    def target(self) -> tuple[float, float]:
        if self.goto is not None:
            return self.goto
        return WAYPOINTS[min(self.waypoint_index, len(WAYPOINTS) - 1)]

    def step(self, dt: float) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        driving = not self.estop and self.armed and self.mode in (
            "AUTONOMOUS", "DOCKING", "RETURN_HOME", "MANUAL"
        )

        tx, ty = self.target
        dx, dy = tx - self.x, ty - self.y
        distance = math.hypot(dx, dy)

        if driving and distance > 1.2:
            desired = math.atan2(dy, dx)
            error = (desired - self.heading + math.pi) % (2 * math.pi) - math.pi
            self.heading += clamp(error, -0.9 * dt, 0.9 * dt)
            wanted = self.speed_limit * (0.45 if self.mode == "DOCKING" else 1.0)
            wanted *= clamp(distance / 8.0, 0.25, 1.0)
            self.speed += clamp(wanted - self.speed, -1.6 * dt, 0.8 * dt)
        else:
            self.speed += clamp(-self.speed, -2.5 * dt, 2.5 * dt)
            if driving and distance <= 1.2 and self.goto is None:
                if self.waypoint_index < len(WAYPOINTS) - 1:
                    self.waypoint_index += 1
                    self.plan_index = min(self.plan_index + 1, len(ROUTE_ROLES) - 1)
                    events.append(("INFO", f"waypoint {self.waypoint_index} reached"))
                elif self.park_hold_from is None:
                    # The last waypoint is the dot in the middle of the parking
                    # space, so arriving there starts the ten-second hold rather
                    # than ending the run. The real behaviour wants 0.2 m and this
                    # accepts 1.2, because the kinematics here are too crude to
                    # park to a hand's width - what is being exercised is the
                    # countdown and the overlay, not the control loop.
                    self.park_hold_from = time.time()
                    events.append(("INFO", "parked on the dot - holding 10 s"))
                elif time.time() - self.park_hold_from >= PARK_HOLD_S:
                    self.waypoint_index = 0
                    self.plan_index = 0
                    self.park_hold_from = None
                    self.x, self.y = 0.0, 0.0
                    events.append(("INFO", "course complete, restarting run"))
            elif self.goto is not None and distance <= 1.2:
                self.goto = None
                events.append(("INFO", "operator go-to reached"))

        self.speed = clamp(self.speed, 0.0, self.speed_limit)

        # A little cross-track wander so the plot is not a dead straight line,
        # plus a slow set to one side. The set is what makes COG differ from
        # heading: the boat points one way and travels another, which is exactly
        # the effect the two-needle compass and the COG ray are there to show.
        now = time.time() - self.t0
        wander = 0.12 * math.sin(now * 0.7)
        self.set_rad = 0.16 * math.sin(now * 0.11) + 0.05
        course = self.heading + wander + (self.set_rad if self.speed > 0.2 else 0.0)
        self.vx = math.cos(course) * self.speed
        self.vy = math.sin(course) * self.speed
        self.x += self.vx * dt
        self.y += self.vy * dt

        power = 42.0 + 190.0 * (self.speed / max(self.speed_limit, 0.1)) ** 2.4
        self.consumed_wh += power * dt / 3600.0
        self.soc = clamp(0.94 - self.consumed_wh / 1800.0, 0.02, 1.0)
        return events

    # -- who is in charge ---------------------------------------------------

    @property
    def status(self) -> str:
        """One of protocol.VESSEL_STATUS.

        The real machine is `ligmax-pi/nodes/io_manager/status.py` and it decides
        this from the autopilot's mode, the RC link and the safety loop. Here it is
        derived from the simulated mode, plus the `lost_until` fault the operator
        can inject, so the OUT_OF_CONTROL branch and its red strobe can be seen
        working without breaking anything on a real boat.
        """
        if self.estop:
            return "KILLED"
        if self.lost_until > time.time():
            return "OUT_OF_CONTROL"
        if self.mode == "MANUAL":
            return "REMOTE"
        if not self.armed or self.mode == "HOLD":
            return "STANDBY"
        return "AUTONOMOUS"

    # -- GNSS ---------------------------------------------------------------

    def latlon(self) -> tuple[float, float]:
        """Grid metres to degrees, flat-earth. Fine over a 150 m course."""
        lat = ORIGIN_LAT + self.y / 111_320.0
        lon = ORIGIN_LON + self.x / (111_320.0 * math.cos(math.radians(ORIGIN_LAT)))
        return lat, lon

    @property
    def heading_deg(self) -> float:
        return (90.0 - math.degrees(self.heading)) % 360.0

    @property
    def cog_deg(self) -> float | None:
        """None below a walking pace: the direction of a near-zero velocity is noise."""
        if math.hypot(self.vx, self.vy) < 0.15:
            return None
        return (90.0 - math.degrees(math.atan2(self.vy, self.vx))) % 360.0

    def cross_track_error(self) -> float:
        """Signed metres from the nearest leg of IDEAL_ROUTE, positive to starboard.

        This is the number the dashboard puts beside "off the ideal route", so it
        is measured against the same line the map draws in amber rather than
        against the planner's own path.
        """
        best = 0.0
        best_distance = float("inf")
        for (ax, ay), (bx, by) in zip(IDEAL_ROUTE, IDEAL_ROUTE[1:]):
            lx, ly = bx - ax, by - ay
            length2 = lx * lx + ly * ly
            if length2 < 1e-9:
                continue
            t = clamp(((self.x - ax) * lx + (self.y - ay) * ly) / length2, 0.0, 1.0)
            px, py = ax + t * lx, ay + t * ly
            distance = math.hypot(self.x - px, self.y - py)
            if distance < best_distance:
                best_distance = distance
                # Cross product sign says which side of the leg we are on.
                best = math.copysign(distance, lx * (self.y - ay) - ly * (self.x - ax))
        return -best  # flip so positive reads as "to starboard of the route"

    # -- what the perception stack would report -----------------------------

    def tracks(self) -> list[dict]:
        out = []
        for item in COURSE:
            tx, ty = item["truth"]
            distance = math.hypot(tx - self.x, ty - self.y)
            if distance > LIDAR_RANGE:
                continue
            # Noise and confidence both degrade with range.
            noise = 0.02 + 0.05 * (distance / LIDAR_RANGE)
            confidence = clamp(1.05 - (distance / LIDAR_RANGE) ** 1.6, 0.18, 0.99)
            out.append({
                "track_id": item["track_id"],
                "position": [tx + random.gauss(0, noise), ty + random.gauss(0, noise)],
                "type": item["type"],
                "confidence": round(confidence, 3),
                "avoid_radius": item["avoid_radius"],
                "source": "lidar+camera" if confidence > 0.6 else "lidar",
            })

        # A crossing vessel, heading west across the channel.
        phase = (time.time() - self.t0) * 0.55
        bx = 26.0 - (phase % 60.0)
        by = 62.0
        if math.hypot(bx - self.x, by - self.y) < LIDAR_RANGE:
            out.append({
                "track_id": 900,
                "position": [bx, by],
                "type": OBSTACLE_TYPES["BOAT"],
                "confidence": 0.83,
                "avoid_radius": 5.5,
                "heading": [-1.0, 0.0],
                "velocity": [-1.9, 0.0],
                "source": "lidar+camera",
            })
        return out

    def scans(self) -> list[dict]:
        """Both lidars, shaped exactly the way the real vessel sends them.

        The boat carries two RPLidar C1s — a front one on the Jetson, whose
        returns the cameras have already coloured, and an aft one on the Pi with
        nothing looking its way — and `ligmax-pi/nodes/io_manager/scan.py` puts
        both into the BOAT frame before publishing. So does this: points are
        `[starboard, forward]` metres, and the dashboard does the rotation onto
        the chart. Simulating the grid-space version instead would leave the one
        transform most likely to be wrong untested by the sim.

        Range is capped at the C1's real 12 m rather than the 45 m this used to
        scatter over, so the plot looks like the one the boat will actually
        produce — on a Njord course that means most marks are out of lidar range
        and only the near one lights up, which is the honest picture.
        """
        if not self.with_scan:
            return []

        world: list[tuple[float, float, tuple[int, int, int] | None]] = []
        for item in COURSE:
            tx, ty = item["truth"]
            if math.hypot(tx - self.x, ty - self.y) > MAX_LIDAR_RANGE_M:
                continue
            bearing = math.atan2(self.y - ty, self.x - tx)
            radius = item["avoid_radius"] * 0.42
            spread = 1.1 if item["type"] == OBSTACLE_TYPES["LAND"] else 0.8
            colour = SCAN_COLOURS.get(item["type"])
            for _ in range(14):
                angle = bearing + random.uniform(-spread, spread)
                jitter = random.gauss(0, 0.045)
                world.append((tx + math.cos(angle) * (radius + jitter),
                              ty + math.sin(angle) * (radius + jitter), colour))
        for _ in range(45):  # water clutter / spray — nothing to colour it with
            angle = random.uniform(0, 2 * math.pi)
            radius = random.uniform(1.0, MAX_LIDAR_RANGE_M)
            world.append((self.x + math.cos(angle) * radius,
                          self.y + math.sin(angle) * radius, None))

        # World -> boat frame, the exact inverse of what `web/js/map.js` does to
        # put these back on the chart.
        hx, hy = math.cos(self.heading), math.sin(self.heading)
        front: list[list[float]] = []
        front_rgb: list[int] = []
        aft: list[list[float]] = []
        for wx, wy, colour in world:
            dx, dy = wx - self.x, wy - self.y
            forward = dx * hx + dy * hy
            starboard = dx * hy - dy * hx
            if forward >= 0.0:
                front.append([round(starboard, 2), round(forward, 2)])
                # -1 is "no camera coloured this return". Whether a lens covers a
                # return is an ANGLE off the bow, not a distance abeam: a point
                # 2 m ahead and 2.5 m to the side is 51 deg out, which no
                # forward-looking camera sees, however close it is. So a mark
                # nearly abeam goes grey as the boat draws level with it, which
                # is what the real rig does too.
                lit = colour and abs(math.degrees(math.atan2(starboard, forward))) <= CAMERA_CONE_DEG
                front_rgb.extend(colour if lit else (-1, -1, -1))
            else:
                # Nothing looks astern, so these never carry colour at all.
                aft.append([round(starboard, 2), round(forward, 2)])

        out = []
        if front:
            out.append({"source": "front_lidar", "frame": "boat",
                        "points": front, "rgb": front_rgb,
                        "coloured": sum(1 for i in range(0, len(front_rgb), 3)
                                        if front_rgb[i] >= 0)})
        if aft:
            out.append({"source": "aft_lidar", "frame": "boat", "points": aft})
        return out

    def path(self) -> dict:
        remaining = WAYPOINTS[self.waypoint_index:]
        if self.goto is not None:
            remaining = [self.goto]
        return {
            "points": [[self.x, self.y], *[list(p) for p in remaining]],
            "target_index": 1,
            "kind": "planned",
            "label": "A* / pure pursuit",
        }

    def paths(self) -> list[dict]:
        """The ideal route, sent alongside the planned one for comparison.

        Whole thing every frame, not just the part still ahead: the operator wants
        to see how far the boat strayed from the legs it has already sailed, and
        that is gone the moment the route is trimmed as it is consumed.
        """
        return [
            {
                "points": [list(point) for point in IDEAL_ROUTE],
                "kind": "reference",
                "label": "Ideal route (GNSS)",
                # The role and the label of each waypoint, in lockstep with the
                # points - see `ligmax-pi/nodes/self_driving/plan.py`'s
                # `reference_layer()`, which is what a real vessel sends.
                "roles": list(ROUTE_ROLES),
                "names": list(ROUTE_NAMES),
                "indices": list(range(len(IDEAL_ROUTE))),
                "target_index": self.plan_index,
                "passed_index": self.plan_index - 1,
            }
        ]

    def parking(self) -> dict | None:
        """`telemetry.autopilot.parking` - the space, the dot, and the countdown.

        Mirrors what `ligmax-pi/nodes/self_driving/behaviours/parking.py` publishes,
        in the same world metres, so the chart's overlay is exercised by exactly the
        payload a real boat sends. The difference is only in where the numbers come
        from: three fitted lidar lines there, one hardcoded rectangle here.

        None until the boat is close enough for the space to have been "found",
        because a chart that draws the berth from the start of the course would hide
        the failure that actually matters - the boat never finding it.
        """
        half_mouth = PARK_MOUTH_M / 2.0
        back = PARK_MOUTH_NORTH_M + PARK_DEPTH_M
        centre = (PARK_EAST_M, PARK_MOUTH_NORTH_M + PARK_DEPTH_M / 2.0)
        if math.hypot(centre[0] - self.x, centre[1] - self.y) > PARK_ACQUIRE_M:
            return None

        low, high = PARK_EAST_M - half_mouth, PARK_EAST_M + half_mouth
        gap = PARK_CORNER_GAP_M
        held = 0.0 if self.park_hold_from is None else time.time() - self.park_hold_from
        holding = self.park_hold_from is not None and held < PARK_HOLD_S

        return {
            "seen": True,
            "kind": "park",
            # Offset zero, so the dot is the middle of the space. Change
            # PARK_DEPTH_M above rather than moving this: the whole point of the
            # overlay is that the dot is derived from the space.
            "target": [round(centre[0], 2), round(centre[1], 2)],
            "centre": [round(centre[0], 2), round(centre[1], 2)],
            # mouth, closed end, closed end, mouth - the order the chart draws as an
            # open U, with the way in left open.
            "corners": [
                [low, PARK_MOUTH_NORTH_M],
                [low, back],
                [high, back],
                [high, PARK_MOUTH_NORTH_M],
            ],
            # The three lines, with the corners deliberately not meeting.
            "lines": [
                [[low + gap, back], [high - gap, back]],
                [[low, PARK_MOUTH_NORTH_M], [low, back - gap]],
                [[high, PARK_MOUTH_NORTH_M], [high, back - gap]],
            ],
            "into_deg": 0.0,
            # The angle the hull has to hold for the countdown to count. This sim
            # parks bow-in, so it is the same as the way in; an alongside park
            # rotates 90 degrees off it once inside the space.
            "park_heading_deg": 0.0,
            # `heading_deg` rather than `self.heading`, which is grid radians with
            # pi/2 for north - the compass property is the one that matches the
            # bearing above.
            "heading_error_deg": round(abs(wrap180(self.heading_deg - 0.0)), 1),
            "mouth_m": PARK_MOUTH_M,
            "depth_m": PARK_DEPTH_M,
            "depth_measured_m": PARK_DEPTH_M,
            "depth_source": "measured",
            "offset_m": 0.0,
            "offset_clamped": False,
            "dot_depth_m": round(PARK_DEPTH_M / 2.0, 2),
            "corner_gap_m": PARK_CORNER_GAP_M,
            "age_s": 0.0,
            "hold_required_s": PARK_HOLD_S,
            "hold_remaining_s": round(max(0.0, PARK_HOLD_S - held), 1) if holding else None,
            "segments": 3,
        }

    def autopilot(self) -> dict:
        """`telemetry.autopilot`, shaped like the autonomy node's own block.

        Mirrors `ligmax-pi/nodes/self_driving/pilot.py:telemetry()`. The reason
        sentence is the field that matters - NJORD §11.4 scores it - so it is
        written the way a behaviour would write it, in words, rather than being
        a mode name repeated back.
        """
        index = min(self.plan_index, len(ROUTE_ROLES) - 1)
        role = ROUTE_ROLES[index]
        tx, ty = IDEAL_ROUTE[index]
        distance = math.hypot(tx - self.x, ty - self.y)

        reasons = {
            "transit": f"running on GNSS to {ROUTE_NAMES[index]}, {distance:.0f} m to go",
            "buoys": "red buoy to port, green to starboard - holding the gate centre",
            "avoid": "vessel crossing from starboard - giving way, turning to pass astern",
            "hold": "arrived; holding station until told otherwise",
            "dock": "berth found from the lidar, 2.0 m gap - lining up bow-in",
            "park": "three lines found - creeping onto the dot in the middle",
        }

        if self.estop:
            mode, reason, blocked = "BLOCKED", "propulsion power is cut", "E-stop engaged"
        elif not self.autopilot_engaged:
            mode, reason, blocked = (
                "IDLE",
                "observing only - press Engage to start the course",
                None,
            )
        elif self.autopilot_paused:
            mode, reason, blocked = "PAUSED", "holding station at the operator's request", None
        elif self.plan_index >= len(ROUTE_ROLES):
            mode, reason, blocked = "FINISHED", "course complete", None
        else:
            mode, reason, blocked = "RUNNING", reasons.get(role, "under way"), None

        block = {
            "mode": mode,
            "reason": reason,
            "stuck": self.autopilot_stuck,
            "behaviour": role,
            "sees": f"{len(self.tracks())}x tracked mark",
            "plan": {
                "name": "sim course",
                "waypoints": len(ROUTE_ROLES),
                "index": self.plan_index,
                "current": ROUTE_NAMES[index],
                "role": role,
                "last_passed": ROUTE_NAMES[self.plan_index - 1] if self.plan_index else None,
                "finished": self.plan_index >= len(ROUTE_ROLES),
            },
            "distance_to_waypoint": round(distance, 1),
            "bearing_to_waypoint": round(
                math.degrees(math.atan2(tx - self.x, ty - self.y)) % 360.0, 1
            ),
            "commander": {
                "engaged": self.autopilot_engaged,
                "intent": "goto" if mode == "RUNNING" else "hold",
                "speed_cmd": round(self.speed, 2),
            },
            "recording": (
                {"recording": True, "file": "sim-run.jsonl.gz"}
                if self.recording
                else {"recording": False}
            ),
            "perception": {
                "front_clusters": len(self.tracks()),
                "aft_clusters": max(0, len(self.tracks()) - 3),
                "tracks": len(self.tracks()),
                "confirmed": len(self.tracks()),
                "edge": "connected",
            },
            "bus": {"hz": 9.9},
        }
        if blocked:
            block["blocked"] = blocked

        # The parking overlay and the hold countdown. `hold_remaining_s` is
        # published at the top level as well as inside `parking`, because the timer
        # beside the boat on the chart is deliberately not parking-specific - any
        # behaviour that holds can drive it.
        parking = self.parking()
        if parking is not None:
            block["parking"] = parking
            block["phase"] = (
                "hold" if parking["hold_remaining_s"] is not None else "enter"
            )
            block["hold_required_s"] = parking["hold_required_s"]
            if parking["hold_remaining_s"] is not None:
                block["hold_remaining_s"] = parking["hold_remaining_s"]
                block["reason"] = (
                    f"parked on the dot - holding "
                    f"{PARK_HOLD_S - parking['hold_remaining_s']:.0f}/"
                    f"{PARK_HOLD_S:.0f} s"
                )
        return block

    def telemetry(self) -> dict:
        now = time.time() - self.t0
        cell_low = 3.62 + 0.42 * self.soc
        pack_v = cell_low * PACK_CELLS + random.gauss(0, 0.02)
        power = 42.0 + 190.0 * (self.speed / max(self.speed_limit, 0.1)) ** 2.4
        roll = 2.6 * math.sin(now * 0.9) + random.gauss(0, 0.25)
        pitch = 1.7 * math.sin(now * 0.62 + 1.0) + random.gauss(0, 0.2)
        cog = self.cog_deg
        tx, ty = self.target

        # Pitch trim: the pack slides fore and aft to cancel pitch, so the rail
        # position tracks the pitch error rather than being an independent wave.
        # BSLD_TRM_OFS moves the *target*, so a trim set from the dashboard shifts
        # where the pack settles - which is what makes the panel testable here.
        pitch_error = pitch - self.tuning.get("BSLD_TRM_OFS", 0.0)
        rail_mm = clamp(20.0 - pitch_error * 18.0, RAIL_MIN_MM, RAIL_MAX_MM)

        # Roll trim. `amas.lua` runs a PD controller on roll and writes the two
        # servo outputs anti-symmetrically, then adds a common-mode ride-height
        # offset. Reproduced here so the panel shows the shape of the real thing:
        # both channels move together for height and apart for roll.
        # Roll error against the trim the dashboard has set, and the standing
        # height offset added on top - the same sum amas.lua makes, so setting
        # either trim from the panel visibly moves these two figures.
        roll_us = clamp(-(roll - self.tuning.get("SCR_USER5", 0.0)) * 42.0, -500.0, 500.0)
        height_us = clamp(
            60.0 + 40.0 * math.sin(now * 0.19) + self.tuning.get("SCR_USER6", 0.0),
            -500.0, 500.0,
        )
        port_us = clamp(1500.0 + roll_us + height_us, 1000.0, 2000.0)
        starboard_us = clamp(1500.0 - roll_us + height_us, 1000.0, 2000.0)
        saturated = port_us in (1000.0, 2000.0) or starboard_us in (1000.0, 2000.0)

        return {
            "battery": {
                # The real numbers come off the Daly BMS over CAN, not from the
                # autopilot - `source` is what says so on the dashboard, and it is
                # the field to watch if the SOC ever looks like a guess.
                "source": "daly_bms",
                "age": round(random.uniform(0.2, 1.1), 2),
                "soc": round(self.soc, 4),
                "voltage": round(pack_v, 2),
                "current": round(power / max(pack_v, 1.0), 2),
                "power": round(power, 1),
                # Wh left, from the BMS's own Ah rating and SOC rather than from a
                # hardcoded pack size. This is the derivation battery.py uses.
                "remaining_wh": round(PACK_CAPACITY_AH * self.soc * PACK_NOMINAL_V, 0),
                "consumed_wh": round(self.consumed_wh, 1),
                "capacity_ah": PACK_CAPACITY_AH,
                "cell_min": round(cell_low, 3),
                "cell_max": round(cell_low + 0.031, 3),
                "cell_delta": 0.031,
                "temperature": round(24.5 + 9.0 * (power / 240.0), 1),
                "cycles": 37,
                "bms_ok": True,
                "charge_fet": False,
                "discharge_fet": not self.estop,
            },
            "power": {
                "propulsion_w": round(power * 0.82, 1),
                "compute_w": round(31.0 + 4.0 * math.sin(now * 0.3), 1),
                "actuators_w": round(9.0 + 6.0 * abs(math.sin(now * 0.8)), 1),
                "total_w": round(power, 1),
            },
            "motion": {
                # SOG and COG are the GNSS figures; `speed` is the fused estimate.
                # They differ here by a hair on purpose, because they do on a boat.
                "sog": round(math.hypot(self.vx, self.vy), 3),
                "cog_deg": None if cog is None else round(cog, 1),
                "heading_deg": round(self.heading_deg, 1),
                "crab_deg": (
                    None if cog is None else round(wrap180(self.heading_deg - cog), 1)
                ),
                "speed": round(self.speed, 3),
                "yaw_rate": round(math.degrees(0.9 * math.sin(now * 0.5)), 2),
                "roll": round(roll, 2),
                "pitch": round(pitch, 2),
                "cross_track_error": round(self.cross_track_error(), 3),
                "distance_to_target": round(math.hypot(tx - self.x, ty - self.y), 2),
                "bearing_to_target": round(
                    (90.0 - math.degrees(math.atan2(ty - self.y, tx - self.x))) % 360.0, 1
                ),
            },
            "gimbal": {
                # The gimbal cancels hull motion, so residual should stay small.
                "pitch": round(-pitch * 0.06 + random.gauss(0, 0.08), 3),
                "roll": round(-roll * 0.05 + random.gauss(0, 0.08), 3),
                "target_pitch": 0.0,
                "target_roll": 0.0,
                "motor_temp": round(31.0 + 3.0 * abs(math.sin(now * 0.4)), 1),
                "locked": True,
                "correction_hz": 187.0 + random.gauss(0, 4),
            },
            "thrusters": {
                "port_pct": round(clamp(self.speed / self.speed_limit, 0, 1) * 100, 1),
                "starboard_pct": round(
                    clamp(self.speed / self.speed_limit, 0, 1) * 100
                    + random.gauss(0, 2.5), 1
                ),
                "port_temp": round(28.0 + 12.0 * self.speed, 1),
                "starboard_temp": round(27.4 + 12.0 * self.speed, 1),
            },
            "trim": {
                "battery_rail_mm": round(rail_mm, 1),
                "battery_rail_pct": round(
                    100.0 * (rail_mm - RAIL_MIN_MM) / (RAIL_MAX_MM - RAIL_MIN_MM), 1
                ),
                # "commanded", because the slider ESP32 has no link back to the Pi:
                # this is the demand it was given, not a measured position. The real
                # node reports the same distinction (docs/hardware.md).
                "rail_source": "commanded",
                "rail_homing": False,
                "ama_port_us": round(port_us, 0),
                "ama_starboard_us": round(starboard_us, 0),
                "ama_roll_us": round(roll_us, 0),
                "ama_height_us": round(height_us, 0),
                # In words, because "1560 µs" tells you nothing about what the
                # actuators are actually doing to the hull.
                "ama_doing": (
                    "levelling, lifting" if roll_us > 25 and height_us > 20
                    else "levelling" if abs(roll_us) > 25
                    else "holding ride height" if height_us > 20
                    else "neutral"
                ),
                "ama_saturated": saturated,
                "outrigger_port_mm": round(-8.0 * math.sin(now * 0.9), 1),
                "outrigger_starboard_mm": round(8.0 * math.sin(now * 0.9), 1),
            },
            "lights": {
                # What the hull is showing. The dashboard cross-checks this against
                # the status and shouts if they disagree (web/js/status.js).
                "colour": LIGHT_COLOURS[self.status],
                "mode": LIGHT_MODES[self.status],
                "for_status": self.status,
                "link": True,
                "acks": int(now),
            },
            "gps": {
                "fix": "RTK_FIXED" if now % 90 > 12 else "3D",
                "satellites": 17 + int(3 * math.sin(now * 0.2)),
                "hdop": round(0.7 + 0.2 * abs(math.sin(now * 0.35)), 2),
                "lat": round(self.latlon()[0], 7),
                "lon": round(self.latlon()[1], 7),
                "altitude": round(1.4 + 0.3 * math.sin(now * 1.1), 2),
            },
            "system": {
                "cpu_pct": round(38.0 + 18.0 * abs(math.sin(now * 0.4)), 1),
                "jetson_pct": round(61.0 + 22.0 * abs(math.sin(now * 0.55)), 1),
                "cpu_temp": round(52.0 + 7.0 * abs(math.sin(now * 0.3)), 1),
                "ram_pct": round(44.0 + 6.0 * math.sin(now * 0.2), 1),
                "disk_pct": 38.0,
                "uptime_s": round(now, 0),
                "link_rtt_ms": round(21.0 + 9.0 * abs(math.sin(now * 1.3)), 1),
            },
            "autonomy": {
                "planner": "hybrid A*",
                "replans": int(now // 4),
                "waypoint": self.waypoint_index,
                "tracks_fused": len(self.tracks()),
                "loop_hz": round(19.4 + random.gauss(0, 0.4), 2),
                "armed": self.armed,
            },
            # The gains and trims themselves. Shape from
            # `ligmax-pi/nodes/io_manager/tuning.py:Tuning.telemetry()`: the
            # panel needs `known`/`of` and `slider_script` as much as the values,
            # because "still reading" and "the Lua script never ran" are the two
            # states it has to be able to say out loud.
            "tuning": {
                "values": dict(self.tuning),
                "known": len(self.tuning),
                "of": len(TUNING_DEFAULTS),
                "loading": False,
                "queued": 0,
                "writes": self.tuning_writes,
                "write_failures": 0,
                "slider_script": True,
                **({"last_write": self.last_param_write} if self.last_param_write else {}),
            },
            "bilge": {"pump_1": False, "pump_2": False, "water_detected": False},
            # The block the autopilot panel is built on. On a real vessel this
            # comes from `nodes/self_driving/`, published at 2 Hz.
            "autopilot": self.autopilot(),
            # What io_manager says about the link to that node. Present because
            # "the autonomy node is not running" and "the node bus is broken"
            # look identical without it.
            "autopilot_bridge": {"state": "connected", "hz": 9.9},
        }

    # -- operator commands --------------------------------------------------

    def apply(self, command: dict) -> tuple[str, str]:
        name = command.get("name")
        args = command.get("args") or {}
        if name == "set_mode":
            self.mode = str(args.get("mode", self.mode))
            return "acked", f"mode is now {self.mode}"
        if name == "estop":
            self.estop = True
            self.speed = 0.0
            return "acked", "propulsion cut, safety loop open"
        if name == "estop_clear":
            self.estop = False
            return "acked", "safety loop closed"
        if name == "hold":
            self.mode = "HOLD"
            return "acked", "holding position"
        if name == "resume":
            self.mode = "AUTONOMOUS"
            self.goto = None
            return "acked", "mission resumed"
        if name == "arm":
            self.armed = True
            return "acked", "propulsion armed"
        if name == "disarm":
            self.armed = False
            return "acked", "propulsion disarmed"
        if name == "goto":
            self.goto = (float(args.get("x", 0.0)), float(args.get("y", 0.0)))
            return "acked", f"driving to {self.goto[0]:.1f}, {self.goto[1]:.1f}"
        if name == "clear_waypoints":
            self.goto = None
            self.waypoint_index = 0
            return "acked", "waypoints cleared"
        if name == "set_speed_limit":
            # The real vessel refuses anything above 5 knots and says so
            # (ligmax-pi/nodes/io_manager/guided.py); this only has to not model
            # a boat that would go faster than one, so it clamps to the same
            # ceiling. The server refuses out-of-range values before either of us
            # sees them anyway.
            self.speed_limit = clamp(float(args.get("value", 2.0)), 0.2, 5.0 * 0.514444)
            return "acked", (
                f"speed limit {self.speed_limit:.2f} m/s "
                f"({self.speed_limit / 0.514444:.2f} kn)"
            )
        if name == "recentre_origin":
            self.x, self.y = 0.0, 0.0
            return "acked", "grid origin re-zeroed"
        # --- the autonomy node ---------------------------------------------
        #
        # Acked the way the real node acks: in words, and refusing where it would
        # refuse. `set_plan` in particular echoes back the shape of what it was
        # given, because that ack is the operator's only confirmation that the
        # roles arrived the way they were typed.
        if name == "set_plan":
            plan = args.get("plan") or args
            waypoints = plan.get("waypoints") or []
            if not waypoints:
                return "failed", "a plan needs at least one waypoint"
            shape: dict[str, int] = {}
            for waypoint in waypoints:
                role = str(waypoint.get("role", "transit"))
                shape[role] = shape.get(role, 0) + 1
            self.plan_index = 0
            self.autopilot_paused = False
            return "acked", (
                f"loaded {len(waypoints)} waypoints: "
                + ", ".join(f"{n}x {role}" for role, n in sorted(shape.items()))
            )
        if name == "clear_plan":
            self.autopilot_engaged = False
            self.plan_index = 0
            return "acked", "plan cleared"
        if name == "autopilot_start":
            if self.estop:
                return "failed", "E-stop is engaged"
            if not self.armed:
                # The real pilot arms as part of engaging; this refusal exists so
                # the panel's "refuses with a readable reason" path is reachable.
                self.armed = True
            self.autopilot_engaged = True
            self.autopilot_paused = False
            self.recording = True
            self.mode = "AUTONOMOUS"
            return "acked", "engaged - GUIDED requested, armed, recording started"
        if name == "autopilot_stop":
            self.autopilot_engaged = False
            self.autopilot_paused = False
            self.recording = False
            self.mode = "HOLD"
            return "acked", "disengaged, holding"
        if name == "autopilot_pause":
            if not self.autopilot_engaged:
                return "failed", "not engaged"
            self.autopilot_paused = True
            return "acked", "holding station"
        if name == "autopilot_resume":
            self.autopilot_paused = False
            return "acked", "carrying on"
        if name == "autopilot_skip":
            self.plan_index = min(self.plan_index + 1, len(ROUTE_ROLES) - 1)
            self.waypoint_index = min(self.waypoint_index + 1, len(WAYPOINTS) - 1)
            return "acked", f"skipped to {ROUTE_NAMES[self.plan_index]}"
        if name == "autopilot_back":
            self.plan_index = max(0, self.plan_index - 1)
            self.waypoint_index = max(0, self.waypoint_index - 1)
            return "acked", f"back to {ROUTE_NAMES[self.plan_index]}"
        if name == "autopilot_goto":
            index = int(args.get("index", 0))
            if not (0 <= index < len(ROUTE_ROLES)):
                return "failed", f"there is no waypoint {index}"
            self.plan_index = index
            return "acked", f"cursor at {ROUTE_NAMES[index]}"
        if name == "record_start":
            self.recording = True
            return "acked", "recording"
        if name == "record_stop":
            self.recording = False
            return "acked", "recording closed"
        if name == "forget_world":
            return "acked", "world model cleared"
        if name == "set_param":
            # The server has already checked the name and the range, so anything
            # arriving here is legal. The real vessel refuses a read-only
            # parameter too, and so does this - it is the one refusal the panel
            # has no other way to show.
            key = str(args.get("name", "")).strip().upper()
            if key not in self.tuning:
                return "failed", f"'{key}' is not a parameter this vessel has"
            if key == "BSLD_SIGN":
                return "failed", "BSLD_SIGN is set on the bench, not from shore"
            try:
                value = float(args.get("value"))
            except (TypeError, ValueError):
                return "failed", f"{key} wants a number"
            self.tuning[key] = value
            self.tuning_writes += 1
            self.last_param_write = f"{key} = {value:g}, saved on the autopilot"
            return "acked", self.last_param_write
        if name == "get_params":
            # Nothing to re-read from - the dict *is* the flight controller here.
            return "acked", f"re-read {len(self.tuning)} parameters"
        if name == "raw":
            # Two debugging hooks, and **no longer reachable from the dashboard**:
            # `raw` was removed from server.py's COMMAND_SPECS on 2026-08-10 (it
            # was never implemented on the real vessel, so every press acked
            # "not implemented"), and /control's raw-command box went with it.
            # Kept here because these are still the only way to see two states
            # without arranging a real fault - to use one, put `"raw": {"label":
            # "Raw command", "args": {"payload": "any"}}` back in COMMAND_SPECS
            # while you debug, and take it out again.
            #
            # `{"lose_control": 20}` fakes losing control for that many seconds,
            # which is what drives the status indicator to OUT_OF_CONTROL and the
            # hull lights to a red strobe.
            payload = args.get("payload")
            if isinstance(payload, dict) and "lose_control" in payload:
                try:
                    seconds = clamp(float(payload["lose_control"]), 1.0, 300.0)
                except (TypeError, ValueError):
                    return "failed", "lose_control wants a number of seconds"
                self.lost_until = time.time() + seconds
                return "acked", f"faking loss of control for {seconds:.0f} s"
            # The other hook worth having: `{"stuck": true}` raises the autopilot
            # panel's STUCK flag, which is NJORD §8.2's twenty-second window
            # starting. There is no way to see that alarm otherwise without
            # actually driving the boat into a corner.
            if isinstance(payload, dict) and "stuck" in payload:
                self.autopilot_stuck = bool(payload["stuck"])
                return "acked", f"stuck flag {'set' if self.autopilot_stuck else 'cleared'}"
            return "acked", f"ignored raw payload: {payload!r}"
        return "failed", f"simulator does not implement '{name}'"


CHATTER = [
    ("DEBUG", "perception", "euclidean clustering: {n} clusters from 1081 returns"),
    ("DEBUG", "gimbal", "imu fusion residual {r:.3f} deg"),
    ("INFO", "planner", "replan complete in {ms:.1f} ms, cost {cost:.2f}"),
    ("DEBUG", "colour", "buoy classifier: red p=0.94 green p=0.03"),
    ("INFO", "trim", "pitch loop settled, rail at {mm:.0f} mm"),
    ("WARN", "gps", "hdop rose to {h:.2f}, degrading position weight"),
    ("DEBUG", "control", "pure pursuit lookahead 4.2 m, xte {x:+.2f} m"),
    ("WARN", "perception", "track 900 unmatched for 2 frames, coasting"),
    ("ERROR", "camera", "starboard frame dropped, resyncing pipeline"),
]


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Simulated Ligmax vessel")
    parser.add_argument("--http", action="store_true", help="use POST /api/ingest")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--hz", type=float, default=10.0)
    parser.add_argument("--no-scan", action="store_true", help="omit lidar points")
    parser.add_argument("--key", default=os.environ.get("LIGMAX_BOAT_KEY", ""))
    args = parser.parse_args()

    if args.http:
        target = f"http://{args.host}:{args.port or config.port}"
    else:
        target = f"udp://{args.host}:{args.port or config.udp_port}"

    sim = Sim(with_scan=not args.no_scan)
    gui = GuiClient(target, key=args.key or None, min_interval=0.0)
    print(f"  simulated vessel -> {target} at {args.hz:g} Hz   (ctrl-c to stop)", flush=True)

    gui.log("INFO", "simulator online, autonomy stack faked", "sim")
    period = 1.0 / max(args.hz, 0.5)
    last = time.time()
    last_chatter = 0.0
    scan_divider = 0

    try:
        while True:
            now = time.time()
            dt = min(now - last, 0.25)
            last = now

            for level, message in sim.step(dt):
                gui.log(level, message, "mission")

            for command in gui.commands():
                status, result = sim.apply(command)
                gui.ack(command["id"], status, result)
                gui.log(
                    "WARN" if command["name"] == "estop" else "INFO",
                    f"operator command '{command['name']}' -> {result}",
                    "command",
                )

            if now - last_chatter > 0.7:
                last_chatter = now
                level, name, template = random.choice(CHATTER)
                gui.log(level, template.format(
                    n=random.randint(4, 19), r=random.uniform(0, 0.4),
                    ms=random.uniform(3, 28), cost=random.uniform(1, 40),
                    mm=random.uniform(90, 160), h=random.uniform(0.7, 1.9),
                    x=random.gauss(0, 0.3),
                ), name)

            scan_divider = (scan_divider + 1) % 3  # scans at a third of the rate
            gui.publish(
                status=sim.status,
                mode=sim.mode,
                estop=sim.estop,
                available_modes=MODES,
                origin={"lat": ORIGIN_LAT, "lon": ORIGIN_LON},
                upstream_direction=[0.0, 1.0],
                grid_bearing=0.0,
                boat={
                    "position": [sim.x, sim.y],
                    # The real velocity vector, which is not along the heading -
                    # see the set in `step()`. That difference is the COG story.
                    "velocity": [sim.vx, sim.vy],
                    "heading": [math.cos(sim.heading), math.sin(sim.heading)],
                    "radius": 1.15,
                },
                tracks=sim.tracks(),
                path=sim.path(),
                paths=sim.paths(),  # the ideal route, drawn against the planned one
                scans=sim.scans() if scan_divider == 0 else None,
                telemetry=sim.telemetry(),
                status_text=(
                    "EMERGENCY STOP ENGAGED" if sim.estop
                    else "NO CONTROL SOURCE" if sim.status == "OUT_OF_CONTROL"
                    else f"{sim.mode.lower()} - waypoint {sim.waypoint_index + 1}"
                    f" of {len(WAYPOINTS)}"
                ),
                force=True,
            )

            time.sleep(max(0.0, period - (time.time() - now)))
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        gui.log("WARN", "simulator going offline", "sim")
        gui.publish(force=True)
        time.sleep(0.15)
        gui.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
