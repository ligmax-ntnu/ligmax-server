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

import { AutopilotPanel } from './autopilot.js';
import { CameraPanel } from './camera.js';
import { CommandPanel } from './commands.js';
import * as fmt from './format.js';
import { WorldMap } from './map.js';
import { legendFor, styleOf } from './obstacles.js';
import { wrongSideDirection } from './nogo.js';
import { roleList } from './plan.js';
import {
  $,
  bootShell,
  connectShellStream,
  notify,
  savePrefs,
  startHeartbeat,
  updateHeader,
} from './shell.js';
import { resolve as resolveStatus } from './status.js';
import { KpiStrip } from './telemetry.js';

/* The figures a visitor can read unaided, in plain words. The precise versions of
   these — and every other tile — are on the control page.

   `status` leads, because it is the required status indicator and because it is
   the only one that answers "should I be worried". */
const BASIC_KPIS = ['status', 'speed', 'battery', 'target', 'detections'];

const BASIC_LABELS = {
  status: {
    label: 'Doing',
    // Plain words for the same five states the lights show, so someone on the
    // pontoon and someone at the screen describe the boat the same way.
    value: (store) => resolveStatus(store).meta.plain,
    sub: (store) => {
      const resolved = resolveStatus(store);
      if (resolved.stale) return resolved.reason ?? 'no word from the boat';
      return store.state.status_text ?? resolved.meta.detail;
    },
  },
  speed: { label: 'Speed' },
  battery: {
    label: 'Battery',
    sub: (store) => {
      const wh = store.telemetry('battery.remaining_wh');
      return Number.isFinite(wh) ? `about ${wh.toFixed(0)} Wh left` : 'of the pack';
    },
  },
  target: {
    label: 'Next waypoint',
    sub: () => 'metres to go',
  },
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
  // The two halves of the required comparison, each toggleable on its own so you
  // can strip the chart back to "where it went" against "where it should have".
  { key: 'route', label: 'Ideal route' },
  { key: 'trail', label: 'Track history' },
  { key: 'cog', label: 'Course over ground' },
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

  const { lat, lon } = store.position;
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    rows.push(['Position', `${lat.toFixed(5)}, ${lon.toFixed(5)}`]);
  }

  // Heading and course as one row: on a page for people who have never seen the
  // boat, "pointing NE, moving ENE" says more than two angles would.
  const heading = store.headingDegrees;
  const course = store.courseDegrees;
  if (Number.isFinite(heading)) {
    rows.push([
      'Pointing',
      Number.isFinite(course)
        ? `${fmt.compassPoint(heading)}, travelling ${fmt.compassPoint(course)}`
        : fmt.compassPoint(heading),
    ]);
  }

  const waypoint = store.distanceToWaypoint;
  if (Number.isFinite(waypoint)) {
    rows.push(['Next waypoint', `${waypoint.toFixed(0)} m away`]);
  }

  const wh = store.telemetry('battery.remaining_wh');
  if (Number.isFinite(wh)) rows.push(['Energy left', `${wh.toFixed(0)} Wh`]);

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
  if (Number.isFinite(track.width_m)) rows.push(['Width', `${track.width_m.toFixed(2)} m`]);
  if (Number.isFinite(track.speed)) rows.push(['Speed', `${track.speed.toFixed(2)} m/s`]);
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
  // How many camera votes a cardinal has, and whether they have settled. The
  // boat falls back to the planned side until they do, so "not committed" is
  // the difference between the rule steering the boat and the plan doing it.
  if (track.cardinal) rows.push(['Topmark', track.cardinal]);

  const title = document.createElement('div');
  title.className = 'map-tooltip-title';
  const swatch = document.createElement('span');
  swatch.className = 'legend-swatch';
  swatch.style.background = style.colour;
  title.append(
    swatch,
    document.createTextNode(`#${track.track_id} · ${track.label ?? style.label}`)
  );

  const list = document.createElement('dl');
  for (const [key, value] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    list.append(dt, dd);
  }

  const nodes = [title, list];
  // The tracker's own sentence about this object. NJORD §11.4 scores exactly
  // this — "how a detected object changed the plan" — so it gets its own line in
  // the boat's words rather than being folded into a field list.
  if (track.why) {
    const why = document.createElement('p');
    why.className = 'map-tooltip-why';
    why.textContent = track.why;
    nodes.push(why);
  }

  tooltip.replaceChildren(...nodes);
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

  /* --- camera ------------------------------------------------------- */

  // No quality picker on this page: choosing what the boat spends its uplink on
  // is an operator decision, and it lives on /control with the rest of them.
  const cameraPanel = new CameraPanel(
    {
      card: $('camera-card'),
      grid: $('camera-grid'),
      status: $('camera-status'),
      toggle: $('camera-toggle'),
      viewerToggle: $('camera-viewer-toggle'),
    },
    { admin, notify, prefs, savePrefs }
  ).start();

  /* --- operator controls (admin only) ------------------------------- */

  if (admin) {
    commandPanel = new CommandPanel(
      {
        estopButton: $('estop-btn'),
        gotoArm: $('goto-arm'),
        missionArm: $('mission-arm'),
        missionUndo: $('mission-undo'),
        missionClear: $('mission-clear'),
        missionSend: $('mission-send'),
        missionCount: $('mission-count'),
      },
      store,
      { notify }
    );
    commandPanel.onGotoArmed = (on) => map.setPickMode(on);
    commandPanel.onMissionArmed = (on) => map.setMissionMode(on);
    commandPanel.onMissionUndo = () => map.undoMissionPoint();
    commandPanel.onMissionClear = () => map.clearMissionDraft();
    map.onMissionChange = (points) => commandPanel.setMissionPoints(points);

    // Which rules apply to the next points clicked. Populated from the server's
    // role table so adding a role on the vessel needs no change here.
    const rolePicker = $('mission-role');
    for (const entry of roleList(store.session)) {
      const option = document.createElement('option');
      option.value = entry.name;
      option.textContent = entry.label;
      option.title = entry.help;
      rolePicker.append(option);
    }
    const applyRole = () => {
      map.setMissionRole(rolePicker.value);
      rolePicker.title =
        roleList(store.session).find((entry) => entry.name === rolePicker.value)?.help ?? '';
    };
    rolePicker.addEventListener('change', applyRole);
    applyRole();

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (commandPanel.pickingGoto) commandPanel.setGotoArmed(false);
      if (commandPanel.pickingMission) commandPanel.setMissionArmed(false);
    });
  }

  /* --- autonomy ----------------------------------------------------- */

  // Rendered for everyone: what the boat has decided is a measurement, and the
  // jury reads it off this page. Only the buttons are gated.
  const autopilotPanel = new AutopilotPanel($('autopilot-panel'), store, {
    notify,
    canSend: admin,
    compact: true,
  });

  /* --- store -> ui -------------------------------------------------- */

  /* The overlay names which "no boat on the chart" this is, because the two have
     different fixes: nothing has ever arrived (link, keys, is the vessel on?)
     versus frames arriving with no position in them (no GNSS fix yet). It is not
     a one-shot latch — a vessel that loses its fix mid-run goes back to saying so
     rather than leaving an empty chart with no explanation. */
  const mapEmpty = $('map-empty');
  function updateMapEmpty() {
    const placed = Array.isArray(store.state.boat?.position);
    mapEmpty.hidden = placed;
    if (placed) return;
    mapEmpty.textContent = store.stats?.last_frame_at
      ? 'The boat is reporting in, but has no position to put on the chart yet.'
      : 'Waiting for the boat to report in…';
  }

  store.on('state', () => {
    updateMapEmpty();
    map.onState();
    updateLegend(store);
    kpiStrip.update();
    updatePlainFacts(store);
    autopilotPanel.update();
    updateHeader(store);
  });

  store.on('stats', () => {
    updateHeader(store);
    updateMapEmpty();
  });
  store.on('link', () => {
    updateHeader(store);
    map.invalidate();
  });

  connectShellStream(store);

  autopilotPanel.update();

  window.ligmax = { store, map, kpiStrip, commandPanel, cameraPanel, autopilotPanel };

  startHeartbeat(store, () => {
    kpiStrip.update();
    updatePlainFacts(store);
  });
}

boot().catch((error) => {
  console.error(error);
  notify(`Page failed to start: ${error.message}`, 'error', 30000);
});
