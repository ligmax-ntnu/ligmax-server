/* Server calls and the telemetry stream.
 *
 * Telemetry arrives over Server-Sent Events rather than a WebSocket: it is
 * one-directional, survives proxies that mangle upgrades, and EventSource
 * reconnects on its own — which matters when the ground station is on 5G.
 * Commands travel the other way as ordinary authenticated POSTs.
 */

export async function fetchSession() {
  const response = await fetch('/api/session', { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`session request failed: ${response.status}`);
  return response.json();
}

export async function sendCommand(name, args = {}) {
  const response = await fetch('/api/command', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, args }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `command failed (${response.status})`);
  }
  return payload;
}

export async function logout() {
  await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
}

/* --- deployments ------------------------------------------------------ */

export async function fetchDeployState() {
  const response = await fetch('/api/deploy', { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`deploy state failed: ${response.status}`);
  return response.json();
}

/** Ask the node that owns `repo` to pull. It acts on its next outbound poll. */
export async function requestDeploy(repo) {
  const response = await fetch(`/api/deploy/${encodeURIComponent(repo)}`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `update request failed (${response.status})`);
  }
  return payload;
}

export async function cancelDeploy(repo) {
  const response = await fetch(`/api/deploy/${encodeURIComponent(repo)}/cancel`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `cancel failed (${response.status})`);
  }
  return payload;
}

/* --- stabilisation tuning --------------------------------------------- */

/**
 * The tunable-parameter table, its ranges and help text, plus the profiles saved
 * on the ground station. The *values* do not come from here — they arrive with
 * the telemetry as `telemetry.tuning.values`, read off the flight controller by
 * the vessel, so the panel and every other measurement share one source.
 */
export async function fetchTuning() {
  const response = await fetch('/api/tuning', { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`tuning table failed: ${response.status}`);
  return response.json();
}

async function tuningCall(path, options) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `request failed (${response.status})`);
  }
  return payload;
}

/** Snapshot the tuning under a name. With no `values`, the server records what
 *  the vessel is reporting right now rather than what this tab last drew. */
export async function saveTuningProfile(name, { note = '', values = null } = {}) {
  return tuningCall('/api/tuning/profiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(values ? { name, note, values } : { name, note }),
  });
}

/** Queue a `set_param` per value the vessel does not already have. */
export async function applyTuningProfile(name) {
  return tuningCall(`/api/tuning/profiles/${encodeURIComponent(name)}/apply`, {
    method: 'POST',
  });
}

export async function deleteTuningProfile(name) {
  return tuningCall(`/api/tuning/profiles/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
}

/**
 * Open the telemetry stream. Returns a handle with `.close()`.
 * `handlers` keys map to SSE event names, plus `onOpen` / `onError`.
 */
export function connectStream(handlers = {}) {
  let source = null;
  let closed = false;
  let retryTimer = null;
  let backoff = 1000;

  const open = () => {
    if (closed) return;
    source = new EventSource('/api/stream', { withCredentials: true });

    source.addEventListener('open', () => {
      backoff = 1000;
      handlers.onOpen?.();
    });

    for (const event of ['hello', 'snapshot', 'state', 'stats', 'logs', 'commands']) {
      source.addEventListener(event, (message) => {
        let payload;
        try {
          payload = JSON.parse(message.data);
        } catch (error) {
          console.warn(`could not parse "${event}" frame`, error);
          return;
        }
        handlers[event]?.(payload);
      });
    }

    source.addEventListener('error', () => {
      handlers.onError?.();
      // EventSource retries by itself, but only while the connection is in a
      // recoverable state. A server restart closes it for good, so take over.
      if (source.readyState === EventSource.CLOSED && !closed) {
        source.close();
        retryTimer = window.setTimeout(open, backoff);
        backoff = Math.min(backoff * 1.7, 15000);
      }
    });
  };

  open();

  return {
    close() {
      closed = true;
      window.clearTimeout(retryTimer);
      source?.close();
    },
  };
}

/**
 * Read and clear the one-shot notice cookie the server sets after an admin
 * key is redeemed (`granted`, `denied` or `throttled`).
 */
export function takeNotice() {
  const match = document.cookie.match(/(?:^|;\s*)lx_notice=([^;]*)/);
  if (!match) return null;
  document.cookie = 'lx_notice=; Max-Age=0; path=/';
  return decodeURIComponent(match[1]);
}

/**
 * Belt-and-braces URL scrubbing. The server already redirects `?key=…` away,
 * but if the console is ever reached without that redirect (a cached page, a
 * hand-edited link) this strips the key from the address bar and from the
 * history entry, so it cannot be read over someone's shoulder or replayed.
 */
export function scrubUrl() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has('key')) return false;
  url.searchParams.delete('key');
  window.history.replaceState({}, '', url.pathname + (url.search || '') + url.hash);
  return true;
}
