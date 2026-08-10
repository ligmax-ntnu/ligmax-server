/* The world model: obstacles, no-go zones, planned path and the vessel,
 * drawn in the boat's own metre grid.
 *
 * The view is always grid-aligned — +x right, +y up — because that is the
 * frame the autonomy code reasons in, and a debug view that silently rotates
 * is a debug view you cannot trust. When the grid is not aligned to north it
 * is the map imagery underneath that gets rotated, not the grid.
 */

import { CARDINALS, letterOf, nameOf, styleOf } from './obstacles.js';
import { zonesFor } from './nogo.js';
import { styleOfRole } from './plan.js';
import { TileLayer } from './tiles.js';

const PALETTE = {
  ink: '#eaf1fd',
  muted: '#9db8e8',
  faint: 'rgba(157, 184, 232, 0.35)',
  grid: 'rgba(157, 184, 232, 0.13)',
  gridMajor: 'rgba(157, 184, 232, 0.26)',
  axis: 'rgba(157, 184, 232, 0.45)',
  nogo: '#e2453f',
  path: '#56d0ff',
  pathAlt: '#6f8ab5',
  // The ideal route the course was set out as, from its GNSS points. Amber so it
  // never gets mistaken for the cyan path the planner actually chose — the whole
  // point of drawing both is that the gap between them is visible.
  route: '#f0b23c',
  // A mission still being laid, before it is sent. Distinct from `route` on
  // purpose: this is not yet the vessel's ideal route (that colour only
  // appears once the vessel has echoed the upload back), it is a local, unsent
  // draft — solid white keeps the two from ever being mistaken for each other.
  draft: '#ffffff',
  scan: '#7fd4ff',
  boat: '#ffffff',
  trail: '#7fb0ff',
  cog: '#9ee6a8',
  // The parking space the vessel has fitted to three lidar lines, and the dot in
  // the middle of it that it is driving to. Amber-gold, matching the `park` role's
  // diamond in `plan.js`, so the waypoint and the space it turned out to be read
  // as the same thing. The dot itself is white and is the only white circle on the
  // chart — it is the one point the whole manoeuvre is about.
  park: '#ffc21f',
  parkFaint: 'rgba(255, 194, 31, 0.42)',
  parkDot: '#ffffff',
};

const GRID_STEPS = [0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000];
const MIN_PPM = 0.15;
const MAX_PPM = 60;
const TRAIL_LIMIT = 1800;
const TRAIL_MAX_AGE = 300; // seconds

/** Path `kind` values that mean "this is the reference, not a plan". */
const REFERENCE_KINDS = new Set(['reference', 'ideal', 'course', 'survey']);

export const DEFAULT_LAYERS = {
  tiles: true,
  grid: true,
  nogo: true,
  radii: true,
  scan: true,
  paths: true,
  route: true,
  trail: true,
  cog: true,
  labels: true,
  ids: false,
};

export class WorldMap {
  constructor({ canvas, wrap, store, onHover, onPick }) {
    this.canvas = canvas;
    this.wrap = wrap;
    this.store = store;
    this.onHover = onHover;
    this.onPick = onPick;

    this.ctx = canvas.getContext('2d');
    this.layers = { ...DEFAULT_LAYERS };
    this.camera = { cx: 0, cy: 40, ppm: 5.5 };
    this.follow = true;
    this.pickMode = false;
    // A course being laid: `missionMode` on means a click adds a waypoint rather
    // than panning, and `missionDraft` is what has been placed so far — each
    // entry `{x, y, role}` — held here (not in the command panel) because it is
    // drawn every frame like everything else on the chart. Survives
    // `setMissionMode(false)` so toggling out to pan and back in does not lose
    // progress.
    //
    // `missionRole` is what the *next* click gets. A course runs in stretches of
    // one role rather than alternating, so it sticks until it is changed.
    this.missionMode = false;
    this.missionDraft = [];
    this.missionRole = 'transit';

    this.width = 0;
    this.height = 0;
    this.ratio = 1;
    this.dirty = true;
    this.settling = false;

    this.trail = [];
    this.hovered = null;
    this.pointer = null;
    this._pan = null;
    this._hasFitted = false;

    this.tiles = new TileLayer(() => this.invalidate());
    this._hatch = null;

    this._bindEvents();
    this._resize();
  }

  // -- lifecycle ---------------------------------------------------------

  start() {
    const frame = () => {
      if (this.dirty || this.settling) this._draw();
      this._raf = requestAnimationFrame(frame);
    };
    this._raf = requestAnimationFrame(frame);
  }

  stop() {
    cancelAnimationFrame(this._raf);
  }

  invalidate() {
    this.dirty = true;
  }

  setLayer(name, on) {
    this.layers[name] = on;
    if (name === 'tiles') this.tiles.setProvider(on ? this._basemap ?? 'satellite' : 'none');
    this.invalidate();
  }

  setBasemap(name) {
    this._basemap = name;
    this.tiles.setProvider(name);
    this.layers.tiles = name !== 'none';
    this.invalidate();
  }

  setFollow(on) {
    this.follow = on;
    if (on) this.settling = true;
    this.invalidate();
  }

  setPickMode(on) {
    this.pickMode = on;
    this.canvas.classList.toggle('is-picking', on);
  }

  /** Arm/disarm mission-laying. Clicks add waypoints instead of panning while on. */
  setMissionMode(on) {
    this.missionMode = on;
    this.canvas.classList.toggle('is-picking', on || this.pickMode);
    this.invalidate();
  }

  /** What role a click adds from now on. Existing points keep theirs. */
  setMissionRole(role) {
    this.missionRole = role;
    this.invalidate();
  }

  addMissionPoint([x, y]) {
    this.missionDraft.push({ x, y, role: this.missionRole });
    this.onMissionChange?.(this.missionDraft);
    this.invalidate();
  }

  undoMissionPoint() {
    this.missionDraft.pop();
    this.onMissionChange?.(this.missionDraft);
    this.invalidate();
  }

  clearMissionDraft() {
    this.missionDraft = [];
    this.onMissionChange?.(this.missionDraft);
    this.invalidate();
  }

  /** Called whenever a new telemetry frame lands. */
  onState() {
    const boat = this.store.state.boat;
    if (boat?.position) {
      const now = Date.now() / 1000;
      const last = this.trail.at(-1);
      // Only extend the trail once the vessel has actually moved, so sitting
      // still does not fill the buffer with duplicate points.
      if (!last || Math.hypot(boat.position[0] - last.x, boat.position[1] - last.y) > 0.15) {
        this.trail.push({ x: boat.position[0], y: boat.position[1], t: now });
      }
      while (
        this.trail.length > TRAIL_LIMIT ||
        (this.trail.length > 1 && now - this.trail[0].t > TRAIL_MAX_AGE)
      ) {
        this.trail.shift();
      }
      if (this.follow) this.settling = true;
      if (!this._hasFitted && this.width > 0) {
        this._hasFitted = true;
        // Frame the whole course once, then hand the view back to follow mode
        // so the vessel stays centred as it moves.
        this.fit();
        this.setFollow(true);
        this.onLayerChange?.();
      }
    }
    this.invalidate();
  }

  clearTrail() {
    this.trail = [];
    this.invalidate();
  }

  // -- transforms --------------------------------------------------------

  worldToScreen(x, y) {
    const { cx, cy, ppm } = this.camera;
    return [this.width / 2 + (x - cx) * ppm, this.height / 2 - (y - cy) * ppm];
  }

  screenToWorld(px, py) {
    const { cx, cy, ppm } = this.camera;
    return [cx + (px - this.width / 2) / ppm, cy - (py - this.height / 2) / ppm];
  }

  zoomBy(factor, anchor) {
    const { ppm } = this.camera;
    const next = Math.max(MIN_PPM, Math.min(MAX_PPM, ppm * factor));
    if (next === ppm) return;

    if (anchor) {
      // Keep the world point under the cursor pinned while zooming.
      const [wx, wy] = this.screenToWorld(anchor[0], anchor[1]);
      this.camera.ppm = next;
      const [sx, sy] = this.worldToScreen(wx, wy);
      this.camera.cx += (anchor[0] - sx) / next;
      this.camera.cy -= (anchor[1] - sy) / next;
    } else {
      this.camera.ppm = next;
    }
    this.invalidate();
  }

  /** Frame everything currently known: vessel, tracks and path. */
  fit() {
    const { boat, tracks, paths } = this.store.state;
    const xs = [];
    const ys = [];

    const include = (point, pad = 0) => {
      if (!Array.isArray(point)) return;
      xs.push(point[0] - pad, point[0] + pad);
      ys.push(point[1] - pad, point[1] + pad);
    };

    include(boat?.position, 6);
    for (const track of tracks ?? []) include(track.position, (track.avoid_radius ?? 0) + 2);
    for (const path of paths ?? []) for (const point of path.points ?? []) include(point, 2);
    // A course being laid is the thing most likely to be off screen — you zoom
    // out to place the far end of it — so "fit everything" has to mean it too.
    for (const point of this.missionDraft) include([point.x, point.y], 2);

    if (!xs.length) {
      this.camera = { cx: 0, cy: 40, ppm: 5.5 };
      this.invalidate();
      return;
    }

    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const spanX = Math.max(maxX - minX, 12);
    const spanY = Math.max(maxY - minY, 12);

    this.camera.cx = (minX + maxX) / 2;
    this.camera.cy = (minY + maxY) / 2;
    this.camera.ppm = Math.max(
      MIN_PPM,
      Math.min(MAX_PPM, Math.min((this.width * 0.9) / spanX, (this.height * 0.9) / spanY))
    );
    this.follow = false;
    this.invalidate();
  }

  // -- events ------------------------------------------------------------

  _bindEvents() {
    const canvas = this.canvas;

    new ResizeObserver(() => this._resize()).observe(this.wrap);

    canvas.addEventListener('pointerdown', (event) => {
      if (this.missionMode && event.button === 0) {
        this.addMissionPoint(this._pointerWorld(event));
        return;
      }
      if (this.pickMode && event.button === 0) {
        const [wx, wy] = this._pointerWorld(event);
        this.onPick?.([wx, wy]);
        return;
      }
      canvas.setPointerCapture(event.pointerId);
      this._pan = { x: event.clientX, y: event.clientY };
      canvas.classList.add('is-panning');
      this._armLongPress(event);
    });

    canvas.addEventListener('pointermove', (event) => {
      const rect = canvas.getBoundingClientRect();
      this.pointer = [event.clientX - rect.left, event.clientY - rect.top];

      if (this._pan) {
        const dx = event.clientX - this._pan.x;
        const dy = event.clientY - this._pan.y;
        // Any real movement is a pan, not a press-and-hold.
        if (Math.hypot(dx, dy) > 1.5) this._cancelLongPress();
        this._pan = { x: event.clientX, y: event.clientY };
        this.camera.cx -= dx / this.camera.ppm;
        this.camera.cy += dy / this.camera.ppm;
        if (this.follow) this.setFollow(false);
        this.onHover?.(null);
        this.invalidate();
        return;
      }

      this._updateHover();
      this.invalidate();
    });

    const endPan = (event) => {
      this._cancelLongPress();
      if (!this._pan) return;
      this._pan = null;
      canvas.classList.remove('is-panning');
      canvas.releasePointerCapture?.(event.pointerId);
    };
    canvas.addEventListener('pointerup', endPan);
    canvas.addEventListener('pointercancel', endPan);

    // Right-click a track to delete it. Deliberately not a plain left click:
    // the chart is panned by dragging and tapped to lay waypoints, and a
    // mis-click that removes a real mark from the world model is not something
    // to make one pixel of travel away from panning the map.
    canvas.addEventListener('contextmenu', (event) => {
      const [px, py] = this._pointerScreen(event);
      const hit = this._trackAt(px, py, 20);
      if (!hit) return; // no track under the cursor: leave the browser menu alone
      event.preventDefault();
      this._cancelLongPress();
      this.onTrackDelete?.(hit.track);
    });

    canvas.addEventListener('pointerleave', () => {
      this._cancelLongPress();
      this.pointer = null;
      this.hovered = null;
      this.onHover?.(null);
      this.invalidate();
    });

    canvas.addEventListener(
      'wheel',
      (event) => {
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const anchor = [event.clientX - rect.left, event.clientY - rect.top];
        this.zoomBy(Math.exp(-event.deltaY * 0.0015), anchor);
      },
      { passive: false }
    );

    canvas.addEventListener('dblclick', (event) => {
      const [wx, wy] = this._pointerWorld(event);
      this.camera.cx = wx;
      this.camera.cy = wy;
      this.setFollow(false);
      this.invalidate();
    });

    canvas.addEventListener('keydown', (event) => {
      const actions = {
        f: () => this.setFollow(!this.follow),
        g: () => this.setLayer('grid', !this.layers.grid),
        n: () => this.setLayer('nogo', !this.layers.nogo),
        s: () => this.setLayer('scan', !this.layers.scan),
        r: () => this.fit(),
        '+': () => this.zoomBy(1.3),
        '=': () => this.zoomBy(1.3),
        '-': () => this.zoomBy(1 / 1.3),
      };
      const action = actions[event.key.toLowerCase()] ?? actions[event.key];
      if (action) {
        event.preventDefault();
        action();
        this.onLayerChange?.();
      }
    });
  }

  _pointerWorld(event) {
    const rect = this.canvas.getBoundingClientRect();
    return this.screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
  }

  /**
   * Press-and-hold on a track is the touch equivalent of a right-click.
   *
   * `contextmenu` covers the desktop but is not dependable under a finger —
   * iOS Safari in particular does not raise it — and the phone on the dock is
   * the case this whole panel is designed around. So the gesture is measured
   * here instead of being delegated to the browser, and it is cancelled by any
   * movement, so a pan that happens to start on a buoy stays a pan.
   */
  _armLongPress(event) {
    this._cancelLongPress();
    if (event.pointerType === 'mouse') return; // the mouse has a real right button
    const [px, py] = this._pointerScreen(event);
    if (!this._trackAt(px, py, 20)) return;
    this._longPress = window.setTimeout(() => {
      this._longPress = null;
      const hit = this._trackAt(px, py, 20);
      // Re-tested at fire time: half a second is long enough for the track to
      // have moved or been dropped, and deleting whatever has since drifted
      // under the finger would be worse than doing nothing.
      if (hit) this.onTrackDelete?.(hit.track);
    }, 550);
  }

  _cancelLongPress() {
    if (this._longPress) {
      window.clearTimeout(this._longPress);
      this._longPress = null;
    }
  }

  /**
   * The track nearest a screen point, within `radius` px, or null.
   *
   * Shared by hover and by the delete gesture so the two can never disagree
   * about what is under the finger — "it highlighted one buoy and deleted a
   * different one" is not a bug worth ever making possible.
   */
  _trackAt(px, py, radius = 16) {
    let best = null;
    let bestDistance = radius;
    for (const track of this.store.state.tracks ?? []) {
      const [sx, sy] = this.worldToScreen(track.position[0], track.position[1]);
      const distance = Math.hypot(sx - px, sy - py);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = { track, screen: [sx, sy] };
      }
    }
    return best;
  }

  _updateHover() {
    if (!this.pointer) return;
    const best = this._trackAt(this.pointer[0], this.pointer[1]);
    const changed = best?.track?.track_id !== this.hovered?.track?.track_id;
    this.hovered = best;
    if (changed) this.onHover?.(best);
  }

  /** Screen coordinates of a pointer event, relative to the canvas. */
  _pointerScreen(event) {
    const rect = this.canvas.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  }

  _resize() {
    this.ratio = window.devicePixelRatio || 1;
    const width = this.wrap.clientWidth;
    const height = this.wrap.clientHeight;
    if (!width || !height) return;
    this.width = width;
    this.height = height;
    this.canvas.width = Math.round(width * this.ratio);
    this.canvas.height = Math.round(height * this.ratio);
    this.invalidate();
  }

  // -- drawing -----------------------------------------------------------

  _draw() {
    this.dirty = false;
    const ctx = this.ctx;
    if (!this.width || !this.height) return;

    if (this.follow) this._settleOnBoat();

    ctx.setTransform(this.ratio, 0, 0, this.ratio, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);

    const gradient = ctx.createLinearGradient(0, 0, 0, this.height);
    gradient.addColorStop(0, '#0e1834');
    gradient.addColorStop(1, '#16244c');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, this.width, this.height);

    const state = this.store.state;

    if (this.layers.tiles) {
      this.tiles.draw(ctx, {
        origin: state.origin,
        gridBearing: state.grid_bearing ?? 0,
        worldToScreen: (x, y) => this.worldToScreen(x, y),
        pixelsPerMetre: this.camera.ppm * this.ratio,
        width: this.width,
        height: this.height,
        ratio: this.ratio,
      });
      ctx.setTransform(this.ratio, 0, 0, this.ratio, 0, 0);
    }

    if (this.layers.grid) this._drawGrid(ctx);
    if (this.layers.nogo) this._drawNoGo(ctx, state);
    if (this.layers.scan) this._drawScan(ctx, state);
    if (this.layers.paths) this._drawPaths(ctx, state);
    if (this.missionDraft.length) this._drawMissionDraft(ctx);
    this._drawTracks(ctx, state);
    if (this.layers.trail) this._drawTrail(ctx);
    // Under the boat, deliberately: the hull is what you are steering and it must
    // never be hidden by the space it is steering into.
    this._drawParking(ctx, state);
    this._drawBoat(ctx, state);
    this._drawHud(ctx, state);
  }

  _settleOnBoat() {
    const position = this.store.state.boat?.position;
    if (!position) {
      this.settling = false;
      return;
    }
    const dx = position[0] - this.camera.cx;
    const dy = position[1] - this.camera.cy;
    if (Math.hypot(dx, dy) < 0.02) {
      this.camera.cx = position[0];
      this.camera.cy = position[1];
      this.settling = false;
      return;
    }
    this.camera.cx += dx * 0.28;
    this.camera.cy += dy * 0.28;
    this.settling = true;
  }

  _gridStep() {
    const target = 62; // aim for a line roughly every 62 px
    return GRID_STEPS.find((step) => step * this.camera.ppm >= target) ?? GRID_STEPS.at(-1);
  }

  _drawGrid(ctx) {
    const step = this._gridStep();
    const [left, top] = this.screenToWorld(0, 0);
    const [right, bottom] = this.screenToWorld(this.width, this.height);

    ctx.lineWidth = 1;
    ctx.font = '10px ui-monospace, monospace';
    ctx.textBaseline = 'bottom';

    const startX = Math.ceil(left / step) * step;
    const startY = Math.ceil(bottom / step) * step;

    for (let x = startX; x <= right; x += step) {
      const major = Math.abs(x / (step * 5) - Math.round(x / (step * 5))) < 1e-6;
      const [sx] = this.worldToScreen(x, 0);
      ctx.strokeStyle = Math.abs(x) < 1e-9 ? PALETTE.axis : major ? PALETTE.gridMajor : PALETTE.grid;
      ctx.beginPath();
      ctx.moveTo(Math.round(sx) + 0.5, 0);
      ctx.lineTo(Math.round(sx) + 0.5, this.height);
      ctx.stroke();
      if (major) {
        ctx.fillStyle = PALETTE.faint;
        ctx.textAlign = 'left';
        ctx.fillText(`${this._formatMetres(x)}`, sx + 3, this.height - 2);
      }
    }

    for (let y = startY; y <= top; y += step) {
      const major = Math.abs(y / (step * 5) - Math.round(y / (step * 5))) < 1e-6;
      const [, sy] = this.worldToScreen(0, y);
      ctx.strokeStyle = Math.abs(y) < 1e-9 ? PALETTE.axis : major ? PALETTE.gridMajor : PALETTE.grid;
      ctx.beginPath();
      ctx.moveTo(0, Math.round(sy) + 0.5);
      ctx.lineTo(this.width, Math.round(sy) + 0.5);
      ctx.stroke();
      if (major) {
        ctx.fillStyle = PALETTE.faint;
        ctx.textAlign = 'left';
        ctx.fillText(`${this._formatMetres(y)}`, 3, sy - 2);
      }
    }

    // Mark the grid origin — the GPS fix the vessel booted at.
    const [ox, oy] = this.worldToScreen(0, 0);
    if (ox > -40 && ox < this.width + 40 && oy > -40 && oy < this.height + 40) {
      ctx.strokeStyle = PALETTE.axis;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(ox, oy, 5, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = PALETTE.faint;
      ctx.textAlign = 'left';
      ctx.fillText('origin', ox + 8, oy - 4);
    }
  }

  _formatMetres(value) {
    if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return Number.isInteger(value) ? `${value}` : value.toFixed(1);
  }

  _hatchPattern(ctx) {
    if (this._hatch) return this._hatch;
    const size = 8;
    const tile = document.createElement('canvas');
    tile.width = size;
    tile.height = size;
    const tileCtx = tile.getContext('2d');
    tileCtx.strokeStyle = 'rgba(226, 69, 63, 0.55)';
    tileCtx.lineWidth = 1.1;
    tileCtx.beginPath();
    tileCtx.moveTo(-size, size);
    tileCtx.lineTo(size, -size);
    tileCtx.moveTo(0, size * 2);
    tileCtx.lineTo(size * 2, 0);
    tileCtx.stroke();
    this._hatch = ctx.createPattern(tile, 'repeat');
    return this._hatch;
  }

  _drawNoGo(ctx, state) {
    const tracks = state.tracks ?? [];
    if (!tracks.length) return;

    const options = {
      upstreamDirection: state.upstream_direction,
      wrongSideLength: this.store.session.wrong_side_length ?? 20,
    };

    // One combined path, filled once: overlapping zones must not stack up into
    // a darker blob, or "how forbidden" starts looking like a gradient.
    const combined = new Path2D();
    const outlines = [];

    for (const track of tracks) {
      const { disc, corridor } = zonesFor(track, options);
      if (disc && this.layers.radii) {
        const [sx, sy] = this.worldToScreen(disc.centre[0], disc.centre[1]);
        const radius = disc.radius * this.camera.ppm;
        if (radius >= 0.5) {
          combined.addPath(this._circlePath(sx, sy, radius));
          outlines.push({ kind: 'circle', sx, sy, radius });
        }
      }
      if (corridor) {
        const screen = corridor.map(([x, y]) => this.worldToScreen(x, y));
        combined.addPath(this._polygonPath(screen));
        outlines.push({ kind: 'polygon', screen });
      }
    }

    ctx.save();
    ctx.fillStyle = 'rgba(226, 69, 63, 0.16)';
    ctx.fill(combined, 'nonzero');

    ctx.globalAlpha = 0.16;
    ctx.fillStyle = this._hatchPattern(ctx);
    ctx.fill(combined, 'nonzero');
    ctx.restore();

    ctx.save();
    ctx.strokeStyle = 'rgba(226, 69, 63, 0.62)';
    ctx.lineWidth = 1.1;
    for (const outline of outlines) {
      ctx.beginPath();
      if (outline.kind === 'circle') {
        ctx.setLineDash([]);
        ctx.arc(outline.sx, outline.sy, outline.radius, 0, Math.PI * 2);
      } else {
        ctx.setLineDash([5, 3]);
        outline.screen.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.restore();
  }

  _circlePath(x, y, radius) {
    const path = new Path2D();
    path.arc(x, y, radius, 0, Math.PI * 2);
    return path;
  }

  _polygonPath(points) {
    const path = new Path2D();
    points.forEach(([x, y], index) => (index ? path.lineTo(x, y) : path.moveTo(x, y)));
    path.closePath();
    return path;
  }

  /**
   * Every lidar sweep the vessel sent, in one plot.
   *
   * Two sensors reach here by different routes and look different on purpose:
   *
   *   front  on the Jetson, fused with the two cameras there, so a return the
   *          lenses covered carries that buoy's own colour. Most of a rotation
   *          is behind both lenses and comes through uncoloured (`-1,-1,-1`) —
   *          that is the normal case, not a fault, and it draws in the layer's
   *          cyan.
   *   aft    on the Pi, facing astern. Nothing looks that way, so it never has
   *          colour at all. Drawn in a muted slate so "no camera sees back
   *          there" cannot be mistaken for "the cameras saw nothing here".
   *
   * A `boat`-frame sweep is `[starboard, forward]` metres from the hull and is
   * rotated onto the chart here, with the vessel's live pose, rather than on
   * the boat — a cloud converted a second ago swings behind the boat through
   * every turn. It uses `boat.heading ?? [0, 1]`, the same fallback
   * `_drawBoat` uses for the vessel glyph, so the cloud and the hull it came
   * off can never end up drawn pointing different ways.
   */
  _drawScan(ctx, state) {
    const sweeps = [...(state.scans ?? []), state.scan].filter((s) => s?.points?.length);
    if (!sweeps.length) return;

    const boat = state.boat;
    const size = this.camera.ppm > 8 ? 2 : 1.5;

    for (const sweep of sweeps) {
      let place;
      if (sweep.frame === 'boat') {
        // No vessel position means no frame to hang these off. Skipping is the
        // honest answer: the chart has no boat on it either in that state.
        if (!boat?.position) continue;
        const [px, py] = boat.position;
        const [hx, hy] = boat.heading ?? [0, 1];
        // forward is the heading; starboard is the heading turned 90° right.
        place = (s, f) => this.worldToScreen(px + f * hx + s * hy, py + f * hy - s * hx);
      } else {
        place = (x, y) => this.worldToScreen(x, y);
      }

      const fallback =
        sweep.source === 'aft_lidar' ? 'rgba(143, 168, 200, 0.5)' : 'rgba(127, 212, 255, 0.5)';
      const rgb = sweep.rgb?.length === sweep.points.length * 3 ? sweep.rgb : null;
      let fill = null;

      sweep.points.forEach(([a, b], index) => {
        const [sx, sy] = place(a, b);
        if (sx < -8 || sy < -8 || sx > this.width + 8 || sy > this.height + 8) return;
        // A negative channel is the "no camera coloured this" sentinel, which is
        // deliberately not black — a dark buoy would be black.
        const r = rgb?.[index * 3] ?? -1;
        const next =
          r < 0 ? fallback : `rgba(${r}, ${rgb[index * 3 + 1]}, ${rgb[index * 3 + 2]}, 0.85)`;
        // Assigning fillStyle is the expensive part of this loop, so only do it
        // when the colour actually changes — an uncoloured sweep sets it once.
        if (next !== fill) {
          fill = next;
          ctx.fillStyle = next;
        }
        ctx.fillRect(sx - size / 2, sy - size / 2, size, size);
      });
    }
  }

  /**
   * Three kinds of line, deliberately kept apart:
   *
   *   reference  the ideal route, laid out from the course's GNSS points. Amber,
   *              long-dashed, with a ring on every waypoint. Bottom of the stack.
   *   candidate  a plan the planner considered and did not commit to. Grey.
   *   planned    what it is actually steering. Cyan, glowing, on top.
   *
   * Comparing the amber and the cyan is the required "COG with trail vs the ideal
   * route" read, so nothing may make them look alike.
   */
  _drawPaths(ctx, state) {
    const paths = state.paths ?? [];
    const rank = (path) => (REFERENCE_KINDS.has(path.kind) ? 0 : path.kind === 'planned' ? 2 : 1);
    const ordered = [...paths].sort((a, b) => rank(a) - rank(b));

    for (const path of ordered) {
      const points = path.points ?? [];

      // Checked before the length guard: a one-waypoint course is a real thing
      // (Task 3 starts 10 m from the dock), and it still has to be drawn.
      if (REFERENCE_KINDS.has(path.kind)) {
        if (this.layers.route && points.length) this._drawReferenceRoute(ctx, path);
        continue;
      }

      if (points.length < 2) continue;

      const primary = path.kind === 'planned';
      const screen = points.map(([x, y]) => this.worldToScreen(x, y));

      ctx.save();
      if (primary) {
        // A soft glow makes the committed path readable over satellite imagery.
        ctx.strokeStyle = 'rgba(86, 208, 255, 0.22)';
        ctx.lineWidth = 7;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.beginPath();
        screen.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
      }

      ctx.strokeStyle = primary ? PALETTE.path : PALETTE.pathAlt;
      ctx.lineWidth = primary ? 2.1 : 1.2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      if (!primary) ctx.setLineDash([6, 4]);
      ctx.beginPath();
      screen.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
      ctx.restore();

      if (!primary) continue;

      screen.forEach(([x, y], index) => {
        if (index === 0) return;
        const isTarget = index === path.target_index;
        ctx.beginPath();
        ctx.arc(x, y, isTarget ? 4.5 : 2.6, 0, Math.PI * 2);
        ctx.fillStyle = isTarget ? '#ffffff' : PALETTE.path;
        ctx.fill();
        if (isTarget) {
          ctx.strokeStyle = PALETTE.path;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(x, y, 8.5, 0, Math.PI * 2);
          ctx.stroke();
        }
      });
    }
  }

  /**
   * The course: one leg per waypoint, each coloured by the rule in force on it.
   *
   * A Njord plan is a list of places *plus what to do between them* — blind GNSS
   * on one leg, buoy rules on the next, COLREG on the one after, a dock at the
   * end. Drawing all of that in one amber line throws away the only thing about
   * a course worth checking before it is run, and hides the mistake that is
   * cheapest to make and most expensive to discover: a role typed into the wrong
   * row. So the **leg** carries the colour of the waypoint it runs to (the role
   * governs the approach, not the arrival), and the **marker** carries the
   * role's letter.
   *
   * The cursor comes from `telemetry.autopilot.plan.index` in preference to the
   * path's own `target_index`: the path is only republished when a plan is
   * uploaded, so its copy goes stale the moment the boat passes waypoint one,
   * while the telemetry cursor is live at 2 Hz and costs nothing extra.
   */
  _drawReferenceRoute(ctx, path) {
    const points = path.points ?? [];
    const screen = points.map(([x, y]) => this.worldToScreen(x, y));
    if (!screen.length) return;

    const roles = path.roles ?? null;
    const names = path.names ?? null;
    const plan = this.store.state.telemetry?.autopilot?.plan ?? null;
    const cursor = Number.isFinite(plan?.index) ? plan.index : path.target_index ?? -1;
    const finished = Boolean(plan?.finished);
    const colourAt = (index) => (roles ? styleOfRole(roles[index]).colour : PALETTE.route);

    ctx.save();
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    // Legs, drawn one at a time so each can carry its own role colour. Dashed
    // throughout, so the course can never be confused with the cyan path the
    // planner has actually committed to.
    ctx.setLineDash([10, 6]);
    ctx.lineWidth = 2;
    for (let index = 1; index < screen.length; index += 1) {
      const done = !finished && index <= cursor;
      ctx.globalAlpha = done ? 0.32 : 0.92;
      ctx.strokeStyle = colourAt(index);
      ctx.beginPath();
      ctx.moveTo(screen[index - 1][0], screen[index - 1][1]);
      ctx.lineTo(screen[index][0], screen[index][1]);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    const showText = this.layers.labels && this.camera.ppm > 1.2;
    screen.forEach(([x, y], index) => {
      const passed = !finished && index < cursor;
      const current = !finished && index === cursor;
      const style = roles ? styleOfRole(roles[index]) : null;
      const colour = colourAt(index);
      const size = current ? 8 : 6;

      ctx.globalAlpha = passed ? 0.4 : 1;

      // The waypoint the boat is heading for gets a halo, so "where is it going"
      // is answerable from across the tent.
      if (current) {
        ctx.beginPath();
        ctx.arc(x, y, size + 6, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255,255,255,0.85)';
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // A diamond, not a circle: obstacles are round, and a place the operator
      // chose must never look like a thing the boat found.
      ctx.beginPath();
      ctx.moveTo(x, y - size);
      ctx.lineTo(x + size, y);
      ctx.lineTo(x, y + size);
      ctx.lineTo(x - size, y);
      ctx.closePath();
      ctx.fillStyle = passed ? 'rgba(10, 17, 40, 0.55)' : colour;
      ctx.fill();
      ctx.strokeStyle = passed ? colour : 'rgba(10, 17, 40, 0.8)';
      ctx.lineWidth = 1.4;
      ctx.stroke();

      // The role's letter inside it. This is the whole point of the layer: a
      // glance says which legs obey the buoy rules and which are blind.
      if (style && size >= 6) {
        ctx.fillStyle = passed ? colour : 'rgba(10, 17, 40, 0.9)';
        ctx.font = `700 ${Math.round(size * 1.1)}px system-ui, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(style.code, x, y + 0.5);
      }

      if (!showText) return;
      const label = names?.[index] || String(index + 1);
      ctx.font = '600 9px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const width = ctx.measureText(label).width + 6;
      ctx.fillStyle = 'rgba(10, 17, 40, 0.7)';
      ctx.fillRect(x - width / 2, y - size - 15, width, 12);
      ctx.fillStyle = passed ? PALETTE.muted : '#ffffff';
      ctx.fillText(label, x, y - size - 9);
    });

    ctx.globalAlpha = 1;
    if (path.label && this.camera.ppm > 0.8) {
      const [lx, ly] = screen[Math.floor(screen.length / 2)];
      ctx.font = '600 10px system-ui, sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      const metrics = ctx.measureText(path.label);
      ctx.fillStyle = 'rgba(10, 17, 40, 0.66)';
      ctx.fillRect(lx + 10, ly - 7, metrics.width + 6, 14);
      ctx.fillStyle = PALETTE.route;
      ctx.fillText(path.label, lx + 13, ly);
    }
    ctx.restore();
  }

  /**
   * A course being laid, not yet sent.
   *
   * Deliberately unlike the layer above: solid line, square markers, and a white
   * outline on every one. This is a local draft that the vessel has never seen,
   * and the moment it starts looking like the route the boat is running is the
   * moment somebody engages autonomy on a course that was never uploaded.
   */
  _drawMissionDraft(ctx) {
    const screen = this.missionDraft.map((point) => this.worldToScreen(point.x, point.y));

    ctx.save();
    if (screen.length >= 2) {
      ctx.strokeStyle = PALETTE.draft;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      screen.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    screen.forEach(([x, y], index) => {
      const style = styleOfRole(this.missionDraft[index].role);
      ctx.fillStyle = style.colour;
      ctx.strokeStyle = PALETTE.draft;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.rect(x - 5, y - 5, 10, 10);
      ctx.fill();
      ctx.stroke();

      ctx.font = '700 9px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = 'rgba(10, 17, 40, 0.9)';
      ctx.fillText(style.code, x, y + 0.5);

      ctx.fillStyle = 'rgba(10, 17, 40, 0.7)';
      ctx.fillRect(x - 6, y - 20, 12, 11);
      ctx.fillStyle = PALETTE.draft;
      ctx.font = '600 9px system-ui, sans-serif';
      ctx.fillText(String(index + 1), x, y - 14.5);
    });
    ctx.restore();
  }

  /**
   * The parking space the vessel has found, and the dot in the middle of it.
   *
   * Everything drawn here is measured on the boat and arrives in world metres
   * (`telemetry.autopilot.parking`, from `behaviours/parking.py`), so this adds
   * only colour — no geometry is recomputed in the browser, because a second
   * implementation of the box arithmetic is a second thing that can disagree with
   * the boat about where the boat is going.
   *
   * Three things are drawn and they are deliberately distinguishable:
   *
   *   the three lines   thick and solid. These are *measurements* — the lidar
   *                     returns fitted to edges. If they do not lie on top of the
   *                     structure in the satellite imagery, the fix or the grid is
   *                     out and nothing else on this overlay means anything.
   *   the rectangle     thin and dashed, open at the mouth. This is *inference* —
   *                     the space the boat believes those three lines imply.
   *   the dot           white, ringed. Where the boat is actually going.
   *
   * Solid for measured and dashed for inferred is the same convention the scan and
   * the planned path already use, so a glance says which half to distrust.
   */
  _drawParking(ctx, state) {
    const parking = state.telemetry?.autopilot?.parking;
    if (!parking?.seen) return;

    const lines = Array.isArray(parking.lines) ? parking.lines : [];
    const corners = Array.isArray(parking.corners) ? parking.corners : [];
    const target = parking.target;

    // A space nobody has looked at for a while is drawn faded rather than
    // removed: during the hold the boat is inside it and sees the least of it,
    // and a rectangle that blinks out at the moment of the manoeuvre is worse
    // than one that says "this is remembered".
    const age = Number.isFinite(parking.age_s) ? parking.age_s : 0;
    const stale = age > 2;

    ctx.save();
    ctx.globalAlpha = stale ? 0.55 : 1;

    if (corners.length === 4) {
      const screen = corners.map(([x, y]) => this.worldToScreen(x, y));
      // The three closed sides, in order: mouth -> back -> back -> mouth.
      ctx.strokeStyle = PALETTE.parkFaint;
      ctx.lineWidth = 1.2;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      screen.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
      // The mouth, dotted much finer — it is the way in, not a wall, and it is the
      // one edge that is not there.
      ctx.setLineDash([2, 5]);
      ctx.beginPath();
      ctx.moveTo(screen[3][0], screen[3][1]);
      ctx.lineTo(screen[0][0], screen[0][1]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    ctx.strokeStyle = PALETTE.park;
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    for (const line of lines) {
      if (!Array.isArray(line) || line.length < 2) continue;
      const [ax, ay] = this.worldToScreen(line[0][0], line[0][1]);
      const [bx, by] = this.worldToScreen(line[1][0], line[1][1]);
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, by);
      ctx.stroke();
    }

    if (Array.isArray(target)) {
      const [tx, ty] = this.worldToScreen(target[0], target[1]);
      ctx.strokeStyle = PALETTE.parkDot;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.arc(tx, ty, 8, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(tx - 11, ty);
      ctx.lineTo(tx - 4, ty);
      ctx.moveTo(tx + 4, ty);
      ctx.lineTo(tx + 11, ty);
      ctx.moveTo(tx, ty - 11);
      ctx.lineTo(tx, ty - 4);
      ctx.moveTo(tx, ty + 4);
      ctx.lineTo(tx, ty + 11);
      ctx.stroke();
      ctx.fillStyle = PALETTE.parkDot;
      ctx.beginPath();
      ctx.arc(tx, ty, 2.6, 0, Math.PI * 2);
      ctx.fill();

      // The angle the hull has to sit at for the hold to count: square to the
      // closed end for a bow-in park, 90 degrees off it for an alongside one. Drawn
      // as a bar through the dot, because "is the boat on the spot" and "is the boat
      // at the right angle" are two separate questions and the second one cannot be
      // answered by eye without something to compare the hull against.
      if (Number.isFinite(parking.park_heading_deg) && this.camera.ppm > 3) {
        const grid = ((parking.park_heading_deg - (state.grid_bearing ?? 0)) * Math.PI) / 180;
        const reach = Math.max(16, 1.2 * this.camera.ppm);
        const dx = Math.sin(grid) * reach;
        const dy = -Math.cos(grid) * reach;
        const off = Number.isFinite(parking.heading_error_deg)
          ? parking.heading_error_deg
          : 0;
        ctx.save();
        // Green once the hull is inside the gate the countdown needs, amber while
        // it is still swinging. The threshold is the vessel's own
        // PARK_HOLD_ANGLE_DEG; 10 deg is duplicated here only as a display cue, and
        // the vessel remains the only thing that decides whether the hold counts.
        ctx.strokeStyle = off <= 10 ? PALETTE.cog : PALETTE.park;
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 3]);
        ctx.beginPath();
        ctx.moveTo(tx - dx, ty - dy);
        ctx.lineTo(tx + dx, ty + dy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
      }

      // How the dot was placed, close in only: the measured span of the space and
      // the static offset that moved the dot off its middle. This is the readback
      // the offset is tuned against, and it belongs next to the dot rather than
      // three panels away on the other page.
      if (this.camera.ppm > 6 && Number.isFinite(parking.dot_depth_m)) {
        const offset = Number.isFinite(parking.offset_m) ? parking.offset_m : 0;
        const label =
          `${parking.dot_depth_m.toFixed(2)} m in` +
          (offset ? ` (offset ${offset > 0 ? '+' : ''}${offset.toFixed(2)})` : '') +
          (parking.offset_clamped ? ' clamped' : '');
        ctx.font = '600 10px ui-monospace, monospace';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        const metrics = ctx.measureText(label);
        ctx.fillStyle = 'rgba(10, 17, 40, 0.68)';
        ctx.fillRect(tx + 12, ty - 7, metrics.width + 6, 14);
        ctx.fillStyle = parking.offset_clamped ? '#ffb0a8' : PALETTE.park;
        ctx.fillText(label, tx + 15, ty);
      }
    }
    ctx.restore();
  }

  _drawTracks(ctx, state) {
    const tracks = state.tracks ?? [];
    const showLabels = this.layers.labels && this.camera.ppm > 1.6;

    for (const track of tracks) {
      const [sx, sy] = this.worldToScreen(track.position[0], track.position[1]);
      if (sx < -60 || sy < -60 || sx > this.width + 60 || sy > this.height + 60) continue;

      const style = styleOf(track);
      const name = nameOf(track);
      const confidence = track.confidence ?? 0;
      const hovered = this.hovered?.track?.track_id === track.track_id;
      const size = Math.max(5, Math.min(11, 2.2 * this.camera.ppm * 0.5 + 4));

      // Confidence as an arc around the marker: a partial ring reads as "not
      // fully trusted" without needing the number.
      ctx.beginPath();
      ctx.arc(sx, sy, size + 3.5, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * confidence);
      ctx.strokeStyle = confidence < 0.5 ? 'rgba(239, 198, 61, 0.85)' : 'rgba(255,255,255,0.55)';
      ctx.lineWidth = 1.6;
      ctx.stroke();

      this._drawMarker(ctx, sx, sy, size, style, name, track);

      if (hovered) {
        ctx.beginPath();
        ctx.arc(sx, sy, size + 7, 0, Math.PI * 2);
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }

      if (showLabels || hovered) {
        // The vessel's own words win over ours when it sent any: it says
        // "cardinal (side unknown)" where a type number alone would say
        // "Cardinal", and the difference is the bit worth reading.
        const label = this.layers.ids
          ? `#${track.track_id} ${track.label ?? style.label}`
          : `#${track.track_id}`;
        ctx.font = '600 10px system-ui, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        const textX = sx + size + 7;
        const metrics = ctx.measureText(label);
        ctx.fillStyle = 'rgba(10, 17, 40, 0.62)';
        ctx.fillRect(textX - 2, sy - 7, metrics.width + 4, 14);
        ctx.fillStyle = PALETTE.ink;
        ctx.fillText(label, textX, sy);
      }
    }
  }

  _drawMarker(ctx, sx, sy, size, style, name, track) {
    ctx.save();
    ctx.fillStyle = style.colour;
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.lineWidth = 1.3;

    if (style.glyph === 'hull') {
      // Another vessel: draw it oriented so its bow direction is obvious.
      const heading = track.heading ?? track.velocity ?? [0, 1];
      const angle = Math.atan2(-heading[1], heading[0]);
      ctx.translate(sx, sy);
      ctx.rotate(angle);
      ctx.beginPath();
      ctx.moveTo(size * 1.5, 0);
      ctx.lineTo(-size * 0.7, size * 0.8);
      ctx.lineTo(-size * 0.4, 0);
      ctx.lineTo(-size * 0.7, -size * 0.8);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    } else if (style.glyph === 'block') {
      ctx.beginPath();
      ctx.rect(sx - size * 0.9, sy - size * 0.9, size * 1.8, size * 1.8);
      ctx.fill();
      ctx.stroke();
    } else if (style.glyph === 'target') {
      ctx.beginPath();
      ctx.arc(sx, sy, size, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(sx - size * 1.5, sy);
      ctx.lineTo(sx + size * 1.5, sy);
      ctx.moveTo(sx, sy - size * 1.5);
      ctx.lineTo(sx, sy + size * 1.5);
      ctx.strokeStyle = style.colour;
      ctx.stroke();
    } else if (style.glyph === 'ring') {
      ctx.beginPath();
      ctx.arc(sx, sy, size * 0.85, 0, Math.PI * 2);
      ctx.strokeStyle = style.colour;
      ctx.lineWidth = 2;
      ctx.stroke();
    } else {
      ctx.beginPath();
      ctx.arc(sx, sy, size, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }

    // Cardinal marks carry their letter: which side you must pass on is the
    // whole point of the mark, so spell it out. An unresolved one draws `?` —
    // the camera has seen black-and-yellow and has not yet committed to which of
    // the four it is, and that is a materially different thing to know than
    // "north cardinal", because it is the state in which the boat falls back to
    // the side the plan asked for.
    if (CARDINALS.has(name) && size >= 6) {
      ctx.fillStyle = '#1a1a1a';
      ctx.font = `700 ${Math.round(size * 1.15)}px system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(letterOf(track), sx, sy + 0.5);
    }
    ctx.restore();
  }

  _drawTrail(ctx) {
    if (this.trail.length < 2) return;
    const now = Date.now() / 1000;
    ctx.lineWidth = 1.8;
    ctx.lineCap = 'round';

    for (let index = 1; index < this.trail.length; index += 1) {
      const previous = this.trail[index - 1];
      const point = this.trail[index];
      const age = now - point.t;
      const alpha = Math.max(0, 0.55 * (1 - age / TRAIL_MAX_AGE));
      if (alpha <= 0.02) continue;
      const [x1, y1] = this.worldToScreen(previous.x, previous.y);
      const [x2, y2] = this.worldToScreen(point.x, point.y);
      ctx.strokeStyle = `rgba(127, 176, 255, ${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
  }

  _drawBoat(ctx, state) {
    const boat = state.boat;
    if (!boat?.position) return;

    const [sx, sy] = this.worldToScreen(boat.position[0], boat.position[1]);
    const heading = boat.heading ?? [0, 1];
    const angle = Math.atan2(-heading[1], heading[0]);
    const radius = boat.radius ?? 1.15;

    if (this.layers.radii && radius > 0) {
      ctx.beginPath();
      ctx.arc(sx, sy, radius * this.camera.ppm, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(255,255,255,0.3)';
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Course over ground as a long thin ray, drawn from the GNSS COG rather than
    // from the velocity vector so it is the same number the tiles show. When it
    // separates visibly from the bow, that gap *is* the crab angle — which is why
    // it runs much further out than the velocity arrow and in its own colour.
    if (this.layers.cog) {
      const course = this.store.courseDegrees;
      if (Number.isFinite(course)) {
        const rad = (course * Math.PI) / 180 - ((state.grid_bearing ?? 0) * Math.PI) / 180;
        const reach = Math.max(46, radius * 9 * this.camera.ppm);
        const ex = sx + Math.sin(rad) * reach;
        const ey = sy - Math.cos(rad) * reach;
        ctx.save();
        ctx.strokeStyle = PALETTE.cog;
        ctx.lineWidth = 1.3;
        ctx.setLineDash([7, 5]);
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(ex, ey);
        ctx.stroke();
        ctx.setLineDash([]);
        if (this.camera.ppm > 1.2) {
          ctx.font = '600 9px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = PALETTE.cog;
          ctx.fillText(`COG ${Math.round(course)}°`, ex, ey - 8);
        }
        ctx.restore();
      }
    }

    // Velocity vector: three seconds of travel at the current speed.
    const velocity = boat.velocity;
    if (Array.isArray(velocity)) {
      const speed = Math.hypot(velocity[0], velocity[1]);
      if (speed > 0.05) {
        const [ex, ey] = this.worldToScreen(
          boat.position[0] + velocity[0] * 3,
          boat.position[1] + velocity[1] * 3
        );
        ctx.strokeStyle = 'rgba(86, 208, 255, 0.85)';
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(ex, ey);
        ctx.stroke();
        const arrow = Math.atan2(ey - sy, ex - sx);
        ctx.beginPath();
        ctx.moveTo(ex, ey);
        ctx.lineTo(ex - Math.cos(arrow - 0.4) * 6, ey - Math.sin(arrow - 0.4) * 6);
        ctx.lineTo(ex - Math.cos(arrow + 0.4) * 6, ey - Math.sin(arrow + 0.4) * 6);
        ctx.closePath();
        ctx.fillStyle = 'rgba(86, 208, 255, 0.85)';
        ctx.fill();
      }
    }

    // Trimaran silhouette, floored to a legible size when zoomed out.
    const length = Math.max(13, radius * 2 * this.camera.ppm);
    const beam = length * 0.62;

    ctx.save();
    ctx.translate(sx, sy);
    ctx.rotate(angle);

    ctx.fillStyle = 'rgba(255,255,255,0.22)';
    for (const side of [-1, 1]) {
      ctx.beginPath();
      ctx.ellipse(-length * 0.05, side * beam * 0.5, length * 0.34, beam * 0.11, 0, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.beginPath();
    ctx.moveTo(length * 0.62, 0);
    ctx.lineTo(-length * 0.3, beam * 0.2);
    ctx.lineTo(-length * 0.38, 0);
    ctx.lineTo(-length * 0.3, -beam * 0.2);
    ctx.closePath();
    ctx.fillStyle = PALETTE.boat;
    ctx.fill();
    ctx.strokeStyle = 'rgba(14, 24, 52, 0.85)';
    ctx.lineWidth = 1.2;
    ctx.stroke();
    ctx.restore();

    if (state.estop) {
      ctx.beginPath();
      ctx.arc(sx, sy, length * 0.9, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(226, 69, 63, 0.9)';
      ctx.lineWidth = 2.4;
      ctx.stroke();
    }

    this._drawHoldTimer(ctx, state, sx, sy, length);
  }

  /**
   * The hold countdown, beside the boat.
   *
   * Next to the hull rather than in a panel, because the question it answers is
   * "can I look away yet" and the thing being watched is the boat. It follows the
   * boat around the chart and it is the only number drawn on the vessel itself.
   *
   * The seconds come from the vessel already counted down
   * (`telemetry.autopilot.hold_remaining_s`) rather than being run off a local
   * clock. That is the difference between a timer and a *readback*: a browser
   * counting its own seconds keeps counting when the link drops, and would sit
   * there ticking confidently towards zero for a boat that stopped reporting ten
   * seconds ago. This one goes stale and stops, which is the honest failure.
   *
   * Nothing here is parking-specific. Any behaviour that publishes
   * `hold_remaining_s` gets the same widget.
   */
  _drawHoldTimer(ctx, state, sx, sy, length) {
    const autopilot = state.telemetry?.autopilot;
    const remaining = autopilot?.hold_remaining_s;
    if (!Number.isFinite(remaining)) return;

    const required = Number.isFinite(autopilot?.hold_required_s)
      ? autopilot.hold_required_s
      : null;
    const seconds = `${remaining.toFixed(1)} s`;
    const caption = required ? `holding ${required.toFixed(0)} s` : 'holding';
    const restarts = autopilot?.hold_restarts;
    const warn = Number.isFinite(restarts) && restarts > 0;

    ctx.save();
    ctx.font = '700 15px ui-monospace, monospace';
    const width = Math.max(ctx.measureText(seconds).width, 66) + 16;
    const height = 34;
    // Screen-right of the hull, flipping to the left near the edge so the number
    // is never the thing that falls off the canvas.
    const gap = Math.max(14, length * 0.75);
    let x = sx + gap;
    if (x + width > this.width - 6) x = sx - gap - width;
    const y = sy - height / 2;

    ctx.fillStyle = 'rgba(10, 17, 40, 0.82)';
    ctx.strokeStyle = warn ? 'rgba(239, 198, 61, 0.95)' : PALETTE.park;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.roundRect?.(x, y, width, height, 6);
    if (!ctx.roundRect) ctx.rect(x, y, width, height);
    ctx.fill();
    ctx.stroke();

    // A bar across the bottom for how much of the hold is left, so the number does
    // not have to be read to see that it is going down.
    if (required > 0) {
      const done = Math.max(0, Math.min(1, 1 - remaining / required));
      ctx.fillStyle = 'rgba(255, 194, 31, 0.9)';
      ctx.fillRect(x + 1, y + height - 3.5, (width - 2) * done, 2.5);
    }

    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = PALETTE.ink;
    ctx.fillText(seconds, x + 8, y + 4);
    ctx.font = '600 9px system-ui, sans-serif';
    ctx.fillStyle = warn ? '#efc63d' : PALETTE.muted;
    ctx.fillText(warn ? `restarted ${restarts}x` : caption, x + 8, y + 21);
    ctx.restore();
  }

  _drawHud(ctx, state) {
    // Scale bar: pick a round distance that lands between 60 and 150 px.
    const target = 110 / this.camera.ppm;
    const magnitude = 10 ** Math.floor(Math.log10(target));
    const metres = [1, 2, 5, 10].map((m) => m * magnitude).find((m) => m >= target) ?? magnitude * 10;
    const pixels = metres * this.camera.ppm;

    const x = this.width - pixels - 16;
    const y = this.height - 34;
    ctx.strokeStyle = 'rgba(255,255,255,0.75)';
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(x, y - 4);
    ctx.lineTo(x, y);
    ctx.lineTo(x + pixels, y);
    ctx.lineTo(x + pixels, y - 4);
    ctx.stroke();
    ctx.fillStyle = PALETTE.ink;
    ctx.font = '600 10px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(metres >= 1000 ? `${metres / 1000} km` : `${metres} m`, x + pixels / 2, y - 5);

    // North arrow, rotated if the grid is not aligned to true north.
    const bearing = ((state.grid_bearing ?? 0) * Math.PI) / 180;
    const nx = this.width - 26;
    const ny = 30;
    ctx.save();
    ctx.translate(nx, ny);
    ctx.rotate(-bearing);
    ctx.beginPath();
    ctx.moveTo(0, -13);
    ctx.lineTo(4.5, 5);
    ctx.lineTo(0, 2);
    ctx.lineTo(-4.5, 5);
    ctx.closePath();
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.fill();
    ctx.font = '700 9px system-ui, sans-serif';
    ctx.fillStyle = PALETTE.muted;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText('N', 0, 7);
    ctx.restore();

    // Upstream direction: the reference that decides which side of a lateral
    // mark is the wrong side, so it belongs on screen.
    const upstream = state.upstream_direction;
    if (Array.isArray(upstream) && Math.hypot(upstream[0], upstream[1]) > 0.1) {
      const ux = 26;
      const uy = this.height - 74;
      const angle = Math.atan2(-upstream[1], upstream[0]);
      ctx.save();
      ctx.translate(ux, uy);
      ctx.rotate(angle);
      ctx.strokeStyle = 'rgba(157, 184, 232, 0.8)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(-11, 0);
      ctx.lineTo(9, 0);
      ctx.moveTo(9, 0);
      ctx.lineTo(4, -3.5);
      ctx.moveTo(9, 0);
      ctx.lineTo(4, 3.5);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = PALETTE.faint;
      ctx.font = '600 9px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText('upstream', ux, uy + 8);
    }

    if (this.store.linkLevel !== 'live' && state.boat) {
      ctx.fillStyle = 'rgba(10, 17, 40, 0.45)';
      ctx.fillRect(0, 0, this.width, this.height);
      ctx.fillStyle = 'rgba(255, 255, 255, 0.82)';
      ctx.font = '700 13px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(
        this.store.linkLevel === 'stale' ? 'TELEMETRY STALE' : 'NO TELEMETRY',
        this.width / 2,
        this.height / 2
      );
    }
  }
}
