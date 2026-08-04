/* The world model: obstacles, no-go zones, planned path and the vessel,
 * drawn in the boat's own metre grid.
 *
 * The view is always grid-aligned — +x right, +y up — because that is the
 * frame the autonomy code reasons in, and a debug view that silently rotates
 * is a debug view you cannot trust. When the grid is not aligned to north it
 * is the map imagery underneath that gets rotated, not the grid.
 */

import { CARDINALS, nameOf, styleOf } from './obstacles.js';
import { zonesFor } from './nogo.js';
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
  scan: '#7fd4ff',
  boat: '#ffffff',
  trail: '#7fb0ff',
};

const GRID_STEPS = [0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000];
const MIN_PPM = 0.15;
const MAX_PPM = 60;
const TRAIL_LIMIT = 1800;
const TRAIL_MAX_AGE = 300; // seconds

export const DEFAULT_LAYERS = {
  tiles: true,
  grid: true,
  nogo: true,
  radii: true,
  scan: true,
  paths: true,
  trail: true,
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
      if (this.pickMode && event.button === 0) {
        const [wx, wy] = this._pointerWorld(event);
        this.onPick?.([wx, wy]);
        return;
      }
      canvas.setPointerCapture(event.pointerId);
      this._pan = { x: event.clientX, y: event.clientY };
      canvas.classList.add('is-panning');
    });

    canvas.addEventListener('pointermove', (event) => {
      const rect = canvas.getBoundingClientRect();
      this.pointer = [event.clientX - rect.left, event.clientY - rect.top];

      if (this._pan) {
        const dx = event.clientX - this._pan.x;
        const dy = event.clientY - this._pan.y;
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
      if (!this._pan) return;
      this._pan = null;
      canvas.classList.remove('is-panning');
      canvas.releasePointerCapture?.(event.pointerId);
    };
    canvas.addEventListener('pointerup', endPan);
    canvas.addEventListener('pointercancel', endPan);

    canvas.addEventListener('pointerleave', () => {
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

  _updateHover() {
    if (!this.pointer) return;
    const [px, py] = this.pointer;
    let best = null;
    let bestDistance = 16;

    for (const track of this.store.state.tracks ?? []) {
      const [sx, sy] = this.worldToScreen(track.position[0], track.position[1]);
      const distance = Math.hypot(sx - px, sy - py);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = { track, screen: [sx, sy] };
      }
    }

    const changed = best?.track?.track_id !== this.hovered?.track?.track_id;
    this.hovered = best;
    if (changed) this.onHover?.(best);
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
    this._drawTracks(ctx, state);
    if (this.layers.trail) this._drawTrail(ctx);
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

  _drawScan(ctx, state) {
    const points = state.scan?.points;
    if (!points?.length) return;
    ctx.fillStyle = 'rgba(127, 212, 255, 0.5)';
    const size = this.camera.ppm > 8 ? 2 : 1.5;
    for (const [x, y] of points) {
      const [sx, sy] = this.worldToScreen(x, y);
      if (sx < -8 || sy < -8 || sx > this.width + 8 || sy > this.height + 8) continue;
      ctx.fillRect(sx - size / 2, sy - size / 2, size, size);
    }
  }

  _drawPaths(ctx, state) {
    const paths = state.paths ?? [];
    // Draw candidates first so the committed path sits on top of them.
    const ordered = [...paths].sort((a, b) => (a.kind === 'planned' ? 1 : -1));

    for (const path of ordered) {
      const points = path.points ?? [];
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
        const label = this.layers.ids
          ? `#${track.track_id} ${style.label}`
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
    // whole point of the mark, so spell it out.
    if (CARDINALS.has(name) && size >= 6) {
      ctx.fillStyle = '#1a1a1a';
      ctx.font = `700 ${Math.round(size * 1.15)}px system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(name[0], sx, sy + 0.5);
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
