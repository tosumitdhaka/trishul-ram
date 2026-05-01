import { api } from '../api.js'
import { bindDataActions, downloadText, relTime, fmtNum, schedBadge, statusBadge, esc, toast, pipelineStartFeedback } from '../utils.js'
import { monitorTriggeredRun, runOutcomeToast } from '../run_monitor.js'
import {
  renderDiffStats,
  renderNumberedDiffLine,
  renderSideBySideYamlDiff,
} from '../yaml_diff.js'
import { renderRunsTable } from './runs_table.js'
import * as bootstrap from 'bootstrap'

let _name = null
let _activeYaml = null
let _activeTab = 'runs'
let _runMonitorToken = 0
let _versions = []
const _versionYamlCache = new Map()

export async function init() {
  _name = window._detailPipeline
  if (!_name) { navigate('pipelines'); return }
  _versionYamlCache.clear()

  const sub = document.getElementById('tb-sub')
  if (sub) sub.textContent = _name

  try {
    const [pipeline, placement, versions] = await Promise.all([
      api.pipelines.get(_name),
      api.pipelines.placement(_name).catch((e) => (e.status === 404 ? null : Promise.reject(e))),
      api.pipelines.versions(_name).catch(() => []),
    ])
    _versions = Array.isArray(versions) ? versions : []
    _activeYaml = pipeline.yaml || null
    renderHeader(pipeline)
    renderCards(pipeline)
    renderPlacement(placement)
    wireActions(pipeline)
  } catch (e) {
    toast(`Detail error: ${e.message}`, 'error')
  }

  wireTabs()
  showTab(_activeTab)
}

function wireTabs() {
  document.querySelectorAll('#detail-tabs .nav-link').forEach(tab => {
    tab.onclick = (e) => {
      e.preventDefault()
      showTab(tab.dataset.tab)
    }
  })
}

function showTab(tabName) {
  _activeTab = tabName || 'runs'
  document.querySelectorAll('#detail-tabs .nav-link').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.tab === _activeTab)
  })
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.add('d-none'))
  document.getElementById(`tab-panel-${_activeTab}`)?.classList.remove('d-none')

  if (_activeTab === 'runs') loadDetailRuns()
  if (_activeTab === 'config') loadConfig()
  if (_activeTab === 'versions') loadVersions()
  if (_activeTab === 'alerts') loadAlerts()
}

function renderHeader(p) {
  const title = document.getElementById('detail-title')
  const meta = document.getElementById('detail-meta')
  const badges = document.getElementById('detail-badges')

  if (title) title.textContent = p.name || _name
  if (meta) {
    const activeVersion = _versions.find(v => v.is_active) || _versions[0] || null
    const items = [
      activeVersion?.created_at
        ? `updated ${fmtTimestamp(activeVersion.created_at)} (${relTime(activeVersion.created_at)})`
        : p.registered_at
          ? `registered ${fmtTimestamp(p.registered_at)}`
          : null,
      p.last_run ? `last run ${relTime(p.last_run)}` : 'no completed runs yet',
    ].filter(Boolean)
    meta.innerHTML = items.map(item => `<span>${esc(item)}</span>`).join('<span>•</span>')
  }
  if (badges) {
    const fragments = [
      statusBadge(p.status),
      schedBadge(p),
      p.enabled === false ? statusBadge('disabled') : '',
    ].filter(Boolean)
    badges.innerHTML = fragments.join('')
  }
}

function renderCards(p) {
  const set = (id, val) => {
    const el = document.getElementById(id)
    if (el) el.textContent = val
  }
  set('detail-source',    p.source?.type || '—')
  const sinks = Array.isArray(p.sinks) ? p.sinks.map(s => s.type || s).join(', ') : '—'
  set('detail-sinks',     sinks)
  set('detail-schedule', scheduleLabel(p))
  set('detail-serializers', p.serializer_in || '—')
  const transformsEl = document.getElementById('detail-transforms')
  if (transformsEl) transformsEl.innerHTML = renderTransformFlow(p.transforms)
  set('detail-error',      p.on_error === 'dlq' && p.dlq ? 'dlq (configured)' : (p.on_error || 'continue'))
}

function renderPlacement(placement) {
  const card = document.getElementById('detail-placement-card')
  const body = document.getElementById('detail-placement-body')
  const count = document.getElementById('detail-placement-count')
  if (!card || !body || !count) return

  if (!placement) {
    card.classList.add('d-none')
    return
  }

  card.classList.remove('d-none')
  count.textContent = `${placement.active_slots}/${placement.slot_count} active`
  body.innerHTML = `
    <div class="d-flex align-items-center gap-2 flex-wrap mb-3">
      ${statusBadge(placement.status)}
      <span class="text-secondary">group ${esc(placement.placement_group_id || '—')}</span>
      <span class="text-secondary">started ${placement.started_at ? relTime(placement.started_at) : '—'}</span>
      <span class="text-secondary">${fmtNum(Math.round(placement.records_out_per_sec || 0))} out/s</span>
      <span class="text-secondary">${fmtNum(placement.error_count || 0)} errors</span>
    </div>
    <div class="table-wrap table-wrap-subtle">
      <table class="table mb-0">
        <thead>
          <tr>
            <th>Slot</th>
            <th>Worker</th>
            <th>Status</th>
            <th>Run</th>
            <th>Out/s</th>
            <th>Restart</th>
          </tr>
        </thead>
        <tbody>
          ${(placement.slots || []).map(slot => `
            <tr>
              <td class="mono">${slot.worker_index ?? '—'}</td>
              <td class="text-secondary">${esc(slot.worker_id || slot.worker_url || '—')}</td>
              <td>${statusBadge(slot.status || (slot.stats?.stale ? 'degraded' : 'running'))}</td>
              <td class="mono-sm text-secondary">${esc(slot.current_run_id || slot.run_id_prefix || '—')}</td>
              <td class="text-secondary">${fmtNum(Math.round(slot.stats?.records_out_per_sec || 0))}</td>
              <td class="text-secondary">${fmtNum(slot.restart_count || 0)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>`
}

function wireActions(pipeline) {
  document.getElementById('detail-back-btn').onclick = () => navigate('pipelines')
  document.getElementById('detail-edit-btn').onclick = () => {
    window._editorReturn = 'detail'
    window._editorPipeline = _name
    navigate('editor')
  }
  document.getElementById('detail-open-runs-btn').onclick = () => {
    window._runsFilters = { pipeline: _name }
    navigate('runs')
  }
  document.getElementById('detail-refresh-btn').onclick = () => { void _detailRefresh() }
  document.getElementById('detail-restart-btn').onclick = () => { void _detailRestart() }
  document.getElementById('detail-test-btn').onclick = () => { void _detailTestConnectors() }
  document.getElementById('detail-download-btn').onclick = () => { void _detailDownload() }
  const btn = document.getElementById('detail-run-btn')
  const triggerBtn = document.getElementById('detail-trigger-btn')
  if (!btn) return
  const isActive = pipeline.status === 'running' || pipeline.status === 'scheduled'
  const isManual = pipeline.schedule_type === 'manual'
  const isStream = pipeline.schedule_type === 'stream'

  if (isManual && !isActive) {
    btn.innerHTML = '<i class="bi bi-lightning"></i><span>Run Now</span>'
    btn.className = 'btn btn-sm btn-primary detail-action-btn'
    btn.onclick = () => { void _detailTrigger() }
  } else if (isActive) {
    btn.innerHTML = '<i class="bi bi-stop-fill"></i><span>Stop</span>'
    btn.className = 'btn btn-sm btn-danger detail-action-btn'
    btn.onclick = () => { void _detailStop() }
  } else {
    btn.innerHTML = '<i class="bi bi-play-fill"></i><span>Start</span>'
    btn.className = 'btn btn-sm btn-primary detail-action-btn'
    btn.onclick = () => { void _detailRun() }
  }

  if (triggerBtn) {
    if (isStream || isManual) {
      triggerBtn.setAttribute('hidden', '')
    } else {
      triggerBtn.removeAttribute('hidden')
      triggerBtn.disabled = isActive
      triggerBtn.onclick = () => { void _detailTrigger() }
    }
  }
}

async function _detailRun() {
  try {
    const result = await api.pipelines.start(_name)
    const feedback = pipelineStartFeedback(_name, result)
    toast(feedback.message, feedback.type)
    setTimeout(() => init(), 800)
  } catch (e) { toast(e.message, 'error') }
}

async function _detailTrigger() {
  const triggerBtn = document.getElementById('detail-trigger-btn')
  if (triggerBtn) {
    triggerBtn.disabled = true
    triggerBtn.innerHTML = '<i class="bi bi-hourglass-split"></i><span>Running…</span>'
  }
  try {
    const result = await api.pipelines.run(_name)
    setTimeout(() => init(), 400)
    if (result?.run_id) {
      const monitorToken = ++_runMonitorToken
      void _monitorTriggeredRun(result.run_id, monitorToken).catch((err) => {
        toast(`Run monitor error: ${err.message}`, 'error')
      })
    }
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (triggerBtn) {
      triggerBtn.disabled = false
      triggerBtn.innerHTML = '<i class="bi bi-lightning"></i><span>Run Now</span>'
    }
  }
}

async function _detailStop() {
  try {
    await api.pipelines.stop(_name)
    toast(`Stopped ${_name}`)
    setTimeout(() => init(), 800)
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function _detailRestart() {
  const btn = document.getElementById('detail-restart-btn')
  if (btn) {
    btn.disabled = true
    btn.innerHTML = '<i class="bi bi-hourglass-split"></i><span>Restarting…</span>'
  }
  try {
    await api.pipelines.restart(_name)
    toast(`Restarting ${_name}`)
    setTimeout(() => init(), 1000)
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) {
      btn.disabled = false
      btn.innerHTML = '<i class="bi bi-arrow-repeat"></i><span>Restart</span>'
    }
  }
}

async function _detailTestConnectors() {
  const btn = document.getElementById('detail-test-btn')
  const orig = btn?.innerHTML
  if (btn) btn.innerHTML = '<i class="bi bi-hourglass"></i>'
  try {
    const p = await api.pipelines.get(_name)
    const yaml = p.yaml || ''
    if (!yaml) { toast('Pipeline YAML not available', 'error'); return }
    const result = await api.connectors.testPipeline(yaml)
    _showConnectorResults(result)
  } catch (e) {
    toast(`Test error: ${e.message}`, 'error')
  } finally {
    if (btn && orig) btn.innerHTML = orig
  }
}

async function _detailDownload() {
  try {
    const yaml = _activeYaml || await api.pipelines.get(_name).then(p => p.yaml || p.raw || JSON.stringify(p, null, 2))
    downloadText(`${_name}.yaml`, yaml, 'text/yaml;charset=utf-8')
  } catch (e) { toast(e.message, 'error') }
}

async function _detailRefresh() {
  const btn = document.getElementById('detail-refresh-btn')
  const icon = document.getElementById('detail-refresh-icon')
  if (btn) btn.disabled = true
  if (icon) icon.className = 'bi bi-arrow-clockwise spin'
  try {
    await init()
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) btn.disabled = false
    if (icon) icon.className = 'bi bi-arrow-clockwise'
  }
}

async function loadDetailRuns() {
  const tbody = document.getElementById('detail-runs-body')
  const count = document.getElementById('detail-runs-count')
  if (!tbody) return
  tbody.innerHTML = '<tr><td colspan="12" class="text-secondary text-center py-4">Loading run history…</td></tr>'
  try {
    const runs = await api.runs.list({ pipeline: _name, limit: 100 })
    renderRunsTable({
      tbody,
      runs,
      rowIdPrefix: 'detail-runs',
      toggleHandlerName: '_detailRunsToggleLog',
      emptyMessage: 'No runs recorded for this pipeline',
    })
    if (count) count.textContent = runs.length
  } catch (e) {
    if (count) count.textContent = ''
    tbody.innerHTML = '<tr><td colspan="12" class="text-secondary text-center py-4">Could not load run history</td></tr>'
    toast(`Run history error: ${e.message}`, 'error')
  }
}

async function loadVersions() {
  const tbody = document.getElementById('detail-versions-body')
  const count = document.getElementById('detail-versions-count')
  if (!tbody) return
  tbody.innerHTML = '<tr><td colspan="3" class="text-secondary text-center py-4">Loading versions…</td></tr>'
  try {
    if (!_versions.length) {
      _versions = await api.pipelines.versions(_name)
    }
    if (count) count.textContent = _versions.length
    if (!_versions?.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-secondary text-center py-4">No versions saved</td></tr>'
      return
    }
    tbody.innerHTML = _versions.map(v => `
      <tr class="${v.is_active ? 'detail-version-row-current' : ''}">
        <td>
          <div class="detail-version-cell">
            <span class="mono fw-semibold">v${v.version}</span>
            ${v.is_active ? '<span class="pill">current</span>' : ''}
          </div>
        </td>
        <td>
          <div class="detail-version-time">
            <span>${fmtTimestamp(v.created_at)}</span>
            <span class="detail-muted-small">${v.created_at ? relTime(v.created_at) : '—'}</span>
          </div>
        </td>
        <td class="text-end">
          <button class="btn-flat detail-action-inline" type="button" data-action="view-version" data-version="${v.version}" title="View version YAML">
            <i class="bi bi-eye"></i><span>View</span>
          </button>
          <button class="btn-flat detail-action-inline" type="button" data-action="copy-version" data-version="${v.version}" title="Copy version YAML">
            <i class="bi bi-clipboard"></i><span>Copy</span>
          </button>
          <button class="btn-flat detail-action-inline" type="button" data-action="download-version" data-version="${v.version}" title="Download version YAML">
            <i class="bi bi-download"></i><span>Download</span>
          </button>
          <button class="btn-flat detail-action-inline" type="button" data-action="compare-version" data-version="${v.version}" title="Compare versions">
            <i class="bi bi-file-diff"></i><span>Compare</span>
          </button>
          <button class="btn-flat detail-action-inline${v.is_active ? ' is-disabled' : ''}" type="button" ${v.is_active ? 'disabled' : ''} data-action="rollback-version" data-version="${v.version}" title="Rollback to this version">
            <i class="bi bi-arrow-counterclockwise"></i><span>Rollback</span>
          </button>
        </td>
      </tr>
    `).join('')
    bindDataActions(tbody, {
      'view-version': (button) => {
        void _openVersionModal(parseInt(button.dataset.version, 10))
      },
      'copy-version': (button) => {
        void _copyVersion(parseInt(button.dataset.version, 10))
      },
      'download-version': (button) => {
        void _downloadVersion(parseInt(button.dataset.version, 10))
      },
      'compare-version': (button) => {
        void _compareVersion(parseInt(button.dataset.version, 10))
      },
      'rollback-version': (button) => {
        void _rollbackVersion(parseInt(button.dataset.version, 10))
      },
    })
  } catch (e) { toast(e.message, 'error') }
}

function _renderDiffPanes(oldYaml, newYaml) {
  const leftPane  = document.getElementById('diff-left-pane')
  const rightPane = document.getElementById('diff-right-pane')
  const stats = document.getElementById('diff-stats')
  renderSideBySideYamlDiff(oldYaml, newYaml, {
    leftPane,
    rightPane,
    statsEl: stats,
    renderLine: (lineNo, line, type) => renderNumberedDiffLine(lineNo, line, type, 'detail-diff'),
    renderStats: (adds, dels) => renderDiffStats(adds, dels, {
      muted: 'detail-diff-stat-muted',
      insert: 'detail-diff-stat-insert',
      delete: 'detail-diff-stat-delete',
    }),
    emptyLine: renderNumberedDiffLine('', '— empty —', 'gap', 'detail-diff'),
  })
}

function resolveCompareTarget(version) {
  const active = _versions.find(v => v.is_active)?.version
  if (active && active !== version) return active
  return _versions.find(v => v.version !== version)?.version ?? version
}

async function openCompareModal(leftVersion, rightVersion) {
  try {
    const leftSelect = document.getElementById('diff-left-select')
    const rightSelect = document.getElementById('diff-right-select')
    if (!leftSelect || !rightSelect) return

    const options = _versions.map(v =>
      `<option value="${v.version}">${esc(versionOptionLabel(v))}</option>`
    ).join('')
    leftSelect.innerHTML = options
    rightSelect.innerHTML = options
    leftSelect.value = String(leftVersion)
    rightSelect.value = String(rightVersion)

    leftSelect.onchange = () => _renderVersionComparison(parseInt(leftSelect.value, 10), parseInt(rightSelect.value, 10))
    rightSelect.onchange = () => _renderVersionComparison(parseInt(leftSelect.value, 10), parseInt(rightSelect.value, 10))
    const swapBtn = document.getElementById('diff-swap-btn')
    if (swapBtn) {
      swapBtn.onclick = () => {
        const currentLeft = leftSelect.value
        leftSelect.value = rightSelect.value
        rightSelect.value = currentLeft
        void _renderVersionComparison(parseInt(leftSelect.value, 10), parseInt(rightSelect.value, 10))
      }
    }

    await _renderVersionComparison(leftVersion, rightVersion)
    new bootstrap.Modal(document.getElementById('detail-diff-modal')).show()
  } catch (e) {
    toast(`Diff error: ${e.message}`, 'error')
  }
}

async function _renderVersionComparison(leftVersion, rightVersion) {
  try {
    const [leftYaml, rightYaml] = await Promise.all([getVersionYaml(leftVersion), getVersionYaml(rightVersion)])
    const leftEntry = _versions.find(v => v.version === leftVersion)
    const rightEntry = _versions.find(v => v.version === rightVersion)
    const leftHeader = document.getElementById('diff-left-header')
    const rightHeader = document.getElementById('diff-right-header')
    if (leftHeader) leftHeader.textContent = versionHeaderLabel(leftEntry)
    if (rightHeader) rightHeader.textContent = versionHeaderLabel(rightEntry)
    _renderDiffPanes(leftYaml, rightYaml)
  } catch (e) {
    toast(`Diff error: ${e.message}`, 'error')
  }
}

async function _openVersionModal(version) {
  try {
    const yaml = await getVersionYaml(version)
    const entry = _versions.find(v => v.version === version)
    const title = document.getElementById('detail-version-modal-title')
    const subtitle = document.getElementById('detail-version-modal-subtitle')
    const pre = document.getElementById('detail-version-modal-pre')
    const copyBtn = document.getElementById('detail-version-copy-btn')
    const downloadBtn = document.getElementById('detail-version-download-btn')

    if (title) title.textContent = `Version v${version}`
    if (subtitle) subtitle.textContent = versionHeaderLabel(entry)
    if (pre) pre.textContent = yaml
    if (copyBtn) copyBtn.onclick = () => copyText(yaml, `Version v${version}`)
    if (downloadBtn) downloadBtn.onclick = () => downloadText(`${_name}.v${version}.yaml`, yaml, 'text/yaml;charset=utf-8')

    new bootstrap.Modal(document.getElementById('detail-version-modal')).show()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function getVersionYaml(version) {
  const cacheKey = `${_name}:${version}`
  if (_versionYamlCache.has(cacheKey)) return _versionYamlCache.get(cacheKey)
  const yaml = await api.versions.yaml(_name, version)
  _versionYamlCache.set(cacheKey, yaml)
  return yaml
}

function versionOptionLabel(version) {
  if (!version) return '—'
  return `v${version.version}${version.is_active ? ' · current' : ''} · ${fmtTimestamp(version.created_at)}`
}

function versionHeaderLabel(version) {
  if (!version) return '—'
  return `${versionOptionLabel(version)} (${relTime(version.created_at)})`
}

// ── Config tab ──────────────────────────────────────────────────────────────

async function loadConfig() {
  const pre = document.getElementById('detail-config-pre')
  if (!pre) return
  pre.textContent = 'Loading…'
  try {
    const p = await api.pipelines.get(_name)
    pre.textContent = p.yaml || p.raw || JSON.stringify(p, null, 2)
  } catch (e) { toast(e.message, 'error') }
}

// ── Alerts tab ──────────────────────────────────────────────────────────────

async function loadAlerts() {
  const wrap = document.getElementById('detail-alerts-wrap')
  if (!wrap) return
  try {
    const rules = await api.alerts.list(_name)
    _renderAlerts(wrap, rules)
  } catch (e) { toast(e.message, 'error') }
}

function _renderAlerts(wrap, rules) {
  wrap.innerHTML = `
    <div class="table-wrap">
      <div class="table-head-row">
        <span class="table-head-title">Alert Rules</span>
        <span class="count-pill">${rules.length} rule${rules.length !== 1 ? 's' : ''}</span>
        <button class="btn btn-sm btn-outline-primary table-head-action-btn ms-auto"
                type="button"
                data-action="add-alert"><i class="bi bi-plus"></i> Add Rule</button>
      </div>
      <table class="table mb-0">
        <thead><tr><th>Name</th><th>Condition</th><th>Action</th><th>Cooldown</th><th></th></tr></thead>
        <tbody>${rules.length
          ? rules.map(_alertRow).join('')
          : '<tr><td colspan="5" class="text-secondary text-center py-4">No alert rules defined</td></tr>'
        }</tbody>
      </table>
    </div>
  `
  bindDataActions(wrap, {
    'add-alert': () => {
      _openAlertModal(null, null, rules)
    },
    'edit-alert': (button) => {
      const idx = parseInt(button.dataset.index, 10)
      _openAlertModal(idx, rules[idx], rules)
    },
    'delete-alert': (button) => {
      void _deleteAlert(parseInt(button.dataset.index, 10), rules)
    },
  })
}

function _alertRow(r, i) {
  const actionStr = r.action === 'email'
    ? `email → ${esc(r.email_to || '?')}`
    : `webhook${r.webhook_url ? ' ✓' : ''}`
  return `<tr>
    <td class="fw-semibold">${esc(r.name || '—')}</td>
    <td class="mono-sm">${esc(r.condition || '')}</td>
    <td class="text-secondary">${actionStr}</td>
    <td class="text-secondary">${r.cooldown_seconds ?? 300}s</td>
    <td class="text-end">
      <button class="btn-flat" type="button" title="Edit" data-action="edit-alert" data-index="${i}"><i class="bi bi-pencil"></i></button>
      <button class="btn-flat-danger" type="button" title="Delete" data-action="delete-alert" data-index="${i}"><i class="bi bi-trash"></i></button>
    </td>
  </tr>`
}

function _openAlertModal(idx, rule, rules) {
  const isEdit = idx !== null
  document.getElementById('alert-modal-title').textContent = isEdit ? 'Edit Alert Rule' : 'Add Alert Rule'
  document.getElementById('alert-name').value        = rule?.name || ''
  document.getElementById('alert-condition').value   = rule?.condition || ''
  document.getElementById('alert-webhook-url').value = rule?.webhook_url || ''
  document.getElementById('alert-email-to').value    = rule?.email_to || ''
  document.getElementById('alert-subject').value     = rule?.subject || ''
  document.getElementById('alert-cooldown').value    = rule?.cooldown_seconds ?? 300

  const action = rule?.action || 'webhook'
  document.querySelector(`input[name="alert-action"][value="${action}"]`).checked = true
  _toggleAlertAction(action)
  document.querySelectorAll('input[name="alert-action"]').forEach(r => {
    r.onchange = () => _toggleAlertAction(r.value)
  })

  const modal = new bootstrap.Modal(document.getElementById('detail-alert-modal'))
  document.getElementById('alert-save-btn').onclick = async () => {
    const payload = {
      name:             document.getElementById('alert-name').value.trim() || null,
      condition:        document.getElementById('alert-condition').value.trim(),
      action:           document.querySelector('input[name="alert-action"]:checked').value,
      webhook_url:      document.getElementById('alert-webhook-url').value.trim() || null,
      email_to:         document.getElementById('alert-email-to').value.trim() || null,
      subject:          document.getElementById('alert-subject').value.trim() || null,
      cooldown_seconds: parseInt(document.getElementById('alert-cooldown').value) || 300,
    }
    if (!payload.condition) { toast('Condition is required', 'error'); return }
    try {
      if (isEdit) await api.alerts.update(_name, idx, payload)
      else        await api.alerts.create(_name, payload)
      modal.hide()
      toast(isEdit ? 'Rule updated' : 'Rule added')
      loadAlerts()
    } catch (e) { toast(e.message, 'error') }
  }
  modal.show()
}

function _toggleAlertAction(action) {
  document.getElementById('alert-webhook-row').classList.toggle('d-none', action !== 'webhook')
  document.getElementById('alert-email-row').classList.toggle('d-none',   action !== 'email')
}

// ── Connector test results toast ─────────────────────────────────────────────

function _showConnectorResults(result) {
  const lines = []
  const fmt = (label, r) => {
    const ok  = r?.ok
    const msg = ok ? (r.detail || 'OK') : (r.error || 'failed')
    const lat = r?.latency_ms != null ? ` (${r.latency_ms}ms)` : ''
    return `${ok ? '✓' : '✗'} ${label}: ${msg}${lat}`
  }
  if (result.source) lines.push(fmt(`source (${result.source.type})`, result.source))
  for (const s of (result.sinks || [])) lines.push(fmt(`sink (${s.type})`, s))
  if (result.error) lines.push(`Error: ${result.error}`)
  const allOk = (result.source?.ok !== false) && (result.sinks || []).every(s => s.ok !== false)
  toast(lines.join('\n') || 'No connectors found', allOk ? 'success' : 'error')
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtTimestamp(iso) {
  if (!iso) return '—'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())} ${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())} UTC`
}

function fmtInterval(s) {
  if (!s) return '?'
  if (s < 60)   return `${s}s`
  if (s < 3600) return `${s / 60}m`
  return `${s / 3600}h`
}

function scheduleLabel(p) {
  if (p.schedule_type === 'interval' && p.interval_seconds) {
    return `every ${fmtInterval(p.interval_seconds)}`
  }
  if (p.schedule_type === 'cron' && p.cron_expr) {
    return p.cron_expr
  }
  return p.schedule_type || '—'
}

function renderTransformFlow(transforms) {
  const items = Array.isArray(transforms) ? transforms.map(t => t.type || t).filter(Boolean) : []
  if (!items.length) return '—'

  const visible = items.slice(0, 3)
  const body = visible.map((item, index) => `
    ${index ? '<span class="detail-transform-arrow">→</span>' : ''}
    <span class="detail-transform-item">${esc(item)}</span>
  `).join('')
  const remaining = items.length - visible.length
  return `
    <div class="detail-transform-flow">
      ${body}
      ${remaining > 0 ? `<span class="count-pill">+${remaining} more</span>` : ''}
    </div>`
}

async function copyText(text, label = 'Text') {
  try {
    await navigator.clipboard.writeText(text)
    toast(`${label} copied`)
  } catch (_) {
    toast('Copy failed — use Ctrl+A / Ctrl+C', 'error')
  }
}

async function _copyVersion(version) {
  try {
    await copyText(await getVersionYaml(version), `Version v${version}`)
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function _downloadVersion(version) {
  try {
    downloadText(`${_name}.v${version}.yaml`, await getVersionYaml(version), 'text/yaml;charset=utf-8')
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function _compareVersion(version) {
  const target = resolveCompareTarget(version)
  await openCompareModal(version, target)
}

async function _rollbackVersion(version) {
  if (!confirm(`Rollback to version ${version}?`)) return
  try {
    await api.pipelines.rollback(_name, version)
    toast(`Rolled back to v${version}`)
    await init()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function _deleteAlert(index, rules) {
  const label = rules[index]?.name || `rule #${index}`
  if (!confirm(`Delete alert rule '${label}'?`)) return
  try {
    await api.alerts.delete(_name, index)
    toast(`Deleted ${label}`)
    loadAlerts()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function _monitorTriggeredRun(runId, token) {
  const run = await monitorTriggeredRun(runId, {
    isActive: () => token === _runMonitorToken && Boolean(document.getElementById('detail-run-btn')),
  })
  if (token !== _runMonitorToken || !document.getElementById('detail-run-btn')) return

  try {
    await init()
    const feedback = runOutcomeToast(run, { genericLabel: 'Run' })
    if (feedback) {
      toast(feedback.message, feedback.type)
    }
  } catch (e) {
    toast(`Refresh error: ${e.message}`, 'error')
  }
}
