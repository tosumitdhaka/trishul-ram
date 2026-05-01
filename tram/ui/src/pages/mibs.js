import { api } from '../api.js'
import { bindDataActions, downloadText, esc, toast } from '../utils.js'

let _allMibs = []

export async function init() {
  wireDropZone()
  document.getElementById('mibs-search')?.addEventListener('input', applyMibFilter)
  document.getElementById('mib-upload-btn')?.addEventListener('click', uploadMibs)
  document.getElementById('mib-download-btn')?.addEventListener('click', downloadRemoteMibs)
  bindDataActions(document.getElementById('mibs-body'), {
    delete: async (button) => deleteMib(button.dataset.name),
    downloadCompiled: async (button) => downloadCompiled(button.dataset.name),
    downloadRaw: async (button) => downloadRaw(button.dataset.name),
  })
  await loadMibs()
}

async function loadMibs() {
  try {
    _allMibs = await api.mibs.list()
    applyMibFilter()
  } catch (e) {
    toast(`MIBs error: ${e.message}`, 'error')
  }
}

function applyMibFilter() {
  const query = (document.getElementById('mibs-search')?.value || '').trim().toLowerCase()
  const visible = _allMibs.filter((mib) => mibMatches(mib, query))
  renderMibs(visible)
  updateCounts(visible.length, _allMibs.length)
}

function renderMibs(mibs) {
  const tbody = document.getElementById('mibs-body')
  if (!tbody) return
  if (!mibs.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-secondary text-center py-4">${
      _allMibs.length && queryActive()
        ? 'No managed MIBs match your search'
        : 'No managed MIBs yet'
    }</td></tr>`
    return
  }
  tbody.innerHTML = mibs.map(m => {
    const name = m.name || m
    const raw = renderArtifactCell({
      file: m.raw_file,
      sizeBytes: m.raw_size_bytes,
      origin: m.raw_origin,
      available: m.raw_available,
      emptyLabel: '—',
      action: 'downloadRaw',
      actionValue: name,
      title: `Download raw source for ${name}`,
    })
    const compiled = renderArtifactCell({
      file: m.compiled_file,
      sizeBytes: m.compiled_size_bytes,
      origin: null,
      available: m.compiled_available,
      emptyLabel: '—',
      action: 'downloadCompiled',
      actionValue: name,
      title: `Download compiled module for ${name}`,
    })
    return `<tr>
      <td class="fw-semibold">${esc(name)}</td>
      <td>${raw}</td>
      <td>${compiled}</td>
      <td class="text-end">
        <button class="btn-flat-danger" type="button" title="Delete" data-action="delete" data-name="${esc(name)}"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
}

async function uploadMibs() {
  const files = Array.from(document.getElementById('mib-file-input')?.files || [])
  if (!files.length) { toast('Select at least one MIB source file first', 'error'); return }
  const autoFetch = Boolean(document.getElementById('mib-upload-auto-fetch')?.checked)
  let ok = 0
  let fail = 0
  let compiledModules = 0
  setUploadBusy(true, `Preparing ${files.length} file${files.length > 1 ? 's' : ''}...`)
  for (const [index, file] of files.entries()) {
    setUploadBusy(true, `Uploading ${index + 1}/${files.length}: ${file.name}`)
    try {
      const res = await api.mibs.upload(file, { resolveMissing: autoFetch })
      const failure = getUploadFailureMessage(res)
      if (failure) {
        fail++
        setUploadBusy(true, `${file.name}: ${failure}`)
        toast(`${file.name}: ${failure}`, 'error')
        continue
      }
      ok++
      compiledModules += res.compiled?.length || 0
      setUploadBusy(true, summarizeUploadResult(file.name, res))
    } catch (e) {
      fail++
      toast(`${file.name}: ${e.message}`, 'error')
    }
  }
  document.getElementById('mib-file-input').value = ''
  const summary = [
    ok ? `processed ${ok} file${ok > 1 ? 's' : ''}` : '',
    ok ? `compiled ${compiledModules} module${compiledModules === 1 ? '' : 's'}` : '',
    fail ? `${fail} failed` : '',
  ].filter(Boolean).join(' · ')
  setUploadBusy(false, summary)
  if (ok) {
    toast(`Uploaded ${ok} file${ok > 1 ? 's' : ''}; compiled ${compiledModules} module${compiledModules === 1 ? '' : 's'}${fail ? `; ${fail} failed` : ''}`)
    await loadMibs()
  }
}

async function downloadRemoteMibs() {
  const raw = document.getElementById('mib-download-names')?.value || ''
  const names = raw.split('\n').map(s => s.trim()).filter(Boolean)
  if (!names.length) { toast('Enter at least one MIB name', 'error'); return }
  setDownloadBusy(true, `Downloading ${names.length} MIB${names.length > 1 ? 's' : ''}...`)
  try {
    const res = await api.mibs.download(names)
    const compiled = res.compiled?.length || 0
    setDownloadBusy(false, `Downloaded ${names.length} request${names.length === 1 ? '' : 's'}; compiled ${compiled} module${compiled === 1 ? '' : 's'}`)
    toast(`Downloaded ${names.length} request${names.length === 1 ? '' : 's'}; compiled ${compiled} module${compiled === 1 ? '' : 's'}`)
    await loadMibs()
  } catch (e) {
    setDownloadBusy(false, '')
    toast(e.message, 'error')
  }
}

async function deleteMib(name) {
  if (!name) return
  if (!confirm(`Delete MIB "${name}"?`)) return
  try {
    await api.mibs.delete(name)
    toast('Deleted')
    await loadMibs()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function downloadCompiled(name) {
  if (!name) return
  try {
    const text = await api.mibs.get(name)
    downloadText(`${name}.py`, text)
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function downloadRaw(name) {
  if (!name) return
  try {
    const text = await api.mibs.source(name)
    downloadText(`${name}.mib`, text)
  } catch (e) {
    toast(e.message, 'error')
  }
}

function wireDropZone() {
  const zone  = document.getElementById('mib-drop-zone')
  const input = document.getElementById('mib-file-input')
  if (!zone || !input) return

  zone.addEventListener('click', () => input.click())
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover') })
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'))
  zone.addEventListener('drop', e => {
    e.preventDefault()
    zone.classList.remove('dragover')
    const files = e.dataTransfer.files
    if (files?.length) {
      input.files = files
      updateHint(zone, files)
    }
  })
  input.addEventListener('change', () => updateHint(zone, input.files))
}

function setUploadBusy(busy, message = '') {
  const btn = document.getElementById('mib-upload-btn')
  const status = document.getElementById('mib-upload-status')
  const input = document.getElementById('mib-file-input')
  const toggle = document.getElementById('mib-upload-auto-fetch')
  if (btn) {
    btn.disabled = busy
    btn.innerHTML = busy
      ? '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Uploading &amp; Compiling...'
      : 'Upload &amp; Compile'
  }
  if (input) input.disabled = busy
  if (toggle) toggle.disabled = busy
  if (status) {
    status.textContent = message
    status.classList.toggle('d-none', !message)
  }
}

function setDownloadBusy(busy, message = '') {
  const btn = document.getElementById('mib-download-btn')
  const status = document.getElementById('mib-download-status')
  const textarea = document.getElementById('mib-download-names')
  if (btn) {
    btn.disabled = busy
    btn.innerHTML = busy
      ? '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Downloading &amp; Compiling...'
      : 'Download &amp; Compile'
  }
  if (textarea) textarea.disabled = busy
  if (status) {
    status.textContent = message
    status.classList.toggle('d-none', !message)
  }
}

function summarizeUploadResult(filename, result) {
  const compiled = result?.compiled?.length || 0
  const builtin = result?.builtin_imports?.length || 0
  const local = result?.local_imports?.length || 0
  const resolved = result?.resolved_imports?.length || 0
  const unresolved = result?.unresolved_imports || []
  const status = result?.target_status || ''
  const suffix = [
    status === 'compiled' ? `${compiled || 1} compiled` : '',
    status === 'builtin_available' ? 'already available in bundled runtime MIBs' : '',
    status === 'already_available' ? 'already available locally' : '',
    builtin ? `${builtin} builtin` : '',
    local ? `${local} local` : '',
    resolved ? `${resolved} resolved during compile` : '',
    unresolved.length ? `unresolved: ${formatImportList(unresolved)}` : '',
    status === 'compile_failed' ? 'compile failed' : '',
    result?.resolve_missing ? 'remote dependency lookup enabled' : '',
  ].filter(Boolean).join(' · ')
  return suffix ? `${filename}: ${suffix}` : `${filename}: upload completed`
}

function getUploadFailureMessage(result) {
  const unresolved = result?.unresolved_imports || []
  const status = result?.target_status || ''
  if (unresolved.length) {
    return `unresolved dependencies: ${formatImportList(unresolved)}`
  }
  if (status === 'compile_failed') {
    return 'compile failed'
  }
  return ''
}

function formatImportList(names, limit = 3) {
  if (!names?.length) return ''
  if (names.length <= limit) return names.join(', ')
  return `${names.slice(0, limit).join(', ')} +${names.length - limit} more`
}

function updateHint(zone, files) {
  const hint = zone.querySelectorAll('.text-secondary')[0]
  if (!hint) return
  hint.textContent = files.length > 1 ? `${files.length} files selected` : files[0]?.name || ''
}

function fmtSize(bytes) {
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`
  if (bytes >= 1024)    return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

function renderArtifactCell({ file, sizeBytes, origin, available, emptyLabel, action, actionValue, title }) {
  if (!available) return `<span class="text-secondary">${emptyLabel}</span>`
  const name = file || 'available'
  const parts = [
    action
      ? `<button type="button" class="artifact-link font-monospace" title="${esc(title || name)}" data-action="${esc(action)}" data-name="${esc(actionValue || name)}">${esc(name)}</button>`
      : `<div class="text-secondary font-monospace artifact-text">${esc(name)}</div>`,
  ]
  const meta = [
    sizeBytes ? fmtSize(sizeBytes) : '',
    origin ? origin : '',
  ].filter(Boolean).join(' · ')
  if (meta) {
    parts.push(`<div class="detail-muted-small">${esc(meta)}</div>`)
  }
  return parts.join('')
}

function mibMatches(mib, query) {
  if (!query) return true
  const haystack = [
    mib?.name || mib,
    mib?.raw_file,
    mib?.compiled_file,
    mib?.raw_origin,
  ].filter(Boolean).join(' ').toLowerCase()
  return haystack.includes(query)
}

function updateCounts(visible, total) {
  const count = document.getElementById('mibs-count')
  const status = document.getElementById('mibs-filter-status')
  if (count) count.textContent = visible === total ? String(total) : `${visible}/${total}`
  if (status) {
    status.textContent = queryActive() && total
      ? `${visible} of ${total} visible`
      : `${total} managed module${total === 1 ? '' : 's'}`
  }
}

function queryActive() {
  return Boolean((document.getElementById('mibs-search')?.value || '').trim())
}
