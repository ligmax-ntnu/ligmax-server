/* The trip recordings the ground station is holding.
 *
 * One gzipped JSONL per autonomous attempt, pushed up off the boat after the
 * run (`ligmax_gui/trips.py`). This panel exists for one moment: between two
 * attempts at the same task, in the tent, when the question is "what did it
 * actually do at waypoint 4" and the answer is in a file that is no longer on
 * the boat.
 *
 * Reading is open to anyone who can read the page, because a recording is
 * evidence in the same sense the telemetry is. Deleting is admin-only and asks,
 * because it is the only irreversible thing here and these files cannot be
 * regenerated — the run they describe is over.
 */

import { deleteTrip, fetchTrips, tripUrl } from './api.js';
import * as fmt from './format.js';

const POLL_MS = 20000;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export class TripPanel {
  constructor(elements, { notify, admin = false }) {
    this.elements = elements;
    this.notify = notify;
    this.admin = admin;
    this.state = null;
    this._timer = null;

    this.elements.reload?.addEventListener('click', () => this.refresh(true));
  }

  start() {
    this.refresh();
    // Polled rather than pushed: an upload finishing is not a telemetry event,
    // and it happens minutes apart at most. Twenty seconds is well inside the
    // gap between someone pressing Disengage and going to look for the file.
    this._timer = window.setInterval(() => this.refresh(), POLL_MS);
    return this;
  }

  stop() {
    if (this._timer) window.clearInterval(this._timer);
    this._timer = null;
  }

  async refresh(loud = false) {
    try {
      this.state = await fetchTrips();
      this._render();
      if (loud) this.notify('Trip list refreshed.', 'ok', 3000);
    } catch (error) {
      this.elements.status.textContent = `Could not list recordings: ${error.message}`;
    }
  }

  _render() {
    const list = this.elements.list;
    const status = this.elements.status;
    if (!list || !this.state) return;

    const trips = this.state.trips ?? [];
    const pending = this.state.pending ?? {};
    const pendingNames = Object.keys(pending);

    list.replaceChildren();

    if (!trips.length && !pendingNames.length) {
      list.append(
        el(
          'p',
          'card-note card-note--tight',
          this.state.error
            ? `The ground station cannot store recordings: ${this.state.error}`
            : 'Nothing here yet. The boat pushes each attempt up after the run; ' +
              'until then it is on the card, and `scp` still works.'
        )
      );
    }

    for (const trip of trips) {
      list.append(this._row(trip));
    }

    // Uploads still arriving, shown separately and never as a recording: a
    // half-file that looked complete would be handed to review_trip.py and fail
    // in a way nobody would connect back to a dropped 4G link.
    for (const name of pendingNames) {
      const row = el('div', 'trip-row trip-row--pending');
      row.append(
        el('span', 'trip-name', name),
        el('span', 'trip-size', `${(pending[name] / 1048576).toFixed(1)} MB so far`),
        el('span', 'trip-note', 'still uploading')
      );
      list.append(row);
    }

    const parts = [];
    if (trips.length) {
      const total = (this.state.bytes ?? 0) / 1048576;
      parts.push(
        `${trips.length} recording${trips.length === 1 ? '' : 's'}, ${total.toFixed(1)} MB`
      );
    }
    if (pendingNames.length) parts.push(`${pendingNames.length} uploading`);
    if (Number.isFinite(this.state.free_mb)) {
      parts.push(`${(this.state.free_mb / 1024).toFixed(1)} GB free here`);
    }
    status.textContent = parts.join(' · ') || 'No recordings held.';
  }

  _row(trip) {
    const row = el('div', 'trip-row');

    const link = el('a', 'trip-name', trip.name);
    link.href = tripUrl(trip.boat, trip.name);
    link.download = trip.name;
    link.title = `Download ${trip.name} (${trip.mb} MB)`;
    row.append(link);

    row.append(el('span', 'trip-size', `${trip.mb} MB`));
    // Age rather than a timestamp: "40 minutes ago" answers "is this the attempt
    // I just watched" without anyone doing arithmetic against a clock that may
    // be in a different timezone from the boat's.
    row.append(
      el('span', 'trip-age', fmt.ago(Math.max(0, Date.now() / 1000 - trip.modified)))
    );
    if (trip.boat && trip.boat !== 'ligmax') {
      row.append(el('span', 'trip-note', trip.boat));
    }

    if (this.admin) {
      const remove = el('button', 'icon-btn icon-btn--sm', '✕');
      remove.type = 'button';
      remove.title = 'Delete this recording from the ground station';
      remove.addEventListener('click', async () => {
        if (
          !window.confirm(
            `Delete ${trip.name}?\n\nThis is the only copy the ground station ` +
              'holds. If the boat has already rotated it off the card, the run ' +
              'is gone for good.'
          )
        ) {
          return;
        }
        try {
          await deleteTrip(trip.boat, trip.name);
          this.notify(`Deleted ${trip.name}.`, 'ok', 4000);
          this.refresh();
        } catch (error) {
          this.notify(error.message, 'error');
        }
      });
      row.append(remove);
    }

    return row;
  }
}
