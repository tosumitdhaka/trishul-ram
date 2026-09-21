// ── Auth session state (shared between main.js and api.js) ──────────────────
// While a login overlay is pending, the router holds renders and api.js must
// not re-trigger the unauthorized event for the login call itself.

let _authPending = true

export function isAuthPending() {
  return _authPending
}

export function setAuthPending(value) {
  _authPending = Boolean(value)
}
