export function normalizeTemplates(raw = []) {
  const seen = new Set()
  return raw.filter(template => {
    if (!template?.source_type) return false
    if (seen.has(template.name)) return false
    seen.add(template.name)
    return true
  })
}

export function populateTemplateFilters(templates, { sourceEl, sinkEl }) {
  const sources = [...new Set(templates.map(template => template.source_type).filter(Boolean))].sort()
  const sinks = [...new Set(templates.flatMap(template => template.sink_types || []).filter(Boolean))].sort()

  if (sourceEl) {
    sourceEl.innerHTML = '<option value="">All sources</option>'
    sources.forEach(source => {
      const option = document.createElement('option')
      option.value = source
      option.textContent = source
      sourceEl.appendChild(option)
    })
  }

  if (sinkEl) {
    sinkEl.innerHTML = '<option value="">All sinks</option>'
    sinks.forEach(sink => {
      const option = document.createElement('option')
      option.value = sink
      option.textContent = sink
      sinkEl.appendChild(option)
    })
  }
}

export function filterTemplates(templates, { query = '', source = '', sink = '', schedule = '' } = {}) {
  const normalizedQuery = query.trim().toLowerCase()
  return templates.filter(template =>
    (!normalizedQuery
      || template.name.toLowerCase().includes(normalizedQuery)
      || (template.description || '').toLowerCase().includes(normalizedQuery)) &&
    (!source || template.source_type === source) &&
    (!sink || (template.sink_types || []).includes(sink)) &&
    (!schedule || template.schedule_type === schedule)
  )
}

export function templateScheduleClass(scheduleType) {
  return {
    stream: 'badge-stream',
    interval: 'badge-interval',
    cron: 'badge-cron',
    manual: 'badge-manual',
  }[scheduleType] || 'badge-interval'
}

export function templateFlowText(template) {
  const sinks = template?.sink_types || []
  if (!sinks.length) return template?.source_type || ''
  return `${template.source_type} -> ${sinks.join(', ')}`
}
