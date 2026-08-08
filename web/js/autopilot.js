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
      if (this._detail) this._detail.replaceChildren();
      return;
    }

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
    this._updateSees(block);
    this._updateActions(mode);
    if (this._detail) this._updateDetail(block);
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
    rows.push([
      'Recording',
      recording.recording ? recording.file ?? 'yes' : 'not recording — attempt 2 will be blind',
    ]);

    const perception = block.perception ?? {};
    if (Number.isFinite(perception.confirmed)) {
      rows.push([
        'World model',
        `${perception.confirmed} confirmed of ${perception.tracks ?? '?'} tracked`,
      ]);
    }
    if (perception.edge) rows.push(['Jetson', perception.edge]);

    const bus = block.bus ?? {};
    if (Number.isFinite(bus.hz)) rows.push(['Node bus', `${bus.hz.toFixed(1)} Hz`]);

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

/** Optional numeric columns, and the range the server will accept. */
const NUMERIC_FIELDS = {
  speed: { label: 'Speed', unit: 'm/s', min: 0.05, max: 3, step: 0.05 },
  hold_s: { label: 'Hold', unit: 's', min: 0, max: 600, step: 1 },
};

export class CoursePlanner {
  constructor(elements, store, { notify, canSend = true }) {
    this.elements = elements;
    this.store = store;
    this.notify = notify;
    this.canSend = canSend;
    this.rows = [];
    this.roles = roleList(store.session);
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
    for (const [field, spec] of Object.entries(NUMERIC_FIELDS)) {
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
