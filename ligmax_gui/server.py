"""Flask application: static frontend, SSE telemetry stream, ingest, commands.

Transports
----------
    boat  -> server   UDP datagram to :8771  (preferred; lowest latency)
                      or POST /api/ingest    (works through any proxy)
    server -> boat    the reply to either of the above carries queued commands
    server -> browser GET /api/stream        (Server-Sent Events)
    browser -> server POST /api/command      (admin cookie required)

    node  -> server   GET  /api/deploy/<repo>/pending   (node key; polled outbound)
                      POST /api/deploy/<repo>/report
    browser -> server POST /api/deploy/<repo>           (admin cookie required)

    jetson -> server  POST /api/camera?cam=0            (boat key; JPEG body)
                      GET  /api/camera/config           (boat key; polled outbound)
    browser -> server GET  /api/camera/0.jpg            (read gate)
                      POST /api/camera/config           (admin cookie required)

    browser -> server POST /api/camera/capture          (admin cookie required)
    jetson -> server  POST /api/camera/capture/upload   (boat key; full-res JPEG)
    browser -> server GET  /api/camera/captures         (read gate)
                      GET  /api/camera/captures/<name>  (read gate)
                      DELETE /api/camera/captures/<name> (admin cookie required)

    boat  -> server   POST /api/trip/<name>             (boat key; gzip body)
                      GET  /api/trip                    (boat key or read gate)
    browser -> server GET  /api/trip/<boat>/<name>      (read gate; downloads)
                      DELETE /api/trip/<boat>/<name>    (admin cookie required)

Every one of those boat-side links is *outbound from the vessel*, including the
video: on 4G there is no route in. See `camera.py`.
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    redirect,
    request,
    send_from_directory,
    url_for,
)

from . import auth, lights_effects, plan as planning, protocol, stills, trips, tuning
from .camera import MAX_FRAME_BYTES as MAX_CAMERA_BYTES, CameraRelay
from .config import Config, REPO_ROOT, WEB_ROOT, load_config
from .deploy import COMMANDED, DeployRegistry
from .rtk import NtripCaster
from .state import Cursor, Store

SELF_REPO = "ligmax-server"  # the one repo we update by restarting ourselves

STREAM_TICK = 1 / 20  # how often an SSE stream checks for new data
STREAM_HEARTBEAT = 15.0  # comment frame keeps proxies from closing the stream
MAX_STREAM_CLIENTS = 24
MAX_INGEST_BYTES = 4 * 1024 * 1024
UDP_RECV_SIZE = 65535

# How recently the vessel must have sent a frame for the Software panel to call a
# command-driven node reachable. Deliberately far above the 1 Hz publish rate and
# just over the panel's own idle poll (deploy.js POLL_IDLE_MS), so the dot does not
# flicker between refreshes on a link that is merely lumpy.
DEPLOY_LINK_WINDOW = 20.0

# Mirrors ligmax-pi/nodes/io_manager/mission.py's MAX_WAYPOINTS - reject an
# oversized mission here rather than let it reach the vessel and fail there.
MAX_MISSION_WAYPOINTS = 100

# Mirrors ligmax-pi/nodes/io_manager/lights.py's MIN_FPS/MAX_FPS - a bad value
# is a 400 here instead of a silent clamp on the vessel three hops away.
MIN_LIGHTS_FPS = 1.0
MAX_LIGHTS_FPS = 60.0

# The headlight covers' travel, mirroring lights.py's SERVO_ENDPOINTS, which in
# turn mirrors lights_esp.ino's SERVO_L_/SERVO_R_ CLOSED and OPEN. Three copies
# of one hand-calibrated fact, in three repos; the firmware's is the one that
# clamps, the vessel's is the one that refuses, and this one exists so a slider
# knows where its ends are and a bad angle is a 400 the operator reads.
#
# The sides are MIRRORED - left opens by increasing the angle, right by
# decreasing it - so nothing here may reduce the pair to one range.
LIGHTS_SERVO_ENDPOINTS = {
    "left": {"closed": 20, "open": 110},
    "right": {"closed": 160, "open": 70},
}


def _lights_servo_limits() -> dict[str, dict[str, float]]:
    """The per-side bounds `/led_control` builds its two sliders from.

    Sent on `/api/session` rather than written into the page, for the reason
    `waypoint_limits` is: an `<input min>` that is a fourth copy of a hardware
    number is the copy that drifts, and the drift is silent until something is
    already at the wrong angle.
    """
    return {
        side: {
            "closed": ends["closed"],
            "open": ends["open"],
            "min": min(ends["closed"], ends["open"]),
            "max": max(ends["closed"], ends["open"]),
        }
        for side, ends in LIGHTS_SERVO_ENDPOINTS.items()
    }

# `set_speed_limit`'s bounds, mirroring ligmax-pi/nodes/io_manager/guided.py's
# MIN_LIMIT_MS/MAX_LIMIT_MS - which are in turn NJORD's 5 knots out of that
# repo's config.py, the number the vessel enforces and no dashboard can raise.
# Refused here as well as there so an operator who types 4 is told immediately
# instead of watching the row come back "failed" a second later.
#
# The floor was 0.2 until 2026-08-11 and is 0.1 now, because this figure is also
# the autonomy node's speed (see the command spec below) and a first parking
# attempt on the water is run at 0.1 m/s.
MIN_SPEED_LIMIT_MS = 0.1
MAX_SPEED_LIMIT_MS = 5.0 * 0.514444  # 2.5722 m/s

# Commands the dashboard is allowed to forward.  An allow-list, so a stray
# fetch() from a browser console cannot invent new vessel behaviour.
#
# `danger` logs the command at ERROR and makes the UI shout; `log_level` overrides
# the audit level on its own, for the commands that are worth finding in the log
# afterwards without being emergencies.
COMMAND_SPECS: dict[str, dict[str, Any]] = {
    "set_mode": {"label": "Set mode", "args": {"mode": "str"}},
    "estop": {"label": "Emergency stop", "args": {}, "danger": True},
    "estop_clear": {"label": "Clear emergency stop", "args": {}, "confirm": True},
    # Re-homes the battery slider: the vessel pulses its homing line and the
    # slider ESP32 hunts for the optical centre endstop. It stops holding pitch
    # trim while it searches, so it asks first.
    "home_battery": {"label": "Home battery rail", "args": {}, "confirm": True},
    # Fast-forward a repo on the vessel and restart the node tree. Issued by the
    # Software panel's Update button rather than typed, and carries `repo` so the
    # right node acts on it - every node reads the same command queue, and one
    # that does not own `repo` ignores it rather than pulling someone else's code.
    # Dangerous because the restart drops the E-stop relay: propulsion power is
    # cut for the length of it (docs/deploy.md).
    "update": {
        "label": "Update from GitHub",
        "args": {"repo": "str"},
        "confirm": True,
        "danger": True,
    },
    "arm": {"label": "Arm propulsion", "args": {}, "confirm": True},
    "disarm": {"label": "Disarm propulsion", "args": {}},
    # One point on the chart, in grid metres, held by the autopilot in GUIDED
    # until it arrives - ligmax-pi/nodes/io_manager/guided.py. The hand-flown
    # route: no planner, no obstacle avoidance, no stored route. The vessel
    # refuses it outside GUIDED, while disarmed, and with the E-stop engaged,
    # rather than switching anything on the operator's behalf, so a refusal here
    # is normal and says which of those it was.
    "goto": {"label": "Go to point", "args": {"x": "float", "y": "float"}},
    # An admin-laid route, grid metres like `goto` - see
    # ligmax-pi/nodes/io_manager/mission.py. The vessel uploads it as a real
    # MAVLink mission and echoes it back as the map's "ideal route" layer once
    # accepted; running it is a separate `set_mode` to AUTO plus `arm`, so laying
    # a route and setting it moving are always two distinct, audited commands.
    "set_mission": {"label": "Send mission", "args": {"points": "any"}, "confirm": True},
    "clear_waypoints": {"label": "Clear waypoints", "args": {}},
    # **The one speed.** The ground speed a `goto` travels at, the speed an AUTO
    # mission runs at (the vessel sends it on as DO_CHANGE_SPEED), and - since
    # 2026-08-11 - the autonomy node's own setting: what it runs a leg at and the
    # ceiling every behaviour plans under, docking included. One press, both
    # nodes: `autopilot_bridge.SHARED_COMMANDS` on the vessel is what makes that
    # true, and `autopilot.commander.speed_kn` is the vessel's own answer for what
    # is in force.
    #
    # Careful mode and `run_profile` used to live here as well and are gone. They
    # were three ways of saying "how fast may the boat go" and `run_profile` never
    # even reached the vessel - it was not in the forwarding list, so every press
    # acked "not implemented".
    #
    # Bounded above by NJORD's 5 knots (MAX_SPEED_LIMIT_MS), which nothing from
    # here can raise, and below by MIN_SPEED_LIMIT_MS. Logged at WARN for the same
    # reason a gain is - a boat that is suddenly slower is a thing somebody will
    # come looking for.
    "set_speed_limit": {
        "label": "Speed limit",
        "args": {"value": "float"},
        "log_level": "WARN",
    },
    "recentre_origin": {"label": "Re-zero grid origin", "args": {}, "confirm": True},
    # One stabilisation gain or trim, written into the flight controller's own
    # storage by ligmax-pi/nodes/io_manager/tuning.py. The name must be on
    # `tuning.TUNABLES` and the value inside its range, both checked below - this
    # command reaches ArduPilot's parameter interface, so the whitelist is what
    # keeps it away from everything else on the flight controller. Logged at WARN
    # because a changed gain is the first thing to look for when the hull starts
    # behaving differently, and it must be findable in the log afterwards.
    "set_param": {
        "label": "Save tuning value",
        "args": {"name": "str", "value": "float"},
        "confirm": True,
        "log_level": "WARN",
    },
    # Walk the amas up or down, as an RC override on channel 14
    # (ligmax-pi/nodes/io_manager/pixhalwk.py). The translator ESP32 reads the
    # pulse as a VELOCITY, so this is not "go to a height" - 1500 is stop and
    # anything else keeps both amas moving for as long as the vessel keeps
    # sending it. That is why it confirms and why it is logged at WARN: it is
    # the one command here that leaves the boat still moving after it is acked.
    #
    # Two commands rather than one with a `release` flag, for two reasons. The
    # mechanical one: every arg declared below is REQUIRED (see the validator),
    # so a single spec of {"pwm", "release"} could not express either of the two
    # things it meant - both forms 400. The real one: releasing is not a stop.
    # 1500 holds the channel at the translator's own STOP; releasing hands it
    # back to the receiver, and if the transmitter has it parked off centre,
    # letting go is what STARTS the creep. Two irreversible-ish actions that
    # differ that sharply get two buttons and two audit entries, the same split
    # `set_mission`/`arm` and `estop`/`estop_clear` already use.
    "set_ride_height": {
        "label": "Move amas (hold to travel)",
        "args": {"pwm": "float"},
        "confirm": True,
        "log_level": "WARN",
    },
    "release_ride_height": {
        "label": "Release amas channel to the receiver",
        "args": {},
        "confirm": True,
        "log_level": "WARN",
    },
    # The Pixhawk's own safety switch - the button on the hull - pressed from
    # the dashboard. ArduPilot forces the board's safety state directly for this
    # command, so it does what holding the button does and is not gated by
    # BRD_SAFETYOPTION the way the physical press is.
    #
    # Two commands rather than one carrying a boolean, and here the reason is the
    # words rather than the mechanism: safety ON *inhibits* the motor outputs and
    # safety OFF makes them live, which is the opposite of what both phrases
    # sound like. An audit line reading `set_safety enabled=false` would be read
    # wrong by exactly the person reading it in a hurry; `safety_off` with the
    # label below cannot be.
    #
    # `safety_off` is the only command on this list that makes the thrusters
    # capable of turning without any other action, which is what `danger` is for.
    "safety_off": {
        "label": "Safety switch OFF — motor outputs live",
        "args": {},
        "confirm": True,
        "danger": True,
    },
    "safety_on": {
        "label": "Safety switch ON — motor outputs inhibited",
        "args": {},
        "log_level": "WARN",
    },
    # ArduPilot's large-vehicle mag cal: point the hull along a heading known
    # from something that is not the compass, send that heading, and the
    # autopilot rewrites the compass offsets against the world magnetic model
    # for where it is standing. The tumble calibration wants the vehicle rotated
    # through all three axes, which a boat in the water cannot do.
    #
    # Confirms and logs at WARN because it overwrites stored calibration on the
    # flight controller: a bad swing survives every reboot in the chain and
    # shows up later as a boat that will not hold a heading, by which time
    # nobody remembers pressing this.
    "compass_cal": {
        "label": "Calibrate compass from a known heading",
        "args": {"heading": "float"},
        "confirm": True,
        "log_level": "WARN",
    },
    # Re-read the whole tuning table off the autopilot. The vessel does this on
    # every connect and once a minute anyway; this is the button for after someone
    # has been editing parameters in Mission Planner.
    "get_params": {"label": "Reload tuning from the vessel", "args": {}},
    # The /led_control switch: standard (status-driven, the default) vs. an
    # admin's authored test pattern. `lights.py` refuses to honour this at all
    # while the boat is KILLED, whatever it is set to - that guarantee lives on
    # the vessel, not here, so it holds even if this dashboard is compromised.
    "set_lights_mode": {
        "label": "Lights: standard / custom",
        "args": {"custom": "any"},
        "log_level": "WARN",
    },
    # The pattern itself - a solid colour, a per-pixel array, or a looping
    # multi-frame animation - authored on /led_control. Fully validated below
    # rather than left to the vessel to discover is malformed.
    "set_lights_pattern": {
        "label": "Send light pattern",
        "args": {"frames": "any"},
        "log_level": "WARN",
    },
    # How often lights.py's worker redraws - the breathe and strobe as well as
    # a loaded pattern. Clamped on the vessel (lights.py's MIN_FPS/MAX_FPS), so
    # this is validated the same way here rather than left to be silently
    # clamped three hops away.
    "set_lights_fps": {
        "label": "Lights refresh rate",
        "args": {"fps": "float"},
        "log_level": "WARN",
    },
    # The two headlight-cover servos on the lights ESP32, to an angle each, from
    # /led_control's sliders. Both angles every time: every arg declared here is
    # required (see the validator), and two sliders that are always sent together
    # is also the honest shape - the covers are a pair on the boat.
    #
    # The only lights command that moves a mechanism rather than lighting one,
    # which is why it is bounded per side below rather than clamped, and why it
    # is logged at WARN: a cover found somewhere nobody left it is a thing to be
    # able to look up afterwards. Not `confirm`, though - it is small, reversible
    # by moving the slider back, and meant to be dragged while watching the bow.
    "set_lights_servos": {
        "label": "Headlight cover angles",
        "args": {"left": "float", "right": "float"},
        "log_level": "WARN",
    },
    # --- the autonomy node -------------------------------------------------
    #
    # These are the only commands on this list that io_manager does NOT handle:
    # it routes anything in its `AUTOPILOT_COMMANDS` set through to
    # ligmax-pi/nodes/self_driving/, which acks each one itself with a
    # human-readable result. So a refused plan comes back in the operator's own
    # words ("waypoint 4: 'dock' needs a berth width"), not as a status code.
    #
    # `set_plan` is the course itself, with a role on every waypoint - see
    # plan.py, and note that laying a plan never moves the boat. Engaging is
    # `autopilot_start`, deliberately a separate, separately-audited action, the
    # same split `set_mission` has with `set_mode` + `arm`.
    "set_plan": {
        "label": "Send course",
        "args": {"plan": "any"},
        "confirm": True,
        "log_level": "WARN",
    },
    "clear_plan": {"label": "Forget the course", "args": {}, "confirm": True},
    # Which camera sources may create red and green marks - the two modes on
    # /surprise_task. `perception/world.absorb_detections` normally refuses to let
    # any camera detection create a mark, because the buoy detector is weak and a
    # phantom buoy is worse than a missing one; with both lidars dead that rule
    # leaves `behaviours/buoys.py` with an empty world model and a scored buoy leg
    # degrades to blind GNSS transit. This is the switch that opens the exception,
    # and the vessel default is OFF.
    #
    # Logged at WARN and not confirmed: it changes what the boat believes rather
    # than what it is doing, it is reversible by pressing another one, and it is
    # meant to be flipped while watching the mask. What must be findable afterwards
    # is which source was live on which attempt - hence the log line.
    #
    # `sources` is a comma-separated string ("colour", "yolo", "colour,yolo") or
    # empty for off. Validated on the vessel, which owns the list of sources that
    # exist, and which answers in the operator's own words.
    "set_mark_source": {
        "label": "Mark source (colour / YOLO)",
        "args": {"sources": "str"},
        "log_level": "WARN",
    },
    # The timer in NJORD §8.1 starts when the boat goes autonomous, and this is
    # the moment it does: it requests GUIDED, arms, and opens a trip recording.
    "autopilot_start": {
        "label": "Engage autonomy",
        "args": {},
        "confirm": True,
        "danger": True,
        "log_level": "WARN",
    },
    "autopilot_stop": {"label": "Disengage autonomy", "args": {}, "log_level": "WARN"},
    "autopilot_pause": {"label": "Hold station", "args": {}},
    "autopilot_resume": {"label": "Carry on", "args": {}},
    "autopilot_skip": {"label": "Skip this waypoint", "args": {}},
    # NJORD §8.2's recovery: after the 20 s search window the team takes over and
    # re-enters *behind the last passed waypoint*. This is the button that is
    # reached for under time pressure, so it takes no argument and no
    # confirmation - a wrong press is undone by pressing Skip.
    "autopilot_back": {"label": "Back one waypoint", "args": {}},
    "autopilot_goto": {"label": "Jump to waypoint", "args": {"index": "float"}},
    # Recording without engaging, so a manual run can be reviewed afterwards with
    # the same tools/review_trip.py as an autonomous one.
    "record_start": {"label": "Start recording", "args": {}},
    "record_stop": {"label": "Stop recording", "args": {}},
    # Between TASKS, not between attempts at the same one. The vessel now keeps
    # the marks it has properly surveyed and reloads them after a restart, so
    # this clears the stored survey as well as the live tracks - which is what an
    # operator pressing "clear everything" means, and what stops it all
    # reappearing a minute later. Attempt two of the same task wants the opposite:
    # see forget_object for removing one bad mark without losing the survey.
    "forget_world": {"label": "Clear what it has seen", "args": {}},
    # One object, by the `track_id` drawn on the chart. `float` is how
    # `autopilot_goto` declares its index, so this needs no new validation
    # machinery; the vessel accepts `id` or `track_id` and suppresses that spot
    # for 30 s so a phantom does not come straight back.
    "forget_object": {"label": "Delete this object", "args": {"id": "float"}},
    # `careful_on`, `careful_off` and `run_profile` were here until 2026-08-11.
    # All three said "how fast may the boat go" and `set_speed_limit` above now
    # says it once, for both nodes and for docking as well; `run_profile` had also
    # never worked, since the vessel did not forward it. Nothing replaced them -
    # that is the point.
    #
    # The cardinal alternation prior. Off by default and deliberately so - it is
    # an inference from how marks are laid, not a measurement, and switching it on
    # is a decision somebody makes knowing that. WARN because "why did it pick
    # that side" has to be answerable from the log afterwards.
    "alternation": {
        "label": "Cardinal alternation prior",
        "args": {"on": "any"},
        "log_level": "WARN",
    },
    # There is deliberately no `raw` here. It advertised an arbitrary JSON
    # payload straight at the vessel, which is the one thing this allow-list
    # exists to prevent, and no node ever had a branch for it - every press acked
    # "'raw' is not implemented on the vessel". Removed 2026-08-10 along with
    # `hold` and `resume`, whose modern equivalents are `autopilot_pause` and
    # `autopilot_resume` above (docs/findings.md).
}

_FAILED_ATTEMPT_LIMIT = 8
_FAILED_ATTEMPT_WINDOW = 300.0


class _AttemptLimiter:
    """Crude per-IP throttle on wrong admin keys."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < _FAILED_ATTEMPT_WINDOW]
            self._hits[ip] = hits
            return len(hits) >= _FAILED_ATTEMPT_LIMIT

    def record(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._hits.setdefault(ip, []).append(now)


def _sse(event: str, data: Any) -> str:
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "?"


def absorb_acks(store: Store, deployments: DeployRegistry | None, acks: Any) -> None:
    """Apply a frame's command acks, and mirror `update` into the deploy panel.

    The vessel answers an `update` on the same channel it answers `estop` on, so
    the Software panel would otherwise never learn the outcome and would sit at
    "Waiting" until the request expired. `result` carries git's own message and
    the ack's `head` the new SHA, both set by io_manager.

    Module level, and takes its collaborators as arguments, because both ingest
    paths need it and `serve_udp()` runs on its own thread outside create_app().
    """
    applied = store.ack_commands(acks)
    if not applied or deployments is None:
        return
    # `head` is an extra field on the ack itself, not part of the command the
    # operator issued, so pick it out of the raw payload.
    heads = {
        str(ack.get("id")): str(ack["head"])[:40]
        for ack in acks or []
        if isinstance(ack, dict) and ack.get("head")
    }
    for command in applied:
        if command.name != "update":
            continue
        repo = str(command.args.get("repo") or "")
        if not deployments.known(repo):
            continue
        deployments.report_command(
            repo,
            result="ok" if command.status == "acked" else "failed",
            message=command.result or "",
            head=heads.get(command.id),
        )


def create_app(config: Config | None = None, store: Store | None = None) -> Flask:
    config = config or load_config()
    store = store or Store(
        max_logs=config.log_buffer, max_scan_points=config.max_scan_points
    )

    app = Flask(__name__, static_folder=None)
    app.config["LIGMAX_CONFIG"] = config
    app.config["LIGMAX_STORE"] = store

    limiter = _AttemptLimiter()
    stream_clients = threading.Semaphore(MAX_STREAM_CLIENTS)
    deployments = DeployRegistry(config.repos)
    app.config["LIGMAX_DEPLOY"] = deployments
    cameras = CameraRelay()
    app.config["LIGMAX_CAMERA"] = cameras
    captures = stills.StillStore(config.stills_store)
    app.config["LIGMAX_STILLS"] = captures
    if captures.last_error:
        config.warnings.append(f"full-res stills unavailable: {captures.last_error}")
    recordings = trips.TripStore(config.trip_store)
    app.config["LIGMAX_TRIPS"] = recordings
    if recordings.last_error:
        config.warnings.append(f"trip recordings unavailable: {recordings.last_error}")
    profiles = tuning.ProfileStore(config.tuning_store)
    app.config["LIGMAX_TUNING"] = profiles
    if profiles.last_error:
        config.warnings.append(
            f"saved tuning profiles unavailable: {profiles.last_error}"
        )
    effects = lights_effects.EffectStore(config.light_effects_store)
    app.config["LIGMAX_LIGHT_EFFECTS"] = effects
    if effects.last_error:
        config.warnings.append(
            f"saved light effects unavailable: {effects.last_error}"
        )
    if effects.examples_error:
        # Only the bundled examples are missing - saving and sending still
        # work - so this is a warning, not a refusal to start.
        config.warnings.append(
            f"example light effects unavailable: {effects.examples_error}"
        )

    # The NTRIP caster is built here so its log lines land in the operator's log
    # panel like everything else, but it does not listen until run.py starts its
    # thread - importing this module must never open a public port.
    caster = (
        NtripCaster(
            host=config.rtk_host,
            port=config.rtk_port,
            mount=config.rtk_mount,
            source_password=config.rtk_source_password,
            client_user=config.rtk_user,
            client_password=config.rtk_password,
            base_lat=config.rtk_base_lat,
            base_lon=config.rtk_base_lon,
            log=lambda level, message: store.add_log(level, message, "gui.rtk"),
        )
        if config.rtk_enabled
        else None
    )
    app.config["LIGMAX_RTK"] = caster

    for warning in [*config.warnings, *protocol.check_shared_settings_sync()]:
        store.add_log("WARN", warning, name="gui.config")

    # -- helpers ------------------------------------------------------------

    def is_admin() -> bool:
        if not config.commands_enabled:
            return False
        return auth.token_is_valid(config, request.cookies.get(auth.COOKIE_NAME))

    def may_read() -> bool:
        return config.public_read or is_admin()

    def boat_authorised() -> bool:
        if not config.boat_key:
            return True  # unset key => open ingest, warned about at startup
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer ") and auth.key_matches(
            header[7:], config.boat_key
        ):
            return True
        return auth.key_matches(request.args.get("key"), config.boat_key)

    def node_authorised() -> bool:
        """Same shape as boat_authorised(), but for the update pollers on each node."""
        if not config.node_key:
            return True  # unset key => open, warned about at startup
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer ") and auth.key_matches(
            header[7:], config.node_key
        ):
            return True
        return auth.key_matches(request.args.get("key"), config.node_key)

    def secure_cookies() -> bool:
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        return proto == "https"

    def deny_read() -> Response:
        response = make_response(
            send_from_directory(WEB_ROOT, "locked.html"), 401
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- admin session ------------------------------------------------------

    def consume_key_param(target: str) -> Response | None:
        """Turn `?key=...` into a cookie, then redirect so the URL is clean."""
        key = request.args.get("key")
        if key is None:
            return None

        ip = _client_ip()
        response = redirect(target, code=303)
        response.headers["Cache-Control"] = "no-store"
        # Referrer-Policy stops the key leaking to any resource we load next.
        response.headers["Referrer-Policy"] = "no-referrer"

        if limiter.blocked(ip):
            store.add_log("ERROR", f"admin key attempts throttled for {ip}", "gui.auth")
            response.set_cookie("lx_notice", "throttled", max_age=15, samesite="Lax")
            return response

        if config.commands_enabled and auth.key_matches(key, config.admin_key):
            response.set_cookie(
                auth.COOKIE_NAME,
                auth.issue_token(config),
                max_age=config.session_seconds,
                httponly=True,
                samesite="Lax",
                secure=secure_cookies(),
                path="/",
            )
            response.set_cookie("lx_notice", "granted", max_age=15, samesite="Lax")
            store.add_log("INFO", f"admin session granted to {ip}", "gui.auth")
        else:
            limiter.record(ip)
            response.set_cookie("lx_notice", "denied", max_age=15, samesite="Lax")
            store.add_log("WARN", f"invalid admin key from {ip}", "gui.auth")
        return response

    @app.get("/")
    def index() -> Response:
        if (response := consume_key_param(url_for("index"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "index.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/control")
    def control() -> Response:
        """The dense page: every telemetry field, the controls, logs and audit.

        Same read gate as `/` — a non-admin may look, but every button on it is
        disabled client-side and `/api/command` would refuse them anyway.
        `?key=` works here too, so an operator can bookmark this page directly.
        """
        if (response := consume_key_param(url_for("control"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "control.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/logout")
    def logout() -> Response:
        response = jsonify({"ok": True})
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        store.add_log("INFO", f"admin session ended by {_client_ip()}", "gui.auth")
        return response

    @app.get("/api/session")
    def session() -> Response:
        return jsonify(
            {
                "admin": is_admin(),
                "admin_possible": config.commands_enabled,
                "public_read": config.public_read,
                "obstacle_types": protocol.OBSTACLE_TYPES,
                "wrong_side_length": protocol.WRONG_SIDE_LENGTH,
                "commands": COMMAND_SPECS,
                # The waypoint roles, so the course editor renders its dropdown
                # and its help text from the same table the validator refuses
                # against. Adding a role on the vessel then means changing
                # plan.py here and nothing in the frontend.
                "waypoint_roles": planning.role_table(),
                # And the numeric bounds beside them, for the same reason: the
                # editor's `<input max>` was a third copy of the vessel's speed
                # limit and it was the one that had drifted.
                "waypoint_limits": planning.limits_table(),
                "max_waypoints": planning.MAX_WAYPOINTS,
                # The headlight covers' travel, for /led_control's two sliders -
                # same reasoning as `waypoint_limits` right above, and see
                # `_lights_servo_limits()`.
                "lights_servos": _lights_servo_limits(),
                "vessel_status": protocol.VESSEL_STATUS,
                "server_time": time.time(),
                "shared_settings": protocol.SHARED_SETTINGS_AVAILABLE,
            }
        )

    # -- camera -------------------------------------------------------------
    #
    # Frames arrive from `ligmax-json.local` as ordinary outbound POSTs, because
    # from the water there is no route in. Off by default: this shares the 4G
    # uplink with the telemetry and the command channel, and video is the only
    # payload here big enough to crowd them out. See camera.py.

    def _vessel_state_for(captured_at: Any) -> dict[str, Any]:
        """Where the boat was and how it was lying, to file with a still.

        **This is the nearest telemetry, not synchronised telemetry**, and the
        difference is recorded rather than glossed over. A still climbs a 4G
        uplink for seconds, so the frame that is current when it lands is not the
        frame that was current when the shutter opened; `vessel_age_s` is that
        gap, computed against the Jetson's own capture time.

        And that gap is measured across two clocks. The Jetson has no
        battery-backed RTC (`ligmax-edge/estimate.py CaptureClock`) and NTP's
        first correction after boot is a hard step, so `vessel_age_s` can be
        minutes when nothing is wrong with either machine. `boat_clock_offset` is
        shipped beside it - that is the Pi's own offset from this server - so a
        reader has both halves of the comparison instead of one number to
        misplace confidence in.

        Why bother at all, given the camera is calibrated: a Kannala-Brandt fit
        is *intrinsics*. It says what the lens does with a ray and nothing about
        where the lens points, so it cannot turn a marker's camera-frame pose
        into a berth position - that needs the mount geometry AND which way is
        down. Roll matters at the scale that decides this task: cross-track error
        is `range * sin(roll)`, which at 4 m and 5 degrees is 0.35 m inside a 2 m
        berth. And a fix type is what makes the pictures *checkable* rather than
        merely viewable: photograph a tag, move a measured distance on RTK,
        photograph it again, and the computed ranges have to differ by what the
        baseline says.
        """
        snapshot = store.snapshot()
        state = snapshot.get("state") or {}
        telemetry = state.get("telemetry") or {}
        boat = state.get("boat") or {}
        stats = snapshot.get("stats") or {}

        out: dict[str, Any] = {}
        vessel: dict[str, Any] = {}
        for key in ("attitude", "motion", "gps"):
            block = telemetry.get(key)
            if isinstance(block, dict) and block:
                vessel[key] = dict(block)
        for key in ("position", "heading", "velocity"):
            if key in boat:
                vessel[key] = boat[key]
        if state.get("origin"):
            # The grid origin, without which `position` is metres from nowhere.
            vessel["origin"] = state["origin"]
        if state.get("status"):
            vessel["status"] = state["status"]
        if not vessel:
            return {"vessel": None, "vessel_why": "no telemetry from the vessel"}

        out["vessel"] = vessel
        last_frame = stats.get("last_frame_at")
        out["vessel_at"] = last_frame
        out["boat_clock_offset"] = stats.get("boat_clock_offset")
        try:
            if last_frame is not None and captured_at is not None:
                out["vessel_age_s"] = round(float(last_frame) - float(captured_at), 2)
        except (TypeError, ValueError):
            pass
        # Two independent skews, named so they cannot be confused with each other
        # or with an elapsed time. `boat_clock_offset` is the Pi's epoch minus
        # ours, measured on every telemetry frame; `vessel_age_s` also contains
        # the *Jetson's* offset, because it compares a Pi-stamped arrival against
        # a Jetson-stamped shutter. So if the Pi's offset is near zero and
        # `vessel_age_s` is tens of seconds, the difference is the Jetson's clock
        # and nothing at all was slow - which is a conclusion worth stating here
        # rather than leaving to be re-derived from two numbers on a page.
        pi_offset = stats.get("boat_clock_offset")
        try:
            if out.get("vessel_age_s") is not None and abs(
                float(out["vessel_age_s"]) - float(pi_offset or 0.0)
            ) > 20.0:
                out["clock_skew_s"] = round(
                    float(out["vessel_age_s"]) - float(pi_offset or 0.0), 2
                )
                out["clock_note"] = (
                    "the Jetson's clock is offset from this server by about this "
                    "much - it has no RTC and NTP's first correction is a step, "
                    "not a slew (ligmax-edge/estimate.py CaptureClock, "
                    "docs/findings.md item 23). Read `latency_s` for how long the "
                    "capture actually took."
                )
        except (TypeError, ValueError):
            pass

        if not telemetry.get("attitude"):
            # Said in the record rather than left as a missing key, because the
            # absence is a fact about the vessel's software and not about this
            # still: nothing published roll or pitch before 2026-08-11
            # (ligmax-pi/nodes/io_manager/navigation.py), so a set of pictures
            # with no attitude was taken by a boat that could not report it.
            out["vessel_why"] = "the vessel published no attitude block"
        return out

    def _camera_poll() -> dict[str, Any]:
        """The stream config plus any outstanding full-res capture.

        One helper because it rides on THREE replies - the config poll and both
        exits from the frame POST - and a capture that only reached one of them
        would work when video was on and appear broken when it was off, which is
        exactly backwards: the capture button is most useful with the stream off.

        `captures.pending()` is what expires a stale request and clears a
        completed one, so calling it on every poll is the point rather than a
        side effect to be avoided.
        """
        # `caps` is what the vessel volunteers about its own build - see
        # `CameraRelay.poll`. Read from the query string on both the config poll
        # and the frame POST, so either kind of contact updates it.
        return {
            **cameras.poll(request.args.get("caps", "")),
            "capture": captures.pending(),
        }

    @app.post("/api/camera")
    def camera_ingest() -> Response:
        if not boat_authorised():
            store.note_rejected()
            cameras.note_refused("frame POST, wrong or missing boat key")
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]

        # Nothing is stored while the stream is off, so a sender that ignores the
        # config cannot keep the panel alive - and the reply tells it to stop.
        if not cameras.enabled:
            return jsonify({"ok": False, "enabled": False, **_camera_poll()})  # type: ignore[return-value]

        body = request.get_data(cache=False)
        if len(body) > MAX_CAMERA_BYTES:
            store.note_rejected()
            return jsonify({"error": "frame too large"}), 413  # type: ignore[return-value]

        meta: dict[str, Any] = {}
        for key in ("t", "width", "height", "label", "fps", "seq"):
            if (value := request.args.get(key)) is not None:
                meta[key] = value
        for key in ("width", "height", "seq"):
            if key in meta:
                try:
                    meta[key] = int(float(meta[key]))
                except (TypeError, ValueError):
                    meta.pop(key)

        ok, why = cameras.accept(
            request.args.get("cam", "0"),
            body,
            request.headers.get("Content-Type", "image/jpeg"),
            meta,
        )
        if not ok:
            store.add_log("WARN", f"camera frame rejected: {why}", "gui.camera")
            return jsonify({"error": why}), 400  # type: ignore[return-value]
        # The config rides back on every frame, so a change to fps or width takes
        # effect on the next frame instead of the next poll.
        return jsonify({"ok": True, **_camera_poll()})

    @app.get("/api/camera/config")
    def camera_config() -> Response:
        """What the Jetson should be sending. Polled outbound, like /pending."""
        if not boat_authorised() and not node_authorised():
            # Recorded, because this is the failure that looks like nothing:
            # the poll never reaches poll(), so the panel would otherwise report
            # a Jetson that has "never asked" while it is asking every 5 s.
            cameras.note_refused("config poll, wrong or missing boat key")
            return jsonify({"error": "boat key required"}), 403  # type: ignore[return-value]
        response = jsonify(_camera_poll())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/camera/config")
    def camera_configure() -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        payload = request.get_json(silent=True) or {}
        stream = cameras.configure(payload, by=_client_ip())
        store.add_log(
            "INFO",
            f"camera stream {'on' if stream['enabled'] else 'off'} "
            f"({stream['max_width']} px, q{stream['jpeg_quality']}, "
            f"{stream['fps']} fps) [{_client_ip()}]",
            "gui.camera",
        )
        return jsonify({"ok": True, "stream": stream})

    @app.get("/api/camera/state")
    def camera_state() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        response = jsonify(cameras.state())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/camera/<cam>.jpg")
    def camera_frame(cam: str) -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        frame = cameras.frame(cam)
        if frame is None:
            # 404, not a placeholder image: the panel decides what "no picture"
            # should look like, and a served grey square would cache as content.
            return jsonify({"error": "no recent frame"}), 404  # type: ignore[return-value]
        response = make_response(frame.data)
        response.headers["Content-Type"] = frame.content_type
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Age"] = f"{frame.age():.2f}"
        response.headers["X-Frame-Seq"] = str(frame.seq)
        return response

    # -- full-resolution stills ---------------------------------------------
    #
    # The whole 2592x1944 sensor frame, both cameras, one press: what the AR-tag
    # work needs and what the live view cannot give, since that is a 480 px crop
    # of a 2:1 band aimed off the bow. Requested here, collected by the Jetson on
    # the config poll it already makes, written to disk. See `stills.py` - the
    # ordering, the sizes and the "why not a command" are all there.
    #
    # Reading is behind the read gate, like trip recordings: a photograph of the
    # dock is evidence, and the people who want it in the tent are the ones
    # without the key. Asking for one is admin, because it costs 4G uplink.

    @app.post("/api/camera/capture")
    def camera_capture() -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        payload = request.get_json(silent=True) or {}
        pending = captures.request(
            cameras=payload.get("cameras"),
            quality=payload.get("quality"),
            note=payload.get("note") or "",
            by=_client_ip(),
            poll_count=cameras.polls,
        )
        store.add_log(
            "INFO",
            f"full-res capture #{pending['id']} requested for "
            f"cam{'/cam'.join(pending['cameras'])} at q{pending['quality']}"
            + (f" - {pending['note']}" if pending["note"] else "")
            + f" [{_client_ip()}]",
            "gui.camera",
        )
        state = captures.state()
        return jsonify(
            {"ok": True, **state, "link": _capture_diagnosis(state.get("pending"))}
        )

    @app.delete("/api/camera/capture")
    def camera_capture_cancel() -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        captures.cancel()
        state = captures.state()
        return jsonify(
            {"ok": True, **state, "link": _capture_diagnosis(state.get("pending"))}
        )

    @app.post("/api/camera/capture/upload")
    def camera_capture_upload() -> Response:
        """The Jetson handing back one full-res frame. Boat key, JPEG body.

        Deliberately NOT gated on `cameras.enabled`: a capture is a separate,
        deliberate action from the live stream, and the case it exists for -
        going to the dock to photograph the AR tags - is one where nobody wants
        video running at all.
        """
        if not boat_authorised():
            store.note_rejected()
            cameras.note_refused("capture upload, wrong or missing boat key")
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]

        body = request.get_data(cache=False)
        if len(body) > stills.MAX_STILL_BYTES:
            store.note_rejected()
            return jsonify({"error": "still too large"}), 413  # type: ignore[return-value]

        meta: dict[str, Any] = {}
        # Everything the vessel chooses to say about how the picture was made.
        # `width`/`height` and the sensor mode matter because a calibration does
        # not transfer across modes; `rotated_180` matters because a calibration
        # is only valid for the orientation it was captured in, and a mismatch
        # fails silently (see sender.py's own warning about it).
        for key in (
            "t", "width", "height", "label", "mode", "rotated_180", "calib",
            "wb", "saturation", "exposure_ms", "gain", "quality", "seq",
        ):
            if (value := request.args.get(key)) is not None:
                meta[key] = value
        for key in ("width", "height", "mode", "seq", "quality"):
            if key in meta:
                try:
                    meta[key] = int(float(meta[key]))
                except (TypeError, ValueError):
                    meta.pop(key)
        for key in ("t", "saturation", "exposure_ms", "gain"):
            if key in meta:
                try:
                    meta[key] = float(meta[key])
                except (TypeError, ValueError):
                    meta.pop(key)
        if "rotated_180" in meta:
            meta["rotated_180"] = str(meta["rotated_180"]).lower() in (
                "1", "true", "yes", "on"
            )

        # What the vessel was doing. Merged in HERE rather than sent by the
        # Jetson, because the Jetson does not know: attitude and position come
        # off the Pixhawk to the Pi and reach shore on the telemetry link, while
        # the picture comes off the Jetson on this one. This server is the first
        # place the two meet, which makes it the only place that can staple them
        # together - see `_vessel_state_for` above for what is and is not honest
        # about doing that.
        meta.update(_vessel_state_for(meta.get("t")))

        info, why = captures.accept(
            request.args.get("cam", "0"),
            request.args.get("id"),
            body,
            meta,
        )
        if why is not None:
            store.add_log("WARN", f"full-res still rejected: {why}", "gui.camera")
            return jsonify({"error": why}), 400  # type: ignore[return-value]
        store.add_log(
            "INFO",
            f"full-res still {info['name']} stored "
            f"({info['bytes'] / 1048576.0:.2f} MB"
            + (
                f", {info.get('width')}x{info.get('height')}"
                if info.get("width")
                else ""
            )
            + ")",
            "gui.camera",
        )
        # The state rides back so the Jetson can see it landed, and so a second
        # camera's upload learns the request is now complete.
        return jsonify({"ok": True, "still": info, "capture": captures.pending()})

    def _capture_diagnosis(pending: dict[str, Any] | None) -> dict[str, Any]:
        """Why a capture has not arrived, in the words that fix it.

        A capture is collected on a poll, so "nothing has happened" has four
        causes that look identical from a spinning button, and the operator's next
        action is different for each. Guessing wrong costs a trip to the boat:

          nothing has ever polled        the Jetson is not running, or cannot
                                        reach us at all
          polls are being REFUSED       it is running and its key is wrong
                                        (LIGMAX_BOAT_KEY on ligmax-json.local)
          polling, `still` not offered   it is running code from before captures
                                        existed - git pull and restart sender.py
          polling, `still` offered       it heard us and the frame is in flight or
                                        the encode failed; the Jetson's own log
                                        is where that shows

        The third is the one worth the machinery: a build that predates a feature
        polls perfectly and ignores the new field, which without `caps` is
        indistinguishable from a board that is switched off.
        """
        state = cameras.state()
        polls = state.get("polls") or 0
        age = state.get("last_poll_age")
        supports = state.get("supports_capture")
        since = polls - int((pending or {}).get("polls_at") or 0) if pending else None

        out: dict[str, Any] = {
            "polls": polls,
            "last_poll_age": age,
            "caps": state.get("caps") or [],
            "supports_capture": supports,
            "polls_since_request": since,
            "refused": state.get("refused") or 0,
            "last_refusal": state.get("last_refusal"),
            "last_refusal_age": state.get("last_refusal_age"),
        }

        if state.get("refused") and (state.get("last_refusal_age") or 1e9) < 120:
            out["verdict"] = "refused"
            out["why"] = (
                f"The vessel is reaching this server and being turned away: "
                f"{state.get('last_refusal')}. Check LIGMAX_BOAT_KEY in "
                f"/etc/ligmax/node.env on ligmax-json.local."
            )
        elif age is None:
            out["verdict"] = "silent"
            out["why"] = (
                "Nothing on the vessel has ever asked this server for the camera "
                "config, so nothing can collect a capture. Start the Jetson's "
                "sender (./run.sh on ligmax-json.local)."
            )
        elif age > 30:
            out["verdict"] = "stale"
            out["why"] = (
                f"The Jetson last checked in {age:.0f} s ago and polls every 5 s, "
                f"so it is not currently listening."
            )
        elif supports is False:
            out["verdict"] = "unsupported"
            out["why"] = (
                "The Jetson is polling normally but its build does not offer "
                "full-resolution captures - it is running sender.py from before "
                "that existed. On ligmax-json.local: git pull, then restart "
                "sender.py (./run.sh). Nothing else needs to change."
            )
        elif pending and (since or 0) >= 2:
            out["verdict"] = "declining"
            out["why"] = (
                f"The Jetson has polled {since} times since you asked and has "
                f"sent nothing. It understands the request, so the failure is on "
                f"that board - check its log for 'still not sent' (a missing "
                f"Pillow is the usual cause) or 'still upload failed'."
            )
        elif pending:
            out["verdict"] = "waiting"
            out["why"] = (
                "Asked. The Jetson collects it on its next poll, then each frame "
                "is a couple of megabytes up the 4G link."
            )
        else:
            out["verdict"] = "ready"
            out["why"] = "The Jetson is polling and offers full-resolution capture."
        return out

    @app.get("/api/camera/captures")
    def camera_captures() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        state = captures.state()
        response = jsonify(
            {
                **state,
                "admin": is_admin(),
                "link": _capture_diagnosis(state.get("pending")),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/camera/captures/<name>")
    def camera_capture_file(name: str) -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        path = captures.path_for(name)
        if path is None:
            return jsonify({"error": "no such still"}), 404  # type: ignore[return-value]
        # Inline rather than an attachment: the gallery shows these in an <img>,
        # and a browser that downloads instead of rendering makes reviewing forty
        # frames from the dock forty saves. `?download=1` is the other case.
        response = make_response(
            send_from_directory(
                path.parent,
                path.name,
                mimetype="image/jpeg",
                as_attachment=bool(request.args.get("download")),
            )
        )
        # These never change once written, so let a browser keep them - a gallery
        # of 2 MB JPEGs re-fetched on every render is the one way this panel
        # could cost more than the uplink did.
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response

    @app.delete("/api/camera/captures/<name>")
    def camera_capture_delete(name: str) -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        ok, why = captures.delete(name)
        if not ok:
            return jsonify({"error": why}), 404  # type: ignore[return-value]
        store.add_log("WARN", f"still {name} deleted [{_client_ip()}]", "gui.camera")
        state = captures.state()
        return jsonify(
            {"ok": True, **state, "link": _capture_diagnosis(state.get("pending"))}
        )

    @app.get("/captures")
    def captures_page() -> Response:
        """The gallery. Standalone, like `/led_control` and `/debug/lidar_viz`."""
        if (response := consume_key_param(url_for("captures_page"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "captures.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- trip recordings ----------------------------------------------------
    #
    # The vessel's own account of a run, pushed up after it. See `trips.py` for
    # the resume rule and why this is not part of /api/ingest.
    #
    # Reading is behind the read gate rather than the admin gate, deliberately:
    # a recording is evidence, the same as the telemetry it was made from, and
    # the people who most want it in the tent are the ones without the key.
    # Deleting is not, because it is the only irreversible thing here.

    @app.post("/api/trip/<name>")
    def trip_upload(name: str) -> Response:
        if not boat_authorised():
            store.note_rejected()
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]

        boat = request.args.get("boat") or trips.DEFAULT_BOAT
        body = request.get_data(cache=False)
        try:
            result = recordings.accept(
                boat, name, body, request.headers.get("Content-Range")
            )
        except trips.TripError as exc:
            # `bytes_held` rides on the refusal as well as the success, so a
            # sender that has lost its place recovers from the 409 itself rather
            # than needing a second request to ask. It is carried on the error
            # rather than looked up again here, because "how much do we hold"
            # was already answered under the lock and re-reading it outside one
            # could hand back a different number than the refusal was based on.
            payload: dict[str, Any] = {"error": exc.message}
            if exc.held is not None:
                payload["bytes_held"] = exc.held
            if exc.status >= 500:
                store.add_log("ERROR", f"trip {name}: {exc.message}", "gui.trip")
            return jsonify(payload), exc.status  # type: ignore[return-value]

        if result.get("complete") and result.get("stored"):
            store.add_log(
                "INFO",
                f"trip recording {boat}/{name} received "
                f"({result['bytes_held'] / 1048576.0:.1f} MB)",
                "gui.trip",
            )
        return jsonify(result)

    @app.get("/api/trip")
    def trip_list() -> Response:
        """What is already held, so the vessel can skip it and a browser can list it.

        The boat key is accepted as well as the read gate: this is the vessel's
        first call after a reconnect, and it has a boat key rather than a cookie.
        """
        if not (may_read() or boat_authorised()):
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        response = jsonify(
            {**recordings.summary(request.args.get("boat")), "admin": is_admin()}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/trip/<boat>/<name>")
    def trip_download(boat: str, name: str) -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        try:
            path = recordings.path_for(boat, name)
        except trips.TripError as exc:
            return jsonify({"error": exc.message}), exc.status  # type: ignore[return-value]
        response = make_response(
            send_from_directory(
                path.parent, path.name, as_attachment=True, mimetype="application/gzip"
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.delete("/api/trip/<boat>/<name>")
    def trip_delete(boat: str, name: str) -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        try:
            recordings.delete(boat, name)
        except trips.TripError as exc:
            return jsonify({"error": exc.message}), exc.status  # type: ignore[return-value]
        store.add_log(
            "WARN", f"trip recording {boat}/{name} deleted [{_client_ip()}]", "gui.trip"
        )
        return jsonify({"ok": True, **recordings.summary()})

    # -- telemetry out to browsers ------------------------------------------

    @app.get("/api/snapshot")
    def snapshot() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        return jsonify(store.snapshot())

    @app.get("/api/stream")
    def stream() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        if not stream_clients.acquire(blocking=False):
            return jsonify({"error": "too many dashboard clients"}), 503  # type: ignore[return-value]

        admin = is_admin()

        def generate():
            cursor = Cursor()
            last_beat = time.time()
            try:
                initial = store.snapshot()
                cursor.state_version = initial["state_version"]
                cursor.log_id = initial["log_id"]
                cursor.command_version = initial["command_version"]
                cursor.stats_version = initial["stats_version"]
                yield _sse("hello", {"admin": admin, "server_time": time.time()})
                yield _sse("snapshot", initial)

                while True:
                    events = store.poll(cursor)
                    if events:
                        last_beat = time.time()
                        for name, payload in events:
                            yield _sse(name, payload)
                    elif time.time() - last_beat > STREAM_HEARTBEAT:
                        last_beat = time.time()
                        yield ": keepalive\n\n"
                    time.sleep(STREAM_TICK)
            except GeneratorExit:  # browser navigated away
                raise
            finally:
                stream_clients.release()

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"  # don't let nginx buffer us
        response.headers["Connection"] = "keep-alive"
        return response

    # -- the lidar debug viewer ---------------------------------------------
    #
    # `/debug/lidar_viz` is the boat and its lidar returns and nothing else: no
    # chart, no imagery, no telemetry panels, no navigation. It exists because
    # the map cannot answer the question you actually have while bolting a lidar
    # down - "is this thing seeing what I think it is seeing, at the right
    # distance, on the right side" - and it answers it with no GNSS fix, no grid
    # origin and no autopilot, none of which a bench has.
    #
    # It is deliberately standalone: one HTML file, no imports, its own endpoint.
    # A debug tool whose first dependency is the thing you are debugging is not
    # much of a debug tool, so it keeps working when the dashboard's own
    # frontend does not.

    @app.get("/api/debug/lidar")
    def debug_lidar() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        response = jsonify(store.lidar_view())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/debug/lidar_viz")
    def debug_lidar_viz() -> Response:
        if (response := consume_key_param(url_for("debug_lidar_viz"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "debug/lidar_viz.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- the LED pattern editor -----------------------------------------------
    #
    # `/led_control` lets an admin paint a solid colour, a per-pixel array, or a
    # looping multi-frame animation and push it to the hull ESP32 for real, over
    # the same `/api/command` path as everything else (`set_lights_mode` for the
    # standard/custom switch, `set_lights_pattern` for the frames,
    # `set_lights_fps` for the refresh rate). Standalone like `/debug/lidar_viz`:
    # one HTML file, no shared frontend modules.
    #
    # Not on `/` or its nav - `/control` links here and this links back, since
    # the two are used together, but neither puts it in front of someone who is
    # just watching the overview. The KILLED override in lights.py is the safety
    # guarantee; keeping this off the page a marshal is actually looking at is
    # the second one.

    @app.get("/dock")
    def dock() -> Response:
        """Task 3 and nothing else: the AR tags, three waypoints, and the cameras.

        A fifth page, same treatment as `/led_control` and `/debug/lidar_viz`: read
        gate like every other page, admin gate per command, and self-contained so
        that a change to the main dashboard's frontend cannot break the one page
        somebody is standing on a pontoon using.

        It exists because the docking task needs almost nothing the overview shows
        and one thing it does not: the boat's own latitude and longitude, large
        enough to read and with a button that copies it, because the middle waypoint
        of Task 3 is laid by driving the boat to a spot where the berth is actually
        in view and pinning it there.
        """
        if (response := consume_key_param(url_for("dock"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "dock.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/surprise_task")
    def surprise_task() -> Response:
        """The surprise task, end to end: two dockings and the buoy legs between.

        A sixth page, same treatment as `/dock` and `/led_control` - read gate like
        every other page, admin gate per command, self-contained so a change to the
        main dashboard's frontend cannot break the one page somebody is standing on
        a pontoon using.

        It is a merge of the overview and `/dock` rather than either, because this
        task is a bow-in docking, a scored buoy course and an alongside docking in
        one run with no pause between them - and because with both lidars dead the
        buoy legs need a decision nothing else on the dashboard offers: which camera
        source, if any, may create the red and green marks the boat steers by
        (`set_mark_source`). The colour mask it draws over the camera pair is the
        vessel's own hue windows, so the thresholds can be checked against the day's
        light before the boat is asked to act on them.
        """
        if (response := consume_key_param(url_for("surprise_task"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "surprise_task.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/led_control")
    def led_control() -> Response:
        if (response := consume_key_param(url_for("led_control"))) is not None:
            return response
        if not may_read():
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, "led_control.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    # Named presets, saved to disk - the one thing `/api/command` cannot do,
    # since a command only ever reaches the vessel, never a file here. Mirrors
    # `/api/tuning/profiles` below: GET behind the read gate like the page
    # itself, save/delete behind the admin gate like sending a pattern is.

    @app.get("/api/lights/effects")
    def lights_effects_list() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        response = jsonify(
            {
                # Names and frame counts only. The page re-lists every 15 s and
                # the bundled examples are a few hundred kB of hex between
                # them; frames come from the route below, one effect at a time,
                # when Load is actually pressed.
                "effects": effects.list(),
                "store": str(effects.path),
                "store_error": effects.last_error,
                "examples": str(effects.examples_path or ""),
                "examples_error": effects.examples_error,
                "admin": is_admin(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/lights/effects/<name>")
    def lights_effects_get(name: str) -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        entry = effects.entry(name)
        if entry is None:
            return jsonify({"error": f"no effect called '{name}'"}), 404  # type: ignore[return-value]
        response = jsonify({"effect": entry})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/lights/effects")
    def lights_effects_save() -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        payload = request.get_json(silent=True) or {}
        saved, why = effects.save(
            payload.get("name", ""), payload.get("frames"), by=_client_ip()
        )
        if why is not None:
            return jsonify({"error": why}), 400  # type: ignore[return-value]
        store.add_log(
            "INFO",
            f"light effect '{saved['name']}' saved with {saved['count']} "
            f"frame(s) [{_client_ip()}]",
            "gui.lights",
        )
        return jsonify({"ok": True, "effect": saved, "effects": effects.list()})

    @app.delete("/api/lights/effects/<name>")
    def lights_effects_delete(name: str) -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        if effects.is_example(name):
            # Not a 404: the name exists, it is just not the operator's to
            # delete. `tools/gen_light_effects.py` owns the examples.
            return jsonify({"error": f"'{name}' is a bundled example"}), 400  # type: ignore[return-value]
        ok, why = effects.delete(name)
        if not ok:
            return jsonify({"error": why}), 404  # type: ignore[return-value]
        store.add_log(
            "INFO", f"light effect '{name}' deleted [{_client_ip()}]", "gui.lights"
        )
        return jsonify({"ok": True, "effects": effects.list()})

    # -- telemetry in from the vessel ---------------------------------------

    @app.post("/api/ingest")
    def ingest() -> Response:
        if not boat_authorised():
            store.note_rejected()
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]

        raw = request.get_data(cache=False)
        if len(raw) > MAX_INGEST_BYTES:
            store.note_rejected()
            return jsonify({"error": "frame too large"}), 413  # type: ignore[return-value]
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            store.note_rejected()
            return jsonify({"error": f"invalid json: {exc}"}), 400  # type: ignore[return-value]
        if not isinstance(payload, dict):
            store.note_rejected()
            return jsonify({"error": "frame must be a JSON object"}), 400  # type: ignore[return-value]

        if acks := payload.pop("acks", None):
            absorb_acks(store, deployments, acks)
        commands = store.ingest(
            payload, transport="http", peer=_client_ip(), size=len(raw)
        )
        return jsonify({"ok": True, "commands": commands})

    # -- commands out to the vessel -----------------------------------------

    @app.post("/api/command")
    def command() -> Response:
        if not is_admin():
            store.add_log(
                "WARN", f"command refused, not an admin: {_client_ip()}", "gui.auth"
            )
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]

        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        spec = COMMAND_SPECS.get(name)
        if spec is None:
            return jsonify({"error": f"unknown command '{name}'"}), 400  # type: ignore[return-value]

        args = payload.get("args") or {}
        if not isinstance(args, dict):
            return jsonify({"error": "args must be an object"}), 400  # type: ignore[return-value]

        cleaned: dict[str, Any] = {}
        for key, kind in spec["args"].items():
            if key not in args:
                return jsonify({"error": f"'{name}' requires arg '{key}'"}), 400  # type: ignore[return-value]
            value = args[key]
            if kind == "float":
                try:
                    cleaned[key] = float(value)
                except (TypeError, ValueError):
                    return jsonify({"error": f"'{key}' must be a number"}), 400  # type: ignore[return-value]
            elif kind == "str":
                cleaned[key] = str(value)[:120]
            else:
                cleaned[key] = value

        if name == "set_mode":
            available = store.state.get("available_modes") or []
            if available and cleaned["mode"] not in available:
                return jsonify(  # type: ignore[return-value]
                    {"error": f"mode '{cleaned['mode']}' not offered by the vessel"}
                ), 400

        if name == "alternation":
            cleaned["on"] = bool(cleaned.get("on"))

        if name == "set_mark_source":
            # Mirrored from `self_driving/commander.set_mark_source`, for the same
            # reason `set_param`'s whitelist is mirrored: a typo comes back
            # immediately instead of sitting at "sent" until the vessel answers.
            # The vessel validates it again and the vessel's answer is the real one -
            # this only catches what can be caught without it.
            #
            # An EMPTY string is valid and means off. That is not a slip: the page's
            # Off button sends it, and rejecting it here would make "no camera marks"
            # the one setting the dashboard could not express.
            known = ("colour", "yolo")
            wanted = [
                part.strip().lower()
                for part in str(cleaned.get("sources") or "").split(",")
                if part.strip()
            ]
            unknown = [part for part in wanted if part not in known]
            if unknown:
                return jsonify(  # type: ignore[return-value]
                    {
                        "error": (
                            f"unknown mark source{'s' if len(unknown) > 1 else ''} "
                            f"{', '.join(unknown)} - the sources are "
                            f"{', '.join(known)}"
                        )
                    }
                ), 400
            cleaned["sources"] = ",".join(dict.fromkeys(wanted))

        if name == "set_param":
            # The whitelist and the ranges live in `tuning.py`, mirrored from the
            # vessel's own copy. Refusing here means the operator gets the reason
            # immediately instead of watching the command sit at "sent" for a
            # second and come back "failed".
            key, number, why = tuning.validate(cleaned.get("name"), cleaned.get("value"))
            if why is not None:
                return jsonify({"error": why}), 400  # type: ignore[return-value]
            cleaned["name"], cleaned["value"] = key, number

        if name == "set_mission":
            points = cleaned.get("points")
            if not isinstance(points, list) or not points:
                return jsonify(  # type: ignore[return-value]
                    {"error": "'points' must be a non-empty list of [x, y] pairs"}
                ), 400
            if len(points) > MAX_MISSION_WAYPOINTS:
                return jsonify(  # type: ignore[return-value]
                    {"error": f"a mission may have at most {MAX_MISSION_WAYPOINTS} waypoints"}
                ), 400
            cleaned_points: list[list[float]] = []
            for item in points:
                if not (isinstance(item, (list, tuple)) and len(item) >= 2):
                    return jsonify(  # type: ignore[return-value]
                        {"error": "each waypoint must be an [x, y] pair"}
                    ), 400
                try:
                    x, y = float(item[0]), float(item[1])
                except (TypeError, ValueError):
                    return jsonify(  # type: ignore[return-value]
                        {"error": "waypoint coordinates must be numbers"}
                    ), 400
                if not (math.isfinite(x) and math.isfinite(y)):
                    return jsonify(  # type: ignore[return-value]
                        {"error": "waypoint coordinates must be finite numbers"}
                    ), 400
                cleaned_points.append([x, y])
            cleaned["points"] = cleaned_points

        if name == "set_plan":
            # plan.validate() mirrors ligmax-pi/nodes/self_driving/plan.py's
            # `Plan.parse()`. The vessel still refuses independently - it is the
            # one that can - but a course is typed in under time pressure on a
            # competition morning, and "waypoint 7 has neither lat/lon nor x/y"
            # is worth having before the upload rather than after it.
            cleaned_plan, why = planning.validate(cleaned.get("plan"))
            if why is not None:
                return jsonify({"error": why}), 400  # type: ignore[return-value]
            cleaned["plan"] = cleaned_plan

        if name == "autopilot_goto":
            index = cleaned.get("index")
            if index is None or not math.isfinite(index) or index < 0:
                return jsonify(  # type: ignore[return-value]
                    {"error": "'index' must be a waypoint number, counting from 0"}
                ), 400
            cleaned["index"] = int(index)

        if name == "forget_object":
            # Sent as a number because that is what the args table can express,
            # but it is the integer `track_id` the chart drew and the vessel
            # matches on. Narrowed here so a stray 7.5 cannot reach the boat and
            # come back "no object 7.5" a second later.
            track = cleaned.get("id")
            if track is None or not math.isfinite(track) or track < 0:
                return jsonify(  # type: ignore[return-value]
                    {"error": "'id' must be the track id shown on the chart"}
                ), 400
            cleaned["id"] = int(track)

        if name == "set_speed_limit":
            # Refused, not clamped, and refused in both places: an operator who
            # asked for 4 m/s and silently got 2.57 would believe the boat was
            # doing 4. The upper bound is the vessel limit, so the message names
            # it rather than just quoting a number.
            value = cleaned.get("value")
            if value is None or not math.isfinite(value):
                return jsonify(  # type: ignore[return-value]
                    {"error": "'value' must be a speed in m/s"}
                ), 400
            if not MIN_SPEED_LIMIT_MS <= value <= MAX_SPEED_LIMIT_MS:
                return jsonify(  # type: ignore[return-value]
                    {
                        "error": (
                            f"'value' must be {MIN_SPEED_LIMIT_MS:g}"
                            f"..{MAX_SPEED_LIMIT_MS:.2f} m/s - 5 knots is the "
                            "vessel limit and the dashboard cannot raise it"
                        )
                    }
                ), 400

        if name == "goto":
            # Grid metres, and the vessel bounds them against its own origin
            # (guided.py's MAX_RANGE_M). Only the arithmetic is checked here: NaN
            # would otherwise travel all the way to a MAVLink int32 and land
            # somewhere real.
            for key in ("x", "y"):
                if not math.isfinite(cleaned.get(key, float("nan"))):
                    return jsonify(  # type: ignore[return-value]
                        {"error": f"'{key}' must be a finite number of grid metres"}
                    ), 400

        if name == "compass_cal":
            # Degrees true, and the vessel wraps rather than refuses - but NaN
            # and inf are not headings at all, and a float32 param carrying one
            # reaches ArduPilot's magnetic model as a number it has no answer
            # for. Refused here so the operator reads why immediately instead of
            # watching the row sit at "sent".
            heading = cleaned.get("heading")
            if heading is None or not math.isfinite(heading):
                return jsonify(  # type: ignore[return-value]
                    {"error": "'heading' must be the vessel's true heading in degrees"}
                ), 400
            cleaned["heading"] = heading % 360.0

        if name == "set_lights_pattern":
            # lights_effects.validate_frames() mirrors
            # ligmax-pi/nodes/io_manager/lights.py's `_parse_pattern()` -
            # deliberately, so a malformed pattern is a 400 here rather than a
            # "pattern rejected" the operator has to go find in the vessel log.
            # The same function backs /api/lights/effects' save path, so a
            # saved effect and a live-sent one are held to one rule, not two.
            cleaned_frames, why = lights_effects.validate_frames(cleaned.get("frames"))
            if why is not None:
                return jsonify({"error": why}), 400  # type: ignore[return-value]
            cleaned["frames"] = cleaned_frames

        if name == "set_lights_fps":
            if not (MIN_LIGHTS_FPS <= cleaned["fps"] <= MAX_LIGHTS_FPS):
                return jsonify(  # type: ignore[return-value]
                    {"error": f"'fps' must be between {MIN_LIGHTS_FPS:g} and {MAX_LIGHTS_FPS:g}"}
                ), 400

        if name == "set_lights_servos":
            # Refused, not clamped, and per side because the two travels are
            # mirrored. The vessel refuses independently and its answer is the
            # real one; this is here so a slider that has somehow got outside its
            # own bounds says so before a servo is asked to drive into a stop.
            limits = _lights_servo_limits()
            for side, bounds in limits.items():
                angle = cleaned[side]
                if not math.isfinite(angle):
                    return jsonify(  # type: ignore[return-value]
                        {"error": f"'{side}' must be an angle in degrees"}
                    ), 400
                if not bounds["min"] <= angle <= bounds["max"]:
                    return jsonify(  # type: ignore[return-value]
                        {
                            "error": (
                                f"'{side}' must be {bounds['min']:g}..{bounds['max']:g}° "
                                f"({bounds['closed']:g}° closed, {bounds['open']:g}° open) "
                                "- past an endpoint the cover is driving into its stop"
                            )
                        }
                    ), 400
                cleaned[side] = round(angle)

        queued = store.queue_command(name, cleaned, issued_by=_client_ip())
        level = spec.get("log_level") or ("ERROR" if spec.get("danger") else "INFO")
        # A pattern's `frames` can run to hundreds of hex strings - dumping the
        # whole thing into the log the way every other command's args are
        # logged would bury the one line worth grepping for later.
        if name == "set_lights_pattern":
            frames = cleaned.get("frames") or []
            loop_s = sum(f.get("hold_ms", 0) for f in frames) / 1000.0
            logged_args = f"{{'frames': {len(frames)} frame(s), {loop_s:.1f}s loop}}"
        elif name == "set_plan":
            # Same reasoning: the audit trail is read after something has gone
            # wrong, and forty lat/lon pairs in it hide the line worth finding.
            # What distinguishes one upload from the next is its shape.
            logged_args = planning.summarise(cleaned["plan"])
        else:
            logged_args = cleaned or ""
        store.add_log(
            level,
            f"operator command {name} {logged_args}".strip() + f" [{_client_ip()}]",
            "gui.command",
        )
        return jsonify({"ok": True, "command": queued.to_ui()})

    # -- deployments --------------------------------------------------------
    #
    # The operator presses a button here and the node that owns the repo collects
    # the request on a channel it already has open: the telemetry reply for a
    # COMMANDED repo, its own /pending poll for the rest. Nothing in this server
    # ever connects to a node, so no node needs an inbound port.

    @app.get("/api/deploy")
    def deploy_state() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        # A COMMANDED repo is reachable exactly when the vessel is, so the dot in
        # the panel tracks the telemetry link rather than a poll that never comes.
        # DEPLOY_LINK_WINDOW, not STALE_AFTER: one missed 1 Hz frame greys the link
        # pill honestly enough, but it must not make Update look unavailable.
        payload = deployments.snapshot(
            vessel_online=store.vessel_online(DEPLOY_LINK_WINDOW)
        )
        payload["admin"] = is_admin()
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/deploy/<repo>")
    def deploy_request(repo: str) -> Response:
        if not is_admin():
            store.add_log(
                "WARN", f"update refused, not an admin: {_client_ip()}", "gui.auth"
            )
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        if not deployments.known(repo):
            return jsonify({"error": f"unknown repo '{repo}'"}), 404  # type: ignore[return-value]

        # This repo is us. Nothing polls on our behalf, so do it directly: hand
        # off to update.py, which waits for our port to free up, pulls, and
        # starts us again. Everything is RAM-only, so there is nothing to flush.
        if repo == SELF_REPO:
            store.add_log(
                "WARN", f"self-update: restarting [{_client_ip()}]", "gui.deploy"
            )
            subprocess.Popen(
                [sys.executable, "update.py"],
                cwd=str(REPO_ROOT),
                creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
            )
            # Exit from a thread so this response reaches the browser first.
            threading.Thread(
                target=lambda: (time.sleep(1), os._exit(0)), daemon=True
            ).start()
            return jsonify({"ok": True, "restarting": True})

        state = deployments.request(repo, issued_by=_client_ip())

        # A COMMANDED repo's node never polls /pending. Send the request down the
        # telemetry channel instead - the same one that carries estop - so it needs
        # no second secret and no second connection. The ack comes back the same
        # way and absorb_acks() turns it into this row's result.
        if repo in COMMANDED:
            queued = store.queue_command(
                "update", {"repo": repo}, issued_by=_client_ip()
            )
            store.add_log(
                "WARN",
                f"update commanded for {repo} as {queued.id}; propulsion power drops "
                f"for the restart [{_client_ip()}]",
                "gui.deploy",
            )
            return jsonify({"ok": True, "repo": state, "command": queued.to_ui()})

        store.add_log(
            "INFO", f"update requested for {repo} [{_client_ip()}]", "gui.deploy"
        )
        return jsonify({"ok": True, "repo": state})

    @app.post("/api/deploy/<repo>/cancel")
    def deploy_cancel(repo: str) -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        if not deployments.known(repo):
            return jsonify({"error": f"unknown repo '{repo}'"}), 404  # type: ignore[return-value]
        state = deployments.cancel(repo)
        store.add_log(
            "INFO", f"update request for {repo} cancelled [{_client_ip()}]", "gui.deploy"
        )
        return jsonify({"ok": True, "repo": state})

    @app.get("/api/deploy/<repo>/pending")
    def deploy_pending(repo: str) -> Response:
        if not node_authorised():
            return jsonify({"error": "node key required"}), 403  # type: ignore[return-value]
        if not deployments.known(repo):
            return jsonify({"error": f"unknown repo '{repo}'"}), 404  # type: ignore[return-value]
        response = jsonify(deployments.pending(repo))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/deploy/<repo>/report")
    def deploy_report(repo: str) -> Response:
        if not node_authorised():
            return jsonify({"error": "node key required"}), 403  # type: ignore[return-value]
        if not deployments.known(repo):
            return jsonify({"error": f"unknown repo '{repo}'"}), 404  # type: ignore[return-value]

        payload = request.get_json(silent=True) or {}
        result = str(payload.get("result", "")).strip()
        message = str(payload.get("message", ""))
        head = payload.get("head")
        nonce = payload.get("nonce")

        accepted, why = deployments.report(
            repo,
            nonce=str(nonce) if nonce else None,
            result=result,
            message=message,
            head=str(head) if head else None,
        )
        if not accepted:
            return jsonify({"error": why}), 400  # type: ignore[return-value]

        level = "ERROR" if result == "failed" else "WARN" if result == "refused" else "INFO"
        detail = f" - {message}" if message else ""
        store.add_log(
            level,
            f"{repo} update {result}{detail}"
            + (f" @ {str(head)[:8]}" if head else ""),
            "gui.deploy",
        )
        return jsonify({"ok": True})

    # -- stabilisation tuning -----------------------------------------------
    #
    # The values themselves are not served here: they arrive with the telemetry as
    # `telemetry.tuning.values`, read off the flight controller by the vessel, and
    # the panel reads them out of the same store as every other measurement. What
    # these routes carry is the *table* - which parameters exist, their ranges and
    # their help text - and the saved profiles, which live on this box.
    #
    # Writing a value is not a route: it is the ordinary `set_param` operator
    # command, so it queues, expires and is audited exactly like an E-stop.

    def _vessel_values() -> dict[str, Any]:
        block = (store.state.get("telemetry") or {}).get("tuning") or {}
        values = block.get("values")
        return values if isinstance(values, dict) else {}

    @app.get("/api/tuning")
    def tuning_state() -> Response:
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        response = jsonify(
            {
                **tuning.spec_payload(),
                "profiles": profiles.list(),
                "store": str(profiles.path),
                "store_error": profiles.last_error,
                "admin": is_admin(),
                "server_time": time.time(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/tuning/profiles")
    def tuning_profile_save() -> Response:
        """Snapshot the tuning under a name. Defaults to what the vessel reports.

        Sending no `values` is the ordinary case: the operator has just tuned the
        boat and wants *what is on it right now* recorded, and the browser's idea
        of that could be a stale field it never refreshed.
        """
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        payload = request.get_json(silent=True) or {}
        values = payload.get("values")
        if not isinstance(values, dict) or not values:
            values = _vessel_values()
        saved, why = profiles.save(
            payload.get("name", ""),
            values,
            by=_client_ip(),
            note=str(payload.get("note") or ""),
        )
        if why is not None:
            return jsonify({"error": why}), 400  # type: ignore[return-value]
        store.add_log(
            "INFO",
            f"tuning profile '{saved['name']}' saved with {saved['count']} "
            f"value(s) [{_client_ip()}]",
            "gui.tuning",
        )
        return jsonify({"ok": True, "profile": saved, "profiles": profiles.list()})

    @app.post("/api/tuning/profiles/<name>/apply")
    def tuning_profile_apply(name: str) -> Response:
        """Queue a `set_param` for every value in the profile the vessel does not
        already have.

        One command per value rather than one bulk write, so each lands in the
        audit trail with its own ack: a profile that half-applied because one
        parameter is missing has to be visible as exactly that.
        """
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        saved = profiles.get(name)
        if saved is None:
            return jsonify({"error": f"no profile called '{name}'"}), 404  # type: ignore[return-value]

        live = _vessel_values()
        queued: list[dict[str, Any]] = []
        skipped: list[str] = []
        for key, value in saved.items():
            spec_name, number, why = tuning.validate(key, value)
            if why is not None or spec_name is None or number is None:
                # A read-only or out-of-range entry in an old profile is skipped
                # rather than failing the whole apply.
                skipped.append(f"{key}: {why or 'not applicable'}")
                continue
            current = live.get(spec_name)
            if isinstance(current, (int, float)) and abs(float(current) - number) < 1e-9:
                continue  # already there; do not spend a command on it
            queued.append(
                store.queue_command(
                    "set_param", {"name": spec_name, "value": number},
                    issued_by=_client_ip(),
                ).to_ui()
            )
        store.add_log(
            "WARN" if queued else "INFO",
            f"tuning profile '{name}' applied: {len(queued)} value(s) queued"
            + (f", {len(skipped)} skipped" if skipped else "")
            + f" [{_client_ip()}]",
            "gui.tuning",
        )
        return jsonify({"ok": True, "queued": queued, "skipped": skipped})

    @app.delete("/api/tuning/profiles/<name>")
    def tuning_profile_delete(name: str) -> Response:
        if not is_admin():
            return jsonify({"error": "admin session required"}), 403  # type: ignore[return-value]
        ok, why = profiles.delete(name)
        if not ok:
            return jsonify({"error": why}), 404  # type: ignore[return-value]
        store.add_log("INFO", f"tuning profile '{name}' deleted [{_client_ip()}]",
                      "gui.tuning")
        return jsonify({"ok": True, "profiles": profiles.list()})

    # -- RTK ----------------------------------------------------------------

    @app.get("/api/rtk")
    def rtk_status() -> Response:
        """Is the base station up, and how old are its corrections?

        Behind the read gate rather than the admin gate: it is status, not
        control, and it carries no credentials. The vessel's own view of the
        same link rides up as `telemetry.rtk` - the two are worth comparing,
        because corrections reaching this caster is not the same as corrections
        reaching the receiver.
        """
        if not may_read():
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]
        if caster is None:
            return jsonify({"enabled": False})
        return jsonify({"enabled": True, **caster.status()})

    # -- static -------------------------------------------------------------

    @app.get("/<path:filename>")
    def static_files(filename: str) -> Response:
        if (response := consume_key_param(url_for("index"))) is not None:
            return response
        if not may_read() and not filename.startswith("assets/"):
            return deny_read()
        response = make_response(send_from_directory(WEB_ROOT, filename))
        if filename.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    return app


# --- UDP ingest -------------------------------------------------------------


def serve_udp(
    config: Config,
    store: Store,
    stop: threading.Event,
    deployments: DeployRegistry | None = None,
) -> None:
    """Receive JSON frames over UDP and reply with any queued commands.

    `deployments` is optional only so a test can start the listener alone; pass
    it from run.py, or an `update` acked over UDP never reaches the panel.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((config.udp_host, config.udp_port))
    except OSError as exc:
        store.add_log(
            "ERROR",
            f"UDP ingest disabled, cannot bind {config.udp_host}:{config.udp_port} "
            f"({exc}). POST /api/ingest still works.",
            "gui.udp",
        )
        return
    sock.settimeout(0.5)
    store.add_log(
        "INFO", f"UDP telemetry ingest on {config.udp_host}:{config.udp_port}", "gui.udp"
    )

    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(UDP_RECV_SIZE)
        except socket.timeout:
            continue
        except OSError:
            break

        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("frame must be a JSON object")
        except ValueError as exc:
            store.note_rejected()
            store.add_log("WARN", f"bad UDP frame from {addr[0]}: {exc}", "gui.udp")
            continue

        if config.boat_key and not auth.key_matches(
            payload.pop("auth", None), config.boat_key
        ):
            store.note_rejected()
            continue

        if acks := payload.pop("acks", None):
            absorb_acks(store, deployments, acks)

        commands = store.ingest(
            payload, transport="udp", peer=f"{addr[0]}:{addr[1]}", size=len(data)
        )
        if commands:
            reply = json.dumps({"commands": commands}, separators=(",", ":"))
            try:
                sock.sendto(reply.encode("utf-8"), addr)
            except OSError:
                pass

    sock.close()


def serve_housekeeping(
    store: Store, stop: threading.Event, recordings: trips.TripStore | None = None
) -> None:
    """Expire stale commands, and sweep up abandoned partial uploads.

    `recordings` is optional so a test can run the loop alone. The sweep is on a
    long counter rather than every tick: an abandoned `.part` blocks its own name
    until it is removed, but nothing about that is urgent, and walking the trip
    directory twice a second would be silly.
    """
    ticks = 0
    while not stop.wait(2.0):
        store.expire_commands()
        ticks += 1
        if recordings is not None and ticks % 300 == 0:  # ~every 10 minutes
            if removed := recordings.sweep():
                store.add_log(
                    "INFO",
                    f"dropped {removed} abandoned trip upload(s)",
                    "gui.trip",
                )
