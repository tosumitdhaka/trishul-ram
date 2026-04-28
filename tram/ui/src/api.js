// ── TRAM REST API client ─────────────────────────────────────────────────────

export function getConfig() {
  return {
    baseUrl: localStorage.getItem('tram_base_url') || window.location.origin,
    apiKey:  localStorage.getItem('tram_api_key')  || '',
  }
}

export function saveConfig(baseUrl, apiKey) {
  localStorage.setItem('tram_base_url', baseUrl)
  localStorage.setItem('tram_api_key',  apiKey)
}

function withAuthHeaders(headers = {}) {
  const next = { ...headers }
  const { apiKey } = getConfig()
  if (apiKey) next['X-API-Key'] = apiKey
  const token = localStorage.getItem('tram_auth_token')
  if (token && !apiKey) next['Authorization'] = `Bearer ${token}`
  return next
}

function errorFromResponse(text, fallback) {
  let detail
  try { detail = JSON.parse(text)?.detail } catch (_) { detail = null }
  return new Error(detail || fallback)
}

async function reqText(path) {
  const { baseUrl } = getConfig()
  const headers = withAuthHeaders()
  const res = await fetch(`${baseUrl}${path}`, { headers })
  if (!res.ok) {
    const text = await res.text()
    throw Object.assign(errorFromResponse(text, res.statusText), { status: res.status })
  }
  return res.text()
}

function normalizeDryRunResult(result) {
  const issues = result?.issues || result?.errors || []
  return {
    ...result,
    valid: result?.valid ?? (result?.status === 'ok'),
    issues,
    errors: result?.errors || issues,
    warnings: result?.warnings || [],
  }
}

async function req(path, options = {}) {
  const { baseUrl } = getConfig()
  const headers = withAuthHeaders(options.headers)
  if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    options = { ...options, body: JSON.stringify(options.body) }
  }
  const res = await fetch(`${baseUrl}${path}`, { ...options, headers })
  if (res.status === 204) return null
  const text = await res.text()
  const json = text ? JSON.parse(text) : null
  if (!res.ok) throw Object.assign(new Error(json?.detail || res.statusText), { status: res.status })
  return json
}

async function reqBlob(path, options = {}) {
  const { baseUrl } = getConfig()
  const headers = withAuthHeaders(options.headers)
  if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    options = { ...options, body: JSON.stringify(options.body) }
  }
  const res = await fetch(`${baseUrl}${path}`, { ...options, headers })
  if (!res.ok) {
    const text = await res.text()
    throw Object.assign(errorFromResponse(text, res.statusText), { status: res.status })
  }
  return res.blob()
}

// ── Health & Meta ────────────────────────────────────────────────────────────
export const api = {
  health:  () => req('/api/health'),
  ready:   () => req('/api/ready'),
  meta:    () => req('/api/meta'),
  plugins: () => req('/api/plugins'),

  // ── Auth ───────────────────────────────────────────────────────────────────
  auth: {
    me:             ()                              => req('/api/auth/me'),
    login:          (username, password)             => req('/api/auth/login', { method: 'POST', body: { username, password } }),
    changePassword: (current_password, new_password) => req('/api/auth/change-password', { method: 'POST', body: { current_password, new_password } }),
  },

  // ── Pipelines ──────────────────────────────────────────────────────────────
  pipelines: {
    list:     ()           => req('/api/pipelines'),
    get:      (name)       => req(`/api/pipelines/${name}`),
    placement:(name)       => req(`/api/pipelines/${name}/placement`),
    dryRun:   async (yaml) => normalizeDryRunResult(await req('/api/pipelines/dry-run', { method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: yaml })),
    create:   (yaml)       => req('/api/pipelines', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: yaml }),
    update:   (name, yaml) => req(`/api/pipelines/${name}`, { method: 'PUT', headers: { 'Content-Type': 'application/yaml' }, body: yaml }),
    delete:   (name)       => req(`/api/pipelines/${name}`, { method: 'DELETE' }),
    start:    (name)       => req(`/api/pipelines/${name}/start`,   { method: 'POST' }),
    stop:     (name)       => req(`/api/pipelines/${name}/stop`,    { method: 'POST' }),
    restart:  (name)       => req(`/api/pipelines/${name}/restart`, { method: 'POST' }),
    run:      (name)       => req(`/api/pipelines/${name}/run`,     { method: 'POST' }),
    reload:   ()           => req('/api/pipelines/reload',          { method: 'POST' }),
    versions: (name)       => req(`/api/pipelines/${name}/versions`),
    rollback: (name, ver)  => req(`/api/pipelines/${name}/rollback?version=${ver}`, { method: 'POST' }),
  },

  // ── Runs ───────────────────────────────────────────────────────────────────
  runs: {
    list:      (params = {}) => req('/api/runs?' + new URLSearchParams(params)),
    get:       (id)          => req(`/api/runs/${id}`),
    exportCsv: (params = {}) => reqBlob('/api/runs?' + new URLSearchParams({ ...params, format: 'csv' })),
  },

  // ── Schemas ────────────────────────────────────────────────────────────────
  schemas: {
    list:   ()          => req('/api/schemas'),
    get:    (filepath)  => reqText(`/api/schemas/${filepath}`),
    delete: (filepath)  => req(`/api/schemas/${filepath}`, { method: 'DELETE' }),
    upload: (file, subdir) => {
      const fd = new FormData()
      fd.append('file', file)
      const qs = subdir ? `?subdir=${encodeURIComponent(subdir)}` : ''
      return req(`/api/schemas/upload${qs}`, { method: 'POST', body: fd })
    },
  },

  // ── MIBs ───────────────────────────────────────────────────────────────────
  mibs: {
    list:     ()        => req('/api/mibs'),
    delete:   (name)    => req(`/api/mibs/${name}`, { method: 'DELETE' }),
    download: (names)   => req('/api/mibs/download', { method: 'POST', body: { names } }),
    upload:   (file, { resolveMissing = false } = {}) => {
      const fd = new FormData()
      fd.append('file', file)
      const qs = resolveMissing ? '?resolve_missing=true' : ''
      return req(`/api/mibs/upload${qs}`, { method: 'POST', body: fd })
    },
  },

  // ── Daemon ─────────────────────────────────────────────────────────────────
  daemon: {
    status: () => req('/api/daemon/status'),
  },

  // ── Cluster ────────────────────────────────────────────────────────────────
  cluster: {
    nodes: () => req('/api/cluster/nodes'),
    streams: () => req('/api/cluster/streams'),
  },

  // ── Connectors ─────────────────────────────────────────────────────────────
  connectors: {
    test:         (type, config) => req('/api/connectors/test', { method: 'POST', body: { type, config } }),
    testPipeline: (yaml)         => req('/api/connectors/test-pipeline', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: yaml }),
  },

  // ── Alert rules ────────────────────────────────────────────────────────────
  alerts: {
    list:   (name)            => req(`/api/pipelines/${name}/alerts`),
    create: (name, rule)      => req(`/api/pipelines/${name}/alerts`, { method: 'POST', body: rule }),
    update: (name, idx, rule) => req(`/api/pipelines/${name}/alerts/${idx}`, { method: 'PUT', body: rule }),
    delete: (name, idx)       => req(`/api/pipelines/${name}/alerts/${idx}`, { method: 'DELETE' }),
  },

  // ── Templates ──────────────────────────────────────────────────────────────
  templates: {
    list: () => req('/api/templates'),
  },

  configSchema: {
    get: () => req('/api/config/schema'),
  },

  // ── Stats ──────────────────────────────────────────────────────────────────
  stats: {
    get: (params = {}) => req('/api/stats?' + new URLSearchParams(params)),
  },

  // ── AI assist ──────────────────────────────────────────────────────────────
  ai: {
    status:    ()        => req('/api/ai/status'),
    getConfig: ()        => req('/api/ai/config'),
    saveConfig:(payload) => req('/api/ai/config', { method: 'POST', body: payload }),
    test:      ()        => req('/api/ai/test',   { method: 'POST', body: {} }),
    suggest:   (payload) => req('/api/ai/suggest', { method: 'POST', body: payload }),
  },

  // ── Pipeline version YAML ──────────────────────────────────────────────────
  versions: {
    yaml: (name, ver) => reqText(`/api/pipelines/${name}/versions/${ver}`),
  },
}
