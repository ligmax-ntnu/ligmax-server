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
