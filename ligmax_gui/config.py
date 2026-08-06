"""Configuration, loaded from `.env` at the repository root."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .deploy import DEFAULT_REPOS
from .rtk import FALLBACK_SOURCE_PASSWORD

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
ENV_PATH = REPO_ROOT / ".env"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # tiny fallback so the server runs without python-dotenv
        if not ENV_PATH.exists():
            return
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        return
    load_dotenv(ENV_PATH)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    admin_key: str
    boat_key: str
    cookie_secret: str
    session_hours: int = 12
    public_read: bool = True
    host: str = "0.0.0.0"
    port: int = 3338  # live.ligmax.no is forwarded here
    udp_host: str = "0.0.0.0"
    udp_port: int = 8771
    max_scan_points: int = 1500
    log_buffer: int = 4000
    # Shared secret the update pollers on each node present. Defaults to boat_key so
    # there is one fewer secret to manage; set LIGMAX_NODE_KEY to separate them.
    node_key: str = ""
    # Repos the dashboard offers an Update button for.
    repos: tuple[str, ...] = DEFAULT_REPOS
    # NTRIP caster (rtk.py). Port 2101 is forwarded to this box for rtk.ligmax.no;
    # both the base station and the vessel dial out to it.
    rtk_enabled: bool = True
    rtk_host: str = "0.0.0.0"
    rtk_port: int = 2101
    rtk_mount: str = "LIGMAX1"
    # Without this the caster refuses every base station: an open source port
    # lets a stranger feed the boat corrections it would trust completely.
    rtk_source_password: str = ""
    rtk_user: str = ""
    rtk_password: str = ""
    rtk_base_lat: float = 0.0
    rtk_base_lon: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def session_seconds(self) -> int:
        return max(60, self.session_hours * 3600)

    @property
    def commands_enabled(self) -> bool:
        return bool(self.admin_key)


def load_config() -> Config:
    _load_dotenv()
    warnings: list[str] = []

    admin_key = os.environ.get("LIGMAX_ADMIN_KEY", "").strip()
    boat_key = os.environ.get("LIGMAX_BOAT_KEY", "").strip()
    cookie_secret = os.environ.get("LIGMAX_COOKIE_SECRET", "").strip()

    if not ENV_PATH.exists():
        warnings.append(
            f"No .env found at {ENV_PATH}. Copy .env.example to .env and set "
            "your keys - admin commands stay disabled until you do."
        )
    if not admin_key:
        warnings.append(
            "LIGMAX_ADMIN_KEY is unset: the dashboard is read-only and no "
            "commands can be sent to the vessel."
        )
    elif admin_key.startswith("change-me"):
        warnings.append(
            "LIGMAX_ADMIN_KEY is still the placeholder from .env.example. "
            "Change it before running this anywhere but your own laptop."
        )
    if not boat_key:
        warnings.append(
            "LIGMAX_BOAT_KEY is unset: telemetry ingest is UNAUTHENTICATED. "
            "Fine on localhost, not on the competition network."
        )
    if not cookie_secret:
        cookie_secret = secrets.token_hex(32)
        warnings.append(
            "LIGMAX_COOKIE_SECRET is unset: generated a random one, so admin "
            "sessions will be invalidated every time the server restarts."
        )

    node_key = os.environ.get("LIGMAX_NODE_KEY", "").strip() or boat_key
    if not node_key:
        warnings.append(
            "Neither LIGMAX_NODE_KEY nor LIGMAX_BOAT_KEY is set: the deploy endpoints "
            "are UNAUTHENTICATED, so anyone who can reach them could trigger a pull on "
            "your nodes. Set one before exposing this server."
        )

    rtk_enabled = _env_bool("LIGMAX_RTK_ENABLED", True)
    rtk_source_password = os.environ.get("LIGMAX_RTK_SOURCE_PASSWORD", "").strip()
    if not rtk_source_password:
        # Falls back to a literal in rtk.py so the base station can come up before
        # anyone can edit `.env` on the ground station. Warned about every start,
        # because that literal is public.
        rtk_source_password = FALLBACK_SOURCE_PASSWORD
        if rtk_enabled:
            warnings.append(
                "LIGMAX_RTK_SOURCE_PASSWORD is unset: the NTRIP caster is using the "
                "fallback password committed in ligmax_gui/rtk.py, which is public. "
                "Anyone who reads the repo can push corrections, and a rover applies "
                "whatever it is given while still reporting RTK FIXED - that is a "
                "position-integrity problem, not a bandwidth one. Set the variable "
                "here and on the base station."
            )

    raw_repos = os.environ.get("LIGMAX_REPOS", "").strip()
    repos = tuple(r.strip() for r in raw_repos.split(",") if r.strip()) or DEFAULT_REPOS

    return Config(
        admin_key=admin_key,
        boat_key=boat_key,
        cookie_secret=cookie_secret,
        node_key=node_key,
        repos=repos,
        session_hours=_env_int("LIGMAX_SESSION_HOURS", 12),
        public_read=_env_bool("LIGMAX_PUBLIC_READ", True),
        host=os.environ.get("LIGMAX_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int("LIGMAX_PORT", 3338),
        udp_host=os.environ.get("LIGMAX_UDP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        udp_port=_env_int("LIGMAX_UDP_PORT", 8771),
        max_scan_points=_env_int("LIGMAX_MAX_SCAN_POINTS", 1500),
        log_buffer=_env_int("LIGMAX_LOG_BUFFER", 4000),
        rtk_enabled=rtk_enabled,
        rtk_host=os.environ.get("LIGMAX_RTK_HOST", "0.0.0.0").strip() or "0.0.0.0",
        rtk_port=_env_int("LIGMAX_RTK_PORT", 2101),
        rtk_mount=os.environ.get("LIGMAX_RTK_MOUNT", "LIGMAX1").strip() or "LIGMAX1",
        rtk_source_password=rtk_source_password,
        rtk_user=os.environ.get("LIGMAX_RTK_USER", "").strip(),
        rtk_password=os.environ.get("LIGMAX_RTK_PASSWORD", "").strip(),
        rtk_base_lat=_env_float("LIGMAX_RTK_BASE_LAT", 0.0),
        rtk_base_lon=_env_float("LIGMAX_RTK_BASE_LON", 0.0),
        warnings=warnings,
    )
