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
"""

from __future__ import annotations

import json
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

from . import auth, protocol
from .config import Config, REPO_ROOT, WEB_ROOT, load_config
from .deploy import COMMANDED, DeployRegistry
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

# Commands the dashboard is allowed to forward.  An allow-list, so a stray
# fetch() from a browser console cannot invent new vessel behaviour.
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
    "clear_waypoints": {"label": "Clear waypoints", "args": {}},
    "set_speed_limit": {"label": "Speed limit", "args": {"value": "float"}},
    "recentre_origin": {"label": "Re-zero grid origin", "args": {}, "confirm": True},
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
                "server_time": time.time(),
                "shared_settings": protocol.SHARED_SETTINGS_AVAILABLE,
            }
        )

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

        queued = store.queue_command(name, cleaned, issued_by=_client_ip())
        level = "ERROR" if spec.get("danger") else "INFO"
        store.add_log(
            level,
            f"operator command {name} {cleaned or ''}".strip() + f" [{_client_ip()}]",
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
