/* The camera panel: pictures pushed up from the Jetson, off unless asked for.
 *
 * There are two separate switches here and confusing them wastes bandwidth on
 * the link that matters:
 *
 *   the vessel switch   POST /api/camera/config {enabled}. Admin only. Tells
 *                       ligmax-json.local whether to send anything at all, so
 *                       this is the one that costs 4G *uplink* — the same link
 *                       the telemetry and the E-stop ack come back through.
 *   the viewer switch   local, per browser, remembered in prefs. Stops *this*
 *                       tab downloading frames. Costs nothing on the boat and
 *                       is available to read-only viewers.
 *
 * So an operator on a metered laptop turns off their own download; an operator
 * who wants the uplink back for telemetry turns off the vessel's stream.
 *
 * Frames are fetched as ordinary <img> loads rather than an MJPEG stream: an
 * MJPEG connection held open through Cloudflare and Caddy is one more thing to
 * go wrong, and at the 1-4 fps this link can afford there is nothing to gain.
 * Each load is preloaded and swapped in, so a slow frame never blanks the tile.
 */

import * as fmt from './format.js';

const STATE_POLL_ON_MS = 1000;
const STATE_POLL_OFF_MS = 5000;

// Never ask for frames faster than the vessel is sending them, whatever the
// config says: a 404 storm against a stopped sender is pure waste.
const MIN_FRAME_INTERVAL_MS = 120;

export async function fetchCameraState() {
  const response = await fetch('/api/camera/state', { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`camera state failed: ${response.status}`);
  return response.json();
}

export async function setCameraConfig(changes) {
  const response = await fetch('/api/camera/config', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `camera config failed (${response.status})`);
  return payload;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

class CameraTile {
  constructor(id) {
    this.id = id;
    this.seq = null; // what the server says is current
    this.lastSeq = null; // what we have asked for
    this.loading = false;

    this.root = element('figure', 'cam-tile');
    this.image = element('img', 'cam-img');
    this.image.alt = `Camera ${id}`;
    this.image.decoding = 'async';
    this.image.loading = 'eager';
    this.image.hidden = true;

    this.placeholder = element('div', 'cam-placeholder', 'no picture');
    this.caption = element('figcaption', 'cam-caption');
    this.name = element('span', 'cam-name', `cam${id}`);
    this.meta = element('span', 'cam-meta', '—');
    this.caption.append(this.name, this.meta);

    this.root.append(this.image, this.placeholder, this.caption);
  }

  /** Meta from /api/camera/state. Returns true if a new frame is worth fetching. */
  describe(info) {
    const parts = [];
    if (info.width && info.height) parts.push(`${info.width}×${info.height}`);
    if (info.hz) parts.push(`${fmt.fixed(info.hz, 1)} fps`);
    if (Number.isFinite(info.bytes)) parts.push(fmt.bytes(info.bytes));
    if (Number.isFinite(info.age) && info.age > 1.5) parts.push(`${fmt.fixed(info.age, 1)} s old`);
    const text = parts.join(' · ') || '—';
    if (this.meta.textContent !== text) this.meta.textContent = text;
    if (info.label && this.name.textContent !== info.label) this.name.textContent = info.label;

    this.root.dataset.live = String(Boolean(info.live));
    this.seq = info.seq ?? null;
    return info.live && info.seq !== this.lastSeq;
  }

  /** Pull the current frame. No-op while one is already in flight. */
  fetchFrame() {
    if (this.loading) return;
    this.loading = true;
    // Remember which frame this request is for *before* it lands, so a stream
    // that stalls does not get re-requested every tick.
    this.lastSeq = this.seq;

    // Preload into a detached Image and swap on success: assigning straight to
    // the visible <img> blanks the tile for the duration of the request, which
    // on a lumpy uplink is most of the time.
    const next = new Image();
    next.addEventListener('load', () => {
      this.loading = false;
      this.image.src = next.src;
      this.image.hidden = false;
      this.placeholder.hidden = true;
    });
    next.addEventListener('error', () => {
      this.loading = false;
      // Let the next tick retry: the frame may simply have aged out between the
      // state poll saying "live" and this request arriving.
      this.lastSeq = null;
    });
    next.src = `/api/camera/${encodeURIComponent(this.id)}.jpg?t=${Date.now()}`;
  }

  blank(reason) {
    this.image.hidden = true;
    this.image.removeAttribute('src');
    this.placeholder.hidden = false;
    this.placeholder.textContent = reason;
    this.lastSeq = null;
  }

  destroy() {
    this.root.remove();
  }
}

export class CameraPanel {
  /**
   * @param {object} nodes  `{card, grid, status, toggle, viewerToggle, quality}`
   *   — every one optional, so a page can carry a cut-down version.
   * @param {object} options `{admin, notify, prefs, savePrefs}`
   */
  constructor(nodes, { admin = false, notify = () => {}, prefs = {}, savePrefs = () => {} } = {}) {
    this.nodes = nodes;
    this.admin = admin;
    this.notify = notify;
    this.prefs = prefs;
    this.savePrefs = savePrefs;

    this.tiles = new Map();
    this.state = null;
    this.stateTimer = null;
    this.frameTimer = null;
    // The viewer's own download switch. Defaults to showing frames *if* the
    // vessel is sending them — the expensive default (making the boat send) is
    // the one that stays off.
    this.viewerOn = prefs.cameraVisible ?? true;

    this._wireControls();
  }

  _wireControls() {
    const { toggle, viewerToggle, quality } = this.nodes;

    if (toggle) {
      toggle.disabled = !this.admin;
      toggle.title = this.admin
        ? 'Start or stop the vessel sending video over 4G'
        : 'Operator key required to change what the vessel sends';
      toggle.addEventListener('click', async () => {
        const enabled = !this.state?.stream?.enabled;
        toggle.disabled = true;
        try {
          const { stream } = await setCameraConfig({ enabled });
          this.notify(
            enabled
              ? `Camera stream on — ${stream.max_width} px at ${stream.fps} fps over the uplink.`
              : 'Camera stream stopped. The uplink is telemetry-only again.',
            'ok',
            5000
          );
          await this.refreshState();
        } catch (error) {
          this.notify(error.message, 'error');
        } finally {
          toggle.disabled = !this.admin;
        }
      });
    }

    if (viewerToggle) {
      viewerToggle.addEventListener('click', () => {
        this.viewerOn = !this.viewerOn;
        this.prefs.cameraVisible = this.viewerOn;
        this.savePrefs(this.prefs);
        if (!this.viewerOn) {
          for (const tile of this.tiles.values()) tile.blank('hidden in this tab');
        }
        this.render();
      });
    }

    if (quality) {
      quality.disabled = !this.admin;
      quality.addEventListener('change', async () => {
        const [max_width, fps, jpeg_quality] = quality.value.split('/').map(Number);
        try {
          await setCameraConfig({ max_width, fps, jpeg_quality });
          await this.refreshState();
        } catch (error) {
          this.notify(error.message, 'error');
        }
      });
    }
  }

  start() {
    this.refreshState();
    this._scheduleState();
    return this;
  }

  stop() {
    window.clearTimeout(this.stateTimer);
    window.clearTimeout(this.frameTimer);
  }

  _scheduleState() {
    window.clearTimeout(this.stateTimer);
    const interval = this.state?.stream?.enabled ? STATE_POLL_ON_MS : STATE_POLL_OFF_MS;
    this.stateTimer = window.setTimeout(async () => {
      await this.refreshState();
      this._scheduleState();
    }, interval);
  }

  async refreshState() {
    try {
      this.state = await fetchCameraState();
    } catch {
      this.state = null;
    }
    this.render();
    this._scheduleFrames();
  }

  _scheduleFrames() {
    window.clearTimeout(this.frameTimer);
    const stream = this.state?.stream;
    if (!stream?.enabled || !this.viewerOn) return;

    const fps = Number(stream.fps) || 2;
    const interval = Math.max(MIN_FRAME_INTERVAL_MS, 1000 / fps);
    const tick = () => {
      for (const info of this.state?.cameras ?? []) {
        const tile = this.tiles.get(info.id);
        if (tile && info.live) tile.fetchFrame();
      }
      this.frameTimer = window.setTimeout(tick, interval);
    };
    this.frameTimer = window.setTimeout(tick, interval);
  }

  render() {
    const { card, grid, status, toggle, viewerToggle, quality } = this.nodes;
    const stream = this.state?.stream;
    const cameras = this.state?.cameras ?? [];

    if (toggle) {
      const on = Boolean(stream?.enabled);
      toggle.textContent = on ? 'Stop sending video' : 'Send video from the boat';
      toggle.classList.toggle('is-on', on);
      toggle.setAttribute('aria-pressed', String(on));
    }
    if (viewerToggle) {
      viewerToggle.textContent = this.viewerOn ? 'Hide in this tab' : 'Show in this tab';
      viewerToggle.classList.toggle('is-on', this.viewerOn);
      viewerToggle.setAttribute('aria-pressed', String(this.viewerOn));
    }
    if (quality && stream) {
      const value = `${stream.max_width}/${stream.fps}/${stream.jpeg_quality}`;
      if ([...quality.options].some((option) => option.value === value)) quality.value = value;
    }
    if (card) card.dataset.streaming = String(Boolean(stream?.enabled));

    if (status) status.textContent = this._statusLine();

    if (!grid) return;

    // Tiles follow whatever the vessel is actually sending, so a camera that
    // drops out disappears rather than sitting there as a stale picture.
    const wanted = new Set(cameras.map((camera) => camera.id));
    for (const [id, tile] of [...this.tiles]) {
      if (!wanted.has(id)) {
        tile.destroy();
        this.tiles.delete(id);
      }
    }
    for (const info of cameras) {
      let tile = this.tiles.get(info.id);
      if (!tile) {
        tile = new CameraTile(info.id);
        this.tiles.set(info.id, tile);
        grid.append(tile.root);
      }
      const fresh = tile.describe(info);
      if (!this.viewerOn) tile.blank('hidden in this tab');
      else if (!info.live) tile.blank('no recent frame');
      else if (fresh) tile.fetchFrame();
    }
    grid.dataset.empty = String(cameras.length === 0);
  }

  /** One line saying which of the several "no picture" cases this is. */
  _statusLine() {
    if (!this.state) return 'Camera state unavailable — the server did not answer.';
    const {
      stream,
      cameras,
      last_poll_age: pollAge,
      frames_received: frames,
      refused,
      last_refusal: refusal,
      last_refusal_age: refusalAge,
    } = this.state;

    // Something is asking and being turned away. Say so before anything else:
    // every other line below would blame the Jetson for being silent, and it is
    // not silent — it is unauthenticated. Almost always LIGMAX_BOAT_KEY missing
    // from /etc/ligmax/node.env on ligmax-json.local.
    const refusedRecently = refused > 0 && refusalAge !== null && refusalAge < 60;
    if (refusedRecently) {
      return `Refused: ${refusal || 'unauthorised'} (${refused} in this session, last ${fmt.ago(refusalAge)}). The boat is reaching the dashboard but its key is wrong — check LIGMAX_BOAT_KEY on ligmax-json.local.`;
    }

    if (!stream.enabled) {
      return this.admin
        ? 'Off. The boat sends nothing until you ask — video shares the 4G uplink with telemetry.'
        : 'Off. An operator has to switch the vessel stream on.';
    }
    if (!cameras.length) {
      // Distinguishing these two is the whole reason the panel polls a state
      // endpoint instead of just pointing an <img> at the frame route.
      if (pollAge === null) {
        return 'On, but ligmax-json.local has never asked for the config. Check the Jetson uploader is running.';
      }
      if (pollAge > 30) {
        return `On, but the Jetson last checked in ${fmt.ago(pollAge)} — it is not sending.`;
      }
      return 'On. The Jetson has the config and the first frame has not landed yet.';
    }
    const live = cameras.filter((camera) => camera.live).length;
    const hz = cameras.reduce((total, camera) => total + (camera.hz || 0), 0);
    const bytes = cameras.reduce((total, camera) => total + (camera.bytes || 0), 0);
    const estimate = hz * bytes / Math.max(cameras.length, 1);
    return [
      `${live}/${cameras.length} live`,
      `${stream.max_width} px · q${stream.jpeg_quality} · ${stream.fps} fps asked`,
      hz ? `${fmt.fixed(hz, 1)} fps in` : null,
      estimate ? `~${fmt.bytes(estimate)}/s uplink` : null,
      frames ? `${frames.toLocaleString('en-GB')} frames` : null,
    ]
      .filter(Boolean)
      .join(' · ');
  }
}
