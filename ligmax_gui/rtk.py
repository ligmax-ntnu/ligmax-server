"""NTRIP caster: the meeting point for the base station and the vessel.

Why this exists at all
----------------------
Both ends of the RTK link are on 4G, so neither can accept a connection. The
base station cannot be reached from the boat and the boat cannot be reached from
anywhere. The ground station is the only box in the fleet with a forwarded port,
so it sits in the middle and both ends dial *out* to it — exactly the same shape
as telemetry and the camera uplink (`server.py` docstring).

    base station ──SOURCE /LIGMAX1──▶  rtk.ligmax.no:2101  ◀──GET /LIGMAX1── vessel
      LC29H, fixed position               this module              LC29H rover
      pushes RTCM3                                              → Pixhawk → GPS

This is a plain NTRIP caster and not a private protocol on purpose. 2101 is the
conventional NTRIP port, so any survey app, u-center, Mission Planner or SW Maps
can point at the same mountpoint and get the same corrections — useful for
checking the base without the boat in the water, and for a second rover.

What it deliberately does not do
--------------------------------
  * **No buffering.** A correction that arrives late is worse than none: the
    rover would apply an old atmosphere to a new epoch and report a confident
    fix built on it. Clients that cannot keep up are dropped, not queued.
  * **No parsing.** The caster is byte-transparent. Whether the base is emitting
    the right RTCM message set is the base station's business
    (`ligmax-subsystems/rtk/base_station.py` reports it) and the rover's fix type
    is the proof that matters.
  * **No TLS.** NTRIP v1 is not HTTPS, which is why `rtk.ligmax.no` must be
    DNS-only in Cloudflare (docs/hosting.md). The mountpoint password is
    therefore sent in clear — it protects against a stranger *injecting*
    corrections, and nothing more. Do not reuse LIGMAX_ADMIN_KEY for it.

Threading model: one thread accepts, one thread per connection. The source
thread fans out to every client's bounded queue; each client thread drains its
own queue onto its own socket. A wedged client can therefore only ever stall
itself.
"""

from __future__ import annotations

import base64
import hmac
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# One mountpoint, one base. A VRS network would need more; a single boat and a
# single base on a pontoon does not.
DEFAULT_MOUNT = "LIGMAX1"
DEFAULT_PORT = 2101

# Stopgap mountpoint password, used when LIGMAX_RTK_SOURCE_PASSWORD is unset -
# the ground station's `.env` is not always editable when the base station needs
# to come up. It is **committed to a public repo**, so treat it as no password at
# all: anyone who reads GitHub can push corrections the rover will trust
# completely. Set the environment variable on both ends and this stops being
# used, with no code change.
#
# The same literal is in `ligmax-subsystems/rtk/base_station.py`. If you change
# one, change both, or the base station gets "ERROR - Bad Password" and RTK
# quietly does not happen.
FALLBACK_SOURCE_PASSWORD = "ligmax-base-2026"

# A client that falls this far behind is disconnected rather than queued: see the
# module docstring. 64 chunks of up to 4 KiB is several seconds of RTCM at the
# ~1 kbit/s a single-base MSM stream actually costs.
CLIENT_QUEUE_DEPTH = 64
READ_SIZE = 4096

REQUEST_TIMEOUT = 10.0  # how long a connection has to state its business
MAX_REQUEST_BYTES = 8192

# No RTCM for this long and the source is considered gone, so the status stops
# claiming a base that is silently wedged. A base emitting MSM at 1 Hz plus 1005
# at 0.1 Hz is never quiet for more than a second or two.
SOURCE_IDLE_TIMEOUT = 30.0

# A base on 4G that loses its IP leaves a half-open socket here that TCP will not
# notice for minutes. Refusing the reconnect for that long would take RTK down
# for the whole window, so an idle incumbent is evicted by the new connection.
# Shorter than SOURCE_IDLE_TIMEOUT so a reconnect wins before the sweep.
SOURCE_TAKEOVER_AFTER = 15.0

CLIENT_SEND_TIMEOUT = 10.0

_ICY_OK = b"ICY 200 OK\r\n\r\n"
_HTTP_OK_STREAM = (
    b"HTTP/1.1 200 OK\r\n"
    b"Ntrip-Version: Ntrip/2.0\r\n"
    b"Server: Ligmax NTRIP caster\r\n"
    b"Content-Type: gnss/data\r\n"
    b"Cache-Control: no-store, no-cache, max-age=0\r\n"
    b"Connection: close\r\n\r\n"
)
_UNAUTHORIZED = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b'WWW-Authenticate: Basic realm="Ligmax RTK"\r\n'
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n\r\n"
)
# NTRIP v1 sources speak a pre-HTTP dialect and expect these exact strings.
_ERR_PASSWORD = b"ERROR - Bad Password\r\n"
_ERR_MOUNT = b"ERROR - Bad Mountpoint\r\n"
_ERR_TAKEN = b"ERROR - Mount Point Taken\r\n"


def _now() -> float:
    return time.monotonic()


def _matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(str(given), str(expected))


@dataclass
class _Client:
    """One rover (or survey app) reading the mountpoint."""

    peer: str
    agent: str
    since: float
    queue: "queue.Queue[bytes]" = field(
        default_factory=lambda: queue.Queue(maxsize=CLIENT_QUEUE_DEPTH)
    )
    sent: int = 0
    dropped: bool = False


class NtripCaster:
    """Accepts one RTCM source and fans it out to every connected client.

    `serve()` blocks and is meant for a daemon thread; `status()` is safe to call
    from a Flask request thread.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        mount: str = DEFAULT_MOUNT,
        source_password: str = "",
        client_user: str = "",
        client_password: str = "",
        base_lat: float = 0.0,
        base_lon: float = 0.0,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.mount = mount.strip("/") or DEFAULT_MOUNT
        self.source_password = source_password
        self.client_user = client_user
        self.client_password = client_password
        self.base_lat = base_lat
        self.base_lon = base_lon
        self._log = log or (lambda level, message: None)

        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._source_peer: str | None = None
        self._source_agent = ""
        self._source_since = 0.0
        self._source_last_data = 0.0
        self._source_bytes = 0
        self._source_socket: socket.socket | None = None
        self._sessions = 0  # how many times a base has connected, ever
        self._rejected = 0  # bad password / bad mountpoint, either role
        self._listening = False
        self._last_error = ""

    # -- state the dashboard reads -----------------------------------------

    def status(self) -> dict:
        """`GET /api/rtk`. Ages in seconds, so a stale panel cannot look live."""
        now = _now()
        with self._lock:
            source_up = self._source_peer is not None
            block = {
                "listening": self._listening,
                "port": self.port,
                "mountpoint": self.mount,
                "source_connected": source_up,
                "clients": len(self._clients),
                "sessions": self._sessions,
                "rejected": self._rejected,
                "authenticated": bool(self.source_password),
            }
            if source_up:
                block["source_peer"] = self._source_peer
                block["source_agent"] = self._source_agent
                block["source_uptime_s"] = round(now - self._source_since, 1)
                block["bytes"] = self._source_bytes
                # The number that matters. Corrections older than a few seconds
                # are not corrections, whatever the connection says.
                block["correction_age_s"] = (
                    round(now - self._source_last_data, 1)
                    if self._source_last_data
                    else None
                )
            block["client_peers"] = [
                {
                    "peer": client.peer,
                    "agent": client.agent,
                    "uptime_s": round(now - client.since, 1),
                    "bytes": client.sent,
                }
                for client in self._clients
            ]
            if self._last_error:
                block["last_error"] = self._last_error[:160]
        return block

    # -- the listener -------------------------------------------------------

    def serve(self, stop: threading.Event) -> None:
        """Bind and accept until `stop` is set. Never raises past this frame."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR means something different on Windows - there it lets a
        # second process steal a live port rather than reclaim a dead one, which
        # on the ground station would let a typo silently take over the caster.
        if sys.platform != "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(8)
        except OSError as exc:
            self._last_error = str(exc)
            self._log(
                "ERROR",
                f"RTK caster disabled: cannot bind {self.host}:{self.port} ({exc})",
            )
            listener.close()
            return

        listener.settimeout(0.5)
        self._listening = True
        self._log(
            "INFO",
            f"NTRIP caster on {self.host}:{self.port}, mountpoint /{self.mount}"
            + ("" if self.source_password else " - NO SOURCE PASSWORD, base refused"),
        )

        try:
            while not stop.is_set():
                try:
                    conn, addr = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle,
                    args=(conn, f"{addr[0]}:{addr[1]}", stop),
                    daemon=True,
                    name=f"ntrip-{addr[0]}",
                ).start()
                self._sweep_source()
        finally:
            self._listening = False
            listener.close()
            self._disconnect_source("caster stopping")

    # -- per-connection dispatch -------------------------------------------

    def _handle(self, conn: socket.socket, peer: str, stop: threading.Event) -> None:
        try:
            conn.settimeout(REQUEST_TIMEOUT)
            request, leftover = self._read_request(conn)
            if request is None:
                return
            line, headers = request

            verb, _, rest = line.partition(" ")
            target, _, _version = rest.partition(" ")
            verb = verb.upper()

            if verb == "SOURCE":
                # NTRIP v1: "SOURCE <password> /<mount>". The password is on the
                # request line, which is why v1 sources are the one thing here
                # that never looks like HTTP.
                password, _, mount = rest.partition(" ")
                self._serve_source(
                    conn, peer, mount, password, headers, leftover, stop, v2=False
                )
            elif verb == "POST":
                # NTRIP v2 source: same job, HTTP framing, Basic auth.
                _user, password = _basic_auth(headers)
                self._serve_source(
                    conn, peer, target, password, headers, leftover, stop, v2=True
                )
            elif verb == "GET":
                mount = target.strip("/")
                if not mount or mount.upper() == "SOURCETABLE.DAT":
                    self._serve_sourcetable(conn, headers)
                else:
                    self._serve_client(conn, peer, mount, headers, stop)
            else:
                conn.sendall(_ERR_MOUNT)
        except (OSError, ValueError):
            pass  # a dropped connection is the normal way these end
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_request(
        self, conn: socket.socket
    ) -> tuple[tuple[str, dict[str, str]] | None, bytes]:
        """Request line + headers, plus whatever body bytes came with them.

        An NTRIP v2 source can put RTCM in the same TCP segment as its POST
        headers, so the leftover is returned rather than discarded - dropping it
        would lose the first correction of every session.
        """
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            if len(buffer) > MAX_REQUEST_BYTES:
                return None, b""
            try:
                chunk = conn.recv(READ_SIZE)
            except (socket.timeout, OSError):
                return None, b""
            if not chunk:
                return None, b""
            buffer += chunk

        head, _, leftover = buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for raw in lines[1:]:
            key, _, value = raw.partition(":")
            if key:
                headers[key.strip().lower()] = value.strip()
        return (lines[0].strip(), headers), leftover

    # -- the base station side ---------------------------------------------

    def _serve_source(
        self,
        conn: socket.socket,
        peer: str,
        mount: str,
        password: str,
        headers: dict[str, str],
        leftover: bytes,
        stop: threading.Event,
        v2: bool,
    ) -> None:
        mount = mount.strip("/")
        if not self.source_password:
            # Refusing is the safe default: an open source port on the internet
            # lets anyone feed the boat corrections, and a rover applies them
            # without complaint - it would report RTK FIXED at the wrong place,
            # which is worse than no RTK at all.
            self._rejected += 1
            self._log(
                "ERROR",
                f"RTK source from {peer} refused: LIGMAX_RTK_SOURCE_PASSWORD is unset",
            )
            conn.sendall(_UNAUTHORIZED if v2 else _ERR_PASSWORD)
            return
        if mount != self.mount:
            self._rejected += 1
            self._log("WARN", f"RTK source from {peer} wanted /{mount}, not this caster")
            conn.sendall(_ERR_MOUNT)
            return
        if not _matches(password, self.source_password):
            self._rejected += 1
            self._log("WARN", f"RTK source from {peer} gave a bad mountpoint password")
            conn.sendall(_UNAUTHORIZED if v2 else _ERR_PASSWORD)
            return

        agent = headers.get("source-agent") or headers.get("user-agent") or "?"
        if not self._claim_source(conn, peer, agent):
            conn.sendall(_ERR_TAKEN)
            return

        conn.sendall(_HTTP_OK_STREAM if v2 else _ICY_OK)
        self._log("INFO", f"RTK base station connected from {peer} ({agent})")

        try:
            conn.settimeout(1.0)
            if leftover:
                self._publish(leftover)
            while not stop.is_set():
                try:
                    data = conn.recv(READ_SIZE)
                except socket.timeout:
                    # `or _source_since`: a base that connects and then says
                    # nothing at all has never set _last_data, and must still
                    # time out rather than be treated as infinitely stale.
                    quiet = _now() - (self._source_last_data or self._source_since)
                    if quiet > SOURCE_IDLE_TIMEOUT:
                        self._log(
                            "WARN",
                            f"RTK base {peer} sent nothing for "
                            f"{SOURCE_IDLE_TIMEOUT:.0f}s - dropping it so the "
                            "dashboard stops claiming corrections",
                        )
                        break
                    continue
                except OSError:
                    break
                if not data:
                    break
                self._publish(data)
        finally:
            with self._lock:
                mine = self._source_socket is conn
            if mine:
                self._disconnect_source(f"base station {peer} disconnected")

    def _claim_source(self, conn: socket.socket, peer: str, agent: str) -> bool:
        """Take the mountpoint, evicting a stale incumbent. See SOURCE_TAKEOVER_AFTER."""
        with self._lock:
            incumbent = self._source_socket
            if incumbent is not None:
                idle = _now() - (self._source_last_data or self._source_since)
                if idle < SOURCE_TAKEOVER_AFTER:
                    self._rejected += 1
                    return False
                self._log(
                    "WARN",
                    f"RTK: evicting silent base {self._source_peer} "
                    f"({idle:.0f}s quiet) for {peer}",
                )
                try:
                    incumbent.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._source_socket = conn
            self._source_peer = peer
            self._source_agent = agent
            self._source_since = _now()
            self._source_last_data = 0.0
            self._source_bytes = 0
            self._sessions += 1
        return True

    def _disconnect_source(self, why: str) -> None:
        with self._lock:
            had = self._source_peer
            self._source_socket = None
            self._source_peer = None
            self._source_agent = ""
        if had:
            self._log("WARN", f"RTK: {why}")

    def _sweep_source(self) -> None:
        """Drop a source that has gone quiet without closing its socket."""
        with self._lock:
            stale = self._source_socket is not None and (
                _now() - (self._source_last_data or self._source_since)
                > SOURCE_IDLE_TIMEOUT
            )
            conn = self._source_socket if stale else None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _publish(self, data: bytes) -> None:
        """Hand one chunk of RTCM to every client. Never blocks on a slow one."""
        with self._lock:
            self._source_last_data = _now()
            self._source_bytes += len(data)
            clients = list(self._clients)
        for client in clients:
            try:
                client.queue.put_nowait(data)
            except queue.Full:
                # Backed up by CLIENT_QUEUE_DEPTH chunks: this client is not
                # reading, and corrections it has not read yet are already too
                # old to use. Cut it loose and let it reconnect.
                client.dropped = True

    # -- the rover side -----------------------------------------------------

    def _serve_client(
        self,
        conn: socket.socket,
        peer: str,
        mount: str,
        headers: dict[str, str],
        stop: threading.Event,
    ) -> None:
        v2 = "2.0" in headers.get("ntrip-version", "")
        if mount != self.mount:
            self._rejected += 1
            # A client asking for the wrong mountpoint gets the sourcetable, per
            # the NTRIP spec - that is how survey apps discover what is on offer.
            self._serve_sourcetable(conn, headers)
            return
        if self.client_password:
            user, password = _basic_auth(headers)
            if not (
                _matches(user, self.client_user) and _matches(password, self.client_password)
            ):
                self._rejected += 1
                self._log("WARN", f"RTK client from {peer} gave bad credentials")
                conn.sendall(_UNAUTHORIZED)
                return

        agent = headers.get("user-agent", "?")
        client = _Client(peer=peer, agent=agent, since=_now())
        with self._lock:
            self._clients.append(client)
        self._log("INFO", f"RTK client connected from {peer} ({agent})")

        try:
            conn.sendall(_HTTP_OK_STREAM if v2 else _ICY_OK)
            conn.settimeout(CLIENT_SEND_TIMEOUT)
            while not stop.is_set() and not client.dropped:
                try:
                    data = client.queue.get(timeout=0.5)
                except queue.Empty:
                    if not self._drain_client(conn):
                        break
                    continue
                conn.sendall(data)
                client.sent += len(data)
                if not self._drain_client(conn):
                    break
        except OSError:
            pass
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            self._log(
                "INFO",
                f"RTK client {peer} disconnected"
                + (" (too slow, dropped)" if client.dropped else ""),
            )

    @staticmethod
    def _drain_client(conn: socket.socket) -> bool:
        """Discard anything the client sends; False once it has hung up.

        Rovers send a periodic GGA so a network caster can pick a base near them.
        There is one base here, so it is noise - but it must still be read, or the
        client's send buffer eventually fills and it stalls. Reading it is also
        the only way to notice a client that closed while the stream was quiet.
        """
        try:
            conn.setblocking(False)
            while True:
                try:
                    chunk = conn.recv(READ_SIZE)
                except (BlockingIOError, socket.timeout):
                    return True
                if not chunk:
                    return False
        except OSError:
            return False
        finally:
            try:
                conn.setblocking(True)
                conn.settimeout(CLIENT_SEND_TIMEOUT)
            except OSError:
                pass

    # -- sourcetable --------------------------------------------------------

    def _serve_sourcetable(self, conn: socket.socket, headers: dict[str, str]) -> None:
        """The one-line catalogue every NTRIP client asks for first.

        Field order is the NTRIP 1.0 STR record. `1` for nmea means the caster
        accepts a GGA from the client, which it does - it just ignores it.
        """
        entry = ";".join(
            [
                "STR",
                self.mount,
                "Ligmax base",
                "RTCM 3.3",
                "",  # format-details: whatever the base is configured to emit
                "2",  # carrier: L1+L2
                "GPS+GLO+GAL+BDS",
                "LIGMAX",
                "NOR",
                f"{self.base_lat:.2f}",
                f"{self.base_lon:.2f}",
                "1",  # accepts NMEA GGA from the client
                "0",  # single base, not a network solution
                "Quectel LC29H",
                "none",  # no compression
                "B" if self.client_password else "N",
                "N",  # no fee
                "1200",
                "",
            ]
        )
        body = f"{entry}\r\nENDSOURCETABLE\r\n".encode("latin-1")
        if "2.0" in headers.get("ntrip-version", ""):
            head = (
                b"HTTP/1.1 200 OK\r\n"
                b"Ntrip-Version: Ntrip/2.0\r\n"
                b"Server: Ligmax NTRIP caster\r\n"
                b"Content-Type: gnss/sourcetable\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n"
            )
        else:
            head = (
                b"SOURCETABLE 200 OK\r\n"
                b"Server: Ligmax NTRIP caster\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            )
        conn.sendall(head + body)


def _basic_auth(headers: dict[str, str]) -> tuple[str, str]:
    """`(user, password)` from an Authorization header, or `("", "")`."""
    header = headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return "", ""
    user, _, password = decoded.partition(":")
    return user, password
