/* The overview page: the map, four figures, and nothing you can break.
 *
 * The audience is someone who has never seen this boat — a judge, a sponsor, a
 * visitor. So this page carries only what such a person can read without being
 * told: where the boat is, what it has spotted, how fast it is going, how much
 * battery is left. Everything that needs context to interpret — every telemetry
 * field, the mode picker, arm/disarm, the log stream, the command audit, the repo
 * list — is on /control (see control.js).
 *
 * Admins additionally get the emergency stop and the go-to picker here, because
 * both belong next to the map: one is the thing you reach for in a hurry, the
 * other is a click on the chart.
 */

import { CommandPanel } from './commands.js';
import * as fmt from './format.js';
import { WorldMap } from './map.js';
import { legendFor, styleOf } from './obstacles.js';
import { wrongSideDirection } from './nogo.js';
import {
  $,
  bootShell,
  connectShellStream,
  notify,
  savePrefs,
  startHeartbeat,
  updateHeader,
} from './shell.js';
import { KpiStrip } from './telemetry.js';

/* The four figures a visitor can read unaided, in plain words. The precise
   versions of these — and the other five tiles — are on the control page. */
const BASIC_KPIS = ['mode', 'speed', 'battery', 'detections'];

const BASIC_LABELS = {
  mode: {
    label: 'Doing',
    sub: (store) =>
      store.state.estop
        ? 'stopped by the operator'
        : store.state.status_text ?? (store.state.mode ? 'under way' : 'waiting for the boat'),
  },
  speed: { label: 'Speed' },
  battery: { label: 'Battery' },
  detections: {
    label: 'Obstacles seen',
    sub: (store) =>
      (store.state.tracks?.length ?? 0) === 0
        ? 'nothing in view'
        : 'buoys and hazards the cameras found',
  },
};

const LAYER_CHIPS = [
  { key: 'grid', label: 'Metre grid' },
  { key: 'nogo', label: 'No-go zones' },
  { key: 'radii', label: 'Avoid radii' },
  { key: 'scan', label: 'Lidar returns' },
  { key: 'paths', label: 'Planned path' },
  { key: 'trail', label: 'Track history' },
  { key: 'labels', label: 'Track IDs' },
  { key: 'ids', label: 'Type names' },
];

/* --- legend ---------------------------------------------------------- */

function updateLegend(store) {
  const container = $('map-legend');
  const entries = legendFor(store.state.tracks);
  const signature = entries.map((entry) => `${entry.name}:${entry.count}`).join(',');
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;

  container.replaceChildren();
  if (!entries.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  for (const entry of entries) {
    const row = document.createElement('div');
    row.className = 'legend-row';
    const swatch = document.createElement('span');
    swatch.className = 'legend-swatch';
    swatch.style.background = entry.colour;
    const label = document.createElement('span');
    label.textContent = entry.label;
    const count = document.createElement('span');
    count.className = 'legend-count';
    count.textContent = entry.count;
    row.append(swatch, label, count);
    container.append(row);
  }
}

/* --- plain-language facts -------------------------------------------- */

/** A handful of rows in words rather than units. Skips whatever is unknown. */
function updatePlainFacts(store) {
  const list = $('plain-facts');
  if (!list) return;

  const rows = [];

  const lat = store.telemetry('gps.lat') ?? store.state.origin?.lat;
  const lon = store.telemetry('gps.lon') ?? store.state.origin?.lon;
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    rows.push(['Position', `${lat.toFixed(5)}, ${lon.toFixed(5)}`]);
  }

  const uptime = store.telemetry('system.uptime_s');
  if (Number.isFinite(uptime)) rows.push(['Running for', fmt.duration(uptime)]);

  const age = store.stats?.last_frame_age;
  if (Number.isFinite(age)) {
    rows.push(['Last update', age < 2 ? 'just now' : fmt.ago(age)]);
  }

  const signature = rows.map((row) => row.join('=')).join('|');
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;

  list.replaceChildren();
  for (const [key, value] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    list.append(dt, dd);
  }
}

/* --- tooltip --------------------------------------------------------- */

function renderTooltip(store, hover) {
  const tooltip = $('map-tooltip');
  if (!hover) {
    tooltip.hidden = true;
    return;
  }

  const { track, screen } = hover;
  const style = styleOf(track);
  const boat = store.state.boat?.position;
  const rows = [];

  rows.push(['Position', `${track.position[0].toFixed(1)}, ${track.position[1].toFixed(1)} m`]);
  rows.push(['Confidence', `${(100 * (track.confidence ?? 0)).toFixed(0)} %`]);
  rows.push(['Avoid radius', `${(track.avoid_radius ?? 0).toFixed(1)} m`]);

  if (boat) {
    const dx = track.position[0] - boat[0];
    const dy = track.position[1] - boat[1];
    const bearing = fmt.bearingFromVector([dx, dy], store.state.grid_bearing ?? 0);
    rows.push(['Range', `${Math.hypot(dx, dy).toFixed(1)} m`]);
    if (bearing !== null) {
      rows.push(['Bearing', `${bearing.toFixed(0)}° ${fmt.compassPoint(bearing)}`]);
    }
  }

  const direction = track.no_go?.dir ?? wrongSideDirection(track, store.state.upstream_direction);
  if (direction) {
    const length = track.no_go?.length ?? store.session.wrong_side_length ?? 20;
    const bearing = fmt.bearingFromVector(direction, store.state.grid_bearing ?? 0);
    rows.push([
      'Wrong side',
      `${length.toFixed(0)} m towards ${bearing === null ? '—' : `${bearing.toFixed(0)}°`}`,
    ]);
  }
  if (track.source) rows.push(['Source', track.source]);
  if (Number.isFinite(track.age)) rows.push(['Age', `${track.age.toFixed(1)} s`]);

  const title = document.createElement('div');
  title.className = 'map-tooltip-title';
  const swatch = document.createElement('span');
  swatch.className = 'legend-swatch';
  swatch.style.background = style.colour;
  title.append(swatch, document.createTextNode(`#${track.track_id} · ${style.label}`));

  const list = document.createElement('dl');
  for (const [key, value] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    list.append(dt, dd);
  }

  tooltip.replaceChildren(title, list);
  tooltip.hidden = false;

  // Keep the tooltip inside the map, flipping sides near the right edge.
  const wrap = $('map-wrap').getBoundingClientRect();
  const box = tooltip.getBoundingClientRect();
  const flipX = screen[0] + box.width + 24 > wrap.width;
  tooltip.style.left = `${Math.max(4, flipX ? screen[0] - box.width - 16 : screen[0] + 16)}px`;
  tooltip.style.top = `${Math.max(4, Math.min(screen[1] - box.height / 2, wrap.height - box.height - 4))}px`;
}

/* --- boot ------------------------------------------------------------ */

async function boot() {
  const { store, admin, prefs } = await bootShell();

  $('estop-card').hidden = !admin;

  /* --- map ---------------------------------------------------------- */

  let commandPanel = null;

  const map = new WorldMap({
    canvas: $('map'),
    wrap: $('map-wrap'),
    store,
    onHover: (hover) => renderTooltip(store, hover),
    onPick: (point) => commandPanel?.submitGoto(point),
  });

  if (prefs.layers) Object.assign(map.layers, prefs.layers);
  map.setBasemap(prefs.basemap ?? 'satellite');
  $('basemap-select').value = prefs.basemap ?? 'satellite';
  $('map-attrib').textContent = map.tiles.attribution;

  const layerBar = $('layer-bar');
  const layerChips = new Map();
  for (const { key, label } of LAYER_CHIPS) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip chip--toggle';
    chip.textContent = label;
    chip.addEventListener('click', () => {
      map.setLayer(key, !map.layers[key]);
      syncLayerChips();
      prefs.layers = { ...map.layers };
      savePrefs(prefs);
    });
    layerChips.set(key, chip);
    layerBar.append(chip);
  }

  const clearTrail = document.createElement('button');
  clearTrail.type = 'button';
  clearTrail.className = 'chip';
  clearTrail.textContent = 'Clear history';
  clearTrail.addEventListener('click', () => map.clearTrail());
  layerBar.append(clearTrail);

  function syncLayerChips() {
    for (const [key, chip] of layerChips) {
      const on = Boolean(map.layers[key]);
      chip.classList.toggle('is-on', on);
      chip.setAttribute('aria-pressed', String(on));
    }
    const follow = $('follow-toggle');
    follow.classList.toggle('is-on', map.follow);
    follow.setAttribute('aria-pressed', String(map.follow));
  }
  map.onLayerChange = syncLayerChips;
  syncLayerChips();

  $('basemap-select').addEventListener('change', (event) => {
    map.setBasemap(event.target.value);
    $('map-attrib').textContent = map.tiles.attribution;
    prefs.basemap = event.target.value;
    savePrefs(prefs);
  });
  $('zoom-in').addEventListener('click', () => map.zoomBy(1.35));
  $('zoom-out').addEventListener('click', () => map.zoomBy(1 / 1.35));
  $('fit-view').addEventListener('click', () => {
    map.fit();
    syncLayerChips();
  });
  $('follow-toggle').addEventListener('click', () => {
    map.setFollow(!map.follow);
    syncLayerChips();
  });

  // Readout: grid metres under the cursor, or the view centre when the pointer
  // is elsewhere, so the box always says something useful.
  const readoutCoords = $('readout-coords');
  const readoutScale = $('readout-scale');
  function updateReadout() {
    const [x, y] = map.pointer
      ? map.screenToWorld(map.pointer[0], map.pointer[1])
      : [map.camera.cx, map.camera.cy];
    const prefix = map.pointer ? '' : 'centre ';
    readoutCoords.textContent = `${prefix}x ${x.toFixed(1)}  y ${y.toFixed(1)} m`;
    readoutScale.textContent = `${map.camera.ppm.toFixed(1)} px/m`;
  }
  $('map').addEventListener('pointermove', updateReadout);
  window.setInterval(updateReadout, 250);
  updateReadout();

  map.start();

  /* --- figures ------------------------------------------------------ */

  const kpiStrip = new KpiStrip($('kpi-strip'), store, {
    keys: BASIC_KPIS,
    overrides: BASIC_LABELS,
  });

  /* --- operator controls (admin only) ------------------------------- */

  if (admin) {
    commandPanel = new CommandPanel(
      {
        estopButton: $('estop-btn'),
        gotoArm: $('goto-arm'),
      },
      store,
      { notify }
    );
    commandPanel.onGotoArmed = (on) => map.setPickMode(on);

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && commandPanel.pickingGoto) commandPanel.setGotoArmed(false);
    });
  }

  /* --- store -> ui -------------------------------------------------- */

  let hasFrame = false;

  store.on('state', () => {
    if (!hasFrame && store.state.boat) {
      hasFrame = true;
      $('map-empty').hidden = true;
    }
    map.onState();
    updateLegend(store);
    kpiStrip.update();
    updatePlainFacts(store);
    updateHeader(store);
  });

  store.on('stats', () => updateHeader(store));
  store.on('link', () => {
    updateHeader(store);
    map.invalidate();
  });

  connectShellStream(store);

  window.ligmax = { store, map, kpiStrip, commandPanel };

  startHeartbeat(store, () => {
    kpiStrip.update();
    updatePlainFacts(store);
  });
}

boot().catch((error) => {
  console.error(error);
  notify(`Page failed to start: ${error.message}`, 'error', 30000);
});
