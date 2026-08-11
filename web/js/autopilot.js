/* The autonomy panel, and the course editor that feeds it.
 *
 * Two things live here because they are two halves of one job: `AutopilotPanel`
 * says what the boat is doing and why and lets an operator interrupt it,
 * `CoursePlanner` is how the course gets in there in the first place.
 *
 * NJORD §11.4 scores "decision-making transparency" as its own line — a jury
 * member has to be able to read what the boat decided, and why, without anyone
 * explaining the screen. That is what the big status line is for, and it is why
 * `reason` is given more room than any number on this page: the boat writes a
 * plain-English sentence on every tick (`nodes/self_driving/behaviours/`), and
 * throwing it away in favour of a mode name would be discarding the part that
 * scores.
 *
 * The editor is built for one moment: 08:15 on a competition morning, on a
 * phone, on a dock, with the coordinates handed out at 08:00 and the first run
 * at 09:00. Hence paste-first — a textarea that swallows whatever the handout
 * looks like and shows back an editable table — rather than anything you have to
 * be sitting down to use. See `plan.js` for the parse rule.
 */

import { sendCommand } from './api.js';
import {
  describeRow,
  numericFields,
  parseCourse,
  roleList,
  rowToWaypoint,
  shapeOf,
  styleOfRole,
} from './plan.js';

/* Mode -> how loud. `blocked` is not a mode the vessel reports; it is the
 * separate `blocked` field, and it outranks everything because it is the answer
 * to "why will it not go", which is the question actually being asked when
 * somebody is staring at this panel. */
const MODE_LEVEL = {
  RUNNING: 'run',
  PAUSED: 'hold',
  BLOCKED: 'blocked',
  FINISHED: 'done',
  IDLE: 'idle',
};

const MODE_WORD = {
  RUNNING: 'Driving itself',
  PAUSED: 'Holding station',
  BLOCKED: 'Will not engage',
  FINISHED: 'Course complete',
  IDLE: 'Standing by',
};

/* The buttons, in the order they are reached for.
 *
 * None of them is disabled by mode. The vessel refuses what it cannot do and
 * says why in the ack, and that is a better answer than a greyed-out button that
 * cannot explain itself — while a guess made here about what is legal would be a
 * second, unsynchronised copy of the pilot's own rules. The one exception is the
 * confirm on Engage: it starts the clock and arms the boat.
 *
 * `Back` is NJORD §8.2's recovery — after the 20 s search window the team takes
 * over and re-enters behind the last passed waypoint — so it takes one tap and
 * asks nothing. */
const ACTIONS = [
  {
    name: 'autopilot_start',
    label: 'Engage',
    variant: 'primary',
    hint: 'Request GUIDED, arm, and start driving the course. The task timer starts here.',
    confirm:
      'Engage autonomy?\n\nThe vessel will request GUIDED, ARM ITSELF and begin ' +
      'driving the loaded course. The Njord task timer starts the moment it goes autonomous.',
  },
  {
    name: 'autopilot_stop',
    label: 'Disengage',
    variant: 'danger',
    hint: 'Stop driving, hold, and close the recording. Never behind a dialog.',
  },
  {
    name: 'autopilot_pause',
    label: 'Hold',
    variant: 'outline',
    hint: 'Stay where it is, keep the plan. Resume carries on from the same waypoint.',
    activeIn: 'PAUSED',
  },
  {
    name: 'autopilot_resume',
    label: 'Carry on',
    variant: 'outline',
    hint: 'Resume the plan from where it was held.',
    activeIn: 'RUNNING',
  },
  {
    name: 'autopilot_back',
    label: '← Back one',
    variant: 'outline',
    hint: 'Step the cursor back a waypoint — the §8.2 re-entry after a takeover.',
  },
  {
    name: 'autopilot_skip',
    label: 'Skip →',
    variant: 'outline',
    hint: 'Treat the current waypoint as passed and move to the next.',
  },
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* --- the panel -------------------------------------------------------- */

export class AutopilotPanel {
  /**
   * @param container  the element to build into
   * @param store      the shared telemetry store
   * @param compact    true on the overview page: the status line and the plan
   *                   progress, but none of the engineering small print
   */
  constructor(container, store, { notify, canSend = true, compact = false }) {
    this.container = container;
    this.store = store;
    this.notify = notify;
    this.canSend = canSend;
    this.compact = compact;
    this._signature = null;
    this._build();
  }

  get block() {
    return this.store.state.telemetry?.autopilot ?? null;
  }

  _build() {
    const root = el('div', 'ap');
    root.dataset.level = 'none';

    // -- the sentence -----------------------------------------------------
    const headline = el('div', 'ap-headline');
    this._modeWord = el('span', 'ap-mode', 'No autonomy node');
    this._stuck = el('span', 'ap-stuck', 'STUCK');
    this._stuck.hidden = true;
    this._stuck.title =
      'The pilot has detected no progress. NJORD §8.2 gives a 20 second ' +
      'autonomous search window before the team must take over by remote and ' +
      're-enter behind the last passed waypoint.';
    headline.append(this._modeWord, this._stuck);

    this._reason = el('p', 'ap-reason', 'Waiting for the autonomy node to report in.');
    this._blocked = el('p', 'ap-blocked');
    this._blocked.hidden = true;

    root.append(headline, this._reason, this._blocked);

    // -- the speed in force ------------------------------------------------
    //
    // Second most-misdiagnosed thing on this boat under time pressure, after
    // "why will it not engage": a boat that is inexplicably slow. It is almost
    // always a speed somebody set two minutes ago and forgot, and without this
    // line the only symptom is a boat that crawls. So the figure the vessel says
    // is in force is stated here, always — and it is stated loudly when it is
    // slower than the tuned default, which is the case worth noticing.
    //
    // **There is no toggle here any more.** Careful mode and the three run
    // profiles are gone: there is one speed, it is set with the Speed field in
    // the command panel below (`set_speed_limit`), and it covers the hand-flown
    // go-to, an AUTO mission and every autonomous behaviour including a berth
    // approach. Two of the old chips never worked at all — `run_profile` was not
    // in the vessel's forwarding list, so every press came back "not
    // implemented" — which is its own argument for one control that does.
    this._speed = el('div', 'ap-speed');
    this._speedText = el('span', 'ap-speed-text');
    this._speed.append(this._speedText);
    root.append(this._speed);

    // The cardinal alternation prior. Off by default, and the label says which
    // way round it is rather than only lighting up, because a switched-on
    // inference the operator has forgotten about is the one thing here that could
    // put the boat on the wrong side of a mark.
    this._switches = el('div', 'ap-speed');
    this._alternation = el('button', 'chip chip--toggle', 'Alternation prior: off');
    this._alternation.type = 'button';
    this._alternation.disabled = !this.canSend;
    this._alternation.title =
      'When the camera never commits to which cardinal a mark is, pass it on the ' +
      'opposite side to the mark before it - marks in a channel alternate, so two ' +
      'of the same hand in a row would constrain nothing. It is a guess, it never ' +
      'overrides a committed camera vote, and it says on this panel what it ' +
      'concluded and why. Switch it on only if the survey shows the cardinals ' +
      'never resolved.';
    this._alternation.addEventListener('click', () => {
      const on = Boolean(this.block?.commander?.alternation);
      this.send('alternation', { on: !on });
    });
    this._switches.append(this._alternation);
    root.append(this._switches);

    // -- progress ---------------------------------------------------------
    this._progress = el('div', 'ap-progress');
    this._progressBar = el('div', 'ap-bar');
    this._progressFill = el('span', 'ap-bar-fill');
    this._progressBar.append(this._progressFill);
    this._progressText = el('div', 'ap-progress-text', 'No course loaded.');
    this._progress.append(this._progressText, this._progressBar);
    root.append(this._progress);

    // -- what it can see --------------------------------------------------
    this._sees = el('p', 'ap-sees');
    root.append(this._sees);

    // -- buttons ----------------------------------------------------------
    this._buttons = new Map();
    const actions = el('div', 'ap-actions');
    for (const spec of ACTIONS) {
      const button = el('button', `btn btn--${spec.variant} btn--sm`, spec.label);
      button.type = 'button';
      button.title = spec.hint;
      button.disabled = !this.canSend;
      button.addEventListener('click', () => {
        if (spec.confirm && !window.confirm(spec.confirm)) return;
        this.send(spec.name);
      });
      this._buttons.set(spec.name, button);
      actions.append(button);
    }
    root.append(actions);

    // -- engineering small print -----------------------------------------
    if (!this.compact) {
      this._detail = el('dl', 'ap-detail');
      root.append(this._detail);

      const extras = el('div', 'ap-extras');
      for (const [name, label, confirm] of [
        ['record_start', 'Record', null],
        ['record_stop', 'Stop recording', null],
        [
          'forget_world',
          'Clear what it has seen',
          'Clear the world model? Every mark the boat is currently tracking is ' +
            'dropped. This is what you do between tasks, not during one.',
        ],
        [
          'clear_plan',
          'Forget the course',
          'Forget the loaded course? The boat stops and the plan is discarded.',
        ],
      ]) {
        const chip = el('button', 'chip', label);
        chip.type = 'button';
        chip.disabled = !this.canSend;
        chip.addEventListener('click', () => {
          if (confirm && !window.confirm(confirm)) return;
          this.send(name);
        });
        extras.append(chip);
      }
      root.append(extras);
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

  update() {
    const block = this.block;

    if (!block) {
      // Two very different faults look identical without saying so: the autonomy
      // node not running, and the node bus between it and io_manager being
      // broken. `telemetry.autopilot_bridge` is what tells them apart, and it is
      // published by the io_manager end, so it survives the node it describes.
      const bridge = this.store.telemetry('autopilot_bridge.state');
      this._root.dataset.level = 'none';
      this._modeWord.textContent = 'No autonomy node';
      this._reason.textContent = bridge
        ? `The io_manager bridge says "${bridge}", but nothing is publishing an ` +
          'autopilot block — the self_driving node is probably not running.'
        : 'Nothing on this vessel is reporting an autopilot state.';
      this._blocked.hidden = true;
      this._stuck.hidden = true;
      this._progressText.textContent = 'No course loaded.';
      this._progressFill.style.width = '0%';
      this._sees.textContent = '';
      // Nothing is reporting a ceiling, so claiming one would be inventing it.
      this._speed.hidden = true;
      if (this._detail) this._detail.replaceChildren();
      return;
    }
    this._speed.hidden = false;

    const mode = String(block.mode ?? 'IDLE').toUpperCase();
    const blocked = block.blocked ?? null;
    const level = blocked ? 'blocked' : MODE_LEVEL[mode] ?? 'idle';

    this._root.dataset.level = level;
    this._modeWord.textContent = MODE_WORD[mode] ?? mode;
    this._reason.textContent = block.reason || '—';

    this._stuck.hidden = !block.stuck;

    if (blocked) {
      this._blocked.hidden = false;
      this._blocked.textContent = `Refusing to drive: ${blocked}`;
    } else {
      this._blocked.hidden = true;
    }

    this._updateProgress(block);
    this._updateSpeed(block);
    this._updateSees(block);
    this._updateActions(mode);
    if (this._detail) this._updateDetail(block);
  }

  /**
   * The speed in force, as the vessel reports it.
   *
   * `speed_ms`/`speed_kn` is the operator's one setting — what a leg runs at and
   * the ceiling every behaviour plans under, docking included. `speed_limit_kn`
   * is NJORD's 5 knots, which nothing can raise. Both are worth showing; only the
   * first is worth shouting about, and only when it is slower than the boat's
   * tuned default, because that is the state that reads as a broken boat.
   */
  _updateSpeed(block) {
    const commander = block.commander ?? {};
    const speed = commander.speed_kn ?? commander.speed_ceiling_kn;
    const limit = commander.speed_limit_kn;
    // Roughly the 1.2 m/s default in knots. Below this the boat is deliberately
    // being crept along and the panel should say so in the loud colour.
    const slow = Number.isFinite(speed) && speed < 2.2;

    this._speed.dataset.careful = String(slow);
    if (Number.isFinite(speed) && Number.isFinite(limit)) {
      this._speedText.textContent = slow
        ? `Held to ${speed.toFixed(2)} kn — everything, docking included`
        : `Speed ${speed.toFixed(2)} kn of the ${limit.toFixed(1)} kn limit`;
    } else if (Number.isFinite(speed)) {
      this._speedText.textContent = `Speed ${speed.toFixed(2)} kn`;
    } else {
      this._speedText.textContent = '';
    }

    const alternation = Boolean(commander.alternation);
    this._alternation.classList.toggle('is-on', alternation);
    this._alternation.setAttribute('aria-pressed', String(alternation));
    this._alternation.textContent = alternation
      ? 'Alternation prior: ON'
      : 'Alternation prior: off';
  }

  _updateProgress(block) {
    const plan = block.plan ?? null;
    if (!plan || !plan.waypoints) {
      this._progressText.textContent = 'No course loaded — send one from the course editor.';
      this._progressFill.style.width = '0%';
      this._progressFill.style.background = '';
      return;
    }

    const total = plan.waypoints;
    const index = Number.isFinite(plan.index) ? plan.index : 0;
    const done = plan.finished ? total : Math.min(index, total);
    this._progressFill.style.width = `${total ? (100 * done) / total : 0}%`;

    const style = styleOfRole(plan.role);
    this._progressFill.style.background = style.colour;

    this._progressText.replaceChildren();
    if (plan.finished) {
      this._progressText.append(
        el('strong', null, `${plan.name}: all ${total} waypoints done`)
      );
      return;
    }

    const chip = el('span', 'ap-role', style.short);
    chip.style.setProperty('--role', style.colour);

    this._progressText.append(
      el('strong', null, `${index + 1} of ${total}`),
      document.createTextNode(' · '),
      el('span', 'ap-wp', plan.current ?? '—'),
      document.createTextNode(' '),
      chip
    );

    const distance = block.distance_to_waypoint;
    if (Number.isFinite(distance)) {
      const bearing = block.bearing_to_waypoint;
      this._progressText.append(
        document.createTextNode(
          ` · ${distance.toFixed(1)} m away` +
            (Number.isFinite(bearing) ? `, bearing ${bearing.toFixed(0)}°` : '')
        )
      );
    }

    // The §8.2 re-entry point. Worth its own words rather than a field name: it
    // is what an operator needs the instant something has gone wrong, and it is
    // the one number that says where to put the boat back.
    if (plan.last_passed) {
      this._progressText.append(
        el('span', 'ap-passed', ` last passed ${plan.last_passed}`)
      );
    }
  }

  _updateSees(block) {
    const sees = block.sees;
    const perception = block.perception ?? {};
    const parts = [];
    if (sees) parts.push(sees);
    if (Number.isFinite(perception.front_clusters) || Number.isFinite(perception.aft_clusters)) {
      parts.push(
        `${perception.front_clusters ?? 0} front + ${perception.aft_clusters ?? 0} aft clusters`
      );
    }
    // What the boat is remembering rather than seeing. Between the two attempts
    // at one task this is the single most useful sentence on the screen — "it is
    // starting with 7 marks it already knows" — and it is the only way to see
    // that the survey actually loaded rather than silently failing to.
    if (perception.restored > 0) {
      parts.push(`${perception.restored} restored from the last attempt`);
    }
    if (perception.remembered > 0) {
      parts.push(`${perception.remembered} remembered, not in view`);
    }
    if (perception.edge && perception.edge !== 'connected') parts.push(perception.edge);
    this._sees.textContent = parts.length ? `Sees: ${parts.join(' · ')}` : '';
  }

  _updateActions(mode) {
    for (const spec of ACTIONS) {
      const button = this._buttons.get(spec.name);
      if (!button) continue;
      // Marked, never disabled — see the note on ACTIONS.
      button.classList.toggle('is-on', Boolean(spec.activeIn) && spec.activeIn === mode);
    }
  }

  _updateDetail(block) {
    const rows = [];
    if (block.behaviour) rows.push(['Behaviour', block.behaviour]);
    if (block.phase) rows.push(['Phase', String(block.phase)]);

    // The parking space, for the operator tuning the depth offset. The chart draws
    // the same numbers as a picture; this is where you read them off. `x.xx m in`
    // is how deep the dot sits measured from the lone line — the side of the space
    // with no partner — which is the figure the offset is set against.
    const parking = block.parking;
    if (parking?.seen) {
      const parts = [];
      if (Number.isFinite(parking.mouth_m) && Number.isFinite(parking.depth_m)) {
        parts.push(`${parking.mouth_m.toFixed(2)} x ${parking.depth_m.toFixed(2)} m`);
      }
      if (parking.depth_source === 'nominal') {
        // The lidar saw a depth it did not believe, so the configured figure is in
        // use. Worth saying: it is the difference between "the space is smaller
        // than we thought" and "we can only see half of it".
        parts.push('depth from config, not measured');
      }
      if (Number.isFinite(parking.dot_depth_m)) {
        parts.push(`dot ${parking.dot_depth_m.toFixed(2)} m in`);
      }
      if (Number.isFinite(parking.offset_m) && parking.offset_m !== 0) {
        parts.push(
          `offset ${parking.offset_m > 0 ? '+' : ''}${parking.offset_m.toFixed(2)} m` +
            (parking.offset_clamped ? ' (CLAMPED)' : '')
        );
      } else if (parking.offset_clamped) {
        parts.push('offset CLAMPED to the space');
      }
      if (Number.isFinite(parking.age_s) && parking.age_s > 2) {
        parts.push(`last seen ${parking.age_s.toFixed(0)} s ago`);
      }
      rows.push(['Parking space', parts.join(' · ')]);

      // The angle, separately, because it is half of what the countdown needs and
      // the half that is easy to miss: the boat can be on the dot to a hand's
      // width and still not be parked. An alongside park reaches its angle by
      // rotating 90 degrees *inside* the space, so this is also where the turn is
      // visible.
      if (Number.isFinite(parking.park_heading_deg)) {
        const angle = [`${parking.park_heading_deg.toFixed(0)}°`];
        if (Number.isFinite(parking.heading_error_deg)) {
          angle.push(`${parking.heading_error_deg.toFixed(0)}° off`);
        }
        rows.push(['Parking angle', angle.join(' · ')]);
      }
    } else if (parking) {
      // Not found yet, and the segment count is the number that says why: 0 means
      // the lidar is giving the fitter nothing, 1-2 means it is seeing part of the
      // space and is probably off its axis. See docs/testing.md 7j.
      const segments = Number.isFinite(parking.segments) ? parking.segments : 0;
      rows.push([
        'Parking space',
        `not found — ${segments} line(s) in view of the three it needs`,
      ]);
    }

    const commander = block.commander ?? {};
    if (commander.intent) {
      const speed = Number.isFinite(commander.speed_cmd)
        ? `, ${commander.speed_cmd.toFixed(2)} m/s`
        : '';
      rows.push(['Commanding', `${commander.intent}${speed}`]);
    }
    if (commander.engaged !== undefined) {
      rows.push(['Commander', commander.engaged ? 'engaged' : 'observing only']);
    }

    const recording = block.recording ?? {};
    const recordingParts = [];
    if (recording.recording) {
      recordingParts.push(recording.file ?? 'yes');
      if (Number.isFinite(recording.mb)) recordingParts.push(`${recording.mb.toFixed(1)} MB`);
    } else {
      recordingParts.push('not recording — attempt 2 will be blind');
    }
    // "The card filled up" is something the crew can act on from the dock and
    // cannot otherwise see. `truncated` means it already has.
    if (recording.truncated) recordingParts.push('TRUNCATED — the card ran out');
    if (Number.isFinite(recording.free_mb)) {
      recordingParts.push(`${recording.free_mb.toFixed(0)} MB free`);
    }
    rows.push(['Recording', recordingParts.join(' · ')]);

    const perception = block.perception ?? {};
    if (Number.isFinite(perception.confirmed)) {
      rows.push([
        'World model',
        `${perception.confirmed} confirmed of ${perception.tracks ?? '?'} tracked`,
      ]);
    }
    // Established marks are the ones promoted to permanent memory — 12 sightings
    // spread over 2 s — and they are what the survey is written from. Worth its
    // own row because "tracked" and "will survive a restart" are very different
    // claims about the same chart.
    if (Number.isFinite(perception.established)) {
      const memory = [`${perception.established} established`];
      if (perception.remembered > 0) memory.push(`${perception.remembered} out of view`);
      if (perception.restored > 0) memory.push(`${perception.restored} restored`);
      rows.push(['Memory', memory.join(', ')]);
    }

    // The survey on disk, which is what makes attempt two start with attempt
    // one's map. `marks: 0` after a run is the failure worth catching early.
    const survey = block.survey ?? {};
    if (survey.enabled !== undefined) {
      if (!survey.enabled) {
        rows.push(['Survey', 'disabled — attempt 2 starts blind']);
      } else {
        const saved = [`${survey.marks ?? 0} mark(s) saved`];
        if (Number.isFinite(survey.age_s)) saved.push(`${survey.age_s.toFixed(0)} s ago`);
        if (survey.last_error) saved.push(survey.last_error);
        rows.push(['Survey', saved.join(' · ')]);
      }
    }

    if (perception.edge) rows.push(['Jetson', perception.edge]);

    const bus = block.bus ?? {};
    if (Number.isFinite(bus.hz)) rows.push(['Node bus', `${bus.hz.toFixed(1)} Hz`]);

    // The io_manager end of the node bus, published by io_manager rather than by
    // the autonomy node, so it survives the node it describes. "The autonomy node
    // is not running" and "the bus between them is broken" look identical without
    // it, and they have different fixes.
    const bridge = this.store.telemetry('autopilot_bridge.state');
    if (bridge) rows.push(['Bridge', String(bridge)]);

    const signature = rows.map((row) => row.join('=')).join('|');
    if (this._detail.dataset.signature === signature) return;
    this._detail.dataset.signature = signature;

    this._detail.replaceChildren();
    for (const [key, value] of rows) {
      this._detail.append(el('dt', null, key), el('dd', null, String(value)));
    }
  }
}

/* --- the course editor ------------------------------------------------- */

export class CoursePlanner {
  constructor(elements, store, { notify, canSend = true }) {
    this.elements = elements;
    this.store = store;
    this.notify = notify;
    this.canSend = canSend;
    this.rows = [];
    this.roles = roleList(store.session);
    // Bounds from the server, not from this file - see plan.js's note on the
    // speed limit, which is the one that had already drifted.
    this.numericFields = numericFields(store.session);
    this.defaultRole = this.roles[0]?.name ?? 'transit';
    /** Called with the current rows whenever they change, so a chart can draw them. */
    this.onRowsChange = null;

    this._bind();
    this._render();
  }

  _bind() {
    const { paste, parseButton, addButton, clearButton, sendButton, loadButton, nameInput } =
      this.elements;

    parseButton?.addEventListener('click', () => this.readPaste());
    // Ctrl/Cmd-Enter in the box is the same as pressing the button: on a phone
    // on a dock, one fewer thing to aim at.
    paste?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        this.readPaste();
      }
    });

    addButton?.addEventListener('click', () => {
      this.rows.push({ name: '', lat: null, lon: null, role: this.defaultRole });
      this._changed();
    });

    clearButton?.addEventListener('click', () => {
      if (!this.rows.length) return;
      if (!window.confirm('Clear the course you are editing? Nothing has been sent yet.')) return;
      this.rows = [];
      this._changed();
    });

    sendButton?.addEventListener('click', () => this.send());
    loadButton?.addEventListener('click', () => this.loadFromVessel());
    nameInput?.addEventListener('input', () => this._updateSummary());

    if (!this.canSend) {
      for (const element of Object.values(this.elements)) {
        if (element?.tagName && /^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(element.tagName)) {
          element.disabled = true;
        }
      }
    }
  }

  /** Add points laid by clicking the chart. They arrive as grid metres. */
  addGridPoints(points, role = null) {
    for (const [x, y] of points) {
      this.rows.push({ name: '', x, y, role: role ?? this.defaultRole });
    }
    this._changed();
  }

  readPaste() {
    const text = this.elements.paste?.value ?? '';
    if (!text.trim()) {
      this.notify('Nothing to read — paste the coordinates first.', 'warn');
      return;
    }
    const { rows, errors } = parseCourse(text, { defaultRole: this.defaultRole });
    for (const error of errors) this.notify(error, 'warn', 12000);
    if (!rows.length) {
      this.notify('No coordinates found in that text.', 'error');
      return;
    }
    // Append rather than replace: a course often arrives in pieces, one task at
    // a time, and losing the rows already typed to a second paste would be its
    // own small disaster at 08:15.
    this.rows.push(...rows);
    this.elements.paste.value = '';
    this._changed();
    this.notify(`Read ${rows.length} waypoint${rows.length === 1 ? '' : 's'}.`, 'ok', 4000);
  }

  /**
   * Rebuild the editor from the course the boat is actually running.
   *
   * The vessel echoes its plan back as the chart's reference layer, roles and
   * all, in grid metres. So this is the honest answer to "what is loaded" —
   * read off the boat rather than off whatever this tab last sent, which may be
   * a different operator's tab, or a reload ago.
   */
  loadFromVessel() {
    const reference = (this.store.state.paths ?? []).find(
      (path) => path.kind === 'reference' && path.points?.length
    );
    if (!reference) {
      this.notify('The vessel has not echoed a course back yet.', 'warn');
      return;
    }
    if (
      this.rows.length &&
      !window.confirm('Replace what you are editing with the course the boat is running?')
    ) {
      return;
    }
    this.rows = reference.points.map(([x, y], index) => ({
      name: reference.names?.[index] ?? '',
      x,
      y,
      role: reference.roles?.[index] ?? this.defaultRole,
    }));
    if (this.elements.nameInput && reference.label) {
      this.elements.nameInput.value = reference.label.replace(/^plan:\s*/, '');
    }
    this._changed();
    this.notify(`Loaded ${this.rows.length} waypoints from the vessel.`, 'ok', 4000);
  }

  _changed() {
    this._render();
    this.onRowsChange?.(this.rows);
  }

  _render() {
    const body = this.elements.rows;
    if (!body) return;

    body.replaceChildren();
    if (!this.rows.length) {
      body.append(
        el(
          'p',
          'card-note card-note--tight',
          'No waypoints yet. Paste the handout above, or click the chart on the overview page.'
        )
      );
      this._updateSummary();
      return;
    }

    this.rows.forEach((row, index) => body.append(this._renderRow(row, index)));
    this._updateSummary();
  }

  _renderRow(row, index) {
    const line = el('div', 'wp-row');
    line.style.setProperty('--role', styleOfRole(row.role).colour);

    line.append(el('span', 'wp-index', String(index + 1)));

    const name = el('input', 'input input--sm wp-name');
    name.type = 'text';
    name.value = row.name ?? '';
    name.placeholder = String(index + 1);
    name.title = 'What the handout calls this point, e.g. 1.3';
    name.disabled = !this.canSend;
    name.addEventListener('input', () => {
      row.name = name.value.slice(0, 32);
    });
    line.append(name);

    const coordinate = el('input', 'input input--sm input--mono wp-coord');
    coordinate.type = 'text';
    coordinate.disabled = !this.canSend;
    coordinate.value = this._coordinateText(row);
    coordinate.title =
      'Latitude, longitude in degrees — or "x, y m" for a point in grid metres. ' +
      'Grid metres are converted by the vessel against its current origin.';
    coordinate.addEventListener('change', () => this._readCoordinate(row, coordinate));
    line.append(coordinate);

    const role = el('select', 'select select--sm wp-role');
    role.disabled = !this.canSend;
    for (const entry of this.roles) {
      const option = el('option', null, entry.label);
      option.value = entry.name;
      option.title = entry.help;
      role.append(option);
    }
    role.value = row.role;
    role.title = this.roles.find((entry) => entry.name === row.role)?.help ?? '';
    role.addEventListener('change', () => {
      row.role = role.value;
      // The next point laid or added inherits this, because a course runs in
      // stretches of one role rather than alternating.
      this.defaultRole = role.value;
      this._changed();
    });
    line.append(role);

    // The hold time only exists for the roles that stop. Showing an empty
    // "hold" box against a transit waypoint invites someone to fill it in and
    // then wonder why the boat drove straight past.
    const settles = this.roles.find((entry) => entry.name === row.role)?.settles;
    for (const [field, spec] of Object.entries(this.numericFields)) {
      if (field === 'hold_s' && !settles) continue;
      const wrap = el('label', 'wp-num');
      wrap.append(el('span', 'wp-num-label', spec.label));
      const input = el('input', 'input input--sm');
      input.type = 'number';
      input.min = spec.min;
      input.max = spec.max;
      input.step = spec.step;
      input.placeholder = spec.unit;
      input.disabled = !this.canSend;
      if (Number.isFinite(row[field])) input.value = row[field];
      input.addEventListener('change', () => {
        const value = Number.parseFloat(input.value);
        row[field] = Number.isFinite(value) ? value : undefined;
        this._updateSummary();
      });
      wrap.append(input);
      line.append(wrap);
    }

    const tools = el('div', 'wp-tools');
    for (const [label, title, action] of [
      ['↑', 'Move up', () => this._move(index, -1)],
      ['↓', 'Move down', () => this._move(index, 1)],
      ['✕', 'Remove this waypoint', () => this._remove(index)],
    ]) {
      const button = el('button', 'icon-btn icon-btn--sm', label);
      button.type = 'button';
      button.title = title;
      button.disabled = !this.canSend;
      button.addEventListener('click', action);
      tools.append(button);
    }
    line.append(tools);

    return line;
  }

  _coordinateText(row) {
    if (Number.isFinite(row.lat) && Number.isFinite(row.lon)) {
      return `${row.lat.toFixed(6)}, ${row.lon.toFixed(6)}`;
    }
    if (Number.isFinite(row.x) && Number.isFinite(row.y)) {
      return `${row.x.toFixed(1)}, ${row.y.toFixed(1)} m`;
    }
    return '';
  }

  /** Re-read one edited coordinate cell. A trailing "m" means grid metres. */
  _readCoordinate(row, input) {
    const text = input.value.trim();
    const grid = /m\s*$/i.test(text);
    const numbers = text
      .replace(/m\s*$/i, '')
      .split(/[\s,;]+/)
      .map((token) => Number.parseFloat(token))
      .filter((value) => Number.isFinite(value));

    if (numbers.length < 2) {
      this.notify('That is not a coordinate pair — expected "lat, lon".', 'warn');
      input.value = this._coordinateText(row);
      return;
    }

    if (grid) {
      row.x = numbers[0];
      row.y = numbers[1];
      row.lat = row.lon = null;
    } else if (Math.abs(numbers[0]) > 90 || Math.abs(numbers[1]) > 180) {
      this.notify(
        `${numbers[0]}, ${numbers[1]} is not a position on Earth — latitude first. ` +
          'Add "m" to the end for a point in grid metres.',
        'warn'
      );
      input.value = this._coordinateText(row);
      return;
    } else {
      row.lat = numbers[0];
      row.lon = numbers[1];
      row.x = row.y = null;
    }
    input.value = this._coordinateText(row);
    this.onRowsChange?.(this.rows);
    this._updateSummary();
  }

  _move(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= this.rows.length) return;
    [this.rows[index], this.rows[target]] = [this.rows[target], this.rows[index]];
    this._changed();
  }

  _remove(index) {
    this.rows.splice(index, 1);
    this._changed();
  }

  _updateSummary() {
    const { summary, sendButton } = this.elements;
    const count = this.rows.length;
    if (summary) {
      summary.textContent = count
        ? `${count} waypoint${count === 1 ? '' : 's'} — ${shapeOf(this.rows)}. ` +
          'Sending loads the course; it does not start the boat.'
        : 'Nothing to send yet.';
    }
    if (sendButton) sendButton.disabled = !this.canSend || !count;
  }

  /** The plan as the vessel's `set_plan` wants it. */
  toPlan() {
    return {
      name: this.elements.nameInput?.value?.trim() || 'course',
      channel_bearing: Number.parseFloat(this.elements.bearingInput?.value) || 0,
      waypoints: this.rows.map(rowToWaypoint),
    };
  }

  async send() {
    if (!this.rows.length) return;
    const incomplete = this.rows.findIndex(
      (row) =>
        !(Number.isFinite(row.lat) && Number.isFinite(row.lon)) &&
        !(Number.isFinite(row.x) && Number.isFinite(row.y))
    );
    if (incomplete >= 0) {
      this.notify(`Waypoint ${incomplete + 1} has no coordinate yet.`, 'error');
      return;
    }

    const plan = this.toPlan();
    const preview = this.rows.map(describeRow).join('\n');
    const bearing = plan.channel_bearing;
    const ok = window.confirm(
      `Send the course "${plan.name}" to the vessel?\n\n${preview}\n\n` +
        `Direction of buoyage: ${bearing.toFixed(0)}° ` +
        `(sailing that way, red is to port).\n\n` +
        'This only loads the course. The boat does not move until you press Engage.'
    );
    if (!ok) return;

    try {
      await sendCommand('set_plan', { plan });
      this.notify(
        `Course "${plan.name}" sent: ${plan.waypoints.length} waypoints. ` +
          'Check the ack, then check the route on the chart before engaging.',
        'ok',
        12000
      );
    } catch (error) {
      this.notify(error.message, 'error', 15000);
    }
  }
}
