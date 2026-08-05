/* Boots the console and wires the pieces together. */

import { connectStream, fetchSession, logout, scrubUrl, takeNotice } from './api.js';
import { CommandPanel, renderCommandHistory } from './commands.js';
import { DeployPanel } from './deploy.js';
import * as fmt from './format.js';
import { LogConsole, downloadText } from './logs.js';
import { WorldMap } from './map.js';
import { legendFor, setTypeTable, styleOf } from './obstacles.js';
import { wrongSideDirection } from './nogo.js';
import { Store } from './store.js';
import { KpiStrip, TelemetryPanels } from './telemetry.js';

const $ = (id) => document.getElementById(id);
const PREFS_KEY = 'ligmax.console.prefs';

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

/* --- preferences ----------------------------------------------------- */

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) ?? '{}');
  } catch {
    return {};
  }
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* private browsing, nothing to do */
  }
}

/* --- notices --------------------------------------------------------- */

function notify(message, level = 'info', timeout = 6000) {
  const stack = $('notice-stack');
  const notice = document.createElement('div');
  notice.className = 'notice';
  notice.dataset.level = level;
  notice.textContent = message;
  stack.append(notice);
  window.setTimeout(() => notice.remove(), timeout);
}

/* --- theme ----------------------------------------------------------- */

function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

/* --- header ---------------------------------------------------------- */

function updateHeader(store) {
  const level = store.linkLevel;
  const pill = $('link-pill');
  pill.dataset.state = level;

  const hz = store.stats?.hz;
  const age = store.stats?.last_frame_age;
  const transport = store.stats?.transport;
  let text;
  if (store.streamState !== 'open') {
    text = store.streamState === 'retrying' ? 'Reconnecting…' : 'Connecting…';
  } else if (!store.stats?.last_frame_at) {
    text = 'No vessel yet';
  } else if (level === 'live') {
    text = `Live · ${fmt.fixed(hz, 1)} Hz${transport ? ` · ${transport}` : ''}`;
  } else if (level === 'stale') {
    text = `Stale · ${fmt.fixed(age, 1)} s`;
  } else {
    text = `Signal lost · ${fmt.ago(age)}`;
  }
  $('link-text').textContent = text;

  $('mode-pill').textContent = store.state.mode ?? 'no mode';

  const armed = store.telemetry('autonomy.armed');
  const armedPill = $('armed-pill');
  armedPill.hidden = armed !== false;

  const estop = Boolean(store.state.estop);
  $('estop-banner').hidden = !estop;

  const peer = store.stats?.peer;
  $('footer-link-meta').textContent = [
    store.stats?.frames ? `${store.stats.frames.toLocaleString('en-GB')} frames` : null,
    store.stats?.seq_gaps ? `${store.stats.seq_gaps} dropped` : null,
    peer ? `from ${peer}` : null,
  ]
    .filter(Boolean)
    .join(' · ');
}

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
  scrubUrl();

  const notice = takeNotice();
  if (notice === 'granted') notify('Operator session started. You can send commands.', 'ok');
  else if (notice === 'denied') notify('That key was not accepted. Still read-only.', 'error');
  else if (notice === 'throttled') {
    notify('Too many failed key attempts from this address. Try again later.', 'error');
  }

  const prefs = loadPrefs();
  applyTheme(prefs.theme);

  const store = new Store();

  let session;
  try {
    session = await fetchSession();
  } catch (error) {
    notify(`Could not reach the server: ${error.message}`, 'error', 20000);
    session = { admin: false, admin_possible: false, commands: {}, obstacle_types: {}, wrong_side_length: 20 };
  }
  store.session = session;
  setTypeTable(session.obstacle_types);

  const admin = Boolean(session.admin);
  $('role-badge').dataset.role = admin ? 'admin' : 'read';
  $('role-badge').textContent = admin ? 'Read / write' : 'Read-only';
  $('command-card').hidden = !admin;
  $('readonly-card').hidden = admin;
  $('logout-btn').hidden = !admin;
  if (!session.shared_settings) {
    notify(
      'Server could not import shared_settings.py — obstacle names come from the built-in mirror.',
      'warn',
      12000
    );
  }

  /* --- map ---------------------------------------------------------- */

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

  /* --- panels ------------------------------------------------------- */

  const kpiStrip = new KpiStrip($('kpi-strip'), store);
  const telemetryPanels = new TelemetryPanels($('telemetry-panels'), store);

  const logConsole = new LogConsole(
    {
      view: $('log-view'),
      chips: $('level-chips'),
      filterInput: $('log-filter'),
      pauseButton: $('log-pause'),
      countLabel: $('log-count'),
      statusLabel: $('log-status'),
    },
    store
  );

  $('log-clear').addEventListener('click', () => logConsole.clear());
  $('log-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(logConsole.visibleText());
      notify('Visible log lines copied.', 'ok', 3000);
    } catch {
      notify('Clipboard access was refused by the browser.', 'warn');
    }
  });
  $('log-download').addEventListener('click', () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    downloadText(`ligmax-log-${stamp}.txt`, logConsole.visibleText());
  });

  /* --- commands ----------------------------------------------------- */

  let commandPanel = null;
  if (admin) {
    commandPanel = new CommandPanel(
      {
        estopButton: $('estop-btn'),
        modeSelect: $('mode-select'),
        modeApply: $('mode-apply'),
        speedLimit: $('speed-limit'),
        speedApply: $('speed-apply'),
        gotoArm: $('goto-arm'),
        rawPayload: $('raw-payload'),
        rawSend: $('raw-send'),
        quickCommands: $('quick-commands'),
      },
      store,
      { notify }
    );
    commandPanel.onGotoArmed = (on) => map.setPickMode(on);

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && commandPanel.pickingGoto) commandPanel.setGotoArmed(false);
    });
  }

  /* The deploy panel is visible read-only too — knowing which SHA each node runs is
     useful without command rights. Only the buttons are gated on admin. */
  const deployPanel = new DeployPanel($('deploy-list'), { notify });
  deployPanel.setAdmin(admin);
  deployPanel.start();

  $('logout-btn').addEventListener('click', async () => {
    await logout();
    window.location.reload();
  });

  $('theme-toggle').addEventListener('click', () => {
    const current =
      document.documentElement.dataset.theme ??
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    prefs.theme = next;
    savePrefs(prefs);
  });

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
    telemetryPanels.update();
    commandPanel?.syncModes();
    updateHeader(store);
  });

  store.on('stats', () => updateHeader(store));
  store.on('link', () => {
    updateHeader(store);
    map.invalidate();
  });
  store.on('logs', (entries) => logConsole.append(entries));
  store.on('snapshot', () => {
    logConsole.rebuild();
    renderCommandHistory($('cmd-list'), store.commands);
  });
  store.on('commands', (commands) => renderCommandHistory($('cmd-list'), commands));

  renderCommandHistory($('cmd-list'), []);

  /* --- stream ------------------------------------------------------- */

  connectStream({
    onOpen: () => store.setStreamState('open'),
    onError: () => store.setStreamState('retrying'),
    hello: () => store.setStreamState('open'),
    snapshot: (payload) => store.applySnapshot(payload),
    state: (payload) => store.applyState(payload),
    stats: (payload) => store.applyStats(payload),
    logs: (payload) => store.applyLogs(payload),
    commands: (payload) => store.applyCommands(payload),
  });

  // Reachable from the browser console, because the whole point of this page
  // is debugging: `ligmax.store.state`, `ligmax.map.camera`, and so on.
  window.ligmax = { store, map, logConsole, commandPanel, kpiStrip, telemetryPanels };

  // The vessel going quiet produces no events, so age-dependent parts of the
  // UI need their own heartbeat.
  window.setInterval(() => {
    if (store.stats?.last_frame_at) {
      store.stats.last_frame_age = Date.now() / 1000 - store.stats.last_frame_at;
    }
    updateHeader(store);
    kpiStrip.update();
  }, 500);
}

boot().catch((error) => {
  console.error(error);
  notify(`Console failed to start: ${error.message}`, 'error', 30000);
});
