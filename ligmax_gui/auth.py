"""Admin sessions.

The flow the operator sees:

    1. Open  http://host:8770/?key=<admin key>
    2. The server verifies the key, sets an HttpOnly cookie holding a *signed
       token* (not the key itself), and answers 303 -> "/".
    3. The address bar shows a bare "/" — nothing useful to photograph, and
       nothing in the browser's URL history to replay.

The token is `expiry.hmac(expiry)`, signed with LIGMAX_COOKIE_SECRET.  It is
stateless (survives a restart as long as the secret is stable), carries its own
expiry, and reveals nothing about the key.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

COOKIE_NAME = "lx_admin"


def _sign(secret: str, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def issue_token(config: "Config", now: float | None = None) -> str:
    expiry = int((now if now is not None else time.time()) + config.session_seconds)
    payload = str(expiry)
    return f"{payload}.{_sign(config.cookie_secret, payload)}"


def token_is_valid(config: "Config", token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, signature = token.rpartition(".")
    if not payload or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(config.cookie_secret, payload)):
        return False
    try:
        expiry = int(payload)
    except ValueError:
        return False
    return expiry > (now if now is not None else time.time())


def key_matches(candidate: str | None, expected: str) -> bool:
    """Constant-time key comparison. An empty expected key never matches."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.strip(), expected)
