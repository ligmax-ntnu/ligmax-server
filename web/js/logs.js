/* The cleartext debug console.
 *
 * Appends incrementally rather than re-rendering, keeps a hard cap on DOM
 * nodes, and only autoscrolls while you are already at the bottom — scrolling
 * up to read something is treated as "hold still", which is what you want the
 * moment an error goes past.
 */

import * as fmt from './format.js';

const LEVELS = ['DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL'];
const DOM_LIMIT = 1200;

export class LogConsole {
  constructor({ view, chips, filterInput, pauseButton, countLabel, statusLabel }, store) {
    this.view = view;
    this.store = store;
    this.filterInput = filterInput;
    this.pauseButton = pauseButton;
    this.countLabel = countLabel;
    this.statusLabel = statusLabel;

    this.enabled = new Set(LEVELS);
    this.filterText = '';
    this.paused = false;
    this.pending = [];
    this.shown = 0;
    this.suppressed = 0;
    this.pinnedToBottom = true;

    this._buildChips(chips);
    this._bind();
    this._renderEmpty();
  }

  _buildChips(container) {
    for (const level of LEVELS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip chip--toggle is-on';
      chip.dataset.level = level;
      chip.textContent = level;
      chip.setAttribute('aria-pressed', 'true');
      chip.addEventListener('click', () => {
        const on = this.enabled.has(level);
        if (on) this.enabled.delete(level);
        else this.enabled.add(level);
        chip.classList.toggle('is-on', !on);
        chip.setAttribute('aria-pressed', String(!on));
        this.rebuild();
      });
      container.append(chip);
    }
  }

  _bind() {
    this.filterInput?.addEventListener('input', () => {
      this.filterText = this.filterInput.value.trim().toLowerCase();
      this.rebuild();
    });

    this.pauseButton?.addEventListener('click', () => {
      this.paused = !this.paused;
      this.pauseButton.classList.toggle('is-on', this.paused);
      this.pauseButton.setAttribute('aria-pressed', String(this.paused));
      this.pauseButton.textContent = this.paused ? 'Resume' : 'Pause';
      if (!this.paused) this.flush();
      this._updateFooter();
    });

    // Track whether the operator is reading history rather than following.
    this.view.addEventListener('scroll', () => {
      const distance = this.view.scrollHeight - this.view.scrollTop - this.view.clientHeight;
      this.pinnedToBottom = distance < 24;
      this._updateFooter();
    });
  }

  _matches(entry) {
    if (!this.enabled.has(entry.level)) return false;
    if (!this.filterText) return true;
    const haystack = `${entry.name} ${entry.msg} ${entry.level}`.toLowerCase();
    return haystack.includes(this.filterText);
  }

  _renderEmpty() {
    if (this.shown > 0) return;
    this.view.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'log-empty';
    empty.textContent = 'No log lines match the current filter.';
    this.view.append(empty);
  }

  _lineNode(entry) {
    const line = document.createElement('div');
    line.className = 'log-line';
    line.dataset.level = entry.level;

    const time = document.createElement('span');
    time.className = 'log-time';
    time.textContent = fmt.clockTime(entry.t);

    const level = document.createElement('span');
    level.className = 'log-level';
    level.textContent = entry.level;

    const name = document.createElement('span');
    name.className = 'log-name';
    name.textContent = entry.name;
    name.title = entry.name;

    const message = document.createElement('span');
    message.className = 'log-msg';
    message.textContent = entry.msg;

    line.append(time, level, name, message);
    return line;
  }

  /** Called with just the newly arrived entries. */
  append(entries) {
    if (this.paused) {
      this.pending.push(...entries);
      if (this.pending.length > 4000) this.pending.splice(0, this.pending.length - 4000);
      this._updateFooter();
      return;
    }
    this._appendNow(entries);
  }

  _appendNow(entries) {
    const fragment = document.createDocumentFragment();
    let added = 0;
    for (const entry of entries) {
      if (!this._matches(entry)) {
        this.suppressed += 1;
        continue;
      }
      fragment.append(this._lineNode(entry));
      added += 1;
    }
    if (!added) {
      this._updateFooter();
      return;
    }

    if (this.shown === 0) this.view.replaceChildren();
    this.view.append(fragment);
    this.shown += added;

    while (this.shown > DOM_LIMIT && this.view.firstChild) {
      this.view.removeChild(this.view.firstChild);
      this.shown -= 1;
    }

    if (this.pinnedToBottom) this.view.scrollTop = this.view.scrollHeight;
    this._updateFooter();
  }

  flush() {
    const pending = this.pending;
    this.pending = [];
    if (pending.length) this._appendNow(pending);
  }

  /** Re-render everything from the store, e.g. after a filter change. */
  rebuild() {
    this.shown = 0;
    this.suppressed = 0;
    this.view.replaceChildren();
    const matching = this.store.logs.filter((entry) => this._matches(entry));
    this.suppressed = this.store.logs.length - matching.length;
    const fragment = document.createDocumentFragment();
    for (const entry of matching.slice(-DOM_LIMIT)) fragment.append(this._lineNode(entry));
    this.shown = Math.min(matching.length, DOM_LIMIT);
    if (this.shown === 0) {
      this._renderEmpty();
    } else {
      this.view.append(fragment);
      this.view.scrollTop = this.view.scrollHeight;
      this.pinnedToBottom = true;
    }
    this._updateFooter();
  }

  clear() {
    this.store.logs = [];
    this.pending = [];
    this.shown = 0;
    this.suppressed = 0;
    this._renderEmpty();
    this._updateFooter();
  }

  visibleText() {
    return this.store.logs
      .filter((entry) => this._matches(entry))
      .map((entry) => `${new Date(entry.t * 1000).toISOString()} ${entry.level.padEnd(8)} ${entry.name.padEnd(16)} ${entry.msg}`)
      .join('\n');
  }

  _updateFooter() {
    if (this.countLabel) {
      const total = this.store.logs.length;
      const hidden = this.suppressed > 0 ? ` · ${this.suppressed} filtered out` : '';
      this.countLabel.textContent = `${total.toLocaleString('en-GB')} lines${hidden}`;
    }
    if (this.statusLabel) {
      const bits = [];
      if (this.paused) bits.push(`paused, ${this.pending.length} buffered`);
      else if (!this.pinnedToBottom) bits.push('scrolled up, not following');
      this.statusLabel.textContent = bits.join(' · ');
    }
  }
}

export function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
