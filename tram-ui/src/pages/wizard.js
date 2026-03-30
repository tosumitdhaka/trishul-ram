import { api } from '../api.js'
import { esc, toast } from '../utils.js'

// ── State ─────────────────────────────────────────────────────────────────────
let _step = 1
let _plugins = {}
let _state = {
  name: '', description: '', scheduleType: 'interval',
  intervalSeconds: 300, cronExpr: '', serializer: 'json',
  source: { type: '', fields: {} },
  transforms: [],
  sinks: [],
}

// ── Field schema for common connectors ────────────────────────────────────────
const ARRAY_FIELDS = new Set(['brokers', 'hosts', 'servers', 'topics'])

const FIELD_SCHEMA = {
  // Sources
  kafka:         [
    {k:'brokers',      lbl:'Brokers',       ph:'kafka:9092',          req:true, hint:'Comma-separated list of broker host:port'},
    {k:'topic',        lbl:'Topic',         ph:'my-topic',            req:true, hint:'Kafka topic to consume'},
    {k:'group_id',     lbl:'Group ID',      ph:'tram-consumer',                 hint:'Consumer group — omit for auto-generated'},
    {k:'auto_offset_reset', lbl:'Offset Reset', type:'select', opts:['latest','earliest'], hint:'Where to start if no committed offset exists'},
  ],
  rest:          [
    {k:'url',       lbl:'URL',      ph:'https://api.example.com/data', req:true, hint:'Full URL to poll each interval'},
    {k:'method',    lbl:'Method',   type:'select', opts:['GET','POST','PUT'],    hint:'HTTP method for the request'},
    {k:'auth_type', lbl:'Auth',     type:'select', opts:['none','basic','bearer'], hint:'Authentication scheme'},
    {k:'username',  lbl:'Username',                                              hint:'Used for basic auth'},
    {k:'password',  lbl:'Password', type:'password',                             hint:'Used for basic auth or bearer token value'},
  ],
  local:         [
    {k:'path',         lbl:'Directory',    ph:'/data/input', req:true, hint:'Directory to scan for input files'},
    {k:'file_pattern', lbl:'File Pattern', ph:'*.json',                hint:'Glob filter e.g. *.json, *.csv — blank matches all'},
    {k:'recursive',    lbl:'Recursive',    type:'select', opts:['false','true'], hint:'Also scan subdirectories'},
  ],
  sftp:          [
    {k:'host',         lbl:'Host',         req:true,                  hint:'SFTP server hostname or IP'},
    {k:'port',         lbl:'Port',         ph:'22',                   hint:'SSH port, default 22'},
    {k:'username',     lbl:'Username',     req:true,                  hint:'SSH login username'},
    {k:'password',     lbl:'Password',     type:'password',           hint:'SSH password (or leave blank if using key auth)'},
    {k:'remote_path',  lbl:'Remote Path',  ph:'/data', req:true,      hint:'Remote directory to list for files'},
    {k:'file_pattern', lbl:'File Pattern', ph:'*.csv',                hint:'Glob filter applied to remote filenames'},
  ],
  s3:            [
    {k:'bucket',               lbl:'Bucket',      req:true,                       hint:'S3 bucket name'},
    {k:'prefix',               lbl:'Prefix',      ph:'',                          hint:'Key prefix / folder — leave blank for bucket root'},
    {k:'endpoint_url',         lbl:'Endpoint URL',ph:'http://minio:9000',         hint:'Override for MinIO or other S3-compatible stores'},
    {k:'aws_access_key_id',    lbl:'Access Key',                                  hint:'Leave blank to use IAM role / environment credentials'},
    {k:'aws_secret_access_key',lbl:'Secret Key',  type:'password',                hint:'AWS secret access key'},
  ],
  sql:           [
    {k:'connection_url', lbl:'Connection URL', ph:'postgresql+psycopg2://user:pass@host/db', req:true, hint:'SQLAlchemy connection URL — supports PostgreSQL, MySQL, SQLite'},
    {k:'query',          lbl:'SQL Query',      type:'textarea', ph:'SELECT * FROM table',    req:true, hint:'Full SELECT query executed each interval — use WHERE to filter new rows'},
  ],
  mqtt:          [
    {k:'host',     lbl:'Host',     req:true,             hint:'MQTT broker hostname or IP'},
    {k:'port',     lbl:'Port',     ph:'1883',             hint:'Broker port — use 8883 for TLS'},
    {k:'topic',    lbl:'Topic',    req:true,             hint:'Topic or wildcard e.g. sensors/# or home/+/temp'},
    {k:'username', lbl:'Username',                       hint:'Leave blank if broker has no auth'},
    {k:'password', lbl:'Password', type:'password',      hint:'MQTT broker password'},
  ],
  nats:          [
    {k:'servers', lbl:'Servers', ph:'nats://localhost:4222', req:true, hint:'Comma-separated NATS server URLs'},
    {k:'subject', lbl:'Subject', req:true,                            hint:'NATS subject — supports wildcards e.g. telemetry.>'},
  ],
  influxdb:      [
    {k:'url',   lbl:'URL',        ph:'http://localhost:8086', req:true, hint:'InfluxDB base URL'},
    {k:'token', lbl:'Token',      req:true, type:'password',           hint:'Auth token from InfluxDB UI → Data → Tokens'},
    {k:'org',   lbl:'Org',        req:true,                            hint:'Organisation name as shown in InfluxDB UI'},
    {k:'query', lbl:'Flux Query', type:'textarea', req:true, ph:'from(bucket:"my-bucket") |> range(start: -1h)', hint:'Flux query — always include range() to bound the time window'},
  ],
  redis:         [
    {k:'host', lbl:'Host',        req:true, ph:'redis',  hint:'Redis server hostname or IP'},
    {k:'port', lbl:'Port',        ph:'6379',             hint:'Redis port, default 6379'},
    {k:'key',  lbl:'Key/Channel', req:true,              hint:'Key name for GET/LRANGE or channel name for SUBSCRIBE'},
  ],
  syslog:        [
    {k:'host', lbl:'Listen Host', ph:'0.0.0.0', hint:'Address to bind — use 0.0.0.0 to accept from any interface'},
    {k:'port', lbl:'Port',        ph:'514',      hint:'UDP/TCP port — default 514; use 5514 to avoid needing root'},
  ],
  webhook:       [
    {k:'path', lbl:'Path', req:true, ph:'/webhook/my-hook', hint:'URL path suffix — must start with /webhook/; TRAM registers this as a POST endpoint'},
  ],
  ftp:           [
    {k:'host',        lbl:'Host',        req:true,          hint:'FTP server hostname or IP'},
    {k:'port',        lbl:'Port',        ph:'21',           hint:'FTP control port, default 21'},
    {k:'username',    lbl:'Username',                       hint:'FTP login username (blank = anonymous)'},
    {k:'password',    lbl:'Password',    type:'password',   hint:'FTP password'},
    {k:'remote_path', lbl:'Remote Path', ph:'/', req:true,  hint:'Remote directory to list for files'},
  ],
  gcs:           [
    {k:'bucket',           lbl:'Bucket',          req:true,                    hint:'GCS bucket name'},
    {k:'prefix',           lbl:'Prefix',          ph:'',                       hint:'Object prefix / folder — leave blank for bucket root'},
    {k:'credentials_file', lbl:'Credentials JSON',ph:'/secrets/sa.json',       hint:'Path to service-account JSON — omit to use Application Default Credentials'},
  ],
  azure_blob:    [
    {k:'connection_string', lbl:'Connection String', req:true, hint:'From Azure Portal → Storage Account → Access keys → Connection string'},
    {k:'container',         lbl:'Container',         req:true, hint:'Blob container name'},
  ],
  elasticsearch: [
    {k:'hosts',          lbl:'Hosts',          ph:'http://es:9200',     req:true, hint:'Comma-separated Elasticsearch node URLs'},
    {k:'index_template', lbl:'Index Template', ph:'pm-{timestamp}',    req:true, hint:'Index name — supports {timestamp} date substitution'},
  ],
  clickhouse:    [
    {k:'host',     lbl:'Host',     req:true,          hint:'ClickHouse server hostname or IP'},
    {k:'port',     lbl:'Port',     ph:'9000',         hint:'Native TCP port, default 9000 (use 8123 for HTTP interface)'},
    {k:'database', lbl:'Database', req:true,          hint:'Target database name'},
    {k:'user',     lbl:'User',                        hint:'ClickHouse username (default: default)'},
    {k:'password', lbl:'Password', type:'password',   hint:'ClickHouse password'},
  ],
  // Sinks (overlap with sources + sink-specific)
  opensearch:    [
    {k:'hosts',    lbl:'Hosts',           ph:'http://opensearch:9200', req:true, hint:'Comma-separated OpenSearch node URLs'},
    {k:'index',    lbl:'Index Template',  ph:'pm-%Y.%m.%d',           req:true, hint:'Index pattern — supports strftime e.g. logs-%Y.%m.%d'},
    {k:'username', lbl:'Username',                                               hint:'Basic auth username (if security plugin enabled)'},
    {k:'password', lbl:'Password',        type:'password',                       hint:'Basic auth password'},
  ],
  ves:           [
    {k:'url',    lbl:'VES URL', req:true, hint:'VES collector endpoint URL'},
    {k:'domain', lbl:'Domain', ph:'fault', hint:'VES domain: fault, measurement, heartbeat, log, etc.'},
  ],
  snmp_trap:     [
    {k:'host', lbl:'Target Host', req:true,   hint:'Host to send SNMP traps to'},
    {k:'port', lbl:'Port',        ph:'162',   hint:'SNMP trap port, default 162'},
  ],
  amqp:          [
    {k:'url',         lbl:'AMQP URL',    req:true, ph:'amqp://user:pass@rabbit:5672/', hint:'Full AMQP connection URL including credentials and vhost'},
    {k:'exchange',    lbl:'Exchange',                                                   hint:'Exchange name — leave blank for the default exchange'},
    {k:'routing_key', lbl:'Routing Key',                                                hint:'Message routing key for the exchange'},
  ],
}

// ── Transform field schema ────────────────────────────────────────────────────
const TRANSFORM_FIELDS = {
  rename:           [{k:'fields', lbl:'Fields (old: new)', type:'kv', req:true, hint:'Map of old_name: new_name pairs — renames matching fields in each record'}],
  cast:             [{k:'fields', lbl:'Fields (col: type)', type:'kv', req:true, hint:'Map of field_name: target_type — types: int, float, str, bool'}],
  add_field:        [
    {k:'field', lbl:'Field Name', req:true, hint:'Name of the new field to inject into every record'},
    {k:'value', lbl:'Value',      req:true, hint:'Static value or {existing_field} interpolation e.g. {host}-{env}'},
  ],
  drop:             [{k:'fields', lbl:'Fields to drop', ph:'col1, col2', req:true, hint:'Comma-separated field names to remove from each record'}],
  filter:           [{k:'condition', lbl:'Condition', ph:"status == 'active'", req:true, hint:"Python expression — record fields are available as variables e.g. count > 0 or type == 'alarm'"}],
  value_map:        [
    {k:'field',   lbl:'Field',              req:true, hint:'Field whose value will be looked up and replaced'},
    {k:'mapping', lbl:'Mapping (from: to)', type:'kv', req:true, hint:'Map of source_value: replacement_value pairs'},
  ],
  regex_extract:    [
    {k:'field',   lbl:'Source Field',  req:true, hint:'Field containing the string to match against'},
    {k:'pattern', lbl:'Regex Pattern', req:true, hint:'Python regex — use a named group (?P<name>...) or single capture group'},
    {k:'target',  lbl:'Target Field',  req:true, hint:'Field to write the captured value into'},
  ],
  jmespath:         [
    {k:'expression', lbl:'JMESPath',     req:true, hint:'JMESPath query applied to the record e.g. data.items[0].value'},
    {k:'target',     lbl:'Target Field', req:true, hint:'Field to write the query result into'},
  ],
  timestamp_normalize: [
    {k:'field',  lbl:'Timestamp Field', req:true, hint:'Field containing the timestamp string to normalise'},
    {k:'format', lbl:'Input Format',    ph:'%Y-%m-%dT%H:%M:%S', hint:'strptime format string — omit for auto-detection'},
  ],
  template:         [
    {k:'field',    lbl:'Target Field', req:true, hint:'Field to write the rendered template value into'},
    {k:'template', lbl:'Template',     ph:'{{name}}-{{env}}', req:true, hint:'Jinja2-style template — reference record fields with {{field_name}}'},
  ],
  mask:             [{k:'fields', lbl:'Fields to mask', ph:'password, token', req:true, hint:'Comma-separated field names — values replaced with ***'}],
  limit:            [{k:'count', lbl:'Max Records', req:true, hint:'Maximum number of records to pass through — excess records are dropped'}],
  deduplicate:      [{k:'fields', lbl:'Key Fields', ph:'id, timestamp', req:true, hint:'Comma-separated fields that together form the deduplication key'}],
  sort:             [
    {k:'field', lbl:'Sort Field', req:true, hint:'Field to sort records by'},
    {k:'order', lbl:'Order', type:'select', opts:['asc','desc'], hint:'Sort direction'},
  ],
  flatten:          [{k:'separator', lbl:'Separator', ph:'_', hint:'Character used to join nested key names e.g. parent_child'}],
  explode:          [{k:'field', lbl:'Array Field', req:true, hint:'Field containing an array — each element becomes a separate record'}],
  unnest:           [{k:'field', lbl:'Nested Field', req:true, hint:'Nested object field whose keys are merged into the parent record'}],
  aggregate:        [
    {k:'group_by',       lbl:'Group By Fields', ph:'host, region', hint:'Comma-separated fields to group records by'},
    {k:'window_seconds', lbl:'Window (s)',                         hint:'Aggregation time window in seconds — records outside window are flushed'},
  ],
  enrich:           [
    {k:'source_field', lbl:'Source Field', req:true, hint:'Field whose value is used as the lookup key'},
    {k:'lookup_file',  lbl:'Lookup File',  req:true, hint:'Path to a CSV or JSON file keyed by the source field value'},
  ],
  validate:         [{k:'schema_file', lbl:'Schema File', req:true, hint:'Path to a JSON Schema file — records failing validation are sent to DLQ'}],
}

// ── Init ──────────────────────────────────────────────────────────────────────
export async function init() {
  _step = 1
  _state = {
    name: '', description: '', scheduleType: 'interval',
    intervalSeconds: 300, cronExpr: '', serializer: 'json',
    source: { type: '', fields: {} }, transforms: [], sinks: [],
  }

  // Pre-fill from window._wizardState if set (e.g. from template deploy)
  if (window._wizardState) {
    Object.assign(_state, window._wizardState)
    window._wizardState = null
  }

  _showStep(1)
  _wireNav()
  await _loadPlugins()
  await _checkAI()
}

async function _loadPlugins() {
  try {
    _plugins = await api.plugins()
  } catch (_) {
    _plugins = { sources: [], sinks: [], transforms: [], serializers: [] }
  }
  // Populate source dropdown
  const sel = document.getElementById('wiz-src-type')
  if (!sel) return
  const sources = _plugins.sources || []
  sel.innerHTML = '<option value="">— select —</option>' +
    sources.map(t => `<option value="${esc(t)}"${_state.source.type === t ? ' selected' : ''}>${esc(t)}</option>`).join('')
  if (_state.source.type) window._wizRenderSrcFields?.()
}

async function _checkAI() {
  try {
    const status = await api.ai.status()
    const sec = document.getElementById('wiz-ai-section')
    if (status.enabled && sec) {
      sec.classList.remove('d-none')
      const modelEl = document.getElementById('wiz-ai-model')
      if (modelEl) modelEl.textContent = `${status.provider} / ${status.model}`
    }
  } catch (_) {}
}

// ── Step navigation ───────────────────────────────────────────────────────────
function _wireNav() {
  window._wizBack  = _goBack
  window._wizNext  = _goNext
  window._wizSave  = _save
  window._wizSchedChange = _schedChange
  window._wizRenderSrcFields = () => _renderConnectorFields('wiz-src-fields', 'wiz-src-type', _state.source.fields)
  window._wizTestSrc = _testSrc
  window._wizAddTransform = _addTransform
  window._wizAddSink = _addSink
  window._wizDryRun = _dryRun
  window._wizAiGenerate = _aiGenerate
}

function _showStep(n) {
  _step = n
  for (let i = 1; i <= 5; i++) {
    document.getElementById(`wiz-step-${i}`)?.classList.toggle('d-none', i !== n)
  }
  document.querySelectorAll('#wiz-steps .wiz-step').forEach(el => {
    const s = parseInt(el.dataset.step)
    el.classList.toggle('active',    s === n)
    el.classList.toggle('done',      s < n)
    el.classList.toggle('upcoming',  s > n)
  })
  const backBtn = document.getElementById('wiz-back-btn')
  const nextBtn = document.getElementById('wiz-next-btn')
  const saveBtn = document.getElementById('wiz-save-btn')
  if (backBtn) backBtn.classList.toggle('d-none', n === 1)
  if (nextBtn) nextBtn.classList.toggle('d-none', n === 5)
  if (saveBtn) saveBtn.classList.toggle('d-none', n !== 5)

  if (n === 4) _renderSinksList()
  if (n === 5) _buildReviewYaml()
}

function _schedChange() {
  const type = document.getElementById('wiz-sched-type')?.value
  document.getElementById('wiz-interval-row')?.classList.toggle('d-none', type !== 'interval')
  document.getElementById('wiz-cron-row')?.classList.toggle('d-none', type !== 'cron')
}

function _goBack() { if (_step > 1) _showStep(_step - 1) }

function _goNext() {
  if (!_collectStep(_step)) return
  _showStep(_step + 1)
}

// ── Step data collection ──────────────────────────────────────────────────────
function _collectStep(n) {
  if (n === 1) {
    const name = document.getElementById('wiz-name')?.value.trim()
    if (!name) { toast('Pipeline name is required', 'error'); return false }
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) { toast('Name must be alphanumeric with hyphens/underscores', 'error'); return false }
    _state.name        = name
    _state.description = document.getElementById('wiz-desc')?.value.trim() || ''
    _state.scheduleType = document.getElementById('wiz-sched-type')?.value || 'interval'
    _state.serializer   = document.getElementById('wiz-serializer')?.value || 'json'
    const val  = parseInt(document.getElementById('wiz-interval-val')?.value) || 5
    const unit = parseInt(document.getElementById('wiz-interval-unit')?.value) || 60
    _state.intervalSeconds = val * unit
    _state.cronExpr = document.getElementById('wiz-cron-expr')?.value.trim() || ''
  } else if (n === 2) {
    const type = document.getElementById('wiz-src-type')?.value
    if (!type) { toast('Select a source type', 'error'); return false }
    _state.source.type = type
    _state.source.fields = _collectConnectorFields('wiz-src-fields')
  } else if (n === 3) {
    _collectTransforms()
  } else if (n === 4) {
    if (!_collectSinks()) return false
  }
  return true
}

// ── Connector field rendering ─────────────────────────────────────────────────
function _renderConnectorFields(containerId, typeSelId, existingFields = {}) {
  const container = document.getElementById(containerId)
  if (!container) return
  const type = document.getElementById(typeSelId)?.value
  if (!type) { container.innerHTML = ''; return }
  const schema = FIELD_SCHEMA[type]
  if (!schema) {
    container.innerHTML = `<div class="col-12"><label class="form-label text-secondary" style="font-size:12px">Config fields (YAML key: value, one per line)</label><textarea class="form-control form-control-sm font-monospace" id="${containerId}-freeform" rows="5" style="background:#0d1117;color:#e6edf3;border-color:#30363d">${_freeformFromFields(existingFields)}</textarea></div>`
    return
  }
  container.innerHTML = `<div class="row g-2">${schema.map(f => _fieldHtml(f, existingFields[f.k] || '', containerId)).join('')}</div>`
}

function _fieldHtml(f, val, prefix) {
  const id = `${prefix}-${f.k}`
  const label = `<label class="form-label text-secondary mb-1" style="font-size:12px">${esc(f.lbl)}${f.req ? ' <span style="color:#f85149">*</span>' : ''}</label>`
  const hint = f.hint ? `<div class="form-text" style="font-size:10px;color:#6e7681">${esc(f.hint)}</div>` : ''
  if (f.type === 'select') {
    return `<div class="col-4">${label}<select class="form-select form-select-sm" id="${id}" style="background:#161b22;color:#e6edf3;border-color:#30363d">${f.opts.map(o => `<option${val===o?' selected':''}>${o}</option>`).join('')}</select>${hint}</div>`
  }
  if (f.type === 'textarea') {
    return `<div class="col-12">${label}<textarea class="form-control form-control-sm font-monospace" id="${id}" rows="3" placeholder="${esc(f.ph||'')}" style="background:#0d1117;color:#e6edf3;border-color:#30363d;resize:none">${esc(val)}</textarea>${hint}</div>`
  }
  const inputType = f.type === 'password' ? 'password' : 'text'
  return `<div class="col-4">${label}<input type="${inputType}" class="form-control form-control-sm" id="${id}" placeholder="${esc(f.ph||'')}" value="${esc(val)}" style="background:#161b22;color:#e6edf3;border-color:#30363d">${hint}</div>`
}

function _collectConnectorFields(containerId) {
  const container = document.getElementById(containerId)
  if (!container) return {}
  const freeform = container.querySelector(`#${containerId}-freeform`)
  if (freeform) return _fieldsFromFreeform(freeform.value)
  const fields = {}
  container.querySelectorAll('input,select,textarea').forEach(el => {
    const k = el.id.replace(`${containerId}-`, '')
    if (el.value.trim()) fields[k] = el.value.trim()
  })
  return fields
}

function _freeformFromFields(fields) {
  return Object.entries(fields).map(([k,v]) => `${k}: ${v}`).join('\n')
}

function _fieldsFromFreeform(text) {
  const fields = {}
  for (const line of text.split('\n')) {
    const idx = line.indexOf(':')
    if (idx < 0) continue
    const k = line.slice(0, idx).trim()
    const v = line.slice(idx + 1).trim()
    if (k) fields[k] = v
  }
  return fields
}

// ── Source test ───────────────────────────────────────────────────────────────
async function _testSrc() {
  const type = document.getElementById('wiz-src-type')?.value
  if (!type) { toast('Select a source type first', 'error'); return }
  const fields = _collectConnectorFields('wiz-src-fields')
  const resultEl = document.getElementById('wiz-src-test-result')
  if (resultEl) resultEl.textContent = 'Testing…'
  try {
    const r = await api.connectors.test(type, fields)
    if (resultEl) {
      resultEl.style.color = r.ok ? '#3fb950' : '#f85149'
      resultEl.textContent = r.ok
        ? `✓ ${r.detail || 'OK'}${r.latency_ms != null ? ` (${r.latency_ms}ms)` : ''}`
        : `✗ ${r.error || 'failed'}`
    }
  } catch (e) {
    if (resultEl) { resultEl.style.color = '#f85149'; resultEl.textContent = `✗ ${e.message}` }
  }
}

// ── Transforms ────────────────────────────────────────────────────────────────
function _addTransform() {
  _state.transforms.push({ type: 'filter', fields: { condition: '' } })
  _renderTransformsList()
}

function _collectTransforms() {
  const list = document.getElementById('wiz-transforms-list')
  if (!list) return
  _state.transforms = []
  list.querySelectorAll('.wiz-transform-card').forEach((card, i) => {
    const type = card.querySelector('.wiz-t-type')?.value
    if (!type) return
    const fields = _collectConnectorFields(`wiz-tf-${i}`)
    _state.transforms.push({ type, fields })
  })
}

function _renderTransformsList() {
  const list = document.getElementById('wiz-transforms-list')
  if (!list) return
  if (!_state.transforms.length) {
    list.innerHTML = '<div class="text-secondary text-center py-3" style="font-size:13px">No transforms — click "Add Transform" or click Next to skip.</div>'
    return
  }
  const allTransforms = _plugins.transforms || Object.keys(TRANSFORM_FIELDS)
  list.innerHTML = _state.transforms.map((t, i) => `
    <div class="wiz-transform-card mb-3 p-3 rounded" style="background:#161b22;border:1px solid #30363d">
      <div class="d-flex gap-2 align-items-center mb-2">
        <select class="form-select form-select-sm wiz-t-type" style="width:180px;background:#0d1117;color:#e6edf3;border-color:#30363d"
                onchange="window._wizChangeTransformType?.(${i}, this.value)">
          ${allTransforms.map(tt => `<option value="${tt}"${t.type===tt?' selected':''}>${tt}</option>`).join('')}
        </select>
        <button class="btn-flat-danger ms-auto" onclick="window._wizDeleteTransform?.(${i})"><i class="bi bi-trash"></i></button>
      </div>
      <div id="wiz-tf-${i}" class="row g-2">
        ${(TRANSFORM_FIELDS[t.type] || [{k:'_yaml',lbl:'Config (YAML key: value)',type:'textarea',ph:'field: value'}]).map(f => _fieldHtml(f, t.fields?.[f.k] || '', `wiz-tf-${i}`)).join('')}
      </div>
    </div>`).join('')

  window._wizDeleteTransform = (i) => {
    _collectTransforms()
    _state.transforms.splice(i, 1)
    _renderTransformsList()
  }
  window._wizChangeTransformType = (i, type) => {
    _collectTransforms()
    _state.transforms[i].type = type
    _state.transforms[i].fields = {}
    _renderTransformsList()
  }
}

// ── Sinks ─────────────────────────────────────────────────────────────────────
function _addSink() {
  _collectSinks()
  _state.sinks.push({ type: '', fields: {}, serializer_out: '', condition: '' })
  _renderSinksList()
}

function _collectSinks() {
  const list = document.getElementById('wiz-sinks-list')
  if (!list) return true
  const updated = []
  let ok = true
  list.querySelectorAll('.wiz-sink-card').forEach((card, i) => {
    const type = card.querySelector('.wiz-s-type')?.value
    const fields = _collectConnectorFields(`wiz-sk-${i}`)
    const serializer_out = card.querySelector(`.wiz-s-ser`)?.value || ''
    const condition      = card.querySelector(`.wiz-s-cond`)?.value.trim() || ''
    updated.push({ type: type || '', fields, serializer_out, condition })
  })
  _state.sinks = updated
  return ok
}

function _renderSinksList() {
  const list = document.getElementById('wiz-sinks-list')
  if (!list) return
  if (!_state.sinks.length) {
    list.innerHTML = '<div class="text-secondary text-center py-3" style="font-size:13px">No sinks — click "Add Sink".</div>'
    return
  }
  const allSinks = _plugins.sinks || []
  const serializers = _plugins.serializers || ['json','csv','xml','ndjson','bytes','text']
  list.innerHTML = _state.sinks.map((s, i) => `
    <div class="wiz-sink-card mb-3 p-3 rounded" style="background:#161b22;border:1px solid #30363d">
      <div class="d-flex gap-2 align-items-center mb-2">
        <select class="form-select form-select-sm wiz-s-type" style="width:180px;background:#0d1117;color:#e6edf3;border-color:#30363d"
                onchange="window._wizChangeSinkType?.(${i}, this.value)">
          <option value="">— select —</option>
          ${allSinks.map(st => `<option value="${st}"${s.type===st?' selected':''}>${st}</option>`).join('')}
        </select>
        <select class="form-select form-select-sm wiz-s-ser" style="width:120px;background:#0d1117;color:#e6edf3;border-color:#30363d">
          <option value="">default ser.</option>
          ${serializers.map(sr => `<option value="${sr}"${s.serializer_out===sr?' selected':''}>${sr}</option>`).join('')}
        </select>
        <button class="btn btn-sm btn-outline-secondary" onclick="window._wizTestSink?.(${i})">
          <i class="bi bi-plug"></i> Test
        </button>
        <span id="wiz-sk-test-${i}" style="font-size:11px"></span>
        <button class="btn-flat-danger ms-auto" onclick="window._wizDeleteSink?.(${i})"><i class="bi bi-trash"></i></button>
      </div>
      <div id="wiz-sk-${i}" class="row g-2">
        ${s.type ? (FIELD_SCHEMA[s.type] || []).map(f => _fieldHtml(f, s.fields?.[f.k] || '', `wiz-sk-${i}`)).join('') : ''}
      </div>
      <div class="mt-2">
        <input type="text" class="form-control form-control-sm wiz-s-cond font-monospace" placeholder="Condition (optional): records_out > 0" value="${esc(s.condition||'')}" style="background:#0d1117;color:#e6edf3;border-color:#30363d;font-size:11px">
      </div>
    </div>`).join('')

  window._wizDeleteSink = (i) => { _collectSinks(); _state.sinks.splice(i, 1); _renderSinksList() }
  window._wizChangeSinkType = (i, type) => {
    _collectSinks()
    _state.sinks[i].type = type
    _state.sinks[i].fields = {}
    _renderSinksList()
  }
  window._wizTestSink = async (i) => {
    _collectSinks()
    const s = _state.sinks[i]
    if (!s?.type) return
    const resEl = document.getElementById(`wiz-sk-test-${i}`)
    if (resEl) resEl.textContent = '…'
    try {
      const r = await api.connectors.test(s.type, s.fields)
      if (resEl) {
        resEl.style.color = r.ok ? '#3fb950' : '#f85149'
        resEl.textContent = r.ok ? `✓${r.latency_ms != null ? ` ${r.latency_ms}ms` : ''}` : `✗ ${r.error||'fail'}`
      }
    } catch (e) { if (resEl) { resEl.style.color='#f85149'; resEl.textContent=`✗ ${e.message}` } }
  }
}

// ── YAML builder ──────────────────────────────────────────────────────────────
function buildYaml(state) {
  const lines = []
  lines.push(`name: ${state.name}`)
  if (state.description) lines.push(`description: "${state.description.replace(/"/g, '\\"')}"`)
  lines.push(`schedule:`)
  lines.push(`  type: ${state.scheduleType}`)
  if (state.scheduleType === 'interval' && state.intervalSeconds)
    lines.push(`  interval_seconds: ${state.intervalSeconds}`)
  if (state.scheduleType === 'cron' && state.cronExpr)
    lines.push(`  cron_expr: "${state.cronExpr}"`)
  lines.push(`source:`)
  lines.push(`  type: ${state.source.type}`)
  for (const [k, v] of Object.entries(state.source.fields || {}))
    if (v || v === 0) lines.push(..._yamlField(k, v, 2))
  if (state.serializer && state.serializer !== 'bytes')
    lines.push(`serializer: ${state.serializer}`)
  if (state.transforms?.length) {
    lines.push(`transforms:`)
    for (const t of state.transforms) {
      lines.push(`  - type: ${t.type}`)
      for (const [k, v] of Object.entries(t.fields || {}))
        if (v || v === 0) lines.push(..._yamlField(k, v, 4))
    }
  }
  if (state.sinks?.length) {
    lines.push(`sinks:`)
    for (const s of state.sinks) {
      if (!s.type) continue
      lines.push(`  - type: ${s.type}`)
      for (const [k, v] of Object.entries(s.fields || {}))
        if (v || v === 0) lines.push(..._yamlField(k, v, 4))
      if (s.serializer_out) lines.push(`    serializer_out: ${s.serializer_out}`)
      if (s.condition) lines.push(`    condition: "${s.condition.replace(/"/g, '\\"')}"`)
    }
  }
  return lines.join('\n')
}

function _yamlField(k, v, indent) {
  const pad = ' '.repeat(indent)
  if (ARRAY_FIELDS.has(k)) {
    const items = String(v).split(',').map(s => s.trim()).filter(Boolean)
    if (items.length === 1) return [`${pad}${k}: ${_yamlVal(items[0])}`]
    return [`${pad}${k}:`, ...items.map(i => `${pad}  - ${_yamlVal(i)}`)]
  }
  if (String(v).includes('\n')) {
    // multiline textarea (e.g. SQL query, Flux query)
    return [`${pad}${k}: |`, ...String(v).split('\n').map(l => `${pad}  ${l}`)]
  }
  return [`${pad}${k}: ${_yamlVal(v)}`]
}

function _yamlVal(v) {
  if (v === 'true' || v === 'false') return v
  if (!isNaN(String(v)) && String(v).trim() !== '') return v
  if (/[:{}\[\],&*#?|<>=!%@`"']/.test(String(v)) || String(v).startsWith(' '))
    return `"${String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
  return v
}

// ── Step 5: review ────────────────────────────────────────────────────────────
function _buildReviewYaml() {
  const ta = document.getElementById('wiz-yaml-preview')
  if (!ta) return
  // Collect current step 4 state before building
  _collectSinks()
  ta.value = buildYaml(_state)
}

async function _dryRun() {
  const yaml = document.getElementById('wiz-yaml-preview')?.value?.trim()
  if (!yaml) return
  const resultEl = document.getElementById('wiz-dryrun-result')
  if (resultEl) resultEl.innerHTML = '<span class="text-secondary" style="font-size:12px">Running…</span>'
  try {
    const { baseUrl } = (await import('../api.js')).getConfig()
    const token = localStorage.getItem('tram_auth_token')
    const headers = { 'Content-Type': 'application/yaml' }
    if (token) headers['Authorization'] = `Bearer ${token}`
    const res = await fetch(`${baseUrl}/api/pipelines/dry-run`, { method: 'POST', headers, body: yaml })
    const json = await res.json()
    if (!resultEl) return
    const ok = json.status === 'ok' || json.valid
    let html = `<div style="font-size:12px;padding:10px;border-radius:4px;background:${ok?'#1a3328':'#3d1a1a'};border:1px solid ${ok?'#3fb950':'#f85149'}">`
    html += `<div style="color:${ok?'#3fb950':'#f85149'};margin-bottom:4px">${ok ? '✓ Dry run passed' : '✗ Dry run failed'}</div>`
    if (json.errors?.length) {
      html += json.errors.map(e => `<div style="color:#f85149">${esc(e)}</div>`).join('')
      html += `<button class="btn btn-sm btn-outline-secondary mt-2" onclick="window._wizExplainError?.(${JSON.stringify(json.errors[0]).replace(/"/g,'"')})"><i class="bi bi-stars me-1"></i>Explain</button>`
      html += `<div id="wiz-ai-explain" class="mt-2 text-secondary" style="font-size:11px"></div>`
    }
    if (json.warnings?.length) html += json.warnings.map(w => `<div style="color:#e3b341">${esc(w)}</div>`).join('')
    html += '</div>'
    resultEl.innerHTML = html
    window._wizExplainError = async (errMsg) => {
      const el = document.getElementById('wiz-ai-explain')
      if (el) el.textContent = 'Explaining…'
      try {
        const r = await api.ai.suggest({ mode: 'explain', error: errMsg, yaml: document.getElementById('wiz-yaml-preview')?.value })
        if (el) el.innerHTML = `<em>${esc(r.explanation || '')}</em>`
      } catch (e) {
        if (el) el.textContent = `Could not explain: ${e.message}`
      }
    }
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<div style="color:#f85149;font-size:12px">Error: ${esc(e.message)}</div>`
  }
}

async function _save() {
  const yaml = document.getElementById('wiz-yaml-preview')?.value?.trim()
  if (!yaml) { toast('No YAML to save', 'error'); return }
  const btn = document.getElementById('wiz-save-btn')
  if (btn) btn.disabled = true
  try {
    await api.pipelines.create(yaml)
    toast(`Pipeline '${_state.name}' created`)
    window._detailPipeline = _state.name
    navigate('detail')
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) btn.disabled = false
  }
}

// ── AI generation ─────────────────────────────────────────────────────────────
async function _aiGenerate() {
  const prompt = document.getElementById('wiz-ai-prompt')?.value.trim()
  if (!prompt) { toast('Enter a description first', 'error'); return }
  const btn = document.getElementById('wiz-ai-gen-btn')
  const status = document.getElementById('wiz-ai-status')
  if (btn) btn.disabled = true
  if (status) status.textContent = 'Generating…'
  try {
    const r = await api.ai.suggest({ mode: 'generate', prompt, plugins: _plugins })
    if (!r.yaml) throw new Error('No YAML returned')
    // Jump to step 5 with the generated YAML
    _state.name = _state.name || 'ai-generated'
    _showStep(5)
    const ta = document.getElementById('wiz-yaml-preview')
    if (ta) ta.value = r.yaml
    if (status) status.textContent = ''
    toast('YAML generated — review and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    if (status) status.textContent = ''
  } finally {
    if (btn) btn.disabled = false
  }
}
