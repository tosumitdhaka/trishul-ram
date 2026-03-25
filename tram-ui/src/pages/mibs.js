import { api } from '../api.js'
import { esc, toast } from '../utils.js'

export async function init() {
  await loadMibs()
  wireDropZone()

  window._mibsUpload = async () => {
    const files = Array.from(document.getElementById('mib-file-input')?.files || [])
    if (!files.length) { toast('Select a .mib file first', 'error'); return }
    let ok = 0, fail = 0
    for (const file of files) {
      try { await api.mibs.upload(file); ok++ }
      catch (e) { fail++; toast(`${file.name}: ${e.message}`, 'error') }
    }
    if (ok) toast(`Uploaded & compiled ${ok} MIB${ok > 1 ? 's' : ''}`)
    document.getElementById('mib-file-input').value = ''
    await loadMibs()
  }

  window._mibsDownload = async () => {
    const raw = document.getElementById('mib-download-names')?.value || ''
    const names = raw.split('\n').map(s => s.trim()).filter(Boolean)
    if (!names.length) { toast('Enter at least one MIB name', 'error'); return }
    try {
      await api.mibs.download(names)
      toast(`Downloaded ${names.length} MIB(s)`)
      await loadMibs()
    } catch (e) { toast(e.message, 'error') }
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
    const size = m.size ? fmtSize(m.size) : '—'
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
