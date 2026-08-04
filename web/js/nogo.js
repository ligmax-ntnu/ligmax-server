/* Where the vessel may not drive.
 *
 * This mirrors `wrong_side_direction()` in ligmax_gui/protocol.py — the rules
 * are duplicated so the map can draw without a round trip. If the planner
 * disagrees with what you see here, the planner is right and one of the two
 * needs fixing; send an explicit `no_go` on the track to settle it:
 *
 *     {"track_id": 7, ..., "no_go": {"dir": [1, 0], "length": 20}}
 *     {"track_id": 8, ..., "no_go": {"polygon": [[x, y], ...]}}
 */

import { CARDINALS, LATERALS, nameOf } from './obstacles.js';

function unit(vector) {
  if (!Array.isArray(vector) || vector.length < 2) return null;
  const [x, y] = vector;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const norm = Math.hypot(x, y);
  if (norm < 1e-9) return null;
  return [x / norm, y / norm];
}

/**
 * Unit vector the forbidden corridor extends along, or null if the obstacle
 * only carries an avoid radius.
 */
export function wrongSideDirection(track, upstreamDirection) {
  const name = nameOf(track);

  if (LATERALS.has(name)) {
    const [ux, uy] = unit(upstreamDirection) ?? [0, 1];
    // Rotate upstream +90° to get the port side going upstream. A red mark is
    // kept to port, so the water to *its* port side is out of bounds; green is
    // the mirror image.
    const port = [-uy, ux];
    return name === 'RED' ? port : [-port[0], -port[1]];
  }

  if (CARDINALS.has(name)) {
    // A cardinal mark names the side you pass it on, so the opposite side is
    // forbidden.
    if (name === 'NORTH') return [0, -1];
    if (name === 'SOUTH') return [0, 1];
    if (name === 'EAST') return [-1, 0];
    return [1, 0]; // WEST
  }

  if (name === 'BOAT') {
    // Don't cross ahead of another vessel.
    return unit(track.heading) ?? unit(track.velocity);
  }

  return null;
}

/**
 * The corridor as a polygon in grid metres: a stadium/capsule of half-width
 * `avoid_radius` running `length` metres from the object along `direction`.
 * Combined with the avoid disc this is the full swept no-go region.
 */
export function corridorPolygon(track, direction, length, { arcSteps = 10 } = {}) {
  const radius = Math.max(track.avoid_radius ?? 0, 0.35);
  const [dx, dy] = direction;
  const [px, py] = [-dy, dx]; // perpendicular
  const [ox, oy] = track.position;
  const [ex, ey] = [ox + dx * length, oy + dy * length];

  const points = [[ox + px * radius, oy + py * radius], [ex + px * radius, ey + py * radius]];
  // Round the far end so the shape reads as a swept capsule, not a box.
  const startAngle = Math.atan2(py, px);
  for (let step = 1; step < arcSteps; step += 1) {
    const angle = startAngle - (Math.PI * step) / arcSteps;
    points.push([ex + Math.cos(angle) * radius, ey + Math.sin(angle) * radius]);
  }
  points.push([ex - px * radius, ey - py * radius], [ox - px * radius, oy - py * radius]);
  return points;
}

/**
 * Everything undrivable because of one track.
 * Returns `{ disc, corridor }` in grid metres; either may be null.
 */
export function zonesFor(track, { upstreamDirection, wrongSideLength }) {
  const radius = track.avoid_radius ?? 0;
  const disc = radius > 0 ? { centre: track.position, radius } : null;

  const override = track.no_go;
  if (override?.polygon?.length >= 3) {
    return { disc, corridor: override.polygon, explicit: true };
  }

  const direction = unit(override?.dir) ?? wrongSideDirection(track, upstreamDirection);
  if (!direction) return { disc, corridor: null, explicit: false };

  const length = Number.isFinite(override?.length) ? override.length : wrongSideLength;
  if (!(length > 0)) return { disc, corridor: null, explicit: false };

  return {
    disc,
    corridor: corridorPolygon(track, direction, length),
    direction,
    length,
    explicit: Boolean(override?.dir || override?.length),
  };
}

/** True if a grid point falls inside any no-go region. Used for the readout. */
export function pointBlocked(point, tracks, options) {
  const [x, y] = point;
  for (const track of tracks || []) {
    const { disc, corridor } = zonesFor(track, options);
    if (disc && Math.hypot(x - disc.centre[0], y - disc.centre[1]) <= disc.radius) {
      return track;
    }
    if (corridor && pointInPolygon(point, corridor)) return track;
  }
  return null;
}

export function pointInPolygon([x, y], polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}
