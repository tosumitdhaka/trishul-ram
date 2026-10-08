"""Unit tests for the V18-01 §4 start authorization (tram/agent/auth_tokens.py).

All times are injected (``issued_at_unix`` / ``now_unix``), so the tests run
on a deterministic clock.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

import tram.core.config as cfg_mod
from tram.agent.auth_tokens import (
    AuthorizationResult,
    mint_start_authorization,
    validate_start_authorization,
)

SECRET = "test-secret"
OLD_SECRET = "old-secret"
T0 = 1_800_000_000
DEFAULT_TTL = 300
MAX_TTL = 600
SKEW = 5


def _mint(
    *,
    attempt_id: str = "a1",
    run_id: str = "r1",
    generation: int = 1,
    slot_id: str = "s1",
    worker_session: str = "ws-1",
    ttl_s: int = DEFAULT_TTL,
    secret: str = SECRET,
    issued_at_unix: int = T0,
) -> str:
    return mint_start_authorization(
        attempt_id=attempt_id,
        run_id=run_id,
        generation=generation,
        slot_id=slot_id,
        worker_session=worker_session,
        ttl_s=ttl_s,
        secret=secret,
        issued_at_unix=issued_at_unix,
    )


def _validate(
    token: str,
    *,
    worker_session: str = "ws-1",
    current_secret: str = SECRET,
    previous_secret: str | None = None,
    max_ttl_s: int = MAX_TTL,
    clock_skew_s: int = SKEW,
    now_unix: int = T0,
) -> AuthorizationResult:
    return validate_start_authorization(
        token,
        worker_session=worker_session,
        current_secret=current_secret,
        previous_secret=previous_secret,
        max_ttl_s=max_ttl_s,
        clock_skew_s=clock_skew_s,
        now_unix=now_unix,
    )


def _raw_token(payload: str, secret: str = SECRET) -> str:
    """Hand-build a token whose payload is signed by ``secret``.

    Lets the tests exercise payload-parsing failures with an otherwise-valid
    signature (the mint helper cannot produce malformed payloads).
    """
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "|" + mac


# ── mint → validate round trip ──────────────────────────────────────────────


def test_round_trip_valid():
    token = _mint()
    result = _validate(token)
    assert result.valid
    assert result.reason is None
    assert result.attempt_id == "a1"
    assert result.run_id == "r1"
    assert result.generation == 1
    assert result.slot_id == "s1"
    assert result.issued_at_unix == T0
    assert result.ttl_s == DEFAULT_TTL


def test_round_trip_parses_string_ids():
    token = _mint(attempt_id="att-42", run_id="run-7", slot_id="slot-3", generation=12, ttl_s=120)
    result = _validate(token)
    assert result.valid
    assert result.attempt_id == "att-42"
    assert result.run_id == "run-7"
    assert result.slot_id == "slot-3"
    assert result.generation == 12
    assert result.ttl_s == 120


def test_mint_rejects_separator_in_fields():
    with pytest.raises(ValueError):
        _mint(attempt_id="a|1")


# ── signature ───────────────────────────────────────────────────────────────


def test_wrong_secret_rejected():
    token = _mint(secret=OLD_SECRET)
    result = _validate(token, current_secret=SECRET)
    assert not result.valid
    assert result.reason == "bad signature"


def test_empty_secret_refuses_validation():
    token = _mint(secret=SECRET)
    result = _validate(token, current_secret="")
    assert not result.valid
    assert result.reason == "no session secret configured"


def test_previous_secret_accepted():
    token = _mint(secret=OLD_SECRET, ttl_s=MAX_TTL)
    result = _validate(token, current_secret=SECRET, previous_secret=OLD_SECRET)
    assert result.valid


def test_previous_secret_rejected_without_rotation_overlap():
    token = _mint(secret=OLD_SECRET)
    result = _validate(token, current_secret=SECRET)
    assert not result.valid
    assert result.reason == "bad signature"


def test_previous_secret_rejected_after_overlap_window():
    # Overlap = max TTL + skew = 605 s: an old-secret token minted at the
    # rotation point with the max TTL lives exactly until issued_at + ttl,
    # so it is valid at t=+600 and expired at t=+605.
    token = _mint(secret=OLD_SECRET, ttl_s=MAX_TTL)
    at_boundary = _validate(token, current_secret=SECRET, previous_secret=OLD_SECRET, now_unix=T0 + MAX_TTL)
    assert at_boundary.valid
    after_overlap = _validate(
        token,
        current_secret=SECRET,
        previous_secret=OLD_SECRET,
        now_unix=T0 + MAX_TTL + SKEW,
    )
    assert not after_overlap.valid
    assert after_overlap.reason == "expired"


# ── worker_session ──────────────────────────────────────────────────────────


def test_worker_session_mismatch_rejected():
    token = _mint(worker_session="ws-1")
    result = _validate(token, worker_session="ws-2")
    assert not result.valid
    assert result.reason == "worker session mismatch"


# ── issued_at skew ──────────────────────────────────────────────────────────


def test_future_issued_beyond_skew_rejected():
    token = _mint(issued_at_unix=T0 + SKEW + 1)
    result = _validate(token, now_unix=T0)
    assert not result.valid
    assert result.reason == "issued in the future"


def test_future_issued_within_skew_accepted():
    for ahead in (SKEW - 1, SKEW):
        token = _mint(issued_at_unix=T0 + ahead)
        result = _validate(token, now_unix=T0)
        assert result.valid, f"issued_at ahead by {ahead}s should be within skew"


# ── expiry ──────────────────────────────────────────────────────────────────


def test_expired_rejected():
    token = _mint(issued_at_unix=T0 - DEFAULT_TTL - 1)
    result = _validate(token, now_unix=T0)
    assert not result.valid
    assert result.reason == "expired"


def test_valid_exactly_at_expiry_boundary():
    token = _mint(issued_at_unix=T0 - DEFAULT_TTL, ttl_s=DEFAULT_TTL)
    result = _validate(token, now_unix=T0)
    assert result.valid


# ── TTL cap ─────────────────────────────────────────────────────────────────


def test_over_max_ttl_rejected():
    token = _mint(ttl_s=MAX_TTL + 1)
    result = _validate(token, now_unix=T0)
    assert not result.valid
    assert result.reason == "ttl exceeds max"


def test_max_ttl_boundary_accepted():
    token = _mint(ttl_s=MAX_TTL)
    result = _validate(token, now_unix=T0)
    assert result.valid


def test_negative_ttl_rejected():
    token = _mint(ttl_s=-5)
    result = _validate(token, now_unix=T0)
    assert not result.valid
    assert result.reason == "negative ttl"


# ── payload parse errors ────────────────────────────────────────────────────


def test_malformed_token_without_separator_rejected():
    result = _validate("not-a-token")
    assert not result.valid
    assert result.reason == "malformed token"


def test_wrong_field_count_rejected():
    result = _validate(_raw_token("a1|r1|1|s1|ws-1|1800000000"))
    assert not result.valid
    assert result.reason == "malformed token payload"


@pytest.mark.parametrize(
    "payload",
    [
        "a1|r1|not-an-int|s1|ws-1|1800000000|300",  # generation
        "a1|r1|1|s1|ws-1|not-an-int|300",  # issued_at_unix
        "a1|r1|1|s1|ws-1|1800000000|not-an-int",  # ttl_s
    ],
)
def test_non_integer_fields_rejected(payload):
    result = _validate(_raw_token(payload))
    assert not result.valid
    assert result.reason == "malformed token fields"


# ── env readers (tram/core/config.py) ───────────────────────────────────────


def test_auth_env_defaults(monkeypatch):
    monkeypatch.delenv("TRAM_AUTH_TOKEN_TTL_S", raising=False)
    monkeypatch.delenv("TRAM_AUTH_MAX_TTL_S", raising=False)
    monkeypatch.delenv("TRAM_AUTH_CLOCK_SKEW_S", raising=False)
    assert cfg_mod.auth_token_ttl_s() == 300
    assert cfg_mod.auth_max_ttl_s() == 600
    assert cfg_mod.auth_clock_skew_s() == 5


def test_auth_env_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_AUTH_TOKEN_TTL_S", "120")
    monkeypatch.setenv("TRAM_AUTH_MAX_TTL_S", "900")
    monkeypatch.setenv("TRAM_AUTH_CLOCK_SKEW_S", "10")
    assert cfg_mod.auth_token_ttl_s() == 120
    assert cfg_mod.auth_max_ttl_s() == 900
    assert cfg_mod.auth_clock_skew_s() == 10


def test_auth_env_invalid_raises(monkeypatch):
    monkeypatch.setenv("TRAM_AUTH_TOKEN_TTL_S", "not-an-int")
    with pytest.raises(ValueError):
        cfg_mod.auth_token_ttl_s()