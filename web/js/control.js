/* The control & data page: everything the overview deliberately leaves out.
 *
 * No map — that is what / is for. This page is the instrument panel: every
 * telemetry field the vessel publishes, the operator controls, the log stream,
 * the command audit trail and the per-repo update list.
 *
 * Access model, which is the whole point of splitting the pages:
 *
 *   read-only viewer   sees every measurement, the logs and the audit trail;
 *                      every control is rendered but disabled, and the repo
 *                      list is hidden entirely.
 *   admin              same page, controls live, repo list visible.
 *
 * The server enforces this independently — /api/command and /api/deploy both
 * require the admin cookie — so the disabling here is honesty about what will
 * work, not the security boundary.
 */

import { AutopilotPanel, CoursePlanner } from './autopilot.js';
import { CommandPanel, renderCommandHistory } from './commands.js';
import { DeployPanel } from './deploy.js';
import { LogConsole, downloadText } from './logs.js';
import {
  $,
  bootShell,
  connectShellStream,
  notify,
  startHeartbeat,
  updateHeader,
} from './shell.js';
import { KpiStrip, TelemetryPanels } from './telemetry.js';
import { TripPanel } from './trips.js';
import { TuningPanel } from './tuning.js';

async function boot() {
  const { store, admin } = await bootShell();

  $('readonly-bar').hidden = admin;
  $('deploy-card').hidden = !admin;

  /* --- figures and panels ------------------------------------------- */

  const kpiStrip = new KpiStrip($('kpi-strip'), store); // all tiles here
  const telemetryPanels = new TelemetryPanels($('telemetry-panels'), store);

  /* --- log console -------------------------------------------------- */

  const logConsole = new LogConsole(
    {
      view: $('log-view'),
      chips: $('level-chips'),
      filterInput: $('log-filter'),
      pauseButton: $('log-pause'),
      countLabel: $('log-count'),
      statusLabel: $('log-status'),
    },
    store
  );

  $('log-clear').addEventListener('click', () => logConsole.clear());
  $('log-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(logConsole.visibleText());
      notify('Visible log lines copied.', 'ok', 3000);
    } catch {
      notify('Clipboard access was refused by the browser.', 'warn');
    }
  });
  $('log-download').addEventListener('click', () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    downloadText(`ligmax-log-${stamp}.txt`, logConsole.visibleText());
  });

  /* --- commands ----------------------------------------------------- */

  // Built for everyone, `canSend` gated: a read-only viewer should be able to
  // see what the operator's console looks like without being able to use it.
  const commandPanel = new CommandPanel(
    {
      estopButton: $('estop-btn'),
      modeSelect: $('mode-select'),
      modeApply: $('mode-apply'),
      speedLimit: $('speed-limit'),
      speedApply: $('speed-apply'),
      rawPayload: $('raw-payload'),
      rawSend: $('raw-send'),
      quickCommands: $('quick-commands'),
    },
    store,
    { notify, canSend: admin }
  );

  /* --- autonomy ----------------------------------------------------- */

  // Both rendered for everyone and gated on `canSend`, for the same reason the
  // tuning panel is: what the boat has decided and what course is loaded are
  // measurements, and a read-only viewer should be able to read them.
  const autopilotPanel = new AutopilotPanel($('autopilot-panel'), store, {
    notify,
    canSend: admin,
  });

  const coursePlanner = new CoursePlanner(
    {
      paste: $('course-paste'),
      parseButton: $('course-parse'),
      addButton: $('course-add'),
      clearButton: $('course-clear'),
      sendButton: $('course-send'),
      loadButton: $('course-load'),
      nameInput: $('course-name'),
      bearingInput: $('course-bearing'),
      rows: $('course-rows'),
      summary: $('course-summary'),
    },
    store,
    { notify, canSend: admin }
  );

  /* --- stabilisation tuning ----------------------------------------- */

  // Rendered for everyone: what the boat is tuned to is a measurement, and a
  // read-only viewer should be able to read it. The fields are disabled without
  // an admin session, and /api/command refuses `set_param` regardless.
  const tuningPanel = new TuningPanel(
    {
      groups: $('tuning-groups'),
      profiles: $('tuning-profiles'),
      status: $('tuning-status'),
    },
    store,
    { notify, canSend: admin }
  );
  const tuningReload = $('tuning-reload');
  tuningReload.disabled = !admin;
  tuningReload.addEventListener('click', () => tuningPanel.reload());
  tuningPanel.start();

  /* --- trip recordings ---------------------------------------------- */

  // Everyone can list and download; only an admin sees a delete button. See
  // trips.js — a recording is evidence, and the tent is full of people who need
  // to read one and do not have the key.
  const tripPanel = new TripPanel(
    {
      list: $('trips-list'),
      status: $('trips-status'),
      reload: $('trips-reload'),
    },
    { notify, admin }
  ).start();

  /* --- deployments -------------------------------------------------- */

  let deployPanel = null;
  if (admin) {
    deployPanel = new DeployPanel($('deploy-list'), { notify });
    deployPanel.setAdmin(true);
    deployPanel.start();
  }

  /* --- store -> ui -------------------------------------------------- */

  store.on('state', () => {
    kpiStrip.update();
    telemetryPanels.update();
    commandPanel.syncModes();
    autopilotPanel.update();
    tuningPanel.update();
    updateHeader(store);
  });

  store.on('stats', () => updateHeader(store));
  store.on('link', () => updateHeader(store));
  store.on('logs', (entries) => logConsole.append(entries));
  store.on('snapshot', () => {
    logConsole.rebuild();
    renderCommandHistory($('cmd-list'), store.commands);
  });
  store.on('commands', (commands) => {
    renderCommandHistory($('cmd-list'), commands);
    // The tuning panel reads a `set_param`'s fate out of the audit list, so it
    // has to see command updates as well as telemetry ones - the vessel's refusal
    // arrives as an ack, not as a state change.
    tuningPanel.update();
  });

  renderCommandHistory($('cmd-list'), []);
  autopilotPanel.update();

  connectShellStream(store);

  // Reachable from the browser console, because this page is for debugging.
  window.ligmax = {
    store,
    logConsole,
    commandPanel,
    kpiStrip,
    telemetryPanels,
    deployPanel,
    tripPanel,
    tuningPanel,
    autopilotPanel,
    coursePlanner,
  };

  startHeartbeat(store, () => kpiStrip.update());
}

boot().catch((error) => {
  console.error(error);
  notify(`Page failed to start: ${error.message}`, 'error', 30000);
});
