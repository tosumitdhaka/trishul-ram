import { api } from '../api.js'
import { esc, toast } from '../utils.js'

const CATEGORY_META = {
  sources: { icon: 'box-arrow-in-right', title: 'Sources' },
  sinks: { icon: 'box-arrow-right', title: 'Sinks' },
  serializers: { icon: 'braces', title: 'Serializers' },
  transforms: { icon: 'shuffle', title: 'Transforms' },
}

export async function init() {
  const body = document.getElementById('plugins-body')
  const total = document.getElementById('plugins-total-count')
  if (!body) return

  try {
    const plugins = await api.plugins()
    const categories = ['sources', 'sinks', 'serializers', 'transforms']
    const totalCount = categories.reduce((sum, key) => sum + ((plugins[key] || []).length), 0)
    if (total) {
      total.textContent = `${totalCount} plugin${totalCount !== 1 ? 's' : ''}`
    }

    body.innerHTML = categories.map((key, index) => {
      const items = plugins[key] || []
      const meta = CATEGORY_META[key]
      return `<div class="accordion-item" style="background:var(--bg-surface);border-color:var(--border)">
        <h2 class="accordion-header">
          <button class="accordion-button ${index === 0 ? '' : 'collapsed'}" type="button"
                  data-bs-toggle="collapse" data-bs-target="#plugins-${key}"
                  style="background:var(--bg-surface);color:var(--fg);font-size:14px;">
            <i class="bi bi-${meta.icon} me-2 text-secondary"></i>
            ${meta.title}
            <span class="count-pill ms-2">${items.length}</span>
          </button>
        </h2>
        <div id="plugins-${key}" class="accordion-collapse collapse ${index === 0 ? 'show' : ''}">
          <div class="accordion-body p-0">
            <table class="table table-sm mb-0" style="font-size:12px">
              <thead><tr><th style="width:220px">Plugin</th></tr></thead>
              <tbody>
                ${items.length
                  ? items.map(item => `<tr><td class="mono fw-semibold">${esc(item)}</td></tr>`).join('')
                  : '<tr><td class="text-secondary text-center py-4">No plugins registered</td></tr>'
                }
              </tbody>
            </table>
          </div>
        </div>
      </div>`
    }).join('')
  } catch (e) {
    body.innerHTML = '<div class="p-4 text-secondary text-center">Failed to load plugins</div>'
    toast(`Plugins error: ${e.message}`, 'error')
  }
}
