/* KPI strip and telemetry panels.
 *
 * Known fields get a proper label, unit and warning thresholds from the table
 * below. Anything the vessel sends that is *not* in the table still renders —
 * with a humanised label and automatic formatting — so adding a field to the
 * boat's telemetry dict makes it appear here with no frontend change. That is
 * deliberate: during debugging you want to see the new number immediately.
 */

import * as fmt from './format.js';
import { drawCompass, drawLevelBubble, drawSparkline } from './sparkline.js';
import { resolve as resolveStatus } from './status.js';
import { flattenNumeric } from './store.js';

const BELOW = 'below';
const ABOVE = 'above';

/** kind: number | percent | fraction | bool | text | angle */
const FIELDS = {
  'battery.soc': { label: 'State of charge', kind: 'fraction', unit: '%', spark: true, bar: true, warn: 0.35, danger: 0.15, direction: BELOW },
  'battery.remaining_wh': { label: 'Energy left', unit: 'Wh', digits: 0, spark: true, warn: 300, danger: 120, direction: BELOW },
  'battery.voltage': { label: 'Pack voltage', unit: 'V', digits: 2, spark: true, warn: 43, danger: 40, direction: BELOW },
  'battery.current': { label: 'Current', unit: 'A', digits: 1, spark: true },
  'battery.power': { label: 'Draw', unit: 'W', digits: 0, spark: true },
  'battery.consumed_wh': { label: 'Consumed', unit: 'Wh', digits: 0 },
  'battery.capacity_ah': { label: 'Pack capacity', unit: 'Ah', digits: 2 },
  'battery.cell_min': { label: 'Cell min', unit: 'V', digits: 3, warn: 3.5, danger: 3.3, direction: BELOW },
  'battery.cell_max': { label: 'Cell max', unit: 'V', digits: 3, warn: 4.15, danger: 4.22, direction: ABOVE },
  'battery.cell_delta': { label: 'Cell spread', unit: 'V', digits: 3, warn: 0.06, danger: 0.12, direction: ABOVE },
  'battery.temperature': { label: 'Pack temp', unit: '°C', digits: 1, spark: true, warn: 45, danger: 55, direction: ABOVE },
  'battery.cycles': { label: 'Cycles', digits: 0 },
  'battery.bms_ok': { label: 'BMS', kind: 'bool', goodWhen: true },
  'battery.charge_fet': { label: 'Charge FET', kind: 'bool' },
  'battery.discharge_fet': { label: 'Discharge FET', kind: 'bool', goodWhen: true },
  // Which sensor these numbers came from. The Njord requirement is that the
  // battery figures are the pack's own, read off the Daly BMS over CAN - not the
  // autopilot's estimate - so it is worth showing which one is answering.
  'battery.source': { label: 'Read from', kind: 'text', goodValues: ['daly_bms'], warnValues: ['pixhawk'] },
  'battery.age': { label: 'Reading age', unit: 's', digits: 1, warn: 4, danger: 10, direction: ABOVE },

  'power.propulsion_w': { label: 'Propulsion', unit: 'W', digits: 0, spark: true },
  'power.compute_w': { label: 'Compute', unit: 'W', digits: 0 },
  'power.actuators_w': { label: 'Actuators', unit: 'W', digits: 0 },
  'power.total_w': { label: 'Total', unit: 'W', digits: 0, spark: true },

  // SOG and COG are the GNSS receiver's own figures, which is what the Njord
  // requirement asks for. `speed` is the fused estimate the planner steers on,
  // and heading is where the bow points - the pair of angles is the interesting
  // part, because heading minus COG is the crab angle the current is imposing.
  'motion.sog': { label: 'Speed over ground', unit: 'm/s', digits: 2, spark: true },
  'motion.cog_deg': { label: 'Course over ground', unit: '°', digits: 0 },
  'motion.heading_deg': { label: 'Heading', unit: '°', digits: 0 },
  'motion.crab_deg': { label: 'Crab (hdg − COG)', unit: '°', digits: 0, warn: 12, danger: 25, direction: ABOVE, absolute: true },
  'motion.speed': { label: 'Speed (fused)', unit: 'm/s', digits: 2, spark: true },
  'motion.yaw_rate': { label: 'Yaw rate', unit: '°/s', digits: 1, spark: true },
  'motion.roll': { label: 'Roll', unit: '°', digits: 1, spark: true, warn: 8, danger: 14, direction: ABOVE, absolute: true },
  'motion.pitch': { label: 'Pitch', unit: '°', digits: 1, spark: true, warn: 8, danger: 14, direction: ABOVE, absolute: true },
  'motion.cross_track_error': { label: 'Off the ideal route', unit: 'm', digits: 2, spark: true, warn: 1.5, danger: 3, direction: ABOVE, absolute: true },
  'motion.distance_to_target': { label: 'To next waypoint', unit: 'm', digits: 1, spark: true },
  'motion.bearing_to_target': { label: 'Waypoint bearing', unit: '°', digits: 0 },

  'gimbal.pitch': { label: 'Residual pitch', unit: '°', digits: 2, spark: true, warn: 1.5, danger: 3, direction: ABOVE, absolute: true },
  'gimbal.roll': { label: 'Residual roll', unit: '°', digits: 2, spark: true, warn: 1.5, danger: 3, direction: ABOVE, absolute: true },
  'gimbal.target_pitch': { label: 'Target pitch', unit: '°', digits: 2 },
  'gimbal.target_roll': { label: 'Target roll', unit: '°', digits: 2 },
  'gimbal.motor_temp': { label: 'Motor temp', unit: '°C', digits: 1, warn: 55, danger: 70, direction: ABOVE },
  'gimbal.locked': { label: 'Locked', kind: 'bool', goodWhen: true },
  'gimbal.correction_hz': { label: 'Loop rate', unit: 'Hz', digits: 0, spark: true, warn: 120, danger: 60, direction: BELOW },

  'thrusters.port_pct': { label: 'Port', unit: '%', digits: 0, bar: true, spark: true },
  'thrusters.starboard_pct': { label: 'Starboard', unit: '%', digits: 0, bar: true, spark: true },
  'thrusters.port_temp': { label: 'Port temp', unit: '°C', digits: 1, warn: 60, danger: 75, direction: ABOVE },
  'thrusters.starboard_temp': { label: 'Stbd temp', unit: '°C', digits: 1, warn: 60, danger: 75, direction: ABOVE },

  // Pitch trim: where the 1.8 kWh pack is sitting on its rails. `rail_source`
  // says whether that is measured or merely commanded - the slider ESP32 has no
  // link back to the Pi, so a number here is the demand it was given unless
  // something new is reporting position (docs/hardware.md).
  'trim.battery_rail_mm': { label: 'Battery slider', unit: 'mm', digits: 0, spark: true, bar: false },
  'trim.battery_rail_pct': { label: 'Slider travel', unit: '%', digits: 0, bar: true },
  'trim.rail_source': { label: 'Slider figure is', kind: 'text', goodValues: ['measured'], warnValues: ['commanded'] },
  'trim.rail_homing': { label: 'Slider homing', kind: 'bool', goodWhen: false },
  // Roll trim: the amas. `amas.lua` on the flight controller writes two servo
  // outputs, anti-symmetric for roll and common-mode for ride height, and the
  // translator ESP32 turns those pulses into H-bridge drive.
  'trim.ama_port_us': { label: 'Ama port demand', unit: 'µs', digits: 0, spark: true },
  'trim.ama_starboard_us': { label: 'Ama stbd demand', unit: 'µs', digits: 0, spark: true },
  'trim.ama_roll_us': { label: 'Roll correction', unit: 'µs', digits: 0, spark: true, warn: 350, danger: 480, direction: ABOVE, absolute: true },
  'trim.ama_height_us': { label: 'Ride height', unit: 'µs', digits: 0, spark: true },
  'trim.ama_doing': { label: 'Amas are', kind: 'text' },
  // Both outputs saturate at 1000/2000 µs. A full-travel height command uses all
  // of that and leaves no roll authority (docs/findings.md item 10), which looks
  // like the roll loop having failed, so it gets its own flag.
  'trim.ama_saturated': { label: 'Ama output maxed', kind: 'bool', goodWhen: false },
  'trim.outrigger_port_mm': { label: 'Outrigger port', unit: 'mm', digits: 0, spark: true },
  'trim.outrigger_starboard_mm': { label: 'Outrigger stbd', unit: 'mm', digits: 0, spark: true },

  'gps.lat': { label: 'Latitude', digits: 6, wide: true },
  'gps.lon': { label: 'Longitude', digits: 6, wide: true },
  'gps.fix': { label: 'Fix', kind: 'text', goodValues: ['RTK_FIXED', 'RTK', 'FIXED'], warnValues: ['3D', 'RTK_FLOAT', 'DGPS'] },
  'gps.satellites': { label: 'Satellites', digits: 0, warn: 8, danger: 5, direction: BELOW },
  'gps.hdop': { label: 'HDOP', digits: 2, warn: 2, danger: 4, direction: ABOVE },
  'gps.altitude': { label: 'Altitude', unit: 'm', digits: 1 },

  // What the hull is showing, reported back by the node that drives the lights
  // ESP32. This is here so a mismatch between the status and the actual colour is
  // visible rather than something only a person on the pontoon can see.
  'lights.colour': { label: 'Showing', kind: 'text' },
  'lights.mode': { label: 'ESP32 mode', digits: 0 },
  'lights.for_status': { label: 'Set for', kind: 'text' },
  'lights.link': { label: 'Lights link', kind: 'bool', goodWhen: true },
  'lights.acks': { label: 'Acks', digits: 0 },

  'autonomy.planner': { label: 'Planner', kind: 'text' },
  'autonomy.replans': { label: 'Replans', digits: 0 },
  'autonomy.waypoint': { label: 'Waypoint', digits: 0 },
  'autonomy.tracks_fused': { label: 'Tracks fused', digits: 0 },
  'autonomy.loop_hz': { label: 'Loop rate', unit: 'Hz', digits: 1, spark: true, warn: 12, danger: 6, direction: BELOW },
  'autonomy.armed': { label: 'Armed', kind: 'bool', goodWhen: true },

  'system.cpu_pct': { label: 'Pi CPU', unit: '%', digits: 0, bar: true, spark: true, warn: 85, danger: 95, direction: ABOVE },
  'system.jetson_pct': { label: 'Jetson', unit: '%', digits: 0, bar: true, spark: true, warn: 90, danger: 97, direction: ABOVE },
  'system.cpu_temp': { label: 'CPU temp', unit: '°C', digits: 1, spark: true, warn: 72, danger: 82, direction: ABOVE },
  'system.ram_pct': { label: 'RAM', unit: '%', digits: 0, bar: true },
  'system.disk_pct': { label: 'Disk', unit: '%', digits: 0, bar: true, warn: 85, danger: 95, direction: ABOVE },
  'system.uptime_s': { label: 'Uptime', kind: 'duration' },
  'system.link_rtt_ms': { label: 'Link RTT', unit: 'ms', digits: 0, spark: true, warn: 120, danger: 300, direction: ABOVE },

  'bilge.pump_1': { label: 'Pump 1', kind: 'bool', goodWhen: false },
  'bilge.pump_2': { label: 'Pump 2', kind: 'bool', goodWhen: false },
  'bilge.pump_3': { label: 'Pump 3', kind: 'bool', goodWhen: false },
  'bilge.water_detected': { label: 'Water', kind: 'bool', goodWhen: false, critical: true },
};

const GROUPS = [
  {
    key: 'battery',
    eyebrow: 'Energy',
    title: 'Battery & BMS',
    hero: 'battery.soc',
    // The Wh figure beside the percentage, because "38 %" of an unknown pack is
    // not a number you can plan a run around.
    heroSide: (map) => {
      const wh = map.get('battery.remaining_wh');
      return Number.isFinite(wh) ? `${fmt.fixed(wh, 0)} Wh left` : '';
    },
  },
  { key: 'power', eyebrow: 'Energy', title: 'Power budget' },
  {
    key: 'motion',
    eyebrow: 'Dynamics',
    title: 'Motion & attitude',
    compass: true,
    bubble: { roll: 'motion.roll', pitch: 'motion.pitch', limit: 15, label: 'hull' },
  },
  { key: 'gps', eyebrow: 'Navigation', title: 'GNSS position' },
  { key: 'gimbal', eyebrow: 'Perception', title: 'Lidar gimbal', bubble: { roll: 'gimbal.roll', pitch: 'gimbal.pitch', limit: 3, label: 'residual' } },
  { key: 'thrusters', eyebrow: 'Propulsion', title: 'Thrusters' },
  { key: 'trim', eyebrow: 'Stabilisation', title: 'Active trim — slider & amas' },
  { key: 'lights', eyebrow: 'Signalling', title: 'Navigation lights' },
  { key: 'autonomy', eyebrow: 'Autonomy', title: 'Planner' },
  { key: 'system', eyebrow: 'Compute', title: 'System health' },
  { key: 'bilge', eyebrow: 'Safety', title: 'Bilge & hull' },
];

const SPARK_INTERVAL = 450; // ms; text updates every frame, charts less often

function levelFor(spec, value) {
  if (spec.kind === 'bool') {
    if (typeof value !== 'boolean' && typeof value !== 'number') return null;
    const truthy = Boolean(value);
    if (spec.goodWhen === undefined) return null;
    if (truthy === spec.goodWhen) return 'ok';
    return spec.critical ? 'danger' : 'warn';
  }
  if (spec.kind === 'text') {
    if (typeof value !== 'string') return null;
    if (spec.goodValues?.includes(value)) return 'ok';
    if (spec.warnValues?.includes(value)) return 'warn';
    if (spec.goodValues) return 'danger';
    return null;
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (spec.warn === undefined) return null;

  const compare = spec.absolute ? Math.abs(value) : value;
  if (spec.direction === BELOW) {
    if (spec.danger !== undefined && compare <= spec.danger) return 'danger';
    if (compare <= spec.warn) return 'warn';
  } else {
    if (spec.danger !== undefined && compare >= spec.danger) return 'danger';
    if (compare >= spec.warn) return 'warn';
  }
  return 'ok';
}

function formatValue(spec, value) {
  if (value === null || value === undefined) return { text: '—', unit: '' };
  if (spec.kind === 'bool') {
    return { text: value ? 'yes' : 'no', unit: '' };
  }
  if (spec.kind === 'text') return { text: String(value), unit: '' };
  if (spec.kind === 'duration') return { text: fmt.duration(value), unit: '' };
  if (spec.kind === 'fraction') return { text: fmt.percent(value, spec.digits ?? 0), unit: '%' };
  if (typeof value !== 'number') return { text: String(value), unit: spec.unit ?? '' };
  const text = spec.digits === undefined ? fmt.auto(value) : value.toFixed(spec.digits);
  return { text, unit: spec.unit ?? '' };
}

/** 0–1 for the bar widget, from either a fraction or a percentage field. */
function barFraction(spec, value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (spec.kind === 'fraction') return Math.max(0, Math.min(1, value));
  if (spec.unit === '%') return Math.max(0, Math.min(1, value / 100));
  return null;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

class MetricView {
  constructor(path, spec) {
    this.path = path;
    this.spec = spec;
    this.root = element('div', 'metric');
    if (spec.wide) this.root.classList.add('metric--wide');
    this.root.append(element('span', 'metric-label', spec.label));
    this.valueNode = element('span', 'metric-value');
    this.textNode = document.createTextNode('—');
    this.unitNode = element('span', 'unit');
    this.valueNode.append(this.textNode, this.unitNode);
    this.root.append(this.valueNode);

    if (spec.bar) {
      this.bar = element('div', 'bar');
      this.barFill = element('div', 'bar-fill');
      this.bar.append(this.barFill);
      this.root.append(this.bar);
    }
    if (spec.spark) {
      this.spark = element('canvas', 'metric-spark');
      this.root.append(this.spark);
    }
  }

  update(value, store, drawSparks) {
    const { text, unit } = formatValue(this.spec, value);
    if (this.textNode.nodeValue !== text) this.textNode.nodeValue = text;
    if (this.unitNode.textContent !== unit) this.unitNode.textContent = unit;

    const level = levelFor(this.spec, value);
    if (level) this.root.dataset.level = level;
    else delete this.root.dataset.level;

    if (this.bar) {
      const fraction = barFraction(this.spec, value);
      this.barFill.style.width = `${((fraction ?? 0) * 100).toFixed(1)}%`;
      if (level) this.barFill.dataset.level = level;
    }

    if (this.spark && drawSparks) {
      const samples = store.history(this.path);
      const colour = level === 'danger' ? '#c62b32' : level === 'warn' ? '#b7791f' : '#4f7fce';
      drawSparkline(this.spark, samples, { colour, zeroFloor: this.spec.kind === 'fraction' });
    }
  }
}

class GroupPanel {
  constructor(group) {
    this.group = group;
    this.metrics = new Map();
    this.root = element('section', 'card panel');

    const head = element('div', 'panel-head');
    const heading = element('div');
    heading.append(element('span', 'eyebrow', group.eyebrow ?? 'Telemetry'));
    heading.append(element('h3', 'panel-title', group.title));
    head.append(heading);
    this.flag = element('span', 'panel-flag');
    this.flag.hidden = true;
    head.append(this.flag);
    this.root.append(head);

    if (group.hero) {
      this.heroWrap = element('div', 'hero-metric');
      this.heroValue = element('span', 'hero-value');
      this.heroText = document.createTextNode('—');
      this.heroUnit = element('span', 'unit');
      this.heroValue.append(this.heroText, this.heroUnit);
      this.heroSide = element('span', 'hero-side');
      this.heroWrap.append(this.heroValue, this.heroSide);
      this.root.append(this.heroWrap);

      this.heroBar = element('div', 'bar hero-bar');
      this.heroBarFill = element('div', 'bar-fill');
      this.heroBar.append(this.heroBarFill);
      this.root.append(this.heroBar);
    }

    if (group.compass || group.bubble) {
      const row = element('div', 'attitude-row');
      if (group.compass) {
        this.compass = element('canvas', 'bubble');
        row.append(this.compass);
      }
      if (group.bubble) {
        this.bubble = element('canvas', 'bubble');
        row.append(this.bubble);
      }
      this.root.append(row);
    }

    this.grid = element('div', 'metric-grid');
    this.root.append(this.grid);
  }

  /** Add views for any field that has appeared since the last update. */
  sync(values) {
    for (const [path, value] of values) {
      if (this.metrics.has(path)) continue;
      if (path === this.group.hero) continue;
      const spec = FIELDS[path] ?? inferSpec(path, value);
      const view = new MetricView(path, spec);
      this.metrics.set(path, view);
      this.grid.append(view.root);
    }
  }

  update(values, store, drawSparks) {
    const map = new Map(values);
    this.sync(values);

    let worst = null;
    for (const [path, view] of this.metrics) {
      const value = map.get(path);
      view.update(value, store, drawSparks);
      const level = view.root.dataset.level;
      if (level === 'danger') worst = 'danger';
      else if (level === 'warn' && worst !== 'danger') worst = 'warn';
    }

    if (this.group.hero) {
      const value = map.get(this.group.hero);
      const spec = FIELDS[this.group.hero] ?? {};
      const { text, unit } = formatValue(spec, value);
      this.heroText.nodeValue = text;
      this.heroUnit.textContent = unit;
      const level = levelFor(spec, value);
      const fraction = barFraction(spec, value);
      this.heroBarFill.style.width = `${((fraction ?? 0) * 100).toFixed(1)}%`;
      if (level) {
        this.heroBarFill.dataset.level = level;
        if (level === 'danger') worst = 'danger';
        else if (level === 'warn' && worst !== 'danger') worst = 'warn';
      }
      this.heroSide.textContent = this.group.heroSide?.(map) ?? '';
    }

    if (worst) {
      this.flag.hidden = false;
      this.flag.dataset.level = worst;
      this.flag.textContent = worst === 'danger' ? 'Attention' : 'Watch';
    } else {
      this.flag.hidden = true;
    }

    if (drawSparks && this.compass) {
      // Heading is the solid arrow, COG the dashed needle. When they separate,
      // the gap is the set the boat is fighting - which is the whole reason the
      // compass draws two things instead of one.
      drawCompass(this.compass, {
        heading: map.get('motion.heading_deg') ?? 0,
        course: map.get('motion.cog_deg') ?? null,
      });
    }
    if (drawSparks && this.bubble) {
      drawLevelBubble(this.bubble, {
        roll: map.get(this.group.bubble.roll) ?? 0,
        pitch: map.get(this.group.bubble.pitch) ?? 0,
        limit: this.group.bubble.limit,
        label: this.group.bubble.label,
      });
    }
  }
}

/** Best-effort spec for a field the dashboard has never seen before. */
function inferSpec(path, value) {
  const leaf = path.split('.').pop() ?? path;
  const spec = { label: fmt.humanise(leaf) };
  if (typeof value === 'boolean') spec.kind = 'bool';
  else if (typeof value === 'string') spec.kind = 'text';

  // Guess a unit from the suffix, which is how most telemetry is named.
  const units = { _w: 'W', _v: 'V', _a: 'A', _c: '°C', _pct: '%', _hz: 'Hz', _ms: 'ms',
    _mm: 'mm', _m: 'm', _wh: 'Wh', _deg: '°', _s: 's' };
  for (const [suffix, unit] of Object.entries(units)) {
    if (leaf.endsWith(suffix)) {
      spec.unit = unit;
      spec.label = fmt.humanise(leaf.slice(0, -suffix.length)) || spec.label;
      break;
    }
  }
  if (/temp/i.test(leaf)) spec.unit ??= '°C';
  if (typeof value === 'number') spec.spark = true;
  return spec;
}

export class TelemetryPanels {
  constructor(container, store) {
    this.container = container;
    this.store = store;
    this.panels = new Map();
    this.lastSparkAt = 0;
  }

  update() {
    const telemetry = this.store.state.telemetry ?? {};
    const now = performance.now();
    const drawSparks = now - this.lastSparkAt > SPARK_INTERVAL;
    if (drawSparks) this.lastSparkAt = now;

    // Known groups first, in a deliberate order, then anything new the vessel
    // has started publishing.
    const known = new Set(GROUPS.map((group) => group.key));
    const extras = Object.keys(telemetry)
      .filter((key) => !known.has(key) && telemetry[key] && typeof telemetry[key] === 'object')
      .sort();
    const groups = [
      ...GROUPS,
      ...extras.map((key) => ({ key, eyebrow: 'Vessel', title: fmt.humanise(key) })),
    ];

    for (const group of groups) {
      const section = telemetry[group.key];
      const hasData = section !== undefined && section !== null;
      let panel = this.panels.get(group.key);

      if (!hasData) {
        if (panel) panel.root.hidden = true;
        continue;
      }
      if (!panel) {
        panel = new GroupPanel(group);
        this.panels.set(group.key, panel);
        this.container.append(panel.root);
      }
      panel.root.hidden = false;

      // Flatten to dotted paths so FIELDS lookups and history keys line up.
      const flat =
        typeof section === 'object'
          ? Object.entries(flattenNumeric(section, group.key))
          : [];
      // flattenNumeric drops strings, but `gps.fix` and friends matter.
      const withText = [...flat];
      if (typeof section === 'object' && !Array.isArray(section)) {
        for (const [key, value] of Object.entries(section)) {
          if (typeof value === 'string') withText.push([`${group.key}.${key}`, value]);
          if (typeof value === 'boolean') {
            const path = `${group.key}.${key}`;
            const index = withText.findIndex(([p]) => p === path);
            if (index >= 0) withText[index] = [path, value];
            else withText.push([path, value]);
          }
        }
      }
      withText.sort((a, b) => specOrder(a[0]) - specOrder(b[0]));
      panel.update(withText, this.store, drawSparks);
    }
  }
}

/** Keeps known fields in their declared order; unknown ones fall to the end. */
const FIELD_ORDER = new Map(Object.keys(FIELDS).map((path, index) => [path, index]));
function specOrder(path) {
  return FIELD_ORDER.get(path) ?? 10000;
}

/* --- KPI strip ------------------------------------------------------- */

const KPIS = [
  {
    // The required status indicator. `resolveStatus` is what makes this read
    // "Out of control" when the link has gone quiet rather than repeating the
    // last thing the boat managed to say.
    key: 'status',
    label: 'Status',
    value: (store) => resolveStatus(store).meta.label,
    sub: (store) => {
      const resolved = resolveStatus(store);
      if (resolved.stale && resolved.reason) return resolved.reason;
      return store.state.mode ? `autopilot: ${store.state.mode}` : resolved.meta.plain;
    },
    level: (store) => {
      const { level } = resolveStatus(store).meta;
      return level === 'idle' ? null : level;
    },
  },
  {
    key: 'mode',
    label: 'Autopilot mode',
    value: (store) => store.state.mode ?? '—',
    sub: (store) => store.state.status_text ?? (store.state.estop ? 'emergency stop' : ''),
    level: (store) => (store.state.estop ? 'danger' : store.state.mode ? 'ok' : null),
  },
  {
    // Speed over ground, from the GNSS receiver, falling back to the fused
    // estimate so the tile is not blank on a bench run with no fix.
    key: 'speed',
    label: 'Speed over ground',
    unit: 'm/s',
    value: (store) => fmt.fixed(store.sog, 2),
    sub: (store) => {
      const parts = [`${fmt.knots(store.sog ?? 0)} kn`];
      if (!Number.isFinite(store.telemetry('motion.sog'))) parts.push('fused, no GNSS');
      return parts.join(' · ');
    },
    spark: 'derived.speed',
  },
  {
    key: 'heading',
    label: 'Heading',
    unit: '°',
    value: (store) => {
      const heading = store.headingDegrees;
      return heading === null ? '—' : Math.round(heading);
    },
    sub: (store) => fmt.compassPoint(store.headingDegrees ?? NaN),
  },
  {
    key: 'cog',
    label: 'Course over ground',
    unit: '°',
    value: (store) => {
      const cog = store.courseDegrees;
      return cog === null ? '—' : Math.round(cog);
    },
    sub: (store) => {
      const crab = store.crabDegrees;
      if (!Number.isFinite(crab)) return fmt.compassPoint(store.courseDegrees ?? NaN);
      // Which way the boat is being pushed relative to where it points. This is
      // the number that explains an otherwise baffling cross-track error.
      const side = crab > 0 ? 'starboard' : 'port';
      return `${fmt.compassPoint(store.courseDegrees ?? NaN)} · ${Math.abs(Math.round(crab))}° to ${side}`;
    },
    level: (store) => levelFor(FIELDS['motion.crab_deg'], store.crabDegrees),
  },
  {
    key: 'position',
    label: 'Position',
    value: (store) => {
      const { lat, lon } = store.position;
      return Number.isFinite(lat) && Number.isFinite(lon) ? lat.toFixed(6) : '—';
    },
    sub: (store) => {
      const { lat, lon } = store.position;
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return 'no fix reported';
      const fix = store.telemetry('gps.fix');
      return [lon.toFixed(6), typeof fix === 'string' ? fix : null].filter(Boolean).join(' · ');
    },
    level: (store) => levelFor(FIELDS['gps.fix'], store.telemetry('gps.fix')),
  },
  {
    key: 'battery',
    label: 'Battery',
    unit: '%',
    value: (store) => fmt.percent(store.telemetry('battery.soc'), 0),
    sub: (store) => {
      const wh = store.telemetry('battery.remaining_wh');
      const power = store.telemetry('battery.power') ?? store.telemetry('power.total_w');
      const parts = [];
      if (Number.isFinite(wh)) parts.push(`${fmt.fixed(wh, 0)} Wh`);
      if (Number.isFinite(power)) parts.push(`${fmt.fixed(power, 0)} W`);
      // Flag it loudly if this is the autopilot's guess rather than the BMS.
      if (store.telemetry('battery.source') === 'pixhawk') parts.push('autopilot estimate');
      return parts.join(' · ');
    },
    spark: 'battery.soc',
    level: (store) => levelFor(FIELDS['battery.soc'], store.telemetry('battery.soc')),
  },
  {
    key: 'detections',
    label: 'Detections',
    value: (store) => store.state.tracks?.length ?? 0,
    sub: (store) => {
      const blocked = store.state.tracks?.filter((t) => (t.avoid_radius ?? 0) > 0).length ?? 0;
      return `${blocked} with avoid radius`;
    },
    spark: 'derived.track_count',
  },
  {
    key: 'target',
    label: 'To next waypoint',
    unit: 'm',
    value: (store) => fmt.fixed(store.distanceToWaypoint, 1),
    sub: (store) => {
      const parts = [];
      const waypoint = store.telemetry('autonomy.waypoint');
      if (Number.isFinite(waypoint)) parts.push(`wp ${waypoint}`);
      const error = store.telemetry('motion.cross_track_error');
      if (Number.isFinite(error)) parts.push(`${fmt.fixed(Math.abs(error), 2)} m off route`);
      return parts.join(' · ');
    },
    level: (store) =>
      levelFor(FIELDS['motion.cross_track_error'], store.telemetry('motion.cross_track_error')),
  },
  {
    key: 'gimbal',
    label: 'Gimbal residual',
    unit: '°',
    value: (store) => {
      const pitch = store.telemetry('gimbal.pitch');
      const roll = store.telemetry('gimbal.roll');
      if (!Number.isFinite(pitch) && !Number.isFinite(roll)) return '—';
      return fmt.fixed(Math.max(Math.abs(pitch ?? 0), Math.abs(roll ?? 0)), 2);
    },
    sub: () => 'worst axis',
    level: (store) => {
      const worst = Math.max(
        Math.abs(store.telemetry('gimbal.pitch') ?? 0),
        Math.abs(store.telemetry('gimbal.roll') ?? 0)
      );
      return worst > 3 ? 'danger' : worst > 1.5 ? 'warn' : 'ok';
    },
  },
  {
    key: 'link',
    label: 'Telemetry link',
    unit: 'Hz',
    value: (store) => fmt.fixed(store.stats?.hz, 1),
    sub: (store) => {
      const rtt = store.telemetry('system.link_rtt_ms');
      const age = store.stats?.last_frame_age;
      const parts = [];
      if (Number.isFinite(rtt)) parts.push(`${fmt.fixed(rtt, 0)} ms`);
      if (Number.isFinite(age)) parts.push(`${fmt.fixed(age, 1)} s old`);
      return parts.join(' · ') || (store.stats?.transport ?? '');
    },
    spark: 'derived.link_hz',
    level: (store) => {
      const link = store.linkLevel;
      return link === 'live' ? 'ok' : link === 'stale' ? 'warn' : 'danger';
    },
  },
];

export class KpiStrip {
  /**
   * `keys` picks a subset, in the order given — the overview page shows the four
   * figures a visitor can read without knowing the boat, the control page shows
   * the lot. `overrides` replaces fields of a tile spec (`label`, `sub`, …) so the
   * same tile can be plain-spoken in one place and precise in the other.
   */
  constructor(container, store, { keys = null, overrides = {} } = {}) {
    this.container = container;
    this.store = store;
    this.views = new Map();
    this.lastSparkAt = 0;

    const byKey = new Map(KPIS.map((kpi) => [kpi.key, kpi]));
    const chosen = keys
      ? keys.map((key) => byKey.get(key)).filter(Boolean)
      : KPIS;

    for (const base of chosen) {
      const kpi = { ...base, ...(overrides[base.key] ?? {}) };
      const root = element('div', 'kpi');
      root.append(element('span', 'kpi-label', kpi.label));
      const valueNode = element('span', 'kpi-value');
      const textNode = document.createTextNode('—');
      const unitNode = element('span', 'unit', kpi.unit ?? '');
      valueNode.append(textNode, unitNode);
      root.append(valueNode);
      const subNode = element('span', 'kpi-sub');
      root.append(subNode);

      let spark = null;
      if (kpi.spark) {
        spark = element('canvas', 'kpi-spark');
        root.append(spark);
      }
      this.views.set(kpi.key, { kpi, root, textNode, subNode, spark });
      container.append(root);
    }
  }

  update() {
    const now = performance.now();
    const drawSparks = now - this.lastSparkAt > SPARK_INTERVAL;
    if (drawSparks) this.lastSparkAt = now;

    for (const view of this.views.values()) {
      const { kpi } = view;
      const text = String(kpi.value(this.store));
      if (view.textNode.nodeValue !== text) view.textNode.nodeValue = text;
      const sub = kpi.sub?.(this.store) ?? '';
      if (view.subNode.textContent !== sub) view.subNode.textContent = sub;
      view.subNode.title = sub;

      const level = kpi.level?.(this.store);
      if (level) view.root.dataset.level = level;
      else delete view.root.dataset.level;

      if (view.spark && drawSparks) {
        const colour = level === 'danger' ? '#c62b32' : level === 'warn' ? '#b7791f' : '#4f7fce';
        drawSparkline(view.spark, this.store.history(kpi.spark), { colour, zeroFloor: true });
      }
    }
  }
}
