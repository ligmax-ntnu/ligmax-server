/* Admin controls and the command audit list.
 *
 * The emergency stop needs a deliberate press-and-hold rather than a click: a
 * stray click on a dashboard is plausible, an 800 ms hold is not. Everything
 * else that cannot be trivially undone asks for confirmation.
 */

import { sendCommand } from './api.js';
import * as fmt from './format.js';

const HOLD_MS = 800;

const QUICK_COMMANDS = [
  { name: 'hold', label: 'Hold', variant: 'outline' },
  { name: 'resume', label: 'Resume', variant: 'primary' },
  { name: 'arm', label: 'Arm', variant: 'outline', confirm: 'Arm propulsion?' },
  { name: 'disarm', label: 'Disarm', variant: 'outline' },
  { name: 'estop_clear', label: 'Clear E-stop', variant: 'outline', confirm: 'Clear the emergency stop? Propulsion power will be restored.' },
  { name: 'home_battery', label: 'Home battery', variant: 'outline', confirm: 'Re-home the battery rail? The slider hunts for its centre endstop and stops holding pitch trim until it finds it.' },
  { name: 'clear_waypoints', label: 'Clear route', variant: 'outline' },
];

export class CommandPanel {
  /**
   * `canSend` false renders the whole panel disabled rather than hiding it, so a
   * read-only viewer can see what an operator would be able to do — which is the
   * point of the control page — without being able to do any of it. The server
   * refuses these commands without an admin cookie regardless; this is only so
   * the UI does not offer something that will bounce.
   */
  constructor(elements, store, { notify, canSend = true }) {
    this.elements = elements;
    this.store = store;
    this.notify = notify;
    this.canSend = canSend;
    this.pickingGoto = false;
    this.onGotoArmed = null;

    this._buildQuickCommands();
    this._bindEstop();
    this._bindMode();
    this._bindSpeed();
    this._bindGoto();
    this._bindRaw();
    if (!canSend) this._lockDown();
  }

  async _send(name, args = {}) {
    if (!this.canSend) {
      this.notify('Read-only session. Open the console with ?key=… to send commands.', 'warn');
      return false;
    }
    try {
      await sendCommand(name, args);
      return true;
    } catch (error) {
      this.notify(error.message, 'error');
      return false;
    }
  }

  /** Disable every control in the panel and say why once. */
  _lockDown() {
    const { estopButton } = this.elements;
    for (const element of Object.values(this.elements)) {
      if (!element) continue;
      for (const node of element.querySelectorAll?.('button, input, select, textarea') ?? []) {
        node.disabled = true;
      }
      if (element.tagName && /^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(element.tagName)) {
        element.disabled = true;
      }
    }
    if (estopButton) {
      estopButton.setAttribute('aria-disabled', 'true');
      const hint = estopButton.querySelector('.estop-btn-hint');
      if (hint) hint.textContent = 'Operator key required';
    }
  }

  _buildQuickCommands() {
    const container = this.elements.quickCommands;
    if (!container) return;
    const available = this.store.session.commands ?? {};
    for (const spec of QUICK_COMMANDS) {
      if (!available[spec.name]) continue; // server does not offer it
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `btn btn--${spec.variant}`;
      button.textContent = spec.label;
      button.disabled = !this.canSend;
      button.addEventListener('click', () => {
        if (spec.confirm && !window.confirm(spec.confirm)) return;
        this._send(spec.name);
      });
      container.append(button);
    }
  }

  _bindEstop() {
    const button = this.elements.estopButton;
    if (!button) return;

    let timer = null;
    const cancel = () => {
      window.clearTimeout(timer);
      timer = null;
      button.classList.remove('is-armed');
    };

    const begin = (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      button.classList.add('is-armed');
      timer = window.setTimeout(async () => {
        cancel();
        await this._send('estop');
        this.notify('Emergency stop sent to the vessel.', 'error');
        if (navigator.vibrate) navigator.vibrate(120);
      }, HOLD_MS);
    };

    button.addEventListener('pointerdown', begin);
    button.addEventListener('pointerup', cancel);
    button.addEventListener('pointerleave', cancel);
    button.addEventListener('pointercancel', cancel);
    // Keyboard parity: hold Enter or Space.
    button.addEventListener('keydown', (event) => {
      if ((event.key === 'Enter' || event.key === ' ') && !timer) begin(event);
    });
    button.addEventListener('keyup', cancel);
    button.addEventListener('blur', cancel);
  }

  _bindMode() {
    this.elements.modeApply?.addEventListener('click', () => {
      const mode = this.elements.modeSelect.value;
      if (!mode) return;
      this._send('set_mode', { mode });
    });
  }

  _bindSpeed() {
    this.elements.speedApply?.addEventListener('click', () => {
      const value = Number.parseFloat(this.elements.speedLimit.value);
      if (!Number.isFinite(value)) {
        this.notify('Speed limit must be a number.', 'warn');
        return;
      }
      this._send('set_speed_limit', { value });
    });
  }

  _bindGoto() {
    const button = this.elements.gotoArm;
    button?.addEventListener('click', () => {
      this.setGotoArmed(!this.pickingGoto);
    });
  }

  setGotoArmed(on) {
    this.pickingGoto = on;
    const button = this.elements.gotoArm;
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
    button.textContent = on
      ? 'Click the map to send the vessel there — Esc to cancel'
      : 'Pick a go-to point on the map';
    this.onGotoArmed?.(on);
  }

  /** Called by the map when the operator clicks while go-to is armed. */
  async submitGoto([x, y]) {
    this.setGotoArmed(false);
    const blocked = window.confirm(
      `Send the vessel to grid ${x.toFixed(1)}, ${y.toFixed(1)} m?`
    );
    if (!blocked) return;
    const ok = await this._send('goto', { x, y });
    if (ok) this.notify(`Go-to ${x.toFixed(1)}, ${y.toFixed(1)} m queued.`, 'ok');
  }

  _bindRaw() {
    this.elements.rawSend?.addEventListener('click', () => {
      const text = this.elements.rawPayload.value.trim();
      if (!text) return;
      let payload;
      try {
        payload = JSON.parse(text);
      } catch (error) {
        this.notify(`Raw payload is not valid JSON: ${error.message}`, 'error');
        return;
      }
      this._send('raw', { payload });
    });
  }

  /** Keep the mode dropdown in step with what the vessel says it supports.
   *  A page may carry only the E-stop and no mode picker, so everything here is
   *  optional. */
  syncModes() {
    const select = this.elements.modeSelect;
    if (!select) return;
    const modes = this.store.state.available_modes ?? [];
    const current = this.store.state.mode;
    const signature = modes.join('|');
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      select.replaceChildren();
      if (!modes.length) {
        const option = document.createElement('option');
        option.textContent = 'vessel has not reported its modes';
        option.value = '';
        select.append(option);
      }
      for (const mode of modes) {
        const option = document.createElement('option');
        option.value = mode;
        option.textContent = mode;
        select.append(option);
      }
      if (current && modes.includes(current)) select.value = current;
    }
    // A read-only viewer still gets to see which modes the vessel offers; it just
    // stays disabled. Note the `!this.canSend` — without it this line would undo
    // `_lockDown()` every time a frame arrived.
    const usable = this.canSend && modes.length > 0;
    select.disabled = !usable;
    if (this.elements.modeApply) {
      this.elements.modeApply.disabled = !usable;
    }
  }
}

const STATUS_LABEL = {
  queued: 'queued',
  delivered: 'sent',
  acked: 'done',
  failed: 'failed',
  expired: 'no reply',
};

export function renderCommandHistory(list, commands) {
  if (!commands?.length) {
    list.replaceChildren();
    const empty = document.createElement('li');
    empty.className = 'cmd-empty';
    empty.textContent = 'No commands issued this session.';
    list.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const command of [...commands].reverse()) {
    const item = document.createElement('li');
    item.className = 'cmd-item';
    item.dataset.status = command.status;

    const time = document.createElement('span');
    time.className = 'cmd-time';
    time.textContent = fmt.clockTime(command.issued_at, { millis: false });

    const body = document.createElement('span');
    const name = document.createElement('span');
    name.className = 'cmd-name';
    name.textContent = command.name;
    body.append(name);

    if (command.args && Object.keys(command.args).length) {
      const args = document.createElement('span');
      args.className = 'cmd-args';
      args.textContent = ` ${Object.entries(command.args)
        .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
        .join(' ')}`;
      body.append(args);
    }

    const detail = [];
    if (command.result) detail.push(command.result);
    if (command.issued_by && command.issued_by !== 'operator') detail.push(`from ${command.issued_by}`);
    if (detail.length) {
      const result = document.createElement('span');
      result.className = 'cmd-result';
      result.textContent = detail.join(' · ');
      body.append(result);
    }

    const status = document.createElement('span');
    status.className = 'cmd-status';
    status.textContent = STATUS_LABEL[command.status] ?? command.status;

    item.append(time, body, status);
    fragment.append(item);
  }
  list.replaceChildren(fragment);
}
