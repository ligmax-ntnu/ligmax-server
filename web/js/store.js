/* Client-side mirror of the vessel state, plus rolling history for sparklines.
 *
 * The server hands over ~3 minutes of history when a tab connects, so charts
 * are populated immediately instead of drawing themselves in from empty.
 */

const HISTORY_LIMIT = 260;
const HISTORY_INTERVAL = 1.0; // seconds
const LOG_LIMIT = 3000;

/** `{battery: {soc: 1}}` -> `{'battery.soc': 1}`, numbers and booleans only. */
export function flattenNumeric(value, prefix = '', out = {}) {
  if (typeof value === 'boolean') {
    out[prefix] = value ? 1 : 0;
  } else if (typeof value === 'number' && Number.isFinite(value)) {
    out[prefix] = value;
  } else if (Array.isArray(value)) {
    value.forEach((item, index) => {
      if (typeof item === 'number' || typeof item === 'boolean') {
        flattenNumeric(item, `${prefix}[${index}]`, out);
      }
    });
  } else if (value && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) {
      flattenNumeric(item, prefix ? `${prefix}.${key}` : key, out);
    }
  }
  return out;
}

export function pluck(object, path) {
  return String(path)
    .split('.')
    .reduce((node, key) => (node && typeof node === 'object' ? node[key] : undefined), object);
}

export class Store {
  constructor() {
    this.session = { admin: false, admin_possible: false, commands: {}, wrong_side_length: 20 };
    this.state = {
      mode: null,
      estop: false,
      available_modes: [],
      origin: null,
      grid_bearing: 0,
      upstream_direction: [0, 1],
      boat: null,
      tracks: [],
      paths: [],
      scan: null,
      telemetry: {},
    };
    this.stats = { connected: false, hz: 0, frames: 0 };
    this.logs = [];
    this.commands = [];
    this.streamState = 'connecting'; // connecting | open | retrying
    this.lastFrameAt = null;

    this._history = new Map();
    this._lastSampleAt = 0;
    this._listeners = new Map();
  }

  on(event, handler) {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(handler);
    return () => this._listeners.get(event)?.delete(handler);
  }

  emit(event, payload) {
    for (const handler of this._listeners.get(event) ?? []) {
      try {
        handler(payload, this);
      } catch (error) {
        console.error(`listener for "${event}" failed`, error);
      }
    }
  }

  // -- ingest ------------------------------------------------------------

  applySnapshot(snapshot) {
    this.state = { ...this.state, ...(snapshot.state ?? {}) };
    this.stats = snapshot.stats ?? this.stats;
    this.logs = (snapshot.logs ?? []).slice(-LOG_LIMIT);
    this.commands = snapshot.commands ?? [];

    this._history.clear();
    for (const sample of snapshot.history ?? []) {
      for (const [path, value] of Object.entries(sample.v ?? {})) {
        this._push(path, sample.t, value);
      }
    }
    this._lastSampleAt = snapshot.history?.at(-1)?.t ?? 0;

    this.emit('snapshot', snapshot);
    this.emit('state', this.state);
    this.emit('stats', this.stats);
    this.emit('logs', this.logs);
    this.emit('commands', this.commands);
  }

  applyState(state) {
    this.state = { ...this.state, ...state };
    this.lastFrameAt = Date.now() / 1000;
    this._sample();
    this.emit('state', this.state);
  }

  applyStats(stats) {
    this.stats = stats;
    this.emit('stats', stats);
  }

  applyLogs(entries) {
    if (!entries?.length) return;
    this.logs.push(...entries);
    if (this.logs.length > LOG_LIMIT) this.logs.splice(0, this.logs.length - LOG_LIMIT);
    this.emit('logs', entries);
  }

  applyCommands(commands) {
    this.commands = commands ?? [];
    this.emit('commands', this.commands);
  }

  setStreamState(streamState) {
    if (this.streamState === streamState) return;
    this.streamState = streamState;
    this.emit('link', streamState);
  }

  // -- history -----------------------------------------------------------

  _push(path, t, value) {
    let series = this._history.get(path);
    if (!series) {
      series = [];
      this._history.set(path, series);
    }
    series.push({ t, v: value });
    if (series.length > HISTORY_LIMIT) series.splice(0, series.length - HISTORY_LIMIT);
  }

  _sample() {
    const now = Date.now() / 1000;
    if (now - this._lastSampleAt < HISTORY_INTERVAL) return;
    this._lastSampleAt = now;

    const flat = flattenNumeric(this.state.telemetry ?? {});
    flat['derived.speed'] = this.speed ?? 0;
    flat['derived.track_count'] = this.state.tracks?.length ?? 0;
    flat['derived.link_hz'] = this.stats?.hz ?? 0;
    for (const [path, value] of Object.entries(flat)) this._push(path, now, value);
  }

  /** Rolling samples for a dotted telemetry path, oldest first. */
  history(path) {
    return this._history.get(path) ?? [];
  }

  telemetry(path) {
    return pluck(this.state.telemetry ?? {}, path);
  }

  // -- derived -----------------------------------------------------------

  get speed() {
    const explicit = this.telemetry('motion.speed');
    if (Number.isFinite(explicit)) return explicit;
    const velocity = this.state.boat?.velocity;
    return Array.isArray(velocity) ? Math.hypot(velocity[0], velocity[1]) : null;
  }

  get headingDegrees() {
    const explicit = this.telemetry('motion.heading_deg');
    if (Number.isFinite(explicit)) return explicit;
    const heading = this.state.boat?.heading;
    if (!Array.isArray(heading)) return null;
    const grid = (Math.atan2(heading[0], heading[1]) * 180) / Math.PI;
    return (grid + (this.state.grid_bearing ?? 0) + 360) % 360;
  }

  /** live | stale | offline — drives the header pill and the map dimming. */
  get linkLevel() {
    if (this.streamState !== 'open') return 'offline';
    const age = this.stats?.last_frame_age;
    if (!this.stats?.last_frame_at) return 'offline';
    if (!Number.isFinite(age)) return 'offline';
    if (age > 8) return 'offline';
    if (age > 2.5) return 'stale';
    return 'live';
  }
}
