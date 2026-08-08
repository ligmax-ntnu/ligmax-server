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

  connectShellStream(store);

  // Reachable from the browser console, because this page is for debugging.
  window.ligmax = {
    store,
    logConsole,
    commandPanel,
    kpiStrip,
    telemetryPanels,
    deployPanel,
    tuningPanel,
  };

  startHeartbeat(store, () => kpiStrip.update());
}

boot().catch((error) => {
  console.error(error);
  notify(`Page failed to start: ${error.message}`, 'error', 30000);
});
