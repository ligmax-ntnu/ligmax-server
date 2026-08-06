/* Who is in charge of the boat — the one question the status indicator answers.
 *
 * The vessel decides this and sends it as `status` (protocol.py VESSEL_STATUS).
 * The same value picks the colour of the lights on the hull, on the vessel, in
 * `ligmax-pi/nodes/io_manager/status.py` — so this file must not invent a sixth
 * state or rename one of the five, or the shore and the hull start disagreeing.
 *
 * The one thing the dashboard decides for itself is what to show when the boat
 * has gone quiet. `resolve()` overrides a stale status with OUT_OF_CONTROL and
 * says why. That is not pessimism: a status indicator still reading "Autonomous"
 * ninety seconds after the last frame is actively misleading, and the honest
 * statement is that nobody on shore knows what the boat is doing.
 */

/** Seconds without a frame before the dashboard stops trusting `status`. */
export const STATUS_TRUST_WINDOW = 10;

export const STATUS_META = {
  AUTONOMOUS: {
    label: 'Autonomous',
    plain: 'sailing itself',
    level: 'ok',
    light: 'green',
    lightName: 'solid green',
    detail: 'Running on its own navigation. Nobody is steering.',
  },
  REMOTE: {
    label: 'Remote control',
    plain: 'being driven by hand',
    level: 'warn',
    light: 'yellow',
    lightName: 'solid yellow',
    detail: 'A human has control, from the RC link or the shore client.',
  },
  STANDBY: {
    label: 'Standby',
    plain: 'waiting, not driving',
    level: 'idle',
    light: 'white',
    lightName: 'breathing white',
    detail: 'Powered and linked, deliberately not moving.',
  },
  OUT_OF_CONTROL: {
    label: 'Out of control',
    plain: 'not answering',
    level: 'danger',
    light: 'red-strobe',
    lightName: 'red strobe',
    detail: 'Nothing is steering it and propulsion has not been cut.',
  },
  KILLED: {
    label: 'Kill switch',
    plain: 'stopped',
    level: 'danger',
    light: 'red',
    lightName: 'solid red',
    detail: 'Propulsion power is cut at the safety loop.',
  },
};

export const UNKNOWN_STATUS = {
  label: 'Unknown',
  plain: 'no word from the boat',
  level: 'offline',
  light: null,
  lightName: null,
  detail: 'No telemetry has arrived, so there is nothing to report.',
};

export function metaFor(status) {
  return STATUS_META[status] ?? UNKNOWN_STATUS;
}

/**
 * What to display, given the store. Returns
 * `{status, meta, reported, stale, reason}`.
 *
 * `reported` is what the vessel last claimed, kept separate from `status` so the
 * UI can say "was Autonomous, 40 s ago" rather than silently replacing it.
 */
export function resolve(store) {
  // E-stop outranks everything the vessel says about itself. It is a physical
  // fact on a relay, and the frame that carries `estop: true` may well have been
  // assembled before the status machine noticed.
  const reported = store.state.estop ? 'KILLED' : store.state.status ?? null;

  const age = store.stats?.last_frame_age;
  const everSeen = Boolean(store.stats?.last_frame_at);

  if (!everSeen) {
    return { status: null, meta: UNKNOWN_STATUS, reported, stale: true, reason: 'no telemetry yet' };
  }
  if (Number.isFinite(age) && age > STATUS_TRUST_WINDOW) {
    return {
      status: 'OUT_OF_CONTROL',
      meta: STATUS_META.OUT_OF_CONTROL,
      reported,
      stale: true,
      reason: `no telemetry for ${Math.round(age)} s`,
    };
  }
  if (reported === null) {
    return {
      status: null,
      meta: UNKNOWN_STATUS,
      reported,
      stale: false,
      reason: 'the vessel is not publishing a status',
    };
  }
  return { status: reported, meta: metaFor(reported), reported, stale: false, reason: null };
}

/**
 * Does the hull agree with the status? `telemetry.lights.colour` is what the
 * lights ESP32 was last told to show, reported back up by the Pi.
 *
 * Returns null when there is nothing to compare, so a boat with no lights node
 * yet does not produce a permanent warning.
 */
export function lightsAgree(store, status) {
  const shown = store.telemetry('lights.colour');
  if (typeof shown !== 'string' || !status) return null;
  const expected = metaFor(status).light;
  if (!expected) return null;
  return shown === expected;
}
