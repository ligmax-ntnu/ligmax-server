/* The flight controller's own two switches: the safety button and the compass.
 *
 * Both are dockside actions rather than anything used under way, and both change
 * something on the Pixhawk rather than on the Pi — which is why they are one card
 * and not scattered through the command panel. The vessel side is
 * `ligmax-pi/nodes/io_manager/preflight.py`, and the two halves are named the
 * same on purpose.
 *
 * The one thing this panel must not do is lie about the safety switch.
 * ArduPilot does not report where the switch is — not in SYS_STATUS, not
 * anywhere — so the word shown here is *what the vessel last commanded and the
 * autopilot accepted*, and it goes back to "unknown" the moment the MAVLink link
 * drops or somebody could have walked up and pressed the button themselves.
 * `safety_switch_seen` is the flag that says which of those it is, and the
 * unknown state is rendered as unknown rather than as "inhibited", because an
 * operator who reads "outputs inhibited" puts their hands near a propeller.
 */

import { sendCommand } from './api.js';
import * as fmt from './format.js';

/* Below this speed over ground, course over ground is noise — a drifting hull
 * reports a heading made of GNSS jitter. The chip that offers COG as the
 * calibration heading is disabled under it rather than hidden, so the reason is
 * readable instead of mysterious. 0.5 m/s is about a knot. */
const COG_MIN_SOG = 0.5;

/* The three states, listed rather than inferred: a word arriving from the vessel
 * is looked up against this, and anything not on it is "unknown". Indexing an
 * object literal with an unvetted string is how `constructor` ends up rendered
 * as a state, and this is the one panel where the fallback must always be the
 * cautious reading. */
const SAFETY_STATES = ['off', 'on', 'unknown'];

const SAFETY_WORD = {
  off: 'Safety OFF — motor outputs are live',
  on: 'Safety ON — motor outputs inhibited',
  unknown: 'Safety switch state unknown',
};

const SAFETY_NOTE = {
  off: 'The vessel set this and the autopilot confirmed it. Anything armed can turn a propeller.',
  on: 'The vessel set this and the autopilot confirmed it. The autopilot will refuse to drive the outputs.',
  unknown:
    'Nothing has set it from here since the link came up, and ArduPilot does not ' +
    'report where the switch is. Treat the outputs as live.',
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export class PreflightPanel {
  /**
   * @param container  the element to build into
   * @param store      the shared telemetry store
   * `canSend` false renders everything disabled rather than hidden, for the same
   * reason the rest of the control page does: a read-only viewer should be able
   * to read the safety switch state, which is a measurement, without being able
   * to change it.
   */
  constructor(container, store, { notify, canSend = true }) {
    this.container = container;
    this.store = store;
    this.notify = notify;
    this.canSend = canSend;
    this._build();
    this.update();
  }

  get block() {
    return this.store.state.telemetry?.preflight ?? null;
  }

  _build() {
    const root = el('div', 'pf');

    /* --- the safety switch -------------------------------------------- */

    this._state = el('div', 'pf-state');
    this._state.dataset.state = 'unknown';
    this._stateWord = el('strong', 'pf-state-word', SAFETY_WORD.unknown);
    this._stateNote = el('span', 'pf-state-note', SAFETY_NOTE.unknown);
    this._state.append(this._stateWord, this._stateNote);

    const actions = el('div', 'pf-actions');
    this._safetyOn = el('button', 'btn btn--outline btn--sm', 'Safety ON — inhibit outputs');
    this._safetyOn.type = 'button';
    this._safetyOn.title =
      'Press the Pixhawk safety switch back on. The autopilot stops driving the ' +
      'motor outputs. This is the safe direction and asks nothing.';
    this._safetyOn.addEventListener('click', () => this.send('safety_on'));

    this._safetyOff = el('button', 'btn btn--danger btn--sm', 'Safety OFF — outputs live');
    this._safetyOff.type = 'button';
    this._safetyOff.title =
      'Release the Pixhawk safety switch, exactly as holding the button on the ' +
      'hull does. The motor outputs become live.';
    this._safetyOff.addEventListener('click', () => {
      if (
        !window.confirm(
          'Take the Pixhawk safety switch OFF?\n\n' +
            'The motor outputs become live. Anything already armed can turn a ' +
            'propeller the moment this is acked. Check that nobody has a hand in ' +
            'the water and that the boat is clear.'
        )
      ) {
        return;
      }
      this.send('safety_off');
    });

    actions.append(this._safetyOn, this._safetyOff);

    const safetyNote = el(
      'p',
      'card-note card-note--tight',
      'This does what walking up and holding the button on the hull does — and ' +
        'unlike the button it is not gated by BRD_SAFETYOPTION. The E-stop is a ' +
        'different thing entirely: it opens a relay in the power path, and this ' +
        'switch cannot substitute for it.'
    );

    root.append(this._state, actions, safetyNote);

    /* --- the compass --------------------------------------------------- */

    root.append(el('h3', 'pf-heading', 'Compass — large-vehicle calibration'));

    const row = el('div', 'command-row');
    const field = el('label', 'field');
    field.append(el('span', 'field-label', 'True heading (°)'));
    this._heading = el('input', 'input');
    this._heading.type = 'number';
    this._heading.min = '0';
    this._heading.max = '360';
    this._heading.step = '0.5';
    this._heading.placeholder = 'e.g. 137';
    field.append(this._heading);

    this._calibrate = el('button', 'btn btn--outline btn--sm', 'Calibrate');
    this._calibrate.type = 'button';
    this._calibrate.addEventListener('click', () => this._submit());
    this._heading.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') this._submit();
    });
    row.append(field, this._calibrate);

    const hints = el('div', 'pf-hints');
    this._useCog = el('button', 'chip', 'Use course over ground');
    this._useCog.type = 'button';
    this._useCog.title =
      'Course over ground comes from the GNSS, not the compass, so it is a ' +
      'legitimate reference — but only with the boat making way in a straight ' +
      'line. It is the crab angle away from heading in any current.';
    this._useCog.addEventListener('click', () => {
      const cog = this.store.telemetry('motion.cog_deg');
      if (!Number.isFinite(cog)) return;
      this._heading.value = cog.toFixed(1);
    });
    this._compare = el('span', 'pf-compare');
    hints.append(this._useCog, this._compare);

    this._last = el('p', 'card-note card-note--tight pf-last');

    const compassNote = el(
      'p',
      'card-note card-note--tight',
      'Point the hull along a heading you know from something that is not this ' +
        'compass — a handheld bearing, a known line on the dock, or the course ' +
        'over ground with the boat making way — and send that heading. The ' +
        'autopilot solves the offsets against the world magnetic model for where ' +
        'it is standing, so it needs a GNSS fix, and it refuses while armed. The ' +
        'tumble calibration every quadcopter uses needs the vehicle rolled through ' +
        'all three axes and is not available to a hull in the water.'
    );

    root.append(row, hints, this._last, compassNote);

    if (!this.canSend) {
      for (const node of root.querySelectorAll('button, input')) node.disabled = true;
    }

    this.container.replaceChildren(root);
    this._root = root;
  }

  async send(name, args = {}) {
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

  _submit() {
    const heading = Number.parseFloat(this._heading.value);
    if (!Number.isFinite(heading)) {
      this.notify('Enter the vessel’s true heading in degrees first.', 'warn');
      return;
    }
    const wrapped = ((heading % 360) + 360) % 360;
    const compass = this.store.telemetry('motion.heading_deg');
    // The size of the correction is the one number worth showing before the
    // press: a 3° nudge and a 90° one are different decisions, and the second is
    // usually somebody typing magnetic where true was asked for, or reading the
    // handheld off the wrong end.
    const drift = Number.isFinite(compass)
      ? `\n\nThe compass currently reads ${compass.toFixed(1)}°, so this is a ` +
        `${Math.abs(((wrapped - compass + 540) % 360) - 180).toFixed(1)}° correction.`
      : '';
    if (
      !window.confirm(
        `Calibrate the compass against a true heading of ${wrapped.toFixed(1)}°?` +
          drift +
          '\n\nThis rewrites the compass offsets stored on the flight controller ' +
          'and survives every reboot. The heading must be true, not magnetic, and ' +
          'must not come from the compass being calibrated.'
      )
    ) {
      return;
    }
    this.send('compass_cal', { heading: wrapped });
  }

  update() {
    const block = this.block;
    // `safety_switch_seen` false means the word is a default rather than an
    // observation — a vessel that has not been asked, or one whose link dropped
    // since it was. Rendering that as "on" would be the one mistake this panel
    // exists to avoid, so the flag outranks the word.
    const reported = block?.safety_switch_seen ? String(block.safety_switch) : 'unknown';
    const state = SAFETY_STATES.includes(reported) ? reported : 'unknown';
    this._state.dataset.state = state;
    this._stateWord.textContent = SAFETY_WORD[state];

    let note = SAFETY_NOTE[state];
    if (block?.safety_switch_at && state !== 'unknown') {
      note += ` Set at ${fmt.clockTime(block.safety_switch_at, { millis: false })}.`;
    }
    if (block?.pending) {
      note = `Waiting for the autopilot to answer: ${block.pending}.`;
    }
    this._stateNote.textContent = note;

    /* --- the compass hints --------------------------------------------- */

    const cog = this.store.telemetry('motion.cog_deg');
    const sog = this.store.telemetry('motion.sog');
    const compass = this.store.telemetry('motion.heading_deg');
    const cogUsable = Number.isFinite(cog) && Number.isFinite(sog) && sog >= COG_MIN_SOG;
    this._useCog.disabled = !this.canSend || !cogUsable;
    this._useCog.textContent = Number.isFinite(cog)
      ? `Use course over ground (${cog.toFixed(0)}°)`
      : 'Use course over ground';
    if (!cogUsable) {
      this._useCog.title = Number.isFinite(cog)
        ? 'The boat is not making way, so course over ground is GNSS jitter rather ' +
          'than a heading. Take a bearing instead, or drive a straight line first.'
        : 'No course over ground reported yet.';
    }
    this._compare.textContent = Number.isFinite(compass)
      ? `compass reads ${compass.toFixed(0)}°`
      : 'no compass heading reported';

    const swing = block?.compass_cal;
    const swungAt = Number(swing?.heading_deg);
    this._last.textContent = swing
      ? `Last calibrated at ${fmt.clockTime(swing.at, { millis: false })}` +
        (Number.isFinite(swungAt) ? ` against ${swungAt.toFixed(1)}° true` : '') +
        '. Check the heading on the chart against a known bearing before trusting it.'
      : 'No calibration has been run from this dashboard since the vessel started.';
  }
}
