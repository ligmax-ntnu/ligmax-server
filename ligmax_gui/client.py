"""Vessel-side client. Import this from the autonomy code.

    from ligmax_gui.client import GuiClient

    gui = GuiClient("udp://127.0.0.1:8771", key="...")
    gui.attach_logging()          # mirror the stdlib `logging` tree to the GUI

    while running:
        gui.publish(
            mode=mode.name,
            boat=boat,                       # a shared_settings.Boat works as-is
            tracks=tracks,                   # dicts, or objects with the fields
            path=planned_path,               # list of [x, y]
            upstream_direction=env.upstream_direction,
            origin=(lat, lon),
            telemetry={
                "battery": {"soc": 0.87, "voltage": 48.2, "current": 12.4},
                "gimbal": {"pitch": 0.4, "roll": -1.2},
            },
        )

        for command in gui.commands():       # operator input, never blocks
            handle(command)

Design rules, because this runs inside a control loop:

  * `publish()` never blocks and never raises.  UDP is fire-and-forget; HTTP
    hands off to a background thread with a latest-frame-wins outbox, so a
    stalled network cannot back-pressure autonomy.
  * numpy arrays, enums and `shared_settings` objects are accepted directly.
  * a dropped frame is not worth a retry - the next one is 100 ms away.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlparse

# The only intra-package import here, and a deliberate one: `status` is a closed
# vocabulary and both ends have to agree on it, so it is defined once.
from . import protocol

_MAX_QUEUED_LOGS = 400
_UDP_SAFE_BYTES = 60000  # below the 65507-byte datagram ceiling


def _to_jsonable(value: Any) -> Any:
    """numpy -> list, Enum -> value, everything else left alone."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "value") and type(value).__mro__[1].__name__ == "Enum":
        return value.value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _track_to_dict(track: Any) -> dict[str, Any] | None:
    """Accept a dict, or any object exposing the track attributes."""
    if isinstance(track, dict):
        return {k: _to_jsonable(v) for k, v in track.items()}
    fields = ("track_id", "position", "type", "confidence", "avoid_radius")
    if not any(hasattr(track, f) for f in fields):
        return None
    out: dict[str, Any] = {}
    for name in (*fields, "heading", "velocity", "age", "hits", "source", "label"):
        if hasattr(track, name):
            out[name] = _to_jsonable(getattr(track, name))
    return out


def _boat_to_dict(boat: Any) -> dict[str, Any] | None:
    if boat is None:
        return None
    if isinstance(boat, dict):
        return {k: _to_jsonable(v) for k, v in boat.items()}
    out: dict[str, Any] = {}
    for name in ("position", "velocity", "heading", "radius"):
        if hasattr(boat, name):
            out[name] = _to_jsonable(getattr(boat, name))
    return out or None


class GuiLogHandler(logging.Handler):
    """Feeds stdlib log records to a `GuiClient`, dropping them if it backs up."""

    def __init__(self, client: "GuiClient") -> None:
        super().__init__()
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._client.log(
                record.levelname,
                record.getMessage()
                if not record.exc_info
                else self.format(record),
                name=record.name,
                t=record.created,
            )
        except Exception:  # a logging handler must never break the caller
            pass


class GuiClient:
    """Push telemetry to the dashboard and pull operator commands back."""

    def __init__(
        self,
        target: str = "udp://127.0.0.1:8771",
        key: str | None = None,
        *,
        min_interval: float = 0.05,
        on_command: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """`target` is `udp://host:port` or `http://host:port` (the dashboard root)."""
        parsed = urlparse(target if "://" in target else f"udp://{target}")
        self.scheme = parsed.scheme.lower()
        if self.scheme not in ("udp", "http", "https"):
            raise ValueError(f"unsupported GUI target scheme: {parsed.scheme!r}")

        self.key = key
        self.min_interval = min_interval
        self._on_command = on_command

        self._seq = 0
        self._last_publish = 0.0
        self._logs: queue.Queue[dict[str, Any]] = queue.Queue(_MAX_QUEUED_LOGS)
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue(256)
        self._acks: list[dict[str, Any]] = []
        self._acks_lock = threading.Lock()
        self._log_handler: GuiLogHandler | None = None
        self._closed = False

        self.dropped_frames = 0
        self.dropped_logs = 0
        self.sent_frames = 0
        self.last_error: str | None = None

        if self.scheme == "udp":
            self._addr = (parsed.hostname or "127.0.0.1", parsed.port or 8771)
            self._sock: socket.socket | None = socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM
            )
            # Bind to an ephemeral port up front. Without an explicit bind,
            # recvfrom() on a socket that has not sent anything yet fails with
            # WSAEINVAL on Windows, which would kill the reader thread before
            # the first command ever arrived.
            self._sock.bind(("0.0.0.0", 0))
            self._sock.setblocking(False)
            self._reader = threading.Thread(
                target=self._udp_reader, daemon=True, name="gui-udp-reader"
            )
            self._reader.start()
        else:
            base = f"{self.scheme}://{parsed.netloc}"
            self._url = f"{base}{parsed.path.rstrip('/')}/api/ingest"
            self._sock = None
            self._outbox: dict[str, Any] | None = None  # latest frame wins
            self._outbox_ready = threading.Event()
            self._outbox_lock = threading.Lock()
            self._sender = threading.Thread(
                target=self._http_sender, daemon=True, name="gui-http-sender"
            )
            self._sender.start()

    # -- public API ---------------------------------------------------------

    def attach_logging(
        self, logger: logging.Logger | None = None, level: int = logging.DEBUG
    ) -> GuiLogHandler:
        """Mirror an existing logger (the root by default) into the dashboard."""
        handler = GuiLogHandler(self)
        handler.setLevel(level)
        (logger or logging.getLogger()).addHandler(handler)
        self._log_handler = handler
        return handler

    def log(
        self, level: str, message: str, name: str = "boat", t: float | None = None
    ) -> None:
        """Queue a log line for the next `publish()`."""
        entry = {
            "level": str(level).upper(),
            "msg": str(message),
            "name": name,
            "t": t if t is not None else time.time(),
        }
        try:
            self._logs.put_nowait(entry)
        except queue.Full:
            self.dropped_logs += 1

    def publish(
        self,
        *,
        boat: Any = None,
        tracks: Iterable[Any] | None = None,
        path: Any = None,
        paths: Sequence[Any] | None = None,
        scan: Any = None,
        telemetry: dict[str, Any] | None = None,
        status: Any = None,
        mode: Any = None,
        estop: bool | None = None,
        available_modes: Sequence[str] | None = None,
        origin: Any = None,
        upstream_direction: Any = None,
        grid_bearing: float | None = None,
        status_text: str | None = None,
        force: bool = False,
        **extra: Any,
    ) -> bool:
        """Send one frame. Returns False if it was rate-limited or dropped.

        Rate limiting is on wall-clock, so calling this every control tick is
        fine - pass `force=True` for a frame you must not lose (a mode change,
        say).  Queued log lines always ride along on the next frame that goes
        out, so nothing is lost to throttling.
        """
        if self._closed:
            return False

        now = time.time()
        throttled = not force and (now - self._last_publish) < self.min_interval
        if throttled and self._logs.empty():
            return False

        frame: dict[str, Any] = {"seq": self._seq, "t": now}
        self._seq += 1

        if (boat_dict := _boat_to_dict(boat)) is not None:
            frame["boat"] = boat_dict
        if tracks is not None:
            frame["tracks"] = [
                item for track in tracks if (item := _track_to_dict(track)) is not None
            ]
        if path is not None:
            frame["path"] = _to_jsonable(path)
        if paths is not None:
            frame["paths"] = [_to_jsonable(p) for p in paths]
        if scan is not None:
            frame["scan"] = _to_jsonable(scan)
        if telemetry is not None:
            frame["telemetry"] = _to_jsonable(telemetry)
        # `status` is the closed vocabulary (protocol.VESSEL_STATUS) that drives the
        # operator's status indicator and the colour of the hull lights; `mode` is
        # whatever the autopilot calls itself, free text.
        #
        # An unrecognised name is dropped and recorded rather than raised on -
        # publish() never raises, because it is called from a control loop - and
        # rather than passed through, because the dashboard would drop it anyway
        # and `last_error` is where someone will actually find out why.
        if status is not None:
            name = getattr(status, "name", None) or str(status)
            if (canonical := protocol.normalise_status(name)) is not None:
                frame["status"] = canonical
            else:
                self.last_error = (
                    f"status {name!r} is not one of {protocol.VESSEL_STATUS}, dropped"
                )
        if mode is not None:
            frame["mode"] = getattr(mode, "name", None) or str(mode)
        if estop is not None:
            frame["estop"] = bool(estop)
        if available_modes is not None:
            frame["available_modes"] = [
                getattr(m, "name", None) or str(m) for m in available_modes
            ]
        if origin is not None:
            frame["origin"] = _to_jsonable(origin)
        if upstream_direction is not None:
            frame["upstream_direction"] = _to_jsonable(upstream_direction)
        if grid_bearing is not None:
            frame["grid_bearing"] = float(grid_bearing)
        if status_text is not None:
            frame["status_text"] = str(status_text)
        for key, value in extra.items():
            frame[key] = _to_jsonable(value)

        logs: list[dict[str, Any]] = []
        while len(logs) < 200:
            try:
                logs.append(self._logs.get_nowait())
            except queue.Empty:
                break
        if logs:
            frame["logs"] = logs

        with self._acks_lock:
            if self._acks:
                frame["acks"] = self._acks
                self._acks = []

        self._last_publish = now
        return self._send(frame)

    def commands(self) -> list[dict[str, Any]]:
        """Drain operator commands received since the last call. Never blocks."""
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._commands.get_nowait())
            except queue.Empty:
                return out

    def ack(self, command_id: str, status: str = "acked", result: str | None = None) -> None:
        """Report a command's outcome; rides along on the next frame."""
        ack: dict[str, Any] = {"id": command_id, "status": status}
        if result is not None:
            ack["result"] = result
        with self._acks_lock:
            self._acks.append(ack)

    def close(self) -> None:
        self._closed = True
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def __enter__(self) -> "GuiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- transports ---------------------------------------------------------

    def _encode(self, frame: dict[str, Any]) -> bytes | None:
        if self.key:
            frame = {**frame, "auth": self.key} if self.scheme == "udp" else frame
        try:
            return json.dumps(frame, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            # Don't let one unserialisable telemetry value kill the stream.
            self.last_error = f"frame not serialisable: {exc}"
            self.dropped_frames += 1
            return None

    def _send(self, frame: dict[str, Any]) -> bool:
        payload = self._encode(frame)
        if payload is None:
            return False

        if self.scheme == "udp":
            if len(payload) > _UDP_SAFE_BYTES:
                # Almost always an oversized lidar scan; drop it, keep the rest.
                frame.pop("scan", None)
                frame.setdefault("logs", []).append(
                    {
                        "level": "WARN",
                        "name": "gui.client",
                        "msg": f"frame was {len(payload)} B, dropped `scan` to fit "
                        "a UDP datagram - decimate it on the vessel or use HTTP",
                        "t": time.time(),
                    }
                )
                payload = self._encode(frame)
                if payload is None or len(payload) > _UDP_SAFE_BYTES:
                    self.dropped_frames += 1
                    return False
            try:
                assert self._sock is not None
                self._sock.sendto(payload, self._addr)
                self.sent_frames += 1
                return True
            except (OSError, AssertionError) as exc:
                self.last_error = str(exc)
                self.dropped_frames += 1
                return False

        with self._outbox_lock:
            if self._outbox is not None:
                self.dropped_frames += 1  # superseded before it went out
            self._outbox = frame
        self._outbox_ready.set()
        return True

    def _udp_reader(self) -> None:
        assert self._sock is not None
        while not self._closed:
            try:
                data, _ = self._sock.recvfrom(65535)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except ConnectionResetError:
                # Windows reports "port unreachable" from an earlier sendto()
                # as an error on the *next* receive. It means the dashboard is
                # not listening yet, which is temporary - keep waiting.
                time.sleep(0.1)
                continue
            except OSError as exc:
                if self._closed:
                    return
                self.last_error = f"udp reader: {exc}"
                time.sleep(0.2)
                continue
            self._absorb_reply(data)

    def _http_sender(self) -> None:
        while not self._closed:
            if not self._outbox_ready.wait(0.2):
                continue
            with self._outbox_lock:
                frame, self._outbox = self._outbox, None
                self._outbox_ready.clear()
            if frame is None:
                continue

            payload = self._encode(frame)
            if payload is None:
                continue
            request = urllib.request.Request(
                self._url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {self.key}"} if self.key else {}),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    self.sent_frames += 1
                    self.last_error = None
                    self._absorb_reply(response.read())
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self.last_error = str(exc)
                self.dropped_frames += 1
                time.sleep(0.5)  # back off rather than hammer a dead server

    def _absorb_reply(self, data: bytes) -> None:
        try:
            payload = json.loads(data or b"{}")
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        for command in payload.get("commands") or []:
            if not isinstance(command, dict):
                continue
            if self._on_command is not None:
                try:
                    self._on_command(command)
                except Exception as exc:  # callback bugs stay contained
                    self.last_error = f"on_command raised: {exc}"
                continue
            try:
                self._commands.put_nowait(command)
            except queue.Full:
                pass
