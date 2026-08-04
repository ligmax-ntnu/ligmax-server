/* Slippy-map underlay, georeferenced from the grid origin.
 *
 * Optional by design: if there is no internet on the competition network the
 * tiles simply never arrive and the map falls back to the navy panel. Nothing
 * else on the dashboard depends on this.
 */

import {
  TILE_SIZE,
  applyTransform,
  bestZoom,
  invertTransform,
  mercatorToCanvasTransform,
} from './geo.js';

/* `maxZoom` is the deepest level the service actually serves imagery for, not
 * the deepest it will answer on. Ask Esri for z19 over Trondheim and you get a
 * 200 response containing a grey "Map data not yet available" placeholder, so
 * these caps are set from what the providers really have at Njord latitudes
 * (Esri: z18 in Trondheim, z19 in Oslo; Kartverket WMTS: z18 then HTTP 400).
 * Past the cap we keep drawing the deepest real tiles, upscaled — soft, but
 * still correctly positioned, and the vector layer carries the detail anyway.
 */
export const PROVIDERS = {
  none: { label: 'No underlay', attribution: '', url: null },
  satellite: {
    label: 'Satellite',
    attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
    maxZoom: 18,
    url: (z, x, y) =>
      `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`,
  },
  nautical: {
    label: 'Nautical chart',
    attribution: '© Kartverket (sjøkart)',
    maxZoom: 18,
    dim: 0.55,
    url: (z, x, y) =>
      `https://cache.kartverket.no/v1/wmts/1.0.0/sjokartraster/default/webmercator/${z}/${y}/${x}.png`,
  },
  topo: {
    label: 'Norway topo',
    attribution: '© Kartverket',
    maxZoom: 18,
    dim: 0.55,
    url: (z, x, y) =>
      `https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/${z}/${y}/${x}.png`,
  },
  streets: {
    label: 'Streets',
    attribution: '© OpenStreetMap contributors',
    maxZoom: 19,
    dim: 0.5,
    url: (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`,
  },
};

const CACHE_LIMIT = 320;
const MAX_TILES_PER_FRAME = 240;

export class TileLayer {
  /** @param {() => void} onRepaint called when a tile finishes loading */
  constructor(onRepaint) {
    this.onRepaint = onRepaint;
    this.provider = 'satellite';
    // Navy wash over the underlay so the vector layer stays readable. Pale
    // chart and street tiles need more of it than dark satellite imagery.
    this.dim = PROVIDERS.satellite.dim ?? 0.42;
    this.cache = new Map(); // key -> {image, state: 'loading'|'ready'|'failed'}
    this.inflight = 0;
    this.failures = 0;
  }

  setProvider(name) {
    if (this.provider === name) return;
    this.provider = PROVIDERS[name] ? name : 'none';
    this.dim = PROVIDERS[this.provider]?.dim ?? 0.42;
    this.failures = 0;
  }

  get attribution() {
    return PROVIDERS[this.provider]?.attribution ?? '';
  }

  get enabled() {
    return this.provider !== 'none' && Boolean(PROVIDERS[this.provider]?.url);
  }

  _tile(zoom, x, y) {
    const key = `${this.provider}/${zoom}/${x}/${y}`;
    const hit = this.cache.get(key);
    if (hit) {
      // Refresh LRU position.
      this.cache.delete(key);
      this.cache.set(key, hit);
      return hit;
    }

    const entry = { image: new Image(), state: 'loading' };
    // Deliberately not setting crossOrigin: we only ever drawImage these, so
    // a tainted canvas costs us nothing and more tile hosts will serve us.
    entry.image.decoding = 'async';
    entry.image.referrerPolicy = 'no-referrer';
    entry.image.onload = () => {
      entry.state = 'ready';
      this.inflight -= 1;
      this.onRepaint?.();
    };
    entry.image.onerror = () => {
      entry.state = 'failed';
      this.inflight -= 1;
      this.failures += 1;
    };
    entry.image.src = PROVIDERS[this.provider].url(zoom, x, y);
    this.inflight += 1;

    this.cache.set(key, entry);
    while (this.cache.size > CACHE_LIMIT) {
      const oldest = this.cache.keys().next().value;
      this.cache.delete(oldest);
    }
    return entry;
  }

  /**
   * Paint the underlay. `worldToScreen` maps grid metres to CSS pixels;
   * `pixelsPerMetre` picks the tile zoom level; `ratio` is the device pixel
   * ratio the caller has scaled the context by.
   *
   * All the geometry below works in CSS pixels, and `ratio` is folded into the
   * matrix only at the setTransform call — because setTransform *replaces* the
   * context transform rather than multiplying into it, so the caller's HiDPI
   * scale has to be reapplied here or the tiles land at the wrong size.
   */
  draw(ctx, { origin, gridBearing = 0, worldToScreen, pixelsPerMetre, width, height, ratio = 1 }) {
    if (!this.enabled || !origin) return false;

    const provider = PROVIDERS[this.provider];
    const zoom = bestZoom(origin.lat, pixelsPerMetre, { max: provider.maxZoom ?? 19 });
    const transform = mercatorToCanvasTransform({ origin, gridBearing, zoom, worldToScreen });
    if (!transform) return false;
    const inverse = invertTransform(transform);
    if (!inverse) return false;

    // Which Mercator pixels are on screen? Check all four canvas corners so a
    // rotated grid is still covered.
    const corners = [[0, 0], [width, 0], [0, height], [width, height]].map(([x, y]) =>
      applyTransform(inverse, x, y)
    );
    const xs = corners.map((p) => p[0]);
    const ys = corners.map((p) => p[1]);

    const span = 2 ** zoom;
    const minX = Math.floor(Math.min(...xs) / TILE_SIZE);
    const maxX = Math.floor(Math.max(...xs) / TILE_SIZE);
    const minY = Math.max(0, Math.floor(Math.min(...ys) / TILE_SIZE));
    const maxY = Math.min(span - 1, Math.floor(Math.max(...ys) / TILE_SIZE));

    if ((maxX - minX + 1) * (maxY - minY + 1) > MAX_TILES_PER_FRAME) return false;

    ctx.save();
    ctx.setTransform(
      transform.a * ratio,
      transform.b * ratio,
      transform.c * ratio,
      transform.d * ratio,
      transform.e * ratio,
      transform.f * ratio
    );
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';

    let painted = 0;
    for (let tileX = minX; tileX <= maxX; tileX += 1) {
      for (let tileY = minY; tileY <= maxY; tileY += 1) {
        // Wrap longitude so panning across the antimeridian still works.
        const wrappedX = ((tileX % span) + span) % span;
        const tile = this._tile(zoom, wrappedX, tileY);
        if (tile.state !== 'ready') continue;
        // Slight overdraw: kills the hairline seams that float error leaves.
        const size = TILE_SIZE * 1.003;
        ctx.drawImage(tile.image, tileX * TILE_SIZE, tileY * TILE_SIZE, size, size);
        painted += 1;
      }
    }
    ctx.restore();

    if (painted > 0 && this.dim > 0) {
      ctx.save();
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.fillStyle = `rgba(10, 17, 40, ${this.dim})`;
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    }
    return painted > 0;
  }
}
