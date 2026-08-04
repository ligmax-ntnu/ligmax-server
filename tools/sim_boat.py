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
LIDAR_RANGE = 65.0
MODES = ["MANUAL", "AUTONOMOUS", "HOLD", "DOCKING", "RETURN_HOME"]

# The course: gate pairs up a channel, a cardinal mark, land to port, a
# crossing vessel and a dock at the far end.
GATES = [(-6.0, 20.0, 6.0, 20.0), (-7.0, 45.0, 5.0, 45.0),
         (-5.0, 70.0, 7.5, 70.0), (-8.0, 95.0, 4.0, 95.0),
         (-6.0, 120.0, 6.5, 120.0)]

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
             (0.5, 120.0), (0.0, 148.0)]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
                    events.append(("INFO", f"waypoint {self.waypoint_index} reached"))
                else:
                    self.waypoint_index = 0
                    self.x, self.y = 0.0, 0.0
                    events.append(("INFO", "course complete, restarting run"))
            elif self.goto is not None and distance <= 1.2:
                self.goto = None
                events.append(("INFO", "operator go-to reached"))

        self.speed = clamp(self.speed, 0.0, self.speed_limit)
        # A little cross-track wander so the plot is not a dead straight line.
        wander = 0.12 * math.sin((time.time() - self.t0) * 0.7)
        self.x += math.cos(self.heading + wander) * self.speed * dt
        self.y += math.sin(self.heading + wander) * self.speed * dt

        power = 42.0 + 190.0 * (self.speed / max(self.speed_limit, 0.1)) ** 2.4
        self.consumed_wh += power * dt / 3600.0
        self.soc = clamp(0.94 - self.consumed_wh / 1800.0, 0.02, 1.0)
        return events

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

    def scan(self) -> dict | None:
        """Synthetic lidar returns: arcs on the near faces of nearby objects."""
        if not self.with_scan:
            return None
        points = []
        for item in COURSE:
            tx, ty = item["truth"]
            distance = math.hypot(tx - self.x, ty - self.y)
            if distance > 45.0:
                continue
            bearing = math.atan2(self.y - ty, self.x - tx)
            radius = item["avoid_radius"] * 0.42
            spread = 1.1 if item["type"] == OBSTACLE_TYPES["LAND"] else 0.8
            for _ in range(14):
                angle = bearing + random.uniform(-spread, spread)
                jitter = random.gauss(0, 0.045)
                points.append([tx + math.cos(angle) * (radius + jitter),
                               ty + math.sin(angle) * (radius + jitter)])
        for _ in range(45):  # water clutter / spray
            angle = random.uniform(0, 2 * math.pi)
            radius = random.uniform(4.0, 42.0)
            points.append([self.x + math.cos(angle) * radius,
                           self.y + math.sin(angle) * radius])
        return {"points": points, "source": "front_lidar"}

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

    def telemetry(self) -> dict:
        now = time.time() - self.t0
        cell_low = 3.62 + 0.42 * self.soc
        pack_v = cell_low * 12 + random.gauss(0, 0.02)
        power = 42.0 + 190.0 * (self.speed / max(self.speed_limit, 0.1)) ** 2.4
        roll = 2.6 * math.sin(now * 0.9) + random.gauss(0, 0.25)
        pitch = 1.7 * math.sin(now * 0.62 + 1.0) + random.gauss(0, 0.2)
        heading_deg = (90.0 - math.degrees(self.heading)) % 360.0

        return {
            "battery": {
                "soc": round(self.soc, 4),
                "voltage": round(pack_v, 2),
                "current": round(power / max(pack_v, 1.0), 2),
                "power": round(power, 1),
                "remaining_wh": round(1800.0 * self.soc, 0),
                "consumed_wh": round(self.consumed_wh, 1),
                "cell_min": round(cell_low, 3),
                "cell_max": round(cell_low + 0.031, 3),
                "temperature": round(24.5 + 9.0 * (power / 240.0), 1),
                "cycles": 37,
                "bms_ok": True,
            },
            "power": {
                "propulsion_w": round(power * 0.82, 1),
                "compute_w": round(31.0 + 4.0 * math.sin(now * 0.3), 1),
                "actuators_w": round(9.0 + 6.0 * abs(math.sin(now * 0.8)), 1),
                "total_w": round(power, 1),
            },
            "motion": {
                "speed": round(self.speed, 3),
                "heading_deg": round(heading_deg, 1),
                "yaw_rate": round(math.degrees(0.9 * math.sin(now * 0.5)), 2),
                "roll": round(roll, 2),
                "pitch": round(pitch, 2),
                "cross_track_error": round(random.gauss(0, 0.22), 3),
                "distance_to_target": round(
                    math.hypot(self.target[0] - self.x, self.target[1] - self.y), 2
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
                "battery_rail_mm": round(120.0 + 40.0 * math.sin(now * 0.25), 1),
                "outrigger_port_mm": round(-8.0 * math.sin(now * 0.9), 1),
                "outrigger_starboard_mm": round(8.0 * math.sin(now * 0.9), 1),
            },
            "gps": {
                "fix": "RTK_FIXED" if now % 90 > 12 else "3D",
                "satellites": 17 + int(3 * math.sin(now * 0.2)),
                "hdop": round(0.7 + 0.2 * abs(math.sin(now * 0.35)), 2),
                "lat": round(ORIGIN_LAT + self.y / 111_320.0, 7),
                "lon": round(
                    ORIGIN_LON
                    + self.x / (111_320.0 * math.cos(math.radians(ORIGIN_LAT))), 7
                ),
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
            "bilge": {"pump_1": False, "pump_2": False, "water_detected": False},
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
            self.speed_limit = clamp(float(args.get("value", 3.2)), 0.2, 8.0)
            return "acked", f"speed limit {self.speed_limit:.1f} m/s"
        if name == "recentre_origin":
            self.x, self.y = 0.0, 0.0
            return "acked", "grid origin re-zeroed"
        if name == "raw":
            return "acked", f"ignored raw payload: {args.get('payload')!r}"
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
                mode=sim.mode,
                estop=sim.estop,
                available_modes=MODES,
                origin={"lat": ORIGIN_LAT, "lon": ORIGIN_LON},
                upstream_direction=[0.0, 1.0],
                grid_bearing=0.0,
                boat={
                    "position": [sim.x, sim.y],
                    "velocity": [math.cos(sim.heading) * sim.speed,
                                 math.sin(sim.heading) * sim.speed],
                    "heading": [math.cos(sim.heading), math.sin(sim.heading)],
                    "radius": 1.15,
                },
                tracks=sim.tracks(),
                path=sim.path(),
                scan=sim.scan() if scan_divider == 0 else None,
                telemetry=sim.telemetry(),
                status_text=(
                    "EMERGENCY STOP ENGAGED" if sim.estop
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
