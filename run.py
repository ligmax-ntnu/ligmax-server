#!/usr/bin/env python
"""Start the Ligmax ground-station dashboard.

    python run.py
    python run.py --port 3338 --no-udp
"""

from __future__ import annotations

import argparse
import threading

from ligmax_gui import protocol
from ligmax_gui.config import load_config
from ligmax_gui.rtk import FALLBACK_SOURCE_PASSWORD
from ligmax_gui.server import create_app, serve_housekeeping, serve_udp
from ligmax_gui.state import Store


def main() -> int:
    config = load_config()

    parser = argparse.ArgumentParser(description="Ligmax ground-station dashboard")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    parser.add_argument("--udp-port", type=int, default=config.udp_port)
    parser.add_argument(
        "--no-udp", action="store_true", help="HTTP ingest only (POST /api/ingest)"
    )
    parser.add_argument(
        "--no-rtk", action="store_true", help="do not run the NTRIP caster on 2101"
    )
    parser.add_argument("--debug", action="store_true", help="Flask reloader + tracebacks")
    args = parser.parse_args()

    config.host, config.port, config.udp_port = args.host, args.port, args.udp_port
    if args.no_udp:
        config.udp_port = 0
    # Read by create_app(), so it has to be settled before the app is built.
    if args.no_rtk:
        config.rtk_enabled = False

    store = Store(max_logs=config.log_buffer, max_scan_points=config.max_scan_points)
    app = create_app(config, store)

    stop = threading.Event()
    if config.udp_port:
        # The registry goes along so an `update` acked over UDP still lands in the
        # Software panel, the same as one acked over POST /api/ingest.
        threading.Thread(
            target=serve_udp,
            args=(config, store, stop, app.config["LIGMAX_DEPLOY"]),
            daemon=True,
            name="udp-ingest",
        ).start()
    threading.Thread(
        target=serve_housekeeping,
        args=(store, stop, app.config.get("LIGMAX_TRIPS")),
        daemon=True,
        name="housekeeping",
    ).start()
    # The RTK caster: the base station and the vessel are both on 4G, so this box
    # is the only place they can meet. Its own thread, and a bind failure only
    # disables RTK - the dashboard is what the operator cannot lose.
    caster = app.config.get("LIGMAX_RTK")
    if caster is not None:
        threading.Thread(
            target=caster.serve, args=(stop,), daemon=True, name="ntrip-caster"
        ).start()

    for warning in [*config.warnings, *protocol.check_shared_settings_sync()]:
        print(f"  !  {warning}")

    print(f"\n  Ligmax dashboard   http://{config.host}:{config.port}")
    if config.commands_enabled:
        print(f"  Admin              http://{config.host}:{config.port}/?key=<LIGMAX_ADMIN_KEY>")
    else:
        print("  Admin              disabled (set LIGMAX_ADMIN_KEY in .env)")
    if config.udp_port:
        print(f"  Vessel telemetry   udp://{config.udp_host}:{config.udp_port}")
    print(f"  Vessel telemetry   POST http://{config.host}:{config.port}/api/ingest")
    if caster is not None:
        fallback = config.rtk_source_password == FALLBACK_SOURCE_PASSWORD
        print(
            f"  RTK corrections    ntrip://{config.rtk_host}:{config.rtk_port}"
            f"/{config.rtk_mount}"
            + ("   (fallback source password - public!)" if fallback else "")
        )
    print(f"  Access             {'public read-only' if config.public_read else 'key required'}\n")

    try:
        # threaded=True is required: every open dashboard holds an SSE stream.
        app.run(
            host=config.host,
            port=config.port,
            debug=args.debug,
            threaded=True,
            use_reloader=args.debug,
        )
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
