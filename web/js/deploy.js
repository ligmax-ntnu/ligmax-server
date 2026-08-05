/* The software-update panel: one row per repo, with a button that asks its node to pull.
 *
 * Nothing here reaches a node directly. Pressing Update records a request on the
 * server; the node notices on its own outbound poll (deploy/ligmax-update.sh
 * --on-request) and reports back what happened. So a row can legitimately sit at
 * "waiting" for up to a poll interval, and the operator needs to see that rather
 * than wonder whether the click registered.
 *
 * This panel polls /api/deploy rather than riding the SSE stream: deployments are a
 * once-in-a-while thing, and keeping them off the telemetry cursor means a stuck
 * update can never interfere with vessel data.
 */

import { cancelDeploy, fetchDeployState, requestDeploy } from './api.js';
import * as fmt from './format.js';

const POLL_IDLE_MS = 15000; // nothing outstanding: check occasionally
const POLL_BUSY_MS = 3000; // something is pending: watch it closely

const RESULT_LEVEL = {
  ok: 'ok',
  'no-change': 'muted',
  refused: 'warn',
  failed: 'danger',
};

const RESULT_LABEL = {
  ok: 'Updated',
  'no-change': 'Already current',
  refused: 'Refused',
  failed: 'Failed',
};

export class DeployPanel {
  constructor(root, { notify }) {
    this.root = root;
    this.notify = notify;
    this.admin = false;
    this.repos = [];
    this.timer = null;
    this.busy = new Set();
    this.serverTime = null;
  }

  /** fmt.ago() wants elapsed seconds, not a unix timestamp. Measure against the
   *  server's own clock so a skewed browser clock cannot invent negative ages. */
  _elapsed(timestamp) {
    if (typeof timestamp !== 'number' || this.serverTime == null) return null;
    return Math.max(0, this.serverTime - timestamp);
  }

  start() {
    this.refresh();
  }

  stop() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  setAdmin(isAdmin) {
    if (this.admin === isAdmin) return;
    this.admin = isAdmin;
    this.render();
  }

  async refresh() {
    if (this.timer) clearTimeout(this.timer);
    try {
      const state = await fetchDeployState();
      this.serverTime = typeof state.server_time === 'number' ? state.server_time : null;
      this.repos = state.repos ?? [];
      this.admin = Boolean(state.admin);
      this.render();
    } catch {
      // A failed poll is not worth a toast — the link pill already shows trouble.
    }
    const pending = this.repos.some((r) => r.pending);
    this.timer = setTimeout(() => this.refresh(), pending ? POLL_BUSY_MS : POLL_IDLE_MS);
  }

  async _request(repo) {
    if (this.busy.has(repo)) return;
    this.busy.add(repo);
    this.render();
    try {
      await requestDeploy(repo);
      this.notify(`Update requested for ${repo}. Waiting for the node to pick it up.`, 'info');
    } catch (error) {
      this.notify(error.message, 'error');
    } finally {
      this.busy.delete(repo);
      this.refresh();
    }
  }

  async _cancel(repo) {
    try {
      await cancelDeploy(repo);
      this.notify(`Update request for ${repo} cancelled.`, 'info');
    } catch (error) {
      this.notify(error.message, 'error');
    } finally {
      this.refresh();
    }
  }

  render() {
    if (!this.root) return;
    this.root.replaceChildren();

    for (const repo of this.repos) {
      this.root.append(this._row(repo));
    }

    if (!this.repos.length) {
      const empty = document.createElement('p');
      empty.className = 'card-note';
      empty.textContent = 'No repos configured. Set LIGMAX_REPOS in .env.';
      this.root.append(empty);
    }
  }

  _row(repo) {
    const row = document.createElement('div');
    row.className = 'deploy-row';
    if (repo.pending) row.dataset.pending = 'true';

    const name = document.createElement('div');
    name.className = 'deploy-name';
    const label = document.createElement('span');
    label.className = 'deploy-repo';
    label.textContent = repo.name;
    name.append(label);

    const dot = document.createElement('span');
    dot.className = 'deploy-dot';
    dot.dataset.state = repo.node_online ? 'online' : 'offline';
    const polled = this._elapsed(repo.last_poll);
    dot.title = repo.last_poll
      ? `Node polled ${fmt.ago(polled)}`
      : 'This node has never polled — is its update timer installed?';
    name.prepend(dot);

    const status = document.createElement('div');
    status.className = 'deploy-status';
    status.append(...this._statusParts(repo));

    const actions = document.createElement('div');
    actions.className = 'deploy-actions';
    if (this.admin) {
      if (repo.pending) {
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn--outline btn--sm';
        cancel.textContent = 'Cancel';
        cancel.addEventListener('click', () => this._cancel(repo.name));
        actions.append(cancel);
      } else {
        const update = document.createElement('button');
        update.type = 'button';
        update.className = 'btn btn--primary btn--sm';
        update.textContent = 'Update';
        update.disabled = this.busy.has(repo.name);
        if (!repo.node_online) {
          update.title =
            'This node has not polled recently. The request will wait until it does.';
        }
        update.addEventListener('click', () => this._request(repo.name));
        actions.append(update);
      }
    }

    row.append(name, status, actions);
    return row;
  }

  _statusParts(repo) {
    const parts = [];

    if (repo.pending) {
      const waiting = document.createElement('span');
      waiting.className = 'deploy-tag';
      waiting.dataset.level = 'warn';
      const since = repo.waiting_for != null ? ` ${fmt.duration(repo.waiting_for)}` : '';
      waiting.textContent = `Waiting${since}`;
      parts.push(waiting);
    } else if (repo.last_result) {
      const tag = document.createElement('span');
      tag.className = 'deploy-tag';
      tag.dataset.level = RESULT_LEVEL[repo.last_result] ?? 'muted';
      tag.textContent = RESULT_LABEL[repo.last_result] ?? repo.last_result;
      parts.push(tag);
    }

    if (repo.head) {
      const head = document.createElement('code');
      head.className = 'deploy-head';
      head.textContent = repo.head.slice(0, 8);
      parts.push(head);
    }

    const detail = document.createElement('span');
    detail.className = 'deploy-detail';
    if (repo.last_message) {
      detail.textContent = repo.last_message;
    } else if (!repo.pending && !repo.last_result) {
      detail.textContent = repo.last_poll
        ? 'No update run this session.'
        : 'Node has not checked in.';
    }
    if (detail.textContent) parts.push(detail);

    if (repo.last_finished && !repo.pending) {
      const when = document.createElement('span');
      when.className = 'deploy-when';
      when.textContent = fmt.ago(this._elapsed(repo.last_finished));
      parts.push(when);
    }

    return parts;
  }
}
