import { api } from '../api.js'
import { bindDataActions, downloadText, esc, toast } from '../utils.js'

let _allSchemas = []

export async function init() {
  wireDropZone()
  document.getElementById('schemas-search')?.addEventListener('input', applySchemaFilter)
  document.getElementById('schemas-upload-btn')?.addEventListener('click', uploadSchemas)
  bindDataActions(document.getElementById('schemas-body'), {
    download: async (button) => downloadSchema(button.dataset.filepath),
    delete: async (button) => deleteSchema(button.dataset.filepath),
  })
  await loadSchemas()
}

async function loadSchemas() {
  try {
    _allSchemas = await api.schemas.list()
    applySchemaFilter()
  } catch (e) {
    toast(`Schemas error: ${e.message}`, 'error')
  }
}

function applySchemaFilter() {
  const query = (document.getElementById('schemas-search')?.value || '').trim().toLowerCase()
  const visible = _allSchemas.filter((schema) => schemaMatches(schema, query))
  renderSchemas(visible)
  updateCounts(visible.length, _allSchemas.length)
}

function renderSchemas(schemas) {
  const tbody = document.getElementById('schemas-body')
  if (!tbody) return
  if (!schemas.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-secondary text-center py-4">${
      _allSchemas.length && queryActive()
        ? 'No schema files match your search'
        : 'No schemas uploaded'
    }</td></tr>`
    return
  }
  tbody.innerHTML = schemas.map(s => {
    const name = s.path || s.name || s
    const ext  = String(name).split('.').pop().toLowerCase()
    const size = s.size_bytes ? fmtSize(s.size_bytes) : '—'
    return `<tr>
      <td class="fw-semibold">${esc(name)}</td>
      <td><span class="type-pill">${esc(ext)}</span></td>
      <td class="text-secondary">${size}</td>
      <td class="text-end">
        <button class="btn-flat" type="button" title="Download schema" data-action="download" data-filepath="${esc(name)}"><i class="bi bi-download"></i></button>
        <button class="btn-flat-danger" type="button" title="Delete" data-action="delete" data-filepath="${esc(name)}"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
}

async function uploadSchemas() {
  const files  = Array.from(document.getElementById('schema-file-input')?.files || [])
  const subdir = document.getElementById('schema-subdir')?.value?.trim() || ''
  if (!files.length) { toast('Select a file first', 'error'); return }
  let ok = 0
  for (const file of files) {
    try {
      await api.schemas.upload(file, subdir)
      ok++
    } catch (e) {
      toast(`${file.name}: ${e.message}`, 'error')
    }
  }
  if (ok) toast(`Uploaded ${ok} file${ok > 1 ? 's' : ''}`)
  document.getElementById('schema-file-input').value = ''
  await loadSchemas()
}

async function deleteSchema(filepath) {
  if (!filepath) return
  if (!confirm(`Delete schema "${filepath}"?`)) return
  try {
    await api.schemas.delete(filepath)
    toast('Deleted')
    await loadSchemas()
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function downloadSchema(filepath) {
  if (!filepath) return
  try {
    const text = await api.schemas.get(filepath)
    downloadText(filepath.replaceAll('/', '__'), text)
  } catch (e) {
    toast(e.message, 'error')
  }
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

function schemaMatches(schema, query) {
  if (!query) return true
  const name = schema?.path || schema?.name || schema || ''
  return String(name).toLowerCase().includes(query)
}

function updateCounts(visible, total) {
  const count = document.getElementById('schemas-count')
  const status = document.getElementById('schemas-filter-status')
  if (count) count.textContent = visible === total ? String(total) : `${visible}/${total}`
  if (status) {
    status.textContent = queryActive() && total
      ? `${visible} of ${total} visible`
      : `${total} schema file${total === 1 ? '' : 's'}`
  }
}

function queryActive() {
  return Boolean((document.getElementById('schemas-search')?.value || '').trim())
}
