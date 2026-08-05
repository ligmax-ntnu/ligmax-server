"""Per-repo update requests, so the operator can press a button and a node pulls.

The flow, and why it is shaped this way:

    operator clicks "Update" in the dashboard
        -> POST /api/deploy/<repo>          (admin cookie required)
        -> a request is recorded here with a fresh nonce

    the node that owns <repo> polls, on its own timer
        -> GET /api/deploy/<repo>/pending   (node key required)
        -> sees {"requested": true, "nonce": "..."}
        -> runs deploy/ligmax-update.sh, which fast-forwards and maybe restarts
        -> POST /api/deploy/<repo>/report   with the outcome and the new HEAD

Nodes **poll outbound**. Nothing here ever connects to a node, so no node needs an
inbound port, a public hostname or a certificate -- which is the only way this works
for the vessel behind 5G and for a laptop on a competition network.

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

    def to_ui(self, now: float) -> dict[str, Any]:
        pending = self.nonce is not None
        self_updating = self.name in SELF_UPDATING
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
            "node_online": self_updating
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

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._expire(now)
            return {
                "repos": [self._repos[n].to_ui(now) for n in self._repos],
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
