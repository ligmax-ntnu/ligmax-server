"""Per-repo update requests, so the operator can press a button and a node pulls.

There are two ways a node can collect its update, and a repo uses exactly one.

**As a vessel command** (`COMMANDED`, currently `ligmax-pi`) -- the default now,
because it rides the one channel that is already proven to work in the field:

    operator clicks "Update" in the dashboard
        -> POST /api/deploy/<repo>          (admin cookie required)
        -> a request is recorded here, and an `update` command is queued
        -> the command goes down in the reply to the vessel's next telemetry POST,
           exactly like `estop` and `home_battery`
        -> io_manager fast-forwards, acks the outcome, and restarts the node tree
        -> server.py turns that ack back into report_command() below, so the panel
           shows Updated/Failed and the new SHA

**By polling** (every other repo) -- the node asks on its own timer:

    operator clicks "Update"
        -> POST /api/deploy/<repo>          (admin cookie required)
        -> a request is recorded here with a fresh nonce
        -> GET /api/deploy/<repo>/pending   (node key required)
        -> sees {"requested": true, "nonce": "..."}
        -> fast-forwards and restarts its child
        -> POST /api/deploy/<repo>/report   with the outcome and the new HEAD

Either way the node **connects outbound**. Nothing here ever connects to a node, so
no node needs an inbound port, a public hostname or a certificate -- which is the
only way this works for the vessel behind 5G and for a laptop on a competition
network. The difference is only which outbound channel carries the request, and the
command channel wins for the vessel because it needs no second secret: a wrong
`LIGMAX_NODE_KEY` is rejected before the poll is recorded, which looks exactly like
a node that is switched off.

This is deliberately NOT part of the vessel command queue in `state.py`. Those
commands steer a 5 kW boat; these ones deploy software. Keeping them apart means an
admin session that can restart the training box cannot accidentally be replayed into
a thruster command, and the audit log reads unambiguously.

State is in RAM, like the rest of the dashboard. If the process restarts, pending
requests are forgotten -- which is the desired behaviour: a request that was already
acted on should not fire twice, and one that was not can be pressed again.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# A request the node never collects expires, so a machine that was switched off for a
# week does not pull the moment it boots and surprise someone.
REQUEST_TTL = 30 * 60.0

# How long after its last poll a node is still considered to be listening.
# Each node's own update.py polls every 30 s (`POLL = 30`), so 180 s is six missed
# polls - long enough not to flicker, short enough to notice a dead node quickly.
# Keep this above whatever POLL the nodes use.
NODE_STALE_AFTER = 180.0

# Repos that update themselves and therefore never poll. `ligmax-server` is the
# dashboard you are reading: server.py handles its Update button in-process by
# handing off to update.py, so nothing ever calls /pending on its behalf and the
# row would otherwise sit at "has not checked in" for ever, wrongly.
SELF_UPDATING = ("ligmax-server",)

# Repos whose node collects its update as an operator command on the telemetry
# channel instead of polling /pending. These never call /pending either, so the
# same "never checked in" trap applies -- their liveness is the vessel link.
#
# Add a repo here only once its node actually handles the `update` command
# (ligmax-pi: nodes/io_manager/main.py). A repo listed here whose node ignores
# the command will sit at "Waiting" until the request expires.
COMMANDED = ("ligmax-pi",)

DEFAULT_REPOS = (
    "ligmax-server",
    "ligmax-pi",
    "ligmax-edge",
    "ligmax-ai",
    "ligmax-subsystems",
)

RESULTS = ("ok", "no-change", "refused", "failed")


@dataclass
class RepoState:
    """Everything known about one repo's deployment, from the server's point of view."""

    name: str
    requested_at: float | None = None
    requested_by: str | None = None
    nonce: str | None = None
    last_poll: float | None = None
    last_result: str | None = None
    last_message: str | None = None
    last_finished: float | None = None
    head: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_ui(self, now: float, vessel_online: bool = False) -> dict[str, Any]:
        pending = self.nonce is not None
        self_updating = self.name in SELF_UPDATING
        commanded = self.name in COMMANDED
        return {
            "name": self.name,
            "pending": pending,
            "requested_at": self.requested_at,
            "requested_by": self.requested_by,
            "waiting_for": (now - self.requested_at) if pending and self.requested_at else None,
            "last_poll": self.last_poll,
            # A self-updating repo has no poller, so "online" would always be false.
            # Report it as online instead of implying something is broken.
            "self_updating": self_updating,
            "commanded": commanded,
            # For a commanded repo the honest liveness question is "can we reach the
            # vessel", because that is the channel the button uses -- not whether
            # some second poller happens to be running.
            "node_online": self_updating
            or (commanded and vessel_online)
            or (
                self.last_poll is not None and (now - self.last_poll) < NODE_STALE_AFTER
            ),
            "last_result": self.last_result,
            "last_message": self.last_message,
            "last_finished": self.last_finished,
            "head": self.head,
            "history": self.history[-5:],
        }


class DeployRegistry:
    """Thread-safe registry of update requests, one entry per repo."""

    def __init__(self, repos: tuple[str, ...] | list[str] | None = None) -> None:
        names = tuple(repos) if repos else DEFAULT_REPOS
        self._lock = threading.Lock()
        self._repos: dict[str, RepoState] = {n: RepoState(name=n) for n in names}

    # -- reads --------------------------------------------------------------

    def known(self, name: str) -> bool:
        return name in self._repos

    def names(self) -> list[str]:
        return list(self._repos)

    def snapshot(self, vessel_online: bool = False) -> dict[str, Any]:
        """`vessel_online` is the telemetry link's state, which is what decides
        whether a COMMANDED repo can be reached at all."""
        now = time.time()
        with self._lock:
            self._expire(now)
            return {
                "repos": [
                    self._repos[n].to_ui(now, vessel_online) for n in self._repos
                ],
                "server_time": now,
                "request_ttl": REQUEST_TTL,
            }

    # -- operator side ------------------------------------------------------

    def request(self, name: str, issued_by: str) -> dict[str, Any]:
        """Record that an update was asked for. Returns the repo's new UI state."""
        now = time.time()
        with self._lock:
            repo = self._repos[name]
            repo.requested_at = now
            repo.requested_by = issued_by
            repo.nonce = secrets.token_hex(8)
            # Clear the previous outcome so the UI cannot show a stale "ok" beside a
            # request that has not been collected yet.
            repo.last_result = None
            repo.last_message = None
            return repo.to_ui(now)

    def cancel(self, name: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            repo = self._repos[name]
            repo.nonce = None
            repo.requested_at = None
            repo.requested_by = None
            return repo.to_ui(now)

    # -- node side ----------------------------------------------------------

    def pending(self, name: str) -> dict[str, Any]:
        """Called by the node on its timer. Records the poll, reports any request."""
        now = time.time()
        with self._lock:
            self._expire(now)
            repo = self._repos[name]
            repo.last_poll = now
            return {
                "repo": name,
                "requested": repo.nonce is not None,
                "nonce": repo.nonce,
                "requested_at": repo.requested_at,
            }

    def report(
        self,
        name: str,
        nonce: str | None,
        result: str,
        message: str = "",
        head: str | None = None,
    ) -> tuple[bool, str]:
        """Record a node's outcome. Returns (accepted, why-not).

        An unrecognised nonce is rejected rather than merged: it means the node is
        reporting on a request that has already been superseded or expired, and
        silently accepting it would show the operator a result for the wrong run.
        """
        if result not in RESULTS:
            return False, f"result must be one of {', '.join(RESULTS)}"

        now = time.time()
        with self._lock:
            repo = self._repos[name]
            if repo.nonce is not None and nonce != repo.nonce:
                return False, "nonce does not match the outstanding request"

            repo.last_result = result
            repo.last_message = message[:400]
            repo.last_finished = now
            if head:
                repo.head = head[:40]
            repo.history.append(
                {
                    "at": now,
                    "result": result,
                    "message": message[:200],
                    "head": repo.head,
                }
            )
            del repo.history[:-20]
            # The request is done either way. A failure must not leave the flag set,
            # or the node would retry forever on its next tick.
            repo.nonce = None
            repo.requested_at = None
            repo.requested_by = None
            return True, ""

    def report_command(
        self,
        name: str,
        result: str,
        message: str = "",
        head: str | None = None,
    ) -> tuple[bool, str]:
        """Record an outcome that arrived as a command ack rather than via /report.

        The vessel acks the `update` command on the telemetry channel and never
        sees a nonce, so we supply the outstanding one on its behalf. Reading it
        outside the lock is deliberate: if the request has since been cancelled or
        superseded, report() rejects the stale outcome, which is what we want.
        """
        with self._lock:
            nonce = self._repos[name].nonce
        return self.report(name, nonce=nonce, result=result, message=message, head=head)

    def note_head(self, name: str, head: str) -> None:
        """Let a node report its current HEAD without an update having happened."""
        with self._lock:
            self._repos[name].head = head[:40]

    # -- internals ----------------------------------------------------------

    def _expire(self, now: float) -> None:
        for repo in self._repos.values():
            if (
                repo.nonce is not None
                and repo.requested_at is not None
                and now - repo.requested_at > REQUEST_TTL
            ):
                repo.nonce = None
                repo.requested_at = None
                repo.requested_by = None
                repo.last_result = "failed"
                repo.last_message = "request expired before the node collected it"
                repo.last_finished = now
