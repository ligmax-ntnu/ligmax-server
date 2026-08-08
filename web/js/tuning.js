/* The stabilisation tuning bench: the roll and pitch gains and trims.
 *
 * These are not dashboard settings. Every field here is an ArduPilot parameter on
 * the flight controller, read and written by the vessel
 * (`ligmax-pi/nodes/io_manager/tuning.py`) over MAVLink. So:
 *
 *   LOADING is automatic and continuous. Values arrive with the telemetry as
 *   `telemetry.tuning.values`, the same way the battery does, and a field the
 *   operator has not touched follows the boat. Open the page and the live tune is
 *   already in front of you; there is no "load" to press.
 *
 *   SAVING is one `set_param` command per changed field, queued and audited like
 *   any other command. ArduPilot's PARAM_SET is a set-and-save, so an acked write
 *   is in the flight controller's own storage — it survives a reboot of the
 *   Pixhawk, the Pi and this dashboard with nobody writing anything down.
 *
 * Which is why a field has three states worth distinguishing, and shows them:
 * what the boat has, what you have typed, and whether the boat has taken it. A
 * panel that only showed the third would be indistinguishable from one that had
 * lost the link, which is the failure this whole page exists to make visible.
 *
 * PROFILES are the one thing that lives on the ground station: a named snapshot
 * of the whole set. The vessel already persists each value, but a parameter reset
 * on the Pixhawk or a swapped flight controller does not respect that, and a
 * bench-tuned set is expensive to recreate. Applying one queues a `set_param` for
 * each value the boat does not already have.
 */

import {
  applyTuningProfile,
  deleteTuningProfile,
  fetchTuning,
  saveTuningProfile,
  sendCommand,
} from './api.js';
import * as fmt from './format.js';

/** How long a row stays marked "saved" before going quiet again. */
const SAVED_FOR_MS = 6000;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Trailing digits a step implies, so 0.005 does not render as 0.0050000001. */
function digitsFor(step) {
  if (!Number.isFinite(step) || step >= 1) return 0;
  return String(step).split('.')[1]?.length ?? 2;
}

function show(value, spec) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (spec.integer) return String(Math.round(value));
  return value.toFixed(digitsFor(spec.step));
}

export class TuningPanel {
  /**
   * `canSend` false leaves every field rendered and readable but disabled — a
   * read-only viewer should be able to see what the boat is tuned to, which is
   * half of what this panel is for. The server refuses `set_param` without an
   * admin cookie regardless.
   */
  constructor(elements, store, { notify, canSend = true }) {
    this.elements = elements;
    this.store = store;
    this.notify = notify;
    this.canSend = canSend;
    this.specs = [];
    this.rows = new Map();
    this.profiles = [];
    this.loaded = false;
  }

  async start() {
    try {
      const payload = await fetchTuning();
      this.specs = payload.params ?? [];
      this.groups = payload.groups ?? [];
      this.profiles = payload.profiles ?? [];
      this.storeError = payload.store_error ?? null;
      this._build();
      this.loaded = true;
      this.update();
    } catch (error) {
      this._fail(error.message);
    }
    return this;
  }

  _fail(message) {
    const { groups } = this.elements;
    if (!groups) return;
    groups.replaceChildren(
      element('p', 'card-note', `Tuning table unavailable: ${message}`)
    );
  }

  /* --- build ---------------------------------------------------------- */

  _build() {
    const container = this.elements.groups;
    if (!container) return;
    container.replaceChildren();

    for (const group of this.groups) {
      const specs = this.specs.filter((spec) => spec.group === group.key);
      if (!specs.length) continue;
      container.append(this._group(group, specs));
    }
    this._buildProfiles();
  }

  _group(group, specs) {
    const section = element('div', 'tune-group');
    const head = element('div', 'tune-group-head');
    head.append(element('h3', 'tune-group-title', group.title));
    head.append(element('code', 'tune-group-script', group.script));
    section.append(head);
    section.append(element('p', 'card-note card-note--tight', group.note));

    for (const spec of specs) {
      section.append(this._row(spec));
    }

    const foot = element('div', 'tune-group-foot');
    const save = element('button', 'btn btn--primary btn--sm', 'Save changed values');
    save.type = 'button';
    save.disabled = true;
    save.addEventListener('click', () => this._saveGroup(group.key));
    const revert = element('button', 'btn btn--outline btn--sm', 'Discard edits');
    revert.type = 'button';
    revert.disabled = true;
    revert.addEventListener('click', () => this._revertGroup(group.key));
    foot.append(save, revert);
    section.append(foot);

    this.groupButtons ??= new Map();
    this.groupButtons.set(group.key, { save, revert });
    return section;
  }

  _row(spec) {
    const row = element('div', 'tune-row');
    row.dataset.param = spec.name;
    if (spec.warn) row.dataset.warn = 'true';

    const label = element('div', 'tune-label');
    label.append(element('span', 'tune-label-text', spec.label));
    const name = element('code', 'tune-name', spec.name);
    label.append(name);

    let field;
    if (!spec.writable) {
      field = element('span', 'tune-readonly', '—');
    } else if (spec.kind === 'choice') {
      field = element('select', 'select select--sm tune-field');
      for (const option of spec.options ?? []) {
        const node = element('option', null, option.label);
        node.value = String(option.value);
        field.append(node);
      }
    } else {
      field = element('input', 'input input--sm tune-field');
      field.type = 'number';
      field.step = String(spec.step ?? 0.01);
      // An RC-channel field is "0, or 9..16" — a range with a hole in it, which
      // `min` cannot express, so the low end stays 0 and the server and the
      // vessel both refuse 1..8 with the reason spelled out.
      field.min = String(spec.kind === 'channel' ? 0 : spec.low);
      field.max = String(spec.high);
      field.inputMode = 'decimal';
    }
    if (field.tagName !== 'SPAN') {
      field.disabled = !this.canSend;
      field.addEventListener('input', () => this._touched(spec.name));
      field.addEventListener('change', () => this._touched(spec.name));
      field.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') this._saveOne(spec.name);
        if (event.key === 'Escape') this._revert(spec.name);
      });
    }

    const live = element('span', 'tune-live', '—');
    live.title = 'What the flight controller says this parameter is right now';
    const flag = element('span', 'tune-flag');
    flag.hidden = true;
    // `dirty`   the operator has typed something not yet sent
    // `wanted`  a value that has been sent and not yet come back in telemetry
    // `pending` the command id it went as, so the vessel's own verdict lands here

    const unit = element('span', 'tune-unit', spec.unit ?? '');

    // Order does not decide the layout - .tune-row places every cell explicitly -
    // but it does decide the tab order and what a screen reader reads.
    row.append(label, field, unit, live, flag);
    if (spec.help) row.append(element('span', 'tune-help', spec.help));
    this.rows.set(spec.name, {
      spec, row, field, live, flag,
      dirty: false, wanted: null, pendingId: null, savedAt: 0,
    });
    return row;
  }

  _buildProfiles() {
    const container = this.elements.profiles;
    if (!container) return;
    container.replaceChildren();

    const select = element('select', 'select select--sm');
    select.id = 'tune-profile-select';
    this._profileSelect = select;
    this._fillProfiles();

    const apply = element('button', 'btn btn--outline btn--sm', 'Apply to vessel');
    apply.type = 'button';
    apply.disabled = !this.canSend;
    apply.addEventListener('click', () => this._apply());

    const remove = element('button', 'btn btn--outline btn--sm', 'Delete');
    remove.type = 'button';
    remove.disabled = !this.canSend;
    remove.addEventListener('click', () => this._delete());

    const save = element('button', 'btn btn--outline btn--sm', 'Save current as…');
    save.type = 'button';
    save.disabled = !this.canSend;
    save.addEventListener('click', () => this._saveProfile());

    const row = element('div', 'tune-profile-row');
    const field = element('label', 'field field--inline');
    field.append(element('span', 'field-label', 'Saved tune'));
    field.append(select);
    row.append(field, apply, save, remove);
    container.append(row);

    const note = element('p', 'card-note card-note--tight');
    note.textContent = this.storeError
      ? `Profiles cannot be read or written: ${this.storeError}`
      : 'Saved on the ground station. The vessel keeps each value in the flight ' +
        'controller’s own storage already — this is the copy that survives a ' +
        'parameter reset, a firmware flash or a swapped Pixhawk.';
    container.append(note);
  }

  _fillProfiles() {
    const select = this._profileSelect;
    if (!select) return;
    const chosen = select.value;
    select.replaceChildren();
    if (!this.profiles.length) {
      const empty = element('option', null, 'nothing saved yet');
      empty.value = '';
      select.append(empty);
      select.disabled = true;
      return;
    }
    select.disabled = false;
    for (const profile of this.profiles) {
      const option = element(
        'option',
        null,
        `${profile.name} · ${profile.count} value${profile.count === 1 ? '' : 's'}`
      );
      option.value = profile.name;
      select.append(option);
    }
    if (chosen && this.profiles.some((p) => p.name === chosen)) select.value = chosen;
  }

  /* --- live values ---------------------------------------------------- */

  /** Called on every telemetry frame and every command update.
   *
   *  Untouched fields follow the boat — that is the whole of "load", and it is
   *  why there is no Load button. A field with a write in flight keeps showing
   *  what was sent until the vessel reports that value back, because reverting to
   *  the old number for the second the round trip takes reads as a rejection.
   */
  update() {
    if (!this.loaded) return;
    const block = this.store.telemetry('tuning') ?? {};
    const values = block.values ?? {};
    const missing = new Set(block.missing ?? []);
    const now = Date.now();

    for (const [name, view] of this.rows) {
      const value = values[name];
      const known = typeof value === 'number' && Number.isFinite(value);
      view.live.textContent = known ? show(value, view.spec) : '—';
      view.row.dataset.known = String(known);

      // Always run: it is what clears a settled write and reports a refused one.
      let flag = this._resolvePending(view, value, known);
      if (view.dirty) {
        flag = ['warn', 'not saved'];
      } else if (!flag && view.savedAt && now - view.savedAt < SAVED_FOR_MS) {
        flag = ['ok', 'saved'];
      } else if (!flag && missing.has(name)) {
        flag = ['danger', 'not on the autopilot'];
      }
      this._flag(view, flag?.[0] ?? null, flag?.[1] ?? null);

      if (!view.spec.writable) {
        view.field.textContent = known ? show(value, view.spec) : '—';
      } else if (view.dirty) {
        // Being edited. Leave it exactly as typed.
      } else if (view.wanted !== null) {
        this._setField(view, view.wanted);
      } else {
        this._setField(view, known ? value : null);
      }
    }

    this._updateStatus(block);
    this._syncGroupButtons();
  }

  _setField(view, value) {
    if (document.activeElement === view.field) return;
    if (view.field.tagName === 'SELECT') {
      if (value !== null) view.field.value = String(Math.round(value));
      return;
    }
    view.field.value = value === null ? '' : show(value, view.spec);
  }

  /**
   * Follow a write through to its end and hand back the flag to draw.
   *
   * There are two separate confirmations and both matter. The command ack says
   * the vessel accepted the write and the autopilot echoed the value it stored;
   * the value appearing in `telemetry.tuning.values` says the panel is now
   * showing the boat rather than showing hope. Only the second clears `wanted`.
   */
  _resolvePending(view, live, known) {
    if (view.pendingId) {
      const command = this.store.commands?.find((item) => item.id === view.pendingId);
      const status = command?.status ?? 'queued';
      if (status === 'queued' || status === 'delivered') return ['warn', 'sending…'];
      view.pendingId = null;
      if (status === 'acked') {
        view.savedAt = Date.now();
      } else {
        // The vessel refused it, or never answered. Its own words are more
        // useful than anything this panel could infer, so they go in the notice.
        view.wanted = null;
        this.notify(
          `${view.spec.name} was not saved: ${command?.result ?? status}`,
          'error',
          14000
        );
        return ['danger', status === 'expired' ? 'no reply' : 'refused'];
      }
    }
    if (view.wanted !== null) {
      if (known && Math.abs(live - view.wanted) < 1e-9) {
        view.wanted = null;
        return ['ok', 'saved'];
      }
      return ['warn', 'sent, waiting'];
    }
    return null;
  }

  _flag(view, level, text) {
    if (!text) {
      view.flag.hidden = true;
      delete view.row.dataset.state;
      return;
    }
    view.flag.hidden = false;
    view.flag.textContent = text;
    view.flag.dataset.level = level;
    view.row.dataset.state = level;
  }

  _updateStatus(block) {
    const label = this.elements.status;
    if (!label) return;
    const known = block.known ?? 0;
    const of = block.of ?? this.specs.length;
    const parts = [];

    if (!Object.keys(block).length) {
      parts.push('The vessel is not reporting any tuning — no telemetry, or an ' +
        'io_manager too old to send it.');
    } else if (block.loading) {
      parts.push(`Reading from the autopilot… ${known} of ${of}.`);
    } else {
      parts.push(`${known} of ${of} read from the autopilot.`);
    }
    if (block.missing?.length) {
      const script = block.slider_script === false
        ? ' — battery_slider.lua looks like it has never run, which is what makes ' +
          'its BSLD_ parameters absent'
        : '';
      parts.push(`Missing: ${block.missing.join(', ')}${script}.`);
    }
    if (block.pending) parts.push(`Writing ${block.pending}…`);
    if (block.queued) parts.push(`${block.queued} write(s) queued.`);
    if (block.last_write) parts.push(block.last_write);
    if (block.last_error) parts.push(`Last error: ${block.last_error}`);
    label.textContent = parts.join(' ');
  }

  /* --- editing -------------------------------------------------------- */

  _touched(name) {
    const view = this.rows.get(name);
    if (!view) return;
    const live = this.store.telemetry(`tuning.values.${name}`);
    const typed = Number.parseFloat(view.field.value);
    // Typing a value back to what the boat already has un-dirties the row, so
    // "Save changed values" never spends a command on a no-op.
    view.dirty = !(
      Number.isFinite(typed) &&
      typeof live === 'number' &&
      Math.abs(typed - live) < 1e-9
    );
    if (view.dirty) view.savedAt = 0;
    this._flag(view, view.dirty ? 'warn' : null, view.dirty ? 'not saved' : null);
    this._syncGroupButtons();
  }

  _syncGroupButtons() {
    if (!this.groupButtons) return;
    for (const [key, buttons] of this.groupButtons) {
      const dirty = [...this.rows.values()].some(
        (view) => view.spec.group === key && view.dirty
      );
      buttons.save.disabled = !dirty || !this.canSend;
      buttons.revert.disabled = !dirty;
    }
  }

  _revert(name) {
    const view = this.rows.get(name);
    if (!view) return;
    view.dirty = false;
    view.wanted = null;
    const live = this.store.telemetry(`tuning.values.${name}`);
    this._setField(view, typeof live === 'number' && Number.isFinite(live) ? live : null);
    this._flag(view, null, null);
    this._syncGroupButtons();
  }

  _revertGroup(key) {
    for (const [name, view] of this.rows) {
      if (view.spec.group === key && view.dirty) this._revert(name);
    }
  }

  async _saveGroup(key) {
    const pending = [...this.rows.entries()].filter(
      ([, view]) => view.spec.group === key && view.dirty
    );
    if (!pending.length) return;

    // Anything that makes the boat move on its own says so before it goes.
    const moving = pending.filter(([, view]) => view.spec.warn);
    const summary = pending
      .map(([name, view]) => `${name} = ${view.field.value}`)
      .join('\n');
    let question = `Save ${pending.length} value(s) to the flight controller?\n\n${summary}`;
    if (moving.length) {
      question +=
        '\n\nThese take effect immediately and are stored on the autopilot: ' +
        moving.map(([name]) => name).join(', ') +
        '. A ride-height trim keeps the amas moving for as long as it is set, ' +
        'including after a reboot.';
    }
    if (!window.confirm(question)) return;

    for (const [name] of pending) {
      // Sequentially, so the audit trail reads in the order they were typed and a
      // refusal stops the rest of the group rather than racing it.
      const ok = await this._send(name);
      if (!ok) break;
    }
  }

  async _saveOne(name) {
    const view = this.rows.get(name);
    if (!view?.dirty) return;
    if (view.spec.warn) {
      if (!window.confirm(
        `Save ${name} = ${view.field.value}? It takes effect immediately and is ` +
        'stored on the autopilot, so it stays set across a reboot.'
      )) return;
    }
    await this._send(name);
  }

  /** Queue one `set_param`. The value is not "saved" until the vessel acks it —
   *  the command row in the audit list is where that shows up. */
  async _send(name) {
    const view = this.rows.get(name);
    if (!view) return false;
    if (!this.canSend) {
      this.notify('Read-only session. Open the console with ?key=… to tune.', 'warn');
      return false;
    }
    const value = Number.parseFloat(view.field.value);
    if (!Number.isFinite(value)) {
      this.notify(`${name} needs a number.`, 'warn');
      return false;
    }
    this._flag(view, 'warn', 'sending…');
    let payload;
    try {
      payload = await sendCommand('set_param', { name, value });
    } catch (error) {
      // Refused by the server, so it never reached the queue: the range check and
      // the whitelist both live there as well as on the vessel.
      this._flag(view, 'danger', 'refused');
      this.notify(`${name}: ${error.message}`, 'error');
      return false;
    }
    view.dirty = false;
    view.wanted = value;
    view.pendingId = payload?.command?.id ?? null;
    this._flag(view, 'warn', 'sending…');
    this._syncGroupButtons();
    return true;
  }

  /** Ask the vessel to re-read every parameter off the autopilot. */
  async reload() {
    if (!this.canSend) {
      this.notify('Read-only session — values still follow the vessel on their own.', 'info');
      return;
    }
    try {
      await sendCommand('get_params', {});
      this.notify('Vessel asked to re-read the tuning from the autopilot.', 'info');
    } catch (error) {
      this.notify(error.message, 'error');
    }
  }

  /* --- profiles ------------------------------------------------------- */

  async _saveProfile() {
    const name = window.prompt(
      'Save the tuning currently on the vessel under what name?',
      `bench ${fmt.clockTime(Date.now() / 1000, { millis: false })}`
    );
    if (!name) return;
    try {
      const payload = await saveTuningProfile(name.trim());
      this.profiles = payload.profiles ?? this.profiles;
      this._fillProfiles();
      this.notify(
        `Saved '${payload.profile.name}' with ${payload.profile.count} value(s).`,
        'ok'
      );
    } catch (error) {
      this.notify(error.message, 'error');
    }
  }

  async _apply() {
    const name = this._profileSelect?.value;
    if (!name) return;
    if (!window.confirm(
      `Apply '${name}' to the vessel? Every value that differs is written to the ` +
      'flight controller, one audited command each.'
    )) return;
    try {
      const payload = await applyTuningProfile(name);
      const queued = payload.queued?.length ?? 0;
      this.notify(
        queued
          ? `'${name}': ${queued} value(s) queued for the vessel.`
          : `'${name}': the vessel already has every value.`,
        queued ? 'ok' : 'info'
      );
      for (const skipped of payload.skipped ?? []) {
        this.notify(`Skipped ${skipped}`, 'warn', 10000);
      }
    } catch (error) {
      this.notify(error.message, 'error');
    }
  }

  async _delete() {
    const name = this._profileSelect?.value;
    if (!name) return;
    if (!window.confirm(`Delete the saved tune '${name}'? This cannot be undone.`)) return;
    try {
      const payload = await deleteTuningProfile(name);
      this.profiles = payload.profiles ?? [];
      this._fillProfiles();
      this.notify(`Deleted '${name}'.`, 'info');
    } catch (error) {
      this.notify(error.message, 'error');
    }
  }
}
