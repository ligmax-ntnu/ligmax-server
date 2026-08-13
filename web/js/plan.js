/* Waypoint roles: how they look, and how a pasted course becomes rows.
 *
 * A Njord course is not a list of places, it is a list of places plus what to do
 * between them — the same GPS point means "drive here and ignore everything" on
 * one leg and "drive here, but a red buoy on this leg must be left to port" on
 * the next. So every waypoint carries a role, and this module is the one place
 * that decides what each role looks like on screen.
 *
 * The role *names*, their help text and the validation ranges come from the
 * server (`/api/session` -> `waypoint_roles`, out of `ligmax_gui/plan.py`), which
 * in turn mirrors the vessel's `nodes/self_driving/plan.py`. Only the
 * presentation is hard-coded here — the same split `obstacles.js` has with
 * ObstacleType, and for the same reason: adding a role on the boat must not mean
 * editing three files in the browser.
 */

/** Display order in the dropdown, and the order the legend uses. */
export const ROLE_ORDER = [
  'transit',
  'buoys',
  'avoid',
  'collision_front',
  'collision_right',
  'collision_front_backup',
  'collision_right_backup',
  'hold',
  'dock',
  'dock_parallel',
  'park',
  'park_parallel',
];

/* Colour and glyph per role.
 *
 * Chosen to sit apart from the obstacle palette in `obstacles.js` — a waypoint
 * is a place the operator chose, an obstacle is a thing the boat found, and the
 * two must never be confusable on a chart being read from behind someone's
 * shoulder. Hence also the shape split: obstacles are filled circles and hulls,
 * waypoints are diamonds carrying a letter.
 *
 * `transit` is deliberately the quietest of the eight. It is the default and the
 * most common, and a course that is mostly transit should read as mostly plain,
 * so the legs that *do* carry a rule stand out.
 *
 * The two parking roles borrow the docking hues rather than taking new ones: they
 * are the same berth by a different method (three lines instead of a gap), and a
 * chart being read over someone's shoulder should say "this is the docking leg"
 * first and "by which method" second. The letters are what tell them apart.
 */
export const ROLE_STYLE = {
  transit: { colour: '#8fa8cf', code: 'T', short: 'Transit' },
  buoys: { colour: '#4fd1c5', code: 'B', short: 'Buoy rules' },
  avoid: { colour: '#ff7a45', code: 'A', short: 'Avoidance' },
  // The four Task 2 roles share the avoidance hue for the same reason the
  // parking roles borrow the docking one: a chart read over someone's shoulder
  // should say "this is the collision leg" first and "by which method" second.
  // The letters do the telling apart, and the two BLIND backups are deliberately
  // the loudest thing in the palette -- a role that manoeuvres whether or not
  // anything is there must never be left on a course by accident.
  collision_front: { colour: '#ff7a45', code: 'F', short: 'Collision, ahead' },
  collision_right: { colour: '#ff7a45', code: 'R', short: 'Collision, starboard' },
  collision_front_backup: { colour: '#ff3b30', code: 'F!', short: 'BLIND ahead' },
  collision_right_backup: { colour: '#ff3b30', code: 'R!', short: 'BLIND starboard' },
  hold: { colour: '#b48ef7', code: 'H', short: 'Hold' },
  dock: { colour: '#ffd23f', code: 'D', short: 'Dock' },
  dock_parallel: { colour: '#f78ec6', code: 'P', short: 'Parallel dock' },
  park: { colour: '#ffc21f', code: 'K', short: 'Park' },
  park_parallel: { colour: '#f26fb4', code: 'L', short: 'Parallel park' },
};

const FALLBACK_STYLE = { colour: '#8b98ae', code: '?', short: 'Unknown role' };

export function styleOfRole(role) {
  return ROLE_STYLE[role] ?? FALLBACK_STYLE;
}

/** The roles the server offers, in display order, with presentation merged in. */
export function roleList(session) {
  const table = session?.waypoint_roles ?? {};
  const names = Object.keys(table);
  // Anything the server offers that this file has never heard of still gets
  // listed — better a role with a grey diamond than a course you cannot lay.
  const ordered = [
    ...ROLE_ORDER.filter((name) => names.includes(name)),
    ...names.filter((name) => !ROLE_ORDER.includes(name)),
  ];
  return ordered.map((name) => ({
    name,
    label: table[name]?.label ?? name,
    help: table[name]?.help ?? '',
    settles: Boolean(table[name]?.settles),
    defaultHold: table[name]?.default_hold_s ?? 0,
    ...styleOfRole(name),
  }));
}

/* Presentation for the optional numeric columns. The *bounds* are deliberately
 * not here: they come from `/api/session` -> `waypoint_limits`, out of the same
 * `ligmax_gui/plan.py` table the validator refuses against.
 *
 * They used to be here, and speed drifted. The vessel dropped to its 5 kn limit
 * (2.5722 m/s) and this file still said `max: 3`, so the editor accepted 2.8,
 * the server returned 200, and the boat refused the *whole plan* — at 08:15, on
 * a dock, with a course being typed in. The values below are only the fallback
 * for a session payload that predates `waypoint_limits`, and speed's fallback is
 * the vessel's real ceiling rather than a round number.
 */
const NUMERIC_STYLE = {
  speed: { label: 'Speed', unit: 'm/s', step: 0.05, min: 0.05, max: 2.5722 },
  hold_s: { label: 'Hold', unit: 's', step: 1, min: 0, max: 600 },
};

/** The optional numeric columns, with the server's own bounds merged in. */
export function numericFields(session) {
  const limits = session?.waypoint_limits ?? {};
  const out = {};
  for (const [field, style] of Object.entries(NUMERIC_STYLE)) {
    const bounds = limits[field];
    out[field] = {
      ...style,
      min: Number.isFinite(bounds?.min) ? bounds.min : style.min,
      max: Number.isFinite(bounds?.max) ? bounds.max : style.max,
    };
  }
  return out;
}

/* --- parsing a pasted course ------------------------------------------ */

/* Words an operator might type for each role. The dropdown is the real
 * interface; these exist so a course pasted with its roles already in it does
 * not have to be re-picked row by row. */
const ROLE_WORDS = {
  transit: 'transit',
  gnss: 'transit',
  blind: 'transit',
  plain: 'transit',
  buoys: 'buoys',
  buoy: 'buoys',
  lateral: 'buoys',
  cardinal: 'buoys',
  cardinals: 'buoys',
  avoid: 'avoid',
  colreg: 'avoid',
  colregs: 'avoid',
  collision: 'avoid',
  otter: 'avoid',
  collision_front: 'collision_front',
  collision_right: 'collision_right',
  collision_front_backup: 'collision_front_backup',
  collision_right_backup: 'collision_right_backup',
  front: 'collision_front',
  right: 'collision_right',
  hold: 'hold',
  stop: 'hold',
  station: 'hold',
  stationary: 'hold',
  dock: 'dock',
  docking: 'dock',
  bow: 'dock',
  dock_parallel: 'dock_parallel',
  parallel: 'dock_parallel',
  alongside: 'dock_parallel',
  park: 'park',
  parking: 'park',
  lines: 'park',
  park_parallel: 'park_parallel',
  parallelpark: 'park_parallel',
  // `parallel` on its own stays with `dock_parallel` above, because it was there
  // first and a pasted course written before parking existed must keep meaning
  // what it meant. Say `park_parallel` for the line-based one.
};

const NUMBER = /^[-+]?\d*\.?\d+$/;

/**
 * Turn pasted text into rows.
 *
 * One waypoint per line. Blank lines and anything after `#` are ignored.
 * Within a line, separators are commas, semicolons, tabs or spaces, and the
 * rule for what is what is deliberately simple enough to state in the UI:
 *
 *     lat lon                 -> two numbers: the coordinate
 *     name lat lon            -> three or more: the first is the label
 *     ... role                -> a role word anywhere on the line
 *
 * Three numbers rather than two is the case that matters, because the Njord
 * handout labels its intermediate points `1.1`, `1.2` … — which are themselves
 * numbers, and would otherwise be eaten as a coordinate. Everything parsed is
 * shown back as an editable table before anything is sent, which is the real
 * protection against a misparse: the operator sees the rows, not the rule.
 *
 * Compass suffixes (`63.4305N`, `10.3951 E`) are accepted because handouts
 * sometimes carry them. A `S` or `W` negates.
 */
export function parseCourse(text, { defaultRole = 'transit' } = {}) {
  const rows = [];
  const errors = [];

  const lines = String(text ?? '').split(/\r?\n/);
  lines.forEach((rawLine, lineIndex) => {
    const line = rawLine.split('#')[0].trim();
    if (!line) return;

    let role = null;
    const numbers = [];
    let name = null;

    for (const rawToken of line.split(/[\s,;]+/)) {
      const token = rawToken.trim();
      if (!token) continue;

      const word = token.toLowerCase().replace(/[^a-z_]/g, '');
      if (word && ROLE_WORDS[word]) {
        role = ROLE_WORDS[word];
        continue;
      }

      // A compass suffix rides on the number: 63.4305N, 10.3951W.
      const compass = /^([-+]?\d*\.?\d+)\s*([nsewNSEW])$/.exec(token);
      if (compass) {
        const value = Number.parseFloat(compass[1]);
        const negative = /[swSW]/.test(compass[2]);
        numbers.push(negative ? -Math.abs(value) : Math.abs(value));
        continue;
      }

      if (NUMBER.test(token)) {
        numbers.push(Number.parseFloat(token));
        continue;
      }

      // Not a number and not a role: a label, e.g. "start" or "gate-2".
      if (name === null) name = token.slice(0, 32);
    }

    if (numbers.length < 2) {
      errors.push(`line ${lineIndex + 1}: no coordinate pair in "${line.slice(0, 40)}"`);
      return;
    }

    // Three or more numbers means the first is a label — the `1.1` case.
    let lat;
    let lon;
    if (numbers.length >= 3) {
      if (name === null) name = trimNumber(numbers[0]);
      [, lat, lon] = numbers;
    } else {
      [lat, lon] = numbers;
    }

    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      errors.push(`line ${lineIndex + 1}: coordinates are not numbers`);
      return;
    }
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      errors.push(
        `line ${lineIndex + 1}: ${lat}, ${lon} is not a position on Earth ` +
          '(latitude first, then longitude)'
      );
      return;
    }

    rows.push({ name: name ?? '', lat, lon, role: role ?? defaultRole });
  });

  return { rows, errors };
}

function trimNumber(value) {
  return String(Number.parseFloat(value.toFixed(6)));
}

/* --- rows <-> the wire ------------------------------------------------- */

/**
 * One editor row as the vessel's plan format wants it.
 *
 * A row holds either a `lat`/`lon` (typed or pasted) or an `x`/`y` in grid
 * metres (clicked on the chart). Both are sent as they are: the *vessel* does
 * the conversion, against whatever origin is current, because it owns the grid
 * and may have re-zeroed it since this page loaded.
 */
export function rowToWaypoint(row, index) {
  const waypoint = { role: row.role || 'transit', name: row.name || String(index + 1) };
  if (Number.isFinite(row.lat) && Number.isFinite(row.lon)) {
    waypoint.lat = row.lat;
    waypoint.lon = row.lon;
  } else {
    waypoint.x = row.x;
    waypoint.y = row.y;
  }
  for (const field of ['speed', 'hold_s', 'radius', 'berth_width_m', 'park_offset_m']) {
    if (Number.isFinite(row[field])) waypoint[field] = row[field];
  }
  if (row.notes) waypoint.notes = row.notes;
  return waypoint;
}

/** How a row reads in a confirmation dialog, one line each. */
export function describeRow(row, index) {
  const where =
    Number.isFinite(row.lat) && Number.isFinite(row.lon)
      ? `${row.lat.toFixed(5)}, ${row.lon.toFixed(5)}`
      : `grid ${(row.x ?? 0).toFixed(1)}, ${(row.y ?? 0).toFixed(1)} m`;
  const style = styleOfRole(row.role);
  const hold = Number.isFinite(row.hold_s) ? `, hold ${row.hold_s}s` : '';
  return `${index + 1}. ${row.name || index + 1} — ${where} — ${style.short}${hold}`;
}

/** Count per role, for the summary line under the editor. */
export function shapeOf(rows) {
  const counts = new Map();
  for (const row of rows) counts.set(row.role, (counts.get(row.role) ?? 0) + 1);
  return [...counts.entries()]
    .sort((a, b) => ROLE_ORDER.indexOf(a[0]) - ROLE_ORDER.indexOf(b[0]))
    .map(([role, count]) => `${count}× ${styleOfRole(role).short.toLowerCase()}`)
    .join(', ');
}
