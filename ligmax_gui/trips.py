"""Trip recordings, taken off the boat and kept here.

The vessel records every autonomous attempt to a gzipped JSONL file
(`ligmax-pi/nodes/self_driving/recorder.py`) and those files are the only way to
answer "why did it do that" after the fact. They are also stuck on a 32 GB SD
card with the operating system on it, on a boat, and the run most worth reviewing
is very often the one where the boat had to be carried back.

So: one route to push a file up, one to ask what is already here, and a page to
pull them down in the tent. Deliberately NOT part of `/api/ingest`:

  * a frame is JSON and capped at 4 MB (`server.MAX_INGEST_BYTES`); a trip is
    binary and a 15-minute attempt measures about 60 MB, so sharing a limit
    would mean raising the frame limit by two orders of magnitude for the
    benefit of something that is not a frame;
  * ingest is latency-critical and runs at 1 Hz forever, and a 60 MB body on
    that path would stall the command channel that rides in its reply;
  * a trip upload wants resume, and a telemetry frame emphatically does not -
    a frame superseded mid-flight should be dropped, not retried.

Resume, and why it is `Content-Range`
-------------------------------------
The uplink is 4G from a boat. A 60 MB POST will not always finish, and a scheme
that starts again from zero on every drop can fail forever on a link that is
merely bad rather than absent. So an upload may be sent in pieces:

    POST /api/trip/<name>
    Content-Range: bytes 12582912-25165823/62914560

Each piece must start exactly where the file on disk currently ends, which makes
the protocol a single rule the sender can obey without any state of its own: ask
`GET /api/trip` how many bytes are held, and send from there. Out-of-order or
overlapping pieces are refused rather than stitched, because a partially-written
recording that *looks* complete is worse than one that is visibly short.

A file is written to `<name>.part` until the declared total arrives, then moved
into place. Nothing lists a `.part`, so an interrupted upload cannot be mistaken
for a recording, and `review_trip.py` is never handed half a file.

Storage
-------
`trips/<boat>/<name>.jsonl.gz`, under the repo by default. The boat segment is
there because a second hull is a realistic thing to add and a name collision
between two boats' "20260810-091455-task1" would silently overwrite a run.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

#: Well above the 4 MB frame limit and well above a worst-case attempt. Measured
#: on the Pi 2026-08-09: a 15-minute run with everything recorded is ~60 MB of
#: incompressible synthetic data, and real sweeps compress far better. 256 MB is
#: about four such attempts in one file, which is more than the recorder will
#: ever produce (`RECORD_MAX_TRIP_MB` caps a single trip at 512 MB uncompressed).
MAX_TRIP_BYTES = 256 * 1024 * 1024

#: One piece of an upload. Small enough that a drop costs little and the server
#: never buffers much; large enough that a 60 MB file is a few dozen requests.
MAX_CHUNK_BYTES = 8 * 1024 * 1024

#: Refuse to accept anything once the disk is this empty. The dashboard, the logs
#: and the RTK caster all share this box, and filling it to take a recording off
#: a boat that is safely on shore would be a poor trade.
MIN_FREE_MB = 2048.0

#: An abandoned `.part` is swept up after this long. A resume that has been
#: silent for two hours is not coming back, and the boat re-uploads from zero.
PART_MAX_AGE_S = 2 * 3600

#: What a trip may be called. The recorder names them
#: `20260810-091455-task1.jsonl.gz`; this is that shape, generously, and nothing
#: with a path separator, a leading dot or an unusual character in it.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

#: Same rule for the boat segment, which comes off an untrusted header.
_BOAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")

DEFAULT_BOAT = "ligmax"

_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$", re.IGNORECASE)


class TripError(Exception):
    """Refused, with a reason meant for whoever is holding the boat.

    `held` is how many bytes of this recording the server already has, carried on
    the refusal itself so a sender that has lost its place recovers from the
    error rather than needing a second request to ask. It is what makes the
    resume rule a single round trip in the failure case as well as the good one.
    None where the question does not apply.
    """

    def __init__(
        self, message: str, status: int = 400, held: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.held = held


def parse_content_range(header: str | None, body_len: int) -> tuple[int, int, bool]:
    """`(start, total, is_ranged)` from a `Content-Range`, or the whole-file case.

    No header at all means "this body is the entire file", which is the ordinary
    path for the small recordings and the one a hand-rolled `curl` will take.
    """
    if not header:
        return 0, body_len, False

    match = _RANGE_RE.match(header)
    if not match:
        raise TripError(
            f"Content-Range must look like 'bytes 0-{max(body_len - 1, 0)}/<total>', "
            f"got {header!r}"
        )
    start, end, total = (int(g) for g in match.groups())

    if start > end:
        raise TripError(f"Content-Range starts after it ends: {header!r}")
    if end - start + 1 != body_len:
        raise TripError(
            f"Content-Range covers {end - start + 1} bytes but the body is "
            f"{body_len}"
        )
    if end >= total:
        raise TripError(f"Content-Range runs past the total it declares: {header!r}")
    return start, total, True


def _free_mb(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.free / 1048576.0


class TripStore:
    """The directory of recordings, and the partial uploads on their way in.

    Thread-safe because ingest, the browser and the housekeeping sweep all reach
    it: Flask serves these on whatever thread the WSGI server hands over, and two
    chunks of the same file arriving at once must not both append.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.last_error: str | None = None
        self._lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Not fatal: everything else on this server still works, and the
            # failure is reported on the page rather than at import time.
            self.last_error = f"cannot use {self.root}: {exc}"

    # -- naming -------------------------------------------------------------

    def _safe(self, boat: str, name: str) -> tuple[str, str]:
        """Validate both halves of the path before either touches the filesystem.

        Whitelisted rather than sanitised. A rule that *rewrites* a bad name has
        to be right about every encoding trick; a rule that refuses one only has
        to be right about what a good name looks like, and the sender is a
        program that already knows.
        """
        boat = (boat or DEFAULT_BOAT).strip()
        name = (name or "").strip()
        if not _BOAT_RE.match(boat):
            raise TripError(f"{boat!r} is not a usable vessel name")
        if not _NAME_RE.match(name):
            raise TripError(
                f"{name!r} is not a usable recording name - letters, digits, "
                "dot, dash and underscore only"
            )
        if name.endswith(".part"):
            # Reserved: `.part` is how an upload in flight is distinguished from
            # a recording, and `list()` hides every one of them. A file genuinely
            # called that would upload successfully and then be invisible.
            raise TripError("a recording may not be named '.part'")
        return boat, name

    def _paths(self, boat: str, name: str) -> tuple[Path, Path]:
        directory = self.root / boat
        return directory / name, directory / f"{name}.part"

    # -- reading ------------------------------------------------------------

    def list(self, boat: str | None = None) -> list[dict[str, Any]]:
        """Finished recordings, newest first. Partial uploads are not listed.

        `bytes_held` on an in-flight upload is what the vessel needs to resume,
        so partials are reported by `pending()` instead - separately, because a
        half-file must never appear anywhere something could try to read it.
        """
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            if boat and directory.name != boat:
                continue
            for entry in directory.iterdir():
                if not entry.is_file() or entry.name.endswith(".part"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                out.append(
                    {
                        "boat": directory.name,
                        "name": entry.name,
                        "bytes": stat.st_size,
                        "mb": round(stat.st_size / 1048576.0, 2),
                        "modified": stat.st_mtime,
                    }
                )
        out.sort(key=lambda item: item["modified"], reverse=True)
        return out

    def pending(self, boat: str | None = None) -> dict[str, int]:
        """`{name: bytes already held}` for uploads in flight. The resume table."""
        out: dict[str, int] = {}
        if not self.root.exists():
            return out
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or (boat and directory.name != boat):
                continue
            for entry in directory.glob("*.part"):
                try:
                    out[entry.name[: -len(".part")]] = entry.stat().st_size
                except OSError:
                    continue
        return out

    def path_for(self, boat: str, name: str) -> Path:
        """The file to serve, validated. Raises rather than returning a guess."""
        boat, name = self._safe(boat, name)
        final, _part = self._paths(boat, name)
        if not final.is_file():
            raise TripError(f"no recording called {name!r} for {boat!r}", status=404)
        return final

    # -- writing ------------------------------------------------------------

    def accept(
        self,
        boat: str,
        name: str,
        body: bytes,
        content_range: str | None = None,
    ) -> dict[str, Any]:
        """Take one whole file, or one piece of one. Raises `TripError` if not.

        The reply always carries `bytes_held`, so a sender that lost track of
        where it was - or got a 409 for sending the wrong piece - learns where to
        resume from the refusal itself and needs no second request to recover.
        """
        boat, name = self._safe(boat, name)
        start, total, ranged = parse_content_range(content_range, len(body))

        if total > MAX_TRIP_BYTES:
            raise TripError(
                f"{total / 1048576.0:.0f} MB is over the "
                f"{MAX_TRIP_BYTES / 1048576.0:.0f} MB limit for one recording",
                status=413,
            )
        # The chunk limit applies to a *piece* of an upload, not to a whole file
        # sent in one go: `curl --data-binary @trip.jsonl.gz` is the path a human
        # takes from the tent, and it should not have to learn Content-Range to
        # push a 60 MB file. The vessel chunks regardless - see the Pi's
        # `trip_upload.py`, which is what actually has to survive a 4G drop.
        if ranged and len(body) > MAX_CHUNK_BYTES:
            raise TripError(
                f"send at most {MAX_CHUNK_BYTES / 1048576.0:.0f} MB per request",
                status=413,
            )
        if total <= 0 or not body:
            # An empty recording is a bug on the boat, not a file worth keeping,
            # and letting one through would put a zero-byte entry on the page
            # that looks exactly like a run nobody can explain.
            raise TripError("empty body - there is nothing to store")

        final, part = self._paths(boat, name)

        with self._lock:
            free = _free_mb(self.root)
            if free is not None and free < MIN_FREE_MB:
                raise TripError(
                    f"only {free:.0f} MB free on the ground station - refusing to "
                    "fill the disk this server runs on",
                    status=507,
                )

            if final.exists():
                # Already held, complete. Not an error: the boat re-offers
                # everything it has after a reconnect, and the honest answer is
                # "no need" rather than a failure it will retry forever.
                return {
                    "ok": True,
                    "stored": False,
                    "complete": True,
                    "already_held": True,
                    "name": name,
                    "boat": boat,
                    "bytes_held": final.stat().st_size,
                }

            try:
                part.parent.mkdir(parents=True, exist_ok=True)
                held = part.stat().st_size if part.exists() else 0

                if start != held:
                    # The one rule the sender has to obey. Answered with where it
                    # should have started, so recovery needs no extra round trip.
                    raise TripError(
                        f"this piece starts at {start} but {name!r} currently "
                        f"holds {held} bytes - resume from there",
                        status=409,
                        held=held,
                    )

                with open(part, "ab") as handle:
                    handle.write(body)
                    # Durable before the rename below claims it is a recording.
                    handle.flush()
                    os.fsync(handle.fileno())
                held += len(body)

                if held >= total:
                    if held > total:
                        # Cannot happen if the sender obeyed the rule above, so
                        # the file is not to be trusted at all.
                        part.unlink(missing_ok=True)
                        raise TripError(
                            f"{name!r} received {held} bytes but declared {total} "
                            "- discarded, send it again"
                        )
                    os.replace(part, final)
                    return {
                        "ok": True,
                        "stored": True,
                        "complete": True,
                        "name": name,
                        "boat": boat,
                        "bytes_held": held,
                    }
            except TripError:
                raise
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise TripError(
                        "the ground station's disk is full", status=507
                    ) from exc
                raise TripError(f"could not write {name!r}: {exc}", status=500) from exc

        return {
            "ok": True,
            "stored": True,
            "complete": False,
            "name": name,
            "boat": boat,
            "bytes_held": held,
            "bytes_total": total,
        }

    def delete(self, boat: str, name: str) -> None:
        """Remove a recording, and any half-finished upload of the same name.

        The unlink is guarded because **this server runs on Windows**, where a
        file being read cannot be unlinked at all. Werkzeug serves a download
        straight off an open handle, so an admin pressing delete while someone
        in the tent is still pulling the same 60 MB file gets `WinError 32` out
        of `os.unlink`. On Linux the unlink would simply succeed and the reader
        would keep its handle to a file that no longer has a name, which is why
        this is not a case the code would ever have hit in development.

        Unguarded, that `OSError` was not a `TripError`, so it went past the
        handler in `server.trip_delete` and Flask answered with a 500 and a
        traceback - "the ground station is broken" rather than "wait for the
        download to finish". 409 and a sentence saying which.
        """
        boat, name = self._safe(boat, name)
        final, part = self._paths(boat, name)
        with self._lock:
            if not final.is_file() and not part.is_file():
                raise TripError(f"no recording called {name!r}", status=404)
            for path in (final, part):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    raise TripError(
                        f"could not delete {name!r}: {exc.strerror or exc}. On "
                        "Windows a recording cannot be removed while it is being "
                        "downloaded - let the transfer finish and try again",
                        status=409,
                    ) from exc

    def sweep(self, now: float | None = None) -> int:
        """Drop `.part` files nothing has touched in a long time. Never raises.

        An abandoned partial otherwise blocks its own name forever: the next
        attempt to send that recording starts at 0, the file already holds
        something, and every retry gets the same 409.
        """
        now = now if now is not None else time.time()
        removed = 0
        if not self.root.exists():
            return 0
        with self._lock:
            for directory in self.root.iterdir():
                if not directory.is_dir():
                    continue
                for entry in directory.glob("*.part"):
                    try:
                        if now - entry.stat().st_mtime > PART_MAX_AGE_S:
                            entry.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        continue
        return removed

    # -- summary ------------------------------------------------------------

    def summary(self, boat: str | None = None) -> dict[str, Any]:
        held = self.list(boat)
        return {
            "trips": held,
            "pending": self.pending(boat),
            "count": len(held),
            "bytes": sum(item["bytes"] for item in held),
            "free_mb": _free_mb(self.root),
            "root": str(self.root),
            "max_mb": MAX_TRIP_BYTES // 1048576,
            "chunk_mb": MAX_CHUNK_BYTES // 1048576,
            "error": self.last_error,
        }
