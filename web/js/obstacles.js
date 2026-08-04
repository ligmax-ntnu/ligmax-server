/* Obstacle type presentation.
 *
 * The numeric values come from the server (`/api/session` reflects
 * ObstacleType out of shared_settings.py), so renumbering the Python enum
 * cannot desynchronise the colours here. Only the *names* are hard-coded.
 */

export const STYLES = {
  UNKNOWN: { colour: '#8b98ae', label: 'Unknown', glyph: 'ring', order: 8 },
  RED: { colour: '#e2453f', label: 'Red lateral', glyph: 'can', order: 0 },
  GREEN: { colour: '#22a06b', label: 'Green lateral', glyph: 'cone', order: 1 },
  NORTH: { colour: '#efc63d', label: 'North cardinal', glyph: 'letter', order: 2 },
  SOUTH: { colour: '#efc63d', label: 'South cardinal', glyph: 'letter', order: 3 },
  WEST: { colour: '#efc63d', label: 'West cardinal', glyph: 'letter', order: 4 },
  EAST: { colour: '#efc63d', label: 'East cardinal', glyph: 'letter', order: 5 },
  BOAT: { colour: '#f08a24', label: 'Vessel', glyph: 'hull', order: 6 },
  LAND: { colour: '#a9764b', label: 'Land', glyph: 'block', order: 7 },
  DOCKING_CENTER: { colour: '#9a6ce0', label: 'Dock centre', glyph: 'target', order: 9 },
};

const FALLBACK = { colour: '#8b98ae', label: 'Unknown', glyph: 'ring', order: 99 };

let valueToName = new Map();

/** Called once the server has told us the enum's numeric values. */
export function setTypeTable(table) {
  valueToName = new Map(Object.entries(table || {}).map(([name, value]) => [value, name]));
}

export function nameOf(track) {
  if (track && typeof track.type_name === 'string') return track.type_name;
  const value = track && typeof track === 'object' ? track.type : track;
  return valueToName.get(value) ?? `TYPE_${value}`;
}

export function styleOf(track) {
  return STYLES[nameOf(track)] ?? FALLBACK;
}

export function labelOf(track) {
  return styleOf(track).label;
}

export const CARDINALS = new Set(['NORTH', 'SOUTH', 'EAST', 'WEST']);
export const LATERALS = new Set(['RED', 'GREEN']);

/** Legend entries, ordered, limited to the types actually on screen. */
export function legendFor(tracks) {
  const counts = new Map();
  for (const track of tracks || []) {
    const name = nameOf(track);
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count, ...(STYLES[name] ?? FALLBACK) }))
    .sort((a, b) => a.order - b.order);
}
