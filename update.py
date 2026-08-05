"""Pull and restart the dashboard. Spawned detached by the Update button.

Deliberately imports nothing from ligmax_gui: the code underneath is about to
change, and this has to survive that.
"""

import os
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("LIGMAX_PORT", "3338"))


def say(msg):
    with open(os.path.join(REPO, "update.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def port_free():
    # No SO_REUSEADDR: on Windows it would let us bind a port still in use, so
    # the check would pass while the old server is very much alive.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
            return True
        except OSError:
            return False


for _ in range(30):
    if port_free():
        break
    time.sleep(1)
else:
    say(f"port {PORT} still held after 30s; starting anyway")

pull = subprocess.run(
    ["git", "-C", REPO, "pull", "--ff-only"], capture_output=True, text=True
)
say((pull.stdout + pull.stderr).strip().replace("\n", " ") or "pull: nothing to say")

flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
subprocess.Popen([sys.executable, "run.py"], cwd=REPO, creationflags=flags)
say("restarted")
