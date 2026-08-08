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

from . import auth, lights_effects, plan as planning, protocol, tuning
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
    "hold": {"label": "Hold position", "args": {}},
    "resume": {"label": "Resume mission", "args": {}},
    "arm": {"label": "Arm propulsion", "args": {}, "confirm": True},
    "disarm": {"label": "Disarm propulsion", "args": {}},
    "goto": {"label": "Go to point", "args": {"x": "float", "y": "float"}},
    # An admin-laid route, grid metres like `goto` - see
    # ligmax-pi/nodes/io_manager/mission.py. The vessel uploads it as a real
    # MAVLink mission and echoes it back as the map's "ideal route" layer once
    # accepted; running it is a separate `set_mode` to AUTO plus `arm`, so laying
    # a route and setting it moving are always two distinct, audited commands.
    "set_mission": {"label": "Send mission", "args": {"points": "any"}, "confirm": True},
    "clear_waypoints": {"label": "Clear waypoints", "args": {}},
    "set_speed_limit": {"label": "Speed limit", "args": {"value": "float"}},
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
    # Between tasks: the world model keeps marks for a few seconds after they go
    # out of view, and the last task's buoys are not this task's.
    "forget_world": {"label": "Clear what it has seen", "args": {}},
    "raw": {"label": "Raw command", "args": {"payload": "any"}},
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
                "max_waypoints": planning.MAX_WAYPOINTS,
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

    @app.post("/api/camera")
    def camera_ingest() -> Response:
        if not boat_authorised():
            store.note_rejected()
            cameras.note_refused("frame POST, wrong or missing boat key")
            return jsonify({"error": "unauthorised"}), 401  # type: ignore[return-value]

        # Nothing is stored while the stream is off, so a sender that ignores the
        # config cannot keep the panel alive - and the reply tells it to stop.
        if not cameras.enabled:
            return jsonify({"ok": False, "enabled": False, **cameras.poll()})  # type: ignore[return-value]

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
        return jsonify({"ok": True, **cameras.poll()})

    @app.get("/api/camera/config")
    def camera_config() -> Response:
        """What the Jetson should be sending. Polled outbound, like /pending."""
        if not boat_authorised() and not node_authorised():
            # Recorded, because this is the failure that looks like nothing:
            # the poll never reaches poll(), so the panel would otherwise report
            # a Jetson that has "never asked" while it is asking every 5 s.
            cameras.note_refused("config poll, wrong or missing boat key")
            return jsonify({"error": "boat key required"}), 403  # type: ignore[return-value]
        response = jsonify(cameras.poll())
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


def serve_housekeeping(store: Store, stop: threading.Event) -> None:
    while not stop.wait(2.0):
        store.expire_commands()
