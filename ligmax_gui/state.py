"""Live vessel state, log ring buffer and operator command queue.

One `Store` instance owns everything and is safe to touch from any thread
(the UDP listener, each Flask request, and the SSE streams all do).

Fan-out uses cursors rather than per-client queues.  A browser tells the store
"I last saw state version 812 and log id 4400"; the store hands back only
what is new.  A slow client therefore *coalesces* — it misses intermediate
telemetry frames instead of accumulating a backlog — while logs and command
updates are still delivered exactly once, in order.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import protocol

# Telemetry keys that are merged one level deep rather than replaced, so the
# BMS task and the gimbal task can publish independently without clobbering
# each other.
_MERGE_DEPTH = 2

HISTORY_SAMPLE_INTERVAL = 1.0  # seconds between sparkline samples
HISTORY_LENGTH = 180  # ~3 minutes of context for a freshly opened tab
HISTORY_MAX_FIELDS = 160
STALE_AFTER = 3.0  # seconds without a frame before the link reads "stale"


@dataclass
class Cursor:
    """What a single SSE client has already been sent."""

    state_version: int = -1
    log_id: int = -1
    command_version: int = -1
    stats_version: int = -1


@dataclass
class Command:
    id: str
    name: str
    args: dict[str, Any]
    issued_at: float
    issued_by: str = "operator"
    status: str = "queued"  # queued -> delivered -> acked | failed | expired
    delivered_at: float | None = None
    acked_at: float | None = None
    result: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": self.args,
                "issued_at": self.issued_at}

    def to_ui(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "args": self.args,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "status": self.status,
            "delivered_at": self.delivered_at,
            "acked_at": self.acked_at,
            "result": self.result,
        }


def _merge(dest: dict[str, Any], src: dict[str, Any], depth: int = 0) -> None:
    for key, value in src.items():
        if (
            depth < _MERGE_DEPTH
            and isinstance(value, dict)
            and isinstance(dest.get(key), dict)
        ):
            _merge(dest[key], value, depth + 1)
        else:
            dest[key] = value


def _flatten_numeric(
    value: Any, prefix: str = "", out: dict[str, float] | None = None
) -> dict[str, float]:
    """`{"battery": {"soc": 91}}` -> `{"battery.soc": 91.0}`, numbers only."""
    if out is None:
        out = {}
    if isinstance(value, bool):
        out[prefix] = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        out[prefix] = float(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _flatten_numeric(item, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, (int, float, bool)):
                _flatten_numeric(item, f"{prefix}[{index}]", out)
    return out


class Store:
    def __init__(self, max_logs: int = 4000, max_scan_points: int = 1500) -> None:
        self._lock = threading.RLock()
        self._max_scan_points = max_scan_points

        self.state: dict[str, Any] = {
            "mode": None,
            "estop": False,
            "available_modes": [],
            "origin": None,
            "grid_bearing": 0.0,
            "upstream_direction": [0.0, 1.0],
            "boat": None,
            "tracks": [],
            "paths": [],
            "scan": None,
            "telemetry": {},
        }
        self._state_version = 0

        self._logs: deque[dict[str, Any]] = deque(maxlen=max_logs)
        self._log_ids = itertools.count(1)
        self._last_log_id = 0

        self._commands: deque[Command] = deque(maxlen=200)
        self._command_ids = itertools.count(1)
        self._command_version = 0

        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LENGTH)
        self._last_history_at = 0.0

        self._stats_version = 0
        self.stats: dict[str, Any] = {
            "connected": False,
            "frames": 0,
            "bytes": 0,
            "hz": 0.0,
            "last_frame_at": None,
            "last_frame_age": None,
            "last_seq": None,
            "seq_gaps": 0,
            "transport": None,
            "peer": None,
            "rejected": 0,
            "boat_clock_offset": None,
        }
        self._frame_times: deque[float] = deque(maxlen=60)

    # -- ingest -------------------------------------------------------------

    def ingest(
        self,
        raw: dict[str, Any],
        *,
        transport: str = "http",
        peer: str | None = None,
        size: int = 0,
    ) -> list[dict[str, Any]]:
        """Merge a boat frame; return commands to hand back to the vessel."""
        frame = protocol.normalise_frame(raw, max_scan_points=self._max_scan_points)
        now = time.time()

        with self._lock:
            logs = frame.pop("logs", None)
            if logs:
                for entry in logs:
                    entry.setdefault("t", now)
                    entry["id"] = self._last_log_id = next(self._log_ids)
                    self._logs.append(entry)

            if "telemetry" in frame:
                telemetry = self.state.setdefault("telemetry", {})
                _merge(telemetry, frame.pop("telemetry"))

            if frame:
                _merge(self.state, frame)
            self._state_version += 1

            seq = frame.get("seq")
            if seq is not None:
                previous = self.stats["last_seq"]
                if isinstance(previous, int) and seq > previous + 1:
                    self.stats["seq_gaps"] += seq - previous - 1
                self.stats["last_seq"] = seq

            self._frame_times.append(now)
            self.stats["frames"] += 1
            self.stats["bytes"] += size
            self.stats["last_frame_at"] = now
            self.stats["connected"] = True
            self.stats["transport"] = transport
            if peer:
                self.stats["peer"] = peer
            if (boat_time := frame.get("t")) is not None:
                self.stats["boat_clock_offset"] = round(now - boat_time, 3)
            if len(self._frame_times) >= 2:
                span = self._frame_times[-1] - self._frame_times[0]
                if span > 0:
                    self.stats["hz"] = round((len(self._frame_times) - 1) / span, 2)
            self._stats_version += 1

            self._sample_history(now)
            return self._take_pending_commands(now)

    def _sample_history(self, now: float) -> None:
        if now - self._last_history_at < HISTORY_SAMPLE_INTERVAL:
            return
        self._last_history_at = now
        sample = _flatten_numeric(self.state.get("telemetry") or {})

        boat = self.state.get("boat") or {}
        if isinstance(velocity := boat.get("velocity"), list) and len(velocity) >= 2:
            sample["derived.speed"] = (velocity[0] ** 2 + velocity[1] ** 2) ** 0.5
        sample["derived.track_count"] = float(len(self.state.get("tracks") or []))
        sample["derived.link_hz"] = float(self.stats.get("hz") or 0.0)

        if len(sample) > HISTORY_MAX_FIELDS:
            sample = dict(itertools.islice(sample.items(), HISTORY_MAX_FIELDS))
        self._history.append({"t": now, "v": sample})

    def note_rejected(self) -> None:
        with self._lock:
            self.stats["rejected"] += 1
            self._stats_version += 1

    def add_log(self, level: str, message: str, name: str = "gui") -> None:
        """Server-side log line, shown in the console alongside vessel logs."""
        entry = protocol.normalise_log({"level": level, "msg": message, "name": name})
        if entry is None:
            return
        with self._lock:
            entry["t"] = time.time()
            entry["id"] = self._last_log_id = next(self._log_ids)
            self._logs.append(entry)

    # -- commands -----------------------------------------------------------

    def queue_command(
        self, name: str, args: dict[str, Any] | None = None, issued_by: str = "operator"
    ) -> Command:
        with self._lock:
            command = Command(
                id=f"c{next(self._command_ids)}",
                name=name,
                args=args or {},
                issued_at=time.time(),
                issued_by=issued_by,
            )
            self._commands.append(command)
            self._command_version += 1
            return command

    def _take_pending_commands(self, now: float) -> list[dict[str, Any]]:
        pending = [c for c in self._commands if c.status == "queued"]
        for command in pending:
            command.status = "delivered"
            command.delivered_at = now
        if pending:
            self._command_version += 1
        return [c.to_wire() for c in pending]

    def ack_commands(self, acks: Iterable[Any]) -> int:
        """Apply `{"id": "c3", "status": "acked", "result": "..."}` reports."""
        applied = 0
        now = time.time()
        with self._lock:
            by_id = {c.id: c for c in self._commands}
            for ack in acks or []:
                if isinstance(ack, str):
                    ack = {"id": ack}
                if not isinstance(ack, dict):
                    continue
                command = by_id.get(str(ack.get("id")))
                if command is None:
                    continue
                status = str(ack.get("status", "acked")).lower()
                command.status = status if status in ("acked", "failed") else "acked"
                command.acked_at = now
                if ack.get("result") is not None:
                    command.result = str(ack["result"])[:400]
                applied += 1
            if applied:
                self._command_version += 1
        return applied

    def expire_commands(self, timeout: float = 20.0) -> None:
        now = time.time()
        with self._lock:
            changed = False
            for command in self._commands:
                if (
                    command.status in ("queued", "delivered")
                    and now - command.issued_at > timeout
                ):
                    command.status = "expired"
                    changed = True
            if changed:
                self._command_version += 1

    # -- read paths ---------------------------------------------------------

    def _refresh_liveness(self) -> None:
        last = self.stats.get("last_frame_at")
        if last is None:
            self.stats["last_frame_age"] = None
            return
        age = time.time() - last
        self.stats["last_frame_age"] = round(age, 3)
        was_connected = self.stats["connected"]
        self.stats["connected"] = age < STALE_AFTER
        if was_connected != self.stats["connected"]:
            self._stats_version += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_liveness()
            return {
                "state": self.state,
                "stats": dict(self.stats),
                "logs": list(self._logs)[-400:],
                "commands": [c.to_ui() for c in list(self._commands)[-40:]],
                "history": list(self._history),
                "state_version": self._state_version,
                "log_id": self._last_log_id,
                "command_version": self._command_version,
                "stats_version": self._stats_version,
                "server_time": time.time(),
            }

    def poll(self, cursor: Cursor) -> list[tuple[str, Any]]:
        """Return `(event, payload)` pairs new since `cursor`, advancing it."""
        with self._lock:
            self._refresh_liveness()
            events: list[tuple[str, Any]] = []

            if self._state_version != cursor.state_version:
                cursor.state_version = self._state_version
                events.append(("state", self.state))

            if self._last_log_id > cursor.log_id:
                fresh = [e for e in self._logs if e["id"] > cursor.log_id]
                cursor.log_id = self._last_log_id
                if fresh:
                    events.append(("logs", fresh))

            if self._command_version != cursor.command_version:
                cursor.command_version = self._command_version
                events.append(
                    ("commands", [c.to_ui() for c in list(self._commands)[-40:]])
                )

            if self._stats_version != cursor.stats_version:
                cursor.stats_version = self._stats_version
                events.append(("stats", dict(self.stats)))

            return events
