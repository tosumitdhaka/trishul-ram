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

async function req(path, options = {}) {
  const { baseUrl, apiKey } = getConfig()
  const headers = { ...options.headers }
  if (apiKey) headers['X-API-Key'] = apiKey
  const token = localStorage.getItem('tram_auth_token')
  if (token && !apiKey) headers['Authorization'] = `Bearer ${token}`
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

// ── Health & Meta ────────────────────────────────────────────────────────────
export const api = {
  health:  () => req('/api/health'),
  ready:   () => req('/api/ready'),
  meta:    () => req('/api/meta'),
  plugins: () => req('/api/plugins'),

  // ── Auth ───────────────────────────────────────────────────────────────────
  auth: {
    me:    ()                    => req('/api/auth/me'),
    login: (username, password)  => req('/api/auth/login', { method: 'POST', body: { username, password } }),
  },

  // ── Pipelines ──────────────────────────────────────────────────────────────
  pipelines: {
    list:     ()           => req('/api/pipelines'),
    get:      (name)       => req(`/api/pipelines/${name}`),
    create:   (yaml)       => req('/api/pipelines', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: yaml }),
    update:   (name, yaml) => req(`/api/pipelines/${name}`, { method: 'PUT', headers: { 'Content-Type': 'application/yaml' }, body: yaml }),
    delete:   (name)       => req(`/api/pipelines/${name}`, { method: 'DELETE' }),
    start:    (name)       => req(`/api/pipelines/${name}/start`,  { method: 'POST' }),
    stop:     (name)       => req(`/api/pipelines/${name}/stop`,   { method: 'POST' }),
    run:      (name)       => req(`/api/pipelines/${name}/run`,    { method: 'POST' }),
    reload:   ()           => req('/api/pipelines/reload',          { method: 'POST' }),
    versions: (name)       => req(`/api/pipelines/${name}/versions`),
    rollback: (name, ver)  => req(`/api/pipelines/${name}/rollback?version=${ver}`, { method: 'POST' }),
  },

  // ── Runs ───────────────────────────────────────────────────────────────────
  runs: {
    list: (params = {}) => req('/api/runs?' + new URLSearchParams(params)),
    get:  (id)          => req(`/api/runs/${id}`),
  },

  // ── Schemas ────────────────────────────────────────────────────────────────
  schemas: {
    list:   ()          => req('/api/schemas'),
    get:    (filepath)  => req(`/api/schemas/${filepath}`),
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
    upload:   (file)    => {
      const fd = new FormData()
      fd.append('file', file)
      return req('/api/mibs/upload', { method: 'POST', body: fd })
    },
  },

  // ── Daemon ─────────────────────────────────────────────────────────────────
  daemon: {
    status: () => req('/api/daemon/status'),
  },
}
