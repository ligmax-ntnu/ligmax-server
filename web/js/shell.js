/* The page shell both consoles share: session, header, theme, notices, stream.
 *
 * There are two pages and they have very different jobs:
 *
 *   /          the overview. A map and four figures anyone can read. Someone who
 *              has never seen the boat should understand it without help.
 *   /control   everything else. Every telemetry field, the controls, the logs,
 *              the audit trail and the repo list. Dense on purpose.
 *
 * Both carry the same header and footer and both need a session, a store and the
 * SSE stream, so that part lives here rather than being written twice. Anything
 * page-specific stays in main.js or control.js.
 *
 * Every DOM lookup here is null-safe: a page is allowed not to have a widget.
 */

import { connectStream, fetchSession, logout, scrubUrl, takeNotice } from './api.js';
import * as fmt from './format.js';
import { setTypeTable } from './obstacles.js';
import { lightsAgree, metaFor, resolve as resolveStatus } from './status.js';
import { Store } from './store.js';

export const $ = (id) => document.getElementById(id);

const PREFS_KEY = 'ligmax.console.prefs';

/* --- preferences ----------------------------------------------------- */

export function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) ?? '{}');
  } catch {
    return {};
  }
}

export function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* private browsing, nothing to do */
  }
}

/* --- notices --------------------------------------------------------- */

export function notify(message, level = 'info', timeout = 6000) {
  const stack = $('notice-stack');
  if (!stack) return;
  const notice = document.createElement('div');
  notice.className = 'notice';
  notice.dataset.level = level;
  notice.textContent = message;
  stack.append(notice);
  window.setTimeout(() => notice.remove(), timeout);
}

/* --- theme ----------------------------------------------------------- */

export function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

/* --- header ---------------------------------------------------------- */

/** Status pill, link pill, mode, armed, E-stop banner, footer counters. */
export function updateHeader(store) {
  const level = store.linkLevel;
  const pill = $('link-pill');
  if (pill) pill.dataset.state = level;

  // The status indicator is the required headline figure, so it goes in the
  // header of both pages and is the same on both. `resolveStatus` is what turns
  // a stale link into "out of control" rather than a stale claim.
  const status = resolveStatus(store);
  const statusPill = $('status-pill');
  if (statusPill) {
    statusPill.dataset.status = status.status ?? 'UNKNOWN';
    statusPill.dataset.level = status.meta.level;
    const label = $('status-text');
    if (label) label.textContent = status.meta.label;
    const detail = status.stale && status.reported
      ? `${status.meta.detail} Last reported ${metaFor(status.reported).label}; ${status.reason}.`
      : status.reason
        ? `${status.meta.detail} (${status.reason})`
        : status.meta.detail;
    statusPill.title = detail;
    statusPill.setAttribute('aria-label', `Vessel status: ${status.meta.label}. ${detail}`);
  }

  // A hull showing the wrong colour is a safety-visible fault, so it is called
  // out rather than left for someone to spot in the telemetry panel.
  const lightsPill = $('lights-pill');
  if (lightsPill) {
    const agrees = lightsAgree(store, status.status);
    const shown = store.telemetry('lights.colour');
    lightsPill.hidden = agrees !== false;
    if (agrees === false) {
      lightsPill.textContent = `Lights show ${shown}`;
      lightsPill.title =
        `The hull is showing ${shown} but the status is ${status.meta.label}, which should be ` +
        `${status.meta.lightName}. Check the lights ESP32 link.`;
    }
  }

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
  const linkText = $('link-text');
  if (linkText) linkText.textContent = text;

  const modePill = $('mode-pill');
  if (modePill) modePill.textContent = store.state.mode ?? 'no mode';

  const armedPill = $('armed-pill');
  if (armedPill) armedPill.hidden = store.telemetry('autonomy.armed') !== false;

  const banner = $('estop-banner');
  if (banner) banner.hidden = !store.state.estop;

  const footer = $('footer-link-meta');
  if (footer) {
    const peer = store.stats?.peer;
    footer.textContent = [
      store.stats?.frames ? `${store.stats.frames.toLocaleString('en-GB')} frames` : null,
      store.stats?.seq_gaps ? `${store.stats.seq_gaps} dropped` : null,
      peer ? `from ${peer}` : null,
    ]
      .filter(Boolean)
      .join(' · ');
  }
}

/* --- boot ------------------------------------------------------------ */

/**
 * Everything both pages do before they diverge. Returns the store, the session
 * payload, whether this viewer is an admin, and the preferences object (mutate
 * and `savePrefs` it).
 */
export async function bootShell() {
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
    session = {
      admin: false,
      admin_possible: false,
      commands: {},
      obstacle_types: {},
      wrong_side_length: 20,
    };
  }
  store.session = session;
  setTypeTable(session.obstacle_types);

  const admin = Boolean(session.admin);

  const badge = $('role-badge');
  if (badge) {
    badge.dataset.role = admin ? 'admin' : 'read';
    badge.textContent = admin ? 'Read / write' : 'Read-only';
  }

  const logoutButton = $('logout-btn');
  if (logoutButton) {
    logoutButton.hidden = !admin;
    logoutButton.addEventListener('click', async () => {
      await logout();
      window.location.reload();
    });
  }

  const themeToggle = $('theme-toggle');
  themeToggle?.addEventListener('click', () => {
    const current =
      document.documentElement.dataset.theme ??
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    prefs.theme = next;
    savePrefs(prefs);
  });

  // Mark the current page in the nav so it is obvious which one you are on.
  for (const link of document.querySelectorAll('[data-nav]')) {
    if (link.getAttribute('href') === window.location.pathname) {
      link.setAttribute('aria-current', 'page');
    }
  }

  if (!session.shared_settings) {
    notify(
      'Server could not import shared_settings.py — obstacle names come from the built-in mirror.',
      'warn',
      12000
    );
  }

  return { store, session, admin, prefs };
}

/* --- stream ---------------------------------------------------------- */

/** Wire the SSE stream into the store. `hooks` adds per-page reactions. */
export function connectShellStream(store, hooks = {}) {
  return connectStream({
    onOpen: () => store.setStreamState('open'),
    onError: () => store.setStreamState('retrying'),
    hello: () => store.setStreamState('open'),
    snapshot: (payload) => {
      store.applySnapshot(payload);
      hooks.snapshot?.(payload);
    },
    state: (payload) => store.applyState(payload),
    stats: (payload) => store.applyStats(payload),
    logs: (payload) => store.applyLogs(payload),
    commands: (payload) => store.applyCommands(payload),
  });
}

/**
 * A quiet vessel produces no events, but "12 s old" still has to keep counting,
 * so age-dependent parts of the UI need their own tick.
 */
export function startHeartbeat(store, onTick, interval = 500) {
  return window.setInterval(() => {
    if (store.stats?.last_frame_at) {
      store.stats.last_frame_age = Date.now() / 1000 - store.stats.last_frame_at;
    }
    updateHeader(store);
    onTick?.();
  }, interval);
}
