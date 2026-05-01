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

function parseJsonSafe(text) {
  if (!text) return null
  try { return JSON.parse(text) } catch (_) { return null }
}

function errorFromResponse(text, fallback) {
  const detail = parseJsonSafe(text)?.detail || null
  return new Error(detail || fallback)
}

function encodePathSegment(value) {
  return encodeURIComponent(String(value ?? ''))
}

function encodePath(value) {
  return String(value ?? '')
    .split('/')
    .map(segment => encodePathSegment(segment))
    .join('/')
}

function buildQuery(params = {}) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    query.set(key, String(value))
  })
  const text = query.toString()
  return text ? `?${text}` : ''
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
  const json = parseJsonSafe(text)
  if (!res.ok) {
    throw Object.assign(new Error(json?.detail || res.statusText), { status: res.status })
  }
  return json ?? text ?? null
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
    get:      (name)       => req(`/api/pipelines/${encodePathSegment(name)}`),
    placement:(name)       => req(`/api/pipelines/${encodePathSegment(name)}/placement`),
    dryRun:   async (yaml) => normalizeDryRunResult(await req('/api/pipelines/dry-run', { method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: yaml })),
    create:   (yaml)       => req('/api/pipelines', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: yaml }),
    update:   (name, yaml) => req(`/api/pipelines/${encodePathSegment(name)}`, { method: 'PUT', headers: { 'Content-Type': 'application/yaml' }, body: yaml }),
    delete:   (name)       => req(`/api/pipelines/${encodePathSegment(name)}`, { method: 'DELETE' }),
    start:    (name)       => req(`/api/pipelines/${encodePathSegment(name)}/start`,   { method: 'POST' }),
    stop:     (name)       => req(`/api/pipelines/${encodePathSegment(name)}/stop`,    { method: 'POST' }),
    restart:  (name)       => req(`/api/pipelines/${encodePathSegment(name)}/restart`, { method: 'POST' }),
    run:      (name)       => req(`/api/pipelines/${encodePathSegment(name)}/run`,     { method: 'POST' }),
    reload:   ()           => req('/api/pipelines/reload',          { method: 'POST' }),
    versions: (name)       => req(`/api/pipelines/${encodePathSegment(name)}/versions`),
    rollback: (name, ver)  => req(`/api/pipelines/${encodePathSegment(name)}/rollback${buildQuery({ version: ver })}`, { method: 'POST' }),
  },

  // ── Runs ───────────────────────────────────────────────────────────────────
  runs: {
    list:      (params = {}) => req(`/api/runs${buildQuery(params)}`),
    get:       (id)          => req(`/api/runs/${id}`),
    exportCsv: (params = {}) => reqBlob(`/api/runs${buildQuery({ ...params, format: 'csv' })}`),
  },

  // ── Schemas ────────────────────────────────────────────────────────────────
  schemas: {
    list:   ()          => req('/api/schemas'),
    get:    (filepath)  => reqText(`/api/schemas/${encodePath(filepath)}`),
    delete: (filepath)  => req(`/api/schemas/${encodePath(filepath)}`, { method: 'DELETE' }),
    upload: (file, subdir) => {
      const fd = new FormData()
      fd.append('file', file)
      const qs = buildQuery({ subdir })
      return req(`/api/schemas/upload${qs}`, { method: 'POST', body: fd })
    },
  },

  // ── MIBs ───────────────────────────────────────────────────────────────────
  mibs: {
    list:     ()        => req('/api/mibs'),
    get:      (name)    => reqText(`/api/mibs/${encodePathSegment(name)}`),
    source:   (name)    => reqText(`/api/mibs/${encodePathSegment(name)}/source`),
    delete:   (name)    => req(`/api/mibs/${encodePathSegment(name)}`, { method: 'DELETE' }),
    download: (names)   => req('/api/mibs/download', { method: 'POST', body: { names } }),
    upload:   (file, { resolveMissing = false } = {}) => {
      const fd = new FormData()
      fd.append('file', file)
      const qs = buildQuery({ resolve_missing: resolveMissing || undefined })
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
    list:   (name)            => req(`/api/pipelines/${encodePathSegment(name)}/alerts`),
    create: (name, rule)      => req(`/api/pipelines/${encodePathSegment(name)}/alerts`, { method: 'POST', body: rule }),
    update: (name, idx, rule) => req(`/api/pipelines/${encodePathSegment(name)}/alerts/${encodePathSegment(idx)}`, { method: 'PUT', body: rule }),
    delete: (name, idx)       => req(`/api/pipelines/${encodePathSegment(name)}/alerts/${encodePathSegment(idx)}`, { method: 'DELETE' }),
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
    get: (params = {}) => req(`/api/stats${buildQuery(params)}`),
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
    yaml: (name, ver) => reqText(`/api/pipelines/${encodePathSegment(name)}/versions/${encodePathSegment(ver)}`),
  },
}
