/* Number, time and label formatting. */

const KNOTS_PER_MS = 1.94384;

/** Pick a sensible precision from the magnitude, so tables stay aligned. */
export function auto(value, { maxDigits = 3 } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value !== 'number') return String(value);
  if (!Number.isFinite(value)) return value > 0 ? '∞' : '−∞';

  const magnitude = Math.abs(value);
  if (magnitude === 0) return '0';
  if (magnitude >= 10000) return Math.round(value).toLocaleString('en-GB');
  if (magnitude >= 100) return value.toFixed(0);
  if (magnitude >= 10) return value.toFixed(1);
  if (magnitude >= 1) return value.toFixed(Math.min(2, maxDigits));
  if (magnitude >= 0.01) return value.toFixed(Math.min(3, maxDigits));
  return value.toExponential(1);
}

export function fixed(value, digits = 1) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

export function percent(fraction, digits = 0) {
  if (typeof fraction !== 'number' || !Number.isFinite(fraction)) return '—';
  // Accept both 0–1 and 0–100 conventions; > 1.5 is almost certainly already %.
  const value = fraction <= 1.5 ? fraction * 100 : fraction;
  return value.toFixed(digits);
}

export function knots(metresPerSecond) {
  if (typeof metresPerSecond !== 'number') return '—';
  return (metresPerSecond * KNOTS_PER_MS).toFixed(1);
}

export function clockTime(unixSeconds, { millis = true } = {}) {
  if (typeof unixSeconds !== 'number' || !Number.isFinite(unixSeconds)) return '—';
  const date = new Date(unixSeconds * 1000);
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  const base = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return millis ? `${base}.${pad(date.getMilliseconds(), 3)}` : base;
}

export function duration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

export function ago(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return 'never';
  if (seconds < 1) return 'just now';
  return `${duration(seconds)} ago`;
}

/** `battery.cell_min` -> `Cell min`, `soc` -> `SoC`. */
const SPECIAL_LABELS = new Map(Object.entries({
  soc: 'SoC',
  hdop: 'HDOP',
  rtt: 'RTT',
  cpu: 'CPU',
  ram: 'RAM',
  gps: 'GPS',
  imu: 'IMU',
  bms: 'BMS',
  pct: '%',
  wh: 'Wh',
  w: 'W',
  hz: 'Hz',
  ms: 'ms',
  mm: 'mm',
  id: 'ID',
  xte: 'XTE',
}));

export function humanise(key) {
  const parts = String(key).split(/[._\s-]+/).filter(Boolean);
  const words = parts.map((part, index) => {
    const lower = part.toLowerCase();
    if (SPECIAL_LABELS.has(lower)) return SPECIAL_LABELS.get(lower);
    if (index === 0) return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
    return lower;
  });
  return words.join(' ');
}

export function bytes(count) {
  if (typeof count !== 'number' || !Number.isFinite(count)) return '—';
  const units = ['B', 'kB', 'MB', 'GB'];
  let value = count;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

/** Compass bearing from a grid-frame vector, honouring a rotated grid. */
export function bearingFromVector(vector, gridBearingDeg = 0) {
  if (!vector) return null;
  const [x, y] = vector;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const gridDegrees = (Math.atan2(x, y) * 180) / Math.PI;
  return (gridDegrees + gridBearingDeg + 360) % 360;
}

const COMPASS_POINTS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

export function compassPoint(degrees) {
  if (typeof degrees !== 'number' || !Number.isFinite(degrees)) return '—';
  return COMPASS_POINTS[Math.round((degrees % 360) / 22.5) % 16];
}

export function latLon(lat, lon) {
  if (typeof lat !== 'number' || typeof lon !== 'number') return '—';
  const hemisphere = (value, positive, negative) =>
    `${Math.abs(value).toFixed(6)}° ${value >= 0 ? positive : negative}`;
  return `${hemisphere(lat, 'N', 'S')}, ${hemisphere(lon, 'E', 'W')}`;
}
