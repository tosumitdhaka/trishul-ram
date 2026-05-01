import { api } from '../api.js'
import { bindDataActions, downloadText, getSavedPollIntervalMs, relTime, statusBadge, schedBadge, esc, toast, pipelineStartFeedback } from '../utils.js'
import { monitorTriggeredRun, runOutcomeToast } from '../run_monitor.js'
import { filterTemplates, normalizeTemplates, populateTemplateFilters, templateFlowText, templateScheduleClass } from './template_helpers.js'
import * as bootstrap from 'bootstrap'

let _all = []
let _templates = []
let _pollTimer = null
let _runMonitorToken = 0
let _pendingImportYaml = null
let _pendingImportName = null

export async function init() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  _pollTimer = setInterval(() => {
    if (!document.getElementById('pl-table')) { clearInterval(_pollTimer); _pollTimer = null; return }
    refresh().catch((e) => toast(e.message, 'error'))
  }, getSavedPollIntervalMs())

  wireToolbar()
  wireTableActions()
  wireImportFlow()
  wireTemplateFlow()

  try {
    _all = await api.pipelines.list()
    renderTable(filteredPipelines())
    _maybeOpenTemplatesFromRouteAlias()
  } catch (e) {
    toast(`Pipelines error: ${e.message}`, 'error')
  }
}

async function refresh() {
  _all = await api.pipelines.list()
  renderTable(filteredPipelines())
}

function wireToolbar() {
  document.getElementById('pl-search')?.addEventListener('input', () => renderTable(filteredPipelines()))
  document.getElementById('pl-status')?.addEventListener('change', () => renderTable(filteredPipelines()))
  document.getElementById('pl-type')?.addEventListener('change', () => renderTable(filteredPipelines()))
  document.getElementById('pl-new-btn')?.addEventListener('click', openNewPipeline)
  document.getElementById('pl-import-btn')?.addEventListener('click', () => document.getElementById('pl-import-input')?.click())
  document.getElementById('pl-import-input')?.addEventListener('change', handleImportSelection)
  document.getElementById('pl-refresh-btn')?.addEventListener('click', refreshWithSpinner)
  document.getElementById('pl-reload-btn')?.addEventListener('click', reloadPipelines)
}

function wireTableActions() {
  const tbody = document.getElementById('pl-body')
  if (!tbody) return

  bindDataActions(tbody, {
    start: async (button, event) => {
      event.stopPropagation()
      await startPipeline(button.dataset.name)
    },
    stop: async (button, event) => {
      event.stopPropagation()
      await stopPipeline(button.dataset.name)
    },
    run: async (button, event) => {
      event.stopPropagation()
      await runPipeline(button.dataset.name)
    },
    edit: (button, event) => {
      event.stopPropagation()
      editPipeline(button.dataset.name)
    },
    download: async (button, event) => {
      event.stopPropagation()
      await downloadPipeline(button.dataset.name)
    },
    delete: async (button, event) => {
      event.stopPropagation()
      await deletePipeline(button.dataset.name)
    },
  })

  if (tbody._tramRowClickListener) {
    tbody.removeEventListener('click', tbody._tramRowClickListener)
  }
  const rowClickListener = (event) => {
    if (event.target.closest('[data-action]')) return
    const row = event.target.closest('tr[data-pipeline-name]')
    if (!row || !tbody.contains(row)) return
    openPipelineDetail(row.dataset.pipelineName)
  }
  tbody.addEventListener('click', rowClickListener)
  tbody._tramRowClickListener = rowClickListener
}

function wireImportFlow() {
  document.getElementById('pl-import-replace').onclick = async () => {
    if (!_pendingImportYaml || !_pendingImportName) return
    const modal = bootstrap.Modal.getInstance(document.getElementById('pl-import-modal'))
    modal?.hide()
    try {
      await api.pipelines.update(_pendingImportName, _pendingImportYaml)
      toast(`Updated ${_pendingImportName}`)
      await refresh()
    } catch (e) {
      toast(e.message, 'error')
    }
  }

  document.getElementById('pl-import-rename').onclick = async () => {
    if (!_pendingImportYaml) return
    const newName = document.getElementById('pl-import-newname').value.trim()
    if (!newName) { toast('Enter a new name', 'error'); return }
    const modal = bootstrap.Modal.getInstance(document.getElementById('pl-import-modal'))
    modal?.hide()
    const patched = _patchName(_pendingImportYaml, newName)
    try {
      await api.pipelines.create(patched)
      toast(`Imported as ${newName}`)
      await refresh()
    } catch (e) {
      toast(e.message, 'error')
    }
  }
}

function wireTemplateFlow() {
  const modalEl = document.getElementById('pl-templates-modal')
  if (modalEl) {
    if (modalEl._tramShowListener) {
      modalEl.removeEventListener('show.bs.modal', modalEl._tramShowListener)
    }
    const showListener = () => {
      showTemplateListView()
      resetTemplateFilters()
      openTemplates()
    }
    modalEl.addEventListener('show.bs.modal', showListener)
    modalEl._tramShowListener = showListener
  }

  document.getElementById('pl-tpl-search')?.addEventListener('input', renderTemplateList)
  document.getElementById('pl-tpl-source')?.addEventListener('change', renderTemplateList)
  document.getElementById('pl-tpl-sink')?.addEventListener('change', renderTemplateList)
  document.getElementById('pl-tpl-schedule')?.addEventListener('change', renderTemplateList)
  document.getElementById('pl-tpl-back-btn')?.addEventListener('click', showTemplateListView)
  document.getElementById('pl-tpl-deploy-view-btn')?.addEventListener('click', () => {
    const button = document.getElementById('pl-tpl-deploy-view-btn')
    const id = button?.dataset.templateId
    if (!id) return
    const template = _templates.find(entry => entry.id === id)
    if (template) doTemplateDeploy(template)
  })

  bindDataActions(document.getElementById('pl-tpl-body'), {
    view: (button) => showTemplateYamlView(button.dataset.templateId),
    deploy: (button) => {
      const template = _templates.find(entry => entry.id === button.dataset.templateId)
      if (template) doTemplateDeploy(template)
    },
  })
}

async function refreshWithSpinner() {
  const btn = document.getElementById('pl-refresh-btn')
  const icon = document.getElementById('pl-refresh-icon')
  if (btn) btn.disabled = true
  if (icon) icon.className = 'bi bi-arrow-clockwise spin'
  try {
    await refresh()
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) btn.disabled = false
    if (icon) icon.className = 'bi bi-arrow-clockwise'
  }
}

async function reloadPipelines() {
  const btn = document.getElementById('pl-reload-btn')
  const icon = document.getElementById('pl-reload-icon')
  if (btn) btn.disabled = true
  if (icon) icon.className = 'bi bi-arrow-repeat spin'
  try {
    await api.pipelines.reload()
    toast('Pipelines reloaded')
    await refresh()
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) btn.disabled = false
    if (icon) icon.className = 'bi bi-arrow-repeat'
  }
}

function openNewPipeline() {
  window._editorReturn = 'pipelines'
  window._editorPipeline = null
  window._editorYaml = null
  navigate('editor')
}

function openPipelineDetail(name) {
  window._detailPipeline = name
  navigate('detail')
}

function editPipeline(name) {
  window._editorReturn = 'pipelines'
  window._editorPipeline = name
  navigate('editor')
}

async function startPipeline(name) {
  try {
    const result = await api.pipelines.start(name)
    const feedback = pipelineStartFeedback(name, result)
    toast(feedback.message, feedback.type)
    await refresh()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function stopPipeline(name) {
  try {
    await api.pipelines.stop(name)
    toast(`Stopped ${name}`)
    await refresh()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function runPipeline(name) {
  try {
    const result = await api.pipelines.run(name)
    await refresh()
    if (result?.run_id) {
      const token = ++_runMonitorToken
      void _monitorTriggeredRun(name, result.run_id, token).catch((err) => {
        toast(`Run monitor error: ${err.message}`, 'error')
      })
    }
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function deletePipeline(name) {
  if (!confirm(`Delete pipeline "${name}"?`)) return
  try {
    await api.pipelines.delete(name)
    toast(`Deleted ${name}`)
    _all = _all.filter(pipeline => pipeline.name !== name)
    renderTable(filteredPipelines())
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function downloadPipeline(name) {
  try {
    const pipeline = await api.pipelines.get(name)
    const yaml = pipeline.yaml || pipeline.raw || JSON.stringify(pipeline, null, 2)
    downloadText(`${name}.yaml`, yaml, 'text/yaml;charset=utf-8')
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function handleImportSelection(event) {
  const input = event.currentTarget
  const file = input?.files?.[0]
  if (!file) return
  input.value = ''
  const yaml = await file.text()
  const name = _extractName(yaml)
  const exists = _all.some(pipeline => pipeline.name === name)
  if (!exists) {
    try {
      await api.pipelines.create(yaml)
      toast(`Imported ${name}`)
      await refresh()
    } catch (e) {
      toast(e.message, 'error')
    }
    return
  }

  _pendingImportYaml = yaml
  _pendingImportName = name
  document.getElementById('pl-import-name').textContent = name
  const renameInput = document.getElementById('pl-import-newname')
  if (renameInput) renameInput.value = ''
  new bootstrap.Modal(document.getElementById('pl-import-modal')).show()
}

async function openTemplates() {
  const tbody = document.getElementById('pl-tpl-body')
  const count = document.getElementById('pl-tpl-count')
  if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-4">Loading…</td></tr>'
  if (count) count.textContent = ''
  try {
    _templates = normalizeTemplates(await api.templates.list())
    populateTemplateFilters(_templates, {
      sourceEl: document.getElementById('pl-tpl-source'),
      sinkEl: document.getElementById('pl-tpl-sink'),
    })
    renderTemplateList()
  } catch (e) {
    toast(e.message, 'error')
  }
}

function renderTemplateList() {
  const tbody = document.getElementById('pl-tpl-body')
  const count = document.getElementById('pl-tpl-count')
  if (!tbody) return
  const templates = filteredTemplates()
  if (count) count.textContent = `${templates.length} template${templates.length !== 1 ? 's' : ''}`
  if (!templates.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-4">No templates match</td></tr>'
    return
  }
  tbody.innerHTML = templates.map(template => `
    <tr>
      <td class="fw-semibold">${esc(template.name)}</td>
      <td class="mono table-cell-compact">${esc(templateFlowText(template))}</td>
      <td><span class="tram-badge ${templateScheduleClass(template.schedule_type)}">${esc(template.schedule_type)}</span></td>
      <td class="text-secondary table-cell-compact table-cell-truncate" title="${esc(template.description || '')}">${esc(template.description || '—')}</td>
      <td class="text-end table-actions-nowrap">
        <button class="btn btn-sm btn-outline-secondary detail-action-btn" type="button" title="View YAML" data-action="view" data-template-id="${esc(template.id)}">
          <i class="bi bi-eye"></i><span>View</span>
        </button>
        <button class="btn btn-sm btn-primary detail-action-btn" type="button" data-action="deploy" data-template-id="${esc(template.id)}">
          <i class="bi bi-rocket-takeoff"></i><span>Deploy</span>
        </button>
      </td>
    </tr>
  `).join('')
}

function showTemplateYamlView(id) {
  const template = _templates.find(entry => entry.id === id)
  if (!template) return
  document.getElementById('pl-tpl-yaml-name').textContent = template.name
  document.getElementById('pl-tpl-yaml-body').textContent = template.yaml || ''
  const deployBtn = document.getElementById('pl-tpl-deploy-view-btn')
  if (deployBtn) deployBtn.dataset.templateId = id
  document.getElementById('pl-tpl-list-view')?.classList.add('d-none')
  document.getElementById('pl-tpl-yaml-view')?.classList.remove('d-none')
}

function showTemplateListView() {
  document.getElementById('pl-tpl-list-view')?.classList.remove('d-none')
  document.getElementById('pl-tpl-yaml-view')?.classList.add('d-none')
}

function doTemplateDeploy(template) {
  document.querySelectorAll('.modal-backdrop').forEach(el => el.remove())
  document.body.classList.remove('modal-open')
  document.body.style.removeProperty('overflow')
  document.body.style.removeProperty('padding-right')
  window._editorReturn = 'pipelines'
  window._editorYaml = template.yaml
  window._editorPipeline = null
  navigate('editor')
  toast(`Template "${template.name}" loaded — edit name and connection details, then save`)
}

function _maybeOpenTemplatesFromRouteAlias() {
  const modalEl = document.getElementById('pl-templates-modal')
  if (!modalEl || !window._openPipelinesTemplatesModal) return
  window._openPipelinesTemplatesModal = false
  bootstrap.Modal.getOrCreateInstance(modalEl).show()
}

function filteredPipelines() {
  const q = (document.getElementById('pl-search')?.value || '').toLowerCase()
  const st = document.getElementById('pl-status')?.value || ''
  const ty = document.getElementById('pl-type')?.value || ''
  return _all.filter(pipeline =>
    (!q || pipeline.name.toLowerCase().includes(q)) &&
    (!st || pipeline.status === st) &&
    (!ty || pipeline.schedule_type === ty)
  )
}

function _extractName(yaml) {
  const m = yaml.match(/^\s*name:\s*(\S+)/m)
  return m ? m[1] : 'unknown'
}

function _patchName(yaml, newName) {
  return yaml.replace(/^(\s*name:\s*)\S+/m, `$1${newName}`)
}

function resetTemplateFilters() {
  const search = document.getElementById('pl-tpl-search')
  const source = document.getElementById('pl-tpl-source')
  const sink = document.getElementById('pl-tpl-sink')
  const schedule = document.getElementById('pl-tpl-schedule')
  if (search) search.value = ''
  if (source) source.innerHTML = '<option value="">All sources</option>'
  if (sink) sink.innerHTML = '<option value="">All sinks</option>'
  if (schedule) schedule.value = ''
}

function filteredTemplates() {
  return filterTemplates(_templates, {
    query: document.getElementById('pl-tpl-search')?.value || '',
    source: document.getElementById('pl-tpl-source')?.value || '',
    sink: document.getElementById('pl-tpl-sink')?.value || '',
    schedule: document.getElementById('pl-tpl-schedule')?.value || '',
  })
}

function renderTable(pipelines) {
  const tbody = document.getElementById('pl-body')
  if (!tbody) return
  if (!pipelines.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-secondary text-center py-4">No pipelines found</td></tr>'
    return
  }
  tbody.innerHTML = pipelines.map(p => {
    const isActive = p.status === 'running' || p.status === 'scheduled'
    const isManual = p.schedule_type === 'manual'
    const actionBtn = isActive
      ? `<button class="btn-flat-danger" type="button" title="Stop" data-action="stop" data-name="${esc(p.name)}"><i class="bi bi-stop-fill"></i></button>`
      : isManual
        ? `<button class="btn-flat-primary" type="button" title="Run now" data-action="run" data-name="${esc(p.name)}"><i class="bi bi-play-fill"></i></button>`
        : `<button class="btn-flat-primary" type="button" title="Start" data-action="start" data-name="${esc(p.name)}"><i class="bi bi-play-fill"></i></button>`
    const sinks = Array.isArray(p.sinks) ? p.sinks.map(s => esc(s.type || s)).join(', ') : '—'
    return `<tr class="table-row-link" data-pipeline-name="${esc(p.name)}">
      <td class="fw-semibold">${esc(p.name)}</td>
      <td class="text-secondary">${esc(p.source?.type || '—')}</td>
      <td class="text-secondary">${sinks}</td>
      <td>${schedBadge(p)}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="text-secondary">${p.last_run ? relTime(p.last_run) : '—'}</td>
      <td>${p.last_run_status ? statusBadge(p.last_run_status) : '—'}</td>
      <td class="text-end">
        ${actionBtn}
        <button class="btn-flat" type="button" title="Edit" data-action="edit" data-name="${esc(p.name)}"><i class="bi bi-pencil"></i></button>
        <button class="btn-flat" type="button" title="Export YAML" data-action="download" data-name="${esc(p.name)}"><i class="bi bi-download"></i></button>
        <button class="btn-flat-danger" type="button" title="Delete" data-action="delete" data-name="${esc(p.name)}"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
}

async function _monitorTriggeredRun(name, runId, token) {
  const run = await monitorTriggeredRun(runId, {
    isActive: () => token === _runMonitorToken && Boolean(document.getElementById('pl-table')),
  })
  if (token !== _runMonitorToken || !document.getElementById('pl-table')) return

  await refresh()
  const feedback = runOutcomeToast(run, { name })
  if (feedback) {
    toast(feedback.message, feedback.type)
  }
}
