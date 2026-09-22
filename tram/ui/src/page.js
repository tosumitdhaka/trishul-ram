// ── Shared page controller: mount/unmount, polling, focus-safe rendering ──────
//
// Every data page (pipelines, dashboard, cluster, detail, runs, plugins,
// schemas, mibs) builds on this instead of hand-rolling its own lifecycle:
//   - mount():   first load — loading state via renderTableState, fetch, render
//   - refresh(): manual re-fetch (spinner paths)
//   - pollNow(): a poll-cycle fetch outside the timer (runs resume)
//   - unmount(): stops the poll timer and invalidates in-flight loads —
//                called by the router when navigating away, replacing the
//                per-page element-existence guards
//
// Renders are wrapped in renderPreservingFocus: if the operator had focus
// on a row control when a poll re-rendered the table, focus is restored to
// the corresponding element in the new DOM (matched by stable row keys).

import { renderTableState, setOfflineBanner, toast } from './utils.js'

const _registry = new Map()

// Router hook — tear down the outgoing page's controller.
export function unmountPage(name) {
  const controller = _registry.get(name)
  if (controller) controller.unmount()
}

function cssq(value) {
  return (window.CSS && CSS.escape) ? CSS.escape(String(value)) : String(value)
}

// Describe the focused element so it can be found again after a re-render.
function _focusAddress(el, root) {
  if (!el || el === document.body || el === document.documentElement || !root || !root.contains(el)) return null
  const row = el.closest('tr')
  let containerSel = null
  if (row) {
    for (const attr of ['data-pipeline-name', 'data-run-id', 'data-worker-key', 'data-plugins-row']) {
      const value = row.getAttribute(attr)
      if (value) { containerSel = `tr[${attr}="${cssq(value)}"]`; break }
    }
    if (!containerSel && row.id) containerSel = `tr#${cssq(row.id)}`
    if (!containerSel) return null
  }
  // Element identity: first data-* attribute wins (data-action, data-run-toggle,
  // …), otherwise the tag. Falls back gracefully for plain cells and links.
  let elSel = null
  if (el !== row) {
    const dataAttr = Object.keys(el.dataset || {})[0]
    elSel = dataAttr
      ? `[data-${dataAttr.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}="${cssq(el.dataset[dataAttr])}"]`
      : el.tagName.toLowerCase()
  }
  return { containerSel, elSel }
}

function _findElement(address, root) {
  if (!address) return null
  const container = address.containerSel ? root.querySelector(address.containerSel) : root
  if (!container) return null
  if (!address.elSel) return container
  return container.querySelector(address.elSel)
}

// Run renderFn, then restore focus (and text selection) lost to innerHTML
// replacement. Exported for page-driven re-renders outside the controller
// (filter changes, load-more, accordion re-renders).
export function renderPreservingFocus(root, renderFn) {
  const active = document.activeElement
  const address = active ? _focusAddress(active, root) : null
  let selection = null
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
    selection = { start: active.selectionStart, end: active.selectionEnd }
  }
  renderFn()
  if (!address) return
  const restored = _findElement(address, root)
  if (!restored) return
  restored.focus({ preventScroll: true })
  if (selection && typeof restored.setSelectionRange === 'function') {
    try { restored.setSelectionRange(selection.start, selection.end) } catch { /**/ }
  }
}

// ── Controller factory ────────────────────────────────────────────────────────
//
// Options:
//   page        registry key (router page name) — unmount target
//   fetch       async () => data — fresh data; may throw
//   render      (data) => void — idempotent DOM update; focus-preserved
//   pollMs      number | () => number — omit for fetch-once pages
//   pollEnabled optional () => boolean — pause/resume without killing the timer
//   root        () => Element — focus scope (default #content)
//   tableBody   () => Element — tbody for the default loading/error states
//   onError     optional (e, mode) => void — mode is 'initial' | 'manual' | 'poll';
//               defaults: initial → renderTableState error + Retry,
//                         manual  → toast, poll → offline banner
//   onLeave     optional () => void — cleanup when the page is left or remounted
export function createPageController(options = {}) {
  const {
    page,
    fetch: fetcher,
    render,
    pollMs,
    pollEnabled,
    root,
    tableBody,
    onError,
    onLeave,
  } = options

  const resolve = (v) => (typeof v === 'function' ? v() : v)
  const rootEl = () => resolve(root) || document.getElementById('content')

  let _timer = null
  let _active = false
  let _gen = 0            // bumped on unmount/remount — invalidates stale loads
  let _inFlight = null
  let _inFlightGen = -1

  function _handleError(e, mode) {
    if (onError) { onError(e, mode); return }
    if (mode === 'poll') { setOfflineBanner(true); return }
    if (mode === 'manual') { toast(e.message, 'error'); return }
    const body = resolve(tableBody)
    if (body) {
      renderTableState(body, 'error', e.message, { onRetry: () => { void mount() } })
    } else {
      toast(e.message, 'error')
    }
  }

  async function _load(mode) {
    // Concurrent callers within the same generation share one fetch; a caller
    // after a remount always starts fresh.
    if (_inFlight && _inFlightGen === _gen) return _inFlight
    const gen = _gen
    _inFlightGen = gen
    _inFlight = (async () => {
      try {
        const data = await fetcher()
        if (!_active || gen !== _gen) return false
        renderPreservingFocus(rootEl(), () => render(data))
        setOfflineBanner(false)
        return true
      } catch (e) {
        if (!_active || gen !== _gen) return false
        _handleError(e, mode)
        return false
      } finally {
        if (_inFlightGen === gen) { _inFlight = null; _inFlightGen = -1 }
      }
    })()
    return _inFlight
  }

  async function mount() {
    unmount()
    _active = true
    _gen += 1
    const body = resolve(tableBody)
    if (body) renderTableState(body, 'loading')
    const ok = await _load('initial')
    if (!_active) return ok
    const ms = resolve(pollMs)
    if (!ms) return ok
    _timer = setInterval(() => {
      if (!_active) { clearInterval(_timer); _timer = null; return }
      if (pollEnabled && !pollEnabled()) return
      void _load('poll')
    }, ms)
    return ok
  }

  function unmount() {
    const wasActive = _active
    _active = false
    _gen += 1
    if (_timer) { clearInterval(_timer); _timer = null }
    if (wasActive) onLeave?.()
  }

  const controller = {
    mount,
    refresh: () => _load('manual'),
    pollNow: () => _load('poll'),
    unmount,
    get active() { return _active },
  }
  if (page) _registry.set(page, controller)
  return controller
}
