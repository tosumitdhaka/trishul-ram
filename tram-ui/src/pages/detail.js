import { api } from '../api.js'
import { relTime, fmtDur, fmtNum, statusBadge, schedBadge, esc, toast } from '../utils.js'

let _name = null
let _activeYaml = null  // current active version YAML for diff comparison

export async function init() {
  _name = window._detailPipeline
  if (!_name) { navigate('pipelines'); return }

  const sub = document.getElementById('tb-sub')
  if (sub) sub.textContent = _name

  try {
    const [pipeline, runs] = await Promise.all([
      api.pipelines.get(_name),
      api.runs.list({ pipeline: _name, limit: 50 }),
    ])
    _activeYaml = pipeline.yaml || null
    renderCards(pipeline)
    renderRuns(runs)
    wireActions(pipeline)
  } catch (e) {
    toast(`Detail error: ${e.message}`, 'error')
  }

  // Tab switching — show/hide isolated panels
  document.querySelectorAll('#detail-tabs .nav-link').forEach(tab => {
    tab.addEventListener('click', e => {
      e.preventDefault()
      document.querySelectorAll('#detail-tabs .nav-link').forEach(t => t.classList.remove('active'))
      tab.classList.add('active')
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('d-none'))
      const t = tab.dataset.tab
      document.getElementById(`tab-panel-${t}`)?.classList.remove('d-none')
      if (t === 'runs')     reloadRuns()
      if (t === 'versions') loadVersions()
      if (t === 'config')   loadConfig()
      if (t === 'alerts')   loadAlerts()
    })
  })

  // Run filter
  document.getElementById('detail-runs-status')?.addEventListener('change', reloadRuns)
  document.getElementById('detail-runs-from')?.addEventListener('change',   reloadRuns)
}

function renderCards(p) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val }
  set('detail-source',    p.source?.type || '—')
  const sinks = Array.isArray(p.sinks) ? p.sinks.map(s => s.type || s).join(', ') : '—'
  set('detail-sinks',     sinks)
  set('detail-schedule',  p.schedule_type === 'interval' && p.interval_seconds
    ? `every ${fmtInterval(p.interval_seconds)}`
    : p.schedule_type || '—')
  set('detail-serializers', p.serializer || p.serializer_in || '—')
  const xforms = Array.isArray(p.transforms) ? p.transforms.map(t => t.type || t).join(', ') : '—'
  set('detail-transforms', xforms)
  set('detail-error',      p.error_policy || p.dlq ? 'DLQ enabled' : 'default')
}

function renderRuns(runs) {
  const tbody = document.getElementById('detail-runs-body')
  if (!tbody) return
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="text-secondary text-center py-4">No runs yet</td></tr>'
    return
  }
  tbody.innerHTML = runs.map(r => `<tr>
    <td style="width:28px"></td>
    <td class="mono" style="font-size:11px">${esc(String(r.run_id || r.id || '').slice(0,8))}</td>
    <td class="text-secondary">${esc(r.node || '—')}</td>
    <td class="text-secondary">${r.started_at ? relTime(r.started_at) : '—'}</td>
    <td class="text-secondary">${fmtDur(r.started_at, r.finished_at)}</td>
    <td class="num-in">${fmtNum(r.records_in)}</td>
    <td class="num-out">${fmtNum(r.records_out)}</td>
    <td class="text-secondary">${fmtNum(r.records_skipped)}</td>
    <td class="text-secondary">${fmtNum(r.dlq_count)}</td>
    <td>${statusBadge(r.status)}</td>
    <td class="text-secondary" style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.error||'')}">${esc(r.error || '')}</td>
  </tr>`).join('')
}

function wireActions(pipeline) {
  window._detailEdit = () => { window._editorPipeline = _name; navigate('editor') }
  const btn = document.getElementById('detail-run-btn')
  if (!btn) return
  const isRunning = pipeline.status === 'running'
  const isStream  = pipeline.schedule_type === 'stream'
  if (isRunning) {
    btn.innerHTML = '<i class="bi bi-stop-fill me-1"></i>Stop'
    btn.className = 'btn btn-sm btn-danger'
    btn.onclick = () => window._detailStop?.()
  } else {
    btn.innerHTML = `<i class="bi bi-play-fill me-1"></i>${isStream ? 'Start' : 'Run Now'}`
    btn.className = 'btn btn-sm btn-primary'
    btn.onclick = () => window._detailRun?.()
  }

  window._detailRun = async () => {
    try {
      if (isStream) { await api.pipelines.start(_name); toast(`Started ${_name}`) }
      else          { await api.pipelines.run(_name);   toast(`Triggered ${_name}`) }
      setTimeout(() => init(), 800)
    } catch (e) { toast(e.message, 'error') }
  }

  window._detailStop = async () => {
    try { await api.pipelines.stop(_name); toast(`Stopped ${_name}`); setTimeout(() => init(), 800) }
    catch (e) { toast(e.message, 'error') }
  }

  window._detailTestConnectors = async () => {
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

  window._detailDownload = async () => {
    try {
      const p = await api.pipelines.get(_name)
      const yaml = p.yaml || p.raw || JSON.stringify(p, null, 2)
      const a = document.createElement('a')
      a.href = 'data:text/yaml;charset=utf-8,' + encodeURIComponent(yaml)
      a.download = `${_name}.yaml`
      a.click()
    } catch (e) { toast(e.message, 'error') }
  }
}

async function reloadRuns() {
  const status = document.getElementById('detail-runs-status')?.value || ''
  const from   = document.getElementById('detail-runs-from')?.value   || ''
  const params = { pipeline: _name, limit: 100 }
  if (status) params.status = status
  if (from)   params.from   = from
  try {
    const runs = await api.runs.list(params)
    renderRuns(runs)
  } catch (e) { toast(e.message, 'error') }
}

async function loadVersions() {
  const tbody = document.getElementById('detail-versions-body')
  if (!tbody) return
  tbody.innerHTML = '<tr><td colspan="4" class="text-secondary text-center py-4">Loading versions…</td></tr>'
  try {
    const versions = await api.pipelines.versions(_name)
    if (!versions?.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-secondary text-center py-4">No versions saved</td></tr>'
      return
    }
    tbody.innerHTML = versions.map(v => `<tr>
      <td class="mono fw-semibold">v${v.version}</td>
      <td class="text-secondary">${v.created_at ? relTime(v.created_at) : '—'}</td>
      <td>${v.is_active ? statusBadge('running') : ''}</td>
      <td class="text-end">
        <button class="btn-flat" onclick="window._detailDiff(${v.version})" title="View diff">
          <i class="bi bi-file-diff"></i>
        </button>
        <button class="btn-flat" onclick="window._detailRollback(${v.version})" title="Rollback to this version">
          <i class="bi bi-arrow-counterclockwise"></i>
        </button>
      </td>
    </tr>`).join('')

    window._detailRollback = async (ver) => {
      if (!confirm(`Rollback to version ${ver}?`)) return
      try { await api.pipelines.rollback(_name, ver); toast('Rolled back'); init() }
      catch (e) { toast(e.message, 'error') }
    }

    window._detailDiff = async (ver) => {
      try {
        const oldYaml = await api.versions.yaml(_name, ver)
        const newYaml = _activeYaml || await api.pipelines.get(_name).then(p => p.yaml || '')
        const versionEntry = versions.find(v => v.version === ver)
        const leftLabel  = `v${ver}${versionEntry?.created_at ? ' · ' + relTime(versionEntry.created_at) : ''}`
        const rightLabel = 'active'
        document.getElementById('diff-left-label').textContent  = leftLabel
        document.getElementById('diff-right-label').textContent = rightLabel
        _renderDiffPanes(oldYaml, newYaml)
        new bootstrap.Modal(document.getElementById('detail-diff-modal')).show()
      } catch (e) { toast(`Diff error: ${e.message}`, 'error') }
    }
  } catch (e) { toast(e.message, 'error') }
}

// ── Myers diff ──────────────────────────────────────────────────────────────

function _myersDiff(a, b) {
  const m = a.length, n = b.length
  const max = m + n
  const v = new Array(2 * max + 1).fill(0)
  const trace = []

  for (let d = 0; d <= max; d++) {
    trace.push([...v])
    for (let k = -d; k <= d; k += 2) {
      let x = (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max]))
        ? v[k + 1 + max]
        : v[k - 1 + max] + 1
      let y = x - k
      while (x < m && y < n && a[x] === b[y]) { x++; y++ }
      v[k + max] = x
      if (x >= m && y >= n) return _backtrack(trace, a, b, max)
    }
  }
  return _backtrack(trace, a, b, max)
}

function _backtrack(trace, a, b, max) {
  const result = []
  let x = a.length, y = b.length
  for (let d = trace.length - 1; d >= 0; d--) {
    const v = trace[d]
    const k = x - y
    const prevK = (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max])) ? k + 1 : k - 1
    const prevX = v[prevK + max]
    const prevY = prevX - prevK
    while (x > prevX && y > prevY) { result.unshift({ type: 'equal', line: a[x - 1] }); x--; y-- }
    if (d > 0) {
      if (x > prevX) { result.unshift({ type: 'delete', line: a[x - 1] }); x-- }
      else            { result.unshift({ type: 'insert', line: b[y - 1] }); y-- }
    }
  }
  return result
}

function _renderDiffPanes(oldYaml, newYaml) {
  const hunks = _myersDiff(oldYaml.split('\n'), newYaml.split('\n'))
  let leftHtml = '', rightHtml = ''
  for (const h of hunks) {
    const line = esc(h.line)
    if (h.type === 'equal') {
      leftHtml  += `<span style="opacity:.5">${line}\n</span>`
      rightHtml += `<span style="opacity:.5">${line}\n</span>`
    } else if (h.type === 'delete') {
      leftHtml  += `<span style="background:#3d1a1a;display:block">- ${line}\n</span>`
    } else {
      rightHtml += `<span style="background:#1a3328;display:block">+ ${line}\n</span>`
    }
  }
  document.getElementById('diff-left-pane').innerHTML  = leftHtml  || '<span style="opacity:.4">— empty —</span>'
  document.getElementById('diff-right-pane').innerHTML = rightHtml || '<span style="opacity:.4">— empty —</span>'
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
    <div class="table-head-row">
      <span class="table-head-title">Alert Rules</span>
      <span class="count-pill">${rules.length} rule${rules.length !== 1 ? 's' : ''}</span>
      <button class="btn btn-sm btn-outline-primary ms-auto" style="font-size:11px;padding:2px 8px"
              onclick="window._alertAdd()"><i class="bi bi-plus"></i> Add Rule</button>
    </div>
    <table class="table mb-0">
      <thead><tr><th>Name</th><th>Condition</th><th>Action</th><th>Cooldown</th><th></th></tr></thead>
      <tbody>${rules.length
        ? rules.map(_alertRow).join('')
        : '<tr><td colspan="5" class="text-secondary text-center py-4">No alert rules defined</td></tr>'
      }</tbody>
    </table>`

  window._alertAdd  = ()    => _openAlertModal(null, null, rules)
  window._alertEdit = (idx) => _openAlertModal(idx, rules[idx], rules)
  window._alertDelete = async (idx) => {
    const label = rules[idx]?.name || `rule #${idx}`
    if (!confirm(`Delete alert rule '${label}'?`)) return
    try {
      await api.alerts.delete(_name, idx)
      toast(`Deleted ${label}`)
      loadAlerts()
    } catch (e) { toast(e.message, 'error') }
  }
}

function _alertRow(r, i) {
  const actionStr = r.action === 'email'
    ? `email → ${esc(r.email_to || '?')}`
    : `webhook${r.webhook_url ? ' ✓' : ''}`
  return `<tr>
    <td class="fw-semibold">${esc(r.name || '—')}</td>
    <td class="mono" style="font-size:12px">${esc(r.condition || '')}</td>
    <td class="text-secondary">${actionStr}</td>
    <td class="text-secondary">${r.cooldown_seconds ?? 300}s</td>
    <td class="text-end">
      <button class="btn-flat" title="Edit" onclick="window._alertEdit(${i})"><i class="bi bi-pencil"></i></button>
      <button class="btn-flat-danger" title="Delete" onclick="window._alertDelete(${i})"><i class="bi bi-trash"></i></button>
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

function fmtInterval(s) {
  if (!s) return '?'
  if (s < 60)   return `${s}s`
  if (s < 3600) return `${s / 60}m`
  return `${s / 3600}h`
}
