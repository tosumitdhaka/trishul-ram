"""V18-01 §4 start authorization — HMAC-SHA256 mint + validate (stdlib only).

The token is the ``|``-joined payload
``attempt_id|run_id|generation|slot_id|worker_session|issued_at_unix|ttl_s``
followed by ``|`` + the hex HMAC-SHA256 of that payload, keyed by the
manager–worker session secret established at ``/agent/handshake`` (riding the
existing ``APIKeyMiddleware`` channel).  Carrying the payload inside the token
keeps validation self-contained: the worker parses the fields back out and
re-checks every claim against the secret before trusting them.

Validation order is parse → signature → session → skew → expiry → TTL, so a
tampered payload is rejected as malformed or as a bad signature regardless of
which check would trip first.  Key rotation: the previous secret stays valid
for the rotation overlap (max TTL + skew, 605 s at defaults); the caller
passes it as ``previous_secret`` and drops it once the overlap has elapsed —
the token's own ``issued_at + ttl`` bounds how long an old-secret token can
live, so no rotation timestamp is needed here.

Deliberately stdlib-``hmac``/``hashlib`` only — the worker image must not
depend on extra auth libraries.  All times are injected (``issued_at_unix``,
``now_unix``) so callers and tests drive a deterministic clock.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

_FIELD_COUNT = 7
_SEPARATOR = "|"


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of ``validate_start_authorization``.

    ``valid=False`` decisions carry a ``reason``; valid decisions carry every
    parsed field from the token payload.
    """

    valid: bool
    reason: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None
    generation: int | None = None
    slot_id: str | None = None
    issued_at_unix: int | None = None
    ttl_s: int | None = None


def _invalid(reason: str) -> AuthorizationResult:
    return AuthorizationResult(valid=False, reason=reason)


def _payload(*, attempt_id: str, run_id: str, generation: int, slot_id: str, worker_session: str, issued_at_unix: int, ttl_s: int) -> str:
    return _SEPARATOR.join(
        (attempt_id, run_id, str(generation), slot_id, worker_session, str(issued_at_unix), str(ttl_s))
    )


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_start_authorization(
    *,
    attempt_id: str,
    run_id: str,
    generation: int,
    slot_id: str,
    worker_session: str,
    ttl_s: int,
    secret: str,
    issued_at_unix: int,
) -> str:
    """Mint a start-authorization token for an ``/agent/run`` dispatch.

    Bounds (``ttl_s <= max_ttl_s``, non-negative TTL) are enforced at
    validation time, not here — the manager may mint with any TTL and the
    worker's validator applies the configured cap.  Fields containing the
    ``|`` separator are rejected up front because they would make the payload
    unparseable (the frozen format has no escaping).
    """
    fields = (attempt_id, run_id, str(generation), slot_id, worker_session, str(issued_at_unix), str(ttl_s))
    if any(_SEPARATOR in field for field in fields):
        raise ValueError("start-authorization fields must not contain '|'")
    payload = _payload(
        attempt_id=attempt_id,
        run_id=run_id,
        generation=generation,
        slot_id=slot_id,
        worker_session=worker_session,
        issued_at_unix=issued_at_unix,
        ttl_s=ttl_s,
    )
    return payload + _SEPARATOR + _sign(payload, secret)


def validate_start_authorization(
    token: str,
    *,
    worker_session: str,
    current_secret: str,
    previous_secret: str | None = None,
    max_ttl_s: int,
    clock_skew_s: int,
    now_unix: int,
) -> AuthorizationResult:
    """Validate a start-authorization token.

    Rejects: malformed payload / unparseable fields, wrong signature (against
    current, then previous secret), ``worker_session`` mismatch (old session),
    ``issued_at`` in the future beyond skew, expiry (``issued_at + ttl <
    now``), and TTL beyond ``max_ttl_s``.  ``previous_secret`` is accepted for
    the rotation overlap — the old-secret token's own expiry bounds the
    window, so the caller simply stops passing it once max TTL + skew has
    elapsed since rotation.
    """
    parts = token.rsplit(_SEPARATOR, 1)
    if len(parts) != 2:
        return _invalid("malformed token")
    payload, mac = parts
    fields = payload.split(_SEPARATOR)
    if len(fields) != _FIELD_COUNT:
        return _invalid("malformed token payload")
    attempt_id, run_id, generation_raw, slot_id, token_session, issued_at_raw, ttl_raw = fields
    try:
        generation = int(generation_raw)
        issued_at_unix = int(issued_at_raw)
        ttl_s = int(ttl_raw)
    except ValueError:
        return _invalid("malformed token fields")

    expected = _sign(payload, current_secret)
    if not hmac.compare_digest(mac, expected):
        if previous_secret is None or not hmac.compare_digest(mac, _sign(payload, previous_secret)):
            return _invalid("bad signature")

    if token_session != worker_session:
        return _invalid("worker session mismatch")
    if issued_at_unix > now_unix + clock_skew_s:
        return _invalid("issued in the future")
    if ttl_s < 0:
        return _invalid("negative ttl")
    if ttl_s > max_ttl_s:
        return _invalid("ttl exceeds max")
    if issued_at_unix + ttl_s < now_unix:
        return _invalid("expired")

    return AuthorizationResult(
        valid=True,
        attempt_id=attempt_id,
        run_id=run_id,
        generation=generation,
        slot_id=slot_id,
        issued_at_unix=issued_at_unix,
        ttl_s=ttl_s,
    )