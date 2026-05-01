import { api } from './api.js'

const TERMINAL_RUN_STATUSES = new Set(['success', 'failed', 'aborted'])

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
  const startedAt = Date.now()

  while (isActive()) {
    if ((Date.now() - startedAt) > timeoutMs) return null
    await wait(pollMs)

    try {
      const run = await api.runs.get(runId)
      if (TERMINAL_RUN_STATUSES.has(run?.status)) {
        return run
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
  if (run.status === 'success') {
    return {
      message: `${prefix}success`,
      type: 'success',
    }
  }
  if (run.error) {
    return {
      message: `${prefix}${run.error}`,
      type: 'error',
    }
  }
  return {
    message: `${prefix}${run.status}`,
    type: run.status === 'aborted' ? 'warning' : 'error',
  }
}
