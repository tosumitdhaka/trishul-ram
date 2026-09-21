"""Tests for the AI-audit append-only log (A10): ai_usage table + TRAM_AI_AUDIT flag."""
from __future__ import annotations

import logging

import pytest

from tram.core.config import ai_audit_enabled
from tram.persistence.db import TramDB


@pytest.fixture
def db(tmp_path):
    d = TramDB(url=f"sqlite:///{tmp_path}/test.db")
    yield d
    d.close()


def _entry(mode="generate", ok=True, ts="2026-09-21T00:00:00+00:00", tokens=(10, 25)):
    return dict(
        ts=ts, mode=mode, client="10.0.0.1", provider="anthropic",
        model="claude-haiku-4-5-20251001", tokens_in=tokens[0], tokens_out=tokens[1], ok=ok,
    )


def test_append_and_read_ai_usage(db):
    db.append_ai_usage(**_entry())
    rows = db.get_ai_usage()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"]
    assert row["ts"] == "2026-09-21T00:00:00+00:00"
    assert row["mode"] == "generate"
    assert row["client"] == "10.0.0.1"
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["tokens_in"] == 10
    assert row["tokens_out"] == 25
    assert row["ok"] == 1


def test_append_is_append_only_every_call_gets_new_id(db):
    first = _entry(ts="2026-09-21T00:00:01+00:00")
    db.append_ai_usage(**first)
    db.append_ai_usage(**first)  # same payload twice — must be two distinct rows
    assert len(db.get_ai_usage()) == 2


def test_tokens_null_when_not_reported(db):
    db.append_ai_usage(**_entry(tokens=(None, None)))
    row = db.get_ai_usage()[0]
    assert row["tokens_in"] is None
    assert row["tokens_out"] is None


def test_ok_false_recorded(db):
    db.append_ai_usage(**_entry(ok=False))
    assert db.get_ai_usage()[0]["ok"] == 0


def test_get_ai_usage_newest_first(db):
    db.append_ai_usage(**_entry(mode="a", ts="2026-09-21T00:00:01+00:00"))
    db.append_ai_usage(**_entry(mode="b", ts="2026-09-21T00:00:02+00:00"))
    assert [r["mode"] for r in db.get_ai_usage()] == ["b", "a"]


def test_get_ai_usage_limit(db):
    for i in range(3):
        db.append_ai_usage(**_entry(mode=f"m{i}", ts=f"2026-09-21T00:00:0{i}+00:00"))
    assert len(db.get_ai_usage(limit=2)) == 2


# ── TRAM_AI_AUDIT feature flag (A10) ────────────────────────────────────────


def test_ai_audit_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("TRAM_AI_AUDIT", raising=False)
    assert ai_audit_enabled() is True


def test_ai_audit_flag_off(monkeypatch):
    monkeypatch.setenv("TRAM_AI_AUDIT", "0")
    assert ai_audit_enabled() is False


def test_ai_audit_flag_unrecognized_value_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("TRAM_AI_AUDIT", "maybe")
    with caplog.at_level(logging.WARNING, logger="tram.core.config"):
        assert ai_audit_enabled() is True
    assert any("TRAM_AI_AUDIT" in rec.getMessage() for rec in caplog.records)