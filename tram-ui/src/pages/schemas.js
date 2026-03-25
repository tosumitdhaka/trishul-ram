import { api } from '../api.js'
import { esc, toast } from '../utils.js'

export async function init() {
  await loadSchemas()
  wireDropZone()

  window._schemasUpload = async () => {
    const files  = Array.from(document.getElementById('schema-file-input')?.files || [])
    const subdir = document.getElementById('schema-subdir')?.value?.trim() || ''
    if (!files.length) { toast('Select a file first', 'error'); return }
    let ok = 0, fail = 0
    for (const file of files) {
      try { await api.schemas.upload(file, subdir); ok++ }
      catch (e) { fail++; toast(`${file.name}: ${e.message}`, 'error') }
    }
    if (ok) toast(`Uploaded ${ok} file${ok > 1 ? 's' : ''}`)
    document.getElementById('schema-file-input').value = ''
    await loadSchemas()
  }

  window._schemaDelete = async (filepath) => {
    if (!confirm(`Delete schema "${filepath}"?`)) return
    try { await api.schemas.delete(filepath); toast('Deleted'); await loadSchemas() }
    catch (e) { toast(e.message, 'error') }
  }
}

async function loadSchemas() {
  try {
    const schemas = await api.schemas.list()
    renderSchemas(schemas)
    const el = document.getElementById('schemas-count')
    if (el) el.textContent = schemas.length
  } catch (e) {
    toast(`Schemas error: ${e.message}`, 'error')
  }
}

function renderSchemas(schemas) {
  const tbody = document.getElementById('schemas-body')
  if (!tbody) return
  if (!schemas.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-secondary text-center py-4">No schemas uploaded</td></tr>'
    return
  }
  tbody.innerHTML = schemas.map(s => {
    const name = s.path || s.name || s
    const ext  = String(name).split('.').pop().toLowerCase()
    const size = s.size ? fmtSize(s.size) : '—'
    return `<tr>
      <td class="fw-semibold">${esc(name)}</td>
      <td><span class="type-pill">${esc(ext)}</span></td>
      <td class="text-secondary">${size}</td>
      <td class="text-end">
        <button class="btn-flat-danger" title="Delete" onclick="window._schemaDelete('${esc(name)}')"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
}

function wireDropZone() {
  const zone  = document.getElementById('schema-drop-zone')
  const input = document.getElementById('schema-file-input')
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
