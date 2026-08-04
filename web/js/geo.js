/* Grid metres <-> WGS84 <-> Web Mercator.
 *
 * The vessel works in a local metre grid whose origin is the GPS fix it was
 * booted at (`Boat.original_gps_position`). To lay real map imagery under
 * that grid we treat the neighbourhood as a tangent plane, which is accurate
 * to well under a metre over a course a few hundred metres across.
 *
 * `gridBearing` is the compass bearing of the grid's +y axis, so a grid that
 * is not aligned to true north still lands on the map correctly.
 */

const EARTH_CIRCUMFERENCE = 40075016.686;
const METRES_PER_DEGREE_LAT = 111320;
const TILE_SIZE = 256;

/** Grid metres -> local east/north metres. */
export function gridToEnu(x, y, gridBearingDeg = 0) {
  const bearing = (gridBearingDeg * Math.PI) / 180;
  const cos = Math.cos(bearing);
  const sin = Math.sin(bearing);
  // +y points along the bearing; +x is that rotated 90° clockwise, which
  // reduces to the plain east/north pair when the bearing is zero.
  return [x * cos + y * sin, -x * sin + y * cos];
}

export function enuToGrid(east, north, gridBearingDeg = 0) {
  const bearing = (gridBearingDeg * Math.PI) / 180;
  const cos = Math.cos(bearing);
  const sin = Math.sin(bearing);
  return [east * cos - north * sin, east * sin + north * cos];
}

export function gridToLatLon(x, y, origin, gridBearingDeg = 0) {
  const [east, north] = gridToEnu(x, y, gridBearingDeg);
  const lat = origin.lat + north / METRES_PER_DEGREE_LAT;
  const lon =
    origin.lon +
    east / (METRES_PER_DEGREE_LAT * Math.cos((origin.lat * Math.PI) / 180));
  return { lat, lon };
}

export function latLonToGrid(lat, lon, origin, gridBearingDeg = 0) {
  const north = (lat - origin.lat) * METRES_PER_DEGREE_LAT;
  const east =
    (lon - origin.lon) *
    METRES_PER_DEGREE_LAT *
    Math.cos((origin.lat * Math.PI) / 180);
  return enuToGrid(east, north, gridBearingDeg);
}

/** WGS84 -> pixel coordinates in the Web Mercator pyramid at `zoom`. */
export function latLonToMercatorPx(lat, lon, zoom) {
  const scale = TILE_SIZE * 2 ** zoom;
  const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const sin = Math.sin((clamped * Math.PI) / 180);
  return [
    ((lon + 180) / 360) * scale,
    (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale,
  ];
}

/** Ground resolution of one Mercator pixel, in metres. */
export function metresPerPixel(lat, zoom) {
  return (EARTH_CIRCUMFERENCE * Math.cos((lat * Math.PI) / 180)) / (TILE_SIZE * 2 ** zoom);
}

/** Tile zoom whose native resolution best matches `pixelsPerMetre` on screen. */
export function bestZoom(lat, pixelsPerMetre, { min = 1, max = 19 } = {}) {
  const ideal = Math.log2(
    (EARTH_CIRCUMFERENCE * Math.cos((lat * Math.PI) / 180) * pixelsPerMetre) / TILE_SIZE
  );
  return Math.max(min, Math.min(max, Math.round(ideal)));
}

/**
 * Affine transform taking Mercator pixels (at `zoom`) to canvas pixels.
 *
 * Both grid->screen and grid->Mercator are affine over a small area, so the
 * composition is too. Rather than deriving it symbolically we sample three
 * grid points and solve, which keeps this correct even as the camera or the
 * grid rotation changes.
 *
 * Returns `{a, b, c, d, e, f}` ready for `ctx.setTransform`, or null if the
 * sample points are degenerate.
 */
export function mercatorToCanvasTransform({ origin, gridBearing = 0, zoom, worldToScreen }) {
  const samples = [
    [0, 0],
    [100, 0],
    [0, 100],
  ].map(([x, y]) => {
    const { lat, lon } = gridToLatLon(x, y, origin, gridBearing);
    return {
      mercator: latLonToMercatorPx(lat, lon, zoom),
      screen: worldToScreen(x, y),
    };
  });

  const [p0, p1, p2] = samples;
  const m1 = [p1.mercator[0] - p0.mercator[0], p1.mercator[1] - p0.mercator[1]];
  const m2 = [p2.mercator[0] - p0.mercator[0], p2.mercator[1] - p0.mercator[1]];
  const s1 = [p1.screen[0] - p0.screen[0], p1.screen[1] - p0.screen[1]];
  const s2 = [p2.screen[0] - p0.screen[0], p2.screen[1] - p0.screen[1]];

  // Solve T * [m1 m2] = [s1 s2] for the 2x2 matrix T.
  const determinant = m1[0] * m2[1] - m2[0] * m1[1];
  if (!Number.isFinite(determinant) || Math.abs(determinant) < 1e-12) return null;

  const inv = [
    [m2[1] / determinant, -m2[0] / determinant],
    [-m1[1] / determinant, m1[0] / determinant],
  ];
  const a = s1[0] * inv[0][0] + s2[0] * inv[1][0];
  const c = s1[0] * inv[0][1] + s2[0] * inv[1][1];
  const b = s1[1] * inv[0][0] + s2[1] * inv[1][0];
  const d = s1[1] * inv[0][1] + s2[1] * inv[1][1];

  return {
    a,
    b,
    c,
    d,
    e: p0.screen[0] - (a * p0.mercator[0] + c * p0.mercator[1]),
    f: p0.screen[1] - (b * p0.mercator[0] + d * p0.mercator[1]),
  };
}

export function invertTransform({ a, b, c, d, e, f }) {
  const determinant = a * d - b * c;
  if (Math.abs(determinant) < 1e-12) return null;
  return {
    a: d / determinant,
    b: -b / determinant,
    c: -c / determinant,
    d: a / determinant,
    e: (c * f - d * e) / determinant,
    f: (b * e - a * f) / determinant,
  };
}

export function applyTransform({ a, b, c, d, e, f }, x, y) {
  return [a * x + c * y + e, b * x + d * y + f];
}

export { TILE_SIZE };
