#!/usr/bin/env python3
"""Viewer: accepts frames from the Jetson and serves them as a web page.

Run this on the machine the Jetson sends to (192.168.99.135 by default), then
open http://<that machine>:8080/ in a browser.

Two ports:
  3338  TCP, the Jetson connects here and streams frames (protocol.py)
  8080  HTTP, browsers connect here

Boxes are drawn server-side rather than by JavaScript in the browser. That costs a
JPEG decode/re-encode per frame, but this runs on a laptop with CPU to spare rather
than the Jetson, and it means the overlay can never be a frame out of step with the
image -- which client-side drawing would risk. Browsers get a plain
multipart/x-mixed-replace MJPEG stream, so no JS is needed to view it.

Requires: pillow  (pip install pillow)

  ./receiver.py                        # listen on 0.0.0.0:3338, serve on :8080
  ./receiver.py --http-port 8000 --no-draw
"""
import argparse
import io
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

import protocol

# Colours per detector class, plus a fallback.
COLOURS = {0: (60, 220, 90), 1: (240, 70, 70), 2: (250, 200, 40)}
FALLBACK = (200, 200, 200)


class Latest:
    """Newest frame per camera, plus a condition so viewers can wait for one."""

    def __init__(self):
        self.cv = threading.Condition()
        self.frames = {}        # cam -> (version, jpeg_bytes, header)
        self.version = 0
        self.stats = {}         # cam -> dict

    def put(self, cam, jpeg, header):
        with self.cv:
            self.version += 1
            self.frames[cam] = (self.version, jpeg, header)
            st = self.stats.setdefault(cam, {"count": 0, "t0": time.monotonic(),
                                             "fps": 0.0, "last": 0.0})
            st["count"] += 1
            st["last"] = time.time()
            el = time.monotonic() - st["t0"]
            if el >= 3.0:
                st["fps"] = st["count"] / el
                st["count"] = 0
                st["t0"] = time.monotonic()
            self.cv.notify_all()

    def wait_newer(self, cam, since, timeout=5.0):
        with self.cv:
            end = time.monotonic() + timeout
            while True:
                item = self.frames.get(cam)
                if item and item[0] > since:
                    return item
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def cameras(self):
        with self.cv:
            return sorted(self.frames)

    def snapshot(self):
        with self.cv:
            return {c: (v[2], dict(self.stats.get(c, {}))) for c, v in self.frames.items()}


LATEST = Latest()


def draw_overlay(jpeg, header):
    """Decode, draw boxes and labels, re-encode."""
    try:
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:
        return jpeg
    net_w = header.get("net_w") or im.width
    net_h = header.get("net_h") or im.height
    # Sender crops to the network aspect and scales uniformly, so one factor per axis.
    sx = im.width / float(net_w)
    sy = im.height / float(net_h)
    d = ImageDraw.Draw(im)

    for det in header.get("dets", []):
        box = det.get("box") or []
        if len(box) != 4:
            continue
        x1, y1, x2, y2 = (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)
        col = COLOURS.get(det.get("cls"), FALLBACK)
        d.rectangle([x1, y1, x2, y2], outline=col, width=2)

        label = f"{det.get('name', '?')} {det.get('conf', 0):.2f}"
        if det.get("card"):
            label += f" [{det['card']} {det.get('card_conf', 0):.2f}]"
        tw = d.textlength(label)
        ty = max(0, y1 - 12)
        d.rectangle([x1, ty, x1 + tw + 4, ty + 12], fill=col)
        d.text((x1 + 2, ty), label, fill=(0, 0, 0))

    tag = (f"cam{header.get('cam')}  {header.get('fps', 0):.1f} fps  "
           f"{len(header.get('dets', []))} det")
    d.rectangle([0, 0, d.textlength(tag) + 6, 14], fill=(0, 0, 0))
    d.text((3, 1), tag, fill=(255, 255, 255))

    out = io.BytesIO()
    im.save(out, format="JPEG", quality=80)
    return out.getvalue()


def ingest(conn, addr, draw):
    print(f"[recv] jetson connected from {addr[0]}:{addr[1]}", flush=True)
    n = 0
    try:
        while True:
            msg = protocol.read_message(conn)
            if msg is None:
                break
            header, jpeg = msg
            cam = int(header.get("cam", 0))
            if draw:
                jpeg = draw_overlay(jpeg, header)
            LATEST.put(cam, jpeg, header)
            n += 1
    except (OSError, ValueError) as e:
        print(f"[recv] {addr[0]} error: {e}", flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"[recv] {addr[0]} disconnected after {n} frames", flush=True)


def ingest_server(host, port, draw):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)
    print(f"[recv] listening for the Jetson on {host}:{port}", flush=True)
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=ingest, args=(conn, addr, draw), daemon=True).start()


PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Buoy detector live view</title>
<style>
  :root { color-scheme: dark light; }
  body { margin:0; background:#111; color:#eee;
         font:14px/1.4 system-ui,-apple-system,sans-serif; }
  header { padding:10px 14px; background:#000; display:flex; gap:16px;
           align-items:baseline; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; font-weight:600; }
  #status { color:#9a9; font-variant-numeric:tabular-nums; }
  .wrap { display:flex; flex-wrap:wrap; gap:10px; padding:10px; }
  figure { margin:0; flex:1 1 480px; min-width:320px; }
  figcaption { padding:4px 2px; color:#9a9; font-size:12px; }
  img { width:100%; height:auto; display:block; background:#000; border-radius:4px; }
</style>
<header>
  <h1>Buoy detector &mdash; live</h1>
  <span id="status">connecting&hellip;</span>
</header>
<div class="wrap" id="wrap"></div>
<script>
function build(cams) {
  const wrap = document.getElementById('wrap');
  wrap.innerHTML = '';
  if (!cams.length) {
    wrap.innerHTML = '<p style="padding:12px;color:#a88">'
      + 'No cameras yet. Waiting for the Jetson to connect&hellip;</p>';
    return;
  }
  for (const c of cams) {
    const f = document.createElement('figure');
    f.innerHTML = '<img src="/cam' + c + '/stream?t=' + Date.now() + '" alt="camera ' + c + '">'
                + '<figcaption>camera ' + c + '</figcaption>';
    wrap.appendChild(f);
  }
}
let shown = '';
async function poll() {
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    const j = await r.json();
    const key = j.cameras.join(',');
    if (key !== shown) { shown = key; build(j.cameras); }
    document.getElementById('status').textContent = j.cameras.length
      ? j.cameras.map(c => 'cam'+c+': '+(j.fps[c]||0).toFixed(1)+' fps, '
          + (j.dets[c]||0) + ' det').join('   |   ')
      : 'waiting for the Jetson';
  } catch (e) {
    document.getElementById('status').textContent = 'viewer unreachable';
  }
  setTimeout(poll, 1000);
}
poll();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # one line per MJPEG frame would be unreadable

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"))
            return
        if path == "/api/status":
            import json
            snap = LATEST.snapshot()
            body = json.dumps({
                "cameras": sorted(snap),
                "fps": {c: round(st.get("fps", 0.0), 2) for c, (_, st) in snap.items()},
                "dets": {c: len(hdr.get("dets", [])) for c, (hdr, _) in snap.items()},
                "detail": {c: hdr.get("dets", []) for c, (hdr, _) in snap.items()},
            }).encode()
            self._send(body, "application/json")
            return
        if path.startswith("/cam") and path.endswith("/stream"):
            try:
                cam = int(path[4:path.index("/", 4)])
            except (ValueError, IndexError):
                self._send(b"bad camera", "text/plain", 400)
                return
            self.stream(cam)
            return
        if path.startswith("/cam") and path.endswith(".jpg"):
            try:
                cam = int(path[4:-4])
            except ValueError:
                self._send(b"bad camera", "text/plain", 400)
                return
            item = LATEST.frames.get(cam)
            if not item:
                self._send(b"no frame yet", "text/plain", 404)
                return
            self._send(item[1], "image/jpeg")
            return
        self._send(b"not found", "text/plain", 404)

    def stream(self, cam):
        boundary = "buoyframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = 0
        try:
            while True:
                item = LATEST.wait_newer(cam, seen, timeout=10.0)
                if item is None:
                    continue        # keep the connection open through a quiet spell
                seen, jpeg, _ = item
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0", help="interface for the Jetson feed")
    ap.add_argument("--port", type=int, default=3338, help="TCP port the Jetson sends to")
    ap.add_argument("--http-port", type=int, default=8080, help="port browsers use")
    ap.add_argument("--no-draw", action="store_true",
                    help="serve frames unannotated (boxes still on /api/status)")
    args = ap.parse_args()

    threading.Thread(target=ingest_server,
                     args=(args.bind, args.port, not args.no_draw),
                     daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)
    httpd.daemon_threads = True
    print(f"[http] open http://<this-machine>:{args.http_port}/ in a browser",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
