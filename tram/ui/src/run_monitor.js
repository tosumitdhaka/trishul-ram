import { api } from './api.js'

// `partial` is terminal (v1.8.0): the run completed with losses under
// continue-on-error — there is nothing left to wait for.
const TERMINAL_RUN_STATUSES = new Set(['success', 'partial', 'failed', 'aborted'])

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export async function monitorTriggeredRun(
  runId,
  {
    isActive = () => true,
    pollMs = 1500,
    timeoutMs = 120_000,
  } = {},
) {
  // A queued run's liveness budget restarts while it waits — see below.
  let deadline = Date.now() + timeoutMs

  while (isActive()) {
    if (Date.now() > deadline) return null
    await wait(pollMs)

    try {
      const run = await api.runs.get(runId)
      if (TERMINAL_RUN_STATUSES.has(run?.status)) {
        return run
      }
      if (run?.status === 'queued') {
        // E.2 (GH #21): a queued manual run can wait out its whole TTL before
        // capacity returns. The queued phase doesn't consume the monitor's
        // timeout — push the deadline so the "Queued…" button and info row
        // keep being watched (and the page re-renders on terminal status)
        // instead of going stale after timeoutMs.
        deadline = Date.now() + timeoutMs
      }
    } catch (e) {
      if (e.status !== 404) throw e
    }
  }

  return null
}

export function runOutcomeToast(run, { name = '', genericLabel = 'Run' } = {}) {
  if (!run) return null

  const prefix = name ? `${name}: ` : `${genericLabel} `
  const status = run.outcome || run.status
  if (status === 'success') {
    return {
      message: `${prefix}success`,
      type: 'success',
    }
  }
  if (status === 'partial') {
    // Completed with losses (v1.8.0): a warning, not an error — the run
    // finished, but records were lost or failed along the way.
    return {
      message: `${prefix}completed with losses (partial)`,
      type: 'warning',
    }
  }
  if (run.error) {
    return {
      message: `${prefix}${run.error}`,
      type: 'error',
    }
  }
  return {
    message: `${prefix}${status}`,
    type: status === 'aborted' ? 'warning' : 'error',
  }
}
