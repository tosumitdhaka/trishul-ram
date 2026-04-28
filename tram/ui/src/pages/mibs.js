import { api } from '../api.js'
import { esc, toast } from '../utils.js'

export async function init() {
  await loadMibs()
  wireDropZone()

  window._mibsUpload = async () => {
    const files = Array.from(document.getElementById('mib-file-input')?.files || [])
    if (!files.length) { toast('Select at least one MIB source file first', 'error'); return }
    const autoFetch = Boolean(document.getElementById('mib-upload-auto-fetch')?.checked)
    let ok = 0, fail = 0
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
      }
      catch (e) { fail++; toast(`${file.name}: ${e.message}`, 'error') }
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

  window._mibsDownload = async () => {
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

  window._mibDelete = async (name) => {
    if (!confirm(`Delete MIB "${name}"?`)) return
    try { await api.mibs.delete(name); toast('Deleted'); await loadMibs() }
    catch (e) { toast(e.message, 'error') }
  }
}

async function loadMibs() {
  try {
    const mibs = await api.mibs.list()
    renderMibs(mibs)
    const el = document.getElementById('mibs-count')
    if (el) el.textContent = mibs.length
  } catch (e) {
    toast(`MIBs error: ${e.message}`, 'error')
  }
}

function renderMibs(mibs) {
  const tbody = document.getElementById('mibs-body')
  if (!tbody) return
  if (!mibs.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-secondary text-center py-4">No MIBs compiled</td></tr>'
    return
  }
  tbody.innerHTML = mibs.map(m => {
    const name = m.name || m
    const file = m.file || '—'
    const size = m.size_bytes ? fmtSize(m.size_bytes) : '—'
    return `<tr>
      <td class="fw-semibold">${esc(name)}</td>
      <td class="text-secondary font-monospace" style="font-size:12px">${esc(file)}</td>
      <td class="text-secondary">${size}</td>
      <td class="text-end">
        <button class="btn-flat-danger" title="Delete" onclick="window._mibDelete('${esc(name)}')"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
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
